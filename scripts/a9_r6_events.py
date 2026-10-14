"""R6 plan §4: one run's numbers -- not-final area over time, blur-to-sharp
pops, new edges, lag and freezes (docs/v16_5_r6_motion_clarity_plan.md).

Usage: a9_r6_events.py CAPTURE_PREFIX MOSAIC_PREFIX INJECT_LOG OUT_JSON [PARAMS_JSON]

PARAMS (step 0 calibration; defaults are placeholders):
  ratio_fail  a block fails when its detail ratio is under this ...
  ssim_fail   ... or its SSIM is under this (or it is blank / black / saturated)
  pop_jump    a pop: the detail ratio rises by at least max(0.2, pop_jump)
              between two consecutive observations of the block (a slow
              rise in small steps is not one); "blurry_for_ms" is how long
              the block had been in its pass/fail state before
  gap_ms      consecutive captures further apart than this make a pop
              "possible" instead of definite (plan: 25 ms)
  min_score   registration score under this: unlocatable
  detail_min  reference detail energy for a block to be scored
  ch_detail_min  a colour channel's own detail for its ratio to count
  pop_from_fail  a pop must start from a failing block (blur -> sharp)
  jump_t      Odon S2: when set_camera was issued (no click in its log;
              read from run.json beside the capture when not given)
  dest_l0     S2: the jump's level-0 centre, to find the real arrival
"""
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import a9_r6_ref as R  # noqa: E402
import a9_r6_score as SC  # noqa: E402

CANVAS = (1307, 788)        # the picture area both programs are captured from
mosaic_prefix_of = [None]   # set by run(): _summary loads the reference for the destination

DEFAULTS = {"ratio_fail": 0.75, "ssim_fail": 0.9, "pop_jump": 0.2, "gap_ms": 25.0, "min_score": 0.5,
            "detail_min": 4.0, "pop_from_fail": True, "ch_detail_min": 8.0}


def phases(inject, jump_t=None):
    """[(t0, t1, name)] from the injector log: "stroke" while the button is
    held (first move .. release), "return" from a release to the next
    stroke's first move, "after" from the last release on."""
    rows = [json.loads(l) for l in open(inject) if l.strip()]
    out, start, last_rel = [], None, None
    for r in rows:
        if r["kind"] == "move" and start is None:
            if last_rel is not None:
                out.append((last_rel, r["t"], "return"))
            start = r["t"]
        elif r["kind"] in ("release_end", "release") and start is not None:
            out.append((start, r["t"], "stroke"))
            start, last_rel = None, r["t"]
    if last_rel is not None:
        out.append((last_rel, float("inf"), "after"))
    jump = [r["t"] for r in rows if r["kind"] == "click"] or ([jump_t] if jump_t else [])
    switch = [r["t"] for r in rows if r["kind"] == "switch"]
    moves = [r["t"] for r in rows if r["kind"] == "move"]
    if jump and moves:                  # S2: from the click to the first move
        out.insert(0, (jump[0], moves[0], "jump"))
    if switch and moves:                # S3: from the step change / Step1 jump to the first move
        out.insert(0, (switch[0], moves[0], "back"))
        jump = jump or switch
    return out, rows, (jump[0] if jump else None)


def phase_of(ph, t):
    for a, b, name in ph:
        if a <= t < b:
            return name
    return "before"


class Tracker:
    """Each mosaic block followed in tissue coordinates (R6 §4, §12.5 item 2).

    An unchanged capture is an observation too (the block is still on
    screen, as it was). A block's EPISODE is an unbroken stretch in view:
    one that left the view and came back starts a new episode and is never
    compared with its value from before it left. Between two observations
    only unlocatable frames (nothing could be observed) keep the episode
    going, and make a pop across them "possible" rather than definite; they
    never count as the block having left. Edge outcomes are per episode."""

    def __init__(self, p):
        self.p, self.state, self.pops, self.episodes = p, {}, [], []
        self.unlocated = set()
        self.first_i = None

    def _close(self, v, how):
        self.episodes.append({"start": v["ep_start"], "pass": v["ep_pass"], "initial": v["ep_initial"],
                              "end_bad": v["bad"], "how": how})

    def observe(self, i, t, gap_ms, phase, obs):
        if self.first_i is None:
            self.first_i = i
        ups = []
        jump = max(0.2, self.p["pop_jump"])
        for r, c, q, bad in obs:
            key = (r, c)
            old = self.state.get(key)
            cont = old is not None and (old["i"] == i - 1 or all(
                k in self.unlocated for k in range(old["i"] + 1, i)))
            if old is not None and not cont:
                self._close(old, "left")
            # pop_from_fail: only a block that was failing can pop (blur to sharp)
            if cont and q - old["q"] >= jump and (old["bad"] or not self.p["pop_from_fail"]):
                definite = old["i"] == i - 1 and (gap_ms or 0) <= self.p["gap_ms"]
                ups.append((r, c, q - old["q"], definite, t - old["since"]))
            if cont:
                v = old
                v["since"] = old["since"] if old["bad"] == bad else t
            else:
                v = {"ep_start": t, "ep_pass": None, "ep_initial": i == self.first_i, "since": t}
            if not bad and v["ep_pass"] is None:
                v["ep_pass"] = t
            v.update(i=i, q=q, bad=bad)
            self.state[key] = v
        if ups:
            self.pops.extend(_events(ups, i, t, phase))

    def finish(self, last_i):
        for v in self.state.values():
            if v["i"] == last_i:
                how = "end"
            elif all(k in self.unlocated for k in range(v["i"] + 1, last_i + 1)):
                how = "unknown"               # not seen leaving: visibility unknown
            else:
                how = "left"
            self._close(v, how)
        self.state = {}
        return self.episodes


def run(cap_prefix, mosaic_prefix, inject, params):
    p = dict(DEFAULTS, **params)
    SC.MIN_SCORE, SC.DETAIL_MIN, SC.CH_DETAIL_MIN = p["min_score"], p["detail_min"], p["ch_detail_min"]
    mosaic_prefix_of[0] = mosaic_prefix
    mosaic = R.load(mosaic_prefix)
    if not mosaic.get("ok", False):          # R6 §12.5 item 1: no scoring on a bad reference
        raise SystemExit(f"reference {mosaic_prefix} is not ok: retake it")
    cap = SC.Capture(cap_prefix, mosaic.get("crop"))
    ref = SC.Reference(mosaic)
    reg = SC.Registrar(ref)
    ph, rows, jump_t = phases(inject, p.get("jump_t"))
    tr = Tracker(p)
    frames, last, last_obs = [], None, None
    worst = (-1.0, None)
    for i in range(len(cap)):
        t = cap.t(i)
        phase, gap = phase_of(ph, t), cap.gap_ms(i)
        if last is not None and cap.same(i, i - 1):
            f = dict(last, t=t, i=i, same=True, phase=phase, gap_ms=gap)
            frames.append(f)
            if last_obs is not None:                     # still on screen, unchanged
                tr.observe(i, t, gap, phase, last_obs)
            else:
                tr.unlocated.add(i)
            continue
        rgb = cap.rgb(i)
        loc = reg.locate(rgb, t)
        f = {"i": i, "t": t, "same": False, "xy": loc["xy"], "score": loc["score"], "ok": loc["ok"],
             "phase": phase, "gap_ms": gap}
        last_obs = None
        b = SC.block_scores(ref, rgb, loc["xy"]) if loc["ok"] else None
        if b is not None:
            rr, cc, ratio, ssim, blank = b
            fail = (ratio < p["ratio_fail"]) | (ssim < p["ssim_fail"]) | blank
            f["n"], f["fail"] = int(len(rr)), int(fail.sum())
            f["fail_frac"] = f["fail"] / max(1, f["n"])
            if f["fail_frac"] > worst[0]:
                worst = (f["fail_frac"], i)
            # quality = detail ratio capped at 1: "sharper than the settled
            # picture" is interpolation noise, not a gain
            last_obs = list(zip(rr.tolist(), cc.tolist(), np.minimum(ratio, 1.0).tolist(), fail.tolist()))
            tr.observe(i, t, gap, phase, last_obs)
        else:
            tr.unlocated.add(i)
        frames.append(f)
        last = f
    return _summary(cap, frames, tr, ph, rows, jump_t, worst, p)


def _events(ups, i, t, phase):
    """Adjacent pop blocks of one frame change -> one event."""
    grid = {(r, c): k for k, (r, c, *_r) in enumerate(ups)}
    seen, out = set(), []
    for key in grid:
        if key in seen:
            continue
        stack, members = [key], []
        seen.add(key)
        while stack:
            r, c = stack.pop()
            members.append(ups[grid[(r, c)]])
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    k2 = (r + dr, c + dc)
                    if k2 in grid and k2 not in seen:
                        seen.add(k2)
                        stack.append(k2)
        out.append({"i": i, "t": t, "phase": phase, "blocks": len(members),
                    "rise": round(float(np.median([m[2] for m in members])), 3),
                    "definite": all(m[3] for m in members),
                    "blurry_for_ms": round(1000 * float(np.median([m[4] for m in members])), 1)})
    return out


def _summary(cap, frames, tr, ph, rows, jump_t, worst, p):
    pops = tr.pops
    ts = np.array([f["t"] for f in frames])
    dur = np.diff(np.append(ts, ts[-1] + (ts[-1] - ts[-2] if len(ts) > 1 else 0.0167)))
    out = {"params": p, "frames": len(frames), "phases": {}}
    for name in ("before", "jump", "back", "stroke", "return", "after"):
        sel = [k for k, f in enumerate(frames) if f.get("phase", phase_of(ph, f["t"])) == name]
        if not sel:
            continue
        w = dur[sel]
        loc = np.array([frames[k]["ok"] and "fail_frac" in frames[k] for k in sel])
        ff = np.array([frames[k].get("fail_frac", 0.0) for k in sel])
        lo = float((ff * loc * w).sum() / w.sum())
        hi = float(((ff * loc + (~loc)) * w).sum() / w.sum())
        ev = [e for e in pops if e["phase"] == name]
        out["phases"][name] = {
            "seconds": round(float(w.sum()), 3), "unlocatable_share": round(float(((~loc) * w).sum() / w.sum()), 4),
            "fail_area_tw": [round(lo, 4), round(hi, 4)],
            "worst_frame_fail": round(max([frames[k].get("fail_frac", 0.0) for k in sel]), 4),
            "pops_definite": sum(e["definite"] for e in ev), "pops_possible": sum(not e["definite"] for e in ev),
            "pop_blocks": sum(e["blocks"] for e in ev)}
    out["pops"] = pops
    # new edges, per episode: a block that came into view after the first
    # located frame, from then until it first passed (R6 §4 item 5)
    eps = tr.finish(len(frames) - 1)
    new = [e for e in eps if not e["initial"]]
    delays = [e["pass"] - e["start"] for e in new if e["pass"] is not None]
    out["edges"] = {"episodes": len(new), "initial_episodes": len(eps) - len(new),
                    "delay_ms_p50_p95_max": [round(1000 * float(np.percentile(delays, q)), 1)
                                             for q in (50, 95, 100)] if delays else None,
                    "never_sharp": sum(1 for e in new if e["pass"] is None),
                    "blurry_at_end": sum(1 for e in eps if e["end_bad"] and e["how"] == "end"),
                    "left_view_blurry": sum(1 for e in eps if e["end_bad"] and e["how"] == "left"),
                    "blurry_visibility_unknown": sum(1 for e in eps if e["end_bad"] and e["how"] == "unknown")}
    # lag: the displayed movement vs the hand's pressed movement
    out["lag"] = _lag(frames, rows)
    # freezes during strokes: identical captures in a row
    out["freeze_ms"] = _freezes(frames, ts, [(a, b) for a, b, n in ph if n == "stroke"])
    gaps = np.diff(np.array([cap.meta["t0"][k] for k in range(len(cap))])) * 1000.0
    acts = [r["t"] for r in rows if r["kind"] in ("move", "click", "hover", "release_end", "notch", "switch")]
    first_act = min(acts + ([jump_t] if jump_t else []))
    # R6 §12.5 item 5: the capture must start >= 0.5 s before the first
    # action and end >= 2.5 s after the last one
    out["capture"] = {"gap_p50_p95_max": [round(float(np.percentile(gaps, q)), 2) for q in (50, 95, 100)],
                      "over_25ms": round(float((gaps > 25).mean()), 4),
                      "lead_s": round(first_act - cap.meta["t0"][0], 3),
                      "tail_s": round(cap.meta["t1"][-1] - max(acts), 3),
                      "covers": bool(first_act - cap.meta["t0"][0] >= 0.5 and
                                     cap.meta["t1"][-1] - max(acts) >= 2.5)}
    moves = [r["t"] for r in rows if r["kind"] == "move"]
    if jump_t and moves:                  # §12.5 item 4: the real first motion
        out["first_move_after_jump_ms"] = round(1000 * (moves[0] - jump_t), 1)
        # plan §3 S2 item 3: when the picture really arrived -- the first
        # capture after the jump that is located AT THE DESTINATION (params
        # "dest_l0": the jump's level-0 centre, turned into this reference's
        # pixels) and stays there until the first move; the same way for
        # both programs
        dest, tol = None, 3.0
        # ours: the Tissue Preview click lands at the preview's resolution,
        # not at the nominal target -- the destination is where the
        # program's own jump record (a9.camera kind=jump, Step1) put it;
        # that record prints 4 significant digits, hence the wider tolerance
        run_log = os.path.join(os.path.dirname(cap.prefix), "run.log")
        jumps = []
        if os.path.exists(run_log):
            import re
            jumps = [(float(a), float(b), float(c)) for a, b, c in re.findall(
                r"t=([\d.]+) ev=a9.camera kind=jump step=1 scale=[\d.e+-]+ cx=([\d.e+-]+) cy=([\d.e+-]+)",
                open(run_log).read()) if float(a) >= jump_t - 0.05]
        run_json = os.path.join(os.path.dirname(cap.prefix), "run.json")
        reply = None
        if os.path.exists(run_json):     # Odon: the camera its set_camera answered with
            try:
                reply = json.load(open(run_json))["jump_reply"]["result"]["camera"]["center_world_lvl0"]
            except (KeyError, TypeError):
                reply = None
        if jumps:
            p = dict(p, dest_l0=[jumps[0][1], jumps[0][2]])
            tol = 10.0
            out["destination_from"] = "own jump record"
        elif reply:
            p = dict(p, dest_l0=list(reply))
            out["destination_from"] = "own jump reply"
        elif p.get("dest_l0"):
            out["destination_from"] = "target"
        if p.get("dest_l0"):
            m = R.load(mosaic_prefix_of[0])
            x0, y0, _w, _h = m["crop"]
            dest = (p["dest_l0"][0] * m["scale"] - CANVAS[0] / 2.0 + x0 - m["origin"][0] * m["scale"],
                    p["dest_l0"][1] * m["scale"] - CANVAS[1] / 2.0 + y0 - m["origin"][1] * m["scale"])
        arr, seen_any, gap_after_seen = None, False, False
        if dest is not None:
            for k, f in enumerate(frames):
                if f["t"] <= jump_t or f["t"] >= moves[0]:
                    continue
                if not f.get("ok"):
                    gap_after_seen = gap_after_seen or seen_any
                    continue
                seen_any = True
                if abs(f["xy"][0] - dest[0]) > tol or abs(f["xy"][1] - dest[1]) > tol:
                    continue
                rest = [g for g in frames[k:] if g["t"] < moves[0]]
                if all(g.get("ok") and g.get("xy") and abs(g["xy"][0] - f["xy"][0]) < 2
                       and abs(g["xy"][1] - f["xy"][1]) < 2 for g in rest):
                    arr = f["t"]
                    break
        out["arrival_checked_against_destination"] = dest is not None
        out["arrival_after_jump_ms"] = None if arr is None else round(1000 * (arr - jump_t), 1)
        out["arrival_to_first_move_ms"] = None if arr is None else round(1000 * (moves[0] - arr), 1)
        # not seen arriving is not the same as moving before arriving: no
        # located frame at all in between is "unobserved"
        if dest is None:
            out["arrival"] = "unchecked"          # no destination: a stable picture proves nothing
        elif arr is not None:
            out["arrival"] = "observed"
        elif not seen_any:
            out["arrival"] = "unobserved"
        elif gap_after_seen:
            out["arrival"] = "uncertain"          # frames that could not be located in between
        else:
            out["arrival"] = "not before the first move"
    out["worst_frame"] = worst[1]
    out["jump_t"] = jump_t
    return out, frames


def _lag(frames, rows):
    moves = [r for r in rows if r["kind"] in ("move", "hover", "release_end")]
    if not moves:
        return None
    ht, acc, prev, pressed, hand = [], np.zeros(2), None, False, []
    for r in rows:
        if r["kind"] == "hover":
            prev = np.array(r["xy"], float)
            continue
        if r["kind"] == "move" and prev is not None:
            xy = np.array(r["xy"], float)
            acc = acc + (xy - prev)
            prev = xy
            ht.append(r["t"])
            hand.append(acc.copy())
    if not ht:
        return None
    ht, hand = np.array(ht), np.array(hand)
    # every capture counts, unchanged ones too: lag grows during a freeze
    loc = [f for f in frames if f.get("ok") and f.get("xy") is not None]
    t_first = ht[0]
    base = [f for f in loc if f["t"] < t_first]
    if not base:
        return {"note": "no located frame before the first move"}
    x0 = np.array(base[-1]["xy"])
    lags = []
    for f in loc:
        if f["t"] < t_first or f.get("phase") not in ("stroke",):
            continue
        k = np.searchsorted(ht, f["t"]) - 1
        h = hand[k] if k >= 0 else np.zeros(2)
        shown = x0 - np.array(f["xy"])           # the picture follows the hand
        lags.append(float(np.hypot(*(h - shown))))
    return {"stroke_lag_px_p50_p95_max": [round(float(np.percentile(lags, q)), 1) for q in (50, 95, 100)]
            if lags else None}


def _freezes(frames, ts, strokes):
    """Per stroke (button held), clipped to its own start and end: the time
    between picture CHANGES, from capture timestamps -- the stroke's start,
    every capture that changed the picture, the stroke's end. An interval
    longer than 25 ms is a freeze; a stroke with no change at all is one
    freeze as long as the stroke."""
    changes = [t for f, t in zip(frames, ts) if not f["same"]]
    runs = []
    for a, b in strokes:
        pts = [a] + [t for t in changes if a < t < b] + [b]
        runs += [y - x for x, y in zip(pts, pts[1:]) if y - x > 0.025]
    return {"total": round(1000 * sum(runs), 1), "longest": round(1000 * max(runs), 1) if runs else 0.0,
            "over_50ms": sum(r > 0.05 for r in runs)}


def main():
    cap, mos, inj, out = sys.argv[1:5]
    params = json.loads(open(sys.argv[5]).read()) if len(sys.argv) > 5 else {}
    # Odon's S2 jump is a control call: its time is in run.json beside the capture
    run_json = os.path.join(os.path.dirname(cap), "run.json")
    if "jump_t" not in params and os.path.exists(run_json) and json.load(open(run_json)).get("jump_issue"):
        params["jump_t"] = json.load(open(run_json))["jump_issue"]
    summary, frames = run(cap, mos, inj, params)
    json.dump(summary, open(out, "w"), indent=1, default=float)
    with open(out.replace(".json", "") + ".frames.jsonl", "w") as fh:
        for f in frames:
            fh.write(json.dumps(f, default=float) + "\n")
    if summary["worst_frame"] is not None:
        c = SC.Capture(cap, R.load(mos).get("crop"))
        cv2.imwrite(out.replace(".json", "") + ".worst.png", c.rgb(summary["worst_frame"])[:, :, ::-1])
    print(json.dumps({k: summary[k] for k in ("phases", "edges", "lag", "freeze_ms", "capture")}, default=float))
    if not summary["capture"]["covers"]:
        sys.exit(3)                            # a measurement failure (§12.5 item 5)


if __name__ == "__main__":
    main()
