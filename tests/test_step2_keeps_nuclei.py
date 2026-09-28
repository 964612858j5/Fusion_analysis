"""Block N2: every method that computes nuclei keeps them (LabelStore).

nuclear-guided runs on a STAND-IN engine (no Mesmer model on this machine):
a fake engine process slices two independently numbered global truths --
cells and nuclei -- to each task's read window. The truth holds every case:
nuclei wholly inside a cell (kept), over two cells, over a cell and
background, on background only (dropped, each counted once), a cell with two
nuclei (both kept), nucleus ids larger than the cell count, and cells across
the 2 x 2 tile seams (kept once, by the tile that owns them).

  * the LabelStore contract: nuclei 1..M unique; the nucleus -> cell table
    has M + 1 entries; every nucleus pixel lies in the cell the table names;
    each truth nucleus that is wholly in a cell appears exactly once with
    exactly its pixels; dropped ones never; `label_store` complete with the
    right counts and retained fraction; zarr attributes; no nucleus OME-TIFF,
    no nucleus `.dat`, no `*.partial`; the nucleus pyramid passes 4a's check
    and 4a resolves both masks from the store;
  * the cells are exactly what the seam merge makes of the cell truth
    (block N3b), its parameters and counts recorded;
  * ROI and whole-image modes;
  * an ordinary I/O failure while the table is written: the run fails, is
    not registered, and leaves no partial; a Stop likewise;
  * a run recovered from .npy has no nuclei and says why;
  * the nucleus write does not materialise the whole image;
  * expansion on the REAL Cellpose engine: the same contract.

Synthetic data in the test's temporary directory only.
"""

import json
import os
import re
import tracemalloc

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from block01.core import label_ownership, label_pyramid, step3_masks  # noqa: E402
from block01.seg_runner import protocol as runner_protocol  # noqa: E402
from block01.utils.tile_scheduler import TileScheduler  # noqa: E402

import test_step2_runner_path as rp  # noqa: E402

H, W, ROWS, COLS, HALO = rp.H, rp.W, rp.ROWS, rp.COLS, rp.HALO
GUIDED = "mesmer_nuclear_guided"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the stand-in engine ──────────────────────────────────────────────────

def _truth():
    """Cells: 16 x 16 squares with shuffled ids (some background). Nuclei:
    own ids from 5000 on, placed to hit every case, several across seams."""
    rng = np.random.default_rng(7)
    grid = rng.permutation(np.arange(1, 14 * 11 + 1)).reshape(11, 14)
    grid[rng.random(grid.shape) < 0.15] = 0                       # some background squares
    cells = np.kron(grid, np.ones((16, 16), int))[:H, :W].astype(np.uint32)
    nuclei = np.zeros((H, W), np.uint32)
    nid = 5000
    expect = {"inside": [], "multiple_cells": [], "partial_background": [], "outside_cells": []}
    for gy in range(11):
        for gx in range(13):
            y, x = gy * 16, gx * 16
            if y + 16 > H or x + 16 > W:
                continue
            here = int(grid[gy, gx])
            # a straddler needs the square to its right INSIDE the image
            right = int(grid[gy, gx + 1]) if x + 32 <= W else None
            if here == 0:
                nuclei[y + 6:y + 10, x + 6:x + 10] = nid
                expect["outside_cells"].append(nid)
            elif (gy + gx) % 5 == 0 and right not in (None, 0, here):
                nuclei[y + 6:y + 10, x + 13:x + 19] = nid                 # over two cells
                expect["multiple_cells"].append(nid)
            elif (gy + gx) % 5 == 1 and right == 0:
                nuclei[y + 6:y + 10, x + 13:x + 19] = nid                 # cell + background
                expect["partial_background"].append(nid)
            else:
                nuclei[y + 3:y + 7, x + 3:x + 7] = nid
                expect["inside"].append(nid)
                if (gy * 13 + gx) % 7 == 0:                                # a second nucleus
                    nid += 1
                    nuclei[y + 9:y + 13, x + 9:x + 13] = nid
                    expect["inside"].append(nid)
            nid += 1
    return cells, nuclei, expect


class _FakeEngine:
    """The engine process's public surface, answering from the truth."""

    def __init__(self, truth, stop_after=None, worker=None):
        self.cells, self.nuclei = truth
        # a task is "tile_<index>" (whole image) or "tile_<ROI>_<index>": the
        # trailing index is the scheduler's tile
        self.tiles = TileScheduler(H, W, ROWS, COLS, HALO).tiles
        self.states = {}
        self.proc = type("P", (), {"pid": os.getpid(), "poll": lambda self: None})()
        self.stop_after = stop_after
        self.worker = worker
        self.served = 0

    def __call__(self, engine, log_path=None, cpu_only=False):
        return self

    def start(self):
        return {"identity": {"engine": "mesmer"}, "device": "cpu"}

    def run(self, tasks, on_settle=None):
        out = {}
        for task in tasks:
            tid = task["task_id"]
            y0, y1, x0, x1 = self.tiles[int(re.search(r"(\d+)$", tid).group(1))].read_bbox
            rec = {}
            for kind, arr in (("cell", self.cells), ("nucleus", self.nuclei)):
                path = os.path.join(task["out_dir"], f"{tid}.{kind}.npy")
                np.save(path, arr[y0:y1, x0:x1])
                rec[kind] = {"status": runner_protocol.OK, "path": path}
            rec_path = os.path.join(task["out_dir"], f"{tid}.record.json")
            with open(rec_path, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            self.states[tid] = {"state": runner_protocol.OK, "detail": rec_path}
            out[tid] = runner_protocol.OK
            self.served += 1
            if self.stop_after is not None and self.served >= self.stop_after:
                self.worker._stop = True
        return out

    def close(self):
        return None

    def terminate(self):
        return None


def _guided_worker(tmp_path, monkeypatch, rois, **fake):
    from block01.workers import segment_merge_worker as smw
    truth = _truth()
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()),
                        dict(rp.PARAMS[GUIDED], method=GUIDED), rois=rois)
    engine = _FakeEngine(truth[:2], worker=worker, **fake)
    monkeypatch.setattr(smw, "EngineProcess", engine)
    monkeypatch.setattr(smw.SegmentMergeWorker, "_validate_mesmer_config",
                        lambda self, *a, **k: None)
    return worker, truth


def _cells_oracle(cells):
    """Block N3b: what the seam merge (the pure functions' reference) makes
    of the cell truth, window by window, in Step2's id order."""
    from block01.core import seam_merge as sm
    from test_seam_merge import reference_merge
    tiles, locals_ = [], []
    for k, tile in enumerate(TileScheduler(H, W, ROWS, COLS, HALO).tiles):
        ry0, ry1, rx0, rx1 = tile.read_bbox
        tiles.append(sm.Tile(k, tuple(tile.read_bbox), tuple(tile.own_bbox)))
        locals_.append(cells[ry0:ry1, rx0:rx1])
    return reference_merge(tiles, locals_, (H, W))


def _meta(worker, name="segmentation_meta.json"):
    with open(os.path.join(worker.output_dir, name), encoding="utf-8") as f:
        return json.load(f)


def _assert_label_store(worker, sfx, truth, store):
    import zarr
    cells_truth, nuclei_truth, expect = truth
    cell = np.asarray(zarr.open(os.path.join(worker.output_dir, f"global_mask{sfx}.zarr"), "r"))
    nz = zarr.open(os.path.join(worker.output_dir, f"global_nuclei_mask{sfx}.zarr"), "r")
    tz = zarr.open(os.path.join(worker.output_dir, f"global_nuclei_cell{sfx}.zarr"), "r")
    nuc, table = np.asarray(nz), np.asarray(tz)
    # the cells are the seam merge's (block N3b), ids included
    np.testing.assert_array_equal(cell, _cells_oracle(cells_truth))
    # nuclei 1..M unique, the table M + 1, every pixel in the named cell
    ids = np.unique(nuc[nuc > 0])
    m = len(expect["inside"])
    assert list(ids) == list(range(1, m + 1))
    assert table.shape == (m + 1,) and table[0] == 0 and table.dtype == np.uint32
    assert (cell[nuc > 0] == table[nuc[nuc > 0]]).all()
    # each truth nucleus inside a cell: once, with exactly its pixels
    for n in expect["inside"]:
        got = np.unique(nuc[nuclei_truth == n])
        assert got.size == 1 and got[0] > 0, n
        assert ((nuc == got[0]) == (nuclei_truth == n)).all(), n
    for reason in ("multiple_cells", "partial_background", "outside_cells"):
        for n in expect[reason]:
            assert not nuc[nuclei_truth == n].any(), (reason, n)
    # two nuclei of one cell: both there, both pointing at it
    per_cell = np.bincount(table[1:])
    assert (per_cell >= 2).any()
    # the label store
    assert store["complete"] is True and store["version"] == 1
    assert store["relation"] == {"nucleus_to_cell": "many_to_one"}
    info = store["nuclei"]
    assert info["kept"] == m
    assert info["dropped"] == dict({k: len(expect[k]) for k in
                                    ("multiple_cells", "partial_background", "outside_cells")},
                                   seam_conflict=0, oversized_cell=0)   # blocks N3b, N4
    assert info["predicted"] == m + sum(info["dropped"].values())
    assert info["retained_fraction"] == pytest.approx(m / info["predicted"])
    assert store["nucleus"]["n_objects"] == m and store["nucleus_to_cell"]["length"] == m + 1
    assert store["cell"]["n_objects"] == int(cell.max())
    # block N3b: the seam merge's parameters and counts are recorded
    assert store["seam_merge"] == {"version": 1, "duplicate_overlap_threshold": 0.5,
                                   "minimum_writable_fraction": 0.5}
    assert store["seam_reconciliation"]["nuclei_dropped_seam_conflict"] == 0
    assert nz.attrs["kind"] == "nucleus" and tz.attrs["kind"] == "nucleus_to_cell"
    # no OME-TIFF of the nuclei, no temporary memmap, no partial
    names = os.listdir(worker.output_dir)
    assert not [n for n in names if n.startswith("global_nuclei_mask") and
                (n.endswith(".ome.tiff") or n.endswith(".dat"))]
    assert not [n for n in names if n.endswith(".partial")]
    return cell, nuc


def _assert_step3_reads_it(worker, sfx, roi_name):
    """4a resolves both masks from the label store; the nucleus pyramid is whole."""
    shapes = [(H, W), (H // 4, W // 4)]
    rdir = worker.output_dir
    for n in ("global_mask", "global_nuclei_mask"):
        path = os.path.join(rdir, f"{n}{sfx}.zarr")
        dest = label_pyramid.pyramid_path_for(path)
        if not os.path.exists(dest):                      # this rig has no slide pyramid
            label_pyramid.build(path, dest, shapes, (0, H, 0, W),
                                "nucleus" if "nuclei" in n else "cell")
    meta = _meta(worker)
    for roi in meta.get("rois") or []:
        if not roi.get("bbox_fullres"):
            roi["bbox_fullres"] = [0, H, 0, W]           # this rig's ROI names no bbox
    run = step3_masks.Run(run_id="r", method=GUIDED, created_at="", run_dir=rdir,
                          meta=meta, meta_path="")
    got = step3_masks.resolve_masks(run, roi_name, (0, H, 0, W), shapes)
    assert got["cell"] is not None and got["nucleus"] is not None, got["reasons"]
    assert os.path.basename(got["nucleus"].mask_path) == f"global_nuclei_mask{sfx}.zarr"
    assert got["nucleus"].pyramid is not None, got["nucleus"].pyramid_reason


@pytest.mark.parametrize("loop", ["roi", "full"])
def test_nuclear_guided_keeps_paired_nuclei(app, tmp_path, monkeypatch, loop):
    rois = [{"name": "A"}] if loop == "roi" else None
    worker, truth = _guided_worker(tmp_path, monkeypatch, rois)
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    sfx = "_A" if loop == "roi" else ""
    meta = _meta(worker)
    store = meta["rois"][0]["label_store"] if loop == "roi" else meta["label_store"]
    _assert_label_store(worker, sfx, truth, store)
    assert rp._registered(worker)
    region = meta["rois"][0] if loop == "roi" else meta
    assert region["nuclei_zarr_path"].endswith(f"global_nuclei_mask{sfx}.zarr")
    assert region["nuclei_cell_table_path"].endswith(f"global_nuclei_cell{sfx}.zarr")
    if loop == "roi":
        assert meta["label_store"]["A"] == store
        region_meta = _meta(worker, "segmentation_meta_A.json")
        assert region_meta["label_store"] == store
    _assert_step3_reads_it(worker, sfx, "A" if loop == "roi" else "Full WSI")


def test_an_io_failure_while_writing_the_table_fails_the_run(app, tmp_path, monkeypatch):
    import zarr
    worker, _truth_ = _guided_worker(tmp_path, monkeypatch, [{"name": "A"}])
    real = zarr.core.Array.append
    calls = []

    def failing(self, data, axis=0):
        calls.append(1)
        # block N3b: the table is appended once per 2^20 nuclei -- here the
        # first append is the whole table
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(self, data, axis)
    monkeypatch.setattr(zarr.core.Array, "append", failing)
    got = rp._collect(worker)
    worker.run()
    assert got["finished"] == [] and got["error"] and "No space left" in got["error"][0]
    assert not rp._registered(worker)
    names = os.listdir(worker.output_dir)
    assert not [n for n in names if n.endswith(".partial")]
    assert "global_nuclei_cell_A.zarr" not in names
    assert not os.path.exists(os.path.join(worker.output_dir, "segmentation_meta.json"))


def test_a_stop_leaves_no_partial(app, tmp_path, monkeypatch):
    worker, _truth_ = _guided_worker(tmp_path, monkeypatch, [{"name": "A"}], stop_after=2)
    got = rp._collect(worker)
    worker.run()
    assert got["finished"] == [] and not rp._registered(worker)
    names = os.listdir(worker.output_dir)
    assert not [n for n in names if n.endswith(".partial")]
    assert not [n for n in names if n.startswith("global_nuclei_cell")]


def test_a_recovered_run_has_no_nuclei_and_says_why(app, tmp_path, monkeypatch):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    cells, _nuclei, _expect = _truth()
    rec = tmp_path / "npy"
    rec.mkdir()
    for tile in TileScheduler(H, W, ROWS, COLS, HALO).tiles:
        y0, y1, x0, x1 = tile.read_bbox
        np.save(rec / f"tile_A_{tile.row}_{tile.col}.npy", cells[y0:y1, x0:x1])
    monkeypatch.setattr(SegmentMergeWorker, "_validate_mesmer_config", lambda self, *a, **k: None)
    worker = SegmentMergeWorker(rp._fused_zarr(tmp_path, rp._image()),
                                seg_config=dict(rp.PARAMS[GUIDED], method=GUIDED),
                                n_rows=ROWS, n_cols=COLS, overlap_px=HALO,
                                output_dir=str(tmp_path / "proj"), rois=[{"name": "A"}],
                                recovery_npy_dir=str(rec))
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [], got["error"]
    store = _meta(worker)["rois"][0]["label_store"]
    assert store["nucleus"] is None and "cannot be recovered" in store["nuclei"]["unavailable"]
    assert not [n for n in os.listdir(worker.output_dir) if n.startswith("global_nuclei")]


def test_the_nucleus_write_does_not_materialise_the_image(app, tmp_path):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    h, w = 3000, 5000                                   # 60 MB as uint32
    mm = np.memmap(str(tmp_path / "n.dat"), dtype="uint32", mode="w+", shape=(h, w))
    mm[::7, ::11] = 3
    mm.flush()
    ro = np.memmap(str(tmp_path / "n.dat"), dtype="uint32", mode="r", shape=(h, w))
    worker = SegmentMergeWorker(str(tmp_path / "x.zarr"), seg_config={"method": GUIDED},
                                output_dir=str(tmp_path / "proj"))
    os.makedirs(worker.output_dir, exist_ok=True)
    state = worker._nuclei_begin("A")
    tracemalloc.start()
    worker._nuclei_finish(state, ro, h, w, "A")
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 16 * 2 ** 20, peak                    # a few 4 MB blocks, never 60 MB


def test_expansion_on_the_real_engine_keeps_its_nuclei(app, tmp_path):
    """The real Cellpose engine: nuclei are kept, paired, in their cells."""
    import zarr
    method = "cellpose_nuclei_expansion"
    if rp._engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    worker = rp._worker(tmp_path, rp._fused_zarr(tmp_path, rp._image()),
                        rp._contract_config(tmp_path, method), rois=[{"name": "A"}])
    got = rp._collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    cell = np.asarray(zarr.open(os.path.join(worker.output_dir, "global_mask_A.zarr"), "r"))
    nuc = np.asarray(zarr.open(os.path.join(worker.output_dir, "global_nuclei_mask_A.zarr"), "r"))
    table = np.asarray(zarr.open(os.path.join(worker.output_dir, "global_nuclei_cell_A.zarr"), "r"))
    m = int(nuc.max())
    assert m > 0 and list(np.unique(nuc[nuc > 0])) == list(range(1, m + 1))
    assert table.shape == (m + 1,) and (cell[nuc > 0] == table[nuc[nuc > 0]]).all()
    store = _meta(worker)["rois"][0]["label_store"]
    assert store["complete"] and store["nuclei"]["kept"] == m
    assert store["cell"]["path"].endswith("global_mask_A.zarr")
    assert not [n for n in os.listdir(worker.output_dir)
                if n.startswith("global_nuclei_mask") and not n.endswith(".zarr")]


def test_a_failure_while_writing_the_nuclei_leaves_no_final_array(app, tmp_path, monkeypatch):
    """The nuclei are written under `*.partial` and renamed only when whole:
    a failure half-way leaves no `global_nuclei_mask_A.zarr` behind."""
    import zarr
    worker, _truth_ = _guided_worker(tmp_path, monkeypatch, [{"name": "A"}])
    real = zarr.core.Array.__setitem__
    writes = []

    def failing(self, key, value):
        where = str(getattr(self.store, "path", "") or getattr(self.store, "dir_path", lambda: "")())
        if "global_nuclei_mask_A" in where and np.ndim(value) == 2:
            writes.append(1)
            if len(writes) == 1:
                raise OSError(5, "Input/output error")
        return real(self, key, value)
    monkeypatch.setattr(zarr.core.Array, "__setitem__", failing)
    got = rp._collect(worker)
    worker.run()
    assert got["finished"] == [] and "Input/output error" in got["error"][0]
    names = os.listdir(worker.output_dir)
    assert "global_nuclei_mask_A.zarr" not in names
    assert not [n for n in names if n.endswith(".partial")]
    assert not rp._registered(worker)
