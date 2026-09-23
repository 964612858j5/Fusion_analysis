"""seg_runner: engines in their own subprocess (plan 7.10, block V0).

Engine tests need the engine libraries in the running interpreter (the
fusion_mesmer environment); they skip elsewhere. Everything is written under
tmp_path; the engine children run with a temporary working directory.
"""
import json
import os
import signal
import threading

# The direct-call references import StarDist in THIS process. Keras picks its
# backend at first import and defaults to torch in some environments, which
# csbdeep refuses; the application sets the same variable before loading
# StarDist (workers/cellpose_worker.py:181), the engine child gets it from
# the client.
os.environ.setdefault("KERAS_BACKEND", "tensorflow")

import numpy as np
import pytest

from block01.seg_runner import protocol, synthetic
from block01.seg_runner.client import EngineProcess


# ── protocol, no engine ─────────────────────────────────────────────────────
def test_messages_round_trip_and_carry_the_version():
    msg = protocol.decode(protocol.encode("result", task_id="t1", record="/r.json"))
    assert msg == {"protocol_version": protocol.PROTOCOL_VERSION, "type": "result",
                   "task_id": "t1", "record": "/r.json"}


def test_a_line_that_is_not_a_message_is_refused():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("Found model '2D_versatile_fluo' for 'StarDist2D'.")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(json.dumps({"protocol_version": 999, "type": "hello"}))


def test_published_files_are_complete_and_leave_no_temporary(tmp_path):
    arr = np.arange(12, dtype=np.uint32).reshape(3, 4)
    protocol.publish_array(str(tmp_path / "m.npy"), arr)
    protocol.publish_json(str(tmp_path / "r.json"), {"a": 1})
    assert np.array_equal(np.load(tmp_path / "m.npy"), arr)
    assert json.loads((tmp_path / "r.json").read_text()) == {"a": 1}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.npy", "r.json"]


def test_cellpose_engine_leaves_the_callers_array_untouched():
    pytest.importorskip("cellpose")
    from block01.seg_runner.engines import CellposeEngine
    img = synthetic.wholecell_rgb()
    keep = img.copy()
    CellposeEngine().predict(img, {"diameter": None})
    assert np.array_equal(img, keep)


def test_a_terminal_state_is_never_replaced():
    ep = EngineProcess("stardist")
    ep._settle("t", protocol.CANCELLED, "stopped by user", None)
    ep._settle("t", protocol.FAILED, "engine exited", None)
    assert ep.states["t"]["state"] == protocol.CANCELLED


# ── engines ─────────────────────────────────────────────────────────────────
def _task(tmp_path, tid, image, method, **params):
    inp = tmp_path / f"{tid}.in.npy"
    np.save(inp, image)
    return {"task_id": tid, "input": str(inp), "out_dir": str(tmp_path / "out"),
            "params": dict(params, method=method)}


def _group_gone(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _mask(record_path, kind):
    rec = json.loads(open(record_path).read())
    return np.load(rec[kind]["path"]), rec


@pytest.fixture
def stardist_ok():
    pytest.importorskip("stardist")


def test_stardist_prints_do_not_reach_the_protocol(tmp_path, stardist_ok):
    # StarDist print()s "Found model ..." while loading, before `hello`. If
    # that reached the protocol stream, the client would refuse the line.
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    hello = ep.start()
    ep.close()
    assert hello["type"] == "hello" and hello["engine"] == "stardist"
    assert "Found model" in (tmp_path / "e.log").read_text()


def test_stardist_in_subprocess_equals_direct_call(tmp_path, stardist_ok):
    from csbdeep.utils import normalize
    from stardist.models import StarDist2D
    img = synthetic.nuclei()
    direct, _ = StarDist2D.from_pretrained("2D_versatile_fluo").predict_instances(
        normalize(img, 1, 99.8, axis=(0, 1)))
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([_task(tmp_path, "s1", img, "stardist_nuclei_dapi")])
    ep.close()
    assert states == {"s1": "ok"}
    mask, rec = _mask(ep.states["s1"]["detail"], "nucleus")
    assert np.array_equal(mask, direct.astype(np.uint32))
    assert rec["cell"] == {"status": "not_produced"}


def test_cellpose_in_subprocess_equals_direct_call(tmp_path):
    pytest.importorskip("cellpose")
    import torch
    from cellpose import models
    img = synthetic.wholecell_rgb()
    # A copy: Cellpose rewrites a multi-channel input in place, and `img` is
    # also what the subprocess is given.
    direct, _, _ = models.CellposeModel(gpu=torch.cuda.is_available()).eval(
        img.copy(), diameter=None, flow_threshold=0.4, cellprob_threshold=0.0, min_size=15,
        channel_axis=-1)
    ep = EngineProcess("cellpose", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([_task(tmp_path, "c1", img, "cellpose_wholecell_fusion", diameter=None)])
    ep.close()
    assert states == {"c1": "ok"}
    mask, _ = _mask(ep.states["c1"]["detail"], "cell")
    assert np.array_equal(mask, np.asarray(direct).astype(np.uint32))


def test_mesmer_in_subprocess_equals_direct_call(tmp_path):
    pytest.importorskip("deepcell")
    import tensorflow as tf
    from deepcell.applications import Mesmer
    from block01.seg_runner.engines import MESMER_MODEL_DEFAULT
    img = synthetic.mesmer_pair()
    app = Mesmer(model=tf.keras.models.load_model(MESMER_MODEL_DEFAULT))
    cell = np.squeeze(app.predict(img[None], image_mpp=0.5, compartment="whole-cell"))
    nuc = np.squeeze(app.predict(img[None], image_mpp=0.5, compartment="nuclear"))
    ep = EngineProcess("mesmer", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([_task(tmp_path, "m1", img, "mesmer_nuclear_guided", image_mpp=0.5)])
    ep.close()
    assert states == {"m1": "ok"}
    got_cell, _ = _mask(ep.states["m1"]["detail"], "cell")
    got_nuc, _ = _mask(ep.states["m1"]["detail"], "nucleus")
    assert np.array_equal(got_cell, cell.astype(np.uint32))
    assert np.array_equal(got_nuc, nuc.astype(np.uint32))


def test_a_failing_task_is_failed_and_the_next_one_still_runs(tmp_path, stardist_ok):
    good = _task(tmp_path, "ok1", synthetic.nuclei(), "stardist_nuclei_dapi")
    bad = dict(good, task_id="bad", input=str(tmp_path / "missing.npy"))
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([bad, good])
    ep.close()
    assert states == {"bad": "failed", "ok1": "ok"}
    assert "missing.npy" in ep.states["bad"]["detail"]


def test_stop_cancels_the_rest_and_ends_the_process(tmp_path, stardist_ok):
    img = synthetic.nuclei()
    tasks = [_task(tmp_path, f"t{i}", img, "stardist_nuclei_dapi") for i in range(4)]
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    ep.start()
    pid = ep.proc.pid
    states = ep.run(tasks, on_settle=lambda tid, st, d: ep.stop())
    assert states == {"t0": "ok", "t1": "cancelled", "t2": "cancelled", "t3": "cancelled"}
    assert ep.proc.poll() is not None and _group_gone(pid)


def test_a_killed_engine_fails_its_tasks_instead_of_hanging(tmp_path, stardist_ok):
    big = synthetic.nuclei(1536, 1536)
    tasks = [_task(tmp_path, f"k{i}", big, "stardist_nuclei_dapi") for i in range(2)]
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    ep.start()
    pid = ep.proc.pid
    killer = threading.Timer(0.5, lambda: os.kill(pid, signal.SIGKILL))
    killer.start()
    states = ep.run(tasks)
    killer.join()
    assert states == {"k0": "failed", "k1": "failed"}
    assert "exited with -9" in ep.states["k0"]["detail"]
    assert _group_gone(pid)


def test_close_ends_normally_and_leaves_nothing(tmp_path, stardist_ok):
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    ep.start()
    pid = ep.proc.pid
    ep.close()
    assert ep.proc.returncode == 0
    assert _group_gone(pid)
