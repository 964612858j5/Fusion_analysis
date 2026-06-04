import unittest

import numpy as np

from block01.workers.constrained_donut_segmentation import (
    run_constrained_donut_segmentation,
)


OFFSET = 2_000_000_000  # globally-offset tile ids that used to drive find_objects/bincount OOM


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


def _make_scene():
    """256x256 crop, ~50 nuclei on a grid, a couple of marker channels."""
    shape = (256, 256)
    nuclei = np.zeros(shape, dtype=np.uint64)
    rng = np.random.default_rng(17)
    lab = 0
    centers = []
    for cy in range(28, 256, 32):
        for cx in range(28, 256, 32):
            lab += 1
            nuclei[_disk(shape, cy, cx, 5)] = lab
            centers.append((cy, cx))

    ch1 = rng.normal(20.0, 2.0, size=shape).astype(np.float32)
    ch2 = rng.normal(15.0, 1.5, size=shape).astype(np.float32)
    # Bright cytoplasmic blobs around a subset of cells so segmentation does real work.
    for i, (cy, cx) in enumerate(centers):
        if i % 2 == 0:
            ch1[_disk(shape, cy, cx, 11)] = 120.0
        else:
            ch2[_disk(shape, cy, cx, 9)] = 90.0
    return shape, nuclei, [ch1, ch2], ["PanCK", "CD45"]


class TestLargeLabelIds(unittest.TestCase):
    def _run(self, nuclei, channels, names, engine):
        return run_constrained_donut_segmentation(
            nuclei,
            channels,
            names,
            {
                "donut_size": 14,
                "max_cell_radius": 14,
                "minimal_radius": 3,
                "shrink_pixels": 0,
                "use_gpu": False,
                "cytoplasm_engine": engine,
            },
        )

    def _check_engine(self, engine):
        shape, nuclei, channels, names = _make_scene()
        offset_nuclei = nuclei.copy()
        offset_nuclei[offset_nuclei > 0] += OFFSET

        dense = self._run(nuclei, channels, names, engine)
        # Pre-fix this raised MemoryError (find_objects/bincount allocate by max id).
        try:
            offset = self._run(offset_nuclei, channels, names, engine)
        except MemoryError as exc:  # pragma: no cover - regression tripwire
            self.fail(f"{engine}: offset ids caused MemoryError: {exc}")

        dense_final = np.asarray(dense["final_labels"], dtype=np.int64)
        offset_final = np.asarray(offset["final_labels"], dtype=np.int64)
        self.assertGreater(int(np.count_nonzero(offset_final)), 0)

        # Result is independent of the id values: offset labels minus the offset
        # must match the dense-id labels pixel for pixel.
        remapped = np.where(offset_final > 0, offset_final - OFFSET, 0)
        self.assertTrue(np.array_equal(remapped, dense_final))

    def test_outside_in_engine_offset_ids(self):
        self._check_engine("outside_in")

    def test_flood_fill_engine_offset_ids(self):
        self._check_engine("flood_fill")


if __name__ == "__main__":
    unittest.main()
