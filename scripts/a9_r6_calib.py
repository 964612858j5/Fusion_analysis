"""R6 plan §6 item 5: calibrate the ruler on calibration positions only.

Usage: a9_r6_calib.py SETTLED_DIR MOSAIC_PREFIX OUT_JSON [PARAMS_JSON]
  SETTLED_DIR: settled shots (NAME.png + NAME.json with its camera) taken at
  positions that are NOT the mosaic's tiles (plan: 20 positions).

For one app and one calibration set, with the given parameters:
  registration  each settled shot located on the mosaic, against where its
                camera says it is (error after removing the common offset;
                plan: < 0.5 px)
  settled       share of failing blocks on settled shots (plan: <= 2 %)
  degraded      settled shots made 2x / 4x coarser (down, then up, on whole
                pixels); share of scorable blocks detected (plan: >= 80 %)
  motion        synthetic motion cut from the mosaic at 1250 / 2500 / 5000
                screen px per second, subpixel positions: failing share and
                pops (plan: <= 5 %, pops about 0)
  blends        a degraded shot turning into the settled one linearly over
                100 / 200 / 300 ms at 60 per second: pops counted (plan: a
                gradual change is not a pop; reported per duration)
"""
import glob
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import a9_r6_events as EV  # noqa: E402
import a9_r6_ref as R  # noqa: E402
import a9_r6_score as SC  # noqa: E402


def shots(settled_dir, crop):
    out = []
    for p in sorted(glob.glob(os.path.join(settled_dir, "*.json"))):
        m = json.load(open(p))
        if R._timed_out(m) or not m.get("camera"):
            continue
        rgb = cv2.imread(p[:-5] + ".png")[:, :, ::-1]
        H0, W0 = rgb.shape[:2]
        x, y, w, h = crop
        out.append((m, rgb[y:y + h, x:x + w].copy(), (W0, H0)))
    return out


def degrade(rgb, k):
    h, w = rgb.shape[:2]
    h2, w2 = h // k * k, w // k * k
    out = rgb.copy()
    small = cv2.resize(rgb[:h2, :w2], (w2 // k, h2 // k), interpolation=cv2.INTER_AREA)
    out[:h2, :w2] = cv2.resize(small, (w2, h2), interpolation=cv2.INTER_LINEAR)
    return out


def fails(ref, rgb, xy, p):
    b = SC.block_scores(ref, rgb, xy)
    if b is None:
        return 0, 0
    _rr, _cc, ratio, ssim, blank = b
    bad = (ratio < p["ratio_fail"]) | (ssim < p["ssim_fail"]) | blank
    return int(bad.sum()), int(len(bad))


def crop_at(mos, x, y, w, h):
    """A frame of the mosaic at a fractional position, cut at the nearest
    whole pixel: a program draws each frame afresh, it does not resample a
    picture, so synthetic motion is not interpolated either."""
    ix, iy = int(round(x)), int(round(y))
    return mos[iy:iy + h, ix:ix + w].copy()


def tracker_pops(ref, frames, p):
    """Run the Tracker of a9_r6_events over (rgb, xy) at 60 per second."""
    tr = EV.Tracker(p)
    for i, (rgb, xy) in enumerate(frames):
        b = SC.block_scores(ref, rgb, xy)
        if b is None:
            tr.unlocated.add(i)
            continue
        rr, cc, ratio, ssim, blank = b
        bad = (ratio < p["ratio_fail"]) | (ssim < p["ssim_fail"]) | blank
        tr.observe(i, i / 60.0, 16.7, "stroke",
                   list(zip(rr.tolist(), cc.tolist(), np.minimum(ratio, 1.0).tolist(), bad.tolist())))
    return sum(e["blocks"] for e in tr.pops), len(tr.pops)


def run(settled_dir, mosaic_prefix, params):
    p = dict(EV.DEFAULTS, **params)
    SC.MIN_SCORE, SC.DETAIL_MIN, SC.CH_DETAIL_MIN = p["min_score"], p["detail_min"], p["ch_detail_min"]
    m = R.load(mosaic_prefix)
    ref = SC.Reference(m)
    crop = m["crop"]
    out = {"params": p, "scorable_blocks": int(ref.mask.sum())}
    # registration and settled shots
    errs, sett, deg = [], [0, 0], {2: [0, 0], 4: [0, 0]}
    found = []
    for meta, rgb, (W0, H0) in shots(settled_dir, crop):
        reg = SC.Registrar(ref)
        loc = reg.locate(rgb, 0.0)
        cx, cy, sc = meta["camera"]
        pred = np.array([cx * sc - W0 / 2.0 + crop[0], cy * sc - H0 / 2.0 + crop[1]]) - np.array(m["origin"]) * sc
        if not loc["ok"]:
            errs.append(None)
            continue
        errs.append((np.array(loc["xy"]) - pred).tolist())
        found.append((rgb, loc["xy"]))
        f, n = fails(ref, rgb, loc["xy"], p)
        sett[0] += f
        sett[1] += n
        for k in (2, 4):
            f, n = fails(ref, degrade(rgb, k), loc["xy"], p)
            deg[k][0] += f
            deg[k][1] += n
    good = np.array([e for e in errs if e is not None])
    if len(good):
        resid = good - np.median(good, axis=0)       # camera read-backs share an offset
        out["registration"] = {"shots": len(errs), "located": len(good),
                               "offset_px": np.median(good, axis=0).round(3).tolist(),
                               "max_abs_err_px": float(np.abs(resid).max().round(3))}
    out["settled_fail"] = round(sett[0] / max(1, sett[1]), 4)
    out["degraded_detected"] = {k: round(v[0] / max(1, v[1]), 4) for k, v in deg.items()}
    # synthetic motion along the mosaic's long axis
    mos = m["rgb"]
    h, w = crop[3], crop[2]
    H, W = mos.shape[:2]
    out["motion"] = {}
    for speed in (1250, 2500, 5000):
        # codex step-0 gate: the whole chain -- each frame is LOCATED by the
        # Registrar (as a real capture is), not handed its true position
        frames, errs = [], []
        x, y = W - w - 3.37, (H - h) / 2.0 + 0.41
        reg = SC.Registrar(ref)
        k = 0
        while x > 3:
            rgb = crop_at(mos, x, y, w, h)
            loc = reg.locate(rgb, k / 60.0)
            truth = [round(x), round(y)]                 # crop_at cuts at the nearest whole pixel
            if loc["ok"]:
                frames.append((rgb, loc["xy"]))
                errs.append(max(abs(loc["xy"][0] - truth[0]), abs(loc["xy"][1] - truth[1])))
            x -= speed / 60.0
            k += 1
        f = n = 0
        for rgb, xy in frames:
            a, b = fails(ref, rgb, xy, p)
            f, n = f + a, n + b
        pb, pe = tracker_pops(ref, frames, p)
        out["motion"][speed] = {"frames": k, "located": len(frames), "fail": round(f / max(1, n), 4),
                                "reg_err_max_px": round(float(max(errs or [0])), 3),
                                "pop_blocks": pb, "pop_events": pe}
    # blends: degraded (4x) to settled, linear, at 60 per second
    out["blends"] = {}
    for ms in (100, 200, 300):
        steps = max(2, int(round(ms / 1000.0 * 60)))
        pb = pe = 0
        for rgb, xy in found[:5]:
            d = degrade(rgb, 4).astype(np.float32)
            s = rgb.astype(np.float32)
            seq = [(np.clip(d + (s - d) * k / steps, 0, 255).astype(np.uint8), xy) for k in range(steps + 1)]
            a, b = tracker_pops(ref, [(d.astype(np.uint8), xy)] * 3 + seq + [(rgb, xy)] * 3, p)
            pb, pe = pb + a, pe + b
        out["blends"][ms] = {"pop_blocks": pb, "pop_events": pe}
    # the same sharp-in-one-frame change, for scale: a pop of 4x -> settled
    pb = pe = 0
    for rgb, xy in found[:5]:
        a, b = tracker_pops(ref, [(degrade(rgb, 4), xy)] * 3 + [(rgb, xy)] * 3, p)
        pb, pe = pb + a, pe + b
    out["step_change"] = {"pop_blocks": pb, "pop_events": pe}
    return out


if __name__ == "__main__":
    params = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
    res = run(sys.argv[1], sys.argv[2], params)
    json.dump(res, open(sys.argv[3], "w"), indent=1, default=float)
    print(json.dumps(res, default=float))
