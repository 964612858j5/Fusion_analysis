"""Block A3: provenance records (`core/provenance.py`) and the artifact graph
v0 (`core/artifact_graph.py`) on hand-made entries.

Gate A3-G6 (the rules every entry keeps) and the parts of A3-G7 that do not
need a producer; the producer chain is in `test_v16_artifact_graph.py`.
"""

import json
import os
import subprocess
import sys

import pytest

from block01.core import artifact_graph as ag  # noqa: E402
from block01.core import provenance as prov  # noqa: E402
from block01.core.project_identity import ProjectSchemaError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = "reg_0123456789abcdef"
SLIDE = "slide_00112233445566ff"


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "project_manifest.json").write_text(json.dumps({"version": 1,
                                                         "project_schema_version": 1}))
    return str(d)


def _slide(project):
    return prov.register(project, "raw_slide", prov.location(project, "/data/s.ome.tif"),
                         SLIDE, artifact_id=SLIDE, slide_id=SLIDE)


def _tree(path):
    out = []
    for base, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(base, f)
            out.append((os.path.relpath(p, path), open(p, "rb").read()))
    return sorted(out)


# ── locations ───────────────────────────────────────────────────────────

def test_project_paths_are_relative_with_forward_slashes(project):
    loc = prov.location(project, os.path.join(project, "rois", "ws", "step1", "f.zarr"),
                        member="Full_WSI\\CD3D")
    assert loc == {"path": "rois/ws/step1/f.zarr", "relative_to": "project",
                   "member": "Full_WSI/CD3D"}
    assert prov.resolve_location(project, loc) == os.path.join(project, "rois", "ws", "step1",
                                                               "f.zarr")


def test_windows_separators_become_forward_slashes():
    assert prov.to_posix("rois\\ws\\step1\\f.zarr", sep="\\") == "rois/ws/step1/f.zarr"
    assert prov.to_posix("rois/ws", sep="/") == "rois/ws"


def test_paths_outside_the_project_stay_absolute(project, tmp_path):
    loc = prov.location(project, str(tmp_path / "slide.tif"))
    assert loc["relative_to"] == "absolute" and os.path.isabs(loc["path"])


# ── the rules ───────────────────────────────────────────────────────────

def test_an_entry_has_every_field(project):
    _slide(project)
    aid = prov.register(project, "fused", prov.location(project, os.path.join(project, "f.zarr")),
                        "t1", depends_on=[SLIDE], operates_on=[REG], workspace_id="ws",
                        slide_id=SLIDE, parameters={"p": 1}, software={"git_commit": "x"})
    entry = json.load(open(os.path.join(project, "provenance", f"{aid}.json")))
    assert set(entry) == {"schema_version", "artifact_id", "kind", "created_at", "workspace_id",
                          "slide_id", "location", "token", "operates_on", "depends_on",
                          "unresolved_inputs", "parameters", "software", "flags"}
    assert entry["depends_on"] == [SLIDE] and entry["operates_on"] == [REG]
    assert aid.startswith("art_fused_")


@pytest.mark.parametrize("bad", [REG, "rois/ws/step1/f.zarr", "", "art_fused_000000000000"])
def test_depends_on_holds_registered_artifact_ids_only(project, bad):
    _slide(project)
    with pytest.raises(prov.ProvenanceError):
        prov.register(project, "fused", prov.location(project, os.path.join(project, "f.zarr")),
                      "t", depends_on=[SLIDE, bad])
    assert not [f for f in os.listdir(os.path.join(project, "provenance"))
                if f.startswith("art_fused")]


def test_a_region_id_is_refused_even_if_a_file_of_that_name_exists(project):
    """The type check stands on its own, not only on "is it registered"."""
    os.makedirs(os.path.join(project, "provenance"), exist_ok=True)
    open(os.path.join(project, "provenance", f"{REG}.json"), "w").write("{}")
    with pytest.raises(prov.ProvenanceError, match="not an artifact id"):
        prov.register(project, "fused", prov.location(project, os.path.join(project, "f.zarr")),
                      "t", depends_on=[REG])


@pytest.mark.parametrize("bad", [SLIDE, "Full WSI", "full_wsi_20260927_121444_6bad"])
def test_operates_on_holds_region_ids_only(project, bad):
    with pytest.raises(prov.ProvenanceError):
        prov.register(project, "fused", prov.location(project, os.path.join(project, "f.zarr")),
                      "t", operates_on=[bad])


def test_an_unknown_kind_is_refused(project):
    with pytest.raises(prov.ProvenanceError):
        prov.register(project, "workspace", prov.location(project, project), "t")


def test_the_same_artifact_registered_twice_is_one_entry(project):
    loc = prov.location(project, os.path.join(project, "f.zarr"))
    a = prov.register(project, "fused", loc, "t1")
    assert prov.register(project, "fused", loc, "t1") == a
    b = prov.register(project, "fused", loc, "t2")
    assert b != a
    assert len(os.listdir(os.path.join(project, "provenance"))) == 2


def test_a_legacy_or_unknown_project_registers_nothing(tmp_path):
    for i, manifest in enumerate(({"version": 1}, {"project_schema_version": 2})):
        d = tmp_path / f"p{i}"
        d.mkdir()
        (d / "project_manifest.json").write_text(json.dumps(manifest))
        with pytest.raises((prov.ProvenanceError, ProjectSchemaError)):
            prov.register(str(d), "fused", prov.location(str(d), str(d / "f.zarr")), "t")
        assert prov.safe_register(str(d), "fused", prov.location(str(d), str(d / "f.zarr")),
                                  "t") is None
        assert not os.path.exists(d / "provenance")


def test_a_failed_write_leaves_no_partial_file(project, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(prov.os, "replace", boom)
    assert prov.safe_register(project, "fused",
                              prov.location(project, os.path.join(project, "f.zarr")), "t") is None
    pdir = os.path.join(project, "provenance")
    assert not os.path.exists(pdir) or os.listdir(pdir) == []


def test_write_json_atomic_keeps_the_old_file_on_a_crash(tmp_path, monkeypatch):
    path = tmp_path / "project_manifest.json"
    prov.write_json_atomic(str(path), {"a": 1})
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("crash")
    monkeypatch.setattr(prov.os, "replace", boom)
    with pytest.raises(OSError):
        prov.write_json_atomic(str(path), {"a": 2})
    assert path.read_bytes() == before
    assert os.listdir(tmp_path) == ["project_manifest.json"]


# ── the graph on hand-made entries ──────────────────────────────────────

def _chain(project, run_collision=False):
    _slide(project)
    p = lambda *a: os.path.join(project, "rois", "ws", *a)  # noqa: E731
    corr = prov.register(project, "corrected_channel", prov.location(project, p("c.zarr"), "G/CD3"),
                         "u1", depends_on=[SLIDE], operates_on=[REG])
    fused = prov.register(project, "fused", prov.location(project, p("f.zarr")), "f1",
                          depends_on=[SLIDE, corr], operates_on=[REG])
    seg = prov.register(project, "segmentation_run", prov.location(project, p("seg")), "seg_1",
                        depends_on=[fused], operates_on=[REG],
                        flags=[prov.FLAG_RUN_ID_COLLISION] if run_collision else [])
    h5 = prov.register(project, "step4_h5ad", prov.location(project, p("cf.h5ad")), "c1",
                       depends_on=[seg, SLIDE, corr], operates_on=[REG])
    return corr, fused, seg, h5, p


def test_the_lineage_of_a_step4_result_comes_from_depends_on_only(project):
    corr, fused, seg, h5, p = _chain(project)
    g = ag.ArtifactGraph(project)
    out = g.step4_lineage(p("cf.h5ad"))
    assert out["segmentation_run"] == [seg] and out["fused"] == [fused]
    assert out["corrected_channels_step4"] == [corr]
    assert out["corrected_channels_fused"] == [corr]
    assert out["raw_slide"] == [SLIDE]
    assert set(g.dependents(corr, recursive=True)) == {fused, seg, h5}


def test_operates_on_is_not_an_edge(project):
    _chain(project)
    g = ag.ArtifactGraph(project)
    assert REG not in g._dependents and all(REG not in g.depends_on(a) for a in g.entries)


def test_an_unregistered_input_is_reported_not_guessed(project):
    fused_path = os.path.join(project, "rois", "ws", "f.zarr")
    seg = prov.register(project, "segmentation_run",
                        prov.location(project, os.path.join(project, "seg")), "seg_old",
                        unresolved_inputs=[{"role": "fused", "path": fused_path}])
    # a fused artifact registered LATER at the same path does not become its input
    prov.register(project, "fused", prov.location(project, fused_path), "f9")
    g = ag.ArtifactGraph(project)
    assert g.depends_on(seg) == []
    assert g.issues()["unresolved_inputs"] == [(seg, [{"role": "fused", "path": fused_path}])]


def test_an_overwritten_location_is_reported(project):
    _corr, fused, seg, _h5, p = _chain(project)
    newer = prov.register(project, "fused", prov.location(project, p("f.zarr")), "f2")
    over = ag.ArtifactGraph(project).issues()["overwritten"]
    assert over == [{"artifact_id": fused, "location": "rois/ws/f.zarr", "now_holds": newer,
                     "depended_on_by": [seg]}]


def test_flags_are_reported(project):
    _corr, _fused, seg, _h5, _p = _chain(project, run_collision=True)
    assert ag.ArtifactGraph(project).issues()["flagged"] == [(seg, [prov.FLAG_RUN_ID_COLLISION])]


def test_the_graph_writes_nothing(project):
    _chain(project)
    before = _tree(project)
    g = ag.ArtifactGraph(project)
    g.issues()
    g.dependents(SLIDE, recursive=True)
    assert _tree(project) == before


def test_the_graph_of_a_legacy_project_is_empty_and_of_an_unknown_one_an_error(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "project_manifest.json").write_text(json.dumps({"version": 1}))
    assert ag.ArtifactGraph(str(d)).legacy and ag.ArtifactGraph(str(d)).entries == {}
    (d / "project_manifest.json").write_text(json.dumps({"project_schema_version": 7}))
    with pytest.raises(ProjectSchemaError):
        ag.ArtifactGraph(str(d))


def test_the_cli_exits_2_on_an_unknown_schema(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "project_manifest.json").write_text(json.dumps({"project_schema_version": 2}))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "artifact_graph.py"),
                        str(d), "list"], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=os.path.dirname(ROOT)))
    assert r.returncode == 2 and "project_schema_version 2" in r.stderr
