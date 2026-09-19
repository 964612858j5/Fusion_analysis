"""Step1's composed frame against the REAL owners: the draft and the display.

Block C3 of `docs/step1_rework_plan.md`. C1 settled the arithmetic and C2 the
planning; here the spec comes from `FusionDomainModel.draft_snapshot()` /
`effective_config()` and `ChannelDisplayState`, and every change reaches the
coordinator as a WHOLE new spec through the public binding.

The exit gates the plan names: an unsaved edit moves the picture and not the
committed snapshot; Search and Generate keep reading the committed one until a
Save replaces it; an old session restores a committed snapshot and a draft that
agrees with it; a window arriving re-composes through the public wiring (this
module never reaches into the coordinator's private last frame); fast
edits end on the last draft's pixels;
and a weight change reads nothing from disk.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core.display_identity import DatasetIdentity  # noqa: E402
from block01.core.fusion_domain import FusionDomainModel  # noqa: E402
from block01.ui.block01_display import ChannelDisplayState  # noqa: E402
from block01.ui.step1_compose_binding import (  # noqa: E402
    Step1ComposeBinding,
)
from block01.ui.step1_compose_coordinator import (  # noqa: E402
    Step1ComposeCoordinator,
)
from block01.ui import step1_draft_spec as draft_spec  # noqa: E402
from block01.viewer import step1_compose as compose_core  # noqa: E402
from block01.viewer.tile_types import (  # noqa: E402
    SourceIdentity, TileAddress, TileGridSpec,
)

GRID = TileGridSpec(tile_size=512, source_chunk_shape=(), grid_version="v1")
WINDOW = (0.0, 100.0, 1.0)
CHANNELS = ("DAPI", "CD3", "CD8")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the viewer's machinery, as C2's tests stand it up ─────────────────

class _Cache(dict):
    def get(self, key):
        return dict.get(self, key)

    def put(self, key, value):
        self[key] = value
        return value


class _Scheduler:
    def __init__(self, provider):
        self.provider = provider
        self.cache = _Cache()
        self.reads = 0
        self.cancelled = []
        self._deferred = []

    def _cache_for(self, _key):
        return self.cache

    def cancel_generation(self, generation):
        self.cancelled.append(generation)

    def request(self, req, callback):
        cached = self.cache.get(req.key)
        if cached is not None:
            callback(SimpleNamespace(request=req, pixels=cached, error=None))
            return
        self._deferred.append((req, callback))

    def deliver(self):
        queued, self._deferred = self._deferred, []
        for req, callback in queued:
            values, _io = self.provider.read_tile(req.key.channel, req.key.tile)
            self.reads += 1
            self.cache.put(req.key, values)
            callback(SimpleNamespace(request=req,
                                     pixels=SimpleNamespace(handle=values),
                                     error=None))


class _Executor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def run(self):
        jobs, self.jobs = self.jobs, []
        for fn, args, kwargs in jobs:
            fn(*args, **kwargs)
        return len(jobs)


class _Provider:
    def __init__(self):
        self._identity = SourceIdentity(dataset_path="/x/c3.ome.tif",
                                        dataset_fingerprint="1:1",
                                        stage="step1",
                                        corrected_artifact="token-1")

    def source_identity(self):
        return self._identity

    def read_tile(self, channel, tile):
        base = {"DAPI": 70.0, "CD3": 10.0, "CD8": 40.0}.get(channel, 5.0)
        return np.full((8, 8), base + tile.tx + tile.ty, np.float32), 0.0


class _Host:
    def __init__(self):
        self.provider = _Provider()
        self.scheduler = _Scheduler(self.provider)
        self.controller = SimpleNamespace(level=0, grid=GRID,
                                          _visible_tiles={(1, 1)})
        self.stack = SimpleNamespace(provider=self.provider,
                                     scheduler=self.scheduler,
                                     controller=self.controller)


def _settle(app, coordinator, executor, rounds=8):
    for _ in range(rounds):
        app.processEvents()
        ran = executor.run()
        app.processEvents()
        if not ran and not executor.jobs:
            break


# ── the owners ────────────────────────────────────────────────────────

def _domain():
    """A draft like the one a project arrives with: two markers in a group,
    DAPI as the nucleus, all three in the science.

    Installed through the RESTORE path, which puts back what it is given --
    `set_fusion_enabled` would run the first-enable rule and write 1.0 over
    CD8's 0.5, which is the right rule for a user's tick and the wrong one
    for a project that already has numbers.
    """
    domain = FusionDomainModel()
    identity = DatasetIdentity(path="/x/c3.ome.tif", fingerprint="1:1")
    domain.bind_dataset(identity)
    domain.prepare_restore(identity, {
        "groups": {"markers": {"group_weight": 1.0,
                               "channels": {"CD3": 1.0, "CD8": 0.5}}},
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "enabled": list(CHANNELS)})
    domain.commit_restore("fixture")
    return domain


def _state(app, windows=CHANNELS):
    state = ChannelDisplayState()
    state.bind(DatasetIdentity(path="/x/c3.ome.tif", fingerprint="1:1"),
               install={"order": CHANNELS})
    with state.using_scope(draft_spec.STEP1_SCOPE):
        for channel in CHANNELS:
            state.set_display_visible(channel, True)
            state.set_color(channel, "#00ff00" if channel == "CD3"
                            else "#ff0000")
            if channel in windows:
                state.set_mapping(channel, *WINDOW)
    return state


def _wired(app, mode=compose_core.MODE_OVERLAY, windows=CHANNELS):
    host = _Host()
    executor = _Executor()
    coordinator = Step1ComposeCoordinator(host, executor=executor)
    domain = _domain()
    state = _state(app, windows=windows)
    binding = Step1ComposeBinding(coordinator, domain, state, mode=mode)
    binding.connect()
    return SimpleNamespace(host=host, executor=executor,
                           coordinator=coordinator, domain=domain,
                           state=state, binding=binding)


def _frame(app, rig):
    """One composed frame of the current draft, as pixels."""
    frames = []
    handle = rig.coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.append(rgba))
    rig.binding.refresh("test")
    rig.host.scheduler.deliver()
    _settle(app, rig.coordinator, rig.executor)
    rig.coordinator.tile_composed.disconnect(handle)
    return frames[-1] if frames else None


# ── 1. the spec is the draft's ────────────────────────────────────────

def test_the_overlay_spec_is_the_drafts_weights_and_the_states_colours(app):
    rig = _wired(app)

    spec = rig.binding.spec()

    assert spec["mode"] == compose_core.MODE_OVERLAY
    assert spec["weights"] == {"DAPI": 1.0, "CD3": 1.0, "CD8": 0.5}
    assert spec["colors"]["CD3"] == (0.0, 1.0, 0.0)
    assert spec["mappings"]["CD8"] == WINDOW


def test_the_fusion_spec_is_the_effective_config(app):
    rig = _wired(app, mode=compose_core.MODE_FUSION)

    spec = rig.binding.spec()
    effective = rig.domain.effective_config()

    assert spec["mode"] == compose_core.MODE_FUSION
    assert spec["groups"] == {"markers": {"CD3": 1.0, "CD8": 0.5}}
    assert spec["group_weights"] == {"markers": 1.0}
    assert spec["nucleus"] == ("DAPI", 1.0)
    assert set(spec["groups"]["markers"]) == set(
        (effective["groups"]["markers"]["channels"]))


def test_a_channel_with_no_window_is_left_out_rather_than_guessed(app):
    rig = _wired(app, windows=("DAPI", "CD3"))

    spec = rig.binding.spec()

    assert "CD8" not in spec["mappings"]
    assert "CD8" in spec["weights"], "the channel left the draft as well"


def test_the_ticks_are_read_in_step1s_own_scope(app):
    """Standing in Step0 may not change what Step1 composes -- including
    while the user IS standing there, which is when the current scope is
    Step0's and reading it would hand back Step0's answer."""
    rig = _wired(app)
    with rig.state.using_scope("step0"):
        rig.state.set_display_visible("CD3", False)
        standing_in_step0 = rig.binding.spec()

    assert "CD3" in standing_in_step0["weights"], (
        "Step1 composed Step0's ticks")
    assert "CD3" in rig.binding.spec()["weights"]


def test_the_group_weight_scales_what_an_overlay_channel_shows(app):
    """The same number the fusion's `gw * w` gives it, so the two pictures
    agree about strength (`MainWindow._overlay_weight`)."""
    rig = _wired(app)
    rig.domain.set_group_weight("markers", 0.5)

    weights = rig.binding.spec()["weights"]

    assert weights["CD3"] == pytest.approx(0.5)      # 0.5 * 1.0
    assert weights["CD8"] == pytest.approx(0.25)     # 0.5 * 0.5
    assert weights["DAPI"] == pytest.approx(1.0), "the nucleus took a group's"


def test_unticking_a_channel_takes_it_out_of_the_overlay(app):
    rig = _wired(app)
    with rig.state.using_scope(draft_spec.STEP1_SCOPE):
        rig.state.set_display_visible("CD8", False)

    assert "CD8" not in rig.binding.spec()["weights"]
    assert rig.domain.fusion_enabled("CD8"), "a tick changed the science"


# ── 2. draft vs committed ─────────────────────────────────────────────

def _committed_snapshot(domain, name="one"):
    return {"hash": name, "fusion_config": domain.effective_config()}


def test_an_unsaved_edit_moves_the_picture_and_not_the_committed_snapshot(app):
    """Exit gate 1."""
    rig = _wired(app)
    rig.domain.install_committed_snapshot(_committed_snapshot(rig.domain))
    committed = rig.domain.committed_snapshot()
    before = _frame(app, rig)
    reads = rig.host.scheduler.reads

    rig.domain.edit_channel_weight("CD8", 0.1, origin="user")
    after = _frame(app, rig)

    assert before is not None and after is not None
    assert not np.array_equal(before, after), "the draft edit changed no pixel"
    assert rig.domain.committed_snapshot() == committed, (
        "an unsaved edit reached the committed snapshot")
    assert rig.host.scheduler.reads == reads, (
        "a weight change read tiles from disk")            # exit gate 7


def test_search_and_generate_keep_reading_the_committed_snapshot(app):
    """Exit gate 2: what a job runs on is not what the screen shows."""
    rig = _wired(app)
    rig.domain.install_committed_snapshot(_committed_snapshot(rig.domain))
    committed = rig.domain.committed_snapshot()

    rig.domain.edit_channel_weight("CD3", 0.25, origin="user")
    _frame(app, rig)

    assert rig.domain.committed_snapshot() == committed
    assert (committed["fusion_config"]["groups"]["markers"]["channels"]["CD3"]
            == 1.0)
    assert rig.binding.spec()["weights"]["CD3"] == 0.25


def test_a_save_is_what_replaces_what_the_jobs_read(app):
    """Exit gate 3."""
    rig = _wired(app)
    rig.domain.install_committed_snapshot(_committed_snapshot(rig.domain))
    rig.domain.edit_channel_weight("CD3", 0.25, origin="user")

    # What Save does, through the model's own entry.
    rig.domain.install_committed_snapshot(
        _committed_snapshot(rig.domain, "two"))

    snapshot = rig.domain.committed_snapshot()
    assert snapshot["hash"] == "two"
    assert (snapshot["fusion_config"]["groups"]["markers"]["channels"]["CD3"]
            == 0.25)


def test_the_binding_never_writes_to_the_domain(app):
    rig = _wired(app)
    before = rig.domain.draft_snapshot()

    rig.binding.spec()
    _frame(app, rig)
    rig.binding.set_mode(compose_core.MODE_FUSION)
    _frame(app, rig)

    assert rig.domain.draft_snapshot() == before


# ── 3. an old session ─────────────────────────────────────────────────

def test_an_old_session_restores_a_committed_snapshot_and_a_draft_to_match(app):
    """Exit gate 4: through `prepare_restore`/`commit_restore`, not by
    writing the model's internals."""
    rig = _wired(app)
    identity = DatasetIdentity(path="/x/c3.ome.tif", fingerprint="1:1")
    restored = {
        "groups": {"markers": {"group_weight": 0.5,
                               "channels": {"CD3": 0.3, "CD8": 0.0}}},
        "nucleus": {"channel": "DAPI", "weight": 0.8},
        "enabled": ["CD3", "CD8", "DAPI"],
    }

    rig.domain.prepare_restore(identity, restored)
    rig.domain.commit_restore("session")
    rig.domain.install_committed_snapshot(
        {"hash": "restored", "fusion_config": rig.domain.effective_config()})

    committed = rig.domain.committed_snapshot()
    spec = draft_spec.build_spec(rig.domain, rig.state,
                                 compose_core.MODE_FUSION)
    assert committed["fusion_config"]["groups"] == spec_groups(spec)
    assert spec["nucleus"] == ("DAPI", 0.8)
    assert spec["group_weights"] == {"markers": 0.5}
    # Exit gate: the explicit 0.0 survived the round trip.
    assert spec["groups"]["markers"]["CD8"] == 0.0
    assert rig.domain.fusion_enabled("CD8")


def spec_groups(spec):
    return {name: {"group_weight": spec["group_weights"][name],
                   "channels": channels}
            for name, channels in spec["groups"].items()}


def test_a_heterogeneous_group_weight_survives_into_the_frame(app):
    rig = _wired(app, mode=compose_core.MODE_FUSION)
    identity = DatasetIdentity(path="/x/c3.ome.tif", fingerprint="1:1")
    rig.domain.prepare_restore(identity, {
        "groups": {"a": {"group_weight": 1.0, "channels": {"CD3": 0.9}},
                   "b": {"group_weight": 0.25, "channels": {"CD8": 0.4}}},
        "nucleus": {"channel": "DAPI", "weight": 1.0},
        "enabled": ["CD3", "CD8", "DAPI"]})
    rig.domain.commit_restore("session")

    spec = rig.binding.spec()

    assert spec["groups"] == {"a": {"CD3": 0.9}, "b": {"CD8": 0.4}}
    assert spec["group_weights"] == {"a": 1.0, "b": 0.25}


def test_unticking_and_ticking_again_keeps_the_history(app):
    rig = _wired(app)
    rig.domain.edit_channel_weight("CD8", 0.35, origin="user")

    rig.domain.set_fusion_enabled("CD8", False, origin="user")
    rig.domain.set_fusion_enabled("CD8", True, origin="user")

    assert rig.binding.spec()["weights"]["CD8"] == pytest.approx(0.35)


def test_selecting_a_channel_changes_neither_weight_nor_participation(app):
    """Clicking a name is a display act."""
    rig = _wired(app)
    before = rig.domain.draft_snapshot()

    with rig.state.using_scope(draft_spec.STEP1_SCOPE):
        rig.state.set_selected_channel("CD8")

    assert rig.domain.draft_snapshot() == before


# ── 4. the wiring, and the generation ─────────────────────────────────

def test_a_draft_edit_advances_the_generation_by_itself(app):
    rig = _wired(app)
    rig.binding.refresh("first")
    before = rig.coordinator.generation

    rig.domain.edit_channel_weight("CD3", 0.4, origin="user")

    assert rig.coordinator.generation != before
    assert before in rig.host.scheduler.cancelled


@pytest.mark.parametrize("move", ["colour", "visibility"])
def test_structural_display_change_advances_the_generation(app, move):
    rig = _wired(app)
    rig.binding.refresh("first")
    before = rig.coordinator.generation

    with rig.state.using_scope(draft_spec.STEP1_SCOPE):
        if move == "colour":
            rig.state.set_color("CD3", "#0000ff")
        else:
            rig.state.set_display_visible("CD8", False)

    assert rig.coordinator.generation != before
    assert before in rig.host.scheduler.cancelled


def test_mapping_display_change_keeps_structural_generation(app):
    rig = _wired(app)
    rig.binding.refresh("first")
    before = rig.coordinator.generation

    with rig.state.using_scope(draft_spec.STEP1_SCOPE):
        rig.state.set_mapping("CD3", 1.0, 200.0, 1.0)

    assert rig.coordinator.generation == before


def test_a_window_arriving_recomposes_through_the_public_wiring(app):
    """Exit gate 5: the seed lands in the SHARED STATE, the binding builds a
    whole new spec from it, and nothing edits the coordinator's last frame."""
    rig = _wired(app, windows=("DAPI", "CD3"))
    named = []
    rig.coordinator.windows_missing.connect(named.append)
    partial = _frame(app, rig)
    assert named and named[-1] == ["CD8"], named

    frames = []
    rig.coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.append(rgba))
    with rig.state.using_scope(draft_spec.STEP1_SCOPE):
        rig.state.set_mapping("CD8", *WINDOW)     # the seed lands
    rig.host.scheduler.deliver()
    _settle(app, rig.coordinator, rig.executor)

    assert frames, "the arriving window composed nothing"
    assert not np.array_equal(frames[-1], partial), (
        "the frame did not take the new window")
    assert named[-1] == [], "the missing notice stayed up"


def test_this_module_never_reaches_into_the_coordinators_private_frame():
    """Exit gate 5 again, as a rule rather than an outcome."""
    import inspect

    forbidden = "_last" + "_spec"          # built, so this line is not a hit
    source = inspect.getsource(inspect.getmodule(_wired))
    assert forbidden not in source, (
        "a test reached into the coordinator's private last frame")


def test_fast_edits_end_on_the_last_drafts_pixels(app):
    """Exit gate 6."""
    rig = _wired(app)
    _frame(app, rig)                       # the tiles are in the tile cache
    frames = []
    rig.coordinator.tile_composed.connect(
        lambda level, tx, ty, rgba, valid: frames.append(rgba))

    for weight in (0.9, 0.6, 0.2):
        rig.domain.edit_channel_weight("CD8", weight, origin="user")
    _settle(app, rig.coordinator, rig.executor)

    assert frames, "the edits composed nothing"
    final = draft_spec.build_spec(rig.domain, rig.state)
    tiles = {}
    for channel in sorted(final["weights"]):
        values, _io = rig.host.provider.read_tile(
            channel, TileAddress(grid=GRID, level=0, tx=1, ty=1))
        tiles[channel] = (values, np.ones(values.shape, bool))
    expected, _valid, _missing = compose_core.compose(
        final["mode"], tiles, weights=final["weights"],
        colors=final["colors"], mappings=final["mappings"])
    assert np.array_equal(frames[-1], expected), (
        "the screen ended on a draft the user had already moved past")


def test_the_mode_switch_is_a_new_generation_and_a_new_picture(app):
    rig = _wired(app)
    overlay = _frame(app, rig)
    reads = rig.host.scheduler.reads

    rig.binding.set_mode(compose_core.MODE_FUSION)
    rig.host.scheduler.deliver()
    _settle(app, rig.coordinator, rig.executor)
    fusion = _frame(app, rig)

    assert not np.array_equal(overlay, fusion)
    assert rig.host.scheduler.reads == reads, (
        "the mode switch re-read tiles it already had")


def test_disconnecting_stops_the_frames(app):
    rig = _wired(app)
    rig.binding.refresh("first")
    rig.binding.disconnect_owners()
    before = rig.coordinator.generation

    rig.domain.edit_channel_weight("CD3", 0.7, origin="user")

    assert rig.coordinator.generation == before


# ── 5. C4: what a change does to what is already on screen ────────────

class _Layer:
    """As much of `Step1ComposedLayer` as the binding touches."""

    def __init__(self):
        self.tiles = []
        self.cleared = 0

    def on_tile_composed(self, level, tx, ty, rgba, valid=None):
        self.tiles.append((level, tx, ty))

    def clear(self):
        self.cleared += 1


def test_the_composed_tiles_reach_the_layer(app):
    rig = _wired(app)
    layer = _Layer()
    rig.binding.attach_layer(layer)

    _frame(app, rig)

    assert layer.tiles, "nothing was drawn"


@pytest.mark.parametrize("reason", ["mode", "source", "dataset",
                                    "draft-restored", "state-installed"])
def test_a_different_picture_clears_the_old_one(app, reason):
    """Gate 2: the old mode's or the old slide's tiles are not a coarser
    version of the new picture, and panning back to one would show it."""
    rig = _wired(app)
    layer = _Layer()
    rig.binding.attach_layer(layer)
    _frame(app, rig)

    rig.binding.refresh(reason)

    assert layer.cleared == 1, reason


@pytest.mark.parametrize("reason", ["draft", "colour", "window", "visibility"])
def test_a_moved_number_replaces_the_tiles_rather_than_clearing_them(app, reason):
    """The picture stays up while a weight settles: every visible tile is
    composed again at the same coordinates and replaced in place."""
    rig = _wired(app)
    layer = _Layer()
    rig.binding.attach_layer(layer)
    _frame(app, rig)
    drawn = len(layer.tiles)

    rig.binding.refresh(reason)
    _settle(app, rig.coordinator, rig.executor)

    assert layer.cleared == 0, reason
    assert len(layer.tiles) > drawn, "the frame was not composed again"


def test_the_mode_switch_clears_before_it_composes(app):
    rig = _wired(app)
    layer = _Layer()
    rig.binding.attach_layer(layer)
    _frame(app, rig)

    rig.binding.set_mode(compose_core.MODE_FUSION)

    assert layer.cleared == 1


def test_detaching_the_layer_stops_the_tiles(app):
    rig = _wired(app)
    layer = _Layer()
    rig.binding.attach_layer(layer)
    _frame(app, rig)
    drawn = len(layer.tiles)

    rig.binding.detach_layer()
    rig.binding.refresh("draft")
    _settle(app, rig.coordinator, rig.executor)

    assert len(layer.tiles) == drawn
