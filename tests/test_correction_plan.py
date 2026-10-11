"""Step0's corrected views: exact where they can be, saved where it exists,
and labelled where they are the approximation (viewer/correction_plan.py).

WHAT THE USER REPORTED (CD45RO, TMA4, 2026-10-11): Step0's TopHat preview
looked very different from Step1's real corrected result. The saved product
was right; the preview corrected a coarse pyramid level with the radius
divided by the downsample, which at radius 1-2 px removes the tissue floor
(CD45RO, tophat 15: share of tissue with signal 0.93 saved vs 0.37 preview
at downsample 16).

Pinned: the per-level rule, NATIVE == level-0 correction block-averaged,
SAVED == the product block-averaged, a saved product is used only for the
exact (method, parameter) it was made with, and the approximation badge.
"""

import numpy as np
import pytest

from block01.core import saved_correction
from block01.core.bg_correction import BG_CORRECTION_ALGO_VERSION
from block01.viewer import correction_plan as cp
from block01.viewer.caches import LRUByteCache
from block01.viewer.correction_compute import CorrectionCompute
from block01.viewer.tile_types import (CorrectionKey, QualityLevel,
                                       SourceIdentity, TileAddress,
                                       TileGridSpec)

H, W, TS = 256, 192, 32


class _Provider:
    """A 3-level mean pyramid (2x) over one random level-0 channel."""

    def __init__(self):
        rng = np.random.default_rng(7)
        self.l0 = (rng.random((H, W)) * 40).astype(np.float32)
        self.levels = [self.l0]
        for _ in range(2):
            a = self.levels[-1]
            h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
            self.levels.append(a[:h, :w].reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3)))
        self.ident = SourceIdentity(dataset_path="/fake/plan.ome.tif",
                                    dataset_fingerprint="f", stage="raw")

    def source_identity(self):
        return self.ident

    def level_shape(self, level):
        return self.levels[level].shape

    def level_downsample(self, level):
        return H / self.levels[level].shape[0]

    def read_tile(self, channel, tile):
        a = self.levels[tile.level]
        ts = tile.grid.tile_size
        y0, x0 = tile.ty * ts, tile.tx * ts
        return a[y0:y0 + ts, x0:x0 + ts].copy(), 0.0


def _key(provider, level, tx, ty, params, quality, algo=BG_CORRECTION_ALGO_VERSION):
    return CorrectionKey(source=provider.source_identity(), channel="M",
                         tile=TileAddress(grid=TileGridSpec(tile_size=TS),
                                          level=level, tx=tx, ty=ty),
                         method="tophat", params=params,
                         algorithm_version=algo, quality=quality)


def _blocks(a, s):
    h, w = a.shape[0] // s, a.shape[1] // s
    return a[:h * s, :w * s].reshape(h, s, w, s).mean(axis=(1, 3))


# ── the rule ──────────────────────────────────────────────────────────
def test_the_rule_per_level():
    assert cp.plan("tophat", (15,), 0, 1.0) == (
        (15,), QualityLevel.INTERACTIVE, BG_CORRECTION_ALGO_VERSION)
    assert cp.plan("tophat", (15,), 1, 2.0)[:2] == ((15,), QualityLevel.NATIVE)
    assert cp.plan("tophat", (15,), 1, 2.0004)[1] == QualityLevel.NATIVE
    assert cp.plan("tophat", (15,), 2, 4.0)[:2] == ((4,), QualityLevel.INTERACTIVE)
    saved = cp.SavedProduct(None, "M", "tophat", 15, "tok")
    assert cp.plan("tophat", (15,), 3, 8.0, saved) == (
        (15,), QualityLevel.SAVED, f"{BG_CORRECTION_ALGO_VERSION}+saved:tok")
    assert not cp.is_approximate(QualityLevel.NATIVE, 1, 2.0)
    assert cp.is_approximate(QualityLevel.INTERACTIVE, 2, 4.0)
    assert not cp.is_approximate(QualityLevel.INTERACTIVE, 0, 1.0)


# ── NATIVE: corrected at level 0, block-averaged ─────────────────────
def test_native_is_the_level0_correction_seen_at_level1():
    provider = _Provider()
    compute = CorrectionCompute(provider, LRUByteCache(1 << 26))
    key = _key(provider, 1, 1, 2, (3,), QualityLevel.NATIVE)
    assert all(rk.tile.level == 0 for rk in compute.raw_keys_for(key))
    tile = compute.compute(key).pixels.handle
    full = compute.correct_array(provider.l0, "tophat", 3)
    want = _blocks(full, 2)[2 * TS:3 * TS, 1 * TS:2 * TS]
    np.testing.assert_allclose(tile, want, atol=1e-4)


# ── SAVED: the product, only for its own (method, parameter) ─────────
class _Region:
    def __init__(self, arr):
        self.array = arr
        self.bbox = (0, arr.shape[0], 0, arr.shape[1])

    def overlaps(self, y0, y1, x0, x1):
        return True

    def read(self, y0, y1, x0, x1):
        a = self.array[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]
        return a, (max(0, y0), min(H, y1), max(0, x0), min(W, x1))


class _Table:
    def __init__(self, arr):
        self.r = _Region(arr)

    def coarse_plane(self, channel, stride):
        return None

    def region(self, channel):
        return self.r


def test_saved_tile_is_the_product_block_averaged(monkeypatch):
    provider = _Provider()
    product = (provider.l0 * 0.5).astype(np.float32)
    saved = cp.SavedProduct(_Table(product), "M", "tophat", 15, "tok")
    monkeypatch.setattr(cp, "saved_product", lambda *a, **k: saved)
    compute = CorrectionCompute(provider, LRUByteCache(1 << 26))
    params, quality, algo = cp.plan("tophat", (15,), 2, 4.0, saved)
    key = _key(provider, 2, 0, 1, params, quality, algo)
    assert compute.raw_keys_for(key) == []
    tile = compute.compute(key).pixels.handle
    np.testing.assert_allclose(tile, _blocks(product, 4)[TS:2 * TS, 0:TS],
                               atol=1e-5)
    # a key naming another write of the product is refused, not served
    stale = _key(provider, 2, 0, 1, params, quality,
                 f"{BG_CORRECTION_ALGO_VERSION}+saved:old")
    with pytest.raises(RuntimeError):
        compute.compute(stale)


def test_saved_product_matches_only_its_own_parameter(tmp_path):
    zarr = pytest.importorskip("zarr")
    slide = str(tmp_path / "slide.ome.tif")
    store = str(tmp_path / "corrected_channels.zarr")
    root = zarr.open_group(store, mode="w")
    arr = root.create_group("Full_WSI").create_dataset(
        "M", shape=(H, W), chunks=(64, 64), dtype="f4")
    arr[:] = 1.0
    arr.attrs.update({"correction_method": "tophat",
                      "correction_param_value": 15,
                      "source_identity": "abc",
                      "roi_bbox_fullres": [0, H, 0, W],
                      "source_shape": [H, W]})

    class _Loader:
        filepath = slide
        _corrected_zarr_path = store
        _corrected_decisions = {"M": "tophat"}

    loader = _Loader()
    saved_correction.register_loader(loader)
    found = cp.saved_product(slide, "M", "tophat", (15,), (H, W))
    assert found is not None and found.token == "abc"
    assert cp.saved_product(slide, "M", "tophat", (14,), (H, W)) is None
    assert cp.saved_product(slide, "M", "cucim", (15,), (H, W)) is None
    assert cp.saved_product(slide, "N", "tophat", (15,), (H, W)) is None
    # the same product does not cover a bigger slide
    assert cp.saved_product(slide, "M", "tophat", (15,), (2 * H, W)) is None
    # an ROI product says nothing about the rest of the slide
    arr.attrs["roi_bbox_fullres"] = [0, H // 2, 0, W]
    saved_correction.register_loader(loader)            # a new Save
    assert cp.saved_product(slide, "M", "tophat", (15,), (H, W)) is None


def test_an_roi_at_the_origin_does_not_vouch_for_the_whole_slide(tmp_path):
    """The writer stamps `source_shape` with the CORRECTED array's shape, so
    an ROI anchored at (0, 0) carries its own size there (codex round 2)."""
    zarr = pytest.importorskip("zarr")
    from block01.core.bg_correction import stamp_corrected_channel_identity as stamp
    slide = str(tmp_path / "slide.ome.tif")
    store = str(tmp_path / "corrected_channels.zarr")
    root = zarr.open_group(store, mode="w")
    arr = root.create_group("ROI_1").create_dataset(
        "M", shape=(H // 2, W), chunks=(64, 64), dtype="f4")
    stamp(arr, "M", None, "tophat", roi_name="ROI_1",
          roi_bbox_fullres=(0, H // 2, 0, W),
          correction_param_name="tophat_radius", correction_param_value=15)
    arr.attrs["source_identity"] = "abc"
    assert list(arr.attrs["source_shape"]) == [H // 2, W]

    class _Loader:
        filepath = slide
        _corrected_zarr_path = store
        _corrected_decisions = {"M": "tophat"}

    saved_correction.register_loader(_Loader())
    assert cp.saved_product(slide, "M", "tophat", (15,), (H, W)) is None


def test_a_resaved_product_does_not_count_old_tiles_as_coverage():
    """Same channel, method and parameter, a new Save: the old SAVED tiles
    are not the current level's coverage (codex round 2)."""
    from block01.viewer.explore_view import ExploreController
    provider = _Provider()
    old = _key(provider, 2, 0, 0, (15,), QualityLevel.SAVED,
               f"{BG_CORRECTION_ALGO_VERSION}+saved:old")
    ctx = (provider.source_identity(), "M", "tophat", (15,), 2,
           QualityLevel.SAVED, f"{BG_CORRECTION_ALGO_VERSION}+saved:new")
    assert not ExploreController._key_matches_context(old, ctx)
    current = old._replace(algorithm_version=ctx[-1]) if hasattr(
        old, "_replace") else None
    if current is None:
        import dataclasses
        current = dataclasses.replace(old, algorithm_version=ctx[-1])
    assert ExploreController._key_matches_context(current, ctx)
