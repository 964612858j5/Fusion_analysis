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
    w.config.all_channels = chans
    w.config.nuc_combo.clear()
    w.config.nuc_combo.addItems(chans)
    w.config.load_panel({"markers": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
    w.config.nuc_row.spin.setValue(1.0)
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
        w.config.nuc_row.spin.setValue(0.8)
        w.config._panels["markers"].gw_row.spin.setValue(0.4)
        w.config._panels["markers"]._rows["CD3"].spin.setValue(0.7)
        before = w.config.get_full_config()

        # These are exactly the fields the session carries.
        assert before["nucleus"] == {"channel": "DAPI", "weight": 0.8}
        assert before["groups"]["markers"]["group_weight"] == 0.4
        assert before["groups"]["markers"]["channels"]["CD3"] == 0.7

        payload = json.loads(json.dumps(before))
        w.config.nuc_row.spin.setValue(0.0)
        w.config._panels["markers"]._rows["CD3"].spin.setValue(0.0)
        w._apply_step1_fusion_config(payload)

        assert w.config.get_full_config() == before
    finally:
        w.close()


def test_the_fusion_preview_arithmetic_is_unchanged(app):
    """Pins group_weight x channel_weight, group max, and the R/B colour map."""
    w = _window(app)
    try:
        w.config._panels["markers"].gw_row.spin.setValue(0.5)
        w.config._panels["markers"]._rows["CD3"].spin.setValue(0.4)
        w.config._panels["markers"]._rows["CD8"].spin.setValue(0.0)
        w.config.nuc_row.spin.setValue(1.0)

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
        w.config._panels["markers"]._rows["CD3"].spin.setValue(0.9)
        w._render_current_patch(reset_view=False)
        assert not np.array_equal(w.prev_img.image, before)
    finally:
        w.close()
