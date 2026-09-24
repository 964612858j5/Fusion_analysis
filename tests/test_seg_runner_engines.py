"""seg_runner's engine-side post-processing (plan block C, step 2).

Expansion keeps the nuclei from before it; the Mesmer thresholds reach
DeepCell's post-processing of the method's primary output and change the
result; `postprocess_mask` (min size) runs on the primary output only; the
Mesmer model comes from the model manifest or not at all. Engine runs go
through the real subprocess and are compared pixel for pixel with a direct
call in the same interpreter; they skip where the engine library is missing
(the Mesmer ones need the fusion_mesmer environment).
"""
import json
import os

os.environ.setdefault("KERAS_BACKEND", "tensorflow")

import numpy as np
import pytest

from block01.seg_runner import engines, synthetic
from block01.seg_runner.client import EngineProcess
from block01.utils import segmentation_param_schema as ps


def _task(tmp_path, tid, image, method, **params):
    inp = tmp_path / f"{tid}.in.npy"
    np.save(inp, image)
    return {"task_id": tid, "input": str(inp), "out_dir": str(tmp_path / "out"),
            "params": dict(params, method=method)}


def _masks(ep, tid):
    rec = json.loads(open(ep.states[tid]["detail"]).read())
    return {k: (np.load(rec[k]["path"]) if rec[k]["status"] == "ok" else None)
            for k in ("cell", "nucleus")}, rec


# ── the output table ────────────────────────────────────────────────────────
def test_every_ui_method_has_the_outputs_of_the_plan_table():
    assert set(engines.METHOD_OUTPUTS) == set(ps.UI_METHODS)
    both = {m for m, o in engines.METHOD_OUTPUTS.items() if set(o) == {"cell", "nucleus"}}
    assert both == {"cellpose_nuclei_expansion", "stardist_nuclei_expansion",
                    "mesmer_nuclear_guided"}
    assert engines.METHOD_OUTPUTS["mesmer_whole_cell"] == ("cell",)


# ── expansion ───────────────────────────────────────────────────────────────
def test_stardist_expansion_returns_the_nuclei_from_before_expanding(tmp_path):
    pytest.importorskip("stardist")
    from csbdeep.utils import normalize
    from skimage.segmentation import expand_labels
    from stardist.models import StarDist2D
    img = synthetic.nuclei()
    direct, _ = StarDist2D.from_pretrained("2D_versatile_fluo").predict_instances(
        normalize(img, 1, 99.8, axis=(0, 1)))
    direct = direct.astype(np.uint32)
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([_task(tmp_path, "x8", img, "stardist_nuclei_expansion", expand_distance=8.0),
                     _task(tmp_path, "x0", img, "stardist_nuclei_expansion", expand_distance=0.0)])
    ep.close()
    assert states == {"x8": "ok", "x0": "ok"}
    m8, rec = _masks(ep, "x8")
    assert np.array_equal(m8["nucleus"], direct)
    assert np.array_equal(m8["cell"], expand_labels(direct, distance=8.0).astype(np.uint32))
    assert not np.array_equal(m8["cell"], m8["nucleus"])
    assert rec["cell"]["count"] == rec["nucleus"]["count"]         # one cell per nucleus
    m0, _ = _masks(ep, "x0")
    assert np.array_equal(m0["cell"], direct) and np.array_equal(m0["nucleus"], direct)


def test_cellpose_expansion_returns_the_nuclei_from_before_expanding(tmp_path):
    pytest.importorskip("cellpose")
    import torch
    from cellpose import models
    from skimage.segmentation import expand_labels
    img = synthetic.nuclei()
    direct, _, _ = models.CellposeModel(gpu=torch.cuda.is_available()).eval(
        img.copy(), diameter=None, flow_threshold=0.4, cellprob_threshold=0.0, min_size=15)
    direct = np.asarray(direct).astype(np.uint32)
    ep = EngineProcess("cellpose", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([_task(tmp_path, "c", img, "cellpose_nuclei_expansion", diameter=None,
                           expand_distance=5.0)])
    ep.close()
    assert states == {"c": "ok"}
    m, _ = _masks(ep, "c")
    assert np.array_equal(m["nucleus"], direct)
    assert np.array_equal(m["cell"], expand_labels(direct, distance=5.0).astype(np.uint32))


# ── Mesmer, without DeepCell: what reaches app.predict ──────────────────────
class _App:
    def __init__(self):
        self.calls = []

    def predict(self, batch, **kw):
        self.calls.append(kw)
        out = np.zeros(batch.shape[:3] + (1,), np.int64)
        out[0, 0:2, 0:2, 0] = 1                  # 4 px
        out[0, 5:9, 5:9, 0] = 2                  # 16 px
        return out


def _fake_mesmer():
    eng = object.__new__(engines.MesmerEngine)
    eng._app, eng.device = _App(), "cpu"
    return eng


@pytest.mark.parametrize("method,cell_kw,nuc_kw", [
    ("mesmer_whole_cell", {"maxima_threshold": 0.3, "interior_threshold": 0.5}, None),
    ("mesmer_nuclei", None, {"maxima_threshold": 0.3, "interior_threshold": 0.5}),
    ("mesmer_nuclear_guided", {"maxima_threshold": 0.3, "interior_threshold": 0.5}, {}),
])
def test_the_listed_thresholds_reach_the_primary_outputs_postprocessing(method, cell_kw, nuc_kw):
    eng = _fake_mesmer()
    img = np.zeros((12, 12, 2), np.float32)
    out = engines.run(eng, method, img, {"maxima_threshold": 0.3, "interior_threshold": 0.5,
                                         "image_mpp": 0.65, "postprocess_min_size": 5})
    calls = {c["compartment"]: c for c in eng._app.calls}
    if cell_kw is not None:
        assert calls["whole-cell"]["postprocess_kwargs_whole_cell"] == cell_kw
        assert "postprocess_kwargs_nuclear" not in calls["whole-cell"]
    if nuc_kw is not None:
        assert calls["nuclear"]["postprocess_kwargs_nuclear"] == nuc_kw
    assert all(c["image_mpp"] == 0.65 and "preprocess_kwargs" not in c for c in eng._app.calls)
    # min size 5 drops the 4-px object from the primary output only
    primary = "nucleus" if method == "mesmer_nuclei" else "cell"
    assert set(np.unique(out[primary])) == {0, 2}
    if method == "mesmer_nuclear_guided":
        assert set(np.unique(out["nucleus"])) == {0, 1, 2}


def test_postprocess_mask_is_step1s_min_size_filter():
    from block01.utils.mesmer_utils import postprocess_mask as step1
    rng = np.random.default_rng(3)
    for min_size in (0, 1, 7, 30):
        m = np.zeros((60, 60), np.uint32)
        for k in range(1, 40):
            y, x = rng.integers(0, 55, 2)
            dy, dx = rng.integers(1, 7, 2)
            m[y:y + dy, x:x + dx] = k
        assert np.array_equal(engines.postprocess_mask(m, min_size), step1(m, min_size=min_size))


# ── the Mesmer model comes from the manifest ────────────────────────────────
def _manifest(tmp_path, root, files, env=None):
    entry = {"engine": "mesmer", "root": str(root), "files": files}
    if env:
        entry["env_override"] = env
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"schema": 1, "models": {"mesmer_multiplex_segmentation": entry}}))
    return p


def test_the_mesmer_model_path_is_the_manifests_and_checked(tmp_path, monkeypatch):
    root = tmp_path / "model"
    (root / "variables").mkdir(parents=True)
    (root / "saved_model.pb").write_bytes(b"x" * 10)
    files = [{"path": "saved_model.pb", "size": 10}]
    assert engines.mesmer_model_path(_manifest(tmp_path, root, files)) == str(root)
    with pytest.raises(engines.ModelMissingError, match="wrong size"):
        engines.mesmer_model_path(_manifest(tmp_path, root, [{"path": "saved_model.pb", "size": 11}]))
    with pytest.raises(engines.ModelMissingError, match="missing"):
        engines.mesmer_model_path(_manifest(tmp_path, root, files + [{"path": "variables/v.index"}]))
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("MY_MESMER", str(other))
    with pytest.raises(engines.ModelMissingError, match="elsewhere"):       # no fallback
        engines.mesmer_model_path(_manifest(tmp_path, root, files, env="MY_MESMER"))
    with pytest.raises(engines.ModelMissingError, match="no Mesmer entry"):
        engines.mesmer_model_path(tmp_path / "absent.json")


def test_the_repo_manifest_names_the_installed_mesmer_model():
    pytest.importorskip("deepcell")
    assert os.path.isdir(engines.mesmer_model_path())


# ── Mesmer, for real ────────────────────────────────────────────────────────
def test_mesmer_thresholds_change_the_result_and_match_a_direct_call(tmp_path):
    pytest.importorskip("deepcell")
    import tensorflow as tf
    from deepcell.applications import Mesmer
    img = synthetic.mesmer_pair()
    app = Mesmer(model=tf.keras.models.load_model(engines.mesmer_model_path()))
    kw = {"maxima_threshold": 0.3, "interior_threshold": 0.5}
    default = np.squeeze(app.predict(img[None], image_mpp=0.5, compartment="whole-cell"))
    tuned = np.squeeze(app.predict(img[None], image_mpp=0.5, compartment="whole-cell",
                                   postprocess_kwargs_whole_cell=kw)).astype(np.uint32)
    assert len(np.unique(tuned)) != len(np.unique(default))
    ep = EngineProcess("mesmer", log_path=str(tmp_path / "e.log"))
    ep.start()
    states = ep.run([_task(tmp_path, "m", img, "mesmer_whole_cell", image_mpp=0.5, **kw)])
    ep.close()
    assert states == {"m": "ok"}
    m, rec = _masks(ep, "m")
    assert np.array_equal(m["cell"], tuned) and m["nucleus"] is None
    assert rec["nucleus"] == {"status": "not_produced"}
