"""Odon side of the A9 §39 comparison: the SAME real-input gestures as our
`kx69` scenario, measured the SAME app-neutral way (a9_pixwatch).

Starts Odon on DATA, makes its canvas the size of our viewer (side panels
hidden), shows CHANNELS (all | comma list), puts the camera where our run
starts, waits for loading to settle, then plays GESTURES while a pixwatch
records every change of a few rows around the canvas centre. Writes into
OUTDIR: marks.tsv (t, label), xwheel.jsonl (all injections), changes.jsonl.

Usage: a9_odon_bench.py DATA OUTDIR CHANNELS GESTURES_JSON
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ODON = "/sda1/Fusion/analysis_pipline/odon/odon-app/usr/lib/odon/odon"
sys.path.insert(0, HERE)
import a9_odon_ctl as ctl  # noqa: E402

CANVAS_W, CANVAS_H = 1307, 788          # our viewer's ViewBox
WIN_X, WIN_Y = 40, 40


def find_window():
    out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if '"odon": ("odon" "odon")' in line:
            return line.split()[0]
    return None


def wait(cond, timeout, step=0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(step)
    return None


def main(data, outdir, channels, gestures_path):
    os.makedirs(outdir, exist_ok=True)
    gestures = json.load(open(gestures_path))
    proc = subprocess.Popen([ODON, data, "--log-level", "warn"],
                            stdout=open(os.path.join(outdir, "odon_stdout.txt"), "w"),
                            stderr=subprocess.STDOUT)
    try:
        win = wait(find_window, 60)
        assert win, "no Odon window"
        wait(lambda: ctl.call("get_camera").get("ok") and
             "camera" in ctl.call("get_camera")["result"], 60)
        ctl.call("set_side_panels", {"left": False, "right": False})
        time.sleep(0.5)
        # canvas = window minus the top bar: measure the bar once
        subprocess.run([PY, os.path.join(HERE, "a9_xresize.py"), win, str(WIN_X), str(WIN_Y),
                        str(CANVAS_W), str(CANVAS_H + 40)])
        time.sleep(1.0)
        names = [c["name"] if isinstance(c, dict) else c
                 for c in ctl.call("list_channels")["result"].get("channels", [])]
        shown = names if channels == "all" else channels.split(",")
        print(ctl.call("set_visible_channels", {"channels": shown, "mode": "only"}))
        cam = gestures["camera"]
        print(ctl.call("set_camera", {"center_world_lvl0": cam["center"], "zoom": cam["zoom"]}))
        # loading settles
        def idle():
            st = ctl.call("get_loading_state").get("result", {})
            return json.dumps(st).count("true") == 0 or st.get("busy") is False
        wait(idle, 120, 1.0)
        time.sleep(gestures.get("settle_s", 20))
        info = subprocess.run(["xwininfo", "-id", win], capture_output=True, text=True).stdout
        ax = int([l for l in info.splitlines() if "Absolute upper-left X" in l][0].split()[-1])
        ay = int([l for l in info.splitlines() if "Absolute upper-left Y" in l][0].split()[-1])
        wh = int([l for l in info.splitlines() if "Height:" in l][0].split()[-1])
        # the canvas is the bottom CANVAS_H rows of the window
        cx_win, cy_win = CANVAS_W // 2, wh - CANVAS_H // 2
        px, py = ax + cx_win, ay + cy_win
        total = sum(g.get("tail_ms", 3000) for g in gestures["gestures"]) / 1000.0 + 120
        watch = subprocess.Popen([PY, os.path.join(HERE, "a9_pixwatch.py"), win,
                                  str(cx_win - 150), str(cy_win - 200), "300", "5",
                                  str(total), os.path.join(outdir, "changes.jsonl")])
        time.sleep(1.0)
        marks = open(os.path.join(outdir, "marks.tsv"), "w")
        inj = os.path.join(outdir, "xwheel.jsonl")
        open(inj, "w").close()
        for g in gestures["gestures"]:
            marks.write(f"{time.monotonic():.6f}\t{g['label']}\n")
            marks.flush()
            part = os.path.join(outdir, "part.jsonl")
            subprocess.run([PY, os.path.join(HERE, "a9_xwheel.py"), str(px), str(py),
                            g["pattern"], part])
            with open(inj, "a") as f:
                f.write(open(part).read())
            time.sleep(g.get("tail_ms", 3000) / 1000.0)
        marks.write(f"{time.monotonic():.6f}\tdone\n")
        marks.close()
        watch.terminate()
        watch.wait()
        print(json.dumps({"window": win, "point": [px, py], "canvas_centre_in_window": [cx_win, cy_win]}))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main(*sys.argv[1:5])
