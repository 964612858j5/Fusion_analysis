"""Step2 merge policy interface definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

import numpy as np

from .step2_tile import crop_valid_region, local_own_bbox


@dataclass
class MergeResult:
    merged_count: int
    labels_count: int
    kept_labels_count: int
    global_id_offset_before: int
    global_id_offset_after: int
    metadata: Dict[str, Any] = field(default_factory=dict)


class CentroidOwnershipMergePolicy:
    """
    Interface/specification for Step2 centroid-based tile merge.

    Step 4B scope:
    - Define methods and expected inputs/outputs.
    - Provide small pure helpers only if safe.
    - Do NOT replace SegmentMergeWorker merge logic yet.
    """

    def __init__(self, copy_crop=True):
        self.copy_crop = bool(copy_crop)

    def crop_valid_region(self, arr, tile, copy=None):
        """
        Return the tile own-region crop from an array defined on tile.read_bbox.
        Internally calls utils.step2_tile.crop_valid_region().
        """
        if copy is None:
            copy = self.copy_crop
        return crop_valid_region(arr, tile, copy=bool(copy))

    def local_own_bbox(self, tile):
        """
        Return local own bbox: coordinates of tile.own_bbox inside tile.read_bbox.
        """
        return local_own_bbox(tile)

    def filter_owned_labels(self, labels, tile):
        """
        Interface placeholder for centroid-based ownership filtering.

        Expected future behavior:
        - labels is a 2D label image over tile.read_bbox.
        - compute centroids for labels.
        - keep labels whose centroids fall inside tile.local_own_bbox.
        - return kept label ids.

        """
        labels = np.asarray(labels)
        n = int(labels.max()) if labels.size else 0
        if n == 0:
            return []
        h, w = labels.shape
        flat = labels.ravel()
        ys = np.repeat(np.arange(h, dtype=np.float32), w)
        xs = np.tile(np.arange(w, dtype=np.float32), h)
        cnts = np.bincount(flat, minlength=n + 2)
        sum_y = np.bincount(flat, weights=ys, minlength=n + 2)
        sum_x = np.bincount(flat, weights=xs, minlength=n + 2)
        valid = cnts[1:n + 1] > 0
        cy = np.where(valid, sum_y[1:n + 1] / np.maximum(cnts[1:n + 1], 1), -1)
        cx = np.where(valid, sum_x[1:n + 1] / np.maximum(cnts[1:n + 1], 1), -1)

        local_y0, local_y1, local_x0, local_x1 = self.local_own_bbox(tile)
        kept = []
        for label_idx in range(n):
            lcy, lcx = cy[label_idx], cx[label_idx]
            if (
                lcy >= local_y0 and lcy < local_y1
                and lcx >= local_x0 and lcx < local_x1
            ):
                kept.append(label_idx + 1)
        return kept

    def relabel_owned_region(self, labels, kept_labels, global_id_offset):
        """
        Relabel kept labels into global ids.
        """
        labels = np.asarray(labels)
        n_raw = int(labels.max()) if labels.size else 0
        offset_before = int(global_id_offset)
        kept_labels = [int(v) for v in (kept_labels or [])]
        lut = np.zeros(n_raw + 1, dtype=np.uint32)
        for new_id, lab in enumerate(kept_labels, start=1):
            if 0 < lab <= n_raw:
                lut[lab] = new_id + offset_before
        relabeled = lut[labels] if n_raw > 0 else np.zeros_like(labels, dtype=np.uint32)
        offset_after = offset_before + len(kept_labels)
        metadata = {
            "n_raw": n_raw,
            "n_kept": len(kept_labels),
            "global_id_offset_before": offset_before,
            "global_id_offset_after": offset_after,
        }
        return relabeled, offset_after, metadata

    def merge_into_global(self, global_mask, local_labels, tile, global_id_offset=0):
        """
        Merge relabeled own-region labels into global mmap.
        """
        oy0, oy1, ox0, ox1 = tile.own_bbox if hasattr(tile, "own_bbox") else tile["own"]
        own_labels = self.crop_valid_region(local_labels, tile, copy=False)
        dst = global_mask[oy0:oy1, ox0:ox1]
        np.copyto(dst, own_labels, where=(own_labels > 0))
        labels_count = int(np.asarray(local_labels).max()) if np.asarray(local_labels).size else 0
        merged_count = int(len(np.unique(own_labels[own_labels > 0]))) if np.any(own_labels > 0) else 0
        return MergeResult(
            merged_count=merged_count,
            labels_count=labels_count,
            kept_labels_count=merged_count,
            global_id_offset_before=int(global_id_offset),
            global_id_offset_after=int(global_id_offset) + merged_count,
            metadata={
                "own_bbox": [int(oy0), int(oy1), int(ox0), int(ox1)],
                "own_shape": list(own_labels.shape),
            },
        )
