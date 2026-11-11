"""Shared test set-up.

The Mesmer engine makes or refreshes files in its torch-weights directory at
start (docs/mesmer_torch_plan.md §10). No test may write into the real one:
for the whole session BLOCK01_MESMER_TORCH_PATH points at a scratch
directory holding a link to the real weights (when present), so engines --
in this process or in their own -- read the real file and write beside the
link. A test that sets its own override still wins.

The StarDist engine only READS its torch weights (docs/stardist_torch_plan.md
§4.5: the installer converts them, the engine does not), but it gets the same
treatment: BLOCK01_STARDIST_TORCH_PATH points at a scratch directory with a
link to the real file, so no test depends on the deployment's own directory.

Step0's existing tests were written against the Python picture of the full
image; since Step0 reads the shared backend switch (docs/v17_step0_engine_plan.md
§3.2, §10.2) they keep it by default: `BLOCK01_STEP1_BACKEND=python` for each
`test_step0_*` test that is not a `test_step0_engine_*` one, unless the
environment already names a backend. A test that sets it still wins.

Background correction on a Mac's GPU (docs/v17_step0_mac_gpu_correction_plan.md)
is off (`BLOCK01_BG_MPS=0`) for every test but `test_mps_correction.py`: the
older tests check the CPU's record and arithmetic.
"""
import gc
import json
import os
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_ENV = "BLOCK01_MESMER_TORCH_PATH"
_STARDIST_ENV = "BLOCK01_STARDIST_TORCH_PATH"


@pytest.fixture(scope="session", autouse=True)
def _mesmer_weights_in_scratch(tmp_path_factory):
    entry = json.loads((_REPO / "envs" / "fusion_mesmer" / "models.json").read_text())[
        "models"]["mesmer_multiplex_segmentation_torch"]
    real = pathlib.Path(os.path.expanduser(os.environ.get(_ENV) or entry["root"]))
    scratch = tmp_path_factory.mktemp("mesmer_torch")
    for f in entry["files"]:
        if (real / f["path"]).is_file():
            (scratch / f["path"]).symlink_to(real / f["path"])
    old = os.environ.get(_ENV)
    os.environ[_ENV] = str(scratch)
    yield scratch
    if old is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = old


@pytest.fixture(scope="session", autouse=True)
def _stardist_weights_in_scratch(tmp_path_factory):
    entry = json.loads((_REPO / "envs" / "fusion_mesmer" / "models.json").read_text())[
        "models"]["stardist_2D_versatile_fluo_torch"]
    real = pathlib.Path(os.path.expanduser(os.environ.get(_STARDIST_ENV) or entry["root"]))
    scratch = tmp_path_factory.mktemp("stardist_torch")
    for f in entry["files"]:
        if (real / f["path"]).is_file():
            (scratch / f["path"]).symlink_to(real / f["path"])
    old = os.environ.get(_STARDIST_ENV)
    os.environ[_STARDIST_ENV] = str(scratch)
    yield scratch
    if old is None:
        os.environ.pop(_STARDIST_ENV, None)
    else:
        os.environ[_STARDIST_ENV] = old


@pytest.fixture
def stardist_direct():
    """`predict_instances` with the torch network IN THIS PROCESS, on the CPU.

    It is the reference for the "engine == direct" wiring checks: the same
    model the engine runs, with the same weights and the same device (the
    engine side passes `cpu_only=True`), so a difference means the engine's
    path differs, not the arithmetic. Skips when the installed model or the
    converted weights are not on this machine.

    Test-scoped, not session-scoped: the model is released at the end of the
    test that needed it. TensorFlow's GPUs are hidden before the Keras model
    is built (plan §6.5), so the reference holds no CUDA context in the pytest
    process either."""
    pytest.importorskip("torch")
    from block01.seg_runner import engines
    from block01.seg_runner.stardist_torch import network, weights

    folder = os.environ.get("BLOCK01_STARDIST_MODEL_PATH")
    if not folder:
        pytest.skip("no installed StarDist model (BLOCK01_STARDIST_MODEL_PATH)")
    try:
        root = engines.model_path(engines.STARDIST_TORCH_ENTRY)
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))
    pytest.importorskip("stardist")
    from stardist.models import StarDist2D

    entry = engines.manifest_entry(engines.STARDIST_TORCH_ENTRY)
    params = weights.load(os.path.join(root, weights.WEIGHTS_FILE), "cpu",
                          entry.get("content_sha256"))
    engines._hide_tensorflow_gpu()          # before the Keras model exists (§6.5)
    folder = os.path.abspath(os.path.expanduser(folder))
    model = StarDist2D(None, name=os.path.basename(folder), basedir=os.path.dirname(folder))
    model.keras_model = network.TorchNetwork(params, "cpu")

    def run(image, **kwargs):
        from csbdeep.utils import normalize
        labels, _ = model.predict_instances(normalize(image, 1, 99.8, axis=(0, 1)), **kwargs)
        return labels

    yield run
    del model
    gc.collect()


@pytest.fixture(autouse=True)
def _step0_tests_keep_the_python_picture(request, monkeypatch):
    name = pathlib.Path(str(request.node.fspath)).name
    if (name.startswith("test_step0_") and not name.startswith("test_step0_engine")
            and "BLOCK01_STEP1_BACKEND" not in os.environ):
        monkeypatch.setenv("BLOCK01_STEP1_BACKEND", "python")


@pytest.fixture(autouse=True)
def _old_tests_keep_the_cpu_correction(request, monkeypatch):
    """Background correction on a Mac's GPU (docs/v17_step0_mac_gpu_correction_plan.md):
    the tests written before it check the CPU's record and arithmetic, so
    every test but `test_mps_correction.py` runs with `BLOCK01_BG_MPS=0`,
    unless the environment already names it. A test that sets it still wins."""
    if (pathlib.Path(str(request.node.fspath)).name != "test_mps_correction.py"
            and "BLOCK01_BG_MPS" not in os.environ):
        monkeypatch.setenv("BLOCK01_BG_MPS", "0")
