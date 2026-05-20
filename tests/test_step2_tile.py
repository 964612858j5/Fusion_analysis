import unittest
from dataclasses import FrozenInstanceError

import numpy as np

from block01.utils.step2_tile import (
    Step2Tile,
    compute_tile_grid_metrics,
    crop_valid_region,
    local_own_bbox,
)


class Step2TileTests(unittest.TestCase):
    def test_geometry_properties_and_legacy_dict(self):
        tile = Step2Tile(
            index=7,
            row=1,
            col=2,
            own_bbox=(100, 220, 300, 560),
            read_bbox=(80, 250, 260, 600),
            overlap=20,
            out_prefix="Full WSI",
        )

        self.assertEqual(tile.tile_id, "Full WSI:7")
        self.assertEqual(tile.own_h, 120)
        self.assertEqual(tile.own_w, 260)
        self.assertEqual(tile.read_h, 170)
        self.assertEqual(tile.read_w, 340)
        self.assertEqual(tile.read_shape, (170, 340))
        self.assertEqual(tile.local_own_bbox, (20, 140, 40, 300))
        self.assertEqual(tile.as_legacy_dict(), {
            "row": 1,
            "col": 2,
            "own": (100, 220, 300, 560),
            "read": (80, 250, 260, 600),
        })

    def test_plain_tile_id_and_frozen(self):
        tile = Step2Tile(
            index=3,
            row=0,
            col=3,
            own_bbox=(0, 10, 30, 40),
            read_bbox=(0, 12, 28, 42),
            overlap=2,
        )

        self.assertEqual(tile.tile_id, "3")
        with self.assertRaises(FrozenInstanceError):
            tile.row = 9

    def test_crop_valid_region_with_step2_tile(self):
        arr = np.arange(10 * 12, dtype=np.uint32).reshape(10, 12)
        tile = Step2Tile(
            index=0,
            row=0,
            col=0,
            own_bbox=(104, 109, 205, 211),
            read_bbox=(100, 110, 200, 212),
            overlap=4,
        )

        self.assertEqual(local_own_bbox(tile), (4, 9, 5, 11))
        cropped = crop_valid_region(arr, tile)
        np.testing.assert_array_equal(cropped, arr[4:9, 5:11])
        self.assertEqual(cropped.shape, (5, 6))

    def test_crop_valid_region_with_legacy_dict(self):
        arr = np.arange(8 * 9, dtype=np.uint32).reshape(8, 9)
        tile = {
            "own": (12, 17, 33, 39),
            "read": (10, 18, 30, 39),
        }

        self.assertEqual(local_own_bbox(tile), (2, 7, 3, 9))
        cropped = crop_valid_region(arr, tile)
        np.testing.assert_array_equal(cropped, arr[2:7, 3:9])
        self.assertEqual(cropped.shape, (5, 6))

    def test_crop_valid_region_copy_semantics(self):
        arr = np.arange(6 * 7, dtype=np.uint32).reshape(6, 7)
        tile = Step2Tile(
            index=0,
            row=0,
            col=0,
            own_bbox=(2, 5, 3, 6),
            read_bbox=(0, 6, 0, 7),
            overlap=2,
        )

        copied = crop_valid_region(arr, tile, copy=True)
        copied[0, 0] = 999
        self.assertNotEqual(arr[2, 3], 999)
        self.assertFalse(np.shares_memory(copied, arr))

        view = crop_valid_region(arr, tile, copy=False)
        self.assertTrue(np.shares_memory(view, arr))
        view[0, 0] = 777
        self.assertEqual(arr[2, 3], 777)

    def test_compute_tile_grid_metrics_single_tile(self):
        tile = Step2Tile(
            index=0,
            row=0,
            col=0,
            own_bbox=(0, 100, 0, 100),
            read_bbox=(0, 100, 0, 100),
            overlap=20,
        )

        metrics = compute_tile_grid_metrics([tile], 100, 100)

        self.assertEqual(metrics["full_pixels"], 10000)
        self.assertEqual(metrics["n_tiles"], 1)
        self.assertEqual(metrics["n_rows"], 1)
        self.assertEqual(metrics["n_cols"], 1)
        self.assertEqual(metrics["overlap"], 20)
        self.assertEqual(metrics["own_pixels_total"], 10000)
        self.assertEqual(metrics["read_pixels_total"], 10000)
        self.assertEqual(metrics["duplicate_pixels"], 0)
        self.assertEqual(metrics["duplicate_factor"], 1.0)
        self.assertEqual(metrics["overlap_overhead_factor"], 1.0)

    def test_compute_tile_grid_metrics_2x2_overlap(self):
        tiles = [
            Step2Tile(0, 0, 0, (0, 50, 0, 50), (0, 60, 0, 60), 10),
            Step2Tile(1, 0, 1, (0, 50, 50, 100), (0, 60, 40, 100), 10),
            Step2Tile(2, 1, 0, (50, 100, 0, 50), (40, 100, 0, 60), 10),
            Step2Tile(3, 1, 1, (50, 100, 50, 100), (40, 100, 40, 100), 10),
        ]

        metrics = compute_tile_grid_metrics(tiles, 100, 100)

        self.assertEqual(metrics["n_tiles"], 4)
        self.assertEqual(metrics["n_rows"], 2)
        self.assertEqual(metrics["n_cols"], 2)
        self.assertEqual(metrics["overlap"], 10)
        self.assertEqual(metrics["own_pixels_total"], 10000)
        self.assertGreater(metrics["read_pixels_total"], metrics["full_pixels"])
        self.assertGreater(metrics["duplicate_factor"], 1.0)
        self.assertEqual(metrics["duplicate_pixels"], metrics["read_pixels_total"] - metrics["own_pixels_total"])

    def test_compute_tile_grid_metrics_legacy_dict(self):
        tiles = [
            {
                "row": 0,
                "col": 0,
                "own": (0, 50, 0, 50),
                "read": (0, 55, 0, 55),
                "overlap": 5,
            },
            {
                "row": 0,
                "col": 1,
                "own": (0, 50, 50, 100),
                "read": (0, 55, 45, 100),
                "overlap": 5,
            },
        ]

        metrics = compute_tile_grid_metrics(tiles, 50, 100)

        self.assertEqual(metrics["n_tiles"], 2)
        self.assertEqual(metrics["n_rows"], 1)
        self.assertEqual(metrics["n_cols"], 2)
        self.assertEqual(metrics["overlap"], 5)
        self.assertEqual(metrics["own_pixels_total"], 5000)
        self.assertGreater(metrics["read_pixels_total"], metrics["own_pixels_total"])


if __name__ == "__main__":
    unittest.main()
