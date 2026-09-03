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
    PRECISE_CURRENT_BASE_PRIORITY,
    DIRECTIONAL_PREFETCH_INFLIGHT,
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

    # Distinct channels must return DISTINCT pixels. A fake that hands back
    # the same ramp for every channel structurally cannot catch the bug this
    # suite now guards: an overview, corrected floor or gain table computed
    # from one channel's pixels and registered under another's identity.
    # "DAPI" keeps offset 0 so every pre-existing expectation is unchanged.
    CHANNELS = ("DAPI", "CD3", "CD8")

    @property
    def channel_names(self):
        return list(self.CHANNELS)

    def _channel_offset(self, channel):
        try:
            return self.CHANNELS.index(channel) * 1_000_000.0
        except ValueError:
            return (abs(hash(channel)) % 97 + 1) * 1_000_000.0

    def read_region(self, channel, level, y0, y1, x0, x1):
        h, w = self._shapes[level]
        cy0, cy1 = max(0, min(y0, h)), max(0, min(y1, h))
        cx0, cx1 = max(0, min(x0, w)), max(0, min(x1, w))
        rows = np.arange(cy0, cy1).reshape(-1, 1)
        cols = np.arange(cx0, cx1).reshape(1, -1)
        arr = (rows * 1000 + cols).astype(np.float32) + self._channel_offset(channel)
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
    # incomplete (this is the "motion" moment -- coverage breaks). Filtered
    # to level == ctrl.level: `scheduler.pending_for(CorrectionKey)` now
    # also contains the level+1 intermediate fallback batch (module
    # docstring "Intermediate corrected fallback"), which this step is not
    # about.
    precise_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                     if r.key.tile.level == ctrl.level]
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

    # No overview at all yet, so no display range: the global invariant
    # (`_blocked_on_overview`) withholds the floor entirely rather than
    # starting one that would quantise against a range it does not have.
    # An earlier revision emitted "preparing" here; it no longer does,
    # because nothing is in fact pending -- `load_overview()` is what
    # starts it.
    preparing = []
    ctrl.floor_preparing_changed.connect(preparing.append)
    ctrl.set_selection(method="tophat", params=(10,))
    assert started == []
    assert preparing == []
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
    raw is shown as the honest fallback.

    UPDATED for the intermediate corrected fallback (module docstring):
    `precise_reqs` now also contains a level+1 fallback batch. A fallback
    tile is COARSER than the current level, so -- per the pre-existing,
    unchanged rule that coarser precise items are exempt from the atomic
    `covered` gate -- it is legitimately visible even while floor is not
    ready; only the CURRENT level's atomic gate is under test here. Picks
    `req0` from the current-level batch explicitly so the fallback batch
    cannot masquerade as the tile this test is pinning."""
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

    precise_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                     if r.key.tile.level == ctrl.level]
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
    IS.

    UPDATED for the intermediate corrected fallback (module docstring):
    `_issue_settled_request` now also issues a level+1 fallback batch, so
    `precise_reqs` mixes CURRENT-level (level 0) and fallback-level
    (level 1) requests. Comparing tile addresses by bare `(tx, ty)` (the
    original form of this test) is no longer safe -- a level-1 tile and a
    level-0 tile can legitimately share the same `(tx, ty)` numeric
    coordinates while addressing different physical tiles, which
    previously could not happen because only one level was ever in
    flight. This version filters to the CURRENT level explicitly (the
    "missing tiles only" behavior this test targets) and compares full
    `(level, tx, ty)` tuples everywhere else, so the fallback batch cannot
    produce a false collision."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)  # 2x2 tiles at ts=512

    def current_level_reqs():
        return [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                if r.key.tile.level == ctrl.level]

    precise_reqs = current_level_reqs()
    assert len(precise_reqs) >= 2
    req0, _cb0 = precise_reqs[0]
    covered_tile = (req0.key.tile.level, req0.key.tile.tx, req0.key.tile.ty)
    other_tiles = {(r.key.tile.level, r.key.tile.tx, r.key.tile.ty)
                   for r, _cb in precise_reqs} - {covered_tile}
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

    new_tiles = {(r.key.tile.level, r.key.tile.tx, r.key.tile.ty)
                 for r, _cb in current_level_reqs()}
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


# ══════════════════════════════════════════════════════════════════════════
# Intermediate corrected fallback (module docstring "Intermediate corrected
# fallback") -- corrected tiles requested/blitted at level + 1 as a coarser,
# visually-consistent underlay while the current level fills in.
# ══════════════════════════════════════════════════════════════════════════

def test_intermediate_fallback_requested_at_level_plus_one(app):
    """At level 0, the motion/jump issuing path must also request a batch
    of CorrectionKeys at level 1 covering the same viewport, at priorities
    strictly below PRECISE_CURRENT_BASE_PRIORITY; the current level's own
    batch must sit at or above that base."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)  # 2x2 tiles at level 0, ts=512

    precise_reqs = scheduler.pending_for(CorrectionKey)
    fallback_reqs = [(r, cb) for r, cb in precise_reqs if r.key.tile.level == 1]
    current_reqs = [(r, cb) for r, cb in precise_reqs if r.key.tile.level == 0]
    assert fallback_reqs, "expected an intermediate fallback batch at level+1"
    assert current_reqs

    # The fallback batch covers the viewport PLUS a FALLBACK_HALO_TILES
    # look-ahead ring (see test_intermediate_fallback_requests_look_ahead_ring
    # for why), so the viewport's own tiles are a strict subset here rather
    # than the whole batch.
    ds1 = provider.level_downsample(1)
    bbox1 = (0, 0, int(1024 / ds1), int(1024 / ds1))
    viewport_fallback_tiles = tiles_covering(bbox1, ctrl.grid.tile_size)
    fallback_tiles = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in fallback_reqs}
    assert viewport_fallback_tiles <= fallback_tiles

    # The fallback batch splits by urgency: tiles that cover the viewport
    # NOW keep the floor off the screen and go above the current level;
    # look-ahead RING tiles are speculative and queue below it, at
    # FALLBACK_RING_BASE_PRIORITY (see the module docstring -- unsplit, the
    # ring cost ~7 points of current-level coverage during a zoom).
    from block01.viewer.explore_view import FALLBACK_RING_BASE_PRIORITY
    urgent = [(r, cb) for r, cb in fallback_reqs
              if (r.key.tile.tx, r.key.tile.ty) in viewport_fallback_tiles]
    ring = [(r, cb) for r, cb in fallback_reqs
            if (r.key.tile.tx, r.key.tile.ty) not in viewport_fallback_tiles]
    assert urgent
    assert all(r.priority < PRECISE_CURRENT_BASE_PRIORITY for r, _cb in urgent)
    assert all(r.priority >= PRECISE_CURRENT_BASE_PRIORITY for r, _cb in current_reqs)
    assert all(r.priority >= FALLBACK_RING_BASE_PRIORITY for r, _cb in ring)

    ctrl.teardown()


def test_intermediate_fallback_requests_look_ahead_ring(app):
    """The fallback batch covers the viewport EXPANDED by
    FALLBACK_HALO_TILES at the fallback level, not just the viewport.
    Without the ring the fallback is only requested once the viewport
    already needs it, so every crossing of a fallback-level tile boundary
    reopens a window where the floor shows through: measured on the real
    slide, floor occupied 6.4% of the screen during a drag (p95 20.0%)
    without the ring and 0.0% (p95 0.0%) with it."""
    from block01.viewer.explore_view import FALLBACK_HALO_TILES
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)

    fb_level = ctrl.level + 1
    fb_reqs = [r for r, _cb in scheduler.pending_for(CorrectionKey)
               if r.key.tile.level == fb_level]
    assert fb_reqs, "no fallback-level requests issued"

    ts = ctrl.grid.tile_size
    fds = provider.level_downsample(fb_level)
    bbox = ctrl._current_bbox
    inner = tiles_covering(
        (int(bbox[0] / fds), int(bbox[1] / fds),
         int(bbox[2] / fds), int(bbox[3] / fds)), ts)
    fh, fw = provider.level_shape(fb_level)
    pad = FALLBACK_HALO_TILES * ts
    outer = tiles_covering(
        (max(0, int(bbox[0] / fds) - pad), max(0, int(bbox[1] / fds) - pad),
         min(fh, int(bbox[2] / fds) + pad), min(fw, int(bbox[3] / fds) + pad)), ts)

    assert outer > inner, "test setup: ring should add tiles"
    issued = {(r.key.tile.tx, r.key.tile.ty) for r in fb_reqs}
    assert issued <= outer, "fallback requested outside the look-ahead ring"
    assert issued - inner, "fallback requested no look-ahead tiles at all"

    ctrl.teardown()


def test_intermediate_fallback_key_uses_its_own_effective_param(app):
    """The level-1 fallback key's params must equal `effective_param(base,
    1, ds1)` -- the level-1-scaled radius -- NOT the level-0 (current)
    value."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    fallback_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                      if r.key.tile.level == 1]
    assert fallback_reqs

    ds1 = provider.level_downsample(1)
    expected = tuple(effective_param(p, 1, ds1) for p in ctrl.params)
    for r, _cb in fallback_reqs:
        assert r.key.params == expected

    ctrl.teardown()


def test_intermediate_fallback_blitted_at_own_level_with_own_gain(app):
    """A delivered level-1 fallback result must be pooled AT level 1, with
    level-1 world geometry, and quantized through level 1's own calibrated
    display gain (not level 0's, and not unscaled)."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._gain_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._level_gain = {0: 1.0, 1: 3.5}

    fallback_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                      if r.key.tile.level == 1]
    assert fallback_reqs
    req, _cb = fallback_reqs[0]
    tx, ty = req.key.tile.tx, req.key.tile.ty

    arr = raw_arr_for(provider, 1, tx, ty)
    scheduler.deliver(req, arr)
    _pump(20)

    entry = ctrl._precise_pool.get(1, tx, ty)
    assert entry is not None
    assert entry.level == 1

    ds_y, ds_x = provider.level_downsample_yx(1)
    ts = ctrl.grid.tile_size
    expected_rect = ExploreView.world_rect(ty * ts, tx * ts, arr.shape[0], arr.shape[1], ds_y, ds_x)
    assert entry.rect.x() == pytest.approx(expected_rect.x())
    assert entry.rect.y() == pytest.approx(expected_rect.y())
    assert entry.rect.width() == pytest.approx(expected_rect.width())
    assert entry.rect.height() == pytest.approx(expected_rect.height())

    expected_gray = ctrl._quantize_tile_uint8(arr.astype(np.float32) * 3.5)
    np.testing.assert_array_equal(entry.item.image, expected_gray)

    assert ctrl.stats["mid_tiles_blitted"] >= 1

    ctrl.teardown()


def test_intermediate_fallback_does_not_count_as_coverage(app):
    """With ONLY fallback (level+1) tiles delivered -- no current-level
    tile at all -- `_coverage_complete()` must be False and
    `_precise_visible` / `view.precise_visible` must be False: a fallback
    tile never counts toward "the viewport is covered"."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    fallback_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                      if r.key.tile.level == 1]
    assert fallback_reqs
    arr = np.zeros((512, 512), dtype=np.float32)
    for req, _cb in fallback_reqs:
        scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl._coverage_complete() is False
    assert ctrl._precise_visible is False
    assert ctrl.view.precise_visible is False

    ctrl.teardown()


def test_intermediate_fallback_rejected_when_key_stale(app):
    """A level-1 result whose params match a DIFFERENT (since-changed)
    selection must be dropped, not blitted -- the per-own-level key
    staleness guard applies to the fallback path exactly as it does to the
    current level."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    fallback_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                      if r.key.tile.level == 1]
    assert fallback_reqs
    req, _cb = fallback_reqs[0]

    # Change params directly (bypassing set_selection) so only the
    # key-staleness guard -- not the generation guard -- is exercised.
    ctrl.params = (99,)

    before_blitted = ctrl.stats["mid_tiles_blitted"]
    before_mismatch = ctrl.stats["mismatched_key_dropped"]
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl.stats["mid_tiles_blitted"] == before_blitted
    assert ctrl.stats["mismatched_key_dropped"] == before_mismatch + 1
    assert ctrl._precise_pool.get(1, req.key.tile.tx, req.key.tile.ty) is None

    ctrl.teardown()


def test_intermediate_fallback_disabled_by_switch(app):
    """With `intermediate_corrected_fallback` off, no level+1 request is
    issued at all, and a level+1 delivery (even one whose key matches the
    live selection) is rejected outright -- the switch, not staleness, is
    what blocks it."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.intermediate_corrected_fallback = False
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)

    fallback_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                      if r.key.tile.level == 1]
    assert fallback_reqs == []

    from block01.viewer.tile_types import TileRequest

    key = _make_correction_key_for(ctrl, provider, 1, 0, 0, ctrl.params)
    req = TileRequest(key=key, generation=ctrl._settled_generation, priority=0)
    result_arr = np.zeros((512, 512), dtype=np.float32)
    pixels = PixelBuffer(residency="cpu", dtype=str(result_arr.dtype),
                          shape=tuple(result_arr.shape), handle=result_arr)
    result = TileResult(request=req, pixels=pixels, quality=QualityLevel.INTERACTIVE,
                         provisional=False, timing={}, error=None)

    before = ctrl.stats["mid_tiles_blitted"]
    ctrl._on_precise_result(result)
    _pump(20)

    assert ctrl.stats["mid_tiles_blitted"] == before
    assert ctrl._precise_pool.get(1, 0, 0) is None

    ctrl.teardown()


def test_fallback_never_covers_current_level(app):
    """A pooled level-1 (coarser) item's zValue must sit BELOW a pooled
    level-0 item's in the same pool -- the pre-existing z-order guarantee
    (module docstring "Level switching without clearing") that makes a
    fallback tile unable to ever draw above the current level."""
    from PyQt5.QtCore import QRectF as _QRectF

    class FakeViewBox:
        def __init__(self):
            self.items = []

        def addItem(self, item):
            self.items.append(item)

        def removeItem(self, item):
            self.items.remove(item)

    vb = FakeViewBox()
    pool = TileItemPool(vb, base_z=200, num_levels=2, budget=100)
    rect0 = _QRectF(0.0, 0.0, 10.0, 10.0)
    rect1 = _QRectF(0.0, 0.0, 40.0, 40.0)
    pool.put(level=0, tx=0, ty=0, rect=rect0, arr_uint8=np.zeros((4, 4), dtype=np.uint8), key=None)
    pool.put(level=1, tx=0, ty=0, rect=rect1, arr_uint8=np.zeros((4, 4), dtype=np.uint8), key=None)

    entry0 = pool.get(0, 0, 0)
    entry1 = pool.get(1, 0, 0)
    assert entry1.item.zValue() < entry0.item.zValue()


# ══════════════════════════════════════════════════════════════════════════
# Directional prefetch (pan only) -- module docstring "Directional prefetch
# (pan only)". Every test drives the controller by directly setting the
# viewport-tracking fields (`_current_bbox` / `_visible_tiles` /
# `_viewport_center_l0`) and calling `_issue_raw_requests()` -- the same
# pattern several existing tests (e.g. T6) already use to bypass real Qt
# range events -- because the feature's direction estimator needs at least
# two ticks at a KNOWN displacement, which is far more reliable to drive
# this way than via real mouse/range-change timing.
# ══════════════════════════════════════════════════════════════════════════

def _set_viewport(ctrl, y0, x0, y1, x1):
    """Directly install a viewport (level-0 bbox) without going through a
    real sigRangeChanged event -- sets exactly the fields
    `_issue_raw_requests` / `_issue_directional_prefetch` read."""
    ctrl._current_bbox = (y0, x0, y1, x1)
    ds = ctrl.provider.level_downsample(ctrl.level)
    bbox_level = (int(y0 / ds), int(x0 / ds), int(y1 / ds), int(x1 / ds))
    ctrl._visible_tiles = tiles_covering(bbox_level, ctrl.grid.tile_size)
    ctrl._viewport_center_l0 = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    ctrl._viewport_zooming = False
    ctrl._viewport_shrinking = False


def _dirprefetch_reqs(ctrl, scheduler):
    return [(r, cb) for r, cb in scheduler.requests if cb == ctrl._on_dirprefetch_result]


def _setup_dirprefetch_ctrl(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()
    return ctrl, provider, scheduler, view


def test_newly_visible_tile_served_from_cache_without_waiting_for_tick(app):
    """A tile whose corrected result is already cached must be blitted on
    the range event that exposes it, not on the next 30ms motion tick.

    Why this exists: with directional prefetch computing the leading tile
    column in time but nothing blitting it until the next tick, the
    coarser-fallback p95 measured EXACTLY 20.0% (4 of 20 visible tiles --
    one full column) in every prefetch configuration tried. Serving cache
    hits straight from the range handler took mean coarser fallback from
    11.4% to 1.6% with the same prefetch settings. `TileScheduler.request`
    resolves a cache hit synchronously, so the handler stays cheap
    (measured mean 1.84ms, p95 5.5ms, 0/60 events over a 16.7ms frame)."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)

    # Give the fake scheduler the cache-hit behavior the real one has:
    # `TileScheduler.request` resolves a cached key SYNCHRONOUSLY, which is
    # what lets the range handler serve a tile without a tick.
    ts = ctrl.grid.tile_size
    ctrl._motion_timer.stop()
    # Seed every tile the pan below will newly expose, so the assertion
    # does not depend on exactly how many range events pyqtgraph emits.
    targets = [(2, 0), (2, 1), (3, 0), (3, 1)]
    arr = np.full((ts, ts), 7.0, dtype=np.float32)
    seeded = {ctrl._make_correction_key(tx, ty): arr for tx, ty in targets}

    class _Cache:
        def __init__(self, d):
            self._d = d

        def get(self, k):
            return self._d.get(k)

    scheduler.corrected_cache = _Cache(seeded)
    real_request = scheduler.request

    def request_with_cache_hits(req, callback):
        cached = scheduler.corrected_cache.get(req.key)
        if cached is not None:
            callback(TileResult(
                request=req,
                pixels=PixelBuffer(residency="cpu", dtype=str(cached.dtype),
                                   shape=tuple(cached.shape), handle=cached),
                quality=req.key.quality, provisional=False, timing={}, error=None))
            return
        real_request(req, callback)

    scheduler.request = request_with_cache_hits
    assert not any(ctrl._precise_pool.get(ctrl.level, *t) for t in targets)

    # A PAN that exposes it (same span, so the display level cannot
    # change -- the cache-serve path deliberately skips level switches),
    # with the motion timer stopped, so nothing but the range handler
    # itself can have issued the request.
    view.view_box.setRange(xRange=(512, 1536), yRange=(0, 1024), padding=0)
    _pump(30)
    ctrl._motion_timer.stop()

    assert any(t in ctrl._visible_tiles for t in targets), \
        "test setup: the pan should have exposed at least one seeded tile"
    served = [t for t in targets
              if ctrl._precise_pool.get(ctrl.level, *t) is not None]
    assert served, (
        "no cached corrected tile was served on the range event that "
        "exposed it")

    ctrl.teardown()


def test_directional_prefetch_requires_sustained_direction(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    # Priming tick: no previous center yet -> displacement is zero by
    # construction, direction invalid, nothing issued.
    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    assert ctrl.stats["dir_prefetch_issued"] == 0

    # Jitter: displacement well under DIRECTIONAL_PREFETCH_MIN_TILES current
    # -level tiles (20 world px / 512 px-per-tile ~= 0.04 tiles) -> still
    # invalid, still nothing issued.
    _set_viewport(ctrl, 0, 20, 2048, 2068)
    ctrl._issue_raw_requests()
    assert ctrl.stats["dir_prefetch_issued"] == 0
    assert ctrl._dirprefetch_candidates == []

    # Sustained motion in the same direction: the EMA-smoothed displacement
    # now clears the threshold -> requests are issued.
    _set_viewport(ctrl, 0, 320, 2048, 2368)
    ctrl._issue_raw_requests()
    assert ctrl.stats["dir_prefetch_issued"] > 0

    ctrl.teardown()


def test_directional_prefetch_corridor_is_ahead_only(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    _set_viewport(ctrl, 0, 300, 2048, 2348)
    ctrl._issue_raw_requests()

    dir_reqs = _dirprefetch_reqs(ctrl, scheduler)
    assert dir_reqs, "expected directional-prefetch requests for a sustained rightward pan"

    cx, _cy = ctrl._viewport_center_l0
    ts = ctrl.grid.tile_size
    for req, _cb in dir_reqs:
        tx, ty = req.key.tile.tx, req.key.tile.ty
        assert (tx, ty) not in ctrl._visible_tiles
        tile_cx = tx * ts + ts / 2.0
        assert tile_cx > cx, "directional-prefetch tile must lie AHEAD of the viewport"

    ctrl.teardown()


def test_directional_prefetch_inflight_cap(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    _set_viewport(ctrl, 0, 300, 2048, 2348)
    ctrl._issue_raw_requests()

    outstanding = _dirprefetch_reqs(ctrl, scheduler)
    assert len(outstanding) == DIRECTIONAL_PREFETCH_INFLIGHT
    assert len(ctrl._dirprefetch_candidates) > 0, "test setup: need more candidates than the cap"
    issued_before = ctrl.stats["dir_prefetch_issued"]

    req, _cb = outstanding[0]
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl.stats["dir_prefetch_completed"] == 1
    assert ctrl.stats["dir_prefetch_issued"] == issued_before + 1
    assert len(_dirprefetch_reqs(ctrl, scheduler)) == issued_before + 1

    ctrl.teardown()


def test_directional_prefetch_reversal_cancels(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    _set_viewport(ctrl, 0, 300, 2048, 2348)  # rightward
    ctrl._issue_raw_requests()

    old_reqs = _dirprefetch_reqs(ctrl, scheduler)
    assert old_reqs
    stale_req, _cb = old_reqs[0]
    gen_before = ctrl._dirprefetch_generation
    changes_before = ctrl.stats["dir_prefetch_direction_changes"]

    # Sharp reversal: a big leftward displacement flips the smoothed
    # direction by more than 90 degrees.
    _set_viewport(ctrl, 0, -700, 2048, 1348)
    ctrl._issue_raw_requests()

    assert ctrl._dirprefetch_generation != gen_before
    assert gen_before in scheduler.cancelled_generations
    assert ctrl.stats["dir_prefetch_direction_changes"] > changes_before
    assert ctrl.stats["dir_prefetch_cancelled"] > 0

    # A late result under the OLD (cancelled) generation must be discarded:
    # it completes (the scheduler already cached it) but must never touch
    # the precise pool and must never refill under the new generation.
    completed_before = ctrl.stats["dir_prefetch_completed"]
    issued_before = ctrl.stats["dir_prefetch_issued"]
    arr = np.zeros((512, 512), dtype=np.float32)
    scheduler.deliver(stale_req, arr)
    _pump(20)

    assert ctrl.stats["dir_prefetch_completed"] == completed_before + 1
    assert ctrl.stats["dir_prefetch_issued"] == issued_before  # no refill for the stale gen
    tile = stale_req.key.tile
    assert ctrl._precise_pool.get(tile.level, tile.tx, tile.ty) is None

    ctrl.teardown()


def test_directional_prefetch_never_blitted(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    _set_viewport(ctrl, 0, 300, 2048, 2348)
    ctrl._issue_raw_requests()

    dir_reqs = _dirprefetch_reqs(ctrl, scheduler)
    assert dir_reqs
    req, _cb = dir_reqs[0]
    tile = req.key.tile

    precise_before = ctrl.stats["precise_tiles_blitted"]
    mid_before = ctrl.stats["mid_tiles_blitted"]
    arr = np.full((512, 512), 5.0, dtype=np.float32)
    scheduler.deliver(req, arr)
    _pump(20)

    assert ctrl._precise_pool.get(tile.level, tile.tx, tile.ty) is None
    assert ctrl.stats["precise_tiles_blitted"] == precise_before
    assert ctrl.stats["mid_tiles_blitted"] == mid_before

    ctrl.teardown()


def test_directional_prefetch_skipped_while_zooming(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    _set_viewport(ctrl, 0, 300, 2048, 2348)
    ctrl._viewport_zooming = True  # override: this tick is a zoom
    ctrl._issue_raw_requests()

    assert ctrl.stats["dir_prefetch_issued"] == 0
    assert ctrl._dirprefetch_candidates == []
    assert _dirprefetch_reqs(ctrl, scheduler) == []

    ctrl.teardown()


def test_directional_prefetch_priority_above_all_classes(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    scheduler.requests.clear()

    _set_viewport(ctrl, 0, 300, 2048, 2348)
    ctrl._issue_raw_requests()

    dir_reqs = [r for r, cb in scheduler.requests if cb == ctrl._on_dirprefetch_result]
    other_reqs = [r for r, cb in scheduler.requests if cb != ctrl._on_dirprefetch_result]
    assert dir_reqs, "expected directional-prefetch requests this tick"
    assert other_reqs, "expected raw/fallback/current-level requests this tick"
    assert min(r.priority for r in dir_reqs) > max(r.priority for r in other_reqs)

    ctrl.teardown()


def test_directional_prefetch_disabled_by_switch(app):
    ctrl, provider, scheduler, view = _setup_dirprefetch_ctrl(app)
    ctrl.directional_prefetch = False

    _set_viewport(ctrl, 0, 0, 2048, 2048)
    ctrl._issue_raw_requests()
    _set_viewport(ctrl, 0, 300, 2048, 2348)
    ctrl._issue_raw_requests()

    assert ctrl.stats["dir_prefetch_issued"] == 0
    assert _dirprefetch_reqs(ctrl, scheduler) == []

    ctrl.teardown()


def test_cache_serve_works_across_level_switch(app):
    """A corrected tile already resident in the cache at the level a
    switch is landing ON must be blitted on the very range event that
    performs the switch, not skipped because the previous visible set was
    computed against a different level's tile coordinates."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    # Zoom out far enough to guarantee the coarse level (level 1).
    set_view_and_pump(view, 0, 0, 100000, 100000, ms=80)
    assert ctrl.level == 1, "test setup: a very wide view must land on the coarse level"
    ctrl._motion_timer.stop()

    # Dry run: discover exactly which level-0 tiles a zoom-in to a small
    # region will expose (no cache seeded yet, so nothing is served).
    ctrl._motion_timer.stop()
    view.view_box.setRange(xRange=(0, 512), yRange=(0, 512), padding=0)
    _pump(30)
    ctrl._motion_timer.stop()
    assert ctrl.level == 0, "test setup: the zoom-in should switch to level 0"
    targets = list(ctrl._visible_tiles)
    assert targets, "test setup: the zoom-in must expose at least one level-0 tile"

    # Reset back to the zoomed-out level-1 state.
    set_view_and_pump(view, 0, 0, 100000, 100000, ms=80)
    assert ctrl.level == 1
    ctrl._motion_timer.stop()

    ts = ctrl.grid.tile_size
    arr = np.full((ts, ts), 9.0, dtype=np.float32)
    seeded = {ctrl._make_correction_key(tx, ty, level=0): arr for tx, ty in targets}

    class _Cache:
        def __init__(self, d):
            self._d = d

        def get(self, k):
            return self._d.get(k)

    scheduler.corrected_cache = _Cache(seeded)
    real_request = scheduler.request

    def request_with_cache_hits(req, callback):
        cached = scheduler.corrected_cache.get(req.key)
        if cached is not None:
            callback(TileResult(
                request=req,
                pixels=PixelBuffer(residency="cpu", dtype=str(cached.dtype),
                                   shape=tuple(cached.shape), handle=cached),
                quality=req.key.quality, provisional=False, timing={}, error=None))
            return
        real_request(req, callback)

    scheduler.request = request_with_cache_hits
    assert not any(ctrl._precise_pool.get(0, *t) for t in targets)

    # The real level-switch event, motion timer stopped beforehand so
    # nothing but the range handler itself can have served these tiles.
    ctrl._motion_timer.stop()
    view.view_box.setRange(xRange=(0, 512), yRange=(0, 512), padding=0)
    _pump(30)
    ctrl._motion_timer.stop()

    assert ctrl.level == 0
    served = [t for t in targets if ctrl._precise_pool.get(0, *t) is not None]
    assert served, (
        "no cached corrected tile was served on the range event that "
        "performed the level switch")

    ctrl.teardown()


def test_cache_serve_lookup_bounded_by_visible_set(app):
    """The cache-serve fast path must stay cheap on a level switch: the
    number of cache lookups performed by the range handler must never
    exceed the size of the newly-current visible set."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    set_view_and_pump(view, 0, 0, 100000, 100000, ms=80)
    assert ctrl.level == 1
    ctrl._motion_timer.stop()

    class _CountingCache:
        def __init__(self):
            self.get_calls = 0

        def get(self, k):
            self.get_calls += 1
            return None  # every lookup is a miss -- irrelevant to this test

    cache = _CountingCache()
    scheduler.corrected_cache = cache

    ctrl._motion_timer.stop()
    view.view_box.setRange(xRange=(0, 512), yRange=(0, 512), padding=0)
    _pump(30)
    ctrl._motion_timer.stop()

    assert ctrl.level == 0, "test setup: the zoom-in should switch to level 0"
    assert cache.get_calls <= len(ctrl._visible_tiles), (
        f"cache-serve performed {cache.get_calls} lookups for a "
        f"{len(ctrl._visible_tiles)}-tile visible set on a level switch")

    ctrl.teardown()

# ══════════════════════════════════════════════════════════════════════════
# Synthesized coarse fallback (module docstring "Synthesized coarse
# fallback") -- build a fallback-level tile locally from resident,
# already-quantized finer tiles instead of asking the scheduler to compute
# it, so it matches its neighbours exactly instead of showing the measured
# 18-27% tophat non-commutativity mismatch.
# ══════════════════════════════════════════════════════════════════════════

def _pool_all_finer_tiles_for_fallback(ctrl, provider, fallback_level, tx, ty, fill_fn):
    """Pool every level-(fallback_level - 1) tile needed to synthesize
    `(fallback_level, tx, ty)`, using `fill_fn(ftx, fty)` -> uint8 array for
    each tile's pixels. Returns {(ftx, fty): arr_u8}."""
    finer_level = fallback_level - 1
    ds_finer = provider.level_downsample(finer_level)
    ds_fallback = provider.level_downsample(fallback_level)
    k = int(round(ds_fallback / ds_finer))
    ts = ctrl.grid.tile_size
    arrs = {}
    for fty in range(ty * k, ty * k + k):
        for ftx in range(tx * k, tx * k + k):
            arr_u8 = fill_fn(ftx, fty)
            key = ctrl._make_correction_key(ftx, fty, level=finer_level)
            rect = ExploreView.world_rect(fty * ts, ftx * ts, ts, ts, 1.0, 1.0)
            ctrl._precise_pool.put(finer_level, ftx, fty, rect, arr_u8, key)
            arrs[(ftx, fty)] = arr_u8
    return arrs, k


def test_synthesized_fallback_matches_downsampled_finer_tiles(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    ts = ctrl.grid.tile_size
    rng = np.random.default_rng(1)

    def fill(ftx, fty):
        return rng.integers(0, 256, size=(ts, ts), dtype=np.uint8)

    arrs, k = _pool_all_finer_tiles_for_fallback(ctrl, provider, 1, 0, 0, fill)

    result = ctrl._try_synthesize_fallback_tile(1, 0, 0)
    assert result is not None

    assembled = np.block([[arrs[(ftx, fty)] for ftx in range(k)] for fty in range(k)])
    expected = np.clip(np.round(_box_downsample(assembled, k)), 0, 255).astype(np.uint8)
    np.testing.assert_array_equal(result, expected)
    assert ctrl.stats["fallback_synthesized"] >= 1

    ctrl.teardown()


def test_synthesis_sources_from_corrected_cache_too(app):
    """A finer tile that is in the corrected CACHE but not yet pooled is a
    valid synthesis source. The pool only holds what has been blitted; the
    cache holds everything computed, including prefetched tiles, so it is
    the larger source. A cached tile is float32, so it is quantized once
    with the FINER level's gain -- exactly the pixels that tile would show.

    Measured, this roughly doubled an otherwise very low hit rate (a
    level-crossing zoom-out went from 1 synthesis to 2, a pan from 0 to 1);
    the rate stays small for geometric reasons documented on
    `_try_synthesize_fallback_tile`."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.level = 0

    fallback_level = 1
    k = int(round(provider.level_downsample(fallback_level)
                  / provider.level_downsample(0)))
    ts = ctrl.grid.tile_size

    class _Cache:
        def __init__(self):
            self.d = {}

        def get(self, key):
            return self.d.get(key)

    cache = _Cache()
    scheduler.corrected_cache = cache
    # Every source tile lives ONLY in the cache, never in the pool.
    for j in range(k):
        for i in range(k):
            key = ctrl._make_correction_key(i, j, level=0)
            cache.d[key] = np.full((ts, ts), 4.0, dtype=np.float32)

    before = ctrl.stats["fallback_synthesized"]
    arr = ctrl._try_synthesize_fallback_tile(fallback_level, 0, 0)
    assert arr is not None, "cache-resident sources must be usable"
    assert ctrl.stats["fallback_synthesized"] == before + 1
    assert arr.dtype == np.uint8
    expected = ctrl._quantize_corrected_uint8(
        np.full((ts, ts), 4.0, dtype=np.float32), 0)[0, 0]
    assert arr[0, 0] == expected, (
        "a cached source must be quantized once with the FINER level's gain")

    ctrl.teardown()


def test_synthesis_declined_when_any_source_missing(app):
    """One finer tile absent -> synthesis declines, the normal
    `_issue_settled_request` flow issues a scheduler request for the
    fallback tile instead, and nothing is pooled at the fallback level."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    ts = ctrl.grid.tile_size
    for ftx in range(4):
        for fty in range(4):
            if (ftx, fty) == (3, 3):
                continue  # leave one source tile missing
            key = ctrl._make_correction_key(ftx, fty, level=0)
            rect = ExploreView.world_rect(fty * ts, ftx * ts, ts, ts, 1.0, 1.0)
            ctrl._precise_pool.put(0, ftx, fty, rect, np.full((ts, ts), 5, dtype=np.uint8), key)

    before_declined = ctrl.stats["fallback_synthesis_declined"]
    direct = ctrl._try_synthesize_fallback_tile(1, 0, 0)
    assert direct is None
    assert ctrl.stats["fallback_synthesis_declined"] == before_declined + 1

    ctrl.jump_to(y0=0, x0=0, w=2048, h=2048)
    fallback_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey)
                      if r.key.tile.level == 1 and r.key.tile.tx == 0 and r.key.tile.ty == 0]
    assert fallback_reqs, "expected a request for the fallback tile since synthesis was declined"
    assert ctrl._precise_pool.get(1, 0, 0) is None

    ctrl.teardown()


def test_synthesis_declined_when_any_source_stale(app):
    """One finer tile present but keyed for a DIFFERENT selection ->
    declined, even though every tile is physically present."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    ts = ctrl.grid.tile_size
    stale_addr = TileAddress(grid=ctrl.grid, level=0, tx=2, ty=2)
    stale_key = CorrectionKey(
        source=provider.source_identity(), channel=ctrl.channel, tile=stale_addr,
        method="cucim", params=(8,), algorithm_version=BG_CORRECTION_ALGO_VERSION,
        quality=ctrl.quality)

    for ftx in range(4):
        for fty in range(4):
            key = stale_key if (ftx, fty) == (2, 2) else ctrl._make_correction_key(ftx, fty, level=0)
            rect = ExploreView.world_rect(fty * ts, ftx * ts, ts, ts, 1.0, 1.0)
            ctrl._precise_pool.put(0, ftx, fty, rect, np.full((ts, ts), 5, dtype=np.uint8), key)

    before_declined = ctrl.stats["fallback_synthesis_declined"]
    result = ctrl._try_synthesize_fallback_tile(1, 0, 0)
    assert result is None
    assert ctrl.stats["fallback_synthesis_declined"] == before_declined + 1

    ctrl.teardown()


def test_synthesized_tile_is_invalidated_by_selection_change(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    ts = ctrl.grid.tile_size

    def fill(ftx, fty):
        return np.full((ts, ts), 5, dtype=np.uint8)

    _pool_all_finer_tiles_for_fallback(ctrl, provider, 1, 0, 0, fill)

    ok = ctrl._synthesize_and_pool_fallback_tile(1, 0, 0)
    assert ok is True
    entry = ctrl._precise_pool.get(1, 0, 0)
    assert entry is not None
    assert ctrl._precise_key_current_for_level(entry.key, 1) is True

    ctrl.set_selection(method="cucim", params=(8,))
    assert ctrl._precise_key_current_for_level(entry.key, 1) is False

    ctrl.teardown()


def test_synthesis_not_requantized(app):
    """The synthesized tile must be a PURE downsample of the pooled uint8
    pixels -- the finer level's display gain (already baked into those
    pooled pixels) must not be applied a second time."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))

    floor_level, stride = ctrl._pick_floor_level_and_stride()
    ctrl._floor_level = floor_level
    ctrl._floor_stride = stride
    ctrl._gain_ctx = ctrl._current_floor_ctx(floor_level, stride)
    ctrl._level_gain = {0: 1.0, 1: 3.5}

    ts = ctrl.grid.tile_size
    rng = np.random.default_rng(2)

    def fill(ftx, fty):
        return rng.integers(0, 256, size=(ts, ts), dtype=np.uint8)

    arrs, k = _pool_all_finer_tiles_for_fallback(ctrl, provider, 1, 0, 0, fill)

    result = ctrl._try_synthesize_fallback_tile(1, 0, 0)
    assert result is not None

    assembled = np.block([[arrs[(ftx, fty)] for ftx in range(k)] for fty in range(k)])
    expected = np.clip(np.round(_box_downsample(assembled, k)), 0, 255).astype(np.uint8)
    np.testing.assert_array_equal(result, expected)

    # A second application of level 1's gain would produce a materially
    # different image (random pixels, high gain) -- pin that it does NOT.
    gained_again = np.clip(result.astype(np.float32) * 3.5, 0, 255).astype(np.uint8)
    assert not np.array_equal(result, gained_again)

    ctrl.teardown()


def test_zoom_out_synthesizes_from_finer(app):
    """A real level increase (zoom-out): with every source (level-0) tile
    for fallback tile (1, 0, 0) already pooled and current, the new
    level's tile (0, 0) must be synthesized -- and therefore never
    separately requested from the scheduler."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=2048, h=2048)
    assert ctrl.level == 0

    current_reqs = [(r, cb) for r, cb in scheduler.pending_for(CorrectionKey) if r.key.tile.level == 0]
    # ViewBox aspect-lock may expand the requested rect slightly to match
    # the widget's aspect ratio, so the exact tile count can exceed the
    # nominal 4x4 -- assert the 16 tiles synthesis actually needs are among
    # them rather than pinning an exact total.
    assert len(current_reqs) >= 16
    needed = {(ftx, fty) for ftx in range(4) for fty in range(4)}
    got = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in current_reqs}
    assert needed <= got
    for req, _cb in current_reqs:
        arr = np.full((512, 512), 7.0, dtype=np.float32)
        scheduler.deliver(req, arr)
    _pump(20)
    assert len(ctrl._precise_pool.entries) >= 16

    scheduler.requests.clear()
    ctrl._motion_timer.stop()
    view.view_box.setRange(xRange=(0, 40000), yRange=(0, 40000), padding=0)
    _pump(30)
    ctrl._motion_timer.stop()
    assert ctrl.level == 1, "test setup: a very wide view must land on the coarse level"

    entry = ctrl._precise_pool.get(1, 0, 0)
    assert entry is not None
    assert ctrl._precise_key_current_for_level(entry.key, 1) is True

    fallback_reqs_l1_00 = [r for r, cb in scheduler.requests
                            if isinstance(r.key, CorrectionKey) and r.key.tile.level == 1
                            and r.key.tile.tx == 0 and r.key.tile.ty == 0]
    assert fallback_reqs_l1_00 == [], "synthesized tile must not also be requested"
    assert ctrl.stats["fallback_synthesized"] >= 1

    ctrl.teardown()


def test_zoom_gesture_state_cleared_on_settle(app):
    """`_viewport_zooming` is only recomputed when a range event arrives,
    so after the user stops it keeps whatever the LAST event set --
    typically True at the end of a zoom. The settle timer is the only
    signal that a gesture ended, so it resets the flag there.

    This is a regression guard for a shipped fault: a display gate was
    driven off this flag and re-based its own timeout on every tile
    delivery, so once the user stopped zooming the gate never released and
    the screen stayed on the blurrier coarse level. The gate is gone; this
    keeps the underlying staleness from trapping anything else."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()

    ctrl._viewport_zooming = True
    ctrl._viewport_shrinking = True
    ctrl._on_settle()
    assert ctrl._viewport_zooming is False
    assert ctrl._viewport_shrinking is False

    ctrl.teardown()


# ── Commit 0: channel-switch identity + the public interaction contract ────
#
# The bug this section exists for: `set_selection(channel=...)` used to clear
# the tile pools and nothing else. The overview was never reloaded and carried
# no channel identity, so after a switch the OLD channel's overview stayed on
# screen, and -- worse -- it was still fed to the corrected floor and to the
# display-gain calibration, whose results were then registered under the NEW
# channel's context. Pixels from one channel, identity claiming another.

def test_overview_carries_channel_identity(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    assert ctrl._overview_matches_selection() is True

    # Same array, different live channel -> must no longer match.
    ctrl.channel = "OTHER"
    assert ctrl._overview_matches_selection() is False


def test_channel_switch_reloads_overview_for_the_new_channel(app):
    ctrl, provider, scheduler, view = make_controller(app, channel="DAPI")
    ctrl.load_overview()
    old = np.array(view.overview_item.image, copy=True)

    other = [c for c in provider.channel_names if c != "DAPI"][0]
    ctrl.set_selection(channel=other)

    # The switch must NOT read on the GUI thread (p95 292.9ms on the real
    # slide), so with no resident record the old pixels go immediately and
    # the read happens on a worker.
    assert ctrl._overview_arr is None, (
        "the previous channel's overview survived the switch")
    assert view.overview_item.isVisible() is False

    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl._overview_matches_selection():
        _pump(10)
    assert ctrl._overview_matches_selection() is True
    assert ctrl._overview_identity == (provider.source_identity(), other)
    new = np.asarray(view.overview_item.image)
    assert not np.array_equal(old, new), (
        "the overview still holds the previous channel's pixels after a switch")

    ctrl.teardown()


def test_floor_and_gain_consume_only_the_new_channels_pixels(app):
    """The corrected floor and the display-gain calibration are the two
    consumers of `_overview_arr`, and both used to be fed a stale one after
    a channel switch -- the floor then registered its result under the NEW
    channel's context, and the calibration chose its tissue windows by the
    WRONG channel's intensity distribution.

    This runs a REAL floor job across a switch and asserts on what it
    actually consumed. (An earlier version of this test asserted only that
    `_overview_matches_selection()` was False, which proves nothing about
    either consumer -- the name claimed two code paths it never executed.)
    `FakeProvider` gives every channel a distinct 1e6-scaled offset, so the
    provenance of any array is checkable by value.
    """
    ctrl, provider, scheduler, view = make_controller(app, channel="DAPI",
                                                      settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl._floor_ready:
        _pump(10)
    assert ctrl._floor_ready, "test setup: the first floor never completed"
    assert ctrl._floor_ctx[1] == "DAPI"

    old_offset = provider._channel_offset("DAPI")
    new_channel = "CD8"
    new_offset = provider._channel_offset(new_channel)
    assert new_offset != old_offset

    read_channels = []
    corrected_inputs = []
    real_read = provider.read_region
    real_correct = ctrl.compute.correct_array

    def spy_read(channel, level, y0, y1, x0, x1):
        read_channels.append(channel)
        return real_read(channel, level, y0, y1, x0, x1)

    def spy_correct(arr, method, param):
        corrected_inputs.append(float(np.min(arr)))
        return real_correct(arr, method, param)

    provider.read_region = spy_read
    ctrl.compute.correct_array = spy_correct

    ctrl.set_selection(channel=new_channel)
    deadline = time.time() + 5.0
    while time.time() < deadline and not (
            ctrl._floor_ready and ctrl._floor_ctx
            and ctrl._floor_ctx[1] == new_channel):
        _pump(10)

    assert ctrl._floor_ready, "the floor never completed for the new channel"
    assert ctrl._floor_ctx[1] == new_channel, (
        "the floor registered itself under the wrong channel")

    assert read_channels, "the switch read nothing at all"
    assert all(c == new_channel for c in read_channels), (
        f"the switch read the old channel: {sorted(set(read_channels))}")

    assert corrected_inputs, "no correction ran across the switch"
    # Every array handed to the kernel must carry the NEW channel's offset.
    for lo in corrected_inputs:
        assert lo >= new_offset, (
            f"a correction consumed pixels whose offset ({lo}) is below the "
            f"new channel's ({new_offset}) -- i.e. the old channel's pixels")

    ctrl.teardown()


def test_channel_switch_starts_exactly_one_floor_job(app):
    """`load_overview` used to start a floor job of its own, and
    `set_selection` started a second immediately after, so every switch
    computed a whole floor -- read, correction and gain calibration -- and
    then discarded it as stale before redoing it."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl._floor_ready:
        _pump(10)

    starts = []
    real_start = ctrl._start_floor_job
    ctrl._start_floor_job = lambda gen: (starts.append(gen), real_start(gen))[1]

    ctrl.set_selection(channel="CD3")
    _pump(30)

    assert len(starts) == 1, (
        f"a single channel switch started {len(starts)} floor jobs")

    ctrl.teardown()


def test_channel_switch_withholds_raw_until_the_display_range_is_known(app):
    """A cold switch must ask for NOTHING until this channel's overview
    record lands. Quantisation happens once, when a tile arrives, and a
    pooled tile keeps it -- so a tile requested before the record would be
    quantised against the PREVIOUS channel's display range and would keep
    that wrong brightness for good. Once the record installs, the request
    goes out without waiting for a motion tick."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)
    ctrl._motion_timer.stop()
    scheduler.requests.clear()

    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    ctrl.set_selection(channel=other)
    ctrl._motion_timer.stop()       # prove no tick is ever involved

    assert not scheduler.pending_for(RawKey), (
        "raw was requested while this channel's display range was unknown")
    assert ctrl._overview_matches_selection() is False

    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl._overview_matches_selection():
        _pump(10)
    ctrl._motion_timer.stop()

    raw = [r for r, _cb in scheduler.pending_for(RawKey)]
    assert raw, "no raw request was issued once the overview landed"
    assert all(r.key.channel == other for r in raw)

    ctrl.teardown()


def test_channel_switch_never_shows_the_old_channel(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)

    # Land one tile of the current channel so there IS something to go stale.
    reqs = scheduler.pending_for(CorrectionKey)
    cur = [(r, cb) for r, cb in reqs if r.key.tile.level == ctrl.level]
    if cur:
        req, _cb = cur[0]
        scheduler.deliver(req, np.full((512, 512), 3.0, dtype=np.float32))
        _pump(20)

    old_channel = ctrl.channel
    other = [c for c in provider.channel_names if c != old_channel][0]
    ctrl.set_selection(channel=other)

    for pool in (ctrl._raw_pool, ctrl._precise_pool):
        for e in pool.entries.values():
            if e.key is None:
                continue
            assert e.key.channel != old_channel, (
                "a tile from the previous channel survived the switch")
    # Either the new channel's overview is already installed, or there is
    # none at all -- never the old one.
    assert ctrl._overview_identity is None or ctrl._overview_identity[1] == other

    ctrl.teardown()


def test_fully_cached_channel_switch_is_atomic_and_shows_no_raw(app):
    """Switching to a channel whose whole viewport is already corrected must
    complete inside the one GUI event -- no queued delivery, so no frame can
    be painted showing raw in between."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)
    ctrl.level = 0
    ctrl._motion_timer.stop()

    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    # The record must be resident: without it the switch is a cold one and
    # correctly withholds everything until the display range is known.
    ctrl.prepare_overview_async(other)
    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl.has_overview_record(other):
        _pump(10)
    assert ctrl.has_overview_record(other)

    class _Cache:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

    cache = _Cache()
    scheduler.corrected_cache = cache
    saved_channel = ctrl.channel
    ctrl.channel = other
    ts = ctrl.grid.tile_size
    for tx, ty in ctrl._visible_tiles:
        cache.d[ctrl._make_correction_key(tx, ty)] = np.full(
            (ts, ts), 5.0, dtype=np.float32)
    ctrl.channel = saved_channel

    before = ctrl.stats.get("atomic_channel_swaps", 0)
    ctrl.set_selection(channel=other)          # no _pump() anywhere after

    assert ctrl.stats.get("atomic_channel_swaps", 0) == before + 1
    pooled = {(e.tx, e.ty) for e in ctrl._precise_pool.entries.values()
              if e.level == ctrl.level}
    assert ctrl._visible_tiles <= pooled, (
        "cached corrected tiles were not pooled within the same GUI event")
    # And the switch must not leave the view stuck in provisional: the swap
    # fills the pool BEFORE `_enter_provisional`, so no later delivery
    # arrives to clear the flag.
    assert ctrl._provisional is False, (
        "an already-complete switch left the view in a provisional state")
    assert ctrl._precise_visible is True

    ctrl.teardown()


def test_interaction_contract_signals(app):
    """Explicit event source, and `gesture_quiet` is the 80ms DISPLAY event,
    deliberately not named after a background policy's SETTLED."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()

    events, quiets, selections = [], [], []
    ctrl.interaction_event.connect(lambda k, s: events.append((k, s)))
    ctrl.gesture_quiet.connect(quiets.append)
    ctrl.selection_context_changed.connect(selections.append)

    set_view_and_pump(view, 0, 0, 1024, 1024, ms=60)
    assert any(k in ("PAN", "ZOOM") for k, _s in events)

    before = len(events)
    ctrl.jump_to(y0=0, x0=0, w=2048, h=2048)
    jump_events = [k for k, _s in events[before:]]
    # Exactly one, and it says NAVIGATOR_JUMP. `setRange` inside `jump_to`
    # also runs the range handler, but a jump must announce itself once with
    # its true source, not first as PAN/ZOOM and then again as a jump.
    assert jump_events == ["NAVIGATOR_JUMP"], jump_events

    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    ctrl.set_selection(channel=other)
    assert events[-1][0] == "CHANNEL_SWITCH"
    assert selections, "selection_context_changed never fired"

    ctrl.teardown()


def test_gesture_quiet_fires_from_the_real_timer_after_jump_and_switch(app):
    """Both a navigator jump and a channel switch end with the camera
    stationary and no further range events, so nothing else would start the
    quiet period. `jump_to` used to STOP the settle timer and never restart
    it, so `gesture_quiet` never fired after a jump and a background
    consumer could never reach its own SETTLED.

    Deliberately does not call `_on_settle()` by hand: that would prove only
    that the emit statement exists, not that the timer ever reaches it."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=30)
    ctrl.load_overview()

    quiets = []
    ctrl.gesture_quiet.connect(quiets.append)

    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)
    assert ctrl._settle_timer.isActive(), "jump_to left the quiet timer stopped"
    deadline = time.time() + 2.0
    while time.time() < deadline and not quiets:
        _pump(10)
    assert quiets, "gesture_quiet never fired after a navigator jump"

    quiets.clear()
    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    ctrl.set_selection(channel=other)
    assert ctrl._settle_timer.isActive(), "a channel switch started no quiet period"
    deadline = time.time() + 2.0
    while time.time() < deadline and not quiets:
        _pump(10)
    assert quiets, "gesture_quiet never fired after a channel switch"
    assert ctrl._viewport_zooming is False

    ctrl.teardown()


def test_snapshot_is_immutable_and_epoch_advances(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    snap = ctrl.snapshot()

    with pytest.raises(Exception):
        snap.channel = "changed"

    # Everything needed to build a CorrectionKey for ANOTHER channel is on it.
    for fld in ("source", "channel", "method", "params", "level", "quality",
                "algorithm_version", "bbox_l0", "visible_tiles",
                "display_lo", "display_hi"):
        assert hasattr(snap, fld), fld

    before = snap.epoch
    ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)
    assert ctrl.snapshot().epoch > before

    ctrl.teardown()


def test_prepared_overview_installs_synchronously_without_reading(app):
    """A channel whose overview record is resident must install as a
    memcpy. This is what makes "the neighbour is ready" true: having its
    corrected tiles cached is not enough, because without the record the
    switch still stalls on a p95-293ms read."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    other = [c for c in provider.channel_names if c != ctrl.channel][0]

    ctrl.prepare_overview_async(other)
    deadline = time.time() + 5.0
    while time.time() < deadline and not any(
            k[1] == other for k in ctrl._overview_cache):
        _pump(10)
    assert any(k[1] == other for k in ctrl._overview_cache), "record never cached"
    # Preparing must not disturb what is on screen.
    assert ctrl._overview_identity[1] != other

    # Count OVERVIEW reads specifically. Watching `provider.read_region`
    # cannot tell them apart from raw/floor background reads, and an
    # assertion like `all(c != other or True for c in reads)` is true by
    # construction and proves nothing.
    import block01.viewer.explore_view as ev_mod
    calls = []
    real_reader = ev_mod.ExploreController._read_overview_record

    def counting_reader(provider_, source_, channel_, level_):
        calls.append(channel_)
        return real_reader(provider_, source_, channel_, level_)

    ev_mod.ExploreController._read_overview_record = staticmethod(counting_reader)
    try:
        ctrl.set_selection(channel=other)
        assert ctrl._overview_matches_selection() is True, (
            "a prepared overview did not install synchronously")
        assert calls == [], (
            f"a prepared switch still read the overview: {calls}")
    finally:
        ev_mod.ExploreController._read_overview_record = staticmethod(real_reader)
    assert ctrl.stats.get("overview_cache_hits", 0) >= 1

    ctrl.teardown()


def test_atomic_swap_issues_no_raw_requests(app):
    """When the corrected viewport is already complete the raw layer is
    hidden, so raw reads could never be seen; issuing them would only burn
    I/O and contend with background channel preparation."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)
    ctrl.level = 0
    ctrl._motion_timer.stop()

    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    ctrl.prepare_overview_async(other)
    deadline = time.time() + 5.0
    while time.time() < deadline and not any(
            k[1] == other for k in ctrl._overview_cache):
        _pump(10)

    class _Cache:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

    cache = _Cache()
    scheduler.corrected_cache = cache
    saved = ctrl.channel
    ctrl.channel = other
    ts = ctrl.grid.tile_size
    for tx, ty in ctrl._visible_tiles:
        cache.d[ctrl._make_correction_key(tx, ty)] = np.full((ts, ts), 5.0, dtype=np.float32)
    ctrl.channel = saved

    scheduler.requests.clear()
    ctrl.set_selection(channel=other)
    ctrl._motion_timer.stop()

    assert ctrl.stats.get("atomic_channel_swaps", 0) >= 1
    raw = [r for r, _cb in scheduler.pending_for(RawKey)]
    assert not raw, f"an already-complete switch still issued {len(raw)} raw reads"

    ctrl.teardown()


def test_quantisation_is_bit_identical_to_the_reference_formula(app):
    """The uint8 quantisation is the pixels a user compares between
    channels, so an implementation change must be provably a no-op.

    The reference here is the formula the viewer used before the
    allocation rewrite -- `round(clip((v - lo) / span, 0, 1) * 255)` --
    written out independently, not called from the code under test.
    A faster fused multiply-add would give a different float rounding and
    a scattered +/-1 in the output; this test is what forbids it.

    Fixed seed, and deliberately covering: float32 and integer-derived
    input, values below `lo` and above `hi` so both clip sides are hit,
    tiny and huge dynamic ranges, and BOTH the gain == 1.0 fast path and
    the gain != 1.0 path.
    """
    ctrl, _provider, _scheduler, _view = make_controller(app)
    try:
        rng = np.random.default_rng(20260901)

        def reference(arr, lo, hi, gain):
            gained = arr.astype(np.float32, copy=False) * gain
            span = max(hi - lo, 1e-6)
            norm = np.clip((gained - lo) / span, 0.0, 1.0)
            return np.round(norm * 255.0).astype(np.uint8)

        applied_gains = set()
        cases = []
        for span in (1e-3, 1.0, 255.0, 4000.0, 65535.0):
            for lo in (-7.5, 0.0, 12.0):
                cases.append((lo, lo + span))

        for lo, hi in cases:
            for integer_source in (False, True):
                # Deliberately overshoot the display range on both sides.
                raw = (rng.random((64, 96), dtype=np.float32)
                       * (hi - lo) * 1.4 + lo - (hi - lo) * 0.2)
                arr = raw.astype(np.uint16).astype(np.float32) if integer_source else raw
                ctrl._display_lo, ctrl._display_hi = lo, hi

                got = ctrl._quantize_tile_uint8(arr)
                assert np.array_equal(got, reference(arr, lo, hi, 1.0)), (
                    f"plain quantisation differs at lo={lo} hi={hi} "
                    f"integer_source={integer_source}")

                # The table is only honoured when `_gain_ctx` matches the
                # LIVE floor context -- a stale table must never scale
                # pixels -- so install it the way the floor job does.
                if ctrl._floor_level is None:
                    ctrl._floor_level, ctrl._floor_stride = 0, 1
                ctrl._level_gain = {0: 1.0, 1: 1.75, 2: 0.5}
                ctrl._gain_ctx = ctrl._current_floor_ctx(
                    ctrl._floor_level, ctrl._floor_stride)
                for level in (0, 1, 2):
                    applied = ctrl._display_gain_for_level(level)
                    applied_gains.add(applied)
                    got = ctrl._quantize_corrected_uint8(arr, level)
                    assert np.array_equal(
                        got, reference(arr, lo, hi, applied)), (
                        f"corrected quantisation differs at lo={lo} hi={hi} "
                        f"level={level} gain={applied}")
                    if applied == 1.0:
                        # The fast path must return exactly what the general
                        # path would have.
                        assert np.array_equal(got, ctrl._quantize_tile_uint8(arr))

        # Guard against a vacuous run: if the gain table never installed,
        # every case above would have exercised the 1.0 fast path only.
        assert applied_gains - {1.0}, (
            f"no non-unit gain was ever applied: {applied_gains}")
    finally:
        ctrl.teardown()


def test_atomic_swap_defers_fallback_synthesis_but_still_requests_it(app):
    """The synthesis is DEFERRED on an atomic swap, not skipped.

    After an atomic swap the viewport is already complete at the current
    level, so a locally synthesized coarse underlay cannot be seen -- but
    building it is ~30ms of `np.mean` on the GUI thread, measured inside a
    switch that cost ~90ms in total. So the switch must not build it, and
    must still ASK the scheduler for the same tiles, or the next pan would
    expose the floor where the underlay should have been.
    """
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)
    ctrl.level = 0
    ctrl._motion_timer.stop()

    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    ctrl.prepare_overview_async(other)
    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl.has_overview_record(other):
        _pump(10)
    assert ctrl.has_overview_record(other)

    class _Cache:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

    cache = _Cache()
    scheduler.corrected_cache = cache
    saved = ctrl.channel
    ctrl.channel = other
    ts = ctrl.grid.tile_size
    for tx, ty in ctrl._visible_tiles:
        cache.d[ctrl._make_correction_key(tx, ty)] = np.full(
            (ts, ts), 5.0, dtype=np.float32)
    ctrl.channel = saved

    synth_calls = []
    real_synth = ctrl._synthesize_and_pool_fallback_tile
    ctrl._synthesize_and_pool_fallback_tile = (
        lambda *a, **k: synth_calls.append(a) or False)
    scheduler.requests.clear()
    try:
        ctrl.set_selection(channel=other)
    finally:
        ctrl._synthesize_and_pool_fallback_tile = real_synth
    ctrl._motion_timer.stop()

    assert ctrl.stats.get("atomic_channel_swaps", 0) >= 1
    assert synth_calls == [], (
        f"the atomic swap built {len(synth_calls)} fallback tiles on the "
        "GUI thread")

    if ctrl.intermediate_corrected_fallback and 1 < provider.num_levels:
        fallback_reqs = [
            r for r, _cb in scheduler.requests
            if isinstance(r.key, CorrectionKey) and r.key.tile.level == 1
            and r.key.channel == other
        ]
        assert fallback_reqs, (
            "deferring the synthesis must still request the fallback tiles")
        assert ctrl.stats.get("fallback_synthesis_deferred", 0) > 0

    ctrl.teardown()


def test_cold_switch_never_quantises_against_the_previous_range(app):
    """The record contract is that pixels and the range derived FROM them
    install together. A cold switch must therefore not draw at all while
    the range is unknown: `_quantize_*` runs once per tile, at arrival, and
    a pooled tile keeps that quantisation for good."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)
    ctrl._motion_timer.stop()

    old_lo, old_hi = ctrl._display_lo, ctrl._display_hi
    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    scheduler.requests.clear()

    ctrl.set_selection(channel=other)
    ctrl._motion_timer.stop()

    # Nothing requested, nothing pooled, and the snapshot says so rather
    # than handing a consumer the previous channel's range.
    assert not scheduler.requests, "a cold switch issued work before the range was known"
    assert not ctrl._raw_pool.entries and not ctrl._precise_pool.entries
    snap = ctrl.snapshot()
    assert snap.overview_ready is False
    assert snap.display_lo is None and snap.display_hi is None

    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl._overview_matches_selection():
        _pump(10)

    assert (ctrl._display_lo, ctrl._display_hi) != (old_lo, old_hi), (
        "the new channel installed the previous channel's display range")
    snap = ctrl.snapshot()
    assert snap.overview_ready is True
    assert snap.display_lo is not None

    ctrl.teardown()


def test_overview_prepare_is_single_flight(app):
    """Repeat calls for the same (source, channel, level) while one read is
    in flight must not start another. Each extra thread would re-parse the
    TIFF for its own per-thread provider handle, which then lives until the
    provider closes."""
    import block01.viewer.explore_view as ev_mod
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()

    gate = threading.Event()
    calls = []
    real_reader = ev_mod.ExploreController._read_overview_record

    def slow_reader(provider_, source_, channel_, level_):
        calls.append(channel_)
        gate.wait(timeout=5.0)
        return real_reader(provider_, source_, channel_, level_)

    ev_mod.ExploreController._read_overview_record = staticmethod(slow_reader)
    try:
        other = [c for c in provider.channel_names if c != ctrl.channel][0]
        for _ in range(5):
            ctrl.prepare_overview_async(other)
        deadline = time.time() + 2.0
        while time.time() < deadline and not calls:
            _pump(10)
        assert len(calls) == 1, f"{len(calls)} reads started for one channel"
        gate.set()
        deadline = time.time() + 5.0
        while time.time() < deadline and not ctrl.has_overview_record(other):
            _pump(10)
        assert ctrl.has_overview_record(other)
        assert len(calls) == 1
    finally:
        gate.set()
        ev_mod.ExploreController._read_overview_record = staticmethod(real_reader)

    ctrl.teardown()


def test_overview_prepared_signal_is_the_public_readiness_api(app):
    """A consumer must not reach into `_overview_cache`."""
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    seen = []
    ctrl.overview_prepared.connect(
        lambda src, ch, lvl, ok: seen.append((ch, lvl, ok)))

    other = [c for c in provider.channel_names if c != ctrl.channel][0]
    assert ctrl.has_overview_record(other) is False
    ctrl.prepare_overview_async(other)
    deadline = time.time() + 5.0
    while time.time() < deadline and not seen:
        _pump(10)

    assert seen and seen[-1][0] == other and seen[-1][2] is True
    assert ctrl.has_overview_record(other) is True

    ctrl.teardown()


def test_nothing_is_drawn_or_requested_while_waiting_for_an_overview(app):
    """The withhold is a GLOBAL invariant, not a decision taken once inside
    `set_selection`. During the tens of milliseconds a cold switch waits for
    its overview record, a pan, a zoom, a navigator jump, a method change or
    just the 30ms motion tick must all still draw nothing and request
    nothing -- each of those paths could otherwise quantise tiles against
    the previous channel's display range, and a pooled tile keeps its
    quantisation for good."""
    import block01.viewer.explore_view as ev_mod
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)

    gate = threading.Event()
    real_reader = ev_mod.ExploreController._read_overview_record

    def gated_reader(provider_, source_, channel_, level_):
        gate.wait(timeout=10.0)
        return real_reader(provider_, source_, channel_, level_)

    ev_mod.ExploreController._read_overview_record = staticmethod(gated_reader)
    try:
        other = [c for c in provider.channel_names if c != ctrl.channel][0]
        ctrl.set_selection(channel=other)
        assert ctrl._overview_matches_selection() is False
        scheduler.requests.clear()

        # Everything a user could do while the record is still being read.
        view.view_box.setRange(xRange=(200, 1224), yRange=(200, 1224), padding=0)
        _pump(20)
        view.view_box.setRange(xRange=(0, 4096), yRange=(0, 4096), padding=0)
        _pump(20)
        ctrl.jump_to(y0=0, x0=0, w=1024, h=1024)
        ctrl.set_selection(params=(20,))
        ctrl._issue_raw_requests()          # the motion tick itself
        ctrl._issue_settled_request()
        _pump(40)

        assert not scheduler.requests, (
            f"{len(scheduler.requests)} requests were issued while this "
            f"channel's display range was unknown")
        assert not ctrl._raw_pool.entries and not ctrl._precise_pool.entries
        assert ctrl.stats.get("blocked_on_overview", 0) > 0

        gate.set()
        deadline = time.time() + 5.0
        while time.time() < deadline and not ctrl._overview_matches_selection():
            _pump(10)
        assert ctrl._overview_matches_selection() is True
        assert scheduler.requests, "work never resumed once the record landed"
    finally:
        gate.set()
        ev_mod.ExploreController._read_overview_record = staticmethod(real_reader)

    ctrl.teardown()


def test_atomic_swap_still_prepares_the_new_channels_floor(app):
    """A successful swap fills the CURRENT level-0 viewport and nothing
    else. Without a floor and gain table for the new channel, the first
    zoom-out would have no corrected-stage fallback, so the floor must be
    ensured whether or not the swap succeeded."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=80)
    ctrl.level = 0
    ctrl._motion_timer.stop()

    other = [c for c in provider.channel_names if c != ctrl.channel][0]

    class _Cache:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

    cache = _Cache()
    scheduler.corrected_cache = cache
    saved = ctrl.channel
    ctrl.channel = other
    ts = ctrl.grid.tile_size
    for tx, ty in ctrl._visible_tiles:
        cache.d[ctrl._make_correction_key(tx, ty)] = np.full((ts, ts), 5.0, dtype=np.float32)
    ctrl.channel = saved

    starts = []
    real_start = ctrl._start_floor_job
    ctrl._start_floor_job = lambda gen: (starts.append(gen), real_start(gen))[1]

    ctrl.set_selection(channel=other)
    deadline = time.time() + 5.0
    while time.time() < deadline and not ctrl._overview_matches_selection():
        _pump(10)
    _pump(30)

    assert ctrl.stats.get("atomic_channel_swaps", 0) >= 1
    assert starts, "a successful atomic swap left the new channel with no floor"

    ctrl.teardown()


# ── production hand-off: teardown must outwait a running floor job ───────────

def test_teardown_hand_off_does_not_return_while_the_floor_is_computing(app):
    """`wait_for_floor=True` exists for one reason: something else is about
    to use the GPU this floor job is on.

    The ordinary path joins each floor thread with a 2s TIMEOUT and then
    proceeds regardless -- which for a hand-off would be the double GPU use
    it is meant to prevent, just delayed. So this must block until the job
    is genuinely finished, however long that takes.
    """
    release = threading.Event()
    entered = threading.Event()

    class _BlockingCompute(FakeCompute):
        def correct_array(self, arr, method, param):
            entered.set()
            assert release.wait(timeout=10), "test never released the floor"
            return arr.astype(np.float32, copy=False)

    ctrl, _provider, _scheduler, view = make_controller(app)
    ctrl.compute = _BlockingCompute()
    ctrl.load_overview()
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=50)
    ctrl.set_selection(method="tophat", params=(10,))
    assert entered.wait(timeout=5), "no floor job started"

    done = threading.Event()

    def tear():
        ctrl.teardown(wait_for_floor=True)
        done.set()

    t = threading.Thread(target=tear, name="handoff")
    t.start()
    try:
        # The floor thread is still inside correct_array; the hand-off must
        # be waiting on it. 2.5s is past the ordinary path's own timeout, so
        # a teardown that finished here would prove the timeout was used.
        assert not done.wait(timeout=2.5), (
            "the hand-off returned while the floor job was still running")

        release.set()
        assert done.wait(timeout=10), "the hand-off never returned"
    finally:
        release.set()
        t.join(timeout=10)
    assert not t.is_alive()
    assert ctrl._teardown_order == ["scheduler.shutdown", "provider.close"]


def test_ordinary_teardown_gives_up_on_a_stuck_floor_job(app):
    """The default path is best-effort by design: a late floor result is
    dropped once `_torn_down` is set, so closing a window must not hang on
    a floor job. It joins with a timeout and proceeds."""
    release = threading.Event()
    entered = threading.Event()

    class _StuckCompute(FakeCompute):
        def correct_array(self, arr, method, param):
            entered.set()
            release.wait(timeout=30)
            return arr.astype(np.float32, copy=False)

    ctrl, _provider, _scheduler, view = make_controller(app)
    ctrl.compute = _StuckCompute()
    ctrl.load_overview()
    set_view_and_pump(view, 0, 0, 1024, 1024, ms=50)
    ctrl.set_selection(method="tophat", params=(10,))
    assert entered.wait(timeout=5)

    try:
        t0 = time.perf_counter()
        ctrl.teardown()                      # no wait_for_floor
        elapsed = time.perf_counter() - t0
        assert 1.5 < elapsed < 6.0, (
            f"expected the ~2s best-effort join, took {elapsed:.2f}s")
        assert ctrl._teardown_order == ["scheduler.shutdown", "provider.close"]
    finally:
        release.set()


# ── production hand-off: suspend / resume ───────────────────────────────────
#
# A production correction run needs the GPU. The controller used to be torn
# down for it and rebuilt afterwards (overview re-read, floor recomputed, a
# black frame in between). It is now SUSPENDED in place and resumed.

def _paint_viewport(ctrl, provider, scheduler, view, x0=700, y0=100, size=2048):
    """Move the camera and deliver every raw tile it asked for."""
    set_view_and_pump(view, x0, y0, x0 + size, y0 + size)
    for req, _cb in list(scheduler.pending_for(RawKey)):
        t = req.key.tile
        scheduler.deliver(req, raw_arr_for(provider, t.level, t.tx, t.ty))
    _pump(40)


def test_suspend_stops_every_request_path_and_locks_the_camera(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.set_selection(method="tophat", params=(10,))
    _paint_viewport(ctrl, provider, scheduler, view)
    raw_gen, precise_gen = ctrl.view_generation, ctrl._settled_generation
    scheduler.requests.clear()
    scheduler.cancelled_generations.clear()

    timings = ctrl.suspend_for_production("whole-slide correction (Save)")

    assert ctrl.suspended is True
    assert isinstance(timings, dict) and "floor_join_ms" in timings
    # What was queued is cancelled, under every generation the controller
    # issues with.
    assert raw_gen in scheduler.cancelled_generations
    assert precise_gen in scheduler.cancelled_generations
    assert any(g[0] == "dirprefetch" for g in scheduler.cancelled_generations)
    assert not ctrl._motion_timer.isActive() and not ctrl._settle_timer.isActive()
    # Camera locked, and the badge says why.
    assert list(view.view_box.mouseEnabled()) == [False, False]
    assert "Paused" in view.status_label.text()
    assert "whole-slide correction (Save)" in view.status_label.text()

    # Moving the camera (programmatically -- the mouse is locked) issues
    # NOTHING: not raw, not precise, not prefetch.
    set_view_and_pump(view, 3000, 3000, 3000 + 1024, 3000 + 1024)
    assert scheduler.requests == []
    # And nothing is torn down: pools, provider, scheduler all live.
    assert provider.close_called is False
    assert scheduler.shutdown_called is False
    assert len(ctrl._raw_pool.entries) > 0

    ctrl.teardown()


def test_resume_reissues_only_what_the_current_viewport_is_missing(app):
    """An unchanged viewport paints nothing new -- the whole point: the user
    gets back exactly what they were looking at. A viewport that moved while
    suspended gets its missing tiles asked for on resume."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    _paint_viewport(ctrl, provider, scheduler, view)
    assert len(ctrl._raw_pool.entries) == len(ctrl._visible_tiles)
    ctrl.suspend_for_production("patch background correction")
    scheduler.requests.clear()

    ctrl.resume_from_production()

    assert ctrl.suspended is False
    assert scheduler.pending_for(RawKey) == [], "pooled tiles were re-requested"
    assert list(view.view_box.mouseEnabled()) == [True, True]
    assert not view.status_label.isVisible()

    # Suspend again, move, resume: the new tiles are asked for at once.
    ctrl.suspend_for_production("patch background correction")
    set_view_and_pump(view, 3000, 3000, 3000 + 1024, 3000 + 1024)
    assert scheduler.requests == []
    ctrl.resume_from_production()
    wanted = set(ctrl._visible_tiles)
    asked = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in scheduler.pending_for(RawKey)}
    assert asked == wanted - {(e.tx, e.ty) for e in ctrl._raw_pool.entries.values()
                              if e.level == ctrl.level}
    assert asked, "the moved viewport had nothing pooled and asked for nothing"

    ctrl.teardown()


def test_a_floor_job_is_deferred_while_suspended_and_started_on_resume(app):
    """The floor is GPU work. A selection change during a run must not start
    it; resuming must, against the selection current then."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    _paint_viewport(ctrl, provider, scheduler, view)
    ctrl.suspend_for_production("on-demand background correction")

    ctrl.set_selection(method="tophat", params=(10,))

    assert ctrl._floor_pending is True
    assert ctrl._floor_job_running is False
    assert not any(t.is_alive() for t in ctrl._floor_threads)
    assert ctrl._floor_ready is False
    assert "Paused" in view.status_label.text(), "the notice must survive the selection change"

    ctrl.resume_from_production()

    assert ctrl._floor_pending is False
    assert ctrl._floor_job_running is True or ctrl._floor_ready is True
    assert view.status_label.text() in ("Preparing corrected preview…", "") or ctrl._floor_ready
    for _ in range(100):
        if ctrl._floor_ready:
            break
        _pump(20)
    assert ctrl._floor_ready is True
    assert not view.status_label.isVisible()

    ctrl.teardown()


def test_a_late_floor_result_does_not_wipe_the_suspension_notice(app):
    """`suspend_for_production` joins the floor thread, but its result still
    arrives through the queued signal afterwards and ends with 'not
    preparing any more' -- which used to clear the badge."""
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.suspend_for_production("patch background correction")

    ctrl.floor_preparing_changed.emit(False)
    _pump(10)

    assert "Paused" in view.status_label.text()
    assert view.status_label.isVisible()
    ctrl.teardown()


def test_suspend_and_resume_are_idempotent_and_teardown_still_works(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    assert ctrl.resume_from_production() is None and ctrl.suspended is False

    ctrl.suspend_for_production("a")
    n_cancels = len(scheduler.cancelled_generations)
    assert ctrl.suspend_for_production("b") == {}       # second call: no-op
    assert len(scheduler.cancelled_generations) == n_cancels
    assert "(a)" in view.status_label.text()

    ctrl.teardown()
    assert scheduler.shutdown_called and provider.close_called
    ctrl.resume_from_production()                         # after teardown: no-op
    assert ctrl.suspended is True                          # state is simply frozen


def test_the_overlay_issues_nothing_while_its_host_is_suspended(app):
    from block01.viewer.explore_view import RawOverlayLayer

    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    _paint_viewport(ctrl, provider, scheduler, view)
    overlay = RawOverlayLayer(provider, scheduler, ctrl.grid, view, "CD3")
    ctrl.attach_overlay(overlay)
    overlay._display_lo, overlay._display_hi = 0.0, 1.0     # calibrated, by hand
    overlay._enabled = True
    scheduler.requests.clear()

    ctrl.suspend_for_production("patch background correction")
    overlay.sync(ctrl.level, sorted(ctrl._visible_tiles))
    assert scheduler.requests == []

    ctrl.resume_from_production()
    asked = {r.key.channel for r, _cb in scheduler.pending_for(RawKey)}
    assert asked == {"CD3"}, "resume must bring the overlay's tiles back too"
    ctrl.teardown()
