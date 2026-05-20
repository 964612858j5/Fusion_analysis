"""Step2 merge policy interface definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

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

        Step 4B intentionally does not replace SegmentMergeWorker logic.
        """
        raise NotImplementedError("Centroid ownership filtering is not wired in Step 4B.")

    def relabel_owned_region(self, labels, kept_labels, global_id_offset):
        """
        Interface placeholder for relabeling kept labels into global ids.
        """
        raise NotImplementedError("Owned-region relabeling is not wired in Step 4B.")

    def merge_into_global(self, global_mask, labels, tile, global_id_offset):
        """
        Interface placeholder for future full merge.
        Must not be used by SegmentMergeWorker in Step 4B.
        """
        raise NotImplementedError("Global merge policy is not wired in Step 4B.")
