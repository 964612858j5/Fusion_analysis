"""
block01/core/gpu_kernels.py — Runtime-parameterised GPU filters for Step0.

WHY THIS MODULE EXISTS
----------------------
`cupyx.scipy.ndimage.grey_erosion/dilation(size=(2r+1, 2r+1))` and
`cupyx.scipy.ndimage.gaussian_filter(sigma=...)` bake the support width into
the generated CUDA source (see `cupyx/scipy/ndimage/_filters_core.py`: the
loop bound is a literal and the kernel NAME carries `_w{w_shape}`).  A new
radius or sigma is therefore a new NVRTC compilation, measured on this
machine at ~4.5 s for the first use of a value and ~45-150 ms once both the
kernel and the result are cached (docs/v17_step0_runtime_kernels.md).

The two kernels below take the support width as a **runtime argument** and
the Gaussian weights as **runtime data**.  Their source never changes, so
CuPy's own cache (`CUPY_CACHE_DIR`) compiles each of them exactly once per
device/architecture/toolchain; every later radius or sigma is free.

NUMERICAL CONTRACT
------------------
* TopHat: square structuring element of side ``2r+1``, ``reflect`` border,
  erosion (min) -> dilation (max) -> ``x - opening`` -> clamp at 0.  Both
  passes are exact min/max, so the result is **bitwise identical** to the
  cupyx reference regardless of pass order (verified in
  tests/test_gpu_runtime_kernels.py).
* Gaussian: the reference is ``cupyx.scipy.ndimage.gaussian_filter(
  x, sigma=s, mode='reflect')`` with its default ``truncate=4``:
  ``radius = int(4*s + 0.5)``, weights computed in float64 from
  ``exp(-0.5*x**2/s**2)`` normalised by their sum, then **cast to float32**
  (cupy's ``_get_weights_dtype`` returns ``promote_types(float32, float32)``
  for a float32 input, and ``_call_kernel`` casts the weights to that dtype),
  separable passes along axis 0 then axis 1, **float32 accumulation and
  float32 intermediate/output**.  This module reproduces exactly that: same
  radius rule, same weight formula, same cast, same pass order, same
  accumulate/write dtypes.

BORDER
------
`reflect` is scipy's half-sample symmetric border (a b c d | d c b a), the
same mapping cupyx generates::

    if (i < 0) i = -1 - i;      i %= n * 2;      i = min(i, 2n - 1 - i);

DEPLOYMENT
----------
The CUDA source lives in this file (a Python string): nothing is read from
/tmp, no absolute path, no per-machine cache is required.  The first process
that imports `core.bg_correction` compiles the two kernels inside the
existing GPU smoke test — the same phase that already compiled the old
kernels — and CuPy stores the cubins in `CUPY_CACHE_DIR`, which the
installer already points at a persistent location.  A fresh machine pays
that one-time cost; every later start, and every parameter change, does not.

Public API
----------
``available()``            -- are CuPy and the kernels usable here?
``warm()``                 -- force compilation now (used by the smoke test)
``tophat(arr32, radius)``  -- float32 HxW -> float32 HxW (numpy)
``gaussian(arr32, sigma)`` -- float32 HxW -> float32 HxW (numpy)
``stats()``                -- call counters, for logs and tests
"""

import time

import numpy as np

try:                                    # CuPy is optional: the CPU path is
    import cupy as cp                   # the contract for machines without it
except Exception:                       # noqa: BLE001 -- import-time probe
    cp = None

#: Env switch: ``BLOCK01_LEGACY_GPU_FILTERS=1`` sends every call back to the
#: cupyx implementation.  It exists for A/B tests and as a fallback when the
#: custom kernels cannot compile; it re-introduces per-parameter compilation.
LEGACY_ENV = "BLOCK01_LEGACY_GPU_FILTERS"

CUDA_SOURCE = r"""
// Shared border mapping (scipy 'reflect', half-sample symmetric).
__device__ __forceinline__ int s0_reflect(int i, int n) {
    if (i < 0) {
        i = -1 - i;
    }
    i %= n * 2;
    int j = 2 * n - 1 - i;
    return i < j ? i : j;
}

// Separable min/max: one pass along one axis, window half-width `r` at
// runtime, `is_max` selects dilation over erosion.  Exact: no arithmetic
// beyond comparisons, so the result matches a 2-D square footprint.
extern "C" __global__ void s0_minmax(
        const float* __restrict__ x, float* __restrict__ y,
        int H, int W, int r, int axis, int is_max) {
    int x0 = blockIdx.x * blockDim.x + threadIdx.x;
    int y0 = blockIdx.y * blockDim.y + threadIdx.y;
    if (x0 >= W || y0 >= H) {
        return;
    }
    float best;
    if (axis == 0) {
        int yy = s0_reflect(y0 - r, H);
        best = x[yy * W + x0];
        for (int d = -r + 1; d <= r; ++d) {
            yy = s0_reflect(y0 + d, H);
            float v = x[yy * W + x0];
            best = is_max ? fmaxf(best, v) : fminf(best, v);
        }
    } else {
        int xx = s0_reflect(x0 - r, W);
        best = x[y0 * W + xx];
        for (int d = -r + 1; d <= r; ++d) {
            xx = s0_reflect(x0 + d, W);
            float v = x[y0 * W + xx];
            best = is_max ? fmaxf(best, v) : fminf(best, v);
        }
    }
    y[y0 * W + x0] = best;
}

// Separable correlation with runtime weights.  float32 weights and float32
// accumulation, exactly what the cupyx reference does for a float32 input:
// `_get_weights_dtype` returns promote_types(float32, float32) == float32,
// `_call_kernel` then casts the weights to float32 and the generated
// expression is `sum += cast<W>(value) * wval` with W == float32.
extern "C" __global__ void s0_gauss(
        const float* __restrict__ x, float* __restrict__ y,
        const float* __restrict__ w, int H, int W, int r, int axis) {
    int x0 = blockIdx.x * blockDim.x + threadIdx.x;
    int y0 = blockIdx.y * blockDim.y + threadIdx.y;
    if (x0 >= W || y0 >= H) {
        return;
    }
    float sum = 0.0f;
    if (axis == 0) {
        for (int d = -r; d <= r; ++d) {
            int yy = s0_reflect(y0 + d, H);
            sum += x[yy * W + x0] * w[d + r];
        }
    } else {
        for (int d = -r; d <= r; ++d) {
            int xx = s0_reflect(x0 + d, W);
            sum += x[y0 * W + xx] * w[d + r];
        }
    }
    y[y0 * W + x0] = sum;
}
"""

_MINMAX_NAME = "s0_minmax"
_GAUSS_NAME = "s0_gauss"

_minmax_kernel = None
_gauss_kernel = None
_compile_error = ""
_warmed = False

STATS = {
    "tophat_calls": 0,
    "gaussian_calls": 0,
    "legacy_calls": 0,
    "compile_ms": None,
    "compiled_at": None,
}


def legacy_forced():
    """Has this process been told to use the old cupyx path?"""
    import os
    return str(os.environ.get(LEGACY_ENV, "")).strip().lower() in {
        "1", "true", "yes", "on"}


def available():
    """True when CuPy is importable (the kernels may still need compiling)."""
    return cp is not None


def compile_error():
    """Why the custom kernels are unusable in this process ("" when fine)."""
    return _compile_error


def _ensure_kernels():
    """Create the two RawKernels (CuPy compiles them on first launch)."""
    global _minmax_kernel, _gauss_kernel, _compile_error
    if _minmax_kernel is not None and _gauss_kernel is not None:
        return True
    if cp is None:
        _compile_error = "CuPy not importable"
        return False
    try:
        _minmax_kernel = cp.RawKernel(CUDA_SOURCE, _MINMAX_NAME)
        _gauss_kernel = cp.RawKernel(CUDA_SOURCE, _GAUSS_NAME)
        return True
    except Exception as exc:                            # noqa: BLE001
        _minmax_kernel = _gauss_kernel = None
        _compile_error = f"{type(exc).__name__}: {exc}"
        return False


def cache_dir():
    """The CuPy kernel cache this process compiles into ('' when unknown).

    Deployment evidence: the same directory CuPy itself resolves, not a
    second answer to the question.  Printed by the smoke test so a machine's
    log says which cache its kernels came from.
    """
    if cp is None:
        return ""
    try:
        from cupy.cuda import compiler as _cmp
        return str(_cmp.get_cache_dir())
    except Exception:                                    # noqa: BLE001
        import os
        return str(os.environ.get("CUPY_CACHE_DIR", ""))


def warm():
    """Compile both kernels now and run them once on a tiny synthetic array.

    Called from `core.bg_correction`'s import-time smoke test: the same phase
    that compiled the old kernels, before the window is shown, so no
    compilation ever happens in the event loop.  Returns True on success.
    Raises the underlying error only if asked to (see `compile_ms` in
    `stats()` for the measured cost).
    """
    global _warmed, _compile_error
    if _warmed:
        return not _compile_error
    t0 = time.perf_counter()
    if not _ensure_kernels():
        STATS["compile_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        return False
    try:
        probe = np.arange(64, dtype=np.float32).reshape(8, 8)
        tophat(probe, 3)
        gaussian(probe, 3.0)
    except Exception as exc:                            # noqa: BLE001
        _compile_error = f"{type(exc).__name__}: {exc}"
        STATS["compile_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        return False
    STATS["compile_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    STATS["compiled_at"] = time.time()
    STATS["cache_dir"] = cache_dir()
    _warmed = True
    return True


#: threads per block; the grid below must be derived from BOTH sizes of it
_BLOCK = (32, 8)


def _grid(h, w):
    bx, by = _BLOCK
    return (max(1, (w + bx - 1) // bx), max(1, (h + by - 1) // by))


def _run_axis(kernel, src, dst, h, w, r, *extra):
    """Launch a kernel with signature (x, y, H, W, r, ...extra)."""
    bx, by = _BLOCK
    gx, gy = _grid(h, w)
    kernel((gx, gy), (bx, by), (src, dst, h, w, r) + extra)


def _run_gauss(kernel, src, dst, weights, h, w, r, axis):
    """Launch with signature (x, y, weights, H, W, r, axis)."""
    bx, by = _BLOCK
    gx, gy = _grid(h, w)
    kernel((gx, gy), (bx, by), (src, dst, weights, h, w, r, axis))


def _as_device_2d(arr32):
    """np/cupy float32 HxW -> contiguous device float32 array (+ shape)."""
    a = cp.asarray(arr32)
    if a.ndim != 2:
        raise ValueError(f"expected a 2-D tile, got shape {a.shape}")
    return cp.ascontiguousarray(a, dtype=cp.float32)


def tophat(arr32, radius):
    """White TopHat: ``clip(x - opening(x, square(2r+1)), 0, None)``.

    Float32 in, float32 out (numpy), same as the cupyx reference.
    """
    if not _ensure_kernels():
        raise RuntimeError(f"runtime GPU kernels unavailable: {_compile_error}")
    radius = max(1, int(radius))
    x = _as_device_2d(arr32)
    h, w = int(x.shape[0]), int(x.shape[1])
    a = cp.empty_like(x)
    b = cp.empty_like(x)
    # erosion: min along axis 0, then along axis 1 (same order as cupyx)
    _run_axis(_minmax_kernel, x, a, h, w, radius, 0, 0)
    _run_axis(_minmax_kernel, a, b, h, w, radius, 1, 0)
    # dilation: max along axis 0, then axis 1
    _run_axis(_minmax_kernel, b, a, h, w, radius, 0, 1)
    _run_axis(_minmax_kernel, a, b, h, w, radius, 1, 1)
    out = cp.clip(x - b, 0, None).astype(cp.float32, copy=False)
    STATS["tophat_calls"] += 1
    return cp.asnumpy(out).astype(np.float32, copy=False)


def gaussian_weights(sigma, radius=None):
    """The reference weights, computed exactly as scipy/cupyx order 0 does.

    ``phi_x = exp(-0.5*x**2/sigma**2); phi_x /= phi_x.sum()`` in float64.
    """
    sigma = float(sigma)
    r = int(4.0 * sigma + 0.5) if radius is None else int(radius)
    if r <= 0:
        return None, 0
    x = np.arange(-r, r + 1, dtype=np.float64)
    phi = np.exp(-0.5 / (sigma * sigma) * x ** 2)
    phi /= phi.sum()
    return phi, r


def gaussian_bg_subtract(arr32, sigma):
    """The corrected tile: ``clip(x - gaussian_filter(x), 0, None)`` on device.

    Same arithmetic as the cupyx path (float32 subtract and clip on the GPU).
    """
    if not _ensure_kernels():
        raise RuntimeError(f"runtime GPU kernels unavailable: {_compile_error}")
    weights, r = gaussian_weights(sigma)
    x = _as_device_2d(arr32)
    if weights is None:
        STATS["gaussian_calls"] += 1
        return np.zeros(tuple(int(v) for v in x.shape), dtype=np.float32)
    h, w = int(x.shape[0]), int(x.shape[1])
    # float32, exactly as cupy's `_call_kernel` casts weights for a float32
    # input -- that is what keeps this bitwise identical.
    wdev = cp.asarray(weights.astype(np.float32, copy=False))
    a = cp.empty_like(x)
    b = cp.empty_like(x)
    _run_gauss(_gauss_kernel, x, a, wdev, h, w, r, 0)
    _run_gauss(_gauss_kernel, a, b, wdev, h, w, r, 1)
    out = cp.clip(x - b, 0, None).astype(cp.float32, copy=False)
    STATS["gaussian_calls"] += 1
    return cp.asnumpy(out).astype(np.float32, copy=False)


def gaussian(arr32, sigma):
    """Gaussian background estimate, separable, reflect border, truncate=4.

    Returns the *background* ``gaussian_filter(x, sigma)`` as float32 numpy
    (the caller subtracts and clamps, exactly as the cupyx path does).
    """
    if not _ensure_kernels():
        raise RuntimeError(f"runtime GPU kernels unavailable: {_compile_error}")
    weights, r = gaussian_weights(sigma)
    x = _as_device_2d(arr32)
    if weights is None:
        STATS["gaussian_calls"] += 1
        return cp.asnumpy(x).astype(np.float32, copy=False)
    h, w = int(x.shape[0]), int(x.shape[1])
    # float32, exactly as cupy's `_call_kernel` casts weights for a float32
    # input -- that is what makes the result bitwise identical.
    wdev = cp.asarray(weights.astype(np.float32, copy=False))
    a = cp.empty_like(x)
    b = cp.empty_like(x)
    _run_gauss(_gauss_kernel, x, a, wdev, h, w, r, 0)
    _run_gauss(_gauss_kernel, a, b, wdev, h, w, r, 1)
    STATS["gaussian_calls"] += 1
    return cp.asnumpy(b).astype(np.float32, copy=False)


def stats():
    """A copy of the counters (safe to log/compare in tests)."""
    return dict(STATS)
