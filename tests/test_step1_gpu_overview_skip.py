"""G3.2b.4D: the overview the GPU takeover would never have shown.

`build_step1_stack` reads the whole slide's coarsest level synchronously,
into the controller's OWN layers. A successful GPU takeover sets those
layers to opacity 0 (`Step1WholeSlideMount._start_gpu_backend`), so the read
is paid for pixels nobody can see. These gates say: the takeover skips it,
and every path that does NOT take over reads it exactly as before -- at
open, and again after a source change.

The rig is the REAL `Step1WholeSlideMount` over a real `ExploreView`,
`ExploreController`, `TileScheduler` and `Step1TileProvider` on a synthetic
pyramid, with the REAL `Step1GpuLayer` unless a test breaks it on purpose.
The overview is counted on the PUBLIC read path -- a whole-level
`read_region`, which is what `ExploreController._read_overview_record`
issues and what no tile read ever looks like.

`BLOCK01_REQUIRE_STEP1_GPU=1` makes the hardware gates fail instead of skip.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.core.fusion_domain import FusionDomainModel  # noqa: E402
from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_draft_spec import STEP1_SCOPE  # noqa: E402
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost, build_step1_stack,
)
from block01.ui.step1_gpu_layer import Step1GpuLayer  # noqa: E402
from block01.ui.step1_viewer_mount import (  # noqa: E402
    BACKEND_CPU_FALLBACK, BACKEND_GPU, DEMO_GPU_RAW_TEXTURE_BYTES,
    Step1WholeSlideMount,
)
from block01.viewer.tile_types import SourceIdentity, TileGridSpec  # noqa: E402

REQUIRE_GPU = os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") == "1"

TILE = 256
SLIDE = 1536                     # level 0; level 1 is 768 -- the overview
ROI = (256, 1280, 256, 1280)
GRID = TileGridSpec(tile_size=TILE, source_chunk_shape=(), grid_version="v1")
CHANNELS = ("DAPI", "CD3", "CD8")
WINDOW = (0.0, 4096.0, 1.0)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the slide, counting the one read this block is about ──────────────

class _Pyramid:
    """A public raw pyramid that records every read it is asked for."""

    def __init__(self, log):
        self._shapes = {0: (SLIDE, SLIDE), 1: (SLIDE // 2, SLIDE // 2)}
        self._log = log

    def source_identity(self):
        return SourceIdentity(dataset_path="/x/g32b4d.ome.tif",
                              dataset_fingerprint="1:1", stage="raw")

    @property
    def num_levels(self):
        return 2

    @property
    def channel_names(self):
        return list(CHANNELS)

    def channel_index(self, channel):
        return CHANNELS.index(channel)

    def level_shape(self, level):
        return self._shapes[level]

    def level_downsample(self, level):
        return 1.0 if level == 0 else 2.0

    def level_downsample_yx(self, level):
        ds = self.level_downsample(level)
        return ds, ds

    def warm_thread_handle(self):
        return False

    def read_region(self, channel, level, y0, y1, x0, x1):
        # An OVERVIEW read is the only one wider than a tile: it asks for
        # the whole coarsest level in one go (clipped to the analysis
        # region by `viewer.step1_source`, which is why the rectangle is
        # not always the full level).
        self._log.append({"channel": str(channel), "level": int(level),
                          "rect": (int(y0), int(y1), int(x0), int(x1)),
                          "wide": (int(y1) - int(y0) > TILE
                                   or int(x1) - int(x0) > TILE)})
        base = (CHANNELS.index(channel) + 1) * 300.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return base + (yy % 7) * 10.0 + (xx % 5) * 3.0, (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        values, _offset = self.read_region(channel, tile.level, y0, y0 + size,
                                           x0, x0 + size)
        return values, 0.0

    def close(self):
        return None


class _Window:
    """Everything the mount reads from a window, and nothing else."""

    def __init__(self, state, domain):
        self.seeds = []

        def _seed(channel, nucleus=False):
            self.seeds.append(str(channel))
            return True

        self._display = SimpleNamespace(state=state, fusion=domain,
                                        request_mapping_seed=_seed)
        self.loader = SimpleNamespace(filepath="/x/g32b4d.ome.tif")
        self._corrected_decisions = {}
        self._corrected_zarr_path = "/tmp/g32b4d-none.zarr"
        self._active_roi = {"name": "ROI_1", "bbox_fullres": list(ROI)}
        self.step0_output = {"channel_remap_config_hash": "rev-1"}


def _stack_factory(reads, stacks):
    """The production factory's shape, including its new keyword.

    Deliberately NOT a wrapper around `build_step1_stack`: that one opens a
    real OME-TIFF. What matters is that this one honours `load_overview`
    the same way, so the mount's wrapper is exercised end to end.
    """

    def _factory(path, chan, table, parent, *, load_overview=True):
        from block01.ui.step0.step0_explore_tab import ExploreStack
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView
        from block01.viewer.scheduler import TileScheduler

        raw = _Pyramid(reads)
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(64 * 1024 * 1024)
        corrected_cache = LRUByteCache(64 * 1024 * 1024)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = TileScheduler(provider, compute, raw_cache,
                                  corrected_cache, io_workers=8,
                                  compute_workers=2)
        view = ExploreView(parent)
        controller = ExploreController(provider, scheduler, compute, GRID,
                                       view, chan)
        if load_overview:
            controller.load_overview()
        view.view_box.setRange(xRange=(0, SLIDE), yRange=(0, SLIDE),
                               padding=0)
        stack = ExploreStack(provider, scheduler, controller, view,
                             (raw_cache, corrected_cache))
        stacks.append(stack)
        return stack

    return _factory


# ── the owners ────────────────────────────────────────────────────────

def _identity():
    return DatasetIdentity(path="/x/g32b4d.ome.tif", fingerprint="1:1")


def _domain():
    domain = FusionDomainModel()
    domain.bind_dataset(_identity())
    domain.prepare_restore(_identity(), {
        "groups": {"markers": {"group_weight": 1.0,
                               "channels": {"CD3": 1.0, "CD8": 0.5}}},
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "enabled": list(CHANNELS)})
    domain.commit_restore("fixture")
    domain.install_committed_snapshot(
        {"hash": "one", "fusion_config": domain.effective_config()})
    return domain


def _state(visible=("CD3",)):
    state = ChannelDisplayState()
    state.bind(_identity(), install={"order": CHANNELS})
    with state.using_scope(STEP1_SCOPE):
        for channel in CHANNELS:
            state.set_display_visible(channel, channel in visible)
            state.set_color(channel, {"DAPI": "#0000ff", "CD3": "#00ff00",
                                      "CD8": "#ff0000"}[channel])
            state.set_mapping(channel, *WINDOW)
        state.set_selected_channel("CD3")
    return state


class _BrokenLayer:
    """The way `tests/test_step1_gpu_takeover.py` already forces fallback."""

    def __init__(self):
        self.disposed = False

    def attach(self, _view):
        raise RuntimeError("no OpenGL 3.3 context on this machine")

    def dispose(self):
        self.disposed = True

    def setParent(self, _parent):
        pass

    def deleteLater(self):
        pass


def _settle(app, rounds=60):
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.005)


def _layer_factory(break_after=None):
    """The real layer, until the Nth build -- then the broken one.

    `break_after=0` is the plain "this machine has no GPU" case;
    `break_after=1` is the GPU that starts and then cannot restart on the
    new source, which is the OTHER place `_start_gpu_backend` returns
    False.
    """
    built = []

    def _build(_stack):
        built.append(1)
        if break_after is not None and len(built) > break_after:
            return _BrokenLayer()
        return Step1GpuLayer(max_raw_texture_bytes=DEMO_GPU_RAW_TEXTURE_BYTES,
                             require_hardware=True)

    return _build


def _mount(app, *, gpu=True, broken_layer=False, break_after=None):
    reads, stacks = [], []
    state = _state()
    domain = _domain()
    window = _Window(state, domain)
    factory = _stack_factory(reads, stacks)
    host = Step1ViewerHost(stack_factory=factory)
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(512, 512)
    host.show()
    app.processEvents()
    layer_factory = None
    if broken_layer:
        layer_factory = _layer_factory(break_after=0)
    elif break_after is not None:
        layer_factory = _layer_factory(break_after=break_after)
    mount = Step1WholeSlideMount(window, host=host, gpu=gpu,
                                 gpu_layer_factory=layer_factory)
    mount.open("CD3")
    _settle(app)
    return SimpleNamespace(mount=mount, window=window, state=state,
                           domain=domain, reads=reads, stacks=stacks,
                           factory=factory, app=app)


def _close(rig):
    rig.mount.close()
    QtWidgets.QApplication.processEvents()


def _overview_reads(rig):
    return [r for r in rig.reads if r["wide"]]


def _require_gpu(rig):
    if rig.mount.backend == BACKEND_GPU:
        return
    reason = rig.mount.gpu_status()["reason"]
    if REQUIRE_GPU:
        pytest.fail(f"required Step1 GPU backend unavailable: {reason}")
    pytest.skip(f"Step1 GPU backend unavailable: {reason}")


def _move_the_source(rig):
    rig.window.step0_output = {"channel_remap_config_hash": "rev-2"}
    assert rig.mount.viewer.source_moved() is True
    assert rig.mount.sync_source("test") is True
    _settle(rig.app)


def _camera_is_sane(mount):
    """The camera answers, and the one controller still has a viewport."""
    camera = mount.current_camera()
    rect = mount.view_rect_l0()
    assert camera is not None and len(camera) == 3
    assert rect is not None and rect[1] > rect[0] and rect[3] > rect[2]
    snapshot = mount.host.stack.controller.snapshot()
    assert isinstance(snapshot.level, int)
    assert 0 <= snapshot.level < 2
    assert snapshot.visible_tiles, "the one controller sees no tiles"
    return snapshot


# ══ the keyword itself ════════════════════════════════════════════════

def test_the_production_factory_still_reads_the_overview_by_default():
    import inspect

    signature = inspect.signature(build_step1_stack)
    parameter = signature.parameters["load_overview"]
    assert parameter.kind is parameter.KEYWORD_ONLY
    assert parameter.default is True


def test_the_product_factory_honours_the_keyword(app, monkeypatch):
    """The real `build_step1_stack`, with only its pyramid replaced."""
    from block01.viewer import raw_tile_provider
    from block01.viewer.step1_source import Step1SourceTable

    reads = []
    monkeypatch.setattr(raw_tile_provider, "RawTileProvider",
                        lambda path: _Pyramid(reads))
    table = Step1SourceTable(decisions={}, corrected_zarr_path="",
                             roi_name="ROI_1", roi_bbox=ROI,
                             handoff_revision="rev-1")

    stack = build_step1_stack("/x/g32b4d.ome.tif", "CD3", table, None,
                              load_overview=False)
    try:
        assert [r for r in reads if r["wide"]] == []
        assert stack.controller.snapshot().overview_ready is False
    finally:
        stack.teardown()

    reads.clear()
    stack = build_step1_stack("/x/g32b4d.ome.tif", "CD3", table, None)
    try:
        assert len([r for r in reads if r["wide"]]) == 1
        assert stack.controller.snapshot().overview_ready is True
    finally:
        stack.teardown()


# ══ 1. the takeover skips it ══════════════════════════════════════════

def test_a_gpu_takeover_never_reads_the_overview(app):
    rig = _mount(app)
    try:
        _require_gpu(rig)
        controller = rig.mount.host.stack.controller
        assert _overview_reads(rig) == [], (
            "the invisible controller read the whole coarsest level")
        # The honest predicate: no record for the live channel, and the
        # controller's own layers hold nothing to show.
        assert controller.has_overview_record("CD3") is False
        assert controller.overview_record("CD3") is None
        # ...and none is INSTALLED either: the controller's own snapshot
        # says so, which is the same answer `_blocked_on_overview` reads.
        assert controller.snapshot().overview_ready is False
        _camera_is_sane(rig.mount)
        # ...and the picture is still the GPU's own.
        assert rig.mount.gpu_binding.descriptor_history
    finally:
        _close(rig)


# ══ 2. a GPU that cannot start gets it, exactly as today ══════════════

def test_a_gpu_layer_that_fails_leaves_the_overview_installed(app):
    rig = _mount(app, broken_layer=True)
    try:
        assert rig.mount.backend == BACKEND_CPU_FALLBACK
        controller = rig.mount.host.stack.controller
        reads = _overview_reads(rig)
        assert len(reads) == 1 and reads[0]["channel"] == "CD3"
        assert controller.has_overview_record("CD3") is True
        assert controller.overview_record("CD3") is not None
        assert controller.snapshot().overview_ready is True
        _settle(app, rounds=40)
        assert rig.mount.layer is not None
        assert rig.mount.layer.tiles_blitted > 0, "the CPU path drew nothing"
        _camera_is_sane(rig.mount)
    finally:
        _close(rig)


# ══ 3. with no GPU wanted, nothing moved at all ═══════════════════════

def test_without_the_gpu_the_stack_reads_its_own_overview(app):
    rig = _mount(app, gpu=False)
    try:
        assert rig.mount.backend == BACKEND_CPU_FALLBACK
        # The injected factory is the one the host was given: a mount that
        # wants no GPU wraps nothing.
        assert rig.mount.host.stack_factory is rig.factory
        controller = rig.mount.host.stack.controller
        reads = _overview_reads(rig)
        assert len(reads) == 1 and reads[0]["channel"] == "CD3"
        assert controller.has_overview_record("CD3") is True
        assert controller.snapshot().overview_ready is True
        _settle(app, rounds=40)
        assert rig.mount.layer.tiles_blitted > 0, "the CPU path drew nothing"
        _camera_is_sane(rig.mount)
    finally:
        _close(rig)


def test_a_factory_that_never_heard_of_the_keyword_is_left_alone(app):
    """An older injected factory keeps the behaviour it was written for."""
    reads, stacks = [], []
    inner = _stack_factory(reads, stacks)

    def _old_factory(path, chan, table, parent):
        return inner(path, chan, table, parent)

    state, domain = _state(), _domain()
    window = _Window(state, domain)
    host = Step1ViewerHost(stack_factory=_old_factory)
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(512, 512)
    host.show()
    app.processEvents()
    mount = Step1WholeSlideMount(window, host=host, gpu=True,
                                 gpu_layer_factory=lambda _s: _BrokenLayer())
    try:
        assert host.stack_factory is _old_factory
        mount.open("CD3")
        _settle(app)
        whole = [r for r in reads if r["wide"]]
        # Read once by the factory, and NOT a second time by the fallback.
        assert len(whole) == 1
        assert mount.host.stack.controller.has_overview_record("CD3") is True
    finally:
        mount.close()
        QtWidgets.QApplication.processEvents()


# ══ 4. the same two answers across a source change ════════════════════

def test_a_source_change_under_the_gpu_reads_no_overview_either(app):
    rig = _mount(app)
    try:
        _require_gpu(rig)
        _move_the_source(rig)
        assert rig.mount.backend == BACKEND_GPU
        assert len(rig.stacks) == 2, "the stack was not rebuilt"
        controller = rig.mount.host.stack.controller
        assert controller is rig.stacks[-1].controller
        assert _overview_reads(rig) == []
        assert controller.has_overview_record("CD3") is False
        assert controller.snapshot().overview_ready is False
        _camera_is_sane(rig.mount)
        assert rig.mount.gpu_binding.descriptor_history
    finally:
        _close(rig)


def test_a_source_change_on_the_cpu_fallback_gets_its_overview(app):
    """The known subtlety: `Step1ViewerBinding.open` calls `jump_to` while
    the controller may still be blocked on the overview, and a late
    synchronous `load_overview()` issues no request of its own. So the gate
    is not "a record exists" but "the picture actually got tiles"."""
    rig = _mount(app, broken_layer=True)
    try:
        assert rig.mount.backend == BACKEND_CPU_FALLBACK
        blitted_before = rig.mount.layer.tiles_blitted
        _move_the_source(rig)
        assert rig.mount.backend == BACKEND_CPU_FALLBACK
        assert len(rig.stacks) == 2, "the stack was not rebuilt"
        controller = rig.mount.host.stack.controller
        assert controller is rig.stacks[-1].controller
        # One read per stack, and the new one is installed.
        assert len(_overview_reads(rig)) == 2
        assert controller.has_overview_record("CD3") is True
        assert controller.snapshot().overview_ready is True
        _settle(app, rounds=80)
        assert rig.mount.layer is not None
        assert rig.mount.layer.tiles_blitted > 0, (
            "the rebuilt CPU picture never got a tile")
        assert blitted_before >= 0
        _camera_is_sane(rig.mount)
    finally:
        _close(rig)


def test_a_gpu_that_cannot_restart_hands_the_overview_to_the_cpu(app):
    """The other `_start_gpu_backend` False: the takeover worked at open
    and the rebuilt source cannot have it."""
    rig = _mount(app, break_after=1)
    try:
        _require_gpu(rig)
        assert _overview_reads(rig) == []
        _move_the_source(rig)
        assert rig.mount.backend == BACKEND_CPU_FALLBACK
        assert len(rig.stacks) == 2, "the stack was not rebuilt"
        controller = rig.mount.host.stack.controller
        assert controller is rig.stacks[-1].controller
        # Read once, on the new stack only.
        reads = _overview_reads(rig)
        assert len(reads) == 1 and reads[0]["channel"] == "CD3"
        assert controller.has_overview_record("CD3") is True
        assert controller.snapshot().overview_ready is True
        _settle(app, rounds=80)
        assert rig.mount.layer is not None
        assert rig.mount.layer.tiles_blitted > 0, (
            "the CPU picture that took over never got a tile")
        _camera_is_sane(rig.mount)
    finally:
        _close(rig)
