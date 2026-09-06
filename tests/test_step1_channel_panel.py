"""ConfigPanel is the one channel state owner in Step1.

The right-hand "Channels" tab mirrored only the per-channel weights of grouped
channels: it could not show the nucleus weight or any group weight, so its
"1.00" was never the effective weight, and its checkbox and colour swatch wrote
state that nothing in Step1 read. It is gone, together with the three verbatim
ConfigPanel/GroupPanel/ChannelWeightRow copies that sat unused in
overview_panel.py.

This commit changes no rendering: the fusion preview arithmetic is pinned here
so a later refactor cannot move it silently.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Loader:
    filepath = "/tmp/dataset.ome.tiff"
    shape = (256, 256)

    def __init__(self):
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    @staticmethod
    def _norm(arr):
        arr = arr.astype(np.float32)
        nz = arr[arr > 0]
        if nz.size < 100:
            return np.zeros_like(arr)
        lo, hi = np.percentile(nz, [1.0, 99.5])
        if hi <= lo:
            return np.zeros_like(arr)
        return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1, normalize=True):
        ds = max(1, int(downsample))
        return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)


def _window(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.loader = _Loader()
    chans = w.loader.channel_names()
    w.config.set_channels(chans)
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    return w


def test_step1_holds_exactly_one_channel_panel(app):
    from block01.ui.step0.config_panel import ConfigPanel
    from block01.ui.widgets.channel_dock import ChannelDock

    w = _window(app)
    try:
        panels = w._step1_page_widget.findChildren(ConfigPanel)
        assert len(panels) == 1
        assert panels[0] is w.config
        # No second channel view, and no second channel state model.
        assert w._step1_page_widget.findChildren(ChannelDock) == []
        assert not hasattr(w, "_step1_dock_adapter")
    finally:
        w.close()


def test_the_right_column_no_longer_offers_a_channels_tab(app):
    w = _window(app)
    try:
        titles = [w.right_tabs.tabText(i) for i in range(w.right_tabs.count())]
        assert "Channels" not in titles
        assert titles == ["Method & Parameters", "Patch Results"]
        assert w.right_tabs.currentWidget() is w.method_params_tab
    finally:
        w.close()


def test_switching_to_the_patch_results_tab_still_works(app):
    w = _window(app)
    try:
        w._show_step1_patch_results_tab("test")
        assert w.right_tabs.currentWidget() is w.patch_results_tab
    finally:
        w.close()


def test_nucleus_and_group_weights_survive_a_session_round_trip(app, tmp_path):
    w = _window(app)
    try:
        w.config.set_group_weight("markers", 0.4)
        w.config._rows["CD3"].spin.setValue(0.7)
        before = w.config.get_full_config()

        assert before["nucleus"] == {"channel": "DAPI", "weight": 1.0}
        assert before["groups"]["markers"]["group_weight"] == 0.4
        assert before["groups"]["markers"]["channels"]["CD3"] == 0.7

        payload = json.loads(json.dumps(before))
        w.config._rows["CD3"].spin.setValue(0.0)
        w._apply_step1_fusion_config(payload)

        assert w.config.get_full_config() == before
    finally:
        w.close()


def test_the_fusion_preview_arithmetic_is_unchanged(app):
    """Pins group_weight x channel_weight, group max, and the R/B colour map."""
    w = _window(app)
    try:
        w.config.set_group_weight("markers", 0.5)
        w.config._rows["CD3"].spin.setValue(0.4)
        w.config._rows["CD8"].spin.setValue(0.0)
        w.config.set_nucleus_weight(1.0)

        rng = np.random.default_rng(0)
        dapi = rng.random((32, 32), dtype=np.float32) + 0.1
        cd3 = rng.random((32, 32), dtype=np.float32) + 0.1
        w._all_patches = [(0, 32, 0, 32)]
        w._preview_patch_idx = 0
        w._patch_channel_cache[0] = {"DAPI": dapi, "CD3": cd3}
        w._patch_load_ready.add(0)

        w._render_current_patch(reset_view=True)

        cyto = np.clip(_Loader._norm(cd3) * 0.4 * 0.5, 0.0, 1.0)
        nuc = np.clip(_Loader._norm(dapi) * 1.0, 0.0, 1.0)
        expected = np.stack([(np.clip(cyto, 0, 1) * 255).astype(np.uint8),
                             np.zeros((32, 32), np.uint8),
                             (np.clip(nuc, 0, 1) * 255).astype(np.uint8)],
                            axis=-1)
        assert np.array_equal(w.prev_img.image, expected)
    finally:
        w.close()


def test_the_duplicated_channel_classes_are_gone_from_overview_panel(app):
    from block01.ui.step0 import overview_panel

    for name in ("ConfigPanel", "GroupPanel", "ChannelWeightRow"):
        assert not hasattr(overview_panel, name), name
    # The live exports are untouched.
    for name in ("OverviewPanel", "TileSelectDialog", "FullFusionWorker"):
        assert hasattr(overview_panel, name), name


def test_the_channel_panel_lives_in_the_left_column(app):
    from block01.ui.step0.config_panel import ConfigPanel

    w = _window(app)
    try:
        # Still exactly one instance, now parented into the left column.
        panels = w._step1_page_widget.findChildren(ConfigPanel)
        assert len(panels) == 1
        assert panels[0] is w.config
        assert w._step1_left_panel.findChildren(ConfigPanel) == [w.config]
        assert w._step1_mid_split.findChildren(ConfigPanel) == []
        # The middle column keeps the preview and nothing else.
        assert w._step1_mid_split.count() == 1
        assert w._step1_left_panel.findChildren(type(w.prev_gv)) == []
    finally:
        w.close()


def test_moving_the_panel_kept_it_wired_to_the_preview(app):
    w = _window(app)
    try:
        rng = np.random.default_rng(1)
        w._all_patches = [(0, 32, 0, 32)]
        w._preview_patch_idx = 0
        w._patch_channel_cache[0] = {
            "DAPI": rng.random((32, 32), dtype=np.float32) + 0.1,
            "CD3": rng.random((32, 32), dtype=np.float32) + 0.1,
        }
        w._patch_load_ready.add(0)
        w._render_current_patch(reset_view=True)
        before = w.prev_img.image.copy()

        # A weight edit in the moved panel still reaches the preview.
        w.config._rows["CD3"].spin.setValue(0.9)
        w._render_current_patch(reset_view=False)
        assert not np.array_equal(w.prev_img.image, before)
    finally:
        w.close()


def test_an_old_multi_group_config_round_trips_value_for_value(app):
    """One row cannot show two numbers, but nothing may be rewritten by it."""
    w = _window(app)
    try:
        cfg = {
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {
                "a": {"group_weight": 0.5, "channels": {"CD3": 0.2, "CD8": 0.9}},
                "b": {"group_weight": 0.25, "channels": {"CD3": 0.7}},
            },
        }
        w._apply_step1_fusion_config(cfg)

        # Nothing was edited, so every (group, channel) keeps its own value.
        assert w.config.get_full_config() == cfg
        assert w.config.ambiguous_channels() == {"CD3": [0.2, 0.7]}
    finally:
        w.close()


def test_editing_a_shared_channel_applies_to_every_group_it_is_in(app):
    w = _window(app)
    try:
        cfg = {
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {
                "a": {"group_weight": 0.5, "channels": {"CD3": 0.2, "CD8": 0.9}},
                "b": {"group_weight": 0.25, "channels": {"CD3": 0.7}},
            },
        }
        w._apply_step1_fusion_config(cfg)
        w.config._rows["CD3"].spin.setValue(0.4)

        out = w.config.get_full_config()
        assert out["groups"]["a"]["channels"]["CD3"] == 0.4
        assert out["groups"]["b"]["channels"]["CD3"] == 0.4
        # An untouched channel is still exactly what it was loaded with.
        assert out["groups"]["a"]["channels"]["CD8"] == 0.9
        # Group weights are never touched by a row edit.
        assert out["groups"]["a"]["group_weight"] == 0.5
        assert out["groups"]["b"]["group_weight"] == 0.25
    finally:
        w.close()


def test_a_shared_channel_shows_one_value_and_says_which(app):
    w = _window(app)
    try:
        w._apply_step1_fusion_config({
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {
                "a": {"group_weight": 1.0, "channels": {"CD3": 0.2}},
                "b": {"group_weight": 1.0, "channels": {"CD3": 0.7}},
            },
        })
        row = w.config._rows["CD3"]
        assert row.weight() == 0.7                  # the largest of the two
        assert "*" in row.name_label.text()
        assert "several groups" in row.toolTip()
    finally:
        w.close()


def test_reset_weights_is_a_real_edit_not_a_repaint(app):
    """What the row shows and what gets fused and saved must be one number."""
    w = _window(app)
    try:
        w._apply_step1_fusion_config({
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {"a": {"group_weight": 1.0,
                             "channels": {"CD3": 0.7, "CD8": 0.4}}},
        })
        assert w.config.get_groups()["a"]["CD3"] == 0.7

        changes = []
        w.config.config_changed.connect(lambda: changes.append(1))
        w.config.zero_marker_weights()

        assert w.config._rows["CD3"].weight() == 0.0
        assert w.config.get_groups()["a"] == {"CD3": 0.0, "CD8": 0.0}
        assert w.config.get_full_config()["groups"]["a"]["channels"]["CD3"] == 0.0
        assert len(changes) == 1              # one signal, not one per row
    finally:
        w.close()


def test_the_nucleus_is_read_only_and_comes_from_the_handoff(app):
    w = _window(app)
    try:
        assert w.config.nucleus_channel() == "DAPI"
        assert w.config.get_nucleus() == ("DAPI", 1.0)

        row = w.config._rows["DAPI"]
        assert row.slider.isEnabled() is False
        assert row.spin.isReadOnly() is True
        # Every other row stays editable.
        assert w.config._rows["CD3"].slider.isEnabled() is True

        # Zeroing markers never touches the nucleus.
        w.config.zero_marker_weights()
        assert w.config.get_nucleus() == ("DAPI", 1.0)
    finally:
        w.close()


def test_a_session_cannot_replace_the_nucleus_step0_handed_over(app):
    """The nucleus is read-only in Step1, so nothing invisible may change it."""
    w = _window(app)
    try:
        assert w.config.get_nucleus() == ("DAPI", 1.0)

        w._apply_step1_fusion_config({
            "nucleus": {"channel": "CD3", "weight": 0.6},
            "groups": {"a": {"group_weight": 1.0, "channels": {"CD8": 0.2}}},
        })

        assert w.config.nucleus_channel() == "DAPI"
        assert w.config.get_nucleus() == ("DAPI", 1.0)
        assert w.config._rows["DAPI"].spin.isReadOnly() is True
        assert w.config._rows["CD3"].spin.isReadOnly() is False
        # The groups it did carry are restored as saved.
        assert w.config.get_groups() == {"a": {"CD8": 0.2}}
    finally:
        w.close()


def test_the_nucleus_never_stays_inside_a_marker_group(app):
    """Otherwise it contributes twice: once as blue, once as red."""
    w = _window(app)
    try:
        w._apply_step1_fusion_config({
            "nucleus": {"channel": "DAPI", "weight": 1.0},
            "groups": {"a": {"group_weight": 1.0,
                             "channels": {"DAPI": 0.9, "CD3": 0.3}}},
        })

        assert w.config.get_groups() == {"a": {"CD3": 0.3}}
        assert w.config.get_nucleus() == ("DAPI", 1.0)
    finally:
        w.close()
