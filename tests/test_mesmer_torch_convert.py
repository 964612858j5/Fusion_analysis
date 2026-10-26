"""Mesmer in PyTorch, step 2: the converter, the manifest entry and the one
path rule (docs/mesmer_torch_plan.md §5). Everything is written into
tmp_path: an override directory for the weights and a scratch manifest --
never the repository's models.json or the shared model directory."""
import json
import os
import sys

import pytest

from block01.seg_runner import engines

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(engines.__file__))), "scripts")


@pytest.fixture
def manifest_tool(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    import model_manifest
    monkeypatch.setattr(model_manifest, "MANIFEST", tmp_path / "models.json")
    return model_manifest


def test_the_path_rule_prefers_the_override(monkeypatch, tmp_path):
    entry = {"root": "/nowhere", "env_override": "BLOCK01_MESMER_TORCH_PATH"}
    monkeypatch.delenv("BLOCK01_MESMER_TORCH_PATH", raising=False)
    assert engines.model_root(entry) == "/nowhere"
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(tmp_path))
    assert engines.model_root(entry) == str(tmp_path)


def test_write_refuses_an_empty_or_incomplete_directory(manifest_tool, monkeypatch, tmp_path, capsys):
    key = "mesmer_multiplex_segmentation_torch"
    monkeypatch.setattr(manifest_tool, "MODELS", {key: manifest_tool.MODELS[key]})   # only this entry
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(tmp_path / "empty"))
    assert manifest_tool.write() == 1
    assert key in capsys.readouterr().out
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "SOURCE.txt").write_text("x")                # the .pt is missing
    assert manifest_tool.write() == 1
    assert "mesmer_multiplex_segmentation.pt" in capsys.readouterr().out
    assert not (tmp_path / "models.json").exists()                    # nothing half-written


def test_convert_write_verify_and_load_through_an_override(manifest_tool, monkeypatch, tmp_path):
    pytest.importorskip("tensorflow")
    try:
        engines.mesmer_model_path()
    except engines.ModelMissingError as exc:
        pytest.skip(str(exc))
    out = tmp_path / "torch_weights"
    monkeypatch.setenv("BLOCK01_MESMER_TORCH_PATH", str(out))
    import convert_mesmer_to_torch as conv
    assert conv.main([]) == 0
    assert (out / "mesmer_multiplex_segmentation.pt").is_file()
    source = (out / "SOURCE.txt").read_text()
    assert "non-commercial academic" in source and "OIHW" in source
    data = json.loads((tmp_path / "models.json").read_text())
    entry = data["models"]["mesmer_multiplex_segmentation_torch"]
    assert entry["derived_from"] == "mesmer_multiplex_segmentation"
    assert {f["path"] for f in entry["files"]} == {"mesmer_multiplex_segmentation.pt", "SOURCE.txt"}
    assert manifest_tool.verify() == 0
    assert engines.model_path("mesmer_multiplex_segmentation_torch",
                              manifest=tmp_path / "models.json") == str(out)
    from block01.seg_runner.mesmer_torch import weights
    params = weights.load(str(out / "mesmer_multiplex_segmentation.pt"))
    assert params["source"]["model_key"] == "mesmer_multiplex_segmentation"
    assert conv.main([]) == 1                                           # exists: --force needed
    (out / "SOURCE.txt").write_text("changed")
    assert manifest_tool.verify() == 1                                  # a changed file is caught


def test_a_missing_conversion_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps({"models": {"mesmer_multiplex_segmentation_torch": {
        "root": str(tmp_path / "none"), "env_override": "BLOCK01_MESMER_TORCH_PATH",
        "files": [{"path": "mesmer_multiplex_segmentation.pt", "size": 1}]}}}))
    monkeypatch.delenv("BLOCK01_MESMER_TORCH_PATH", raising=False)
    with pytest.raises(engines.ModelMissingError):
        engines.model_path("mesmer_multiplex_segmentation_torch", manifest=manifest)
