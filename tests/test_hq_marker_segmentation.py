import json
import unittest

import numpy as np

from block01.workers.hq_marker_segmentation import (
    parse_hq_channels,
    segment_nuclei_hq,
    validate_hq_channels,
)
from block01.workers.segment_merge_worker import SegmentMergeWorker


def _synthetic_inputs():
    nuclei = np.zeros((64, 64), dtype=np.uint32)
    nuclei[18:25, 18:25] = 1
    nuclei[38:45, 40:47] = 2

    yy, xx = np.indices(nuclei.shape)
    ch1 = np.exp(-(((yy - 21) ** 2 + (xx - 21) ** 2) / 90.0))
    ch2 = np.exp(-(((yy - 41) ** 2 + (xx - 43) ** 2) / 90.0))
    ch3 = 0.5 * ch1 + 0.5 * ch2
    return nuclei, [ch1, ch2, ch3]


class TestHqMarkerSegmentation(unittest.TestCase):
    def test_parse_and_validate_hq_channels(self):
        channels = parse_hq_channels("PanCK;CD45; CD68 ")
        self.assertEqual(channels, ["PanCK", "CD45", "CD68"])
        self.assertEqual(validate_hq_channels(channels, ["DAPI", "PanCK", "CD45", "CD68"]), channels)
        with self.assertRaisesRegex(ValueError, "Missing HQ channel"):
            validate_hq_channels(["PanCK", "Missing"], ["PanCK"])
        with self.assertRaisesRegex(ValueError, "at least one"):
            validate_hq_channels([], ["PanCK"])


    def test_synthetic_hq_consensus_preserves_nucleus_ids_and_areas(self):
        nuclei, markers = _synthetic_inputs()
        final, nuclei_out, rows = segment_nuclei_hq(
            nuclei,
            markers,
            ["PanCK", "CD45", "CD68"],
            max_cell_radius=8,
            min_signal_threshold=0.02,
        )

        np.testing.assert_array_equal(nuclei_out, nuclei)
        self.assertLessEqual(set(np.unique(final)), {0, 1, 2})
        self.assertTrue({1, 2}.issubset(set(np.unique(final))))
        for label in (1, 2):
            self.assertGreaterEqual(np.count_nonzero(final == label), np.count_nonzero(nuclei == label))
        self.assertEqual(final.ndim, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("per_channel_support_score" in row for row in rows))


    def test_hq_segmentation_meta_fields_are_written(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            worker = SegmentMergeWorker.__new__(SegmentMergeWorker)
            worker.seg_config = {
                "model_type": "cpsam",
                "diameter": None,
                "flow_threshold": 0.4,
                "cellprob_threshold": 0.0,
                "min_size": 15,
                "hq_channels": ["PanCK", "CD45", "CD68"],
                "max_cell_radius": 12,
                "normalization_percentile_low": 1.0,
                "normalization_percentile_high": 99.5,
                "consensus_mode": "adaptive_best_channel",
                "channel_weights": {"PanCK": 1.0, "CD45": 0.8, "CD68": 1.0},
                "min_signal_threshold": 0.08,
            }
            meta = worker._hq_meta_fields(
                str(tmp_path / "global_nuclei_mask.ome.tiff"),
                str(tmp_path / "global_mask.ome.tiff"),
                str(tmp_path / "hq_qc_table.csv"),
            )
            out = tmp_path / "segmentation_meta.json"
            out.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            loaded = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(loaded["method"], "cellpose_nuclei_hq")
        self.assertEqual(loaded["display_name"], "Cellpose nuclei + HQ")
        self.assertEqual(loaded["hq_channels"], ["PanCK", "CD45", "CD68"])
        self.assertEqual(loaded["consensus_mode"], "adaptive_best_channel")
        self.assertTrue(loaded["final_cell_mask_path"].endswith("global_mask.ome.tiff"))
        self.assertTrue(loaded["nuclei_mask_path"].endswith("global_nuclei_mask.ome.tiff"))
        self.assertTrue(loaded["qc_table_path"].endswith("hq_qc_table.csv"))


if __name__ == "__main__":
    unittest.main()
