"""F4-3 (docs/f4_regions_downstream_plan.md E / E.8): Step2 with the frozen
Regions snapshot of the Step1 hand-over -- tiles with no selected-core pixel
are skipped whatever the tissue plan says, the Regions rules drop objects
after the seams are decided and before they take an id (ids stay 1..N), the
written cells' facts are kept in id order, and a run without Regions is
exactly the old run. Synthetic labels (the fake model of
`test_step2_ownership_move`), no engine.
"""

import logging
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from block01.core import seam_merge  # noqa: E402
from block01.core.regions.raster import AnalysisRaster  # noqa: E402
from test_step2_ownership_move import _labels  # noqa: E402
from test_step2_skip_empty_tiles import LOOPS, _outputs, _run, _worker  # noqa: E402

ROWS, COLS, OVERLAP = 3, 4, 8


def _snapshot(h, w, core_box=(0, 97, 60, 113), excl_box=None, channels="all"):
    """One selected core (a rectangle) on an h x w image; optionally one
    exclusion -- segmentation-invalid unless `channels` names others."""
    y0, y1, x0, x1 = core_box
    snap = {"snapshot_version": 1, "mode": "tma", "slide_id": "slide_0000000000000000",
            "slide_shape": [h, w], "parent": {"region_id": None, "bbox_fullres": [0, h, 0, w],
                                              "polygon_fullres": None},
            "cores": [{"position_key": "tmapos_a", "polygon": [[x0, y0], [x1 - 1, y0], [x1 - 1, y1 - 1],
                                                              [x0, y1 - 1]]}],
            "exclusions": [], "map_sheet": None, "seg_channels": [], "policies": {}}
    if excl_box is not None:
        ey0, ey1, ex0, ex1 = excl_box
        snap["exclusions"].append({"uid": "excl_a", "channels": channels,
                                   "polygon": [[ex0, ey0], [ex1 - 1, ey0], [ex1 - 1, ey1 - 1], [ex0, ey1 - 1]],
                                   "seg_invalid": channels == "all"})
    return _identified(snap)


def _identified(snap):
    """`snap` with its real identity (the worker checks it, 9.2)."""
    from block01.core.regions import analysis as RA
    for c in snap["cores"]:
        for k in ("donor_block_id", "tma_unique_sample_id", "annotation", "qc"):
            c.setdefault(k, "")
    snap["identity"] = RA.identity(snap)
    return snap


def _regions(snap):
    meas = [i for i, e in enumerate(snap["exclusions"]) if not e["seg_invalid"]]
    return {"snapshot": snap, "identity": "id", "raster": AnalysisRaster(snap),
            "bit": {ex: b for b, ex in enumerate(meas)},
            "measurement_exclusions": [snap["exclusions"][ex]["uid"] for ex in meas]}


def _with_regions(monkeypatch, worker, snap):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    regions = _regions(snap)

    def begin(self):
        from block01.core.regions import analysis as RA
        RA.save_snapshot(os.path.join(self.output_dir, self.REGIONS_SNAPSHOT_FILE), snap)
        self._regions_skipped = {}
        return regions
    monkeypatch.setattr(W, "_regions_begin", begin)
    worker._regions = begin(worker)
    return worker


def _cells(worker, loop):
    p = os.path.join(worker.output_dir, "global_mask_t.zarr" if loop == "segment_one_zarr"
                     else "global_mask.zarr")
    if not os.path.exists(p):
        p = next(os.path.join(b, "") for b, d, f in os.walk(worker.output_dir)
                 if b.endswith(".zarr") and "global_mask" in os.path.basename(b))
    return np.asarray(zarr.open(p, mode="r"))


@pytest.mark.parametrize("loop", LOOPS)
def test_only_cells_in_selected_cores_are_kept_with_dense_ids_and_their_facts(tmp_path, monkeypatch, loop):
    lab = _labels(3)                                    # tissue in columns 60..112 only
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="on")
    _with_regions(monkeypatch, worker, _snapshot(h, w, core_box=(0, 50, 60, 113), excl_box=(30, 50, 90, 113)))
    skipped = []
    worker.tile_skipped.connect(lambda i, n: skipped.append(i))
    n = _run(worker, zp, loop)
    cells = _cells(worker, loop)
    ids = np.unique(cells[cells > 0])
    assert n == ids.size and list(ids) == list(range(1, ids.size + 1))       # dense 1..N
    core = np.zeros_like(cells, bool)
    core[0:50, 60:113] = True
    for i in ids:                                                           # each mostly in the core
        assert core[cells == i].mean() > 0.5, i
    assert not cells[30:50, 90:113].any()                                   # nothing on the fold
    assert skipped and all(i in worker._regions_skipped["t" if loop == "segment_one_zarr" else ""]
                           for i in skipped)                                # counted as Regions skips
    prefix = "_t" if loop == "segment_one_zarr" else ""
    facts = np.load(os.path.join(worker.output_dir, f"global_cell_regions{prefix}.npz"),
                    allow_pickle=False)                     # F4-4: no object arrays
    assert int(facts["facts_version"]) == 2
    assert facts["core"].shape == (n,) and (facts["core"] == 0).all()
    assert os.path.exists(os.path.join(worker.output_dir, "regions_snapshot.json"))
    assert not [f for f in os.listdir(worker.output_dir) if f.endswith(".partial")]


@pytest.mark.parametrize("loop", LOOPS)
def test_a_run_without_regions_is_the_old_run(tmp_path, monkeypatch, loop):
    lab = _labels(3)
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="plain")
    _run(worker, zp, loop)
    assert getattr(worker, "_regions", None) is None
    assert "regions" not in worker._step2_engine_meta()
    assert not [f for f in os.listdir(worker.output_dir) if "regions" in f]
    store = worker._label_store("c", "", "", 3, None, lab.shape)
    assert "regions" not in store


def test_the_kept_cells_are_exactly_the_old_run_minus_the_rejected_ones(tmp_path, monkeypatch):
    lab = _labels(3)
    h, w = lab.shape
    plain, zp0 = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="plain")
    _run(plain, zp0, "segment_one_zarr")
    before = _cells(plain, "segment_one_zarr")
    on, zp1 = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="on")
    _with_regions(monkeypatch, on, _snapshot(h, w, core_box=(0, 97, 60, 113)))   # the whole tissue
    _run(on, zp1, "segment_one_zarr")
    np.testing.assert_array_equal(_cells(on, "segment_one_zarr"), before)       # everything in the core


def test_the_merger_drops_rejected_winners_before_their_id_and_keeps_facts_in_id_order(tmp_path):
    tiles = [seam_merge.Tile(0, (0, 40, 0, 40), (0, 40, 0, 40))]
    local = np.zeros((40, 40), np.uint32)
    local[2:6, 2:6], local[10:14, 10:14], local[20:24, 20:24] = 1, 2, 3
    rows = {1: np.array([1, 0, 0, 1.0, 2, 0.0]), 2: np.array([0, 3, -1, 1.0, 0, 0.0]),
            3: np.array([1, 0, 0, 0.75, 1, 5.0])}
    m = seam_merge.SeamMerger((40, 40), tiles, str(tmp_path / "spill"), judge=lambda t, l: rows)
    cells = np.zeros((40, 40), np.uint32)
    m.add_tile(tiles[0], local, cells)
    m.finish(cells)
    assert m.n_cells == 2 and set(np.unique(cells)) == {0, 1, 2}
    assert m.stats["regions_dropped_exclusion"] == 1
    assert m.facts.shape == (2, seam_merge.VERDICT_COLS) and m.facts[1, 3] == 0.75 and m.facts[1, 5] == 5
    seam_merge.validate(cells, n_cells=m.n_cells)
    plain = seam_merge.SeamMerger((40, 40), tiles, str(tmp_path / "spill2"))
    assert not any(k.startswith("regions") for k in plain.stats)           # the old record unchanged


def test_a_seam_candidates_verdict_survives_the_spill(tmp_path):
    tiles = [seam_merge.Tile(0, (0, 40, 0, 30), (0, 40, 0, 20)),
             seam_merge.Tile(1, (0, 40, 10, 40), (0, 40, 20, 40))]
    local0 = np.zeros((40, 30), np.uint32)
    local0[5:10, 14:20] = 1                                   # in both read windows: a seam candidate
    local1 = np.zeros((40, 30), np.uint32)
    local1[5:10, 4:10] = 1
    keep = np.array([1, 0, 0, 0.5, 4, 0.0])
    drop = np.array([0, 1, -1, 0.1, 0, 0.0])
    m = seam_merge.SeamMerger((40, 40), tiles, str(tmp_path / "spill"),
                              judge=lambda t, l: {1: keep if t.index == 0 else drop})
    cells = np.zeros((40, 40), np.uint32)
    m.add_tile(tiles[0], local0, cells)
    m.add_tile(tiles[1], local1, cells)
    m.finish(cells)
    # one object, two versions; whichever wins carries its own spilled verdict
    assert m.n_cells + m.stats["regions_dropped_no_core"] == 1
    if m.n_cells:
        assert m.facts[0, 4] == 4


def test_a_snapshot_for_another_region_or_input_is_refused(tmp_path, monkeypatch):
    from block01.utils.tile_scheduler import TileScheduler
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    begin = W._regions_begin                                     # the real one, before any patch
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="x")
    snap = _snapshot(h, w)
    snap["parent"]["bbox_fullres"] = [0, h - 1, 0, w]
    worker._regions, worker._regions_skipped = _regions(snap), {}
    with pytest.raises(RuntimeError, match="frozen for the region"):
        worker._regions_for("", TileScheduler(h, w, ROWS, COLS, OVERLAP).tiles, h, w)
    _identified(snap)
    worker.method = "cellpose_nuclei_dapi"
    worker._contract = {"method": "cellpose_nuclei_dapi",
                        "regions": {"snapshot": snap, "identity": snap["identity"],
                                    "input_kind": "fusion_fusion_nucleus"}}
    with pytest.raises(RuntimeError, match="input"):
        begin(worker)
    worker.method = worker._contract["method"] = "cellpose_wholecell_fusion"
    worker.rois = [{"name": "a"}, {"name": "b"}]
    with pytest.raises(RuntimeError, match="one region"):
        begin(worker)
    worker.rois = None
    worker._contract = None
    assert begin(worker) is None                                 # no hand-over: no Regions


def test_nuclei_of_rejected_cells_are_counted_so_predicted_still_adds_up(tmp_path):
    tiles = [seam_merge.Tile(0, (0, 40, 0, 40), (0, 40, 0, 40))]
    cellsl = np.zeros((40, 40), np.uint32)
    cellsl[2:8, 2:8], cellsl[20:26, 20:26] = 1, 2
    nucl = np.zeros((40, 40), np.uint32)
    nucl[3:5, 3:5], nucl[21:23, 21:23], nucl[24:25, 24:25] = 1, 2, 3
    cell_of = np.array([0, 1, 2, 2], np.uint32)                 # nucleus -> local cell
    rows = {1: np.array([1, 0, 0, 1.0, 2, 0.0]), 2: np.array([0, 1, -1, 0.2, 0, 0.0])}
    table = []
    m = seam_merge.SeamMerger((40, 40), tiles, str(tmp_path / "spill"), with_nuclei=True,
                              table_sink=table.append, judge=lambda t, l: rows)
    cells, nuclei = np.zeros((40, 40), np.uint32), np.zeros((40, 40), np.uint32)
    m.add_tile(tiles[0], cellsl, cells, nuclei, nucl, cell_of)
    m.finish(cells, nuclei)
    assert m.n_cells == 1 and m.n_nuclei == 1
    assert m.stats["nuclei_dropped_regions"] == 2                # the rejected cell's two nuclei
    assert not nuclei[20:26, 20:26].any()


# ── review fixes (codex_f4_step2_1.out) ─────────────────────────────────

def test_regions_are_refused_when_the_slide_cannot_be_checked_or_differs(tmp_path, monkeypatch):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    h, w = lab.shape
    worker, _zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="s")
    snap = _snapshot(h, w)
    worker.method = "cellpose_wholecell_fusion"
    worker._contract = {"method": "cellpose_wholecell_fusion",
                        "regions": {"snapshot": snap, "identity": snap["identity"],
                                    "input_kind": "fusion_fusion_nucleus"}}
    worker.roi_manifest = {"source_ome": str(tmp_path / "gone.ome.tiff")}
    with pytest.raises(RuntimeError, match="cannot be read"):
        W._regions_begin(worker)
    monkeypatch.setattr(W, "_workspace_slide_id", lambda self: "slide_ffffffffffffffff")
    with pytest.raises(RuntimeError, match="is for slide"):
        W._regions_begin(worker)
    monkeypatch.setattr(W, "_workspace_slide_id", lambda self: snap["slide_id"])
    snap["exclusions"] = [{"uid": f"e{k}", "channels": ["CD8"], "seg_invalid": False,
                           "polygon": [[0, 0], [3, 0], [3, 3], [0, 3]]} for k in range(53)]
    worker._contract["regions"]["identity"] = _identified(snap)["identity"]
    with pytest.raises(RuntimeError, match="at most 52"):
        W._regions_begin(worker)
    snap["exclusions"] = snap["exclusions"][:52]
    worker._contract["regions"]["identity"] = _identified(snap)["identity"]
    reg = W._regions_begin(worker)
    assert reg["bit"][51] == 51 and os.path.exists(os.path.join(worker.output_dir, "regions_snapshot.json"))


def test_a_stop_after_the_last_tile_publishes_nothing(tmp_path, monkeypatch):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    h, w = lab.shape
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="stop")
    _with_regions(monkeypatch, worker, _snapshot(h, w))
    real = W._close_tile_scheduler
    monkeypatch.setattr(W, "_close_tile_scheduler",                      # Stop right after the last tile
                        staticmethod(lambda sch: (real(sch), setattr(worker, "_stop", True))))
    got = {}
    worker.finished.connect(lambda d, n: got.setdefault("n", n))
    worker.error.connect(lambda m: got.setdefault("error", m))
    worker.run()
    assert "n" not in got and "Stopped" in got.get("error", "")
    assert not os.path.exists(os.path.join(worker.output_dir, "segmentation_meta.json"))
    assert not [f for f in os.listdir(worker.output_dir) if "cell_regions" in f]


def test_a_rejected_seam_winner_takes_no_id_and_its_nuclei_count_once(tmp_path):
    tiles = [seam_merge.Tile(0, (0, 40, 0, 30), (0, 40, 0, 20)),
             seam_merge.Tile(1, (0, 40, 10, 40), (0, 40, 20, 40))]
    l0 = np.zeros((40, 30), np.uint32)
    l0[5:10, 14:20] = 1
    l1 = np.zeros((40, 30), np.uint32)
    l1[5:10, 4:10] = 1
    n0 = np.zeros((40, 30), np.uint32)
    n0[6:8, 15:17] = 1
    n1 = np.zeros((40, 30), np.uint32)
    n1[6:8, 5:7] = 1
    drop = np.array([0, 3, -1, 1.0, 0, 0.0])
    m = seam_merge.SeamMerger((40, 40), tiles, str(tmp_path / "spill"), with_nuclei=True,
                              table_sink=lambda b: None, judge=lambda t, l: {1: drop})
    cells, nuclei = np.zeros((40, 40), np.uint32), np.zeros((40, 40), np.uint32)
    cell_of = np.array([0, 1], np.uint32)
    m.add_tile(tiles[0], l0, cells, nuclei, n0, cell_of)
    m.add_tile(tiles[1], l1, cells, nuclei, n1, cell_of)
    m.finish(cells, nuclei)
    assert m.n_cells == 0 and m.stats["regions_dropped_exclusion"] == 1     # the winner only
    assert m.stats["nuclei_dropped_regions"] == 1 and not cells.any()


def test_a_roi_off_the_origin_is_judged_in_slide_coordinates(tmp_path, monkeypatch):
    from block01.utils.tile_scheduler import TileScheduler
    lab = _labels(3)
    h, w = lab.shape
    worker, _zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="o")
    oy, ox = 1000, 2000
    snap = _snapshot(oy + h + 10, ox + w + 10, core_box=(oy, oy + h, ox + 60, ox + w))
    snap["parent"]["bbox_fullres"] = [oy, oy + h, ox, ox + w]
    worker._regions, worker._regions_skipped = _regions(snap), {}
    tiles = TileScheduler(h, w, ROWS, COLS, OVERLAP).tiles
    judge, skip = worker._regions_for("r", tiles, h, w, bbox=[oy, oy + h, ox, ox + w])
    assert skip and all(t.read_bbox[3] <= 60 for k, t in enumerate(tiles) if k in skip)
    with pytest.raises(RuntimeError, match="does not match its input"):
        worker._regions_for("r", tiles, h, w, bbox=[oy, oy + h + 1, ox, ox + w])
    local = np.zeros((h, w), np.uint32)
    local[10:14, 80:84], local[10:14, 10:14] = 1, 2
    rows = judge(seam_merge.Tile(0, (0, h, 0, w), (0, h, 0, w)), local)
    assert rows[1][0] == 1 and rows[1][2] == 0 and rows[2][0] == 0          # in / out of the core


def test_the_hand_overs_regions_no_longer_tie_the_fused_input(tmp_path, monkeypatch):
    """§11 (user ruling 2026-10-19): the hand-over's own Regions are not used,
    so a fused input made with other Fusion settings is not refused for them
    (the cores are frozen at Run with the input's own settings)."""
    import json
    import types
    from PyQt5.QtWidgets import QMessageBox
    from block01.core import preseg_contract
    from block01.ui.step2_page import Step2Page
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    block = {"method": "cellpose_nuclei_dapi", "halo_px": 8, "fusion_settings_hash": "fh",
             "regions": {"identity": "id"}, "params": {}}
    monkeypatch.setattr(preseg_contract, "validate", lambda cfg: block)
    monkeypatch.setattr(preseg_contract, "mismatches", lambda b, e: [])
    run = tmp_path / "fuse_run"
    run.mkdir()
    (run / "params.json").write_text(json.dumps({"fusion_settings": {"hash": "other"}}))
    fake = types.SimpleNamespace(_overlap_spin=types.SimpleNamespace(value=lambda: 8),
                                 selected_fuse_run=lambda: str(run))
    assert Step2Page._check_preseg_contract(fake, {"method": "cellpose_nuclei_dapi"}) is True
    assert not shown
    fake.selected_fuse_run = lambda: ""
    assert Step2Page._check_preseg_contract(fake, {"method": "cellpose_nuclei_dapi"}) is True


def test_provenance_depends_on_the_frozen_regions_save_or_lists_it_unresolved(tmp_path, monkeypatch):
    from block01.core import provenance as prov
    seen = {}
    monkeypatch.setattr(prov, "find_workspace", lambda d: (str(tmp_path), str(tmp_path / "ws")))
    monkeypatch.setattr(prov, "_load", lambda p: {"source_ome": "x"})
    monkeypatch.setattr(prov, "ensure_raw_slide", lambda pd, s: "slide_1")
    monkeypatch.setattr(prov, "workspace_regions", lambda ws, sid: {})
    monkeypatch.setattr(prov, "load_entries", lambda pd: [])
    monkeypatch.setattr(prov, "lookup", lambda pd, kind, loc, tok: "reg_layout_1" if tok == "sha_a" else None)
    monkeypatch.setattr(prov, "safe_register", lambda *a, **k: seen.update(k) or "id")
    snap = {"regions": {"content_sha256": "sha_a"}, "cores": [{"position_key": "tmapos_a"}]}
    meta = {"run_id": "segment_1", "rois": [{"roi_name": "", "bbox_fullres": None}],
            "seg_config": {"preseg_contract": {"method": "m", "regions": {"identity": "id", "input_kind": "k",
                                                                          "snapshot": snap}}}}
    prov.register_segmentation_run(str(tmp_path / "run"), meta)
    assert "reg_layout_1" in seen["depends_on"]
    assert seen["parameters"]["regions"]["position_keys"] == ["tmapos_a"]
    assert "snapshot" not in seen["parameters"]["preseg_contract"]["regions"]
    snap["regions"]["content_sha256"] = "sha_b"                              # a Save no longer registered
    prov.register_segmentation_run(str(tmp_path / "run"), meta)
    assert {"role": "regions_layout", "content_sha256": "sha_b"} in seen["unresolved_inputs"]


@pytest.mark.parametrize("loop", LOOPS)
def test_regions_and_a_tissue_plan_skip_together_and_are_counted_apart(tmp_path, monkeypatch, loop):
    from test_step2_skip_empty_tiles import _empty_tiles, _plan
    lab = _labels(3)
    h, w = lab.shape
    empty = _empty_tiles(lab, ROWS, COLS, OVERLAP)
    worker, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP, name="both",
                         skip=_plan(ROWS, COLS, OVERLAP, empty))
    worker.seg_config["enable_tile_prefetch"] = True
    _with_regions(monkeypatch, worker, _snapshot(h, w, core_box=(0, 40, 60, 113)))
    skipped = []
    worker.tile_skipped.connect(lambda i, n: skipped.append(i))
    _run(worker, zp, loop)
    regions_only = worker._regions_skipped["t" if loop == "segment_one_zarr" else ""]
    assert set(skipped) == set(empty) | set(regions_only) and regions_only
    assert not set(regions_only) & set(empty)                      # a tile in both counts as tissue
    assert sorted(worker._skipped_tiles) == sorted(empty)
