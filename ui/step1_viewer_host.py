"""Step1's whole-slide viewer: one host, one view, one controller.

Block B2 of `docs/step1_rework_plan.md`. What is here:

* `Step1TileProvider` -- the raw pyramid wrapped in Step1's own source rule
  (`viewer.step1_source`). Its `read_tile(channel, tile)` keeps the
  provider's interface, so the channel is a PARAMETER and not a property of
  the instance: one provider, one scheduler and one cache serve every
  channel, and C can ask the same viewport for N of them;
* `Step1ViewerHost` -- the widget that owns the stack: one `ExploreView`, one
  `ExploreController`, built lazily and torn down completely.

WHAT IS DELIBERATELY NOT HERE. Step0's own stack, its preview methods and its
GPU hand-off are untouched (`ui/step0/step0_explore_tab.py` is not imported).
There is no Overlay or Fusion composition either: block B draws ONE channel --
the selected one -- as the载体 for the real-pixel gates, and the host is not
reachable from the interface (B.5, re-ruled 2026-09-17). C puts the多通道
composition on this same host, this same viewport and this same cache.

NO PIXELS IS NOT BLACK. Outside the analysis region, and for a channel whose
corrected product is missing, the provider yields NaN rather than zeros: a
consumer that paints it must treat it as absent. The three kinds of tile are
the source table's, unchanged.
"""

import numpy as np
from PyQt5 import QtCore, QtWidgets

from ..viewer import step1_source as sources
from ..viewer.tile_types import SourceIdentity

#: Tile edge, in level-0 pixels. The same value Step0's stack uses.
TILE_SIZE = 512

#: What the caches may hold before the least-recently-used goes.
RAW_CACHE_BYTES = 512 * 1024 * 1024
CORRECTED_CACHE_BYTES = 2 * 1024 * 1024 * 1024

#: What the viewer has to say about where the camera is standing. Block B
#: keeps the TEXT; the information layer that shows it is C's (B.4/B.5), and
#: no button, window or bus is added for it here.
STATUS_OK = ""
STATUS_OUTSIDE_ROI = (
    "Outside the analysis region: there are no pixels to show here. "
    "The region is what Step0 published; move back inside it to see the slide."
)

PLACEHOLDER_NO_DATASET = (
    "No image loaded\n\n"
    "Load an OME-TIFF in Step 0. Step 1 then shows the whole slide, with the "
    "channels the pipeline decided on."
)


class Step1TileProvider:
    """The raw provider, plus Step1's rule about which pixels may be drawn.

    Geometry, levels, channel names and the cache identity come straight from
    the wrapped provider -- this is the SAME slide. What changes is what a
    tile READ returns: the source table decides per channel, and the analysis
    region decides per tile.
    """

    def __init__(self, raw_provider, table):
        self._raw = raw_provider
        self._table = table

    # ── the wrapped provider's own answers ────────────────────────────
    def __getattr__(self, name):
        # Geometry is the slide's, not this wrapper's. Only the identity and
        # the reads below are Step1's business.
        return getattr(self._raw, name)

    def source_identity(self):
        """WHAT THESE PIXELS MEAN, which is more than which file they are in.

        The caches key on this. Delegating it to the raw provider would give
        a channel whose decision has become `tophat`, or one whose product
        was regenerated, the identity its raw tiles already have -- and the
        old tile would be served for the new question. The source table's
        token carries the region, the decisions, each product's own identity
        and the handoff revision, so any of those moving retires every key.
        """
        base = self._raw.source_identity()
        return SourceIdentity(
            dataset_path=base.dataset_path,
            dataset_fingerprint=base.dataset_fingerprint,
            stage="step1",
            corrected_artifact=self._table.identity_token())

    @property
    def source_table(self):
        return self._table

    def missing_products(self):
        return self._table.missing()

    # ── the reads ─────────────────────────────────────────────────────
    def _level_rect(self, tile):
        """A tile address as a rectangle in ITS OWN level's pixels.

        Level-k coordinates, not level-0 ones: a raw channel is then read
        from the pyramid at that level instead of being averaged down from
        level 0 (`viewer.step1_source.read_tile`).
        """
        stride = max(1, int(round(self._raw.level_downsample(tile.level))))
        size = tile.grid.tile_size
        y0 = tile.ty * size
        x0 = tile.tx * size
        h, w = self._raw.level_shape(tile.level)
        y1 = min(y0 + size, h)
        x1 = min(x0 + size, w)
        return (y0, y1, x0, x1), stride

    def _raw_reader(self, level):
        """`read_raw` for one level: the pyramid's own pixels, not level 0."""

        def _read(channel, rect):
            y0, y1, x0, x1 = rect
            values, _offset = self._raw.read_region(channel, level,
                                                    y0, y1, x0, x1)
            return np.asarray(values, np.float32)

        return _read

    def read_tile(self, channel, tile):
        """`(array, io_ms)` -- the provider's contract, Step1's pixels.

        Absent pixels are NaN. A channel with no product at all yields an
        all-NaN tile: it is refused, not drawn, and the host says why.
        """
        import time

        start = time.perf_counter()
        rect, stride = self._level_rect(tile)
        values, valid = sources.read_tile(
            self._table, channel, rect, stride=stride,
            read_raw=self._raw_reader(tile.level))
        io_ms = (time.perf_counter() - start) * 1000.0
        if values is None:
            size = tile.grid.tile_size
            return np.full((size, size), np.nan, np.float32), io_ms
        out = np.array(values, np.float32, copy=True)
        out[~valid] = np.nan
        return out, io_ms

    def read_region(self, channel, level, y0, y1, x0, x1):
        """The same rule, for the callers that read a rectangle directly.

        `y0..x1` are LEVEL-k coordinates, as the wrapped provider's own
        `read_region` takes them.
        """
        stride = max(1, int(round(self._raw.level_downsample(level))))
        values, valid = sources.read_tile(
            self._table, channel, (y0, y1, x0, x1), stride=stride,
            read_raw=self._raw_reader(level))
        if values is None:
            return np.full((max(0, y1 - y0), max(0, x1 - x0)), np.nan,
                           np.float32), (y0, x0)
        out = np.array(values, np.float32, copy=True)
        out[~valid] = np.nan
        return out, (y0, x0)


class Step1ViewerHost(QtWidgets.QWidget):
    """Owns Step1's viewer stack for the current dataset.

    Built on first use and never twice; a dataset switch tears the old one
    down COMPLETELY before anything is bound to the new one, in the pinned
    order -- scheduler joined, provider closed, caches dropped -- and is
    idempotent.

    NOT REACHABLE FROM THE INTERFACE in block B: nothing mounts this widget.
    The gates drive it directly, against a real `ExploreView` and real tiles.
    """

    def __init__(self, parent=None, stack_factory=None):
        super().__init__(parent)
        self._stack = None
        self._dataset_path = ""
        self._table = None
        self._channel = ""
        self._stack_factory = stack_factory or build_step1_stack

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QtWidgets.QLabel(PLACEHOLDER_NO_DATASET)
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color:#bbb; background:#1c1c1c;")
        layout.addWidget(self._placeholder)
        #: The channels refused for want of a product, and why. The host
        #: keeps the text; block B has no widget for it (B.4 lands with the
        #: visible viewer in C).
        self.missing_notice = []
        #: Where the camera is, in words. Empty while it is over the region.
        self._status = STATUS_OK

        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.teardown)

    # ── state ─────────────────────────────────────────────────────────
    @property
    def stack(self):
        return self._stack

    @property
    def channel(self):
        return self._channel

    @property
    def dataset_path(self):
        return self._dataset_path

    #: Emitted when the camera moves in or out of the analysis region. A
    #: LOCAL signal for C's information layer -- not a bus, and nothing in
    #: block B listens to it.
    status_changed = QtCore.pyqtSignal(str)

    @property
    def status(self):
        """What the viewer would say about the camera's position."""
        return self._status

    def refresh_status(self):
        """Recompute the status from the camera and the analysis region.

        Standing outside the region is not an error and not a crash: there
        are simply no pixels there, and the viewer says so.
        """
        text = STATUS_OK
        table = self._table
        stack = self._stack
        roi = None if table is None else table.roi_bbox()
        if stack is not None and roi is not None:
            rect = stack.view.view_box.viewRect()
            ry0, ry1, rx0, rx1 = roi
            y0, y1 = rect.y(), rect.y() + rect.height()
            x0, x1 = rect.x(), rect.x() + rect.width()
            if y1 <= ry0 or y0 >= ry1 or x1 <= rx0 or x0 >= rx1:
                text = STATUS_OUTSIDE_ROI
        if text != self._status:
            self._status = text
            self.status_changed.emit(text)
        return text

    # ── build / teardown ──────────────────────────────────────────────
    def open(self, dataset_path, channel, *, decisions=None,
             corrected_zarr_path="", roi_name="", roi_bbox=None,
             handoff_revision="", viewport_l0=None):
        """Show `channel` of `dataset_path`, building the stack if needed."""
        dataset_path = str(dataset_path or "")
        if not dataset_path:
            return None
        if self._stack is not None and dataset_path != self._dataset_path:
            self.teardown()
        if self._stack is None:
            self._table = sources.Step1SourceTable(
                decisions=decisions, corrected_zarr_path=corrected_zarr_path,
                roi_name=roi_name, roi_bbox=roi_bbox,
                handoff_revision=handoff_revision)
            self._stack = self._stack_factory(dataset_path, channel,
                                              self._table, self)
            self._dataset_path = dataset_path
            self._channel = channel
            self.missing_notice = list(self._table.missing())
            self._show(self._stack.view)
            self.refresh_status()
        elif channel and channel != self._channel:
            self.set_channel(channel)
        if viewport_l0 is not None:
            self.jump_to(*viewport_l0)
        return self._stack

    def set_channel(self, channel):
        """Draw another channel, keeping the camera where it is.

        ONE controller owns the viewport (ruling, 2026-09-17), so a channel
        change is a selection change on it: the camera is untouched, the
        generation moves, and the previous channel's late tiles are refused
        by that generation rather than by luck.
        """
        channel = str(channel or "")
        if not channel or self._stack is None or channel == self._channel:
            return False
        self._channel = channel
        self._stack.controller.set_selection(channel=channel)
        return True

    def jump_to(self, y0, x0, width, height):
        """Put the camera on a level-0 rectangle -- a patch, or a click on
        the shared Tissue Preview."""
        if self._stack is None:
            return False
        self._stack.controller.jump_to(int(y0), int(x0),
                                       int(width), int(height))
        self.refresh_status()
        return True

    def apply_display_mapping(self, lo, hi, gamma, channel=None):
        """The Intensity answer, from the shared state's own numbers.

        This host stores no window of its own (B.3): the caller reads
        `ChannelDisplayState` and hands the three numbers over.
        """
        if self._stack is None:
            return False
        controller = self._stack.controller
        setter = getattr(controller, "set_display_mapping", None)
        if setter is None:
            return False
        setter(float(lo), float(hi),
               None if gamma is None else float(gamma),
               channel=channel or self._channel)
        return True

    def teardown(self, *, wait_for_floor=False):
        """Scheduler joined, provider closed, caches dropped. Idempotent."""
        stack = self._stack
        self._stack = None
        self._dataset_path = ""
        self._channel = ""
        self._table = None
        self.missing_notice = []
        self._status = STATUS_OK
        if stack is None:
            return False
        try:
            stack.teardown(wait_for_floor=wait_for_floor)
        finally:
            self._show(self._placeholder)
        return True

    # ── widgets ───────────────────────────────────────────────────────
    def _show(self, widget):
        layout = self.layout()
        for index in reversed(range(layout.count())):
            item = layout.itemAt(index)
            child = item.widget()
            if child is not None and child is not widget:
                child.setParent(None)
        if widget is not None and layout.indexOf(widget) < 0:
            layout.addWidget(widget)
        if widget is not None:
            widget.setVisible(True)


def build_step1_stack(dataset_path, channel, table, parent_widget=None):
    """The real stack: provider -> scheduler -> controller -> view.

    Composed from the same primitives Step0's factory uses
    (`RawTileProvider`, `TileScheduler`, `LRUByteCache`, `TileGridSpec`,
    `ExploreView`, `ExploreController`) -- the pieces are shared, the
    ownership is not: Step0's own stack is neither moved nor touched.
    """
    import pyqtgraph as pg

    from ..viewer.caches import LRUByteCache
    from ..viewer.correction_compute import CorrectionCompute
    from ..viewer.explore_view import ExploreController, ExploreView
    from ..viewer.raw_tile_provider import RawTileProvider
    from ..viewer.scheduler import TileScheduler
    from ..viewer.tile_types import TileGridSpec
    from ..ui.step0.step0_explore_tab import ExploreStack

    pg.setConfigOptions(imageAxisOrder="row-major")

    raw = provider = scheduler = controller = view = None
    try:
        raw = RawTileProvider(dataset_path)
        if not channel or channel not in raw.channel_names:
            channel = raw.channel_names[0]
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(RAW_CACHE_BYTES)
        corrected_cache = LRUByteCache(CORRECTED_CACHE_BYTES)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache,
                                  corrected_cache)
        grid = TileGridSpec(tile_size=TILE_SIZE, source_chunk_shape=(),
                            grid_version="v1")
        view = ExploreView(parent_widget)
        controller = ExploreController(provider, scheduler, compute, grid,
                                       view, channel)
        controller.load_overview()
        h0, w0 = raw.level_shape(0)
        view.view_box.setRange(xRange=(0, w0), yRange=(0, h0), padding=0)
        return ExploreStack(provider, scheduler, controller, view,
                            (raw_cache, corrected_cache))
    except Exception:
        for closer in (getattr(scheduler, "shutdown", None),
                       getattr(raw, "close", None)):
            try:
                if closer is not None:
                    closer()
            except Exception:
                pass
        raise
