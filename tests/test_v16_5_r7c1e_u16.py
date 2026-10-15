"""v16_5 R7c 1E (docs/v16_5_r7c_odon_alignment_plan.md §2b): a corrected
channel whose every drawn stride has an accepted display layer is drawn as
uint16 (R16UI) instead of float32.

Synthetic projects under tmp_path only.

* Eligibility: every stride (1 and the pyramid's) served by a display
  layer; not with `BLOCK01_STEP1_DISPLAY_U16=0`, `..._DISPLAY_S1=0`,
  `..._DISPLAY_LAYERS=0`, or a missing layer.
* The core's uint16 tiles equal the layers, valid on the layer's rectangle.
* A layer read that fails falls back to the reduction quantised the layers'
  way, in float64: a stride-32 block of 511 x 32769 and 513 x 32768 is
  32768 (a float32 mean would round to 32769).
* GPU: the stable pictures of uint16 and float32 uploads are pixel-identical
  (`scripts/r2_pixel_compare.py`; BLOCK01_REQUIRE_STEP1_GPU=1, real display).
"""

import json
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.viewer import display_pyramid as dp  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDE = 1024
STRIDES = [2, 4, 8, 16, 32]


def _project(tmp_path, layers=True, values=None):
    """A 6-level uint16 slide (channels r, c) and an integer product for c."""
    rng = np.random.default_rng(2)
    raw = rng.integers(0, 4000, (2, SIDE, SIDE), dtype=np.uint16)
    slide = tmp_path / "slide.ome.tif"
    with tifffile.TiffWriter(str(slide), ome=True) as tw:
        tw.write(raw, subifds=5, tile=(256, 256), metadata={"axes": "CYX", "Channel": {"Name": ["r", "c"]}})
        cur = raw
        for _ in range(5):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(256, 256) if cur.shape[1] >= 256 else None)
    if values is None:
        values = rng.integers(0, 3000, (SIDE, SIDE)).astype(np.float32)
    bbox = [0, SIDE, 0, SIDE]
    product = zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="w").require_group(
        "R").create_dataset("c", shape=(SIDE, SIDE), chunks=(1024, 1024), dtype=np.float32)
    product[:, :] = values
    product.attrs.update({"channel_name": "c", "correction_method": "tophat", "roi_name": "R",
                          "roi_bbox_fullres": bbox, "source_shape": [SIDE, SIDE], "source_identity": "tok"})
    if layers:
        b = dp.DisplayBuilder(dp.display_root(tmp_path / "corrected_channels.zarr"), "R", "c", bbox, STRIDES)
        b.add(values, 0, 0)
        assert b.finish(dict(product.attrs), product), b.refused
    table = sources.Step1SourceTable(decisions={"r": "original", "c": "tophat"},
                                     corrected_zarr_path=str(tmp_path / "corrected_channels.zarr"),
                                     roi_name="R", roi_bbox=bbox)
    return str(slide), table, values


def _manifest(slide, table):
    raw = RawTileProvider(slide)
    try:
        strides = [1] + STRIDES
        return raw, build_corrected_manifest(table, raw, strides)
    except Exception:
        raw.close()
        raise


@pytest.mark.parametrize("env,expect", [({}, True), ({"BLOCK01_STEP1_DISPLAY_U16": "0"}, False),
                                        ({"BLOCK01_STEP1_DISPLAY_S1": "0"}, False),
                                        ({"BLOCK01_STEP1_DISPLAY_LAYERS": "0"}, False)],
                         ids=["on", "u16-off", "s1-off", "layers-off"])
def test_eligibility(tmp_path, monkeypatch, env, expect):
    for k in ("BLOCK01_STEP1_DISPLAY_U16", "BLOCK01_STEP1_DISPLAY_S1", "BLOCK01_STEP1_DISPLAY_LAYERS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    slide, table, _v = _project(tmp_path)
    raw, manifest = _manifest(slide, table)
    raw.close()
    assert manifest["channels"]["c"]["display_u16"] is expect


def test_a_missing_layer_keeps_float(tmp_path):
    slide, table, _v = _project(tmp_path)
    import shutil
    shutil.rmtree(os.path.join(dp.display_root(tmp_path / "corrected_channels.zarr"), "R", "c", "s8"))
    raw, manifest = _manifest(slide, sources.Step1SourceTable(
        decisions={"r": "original", "c": "tophat"},
        corrected_zarr_path=str(tmp_path / "corrected_channels.zarr"), roi_name="R", roi_bbox=[0, SIDE, 0, SIDE]))
    raw.close()
    assert manifest["channels"]["c"]["display_u16"] is False


def test_a_float_coarse_plane_chosen_keeps_float(tmp_path):
    """The stride-32 display layer gone and a float32 coarse plane there:
    `coarse_plane` picks the float plane, so the channel stays float32."""
    slide, table, values = _project(tmp_path)
    import shutil
    shutil.rmtree(os.path.join(dp.display_root(tmp_path / "corrected_channels.zarr"), "R", "c", "s32"))
    product = zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="r")["R"]["c"]
    origin, shape = sources.coarse_block_range([0, SIDE, 0, SIDE], 32)
    plane = zarr.open_group(str(tmp_path / sources.COARSE_SIDECAR_DIRNAME), mode="w").require_group(
        "R").create_dataset("c", shape=shape, chunks=(64, 64), dtype=np.float32)
    plane[:, :] = values.reshape(32, 32, 32, 32).mean(axis=(1, 3))
    plane.attrs.update(sources.coarse_plane_identity(dict(product.attrs), 32, level=5))
    plane.attrs.update(block_origin=list(origin), complete=True)
    table = sources.Step1SourceTable(decisions={"r": "original", "c": "tophat"},
                                     corrected_zarr_path=str(tmp_path / "corrected_channels.zarr"),
                                     roi_name="R", roi_bbox=[0, SIDE, 0, SIDE])
    chosen = table.coarse_plane("c", 32)
    assert chosen is not None and not chosen.display
    raw, manifest = _manifest(slide, table)
    raw.close()
    assert manifest["channels"]["c"]["display_u16"] is False


def _tile(reader, stride, y0, x0, size):
    level = int(np.log2(stride))
    t = reader.read_tile("c", level, y0, y0 + size, x0, x0 + size, stride, size)
    assert t["dtype"] == "uint16"
    v = np.frombuffer(t["data"], "<u2").reshape(t["rows"], t["cols"])
    return v, t


def test_uint16_tiles_equal_the_layers(tmp_path):
    slide, table, values = _project(tmp_path)
    raw, manifest = _manifest(slide, table)
    try:
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        root = dp.display_root(tmp_path / "corrected_channels.zarr")
        for stride in [1] + STRIDES:
            layer = dp.accept(root, "R/c", dict(zarr.open_group(str(tmp_path / "corrected_channels.zarr"),
                                                               mode="r")["R"]["c"].attrs), [0, SIDE, 0, SIDE],
                              stride)[...]
            size = min(256, layer.shape[0])
            v, t = _tile(reader, stride, 0, 0, size)
            assert t["valid"] == "rect" and t["rect"] == (0, size, 0, size)
            assert np.array_equal(v, layer[:size, :size]), stride
    finally:
        raw.close()


def test_a_failed_layer_read_is_quantised_in_float64(tmp_path):
    values = np.zeros((SIDE, SIDE), np.float32)
    block = np.full(1024, 32768, np.float32)
    block[:511] = 32769
    values[:32, :32] = block.reshape(32, 32)
    slide, table, _v = _project(tmp_path, values=values)
    root = dp.display_root(tmp_path / "corrected_channels.zarr")
    # the stride-32 layer says 32768 (float64 mean 32768.499, rounded once)
    attrs = dict(zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="r")["R"]["c"].attrs)
    assert dp.accept(root, "R/c", attrs, [0, SIDE, 0, SIDE], 32)[0, 0] == 32768
    raw, manifest = _manifest(slide, table)
    try:
        with open(os.path.join(root, "R", "c", "s32", "0.0"), "wb") as fh:
            fh.write(b"not a blosc frame")           # the layer read now fails
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        v, t = _tile(reader, 32, 0, 0, 32)
        assert v[0, 0] == 32768                     # not 32769: no float32 on the way
        assert t["valid"] == "rect"
    finally:
        raw.close()


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    out = tmp_path_factory.mktemp("r7c1e") / "synth"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "r2_synth_project.py"), str(out)],
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-2000:]
    project = json.load(open(out / "project.json"))
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_corrected_display.py"),
                            project["zarr"], "--in-place", "--slide", project["slide"]],
                           capture_output=True, text=True, timeout=600)
    published = [json.loads(l) for l in built.stdout.splitlines() if l.startswith('{"published')]
    assert {d["channel"] for d in published if d["published"]} >= {"c", "e"}, built.stdout[-2000:]
    return out


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")
@pytest.mark.parametrize("mode,channels,env", [
    ("overlay", "a,c,e", {}),
    ("overlay", "a,b,c,e", {}),                                   # uint16 c, e beside float32 b
    ("fusion", "a,b,c,e", {}),
    ("overlay", "a,b,c,e", {"BLOCK01_A9_RIG_WINDOW": "100,140"}),  # a narrow window (gamma 0.7 on odd rows)
    ("overlay", "a,b,c,e", {"BLOCK01_A9_RIG_GPU_BYTES": str(160 * 1024 * 1024)}),  # budget pressure (float32 still fits)
], ids=["overlay", "mixed", "fusion", "narrow", "budget"])
def test_uint16_and_float32_uploads_draw_the_same_picture(tmp_path, synth, mode, channels, env):
    out = synth
    pics = {}
    for tag, u16 in (("u16", "1"), ("f32", "0")):
        env_ = dict(os.environ, BLOCK01_STEP1_DISPLAY_U16=u16, BLOCK01_A9_DRAW_LOG="1", **env)
        npz = tmp_path / f"{tag}.npz"
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "r2_pixel_compare.py"), "probe",
                              "--backend", "rust", "--project", str(out / "project.json"), "--out", str(npz),
                              "--channels", channels, "--mode", mode],
                             capture_output=True, text=True, timeout=1200, env=env_)
        lines = [l for l in run.stdout.splitlines() if l.startswith("{")]
        assert lines, run.stdout[-2000:] + run.stderr[-3000:]
        assert all(c["stable"] for c in json.loads(lines[-1])["cases"])
        # the run really drew c and e as uint16 (or not); b never
        marks = {l.split(":")[0].split()[-1]: l.rstrip().endswith(" u16")
                 for l in run.stdout.splitlines() if l.startswith("[sources] ")}
        assert marks.get("c") is (u16 == "1") and marks.get("e") is (u16 == "1"), marks
        assert marks.get("b") is False, marks
        pics[tag] = np.load(npz)
    for case in pics["u16"].files:
        assert np.array_equal(pics["u16"][case], pics["f32"][case]), case
