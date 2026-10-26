"""Mesmer in PyTorch: the weights made on first use (docs/mesmer_torch_plan.md
§5, §10) -- content digest, `prepare.ensure`, the manifest tool and the
hand-run converter. Everything is written into tmp_path: an override
directory for the weights and a scratch manifest -- never the repository's
models.json or the shared model directory. Most tests replace the TensorFlow
child with a fake that writes synthetic weights; the last one converts the
real Keras model through a real engine start."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

torch = pytest.importorskip("torch")

from block01.seg_runner import engines  # noqa: E402
from block01.seg_runner.mesmer_torch import convert, graph, prepare, weights  # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(engines.__file__))), "scripts")
KEY = engines.MESMER_TORCH_ENTRY
PT = weights.WEIGHTS_FILE


def _synthetic(seed=0, root="/keras/here"):
    g = torch.Generator().manual_seed(seed)
    tensors = {k: torch.randn(shape, generator=g) for k, shape in graph.expected_shapes().items()}
    eps = {name: 1e-3 for name in graph.batch_norm_layers()}
    return {"format": weights.FORMAT, "tensors": tensors, "eps": eps,
            "source": {"model_key": "mesmer_multiplex_segmentation", "root": root,
                       "files": {"saved_model.pb": "cd" * 32}, "head_diffs": [1e-6] * 4}}


DIGEST = weights.content_digest(_synthetic())


class _FakeConverter:
    """Stands in for the TensorFlow child: writes `_synthetic(seed)`."""

    def __init__(self, seed=0, fail=None, delay=0.0):
        self.seed, self.fail, self.delay, self.calls = seed, fail, delay, 0

    def __call__(self, source_root, out, workdir):
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        torch.save(_synthetic(self.seed, str(source_root)), out)


@pytest.fixture
def dirs(tmp_path):
    src = tmp_path / "keras"
    src.mkdir()
    return src, tmp_path / "torch"


def _leftovers(root):
    return sorted(p.name for p in root.glob(prepare.TEMP_PREFIX + "*"))


# ── the content digest ────────────────────────────────────────────────

def test_the_digest_ignores_where_and_when_but_not_what(tmp_path):
    a = _synthetic(root="/machine/a")
    assert weights.content_digest(a) == weights.content_digest(_synthetic(root="/b"))
    a["source"]["converted"] = "2030-01-01"
    assert weights.content_digest(a) == DIGEST

    def changed(mutate):
        d = _synthetic()
        mutate(d)
        return weights.content_digest(d)

    assert changed(lambda d: d["tensors"]["P3.bias"].add_(1e-7)) != DIGEST
    assert changed(lambda d: d["tensors"].update({"P3.bias": d["tensors"]["P3.bias"].double()})) != DIGEST
    assert changed(lambda d: d["eps"].update({"conv1_bn": 1.001e-3})) != DIGEST


def test_load_checks_the_digest_and_never_deletes(tmp_path):
    path = tmp_path / PT
    torch.save(_synthetic(), path)
    params = weights.load(str(path), "cpu", DIGEST)
    assert params["content_sha256"] == DIGEST
    with pytest.raises(weights.WeightsError, match="other weights"):
        weights.load(str(path), "cpu", "0" * 64)
    with pytest.raises(weights.WeightsError, match="no expected content digest"):
        weights.load(str(path), "cpu", None)
    assert path.is_file()


# ── prepare.ensure ────────────────────────────────────────────────────

def test_a_missing_file_is_made_once_and_then_used(dirs, monkeypatch):
    src, out = dirs
    fake = _FakeConverter()
    monkeypatch.setattr(prepare, "run_converter", fake)
    logs = []
    assert prepare.ensure(out, src, DIGEST, logs.append) == DIGEST
    assert (out / PT).is_file() and fake.calls == 1
    assert any("first use" in m for m in logs)
    source = (out / prepare.SOURCE_FILE).read_text()
    assert source.splitlines()[0] == f"content sha256 {DIGEST}"
    assert str(src) in source and "non-commercial academic" in source
    assert prepare.ensure(out, src, DIGEST, logs.append) is None      # there: no conversion
    assert fake.calls == 1 and _leftovers(out) == []


def test_two_starts_at_once_convert_once(dirs, monkeypatch):
    src, out = dirs
    fake = _FakeConverter(delay=0.5)
    monkeypatch.setattr(prepare, "run_converter", fake)
    errors, logs = [], []

    def start():
        try:
            prepare.ensure(out, src, DIGEST, logs.append)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and fake.calls == 1
    assert any("another run is preparing" in m and prepare.HAND_RUN in m for m in logs)
    assert weights.load(str(out / PT), "cpu", DIGEST)


@pytest.mark.parametrize("fake", [
    _FakeConverter(fail=prepare.PrepareError("the conversion failed")),   # child failed / head check
    _FakeConverter(seed=1),                                                # other weights
])
def test_a_failed_conversion_leaves_no_file_and_no_temp(dirs, monkeypatch, fake):
    src, out = dirs
    monkeypatch.setattr(prepare, "run_converter", fake)
    with pytest.raises(prepare.PrepareError):
        prepare.ensure(out, src, DIGEST, lambda m: None)
    assert not (out / PT).exists() and _leftovers(out) == []


def test_the_temp_dir_of_a_killed_run_is_cleared(dirs, monkeypatch):
    src, out = dirs
    (out / ".convert-999999-abc").mkdir(parents=True)
    (out / ".convert-999999-abc" / PT).write_bytes(b"half")
    (out / ".convert-source-999999.tmp").write_text("half")
    monkeypatch.setattr(prepare, "run_converter", _FakeConverter())
    prepare.ensure(out, src, DIGEST, lambda m: None)
    assert _leftovers(out) == []


def test_force_replaces_only_on_success(dirs, monkeypatch):
    src, out = dirs
    monkeypatch.setattr(prepare, "run_converter", _FakeConverter())
    prepare.ensure(out, src, DIGEST, lambda m: None)
    before = (out / PT).read_bytes()
    for fake in (_FakeConverter(fail=prepare.PrepareError("x")), _FakeConverter(seed=1)):
        monkeypatch.setattr(prepare, "run_converter", fake)
        with pytest.raises(prepare.PrepareError):
            prepare.ensure(out, src, DIGEST, lambda m: None, force=True)
        assert (out / PT).read_bytes() == before and _leftovers(out) == []
    other = weights.content_digest(_synthetic(1))
    monkeypatch.setattr(prepare, "run_converter", _FakeConverter(seed=1))
    assert prepare.ensure(out, src, other, lambda m: None, force=True) == other
    assert weights.load(str(out / PT), "cpu", other)
    assert (out / prepare.SOURCE_FILE).read_text().splitlines()[0] == f"content sha256 {other}"


def test_source_txt_is_regenerated_from_the_weights(dirs, monkeypatch):
    src, out = dirs
    monkeypatch.setattr(prepare, "run_converter", _FakeConverter())
    prepare.ensure(out, src, DIGEST, lambda m: None)
    good = (out / prepare.SOURCE_FILE).read_text()
    (out / prepare.SOURCE_FILE).unlink()
    prepare.ensure(out, src, DIGEST, lambda m: None)
    assert (out / prepare.SOURCE_FILE).read_text() == good
    (out / prepare.SOURCE_FILE).write_text(good.replace(str(src), "/somewhere/else"))  # same digest line
    prepare.ensure(out, src, DIGEST, lambda m: None)
    assert (out / prepare.SOURCE_FILE).read_text() == good

    (out / prepare.SOURCE_FILE).unlink()
    real_replace = os.replace

    def readonly(a, b):
        if str(b).endswith(prepare.SOURCE_FILE):
            raise PermissionError("read-only")
        return real_replace(a, b)

    monkeypatch.setattr(prepare.os, "replace", readonly)
    logs = []
    assert prepare.ensure(out, src, DIGEST, logs.append) is None        # a warning, no error
    assert any("warning" in m for m in logs)


def test_relative_paths_and_a_garbled_description(dirs, monkeypatch):
    src, out = dirs
    seen = {}

    def fake(source_root, made, workdir):
        seen.update(source=source_root, out=made)
        torch.save(_synthetic(), made)

    monkeypatch.setattr(prepare, "run_converter", fake)
    monkeypatch.chdir(src.parent)
    prepare.ensure(out.name, src.name, DIGEST, lambda m: None)
    assert seen["source"].is_absolute() and seen["source"] == src
    assert seen["out"].is_absolute() and (out / PT).is_file()
    good = (out / prepare.SOURCE_FILE).read_bytes()
    (out / prepare.SOURCE_FILE).write_bytes(b"\xff\xfe not text")
    prepare.ensure(out, src, DIGEST, lambda m: None)
    assert (out / prepare.SOURCE_FILE).read_bytes() == good


def test_a_wrong_existing_file_is_refused_but_kept(dirs, monkeypatch):
    src, out = dirs
    out.mkdir()
    torch.save(_synthetic(seed=5), out / PT)
    monkeypatch.setattr(prepare, "run_converter", _FakeConverter())
    prepare.ensure(out, src, DIGEST, lambda m: None)                   # there: not remade
    with pytest.raises(weights.WeightsError):
        weights.load(str(out / PT), "cpu", DIGEST)
    assert (out / PT).is_file()


def test_clear_errors(tmp_path, monkeypatch, dirs):
    src, out = dirs
    with pytest.raises(prepare.PrepareError, match="DEEPCELL_MESMER_MODEL_PATH"):
        prepare.ensure(out, tmp_path / "no_keras", DIGEST, lambda m: None)
    blocker = tmp_path / "a_file"
    blocker.write_text("")
    with pytest.raises(prepare.PrepareError, match="BLOCK01_MESMER_TORCH_PATH"):
        prepare.ensure(blocker / "torch", src, DIGEST, lambda m: None)

    class _Done:
        def __init__(self, code):
            self.returncode = code

    for code, words in ((convert.NO_TENSORFLOW, "fusion_mesmer"), (convert.HEAD_CHECK, "does not reproduce"),
                        (1, "exit code 1")):
        monkeypatch.setattr(prepare.subprocess, "run", lambda *a, code=code, **k: _Done(code))
        with pytest.raises(prepare.PrepareError, match=words):
            prepare.ensure(out, src, DIGEST, lambda m: None)
        assert not (out / PT).exists() and _leftovers(out) == []


def test_the_child_runs_on_the_cpu_and_finds_its_package(dirs, monkeypatch):
    src, out = dirs
    seen = {}

    def fake_run(cmd, cwd, env):
        seen.update(cmd=cmd, cwd=cwd, env=env)
        torch.save(_synthetic(), cmd[cmd.index("--out") + 1])
        return type("Done", (), {"returncode": 0})()

    real_run = subprocess.run
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(prepare.subprocess, "run", fake_run)
    prepare.ensure(out, src, DIGEST, lambda m: None)
    assert seen["cmd"][:3] == [sys.executable, "-m", prepare.CHILD_MODULE]
    assert seen["env"]["CUDA_VISIBLE_DEVICES"] == ""
    # the child really imports from that root, whatever alias this process used
    probe = real_run([sys.executable, "-m", prepare.CHILD_MODULE, "--help"], cwd=str(src),
                           env=seen["env"], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    assert seen["cwd"].startswith(str(out / prepare.TEMP_PREFIX))


# ── the manifest tool and the hand-run converter ──────────────────────

@pytest.fixture
def manifest_tool(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    import model_manifest
    scratch = tmp_path / "models.json"
    shutil.copy(model_manifest.MANIFEST, scratch)
    monkeypatch.setattr(model_manifest, "MANIFEST", scratch)
    return model_manifest


def test_the_path_rule_prefers_the_override(monkeypatch, tmp_path):
    entry = {"root": "/nowhere", "env_override": "BLOCK01_MESMER_TORCH_PATH"}
    monkeypatch.delenv("BLOCK01_MESMER_TORCH_PATH", raising=False)
    assert engines.model_root(entry) == "/nowhere"
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(tmp_path))
    assert engines.model_root(entry) == str(tmp_path)


def test_no_test_writes_into_the_real_weights_directory():
    root = engines.manifest_entry(KEY)["root"]
    assert os.environ.get("BLOCK01_MESMER_TORCH_PATH")
    assert engines.model_root(engines.manifest_entry(KEY)) != root


def test_the_repository_manifest_names_the_content():
    entry = engines.manifest_entry(KEY)
    assert entry["generated"] is True and len(entry["content_sha256"]) == 64
    assert entry["files"] == [{"path": PT}]


def test_verify_compares_content_and_write_keeps_the_digest(manifest_tool, monkeypatch, tmp_path, capsys):
    out = tmp_path / "torch"
    out.mkdir()
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(out))
    monkeypatch.setattr(manifest_tool, "MODELS", {KEY: manifest_tool.MODELS[KEY]})
    data = json.loads(manifest_tool.MANIFEST.read_text())
    data["models"] = {KEY: dict(data["models"][KEY], content_sha256=DIGEST)}
    manifest_tool.MANIFEST.write_text(json.dumps(data))
    torch.save(_synthetic(root="/one/machine"), out / PT)
    assert manifest_tool.verify() == 0
    torch.save(_synthetic(root="/another/machine"), out / PT)          # other bytes, same content
    assert manifest_tool.verify() == 0
    torch.save(_synthetic(seed=2), out / PT)
    assert manifest_tool.verify() == 1
    assert manifest_tool.write() == 0
    assert json.loads(manifest_tool.MANIFEST.read_text())["models"][KEY]["content_sha256"] == DIGEST
    data["models"][KEY].pop("content_sha256")
    manifest_tool.MANIFEST.write_text(json.dumps(data))
    assert manifest_tool.write() == 1
    assert "content_sha256" in capsys.readouterr().out


def test_the_converter_never_touches_the_manifest_unless_asked(manifest_tool, monkeypatch, tmp_path):
    out = tmp_path / "torch"
    keras = tmp_path / "keras"
    keras.mkdir()
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(out))
    monkeypatch.setenv("DEEPCELL_MESMER_MODEL_PATH", str(keras))
    import convert_mesmer_to_torch as conv
    data = json.loads(manifest_tool.MANIFEST.read_text())
    data["models"][KEY]["content_sha256"] = DIGEST
    manifest_tool.MANIFEST.write_text(json.dumps(data, indent=2) + "\n")
    before = manifest_tool.MANIFEST.read_bytes()
    # the script imports `seg_runner...` (its own module objects): fake there
    monkeypatch.setattr(conv.prepare, "run_converter", _FakeConverter())
    assert conv.main([]) == 0 and manifest_tool.MANIFEST.read_bytes() == before
    assert conv.main([]) == 0                                           # there: checked, kept
    monkeypatch.setattr(conv.prepare, "run_converter", _FakeConverter(seed=3))
    assert conv.main(["--force"]) == 1                                  # other content: refused
    assert conv.weights.load(str(out / PT), "cpu", DIGEST)              # the old file stays
    assert conv.main(["--force", "--update-manifest"]) == 0             # a new baseline
    new = weights.content_digest(_synthetic(3))
    assert json.loads(manifest_tool.MANIFEST.read_text())["models"][KEY]["content_sha256"] == new


def test_a_missing_conversion_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps({"models": {KEY: {
        "root": str(tmp_path / "none"), "env_override": "BLOCK01_MESMER_TORCH_PATH",
        "generated": True, "files": [{"path": PT}]}}}))
    monkeypatch.delenv("BLOCK01_MESMER_TORCH_PATH", raising=False)
    with pytest.raises(engines.ModelMissingError):
        engines.model_path(KEY, manifest=manifest)


# ── for real: the Keras model converted at an engine's first start ────

def test_a_real_first_start_converts_and_the_second_loads(tmp_path, monkeypatch):
    """No fakes: TensorFlow, the installed Keras model and the repository's
    content digest. The release check must see this run, not skip."""
    pytest.importorskip("tensorflow")
    try:
        engines.mesmer_model_path()
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))
    import numpy as np
    from block01.seg_runner import synthetic
    from block01.seg_runner.client import EngineProcess
    out = tmp_path / "torch"
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(out))
    log = tmp_path / "engine.log"
    first = EngineProcess("mesmer", log_path=str(log))
    hello = first.start()
    first.close()
    text = log.read_text(errors="replace")
    assert "first use" in text and "PyTorch weights ready" in text
    want = engines.manifest_entry(KEY)["content_sha256"]
    assert hello["provenance"]["model_artifact"]["content_sha256"] == want
    assert (out / prepare.SOURCE_FILE).read_text().splitlines()[0] == f"content sha256 {want}"
    assert _leftovers(out) == []

    log2 = tmp_path / "engine2.log"
    second = EngineProcess("mesmer", log_path=str(log2))
    second.start()
    np.save(tmp_path / "in.npy", synthetic.mesmer_pair())
    task = {"task_id": "t0", "input": str(tmp_path / "in.npy"), "out_dir": str(tmp_path / "res"),
            "params": {"method": "mesmer_nuclei", "image_mpp": 0.5}}
    states = second.run([task])
    second.close()
    assert states == {"t0": "ok"}
    assert "first use" not in log2.read_text(errors="replace")
