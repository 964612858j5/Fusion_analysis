"""Block A3: identity and coordinates (`core/project_identity.py`).

Gates A3-G1 (project schema), A3-G2 (slide_id), A3-G3 (region_id), A3-G4
(P2 description) and A3-G5 (coordinates) of the A3+A4 application v2 §7.2.
All data is synthetic, in a temporary directory, except the read-only
checks on the test1 copy, which are skipped when it is absent.
"""

import json
import os
import shutil

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")

from block01.core import project_identity as pid  # noqa: E402

COPY = os.path.expanduser("~/fusionflux/bench_a0/test1_copy")
NAMES = ["DAPI", "CD3", "CD8"]


def _write_slide(path, shape=(1001, 999), levels=3, physical=True, seed=0):
    rng = np.random.default_rng(seed)
    data = rng.integers(0, 255, size=(len(NAMES),) + shape, endpoint=True).astype(np.uint8)
    meta = {"axes": "CYX", "Channel": {"Name": NAMES}}
    if physical:
        meta.update({"PhysicalSizeX": 0.5, "PhysicalSizeY": 0.25,
                     "PhysicalSizeXUnit": "µm", "PhysicalSizeYUnit": "µm"})
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=levels - 1, metadata=meta, tile=(64, 64))
        cur = data
        for _ in range(levels - 1):
            cur = cur[:, ::4, ::4]                     # non-integer level ratios
            tw.write(np.ascontiguousarray(cur), subfiletype=1, tile=(64, 64))
    return str(path)


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    return _write_slide(tmp_path_factory.mktemp("a3") / "slide.ome.tif")


def _manifest(tmp_path, payload):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    (d / "project_manifest.json").write_text(json.dumps(payload) if not isinstance(payload, str)
                                             else payload)
    return str(d)


# ── A3-G1: project schema ───────────────────────────────────────────────

def test_a_project_without_the_field_is_legacy(tmp_path):
    assert pid.read_project_schema(_manifest(tmp_path, {"version": 1})) == pid.LEGACY
    assert pid.read_project_schema(str(tmp_path / "nothing")) == pid.LEGACY


def test_the_current_version_is_read(tmp_path):
    assert pid.read_project_schema(_manifest(tmp_path, {"project_schema_version": 1})) == 1


@pytest.mark.parametrize("value", [2, "1", True, -1, 1.0, 0, [1]])
def test_an_unknown_version_is_an_explicit_error(tmp_path, value):
    proj = _manifest(tmp_path, {"project_schema_version": value})
    with pytest.raises(pid.ProjectSchemaError) as exc:
        pid.read_project_schema(proj)
    assert "project_manifest.json" in str(exc.value) and repr(value) in str(exc.value)


def test_a_manifest_that_is_not_json_is_an_explicit_error(tmp_path):
    with pytest.raises(pid.ProjectSchemaError):
        pid.read_project_schema(_manifest(tmp_path, "{not json"))


# ── A3-G2: slide_id ─────────────────────────────────────────────────────

def test_slide_id_is_stable_across_copy_rename_and_touch(slide, tmp_path):
    sid, desc = pid.describe_slide(slide)
    assert sid.startswith("slide_") and len(sid) == 22
    assert desc["slide_id_method"] == "slide_sig_v1"
    copy = tmp_path / "elsewhere" / "renamed.ome.tif"
    copy.parent.mkdir()
    shutil.copy(slide, copy)
    os.utime(copy, None)
    assert pid.describe_slide(str(copy))[0] == sid


def test_another_slide_has_another_id(slide, tmp_path):
    other = _write_slide(tmp_path / "other.ome.tif", seed=1)
    assert pid.describe_slide(other)[0] != pid.describe_slide(slide)[0]


def test_an_unchanged_fingerprint_is_not_hashed_again(slide):
    sid, desc = pid.describe_slide(slide)
    before = pid.signature_computations
    again, _ = pid.describe_slide(slide, cached={sid: desc})
    assert again == sid and pid.signature_computations == before
    stale = dict(desc, fingerprint="0:0")
    pid.describe_slide(slide, cached={sid: stale})
    assert pid.signature_computations == before + 1


# ── A3-G4: the raw-source description (P2) ──────────────────────────────

def test_the_description_has_kind_levels_and_the_coarsest_shape(slide):
    _, desc = pid.describe_slide(slide)
    assert desc["kind"] == "ome_tiff"
    assert desc["level_count"] == 3
    assert desc["level_shapes_yx"] == [[1001, 999], [251, 250], [63, 63]]
    assert desc["coarsest_level_shape_yx"] == [63, 63]
    assert desc["dtype"] == "uint8" and desc["channel_count"] == 3
    assert desc["physical_size_yx_um"] == [0.25, 0.5]


def test_no_physical_size_is_null_never_a_default(tmp_path):
    path = _write_slide(tmp_path / "nophys.ome.tif", physical=False)
    _, desc = pid.describe_slide(path)
    assert desc["physical_size_yx_um"] is None
    entry = pid.transforms_entry(desc)
    assert entry["physical_um"]["available"] is False
    frames = pid.SlideFrames.from_description(desc)
    with pytest.raises(ValueError):
        frames.global_to_physical(1.0, 1.0)


def test_an_unknown_source_has_no_kind():
    with pytest.raises(ValueError):
        pid.source_kind(object())


@pytest.mark.skipif(not os.path.exists(os.path.join(COPY, "project_manifest.json")),
                    reason="test1 copy absent")
def test_the_test1_slide_description():
    slide = json.load(open(os.path.join(COPY, "project_manifest.json")))["source_ome"]
    _, desc = pid.describe_slide(slide)
    assert desc["level_count"] == 3
    assert desc["coarsest_level_shape_yx"] == [964, 1013]
    assert desc["level_shapes_yx"] == [[15437, 16215], [3859, 4053], [964, 1013]]
    assert desc["physical_size_yx_um"][0] == pytest.approx(0.50686, abs=1e-5)
    frames = pid.SlideFrames.from_description(desc)
    sy, sx = frames.scale_yx(1)
    assert (round(sy, 6), round(sx, 6)) == (4.000259, 4.000740)


# ── A3-G3: region_id ────────────────────────────────────────────────────

SID = "slide_0123456789abcdef"
TRI = [(10.0, 10.0), (50.0, 12.0), (30.0, 40.0)]


def test_the_same_geometry_is_one_region_whatever_the_name_or_workspace():
    a = {"roi_id": "full_wsi_1", "display_name": "Full WSI", "type": "full_wsi",
         "bbox_fullres": [0, 100, 0, 200], "polygon_fullres": None}
    b = dict(a, roi_id="full_wsi_2", display_name="renamed", polygon_fullres=[])
    assert pid.region_id_of_roi(SID, a) == pid.region_id_of_roi(SID, b)


def test_a_one_pixel_bbox_change_is_another_region():
    assert pid.region_id(SID, "roi", [0, 100, 0, 200]) != pid.region_id(SID, "roi", [0, 100, 0, 201])


def test_another_slide_or_type_is_another_region():
    base = pid.region_id(SID, "roi", [0, 10, 0, 10])
    assert base != pid.region_id("slide_fedcba9876543210", "roi", [0, 10, 0, 10])
    assert base != pid.region_id(SID, "full_wsi", [0, 10, 0, 10])


def test_a_second_roi_without_roi_id_still_has_a_region():
    rois = [{"name": "ROI_1", "roi_id": "roi_x", "bbox_fullres": [0, 50, 0, 50]},
            {"name": "ROI_2", "bbox_fullres": [60, 90, 60, 90]}]
    ids = [pid.region_id_of_roi(SID, r) for r in rois]
    assert len(set(ids)) == 2 and all(i.startswith("reg_") for i in ids)


@pytest.mark.parametrize("variant", [
    TRI, TRI[1:] + TRI[:1], TRI[2:] + TRI[:2],          # cyclic shifts
    TRI[::-1],                                          # the other direction
    TRI + [TRI[0]],                                     # a closing vertex
    [TRI[0], TRI[0], TRI[1], TRI[2], TRI[2]],           # repeated vertices
    [(x + 0.0001, y) for x, y in TRI],                  # below the 1e-3 grid
    [[int(x), int(y)] for x, y in TRI],                 # ints vs floats
])
def test_one_polygon_drawn_in_any_order_is_one_region(variant):
    assert pid.region_id(SID, "roi", [0, 50, 0, 60], variant) == \
        pid.region_id(SID, "roi", [0, 50, 0, 60], TRI)


def test_moving_a_vertex_is_another_region():
    moved = [(10.001, 10.0)] + TRI[1:]
    assert pid.region_id(SID, "roi", [0, 50, 0, 60], moved) != \
        pid.region_id(SID, "roi", [0, 50, 0, 60], TRI)


def test_degenerate_polygons_are_no_polygon():
    for poly in (None, [], [(1, 1), (2, 2)], [(1, 1), (1, 1), (1, 1)]):
        assert pid.normalize_polygon(poly) is None


def test_an_unknown_region_type_is_an_error():
    with pytest.raises(ValueError):
        pid.region_id(SID, "correction_group", [0, 10, 0, 10])
    with pytest.raises(ValueError):
        pid.region_type({"type": "blob"})
    assert pid.region_type({}) == "roi"


@pytest.mark.skipif(not os.path.isdir(os.path.join(COPY, "rois")), reason="test1 copy absent")
def test_the_test1_workspaces_are_one_region_and_nothing_is_written():
    def tree():
        out = []
        for base, _dirs, files in os.walk(COPY):
            for f in files:
                st = os.stat(os.path.join(base, f))
                out.append((os.path.relpath(os.path.join(base, f), COPY), st.st_size,
                            st.st_mtime_ns))
        return sorted(out)
    before = tree()
    slide = json.load(open(os.path.join(COPY, "project_manifest.json")))["source_ome"]
    sid, _ = pid.describe_slide(slide)
    ids = set()
    for ws in sorted(os.listdir(os.path.join(COPY, "rois"))):
        manifest = json.load(open(os.path.join(COPY, "rois", ws, "roi_manifest.json")))
        ids.add(pid.region_id_of_roi(sid, manifest))
    assert len(ids) == 1
    assert tree() == before


# ── A3-G5: coordinates ──────────────────────────────────────────────────

def test_level_ratios_are_exact_per_axis(slide):
    _, desc = pid.describe_slide(slide)
    frames = pid.SlideFrames.from_description(desc)
    assert frames.scale_yx(1) == (1001 / 251, 999 / 250)
    assert frames.scale_yx(1) != (4.0, 4.0)


def test_level_and_global_round_trip(slide):
    _, desc = pid.describe_slide(slide)
    f = pid.SlideFrames.from_description(desc)
    xs, ys = np.array([0.0, 17.0, 249.0]), np.array([0.0, 5.0, 250.0])
    gx, gy = f.level_to_global(xs, ys, 1)
    bx, by = f.global_to_level(gx, gy, 1)
    np.testing.assert_allclose(bx, xs, atol=1e-9)
    np.testing.assert_allclose(by, ys, atol=1e-9)
    # pixel 0 of level 1 has its centre at (s - 1) / 2 in global
    sy, sx = f.scale_yx(1)
    assert gx[0] == pytest.approx((sx - 1) / 2) and gy[0] == pytest.approx((sy - 1) / 2)


def test_physical_region_local_and_viewer_world(slide):
    _, desc = pid.describe_slide(slide)
    f = pid.SlideFrames.from_description(desc)
    assert f.global_to_physical(10, 4) == (5.0, 1.0)
    assert f.physical_to_global(5.0, 1.0) == (10.0, 4.0)
    bbox = [100, 300, 40, 90]                           # [y0, y1, x0, x1]
    assert f.global_to_region_local(45, 120, bbox) == (5, 20)
    assert f.region_local_to_global(5, 20, bbox) == (45, 120)
    wx, wy = f.global_to_viewer_world(7, 9)
    assert (wx, wy) == (7.5, 9.5)
    px, py = f.viewer_world_to_global(np.array([7.0, 7.99]), np.array([9.5, 9.0]))
    assert px.tolist() == [7, 7] and py.tolist() == [9, 9]


def test_a_tma_core_is_an_ordinary_region(slide):
    """Gate 4: a core needs no second coordinate model."""
    _, desc = pid.describe_slide(slide)
    sid = pid.describe_slide(slide)[0]
    core = {"type": "TMA_core", "bbox_fullres": [200, 260, 300, 370],
            "polygon_fullres": [(300, 200), (370, 200), (370, 260), (300, 260)]}
    rid = pid.region_id_of_roi(sid, core)
    assert rid.startswith("reg_")
    f = pid.SlideFrames.from_description(desc)
    lx, ly = f.global_to_region_local(310, 205, core["bbox_fullres"])
    assert (lx, ly) == (10, 5)
    assert pid.bbox_columns(core["bbox_fullres"]) == {"bbox_x0": 300, "bbox_y0": 200,
                                                      "bbox_x1": 370, "bbox_y1": 260}


def test_transforms_are_grouped_by_slide():
    desc_a = {"level_shapes_yx": [[100, 100], [25, 25]], "physical_size_yx_um": [0.5, 0.5]}
    desc_b = {"level_shapes_yx": [[80, 60]], "physical_size_yx_um": None}
    data = pid.merged_transforms(None, "slide_a", desc_a)
    data = pid.merged_transforms(data, "slide_b", desc_b)
    assert sorted(data["slides"]) == ["slide_a", "slide_b"]
    assert data["slides"]["slide_a"]["pyramid_levels"][1]["scale_yx"] == [4.0, 4.0]
    assert data["slides"]["slide_b"]["physical_um"]["available"] is False


def test_new_identity_code_uses_no_nominal_factor():
    """The rule binds new code: no ``2 ** level`` and no rounded legacy
    factor (checked on the code, not the docstrings)."""
    import ast
    tree = ast.parse(open(pid.__file__, encoding="utf-8").read())
    pows = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow)]
    attrs = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert pows == [] and "legacy_level_downsample_rounded" not in attrs
