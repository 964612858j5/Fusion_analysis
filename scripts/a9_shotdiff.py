"""§41: is the hand-stop -> sharp switch noticeable? For each gesture of a
stop-shot set: the share of pixels that still differ (> 8 grey levels on
any colour) from the final picture (the last shot) at each time after the
last input, and the first time it is below 1 %.
Usage: a9_shotdiff.py SHOTS_DIR [LABELS_COMMA]"""
import glob
import os
import re
import sys

import numpy as np
from PIL import Image


def main(d, labels=""):
    names = labels.split(",") if labels else []
    files = glob.glob(os.path.join(d, "g*_*ms.png"))
    by_g = {}
    for f in files:
        m = re.search(r"g(\d+)_(\d+)ms\.png$", f)
        by_g.setdefault(int(m.group(1)), []).append((int(m.group(2)), f))
    rows = []
    for g in sorted(by_g):
        shots = sorted(by_g[g])
        final = np.asarray(Image.open(shots[-1][1])).astype(np.int16)
        cells, under = [], None
        for ms, f in shots[:-1]:
            a = np.asarray(Image.open(f)).astype(np.int16)
            share = float((np.abs(a - final).max(axis=2) > 8).mean() * 100.0)
            cells.append(f"{ms:4d}:{share:5.1f}%")
            if under is None and share < 1.0:
                under = ms
        label = names[g - 1] if g - 1 < len(names) else f"g{g}"
        rows.append((label, under))
        print(f"{label[:14]:14s} " + " ".join(cells) + f" | <1% from {under if under is not None else '>' + str(shots[-2][0])} ms")


if __name__ == "__main__":
    main(*sys.argv[1:3])
