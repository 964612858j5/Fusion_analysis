"""G3.2b.5A: does drawing a new Patch move the Step1 Viewer, and where?

The report: "in Step1's Tissue Preview, drawing a new Patch makes the main
Viewer image jump for a moment and then come back. The final position is the
same, but the wobble in between is unpleasant."

This measures rather than reads. A REAL `MainWindow` is built on the real
demo slide (read-only) so the Step1 whole-slide mount comes up on its real
GPU backend; the shared Tissue Preview is opened through the product entry;
and a new Patch is drawn with real `QMouseEvent`s on the popup's canvas, so
`patches_changed`, Step0's single geometry model, the async geometry
persist, `geometry_committed`, `MainWindow._on_step0_geometry_committed`,
`_on_patches` and `_rebuild_patch_buttons` all happen the way they do for a
user. Nothing calls `_on_patches` directly.

At every stage it records the camera (mount, ViewBox range, pixel size,
centre, scale, level-0 viewport rect), the geometry of every widget between
the window and the GPU surface, and the navigation/repaint counters -- so a
finding names the layer it belongs to instead of "it wobbles".

Honest limits: offscreen Qt. Geometry and events are real Qt layout and real
events, but this is not a photograph of the user's monitor.

Usage: python scripts/diagnose_step1_patch_draw_wobble.py OUT.json [label]
"""

import importlib.util
import json
import os
import pathlib
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "tests"))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

SLIDE = "/sda1/Fusion/benchmark/biopsy.ome.tif"
#: Never written; only compared against, which is all the product does.
MANIFEST = pathlib.Path("/tmp/g325a_step0_manifest.json")

# The tissue-preview suite's own real-MainWindow rig, borrowed rather than
# rebuilt: one rig for one window.
import test_block01_tissue_preview_contract as RIG  # noqa: E402


def real_window():
    """A MainWindow whose Tissue Preview and Viewer are the SAME real slide.

    G3.2b.5A measured a bench whose Tissue Preview was a 512x256 synthetic
    slide while the Viewer held the real 19480x21804 one: two coordinate
    systems, so a Patch drawn in the popup named a place the Viewer does not
    have, and "nothing moved" could not exclude anything. Here Step0's page,
    its overview panel and the Step1 mount are all given the real
    `OMETIFFLoader` for one file, and `identity()` below checks that they
    really did end up on it.

    Everything is READ-ONLY: the slide is opened for reading and no project
    is bound, so nothing is written.
    """
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.main_window import MainWindow

    loader = OMETIFFLoader(SLIDE)
    window = MainWindow()
    window.loader = loader
    page = window._step0
    page.loader = loader
    page.ome_path = SLIDE
    names = loader.channel_names()
    page.nucleus_channel = names[0]
    page.patches = []
    page._rebuild_channel_list()
    page.current_channel = names[1] if len(names) > 1 else names[0]
    page.overview.loader = loader
    page.overview.nuc_ch = names[0]
    page.overview.full_h, page.overview.full_w = loader.shape
    page._bind_panels_to_dataset()
    window.config.set_channels(names)
    window.config.set_nucleus(names[0], 1.0)
    window._all_patches = []
    window._preview_patch_idx = -1
    window._set_step_active(1)
    return window, loader


def identity(window, loader, popup):
    """Proof that BOTH ends are the same slide in the same coordinates."""
    page = window._step0
    panel = popup.overview
    mount = window._step1_mount
    provider = mount.host.stack.controller.provider
    level0 = tuple(int(v) for v in provider.level_shape(0))
    return {
        "loader_filepath": str(loader.filepath),
        "page_ome_path": str(page.ome_path),
        "loader_shape": [int(v) for v in loader.shape],
        "overview_full_hw": [int(panel.full_h), int(panel.full_w)],
        "viewer_level0_shape": list(level0),
        "viewer_source_identity": str(provider.source_identity()),
        "overview_downsample": int(getattr(panel, "ds", 0) or 0),
        "overview_grid": [int(getattr(panel, "ov_h", 0) or 0),
                          int(getattr(panel, "ov_w", 0) or 0)],
        "same_file": str(loader.filepath) == str(page.ome_path),
        "same_shape": ([int(panel.full_h), int(panel.full_w)] == list(level0)),
    }


def geom(widget):
    if widget is None:
        return None
    try:
        rect = widget.geometry()
        hint, mhint = widget.sizeHint(), widget.minimumSizeHint()
        return {"x": rect.x(), "y": rect.y(),
                "w": rect.width(), "h": rect.height(),
                "hint": (hint.width(), hint.height()),
                "min_hint": (mhint.width(), mhint.height()),
                "visible": bool(widget.isVisible())}
    except Exception:                                       # noqa: BLE001
        return None


class Counters(QtCore.QObject):
    """Navigation entries, range changes and real paints. Records only."""

    def __init__(self, window):
        super().__init__()
        self.n = {"show_patch": 0, "jump_to_point": 0, "host_jump_to": 0,
                  "controller_jump_to": 0, "range_changed": 0,
                  "paint": 0, "resize": 0, "layout_request": 0,
                  "on_patches": 0, "rebuild_buttons": 0,
                  "geometry_committed": 0, "patches_changed": 0}
        self.paint_cameras = []
        self.window = window
        self._watched = []

        mount = window._step1_mount
        host = mount.host
        controller = host.stack.controller

        def wrap(obj, name, key):
            real = getattr(obj, name, None)
            if real is None:
                return
            def called(*a, _real=real, _k=key, **kw):
                self.n[_k] += 1
                return _real(*a, **kw)
            setattr(obj, name, called)

        wrap(mount, "show_patch", "show_patch")
        wrap(mount, "jump_to_point", "jump_to_point")
        wrap(host, "jump_to", "host_jump_to")
        wrap(controller, "jump_to", "controller_jump_to")
        # ENTRY AND RETURN of the two handlers, so a stage can be pinned to
        # the inside of them rather than to "some time after the release".
        self.probe = None

        def on_patches(patches, _real=window._on_patches):
            self.n["on_patches"] += 1
            if self.probe is not None:
                self.probe.mark("8a_on_patches_entry")
            out = _real(patches)
            if self.probe is not None:
                self.probe.mark("8b_on_patches_return")
            return out

        def rebuild(patches, _real=window._rebuild_patch_buttons):
            self.n["rebuild_buttons"] += 1
            if self.probe is not None:
                self.probe.mark("9a_rebuild_buttons_entry")
            out = _real(patches)
            if self.probe is not None:
                self.probe.mark("9b_rebuild_buttons_return")
            return out

        window._on_patches = on_patches
        window._rebuild_patch_buttons = rebuild

        real_commit = window._on_step0_geometry_committed

        def committed(payload, _real=real_commit):
            self.n["geometry_committed"] += 1
            if self.probe is not None:
                self.probe.mark("7a_geometry_committed_entry")
            return _real(payload)

        window._on_step0_geometry_committed = committed
        try:
            window._step0.geometry_committed.disconnect(real_commit)
            window._step0.geometry_committed.connect(committed)
        except (TypeError, RuntimeError):
            pass

        try:
            controller.view.view_box.sigRangeChanged.connect(
                lambda *_: self.n.__setitem__("range_changed",
                                              self.n["range_changed"] + 1))
        except Exception:                                   # noqa: BLE001
            pass

    def watch_paints(self, camera_fn, *widgets):
        """Count real paint/resize/layout events on the viewer's widgets."""
        for widget in widgets:
            if widget is None:
                continue
            widget.installEventFilter(self)
            self._watched.append(widget)
        self._camera_fn = camera_fn

    def eventFilter(self, obj, event):                      # noqa: N802
        kind = event.type()
        if kind == QtCore.QEvent.Paint:
            self.n["paint"] += 1
            try:
                self.paint_cameras.append(self._camera_fn())
            except Exception:                               # noqa: BLE001
                pass
        elif kind == QtCore.QEvent.Resize:
            self.n["resize"] += 1
        elif kind == QtCore.QEvent.LayoutRequest:
            self.n["layout_request"] += 1
        return False

    def snapshot(self):
        return dict(self.n)


class Probe:
    def __init__(self, window):
        self.w = window
        self.mount = window._step1_mount
        self.host = self.mount.host
        self.controller = self.host.stack.controller
        self.counters = Counters(window)
        self.counters.probe = self
        self.counters.watch_paints(
            self.camera,
            self.controller.view.graphics.viewport(),
            getattr(self.mount, "widget", None),
            getattr(self.w, "viewer_tab", None))
        self.stages = []

    # ── the camera, five ways ────────────────────────────────────────
    def camera(self):
        out = {}
        try:
            out["mount_current_camera"] = list(self.mount.current_camera())
        except Exception:                                   # noqa: BLE001
            out["mount_current_camera"] = None
        box = self.controller.view.view_box
        try:
            (x0, x1), (y0, y1) = box.viewRange()
            per_px = float(box.viewPixelSize()[0])
            out["view_range"] = [x0, x1, y0, y1]
            out["view_pixel_size"] = per_px
            out["centre"] = [(x0 + x1) / 2.0, (y0 + y1) / 2.0]
            out["scale"] = (1.0 / per_px) if per_px > 0 else None
            out["viewport_l0_rect"] = [x0, y0, x1 - x0, y1 - y0]
        except Exception:                                   # noqa: BLE001
            pass
        out["viewport_epoch"] = getattr(self.controller,
                                        "_interaction_epoch", None)
        return out

    def geometry(self):
        w = self.w
        controller = self.controller
        splitter = None
        for child in w.findChildren(QtWidgets.QSplitter):
            if child.isVisible() and child.count() >= 2:
                splitter = child
                break
        return {
            "window": (w.width(), w.height()),
            "splitter_sizes": splitter.sizes() if splitter else None,
            "viewer_tab": geom(getattr(w, "viewer_tab", None)),
            "step1_page": geom(getattr(w, "_step1_page_widget", None)),
            "mount_widget": geom(getattr(self.mount, "widget", None)),
            "host": geom(getattr(self.host, "widget", None)
                         or getattr(self.host, "view", None)),
            "graphics_viewport": geom(controller.view.graphics.viewport()),
            "gpu_widget": geom(getattr(self.mount, "gpu_widget", None)
                               or getattr(self.mount, "_gpu_widget", None)),
            "patch_selector_row": geom(
                w._patch_sel_container.parentWidget()
                if getattr(w, "_patch_sel_container", None) is not None
                and w._patch_sel_container.parentWidget() is not None
                else getattr(w, "patch_cache_status", None)),
            "patch_buttons": len(w._patch_sel_btns),
            "patch_button_geoms": [geom(b) for b in w._patch_sel_btns],
            "live_orphan_buttons": sum(
                1 for b in w.findChildren(QtWidgets.QPushButton)
                if b.text().startswith("P") and b not in w._patch_sel_btns
                and b.isVisible()),
        }

    def mark(self, stage):
        self.stages.append({
            "stage": stage,
            "t_ms": round(time.perf_counter() * 1000.0, 3),
            "camera": self.camera(),
            "geometry": self.geometry(),
            "counters": self.counters.snapshot(),
            "patches": len(self.w._all_patches),
            "preview_patch_idx": self.w._preview_patch_idx,
        })
        return self.stages[-1]


def forced_frame(widget):
    """A FORCED frame of `widget`, as a digest. NOT a natural presentation.

    `QWidget.grab()` asks the widget to paint into a pixmap right now. It is
    therefore evidence about what the widget draws AT THIS MOMENT, and says
    nothing about whether some intermediate natural frame differed. Labelled
    accordingly everywhere it is used.
    """
    import hashlib
    if widget is None:
        return None
    pixmap = widget.grab()
    image = pixmap.toImage().convertToFormat(QtGui.QImage.Format_RGBA8888)
    ptr = image.bits()
    ptr.setsize(image.byteCount())
    raw = bytes(ptr)
    return {"w": image.width(), "h": image.height(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "forced": True,
            "note": "QWidget.grab() forces a paint; not a natural frame"}


def pump(ms=250):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        APP.processEvents()


def settle(window, ms=2500):
    """Hot steady state: let every tile in flight land before measuring."""
    pump(ms)


def commit_geometry(window, popup, probe):
    """Let the async geometry persist REACH its published callback.

    The worker itself needs a project on disk that this rig has none of, so
    its COMPLETION POINT is driven here -- which is the one thing the task
    allows to be controlled. Everything downstream is the product's own
    chain and is not touched: `_on_geometry_persist_published` emits
    `geometry_committed`, `MainWindow._on_step0_geometry_committed` receives
    it, and that is what calls `_on_patches`. Nothing calls `_on_patches`.
    """
    page = window._step0
    # THE WINDOW MUST BE BOUND TO A HANDOFF for the product to adopt any
    # commit at all -- `_on_step0_geometry_committed` compares the incoming
    # manifest path with the bound one and ignores anything else. That is
    # test DATA, not a bypass: the comparison itself still runs.
    manifest = str(MANIFEST)
    window.step0_output = dict(window.step0_output or {})
    window.step0_output["step0_manifest_path"] = manifest
    probe.mark("6_geometry_persist_submitted")
    page._on_geometry_persist_published({
        "step0_manifest_path": manifest,
        "revision": int(getattr(page, "_geometry_revision", 0)) + 1,
        "rois": list(getattr(page, "rois", []) or []),
        "patches": list(getattr(page, "patches", []) or []),
    })
    pump(120)
    probe.mark("7b_geometry_committed_returned")


def draw_patch(popup, probe, fraction=(0.20, 0.20, 0.80, 0.80)):
    """A real press / move / release on the Tissue Preview's canvas."""
    panel = popup.overview
    viewport = panel.gview.viewport()
    w, h = viewport.width(), viewport.height()
    x0, y0 = int(w * fraction[0]), int(h * fraction[1])
    x1, y1 = int(w * fraction[2]), int(h * fraction[3])

    def send(kind, pos, buttons):
        QtWidgets.QApplication.sendEvent(
            viewport,
            QtGui.QMouseEvent(kind, QtCore.QPointF(pos),
                              QtCore.Qt.LeftButton, buttons,
                              QtCore.Qt.NoModifier))

    probe.mark("1_stable_before_draw")
    send(QtCore.QEvent.MouseButtonPress, QtCore.QPoint(x0, y0),
         QtCore.Qt.LeftButton)
    pump(60)
    probe.mark("2_mouse_press")
    steps = 6
    for i in range(1, steps + 1):
        px = x0 + (x1 - x0) * i // steps
        py = y0 + (y1 - y0) * i // steps
        send(QtCore.QEvent.MouseMove, QtCore.QPoint(px, py),
             QtCore.Qt.LeftButton)
        pump(20)
    probe.mark("3_dragging")
    send(QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(x1, y1),
         QtCore.Qt.NoButton)
    probe.mark("4_mouse_release_returned")
    pump(120)
    probe.mark("5_after_patches_changed")
    commit_geometry(probe.w, popup, probe)
    pump(1500)
    probe.mark("10_first_natural_layout_and_paint")
    pump(2500)
    probe.mark("12_after_deferred_delete")
    settle(None, 1500)
    probe.mark("13_final_stable")


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "wobble.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g32b5a"

    window, loader = real_window()
    window.resize(1600, 1000)
    window.show()
    pump(400)
    mount = getattr(window, "_step1_mount", None)
    assert mount is not None and mount.host.stack is not None, (
        "the Step1 whole-slide mount did not come up")
    backend = getattr(mount, "backend", None)
    probe = Probe(window)

    # THE VIEWER MUST ACTUALLY BE ON SCREEN. Entering Step1 is two steps --
    # the window's page stack, then the right-hand tab -- and without both
    # the viewer subtree stays unmapped at its default 640x480 and never
    # paints, so a "nothing moved" reading would be about a hidden widget.
    window._stack.setCurrentWidget(window._step1_page_widget)
    pump(600)
    tabs, tab = getattr(window, "right_tabs", None), getattr(window, "viewer_tab", None)
    if tabs is not None and tab is not None:
        tabs.setCurrentWidget(tab)
    pump(600)
    assert tab.isVisible(), "the Step1 Viewer tab is still not on screen"
    assert window._step1_mount.host.stack.controller.view.graphics.viewport(
        ).isVisible(), "the viewer's graphics viewport is not on screen"

    popup = window._display.show_navigator()
    assert popup is not None, "the shared Tissue Preview did not open"
    # The popup's own panel is fed through the public adapter, with the SAME
    # loader and the same nucleus channel, so its thumbnail is this slide's.
    popup.set_overview_context(loader=loader,
                               nuc_ch=window._step0.nucleus_channel,
                               rois=[], patches=[],
                               dataset_token=str(loader.filepath))
    pump(500)
    # THE THUMBNAIL MUST REALLY BE THERE. Without pixels the panel's grid is
    # 0x0, every drag collapses to "too small -- treat it as a click" and the
    # release NAVIGATES instead of making a Patch: a first run measured
    # exactly that and would have blamed patch creation for a click's jump.
    deadline = time.perf_counter() + 90.0
    while time.perf_counter() < deadline:
        pump(250)
        panel = popup.overview
        if (int(getattr(panel, "ov_h", 0) or 0) > 8
                and int(getattr(panel, "ov_w", 0) or 0) > 8
                and panel.img_item.image is not None):
            break
    panel = popup.overview
    assert (int(getattr(panel, "ov_h", 0) or 0) > 8
            and int(getattr(panel, "ov_w", 0) or 0) > 8
            and panel.img_item.image is not None), (
        "the Tissue Preview never loaded the real slide's thumbnail "
        f"(grid={getattr(panel, 'ov_h', 0)}x{getattr(panel, 'ov_w', 0)}, "
        f"status={panel.status.text()!r}) -- refusing to draw on an empty "
        "canvas and call the result a reproduction")
    assert popup.overview.edit_policy().get("patch_edit") is True

    ident = identity(window, loader, popup)
    print("IDENTITY:", json.dumps(ident, indent=2))
    assert ident["same_file"], ident
    assert ident["same_shape"], (
        "the Tissue Preview and the Viewer are NOT the same slide: "
        f"{ident} -- stopping rather than measuring a mismatched bench")

    # HOT STEADY STATE before the gesture: nothing in flight, so a wobble
    # cannot be confused with tiles still arriving.
    settle(window, 3500)

    viewport_widget = window._step1_mount.host.stack.controller.view.graphics.viewport()
    frame_before = forced_frame(viewport_widget)
    before_patches = len(window._all_patches)
    panel_patches_before = len(popup.overview._patches)
    draw_patch(popup, probe)
    # THE GESTURE MUST HAVE MADE A PATCH. If it did not, whatever the camera
    # did belongs to some other branch and must not be reported as the
    # patch-drawing path.
    made = len(popup.overview._patches) - panel_patches_before
    print(f"PATCHES ADDED BY THE DRAG: {made}")
    assert made == 1, (
        f"the drag added {made} patches, not 1 -- the gesture did not take "
        "the patch-creation branch, so this run proves nothing about it")

    # ── CASE B: the selector row GROWING. One button cannot widen a row
    # enough to move a splitter, so the strongest remaining candidate --
    # "the Patch selector's size hint pushes the Viewer" -- is exercised
    # directly by committing a list that takes the row from 1 to 8 buttons,
    # through the very same product chain.
    grown = []
    manifest = str(MANIFEST)
    base = list(window.step0_output.get("patches") or [])
    probe.stages.append({"stage": "B0_before_growing_the_row", "t_ms": 0,
                         "camera": probe.camera(), "geometry": probe.geometry(),
                         "counters": probe.counters.snapshot(),
                         "patches": len(window._all_patches),
                         "preview_patch_idx": window._preview_patch_idx})
    for count in range(2, 9):
        many = [(0, 32, 0, 32)] * count
        window._step0.patches = list(many)
        window._step0._on_geometry_persist_published({
            "step0_manifest_path": manifest,
            "revision": 10 + count,
            "rois": [],
            "patches": list(many),
        })
        pump(220)
        grown.append(probe.mark(f"B_{count}_buttons"))

    frame_after = forced_frame(viewport_widget)

    report = {
        "label": label,
        "forced_frame_before": frame_before,
        "forced_frame_after": frame_after,
        "forced_frame_identical": (
            bool(frame_before and frame_after
                 and frame_before["sha256"] == frame_after["sha256"])),
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "slide": SLIDE,
        "backend": str(backend),
        "renderer": str(getattr(mount, "renderer", None)
                        or getattr(mount, "backend", None)),
        "identity": ident,
        "window_px": [window.width(), window.height()],
        "device_pixel_ratio": float(window.devicePixelRatioF()),
        "patches_before": before_patches,
        "patches_after": len(window._all_patches),
        "honesty": [
            "real MainWindow, real slide (read-only), real Step1 mount on its "
            "real backend, real QMouseEvent drag in the shared Tissue Preview",
            "`_on_patches` is never called directly; the whole signal chain runs",
            "offscreen Qt: real layout and real events, not a photograph",
        ],
        "stages": probe.stages,
        "grown_row_stages": [s["stage"] for s in grown],
        "paint_cameras": probe.counters.paint_cameras,
        "counters_final": probe.counters.snapshot(),
    }
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")

    first = probe.stages[0]["camera"]
    print(f"backend={backend} patches {before_patches} -> "
          f"{len(window._all_patches)}")
    for stage in probe.stages:
        cam = stage["camera"]
        centre, scale = cam.get("centre"), cam.get("scale")
        if centre is None or first.get("centre") is None:
            print(f"  {stage['stage']:34s} camera unavailable")
            continue
        dx = (centre[0] - first["centre"][0]) * (scale or 0)
        dy = (centre[1] - first["centre"][1]) * (scale or 0)
        g = stage["geometry"]
        print(f"  {stage['stage']:34s} d_screen_px=({dx:8.3f},{dy:8.3f}) "
              f"scale_ratio={scale / first['scale']:.9f} "
              f"vp={g['graphics_viewport']['w']}x{g['graphics_viewport']['h']} "
              f"split={g['splitter_sizes']} btns={g['patch_buttons']}")
    print("counters:", probe.counters.snapshot())
    try:
        window.close()
    except Exception:                                       # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
