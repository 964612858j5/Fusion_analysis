"""Tests for block01 Batch Step 4 discovery, CSV, and validation."""

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor

from block01.ui.batch_step4_dialog import (
    BatchStep4Dialog, discover_samples,
    COL_SAMPLE, COL_OME_TIFF, COL_MASK, COL_OUTPUT_DIR, COL_PREFIX, COL_CHECK,
    _BG_BAD,
)

_app = QApplication.instance() or QApplication([])


def _make_sample(root, name, *, with_mask=True, with_ome=True, done=False):
    """Build a sample dir tree; return (seg_run_dir, ome_path, mask_path)."""
    scan = os.path.join(root, name, "Scan1")
    os.makedirs(scan, exist_ok=True)
    ome_path = ""
    if with_ome:
        ome_path = os.path.join(scan, f"{name}_Scan1.ome.tiff")
        open(ome_path, "w").close()

    seg = os.path.join(scan, "v10", "rois", "ROI_1", "step2",
                       "segmentation_runs", "seg_001")
    os.makedirs(seg, exist_ok=True)
    mask_path = ""
    if with_mask:
        mask_path = os.path.join(seg, "global_mask.dat")
        open(mask_path, "w").close()
    if done:
        open(os.path.join(seg, f"{name}_v10_cell_features.h5ad"), "w").close()
    return seg, ome_path, mask_path


class DiscoverSamplesTests(unittest.TestCase):

    def test_discover_samples_basic(self):
        with tempfile.TemporaryDirectory() as td:
            for n in ("sampleA", "sampleB", "sampleC"):
                _make_sample(td, n)
            results = discover_samples(td)
            self.assertEqual(len(results), 3)
            names = [r["sample_name"] for r in results]
            self.assertEqual(names, ["sampleA", "sampleB", "sampleC"])
            for r in results:
                self.assertTrue(r["ome_tiff"].endswith(".ome.tiff"))
                self.assertTrue(r["mask"].endswith("global_mask.dat"))
                self.assertTrue(r["output_dir"].endswith("seg_001"))
                self.assertEqual(r["file_prefix"], f"{r['sample_name']}_v10")
                self.assertTrue(r["checked"])
                self.assertEqual(r["status"], "pending")

    def test_discover_missing_mask(self):
        with tempfile.TemporaryDirectory() as td:
            _make_sample(td, "noMask", with_mask=False)
            results = discover_samples(td)
            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertEqual(r["mask"], "(not found)")
            self.assertFalse(r["mask_ok"])
            self.assertFalse(r["checked"])

    def test_discover_already_done(self):
        with tempfile.TemporaryDirectory() as td:
            _make_sample(td, "doneSample", done=True)
            results = discover_samples(td)
            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertEqual(r["status"], "done ✓")
            self.assertFalse(r["checked"])

    def test_discover_skips_dirs_without_seg_run(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "no_seg", "Scan1"))
            _make_sample(td, "good")
            results = discover_samples(td)
            self.assertEqual([r["sample_name"] for r in results], ["good"])


class CsvRoundtripTests(unittest.TestCase):

    def test_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            seg, ome, mask = _make_sample(td, "sX")
            dlg = BatchStep4Dialog()
            dlg._add_row(checked=True, sample_name="sX", ome_tiff=ome,
                         mask=mask, output_dir=seg, file_prefix="sX_v10")
            dlg._add_row(checked=False, sample_name="sY", ome_tiff="(not found)",
                         mask="(not found)", output_dir="/tmp", file_prefix="sY_v10")
            csv_path = os.path.join(td, "plan.csv")
            dlg.export_csv(csv_path)

            dlg2 = BatchStep4Dialog()
            dlg2.import_csv(csv_path)
            self.assertEqual(dlg2.table.rowCount(), 2)
            self.assertEqual(dlg2.table.item(0, COL_SAMPLE).text(), "sX")
            self.assertEqual(dlg2.table.item(0, COL_OME_TIFF).text(), ome)
            self.assertEqual(dlg2.table.item(0, COL_MASK).text(), mask)
            self.assertEqual(dlg2.table.item(0, COL_OUTPUT_DIR).text(), seg)
            self.assertEqual(dlg2.table.item(0, COL_PREFIX).text(), "sX_v10")
            self.assertTrue(dlg2.table.cellWidget(0, COL_CHECK).isChecked())
            self.assertFalse(dlg2.table.cellWidget(1, COL_CHECK).isChecked())


class ValidateRowTests(unittest.TestCase):

    def test_validate_row_highlights_missing(self):
        with tempfile.TemporaryDirectory() as td:
            seg, ome, mask = _make_sample(td, "sV")
            dlg = BatchStep4Dialog()
            # Good paths → not flagged.
            dlg._add_row(sample_name="sV", ome_tiff=ome, mask=mask,
                         output_dir=seg, file_prefix="sV_v10")
            self.assertNotEqual(
                dlg.table.item(0, COL_OME_TIFF).background().color(), QColor(_BG_BAD))
            # Missing paths → red background.
            dlg._add_row(sample_name="bad", ome_tiff="(not found)",
                         mask="/does/not/exist.dat", output_dir="/nope")
            self.assertEqual(
                dlg.table.item(1, COL_OME_TIFF).background().color(), QColor(_BG_BAD))
            self.assertEqual(
                dlg.table.item(1, COL_MASK).background().color(), QColor(_BG_BAD))


if __name__ == "__main__":
    unittest.main()
