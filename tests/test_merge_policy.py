import unittest

import numpy as np

from block01.utils.merge_policy import CentroidOwnershipMergePolicy, MergeResult
from block01.utils.step2_tile import Step2Tile, crop_valid_region
from block01.utils.tile_scheduler import TileScheduler


class MergePolicyTests(unittest.TestCase):
    def test_policy_crop_valid_region_matches_step2_tile_helper(self):
        policy = CentroidOwnershipMergePolicy()
        arr = np.arange(10 * 12, dtype=np.uint32).reshape(10, 12)
        tile = Step2Tile(
            index=0,
            row=0,
            col=0,
            own_bbox=(104, 109, 205, 211),
            read_bbox=(100, 110, 200, 212),
            overlap=4,
        )

        expected = crop_valid_region(arr, tile, copy=True)
        actual = policy.crop_valid_region(arr, tile, copy=True)

        np.testing.assert_array_equal(actual, expected)
        self.assertFalse(np.shares_memory(actual, arr))

    def test_policy_local_own_bbox(self):
        policy = CentroidOwnershipMergePolicy()
        tile = Step2Tile(
            index=0,
            row=0,
            col=0,
            own_bbox=(10, 30, 40, 70),
            read_bbox=(5, 35, 25, 80),
            overlap=15,
        )

        self.assertEqual(policy.local_own_bbox(tile), (5, 25, 15, 45))

    def test_policy_placeholders_not_used(self):
        policy = CentroidOwnershipMergePolicy()
        labels = np.zeros((8, 8), dtype=np.uint32)
        tile = Step2Tile(0, 0, 0, (0, 4, 0, 4), (0, 8, 0, 8), 4)

        with self.assertRaises(NotImplementedError):
            policy.filter_owned_labels(labels, tile)
        with self.assertRaises(NotImplementedError):
            policy.relabel_owned_region(labels, [1], 10)
        with self.assertRaises(NotImplementedError):
            policy.merge_into_global(labels, labels, tile, 10)

    def test_scheduler_has_merge_policy(self):
        scheduler = TileScheduler(
            full_h=100,
            full_w=120,
            n_rows=2,
            n_cols=3,
            overlap_px=10,
        )
        tile = scheduler.tiles[4]
        arr = np.arange(tile.read_h * tile.read_w, dtype=np.uint32).reshape(tile.read_h, tile.read_w)

        self.assertIsInstance(scheduler.merge_policy, CentroidOwnershipMergePolicy)
        cropped = scheduler.crop_valid_region(arr, tile)
        self.assertEqual(cropped.shape, (tile.own_h, tile.own_w))

    def test_merge_result_dataclass(self):
        result = MergeResult(
            merged_count=3,
            labels_count=5,
            kept_labels_count=3,
            global_id_offset_before=10,
            global_id_offset_after=13,
            metadata={"tile_id": "0"},
        )

        self.assertEqual(result.merged_count, 3)
        self.assertEqual(result.metadata["tile_id"], "0")


if __name__ == "__main__":
    unittest.main()
