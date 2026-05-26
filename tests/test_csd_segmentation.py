import json
import os
import tempfile
import unittest

import numpy as np

from block01.workers.constrained_donut_segmentation import (
    run_constrained_donut_segmentation,
    save_csd_outputs,
)


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


class TestConstrainedDonutSegmentation(unittest.TestCase):
    def test_background_noise_does_not_force_round_expansion(self):
        shape = (96, 96)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 48, 48, 6)] = 1
        rng = np.random.default_rng(4)
        noise = rng.normal(100.0, 2.0, size=shape).astype(np.float32)

        result = run_constrained_donut_segmentation(
            nuclei,
            [noise],
            ["CD45"],
            {"donut_size": 24, "nucleus_shrink": 2, "use_gpu": False},
        )

        nuc_area = int(np.count_nonzero(nuclei == 1))
        final_area = int(np.count_nonzero(result["final_labels"] == 1))
        self.assertLess(final_area, nuc_area * 1.25)
        self.assertTrue(np.all(result["final_labels"][nuclei == 1] == 1))

    def test_irregular_signal_expands_to_signal_contour(self):
        shape = (100, 110)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 50, 45, 5)] = 1
        yy, xx = np.indices(shape)
        rng = np.random.default_rng(7)
        marker = rng.normal(20.0, 1.5, size=shape).astype(np.float32)
        signal = (
            (((yy - 50) / 9.0) ** 2 + ((xx - 52) / 23.0) ** 2 < 1.0)
            | (((yy - 42) / 5.0) ** 2 + ((xx - 62) / 15.0) ** 2 < 1.0)
        )
        marker[signal] = 100.0

        result = run_constrained_donut_segmentation(
            nuclei,
            [marker],
            ["PanCK"],
            {
                "donut_size": 30,
                "nucleus_shrink": 2,
                "bg_sigma_factor": 3.0,
                "max_circularity": 0.99,
                "use_gpu": False,
            },
        )

        nuc_area = int(np.count_nonzero(nuclei == 1))
        final = result["final_labels"] == 1
        self.assertGreater(int(np.count_nonzero(final)), nuc_area * 2)
        self.assertGreater(np.count_nonzero(final & signal), nuc_area)
        self.assertTrue(result["qc_rows"][0]["refinement_applied"])

    def test_mixed_signal_only_expands_positive_cell(self):
        shape = (112, 112)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 38, 36, 5)] = 1
        nuclei[_disk(shape, 72, 76, 5)] = 2
        rng = np.random.default_rng(9)
        marker = rng.normal(30.0, 1.0, size=shape).astype(np.float32)
        marker[_disk(shape, 38, 43, 13)] = 120.0

        result = run_constrained_donut_segmentation(
            nuclei,
            [marker],
            ["CD68"],
            {"donut_size": 24, "nucleus_shrink": 2, "use_gpu": False},
        )

        nuc1 = int(np.count_nonzero(nuclei == 1))
        nuc2 = int(np.count_nonzero(nuclei == 2))
        area1 = int(np.count_nonzero(result["final_labels"] == 1))
        area2 = int(np.count_nonzero(result["final_labels"] == 2))
        self.assertGreater(area1, nuc1 * 1.5)
        self.assertLess(area2, nuc2 * 1.25)

    def test_circularity_fallback_replaces_round_artifact(self):
        shape = (96, 96)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 48, 48, 5)] = 1
        rng = np.random.default_rng(11)
        marker = rng.normal(10.0, 0.5, size=shape).astype(np.float32)
        marker[_disk(shape, 48, 48, 24)] = 100.0

        result = run_constrained_donut_segmentation(
            nuclei,
            [marker],
            ["artifact"],
            {
                "donut_size": 28,
                "nucleus_shrink": 2,
                "max_circularity": 0.92,
                "circularity_ratio_threshold": 3.0,
                "circularity_fallback_expand": 3,
                "use_gpu": False,
            },
        )

        row = result["qc_rows"][0]
        self.assertTrue(row["fallback_to_hq"])
        self.assertEqual(row["low_confidence_reason"], "circularity_fallback")
        self.assertLess(row["cell_to_nucleus_ratio"], 3.0)

    def test_neighboring_nuclei_are_split_by_watershed(self):
        shape = (80, 110)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 40, 42, 5)] = 1
        nuclei[_disk(shape, 40, 62, 5)] = 2
        rng = np.random.default_rng(13)
        marker = rng.normal(10.0, 0.5, size=shape).astype(np.float32)
        bridge = (np.abs(np.arange(shape[0])[:, None] - 40) <= 5) & (np.arange(shape[1])[None, :] >= 42) & (np.arange(shape[1])[None, :] <= 62)
        marker[bridge] = 80.0

        result = run_constrained_donut_segmentation(
            nuclei,
            [marker],
            ["bridge"],
            {"donut_size": 22, "nucleus_shrink": 2, "max_circularity": 1.1, "use_gpu": False},
        )

        final = result["final_labels"]
        self.assertGreater(np.count_nonzero(final == 1), np.count_nonzero(nuclei == 1))
        self.assertGreater(np.count_nonzero(final == 2), np.count_nonzero(nuclei == 2))
        self.assertFalse(np.any(final[nuclei == 1] == 2))
        self.assertFalse(np.any(final[nuclei == 2] == 1))

    def test_save_outputs_metadata(self):
        shape = (48, 48)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 24, 24, 4)] = 1
        marker = np.zeros(shape, dtype=np.float32)
        marker[_disk(shape, 24, 30, 8)] = 50.0
        params = {"hq_channels": ["CD45"], "donut_size": 12, "use_gpu": False}
        result = run_constrained_donut_segmentation(nuclei, [marker], ["CD45"], params)

        with tempfile.TemporaryDirectory() as td:
            paths, meta_path = save_csd_outputs(td, "synthetic", result, params)
            self.assertTrue(os.path.exists(paths["final_cell_mask_path"]))
            self.assertTrue(os.path.exists(paths["qc_table_path"]))
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["method"], "cellpose_nuclei_csd")
            self.assertEqual(meta["display_name"], "Cellpose nuclei + CSD")
            self.assertEqual(meta["hq2_mode"], "constrained_signal_donut")


if __name__ == "__main__":
    unittest.main()
