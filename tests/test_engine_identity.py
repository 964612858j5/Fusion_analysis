"""Block A0.5: the compatibility identity is small; the rest is provenance.

`engine_identity` (compared Step1 -> Step2) is the engine kind, an explicit
behaviour version and the model id -- nothing about the environment, so an
unrelated package, a log line or a protocol change cannot read as "another
engine". `engine_provenance` records what the process ran with and is never
compared. No real engine here: the identity must be answerable without one.
"""

import pathlib
import shutil

import pytest

from block01.seg_runner import engines, runner


class _FakeEngine:
    device = "cpu"

    def __init__(self, libs=None, path="/models/x"):
        self._libs = libs or {"stardist": "0.9.2", "numpy": "1.26.4"}
        self._path = path

    def lib_versions(self):
        return dict(self._libs)

    def model_resolved_path(self):
        return self._path


@pytest.mark.parametrize("engine", sorted(engines.BEHAVIOR_VERSION))
def test_the_identity_is_the_engine_its_behaviour_version_and_its_model(engine):
    ident = runner.engine_identity(engine, _FakeEngine())
    assert ident == engines.behavior_identity(engine)
    assert set(ident) == {"version", "engine", "behavior_version", "model_id"}
    assert ident["version"] == engines.IDENTITY_VERSION == 2
    assert ident["model_id"] == engines.MODEL_ID[engine]
    for gone in ("lock_hash", "lib_versions", "runner_version", "model_checksum"):
        assert gone not in ident


def test_the_models_are_the_manifests():
    import json
    models = json.loads(engines.MODELS_MANIFEST.read_text())["models"]
    for engine, model_id in engines.MODEL_ID.items():
        assert models[model_id]["engine"] == engine


def test_library_versions_do_not_change_the_identity():
    a = runner.engine_identity("stardist", _FakeEngine({"stardist": "0.9.2"}))
    b = runner.engine_identity("stardist", _FakeEngine({"stardist": "0.9.3", "numpy": "2.0"}))
    assert a == b


def test_the_lock_files_and_the_runner_code_are_provenance_only(tmp_path, monkeypatch):
    """Another environment lock (a package added for Step4) and another
    seg_runner source change the provenance and leave the identity alone."""
    repo = pathlib.Path(runner.__file__).resolve().parent.parent
    fake = tmp_path / "repo"
    (fake / "envs" / "fusion_mesmer").mkdir(parents=True)
    for name in ("conda-linux-64.lock", "requirements-pip.txt", "models.json"):
        shutil.copy(repo / "envs" / "fusion_mesmer" / name, fake / "envs" / "fusion_mesmer" / name)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(runner, "_REPO", fake)
    monkeypatch.setattr(runner, "_PKG", pkg)
    engine = _FakeEngine()
    ident0 = runner.engine_identity("stardist", engine)
    prov0 = runner.engine_provenance("stardist", engine)
    with open(fake / "envs" / "fusion_mesmer" / "requirements-pip.txt", "a") as f:
        f.write("pyarrow==17.0.0\n")
    (pkg / "a.py").write_text("x = 1  # a log line\n")
    ident1 = runner.engine_identity("stardist", engine)
    prov1 = runner.engine_provenance("stardist", engine)
    assert ident1 == ident0
    assert prov1["env_lock_hash"] != prov0["env_lock_hash"]
    assert prov1["runner_version"] != prov0["runner_version"]


def test_the_provenance_records_what_the_process_ran_with():
    prov = runner.engine_provenance("stardist", _FakeEngine(path="/m/2D_versatile_fluo"))
    assert set(prov) == {"git_commit", "runner_version", "lib_versions", "env_lock_hash",
                         "device", "model_manifest_entry_hash", "model_resolved_path"}
    assert prov["lib_versions"] == {"stardist": "0.9.2", "numpy": "1.26.4"}
    assert prov["device"] == "cpu" and prov["model_resolved_path"] == "/m/2D_versatile_fluo"
    assert len(prov["model_manifest_entry_hash"]) == 16


def test_a_model_path_that_cannot_be_read_is_null_not_an_error():
    class _Broken(_FakeEngine):
        def model_resolved_path(self):
            raise AttributeError("no path")
    assert runner.engine_provenance("stardist", _Broken())["model_resolved_path"] is None


# ── the comparison ────────────────────────────────────────────────────

def _v2(**kw):
    return dict(engines.behavior_identity("stardist"), **kw)


def test_the_same_engine_has_no_difference():
    got = engines.compare_identity(_v2(), _v2())
    assert got == {"engine_differs": False, "legacy_identity": False, "differences": []}


@pytest.mark.parametrize("field,value", [("behavior_version", 0), ("model_id", "stardist_other")])
def test_a_behaviour_or_model_change_is_a_difference(field, value):
    got = engines.compare_identity(_v2(**{field: value}), _v2())
    assert got["differences"] == [[field, value, _v2()[field]]]
    assert not got["engine_differs"] and not got["legacy_identity"]


def test_another_engine_kind_is_flagged():
    got = engines.compare_identity(dict(engines.behavior_identity("cellpose")), _v2())
    assert got["engine_differs"] is True


def test_a_legacy_identity_is_compared_on_the_engine_kind_only():
    legacy = {"engine": "stardist", "lock_hash": "ddf963353c497824",
              "lib_versions": {"stardist": "0.9.2", "tensorflow": "2.8.4"},
              "model_checksum": "e6b06cf4f8fb9dd5", "runner_version": "f52e7d9a5a70cf8b"}
    got = engines.compare_identity(legacy, _v2())
    assert got == {"engine_differs": False, "legacy_identity": True, "differences": []}
    assert engines.compare_identity(dict(legacy, engine="cellpose"), _v2())["engine_differs"]
