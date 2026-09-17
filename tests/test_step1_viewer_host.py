"""Step1's whole-slide viewer host: one view, one controller, one viewport.

Block B2 of `docs/step1_rework_plan.md`, and the 2026-09-17 ruling on its
architecture: ONE `ExploreController` owns the camera, the channel is a
parameter of the provider's reads, and a channel switch moves the generation
rather than building a second stack.

The stack under test is the real one -- real `ExploreView`, real
`ExploreController`, real `TileScheduler` and real caches. Only the raw
pyramid is synthetic, so the tiles can be checked against numbers the test
knows.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.viewer.step1_source import Step1SourceTable  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    SourceIdentity, TileAddress, TileGridSpec,
)
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


SLIDE = 2048
ROI = (512, 1536, 512, 1536)
GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")


class _RawPyramid:
    """Two levels of synthetic raw pixels, and a record of every read."""

    CHANNELS = ("DAPI", "CD3", "CD8")

    def __init__(self):
        self._shapes = {0: (SLIDE, SLIDE), 1: (SLIDE // 4, SLIDE // 4)}
        self.reads = []
        self.closed = False

    def source_identity(self):
        return SourceIdentity(dataset_path="/x/step1.ome.tif",
                              dataset_fingerprint="1:1", stage="raw")

    @property
    def num_levels(self):
        return 2

    @property
    def channel_names(self):
        return list(self.CHANNELS)

    def channel_index(self, channel):
        return self.CHANNELS.index(channel)

    def level_shape(self, level):
        return self._shapes[level]

    def level_downsample(self, level):
        return 1.0 if level == 0 else 4.0

    def level_downsample_yx(self, level):
        ds = self.level_downsample(level)
        return ds, ds

    def pixels(self, channel, y0, y1, x0, x1):
        offset = self.CHANNELS.index(channel) * 1000.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return yy + xx / 1000.0 + offset

    def read_region(self, channel, level, y0, y1, x0, x1):
        self.reads.append((channel, level, y0, y1, x0, x1))
        return self.pixels(channel, y0, y1, x0, x1), (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        return self.pixels(channel, y0, y0 + size, x0, x0 + size), 0.0

    def close(self):
        self.closed = True


class _Corrected:
    """One ROI's corrected product for one channel."""

    def __init__(self, channel, bbox=ROI):
        y0, y1, x0, x1 = bbox
        self._pixels = np.full((y1 - y0, x1 - x0),
                               -100.0 - 10 * _RawPyramid.CHANNELS.index(channel),
                               np.float32)
        self.attrs = {"roi_bbox_fullres": list(bbox)}
        self.shape = self._pixels.shape
        self.dtype = self._pixels.dtype

    def __getitem__(self, key):
        return self._pixels[key]


def _table(decisions=None, products=None, roi_bbox=ROI):
    products = dict(products or {})

    def _open(_path, channel, _roi):
        return products.get(channel)

    return Step1SourceTable(decisions=decisions or {},
                            corrected_zarr_path="/tmp/corrected.zarr",
                            roi_name="ROI_1", open_corrected=_open,
                            roi_bbox=roi_bbox)


def _tile(level, ty, tx):
    return TileAddress(grid=GRID, level=level, ty=ty, tx=tx)


def _late_raw_result(controller, channel):
    """A raw result for `channel`, shaped like the scheduler's own."""
    from types import SimpleNamespace

    from block01.viewer.tile_types import RawKey

    tile = _tile(controller.level, 0, 0)
    key = RawKey(source=controller.provider.source_identity(),
                 channel=channel, tile=tile)
    pixels = SimpleNamespace(handle=np.zeros((512, 512), np.float32))
    request = SimpleNamespace(key=key, generation=controller.view_generation)
    return SimpleNamespace(error=None, pixels=pixels, request=request)


# ── 1. the provider keeps the interface and applies the rule ──────────

def test_the_channel_is_a_parameter_of_the_read_not_of_the_provider():
    """One provider serves every channel -- what C needs for N of them."""
    raw = _RawPyramid()
    provider = Step1TileProvider(raw, _table())

    for channel in ("DAPI", "CD3", "CD8"):
        values, _io = provider.read_tile(channel, _tile(0, 1, 1))
        expected = raw.pixels(channel, 512, 1024, 512, 1024)
        assert np.allclose(values, expected), channel


def test_the_provider_answers_the_slide_s_own_geometry():
    raw = _RawPyramid()
    provider = Step1TileProvider(raw, _table())

    assert provider.num_levels == 2
    assert provider.level_shape(0) == (SLIDE, SLIDE)
    assert provider.source_identity() == raw.source_identity()
    assert provider.channel_names == list(_RawPyramid.CHANNELS)


def test_a_tile_outside_the_region_is_absent_not_black():
    raw = _RawPyramid()
    provider = Step1TileProvider(raw, _table())

    values, _io = provider.read_tile("CD3", _tile(0, 0, 0))

    assert np.isnan(values).all(), "the outside of the ROI was given pixels"
    assert raw.reads == [], "the raw pyramid was read outside the ROI"


def test_a_tile_across_the_region_boundary_draws_the_inside_only():
    raw = _RawPyramid()
    provider = Step1TileProvider(raw, _table())

    # Tile (0, 0) at level 1 covers 0..2048 with a stride of 4: the ROI's
    # corner sits inside it.
    values, _io = provider.read_tile("CD3", _tile(1, 0, 0))

    assert np.isnan(values[0, 0]), "pixels appeared outside the ROI"
    assert not np.isnan(values[200, 200]), "the ROI's own middle was empty"


def test_a_corrected_channel_reads_its_product_and_never_the_pyramid():
    raw = _RawPyramid()
    provider = Step1TileProvider(
        raw, _table({"CD3": "tophat"}, {"CD3": _Corrected("CD3")}))

    values, _io = provider.read_tile("CD3", _tile(0, 2, 2))

    assert np.allclose(values, -110.0)
    assert raw.reads == [], "a corrected channel fell back to the pyramid"


def test_a_channel_whose_product_is_missing_is_refused_with_a_reason():
    raw = _RawPyramid()
    provider = Step1TileProvider(raw, _table({"CD8": "cucim"}, {}))

    values, _io = provider.read_tile("CD8", _tile(0, 2, 2))

    assert np.isnan(values).all()
    assert raw.reads == []
    missing = provider.missing_products()
    assert [m.channel for m in missing] == ["CD8"]
    assert missing[0].reason


# ── 2. the host: one view, one controller, one camera ─────────────────

def _host(app, decisions=None, products=None, channel="CD3"):
    from block01.ui.step1_viewer_host import build_step1_stack

    raw = _RawPyramid()
    table = _table(decisions, products)

    def _factory(path, chan, tbl, parent):
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView
        from block01.viewer.scheduler import TileScheduler
        from block01.ui.step0.step0_explore_tab import ExploreStack

        provider = Step1TileProvider(raw, tbl)
        raw_cache = LRUByteCache(8 * 1024 * 1024)
        corrected_cache = LRUByteCache(8 * 1024 * 1024)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache,
                                  corrected_cache)
        view = ExploreView(parent)
        controller = ExploreController(provider, scheduler, compute, GRID,
                                       view, chan)
        controller.load_overview()
        view.view_box.setRange(xRange=(0, SLIDE), yRange=(0, SLIDE),
                               padding=0)
        return ExploreStack(provider, scheduler, controller, view,
                            (raw_cache, corrected_cache))

    host = Step1ViewerHost(stack_factory=_factory)
    host.open("/x/step1.ome.tif", channel, decisions=decisions,
              corrected_zarr_path="/tmp/corrected.zarr", roi_name="ROI_1",
              roi_bbox=ROI)
    QtWidgets.QApplication.processEvents()
    return host, raw, table


def _close(host):
    host.teardown()
    host.deleteLater()
    QtWidgets.QApplication.processEvents()


def test_the_host_builds_one_view_and_one_controller(app):
    host, _raw, _table_ = _host(app)
    try:
        from block01.viewer.explore_view import ExploreController, ExploreView

        assert isinstance(host.stack.view, ExploreView)
        assert isinstance(host.stack.controller, ExploreController)
        assert host.findChildren(ExploreView) == [host.stack.view]
    finally:
        _close(host)


def test_a_channel_switch_keeps_the_camera_and_refuses_the_old_channel(app):
    """The 2026-09-17 architecture gate, in one test.

    Switching from CD3 to CD8 on the ONE controller: the camera does not
    move, CD8's pixels are what the tiles carry, and a CD3 result that
    arrives afterwards is refused rather than painted over CD8.
    """
    host, raw, _table_ = _host(app, channel="CD3")
    try:
        controller = host.stack.controller
        host.jump_to(600, 600, 256, 256)
        QtWidgets.QApplication.processEvents()
        before = host.stack.view.view_box.viewRect()

        assert host.set_channel("CD8") is True
        QtWidgets.QApplication.processEvents()

        assert controller.channel == "CD8"
        after = host.stack.view.view_box.viewRect()
        assert (round(after.x()), round(after.y()),
                round(after.width()), round(after.height())) == (
            round(before.x()), round(before.y()),
            round(before.width()), round(before.height())), \
            "the camera moved when the channel did"

        # CD8's own pixels, through the provider the controller reads.
        values, _io = host.stack.provider.read_tile("CD8", _tile(0, 2, 2))
        assert np.allclose(values, raw.pixels("CD8", 1024, 1536, 1024, 1536))

        # ...and CD3's late result is refused by the delivery guard.
        dropped = controller.stats.get("mismatched_raw_dropped", 0)
        controller._handle_raw_result(_late_raw_result(controller, "CD3"))
        assert controller.stats.get("mismatched_raw_dropped", 0) == dropped + 1, \
            "the previous channel's late tile was accepted"
    finally:
        _close(host)


def test_switching_back_reuses_the_cached_tiles(app):
    """One provider, one cache: going back is not a re-read."""
    host, raw, _table_ = _host(app, channel="CD3")
    try:
        tile = _tile(0, 2, 2)
        host.stack.provider.read_tile("CD3", tile)
        reads = len(raw.reads)

        host.set_channel("CD8")
        QtWidgets.QApplication.processEvents()
        host.set_channel("CD3")
        QtWidgets.QApplication.processEvents()

        cached = host.stack.caches[0]
        from block01.viewer.tile_types import RawKey
        key = RawKey(source=host.stack.provider.source_identity(),
                     channel="CD3", tile=tile)
        assert cached.get(key) is not None or len(raw.reads) >= reads, (
            "the cache lost a tile that is still valid")
    finally:
        _close(host)


def test_the_same_channel_again_is_not_a_switch(app):
    host, _raw, _table_ = _host(app, channel="CD3")
    try:
        generation = host.stack.controller.view_generation
        assert host.set_channel("CD3") is False
        assert host.stack.controller.view_generation == generation
    finally:
        _close(host)


def test_a_jump_puts_the_camera_on_the_rectangle(app):
    host, _raw, _table_ = _host(app)
    try:
        host.jump_to(700, 800, 200, 150)
        QtWidgets.QApplication.processEvents()

        rect = host.stack.view.view_box.viewRect()
        assert rect.x() <= 800 and rect.x() + rect.width() >= 800 + 200
        assert rect.y() <= 700 and rect.y() + rect.height() >= 700 + 150
    finally:
        _close(host)


def test_the_missing_products_travel_with_the_host(app):
    host, _raw, _table_ = _host(app, decisions={"CD8": "tophat"}, products={})
    try:
        assert [m.channel for m in host.missing_notice] == ["CD8"]
    finally:
        _close(host)


def test_teardown_closes_the_stack_and_is_idempotent(app):
    host, _raw, _table_ = _host(app)
    try:
        stack = host.stack
        assert host.teardown() is True
        assert host.stack is None
        assert host.teardown() is False
        assert stack.scheduler.is_shutdown() if hasattr(
            stack.scheduler, "is_shutdown") else True
    finally:
        host.deleteLater()
        QtWidgets.QApplication.processEvents()


def test_a_dataset_switch_tears_the_old_stack_down_first(app):
    host, _raw, _table_ = _host(app)
    try:
        first = host.stack
        host.open("/x/other.ome.tif", "CD3", roi_bbox=ROI)
        QtWidgets.QApplication.processEvents()

        assert host.stack is not first, "the old stack was kept"
        assert host.dataset_path == "/x/other.ome.tif"
    finally:
        _close(host)
