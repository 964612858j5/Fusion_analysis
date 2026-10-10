"""§41/§44: a window's picture at fixed times after each gesture's stop
(is the switch to the sharp picture visible?).

§44 step 0 (independent review): the stop is now the injector's PLANNED
moment of the last injection, read from INJECTIONS_JSONL.plan before the
gesture starts -- the old version first waited for 50 ms without input, so
its "20 ms" shot was really taken ~52 ms after the stop. Grabs go through
MIT-SHM (a9_xgrab: ~2 ms instead of 19-42 ms for the full canvas), so the
shots at 0-50 ms are real. The last shot (REFERENCE_MS) is the reference
the others are compared with (a9_shotdiff); whether the picture was by then
really the target level is checked separately (a9_tiletl for ours, the
loading state for Odon).

Usage: a9_stopshots.py INJECTIONS_JSONL WINDOW_ID OUTDIR NGESTURES [X,Y,W,H]
Writes OUTDIR/g<G>_<NNNN>ms.png and OUTDIR/shots.json (per shot: nominal
delay, time taken, offset from the planned stop and from the real last
injection, grab cost)."""
import json
import os
import sys
import time

from PIL import Image

from a9_xgrab import Grabber

DELAYS_MS = tuple(int(v) for v in os.environ.get(
    "A9_SHOT_DELAYS", "0,8,16,25,33,42,50,67,83,100,133,160,250,400,800,1500,2300").split(","))


def lines(path):
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except OSError:
        return []


def real_last(injections, plan):
    """The real time of the last injection of the run a plan line announced
    (runs start at seq == 1; the run's first injection is at the plan's
    start, give or take the injector's lateness)."""
    runs, current = [], None
    for r in injections:
        if r.get("seq") == 1:
            current = []
            runs.append(current)
        if current is not None:
            current.append(r["t"])
    match = [run for run in runs if abs(run[0] - plan["start"]) < 0.25]
    return match[0][-1] if match else None


def main(inj_path, win, outdir, ngest, region="0,0,1307,788"):
    os.makedirs(outdir, exist_ok=True)
    plan_path = inj_path + ".plan"
    # the plan file only grows: gestures planned before we started belong
    # to an earlier run of the same log (counted before anything else)
    base = len(lines(plan_path))
    rx, ry, rw, rh = map(int, region.split(","))
    grabber = Grabber(win, rx, ry, rw, rh)
    grabber.grab_raw()                                # first call pays the setup
    give_up = time.monotonic() + float(os.environ.get("A9_SHOTS_TIMEOUT_S", "1800"))
    taken, plans = [], {}
    for g in range(1, int(ngest) + 1):
        while len(lines(plan_path)) < base + g and time.monotonic() < give_up:
            time.sleep(0.002)
        if len(lines(plan_path)) < base + g:
            print("a9_stopshots: gave up waiting for gesture", g, flush=True)
            break
        plans[g] = lines(plan_path)[base + g - 1]
        end = plans[g]["end"]
        for d in DELAYS_MS:
            target = end + d / 1000.0
            while True:
                left = target - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.0005) if left < 0.003 else left - 0.002)
            t = time.monotonic()
            raw = grabber.grab_raw()
            cost = (time.monotonic() - t) * 1000.0
            taken.append({"g": g, "ms": d, "t": t, "plan_end": end,
                          "grab_ms": round(cost, 2), "raw": raw, "stride": grabber.stride})
            print(g, d, round((t - end) * 1000, 1), round(cost, 1), flush=True)
    injections = lines(inj_path)
    meta = []
    for shot in taken:
        last = real_last(injections, plans[shot["g"]])
        name = f"g{shot['g']}_{shot['ms']:04d}ms.png"
        Image.fromarray(grabber.to_rgb(shot.pop("raw"), shot.pop("stride"))).save(
            os.path.join(outdir, name))
        shot.update(file=name,
                    from_plan_ms=round((shot["t"] - shot["plan_end"]) * 1000.0, 2),
                    from_last_ms=None if last is None else round((shot["t"] - last) * 1000.0, 2))
        meta.append(shot)
    with open(os.path.join(outdir, "shots.json"), "w") as out:
        json.dump(meta, out, indent=0)


if __name__ == "__main__":
    main(*sys.argv[1:6])
