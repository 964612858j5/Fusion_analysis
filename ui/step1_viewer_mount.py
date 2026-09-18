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

    # ── navigation: the same two gestures, the same camera ────────────
    def show_patch(self, bbox):
        moved = self.viewer.show_patch(bbox)
        self._recompose_for_the_camera()
        return moved

    def jump_to_point(self, y, x, size):
        moved = self.viewer.jump_to_point(y, x, size)
        self._recompose_for_the_camera()
        return moved

    def _on_gesture_quiet(self, _snapshot):
        self._recompose_for_the_camera()

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
