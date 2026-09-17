"""What Step1's whole-slide viewer is given, and by whom.

Block B3 of `docs/step1_rework_plan.md`, and the review's own list: rebind on
a moved source while keeping the viewport, read the channel in Step1's scope,
let the shared Intensity answer win, ask for a seed rather than inventing one,
navigate through the one controller, open with no patch at all, and tear a
dataset down before binding another.

The stack is the real one -- real `ExploreView`, controller, scheduler and
caches -- with a synthetic raw pyramid so the numbers are known.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.ui.step1_viewer_binding import Step1ViewerBinding  # noqa: E402
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost,
)
from block01.viewer.tile_types import (  # noqa: E402
    SourceIdentity, TileAddress, TileGridSpec,
)

SLIDE = 2048
ROI = (512, 1536, 512, 1536)
GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _RawPyramid:
    CHANNELS = ("DAPI", "CD3", "CD8")

    def __init__(self):
        self._shapes = {0: (SLIDE, SLIDE), 1: (SLIDE // 4, SLIDE // 4)}
        self.reads = []

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

    def read_region(self, channel, level, y0, y1, x0, x1):
        self.reads.append((channel, level, y0, y1, x0, x1))
        offset = self.CHANNELS.index(channel) * 1000.0
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        return yy + xx / 1000.0 + offset, (y0, x0)

    def read_tile(self, channel, tile):
        size = tile.grid.tile_size
        y0, x0 = tile.ty * size, tile.tx * size
        values, _off = self.read_region(channel, tile.level, y0, y0 + size,
                                        x0, x0 + size)
        return values, 0.0

    def close(self):
        pass


class _Seeder:
    """The display services' seed port, with its own record of requests."""

    def __init__(self, state, answers=None):
        self.state = state
        self.requested = []
        self._answers = dict(answers or {})

    def request_mapping_seed(self, channel, nucleus=False):
        self.requested.append(str(channel))
        return True

    def deliver(self, channel, mapping):
        """What the seed worker does when it lands: write the shared state."""
        self.state.set_mapping(str(channel), *mapping, origin="seed")


class _Window:
    """Everything the binding reads from a window, and nothing else."""

    def __init__(self, state, seeder, decisions=None, roi=ROI,
                 revision="rev-1", products=None):
        self._display = SimpleNamespace(state=state,
                                        request_mapping_seed=seeder.request_mapping_seed)
        self.loader = SimpleNamespace(filepath="/x/step1.ome.tif")
        self._corrected_decisions = dict(decisions or {})
        self._corrected_zarr_path = "/tmp/corrected.zarr"
        self._active_roi = ({"name": "ROI_1", "bbox_fullres": list(roi)}
                            if roi else None)
        self.step0_output = {"channel_remap_config_hash": revision}
        self.products = dict(products or {})


class _Corrected:
    def __init__(self, bbox=ROI, stamp="2026-09-17T10:00:00"):
        y0, y1, x0, x1 = bbox
        self._pixels = np.full((y1 - y0, x1 - x0), -50.0, np.float32)
        self.attrs = {"roi_bbox_fullres": list(bbox), "written_at": stamp}
        self.shape = self._pixels.shape
        self.dtype = self._pixels.dtype

    def __getitem__(self, key):
        return self._pixels[key]


def _binding(app, window, channel="CD3"):
    raws = []

    def _factory(path, chan, table, parent):
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView
        from block01.viewer.scheduler import TileScheduler
        from block01.ui.step0.step0_explore_tab import ExploreStack

        raw = _RawPyramid()
        raws.append(raw)
        provider = Step1TileProvider(raw, table)
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
    binding = Step1ViewerBinding(window, host=host)
    binding.open(channel)
    QtWidgets.QApplication.processEvents()
    return binding, raws


def _state_with_channels(app):
    state = ChannelDisplayState()
    state.bind(DatasetIdentity(path="/x/step1.ome.tif", fingerprint="1:1"))
    return state


def _close(binding):
    binding.close()
    QtWidgets.QApplication.processEvents()


# ── 1. the channel comes from Step1's own scope ───────────────────────

def test_the_current_channel_is_read_in_step1_s_scope(app):
    state = _state_with_channels(app)
    with state.using_scope("step0"):
        state.set_selected_channel("DAPI", origin="step0")
    with state.using_scope("step1"):
        state.set_selected_channel("CD8", origin="step1")
    window = _Window(state, _Seeder(state))

    binding, _raws = _binding(app, window, channel="")
    try:
        assert binding.current_channel() == "CD8"
        assert binding.host.channel == "CD8", \
            "the viewer followed another step's selection"
    finally:
        _close(binding)


def test_following_the_channel_keeps_the_camera(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, _raws = _binding(app, window, channel="CD3")
    try:
        binding.show_patch((600, 800, 600, 800))
        QtWidgets.QApplication.processEvents()
        before = binding.host.stack.view.view_box.viewRect()

        with state.using_scope("step1"):
            state.set_selected_channel("CD8", origin="step1")
        assert binding.follow_channel() is True

        after = binding.host.stack.view.view_box.viewRect()
        assert binding.host.channel == "CD8"
        assert (round(before.x()), round(before.y())) == (round(after.x()),
                                                          round(after.y()))
    finally:
        _close(binding)


# ── 2. Intensity: the shared state's answer ───────────────────────────

def test_a_manual_window_is_applied_and_never_seeded_over(app):
    state = _state_with_channels(app)
    seeder = _Seeder(state)
    state.set_mapping("CD3", 10.0, 200.0, 1.3, origin="user")
    window = _Window(state, seeder)

    binding, _raws = _binding(app, window, channel="CD3")
    try:
        assert seeder.requested == [], "a stored window was seeded over"
        # ...and the stored numbers are what the viewer was given.
        applied = []
        binding.host.apply_display_mapping = lambda *a, **k: applied.append(a)
        binding.apply_intensity("CD3")
        assert applied and applied[-1][:3] == (10.0, 200.0, 1.3)
    finally:
        _close(binding)


def test_a_missing_window_is_ASKED_for_and_applied_when_it_lands(app):
    state = _state_with_channels(app)
    seeder = _Seeder(state)
    window = _Window(state, seeder)

    binding, _raws = _binding(app, window, channel="CD3")
    try:
        assert seeder.requested == ["CD3"], (
            "the host invented a window instead of asking for one")
        applied = []
        binding.host.apply_display_mapping = lambda *a, **k: applied.append(a)

        seeder.deliver("CD3", (5.0, 50.0, 1.0))
        QtWidgets.QApplication.processEvents()

        assert applied and applied[-1][:3] == (5.0, 50.0, 1.0), (
            "the window that landed was never applied")
    finally:
        _close(binding)


def test_the_binding_keeps_no_already_applied_set(app):
    """A user's later edit must reach the viewer, however many came before."""
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, _raws = _binding(app, window, channel="CD3")
    try:
        applied = []
        binding.host.apply_display_mapping = lambda *a, **k: applied.append(a)

        for value in (10.0, 20.0, 30.0):
            state.set_mapping("CD3", value, 100.0, 1.0, origin="user")
            QtWidgets.QApplication.processEvents()

        assert [a[0] for a in applied] == [10.0, 20.0, 30.0], applied
    finally:
        _close(binding)


def test_another_channel_s_window_is_not_applied(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, _raws = _binding(app, window, channel="CD3")
    try:
        applied = []
        binding.host.apply_display_mapping = lambda *a, **k: applied.append(a)

        state.set_mapping("CD8", 1.0, 2.0, 1.0, origin="user")
        QtWidgets.QApplication.processEvents()

        assert applied == [], "another channel's window reached the viewer"
    finally:
        _close(binding)


# ── 3. a moved source rebinds, and keeps the viewport ─────────────────

def test_a_republished_handoff_rebinds_and_keeps_the_viewport(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, raws = _binding(app, window, channel="CD3")
    try:
        binding.show_patch((700, 900, 700, 900))
        QtWidgets.QApplication.processEvents()
        before = binding.host.stack.view.view_box.viewRect()
        first = binding.host.stack

        window.step0_output["channel_remap_config_hash"] = "rev-2"
        binding.open("CD3")
        QtWidgets.QApplication.processEvents()

        assert binding.host.stack is not first, "the source was not rebound"
        after = binding.host.stack.view.view_box.viewRect()
        assert (round(before.x()), round(before.y()),
                round(before.width())) == (round(after.x()), round(after.y()),
                                           round(after.width())), \
            "the rebind threw the user back to the whole slide"
    finally:
        _close(binding)


def test_a_rebind_does_not_clear_the_window_the_user_set(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    state.set_mapping("CD3", 3.0, 30.0, 1.1, origin="user")
    binding, _raws = _binding(app, window, channel="CD3")
    try:
        window.step0_output["channel_remap_config_hash"] = "rev-2"
        binding.open("CD3")
        QtWidgets.QApplication.processEvents()

        assert state.mapping("CD3") == (3.0, 30.0, 1.1), (
            "a rebind cleared the user's Intensity")
    finally:
        _close(binding)


def test_an_unchanged_handoff_does_not_rebuild_the_stack(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, _raws = _binding(app, window, channel="CD3")
    try:
        first = binding.host.stack
        binding.open("CD3")
        QtWidgets.QApplication.processEvents()
        assert binding.host.stack is first, "an unchanged handoff rebuilt"
    finally:
        _close(binding)


# ── 4. navigation and the no-patch case ───────────────────────────────

def test_a_patch_and_a_preview_click_use_the_one_controller(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, _raws = _binding(app, window, channel="CD3")
    try:
        jumps = []
        binding.host.jump_to = lambda *a: jumps.append(a) or True

        binding.show_patch((600, 700, 800, 900))
        binding.jump_to_point(1000, 1200, 256)

        assert jumps == [(600, 800, 100, 100), (872, 1072, 256, 256)]
    finally:
        _close(binding)


def test_the_viewer_opens_with_no_patch_at_all(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    assert not hasattr(window, "_all_patches")

    binding, _raws = _binding(app, window, channel="CD3")
    try:
        assert binding.host.stack is not None
        rect = binding.host.stack.view.view_box.viewRect()
        assert rect.width() >= SLIDE / 2, "the whole slide was not shown"
    finally:
        _close(binding)


def test_a_project_with_no_roi_opens_on_the_whole_slide(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state), roi=None)

    binding, _raws = _binding(app, window, channel="CD3")
    try:
        assert binding.host.stack is not None
        assert binding.roi_bbox() is None
    finally:
        _close(binding)


# ── 5. lifecycle ──────────────────────────────────────────────────────

def test_closing_disconnects_and_tears_the_stack_down(app):
    state = _state_with_channels(app)
    window = _Window(state, _Seeder(state))
    binding, _raws = _binding(app, window, channel="CD3")

    binding.close()
    QtWidgets.QApplication.processEvents()

    assert binding.host.stack is None
    applied = []
    binding.host.apply_display_mapping = lambda *a, **k: applied.append(a)
    state.set_mapping("CD3", 9.0, 90.0, 1.0, origin="user")
    QtWidgets.QApplication.processEvents()
    assert applied == [], "a closed binding still followed the state"
