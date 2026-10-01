"""Step2: opt-in skipping of tiles without tissue (block S2T, segment 2;
docs/v16_Step2_tiles_application.md gates S1-S10, O1).

The plan is made on the page from the raw slide's ~16x tissue mask
(`core/tile_tissue.py`) and confirmed by the user; the worker executes the
final list. With no plan nothing new runs; with a plan whose tiles really
are empty the outputs equal a run without it. The model is the fixed
labelling of `test_step2_ownership_move` (no engine).
"""

import json
import logging
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
from PyQt5 import QtWidgets  # noqa: E402

from block01.core import tile_tissue as tt  # noqa: E402
from block01.utils.tile_scheduler import TileScheduler  # noqa: E402
from test_step2_ownership_move import _fake_result, _labels  # noqa: E402

METHOD = "cellpose_wholecell_fusion"
SH, SW = 2048, 3072              # synthetic slide, level 0
TISSUE_X1 = 1400                 # tissue in columns [0, 1400)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── a workspace with a slide that is half tissue, half glass ────────────

def _slide(path):
    rng = np.random.default_rng(0)
    dapi = rng.integers(0, 8, (SH, SW)).astype(np.uint8)
    dapi[:, :TISSUE_X1] = rng.integers(110, 140, (SH, TISSUE_X1)).astype(np.uint8)
    data = np.stack([dapi, dapi // 2])
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(256, 256),
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3"]}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::4, ::4])
            tw.write(cur, subfiletype=1, tile=(256, 256))
    return str(path)


def _workspace(tmp_path, bbox):
    """<proj>/rois/ws/{roi_manifest.json, step1/{settings, fused zarr}}."""
    slide = _slide(tmp_path / "slide.ome.tif")
    ws = tmp_path / "proj" / "rois" / "ws"
    (ws / "step1").mkdir(parents=True)
    json.dump({"roi_id": "ws", "source_ome": slide, "bbox_fullres": list(bbox)},
              open(ws / "roi_manifest.json", "w"))
    json.dump({"fusion_config": {"nucleus": {"channel": "DAPI", "weight": 1.0}}},
              open(ws / "step1" / "step1_fusion_settings.json", "w"))
    y0, y1, x0, x1 = bbox
    zp = str(ws / "step1" / "fused_Full WSI.zarr")
    z = zarr.open(zp, mode="w", shape=(y1 - y0, x1 - x0, 2), chunks=(256, 256, 2),
                  dtype=np.uint16)
    z[...] = 1
    z.attrs["bbox_fullres"] = list(bbox)
    return slide, zp


# ── the plan (core/tile_tissue.py): S3, S8 ──────────────────────────────

def _expected_auto(bbox, rows, cols, overlap, margin):
    """Tiles whose global read window starts clearly right of the tissue."""
    y0, y1, x0, x1 = bbox
    out = []
    for k, t in enumerate(TileScheduler(y1 - y0, x1 - x0, rows, cols, overlap).tiles):
        if t.read_bbox[2] + x0 >= TISSUE_X1 + margin:
            out.append(k)
    return out


def test_only_tiles_without_tissue_are_proposed(tmp_path):
    bbox = (0, SH, 0, SW)
    slide, zp = _workspace(tmp_path, bbox)
    got_bbox, got_slide, ch = tt.fused_region(zp)
    assert got_bbox == bbox and got_slide == slide and ch == "DAPI"
    tissue = tt.SlideTissue(slide, ch)
    assert tissue.level == 2                               # the ~16x level
    plan = tt.tile_plan(tissue, (SH, SW), (0, 0), 2, 6, 100)
    # tiles fully right of the tissue (plus the mask's smoothing margin) skip;
    # every tile touching the tissue is kept
    assert set(_expected_auto(bbox, 2, 6, 100, 300)) <= set(plan["auto"])
    for k in plan["auto"]:
        assert plan["fractions"][k] == 0.0
    assert all(plan["fractions"][k] > 0 for k in range(12) if k not in plan["auto"])
    assert plan["grid"] == [2, 6] and plan["overlap"] == 100


def test_a_tile_with_tissue_only_in_its_halo_is_kept(tmp_path):
    """S3: the READ window decides, not the own region."""
    bbox = (0, SH, 0, SW)
    slide, zp = _workspace(tmp_path, bbox)
    tissue = tt.SlideTissue(slide, "DAPI")
    # cols chosen so a tile's own region starts just right of the tissue
    # while its halo reaches back into it
    for cols in range(4, 12):
        for k, t in enumerate(TileScheduler(SH, SW, 1, cols, 400).tiles):
            own_x0, read_x0 = t.own_bbox[2], t.read_bbox[2]
            if TISSUE_X1 + 50 < own_x0 and read_x0 < TISSUE_X1 - 50:
                plan = tt.tile_plan(tissue, (SH, SW), (0, 0), 1, cols, 400)
                assert k not in plan["auto"] and plan["fractions"][k] > 0
                return
    pytest.fail("no halo-only tile in the tried grids")


def test_a_roi_off_the_origin_maps_its_tiles_onto_the_slide(tmp_path):
    """S8: ROI origin + non-integer level ratios (2048 / 128 = 16, 3072 / 192 = 16 here;
    the ROI's own origin is what matters)."""
    bbox = (300, 1900, 900, 3072)                     # starts left of the tissue edge
    slide, zp = _workspace(tmp_path, bbox)
    tissue = tt.SlideTissue(slide, "DAPI")
    y0, y1, x0, x1 = bbox
    plan = tt.tile_plan(tissue, (y1 - y0, x1 - x0), (y0, x0), 1, 5, 50)
    assert set(_expected_auto(bbox, 1, 5, 50, 300)) <= set(plan["auto"])
    assert 0 not in plan["auto"]                       # the ROI's first tile holds tissue
    wrong = tt.tile_plan(tissue, (y1 - y0, x1 - x0), (0, 0), 1, 5, 50)
    assert wrong["auto"] != plan["auto"]              # the origin matters


def test_a_fused_input_without_its_place_is_unavailable(tmp_path):
    _slide, zp = _workspace(tmp_path, (0, SH, 0, SW))
    zarr.open(zp, mode="r+").attrs.pop("bbox_fullres")
    with pytest.raises(tt.TissueUnavailable):
        tt.fused_region(zp)


# ── the worker: S1, S2, S4, S5, S10 ─────────────────────────────────────

def _worker(tmp_path, monkeypatch, lab, rows, cols, overlap, skip=None, calls=None, name="run"):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    zp = str(tmp_path / f"{name}_fused.zarr")
    z = zarr.open(zp, mode="w", shape=lab.shape + (2,), chunks=(32, 32, 2), dtype=np.uint16)
    z[..., 0] = lab                       # the fake model reads channel 0
    z[..., 1] = np.random.default_rng(1).integers(1, 4000, lab.shape)   # DAPI: never 0

    def seg(self, tile_data, *a, **k):
        if calls is not None:
            calls.append(1)
        return _fake_result(self.method, tile_data)
    monkeypatch.setattr(W, "_segment_tile", seg)
    monkeypatch.setattr(W, "_init_segmentation_backend", lambda self, *a, **k: None)
    monkeypatch.setattr(W, "_uses_torch_backend", lambda self: False)
    cfg = {"method": METHOD}
    if skip is not None:
        cfg["skip_tiles"] = skip
    worker = W(zp, seg_config=cfg, n_rows=rows, n_cols=cols, overlap_px=overlap,
               output_dir=str(tmp_path / name))
    return worker, zp


def _empty_tiles(lab, rows, cols, overlap):
    h, w = lab.shape
    return [k for k, t in enumerate(TileScheduler(h, w, rows, cols, overlap).tiles)
            if not lab[t.read_bbox[0]:t.read_bbox[1], t.read_bbox[2]:t.read_bbox[3]].any()]


def _plan(rows, cols, overlap, auto, forced=()):
    return {"grid": [rows, cols], "overlap": overlap, "auto": list(auto), "user_kept": [],
            "user_forced": [{"tile": t, "tissue_fraction": 0.5} for t in forced]}


def _outputs(d):
    out = {}
    for base, dirs, files in os.walk(d):
        if base.endswith(".zarr") and ".zarray" in files:
            out[os.path.relpath(base, d)] = np.asarray(zarr.open(base, mode="r"))
        for f in files:
            if f.endswith((".ome.tiff", ".ome.tif")):
                out[f] = tifffile.imread(os.path.join(base, f))
    return out


def _run(worker, zp, loop):
    if loop == "segment_one_zarr":
        return worker._segment_one_zarr(zp, "t", model=None, use_gpu=False,
                                        log=logging.getLogger("test"))
    got = {}
    worker.finished.connect(lambda d, n: got.setdefault("n", n))
    worker.error.connect(lambda m: got.setdefault("error", m))
    worker.run()
    assert "error" not in got, got.get("error")
    return got.get("n")


LOOPS = ["segment_one_zarr", "run"]
ROWS, COLS, OVERLAP = 3, 4, 8


@pytest.mark.parametrize("loop", LOOPS)
def test_with_no_plan_nothing_changes(tmp_path, monkeypatch, loop):
    """S1: a config without `skip_tiles` is today's run, outputs and all."""
    lab = _labels(3)
    w, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP)
    _run(w, zp, loop)
    assert "skip_tiles" not in w._step2_engine_meta()
    assert w._tile_skip_set() == frozenset()


@pytest.mark.parametrize("loop", LOOPS)
def test_skipping_empty_tiles_changes_no_output(tmp_path, monkeypatch, loop):
    """S2: the skipped tiles really are empty -> every output is equal; the
    engine runs fewer times; the DAPI output has no hole."""
    lab = _labels(3)
    empty = _empty_tiles(lab, ROWS, COLS, OVERLAP)
    assert empty, "the test image must have empty tiles"
    calls_off, calls_on, skipped = [], [], []
    w_off, zp_off = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                            calls=calls_off, name="off")
    n_off = _run(w_off, zp_off, loop)
    w_on, zp_on = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                          skip=_plan(ROWS, COLS, OVERLAP, empty), calls=calls_on, name="on")
    w_on.tile_skipped.connect(lambda i, n: skipped.append(i))
    n_on = _run(w_on, zp_on, loop)
    assert n_on == n_off and n_off > 0
    assert sorted(skipped) == empty
    assert len(calls_on) == len(calls_off) - len(empty)
    a, b = _outputs(w_off.output_dir), _outputs(w_on.output_dir)
    assert sorted(a) == sorted(b) and a
    assert any("dapi" in k.lower() for k in a), sorted(a)      # the DAPI output is compared
    for k in a:
        np.testing.assert_array_equal(a[k], b[k], err_msg=k)


def test_the_prefetcher_keeps_up(tmp_path, monkeypatch):
    """S4: no dead entries in the read-ahead queue."""
    from block01.utils import tile_scheduler as ts
    seen = []
    real = ts.TileScheduler.attach_prefetcher

    def spy(self, *a, **k):
        p = real(self, *a, **k)
        seen.append(p)
        return p
    monkeypatch.setattr(ts.TileScheduler, "attach_prefetcher", spy)
    lab = _labels(3)
    empty = _empty_tiles(lab, ROWS, COLS, OVERLAP)
    w, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                    skip=_plan(ROWS, COLS, OVERLAP, empty))
    _run(w, zp, "segment_one_zarr")
    m = seen[-1].metrics
    assert m["prefetch_miss"] <= 1 and m["prefetch_hit"] >= ROWS * COLS - 1


def test_the_run_records_what_it_left_out(tmp_path, monkeypatch):
    """S5: the plan in the run's params and meta; the user's forced tile
    apart from the tiles without tissue."""
    lab = _labels(3)
    empty = _empty_tiles(lab, ROWS, COLS, OVERLAP)
    forced = [k for k in range(ROWS * COLS) if k not in empty][:1]
    w, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                    skip=_plan(ROWS, COLS, OVERLAP, empty, forced))
    _run(w, zp, "run")
    meta = json.load(open(os.path.join(w.output_dir, "segmentation_meta.json")))
    rec = meta["tile_strategy"]["skip_tiles"]
    assert rec["skipped"] == sorted(empty + forced)
    assert rec["skipped_without_tissue"] == sorted(empty)
    assert rec["skipped_by_user_with_tissue"] == [{"tile": forced[0], "tissue_fraction": 0.5}]
    params = json.load(open(os.path.join(w.output_dir, "run_segmentation_params.json")))
    assert params["skip_tiles"]["auto"] == empty


@pytest.mark.parametrize("bad", [{"grid": [4, 4]}, {"overlap": 9}, {"auto": [99]}])
def test_a_plan_for_another_grid_is_refused(tmp_path, monkeypatch, bad):
    """S10: fail-closed, before any output."""
    lab = _labels(3)
    w, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP,
                    skip=dict(_plan(ROWS, COLS, OVERLAP, [0]), **bad))
    errors = []
    w.error.connect(errors.append)
    w.run()
    assert errors and ("skip" in errors[0].lower())


# ── the page: S6, S9, O1 ────────────────────────────────────────────────

@pytest.fixture
def page(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    _slide, zp = _workspace(tmp_path, (0, SH, 0, SW))
    p = Step2Page()
    p._zarr_edit.setText(zp)
    p._load_zarr_info()
    p._rows_spin.setValue(2)
    p._cols_spin.setValue(6)
    p._overlap_spin.setValue(100)
    yield p
    p.deleteLater()


def _pen(p, k):
    nc = p._cols_spin.value()
    return p._tile_rects[divmod(k, nc)].pen.color().name()


def test_the_option_is_off_by_default_and_proposes_when_ticked(page):
    assert not page._skip_empty_cb.isChecked() and page.skip_tiles_config() is None
    assert "skip_tiles" not in page.get_seg_config()
    page._skip_empty_cb.setChecked(True)
    plan = page.skip_tiles_config()
    assert plan and plan["grid"] == [2, 6] and plan["auto"]
    for k in plan["auto"]:
        assert _pen(page, k) == "#5a7896"
    assert page.get_seg_config()["skip_tiles"]["auto"] == plan["auto"]


def test_clicking_tiles_and_the_question_for_tissue(page, monkeypatch):
    page._skip_empty_cb.setChecked(True)
    auto = page.skip_tiles_config()["auto"]
    tissue = [k for k in range(12) if k not in auto][0]
    asked = []
    monkeypatch.setattr(page, "_confirm_forced_skip",
                        lambda i, f: asked.append((i, f)) or False)
    page._on_tile_clicked(*divmod(auto[0], 6))       # back to segmenting: no question
    assert asked == [] and auto[0] in page.skip_tiles_config()["user_kept"]
    page._on_tile_clicked(*divmod(tissue, 6))        # tissue: asked; refused
    assert asked and asked[0][0] == tissue and asked[0][1] > 0
    assert page.skip_tiles_config()["user_forced"] == []
    monkeypatch.setattr(page, "_confirm_forced_skip", lambda i, f: True)
    page._on_tile_clicked(*divmod(tissue, 6))        # confirmed: the user's skip
    forced = page.skip_tiles_config()["user_forced"]
    assert [e["tile"] for e in forced] == [tissue] and forced[0]["tissue_fraction"] > 0
    assert _pen(page, tissue) == "#c678dd"
    page._cols_spin.setValue(5)                      # another grid: choices reset
    cfg = page.skip_tiles_config()
    assert cfg["grid"] == [2, 5] and cfg["user_forced"] == [] and cfg["user_kept"] == []
    assert "reset" in page._skip_hint_lbl.text()


def test_during_a_run_the_option_is_locked_and_skips_are_drawn(page):
    page._skip_empty_cb.setChecked(True)
    auto = page.skip_tiles_config()["auto"]
    page._n_rows, page._n_cols = 2, 6
    page._begin_run_progress(2, 6)
    assert not page._skip_empty_cb.isEnabled()
    page._on_tile_clicked(*divmod(auto[0], 6))       # no edits while running
    assert auto[0] not in page.skip_tiles_config()["user_kept"]
    page._on_tile_skipped(auto[0], 12)
    assert _pen(page, auto[0]) == "#5a7896"
    page._rows_spin.setValue(3)                      # another grid: nothing drawn
    assert {r.pen.color().name() for r in page._tile_rects.values()} == {"#808080"}
    page._rows_spin.setValue(2)
    assert _pen(page, auto[0]) == "#5a7896"


def test_the_overview_long_side_is_at_most_4096(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    zp = str(tmp_path / "wide.zarr")
    zarr.open(zp, mode="w", shape=(600, 8200, 2), chunks=(600, 1024, 2), dtype=np.uint16)
    p = Step2Page()
    p._zarr_edit.setText(zp)
    p._load_zarr_info()
    assert p._ov_ds == 3 and max(p._ov_h, p._ov_w) <= 4096
    x0 = p._tile_rects[(0, 1)].pos().x() * p._ov_ds              # tile (0, 1) starts at
    assert abs(x0 - -(-8200 // p._cols_spin.value())) < p._ov_ds  # its full-res column
    p.deleteLater()


# ── S7: a run with skipped tiles goes on to Step3 and Step4 ─────────────

def test_a_run_with_skipped_tiles_opens_in_step3_and_quantifies(app, tmp_path):
    pytest.importorskip("stardist")
    import test_step2_runner_path as t2
    import test_v16_artifact_graph as tg
    from block01.core import quant_sources as qs
    from block01.core import step3_masks as sm
    from block01.workers.feature_extract_worker import run_extraction
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    slide = tg._nuclei_slide(tmp_path)
    _project, ctx = tg._step0(tmp_path, slide)
    fused = tg._fuse(ctx, slide)
    cfg = dict(t2._contract_config(tmp_path / "cfg", "stardist_nuclei_expansion"))
    cfg["skip_tiles"] = _plan(2, 2, t2.HALO, [3])
    w = SegmentMergeWorker(fused, seg_config=cfg, n_rows=2, n_cols=2, overlap_px=t2.HALO,
                           output_dir=ctx["step_dirs"]["step2"],
                           rois=[dict(tg.ROI, roi_id=ctx["roi_id"])])
    got = t2._collect(w)
    w.run()
    assert got["error"] == [] and got["finished"]
    run = sm.load_run(w.output_dir)
    assert not isinstance(run, str), run
    mask = np.asarray(zarr.open(os.path.join(w.output_dir, "global_mask_Full WSI.zarr"), mode="r"))
    tiles = TileScheduler(mask.shape[0], mask.shape[1], 2, 2, t2.HALO).tiles
    oy0, oy1, ox0, ox1 = tiles[3].own_bbox
    seen = np.zeros(mask.shape, bool)              # what the segmented tiles read
    for t in tiles[:3]:
        ry0, ry1, rx0, rx1 = t.read_bbox
        seen[ry0:ry1, rx0:rx1] = True
    # nothing was segmented in the skipped tile: every labelled pixel there
    # comes from a neighbour that saw it through its halo
    labelled = mask[oy0:oy1, ox0:ox1] > 0
    assert not (labelled & ~seen[oy0:oy1, ox0:ox1]).any()
    res = run_extraction(w.output_dir, str(tmp_path / "q"), roi_name="Full WSI")
    assert res["n_cells"] > 0
    job = qs.resolve_quant_job(w.output_dir)
    assert job.run_id == os.path.basename(w.output_dir)
