"""v16_5 R2 (plan §2.9, §3, §5.1): the stable Step1 picture of the Python
and the Rust backend, compared pixel by pixel.

    r2_pixel_compare.py probe --backend python|rust --project P.json --out OUT.npz
                              [--mode overlay|fusion] [--channels a,b,...]
                              [--groups JSON] [--nucleus C] [--smooth 0|1]
                              [--cases x0:x1:y0:y1;...] [--no-polygon]
    r2_pixel_compare.py compare --project P.json --out DIR [same options]

`probe` opens the REAL Step1 mount (one backend per process, one GL widget
per process) on the project (`r2_synth_project.py` writes one; a real one
is a JSON with slide / zarr / decisions / roi / polygon), sets the mode and
channels, and for each camera case waits until the picture is STABLE:
  - the backend asked for is the one drawing (binding type), and it has
    not fallen back;
  - the camera is where the case put it;
  - nothing is in flight or waiting for upload, the target level is all
    on the GPU, every expected channel drawn (Rust: rows == expected,
    shown == wanted; Python: scheduler idle, no deferred upload, nothing
    pending);
  - the mode drawn is the one asked for and the channels drawn are exactly
    the expected ones (Rust: the binding's display and active channels;
    Python: the layer's last submission and the published coarse channels);
  - a paint ran after the check above, with the camera of the case (the
    paint is wrapped and records the view it read);
  - two readbacks of `final` 300 ms apart are identical, each after such a
    paint. `final` is what the paint blits to the window; the window's own
    presentation is not read back (r2_gl_state_probe's other-painter step
    reads the window framebuffer);
then stores `final` (RGBA, top row first).
`compare` runs both probes and reports per case the differing pixels, the
largest difference per component and (when they differ) a diff image.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _cases(arg, h0, w0):
    if arg:
        return [tuple(float(v) for v in c.split(":")) for c in arg.split(";") if c]
    return [
        (0.0, w0, 0.0, h0),                                       # whole slide (coarse)
        (w0 * 0.25, w0 * 0.75, h0 * 0.25, h0 * 0.75),             # middle level
        (w0 * 0.40, w0 * 0.40 + 600, h0 * 0.40, h0 * 0.40 + 600),  # level 0
        (w0 - 400, w0 + 200, h0 - 400, h0 + 200),                 # past the slide's corner
        (w0 * 0.1, w0 * 0.1 + 160, h0 * 0.6, h0 * 0.6 + 160),     # magnified: smoothing
    ]


def _near(a, b):
    return a is not None and max(abs(u - v) for u, v in zip(a, b)) <= 1.0


def _watch_paints(layer):
    """Wrap the layer's paintGL: count paints and record the camera each
    one read (both layers read the view box at paint time)."""
    seen = {"paints": 0, "view": None}
    paint = layer.paintGL
    box = layer._attached_view.view_box

    def watched():
        (x0, x1), (y0, y1) = box.viewRange()
        seen["view"] = (x0, x1, y0, y1)
        paint()
        seen["paints"] += 1
    layer.paintGL = watched
    return seen


def _expected(rig, args, groups):
    """The channels the picture must draw: Overlay the chosen ones, Fusion
    the groups' (weight > 0) and the nucleus; less those with no product."""
    if args.mode == "fusion":
        names = {c for chs in (groups or {"markers": {c: 1.0 for c in rig.chosen[1:]}}).values()
                 for c, w in chs.items() if float(w) > 0}
        names.add(args.nucleus or rig.chosen[0])
    else:
        names = set(rig.chosen)
    table = getattr(rig.mount.host.stack.provider, "source_table", None)
    return {c for c in names if table is None or table.source_of(c) != "missing"}


def _settled(rig, expect_backend, case, mode, expected):
    """Everything but the paint and readback checks of `stable`, as a
    reason string ('' = settled)."""
    mount = rig.mount
    binding = mount.gpu_binding
    if binding is None or type(binding).__name__ != expect_backend:
        return f"binding {type(binding).__name__}"
    box = mount.host.stack.view.view_box
    (vx0, vx1), (vy0, vy1) = box.viewRange()
    if not _near((vx0, vx1, vy0, vy1), case):
        return "camera elsewhere"
    if expect_backend == "Step1CoreBinding":
        if binding.stats().get("unsupported"):
            return "fell back"
        report = mount.gpu_layer.last_report or {}
        if (not binding.idle() or report.get("deferred", 1) or report.get("reserving")
                or report.get("shown", 0) < report.get("wanted", 0)):
            return "rust busy"
        if int(report.get("channels", 0)) < len(binding._active):
            return "rust channels missing"
        if binding._display is None or binding._display.mode != mode:
            return "rust mode"
        if set(binding._active) != expected:
            return f"rust channels {sorted(binding._active)}"
    else:
        stats = binding.stats()
        if (not mount.host.stack.scheduler.idle() or stats.get("deferred_uploads")
                or binding._fine or binding.coarse_pending_channels()):
            return "python busy"
        if mount.gpu_layer._submission.get("mode") != mode:
            return "python mode"
        if set(stats.get("coarse_channels", ())) != expected:
            return f"python channels {sorted(stats.get('coarse_channels', ()))}"
    return ""


def probe(args):
    sys.path.insert(0, HERE)
    import numpy as np
    from sraw_rig import open_rig
    project = json.load(open(args.project))
    groups = json.loads(args.groups) if args.groups else None
    chosen = [c for c in args.channels.split(",") if c] or project["names"]
    rig = open_rig(args.backend, project["slide"], channels=chosen, roi=project.get("roi"),
                   size=args.size, smooth=args.smooth, decisions=project.get("decisions"),
                   zarr=project.get("zarr", ""), polygon=None if args.no_polygon else project.get("polygon"),
                   groups=groups, nucleus=args.nucleus or None)
    mount, pump = rig.mount, rig.pump
    if args.mode == "fusion":
        from block01.ui.step1_viewer_mount import LEGACY_FUSION
        mount.set_mode(LEGACY_FUSION)
    pump(1.0)
    expect = "Step1CoreBinding" if args.backend == "rust" else "Step1GpuBinding"
    from block01.ui.step1_gpu_layer import MODE_FUSION, MODE_OVERLAY
    mode = MODE_FUSION if args.mode == "fusion" else MODE_OVERLAY
    expected = _expected(rig, args, groups)
    seen = _watch_paints(mount.gpu_layer)
    h0, w0 = rig.shape
    box = mount.host.stack.view.view_box
    out, meta = {}, []
    for i, case in enumerate(_cases(args.cases, h0, w0)):
        x0, x1, y0, y1 = case
        moved = time.monotonic()
        box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        (vx0, vx1), (vy0, vy1) = box.viewRange()
        case = (vx0, vx1, vy0, vy1)                   # where the view really went
        deadline, last, why, shot = time.monotonic() + 60.0, None, "timeout", None
        settled_at = None
        while time.monotonic() < deadline:
            pump(0.3)
            why = _settled(rig, expect, case, mode, expected)
            if why:
                last = None
                continue
            painted = seen["paints"]
            mount.gpu_layer.update()
            pump(0.1)                                  # a paint after the checks
            if seen["paints"] == painted or not _near(seen["view"], case):
                why, last = "no paint of this camera", None
                continue
            shot = mount.gpu_layer.readback_rgba_for_test()
            if last is not None and shot.shape == last.shape and np.array_equal(shot, last):
                why = ""
                break
            settled_at = time.monotonic()               # the first of the two equal readbacks
            last, why = shot, "settling"
        out[f"case{i}"] = shot if shot is not None else np.zeros((0, 0, 4), np.uint8)
        meta.append({"case": i, "range": list(case), "stable": why == "", "why": why,
                     # move -> first of the two equal readbacks (coarse: the
                     # polling is 0.4 s; for reporting, not a gate)
                     "settle_s": None if settled_at is None else round(settled_at - moved, 2)})
    np.savez_compressed(args.out, **out)
    stats = mount.gpu_binding.stats() if mount.gpu_binding is not None else {}
    result = {"backend": args.backend, "binding": type(mount.gpu_binding).__name__,
              "rust_refused": mount._rust_refused, "cases": meta,
              "unsupported": stats.get("unsupported", ""), "missing_windows": stats.get("missing_windows"),
              "roi_polygon_error": stats.get("roi_polygon_error"),
              "unavailable": stats.get("unavailable"),
              "sampler_split": (stats.get("core") or {}).get("sampler_split")}
    mount.close()
    rig.app.processEvents()
    print(json.dumps(result, default=str), flush=True)
    os._exit(0)


def compare(args):
    import numpy as np
    os.makedirs(args.out, exist_ok=True)
    runs = {}
    for backend in ("python", "rust"):
        npz = os.path.join(args.out, f"{backend}.npz")
        cmd = [sys.executable, os.path.abspath(__file__), "probe", "--backend", backend,
               "--project", args.project, "--out", npz, "--mode", args.mode, "--smooth", str(args.smooth),
               "--size", str(args.size)]
        for flag, value in (("--channels", args.channels), ("--groups", args.groups),
                            ("--nucleus", args.nucleus), ("--cases", args.cases)):
            if value:
                cmd += [flag, value]
        if args.no_polygon:
            cmd.append("--no-polygon")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=dict(os.environ))
        lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
        if not lines:
            print(json.dumps({"ok": False, "backend": backend, "stderr": proc.stderr[-2000:]}))
            return 1
        runs[backend] = (json.loads(lines[-1]), np.load(npz))
    (py, py_px), (rs, rs_px) = runs["python"], runs["rust"]
    cases = []
    for name in py_px.files:
        a, b = py_px[name], rs_px[name]
        entry = {"case": name, "shape": list(a.shape)}
        if a.shape != b.shape or not a.size:
            entry.update(differ=-1)
        else:
            d = np.abs(a.astype(int) - b.astype(int))
            entry.update(differ=int((d.max(axis=2) > 0).sum()), max_rgba=[int(v) for v in d.max(axis=(0, 1))])
            if entry["differ"]:
                np.save(os.path.join(args.out, f"diff_{name}.npy"), d.max(axis=2).astype(np.uint8))
        cases.append(entry)
    ok = (py["binding"] == "Step1GpuBinding" and rs["binding"] == "Step1CoreBinding"
          and all(c["stable"] for c in py["cases"] + rs["cases"]) and all(c["differ"] == 0 for c in cases))
    result = {"ok": ok, "python": py, "rust": rs, "cases": cases}
    print(json.dumps(result, default=str), flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("probe", "compare"))
    ap.add_argument("--backend", choices=("python", "rust"))
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=("overlay", "fusion"), default="overlay")
    ap.add_argument("--channels", default="")
    ap.add_argument("--groups", default="")
    ap.add_argument("--nucleus", default="")
    ap.add_argument("--smooth", type=int, default=1)
    ap.add_argument("--cases", default="")
    ap.add_argument("--size", type=int, default=530)
    ap.add_argument("--no-polygon", action="store_true")
    args = ap.parse_args()
    if args.what == "probe":
        probe(args)
    else:
        sys.exit(compare(args))


if __name__ == "__main__":
    main()
