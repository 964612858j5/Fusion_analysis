"""§41/§44: is the hand-stop -> sharp switch noticeable? For each gesture of
a stop-shot set, every shot is compared with the gesture's reference (its
last shot): a pixel differs when any colour differs by more than 8 grey
levels.

Per shot it reports (§44 step 0, independent review):
  all    the share of differing pixels over the whole canvas;
  uns    the share over pixels NOT saturated in the reference (all colours
         < 250) -- with many channels the overlay goes white, and a missing
         channel there changes nothing visible to the global share;
  cell   the worst 64x64 cell's share -- a blurry seam can be 0.5 % of the
         canvas and still be seen.
and per gesture the SETTLE time: the first shot from which `all` stays
<= 1.2 % (and `uns`, `cell` <= their limits) for every later shot. Times
are the shots' real offsets from the last injection when shots.json has
them (the nominal delay otherwise).

Usage: a9_shotdiff.py SHOTS_DIR [LABELS_COMMA]   (writes SHOTS_DIR/diff.json)"""
import glob
import json
import os
import re
import sys

import numpy as np
from PIL import Image

LIMIT_ALL, LIMIT_UNS, LIMIT_CELL = 1.2, 1.2, 10.0
CELL = 64


def measure(a, ref):
    diff = np.abs(a - ref).max(axis=2) > 8
    uns = ref.max(axis=2) < 250
    h, w = diff.shape
    # partial cells at the right and bottom edges count too (their own size)
    ch, cw = -(-h // CELL), -(-w // CELL)
    pad = np.zeros((ch * CELL, cw * CELL), np.float64)
    inside = np.zeros_like(pad)
    pad[:h, :w] = diff
    inside[:h, :w] = 1.0
    cells = (pad.reshape(ch, CELL, cw, CELL).sum(axis=(1, 3))
             / inside.reshape(ch, CELL, cw, CELL).sum(axis=(1, 3)))
    return (float(diff.mean() * 100.0),
            float(diff[uns].mean() * 100.0) if uns.any() else 0.0,
            float(cells.max() * 100.0))


def main(d, labels=""):
    names = labels.split(",") if labels else []
    offsets = {}
    meta_path = os.path.join(d, "shots.json")
    if os.path.exists(meta_path):
        for shot in json.load(open(meta_path)):
            offsets[shot["file"]] = shot.get("from_last_ms")
    by_g = {}
    for f in glob.glob(os.path.join(d, "g*_*ms.png")):
        m = re.search(r"g(\d+)_(\d+)ms\.png$", f)
        by_g.setdefault(int(m.group(1)), []).append((int(m.group(2)), f))
    out = []
    for g in sorted(by_g):
        shots = sorted(by_g[g])
        ref = np.asarray(Image.open(shots[-1][1])).astype(np.int16)
        rows = []
        for ms, f in shots[:-1]:
            a = np.asarray(Image.open(f)).astype(np.int16)
            real = offsets.get(os.path.basename(f))
            rows.append({"ms": ms, "real_ms": real if real is not None else float(ms),
                         **dict(zip(("all", "uns", "cell"), measure(a, ref)))})
        settle = None
        for i in range(len(rows)):
            if all(r["all"] <= LIMIT_ALL and r["uns"] <= LIMIT_UNS and r["cell"] <= LIMIT_CELL
                   for r in rows[i:]):
                settle = rows[i]["real_ms"]
                break
        label = names[g - 1] if g - 1 < len(names) else f"g{g}"
        out.append({"g": g, "label": label, "settle_ms": settle, "shots": rows,
                    "reference": os.path.basename(shots[-1][1])})
        cells = " ".join(f"{r['real_ms']:5.0f}:{r['all']:4.1f}/{r['uns']:4.1f}/{r['cell']:3.0f}"
                         for r in rows)
        print(f"{label[:14]:14s} settle {'>' + str(rows[-1]['ms']) if settle is None else round(settle)} ms | {cells}")
    with open(os.path.join(d, "diff.json"), "w") as f:
        json.dump(out, f, indent=0)


if __name__ == "__main__":
    main(*sys.argv[1:3])
