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
