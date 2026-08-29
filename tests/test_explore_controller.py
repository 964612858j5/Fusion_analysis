"""Unit tests for viewer/explore_view.py (ExploreView + ExploreController).

Offscreen Qt (QT_QPA_PLATFORM=offscreen), following the existing viewer Qt
test style (see tests/test_channel_workbench.py / test_high_quality_image_
viewer.py: module-scope QApplication fixture, pytest.importorskip("PyQt5")).

Uses a FakeProvider (synthetic 2-level pyramid, ds=[1, 4], deterministic
ramp arrays) and a FakeScheduler that records every TileRequest/callback
pair and lets the test deliver results synchronously on demand -- no real
threads, so results are delivered directly via the controller's public
`_on_raw_result` / `_on_precise_result` slots (which internally marshal
through a QueuedConnection signal; `QtWidgets.QApplication.processEvents()`
drains the queue).
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets, QtTest  # noqa: E402

from block01.viewer.tile_types import (  # noqa: E402
    CorrectionKey,
    PixelBuffer,
    QualityLevel,
    RawKey,
    SourceIdentity,
    TileGridSpec,
    TileResult,
    effective_param,
    tiles_covering,
)
from block01.viewer.explore_view import ExploreController, ExploreView  # noqa: E402


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
    """Synthetic 2-level pyramid: level 0 is 4096x4096 (ds=1), level 1 is
    1024x1024 (ds=4). read_region returns a deterministic ramp so blitted
    positions are verifiable."""

    def __init__(self, closed_ok=True):
        self._shapes = {0: (4096, 4096), 1: (1024, 1024)}
        self._closed = False
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
        self._closed = True


class FakeScheduler:
    """Records every request; delivers on demand via `deliver(key, result)`
    or `deliver_all(pixel_fn)`. Tracks cancel_generation / shutdown calls."""

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


# ── helpers ──────────────────────────────────────────────────────────────────

def make_controller(app, settle_ms=30, channel="DAPI"):
    provider = FakeProvider()
    scheduler = FakeScheduler()
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
    arr, _off = provider.read_region("DAPI", level, y0, y0 + ts, x0, x0 + ts)
    return arr


# ── 1. overview loads full extent ───────────────────────────────────────────

def test_overview_loads_full_extent(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()

    rect = view.overview_item.sceneBoundingRect() if False else view.overview_item.boundingRect()
    # boundingRect is in item-local (pixel) coords; the world placement is
    # via setRect, verified through the item's mapped rect instead.
    item_rect = view.overview_item.getViewBox() and view.overview_item.viewRect() or None
    # Simpler + robust: verify the stored image shape matches the whole
    # chosen level, and the rect (via QGraphicsItem sceneTransform/mapRect)
    # covers the full level-0 extent.
    h, w = provider.level_shape(ctrl._overview_level)
    ds = provider.level_downsample(ctrl._overview_level)
    mapped = view.overview_item.mapRectToParent(view.overview_item.boundingRect())
    assert mapped.width() == pytest.approx(w * ds, rel=1e-6)
    assert mapped.height() == pytest.approx(h * ds, rel=1e-6)
    assert mapped.x() == pytest.approx(0.0, abs=1e-6)
    assert mapped.y() == pytest.approx(0.0, abs=1e-6)

    ctrl.teardown()


# ── 2. range change computes correct tile set + center-out priority ────────

def test_range_change_requests_missing_raw_tiles_center_out(app):
    ctrl, provider, scheduler, view = make_controller(app)
    ctrl.load_overview()
    ctrl.level = 0  # force level 0 for a deterministic tile-size expectation

    # Unaligned viewport in level-0 world coords.
    y0, x0, y1, x1 = 100, 700, 100 + 2048, 700 + 2048
    view.view_box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
    _pump(10)

    # pyqtgraph's aspect-locked ViewBox may pad the requested range to match
    # the widget's aspect ratio, so recompute the expected tile set from the
    # ACTUAL applied range (this is what the controller itself must do).
    (ax0, ax1), (ay0, ay1) = view.view_box.viewRange()
    y0, x0, y1, x1 = int(ay0), int(ax0), int(ay1), int(ax1)
    expected_tiles = tiles_covering((y0, x0, y1, x1), ctrl.grid.tile_size)
    raw_reqs = scheduler.pending_for(RawKey)
    requested_coords = {(r.key.tile.tx, r.key.tile.ty) for r, _cb in raw_reqs}
    assert requested_coords == expected_tiles

    # Center-out priority: sort by distance from viewport center.
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


# ── 3. settle timer: rapid range changes -> exactly one settled batch ──────

def test_settle_debounce_fires_once(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=30)
    ctrl.load_overview()
    ctrl.level = 0
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()  # clear the immediate re-issue from set_selection

    for i in range(5):
        view.view_box.setRange(xRange=(700 + i, 700 + i + 2048), yRange=(100, 2148), padding=0)
        _pump(5)  # well under settle_ms between range changes

    settled_gen_before = ctrl._settled_generation
    _pump(80)  # let the (restarted) settle timer fire once
    assert ctrl._settled_generation == settled_gen_before + 1

    # Precisely one settled batch of CorrectionKey requests should exist,
    # all sharing the same request.generation.
    precise_reqs = scheduler.pending_for(CorrectionKey)
    gens = {r.generation for r, _cb in precise_reqs}
    assert gens == {ctrl._settled_generation}

    ctrl.teardown()


# ── 4. jump_to issues the settled batch immediately ────────────────────────

def test_jump_to_issues_settled_batch_immediately(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)  # long settle
    ctrl.load_overview()
    ctrl.level = 0
    ctrl.set_selection(method="tophat", params=(10,))
    scheduler.requests.clear()

    gen_before = ctrl._settled_generation
    ctrl.jump_to(y0=0, x0=0, w=2048, h=2048)

    # No qWait needed: jump_to must issue the settled batch synchronously.
    assert ctrl._settled_generation == gen_before + 1
    precise_reqs = scheduler.pending_for(CorrectionKey)
    assert len(precise_reqs) > 0

    ctrl.teardown()


# ── 5. stale-generation precise result dropped ─────────────────────────────

def test_stale_precise_result_dropped(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.level = 0
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    precise_reqs = scheduler.pending_for(CorrectionKey)
    req, _cb = precise_reqs[0]

    # A second jump bumps the settled generation, making `req` stale.
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

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
    ctrl.level = 0
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    precise_reqs = scheduler.pending_for(CorrectionKey)
    req, _cb = precise_reqs[0]  # generation == current settled generation

    # Change the selection AFTER the request was issued but BEFORE delivery;
    # the settled generation stays the same only if we don't jump again --
    # so mutate method directly to simulate a race without bumping settle.
    ctrl.method = "cucim"

    arr = np.zeros((512, 512), dtype=np.float32)
    before = ctrl.stats["mismatched_key_dropped"]
    # req.generation still matches ctrl._settled_generation, so this exercises
    # the KEY-mismatch path, not the stale-generation path.
    assert req.generation == ctrl._settled_generation
    scheduler.deliver(req, arr)
    _pump(20)
    assert ctrl.stats["mismatched_key_dropped"] == before + 1

    ctrl.teardown()


# ── 7. selection change -> provisional; fresh matching tiles restore it ────

def test_selection_change_sets_provisional_then_restores(app):
    ctrl, provider, scheduler, view = make_controller(app, settle_ms=5000)
    ctrl.load_overview()
    ctrl.level = 0
    ctrl.set_selection(method="tophat", params=(10,))
    ctrl.jump_to(y0=0, x0=0, w=512, h=512)

    seen = []
    ctrl.provisional_changed.connect(seen.append)

    # Change method: must go provisional immediately.
    ctrl.set_selection(method="cucim", params=(8,))
    assert ctrl._provisional is True
    assert view.precise_item.opacity() == pytest.approx(0.5)
    assert True in seen

    # Deliver every visible tile under the NEW selection -> provisional clears.
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
    assert view.precise_item.opacity() == pytest.approx(1.0)
    assert False in seen

    ctrl.teardown()


# ── 8. world-rect math for a level-1 tile ──────────────────────────────────

def test_world_rect_math_level1_tile():
    # tx=2, ty=3, ds=4, tile_size=512 -> world origin (2*512*4, 3*512*4)
    rect = ExploreView.world_rect(y0=3 * 512, x0=2 * 512, h=512, w=512, ds=4.0)
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

    # teardown() is idempotent.
    ctrl.teardown()
    assert ctrl._teardown_order == ["scheduler.shutdown", "provider.close"]
