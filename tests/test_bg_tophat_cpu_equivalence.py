"""Block CS P1: the CPU TopHat computed with OpenCV is bitwise identical to
skimage's white_tophat(disk(r), mode='reflect'), the reference it replaces."""
import numpy as np
import pytest
from skimage.morphology import disk, white_tophat

from block01.core import bg_correction as bg


def _reference(a, r):
    return white_tophat(np.asarray(a, np.float32), footprint=disk(r),
                        mode="reflect").astype(np.float32)


@pytest.mark.skipif(bg._cv2 is None, reason="OpenCV not installed")
@pytest.mark.parametrize("r", list(range(1, 41)))
def test_every_radius_matches_skimage(r):
    rng = np.random.default_rng(r)
    a = (rng.random((180, 211)) * 255).astype(np.uint8).astype(np.float32)
    got = bg._tophat_cpu(a, r)
    assert got.dtype == np.float32
    assert np.array_equal(got, _reference(a, r))


@pytest.mark.parametrize("shape", [(1, 1), (1, 9), (9, 1), (2, 3), (3, 5), (31, 31),
                                   (32, 33), (64, 63), (300, 400)])
@pytest.mark.parametrize("r", [1, 2, 5, 15, 30])
def test_odd_even_and_tiny_shapes_match(shape, r, monkeypatch):
    rng = np.random.default_rng(sum(shape) + r)
    a = (rng.standard_normal(shape) * 1e4).astype(np.float32)
    if min(shape) >= 2 * r + 1:
        assert np.array_equal(bg._tophat_cpu(a, r), _reference(a, r))
        return
    # An image smaller than the kernel goes to skimage (whose own output is
    # then not even repeatable: it reads past the image), never to OpenCV.
    class _NoCv2:
        def __getattr__(self, name):
            raise AssertionError("OpenCV used for an image smaller than the kernel")
    monkeypatch.setattr(bg, "_cv2", _NoCv2())
    assert bg._tophat_cpu(a, r).shape == shape


def test_non_contiguous_uint16_and_non_finite_inputs_match():
    rng = np.random.default_rng(7)
    a = (rng.random((200, 400)) * 65535).astype(np.uint16)[:, ::2]
    assert np.array_equal(bg._tophat_cpu(a, 6), _reference(a, 6))
    b = rng.random((120, 120)).astype(np.float32)
    b[5, 7], b[60, 60] = np.nan, np.inf
    assert np.array_equal(bg._tophat_cpu(b, 4), _reference(b, 4), equal_nan=True)


def test_the_public_cpu_paths_use_it():
    rng = np.random.default_rng(9)
    a = (rng.random((256, 256)) * 255).astype(np.float32)
    ref = _reference(a, 5)
    assert np.array_equal(bg.correct_tile(a, "tophat", 5, "cpu"), ref)
    if not bg.GPU_MORPH_AVAILABLE:
        assert np.array_equal(bg._apply_tophat_cpu(a, 5), ref)


def test_without_opencv_skimage_is_used(monkeypatch):
    monkeypatch.setattr(bg, "_cv2", None)
    a = np.arange(400, dtype=np.float32).reshape(20, 20)
    assert np.array_equal(bg._tophat_cpu(a, 3), _reference(a, 3))
