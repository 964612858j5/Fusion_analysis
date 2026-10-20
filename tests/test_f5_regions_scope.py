"""Regions' channel ticks only show (docs/f5_tma_display_plan.md §13, user
ruling 2026-10-20): its own display-only scope, seeded once from Step1's
current ticks; ticking there never changes Step1's fusion or Step1's ticks,
the Step1 session and the Tissue preview keep Step1's, and its Overlay shows
every ticked channel at full strength. The real window (the R2 rig)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from test_regions_r2 import _enter_regions, _pump  # noqa: E402
from test_step0_compare_tiles import app  # noqa: E402,F401
from test_step1_shared_camera import _close, _in, _window  # noqa: E402

from block01.ui import step1_draft_spec as ds  # noqa: E402


def _ticks(state, scope):
    with state.using_scope(scope):
        return {c for c, v in state.display_visibility().items() if v}


def test_regions_ticks_show_only_and_never_reach_step1(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state, fusion = w._display.state, w._display.fusion
        step1_before = _ticks(state, "step1")
        draft_before = fusion.draft_snapshot()
        _enter_regions(rig)
        assert state.scope() == "regions"
        assert _ticks(state, "regions") == step1_before                  # seeded from Step1's
        assert w._channel_dock._step == 2                                 # display-only rows
        other = next(c for c in state.channel_order() if c not in step1_before)
        w._channel_dock.row(other).checkbox.setChecked(True)              # the user ticks one
        _pump()
        assert other in _ticks(state, "regions")
        assert _ticks(state, "step1") == step1_before                     # Step1 untouched
        assert fusion.draft_snapshot() == draft_before                    # the fusion untouched
        payload = w._step1_session_payload()                              # a save while in Regions
        assert {c for c, v in payload["display_visibility"].items() if v} == step1_before
        spec = w._regions_view.mount._spec()
        if spec.get("mode") == ds.MODE_OVERLAY:
            assert spec["weights"].get(other) == 1.0                       # shown at full strength
        rig.w._stack.setCurrentIndex(1)
        _in(rig, 1)
        _pump()
        assert state.scope() == "step1" and _ticks(state, "step1") == step1_before
        _enter_regions(rig)
        assert other in _ticks(state, "regions")                          # Regions keeps its own
    finally:
        _close(rig)


def test_display_only_overlay_shows_a_never_fused_channel():
    class _Dom:
        def fusion_enabled(self, c):
            return c == "DAPI"

        def nucleus(self):
            return ("DAPI", 1.0)

        def groups(self):
            return {}

        def group_weight(self, n):
            return 1.0

    class _State:
        def using_scope(self, s):
            import contextlib
            return contextlib.nullcontext()

        def display_visibility(self):
            return {"DAPI": True, "CD8": True}

        def channel_order(self):
            return ["DAPI", "CD8"]

        def color(self, c):
            return "#ffffff"

        def mapping(self, c):
            return (0.0, 1.0, 1.0)

    science = ds.build_spec(_Dom(), _State(), ds.MODE_OVERLAY, scope="step1")
    shown = ds.build_spec(_Dom(), _State(), ds.MODE_OVERLAY, scope="regions", display_only=True)
    assert "CD8" not in science["weights"]                                 # Step1: weight 0, not drawn
    assert shown["weights"] == {"DAPI": 1.0, "CD8": 1.0}


def test_the_other_mounts_keep_step1s_scope():
    from block01.ui.step1_viewer_mount import Step1WholeSlideMount
    import inspect
    sig = inspect.signature(Step1WholeSlideMount.__init__)
    assert sig.parameters["display_scope"].default == "step1"
    assert sig.parameters["display_only"].default is False


def test_intensity_follows_the_shown_steps_current_channel(app, monkeypatch, tmp_path):
    """§13.6: an open Intensity window points at Regions' current channel
    in Regions and back at Step1's in Step1."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state = w._display.state
        focused = []
        monkeypatch.setattr(w._display, "focus_intensity", lambda ch: focused.append(ch) or True)
        chans = list(state.channel_order())
        with state.using_scope("step1"):
            state.set_selected_channel(chans[0])
        _enter_regions(rig)
        state.set_selected_channel(chans[1])                    # Regions selects B
        _pump()
        assert focused[-1] == chans[1]
        rig.w._stack.setCurrentIndex(1)
        _in(rig, 1)
        _pump()
        assert focused[-1] == chans[0]                          # back on Step1's A
        with state.using_scope("step1"):
            assert state.selected_channel() == chans[0]        # Step1's selection untouched
    finally:
        _close(rig)


def test_fusion_mode_keeps_step1s_model_and_the_tissue_preview_step1s_ticks(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state, fusion = w._display.state, w._display.fusion
        step1_before = _ticks(state, "step1")
        model_before = fusion.effective_config()
        _enter_regions(rig)
        other = next(c for c in state.channel_order() if c not in step1_before)
        w._channel_dock.row(other).checkbox.setChecked(True)
        _pump()
        spec = ds.build_spec(fusion, state, ds.MODE_FUSION, scope="regions", display_only=True)
        assert fusion.effective_config() == model_before                     # the science is Step1's
        for members in (spec.get("groups") or {}).values():
            assert other not in members                                      # ticking does not fuse it
        seen = []                                                            # Tissue preview: Step1's ticks
        monkeypatch.setattr(w.config, "visible_channels", lambda: seen.append(state.scope()) or [])
        w.tissue_render_snapshot()
        assert seen and set(seen) == {"step1"} and state.scope() == "regions"
    finally:
        _close(rig)


def test_regions_is_seeded_again_for_another_dataset(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state = w._display.state
        _enter_regions(rig)
        assert w._seed_regions_scope() is False                             # once per dataset
        chans = list(state.channel_order())
        with state.using_scope("step1"):                                    # Step1 moves on meanwhile
            for c in chans:
                state.set_display_visible(c, c == chans[-1])
            state.set_selected_channel(chans[-1])
        assert _ticks(state, "regions") != {chans[-1]}                      # Regions keeps its own
        w._regions_seeded_for = object()                                    # (as after a dataset switch)
        assert w._seed_regions_scope() is True
        assert _ticks(state, "regions") == {chans[-1]}                      # copied again from Step1
        with state.using_scope("regions"):
            assert state.selected_channel() == chans[-1]
    finally:
        _close(rig)


# ── §13.7: the user's acceptance feedback (2026-10-20) ──────────────────

def test_regions_intensity_button_is_step0s_in_step0s_place(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        page = w._regions
        assert page.btn_intensity.styleSheet() == w._step0._btn_intensity_window.styleSheet()
        head = page.channels_head
        assert head.itemAt(0).widget() is w._regions_cb_all                   # `Show all` first, as Step1's
        assert head.itemAt(1).widget() is page.btn_intensity
        assert w._regions_cb_all.text() == "Show all"
        assert w._regions_cb_all.styleSheet() == w._step0._cb_all.styleSheet()
        assert w._regions_cb_all.sizeHint().width() >= (
            w._step0._method_all.width() if w._step0._method_all.testAttribute(
                __import__("PyQt5.QtCore", fromlist=["Qt"]).Qt.WA_Resized)
            else w._step0._method_all.sizeHint().width())
        assert head.spacing() == w._step0._channels_host.itemAt(0).layout().spacing()
    finally:
        _close(rig)


def test_the_tissue_preview_draws_regions_own_ticks_live_and_publishes_nothing(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state = w._display.state
        coordinator = w._display.coordinator
        _enter_regions(rig)
        assert coordinator.active_context_id() == "regions"
        frames = []
        monkeypatch.setattr(coordinator, "request_frame", lambda **k: frames.append(k))
        other = next(c for c in state.channel_order() if c not in _ticks(state, "step1"))
        w._channel_dock.row(other).checkbox.setChecked(True)                  # a Regions tick
        _pump()
        assert any(f.get("kind") == "visibility" for f in frames)             # redrawn live
        published = []
        monkeypatch.setattr(w._display, "publish_render_spec", lambda spec: published.append(spec))
        seen = []
        real = w.config.visible_channels
        monkeypatch.setattr(w.config, "visible_channels", lambda: seen.append(state.scope()) or real())
        ctx = coordinator.context("regions")
        ctx.tissue_render_snapshot()
        assert seen and set(seen) == {"regions"}                              # Regions' ticks
        assert published == []                                                # Step2/3's spec untouched
        seen.clear()
        w.tissue_render_snapshot()                                            # Step1's context still
        assert set(seen) == {"step1"}
    finally:
        _close(rig)


def test_unticking_everything_in_regions_blanks_the_tissue_preview(app, monkeypatch, tmp_path):
    """codex §13.7 P2: an empty Regions selection is an empty picture (black),
    not the previous frame and not the DAPI overview."""
    import numpy as np
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state = w._display.state
        coordinator = w._display.coordinator
        _enter_regions(rig)
        for c in state.channel_order():
            state.set_display_visible(c, False)                              # Regions' own ticks only
        _pump()
        snap = coordinator.context("regions").tissue_render_snapshot()
        assert isinstance(snap, dict) and snap.get("blank") is True
        shown = []

        class _Panel:
            def adopt_dataset(self, token):
                pass

            def set_channel_image(self, rgb, token=None):
                shown.append(np.asarray(rgb))

        monkeypatch.setattr(coordinator, "_panels", lambda: [_Panel()])
        coordinator._pending_rev = coordinator._pending_rev or 1
        coordinator._in_flight = False
        coordinator._apply_pending_frame()
        assert shown and not shown[-1].any()                                 # black, now
        assert _ticks(state, "step1")                                        # Step1's ticks untouched
    finally:
        _close(rig)


def test_show_all_in_regions_sweeps_regions_ticks_only(app, monkeypatch, tmp_path):
    """§13.9: Regions' `Show all` ticks / unticks every bulk channel in the
    Regions scope; Step1's ticks and fusion are untouched; the Tissue preview
    is asked to redraw as for single ticks."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        state, fusion = w._display.state, w._display.fusion
        step1_before = _ticks(state, "step1")
        draft_before = fusion.draft_snapshot()
        _enter_regions(rig)
        frames = []
        monkeypatch.setattr(w._display.coordinator, "request_frame", lambda **k: frames.append(k))
        bulk = {c for c in w._channel_dock._order                          # the dock's own rows
                if getattr(state.capabilities(c), "bulk_toggleable", True)}
        assert bulk
        w._regions_cb_all.setChecked(True)
        _pump()
        assert bulk <= _ticks(state, "regions")
        w._regions_cb_all.setChecked(False)
        _pump()
        assert not (bulk & _ticks(state, "regions"))
        assert _ticks(state, "step1") == step1_before and fusion.draft_snapshot() == draft_before
        assert any(f.get("kind") == "visibility" for f in frames)
    finally:
        _close(rig)
