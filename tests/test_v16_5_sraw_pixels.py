"""v16_5 S-Raw: the Rust-drawn stable Step1 picture equals the Python one.

`scripts/sraw_pixel_probe.py` opens the REAL Step1 mount on an OME-TIFF in
one backend per process (one GL widget per process), waits for the stable
picture in several camera cases (whole slide, a middle level, level 0, past
the slide's corner, magnified with smoothing) and saves the layer's `final`
readback. Both backends must give the same bytes, alpha included.

GPU gate as the other GPU tests: BLOCK01_REQUIRE_STEP1_GPU=1, real display.
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1":
    pytest.skip("GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1", allow_module_level=True)
pytest.importorskip("fusion_viewer_core")
tifffile = pytest.importorskip("tifffile")

PROBE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sraw_pixel_probe.py"
TMA3 = "/sdb1/cop/Kevin/TMA3/FINALRUN_updated_TMA3_Scan1_bgcorrected.ome.tiff"


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    """3 uint8 channels, 3000 x 2500 (edge tiles of every level are cut),
    512-px deflate tiles, three levels."""
    path = tmp_path_factory.mktemp("sraw") / "synth3.ome.tif"
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:3000, 0:2500]
    data = np.stack([((yy // 7 + xx // 5 + k * 40) % 256).astype(np.uint8)
                     ^ rng.integers(0, 30, (3000, 2500), dtype=np.uint8) for k in range(3)])
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3", "CD8"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _run(backend, slide, out, *extra, timeout=600):
    proc = subprocess.run([sys.executable, str(PROBE), "--backend", backend, "--slide", slide,
                           "--out", str(out), *extra], capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ))
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert lines, f"{backend}: no result\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1]), np.load(out)


def _compare(tmp_path, slide, *extra):
    py, py_px = _run("python", slide, tmp_path / "py.npz", *extra)
    rs, rs_px = _run("rust", slide, tmp_path / "rust.npz", *extra)
    assert py["binding"] == "Step1GpuBinding", py
    assert rs["binding"] == "Step1CoreBinding", rs
    assert rs["scheduler"] == "DeferredScheduler" and rs["scheduler_started"] is False, rs
    for case in py["cases"] + rs["cases"]:
        assert case["stable"], case
    for name in py_px.files:
        a, b = py_px[name], rs_px[name]
        assert a.shape == b.shape and a.size, (name, a.shape, b.shape)
        differ = int((np.abs(a.astype(int) - b.astype(int)).max(axis=2) > 0).sum())
        assert differ == 0, f"{name}: {differ} pixels differ"


def test_a_stable_pictures_are_identical_smooth_on(tmp_path, slide):
    _compare(tmp_path, slide, "--smooth", "1")


def test_b_stable_pictures_are_identical_smooth_off(tmp_path, slide):
    _compare(tmp_path, slide, "--smooth", "0")


def test_c_real_slide_twenty_channels(tmp_path):
    if not os.path.exists(TMA3):
        pytest.skip("Kevin TMA3 is not on this machine")
    from block01.viewer.raw_tile_provider import RawTileProvider
    raw = RawTileProvider(TMA3)
    names = raw.channel_names[:20]
    raw.close()
    _compare(tmp_path, TMA3, "--channels", ",".join(names))
