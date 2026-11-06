"""Regions overlay: static-outline paint cost under a real pan/zoom stream.

    python scripts/benchmark_regions_overlay.py [--shapes 200] [--points 300]
                                                [--frames 40] [--spread 4000]
                                                [--json PATH]

What it measures
----------------
`RegionsOverlay.paintEvent` while the view moves one pixel per frame: the
static picture is re-rendered on every frame (that is what panning and
zooming do), and the dynamic layer is empty. The widget is a real
`QGraphicsView` + `pg.ViewBox`, so `canvas_transform()` takes the real path.

What it does NOT measure: the GUI round trip to the screen, tile reads, or
the Rust/GL picture. Those are separate numbers (see the block's report);
this is the outline-drawing cost alone, on the same data, viewport, DPR and
transform stream for every implementation being compared.

The script only builds its own widget and synthetic shapes; it never opens a
project.
"""

import argparse
import json
import os
import statistics
import sys
import time

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _cand in (os.path.join(_REPO, ".block01", "pkg"), os.path.dirname(_REPO)):
    if os.path.isdir(os.path.join(_cand, "block01")) and _cand not in sys.path:
        sys.path.insert(0, _cand)

from block01.qt import QtCore, QtGui, QtWidgets  # noqa: E402

import pyqtgraph as pg  # noqa: E402

from block01.ui.regions.overlay import RegionsOverlay  # noqa: E402


def _shapes(n, points, spread, seed=0):
    """n closed wiggly polygons with `points` vertices, spread over a canvas."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        cx, cy = rng.uniform(0, spread), rng.uniform(0, spread)
        r = rng.uniform(spread * 0.02, spread * 0.05)
        ang = np.linspace(0, 2 * np.pi, points, endpoint=False)
        wob = 1.0 + 0.15 * np.sin(ang * rng.integers(3, 7))
        poly = [(float(cx + r * w * np.cos(a)), float(cy + r * w * np.sin(a)))
                for a, w in zip(ang, wob)]
        out.append({"id": "%d" % (i + 1), "label": "R%d" % (i + 1),
                    "polygon": poly, "color": "#6bcb77",
                    "dash": bool(i % 3 == 0), "hatch": bool(i % 5 == 0)})
    return out


def run(args):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = pg.GraphicsView()
    view.resize(1200, 800)
    vb = pg.ViewBox()
    view.setCentralItem(vb)
    view.show()
    ov = RegionsOverlay(view, vb, None)
    shapes = _shapes(args.shapes, args.points, args.spread)

    # A viewport showing part of the canvas: shapes outside it are the culling
    # case; panning one pixel per frame invalidates the static picture.
    span = args.spread * args.span_frac
    vb.setRange(xRange=(0, span), yRange=(0, span * 0.66), padding=0)
    ov.set_items(shapes)
    app.processEvents()

    # `paintEvent` is bypassed on purpose: `QWidget.render()` hands the painter
    # a clip that need not cover the widget, and then the drawing is cheap
    # without being done. `_paint_static` is the same entry `paintEvent` uses,
    # on a plain pixmap, with the real transform of each frame.
    pm = QtGui.QPixmap(1200, 800)
    pm.fill(QtCore.Qt.transparent)
    times = []
    for i in range(args.frames):
        vb.setRange(xRange=(i, span + i), yRange=(i, span * 0.66 + i), padding=0)
        app.processEvents()
        tr = ov.canvas_transform()
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        t0 = time.perf_counter()
        ov._paint_static(p, tr)
        times.append((time.perf_counter() - t0) * 1000.0)
        p.end()

    times_sorted = sorted(times)
    # How many shapes the last frame actually had on screen (same transform
    # for every implementation: it is the view, not the painter).
    tr = ov.canvas_transform()
    area = QtCore.QRectF(0, 0, ov.width(), ov.height())
    visible = 0
    for shape in getattr(ov, "_shapes", ()):
        x0, y0, x1, y1 = shape.bbox
        xs = (tr.m11() * x0 + tr.m21() * y0 + tr.dx(),
              tr.m11() * x1 + tr.m21() * y0 + tr.dx(),
              tr.m11() * x1 + tr.m21() * y1 + tr.dx(),
              tr.m11() * x0 + tr.m21() * y1 + tr.dx())
        ys = (tr.m12() * x0 + tr.m22() * y0 + tr.dy(),
              tr.m12() * x1 + tr.m22() * y0 + tr.dy(),
              tr.m12() * x1 + tr.m22() * y1 + tr.dy(),
              tr.m12() * x0 + tr.m22() * y1 + tr.dy())
        box = QtCore.QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        if box.intersects(area):
            visible += 1
    result = {
        "shapes": args.shapes, "points_per_shape": args.points,
        "visible_shapes": visible,
        "viewport": [ov.width(), ov.height()],
        "dpr": ov.devicePixelRatioF(),
        "frames": len(times),
        "median_ms": round(statistics.median(times), 2),
        "p95_ms": round(times_sorted[max(0, int(len(times) * 0.95) - 1)], 2),
        "min_ms": round(times_sorted[0], 2),
        "max_ms": round(times_sorted[-1], 2),
    }
    for line in (f"shapes={result['shapes']} points={result['points_per_shape']} "
                 f"viewport={result['viewport'][0]}x{result['viewport'][1]} "
                 f"dpr={result['dpr']} frames={result['frames']}",
                 f"median={result['median_ms']} ms  p95={result['p95_ms']} ms  "
                 f"min={result['min_ms']} ms  max={result['max_ms']} ms"):
        print(line)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shapes", type=int, default=200)
    ap.add_argument("--points", type=int, default=300)
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--spread", type=int, default=4000)
    ap.add_argument("--span-frac", type=float, default=0.5,
                    help="fraction of the canvas the viewport shows (default: all)")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
