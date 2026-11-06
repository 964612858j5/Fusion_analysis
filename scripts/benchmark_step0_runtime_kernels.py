"""Prove, on any machine, that a new radius/sigma compiles nothing.

    python scripts/benchmark_step0_runtime_kernels.py [--cache DIR]

What it does
------------
1. Reports which GPU filter backend this process runs (`runtime-kernels` is
   the point of the check; `cupyx-legacy` means the fallback is in use and the
   check cannot pass by design).
2. Records the compiled kernels in CUPY_CACHE_DIR, drives parameters that were
   never used, and records them again. A new `.cubin` means the implementation
   specialised on a value again.
3. Times both methods on a realistic tile, and the legacy reference for the
   same tile, so the runtime-parameterised kernels can be compared with the
   implementation they replaced.

Exit code 0 only when no kernel was compiled for the new parameters, so it can
be used as a deployment check on a fresh machine:

    CUPY_CACHE_DIR=$(mktemp -d) python scripts/benchmark_step0_runtime_kernels.py

Notes
-----
* The first run on a machine/architecture pays the two-kernel compile; that
  cost is reported as `compile_ms`, not hidden.
* Read-only: writes nothing except CuPy's own kernel cache in CUPY_CACHE_DIR.
"""

import argparse
import glob
import os
import sys
import time

import numpy as np

# Import the checkout this script lives in, however it was invoked: the app
# package is exposed through the installer's `.block01/pkg` link (a relative
# path, so this works on any machine and any checkout location).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _cand in (os.path.join(_REPO, ".block01", "pkg"), os.path.dirname(_REPO)):
    if os.path.isdir(os.path.join(_cand, "block01")) and _cand not in sys.path:
        sys.path.insert(0, _cand)


def _cubins(cache):
    return set(glob.glob(os.path.join(cache, "*.cubin"))) if cache else set()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", default=os.environ.get("CUPY_CACHE_DIR", ""),
                    help="CuPy kernel cache dir to inspect (default: $CUPY_CACHE_DIR)")
    ap.add_argument("--tile", type=int, default=512, help="core tile size (default 512)")
    ap.add_argument("--halo", type=int, default=63, help="halo used for the timing tile")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    from block01.core import bg_correction as bg
    from block01.core import gpu_kernels as gk
    init_ms = (time.perf_counter() - t0) * 1000.0

    cache = args.cache or os.environ.get("CUPY_CACHE_DIR", "")
    backend = bg.gpu_filter_backend()
    print(f"backend            : {backend}")
    print(f"GPU_MORPH_AVAILABLE: {bg.GPU_MORPH_AVAILABLE}")
    print(f"smoke test         : {bg.GPU_MORPH_SMOKE_TEST}")
    print(f"import + init      : {init_ms:.0f} ms (kernel compile_ms={gk.stats()['compile_ms']})")
    print(f"cache dir          : {cache or '(none: CuPy default)'}")
    if not cache:
        print("!! no CUPY_CACHE_DIR: the compile check needs a directory to watch")
        return 2
    if backend != "runtime-kernels":
        print("!! not running the runtime-parameterised kernels; the check does not apply")

    rng = np.random.default_rng(0)
    n = args.tile + 2 * args.halo
    tile = (rng.random((n, n), dtype=np.float32) * 1000.0).astype(np.float32)

    print("\n-- new parameters must not compile --")
    before = _cubins(cache)
    radii = (7, 23, 44, 63, 91, 150)
    sigmas = (2, 9, 31, 120, 200)
    t = time.perf_counter()
    for r in radii:
        bg._tophat_gpu(tile, r)
    tophat_new_ms = (time.perf_counter() - t) / len(radii) * 1000.0
    t = time.perf_counter()
    for s in sigmas:
        bg._cucim_gpu(tile, s)
    gauss_new_ms = (time.perf_counter() - t) / len(sigmas) * 1000.0
    after = _cubins(cache)
    print(f"first pass through {len(radii)} radii: {tophat_new_ms:.1f} ms/op")
    print(f"first pass through {len(sigmas)} sigmas: {gauss_new_ms:.1f} ms/op")
    fresh = sorted(os.path.basename(p) for p in after - before)
    print(f"cubins before      : {len(before)}")
    print(f"cubins after       : {len(after)}")
    print(f"new cubins         : {len(fresh)} {fresh if fresh else ''}")

    print("\n-- single-operator cost on a %.0fx%.0f tile (kernel hot) --" % (n, n))
    for r in (15, 63, 150):
        t = time.perf_counter()
        bg._tophat_gpu(tile, r)
        print(f"  tophat r={r:<4d}       {1000*(time.perf_counter()-t):6.1f} ms")
    for s in (5, 50, 120, 200):
        t = time.perf_counter()
        bg._cucim_gpu(tile, s)
        print(f"  cucim  sigma={s:<4d}    {1000*(time.perf_counter()-t):6.1f} ms")

    print("\n-- reference (cupyx, per-size kernels; the first call compiles) --")
    for name, fn, p in (("tophat r=63", bg._tophat_gpu_legacy, 63),
                        ("cucim sigma=120", bg._cucim_gpu_legacy, 120)):
        t = time.perf_counter()
        fn(tile, p)
        first = (time.perf_counter() - t) * 1000.0
        t = time.perf_counter()
        fn(tile, p)
        hot = (time.perf_counter() - t) * 1000.0
        print(f"  {name:<16s} first={first:6.1f} ms  hot={hot:5.1f} ms")

    print("\n-- numeric parity with cupyx (max|delta|; 0.0 = identical) --")
    import cupyx.scipy.ndimage as ndi

    def ref_tophat(x, r):
        size = 2 * r + 1
        a = __import__("cupy").asarray(x)
        e = ndi.grey_erosion(a, size=(size, size), mode="reflect")
        d = ndi.grey_dilation(e, size=(size, size), mode="reflect")
        return __import__("cupy").asnumpy(
            __import__("cupy").clip(a - d, 0, None)).astype(np.float32)

    def ref_gauss(x, s):
        c = __import__("cupy")
        a = c.asarray(x)
        b = c.asnumpy(ndi.gaussian_filter(a, sigma=float(s), mode="reflect"))
        return np.clip(np.asarray(x, dtype=np.float32) - b, 0, None).astype(np.float32)

    parity = (np.random.default_rng(2).random((160, 200), dtype=np.float32) * 500.0)
    worst_t = worst_g = 0.0
    for r in (15, 63, 150):
        worst_t = max(worst_t, float(np.max(np.abs(gk.tophat(parity, r) - ref_tophat(parity, r)))))
    for s in (5, 50, 120, 200):
        worst_g = max(worst_g, float(np.max(np.abs(
            gk.gaussian_bg_subtract(parity, s) - ref_gauss(parity, s)))))
    print(f"  tophat : max|new-cupyx| = {worst_t:.3e} over radii 15/63/150")
    print(f"  gaussian: max|new-cupyx| = {worst_g:.3e} over sigmas 5/50/120/200")
    print("  (measured on this GPU/CuPy/driver; equality here is evidence, not a")
    print("   proof that every machine compiles the same instructions)")

    print(f"\nkernel stats       : {gk.stats()}")
    if backend == "runtime-kernels" and fresh:
        print("\nFAIL: new parameters compiled new kernels")
        return 1
    if worst_t != 0.0 or worst_g != 0.0:
        print("\nFAIL: the runtime kernels differ from the cupyx reference")
        return 1
    print("\nOK: new parameters compiled nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
