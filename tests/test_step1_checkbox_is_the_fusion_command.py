"""Step1's tick box: one gesture, one decision.

Ticking a marker in Step1 means "use this channel": show it, put it into the
fusion, and -- if nobody has ever weighted it -- weigh it 1.0. That is the
product's own contract and it predates the global dock.

For one release the dock split it in two: the tick wrote display visibility
and a separate `f` box wrote participation. `effective_config()` filters on
participation, so a user who ticked CD3 got a visible CD3 that the fusion
ignored, and a whole slide fused to DAPI alone.

What is pinned here is the restored contract, through the REAL row widgets:
the tick, the weight, the participation, the effective config and the pixels.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from block01.core.fusion_domain import (  # noqa: E402
    ABSENT, AUTHORITATIVE, AUTO, EXPLICIT)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    filepath = "/tmp/step1_cmd.ome.tiff"
    shape = (256, 256)

    def __init__(self, names=("DAPI", "CD3", "CD8", "CD20")):
        self._names = list(names)
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        i = self.ch_map.get(channel, 0)
        h, w = (y1 - y0) or 1, (x1 - x0) or 1
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        return ((yy * (i + 1) + xx) / float(h + w) * 200.0 + 10.0 * (i + 1)
                ).astype(np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    loader = _Loader()
    w.loader = loader
    # Step0's list first: rebuilding it is part of a dataset arriving, and
    # the handoff below is what puts the groups into the model.
    w._step0.loader = loader
    w._step0.nucleus_channel = "DAPI"
    w._step0._rebuild_channel_list()
    w.config.set_channels(loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0, "CD20": 0.0}},
                        "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    w._set_step_active(1)
    return w


def _close(w):
    w._display.shutdown("test")
    w.deleteLater()


def _pump(ms=200):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)


def _tick(w, channel, on=True):
    """A REAL click on the row's tick box."""
    box = w._channel_dock.row(channel).checkbox
    if box.isChecked() != on:
        QtTest.QTest.mouseClick(box, QtCore.Qt.LeftButton,
                                pos=box.rect().center())
    _pump()
    return box


def _fused(w):
    """The channels the fusion would actually use."""
    cfg = w._display.fusion.effective_config()
    used = set()
    for data in (cfg.get("groups") or {}).values():
        used.update((data.get("channels") or {}).keys())
    nucleus = (cfg.get("nucleus") or {})
    if nucleus.get("channel") and nucleus.get("weight"):
        used.add(nucleus["channel"])
    return used


# ── 1. the tick is the whole gesture ────────────────────────────────────────

def test_ticking_a_marker_shows_it_fuses_it_and_weighs_it_one(app):
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        assert model.fusion_enabled("CD3") is False
        assert model.weight_provenance("CD3") == ABSENT

        _tick(w, "CD3", True)

        assert state.display_visible("CD3") is True
        assert model.fusion_enabled("CD3") is True
        assert model.channel_weight("CD3") == pytest.approx(1.0)
        assert model.weight_provenance("CD3") == AUTO
        assert w._channel_dock.row("CD3").spin.value() == pytest.approx(1.0)
        assert "CD3" in _fused(w)
    finally:
        _close(w)


def test_unticking_keeps_every_number_and_reticking_restores_it(app):
    w = _window(app)
    try:
        model = w._display.fusion
        _tick(w, "CD3", True)
        w._channel_dock.row("CD3").spin.setValue(0.4)
        _pump()
        assert model.weight_provenance("CD3") == EXPLICIT

        _tick(w, "CD3", False)
        assert w._display.state.display_visible("CD3") is False
        assert model.fusion_enabled("CD3") is False
        assert model.channel_weight("CD3") == pytest.approx(0.4), \
            "unticking threw the weight away"
        assert "CD3" not in _fused(w)

        _tick(w, "CD3", True)
        assert model.channel_weight("CD3") == pytest.approx(0.4), \
            "reticking forced 1.0 over an answer the user gave"
        assert model.weight_provenance("CD3") == EXPLICIT
    finally:
        _close(w)


def test_an_explicit_zero_and_a_restored_project_are_never_overwritten(app):
    w = _window(app)
    try:
        model = w._display.fusion
        model.install_draft({
            "groups": {"A": {"group_weight": 1.0,
                             "channels": {"CD3": 0.2, "CD8": 0.0}},
                       "B": {"group_weight": 0.5, "channels": {"CD3": 0.7}}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": ["DAPI"],
            "provenance": {"CD3": AUTHORITATIVE, "CD8": EXPLICIT,
                           "DAPI": AUTHORITATIVE},
        })
        w.config._rebuild_rows()
        _pump()

        _tick(w, "CD3", True)          # heterogeneous 0.2 / 0.7
        _tick(w, "CD8", True)          # an explicit 0.0

        assert model.groups()["A"]["CD3"] == pytest.approx(0.2)
        assert model.groups()["B"]["CD3"] == pytest.approx(0.7)
        assert model.groups()["A"]["CD8"] == 0.0
        assert model.weight_provenance("CD8") == EXPLICIT
        assert model.fusion_enabled("CD3") and model.fusion_enabled("CD8")
    finally:
        _close(w)


def test_a_programmatic_selection_or_restore_is_not_a_first_enable(app):
    w = _window(app)
    try:
        model, state = w._display.fusion, w._display.state
        state.set_selected_channel("CD20", origin="restore")
        w._channel_dock.refresh()
        w._set_step_active(2)
        w._set_step_active(1)
        _pump()

        assert model.fusion_enabled("CD20") is False
        assert model.weight_provenance("CD20") == ABSENT

        model.install_draft({
            "groups": {"A": {"group_weight": 1.0, "channels": {"CD20": 0.0}}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": ["CD20", "DAPI"],
            "provenance": {"CD20": EXPLICIT, "DAPI": AUTHORITATIVE},
        })
        _pump()
        assert model.channel_weight("CD20") == 0.0, \
            "a restore was mistaken for a first enable"
        assert model.weight_provenance("CD20") == EXPLICIT
    finally:
        _close(w)


# ── 2. the fusion actually contains the markers ─────────────────────────────

def test_two_markers_and_the_nucleus_all_reach_the_effective_config(app):
    w = _window(app)
    try:
        model = w._display.fusion
        _tick(w, "CD3", True)
        _tick(w, "CD8", True)

        used = _fused(w)
        assert {"CD3", "CD8"} <= used, used
        assert "DAPI" in used or model.fusion_enabled("DAPI")

        _tick(w, "CD3", False)
        used = _fused(w)
        assert "CD3" not in used
        assert "CD8" in used, "unticking one marker dropped the other"
    finally:
        _close(w)


def test_the_tick_is_one_command_with_no_half_state_in_between(app):
    """No observer may see "shown but not participating" or "participating
    at 0": the model raises its draft revision once and emits in order."""
    w = _window(app)
    try:
        model, state = w._display.fusion, w._display.state
        seen = []

        def record(*_a):
            seen.append((state.display_visible("CD3"),
                         model.fusion_enabled("CD3"),
                         model.channel_weight("CD3")))

        model.weight_changed.connect(record)
        model.participation_changed.connect(record)
        model.draft_changed.connect(record)
        state.visibility_changed.connect(record)

        _tick(w, "CD3", True)

        assert seen, "nothing was announced"
        # NOBODY EVER SEES A SHOWN CHANNEL THAT IS NOT IN THE FUSION. The
        # forbidden intermediate states are "shown but not participating"
        # and "shown but still weighing 0": the display answer is published
        # last, after the science is settled.
        for visible, enabled, weight in seen:
            if visible:
                assert enabled, seen
                assert weight == pytest.approx(1.0), seen
        # by the time the draft is announced, everything is true at once
        assert seen[-1] == (True, True, pytest.approx(1.0)), seen
    finally:
        _close(w)


def test_a_weight_drag_changes_the_published_picture(app):
    """The row's own control still drives the pixels."""
    w = _window(app)
    try:
        model = w._display.fusion
        _tick(w, "CD3", True)
        _pump()
        spec_before = dict(w._display.render_spec() or {})
        weights_before = dict(spec_before.get("weights") or {})

        w._channel_dock.row("CD3").spin.setValue(0.4)
        _pump(300)

        assert model.channel_weight("CD3") == pytest.approx(0.4)
        spec_after = dict(w._display.render_spec() or {})
        weights_after = dict(spec_after.get("weights") or {})
        if weights_before or weights_after:
            assert weights_after.get("CD3") == pytest.approx(0.4), \
                (weights_before, weights_after)
        assert w._effective_fusion_config()["groups"]["markers"]["channels"][
            "CD3"] == pytest.approx(0.4)
    finally:
        _close(w)


def test_the_overlay_still_behaves_when_a_channel_is_ticked(app):
    w = _window(app)
    try:
        state = w._display.state
        _tick(w, "CD3", True)
        assert state.display_visible("CD3") is True
        # the other channels are untouched by one tick
        assert state.display_visible("CD8") is False
        assert w._display.fusion.fusion_enabled("CD8") is False
    finally:
        _close(w)


# ── every real Step1 gesture resolves to the same command ───────────────────

def _click_name(w, channel):
    """A REAL click on a row's channel name."""
    row = w._channel_dock.row(channel)
    label = row.name_label
    QtTest.QTest.mouseClick(label, QtCore.Qt.LeftButton,
                            pos=label.rect().center())
    _pump()


def test_clicking_a_hidden_channels_name_uses_it_like_the_tick(app):
    """The second real entry, and it used to mean something else.

    Clicking the name of a hidden channel shows it -- selecting something you
    cannot see is a dead end -- and in Step1 showing a channel IS the
    scientific act. Writing visibility alone left a marker on screen that the
    fusion ignored: the same "only DAPI is fused" report, reached by the
    other gesture.
    """
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        assert state.display_visible("CD8") is False
        assert model.fusion_enabled("CD8") is False

        _click_name(w, "CD8")

        assert state.display_visible("CD8") is True
        assert model.fusion_enabled("CD8") is True
        assert model.channel_weight("CD8") == pytest.approx(1.0)
        assert model.weight_provenance("CD8") == AUTO
        assert "CD8" in _fused(w)
        assert state.selected_channel() == "CD8"
    finally:
        _close(w)


def test_clicking_a_visible_channels_name_only_selects_it(app):
    """A click on a channel that is already on changes nothing but the
    selection -- it is not a second enable and not a re-weighting."""
    w = _window(app)
    try:
        model = w._display.fusion
        _tick(w, "CD3", True)
        w._channel_dock.row("CD3").spin.setValue(0.25)
        _pump()
        rev = model.draft_revision()

        _click_name(w, "CD3")

        assert model.channel_weight("CD3") == pytest.approx(0.25)
        assert model.draft_revision() == rev
        assert w._display.state.selected_channel() == "CD3"
    finally:
        _close(w)


def test_the_panels_auto_show_path_is_the_same_command(app):
    """`ConfigPanel.set_current_channel(auto_show=True)` is the click's own
    route into the panel; `auto_show=False` is a restore and stays silent."""
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion

        w.config.set_current_channel("CD20", auto_show=True)
        _pump()
        assert state.display_visible("CD20") is True
        assert model.fusion_enabled("CD20") is True
        assert model.channel_weight("CD20") == pytest.approx(1.0)

        # ...and a restore is not a click
        state.set_display_visible("CD8", False, origin="test")
        w.config.set_current_channel("CD8", auto_show=False)
        _pump()
        assert state.display_visible("CD8") is False
        assert model.fusion_enabled("CD8") is False
        assert model.weight_provenance("CD8") == ABSENT
    finally:
        _close(w)


def test_outside_step1_the_same_gestures_are_display_only(app):
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        for step in (0, 2, 3):
            w._set_step_active(step)
            state.set_display_visible("CD20", False, origin="test")
            rev = model.draft_revision()

            _click_name(w, "CD20")
            _tick(w, "CD20", True)

            assert state.display_visible("CD20") is True, step
            assert model.fusion_enabled("CD20") is False, step
            assert model.draft_revision() == rev, step
    finally:
        _close(w)


def test_no_notice_of_either_owner_shows_a_half_finished_command(app):
    """Both directions, not one.

    An observer woken by EITHER owner must find the other one already final:
    never "shown but not fused", and never "fused but still hidden".
    """
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        seen = []

        def record(*_a):
            seen.append((state.display_visible("CD3"),
                         model.fusion_enabled("CD3"),
                         model.channel_weight("CD3")))

        model.weight_changed.connect(record)
        model.participation_changed.connect(record)
        model.draft_changed.connect(record)
        state.visibility_changed.connect(record)

        _tick(w, "CD3", True)

        assert seen, "nothing was announced"
        for visible, enabled, weight in seen:
            assert visible is True, ("fused but still hidden", seen)
            assert enabled is True, ("shown but not fused", seen)
            assert weight == pytest.approx(1.0), seen
    finally:
        _close(w)


def test_the_nucleus_is_not_dragged_into_the_union_by_a_restore(app):
    """The S4 reconciliation is about MARKERS.

    DAPI is the reference layer: its switch never meant "use this channel",
    so a project that recorded it out of the fusion must not come back with
    it in, and one that recorded it hidden must not come back showing it.
    """
    from block01.core import fusion_domain

    sess = {
        "version": fusion_domain.SESSION_SCHEMA_VERSION,
        "fusion_config": {},
        "fusion_draft": {
            "groups": {"markers": {"group_weight": 1.0,
                                   "channels": {"CD3": 0.5}}},
            "group_weights": {"markers": {"CD3": 0.5}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": ["CD3"],                 # DAPI deliberately OUT
            "provenance": {"CD3": AUTHORITATIVE},
            "channel_weight": {"CD3": 0.5},
        },
        "display_visibility": {"DAPI": True, "CD3": False, "CD8": True},
    }
    spec, visibility = fusion_domain.migrate_session(
        sess, channels=["DAPI", "CD3", "CD8"])

    # the markers are reconciled...
    assert set(spec["enabled"]) == {"CD3", "CD8"}
    assert visibility["CD3"] is True and visibility["CD8"] is True
    assert spec["channel_weight"]["CD3"] == pytest.approx(0.5)
    assert spec["channel_weight"]["CD8"] == pytest.approx(1.0)
    # ...and the nucleus is left exactly as the session recorded it
    assert "DAPI" not in spec["enabled"]
    assert visibility["DAPI"] is True


def test_a_nucleus_recorded_in_the_fusion_stays_in_it(app):
    from block01.core import fusion_domain

    sess = {
        "version": fusion_domain.SESSION_SCHEMA_VERSION,
        "fusion_config": {},
        "fusion_draft": {
            "groups": {"markers": {"group_weight": 1.0,
                                   "channels": {"CD3": 0.5}}},
            "group_weights": {"markers": {"CD3": 0.5}},
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "enabled": ["CD3", "DAPI"],
            "provenance": {"CD3": AUTHORITATIVE, "DAPI": AUTHORITATIVE},
            "channel_weight": {"CD3": 0.5, "DAPI": 1.0},
        },
        "display_visibility": {"DAPI": False, "CD3": True},
    }
    spec, visibility = fusion_domain.migrate_session(
        sess, channels=["DAPI", "CD3"])

    assert "DAPI" in spec["enabled"]
    assert visibility["DAPI"] is False, \
        "the nucleus layer switch is not a participation answer"


# ── the remaining ways in ───────────────────────────────────────────────────

def test_a_sweep_uses_the_channels_it_sweeps_in(app):
    """Show all / Hide all is the same decision, made for many channels.

    The method wrote display visibility directly, so a sweep in Step1 left
    every marker it turned on visible and outside the fusion -- the same
    "only DAPI is fused" state, reached by a third gesture. (The button is
    hidden today; the method and its connection are not.)
    """
    w = _window(app)
    try:
        state, model, dock = w._display.state, w._display.fusion, w._channel_dock
        markers = [c for c in dock.channel_order() if c != "DAPI"]
        for ch in markers:
            assert model.fusion_enabled(ch) is False

        dock.set_all_visible(True)
        _pump()

        used = _fused(w)
        for ch in markers:
            assert state.display_visible(ch) is True, ch
            assert model.fusion_enabled(ch) is True, ch
            assert model.channel_weight(ch) == pytest.approx(1.0), ch
            assert ch in used, (ch, used)

        dock.set_all_visible(False)
        _pump()
        for ch in markers:
            assert state.display_visible(ch) is False, ch
            assert model.fusion_enabled(ch) is False, ch
            # ...and the numbers are still there
            assert model.channel_weight(ch) == pytest.approx(1.0), ch
    finally:
        _close(w)


def test_a_sweep_outside_step1_is_display_only(app):
    w = _window(app)
    try:
        state, model, dock = w._display.state, w._display.fusion, w._channel_dock
        w._set_step_active(2)
        rev = model.draft_revision()

        dock.set_all_visible(True)
        _pump()

        assert state.display_visible("CD3") is True
        assert model.fusion_enabled("CD3") is False
        assert model.draft_revision() == rev
    finally:
        _close(w)


def test_a_click_completes_a_channel_that_is_shown_but_not_fused(app):
    """The state a project saved by the two-control release comes back in.

    Judging the gesture by visibility alone left it unfixable: the channel
    was already visible, so the click did nothing, and the fusion went on
    without it. The question is whether the channel is IN USE.
    """
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        # shown, but outside the science -- exactly the half state
        state.set_display_visible("CD8", True, origin="restore")
        assert model.fusion_enabled("CD8") is False

        _click_name(w, "CD8")

        assert model.fusion_enabled("CD8") is True
        assert model.channel_weight("CD8") == pytest.approx(1.0)
        assert state.display_visible("CD8") is True
        assert "CD8" in _fused(w)
    finally:
        _close(w)


def test_the_panel_completes_a_shown_but_unfused_channel_too(app):
    w = _window(app)
    try:
        state, model = w._display.state, w._display.fusion
        state.set_display_visible("CD20", True, origin="restore")
        assert model.fusion_enabled("CD20") is False

        w.config.set_current_channel("CD20", auto_show=True)
        _pump()

        assert model.fusion_enabled("CD20") is True
        assert model.channel_weight("CD20") == pytest.approx(1.0)
        # ...and a restore still completes nothing
        state.set_display_visible("CD8", True, origin="restore")
        w.config.set_current_channel("CD8", auto_show=False)
        _pump()
        assert model.fusion_enabled("CD8") is False
    finally:
        _close(w)


def test_a_command_that_fails_half_way_still_announces_what_it_wrote(app):
    """No silent half-transaction.

    If the second owner raises, this model has already been changed. Holding
    the notices back would leave the values moved and every view drawing the
    old answer with no way to learn otherwise, so what was written is
    published and the failure propagates.
    """
    w = _window(app)
    try:
        model = w._display.fusion
        seen = []
        model.participation_changed.connect(lambda c, e: seen.append((c, e)))
        rev = model.draft_revision()

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with model.deferred_notices():
                model.set_fusion_enabled("CD3", True, origin="test")
                raise _Boom("the display owner refused")

        # the model really did change...
        assert model.fusion_enabled("CD3") is True
        # ...and it said so, exactly once, with the revision moved
        assert seen == [("CD3", True)], seen
        assert model.draft_revision() > rev
    finally:
        _close(w)
