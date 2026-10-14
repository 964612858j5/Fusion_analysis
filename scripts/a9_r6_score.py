"""R6 plan §4: where each captured frame is, and how far each block of it is
from the settled picture (docs/v16_5_r6_motion_clarity_plan.md).

REGISTRATION -- translation only, subpixel: a coarse search at 1/4 size
around the predicted spot (previous position + velocity; the whole mosaic
when that fails), then a full-size correlation in a +-6 px window refined by
a parabola. Both images are blurred a little first (sigma 1.5) so a blurry
frame still finds its place; the score is that correlation (0-1). A frame
is "unlocatable" when the score is under `MIN_SCORE` or its jump from the
previous located frame is implausible (plan §4: reported as bounds).

BLOCKS -- on a FIXED grid of the mosaic (32 x 32 mosaic px), so every block
is tracked in tissue coordinates: the frame is placed on the grid at the
nearest WHOLE pixel (no interpolation; step 0), then per block and per
colour channel, the worst of the three:
  detail = mean((C - blur(C, 1))^2) of the frame / of the reference
  ssim   = structural similarity of the two blocks (after blur sigma 1)
Only blocks whose reference has tissue detail (`detail_ref >= DETAIL_MIN`)
are scored; the thresholds come from step 0 calibration (plan §6).

    cap = Capture(prefix)                 # a9_framecap output
    reg = Registrar(mosaic)
    for i in range(len(cap)):
        r = reg.locate(cap.rgb(i), cap.t(i))     # {"xy", "score", "ok"}
        b = block_scores(mosaic_prep, cap.rgb(i), r["xy"])
"""
import json

import cv2
import numpy as np

BLOCK = 32
MIN_SCORE = 0.5          # step 0 sets the final value
MAX_SPEED = 12000.0      # screen px / s: faster jumps are implausible
DETAIL_MIN = 4.0         # reference detail energy for a scorable block
SSIM_SIGMA = 1.0         # blur before SSIM (whole-pixel alignment, see block_scores)
CH_DETAIL_MIN = 8.0      # a colour channel's own detail for its ratio to count (step 0)


class Capture:
    """`crop` (x, y, w, h): the same cut as the mosaic's (a9_r6_ref)."""

    def __init__(self, prefix, crop=None):
        self.prefix = prefix
        self.meta = json.load(open(prefix + ".json"))
        w, h, stride = self.meta["w"], self.meta["h"], self.meta["stride"]
        self.raw = np.memmap(prefix + ".frames", np.uint8, "r")
        self.n = self.raw.size // (h * stride)
        self.raw = self.raw[:self.n * h * stride].reshape(self.n, h, stride)
        self.w, self.h = w, h
        self.crop = crop or (0, 0, w, h)

    def __len__(self):
        return self.n

    def t(self, i):
        return 0.5 * (self.meta["t0"][i] + self.meta["t1"][i])

    def gap_ms(self, i):
        """Time from the previous capture's start to this one's (ms)."""
        return None if i == 0 else (self.meta["t0"][i] - self.meta["t0"][i - 1]) * 1000.0

    def same(self, i, j):
        x, y, w, h = self.crop
        return np.array_equal(self.raw[i][y:y + h, 4 * x:4 * (x + w)], self.raw[j][y:y + h, 4 * x:4 * (x + w)])

    def rgb(self, i):
        x, y, w, h = self.crop
        a = np.asarray(self.raw[i])[:, :self.w * 4].reshape(self.h, self.w, 4)
        return a[y:y + h, x:x + w][:, :, [2, 1, 0]].copy()


def luma(rgb):
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)


def highpass2(y):
    d = y - cv2.GaussianBlur(y, (0, 0), 1.0)
    return d * d


def block_mean(a):
    h, w = a.shape[0] // BLOCK * BLOCK, a.shape[1] // BLOCK * BLOCK
    return a[:h, :w].reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK).mean(axis=(1, 3))


class Reference:
    """The mosaic, prepared once: luminance, blurred copies, block stats."""

    def __init__(self, mosaic):
        self.m = mosaic
        self.y = luma(mosaic["rgb"])
        self.yb = cv2.GaussianBlur(self.y, (0, 0), 1.5)
        self.yq = cv2.resize(self.yb, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        self.detail = block_mean(highpass2(self.y))
        cov = block_mean(mosaic["covered"].astype(np.float32))
        self.mask = (self.detail >= DETAIL_MIN) & (cov >= 1.0)
        # SSIM on lightly blurred luminance (step 0: whole-pixel alignment
        # leaves up to half a pixel, and nearest-neighbour pictures differ
        # in where their pixel blocks start)
        self.ys = cv2.GaussianBlur(self.y, (0, 0), SSIM_SIGMA)
        # step 0 (R6): scored per colour channel as well -- one marker
        # channel still missing or coarse while the others are sharp hardly
        # changes the luminance (hand labels: 92 % agreement on luminance)
        rgb = mosaic["rgb"].astype(np.float32)
        self.c = [rgb[:, :, k] for k in range(3)]
        self.cs = [cv2.GaussianBlur(c, (0, 0), SSIM_SIGMA) for c in self.c]
        self.cdet = [block_mean(highpass2(c)) for c in self.c]
        self.cmu = [block_mean(c) for c in self.cs]
        self.cvar = [block_mean(c * c) - m ** 2 for c, m in zip(self.cs, self.cmu)]
        # where the settled picture itself is blank / black / saturated, the
        # same in a frame is not a failure (a real project with bright channels)
        rmu = block_mean(self.y)
        self.blank = (rmu < 2.0) | (rmu > 250.0) | (block_mean(self.y * self.y) - rmu ** 2 < 1.0)


class Registrar:
    def __init__(self, ref):
        self.ref = ref
        self.prev = None          # (t, xy)
        self.vel = np.zeros(2)

    def _coarse(self, fq, guess, radius):
        H, W = self.ref.yq.shape
        h, w = fq.shape
        if guess is None:
            x0, y0, x1, y1 = 0, 0, W, H
        else:
            gx, gy = guess[0] / 4.0, guess[1] / 4.0
            r = radius / 4.0
            x0, y0 = int(max(0, gx - r)), int(max(0, gy - r))
            x1, y1 = int(min(W, gx + w + r)), int(min(H, gy + h + r))
        if x1 - x0 < w or y1 - y0 < h:
            return None, -1.0
        res = cv2.matchTemplate(self.ref.yq[y0:y1, x0:x1], fq, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        return np.array([(x0 + ml[0]) * 4.0, (y0 + ml[1]) * 4.0]), float(mv)

    def _fine(self, fb, xy, pad=6):
        from a9_r6_ref import subpixel_peak
        H, W = self.ref.yb.shape
        h, w = fb.shape
        ix, iy = int(round(xy[0])), int(round(xy[1]))
        x0, y0 = ix - pad, iy - pad
        if x0 < 0 or y0 < 0 or x0 + w + 2 * pad > W or y0 + h + 2 * pad > H:
            # frame partly outside the mosaic: correlate its inner part only
            m = 64
            fb = fb[m:-m, m:-m]
            x0, y0 = ix + m - pad, iy + m - pad
            if x0 < 0 or y0 < 0 or x0 + fb.shape[1] + 2 * pad > W or y0 + fb.shape[0] + 2 * pad > H:
                return None, -1.0
            res = cv2.matchTemplate(self.ref.yb[y0:y0 + fb.shape[0] + 2 * pad, x0:x0 + fb.shape[1] + 2 * pad],
                                    fb, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            px, py = subpixel_peak(res, ml)
            return np.array([x0 + px - m, y0 + py - m]), float(mv)
        res = cv2.matchTemplate(self.ref.yb[y0:y0 + h + 2 * pad, x0:x0 + w + 2 * pad], fb, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        px, py = subpixel_peak(res, ml)
        return np.array([x0 + px, y0 + py]), float(mv)

    def locate(self, rgb, t):
        fb = cv2.GaussianBlur(luma(rgb), (0, 0), 1.5)
        fq = cv2.resize(fb, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        guess = None
        if self.prev is not None:
            guess = self.prev[1] + self.vel * (t - self.prev[0])
        xy, s = self._coarse(fq, guess, 240) if guess is not None else (None, -1.0)
        if s < MIN_SCORE:
            xy, s = self._coarse(fq, None, 0)
        if xy is None:
            return {"xy": None, "score": s, "ok": False, "why": "no coarse match"}
        xy, s = self._fine(fb, xy)
        if xy is None:
            return {"xy": None, "score": s, "ok": False, "why": "outside mosaic"}
        ok, why = s >= MIN_SCORE, None if s >= MIN_SCORE else "low score"
        if ok and self.prev is not None:
            dt = max(1e-3, t - self.prev[0])
            if np.hypot(*(xy - self.prev[1])) / dt > MAX_SPEED:
                ok, why = False, "implausible jump"
        if ok:
            if self.prev is not None:
                dt = max(1e-3, t - self.prev[0])
                self.vel = 0.5 * self.vel + 0.5 * (xy - self.prev[1]) / dt
            self.prev = (t, xy)
        return {"xy": xy.tolist(), "score": round(s, 4), "ok": ok, "why": why}


def _ssim(f, mu_r, var_r, r, c2=58.5, c1=6.5):
    mu_f = block_mean(f)
    var_f = block_mean(f * f) - mu_f ** 2
    cov = block_mean(f * r) - mu_f * mu_r
    return ((2 * mu_f * mu_r + c1) * (2 * cov + c2)) / ((mu_f ** 2 + mu_r ** 2 + c1) * (var_f + var_r + c2))


def block_scores(ref, rgb, xy):
    """Per mosaic-grid block the frame fully covers: detail ratio and SSIM.
    Returns (rows, cols, detail_ratio, ssim, blank) for the covered blocks
    of the reference's tissue mask.

    Step 0 (R6): the frame is placed on the grid at the WHOLE pixel nearest
    its registered position -- no interpolation. Shifting by a fraction of
    a pixel (Lanczos) softened the hard pixel edges of a nearest-neighbour
    picture at zoom 2 (detail ratio 0.58 for an unchanged picture). The
    detail ratio is insensitive to the remaining half pixel; SSIM is taken
    on lightly blurred values for the same reason. Both are the WORST of
    the three colour channels (a channel counts for the ratio where the
    reference has enough detail in it, CH_DETAIL_MIN), so one marker channel that is
    still coarse or missing fails the block."""
    h, w = rgb.shape[:2]
    ix, iy = int(round(xy[0])), int(round(xy[1]))
    c0, r0 = -(-ix // BLOCK), -(-iy // BLOCK)              # ceil
    c1, r1 = (ix + w) // BLOCK, (iy + h) // BLOCK
    c0, r0 = max(c0, 0), max(r0, 0)
    c1, r1 = min(c1, ref.detail.shape[1]), min(r1, ref.detail.shape[0])
    if c1 <= c0 or r1 <= r0:
        return None
    # grid pixel (gx, gy) = frame pixel (gx - ix, gy - iy)
    gx0, gy0 = c0 * BLOCK, r0 * BLOCK
    W, H = (c1 - c0) * BLOCK, (r1 - r0) * BLOCK
    fy, fx = slice(gy0 - iy, gy0 - iy + H), slice(gx0 - ix, gx0 - ix + W)
    gy, gx = slice(gy0, gy0 + H), slice(gx0, gx0 + W)
    sl = (slice(r0, r1), slice(c0, c1))
    frame = rgb.astype(np.float32)
    ratio = np.full((r1 - r0, c1 - c0), np.inf, np.float32)
    ssim = np.full((r1 - r0, c1 - c0), np.inf, np.float32)
    for k in range(3):
        f = frame[:, :, k]
        fs = cv2.GaussianBlur(f, (0, 0), SSIM_SIGMA)[fy, fx]
        ssim = np.minimum(ssim, _ssim(fs, ref.cmu[k][sl], ref.cvar[k][sl], ref.cs[k][gy, gx]))
        rd = ref.cdet[k][sl]
        rk = block_mean(highpass2(f[fy, fx])) / np.maximum(rd, 1e-6)
        ratio = np.minimum(ratio, np.where(rd >= CH_DETAIL_MIN, rk, np.inf))
    # a block whose detail is only in the luminance mix: the luminance ratio
    moved = luma(rgb)[fy, fx]
    lum = block_mean(highpass2(moved)) / np.maximum(ref.detail[sl], 1e-6)
    ratio = np.where(np.isfinite(ratio), ratio, lum)
    raw_mu = block_mean(moved)
    raw_var = block_mean(moved * moved) - raw_mu ** 2
    blank = ((raw_mu < 2.0) | (raw_mu > 250.0) | (raw_var < 1.0)) & ~ref.blank[sl]
    mask = ref.mask[sl]
    rr, cc = np.nonzero(mask)
    return rr + r0, cc + c0, ratio[mask], ssim[mask], blank[mask]
