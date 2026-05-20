import unittest
from dataclasses import FrozenInstanceError

from block01.utils.step2_tile import Step2Tile


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


if __name__ == "__main__":
    unittest.main()
