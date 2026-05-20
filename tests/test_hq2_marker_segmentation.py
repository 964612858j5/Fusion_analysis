import json
import os
import tempfile
import unittest

import numpy as np

from block01.workers.hq2_marker_segmentation import (
    run_hq2_segmentation,
    save_hq2_outputs,
)


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


class TestHQ2MarkerSegmentation(unittest.TestCase):
    def test_synthetic_irregular_macrophage_like_signal(self):
        shape = (96, 112)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 30, 34, 6)] = 1
        nuclei[_disk(shape, 58, 72, 7)] = 2
        nuclei[_disk(shape, 68, 34, 5)] = 3

        yy, xx = np.indices(shape)
        cd68 = np.zeros(shape, dtype=np.float32)
        cd206 = np.zeros(shape, dtype=np.float32)
        cd45 = np.zeros(shape, dtype=np.float32)

        # Irregular macrophage-like arm around nucleus 2.
        region2 = (
            (((yy - 58) / 13.0) ** 2 + ((xx - 72) / 22.0) ** 2 < 1.0)
            | (((yy - 48) / 7.0) ** 2 + ((xx - 52) / 19.0) ** 2 < 1.0)
        )
        cd68[region2] = 0.95
        cd206[region2 & (xx < 78)] = 0.8

        region1 = _disk(shape, 30, 34, 13)
        cd45[region1] = 0.75
        region3 = _disk(shape, 68, 34, 10)
        cd68[region3] = 0.65

        # Add weak deterministic background.
        cd68 += (xx % 9).astype(np.float32) * 0.004
        cd206 += (yy % 7).astype(np.float32) * 0.003
        cd45 += ((xx + yy) % 11).astype(np.float32) * 0.003

        params = {
            "hq_channels": ["CD68", "CD206", "CD45"],
            "max_cell_radius": 18,
            "normalization_percentile_low": 1.0,
            "normalization_percentile_high": 99.5,
            "min_signal_threshold": 0.05,
            "signal_map_mode": "per_cell_best_channel",
            "hq2_expansion_engine": "conservative",
            "max_refine_radius": 6,
            "min_refine_signal": 0.05,
        }

        result = run_hq2_segmentation(
            nuclei,
            [cd68, cd206, cd45],
            ["CD68", "CD206", "CD45"],
            params,
        )

        final = result["final_labels"]
        self.assertEqual(final.shape, nuclei.shape)
        self.assertEqual(result["high_confidence_core_labels"].shape, nuclei.shape)
        self.assertEqual(result["expansion_added_pixels"].shape, nuclei.shape)
        self.assertGreaterEqual(int(final.max()), 3)

        for cell_id in (1, 2, 3):
            nucleus_area = int(np.count_nonzero(nuclei == cell_id))
            final_area = int(np.count_nonzero(final == cell_id))
            self.assertGreaterEqual(final_area, nucleus_area)
            self.assertTrue(np.all(final[nuclei == cell_id] == cell_id))

        self.assertTrue(result["qc_rows"])
        self.assertEqual({row["cell_id"] for row in result["qc_rows"]}, {1, 2, 3})

        with tempfile.TemporaryDirectory() as td:
            paths, meta_path = save_hq2_outputs(td, "synthetic", result, params)
            self.assertTrue(os.path.exists(paths["final_cell_mask_path"]))
            self.assertTrue(os.path.exists(paths["qc_table_path"]))
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["method"], "cellpose_nuclei_hq2")
            self.assertEqual(meta["display_name"], "Cellpose nuclei + HQ2")
            self.assertEqual(meta["hq2_mode"], "conservative_refinement")
            self.assertFalse(meta["imagej_proposal_enabled"])
            self.assertTrue(meta["hq_proposal_mask_path"])
            self.assertTrue(meta["initial_hq_mask_path"])
            self.assertTrue(meta["refinement_added_pixels_path"])


if __name__ == "__main__":
    unittest.main()
