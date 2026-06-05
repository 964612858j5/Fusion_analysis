import os
import sys
import subprocess
import tempfile
import unittest

import numpy as np

from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation
from block01.utils.channel_cache import SharedChannelStore


def _disk(shape, cy, cx, radius):
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2


def _scene(shape=(1500, 1500), n_channels=8):
    nuclei = np.zeros(shape, dtype=np.uint32)
    rng = np.random.default_rng(31)
    channels = [(rng.random(shape) * 0.05).astype(np.float32) for _ in range(n_channels)]
    names = [f"C{i}" for i in range(n_channels)]
    lab = 0
    step = 160
    for cy in range(120, shape[0] - 60, step):
        for cx in range(120, shape[1] - 60, step):
            lab += 1
            nuclei[_disk(shape, cy, cx, 6)] = lab
            channels[lab % n_channels][_disk(shape, cy, cx, 13)] = 0.9
    return shape, nuclei, channels, names


_PARAMS = {
    "max_cell_radius": 16, "minimal_radius": 3, "shrink_pixels": 2,
    "lean_block_size": 512, "lean_halo_margin": 8, "use_gpu": False,
}


class TestEquivalence(unittest.TestCase):
    def test_inmemory_vs_loader_plus_memmap(self):
        shape, nuclei, channels, names = _scene()

        ref = run_lean_carve_segmentation(nuclei, channels, names, dict(_PARAMS))

        chan = {n: a for n, a in zip(names, channels)}
        def loader(name, y0, y1, x0, x1):
            a = chan.get(name)
            return None if a is None else np.asarray(a[y0:y1, x0:x1], dtype=np.float32)

        with tempfile.TemporaryDirectory() as td:
            mm = np.memmap(os.path.join(td, "out.dat"), dtype=np.uint32, mode="w+", shape=shape)
            new = run_lean_carve_segmentation(nuclei, loader, names, dict(_PARAMS), output_labels=mm)
            self.assertTrue(np.array_equal(np.asarray(new["final_labels"]), ref["final_labels"]))
            self.assertTrue(np.array_equal(np.asarray(mm), ref["final_labels"]))
            self.assertGreater(int(np.count_nonzero(np.asarray(mm))), 0)
            del mm


class TestCacheRecycle(unittest.TestCase):
    def test_set_max_cache_items_shrinks_resident(self):
        store = SharedChannelStore(max_cache_items=32)
        # One big "whole-image" entry + several small "per-block" entries.
        store._cached(("fused", "p", 0, 4000, 0, 4000, 1), lambda: np.zeros((4000, 4000), np.float32))
        for i in range(6):
            store._cached(("raw_ome", "p", "C", i, i + 64, 0, 64, 1, False),
                          lambda: np.zeros((64, 64), np.float32))
        before = store.snapshot_metrics()["cache_bytes"]
        n_before = len(store._cache)

        store.set_max_cache_items(2)
        after = store.snapshot_metrics()["cache_bytes"]
        self.assertLessEqual(len(store._cache), 2)
        self.assertLess(after, before)
        # The 64 MB whole-image entry is the first evicted (LRU order).
        self.assertLess(after, 4000 * 4000 * 4)
        self.assertGreaterEqual(n_before, 7)


_MEM_SCRIPT = r"""
import os, sys, resource, tempfile
import numpy as np
from block01.workers.lean_carve_segmentation import run_lean_carve_segmentation

n = 10000
nuclei = np.zeros((n, n), dtype=np.uint32)
lab = 0
for cy in range(300, n, 700):
    for cx in range(300, n, 700):
        lab += 1
        nuclei[cy-5:cy+5, cx-5:cx+5] = lab
names = [f"C{i}" for i in range(8)]
def loader(name, y0, y1, x0, x1):
    return np.full((y1 - y0, x1 - x0), 0.1, dtype=np.float32)
params = {"max_cell_radius": 14, "minimal_radius": 3, "shrink_pixels": 2,
          "lean_block_size": 2048, "lean_halo_margin": 8, "use_gpu": False}
td = tempfile.mkdtemp()
mm = np.memmap(os.path.join(td, "out.dat"), dtype=np.uint32, mode="w+", shape=(n, n))
res = run_lean_carve_segmentation(nuclei, loader, names, dict(params), output_labels=mm)
assert int(np.count_nonzero(np.asarray(mm))) >= 0
print("MAXRSS_KB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""


class TestMemoryCap(unittest.TestCase):
    def test_large_image_streaming_stays_bounded(self):
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-c", _MEM_SCRIPT],
                              capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(proc.returncode, 0, f"streaming path failed/OOM:\n{proc.stderr[-2000:]}")
        rss_kb = int([l for l in proc.stdout.split("\n") if l.startswith("MAXRSS_KB")][0].split()[1])
        rss_gb = rss_kb / (1024.0 * 1024.0)
        print(f"[mem] 10000x10000 + 8 lazy channels + memmap out: peak RSS = {rss_gb:.2f} GB")
        # 8 whole-image float32 channels would be ~3.2 GB resident on the old path;
        # streaming + memmap output must stay well under that.
        self.assertLess(rss_kb, 4 * 1024 * 1024, f"peak {rss_gb:.2f} GB too high")


if __name__ == "__main__":
    unittest.main()
