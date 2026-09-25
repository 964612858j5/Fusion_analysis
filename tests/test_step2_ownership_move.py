"""Step2's two tile loops use the shared ownership code, unchanged in effect.

Plan: Step2 hook-up, step 1 (a pure move). Both loops -- `_segment_one_zarr`
and the full-image loop in `run()` -- now call `core.label_ownership` for the
centroid ownership and the renumbering LUT instead of their inline copies.

The oracle below is the inline code as it stood before the move (frozen
here, since the loops no longer contain it): centroid by bincount, keep the
labels whose centroid lies in the half-open own region, one LUT for the
primary mask, the HQ nuclei, every HQ2 layer and the QC rows (ids above the
primary's max dropped), and Step2's paste of the whole read window. Each
loop's real outputs must equal it -- primary, nuclei, HQ2 layers, QC table
ids and the total -- over boundary-crossing cells and empty tiles.

The model is replaced by a fixed labelling, as in `test_label_ownership.py`.
The shadow compare is switched off: never called, and neither the log nor
the run's engine metadata claims it is on.
"""

import csv
import logging
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HQ2_LAYERS = ("hq_proposal", "imagej_proposal", "core", "expansion")


def _labels(seed, h=97, w=113, n=80):
    rng = np.random.default_rng(seed)
    lab = np.zeros((h, w), np.uint16)
    for k in range(1, n + 1):
        y, x = rng.integers(0, h - 6), rng.integers(0, w - 6)
        dy, dx = rng.integers(2, 12, 2)
        lab[y:y + dy, x:x + dx] = k
    if seed == 3:
        lab[:, :60] = 0                                    # whole tiles empty
    return lab


def _fake_result(method, tile_data):
    """The model's stand-in: labels = distinct values of channel 0. HQ adds
    nuclei (some cells without one, one id above the cells' max) and QC rows
    (plus a background row); HQ2 adds its four layers."""
    t = np.asarray(tile_data)[..., 0]
    u, inv = np.unique(t, return_inverse=True)
    m = inv.reshape(t.shape).astype(np.uint32)
    if u[0] != 0:
        m += 1
    if method == "cellpose_wholecell_fusion":
        return m
    n = int(m.max())
    nuc = np.where(m % 3 == 0, 0, m).astype(np.uint32)
    if n:
        nuc[0, 0] = n + 5
    rows = [{"cell_id": k, "cell_area": int((m == k).sum()), "main_channel": f"q{k}"}
            for k in range(1, n + 1)]
    rows.append({"cell_id": 0, "main_channel": "bg"})
    out = {"mask": m, "nuclei": nuc, "qc_rows": rows}
    if method == "cellpose_nuclei_hq2":
        out["hq2_layers"] = {
            "hq_proposal": m,
            "imagej_proposal": (m * (m % 2)).astype(np.uint32),
            "core": nuc,
            "expansion": np.where(m > 0, m + 1, 0).astype(np.uint32),
        }
        out["hq2_metadata"] = {"n": n}
    return out


def _legacy_merge(method, data, rows, cols, overlap):
    """Step2's inline ownership + paste before the move, frozen."""
    from block01.utils.tile_scheduler import TileScheduler

    h, w = data.shape[:2]
    prim = np.zeros((h, w), np.uint32)
    nuclei = np.zeros((h, w), np.uint32)
    layers = {k: np.zeros((h, w), np.uint32) for k in HQ2_LAYERS}
    qc_ids = []
    hq2_tiles = []
    offset = 0
    for tile in TileScheduler(h, w, rows, cols, overlap).tiles:
        ry0, ry1, rx0, rx1 = tile.read_bbox
        oy0, oy1, ox0, ox1 = tile.own_bbox
        res = _fake_result(method, data[ry0:ry1, rx0:rx1])
        local = res["mask"] if isinstance(res, dict) else res
        n_raw = int(local.max())
        if n_raw == 0:
            continue
        lh, lw = local.shape
        flat = local.ravel()
        ys = np.repeat(np.arange(lh, dtype=np.float32), lw)
        xs = np.tile(np.arange(lw, dtype=np.float32), lh)
        cnts = np.bincount(flat, minlength=n_raw + 2)
        sy = np.bincount(flat, weights=ys, minlength=n_raw + 2)
        sx = np.bincount(flat, weights=xs, minlength=n_raw + 2)
        valid = cnts[1:n_raw + 1] > 0
        cy = np.where(valid, sy[1:n_raw + 1] / np.maximum(cnts[1:n_raw + 1], 1), -1)
        cx = np.where(valid, sx[1:n_raw + 1] / np.maximum(cnts[1:n_raw + 1], 1), -1)
        loy0, loy1, lox0, lox1 = oy0 - ry0, oy1 - ry0, ox0 - rx0, ox1 - rx0
        keep = [i + 1 for i in range(n_raw)
                if loy0 <= cy[i] < loy1 and lox0 <= cx[i] < lox1]
        if not keep:
            continue
        lut = np.zeros(n_raw + 1, dtype=np.uint32)
        for new_id, lab in enumerate(keep, start=1):
            lut[lab] = new_id + offset

        def paste(dst, arr):
            out = lut[np.where(arr <= n_raw, arr, 0).astype(np.uint32)]
            view = dst[ry0:ry1, rx0:rx1]
            np.copyto(view, out, where=out > 0)

        paste(prim, local)
        if isinstance(res, dict):
            paste(nuclei, res["nuclei"])
            for k, arr in (res.get("hq2_layers") or {}).items():
                paste(layers[k], np.asarray(arr, np.uint32))
            kept = set(keep)
            qc_ids += [int(lut[r["cell_id"]]) for r in res["qc_rows"] if r["cell_id"] in kept]
            if res.get("hq2_metadata"):
                hq2_tiles.append((tile.row, tile.col))
        offset += len(keep)
    return prim, nuclei, layers, qc_ids, hq2_tiles, offset


def _worker(tmp_path, monkeypatch, method, lab, rows, cols, overlap):
    import zarr
    from block01.workers.segment_merge_worker import SegmentMergeWorker as W

    zp = str(tmp_path / "fused.zarr")
    z = zarr.open(zp, mode="w", shape=lab.shape + (2,), chunks=(32, 32, 2), dtype=np.uint16)
    z[..., 0] = lab
    z[..., 1] = lab
    monkeypatch.setattr(W, "_segment_tile",
                        lambda self, tile_data, *a, **k: _fake_result(self.method, tile_data))
    monkeypatch.setattr(W, "_validate_hq_config", lambda self, p=None: (["DAPI", "M1"], None))
    monkeypatch.setattr(W, "_read_hq_marker_channels", lambda self, *a, **k: None)
    monkeypatch.setattr(W, "_is_lean_csd", lambda self: False)
    monkeypatch.setattr(W, "_init_segmentation_backend", lambda self, *a, **k: None)
    monkeypatch.setattr(W, "_uses_torch_backend", lambda self: False)

    def no_shadow(*a, **k):
        raise AssertionError("the shadow compare must not run")

    monkeypatch.setattr(W, "_merge_policy_shadow_compare", no_shadow)
    worker = W(zp, seg_config={"method": method, "write_hq2_debug_layers": True},
               n_rows=rows, n_cols=cols, overlap_px=overlap,
               output_dir=str(tmp_path / "proj"))
    return worker, zp


CASES = [(0, 2, 3, 9), (1, 3, 2, 14), (2, 1, 1, 0), (3, 4, 4, 5)]
METHODS = ["cellpose_wholecell_fusion", "cellpose_nuclei_hq", "cellpose_nuclei_hq2"]


@pytest.mark.parametrize("loop", ["segment_one_zarr", "run"])
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("seed,rows,cols,overlap", CASES)
def test_both_loops_equal_the_legacy_inline_ownership(tmp_path, monkeypatch, caplog, loop,
                                                      method, seed, rows, cols, overlap):
    import zarr

    lab = _labels(seed)
    worker, zp = _worker(tmp_path, monkeypatch, method, lab, rows, cols, overlap)
    caplog.set_level(logging.INFO)
    if loop == "segment_one_zarr":
        total = worker._segment_one_zarr(zp, "t", model=None, use_gpu=False,
                                         log=logging.getLogger("test"))
        sfx = "_t"
    else:
        got = {}
        worker.finished.connect(lambda d, n: got.setdefault("n", n))
        worker.run()
        total = got.get("n")
        sfx = ""

    def read(name):
        return np.asarray(zarr.open(os.path.join(worker.output_dir, f"{name}{sfx}.zarr"),
                                    mode="r"))

    prim, nuclei, layers, qc_ids, hq2_tiles, offset = _legacy_merge(
        method, np.stack([lab, lab], -1), rows, cols, overlap)
    assert total == offset
    np.testing.assert_array_equal(read("global_mask"), prim)
    if method == "cellpose_wholecell_fusion":
        return
    np.testing.assert_array_equal(read("global_nuclei_mask"), nuclei)
    qc_name = "hq2_qc_table" if method == "cellpose_nuclei_hq2" else "hq_qc_table"
    with open(os.path.join(worker.output_dir, f"{qc_name}{sfx}.csv")) as f:
        assert [int(r["cell_id"]) for r in csv.DictReader(f)] == qc_ids
    if method == "cellpose_nuclei_hq2":
        for k in HQ2_LAYERS:
            np.testing.assert_array_equal(read(f"global_hq2_{k}_mask"), layers[k])
        assert [(m["row"], m["col"]) for m in worker._hq2_tile_metadata] == hq2_tiles


def test_the_shadow_compare_is_off_in_the_log_and_the_metadata(tmp_path, monkeypatch, caplog):
    lab = _labels(0)
    worker, _ = _worker(tmp_path, monkeypatch, "cellpose_wholecell_fusion", lab, 2, 2, 8)
    got = {}
    worker.finished.connect(lambda d, n: got.setdefault("n", n))
    worker.run()
    assert got.get("n", 0) > 0
    log_text = "".join(open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                       for root, _, files in os.walk(worker.output_dir)
                       for f in files if f.endswith(".log"))
    assert log_text and "shadow_compare=enabled" not in log_text
    assert worker._step2_engine_meta()["merge_policy"]["shadow_compare_enabled"] is False
    assert worker._merge_shadow_compare_count == 0
