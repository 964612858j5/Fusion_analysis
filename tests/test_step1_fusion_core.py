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


# ── the three production paths, on the same input ────────────────────────────

H = W = 32
_REMAP = {"DAPI": {"min": 0.0, "max": 1050.0, "gamma": 1.0},
          "CD3": {"min": 0.0, "max": 820.0, "gamma": 1.0},
          "CD8": {"min": 0.0, "max": 410.0, "gamma": 1.0}}


def _raw():
    rng = np.random.default_rng(7)
    return {"DAPI": (rng.random((H, W), np.float32) * 1000 + 50).astype(np.float32),
            "CD3": (rng.random((H, W), np.float32) * 800 + 20).astype(np.float32),
            "CD8": (rng.random((H, W), np.float32) * 400 + 10).astype(np.float32)}


class _FakeLoader:
    shape = (H, W)
    filepath = "/tmp/synthetic.ome.tif"

    def __init__(self, raw):
        self.raw = raw
        self.ch_map = {c: i for i, c in enumerate(raw)}

    def channel_names(self):
        return list(self.raw)

    @staticmethod
    def _norm(arr):
        return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
        return self.raw[ch][y0:y1, x0:x1].copy()


def _engine_u16(raw, groups, gws, nuc_w, remap=None):
    return FusionEngine().fuse_fullres(
        _FakeLoader(raw), 0, H, 0, W, groups, gws, "DAPI", nuc_w,
        channel_remap_params=_REMAP if remap is None else remap)


def _worker_u16(raw, groups, gws, nuc_w, tiles=1, remap=None):
    from block01.ui.step0.overview_panel import FullFusionWorker

    wk = FullFusionWorker.__new__(FullFusionWorker)
    wk._remap_params = _REMAP if remap is None else remap
    wk._unmapped_reported = set()
    out = np.zeros((H, W, 2), np.uint16)
    step = H // tiles
    for t in range(tiles):
        y0, y1 = t * step, (t + 1) * step if t < tiles - 1 else H
        out[y0:y1] = wk._fuse_tile({c: a[y0:y1].copy() for c, a in raw.items()},
                                   groups, gws, "DAPI", nuc_w)
    return out


def _preview_rgb(app, raw, groups, gws, nuc_w):
    from block01.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.loader = _FakeLoader(raw)
        chans = list(raw)
        w.config.set_channels(chans)
        w.config.load_panel({g: list(c) for g, c in groups.items()}, "DAPI")
        w.config.set_nucleus("DAPI", nuc_w)
        for g, chw in groups.items():
            w.config.set_group_weight(g, gws.get(g, 1.0))
            for ch, val in chw.items():
                w.config.set_channel_weight(ch, val)
                w.config._edited_channels.add(ch)
        for ch in chans:
            w.config.set_channel_visible(ch, True)
        w._all_patches = [(0, H, 0, W)]
        w._preview_patch_idx = 0
        w._patch_channel_cache[0] = {c: a.copy() for c, a in raw.items()}
        w._patch_load_ready.add(0)
        w._display_mapping = lambda: _REMAP
        w.set_preview_mode("fusion", force=True, reconcile=False)
        w._render_current_patch(reset_view=True)
        return w.prev_img.image.copy()
    finally:
        w.close()


def _as255(u16):
    return np.stack([np.round(u16[:, :, 0] / 65535 * 255),
                     np.zeros(u16.shape[:2]),
                     np.round(u16[:, :, 1] / 65535 * 255)], -1)


CASES = [
    ("one marker", {"g": {"CD3": 0.5}}, {"g": 1.0}, 1.0),
    ("two markers, one group", {"g": {"CD3": 0.5, "CD8": 0.5}}, {"g": 1.0}, 1.0),
    ("two groups", {"a": {"CD3": 0.6}, "b": {"CD8": 0.6}}, {"a": 1.0, "b": 1.0}, 1.0),
    ("two groups, uneven", {"a": {"CD3": 0.6}, "b": {"CD8": 0.6}},
     {"a": 0.25, "b": 1.0}, 1.0),
    ("one group at 0.1", {"g": {"CD3": 0.5}}, {"g": 0.1}, 1.0),
    ("saturating", {"g": {"CD3": 1.0, "CD8": 1.0}}, {"g": 1.0}, 1.0),
    ("no nucleus", {"g": {"CD3": 0.5}}, {"g": 1.0}, 0.0),
    ("quarter nucleus", {"g": {"CD3": 0.5}}, {"g": 1.0}, 0.25),
]


@pytest.mark.parametrize("name,groups,gws,nuc_w", CASES)
def test_the_engine_and_the_disk_write_the_same_pixels(name, groups, gws, nuc_w):
    raw = _raw()
    assert np.array_equal(_engine_u16(raw, groups, gws, nuc_w),
                          _worker_u16(raw, groups, gws, nuc_w))


@pytest.mark.parametrize("name,groups,gws,nuc_w", CASES)
def test_the_screen_shows_what_is_written(app, name, groups, gws, nuc_w):
    """Equal but for the screen's 8-bit truncation: at most one level."""
    raw = _raw()
    screen = _preview_rgb(app, raw, groups, gws, nuc_w).astype(np.float64)
    disk = _as255(_worker_u16(raw, groups, gws, nuc_w))
    assert np.abs(screen - disk).max() <= 1.0


@pytest.mark.parametrize("tiles", [2, 4, 8])
def test_the_tile_grid_does_not_change_the_result(tiles):
    raw = _raw()
    groups, gws = {"g": {"CD3": 0.5, "CD8": 0.3}}, {"g": 1.0}
    one = _worker_u16(raw, groups, gws, 1.0, tiles=1)
    many = _worker_u16(raw, groups, gws, 1.0, tiles=tiles)
    assert np.array_equal(one, many)


def test_the_group_weight_now_reaches_the_disk():
    raw = _raw()
    full = _worker_u16(raw, {"g": {"CD3": 0.5}}, {"g": 1.0}, 1.0)
    tenth = _worker_u16(raw, {"g": {"CD3": 0.5}}, {"g": 0.1}, 1.0)
    assert tenth[:, :, 0].mean() < full[:, :, 0].mean() / 5


def test_the_nucleus_weight_now_reaches_the_disk():
    raw = _raw()
    full = _worker_u16(raw, {"g": {"CD3": 0.5}}, {"g": 1.0}, 1.0)
    quarter = _worker_u16(raw, {"g": {"CD3": 0.5}}, {"g": 1.0}, 0.25)
    assert np.allclose(quarter[:, :, 1] / 65535,
                       full[:, :, 1] / 65535 * 0.25, atol=2e-4)


def test_the_channel_weight_scale_now_reaches_the_disk():
    raw = _raw()
    small = _worker_u16(raw, {"g": {"CD3": 0.2, "CD8": 0.2}}, {"g": 1.0}, 1.0)
    large = _worker_u16(raw, {"g": {"CD3": 0.8, "CD8": 0.8}}, {"g": 1.0}, 1.0)
    assert small[:, :, 0].mean() < large[:, :, 0].mean()


def test_a_channel_without_a_committed_window_takes_no_part():
    raw = _raw()
    partial = {k: v for k, v in _REMAP.items() if k != "CD8"}
    groups, gws = {"g": {"CD3": 0.5, "CD8": 0.5}}, {"g": 1.0}

    with_cd8 = _worker_u16(raw, groups, gws, 1.0, remap=_REMAP)
    without = _worker_u16(raw, groups, gws, 1.0, remap=partial)
    only_cd3 = _worker_u16(raw, {"g": {"CD3": 0.5}}, gws, 1.0, remap=partial)

    assert not np.array_equal(with_cd8, without)
    assert np.array_equal(without, only_cd3)
    # The engine follows the same rule.
    assert np.array_equal(_engine_u16(raw, groups, gws, 1.0, remap=partial),
                          only_cd3)


def test_the_formula_version_says_the_paths_agree():
    assert FUSION_FORMULA_VERSION == 2
