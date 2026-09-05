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

# Left to right, and the same order (and the same names) as the full
# image's own method switch, so "the middle panel" and "the TopHat button"
# cannot come to mean different things.
COMPARE_SOURCES = ("original", "tophat", "cucim")
COMPARE_METHODS = {"original": None, "tophat": "tophat", "cucim": "cucim"}
COMPARE_TITLES = {"original": "Original", "tophat": "TopHat",
                  "cucim": "cucim"}


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
        for (column, col_lay), view in zip(self._columns, stacks.views):
            view.setParent(column)
            col_lay.addWidget(view, stretch=1)
        self._connect_camera_link()
        self._placeholder.setVisible(False)
        self._panes.setVisible(True)
        return stacks

    def teardown(self, *, wait_for_floor: bool = False):
        stacks, self._stacks = self._stacks, None
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
        """Every panel moves to `channel` together, each keeping its own
        method and taking that method's current parameter."""
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

    def set_params(self, params_for):
        """A parameter edit re-selects TopHat and cuCIM. Original has no
        parameter and is deliberately left alone: re-selecting it would
        cancel and re-issue a batch of raw tiles that cannot have changed."""
        for source, controller in zip(COMPARE_SOURCES, self.controllers):
            method = COMPARE_METHODS.get(source)
            if controller is None or method is None:
                continue
            controller.set_selection(method=method,
                                     params=tuple(params_for(source)))

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

    @property
    def suspended(self):
        return self._suspended
