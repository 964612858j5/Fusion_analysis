"""Step0 Explore tab — P0 skeleton mount of the v15 viewer stack.

See `docs/v15_step0_mount_plan.md`. This tab is a v15 INTEGRATION TRIAL
entry point, not the final UI: the final shape is Explore as a view mode
inside the Background Correction workspace (Explore / Compare / Pinned
patches). It exists first as a separate tab to prove lifecycle, dataset
switching and behaviour under real Step0 load at the lowest risk.

P0 scope, deliberately narrow:

* the viewer stack is built LAZILY, on the first activation of the tab
  with a dataset loaded, and never twice;
* mode is `Original` only -- no correction is selected, so the controller
  serves raw pixels (`ExploreController.method` stays None);
* the channel is a SNAPSHOT of the page's `current_channel` taken when the
  stack is built; two-way channel sync arrives in P1;
* a dataset switch tears the old stack down COMPLETELY before anything is
  bound to the new dataset, so no pixel and no source identity of the
  previous dataset can survive;
* teardown order is the pinned one -- `scheduler.shutdown()` (joins
  workers) then `provider.close()`, then the caches are dropped -- and is
  idempotent.

NOT in P0 (later phases): HOT/COVERAGE prefetch, the four preview modes,
two-way channel sync, and the BG-worker drain hand-off. Nothing here
writes to Save, to any config, or to the correction numerics, and nothing
here touches the existing patch preview.
"""

import time
import traceback

from PyQt5 import QtCore, QtWidgets

RAW_CACHE_BYTES = 512 * 1024 * 1024
CORRECTED_CACHE_BYTES = 2 * 1024 * 1024 * 1024
TILE_SIZE = 512

PLACEHOLDER_NO_DATASET = (
    "Explore (v15 trial)\n\n"
    "Load an OME-TIFF in the Background Correction tab first.\n"
    "This view then shows the whole slide at full resolution."
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

    def teardown(self):
        """Idempotent. `ExploreController.teardown` performs, in order,
        `scheduler.shutdown()` (which joins the worker threads) and then
        `provider.close()`. Only THEN are the caches emptied: nothing can
        still be writing into them, and dropping the tuple reference alone
        would free nothing, since the scheduler and the compute layer hold
        their own references to the same two cache objects."""
        if self.torn_down:
            return
        self.torn_down = True
        try:
            self.controller.teardown()
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


def build_default_stack(path, channel, parent_widget=None):
    """Construct the real viewer stack for `path`.

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
        # `method` is left None: P0 is Original-only, so nothing corrected is
        # requested and no correction parameter is read from the page.
        controller.load_overview(ensure_floor=False)
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
                 parent=None):
        super().__init__(parent)
        self._page = page
        self._stack_factory = stack_factory
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
        self._show_placeholder(PLACEHOLDER_NO_DATASET)

    def activate(self):
        """Called when this tab becomes the current one. Builds the stack
        on first activation with a dataset loaded, and never again for the
        same dataset."""
        if self._stack is not None or self._dataset_path is None:
            return
        if self._build_error is not None:
            # A failed build is not retried silently on every tab click;
            # the user gets the error until the dataset is reloaded.
            return
        self._build_attempts += 1
        channel = getattr(self._page, "current_channel", None)
        # Lifecycle logging, deliberately kept: building this stack blocks
        # the GUI thread (a synchronous overview read), so when a manual test
        # reports "nothing appeared" the log has to be able to say whether
        # the build ran at all, and how long it took.
        print(f"[explore] building stack: {self._dataset_path} "
              f"channel={channel!r}", flush=True)
        t0 = time.perf_counter()
        try:
            stack = self._stack_factory(self._dataset_path, channel, self)
        except Exception as exc:
            self._build_error = exc
            print(f"[explore] build FAILED after "
                  f"{(time.perf_counter() - t0) * 1000:.0f} ms: {exc}",
                  flush=True)
            self._show_placeholder(
                "Explore could not open this dataset:\n\n"
                f"{exc}\n\n"
                "The Background Correction tab is unaffected.")
            traceback.print_exc()
            return
        self._stack = stack
        self._show_widget(stack.view)
        print(f"[explore] stack ready in "
              f"{(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)

    def teardown(self):
        """Idempotent full teardown. Safe to call from `aboutToQuit`, from
        the page's destruction, from the page's own cleanup entry point, or
        twice."""
        self._discard_stack()

    def _on_page_destroyed(self, *_args):
        self._page = None
        self.teardown()

    # ── internals ─────────────────────────────────────────────────────
    def _discard_stack(self):
        stack, self._stack = self._stack, None
        if stack is None:
            return
        print("[explore] tearing stack down", flush=True)
        try:
            stack.teardown()
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
