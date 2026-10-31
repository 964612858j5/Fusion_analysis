"""v16_5 R3b plan §3.3: Step3's masks through each backend's REAL label path
-- the core planning and reading them (Rust), `Step3LabelBinding` (Python)
-- over a synthetic slide, optionally inside the label tests' concave
region; the presented picture at three cameras once settled.

    r3b_pixel_compare.py --backend rust|python --slide PATH --work DIR --out NPZ [--clip]
    r3b_pixel_compare.py --compare A.npz B.npz
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CONCAVE = ((100.0, 60.0), (900.0, 60.0), (900.0, 300.0), (420.0, 420.0),
           (900.0, 560.0), (900.0, 760.0), (100.0, 760.0))


def run(args):
    import numpy as np
    from sraw_rig import open_rig, stable
    from r3b_label_probe import _mask, _source
    kw = dict(roi=[60, 760, 100, 900], polygon=CONCAVE) if args.clip else {}
    rig = open_rig(args.backend, args.slide, labels=True, **kw)
    mount = rig.mount
    h0, w0 = rig.shape
    out = {"backend": args.backend, "binding": type(mount.gpu_binding).__name__,
           "label_binding": type(mount.label_binding).__name__}
    cell = _mask(args.work, "cell", (h0, w0))
    nuc = _mask(args.work, "nucleus", (h0, w0))
    sources = _source(cell, (h0, w0))
    sources["nucleus"] = _source(nuc, (h0, w0))["cell"].__class__(
        **{**sources["cell"].__dict__, "kind": "nucleus", "mask_path": nuc,
           "pyramid_path": sources["cell"].pyramid_path.replace("cell", "nucleus")})
    import dataclasses
    from block01.ui.step3_label_binding import DEFAULT_STYLES, kind_slots
    styles = {"cell": dataclasses.replace(DEFAULT_STYLES["cell"], width=2.0),
              "nucleus": dataclasses.replace(DEFAULT_STYLES["nucleus"], mode="fill", alpha=0.5)}
    mount.set_mask_slots(kind_slots(sources, styles))
    box = mount.host.stack.view.view_box
    cameras = {"whole": (0, w0, 0, h0), "middle": (60, 960, 40, 840), "fine": (250, 450, 250, 450)}
    saved = {}
    for name, (x0, x1, y0, y1) in cameras.items():
        box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        end = time.monotonic() + 90
        while time.monotonic() < end:
            _shot, ok = stable(rig, timeout=40.0)
            s = mount.label_binding.stats()
            if (ok and s["queued"] == 0 and s["reading"] == 0 and not s["pending_bytes"]
                    and mount.gpu_layer.label_stats().get("shown")
                    and not mount.label_binding.mask_status()["cell"]["building"]):
                break
            rig.pump(0.3)
        rig.pump(0.5)
        mount.gpu_layer.update()
        rig.pump(0.3)
        saved[name] = mount.gpu_layer.readback_shown_for_test()
        saved[f"{name}_ids"] = mount.gpu_layer.readback_label_ids_for_test()
        out[f"{name}_settled"] = time.monotonic() < end
    np.savez_compressed(args.out, **saved)
    out["ok"] = all(out[f"{n}_settled"] for n in cameras)
    mount.close()
    return out


def compare(a, b):
    import numpy as np
    x, y = np.load(a), np.load(b)
    out = {k: ("identical" if x[k].shape == y[k].shape and np.array_equal(x[k], y[k])
               else f"differ {int(np.sum(x[k] != y[k]))} values") for k in x.files}
    out["ok"] = all(v == "identical" for v in out.values())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("rust", "python"))
    ap.add_argument("--slide")
    ap.add_argument("--work")
    ap.add_argument("--out")
    ap.add_argument("--clip", action="store_true")
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
