"""G3.2b.5A.2: record the REAL desktop's Block01 window and look for the wobble.

5A and 5A.1 measured the camera, the widget geometry and a forced frame, on
one real slide, and found nothing moving. What neither could get was a
NATURAL PRESENTED FRAME -- the thing the user is actually looking at. This
records those frames off the real X11 session and compares them.

Three modes, all read-only with respect to the application:

    find      locate the Block01 main window and print its identity
    record    ffmpeg x11grab of THAT WINDOW ONLY, lossless
    analyse   per-frame translation / scale / shear against a stable frame

WHY LOSSLESS. A lossy codec can smear a one-frame shift into something the
analysis would not see. `ffv1` is used so a geometric change cannot be
blamed on, or hidden by, the encoder.

WHAT THIS DOES NOT DO. It never sends the application an event: x11grab
reads the window's pixels from the X server and does not post Expose, so
nothing here forces a repaint. It records ONE window id -- never the
desktop, never another application.

HONEST LIMIT ON `-window_id`: on a compositing WM the window is redirected
and its own pixmap is captured whole; without compositing, anything drawn
OVER the window can appear in the capture. `find` reports whether a
compositor is present so the report can say which case applied.

Usage:
    python scripts/record_step1_patch_wobble_x11.py find
    python scripts/record_step1_patch_wobble_x11.py record --window 0x... \\
        --seconds 15 --out /tmp/wobble.mkv
    python scripts/record_step1_patch_wobble_x11.py analyse \\
        --video /tmp/wobble.mkv --crop X,Y,W,H --out report.json
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

DISPLAY = os.environ.get("DISPLAY", ":1")
#: Substrings that identify the product's own main window, not a terminal
#: that happens to have the project path in its title.
#: A real Qt top-level under this WM is wrapped in a server-side decoration
#: frame whose WM_CLASS is `mutter-x11-frames`, so the class says nothing
#: about WHOSE window it is. Identity is taken from `_NET_WM_PID` ->
#: /proc/<pid>/cmdline instead, which cannot be confused with a terminal
#: that merely has the project path in its title.
WANTED_CMDLINE = ("block01", "main.py")
BLOCKED_CLASSES = ("gnome-terminal", "google-chrome", "chromium", "gjs",
                   "nvidia-settings", "cc-switch", "update-manager",
                   "gsd-", "ibus", "update-notifier", "webkit")


def x(*args):
    env = dict(os.environ, DISPLAY=DISPLAY)
    return subprocess.run(args, capture_output=True, text=True, env=env)


def window_rows(parent="-root"):
    """Top-levels, PLUS the client window inside each decoration frame.

    Under this WM a real Qt top-level is wrapped in a `mutter-x11-frames`
    frame; the frame's `_NET_WM_PID` is mutter's, not the application's, so
    a search that stops at the frame finds nothing that looks like Block01.
    """
    args = (("xwininfo", "-root", "-children") if parent == "-root"
            else ("xwininfo", "-id", parent, "-children"))
    out = x(*args).stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r'\s+(0x[0-9a-f]+)\s+(".*?"|\(has no name\)):\s+'
                     r'\((.*?)\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
        if not m:
            continue
        wid, title, cls, w, h, px, py = m.groups()
        rows.append({"id": wid, "title": title.strip('"'), "wm_class": cls,
                     "w": int(w), "h": int(h), "x": int(px), "y": int(py)})
    if parent == "-root":
        for row in list(rows):
            if "mutter-x11-frames" in row["wm_class"]:
                for child in window_rows(row["id"]):
                    if child["w"] > 200 and child["h"] > 200:
                        child["inside_frame"] = row["id"]
                        rows.append(child)
    return rows


def pid_of(wid):
    out = x("xprop", "-id", wid, "_NET_WM_PID").stdout
    m = re.search(r"=\s*(\d+)", out)
    return int(m.group(1)) if m else None


def cmdline(pid):
    if not pid:
        return ""
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        return " ".join(raw.decode("utf-8", "replace").split("\0")).strip()
    except OSError:
        return ""


def compositor():
    out = x("xprop", "-root", "_NET_SUPPORTING_WM_CHECK").stdout
    name = x("xprop", "-root", "_NET_WM_CM_S0").stdout
    return {"supporting_wm_check": out.strip(), "cm_s0": name.strip()}


def find(args):
    rows = window_rows()
    candidates = []
    for row in rows:
        low = (row["wm_class"] + " " + row["title"]).lower()
        if any(b in low for b in BLOCKED_CLASSES):
            continue
        if row["w"] < 400 or row["h"] < 300:
            continue
        pid = pid_of(row["id"])
        cmd = cmdline(pid)
        row["pid"], row["cmdline"] = pid, cmd
        row["looks_like_block01"] = any(k in cmd.lower()
                                        for k in WANTED_CMDLINE)
        candidates.append(row)
    report = {"display": DISPLAY, "compositor": compositor(),
              "candidates": candidates,
              "block01_windows": [c for c in candidates
                                  if c["looks_like_block01"]]}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["block01_windows"]:
        print("\nNO BLOCK01 WINDOW FOUND. Start the application the way you "
              "normally do, then run `find` again. Nothing is launched or "
              "touched here.", file=sys.stderr)
        return 1
    return 0


def record(args):
    wid = args.window
    rows = {r["id"]: r for r in window_rows()}
    info = rows.get(wid) or rows.get(wid.lower())
    if info is None:
        # An id given explicitly is honoured even if the tree walk missed
        # it, as long as X still knows it.
        probe = x("xwininfo", "-id", wid)
        if probe.returncode == 0:
            info = {"id": wid, "raw_xwininfo": probe.stdout.strip()}
    if info is None:
        print(f"window {wid} is not on the display any more", file=sys.stderr)
        return 1
    pid = pid_of(wid)
    meta = {"window": info, "pid": pid, "cmdline": cmdline(pid),
            "display": DISPLAY, "compositor": compositor(),
            "seconds": args.seconds, "fps": args.fps,
            "started_unix": time.time(),
            "started_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "codec": "ffv1 (lossless)",
            "note": ("x11grab of ONE window id; no Expose is posted, so the "
                     "application is not forced to repaint"),
            }
    out = pathlib.Path(args.out)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           # NO CURSOR: as root, x11grab's pointer query fails on this
           # session and aborts the capture mid-way ("Failed to query xcb
           # pointer"). It also keeps the cursor out of the frames, which
           # the per-frame comparison would otherwise see as motion.
           "-f", "x11grab", "-draw_mouse", "0",
           "-framerate", str(args.fps),
           "-window_id", wid, "-i", DISPLAY,
           "-t", str(args.seconds),
           "-c:v", "ffv1", "-level", "3", str(out)]
    meta["ffmpeg"] = " ".join(cmd)
    print("recording:", meta["ffmpeg"], flush=True)
    proc = subprocess.run(cmd, env=dict(os.environ, DISPLAY=DISPLAY),
                          capture_output=True, text=True)
    meta["returncode"] = proc.returncode
    meta["stderr_tail"] = proc.stderr.strip().splitlines()[-8:]
    meta["finished_unix"] = time.time()
    meta["size_bytes"] = out.stat().st_size if out.exists() else 0
    pathlib.Path(args.out + ".meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False))
    print(json.dumps({k: meta[k] for k in
                      ("returncode", "size_bytes", "stderr_tail")}, indent=2))
    return 0 if proc.returncode == 0 and meta["size_bytes"] > 0 else 1


def analyse(args):
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"cannot open {args.video}", file=sys.stderr)
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS) or float(args.fps)
    crop = None
    if args.crop:
        crop = tuple(int(v) for v in args.crop.split(","))

    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if crop:
            cx, cy, cw, ch = crop
            frame = frame[cy:cy + ch, cx:cx + cw]
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(
            np.float32))
    cap.release()
    if len(frames) < 3:
        print("too few frames", file=sys.stderr)
        return 1

    reference = frames[0]
    window = np.hanning(reference.shape[0])[:, None] * \
        np.hanning(reference.shape[1])[None, :]
    rows = []
    for i, frame in enumerate(frames):
        # TRANSLATION, sub-pixel, by phase correlation -- a shift of even a
        # fraction of a pixel shows here.
        (dx, dy), response = cv2.phaseCorrelate(reference * window,
                                                frame * window)
        # SCALE / SHEAR: fit an affine between the two and read its linear
        # part. `None` when the fit fails (a frame that changed too much).
        scale = shear = None
        try:
            warp = np.eye(2, 3, dtype=np.float32)
            cc, warp = cv2.findTransformECC(
                reference, frame, warp, cv2.MOTION_AFFINE,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5),
                None, 5)
            scale = [float(warp[0, 0]), float(warp[1, 1])]
            shear = [float(warp[0, 1]), float(warp[1, 0])]
        except cv2.error:
            pass
        rows.append({
            "frame": i,
            "t_s": round(i / fps, 4),
            "dx_px": round(float(dx), 4),
            "dy_px": round(float(dy), 4),
            "shift_px": round(float((dx ** 2 + dy ** 2) ** 0.5), 4),
            "phase_response": round(float(response), 5),
            "scale_xy": scale,
            "shear_xy": shear,
            "mean_abs_diff": round(float(np.abs(frame - reference).mean()), 4),
        })

    moved = [r for r in rows if r["shift_px"] > args.threshold_px]
    stretched = [r for r in rows if r["scale_xy"] and
                 max(abs(r["scale_xy"][0] - 1.0),
                     abs(r["scale_xy"][1] - 1.0)) > args.threshold_scale]
    report = {
        "video": args.video, "crop": crop, "fps": fps,
        "frames": len(frames),
        "threshold_px": args.threshold_px,
        "threshold_scale": args.threshold_scale,
        "first_moved_frame": moved[0] if moved else None,
        "moved_frame_count": len(moved),
        "moved_frames": [r["frame"] for r in moved],
        "first_stretched_frame": stretched[0] if stretched else None,
        "stretched_frame_count": len(stretched),
        "max_shift_px": max(r["shift_px"] for r in rows),
        "recovered": (bool(moved) and rows[-1]["shift_px"]
                      <= args.threshold_px),
        "per_frame": rows,
        "honesty": [
            "frames are NATURAL presented frames captured off the X server; "
            "nothing here forced the application to repaint",
            "lossless ffv1, so a geometric change cannot be an encoder artefact",
            "translation is sub-pixel phase correlation against frame 0; "
            "scale/shear is an ECC affine fit, `null` where the fit failed",
        ],
    }
    pathlib.Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in
                      ("frames", "max_shift_px", "moved_frame_count",
                       "first_moved_frame", "stretched_frame_count",
                       "recovered")}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("find")
    rec = sub.add_parser("record")
    rec.add_argument("--window", required=True)
    rec.add_argument("--seconds", type=int, default=15)
    rec.add_argument("--fps", type=int, default=30)
    rec.add_argument("--out", default="/tmp/step1_patch_wobble.mkv")
    ana = sub.add_parser("analyse")
    ana.add_argument("--video", required=True)
    ana.add_argument("--crop", default=None, help="X,Y,W,H of the Viewer")
    ana.add_argument("--fps", type=int, default=30)
    ana.add_argument("--threshold-px", type=float, default=0.5)
    ana.add_argument("--threshold-scale", type=float, default=0.002)
    ana.add_argument("--out", default="/tmp/step1_patch_wobble_frames.json")
    args = parser.parse_args()
    return {"find": find, "record": record, "analyse": analyse}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
