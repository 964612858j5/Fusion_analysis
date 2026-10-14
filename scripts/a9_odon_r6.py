"""Odon side of R6 (docs/v16_5_r6_motion_clarity_plan.md): the same strokes,
captured the same way (full canvas, a9_framecap) as our viewer.

Usage: a9_odon_r6.py DATA OUTDIR CHANNELS PATH_JSON MODE [SPEED]
  MODE ref   settled tiles of the path's reference cells -> OUTDIR/tiles
       cold  the scenario straight away (the caller drops the page cache)
       warm  the reference walk first (not saved), back to the start, then
             the scenario (plan §3 "warmed replay")
  SPEED      stroke pixels per 8 ms (S1: 10 / 20 / 40; S2 always 20)
PATH_JSON: {"name", "kind": "s1"|"s2", "scale", "start": [cx, cy],
  "jump_from": [cx, cy] (s2), "dx": +-900, "strokes": 4|2,
  "start_frac": [fx, fy], "ref_cells": [[cx, cy], ...]}
Writes OUTDIR/setup.json (window, canvas, channels, contrast, smooth),
OUTDIR/cap.{frames,json}, OUTDIR/inject.jsonl, OUTDIR/run.json.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import a9_odon_ctl as ctl  # noqa: E402
from a9_odon_bench import CANVAS_H, CANVAS_W, ODON, WIN_X, WIN_Y, find_window, wait  # noqa: E402
from a9_xgrab import Grabber  # noqa: E402

PY = sys.executable
BEFORE_S, AFTER_S = 0.5, 2.5
#: R6 step 0: Odon draws an 8 px frame left, right and below its picture;
#: the window is made that much larger so the picture itself is 1307 x 788,
#: the same as ours (whose widget frame is cut the same way)
FRAME_X, FRAME_B = 8, 8


def root_id():
    out = subprocess.run(["xwininfo", "-root"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "Window id:" in l][0].split()[3]


def canvas_rect(win):
    info = subprocess.run(["xwininfo", "-id", win], capture_output=True, text=True).stdout
    get = lambda k: int([l for l in info.splitlines() if k in l][0].split()[-1])  # noqa: E731
    ax, ay, wh = get("Absolute upper-left X"), get("Absolute upper-left Y"), get("Height:")
    return ax + FRAME_X, ay + wh - FRAME_B - CANVAS_H, CANVAS_W, CANVAS_H


def setup(data, outdir, channels, scale):
    proc = subprocess.Popen([ODON, data, "--log-level", "warn"],
                            stdout=open(os.path.join(outdir, "odon_stdout.txt"), "w"), stderr=subprocess.STDOUT)
    win = wait(find_window, 60)
    assert win, "no Odon window"
    wait(lambda: "camera" in ctl.call("get_camera")["result"], 60)
    ctl.call("set_side_panels", {"left": False, "right": False})
    time.sleep(0.5)
    subprocess.run([PY, os.path.join(HERE, "a9_xresize.py"), win, str(WIN_X), str(WIN_Y),
                    str(CANVAS_W + 2 * FRAME_X), str(CANVAS_H + 40 + FRAME_B)])
    time.sleep(1.0)
    shown = channels.split(",")
    log = {"window": win, "visible": ctl.call("set_visible_channels", {"channels": shown, "mode": "only"})}
    log["contrast"] = {c: ctl.call("set_channel_contrast", {"channel": c, "min": 0, "max": 255}) for c in shown}
    log["smooth"] = ctl.call("set_smooth_pixels", {"smooth": False})
    log["channels"] = ctl.call("list_channels")
    log["smooth_now"] = ctl.call("get_smooth_pixels")
    log["contrast_now"] = {c: ctl.call("get_channel_contrast", {"channel": c}) for c in shown}
    log["canvas"] = canvas_rect(win)
    json.dump(log, open(os.path.join(outdir, "setup.json"), "w"), indent=1)
    return proc, win, log["canvas"]


def idle():
    st = ctl.call("get_loading_state").get("result", {}).get("loading", {})
    return st.get("busy") is False and st.get("image_tiles", {}).get("in_flight_or_pending", 1) == 0


def settle_shot(grab, cx, cy, scale, timeout_s=15.0, still_s=1.0):
    """set_camera, then loading idle AND the canvas unchanged for still_s."""
    t0 = time.monotonic()
    ctl.call("set_camera", {"center_world_lvl0": [cx, cy], "zoom": scale})
    last, since, was_idle = None, time.monotonic(), False
    while True:
        raw = grab.grab_raw()
        now = time.monotonic()
        if raw != last:
            last, since = raw, now
        was_idle = idle()
        if (was_idle and now - since >= still_s) or now - t0 >= timeout_s:
            break
        time.sleep(0.1)
    cam = ctl.call("get_camera")["result"]["camera"]
    return grab.to_rgb(raw), {"camera": cam["center_world_lvl0"] + [cam["zoom_screen_per_lvl0_px"]],
                              "idle": was_idle, "still_s": round(now - since, 3),
                              "timed_out": not (was_idle and now - since >= still_s),
                              "elapsed_s": round(now - t0, 3)}


def walk(grab, path, save_dir=None):
    import cv2
    out = []
    for k, (cx, cy) in enumerate(path["ref_cells"]):
        rgb, meta = settle_shot(grab, cx, cy, path["scale"])
        meta["name"] = f"c{k:02d}"
        if save_dir:
            cv2.imwrite(os.path.join(save_dir, meta["name"] + ".png"), rgb[:, :, ::-1])
            json.dump(meta, open(os.path.join(save_dir, meta["name"] + ".json"), "w"))
        out.append(meta)
    return out


def pattern(path, speed):
    speed = 20 if path["kind"] == "s2" else int(speed)
    n = int(abs(path["dx"]) / speed)
    return ",".join([f"/{path['dx']}:0:{n}@8:50"] * int(path["strokes"])), n


def scenario(outdir, path, speed, canvas, root, win):
    pat, n = pattern(path, speed)
    total = int(path["strokes"]) * ((n + 1) * 0.008 + 0.05) + (0.1 if path["kind"] == "s2" else 0.0)
    secs = BEFORE_S + total + AFTER_S + 0.6
    if os.environ.get("A9_R6_NOCAP") == "1":     # §4 capture overhead check: no full capture
        cap = subprocess.Popen([PY, "-c", f"import time; time.sleep({secs})"])
    else:
        cap = subprocess.Popen([PY, os.path.join(HERE, "a9_framecap.py"), root, *map(str, canvas),
                                str(secs), os.path.join(outdir, "cap")])
    time.sleep(0.6)                                # the grabber is up
    t_first = time.monotonic() + BEFORE_S
    sx = int(canvas[0] + canvas[2] * path["start_frac"][0])
    sy = int(canvas[1] + canvas[3] * path["start_frac"][1])
    env = dict(os.environ, A9_XWHEEL_T0=repr(t_first + (0.1 if path["kind"] == "s2" else 0.0)))
    inj = os.path.join(outdir, "inject.jsonl")
    open(inj, "w").close()
    # the target window: delivery evidence in the log (on_target), as ours
    xw = subprocess.Popen([PY, os.path.join(HERE, "a9_xwheel.py"), str(sx), str(sy), pat, inj, win], env=env)
    run = {"pattern": pat, "start_xy": [sx, sy], "t_first": t_first}
    if path["kind"] == "s2":                        # plan §3: set_camera stands in for a click
        time.sleep(max(0.0, t_first - time.monotonic()))
        run["jump_issue"] = time.monotonic()
        run["jump_reply"] = ctl.call("set_camera", {"center_world_lvl0": path["start"], "zoom": path["scale"]})
        run["jump_ack"] = time.monotonic()
    xw.wait()
    cap.wait()
    run["camera_end"] = ctl.call("get_camera")["result"]["camera"]
    json.dump(run, open(os.path.join(outdir, "run.json"), "w"), indent=1)


def main(data, outdir, channels, path_json, mode, speed="20"):
    os.makedirs(outdir, exist_ok=True)
    path = json.load(open(path_json))
    proc, win, canvas = setup(data, outdir, channels, path["scale"])
    try:
        root = root_id()
        grab = Grabber(root, *canvas)
        time.sleep(2.0)
        if mode == "ref":
            os.makedirs(os.path.join(outdir, "tiles"), exist_ok=True)
            walk(grab, path, os.path.join(outdir, "tiles"))
            return
        if mode == "warm":
            json.dump(walk(grab, path), open(os.path.join(outdir, "warm_walk.json"), "w"))
        first = path["jump_from"] if path["kind"] == "s2" else path["start"]
        _rgb, meta = settle_shot(grab, first[0], first[1], path["scale"])
        json.dump(meta, open(os.path.join(outdir, "start.json"), "w"))
        scenario(outdir, path, speed, canvas, root, win)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main(*sys.argv[1:7])
