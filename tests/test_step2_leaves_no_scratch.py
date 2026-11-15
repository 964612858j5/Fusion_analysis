"""Backlog 23 (user ruling 2026-11-15): a Step2 run that succeeded keeps no
merge scratch. The cell mask and DAPI memmaps (`global_mask*.dat`,
`global_dapi*.dat`) stayed in every run -- 1.43 GB on the laptop's 2000 x 2000
crop -- though nobody reads them once the zarr and OME-TIFF are written. Both
loops (the whole image, regions) through the public `run`; a run stopped
while its outputs are written keeps them, as before."""
import os

import numpy as np
import pytest
import zarr

pytest.importorskip("block01.qt")

from test_step2_skip_empty_tiles import COLS, OVERLAP, ROWS, _labels, _run, _worker  # noqa: E402


def _dats(d):
    return [os.path.join(b, f) for b, _d, fs in os.walk(d) for f in fs if f.endswith(".dat")]


@pytest.mark.parametrize("regions", [False, True])
def test_a_finished_run_keeps_no_scratch_memmap(tmp_path, monkeypatch, regions):
    lab = _labels(3)
    w, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP)
    if regions:
        w.rois = [{"name": "R1", "bbox_fullres": [0, lab.shape[0], 0, lab.shape[1]],
                   "polygon_fullres": None}]
    _run(w, zp, "run")
    names = []
    for base, _dirs, files in os.walk(w.output_dir):
        names += [os.path.relpath(os.path.join(base, f), w.output_dir) for f in files]
    assert not [n for n in names if n.endswith(".dat")], names
    stores = [n for n in os.listdir(w.output_dir) if n.startswith("global_mask") and n.endswith(".zarr")]
    assert stores, os.listdir(w.output_dir)
    mask = np.asarray(zarr.open(os.path.join(w.output_dir, stores[0]), mode="r"))
    assert mask.max() > 0
    assert [n for n in os.listdir(w.output_dir) if n.startswith("global_dapi") and n.endswith(".ome.tiff")]


def test_a_run_stopped_while_writing_its_outputs_keeps_its_scratch(tmp_path, monkeypatch):
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W
    lab = _labels(3)
    w, zp = _worker(tmp_path, monkeypatch, lab, ROWS, COLS, OVERLAP)
    real = W._write_run_metadata

    def stop_while_writing(self, meta):
        real(self, meta)
        self._stop = True                  # the user's Stop lands during the last writes
    monkeypatch.setattr(W, "_write_run_metadata", stop_while_writing)
    got = {}
    w.error.connect(lambda m: got.setdefault("error", m))
    w.run()
    assert got.get("error") == "Stopped by user."
    assert _dats(w.output_dir)                                   # left as before
