"""The nucleus overview Step2 draws a fused store's tile grid over (block
PA-5b fix 2b, user ruling 2026-10-06).

Step2 used to build it by reading ``z[::ds, ::ds, 1]`` from the fused zarr on
the GUI thread -- a store with no pyramid, so the strided read decoded every
chunk (4.7 s on the A5 synthetic slide, twice per entry). Generate already
holds each unit's fused pixels when it writes them, so it samples the same
pixels there, saves them beside the store and keeps them in memory.

Exactly the same pixels as the old read: the stride is computed from the
store's shape by the one rule below, and a unit contributes the rows and
columns of the GLOBAL grid ``0, ds, 2*ds, ...`` that fall inside it.
"""

import collections
import os
import threading

import numpy as np

#: the overview's long side at most (Step2 block S2T)
OVERVIEW_LONG_SIDE = 4096
NUCLEUS_CHANNEL = 1          # the fused store's (cyto, nucleus) planes

_MEMORY = collections.OrderedDict()      # realpath -> array, newest last
_MEMORY_ENTRIES = 2                      # <= 2 x 32 MB
_LOCK = threading.Lock()


def stride(shape):
    """The sampling step for a store of `shape` (h, w, ...)."""
    h, w = int(shape[0]), int(shape[1])
    return max(1, -(-max(h, w) // OVERVIEW_LONG_SIDE))


def overview_shape(shape):
    ds = stride(shape)
    return (-(-int(shape[0]) // ds), -(-int(shape[1]) // ds))


def path_for(zarr_path):
    """Where a store's overview is saved: beside it, in its run."""
    return os.path.splitext(os.path.abspath(zarr_path))[0] + "_overview_nucleus.npy"


class Sampler:
    """Built while a region's units are written: `add` each unit's fused
    (h, w, 2) block at its region offset; `array` is the overview."""

    def __init__(self, shape, dtype=np.uint16):
        self.ds = stride(shape)
        self.array = np.zeros(overview_shape(shape), dtype=dtype)

    def add(self, block, oy, ox):
        ds = self.ds
        fy, fx = (-int(oy)) % ds, (-int(ox)) % ds      # first grid row / column inside
        part = np.asarray(block)[fy::ds, fx::ds, NUCLEUS_CHANNEL]
        if not part.size:
            return
        y, x = (int(oy) + fy) // ds, (int(ox) + fx) // ds
        self.array[y:y + part.shape[0], x:x + part.shape[1]] = part


def save(zarr_path, array):
    """Atomically, beside the store. Returns the path."""
    dst = path_for(zarr_path)
    tmp = dst + ".tmp.npy"
    np.save(tmp, np.asarray(array))
    os.replace(tmp, dst)
    remember(zarr_path, array)
    return dst


def forget(zarr_path):
    """Before a store at `zarr_path` is replaced: its old overview -- file and
    memory -- is no longer its own (codex PA-5b). Never raises."""
    key = os.path.realpath(zarr_path)
    with _LOCK:
        _MEMORY.pop(key, None)
    try:
        os.remove(path_for(zarr_path))
    except OSError:
        pass


def remember(zarr_path, array):
    key = os.path.realpath(zarr_path)
    with _LOCK:
        _MEMORY[key] = array
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > _MEMORY_ENTRIES:
            _MEMORY.popitem(last=False)


def load(zarr_path, shape):
    """The overview of the store at `zarr_path` (of `shape`) from memory or
    from its saved file, or None. A file of another shape is not believed."""
    key = os.path.realpath(zarr_path)
    want = overview_shape(shape)
    with _LOCK:
        arr = _MEMORY.get(key)
    if arr is not None and tuple(arr.shape) == want:
        return arr
    path = path_for(zarr_path)
    if not os.path.isfile(path):
        return None
    try:
        arr = np.load(path)
    except (OSError, ValueError):
        return None
    if tuple(arr.shape) != want:
        return None
    remember(zarr_path, arr)
    return arr


def read_from_store(z):
    """The old way, for a store saved before this existed: the strided read.
    Off the GUI thread only."""
    ds = stride(z.shape)
    return np.asarray(z[::ds, ::ds, NUCLEUS_CHANNEL])


def normalised(arr):
    """[0, 1] float32 by the 1st / 99.5th percentile of the non-zero
    pixels -- Step2's display rule (unchanged)."""
    arr = np.asarray(arr).astype(np.float32)
    nz = arr[arr > 0]
    if nz.size > 100:
        lo, hi = np.percentile(nz, [1, 99.5])
        if hi > lo:
            return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    return np.zeros_like(arr)
