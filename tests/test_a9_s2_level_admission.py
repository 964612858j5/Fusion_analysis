"""Block A9 S2a + S2b (application §26, §26.7, §27.6): the Step1/Step3
viewers pick the pyramid level closest to 1:1 (Odon), and every fine plan
is ADMITTED against the per-channel limits and the GPU layer's total
raw-texture budget -- a viewport that does not fit is drawn one level
coarser and reported, never refused and left unresolved, and a submission
never exceeds the total budget.

Synthetic data only; the binding fakes are the ones of
`test_step1_gpu_sources`.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from block01.viewer import request_planning as planning  # noqa: E402
from block01.ui.step1_gpu_binding import BindingBudgets  # noqa: E402

import test_step1_gpu_sources as g  # noqa: E402

TILE_BYTES = 4 * 4 * 4                  # one 4x4 float32 tile of the fakes


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── S2a: the level choice ───────────────────────────────────────────────

@pytest.mark.parametrize("spp, expected", [
    (1.0, 0),        # 1:1 at level 0
    (0.26, 1),       # ideal ds 3.85: level 1 (ds 4) is nearer than 0
    (0.6, 0),        # ideal ds 1.67: nearer to 1 than to 4 in log
    (0.5, 1),        # ln(0.5) vs ln(2): a tie -> the COARSER level
    (0.01, 2),       # far out: the coarsest
    (10.0, 0),       # magnified: the finest
])
def test_the_nearest_level_in_log_scale(spp, expected):
    assert planning.pick_display_level_nearest([1.0, 4.0, 16.0], spp) == expected


def test_the_nearest_choice_ignores_a_non_positive_zoom():
    assert planning.pick_display_level_nearest([1.0, 4.0], 0.0) == 0


def test_log_hysteresis_holds_near_the_midpoint_and_switches_beyond_it():
    ds = [1.0, 4.0, 16.0]
    # the midpoint between levels 0 and 1 is spp 0.5 (ideal ds 2)
    just_past = 0.5 / 1.05                      # nearest is level 1, barely
    assert planning.pick_display_level_nearest(ds, just_past) == 1
    assert planning.apply_level_hysteresis_log(1, 0, ds, just_past) == 0, "held"
    clearly = 0.5 / 1.5
    assert planning.apply_level_hysteresis_log(1, 0, ds, clearly) == 1, "switched"
    # several levels at once go straight to the nearest
    assert planning.apply_level_hysteresis_log(2, 0, ds, 0.01) == 2
    # nothing to hold: the same level, or a current level off the pyramid
    assert planning.apply_level_hysteresis_log(1, 1, ds, 0.3) == 1
    assert planning.apply_level_hysteresis_log(1, 7, ds, 0.3) == 1


def test_the_step1_factory_uses_the_log_policy(app, monkeypatch):
    """The real `build_step1_stack` (pyramid replaced, as in
    test_step1_gpu_overview_skip): its controller picks log-nearest."""
    import test_step1_gpu_overview_skip as ov
    from block01.ui.step1_viewer_host import build_step1_stack
    from block01.viewer import raw_tile_provider
    from block01.viewer.step1_source import Step1SourceTable

    monkeypatch.setattr(raw_tile_provider, "RawTileProvider",
                        lambda path: ov._Pyramid([]))
    table = Step1SourceTable(decisions={}, corrected_zarr_path="",
                             roi_name="ROI_1", roi_bbox=ov.ROI,
                             handoff_revision="rev-1")
    stack = build_step1_stack("/x/s2a.ome.tif", "CD3", table, None, load_overview=False)
    try:
        assert stack.controller.level_policy == planning.LEVEL_POLICY_NEAREST_LOG
    finally:
        stack.teardown()


def test_every_other_controller_keeps_the_nearest_below_policy():
    from block01.viewer.explore_view import ExploreController
    import inspect
    source = inspect.getsource(ExploreController.__init__)
    assert "self.level_policy = planning.LEVEL_POLICY_NEAREST_BELOW" in source


# ── S2b: admission against the total budget ─────────────────────────────

def _budgets(total, fine_tiles=64, fine_bytes=1_000_000):
    return BindingBudgets(64, 1_000_000, fine_tiles, fine_bytes,
                          motion_interval_ms=1, max_raw_texture_bytes=total)


def _union_bytes(descriptor):
    seen = {}
    for source in descriptor.channels:
        for plane in tuple(source.coarse) + tuple(source.fine):
            seen[plane.identity] = np.asarray(plane.values).size * 4
    return sum(seen.values())


def _rig(app, channels, total):
    # two levels: level 0 is 16x16 (4x4 tiles), level 1 the 8x8 coarse
    provider = g._Provider(level_shape=(8, 8), levels=2)
    controller = g._Controller(provider, visible_tiles=((0, 0), (1, 0), (0, 1), (1, 1)),
                               level=0)
    scheduler = g._Scheduler()
    layer = g._RecordingLayer()
    binding, holder = g._binding(provider, scheduler, controller, layer,
                                 display=g._display(channels), budgets=_budgets(total))
    binding.source_changed()
    g._deliver_all(app, scheduler, list(scheduler.requests),
                   value=np.full((4, 4), 0.2, np.float32))
    return provider, controller, scheduler, layer, binding, holder


def test_a_viewport_that_fits_is_admitted_at_its_own_level(app):
    _p, controller, scheduler, layer, binding, _h = _rig(app, ("A", "B"), total=10_000)
    try:
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        asked = scheduler.requests[marker:]
        assert asked and {int(r.key.tile.level) for r in asked} == {0}
        stats = binding.stats()
        assert (stats["ideal_level"], stats["admitted_level"]) == (0, 0)
        assert stats["resolution_limited"] is False
    finally:
        binding.dispose()


def test_over_the_total_budget_the_coarser_level_is_admitted_and_reported(app):
    # coarse 2 x 4 tiles = 512 B; level-0 fine 2 x 4 tiles = 512 B more
    _p, controller, scheduler, layer, binding, _h = _rig(app, ("A", "B"), total=900)
    try:
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        g._deliver_all(app, scheduler, scheduler.requests[marker:],
                       value=np.full((4, 4), 0.7, np.float32))
        asked = scheduler.requests[marker:]
        assert not [r for r in asked if int(r.key.tile.level) == 0], \
            "the level that does not fit is never half-requested"
        stats = binding.stats()
        assert (stats["ideal_level"], stats["admitted_level"]) == (0, 1)
        assert stats["resolution_limited"] is True
        assert stats["fine_budget_refused"] == ()
        for descriptor, _d, _v in layer.calls:
            assert _union_bytes(descriptor) <= 900, "a submission over the total budget"
    finally:
        binding.dispose()


def test_ticking_a_channel_readmits_every_active_channel(app):
    """A alone fits at level 0; with B ticked the total no longer does, so
    A is re-planned one level coarser too -- through `refresh_display`,
    with the camera standing still (codex: that path bypassed admission)."""
    _p, controller, scheduler, layer, binding, holder = _rig(app, ("A",), total=900)
    try:
        binding.update_viewport(controller.snapshot())
        g._deliver_all(app, scheduler, g._fine_requests(scheduler, "A"),
                       value=np.full((4, 4), 0.7, np.float32))
        assert binding.stats()["admitted_level"] == 0
        holder[0] = g._display(("A", "B"))
        binding.refresh_display()
        g._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.4, np.float32))
        stats = binding.stats()
        assert stats["admitted_level"] == 1 and stats["resolution_limited"] is True
        assert set(stats["coarse_channels"]) == {"A", "B"}
        for descriptor, _d, _v in layer.calls:
            assert _union_bytes(descriptor) <= 900
    finally:
        binding.dispose()


def test_an_unchanged_admission_keeps_the_request_already_under_way(app):
    _p, controller, scheduler, layer, binding, holder = _rig(app, ("A", "B"), total=10_000)
    try:
        binding.update_viewport(controller.snapshot())
        before = len(scheduler.requests)
        cancelled = len(scheduler.cancelled)
        holder[0] = g._display(("A",))                 # untick B
        binding.refresh_display()
        g._events(app)
        assert len(scheduler.requests) == before, "A was asked for again"
        # a generation token is ("step1-gpu-binding", id, tier, revision,
        # channel, epoch, serial)
        assert scheduler.cancelled[cancelled:], "B's request was not cancelled"
        assert all(c[4] != "A" for c in scheduler.cancelled[cancelled:]), \
            "A's request was cancelled"
    finally:
        binding.dispose()


def test_channels_are_admitted_together_from_the_first_coarse(app):
    """codex P1: before any viewport epoch, a complete coarse landing must
    not admit its channel alone -- A and B share the total budget."""
    provider = g._Provider(level_shape=(8, 8), levels=2)
    controller = g._Controller(provider, visible_tiles=((0, 0), (1, 0), (0, 1), (1, 1)), level=0)
    scheduler = g._Scheduler()
    layer = g._RecordingLayer()
    binding, _h = g._binding(provider, scheduler, controller, layer,
                             display=g._display(("A", "B")), budgets=_budgets(900))
    try:
        binding.source_changed()        # records the viewport; no epoch yet
        g._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.2, np.float32))
        g._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.5, np.float32))
        assert not [r for r in scheduler.requests
                    if r.priority != g.PRIORITY_COARSE and int(r.key.tile.level) == 0]
        assert binding.stats()["admitted_level"] == 1
        for descriptor, _d, _v in layer.calls:
            assert _union_bytes(descriptor) <= 900
    finally:
        binding.dispose()


def test_base_layers_over_the_total_keep_the_last_frame_and_recover(app):
    """Delegated decision 2026-10-09 (codex astra high): if the complete
    coarse of the active channels alone exceeds the budget, nothing is
    submitted (no channel is silently omitted, the layer is never handed a
    set it must refuse), the count is reported, and the view recovers once
    the selection fits again."""
    # each channel's coarse is 4 tiles = 256 B; the cap fits exactly one
    _p, controller, scheduler, layer, binding, holder = _rig(app, ("A",), total=256)
    try:
        binding.update_viewport(controller.snapshot())
        g._events(app)
        assert binding.stats()["base_overflow_channels"] == 0
        frames = len(layer.calls)
        holder[0] = g._display(("A", "B"))
        binding.refresh_display()
        g._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.4, np.float32))
        stats = binding.stats()
        assert stats["base_overflow_channels"] == 2
        for descriptor, _d, _v in layer.calls[frames:]:
            assert _union_bytes(descriptor) <= 256, "an over-budget set was submitted"
        holder[0] = g._display(("A",))                 # back within the cap
        binding.refresh_display()
        g._events(app)
        assert binding.stats()["base_overflow_channels"] == 0
        latest = layer.calls[-1][0]
        assert {s.channel for s in latest.channels} == {"A"}
    finally:
        binding.dispose()


def test_a_partly_delivered_identical_plan_is_kept(app):
    """codex P2: a re-plan against an unchanged admission keeps a plan that
    has already delivered some of its tiles -- nothing is asked again."""
    _p, controller, scheduler, layer, binding, holder = _rig(app, ("A",), total=10_000)
    try:
        binding.update_viewport(controller.snapshot())
        fine = g._fine_requests(scheduler, "A")
        g._deliver_all(app, scheduler, fine[:1], value=np.full((4, 4), 0.7, np.float32))
        before, cancelled = len(scheduler.requests), len(scheduler.cancelled)
        holder[0] = g._display(("A", "B"))
        binding.refresh_display()
        g._events(app)
        again = [r for r in scheduler.requests[before:] if r.key.channel == "A"]
        assert again == [], "A's unfinished tiles were asked for again"
        assert all(c[4] != "A" for c in scheduler.cancelled[cancelled:])
    finally:
        binding.dispose()


def test_a_readmission_back_to_a_resident_level_cancels_the_finer_requests(app):
    """codex: A shows level 1, zooms to level 0 with reads pending, then B
    is ticked and the admission falls back to the resident level 1 -- A's
    level-0 requests must be cancelled, and their late tiles never land."""
    _p, controller, scheduler, layer, binding, holder = _rig(app, ("A",), total=1000)
    try:
        # first the coarse level as A's own target (a far zoom)
        controller.set_snapshot(visible_tiles=((0, 0), (1, 0), (0, 1), (1, 1)), level=1, epoch=2)
        binding.update_viewport(controller.snapshot())
        g._deliver_all(app, scheduler, g._fine_requests(scheduler, "A"),
                       value=np.full((4, 4), 0.6, np.float32))
        # zoom in: level 0 fits for A alone, requests left pending
        controller.set_snapshot(visible_tiles=((0, 0), (1, 0), (0, 1), (1, 1)), level=0, epoch=3)
        controller.snapshot().bbox_l0 = (0, 0, 8, 8)    # the area those tiles cover
        marker = len(scheduler.requests)
        binding.update_viewport(controller.snapshot())
        pending = [r for r in scheduler.requests[marker:] if int(r.key.tile.level) == 0]
        assert pending and binding.stats()["admitted_level"] == 0
        # A's level-1 planes are kept as stand-ins (the carry allows them)
        assert 1 in binding.stats()["fine_levels"]["A"]
        cancelled = len(scheduler.cancelled)
        holder[0] = g._display(("A", "B"))
        binding.refresh_display()
        g._deliver_all(app, scheduler, [r for r in scheduler.requests if r.key.channel == "B"],
                       value=np.full((4, 4), 0.4, np.float32))
        assert binding.stats()["admitted_level"] == 1
        assert any(c[4] == "A" for c in scheduler.cancelled[cancelled:]), \
            "A's level-0 requests were left live"
        g._deliver_all(app, scheduler, pending, value=np.full((4, 4), 0.9, np.float32))
        levels = binding.stats()["fine_levels"].get("A", ())
        assert 0 not in levels, "a late level-0 tile was accepted"
        for descriptor, _d, _v in layer.calls:
            assert _union_bytes(descriptor) <= 1000
    finally:
        binding.dispose()


# ── §38: the profile's shortcuts give the answers they replace ───────────

def _planes(descriptor):
    return [(s.channel, tuple(id(p) for p in s.coarse), tuple(id(p) for p in s.fine),
             s.selected_level, s.target_level) for s in descriptor.channels]


@pytest.mark.parametrize("tiles, level", [(((1, 1), (2, 1), (1, 2), (2, 2)), 0),
                                          (((0, 0), (1, 0), (0, 1), (1, 1)), 1)],
                         ids=["same-level-pan", "zoom-out-to-coarsest"])
def test_a_camera_move_publishes_what_a_fresh_build_would(app, tiles, level):
    """Admission's shared bytes / rect equal the per-key ones; the publish
    cache (no longer keyed by the admission object) gives the sources a
    build from scratch gives, after a pan and after a zoom-out."""
    _p, controller, scheduler, layer, binding, _h = _rig(app, ("A", "B"), total=10_000)
    try:
        binding.update_viewport(controller.snapshot())
        g._deliver_all(app, scheduler, list(scheduler.requests),
                       value=np.full((4, 4), 0.5, np.float32))
        marker = len(scheduler.requests)
        binding.update_viewport(controller.set_snapshot(visible_tiles=tiles, level=level, epoch=2))
        g._deliver_all(app, scheduler, scheduler.requests[marker:],
                       value=np.full((4, 4), 0.6, np.float32))
        admission = binding._admission
        assert admission.level == level and admission.keys.get("A")
        for channel in ("A", "B"):
            keys = set(admission.keys.get(channel, frozenset()))
            if keys:
                assert admission.fine_bytes[channel] == binding._planned_bytes(keys)
                assert admission.rect == binding._keys_world_rect(keys)
        binding._publish_current()
        cached = _planes(layer.calls[-1][0])
        binding._source_cache.clear()
        binding._publish_current()
        assert cached == _planes(layer.calls[-1][0])
    finally:
        binding.dispose()
