"""One fusion implementation, called by every path.

The preview used to write the arithmetic out a second time, which is how the
screen and the saved file drifted apart. `fuse_channels` is now the only place
the maths lives; this module pins both the maths itself and the fact that each
caller reaches it.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core.fusion_engine import (  # noqa: E402
    FusionEngine, fuse_channels, FUSION_FORMULA_VERSION,
)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _signals(shape=(16, 16)):
    rng = np.random.default_rng(11)
    return {c: rng.random(shape, dtype=np.float32) for c in ("DAPI", "CD3", "CD8")}


def test_the_core_sums_inside_a_group_and_takes_the_max_across_them():
    s = _signals()
    cyto, nuc = fuse_channels(
        s, {"a": {"CD3": 0.5, "CD8": 0.25}, "b": {"CD8": 1.0}},
        {"a": 1.0, "b": 1.0}, "DAPI", 1.0)

    a = np.clip(s["CD3"] * 0.5 + s["CD8"] * 0.25, 0, 1)
    b = np.clip(s["CD8"] * 1.0, 0, 1)
    assert np.array_equal(cyto, np.maximum(a, b))
    assert np.array_equal(nuc, np.clip(s["DAPI"], 0, 1))


def test_the_group_weight_multiplies_before_the_clip():
    s = _signals()
    half = fuse_channels(s, {"a": {"CD3": 1.0}}, {"a": 0.5}, "DAPI", 1.0)[0]
    full = fuse_channels(s, {"a": {"CD3": 1.0}}, {"a": 1.0}, "DAPI", 1.0)[0]
    assert np.allclose(half, np.clip(full * 0.5, 0, 1), atol=1e-6)


def test_the_group_weight_applies_before_saturation_not_after():
    """Order matters exactly where the sum would clip: two full-weight channels."""
    s = _signals()
    s["CD3"] = np.full((16, 16), 0.9, np.float32)
    s["CD8"] = np.full((16, 16), 0.8, np.float32)
    groups = {"a": {"CD3": 1.0, "CD8": 1.0}}

    cyto = fuse_channels(s, groups, {"a": 0.5}, "DAPI", 1.0)[0]

    scale_then_clip = np.clip(0.5 * (0.9 + 0.8), 0, 1)      # 0.85
    clip_then_scale = np.clip(0.9 + 0.8, 0, 1) * 0.5        # 0.50
    assert np.allclose(cyto, scale_then_clip, atol=1e-6)
    assert not np.allclose(cyto, clip_then_scale, atol=1e-6)


def test_the_nucleus_weight_scales_the_nucleus():
    s = _signals()
    for w in (0.0, 0.25, 1.0):
        nuc = fuse_channels(s, {}, {}, "DAPI", w)[1]
        assert np.allclose(nuc, np.clip(s["DAPI"] * w, 0, 1), atol=1e-6)


def test_a_channel_that_is_absent_is_skipped_not_zeroed():
    s = _signals()
    del s["CD8"]
    cyto = fuse_channels(s, {"a": {"CD3": 0.5, "CD8": 0.5}}, {"a": 1.0},
                         "DAPI", 1.0)[0]
    assert np.array_equal(cyto, np.clip(s["CD3"] * 0.5, 0, 1))


def test_weights_are_clipped_so_scale_alone_cannot_win():
    s = _signals()
    a = fuse_channels(s, {"a": {"CD3": 1.0}}, {"a": 1.0}, "DAPI", 1.0)[0]
    b = fuse_channels(s, {"a": {"CD3": 5.0}}, {"a": 5.0}, "DAPI", 1.0)[0]
    assert np.array_equal(a, b)


def test_nothing_to_fuse_answers_nothing():
    assert fuse_channels({}, {"a": {"CD3": 1.0}}, {"a": 1.0}, "DAPI", 1.0) == (None, None)


def test_the_engine_is_a_thin_wrapper_over_the_core():
    s = _signals()
    groups, gws = {"a": {"CD3": 0.5, "CD8": 0.2}}, {"a": 0.6}
    engine = FusionEngine().compute(s, groups, gws, "DAPI", 0.8, prenormalized=True)
    core = fuse_channels(s, groups, gws, "DAPI", 0.8)
    assert all(np.array_equal(x, y) for x, y in zip(engine, core))


def test_the_preview_draws_what_the_core_computes(app):
    """Same signals in, same picture out — through the real render method."""
    from block01.ui.main_window import MainWindow

    class _Loader:
        shape = (32, 32)
        filepath = "/tmp/x.ome.tif"

        def __init__(self):
            self._n = ["DAPI", "CD3", "CD8"]
            self.ch_map = {c: i for i, c in enumerate(self._n)}

        def channel_names(self):
            return list(self._n)

        @staticmethod
        def _norm(arr):
            return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

        def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
            return np.zeros((y1 - y0, x1 - x0), np.float32)

    w = MainWindow()
    try:
        w.loader = _Loader()
        chans = w.loader.channel_names()
        w.config.set_channels(chans)
        w.config.load_panel({"g": {"CD3": 0.0, "CD8": 0.0}}, "DAPI")
        w.config.set_nucleus("DAPI", 0.8)
        w.config.set_group_weight("g", 0.6)
        for ch, val in (("CD3", 0.5), ("CD8", 0.2)):
            w.config.set_channel_weight(ch, val)
            w.config._edited_channels.add(ch)
        for ch in chans:
            w.config.set_channel_visible(ch, True)

        s = _signals((32, 32))
        w._all_patches = [(0, 32, 0, 32)]
        w._preview_patch_idx = 0
        w._patch_channel_cache[0] = {c: v.copy() for c, v in s.items()}
        w._patch_load_ready.add(0)
        w._load_step0_remap_params = lambda: ({}, "")
        w.set_preview_mode("fusion", force=True, reconcile=False)
        w._render_current_patch(reset_view=True)

        cyto, nuc = fuse_channels(s, w.config.get_groups(),
                                  w.config.get_group_weights(), "DAPI", 0.8)
        expected = FusionEngine.to_rgb(cyto, nuc)
        assert np.array_equal(w.prev_img.image, expected)
    finally:
        w.close()


def test_the_formula_version_is_a_number_the_code_can_read():
    assert isinstance(FUSION_FORMULA_VERSION, int)
    assert FUSION_FORMULA_VERSION >= 1
