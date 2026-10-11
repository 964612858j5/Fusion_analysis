"""v16_5 S-Raw: the stable Step1 picture of one backend, for pixel comparison.

    sraw_pixel_probe.py --backend python|rust --slide PATH --out OUT.npz
                        [--smooth 0|1] [--channels A,B,...] [--roi y0,y1,x0,x1]

Opens the REAL Step1 mount (real host, stack, controller and view) on an
OME-TIFF, ticks the channels, then for each camera case waits until the
picture is STABLE -- nothing in flight, nothing waiting for upload, and two
readbacks 300 ms apart identical -- and stores the layer's own `final`
readback. One backend per process (one GL widget per process). The test
`tests/test_v16_5_sraw_pixels.py` runs it twice and compares.
"""

import argparse
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("python", "rust"), required=True)
    ap.add_argument("--slide", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--smooth", type=int, default=1)
    ap.add_argument("--channels", default="")
    ap.add_argument("--roi", default="")
    ap.add_argument("--cases", default="")
    ap.add_argument("--size", type=int, default=530)
    args = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import numpy as np
    from sraw_rig import open_rig, stable

    rig = open_rig(args.backend, args.slide, channels=[c for c in args.channels.split(",") if c],
                   roi=[int(v) for v in args.roi.split(",")] if args.roi else None,
                   size=args.size, smooth=args.smooth)
    mount, pump = rig.mount, rig.pump
    h0, w0 = rig.shape
    pump(1.0)
    box = mount.host.stack.view.view_box
    cases = [tuple(float(v) for v in c.split(":")) for c in args.cases.split(";") if c] or [
        (0.0, w0, 0.0, h0),                                   # whole slide (coarse)
        (w0 * 0.25, w0 * 0.75, h0 * 0.25, h0 * 0.75),         # middle level
        (w0 * 0.40, w0 * 0.40 + 600, h0 * 0.40, h0 * 0.40 + 600),   # level 0
        (w0 - 400, w0 + 200, h0 - 400, h0 + 200),             # past the slide's corner
        (w0 * 0.1, w0 * 0.1 + 160, h0 * 0.6, h0 * 0.6 + 160),       # magnified: smoothing
    ]
    out, meta = {}, []
    for i, (x0, x1, y0, y1) in enumerate(cases):
        box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        shot, ok = stable(rig)
        out[f"case{i}"] = shot if shot is not None else np.zeros((0, 0, 4), np.uint8)
        meta.append({"case": i, "range": [x0, x1, y0, y1], "stable": ok,
                     "view_range": [list(r) for r in box.viewRange()]})
    np.savez_compressed(args.out, **out)
    result = {"backend": mount.backend, "binding": type(mount.gpu_binding).__name__,
              "reason": mount.gpu_status().get("reason", ""), "rust_refused": mount._rust_refused,
              "cases": meta, "scheduler": type(mount.host.stack.scheduler).__name__,
              "scheduler_started": getattr(mount.host.stack.scheduler, "started", True)}
    mount.close()
    rig.app.processEvents()
    print(json.dumps(result, default=str), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
