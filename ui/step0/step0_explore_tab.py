"""The full-image view of the v15 viewer stack, inside Step0.

See `docs/v15_step0_mount_plan.md`. This is no longer a separate trial
tab: it is the Full Image page of the Background Correction workspace's
preview stack, reached by the ⤢ button on a compare panel and left with
"Back to compare". The class keeps its `Step0ExploreTab` name -- it is
still the owner of the viewer stack's lifecycle -- but nothing here is a
top-level tab any more, and the placeholder texts say so.

Scope, deliberately narrow:

* the viewer stack is built LAZILY, on the first opening of the view
  with a dataset loaded, and never twice;
* the SELECTION comes from the caller: each compare panel's ⤢ button asks
  for one result -- Original (`method=None`, so the controller serves raw
  pixels), TopHat or cuCIM -- and the stack is built with that selection
  rather than built as Original and switched afterwards. The parameters
  come from the page's preview provider, never from this file;
* the channel FOLLOWS the page: the stack is built with whatever
  `current_channel` the compare panels hold at build time, and every later
  opening of this view re-reads it and switches if it changed. There is no
  reverse direction -- this view has no channel control of its own to
  change it with;
* a dataset switch tears the old stack down COMPLETELY before anything is
  bound to the new dataset, so no pixel and no source identity of the
  previous dataset can survive;
* teardown order is the pinned one -- `scheduler.shutdown()` (joins
  workers) then `provider.close()`, then the caches are dropped -- and is
  idempotent.

The GPU hand-off IS here, in both directions: a production correction run
releases this view first (`release_for_production` -> teardown with
`wait_for_floor=True`, which blocks until a floor computation is off the
GPU), and a build is refused while such a run is going (`busy_probe`).

Still NOT here (later phases): HOT/COVERAGE prefetch, a multi-channel
overlay, a channel control inside this view and the reverse sync that
would need, and the viewport mapping that would open the full image on the
region the compare panels are showing. Nothing here writes to Save, to any
config, or to the correction numerics, and nothing here touches the
existing patch preview.
"""

import time
import traceback

from PyQt5 import QtCore, QtWidgets

RAW_CACHE_BYTES = 512 * 1024 * 1024
CORRECTED_CACHE_BYTES = 2 * 1024 * 1024 * 1024
TILE_SIZE = 512

PLACEHOLDER_RELEASED = (
    "Full image released\n\n"
    "Background correction is running ({reason}).\n"
    "Open this view again when it finishes -- it will be rebuilt with the "
    "parameters current at that time."
)

PLACEHOLDER_BUSY = (
    "Background correction is running ({reason}).\n\n"
    "The full image shares the GPU with it, so it cannot open until that "
    "finishes. Try again then."
)

# Three distinct reasons the full image is not on screen. They were one
# constant, which meant a dataset that WAS loaded still got told to load
# one -- the states are separated here so each says something true.
PLACEHOLDER_NO_DATASET = (
    "No image loaded\n\n"
    "Load an OME-TIFF in the Data & Paths box above.\n"
    "The full image then shows the whole slide at full resolution."
)

PLACEHOLDER_NOT_OPEN = (
    "Full image not open\n\n"
    "Use the ⤢ button on a compare panel to open that result -- Original, "
    "TopHat or cuCIM -- at full resolution."
)

PLACEHOLDER_BUILD_FAILED = (
    "Full image could not be opened\n\n"
    "{error}\n\n"
    "The compare panels are unaffected."
)


class ExploreStack:
    """The four objects the tab owns, plus the teardown order contract.

    Kept as a plain object (not a QWidget) so a test can substitute a fake
    without a real slide: everything the tab does to a stack goes through
    this interface.
    """

    def __init__(self, provider, scheduler, controller, view, caches):
        self.provider = provider
        self.scheduler = scheduler
        self.controller = controller
        self.view = view
        self.caches = caches
        self.torn_down = False

    def teardown(self, *, wait_for_floor: bool = False):
        """Idempotent. `ExploreController.teardown` performs, in order,
        `scheduler.shutdown()` (which joins the worker threads) and then
        `provider.close()`. Only THEN are the caches emptied: nothing can
        still be writing into them, and dropping the tuple reference alone
        would free nothing, since the scheduler and the compute layer hold
        their own references to the same two cache objects.

        `wait_for_floor` is passed straight through: True means a hand-off
        to something else that needs the GPU, so a running floor
        computation is waited out rather than joined with a timeout."""
        if self.torn_down:
            return
        self.torn_down = True
        try:
            self.controller.teardown(wait_for_floor=wait_for_floor)
        finally:
            for cache in (self.caches or ()):
                clear = getattr(cache, "clear", None)
                if clear is not None:
                    clear()
            self.caches = None


def _cleanup_partial_stack(controller, scheduler, provider, view):
    """Undo a half-built stack. Order matters and mirrors the normal path.

    If the controller exists it owns the shutdown sequence -- it also stops
    timers, disconnects signals, joins the floor threads and shuts the
    overview pool down, none of which a bare `scheduler.shutdown()` +
    `provider.close()` would do. Only when there is no controller yet do we
    close the two backend objects directly. The view is dropped either way:
    a widget left parented to the tab would show a dead stack's canvas.
    """
    try:
        if controller is not None:
            controller.teardown()
        else:
            if scheduler is not None:
                scheduler.shutdown()
            if provider is not None:
                provider.close()
    finally:
        if view is not None:
            try:
                view.setParent(None)
                view.deleteLater()
            except RuntimeError:
                pass


def build_default_stack(path, channel, parent_widget=None, *,
                        method=None, params=()):
    """Construct the real viewer stack for `path`.

    `method` / `params` are the selection the stack should come up WITH --
    `None` / `()` for Original, or `"tophat"` / `"cucim"` with that
    channel's effective parameter. When a method is given it is applied
    BEFORE the overview is installed: at that point
    `_overview_matches_selection()` is false and `_current_bbox` is still
    None, so the controller withholds the floor and issues nothing, and
    `load_overview(ensure_floor=True)` then starts the floor once, already
    against the right method.

    What this buys over building Original and switching afterwards, stated
    no wider than the evidence: the very first viewport request already
    carries the right method/params context, and one `set_selection` call
    with its provisional/precise-generation cancel cycle does not happen.
    NOT claimed: a saved floor job (Original has no method, so it computes
    no floor), fewer raw reads (both paths need raw for the provisional
    display), or a wrong frame avoided -- none of that has a request trace
    behind it.

    When `method is None` the params are normalised to `()`: nothing reads
    them, and letting a caller pass `(15,)` for Original would leave the
    controller's state disagreeing with the triple the caller asked for.

    Explore opens its OWN `RawTileProvider` rather than reusing the page's
    `OMETIFFLoader`: the loader is GUI-thread single-handle, and
    `set_corrected_zarr_store` changes which pixels it returns. The cost,
    stated plainly, is a second set of file handles, a second copy of the
    TIFF metadata and decoder state, and a second application-level tile
    cache. The OS page cache is shared, so that is not duplicated.

    On failure this closes whatever it already created before re-raising:
    a half-built stack must not leave worker threads or an open handle
    behind.
    """
    import pyqtgraph as pg

    from ...viewer.caches import LRUByteCache
    from ...viewer.correction_compute import CorrectionCompute
    from ...viewer.explore_view import ExploreController, ExploreView
    from ...viewer.raw_tile_provider import RawTileProvider
    from ...viewer.scheduler import TileScheduler
    from ...viewer.tile_types import TileGridSpec

    # Match main.py: standalone construction must not render transposed.
    pg.setConfigOptions(imageAxisOrder="row-major")

    provider = None
    scheduler = None
    controller = None
    view = None
    try:
        provider = RawTileProvider(path)
        if not channel or channel not in provider.channel_names:
            channel = provider.channel_names[0]
        raw_cache = LRUByteCache(RAW_CACHE_BYTES)
        corrected_cache = LRUByteCache(CORRECTED_CACHE_BYTES)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache, corrected_cache)
        grid = TileGridSpec(tile_size=TILE_SIZE, source_chunk_shape=(),
                            grid_version="v1")
        view = ExploreView(parent_widget)
        controller = ExploreController(provider, scheduler, compute, grid,
                                       view, channel)
        if method is None:
            params = ()
        else:
            # Before the overview: withheld, so this issues nothing (see
            # this function's docstring).
            controller.set_selection(method=method, params=tuple(params))
        # `ensure_floor` only where there is a method to compute a floor
        # for; Original needs none.
        controller.load_overview(ensure_floor=method is not None)
        # Open on the whole slide. Without an explicit range the ViewBox
        # keeps its default one, no tile is visible, and the tab would come
        # up empty even though the stack is healthy.
        h0, w0 = provider.level_shape(0)
        view.view_box.setRange(xRange=(0, w0), yRange=(0, h0), padding=0)
        return ExploreStack(provider, scheduler, controller, view,
                            (raw_cache, corrected_cache))
    except Exception:
        # `load_overview` and `setRange` run AFTER the controller exists, so
        # the failure path must be able to unwind a controller, not just the
        # two backend objects.
        try:
            _cleanup_partial_stack(controller, scheduler, provider, view)
        except Exception:
            pass
        raise


class Step0ExploreTab(QtWidgets.QWidget):
    """Hosts the Explore stack for the currently loaded dataset."""

    def __init__(self, page=None, stack_factory=build_default_stack,
                 parent=None, busy_probe=None):
        """`busy_probe` is an optional read-only callable returning the name
        of a production correction run in progress, or None when the GPU is
        free. The host owns that judgement -- this tab asks, and never
        inspects the host's worker handles itself. Default None means
        "never busy", which is what every existing caller gets.
        """
        super().__init__(parent)
        self._page = page
        self._stack_factory = stack_factory
        self._busy_probe = busy_probe
        self._stack = None
        self._dataset_path = None
        self._build_attempts = 0
        self._build_error = None

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QtWidgets.QLabel(PLACEHOLDER_NO_DATASET)
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color:#bbb; background:#1c1c1c;")
        self._layout.addWidget(self._placeholder)

        # An application quitting is a teardown point like any other: the
        # worker threads must be joined and the handles closed while the
        # objects are still alive.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.teardown)
        # Destroying or replacing the Step0 page is a teardown point too --
        # relying on Python GC to collect a stack that owns 12 worker
        # threads and two open file handles is not a lifecycle contract.
        # `QObject.destroyed` is emitted at the start of the page's
        # destructor, BEFORE its children are deleted, so the stack is still
        # intact here; `_discard_stack` is defensive about the widget side
        # regardless.
        if isinstance(page, QtCore.QObject):
            page.destroyed.connect(self._on_page_destroyed)

    # ── state, readable by tests and by the page ──────────────────────
    @property
    def stack(self):
        return self._stack

    @property
    def dataset_path(self):
        return self._dataset_path

    @property
    def build_attempts(self):
        return self._build_attempts

    # ── lifecycle ─────────────────────────────────────────────────────
    def set_dataset(self, path):
        """Bind a dataset (or None). Tears the previous stack down FIRST.

        Ordering is the whole point: the old provider is closed and the old
        caches dropped before the new path is recorded, so no pixel and no
        source identity of the previous dataset can survive into the next
        one -- not even transiently on screen.
        """
        self._discard_stack()
        self._dataset_path = path or None
        self._build_error = None
        # A bound dataset is NOT the "load something" state: the view simply
        # has not been opened yet, and it is opened from a compare panel.
        self._show_placeholder(PLACEHOLDER_NO_DATASET
                               if self._dataset_path is None
                               else PLACEHOLDER_NOT_OPEN)

    def activate(self):
        """Open-the-view lifecycle entry: build if needed, else re-sync.

        No production caller since the full image became a page of the
        preview stack -- `show_source` is the live entry point, because
        every real way in names a result. This is kept as the
        selection-free form of the same lifecycle: build on first open with
        whatever channel the compare panels hold, re-read that channel on
        every later open. It stays because a host that reopens the view
        without naming a method needs exactly this, and because the pull
        semantics below are the contract `show_source` builds on.

        Two jobs. The first call with a dataset loaded BUILDS the stack,
        using whatever channel the compare panels have selected right then,
        and never builds again for the same dataset. Every later call PULLS
        that selection again: the user picks a channel over there, comes
        back here, and sees it.

        Pull rather than subscribe, deliberately. A signal connection would
        have to decide what to do while this view is hidden -- switching an
        invisible view costs a synchronous overview read for nothing -- and
        would fire once per intermediate selection on the way to the one the
        user actually wants. Reading on entry answers both by construction:
        it happens only when the view is about to be seen, and it sees only
        the final choice.

        The channel is compared before switching. `set_selection` is not
        free even for the channel already displayed: it cancels the
        directional prefetch and re-enters the provisional state.
        """
        if self._dataset_path is None:
            return
        if self._stack is not None:
            self._pull_channel_from_page()
            return
        if self._build_error is not None:
            # A failed build is not retried silently on every tab click;
            # the user gets the error until the dataset is reloaded.
            return
        self._build(getattr(self._page, "current_channel", None))

    def _build(self, channel, *, method=None, params=()):
        """The one build path, used by `activate` and by `show_source`.

        Kept single deliberately: a second construction site is how the
        failure handling, the logging and the placeholder state drift apart.
        It is also the single place the production-run gate is checked: a
        stack built while a correction run is going would put a second user
        on the GPU, which is exactly what the release on the other side
        exists to prevent.
        """
        busy = self._busy_probe() if self._busy_probe is not None else None
        if busy:
            print(f"[explore] refusing to build: {busy} is running",
                  flush=True)
            self._show_placeholder(PLACEHOLDER_BUSY.format(reason=busy))
            return
        self._build_attempts += 1
        # Lifecycle logging, deliberately kept: building this stack blocks
        # the GUI thread (a synchronous overview read), so when a manual test
        # reports "nothing appeared" the log has to be able to say whether
        # the build ran at all, and how long it took.
        print(f"[explore] building stack: {self._dataset_path} "
              f"channel={channel!r} method={method!r} params={tuple(params)!r}",
              flush=True)
        t0 = time.perf_counter()
        try:
            stack = self._stack_factory(self._dataset_path, channel, self,
                                        method=method, params=tuple(params))
        except Exception as exc:
            self._build_error = exc
            print(f"[explore] build FAILED after "
                  f"{(time.perf_counter() - t0) * 1000:.0f} ms: {exc}",
                  flush=True)
            self._show_placeholder(
                PLACEHOLDER_BUILD_FAILED.format(error=exc))
            traceback.print_exc()
            return
        self._stack = stack
        self._show_widget(stack.view)
        print(f"[explore] stack ready in "
              f"{(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)

    def show_source(self, channel, method, params=()):
        """Show `(channel, method, params)`, building the stack if needed.

        The drill-down entry point: the compare panels' "full image" buttons
        each name one result, and this is how they ask for it. Original is
        `method=None, params=()`.

        Two rules, both about not doing work twice:

        * with no stack yet, the stack is BUILT with this selection, so the
          first viewport request already carries the right method/params
          and no extra `set_selection` (with its provisional/generation
          cancel cycle) is needed;
        * with a stack already up, the WHOLE triple is compared first and
          `set_selection` is called only if something differs.
          `set_selection` is not free even for an identical selection: it
          cancels the directional prefetch and re-enters the provisional
          state.

        `method=None` (Original) always means `params=()`: the parameters
        are not read in that case, and accepting them would let this report
        success for a triple the controller does not actually hold.

        Returns True when the selection has been ACCEPTED and a stack is
        available to serve it -- not that its pixels or its floor are on
        screen yet; those arrive asynchronously. False when there is
        nothing to serve it with: no dataset bound, or a build that failed.
        """
        if self._dataset_path is None:
            return False

        params = () if method is None else tuple(params)
        wanted = (channel, method, params)
        if self._stack is None:
            if self._build_error is not None:
                return False
            self._build(channel, method=method, params=params)
            return self._stack is not None

        controller = self._stack.controller
        current = (controller.channel, controller.method,
                   tuple(controller.params))
        if current == wanted:
            return True
        print(f"[explore] switching source: {current} -> {wanted}", flush=True)
        controller.set_selection(channel=channel, method=method,
                                 params=params)
        return True

    def _pull_channel_from_page(self):
        """Re-read the page's channel and switch to it if it changed.

        Only on a real change, and only for a real channel: None (no
        selection, or the page between datasets) and the empty string leave
        the view alone rather than switching it to nothing.
        """
        channel = getattr(self._page, "current_channel", None)
        if not channel:
            return
        controller = self._stack.controller
        if channel == controller.channel:
            return
        print(f"[explore] following the page's channel: "
              f"{controller.channel!r} -> {channel!r}", flush=True)
        controller.set_selection(channel=channel)

    def release_for_production(self, reason):
        """Give the GPU up to a production correction run.

        The same physical teardown as `teardown(wait_for_floor=True)` -- it
        blocks until a running floor computation has actually finished --
        but a DIFFERENT user-facing state: this is recoverable. The view
        says why it went away and that reopening will rebuild it, where a
        real teardown either says nothing (destruction) or asks for a
        dataset. Without a placeholder here the tab would simply go blank:
        `_discard_stack` removes the view and puts nothing in its place.

        Idempotent; a no-op when no stack exists (the placeholder is left
        alone in that case, so a "no dataset" message is not overwritten).
        """
        if self._stack is None:
            return
        self._discard_stack(wait_for_floor=True)
        self._show_placeholder(PLACEHOLDER_RELEASED.format(reason=reason))

    def teardown(self, *, wait_for_floor: bool = False):
        """Idempotent full teardown. Safe to call from `aboutToQuit`, from
        the page's destruction, from the page's own cleanup entry point, or
        twice.

        `wait_for_floor=True` is the HAND-OFF form: the caller is about to
        run something else on the GPU, so this must not return while a
        floor computation is still using it. It blocks for as long as that
        job needs. The host asks for it through this argument rather than
        by reaching into the controller's thread list.
        """
        self._discard_stack(wait_for_floor=wait_for_floor)

    def _on_page_destroyed(self, *_args):
        self._page = None
        self.teardown()

    # ── internals ─────────────────────────────────────────────────────
    def _discard_stack(self, *, wait_for_floor: bool = False):
        stack, self._stack = self._stack, None
        if stack is None:
            return
        print(f"[explore] tearing stack down "
              f"(wait_for_floor={wait_for_floor})", flush=True)
        try:
            stack.teardown(wait_for_floor=wait_for_floor)
        finally:
            view = getattr(stack, "view", None)
            if view is not None:
                # The C++ side may already be going away when this runs from
                # the page's destruction; the backend shutdown above is what
                # matters and has already happened.
                try:
                    self._layout.removeWidget(view)
                    view.setParent(None)
                    view.deleteLater()
                except RuntimeError:
                    pass

    def _show_placeholder(self, text):
        self._placeholder.setText(text)
        self._show_widget(self._placeholder)

    def _show_widget(self, widget):
        # Membership in the LAYOUT, not parenthood. `ExploreView` is built
        # with this tab as its Qt parent, so a parenthood test skipped
        # `addWidget` entirely: the view was a child of the tab but under no
        # layout, so it got no geometry and the tab came up blank while the
        # stack behind it was perfectly healthy.
        if self._layout.indexOf(widget) < 0:
            self._layout.addWidget(widget)
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i).widget()
            if item is not None:
                item.setVisible(item is widget)
