import unittest

import numpy as np

from block01.workers.hq2_marker_segmentation import run_hq2_segmentation


OFFSET = 2_000_000_000  # globally-offset tile ids that used to drive bincount/find_objects OOM


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


def _make_scene():
    """256x256 crop, ~49 nuclei on a grid, two marker channels with blobs."""
    shape = (256, 256)
    nuclei = np.zeros(shape, dtype=np.uint64)
    lab = 0
    centers = []
    for cy in range(28, 256, 32):
        for cx in range(28, 256, 32):
            lab += 1
            nuclei[_disk(shape, cy, cx, 5)] = lab
            centers.append((cy, cx))

    rng = np.random.default_rng(23)
    ch1 = (rng.random(shape) * 0.05).astype(np.float32)
    ch2 = (rng.random(shape) * 0.05).astype(np.float32)
    for i, (cy, cx) in enumerate(centers):
        if i % 2 == 0:
            ch1[_disk(shape, cy, cx, 10)] = 0.9
        else:
            ch2[_disk(shape, cy, cx, 8)] = 0.8
    return shape, nuclei, [ch1, ch2], ["CD68", "CD45"]


_PARAMS = {
    "hq_channels": ["CD68", "CD45"],
    "max_cell_radius": 14,
    "min_signal_threshold": 0.05,
    "signal_map_mode": "per_cell_best_channel",
    "hq2_expansion_engine": "conservative",
    "max_refine_radius": 5,
    "min_refine_signal": 0.05,
    "use_gpu": False,
}


class TestHQ2LargeLabelIds(unittest.TestCase):
    def test_offset_ids_do_not_oom_and_match_dense(self):
        shape, nuclei, channels, names = _make_scene()
        offset_nuclei = nuclei.copy()
        offset_nuclei[offset_nuclei > 0] += OFFSET

        dense = run_hq2_segmentation(nuclei, channels, names, dict(_PARAMS))
        # Pre-fix this raised MemoryError (bincount/find_objects allocate by max id).
        try:
            offset = run_hq2_segmentation(offset_nuclei, channels, names, dict(_PARAMS))
        except MemoryError as exc:  # pragma: no cover - regression tripwire
            self.fail(f"offset ids caused MemoryError: {exc}")

        dense_final = np.asarray(dense["final_labels"], dtype=np.int64)
        offset_final = np.asarray(offset["final_labels"], dtype=np.int64)
        self.assertGreater(int(np.count_nonzero(offset_final)), 0)

        # Result is independent of id values: offset labels minus the offset match.
        remapped = np.where(offset_final > 0, offset_final - OFFSET, 0)
        self.assertTrue(np.array_equal(remapped, dense_final))

        # QC cell_ids carry the original (offset) ids back to the caller.
        offset_ids = {int(r["cell_id"]) for r in offset["qc_rows"]}
        dense_ids = {int(r["cell_id"]) for r in dense["qc_rows"]}
        self.assertEqual(offset_ids, {i + OFFSET for i in dense_ids})


if __name__ == "__main__":
    unittest.main()
