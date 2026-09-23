"""Deterministic synthetic inputs, already shaped the way plan 7.11.4 says each
engine receives them (values in [0, 1], float32)."""
import numpy as np


def _blobs(h, w, r_lo, r_hi, pitch, seed):
    from skimage.draw import disk
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w), np.float32)
    for cy in range(pitch // 2, h - pitch // 2, pitch):
        for cx in range(pitch // 2, w - pitch // 2, pitch):
            rr, cc = disk((cy + rng.integers(-3, 4), cx + rng.integers(-3, 4)),
                          int(rng.integers(r_lo, r_hi)), shape=(h, w))
            img[rr, cc] = rng.uniform(0.5, 1.0)
    return img


def nuclei(h=384, w=384, seed=0):
    from skimage.filters import gaussian
    rng = np.random.default_rng(seed + 1)
    img = gaussian(_blobs(h, w, 6, 10, 30, seed), 1.0)
    return np.clip(img + rng.normal(0, 0.02, (h, w)), 0, 1).astype(np.float32)


def membrane(h=384, w=384, seed=0):
    from skimage.filters import gaussian
    from skimage.segmentation import find_boundaries
    from skimage.morphology import dilation, disk
    cells = _blobs(h, w, 12, 15, 30, seed)
    lab = (cells > 0).astype(np.int32)
    ring = dilation(find_boundaries(lab), disk(1)).astype(np.float32)
    return gaussian(ring, 1.0).astype(np.float32)


def wholecell_rgb(h=384, w=384, seed=0):
    """[fusion, fusion, DAPI] (R10)."""
    f, d = membrane(h, w, seed), nuclei(h, w, seed)
    return np.stack([f, f, d], axis=-1)


def mesmer_pair(h=384, w=384, seed=0):
    """[nucleus, fusion] (7.11.1)."""
    return np.stack([nuclei(h, w, seed), membrane(h, w, seed)], axis=-1)
