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
pytest.importorskip("block01.qt")
tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
from block01.qt import QtWidgets  # noqa: E402

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
    """<proj>/rois/ws/{roi_manifest.json, settings/step1_fusion_settings.json,
    step1/fused zarr} (block RM §4: saved settings live in settings/)."""
    slide = _slide(tmp_path / "slide.ome.tif")
    ws = tmp_path / "proj" / "rois" / "ws"
    (ws / "step1").mkdir(parents=True)
    (ws / "settings").mkdir(parents=True)
    json.dump({"roi_id": "ws", "source_ome": slide, "bbox_fullres": list(bbox)},
              open(ws / "roi_manifest.json", "w"))
    json.dump({"fusion_config": {"nucleus": {"channel": "DAPI", "weight": 1.0}}},
              open(ws / "settings" / "step1_fusion_settings.json", "w"))
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
    _overview_loaded(p)
    yield p
    p.deleteLater()


def _overview_loaded(p, timeout=20.0):
    """Wait for the overview's background read: a page deleted while it
    still runs is a test artefact (its signal would reach a dead object)."""
    import time
    end = time.monotonic() + timeout
    while p._ov_status.text().startswith('Loading') and time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    assert not p._ov_status.text().startswith('Loading'), p._ov_status.text()


def _pen(p, k):
    nc = p._cols_spin.value()
    return p._tile_rects[divmod(k, nc)].pen.color().name()


def test_the_option_is_off_by_default_and_ticking_it_skips_nothing(page):
    assert not page._skip_empty_cb.isChecked() and page.skip_tiles_config() is None
    assert "skip_tiles" not in page.get_seg_config()
    page._skip_empty_cb.setChecked(True)
    plan = page.skip_tiles_config()
    assert plan and plan["grid"] == [2, 6] and plan["auto"]            # proposed, not applied
    assert page._plan_skips() == set() and plan["user_kept"] == plan["auto"]
    assert {r.pen.color().name() for r in page._tile_rects.values()} == {"#808080"}
    assert f'0 of 12 tiles skipped ({len(plan["auto"])} have no tissue' in page._skip_hint_lbl.text()
    page._skip_all_empty_btn.click()                                   # the proposal applied
    for k in plan["auto"]:
        assert _pen(page, k) == "#c678dd"
    assert page.get_seg_config()["skip_tiles"]["user_kept"] == []


def test_clicking_tiles_and_the_question_for_tissue(page, monkeypatch):
    _tick(page)
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
    assert cfg["grid"] == [2, 5] and cfg["user_forced"] == [] and page._plan_skips() == set()
    assert "reset" in page._skip_hint_lbl.text()


def test_during_a_run_the_option_is_locked_and_skips_are_drawn(page):
    _tick(page)
    auto = page.skip_tiles_config()["auto"]
    page._n_rows, page._n_cols = 2, 6
    page._begin_run_progress(2, 6)
    assert not page._skip_empty_cb.isEnabled()
    page._on_tile_clicked(*divmod(auto[0], 6))       # no edits while running
    assert auto[0] not in page.skip_tiles_config()["user_kept"]
    page._on_tile_skipped(auto[0], 12)
    assert _pen(page, auto[0]) == "#c678dd"
    page._rows_spin.setValue(3)                      # another grid: nothing drawn
    assert {r.pen.color().name() for r in page._tile_rects.values()} == {"#808080"}
    page._rows_spin.setValue(2)
    assert _pen(page, auto[0]) == "#c678dd"


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
    _project, ctx = tg._step0_run(tmp_path, slide)      # block RM: the run chain
    fused = tg._fuse_run(ctx, slide)
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


# ── a visible selection and a Shift+drag box (docs/v17_step2_skip_tiles_ux_plan.md)

from block01.qt import QtCore, QtGui  # noqa: E402

PURPLE = "#c678dd"
Qt = QtCore.Qt


@pytest.fixture
def shown(page):
    """The page on an (offscreen) screen, so its views have a geometry."""
    page.setAttribute(Qt.WA_DontShowOnScreen, True)
    page.resize(1400, 900)
    page.show()
    QtWidgets.QApplication.processEvents()
    page._ov_vb.autoRange(padding=0)
    QtWidgets.QApplication.processEvents()
    return page


def _rect(p, k):
    return p._tile_rects[divmod(k, p._cols_spin.value())]


def _empty_and_tissue(page):
    auto = page.skip_tiles_config()["auto"]
    return auto, [k for k in range(12) if k not in auto]


def _questions(page, monkeypatch, answer=True):
    asked = []
    monkeypatch.setattr(page, "_confirm_forced_skip", lambda i, f: asked.append([i]) or answer)
    monkeypatch.setattr(page, "_confirm_forced_skips", lambda t: asked.append(sorted(t)) or answer)
    return asked


def _tick(page):
    """The option ticked and its proposal applied (ticking alone skips
    nothing, plan §13)."""
    page._skip_empty_cb.setChecked(True)
    page._skip_all_empty_btn.click()


def _let_go(page, monkeypatch):
    """Shift released: the collected tiles are committed (the 100 ms check, now)."""
    monkeypatch.setattr(page, "_shift_held", lambda: False)
    page._shift_poll()


def _skipped(page):
    plan = page._skip_plan
    return sorted(page._plan_skips()), sorted(plan["user_kept"]), sorted(plan["user_forced"])


# the look ---------------------------------------------------------------

def test_a_skipped_tile_is_filled_purple_with_a_thick_solid_border(page, monkeypatch):
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    r = _rect(page, auto[0])
    assert r.pen.color().name() == PURPLE and r.pen.widthF() == 4.0
    assert r.pen.style() == Qt.SolidLine
    assert r.fill is not None and r.fill.name() == PURPLE and abs(r.fill.alphaF() - 0.30) < 0.01
    assert r.hatch is None
    idle = _rect(page, tissue[0])
    assert idle.fill is None and idle.hatch is None and idle.zValue() < r.zValue()
    _questions(page, monkeypatch, True)
    page._on_tile_clicked(*divmod(tissue[0], 6))         # a tissue tile the user skips
    forced = _rect(page, tissue[0])
    assert forced.fill is not None and forced.hatch is not None and forced.pen.widthF() == 4.0
    assert "picked" not in page.TILE_COLOURS             # no cyan intermediate state


def test_during_a_run_purple_stays_and_the_users_tissue_tiles_stay_hatched(page, monkeypatch):
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    _questions(page, monkeypatch, True)
    page._on_tile_clicked(*divmod(tissue[0], 6))
    page._begin_run_progress(2, 6)
    page._on_tile_skipped(auto[0], 12)
    page._on_tile_skipped(tissue[0], 12)
    page._record_tile(auto[0] - 1 if auto[0] % 6 else auto[0] + 1, "done")
    a, f = _rect(page, auto[0]), _rect(page, tissue[0])
    assert a.fill is not None and a.hatch is None and a.pen.widthF() == 4.0
    assert f.fill is not None and f.hatch is not None
    done = _rect(page, auto[0] - 1 if auto[0] % 6 else auto[0] + 1)
    assert done.pen.color().name() == page.TILE_COLOURS["done"] and done.zValue() > a.zValue()


def test_the_zoomable_items_carry_the_look_in_draw_order(page, monkeypatch):
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    _questions(page, monkeypatch, True)
    page._on_tile_clicked(*divmod(tissue[0], 6))
    page._begin_run_progress(2, 6)
    page._on_tile_skipped(auto[0], 12)
    page._on_tile_skipped(tissue[0], 12)
    page._record_tile(tissue[1], "done")
    items = page.tile_items()
    ids = [i["id"] for i in items]
    nc = 6
    sk = items[ids.index("tile_%d_%d" % divmod(auto[0], nc))]
    fo = items[ids.index("tile_%d_%d" % divmod(tissue[0], nc))]
    dn = items[ids.index("tile_%d_%d" % divmod(tissue[1], nc))]
    assert sk["color"] == PURPLE and sk["width"] == 4.0 and sk["fill"] == page.SKIP_FILL
    assert "hatch" not in sk and fo["hatch"] is True
    assert "width" not in dn and "fill" not in dn                     # the overlay's own default
    idle = [k for k in range(12) if k not in (auto[0], tissue[0], tissue[1])][0]
    assert ids.index("tile_%d_%d" % divmod(idle, nc)) < ids.index(sk["id"]) < ids.index(dn["id"])


# the overview: real mouse events on its viewport ------------------------

def _vp(page, r, c, fy=0.5, fx=0.5):
    nr, nc, th, tw, _oy, _ox = page._tile_geometry()
    ds = page._ov_ds
    sc = page._ov_vb.mapViewToScene(QtCore.QPointF((c + fx) * tw / ds, (r + fy) * th / ds))
    return page._ov_gv.mapFromScene(sc)


def _mouse(widget, kind, pos, mods=Qt.NoModifier):
    t = {"press": QtCore.QEvent.MouseButtonPress, "move": QtCore.QEvent.MouseMove,
         "release": QtCore.QEvent.MouseButtonRelease}[kind]
    button = Qt.NoButton if kind == "move" else Qt.LeftButton
    buttons = Qt.NoButton if kind == "release" else Qt.LeftButton
    local = QtCore.QPointF(pos)
    ev = QtGui.QMouseEvent(t, local, QtCore.QPointF(widget.mapToGlobal(QtCore.QPoint(int(local.x()), int(local.y())))),
                           button, buttons, mods)
    QtWidgets.QApplication.sendEvent(widget, ev)
    QtWidgets.QApplication.processEvents()


def _drag(page, a, b, *, shift=True, monkeypatch=None, between=None):
    vp = page._ov_gv.viewport()
    mods = Qt.ShiftModifier if shift else Qt.NoModifier
    _mouse(vp, "press", a, mods)
    if between is not None:
        between()
    _mouse(vp, "move", b, mods)
    _mouse(vp, "release", b, mods)


def test_a_box_from_a_segmented_tile_skips_with_one_question(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    asked = _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    _drag(page, _vp(page, 0, 0), _vp(page, 1, 1))
    assert asked == [] and page._skip_plan["user_forced"] == {}        # collected only
    _let_go(page, monkeypatch)
    assert asked == [[0, 1, 6, 7]]                                     # once, for all four
    assert sorted(page._skip_plan["user_forced"]) == [0, 1, 6, 7]
    assert all(_rect(page, k).hatch is not None for k in (0, 1, 6, 7))


def test_a_box_from_a_skipped_tile_restores_without_a_question(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    asked = _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    r0, c0 = divmod(min(auto), 6)
    _drag(page, _vp(page, r0, c0), _vp(page, 1, 5))
    _let_go(page, monkeypatch)
    assert asked == [] and page._plan_skips() == set()
    assert sorted(page._skip_plan["user_kept"]) == sorted(auto)


def test_a_box_sets_mixed_tiles_and_leaves_those_already_so(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    a0, a1, a2 = sorted(k for k in auto if k < 6)[:3]
    _questions(page, monkeypatch, True)
    page._on_tile_clicked(0, a1)                                       # the middle one restored
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    toggled = []
    real = page._toggle_tiles
    monkeypatch.setattr(page, "_toggle_tiles", lambda t: toggled.append(sorted(t)) or real(t))
    _drag(page, _vp(page, 0, a1), _vp(page, 0, a2))                    # start segmented: skip
    _let_go(page, monkeypatch)
    assert toggled == [[a1]]                                           # a2 was skipped already
    assert a1 in page._plan_skips() and a2 in page._plan_skips()
    toggled.clear()
    page._on_tile_clicked(0, a1)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    _drag(page, _vp(page, 0, a0), _vp(page, 0, a2))                    # start skipped: restore
    _let_go(page, monkeypatch)
    assert toggled[-1] == [a0, a2]                                     # a1 was segmented already


def test_no_to_the_question_keeps_tissue_and_skips_the_empty_tiles(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    page._skip_none_btn.click()
    asked = _questions(page, monkeypatch, False)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    _drag(page, _vp(page, 0, 0), _vp(page, 0, 5))
    _let_go(page, monkeypatch)
    row0_tissue = [k for k in tissue if k < 6]
    assert asked == [row0_tissue] or (len(row0_tissue) == 1 and asked == [row0_tissue])
    assert page._plan_skips() == {k for k in auto if k < 6}


def test_shift_clicks_collect_and_letting_go_commits_once(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    r, c = divmod(auto[0], 6)
    vp = page._ov_gv.viewport()
    toggled = []
    real = page._toggle_tiles
    monkeypatch.setattr(page, "_toggle_tiles", lambda t: toggled.append(sorted(t)) or real(t))
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    _mouse(vp, "press", _vp(page, r, c), Qt.ShiftModifier)
    _mouse(vp, "release", _vp(page, r, c), Qt.ShiftModifier)
    assert toggled == [] and page._pending == {auto[0]: False}          # collected, not applied
    assert _rect(page, auto[0]).frame is not None and _rect(page, auto[0]).fill is None
    assert page._shift_timer.isActive()
    _mouse(vp, "press", _vp(page, r, c), Qt.ShiftModifier)              # again: taken back out
    _mouse(vp, "release", _vp(page, r, c), Qt.ShiftModifier)
    assert page._pending == {} and _rect(page, auto[0]).frame is None
    assert not page._shift_timer.isActive()                             # nothing left to wait for
    _mouse(vp, "press", _vp(page, r, c), Qt.ShiftModifier)
    _mouse(vp, "release", _vp(page, r, c), Qt.ShiftModifier)
    _let_go(page, monkeypatch)
    assert toggled == [[auto[0]]] and auto[0] not in page._plan_skips()
    assert not page._shift_timer.isActive() and _rect(page, auto[0]).frame is None
    _mouse(vp, "press", _vp(page, r, c))                                # a plain click: at once
    _mouse(vp, "release", _vp(page, r, c))
    assert toggled == [[auto[0]], [auto[0]]] and auto[0] in page._plan_skips()


def test_shift_collects_clicks_and_boxes_and_asks_once_at_the_end(shown, monkeypatch):
    """The user's rule (plan §13): no question while Shift is held; one
    question naming every collected tile with tissue when it is let go."""
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    page._skip_none_btn.click()
    asked = _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    vp = page._ov_gv.viewport()
    t0, t1 = tissue[0], tissue[1]
    for k in (t0, t1):                                                  # two Shift-clicks
        _mouse(vp, "press", _vp(page, *divmod(k, 6)), Qt.ShiftModifier)
        _mouse(vp, "release", _vp(page, *divmod(k, 6)), Qt.ShiftModifier)
    e0 = auto[0]
    _drag(page, _vp(page, *divmod(e0, 6)), _vp(page, *divmod(e0, 6), fx=0.95))   # a small box
    _drag(page, _vp(page, 1, 4), _vp(page, 1, 5))                       # and a real one
    assert asked == [] and page._plan_skips() == set()
    framed = {k for k in range(12) if _rect(page, k).frame is not None}
    want = {t0, t1, e0, 10, 11}
    assert framed == want
    _let_go(page, monkeypatch)
    with_tissue = sorted(k for k in want if k in tissue)
    assert asked == [with_tissue]                                       # ONE question
    assert page._plan_skips() == want


def test_no_at_the_end_keeps_the_collected_tissue_tiles(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    page._skip_none_btn.click()
    asked = _questions(page, monkeypatch, False)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    for k in (tissue[0], tissue[1], auto[0]):
        page._box_begin(*divmod(k, 6))
        page._box_click()
    _let_go(page, monkeypatch)
    assert asked == [sorted((tissue[0], tissue[1]))]
    assert page._plan_skips() == {auto[0]}


def _wait(ms):
    import time
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


def test_the_running_check_commits_when_shift_is_let_go_but_not_during_a_box(shown, monkeypatch):
    """The real 100 ms check, not a direct call: nothing while Shift is down
    or a box is held; the commit once Shift is up and the box is released."""
    page = shown
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    held = [True]
    monkeypatch.setattr(page, "_shift_held", lambda: held[0])
    page._box_begin(*divmod(auto[0], 6))
    page._box_click()                                                   # collected
    _wait(350)
    assert page._pending and page._shift_timer.isActive()               # Shift still down
    page._box_begin(*divmod(auto[1], 6))                                # a box is being dragged
    held[0] = False
    _wait(350)
    assert page._pending and page._box is not None                      # not while the box is held
    page._box_finish()                                                  # released, Shift up: at once
    assert page._pending == {} and not page._shift_timer.isActive()
    assert auto[0] not in page._plan_skips() and auto[1] not in page._plan_skips()
    held[0] = True
    page._box_begin(*divmod(auto[2], 6))
    page._box_click()
    held[0] = False
    _wait(350)                                                          # let go: the timer commits
    assert page._pending == {} and auto[2] not in page._plan_skips()
    assert not page._shift_timer.isActive()


def test_shift_is_read_live_from_the_system():
    """Shift let go in another window must be seen: the live state."""
    import inspect
    from block01.ui import step2_page as mod
    src = inspect.getsource(mod.Step2Page._shift_held)
    assert "queryKeyboardModifiers" in src


def test_a_plain_click_commits_the_collected_tiles_first(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    asked = _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    page._box_begin(*divmod(auto[0], 6))
    page._box_click()                                                   # collected: restore
    monkeypatch.setattr(page, "_shift_held", lambda: False)
    page._on_tile_clicked(*divmod(auto[1], 6))                          # before the 100 ms check
    assert page._pending == {} and asked == []
    assert auto[0] not in page._plan_skips() and auto[1] not in page._plan_skips()


@pytest.mark.parametrize("how", ["rows", "untick", "skip_all_empty", "skip_none"])
def test_collected_tiles_are_dropped(shown, monkeypatch, how):
    page = shown
    _tick(page)
    _auto, tissue = _empty_and_tissue(page)
    asked = _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    page._box_begin(*divmod(tissue[0], 6))
    page._box_click()
    assert page._pending
    before = page._plan_skips()
    {"rows": lambda: page._rows_spin.setValue(3),
     "untick": lambda: page._skip_empty_cb.setChecked(False),
     "skip_all_empty": page._skip_all_empty_btn.click,
     "skip_none": page._skip_none_btn.click}[how]()
    assert page._pending == {} and not page._shift_timer.isActive()
    _let_go(page, monkeypatch)
    assert asked == []
    if how in ("skip_all_empty",):
        assert page._plan_skips() == before
    assert all(r.frame is None for r in page._tile_rects.values())


def test_without_shift_a_drag_changes_nothing(shown, monkeypatch):
    page = shown
    _tick(page)
    before = _skipped(page)
    monkeypatch.setattr(page, "_shift_held", lambda: False)
    _drag(page, _vp(page, 0, 0), _vp(page, 1, 5), shift=False)
    assert _skipped(page) == before and page._box is None


def test_the_box_previews_the_result_framed_in_white_and_clears_after(shown, monkeypatch):
    page = shown
    _tick(page)
    _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    vp = page._ov_gv.viewport()
    _mouse(vp, "press", _vp(page, 0, 0), Qt.ShiftModifier)
    _mouse(vp, "move", _vp(page, 0, 1), Qt.ShiftModifier)
    for k in (0, 1):                                 # tissue tiles to be skipped: purple, hatched
        r = _rect(page, k)
        assert r.pen.color().name() == PURPLE and r.pen.widthF() == 4.0
        assert r.fill is not None and r.hatch is not None
        assert r.frame is not None and r.frame.color().name() == "#ffffff" and r.frame.widthF() == 2.0
    assert _rect(page, 2).frame is None
    _mouse(vp, "release", _vp(page, 0, 1), Qt.ShiftModifier)
    assert _rect(page, 0).frame is not None                            # collected until let go
    _let_go(page, monkeypatch)
    assert all(r.frame is None for r in page._tile_rects.values())


def test_a_restore_box_previews_plain_tiles_framed_in_white(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    r0, c0 = divmod(min(auto), 6)
    page._box_begin(r0, c0)
    r = _rect(page, min(auto))
    assert r.fill is None and r.hatch is None and r.frame is not None
    page._box_cancel()
    assert r.fill is not None and r.frame is None


def test_shift_released_mid_drag_still_commits_and_outside_is_clamped(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    r0, c0 = divmod(min(auto), 6)
    far = _vp(page, 1, 5, fy=3.0, fx=3.0)                               # beyond the grid's corner
    _drag(page, _vp(page, r0, c0), far,
          between=lambda: monkeypatch.setattr(page, "_shift_held", lambda: False))
    assert page._plan_skips() == set()                                  # every boxed tile restored


def test_moving_within_the_same_range_does_not_redraw(shown, monkeypatch):
    page = shown
    _tick(page)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    shows = []
    real = page._show_box
    monkeypatch.setattr(page, "_show_box", lambda: shows.append(1) or real())
    vp = page._ov_gv.viewport()
    _mouse(vp, "press", _vp(page, 0, 0, 0.2, 0.2), Qt.ShiftModifier)
    n = len(shows)
    _mouse(vp, "move", _vp(page, 0, 0, 0.6, 0.6), Qt.ShiftModifier)
    _mouse(vp, "move", _vp(page, 0, 0, 0.8, 0.3), Qt.ShiftModifier)
    assert len(shows) == n
    _mouse(vp, "move", _vp(page, 0, 1), Qt.ShiftModifier)
    assert len(shows) == n + 1
    page._box_cancel()


@pytest.mark.parametrize("how", ["rows", "cols", "overlap", "untick", "skip_all_empty",
                                 "skip_none", "run"])
def test_a_box_in_progress_is_dropped(shown, monkeypatch, how):
    page = shown
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    asked = _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    vp = page._ov_gv.viewport()
    _mouse(vp, "press", _vp(page, 0, 0), Qt.ShiftModifier)
    _mouse(vp, "move", _vp(page, 1, 1), Qt.ShiftModifier)
    assert page._box is not None
    if how == "rows":
        page._rows_spin.setValue(3)
    elif how == "cols":
        page._cols_spin.setValue(5)
    elif how == "overlap":
        page._overlap_spin.setValue(150)
    elif how == "untick":
        page._skip_empty_cb.setChecked(False)
    elif how == "skip_all_empty":
        page._skip_all_empty_btn.click()
    elif how == "skip_none":
        page._skip_none_btn.click()
    else:
        from block01.ui import step2_page as mod
        monkeypatch.setattr(mod.QMessageBox, "warning", lambda *a, **k: None)
        page._zarr_path = None
        page._run()
    assert page._box is None and not page._box_preview
    before = page.skip_tiles_config()
    _mouse(vp, "release", _vp(page, 1, 1), Qt.ShiftModifier)
    assert asked == [] and page.skip_tiles_config() == before
    assert all(r.frame is None for r in page._tile_rects.values())


# the two buttons --------------------------------------------------------

def test_the_two_buttons(page, monkeypatch):
    assert page._skip_btn_row.isHidden()                               # the option is off
    _tick(page)
    assert not page._skip_btn_row.isHidden()
    auto, tissue = _empty_and_tissue(page)
    asked = _questions(page, monkeypatch, True)
    page._on_tile_clicked(*divmod(auto[0], 6))
    page._on_tile_clicked(*divmod(tissue[0], 6))
    asked.clear()
    page._skip_none_btn.click()
    assert asked == [] and page._plan_skips() == set()
    assert page.skip_tiles_config()["user_forced"] == []
    page._skip_all_empty_btn.click()
    assert asked == [] and page._plan_skips() == set(auto)
    cfg = page.skip_tiles_config()
    assert cfg["user_kept"] == [] and cfg["user_forced"] == []
    page._begin_run_progress(2, 6)
    assert not page._skip_all_empty_btn.isEnabled() and not page._skip_none_btn.isEnabled()
    page._skip_none_btn.click()                                        # locked: nothing happens
    assert page._plan_skips() == set(auto)
    page._skip_empty_cb.setEnabled(True)
    page._skip_empty_cb.setChecked(False)
    assert page._skip_btn_row.isHidden()


# painted pixels ---------------------------------------------------------

def _px(img, pt):
    return img.pixelColor(int(round(pt.x())), int(round(pt.y())))


def test_the_overview_paints_purple_hatching_green_edges_and_the_white_frame(shown, monkeypatch):
    page = shown
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    _questions(page, monkeypatch, True)
    page._on_tile_clicked(*divmod(tissue[0], 6))
    QtWidgets.QApplication.processEvents()
    img = page._ov_gv.viewport().grab().toImage()
    pr, pc = divmod(auto[0], 6)
    purple = _px(img, _vp(page, pr, pc))
    idle_k = [k for k in tissue if k != tissue[0]][0]
    idle = _px(img, _vp(page, *divmod(idle_k, 6)))
    assert (purple.blue() - purple.green()) > (idle.blue() - idle.green()) + 20
    fr, fc = divmod(tissue[0], 6)
    row = [_px(img, _vp(page, fr, fc, 0.5, 0.2 + 0.6 * i / 80)) for i in range(81)]
    reds = [c.red() for c in row]
    assert max(reds) - min(reds) > 30                                   # hatch lines over the fill
    # a done tile next to a skipped one: its green edge is drawn over the purple
    page._begin_run_progress(2, 6)
    left = min(k for k in auto if k < 6)
    page._on_tile_skipped(left, 12)
    page._record_tile(left - 1, "done")
    QtWidgets.QApplication.processEvents()
    img = page._ov_gv.viewport().grab().toImage()
    edge = _vp(page, 0, left % 6, 0.5, 0.0)
    near = [img.pixelColor(int(edge.x()) + d, int(edge.y())) for d in (-1, 0, 1)]
    assert any(c.green() > c.red() + 30 and c.green() > c.blue() + 30 for c in near)


def test_the_white_frame_is_painted_during_a_box(shown, monkeypatch):
    page = shown
    _tick(page)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    vp = page._ov_gv.viewport()
    _mouse(vp, "press", _vp(page, 0, 0), Qt.ShiftModifier)
    QtWidgets.QApplication.processEvents()
    img = vp.grab().toImage()
    top = _vp(page, 0, 0, 0.0, 0.5)
    near = [img.pixelColor(int(top.x()), int(top.y()) + d) for d in (-1, 0, 1)]
    assert any(min(c.red(), c.green(), c.blue()) > 200 for c in near)
    # the frame stays on its tile: nothing white along its sides' lines
    # beyond it (below the tile under its left side, right of it on its top)
    below = _vp(page, 1, 0, 0.5, 0.0)
    right = _vp(page, 0, 3, 0.0, 0.5)
    for pt in (below, right):
        for d in (-1, 0, 1):
            c = img.pixelColor(int(pt.x()) + (d if pt is below else 0), int(pt.y()) + (d if pt is right else 0))
            assert min(c.red(), c.green(), c.blue()) <= 200, (pt, c.name())
    page._box_cancel()


# the zoomable picture: a real RegionsOverlay with the grid's controller ----

@pytest.fixture
def zoom(page):
    import pyqtgraph as pg
    from block01.ui.regions.overlay import RegionsOverlay
    from block01.ui.step2_viewer import _GridClicks
    view = pg.GraphicsView()
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.resize(900, 600)
    vb = pg.ViewBox()
    vb.invertY(True)
    view.setCentralItem(vb)
    view.show()
    clicks = _GridClicks(page.tile_at, page._on_tile_clicked, box=page)
    ov = RegionsOverlay(view, vb, clicks)
    clicks.overlay = ov                                     # as Step2DapiViewer.refresh does
    vb.setRange(xRange=(0, SW), yRange=(0, SH), padding=0)
    QtWidgets.QApplication.processEvents()
    yield view, ov
    ov.detach()
    view.close()


def _zp(page, ov, r, c, fy=0.5, fx=0.5):
    nr, nc, th, tw, oy, ox = page._tile_geometry()
    return ov.to_widget(ox + (c + fx) * tw, oy + (r + fy) * th)


def test_the_zoomable_picture_box_commits_once_and_asks_once(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    asked = _questions(page, monkeypatch, True)
    toggled = []
    real = page._toggle_tiles
    monkeypatch.setattr(page, "_toggle_tiles", lambda t: toggled.append(sorted(t)) or real(t))
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    vp = view.viewport()
    _mouse(vp, "press", _zp(page, ov, 0, 0), Qt.ShiftModifier)
    _mouse(vp, "move", _zp(page, ov, 1, 1), Qt.ShiftModifier)
    items = page.tile_items()
    assert {i["id"] for i in items if i["color"] == "#ffffff"} == \
        {"tile_0_0_box", "tile_0_1_box", "tile_1_0_box", "tile_1_1_box"}
    under = [i for i in items if i["id"] == "tile_0_0"][0]
    assert under["color"] == PURPLE and under["hatch"] is True                 # the result, hatched
    ids = [i["id"] for i in items]
    assert ids.index("tile_0_0") < ids.index("tile_0_0_box")                   # the frame on top
    _mouse(vp, "release", _zp(page, ov, 1, 1), Qt.ShiftModifier)
    assert toggled == [] and asked == []
    _let_go(page, monkeypatch)
    assert toggled == [[0, 1, 6, 7]] and asked == [[0, 1, 6, 7]]


def test_the_zoomable_picture_shift_click_and_plain_click_toggle_once(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    r, c = divmod(auto[0], 6)
    toggled = []
    real = page._toggle_tiles
    monkeypatch.setattr(page, "_toggle_tiles", lambda t: toggled.append(sorted(t)) or real(t))
    vp = view.viewport()
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    _mouse(vp, "press", _zp(page, ov, r, c), Qt.ShiftModifier)
    _mouse(vp, "release", _zp(page, ov, r, c), Qt.ShiftModifier)
    assert toggled == [] and page._pending == {auto[0]: False}
    _let_go(page, monkeypatch)
    assert toggled == [[auto[0]]]
    _mouse(vp, "press", _zp(page, ov, r, c))
    _mouse(vp, "release", _zp(page, ov, r, c))
    assert toggled == [[auto[0]], [auto[0]]] and auto[0] in page._plan_skips()


def test_the_zoomable_picture_paints_the_fill(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    ov.set_items(page.tile_items())
    pm = QtGui.QPixmap(view.viewport().size())
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    ov._paint_static(p, ov.canvas_transform())
    p.end()
    img = pm.toImage()
    inside = _px(img, _zp(page, ov, *divmod(auto[0], 6)))
    empty = _px(img, _zp(page, ov, *divmod(tissue[0], 6)))
    assert inside.alpha() > 0 and inside.blue() > inside.green()
    assert empty.alpha() == 0


def _zdrag(page, ov, view, a, b, *, between=None):
    vp = view.viewport()
    _mouse(vp, "press", a, Qt.ShiftModifier)
    if between is not None:
        between()
    _mouse(vp, "move", b, Qt.ShiftModifier)
    _mouse(vp, "release", b, Qt.ShiftModifier)


def test_the_zoomable_picture_shift_released_mid_drag_and_outside_is_clamped(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    r0, c0 = divmod(min(auto), 6)
    far = ov.to_widget(SW + 900, SH + 900)                                     # beyond the corner
    _zdrag(page, ov, view, _zp(page, ov, r0, c0), far,
           between=lambda: monkeypatch.setattr(page, "_shift_held", lambda: False))
    assert page._plan_skips() == set()


def test_the_zoomable_picture_sets_mixed_tiles_and_redraws_only_on_a_new_range(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    a0, a1, a2 = sorted(k for k in auto if k < 6)[:3]
    page._on_tile_clicked(0, a1)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    toggled, shows = [], []
    real_t, real_s = page._toggle_tiles, page._show_box
    monkeypatch.setattr(page, "_toggle_tiles", lambda t: toggled.append(sorted(t)) or real_t(t))
    monkeypatch.setattr(page, "_show_box", lambda: shows.append(1) or real_s())
    vp = view.viewport()
    _mouse(vp, "press", _zp(page, ov, 0, a0, 0.2, 0.2), Qt.ShiftModifier)          # start skipped
    n = len(shows)
    _mouse(vp, "move", _zp(page, ov, 0, a0, 0.7, 0.7), Qt.ShiftModifier)
    assert len(shows) == n                                                      # same range
    _mouse(vp, "move", _zp(page, ov, 0, a2), Qt.ShiftModifier)
    assert len(shows) == n + 1
    _mouse(vp, "release", _zp(page, ov, 0, a2), Qt.ShiftModifier)
    _let_go(page, monkeypatch)
    assert toggled == [[a0, a2]]                                                # a1 left alone


def test_the_zoomable_picture_release_after_a_dropped_box_does_nothing(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    auto, _tissue = _empty_and_tissue(page)
    r, c = divmod(auto[0], 6)
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    before = page.skip_tiles_config()
    vp = view.viewport()
    _mouse(vp, "press", _zp(page, ov, r, c), Qt.ShiftModifier)
    page._box_cancel()                                                          # dropped meanwhile
    _mouse(vp, "release", _zp(page, ov, r, c), Qt.ShiftModifier)               # no movement
    assert page.skip_tiles_config() == before


def test_the_zoomable_picture_paints_hatching_green_edges_and_the_white_frame(page, zoom, monkeypatch):
    view, ov = zoom
    _tick(page)
    auto, tissue = _empty_and_tissue(page)
    _questions(page, monkeypatch, True)
    page._on_tile_clicked(*divmod(tissue[0], 6))

    def paint():
        ov.set_items(page.tile_items())
        pm = QtGui.QPixmap(view.viewport().size())
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        ov._paint_static(p, ov.canvas_transform())
        p.end()
        return pm.toImage()
    img = paint()
    fr, fc = divmod(tissue[0], 6)
    # painted on a transparent picture: the fill is translucent purple, the
    # hatch lines opaque purple -- they differ in alpha
    alphas = [_px(img, _zp(page, ov, fr, fc, 0.5, 0.2 + 0.6 * i / 80)).alpha() for i in range(81)]
    assert min(alphas) > 0 and max(alphas) - min(alphas) > 100                  # hatching over the fill
    monkeypatch.setattr(page, "_shift_held", lambda: True)
    page._box_begin(1, 0)
    img = paint()
    top = _zp(page, ov, 1, 0, 0.0, 0.5)
    assert any(min(c.red(), c.green(), c.blue()) > 200
               for c in (img.pixelColor(int(top.x()), int(top.y()) + d) for d in (-1, 0, 1)))
    page._box_cancel()
    page._begin_run_progress(2, 6)
    left = min(k for k in auto if k < 6)
    page._on_tile_skipped(left, 12)
    page._record_tile(left - 1, "done")
    img = paint()
    edge = _zp(page, ov, 0, left % 6, 0.5, 0.0)
    assert any(c.green() > c.red() + 30 and c.green() > c.blue() + 30
               for c in (img.pixelColor(int(edge.x()) + d, int(edge.y())) for d in (-1, 0, 1)))


def test_shift_is_taken_from_the_press_itself_in_both_pictures(shown, zoom, monkeypatch):
    """The user's Mac (2026-11-09): Shift + drag did nothing in the zoomable
    picture. What decides is the Shift the system sent WITH the press, not a
    remembered keyboard state; without it a drag changes nothing."""
    page = shown
    view, ov = zoom
    _tick(page)
    page._skip_none_btn.click()
    _questions(page, monkeypatch, True)
    monkeypatch.setattr(page, "_shift_held", lambda: False)            # the global state says no
    vp = view.viewport()
    _mouse(vp, "press", _zp(page, ov, 0, 0), Qt.ShiftModifier)          # ...the press says Shift
    _mouse(vp, "move", _zp(page, ov, 1, 1), Qt.ShiftModifier)
    assert page._box is not None
    _mouse(vp, "release", _zp(page, ov, 1, 1), Qt.ShiftModifier)        # Shift is up now: committed
    assert page._plan_skips() == {0, 1, 6, 7}
    ovp = page._ov_gv.viewport()                                        # the Tile Status overview
    _mouse(ovp, "press", _vp(page, 0, 4), Qt.ShiftModifier)
    _mouse(ovp, "move", _vp(page, 0, 5), Qt.ShiftModifier)
    assert page._box is not None
    _mouse(ovp, "release", _vp(page, 0, 5), Qt.ShiftModifier)
    assert {4, 5} <= page._plan_skips()
    before = page._plan_skips()
    monkeypatch.setattr(page, "_shift_held", lambda: True)             # global yes, press no
    _mouse(vp, "press", _zp(page, ov, 1, 4))
    _mouse(vp, "move", _zp(page, ov, 1, 5))
    _mouse(vp, "release", _zp(page, ov, 1, 5))
    assert page._box is None and page._plan_skips() == before
