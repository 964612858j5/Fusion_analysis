import os
import tempfile
import time
import unittest

import numpy as np
import zarr

from block01.utils.channel_cache import SharedChannelStore
from block01.utils.step2_profiler import Step2Profiler
from block01.utils.tile_prefetch import TilePrefetcher
from block01.utils.tile_strategy import suggest_tile_strategy
from block01.workers.segment_merge_worker import SegmentMergeWorker


class Step2EnginePerfTests(unittest.TestCase):
    def test_tile_strategy_recommendation_limits_large_tiles(self):
        strategy = suggest_tile_strategy(
            19880,
            9280,
            "cellpose_wholecell_fusion",
            vram_gb=12,
            channel_count=2,
        )
        self.assertGreater(strategy["n_rows"] * strategy["n_cols"], 1)
        self.assertLess(strategy["estimated_tile_mpx"], 60.0)
        self.assertIn("estimated_vram_usage", strategy)

    def test_prefetch_queue_correctness(self):
        tiles = [{"value": i} for i in range(4)]
        calls = []

        def load(idx, tile):
            time.sleep(0.01)
            calls.append(idx)
            return {"idx": idx, "value": tile["value"] * 2}

        prefetcher = TilePrefetcher(tiles, load, prefetch_queue_size=2)
        try:
            out = [prefetcher.get(i) for i in range(len(tiles))]
        finally:
            metrics = prefetcher.snapshot_metrics()
            prefetcher.close()
        self.assertEqual([item["value"] for item in out], [0, 2, 4, 6])
        self.assertEqual(sorted(calls), [0, 1, 2, 3])
        self.assertGreaterEqual(metrics["prefetch_hit"], 1)

    def test_persistent_zarr_handle_and_cache_hit_miss(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "fused.zarr")
            arr = zarr.open(path, mode="w", shape=(8, 9, 2), dtype="uint16", chunks=(4, 4, 2))
            data = np.arange(8 * 9 * 2, dtype=np.uint16).reshape(8, 9, 2)
            arr[:] = data

            store = SharedChannelStore(max_cache_items=4)
            try:
                self.assertIs(store.zarr_group(path), store.zarr_group(path))
                first = store.read_fused(path, 1, 6, 2, 8)
                second = store.read_fused(path, 1, 6, 2, 8)
                dapi = store.read_dapi(path, 0, 4, 0, 5)
                metrics = store.snapshot_metrics()
            finally:
                store.close()

        np.testing.assert_array_equal(first, second)
        np.testing.assert_array_equal(dapi, data[0:4, 0:5, 1])
        self.assertGreaterEqual(metrics["cache_hit"], 1)
        self.assertGreaterEqual(metrics["cache_miss"], 2)
        self.assertGreater(metrics["cache_bytes"], 0)

    def test_output_label_consistency_with_cached_reads(self):
        mask = np.array(
            [
                [0, 1, 1, 0],
                [0, 1, 0, 2],
                [0, 0, 2, 2],
            ],
            dtype=np.uint32,
        )
        cy1, cx1 = SegmentMergeWorker._centroids_vectorised(mask)
        cy2, cx2 = SegmentMergeWorker._centroids_vectorised(mask.copy())
        np.testing.assert_allclose(cy1, cy2)
        np.testing.assert_allclose(cx1, cx2)

    def test_profiling_output_generation_with_engine_fields(self):
        with tempfile.TemporaryDirectory() as td:
            profiler = Step2Profiler(enabled=True, output_dir=td, run_id="engine", method="cellpose_wholecell_fusion")
            profiler.log_tile_stage("0", "tile_prefetch_wait", 0.01, prefetch_hit=1, prefetch_queue_depth=1)
            profiler.log_tile_stage("0", "cache_lookup", 0.0, cache_hit=2, cache_miss=1, cache_bytes=128, cache_evictions=0)
            profiler.log_tile_stage("0", "tile_prepare", 0.02)
            profiler.log_tile_stage("0", "tile_write", 0.03)
            summary = profiler.finalize()

            self.assertTrue(os.path.exists(os.path.join(td, "step2_profile.json")))
            self.assertTrue(os.path.exists(os.path.join(td, "step2_profile.csv")))
            self.assertIn("cache_hit_rate", summary)
            self.assertIn("io_hidden_by_prefetch_seconds", summary)
            self.assertIn("gpu_idle_estimate_seconds", summary)


if __name__ == "__main__":
    unittest.main()
