import builtins
import json
import os
import tempfile
import unittest

from block01.utils.step2_profiler import Step2Profiler


class Step2ProfilerTests(unittest.TestCase):
    def test_start_end_and_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            profiler = Step2Profiler(enabled=True, output_dir=td, run_id="run-a", method="cellpose_nuclei")
            profiler.start_stage("read_tile", tile_id=3, tile_h=16, tile_w=12)
            event = profiler.end_stage("read_tile", tile_id=3, labels_count=4)
            self.assertIsNotNone(event)
            profiler.log_tile_stage(3, "merge_or_write", 0.01, labels_count=4)
            summary = profiler.finalize()

            self.assertTrue(os.path.exists(os.path.join(td, "step2_profile.json")))
            self.assertTrue(os.path.exists(os.path.join(td, "step2_profile.csv")))
            self.assertTrue(os.path.exists(os.path.join(td, "step2_profile_summary.txt")))
            self.assertIn(summary["suspected_bottleneck"], {"read_tile", "merge_or_write"})
            with open(os.path.join(td, "step2_profile.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["run_id"], "run-a")
            self.assertTrue(data["events"])

    def test_context_manager(self):
        with tempfile.TemporaryDirectory() as td:
            profiler = Step2Profiler(enabled=True, output_dir=td)
            with profiler.time_stage("model_inference", tile_id=1):
                value = sum([1, 2, 3])
            self.assertEqual(value, 6)
            self.assertEqual(profiler.events[0]["stage"], "model_inference")

    def test_psutil_missing_does_not_crash(self):
        profiler = Step2Profiler(enabled=True)
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("missing psutil")
            return real_import(name, *args, **kwargs)

        try:
            builtins.__import__ = fake_import
            self.assertIsNone(profiler.snapshot_memory())
            profiler.log_tile_stage(1, "read_tile", 0.0)
        finally:
            builtins.__import__ = real_import

    def test_gpu_snapshot_failure_does_not_crash(self):
        profiler = Step2Profiler(enabled=True)
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name in {"torch", "cupy", "tensorflow"}:
                raise RuntimeError("gpu library import failed")
            return real_import(name, *args, **kwargs)

        try:
            builtins.__import__ = fake_import
            self.assertIsNone(profiler.snapshot_gpu())
        finally:
            builtins.__import__ = real_import

    def test_disabled_profiler_has_no_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            profiler = Step2Profiler(enabled=False, output_dir=td)
            profiler.start_stage("read_tile")
            profiler.end_stage("read_tile")
            profiler.log_tile_stage(1, "read_tile", 1.0)
            self.assertEqual(profiler.finalize(), {})
            self.assertFalse(os.path.exists(os.path.join(td, "step2_profile.json")))


if __name__ == "__main__":
    unittest.main()
