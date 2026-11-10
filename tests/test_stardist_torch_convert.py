"""StarDist on PyTorch, step 1: the conversion from the installed Keras weights.

Everything this module writes goes into tmp_path: the converted file, the
copies of the source model and the scratch output directory. The installed
model directory is only read. The last tests run the hand converter exactly as
`scripts/install_block01.sh` does -- with the entry paths taken from the two
environment variables -- into a temporary directory.
"""
import json
import os
import subprocess
import sys

import h5py
import numpy as np
import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner import engines  # noqa: E402
from block01.seg_runner.stardist_torch import convert, weights  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(engines.__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)
SCRIPT = os.path.join(SCRIPTS, "convert_stardist_to_torch.py")
MODEL_KEY = "stardist_2D_versatile_fluo"
ENTRY = engines.STARDIST_TORCH_ENTRY


def _model_dir():
    try:
        return engines.model_path(MODEL_KEY)
    except engines.ModelMissingError:
        pytest.skip("no installed StarDist model (BLOCK01_STARDIST_MODEL_PATH)")


def _copy_source(tmp_path, name="src", **config_changes):
    """A source folder with the two files the conversion reads."""
    src = tmp_path / name
    src.mkdir()
    real = _model_dir()
    config = json.loads(open(os.path.join(real, "config.json"), encoding="utf-8").read())
    config.update(config_changes)
    (src / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with open(os.path.join(real, "weights_best.h5"), "rb") as f, open(src / "weights_best.h5", "wb") as g:
        g.write(f.read())
    return src


def _zero_kernel(h5_path, layer):
    """Same shapes, other values: the trick a wrong model folder plays."""
    with h5py.File(h5_path, "r+") as f:
        f[layer][f"{layer}/kernel:0"][...] = 0.0


def test_the_conversion_reaches_the_manifests_content_digest(tmp_path):
    entry = engines.manifest_entry(ENTRY)
    digest = convert.convert_dir(_model_dir(), tmp_path)
    assert digest == entry["content_sha256"]
    assert digest == weights.content_digest(weights.read(tmp_path / weights.WEIGHTS_FILE))
    assert sorted(p.name for p in tmp_path.iterdir()) == [weights.WEIGHTS_FILE]  # no temp left


def test_a_second_run_reuses_the_file(tmp_path):
    convert.convert_dir(_model_dir(), tmp_path)
    target = tmp_path / weights.WEIGHTS_FILE
    before = (target.stat().st_mtime_ns, target.stat().st_ino)
    digest = convert.convert_dir(_model_dir(), tmp_path)
    assert digest == engines.manifest_entry(ENTRY)["content_sha256"]
    assert (target.stat().st_mtime_ns, target.stat().st_ino) == before


def test_another_file_needs_force(tmp_path):
    target = tmp_path / weights.WEIGHTS_FILE
    torch.save({"format": weights.FORMAT, "tensors": {}}, target)
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(_model_dir(), tmp_path)
    assert weights.read(target)["format"] == weights.FORMAT         # untouched, not deleted
    digest = convert.convert_dir(_model_dir(), tmp_path, force=True)
    assert digest == engines.manifest_entry(ENTRY)["content_sha256"]


def test_a_different_architecture_is_refused(tmp_path):
    src = _copy_source(tmp_path, unet_n_depth=4)
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(src, tmp_path / "out")
    assert not (tmp_path / "out" / weights.WEIGHTS_FILE).exists()


def test_a_missing_weights_file_is_an_error(tmp_path):
    src = _copy_source(tmp_path)
    (src / "weights_best.h5").unlink()
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(src, tmp_path / "out")


def test_an_incomplete_weights_file_is_refused(tmp_path):
    src = _copy_source(tmp_path)
    with h5py.File(src / "weights_best.h5", "r+") as f:
        del f["features"]
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(src, tmp_path / "out")
    assert not (tmp_path / "out" / weights.WEIGHTS_FILE).exists()


def test_a_failed_self_check_publishes_nothing(tmp_path, monkeypatch):
    def boom(data):
        raise convert.ConvertError("self-check failed")

    monkeypatch.setattr(convert, "self_check", boom)
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(_model_dir(), tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_the_converter_runs_from_the_two_environment_variables(tmp_path):
    """What the installer does: source and output from the entry variables."""
    out = tmp_path / "weights"
    env = dict(os.environ, BLOCK01_STARDIST_MODEL_PATH=_model_dir(),
               BLOCK01_STARDIST_TORCH_PATH=str(out))
    done = subprocess.run([sys.executable, SCRIPT], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    assert (out / weights.WEIGHTS_FILE).is_file()
    assert weights.content_digest(weights.read(out / weights.WEIGHTS_FILE)) == \
        engines.manifest_entry(ENTRY)["content_sha256"]


def test_the_manifest_tool_reads_each_engines_own_format(tmp_path):
    import model_manifest
    digest = convert.convert_dir(_model_dir(), tmp_path)
    assert model_manifest._content_digest(tmp_path / weights.WEIGHTS_FILE, "stardist") == digest


def test_the_manifest_entry_and_the_tool_table_agree():
    import model_manifest
    recorded = engines.manifest_entry(ENTRY)
    spec = model_manifest.MODELS[ENTRY]
    assert spec["generated"] is True
    for key in ("engine", "root", "env_override", "derived_from"):
        assert recorded[key] == spec[key]
    assert [f["path"] for f in recorded["files"]] == spec["files"]
    assert len(recorded["content_sha256"]) == 64


def test_a_source_that_is_not_the_manifest_model_writes_nothing(tmp_path):
    """P2: the expected digest is checked BEFORE the one publish point."""
    src = _copy_source(tmp_path)
    _zero_kernel(src / "weights_best.h5", "features")
    out = tmp_path / "out"
    expected = engines.manifest_entry(ENTRY)["content_sha256"]
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(src, out, expected_digest=expected)
    assert not out.exists()


def test_a_wrong_source_never_replaces_the_good_file(tmp_path):
    out = tmp_path / "out"
    digest = convert.convert_dir(_model_dir(), out)
    good = weights.content_digest(weights.read(out / weights.WEIGHTS_FILE))
    assert good == digest
    src = _copy_source(tmp_path, name="other")
    _zero_kernel(src / "weights_best.h5", "middle_0")
    with pytest.raises(convert.ConvertError):
        convert.convert_dir(src, out, force=True, expected_digest=digest)
    assert weights.content_digest(weights.read(out / weights.WEIGHTS_FILE)) == good
    assert sorted(p.name for p in out.iterdir()) == [weights.WEIGHTS_FILE]


def test_update_manifest_records_a_new_digest_in_a_scratch_manifest(tmp_path, monkeypatch):
    """--update-manifest writes the manifest it is given, never the repository's."""
    import importlib.util

    import model_manifest
    repo_manifest = os.path.join(REPO, "envs", "fusion_mesmer", "models.json")
    with open(repo_manifest, "rb") as f:
        repo_bytes = f.read()
    scratch = tmp_path / "models.json"
    scratch.write_bytes(repo_bytes)
    monkeypatch.setattr(model_manifest, "MANIFEST", scratch)

    src = _copy_source(tmp_path, name="other")
    _zero_kernel(src / "weights_best.h5", "dist")
    out = tmp_path / "weights"
    monkeypatch.setenv("BLOCK01_STARDIST_MODEL_PATH", str(src))
    monkeypatch.setenv("BLOCK01_STARDIST_TORCH_PATH", str(out))

    spec = importlib.util.spec_from_file_location("convert_stardist_script", SCRIPT)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(["--update-manifest"]) == 0

    recorded = json.loads(scratch.read_text(encoding="utf-8"))["models"][ENTRY]["content_sha256"]
    original = json.loads(repo_bytes)["models"][ENTRY]["content_sha256"]
    assert recorded != original                       # the changed source made other weights
    assert recorded == weights.content_digest(weights.read(out / weights.WEIGHTS_FILE))
    with open(repo_manifest, "rb") as f:
        assert f.read() == repo_bytes                 # the repository's manifest is untouched
