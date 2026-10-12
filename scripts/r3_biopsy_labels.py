"""v16_5 R3 plan §2.6: Step3's masks over the real biopsy, Rust vs Python.

    r3_biopsy_labels.py --backend rust|python --project biopsy.json --mask ZARR --out NPZ
    r3_biopsy_labels.py --compare A.npz B.npz

A real cell mask (a COPY in a work folder: drawing it builds its zoomed-out
pyramid beside it) over the biopsy slide, read and drawn by the REAL label
binding of a Step3-kind viewer. For three cameras (whole slide, a middle
zoom, the finest) the presented picture is saved once the picture is
stable, the binding has nothing in flight and the masks are shown.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CHANNELS = ["DRAG5", "CD3D", "CD8"]


def _settled(rig, timeout=90.0):
    """Stable picture, then the label binding idle and its masks shown."""
    from sraw_rig import stable
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _shot, ok = stable(rig, timeout=40.0)
        binding, layer = rig.mount.label_binding, rig.mount.gpu_layer
        stats = binding.stats() if binding is not None else {}
        if (ok and binding is not None and stats.get("reading") == 0 and stats.get("queued") == 0
                and not stats.get("pending_bytes") and layer.label_stats().get("shown")):
            rig.pump(0.5)
            layer.update()
            rig.pump(0.3)
            return layer.readback_shown_for_test(), True
        rig.pump(0.3)
    return rig.mount.gpu_layer.readback_shown_for_test(), False


def run(args):
    import numpy as np
    from sraw_rig import open_rig
    import zarr
    project = json.load(open(args.project))
    rig = open_rig(args.backend, project["slide"], channels=CHANNELS, roi=project["roi"],
                   decisions=project["decisions"], zarr=project["zarr"], size=900, labels=True)
    from block01.core import label_pyramid as lp      # `block01` exists once the rig is open
    from block01.core import step3_masks as sm
    out = {"backend": args.backend, "binding": type(rig.mount.gpu_binding).__name__}
    mask = os.path.realpath(args.mask)
    h, w = zarr.open(mask, mode="r").shape
    rig.mount.set_mask_sources({
        "cell": sm.MaskSource(kind="cell", mask_path=mask, bbox=(0, h, 0, w),
                              pyramid_path=lp.pyramid_path_for(mask), pyramid_kind="cell",
                              pyramid=None, pyramid_reason="no label pyramid"),
        "nucleus": None})
    box = rig.mount.host.stack.view.view_box
    cameras = {"whole": (0, w, 0, h), "middle": (w * 0.4, w * 0.4 + 2000, h * 0.4, h * 0.4 + 2000),
               "fine": (w * 0.5, w * 0.5 + 300, h * 0.5, h * 0.5 + 300)}
    saved = {}
    for name, (x0, x1, y0, y1) in cameras.items():
        box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        shot, out[f"{name}_settled"] = _settled(rig)
        saved[name] = shot
        out[f"{name}_label_px"] = int((shot[..., :3] != rig.mount.gpu_layer.readback_rgba_for_test()[..., :3])
                                      .any(axis=-1).sum())
    np.savez_compressed(args.out, **saved)
    out["ok"] = all(out[f"{n}_settled"] for n in cameras)
    rig.mount.close()
    return out


def compare(a, b):
    import numpy as np
    x, y = np.load(a), np.load(b)
    out = {}
    for k in x.files:
        same = x[k].shape == y[k].shape and np.array_equal(x[k], y[k])
        out[k] = "identical" if same else f"differ {int((x[k] != y[k]).any(axis=-1).sum())} px"
    out["ok"] = all(v == "identical" for v in out.values())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("rust", "python"))
    ap.add_argument("--project")
    ap.add_argument("--mask")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2)
    args = ap.parse_args()
    try:
        out = compare(*args.compare) if args.compare else run(args)
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out = {"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-2000:]}
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
