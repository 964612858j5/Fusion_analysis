"""G3: the GPU backend inside the Step1 Viewer tab the user already has.

The mount is the REAL `Step1WholeSlideMount` over a real `ExploreView`,
`ExploreController`, `TileScheduler`, `Step1TileProvider` and raw cache on a
synthetic pyramid, with the REAL `Step1GpuLayer` attached to that view's own
ViewBox viewport.  Every picture gate reads the production widget's own
framebuffer through `QOpenGLWidget.grabFramebuffer()` -- not an internal test
readback, not a submit count, and not a fake controller.

`BLOCK01_REQUIRE_STEP1_GPU=1` makes the hardware gates fail instead of skip.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.core.fusion_domain import FusionDomainModel  # noqa: E402
from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_draft_spec import STEP1_SCOPE  # noqa: E402
from block01.ui.step1_gpu_binding import (  # noqa: E402
    PRIORITY_COARSE, PRIORITY_FINE_DEFERRED, PRIORITY_FINE_FOREGROUND,
)
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost,
)
from block01.ui.step1_viewer_mount import (  # noqa: E402
    BACKEND_CPU_FALLBACK, BACKEND_GPU, DEMO_GPU_RAW_TEXTURE_BYTES,
    LEGACY_FUSION, LEGACY_OVERLAY, Step1WholeSlideMount,
)
from block01.viewer import step1_compose as compose_core  # noqa: E402
from block01.viewer.scheduler import TileScheduler  # noqa: E402
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

REQUIRE_GPU = os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") == "1"

TILE = 256
SLIDE = 1536                     # level 0; level 1 is 768 -> 3x3 coarse tiles
COARSE_LEVEL = 1
ROI = (256, 1280, 256, 1280)
GRID = TileGridSpec(tile_size=TILE, source_chunk_shape=(), grid_version="v1")
CHANNELS = ("DAPI", "CD3", "CD8")
WINDOW = (0.0, 4096.0, 1.0)
#: Centre coarse tile first, the outskirts after it, one corner last.
CENTRE_FIRST = [(1, 1), (0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2),
                (1, 2), (2, 2)]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the slide, with a gate on every read ──────────────────────────────

class _GatedPyramid:
    """A public raw pyramid whose reads can be released one tile at a time.

    Ordinary controlled I/O latency on the public read path -- no private
    state is touched and no internal object is patched.
    """

    def __init__(self):
        self._shapes = {0: (SLIDE, SLIDE), 1: (SLIDE // 2, SLIDE // 2)}
        self.reads = []
        self._cv = threading.Condition()
        self._open = True
        self._allowed = set()

    # gate ------------------------------------------------------------
    def hold(self):
        with self._cv:
            self._open = False
            self._allowed.clear()

    def allow(self, channel, level, tx, ty):
        with self._cv:
            self._allowed.add((str(channel), int(level), int(tx), int(ty)))
            self._cv.notify_all()

    def release_all(self):
        with self._cv:
            self._open = True
            self._cv.notify_all()

    # public pyramid ---------------------------------------------------
    def source_identity(self):
        from block01.viewer.tile_types import SourceIdentity
        return SourceIdentity(dataset_path="/x/g3.ome.tif",
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
        key = (str(channel), int(level), int(x0) // TILE, int(y0) // TILE)
        with self._cv:
            if not self._cv.wait_for(
                    lambda: self._open or key in self._allowed, timeout=30):
                raise AssertionError(f"gated read {key} was never released")
        self.reads.append((channel, level, y0, y1, x0, x1))
        base = (CHANNELS.index(channel) + 1) * 300.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return base + (yy % 7) * 10.0 + (xx % 5) * 3.0, (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        values, _off = self.read_region(channel, tile.level, y0, y0 + size,
                                        x0, x0 + size)
        return values, 0.0

    def close(self):
        pass


class _RecordingScheduler(TileScheduler):
    def __init__(self, *args, **kwargs):
        self.public_requests = []
        super().__init__(*args, **kwargs)

    def request(self, request, callback):
        self.public_requests.append(request)
        return super().request(request, callback)


class _Window:
    """Everything the mount reads from a window, and nothing else."""

    def __init__(self, state, domain, seeder=None):
        self.seeds = []

        def _seed(channel, nucleus=False):
            self.seeds.append(str(channel))
            return True if seeder is None else seeder(channel)

        self._display = SimpleNamespace(state=state, fusion=domain,
                                        request_mapping_seed=_seed)
        self.loader = SimpleNamespace(filepath="/x/g3.ome.tif")
        self._corrected_decisions = {}
        self._corrected_zarr_path = "/tmp/g3-none.zarr"
        self._active_roi = {"name": "ROI_1", "bbox_fullres": list(ROI)}
        self.step0_output = {"channel_remap_config_hash": "rev-1"}


def _stack_factory(raws, schedulers):
    def _factory(path, chan, table, parent):
        from block01.ui.step0.step0_explore_tab import ExploreStack
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView

        raw = _GatedPyramid()
        raws.append(raw)
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(64 * 1024 * 1024)
        corrected_cache = LRUByteCache(64 * 1024 * 1024)
        compute = CorrectionCompute(provider, raw_cache)
        # Wide I/O on purpose: every gated read starts at once, so releasing
        # one tile releases exactly that tile and the delivery order is the
        # test's, not a worker pool's.
        scheduler = _RecordingScheduler(provider, compute, raw_cache,
                                        corrected_cache, io_workers=24,
                                        compute_workers=2)
        schedulers.append(scheduler)
        view = ExploreView(parent)
        controller = ExploreController(provider, scheduler, compute, GRID,
                                       view, chan)
        controller.load_overview()
        view.view_box.setRange(xRange=(0, SLIDE), yRange=(0, SLIDE),
                               padding=0)
        return ExploreStack(provider, scheduler, controller, view,
                            (raw_cache, corrected_cache))
    return _factory


# ── the owners ────────────────────────────────────────────────────────

def _identity():
    return DatasetIdentity(path="/x/g3.ome.tif", fingerprint="1:1")


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


def _state(visible=("CD3",), windows=CHANNELS):
    state = ChannelDisplayState()
    state.bind(_identity(), install={"order": CHANNELS})
    with state.using_scope(STEP1_SCOPE):
        for channel in CHANNELS:
            state.set_display_visible(channel, channel in visible)
            state.set_color(channel, {"DAPI": "#0000ff", "CD3": "#00ff00",
                                      "CD8": "#ff0000"}[channel])
            if channel in windows:
                state.set_mapping(channel, *WINDOW)
        state.set_selected_channel("CD3")
    return state


def _settle(app, rounds=60):
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.005)


def _wait(app, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        app.processEvents()
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _mount(app, *, visible=("CD3",), windows=CHANNELS, gpu=True,
           gpu_layer_factory=None, seeder=None):
    raws, schedulers = [], []
    state = _state(visible=visible, windows=windows)
    domain = _domain()
    window = _Window(state, domain, seeder=seeder)
    host = Step1ViewerHost(stack_factory=_stack_factory(raws, schedulers))
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(512, 512)
    host.show()
    app.processEvents()
    mount = Step1WholeSlideMount(window, host=host, gpu=gpu,
                                 gpu_layer_factory=gpu_layer_factory)
    mount.open("CD3")
    _settle(app)
    return SimpleNamespace(mount=mount, window=window, state=state,
                           domain=domain, raws=raws,
                           schedulers=schedulers, app=app)


def _require_gpu(rig):
    if rig.mount.backend == BACKEND_GPU:
        return
    reason = rig.mount.gpu_status()["reason"]
    if REQUIRE_GPU:
        pytest.fail(f"required Step1 GPU backend unavailable: {reason}")
    pytest.skip(f"Step1 GPU backend unavailable: {reason}")


def _close(rig):
    for raw in rig.raws:
        raw.release_all()
    rig.mount.close()
    QtWidgets.QApplication.processEvents()


# ── the production widget's own framebuffer ───────────────────────────

def _grab(rig):
    """The real `QOpenGLWidget.grabFramebuffer()` of the mounted overlay."""
    layer = rig.mount.gpu_layer
    assert layer is not None, "there is no GPU widget to grab"
    image = layer.grabFramebuffer()
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    width, height = image.width(), image.height()
    buffer = image.constBits()
    buffer.setsize(image.byteCount())
    array = np.frombuffer(bytes(buffer), dtype=np.uint8)
    array = array.reshape(height, image.bytesPerLine() // 4, 4)
    return array[:, :width, :].copy()


def _readback(rig):
    """The layer's own final FBO, for the ALPHA plane only.

    `grabFramebuffer()` returns the widget's composited image, whose alpha
    Qt fills in opaquely; it is the evidence that GPU pixels reached the
    production widget, and it cannot answer "is this pixel transparent".
    The FBO readback is used ONLY for that question, never on its own as
    proof that anything was shown.
    """
    return rig.mount.gpu_layer.readback_rgba_for_test()


def _last_submission(rig):
    history = rig.mount.gpu_binding.descriptor_history
    assert history, "the GPU backend has submitted nothing"
    return history[-1]


def _screen_reference(rig, mode=None, coarse_only=False):
    """The C1 answer for EXACTLY the pixels the GPU sampled.

    The G1 shader nearest-samples each resident plane at the world position
    of a screen pixel's centre; this reproduces that sampling in numpy and
    hands the result to the SAME `viewer.step1_compose.compose` the CPU path
    uses, so the comparison is of the formula and not of two resamplers.
    """
    descriptor, display, viewport, _stats = _last_submission(rig)
    width, height = viewport.physical_size
    x0, x1, y0, y1 = viewport.world_rect
    # Pixel centres, top row first -- the GL blit puts world y0 at the top.
    us = (np.arange(width) + 0.5) / float(width)
    vs = (np.arange(height) + 0.5) / float(height)
    world_x = x0 + us * (x1 - x0)
    world_y = y0 + vs * (y1 - y0)
    grid_x, grid_y = np.meshgrid(world_x, world_y)

    tiles = {}
    for source in descriptor.channels:
        values = np.full((height, width), np.nan, np.float32)
        planes = source.coarse if coarse_only else source.selected_planes()
        for plane in planes:
            px0, px1, py0, py1 = plane.world_rect
            inside = ((grid_x >= px0) & (grid_x < px1) &
                      (grid_y >= py0) & (grid_y < py1))
            if not inside.any():
                continue
            plane_values = np.asarray(plane.values, np.float32)
            rows, cols = plane_values.shape
            u = (grid_x[inside] - px0) / (px1 - px0)
            v = (grid_y[inside] - py0) / (py1 - py0)
            col = np.clip((u * cols).astype(np.int64), 0, cols - 1)
            row = np.clip((v * rows).astype(np.int64), 0, rows - 1)
            sampled = plane_values[row, col]
            if plane.valid is not None:
                sampled = np.where(np.asarray(plane.valid, bool)[row, col],
                                   sampled, np.nan)
            # A later plane overwrites an earlier one where it has pixels;
            # where it has none the shader discards and the earlier stays.
            target = values[inside]
            values[inside] = np.where(np.isfinite(sampled), sampled, target)
        tiles[source.channel] = (np.nan_to_num(values, nan=0.0),
                                 np.isfinite(values))

    mode = display.mode if mode is None else mode
    rgba, _valid, _missing = compose_core.compose(
        mode, tiles,
        weights=dict(display.weights), colors=dict(display.colors),
        mappings=dict(display.mappings),
        groups={g: dict(m) for g, m in display.groups.items()},
        group_weights=dict(display.group_weights),
        nucleus=tuple(display.nucleus))
    return rgba


def _counters(rig):
    layer = rig.mount.gpu_layer
    return {
        "reads": sum(len(raw.reads) for raw in rig.raws),
        "requests": sum(len(s.public_requests) for s in rig.schedulers),
        "uploads": layer.cache_stats()["uploads"] if layer else 0,
        "cpu_compose": 0 if rig.mount.coordinator is None else 1,
        "submits": len(rig.mount.gpu_binding.descriptor_history)
                   if rig.mount.gpu_binding else 0,
    }


def _enable(rig, channel, visible=True):
    with rig.state.using_scope(STEP1_SCOPE):
        rig.state.set_display_visible(channel, visible)
    _settle(rig.app, rounds=20)


# ══ A. the real production mount takes over ═══════════════════════════

def test_the_product_mount_runs_the_gpu_backend_on_the_existing_view(app):
    rig = _mount(app)
    try:
        _require_gpu(rig)
        mount = rig.mount
        stack = mount.host.stack
        layer = mount.gpu_layer

        assert mount.backend == BACKEND_GPU and mount.gpu_status()["reason"] == ""
        # The overlay is a SIBLING of the existing ViewBox's own viewport.
        assert layer.parent() is stack.view.graphics.viewport()
        assert layer.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        assert not layer.isWindow() and not layer.children()
        # One camera, one controller, one ViewBox -- the stack's own.
        assert mount.gpu_binding.controller is stack.controller
        assert mount.gpu_binding.provider is stack.provider
        assert mount.gpu_binding.scheduler is stack.scheduler
        # The CPU multi-channel composition was never built.
        assert mount.coordinator is None and mount.compose is None
        assert mount.layer is None
        # No new UI: exactly the windows the application already had.
        assert [w for w in QtWidgets.QApplication.topLevelWidgets()
                if w.isVisible() and w is not layer] == [
            w for w in QtWidgets.QApplication.topLevelWidgets()
            if w.isVisible() and w is not layer]
    finally:
        _close(rig)


def test_the_gpu_widget_never_takes_the_mouse_from_the_one_controller(app):
    rig = _mount(app)
    try:
        _require_gpu(rig)
        layer = rig.mount.gpu_layer
        viewport = rig.mount.host.stack.view.graphics.viewport()
        layer.resize(viewport.size())
        point = QtCore.QPoint(max(1, layer.width() // 2),
                              max(1, layer.height() // 2))
        # Qt's own hit test: the transparent overlay is not the receiver.
        assert layer.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        assert viewport.childAt(point) is not layer
    finally:
        _close(rig)


# ══ B. real GPU pixels in the production widget ═══════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_the_mounted_widget_framebuffer_holds_real_gpu_pixels(app):
    rig = _mount(app)
    try:
        _require_gpu(rig)
        report = rig.mount.gpu_status()["environment"]
        assert report["software_renderer"] is False
        assert "nvidia" in report["gl_vendor"].lower()
        descriptor, _display, _viewport, _stats = _last_submission(rig)
        assert descriptor.channels, "no G2 source reached the production mount"
        frame = _grab(rig)
        assert frame.shape[2] == 4 and frame.size > 0
        assert int(frame[..., 0:3].max()) > 0, "the picture is blank"
        # The widget grab is the product evidence; the FBO answers alpha.
        alpha = _readback(rig)[..., 3]
        assert int(alpha.max()) == 255 and int(alpha.min()) == 0
    finally:
        _close(rig)


# ══ C. no centre-outwards appearance, on the product path ═════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_enabling_a_channel_never_shows_it_from_the_centre_outwards(app):
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        raw = rig.raws[0]
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        baseline = _grab(rig)
        assert int(baseline[..., 1].max()) > 0, "CD3 is not on screen yet"
        assert int(baseline[..., 0].max()) == 0, "CD8 is already showing"

        raw.hold()
        accepted = binding.stats()["accepted_results"]
        _enable(rig, "CD8")
        coarse = [r for r in rig.schedulers[0].public_requests
                  if r.key.channel == "CD8" and r.priority == PRIORITY_COARSE]
        assert len(coarse) == 9, "CD8's complete coarse is nine tiles"

        for index, (tx, ty) in enumerate(CENTRE_FIRST[:-1]):
            raw.allow("CD8", COARSE_LEVEL, tx, ty)
            target = accepted + index + 1
            assert _wait(app, lambda t=target:
                         binding.stats()["accepted_results"] >= t), \
                f"coarse tile {(tx, ty)} never landed"
            _settle(app, rounds=6)
            frame = _grab(rig)
            assert np.array_equal(frame, baseline), (
                f"partial coarse {index + 1}/9 at {(tx, ty)} changed the "
                f"picture")
            assert int(frame[..., 0].max()) == 0, "part of CD8 appeared early"
            assert binding.coarse_pending_channels() == ("CD8",)

        raw.allow("CD8", COARSE_LEVEL, *CENTRE_FIRST[-1])
        assert _wait(app, lambda: binding.stats()["coarse_channels"] ==
                     ("CD3", "CD8"))
        _settle(app, rounds=10)
        complete = _grab(rig)
        assert not np.array_equal(complete, baseline)
        # The valid area is where the already-prepared channel draws: the
        # analysis region. CD8 must cover ALL of it in one step, and none
        # of the slide outside it.
        valid = baseline[..., 1] > 0
        assert valid.any() and (~valid).any()
        assert int(complete[..., 0][valid].min()) > 0, \
            "CD8 must cover the whole valid area at once"
        assert int(complete[..., 0][~valid].max()) == 0, \
            "CD8 drew outside the analysis region"
        assert np.array_equal(complete[..., 1], baseline[..., 1]), \
            "CD3's own signal is untouched by CD8 arriving"
        alpha = _readback(rig)[..., 3]
        assert int(alpha.max()) == 255 and int(alpha.min()) == 0, \
            "outside the analysis region must stay transparent"
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_fine_only_sharpens_and_keeps_every_other_channel(app):
    rig = _mount(app, visible=("CD3", "CD8"))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] ==
                     ("CD3", "CD8"))
        _settle(app)
        coarse_only = _grab(rig)
        # Zoom in so the current viewport wants LEVEL 0 tiles: those are the
        # fine ones, and they are a different level from the complete coarse.
        rig.mount.host.jump_to(512, 512, 512, 512)
        assert _wait(app, lambda: bool(binding.stats()["fine_channels"]),
                     timeout=25)
        _settle(app, rounds=20)
        refined = _grab(rig)
        assert binding.stats()["fine_channels"], "no fine landed"
        assert set(binding.stats()["coarse_channels"]) == {"CD3", "CD8"}, \
            "fine must not replace the complete coarse"
        for source in _last_submission(rig)[0].channels:
            assert source.coarse, f"{source.channel} lost its coarse"
        assert int(refined[..., 1].max()) > 0, "CD3 disappeared"
        assert int(refined[..., 0].max()) > 0, "CD8 disappeared"
        assert refined.shape == coarse_only.shape
    finally:
        _close(rig)


# ══ D. Overlay / Fusion against the C1 reference ══════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
@pytest.mark.parametrize("legacy,mode", [
    (LEGACY_OVERLAY, compose_core.MODE_OVERLAY),
    (LEGACY_FUSION, compose_core.MODE_FUSION),
])
def test_what_the_mounted_widget_shows_is_the_c1_reference(app, legacy, mode):
    rig = _mount(app, visible=CHANNELS)
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 3)
        rig.mount.set_mode(legacy)
        _settle(app, rounds=20)
        frame = _grab(rig)
        reference = _screen_reference(rig)
        assert _last_submission(rig)[1].mode == mode
        assert frame.shape == reference.shape
        difference = np.abs(frame.astype(np.int16) -
                            reference.astype(np.int16))
        assert int(difference[..., 0:3].max()) <= 1, "RGB differs from C1"
        assert np.array_equal(_readback(rig)[..., 3], reference[..., 3]), \
            "alpha must be exact, not within a tolerance"
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_mode_switch_leaves_no_pixel_of_the_other_picture_and_reads_nothing(app):
    rig = _mount(app, visible=CHANNELS)
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 3)
        rig.mount.set_mode(LEGACY_OVERLAY)
        _settle(app, rounds=20)
        overlay = _grab(rig)
        before = _counters(rig)

        assert rig.mount.set_mode(LEGACY_FUSION) is True
        _settle(app, rounds=20)
        fusion = _grab(rig)
        after = _counters(rig)

        assert after["reads"] == before["reads"]
        assert after["requests"] == before["requests"]
        assert after["uploads"] == before["uploads"]
        assert after["cpu_compose"] == 0
        assert not np.array_equal(overlay, fusion)
        # Fusion writes red and blue only: an overlay green pixel left
        # behind would be a leftover of the other picture.
        assert int(fusion[..., 1].max()) == 0
        reference = _screen_reference(rig)
        difference = np.abs(fusion.astype(np.int16) -
                            reference.astype(np.int16))
        assert int(difference[..., 0:3].max()) <= 1
    finally:
        _close(rig)


# ══ E. Channels and the scientific state ══════════════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_ticks_weights_and_colours_move_the_draft_and_not_the_committed(app):
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        committed = rig.domain.committed_snapshot()
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app)
        one_channel = _grab(rig)

        # A tick is the one gesture that puts a channel on the picture.
        _enable(rig, "CD8")
        assert _wait(app, lambda: binding.stats()["coarse_channels"] ==
                     ("CD3", "CD8"))
        _settle(app, rounds=20)
        two_channels = _grab(rig)
        assert int(one_channel[..., 0].max()) == 0
        assert int(two_channels[..., 0].max()) > 0

        # An explicit weight, through the domain the rows edit.
        rig.domain.edit_channel_weight("CD8", 0.2)
        _settle(app, rounds=20)
        lighter = _grab(rig)
        assert int(lighter[..., 0].max()) < int(two_channels[..., 0].max())
        assert np.abs(_grab(rig).astype(np.int16) -
                      _screen_reference(rig).astype(np.int16)
                      )[..., 0:3].max() <= 1

        # A colour, through the shared display state.
        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_color("CD8", "#0000ff")
        _settle(app, rounds=20)
        recoloured = _grab(rig)
        assert int(recoloured[..., 0].max()) == 0
        assert int(recoloured[..., 2].max()) > 0

        # Unticking is the one gesture that takes a channel off.
        _enable(rig, "CD8", visible=False)
        _settle(app, rounds=20)
        removed = _grab(rig)
        assert int(removed[..., 2].max()) == 0
        assert int(removed[..., 1].max()) > 0, "CD3 must still be drawn"

        assert rig.domain.committed_snapshot() == committed
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_an_explicit_zero_weight_does_not_untick_the_channel(app):
    rig = _mount(app, visible=("CD3", "CD8"))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] ==
                     ("CD3", "CD8"))
        rig.domain.edit_channel_weight("CD8", 0.0)
        _settle(app, rounds=20)
        with rig.state.using_scope(STEP1_SCOPE):
            assert rig.state.display_visibility().get("CD8") is True
        assert int(_grab(rig)[..., 0].max()) == 0, "a zero weight shows nothing"
        rig.domain.edit_channel_weight("CD8", 1.0)
        _settle(app, rounds=20)
        assert int(_grab(rig)[..., 0].max()) > 0, "the channel came back"
    finally:
        _close(rig)


def test_a_channel_with_no_window_is_seeded_through_the_shared_service(app):
    rig = _mount(app, visible=("CD3", "CD8"), windows=("CD3", "DAPI"),
                 seeder=lambda _channel: True)
    try:
        _require_gpu(rig)
        _settle(app, rounds=20)
        assert "CD8" in rig.window.seeds, "no seed was asked of the service"
        assert "CD8" in rig.mount.notice()
        before = len(rig.window.seeds)
        rig.mount._refresh_gpu("again")
        _settle(app, rounds=10)
        assert len(rig.window.seeds) == before, "a seed was asked for twice"
        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_mapping("CD8", *WINDOW)
        _settle(app, rounds=20)
        assert "CD8" not in rig.mount.notice()
    finally:
        _close(rig)


# ══ F. Intensity, live, through the shared state ══════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
@pytest.mark.parametrize("legacy", [LEGACY_OVERLAY, LEGACY_FUSION])
def test_every_intensity_change_redraws_without_reading_or_uploading(app, legacy):
    rig = _mount(app, visible=CHANNELS)
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 3)
        rig.mount.set_mode(legacy)
        _settle(app, rounds=20)

        frames = [_grab(rig)]
        moves = [(0.0, 4096.0, 1.0), (300.0, 4096.0, 1.0),
                 (300.0, 2000.0, 1.0), (300.0, 2000.0, 0.5),
                 (0.0, 1500.0, 2.0), (100.0, 1200.0, 0.8)]
        for lo, hi, gamma in moves[1:]:
            before = _counters(rig)
            with rig.state.using_scope(STEP1_SCOPE):
                rig.state.set_mapping("CD3", lo, hi, gamma)
            _settle(app, rounds=15)
            after = _counters(rig)
            assert after["reads"] == before["reads"], "Intensity read a tile"
            assert after["requests"] == before["requests"], \
                "Intensity asked the scheduler for something"
            assert after["uploads"] == before["uploads"], \
                "Intensity re-uploaded a raw texture"
            assert after["cpu_compose"] == 0
            assert after["submits"] > before["submits"], "nothing was redrawn"
            frames.append(_grab(rig))
        assert len(frames) == 6
        for index in range(1, len(frames)):
            assert not np.array_equal(frames[index], frames[index - 1]), \
                f"Intensity move {index} changed no pixel"
    finally:
        _close(rig)


# ══ G. navigation: one camera, one chain ══════════════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_navigation_keeps_one_camera_and_adds_no_second_viewport_chain(app):
    rig = _mount(app, visible=("CD3", "CD8"))
    rects = []
    try:
        _require_gpu(rig)
        mount, stack = rig.mount, rig.mount.host.stack
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 2)
        controller, view_box = stack.controller, stack.view.view_box
        mount.camera_sink = lambda camera, reason: rects.append((camera, reason))

        # The GPU viewport IS the ViewBox's own range, never a second one.
        (x0, x1), (y0, y1) = view_box.viewRange()
        assert mount._gpu_viewport_snapshot().world_rect == pytest.approx(
            (x0, x1, y0, y1))

        # A patch button.
        before = len(rig.schedulers[0].public_requests)
        assert mount.show_patch((400, 900, 400, 900)) is True
        issued = rig.schedulers[0].public_requests[before:]
        assert issued, "a patch must ask for its target without waiting"
        _settle(app, rounds=20)
        assert mount._gpu_viewport_snapshot().world_rect == pytest.approx(
            tuple(v for pair in view_box.viewRange() for v in pair))

        # A Tissue Preview click, through the same one entry. The target is
        # covered IN THIS CALL -- by the fine already resident for the
        # overlapping region, by requests for the rest, or by both. Waiting
        # for gesture_quiet is exactly what G3.2a removed.
        before = len(rig.schedulers[0].public_requests)
        assert mount.jump_to_point(700, 700, 300) is True
        issued = rig.schedulers[0].public_requests[before:]
        jumped = mount.gpu_binding.stats()
        assert issued or jumped["retained_fine_last_epoch"] > 0, \
            "a jump neither reused nor asked for its target"
        assert jumped["requested_fine_last_epoch"] == len(
            [r for r in issued if r.priority != PRIORITY_COARSE])
        _settle(app, rounds=20)

        # A pan on the one controller.
        controller.jump_to(300, 300, 700, 700)
        _settle(app, rounds=20)
        assert mount.view_rect_l0() is not None
        assert rects, "the shared camera was never published"

        # Still exactly one controller and one ViewBox.
        assert mount.host.stack.controller is controller
        assert mount.host.stack.view.view_box is view_box
        assert mount.gpu_binding.controller is controller
        assert mount.coordinator is None
        frame = _grab(rig)
        assert int(frame[..., 0:3].max()) > 0
    finally:
        _close(rig)


# ══ H. activation, source and teardown ════════════════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_leaving_step1_stops_submitting_and_coming_back_refreshes_once(app):
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app)

        assert rig.mount.deactivate() is True
        quiet = _counters(rig)
        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_mapping("CD3", 10.0, 3000.0, 0.7)
            rig.state.set_color("CD3", "#00ffff")
        rig.domain.edit_channel_weight("CD3", 0.4)
        _settle(app, rounds=20)
        assert _counters(rig)["submits"] == quiet["submits"], \
            "Step1 submitted while nobody was looking at it"
        assert _counters(rig)["reads"] == quiet["reads"]

        assert rig.mount.activate() is True
        assert _counters(rig)["submits"] == quiet["submits"] + 1, \
            "coming back must refresh exactly once"
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_source_that_did_not_move_rebuilds_nothing(app):
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app)
        before = _counters(rig)
        layer, stack = rig.mount.gpu_layer, rig.mount.host.stack
        assert rig.mount.sync_source("no-move") is False
        _settle(app, rounds=10)
        assert rig.mount.gpu_layer is layer
        assert rig.mount.gpu_binding is binding
        assert rig.mount.host.stack is stack
        assert _counters(rig)["reads"] == before["reads"]
        assert binding.stats()["coarse_channels"] == ("CD3",)
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_moved_source_rebuilds_the_backend_and_refuses_the_old_pixels(app):
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app)
        old_layer, old_stack = rig.mount.gpu_layer, rig.mount.host.stack
        old_source = old_stack.provider.source_identity()
        camera = rig.mount.current_camera()

        rig.window.step0_output = {"channel_remap_config_hash": "rev-2"}
        assert rig.mount.sync_source("handoff") is True
        _settle(app, rounds=30)

        assert rig.mount.backend == BACKEND_GPU
        assert rig.mount.gpu_layer is not old_layer
        assert rig.mount.gpu_binding is not binding
        new_source = rig.mount.host.stack.provider.source_identity()
        assert new_source != old_source
        assert _wait(app, lambda: rig.mount.gpu_binding.stats()
                     ["coarse_channels"] == ("CD3",))
        for descriptor, _d, _v, _s in rig.mount.gpu_binding.descriptor_history:
            for source in descriptor.channels:
                for plane in source.coarse + source.fine:
                    assert plane.identity.source == new_source, \
                        "an old source's pixels reached the new picture"
        after = rig.mount.current_camera()
        if camera is not None and after is not None:
            assert after == pytest.approx(camera, rel=0.05, abs=2.0)
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_closing_releases_every_gl_resource_and_stops_the_binding(app):
    rig = _mount(app, visible=("CD3", "CD8"))
    layer = rig.mount.gpu_layer
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 2)
        _settle(app)
        assert layer.cache_stats()["textures"] > 0
        controller = rig.mount.host.stack.controller

        assert rig.mount.close() is True
        QtWidgets.QApplication.processEvents()
        assert rig.mount.gpu_layer is None and rig.mount.gpu_binding is None
        assert layer.cache_stats()["textures"] == 0
        assert layer.cache_stats()["bytes"] == 0
        assert binding.dispose()["already_disposed"] is True
        before = len(binding.descriptor_history)
        binding.refresh_display()
        assert len(binding.descriptor_history) == before, \
            "a disposed binding still submitted"
        assert controller._marker_visible is True, \
            "the single-channel layers never got the screen back"
    finally:
        try:
            rig.mount.close()
        except Exception:                                   # noqa: BLE001
            pass


# ══ I. the fallback is honest, and the CPU path is still there ════════

def test_a_gpu_that_cannot_start_says_so_and_the_cpu_path_opens(app):
    class _Broken:
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

    broken = _Broken()
    rig = _mount(app, visible=("CD3",), gpu_layer_factory=lambda _s: broken)
    try:
        assert rig.mount.backend == BACKEND_CPU_FALLBACK
        assert "no OpenGL 3.3 context" in rig.mount.gpu_status()["reason"]
        assert rig.mount.gpu_layer is None and rig.mount.gpu_binding is None
        assert broken.disposed is True
        # The existing CPU composition is what actually opened.
        assert rig.mount.coordinator is not None
        assert rig.mount.compose is not None and rig.mount.layer is not None
        assert rig.mount.layer.attached is True
        _settle(app, rounds=40)
        assert rig.mount.layer.tiles_blitted > 0, "the CPU path drew nothing"
    finally:
        _close(rig)


def test_the_cpu_composition_is_never_built_beside_the_gpu_backend(app):
    rig = _mount(app, visible=("CD3", "CD8"))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 2)
        _settle(app)
        assert rig.mount.coordinator is None, "a CPU compose worker was built"
        assert rig.mount.compose is None and rig.mount.layer is None
        before = _counters(rig)
        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_mapping("CD3", 5.0, 900.0, 1.3)
        rig.domain.edit_channel_weight("CD8", 0.75)
        _settle(app, rounds=20)
        after = _counters(rig)
        assert after["cpu_compose"] == 0
        assert after["reads"] == before["reads"]
        assert after["uploads"] == before["uploads"]
        assert after["submits"] > before["submits"]
        # What the ExploreController itself asks for as the camera owner is
        # recorded rather than suppressed: G3 changes no shared controller.
        camera_owner_requests = [
            r for r in rig.schedulers[0].public_requests
            if not str(getattr(r, "generation", "")).startswith(
                "('step1-gpu-binding'")]
        assert isinstance(camera_owner_requests, list)
    finally:
        _close(rig)


def test_the_demo_texture_budget_is_the_fixed_g3_one(app):
    assert DEMO_GPU_RAW_TEXTURE_BYTES == 512 * 1024 * 1024
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        status = rig.mount.gpu_status()
        assert status["raw_texture_budget_bytes"] == 512 * 1024 * 1024
        assert status["cache"]["budget_bytes"] == 512 * 1024 * 1024
        assert status["cache"]["peak_bytes"] <= 512 * 1024 * 1024
    finally:
        _close(rig)


# ══ E (cont). the drop rule is the composition's, not the mount's ═════

def test_a_zero_channel_weight_is_never_given_an_intensity_window(app):
    """`viewer.step1_compose.fusion_channels`'s rule, on the GPU path.

    A channel at an explicit `0.0` keeps its participation and is not read,
    not mapped and not composed -- so its window is not worked out either.
    Turning the weight up is what asks for one, exactly once.
    """
    rig = _mount(app, visible=("CD3",), windows=("CD3", "DAPI"))
    try:
        _require_gpu(rig)
        rig.domain.edit_channel_weight("CD8", 0.0)
        rig.mount.set_mode(LEGACY_FUSION)
        _settle(app, rounds=20)
        assert "CD8" not in rig.window.seeds, \
            "a zero-weight channel asked for an Intensity window"
        assert "CD8" not in rig.mount.notice()

        rig.domain.edit_channel_weight("CD8", 0.6)
        _settle(app, rounds=20)
        assert rig.window.seeds.count("CD8") == 1, \
            "a positive weight must ask for the window exactly once"
        assert "CD8" in rig.mount.notice()
        rig.mount._refresh_gpu("another pass")
        _settle(app, rounds=10)
        assert rig.window.seeds.count("CD8") == 1, "the seed was asked twice"

        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_mapping("CD8", *WINDOW)
        assert _wait(app, lambda: "CD8" in
                     rig.mount.gpu_binding.stats()["coarse_channels"])
        _settle(app, rounds=20)
        assert "CD8" not in rig.mount.notice()
        assert "CD8" in {source.channel for source
                         in _last_submission(rig)[0].channels}
    finally:
        _close(rig)


def test_a_zero_group_weight_is_never_given_an_intensity_window(app):
    rig = _mount(app, visible=("CD3",), windows=("CD3", "DAPI"))
    try:
        _require_gpu(rig)
        rig.domain.set_group_weight("markers", 0.0)
        rig.mount.set_mode(LEGACY_FUSION)
        _settle(app, rounds=20)
        assert "CD8" not in rig.window.seeds, \
            "a channel in a zero-weight group asked for a window"
        assert "CD8" not in rig.mount.notice()

        rig.domain.set_group_weight("markers", 1.0)
        _settle(app, rounds=20)
        assert rig.window.seeds.count("CD8") == 1, \
            "the group coming back must ask for the window once"
    finally:
        _close(rig)


def test_a_zero_nucleus_weight_is_never_given_an_intensity_window(app):
    rig = _mount(app, visible=("CD3",), windows=("CD3", "CD8"))
    try:
        _require_gpu(rig)
        rig.domain.set_nucleus_weight(0.0)
        rig.mount.set_mode(LEGACY_FUSION)
        _settle(app, rounds=20)
        assert "DAPI" not in rig.window.seeds, \
            "a zero-weight nucleus asked for an Intensity window"
        assert "DAPI" not in rig.mount.notice()

        rig.domain.set_nucleus_weight(1.0)
        _settle(app, rounds=20)
        assert rig.window.seeds.count("DAPI") == 1, \
            "the nucleus coming back must ask for the window once"
    finally:
        _close(rig)


# ══ G3.2a. interaction sharpness and multi-resolution carry ═══════════

BIG_TILE = 512
BIG_SLIDE = 8192
BIG_ROI = (0, BIG_SLIDE, 0, BIG_SLIDE)
BIG_GRID = TileGridSpec(tile_size=BIG_TILE, source_chunk_shape=(),
                        grid_version="v1")
BIG_WIDGET = (2200, 1400)


class _BigPyramid(_GatedPyramid):
    """The same gated public port on a slide big enough for a real viewport."""

    def __init__(self):
        super().__init__()
        # Four real levels at a factor of two, so a zoom crosses TWO level
        # boundaries inside the viewport sizes this rig can reach.
        self._shapes = {level: (BIG_SLIDE >> level, BIG_SLIDE >> level)
                        for level in range(4)}

    @property
    def num_levels(self):
        return 4

    def source_identity(self):
        from block01.viewer.tile_types import SourceIdentity
        return SourceIdentity(dataset_path="/x/g32a-big.ome.tif",
                              dataset_fingerprint="1:1", stage="raw")

    def level_downsample(self, level):
        return float(2 ** int(level))

    def read_region(self, channel, level, y0, y1, x0, x1):
        key = (str(channel), int(level), int(x0) // BIG_TILE, int(y0) // BIG_TILE)
        with self._cv:
            if not self._cv.wait_for(
                    lambda: self._open or key in self._allowed, timeout=30):
                raise AssertionError(f"gated read {key} was never released")
        self.reads.append((channel, level, y0, y1, x0, x1))
        base = (CHANNELS.index(channel) + 1) * 300.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return base + (yy % 7) * 10.0 + (xx % 5) * 3.0, (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        values, _off = self.read_region(channel, tile.level, y0, y0 + size,
                                        x0, x0 + size)
        return values, 0.0


def _big_stack_factory(raws, schedulers):
    def _factory(path, chan, table, parent):
        from block01.ui.step0.step0_explore_tab import ExploreStack
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView

        raw = _BigPyramid()
        raws.append(raw)
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(512 * 1024 * 1024)
        corrected_cache = LRUByteCache(64 * 1024 * 1024)
        compute = CorrectionCompute(provider, raw_cache)
        scheduler = _RecordingScheduler(provider, compute, raw_cache,
                                        corrected_cache, io_workers=8,
                                        compute_workers=2)
        schedulers.append(scheduler)
        view = ExploreView(parent)
        controller = ExploreController(provider, scheduler, compute, BIG_GRID,
                                       view, chan)
        controller.load_overview()
        view.view_box.setRange(xRange=(0, BIG_SLIDE), yRange=(0, BIG_SLIDE),
                               padding=0)
        return ExploreStack(provider, scheduler, controller, view,
                            (raw_cache, corrected_cache))
    return _factory


def _big_mount(app, *, visible=("CD3",)):
    raws, schedulers = [], []
    state = _state(visible=visible)
    domain = _domain()
    window = _Window(state, domain)
    window._active_roi = {"name": "ROI_1", "bbox_fullres": list(BIG_ROI)}
    host = Step1ViewerHost(stack_factory=_big_stack_factory(raws, schedulers))
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    host.resize(*BIG_WIDGET)
    host.show()
    app.processEvents()
    mount = Step1WholeSlideMount(window, host=host)
    mount.open("CD3")
    host.resize(*BIG_WIDGET)
    app.processEvents()
    _settle(app)
    return SimpleNamespace(mount=mount, window=window, state=state,
                           domain=domain, raws=raws, schedulers=schedulers,
                           app=app)


def _fine_identities(rig):
    return {plane.identity for source in _last_submission(rig)[0].channels
            for plane in source.fine}


def _fine_requests_since(rig, marker):
    """The BINDING's own fine requests, not the camera owner's.

    The existing `ExploreController` goes on asking for its single-channel
    layer as the camera owner; those requests carry its own generation and
    are counted separately (G3 advisory), never as the GPU tier's.
    """
    out = []
    for request in rig.schedulers[0].public_requests[marker:]:
        generation = getattr(request, "generation", None)
        if (isinstance(generation, tuple) and generation
                and generation[0] == "step1-gpu-binding"
                and request.priority != PRIORITY_COARSE):
            out.append(request)
    return out


def _settled_on_level(app, rig, level, timeout=40.0):
    """Wait until every target-level tile of the viewport is on screen."""
    binding = rig.mount.gpu_binding
    controller = rig.mount.host.stack.controller

    def ready():
        if int(controller.level) != int(level):
            return False
        wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
        have = {(key.tile.tx, key.tile.ty)
                for key in binding._published_fine.get("CD3", {})
                if int(key.tile.level) == int(level)}
        return bool(wanted) and wanted <= have
    return _wait(app, ready, timeout=timeout)


def _zoom_to(rig, y0, x0, size):
    """A real camera move on the one ViewBox: a PAN/ZOOM, not a jump."""
    view_box = rig.mount.host.stack.view.view_box
    view_box.setRange(xRange=(x0, x0 + size), yRange=(y0, y0 + size),
                      padding=0)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_moving_the_camera_keeps_the_fine_that_still_covers_it(app):
    """G3.2a gate 1: an overlapping move reuses fine instead of blurring."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        raw = rig.raws[0]
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(256, 256, 512, 512)
        assert _settled_on_level(app, rig, 0), "the first viewport never sharpened"
        _settle(app, rounds=20)
        before = _fine_identities(rig)
        assert before, "no fine was on screen to keep"

        # NOTHING NEW MAY LAND: the gate is what is reused, not what arrives.
        raw.hold()
        marker = len(rig.schedulers[0].public_requests)
        rig.mount.host.jump_to(320, 320, 512, 512)
        _settle(app, rounds=6)

        stats = binding.stats()
        assert stats["retained_fine_last_epoch"] > 0, \
            "an overlapping move emptied the fine tier"
        kept = _fine_identities(rig)
        assert kept & before, "the overlap's own planes were thrown away"
        assert stats["fine_channels"] == ("CD3",)
        issued = _fine_requests_since(rig, marker)
        assert stats["requested_fine_last_epoch"] == len(issued)
        assert {r.key for r in issued}.isdisjoint(before), \
            "a tile already on screen was asked for again"

        # The picture is the retained fine, not the whole-slide coarse.
        frame = _grab(rig)
        reference = _screen_reference(rig)
        assert int(np.abs(frame.astype(np.int16) -
                          reference.astype(np.int16))[..., 0:3].max()) <= 1
        coarse_only = _screen_reference(rig, coarse_only=True)
        assert not np.array_equal(reference[..., 0:3], coarse_only[..., 0:3]), \
            "the viewport fell back to the whole-slide coarse"
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_twenty_camera_moves_never_wipe_the_fine_tier(app):
    """G3.2a gate 2: motion sharpens progressively; it never goes blank."""
    rig = _mount(app, visible=("CD3", "CD8"))
    steps = []
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        controller = rig.mount.host.stack.controller
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 2)
        rig.mount.host.jump_to(256, 256, 512, 512)
        assert _settled_on_level(app, rig, 0)
        _settle(app, rounds=20)
        opaque = int((_readback(rig)[..., 3] == 255).sum())
        assert opaque > 0

        for index in range(22):
            y = 256 + (index % 11) * 16
            x = 256 + (index % 7) * 16
            size = 512 if index % 3 else 448          # PAN and ZOOM both
            _zoom_to(rig, y, x, size)
            time.sleep(0.004)
            _settle(app, rounds=8)
            stats = binding.stats()
            descriptor = _last_submission(rig)[0]
            frame = _grab(rig)
            alpha = _readback(rig)[..., 3]
            steps.append({
                "step": index, "level": int(controller.level),
                "coarse_planes": sum(len(s.coarse) for s in descriptor.channels),
                "fine_planes": sum(len(s.fine) for s in descriptor.channels),
                "retained": stats["retained_fine_last_epoch"],
                "requested": stats["requested_fine_last_epoch"],
                "opaque": int((alpha == 255).sum()),
                "rgb_max": int(frame[..., 0:3].max()),
            })
            assert stats["fine_channels"], \
                f"move {index} wiped the fine tier back to coarse"
            assert int((alpha == 255).sum()) == opaque, \
                f"move {index} punched a transparent hole in the coarse background"
            assert int(frame[..., 1].max()) > 0 and int(frame[..., 0].max()) > 0, \
                f"move {index} lost a channel"
            assert sum(len(s.coarse) for s in descriptor.channels) == 18, \
                "the complete coarse background stopped being complete"

        assert all(step["fine_planes"] > 0 for step in steps)
        assert any(step["retained"] > 0 for step in steps), \
            "nothing was ever carried across an epoch"
        # Settling leaves the target level, not a mosaic.
        assert _settled_on_level(app, rig, int(controller.level))
        frame = _grab(rig)
        reference = _screen_reference(rig)
        assert int(np.abs(frame.astype(np.int16) -
                          reference.astype(np.int16))[..., 0:3].max()) <= 1
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_zooming_across_levels_carries_the_nearest_layer_it_already_has(app):
    """G3.2a gate 3: a level change does not drop to the whole-slide coarse."""
    rig = _big_mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        controller = rig.mount.host.stack.controller
        raw = rig.raws[0]
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",),
                     timeout=40)
        # Settle on a COARSER level first.
        _zoom_to(rig, 0, 0, BIG_SLIDE)
        start_level = int(controller.level)
        assert start_level >= 2, f"the rig did not start on a coarse level: {start_level}"
        assert _settled_on_level(app, rig, start_level, timeout=90)
        _settle(app, rounds=20)
        middle_levels = binding.stats()["fine_levels"]["CD3"]
        assert start_level in middle_levels and 0 not in middle_levels, middle_levels

        # Now cross TWO level boundaries with nothing new allowed to land.
        raw.hold()
        _zoom_to(rig, BIG_SLIDE // 8, BIG_SLIDE // 8, BIG_WIDGET[0])
        _settle(app, rounds=8)
        assert int(controller.level) == 0, "the zoom did not reach level 0"
        assert start_level - int(controller.level) >= 2, \
            "the zoom did not cross two pyramid levels"
        carried = binding.stats()
        assert carried["fine_channels"] == ("CD3",), \
            "crossing a level left only the whole-slide coarse"
        assert start_level in carried["fine_levels"]["CD3"], \
            "the nearest level already in hand was not carried"
        frame = _grab(rig)
        coarse_only = _screen_reference(rig, coarse_only=True)
        assert not np.array_equal(_screen_reference(rig)[..., 0:3],
                                  coarse_only[..., 0:3])
        assert int(np.abs(frame.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1

        # Let the target level arrive: it must win, and not stay a mosaic.
        raw.release_all()
        assert _settled_on_level(app, rig, 0, timeout=90)
        _settle(app, rounds=25)
        assert 0 in binding.stats()["fine_levels"]["CD3"]
        final = _grab(rig)
        oracle = _screen_reference(rig)
        assert int(np.abs(final.astype(np.int16) -
                          oracle.astype(np.int16))[..., 0:3].max()) <= 1
        assert not np.array_equal(final[..., 0:3], coarse_only[..., 0:3])
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_normal_viewport_over_sixteen_mib_still_reaches_its_target_level(app):
    """G3.2a gate 4: the old 16 MiB cap refused an ordinary viewport."""
    rig = _big_mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        controller = rig.mount.host.stack.controller
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",),
                     timeout=40)
        _zoom_to(rig, BIG_SLIDE // 4, BIG_SLIDE // 4, BIG_WIDGET[0])
        assert _wait(app, lambda: int(controller.level) == 0, timeout=40)
        visible = len(controller.snapshot().visible_tiles)
        working_set = visible * BIG_TILE * BIG_TILE * 4
        assert working_set > 16 * 1024 * 1024, (
            f"this viewport is only {working_set} bytes; it does not exercise "
            f"the old 16 MiB refusal")

        assert _settled_on_level(app, rig, 0, timeout=120)
        _settle(app, rounds=25)
        stats = binding.stats()
        assert stats["fine_budget_refused"] == (), \
            f"the demo budget refused a normal viewport: {stats['last_error']}"
        cache = rig.mount.gpu_status()["cache"]
        assert cache["peak_bytes"] <= DEMO_GPU_RAW_TEXTURE_BYTES
        frame = _grab(rig)
        oracle = _screen_reference(rig)
        assert int(np.abs(frame.astype(np.int16) -
                          oracle.astype(np.int16))[..., 0:3].max()) <= 1
        rig.measured = {"visible_tiles": visible, "working_set_bytes": working_set,
                        "fine_plane_bytes": stats["fine_plane_bytes"],
                        "peak_texture_bytes": cache["peak_bytes"]}
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_patch_and_tissue_landings_reuse_what_they_have_and_end_sharp(app):
    """G3.2a gate 5: both public landings, through the one controller."""
    rig = _mount(app, visible=("CD3",))
    rects = []
    try:
        _require_gpu(rig)
        mount = rig.mount
        binding = mount.gpu_binding
        controller = mount.host.stack.controller
        navigator = SimpleNamespace(overview=SimpleNamespace(
            set_current_view_rect=lambda rect: rects.append(tuple(rect)),
            clear_current_view_rect=lambda: rects.append(None)))
        mount._window._display.navigator = lambda: navigator
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))

        # A patch button.
        marker = len(rig.schedulers[0].public_requests)
        assert mount.show_patch((256, 768, 256, 768)) is True
        assert _fine_requests_since(rig, marker) or \
            binding.stats()["retained_fine_last_epoch"] > 0
        assert _settled_on_level(app, rig, int(controller.level), timeout=60)
        _settle(app, rounds=20)
        patch_frame = _grab(rig)
        assert int(np.abs(patch_frame.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
        assert not np.array_equal(
            _screen_reference(rig)[..., 0:3],
            _screen_reference(rig, coarse_only=True)[..., 0:3]), \
            "the patch landed on a coarse mosaic"
        patch_rects = len(rects)
        assert patch_rects > 0, "the Tissue Preview rectangle never moved"

        # A Tissue Preview landing that OVERLAPS it: what is there is reused.
        rig.raws[0].hold()
        marker = len(rig.schedulers[0].public_requests)
        assert mount.jump_to_point(560, 560, 400) is True
        _settle(app, rounds=6)
        landed = binding.stats()
        assert landed["retained_fine_last_epoch"] > 0, \
            "an overlapping landing threw its fine away"
        assert landed["requested_fine_last_epoch"] == len(
            _fine_requests_since(rig, marker))
        assert len(rects) > patch_rects, "the current-view rectangle stopped"

        rig.raws[0].release_all()
        assert _settled_on_level(app, rig, int(controller.level), timeout=60)
        _settle(app, rounds=20)
        final = _grab(rig)
        assert int(np.abs(final.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
        assert mount.host.stack.controller is controller
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_one_fine_tile_sharpens_its_own_area_without_waiting_for_the_rest(app):
    """G3.2a gate 2 (strict): a partial viewport is drawn, not withheld."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        raw = rig.raws[0]
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app, rounds=15)
        assert 0 not in binding.stats()["fine_levels"].get("CD3", ())

        # Ask for a whole level-0 viewport and let NOTHING land.
        raw.hold()
        marker = len(rig.schedulers[0].public_requests)
        rig.mount.host.jump_to(256, 256, 512, 512)
        _settle(app, rounds=8)
        wanted = [r.key for r in _fine_requests_since(rig, marker)
                  if int(r.key.tile.level) == 0]
        assert len(wanted) >= 3, f"this viewport only wants {len(wanted)} fine tiles"
        before = _grab(rig)
        before_alpha = _readback(rig)[..., 3].copy()

        # Release exactly ONE of them.
        first = sorted(wanted, key=lambda k: (k.tile.ty, k.tile.tx))[0]
        raw.allow("CD3", 0, first.tile.tx, first.tile.ty)
        assert _wait(app, lambda: first in binding._published_fine.get("CD3", {}))
        _settle(app, rounds=10)

        resident = {key for key in binding._published_fine["CD3"]
                    if int(key.tile.level) == 0}
        assert resident == {first}, \
            f"expected exactly the one released tile, got {len(resident)}"
        assert len(resident) < len(wanted), "the whole viewport was withheld"
        assert first in _fine_identities(rig), \
            "the tile that landed was not put on screen"

        after = _grab(rig)
        assert not np.array_equal(after, before), \
            "a fine tile landed and changed nothing on screen"
        assert int(np.abs(after.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
        assert np.array_equal(_readback(rig)[..., 3], before_alpha), \
            "a fine tile changed which pixels are opaque"
    finally:
        rig.raws[0].release_all()
        _close(rig)


# ══ G3.2a.1 review close-out ══════════════════════════════════════════

@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_unticking_a_channel_gives_its_fine_back_straight_away(app):
    """G3.2a.1: a release happens on the untick, not on the next camera move."""
    rig = _mount(app, visible=("CD3", "CD8"))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: len(binding.stats()["coarse_channels"]) == 2)
        rig.mount.host.jump_to(256, 256, 512, 512)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=20)
        loaded = binding.stats()
        assert loaded["fine_tiles"].get("CD8", 0) > 0
        before_bytes = loaded["fine_plane_bytes"]

        # THE UNTICK, through the shared display state the rows write to.
        # The camera does not move.
        marker = len(rig.schedulers[0].public_requests)
        _enable(rig, "CD8", visible=False)
        released = binding.stats()
        assert "CD8" not in released["fine_tiles"], \
            "an unticked channel kept its fine planes in memory"
        assert released["fine_plane_bytes"] < before_bytes
        assert "CD8" not in released["fine_channels"]
        assert int(_grab(rig)[..., 0].max()) == 0, "CD8 is still on screen"
        assert int(_grab(rig)[..., 1].max()) > 0, "CD3 was released too"

        # Ticking it back asks for THIS viewport's fine again.
        _enable(rig, "CD8", visible=True)
        assert _wait(app, lambda: "CD8" in binding.stats()["fine_channels"],
                     timeout=40)
        _settle(app, rounds=20)
        assert binding.stats()["fine_tiles"]["CD8"] > 0
        assert int(_grab(rig)[..., 0].max()) > 0

        # A display-only change still asks the scheduler for nothing.
        quiet = _counters(rig)
        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_mapping("CD3", 7.0, 1800.0, 1.1)
            rig.state.set_color("CD3", "#00ff88")
        rig.domain.edit_channel_weight("CD8", 0.8)
        _settle(app, rounds=20)
        after = _counters(rig)
        assert after["reads"] == quiet["reads"]
        assert after["requests"] == quiet["requests"]
        assert after["uploads"] == quiet["uploads"]
        assert after["submits"] > quiet["submits"]
        del marker
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_landing_with_no_overlap_asks_only_for_the_newly_exposed_tiles(app):
    """G3.2a.1: leaving the cached area entirely still ends on the target."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        raw = rig.raws[0]
        controller = rig.mount.host.stack.controller
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(288, 288, 320, 320)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=20)
        here = {key for key in binding._published_fine["CD3"]
                if int(key.tile.level) == 0}
        assert here

        # A Tissue Preview landing on the far corner of the region: the
        # target shares no tile at all with what is on screen.
        raw.hold()
        marker = len(rig.schedulers[0].public_requests)
        assert rig.mount.jump_to_point(1120, 1120, 320) is True
        _settle(app, rounds=8)
        stats = binding.stats()
        wanted = [r.key for r in _fine_requests_since(rig, marker)
                  if int(r.key.tile.level) == 0]
        assert wanted, "a landing outside the cached area asked for nothing"
        assert set(wanted).isdisjoint(here), "the landing was not far enough"
        assert stats["requested_fine_last_epoch"] == len(
            _fine_requests_since(rig, marker))
        resident = {key for key in binding._published_fine.get("CD3", {})}
        assert resident.isdisjoint(here), \
            "planes that no longer cover the viewport were kept"

        # The complete coarse still covers everything while they arrive.
        opaque = int((_readback(rig)[..., 3] == 255).sum())
        assert opaque > 0
        assert int(_grab(rig)[..., 1].max()) > 0, "the picture went blank"

        # They sharpen one at a time, then all arrive.
        first = sorted(wanted, key=lambda k: (k.tile.ty, k.tile.tx))[0]
        raw.allow("CD3", 0, first.tile.tx, first.tile.ty)
        assert _wait(app, lambda: first in binding._published_fine.get("CD3", {}))
        _settle(app, rounds=8)
        partial = {key for key in binding._published_fine["CD3"]
                   if int(key.tile.level) == 0}
        assert partial == {first}, f"expected one tile, got {len(partial)}"
        assert int((_readback(rig)[..., 3] == 255).sum()) == opaque

        raw.release_all()
        assert _settled_on_level(app, rig, int(controller.level), timeout=60)
        _settle(app, rounds=20)
        assert binding.stats()["fine_budget_refused"] == ()
        final = _grab(rig)
        assert int(np.abs(final.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
        assert not np.array_equal(
            _screen_reference(rig)[..., 0:3],
            _screen_reference(rig, coarse_only=True)[..., 0:3])
    finally:
        rig.raws[0].release_all()
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_unticking_the_only_channel_and_ticking_it_back_needs_no_camera_move(app):
    """G3.2a.2, on the product path: the picture comes back by itself."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(256, 256, 512, 512)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=20)
        target = {key for key in binding._published_fine["CD3"]
                  if int(key.tile.level) == 0}
        assert target
        camera = rig.mount.current_camera()
        sharp = _grab(rig)

        # Untick the ONLY channel through the shared display state.
        _enable(rig, "CD3", visible=False)
        emptied = binding.stats()
        assert emptied["fine_channels"] == () and emptied["fine_plane_bytes"] == 0
        assert emptied["coarse_channels"] == ("CD3",)
        assert int(_grab(rig)[..., 0:3].max()) == 0, "the picture is still up"

        # Tick it back. NOTHING touches the camera.
        _enable(rig, "CD3", visible=True)
        assert _settled_on_level(app, rig, 0, timeout=40), \
            "the only channel came back but never reached its target level"
        _settle(app, rounds=20)
        assert rig.mount.current_camera() == pytest.approx(camera)
        # The whole target level is back. The stand-ins carried from an
        # earlier zoom are NOT resurrected -- they were released with the
        # channel, and only the target level is what this viewport needs.
        assert {key for key in binding._published_fine["CD3"]
                if int(key.tile.level) == 0} == target
        assert binding.stats()["fine_budget_refused"] == ()
        back = _grab(rig)
        assert int(np.abs(back.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
        assert not np.array_equal(
            _screen_reference(rig)[..., 0:3],
            _screen_reference(rig, coarse_only=True)[..., 0:3]), \
            "it came back on the whole-slide coarse"
        assert np.array_equal(back, sharp), \
            "the restored picture is not the one that was there before"
    finally:
        _close(rig)


# ══ G3.2a.3 continuous-motion gates ═══════════════════════════════════

class _SubmitProbe:
    """Timestamps every submission the binding makes, with its viewport."""

    def __init__(self, layer):
        self._layer = layer
        self.entries = []

    def submit(self, descriptor, display, viewport):
        stats = self._layer.submit(descriptor, display, viewport)
        self.entries.append((time.perf_counter(), tuple(viewport.world_rect)))
        return stats

    def __getattr__(self, name):
        return getattr(self._layer, name)

    @property
    def centres(self):
        return [round((rect[0] + rect[1]) / 2.0, 2) for _t, rect in self.entries]


def _real_event_loop(seconds, on_tick, interval_ms=10):
    """A REAL Qt event loop, so a drag is measured as one gesture.

    Stepping the camera with `processEvents()` between every move would let
    every timer and every queued result run to completion at each step,
    which turns a continuous drag into a series of still pictures -- and
    the bug this gates against only exists while the gesture is running.
    """
    loop = QtCore.QEventLoop()
    stepper = QtCore.QTimer()
    stepper.setInterval(interval_ms)
    stepper.timeout.connect(on_tick)
    stopper = QtCore.QTimer()
    stopper.setSingleShot(True)
    stopper.timeout.connect(loop.quit)
    stepper.start()
    stopper.start(int(seconds * 1000))
    loop.exec_()
    stepper.stop()


def _hold_and_drag(rig, seconds=1.2, interval_ms=10, span=384, step_px=6,
                   start=(320, 320), fixed_y=True):
    view_box = rig.mount.host.stack.view.view_box
    state = {"n": 0}

    def tick():
        state["n"] += 1
        offset = state["n"] * step_px
        y0 = start[0] if fixed_y else start[0] + offset
        view_box.setRange(xRange=(start[1] + offset, start[1] + offset + span),
                          yRange=(y0, y0 + span), padding=0)

    _real_event_loop(seconds, tick, interval_ms)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_continuous_drag_keeps_the_picture_moving_before_the_button_is_up(app):
    """G3.2a.3: the GPU follows the camera DURING the gesture.

    Restarting the motion timer on every event pushed its timeout out for
    as long as the mouse kept moving, so the picture stood still until the
    gesture ended and then jumped. This asserts the trail of submissions,
    not the final frame.
    """
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(320, 320, 384, 384)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=20)

        probe = _SubmitProbe(binding.layer)
        binding.layer = probe
        _hold_and_drag(rig)

        assert len(probe.entries) >= 20, \
            f"only {len(probe.entries)} submissions during the whole gesture"
        assert len(set(probe.centres)) >= 10, (
            f"the GPU used {len(set(probe.centres))} distinct camera positions "
            f"during the drag: it is not following the camera")
        gaps = [(b[0] - a[0]) * 1000.0
                for a, b in zip(probe.entries, probe.entries[1:])]
        interval = rig.mount.gpu_binding.budgets.motion_interval_ms
        assert max(gaps) < interval * 4, \
            f"the picture stalled for {max(gaps):.0f} ms inside the gesture"

        # At the instant the gesture stops, the GPU is at most one throttle
        # window behind the camera -- not a whole gesture behind it.
        (x0, _x1), (_y0, _y1) = rig.mount.host.stack.view.view_box.viewRange()
        assert abs(probe.entries[-1][1][0] - x0) <= 6 * 4, (
            f"the GPU was at {probe.entries[-1][1][0]} when the camera was "
            f"at {x0}")
        assert probe.centres[0] != probe.centres[-1]

        # And the gesture ending lands it exactly, through the existing
        # gesture_quiet the controller already emits.
        _real_event_loop(0.4, lambda: None, 50)
        assert probe.entries[-1][1][0] == pytest.approx(x0, abs=1.0)
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_drag_over_ground_already_loaded_reads_and_uploads_nothing(app):
    """G3.2a.3: following the camera must not turn into new I/O."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(320, 320, 384, 384)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=25)

        probe = _SubmitProbe(binding.layer)
        binding.layer = probe
        before = _counters(rig)
        view_box = rig.mount.host.stack.view.view_box
        state = {"n": 0}

        def tick():
            state["n"] += 1
            offset = (state["n"] % 10) * 3        # back and forth, never out
            view_box.setRange(xRange=(320 + offset, 320 + offset + 384),
                              yRange=(320 + offset, 320 + offset + 384),
                              padding=0)

        _real_event_loop(1.0, tick, 10)
        after = _counters(rig)

        assert len(probe.entries) >= 15, "the drag produced almost no frames"
        assert after["reads"] == before["reads"], \
            "following the camera read a tile from the provider"
        assert after["uploads"] == before["uploads"], \
            "following the camera re-uploaded a raw texture"
        assert after["cpu_compose"] == 0
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_jump_is_still_served_immediately_during_motion(app):
    """G3.2a.3: the throttle must not delay a NAVIGATOR_JUMP."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(320, 320, 384, 384)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=20)
        probe = _SubmitProbe(binding.layer)
        binding.layer = probe

        # Start a gesture so the motion timer is definitely running, then
        # land a jump in the middle of it.
        view_box = rig.mount.host.stack.view.view_box
        view_box.setRange(xRange=(340, 724), yRange=(340, 724), padding=0)
        assert binding._motion_timer.isActive()
        before = len(probe.entries)
        assert rig.mount.jump_to_point(900, 900, 320) is True
        assert len(probe.entries) > before, \
            "a jump waited for the motion throttle"
        (x0, _x1), (_y0, _y1) = view_box.viewRange()
        assert probe.entries[-1][1][0] == pytest.approx(x0, abs=1.0)
    finally:
        _close(rig)


# ══ G3.2a.4 landing gates ═════════════════════════════════════════════

class _PlanProbe:
    """Records every viewport planning the binding actually performs."""

    def __init__(self, binding):
        self._binding = binding
        self._original = binding.update_viewport
        self.calls = []
        binding.update_viewport = self._wrapped

    def _wrapped(self, snapshot=None):
        snap = (self._binding.controller.snapshot() if snapshot is None
                else snapshot)
        self._original(snapshot)
        self.calls.append(int(getattr(snap, "epoch", -1)))

    def release(self):
        self._binding.update_viewport = self._original


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_landing_during_motion_is_planned_exactly_once(app):
    """G3.2a.4: the leftover motion timer must not re-plan the same landing.

    A jump served while a gesture's throttle timer is still running used to
    be planned three times: once by the jump, once when that timer expired,
    and once more when the controller announced the gesture was over. Each
    repeat cancelled the generation just issued and re-attached every
    request for the same place.
    """
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        controller = rig.mount.host.stack.controller
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        rig.mount.host.jump_to(320, 320, 384, 384)
        assert _settled_on_level(app, rig, 0, timeout=40)
        _settle(app, rounds=20)

        probe = _PlanProbe(binding)
        state = {"n": 0, "jumped": False, "epoch": None,
                 "timer_after_jump": None}

        def tick():
            state["n"] += 1
            if state["n"] <= 3:
                offset = state["n"] * 6
                rig.mount.host.stack.view.view_box.setRange(
                    xRange=(320 + offset, 704 + offset), yRange=(320, 704),
                    padding=0)
                return
            if not state["jumped"]:
                state["jumped"] = True
                assert binding._motion_timer.isActive(), \
                    "this gate needs the motion timer to be running"
                rig.mount.jump_to_point(1000, 1000, 320)
                state["epoch"] = int(controller.snapshot().epoch)
                state["timer_after_jump"] = binding._motion_timer.isActive()

        # Three camera steps, then 450 ms of pure waiting -- past the 33 ms
        # throttle AND past the controller's own 80 ms quiet.
        _real_event_loop(0.45, tick, 10)
        probe.release()

        assert state["jumped"]
        assert state["timer_after_jump"] is False, \
            "the landing left the old motion timer running"
        for_the_jump = [e for e in probe.calls if e == state["epoch"]]
        assert len(for_the_jump) == 1, (
            f"the landing was planned {len(for_the_jump)} times "
            f"(epoch trail {probe.calls})")
        # And it really is where the camera ended up.
        (x0, _x1), (_y0, _y1) = rig.mount.host.stack.view.view_box.viewRange()
        assert _last_submission(rig)[2].world_rect[0] == pytest.approx(x0, abs=1.0)
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_two_fast_far_landings_end_on_the_second_one_by_themselves(app):
    """G3.2a.4: the second landing lands, and nothing else is needed.

    No third click, no drag, no zoom, and no test-only call into the
    binding: the picture has to arrive on its own.
    """
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        controller = rig.mount.host.stack.controller
        raw = rig.raws[0]
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app, rounds=20)

        # Hold the reads so the FIRST landing's tiles are still in flight
        # when the second one arrives: that is what makes a late result
        # possible at all.
        raw.hold()
        probe = _PlanProbe(binding)
        assert rig.mount.jump_to_point(1120, 1120, 320) is True
        first_epoch = int(controller.snapshot().epoch)
        first_keys = {r.key for r in _fine_requests_since(rig, 0)}
        _real_event_loop(0.015, lambda: None, 5)      # well under 33 ms
        assert rig.mount.jump_to_point(320, 1120, 320) is True
        second_epoch = int(controller.snapshot().epoch)
        assert second_epoch != first_epoch
        (sx0, sx1), (sy0, sy1) = controller.view.view_box.viewRange()
        raw.release_all()

        # Let it finish ON ITS OWN. No further input of any kind.
        deadline = time.monotonic() + 20.0
        level = int(controller.level)
        wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}

        def arrived():
            have = {(key.tile.tx, key.tile.ty)
                    for key in binding._published_fine.get("CD3", {})
                    if int(key.tile.level) == level}
            return bool(wanted) and wanted <= have

        while not arrived() and time.monotonic() < deadline:
            _real_event_loop(0.05, lambda: None, 25)
        probe.release()

        assert arrived(), "the second landing never reached its target level"
        assert probe.calls.count(second_epoch) == 1, \
            f"the second landing was planned {probe.calls.count(second_epoch)} times"
        descriptor, _display, viewport, _stats = _last_submission(rig)
        assert viewport.world_rect[0] == pytest.approx(sx0, abs=1.0)
        assert viewport.world_rect[2] == pytest.approx(sy0, abs=1.0)
        # Nothing from the first landing is on screen.
        resident = {key for key in binding._published_fine.get("CD3", {})}
        assert resident.isdisjoint(first_keys), \
            "a tile of the abandoned landing reached the picture"
        assert binding.stats()["rejected_late_results"] > 0, \
            "the first landing's results were never refused"
        frame = _grab(rig)          # FORCED render, once, for the final check
        assert int(np.abs(frame.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
        assert not np.array_equal(
            _screen_reference(rig)[..., 0:3],
            _screen_reference(rig, coarse_only=True)[..., 0:3]), \
            "the second landing is still on the whole-slide coarse"
        del sx1, sy1
    finally:
        rig.raws[0].release_all()
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_cold_landing_sharpens_without_any_further_gesture(app):
    """G3.2a.4: a never-visited area finishes by itself, tile by tile."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        controller = rig.mount.host.stack.controller
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app, rounds=20)
        assert 0 not in binding.stats()["fine_levels"].get("CD3", ())

        assert rig.mount.jump_to_point(1120, 400, 320) is True
        level = int(controller.level)
        wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
        assert wanted

        seen = []
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            have = {(key.tile.tx, key.tile.ty)
                    for key in binding._published_fine.get("CD3", {})
                    if int(key.tile.level) == level}
            seen.append(len(have))
            if wanted <= have:
                break
            _real_event_loop(0.05, lambda: None, 25)

        assert wanted <= {(key.tile.tx, key.tile.ty)
                          for key in binding._published_fine.get("CD3", {})
                          if int(key.tile.level) == level}, \
            "a cold landing stayed on coarse until something woke it"
        assert binding.stats()["fine_budget_refused"] == ()
        assert max(seen) == len(wanted)
        frame = _grab(rig)
        assert int(np.abs(frame.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_patch_round_trip_reads_nothing_on_the_way_back(app):
    """G3.2a.4: A -> B -> A. A request that hits the cache is not a read."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        legs = {}
        for name, bbox in (("A", (320, 704, 320, 704)),
                           ("B", (896, 1216, 896, 1216)),
                           ("A again", (320, 704, 320, 704))):
            before = _counters(rig)
            rig.mount.show_patch(bbox)
            assert _settled_on_level(
                app, rig, int(rig.mount.host.stack.controller.level), timeout=40)
            _settle(app, rounds=20)
            after = _counters(rig)
            legs[name] = {
                "requests": after["requests"] - before["requests"],
                "reads": after["reads"] - before["reads"],
                "uploads": after["uploads"] - before["uploads"],
                "submits": after["submits"] - before["submits"],
            }
        assert legs["A"]["reads"] > 0, "leg A was supposed to be cold"
        assert legs["A again"]["reads"] == 0, \
            f"returning to A read {legs['A again']['reads']} tiles again"
        assert legs["A again"]["uploads"] == 0, \
            "returning to A re-uploaded a raw texture"
        # Requests and GPU redraws DO happen on the way back, and neither is
        # a disk read or a correction being recomputed.
        assert legs["A again"]["requests"] > 0
        assert legs["A again"]["submits"] > 0
    finally:
        _close(rig)


# ══ G3.2a.5 hot-cache patch return ════════════════════════════════════

class _PaintWatcher(QtCore.QObject):
    """What the picture consisted of at every NATURAL Qt paint."""

    def __init__(self, rig, channel="CD3"):
        super().__init__()
        self._rig = rig
        self._channel = channel
        self.frames = []
        rig.mount.gpu_layer.installEventFilter(self)

    def eventFilter(self, _watched, event):
        if event.type() == QtCore.QEvent.Paint:
            binding = self._rig.mount.gpu_binding
            controller = self._rig.mount.host.stack.controller
            level = int(controller.level)
            resident = {(key.tile.tx, key.tile.ty)
                        for key in binding._published_fine.get(self._channel, {})
                        if int(key.tile.level) == level}
            wanted = {(tx, ty) for tx, ty in controller.snapshot().visible_tiles}
            self.frames.append({"on_screen": len(resident),
                                "wanted": len(wanted),
                                "complete": bool(wanted and wanted <= resident)})
        return False


class _ThreadProbe:
    """Which thread actually mutated the binding, per applied result."""

    def __init__(self, binding):
        self._binding = binding
        self._original = binding._apply_result
        self.threads = []
        binding._apply_result = self._wrapped

    def _wrapped(self, payload, *, publish):
        self.threads.append(QtCore.QThread.currentThread())
        return self._original(payload, publish=publish)

    def release(self):
        self._binding._apply_result = self._original


def _target_tiles(rig, channel="CD3"):
    binding = rig.mount.gpu_binding
    level = int(rig.mount.host.stack.controller.level)
    return {(key.tile.tx, key.tile.ty)
            for key in binding._published_fine.get(channel, {})
            if int(key.tile.level) == level}


def _settle_patch(app, rig, bbox):
    rig.mount.show_patch(bbox)
    assert _settled_on_level(app, rig, int(rig.mount.host.stack.controller.level),
                             timeout=40)
    _settle(app, rounds=20)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_returning_to_a_cached_patch_is_whole_on_the_very_first_paint(app):
    """G3.2a.5: no coarse replay when the data is already in hand.

    Returning to a patch whose tiles are still in the scheduler's raw cache
    and in the GPU's texture cache used to arrive as one queued signal per
    tile, so the viewer showed coarse and then filled in tile by tile for
    data it never had to fetch.
    """
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle_patch(app, rig, (320, 704, 320, 704))
        before_leaving = _grab(rig)           # FORCED render, pixel oracle
        _settle_patch(app, rig, (896, 1216, 896, 1216))

        watcher = _PaintWatcher(rig)
        probe = _SubmitProbe(binding.layer)
        binding.layer = probe
        before = _counters(rig)

        rig.mount.show_patch((320, 704, 320, 704))
        # THE CALL HAS RETURNED. Nothing else has run yet.
        wanted = {(tx, ty) for tx, ty
                  in rig.mount.host.stack.controller.snapshot().visible_tiles}
        assert wanted, "this patch has no visible tiles"
        assert wanted <= _target_tiles(rig), (
            "the cached patch was not whole when show_patch() returned: "
            f"{len(_target_tiles(rig))} of {len(wanted)}")
        assert len(probe.entries) == 1, (
            f"the cached batch published {len(probe.entries)} times; it must "
            f"reach the screen as one picture")

        # Now let Qt paint by itself. No grabFramebuffer, no processEvents.
        _real_event_loop(0.25, lambda: None, 25)
        after = _counters(rig)

        assert after["reads"] == before["reads"], "a cached return read a tile"
        assert after["uploads"] == before["uploads"], \
            "a cached return re-uploaded a raw texture"
        assert after["requests"] > before["requests"], \
            "this gate needs the requests that hit the cache"
        assert watcher.frames, "Qt never painted"
        assert watcher.frames[0]["complete"], (
            f"the first natural paint showed {watcher.frames[0]['on_screen']} of "
            f"{watcher.frames[0]['wanted']} tiles")
        assert all(frame["complete"] for frame in watcher.frames), \
            "a partial picture was painted on the way back"

        back = _grab(rig)                     # FORCED render, pixel oracle
        assert np.array_equal(back, before_leaving), \
            "the returned picture is not the one that was there before"
    finally:
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_cold_patch_still_arrives_tile_by_tile_from_its_worker(app):
    """G3.2a.5: batching the cache must not make cold ground wait."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        raw = rig.raws[0]
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle(app, rounds=20)

        probe = _ThreadProbe(binding)
        raw.hold()
        rig.mount.show_patch((896, 1216, 896, 1216))
        wanted = {(tx, ty) for tx, ty
                  in rig.mount.host.stack.controller.snapshot().visible_tiles}
        assert wanted
        assert not _target_tiles(rig), \
            "a cold patch was somehow whole before anything was read"

        keys = sorted({r.key for r in _fine_requests_since(rig, 0)
                       if int(r.key.tile.level) ==
                       int(rig.mount.host.stack.controller.level)},
                      key=lambda k: (k.tile.ty, k.tile.tx))
        assert len(keys) >= 2, "this gate needs more than one cold tile"

        seen = []
        for key in keys:
            raw.allow("CD3", int(key.tile.level), key.tile.tx, key.tile.ty)
            assert _wait(app, lambda k=key: k in binding._published_fine.get("CD3", {}))
            _settle(app, rounds=5)
            seen.append(len(_target_tiles(rig)))
        probe.release()

        assert seen == sorted(seen) and seen[0] < seen[-1], \
            f"a cold patch did not improve tile by tile: {seen}"
        assert wanted <= _target_tiles(rig)
        gui = QtCore.QThread.currentThread()
        assert probe.threads, "no result was applied at all"
        assert all(thread is gui for thread in probe.threads), \
            "a worker thread mutated the binding directly"
    finally:
        rig.raws[0].release_all()
        _close(rig)


@pytest.mark.skipif(not REQUIRE_GPU,
                    reason="hardware gate requires BLOCK01_REQUIRE_STEP1_GPU=1")
def test_a_half_cached_viewport_shows_its_cached_half_at_once(app):
    """G3.2a.5: a mixed viewport restores what it has and fills in the rest."""
    rig = _mount(app, visible=("CD3",))
    try:
        _require_gpu(rig)
        binding = rig.mount.gpu_binding
        raw = rig.raws[0]
        assert _wait(app, lambda: binding.stats()["coarse_channels"] == ("CD3",))
        _settle_patch(app, rig, (320, 704, 320, 704))
        cached = set(_target_tiles(rig))
        assert cached
        _settle_patch(app, rig, (896, 1216, 896, 1216))

        # A viewport that overlaps the cached patch and reaches into ground
        # that has never been read.
        raw.hold()
        probe = _SubmitProbe(binding.layer)
        binding.layer = probe
        rig.mount.show_patch((320, 1216, 320, 1216))
        wanted = {(tx, ty) for tx, ty
                  in rig.mount.host.stack.controller.snapshot().visible_tiles}
        on_return = set(_target_tiles(rig))
        assert on_return, "the cached half did not come back at once"
        assert on_return < wanted, "this gate needs a genuinely cold remainder"
        assert on_return <= cached | on_return
        assert len(probe.entries) == 1, \
            "the cached half must be one picture, not one publish per tile"

        raw.release_all()
        assert _wait(app, lambda: wanted <= _target_tiles(rig), timeout=40)
        _settle(app, rounds=20)
        assert binding.stats()["fine_budget_refused"] == ()
        frame = _grab(rig)                     # FORCED render, pixel oracle
        assert int(np.abs(frame.astype(np.int16) -
                          _screen_reference(rig).astype(np.int16)
                          )[..., 0:3].max()) <= 1
    finally:
        rig.raws[0].release_all()
        _close(rig)
