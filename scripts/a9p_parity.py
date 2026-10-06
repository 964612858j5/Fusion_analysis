"""Block A9-P real-data oracle: the viewers' new reader (through PixelSource)
against the old RawTileProvider, random regions on every level.

    python -m block01.scripts.a9p_parity SLIDE [--n 200] [--seed 0]
"""

import argparse
import json
import time

import numpy as np

from block01.viewer.raw_tile_provider import RawTileProvider
from block01.viewer.source_tile_provider import open_viewer_source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slide")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    old, new = RawTileProvider(a.slide), open_viewer_source(a.slide)
    out = {"slide": a.slide, "levels": [], "mismatches": 0, "compared": 0}
    t0 = time.time()
    try:
        names = old.channel_names
        for level in range(old.num_levels):
            h, w = old.level_shape(level)
            side = max(16, min(h, w) // 8)
            n_bad = 0
            for _ in range(a.n):
                ch = names[int(rng.integers(len(names)))]
                y0 = int(rng.integers(-side // 4, h))
                x0 = int(rng.integers(-side // 4, w))
                r = (y0, y0 + int(rng.integers(1, side)), x0, x0 + int(rng.integers(1, side)))
                aa, ao = old.read_region(ch, level, *r)
                bb, bo = new.read_region(ch, level, *r)
                same = ao == bo and aa.dtype == bb.dtype and np.array_equal(aa, bb)
                n_bad += not same
                out["compared"] += 1
            out["levels"].append({"level": level, "shape": [h, w], "mismatches": n_bad})
            out["mismatches"] += n_bad
    finally:
        old.close()
        new.close()
    out["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
