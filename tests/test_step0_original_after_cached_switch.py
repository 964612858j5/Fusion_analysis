"""Step0 Full Image: back to Original after a cached cuCIM channel switch.

Found on the real machine (2026-09-23): in the full image, PD1 on cuCIM,
switch to another channel while staying on cuCIM (it looks right), then
switch that channel to Original -- the picture stays blurry and only
sharpens after a zoom in/out.

Traced to `ExploreController.set_selection`. A cuCIM channel switch whose
whole viewport is already corrected in the cache (what HOT prepares) is
served by the atomic swap: both pools are cleared, the corrected tiles are
pooled, and -- correctly, since the raw layer is hidden under the floor --
no raw is asked for. Going to Original afterwards changes only the method,
and that branch only re-issued the PRECISE batch, which has nothing to do
without a method. So the current level had no raw tile and no request for
one; the overview was the whole picture until the camera moved.

Everything is driven through `Step0ExploreTab.show_source`, the one call
the page makes for a channel or method change, over the real scheduler,
caches and correction compute. Only the slide is synthetic, injected where
`build_default_stack` opens its provider; the scheduler's `request` is
wrapped on the instance to COUNT, and the provider counts its reads.
"""

import os
import time
from collections import Counter

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtGui, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0.step0_explore_tab import Step0ExploreTab  # noqa: E402
from block01.viewer import raw_tile_provider as rtp  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey, RawKey, SourceIdentity)

SIGMA = 20
SLIDE = 4096
#: Where the view sits for the whole test, in level-0 pixels (y0, x0, w, h).
VIEW = (1024, 1024, 2048, 1536)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Slide:
    """A 4-level 2x pyramid with per-channel fine texture, counting reads."""

    channel_names = ["DAPI", "CD3", "CD20", "PD1"]
    num_levels = 4
    open_count = 1

    def __init__(self):
        self.tile_reads = Counter()

    def close(self):
        pass

    def source_identity(self):
        return SourceIdentity(dataset_path="/synthetic/slide.ome.tif",
                              dataset_fingerprint="1:1", stage="raw")

    def channel_index(self, channel):
        return self.channel_names.index(channel)

    def level_shape(self, level):
        return (SLIDE >> int(level), SLIDE >> int(level))

    def level_downsample(self, level):
        return float(1 << int(level))

    def level_downsample_yx(self, level):
        return (self.level_downsample(level),) * 2

    def _pixels(self, channel, level, y0, y1, x0, x1):
        ds = 1 << int(level)
        ys = (np.arange(y0, y1) * ds)[:, None]
        xs = (np.arange(x0, x1) * ds)[None, :]
        k = self.channel_names.index(channel) + 3
        # Cell-sized spots: sharp at level 0/1, averaged away in the overview.
        spots = (((ys // 7 + xs // 5 * k) % 5) == 0).astype(np.float32)
        return (1000.0 + 3000.0 * spots + 0.02 * (ys + xs)).astype(np.float32)

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self.level_shape(level)
        cy0, cy1 = max(0, min(int(y0), h)), max(0, min(int(y1), h))
        cx0, cx1 = max(0, min(int(x0), w)), max(0, min(int(x1), w))
        return self._pixels(channel, level, cy0, cy1, cx0, cx1), (cy0, cx0)

    def read_tile(self, channel, tile):
        h, w = self.level_shape(tile.level)
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        self.tile_reads[(channel, tile.level, tile.tx, tile.ty)] += 1
        return (self._pixels(channel, tile.level, y0, min(y0 + size, h),
                             x0, min(x0 + size, w)), 0.0)


def _drain(app, ms):
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents()
        QtTest.QTest.qWait(10)


def _until(app, predicate, timeout_ms=20000):
    end = time.perf_counter() + timeout_ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents()
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return bool(predicate())


class _Harness:
    def __init__(self, app, monkeypatch):
        self.app = app
        self.slide = _Slide()
        monkeypatch.setattr(rtp, "RawTileProvider", lambda _path: self.slide)
        self.tab = Step0ExploreTab(page=None)
        self.tab.resize(800, 600)
        self.tab.show()
        _drain(app, 50)
        self.tab.set_dataset("/synthetic/slide.ome.tif")
        assert self.tab.show_source("CD3", "cucim", (SIGMA,))
        self.ctl = self.tab.stack.controller
        self.sch = self.tab.stack.scheduler
        self.requests = []
        real = self.sch.request

        def counted(req, callback):
            self.requests.append(req.key)
            return real(req, callback)

        self.sch.request = counted
        self.ctl.jump_to(*VIEW)
        assert _until(app, lambda: self.precise_covers() and self.ctl._floor_ready)

    # ── what is on screen ──────────────────────────────────────────────

    def raw_pooled(self):
        lvl = self.ctl.level
        return {c for c in self.ctl._visible_tiles
                if self.ctl._raw_pool.get(lvl, *c) is not None}

    def raw_covers(self):
        vis = self.ctl._visible_tiles
        return bool(vis) and self.raw_pooled() == set(vis)

    def raw_shown(self):
        lvl = self.ctl.level
        return {(e.tx, e.ty) for e in self.ctl._raw_pool.entries.values()
                if e.level == lvl and e.item.isVisible()}

    def precise_covers(self):
        lvl = self.ctl.level
        vis = self.ctl._visible_tiles
        return bool(vis) and all(self.ctl._precise_pool.get(lvl, *c) is not None
                                 for c in vis)

    def overview_shown(self):
        return bool(self.ctl.view.overview_item.isVisible())

    def render(self):
        img = self.tab.stack.view.grab().toImage().convertToFormat(
            QtGui.QImage.Format_Grayscale8)
        ptr = img.constBits()
        ptr.setsize(img.byteCount())
        return np.frombuffer(ptr, np.uint8).reshape(
            img.height(), img.bytesPerLine())[:, :img.width()].astype(np.float32)

    # ── what was asked for ─────────────────────────────────────────────

    def mark(self):
        return len(self.requests), Counter(self.slide.tile_reads)

    def raw_requests_since(self, mark, *, level=None):
        lvl = self.ctl.level if level is None else level
        return [k for k in self.requests[mark[0]:]
                if isinstance(k, RawKey) and k.channel == self.ctl.channel
                and k.tile.level == lvl]

    def reads_since(self, mark):
        now = self.slide.tile_reads
        return Counter({k: now[k] - mark[1].get(k, 0) for k in now
                        if now[k] - mark[1].get(k, 0) > 0})

    def corrected_resident(self, channel):
        cache = self.sch.corrected_cache
        saved = self.ctl.channel
        self.ctl.channel = channel      # key construction only; restored below
        try:
            keys = [self.ctl._make_correction_key(tx, ty)
                    for tx, ty in self.ctl._visible_tiles]
        finally:
            self.ctl.channel = saved
        return bool(keys) and all(cache.get(k) is not None for k in keys)

    def switch_via_cached_swap(self, target):
        """cuCIM `target`, served by the atomic PRECISE swap -- asserted,
        not assumed: a bench that silently took the ordinary path would
        prove nothing about this bug."""
        # Visit the target once so its corrected viewport and overview are
        # resident (what HOT does in the product), then come back.
        start = self.ctl.channel
        self.tab.show_source(target, "cucim", (SIGMA,))
        assert _until(self.app, lambda: self.precise_covers()
                      and self.corrected_resident(target))
        self.tab.show_source(start, "cucim", (SIGMA,))
        assert _until(self.app, self.precise_covers)
        _drain(self.app, 200)
        swaps = self.ctl.stats.get("atomic_channel_swaps", 0)
        raw_swaps = self.ctl.stats.get("atomic_raw_channel_swaps", 0)
        self.tab.show_source(target, "cucim", (SIGMA,))
        assert self.ctl.stats.get("atomic_channel_swaps", 0) == swaps + 1
        assert self.ctl.stats.get("atomic_raw_channel_swaps", 0) == raw_swaps
        assert (self.ctl.channel, self.ctl.method) == (target, "cucim")
        _drain(self.app, 300)
        assert self.precise_covers()
        assert not self.raw_pooled(), (
            "bench did not reproduce: the cached swap left raw tiles pooled")

    def teardown(self):
        self.tab.teardown(wait_for_floor=True)
        self.tab.deleteLater()
        _drain(self.app, 20)


@pytest.fixture
def bench(app, monkeypatch):
    h = _Harness(app, monkeypatch)
    yield h
    h.teardown()


def _sharpness(a):
    return float((4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1]
                  - a[1:-1, :-2] - a[1:-1, 2:]).var())


def _zoom_in_out(bench):
    vb = bench.ctl.view.view_box
    before = [list(r) for r in vb.viewRange()]
    vb.scaleBy((0.8, 0.8))
    _drain(bench.app, 300)
    vb.setRange(xRange=before[0], yRange=before[1], padding=0)


def test_original_after_a_cached_cucim_switch_sharpens_without_moving(bench):
    bench.switch_via_cached_swap("CD20")
    camera = [list(r) for r in bench.ctl.view.view_box.viewRange()]
    level = bench.ctl.level
    mark = bench.mark()

    bench.tab.show_source("CD20", None, ())
    assert (bench.ctl.channel, bench.ctl.method) == ("CD20", None)
    # Rendered in the same GUI turn: no tile can have landed yet, so this is
    # what the overview alone looks like at this camera.
    assert bench.overview_shown() and not bench.raw_shown()
    overview_only = bench.render()

    # The regression: nothing asked for the current level's raw tiles.
    asked = bench.raw_requests_since(mark)
    assert {(k.tile.tx, k.tile.ty) for k in asked} >= set(bench.ctl._visible_tiles), (
        f"Original issued {len(asked)} current-level raw requests for "
        f"{len(bench.ctl._visible_tiles)} visible tiles")

    # With the camera never touched, the raw layer fills the view and the
    # overview goes behind it.
    assert _until(bench.app, bench.raw_covers, 10000)
    _drain(bench.app, 200)
    assert [list(r) for r in bench.ctl.view.view_box.viewRange()] == camera
    assert bench.ctl.level == level
    assert bench.raw_shown() == set(bench.ctl._visible_tiles)
    assert not bench.overview_shown()
    settled = bench.render()

    # Control: one zoom in/out back to the same camera shows the same picture.
    _zoom_in_out(bench)
    assert _until(bench.app, bench.raw_covers, 10000)
    _drain(bench.app, 300)
    assert bench.ctl.level == level
    zoomed = bench.render()
    assert float(np.abs(settled - zoomed).mean()) < 1.0

    # ...and the settled picture is not the overview: the fine structure the
    # overview averages away is back.
    assert _sharpness(settled) > 3.0 * _sharpness(overview_only), (
        _sharpness(settled), _sharpness(overview_only))


def test_the_ordinary_switch_path_is_unchanged_and_asks_nothing_twice(bench):
    # PD1 was never visited: its cuCIM switch takes the ordinary path, which
    # asks for raw at the switch.
    mark = bench.mark()
    swaps = bench.ctl.stats.get("atomic_channel_swaps", 0)
    bench.tab.show_source("PD1", "cucim", (SIGMA,))
    assert bench.ctl.stats.get("atomic_channel_swaps", 0) == swaps
    assert _until(bench.app, lambda: bench.raw_covers() and bench.precise_covers())
    _drain(bench.app, 300)

    mark = bench.mark()
    bench.tab.show_source("PD1", None, ())
    _drain(bench.app, 300)
    assert bench.raw_covers() and not bench.overview_shown()
    assert bench.raw_shown() == set(bench.ctl._visible_tiles)
    # Already pooled: no visible tile is asked for or read again.
    assert bench.raw_requests_since(mark) == []
    reads = bench.reads_since(mark)
    assert not [k for k in reads if k[0] == "PD1" and k[1] == bench.ctl.level]
    assert all(n == 1 for n in reads.values()), reads


def test_original_with_raw_already_resident_reads_nothing_again(bench):
    # The bench settled CD3/cuCIM through jump_to, which asks for raw too.
    assert bench.raw_covers()
    mark = bench.mark()
    bench.tab.show_source("CD3", None, ())
    _drain(bench.app, 300)
    assert bench.raw_covers() and not bench.overview_shown()
    assert bench.raw_requests_since(mark) == []
    assert not [k for k in bench.reads_since(mark)
                if k[0] == "CD3" and k[1] == bench.ctl.level]


def test_the_cached_path_reads_each_tile_at_most_once_through_a_zoom(bench):
    bench.switch_via_cached_swap("CD20")
    mark = bench.mark()
    bench.tab.show_source("CD20", None, ())
    assert _until(bench.app, bench.raw_covers, 10000)
    _zoom_in_out(bench)
    assert _until(bench.app, bench.raw_covers, 10000)
    _drain(bench.app, 300)
    assert not bench.overview_shown()
    reads = bench.reads_since(mark)
    # Each raw tile reaches the slide at most once: the raw cache and the
    # scheduler's single-flight serve every repeat.
    assert all(n == 1 for n in reads.values()), reads
