"""Step2 runs core by core when it follows cores (docs/v17_step2_core_by_core_plan.md
15A, §5): the units, one engine call per unit and none outside, whole cells
at a core's edge, the DAPI image complete, the skip plan ignored, Recovery
Mode refused, the metadata, Stop, and the page without a grid.

Synthetic slides and the fake model of `test_step2_skip_empty_tiles`.
"""

import json
import logging
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")

from test_f4_step2 import _cells, _snapshot, _with_regions  # noqa: E402
from test_step2_ownership_move import _labels  # noqa: E402
from test_step2_skip_empty_tiles import _plan, _worker  # noqa: E402

from block01.utils.step2_units import CAP_PX, core_units, engine_cap_px, split_grid  # noqa: E402

LOOPS = ["segment_one_zarr", "run"]


def _go(worker, zp, loop):
    if loop == "segment_one_zarr":
        return worker._segment_one_zarr(zp, "t", model=None, use_gpu=False, log=logging.getLogger("t"))
    got = {}
    worker.job_finished.connect(lambda d, n: got.setdefault("n", n))
    worker.error.connect(lambda m: got.setdefault("error", m))
    worker.run()
    assert "error" not in got, got.get("error")
    return got.get("n")


def _snap(h, w, boxes):
    """A cores snapshot with one rectangle core per box (y0, y1, x0, x1)."""
    s = _snapshot(h, w, core_box=boxes[0])
    s["mode"] = "cores"
    for i, (y0, y1, x0, x1) in enumerate(boxes):
        if i:
            s["cores"].append(dict(s["cores"][0], polygon=[[x0, y0], [x1 - 1, y0], [x1 - 1, y1 - 1], [x0, y1 - 1]]))
    for i, c in enumerate(s["cores"]):
        c["uid"], c["position_key"] = f"core_{i}", None
    from block01.core.regions import analysis as RA
    s["identity"] = RA.identity(s)
    return s


# ── the units ────────────────────────────────────────────────────────────

def test_units_are_the_cores_boxes_in_order_and_large_cores_split():
    snap = {"slide_shape": [10_000, 10_000], "cores": [
        {"uid": "a", "bbox": [100, 2100, 200, 2200]},
        {"uid": "b", "bbox": [5000, 9000, 1000, 9000]},          # 4000 x 8000 = 32 MP
        {"uid": "c", "bbox": [9500, 10_500, 9500, 10_500]},      # partly outside the region
    ]}
    units = core_units(snap, [0, 10_000, 0, 10_000], "cellpose_wholecell_fusion", 200)
    assert [u.core_uid for u in units] == ["a", "b", "c"] and [u.index for u in units] == [0, 1, 2]
    assert units[0].read_bbox == units[0].own_bbox == (100, 2100, 200, 2200)       # no halo
    assert units[2].read_bbox == (9500, 10_000, 9500, 10_000)                     # clipped
    ms = core_units(snap, [0, 10_000, 0, 10_000], "mesmer_whole_cell", 200)       # 35 MP cap: no split
    assert len(ms) == 3
    big = {"slide_shape": [20_000, 20_000], "cores": [{"uid": "x", "bbox": [0, 9000, 0, 9000]}]}   # 81 MP
    parts = core_units(big, [0, 20_000, 0, 20_000], "stardist_nuclei_expansion", 200)
    assert len(parts) == 2 and all(p.core_uid == "x" for p in parts)
    assert [p.part for p in parts] == [(0, 2), (1, 2)]
    assert all((p.read_bbox[1] - p.read_bbox[0]) * (p.read_bbox[3] - p.read_bbox[2]) <= CAP_PX["stardist"]
               for p in parts)
    assert parts[0].read_bbox[3] - parts[1].read_bbox[2] == 400                   # overlap inside the core
    assert parts[0].read_bbox[2] == 0 and parts[1].read_bbox[3] == 9000          # never beyond it
    assert split_grid((0, 9000, 0, 9000), 60_000_000, 200) == (1, 2)             # fewer rows on a tie
    assert engine_cap_px("hybrid_cellpose_stardist") == CAP_PX["stardist"]
    off = core_units(snap, [5000, 10_000, 0, 10_000], "cellpose_wholecell_fusion", 200)
    assert [u.core_uid for u in off] == ["b", "c"] and off[0].read_bbox == (0, 4000, 1000, 9000)   # region-local


# ── the run ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("loop", LOOPS)
@pytest.mark.parametrize("prefetch", [False, True])
def test_one_engine_call_per_core_and_none_outside(tmp_path, monkeypatch, loop, prefetch):
    lab = _labels(3)
    h, w = lab.shape
    calls = []
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, calls=calls, name=f"c{loop}{prefetch}")
    worker.seg_config["enable_tile_prefetch"] = prefetch
    snap = _snap(h, w, [(0, 50, 55, 113), (50, 97, 55, 113)])
    _with_regions(monkeypatch, worker, snap)
    progress = []
    worker.progress.connect(lambda a, b, m: progress.append(m))
    n = _go(worker, zp, loop)
    assert len(calls) == 2                                          # one per core, nothing else
    cells = _cells(worker, loop)
    ids = np.unique(cells[cells > 0])
    if loop == "segment_one_zarr":
        assert n == ids.size
    assert ids.size and list(ids) == list(range(1, ids.size + 1))
    assert not cells[:, :55].any()                                  # nothing outside the cores
    assert any("core 1 of 2" in m for m in progress) and any("core 2 of 2" in m for m in progress)
    prefix = "t" if loop == "segment_one_zarr" else ""
    meta = worker._step2_engine_meta()
    assert meta["layout"] == "cores" and meta["tile_strategy"] == "not used (cores run)"
    assert [u["core_uid"] for u in meta["units"][prefix or "whole_image"]] == ["core_0", "core_1"]
    assert "skipped_tiles_without_selected_cores" not in meta["regions"]


@pytest.mark.parametrize("loop", LOOPS)
def test_the_dapi_image_is_complete(tmp_path, monkeypatch, loop):
    import tifffile
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name=f"d{loop}")
    _with_regions(monkeypatch, worker, _snap(h, w, [(0, 40, 60, 100)]))
    _go(worker, zp, loop)
    name = "global_dapi_t.ome.tiff" if loop == "segment_one_zarr" else "global_dapi.ome.tiff"
    path = next(os.path.join(b, f) for b, _d, fs in os.walk(worker.output_dir) for f in fs if f == name)
    dapi = np.squeeze(tifffile.imread(path))
    import zarr
    want = np.asarray(zarr.open(zp, mode="r")[..., 1])
    assert dapi.shape == want.shape and (dapi > 0).all()           # gaps and outside the core too
    np.testing.assert_array_equal(dapi, want)


def test_a_skip_plan_is_ignored_and_recovery_is_refused(tmp_path, monkeypatch):
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, skip=_plan(3, 4, 8, [0, 1]), name="s")
    snap = _snap(h, w, [(0, 50, 55, 113)])
    worker._regions_request = {"snapshot": snap, "identity": snap["identity"], "input_kind": "x"}
    assert worker._tile_skip_set() == frozenset() and worker._skip_ignored
    worker2, _zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="r")
    worker2.recovery_npy_dir = str(tmp_path / "npy")
    worker2._regions_request = {"snapshot": snap, "identity": snap["identity"], "input_kind": "x"}
    errors = []
    worker2.error.connect(errors.append)
    worker2.run()
    assert errors and "Recovery Mode cannot run a run that follows Regions cores" in errors[0]
    assert not os.path.exists(os.path.join(str(tmp_path / "r"), "regions_snapshot.json"))   # before any output


def test_two_cores_whose_boxes_overlap_keep_each_object_once(tmp_path, monkeypatch):
    """Two L-shaped cores: their outlines do not overlap (as a snapshot
    requires), their boxes do (rows 50-60). An object wholly inside one core
    and whole in its window is kept, once; nothing is kept twice."""
    from block01.core.regions import analysis as RA
    from block01.core.regions.raster import AnalysisRaster
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="o")
    snap = _snap(h, w, [(0, 60, 55, 113)])
    a = [[55, 0], [112, 0], [112, 49], [84, 49], [84, 59], [55, 59]]          # x, y
    b = [[84, 50], [112, 50], [112, 96], [55, 96], [55, 60], [84, 60]]
    snap["cores"] = [dict(snap["cores"][0], uid="core_a", polygon=a), dict(snap["cores"][0], uid="core_b", polygon=b)]
    snap["identity"] = RA.identity(snap)
    _with_regions(monkeypatch, worker, snap)
    n = _go(worker, zp, "segment_one_zarr")
    cells = _cells(worker, "segment_one_zarr")
    plain, zp0 = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="p")
    _go(plain, zp0, "segment_one_zarr")
    before = _cells(plain, "segment_one_zarr")
    core = AnalysisRaster(snap).core_index((0, h, 0, w))
    windows = {1: (0, 60), 2: (50, 97)}                                       # rows of each core's box
    for i in np.unique(before[before > 0]):
        got = np.unique(cells[before == i])
        assert got.size == 1, i                                               # never split or doubled
        ys = np.nonzero(before == i)[0]
        k = np.unique(core[before == i])
        y0, y1 = windows.get(int(k[0]), (0, 0)) if k.size == 1 else (0, 0)
        whole = k.size == 1 and k[0] > 0 and (ys.min() > y0 or y0 == 0) and (ys.max() < y1 - 1 or y1 == h)
        if whole:
            assert got[0] > 0, i                                              # wholly in one core: kept
    assert len(np.unique(cells[cells > 0])) == n


def test_a_stop_during_the_dapi_fill_publishes_nothing(tmp_path, monkeypatch):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, 3, 4, 8, name="stop")
    _with_regions(monkeypatch, worker, _snap(h, w, [(0, 40, 60, 100)]))
    real = W._fill_dapi

    def fill(self, *a, **k):
        self._stop = True                                         # Stop pressed during the fill
        return real(self, *a, **k)
    monkeypatch.setattr(W, "_fill_dapi", fill)
    got = {}
    worker.job_finished.connect(lambda d, n: got.setdefault("n", n))
    worker.error.connect(lambda m: got.setdefault("error", m))
    worker.run()
    assert "n" not in got and got.get("error") == "Stopped by user."


# ── the page ─────────────────────────────────────────────────────────────

def test_the_page_drops_the_grid_for_a_cores_run(monkeypatch):
    from block01.qt.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from block01.core.regions import display as RD
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    items = [{"id": "core_a#0", "polygon": [[0, 0], [9, 0], [9, 9]], "color": RD.GREEN},
             {"id": "core_b#0", "polygon": [[20, 0], [29, 0], [29, 9]], "color": RD.GREEN},
             {"id": "core_c#0", "polygon": [[40, 0], [49, 0], [49, 9]], "color": RD.YELLOW}]
    page.saved_core_items = lambda: items
    assert page.cores_run_count() == 2
    page._apply_cores_layout()
    assert not page._rows_spin.isEnabled() and not page._cols_spin.isEnabled()
    assert not page._auto_tile_strategy.isEnabled() and not page._skip_empty_cb.isEnabled()
    assert page._overlap_spin.isEnabled()
    assert "core by core (2 cores)" in page._tile_suggestion_lbl.text()
    assert page.tile_items() == [] and page._stardist_grid_ok_for_run()
    page.saved_core_items = lambda: [it for it in items if it["color"] != RD.GREEN]
    page._apply_cores_layout()                                     # no run core: the grid comes back
    assert page._rows_spin.isEnabled() and page._cores_preview == 0
    page.deleteLater()


def _page_with(items):
    from block01.qt.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    page.saved_core_items = lambda: items
    return page


def _three():
    from block01.core.regions import display as RD
    return [{"id": "core_a#0", "polygon": [[0, 0], [9, 0], [9, 9]], "color": RD.GREEN},
            {"id": "core_b#0", "polygon": [[20, 0], [29, 0], [29, 9]], "color": RD.GREEN},
            {"id": "core_c#0", "polygon": [[40, 0], [49, 0], [49, 9]], "color": RD.YELLOW}]


def test_a_newer_save_after_a_finished_run_is_what_step2_follows():
    """15A acceptance (2026-11-12): a run froze 1 core; Regions then saved
    with every core ticked: once that run has ended, Step2 shows and counts
    the Save, not the old run's snapshot."""
    from block01.core.regions import display as RD
    old = {"display_cores": [{"uid": "core_a", "polygons": [[[0, 0], [9, 0], [9, 9]]], "run": True},
                             {"uid": "core_b", "polygons": [[[20, 0], [29, 0], [29, 9]]], "run": False}]}
    page = _page_with([dict(it, color=RD.GREEN) for it in _three()])
    page._run_core_snapshot = old
    page._run_active = True
    assert page.cores_run_count() == 1                             # the run on: its frozen cores
    page._run_active = False
    assert page.cores_run_count() == 3                             # ended: the newer Save
    page.deleteLater()


def test_the_core_being_segmented_is_highlighted_then_turns_green():
    """15A acceptance (2026-11-12): Step2's cores in the tile scheme; the
    core under segmentation like the running tile (with a fill); a core
    whose units are all done gets the done tile's green border, kept after
    the run; a later progress index (the DAPI fill) lights nothing again; a
    new run starts grey, with its own worker's units."""
    import types
    page = _page_with(_three())
    page._cores_preview = 2
    look = lambda: {it["id"].split("#")[0]: (it["color"], it.get("fill")) for it in page._core_items()}   # noqa: E731
    idle, run, done = page.TILE_COLOURS['idle'], page.TILE_COLOURS['running'], page.TILE_COLOURS['done']
    purple = page.SKIP_PURPLE
    assert look() == {"core_a": (idle, None), "core_b": (idle, None), "core_c": (purple, None)}
    page._run_view_live = lambda: True
    units = {"": [{"index": 0, "core_uid": "core_a"}, {"index": 1, "core_uid": "core_b"},
                  {"index": 2, "core_uid": "core_b"}]}
    page._worker = types.SimpleNamespace(_units_meta=units)          # the run before
    page._begin_run_progress(1, 1)
    page._on_progress(0, 3, "core 1 of 2")
    assert look()["core_a"] == (idle, None)                          # not the old worker's units
    page._worker = types.SimpleNamespace(_units_meta=units)          # this run's worker
    page._on_progress(0, 3, "core 1 of 2")
    assert look()["core_a"] == (run, page.CORE_RUNNING_FILL) and look()["core_b"] == (idle, None)
    page._on_tile_done(0, 3, 5)
    page._on_progress(1, 3, "core 2 of 2, part 1 of 2")
    assert look()["core_a"] == (done, None) and look()["core_b"][0] == run
    page._on_tile_done(1, 3, 5)
    page._on_progress(2, 3, "core 2 of 2, part 2 of 2")
    assert look()["core_b"][0] == run                                # its second part
    page._on_tile_done(2, 3, 5)
    page._on_progress(1, 4, "DAPI image 2/4")                        # the DAPI fill reuses the indices
    assert look() == {"core_a": (done, None), "core_b": (done, None), "core_c": (purple, None)}
    page._run_active = False                                         # the run ended: green stays
    assert look() == {"core_a": (done, None), "core_b": (done, None), "core_c": (purple, None)}
    page._begin_run_progress(1, 1)                                   # a new run: grey again
    assert look() == {"core_a": (idle, None), "core_b": (idle, None), "core_c": (purple, None)}
    page.deleteLater()
