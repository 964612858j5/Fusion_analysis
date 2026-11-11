"""
block01/core/mps_kernels.py — Step0's background correction on the Mac's GPU
(Apple MPS, through PyTorch). docs/v17_step0_mac_gpu_correction_plan.md.

The counterpart of `core/gpu_kernels.py` (CUDA) for machines without CUDA.

NUMERICAL CONTRACT
------------------
* TopHat: white top-hat with skimage's ``disk(r)`` footprint and scipy's
  ``reflect`` border (= OpenCV ``BORDER_REFLECT``): erosion (min), then
  dilation (max), then ``x - opening``. Min and max are exact, so the result
  is **bit for bit** the CPU kernel's (`bg_correction._tophat_cpu`) on every
  array this module takes (`tophat_takes`). The disk is taken apart in one
  of two exact ways, whichever is faster for the radius (`RECT_MAX_RADIUS`):
  the union of its "staircase" rectangles (one 2-D max-pool each), or a
  running horizontal min/max per half-width folded into the rows that use
  it.
* Gaussian: background = ``gaussian(x, sigma)`` with scipy's truncate-4
  weights (``radius = int(4*sigma + 0.5)``, float64 weights normalised and
  cast to float32), two separable float32 passes, ``reflect`` border;
  result ``max(x - background, 0)``. Within float32 rounding of OpenCV /
  scipy (not bitwise): a Save channel records ``torch-mps-gaussian``.

PADDING is by index, scipy's ``reflect`` rule (as in `gpu_kernels`), so an
array narrower than the kernel works too.

THREADS: one process-wide lock; every device call holds it, so the preview's
threads and the Save thread take turns on the one device.

READINESS never blocks: `state()` reads a flag ("unknown" / "ready" /
"off"). torch is imported only by `ensure_ready()` (the smoke test), which
callers run on a background thread.

`BLOCK01_BG_MPS=0` switches all of this off (the CPU, as before).
"""

import os
import sys
import threading

import numpy as np

ENV_SWITCH = "BLOCK01_BG_MPS"

#: up to this radius the rectangle decomposition is the faster one; above
#: it, the running min/max (measured on Apple M6, 2048² tile: r=15 37 vs
#: 51 ms, r=30 105 vs 100 ms, r=50 263 vs 166 ms; plan §7)
RECT_MAX_RADIUS = 28

_state = "unknown"           # "unknown" | "ready" | "off"
_error = ""
_torch = None
_device = None
_LOCK = threading.Lock()     # the device: one kernel call at a time
_READY_LOCK = threading.Lock()   # the smoke test runs once
_warm_thread = None


def _switched_off():
    return os.environ.get(ENV_SWITCH, "").strip() == "0"


def state():
    """"ready", "unknown" (smoke test not run yet) or "off". Never imports,
    never runs anything, never waits."""
    if _switched_off():
        return "off"
    return _state


def ready():
    return state() == "ready"


def error():
    return _error


def disable(exc, context):
    """A failure on the device: off for the rest of the session."""
    global _state, _error
    if _state == "off":
        return
    _state = "off"
    _error = f"{type(exc).__name__}: {exc}"
    print(f"[MPS] {context} failed ({_error}); background correction uses the CPU "
          "for this session.", file=sys.stderr, flush=True)


def ensure_ready():
    """Run the smoke test once (blocking: call it off the GUI thread).
    True when the device is ready."""
    global _state, _error, _torch, _device
    if _switched_off():
        return False
    if _state != "unknown":
        return _state == "ready"
    with _READY_LOCK:
        if _state != "unknown":
            return _state == "ready"
        try:
            import torch
            if not (getattr(torch.backends, "mps", None) is not None
                    and torch.backends.mps.is_available()):
                raise RuntimeError("MPS is not available")
            _torch, _device = torch, torch.device("mps")
            _smoke_test()
            _state = "ready"
            print("[MPS] background correction on the Mac's GPU (torch "
                  f"{torch.__version__})", flush=True)
        except Exception as exc:                          # noqa: BLE001
            _state = "off"
            _error = f"{type(exc).__name__}: {exc}"
            print(f"[MPS] not used for background correction: {_error}", flush=True)
    return _state == "ready"


def start_warmup():
    """Start the smoke test on a background thread (once)."""
    global _warm_thread
    if state() != "unknown" or _warm_thread is not None:
        return
    _warm_thread = threading.Thread(target=ensure_ready, name="mps-warmup", daemon=True)
    _warm_thread.start()


def _smoke_test():
    """The device gives the CPU's answers (raises otherwise)."""
    from . import bg_correction as bc
    rng = np.random.default_rng(7)
    a = (rng.random((71, 67)) * 255).round().astype(np.float32)
    for r in (2, RECT_MAX_RADIUS + 1):            # both decompositions
        got = _tophat(a, r)
        want = bc._tophat_cpu(a, r)
        if not np.array_equal(got.view(np.uint32), want.view(np.uint32)):
            raise RuntimeError(f"TopHat r={r} differs from the CPU")
    got = _gaussian(a, 5)
    want = (bc._cucim_cpu_opencv(a, 5) if bc._cucim_cpu_opencv_ok(a)
            else bc._cucim_cpu_scipy(a, 5))
    if not np.allclose(got, want, rtol=0, atol=1e-3):
        raise RuntimeError("Gaussian differs from the CPU")


# ── what each kernel takes ─────────────────────────────────────────────

def tophat_takes(arr32, radius):
    """2-D, finite and at least 2r+1 on each side: where OpenCV and skimage
    agree bit for bit (block CS P1) and so this module does too."""
    r = int(radius)
    return (arr32.ndim == 2 and min(arr32.shape) >= 2 * r + 1
            and bool(np.isfinite(arr32).all()))


def gaussian_takes(arr32):
    return arr32.ndim == 2 and arr32.size > 0 and bool(np.isfinite(arr32).all())


# ── public kernels (numpy in, numpy out; the caller checks *_takes) ─────

class MpsOff(RuntimeError):
    """The device was switched off for the session (by a failure elsewhere,
    or the switch) while this call waited for it."""


def _on_device(fn, arr32, param, context):
    """Run `fn` holding the device: refused once the device is off, and a
    failure switches it off before the next caller gets the lock."""
    with _LOCK:
        if state() != "ready":
            raise MpsOff(f"the Mac's GPU is off for this session ({_error or 'switched off'})")
        try:
            return fn(np.ascontiguousarray(arr32, dtype=np.float32), int(param))
        except Exception as exc:                          # noqa: BLE001
            disable(exc, context)
            raise


def tophat(arr32, radius):
    return _on_device(_tophat, arr32, radius, "tophat")


def gaussian_bg_subtract(arr32, sigma):
    return _on_device(_gaussian, arr32, sigma, "cucim")


# ── implementation ─────────────────────────────────────────────────────

def reflect_index(n, pad):
    """Source index of each padded position, scipy's ``reflect`` rule
    (a b c | c b a), for any n and pad."""
    i = np.arange(-pad, n + pad, dtype=np.int64)
    i = np.where(i < 0, -1 - i, i)
    i = i % (2 * n)
    return np.minimum(i, 2 * n - 1 - i)


def _pad(t, pad_y, pad_x):
    torch = _torch
    h, w = t.shape
    iy = torch.from_numpy(reflect_index(h, pad_y)).to(_device)
    ix = torch.from_numpy(reflect_index(w, pad_x)).to(_device)
    return t.index_select(0, iy).index_select(1, ix)


def _halfwidths(r):
    from skimage.morphology import disk
    k = disk(r).astype(bool)
    return [int(np.flatnonzero(k[dy]).size // 2) for dy in range(2 * r + 1)]


def _rects(r):
    """The disk as the union of its staircase rectangles (v, h): rows
    |dy| <= v, columns |dx| <= h; only the maximal ones."""
    half = _halfwidths(r)
    out = []
    for h in sorted(set(half)):
        v = max(abs(dy - r) for dy, hh in enumerate(half) if hh >= h)
        out.append((v, h))
    return [(v, h) for (v, h) in out
            if not any(v2 >= v and h2 >= h and (v2, h2) != (v, h) for v2, h2 in out)]


def _max_disk_rect(x, r):
    """max over disk(r) of `x` (H x W on the device), reflect border."""
    F = _torch.nn.functional
    p = _pad(x, r, r)[None, None]
    ho, wo = x.shape
    out = None
    for v, h in _rects(r):
        m = F.max_pool2d(p, kernel_size=(2 * v + 1, 2 * h + 1), stride=1)[0, 0]
        m = m[r - v:r - v + ho, r - h:r - h + wo]
        out = m.clone() if out is None else _torch.maximum(out, m, out=out)
    return out


def _max_disk_rows(x, r):
    """The same max, by a running horizontal max per half-width, each folded
    into the rows that use it as soon as it exists."""
    torch = _torch
    half = _halfwidths(r)
    p = _pad(x, r, r)
    ho, wo = x.shape
    need = {}
    for dy, h in enumerate(half):
        need.setdefault(h, []).append(dy)
    out = None
    cur = p[:, r:r + wo]
    for h in range(0, max(half) + 1):
        if h > 0:
            cur = torch.maximum(torch.maximum(cur, p[:, r - h:r - h + wo]),
                                p[:, r + h:r + h + wo])
        for dy in need.get(h, ()):
            s = cur[dy:dy + ho]
            out = s.clone() if out is None else torch.maximum(out, s, out=out)
    return out


def _max_disk(x, r):
    return _max_disk_rect(x, r) if r <= RECT_MAX_RADIUS else _max_disk_rows(x, r)


def _tophat(a, r):
    t = _torch.from_numpy(a).to(_device)
    eroded = -_max_disk(-t, r)
    opened = _max_disk(eroded, r)
    return (t - opened).cpu().numpy()


def gaussian_weights(sigma):
    """scipy's truncate-4 weights, float64 normalised, cast to float32."""
    radius = int(4.0 * float(sigma) + 0.5)
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    w = np.exp(-0.5 * x * x / (float(sigma) ** 2))
    return (w / w.sum()).astype(np.float32), radius


def _gaussian(a, sigma):
    torch = _torch
    F = torch.nn.functional
    w, radius = gaussian_weights(sigma)
    t = torch.from_numpy(a).to(_device)
    wk = torch.from_numpy(w).to(_device)
    g = F.conv2d(_pad(t, radius, 0)[None, None], wk.view(1, 1, -1, 1))[0, 0]   # axis 0
    g = F.conv2d(_pad(g, 0, radius)[None, None], wk.view(1, 1, 1, -1))[0, 0]   # axis 1
    return torch.clamp(t - g, min=0).cpu().numpy()
