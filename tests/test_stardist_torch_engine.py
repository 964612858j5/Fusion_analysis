"""The StarDist engine on PyTorch (docs/stardist_torch_plan.md §9, §12.3):
identity, provenance, the TensorFlow isolation and strict weight loading, in
the real engine process.

The mask equality with the in-process port is the wiring check, in
`tests/test_seg_runner.py`. The comparison with TensorFlow is the diagnostic
in `scripts/stardist_consistency.py`; it asserts nothing here.
"""
import pathlib
import sys

import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner import engines  # noqa: E402
from block01.seg_runner import runner as runner_module  # noqa: E402
from block01.seg_runner import synthetic  # noqa: E402
from block01.seg_runner.client import EngineProcess, EngineStartError  # noqa: E402
from block01.seg_runner.stardist_torch import graph, weights  # noqa: E402


def _have_weights():
    try:
        engines.model_path(engines.STARDIST_TORCH_ENTRY)
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))


def test_the_engine_declares_the_torch_identity_and_its_artifact(tmp_path):
    _have_weights()
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"), cpu_only=True)
    hello = ep.start()
    ep.close()

    identity = hello["identity"]
    assert identity["behavior_version"] == 2            # user ruling 2026-11-10
    assert identity["model_id"] == "stardist_2D_versatile_fluo"
    assert hello["device"] == "cpu"                     # cpu_only
    provenance = hello["provenance"]
    assert provenance["device"] == "cpu"
    assert provenance["lib_versions"]["stardist_backend"] == "torch"
    assert provenance["lib_versions"]["tf_gpu_hidden"] is True
    artifact = provenance["model_artifact"]
    assert artifact["entry"] == engines.STARDIST_TORCH_ENTRY
    assert len(artifact["content_sha256"]) == 64
    assert artifact["source"]["model_key"] == "stardist_2D_versatile_fluo"


def test_the_code_hash_covers_the_new_package(tmp_path):
    """`runner_version` is the README's 'is the engine code that ran the code
    in the tree': it must include seg_runner/stardist_torch/*.py."""
    _have_weights()
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"), cpu_only=True)
    hello = ep.start()
    ep.close()
    package = pathlib.Path(runner_module.__file__).parent
    expected = runner_module._sha256_files(
        sorted(package.glob("*.py"))
        + sorted(package.glob("mesmer_torch/*.py"))
        + sorted(package.glob("stardist_torch/*.py")))
    assert hello["provenance"]["runner_version"] == expected


def test_without_the_cpu_only_flag_the_engine_follows_torch_device(tmp_path):
    _have_weights()
    chosen = engines.torch_device()
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"))
    hello = ep.start()
    ep.close()
    assert hello["device"] == {"cuda": "gpu"}.get(chosen, chosen)


def test_two_tasks_in_one_process_give_the_same_mask(tmp_path):
    _have_weights()
    import numpy as np

    image = synthetic.nuclei()
    inputs = []
    for tid in ("a", "b"):
        path = tmp_path / f"{tid}.npy"
        np.save(path, image)
        inputs.append(path)
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"), cpu_only=True)
    ep.start()
    states = ep.run([{"task_id": tid, "input": str(path), "out_dir": str(tmp_path / "out"),
                      "params": {"method": "stardist_nuclei_dapi"}}
                     for tid, path in zip(("a", "b"), inputs)])
    ep.close()
    assert states == {"a": "ok", "b": "ok"}
    masks = []
    for tid in ("a", "b"):
        record = ep.states[tid]["detail"]
        import json
        with open(record, encoding="utf-8") as f:
            masks.append(np.load(json.load(f)["nucleus"]["path"]))
    assert np.array_equal(masks[0], masks[1])


def test_weights_that_do_not_match_the_manifest_stop_the_engine(tmp_path, monkeypatch):
    bad = tmp_path / "weights"
    bad.mkdir()
    torch.save({"format": weights.FORMAT,
                "tensors": {k: torch.zeros(shape) for k, shape in graph.expected_shapes().items()}},
               bad / weights.WEIGHTS_FILE)
    monkeypatch.setenv("BLOCK01_STARDIST_TORCH_PATH", str(bad))
    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"), cpu_only=True)
    with pytest.raises(EngineStartError) as info:
        ep.start()
    assert "other weights" in str(info.value) or "content" in str(info.value)


# ── the TensorFlow isolation must REFUSE, not just log (plan §6.5) ──────────
class _Device:
    """What `tf.config.list_*_devices` returns: objects with a `name`."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


class _FakeTensorflow:
    """Enough of `tf.config` for `engines._hide_tensorflow_gpu`."""

    def __init__(self, gpus=0, hide_raises=False, still_visible=False):
        self._gpus = gpus
        self._hide_raises = hide_raises
        self._still_visible = still_visible
        self.hidden = False
        self.config = self

    def list_physical_devices(self, kind=None):
        if kind != "GPU":
            return [_Device("/physical_device:CPU:0")]
        return [_Device(f"/physical_device:GPU:{i}") for i in range(self._gpus)]

    def set_visible_devices(self, devices, kind=None):
        if self._hide_raises:
            raise RuntimeError("GPU already initialised")
        self.hidden = True

    def list_logical_devices(self, kind=None):
        if kind != "GPU":
            return []
        if self._still_visible or not self.hidden:
            return [_Device(f"/device:GPU:{i}") for i in range(self._gpus)]
        return []


def test_without_a_tensorflow_gpu_nothing_is_hidden(monkeypatch):
    monkeypatch.setitem(sys.modules, "tensorflow", _FakeTensorflow(gpus=0))
    assert engines._hide_tensorflow_gpu() == {"physical_before": [], "logical_after": []}


def test_the_engine_refuses_when_tensorflow_still_sees_a_gpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "tensorflow",
                        _FakeTensorflow(gpus=1, still_visible=True))
    with pytest.raises(RuntimeError) as info:
        engines._hide_tensorflow_gpu()
    assert "still sees GPU devices" in str(info.value)


def test_the_engine_refuses_when_the_gpu_cannot_be_hidden(monkeypatch):
    monkeypatch.setitem(sys.modules, "tensorflow",
                        _FakeTensorflow(gpus=1, hide_raises=True))
    with pytest.raises(RuntimeError) as info:
        engines._hide_tensorflow_gpu()
    assert "before it could be hidden" in str(info.value)


def test_the_engine_constructor_refuses_before_loading_anything(monkeypatch):
    monkeypatch.setitem(sys.modules, "tensorflow",
                        _FakeTensorflow(gpus=1, still_visible=True))
    with pytest.raises(RuntimeError):
        engines.StardistEngine()


def test_a_version_1_step1_result_is_reported_as_another_behaviour():
    """What Step2's warning is driven by (plan §5): a Step1 result recorded
    with behavior_version 1 differs from the engine this code runs now."""
    old = {"version": 2, "engine": "stardist", "behavior_version": 1,
           "model_id": "stardist_2D_versatile_fluo"}
    current = engines.behavior_identity("stardist")
    assert current["behavior_version"] == 2
    comparison = engines.compare_identity(old, current)
    assert comparison["engine_differs"] is False
    assert comparison["differences"] == [["behavior_version", 1, 2]]
