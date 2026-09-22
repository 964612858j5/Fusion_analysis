"""Step1's whole-slide viewer inside the tab the user already has.

Block C4 of `docs/step1_rework_plan.md`. The stack is the REAL one -- real
`ExploreView`, controller, scheduler and caches over a synthetic pyramid -- so
the gates read the pixels that were actually painted, through
`pg.ImageItem.qimage`, and not the arguments of a signal.

The exit gates the plan names: Overlay and Fusion match the C1 CPU reference
pixel for pixel; a mode switch leaves no tile of the old picture; a weight, a
colour or an Intensity change reads nothing from disk; outside the analysis
region is transparent and coming back inside restores the pixels; a patch
button and a Tissue Preview click both put real tiles in the target viewport;
a missing corrected product is said in the badge while the other channels
still draw; Step1 composes nothing while it is not on screen and composes once
when it comes back; and the committed snapshot is untouched throughout.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402
from PyQt5.QtGui import QColor  # noqa: E402

from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.core.fusion_domain import FusionDomainModel  # noqa: E402
from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_draft_spec import STEP1_SCOPE  # noqa: E402
from block01.ui.step1_viewer_host import (  # noqa: E402
    Step1TileProvider, Step1ViewerHost,
)
from block01.ui.step1_viewer_mount import (  # noqa: E402
    LEGACY_FUSION, LEGACY_OVERLAY, Step1WholeSlideMount,
)
from block01.viewer import step1_compose as compose_core  # noqa: E402
from block01.viewer.tile_types import TileGridSpec  # noqa: E402

SLIDE = 2048
ROI = (512, 1536, 512, 1536)
GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
CHANNELS = ("DAPI", "CD3", "CD8")
WINDOW = (0.0, 4096.0, 1.0)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the slide ─────────────────────────────────────────────────────────

class _RawPyramid:
    def __init__(self):
        self._shapes = {0: (SLIDE, SLIDE), 1: (SLIDE // 4, SLIDE // 4)}
        self.reads = []

    def source_identity(self):
        from block01.viewer.tile_types import SourceIdentity
        return SourceIdentity(dataset_path="/x/c4.ome.tif",
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
        return 1.0 if level == 0 else 4.0

    def level_downsample_yx(self, level):
        ds = self.level_downsample(level)
        return ds, ds

    def read_region(self, channel, level, y0, y1, x0, x1):
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


class _Window:
    """Everything the mount reads from a window, and nothing else."""

    def __init__(self, state, domain, decisions=None, seeder=None,
                 corrected_path="/tmp/c4-corrected.zarr"):
        self._display = SimpleNamespace(
            state=state, fusion=domain,
            request_mapping_seed=(seeder or (lambda ch, nucleus=False: True)))
        self.loader = SimpleNamespace(filepath="/x/c4.ome.tif")
        self._corrected_decisions = dict(decisions or {})
        self._corrected_zarr_path = corrected_path
        self._active_roi = {"name": "ROI_1", "bbox_fullres": list(ROI)}
        self.step0_output = {"channel_remap_config_hash": "rev-1"}


def _stack_factory(raws):
    def _factory(path, chan, table, parent):
        from block01.ui.step0.step0_explore_tab import ExploreStack
        from block01.viewer.caches import LRUByteCache
        from block01.viewer.correction_compute import CorrectionCompute
        from block01.viewer.explore_view import ExploreController, ExploreView
        from block01.viewer.scheduler import TileScheduler

        raw = _RawPyramid()
        raws.append(raw)
        provider = Step1TileProvider(raw, table)
        raw_cache = LRUByteCache(32 * 1024 * 1024)
        corrected_cache = LRUByteCache(32 * 1024 * 1024)
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
    return _factory


# ── the owners ────────────────────────────────────────────────────────

def _identity():
    return DatasetIdentity(path="/x/c4.ome.tif", fingerprint="1:1")


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


def _state(app, windows=CHANNELS):
    state = ChannelDisplayState()
    state.bind(_identity(), install={"order": CHANNELS})
    with state.using_scope(STEP1_SCOPE):
        for channel in CHANNELS:
            state.set_display_visible(channel, True)
            state.set_color(channel, {"DAPI": "#0000ff", "CD3": "#00ff00",
                                      "CD8": "#ff0000"}[channel])
            if channel in windows:
                state.set_mapping(channel, *WINDOW)
        state.set_selected_channel("CD3")
    return state


def _mount(app, decisions=None, windows=CHANNELS, corrected_path=""):
    raws = []
    state = _state(app, windows=windows)
    domain = _domain()
    window = _Window(state, domain, decisions=decisions,
                     corrected_path=corrected_path or "/tmp/c4-none.zarr")
    host = Step1ViewerHost(stack_factory=_stack_factory(raws))
    # THE CPU COMPOSITION IS THE ROLLBACK PATH NOW (G3): the product mount
    # prefers the GPU backend, so this suite -- the C4 gates for the CPU
    # whole-slide composition -- asks for that path explicitly. The GPU
    # backend has its own gates in `tests/test_step1_gpu_takeover.py`.
    mount = Step1WholeSlideMount(window, host=host, gpu=False)
    mount.open("CD3")
    _settle(app)
    return SimpleNamespace(mount=mount, window=window, state=state,
                           domain=domain, raws=raws)


def _settle(app, rounds=60):
    """Let the scheduler's reads and the compose workers land."""
    import time

    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.01)


def _close(rig):
    rig.mount.close()
    QtWidgets.QApplication.processEvents()


def _pixel(item, row, col):
    item.render()
    image = item.qimage
    assert image is not None, "the composed item never rendered"
    colour = QColor.fromRgba(image.pixel(col, row))
    return (colour.red(), colour.green(), colour.blue(), colour.alpha())


def _composed(rig):
    return dict(rig.mount.layer.pool.entries)


def _wait_for_tiles(app, rig, count=1, rounds=80):
    import time

    for _ in range(rounds):
        app.processEvents()
        if len(_composed(rig)) >= count:
            return True
        time.sleep(0.01)
    return len(_composed(rig)) >= count


def _reference(rig, level, tx, ty, mode):
    """The C1 CPU answer for one composed tile, from the same pixels."""
    from block01.ui import step1_draft_spec as draft_spec
    from block01.viewer.tile_types import TileAddress

    stack = rig.mount.host.stack
    spec = draft_spec.build_spec(rig.domain, rig.state, mode)
    channels = (compose_core.fusion_channels(spec.get("groups"),
                                             spec.get("group_weights"),
                                             spec.get("nucleus"))
                if mode == compose_core.MODE_FUSION
                else compose_core.overlay_channels(spec.get("weights")))
    tiles = {}
    for channel in sorted(channels):
        values, _io = stack.provider.read_tile(
            channel, TileAddress(grid=GRID, level=level, tx=tx, ty=ty))
        tiles[channel] = (values, ~np.isnan(values))
    rgba, _valid, _missing = compose_core.compose(
        spec["mode"], tiles, weights=spec.get("weights"),
        colors=spec.get("colors"), mappings=spec.get("mappings"),
        groups=spec.get("groups"), group_weights=spec.get("group_weights"),
        nucleus=spec.get("nucleus") or ("", 0.0))
    return rgba


# ── 1. the pixels on screen ───────────────────────────────────────────

@pytest.mark.parametrize("legacy,mode", [
    (LEGACY_OVERLAY, compose_core.MODE_OVERLAY),
    (LEGACY_FUSION, compose_core.MODE_FUSION),
])
def test_what_is_painted_is_the_cpu_reference(app, legacy, mode):
    """Exit gate 1, through the real `ImageItem.qimage`."""
    rig = _mount(app)
    try:
        rig.mount.set_mode(legacy)
        assert _wait_for_tiles(app, rig), "nothing was composed"

        for (level, tx, ty), entry in sorted(_composed(rig).items()):
            expected = _reference(rig, level, tx, ty, mode)
            if expected is None:
                continue
            for row, col in ((0, 0), (7, 13), (200, 300)):
                assert _pixel(entry.item, row, col) == tuple(
                    int(v) for v in expected[row, col]), (
                        f"tile {(level, tx, ty)} at {(row, col)}")
            break
    finally:
        _close(rig)


def _roi_pixel(level):
    """A (row, col) inside the analysis region, in a level's own pixels."""
    ds = 4.0 if level else 1.0
    row = int((ROI[0] + (ROI[1] - ROI[0]) // 2) / ds)
    col = int((ROI[2] + (ROI[3] - ROI[2]) // 2) / ds)
    return row, col


def test_outside_the_region_is_transparent_and_inside_is_not(app):
    """Exit gate 5: no pixels is not black.

    Both answers are in the SAME composed tile at the whole-slide level --
    the region is a square in the middle of the slide -- so this is one
    picture that is transparent where there are no pixels and opaque where
    there are, which is exactly what a pan across the boundary shows.
    """
    rig = _mount(app, decisions={})
    try:
        assert _wait_for_tiles(app, rig)
        for (level, _tx, _ty), entry in sorted(_composed(rig).items()):
            row, col = _roi_pixel(level)
            assert _pixel(entry.item, row, col)[3] == 255, (
                "inside the analysis region was painted transparent")
            assert _pixel(entry.item, 1, 1)[3] == 0, (
                "outside the region was painted opaque")
            break
        else:
            pytest.fail("nothing was composed")
    finally:
        _close(rig)


# ── 2. what a change does ─────────────────────────────────────────────

def test_a_mode_switch_leaves_no_tile_of_the_old_picture(app):
    """Exit gate 2."""
    rig = _mount(app)
    try:
        assert _wait_for_tiles(app, rig)
        overlay_items = {coord: entry.item
                         for coord, entry in _composed(rig).items()}
        overlay_pixels = {coord: _pixel(entry.item, *_roi_pixel(coord[0]))
                          for coord, entry in _composed(rig).items()}

        rig.mount.set_mode(LEGACY_FUSION)
        assert _composed(rig) == {}, "the old mode's tiles stayed in the pool"
        assert _wait_for_tiles(app, rig)

        changed = [coord for coord, before in overlay_pixels.items()
                   if coord in _composed(rig)
                   and _pixel(_composed(rig)[coord].item,
                              *_roi_pixel(coord[0])) != before]
        assert changed, "the fusion painted the overlay's pixels"
        scene = set(rig.mount.host.stack.view.view_box.addedItems)
        stale = [item for item in overlay_items.values()
                 if item in scene and item not in
                 {e.item for e in _composed(rig).values()}]
        assert stale == [], "an old-mode item was left in the view"
    finally:
        _close(rig)


@pytest.mark.parametrize("move", ["weight", "colour", "intensity"])
def test_a_display_or_weight_change_reads_nothing_from_disk(app, move):
    """Exit gate 4."""
    rig = _mount(app)
    try:
        assert _wait_for_tiles(app, rig)
        reads = len(rig.raws[0].reads)

        if move == "weight":
            rig.domain.edit_channel_weight("CD8", 0.2, origin="user")
        elif move == "colour":
            with rig.state.using_scope(STEP1_SCOPE):
                rig.state.set_color("CD3", "#ffff00")
        else:
            with rig.state.using_scope(STEP1_SCOPE):
                rig.state.set_mapping("CD3", 10.0, 2000.0, 1.1)
        _settle(app, rounds=30)

        assert len(rig.raws[0].reads) == reads, (
            f"a {move} change read tiles from disk")
        assert _composed(rig), "the frame disappeared"
    finally:
        _close(rig)


def test_the_committed_snapshot_is_untouched_by_the_visible_takeover(app):
    """Exit gate 9."""
    rig = _mount(app)
    try:
        committed = rig.domain.committed_snapshot()
        assert _wait_for_tiles(app, rig)

        rig.mount.set_mode(LEGACY_FUSION)
        rig.domain.edit_channel_weight("CD3", 0.3, origin="user")
        _settle(app, rounds=20)

        assert rig.domain.committed_snapshot() == committed
    finally:
        _close(rig)


# ── 3. navigation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("gesture", ["patch", "preview"])
def test_a_jump_puts_real_tiles_in_the_target_viewport(app, gesture):
    """Exit gate 6: the same `jump_to`, and composed tiles where it landed."""
    rig = _mount(app)
    try:
        assert _wait_for_tiles(app, rig)
        target = (ROI[0] + 128, ROI[2] + 128)

        if gesture == "patch":
            moved = rig.mount.show_patch((target[0], target[0] + 256,
                                          target[1], target[1] + 256))
        else:
            moved = rig.mount.jump_to_point(target[0] + 128, target[1] + 128,
                                            256)
        assert moved
        assert _wait_for_tiles(app, rig)
        _settle(app, rounds=40)

        stack = rig.mount.host.stack
        level = stack.controller.level
        covering = {(level, tx, ty) for (tx, ty) in stack.controller._visible_tiles}
        composed = set(_composed(rig))
        assert composed & covering, (
            f"nothing was composed for the viewport {covering}")
    finally:
        _close(rig)


# ── 4. the information layer ──────────────────────────────────────────

def test_a_missing_product_is_said_and_the_rest_still_draws(app):
    """Exit gate 7: a decision with no product on disk."""
    rig = _mount(app, decisions={"CD8": "tophat"},
                 corrected_path="/tmp/c4-does-not-exist.zarr")
    try:
        assert _wait_for_tiles(app, rig)
        notice = rig.mount.notice()

        assert "CD8" in notice and "corrected" in notice.lower(), notice
        stack = rig.mount.host.stack
        # `isVisibleTo`, not `isVisible`: the view itself is never shown in
        # a headless run, and a hidden parent would make every child report
        # invisible whatever the badge was told.
        assert stack.view.status_label.isVisibleTo(stack.view)
        assert "CD8" in stack.view.status_label.text()
        assert rig.mount.layer.pool.entries, "the other channels stopped drawing"
    finally:
        _close(rig)


def test_a_window_still_being_worked_out_is_said_not_guessed(app):
    rig = _mount(app, windows=("DAPI", "CD3"))
    try:
        _settle(app, rounds=30)
        notice = rig.mount.notice()

        assert "CD8" in notice, notice
        assert "display window" in notice, notice
    finally:
        _close(rig)


# ── 5. being looked at, or not ────────────────────────────────────────

def test_step1_composes_nothing_while_it_is_not_on_screen(app):
    """Exit gate 8, first half: Step0's ticks may not drive Step1's frames."""
    rig = _mount(app)
    try:
        assert _wait_for_tiles(app, rig)
        rig.mount.deactivate()
        before = rig.mount.coordinator.generation
        reads = len(rig.raws[0].reads)

        with rig.state.using_scope("step0"):
            rig.state.set_display_visible("CD8", False)
            rig.state.set_color("CD3", "#123456")
        rig.domain.edit_channel_weight("CD8", 0.9, origin="user")
        _settle(app, rounds=20)

        assert rig.mount.coordinator.generation == before, (
            "Step1 recomposed while it was not on screen")
        assert len(rig.raws[0].reads) == reads
    finally:
        _close(rig)


def test_coming_back_composes_once_with_what_moved_while_away(app):
    """Exit gate 8, second half."""
    rig = _mount(app)
    try:
        assert _wait_for_tiles(app, rig)
        rig.mount.deactivate()
        rig.domain.edit_channel_weight("CD8", 0.05, origin="user")
        with rig.state.using_scope(STEP1_SCOPE):
            rig.state.set_color("CD3", "#00ffff")
        before = rig.mount.coordinator.generation

        rig.mount.activate()
        assert _wait_for_tiles(app, rig)
        _settle(app, rounds=40)

        assert rig.mount.coordinator.generation != before
        for (level, tx, ty), entry in sorted(_composed(rig).items()):
            expected = _reference(rig, level, tx, ty,
                                  compose_core.MODE_OVERLAY)
            if expected is None:
                continue
            row, col = _roi_pixel(level)
            assert _pixel(entry.item, row, col) == tuple(
                int(v) for v in expected[row, col]), (
                    "the frame does not show what moved while Step1 was away")
            break
    finally:
        _close(rig)


# ── 6. the tab, and the rollback path ─────────────────────────────────

def test_installing_takes_the_picture_slot_and_keeps_the_old_one(app):
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    rig = _mount(app)
    try:
        page = QWidget()
        layout = QVBoxLayout(page)
        legacy = QWidget()
        layout.addWidget(legacy, stretch=1)

        rig.mount.install(layout, legacy)

        assert layout.indexOf(rig.mount.host) >= 0
        assert legacy.isVisible() is False
        assert layout.indexOf(legacy) >= 0, "the rollback path was removed"

        rig.mount.restore_legacy()
        assert rig.mount.host.isVisible() is False
    finally:
        _close(rig)


def test_closing_gives_back_the_screen_and_stops_the_workers(app):
    rig = _mount(app)
    assert _wait_for_tiles(app, rig)
    stack = rig.mount.host.stack
    controller = stack.controller
    layer = rig.mount.layer

    rig.mount.close()
    QtWidgets.QApplication.processEvents()

    assert layer.pool.entries == {}
    assert controller._marker_visible is True
    assert rig.mount.compose is None and rig.mount.coordinator is None
    assert rig.mount.host.stack is None
    rig.mount.close()                            # idempotent
