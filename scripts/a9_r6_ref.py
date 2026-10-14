"""R6 plan §4 reference mosaic (docs/v16_5_r6_motion_clarity_plan.md).

Each settled tile NAME.png comes with NAME.json holding the camera it was
taken at (`camera`: [cx, cy, scale] -- level-0 centre, screen px per level-0
px; the same for our viewer and Odon). Tiles are placed by their cameras,
then every placement is corrected by registering each tile against the
tiles it overlaps (translation, subpixel), so a camera read-back rounded by
either program does not leave a seam; each tile is then put down at the
nearest whole pixel, never resampled (step 0). The mosaic is in SCREEN pixels at the
run's scale: mosaic (u, v) = (level-0 - origin) * scale.

Seam check (plan §4): in every overlap the two tiles' mean absolute
difference after the correction must stay under `SEAM_MAD` (grey levels);
a tile that fails is reported and the caller retakes it.

    m = build(tile_dir, crop)  # {"rgb", "origin", "scale", "tiles", "seams"}

CROP (x, y, w, h), canvas pixels: the part of every tile used, the same
for both programs (step 0: Odon draws a border, a moving text line and a
scale bar inside its canvas; they are cut away on both sides).
    save(m, out_prefix) / load(out_prefix)
"""
import glob
import json
import os

import cv2
import numpy as np

SEAM_MAD = 6.0          # grey levels, 0-255 luminance; checked in step 0
MIN_OVERLAP = 0.25


def luma(rgb):
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)


def subpixel_peak(res, loc):
    """Parabolic refinement of a correlation peak at integer `loc` (x, y)."""
    x, y = loc
    dx = dy = 0.0
    if 0 < x < res.shape[1] - 1:
        l, c, r = res[y, x - 1], res[y, x], res[y, x + 1]
        d = l - 2 * c + r
        dx = 0.5 * (l - r) / d if d < 0 else 0.0
    if 0 < y < res.shape[0] - 1:
        u, c, b = res[y - 1, x], res[y, x], res[y + 1, x]
        d = u - 2 * c + b
        dy = 0.5 * (u - b) / d if d < 0 else 0.0
    return x + float(np.clip(dx, -0.5, 0.5)), y + float(np.clip(dy, -0.5, 0.5))


def _pair_shift(a, b, guess, pad=12):
    """Where tile b sits relative to tile a (b's top-left in a's pixels),
    refined around `guess`; returns (dx, dy, score, overlap_box)."""
    gx, gy = int(round(guess[0])), int(round(guess[1]))
    h, w = a.shape
    x0, y0 = max(0, gx), max(0, gy)
    x1, y1 = min(w, gx + b.shape[1]), min(h, gy + b.shape[0])
    if (x1 - x0) * (y1 - y0) < MIN_OVERLAP * 0.5 * w * h:
        return None
    # template: b's part of the overlap, shrunk by pad; search: a's overlap
    tb = b[y0 - gy + pad:y1 - gy - pad, x0 - gx + pad:x1 - gx - pad]
    sa = a[y0:y1, x0:x1]
    res = cv2.matchTemplate(sa, tb, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(res)
    px, py = subpixel_peak(res, ml)
    return x0 + px - pad - (x0 - gx), y0 + py - pad - (y0 - gy), float(mv)


def _timed_out(m):
    return bool(m.get("pixels_timed_out") or m.get("settle_timed_out") or m.get("timed_out"))


def build(tile_dir, crop=None, skip=()):
    metas = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(tile_dir, "*.json")))]
    metas = [m for m in metas if m.get("camera") and os.path.exists(os.path.join(tile_dir, m["name"] + ".png"))]
    # R6 §12.5 item 1: a tile that timed out (ours: either wait; Odon:
    # `timed_out`) is not part of the reference; the caller retakes it
    excluded = [m["name"] for m in metas if _timed_out(m) or m["name"] in skip]
    metas = [m for m in metas if m["name"] not in excluded]
    if not metas:
        raise ValueError(f"no tiles in {tile_dir}")
    scale = float(np.median([m["camera"][2] for m in metas]))
    rgbs = [cv2.imread(os.path.join(tile_dir, m["name"] + ".png"))[:, :, ::-1].copy() for m in metas]
    H0, W0 = rgbs[0].shape[:2]
    cx0, cy0, cw, ch = crop or (0, 0, W0, H0)
    rgbs = [r[cy0:cy0 + ch, cx0:cx0 + cw].copy() for r in rgbs]
    h, w = rgbs[0].shape[:2]
    ys = [cv2.GaussianBlur(luma(r), (0, 0), 1.0) for r in rgbs]
    # camera placement: the cropped tile's top-left, level-0-derived screen px
    pos = np.array([[m["camera"][0] * scale - W0 / 2.0 + cx0, m["camera"][1] * scale - H0 / 2.0 + cy0]
                    for m in metas])
    # pairwise corrections, then a least-squares solve anchored at tile 0
    rows, rhs, pairs = [], [], []
    for i in range(len(metas)):
        for j in range(i + 1, len(metas)):
            d = pos[j] - pos[i]
            if abs(d[0]) >= w * (1 - MIN_OVERLAP / 2) or abs(d[1]) >= h * (1 - MIN_OVERLAP / 2):
                continue
            r = _pair_shift(ys[i], ys[j], d)
            if r is None:
                continue
            pairs.append((i, j, r[2], r[0] - d[0], r[1] - d[1]))
            for k in (0, 1):
                row = np.zeros(2 * len(metas))
                row[2 * j + k], row[2 * i + k] = 1, -1
                rows.append(row)
                rhs.append(r[k])
    # tile 0 held at its camera place, and every tile pulled weakly towards
    # its own camera place: a group of tiles not linked to tile 0 by any
    # seam (a tile left out in the middle) keeps its camera position instead
    # of drifting to the origin; linked tiles follow their seams
    n2 = 2 * len(metas)
    anchor = np.zeros((2, n2))
    anchor[0, 0] = anchor[1, 1] = 1
    prior = np.eye(n2) * 1e-3
    A = np.vstack([np.array(rows).reshape(-1, n2), anchor * 1e3, prior])
    b = np.concatenate([rhs, pos[0] * 1e3, pos.reshape(-1) * 1e-3])
    sol = np.linalg.lstsq(A, b, rcond=None)[0].reshape(-1, 2) if rows else pos
    # mosaic frame: integer-aligned canvas; each tile resampled to its
    # whole-pixel spot, nearest-centre wins (no blending, no resampling)
    lo = np.floor(sol.min(axis=0)).astype(int)
    hi = np.ceil(sol.max(axis=0) + [w, h]).astype(int)
    W, H = hi - lo
    rgb = np.zeros((H, W, 3), np.uint8)
    best = np.full((H, W), np.inf, np.float32)
    gy, gx = np.mgrid[0:H, 0:W]
    for k, (t, (x, y)) in enumerate(zip(rgbs, sol)):
        # step 0 (R6): placed at the nearest WHOLE pixel, never resampled
        # (interpolation softens a nearest-neighbour picture's pixel edges)
        ix, iy = int(round(x - lo[0])), int(round(y - lo[1]))
        y0, x0 = max(0, iy), max(0, ix)
        y1, x1 = min(H, iy + h), min(W, ix + w)
        sub = (slice(y0, y1), slice(x0, x1))
        dist = (gx[sub] - (ix + w / 2.0)) ** 2 + (gy[sub] - (iy + h / 2.0)) ** 2
        take = dist < best[sub]
        best[sub][take] = dist[take]
        rgb[sub][take] = t[y0 - iy:y1 - iy, x0 - ix:x1 - ix][take]
    seams = []
    for i, j, score, ex, ey in pairs:
        a = (sol[i] - lo).round().astype(int)
        c = (sol[j] - lo).round().astype(int)
        x0, y0 = np.maximum(a, c) + 2
        x1, y1 = np.minimum(a, c) + [w, h] - 2
        ta = ys[i][y0 - a[1]:y1 - a[1], x0 - a[0]:x1 - a[0]]
        tb = ys[j][y0 - c[1]:y1 - c[1], x0 - c[0]:x1 - c[0]]
        mad = float(np.mean(np.abs(ta - tb))) if ta.size else None
        seams.append({"a": metas[i]["name"], "b": metas[j]["name"], "score": round(score, 4),
                      "camera_error_px": [round(ex, 2), round(ey, 2)],
                      "mad": None if mad is None else round(mad, 2),
                      "ok": mad is not None and mad < SEAM_MAD})
    origin = (lo / scale).tolist()        # level-0 of mosaic (0, 0)
    tiles = [{"name": m["name"], "xy": (s - lo).round(3).tolist(),
              # where its pixels were actually put down (rounded once, here)
              "xy_px": [int(round(s[0] - lo[0])), int(round(s[1] - lo[1]))], "camera": m["camera"],
              "timed_out": _timed_out(m)}
             for m, s in zip(metas, sol)]
    return {"rgb": rgb, "origin": origin, "scale": scale, "tiles": tiles, "seams": seams,
            "covered": np.isfinite(best), "crop": [int(cx0), int(cy0), int(cw), int(ch)],
            "excluded": excluded, "accepted_exclusions": bool(skip),
            "ok": (not excluded or bool(skip)) and all(s["ok"] for s in seams)}


def save(m, prefix):
    cv2.imwrite(prefix + ".png", m["rgb"][:, :, ::-1])
    np.save(prefix + ".covered.npy", m["covered"])
    json.dump({k: m[k] for k in ("origin", "scale", "tiles", "seams", "crop", "excluded",
                                     "accepted_exclusions", "ok")}, open(prefix + ".json", "w"), indent=1)


def load(prefix):
    m = json.load(open(prefix + ".json"))
    m["rgb"] = cv2.imread(prefix + ".png")[:, :, ::-1].copy()
    m["covered"] = np.load(prefix + ".covered.npy")
    return m


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    crop = json.loads(args[2]) if len(args) > 2 else None
    mm = build(args[0], crop)
    if not mm["ok"] and "--accept-exclusions" in sys.argv:
        # plan §4: after two failed retakes the region is left out, reported:
        # timed-out tiles and both tiles of every failed seam are dropped,
        # their area is not covered, so its blocks are not scored
        bad = set(mm["excluded"]) | {n for s in mm["seams"] if not s["ok"] for n in (s["a"], s["b"])}
        mm = build(args[0], crop, skip=bad)
    save(mm, args[1])
    bad = [s for s in mm["seams"] if not s["ok"]]
    print(json.dumps({"tiles": len(mm["tiles"]), "size": list(mm["rgb"].shape[:2]),
                      "seams": len(mm["seams"]), "bad_seams": bad,
                      "excluded": mm["excluded"], "accepted_exclusions": mm["accepted_exclusions"], "ok": mm["ok"]}))
    # not ok: retake the timed-out tiles and both tiles of a failed seam
    sys.exit(0 if mm["ok"] else 2)
