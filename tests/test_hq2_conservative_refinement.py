import time
import unittest

import numpy as np

from block01.workers.hq2_marker_segmentation import run_conservative_refinement


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


class TestHQ2ConservativeRefinement(unittest.TestCase):
    def test_small_continuous_extension_is_added_locally(self):
        shape = (128, 128)
        nuclei = np.zeros(shape, dtype=np.uint32)
        initial = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 64, 64, 5)] = 1
        initial[_disk(shape, 64, 64, 10)] = 1

        signal = np.zeros(shape, dtype=np.float32) + 0.02
        initial_mask = initial == 1
        extension = _disk(shape, 64, 64, 13) & ~initial_mask & (np.indices(shape)[1] >= 64)
        signal[initial_mask] = 0.7
        signal[extension] = 0.8
        norm = {"M1": signal}
        params = {
            "enable_refinement": True,
            "hq2_expansion_engine": "conservative",
            "max_cell_radius": 20,
            "max_refine_radius": 4,
            "min_refine_signal": 0.08,
            "refine_signal_mad_factor": 1.5,
            "max_added_area_fraction": 0.35,
            "max_cell_to_nucleus_ratio": 10,
            "max_candidate_pixels_per_cell": 3000,
        }

        started = time.perf_counter()
        final, added, info = run_conservative_refinement(
            initial, nuclei, signal, norm, ["M1"], params, total_started=started
        )
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 5.0)
        self.assertEqual(final.shape, shape)
        self.assertGreaterEqual(int(np.count_nonzero(final == 1)), int(np.count_nonzero(initial == 1)))
        self.assertGreater(int(np.count_nonzero(added == 1)), 0)
        self.assertLessEqual(
            int(np.count_nonzero(added == 1)),
            int(np.count_nonzero(initial == 1) * params["max_added_area_fraction"]),
        )
        from scipy import ndimage as ndi
        dist = ndi.distance_transform_edt(initial != 1)
        self.assertTrue(np.all(dist[added == 1] <= params["max_refine_radius"] + 0.01))
        self.assertEqual(int(final.max()), 1)
        self.assertGreaterEqual(info["summary"]["cells_refined"], 1)

    def test_overexpanded_refinement_falls_back_to_initial_cell(self):
        shape = (128, 128)
        nuclei = np.zeros(shape, dtype=np.uint32)
        initial = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 64, 64, 5)] = 1
        initial[_disk(shape, 64, 64, 8)] = 1
        signal = np.ones(shape, dtype=np.float32)
        norm = {"M1": signal}
        params = {
            "enable_refinement": True,
            "hq2_expansion_engine": "conservative",
            "max_cell_radius": 30,
            "max_refine_radius": 6,
            "min_refine_signal": 0.01,
            "refine_signal_mad_factor": 0.0,
            "max_added_area_fraction": 0.01,
            "max_cell_to_nucleus_ratio": 10,
            "fallback_to_hq_if_overexpanded": True,
        }

        final, added, info = run_conservative_refinement(
            initial, nuclei, signal, norm, ["M1"], params, total_started=time.perf_counter()
        )

        self.assertTrue(np.array_equal(final, initial))
        self.assertEqual(int(np.count_nonzero(added)), 0)
        self.assertEqual(info["summary"]["overexpanded_cell_count"], 1)
        self.assertIn("overexpanded_refinement_fallback", info["rows"][0]["low_confidence_reason"])


if __name__ == "__main__":
    unittest.main()
