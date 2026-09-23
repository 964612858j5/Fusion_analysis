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

WHICH RENDERER DRAWS IT (G3, `docs/step1_gpu_demo_plan.md`). The picture is
composed on the GPU when this machine can: `Step1GpuLayer` is a sibling
overlay on the EXISTING ViewBox's own viewport, with the mouse passing
through it, fed by `Step1GpuBinding` from the EXISTING provider, scheduler
and raw cache. Nothing new owns a camera, a queue or a cache, and no control
is added anywhere. When the GPU backend is up the CPU composition below is
never built -- two compositors of one picture is the double work this exists
to remove. When it cannot start, the reason is logged, `backend` says
`cpu-fallback`, and the CPU path opens exactly as before: it is the rollback,
so not one line of it is deleted or rewritten.

WHAT THE INFORMATION LAYER SAYS. The view's own status badge, which B already
uses for "outside the analysis region", also carries a missing corrected
product and the channels whose window is still being computed. Three
sentences, one badge, no control: the other channels go on drawing and
navigation is never blocked.
"""

import inspect
import logging
import math

from PyQt5 import QtCore

from ..utils import perf_trace
from ..viewer import step1_compose as compose_core
from . import step1_draft_spec as draft_spec
from .step1_compose_binding import Step1ComposeBinding
from .step1_compose_coordinator import Step1ComposeCoordinator
from .step1_composed_layer import Step1ComposedLayer
from .step1_draft_spec import MODE_FUSION, MODE_OVERLAY, STEP1_SCOPE
from .step1_gpu_binding import BindingBudgets, Step1GpuBinding
from .step1_gpu_layer import DisplaySnapshot, Step1GpuLayer, ViewportSnapshot
from .step1_viewer_binding import Step1ViewerBinding

_log = logging.getLogger(__name__)

#: What Step1's existing mode buttons call the two pictures.
LEGACY_OVERLAY = "overlay"
LEGACY_FUSION = "fusion"

#: Which renderer this mount actually has on screen. A read-only diagnostic
#: for tests and the log -- NOT a control, and never a claim: a GPU setup
#: that failed says `cpu-fallback`, with the real reason beside it.
BACKEND_GPU = "gpu"
BACKEND_CPU_FALLBACK = "cpu-fallback"
BACKEND_LEGACY = "legacy"

#: THE G3 DEMO TEXTURE BUDGET, fixed. The GPU layer refuses a submission
#: whose active raw working set does not fit -- it fails closed, naming the
#: budget, rather than dropping a channel, lowering precision, falling back
#: to raw pixels or quietly asking for more VRAM. G4 is where a real-machine
#: measurement may move it; nothing here raises it on its own.
DEMO_GPU_RAW_TEXTURE_BYTES = 512 * 1024 * 1024

#: The per-channel supply limits handed to the G2 binding. A channel whose
#: complete coarse or current fine does not fit is REFUSED and named in the
#: badge the viewer already has; it is never drawn from partial data.
#:
#: THE FINE CAP IS MEASURED, not guessed (G3.2a,
#: `docs/benchmarks/step1_gpu_demo/2026-09-20_g3_2a_report.md`). On the demo
#: slide, at 512-pixel tiles, one fine tile is 1 MiB and an UNALIGNED
#: viewport's target level needs:
#:
#:     1380x900  Step1 viewer area  -> 12 tiles = 12 MiB
#:     1840x1250 Step1 viewer area  -> 15 tiles = 15 MiB
#:     2760x1900 Step1 viewer area  -> 30 tiles = 30 MiB
#:     3840x2160 full screen        -> 40 tiles = 40 MiB
#:
#: The old 16 MiB cap therefore refused an ordinary 1440p viewport outright
#: and left it on the whole-slide coarse for good. 48 MiB covers the largest
#: measured viewport with room for the planes carried across a zoom, and
#: eight active channels come to 8 x 48 MiB of fine plus the measured 1 MiB
#: of complete coarse each = 392 MiB, inside the fixed 512 MiB above. The
#: total is still the hard one: it fails closed, it is not raised here.
DEMO_GPU_COARSE_TILES_PER_CHANNEL = 1024
DEMO_GPU_COARSE_BYTES_PER_CHANNEL = 32 * 1024 * 1024
DEMO_GPU_FINE_TILES_PER_VIEWPORT = 256
DEMO_GPU_FINE_BYTES_PER_CHANNEL = 48 * 1024 * 1024


def demo_budgets():
    """The G3 demo supply budget. One place, so a test reads what ships."""
    return BindingBudgets(
        max_coarse_tiles_per_channel=DEMO_GPU_COARSE_TILES_PER_CHANNEL,
        max_coarse_plane_bytes_per_channel=DEMO_GPU_COARSE_BYTES_PER_CHANNEL,
        max_fine_tiles_per_viewport=DEMO_GPU_FINE_TILES_PER_VIEWPORT,
        max_fine_plane_bytes_per_channel=DEMO_GPU_FINE_BYTES_PER_CHANNEL,
    )


def _compose_mode(mode):
    return MODE_FUSION if str(mode) == LEGACY_FUSION else MODE_OVERLAY


def _takes_load_overview(factory):
    """Does this stack factory know the `load_overview` keyword?

    The factory is an injection point (`Step1ViewerHost(stack_factory=...)`)
    and a bench's or a test's own function is as valid as the product one.
    One that predates the keyword is left to build the stack exactly as it
    was written to: an overview too many is slow, an unexpected keyword is
    a crash.
    """
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):                         # not introspectable
        return False
    return any(p.kind is p.VAR_KEYWORD or p.name == "load_overview"
               for p in parameters)


class Step1WholeSlideMount(QtCore.QObject):
    """The whole-slide viewer's life inside Step1's Viewer tab."""

    def __init__(self, window, host=None, parent=None, *, gpu=True,
                 gpu_layer_factory=None):
        super().__init__(parent)
        self._window = window
        #: Whether this mount may take the GPU path at all, and how its
        #: layer is built. Constructor seams, not a user control: the
        #: product leaves both alone, and a test uses them to exercise the
        #: rollback path and a failed initialisation without breaking the
        #: real environment.
        self._gpu_wanted = bool(gpu)
        self._gpu_layer_factory = gpu_layer_factory
        # The host is built here in the product and handed in by a test that
        # wants a synthetic pyramid -- the same seam B gave the binding.
        self.viewer = Step1ViewerBinding(window, host=host)
        if self._gpu_wanted:
            self._defer_overview_to_the_cpu_fallback()
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
        #: The GPU backend, when it is the one on screen.
        self.gpu_layer = None
        self.gpu_binding = None
        self._backend = BACKEND_LEGACY
        self._gpu_reason = ""
        self._gpu_owners_connected = False
        #: A seed request can answer straight away, and that answer is a
        #: `mapping_changed`. One refresh at a time, so an owner's reply
        #: cannot re-enter the binding in the middle of a submission.
        self._gpu_refreshing = False
        #: Seeds already asked of the SHARED service, as `(source, channel)`
        #: -- the same key the CPU coordinator uses, for the same reason: a
        #: new source makes the same channel a different question.
        self._gpu_seed_requested = set()

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
        """Put the old patch view back on screen. The rollback path.

        WHAT COMES BACK IS THE OLD PATCH WIDGET, not this controller's
        layers: the line below HIDES the whole-slide host, ViewBox and all.
        So the controller's own viewport supply stays off -- turning it back
        on here would buy a round of reads for a widget nobody can see,
        which is the exact waste this block exists to remove.
        """
        if self._legacy is None:
            return False
        self.host.setVisible(False)
        self._legacy.setVisible(True)
        return True

    # ── opening ───────────────────────────────────────────────────────
    def open(self, channel=""):
        """Open the slide and start drawing the current draft.

        THE GPU BACKEND IS TRIED FIRST and the CPU composition is not built
        at all when it succeeds -- two compositors of the same picture is
        double work, and the point of the GPU path is to stop paying for it.
        A GPU that cannot start says so and the existing CPU path opens
        instead; nothing here reports a GPU that is not there.
        """
        with perf_trace.span("step1.entry.stack_open"):
            stack = self.viewer.open(channel)
        if stack is None:
            return None
        with perf_trace.span("step1.entry.gpu_start"):
            gpu_started = self._start_gpu_backend(stack)
        if gpu_started:
            self._connect_view_rect()
            self.activate()
            return stack
        self._load_overview_for_the_cpu_picture(stack)
        return self._start_cpu_backend(stack)

    # ── the overview nobody would have looked at (G3.2b.4D) ───────────
    def _defer_overview_to_the_cpu_fallback(self):
        """Build the stack without the synchronous coarsest-level read.

        `build_step1_stack` reads the whole slide's coarsest level on the
        GUI thread before the view is even ranged, and a corrected product
        has no pyramid, so that read is a whole-region reduction: measured
        at 9497 ms of the 9729 ms a source rebind cost on the real slide,
        97.6 % of it, with the event loop frozen for the duration. Every
        one of those pixels goes into the controller's own layers -- and
        the GPU takeover sets those layers to opacity 0
        (`_start_gpu_backend`), so nobody ever sees them.

        So the mount that WANTS the GPU asks for a stack without them, and
        the two paths where the takeover does not happen read the overview
        themselves, before the CPU composition is built. Nothing is made
        asynchronous and nothing is cached: the read is either not needed
        or it is done where it always was, on the way to the picture that
        needs it.
        """
        host = self.host
        factory = getattr(host, "stack_factory", None)
        if factory is None or not _takes_load_overview(factory):
            return False

        def _stack_without_overview(*args, **kwargs):
            kwargs.setdefault("load_overview", False)
            return factory(*args, **kwargs)

        host.stack_factory = _stack_without_overview
        return True

    @staticmethod
    def _load_overview_for_the_cpu_picture(stack):
        """Read the overview the stack factory was told to skip.

        The controller's layers are about to BE the picture, and without
        its channel's record nothing may be drawn or requested at all
        (`ExploreController._blocked_on_overview`). Synchronous, on the GUI
        thread, at the same point in the sequence the factory used to do it
        -- the CPU path is the rollback, so it pays exactly what it always
        paid.

        Asked of the controller's own cache first: a stack built by a
        factory that still reads the overview (an injected one, or a mount
        with no GPU wanted) has nothing to do here.
        """
        controller = getattr(stack, "controller", None)
        if controller is None:
            return False
        try:
            if controller.has_overview_record(controller.channel):
                return False
            controller.load_overview()
        except (AttributeError, RuntimeError):
            return False
        return True

    def _start_cpu_backend(self, stack):
        """The existing whole-slide CPU composition. The rollback path."""
        self._backend = BACKEND_CPU_FALLBACK
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

    # ── the GPU backend (G3) ──────────────────────────────────────────
    @property
    def backend(self):
        """`gpu`, `cpu-fallback` or `legacy`. Read-only; never a claim."""
        return self._backend

    def gpu_status(self):
        """Everything a test or a log line needs about the GPU backend."""
        layer, binding = self.gpu_layer, self.gpu_binding
        return {
            "backend": self._backend,
            "reason": self._gpu_reason,
            "raw_texture_budget_bytes": DEMO_GPU_RAW_TEXTURE_BYTES,
            "environment": (layer.environment_report()
                            if layer is not None else {}),
            "cache": layer.cache_stats() if layer is not None else {},
            "binding": binding.stats() if binding is not None else {},
        }

    def _build_gpu_layer(self, stack):
        factory = self._gpu_layer_factory
        if factory is not None:
            return factory(stack)
        return Step1GpuLayer(max_raw_texture_bytes=DEMO_GPU_RAW_TEXTURE_BYTES,
                             require_hardware=True)

    def _start_gpu_backend(self, stack):
        """Put the G1 layer on the EXISTING view and feed it from G2.

        Nothing is created here that the viewer already owns: the provider,
        the scheduler, the raw cache, the controller, the camera and the
        ViewBox are the stack's, and the layer is a sibling overlay on that
        ViewBox's own viewport with the mouse passing straight through it.
        """
        if not self._gpu_wanted or self.gpu_binding is not None:
            return False
        layer = None
        try:
            layer = self._build_gpu_layer(stack)
            if layer is None:
                self._gpu_reason = "no GPU layer was built"
                return False
            layer.attach(stack.view)
            if layer.width() <= 0 or layer.height() <= 0:
                layer.resize(1, 1)
            with perf_trace.span("step1.entry.gl_show"):
                layer.show()
            # Qt realizes a QOpenGLWidget's context lazily. Asking it for
            # its framebuffer once forces initializeGL NOW, so the backend
            # decision is made on what actually happened rather than on
            # what might happen at the first paint.
            with perf_trace.span("step1.entry.gl_initialize"):
                layer.grabFramebuffer()
            if not layer.initialized:
                raise layer.init_error or RuntimeError(
                    "the Step1 GPU layer did not initialize")
        except Exception as exc:                            # noqa: BLE001
            self._gpu_reason = f"{type(exc).__name__}: {exc}"
            _log.warning("Step1 GPU backend unavailable, using the CPU "
                         "composition instead: %s", self._gpu_reason)
            self._release_gpu_layer(layer)
            return False
        self.gpu_layer = layer
        self.gpu_binding = Step1GpuBinding(
            provider=stack.provider, scheduler=stack.scheduler,
            controller=stack.controller, layer=layer,
            build_display_snapshot=self._gpu_display_snapshot,
            build_viewport_snapshot=self._gpu_viewport_snapshot,
            budgets=demo_budgets(), parent=self)
        # The same switch the CPU composed layer uses: a composition is on
        # screen, so the single-channel layers this controller owns stop
        # painting under it. Visibility only -- no request is cancelled and
        # no shared controller behaviour is changed.
        stack.controller.set_marker_visible(False)
        # ...and now that those layers are demonstrably invisible, the
        # controller stops READING for them too. Measured (G3.2b.4B2, real
        # slide, real mount): 73 % of a cold Patch's provider reads and 79 %
        # of a cold Tissue landing's were for tiles that can never reach the
        # screen, competing with this binding's own multi-channel tiles for
        # the one scheduler. Only AFTER `initialized` is confirmed above, so
        # a GPU that failed to start leaves the old picture fully supplied.
        # The controller stays the one camera, the one mouse and the one
        # publisher of interaction/quiet/view-rect -- see
        # `ExploreController.set_viewport_requests_enabled`.
        self._set_controller_viewport_requests(stack.controller, False)
        self._backend = BACKEND_GPU
        self._gpu_reason = ""
        self._gpu_seed_requested.clear()
        self.gpu_binding.source_changed()
        return True

    def _stop_gpu_backend(self, restore_controller=True):
        """Give the GL names and the screen back. Idempotent.

        `restore_controller` says whether THIS controller's own viewport
        supply should be turned back on -- i.e. whether its layers are about
        to be the picture. That is true only for a real hand-over to the CPU
        whole-slide renderer on the SAME stack. It is false for the two
        callers this mount has today, and deliberately so:

        * a SOURCE REBUILD is about to destroy this controller and build a
          new one. Waking it up first buys one round of reads for a stack
          that is already on its way out, competing with the new stack for
          the same scheduler;
        * CLOSE is a teardown. Nothing may start a read on the way out.

        (A GPU that fails to start never disabled anything: the switch is
        only thrown after `layer.initialized` is confirmed, and a rebuilt
        stack brings a brand-new controller whose switch is on by default.
        So the CPU fallback is reached with the supply already on either
        way.)
        """
        binding, layer = self.gpu_binding, self.gpu_layer
        self.gpu_binding = self.gpu_layer = None
        self._disconnect_gpu_owners()
        if binding is None and layer is None:
            return False
        if binding is not None:
            binding.dispose()
        self._release_gpu_layer(layer)
        stack = self.host.stack
        controller = getattr(stack, "controller", None)
        if controller is not None:
            try:
                controller.set_marker_visible(True)
            except RuntimeError:                            # C++ side gone
                pass
            if restore_controller:
                # Its layers are the picture again, so they are supplied
                # again -- and `set_viewport_requests_enabled(True)`
                # re-issues for the CURRENT viewport once, so the user does
                # not have to move the camera to get pixels back.
                self._set_controller_viewport_requests(controller, True)
        if self._backend == BACKEND_GPU:
            self._backend = BACKEND_LEGACY
        return True

    @staticmethod
    def _set_controller_viewport_requests(controller, enabled):
        """Thin wiring to the controller's own public switch.

        Tolerant on purpose: a controller built by an older stack factory,
        or one whose C++ side has already gone, simply keeps the behaviour
        it has -- this is a performance gate, never a correctness one.
        """
        setter = getattr(controller, "set_viewport_requests_enabled", None)
        if setter is None:
            return False
        try:
            return bool(setter(bool(enabled)))
        except RuntimeError:                                # C++ side gone
            return False

    @staticmethod
    def _release_gpu_layer(layer):
        if layer is None:
            return False
        try:
            layer.dispose()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            layer.setParent(None)
            layer.deleteLater()
        except RuntimeError:                                # C++ side gone
            pass
        return True

    def _connect_gpu_owners(self):
        """Follow the same two owners the CPU binding follows. Idempotent."""
        if self._gpu_owners_connected:
            return False
        domain, state = self._domain, self._state
        for signal, slot in self._gpu_links(domain, state):
            signal.connect(slot)
        self._gpu_owners_connected = True
        return True

    def _disconnect_gpu_owners(self):
        if not self._gpu_owners_connected:
            return False
        for signal, slot in self._gpu_links(self._domain, self._state):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._gpu_owners_connected = False
        return True

    def _gpu_links(self, domain, state):
        links = []
        if domain is not None:
            links += [(domain.draft_changed, self._on_gpu_draft),
                      (domain.draft_restored, self._on_gpu_draft_restored),
                      (domain.dataset_bound, self._on_gpu_dataset_bound)]
        if state is not None:
            links += [(state.color_changed, self._on_gpu_color),
                      (state.mapping_changed, self._on_gpu_mapping),
                      (state.visibility_changed, self._on_gpu_visibility),
                      (state.state_installed, self._on_gpu_state_installed)]
        return links

    def _on_gpu_draft(self):
        self._refresh_gpu("draft")

    def _on_gpu_draft_restored(self):
        self._refresh_gpu("draft-restored")

    def _on_gpu_dataset_bound(self, _identity):
        self._refresh_gpu("dataset")

    def _on_gpu_color(self, _channel, _hexc):
        self._refresh_gpu("colour")

    def _on_gpu_mapping(self, _channel):
        self._refresh_gpu("intensity")

    def _on_gpu_visibility(self, _channel, _visible):
        self._refresh_gpu("visibility")

    def _on_gpu_state_installed(self, _binding):
        self._refresh_gpu("state-installed")

    def _refresh_gpu(self, reason):
        """The ONE way a display change reaches the GPU.

        `refresh_display()` re-submits the planes already resident with the
        spec as it now reads: no tile is asked of the scheduler, no raw
        texture is uploaded again, and nothing is read from disk. Only a
        channel that has just become scientifically active -- and therefore
        has no coarse at all -- starts a supply of its own.
        """
        binding = self.gpu_binding
        if binding is None or not self._active or self._gpu_refreshing:
            return False
        self._gpu_refreshing = True
        try:
            binding.refresh_display()
            self._refresh_notice()
        finally:
            self._gpu_refreshing = False
        del reason
        return True

    def _gpu_display_snapshot(self):
        """`build_spec()`'s answer, frozen into a G1 snapshot.

        A COPY AND NOTHING ELSE. `step1_draft_spec.build_spec` stays the one
        place that reads the draft and the shared display state; this copies
        its fields across and changes no number and no rule.
        """
        spec = draft_spec.build_spec(self._domain, self._state, self._mode,
                                     scope=STEP1_SCOPE)
        mappings = {str(channel): tuple(float(v) for v in value)
                    for channel, value in (spec.get("mappings") or {}).items()}
        self._note_gpu_missing_windows(spec, mappings)
        return DisplaySnapshot(
            mode=str(spec.get("mode") or MODE_OVERLAY),
            mappings=mappings,
            weights={str(channel): float(weight or 0.0)
                     for channel, weight in (spec.get("weights") or {}).items()},
            colors={str(channel): tuple(float(v) for v in value)
                    for channel, value in (spec.get("colors") or {}).items()},
            groups={str(group): {str(channel): float(weight or 0.0)
                                 for channel, weight in (members or {}).items()}
                    for group, members in (spec.get("groups") or {}).items()},
            group_weights={str(group): float(weight or 0.0) for group, weight
                           in (spec.get("group_weights") or {}).items()},
            nucleus=(str((spec.get("nucleus") or ("", 0.0))[0]),
                     float((spec.get("nucleus") or ("", 0.0))[1] or 0.0)),
        )

    @staticmethod
    def _gpu_spec_channels(spec):
        """The channels this spec actually composes from.

        THE COMPOSITION'S OWN ANSWER (`viewer.step1_compose`), the same two
        functions the CPU coordinator plans with -- not a second opinion
        written here. The drop rule lives in one place: a channel weight,
        a group weight or a nucleus weight of `0.0` is not read, not mapped
        and not composed, and a channel left out of THIS set is therefore
        never one whose Intensity window is worked out.
        """
        if str(spec.get("mode")) == MODE_FUSION:
            return compose_core.fusion_channels(spec.get("groups"),
                                                spec.get("group_weights"),
                                                spec.get("nucleus"))
        return compose_core.overlay_channels(spec.get("weights"))

    def _note_gpu_missing_windows(self, spec, mappings):
        """Ask the SHARED service for a window this frame has not got.

        No percentile is computed here and no second seed cache is kept:
        `Block01DisplayServices` answers whether a pass is outstanding, the
        window arrives through the shared `mapping_changed`, and the badge
        the viewer already has names the channels still being worked out.
        """
        missing = tuple(sorted(self._gpu_spec_channels(spec) - set(mappings)))
        for channel in missing:
            self._request_gpu_seed(channel)
        if missing != self._missing_windows:
            self._missing_windows = missing
        return missing

    def _request_gpu_seed(self, channel):
        source = self._gpu_source()
        key = (source, str(channel))
        if key in self._gpu_seed_requested:
            return False
        port = self._display
        request = getattr(port, "request_mapping_seed", None)
        if request is None:
            return False
        outstanding = bool(request(channel))
        if not outstanding:
            pending = getattr(port, "mapping_seed_pending", None)
            outstanding = bool(pending(channel)) if pending is not None else False
        if outstanding:
            self._gpu_seed_requested.add(key)
        return outstanding

    def _gpu_source(self):
        stack = self.host.stack
        provider = getattr(stack, "provider", None)
        return None if provider is None else provider.source_identity()

    def _gpu_viewport_snapshot(self):
        """What the ONE ViewBox can see, read from it and frozen.

        The camera is not this layer's: the world rectangle is the existing
        ViewBox's own `viewRange()`, and the output size is the widget Qt
        has already given the overlay.
        """
        layer = self.gpu_layer
        stack = self.host.stack
        view_box = getattr(getattr(stack, "view", None), "view_box", None)
        if layer is None or view_box is None:
            raise RuntimeError("no Step1 GPU viewport without a view")
        (x0, x1), (y0, y1) = view_box.viewRange()
        width = max(1, int(layer.width()))
        height = max(1, int(layer.height()))
        try:
            ratio = float(layer.devicePixelRatioF())
        except AttributeError:                              # very old Qt
            ratio = float(layer.devicePixelRatio())
        roi_rect = self._gpu_roi_world_rect(stack)
        return ViewportSnapshot((float(x0), float(x1), float(y0), float(y1)),
                                (width, height), ratio if ratio > 0 else 1.0,
                                roi_world_rect=roi_rect,
                                roi_polygon_world=self._gpu_roi_polygon(roi_rect))

    @staticmethod
    def _gpu_roi_world_rect(stack):
        """The analysis region for THIS stack, in the layer's rect order.

        Read from the live source table every submission, so a rebuilt
        stack -- another ROI, another handoff -- brings its own region and
        an old one cannot survive the rebind. `Step1SourceTable.roi_bbox()`
        answers `(y0, y1, x0, x1)`; the layer's rectangles are
        `(x0, x1, y0, y1)`. `None` (a whole-slide project) clips nothing.
        """
        table = getattr(getattr(stack, "provider", None), "source_table", None)
        bbox = None
        if table is not None:
            try:
                bbox = table.roi_bbox()
            except Exception:                               # noqa: BLE001
                bbox = None
        if not bbox or len(bbox) != 4:
            return None
        y0, y1, x0, x1 = (float(v) for v in bbox)
        return (x0, x1, y0, y1)

    def _gpu_roi_polygon(self, roi_rect):
        """The drawn shape for the region THIS stack is clipped to (G3.2c.1).

        Read from the window's active ROI, but only accepted when it belongs
        to `roi_rect`: the layer compares the polygon's own bounds with the
        rectangle and refuses a pair that does not match, so a polygon left
        over from another ROI cannot be stapled onto this source. With no
        rectangle there is no region at all and no polygon either.
        """
        if roi_rect is None:
            return None
        roi = getattr(self._window, "_active_roi", None) or {}
        points = roi.get("polygon_fullres")
        if not points:
            return None
        try:
            return tuple((float(x), float(y)) for x, y in points)
        except (TypeError, ValueError):
            # Shaped wrongly rather than absent: hand it on as given and let
            # the layer refuse it loudly -- silently dropping it here would
            # look exactly like "this ROI has no polygon".
            return tuple(points)

    # ── being looked at, or not ───────────────────────────────────────
    def activate(self):
        """Step1 is on screen: follow the owners and compose once.

        The ONE refresh is what picks up a colour, an Intensity window or a
        draft edit made while the user was somewhere else.
        """
        if self.gpu_binding is not None:
            self._connect_gpu_owners()
            self._active = True
            # THE ONE refresh. It re-submits what is already resident with
            # the spec as it now reads, and asks the scheduler for nothing
            # but a channel that became active while Step1 was away.
            self.gpu_binding.refresh_display()
            self._refresh_notice()
            self._connect_view_rect()
            self.publish_view_rect()
            return True
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
        if self.gpu_binding is not None:
            # The textures, the complete coarse and the current fine stay:
            # coming back is a submission, not a reload.
            self._disconnect_gpu_owners()
            self._active = False
            return True
        if self.compose is None:
            return False
        self.compose.disconnect_owners()
        self._active = False
        return True

    # ── the two pictures ──────────────────────────────────────────────
    def set_mode(self, mode):
        """The existing Overlay / Fusion buttons, through the one binding."""
        mode = _compose_mode(mode)
        if self.gpu_binding is not None:
            if mode == self._mode:
                return False
            self._mode = mode
            # A different picture, not a coarser one -- and the GPU draws
            # every submission from a cleared target, so nothing of the
            # other mode survives it. No tile is read and no raw texture is
            # uploaded again: the same planes are composed another way.
            return self._refresh_gpu("mode")
        self._mode = mode
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
        if self.gpu_binding is not None or self.gpu_layer is not None:
            return self._gpu_source_changed(reason)
        stack = self.viewer.open()
        if stack is None:
            return None
        # A mount that wanted the GPU and never got it rebuilds its stack
        # through the same overview-less factory, so this picture's
        # controller needs the read here too -- before the composition is
        # attached and asked to draw.
        self._load_overview_for_the_cpu_picture(stack)
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

    def _gpu_source_changed(self, reason="source"):
        """Another product, ROI or dataset, with the GPU backend on screen.

        The old binding and the old GL names go FIRST: the layer is a
        sibling of a ViewBox that is about to be destroyed, and a texture
        read from the old source may not reach the new picture. The viewer
        binding then rebuilds the stack, carrying the viewport across, and a
        new layer and binding start on the new source.
        """
        # The outgoing controller is about to be destroyed by `open()`;
        # it must not issue one last round on its way out.
        self._stop_gpu_backend(restore_controller=False)
        stack = self.viewer.open()
        if stack is None:
            self._backend = BACKEND_LEGACY
            return None
        if not self._start_gpu_backend(stack):
            self._load_overview_for_the_cpu_picture(stack)
            self._start_cpu_backend(stack)
            return stack
        self._connect_view_rect()
        self.publish_view_rect()
        if self._active:
            self._active = False
            self.activate()
        del reason
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
        refused = self._gpu_refusals()
        if refused:
            lines.append(refused)
        return "\n".join(lines)

    def _gpu_refusals(self):
        """What the GPU backend could NOT draw, and why. Honest or silent.

        A channel whose complete coarse does not fit the demo budget, and a
        submission whose active working set does not fit the texture cap,
        are both refusals: they are named here rather than papered over with
        partial data, coarser pixels or a quietly larger budget.
        """
        binding = self.gpu_binding
        if binding is None:
            return ""
        stats = binding.stats()
        lines = []
        unavailable = dict(stats.get("unavailable") or {})
        budget = sorted(channel for channel, why in unavailable.items()
                        if "budget" in str(why))
        if budget:
            lines.append(
                f"Not drawing {', '.join(budget)}: the display budget of "
                f"{DEMO_GPU_RAW_TEXTURE_BYTES // (1024 * 1024)} MiB has no "
                f"room for them. Nothing is shown from partial data.")
        error = str(stats.get("last_error") or "")
        if "budget" in error or "working set" in error:
            lines.append(f"The display could not be composed: {error}")
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
        # Nothing starts a read on the way out.
        self._stop_gpu_backend(restore_controller=False)
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
