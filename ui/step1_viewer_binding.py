"""What Step1's whole-slide viewer is given, and by whom.

Block B3 of `docs/step1_rework_plan.md`. B1 decided which pixels may be drawn
and B2 built the host; this is the wiring between the host and the window's
own answers -- the handoff, the shared display state, and the navigation.

WHAT IT DOES NOT DO. It mounts nothing: the host stays unreachable from the
interface (B.5, re-ruled 2026-09-17), and Overlay/Fusion composition is C's.
It also writes nothing to either owner: every value here is READ from the
handoff or from `ChannelDisplayState`.

THE RULES IT KEEPS, each one a gate:

* the source is rebound when the handoff revision or a corrected product's
  identity moves -- the stack is rebuilt, so the controller's own generation
  refuses everything that was in flight. The VIEWPORT is carried across:
  saving a new decision must not throw the user back to the whole slide;
* the current channel is read in STEP1'S display scope, never in whichever
  scope another page happens to have left current;
* Intensity is the shared state's. A manual Min/Max/Gamma always wins; when
  nothing is stored the seed is ASKED FOR through
  `Block01DisplayServices.request_mapping_seed` and applied when it lands,
  through `mapping_changed`. This object keeps no "already seeded" or
  "already applied" set: whether a window exists is the shared state's
  answer, the in-flight de-duplication is the seed worker's, and the
  controller skips a repaint for a value it already has;
* a handoff change does NOT clear a window. The identity token is about
  pixel caches and late results, not about the user's numbers;
* a patch button and a click on the shared Tissue Preview are the same
  gesture here: both call `jump_to` on the one controller;
* with no patch at all the viewer still opens, on the whole slide.
"""

from PyQt5 import QtCore

from .step1_viewer_host import Step1ViewerHost

#: The display scope Step1's own answers live in (`MainWindow._DISPLAY_SCOPES`).
STEP1_SCOPE = "step1"


class Step1ViewerBinding(QtCore.QObject):
    """Feeds one `Step1ViewerHost` from a window's handoff and display state.

    The window is passed in rather than reached for: everything this needs is
    an attribute the window already publishes, and a test can hand over a
    plain object with the same ones.
    """

    def __init__(self, window, host=None, parent=None):
        super().__init__(parent)
        self._window = window
        self.host = host if host is not None else Step1ViewerHost()
        self._connected = False
        self._token = None

    # ── what the window says ──────────────────────────────────────────
    @property
    def _display(self):
        return getattr(self._window, "_display", None)

    @property
    def _state(self):
        return getattr(self._display, "state", None)

    def dataset_path(self):
        loader = getattr(self._window, "loader", None)
        return str(getattr(loader, "filepath", "") or "")

    def decisions(self):
        return dict(getattr(self._window, "_corrected_decisions", {}) or {})

    def corrected_zarr_path(self):
        return str(getattr(self._window, "_corrected_zarr_path", "") or "")

    def roi_name(self):
        roi = getattr(self._window, "_active_roi", None) or {}
        return str(roi.get("name") or "")

    def roi_bbox(self):
        """The analysis region, or None for a full-slide project."""
        roi = getattr(self._window, "_active_roi", None) or {}
        bbox = roi.get("bbox_fullres")
        if bbox and len(bbox) == 4:
            return tuple(int(v) for v in bbox)
        return None

    def handoff_revision(self):
        """Whatever the handoff calls this version of itself."""
        output = getattr(self._window, "step0_output", None) or {}
        for key in ("channel_remap_config_hash", "handoff_hash",
                    "step0_manifest_path"):
            value = output.get(key)
            if value:
                return str(value)
        return ""

    def current_channel(self):
        """STEP1'S current channel -- read in Step1's own display scope.

        Reading "the current scope" would hand back Step0's choice whenever
        the user is standing there, which is exactly what the per-step
        separation exists to prevent.
        """
        state = self._state
        if state is None:
            return ""
        using = getattr(state, "using_scope", None)
        if using is None:
            return str(state.selected_channel() or "")
        with using(STEP1_SCOPE):
            return str(state.selected_channel() or "")

    # ── opening and rebinding ─────────────────────────────────────────
    def open(self, channel=""):
        """Show the current channel of the current handoff.

        Rebinds when the source identity moved -- a republished handoff, a
        regenerated product, another ROI -- and carries the viewport across
        so the user stays where they were looking.
        """
        path = self.dataset_path()
        if not path:
            return None
        channel = str(channel or self.current_channel() or "")
        kwargs = dict(decisions=self.decisions(),
                      corrected_zarr_path=self.corrected_zarr_path(),
                      roi_name=self.roi_name(), roi_bbox=self.roi_bbox(),
                      handoff_revision=self.handoff_revision())

        keep = None
        if self.host.stack is not None and self._source_moved(**kwargs):
            keep = self._viewport()
            self.host.teardown()

        stack = self.host.open(path, channel, **kwargs)
        if stack is None:
            return None
        self._token = self.host.stack.provider.source_identity()
        self._connect()
        if keep is not None:
            self.host.jump_to(*keep)
        self.apply_intensity(channel)
        return stack

    def source_moved(self):
        """Would the CURRENT window answers give a different source?

        The public form of the check `open` makes: the handoff revision, the
        decisions, the corrected product and the ROI all fold into one
        identity token, and any of them moving means the pixels this viewer
        may draw are not the ones it is drawing.
        """
        if self.host.stack is None:
            return False
        return self._source_moved(
            decisions=self.decisions(),
            corrected_zarr_path=self.corrected_zarr_path(),
            roi_name=self.roi_name(), roi_bbox=self.roi_bbox(),
            handoff_revision=self.handoff_revision())

    def _source_moved(self, **kwargs):
        """Would a new table answer differently from the live one?"""
        from ..viewer.step1_source import Step1SourceTable

        table = Step1SourceTable(**kwargs)
        current = getattr(self._token, "corrected_artifact", None)
        return table.identity_token() != current

    def _viewport(self):
        """The camera's level-0 rectangle, as `jump_to` takes it."""
        stack = self.host.stack
        if stack is None:
            return None
        rect = stack.view.view_box.viewRect()
        return (int(rect.y()), int(rect.x()),
                max(1, int(rect.width())), max(1, int(rect.height())))

    # ── navigation: one controller, two gestures ──────────────────────
    def show_patch(self, bbox):
        """A patch button: an ANCHOR, not a different picture."""
        if not bbox or len(bbox) != 4:
            return False
        y0, y1, x0, x1 = (int(v) for v in bbox)
        return self.host.jump_to(y0, x0, max(1, x1 - x0), max(1, y1 - y0))

    def jump_to_point(self, y, x, size):
        """A click on the shared Tissue Preview, through the same entry."""
        size = max(1, int(size))
        return self.host.jump_to(int(y) - size // 2, int(x) - size // 2,
                                 size, size)

    # ── Intensity: the shared state's, never this object's ────────────
    def _connect(self):
        state = self._state
        if self._connected or state is None:
            return
        state.mapping_changed.connect(self._on_mapping_changed)
        state.state_installed.connect(self._on_state_installed)
        self._connected = True

    def apply_intensity(self, channel=""):
        """Give the host the window the shared state has, or ask for one."""
        state = self._state
        if state is None or self.host.stack is None:
            return False
        channel = str(channel or self.host.channel or "")
        if not channel:
            return False
        mapping = state.mapping_or_seed(channel)
        if mapping is None:
            display = self._display
            request = getattr(display, "request_mapping_seed", None)
            if request is not None:
                request(channel)
            return False
        self.host.apply_display_mapping(*mapping, channel=channel)
        return True

    def _on_mapping_changed(self, channel):
        """A window settled anywhere -- the user's, or the seed's."""
        if self.host.stack is None or str(channel) != self.host.channel:
            return
        state = self._state
        mapping = None if state is None else state.mapping(str(channel))
        if mapping is not None:
            self.host.apply_display_mapping(*mapping, channel=str(channel))

    def _on_state_installed(self, _binding):
        """A whole-state install: replay the current channel's window once."""
        self.apply_intensity()

    # ── the channel ───────────────────────────────────────────────────
    def follow_channel(self, channel=""):
        """Draw Step1's current channel, keeping the camera."""
        channel = str(channel or self.current_channel() or "")
        if not channel or self.host.stack is None:
            return False
        moved = self.host.set_channel(channel)
        if moved:
            self.apply_intensity(channel)
        return moved

    def close(self):
        state = self._state
        if self._connected and state is not None:
            try:
                state.mapping_changed.disconnect(self._on_mapping_changed)
                state.state_installed.disconnect(self._on_state_installed)
            except (TypeError, RuntimeError):
                pass
        self._connected = False
        self.host.teardown()
