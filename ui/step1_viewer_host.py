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

#: Tile edge, in level-0 pixels. The same value Step0's stack uses.
TILE_SIZE = 512

#: What the caches may hold before the least-recently-used goes.
RAW_CACHE_BYTES = 512 * 1024 * 1024
CORRECTED_CACHE_BYTES = 2 * 1024 * 1024 * 1024

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
        # Geometry and identity are the slide's, not this wrapper's. Only the
        # reads below are Step1's business.
        return getattr(self._raw, name)

    @property
    def source_table(self):
        return self._table

    def missing_products(self):
        return self._table.missing()

    # ── the reads ─────────────────────────────────────────────────────
    def _level_rect(self, tile):
        """A tile address as a rectangle in LEVEL-0 pixels, plus its stride."""
        stride = int(round(self._raw.level_downsample(tile.level)))
        size = tile.grid.tile_size
        y0 = tile.ty * size * stride
        x0 = tile.tx * size * stride
        h0, w0 = self._raw.level_shape(0)
        y1 = min(y0 + size * stride, h0)
        x1 = min(x0 + size * stride, w0)
        return (y0, y1, x0, x1), max(1, stride)

    def _read_raw_region(self, channel, rect):
        y0, y1, x0, x1 = rect
        values, _offset = self._raw.read_region(channel, 0, y0, y1, x0, x1)
        return np.asarray(values, np.float32)

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
            read_raw=self._read_raw_region)
        io_ms = (time.perf_counter() - start) * 1000.0
        if values is None:
            size = tile.grid.tile_size
            return np.full((size, size), np.nan, np.float32), io_ms
        out = np.array(values, np.float32, copy=True)
        out[~valid] = np.nan
        return out, io_ms

    def read_region(self, channel, level, y0, y1, x0, x1):
        """The same rule, for the callers that read a rectangle directly."""
        stride = max(1, int(round(self._raw.level_downsample(level))))
        rect = (y0 * stride, y1 * stride, x0 * stride, x1 * stride)
        values, valid = sources.read_tile(
            self._table, channel, rect, stride=stride,
            read_raw=self._read_raw_region)
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

    # ── build / teardown ──────────────────────────────────────────────
    def open(self, dataset_path, channel, *, decisions=None,
             corrected_zarr_path="", roi_name="", roi_bbox=None,
             viewport_l0=None):
        """Show `channel` of `dataset_path`, building the stack if needed."""
        dataset_path = str(dataset_path or "")
        if not dataset_path:
            return None
        if self._stack is not None and dataset_path != self._dataset_path:
            self.teardown()
        if self._stack is None:
            self._table = sources.Step1SourceTable(
                decisions=decisions, corrected_zarr_path=corrected_zarr_path,
                roi_name=roi_name, roi_bbox=roi_bbox)
            self._stack = self._stack_factory(dataset_path, channel,
                                              self._table, self)
            self._dataset_path = dataset_path
            self._channel = channel
            self.missing_notice = list(self._table.missing())
            self._show(self._stack.view)
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
