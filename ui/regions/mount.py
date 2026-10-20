"""The Regions page's whole-slide viewer (docs/region_qc_module_plan.md §2):
a third resident `Step1WholeSlideMount`, built with the real window like
Step3's (`MainWindow._step3_whole_slide`, ui/main_window.py:1931-1954), so
it shows Step1's data (raw or corrected channels by the Step0 decisions,
clipped to the ROI) in Regions' OWN display-only scope (user ruling
2026-10-20, docs/f5_tma_display_plan.md §13: ticks here only show; they never
change Step1's fusion). It follows Step1's Overlay / Fusion mode as Step3's
does.

The lifecycle mirrors Step3's: paused unless Regions is on screen, opened
on the first entry, re-synced when the handoff moves, closed when the
dataset moves and when the window closes. After every open / sync / entry
the overlay is (re)attached to the CURRENT viewport and raised (a source
change builds a new view; the GL layer raises itself on every resize).
"""

from __future__ import annotations

import os

from .overlay import RegionsOverlay


class RegionsViewerHost:
    def __init__(self, window, page):
        self.window = window
        self.page = page
        self.mount = None
        self.overlay = None
        self.refused_for = None

    # ── building ──
    def _build(self):
        if self.mount is None:
            from ..step1_viewer_mount import Step1WholeSlideMount
            w = self.window
            # its own display-only ticks (docs/f5_tma_display_plan.md §13):
            # ticking here never touches Step1's fusion
            mount = Step1WholeSlideMount(w, parent=w, camera_reason="regions", labels=False,
                                         display_scope="regions", display_only=True)
            mount.camera_sink = w._on_regions_camera
            mount.camera_jump_sink = w._on_regions_jump
            self.mount = mount
            # the Navigator click must not depend on Step1's or Step3's viewer
            # having been built (a direct Step0 -> Regions entry builds neither)
            from PyQt5.QtCore import Qt
            try:
                w._display.navigator_created.connect(w._wire_step1_tissue_navigation, Qt.UniqueConnection)
            except TypeError:
                pass
            layout = self.page.viewer_layout()
            if layout is not None:
                mount.install(layout, self.page.viewer_notice())
        return self.mount

    def view(self):
        m = self.mount
        if m is None or m.host.stack is None:
            return None
        return m.host.stack.view

    def attach_overlay(self):
        """(Re)attach the overlay to the viewer on screen now."""
        view = self.view()
        if view is None:
            return None
        if self.overlay is not None and self.overlay.viewport() is view.graphics.viewport() \
                and self.overlay.vb is view.view_box:
            self.overlay.raise_()
            return self.overlay
        if self.overlay is not None:
            self.overlay.detach()
        self.overlay = RegionsOverlay(view.graphics, view.view_box, self.page.ctl)
        try:
            view.sigRightClicked.connect(self._right_click)
        except TypeError:
            pass
        self.page.set_view(self.overlay)
        return self.overlay

    def _right_click(self, x, y):
        self.page.ctl.context(y, x)

    # ── lifecycle ──
    def follow(self, active, regions_step):
        """Paused unless Regions is on screen; opened / resumed on entry."""
        w = self.window
        if active != regions_step:
            if self.mount is not None:
                self.mount.pause_requests()
            return False
        if not getattr(w, "loader", None):
            return False
        path = str(getattr(w.loader, "filepath", "") or "")
        if self.refused_for == path:
            return False
        mount = self._build()
        if mount.host.stack is not None:
            try:
                mount.sync_source("handoff")
                if mount.paused:
                    mount.resume_requests()
                else:
                    mount.activate()
            except Exception as exc:                        # noqa: BLE001
                return self._failed(path, exc)
            if mount.host.stack is None:
                return self._failed(path, "the source could not be reopened")
            self._shown()
            return True
        reason = "the slide could not be opened"
        try:
            opened = mount.open()
        except Exception as exc:                            # noqa: BLE001
            opened, reason = None, exc
        if opened is None:
            return self._failed(path, reason)
        self.refused_for = None
        mount.set_mode(w._step1_preview_mode)
        self._shown()
        return True

    def _shown(self):
        self.mount.host.setVisible(True)
        notice = self.page.viewer_notice()
        if notice is not None:
            notice.setVisible(False)
        self.page.set_viewer_notice(None)
        self.attach_overlay()
        wire = getattr(self.window, "_wire_step1_tissue_navigation", None)
        if wire is not None:
            wire()
        self.page.refresh_view()

    def _failed(self, path, reason):
        print(f"[Regions-Viewer] whole-slide viewer unavailable: {reason}")
        self.refused_for = path
        try:
            self.mount.pause_requests()
        except Exception:                                   # noqa: BLE001
            pass
        self.mount.restore_legacy()
        self._drop_overlay()
        self.page.set_viewer_notice(f"The whole-slide view could not open: {reason}")
        return False

    def sync(self, reason="handoff"):
        """The handoff moved: rebind, and re-attach the overlay."""
        self.refused_for = None
        m = self.mount
        if m is None or m.host.stack is None:
            return False
        try:
            ok = bool(m.sync_source(reason))
        except Exception as exc:                            # noqa: BLE001
            print(f"[Regions-Viewer] rebind failed: {exc}")
            m.restore_legacy()
            self._drop_overlay()
            return False
        self.attach_overlay()
        return ok

    def set_mode(self, mode):
        if self.mount is not None:
            self.mount.set_mode(mode)

    def close(self):
        """The dataset moved / the window closes."""
        self.refused_for = None
        self._drop_overlay()
        if self.mount is None:
            return False
        try:
            self.mount.close()
        except Exception as exc:                            # noqa: BLE001
            print(f"[Regions-Viewer] close failed: {exc}")
        self.page.set_viewer_notice(None)
        return True

    def forget(self):
        self.close()
        self.mount = None

    def _drop_overlay(self):
        if self.overlay is not None:
            self.overlay.detach()
            self.overlay = None
        self.page.set_view(None)

    # ── camera ──
    def apply_camera(self, cx, cy, scale):
        m = self.mount
        return bool(m is not None and m.host.stack is not None and m.apply_camera(cx, cy, scale))

    def show_box(self, bbox):
        """Jump to a level-0 box (Prev / Next / a table row): published as a
        jump to the shared camera."""
        m = self.mount
        if m is None or m.host.stack is None:
            return False
        m.show_patch(tuple(int(v) for v in bbox))
        return True

    def jump_to_point(self, y, x, size):
        m = self.mount
        return bool(m is not None and m.host.stack is not None and m.jump_to_point(int(y), int(x), int(size)))


def slide_path_of(window):
    return os.path.abspath(str(getattr(getattr(window, "loader", None), "filepath", "") or ""))
