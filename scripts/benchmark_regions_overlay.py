"""Regions overlay: static-outline paint cost, pan and zoom separately.

This is a MICROBENCHMARK of `RegionsOverlay._paint_static` -- the function
`paintEvent` calls when its cached picture is stale. It is not `paintEvent`,
not a screen round trip, and not "how long a drag feels": nothing here reads
pixels from a device, uploads a texture or presents a frame.

    python scripts/benchmark_regions_overlay.py --mode pan  [--frames 100]
    python scripts/benchmark_regions_overlay.py --mode zoom [--frames 100]

Per frame the target pixmap is cleared (OUTSIDE the timed region -- the clear
is the same for every implementation, so it is excluded rather than charged to
whichever one is measured), and `_paint_static` is called with the frame's
real transform. `--warmup` frames run first and are not recorded. Raw
per-frame milliseconds are written with `--raw-out`; the JSON summary carries
the binding, versions, source paths, viewport, DPR and the transform range, so
two runs are only compared when those agree.

`--span-frac` is the fraction of the canvas the VIEWPORT shows (not a count of
visible shapes); the visible-shape count is computed independently from the
input `shapes` list and reported.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _cand in (os.path.join(_REPO, ".block01", "pkg"), os.path.dirname(_REPO)):
    if os.path.isdir(os.path.join(_cand, "block01")) and _cand not in sys.path:
        sys.path.insert(0, _cand)

from block01.qt import QtCore, QtGui, QtWidgets  # noqa: E402

import block01  # noqa: E402
import block01.qt as _qt  # noqa: E402
import pyqtgraph as pg  # noqa: E402

from block01.ui.regions import overlay as _overlay  # noqa: E402
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


def _visible_count(shapes, transform, width, height):
    """How many shapes the viewport shows -- from the INPUT, not from the
    implementation under test (`ov._shapes` does not exist in every one)."""
    m11, m12 = transform.m11(), transform.m12()
    m21, m22 = transform.m21(), transform.m22()
    dx, dy = transform.dx(), transform.dy()
    area = QtCore.QRectF(0, 0, width, height)
    n = 0
    for it in shapes:
        pts = it["polygon"]
        xs = [m11 * x + m21 * y + dx for x, y in pts]
        ys = [m12 * x + m22 * y + dy for x, y in pts]
        box = QtCore.QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        if box.intersects(area):
            n += 1
    return n


def _pan_transform(vb, width, height, span, i):
    """One pixel per frame to the right, both axes."""
    x0 = span * 0.1 + i * (span / max(1, width))
    y0 = span * 0.1 + i * (span / max(1, height))
    vb.setRange(xRange=(x0, x0 + span), yRange=(y0, y0 + span * height / width),
                padding=0)


def _zoom_transform(vb, width, height, span, i):
    """1 % closer per frame, around the centre."""
    s = span / (1.0 + 0.01 * i)
    x0 = (span - s) / 2.0
    y0 = (span - s) * (height / width) / 2.0
    vb.setRange(xRange=(x0, x0 + s), yRange=(y0, y0 + s * height / width),
                padding=0)


def _percentile(sorted_vals, q):
    """Nearest-rank percentile of an already sorted list (documented, not
    numpy's default interpolation)."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(q * len(sorted_vals))) - 1))
    return sorted_vals[k]


def _git_head():
    try:
        return subprocess.run(["git", "-C", _REPO, "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return "?"


def run(args):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = pg.GraphicsView()
    view.resize(1200, 800)
    vb = pg.ViewBox()
    view.setCentralItem(vb)
    view.show()
    ov = RegionsOverlay(view, vb, None)
    shapes = _shapes(args.shapes, args.points, args.spread)

    width, height = ov.width(), ov.height()
    span = args.spread * args.span_frac
    step = _pan_transform if args.mode == "pan" else _zoom_transform
    step(vb, width, height, span, 0)
    ov.set_items(shapes)
    app.processEvents()

    pixmap = QtGui.QPixmap(width, height)
    transforms = []
    times = []
    frames = args.warmup + args.frames
    non_empty = 0
    for i in range(frames):
        step(vb, width, height, span, i)
        app.processEvents()
        tr = ov.canvas_transform()
        transforms.append((tr.m11(), tr.m22(), tr.dx(), tr.dy()))
        pixmap.fill(QtCore.Qt.transparent)          # outside the timed region
        p = QtGui.QPainter(pixmap)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        t0 = time.perf_counter()
        ov._paint_static(p, tr)
        dt = (time.perf_counter() - t0) * 1000.0
        p.end()
        if i == args.warmup and args.check_pixels:
            img = pixmap.toImage()
            for y in range(0, height, 16):
                for x in range(0, width, 16):
                    if img.pixelColor(x, y).alpha() != 0:
                        non_empty += 1
        if i >= args.warmup:
            times.append(dt)

    ordered = sorted(times)
    result = {
        "mode": args.mode,
        "shapes": args.shapes,
        "points_per_shape": args.points,
        "viewport": [width, height],
        "dpr": ov.devicePixelRatioF(),
        "span_frac": args.span_frac,
        "frames_measured": len(times),
        "warmup_frames": args.warmup,
        "median_ms": round(statistics.median(times), 3),
        "mean_ms": round(statistics.fmean(times), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "percentile_rule": "nearest-rank on the measured frames",
        "clear_timed": False,
        "transform_first": transforms[args.warmup],
        "transform_last": transforms[-1],
        "non_empty_samples": non_empty,
        "binding": _qt.BINDING,
        "qt_version": QtCore.qVersion(),
        "plugin": app.platformName(),
        "python": platform.python_version(),
        "block01": os.path.dirname(block01.__file__),
        "overlay_source": _overlay.__file__,
        "git_head": _git_head(),
        "host": platform.node(),
    }
    # The visible count belongs to the FIRST measured frame's transform.
    first = transforms[args.warmup]
    tr0 = QtGui.QTransform(first[0], 0, 0, first[1], first[2], first[3])
    result["visible_shapes_first"] = _visible_count(shapes, tr0, width, height)

    print("mode=%(mode)s binding=%(binding)s qt=%(qt_version)s head=%(git_head)s" % result)
    print("shapes=%(shapes)d points=%(points_per_shape)d visible=%(visible_shapes_first)d "
          "viewport=%(viewport)s dpr=%(dpr)s span_frac=%(span_frac)s" % result)
    print("frames=%(frames_measured)d (warmup %(warmup_frames)d) median=%(median_ms)s ms "
          "p95=%(p95_ms)s min=%(min_ms)s max=%(max_ms)s non_empty=%(non_empty_samples)d" % result)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
    if args.raw_out:
        with open(args.raw_out, "w", encoding="utf-8") as fh:
            json.dump({"times_ms": times, "transforms": transforms[args.warmup:]},
                      fh, indent=0)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("pan", "zoom"), default="pan")
    ap.add_argument("--shapes", type=int, default=200)
    ap.add_argument("--points", type=int, default=300)
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--spread", type=int, default=4000)
    ap.add_argument("--span-frac", type=float, default=0.5,
                    help="fraction of the canvas the viewport shows")
    ap.add_argument("--check-pixels", action="store_true",
                    help="count non-transparent samples on the first measured frame")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--raw-out", default="")
    args = ap.parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
