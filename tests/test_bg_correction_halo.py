"""Production tiled-correction halo contract (BG_CORRECTION_ALGO_VERSION 2).

The tiled production path must match a whole-image single call within float
tolerance for BOTH methods — i.e. tile borders leave no seams. The gaussian
halo was widened from 2*sigma to ceil(4*sigma) (full filter support); this
test is the regression pin. CPU paths only (deterministic); GPU seam parity
is covered separately (viewer golden seam tests / optional GPU test).
"""

import numpy as np
import pytest


@pytest.fixture()
def image():
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:600, 0:600]
    img = (0.2 * xx + 0.1 * yy).astype(np.float32)          # smooth background
    for _ in range(60):                                     # bright blobs
        y, x = rng.integers(20, 580, 2)
        img[y - 3:y + 3, x - 3:x + 3] += rng.uniform(50, 200)
    return img


def test_gaussian_tiled_matches_whole_image(image, monkeypatch):
    from block01.core import bg_correction as bg
    monkeypatch.setattr(bg, "GPU_MORPH_AVAILABLE", False)
    sigma = 40
    whole = bg._apply_cucim_or_cpu(image, sigma, prefer_gpu=False)
    tiled = bg._apply_background_method_tiled(
        image, "cucim", sigma=sigma, tile_size=256, prefer_gpu=False)
    assert np.allclose(tiled, whole, atol=1e-3), (
        f"gaussian seam error max={np.abs(tiled - whole).max()}")


def test_tophat_tiled_matches_whole_image(image, monkeypatch):
    from block01.core import bg_correction as bg
    monkeypatch.setattr(bg, "GPU_MORPH_AVAILABLE", False)
    radius = 12
    whole = bg._apply_tophat_cpu(image, radius)
    tiled = bg._apply_background_method_tiled(
        image, "tophat", radius=radius, tile_size=256, prefer_gpu=False)
    assert np.allclose(tiled, whole, atol=1e-3), (
        f"tophat seam error max={np.abs(tiled - whole).max()}")


def test_algo_version_bumped():
    from block01.core import bg_correction as bg
    assert bg.BG_CORRECTION_ALGO_VERSION == "2"
