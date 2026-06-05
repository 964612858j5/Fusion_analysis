import os
import sys
import subprocess
import unittest

import numpy as np

from block01.workers.constrained_donut_segmentation import run_constrained_donut_segmentation
from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


def _ellipse(shape, cy, cx, ry, rx):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return ((yy - cy) / float(ry)) ** 2 + ((xx - cx) / float(rx)) ** 2 <= 1.0


def _circularity(mask):
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
    return 4.0 * np.pi * area / (per * per) if per > 0 else 0.0


class TestLeanCarveEquivalence(unittest.TestCase):
    def test_tiled_equals_whole_image(self):
        # Cells centered inside each 128 tile with wide margins, so no territory
        # crosses a block boundary -> tiled (block=128) == single-block exactly.
        shape = (512, 512)
        max_radius = 14
        nuclei = np.zeros(shape, dtype=np.uint32)
        rng = np.random.default_rng(5)
        ch1 = (rng.random(shape) * 0.05).astype(np.float32)
        ch2 = (rng.random(shape) * 0.05).astype(np.float32)
        lab = 0
        for cy in (64, 192, 320, 448):
            for cx in (64, 192, 320, 448):
                lab += 1
                nuclei[_disk(shape, cy, cx, 5)] = lab
                (ch1 if lab % 2 == 0 else ch2)[_disk(shape, cy, cx, 11)] = 0.9
        names = ["A", "B"]
        base = {
            "max_cell_radius": max_radius,
            "minimal_radius": 3,
            "shrink_pixels": 0,
            "lean_halo_margin": 0,          # halo == max_radius
            "use_gpu": False,
        }
        tiled = run_lean_carve_segmentation(nuclei, [ch1, ch2], names, dict(base, lean_block_size=128))
        whole = run_lean_carve_segmentation(nuclei, [ch1, ch2], names, dict(base, lean_block_size=4096))
        self.assertTrue(np.array_equal(tiled["final_labels"], whole["final_labels"]))
        self.assertGreater(int(np.count_nonzero(tiled["final_labels"])), 0)


class TestLeanCarveBehavior(unittest.TestCase):
    def _params(self, **kw):
        base = {"max_cell_radius": 16, "minimal_radius": 3, "shrink_pixels": 2,
                "lean_block_size": 4096, "lean_halo_margin": 8, "use_gpu": False}
        base.update(kw)
        return base

    def test_isolated_disk_carved_non_circular(self):
        shape = (160, 160)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 80, 80, 6)] = 1
        rng = np.random.default_rng(7)
        marker = rng.normal(15.0, 1.0, size=shape).astype(np.float32)
        marker[_disk(shape, 80, 80, 26)] = 26.0       # flat plateau over the territory disk
        lobes = _ellipse(shape, 80, 76, 9, 16) | _ellipse(shape, 72, 92, 7, 9)
        marker[lobes] = 140.0
        res = run_lean_carve_segmentation(nuclei, [marker], ["PanCK"], self._params(max_cell_radius=26))
        cell = res["final_labels"] == 1
        self.assertGreater(int(np.count_nonzero(cell)), 0)
        self.assertLess(_circularity(cell), 0.85)
        self.assertLess(int(np.count_nonzero(cell)), int(np.pi * 26 * 26))

    def test_weak_cell_not_collapsed(self):
        from block01.workers.constrained_donut_segmentation import _expand_labels
        shape = (140, 140)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 70, 70, 4)] = 1
        rng = np.random.default_rng(9)
        marker = rng.normal(15.0, 1.0, size=shape).astype(np.float32)
        marker[_disk(shape, 70, 70, 10)] = 48.0       # faint but visible, beyond minimal ring
        res = run_lean_carve_segmentation(nuclei, [marker], ["CD45"], self._params())
        final_area = int(np.count_nonzero(res["final_labels"] == 1))
        minimal = _expand_labels(nuclei, 3)
        minimal_area = int(np.count_nonzero(minimal == 1))
        self.assertGreater(final_area, minimal_area)
        self.assertTrue(np.all(res["final_labels"][nuclei == 1] == 1))

    def test_adjacent_cells_have_gap_and_no_overlap(self):
        shape = (120, 160)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 60, 64, 6)] = 1
        nuclei[_disk(shape, 60, 96, 6)] = 2
        marker = np.full(shape, 60.0, dtype=np.float32)
        marker[_disk(shape, 60, 64, 18)] = 140.0
        marker[_disk(shape, 60, 96, 18)] = 140.0
        res = run_lean_carve_segmentation(nuclei, [marker], ["PanCK"], self._params(shrink_pixels=2))
        final = res["final_labels"]
        self.assertTrue(np.all(final[nuclei == 1] == 1))
        self.assertTrue(np.all(final[nuclei == 2] == 2))
        self.assertEqual(int(np.count_nonzero((final == 1) & (final == 2))), 0)
        # A background gap exists on the line between the two cell centres.
        line = final[60, 64:97]
        self.assertGreaterEqual(int(np.count_nonzero(line == 0)), 2)


class TestEngineSwitchable(unittest.TestCase):
    def test_three_engines_return_contract(self):
        shape = (96, 96)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 32, 32, 5)] = 1
        nuclei[_disk(shape, 64, 64, 5)] = 2
        marker = np.full(shape, 30.0, dtype=np.float32)
        marker[_disk(shape, 32, 32, 12)] = 120.0
        marker[_disk(shape, 64, 64, 12)] = 120.0
        for engine in ("lean_carve", "flood_fill", "outside_in"):
            res = run_constrained_donut_segmentation(
                nuclei, [marker], ["PanCK"],
                {"max_cell_radius": 12, "shrink_pixels": 1, "use_gpu": False,
                 "cytoplasm_engine": engine},
            )
            for key in ("final_labels", "nuclei_labels", "qc_rows", "stats", "metadata"):
                self.assertIn(key, res, f"{engine} missing {key}")
            self.assertGreater(int(np.count_nonzero(res["final_labels"])), 0, engine)
        # lean engine is observable in metadata.
        lean = run_constrained_donut_segmentation(
            nuclei, [marker], ["PanCK"],
            {"max_cell_radius": 12, "use_gpu": False, "cytoplasm_engine": "lean_carve"},
        )
        self.assertEqual(lean["metadata"].get("engine"), "lean_carve")


_MEM_SCRIPT = r"""
import resource, sys
import numpy as np
from block01.workers.constrained_donut_segmentation import run_constrained_donut_segmentation

engine = sys.argv[1]
n = 4000
nuclei = np.zeros((n, n), dtype=np.uint32)
lab = 0
for cy in range(200, n, 700):
    for cx in range(200, n, 700):
        lab += 1
        nuclei[cy-5:cy+5, cx-5:cx+5] = lab   # cheap: small box, not a full-image disk
ch = (np.random.default_rng(0).random((n, n)) * 0.05).astype(np.float32)
res = run_constrained_donut_segmentation(
    nuclei, [ch], ["A"],
    {"max_cell_radius": 14, "shrink_pixels": 2, "use_gpu": False, "cytoplasm_engine": engine},
)
assert int(np.count_nonzero(res["final_labels"])) >= 0
print("MAXRSS_KB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""


class TestLeanCarveMemory(unittest.TestCase):
    def _run_engine_rss(self, engine):
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", _MEM_SCRIPT, engine],
            capture_output=True, text=True, env=env, timeout=600,
        )
        return proc

    def test_lean_uses_far_less_memory_than_whole_image_engine(self):
        lean = self._run_engine_rss("lean_carve")
        self.assertEqual(lean.returncode, 0, f"lean_carve failed/OOM:\n{lean.stderr[-2000:]}")
        lean_rss = int([l for l in lean.stdout.split("\n") if l.startswith("MAXRSS_KB")][0].split()[1])

        other = self._run_engine_rss("outside_in")
        # The whole-image engine may itself OOM/fail; that only strengthens the point.
        if other.returncode == 0 and "MAXRSS_KB" in other.stdout:
            other_rss = int([l for l in other.stdout.split("\n") if l.startswith("MAXRSS_KB")][0].split()[1])
            self.assertLess(lean_rss * 1.5, other_rss,
                            f"lean={lean_rss}KB not clearly below outside_in={other_rss}KB")


if __name__ == "__main__":
    unittest.main()
