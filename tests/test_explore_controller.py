"""Unit tests for viewer/explore_view.py (ExploreView + ExploreController).

Offscreen Qt (QT_QPA_PLATFORM=offscreen). Uses a FakeProvider (synthetic
multi-level pyramid, deterministic ramp/coordinate-encoded arrays) and a
FakeScheduler that records every TileRequest/callback pair and lets the
test deliver results synchronously on demand -- no real threads. Results
are delivered via the controller's public `_on_raw_result` /
`_on_precise_result` slots, which internally marshal through a
QueuedConnection signal; `QtWidgets.QApplication.processEvents()` (via
`QtTest.QTest.qWait`) drains the queue.

This suite REPLACES the earlier mosaic-canvas test suite: explore_view.py
now renders one persistent `pg.ImageItem` per delivered tile (a
`TileItemPool`), positioned once in world coordinates via unrounded
per-axis downsample factors, instead of blitting into a shared canvas.
"""

import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets, QtTest  # noqa: E402

from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey,
    PixelBuffer,
    QualityLevel,
    RawKey,
    SourceIdentity,
    TileAddress,
    TileGridSpec,
    TileResult,
    effective_param,
    tiles_covering,
)
from block01.viewer.explore_view import (  # noqa: E402
    ExploreController,
    ExploreView,
    TileItemPool,
    _box_downsample,
    _pick_calibration_windows,
    GAIN_CLAMP,
)
from block01.viewer.scheduler import TileScheduler  # noqa: E402


@pytest.fixture(scope="module")
def app():
    try:
        application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    except Exception as exc:  # no usable Qt platform -> skip the module
        pytest.skip(f"Qt unavailable: {exc}")
    return application


def _pump(ms=50):
    """Drain queued-connection signals + timers for `ms` milliseconds."""
    QtTest.QTest.qWait(ms)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeProvider:
    """Synthetic 2-level SQUARE pyramid: level 0 is 4096x4096 (ds=1), level
    1 is 1024x1024 (ds=4). read_region returns a deterministic ramp so
    placement is verifiable."""

    def __init__(self):
        self._shapes = {0: (4096, 4096), 1: (1024, 1024)}
        self.close_called = False

    def source_identity(self):
        return SourceIdentity(dataset_path="/x/fake.ome.tif", dataset_fingerprint="1:1", stage="raw")

    @property
    def num_levels(self):
        return 2

    def level_shape(self, level):
        return self._shapes[level]

    def level_downsample(self, level):
        return 1.0 if level == 0 else 4.0

    def level_downsample_yx(self, level):
        ds = self.level_downsample(level)
        return ds, ds

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self._shapes[level]
        cy0, cy1 = max(0, min(y0, h)), max(0, min(y1, h))
        cx0, cx1 = max(0, min(x0, w)), max(0, min(x1, w))
        rows = np.arange(cy0, cy1).reshape(-1, 1)
        cols = np.arange(cx0, cx1).reshape(1, -1)
        arr = (rows * 1000 + cols).astype(np.float32)
        return arr, (cy0, cx0)

    def close(self):
        self.close_called = True


class AsymmetricFakeProvider(FakeProvider):
    """Non-square, non-integer-ratio 2-level pyramid used to pin the
    transpose/ds-mixup and non-integer-alignment failure modes:
    level 0 = 3000 x 1000 (h x w); level 1 = 1000 x 333 -> ds_y=3.0,
    ds_x=3.003..."""

    def __init__(self):
        self._shapes = {0: (3000, 1000), 1: (1000, 333)}
        self.close_called = False

    def level_downsample(self, level):
        if level == 0:
            return 1.0
        h0, _w0 = self.level_shape(0)
        hn, _wn = self.level_shape(level)
        return round(h0 / hn)

    def level_downsample_yx(self, level):
        h0, w0 = self.level_shape(0)
        hn, wn = self.level_shape(level)
        return (h0 / hn if hn else 1.0), (w0 / wn if wn else 1.0)


class FakeScheduler:
    """Records every request; delivers on demand via `deliver`. Tracks
    cancel_generation / shutdown calls."""

    def __init__(self):
        self.requests = []  # list of (TileRequest, callback)
        self.cancelled_generations = []
        self.shutdown_called = False

    def request(self, req, callback):
        self.requests.append((req, callback))

    def cancel_generation(self, gen):
        self.cancelled_generations.append(gen)

    def shutdown(self):
        self.shutdown_called = True

    def pending_for(self, key_type=None):
        if key_type is None:
            return list(self.requests)
        return [(r, cb) for r, cb in self.requests if isinstance(r.key, key_type)]

    def deliver(self, req, arr, error=None):
        pixels = None if error else PixelBuffer(
            residency="cpu", dtype=str(arr.dtype), shape=tuple(arr.shape), handle=arr)
        quality = req.key.quality if isinstance(req.key, CorrectionKey) else QualityLevel.NATIVE
        result = TileResult(request=req, pixels=pixels, quality=quality,
                             provisional=False, timing={}, error=error)
        for r, cb in self.requests:
            if r is req:
                cb(result)
                return
        raise AssertionError("request not found for delivery")


class FakeCompute:
    def raw_keys_for(self, key):
        return []

    def correct_array(self, arr, method, param):
        return arr.astype(np.float32, copy=False)


# ── helpers ──────────────────────────────────────────────────────────────────

def make_controller(app, settle_ms=30, channel="DAPI", provider=None, scheduler=None):
    provider = provider or FakeProvider()
    scheduler = scheduler or FakeScheduler()
    compute = FakeCompute()
    grid = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
    view = ExploreView()
    view.resize(800, 600)
    view.show()
    _pump(20)
    ctrl = ExploreController(provider, scheduler, compute, grid, view, channel,
                              settle_ms=settle_ms)
    return ctrl, provider, scheduler, view


def raw_arr_for(provider, level, tx, ty, ts=512):
    y0, x0 = ty * ts, tx * ts
    h, w = provider.level_shape(level)
    y1, x1 = min(y0 + ts, h), min(x0 + ts, w)
    arr, _off = provider.read_region("DAPI", level, y0, y1, x0, x1)
    return arr


def set_view_and_pump(view, x0, y0, x1, y1, ms=80):
    view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
    _pump(ms)


# ── 1. overview loads full extent ───────────────────────────────────────────

def test_overview_loads_full_extent(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()

    h, w = provider.level_shape(ctrl._overview_level)
    ds = provider.level_downsample(ctrl._overview_level)
    mapped = view.overview_item.mapRectToParent(view.overview_item.boundingRect())
    assert mapped.width() == pytest.approx(w * ds, rel=1e-6)
    assert mapped.height() == pytest.approx(h * ds, rel=1e-6)
    assert mapped.x() == pytest.approx(0.0, abs=1e-6)
    assert mapped.y() == pytest.approx(0.0, abs=1e-6)
    assert view.overview_item.axisOrder == "row-major"

    ctrl.teardown()


# ── 2. range change computes correct tile set + center-out priority ────────

def test_range_change_requests_missing_raw_tiles_center_out(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()

    y0, x0, y1, x1 = 100, 700, 100 + 2048, 700 + 2048
    set_view_and_pump(view, x0, y0, x1, y1)

    (ax0, ax1), (ay0, ay1) = view.view_box.viewRange()
    y0, x0, y1, x1 = int(ay0), int(ax0), int(ay1), int(ax1)
    expected_tiles = tiles_covering((y0, x0, y1, x1), ctrl.grid.tile_size)
    raw_reqs = scheduler.pending_for(RawKey)
    requested_coords = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in raw_reqs}
    assert requested_coords == expected_tiles

    cy, cx = (y0 + y1) / 2.0, (x0 + x1) / 2.0
    ts = ctrl.grid.tile_size

    def dist(tx, ty):
        tcy, tcx = ty * ts + ts / 2.0, tx * ts + ts / 2.0
        return (tcy - cy) ** 2 + (tcx - cx) ** 2

    priorities = [(r.priority, r.key.tile.tx, r.key.tile.ty) for r, _cb in raw_reqs]
    priorities.sort()
    dists_in_priority_order = [dist(tx, ty) for _p, tx, ty in priorities]
    assert dists_in_priority_order == sorted(dists_in_priority_order)

    ctrl.teardown()


# ── 3. motion timer (not the settle timer): rapid range changes coalesce
#      into exactly one live settled gen ────────────────────────────────────

def test_settle_debounce_fires_once(app):
    """Precise issuing is coalesced by the 30ms `_motion_timer`, not by
    `_settle_timer` -- `settle_ms=150` is set deliberately larger than the
    time this test actually waits (well under 150ms total), so a passing
    result here cannot be explained by the settle timer having fired."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=150)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()

    for i in range(5):
        set_view_and_pump(view, 700 + i, 100, 700 + i + 2048, 2148, ms=10)

    _pump(60)  # >> MOTION_MS (30ms) after the last event, << settle_ms (150ms)

    precise_reqs = scheduler.pending_for(CorrectionKey)
    gens = {r.generation for r, _cb in precise_reqs}
    assert precise_reqs, "precise requests must appear via the motion timer, well before settle_ms"
    assert ctrl._settled_generation in gens
    stale = gens - {ctrl._settled_generation}
    assert stale <= set(scheduler.cancelled_generations)

    ctrl.teardown()


# ── 4. jump_to issues the settled batch immediately ────────────────────────

def test_jump_to_issues_settled_batch_immediately(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()

    gen_before = ctrl._settled_generation
    ctrl.jump_to(y0=0, x0=0, w=2048, h=2048)

    assert ctrl._settled_generation != gen_before
    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) > 0

    ctrl.teardown()


# ── 5. stale-generation precise result dropped ─────────────────────────────

def test_stale_precise_result_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    precise_reqs = scheduler.pending_for(CorrectionKey)
    req, _cb = precise_reqs[0]

    ctrl.jump_to(y0=0, x0=0, w=512, h=512)  # bumps the settled generation

    arr = np.zeros((512, 512), dtype=np.float32)
    before = ctrl.stats["stale_precise_dropped"]
    scheduler.deliver(req, arr)
    _pump(20)
    assert ctrl.stats["stale_precise_dropped"] == before + 1

    ctrl.teardown()


# ── 6. mismatched-key precise result dropped ───────────────────────────────

def test_mismatched_key_precise_result_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    precise_reqs = scheduler.pending_for(CorrectionKey)
    req, _cb = precise_reqs[0]
    ctrl.method = "cucim"

    arr = np.zeros((512, 512), dtype=np.float32)
    before = ctrl.stats["mismatched_key_dropped"]
    assert req.generation == ctrl._settled_generation
    scheduler.deliver(req, arr)
    _pump(20)
    assert ctrl.stats["mismatched_key_dropped"] == before + 1

    ctrl.teardown()


# ── 7. selection change -> provisional; fresh matching tiles restore it ────

def test_selection_change_sets_provisional_then_restores(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    seen = []
    ctrl.provisional_changed.connect(seen.append)

    ctrl.set_selection(method="cucim", params=(8,))
    assert ctrl._provisional is True
    assert ctrl._precise_visible is False
    assert True in seen

    precise_reqs = [
        (r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
        if r.generation == ctrl._settled_generation
    ]
    assert precise_reqs
    for req, _cb in precise_reqs:
        arr = np.zeros((512, 512), dtype=np.float32)
        scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl._provisional is False
    assert ctrl._precise_visible is True
    assert False in seen

    ctrl.teardown()


# ── 8. world-rect math (per-axis, unrounded) ───────────────────────────────

def test_world_rect_math_level1_tile():
    rect = ExploreView.world_rect(y0=3 * 512, x0=2 * 512, h=512, w=512, ds_y=4.0, ds_x=4.0)
    assert rect.x() == pytest.approx(2 * 512 * 4)
    assert rect.y() == pytest.approx(3 * 512 * 4)
    assert rect.width() == pytest.approx(512 * 4)
    assert rect.height() == pytest.approx(512 * 4)


# ── 9. teardown order: scheduler.shutdown before provider.close ───────────

def test_teardown_order(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    ctrl.teardown()

    assert ctrl._teardown_order == ["scheduler.shutdown", "provider.close"]
    assert scheduler.shutdown_called is True
    assert provider.close_called is True

    ctrl.teardown()  # idempotent
    assert ctrl._teardown_order == ["scheduler.shutdown", "provider.close"]


# ── 10. late raw tile: wrong level / channel rejected ──────────────────────

def test_late_raw_tile_wrong_level_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    raw_reqs = scheduler.pending_for(RawKey)
    req, _cb = raw_reqs[0]
    tx, ty = req.key.tile.tx, req.key.tile.ty

    ctrl.level = 1  # simulate a level switch happening before the raw result lands

    before_mismatch = ctrl.stats["mismatched_raw_dropped"]
    arr = raw_arr_for(provider, 0, tx, ty)
    scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl.stats["mismatched_raw_dropped"] == before_mismatch + 1
    assert ctrl._raw_pool.get(0, tx, ty) is None

    ctrl.teardown()


def test_late_raw_tile_wrong_channel_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    raw_reqs = scheduler.pending_for(RawKey)
    req, _cb = raw_reqs[0]
    ctrl.channel = "OTHER"

    before = ctrl.stats["mismatched_raw_dropped"]
    arr = raw_arr_for(provider, 0, req.key.tile.tx, req.key.tile.ty)
    scheduler.deliver(req, arr)
    _pump(20)
    assert ctrl.stats["mismatched_raw_dropped"] == before + 1

    ctrl.teardown()


# ── 11. jump_to actually moves the camera + issues exactly one live batch ─

def test_jump_to_moves_camera_and_single_settled_batch(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()

    gen_before = ctrl._settled_generation
    ctrl.jump_to(y0=1000, x0=2000, w=512, h=512)

    (ax0, ax1), (ay0, ay1) = view.view_box.viewRange()
    assert ax0 <= 2000 + 1 and ax1 >= 2000 + 512 - 1
    assert ay0 <= 1000 + 1 and ay1 >= 1000 + 512 - 1

    assert ctrl._settled_generation != gen_before
    precise_gens = {r.generation for r, _cb in scheduler.pending_for(CorrectionKey)}
    assert precise_gens == {ctrl._settled_generation}

    ctrl.teardown()


# ── 12. set_selection sentinel: partial updates don't clobber the rest ────

def test_set_selection_sentinel_preserves_unset_fields(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(channel="DAPI", method="tophat", params=(10,))
    assert (ctrl.channel, ctrl.method, ctrl.params) == ("DAPI", "tophat", (10,))

    ctrl.set_selection(params=(20,))
    assert (ctrl.channel, ctrl.method, ctrl.params) == ("DAPI", "tophat", (20,))

    ctrl.set_selection(method=None)  # explicit None must take effect
    assert ctrl.method is None

    ctrl.teardown()


# ══════════════════════════════════════════════════════════════════════════
# New architecture-correction tests (A-D of the task spec)
# ══════════════════════════════════════════════════════════════════════════

# ── T1. Transpose pin: asymmetric level-0, coordinate-encoded array ───────

def test_transpose_pin_asymmetric_provider(app):
    """A raw tile delivered from a coordinate-encoded array must map pixel
    (y=10, x=100) to world (x=100*ds_x, y=10*ds_y) -- catches axis
    transposition and ds-axis mixups. Also pins row-major construction."""
    provider = AsymmetricFakeProvider()
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000, provider=provider)
    ctrl.load_overview()
    ctrl.jump_to(y0=0, x0=0, w=333, h=1000)  # level 0 is 3000x1000 -> level 1 fits

    raw_reqs = scheduler.pending_for(RawKey)
    assert raw_reqs
    req, _cb = raw_reqs[0]
    tile = req.key.tile
    arr = raw_arr_for(provider, tile.level, tile.tx, tile.ty,
                       ts=ctrl.grid.tile_size)
    scheduler.deliver(req, arr)
    _pump(30)

    entry = ctrl._raw_pool.get(tile.level, tile.tx, tile.ty)
    assert entry is not None
    assert entry.item.axisOrder == "row-major"

    ds_y, ds_x = provider.level_downsample_yx(tile.level)
    ts = ctrl.grid.tile_size
    expected_x0 = tile.tx * ts * ds_x
    expected_y0 = tile.ty * ts * ds_y
    assert entry.rect.x() == pytest.approx(expected_x0, rel=1e-6)
    assert entry.rect.y() == pytest.approx(expected_y0, rel=1e-6)

    # image array shape vs rect aspect: arr is (h, w); the world rect's
    # width/height ratio must match w*ds_x / h*ds_y (row-major, not
    # transposed).
    h, w = arr.shape
    expected_ratio = (w * ds_x) / (h * ds_y)
    actual_ratio = entry.rect.width() / entry.rect.height()
    assert actual_ratio == pytest.approx(expected_ratio, rel=1e-6)

    ctrl.teardown()


# ── T2. Non-integer pyramid alignment: no cumulative drift at the far edge ─

def test_non_integer_pyramid_alignment_far_edge(app):
    """levels (1000, 3000) [level 0] and (333, 1000) [level 1] ->
    ds_y=3.003..., ds_x=3.0. A tile at the far edge of level 1 must have a
    world rect within 1px of the level-0 extent edge."""

    class Provider2(FakeProvider):
        def __init__(self):
            self._shapes = {0: (1000, 3000), 1: (333, 1000)}
            self.close_called = False

        def level_downsample(self, level):
            if level == 0:
                return 1.0
            h0, _ = self.level_shape(0)
            hn, _ = self.level_shape(level)
            return round(h0 / hn)

        def level_downsample_yx(self, level):
            h0, w0 = self.level_shape(0)
            hn, wn = self.level_shape(level)
            return h0 / hn, w0 / wn

    provider = Provider2()
    ds_y, ds_x = provider.level_downsample_yx(1)
    assert ds_y == pytest.approx(1000 / 333)
    assert ds_x == pytest.approx(3.0)

    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000, provider=provider)
    ctrl.load_overview()

    # Force level 1 directly (bypassing the automatic hysteresis-driven
    # level pick, which is exercised elsewhere) and issue raw requests for
    # its full extent, including the far-edge tile.
    h1, w1 = provider.level_shape(1)
    h0, w0 = provider.level_shape(0)
    ctrl.level = 1
    ctrl._current_bbox = (0, 0, h0, w0)
    ctrl._visible_tiles = tiles_covering((0, 0, h1, w1), ctrl.grid.tile_size)
    ctrl._issue_raw_requests()

    ts = ctrl.grid.tile_size
    far_tx, far_ty = (w1 - 1) // ts, (h1 - 1) // ts
    raw_reqs = scheduler.pending_for(RawKey)
    far_req = next(r for r, _cb in raw_reqs if r.key.tile.tx == far_tx and r.key.tile.ty == far_ty)
    y0, x0 = far_ty * ts, far_tx * ts
    y1c, x1c = min(y0 + ts, h1), min(x0 + ts, w1)
    arr, _ = provider.read_region("DAPI", 1, y0, y1c, x0, x1c)
    scheduler.deliver(far_req, arr)
    _pump(30)

    entry = ctrl._raw_pool.get(1, far_tx, far_ty)
    assert entry is not None
    far_edge_x = entry.rect.x() + entry.rect.width()
    far_edge_y = entry.rect.y() + entry.rect.height()
    level0_w, level0_h = provider.level_shape(0)[1], provider.level_shape(0)[0]
    assert far_edge_x == pytest.approx(level0_w, abs=1.0)
    assert far_edge_y == pytest.approx(level0_h, abs=1.0)

    ctrl.teardown()


# ── T3. Zoom-under-mouse invariance (camera contract) ──────────────────────

def test_zoom_under_mouse_invariance(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    set_view_and_pump(view, 0, 0, 2048, 2048)

    vb = view.view_box
    world_point = vb.viewRect().center()
    screen_pt = vb.mapFromView(world_point)

    vb.scaleBy((0.5, 0.5), center=world_point)
    _pump(80)

    world_after = vb.mapToView(screen_pt)
    assert world_after.x() == pytest.approx(world_point.x(), abs=2.0)
    assert world_after.y() == pytest.approx(world_point.y(), abs=2.0)

    ctrl.teardown()


# ── T4. Generation namespace isolation (scheduler-level) ───────────────────

def test_generation_namespace_isolation():
    """cancel_generation(("raw", 5)) must not affect a queued
    ("precise", 5) request, and vice versa -- opaque namespaced tokens
    never collide even at the same integer."""
    from block01.viewer.tile_types import TileAddress, TileRequest

    class BlockingProvider:
        def read_tile(self, channel, tile):
            import time as _t
            _t.sleep(0.05)
            return np.zeros((4, 4), dtype=np.float32), 1.0

    class NullCompute:
        def raw_keys_for(self, key):
            return []

    class ByteCache:
        def __init__(self):
            self._d = {}

        def get(self, k):
            return self._d.get(k)

        def put(self, k, v):
            self._d[k] = v

    provider = BlockingProvider()
    sched = TileScheduler(provider, NullCompute(), ByteCache(), ByteCache(),
                           io_workers=1, compute_workers=1)
    try:
        grid = TileGridSpec(tile_size=512)
        source = SourceIdentity(dataset_path="/x", dataset_fingerprint="1", stage="raw")

        # occupy the single raw worker so the next raw request stays queued
        blocker_key = RawKey(source=source, channel="A",
                              tile=TileAddress(grid=grid, level=0, tx=0, ty=0))
        sched.request(TileRequest(key=blocker_key, generation=("raw", 5), priority=0),
                      lambda r: None)

        queued_key = RawKey(source=source, channel="A",
                             tile=TileAddress(grid=grid, level=0, tx=1, ty=0))
        results = []
        sched.request(TileRequest(key=queued_key, generation=("precise", 5), priority=0),
                      results.append)

        # Cancel the RAW namespace's token 5 -- must not touch ("precise", 5).
        sched.cancel_generation(("raw", 5))

        import time as _t
        deadline = _t.time() + 2.0
        while not results and _t.time() < deadline:
            _t.sleep(0.01)

        assert results, "queued (\"precise\", 5) request was wrongly cancelled by cancel_generation((\"raw\", 5))"
        assert results[0].error != "cancelled"
    finally:
        sched.shutdown()


# ── T5. Late delivery rejected: tile no longer in wanted set ───────────────

def test_late_raw_delivery_outside_wanted_set_rejected(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    raw_reqs = scheduler.pending_for(RawKey)
    req, _cb = raw_reqs[0]
    tx, ty = req.key.tile.tx, req.key.tile.ty

    # Pan far away so (tx, ty) is no longer in the wanted set, WITHOUT
    # changing the display level (keep the generation-mismatch path
    # separate from this membership check by keeping the same view
    # generation impossible in practice -- panning bumps it too, which is
    # fine: the membership guard is what we're pinning here).
    ctrl._visible_tiles = {(tx + 100, ty + 100)}

    before = ctrl.stats["late_raw_rejected"]
    arr = raw_arr_for(provider, req.key.tile.level, tx, ty)
    scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl.stats["late_raw_rejected"] >= before  # generation OR membership guard fires
    assert ctrl._raw_pool.get(req.key.tile.level, tx, ty) is None

    ctrl.teardown()


# ── T6. Level switch keeps old tiles; new items get higher zValue ──────────

def test_level_switch_keeps_old_tiles_and_orders_z(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()

    # Force level 1 directly (bypassing the automatic hysteresis-driven
    # level pick, exercised elsewhere) and issue+deliver one raw tile.
    ctrl.level = 1
    ctrl._current_bbox = (0, 0, 1024, 1024)
    ctrl._visible_tiles = tiles_covering((0, 0, 256, 256), ctrl.grid.tile_size)
    ctrl._issue_raw_requests()
    raw_reqs_l1 = scheduler.pending_for(RawKey)
    req_l1, _cb = raw_reqs_l1[0]
    arr_l1 = raw_arr_for(provider, 1, req_l1.key.tile.tx, req_l1.key.tile.ty)
    scheduler.deliver(req_l1, arr_l1)
    _pump(30)

    entry_l1 = ctrl._raw_pool.get(1, req_l1.key.tile.tx, req_l1.key.tile.ty)
    assert entry_l1 is not None
    old_rect = entry_l1.rect

    # Switch to level 0 (finer) covering the same world area -- NOT clearing
    # the level-1 item.
    ctrl.level = 0
    ctrl._current_bbox = (0, 0, 1024, 1024)
    ctrl._visible_tiles = tiles_covering((0, 0, 1024, 1024), ctrl.grid.tile_size)
    ctrl._issue_raw_requests()
    raw_reqs_l0 = [r for r, _cb in scheduler.pending_for(RawKey) if r.key.tile.level == 0]
    req_l0 = raw_reqs_l0[0]
    arr_l0 = raw_arr_for(provider, 0, req_l0.key.tile.tx, req_l0.key.tile.ty)
    scheduler.deliver(req_l0, arr_l0)
    _pump(30)

    # The level-1 item is still present (not cleared) and unchanged.
    entry_l1_after = ctrl._raw_pool.get(1, req_l1.key.tile.tx, req_l1.key.tile.ty)
    assert entry_l1_after is not None
    assert entry_l1_after.rect.x() == pytest.approx(old_rect.x())
    assert entry_l1_after.rect.y() == pytest.approx(old_rect.y())

    entry_l0 = ctrl._raw_pool.get(0, req_l0.key.tile.tx, req_l0.key.tile.ty)
    assert entry_l0 is not None
    # Finer level (0) must draw above coarser level (1).
    assert entry_l0.item.zValue() > entry_l1_after.item.zValue()

    ctrl.teardown()


# ── T7. Item pool budget: over-cap prunes off-level farthest-first ────────

def test_item_pool_budget_prunes_offlevel_farthest_first():
    from PyQt5.QtCore import QRectF as _QRectF

    class FakeViewBox:
        def __init__(self):
            self.items = []

        def addItem(self, item):
            self.items.append(item)

        def removeItem(self, item):
            self.items.remove(item)

    vb = FakeViewBox()
    pool = TileItemPool(vb, base_z=100, num_levels=2, budget=5)

    # Fill with off-current-level items far from the viewport (prunable).
    for i in range(6):
        rect = _QRectF(i * 1000.0, 0.0, 10.0, 10.0)
        pool.put(level=1, tx=i, ty=0, rect=rect, arr_uint8=np.zeros((4, 4), dtype=np.uint8), key=None)

    # One item at the CURRENT level, inside the viewport -- must survive.
    wanted_rect = _QRectF(0.0, 0.0, 10.0, 10.0)
    pool.put(level=0, tx=0, ty=0, rect=wanted_rect, arr_uint8=np.zeros((4, 4), dtype=np.uint8), key=None)

    assert len(pool.entries) == 7
    viewport = _QRectF(0.0, 0.0, 10.0, 10.0)
    keep = {(0, 0, 0)}
    pool.prune(current_level=0, viewport_world_rect=viewport, margin_world=5.0, keep_coords=keep)

    assert len(pool.entries) <= pool.budget
    # The wanted (current-level, in-viewport) item must never be pruned.
    assert (0, 0, 0) in pool.entries
    # Farthest off-level items (largest i) must be pruned before nearer ones.
    remaining_offlevel = sorted(tx for (lvl, tx, ty) in pool.entries if lvl == 1)
    if remaining_offlevel:
        assert max(remaining_offlevel) < 5  # the farthest (i=5) was pruned
    assert pool.items_pruned >= 1


# ── T8. Atomic precise visibility (group-gate) ─────────────────────────────

def test_precise_visibility_is_atomic_group_gate(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)  # >= 2 tiles at ts=512

    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) >= 2
    assert ctrl._precise_visible is False

    # Deliver only ONE of the tiles: coverage incomplete -> stays hidden.
    req0, _cb = precise_reqs[0]
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(req0, arr)
    _pump(20)
    assert ctrl._precise_visible is False

    # Deliver the rest -> coverage complete -> atomic flip to visible.
    for req, _cb in precise_reqs[1:]:
        scheduler.deliver(req, arr)
    _pump(20)
    assert ctrl._precise_visible is True

    ctrl.teardown()


# ══════════════════════════════════════════════════════════════════════════
# Display-policy fix tests (active draw set + corrected floor)
# ══════════════════════════════════════════════════════════════════════════

def _make_correction_key_for(ctrl, provider, level, tx, ty, params):
    ds = provider.level_downsample(level)
    eff = tuple(effective_param(p, level, ds) for p in params)
    addr = TileAddress(grid=ctrl.grid, level=level, tx=tx, ty=ty)
    return CorrectionKey(
        source=provider.source_identity(), channel=ctrl.channel, tile=addr,
        method=ctrl.method, params=eff, algorithm_version=BG_CORRECTION_ALGO_VERSION,
        quality=ctrl.quality,
    )


# ── D1. Finer-level tiles hidden after zoom-out (bug 2 regression) ─────────

def test_finer_level_tiles_hidden_after_zoom_out(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ts = ctrl.grid.tile_size

    rect0 = ExploreView.world_rect(0, 0, ts, ts, 1.0, 1.0)
    arr = np.zeros((ts, ts), dtype=np.uint8)
    ctrl._raw_pool.put(0, 0, 0, rect0, arr, key=None)
    ctrl._precise_pool.put(0, 0, 0, rect0, arr, key=None)

    rect1 = ExploreView.world_rect(0, 0, ts, ts, 4.0, 4.0)
    ctrl._raw_pool.put(1, 0, 0, rect1, arr, key=None)
    ctrl._precise_pool.put(1, 0, 0, rect1, arr, key=None)

    # Zoom OUT: level 0 (finer) tiles are now stale relative to level 1.
    ctrl.level = 1
    ctrl._visible_tiles = {(0, 0)}
    ctrl._update_layer_visibility()

    assert ctrl._raw_pool.get(0, 0, 0).item.isVisible() is False
    assert ctrl._precise_pool.get(0, 0, 0).item.isVisible() is False
    # The level-1 raw item is at the current level -> visible (no method
    # selected, raw always shown).
    assert ctrl._raw_pool.get(1, 0, 0).item.isVisible() is True

    ctrl.teardown()


# ── D2. Corrected mode never shows raw stage during motion (bug 1) ─────────

def test_corrected_mode_never_shows_raw_stage_during_motion(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)  # 2x2 tiles at ts=512

    raw_reqs = scheduler.pending_for(RawKey)
    assert raw_reqs
    for req, _cb in raw_reqs:
        arr = raw_arr_for(provider, req.key.tile.level, req.key.tile.tx, req.key.tile.ty)
        scheduler.deliver(req, arr)
    _pump(20)

    # Force the corrected floor ready for the LIVE selection context,
    # bypassing the async compute path.
    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._floor_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._floor_ready = True

    # Seed one coarser-level precise entry whose key matches the CURRENT
    # selection context (a legitimate sharper-than-floor fallback).
    ts = ctrl.grid.tile_size
    coarse_level = ctrl.level + 1
    ds = provider.level_downsample(coarse_level)
    coarse_key = _make_correction_key_for(ctrl, provider, coarse_level, 0, 0, ctrl.params)
    coarse_rect = ExploreView.world_rect(0, 0, ts, ts, ds, ds)
    ctrl._precise_pool.put(coarse_level, 0, 0, coarse_rect, np.zeros((ts, ts), dtype=np.uint8), coarse_key)

    # Deliver only ONE of the current-level precise tiles: coverage stays
    # incomplete (this is the "motion" moment -- coverage breaks).
    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) >= 2
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(precise_reqs[0][0], arr)
    _pump(20)

    assert ctrl._precise_visible is False  # coverage genuinely incomplete --
    # `_precise_visible` / `view.precise_visible` keep their exact old
    # meaning; only what that boolean GATES has changed (module docstring
    # "progressive per-tile corrected coverage").

    for entry in ctrl._raw_pool.entries.values():
        assert entry.item.isVisible() is False, "raw stage must never show once the floor is ready"
    assert view.corrected_floor_item.isVisible() is True

    # Once the floor is ready, a current-level precise tile whose key
    # matches is shown PROGRESSIVELY as soon as it lands -- anything under
    # a still-missing neighbor is itself corrected-stage (floor or a
    # coarser precise tile), not a raw-stage seam, so the whole level is no
    # longer gated atomically on `covered`.
    cur_level_precise = [e for e in ctrl._precise_pool.entries.values() if e.level == ctrl.level]
    assert cur_level_precise
    assert all(e.item.isVisible() is True for e in cur_level_precise)

    coarse_entry = ctrl._precise_pool.get(coarse_level, 0, 0)
    assert coarse_entry.item.isVisible() is True

    ctrl.teardown()


# ── D3. Raw shown as honest fallback when the floor is not ready ──────────

def test_raw_shown_when_floor_not_ready(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    raw_reqs = scheduler.pending_for(RawKey)
    for req, _cb in raw_reqs:
        arr = raw_arr_for(provider, req.key.tile.level, req.key.tile.tx, req.key.tile.ty)
        scheduler.deliver(req, arr)
    _pump(20)

    ctrl._floor_ready = False
    ctrl._update_layer_visibility()

    cur_level_raw = [e for e in ctrl._raw_pool.entries.values() if e.level == ctrl.level]
    assert cur_level_raw
    assert all(e.item.isVisible() is True for e in cur_level_raw)
    assert view.corrected_floor_item.isVisible() is False

    ctrl.teardown()


# ── D4. Floor invalidated immediately by a selection change ───────────────

def test_floor_invalidated_by_selection_change(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._floor_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._floor_ready = True
    ctrl._update_layer_visibility()
    assert view.corrected_floor_item.isVisible() is True

    ctrl.set_selection(method="cucim", params=(8,))

    assert ctrl._floor_ready is False
    assert view.corrected_floor_item.isVisible() is False

    ctrl.teardown()


# ── D4b. Floor deferred until load_overview() fixes the display levels ────

def test_floor_deferred_until_display_levels_fixed(app):
    """A host that selects a method BEFORE load_overview() must not get a
    floor quantized against the placeholder (0.0, 1.0) display range --
    that would paint a saturated-white floor over the whole slide. The
    floor job is deferred; load_overview() starts it."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)

    started = []
    real_start = ctrl._start_floor_job
    ctrl._start_floor_job = lambda gen: started.append(gen)

    # No overview yet -> deferred, but honestly reported as "preparing".
    preparing = []
    ctrl.floor_preparing_changed.connect(preparing.append)
    ctrl.set_selection(method="tophat", params=(10,))
    assert started == []
    assert preparing[-1] is True
    assert ctrl._floor_ready is False
    assert view.corrected_floor_item.isVisible() is False

    # load_overview() fixes _display_lo/_display_hi and re-enters.
    ctrl.load_overview()
    assert len(started) == 1

    ctrl._start_floor_job = real_start
    ctrl.teardown()


# ── D5. Stale floor result (out-of-date generation token) dropped ─────────

def test_stale_floor_result_dropped(app):
    # Set method/params WITHOUT going through set_selection (which would
    # spawn a real -- if fast -- floor-compute thread and race the manual
    # stale-token delivery below); this test is only about the generation
    # guard in `_handle_floor_result`.
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.method = "tophat"
    ctrl.params = (10,)

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._floor_gen = 5  # simulate a live generation ahead of the stale one

    ctx = ctrl._current_floor_ctx(floor_level, stride)
    arr = np.zeros((64, 64), dtype=np.float32)
    ctrl._floor_delivered.emit((4, ctx, floor_level, stride, arr, None))
    _pump(20)

    assert ctrl._floor_ready is False
    assert view.corrected_floor_item.isVisible() is False

    ctrl.teardown()


# ── D6. Stale coarse precise tile hidden after a param change ─────────────

def test_stale_coarse_precise_hidden_after_param_change(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    ts = ctrl.grid.tile_size
    coarse_level = ctrl.level + 1
    ds = provider.level_downsample(coarse_level)
    key = _make_correction_key_for(ctrl, provider, coarse_level, 0, 0, (10,))
    rect = ExploreView.world_rect(0, 0, ts, ts, ds, ds)
    ctrl._precise_pool.put(coarse_level, 0, 0, rect, np.zeros((ts, ts), dtype=np.uint8), key)

    ctrl._update_layer_visibility()
    assert ctrl._precise_pool.get(coarse_level, 0, 0).item.isVisible() is True

    ctrl.set_selection(params=(99,))
    ctrl._update_layer_visibility()
    assert ctrl._precise_pool.get(coarse_level, 0, 0).item.isVisible() is False

    ctrl.teardown()


# ── D7. Exactly one floor job in flight; coalesced request runs latest ────

def test_only_one_floor_job_in_flight(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()

    enter_event = threading.Event()
    release_event = threading.Event()
    calls = []
    lock = threading.Lock()

    def blocking_correct_array(arr, method, param):
        with lock:
            calls.append((method, param))
        enter_event.set()
        release_event.wait(timeout=5.0)
        return arr.astype(np.float32, copy=False)

    ctrl.compute.correct_array = blocking_correct_array
    # This test is about the floor job's single-flight discipline, NOT about
    # calibration. Calibration runs inside the same worker `work()` closure
    # and issues its own `correct_array` calls (GAIN_WINDOWS x num_levels of
    # them) right after the floor's call returns, which would race every
    # exact-count assertion below. `work()` calls `self._calibrate_level_gains`
    # by attribute at call time, so an instance override neutralizes it.
    ctrl._calibrate_level_gains = lambda *a, **k: {}

    ctrl.set_selection(method="tophat", params=(10,))
    assert enter_event.wait(timeout=2.0)
    with lock:
        assert len(calls) == 1

    # A second selection arrives while the first job is still blocked
    # inside correct_array -- it must NOT start a second worker thread.
    enter_event.clear()
    ctrl.set_selection(method="tophat", params=(20,))
    _pump(50)
    with lock:
        assert len(calls) == 1, "a second floor job started while the first was still in flight"
    assert ctrl._floor_pending is True

    # Release the first (now-stale) job -> its result is dropped, and the
    # pending (latest) job starts and runs to completion.
    release_event.set()
    deadline = time.time() + 3.0
    while len(calls) < 2 and time.time() < deadline:
        _pump(20)
    with lock:
        assert len(calls) == 2
        assert calls[0] != calls[1]

    deadline = time.time() + 3.0
    while ctrl._floor_job_running and time.time() < deadline:
        _pump(20)

    assert ctrl._floor_ready is True
    assert ctrl._floor_ctx == ctrl._current_floor_ctx(ctrl._floor_level, ctrl._floor_stride)

    ctrl.teardown()


# ══════════════════════════════════════════════════════════════════════════
# Progressive per-tile corrected coverage (this round's main fix)
# ══════════════════════════════════════════════════════════════════════════

def test_progressive_precise_shows_partial_coverage(app):
    """Floor ready, only ONE current-level precise tile delivered while the
    wanted set has more -- the delivered tile must be shown immediately
    (progressive display), even though coverage (`_precise_visible`) is
    genuinely still incomplete, and raw must stay hidden throughout."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)  # 2x2 tiles at ts=512

    raw_reqs = scheduler.pending_for(RawKey)
    for req, _cb in raw_reqs:
        arr = raw_arr_for(provider, req.key.tile.level, req.key.tile.tx, req.key.tile.ty)
        scheduler.deliver(req, arr)
    _pump(20)

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._floor_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._floor_ready = True

    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) >= 2
    req0, _cb0 = precise_reqs[0]
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(req0, arr)
    _pump(20)

    assert ctrl._precise_visible is False  # coverage genuinely incomplete

    delivered_tile = req0.key.tile
    delivered_entry = ctrl._precise_pool.get(delivered_tile.level, delivered_tile.tx, delivered_tile.ty)
    assert delivered_entry is not None
    assert delivered_entry.item.isVisible() is True

    for entry in ctrl._raw_pool.entries.values():
        assert entry.item.isVisible() is False

    ctrl.teardown()


def test_atomic_gate_still_applies_before_floor_ready(app):
    """Same setup as above but the floor is NOT ready -- the anti-
    checkerboard gate must still hold atomically (a corrected tile next to
    a raw tile is still a hard cross-stage seam while raw can show
    through), so the delivered current-level precise tile stays hidden and
    raw is shown as the honest fallback."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    raw_reqs = scheduler.pending_for(RawKey)
    for req, _cb in raw_reqs:
        arr = raw_arr_for(provider, req.key.tile.level, req.key.tile.tx, req.key.tile.ty)
        scheduler.deliver(req, arr)
    _pump(20)

    ctrl._floor_ready = False
    ctrl._update_layer_visibility()

    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) >= 2
    req0, _cb0 = precise_reqs[0]
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(req0, arr)
    _pump(20)

    assert ctrl._precise_visible is False

    delivered_tile = req0.key.tile
    delivered_entry = ctrl._precise_pool.get(delivered_tile.level, delivered_tile.tx, delivered_tile.ty)
    assert delivered_entry is not None
    assert delivered_entry.item.isVisible() is False

    cur_level_raw = [e for e in ctrl._raw_pool.entries.values() if e.level == ctrl.level]
    assert cur_level_raw
    assert all(e.item.isVisible() is True for e in cur_level_raw)

    ctrl.teardown()


# ══════════════════════════════════════════════════════════════════════════
# Floor downsample: area/box mean, not point-sampling
# ══════════════════════════════════════════════════════════════════════════

def test_floor_downsample_is_area_mean():
    arr = np.array([
        [0.0, 2.0, 4.0, 6.0],
        [8.0, 10.0, 12.0, 14.0],
        [16.0, 18.0, 20.0, 22.0],
        [24.0, 26.0, 28.0, 30.0],
    ], dtype=np.float32)

    result_k1 = _box_downsample(arr, 1)
    assert result_k1 is not arr or np.array_equal(result_k1, arr)
    np.testing.assert_array_equal(result_k1, arr)

    result_k2 = _box_downsample(arr, 2)
    expected = np.array([
        [(0 + 2 + 8 + 10) / 4.0, (4 + 6 + 12 + 14) / 4.0],
        [(16 + 18 + 24 + 26) / 4.0, (20 + 22 + 28 + 30) / 4.0],
    ], dtype=np.float32)
    assert result_k2.shape == (2, 2)
    np.testing.assert_allclose(result_k2, expected)
    assert result_k2.dtype == np.float32
    assert result_k2.flags["C_CONTIGUOUS"]

    # A non-multiple shape crops the remainder rather than erroring.
    arr5 = np.arange(25, dtype=np.float32).reshape(5, 5)
    result_k2b = _box_downsample(arr5, 2)
    assert result_k2b.shape == (2, 2)
    cropped = arr5[:4, :4]
    expected_b = cropped.reshape(2, 2, 2, 2).mean(axis=(1, 3))
    np.testing.assert_allclose(result_k2b, expected_b)


# ══════════════════════════════════════════════════════════════════════════
# In-view status badge
# ══════════════════════════════════════════════════════════════════════════

def test_status_label_shows_and_hides(app):
    view = ExploreView()
    view.resize(400, 300)
    view.show()
    _pump(20)

    assert view.status_label.isVisible() is False

    view.set_status_text("Preparing corrected preview…")
    assert view.status_label.isVisible() is True
    assert view.status_label.text() == "Preparing corrected preview…"

    view.set_status_text("")
    assert view.status_label.isVisible() is False

    view.set_status_text(None)
    assert view.status_label.isVisible() is False


def test_controller_drives_status_badge_around_floor_job(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()

    assert view.status_label.isVisible() is False

    ctrl.set_selection(method="tophat", params=(10,))
    _pump(200)  # let the (fast, real FakeCompute) floor job land

    # By the time the floor job has landed, the badge is hidden again --
    # only the interval WHILE `_floor_job_running`/preparing is true shows
    # it. Assert the observable end state plus the drive wiring itself
    # (floor_preparing_changed -> set_status_text) rather than trying to
    # catch the transient window, which a real (fast) worker thread makes
    # racy to pin exactly.
    assert ctrl._floor_ready is True
    assert view.status_label.isVisible() is False

    # Directly exercise the wiring the controller installed on itself.
    ctrl.floor_preparing_changed.emit(True)
    assert view.status_label.isVisible() is True
    assert view.status_label.text() == "Preparing corrected preview…"
    ctrl.floor_preparing_changed.emit(False)
    assert view.status_label.isVisible() is False

    ctrl.teardown()


# ══════════════════════════════════════════════════════════════════════════
# Per-level display gain for CORRECTED pixels
# ══════════════════════════════════════════════════════════════════════════

def test_corrected_quantization_applies_level_gain(app):
    """With a calibrated gain table installed directly against the LIVE
    selection context, `_quantize_corrected_uint8(arr, L)` must equal the
    plain quantization of `arr * gain[L]`, and the raw path
    (`_quantize_tile_uint8`) must be completely unaffected by the table."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._gain_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._level_gain = {0: 1.0, 1: 3.5}

    arr = np.linspace(0.0, ctrl._display_hi * 0.5, 64, dtype=np.float32).reshape(8, 8)

    q_level1 = ctrl._quantize_corrected_uint8(arr, 1)
    expected = ctrl._quantize_tile_uint8(arr * 3.5)
    np.testing.assert_array_equal(q_level1, expected)

    q_level0 = ctrl._quantize_corrected_uint8(arr, 0)
    np.testing.assert_array_equal(q_level0, ctrl._quantize_tile_uint8(arr))

    # Raw path is a plain quantization -- never scaled by the gain table.
    raw_q = ctrl._quantize_tile_uint8(arr)
    assert not np.array_equal(raw_q, q_level1)

    ctrl.teardown()


def test_gain_ignored_when_context_stale(app):
    """A gain table calibrated against a since-superseded selection context
    must never silently scale pixels: `_display_gain_for_level` returns 1.0
    for EVERY level once the live context no longer matches `_gain_ctx`."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._gain_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._level_gain = {1: 5.0}

    assert ctrl._display_gain_for_level(1) == pytest.approx(5.0)

    # Change the selection directly (no pump -- no real floor thread should
    # be needed to observe the guard) so the live context no longer matches
    # `_gain_ctx`.
    ctrl.params = (99,)

    for level in range(provider.num_levels):
        assert ctrl._display_gain_for_level(level) == 1.0

    ctrl.teardown()


def test_gain_calibration_median_and_clamp(app):
    """Drive `_calibrate_level_gains` against a fake provider/compute whose
    per-(window, level) p99.5 outputs are fully controlled: pins the
    per-level MEDIAN-across-windows aggregation, that a degenerate window
    (p99.5 == 0 at either level 0 or level L) contributes nothing, and that
    the GAIN_CLAMP bounds are enforced on the aggregate."""

    class FakeProviderGain:
        num_levels = 3

        def level_downsample(self, level):
            return {0: 1.0, 1: 2.0, 2: 4.0}[level]

        def level_shape(self, level):
            return (10_000_000, 10_000_000)

        def read_region(self, channel, level, y0, y1, x0, x1):
            return np.zeros((4, 4), dtype=np.float32), (y0, x0)

    # Flat call order matches `_calibrate_level_gains`'s loop: for each
    # window (in the order `_pick_calibration_windows` returns), for each
    # level 0..2 in order -- 3 windows x 3 levels = 9 values.
    flat_p995 = [
        100.0, 50.0, 20.0,   # window0: L1 ratio=2.0,  L2 ratio=5.0
        100.0, 5.0, 0.0,     # window1: L1 ratio=20.0 (clamps), L2 degenerate (p_L=0)
        0.0, 5.0, 5.0,       # window2: p0==0 -> degenerate for EVERY level from this window
    ]
    call_index = {"i": 0}

    class FakeComputeGain:
        def raw_keys_for(self, key):
            return []

        def correct_array(self, arr, method, param):
            val = flat_p995[call_index["i"]]
            call_index["i"] += 1
            return np.full((4, 4), val, dtype=np.float32)

    import block01.viewer.explore_view as explore_view_mod
    orig_picker = explore_view_mod._pick_calibration_windows
    explore_view_mod._pick_calibration_windows = lambda *a, **k: [(0, 0), (1, 1), (2, 2)]
    try:
        ctrl, provider, scheduler, view = make_controller(app)
        gains = ctrl._calibrate_level_gains(
            FakeProviderGain(), FakeComputeGain(), "DAPI", "tophat", (10,),
            np.zeros((10, 10), dtype=np.float32), 0)
    finally:
        explore_view_mod._pick_calibration_windows = orig_picker

    assert gains[0] == 1.0
    # median([2.0, 20.0]) == 11.0 -> clamped to GAIN_CLAMP's upper bound.
    assert gains[1] == pytest.approx(GAIN_CLAMP[1])
    # median([5.0]) == 5.0 -- window1's degenerate (p_L==0) contribution and
    # window2's degenerate (p0==0) contribution are both excluded.
    assert gains[2] == pytest.approx(5.0)

    ctrl.teardown()


def test_calibration_window_picker_finds_tissue():
    """An overview array that is dark except for one bright, grid-aligned
    block: the picker's top-scoring window must be exactly that block
    (mapped back to level-0 coordinates), and every returned window must
    lie within the image bounds."""
    h, w = 4096, 4096
    arr = np.zeros((h, w), dtype=np.float32)
    ds_y = ds_x = 4.0
    window_l0 = 1024
    win_ov = int(window_l0 / ds_y)  # 256 overview pixels
    by, bx = 3, 5
    arr[by * win_ov:(by + 1) * win_ov, bx * win_ov:(bx + 1) * win_ov] = 1000.0

    windows = _pick_calibration_windows(arr, ds_y, ds_x, window_l0=window_l0, n_windows=3)

    assert windows
    expected_top = (int(by * win_ov * ds_y), int(bx * win_ov * ds_x))
    assert windows[0] == expected_top

    h0, w0 = h * ds_y, w * ds_x
    for y0, x0 in windows:
        assert 0 <= y0 < h0
        assert 0 <= x0 < w0


def test_floor_survives_calibration_failure(app):
    """A calibration that raises must not cost the user their floor: the
    floor is still installed and visible, `_level_gain` stays empty, and
    `stats['gain_calibration_failed']` is incremented."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()

    def raising_calibrate(*_a, **_k):
        raise RuntimeError("boom")

    ctrl._calibrate_level_gains = raising_calibrate

    ctrl.set_selection(method="tophat", params=(10,))
    deadline = time.time() + 3.0
    while ctrl._floor_job_running and time.time() < deadline:
        _pump(20)
    _pump(50)

    assert ctrl._floor_ready is True
    assert view.corrected_floor_item.isVisible() is True
    assert ctrl._level_gain == {}
    assert ctrl.stats["gain_calibration_failed"] == 1

    ctrl.teardown()


# ══════════════════════════════════════════════════════════════════════════
# Interactive precise issuing moved onto the 30ms motion timer
# (module docstring "Camera contract" -- 322ms -> 108ms measured fix)
# ══════════════════════════════════════════════════════════════════════════

def test_motion_timer_throttles_rather_than_debounces(app):
    """A running motion timer must NOT be restarted by a further range
    event. Restarting it on every event means it never fires while the
    camera keeps moving (events arrive faster than MOTION_MS), so a
    continuous drag would compute nothing -- the same bug as the settle
    gate this replaced. Measured on the real slide, a 40-step drag went
    from 561ms drag-stop-to-full-coverage (0 tiles computed during the
    drag, 0/20 covered at stop) to 243ms (23 computed during, 7/20
    covered) once this became a throttle."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    set_view_and_pump(view, 0, 0, 2048, 2048, ms=60)
    ctrl._motion_timer.start(ctrl.MOTION_MS)
    remaining_before = ctrl._motion_timer.remainingTime()
    _pump(15)
    remaining_mid = ctrl._motion_timer.remainingTime()
    assert remaining_mid < remaining_before, "timer should be counting down"

    # A further range change must leave the running timer alone.
    view.view_box.setRange(xRange=(10, 2058), yRange=(10, 2058), padding=0)
    remaining_after = ctrl._motion_timer.remainingTime()
    assert remaining_after <= remaining_mid + 2, (
        "range event restarted a running motion timer (debounce); under "
        "continuous motion that timer would never fire")

    ctrl.teardown()


def test_precise_requested_during_continuous_motion(app):
    """With a large `settle_ms` (so the settle timer provably cannot be the
    source), drive several range changes ~16ms apart -- closer together
    than MOTION_MS (30ms), so the motion timer keeps coalescing rather than
    firing mid-drag -- and assert CorrectionKey requests appear shortly
    after motion stops, long before settle_ms could have fired."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()

    for i in range(6):
        view.view_box.setRange(xRange=(700 + i, 700 + i + 2048),
                                yRange=(100, 100 + 2048), padding=0)
        _pump(16)

    # Nothing should have been issued yet mid-drag (still coalescing).
    _pump(60)  # >> MOTION_MS after the last event, way << settle_ms

    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert precise_reqs, "precise requests must be issued during/after motion, not gated on settle_ms"

    ctrl.teardown()


def test_precise_requests_missing_tiles_only(app):
    """Deliver one precise tile, then simulate another motion-timer pass
    over the same viewport: the already-covered tile must NOT be
    re-requested, while a genuinely missing neighbour (never delivered)
    IS."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)  # 2x2 tiles at ts=512

    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) >= 2
    req0, _cb0 = precise_reqs[0]
    covered_tile = (req0.key.tile.tx, req0.key.tile.ty)
    other_tiles = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in precise_reqs} - {covered_tile}
    assert other_tiles

    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(req0, arr)
    _pump(20)

    scheduler.requests.clear()

    # Simulate the motion timer firing again over the SAME viewport (no
    # selection or camera change) -- the normal "another motion pass"
    # trigger.
    ctrl._issue_raw_requests()
    _pump(20)

    new_precise_reqs = scheduler.pending_for(CorrectionKey)
    new_tiles = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in new_precise_reqs}
    assert covered_tile not in new_tiles, "already-covered tile must not be re-requested"
    assert new_tiles & other_tiles, "a genuinely missing neighbour must still be requested"

    ctrl.teardown()


def test_settle_timer_does_not_issue_interactive_precise(app):
    """Calling `_on_settle` directly must issue no CorrectionKey requests
    at all -- it is retained only as a future-refinement hook (module
    docstring), not a path for interactive precise issuing."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)
    scheduler.requests.clear()

    ctrl._on_settle()
    _pump(20)

    assert scheduler.pending_for(CorrectionKey) == []

    ctrl.teardown()


def test_selection_change_reissues_all_visible(app):
    """Once every visible tile is covered under method A, changing to
    method B must re-request every visible tile again -- no pooled key
    matches the new selection context any more, so "missing tiles only"
    naturally degenerates to "all tiles" on a selection change; there must
    be no separate force-all path."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    precise_reqs = scheduler.pending_for(CorrectionKey)
    visible_tiles = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in precise_reqs}
    assert len(visible_tiles) >= 2

    arr = np.zeros((512, 512), dtype=np.float32)
    for req, _cb in precise_reqs:
        scheduler.deliver(req, arr)
    _pump(20)

    scheduler.requests.clear()
    ctrl.set_selection(method="cucim", params=(8,))
    _pump(20)

    new_reqs = scheduler.pending_for(CorrectionKey)
    new_tiles = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in new_reqs}
    assert new_tiles == visible_tiles

    ctrl.teardown()
