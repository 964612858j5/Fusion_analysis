"""Which labels of a read window belong to its own region, and their new ids.

One implementation for Step1 and Step2 (plan 7.11.3, 7.12 "one set of
wheels"). MOVED, not rewritten, from the authoritative inline code of
Step2's tile loop (`workers/segment_merge_worker.py`, the relabel stage after
`_centroids_vectorised`), and held to it by `tests/test_label_ownership.py`,
which runs Step2's real merge and compares:

- centroids by bincount over labels 1..max (a missing label gets -1);
- a label is kept when its centroid lies in the half-open own region
  [oy0, oy1) x [ox0, ox1), in LOCAL coordinates of the window;
- kept labels are renumbered in their original order, offset + 1, offset + 2 ...
  through one LUT;
- a secondary mask that SHARES the primary's labels (nuclei + expansion) is
  renumbered with the same LUT, labels above the primary's max dropped;
- the count is the number of kept labels, never the largest id.

Step2 keeps its own copy until it is switched to this one (plan block V2).
"""

import numpy as np


def centroids(mask):
    """(cy, cx) for labels 1..max(mask); index i is label i + 1, and a label
    that is not present gets -1 in both."""
    n = int(mask.max())
    if n == 0:
        return np.array([]), np.array([])
    h, w = mask.shape
    flat = mask.ravel()
    ys = np.repeat(np.arange(h, dtype=np.float32), w)
    xs = np.tile(np.arange(w, dtype=np.float32), h)
    cnts = np.bincount(flat, minlength=n + 2)
    sum_y = np.bincount(flat, weights=ys, minlength=n + 2)
    sum_x = np.bincount(flat, weights=xs, minlength=n + 2)
    valid = cnts[1:n + 1] > 0
    cy = np.where(valid, sum_y[1:n + 1] / np.maximum(cnts[1:n + 1], 1), -1)
    cx = np.where(valid, sum_x[1:n + 1] / np.maximum(cnts[1:n + 1], 1), -1)
    return cy, cx


def kept_labels(mask, own_local):
    """The labels whose centroid lies in `own_local` = (oy0, oy1, ox0, ox1),
    half-open, in the window's own coordinates; ascending."""
    oy0, oy1, ox0, ox1 = own_local
    cy, cx = centroids(mask)
    return [i + 1 for i in range(len(cy))
            if oy0 <= cy[i] < oy1 and ox0 <= cx[i] < ox1]


def ownership_lut(mask, keep, offset=0):
    """A LUT over 0..max(mask) giving each kept label offset + its rank."""
    lut = np.zeros(int(mask.max()) + 1, dtype=np.uint32)
    for new_id, lab in enumerate(keep, start=1):
        lut[lab] = new_id + offset
    return lut


def apply_ownership(primary, own_local, offset=0, shared=None):
    """Keep what the window owns and renumber it.

    `primary` decides ownership. `shared` is a mask whose labels are the
    primary's (nuclei before expansion); it goes through the same LUT.
    Returns (primary_out, shared_out or None, n_kept); both outputs have the
    window's shape, 0 where nothing is kept.
    """
    primary = np.asarray(primary)
    keep = kept_labels(primary, own_local) if primary.size and primary.max() > 0 else []
    if not keep:
        zero = np.zeros(primary.shape, dtype=np.uint32)
        return zero, (None if shared is None else np.zeros(np.shape(shared), np.uint32)), 0
    lut = ownership_lut(primary, keep, offset)
    out = lut[primary]
    shared_out = None
    if shared is not None:
        n_raw = int(primary.max())
        safe = np.where(shared <= n_raw, shared, 0).astype(np.uint32, copy=False)
        shared_out = lut[safe]
    return out, shared_out, len(keep)


def crop(mask, own_local):
    oy0, oy1, ox0, ox1 = own_local
    return mask[oy0:oy1, ox0:ox1]
