"""Step1's whole-slide viewer, in the tab the user already has.

Block C4 of `docs/step1_rework_plan.md`. B built the host, C1-C3 the
composition, its planning and its wiring to the owners; this is the object
that puts all of it on the existing Viewer tab and takes it down again.

NO NEW SURFACE (UI_SURFACE_RULES §4). The Viewer tab, the Overlay and Fusion
buttons, the patch buttons and the shared Tissue Preview are the ones that
were already there. What changes is which widget the Viewer tab shows and
where the picture comes from. The old patch renderer is KEPT, hidden, as the
rollback path until block D retires it.

ONE VIEW, ONE CONTROLLER, ONE CAMERA. Everything here goes through
`Step1ViewerBinding` -- the same object B gave the host -- so a patch button
and a click on the Tissue Preview stay the two gestures that call the ONE
`jump_to`, and Step0 -> Step1 keeps carrying the viewport rather than the
pixels.

WHEN STEP1 IS NOT ON SCREEN IT COMPOSES NOTHING. Colours, Intensity and the
draft are shared, so standing in Step0 and ticking channels there would
otherwise drive a background recomposition of a picture nobody is looking at.
Leaving Step1 disconnects the owners; coming back connects them and composes
ONCE, which is what picks up everything that moved while it was away.

WHAT THE INFORMATION LAYER SAYS. The view's own status badge, which B already
uses for "outside the analysis region", also carries a missing corrected
product and the channels whose window is still being computed. Three
sentences, one badge, no control: the other channels go on drawing and
navigation is never blocked.
"""

import math

from PyQt5 import QtCore

from .step1_compose_binding import Step1ComposeBinding
from .step1_compose_coordinator import Step1ComposeCoordinator
from .step1_composed_layer import Step1ComposedLayer
from .step1_draft_spec import MODE_FUSION, MODE_OVERLAY
from .step1_viewer_binding import Step1ViewerBinding

#: What Step1's existing mode buttons call the two pictures.
LEGACY_OVERLAY = "overlay"
LEGACY_FUSION = "fusion"


def _compose_mode(mode):
    return MODE_FUSION if str(mode) == LEGACY_FUSION else MODE_OVERLAY


class Step1WholeSlideMount(QtCore.QObject):
    """The whole-slide viewer's life inside Step1's Viewer tab."""

    def __init__(self, window, host=None, parent=None):
        super().__init__(parent)
        self._window = window
        # The host is built here in the product and handed in by a test that
        # wants a synthetic pyramid -- the same seam B gave the binding.
        self.viewer = Step1ViewerBinding(window, host=host)
        self.coordinator = None
        self.compose = None
        self.layer = None
        self._legacy = None
        self._container = None
        self._active = False
        self._mode = MODE_OVERLAY
        self._missing_windows = ()
        #: Where this viewer publishes its camera, set by the window. A
        #: plain callable, not a signal: one writer, one reader.
        self.camera_sink = None
        self._rect_connected = False

    # ── what the window gives it ──────────────────────────────────────
    @property
    def _display(self):
        return getattr(self._window, "_display", None)

    @property
    def _domain(self):
        return getattr(self._display, "fusion", None)

    @property
    def _state(self):
        return getattr(self._display, "state", None)

    @property
    def host(self):
        return self.viewer.host

    @property
    def active(self):
        return self._active

    # ── mounting ──────────────────────────────────────────────────────
    def install(self, container_layout, legacy_widget):
        """Take the Viewer tab's picture slot, and keep the old one.

        The legacy patch view is hidden rather than removed: it is the
        rollback path until D, and a widget that is gone cannot be a
        rollback.
        """
        self._container = container_layout
        self._legacy = legacy_widget
        index = container_layout.indexOf(legacy_widget)
        if index < 0:
            container_layout.addWidget(self.host)
        else:
            container_layout.insertWidget(index, self.host)
        stretch = container_layout.stretch(container_layout.indexOf(self.host))
        if stretch == 0:
            container_layout.setStretch(
                container_layout.indexOf(self.host), 1)
        legacy_widget.setVisible(False)
        self.host.setVisible(True)
        return True

    def restore_legacy(self):
        """Put the old patch view back on screen. The rollback path."""
        if self._legacy is None:
            return False
        self.host.setVisible(False)
        self._legacy.setVisible(True)
        return True

    # ── opening ───────────────────────────────────────────────────────
    def open(self, channel=""):
        """Open the slide and start composing the current draft."""
        stack = self.viewer.open(channel)
        if stack is None:
            return None
        if self.coordinator is None:
            self.coordinator = Step1ComposeCoordinator(
                self.host, seed_port=self._display, parent=self)
            self.coordinator.windows_missing.connect(self._on_windows_missing)
        if self.layer is None:
            self.layer = Step1ComposedLayer(stack)
        if self.compose is None:
            self.compose = Step1ComposeBinding(
                self.coordinator, self._domain, self._state, mode=self._mode,
                parent=self)
            self.compose.attach_layer(self.layer)
        self.layer.attach()
        self._connect_view_rect()
        # THE CAMERA SETTLING is what asks for the tiles that came into
        # view: the controller already decides when a gesture is over
        # (`gesture_quiet`), and a second timer here would be a second
        # opinion about the same thing.
        try:
            stack.controller.gesture_quiet.connect(
                self._on_gesture_quiet, QtCore.Qt.UniqueConnection)
        except TypeError:                       # already connected
            pass
        self.activate()
        return stack

    # ── being looked at, or not ───────────────────────────────────────
    def activate(self):
        """Step1 is on screen: follow the owners and compose once.

        The ONE refresh is what picks up a colour, an Intensity window or a
        draft edit made while the user was somewhere else.
        """
        if self.compose is None:
            return False
        self.compose.connect()
        self._active = True
        self.compose.refresh("step1-entered")
        self._refresh_notice()
        # THE RECTANGLE FOLLOWS THE STEP: coming back to Step1 takes the
        # Tissue Preview's current-view rectangle back from Step0.
        self._connect_view_rect()
        self.publish_view_rect()
        return True

    def deactivate(self):
        """Step1 is not on screen: stop following, and compose nothing.

        The frames already composed stay in the pool and on the camera they
        were composed for, so coming back is a repaint and not a reload.
        """
        if self.compose is None:
            return False
        self.compose.disconnect_owners()
        self._active = False
        return True

    # ── the two pictures ──────────────────────────────────────────────
    def set_mode(self, mode):
        """The existing Overlay / Fusion buttons, through the one binding."""
        self._mode = _compose_mode(mode)
        if self.compose is None:
            return False
        return self.compose.set_mode(self._mode)

    def source_changed(self, reason="source"):
        """The handoff, the product or the ROI moved.

        The stack is rebuilt by the viewer binding (which carries the
        viewport across), the composed layer goes with it -- its items are a
        picture of the OLD source -- and a new generation composes the new
        one.
        """
        stack = self.viewer.open()
        if stack is None:
            return None
        if self.layer is not None:
            # The pool belongs to the view that has just been rebuilt: the
            # old items live in a ViewBox nothing points at any more.
            self.layer.teardown()
        self.layer = Step1ComposedLayer(stack)
        self.layer.attach()
        if self.compose is not None:
            self.compose.attach_layer(self.layer)
            if self._active:
                self.compose.source_changed(reason)
        # A NEW STACK IS A NEW ViewBox: the rectangle would otherwise go on
        # following a view nobody is looking at.
        self._connect_view_rect()
        self.publish_view_rect()
        return stack

    def sync_source(self, reason="source"):
        """Rebind IF the handoff, the decisions, the product or the ROI moved.

        Asked whenever Step1 comes forward and whenever a handoff is applied:
        a source that has not moved costs nothing, and one that has must not
        go on being drawn from the old identity's tiles.
        """
        if self.host.stack is None:
            return False
        if not self.viewer.source_moved():
            return False
        return self.source_changed(reason) is not None

    # ── the shared camera port (C4.5a) ────────────────────────────────
    def current_camera(self):
        """`(cx, cy, scale)` of this viewer, or None.

        The SAME measurement Step0 answers with: the centre of what is on
        screen in level-0 coordinates, and screen pixels per level-0 pixel
        from the ViewBox's own `viewPixelSize`. Two different widgets can
        then be put at the same place without either of their aspect locks
        reshaping a rectangle on the way.
        """
        stack = self.host.stack
        view_box = getattr(getattr(stack, "view", None), "view_box", None)
        if view_box is None:
            return None
        try:
            (x0, x1), (y0, y1) = view_box.viewRange()
            per_pixel = float(view_box.viewPixelSize()[0])
        except Exception:                                   # noqa: BLE001
            return None
        width, height = float(x1) - float(x0), float(y1) - float(y0)
        if not (width > 0 and height > 0 and per_pixel > 0):
            return None
        if not all(math.isfinite(v) for v in (x0, x1, y0, y1, per_pixel)):
            return None
        return (float(x0) + width / 2.0, float(y0) + height / 2.0,
                1.0 / per_pixel)

    def apply_camera(self, cx, cy, scale):
        """Put the shared centre and scale on THIS viewer.

        The rectangle is solved for this widget's own size, so the camera
        arrives as a centre and a magnification rather than someone else's
        rectangle, and it goes through the ONE controller's `jump_to` -- the
        same entry a patch button and the Tissue Preview use -- so the tiles
        for where it lands are asked for in the same turn.
        """
        stack = self.host.stack
        view_box = getattr(getattr(stack, "view", None), "view_box", None)
        if view_box is None or not (scale > 0 and math.isfinite(scale)):
            return False
        try:
            w_px, h_px = float(view_box.width()), float(view_box.height())
        except Exception:                                   # noqa: BLE001
            return False
        if not (w_px > 0 and h_px > 0):
            return False
        width, height = w_px / float(scale), h_px / float(scale)
        moved = self.host.jump_to(int(round(cy - height / 2.0)),
                                  int(round(cx - width / 2.0)),
                                  max(1, int(round(width))),
                                  max(1, int(round(height))))
        self.publish_view_rect()
        return bool(moved)

    def publish_camera(self, reason=""):
        """Tell the window where this viewer is looking."""
        sink = self.camera_sink
        if sink is None:
            return False
        camera = self.current_camera()
        if camera is None:
            return False
        sink(camera, reason)
        return True

    # ── the Tissue Preview's current-view rectangle ───────────────────
    def view_rect_l0(self):
        """What this viewer can see, as level-0 `(y0, y1, x0, x1)`, or None."""
        stack = self.host.stack
        view_box = getattr(getattr(stack, "view", None), "view_box", None)
        if view_box is None:
            return None
        try:
            (x0, x1), (y0, y1) = view_box.viewRange()
        except Exception:                                   # noqa: BLE001
            return None
        if not all(math.isfinite(float(v)) for v in (x0, x1, y0, y1)):
            return None
        if float(x1) <= float(x0) or float(y1) <= float(y0):
            return None
        return (float(y0), float(y1), float(x0), float(x1))

    def publish_view_rect(self):
        """Draw THIS viewer's viewport on the shared Tissue Preview.

        The rectangle only: no frame is asked for, no pixels are read, and
        nothing is added to the popup. It is the same `set_current_view_rect`
        Step0 draws its own viewport with, and the step on screen owns it.
        """
        popup = self._navigator()
        overview = getattr(popup, "overview", None)
        if overview is None:
            return False
        rect = self.view_rect_l0()
        if rect is None:
            clear = getattr(overview, "clear_current_view_rect", None)
            if clear is not None:
                clear()
            return False
        overview.set_current_view_rect(rect)
        return True

    def _navigator(self):
        display = getattr(self._window, "_display", None)
        getter = getattr(display, "navigator", None)
        return None if getter is None else getter()

    def _connect_view_rect(self):
        """Follow the REAL ViewBox, once per stack.

        A rebuilt source means a new ViewBox; the flag lives on the stack so
        the new one is connected and the old one is not reconnected.
        """
        stack = self.host.stack
        view_box = getattr(getattr(stack, "view", None), "view_box", None)
        if view_box is None or getattr(stack, "_step1_rect_connected", False):
            return False
        try:
            view_box.sigRangeChanged.connect(self._on_range_changed)
        except (AttributeError, RuntimeError, TypeError):
            return False
        try:
            stack._step1_rect_connected = True
        except (AttributeError, RuntimeError):
            pass
        return True

    def _on_range_changed(self, *_args):
        """The camera moved -- by hand, by a jump, by a rebuild."""
        self.publish_view_rect()
        self.publish_camera("step1")

    # ── navigation: the same two gestures, the same camera ────────────
    def show_patch(self, bbox):
        moved = self.viewer.show_patch(bbox)
        self._recompose_for_the_camera()
        self.publish_view_rect()
        self.publish_camera("step1-patch")
        return moved

    def jump_to_point(self, y, x, size):
        moved = self.viewer.jump_to_point(y, x, size)
        self._recompose_for_the_camera()
        self.publish_view_rect()
        self.publish_camera("step1-preview")
        return moved

    def _on_gesture_quiet(self, _snapshot):
        self._recompose_for_the_camera()
        self.publish_view_rect()
        self.publish_camera("step1-gesture")

    def _recompose_for_the_camera(self):
        """The camera moved; the draft did not.

        No new generation: what is already composed is still the right
        picture, and the tiles that came into view are the only new work.
        """
        if self.compose is None or not self._active:
            return 0
        composed = self.compose.recompose()
        self._refresh_notice()
        return composed

    # ── the information layer ─────────────────────────────────────────
    def _on_windows_missing(self, channels):
        self._missing_windows = tuple(channels or ())
        self._refresh_notice()

    def notice(self):
        """The three sentences the badge may carry, in one string."""
        lines = []
        status = self.host.status
        if status:
            lines.append(status)
        missing = list(self.host.missing_notice or ())
        if missing:
            names = ", ".join(f"{m.channel} ({m.reason})" for m in missing)
            lines.append(
                f"No corrected pixels for {names}. Step0 decided on the "
                f"correction, but its product is not on disk; the channels "
                f"that do have one are drawn.")
        if self._missing_windows:
            names = ", ".join(sorted(self._missing_windows))
            lines.append(
                f"Working out the display window for {names}. The other "
                f"channels are drawn; this one joins them when it is ready.")
        return "\n".join(lines)

    def _refresh_notice(self):
        stack = self.host.stack
        if stack is None:
            return ""
        self.host.refresh_status()
        text = self.notice()
        stack.view.set_status_text(text)
        return text

    # ── teardown ──────────────────────────────────────────────────────
    def close(self):
        """Give everything back: the screen, the owners, the workers."""
        if self.compose is not None:
            self.compose.detach_layer()
            self.compose.disconnect_owners()
        if self.layer is not None:
            self.layer.teardown()
        if self.coordinator is not None:
            try:
                self.coordinator.windows_missing.disconnect(
                    self._on_windows_missing)
            except (TypeError, RuntimeError):
                pass
            self.coordinator.shutdown()
        self.viewer.close()
        self.compose = self.layer = self.coordinator = None
        self._active = False
        self.restore_legacy()
        return True
