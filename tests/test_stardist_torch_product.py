"""Layer 3 of docs/stardist_torch_plan.md §8 on the real engine, CPU and GPU:

* the engine process's masks against the same torch port in this process, on
  the SAME device (the wiring check), and a repeat run in one process;
* a real Step2 run through `SegmentMergeWorker` on a synthetic project, with
  the device and the weights it used recorded.

Step2's worker writes its label pyramids only when the project has a raw OME
with a pyramid (`workers/segment_merge_worker.py:_write_label_pyramids`), so
the synthetic project here gets one; without it the run finishes its masks but
never writes its meta (that is why `tests/test_step2_runner_path.py` fails on
this machine, on HEAD too).
"""
import json
import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("KERAS_BACKEND", "tensorflow")

import numpy as np
import pytest

pytest.importorskip("block01.qt")
numpy = pytest.importorskip("numpy")
zarr = pytest.importorskip("zarr")
tifffile = pytest.importorskip("tifffile")
from block01.qt import QtWidgets  # noqa: E402

import test_step2_runner_path as rp  # noqa: E402

from block01.seg_runner import engines, synthetic  # noqa: E402
from block01.seg_runner.stardist_torch import network, weights  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _weights():
    try:
        root = engines.model_path(engines.STARDIST_TORCH_ENTRY)
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))
    entry = engines.manifest_entry(engines.STARDIST_TORCH_ENTRY)
    return os.path.join(root, weights.WEIGHTS_FILE), entry.get("content_sha256")


def _engine_label(device):
    return {"cuda": "gpu"}.get(device, device)


def _direct(device):
    """The same model in this process, on `device`, for the wiring check."""
    pytest.importorskip("stardist")
    weights_path, digest = _weights()
    folder = os.environ.get("BLOCK01_STARDIST_MODEL_PATH")
    if not folder:
        pytest.skip("no installed StarDist model")
    from stardist.models import StarDist2D

    engines._hide_tensorflow_gpu()          # no TF GPU next to the torch one (§6.5)
    folder = os.path.abspath(os.path.expanduser(folder))
    model = StarDist2D(None, name=os.path.basename(folder), basedir=os.path.dirname(folder))
    model.keras_model = network.TorchNetwork(weights.load(weights_path, device, digest), device)

    def run(image):
        from csbdeep.utils import normalize
        labels, _ = model.predict_instances(normalize(image, 1, 99.8, axis=(0, 1)))
        return labels
    return run


def _task(tmp_path, tid, image):
    path = tmp_path / f"{tid}.npy"
    np.save(path, image)
    return {"task_id": tid, "input": str(path), "out_dir": str(tmp_path / "out"),
            "params": {"method": "stardist_nuclei_dapi"}}


def _mask(record_path):
    with open(record_path, encoding="utf-8") as f:
        return np.load(json.load(f)["nucleus"]["path"])


@pytest.mark.parametrize("wanted", ["cpu", "device"])
def test_the_engine_equals_the_torch_port_on_the_same_device(tmp_path, wanted):
    from block01.seg_runner.client import EngineProcess

    weights_path, digest = _weights()                      # skip before anything else
    chosen = engines.torch_device()
    if wanted == "cpu":
        device, kwargs = "cpu", {"cpu_only": True}
    else:
        if chosen == "cpu":
            pytest.skip("no GPU on this machine")
        device, kwargs = chosen, {}
    direct = _direct(device)
    image = synthetic.nuclei()
    expected = direct(image).astype(np.uint32)

    ep = EngineProcess("stardist", log_path=str(tmp_path / "e.log"), **kwargs)
    hello = ep.start()
    ep.run([_task(tmp_path, "a", image), _task(tmp_path, "b", image)])
    ep.close()

    assert hello["device"] == _engine_label(device)
    for tid in ("a", "b"):
        assert np.array_equal(_mask(ep.states[tid]["detail"]), expected), tid


def _raw_ome(path, image):
    """A raw slide with a pyramid: what Step2 needs to write label pyramids.
    `image` is the fused window Step2 reads, HWC; a slide is CYX."""
    data = np.asarray(image)
    data = np.moveaxis(data, -1, 0) if data.ndim == 3 else data[None]
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, metadata={"axes": "CYX"}, subifds=2)
        tw.write(data[:, ::2, ::2], subfiletype=1)
        tw.write(data[:, ::4, ::4], subfiletype=1)
    return str(path)


@pytest.mark.parametrize("wanted", ["cpu", "device"])
def test_step2_runs_through_the_engine_and_records_the_device(app, tmp_path, wanted):
    pytest.importorskip("stardist")
    _weights()
    chosen = engines.torch_device()
    if wanted == "cpu":
        use_gpu, label = False, "cpu"
    else:
        if chosen == "cpu":
            pytest.skip("no GPU on this machine")
        use_gpu, label = True, _engine_label(chosen)

    image = rp._image()
    zp = rp._fused_zarr(tmp_path, image)
    cfg = rp._contract_config(tmp_path, "stardist_nuclei_dapi")
    cfg["raw_ome_path"] = _raw_ome(tmp_path / "slide.ome.tif", image)
    # Step2's own switch: EngineProcess(cpu_only=not use_gpu). Setting
    # BLOCK01_ENGINE_CPU_ONLY in the environment does NOT work -- client.start()
    # removes it when cpu_only is false (seg_runner/client.py).
    cfg["use_gpu"] = use_gpu
    worker = rp._worker(tmp_path, zp, cfg)
    worker.run()

    meta_path = os.path.join(worker.output_dir, "segmentation_meta.json")
    assert os.path.isfile(meta_path), "the run wrote no meta (see the worker's log)"
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    engine_meta = meta["seg_engine"]
    assert engine_meta["engine"] == "stardist"
    assert engine_meta["device"] == label
    assert engine_meta["identity"]["behavior_version"] == 2
    assert engine_meta["provenance"]["model_artifact"]["entry"] == engines.STARDIST_TORCH_ENTRY
    masks = [p for p in pathlib.Path(worker.output_dir).rglob("*mask*.zarr")]
    assert masks, "the run wrote no mask"
