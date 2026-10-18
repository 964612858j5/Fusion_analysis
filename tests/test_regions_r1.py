"""Regions R1 (docs/region_qc_module_plan.md §3, §5 R1): the annotation
document -- polygon pieces, numbering (ported from F3), merge, marks,
exclusions -- its two-part lock, versions, provenance and the migration of
F3 data; and the detection's natural outlines (format 2).

A small synthetic map (4 rows x 3 columns, two empty positions) laid on a
synthetic slide in several placements; pieces are 64-gons (what F2 format 1
and drawn circles become) or outlines from the detection.
"""

import json
import math
import os

import numpy as np
import pytest

from block01.core import provenance as prov
from block01.core.regions import document as D
from block01.core.regions import geometry as G
from block01.core.regions import lock as K
from block01.core.regions import migrate as M
from block01.core.regions import numbering as N
from block01.core.tma import cores as tc

ROWS, COLS = 4, 3
EMPTY = {(3, 3), (3, 4)}
SHAPE = (1400, 1100)
DIA = 200.0
SID = "slide_00112233445566aa"
SHA = "ab" * 32


def _study():
    pos = []
    for x in range(1, COLS + 1):
        for y in range(1, ROWS + 1):
            occ = (x, y) not in EMPTY
            pos.append({"position_id": (x - 1) * ROWS + y, "x": x, "y": y,
                        "donor_block_id": f"D{x}{y}" if occ else "", "sample_id": f"S{x}{y}" if occ else "",
                        "core_height": "", "annotation": "", "spot_slide": ""})
    return {"slide_id": SID, "map_sheet": {"sha256": SHA, "rows": ROWS, "cols": COLS, "positions": pos}}


def _place(x, y, placement="identity"):
    cx, cy = 250 + (x - 1) * 300, 250 + (y - 1) * 300
    if placement == "rot180":
        cx, cy = 250 + (COLS - x) * 300, 250 + (ROWS - y) * 300
    elif placement == "mirror":
        cx = 250 + (COLS - x) * 300
    return float(cy), float(cx)


def _candidates(placement="identity", missing=(), extra=(), outlines=False):
    cands, k = [], 0
    for x in range(1, COLS + 1):
        for y in range(1, ROWS + 1):
            if (x, y) in EMPTY or (x, y) in missing:
                continue
            k += 1
            cy, cx = _place(x, y, placement)
            c = {"candidate_id": k, "cy": cy, "cx": cx, "radius": DIA / 2, "area": math.pi * (DIA / 2) ** 2}
            if outlines:
                c["outline"] = G.circle_polygon(cy, cx, DIA / 2 * 1.03, SHAPE)
            cands.append(c)
    for cy, cx, r in extra:
        k += 1
        cands.append({"candidate_id": k, "cy": cy, "cx": cx, "radius": r, "area": math.pi * r * r})
    return {"version": 1, "core_diameter_px": DIA, "candidates": cands}


def _doc(placement="identity", **kw):
    d = D.new_document(SID, SHAPE)
    D.attach_tma(d, _study(), _candidates(placement, **kw))
    return d


def _objects(d):
    out = [(uid, *D.core_centre(d, uid)) for uid in d["cores"]]
    out += [(pid, *G.centroid(p["polygon"])) for pid, p in d["pieces"].items()
            if p["owner"] is None and not p["dropped"]]
    return out


def _number_column(d, s, x, placement="identity"):
    recs = N.target_records(s, (x - 1) * ROWS + 1, (x - 1) * ROWS + ROWS, "column")
    ys = [p["y"] for p in s["map_sheet"]["positions"] if p["x"] == x and p["donor_block_id"]]
    sel = N.band_select(_objects(d), _place(x, min(ys), placement), _place(x, max(ys), placement), DIA / 2)
    plan = N.plan_binding(d, sel, recs)
    assert plan["ok"], plan["problems"]
    return N.apply_binding(d, plan, "line", rebind=bool(plan["conflicts"]))


def _key(x, y):
    return tc.position_key(SID, SHA, (x - 1) * ROWS + y)


def _label(d, uid):
    key = d["cores"][uid]["position_key"]
    for p in _study()["map_sheet"]["positions"]:
        if tc.position_key(SID, SHA, p["position_id"]) == key:
            return f"X{p['x']}-Y{p['y']:02d}"
    return None


def _finished(placement="identity"):
    s = _study()
    d = _doc(placement)
    for x in range(1, COLS + 1):
        _number_column(d, s, x, placement)
    return s, d


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "project_manifest.json").write_text(json.dumps({"version": 1, "project_schema_version": 1}))
    return str(p)


# ── document and numbering (ported F3 behaviour) ───────────────────────

def test_pieces_are_polygons_from_outlines_or_circles():
    d = D.new_document(SID, SHAPE)
    D.attach_tma(d, _study(), _candidates(outlines=True))
    assert all(len(p["polygon"]) == 64 for p in d["pieces"].values())
    d2 = _doc()                                                        # format 1: circles -> 64-gons
    assert all(len(p["polygon"]) == 64 for p in d2["pieces"].values())
    assert len(d2["cores"]) == ROWS * COLS - len(EMPTY)


@pytest.mark.parametrize("placement", ["identity", "rot180", "mirror"])
def test_every_placement_is_numbered_by_the_users_lines(placement):
    s, d = _finished(placement)
    for uid, c in d["cores"].items():
        lab = _label(d, uid)
        cy, cx = D.core_centre(d, uid)
        ey, ex = _place(int(lab[1]), int(lab[4:]), placement)
        assert abs(cy - ey) < 2 and abs(cx - ex) < 2 and c["mapping"] == "matched"


def test_ranges_cross_columns_and_skip_empties():
    s = _study()
    assert [r["label"] for r in N.target_records(s, 9, 12, "column")] == ["X3-Y01", "X3-Y02"]
    assert [r["label"] for r in N.target_records(s, 3, 6, "column")] == ["X1-Y03", "X1-Y04", "X2-Y01", "X2-Y02"]
    with pytest.raises(D.RegionsError):
        N.records_by_ids(s, [11])


def test_conflicts_rebind_swap_custom_and_review():
    s, d = _finished()
    a = next(u for u in d["cores"] if _label(d, u) == "X1-Y01")
    b = next(u for u in d["cores"] if _label(d, u) == "X1-Y02")
    plan = N.plan_binding(d, [a], N.records_by_ids(s, [2]))
    assert sorted(c["kind"] for c in plan["conflicts"]) == ["object_numbered", "record_held"]
    with pytest.raises(D.RegionsError):
        N.apply_binding(d, plan, "single")
    N.apply_binding(d, plan, "single", rebind=True)
    assert _label(d, a) == "X1-Y02" and d["cores"][b]["position_key"] is None
    with pytest.raises(D.RegionsError):
        N.swap(d, a, b)                                                # b has no number
    N.set_custom(d, b, "EXTRA")
    assert any("custom core EXTRA" in p for p in K.lock_problems(d, s))
    cy, cx = _place(2, 2)
    frag = D.add_circle_piece(d, cy, cx + 150, 20)
    c22 = next(u for u in d["cores"] if _label(d, u) == "X2-Y02")
    D.merge(d, c22, frag)
    assert d["cores"][c22]["mapping"] == "review"
    N.confirm_number(d, c22)
    assert d["cores"][c22]["mapping"] == "matched"


# ── shape edits ─────────────────────────────────────────────────────────

def test_translate_scale_and_vertex_edit_stay_whole_pixels_on_the_slide():
    d = _doc()
    pid = next(iter(d["pieces"]))
    a0 = G.area(d["pieces"][pid]["polygon"])
    D.translate_shape(d, pid, 10.4, -5.6)
    D.scale_shape(d, pid, 1.5)
    assert abs(G.area(d["pieces"][pid]["polygon"]) / a0 - 2.25) < 0.05
    D.move_vertex(d, pid, 0, -50, 5000)                                # clipped into the slide
    v = d["pieces"][pid]["polygon"][0]
    assert v == [SHAPE[1] - 1, 0] and all(isinstance(c, int) for q in d["pieces"][pid]["polygon"] for c in q)
    with pytest.raises(D.RegionsError):
        D.add_piece(d, [[0, 0], [5, 5], [10, 10]])                     # no area


# ── exclusions ──────────────────────────────────────────────────────────

def test_exclusions_keep_their_uid_through_edits_and_type_follows_reasons():
    d = D.new_document(SID, SHAPE)
    D.set_session(d, "ws_a", "/p/rois/ws_a/step0/correction_config.json", "11" * 32)
    uid = D.add_exclusion(d, G.circle_polygon(500, 500, 80, SHAPE), ["blur"])
    assert d["exclusions"][uid]["created_in_workspace"] == "ws_a"
    D.translate_shape(d, uid, 5, 5)
    D.update_exclusion(d, uid, reasons=["blur", "fold"], channels=["CD8"])
    assert uid in d["exclusions"]
    assert D.exclusion_type(["blur"]) == "blur" and D.exclusion_type(["fold"]) == "fold"
    assert D.exclusion_type(["blur", "fold"]) == D.exclusion_type(["other"]) == "manual_annotation"
    with pytest.raises(D.RegionsError):
        D.add_exclusion(d, G.circle_polygon(100, 100, 10, SHAPE), [])
    with pytest.raises(D.RegionsError):
        D.update_exclusion(d, uid, channels=[])
    assert d["operations"][-1]["workspace_id"] == "ws_a"


def test_exclusion_checks_and_the_link_to_the_overlapping_core():
    s, d = _finished()
    cy, cx = _place(1, 1)
    inside = D.add_exclusion(d, G.circle_polygon(cy, cx, 30, SHAPE), ["fold"])
    apart = D.add_exclusion(d, G.circle_polygon(1300, 1000, 30, SHAPE), ["blur"], channels=["CD8"])
    twin = D.add_exclusion(d, G.circle_polygon(cy, cx, 30, SHAPE), ["fold"])
    probs = K.lock_problems(d, s, channel_names=["DAPI"])
    assert any("the same shape and type" in p for p in probs)
    assert any("unknown channels ['CD8']" in p for p in probs)
    D.delete_exclusion(d, twin)
    D.update_exclusion(d, apart, channels="all")
    assert K.lock_problems(d, s, channel_names=["DAPI"]) == []
    excl = K.exclusion_regions(d)
    links = K.exclusion_cores(d, excl, K.core_regions(d))
    assert _label(d, links[inside]) == "X1-Y01" and links[apart] is None


def test_a_document_without_a_map_sheet_locks_its_exclusions_only(project):
    d = D.new_document(SID, SHAPE)
    D.add_exclusion(d, G.circle_polygon(400, 400, 50, SHAPE), ["blur"])
    d["coverage_confirmed"] = "2026-10-18T10:00:00"
    rec = K.lock(project, d, None)
    assert rec["version"] == 1 and rec["cores"] == {} and len(rec["exclusions"]) == 1
    assert not os.path.exists(tc.table_path(project, 1))
    e = next(iter(rec["exclusions"].values()))
    assert e["type"] == "blur" and e["region_id"].startswith("reg_") and e["core_uid"] is None


# ── lock, versions, provenance ──────────────────────────────────────────

def test_lock_writes_both_files_counts_up_after_old_files_and_registers(project):
    s, d = _finished("rot180")
    D.set_session(d, "ws_full", "/p/rois/ws_full/step0/x.json", "22" * 32)
    D.add_exclusion(d, G.circle_polygon(*_place(2, 2, "rot180"), 30, SHAPE), ["blur"])
    os.makedirs(K._dir(project), exist_ok=True)
    open(os.path.join(K._dir(project), "tma_layout_v3.json"), "w").close()   # an F3 version exists
    with pytest.raises(D.RegionsError):
        K.lock(project, d, s)                                          # coverage not confirmed
    d["coverage_confirmed"] = "2026-10-18T10:00:00"
    rec = K.lock(project, d, s)
    assert rec["version"] == 4 and K.locked_versions(project) == [4]
    rows = tc.read_table(tc.table_path(project, 4))
    from block01.core.project_identity import region_id
    for r in (r for r in rows if r["region_id"]):
        g = json.loads(r["geometry"])
        assert r["region_id"] == region_id(SID, "TMA_core", g["bbox_fullres"], g["polygon_fullres"])
    e = [e for e in prov.load_entries(project) if e["kind"] == "regions_layout"]
    assert len(e) == 1 and e[0]["unresolved_inputs"] == [
        {"role": "step0_handoff", "workspace_id": "ws_full", "path": "/p/rois/ws_full/step0/x.json", "sha256": "22" * 32}]
    d2 = K.draft_from_locked(K.load_locked(project, 4))
    assert d2["based_on"] == 4 and len(d2["exclusions"]) == 1 and set(d2["cores"]) == set(rec["cores"])
    d2["coverage_confirmed"] = "x"
    assert K.lock(project, d2, s)["version"] == 5


def test_overlapping_cores_and_degenerate_outlines_block_the_lock():
    s, d = _finished()
    a = next(u for u in d["cores"] if _label(d, u) == "X1-Y01")
    b = next(u for u in d["cores"] if _label(d, u) == "X1-Y02")
    pb = d["cores"][b]["pieces"][0]
    D.translate_shape(d, pb, -120, 0)
    assert any("overlap" in p for p in K.lock_problems(d, s))
    D.translate_shape(d, pb, 120, 0)
    pa = d["cores"][a]["pieces"][0]
    D.translate_shape(d, pa, 0, -10000)                                # pushed off the slide: clipped to a line
    assert any("degenerate" in p for p in K.lock_problems(d, s))


def test_draft_file_round_trip_and_discard(project):
    s, d = _finished()
    K.save_draft(project, d)
    assert K.load_draft(project) == d
    K.discard_draft(project)
    assert K.load_draft(project) is None


# ── migration of F3 data ────────────────────────────────────────────────

def _f3_draft():
    return {"schema_version": 1, "slide_id": SID, "map_sha256": SHA, "candidates_version": 1,
            "core_diameter_px": DIA, "based_on": None,
            "pieces": {"p1": {"cy": 250.0, "cx": 250.0, "r": 100.0, "source": "detected", "candidate_id": 1,
                              "tissue": 0.9, "owner": "core_a", "dropped": None},
                       "p2": {"cy": 600.0, "cx": 600.0, "r": 20.0, "source": "drawn", "candidate_id": None,
                              "tissue": None, "owner": None, "dropped": "dust"}},
            "cores": {"core_a": {"pieces": ["p1"], "position_key": _key(1, 1), "custom_label": None,
                                 "mapping": "matched"}},
            "positions": {_key(1, 1): {"observed": "present", "missing_reason": "", "qc": "unreviewed",
                                       "qc_reasons": [], "run": True}},
            "operations": [{"kind": "bind"}], "coverage_confirmed": None}


def test_an_f3_draft_is_converted_once_and_never_comes_back(project):
    os.makedirs(K._dir(project), exist_ok=True)
    old = os.path.join(K._dir(project), M.F3_DRAFT)
    with open(old, "w") as f:
        json.dump(_f3_draft(), f)
    d = M.migrate_draft(project, SHAPE)
    assert d["schema_version"] == 2 and len(d["pieces"]["p1"]["polygon"]) == 64
    assert d["cores"]["core_a"]["position_key"] == _key(1, 1) and d["pieces"]["p2"]["dropped"] == "dust"
    assert not os.path.exists(old) and os.path.exists(old + ".migrated")
    K.discard_draft(project)
    assert M.migrate_draft(project, SHAPE) is None                    # nothing comes back


def test_when_both_drafts_exist_the_regions_draft_wins(project):
    os.makedirs(K._dir(project), exist_ok=True)
    mine = D.new_document(SID, SHAPE)
    D.add_exclusion(mine, G.circle_polygon(300, 300, 40, SHAPE), ["fold"])
    K.save_draft(project, mine)
    old = os.path.join(K._dir(project), M.F3_DRAFT)
    with open(old, "w") as f:
        json.dump(_f3_draft(), f)
    d = M.migrate_draft(project, SHAPE)
    assert d == mine and not os.path.exists(old)
    K.discard_draft(project)
    assert M.migrate_draft(project, SHAPE) is None


def test_an_f3_locked_version_is_read_only_input(project):
    os.makedirs(K._dir(project), exist_ok=True)
    rec = dict(_f3_draft(), version=1)
    path = os.path.join(K._dir(project), "tma_layout_v1.json")
    with open(path, "w") as f:
        json.dump(rec, f)
    before = open(path).read()
    d = M.load_f3_locked(project, 1, SHAPE)
    assert d["cores"]["core_a"]["mapping"] == "matched" and M.f3_locked_versions(project) == [1]
    assert open(path).read() == before


# ── detection outlines (format 2) ───────────────────────────────────────

def test_grown_parts_never_trim_tissue_and_leave_a_gap_between_neighbours():
    from block01.core.tma.dearray import grown_parts
    lab = np.zeros((60, 80), np.int32)
    yy, xx = np.mgrid[0:60, 0:80]
    lab[(yy - 30) ** 2 + (xx - 25) ** 2 <= 15 ** 2] = 1
    lab[((yy - 30) ** 2 + (xx - 55) ** 2 <= 15 ** 2) & (lab == 0)] = 2   # touching discs (split pair)
    lab[(yy - 3) ** 2 + (xx - 75) ** 2 <= 6 ** 2] = 3                    # at the image edge
    g = grown_parts(lab, 3)
    for k in (1, 2, 3):
        assert ((lab == k) <= (g == k)).all(), "a piece keeps every pixel"
        assert (g == k).sum() > (lab == k).sum(), "and grows"
    added = (lab == 0) & (g > 0)
    pad = np.pad(g, 1)
    for y, x in zip(*np.nonzero(added)):
        nb = pad[y:y + 3, x:x + 3]
        assert set(np.unique(nb)) <= {0, g[y, x]}, "a grown pixel never touches another piece"


def test_detected_outlines_cover_the_tissue_and_neighbours_do_not_overlap(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    from block01.core.tma import dearray as da
    rng = np.random.default_rng(4)
    h, w = 600, 700
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w), np.float32)
    discs = [(200, 150), (200, 240), (450, 500), (40, 650)]           # a touching pair, one alone, one at the edge
    for cy, cx in discs:
        m = (yy - cy) ** 2 + (xx - cx) ** 2 <= 50 ** 2
        img[m] = rng.uniform(20, 200, m.sum())
    path = tmp_path / "s.ome.tif"
    tifffile.imwrite(str(path), np.stack([img.astype(np.uint8), np.zeros((h, w), np.uint8)]), ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}})
    r = da.detect_cores(str(path), core_diameter_px=100, channel=0, params={"min_core_px": 20})
    assert r["schema_version"] == 2
    cands = r["candidates"]
    assert all(len(c["outline"]) >= 3 and {"cy", "cx", "radius", "area"} <= set(c) for c in cands)
    regs = [G.region_outline([c["outline"]], (h, w)) for c in cands]
    for i in range(len(regs)):
        for j in range(i + 1, len(regs)):
            assert not G.overlap(regs[i], regs[j]), (i, j)
    for g, c in zip(regs, cands):                                      # slightly larger than the equal-area circle
        assert G.area(g["polygon_fullres"]) > c["area"]
    # every tissue pixel lies inside a filled outline (level 0 here; the
    # touching pair's split line hands some pixels to the neighbour)
    covered = np.zeros((h, w), bool)
    for g in regs:
        y0, y1, x0, x1 = g["bbox_fullres"]
        covered[y0:y1, x0:x1] |= G.fill(g["polygon_fullres"], g["bbox_fullres"])
    tissue = img > 0
    assert covered[tissue].mean() > 0.999


def test_format_1_candidate_lists_are_still_read(tmp_path):
    from block01.core.tma import dearray as da
    proj = tmp_path / "p"
    (proj / "tma").mkdir(parents=True)
    (proj / "tma" / "dearray_candidates_v1.json").write_text(json.dumps({"schema_version": 1, "candidates": []}))
    assert da.load_candidates(str(proj), 1)["schema_version"] == 1
    (proj / "tma" / "dearray_candidates_v2.json").write_text(json.dumps({"schema_version": 7, "candidates": []}))
    with pytest.raises(ValueError):
        da.load_candidates(str(proj), 2)


# ── R1 review fixes (codex_regions_r1_1.out) ────────────────────────────

def test_a_workspace_saved_again_keeps_both_handoffs(project):
    d = D.new_document(SID, SHAPE)
    D.set_session(d, "ws_a", "/a.json", "aa" * 32)
    D.add_exclusion(d, G.circle_polygon(300, 300, 40, SHAPE), ["blur"])
    D.set_session(d, "ws_a", "/a.json", "bb" * 32)                    # Step0 saved again
    D.add_exclusion(d, G.circle_polygon(700, 700, 40, SHAPE), ["fold"])
    d["coverage_confirmed"] = "x"
    K.lock(project, d, None)
    e = [e for e in prov.load_entries(project) if e["kind"] == "regions_layout"][0]
    assert sorted(h["sha256"] for h in e["unresolved_inputs"]) == ["aa" * 32, "bb" * 32]
    from block01.core.project_identity import region_id
    assert e["operates_on"] == [region_id(SID, "full_wsi", [0, SHAPE[0], 0, SHAPE[1]])]


def test_an_exclusion_still_belongs_to_a_qc_excluded_core_and_ties_go_to_the_larger_overlap():
    s, d = _finished()
    cy, cx = _place(1, 1)
    inside = D.add_exclusion(d, G.circle_polygon(cy, cx, 30, SHAPE), ["fold"])
    D.set_qc(d, _key(1, 1), "excluded", ["torn"])
    links = K.exclusion_cores(d, K.exclusion_regions(d), K.core_regions(d, include_excluded=True))
    assert _label(d, links[inside]) == "X1-Y01"
    ay, ax = _place(1, 1)
    by, bx = _place(1, 2)                                              # between two cores, more in X1-Y02
    mid = D.add_exclusion(d, G.circle_polygon((ay + by) / 2 + 60, ax, 70, SHAPE), ["blur"])
    links = K.exclusion_cores(d, K.exclusion_regions(d), K.core_regions(d, include_excluded=True))
    assert _label(d, links[mid]) == "X1-Y02"


def test_readers_refuse_another_slide_and_unknown_f3_schemas(project):
    d = D.new_document(SID, SHAPE)
    D.set_session(d, "ws", "/x", "cc" * 32)
    K.save_draft(project, d)
    with pytest.raises(D.RegionsError):
        K.load_draft(project, slide_id="slide_ffffffffffffffff")
    assert K.load_draft(project, slide_id=SID)["session"] is None      # the session is not carried over
    K.discard_draft(project)
    old = os.path.join(K._dir(project), M.F3_DRAFT)
    with open(old, "w") as f:
        json.dump(dict(_f3_draft(), schema_version=99), f)
    with pytest.raises(D.RegionsError):
        M.migrate_draft(project, SHAPE, slide_id=SID)
    assert os.path.exists(old) and K.load_draft(project) is None      # refused and left where it was
    with open(old, "w") as f:
        json.dump(dict(_f3_draft(), slide_id="slide_ffffffffffffffff"), f)
    with pytest.raises(D.RegionsError):
        M.migrate_draft(project, SHAPE, slide_id=SID)
    d.update(coverage_confirmed="x")
    D.add_exclusion(d, G.circle_polygon(300, 300, 40, SHAPE), ["blur"])
    K.lock(project, d, None)
    with pytest.raises(D.RegionsError):
        K.load_locked(project, K.locked_versions(project)[-1], slide_id="slide_ffffffffffffffff")


def test_a_document_with_a_map_sheet_needs_its_study_to_lock():
    s, d = _finished()
    with pytest.raises(D.RegionsError):
        K.lock_problems(d, None)
    other = _study()
    other["map_sheet"] = dict(other["map_sheet"], sha256="cd" * 32)
    with pytest.raises(D.RegionsError):
        K.lock_problems(d, other)
