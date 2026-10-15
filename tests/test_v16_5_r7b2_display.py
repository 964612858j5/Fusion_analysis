"""v16_5 R7b B2 (docs/v16_5_r7b_corrected_read_plan.md B2.1-B2.4): the
display layers of a corrected channel (the bridge level was withdrawn by
R7c step 0, docs/v16_5_r7c_odon_alignment_plan.md).

On synthetic projects under tmp_path only:

* Step0's save writes, for every level of the slide's pyramid, a uint16
  layer equal to the level-0 block means rounded once (half up) -- through
  an unaligned ROI with a polygon, in small writer tiles -- and leaves the
  product and the coarse plane exactly what they are without it.
* A channel that is not all integers 0..65535 gets none; a cancel leaves
  none; a second save retires the first save's layers.
* Step1 prefers an accepted layer (Python table and the core's manifest);
  incomplete, stale, wrong-dtype or switched-off layers fall back.
* Python and Rust tiles are equal on layers.
* A GPU failure part-way ends the first display builder before the channel
  is computed again, and the layers then match the CPU product.
* The backfill never writes the product and refuses an output that is the
  product, inside it, or the display store beside it.
"""

import os
import subprocess
import sys

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.ui.step0 import search_ctrl  # noqa: E402
from block01.viewer import display_pyramid as dp  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402

ROI = [96, 96 + 640, 48, 48 + 512]          # unaligned on purpose
POLY = [(48, 96), (560, 96), (560, 736), (300, 600), (48, 736)]
CHANNELS = ("CH_A", "CH_B")


class _Loader:
    def __init__(self, path, data):
        self.filepath = str(path)
        self._data = data
        self.shape = data.shape[1:]
        self.ch_map = {name: i for i, name in enumerate(CHANNELS)}

    def _read_roi_zarr(self, index, y0, y1, x0, x1):
        return self._data[index, y0:y1, x0:x1]


def _slide(tmp_path, seed=1):
    """A 3-level pyramid (strides 2 and 4) of uint16."""
    data = (np.random.default_rng(seed).random((len(CHANNELS), 1024, 768)) * 400).astype(np.uint16)
    path = tmp_path / f"slide_{seed}.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as writer:
        writer.write(data, subifds=2, tile=(256, 256), photometric="minisblack",
                     metadata={"axes": "CYX", "Channel": {"Name": list(CHANNELS)}})
        reduced = data
        for _ in range(2):
            reduced = reduced[:, ::2, ::2].copy()
            writer.write(reduced, subfiletype=1, tile=(256, 256), photometric="minisblack")
    return path, data


def _save(tmp_path, out, method="tophat", seed=1, hook=None, monkeypatch=None):
    path, data = _slide(tmp_path, seed)
    if monkeypatch is not None:
        real = search_ctrl._tile_slices
        monkeypatch.setattr(search_ctrl, "_tile_slices", lambda h, w, t, o: real(h, w, 256, o))
    cfg = {"channel_decisions": {"CH_A": method},
           "method_params": {"tophat_radius": 3, "cucim_sigma": 6}, "channel_params": {}}
    worker = search_ctrl.WsiCorrectionWorker(
        _Loader(path, data), str(out), cfg,
        rois=[{"name": "R", "bbox_fullres": list(ROI), "polygon_fullres": POLY}])
    got = {"finished": [], "error": [], "canceled": []}
    worker.finished.connect(lambda p, d: got["finished"].append(p))
    worker.error.connect(got["error"].append)
    worker.canceled.connect(got["canceled"].append)
    if hook:
        hook(worker)
    worker.run()
    assert got["error"] == [], got["error"]
    return got


def _product(out):
    return zarr.open_group(str(out / "corrected_channels.zarr"), mode="r")["R"]["CH_A"]


def _rounded_reduction(product, stride):
    region = sources.CorrectedRegion(product, ROI)
    origin, shape = sources.coarse_block_range(ROI, stride)
    rect = (origin[0] * stride, (origin[0] + shape[0]) * stride,
            origin[1] * stride, (origin[1] + shape[1]) * stride)
    mean, valid = sources.reduce_corrected(region, rect, stride)
    assert valid.all()
    return np.floor(mean.astype(np.float64) + 0.5).astype(np.uint16)


def test_a_save_writes_every_level_as_the_rounded_reduction(tmp_path, monkeypatch):
    out = tmp_path / "out"
    assert _save(tmp_path, out, monkeypatch=monkeypatch)["finished"]
    product = _product(out)
    attrs = dict(product.attrs)
    root = dp.display_root(out / "corrected_channels.zarr")
    for stride in (2, 4):
        layer = dp.accept(root, "R/CH_A", attrs, ROI, stride)
        assert layer is not None, stride
        want = _rounded_reduction(product, stride)
        got = layer[...]
        assert got.dtype == np.uint16 and np.array_equal(got, want), stride
        # the source error of a layer is at most one half
        mean, _v = sources.reduce_corrected(sources.CorrectedRegion(product, ROI),
                                            tuple(np.array(sources.coarse_block_range(ROI, stride)[0]
                                                           ).repeat(2) * stride + [0, layer.shape[0] * stride,
                                                                                   0, layer.shape[1] * stride]),
                                            stride)
        assert np.abs(got - mean).max() <= 0.5
    group = zarr.open_group(os.path.join(root, "R", "CH_A"), mode="r")
    assert [d["path"] for d in group.attrs["multiscales"][0]["datasets"]] == ["s1", "s2", "s4"]
    # R7c 1A: level 0 itself, lossless as uint16
    s1 = dp.accept(root, "R/CH_A", attrs, ROI, 1)
    assert s1 is not None and s1.dtype == np.uint16
    assert np.array_equal(s1[...].astype(np.float32), product[...])


def test_the_product_and_the_coarse_plane_do_not_change(tmp_path, monkeypatch):
    with_layers, without = tmp_path / "with", tmp_path / "without"
    assert _save(tmp_path, with_layers, monkeypatch=monkeypatch)["finished"]
    monkeypatch.setattr(dp, "level_strides", lambda path: [])
    assert _save(tmp_path, without, monkeypatch=monkeypatch)["finished"]
    assert not os.path.isdir(dp.display_root(without / "corrected_channels.zarr"))
    assert np.array_equal(_product(with_layers)[...], _product(without)[...])
    assert _product(with_layers).dtype == np.float32           # analysis reads the float product
    planes = [zarr.open_group(str(d / "corrected_coarse.zarr"), mode="r")["R"]["CH_A"][...]
              for d in (with_layers, without)]
    assert np.array_equal(*planes)


def test_a_channel_that_is_not_all_integers_gets_no_layers(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    assert _save(tmp_path, out, method="cucim", monkeypatch=monkeypatch)["finished"]
    attrs = dict(_product(out).attrs)
    root = dp.display_root(out / "corrected_channels.zarr")
    assert all(dp.accept(root, "R/CH_A", attrs, ROI, s) is None for s in (2, 4))
    assert not os.path.isdir(os.path.join(root, "R", "CH_A"))
    assert "not an integer" in capsys.readouterr().out


def test_a_cancel_leaves_no_layers(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def hook(w):
        w.progress.connect(lambda *a: w.stop_after_current_channel() if a[2] == 2 else None)
    got = _save(tmp_path, out, monkeypatch=monkeypatch, hook=hook)
    assert got["canceled"]
    assert not os.path.isdir(dp.display_root(out / "corrected_channels.zarr"))


def test_a_second_save_retires_the_first_layers(tmp_path, monkeypatch):
    out = tmp_path / "out"
    assert _save(tmp_path, out, seed=1, monkeypatch=monkeypatch)["finished"]
    old = dict(_product(out).attrs)
    root = dp.display_root(out / "corrected_channels.zarr")
    first = dp.accept(root, "R/CH_A", old, ROI, 2)[...]
    assert _save(tmp_path, out, seed=2, monkeypatch=monkeypatch)["finished"]
    new = dict(_product(out).attrs)
    assert dp.accept(root, "R/CH_A", old, ROI, 2) is None       # the old token no longer matches
    layer = dp.accept(root, "R/CH_A", new, ROI, 2)[...]
    assert np.array_equal(layer, _rounded_reduction(_product(out), 2))
    assert not np.array_equal(layer, first)


def _table(out):
    return sources.Step1SourceTable(decisions={"CH_A": "tophat"},
                                    corrected_zarr_path=str(out / "corrected_channels.zarr"),
                                    roi_name="R", roi_bbox=ROI)


def test_step1_prefers_an_accepted_layer_and_falls_back(tmp_path, monkeypatch):
    out = tmp_path / "out"
    assert _save(tmp_path, out, monkeypatch=monkeypatch)["finished"]
    plane = _table(out).coarse_plane("CH_A", 2)
    assert plane is not None and plane.display
    values, valid = plane.tile((50, 60, 30, 45))
    assert valid.all() and np.array_equal(values, plane.array[50 - plane.origin[0]:60 - plane.origin[0],
                                                              30 - plane.origin[1]:45 - plane.origin[1]])
    monkeypatch.setenv("BLOCK01_STEP1_DISPLAY_LAYERS", "0")
    assert _table(out).coarse_plane("CH_A", 2) is None             # no float plane at stride 2
    plane4 = _table(out).coarse_plane("CH_A", 4)
    assert plane4 is not None and not plane4.display               # the float coarse plane
    monkeypatch.delenv("BLOCK01_STEP1_DISPLAY_LAYERS")
    layer = zarr.open_array(os.path.join(dp.display_root(out / "corrected_channels.zarr"),
                                         "R", "CH_A", "s2"), mode="r+")
    for tweak in ("incomplete", "stale", "dtype"):
        saved = dict(layer.attrs)
        if tweak == "incomplete":
            del layer.attrs["complete"]
        elif tweak == "stale":
            layer.attrs["source_identity"] = "an-earlier-write"
        else:
            layer.attrs["dtype"] = "<f4"
        assert _table(out).coarse_plane("CH_A", 2) is None, tweak
        layer.attrs.put(saved)
    assert _table(out).coarse_plane("CH_A", 2).display


def test_python_and_rust_tiles_are_equal_on_layers(tmp_path, monkeypatch):
    core = pytest.importorskip("fusion_viewer_core")
    from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest
    from block01.viewer.raw_tile_provider import RawTileProvider
    out = tmp_path / "out"
    assert _save(tmp_path, out, monkeypatch=monkeypatch)["finished"]
    table = _table(out)
    raw = RawTileProvider(str(tmp_path / "slide_1.ome.tif"))
    try:
        manifest = build_corrected_manifest(table, raw, [1, 2, 4])
        planes = manifest["channels"]["CH_A"]["planes"]
        assert {p["stride"]: p["display"] for p in planes} == {1: True, 2: True, 4: True}
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        for stride in (1, 2, 4):
            h, w = 1024 // stride, 768 // stride
            for y0 in range(0, h, 128):
                for x0 in range(0, w, 128):
                    rect = (y0, min(h, y0 + 128), x0, min(w, x0 + 128))
                    want, valid = sources.read_tile(table, "CH_A", rect, stride=stride)
                    t = reader.read_tile("CH_A", 1, *rect, stride, 128)
                    # every stride has a display layer: uint16 tiles (R7c 1E)
                    assert t["dtype"] == "uint16"
                    got = np.frombuffer(t["data"], "<u2").reshape(t["rows"], t["cols"]).astype(np.float32)
                    mask = np.zeros(got.shape, bool)
                    if t["valid"] == "rect":
                        ry0, ry1, rx0, rx1 = t["rect"]
                        mask[ry0:ry1, rx0:rx1] = True
                    assert np.array_equal(mask, valid)
                    assert np.array_equal(got[valid], want[valid]), (stride, rect)
    finally:
        raw.close()


def test_a_gpu_failure_part_way_ends_the_first_builder(tmp_path, monkeypatch):
    import threading
    from block01.core import bg_correction as bg
    calls = []

    def fake_correct(raw, method, param, path, impl=None):
        calls.append(path)
        if path == "gpu" and calls.count("gpu") == 3:            # the 3rd GPU tile fails
            raise bg.GpuBackendFailed("simulated")
        return bg.correct_tile(raw, method, param, "cpu")
    monkeypatch.setattr(search_ctrl, "compute_path", lambda method: "gpu")
    monkeypatch.setattr(search_ctrl, "correct_tile", fake_correct)
    out = tmp_path / "out"
    assert _save(tmp_path, out, monkeypatch=monkeypatch)["finished"]
    assert "gpu" in calls and calls[-1] == "cpu"
    assert not [t for t in threading.enumerate() if t.name.startswith("display ")]
    product = _product(out)
    layer = dp.accept(dp.display_root(out / "corrected_channels.zarr"), "R/CH_A",
                      dict(product.attrs), ROI, 2)
    assert np.array_equal(layer[...], _rounded_reduction(product, 2))


SIDE = 4096


def _square_project(tmp_path):
    """One corrected channel covering a 4096 square (integers)."""
    data = np.random.default_rng(3).integers(0, 255, (SIDE, SIDE)).astype(np.float32)
    product = zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="w").require_group(
        "R").create_dataset("c", shape=(SIDE, SIDE), chunks=(1024, 1024), dtype=np.float32)
    product[:, :] = data
    product.attrs.update({"channel_name": "c", "correction_method": "tophat", "roi_name": "R",
                          "roi_bbox_fullres": [0, SIDE, 0, SIDE], "source_shape": [SIDE, SIDE],
                          "source_identity": "tok"})


@pytest.mark.parametrize("where", ["product", "inside", "beside", "parent"])
def test_the_backfill_refuses_an_unsafe_output(tmp_path, where):
    _square_project(tmp_path)
    product = tmp_path / "corrected_channels.zarr"
    out = {"product": product, "inside": product / "R", "parent": tmp_path,
           "beside": tmp_path / dp.DISPLAY_DIRNAME}[where]
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "build_corrected_display.py")
    before = _digest(product)
    done = subprocess.run([sys.executable, script, str(product), "--strides", "2", "--out", str(out)],
                          capture_output=True, text=True)
    assert done.returncode != 0 and "refusing --out" in done.stderr
    assert _digest(product) == before


def _digest(folder):
    import hashlib
    return {str(p.relative_to(folder)): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_the_backfill_writes_to_a_relative_output_outside_the_project(tmp_path):
    _square_project(tmp_path)
    product = tmp_path / "corrected_channels.zarr"
    elsewhere = tmp_path.parent / f"{tmp_path.name}_elsewhere"
    elsewhere.mkdir()
    before = _digest(product)
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts",
                                          "build_corrected_display.py"))
    done = subprocess.run([sys.executable, script, str(product), "--strides", "2,4",
                           "--out", "new_display", "--max-bytes", str(256 << 20)],
                          capture_output=True, text=True, cwd=str(elsewhere))
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    attrs = dict(zarr.open_group(str(product), mode="r")["R"]["c"].attrs)
    assert dp.accept(str(elsewhere / "new_display"), "R/c", attrs, [0, SIDE, 0, SIDE], 4) is not None
    assert _digest(product) == before


def test_the_backfill_never_writes_the_product(tmp_path):
    _square_project(tmp_path)
    product = tmp_path / "corrected_channels.zarr"
    before = {p: os.stat(os.path.join(d, p)).st_mtime_ns
              for d, _s, fs in os.walk(product) for p in fs}
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "build_corrected_display.py")
    refused = subprocess.run([sys.executable, script, str(product), "--strides", "2,4,8"],
                             capture_output=True, text=True)
    assert refused.returncode != 0 and "--in-place" in refused.stderr
    done = subprocess.run([sys.executable, script, str(product), "--strides", "2,4,8", "--in-place",
                           "--max-bytes", str(256 << 20)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    after = {p: os.stat(os.path.join(d, p)).st_mtime_ns
             for d, _s, fs in os.walk(product) for p in fs}
    assert before == after
    arr = zarr.open_group(str(product), mode="r")["R"]["c"]
    layer = dp.accept(dp.display_root(product), "R/c", dict(arr.attrs), [0, SIDE, 0, SIDE], 4)
    want = arr[...].reshape(SIDE // 4, 4, SIDE // 4, 4).mean(axis=(1, 3))
    assert np.array_equal(layer[...], np.floor(want + 0.5).astype(np.uint16))


def test_the_full_resolution_layer_can_be_switched_off(tmp_path, monkeypatch):
    out = tmp_path / "out"
    assert _save(tmp_path, out, monkeypatch=monkeypatch)["finished"]
    assert _table(out).coarse_plane("CH_A", 1).display
    monkeypatch.setenv("BLOCK01_STEP1_DISPLAY_S1", "0")
    assert _table(out).coarse_plane("CH_A", 1) is None        # level 0 is read as before
    assert _table(out).coarse_plane("CH_A", 2).display        # the coarser layers stay


def test_s1_that_differs_from_the_product_is_not_published(tmp_path):
    _square_project(tmp_path)
    product = zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="r")["R"]["c"]
    values = product[...]
    root = tmp_path / "display"
    b = dp.DisplayBuilder(str(root), "R", "c", [0, SIDE, 0, SIDE], [2])
    changed = values.copy()
    changed[0, 0] += 1                                       # what s1 holds is not the product
    b.add(changed, 0, 0)
    assert not b.finish(dict(product.attrs), product)
    assert "differs" in b.refused
    assert not (root / "R" / "c").exists()
    b = dp.DisplayBuilder(str(root), "R", "c", [0, SIDE, 0, SIDE], [2])
    b.add(values, 0, 0)
    assert b.finish(dict(product.attrs), product)
    assert dp.accept(str(root), "R/c", dict(product.attrs), [0, SIDE, 0, SIDE], 1) is not None


def _thin_product(tmp_path, fill=0.0, width=5120):
    """One row of 512 chunks across (1 x 10 s1 chunks): only the long axis
    has 1024 source-chunk boundaries."""
    product = zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="w").require_group(
        "R").create_dataset("t", shape=(512, width), chunks=(1024, 1024), dtype=np.float32,
                            fill_value=fill)
    values = np.random.default_rng(9).integers(0, 255, (512, width)).astype(np.float32)
    product[:, :1024] = values[:, :1024]             # the rest is never written: fill
    product.attrs.update({"source_identity": "tok", "roi_bbox_fullres": [0, 512, 0, width]})
    values[:, 1024:] = fill
    return product, values


def test_s1_keeps_the_product_fill(tmp_path):
    product, values = _thin_product(tmp_path, fill=7.0)
    root = tmp_path / "display"
    b = dp.DisplayBuilder(str(root), "R", "t", [0, 512, 0, 5120], [2], fill=float(product.fill_value))
    b.add(np.asarray(product[...]), 0, 0)
    assert b.finish(dict(product.attrs), product), b.refused
    s1 = dp.accept(str(root), "R/t", dict(product.attrs), [0, 512, 0, 5120], 1)
    assert s1.fill_value == 7 and np.array_equal(s1[...].astype(np.float32), values)
    bad = dp.DisplayBuilder(str(tmp_path / "d2"), "R", "t", [0, 512, 0, 5120], [2], fill=float("nan"))
    assert "fill" in bad.refused


def test_the_spot_check_covers_a_single_axis_boundary(tmp_path):
    product, values = _thin_product(tmp_path)
    changed = values.copy()
    changed[10, 1024 + 3] += 1                      # s1 chunk (0, 2): right of the 1024 boundary
    b = dp.DisplayBuilder(str(tmp_path / "display"), "R", "t", [0, 512, 0, 5120], [2])
    b.add(changed, 0, 0)
    assert not b.finish(dict(product.attrs), product)
    assert "0.2 differs" in b.refused


def test_a_spot_check_that_cannot_read_drops_only_the_layers(tmp_path, monkeypatch):
    product, values = _thin_product(tmp_path)
    root = tmp_path / "display"
    b = dp.DisplayBuilder(str(root), "R", "t", [0, 512, 0, 5120], [2])
    b.add(values, 0, 0)

    def broken(self, product):
        raise OSError("disk")
    monkeypatch.setattr(dp.DisplayBuilder, "_spot_check", broken)
    assert not b.finish(dict(product.attrs), product)
    assert "could not read" in b.refused and not (root / "R" / "t").exists()
