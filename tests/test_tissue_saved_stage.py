"""Step1-3's Tissue Preview draws a corrected channel from its SAVED product.

WHAT THE USER REPORTED (CD45RO, TMA4, 2026-10-11): after Save, Step1's
viewer showed CD45RO corrected, but Step1's Tissue Preview still showed its
whole raw background -- the thumbnail was Step0's raw slide drawn through the
window calibrated on the corrected product. And Step0's two producers of
whole-slide arrays disagree on their grid (a pyramid level vs the loader's
overview stride), so a frame mixing them dropped layers.

Pinned here: the grid, the corrected overview against a level-0 block mean,
"no product" is no picture (never raw), and the services' saved stage --
corrected from the product, raw on the one grid, a new key refuses the old
results.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.ui import block01_display as bd  # noqa: E402
from block01.viewer import tissue_grid as tg  # noqa: E402
from block01.viewer.step1_source import (  # noqa: E402
    CoarsePlane, CorrectedRegion, SOURCE_CORRECTED, SOURCE_MISSING, SOURCE_RAW)


H, W = 200, 136          # level 0; grid at stride 16 is 13 x 9


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Table:
    """`Step1SourceTable`'s answers over one in-memory level-0 product."""

    def __init__(self, product=None, plane_stride=None, decisions=None):
        self._decisions = dict(decisions or {"M": "tophat"})
        self.product = product
        self.plane_stride = plane_stride
        self.region_calls = 0

    def decision(self, channel):
        return self._decisions.get(channel, "original")

    def source_of(self, channel):
        if self.decision(channel) not in ("tophat", "cucim"):
            return SOURCE_RAW
        return SOURCE_CORRECTED if self.product is not None else SOURCE_MISSING

    def region(self, channel):
        self.region_calls += 1
        return CorrectedRegion(self.product, (0, H, 0, W))

    def coarse_plane(self, channel, stride):
        if stride != self.plane_stride or self.product is None:
            return None
        mean, valid = _block_mean(self.product, stride)
        return CoarsePlane(np.where(valid, mean, 0).astype(np.float32),
                           stride, (0, 0))


def _block_mean(a, s):
    h, w = a.shape
    hh, ww = -(-h // s), -(-w // s)
    v = np.zeros((hh * s, ww * s)); m = np.zeros_like(v)
    v[:h, :w] = a; m[:h, :w] = 1
    sums = v.reshape(hh, s, ww, s).sum(axis=(1, 3))
    cnt = m.reshape(hh, s, ww, s).sum(axis=(1, 3))
    return (sums / np.maximum(cnt, 1)).astype(np.float32), cnt > 0


def _product():
    rng = np.random.default_rng(3)
    return rng.random((H, W)).astype(np.float32) * 50


# ── the grid ──────────────────────────────────────────────────────────
def test_grid_is_the_loaders_ceil_grid():
    assert tg.tissue_grid((H, W), 16) == (13, 9)
    raw = np.arange(H * W, dtype=np.float32).reshape(H, W)
    loader_like = raw[::16, ::16]                       # read_region_lowres
    assert loader_like.shape == (13, 9)
    assert tg.to_tissue_grid(loader_like, (H, W), 16) is loader_like


def _loader_rows(h0, n_level, ds):
    """`read_region_lowres`'s own mapping (core/io_loader.py)."""
    rows0 = np.arange(0, h0, ds)
    return np.clip((rows0 / (h0 / float(n_level))).astype(np.int64), 0,
                   n_level - 1)


def test_a_finer_pyramid_record_takes_the_loaders_rows():
    raw = np.arange(H * W, dtype=np.float32).reshape(H, W)
    record = raw[::8, ::8][: H // 8, : W // 8]          # floor level, 25 x 17
    out = tg.to_tissue_grid(record, (H, W), 16)
    assert out.shape == (13, 9)
    rows = _loader_rows(H, record.shape[0], 16)
    cols = _loader_rows(W, record.shape[1], 16)
    np.testing.assert_array_equal(out, record[np.ix_(rows, cols)])


def test_the_real_slide_shapes_follow_the_loader():
    # TMA4: 46800 rows, pyramid record 1462, grid stride 64 -> 732 rows
    record = np.arange(1462, dtype=np.float32)[:, None] * np.ones((1, 840))
    out = tg.to_tissue_grid(record, (46800, 26880), 64)
    assert out.shape == (732, 420)
    np.testing.assert_array_equal(out[:, 0], _loader_rows(46800, 1462, 64))


def test_a_coarser_or_unrelated_shape_is_refused():
    assert tg.to_tissue_grid(np.zeros((7, 5), np.float32), (H, W), 16) is None


# ── the corrected overview ────────────────────────────────────────────
@pytest.mark.parametrize("plane", [None, 8, 16])
def test_corrected_overview_is_the_level0_block_mean(plane, monkeypatch):
    reductions = []
    real = tg.reduce_corrected
    monkeypatch.setattr(tg, "reduce_corrected",
                        lambda *a, **k: reductions.append(1) or real(*a, **k))
    product = _product()
    table = _Table(product, plane_stride=plane)
    out = tg.corrected_overview(table, "M", (H, W), 16)
    want, _valid = _block_mean(product, 16)
    assert out.shape == (13, 9)
    # partial edge blocks too: each plane block weighs the level-0 samples
    # it stands for
    np.testing.assert_allclose(out, want, rtol=1e-5)
    assert (not reductions) == (plane is not None)     # a plane is sliced


def test_no_product_is_no_picture_never_raw():
    assert tg.corrected_overview(_Table(None), "M", (H, W), 16) is None


def test_a_runtime_reduction_can_be_cancelled():
    asked = []

    def cancelled():
        asked.append(1)
        return len(asked) > 1
    with pytest.raises(tg.Cancelled):
        tg.corrected_overview(_Table(_product()), "M", (H * 4, W), 4,
                              cancelled=cancelled)


# ── the services' saved stage ─────────────────────────────────────────
class _RawSource:
    def __init__(self, arrays):
        self.arrays = arrays
        self.asked = []

    def tissue_lowres_array(self, channel):
        return self.arrays.get(channel)

    def tissue_lowres_status(self, channels):
        self.asked.extend(channels)
        return {ch: "resident" if ch in self.arrays else "requested"
                for ch in channels}

    def tissue_dataset_token(self):
        return ("t", 1)


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _services(app, table, key="k1"):
    services = bd.Block01DisplayServices()
    raw = np.arange(H * W, dtype=np.float32).reshape(H, W)
    source = _RawSource({"M": raw[::8, ::8][: H // 8, : W // 8],   # raw M too
                         "N": raw[::8, ::8][: H // 8, : W // 8]})
    services._lowres_source = source
    stage = {"key": key, "table": table, "level0_shape": (H, W), "stride": 16}
    services.set_saved_stage_port(lambda: dict(stage))
    return services, source, stage, raw


def test_corrected_channel_comes_from_the_product_and_raw_on_the_grid(app):
    product = _product()
    services, source, _stage, raw = _services(app, _Table(product, 16))
    try:
        assert services.tissue_array("M") is None       # never the raw M
        assert services.ensure_tissue_arrays(["M", "N"]) == ["M"]
        assert "M" not in source.asked                  # no raw read for M
        assert _wait(lambda: services.tissue_array("M") is not None)
        np.testing.assert_allclose(services.tissue_array("M"),
                                   _block_mean(product, 16)[0], rtol=1e-5)
        n = services.tissue_array("N")
        assert n.shape == (13, 9)
        np.testing.assert_array_equal(n[:12, :8], raw[::16, ::16][:12, :8])
    finally:
        services.finalize_close()


def test_a_missing_product_draws_empty_never_raw(app):
    services, _source, _stage, _raw = _services(app, _Table(None))
    frames = []
    services.coordinator.request_frame = lambda **kw: frames.append(kw)
    try:
        assert services.ensure_tissue_arrays(["M"]) == ["M"]
        assert _wait(lambda: "M" in services._saved_missing)
        assert services.ensure_tissue_arrays(["M"]) == []
        empty = services.tissue_array("M")
        assert empty.shape == (13, 9) and not empty.any()
        assert frames and frames[-1]["channel"] == "M"     # redrawn without it
    finally:
        services.finalize_close()


def test_a_failed_read_draws_empty_and_asks_for_a_frame(app):
    services, _source, _stage, _raw = _services(app, _Table(_product(), 16))
    frames = []
    services.coordinator.request_frame = lambda **kw: frames.append(kw)
    try:
        services._saved_stage()
        services._on_saved_failed({"channel": "M", "error": "boom"},
                                  services._saved_key)
        assert "M" in services._saved_missing
        assert not services.tissue_array("M").any()
        assert frames and frames[-1]["channel"] == "M"
        services._on_saved_failed({"channel": "N", "error": "late"}, "old")
        assert "N" not in services._saved_missing     # another Save's
    finally:
        services.finalize_close()


def test_a_new_save_drops_the_old_arrays_and_refuses_late_results(app):
    product = _product()
    services, _source, stage, _raw = _services(app, _Table(product, 16))
    try:
        services.ensure_tissue_arrays(["M"])
        assert _wait(lambda: services.tissue_array("M") is not None)
        stage["key"] = "k2"
        stage["table"] = _Table(product * 0 + 7, 16)
        services.set_saved_stage_port(lambda: dict(stage))
        assert services.tissue_array("M") is None       # old save is gone
        services._on_saved_read({"token": "k1", "channel": "M",
                                 "array": np.ones((13, 9), np.float32)})
        assert services.tissue_array("M") is None       # late, refused
        services.ensure_tissue_arrays(["M"])
        assert _wait(lambda: services.tissue_array("M") is not None)
        assert float(services.tissue_array("M").max()) == pytest.approx(7.0)
    finally:
        services.finalize_close()


def test_without_a_port_the_old_answer_stands(app):
    services = bd.Block01DisplayServices()
    try:
        arr = np.ones((4, 4), np.float32)
        services._lowres_source = _RawSource({"N": arr})
        assert services.tissue_array("N") is arr
    finally:
        services.finalize_close()
