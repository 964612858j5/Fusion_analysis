"""Block F5-1 (docs/f5_rust_fusion_plan.md §7.2 items 1-2, §9): the Rust
fusion operators give the same bits as the Python path.

  * the remap -- `fuse_remap_values` against `core.channel_remap.apply_channel_remap`:
    identical f32 bits for every non-NaN value (the sign of zero included) and
    NaN at exactly the same positions; NaN payload/sign is not compared, since
    every NaN becomes 0 in the fused uint16 image (see `_same`);
  * its power step -- `fuse_power_values` against NumPy's `x ** float32(gamma)`
    (same comparison as the remap);
  * the fusion -- `fuse_values` against `FullFusionWorker._fuse_tile` itself
    (`fuse_channels` + the x65535 cast): exactly equal uint16 output, for
    random plans over float32, uint8 and uint16 planes.

Skips are tiered (§7.2): these tests need only the three test bindings; the
`FuseJob` tests of later steps need `FuseJob`. At F5-1 acceptance none of
these may be skipped (run with `-rs`). Until the shared wheel is reinstalled
(F5-5), load a dev build first on PYTHONPATH.

Two slow checks run only when asked:
  * F5_EXHAUSTIVE=1  -- every float32 in [0, 1] (and -0.0) for every UI gamma
    (0.10..3.00 step 0.01), legacy/fast-path gammas and 200 random ones;
    ~5.3e11 values, about 50 min on 16 processes (F5_PROCESSES; F5_GAMMAS=
    ui|random|all);
  * F5_SLICE=<slide> F5_SETTINGS=<step1_fusion_settings.json> -- every unit of
    the slide (or F5_BOX=y0,y1,x0,x1) through `fuse_values` against
    `_fuse_tile`, for the saved settings and a heavy 21-channel variant.
"""

import itertools
import json
import os
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from block01.core.channel_remap import apply_channel_remap  # noqa: E402
from block01.ui.step0.overview_panel import FullFusionWorker  # noqa: E402

try:
    import fusion_viewer_core as core
except ImportError:  # pragma: no cover - the wheel is not installed
    core = None

ARITH = ("fuse_remap_values", "fuse_power_values", "fuse_values")
needs_arith = pytest.mark.skipif(core is None or not all(hasattr(core, n) for n in ARITH),
                                 reason="fusion_viewer_core without the F5-1 operators")

UI_GAMMAS = [k / 100 for k in range(10, 301)]          # the spinbox: 0.10..3.00 step 0.01
EXTRA_GAMMAS = [1e-50, 1e-6, -1.0, 0.0, 0.05, 3.3, 5.0, 10.0, 0.123456, 1.0000001, 2.2, 180.6949,
                float("nan"), float("inf")]
# 200 deterministic exponents, log-uniform in [1e-3, 300] (plan §3 step 5)
RANDOM_GAMMAS = [float(g) for g in 10 ** np.random.default_rng(2026).uniform(-3, np.log10(300), 200)]


def _bits(a):
    return np.asarray(a, dtype=np.float32).view(np.uint32)


def _same(ref, got):
    """Identical f32 bits, except that a NaN matches any NaN: NumPy picks
    the NaN operand's payload/sign differently in its vector body and its
    scalar tail when two NaNs meet (codex F5-1 r1 P2), and every NaN
    reaches the fused image as 0 (`astype(uint16)`), so the payload is not
    part of the result."""
    ref, got = np.asarray(ref, np.float32), np.asarray(got, np.float32)
    nan = np.isnan(ref)
    return bool(np.array_equal(nan, np.isnan(got)) and np.array_equal(_bits(ref)[~nan], _bits(got)[~nan]))


def _rust_remap(values, params):
    out = core.fuse_remap_values(params, np.ascontiguousarray(values, np.float32).tobytes())
    return np.frombuffer(out, np.float32)


# ── 1. the remap ─────────────────────────────────────────────────────────
_RNG = np.random.default_rng(1)
_SPECIAL = np.array([np.nan, np.inf, -np.inf, 0.0, -0.0, 1e-30, 1e30, 255.0, 0.5, 1e-45, -1e-45],
                    np.float32)
DATA = {
    "every u16 value": np.arange(65536, dtype=np.float32),
    "floats": np.concatenate([_RNG.normal(50, 80, 100000).astype(np.float32),
                              _RNG.uniform(-1, 2, 50000).astype(np.float32), _SPECIAL]),
}
MINS = [0.0, 1.0, 3.3, None, 100.0, -5.0]
MAXS = [22.0, 105.203125, 1.0, None, 100.0, 7e4]
GAMMAS = [1.0, 0.5, 2.0, 0.7, 1.3, 2.2, 0.0, -1.0, 1e-50, 0.999999999]
CONTRAST_BRIGHTNESS = [(1.0, 0.0), (1.5, 0.1), (0.7, -0.2), (1.0, 0.3)]


@needs_arith
@pytest.mark.parametrize("name", list(DATA))
def test_the_remap_is_bit_identical(name):
    x = DATA[name]
    bad = []
    with np.errstate(all="ignore"):
        for mn, mx, g, (c, b) in itertools.product(MINS, MAXS, GAMMAS, CONTRAST_BRIGHTNESS):
            p = {"min": mn, "max": mx, "gamma": g, "contrast": c, "brightness": b}
            ref = apply_channel_remap(x, p)
            got = _rust_remap(x, p)
            if not _same(ref, got):
                bad.append((p, int((_bits(ref) != _bits(got)).sum())))
    assert not bad, bad[:5]


@needs_arith
@pytest.mark.parametrize("params,value", [
    ({"min": 0.0, "max": 1e-50, "brightness": 0.1, "gamma": 1.0}, 0.0),    # 0/0 -> NaN
    ({"min": 0.0, "max": 1e-50, "brightness": 0.1, "gamma": 1e-50}, 0.0),  # NaN ** 0 -> 1
    ({"min": -1e300, "max": 0.0}, 0.0),                                    # inf/inf -> NaN
    ({}, 3.0),                                                             # defaults: min/max derived
    ({"min": 2, "max": "9", "gamma": True}, 5.0),                          # Python's float()
    ({"min": float("nan"), "max": 1.0}, 0.0),                              # NaN endpoints are not
    ({"min": 0.0, "max": float("nan")}, 0.0),                              # "degenerate" (r1 P0)
    ({"min": float("nan"), "max": 1.0, "gamma": 1e-50}, 0.0),
    ({"min": 0.0, "max": 10.0, "contrast": float("nan")}, 0.0),
    ({"min": 0.0, "max": 10.0, "brightness": float("nan")}, 0.0),
    ({"min": 0.0, "max": 10.0, "gamma": float("nan")}, 0.0),
    ({"min": 0.0, "max": 10.0, "gamma": float("inf")}, 0.0),
])
def test_the_remap_carries_nans_and_converts_like_python(params, value):
    x = np.array([value, 1.0, 7.0, np.nan], np.float32)
    with np.errstate(all="ignore"):
        ref = apply_channel_remap(x, params)
    assert _same(ref, _rust_remap(x, params))


@needs_arith
@pytest.mark.parametrize("n", [1, 16, 17, 33, 1004])
def test_two_nans_meeting_give_a_nan_whatever_its_bits(n):
    x = np.zeros(n, np.float32)
    for p in ({"min": -1e300, "max": 1.0, "contrast": float("nan")},
              {"min": -1e300, "max": 1.0, "brightness": float("nan")}):
        with np.errstate(all="ignore"):
            ref = apply_channel_remap(x, p)
        got = _rust_remap(x, p)
        assert np.isnan(ref).all() and np.isnan(got).all()


@needs_arith
def test_a_missing_number_fails_like_python():
    # Python converts contrast/brightness/gamma only when the window is not
    # degenerate; Rust refuses a None at once, whatever the pixels (a
    # malformed committed window fails the run instead of sometimes giving 0)
    x = np.arange(3, dtype=np.float32)
    for key in ("contrast", "brightness", "gamma"):
        with pytest.raises(TypeError):
            apply_channel_remap(x, {key: None})
        with pytest.raises(TypeError):
            _rust_remap(x, {key: None})


# ── the power step ───────────────────────────────────────────────────────
def _power_check(gammas, x):
    bad = {}
    with np.errstate(all="ignore"):
        for g in gammas:
            ref = x ** np.float32(1e-6 if g <= 0 else g)   # the remap's own expression (NaN stays)
            got = np.frombuffer(core.fuse_power_values(g, x.tobytes()), np.float32)
            n = 0 if _same(ref, got) else int((_bits(ref) != _bits(got)).sum())
            if n:
                bad[g] = n
    return bad


@needs_arith
def test_the_power_step_matches_numpy_on_a_sample():
    x = np.concatenate([np.arange(0, 0x3f800001, 4093, dtype=np.uint32).view(np.float32),
                        np.array([-0.0, np.nan, 1.0, 1e-45], np.float32)])
    assert _power_check(UI_GAMMAS + EXTRA_GAMMAS + RANDOM_GAMMAS, x) == {}


def _exhaustive_task(args):
    g, lo, n = args
    x = np.arange(lo, lo + n, dtype=np.uint32).view(np.float32)
    if lo == 0:
        x = np.concatenate([x, np.array([-0.0], np.float32)])
    return g, sum(_power_check([g], x).values())


@needs_arith
@pytest.mark.skipif(os.environ.get("F5_EXHAUSTIVE") != "1", reason="set F5_EXHAUSTIVE=1 (about 50 min)")
def test_the_power_step_matches_numpy_for_every_value_and_ui_gamma():
    import multiprocessing as mp
    top, step = 0x3f800001, 1 << 24
    gammas = {"ui": UI_GAMMAS + EXTRA_GAMMAS, "random": RANDOM_GAMMAS,
              "all": UI_GAMMAS + EXTRA_GAMMAS + RANDOM_GAMMAS}[os.environ.get("F5_GAMMAS", "all")]
    jobs = [(g, lo, min(step, top - lo)) for g in gammas for lo in range(0, top, step)]
    bad = {}
    with mp.get_context("fork").Pool(int(os.environ.get("F5_PROCESSES", "16"))) as pool:
        for g, n in pool.imap_unordered(_exhaustive_task, jobs, chunksize=4):
            if n:
                bad[g] = bad.get(g, 0) + n
    assert bad == {}


# ── 2. the fusion ────────────────────────────────────────────────────────
def _python_fused(raw_cache, windows, groups, gws, nuc, nuc_w):
    """`FullFusionWorker._fuse_tile` itself, on a stand-in holding only what it reads."""
    fake = types.SimpleNamespace(_remap_params=windows, _unmapped_reported=set())
    fake._channel_norm = types.MethodType(FullFusionWorker._channel_norm, fake)
    with np.errstate(all="ignore"):
        return FullFusionWorker._fuse_tile(fake, raw_cache, groups, gws, nuc, nuc_w)


def _rust_fused(planes, kinds, windows, names, groups, gws, nuc, nuc_w):
    slot = {c: i for i, c in enumerate(names)}
    plan = {"channels": [{"window": windows.get(c)} for c in names],
            "groups": [(gws.get(g, 1.0), [(slot[c], w) for c, w in cw.items() if c in slot])
                       for g, cw in groups.items()],
            "nucleus": (slot[nuc], nuc_w) if nuc in slot else None}
    out = core.fuse_values(plan, [p.tobytes() for p in planes], kinds)
    return np.frombuffer(out, np.uint16).reshape(planes[0].shape + (2,))


def _random_case(rng, n_px, kinds_pool):
    nch = int(rng.integers(1, 9))
    names = [f"c{i}" for i in range(nch)]
    kinds, planes = [], []
    for _ in names:
        k = str(rng.choice(kinds_pool))
        if k == "u8":
            planes.append(rng.integers(0, 256, n_px).astype(np.uint8))
        elif k == "u16":
            planes.append(rng.integers(0, 65536, n_px).astype(np.uint16))
        else:
            p = rng.normal(40, 60, n_px).astype(np.float32)
            p[rng.random(n_px) < 0.01] = np.nan
            planes.append(p)
        kinds.append(k)
    windows = {}
    for c in names:
        r = rng.random()
        if r < 0.1:
            continue                                         # no committed window
        w = {"min": float(rng.choice([0, 1, 2.5])), "max": float(rng.choice([13, 22, 105.203125, 255, 30000])),
             "gamma": float(rng.choice([1, 1, 0.5, 2, 0.8, 1.7, 0.33])),
             "contrast": float(rng.choice([1, 1, 1.4])), "brightness": float(rng.choice([0, 0, 0.1, -0.1]))}
        if r < 0.15:
            w["min"] = None                                  # derived from the pixels in hand
        if r > 0.97:
            w.update(min=0.0, max=1e-50, brightness=0.1)     # NaN channel (§3 step 6)
        windows[c] = w
    groups, gws = {}, {}
    for g in range(int(rng.integers(0, 4))):
        members = rng.choice(nch, size=int(rng.integers(1, nch + 1)), replace=False)
        groups[f"g{g}"] = {f"c{int(s)}": float(rng.choice([1.0, 0.5, 0.33, 0.0, 1.7, -0.2])) for s in members}
        if rng.random() < 0.8:
            gws[f"g{g}"] = float(rng.choice([1.0, 0.6, 0.25, 1.5, 0.0]))
    nuc = None if rng.random() < 0.2 else f"c{int(rng.integers(0, nch))}"
    nuc_w = float(rng.choice([1.0, 0.7, 0.0, 2.0, 0.9]))
    return names, planes, kinds, windows, groups, gws, nuc, nuc_w


@needs_arith
@pytest.mark.parametrize("kinds_pool", [("f32",), ("u8",), ("u16",), ("f32", "u8", "u16")])
def test_the_fusion_is_bit_identical_to_fuse_tile(kinds_pool):
    rng = np.random.default_rng(7)
    bad = []
    for t in range(80):
        names, planes, kinds, windows, groups, gws, nuc, nuc_w = _random_case(rng, 20000, kinds_pool)
        raw = {c: p.astype(np.float32) for c, p in zip(names, planes)}   # `_read_one_source` gives float32
        ref = _python_fused(raw, windows, groups, gws, nuc, nuc_w)
        got = _rust_fused(planes, kinds, windows, names, groups, gws, nuc, nuc_w)
        if not np.array_equal(ref, got):
            bad.append((t, int((ref != got).sum())))
    assert not bad, bad[:5]


@needs_arith
@pytest.mark.parametrize("together", [True, False])
@pytest.mark.parametrize("nan_window", [{"min": 0.0, "max": 1e-50, "brightness": 0.1},
                                        {"min": float("nan"), "max": 1.0},
                                        {"min": 0.0, "max": 1e-50, "brightness": 0.1, "gamma": 1e-50}])
def test_a_nan_channel_poisons_its_group_and_the_maximum(together, nan_window):
    a = np.array([0.0, 50.0, 7.0], np.float32)
    windows = {"ok": {"min": 0.0, "max": 100.0}, "nan": nan_window}
    groups = ({"g": {"ok": 1.0, "nan": 1.0}} if together else {"g1": {"ok": 1.0}, "g2": {"nan": 1.0}})
    ref = _python_fused({"ok": a, "nan": a}, windows, groups, {}, "nan", 1.0)
    got = _rust_fused([a, a], ["f32", "f32"], windows, ["ok", "nan"], groups, {}, "nan", 1.0)
    assert np.array_equal(ref, got)


# ── real slide (slow, only when asked) ───────────────────────────────────
def _heavy(fc, dm):
    names = [c for c in dm if c != fc["nucleus"]["channel"]][:20]
    fc["groups"] = {"g1": {"group_weight": 0.8, "channels": {c: [1.0, 0.5, 0.75, 0.0][i % 4] for i, c in enumerate(names[:10])}},
                    "g2": {"group_weight": 1.0, "channels": {c: [1.0, 0.33][i % 2] for i, c in enumerate(names[10:])}}}
    fc["nucleus"]["weight"] = 0.9
    for i, c in enumerate(names + [fc["nucleus"]["channel"]]):
        dm[c] = dict(dm[c], gamma=[1.0, 0.7, 1.5, 0.5, 2.0][i % 5], contrast=[1.0, 1.3, 0.8][i % 3],
                     brightness=[0.0, 0.05, -0.03][i % 3])
    dm[names[3]] = dict(dm[names[3]], min=None)
    del dm[names[5]]


@needs_arith
@pytest.mark.skipif(not (os.environ.get("F5_SLICE") and os.environ.get("F5_SETTINGS")),
                    reason="set F5_SLICE and F5_SETTINGS (a real slide, read only)")
@pytest.mark.parametrize("variant", ["saved", "heavy"])
def test_real_slide_units_are_bit_identical(variant):
    """Every unit (or F5_BOX) of a real slide: the worker's own Python
    `_compute_unit` against the region's Rust `FuseJob` built by
    `_build_fuse_job` (sources, reads, plan and kernel), nothing written."""
    from concurrent.futures import ThreadPoolExecutor
    from block01.core.io_loader import OMETIFFLoader
    from block01.sources import OmeTiffSource
    assert hasattr(core, "FuseJob"), "this check needs FuseJob"
    fs = json.load(open(os.environ["F5_SETTINGS"]))
    fc, dm = json.loads(json.dumps(fs["fusion_config"])), json.loads(json.dumps(fs["display_mapping"]))
    if variant == "heavy":
        _heavy(fc, dm)
    slide = os.environ["F5_SLICE"]
    loader = OMETIFFLoader(slide)
    h, w = loader.shape
    groups = {g: d["channels"] for g, d in fc["groups"].items()}
    gws = {g: d["group_weight"] for g, d in fc["groups"].items()}
    nuc, nuc_w = fc["nucleus"]["channel"], fc["nucleus"]["weight"]
    chans = [c for c in {nuc, *[c for cw in groups.values() for c in cw]} if c in loader.ch_map]
    box = [int(v) for v in os.environ["F5_BOX"].split(",")] if os.environ.get("F5_BOX") else [0, h, 0, w]
    worker = FullFusionWorker(loader, dict(fc, ome_tiff=slide, output_dir="/nonexistent",
                                           channel_remap_params=dm), 1, 1, use_pixel_sources=True)
    worker._open_sources = []
    raw = OmeTiffSource(slide)
    try:
        sources = worker._region_sources(raw, "x", chans)
        packed = None
        if variant == "heavy":                                # a polygon cut too
            y0, y1, x0, x1 = box
            poly = [(x0 + 100, y0 + 50), (x1 - 300, y0 + 700), (x1 - 50, y1 - 100), (x0 + 900, y1 - 10)]
            packed = worker._poly_mask_packed(poly, y0, x0, y1 - y0, x1 - x0)
        job, _ = worker._build_fuse_job(raw, sources, chans, groups, gws, nuc, nuc_w, "x")
        assert job is not None and worker.engine_by_region == {"x": "rust"}
        args = (chans, sources, packed, box[0], box[2], groups, gws, nuc, nuc_w)

        def one(u):
            with np.errstate(all="ignore"):
                ref = worker._compute_unit(u, *args)
            got = worker._compute_unit(u, *args, job=job)
            return int((ref != got).sum())

        units = worker._units(*box)
        with ThreadPoolExecutor(int(os.environ.get("F5_THREADS", "8"))) as ex:
            diff = sum(ex.map(one, units))
        print(f"[F5] {variant}: {len(units)} units, {len(chans)} channels, {diff} differing values")
        assert diff == 0
    finally:
        raw.close()


# ── F5-2/F5-3: FuseJob through the real worker, synthetic projects ───────
# (plan §7.2 items 3, 4, 5, 7). Every run writes into tmp_path only.
needs_job = pytest.mark.skipif(core is None or not hasattr(core, "FuseJob"),
                               reason="fusion_viewer_core without FuseJob")

SH, SW = 300, 450
NAMES = ["DAPI", "CD3", "CD8", "CD20", "KI67"]


def _slide(path, dtype, compression="lzw", predictor=False, seed=3):
    import tifffile
    rng = np.random.default_rng(seed)
    if np.dtype(dtype).kind == "f":
        data = (rng.normal(60, 50, (len(NAMES), SH, SW))).astype(dtype)
        data[1, ::37, ::11] = np.nan
    else:
        hi = min(np.iinfo(dtype).max, 4000)
        lo = max(np.iinfo(dtype).min, -300)
        data = rng.integers(lo, hi, (len(NAMES), SH, SW)).astype(dtype)
    tifffile.imwrite(str(path), data, ome=True, tile=(64, 64), compression=compression,
                     predictor=predictor, metadata={"axes": "CYX", "Channel": {"Name": NAMES}})
    return str(path)


class _ShapeLoader:
    """The page loader the worker asks only for its channel index and shape."""
    def __init__(self):
        self.shape = (SH, SW)
        self.ch_map = {n: i for i, n in enumerate(NAMES)}


def _corrected(path, regions, chans, seed=5, shuffle=None):
    """A Step0-like corrected product: one roi_only group per region."""
    import zarr as _zarr
    from numcodecs import Blosc
    rng = np.random.default_rng(seed)
    root = _zarr.open_group(str(path), mode="w")
    root.attrs.update(mode="roi_only")
    for name, (y0, y1, x0, x1) in regions.items():
        g = root.create_group(name.replace(" ", "_"))
        g.attrs.update(roi_name=name, bbox_fullres=[y0, y1, x0, x1], shape=[y1 - y0, x1 - x0])
        for ch in chans:
            kw = {} if shuffle is None else {"compressor": Blosc(cname="lz4", clevel=5, shuffle=shuffle)}
            ds = g.create_dataset(ch, shape=(y1 - y0, x1 - x0), chunks=(64, 64), dtype=np.float32, **kw)
            a = rng.normal(30, 40, (y1 - y0, x1 - x0)).astype(np.float32)
            a[::13, ::7] = np.nan
            a[3::29, ::5] = np.inf
            ds[:] = a
            chunk = os.path.join(str(path), name.replace(" ", "_"), ch, "1.1")
            if os.path.exists(chunk):
                os.remove(chunk)                         # a missing chunk reads as fill
    return str(path)


WINDOWS = {"DAPI": {"min": 5.0, "max": 3000.0, "gamma": 0.7},
           "CD3": {"min": 10.0, "max": 180.0, "gamma": 1.3, "brightness": 0.1, "contrast": 1.2},
           "CD8": {"min": None, "max": 250.0, "gamma": 1.0},      # open: per unit
           "CD20": {"min": 0.0, "max": 2000.0, "gamma": 2.0}}     # KI67: no window


def _run_worker(tmp_path, name, slide, engine, monkeypatch, rois=None, corrected="", decided=(),
                chunk=64, wide=2, stop_after=None):
    from block01.ui.step0.overview_panel import FullFusionWorker as W
    import zarr as _zarr
    if engine == "python":
        monkeypatch.setenv(W.ENGINE_ENV, "python")
    else:
        monkeypatch.delenv(W.ENGINE_ENV, raising=False)
    cfg = {"ome_tiff": slide, "output_dir": str(tmp_path / name),
           "nucleus": {"channel": "DAPI", "weight": 0.8},
           "groups": {"a": {"group_weight": 1.0, "channels": {"CD3": 0.7, "CD8": 0.4, "KI67": 1.0}},
                      "b": {"group_weight": 0.6, "channels": {"CD8": 1.0, "CD20": 0.5}}},
           "channel_remap_params": WINDOWS, "artifact_kind": "step1_fused_zarr", "config_hash": "h"}
    w = W(_ShapeLoader(), cfg, 1, 1, rois=rois, corrected_zarr_path=corrected,
          corrected_decisions={c: "tophat" for c in decided}, use_pixel_sources=True)
    w.zarr_chunk, w.UNIT_CHUNKS_WIDE = chunk, wide
    w.register_provenance = False
    errors = []
    w.error.connect(errors.append)
    if stop_after is not None:
        seen = []

        def _progress(done, total, msg):
            if "Part [" in msg:
                seen.append(msg)
                if len(seen) == stop_after:
                    w.stop()
        w.progress.connect(_progress)
    w.run()
    out = {}
    for r in (rois or [{"name": None}]):
        store = tmp_path / name / ("fused.zarr" if r["name"] is None else f"fused_{r['name']}.zarr")
        if store.exists():
            z = _zarr.open(str(store), mode="r")
            out[r["name"]] = (np.array(z[:]), {k: v for k, v in z.attrs.items() if k != "created_at"})
    return w, errors, out


def _assert_same(a, b):
    assert set(a) == set(b) and a
    for k in a:
        assert np.array_equal(a[k][0], b[k][0]), k
        assert a[k][1] == b[k][1], k


ROIS = [{"name": "A", "bbox_fullres": [7, 203, 13, 391],                 # origin off the grid
         "polygon_fullres": [(20, 10), (380, 30), (300, 200), (15, 150)]},
        {"name": "B", "bbox_fullres": [150, 300, 200, 450], "polygon_fullres": None}]   # slide edge


@needs_job
@pytest.mark.parametrize("dtype,compression,predictor", [
    ("uint8", "lzw", False), ("uint8", "zlib", True), ("uint16", "lzw", True), ("uint16", None, False),
    ("int16", "zlib", False), ("uint32", "lzw", False), ("int32", None, False), ("float32", "zlib", False)])
def test_the_rust_job_writes_the_python_pixels(tmp_path, monkeypatch, dtype, compression, predictor):
    slide = _slide(tmp_path / "s.ome.tif", dtype, compression, predictor)
    corr = _corrected(tmp_path / "corrected.zarr", {r["name"]: r["bbox_fullres"] for r in ROIS}, ["CD3", "CD20"])
    for rois, decided in [(None, ()), (ROIS, ("CD3", "CD20"))]:
        tag = "roi" if rois else "full"
        _, e1, py = _run_worker(tmp_path, f"py_{tag}", slide, "python", monkeypatch, rois, corr if rois else "", decided)
        w, e2, rs = _run_worker(tmp_path, f"rs_{tag}", slide, "rust", monkeypatch, rois, corr if rois else "", decided)
        assert not e1 and not e2, (e1, e2)
        assert set(w.engine_by_region.values()) == {"rust"}, w.engine_by_region
        _assert_same(py, rs)
        meta = [json.load(open(tmp_path / d / "fusion_meta.json")) for d in (f"py_{tag}", f"rs_{tag}")]
        strip = lambda m: [{k: v for k, v in r.items() if k not in ("elapsed_s", "zarr_path")} for r in m["regions"]]
        assert strip(meta[0]) == strip(meta[1])
        for r in (rois or [{"name": None}]):
            stem = "fused" if r["name"] is None else f"fused_{r['name']}"
            assert np.array_equal(np.load(tmp_path / f"py_{tag}" / f"{stem}_overview_nucleus.npy"),
                                  np.load(tmp_path / f"rs_{tag}" / f"{stem}_overview_nucleus.npy"))


@needs_job
def test_a_float_predictor_falls_back_to_python_at_the_start(tmp_path, monkeypatch, capsys):
    slide = _slide(tmp_path / "s.ome.tif", "float32", "lzw", True)       # predictor 3
    _, e1, py = _run_worker(tmp_path, "py", slide, "python", monkeypatch)
    w, e2, rs = _run_worker(tmp_path, "rs", slide, "rust", monkeypatch)
    assert not e1 and not e2
    assert w.engine_by_region == {"full": "python"}
    assert "Python fusion (no Rust job" in capsys.readouterr().out
    _assert_same(py, rs)


@needs_job
def test_without_fusejob_the_region_is_python(tmp_path, monkeypatch, capsys):
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    _, _, py = _run_worker(tmp_path, "py", slide, "python", monkeypatch)
    monkeypatch.delattr(core, "FuseJob")
    w, errors, rs = _run_worker(tmp_path, "rs", slide, "rust", monkeypatch)
    assert not errors and w.engine_by_region == {"full": "python"}
    assert "has no FuseJob" in capsys.readouterr().out
    _assert_same(py, rs)


@needs_job
def test_a_bitshuffled_corrected_product_is_python(tmp_path, monkeypatch, capsys):
    from numcodecs import Blosc
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    corr = _corrected(tmp_path / "c.zarr", {"A": ROIS[0]["bbox_fullres"]}, ["CD3"], shuffle=Blosc.BITSHUFFLE)
    rois = ROIS[:1]
    _, _, py = _run_worker(tmp_path, "py", slide, "python", monkeypatch, rois, corr, ("CD3",))
    w, errors, rs = _run_worker(tmp_path, "rs", slide, "rust", monkeypatch, rois, corr, ("CD3",))
    assert not errors and w.engine_by_region == {"A": "python"}
    assert "shuffle 2" in capsys.readouterr().out
    _assert_same(py, rs)


@needs_job
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_stop_keeps_the_old_result_and_leaves_no_temporary_store(tmp_path, monkeypatch, engine):
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    _, errors, first = _run_worker(tmp_path, "out", slide, engine, monkeypatch)
    assert not errors
    w, errors, again = _run_worker(tmp_path, "out", slide, engine, monkeypatch, stop_after=2)
    assert errors and "stopped" in errors[-1]
    _assert_same(first, again)                                  # the published store is the old one
    assert not [p for p in os.listdir(tmp_path / "out") if ".inprogress" in p]
    assert w._fuse_job is None


@needs_job
def test_a_corrupt_tile_fails_the_rust_run_like_python(tmp_path, monkeypatch):
    import tifffile
    slide = _slide(tmp_path / "s.ome.tif", "uint8", "zlib")
    with tifffile.TiffFile(slide) as tf:
        page = tf.pages[2]
        off, n = page.dataoffsets[5], page.databytecounts[5]
    with open(slide, "r+b") as fh:                              # garbage inside one tile of CD8
        fh.seek(off + 2)
        fh.write(b"\xff" * (n - 4))
    _, e1, py = _run_worker(tmp_path, "py", slide, "python", monkeypatch)
    w, e2, rs = _run_worker(tmp_path, "rs", slide, "rust", monkeypatch)
    assert e1 and e2, "both runs fail"
    assert w.engine_by_region == {"full": "rust"}
    assert "decode" in e2[-1].lower()
    assert 'channel "CD8", unit [' in e2[-1]                  # which channel, which unit (plan §6)
    assert not py and not rs                                   # nothing published
    assert not [p for p in os.listdir(tmp_path / "rs") if ".inprogress" in p]


@needs_job
def test_the_rust_memory_estimate_counts_the_channels_and_a_block(tmp_path):
    from block01.ui.step0.overview_panel import FullFusionWorker as W
    w = W(_ShapeLoader(), {"channel_remap_params": {}}, 1, 1)
    px = 1024 * 1024 * 2
    assert w._rust_unit_bytes(6, 512 * 512) == px * (6 + 11) + 2 * 512 * 512
    assert w._rust_unit_bytes(80, 4 << 20) == px * 91 + (8 << 20)


@needs_job
def test_stop_waits_for_the_units_in_flight_before_the_store_is_removed(tmp_path, monkeypatch):
    """Plan §7.2 item 4: units still computing when Stop is pressed have
    ended before the temporary store is deleted."""
    import threading
    import time as _time
    from block01.ui.step0 import overview_panel as op
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    lock, state = threading.Lock(), {"active": 0, "peak": 0, "removed_while_active": []}
    real_unit = op.FullFusionWorker._compute_unit

    def slow_unit(self, *a, **k):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            _time.sleep(0.05)                                # keep units in flight
            return real_unit(self, *a, **k)
        finally:
            with lock:
                state["active"] -= 1

    real_rmtree = op.shutil.rmtree

    def watched_rmtree(path, *a, **k):
        if ".inprogress" in str(path):
            with lock:
                state["removed_while_active"].append(state["active"])
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(op.FullFusionWorker, "_compute_unit", slow_unit)
    monkeypatch.setattr(op.shutil, "rmtree", watched_rmtree)
    w, errors, out = _run_worker(tmp_path, "out", slide, "rust", monkeypatch, stop_after=1)
    assert errors and "stopped" in errors[-1]
    assert w.engine_by_region == {"full": "rust"}
    assert state["peak"] > 1, "units were in flight"
    assert state["removed_while_active"] == [0]               # removed once, nothing running
    assert not out and not [p for p in os.listdir(tmp_path / "out") if ".inprogress" in p]


# ── F5-4: the parallel writer (plan §5.3, §7.2 items 3, 4, 6) ────────────
def _watch_writes(monkeypatch, fail_at=None, delay=0.0):
    """Record every write of a fused store: thread, region rows/cols; raise
    on the write starting at `fail_at` (row, col) -- a test-only failure."""
    import threading
    import time as _time
    import zarr as _zarr
    real = _zarr.core.Array.__setitem__
    log, lock, active = [], threading.Lock(), {"n": 0, "peak": 0}

    def setitem(self, sel, value):
        if isinstance(sel, tuple) and len(sel) == 3 and getattr(self, "ndim", 0) == 3:
            key = (sel[0].start, sel[1].start)
            with lock:
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
                log.append((threading.current_thread().name, key, (sel[0].stop, sel[1].stop)))
            try:
                if delay:
                    _time.sleep(delay)
                if fail_at is not None and key == fail_at:
                    raise OSError("test: this write fails")
                return real(self, sel, value)
            finally:
                with lock:
                    active["n"] -= 1
        return real(self, sel, value)

    monkeypatch.setattr(_zarr.core.Array, "__setitem__", setitem)
    return log, active


@needs_job
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_several_writers_write_distinct_chunks_and_progress_stays_in_order(tmp_path, monkeypatch, engine):
    from block01.ui.step0 import overview_panel as op
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    _, _, ref = _run_worker(tmp_path, "ref", slide, "python", monkeypatch)        # before patching
    log, active = _watch_writes(monkeypatch, delay=0.01)
    messages = []
    real_init = op.FullFusionWorker.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self.progress.connect(lambda d, t, m: messages.append(m))
    monkeypatch.setattr(op.FullFusionWorker, "__init__", init)
    w, errors, out = _run_worker(tmp_path, "out", slide, engine, monkeypatch)
    assert not errors
    _assert_same(ref, out)
    assert len({t for t, _, _ in log}) > 1 and active["peak"] > 1         # really in parallel
    spans = [(a, b) for _, a, b in log]
    assert len(spans) == len(set(spans))                                  # each unit written once
    parts = [int(m.split("Part [")[1].split("/")[0]) for m in messages if "Part [" in m]
    assert parts == list(range(1, len(parts) + 1)) and parts            # in unit order


@needs_job
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_a_failing_write_fails_the_run_after_the_writers_end(tmp_path, monkeypatch, engine):
    from block01.ui.step0 import overview_panel as op
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    _, errors, first = _run_worker(tmp_path, "out", slide, engine, monkeypatch)
    assert not errors
    log, active = _watch_writes(monkeypatch, fail_at=(128, 0), delay=0.02)
    removed = []
    real_rmtree = op.shutil.rmtree

    def watched_rmtree(path, *a, **k):
        if ".inprogress" in str(path):
            removed.append(active["n"])
        return real_rmtree(path, *a, **k)
    monkeypatch.setattr(op.shutil, "rmtree", watched_rmtree)
    w, errors, again = _run_worker(tmp_path, "out", slide, engine, monkeypatch)
    assert errors and "this write fails" in errors[-1]
    _assert_same(first, again)                                 # the old result stays published
    assert removed == [0]                                      # no write was running then
    assert not [p for p in os.listdir(tmp_path / "out") if ".inprogress" in p]


def test_the_writer_memory_is_set_aside_before_the_units(monkeypatch):
    from block01.core import bg_parallel
    from block01.ui.step0.overview_panel import FullFusionWorker as W
    w = W(_ShapeLoader(), {"channel_remap_params": {}}, 1, 1)
    out = 1024 * 1024 * 2 * 4
    assert w._writer_bytes(8) == 8 * out * 4
    seen = []
    monkeypatch.setattr(bg_parallel, "mem_available_bytes", lambda: 10 << 30)
    monkeypatch.setattr(bg_parallel, "choose_workers",
                        lambda cpu, n, avail, per_tile_bytes: seen.append(avail) or 4)
    w._unit_workers(100, 6, 50 << 20, reserve_bytes=w._writer_bytes(8))
    assert seen == [(10 << 30) - 8 * out * 4]


@needs_job
@pytest.mark.parametrize("stop_after", [1, 19])        # early, and during the final drain (20 units)
def test_stop_with_writes_in_flight_ends_both_pools_before_the_store_is_removed(tmp_path, monkeypatch,
                                                                              stop_after):
    from block01.ui.step0 import overview_panel as op
    slide = _slide(tmp_path / "s.ome.tif", "uint8")
    _, errors, first = _run_worker(tmp_path, "out", slide, "rust", monkeypatch)
    assert not errors
    log, active = _watch_writes(monkeypatch, delay=0.05)               # writes stay in flight
    removed, finished = [], []
    real_rmtree = op.shutil.rmtree

    def watched_rmtree(path, *a, **k):
        if ".inprogress" in str(path):
            removed.append(active["n"])
        return real_rmtree(path, *a, **k)
    monkeypatch.setattr(op.shutil, "rmtree", watched_rmtree)
    real_init = op.FullFusionWorker.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self.finished.connect(finished.append)
    monkeypatch.setattr(op.FullFusionWorker, "__init__", init)
    w, errors, again = _run_worker(tmp_path, "out", slide, "rust", monkeypatch, stop_after=stop_after)
    assert errors and "stopped" in errors[-1] and not finished
    assert active["peak"] > 1                                  # writes were in flight together
    assert removed == [0]                                      # none running at the deletion
    _assert_same(first, again)                                 # the old result stays published
    assert not [p for p in os.listdir(tmp_path / "out") if ".inprogress" in p]
