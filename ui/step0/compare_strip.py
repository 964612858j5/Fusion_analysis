"""Compare mode: three tile viewers of one slide, side by side.

The compare panels used to be a SNAPSHOT -- one crop, cut for the panels'
size, replaced wholesale when the camera settled somewhere else, with a
whole-slide low-res picture underneath so a zoom-out uncovered something
rather than nothing. Every judgement in that design was defensible on its
own and the result was still rejected, in the user's words, as "极不流畅,
不如不改": zoom out and you watched a blurry floor for a beat and then the
picture POPPED to sharp when the refill landed. The full image, on the same
slide and the same GPU, has never done that -- it refines tile by tile, so
there is no moment where the whole frame is wrong and no moment where the
whole frame changes at once.

So compare mode is now the full image, three times. Same `ExploreView`,
same `ExploreController`, same pyramid levels, tile pools, raw level+1
underlay, corrected floor, occlusion culling and smooth transform. The only
things this module adds are the ones that make three of them behave as one
instrument:

* ONE BACKEND. Three controllers, but a single `RawTileProvider`, a single
  raw tile cache, a single corrected tile cache, one `CorrectionCompute`
  and one `TileScheduler`. The provider is thread-safe and holds no
  per-consumer state, the caches are keyed by a tuple that already includes
  channel and method, and the scheduler was built for several consumers
  (the DAPI overlay has always been a second one). Concretely this is what
  stops the raw pixels under the Original panel being read again for the
  TopHat panel and a third time for the cuCIM one: TopHat and cuCIM stage
  their halo from the SAME raw cache Original is filling.

  Two hazards come with sharing and both are handled at the source rather
  than here: generation tokens are namespaced per controller (`gen_ns`), so
  one panel's cancel cannot drop another's queued tiles, and the whole-slide
  overview lives in a `SharedOverviewStore`, so it is read once and
  installed three times.

* ONE CAMERA. A range change on any of the three is copied to the other two
  as a centre and a scale, never as a rectangle -- the three panels are
  aspect-locked columns whose widths differ by a pixel or two, and a shared
  rectangle would be reshaped differently in each of them.

* ONE SELECTION. The channel and the display mapping are the page's, so all
  three follow them together; the METHOD is what makes a panel itself and is
  the one thing that differs between them.
"""

import math
import weakref

from PyQt5 import QtCore, QtWidgets

from ...viewer.tile_types import (CorrectionKey, RawKey, TileAddress,
                                  TileRequest, effective_param)

# Left to right, and the same order (and the same names) as the full
# image's own method switch, so "the middle panel" and "the TopHat button"
# cannot come to mean different things.
COMPARE_SOURCES = ("original", "tophat", "cucim")
COMPARE_METHODS = {"original": None, "tophat": "tophat", "cucim": "cucim"}
COMPARE_TITLES = {"original": "Original", "tophat": "TopHat",
                  "cucim": "cucim"}

# The priority a PENDING channel switch's own tiles are asked for at.
#
# Zero is the top tier of the strip's shared scheduler -- ahead of the
# visible corrected batch (`PRECISE_CURRENT_BASE_PRIORITY` 100), the
# current level's raw batch (`RAW_CURRENT_BASE_PRIORITY` 200) and, by three
# orders of magnitude, of neighbour preparation (`HOT_PRIORITY_BASE`
# 5000). It is the foreground: the user is looking at a strip that says it
# is preparing this channel, and nothing else queued on this scheduler is
# more urgent than the thing they are waiting for.
COMPARE_PENDING_PRIORITY = 0


class CompareStacks:
    """The shared backend plus the three per-panel triples.

    Not a QWidget: a test can substitute a fake, and everything the strip
    does to the stacks goes through this interface.
    """

    def __init__(self, provider, scheduler, compute, grid, caches,
                 overview_store, controllers, views, overlays,
                 owns_overview_store=True):
        self.provider = provider
        self.scheduler = scheduler
        self.compute = compute
        self.grid = grid
        self.caches = caches
        self.overview_store = overview_store
        # False when the store was BORROWED from the full image's stack.
        # Shutting a borrowed pool down would leave the full image unable
        # to switch channels for the rest of the session.
        self.owns_overview_store = bool(owns_overview_store)
        self.controllers = list(controllers)
        self.views = list(views)
        self.overlays = list(overlays)
        self.torn_down = False

    def teardown(self, *, wait_for_floor: bool = False):
        """Idempotent, and ordered so the shared backend outlives every one
        of its users.

        The two follower controllers are torn down with
        `shutdown_backend=False` -- they do not own the scheduler or the
        provider and must not close them out from under the third. The
        overview pool is shut down here, between the last controller's
        overview traffic and `provider.close()`, because no controller owns
        the shared store and so none of them will do it. Only then does the
        first controller run the full teardown, which performs
        `scheduler.shutdown()` (joining the worker threads) and
        `provider.close()` in that pinned order.
        """
        if self.torn_down:
            return
        self.torn_down = True
        controllers = list(self.controllers)
        try:
            for controller in controllers[1:]:
                try:
                    controller.teardown(shutdown_backend=False,
                                        wait_for_floor=wait_for_floor)
                except Exception:                           # noqa: BLE001
                    pass
            store = self.overview_store
            if store is not None and self.owns_overview_store:
                try:
                    store.shutdown()
                except Exception:                           # noqa: BLE001
                    pass
            if controllers:
                controllers[0].teardown(shutdown_backend=True,
                                        wait_for_floor=wait_for_floor)
        finally:
            for cache in (self.caches or ()):
                clear = getattr(cache, "clear", None)
                if clear is not None:
                    clear()
            self.caches = None


def build_compare_stacks(path, channel, parent_widget=None, *,
                         sources=COMPARE_SOURCES, params_for=None,
                         tint=None, nucleus_channel=None, nucleus_tint=None,
                         nucleus_enabled=False, viewport_l0=None,
                         overview_store=None):
    """Build the three tile stacks for `path` over ONE backend.

    `overview_store` is the FULL IMAGE's whole-slide overview store, lent
    for the duration. It is the one piece of the full image's backend the
    strip borrows, and the reason is measured: building the strip cost
    1622 ms on the real 59040x35520 slide, of which the provider, the
    scheduler, three views and three controllers were 163 ms and the first
    `load_overview` was 1110 ms -- a synchronous, GUI-thread re-read of a
    whole pyramid level the full image had already read and still held.
    Sharing the store turns that read into a memcpy.

    Only the store. The provider, the scheduler and the two tile caches
    stay the strip's own: sharing the scheduler would make
    `suspend_for_production`'s "wait until idle" mean "wait for the other
    mode too", which is a GUI-thread wait on somebody else's work, and
    sharing the 2 GB corrected cache would make the two modes evict each
    other's tiles. Neither is worth 163 ms.

    None means the strip makes and owns a private store, which is what a
    strip built with no full image behind it must do.

    `params_for(source)` returns the parameter tuple that source should come
    up with -- `()` for Original, `(radius,)` for TopHat, `(sigma,)` for
    cuCIM. It is a callback rather than a dict because the numbers are the
    channel row's CURRENT ones and the page owns that judgement.

    `viewport_l0` is `(y0, x0, w, h)`: where all three should OPEN. Applied
    after the overviews are installed, for the same reason
    `build_default_stack` does it last -- before that the controllers are
    blocked on the overview and a camera move would issue nothing.

    The failure path closes whatever was already built. A half-built strip
    must not leave twelve worker threads and an open TIFF handle behind.
    """
    import pyqtgraph as pg

    from ...viewer.caches import LRUByteCache
    from ...viewer.correction_compute import CorrectionCompute
    from ...viewer.explore_view import (ExploreController, ExploreView,
                                        RawOverlayLayer, SharedOverviewStore)
    from ...viewer.raw_tile_provider import RawTileProvider
    from ...viewer.scheduler import TileScheduler
    from ...viewer.tile_types import TileGridSpec
    from .step0_explore_tab import (CORRECTED_CACHE_BYTES, RAW_CACHE_BYTES,
                                    TILE_SIZE)

    # Match main.py: standalone construction must not render transposed.
    pg.setConfigOptions(imageAxisOrder="row-major")

    provider = None
    scheduler = None
    store = None
    controllers, views, overlays = [], [], []
    try:
        provider = RawTileProvider(path)
        if not channel or channel not in provider.channel_names:
            channel = provider.channel_names[0]
        # ONE of each. Sized as the full image's are: the three panels look
        # at the same place at the same moment, so they want one cache the
        # size of one view's, not three.
        raw_cache = LRUByteCache(RAW_CACHE_BYTES)
        corrected_cache = LRUByteCache(CORRECTED_CACHE_BYTES)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache,
                                  corrected_cache)
        grid = TileGridSpec(tile_size=TILE_SIZE, source_chunk_shape=(),
                            grid_version="v1")
        owns_store = overview_store is None
        store = SharedOverviewStore() if owns_store else overview_store

        for source in sources:
            method = COMPARE_METHODS.get(source)
            params = () if method is None else tuple(
                params_for(source) if params_for is not None else ())
            view = ExploreView(parent_widget)
            controller = ExploreController(
                provider, scheduler, compute, grid, view, channel,
                gen_ns=source, overview_store=store)
            if method is not None:
                # Before the overview, exactly as `build_default_stack` does
                # it: the controller withholds the floor and issues nothing
                # until `load_overview(ensure_floor=True)` starts it once,
                # already against the right method.
                controller.set_selection(method=method, params=params)
            if tint is not None:
                controller.set_tint(tint)
            overlay = None
            if nucleus_channel and nucleus_channel in provider.channel_names:
                overlay = RawOverlayLayer(provider, scheduler, grid, view,
                                          nucleus_channel, gen_ns=source)
                controller.attach_overlay(overlay)
                if nucleus_tint is not None:
                    overlay.set_tint(nucleus_tint)
                overlay.set_suppressed(nucleus_channel == channel,
                                       host=controller)
                overlay.set_enabled(bool(nucleus_enabled), host=controller)
            controllers.append(controller)
            views.append(view)
            overlays.append(overlay)

        # The FIRST call reads the overview off the disk; the other two find
        # the record resident in the shared store and install it with a
        # memcpy. This is the whole reason the store is shared.
        for controller in controllers:
            controller.load_overview(
                ensure_floor=controller.method is not None)

        if viewport_l0 is not None:
            for controller in controllers:
                controller.jump_to(*(int(v) for v in viewport_l0))
        else:
            h0, w0 = provider.level_shape(0)
            for view in views:
                view.view_box.setRange(xRange=(0, w0), yRange=(0, h0),
                                       padding=0)
        return CompareStacks(provider, scheduler, compute, grid,
                             (raw_cache, corrected_cache), store,
                             controllers, views, overlays,
                             owns_overview_store=owns_store)
    except Exception:
        _cleanup_partial(controllers, store if owns_store else None,
                         scheduler, provider, views)
        raise


def _cleanup_partial(controllers, store, scheduler, provider, views):
    """Undo a half-built strip, in the same order a whole one unwinds."""
    try:
        for controller in list(controllers)[1:]:
            try:
                controller.teardown(shutdown_backend=False)
            except Exception:                               # noqa: BLE001
                pass
        if store is not None:
            try:
                store.shutdown()
            except Exception:                               # noqa: BLE001
                pass
        if controllers:
            try:
                controllers[0].teardown(shutdown_backend=True)
            except Exception:                               # noqa: BLE001
                pass
        else:
            if scheduler is not None:
                scheduler.shutdown()
            if provider is not None:
                provider.close()
    finally:
        for view in list(views):
            try:
                view.setParent(None)
                view.deleteLater()
            except RuntimeError:
                pass


class CompareStrip(QtWidgets.QWidget):
    """Three tile viewers under one camera, one channel and one mapping.

    Owns the stacks' lifecycle the way `Step0ExploreTab` owns the full
    image's: built LAZILY on the first entry into compare mode with a
    dataset loaded, never twice, and torn down COMPLETELY before anything is
    bound to a new dataset.
    """

    # ONE notification per camera change, whichever of the three moved and
    # however many of them the mirroring then moves. The page draws the
    # panels' viewport on the Tissue Preview from it; before this existed
    # nothing was connected to `_on_compare_range_changed` at all, so the
    # dashed rectangle was set once on entry and never again -- it did not
    # follow a pan and it did not change size under a zoom.
    #
    # Emitted AFTER the mirroring has finished and the re-entrancy guard is
    # down, so a listener that reads the camera reads the settled one, and
    # the two followers' own range signals (which the guard swallows) do not
    # each produce a page-level redraw.
    camera_changed = QtCore.pyqtSignal()

    # ONE notification per COMMITTED channel switch, emitted after all three
    # panels have been moved and before the event loop gets a chance to
    # paint. It is what the page hangs "measure the panels" off: while a
    # switch is pending the three panels are showing the PREVIOUS channel,
    # and a measurement taken then would be the previous channel's numbers
    # under the new channel's labels.
    publication_changed = QtCore.pyqtSignal()

    # The pending state was entered, re-planned or left. Carries nothing:
    # `pending_text()` is the question a listener actually has.
    pending_changed = QtCore.pyqtSignal()

    # A pending tile came back. Scheduler callbacks fire on a COMPUTE WORKER
    # thread; every other path in this viewer marshals such a callback to
    # the GUI thread through a queued signal before touching state, and this
    # one must too -- `_on_pending_tile` re-runs the readiness probe and can
    # publish, which touches three views.
    _pending_tile = QtCore.pyqtSignal(int, object)

    def __init__(self, page=None, stack_factory=build_compare_stacks,
                 parent=None):
        super().__init__(parent)
        self._page = page
        self._stack_factory = stack_factory
        self._stacks = None
        self._dataset_path = None
        self._build_error = None
        # Re-entrancy guard for the camera link. A `setRange` on a follower
        # emits its own `sigRangeChanged`, so without this the three would
        # chase each other for as long as floating point kept changing the
        # numbers.
        self._linking = False
        # Set while the strip is off screen: the pools and the camera are
        # kept, the GPU is not.
        self._suspended = False
        # The strip's ONE HOT coordinator and the callback that tells it
        # what the neighbourhood is. See the "neighbour preparation"
        # section below.
        self._hot = None
        self._hot_specs_provider = None

        # ── the staged publication (see the "one selection" section) ─────
        # What the three panels ARE showing, as opposed to what the page
        # has asked for. They differ only while a switch is pending.
        self._displayed_channel = None
        self._displayed_params_for = None
        self._displayed_tint = None
        # The pending switch, or None. `_pending_serial` is the latest-wins
        # token: every plan carries the serial it was made under and a
        # delivery whose serial has moved on is dropped, so a run of clicks
        # can only ever publish the last one.
        self._pending = None
        self._pending_serial = 0
        self._pending_gen_n = 0
        self._pending_error = None
        # The controller whose `overview_prepared` a pending switch is
        # listening to, or None. Held so the connection is made at most
        # once and dropped before the controllers are.
        self._overview_watch = None
        self._pending_tile.connect(self._on_pending_tile,
                                   QtCore.Qt.QueuedConnection)

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._placeholder = QtWidgets.QLabel(
            "No image loaded\n\n"
            "Load an OME-TIFF in the Data & Paths box above, then "
            "right-click the full image to compare that spot.")
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color:#bbb; background:#1c1c1c;")
        self._layout.addWidget(self._placeholder)

        # The one place the strip says, in words, that what is on screen is
        # NOT what the page's channel list says. Hidden unless a switch is
        # pending: three panels showing CD22 under a page that has moved to
        # TIM3 is only honest if it is labelled, and a title that says TIM3
        # over CD22 pixels with no hint is the state this exists to
        # prevent.
        self._pending_lbl = QtWidgets.QLabel("")
        self._pending_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._pending_lbl.setWordWrap(True)
        self._pending_lbl.setStyleSheet(
            "color:#ffd479;font-size:11px;font-weight:bold;background:#2a2418;"
            "padding:2px;")
        self._pending_lbl.setVisible(False)
        self._layout.addWidget(self._pending_lbl)

        self._panes = QtWidgets.QWidget(self)
        panes_lay = QtWidgets.QHBoxLayout(self._panes)
        panes_lay.setContentsMargins(0, 0, 0, 0)
        panes_lay.setSpacing(3)
        self._columns = []
        self._titles = []
        for source in COMPARE_SOURCES:
            column = QtWidgets.QWidget(self._panes)
            col_lay = QtWidgets.QVBoxLayout(column)
            col_lay.setContentsMargins(0, 0, 0, 0)
            col_lay.setSpacing(1)
            title = QtWidgets.QLabel(COMPARE_TITLES[source], column)
            title.setAlignment(QtCore.Qt.AlignCenter)
            title.setStyleSheet(
                "color:#ddd;font-size:11px;font-weight:bold;background:#111;")
            col_lay.addWidget(title)
            self._titles.append(title)
            self._columns.append((column, col_lay))
            panes_lay.addWidget(column, stretch=1)
        self._panes.setVisible(False)
        self._layout.addWidget(self._panes, stretch=1)

        # An application quitting, or the page being destroyed, is a
        # teardown point: twelve worker threads and an open TIFF handle are
        # not something to leave to the garbage collector. Same contract,
        # and same reasons, as `Step0ExploreTab`.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.teardown)
        if isinstance(page, QtCore.QObject):
            page.destroyed.connect(lambda *_a: self.teardown())

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def stacks(self):
        return self._stacks

    @property
    def controllers(self):
        return list(getattr(self._stacks, "controllers", ()) or ())

    @property
    def views(self):
        return list(getattr(self._stacks, "views", ()) or ())

    @property
    def overlays(self):
        return list(getattr(self._stacks, "overlays", ()) or ())

    @property
    def view_boxes(self):
        return [getattr(v, "view_box", None) for v in self.views]

    @property
    def built(self):
        return self._stacks is not None

    def set_dataset(self, path):
        """Bind to `path`. A CHANGE tears the old strip down completely
        before anything is bound to the new one: no pixel and no source
        identity of the previous dataset may survive."""
        path = path or None
        if path == self._dataset_path:
            return
        self.teardown()
        self._dataset_path = path
        self._build_error = None

    def ensure_built(self, channel, *, params_for=None, tint=None,
                     nucleus=None, viewport_l0=None, overview_store=None):
        """Build on first use; afterwards return what is already there.

        Returns the `CompareStacks`, or None when there is no dataset or the
        build failed (in which case the placeholder says so).
        """
        if self._stacks is not None:
            return self._stacks
        if not self._dataset_path:
            return None
        try:
            stacks = self._stack_factory(
                self._dataset_path, channel, self,
                params_for=params_for, tint=tint,
                viewport_l0=viewport_l0, overview_store=overview_store,
                **dict(nucleus or {}))
        except Exception as exc:                            # noqa: BLE001
            self._build_error = str(exc)
            self._placeholder.setText(
                "Compare could not be opened\n\n"
                f"{self._build_error}\n\n"
                "The full image is unaffected.")
            return None
        self._stacks = stacks
        # The build IS a publication: the three panels come up on `channel`
        # already, so that is what they are displaying and what a later
        # switch is a switch FROM.
        self._displayed_channel = channel
        self._displayed_params_for = params_for
        self._displayed_tint = tint
        for (column, col_lay), view in zip(self._columns, stacks.views):
            view.setParent(column)
            col_lay.addWidget(view, stretch=1)
        self._connect_camera_link()
        self._placeholder.setVisible(False)
        self._panes.setVisible(True)
        return stacks

    def teardown(self, *, wait_for_floor: bool = False):
        # BEFORE the controllers: HOT holds a host controller and four of
        # its signals, and its queue is spent through the scheduler the
        # teardown below shuts down. Stopping it first means no request can
        # be issued into a scheduler that is joining its workers, and no
        # slot fires on a controller that is being destroyed.
        self.stop_hot()
        # Before the scheduler is asked to join its workers: a pending
        # switch owns a generation on it and a queued `_pending_tile`
        # delivery that must not reach a strip whose stacks are gone.
        self._cancel_pending()
        stacks, self._stacks = self._stacks, None
        self._displayed_channel = None
        self._displayed_params_for = None
        self._displayed_tint = None
        self._suspended = False
        if stacks is None:
            return
        for view in stacks.views:
            try:
                view.setParent(None)
                view.deleteLater()
            except RuntimeError:
                pass
        try:
            stacks.teardown(wait_for_floor=wait_for_floor)
        finally:
            # The page connects to the controllers this teardown destroys
            # and guards those connections with a flag on THIS object. A
            # rebuild makes new controllers, so the flags have to go with
            # the old ones or the new strip is never connected at all.
            self._exit_connected = False
            self._seed_connected = False
            self._panes.setVisible(False)
            self._placeholder.setVisible(True)

    # ── the shared camera ────────────────────────────────────────────────

    def _connect_camera_link(self):
        """Mirror any panel's range onto the other two.

        The connection captures a WEAK reference to the strip and the index
        by value. A bound method (or a closure over `self`) on a signal
        owned by a pyqtgraph item closes a reference cycle through sip, and
        that is the exact shape of the offscreen segfaults this codebase has
        measured twice; the value-capturing closure has no such cycle.
        """
        ref = weakref.ref(self)
        for i, vb in enumerate(self.view_boxes):
            if vb is None:
                continue

            def _on_range(*_a, _i=i, _ref=ref):
                strip = _ref()
                if strip is not None:
                    strip._mirror_from(_i)

            vb.sigRangeChanged.connect(_on_range)

    def _mirror_from(self, idx):
        if self._linking:
            return
        camera = self.camera(idx)
        if camera is None:
            return
        self._linking = True
        try:
            self._apply_camera_to(camera, skip=idx)
        finally:
            self._linking = False
        # A pending switch was planned for where the panels WERE. This is
        # the single place every pan, zoom, navigator jump and patch
        # navigation of the compare panels comes through, so it is the
        # single place a stale plan is caught -- and it is a no-op when the
        # token has not actually moved.
        self._invalidate_pending_on_move()
        self.camera_changed.emit()

    def camera(self, idx=0):
        """`(cx, cy, scale)` of panel `idx`, or None.

        `scale` is SCREEN PIXELS PER LEVEL-0 PIXEL, from `viewPixelSize()` --
        the same measurement the full image's `_full_image_scale` makes, and
        deliberately not a width divided by a range. `viewPixelSize` is the
        number pyqtgraph itself uses and it carries whatever device
        transform the graphics view has, which is what makes "the same
        magnification" mean the same thing in two differently-shaped
        widgets. A ratio of logical numbers would agree with it only while
        that transform is the identity, and the claim being made here --
        that a structure is the same size on screen in both modes -- is
        exactly the claim that would then be false.

        A centre and a scale rather than a rectangle, because the panels are
        a third as wide as the full image: a rectangle handed between the
        two would be reshaped by one of the aspect locks and, cycle after
        cycle, walk the view away from where it started.
        """
        boxes = self.view_boxes
        if idx >= len(boxes) or boxes[idx] is None:
            return None
        try:
            (x0, x1), (y0, y1) = boxes[idx].viewRange()
            per_pixel = float(boxes[idx].viewPixelSize()[0])
        except Exception:                                   # noqa: BLE001
            return None
        if not (per_pixel > 0 and math.isfinite(per_pixel)):
            return None
        scale = 1.0 / per_pixel
        return ((float(x0) + float(x1)) / 2.0,
                (float(y0) + float(y1)) / 2.0, scale)

    def panel_px(self, idx=0):
        """One panel's size in screen pixels, or None before layout."""
        boxes = self.view_boxes
        if idx < len(boxes) and boxes[idx] is not None:
            try:
                w, h = float(boxes[idx].width()), float(boxes[idx].height())
                if w > 1.0 and h > 1.0:
                    return (w, h)
            except Exception:                               # noqa: BLE001
                pass
        return None

    def set_camera(self, cx, cy, scale):
        """Put centre `(cx, cy)` at `scale` on all three panels.

        Each panel is given the rectangle that fits ITS OWN widget at that
        scale rather than one rectangle for all three: they are aspect-locked
        columns whose widths differ by a pixel or two, and a shared rectangle
        would be reshaped differently in each. Solved per panel, all three
        end up at the same centre and the same magnification, which is what
        "one camera" is supposed to mean.
        """
        if not (scale > 0 and math.isfinite(scale)):
            return False
        self._linking = True
        try:
            applied = self._apply_camera_to((cx, cy, scale), skip=None)
        finally:
            self._linking = False
        if applied:
            self.camera_changed.emit()
        return applied

    def _apply_camera_to(self, camera, skip=None):
        cx, cy, scale = camera
        applied = False
        for i, controller in enumerate(self.controllers):
            if i == skip or controller is None:
                continue
            box = self.view_boxes[i] if i < len(self.view_boxes) else None
            if box is None:
                continue
            try:
                w_px, h_px = float(box.width()), float(box.height())
            except Exception:                               # noqa: BLE001
                continue
            if not (w_px > 0 and h_px > 0):
                continue
            w, h = w_px / float(scale), h_px / float(scale)
            # `set_view_rect_l0` and not a bare `setRange`: it sets both
            # axes with no padding (so the aspect lock has nothing to refit
            # and the trip is exactly reversible) and then issues both
            # request batches at once, instead of waiting out the motion
            # timer for pixels the user is already looking at.
            try:
                controller.set_view_rect_l0(cx - w / 2.0, cy - h / 2.0, w, h)
                applied = True
            except Exception:                               # noqa: BLE001
                continue
        return applied

    def view_rect_l0(self, idx=0):
        """Panel `idx`'s viewport as level-0 `(x, y, w, h)`, or None."""
        boxes = self.view_boxes
        if idx >= len(boxes) or boxes[idx] is None:
            return None
        try:
            (x0, x1), (y0, y1) = boxes[idx].viewRange()
        except Exception:                                   # noqa: BLE001
            return None
        return (float(x0), float(y0), float(x1) - float(x0),
                float(y1) - float(y0))

    # ── the shared selection ─────────────────────────────────────────────

    def set_channel(self, channel, *, params_for=None, tint=None):
        """Every panel moves to `channel` TOGETHER, in one publication.

        Three panels exist to be compared, so a channel switch that reaches
        them one at a time makes "this one loaded first" look like "this
        algorithm is different". Measured on the real 59040x35520 slide,
        with 16 ms scene sampling and a paint-level probe, the old
        one-call-per-panel loop published the three final images:

            level 0, everything HOT-prepared   401 / 401 / 473 ms  (71 ms)
            level 1, everything HOT-prepared   445 / 523 / 618 ms  (173 ms)
            level 0, cuCIM evicted             413 / 518 / 700 ms  (287 ms)

        -- with correction compute 0 in the first two. The serialisation
        was never in the compute layer and never in the paint layer: it was
        in DELIVERY. Three separate `set_selection` calls each hand their
        cache hits back through a queued signal, so each lands in its own
        GUI turn and is painted in its own frame.

        So this is a two-path publication:

        FAST PATH. A read-only preflight (`_missing_for`) asks whether the
        target is completely ready -- overview record resident, Original's
        raw tiles for the viewport in the raw cache, TopHat's and cuCIM's
        exact `CorrectionKey`s in the corrected cache, all at the level the
        panels are actually on. If it is, the three controllers are moved
        in ONE GUI turn (`_publish`), each of them swapping its cached
        viewport in synchronously, with no `processEvents` between them and
        one label/metrics update at the end. The preflight touches no
        pixels and issues no request, so this is not slower than the old
        loop: it is the old loop with a cache probe in front of it.

        COLD/PARTIAL PATH. Otherwise NOTHING on screen changes. The three
        panels keep the previous channel's three complete images, the strip
        says `Preparing TIM3 -- still showing CD22` in words, and the
        missing tiles are asked for at FOREGROUND priority
        (`COMPARE_PENDING_PRIORITY`) on the strip's own scheduler, under
        the strip's own generation. Those requests write only to the shared
        caches and the shared overview store -- never into a visible pool --
        because a pending target that could paint would be exactly the
        partial reveal this is here to stop. When the last one lands the
        fast path runs and all three change at once.
        """
        self._pending_serial += 1
        serial = self._pending_serial
        self._cancel_pending()
        if not self.controllers:
            return
        params_for = params_for or self._displayed_params_for
        if tint is None:
            tint = self._displayed_tint

        if channel == self._displayed_channel:
            # Not a switch. The row the user clicked may carry different
            # parameters or a different colour, and those are applied the
            # way they always were -- there is nothing to stage, and
            # "Preparing CD22 -- still showing CD22" would be nonsense.
            self._publish(channel, params_for, tint)
            return

        missing = self._missing_for(channel, params_for)
        if missing is None or not missing:
            # None: readiness is not answerable (no viewport yet, a fake
            # backend in a test). Publishing straight through is what this
            # method has always done and is still the right answer -- a
            # strip that cannot preflight must not become a strip that
            # cannot switch.
            self._publish(channel, params_for, tint)
            return
        self._enter_pending(serial, channel, params_for, tint, missing)

    # ── the staged publication ───────────────────────────────────────────

    def _publish(self, channel, params_for, tint):
        """Move all three panels to `channel` in ONE GUI turn.

        The loop itself is unchanged -- it is the same three
        `set_selection` calls in the same order -- and that is deliberate:
        atomicity here is a property of the TURN, not of a new mechanism.
        What makes the turn produce one frame instead of three is that
        every one of the three finds its tiles in the cache and swaps them
        in synchronously (`ExploreController._try_atomic_cached_channel_swap`),
        so no queued delivery is left to arrive afterwards.

        No `processEvents`, no timer, no yield: from the first
        `set_selection` to the last, Qt is never given the chance to paint.
        """
        self._disconnect_overview_watch()
        for source, controller in zip(COMPARE_SOURCES, self.controllers):
            if controller is None:
                continue
            method = COMPARE_METHODS.get(source)
            params = () if method is None else tuple(
                params_for(source) if params_for is not None else ())
            controller.set_selection(channel=channel, method=method,
                                     params=params)
            if tint is not None:
                controller.set_tint(tint)
        self._displayed_channel = channel
        self._displayed_params_for = params_for
        self._displayed_tint = tint
        self._clear_pending_banner()
        # AFTER all three, so the specs HOT is given are read once the new
        # channel is the strip's channel. The channel change itself already
        # reaches HOT through the host controller's own signals; this call
        # exists for the case where the row the user clicked also carries
        # different per-channel parameters.
        self.refresh_hot()
        # A cold publication stopped HOT to give the pending target the
        # device. Now that the target IS the strip's channel, HOT comes
        # back and plans the +-1/+-2 neighbourhood around it. Only when it
        # is actually gone: a live coordinator has already been told, by
        # the three `set_selection` calls above, that its plan is stale,
        # and re-mounting logic must not turn every row click into an extra
        # `replan`.
        if self._hot is None:
            self.start_hot()
        self.publication_changed.emit()

    def pending_text(self):
        """The words the strip is showing about a pending switch, or None."""
        if self._pending_error is not None:
            return self._pending_error
        if self._pending is None:
            return None
        return "Preparing %s — still showing %s" % (
            self._pending["channel"], self._displayed_channel or "the previous channel")

    @property
    def pending(self):
        """True while a channel switch is prepared but not yet published."""
        return self._pending is not None

    @property
    def pending_channel(self):
        return self._pending["channel"] if self._pending else None

    @property
    def displayed_channel(self):
        """The channel the three panels ARE showing. Never the pending one."""
        return self._displayed_channel

    def _enter_pending(self, serial, channel, params_for, tint, missing):
        """Keep the old three images up and prepare `channel` in the back."""
        # HOT is a producer of work on the very scheduler the pending
        # target now needs, and it is the LOW-priority producer: stopping
        # it cancels its generation, so its queued neighbour tiles are
        # dropped rather than left competing with the thing the user is
        # waiting for. It comes back, re-planned around the new channel,
        # in `_publish`.
        self.stop_hot()
        self._pending_gen_n += 1
        generation = ("compare_pending", self._pending_gen_n)
        self._pending = {
            "serial": serial,
            "channel": channel,
            "params_for": params_for,
            "tint": tint,
            "generation": generation,
            "overview_wanted": False,
            "asked": set(),
            "token": self.pending_token(channel, params_for),
        }
        self._pending_error = None
        self._show_pending_banner()
        self._request_missing(missing)

    def _request_missing(self, missing):
        """Ask for exactly what the preflight said was not there.

        CACHES ONLY. These requests are the strip's, not a controller's:
        nothing they deliver is pooled, drawn or given to a view. The
        scheduler writes the result into the shared raw/corrected cache and
        the callback's only job is to re-run the preflight.
        """
        pending = self._pending
        if pending is None:
            return
        stacks = self._stacks
        if stacks is None:
            return
        serial = pending["serial"]
        generation = pending["generation"]
        overview_channel = None
        keys = []
        for item in missing:
            if item[0] == "overview":
                overview_channel = item[1]
            else:
                keys.append(item[1])
        if overview_channel is not None and not pending["overview_wanted"]:
            # A record read is not a scheduler request and so never reaches
            # `_pending_tile`. Without this the one case where the ONLY
            # thing missing is the overview -- every tile of the target
            # already cached, which is precisely what HOT leaves behind for
            # a channel whose record was evicted -- would wait for a
            # delivery that is never coming.
            self._connect_overview_watch()
            # The one-at-a-time shared overview worker. `live=False`: this
            # is not the channel any controller is displaying yet, so it
            # must not evict a displayed controller's own live interest.
            pending["overview_wanted"] = True
            host = self._hot_host() or self.controllers[0]
            try:
                host.prepare_overview_async(overview_channel)
            except Exception:                               # noqa: BLE001
                pass
        # ONCE PER KEY PER GENERATION. The scheduler is single-flight, so a
        # second request for a key already in flight is not a second read --
        # it is a second WAITER, and therefore a second callback, and
        # therefore a second settle, which asks again for everything still
        # missing. That feedback loop multiplies the callbacks on every
        # delivery. `asked` is what breaks it; a re-plan makes a new
        # generation and starts it empty.
        asked = pending["asked"]
        for key in keys:
            if key in asked:
                continue
            request = TileRequest(key=key, generation=generation,
                                  priority=COMPARE_PENDING_PRIORITY + len(asked))
            asked.add(key)
            # Worker thread: emit only (see `_pending_tile`).
            callback = (lambda result, _serial=serial:
                        self._pending_tile.emit(_serial, result))
            try:
                stacks.scheduler.request(request, callback)
            except Exception:                               # noqa: BLE001
                asked.discard(key)

    def _connect_overview_watch(self):
        """Listen for the shared overview worker's answer, once."""
        if self._overview_watch is not None:
            return
        host = self._hot_host() or (self.controllers or [None])[0]
        if host is None:
            return
        try:
            host.overview_prepared.connect(self._on_overview_prepared)
        except Exception:                                   # noqa: BLE001
            return
        self._overview_watch = host

    def _disconnect_overview_watch(self):
        host, self._overview_watch = self._overview_watch, None
        if host is None:
            return
        try:
            host.overview_prepared.disconnect(self._on_overview_prepared)
        except (TypeError, RuntimeError):
            pass

    def _on_overview_prepared(self, _source, channel, _level, ok):
        pending = self._pending
        if pending is None or channel != pending["channel"]:
            return
        if not ok:
            self._fail_pending("its overview could not be read")
            return
        self._settle_pending()

    def _on_pending_tile(self, serial, result):
        """GUI thread. One pending tile is in the cache (or failed)."""
        pending = self._pending
        if pending is None or serial != pending["serial"]:
            return
        error = getattr(result, "error", None)
        if error is not None and error not in ("cancelled", "stale"):
            # A target that cannot be prepared must not take the panels
            # down with it: the old channel's three complete images stay
            # up, and the words say why.
            self._fail_pending(error)
            return
        self._settle_pending()

    def _settle_pending(self):
        """Re-run the preflight; publish when nothing is missing."""
        pending = self._pending
        if pending is None:
            return
        missing = self._missing_for(pending["channel"], pending["params_for"])
        if missing is None or not missing:
            channel = pending["channel"]
            params_for = pending["params_for"]
            tint = pending["tint"]
            self._pending = None
            self._publish(channel, params_for, tint)
            return
        # Still short. Nothing already asked for under this generation is
        # asked for again (see `_request_missing`); this call exists for
        # the keys a re-plan added.
        self._request_missing(missing)

    def _fail_pending(self, error):
        self._pending = None
        self._disconnect_overview_watch()
        self._pending_error = (
            "Preparing failed — still showing %s (%s)"
            % (self._displayed_channel or "the previous channel", error))
        self._show_pending_banner()

    def _cancel_pending(self):
        """Drop a pending switch. Idempotent.

        The generation is cancelled at the scheduler, so queued-but-unstarted
        work for it is dropped; work already RUNNING is left to finish into
        the cache, where it is simply a tile somebody may want later. No
        result of a cancelled generation can publish: `_on_pending_tile`
        compares serials, and the serial has moved.
        """
        pending, self._pending = self._pending, None
        self._pending_error = None
        self._disconnect_overview_watch()
        if pending is not None and self._stacks is not None:
            try:
                self._stacks.scheduler.cancel_generation(pending["generation"])
            except Exception:                               # noqa: BLE001
                pass
        self._clear_pending_banner()

    def _show_pending_banner(self):
        text = self.pending_text()
        self._pending_lbl.setText(text or "")
        self._pending_lbl.setVisible(bool(text))
        self.pending_changed.emit()

    def _clear_pending_banner(self):
        """Take the words down. Silent when there were none: this runs on
        every publication, and a listener must not be woken by a state that
        did not change."""
        self._pending_error = None
        if not (self._pending_lbl.isVisible() or self._pending_lbl.text()):
            return
        self._pending_lbl.setText("")
        self._pending_lbl.setVisible(False)
        self.pending_changed.emit()

    # ── readiness ────────────────────────────────────────────────────────

    def pending_token(self, channel, params_for=None):
        """Everything a publication of `channel` depends on, as one value.

        A pending plan is only valid while this is unchanged, so it names
        every input whose movement would make an already-prepared result
        the wrong thing to publish: the dataset's source identity, the
        target channel, each panel's method and EFFECTIVE parameters, its
        display level, its visible tile set (which is the camera, expressed
        in the units the keys are actually built in) and its display
        mapping. Returns None when the panels have no viewport yet and the
        question has no answer.
        """
        plans = self._panel_plans(channel, params_for)
        if plans is None:
            return None
        first = plans[0]
        panels = tuple((p["method"], p["eff"], p["mapping"], p["level"],
                        p["tiles"]) for p in plans)
        return (first["source"], channel, panels, first["quality"],
                first["algorithm_version"])

    def _panel_plans(self, channel, params_for=None):
        """Per panel: its method, effective params, level, tiles, mapping.

        Read PER PANEL and not once from the first of the three. They are
        one camera, but they are aspect-locked columns whose widths differ
        by a pixel or two, so the tiling of "the same" viewport can differ
        at its edge -- and it is each panel's OWN visible set that its own
        publication has to be complete over. Reading panel 0's and using it
        for all three would let a panel with one extra tile column be
        called ready when it is not, and that panel would then fall back to
        the queued path: one late frame, which is the whole of what this is
        here to prevent.

        This is also the single place those facts are read, so the
        readiness probe and the invalidation token can never be answering
        slightly different questions. None when any panel has no viewport
        yet or cannot answer (a fake backend in a test).
        """
        controllers = self.controllers
        if not controllers or any(c is None for c in controllers):
            return None
        plans = []
        for source, controller in zip(COMPARE_SOURCES, controllers):
            try:
                snapshot = controller.snapshot()
                downsample = controller.provider.level_downsample(snapshot.level)
                mapping = tuple(controller.display_mapping)
            except Exception:                               # noqa: BLE001
                return None
            if snapshot.bbox_l0 is None or not snapshot.visible_tiles:
                return None
            method = COMPARE_METHODS.get(source)
            base = () if method is None else tuple(
                params_for(source) if params_for is not None else ())
            try:
                eff = tuple(effective_param(p, snapshot.level, downsample)
                            for p in base)
            except Exception:                               # noqa: BLE001
                return None
            plans.append({
                "controller": controller,
                "method": method,
                "eff": eff,
                "mapping": mapping,
                "level": snapshot.level,
                "tiles": tuple(sorted(snapshot.visible_tiles)),
                "source": snapshot.source,
                "quality": snapshot.quality,
                "algorithm_version": snapshot.algorithm_version,
            })
        return plans

    def _missing_for(self, channel, params_for=None):
        """READ-ONLY preflight: what `channel` still needs, as a list.

        Entries are `("overview", channel)` or `("tile", key)`. An empty
        list means the fast path is available. None means the question
        cannot be answered here (no viewport, a fake backend), which the
        caller treats as "publish the old way".

        Nothing in here reads the slide, runs a correction or waits on a
        worker: it is one cache lookup per visible tile per panel plus one
        resident-record test, all on the GUI thread, all O(tiles).
        """
        stacks = self._stacks
        if stacks is None:
            return None
        plans = self._panel_plans(channel, params_for)
        if plans is None:
            return None
        scheduler = stacks.scheduler
        raw_cache = getattr(scheduler, "raw_cache", None)
        corrected_cache = getattr(scheduler, "corrected_cache", None)
        if raw_cache is None or corrected_cache is None:
            return None
        first = plans[0]
        try:
            resident = first["controller"].has_overview_record(
                channel, source=first["source"])
        except Exception:                                   # noqa: BLE001
            return None
        missing = []
        if not resident:
            # THE gate, and the reason a cached-tile-only test is not
            # enough: without this channel's overview record its display
            # range is unknown, `_blocked_on_overview` refuses to draw, and
            # `_try_atomic_cached_channel_swap` refuses to swap.
            missing.append(("overview", channel))
        grid = stacks.grid
        for plan in plans:
            method = plan["method"]
            for tx, ty in plan["tiles"]:
                address = TileAddress(grid=grid, level=plan["level"],
                                      tx=tx, ty=ty)
                if method is None:
                    # The Original panel's visible image IS its raw layer:
                    # it never produces a corrected tile, so the raw cache
                    # is what has to be complete for it.
                    key = RawKey(source=plan["source"], channel=channel,
                                 tile=address)
                    cache = raw_cache
                else:
                    key = CorrectionKey(
                        source=plan["source"], channel=channel, tile=address,
                        method=method, params=plan["eff"],
                        algorithm_version=plan["algorithm_version"],
                        quality=plan["quality"])
                    cache = corrected_cache
                if cache.get(key) is None:
                    missing.append(("tile", key))
        return missing

    def _invalidate_pending_on_move(self):
        """A camera move re-plans a pending switch against the new viewport.

        Publishing from a stale viewport is the one thing a pending state
        must never do: the tiles it prepared are for where the panels WERE.
        The token is the test -- if it still matches, the same plan is
        still the right one and nothing is disturbed.
        """
        pending = self._pending
        if pending is None:
            return
        token = self.pending_token(pending["channel"], pending["params_for"])
        if token == pending.get("token"):
            return
        pending["token"] = token
        missing = self._missing_for(pending["channel"], pending["params_for"])
        if missing is None:
            return
        try:
            self._stacks.scheduler.cancel_generation(pending["generation"])
        except Exception:                                   # noqa: BLE001
            pass
        self._pending_gen_n += 1
        pending["generation"] = ("compare_pending", self._pending_gen_n)
        pending["overview_wanted"] = False
        # A new generation asks afresh: the keys of the OLD viewport were
        # asked for under a generation that is now cancelled, and the new
        # viewport's keys have never been asked for at all.
        pending["asked"] = set()
        if not missing:
            channel = pending["channel"]
            params_for = pending["params_for"]
            tint = pending["tint"]
            self._pending = None
            self._publish(channel, params_for, tint)
            return
        self._request_missing(missing)

    def set_params(self, params_for, *, method=None):
        """Re-select the edited method, or both corrected methods if omitted.

        Original has no parameter and is deliberately left alone. Keeping
        the method identity here prevents a sigma edit from cancelling and
        recomputing TopHat (and vice versa).
        """
        self._displayed_params_for = params_for
        if self._pending is not None:
            # The parameters are IN the CorrectionKey, so an edit makes
            # every key the pending plan prepared a key nobody will ask for
            # again. Re-plan the same target under the new numbers rather
            # than publish a result computed from the old ones.
            pending = self._pending
            pending["params_for"] = params_for
            self._invalidate_pending_on_move()
            return
        for source, controller in zip(COMPARE_SOURCES, self.controllers):
            source_method = COMPARE_METHODS.get(source)
            if (controller is None or source_method is None
                    or method is not None and source_method != method):
                continue
            controller.set_selection(method=source_method,
                                     params=tuple(params_for(source)))
        # A legacy/all-method refresh also updates HOT immediately. A named
        # edit deliberately leaves its generation alone: cancelling one shared
        # HOT generation for a sigma edit would abandon/requeue TopHat work too.
        # Enter soon suspends HOT for the production worker; if the user instead
        # changes channel, `_publish` re-reads all live specs before replanning.
        if method is None:
            self.refresh_hot()

    def set_tint(self, rgb):
        for controller in self.controllers:
            if controller is not None:
                controller.set_tint(rgb)

    def set_display_mapping(self, lo, hi, gamma=None, *, channel=None):
        for controller in self.controllers:
            if controller is not None:
                controller.set_display_mapping(lo, hi, gamma,
                                               channel=channel)

    def set_nucleus_display_mapping(self, lo, hi, gamma=None):
        for overlay in self.overlays:
            if overlay is not None:
                overlay.set_display_mapping(lo, hi, gamma)

    def set_nucleus_tint(self, rgb):
        for overlay in self.overlays:
            if overlay is not None:
                overlay.set_tint(rgb)

    def set_nucleus_enabled(self, enabled):
        for controller, overlay in zip(self.controllers, self.overlays):
            if overlay is not None:
                overlay.set_enabled(bool(enabled), host=controller)

    def set_nucleus_suppressed(self, suppressed):
        for controller, overlay in zip(self.controllers, self.overlays):
            if overlay is not None:
                overlay.set_suppressed(bool(suppressed), host=controller)

    def set_marker_visible(self, visible):
        for controller in self.controllers:
            if controller is not None:
                controller.set_marker_visible(bool(visible))

    def selection(self):
        """`[(channel, method, params), ...]` -- what each panel is showing.
        Three entries that agree on channel and disagree on method is the
        invariant this mode exists to hold."""
        out = []
        for controller in self.controllers:
            if controller is None:
                out.append(None)
            else:
                out.append((controller.channel, controller.method,
                            tuple(controller.params)))
        return out

    # ── the GPU hand-off ─────────────────────────────────────────────────

    def suspend(self, reason, badge=None):
        """Off screen: stop issuing, drop what is queued, join the floors,
        and KEEP the pools, the caches and the camera -- so coming back to
        the same place is a re-issue for what is missing, not a rebuild.

        The three are suspended in one turn deliberately. They share a
        scheduler, and `suspend_for_production` waits that scheduler idle;
        suspending them one at a time would make each of the first two wait
        out the other two's traffic.
        """
        if self._suspended or not self.controllers:
            return {}
        self._suspended = True
        # FIRST, and before `suspend_for_production` waits the scheduler
        # idle: HOT is a producer of scheduler work, and a producer that is
        # still refilling while somebody waits for idle is a wait that need
        # not end. This is also the one place that covers every way compare
        # leaves the screen, because all of them come through here.
        self.stop_hot()
        # And for the same reason, and through the same single door: a
        # pending switch is a producer of scheduler work too, and one whose
        # publication would land on panels nobody is looking at. Leaving
        # compare, a Process or Save run and a dataset switch all arrive
        # here, so this is where a pending target is dropped.
        self._cancel_pending()
        timings = {}
        for source, controller in zip(COMPARE_SOURCES, self.controllers):
            if controller is None:
                continue
            try:
                timings[source] = controller.suspend_for_production(
                    reason, badge=badge)
            except Exception:                               # noqa: BLE001
                pass
        return timings

    def resume(self):
        if not self._suspended:
            return
        self._suspended = False
        for controller in self.controllers:
            if controller is None:
                continue
            try:
                controller.resume_from_production()
            except Exception:                               # noqa: BLE001
                pass
        # Back on screen: plan again, from the channel, the viewport and
        # the effective parameters as they are NOW -- not from whatever was
        # true when the strip was put away.
        self.start_hot()

    @property
    def suspended(self):
        return self._suspended

    # ── neighbour preparation (HOT) ──────────────────────────────────────
    #
    # ONE coordinator for the whole strip, not one per panel. The three
    # panels are one instrument -- one camera, one channel, one backend --
    # so "prepare the neighbouring channels" is one question with one
    # answer, and three coordinators would ask it three times and spend
    # three times the budget racing each other for the same scheduler.
    #
    # It is `viewer.multichannel_prefetch.MultiChannelPrefetchController`,
    # the production HOT implementation, mounted on THIS backend: the
    # strip's own scheduler, its own corrected cache and the overview store
    # the strip is using. Nothing is shared with the full image beyond the
    # overview store the strip already borrowed -- sharing the scheduler
    # would make `suspend_for_production`'s "wait until idle" mean "wait for
    # the other mode too", and sharing the corrected cache would make the
    # two modes evict each other (see `build_compare_stacks`).
    #
    # HOT writes CACHES ONLY. It never touches a view, a pool or a camera:
    # a prepared channel is one whose corrected tiles and overview record
    # are resident, so that switching to it is a cache read rather than a
    # computation. What the user sees is still produced by the three
    # controllers, from the same code path as an unprepared channel.
    #
    # HOST CONTROLLER: the TopHat panel, deliberately one of the three and
    # not all of them. The three cameras are the same camera, so any of
    # them reports the same viewport, level and visible tile set; the
    # difference is that Original carries no method and no parameters, so a
    # parameter edit is invisible in its selection context and HOT would
    # not learn that its plan had gone stale. TopHat has both.

    HOT_HOST_SOURCE = "tophat"

    def set_hot_specs_provider(self, provider):
        """Install the callback that answers "what is the neighbourhood?".

        `provider()` returns the `ChannelCorrectionSpec`s for every channel
        the user can switch to, IN THE ORDER THE USER SEES THEM -- that
        order is what "the channel above" and "the channel below" mean, and
        it is the page's to know, not the strip's.

        A callback rather than a list because the parameters in it are
        live: the answer is re-read every time HOT re-plans, so a spec can
        never be older than the last plan.
        """
        self._hot_specs_provider = provider

    @property
    def hot(self):
        """The live HOT coordinator, or None. For tests and measurement."""
        return self._hot

    def hot_stats(self):
        """HOT's counters, or None when it is not running."""
        return dict(self._hot.stats) if self._hot is not None else None

    def _hot_host(self):
        for source, controller in zip(COMPARE_SOURCES, self.controllers):
            if source == self.HOT_HOST_SOURCE and controller is not None:
                return controller
        return None

    def _hot_specs(self):
        provider = self._hot_specs_provider
        if provider is None:
            return ()
        try:
            return tuple(provider() or ())
        except Exception:                                   # noqa: BLE001
            return ()

    def start_hot(self):
        """Mount HOT, or re-plan the one already mounted.

        Refuses while the strip is suspended or unbuilt: HOT off screen
        would be spending the GPU on channels of a mode nobody is looking
        at, and that is exactly what the suspend contract exists to stop.
        """
        if self._stacks is None or self._suspended:
            return None
        specs = self._hot_specs()
        if not specs:
            return None
        if self._hot is not None:
            self._hot.set_specs(specs)
            self._hot.replan()
            return self._hot
        host = self._hot_host()
        if host is None:
            return None
        from ...viewer.multichannel_prefetch import (
            MultiChannelPrefetchController)
        try:
            hot = MultiChannelPrefetchController(
                host, self._stacks.scheduler, specs, self._stacks.grid,
                parent=self)
        except Exception:                                   # noqa: BLE001
            # A strip that cannot prepare its neighbours is a slower strip,
            # never a broken one.
            return None
        self._hot = hot
        # The camera is already sitting where the user put it, so there is
        # no gesture left to go quiet on its own. `replan` arms the same
        # confirmation a real settle would.
        hot.replan()
        return hot

    def stop_hot(self):
        """Cancel HOT and let go of the host. Idempotent."""
        hot, self._hot = self._hot, None
        if hot is None:
            return
        try:
            hot.stop()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            hot.setParent(None)
            hot.deleteLater()
        except RuntimeError:
            pass

    def refresh_hot(self):
        """Re-read the specs. A no-op when nothing HOT cares about moved."""
        if self._hot is None:
            return
        self._hot.set_specs(self._hot_specs())
