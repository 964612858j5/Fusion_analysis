"""v16_5 R3 plan §2.1 / gate 7: what drawing Step3's masks costs the GUI
thread during gestures (L1: Python plans and reads, the core draws).

    r3_label_gate.py --backend rust|python --project biopsy.json --mask ZARR --out JSON

A Step3-kind viewer on the biopsy slide with a real cell mask (a COPY in a
work folder). The same scripted gesture -- a drag, then zooms in and out,
one camera step per 16 ms through the ViewBox, as the controller sees a
drag -- runs with no masks and with the masks shown, alternated, twice
each; the second of each counts (warm). Per phase:
  * paint: every paint's time on the GUI thread (`paintGL`, both layers);
  * handover (Rust): every `set_labels` call's time, of it the hand-over
    (numbering, the missing planes' bytes, the core's copy);
  * gaps: a 5 ms heartbeat's intervals; how many exceed 50 ms.
Scripted camera steps, not injected mouse input: the GUI-thread work of a
gesture, not its end-to-end response.
"""

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CHANNELS = ["DRAG5", "CD3D", "CD8"]


def _pct(values, q):
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("rust", "python"), required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--mask", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = {"backend": args.backend}
    try:
        from PyQt5 import QtCore
        from sraw_rig import open_rig, stable
        project = json.load(open(args.project))
        rig = open_rig(args.backend, project["slide"], channels=CHANNELS, roi=project["roi"],
                       decisions=project["decisions"], zarr=project["zarr"], size=900, labels=True)
        from block01.core import label_pyramid as lp
        from block01.core import step3_masks as sm
        import zarr
        mount, layer = rig.mount, rig.mount.gpu_layer
        out["binding"] = type(mount.gpu_binding).__name__
        mask = os.path.realpath(args.mask)
        h, w = zarr.open(mask, mode="r").shape
        sources = {"cell": sm.MaskSource(kind="cell", mask_path=mask, bbox=(0, h, 0, w),
                                         pyramid_path=lp.pyramid_path_for(mask), pyramid_kind="cell",
                                         pyramid=None, pyramid_reason="no label pyramid"),
                   "nucleus": None}
        box = mount.host.stack.view.view_box
        start = (w * 0.3, w * 0.3 + 3000, h * 0.3, h * 0.3 + 3000)

        rec = {"paint": [], "set": [], "send": [], "gaps": [], "label": []}
        paint = layer.paintGL

        def timed_paint():
            t = time.perf_counter()
            paint()
            rec["paint"].append((time.perf_counter() - t) * 1000)
            report = getattr(layer, "last_report", None) or {}
            if "label_ms" in report:                         # R3b: the core's own count
                rec["label"].append(float(report["label_ms"]))
        layer.paintGL = timed_paint
        if hasattr(layer, "_send_labels"):
            send = layer._send_labels

            def timed_send():
                t = time.perf_counter()
                send()
                rec["send"].append((time.perf_counter() - t) * 1000)
            layer._send_labels = timed_send
        set_labels = layer.set_labels

        def timed_set(snapshot):
            t = time.perf_counter()
            try:
                return set_labels(snapshot)
            finally:
                rec["set"].append((time.perf_counter() - t) * 1000)
        layer.set_labels = timed_set
        beat = {"last": time.perf_counter()}
        heart = QtCore.QTimer()
        heart.setInterval(5)

        def on_beat():
            now = time.perf_counter()
            rec["gaps"].append((now - beat["last"]) * 1000)
            beat["last"] = now
        heart.timeout.connect(on_beat)

        def gesture():
            box.setRange(xRange=start[:2], yRange=start[2:], padding=0)
            stable(rig)
            for k in rec:
                rec[k].clear()
            lb = mount.label_binding
            before = dict(lb.stats()) if lb is not None else {}
            beat["last"] = time.perf_counter()
            heart.start()
            for _ in range(60):                             # drag right
                box.translateBy(x=40.0)
                rig.pump(0.016)
            for i, f in enumerate([0.92] * 12 + [1.08] * 12):  # zoom in, then out
                box.scaleBy((f, f))
                if i == 23:
                    last_change = time.perf_counter()   # the camera's last change
                rig.pump(0.016)
            # from the camera's last change: until every wanted tile is held,
            # nothing queued or read, no label upload left for a later paint
            # -- and then a frame presented (on screen), at most 5 s
            quiet = last_change
            caught = None
            presents = [0]
            layer.frameSwapped.connect(lambda: presents.__setitem__(0, presents[0] + 1))

            def complete():
                if lb is None:
                    return True
                st = lb.stats()
                if st.get("queued") or st.get("reading") or st.get("pending_bytes"):
                    return False
                if hasattr(lb, "_wanted"):                  # Python: every wanted tile resident
                    return all(k in lb._resident for k in lb._wanted)
                core_st = layer.core.core_label_stats()
                if (layer.last_report or {}).get("label_deferred", 0):
                    return False                            # label uploads still to come
                return set(map(tuple, core_st["wanted_keys"])) <= set(map(tuple, core_st["held_keys"]))
            done_at = None
            while time.perf_counter() - quiet < 5.0:
                layer.update()
                rig.pump(0.005)
                if done_at is None and complete():
                    done_at = presents[0]                  # the next present shows it
                elif done_at is not None and presents[0] > done_at:
                    caught = (time.perf_counter() - quiet) * 1000
                    break
            rig.pump(0.5)
            heart.stop()
            after = dict(lb.stats()) if lb is not None else {}
            moved = {k: int(after.get(k, 0) or 0) - int(before.get(k, 0) or 0)
                     for k in ("requested", "read", "accepted", "stale_dropped", "late_dropped")}
            core = getattr(layer, "core", None)
            core_stats = dict(core.core_label_stats()) if hasattr(core, "core_label_stats") else {}
            moved["split_ms"] = core_stats.get("split_ms")
            moved["uploads_total"] = core_stats.get("uploads")
            return {"catch_up_ms": None if caught is None else round(caught, 1), "label_frames": len(rec["label"]),
                    "label_p50": round(_pct(rec["label"], 0.5), 3), "label_p95": round(_pct(rec["label"], 0.95), 3),
                    "label_max": round(max(rec["label"] or [0]), 3), "binding": type(lb).__name__,
                    "l1": getattr(lb, "l1_reason", None), **moved,"paints": len(rec["paint"]), "paint_p50": round(_pct(rec["paint"], 0.5), 3),
                    "paint_p95": round(_pct(rec["paint"], 0.95), 3),
                    "paint_max": round(max(rec["paint"] or [0]), 3),
                    "sets": len(rec["set"]), "set_p95": round(_pct(rec["set"], 0.95), 3),
                    "set_max": round(max(rec["set"] or [0]), 3),
                    "send_p95": round(_pct(rec["send"], 0.95), 3),
                    "gaps_over_50": sum(g > 50 for g in rec["gaps"]),
                    "gap_p95": round(_pct(rec["gaps"], 0.95), 3), "gap_max": round(max(rec["gaps"] or [0]), 3)}

        mount.set_mask_sources(sources)                    # warm: the pyramid is built once
        box.setRange(xRange=start[:2], yRange=start[2:], padding=0)
        end = time.monotonic() + 60
        while time.monotonic() < end and not layer.label_stats().get("shown"):
            layer.update()
            rig.pump(0.2)
        out["warm_shown"] = bool(layer.label_stats().get("shown"))
        built = mount.label_binding.sources()               # with the pyramid just built
        sources = {k: built.get(k) for k in ("cell", "nucleus")}
        out["pyramid_known"] = sources["cell"] is not None and sources["cell"].pyramid is not None
        phases = {}
        for rnd in (1, 2):
            for name, src in (("no_masks", None), ("masks", sources)):
                mount.set_mask_sources(src)
                rig.pump(0.5)
                phases[f"{name}_{rnd}"] = gesture()
        out["phases"] = phases
        out["ok"] = out["warm_shown"] and out["pyramid_known"]
        mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
