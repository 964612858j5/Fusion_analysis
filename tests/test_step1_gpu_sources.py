"""G2 public-tile-supply tests for the isolated Step1 GPU binding.

Controlled fixtures expose only the public provider/scheduler/controller
contracts consumed by ``Step1GpuBinding``.  The final hardware test is enabled
only with ``BLOCK01_REQUIRE_STEP1_GPU=1`` and reads the real G1 FBO; it does
not mount the layer into a production viewer.
"""

import importlib.util
import os
import pathlib
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt5 import QtCore, QtWidgets

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(_ROOT.parent))

from block01.ui.step1_gpu_binding import (
    PRIORITY_COARSE,
    PRIORITY_FINE_DEFERRED,
    PRIORITY_FINE_FOREGROUND,
    BindingBudgets,
    Step1GpuBinding,
)
from block01.ui.step1_viewer_host import Step1TileProvider
from block01.viewer.caches import LRUByteCache
from block01.viewer.scheduler import TileScheduler
from block01.viewer.step1_source import Step1SourceTable
from block01.ui.step1_gpu_layer import (
    MODE_OVERLAY,
    DisplaySnapshot,
    Step1GpuLayer,
    ViewportSnapshot,
)
from block01.viewer.step1_compose import compose
from block01.viewer.tile_types import (
    PixelBuffer,
    RawKey,
    SourceIdentity,
    TileGridSpec,
    TileResult,
)

GRID = TileGridSpec(tile_size=4, source_chunk_shape=(), grid_version="g2-test")


class _SourceTable:
    def __init__(self, missing=()):
        self._missing = set(missing)

    def source_of(self, channel):
        return "missing" if channel in self._missing else "raw"


class _ActualRaw:
    """Minimal public raw-pyramid port for a real scheduler/provider G2 test."""

    def __init__(self):
        self.reads = []
        self.gate = threading.Event()
        self.gate.clear()

    @property
    def num_levels(self):
        return 1

    def source_identity(self):
        return SourceIdentity("real-scheduler", "one", "raw")

    def level_shape(self, level):
        assert level == 0
        return 4, 4

    def level_downsample(self, level):
        assert level == 0
        return 1.0

    def level_downsample_yx(self, level):
        assert level == 0
        return 1.0, 1.0

    def warm_thread_handle(self):
        return False

    def read_region(self, channel, level, y0, y1, x0, x1):
        self.gate.wait(2)
        self.reads.append((channel, level, y0, y1, x0, x1))
        offset = 0.2 if channel == "A" else 0.4
        return np.full((y1 - y0, x1 - x0), offset, np.float32), (y0, x0)


class _GatedRawPyramid:
    """Public multi-level raw port whose reads can be held and are recorded.

    Only the public pyramid contract the real ``Step1TileProvider`` and
    ``TileScheduler`` already use; the gate is ordinary controlled I/O
    latency, not a private-state probe.
    """

    def __init__(self, level_shapes):
        self._level_shapes = tuple(tuple(shape) for shape in level_shapes)
        self.reads = []
        self._lock = threading.Lock()
        self.gate = threading.Event()
        self.gate.set()

    @property
    def num_levels(self):
        return len(self._level_shapes)

    def source_identity(self):
        return SourceIdentity("g2-1-fairness", "one", "raw")

    def level_shape(self, level):
        return self._level_shapes[level]

    def level_downsample(self, level):
        return float(2 ** level)

    def level_downsample_yx(self, level):
        return float(2 ** level), float(2 ** level)

    def warm_thread_handle(self):
        return False

    def read_region(self, channel, level, y0, y1, x0, x1):
        if not self.gate.wait(20):
            raise AssertionError("gated read was never released")
        with self._lock:
            self.reads.append((channel, level, y0, x0))
        value = 0.2 if channel == "A" else 0.4
        return np.full((y1 - y0, x1 - x0), value, np.float32), (y0, x0)


class _SourceTableWithRaw:
    def source_of(self, _channel):
        return "raw"

    def identity_token(self):
        return "test-source-table"

    def missing(self):
        return []


class _CorrectedPlane:
    def __init__(self, value, *, bbox=(0, 4, 0, 4)):
        self._values = np.full((bbox[1] - bbox[0], bbox[3] - bbox[2]), value, np.float32)
        self.attrs = {"roi_bbox_fullres": list(bbox)}
        self.shape = self._values.shape
        self.dtype = self._values.dtype

    def __getitem__(self, key):
        return self._values[key]


class _Provider:
    def __init__(self, *, level_shape=(8, 8), levels=1, source_suffix="one", missing=()):
        self.num_levels = levels
        self._level_shape = level_shape
        self.source_table = _SourceTable(missing)
        self._source = SourceIdentity("dataset", source_suffix, "step1", f"token-{source_suffix}")
        self._missing = []

    def source_identity(self):
        return self._source

    def level_shape(self, level):
        assert 0 <= level < self.num_levels
        if self.num_levels == 1:
            return self._level_shape
        divisor = 2 ** (self.num_levels - 1 - level)
        return tuple(int(value * divisor) for value in self._level_shape)

    def level_downsample_yx(self, level):
        return (float(2 ** level),) * 2

    def missing_products(self):
        return list(self._missing)

    def move_source(self, suffix):
        self._source = SourceIdentity("dataset", suffix, "step1", f"token-{suffix}")


class _Controller(QtCore.QObject):
    interaction_event = QtCore.pyqtSignal(str, object)
    gesture_quiet = QtCore.pyqtSignal(object)

    def __init__(self, provider, *, visible_tiles=((0, 0),), level=0, epoch=1):
        super().__init__()
        self.grid = GRID
        self.provider = provider
        self._snapshot = self.make_snapshot(visible_tiles=visible_tiles, level=level, epoch=epoch)

    def make_snapshot(self, *, visible_tiles, level=0, epoch=1):
        h, w = self.provider.level_shape(level)
        return SimpleNamespace(source=self.provider.source_identity(), level=level,
                               visible_tiles=frozenset(visible_tiles), epoch=epoch,
                               bbox_l0=(0, 0, h, w))

    def snapshot(self):
        return self._snapshot

    def set_snapshot(self, *, visible_tiles, level=0, epoch=1):
        self._snapshot = self.make_snapshot(visible_tiles=visible_tiles, level=level, epoch=epoch)
        return self._snapshot


class _Scheduler:
    """Controlled public request/cancel fake; never exposes cache internals."""

    def __init__(self, *, sync=False):
        self.sync = sync
        self.requests = []
        self.cancelled = []
        self._callbacks = {}

    def request(self, request, callback):
        self.requests.append(request)
        self._callbacks.setdefault(request.key, []).append((request, callback))
        if self.sync:
            callback(self.result_for(request, self._array(request.key)))

    def cancel_generation(self, generation):
        self.cancelled.append(generation)

    @staticmethod
    def _array(key, value=None):
        if value is not None:
            return np.asarray(value, dtype=np.float32)
        return np.full((4, 4), 0.2 if key.channel == "A" else 0.4, dtype=np.float32)

    @staticmethod
    def result_for(request, array, error=None):
        pixels = None if error else PixelBuffer("cpu", str(array.dtype), array.shape, array)
        return TileResult(request=request, pixels=pixels, quality="native", provisional=False,
                          timing={"cache": "hit"}, error=error)

    def deliver(self, key, *, array=None, error=None, request_index=0):
        request, callback = self._callbacks[key][request_index]
        callback(self.result_for(request, self._array(key, array), error))

    def deliver_request(self, request, *, array=None, error=None):
        for candidate, callback in self._callbacks[request.key]:
            if candidate is request:
                callback(self.result_for(request, self._array(request.key, array), error))
                return
        raise AssertionError("request callback was not registered")


class _RecordingLayer:
    def __init__(self):
        self.calls = []
        self.disposed = False

    def submit(self, descriptor, display, viewport):
        self.calls.append((descriptor, display, viewport))
        return {"cache": {"uploads": 0}, "pass_count": 0}

    def dispose(self):
        self.disposed = True
        return {"raw_textures_remaining": 0}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _events(app, cycles=8):
    for _ in range(cycles):
        app.processEvents()


def _display(active=("A", "B")):
    return DisplaySnapshot(
        MODE_OVERLAY,
        {channel: (0.0, 1.0, 1.0) for channel in active},
        {channel: 1.0 for channel in active},
        {"A": (1.0, 0.0, 0.0), "B": (0.0, 1.0, 0.0)},
    )


def _viewport(provider):
    h, w = provider.level_shape(0)
    return ViewportSnapshot((0.0, float(w), 0.0, float(h)), (w, h), 1.0)


def _budgets(*, coarse_tiles=64, coarse_bytes=1_000_000, fine_tiles=64, fine_bytes=1_000_000):
    return BindingBudgets(coarse_tiles, coarse_bytes, fine_tiles, fine_bytes, motion_interval_ms=1)


def _binding(provider, scheduler, controller, layer, *, display=None, budgets=None):
    current_display = [display or _display()]
    return Step1GpuBinding(
        provider=provider, scheduler=scheduler, controller=controller, layer=layer,
        build_display_snapshot=lambda: current_display[0],
        build_viewport_snapshot=lambda: _viewport(provider),
        budgets=budgets or _budgets(),
    ), current_display


def _wait_until(app, predicate, *, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        _events(app, 2)
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _fine_requests(scheduler, channel, since=0):
    """The binding's fine requests for one channel (anything not coarse)."""
    return [request for request in scheduler.requests[since:]
            if request.key.channel == channel and request.priority != PRIORITY_COARSE]


def _coarse_requests(scheduler, channel):
    return [request for request in scheduler.requests if request.key.channel == channel and request.priority == 100]


def _deliver_all(app, scheduler, requests, *, value=None):
    for request in requests:
        scheduler.deliver_request(request, array=value)
    _events(app)


def test_real_step1_tileprovider_and_scheduler_singleflight_feed_binding(app):
    class RecordingScheduler(TileScheduler):
        def __init__(self, *args, **kwargs):
            self.public_requests = []
            super().__init__(*args, **kwargs)

        def request(self, request, callback):
            self.public_requests.append(request)
            return super().request(request, callback)

    raw = _ActualRaw()
    table = Step1SourceTable({}, "", "", lambda *_args: None, roi_bbox=(0, 4, 0, 4))
    provider = Step1TileProvider(raw, table)
    controller = _Controller(provider, visible_tiles=((0, 0),), level=0)
    scheduler = RecordingScheduler(provider, None, LRUByteCache(1024 * 1024),
                                   LRUByteCache(1024 * 1024), io_workers=1, compute_workers=0)
    layer = _RecordingLayer()
    binding, _ = _binding(provider, scheduler, controller, layer, display=_display(("A",)))
    joined = []
    try:
        binding.source_changed()
        assert len(scheduler.public_requests) == 1
        request = scheduler.public_requests[0]
        assert isinstance(request.key, RawKey)
        # A second public request for the exact same RawKey joins the real
        # scheduler's single-flight entry rather than causing a second read.
        scheduler.request(request, joined.append)
        raw.gate.set()
        deadline = time.monotonic() + 3
        while not any(call[0].channels for call in layer.calls) and time.monotonic() < deadline:
            _events(app, 1)
            time.sleep(0.01)
        assert any(call[0].channels for call in layer.calls)
        assert len(raw.reads) == 1
        assert len(joined) == 1 and joined[0].error is None
        assert scheduler.raw_cache.stats()["items"] == 1
    finally:
        binding.dispose()
        scheduler.shutdown()


def test_step1_provider_original_corrected_missing_and_roi_rules_are_preserved():
    raw = _ActualRaw()
    raw.gate.set()
    corrected = _CorrectedPlane(-0.5, bbox=(1, 3, 1, 3))
    table = Step1SourceTable(
        {"B": "tophat", "C": "tophat"}, "/tmp/corrected.zarr", "roi",
        lambda _path, channel, _roi: corrected if channel == "B" else None,
        roi_bbox=(1, 3, 1, 3), handoff_revision="g2-source-test",
    )
    provider = Step1TileProvider(raw, table)
    tile = type("Tile", (), {"grid": GRID, "level": 0, "tx": 0, "ty": 0})()
    original, _ = provider.read_tile("A", tile)
    corrected_values, _ = provider.read_tile("B", tile)
    missing, _ = provider.read_tile("C", tile)
    assert np.isnan(original[0, 0]) and np.isfinite(original[1, 1])
    assert np.allclose(corrected_values[1:3, 1:3], -0.5)
    assert np.isnan(corrected_values[0, 0])
    assert np.isnan(missing).all()
    assert all(read[0] != "C" for read in raw.reads), "missing corrected product must not raw-fallback"
    assert provider.source_identity().stage == "step1"


def test_atomic_complete_coarse_uses_public_rawkeys_and_geometry(app):
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0),))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, _display_holder = _binding(provider, scheduler, controller, layer)
    try:
        binding.source_changed()
        requests = _coarse_requests(scheduler, "A")
        assert len(requests) == 4
        assert all(isinstance(request.key, RawKey) for request in requests)
        assert all(request.key.source == provider.source_identity() for request in requests)
        assert all(request.key.tile.grid == GRID for request in requests)
        # Center/edge arrivals cannot expose a partial A channel.
        for request in requests[:-1]:
            scheduler.deliver(request.key, array=np.full((4, 4), 0.2, np.float32))
            _events(app)
            assert not any(any(source.channel == "A" for source in call[0].channels) for call in layer.calls)
        scheduler.deliver(requests[-1].key, array=np.full((4, 4), 0.2, np.float32))
        _events(app)
        # G3.2b: the complete coarse is in, but this channel has never been
        # drawn and its current viewport's fine is still on its way, so it
        # is held back rather than shown blurry and then sharpened.
        assert not any(any(source.channel == "A" for source in call[0].channels)
                       for call in layer.calls)
        fine = _fine_requests(scheduler, "A")
        assert fine, "the viewport's fine must have been asked for"
        _deliver_all(app, scheduler, fine, value=np.full((4, 4), 0.5, np.float32))
        published = [call[0] for call in layer.calls if any(source.channel == "A" for source in call[0].channels)]
        assert len(published) == 1, "the channel must appear exactly once"
        a = next(source for source in published[0].channels if source.channel == "A")
        assert len(a.coarse) == 4 and a.fine and a.selected_level == "fine", \
            "the first appearance must already carry the viewport's fine"
        assert {plane.identity for plane in a.coarse} == {request.key for request in requests}
        assert {plane.world_rect for plane in a.coarse} == {
            (0.0, 4.0, 0.0, 4.0), (4.0, 8.0, 0.0, 4.0),
            (0.0, 4.0, 4.0, 8.0), (4.0, 8.0, 4.0, 8.0),
        }
    finally:
        binding.dispose()


def test_missing_source_and_coarse_budget_refuse_before_requests(app):
    provider = _Provider(level_shape=(8, 8), levels=1, missing=("B",))
    controller = _Controller(provider)
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, _ = _binding(provider, scheduler, controller, layer,
                          budgets=_budgets(coarse_tiles=1, coarse_bytes=4 * 4 * 4))
    try:
        binding.source_changed()
        assert not scheduler.requests
        state = binding.stats()
        assert state["unavailable"]["A"] == "coarse budget refused"
        assert state["unavailable"]["B"] == "Step1 source unavailable"
    finally:
        binding.dispose()


def test_sync_delivery_is_queued_and_hot_display_does_not_request_raw(app):
    provider = _Provider(level_shape=(4, 4), levels=1)
    controller = _Controller(provider)
    scheduler = _Scheduler(sync=True)
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer)
    try:
        binding.source_changed()
        # Sync scheduler callbacks arrive during request(), but Qt delivery is
        # queued until the complete request ledger was installed.
        assert not any(call[0].channels for call in layer.calls)
        _events(app)
        assert any(call[0].channels for call in layer.calls)
        before = len(scheduler.requests)
        display_holder[0] = dataclasses_replace(display_holder[0], weights={"A": 0.5, "B": 1.0})
        binding.refresh_display()
        _events(app)
        assert len(scheduler.requests) == before
    finally:
        binding.dispose()


def test_late_source_and_old_viewport_results_cannot_publish(app):
    provider = _Provider(level_shape=(4, 4), levels=1, source_suffix="old")
    controller = _Controller(provider, visible_tiles=((0, 0),), epoch=1)
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, _ = _binding(provider, scheduler, controller, layer)
    try:
        binding.source_changed()
        old_requests = list(scheduler.requests)
        provider.move_source("new")
        controller.set_snapshot(visible_tiles=((0, 0),), epoch=2)
        binding.source_changed()
        new_requests = scheduler.requests[len(old_requests):]
        _deliver_all(app, scheduler, old_requests, value=np.full((4, 4), 0.9, np.float32))
        assert binding.stats()["rejected_late_results"] >= len(old_requests)
        _deliver_all(app, scheduler, new_requests, value=np.full((4, 4), 0.2, np.float32))
        # G3.2b: a first appearance also waits for its viewport fine.
        for channel in ("A", "B"):
            _deliver_all(app, scheduler, _fine_requests(scheduler, channel),
                         value=np.full((4, 4), 0.2, np.float32))
        latest = next(call[0] for call in reversed(layer.calls) if call[0].channels)
        assert all(plane.identity.source == provider.source_identity()
                   for source in latest.channels for plane in source.coarse)
        controller.set_snapshot(visible_tiles=((0, 0),), epoch=3)
        before_fine = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        old_fine = scheduler.requests[before_fine:]
        controller.set_snapshot(visible_tiles=((0, 0),), epoch=4)
        before_new_fine = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        new_fine = scheduler.requests[before_new_fine:]
        _deliver_all(app, scheduler, old_fine, value=np.full((4, 4), 0.9, np.float32))
        _deliver_all(app, scheduler, new_fine, value=np.full((4, 4), 0.4, np.float32))
        assert binding.stats()["rejected_late_results"] >= len(old_requests) + len(old_fine)
    finally:
        binding.dispose()


def test_fine_is_immediate_atomic_and_retains_complete_coarse(app):
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0),), epoch=1)
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, _ = _binding(provider, scheduler, controller, layer)
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests), value=np.full((4, 4), 0.2, np.float32))
        # G3.2b: both channels appear only once their viewport fine is in.
        for channel in ("A", "B"):
            _deliver_all(app, scheduler, _fine_requests(scheduler, channel),
                         value=np.full((4, 4), 0.2, np.float32))
        assert binding.stats()["fine_channels"] == ("A", "B")
        # Jump to a tile whose fine is NOT already in hand, or there is
        # nothing for the jump to ask for.
        controller.set_snapshot(visible_tiles=((1, 0),), epoch=2)
        request_before_jump = len(scheduler.requests)
        controller.interaction_event.emit("NAVIGATOR_JUMP", controller.snapshot())
        _events(app)
        fine = [request for request in scheduler.requests[request_before_jump:] if request.priority == 0]
        assert fine, "jump must request visible fine before gesture_quiet"
        # A alone may commit a SHARPER fine while B keeps what it has: a
        # channel already on screen is never held back.
        a_fine = [request for request in fine if request.key.channel == "A"]
        _deliver_all(app, scheduler, a_fine, value=np.full((4, 4), 0.9, np.float32))
        latest = next(call[0] for call in reversed(layer.calls) if call[0].channels)
        a = next(source for source in latest.channels if source.channel == "A")
        b = next(source for source in latest.channels if source.channel == "B")
        assert a.selected_level == "fine"
        assert any(np.allclose(np.asarray(plane.values), 0.9) for plane in a.fine), \
            "the jump's own sharper tile is on screen"
        assert len(b.coarse) == 4, "B keeps its complete coarse"
    finally:
        binding.dispose()


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="hardware FBO G2 gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_hardware_fbo_full_coarse_then_local_fine_and_roi_alpha(app):
    provider = _Provider(level_shape=(8, 8), levels=2)
    controller = _Controller(provider, visible_tiles=((0, 0),), level=0, epoch=1)
    scheduler = _Scheduler()
    layer = Step1GpuLayer(max_raw_texture_bytes=64 * 1024, require_hardware=True)
    layer.resize(8, 8)
    layer.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    layer.show()
    _events(app)
    if not layer._initialized:
        pytest.fail(f"required G2 hardware layer unavailable: {layer._init_error}")
    display = _display()
    binding, _ = _binding(provider, scheduler, controller, layer, display=display)
    try:
        binding.source_changed()
        requests = list(scheduler.requests)
        # Deliver all complete coarse: A red has one invalid corner, B green.
        for request in requests:
            value = np.full((4, 4), 0.2 if request.key.channel == "A" else 0.3, np.float32)
            if request.key.channel == "A" and request.key.tile.tx == 0 and request.key.tile.ty == 0:
                value[0, 0] = np.nan
            scheduler.deliver(request.key, array=value)
        _events(app)
        # G3.2b: a first appearance also waits for its viewport fine. The
        # fine has to repeat the coarse's holes, or it would fill them in.
        for channel in ("A", "B"):
            for request in _fine_requests(scheduler, channel):
                value = np.full((4, 4), 0.2 if channel == "A" else 0.3, np.float32)
                if channel == "A" and request.key.tile.tx == 0 and request.key.tile.ty == 0:
                    value[0, 0] = np.nan
                scheduler.deliver_request(request, array=value)
        _events(app)
        coarse = layer.readback_rgba_for_test()
        assert np.all(coarse[..., 3][1:, 1:] == 255)
        assert coarse[0, 0, 3] == 255  # B makes the A-invalid corner valid.
        # Sharpen a tile whose fine is NOT already in hand: tile (1, 1) at
        # level 0 covers world 4..8, which is pixel (5, 5) of this readback.
        controller.set_snapshot(visible_tiles=((1, 1),), epoch=2)
        before_fine = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        fine = [request for request in scheduler.requests[before_fine:]
                if request.priority == 0 and request.key.channel == "A"]
        assert fine, "the new viewport tile must be asked for"
        _deliver_all(app, scheduler, fine, value=np.full((4, 4), 0.9, np.float32))
        refined = layer.readback_rgba_for_test()
        assert refined[5, 5, 0] > coarse[5, 5, 0]
        assert refined[6, 6, 1] == coarse[6, 6, 1]  # B coarse survives A fine.
        assert layer.environment_report()["software_renderer"] is False
    finally:
        binding.dispose()
        layer.dispose()
        layer.close()


def dataclasses_replace(instance, **changes):
    import dataclasses
    return dataclasses.replace(instance, **changes)


def test_new_channel_complete_coarse_is_not_starved_by_continuous_pan_fine(app):
    """G2.1 Blocker 1, judged on the REAL scheduler's actual read order.

    A is already shown.  B is enabled and begins its complete coarse
    transaction.  Twelve further public pan events keep asking for the
    current viewport's fine tiles.  Nothing here inspects the scheduler's
    private heap or pending map: the verdict is the order in which the
    single real I/O worker actually read tiles, plus what the binding
    published.
    """

    class RecordingScheduler(TileScheduler):
        def __init__(self, *args, **kwargs):
            self.public_requests = []
            super().__init__(*args, **kwargs)

        def request(self, request, callback):
            self.public_requests.append(request)
            return super().request(request, callback)

    raw = _GatedRawPyramid(((24, 24), (12, 12)))
    table = Step1SourceTable({}, "", "", lambda *_args: None, roi_bbox=(0, 24, 0, 24))
    provider = Step1TileProvider(raw, table)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)), level=0, epoch=1)
    scheduler = RecordingScheduler(provider, None, LRUByteCache(8 << 20),
                                   LRUByteCache(8 << 20), io_workers=1, compute_workers=0)
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A",)))
    try:
        binding.source_changed()
        assert _wait_until(app, lambda: binding.stats()["coarse_channels"] == ("A",)
                           and scheduler.idle()), "A must reach its complete coarse first"

        raw.gate.clear()
        read_marker = len(raw.reads)
        request_marker = len(scheduler.public_requests)

        display_holder[0] = _display(("A", "B"))
        binding.refresh_display()
        assert binding.coarse_pending_channels() == ("B",)

        # A jump still asks for its target straight away while coarse runs.
        controller.set_snapshot(visible_tiles=((4, 4),), level=0, epoch=2)
        jump_marker = len(scheduler.public_requests)
        controller.interaction_event.emit("NAVIGATOR_JUMP", controller.snapshot())
        jumped = scheduler.public_requests[jump_marker:]
        assert jumped, "a navigator jump must request its target without waiting"
        assert {(request.key.tile.tx, request.key.tile.ty) for request in jumped} == {(4, 4)}

        def pan_sweep(first_epoch, count=12):
            for step in range(count):
                tx = step % 5
                controller.set_snapshot(visible_tiles=((tx, 0), (tx + 1, 0)),
                                        level=0, epoch=first_epoch + step)
                controller.interaction_event.emit("PAN", controller.snapshot())
                time.sleep(0.004)
                _events(app)

        pan_sweep(3)
        # Bounded pending work: the scheduler dedups by tile identity and the
        # binding cancels the epoch it replaces, so twelve MORE pan events over
        # the same ground add no further outstanding work.
        pending_after_first_sweep = scheduler.activity_snapshot()["total"]
        pan_sweep(3 + 12)
        assert scheduler.activity_snapshot()["total"] == pending_after_first_sweep
        # 9 B coarse + the distinct fine tiles of BOTH channels: since
        # G3.2b a channel's viewport fine is asked for while its coarse is
        # still loading, so B contributes fine keys here too.
        assert pending_after_first_sweep <= 9 + 7 + 7

        issued = scheduler.public_requests[request_marker:]
        fine_issued = [request for request in issued if request.priority != PRIORITY_COARSE]
        assert fine_issued, "continuous panning must still ask for the current viewport"
        assert all(request.priority == PRIORITY_FINE_DEFERRED for request in fine_issued)
        assert not [request for request in fine_issued
                    if request.priority == PRIORITY_FINE_FOREGROUND]
        assert binding.coarse_pending_channels() == ("B",)
        # Bounded: each B coarse tile is asked for exactly once, and one
        # cancelled-and-replaced fine epoch never accumulates.
        b_coarse = [request for request in issued
                    if request.key.channel == "B" and request.priority == PRIORITY_COARSE]
        assert len(b_coarse) == 9

        raw.gate.set()
        assert _wait_until(app, lambda: binding.stats()["coarse_channels"] == ("A", "B")), \
            "B's complete coarse must finish under continuous pan/zoom"

        after = raw.reads[read_marker:]
        coarse_reads = [item for item in after if item[1] == 1]
        assert len(coarse_reads) == 9 and all(item[0] == "B" for item in coarse_reads)
        assert after[:9] == coarse_reads, \
            "every B coarse tile was read before any pan-issued fine tile"

        for descriptor, _display_snapshot, _viewport_snapshot, _stats in binding.descriptor_history:
            by_channel = {source.channel: source for source in descriptor.channels}
            if "B" in by_channel:
                assert len(by_channel["B"].coarse) == 9, "B never appears partially"
                assert "A" in by_channel, "enabling B never takes A off the picture"
        published = [item[0] for item in binding.descriptor_history if item[0].channels]
        assert published and all(any(source.channel == "A" for source in descriptor.channels)
                                 for descriptor in published)

        # Normal fine updates resume the moment coarse is done.
        assert binding.coarse_pending_channels() == ()
        controller.set_snapshot(visible_tiles=((0, 0),), level=0, epoch=99)
        resume_marker = len(scheduler.public_requests)
        binding.update_viewport(controller.snapshot())
        resumed = scheduler.public_requests[resume_marker:]
        assert resumed and all(request.priority == PRIORITY_FINE_FOREGROUND
                               for request in resumed)
    finally:
        raw.gate.set()
        binding.dispose()
        scheduler.shutdown()


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="hardware FBO G2.1 gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_hardware_fbo_partial_coarse_never_shows_a_centre_first_channel(app):
    """G2.1 Blocker 2: a real RTX framebuffer after EVERY partial coarse.

    Channel A is already prepared and drawn.  B's nine coarse tiles are
    delivered centre first, outskirts last.  After each of the first eight
    the real framebuffer must still be the A-only baseline, pixel for pixel:
    no centre-outwards appearance.  Only the ninth makes B appear, at once,
    over the whole valid area.
    """
    provider = _Provider(level_shape=(12, 12), levels=2)
    controller = _Controller(provider, visible_tiles=((0, 0),), level=0, epoch=1)
    scheduler = _Scheduler()
    layer = Step1GpuLayer(max_raw_texture_bytes=4 * 1024 * 1024, require_hardware=True)
    layer.resize(24, 24)
    layer.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    layer.show()
    _events(app)
    if not layer._initialized:
        pytest.fail(f"required G2.1 hardware layer unavailable: {layer._init_error}")
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A",)))

    def coarse_values(channel):
        values = np.full((4, 4), 0.2 if channel == "A" else 0.3, np.float32)
        return values

    try:
        # ---- baseline: A alone, complete coarse, with an invalid corner and
        # a valid-dark block that must stay an opaque dark pixel.
        binding.source_changed()
        a_requests = list(scheduler.requests)
        assert len(a_requests) == 9
        for request in a_requests:
            values = coarse_values("A")
            if request.key.tile.tx == 0 and request.key.tile.ty == 0:
                values[0:2, 0:2] = np.nan   # world 0..4: no pixels at all
                values[0:2, 2:4] = 0.0      # world x 4..8: valid, but dark
            scheduler.deliver(request.key, array=values)
        _events(app)
        # G3.2b: a first appearance also waits for its viewport fine. The
        # fine is a LEVEL-0 tile, a quarter of the coarse one, so the holes
        # are whole tiles here: (0,0) is the no-pixel corner and (1,0) is
        # the valid-but-dark block the coarse marked.
        for request in _fine_requests(scheduler, "A"):
            values = coarse_values("A")
            if request.key.tile.ty == 0 and request.key.tile.tx == 0:
                values[:] = np.nan
            elif request.key.tile.ty == 0 and request.key.tile.tx == 1:
                values[:] = 0.0
            scheduler.deliver_request(request, array=values)
        _events(app)
        baseline = layer.readback_rgba_for_test()
        assert baseline.shape == (24, 24, 4)
        assert np.all(baseline[0:4, 0:4, 3] == 0), "outside the ROI alpha stays 0"
        assert np.all(baseline[0:4, 4:8, 3] == 255), "valid-dark is an opaque dark pixel"
        assert np.all(baseline[0:4, 4:8, 0:3] == 0)
        assert np.all(baseline[..., 1] == 0), "B contributes nothing yet"
        assert np.any(baseline[8:16, 8:16, 0] > 0), "A already covers the centre"

        # ---- B enabled: nine coarse tiles, centre first.
        display_holder[0] = _display(("A", "B"))
        before_b = len(scheduler.requests)
        binding.refresh_display()
        b_requests = [request for request in scheduler.requests[before_b:]
                      if request.key.channel == "B"
                      and request.priority == PRIORITY_COARSE]
        assert len(b_requests) == 9
        by_address = {(request.key.tile.tx, request.key.tile.ty): request
                      for request in b_requests}
        order = [(1, 1), (0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2)]
        assert set(order) == set(by_address)

        intermediate = []
        for index, address in enumerate(order[:-1]):
            request = by_address[address]
            values = coarse_values("B")
            if address == (0, 0):
                values[0:2, 0:2] = np.nan
            scheduler.deliver(request.key, array=values)
            # A public pan between deliveries: it forces a real re-render and
            # keeps asking for fine, which must not preempt the coarse.
            controller.set_snapshot(visible_tiles=((index % 5, 0),), level=0, epoch=10 + index)
            controller.interaction_event.emit("PAN", controller.snapshot())
            time.sleep(0.004)
            _events(app)
            frame = layer.readback_rgba_for_test()
            intermediate.append(frame)
            assert np.array_equal(frame, baseline), (
                f"partial coarse {index + 1}/9 at {address} changed the picture")
            assert np.all(frame[..., 1] == 0), "no part of B may appear early"
            assert binding.coarse_pending_channels() == ("B",)

        assert len(intermediate) == 8
        pan_fine = [request for request in scheduler.requests[before_b:]
                    if request.priority != PRIORITY_COARSE]
        assert pan_fine and all(request.priority == PRIORITY_FINE_DEFERRED
                                for request in pan_fine)

        # ---- the last coarse tile: B appears once, over the whole valid area.
        last = by_address[order[-1]]
        scheduler.deliver(last.key, array=coarse_values("B"))
        _events(app)
        # G3.2b: the complete coarse is in, but B has never been drawn, so
        # it waits for its viewport fine too and the picture is STILL the
        # A-only baseline -- no blurry B, not even for one frame.
        held = layer.readback_rgba_for_test()
        assert np.array_equal(held, baseline), \
            "B appeared before its viewport fine was ready"
        b_fine = [request for request in scheduler.requests[before_b:]
                  if request.key.channel == "B"
                  and request.priority != PRIORITY_COARSE]
        assert b_fine, "B's viewport fine must have been asked for"
        _deliver_all(app, scheduler, b_fine, value=coarse_values("B"))
        complete = layer.readback_rgba_for_test()
        assert binding.coarse_pending_channels() == ()
        assert not np.array_equal(complete, baseline)
        valid = complete[..., 3] == 255
        assert np.all(valid[0:4, 4:8]) and not np.any(complete[0:4, 0:4, 3])
        assert np.all(complete[..., 1][valid] > 0), "B covers every valid pixel at once"
        assert np.array_equal(complete[..., 0], baseline[..., 0]), "A's own signal is untouched"

        # ---- local fine: only that region sharpens; B coarse and A stay.
        controller.set_snapshot(visible_tiles=((2, 2), (3, 3)), level=0, epoch=50)
        before_fine = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        fine = [request for request in scheduler.requests[before_fine:]
                if request.key.channel == "B"]
        assert fine and all(request.priority == PRIORITY_FINE_FOREGROUND for request in fine)
        _deliver_all(app, scheduler, fine, value=np.full((4, 4), 0.9, np.float32))
        refined = layer.readback_rgba_for_test()
        sharpened = np.zeros((24, 24), bool)
        sharpened[8:12, 8:12] = True
        sharpened[12:16, 12:16] = True
        assert np.all(refined[..., 1][sharpened] > complete[..., 1][sharpened])
        assert np.array_equal(refined[..., 1][~sharpened], complete[..., 1][~sharpened]), \
            "B keeps its coarse everywhere the fine did not land"
        assert np.array_equal(refined[..., 0], complete[..., 0]), "A survives B's fine"
        assert np.array_equal(refined[..., 3], complete[..., 3])
        assert layer.environment_report()["software_renderer"] is False
    finally:
        binding.dispose()
        layer.dispose()
        layer.close()


def test_results_from_a_superseded_binding_revision_are_refused(app):
    """A rebind that lands on the SAME source still refuses the old round.

    The tile keys are identical across the rebind, so nothing but the
    binding's own revision and generation can tell the two rounds apart.
    """
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0),))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, _ = _binding(provider, scheduler, controller, layer,
                          display=_display(("A",)))
    try:
        binding.source_changed()
        first = list(scheduler.requests)
        assert first
        binding.source_changed()
        second = scheduler.requests[len(first):]
        assert {request.key for request in second} == {request.key for request in first}
        before = binding.stats()["rejected_late_results"]

        _deliver_all(app, scheduler, first, value=np.full((4, 4), 0.9, np.float32))
        assert binding.stats()["rejected_late_results"] >= before + len(first)
        assert binding.stats()["coarse_channels"] == (), \
            "a superseded revision's tiles reached the picture"
        assert not any(call[0].channels for call in layer.calls)

        _deliver_all(app, scheduler, second, value=np.full((4, 4), 0.2, np.float32))
        assert binding.stats()["coarse_channels"] == ("A",)
    finally:
        binding.dispose()


def _fine_budgets(fine_bytes):
    return BindingBudgets(64, 1_000_000, 64, fine_bytes, motion_interval_ms=1)


def _settle_coarse(app, scheduler, binding):
    _deliver_all(app, scheduler, list(scheduler.requests),
                 value=np.full((4, 4), 0.2, np.float32))
    assert binding.stats()["coarse_channels"] == ("A",)


def test_a_carried_layer_never_pushes_the_current_target_out_of_budget(app):
    """G3.2a.1: the target level is reserved BEFORE any stand-in is kept.

    A layer carried across a zoom is only a stand-in until the target
    arrives.  If it were allowed to eat the budget first, the target could
    be refused for good and the viewport would sit on the stand-in -- which
    is exactly the "some zoom levels stay blurry" report.
    """
    provider = _Provider(level_shape=(8, 8), levels=2)     # L1 2x2, L0 4x4
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0),
                                                      (0, 1), (1, 1)), level=1)
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    tile_bytes = 4 * 4 * 4
    # Room for the six-tile target set and ONE stand-in, and no more.
    binding, _ = _binding(provider, scheduler, controller, layer,
                          display=_display(("A",)),
                          budgets=_fine_budgets(7 * tile_bytes))
    try:
        binding.source_changed()
        _settle_coarse(app, scheduler, binding)
        # Fill the coarser level's fine first: four planes, all of which
        # will still overlap the level-0 viewport below.
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        _deliver_all(app, scheduler, scheduler.requests[marker:],
                     value=np.full((4, 4), 0.3, np.float32))
        assert binding.stats()["fine_levels"]["A"] == (1,)
        assert binding.stats()["fine_tiles"]["A"] == 4

        # Cross to level 0. Target = six tiles; the four stand-ins plus the
        # target would be ten, well over the seven-tile budget.
        controller.set_snapshot(visible_tiles=((0, 0), (1, 0), (2, 0),
                                               (0, 1), (1, 1), (2, 1)),
                                level=0, epoch=2)
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        stats = binding.stats()
        assert stats["fine_budget_refused"] == (), \
            f"the target level was refused for a stand-in: {stats['last_error']}"
        issued = scheduler.requests[marker:]
        assert len(issued) == 6, f"the whole target set must be asked for: {len(issued)}"
        assert stats["retained_fine_last_epoch"] == 1, \
            "the stand-in may only use what the target set left over"

        _deliver_all(app, scheduler, issued, value=np.full((4, 4), 0.9, np.float32))
        settled = binding.stats()
        assert 0 in settled["fine_levels"]["A"]
        # Six target tiles reached the screen, plus the one stand-in the
        # leftover budget allowed -- and not a byte more.
        assert settled["fine_tiles"]["A"] == 7, settled["fine_tiles"]
        assert settled["fine_plane_bytes"] <= 7 * tile_bytes
        resident = {key for key in binding._published_fine["A"]
                    if int(key.tile.level) == 0}
        assert len(resident) == 6, "the whole target level must be resident"
    finally:
        binding.dispose()


def test_a_target_level_that_does_not_fit_by_itself_fails_closed(app):
    """The refusal that IS legitimate: this viewport's own target is too big."""
    provider = _Provider(level_shape=(8, 8), levels=2)
    controller = _Controller(provider, visible_tiles=((0, 0),), level=1)
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    tile_bytes = 4 * 4 * 4
    binding, _ = _binding(provider, scheduler, controller, layer,
                          display=_display(("A",)),
                          budgets=_fine_budgets(2 * tile_bytes))
    try:
        binding.source_changed()
        _settle_coarse(app, scheduler, binding)
        controller.set_snapshot(visible_tiles=((0, 0), (1, 0), (2, 0), (3, 0)),
                                level=0, epoch=2)
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        assert scheduler.requests[marker:] == [], \
            "a target set that cannot fit must not be half-requested"
        stats = binding.stats()
        assert stats["fine_budget_refused"] == ("A",)
        assert "target level needs" in str(stats["last_error"])
    finally:
        binding.dispose()


def test_unticking_a_channel_cancels_its_fine_and_releases_its_planes(app):
    """G3.2a.1: the release is on the untick, with the camera standing still."""
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A", "B")))
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests),
                     value=np.full((4, 4), 0.2, np.float32))
        assert binding.stats()["coarse_channels"] == ("A", "B")
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        fine = scheduler.requests[marker:]
        b_fine = [request for request in fine if request.key.channel == "B"]
        assert b_fine
        # Deliver A's fine only, so B still has one in flight.
        _deliver_all(app, scheduler, [r for r in fine if r.key.channel == "A"],
                     value=np.full((4, 4), 0.9, np.float32))
        assert binding.stats()["fine_tiles"].get("A", 0) > 0
        before_cancelled = len(scheduler.cancelled)

        display_holder[0] = _display(("A",))
        binding.refresh_display()
        _events(app)
        stats = binding.stats()
        assert "B" not in stats["fine_tiles"], "B kept its fine after the untick"
        assert "B" not in stats["fine_channels"]
        assert len(scheduler.cancelled) > before_cancelled, \
            "B's unfinished fine request was not cancelled"
        assert stats["fine_tiles"].get("A", 0) > 0, "A was released too"
        latest = next(call[0] for call in reversed(layer.calls))
        assert {source.channel for source in latest.channels} == {"A"}

        # B's late tile must not resurrect it.
        _deliver_all(app, scheduler, b_fine, value=np.full((4, 4), 0.5, np.float32))
        assert "B" not in binding.stats()["fine_tiles"]

        # Ticking B back asks for this viewport's fine again.
        marker = len(scheduler.requests)
        display_holder[0] = _display(("A", "B"))
        binding.refresh_display()
        _events(app)
        again = [r for r in scheduler.requests[marker:] if r.key.channel == "B"]
        assert again, "the channel came back but was never asked for"
        _deliver_all(app, scheduler, again, value=np.full((4, 4), 0.7, np.float32))
        assert binding.stats()["fine_tiles"].get("B", 0) > 0
    finally:
        binding.dispose()


def test_a_display_only_change_still_asks_for_nothing_after_the_release(app):
    """The release must not turn a colour or a weight into new I/O."""
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A", "B")))
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests),
                     value=np.full((4, 4), 0.2, np.float32))
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        _deliver_all(app, scheduler, scheduler.requests[marker:],
                     value=np.full((4, 4), 0.9, np.float32))
        before = len(scheduler.requests)
        submissions = len(binding.descriptor_history)

        # Same channels, different numbers: a weight and a window.
        display_holder[0] = dataclasses_replace(
            display_holder[0], weights={"A": 0.4, "B": 1.0})
        binding.refresh_display()
        display_holder[0] = dataclasses_replace(
            display_holder[0], mappings={"A": (10.0, 900.0, 0.8),
                                         "B": (0.0, 1.0, 1.0)})
        binding.refresh_display()
        _events(app)
        assert len(scheduler.requests) == before, "a display change asked for tiles"
        assert len(binding.descriptor_history) > submissions
        assert binding.stats()["fine_tiles"]["A"] > 0
        assert binding.stats()["fine_tiles"]["B"] > 0
    finally:
        binding.dispose()


def test_unticking_the_last_channel_and_ticking_it_back_restores_its_fine(app):
    """G3.2a.2: the only channel comes back WITHOUT a camera move.

    Unticking the last one leaves the active set empty; if that were not
    recorded, ticking the same channel back would look like "nothing
    changed" and its fine would never be planned again.
    """
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A",)))
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests),
                     value=np.full((4, 4), 0.2, np.float32))
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        _deliver_all(app, scheduler, scheduler.requests[marker:],
                     value=np.full((4, 4), 0.9, np.float32))
        loaded = binding.stats()["fine_tiles"].get("A", 0)
        assert loaded > 0

        display_holder[0] = _display(())
        binding.refresh_display()
        _events(app)
        emptied = binding.stats()
        assert emptied["fine_channels"] == () and emptied["fine_plane_bytes"] == 0
        assert emptied["coarse_channels"] == ("A",), "the coarse is kept"

        # Tick it back. THE CAMERA HAS NOT MOVED.
        marker = len(scheduler.requests)
        display_holder[0] = _display(("A",))
        binding.refresh_display()
        _events(app)
        again = scheduler.requests[marker:]
        assert again, "the only channel came back but was never asked for"
        _deliver_all(app, scheduler, again, value=np.full((4, 4), 0.9, np.float32))
        assert binding.stats()["fine_tiles"].get("A", 0) == loaded
        latest = next(call[0] for call in reversed(layer.calls) if call[0].channels)
        assert next(s for s in latest.channels if s.channel == "A").fine
    finally:
        binding.dispose()


def test_unticking_one_channel_leaves_the_other_channels_load_alone(app):
    """G3.2a.2: a removal touches the removed channel and nothing else."""
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A", "B")))
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests),
                     value=np.full((4, 4), 0.2, np.float32))
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        fine = scheduler.requests[marker:]
        a_fine = [request for request in fine if request.key.channel == "A"]
        b_fine = [request for request in fine if request.key.channel == "B"]
        assert a_fine and b_fine
        # NEITHER has landed: both channels are still loading.
        a_generations = {request.generation for request in a_fine}
        cancelled_before = len(scheduler.cancelled)
        requests_before = len(scheduler.requests)

        display_holder[0] = _display(("A",))
        binding.refresh_display()
        _events(app)

        assert len(scheduler.requests) == requests_before, \
            "the surviving channel's tiles were asked for again"
        new_cancels = scheduler.cancelled[cancelled_before:]
        assert new_cancels, "the unticked channel's request was not cancelled"
        assert not (set(new_cancels) & a_generations), \
            "the surviving channel's generation was cancelled too"

        # A's ORIGINAL requests still publish: its generation is untouched.
        _deliver_all(app, scheduler, a_fine, value=np.full((4, 4), 0.9, np.float32))
        assert binding.stats()["fine_tiles"].get("A", 0) == len(a_fine)
        # B's late tiles do not bring it back.
        _deliver_all(app, scheduler, b_fine, value=np.full((4, 4), 0.5, np.float32))
        assert "B" not in binding.stats()["fine_tiles"]
    finally:
        binding.dispose()


def _enable_second_channel(app, binding, display_holder, scheduler):
    """Tick B on with A already drawn, and hand B its complete coarse."""
    display_holder[0] = _display(("A", "B"))
    binding.refresh_display()
    _events(app)
    coarse = _coarse_requests(scheduler, "B")
    assert coarse, "B's complete coarse must have been asked for"
    _deliver_all(app, scheduler, coarse, value=np.full((4, 4), 0.4, np.float32))
    return coarse


def _drawn(layer):
    latest = next((call[0] for call in reversed(layer.calls) if call[0].channels), None)
    return set() if latest is None else {s.channel for s in latest.channels}


def test_a_first_channel_over_the_fine_budget_stays_off_the_screen(app):
    """G3.2b.1: a refused viewport is not 'ready'. Fail closed, stay hidden.

    The complete coarse is in hand, but the current viewport's target level
    does not fit the per-channel fine budget, so there is no plan in flight
    and there never will be one. That must not be read as 'the fine is
    ready' -- showing the coarse then would be exactly the blurry first
    appearance this contract removes.
    """
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    tile_bytes = 4 * 4 * 4
    binding, display_holder = _binding(
        provider, scheduler, controller, layer, display=_display(("A",)),
        budgets=_budgets(fine_bytes=2 * tile_bytes))   # room for two tiles
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests),
                     value=np.full((4, 4), 0.2, np.float32))
        _deliver_all(app, scheduler, _fine_requests(scheduler, "A"),
                     value=np.full((4, 4), 0.2, np.float32))
        assert _drawn(layer) == {"A"}

        # Now widen the viewport past the budget. A is already on screen and
        # keeps what it has; B has never been drawn.
        controller.set_snapshot(visible_tiles=((0, 0), (1, 0), (0, 1)), epoch=2)
        binding.update_viewport(controller.snapshot())
        _events(app)
        assert _drawn(layer) == {"A"}
        marker = len(scheduler.requests)
        _enable_second_channel(app, binding, display_holder, scheduler)
        stats = binding.stats()
        assert "B" in stats["coarse_channels"], "B's complete coarse is in hand"
        assert "B" in stats["fine_budget_refused"], "the refusal must be recorded"
        assert "target level needs" in str(stats["last_error"])
        assert not _fine_requests(scheduler, "B", marker), \
            "a refused viewport must not be half-requested"
        assert _drawn(layer) == {"A"}, "B leaked its coarse onto the screen"

        # An ordinary redraw must not let it out either.
        for _ in range(3):
            binding.refresh_display()
            _events(app)
            assert _drawn(layer) == {"A"}, "a refresh leaked the refused channel"
        assert "B" in binding.stats()["fine_budget_refused"]
    finally:
        binding.dispose()


def test_a_first_channel_whose_fine_tile_fails_stays_off_the_screen(app):
    """G3.2b.1: one failed tile is not 'ready' either."""
    provider = _Provider(level_shape=(8, 8), levels=1)
    controller = _Controller(provider, visible_tiles=((0, 0), (1, 0)))
    scheduler = _Scheduler()
    layer = _RecordingLayer()
    binding, display_holder = _binding(provider, scheduler, controller, layer,
                                       display=_display(("A",)))
    try:
        binding.source_changed()
        _deliver_all(app, scheduler, list(scheduler.requests),
                     value=np.full((4, 4), 0.2, np.float32))
        _deliver_all(app, scheduler, _fine_requests(scheduler, "A"),
                     value=np.full((4, 4), 0.2, np.float32))
        assert _drawn(layer) == {"A"}

        marker = len(scheduler.requests)
        _enable_second_channel(app, binding, display_holder, scheduler)
        # The LIVE plan's own requests: a first planning happened while the
        # coarse was still loading and was superseded when it landed.
        generation = binding._fine["B"].generation
        fine = [request for request in scheduler.requests
                if request.key.channel == "B" and request.generation == generation]
        assert len(fine) >= 2, "this gate needs more than one viewport tile"
        del marker
        # One tile lands, the next one fails.
        scheduler.deliver_request(fine[0], array=np.full((4, 4), 0.4, np.float32))
        _events(app)
        scheduler.deliver_request(fine[1], error="read failed")
        _events(app)

        stats = binding.stats()
        assert "B" in stats["coarse_channels"]
        # PARTIAL fine really is resident -- one tile landed, the other
        # failed -- and the channel is STILL not on screen. That is the
        # whole point: a partial viewport is not a reason to show it.
        assert stats["fine_tiles"].get("B") == 1, stats["fine_tiles"]
        assert "B" in stats["fine_channels"]
        assert _drawn(layer) == {"A"}, "B leaked its coarse after a failed tile"
        assert "fine tile failed for B" in str(stats["last_error"])

        for _ in range(3):
            binding.refresh_display()
            _events(app)
            assert _drawn(layer) == {"A"}, "a refresh leaked the failed channel"
    finally:
        binding.dispose()
