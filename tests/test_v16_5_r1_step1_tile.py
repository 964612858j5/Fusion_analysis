"""v16_5 R1 (plan §2, §7 gate 3): Rust's Step1 tiles equal Python's on
synthetic projects written to disk -- every tile of every level of every
channel.

The project: a raw OME pyramid (u8 / u16 / i16 / u32 / i32 / f32, 256-px blocks, three
levels) and a corrected zarr (`root[ROI_1][<channel>]`, f32, Blosc-LZ4, the
product writer's attrs) with a coarse-plane sidecar. Channels:

  a  original                       raw pyramid, ROI-clipped
  b  cucim, product with NaN, +-inf and negative values, no sidecar
  c  tophat, sidecar whose source_identity is stale (not used)
  d  cucim, no product at all       missing
  e  cucim, valid sidecar planes at strides 2 and 4
  f  cucim, sidecar without its completion mark (not used)

Oracle: `Step1TileProvider.read_tile_native` (and `read_tile`). Integers
equal; floats within |a-b| <= 1e-6 + 1e-6|b|; NaN and validity equal. The
slab path of the reduction is driven with tiny budgets through
`step1_source.read_tile(max_elements=...)`.
"""

import json
import types

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.ui.step1_viewer_host import Step1TileProvider  # noqa: E402
from block01.viewer import native_tile as nt  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

H0, W0 = 1400, 1100
NAMES = ["a", "b", "c", "d", "e", "f"]
DECISIONS = {"a": "original", "b": "cucim", "c": "tophat", "d": "cucim", "e": "cucim", "f": "cucim"}
TILE = 256


def _raw(path, dtype):
    rng = np.random.default_rng(11)
    if np.dtype(dtype).kind == "f":
        data = rng.normal(100, 40, size=(6, H0, W0)).astype(dtype)
        data[0, 5:9, 5:9] = np.nan
        data[0, 20, 20] = np.inf
    else:
        info = np.iinfo(dtype)
        data = rng.integers(info.min, info.max, size=(6, H0, W0), dtype=dtype, endpoint=True)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(256, 256), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": NAMES}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(256, 256), compression="zlib")
    return str(path)


def _product_attrs(name, bbox, shape, token):
    return {"channel_name": name, "correction_method": DECISIONS[name],
            "correction_param_name": "cucim_sigma", "correction_param_value": 50,
            "bg_correction_algo_version": "7", "roi_name": "ROI_1",
            "roi_bbox_fullres": [int(v) for v in bbox], "source_shape": [int(v) for v in shape],
            "source_identity": token, "dtype": "float32"}


def _project(tmp_path, dtype, bbox):
    step0 = tmp_path / "step0"
    step0.mkdir()
    slide = _raw(tmp_path / "slide.ome.tif", dtype)
    y0, y1, x0, x1 = bbox
    shape = (y1 - y0, x1 - x0)
    rng = np.random.default_rng(5)
    root = zarr.open_group(str(step0 / "corrected_channels.zarr"), mode="w")
    group = root.require_group("ROI_1")
    attrs = {}
    for name in ("b", "c", "e", "f"):
        values = rng.normal(0, 50, size=shape).astype(np.float32)
        if name == "b":
            values[10:14, 10:30] = np.nan
            values[200, 200:240] = np.inf
            values[300, 300:310] = -np.inf
        arr = group.create_dataset(name, shape=shape, chunks=(300, 250), dtype=np.float32)
        arr[:, :] = values
        attrs[name] = _product_attrs(name, bbox, shape, f"token-{name}")
        arr.attrs.update(attrs[name])
    side = zarr.open_group(str(step0 / "corrected_coarse.zarr"), mode="w").require_group("ROI_1")
    table0 = sources.Step1SourceTable(decisions=DECISIONS,
                                      corrected_zarr_path=str(step0 / "corrected_channels.zarr"),
                                      roi_name="ROI_1", roi_bbox=bbox)
    for name, stride, tweak in (("c", 2, "stale"), ("e", 2, None), ("e", 4, None), ("f", 2, "incomplete")):
        origin, bshape = sources.coarse_block_range(bbox, stride)
        rect = (origin[0] * stride, (origin[0] + bshape[0]) * stride,
                origin[1] * stride, (origin[1] + bshape[1]) * stride)
        mean, valid = sources.reduce_corrected(table0.region(name), rect, stride)
        assert valid.all()
        plane = side.create_dataset(name if stride == 2 else f"{name}_s4", shape=bshape,
                                    chunks=(64, 64), dtype=np.float32)
        plane[:, :] = mean
        ident = sources.coarse_plane_identity(attrs[name], stride, level=1 if stride == 2 else 2)
        if tweak == "stale":
            ident["source_identity"] = "an-earlier-write"
        plane.attrs.update(ident)
        plane.attrs["block_origin"] = [int(origin[0]), int(origin[1])]
        if tweak != "incomplete":
            plane.attrs["complete"] = True
    # the table opens planes by channel name: the stride-4 plane of e lives
    # in its own sidecar group so both strides resolve
    return slide, str(step0 / "corrected_channels.zarr")


class _Opener:
    """`open_corrected_channel_array`, but the sidecar's stride-4 plane of
    `e` is found under `e_s4` (one array per channel name per group)."""

    def __init__(self):
        from block01.utils.calibration_source import open_corrected_channel_array
        self.real = open_corrected_channel_array
        self.stride = None

    def __call__(self, path, channel, roi_name=None):
        if path.endswith("corrected_coarse.zarr") and self.stride == 4:
            channel = f"{channel}_s4"
        return self.real(path, channel, roi_name)


def _table(zarr_path, bbox, opener):
    table = sources.Step1SourceTable(decisions=DECISIONS, corrected_zarr_path=zarr_path,
                                     roi_name="ROI_1", roi_bbox=bbox, open_corrected=opener)
    for stride in (2, 4):                    # resolve both strides' planes now
        opener.stride = stride
        for name in NAMES:
            table.coarse_plane(name, stride)
    opener.stride = None
    return table


def _py(provider, channel, level, ty, tx):
    tile = types.SimpleNamespace(level=level, ty=ty, tx=tx, grid=types.SimpleNamespace(tile_size=TILE))
    native, _ = provider.read_tile_native(channel, tile)
    values = np.asarray(native.values)
    if native.valid_rect is None:
        mask = np.zeros(values.shape, bool)
    elif native.valid_rect == nt.FINITE:
        mask = np.isfinite(values)
    else:
        y0, y1, x0, x1 = native.valid_rect
        mask = np.zeros(values.shape, bool)
        mask[y0:y1, x0:x1] = True
    floats, _ = provider.read_tile(channel, tile)
    return values, mask, floats, tile


def _rs(reader, provider, channel, tile):
    rect, stride = provider._level_rect(tile)
    t = reader.read_tile(channel, tile.level, *rect, stride, TILE)
    values = np.frombuffer(t["data"], np.dtype(t["dtype"]).newbyteorder("<")).reshape(t["rows"], t["cols"])
    if t["valid"] == "nothing":
        mask = np.zeros(values.shape, bool)
    elif t["valid"] == "finite":
        mask = np.isfinite(values)
    else:
        y0, y1, x0, x1 = t["rect"]
        mask = np.zeros(values.shape, bool)
        mask[y0:y1, x0:x1] = True
    return values, mask


def _close(a, b):
    fa, fb = np.asarray(a, np.float64), np.asarray(b, np.float64)
    if fa.shape != fb.shape or not np.array_equal(np.isnan(fa), np.isnan(fb)):
        return False
    ok = ~np.isnan(fa)
    same_inf = np.array_equal(np.isinf(fa), np.isinf(fb)) and np.array_equal(fa[np.isinf(fa)], fb[np.isinf(fb)])
    fin = ok & ~np.isinf(fa)
    return same_inf and bool(np.all(np.abs(fa[fin] - fb[fin]) <= 1e-6 + 1e-6 * np.abs(fb[fin])))


def _assert_same(py, rs, where):
    pv, pm, floats, _tile = py
    rv, rm = rs
    assert pv.shape == rv.shape, where
    assert np.array_equal(pm, rm), where
    if pv.dtype.kind in "ui":
        assert pv.dtype == rv.dtype and np.array_equal(pv, rv), where
    else:
        assert rv.dtype == np.float32 and _close(pv, rv), where
    # read_tile's float picture: Rust's floats as they are (NaN where not
    # valid, +-inf kept), or its integers with NaN outside their rectangle
    if rv.dtype.kind == "f":
        as_float = rv
    else:
        as_float = np.where(rm, rv.astype(np.float32), np.nan)
    assert _close(floats, as_float), where


def _rig(tmp_path, dtype, bbox):
    slide, zpath = _project(tmp_path, dtype, bbox)
    raw = RawTileProvider(slide)
    opener = _Opener()
    table = _table(zpath, bbox, opener)
    provider = Step1TileProvider(raw, table)
    strides = [max(1, int(round(raw.level_downsample(l)))) for l in range(raw.num_levels)]
    manifest = build_corrected_manifest(table, raw, strides)
    return raw, table, provider, manifest


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.int16, np.uint32, np.int32, np.float32])
@pytest.mark.parametrize("bbox", [(100, 1250, 70, 990), None], ids=["partial_roi", "whole_slide"])
def test_a_every_tile_matches(tmp_path, dtype, bbox):
    product_bbox = bbox or (0, H0, 0, W0)
    raw, table, provider, manifest = _rig(tmp_path, dtype, product_bbox)
    if bbox is None:
        manifest["roi"] = None
        table._roi_bbox = None                 # the whole slide is the region
        manifest["revision"] = table.identity_token()
    try:
        # the table's own decisions, as the manifest carries them
        kinds = {n: c["source"] for n, c in manifest["channels"].items()}
        assert kinds == {"a": "raw", "b": "corrected", "c": "corrected", "d": "missing",
                         "e": "corrected", "f": "corrected"}
        planes = {n: [p["stride"] for p in c.get("planes", [])] for n, c in manifest["channels"].items()}
        assert planes["e"] == [2, 4] and planes["c"] == [] and planes["f"] == [] and planes["b"] == []
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        assert reader.revision == table.identity_token()
        for level in range(raw.num_levels):
            h, w = raw.level_shape(level)
            for ty in range(-(-h // TILE)):
                for tx in range(-(-w // TILE)):
                    for name in NAMES:
                        py = _py(provider, name, level, ty, tx)
                        _assert_same(py, _rs(reader, provider, name, py[3]), (level, ty, tx, name))
    finally:
        raw.close()


@pytest.mark.parametrize("budget", [997, 5000, 70000])
def test_b_the_slab_path_matches_at_tiny_budgets(tmp_path, budget):
    bbox = (100, 1250, 70, 990)
    raw, table, provider, manifest = _rig(tmp_path, np.uint8, bbox)
    manifest["budget"] = budget
    try:
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        rng = np.random.default_rng(budget)
        for _ in range(60):
            stride = int(rng.choice([1, 2, 3, 4, 7]))
            hk, wk = -(-H0 // stride), -(-W0 // stride)
            y0, x0 = int(rng.integers(0, hk)), int(rng.integers(0, wk))
            rect = (y0, min(hk, y0 + int(rng.integers(1, 400))), x0, min(wk, x0 + int(rng.integers(1, 400))))
            want, valid = sources.read_tile(table, "b", rect, stride=stride, max_elements=budget)
            t = reader.read_tile("b", 0, *rect, stride, TILE)
            got = np.frombuffer(t["data"], "<f4").reshape(t["rows"], t["cols"])
            assert want.shape == got.shape, (stride, rect)
            assert np.array_equal(valid, ~np.isnan(got) | (np.isnan(want) & valid)), (stride, rect)
            assert _close(np.where(valid, want, np.nan), got), (stride, rect)
    finally:
        raw.close()


def test_c_a_product_replaced_after_the_manifest_is_refused(tmp_path):
    bbox = (100, 1250, 70, 990)
    raw, table, provider, manifest = _rig(tmp_path, np.uint8, bbox)
    try:
        path = manifest["channels"]["b"]["product"]["dir"] + "/.zattrs"
        with open(path) as fh:
            attrs = json.load(fh)
        attrs["source_identity"] = "a-newer-write"
        with open(path, "w") as fh:
            json.dump(attrs, fh)
        with pytest.raises(Exception, match="changed after the manifest"):
            core.Step1Reader(build_raw_manifest(raw), manifest)
    finally:
        raw.close()


def test_d_layouts_the_core_does_not_take_are_refused(tmp_path):
    bbox = (100, 1250, 70, 990)
    raw, table, provider, manifest = _rig(tmp_path, np.uint8, bbox)
    try:
        for field, value in (("dtype", "<f8"), ("order", "F"), ("cname", "zstd"),
                             ("filters_none", False), ("separator", "-")):
            bent = json.loads(json.dumps(manifest))
            bent["channels"]["b"]["product"][field] = value
            with pytest.raises(Exception, match="not taken"):
                core.Step1Reader(build_raw_manifest(raw), bent)
    finally:
        raw.close()
