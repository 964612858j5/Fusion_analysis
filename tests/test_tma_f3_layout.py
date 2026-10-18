"""TMA F3 layout (docs/tma_f3_page_application.md §1-§3): numbering by
segments / clicks / one core, merging pieces, the four separate marks, the
region outline and the locked versions.

A small synthetic map (4 rows x 3 columns, two empty positions) laid on a
synthetic slide in several placements (as is, turned 180 degrees,
mirrored): the numbering is the user's, so every placement is numbered the
same way -- a line from the first record's core to the last.
"""

import json
import math

import numpy as np
import pytest

from block01.core import provenance as prov
from block01.core.tma import cores as tc
from block01.core.tma import layout as L
from block01.core.tma import layout_geometry as G
from block01.core.tma import layout_lock as K

ROWS, COLS = 4, 3
EMPTY = {(3, 3), (3, 4)}
SHAPE = (1400, 1100)
D = 200.0
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


def _place(x, y, placement):
    """Image centre (cy, cx) of map position (x, y)."""
    cx, cy = 250 + (x - 1) * 300, 250 + (y - 1) * 300
    if placement == "rot180":
        cx, cy = 250 + (COLS - x) * 300, 250 + (ROWS - y) * 300
    elif placement == "mirror":
        cx = 250 + (COLS - x) * 300
    return float(cy), float(cx)


def _candidates(placement="identity", missing=(), extra=()):
    cands, k = [], 0
    for x in range(1, COLS + 1):
        for y in range(1, ROWS + 1):
            if (x, y) in EMPTY or (x, y) in missing:
                continue
            k += 1
            cy, cx = _place(x, y, placement)
            cands.append({"candidate_id": k, "cy": cy, "cx": cx, "radius": D / 2, "area": math.pi * (D / 2) ** 2})
    for cy, cx, r in extra:
        k += 1
        cands.append({"candidate_id": k, "cy": cy, "cx": cx, "radius": r, "area": math.pi * r * r})
    return {"version": 1, "core_diameter_px": D, "candidates": cands}


def _objects(d):
    out = [(uid, *L.core_centre(d, uid)) for uid in d["cores"]]
    out += [(pid, p["cy"], p["cx"]) for pid, p in d["pieces"].items() if p["owner"] is None and not p["dropped"]]
    return out


def _number_column(d, study, x, placement):
    first = (x - 1) * ROWS + 1
    last = (x - 1) * ROWS + ROWS
    recs = L.target_records(study, first, last, "column")
    ys = [p["y"] for p in study["map_sheet"]["positions"] if p["x"] == x and p["donor_block_id"]]
    sel = L.band_select(_objects(d), _place(x, min(ys), placement), _place(x, max(ys), placement), D / 2)
    plan = L.plan_binding(d, sel, recs)
    assert plan["ok"], plan["problems"]
    return L.apply_binding(d, plan, "line", rebind=bool(plan["conflicts"]))


def _key(study, x, y):
    return tc.position_key(SID, SHA, (x - 1) * ROWS + y)


def _holder_label(d, study, uid):
    key = d["cores"][uid]["position_key"]
    return next(f"X{p['x']}-Y{p['y']:02d}" for p in study["map_sheet"]["positions"]
                if tc.position_key(SID, SHA, p["position_id"]) == key)


# ── target records ──────────────────────────────────────────────────────

def test_target_records_skip_empty_positions_only():
    s = _study()
    recs = L.target_records(s, 9, 12, "column")
    assert [r["label"] for r in recs] == ["X3-Y01", "X3-Y02"]          # Y03, Y04 empty
    row = L.target_records(s, 1, 9, "row")                            # X1-Y01 .. X3-Y01 along row 1
    assert [r["label"] for r in row] == ["X1-Y01", "X2-Y01", "X3-Y01"]
    rev = L.target_records(s, 4, 1, "column_rev")
    assert [r["label"] for r in rev] == ["X1-Y04", "X1-Y03", "X1-Y02", "X1-Y01"]
    with pytest.raises(L.LayoutError):
        L.target_records(s, 4, 1, "column")                            # end before start
    assert [r["label"] for r in L.records_by_ids(s, [6, 2])] == ["X2-Y02", "X1-Y02"]
    with pytest.raises(L.LayoutError):
        L.records_by_ids(s, [11])                                      # empty on the map


@pytest.mark.parametrize("placement", ["identity", "rot180", "mirror"])
def test_every_placement_is_numbered_by_the_users_lines(placement):
    s = _study()
    d = L.new_draft(s, _candidates(placement))
    for x in range(1, COLS + 1):
        _number_column(d, s, x, placement)
    for uid, c in d["cores"].items():
        cy, cx = L.core_centre(d, uid)
        lab = _holder_label(d, s, uid)
        x, y = int(lab[1]), int(lab[4:])
        assert (cy, cx) == _place(x, y, placement)
        assert c["mapping"] == "matched"


def test_a_missing_core_is_bypassed_by_two_segments_without_shifting():
    s = _study()
    d = L.new_draft(s, _candidates(missing={(1, 2)}))
    objs = _objects(d)
    # one line over column 1 sees 3 cores against 4 records: refused
    sel = L.band_select(objs, _place(1, 1, "identity"), _place(1, 4, "identity"), D / 2)
    assert not L.plan_binding(d, sel, L.target_records(s, 1, 4, "column"))["ok"]
    # two segments: Y01 alone, then Y03..Y04
    a = L.band_select(objs, _place(1, 1, "identity"), _place(1, 1.2, "identity"), D / 2)
    L.apply_binding(d, L.plan_binding(d, a, L.target_records(s, 1, 1, "column")), "line")
    b = L.band_select(objs, _place(1, 3, "identity"), _place(1, 4, "identity"), D / 2)
    L.apply_binding(d, L.plan_binding(d, b, L.target_records(s, 3, 4, "column")), "line")
    labels = sorted(_holder_label(d, s, u) for u, c in d["cores"].items() if c["position_key"])
    assert labels == ["X1-Y01", "X1-Y03", "X1-Y04"]
    assert L.status_text(d, _key(s, 1, 2), True) == "未对应"           # not made missing by itself
    L.set_missing(d, _key(s, 1, 2))
    assert L.status_text(d, _key(s, 1, 2), True) == "缺失"


def test_band_order_and_reverse_and_diagonal():
    objs = [("a", 0, 0), ("b", 100, 100), ("c", 200, 200), ("off", 0, 200)]
    assert L.band_select(objs, (0, 0), (200, 200), 20) == ["a", "b", "c"]
    assert L.band_select(objs, (200, 200), (0, 0), 20) == ["c", "b", "a"]
    with pytest.raises(L.LayoutError):
        L.band_select(objs, (5, 5), (5, 5), 20)


def test_conflicts_are_previewed_and_rebinding_keeps_the_pushed_core():
    s = _study()
    d = L.new_draft(s, _candidates())
    _number_column(d, s, 1, "identity")
    a = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    b = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y02")
    pieces_b = list(d["cores"][b]["pieces"])
    plan = L.plan_binding(d, [a], L.records_by_ids(s, [2]))           # X1-Y01's core -> X1-Y02
    kinds = sorted(c["kind"] for c in plan["conflicts"])
    assert kinds == ["object_numbered", "record_held"]
    with pytest.raises(L.LayoutError):
        L.apply_binding(d, plan, "single")                             # not without the rebind
    L.apply_binding(d, plan, "single", rebind=True)
    assert _holder_label(d, s, a) == "X1-Y02"
    assert d["cores"][b]["position_key"] is None and d["cores"][b]["pieces"] == pieces_b
    assert L.status_text(d, _key(s, 1, 1), True) == "未对应"
    # changing one core's number renumbers no other core
    others = {u: c["position_key"] for u, c in d["cores"].items() if u not in (a, b)}
    L.apply_binding(d, L.plan_binding(d, [b], L.records_by_ids(s, [1])), "single")
    assert {u: d["cores"][u]["position_key"] for u in others} == others


def test_swap_and_custom_core_completion():
    s = _study()
    d = L.new_draft(s, _candidates())
    _number_column(d, s, 1, "identity")
    a, b = sorted(d["cores"], key=lambda u: _holder_label(d, s, u) if d["cores"][u]["position_key"] else "Z")[:2]
    la, lb = _holder_label(d, s, a), _holder_label(d, s, b)
    L.swap(d, a, b)
    assert (_holder_label(d, s, a), _holder_label(d, s, b)) == (lb, la)
    free = next(u for u, c in d["cores"].items() if not c["position_key"])
    L.set_custom(d, free, "EXTRA-1")
    assert any("custom core EXTRA-1" in p for p in K.lock_problems(d, s, SHAPE))
    L.apply_binding(d, L.plan_binding(d, [free], L.records_by_ids(s, [5])), "single")   # completed
    assert d["cores"][free]["custom_label"] is None and _holder_label(d, s, free) == "X2-Y01"


def test_merge_sends_the_number_to_review_and_unmerge_restores_the_box():
    s = _study()
    cy, cx = _place(1, 1, "identity")
    d = L.new_draft(s, _candidates(extra=[(cy, cx + 160, 30.0)]))   # beside the column: outside its band
    frag = next(p for p, v in d["pieces"].items() if v["owner"] is None)   # small: starts unassigned
    _number_column(d, s, 1, "identity")
    core = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    box0 = G.core_geometry([d["pieces"][p] for p in d["cores"][core]["pieces"]], SHAPE)["bbox_fullres"]
    L.merge(d, core, frag)
    g = G.core_geometry([d["pieces"][p] for p in d["cores"][core]["pieces"]], SHAPE)
    assert len(d["cores"][core]["pieces"]) == 2 and g["bbox_fullres"][3] > box0[3]
    assert d["cores"][core]["mapping"] == "review"
    assert L.status_text(d, _key(s, 1, 1), True) == "待复核"
    L.confirm_number(d, core)
    assert d["cores"][core]["mapping"] == "matched"
    L.unmerge(d, frag)
    assert d["pieces"][frag]["owner"] is None and d["cores"][core]["mapping"] == "review"
    assert G.core_geometry([d["pieces"][p] for p in d["cores"][core]["pieces"]], SHAPE)["bbox_fullres"] == box0


def test_the_four_marks_are_kept_apart():
    s = _study()
    d = L.new_draft(s, _candidates())
    _number_column(d, s, 1, "identity")
    key = _key(s, 1, 1)
    with pytest.raises(L.LayoutError):
        L.set_qc(d, key, "excluded")                                   # needs a reason
    L.set_qc(d, key, "excluded", ["fold"])
    pos = d["positions"][key]
    assert pos["observed"] == "present" and pos["qc"] == "excluded"
    assert L.status_text(d, key, True) == "已对应·排除"
    assert L.status_text(d, _key(s, 3, 4), False) == "空位"
    with pytest.raises(L.LayoutError):
        L.set_missing(d, key)                                          # bound: not missing


# ── geometry ────────────────────────────────────────────────────────────

def _roimask(poly, bbox):
    from block01.core.preseg_input import RoiMask
    m = RoiMask(poly, bbox)
    return m.window(bbox)


def _union(pieces, bbox):
    import cv2
    y0, y1, x0, x1 = bbox
    canvas = np.zeros((y1 - y0, x1 - x0), np.uint8)
    for p in pieces:
        poly = G.circle_polygon(p["cy"], p["cx"], p["r"], SHAPE)
        cv2.fillPoly(canvas, [np.array([[x - x0, y - y0] for x, y in poly], np.int32)], 1)
    found, _ = cv2.findContours(canvas, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(canvas)
    cv2.drawContours(out, found, -1, 1, thickness=cv2.FILLED)
    return out.astype(bool)


CASES = {
    "apart": [dict(cy=300, cx=300, r=60), dict(cy=300, cx=520, r=40)],
    "overlap": [dict(cy=300, cx=300, r=25), dict(cy=300, cx=320, r=25)],
    "inside": [dict(cy=300, cx=300, r=80), dict(cy=310, cx=300, r=20)],
    "touching": [dict(cy=300, cx=300, r=50), dict(cy=300, cx=400, r=50)],
    "ring": [dict(cy=300, cx=300, r=50), dict(cy=300, cx=460, r=30), dict(cy=460, cx=300, r=30),
             dict(cy=150, cx=300, r=30), dict(cy=300, cx=140, r=25)],
    "edge": [dict(cy=10, cx=500, r=60), dict(cy=120, cx=500, r=30)],
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_multi_piece_outline_fills_as_the_union_plus_thin_bridges(name):
    pieces = CASES[name]
    g = G.core_geometry(pieces, SHAPE)
    bbox = g["bbox_fullres"]
    filled = _roimask(g["polygon_fullres"], bbox)
    assert np.array_equal(filled, G.fill(g["polygon_fullres"], bbox))   # our fill is RoiMask's
    union = _union(pieces, bbox)
    assert not (union & ~filled).any(), "every piece pixel is in the region"
    extra = filled & ~union
    assert extra.sum() <= 3 * sum(abs(a - b) for a, b in zip(bbox[::2], bbox[1::2])), "only thin bridges"
    from block01.core.project_identity import normalize_polygon
    norm = normalize_polygon(g["polygon_fullres"])
    assert np.array_equal(G.fill(norm, bbox), filled)                   # orientation / start do not change the fill


def test_the_outline_is_deterministic_and_its_id_follows_the_geometry():
    from block01.core.project_identity import region_id
    pieces = CASES["ring"]
    a = G.core_geometry(pieces, SHAPE)
    b = G.core_geometry(list(reversed(pieces)), SHAPE)
    assert a["polygon_fullres"] == b["polygon_fullres"]
    rid = region_id(SID, "TMA_core", a["bbox_fullres"], a["polygon_fullres"])
    moved = [dict(p) for p in pieces]
    moved[2]["cx"] += 7
    c = G.core_geometry(moved, SHAPE)
    assert region_id(SID, "TMA_core", c["bbox_fullres"], c["polygon_fullres"]) != rid
    tiny = [dict(p) for p in pieces]
    tiny[2]["cx"] += 0.01                                              # below a pixel: same outline
    assert G.core_geometry(tiny, SHAPE)["polygon_fullres"] == a["polygon_fullres"]


def test_bbox_is_half_open_and_clipped():
    poly = [[0, 0], [9, 0], [9, 4]]
    assert G.bbox_of(poly, (100, 100)) == [0, 5, 0, 10]
    assert G.bbox_of([[95, 98], [99, 99], [97, 90]], (100, 100)) == [90, 100, 95, 100]


# ── lock ────────────────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "project_manifest.json").write_text(json.dumps({"version": 1, "project_schema_version": 1}))
    return str(d)


def _finished(placement="identity"):
    s = _study()
    d = L.new_draft(s, _candidates(placement))
    for x in range(1, COLS + 1):
        _number_column(d, s, x, placement)
    return s, d


def test_lock_problems_list_every_open_item():
    s = _study()
    cy, cx = _place(2, 2, "identity")
    d = L.new_draft(s, _candidates(missing={(1, 2)}, extra=[(cy + 150, cx + 150, 20.0)]))
    probs = K.lock_problems(d, s, SHAPE)
    assert any("neither bound nor confirmed missing" in p for p in probs)
    assert any("belongs to no core" in p for p in probs)
    assert any("has no number" in p for p in probs)


def test_overlapping_regions_block_the_lock_but_touching_boxes_do_not():
    s, d = _finished()
    a = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    b = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y02")
    assert K.lock_problems(d, s, SHAPE) == []
    pb = d["pieces"][d["cores"][b]["pieces"][0]]
    pb["cy"] -= 120                                                     # 180 apart, radius 100: overlap
    assert any("overlap" in p for p in K.lock_problems(d, s, SHAPE))
    pb["cy"] += 120
    c = next(u for u in d["cores"] if _holder_label(d, s, u) == "X2-Y02")   # diagonal neighbour, 424 apart
    for u in (a, c):
        d["pieces"][d["cores"][u]["pieces"][0]]["r"] = 160               # boxes overlap, circles do not
    ga = G.core_geometry([d["pieces"][p] for p in d["cores"][a]["pieces"]], SHAPE)["bbox_fullres"]
    gc = G.core_geometry([d["pieces"][p] for p in d["cores"][c]["pieces"]], SHAPE)["bbox_fullres"]
    assert ga[1] > gc[0] and ga[3] > gc[2]
    assert not any("overlap" in p for p in K.lock_problems(d, s, SHAPE))


def test_a_bridge_through_a_neighbour_blocks_the_lock():
    s, d = _finished()
    a = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    cy, cx = _place(1, 3, "identity")                                   # a fragment beyond X1-Y02
    frag = L.add_piece(d, cy, cx + 0, 10.0)
    L.merge(d, a, frag)
    L.confirm_number(d, a)
    assert any("overlap" in p for p in K.lock_problems(d, s, SHAPE))


def test_lock_writes_versions_that_only_count_up(project):
    s, d = _finished("rot180")
    with pytest.raises(L.LayoutError):
        K.lock(project, d, s, SHAPE)                                   # coverage not confirmed
    d["coverage_confirmed"] = "2026-10-17T12:00:00"
    key = _key(s, 2, 2)
    d["positions"][key]["run"] = True
    rec = K.lock(project, d, s, SHAPE)
    assert rec["version"] == 1 and rec["run"] == [key]
    rows = tc.read_table(tc.table_path(project, 1))
    bound = [r for r in rows if r["region_id"]]
    assert len(bound) == ROWS * COLS - len(EMPTY)
    from block01.core.project_identity import region_id
    for r in bound:
        g = json.loads(r["geometry"])
        assert r["region_id"] == region_id(SID, "TMA_core", g["bbox_fullres"], g["polygon_fullres"])
    assert {r["status"] for r in rows if not r["occupied"]} == {"空位"}
    entries = [e for e in prov.load_entries(project) if e["kind"] == "tma_layout"]
    assert len(entries) == 1
    # a new draft on v1, changed and locked: v2; v1 stays as it was
    d2 = K.draft_from_locked(K.load_locked(project, 1))
    assert d2["based_on"] == 1
    u = next(iter(d2["cores"]))
    d2["pieces"][d2["cores"][u]["pieces"][0]]["r"] -= 5
    d2["coverage_confirmed"] = "2026-10-17T13:00:00"
    assert K.lock(project, d2, s, SHAPE)["version"] == 2
    assert tc.read_table(tc.table_path(project, 1)) == rows
    assert K.locked_versions(project) == [1, 2]


def test_the_draft_file_round_trips_and_can_be_discarded(project):
    s, d = _finished()
    K.save_draft(project, d)
    assert K.load_draft(project) == d
    K.discard_draft(project)
    assert K.load_draft(project) is None


# ── the page's draft holder (ui/tma/draft.py, no Qt) ───────────────────

def test_undo_redo_one_step_per_edit_and_per_drag():
    from block01.ui.tma.draft import DraftHolder, ReadOnly
    s = _study()
    saved = []
    h = DraftHolder(L.new_draft(s, _candidates()), autosave=lambda d: saved.append(len(d["operations"])))
    start = json.dumps(h.d, sort_keys=True)
    _ = h.do(lambda d: _number_column(d, s, 1, "identity"))
    numbered = json.dumps(h.d, sort_keys=True)
    with pytest.raises(L.LayoutError):
        h.do(L.set_qc, _key(s, 1, 1), "excluded")                     # fails: draft unchanged, no step
    assert json.dumps(h.d, sort_keys=True) == numbered
    uid = next(iter(h.d["cores"]))
    h.begin_drag()
    for _i in range(5):                                                # many moves, one step
        h.d["pieces"][h.d["cores"][uid]["pieces"][0]]["cx"] += 1
    h.end_drag()
    assert h.undo() and json.dumps(h.d, sort_keys=True) == numbered
    assert h.undo() and json.dumps(h.d, sort_keys=True) == start
    assert not h.undo()
    assert h.redo() and json.dumps(h.d, sort_keys=True) == numbered
    assert saved                                                       # every step autosaved
    ro = DraftHolder(h.d, read_only=True)
    with pytest.raises(ReadOnly):
        ro.do(L.set_missing, _key(s, 1, 2))
    assert not ro.can_undo


# ── review fixes (codex_tma_f3_p1_1.out) ───────────────────────────────

def test_a_range_may_cross_columns_and_rows():
    """User ruling 2026-10-18: no same-column / same-row restriction."""
    s = _study()
    assert [r["label"] for r in L.target_records(s, 3, 6, "column")] == ["X1-Y03", "X1-Y04", "X2-Y01", "X2-Y02"]
    assert [r["label"] for r in L.target_records(s, 1, 6, "row")] == ["X1-Y01", "X2-Y01", "X3-Y01", "X1-Y02", "X2-Y02"]


def test_swap_needs_two_numbered_cores_and_custom_frees_its_position():
    s = _study()
    d = L.new_draft(s, _candidates())
    _number_column(d, s, 1, "identity")
    a = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    free = next(u for u, c in d["cores"].items() if not c["position_key"])
    L.set_custom(d, free, "EXTRA")
    with pytest.raises(L.LayoutError):
        L.swap(d, a, free)
    L.set_custom(d, a, "EXTRA-2")                                      # a numbered core made custom
    assert d["positions"][_key(s, 1, 1)]["observed"] is None
    assert L.status_text(d, _key(s, 1, 1), True) == "未对应"


def test_drop_and_restore_of_a_single_and_a_multi_piece_core():
    s = _study()
    cy, cx = _place(1, 1, "identity")
    d = L.new_draft(s, _candidates(extra=[(cy, cx + 160, 30.0)]))
    _number_column(d, s, 1, "identity")
    core = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    frag = next(p for p, v in d["pieces"].items() if v["owner"] is None)
    L.merge(d, core, frag)
    L.drop_piece(d, frag, "dust")                                      # multi -> single, to review
    assert d["pieces"][frag]["owner"] is None and d["cores"][core]["mapping"] == "review"
    L.restore_piece(d, frag)
    assert d["pieces"][frag]["owner"] is None and not d["pieces"][frag]["dropped"]   # back unassigned
    only = d["cores"][core]["pieces"][0]
    L.drop_piece(d, only, "not tissue")                                # the core disappears, number freed
    assert core not in d["cores"] and d["positions"][_key(s, 1, 1)]["observed"] is None
    L.restore_piece(d, only)
    assert d["pieces"][only]["owner"] is None                          # not silently re-bound


def test_an_excluded_core_keeps_its_identity_but_writes_no_region(project):
    s, d = _finished()
    key = _key(s, 1, 1)
    L.set_qc(d, key, "excluded", ["fold"])
    d["coverage_confirmed"] = "2026-10-17T12:00:00"
    rec = K.lock(project, d, s, SHAPE)
    row = next(r for r in tc.read_table(tc.table_path(project, 1)) if r["position_key"] == key)
    assert row["region_id"] == "" and row["geometry"] == "" and "排除" in row["status"]
    held = [c for c in rec["cores"].values() if c["position_key"] == key]
    assert len(held) == 1 and held[0]["region_id"] is None and held[0]["pieces"]
    pos = rec["positions"][key]
    assert (pos["map_use"], pos["mapping"], pos["observed"], pos["qc"]) == ("sample", "matched", "present", "excluded")
    assert rec["positions"][_key(s, 3, 4)]["map_use"] == "empty"
    e = [e for e in prov.load_entries(project) if e["kind"] == "tma_layout"][0]
    assert e["depends_on"] == [] and len(e["unresolved_inputs"]) == 2     # no F1/F2 entries in this project
    d2 = K.draft_from_locked(rec)
    assert set(d2["positions"][key]) == {f for f, _ in K.DRAFT_POSITION_FIELDS}


def test_an_outline_clipped_to_a_line_is_degenerate():
    s, d = _finished()
    a = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    p = d["pieces"][d["cores"][a]["pieces"][0]]
    p["cx"], p["r"] = -100.0, 50.0                                     # wholly left of the slide
    assert any("degenerate" in q for q in K.lock_problems(d, s, SHAPE))


def test_missing_keeps_a_reason_and_an_interrupted_lock_keeps_its_number(project):
    s, d = _finished()
    u = next(u for u in d["cores"] if _holder_label(d, s, u) == "X2-Y03")
    L.unbind(d, u)
    L.drop_piece(d, d["cores"][u]["pieces"][0], "torn")
    L.set_missing(d, _key(s, 2, 3), reason="lost in staining")
    assert d["positions"][_key(s, 2, 3)]["missing_reason"] == "lost in staining"
    import os
    os.makedirs(os.path.dirname(tc.table_path(project, 1)), exist_ok=True)
    open(tc.table_path(project, 1), "w").close()                       # a lock that died after its table
    assert K.locked_versions(project) == []
    d["coverage_confirmed"] = "2026-10-17T12:00:00"
    assert K.lock(project, d, s, SHAPE)["version"] == 2
    assert K.locked_versions(project) == [2]


def test_review_round_two_transitions():
    """codex_tma_f3_p1_2.out: an exchange confirms both numbers; leaving
    'missing' clears its reason; undo is refused during a drag; a no-op
    edit keeps the redo; review and run tick block the lock."""
    from block01.ui.tma.draft import DraftHolder
    s = _study()
    cy, cx = _place(1, 1, "identity")
    d = L.new_draft(s, _candidates(extra=[(cy, cx + 160, 30.0)]))
    _number_column(d, s, 1, "identity")
    a = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y01")
    b = next(u for u in d["cores"] if _holder_label(d, s, u) == "X1-Y02")
    frag = next(p for p, v in d["pieces"].items() if v["owner"] is None)
    L.merge(d, a, frag)
    assert any("to be confirmed" in p for p in K.lock_problems(d, s, SHAPE))
    L.swap(d, a, b)
    assert d["cores"][a]["mapping"] == d["cores"][b]["mapping"] == "matched"
    key = _key(s, 2, 1)
    L.set_missing(d, key, reason="gone")
    free = next(u for u in d["cores"] if not d["cores"][u]["position_key"])
    L.apply_binding(d, L.plan_binding(d, [free], L.records_by_ids(s, [5])), "single")
    assert d["positions"][key]["missing_reason"] == "" and d["positions"][key]["observed"] == "present"
    d["positions"][_key(s, 3, 1)]["run"] = True
    probs = K.lock_problems(d, s, SHAPE)
    assert any("ticked to run" in p for p in probs)
    h = DraftHolder(d)
    h.do(L.unbind, b)
    assert h.undo() and h.can_redo
    h.do(L.merge, a, frag)                                             # already there: no step
    assert h.can_redo
    h.begin_drag()
    assert not h.can_undo and not h.undo()
    with pytest.raises(RuntimeError):
        h.do(L.unbind, a)
    h.end_drag(moved=False)
    assert h.can_redo and not h.can_undo                               # the drag took no step
