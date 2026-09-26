"""Step2 runs a Step1 hand-over through the engine process.

Step2 hook-up, step 3 (plan 7.10.7, V2). A params file carrying a
`preseg_contract` block is segmented tile by tile by `seg_runner` -- the same
engine, model and input construction as Step1 -- while Step2's own tiling,
ownership, paste and outputs stay as they are.

  * equality: the global mask of a hand-over run equals running the SAME
    runner tile by tile and pasting with the shared ownership functions the
    way Step2 pastes; both loops (ROI and full image); nuclear-guided's
    nuclei as well. Real engines only; a method whose model is missing on
    this machine is skipped with the reason "not accepted" -- never mocked.
  * inputs: what the runner receives for each method is Step1's
    construction of the same window;
  * refusals: a HALO other than the overlap; another engine version runs
    and is recorded (block M);
  * failure: a tile the engine fails on ends the run -- never an empty tile
    registered as success;
  * Stop (during the model load, during inference, between ROIs) and window
    close: the GUI thread never waits, nothing is registered, no `finished`,
    no engine process and no runner files are left.
"""

import glob
import json
import os
import signal
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.core import label_ownership, preseg_contract, preseg_input  # noqa: E402
from block01.seg_runner import engines as seg_engines  # noqa: E402
from block01.seg_runner.client import EngineProcess  # noqa: E402

import test_preseg_contract as tpc  # noqa: E402  (the Use + Save helpers)

H, W = 176, 208
ROWS, COLS, HALO = 2, 2, 32

# One combination per method, sized for the synthetic blobs (diameter ~16).
PARAMS = {
    "cellpose_wholecell_fusion": {"diameter": 16.0, "flow_threshold": 0.4,
                                  "cellprob_threshold": 0.0, "min_size": 15},
    "cellpose_nuclei_dapi": {"diameter": 16.0, "flow_threshold": 0.4,
                             "cellprob_threshold": 0.0, "min_size": 15},
    "cellpose_nuclei_expansion": {"diameter": 16.0, "flow_threshold": 0.4,
                                  "cellprob_threshold": 0.0, "min_size": 15,
                                  "expand_distance": 4.0},
    "stardist_nuclei_dapi": {"prob_thresh": 0.5, "nms_thresh": 0.4,
                             "model_name": "2D_versatile_fluo"},
    "stardist_nuclei_expansion": {"prob_thresh": 0.5, "nms_thresh": 0.4,
                                  "expand_distance": 4.0, "model_name": "2D_versatile_fluo"},
    "mesmer_whole_cell": {"maxima_threshold": 0.075, "interior_threshold": 0.2,
                          "image_mpp": 0.5, "postprocess_min_size": 0},
    "mesmer_nuclei": {"maxima_threshold": 0.1, "interior_threshold": 0.2,
                      "image_mpp": 0.5, "postprocess_min_size": 0},
    "mesmer_nuclear_guided": {"maxima_threshold": 0.075, "interior_threshold": 0.2,
                              "image_mpp": 0.5, "postprocess_min_size": 0},
}
EXPECTED_KIND = {m: k for m, k in preseg_input.INPUT_KIND.items()}


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _engine_missing(engine):
    if engine == "mesmer":
        try:
            seg_engines.mesmer_model_path()
        except seg_engines.ModelMissingError as exc:
            return f"Mesmer model missing on this machine -- not accepted: {exc}"
        pytest.importorskip("deepcell")
    elif engine == "stardist":
        pytest.importorskip("stardist")
    else:
        pytest.importorskip("cellpose")
    return ""


_IDENT = {}


def _identity(engine):
    """The real engine's identity, as its hello reports it (cached)."""
    if engine not in _IDENT:
        ep = EngineProcess(engine)
        try:
            _IDENT[engine] = ep.start()["identity"]
        finally:
            ep.close()
    return _IDENT[engine]


def _image(seed=0):
    """uint16 [fusion, nucleus]: round nuclei, each in a brighter ring."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    nuc = np.zeros((H, W), np.float32)
    mem = np.zeros((H, W), np.float32)
    for _ in range(70):
        cy, cx = rng.uniform(8, H - 8), rng.uniform(8, W - 8)
        d = np.hypot(yy - cy, xx - cx)
        nuc = np.maximum(nuc, np.clip(1.0 - d / 7.0, 0, 1))
        mem = np.maximum(mem, np.exp(-((d - 11.0) ** 2) / 4.0))
    img = np.stack([mem, nuc], -1) + rng.normal(0, 0.02, (H, W, 2))
    return (np.clip(img, 0, 1) * 60000).astype(np.uint16)


def _fused_zarr(tmp_path, img):
    import zarr
    zp = str(tmp_path / "fused.zarr")
    z = zarr.open(zp, mode="w", shape=img.shape, chunks=(64, 64, 2), dtype=np.uint16)
    z[:] = img
    return zp


def _contract_config(tmp_path, method, identity=None, halo=HALO):
    """The params file a real Use + Save writes, read the way Step2 reads it."""
    from block01.utils.segmentation_config import normalize_segmentation_config
    engine = seg_engines.METHOD_ENGINE[method]
    ident = identity if identity is not None else _identity(engine)
    _, _, path = tpc._use_and_save(tmp_path, method, PARAMS[method], identity=ident, halo=halo)
    with open(path, encoding="utf-8") as f:
        return normalize_segmentation_config(json.load(f))


def _worker(tmp_path, zp, cfg, rois=None, overlap=HALO):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    return SegmentMergeWorker(zp, seg_config=cfg, n_rows=ROWS, n_cols=COLS, overlap_px=overlap,
                              output_dir=str(tmp_path / "proj"), rois=rois)


def _collect(worker):
    got = {"finished": [], "error": [], "progress": []}
    d = QtCore.Qt.DirectConnection
    worker.finished.connect(lambda o, n: got["finished"].append(n), d)
    worker.error.connect(lambda m: got["error"].append(m), d)
    worker.progress.connect(lambda a, b, m: got["progress"].append(m), d)
    return got


def _registered(worker):
    path = os.path.join(worker.project_output_dir, "segmentation_results",
                        "segmentation_results_index.json")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        runs = json.load(f).get("runs") or []
    return any(worker.result_id in (r.get("run_id"), r.get("result_id")) for r in runs)


def _assert_nothing_left(worker, pgid=None):
    assert worker._engine is None
    assert not os.path.exists(os.path.join(worker.output_dir, "runner_io"))
    if pgid is not None:
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)


def _oracle(method, img):
    """The same runner, tile by tile, pasted with the shared ownership code
    the way Step2 pastes (whole read window, ids above the primary's max
    dropped from the nuclei)."""
    from block01.utils.tile_scheduler import TileScheduler
    import tempfile

    engine = seg_engines.METHOD_ENGINE[method]
    outputs = seg_engines.METHOD_OUTPUTS[method]
    primary = "cell" if "cell" in outputs else "nucleus"
    prim = np.zeros((H, W), np.uint32)
    nuclei = np.zeros((H, W), np.uint32)
    offset = 0
    tmp = tempfile.mkdtemp()
    tiles = TileScheduler(H, W, ROWS, COLS, HALO).tiles
    tasks = []
    for i, tile in enumerate(tiles):
        ry0, ry1, rx0, rx1 = tile.read_bbox
        path = os.path.join(tmp, f"t{i}.npy")
        np.save(path, preseg_input.model_input(preseg_input.INPUT_KIND[method],
                                               img[ry0:ry1, rx0:rx1]))
        tasks.append({"task_id": f"t{i}", "input": path, "out_dir": tmp,
                      "params": dict(PARAMS[method],
                                     **preseg_contract.fixed_rules(method), method=method)})
    ep = EngineProcess(engine)
    ep.start()
    try:
        states = ep.run(tasks)
    finally:
        ep.close()
    assert set(states.values()) == {"ok"}
    for i, tile in enumerate(tiles):
        with open(ep.states[f"t{i}"]["detail"], encoding="utf-8") as f:
            rec = json.load(f)
        local = np.load(rec[primary]["path"]).astype(np.uint32)
        nuc = np.load(rec["nucleus"]["path"]) if method == "mesmer_nuclear_guided" else None
        n_raw = int(local.max())
        if n_raw == 0:
            continue
        ry0, ry1, rx0, rx1 = tile.read_bbox
        oy0, oy1, ox0, ox1 = tile.own_bbox
        keep = label_ownership.kept_labels(local, (oy0 - ry0, oy1 - ry0, ox0 - rx0, ox1 - rx0))
        if len(keep) == 0:
            continue
        lut = label_ownership.ownership_lut(local, keep, offset)
        for dst, arr in ((prim, local), (nuclei, nuc)):
            if arr is None:
                continue
            out = lut[np.where(arr <= n_raw, arr, 0).astype(np.uint32)]
            np.copyto(dst[ry0:ry1, rx0:rx1], out, where=out > 0)
        offset += len(keep)
    return prim, nuclei, offset


# ── equality, 8 methods × 2 loops ─────────────────────────────────────

@pytest.mark.parametrize("loop", ["full", "roi"])
@pytest.mark.parametrize("method", sorted(PARAMS))
def test_a_hand_over_equals_the_runner_with_shared_ownership(app, tmp_path, method, loop):
    import zarr
    reason = _engine_missing(seg_engines.METHOD_ENGINE[method])
    if reason:
        pytest.skip(reason)
    img = _image()
    zp = _fused_zarr(tmp_path, img)
    cfg = _contract_config(tmp_path, method)
    rois = [{"name": "A"}] if loop == "roi" else None
    worker = _worker(tmp_path, zp, cfg, rois=rois)
    got = _collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    sfx = "_A" if loop == "roi" else ""
    prim, nuclei, total = _oracle(method, img)
    assert total > 0 and got["finished"][0] == total
    mask = np.asarray(zarr.open(os.path.join(worker.output_dir, f"global_mask{sfx}.zarr"), mode="r"))
    np.testing.assert_array_equal(mask, prim)
    if method == "mesmer_nuclear_guided":
        nz = zarr.open(os.path.join(worker.output_dir, f"global_nuclei_mask{sfx}.zarr"), mode="r")
        np.testing.assert_array_equal(np.asarray(nz), nuclei)
    assert _registered(worker)
    _assert_nothing_left(worker)


# ── inputs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", sorted(PARAMS))
def test_the_runner_gets_step1s_input_for_the_method(app, tmp_path, monkeypatch, method):
    """The array the engine receives, per method, against the table in plan
    7.11.4 written out here (not through preseg_input): the engine process
    is replaced for this check only; the real engines run above."""
    img = _image(1)
    zp = _fused_zarr(tmp_path, img)
    engine = seg_engines.METHOD_ENGINE[method]
    ident = tpc.IDENT[engine]
    cfg = _contract_config(tmp_path, method, identity=ident)
    seen = []

    def start(self):
        self.hello = {"identity": ident, "device": "cpu"}
        return self.hello

    def run(self, tasks, on_settle=None):
        for t in tasks:
            arr = np.load(t["input"])
            seen.append((t["task_id"], arr))
            h, w = arr.shape[:2]
            out = {"task_id": t["task_id"], "status": "ok"}
            for k in ("cell", "nucleus"):
                p = os.path.join(t["out_dir"], f"{t['task_id']}.{k}.npy")
                np.save(p, np.zeros((h, w), np.uint32))
                out[k] = {"status": "ok", "path": p}
            rp = os.path.join(t["out_dir"], f"{t['task_id']}.record.json")
            with open(rp, "w") as f:
                json.dump(out, f)
            self.states[t["task_id"]] = {"state": "ok", "detail": rp}
        return {t["task_id"]: "ok" for t in tasks}

    monkeypatch.setattr(EngineProcess, "start", start)
    monkeypatch.setattr(EngineProcess, "run", run)
    monkeypatch.setattr(EngineProcess, "close", lambda self: None)
    monkeypatch.setattr(EngineProcess, "terminate", lambda self, grace=5.0: None)
    worker = _worker(tmp_path, zp, cfg)
    worker.run()
    from block01.utils.tile_scheduler import TileScheduler
    tiles = TileScheduler(H, W, ROWS, COLS, HALO).tiles
    assert len(seen) == len(tiles)
    assert len({tid for tid, _ in seen}) == len(tiles)            # one task id per tile
    for (_, arr), tile in zip(seen, tiles):
        ry0, ry1, rx0, rx1 = tile.read_bbox
        F = img[ry0:ry1, rx0:rx1].astype(np.float32) / 65535.0
        f, n = F[..., 0], F[..., 1]
        want = {"fusion_fusion_nucleus": np.stack([f, f, n], -1),
                "nucleus": n,
                "nucleus_fusion": np.stack([n, f], -1),
                "nucleus_zero": np.stack([n, np.zeros_like(n)], -1)}[EXPECTED_KIND[method]]
        assert arr.dtype == np.float32
        np.testing.assert_array_equal(arr, want)


def test_a_mesmer_hand_over_opens_no_channel_group(app, tmp_path, monkeypatch):
    """The file's input_mode is the normaliser's default "selected_channels";
    a hand-over still reads the fused tile only."""
    from block01.workers.segment_merge_worker import SegmentMergeWorker as Wk
    cfg = _contract_config(tmp_path, "mesmer_nuclear_guided", identity=tpc.IDENT["mesmer"])
    assert cfg.get("input_mode") == "selected_channels"
    monkeypatch.setattr(Wk, "_open_hq_channel_group",
                        lambda self, *a, **k: pytest.fail("a channel group was opened"))
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg)
    worker._contract = preseg_contract.validate(worker.seg_config)
    assert worker._validate_mesmer_config() is None
    assert worker.seg_config["mesmer_input_source"] == "fused_zarr"


# ── refusals ──────────────────────────────────────────────────────────

def test_another_engine_version_runs_and_is_recorded(app, tmp_path, capsys):
    # Block M: the Step1 run's identity is recorded, not required -- the
    # parameters are what Step1 decided. Only another engine kind is refused
    # (the contract itself cannot name one: `preseg_contract.validate`).
    if _engine_missing("stardist"):
        pytest.skip("no StarDist")
    ident = dict(_identity("stardist"), lib_versions={"stardist": "0.0.1"})
    cfg = _contract_config(tmp_path, "stardist_nuclei_dapi", identity=ident)
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg)
    got = _collect(worker)
    worker.run()
    assert got["error"] == [] and len(got["finished"]) == 1
    assert "the engine differs from the Step1 run in: lib_versions" in capsys.readouterr().out
    with open(os.path.join(worker.output_dir, "segmentation_meta.json"), encoding="utf-8") as f:
        eng = json.load(f)["seg_engine"]
    assert eng["step1_identity"] == ident and eng["identity"] == _identity("stardist")
    assert _registered(worker)
    _assert_nothing_left(worker)
    other = dict(ident, engine="cellpose")
    wrong = _contract_config(tmp_path / "other", "stardist_nuclei_dapi", identity=other)
    with pytest.raises(preseg_contract.ContractError, match="engine identity"):
        preseg_contract.validate(wrong)


def test_a_halo_other_than_the_overlap_is_refused(app, tmp_path, monkeypatch):
    cfg = _contract_config(tmp_path, "stardist_nuclei_dapi", identity=tpc.IDENT["stardist"])
    started = []
    monkeypatch.setattr(EngineProcess, "start", lambda self: started.append(1))
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg, overlap=HALO + 8)
    got = _collect(worker)
    worker.run()
    assert got["finished"] == [] and "HALO of 32 px" in got["error"][0]
    assert started == [] and not _registered(worker)


def test_the_page_refuses_a_halo_other_than_the_overlap(app, tmp_path, monkeypatch):
    _, out, _ = tpc._use_and_save(tmp_path, "stardist_nuclei_dapi",
                                  tpc.COMBOS["stardist_nuclei_dapi"], halo=250)
    page = tpc._page_config(app, out)
    said = tpc._told(monkeypatch)
    page._overlap_spin.setValue(200)
    assert page._check_preseg_contract(page.get_seg_config()) is False
    assert "HALO of 250 px" in said[-1][1]
    page._overlap_spin.setValue(250)
    assert page._check_preseg_contract(page.get_seg_config()) is True


# ── failure, Stop, close ─────────────────────────────────────────────

def _start_thread(worker):
    worker.start()
    return worker


def _wait_for(cond, timeout=120.0):
    t0 = time.monotonic()
    while not cond():
        QtWidgets.QApplication.processEvents()
        if time.monotonic() - t0 > timeout:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _in_flight(worker):
    io = os.path.join(worker.output_dir, "runner_io")
    return bool(glob.glob(os.path.join(io, "*.input.npy")))


def _loading(worker):
    ep = worker._engine
    return ep is not None and ep.proc is not None and ep.hello is None


def _timed_stop(worker):
    t0 = time.perf_counter()
    worker.stop()
    return time.perf_counter() - t0


@pytest.mark.parametrize("when", ["loading", "inference"])
@pytest.mark.parametrize("loop", ["full", "roi"])
def test_stop_ends_the_run_with_nothing_registered(app, tmp_path, when, loop):
    engine_method = "cellpose_nuclei_dapi"
    if _engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    cfg = _contract_config(tmp_path, engine_method)
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg,
                     rois=[{"name": "A"}] if loop == "roi" else None)
    got = _collect(worker)
    _start_thread(worker)
    try:
        _wait_for((lambda: _loading(worker)) if when == "loading" else (lambda: _in_flight(worker)))
        pgid = worker._engine.proc.pid
        assert _timed_stop(worker) < 0.05                    # the GUI thread never waits
        t0 = time.monotonic()
        _wait_for(lambda: worker.isFinished())
        if when == "loading":
            # ended by the signal, not by the model finishing its load
            assert time.monotonic() - t0 < 2.0
    finally:
        worker.wait(60000)
    assert got["finished"] == [] and got["error"] == ["Stopped by user."]
    assert not _registered(worker)
    _assert_nothing_left(worker, pgid)


def test_stop_between_rois_registers_nothing(app, tmp_path):
    if _engine_missing("stardist"):
        pytest.skip("no StarDist")
    cfg = _contract_config(tmp_path, "stardist_nuclei_dapi")
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg,
                     rois=[{"name": "A"}, {"name": "B"}])
    got = _collect(worker)
    # Stop as soon as ROI A is done (the progress line Step2 shows for it).
    worker.progress.connect(lambda a, b, m: worker.stop() if m.startswith("✓ ROI A") else None,
                            QtCore.Qt.DirectConnection)
    worker.run()
    assert any(m.startswith("✓ ROI A") for m in got["progress"])
    assert not any("ROI [2/2]" in m for m in got["progress"])
    assert got["finished"] == [] and got["error"] == ["Stopped by user."]
    assert not _registered(worker)
    _assert_nothing_left(worker)


@pytest.mark.parametrize("loop", ["full", "roi"])
def test_a_killed_engine_fails_the_run_instead_of_an_empty_tile(app, tmp_path, loop):
    if _engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    cfg = _contract_config(tmp_path, "cellpose_nuclei_dapi")
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg,
                     rois=[{"name": "A"}] if loop == "roi" else None)
    got = _collect(worker)
    _start_thread(worker)
    try:
        _wait_for(lambda: _in_flight(worker))
        pgid = worker._engine.proc.pid
        os.kill(worker._engine.proc.pid, signal.SIGKILL)       # the engine child, not Step2
        _wait_for(lambda: worker.isFinished())
    finally:
        worker.wait(60000)
    assert got["finished"] == [] and len(got["error"]) == 1
    assert "the engine failed on tile_" in got["error"][0]
    assert not _registered(worker)
    _assert_nothing_left(worker, pgid)


def test_closing_the_window_waits_for_step2_without_blocking(app, tmp_path, monkeypatch):
    if _engine_missing("cellpose"):
        pytest.skip("no Cellpose")
    from block01.ui import main_window as mw
    cfg = _contract_config(tmp_path, "cellpose_nuclei_dapi")
    retries = []
    # The window's retry timer, recorded instead of scheduled, so the test
    # decides when the second close comes.
    monkeypatch.setattr(mw.QtCore.QTimer, "singleShot", lambda ms, fn: retries.append(ms))
    w = mw.MainWindow()
    worker = _worker(tmp_path, _fused_zarr(tmp_path, _image()), cfg)
    w._step2._worker = worker
    _start_thread(worker)
    try:
        _wait_for(lambda: _loading(worker))
        pgid = worker._engine.proc.pid
        t0 = time.perf_counter()
        closed = w.close()
        assert time.perf_counter() - t0 < 1.0                # no wait on the GUI thread
        assert closed is False and 500 in retries             # held open, retried
        assert "Waiting for Step2 segmentation to stop" in w.prev_status.text()
        _wait_for(lambda: worker.isFinished())
        assert w.close() is True                             # the retry now closes
    finally:
        worker.wait(60000)
    _assert_nothing_left(worker, pgid)
