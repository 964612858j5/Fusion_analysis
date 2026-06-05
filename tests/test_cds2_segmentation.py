import os
import sys
import subprocess
import unittest

import numpy as np

from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
from block01.workers.cds2_segmentation import run_cds2_segmentation
from block01.workers.constrained_donut_segmentation import _expand_labels


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


_BASE = {
    "max_cell_radius": 30, "minimal_radius": 3, "shrink_pixels": 0,
    "lean_block_size": 4096, "lean_halo_margin": 8, "use_gpu": False,
    "outside_in_z_threshold": 0.5,
}


def _epi_macro_scene():
    """EPI nucleus + HsBAg body; MACRO nucleus + CD68 body; a CD68 tail sitting
    inside the EPI territory (the thing lean_carve over-claims)."""
    shape = (120, 160)
    nuclei = np.zeros(shape, dtype=np.uint32)
    nuclei[_disk(shape, 60, 40, 5)] = 1      # EPI
    nuclei[_disk(shape, 60, 116, 5)] = 2     # MACRO
    hsbag = np.zeros(shape, dtype=np.float32)
    cd68 = np.zeros(shape, dtype=np.float32)
    hsbag[_disk(shape, 60, 40, 14)] = 1.0    # EPI body
    cd68[_disk(shape, 60, 116, 14)] = 1.0    # MACRO body
    cd68[58:63, 54:72] = 1.0                 # MACRO tail reaching into EPI territory
    names = ["HsBAg", "CD68"]
    return shape, nuclei, [hsbag, cd68], names, hsbag, cd68


class TestDegradeEquivalence(unittest.TestCase):
    def test_no_fingerprint_matches_lean(self):
        shape = (160, 160)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 50, 50, 6)] = 1
        nuclei[_disk(shape, 110, 100, 6)] = 2
        rng = np.random.default_rng(3)
        # All HQ channels near background -> no camp ever passes tau -> no fingerprint.
        channels = [(rng.random(shape) * 0.02).astype(np.float32) for _ in range(8)]
        names = ["HsBAg", "CK19", "CD68", "CD163", "CD3D", "CD4", "CD8", "DAPIish"]
        lean = run_lean_carve_segmentation(nuclei, channels, names, dict(_BASE))
        cds2 = run_cds2_segmentation(nuclei, channels, names, dict(_BASE))
        self.assertTrue(np.array_equal(cds2["final_labels"], lean["final_labels"]))


class TestSpitOut(unittest.TestCase):
    def test_epi_does_not_eat_macro_tail(self):
        shape, nuclei, channels, names, hsbag, cd68 = _epi_macro_scene()
        cd68_only = (cd68 >= 0.5) & (hsbag < 0.1)

        lean = run_lean_carve_segmentation(nuclei, channels, names, dict(_BASE))["final_labels"]
        cds2 = run_cds2_segmentation(nuclei, channels, names, dict(_BASE))["final_labels"]

        lean_epi_cd68 = int(np.count_nonzero((lean == 1) & cd68_only))
        cds2_epi_cd68 = int(np.count_nonzero((cds2 == 1) & cd68_only))
        self.assertGreater(lean_epi_cd68, 30)               # lean swallows the tail
        self.assertLess(cds2_epi_cd68, lean_epi_cd68 * 0.2)  # cds2 spits it out

    def test_real_macro_keeps_its_body(self):
        shape, nuclei, channels, names, hsbag, cd68 = _epi_macro_scene()
        lean = run_lean_carve_segmentation(nuclei, channels, names, dict(_BASE))["final_labels"]
        cds2 = run_cds2_segmentation(nuclei, channels, names, dict(_BASE))["final_labels"]
        a2_lean = int(np.count_nonzero(lean == 2))
        a2_cds2 = int(np.count_nonzero(cds2 == 2))
        self.assertGreater(a2_cds2, 0)
        self.assertGreaterEqual(a2_cds2, a2_lean * 0.9)      # MACRO cell not collateral-shrunk


class TestSafetyValve(unittest.TestCase):
    def test_small_high_intensity_contamination_filtered(self):
        shape = (120, 120)
        nuclei = np.zeros(shape, dtype=np.uint32)
        nuclei[_disk(shape, 60, 60, 6)] = 1
        hsbag = np.zeros(shape, dtype=np.float32)
        cd68 = np.zeros(shape, dtype=np.float32)
        hsbag[_disk(shape, 60, 60, 16)] = 1.0               # EPI body around nucleus
        # tiny but intense CD68 arc touching < 20% of the ring
        cd68[54:58, 70:74] = 2.0
        names = ["HsBAg", "CD68"]
        cds2 = run_cds2_segmentation(nuclei, [hsbag, cd68], names, dict(_BASE))["final_labels"]
        minimal_area = int(np.count_nonzero(_expand_labels(nuclei, 3) == 1))
        epi_area = int(np.count_nonzero(cds2 == 1))
        # If the arc had wrongly become the fingerprint, the EPI body would be vetoed
        # and collapse to ~minimal. It must keep its real HsBAg body.
        self.assertGreater(epi_area, minimal_area * 1.5)


_MEM_SCRIPT = r"""
import os, sys, resource, tempfile
import numpy as np
from block01.workers.cds2_segmentation import run_cds2_segmentation

n = 6000
nuclei = np.zeros((n, n), dtype=np.uint32)
lab = 0
for cy in range(300, n, 700):
    for cx in range(300, n, 700):
        lab += 1
        nuclei[cy-5:cy+5, cx-5:cx+5] = lab
names = ["HsBAg", "CD68", "CD163", "CD3D"]
def loader(name, y0, y1, x0, x1):
    return np.full((y1 - y0, x1 - x0), 0.1, dtype=np.float32)
params = {"max_cell_radius": 14, "minimal_radius": 3, "shrink_pixels": 2,
          "lean_block_size": 2048, "lean_halo_margin": 8, "use_gpu": False}
td = tempfile.mkdtemp()
mm = np.memmap(os.path.join(td, "out.dat"), dtype=np.uint32, mode="w+", shape=(n, n))
res = run_cds2_segmentation(nuclei, loader, names, dict(params), output_labels=mm)
assert int(np.count_nonzero(np.asarray(mm))) >= 0
print("MAXRSS_KB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""


class TestStreamingMemory(unittest.TestCase):
    def test_cds2_streaming_bounded(self):
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-c", _MEM_SCRIPT],
                              capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(proc.returncode, 0, f"cds2 failed/OOM:\n{proc.stderr[-2000:]}")
        rss_kb = int([l for l in proc.stdout.split("\n") if l.startswith("MAXRSS_KB")][0].split()[1])
        print(f"[mem] cds2 6000x6000 + 4 lazy channels + memmap: peak RSS = {rss_kb/1048576:.2f} GB")
        self.assertLess(rss_kb, 3 * 1024 * 1024)            # same magnitude as lean_carve


if __name__ == "__main__":
    unittest.main()
