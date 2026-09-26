"""Block N: Step2 writes a label pyramid of each mask on the slide's grids.

  * the pure builder: every level pixel equals the mask at the level-0 slide
    pixel under its centre, computed here from the VIEWER's own mapping
    (`RawTileProvider.level_downsample_yx` on the same synthetic OME-TIFF),
    for odd sizes, non-integer and per-axis different ratios, a ROI whose
    origin is not a multiple and a ROI on the slide's edge; each level covers
    the ROI's footprint on that level's grid; zero and empty masks;
  * completeness: a cancel leaves neither the pyramid nor its partial
    directory; `read` refuses a pyramid without `complete` or whose level-0
    mask changed or went away;
  * Step2: every ROI's (and the whole-image) pyramid path is in the ROI meta,
    the outer ROI records and the summary, and opens with `read`; a Stop in
    the second ROI keeps the first ROI's pyramid, leaves none for the second
    and registers nothing; an ordinary failure of the pyramid is reported and
    the segmentation still stands.

Synthetic projects in the test's temporary directory only.
"""

import json
import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.core import label_pyramid as lp  # noqa: E402

SHAPES = [(401, 523), (101, 131), (26, 33)]      # ratios 3.970 / 3.992, 15.42 / 15.85


def _slide(tmp_path, shapes=SHAPES):
    import tifffile
    path = str(tmp_path / "slide.ome.tif")
    with tifffile.TiffWriter(path, ome=True) as tw:
        tw.write(np.zeros((2,) + shapes[0], np.uint8), subifds=len(shapes) - 1,
                 tile=(64, 64), metadata={"axes": "CYX"})
        for s in shapes[1:]:
            tw.write(np.zeros((2,) + s, np.uint8), subfiletype=1, tile=(64, 64))
    return path


def _mask(tmp_path, shape, seed=0, name="mask.zarr"):
    import zarr
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 5000, size=shape, dtype=np.uint32)
    z = zarr.open(str(tmp_path / name), mode="w", shape=shape, chunks=(64, 64), dtype="<u4")
    z[:] = arr
    return str(tmp_path / name), arr


def _viewer_expected(slide, arr, bbox, level):
    """The level as the viewer places it: pixel (i, j) of level L covers
    [i*ds_y, (i+1)*ds_y) x [j*ds_x, (j+1)*ds_x) of level 0 (per-axis,
    unrounded); its centre picks the mask pixel."""
    from block01.viewer.raw_tile_provider import RawTileProvider
    rp = RawTileProvider(slide)
    ds_y, ds_x = rp.level_downsample_yx(level)
    hl, wl = rp.level_shape(level)
    y0, y1, x0, x1 = bbox
    i0, i1 = int(y0 // ds_y), min(hl, math.ceil(y1 / ds_y))
    j0, j1 = int(x0 // ds_x), min(wl, math.ceil(x1 / ds_x))
    out = np.zeros((i1 - i0, j1 - j0), np.uint32)
    for i in range(i0, i1):
        yc = int((i + 0.5) * ds_y)
        if not (y0 <= yc < y1):
            continue
        for j in range(j0, j1):
            xc = int((j + 0.5) * ds_x)
            if x0 <= xc < x1:
                out[i - i0, j - j0] = arr[yc - y0, xc - x0]
    return out, (i0, j0)


@pytest.mark.parametrize("bbox", [
    (37, 37 + 176, 101, 101 + 208),          # origin not a multiple of the ratio
    (0, 401, 0, 523),                        # the whole slide
    (250, 401, 390, 523),                    # on the bottom-right edge, odd sizes
    (3, 4, 5, 9),                            # smaller than one coarse pixel
])
def test_every_level_matches_the_viewers_grid(tmp_path, bbox):
    import zarr
    slide = _slide(tmp_path)
    src, arr = _mask(tmp_path, (bbox[1] - bbox[0], bbox[3] - bbox[2]))
    out = lp.build(src, str(tmp_path / "pyr.zarr"), lp.raw_level_shapes(slide), bbox, "cell")
    meta = lp.read(out)
    assert meta is not None and meta["kind"] == "cell" and meta["roi_bbox"] == list(bbox)
    assert [lv["level"] for lv in meta["levels"]] == [1, 2]
    for lv in meta["levels"]:
        want, origin = _viewer_expected(slide, arr, bbox, lv["level"])
        got = np.asarray(zarr.open(out, mode="r")[lv["path"]])
        assert list(origin) == lv["origin"] and list(want.shape) == lv["shape"]
        np.testing.assert_array_equal(got, want)
    assert not os.path.exists(out + ".partial")


def test_zero_and_empty_masks(tmp_path):
    import zarr
    slide = _slide(tmp_path)
    bbox = (10, 90, 20, 140)
    z = zarr.open(str(tmp_path / "zero.zarr"), mode="w", shape=(80, 120), chunks=(64, 64),
                  dtype="<u4")
    z[:] = 0
    out = lp.build(str(tmp_path / "zero.zarr"), str(tmp_path / "p0.zarr"),
                   lp.raw_level_shapes(slide), bbox, "cell")
    for lv in lp.read(out)["levels"]:
        assert not np.asarray(zarr.open(out, mode="r")[lv["path"]]).any()
    with pytest.raises(ValueError, match="bbox"):
        lp.build(str(tmp_path / "zero.zarr"), str(tmp_path / "p1.zarr"),
                 lp.raw_level_shapes(slide), (0, 81, 0, 120), "cell")
    assert not os.path.exists(str(tmp_path / "p1.zarr.partial"))


def test_a_cancel_leaves_nothing(tmp_path):
    slide = _slide(tmp_path, [(3000, 2500), (750, 625), (188, 157)])
    src, _ = _mask(tmp_path, (3000, 2500))
    calls = []
    out = str(tmp_path / "pyr.zarr")
    with pytest.raises(lp.Cancelled):
        lp.build(src, out, lp.raw_level_shapes(slide), (0, 3000, 0, 2500), "cell",
                 cancel_check=lambda: calls.append(1) or len(calls) > 1)
    assert not os.path.exists(out) and not os.path.exists(out + ".partial")


def test_read_accepts_only_a_whole_pyramid_of_the_same_mask(tmp_path):
    import zarr
    slide = _slide(tmp_path)
    bbox = (37, 37 + 176, 101, 101 + 208)
    src, _ = _mask(tmp_path, (176, 208))
    out = lp.build(src, str(tmp_path / "pyr.zarr"), lp.raw_level_shapes(slide), bbox, "cell")
    assert lp.read(out)["level0_abs"] == os.path.abspath(src)
    g = zarr.open_group(out, mode="r+")
    g.attrs["complete"] = False
    assert lp.read(out) is None
    g.attrs["complete"] = True
    assert lp.read(out) is not None
    zarr.open(src, mode="w", shape=(170, 208), chunks=(64, 64), dtype="<u4")   # mask changed
    assert lp.read(out) is None
    import shutil
    shutil.rmtree(src)                                                          # mask gone
    assert lp.read(out) is None
    assert lp.read(str(tmp_path / "nothing.zarr")) is None


# ── Step2 ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PyQt5")
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


BBOX = [37, 37 + 176, 101, 101 + 208]            # the synthetic image of the runner tests


def _roi_project(tmp_path):
    """A ROI workspace on a synthetic slide pyramid; step1/fused.zarr is the
    ROI's crop (the runner tests' synthetic image)."""
    import test_step2_runner_path as rp
    slide = _slide(tmp_path)
    d = tmp_path / "rois" / "roi_n"
    (d / "step1").mkdir(parents=True)
    (d / "step2").mkdir()
    with open(d / "roi_manifest.json", "w", encoding="utf-8") as f:
        json.dump({"roi_id": "roi_n", "display_name": "A", "source_ome": slide,
                   "bbox_fullres": BBOX}, f)
    import zarr
    img = rp._image()
    z = zarr.open(str(d / "step1" / "fused.zarr"), mode="w", shape=img.shape,
                  chunks=(64, 64, 2), dtype=np.uint16)
    z[:] = img
    return d


def _worker(d, rois):
    import test_step2_runner_path as rp
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    return SegmentMergeWorker(str(d / "step1" / "fused.zarr"),
                              seg_config=dict(rp.PARAMS["stardist_nuclei_dapi"],
                                              method="stardist_nuclei_dapi"),
                              n_rows=2, n_cols=2, overlap_px=32,
                              output_dir=str(d / "step2"), rois=rois)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _stardist():
    import test_step2_runner_path as rp
    if rp._engine_missing("stardist"):
        pytest.skip("no StarDist")
    return rp


def test_each_roi_pyramid_is_recorded_everywhere_and_opens(app, tmp_path):
    rp = _stardist()
    d = _roi_project(tmp_path)
    worker = _worker(d, [{"name": "A", "bbox_fullres": BBOX}, {"name": "B", "bbox_fullres": BBOX}])
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    summary = _read_json(os.path.join(worker.output_dir, "segmentation_meta.json"))
    for name in ("A", "B"):
        inner = _read_json(os.path.join(worker.output_dir, f"segmentation_meta_{name}.json"))
        outer = [r for r in summary["rois"] if r["roi_name"] == name][0]
        for record in (inner["label_pyramid"], outer["label_pyramid"],
                       summary["label_pyramid"][name]):
            assert record["nucleus"] is None
            meta = lp.read(record["cell"])
            assert meta is not None and meta["roi_bbox"] == BBOX
            assert meta["level0_abs"] == os.path.abspath(
                os.path.join(worker.output_dir, f"global_mask_{name}.zarr"))


def test_the_whole_image_run_records_its_pyramid(app, tmp_path):
    rp = _stardist()
    d = _roi_project(tmp_path)
    worker = _worker(d, None)
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    meta = _read_json(os.path.join(worker.output_dir, "segmentation_meta.json"))
    pyr = lp.read(meta["label_pyramid"]["cell"])
    assert pyr is not None and pyr["roi_bbox"] == BBOX


def test_a_stop_in_the_second_roi_keeps_the_first_pyramid_only(app, tmp_path):
    rp = _stardist()
    from PyQt5 import QtCore
    d = _roi_project(tmp_path)
    worker = _worker(d, [{"name": "A", "bbox_fullres": BBOX}, {"name": "B", "bbox_fullres": BBOX}])
    got = rp._collect(worker)
    worker.progress.connect(lambda a, b, m: worker.stop() if m.startswith("[B] Tile [2/") else None,
                            QtCore.Qt.DirectConnection)
    worker.run()
    assert got["finished"] == [] and got["error"] == ["Stopped by user."]
    assert not rp._registered(worker)
    a = os.path.join(worker.output_dir, "label_pyramid_A.zarr")
    b = os.path.join(worker.output_dir, "label_pyramid_B.zarr")
    assert lp.read(a) is not None
    assert not os.path.exists(b) and not os.path.exists(b + ".partial")


def test_a_failed_pyramid_is_reported_and_the_segmentation_stands(app, tmp_path, monkeypatch,
                                                                   capsys):
    rp = _stardist()
    d = _roi_project(tmp_path)

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(lp, "build", disk_full)                 # an ordinary I/O failure
    worker = _worker(d, [{"name": "A", "bbox_fullres": BBOX}])
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1 and rp._registered(worker)
    summary = _read_json(os.path.join(worker.output_dir, "segmentation_meta.json"))
    assert summary["label_pyramid"]["A"] == {"cell": None, "nucleus": None}
    assert "label pyramid of global_mask_A.zarr failed" in capsys.readouterr().out
    assert os.path.exists(os.path.join(worker.output_dir, "global_mask_A.zarr"))


def test_a_nucleus_mask_gets_its_own_pyramid(app, tmp_path):
    import zarr
    d = _roi_project(tmp_path)
    worker = _worker(d, [{"name": "A", "bbox_fullres": BBOX}])
    cell, _ = _mask(tmp_path, (176, 208), name="global_mask_A.zarr")
    nuc, _ = _mask(tmp_path, (176, 208), seed=1, name="global_nuclei_mask_A.zarr")
    out = worker._write_label_pyramids(cell, nuc, BBOX, 176, 208)
    assert os.path.basename(out["cell"]) == "label_pyramid_A.zarr"
    assert os.path.basename(out["nucleus"]) == "label_pyramid_nuclei_A.zarr"
    assert lp.read(out["nucleus"])["kind"] == "nucleus"
    assert zarr.open(out["nucleus"], mode="r")["1"].shape == tuple(
        lp.read(out["nucleus"])["levels"][0]["shape"])
