"""Step1's composed frame, driven by the owners rather than by whoever asks.

Block C3 of `docs/step1_rework_plan.md`. `Step1ComposeCoordinator` (C2) knows
how to plan and compose a frame; `step1_draft_spec` (C3) knows what a frame is
made of. This connects the two to the objects that actually move: the fusion
DRAFT, the shared display state, and the viewer's own source.

EVERY CHANGE BUILDS A WHOLE SPEC. A weight, a colour, a window, the ticks, the
mode: each of them ends here, and each of them reads both owners again and
hands the coordinator a complete, current spec. Nothing patches a spec in
place, and nothing reaches into the coordinator's private last frame -- a
half-updated draft is how a screen comes to show a state nobody is in.

EVERY CHANGE ADVANCES THE GENERATION. The old frame's queued reads are
cancelled and its late results are refused; the tiles it read stay in the tile
cache, so moving a weight re-composes without touching the disk.

WHAT A CHANGE DOES TO WHAT IS ALREADY ON SCREEN. Moving a weight, a colour
or a window re-composes the same coordinates, and each composed item is
replaced in place -- the picture stays up while it settles. A change of MODE
or of SOURCE is not that: the tiles on screen are a DIFFERENT picture, and one
left in the pool would come back the moment the user panned to it. Those
clear the composed layer, which is why this object is the one that knows the
reason.

THE DRAFT, NOT THE COMMITTED SNAPSHOT. What is on screen is what the user is
editing. What a job runs on is `committed_snapshot()`, which only a successful
Save replaces -- nothing here writes to the domain model at all.
"""

from PyQt5 import QtCore

from . import step1_draft_spec as draft_spec
from .step1_draft_spec import MODE_FUSION, MODE_OVERLAY, STEP1_SCOPE


class Step1ComposeBinding(QtCore.QObject):
    """Feeds one `Step1ComposeCoordinator` from the draft and the display.

    Owns neither: it reads them, and it writes only to the coordinator.
    """

    #: Reasons whose old pixels are a different picture rather than a
    #: coarser one. Everything else is replaced tile by tile.
    HARD_REASONS = frozenset({"mode", "source", "dataset", "draft-restored",
                              "state-installed"})

    def __init__(self, coordinator, domain, state, mode=MODE_OVERLAY,
                 scope=STEP1_SCOPE, layer=None, parent=None):
        super().__init__(parent)
        self._coordinator = coordinator
        self._domain = domain
        self._state = state
        self._mode = mode
        self._scope = scope
        self._layer = layer
        self._layer_linked = False
        self._connected = False

    # ── what a frame is ───────────────────────────────────────────────
    @property
    def mode(self):
        return self._mode

    def spec(self):
        """The current draft and display, as one composition spec."""
        return draft_spec.build_spec(self._domain, self._state, self._mode,
                                     scope=self._scope)

    # ── the wiring ────────────────────────────────────────────────────
    def attach_layer(self, layer):
        """Send the composed frames to the screen through `layer`.

        The coordinator announces tiles whether or not anything is drawing
        them (C2 gates it on its own), so this is a connection and not a
        mode: nothing about composition changes when a layer is or is not
        there.
        """
        self.detach_layer()
        self._layer = layer
        if layer is None:
            return False
        self._coordinator.tile_composed.connect(layer.on_tile_composed)
        self._layer_linked = True
        return True

    def detach_layer(self):
        """Stop drawing. Idempotent, and never raises on a dead layer."""
        if self._layer_linked and self._layer is not None:
            try:
                self._coordinator.tile_composed.disconnect(
                    self._layer.on_tile_composed)
            except (TypeError, RuntimeError):
                pass
        self._layer_linked = False
        self._layer = None
        return True

    def connect(self):
        """Follow both owners. Idempotent."""
        if self._connected:
            return False
        domain, state = self._domain, self._state
        if domain is not None:
            domain.draft_changed.connect(self._on_draft_changed)
            domain.draft_restored.connect(self._on_draft_restored)
            domain.dataset_bound.connect(self._on_dataset_bound)
        if state is not None:
            state.color_changed.connect(self._on_color_changed)
            state.mapping_changed.connect(self._on_mapping_changed)
            state.visibility_changed.connect(self._on_visibility_changed)
            state.state_installed.connect(self._on_state_installed)
        self._connected = True
        return True

    def disconnect_owners(self):
        """Stop following them. Idempotent, and never raises on a dead one."""
        if not self._connected:
            return False
        domain, state = self._domain, self._state
        for signal, slot in self._links(domain, state):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connected = False
        return True

    def _links(self, domain, state):
        links = []
        if domain is not None:
            links += [(domain.draft_changed, self._on_draft_changed),
                      (domain.draft_restored, self._on_draft_restored),
                      (domain.dataset_bound, self._on_dataset_bound)]
        if state is not None:
            links += [(state.color_changed, self._on_color_changed),
                      (state.mapping_changed, self._on_mapping_changed),
                      (state.visibility_changed, self._on_visibility_changed),
                      (state.state_installed, self._on_state_installed)]
        return links

    # ── the one way a frame is asked for ──────────────────────────────
    def refresh(self, reason):
        """Void the old frame and compose the current draft.

        THE ONLY entry: every signal below comes through here, so there is
        one place where a generation is advanced and one place where a spec
        is built.
        """
        self._coordinator.invalidate(reason)
        if self._layer is not None and reason in self.HARD_REASONS:
            self._layer.clear()
        return self._coordinator.compose_visible(self.spec())

    def recompose(self):
        """Ask for the visible tiles again WITHOUT voiding the frame.

        The camera moved: the draft is the same, the frames already composed
        are still the right ones, and the tiles that came into view are the
        only new work.
        """
        return self._coordinator.compose_visible(self.spec())

    def set_mode(self, mode):
        """Overlay or Fusion. A different picture, so a new generation."""
        mode = MODE_FUSION if mode == MODE_FUSION else MODE_OVERLAY
        if mode == self._mode:
            return False
        self._mode = mode
        self.refresh("mode")
        return True

    def source_changed(self, reason="source"):
        """The viewer rebound its source: another product, ROI or dataset."""
        return self.refresh(reason)

    # ── the owners' notices ───────────────────────────────────────────
    def _on_draft_changed(self):
        self.refresh("draft")

    def _on_draft_restored(self):
        self.refresh("draft-restored")

    def _on_dataset_bound(self, _identity):
        self.refresh("dataset")

    def _on_color_changed(self, _channel, _hexc):
        self.refresh("colour")

    def _on_visibility_changed(self, _channel, _visible):
        self.refresh("visibility")

    def _on_state_installed(self, _binding):
        self.refresh("state-installed")

    def _on_mapping_changed(self, channel):
        """A window settled -- the user's, or the seed the frame asked for.

        The coordinator is told WHICH channel, so it stops holding a seed
        request open for it, and is handed a whole new spec built from the
        state that now has the window. Nobody edits its last frame.
        """
        self._coordinator.window_arrived(str(channel), spec=self.spec())
