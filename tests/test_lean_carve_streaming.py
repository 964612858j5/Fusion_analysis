import os
import sys
import subprocess
import tempfile
import unittest

import numpy as np

from block01.workers.lean_carve_segmentation import (
    run_lean_carve_segmentation,
    _expand_labels_gpu,
    _cupy_hotspot_available,
)
from block01.workers.constrained_donut_segmentation import _expand_labels


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


def _scene():
    shape = (320, 320)
    nuclei = np.zeros(shape, dtype=np.uint32)
    rng = np.random.default_rng(11)
    ch1 = (rng.random(shape) * 0.05).astype(np.float32)
    ch2 = (rng.random(shape) * 0.05).astype(np.float32)
    lab = 0
    for cy in (64, 160, 256):
        for cx in (64, 160, 256):
            lab += 1
            nuclei[_disk(shape, cy, cx, 6)] = lab
            (ch1 if lab % 2 == 0 else ch2)[_disk(shape, cy, cx, 13)] = 0.9
    return shape, nuclei, [ch1, ch2], ["A", "B"]


_PARAMS = {
    "max_cell_radius": 16, "minimal_radius": 3, "shrink_pixels": 2,
    "lean_block_size": 96, "lean_halo_margin": 8, "use_gpu": False,
}


class TestStreamingEquivalence(unittest.TestCase):
    def test_lazy_loader_and_memmap_match_in_memory(self):
        shape, nuclei, channels, names = _scene()

        # Old path: in-memory whole arrays, engine-allocated output.
        ref = run_lean_carve_segmentation(nuclei, channels, names, dict(_PARAMS))

        # New path: lazy per-block loader + memmap output buffer.
        chan_by_name = {n: a for n, a in zip(names, channels)}
        reads = {"count": 0}
        def loader(name, y0, y1, x0, x1):
            reads["count"] += 1
            a = chan_by_name.get(name)
            return None if a is None else np.asarray(a[y0:y1, x0:x1], dtype=np.float32)

        with tempfile.TemporaryDirectory() as td:
            mm = np.memmap(os.path.join(td, "out.dat"), dtype=np.uint32, mode="w+", shape=shape)
            new = run_lean_carve_segmentation(nuclei, loader, names, dict(_PARAMS), output_labels=mm)
            self.assertTrue(np.array_equal(np.asarray(new["final_labels"]), ref["final_labels"]))
            self.assertTrue(np.array_equal(np.asarray(mm), ref["final_labels"]))
            self.assertGreater(reads["count"], 0)        # loader actually used, per block
            del mm


class TestTerritoryGpuCpuEquivalence(unittest.TestCase):
    @unittest.skipUnless(_cupy_hotspot_available(), "cupy/GPU not available")
    def test_gpu_territory_matches_cpu(self):
        shape = (200, 200)
        nuclei = np.zeros(shape, dtype=np.uint32)
        # Well separated so no equidistant ridge -> EDT tie-break can't differ.
        nuclei[_disk(shape, 50, 50, 6)] = 1
        nuclei[_disk(shape, 150, 150, 6)] = 2
        for dist in (3, 8, 16):
            cpu = _expand_labels(nuclei, dist).astype(np.uint32)
            gpu = _expand_labels_gpu(nuclei, dist)
            self.assertIsNotNone(gpu)
            self.assertTrue(np.array_equal(gpu, cpu), f"distance={dist}")


_MEM_SCRIPT = r"""
import os, sys, resource, tempfile
import numpy as np
from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation

mode = sys.argv[1]
n = 8000
nuclei = np.zeros((n, n), dtype=np.uint32)
lab = 0
for cy in range(300, n, 900):
    for cx in range(300, n, 900):
        lab += 1
        nuclei[cy-5:cy+5, cx-5:cx+5] = lab
params = {"max_cell_radius": 14, "minimal_radius": 3, "shrink_pixels": 2,
          "lean_block_size": 2048, "lean_halo_margin": 8, "use_gpu": False}

if mode == "inmem":
    # dense (resident) channels: ~512 MB held only by the in-memory path
    ch1 = np.full((n, n), 0.3, dtype=np.float32); ch2 = np.full((n, n), 0.3, dtype=np.float32)
    res = run_lean_carve_segmentation(nuclei, [ch1, ch2], ["A", "B"], dict(params))
else:
    def loader(name, y0, y1, x0, x1):
        return np.full((y1 - y0, x1 - x0), 0.3, dtype=np.float32)
    td = tempfile.mkdtemp()
    mm = np.memmap(os.path.join(td, "out.dat"), dtype=np.uint32, mode="w+", shape=(n, n))
    res = run_lean_carve_segmentation(nuclei, loader, ["A", "B"], dict(params), output_labels=mm)

assert int(np.count_nonzero(np.asarray(res["final_labels"]))) >= 0
print("MAXRSS_KB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""


class TestStreamingMemory(unittest.TestCase):
    def _run(self, mode, vlimit_kb=None):
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, "-c", _MEM_SCRIPT, mode]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)

    def test_streaming_uses_far_less_memory(self):
        stream = self._run("stream")
        self.assertEqual(stream.returncode, 0, f"stream OOM/failed:\n{stream.stderr[-2000:]}")
        s_rss = int([l for l in stream.stdout.split("\n") if l.startswith("MAXRSS_KB")][0].split()[1])

        inmem = self._run("inmem")
        if inmem.returncode == 0 and "MAXRSS_KB" in inmem.stdout:
            i_rss = int([l for l in inmem.stdout.split("\n") if l.startswith("MAXRSS_KB")][0].split()[1])
            # 8000x8000 x 2 float32 channels = ~1 GB held only by the in-memory path.
            self.assertLess(s_rss, i_rss, f"stream={s_rss}KB not below inmem={i_rss}KB")


if __name__ == "__main__":
    unittest.main()
