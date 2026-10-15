"""v16_5 R7c 1B (docs/v16_5_r7c_odon_alignment_plan.md §2 1B): a corrected
channel drawn from an already-built tiled pyramid (the merged OME-TIFF),
behind the measurement switch `BLOCK01_STEP1_DISPLAY_OMETIFF`.

Synthetic project under tmp_path: a 3-level uint8 slide (channels a, b), a
float product for b over the whole slide (integers), and a merged uint8
OME-TIFF holding a (raw) and b (its product), levels reduced 2x2.

* With the switch, b is read from the merged file: level 0 equals the
  product, coarser levels equal the file's own levels; the raw channel a
  is read from the same file (R7c 1F).
* Without it, or when the region is not the whole slide, the file lacks the
  channel, or its levels are not the slide's, b stays on its product.
* A core with this manifest reads b's tiles without errors.
"""

import time

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

H, W = 2048, 1536


def _pyramid(path, data, names, levels=3):
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=levels - 1, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": list(names)}})
        cur = data
        for _ in range(levels - 1):
            h, w = cur.shape[1] // 2 * 2, cur.shape[2] // 2 * 2
            s = (cur[:, :h:2, :w:2].astype(np.uint16) + cur[:, 1:h:2, :w:2]
                 + cur[:, :h:2, 1:w:2] + cur[:, 1:h:2, 1:w:2])
            cur = ((s + 2) // 4).astype(np.uint8)
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")


def _project(tmp_path, bbox=(0, H, 0, W), merged_names=("a", "b"), merged_levels=3):
    rng = np.random.default_rng(4)
    raw = rng.integers(0, 255, (2, H, W), dtype=np.uint8)
    slide = tmp_path / "slide.ome.tif"
    _pyramid(slide, raw, ["a", "b"])
    y0, y1, x0, x1 = bbox
    product = rng.integers(0, 200, (y1 - y0, x1 - x0)).astype(np.float32)
    arr = zarr.open_group(str(tmp_path / "corrected_channels.zarr"), mode="w").require_group(
        "R").create_dataset("b", shape=product.shape, chunks=(1024, 1024), dtype=np.float32)
    arr[:, :] = product
    arr.attrs.update({"channel_name": "b", "correction_method": "tophat", "roi_name": "R",
                      "roi_bbox_fullres": list(bbox), "source_shape": list(product.shape),
                      "source_identity": "tok"})
    merged = np.zeros((len(merged_names), H, W), np.uint8)
    for i, n in enumerate(merged_names):
        if n == "b":
            merged[i, y0:y1, x0:x1] = product.astype(np.uint8)
        else:
            merged[i] = raw[0]
    mpath = tmp_path / "merged.ome.tif"
    _pyramid(mpath, merged, merged_names, merged_levels)
    table = sources.Step1SourceTable(decisions={"a": "original", "b": "tophat"},
                                     corrected_zarr_path=str(tmp_path / "corrected_channels.zarr"),
                                     roi_name="R", roi_bbox=list(bbox))
    return str(slide), str(mpath), table, product, merged


def test_the_switch_draws_b_from_the_merged_file(tmp_path, monkeypatch):
    slide, merged_path, table, product, merged = _project(tmp_path)
    raw = RawTileProvider(slide)
    try:
        assert build_corrected_manifest(table, raw, [1, 2, 4])["channels"]["b"]["source"] == "corrected"
        monkeypatch.setenv("BLOCK01_STEP1_DISPLAY_OMETIFF", merged_path)
        manifest = build_corrected_manifest(table, raw, [1, 2, 4])
        assert manifest["channels"]["b"] == {"source": "display_tiff", "index": 1, "tiff_index": 1}
        # R7c 1F: the raw channel too, so every channel comes from the one file
        assert manifest["channels"]["a"] == {"source": "display_tiff", "index": 0, "tiff_index": 0}
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        mtiff = tifffile.TiffFile(merged_path)
        for level, stride in ((0, 1), (1, 2), (2, 4)):
            lvl = np.asarray(mtiff.series[0].levels[level].asarray())[1]
            for y0 in (0, 512):
                for x0 in (0, 512):
                    t = reader.read_tile("b", level, y0, y0 + 512, x0, x0 + 512, stride, 512)
                    assert t["dtype"] == "uint8"
                    if t["valid"] == "nothing":              # wholly outside this level
                        assert y0 >= lvl.shape[0] or x0 >= lvl.shape[1]
                        continue
                    assert t["valid"] == "rect"
                    got = np.frombuffer(t["data"], np.uint8).reshape(t["rows"], t["cols"])
                    ry0, ry1, rx0, rx1 = t["rect"]                # inside the level
                    got = got[ry0:ry1, rx0:rx1]
                    assert np.array_equal(got, lvl[y0:y0 + 512, x0:x0 + 512]), (level, y0, x0)
                    if level == 0:
                        assert np.array_equal(got, product[y0:y0 + 512, x0:x0 + 512].astype(np.uint8))
        mtiff.close()
    finally:
        raw.close()


@pytest.mark.parametrize("case", ["partial-region", "no-channel", "other-levels"])
def test_b_stays_on_its_product_otherwise(tmp_path, monkeypatch, case):
    kw = {"partial-region": {"bbox": (0, H, 0, W - 512)}, "no-channel": {"merged_names": ("a", "c")},
          "other-levels": {"merged_levels": 2}}[case]
    slide, merged_path, table, _p, _m = _project(tmp_path, **kw)
    monkeypatch.setenv("BLOCK01_STEP1_DISPLAY_OMETIFF", merged_path)
    raw = RawTileProvider(slide)
    try:
        manifest = build_corrected_manifest(table, raw, [1, 2, 4])
        assert manifest["channels"]["b"]["source"] == "corrected"
        if case != "partial-region":
            # the raw channel a still comes from the file when it names it and the region is whole
            assert manifest["channels"]["a"]["source"] == ("display_tiff" if case == "no-channel" else "raw")
        else:
            assert manifest["channels"]["a"]["source"] == "raw"
    finally:
        raw.close()


def test_a_core_reads_b_from_the_merged_file(tmp_path, monkeypatch):
    from block01.ui.step1_core_binding import core_config
    from block01.ui.step1_viewer_mount import demo_budgets
    slide, merged_path, table, _p, _m = _project(tmp_path)
    monkeypatch.setenv("BLOCK01_STEP1_DISPLAY_OMETIFF", merged_path)
    raw = RawTileProvider(slide)
    c = core.ViewerCore(build_raw_manifest(raw), dict(core_config(demo_budgets()), threads=2),
                        trace=True, sources=build_corrected_manifest(table, raw, [1, 2, 4]))
    try:
        c.set_display([(1, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0)], False)
        c.set_view(0, [0, 0, 1024, 1024], 1, False)
        end = time.monotonic() + 30
        while time.monotonic() < end and not c.idle():
            c.paint(0, 1, 1, [0.0, 1024.0, 0.0, 1024.0])
            time.sleep(0.005)
        st = c.stats()
        assert st["reads"] > 0 and st["read_errors"] == 0, st
        assert any(r[1] == "read" and r[2][1] == 0 for r in c.drain_trace())
    finally:
        c.close()
        raw.close()
