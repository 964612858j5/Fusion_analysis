"""Block B's exit gates: the ones that need the real thing to answer.

`docs/step1_rework_plan.md` B.6, the items B1-B3 could not close on their own,
and the review's own evidence rules (2026-09-17):

* the adapter reads a REAL `MainWindow`'s handoff fields, not a stand-in's;
* Intensity is checked on the pooled arrays and the item's own levels and
  lookup table, not by watching a setter;
* "seeded once" counts what the seed WORKER computed, not how many times it
  was asked -- the shared service de-duplicates;
* a manual window is held by Step0's and Step1's controllers alike, and
  survives the walk back;
* a jump is proved by the tiles that land in the pool, with their keys and
  their world rectangles, not by the camera rectangle alone;
* a dataset switch is proved by the old scheduler's workers being gone, its
  provider closed, its caches empty and its late results refused.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_viewer_binding import Step1ViewerBinding  # noqa: E402
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost,
)
from block01.viewer.tile_types import (  # noqa: E402
    RawKey, SourceIdentity, TileAddress, TileGridSpec,
)

SLIDE = 2048
ROI = (512, 1536, 512, 1536)
GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(ms=200):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)


class _RawPyramid:
    CHANNELS = ("DAPI", "CD3", "CD8")

    def __init__(self, path="/x/step1.ome.tif"):
        self.path = path
        self._shapes = {0: (SLIDE, SLIDE), 1: (SLIDE // 4, SLIDE // 4)}
        self.reads = []
        self.closed = False

    def source_identity(self):
        return SourceIdentity(dataset_path=self.path,
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

    def pixels(self, channel, level, y0, y1, x0, x1):
        offset = self.CHANNELS.index(channel) * 1000.0 + level * 10.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return yy + xx / 1000.0 + offset

    def read_region(self, channel, level, y0, y1, x0, x1):
        if self.closed:
            raise RuntimeError("provider closed")
        self.reads.append((channel, level, y0, y1, x0, x1))
        return self.pixels(channel, level, y0, y1, x0, x1), (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        values, _off = self.read_region(channel, tile.level, y0, y0 + size,
                                        x0, x0 + size)
        return values, 0.0

    def close(self):
        self.closed = True


def _tile(level, ty, tx):
    return TileAddress(grid=GRID, level=level, ty=ty, tx=tx)


class _Seeder:
    def __init__(self):
        self.requested = []

    def request_mapping_seed(self, channel, nucleus=False):
        self.requested.append(str(channel))
        return True


class _Window:
    def __init__(self, state, seeder, path="/x/step1.ome.tif"):
        self._display = SimpleNamespace(
            state=state, request_mapping_seed=seeder.request_mapping_seed)
        self.loader = SimpleNamespace(filepath=path)
        self._corrected_decisions = {}
        self._corrected_zarr_path = ""
        self._active_roi = {"name": "ROI_1", "bbox_fullres": list(ROI)}
        self.step0_output = {"channel_remap_config_hash": "rev-1"}


def _state(path="/x/step1.ome.tif"):
    state = ChannelDisplayState()
    state.bind(DatasetIdentity(path=path, fingerprint="1:1"))
    return state


def _make(app, window, channel="CD3"):
    raws = []

    def _factory(path, chan, table, parent):
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView
        from block01.viewer.scheduler import TileScheduler
        from block01.ui.step0.step0_explore_tab import ExploreStack

        raw = _RawPyramid(path)
        raws.append(raw)
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(16 * 1024 * 1024)
        corrected_cache = LRUByteCache(16 * 1024 * 1024)
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
    binding = Step1ViewerBinding(window, host=host)
    binding.open(channel)
    _pump(120)
    return binding, raws


def _close(binding):
    binding.close()
    _pump(80)


# ── 1. the adapter against a REAL MainWindow ──────────────────────────

def test_the_binding_reads_a_real_main_window_s_handoff(app, tmp_path):
    """Not a stand-in: the fields a real window publishes after a handoff."""
    from block01.ui.main_window import MainWindow

    window = MainWindow()
    window._schedule_step1_session_save = lambda: None
    window._save_step1_session = lambda *a, **k: None
    try:
        window.loader = SimpleNamespace(filepath="/x/real.ome.tif")
        window._corrected_decisions = {"CD3": "tophat", "CD8": "original"}
        window._corrected_zarr_path = str(tmp_path / "corrected.zarr")
        window._active_roi = {"name": "ROI_7", "bbox_fullres": [10, 90, 20, 80]}
        window.step0_output = {"channel_remap_config_hash": "hash-42"}
        window._set_step_active(1)
        state = window._display.state
        with state.using_scope("step1"):
            state.set_selected_channel("CD8", origin="test")
        # ...and the user then WALKS BACK to Step0, so the current scope is
        # not Step1's. An adapter that read "the current scope" would answer
        # DAPI here, which is the failure this gate is for.
        window._set_step_active(0)
        state.set_selected_channel("DAPI", origin="test")
        assert state.scope() == "step0"

        binding = Step1ViewerBinding(window, host=Step1ViewerHost())

        assert binding.dataset_path() == "/x/real.ome.tif"
        assert binding.decisions() == {"CD3": "tophat", "CD8": "original"}
        assert binding.corrected_zarr_path().endswith("corrected.zarr")
        assert binding.roi_name() == "ROI_7"
        assert binding.roi_bbox() == (10, 90, 20, 80)
        assert binding.handoff_revision() == "hash-42"
        assert binding.current_channel() == "CD8", \
            "the adapter read another step's channel"
    finally:
        window._display.shutdown("test")
        window.deleteLater()
        _pump(50)


# ── 2. Intensity on the real pool ─────────────────────────────────────

def test_the_mapping_reaches_the_item_s_levels_and_table(app):
    state = _state()
    window = _Window(state, _Seeder())
    state.set_mapping("CD3", 12.0, 240.0, 1.4, origin="user")
    binding, _raws = _make(app, window, channel="CD3")
    try:
        controller = binding.host.stack.controller
        assert controller.display_mapping[:2] == (12.0, 240.0)
        assert controller._raw_levels() == (12.0, 240.0), (
            "the pool would paint with numbers nobody set")

        state.set_mapping("CD3", 30.0, 300.0, 1.0, origin="user")
        _pump(60)
        assert controller.display_mapping[:2] == (30.0, 300.0)
        assert controller._raw_levels() == (30.0, 300.0)
    finally:
        _close(binding)


def test_every_pooled_tile_paints_with_the_same_window(app):
    """B.6 gate 3b: ONE set of numbers, on the items themselves.

    Tiles from two levels are put into the real pool and their own
    `ImageItem.levels` are read back -- a setter that was called proves
    nothing about what the painter will use.
    """
    state = _state()
    window = _Window(state, _Seeder())
    state.set_mapping("CD3", 5.0, 105.0, 1.0, origin="user")
    binding, _raws = _make(app, window, channel="CD3")
    try:
        controller = binding.host.stack.controller
        provider = binding.host.stack.provider
        for level, ty, tx in ((0, 2, 2), (1, 0, 0)):
            tile = _tile(level, ty, tx)
            values, _io = provider.read_tile("CD3", tile)
            pooled = controller._prepare_raw(values)
            ds_y, ds_x = controller._downsample_yx(level)
            from block01.viewer.explore_view import ExploreView
            rect = ExploreView.world_rect(ty * 512, tx * 512, pooled.shape[0],
                                          pooled.shape[1], ds_y, ds_x)
            controller._raw_pool.put(level, tx, ty, rect, pooled,
                                     RawKey(source=provider.source_identity(),
                                            channel="CD3", tile=tile))

        seen = {tuple(entry.item.levels)
                for entry in controller._raw_pool.entries.values()}
        assert seen == {(5.0, 105.0)}, seen

        # ...and a new window reaches every one of them.
        state.set_mapping("CD3", 9.0, 90.0, 1.0, origin="user")
        _pump(60)
        seen = {tuple(entry.item.levels)
                for entry in controller._raw_pool.entries.values()}
        assert seen == {(9.0, 90.0)}, seen
    finally:
        _close(binding)


def test_the_same_source_pixel_reads_the_same_after_a_jump(app):
    """B.6 gate 3a: the value does not depend on where the camera is."""
    state = _state()
    window = _Window(state, _Seeder())
    binding, raws = _make(app, window, channel="CD3")
    try:
        provider = binding.host.stack.provider
        first, _io = provider.read_tile("CD3", _tile(0, 2, 2))
        binding.show_patch((1100, 1200, 1100, 1200))
        _pump(60)
        again, _io = provider.read_tile("CD3", _tile(0, 2, 2))
        assert np.array_equal(first, again)
    finally:
        _close(binding)


# ── 3. seeded once, counted at the worker ─────────────────────────────

def test_a_window_is_computed_once_per_source(app):
    """Counted where the work happens: the seed worker's own tally."""
    from block01.workers.display_seed_worker import DisplaySeedWorker

    worker = DisplaySeedWorker()
    worker.start()
    try:
        binding = DatasetIdentity(path="/x/step1.ome.tif", fingerprint="1:1")
        array = np.linspace(0, 1000, 64 * 64, dtype=np.float32).reshape(64, 64)

        taken = [worker.submit(binding, "CD3", array) for _ in range(5)]
        deadline = time.monotonic() + 2.0
        while worker.stats()["computed"] < 1 and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)

        stats = worker.stats()
        assert taken[0] is True
        assert stats["computed"] == 1, stats
        assert stats["deduped"] >= 1, (
            "the repeated asks were not de-duplicated by the shared worker")
    finally:
        worker.stop() if hasattr(worker, "stop") else None
        _pump(50)


def test_a_stored_window_starts_no_pass_at_all(app):
    state = _state()
    seeder = _Seeder()
    state.set_mapping("CD3", 1.0, 2.0, 1.0, origin="user")
    window = _Window(state, seeder)
    binding, _raws = _make(app, window, channel="CD3")
    try:
        assert seeder.requested == [], (
            "a channel that already has a window was seeded anyway")
    finally:
        _close(binding)


# ── 4. a manual window, held by both steps ────────────────────────────

def test_a_manual_window_survives_the_walk_and_reaches_step1(app):
    """B.6 gate 5, as far as ONE test can carry it.

    What this proves: the numbers do not change between Step0's scope and
    Step1's, and Step1's controller holds them. It does NOT build a Step0
    controller -- that viewer's own following of the shared window is Step0's
    regression, and the two together are the gate.
    """
    state = _state()
    window = _Window(state, _Seeder())
    binding, _raws = _make(app, window, channel="CD3")
    try:
        with state.using_scope("step0"):
            state.set_mapping("CD3", 7.0, 77.0, 1.2, origin="step0-user")
        _pump(60)

        controller = binding.host.stack.controller
        assert controller.display_mapping[:2] == (7.0, 77.0), (
            "Step1's viewer did not follow the window set in Step0")

        with state.using_scope("step1"):
            assert state.mapping("CD3") == (7.0, 77.0, 1.2)
        with state.using_scope("step0"):
            assert state.mapping("CD3") == (7.0, 77.0, 1.2), (
                "the walk back changed the user's numbers")
    finally:
        _close(binding)


# ── 5. a jump lands real tiles ────────────────────────────────────────

def test_a_jump_puts_the_right_tiles_in_the_pool(app):
    """B.6 gate 8: the tiles, their keys and their world rectangles."""
    state = _state()
    window = _Window(state, _Seeder())
    binding, raws = _make(app, window, channel="CD3")
    try:
        controller = binding.host.stack.controller
        binding.show_patch((600, 900, 600, 900))

        def _covering():
            return {(e.level, e.tx, e.ty)
                    for e in controller._raw_pool.entries.values()
                    if e.level == controller.level}

        wanted_now = {(0, tx, ty)
                      for tx in range(600 // 512, 900 // 512 + 1)
                      for ty in range(600 // 512, 900 // 512 + 1)}
        deadline = time.monotonic() + 5.0
        # The level+1 underlay lands first; wait for the CURRENT level's own
        # tiles, which are what the user is looking at.
        while not wanted_now <= _covering() and time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)

        entries = list(controller._raw_pool.entries.values())
        assert entries, "the jump landed no tiles at all"
        # THE TILES THAT COVER THE RECTANGLE ASKED FOR, not just any tiles:
        # the initial whole-slide view lands tiles of its own, so a jump that
        # never reached the controller would still leave the pool non-empty.
        assert controller.level == 0, (
            f"the jump left the camera at level {controller.level}")
        assert wanted_now <= _covering(), (
            f"the jumped-to rectangle is not in the pool: {sorted(_covering())}")
        source = binding.host.stack.provider.source_identity()
        raw = raws[-1]
        for entry in entries:
            assert isinstance(entry.key, RawKey), (
                "a pooled tile carries no key to check")
            assert entry.key.channel == "CD3"
            assert entry.key.source == source
            assert entry.key.tile.level == entry.level
            assert (entry.key.tile.tx, entry.key.tile.ty) == (entry.tx,
                                                              entry.ty)
            # The world rectangle is this tile's own place on the slide.
            ds = raw.level_downsample(entry.level)
            assert entry.rect.x() == pytest.approx(entry.tx * 512 * ds)
            assert entry.rect.y() == pytest.approx(entry.ty * 512 * ds)
            assert entry.rect.width() > 0 and entry.rect.height() > 0
        # ...and the pixels are this channel's, at this level.
        level = entries[0].level
        expected = raw.pixels("CD3", level,
                              entries[0].ty * 512, entries[0].ty * 512 + 512,
                              entries[0].tx * 512, entries[0].tx * 512 + 512)
        stored = np.asarray(entries[0].item.image)
        inside = ~np.isnan(stored)
        assert inside.any(), "every pixel of the landed tile was absent"
        assert np.allclose(stored[inside], expected[inside])
    finally:
        _close(binding)


# ── 6. lifecycle across two datasets ──────────────────────────────────

def test_two_dataset_switches_leave_nothing_of_the_old_stacks(app):
    """B.6 gate 9: workers gone, provider closed, caches empty, results refused."""
    state = _state()
    window = _Window(state, _Seeder())
    binding, raws = _make(app, window, channel="CD3")
    try:
        first = binding.host.stack
        first_raw = raws[0]
        binding.show_patch((600, 900, 600, 900))
        _pump(120)

        window.loader.filepath = "/x/second.ome.tif"
        binding.open("CD3")
        _pump(120)
        second = binding.host.stack
        assert second is not first

        window.loader.filepath = "/x/third.ome.tif"
        binding.open("CD3")
        _pump(120)
        third = binding.host.stack
        assert third is not second

        for stack, raw in ((first, raws[0]), (second, raws[1])):
            assert raw.closed, "an old provider was left open"
            assert stack.scheduler._shutdown, "an old scheduler still runs"
            assert stack.torn_down is True
            # `ExploreStack.teardown` empties the caches and then drops the
            # tuple -- both halves matter, because the scheduler and the
            # compute layer hold their own references to the same objects.
            assert stack.caches is None, "the caches were not dropped"
            for thread in getattr(stack.scheduler, "_threads", ()) or ():
                assert not thread.is_alive(), "a worker thread outlived its stack"
        assert first_raw.closed

        # A late result from the first stack must not reach the live view.
        live = third.controller
        dropped = live.stats.get("mismatched_raw_dropped", 0)
        stale_key = RawKey(source=first.provider.source_identity(),
                           channel="CD3", tile=_tile(live.level, 1, 1))
        live._handle_raw_result(SimpleNamespace(
            error=None,
            pixels=SimpleNamespace(handle=np.zeros((512, 512), np.float32)),
            request=SimpleNamespace(key=stale_key,
                                    generation=live.view_generation)))
        assert live.stats.get("mismatched_raw_dropped", 0) == dropped + 1, \
            "a result from a torn-down stack was accepted"
    finally:
        _close(binding)


# ── 7. B4.1: the gates the first summary claimed too early ────────────

def _wait_for_tiles(controller, wanted, seconds=5.0):
    """Wait until the CURRENT level's tiles at `wanted` are in the pool."""
    def _have():
        return {(e.tx, e.ty) for e in controller._raw_pool.entries.values()
                if e.level == controller.level}

    deadline = time.monotonic() + seconds
    while not wanted <= _have() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    return _have()


def test_with_no_patch_the_region_s_own_tiles_reach_the_pool(app):
    """B.6 gate 7, with pixels: opening on the whole slide is not enough.

    The viewer must actually draw the region when nobody has drawn a patch.
    """
    state = _state()
    window = _Window(state, _Seeder())
    binding, raws = _make(app, window, channel="CD3")
    try:
        controller = binding.host.stack.controller
        # Inside the ROI, without ever asking for a patch: the camera is put
        # on the region's own middle the way any navigation would.
        binding.jump_to_point(1024, 1024, 512)
        have = _wait_for_tiles(controller, {(2, 2)})

        assert (2, 2) in have, f"the region's own tile never landed: {have}"
        entry = next(e for e in controller._raw_pool.entries.values()
                     if (e.tx, e.ty) == (2, 2) and e.level == controller.level)
        assert isinstance(entry.key, RawKey) and entry.key.channel == "CD3"
        ds = raws[-1].level_downsample(entry.level)
        assert entry.rect.x() == pytest.approx(entry.tx * 512 * ds)
        stored = np.asarray(entry.item.image)
        expected = raws[-1].pixels("CD3", entry.level,
                                   entry.ty * 512, entry.ty * 512 + 512,
                                   entry.tx * 512, entry.tx * 512 + 512)
        inside = ~np.isnan(stored)
        assert inside.any()
        assert np.allclose(stored[inside], expected[inside])
    finally:
        _close(binding)


def test_a_tissue_preview_click_lands_its_own_tiles(app):
    """B.6 gate 8 for the OTHER gesture: a point jump, proved by pixels."""
    state = _state()
    window = _Window(state, _Seeder())
    binding, raws = _make(app, window, channel="CD3")
    try:
        controller = binding.host.stack.controller
        binding.jump_to_point(1300, 1300, 256)     # inside the ROI
        have = _wait_for_tiles(controller, {(2, 2)})

        assert (2, 2) in have, f"the click landed no tiles: {have}"
        entry = next(e for e in controller._raw_pool.entries.values()
                     if (e.tx, e.ty) == (2, 2) and e.level == controller.level)
        stored = np.asarray(entry.item.image)
        expected = raws[-1].pixels("CD3", entry.level,
                                   entry.ty * 512, entry.ty * 512 + 512,
                                   entry.tx * 512, entry.tx * 512 + 512)
        inside = ~np.isnan(stored)
        assert inside.any()
        assert np.allclose(stored[inside], expected[inside])
    finally:
        _close(binding)


def test_outside_the_region_the_viewer_is_empty_and_says_so(app):
    """B.6 gate 8's other half: no pixels, no error, and a word about it."""
    from block01.ui.step1_viewer_host import STATUS_OK, STATUS_OUTSIDE_ROI

    state = _state()
    window = _Window(state, _Seeder())
    binding, raws = _make(app, window, channel="CD3")
    try:
        host = binding.host
        said = []
        host.status_changed.connect(said.append)

        binding.jump_to_point(1024, 1024, 256)      # inside
        assert host.status == STATUS_OK

        binding.jump_to_point(100, 100, 256)        # outside the ROI
        _pump(120)

        assert host.status == STATUS_OUTSIDE_ROI, host.status
        assert said and said[-1] == STATUS_OUTSIDE_ROI
        assert "no pixels" in host.status

        # ...and what is drawn there carries no pixels at all.
        values, _io = host.stack.provider.read_tile("CD3", _tile(0, 0, 0))
        assert np.isnan(values).all()
        for entry in host.stack.controller._raw_pool.entries.values():
            if entry.level == host.stack.controller.level and \
                    (entry.tx, entry.ty) == (0, 0):
                assert np.isnan(np.asarray(entry.item.image)).all()

        binding.jump_to_point(1024, 1024, 256)      # back inside
        assert host.status == STATUS_OK
        assert said[-1] == STATUS_OK
    finally:
        _close(binding)


def test_gamma_reaches_every_pooled_tile_and_the_ones_that_follow(app):
    """B.6 gate 3b, the half the first version missed: the LUT.

    A window is more than Min/Max -- gamma is carried by the lookup table,
    and a tile that lands after the change must be painted the same way as
    the ones already pooled.
    """
    state = _state()
    window = _Window(state, _Seeder())
    state.set_mapping("CD3", 5.0, 105.0, 2.2, origin="user")
    binding, _raws = _make(app, window, channel="CD3")
    try:
        controller = binding.host.stack.controller
        provider = binding.host.stack.provider

        def _pool(level, ty, tx):
            from block01.viewer.explore_view import ExploreView
            tile = _tile(level, ty, tx)
            values, _io = provider.read_tile("CD3", tile)
            pooled = controller._prepare_raw(values)
            ds_y, ds_x = controller._downsample_yx(level)
            rect = ExploreView.world_rect(ty * 512, tx * 512, pooled.shape[0],
                                          pooled.shape[1], ds_y, ds_x)
            controller._raw_pool.put(level, tx, ty, rect, pooled,
                                     RawKey(source=provider.source_identity(),
                                            channel="CD3", tile=tile))

        _pool(0, 2, 2)
        _pool(1, 0, 0)

        def _luts():
            out = []
            for entry in controller._raw_pool.entries.values():
                lut = entry.item.lut
                out.append(None if lut is None else np.asarray(lut).tobytes())
            return set(out)

        first = _luts()
        assert len(first) == 1 and None not in first, (
            "the pooled tiles do not share one lookup table")

        # A different gamma, same Min/Max: the table must move.
        state.set_mapping("CD3", 5.0, 105.0, 1.0, origin="user")
        _pump(80)
        second = _luts()
        assert len(second) == 1
        assert second != first, "gamma did not reach the pooled tiles"

        # ...and a tile that lands afterwards inherits it.
        _pool(0, 2, 1)
        assert _luts() == second, "a late tile was painted with the old table"
    finally:
        _close(binding)
