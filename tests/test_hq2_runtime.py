import time
import unittest

import numpy as np

from block01.workers.hq2_marker_segmentation import run_hq2_segmentation


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


class TestHQ2Runtime(unittest.TestCase):
    def test_512_patch_finishes_under_30_seconds(self):
        shape = (512, 512)
        nuclei = np.zeros(shape, dtype=np.uint32)
        rng = np.random.default_rng(42)
        centers = []
        grid_y = np.linspace(40, 472, 10, dtype=int)
        grid_x = np.linspace(40, 472, 5, dtype=int)
        for y in grid_y:
            for x in grid_x:
                centers.append((int(y + rng.integers(-8, 9)), int(x + rng.integers(-8, 9))))
        for lab, (cy, cx) in enumerate(centers[:50], start=1):
            nuclei[_disk(shape, cy, cx, int(rng.integers(5, 9)))] = lab

        yy, xx = np.indices(shape)
        channels = []
        for ch_idx in range(3):
            arr = rng.normal(0.01, 0.003, shape).astype(np.float32)
            for lab, (cy, cx) in enumerate(centers[:50], start=1):
                if lab % 3 != ch_idx:
                    continue
                region = ((yy - cy) / 15.0) ** 2 + ((xx - cx) / 22.0) ** 2 <= 1.0
                arr[region] += 0.8
            channels.append(np.clip(arr, 0.0, 1.0).astype(np.float32))

        params = {
            "hq_channels": ["M1", "M2", "M3"],
            "max_cell_radius": 18,
            "normalization_percentile_low": 1.0,
            "normalization_percentile_high": 99.5,
            "min_signal_threshold": 0.05,
            "signal_map_mode": "per_cell_best_channel",
            "hq2_expansion_engine": "conservative",
            "max_refine_radius": 6,
            "max_candidate_pixels_per_cell": 3000,
            "timeout_seconds": 30,
        }

        started = time.perf_counter()
        result = run_hq2_segmentation(nuclei, channels, ["M1", "M2", "M3"], params)
        elapsed = time.perf_counter() - started

        final = result["final_labels"]
        self.assertLess(elapsed, 30.0)
        self.assertEqual(final.shape, shape)
        self.assertGreaterEqual(int(final.max()), 45)
        self.assertTrue(result.get("metadata", {}).get("timings"))

if __name__ == "__main__":
    unittest.main()
