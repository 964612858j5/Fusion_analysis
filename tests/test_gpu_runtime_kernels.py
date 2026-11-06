"""The runtime-parameterised kernels must equal the cupyx reference exactly.

Block RT (docs/v17_step0_runtime_kernels.md §6.1).

Why this file drives the kernels DIRECTLY
-----------------------------------------
The public entries (`bg._tophat_gpu` / `bg._cucim_gpu`) dispatch to the new
kernels **or** to the legacy cupyx implementation when the backend is
unavailable. A test that only calls the public entry could therefore compare
legacy-with-legacy and pass while the new implementation is broken. So:

* the numerical tests call `gk.tophat()` / `gk.gaussian_bg_subtract()` — the
  new kernels only — and compute the reference **in the test** with
  `cupyx.scipy.ndimage` directly (never through `bg`'s dispatcher);
* the integration test calls the public entries and asserts the backend is
  `runtime-kernels`, that the call counters moved, and that
  `legacy_calls` did NOT move (no silent fallback);
* the fallback switch has its own test, so it can never mask a numeric
  failure.

Equality criterion
------------------
`max|new - reference| == 0.0` for every case below. That is measured on this
environment (this CuPy/CUDA/driver/GPU, these inputs); it is evidence of
equality here, not a proof that every machine's NVRTC compiles the same
instructions in the same order. The number is printed in the failure message.

Coverage: constant / zeros / random / gradient / impulse on a border /
non-square / thinner or smaller than the window / non-contiguous (transpose),
TopHat radius 1..150, Gaussian sigma 1..200, plus tile-seam parity.
"""

import numpy as np
import pytest

cp = pytest.importorskip("cupy")
ndi = pytest.importorskip("cupyx.scipy.ndimage")

bg = pytest.importorskip("block01.core.bg_correction")
gk = pytest.importorskip("block01.core.gpu_kernels")


def _require_gpu():
    """No GPU -> skip (nothing to test); GPU but no runtime kernels -> FAIL.

    A machine that can run the product but not its new kernels is exactly the
    regression this block must catch, so it must not be skipped away.
    """
    if not bg.GPU_MORPH_AVAILABLE:
        pytest.skip("no usable GPU morphology in this environment")
    backend = bg.gpu_filter_backend()
    if backend != "runtime-kernels":
        pytest.fail(
            f"runtime kernels are not active (backend={backend}, "
            f"error={bg.RUNTIME_KERNEL_ERROR!r}); the numerical acceptance "
            "cannot be demonstrated by the legacy path")


# ── references, computed here with cupyx (not through bg) ────────────────

def ref_tophat(x, radius):
    size = 2 * int(radius) + 1
    a = cp.asarray(x)
    eroded = ndi.grey_erosion(a, size=(size, size), mode="reflect")
    dilated = ndi.grey_dilation(eroded, size=(size, size), mode="reflect")
    return cp.asnumpy(cp.clip(a - dilated, 0, None)).astype(np.float32)


def ref_gaussian(x, sigma):
    a = cp.asarray(x)
    bg_est = cp.asnumpy(ndi.gaussian_filter(a, sigma=float(sigma), mode="reflect"))
    return np.clip(np.asarray(x, dtype=np.float32) - bg_est, 0, None).astype(np.float32)


def _cases():
    rng = np.random.default_rng(11)
    impulse = np.zeros((48, 64), np.float32)
    impulse[3, 5] = 1000.0
    impulse[0, 63] = 500.0                    # a corner: a border pixel
    impulse[47, 0] = 250.0
    return {
        "constant": np.full((96, 130), 7.5, np.float32),
        "zeros": np.zeros((96, 130), np.float32),
        "random": (rng.random((96, 130), dtype=np.float32) * 1000.0),
        "gradient": np.tile(np.linspace(0, 255, 130, dtype=np.float32), (96, 1)),
        "impulse_on_borders": impulse,
        "non_square": (rng.random((37, 211), dtype=np.float32) * 50.0),
        "one_pixel": np.array([[3.0]], np.float32),
        "smaller_than_window": (rng.random((5, 5), dtype=np.float32) * 10.0),
        "thinner_than_window": (rng.random((3, 500), dtype=np.float32) * 10.0),
    }


# ── the kernels vs the reference ─────────────────────────────────────────

@pytest.mark.parametrize("radius", [1, 2, 15, 63, 150])
def test_tophat_kernel_equals_cupyx(radius):
    _require_gpu()
    worst, where = 0.0, ""
    for name, arr in _cases().items():
        a = np.ascontiguousarray(arr)
        new = gk.tophat(a, radius)
        ref = ref_tophat(a, radius)
        assert new.dtype == np.float32 and new.shape == a.shape
        d = float(np.max(np.abs(new - ref)))
        if d > worst:
            worst, where = d, name
    assert worst == 0.0, (
        f"TopHat radius={radius}: max|new-cupyx| = {worst} on {where} "
        "(criterion: 0.0; square element, reflect border)")


@pytest.mark.parametrize("sigma", [1, 5, 50, 120, 200])
def test_gaussian_kernel_equals_cupyx(sigma):
    _require_gpu()
    worst, where = 0.0, ""
    for name, arr in _cases().items():
        a = np.ascontiguousarray(arr)
        new = gk.gaussian_bg_subtract(a, sigma)
        ref = ref_gaussian(a, sigma)
        assert new.dtype == np.float32 and new.shape == a.shape
        d = float(np.max(np.abs(new - ref)))
        if d > worst:
            worst, where = d, name
    assert worst == 0.0, (
        f"cuCIM sigma={sigma}: max|new-cupyx| = {worst} on {where} "
        "(criterion: 0.0; truncate=4, reflect, float32 weights/accumulation)")


@pytest.mark.parametrize("radius", [7, 63])
def test_tophat_kernel_on_a_non_contiguous_view(radius):
    _require_gpu()
    base = np.ascontiguousarray(np.random.default_rng(3).random((120, 80), dtype=np.float32) * 100)
    view = base.T
    assert not view.flags["C_CONTIGUOUS"]
    assert np.array_equal(gk.tophat(view, radius), ref_tophat(view, radius))


@pytest.mark.parametrize("sigma", [9, 120])
def test_gaussian_kernel_on_a_non_contiguous_view(sigma):
    _require_gpu()
    base = np.ascontiguousarray(np.random.default_rng(4).random((120, 80), dtype=np.float32) * 100)
    view = base.T
    assert not view.flags["C_CONTIGUOUS"]
    assert np.array_equal(gk.gaussian_bg_subtract(view, sigma), ref_gaussian(view, sigma))


# ── the public entries must actually use them ────────────────────────────

def test_public_tophat_entry_uses_the_runtime_kernel_without_falling_back():
    _require_gpu()
    tile = (np.random.default_rng(8).random((128, 128), dtype=np.float32) * 100)
    before = gk.stats()
    out = bg._tophat_gpu(tile, 33)
    after = gk.stats()
    assert bg.gpu_filter_backend() == "runtime-kernels"
    assert after["tophat_calls"] == before["tophat_calls"] + 1
    assert after["legacy_calls"] == before["legacy_calls"], "public entry fell back to cupyx"
    assert np.array_equal(out, gk.tophat(tile, 33))


def test_public_gaussian_entry_uses_the_runtime_kernel_without_falling_back():
    _require_gpu()
    tile = (np.random.default_rng(9).random((128, 128), dtype=np.float32) * 100)
    before = gk.stats()
    out = bg._cucim_gpu(tile, 44)
    after = gk.stats()
    assert bg.gpu_filter_backend() == "runtime-kernels"
    assert after["gaussian_calls"] == before["gaussian_calls"] + 1
    assert after["legacy_calls"] == before["legacy_calls"], "public entry fell back to cupyx"
    assert np.array_equal(out, gk.gaussian_bg_subtract(tile, 44))


# ── the fallback switch, tested on its own ───────────────────────────────

def test_legacy_switch_selects_the_reference_implementation(monkeypatch):
    _require_gpu()
    monkeypatch.setenv(gk.LEGACY_ENV, "1")
    assert bg.gpu_filter_backend() == "cupyx-legacy"
    tile = (np.random.default_rng(10).random((64, 64), dtype=np.float32) * 100)
    before = gk.stats()["legacy_calls"]
    out = bg._tophat_gpu(tile, 21)
    assert gk.stats()["legacy_calls"] == before + 1
    assert np.array_equal(out, ref_tophat(tile, 21))


# ── nothing compiled for new parameters ──────────────────────────────────

def test_new_parameters_do_not_compile():
    _require_gpu()
    import glob
    import os
    cache = os.environ.get("CUPY_CACHE_DIR", "")
    if not cache or not os.path.isdir(cache):
        pytest.skip("no CUPY_CACHE_DIR in this process")
    tile = (np.random.default_rng(6).random((320, 320), dtype=np.float32) * 100).astype(np.float32)
    gk.tophat(tile, 63)                       # make sure both kernels are warm
    gk.gaussian_bg_subtract(tile, 120)
    before = set(glob.glob(os.path.join(cache, "*.cubin")))
    for r in (7, 23, 44, 91, 150):
        gk.tophat(tile, r)
    for s in (2, 9, 31, 200):
        gk.gaussian_bg_subtract(tile, s)
    after = set(glob.glob(os.path.join(cache, "*.cubin")))
    assert not (after - before), f"new kernels compiled: {sorted(map(os.path.basename, after - before))}"


# ── the halo/tile contract still holds on the new path ───────────────────

def test_tiled_matches_whole_image_on_gpu():
    _require_gpu()
    assert bg.gpu_filter_backend() == "runtime-kernels"
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:512, 0:512]
    img = (0.2 * xx + 0.1 * yy).astype(np.float32)
    for _ in range(40):
        y, x = rng.integers(10, 500, 2)
        img[y - 3:y + 3, x - 3:x + 3] += rng.uniform(50, 200)
    for method, param, whole in (
            ("tophat", 23, gk.tophat(img, 23)),
            ("cucim", 37, gk.gaussian_bg_subtract(img, 37))):
        tiled = bg._apply_background_method_tiled(
            img, method, radius=param, sigma=param, tile_size=192, prefer_gpu=True)
        assert float(np.max(np.abs(tiled - whole))) == 0.0, f"{method} tiled seam"
