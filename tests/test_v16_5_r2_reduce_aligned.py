"""v16_5 R2 (plan §2.2, §3, §5.3): the corrected reduction walks slabs on
the PRODUCT's chunk grid (user ruling 2026-10-12) and still answers what
`step1_source.reduce_corrected` answers: values within |a-b| <= 1e-6 +
1e-6|b|, validity exactly, NaN where Python has NaN -- on a product whose
region starts on neither the chunk nor the block grid, at many strides and
budgets. One read decodes each chunk it touches once, with the default
budget (one slab) and with budgets that cut the region into many slabs
(each slab whole chunks; codex R2 review).
"""

import types

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

H0, W0 = 1400, 1100
BBOX = (101, 1253, 67, 991)                 # off the 300x250 chunks and every block grid
CHUNKS = (300, 250)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root_dir = tmp_path_factory.mktemp("r2red")
    slide = root_dir / "s.ome.tif"
    data = np.zeros((2, H0, W0), np.uint8)
    with tifffile.TiffWriter(str(slide), ome=True) as tw:
        tw.write(data, tile=(256, 256), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["b", "c"]}})
    y0, y1, x0, x1 = BBOX
    shape = (y1 - y0, x1 - x0)
    values = np.random.default_rng(9).normal(0, 1e4, size=shape).astype(np.float32)
    values[40:44, 30:70] = np.nan
    values[500, 100:140] = np.inf
    values[700, 300:330] = -np.inf
    values[900:960, 600:700] = 3.0e7                       # large magnitudes, sums stress
    zpath = root_dir / "step0" / "corrected_channels.zarr"
    arr = zarr.open_group(str(zpath), mode="w").require_group("ROI_1").create_dataset(
        "b", shape=shape, chunks=CHUNKS, dtype=np.float32)
    arr[:, :] = values
    arr.attrs.update({"roi_bbox_fullres": list(BBOX), "roi_name": "ROI_1",
                      "source_identity": "t", "correction_method": "cucim"})
    raw = RawTileProvider(str(slide))
    table = sources.Step1SourceTable(decisions={"b": "cucim"}, corrected_zarr_path=str(zpath),
                                     roi_name="ROI_1", roi_bbox=list(BBOX))
    manifest = build_corrected_manifest(table, raw, [1])
    yield raw, table, manifest
    raw.close()


def _rust(reader, rect, stride):
    t = reader.read_tile("b", 0, *rect, stride, 512)
    return np.frombuffer(t["data"], "<f4").reshape(t["rows"], t["cols"])


def _python(table, rect, stride, budget):
    values, valid = sources.read_tile(table, "b", rect, stride=stride, max_elements=budget)
    return np.where(valid, values, np.nan).astype(np.float32), valid


def _assert_close(want, got, where):
    assert want.shape == got.shape, where
    assert np.array_equal(np.isnan(want), np.isnan(got)), where
    finite = np.isfinite(want)
    assert np.array_equal(np.isinf(want), np.isinf(got)) and np.array_equal(want[np.isinf(want)], got[np.isinf(got)]), where
    a, b = got[finite].astype(np.float64), want[finite].astype(np.float64)
    assert np.all(np.abs(a - b) <= 1e-6 + 1e-6 * np.abs(b)), (where, float(np.max(np.abs(a - b))))


@pytest.mark.parametrize("budget", [sources.MAX_REDUCTION_ELEMENTS, 70000, 5000, 997])
def test_a_values_and_validity_match_python(rig, budget):
    raw, table, manifest = rig
    manifest = dict(manifest, budget=budget)
    reader = core.Step1Reader(build_raw_manifest(raw), manifest)
    rng = np.random.default_rng(budget)
    cases = [(1, (0, H0, 0, W0)), (64, (0, 22, 0, 18)), (16, (0, 88, 0, 69))]
    for _ in range(40):
        stride = int(rng.choice([1, 2, 3, 4, 7, 16, 64]))
        hk, wk = -(-H0 // stride), -(-W0 // stride)
        y0, x0 = int(rng.integers(0, hk)), int(rng.integers(0, wk))
        cases.append((stride, (y0, min(hk, y0 + int(rng.integers(1, 300))),
                               x0, min(wk, x0 + int(rng.integers(1, 300))))))
    for stride, rect in cases:
        want, _valid = _python(table, rect, stride, budget)
        _assert_close(want, _rust(reader, rect, stride), (stride, rect, budget))


@pytest.mark.parametrize("budget", [None, 4 * 75000, 2 * 75000, 75000])
@pytest.mark.parametrize("stride", [2, 4, 16, 64])
def test_b_each_chunk_is_decoded_once_per_read(rig, stride, budget):
    raw, table, manifest = rig
    if budget is not None:                                # 75000 = one chunk: 1 x 1 .. 2 x 2 chunk slabs
        manifest = dict(manifest, budget=budget)
    reader = core.Step1Reader(build_raw_manifest(raw), manifest)
    hk, wk = -(-H0 // stride), -(-W0 // stride)
    before = core.chunks_decoded_for_test()
    got = _rust(reader, (0, hk, 0, wk), stride)           # the whole region at once
    decoded = core.chunks_decoded_for_test() - before
    want, _valid = _python(table, (0, hk, 0, wk), stride, budget or sources.MAX_REDUCTION_ELEMENTS)
    _assert_close(want, got, (stride, budget))
    ny = -(-(BBOX[1] - BBOX[0]) // CHUNKS[0])
    nx = -(-(BBOX[3] - BBOX[2]) // CHUNKS[1])
    assert decoded == ny * nx, (decoded, ny * nx)
