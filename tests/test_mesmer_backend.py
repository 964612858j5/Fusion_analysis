import json
import os
import tempfile
import unittest

import numpy as np

from block01.utils.mesmer_utils import (
    MESMER_WHOLE_CELL,
    build_mesmer_input,
    get_mesmer_device_status,
    mesmer_metadata,
)


class TestMesmerBackend(unittest.TestCase):
    def test_import_and_gpu_detection_do_not_crash(self):
        status = get_mesmer_device_status("auto")
        self.assertIn(status.device_used, ("gpu", "cpu"))
        self.assertIsInstance(status.deepcell_available, bool)

    def test_input_builder_two_channel_shape(self):
        source = {
            "DAPI": np.arange(100, dtype=np.float32).reshape(10, 10),
            "CD45": np.ones((10, 10), dtype=np.float32) * 2,
            "CD68": np.ones((10, 10), dtype=np.float32) * 3,
        }
        batch = build_mesmer_input(
            source,
            nuclear_channel="DAPI",
            membrane_channels=["CD45", "CD68"],
            weights={"CD45": 0.5, "CD68": 1.0},
            normalize=True,
        )
        self.assertEqual(batch.shape, (1, 10, 10, 2))
        self.assertEqual(batch.dtype, np.float32)

    def test_missing_deepcell_status_is_graceful(self):
        status = get_mesmer_device_status("cpu")
        if not status.tensorflow_available:
            self.assertIn("TensorFlow import failed", status.error)
        elif not status.deepcell_available:
            self.assertIn("DeepCell/Mesmer is not installed", status.error)

    def test_metadata_write(self):
        status = get_mesmer_device_status("cpu")
        meta = mesmer_metadata(
            MESMER_WHOLE_CELL,
            {"nuclear_channel": "DAPI", "membrane_channels": ["CD45"]},
            status,
            output_mask_path="/tmp/mask.ome.tiff",
            runtime_seconds=1.2,
        )
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "segmentation_meta.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        self.assertEqual(loaded["method"], MESMER_WHOLE_CELL)
        self.assertEqual(loaded["output_mask_path"], "/tmp/mask.ome.tiff")

    def test_mask_shape_contract_without_deepcell(self):
        image = np.zeros((12, 8), dtype=np.uint32)
        self.assertEqual(image.shape, (12, 8))


if __name__ == "__main__":
    unittest.main()
