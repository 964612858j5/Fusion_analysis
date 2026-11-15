"""`scripts/model_manifest.py --verify --installed` (backlog 23, 2026-11-15):
Mesmer may be left out of an install (DeepCell's archive is gone and no
mirror or copy was given). A model none of whose files is present is "not
installed", not a problem; a partly present or different one still fails;
plain `--verify` is unchanged. Synthetic: every model directory is pointed
at an empty temporary one through its manifest variable."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "model_manifest.py"
MANIFEST = REPO / "envs" / "fusion_mesmer" / "models.json"


def _entries():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["models"]


def _run(tmp_path, *args):
    env = dict(os.environ)
    for key, entry in _entries().items():
        var = entry.get("env_override")
        if not var:
            pytest.skip(f"{key} has no env_override to point at a temporary folder")
        d = tmp_path / key
        d.mkdir(exist_ok=True)
        env[var] = str(d)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, str(SCRIPT), "--verify", *args], capture_output=True,
                          text=True, env=env, cwd=str(REPO), timeout=120)


def test_nothing_installed_is_not_a_problem_with_installed(tmp_path):
    r = _run(tmp_path, "--installed")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("NOT INSTALLED") == len(_entries())
    assert "models: ok" in r.stdout


def test_plain_verify_still_fails_on_a_missing_model(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 1 and "MISSING" in r.stdout


def test_a_partly_present_model_still_fails(tmp_path):
    key, entry = next((k, e) for k, e in _entries().items()
                      if not e.get("generated") and len(e["files"]) > 1)
    first = tmp_path / key / entry["files"][0]["path"]
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"not the model")
    r = _run(tmp_path, "--installed")
    assert r.returncode == 1
    assert f"MISMATCH {key}" in r.stdout and f"MISSING {key}" in r.stdout


def test_a_mesmer_method_without_the_model_says_how_to_add_it(tmp_path, monkeypatch):
    """What Step2 shows when a Mesmer method is chosen on an install without
    Mesmer: the installer's import command, not a bare missing path."""
    pytest.importorskip("torch")
    from block01.seg_runner import engines
    for key in (engines.MESMER_TORCH_ENTRY, engines.manifest_entry(engines.MESMER_TORCH_ENTRY)["derived_from"]):
        var = engines.manifest_entry(key)["env_override"]
        monkeypatch.setenv(var, str(tmp_path / "absent" / key))
    with pytest.raises(engines.ModelMissingError) as got:
        engines.MesmerEngine()
    assert "not installed" in str(got.value)
    assert "-MesmerOnly" in str(got.value) and "--mesmer-only" in str(got.value)
