"""Block A3: the producers' provenance entries and the artifact graph v0 over
a synthetic project made by the REAL producer functions.

A3-2: `ensure_project_manifest` (schema, P2 description, transforms,
`raw_slide`), Step0's `write_handoff` (`corrected_channel`), Step4's schema
check in `resolve_quant_job`. A3-3 adds fusion, Step2 and Step4 and the
chain question of gate 7.
"""

import json
import os

import numpy as np
import pytest

pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from block01.core import artifact_graph as ag  # noqa: E402
from block01.core import project_identity as pid  # noqa: E402
from block01.core import provenance as prov  # noqa: E402
from block01.core import quant_sources as qs  # noqa: E402
from block01.utils import roi_project as rp  # noqa: E402
from test_quant_sources import NAMES, ROI_BBOX, SLIDE_SHAPE, write_slide  # noqa: E402

ROI = {"name": "Full WSI", "display_name": "Full WSI", "bbox_fullres": list(ROI_BBOX),
       "polygon_fullres": None, "type": "roi", "analysis_region_type": "roi"}


def _entries(project, kind=None):
    return [e for e in prov.load_entries(project) if kind is None or e["kind"] == kind]


def _slide(root, seed=0):
    rng = np.random.default_rng(seed)
    data = rng.integers(0, 256, size=(len(NAMES),) + SLIDE_SHAPE, dtype=np.uint8)
    path = os.path.join(str(root), f"slide{seed}.ome.tif")
    write_slide(path, data, tiled=True)
    return path


def _workspace(root, slide, roi=ROI):
    """A project + workspace the way Step0 Save makes one."""
    project = os.path.join(str(root), "proj")
    ctx = rp.create_roi_context(project, roi, slide, display_name=roi["name"])
    return project, ctx


def _corrected_product(step0_dir, roi=ROI, channels=("CD3",), token="tok-1", bbox=None):
    zpath = os.path.join(step0_dir, "corrected_channels.zarr")
    root = zarr.open_group(zpath, mode="a")
    root.attrs["mode"] = "roi_only"
    root.attrs["correction_config"] = {"channel_decisions": {ch: "tophat" for ch in channels}}
    g = root.require_group(qs.region_folder(roi["name"]))
    b = bbox or roi["bbox_fullres"]
    g.attrs.update({"roi_name": roi["name"], "bbox_fullres": list(b),
                    "shape": [b[1] - b[0], b[3] - b[2]]})
    rng = np.random.default_rng(3)
    for ch in channels:
        a = g.create_dataset(ch, data=rng.random((b[1] - b[0], b[3] - b[2]), dtype=np.float32) * 50,
                             chunks=(64, 64), overwrite=True)
        a.attrs.update({"correction_method": "tophat", "correction_param_value": 10,
                        "correction_param_name": "tophat_radius",
                        "channel_index": NAMES.index(ch), "source_identity": f"{token}-{ch}",
                        "roi_name": roi["name"], "written_at": "2026-10-01T12:00:00"})
    return zpath


def _handoff(project, ctx, slide, roi=ROI, decisions=None):
    from block01.core.step0_handoff import write_handoff
    step0 = ctx["step_dirs"]["step0"]
    decisions = {"CD3": "tophat"} if decisions is None else decisions
    config = {"channel_decisions": dict({n: "original" for n in NAMES}, **decisions),
              "method_params": {"tophat_radius": 15, "cucim_sigma": 50},
              "channel_params": {"CD3": {"tophat_radius": 10}}}
    spec = {"raw_path": slide, "step0_dir": step0, "config": config,
            "rois": [dict(roi, roi_id=ctx["roi_id"], roi_dir=ctx["roi_dir"])], "patches": [],
            "corrected_path": os.path.join(step0, "corrected_channels.zarr"),
            "manifest_path": os.path.join(step0, "step0_roi_result.json"),
            "analysis_region_type": roi["type"], "roi_id": ctx["roi_id"],
            "roi_dir": ctx["roi_dir"], "project_dir": project,
            "remap_path": os.path.join(step0, "step0_channel_remap.json"),
            "nucleus_channel": "DAPI"}
    return write_handoff(spec)


# ── A3-2: the project manifest ──────────────────────────────────────────

def test_a_new_project_gets_the_schema_the_description_and_transforms(tmp_path):
    slide = _slide(tmp_path)
    project, _ctx = _workspace(tmp_path, slide)
    manifest = json.load(open(os.path.join(project, "project_manifest.json")))
    assert manifest["project_schema_version"] == 1 and manifest["version"] == 1
    sid, desc = pid.describe_slide(slide)
    assert manifest["sources"] == {sid: desc}
    assert desc["kind"] == "ome_tiff" and desc["coarsest_level_shape_yx"] == list(SLIDE_SHAPE)
    transforms = json.load(open(os.path.join(project, "transforms.json")))
    assert list(transforms["slides"]) == [sid]
    raw = _entries(project, "raw_slide")
    assert [e["artifact_id"] for e in raw] == [sid] and raw[0]["parameters"] == desc


def test_a_legacy_project_is_adopted_and_keeps_its_keys(tmp_path):
    slide = _slide(tmp_path)
    project = os.path.join(str(tmp_path), "proj")
    os.makedirs(project)
    json.dump({"version": 1, "created_at": "2026-09-25T17:49:38", "custom": "kept"},
              open(os.path.join(project, "project_manifest.json"), "w"))
    rp.ensure_project_manifest(project, slide)
    m = json.load(open(os.path.join(project, "project_manifest.json")))
    assert m["project_schema_version"] == 1 and m["custom"] == "kept"
    assert m["created_at"] == "2026-09-25T17:49:38"


def test_an_unknown_schema_is_not_overwritten_and_gets_nothing_new(tmp_path, capsys):
    slide = _slide(tmp_path)
    project = os.path.join(str(tmp_path), "proj")
    os.makedirs(project)
    json.dump({"version": 1, "project_schema_version": 2},
              open(os.path.join(project, "project_manifest.json"), "w"))
    rp.ensure_project_manifest(project, slide)              # never raises in the Save path
    m = json.load(open(os.path.join(project, "project_manifest.json")))
    assert m["project_schema_version"] == 2 and "sources" not in m
    assert not os.path.exists(os.path.join(project, "transforms.json"))
    assert not os.path.exists(os.path.join(project, "provenance"))
    assert "project_schema_version 2" in capsys.readouterr().out


def test_an_unreadable_slide_does_not_break_the_save(tmp_path):
    project = os.path.join(str(tmp_path), "proj")
    bad = os.path.join(str(tmp_path), "not_a_slide.ome.tif")
    open(bad, "wb").write(b"not a tiff")
    rp.ensure_project_manifest(project, bad)
    m = json.load(open(os.path.join(project, "project_manifest.json")))
    assert m["project_schema_version"] == 1 and "sources" not in m


def test_the_manifest_is_written_atomically(tmp_path, monkeypatch):
    slide = _slide(tmp_path)
    project, _ = _workspace(tmp_path, slide)
    path = os.path.join(project, "project_manifest.json")
    before = open(path, "rb").read()

    def boom(src, dst):
        raise OSError("crash")
    monkeypatch.setattr(prov.os, "replace", boom)
    with pytest.raises(OSError):
        rp.ensure_project_manifest(project, slide)
    assert open(path, "rb").read() == before
    assert not [f for f in os.listdir(project) if ".tmp." in f]


def test_two_slides_in_one_project_are_two_sources(tmp_path):
    a, b = _slide(tmp_path, 0), _slide(tmp_path, 1)
    project, _ = _workspace(tmp_path, a)
    rp.ensure_project_manifest(project, b)
    m = json.load(open(os.path.join(project, "project_manifest.json")))
    assert len(m["sources"]) == 2
    assert len(json.load(open(os.path.join(project, "transforms.json")))["slides"]) == 2


# ── A3-2: Step0's corrected channels ────────────────────────────────────

def test_the_handoff_registers_each_corrected_channel(tmp_path):
    slide = _slide(tmp_path)
    project, ctx = _workspace(tmp_path, slide)
    zpath = _corrected_product(ctx["step_dirs"]["step0"], channels=("CD3", "CD8"))
    _handoff(project, ctx, slide, decisions={"CD3": "tophat", "CD8": "tophat"})
    sid = pid.describe_slide(slide)[0]
    rid = pid.region_id_of_roi(sid, ROI)
    corr = _entries(project, "corrected_channel")
    assert sorted(e["location"]["member"] for e in corr) == ["Full_WSI/CD3", "Full_WSI/CD8"]
    for e in corr:
        assert e["depends_on"] == [sid] and e["operates_on"] == [rid] and e["flags"] == []
        assert e["location"]["path"] == f"rois/{ctx['roi_id']}/step0/corrected_channels.zarr"
        assert e["parameters"]["valid_bounds"] == list(ROI_BBOX)
        assert e["token"] == "tok-1-" + e["location"]["member"].split("/")[1]
        assert e["workspace_id"] == ctx["roi_id"]
    # a second Save over unchanged arrays registers nothing new
    _handoff(project, ctx, slide, decisions={"CD3": "tophat", "CD8": "tophat"})
    assert len(_entries(project, "corrected_channel")) == 2
    # a recomputed channel (new token) is a new artifact
    _corrected_product(ctx["step_dirs"]["step0"], channels=("CD3",), token="tok-2")
    _handoff(project, ctx, slide, decisions={"CD3": "tophat", "CD8": "tophat"})
    assert len(_entries(project, "corrected_channel")) == 3
    assert zpath


def test_a_group_whose_geometry_differs_from_its_roi_is_flagged_not_guessed(tmp_path):
    slide = _slide(tmp_path)
    project, ctx = _workspace(tmp_path, slide)
    _corrected_product(ctx["step_dirs"]["step0"], bbox=[0, 100, 0, 100])
    from block01.core.step0_handoff import ensure_empty_corrected_zarr  # noqa: F401
    _handoff(project, ctx, slide)
    # the handoff rewrites the group's bbox from its ROI: geometry agrees again
    e = _entries(project, "corrected_channel")[0]
    assert e["operates_on"] and e["flags"] == []
    # a group left with another bbox (written by something else) is flagged
    g = zarr.open_group(os.path.join(ctx["step_dirs"]["step0"], "corrected_channels.zarr"))
    g["Full_WSI"].attrs["bbox_fullres"] = [0, 100, 0, 100]
    g["Full_WSI"]["CD3"].attrs["source_identity"] = "tok-other"
    prov.register_corrected_channels(project, ctx["roi_dir"],
                                     os.path.join(ctx["step_dirs"]["step0"],
                                                  "corrected_channels.zarr"), slide)
    flagged = [x for x in _entries(project, "corrected_channel") if x["token"] == "tok-other"]
    assert flagged[0]["operates_on"] == [] and flagged[0]["flags"] == [prov.FLAG_REGION_MISMATCH]


def test_a_legacy_project_handoff_still_succeeds_and_registers_nothing(tmp_path):
    slide = _slide(tmp_path)
    project, ctx = _workspace(tmp_path, slide)
    m = json.load(open(os.path.join(project, "project_manifest.json")))
    m.pop("project_schema_version")
    json.dump(m, open(os.path.join(project, "project_manifest.json"), "w"))
    _corrected_product(ctx["step_dirs"]["step0"])
    out = _handoff(project, ctx, slide)
    assert os.path.exists(out["manifest_path"])
    assert _entries(project, "corrected_channel") == []


def test_registration_does_not_change_the_handoff(tmp_path, monkeypatch):
    """The products are the same with and without registration."""
    def files(ctx):
        d = ctx["step_dirs"]["step0"]
        out = {}
        for base, _dirs, fs in os.walk(d):
            for f in fs:
                if f == "step0_roi_result.json":
                    continue                    # absolute paths differ between projects
                data = open(os.path.join(base, f), "rb").read()
                for real, alias in ((ctx["roi_dir"], b"<WS>"), (ctx["project_dir"], b"<P>"),
                                    (ctx["roi_id"], b"<ID>")):
                    data = data.replace(real.encode(), alias)
                out[os.path.relpath(os.path.join(base, f), d)] = data
        return out
    slide = _slide(tmp_path)
    p1, c1 = _workspace(tmp_path / "a", slide)
    _corrected_product(c1["step_dirs"]["step0"])
    _handoff(p1, c1, slide)
    monkeypatch.setattr(prov, "register", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    p2, c2 = _workspace(tmp_path / "b", slide)
    _corrected_product(c2["step_dirs"]["step0"])
    _handoff(p2, c2, slide)
    assert files(c1) == files(c2)


# ── A3-2: Step4 refuses an unknown project schema ───────────────────────

def test_step4_refuses_an_unknown_project_schema(tmp_path):
    from test_quant_sources import build_project
    p = build_project(tmp_path)
    project = os.path.dirname(os.path.dirname(p["ws"]))
    qs.resolve_quant_job(p["run_dir"])                     # legacy: as today
    json.dump({"project_schema_version": 1}, open(os.path.join(project, "project_manifest.json"),
                                                  "w"))
    qs.resolve_quant_job(p["run_dir"])
    json.dump({"project_schema_version": 3}, open(os.path.join(project, "project_manifest.json"),
                                                  "w"))
    with pytest.raises(qs.QuantSourceError, match="project_schema_version 3"):
        qs.resolve_quant_job(p["run_dir"])


# ── A3-3: fusion, Step2, Step4 and the chain question (gate 7) ──────────

REMAP = {"DAPI": {"min": 0.0, "max": 255.0, "gamma": 1.0},
         "CD3": {"min": 0.0, "max": 50.0, "gamma": 1.0},
         "CD8": {"min": 0.0, "max": 255.0, "gamma": 1.0}}


def _nuclei_slide(root):
    """A slide whose DAPI has round nuclei, so a real engine finds cells."""
    rng = np.random.default_rng(5)
    h, w = SLIDE_SHAPE
    yy, xx = np.mgrid[0:h, 0:w]
    dapi = np.zeros((h, w), np.float32)
    for _ in range(40):
        cy, cx = rng.uniform(15, h - 15), rng.uniform(25, w - 15)
        dapi = np.maximum(dapi, np.clip(1.0 - np.hypot(yy - cy, xx - cx) / 6.0, 0, 1))
    data = rng.integers(0, 40, size=(len(NAMES), h, w), dtype=np.uint8)
    data[0] = np.clip(dapi * 230 + rng.normal(0, 4, (h, w)), 0, 255).astype(np.uint8)
    path = os.path.join(str(root), "nuclei.ome.tif")
    write_slide(path, data, tiled=True)
    return path


def _fuse(ctx, slide, *, sources=True, remap=REMAP):
    from PyQt5 import QtCore
    QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.step0.overview_panel import FullFusionWorker
    step0 = ctx["step_dirs"]["step0"]
    loader = OMETIFFLoader(slide)
    zpath = os.path.join(step0, "corrected_channels.zarr")
    loader.set_corrected_zarr_store(zpath, {"CD3": "tophat"})
    cfg = {"ome_tiff": slide, "output_dir": ctx["step_dirs"]["step1"],
           "nucleus": {"channel": "DAPI", "weight": 1.0},
           "groups": {"markers": {"group_weight": 1.0, "channels": {"CD3": 1.0, "CD8": 1.0}}},
           "channel_remap_params": remap, "artifact_kind": "test", "config_hash": "cfg1"}
    kw = ({"corrected_zarr_path": zpath, "corrected_decisions": {"CD3": "tophat"},
           "use_pixel_sources": True} if sources else {})
    w = FullFusionWorker(loader=loader, fusion_cfg=cfg, n_rows=2, n_cols=2,
                         rois=[dict(ROI)], **kw)
    errs, done = [], []
    w.error.connect(errs.append)
    w.finished.connect(done.append)
    w.run()
    assert errs == [] and done
    return done[0]


def _step0(tmp_path, slide):
    project, ctx = _workspace(tmp_path, slide)
    _corrected_product(ctx["step_dirs"]["step0"])
    _handoff(project, ctx, slide)
    return project, ctx


def test_fusion_registers_its_inputs(tmp_path):
    slide = _slide(tmp_path)
    project, ctx = _step0(tmp_path, slide)
    fused = _fuse(ctx, slide)
    sid = pid.describe_slide(slide)[0]
    corr = _entries(project, "corrected_channel")[0]["artifact_id"]
    [e] = _entries(project, "fused")
    assert e["location"]["path"] == f"rois/{ctx['roi_id']}/step1/fused_Full WSI.zarr"
    assert sorted(e["depends_on"]) == sorted([sid, corr])
    assert e["operates_on"] == [pid.region_id_of_roi(sid, ROI)] and e["unresolved_inputs"] == []
    assert e["token"] == prov.fused_token(dict(zarr.open(fused, mode="r").attrs))


def test_a_corrected_channel_without_a_window_is_not_a_dependency(tmp_path):
    slide = _slide(tmp_path)
    project, ctx = _step0(tmp_path, slide)
    _fuse(ctx, slide, remap={k: v for k, v in REMAP.items() if k != "CD3"})
    [e] = _entries(project, "fused")
    assert [d for d in e["depends_on"] if d.startswith("art_")] == []
    assert e["parameters"]["corrected_read_without_window"] == ["CD3"]


def test_the_loader_path_records_that_it_cannot_say(tmp_path):
    slide = _slide(tmp_path)
    project, ctx = _step0(tmp_path, slide)
    _fuse(ctx, slide, sources=False)
    [e] = _entries(project, "fused")
    assert [u["role"] for u in e["unresolved_inputs"]] == ["pixels_via_loader"]


def test_fusion_output_is_unchanged_by_registration(tmp_path, monkeypatch):
    slide = _slide(tmp_path)
    _p1, c1 = _step0(tmp_path / "a", slide)
    a = zarr.open(_fuse(c1, slide), mode="r")[...]
    monkeypatch.setattr(prov, "register", lambda *x, **k: (_ for _ in ()).throw(OSError("x")))
    _p2, c2 = _step0(tmp_path / "b", slide)
    b = zarr.open(_fuse(c2, slide), mode="r")[...]
    assert a.any() and np.array_equal(a, b)


def _segment(ctx, fused_path):
    """A real Step2 run (StarDist) on the workspace's fused product."""
    pytest.importorskip("stardist")
    import test_step2_runner_path as t2
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    from PyQt5 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = t2._contract_config(__import__("pathlib").Path(ctx["roi_dir"]) / "future",
                              "stardist_nuclei_expansion")
    w = SegmentMergeWorker(fused_path, seg_config=cfg, n_rows=2, n_cols=2, overlap_px=t2.HALO,
                           output_dir=ctx["step_dirs"]["step2"],
                           rois=[dict(ROI, roi_id=ctx["roi_id"])])
    got = t2._collect(w)
    w.run()
    assert got["error"] == [] and got["finished"] and got["finished"][0] > 0
    return w.output_dir


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """Step0 -> fusion -> Step2 -> Step4, every step its real producer."""
    from block01.workers.feature_extract_worker import run_extraction
    root = tmp_path_factory.mktemp("chain")
    slide = _nuclei_slide(root)
    project, ctx = _step0(root, slide)
    fused = _fuse(ctx, slide)
    run_dir = _segment(ctx, fused)
    out = os.path.join(ctx["roi_dir"], "step4", "quantification_runs",
                       os.path.basename(run_dir), "Full_WSI")
    res = run_extraction(run_dir, out, roi_name="Full WSI", write_csv=True)
    return {"project": project, "ctx": ctx, "slide": slide, "fused": fused, "run_dir": run_dir,
            "h5ad": res["h5ad"]}


def test_step2_registers_the_run_with_its_fused_input_and_scope(chain):
    project = chain["project"]
    [seg] = _entries(project, "segmentation_run")
    [fused] = _entries(project, "fused")
    sid = pid.describe_slide(chain["slide"])[0]
    assert seg["depends_on"] == [fused["artifact_id"]]
    assert seg["operates_on"] == [pid.region_id_of_roi(sid, ROI)]
    assert seg["token"] == os.path.basename(chain["run_dir"])
    assert seg["parameters"]["engine"] == "stardist" and seg["software"]["engine_provenance"]
    assert not any(d.startswith("reg_") for d in seg["depends_on"])


def test_the_graph_answers_which_inputs_a_step4_result_used(chain):
    """Gate 7: Step4 -> segmentation run -> fused -> corrected, through
    depends_on only."""
    project = chain["project"]
    g = ag.ArtifactGraph(project)
    out = g.step4_lineage(chain["h5ad"])
    [seg] = _entries(project, "segmentation_run")
    [fused] = _entries(project, "fused")
    [corr] = _entries(project, "corrected_channel")
    sid = pid.describe_slide(chain["slide"])[0]
    assert out["segmentation_run"] == [seg["artifact_id"]]
    assert out["fused"] == [fused["artifact_id"]]
    assert out["corrected_channels_fused"] == [corr["artifact_id"]]
    assert out["corrected_channels_step4"] == [corr["artifact_id"]]
    assert out["raw_slide"] == [sid] and out["unresolved_inputs"] == {}
    assert out["operates_on"] == [pid.region_id_of_roi(sid, ROI)]
    assert set(g.dependents(corr["artifact_id"], recursive=True)) >= {
        fused["artifact_id"], seg["artifact_id"], out["step4_h5ad"]}
    assert g.issues() == {"dangling": [], "unresolved_inputs": [], "flagged": [],
                          "overwritten": []}


def test_step4_outputs_are_unchanged_by_registration(chain, tmp_path, monkeypatch):
    """Registration failing changes nothing in the h5ad / CSV."""
    import h5py
    import pandas as pd
    from block01.workers.feature_extract_worker import run_extraction
    base = os.path.join(chain["ctx"]["roi_dir"], "step4", "invariance")
    calls = []
    real = prov.register
    monkeypatch.setattr(prov, "register", lambda *x, **k: calls.append(x[1]) or real(*x, **k))
    a = run_extraction(chain["run_dir"], os.path.join(base, "a"), roi_name="Full WSI",
                       write_csv=True)
    assert "step4_h5ad" in calls                      # inside the workspace: it registers
    monkeypatch.setattr(prov, "register", lambda *x, **k: (_ for _ in ()).throw(OSError("x")))
    b = run_extraction(chain["run_dir"], os.path.join(base, "b"), roi_name="Full WSI",
                       write_csv=True)
    with h5py.File(a["h5ad"], "r") as fa, h5py.File(b["h5ad"], "r") as fb:
        names = []
        fa.visititems(lambda n, o: names.append(n) if isinstance(o, h5py.Dataset) else None)
        assert names and all(np.array_equal(fa[n][()], fb[n][()]) for n in names
                             if n != "uns/provenance_json")
    assert pd.read_csv(a["csv"]).equals(pd.read_csv(b["csv"]))
    assert sorted(os.listdir(os.path.join(base, "a"))) == sorted(os.listdir(os.path.join(base, "b")))


def test_a_step4_result_on_an_unregistered_run_says_so(chain, tmp_path):
    """A run made before A3 is an unresolved input, never guessed."""
    import shutil
    from block01.workers.feature_extract_worker import run_extraction
    project = chain["project"]
    legacy = chain["run_dir"] + "_legacy_copy"
    shutil.copytree(chain["run_dir"], legacy)
    meta_path = os.path.join(legacy, "segmentation_meta.json")
    meta = json.load(open(meta_path))
    meta["run_id"] = os.path.basename(legacy)
    json.dump(meta, open(meta_path, "w"))
    out = os.path.join(os.path.dirname(legacy), "q_legacy")
    try:
        res = run_extraction(legacy, out, roi_name="Full WSI")
        lin = ag.ArtifactGraph(project).step4_lineage(res["h5ad"])
        assert lin["segmentation_run"] == []
        [(_aid, unresolved)] = lin["unresolved_inputs"].items()
        assert unresolved == [{"role": "segmentation_run", "path": os.path.realpath(legacy)}]
    finally:
        shutil.rmtree(legacy, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_an_overwritten_fused_target_is_reported(chain):
    """Re-fusing writes the shared target again: the graph says the run's
    input has since been overwritten."""
    project, ctx = chain["project"], chain["ctx"]
    _fuse(ctx, chain["slide"])
    over = ag.ArtifactGraph(project).issues()["overwritten"]
    [seg] = _entries(project, "segmentation_run")
    assert len(over) == 1 and over[0]["depended_on_by"] == [seg["artifact_id"]]


def test_the_single_region_step2_path_registers_too(chain):
    """`segment_merge_worker`'s second call site (no ROI list)."""
    pytest.importorskip("stardist")
    import test_step2_runner_path as t2
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    ctx, project = chain["ctx"], chain["project"]
    before = {e["artifact_id"] for e in _entries(project, "segmentation_run")}
    cfg = t2._contract_config(__import__("pathlib").Path(ctx["roi_dir"]) / "future",
                              "stardist_nuclei_expansion")
    w = SegmentMergeWorker(chain["fused"], seg_config=cfg, n_rows=2, n_cols=2,
                           overlap_px=t2.HALO, output_dir=ctx["step_dirs"]["step2"], rois=None)
    got = t2._collect(w)
    w.run()
    assert got["error"] == [] and got["finished"]
    new = [e for e in _entries(project, "segmentation_run") if e["artifact_id"] not in before]
    assert len(new) == 1 and new[0]["token"] == os.path.basename(w.output_dir)
    assert not any(d.startswith("reg_") for d in new[0]["depends_on"])
