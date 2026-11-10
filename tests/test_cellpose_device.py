"""The torch engines' device (docs/v17_cellpose_mac_gpu_plan.md): CUDA
first, then the Mac's GPU (MPS), then the CPU; Step2's "Use GPU" off means
the CPU on every system. Availability is faked: no model is loaded."""

import types

import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner import engines  # noqa: E402


def _fake(monkeypatch, cuda, mps, cpu_only=False):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
    monkeypatch.setattr(torch.backends, "mps", types.SimpleNamespace(is_available=lambda: mps))
    if cpu_only:
        monkeypatch.setenv(engines.CPU_ONLY_ENV, "1")
    else:
        monkeypatch.delenv(engines.CPU_ONLY_ENV, raising=False)


@pytest.mark.parametrize("cuda, mps, cpu_only, want", [
    (True, True, False, "cuda"),          # CUDA first
    (True, False, False, "cuda"),
    (False, True, False, "mps"),          # then the Mac's GPU
    (False, False, False, "cpu"),
    (True, True, True, "cpu"),            # Use GPU off wins everywhere
    (False, True, True, "cpu"),
])
def test_the_device_order(monkeypatch, cuda, mps, cpu_only, want):
    _fake(monkeypatch, cuda, mps, cpu_only)
    assert engines.torch_device() == want


def test_cellpose_on_cuda_is_exactly_the_old_call(monkeypatch):
    _fake(monkeypatch, True, True)
    assert engines.cellpose_model_args() == {"gpu": True}          # Cellpose's own check stays


def test_cellpose_on_the_mac_gpu_names_the_device(monkeypatch):
    _fake(monkeypatch, False, True)
    args = engines.cellpose_model_args()
    assert args["gpu"] is True and str(args["device"]) == "mps"


def test_cellpose_on_the_cpu(monkeypatch):
    _fake(monkeypatch, False, True, cpu_only=True)
    assert engines.cellpose_model_args() == {"gpu": False}


@pytest.mark.parametrize("dev, label", [("cuda", "gpu"), ("mps", "mps"), ("cpu", "cpu")])
def test_mesmer_loads_and_runs_on_the_chosen_device(monkeypatch, dev, label):
    """MesmerEngine hands `torch_device()` to the weights and the network and
    names the device ("gpu" on CUDA, as before)."""
    from block01.seg_runner.mesmer_torch import app, prepare, weights
    seen = {}
    monkeypatch.setattr(engines, "torch_device", lambda: dev)
    monkeypatch.setattr(engines, "manifest_entry", lambda name: {"content_sha256": "x", "derived_from": "k"})
    monkeypatch.setattr(engines, "model_root", lambda entry: "/nowhere")
    monkeypatch.setattr(engines, "model_path", lambda name: "/nowhere")
    monkeypatch.setattr(engines, "_file_sha256", lambda path: "f")
    monkeypatch.setattr(prepare, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(weights, "load", lambda path, d, digest: seen.setdefault("load", d) and
                        {"tensors": {}, "eps": {}, "content_sha256": "x"})

    class _App:
        def __init__(self, params, d):
            seen["app"] = d
    monkeypatch.setattr(app, "MesmerTorch", _App)
    eng = engines.MesmerEngine()
    assert seen == {"load": dev, "app": dev} and eng.device == label


def test_the_client_tells_the_child_before_torch_starts(monkeypatch, tmp_path):
    """`EngineProcess.start` puts the CPU-only switch and the MPS fallback in
    the child's environment -- the Popen call itself, before any import."""
    from block01.seg_runner import client
    got = []

    class _Stop(Exception):
        pass

    def popen(cmd, **kw):
        got.append(dict(kw["env"]))
        raise _Stop()
    monkeypatch.setattr(client.subprocess, "Popen", popen)
    monkeypatch.delenv(engines.CPU_ONLY_ENV, raising=False)    # the client must set it itself
    monkeypatch.setenv("PYTORCH_ENABLE_MPS_FALLBACK", "0")     # ...and the fallback, whatever was there
    with pytest.raises(_Stop):
        client.EngineProcess("cellpose", cpu_only=True).start()
    monkeypatch.setenv(engines.CPU_ONLY_ENV, "1")              # a stale value must not leak
    with pytest.raises(_Stop):
        client.EngineProcess("cellpose", cpu_only=False).start()
    on, off = got
    assert on[engines.CPU_ONLY_ENV] == "1" and on["CUDA_VISIBLE_DEVICES"] == ""
    assert engines.CPU_ONLY_ENV not in off
    assert on["PYTORCH_ENABLE_MPS_FALLBACK"] == off["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
