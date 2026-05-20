import unittest

import numpy as np

from block01.utils.tile_scheduler import TileScheduler


class TileSchedulerTests(unittest.TestCase):
    def test_scheduler_matches_legacy_geometry(self):
        scheduler = TileScheduler(
            full_h=1000,
            full_w=800,
            n_rows=3,
            n_cols=4,
            overlap_px=50,
        )

        self.assertEqual(scheduler.tile_h, 334)
        self.assertEqual(scheduler.tile_w, 200)
        self.assertEqual(len(scheduler), 12)

        first = scheduler.tiles[0]
        self.assertEqual(first.index, 0)
        self.assertEqual(first.row, 0)
        self.assertEqual(first.col, 0)
        self.assertEqual(first.own_bbox, (0, 334, 0, 200))
        self.assertEqual(first.read_bbox, (0, 384, 0, 250))

        last = scheduler.tiles[-1]
        self.assertEqual(last.index, 11)
        self.assertEqual(last.row, 2)
        self.assertEqual(last.col, 3)
        self.assertEqual(last.own_bbox, (668, 1000, 600, 800))
        self.assertEqual(last.read_bbox, (618, 1000, 550, 800))

    def test_scheduler_tile_ids(self):
        scheduler = TileScheduler(
            full_h=100,
            full_w=100,
            n_rows=1,
            n_cols=2,
            overlap_px=10,
            out_prefix="ROI_1",
        )

        self.assertEqual(scheduler.tiles[0].tile_id, "ROI_1:0")
        self.assertEqual(scheduler.tiles[1].tile_id, "ROI_1:1")

    def test_scheduler_metrics(self):
        scheduler = TileScheduler(
            full_h=100,
            full_w=100,
            n_rows=2,
            n_cols=2,
            overlap_px=10,
        )

        metrics = scheduler.metrics()

        self.assertEqual(metrics["n_tiles"], 4)
        self.assertEqual(metrics["n_rows"], 2)
        self.assertEqual(metrics["n_cols"], 2)
        self.assertGreaterEqual(metrics["duplicate_factor"], 1.0)

    def test_scheduler_crop_valid_region(self):
        scheduler = TileScheduler(
            full_h=100,
            full_w=120,
            n_rows=2,
            n_cols=3,
            overlap_px=10,
        )
        tile = scheduler.tiles[4]
        arr = np.arange(tile.read_h * tile.read_w, dtype=np.uint32).reshape(tile.read_h, tile.read_w)

        cropped = scheduler.crop_valid_region(arr, tile)

        self.assertEqual(cropped.shape, (tile.own_h, tile.own_w))
        np.testing.assert_array_equal(cropped, arr[tile.local_own_bbox[0]:tile.local_own_bbox[1],
                                                   tile.local_own_bbox[2]:tile.local_own_bbox[3]])

    def test_scheduler_as_legacy_tiles(self):
        scheduler = TileScheduler(
            full_h=50,
            full_w=60,
            n_rows=1,
            n_cols=2,
            overlap_px=5,
        )

        legacy = scheduler.as_legacy_tiles()

        self.assertEqual(len(legacy), 2)
        self.assertEqual(legacy[0], scheduler.tiles[0].as_legacy_dict())


if __name__ == "__main__":
    unittest.main()
