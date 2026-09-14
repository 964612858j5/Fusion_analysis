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
