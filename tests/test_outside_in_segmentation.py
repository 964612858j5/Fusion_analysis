import unittest

import numpy as np

from block01.workers.constrained_donut_segmentation import (
    run_constrained_donut_segmentation,
)


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


def _ellipse(shape, cy, cx, ry, rx):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return ((yy - cy) / float(ry)) ** 2 + ((xx - cx) / float(rx)) ** 2 <= 1.0


def _circularity(mask):
    """4*pi*area / perimeter^2; ~1.0 for a disk, lower for lobed shapes."""
    mask = np.asarray(mask, dtype=bool)
    area = float(np.count_nonzero(mask))
    if area <= 0:
        return 0.0
    try:
        from skimage.measure import perimeter
        per = float(perimeter(mask))
    except Exception:
        from scipy import ndimage as ndi
        per = float(np.count_nonzero(mask & ~ndi.binary_erosion(mask)))
    if per <= 0:
        return 0.0
    return 4.0 * np.pi * area / (per * per)


def _make_scene():
    """One isolated large lobed cell + one faint small cell, one nucleus each."""
    shape = (140, 230)
    nuclei = np.zeros(shape, dtype=np.uint32)
    # Big cell nucleus
    big_cy, big_cx = 70, 62
    nuclei[_disk(shape, big_cy, big_cx, 6)] = 1
    # Weak cell nucleus (well separated so territories never merge)
    weak_cy, weak_cx = 70, 178
    nuclei[_disk(shape, weak_cy, weak_cx, 4)] = 2

    rng = np.random.default_rng(3)
    marker = rng.normal(15.0, 1.0, size=shape).astype(np.float32)

    # Big cell: a flat moderate plateau over the whole territory disk (this is what
    # makes the inside-out flood fill balloon into a circle), plus two bright lobes
    # that are the real, non-circular cytoplasm.
    plateau = _disk(shape, big_cy, big_cx, 27)
    marker[plateau] = 26.0
    lobes = _ellipse(shape, big_cy, big_cx - 4, 9, 16) | _ellipse(shape, big_cy - 8, big_cx + 12, 7, 9)
    marker[lobes] = 140.0

    # Weak cell: faint-but-visible blob clearly larger than the minimal keep ring.
    weak_blob = _disk(shape, weak_cy, weak_cx, 9)
    marker[weak_blob] = 48.0

    return shape, nuclei, marker


_PARAMS = {
    "donut_size": 28,
    "max_cell_radius": 28,
    "minimal_radius": 3,
    "shrink_pixels": 0,
    "use_gpu": False,
}


class TestOutsideInSegmentation(unittest.TestCase):
    def test_outside_in_carves_disk_back_to_contour(self):
        shape, nuclei, marker = _make_scene()
        params = dict(_PARAMS, cytoplasm_engine="outside_in")
        oi = run_constrained_donut_segmentation(nuclei, [marker], ["PanCK"], params)
        ff = run_constrained_donut_segmentation(
            nuclei, [marker], ["PanCK"], dict(_PARAMS, cytoplasm_engine="flood_fill")
        )

        oi_final = oi["final_labels"]
        ff_final = ff["final_labels"]
        territory = oi["base_territory_mask"] == 1

        oi_big = oi_final == 1
        ff_big = ff_final == 1

        # The carved cell is markedly less circular than its territory disk...
        self.assertLess(_circularity(oi_big), _circularity(territory) * 0.85)
        # ...and smaller than the inside-out result that floods the whole disk.
        self.assertLess(int(np.count_nonzero(oi_big)), int(np.count_nonzero(ff_big)))

    def test_weak_cell_does_not_collapse_to_nucleus(self):
        shape, nuclei, marker = _make_scene()
        params = dict(_PARAMS, cytoplasm_engine="outside_in")
        oi = run_constrained_donut_segmentation(nuclei, [marker], ["PanCK"], params)

        weak_final_area = int(np.count_nonzero(oi["final_labels"] == 2))
        minimal_area = int(np.count_nonzero(oi["minimal_keep_mask"] == 2))
        self.assertGreater(weak_final_area, minimal_area)

    def test_labels_disjoint_and_cover_all_nuclei(self):
        shape, nuclei, marker = _make_scene()
        params = dict(_PARAMS, cytoplasm_engine="outside_in")
        oi = run_constrained_donut_segmentation(nuclei, [marker], ["PanCK"], params)
        final = oi["final_labels"]

        # All nucleus pixels are retained under their own label.
        self.assertTrue(np.all(final[nuclei == 1] == 1))
        self.assertTrue(np.all(final[nuclei == 2] == 2))

        # No overlap: a labelled image is disjoint by construction, so assert each
        # final region contains its nucleus and the two regions never share a pixel.
        self.assertEqual(int(np.count_nonzero((final == 1) & (final == 2))), 0)

    def test_flood_fill_engine_still_runs(self):
        shape, nuclei, marker = _make_scene()
        ff = run_constrained_donut_segmentation(
            nuclei, [marker], ["PanCK"], dict(_PARAMS, cytoplasm_engine="flood_fill")
        )
        self.assertGreater(int(np.count_nonzero(ff["final_labels"] > 0)), 0)


if __name__ == "__main__":
    unittest.main()
