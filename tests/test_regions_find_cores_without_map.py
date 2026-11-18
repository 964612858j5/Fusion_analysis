"""Find cores without the TMA map sheet (docs/v17_find_cores_without_map_plan.md
stage 1, §3.2, §3.4.2-7), offscreen on the Regions page:

* Find cores runs with no map sheet and asks no diameter (the size is
  estimated); not in an ROI workspace, not without a DAPI channel;
* the cores land in the document without a map sheet, in one undo step;
* choosing the map sheet afterwards keeps them (ids, outlines, owners,
  exclusions, counters) and numbering works;
* after an Undo of the attach (or a Revert to a Save before it) numbering
  follows the document's map sheet: refused, and `Attach map sheet` brings
  it back;
* with the map sheet first, `attach_tma` builds the same document as before;
* a Save without a map sheet: the analysis is the whole region, the found
  cores are drawn only.

The page, the synthetic slide and the workbook are `test_tma_f3_page.py`'s.
"""

import copy
import importlib.util
import os
import sys

import pytest

pytest.importorskip("openpyxl")

_F3 = os.path.join(os.path.dirname(__file__), "test_tma_f3_page.py")
_spec = importlib.util.spec_from_file_location("_tma_f3_page_helpers", _F3)
f3 = importlib.util.module_from_spec(_spec)
sys.modules["_tma_f3_page_helpers"] = f3
_spec.loader.exec_module(f3)

from block01.core.regions import document as D  # noqa: E402
from block01.core.regions import lock as K  # noqa: E402
from block01.qt.QtWidgets import QInputDialog, QMessageBox  # noqa: E402

app = f3.app
env = f3.env
N_CORES = 5                       # the 3 x 2 slide with position 6 empty


@pytest.fixture
def no_dialog(monkeypatch):
    """Any diameter question fails the test (there must be none)."""
    def ask(*a, **k):
        raise AssertionError("a diameter was asked for")
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(ask))


def _find(app, page):
    page.cores.btn_find.click()
    f3._wait(app, lambda: not page.cores._detecting)


def _without_map(app, env, monkeypatch):
    page = f3._page(app, env, monkeypatch, find=False)
    assert page.cores.study is None
    assert page.cores.btn_find.isEnabled()                                  # no map sheet needed
    _find(app, page)
    d = page.holder.d
    assert len(d["pieces"]) == N_CORES and len(d["cores"]) == N_CORES
    assert not d["map_sha256"] and d["positions"] == {}
    return page


def test_find_cores_runs_without_a_map_sheet_and_asks_no_size(app, env, monkeypatch, no_dialog):
    page = _without_map(app, env, monkeypatch)
    info = page.cores.lbl_info.text()
    assert "5 found" in info and "core size estimated" in info and "Choose the map sheet" in info
    rec = page.cores.candidates
    assert rec["core_diameter_source"] == "estimated"
    assert abs(rec["core_diameter_px"] - f3.DIA) <= 0.15 * f3.DIA
    assert not page.cores.numbering.isEnabled() and not page.cores.numbering_allowed()
    assert page.cores.btn_map.text() == "Choose map sheet…" and page.cores.btn_map.isEnabled()
    page.undo()                                                             # one step takes them all back
    assert page.holder.d["pieces"] == {} and page.holder.d["cores"] == {}
    page.shutdown()


def test_find_cores_needs_dapi_and_the_whole_slide(app, env, monkeypatch):
    ctx, xlsx = env
    page = f3._page(app, (dict(ctx, channel_names=["Hoechst", "CD8"]), xlsx), monkeypatch, find=False)
    assert not page.cores.btn_find.isEnabled()
    page.cores.find_cores()                                                 # the action says why
    assert "needs a channel named DAPI" in page.cores.lbl_info.text()
    page.shutdown()
    page = f3._page(app, (dict(ctx, full_wsi=False, workspace_id="roi_1"), xlsx), monkeypatch, find=False)
    assert not page.cores.btn_find.isEnabled()
    page.shutdown()


def _snapshot(d):
    return {k: copy.deepcopy(d[k]) for k in ("pieces", "exclusions", "piece_counter", "candidates_version",
                                             "core_diameter_px")}


def test_the_map_sheet_chosen_afterwards_keeps_the_cores(app, env, monkeypatch, no_dialog):
    page = _without_map(app, env, monkeypatch)
    d = page.holder.d
    # edits before the map sheet: a drawn piece, a deleted piece, a merge, an exclusion
    page._set_tool("circle")
    cy, cx = f3._centre(1, 1)
    f3._drag(page, (cy - 10, cx + 90), (cy + 10, cx + 110))
    drawn = page.selected
    gone = next(p for p in d["pieces"] if p != drawn and d["pieces"][p]["source"] == "detected")
    page.select(gone)
    page._delete_selected()
    assert gone not in d["pieces"]
    page._set_tool("select")
    page.ctl.click(cy, cx)
    with monkeypatch.context() as m:                                # Shift+click adds (B25)
        m.setattr(type(page), "shift_held", staticmethod(lambda: True))
        page.ctl.click(cy, cx + 100)
    f3._merge(page)
    merged = d["pieces"][drawn]["owner"]
    page.edit(D.add_exclusion, [[10, 10], [60, 10], [60, 60], [10, 60]], ["blur"])
    # a second Find cores, refused: the cores stay
    from block01.ui.theme import dialogs as theme_dialogs
    monkeypatch.setattr(theme_dialogs, "confirm_danger", lambda *a, **k: False)
    _find(app, page)
    assert drawn in page.holder.d["pieces"] and gone not in page.holder.d["pieces"]
    before = _snapshot(page.holder.d)
    owners = {u: list(c["pieces"]) for u, c in page.holder.d["cores"].items()}
    page.cores.btn_map.click()                                              # Choose map sheet…
    d = page.holder.d
    assert page.cores.study is not None and d["map_sha256"] == page.cores.study["map_sheet"]["sha256"]
    assert _snapshot(d) == before                                           # ids, outlines, owners, counters
    assert {u: c["pieces"] for u, c in d["cores"].items()} == owners
    assert all(c["position_key"] is None and c["mapping"] == "unmatched" for c in d["cores"].values())
    assert len(d["positions"]) == f1_positions() and d["operations"][-1]["kind"] == "attach_map"
    assert merged in d["cores"]
    # numbering works now
    assert page.cores.numbering_allowed() and page.cores.numbering.isEnabled()
    page.select(next(p for p in d["pieces"] if d["pieces"][p]["owner"] and p != drawn
                     and abs(sum(q[1] for q in d["pieces"][p]["polygon"]) / len(d["pieces"][p]["polygon"])
                             - f3._centre(1, 2)[0]) < 40))
    page.cores.bind_single("X1-Y02")
    assert any(c["position_key"] for c in d["cores"].values())
    page.btn_save.click()
    rec = K.load_saved(page.ctx["project_dir"])
    assert rec["document"]["map_sha256"] and K.table_matches(page.ctx["project_dir"], rec)
    # reopen, draw again: the new piece's id follows piece_counter
    counter = page.holder.d["piece_counter"]
    ctx = page.ctx
    page.shutdown()
    page = f3._page(app, (ctx, env[1]), monkeypatch, find=False)
    page._set_tool("circle")
    cy, cx = f3._centre(2, 2)
    f3._drag(page, (cy - 10, cx + 90), (cy + 10, cx + 110))
    assert int(page.selected[1:]) == counter + 1
    page.shutdown()


def f1_positions():
    return f3.ROWS * f3.COLS


def test_numbering_follows_the_documents_map_sheet(app, env, monkeypatch, no_dialog):
    page = _without_map(app, env, monkeypatch)
    page.btn_save.click()                                                   # a Save before the attach
    page.cores.btn_map.click()
    assert page.holder.d["map_sha256"] and page.cores.numbering_allowed()
    page.undo()                                                             # the attach undone
    d = page.holder.d
    assert not d["map_sha256"] and page.cores.study is not None
    assert not page.cores.numbering_allowed() and not page.cores.numbering.isEnabled()
    assert "not attached to these cores" in page.annotate_blocker()
    assert page.cores.btn_map.text() == "Attach map sheet" and page.cores.btn_map.isEnabled()
    page.select(next(iter(d["pieces"])))
    page.cores.bind_single("X1-Y01")                                        # refused, no error
    assert all(c["position_key"] is None for c in d["cores"].values())
    assert "Attach map sheet" in page.cores.lbl_info.text()
    page.cores.confirm_numbering()                                          # refused too
    assert all(c["position_key"] is None for c in d["cores"].values())
    page.redo()
    page._set_tool("line")                                                  # a numbering tool left active ...
    page.undo()                                                             # ... across the Undo of the attach
    assert not page.holder.d["map_sha256"] and page.ctl.tool == "line"
    f3._drag(page, f3._centre(1, 1), f3._centre(1, 3))                      # takes no numbering gesture
    assert page.ctl.line is None and not page.ctl.has_preview()
    page.ctl.click(*f3._centre(1, 1))
    assert page.ctl.sequence == [] and not page.cores.select_preview_pending()
    page._set_tool("select")
    page.redo()
    assert page.holder.d["map_sha256"] and page.cores.numbering_allowed()
    page.undo()
    page.cores.btn_map.click()                                              # Attach map sheet
    assert page.holder.d["map_sha256"] and page.holder.d["operations"][-1]["kind"] == "attach_map"
    page.undo()
    assert not page.holder.d["map_sha256"]
    page.redo()
    assert page.holder.d["map_sha256"]
    # Revert to the Save made before the attach: the same state as after the Undo
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    page._revert_to_saved()
    assert not page.holder.d["map_sha256"] and len(page.holder.d["cores"]) == N_CORES
    assert not page.cores.numbering_allowed() and page.cores.btn_map.text() == "Attach map sheet"
    page.cores.btn_map.click()
    assert page.holder.d["map_sha256"] and page.cores.numbering_allowed()
    page.shutdown()


def test_with_the_map_sheet_first_the_document_is_as_before(app, env, monkeypatch, no_dialog):
    """`attach_tma` now builds its pieces with `attach_candidates`' code:
    the document is the old function's, field by field."""
    page = f3._page(app, env, monkeypatch, find=True)
    d = page.holder.d
    study, rec = page.cores.study, page.cores.candidates
    old = D.new_document(d["slide_id"], tuple(d["slide_shape"]))
    _old_attach_tma(old, study, rec)
    for k in ("map_sha256", "candidates_version", "core_diameter_px", "positions", "piece_counter"):
        assert d[k] == old[k], k
    strip = lambda ps: {pid: dict(p, owner=bool(p["owner"])) for pid, p in ps.items()}   # noqa: E731 (owner = a random core id)
    assert strip(d["pieces"]) == strip(old["pieces"])
    assert sorted(sorted(c["pieces"]) for c in d["cores"].values()) == \
        sorted(sorted(c["pieces"]) for c in old["cores"].values())
    assert all(c["mapping"] == "unmatched" and c["position_key"] is None for c in d["cores"].values())
    assert [o["kind"] for o in d["operations"]][-1] == "attach_tma"
    page.shutdown()


def _old_attach_tma(d, study, candidates):
    """`attach_tma` as it was before this block (43e18b7), for comparison."""
    import math
    from block01.core.regions import geometry as G
    from block01.core.tma.cores import position_key
    sheet = study["map_sheet"]
    shape = tuple(d["slide_shape"])
    dia = float(candidates["core_diameter_px"])
    core_area = math.pi * (dia / 2) ** 2
    d["piece_counter"] = D.piece_counter(d)
    d.update(map_sha256=sheet["sha256"], candidates_version=int(candidates["version"]), core_diameter_px=dia,
             pieces={}, cores={}, positions={})
    for p in sheet["positions"]:
        key = position_key(study["slide_id"], sheet["sha256"], p["position_id"])
        d["positions"][key] = {"observed": None, "missing_reason": "", "qc": "unreviewed", "qc_reasons": [],
                               "run": False}
    for c in candidates["candidates"]:
        pid = f"p{int(c['candidate_id'])}"
        poly = [list(q) for q in c.get("outline") or []]
        if len(poly) < 3:
            poly = G.circle_polygon(c["cy"], c["cx"], c["radius"], shape)
        d["pieces"][pid] = {"polygon": poly, "source": "detected", "candidate_id": int(c["candidate_id"]),
                            "tissue": c.get("tissue_fraction"), "owner": None, "dropped": None}
        if c["area"] >= D.CORE_PIECE_FRAC * core_area:
            D.new_core(d, [pid])
    d["piece_counter"] = max(D.piece_counter(d), *[int(c["candidate_id"]) for c in candidates["candidates"]] or [0])


def test_a_save_without_a_map_sheet_analyses_its_cores(app, env, monkeypatch, no_dialog):
    """Stage 1 analysed such a Save as one region; stages 2-3
    (docs/v17_cores_without_map_downstream_plan.md) analyse its cores."""
    from block01.core.regions import analysis as A
    page = _without_map(app, env, monkeypatch)
    page.edit(D.add_exclusion, [[10, 10], [60, 10], [60, 60], [10, 60]], ["blur"])
    page.btn_save.click()
    pd, sid = page.ctx["project_dir"], page.ctx["slide_id"]
    rec = K.load_saved(pd, sid)
    assert len(rec["document"]["cores"]) == N_CORES and not rec["document"]["map_sha256"]
    snap = A.build_snapshot(pd, sid, seg_channels=["DAPI"])
    assert snap["mode"] == "cores" and len(snap["cores"]) == N_CORES      # every core ticked by default
    assert len(snap["display_cores"]) == N_CORES and all(c["run"] for c in snap["display_cores"])
    # the Cores tab: each core has its own Run tick; unticking one leaves it out
    uid = next(iter(page.holder.d["cores"]))
    page.cores.run_toggled(uid, False)
    page.btn_save.click()
    snap = A.build_snapshot(pd, sid, seg_channels=["DAPI"])
    assert uid not in {c["uid"] for c in snap["cores"]} and len(snap["cores"]) == N_CORES - 1
    page.undo()
    assert D.core_runs(page.holder.d, uid)
    assert page.exclusions._links(page.holder.d) == {}                      # not linked to cores
    # the map sheet chosen after that Save, not saved again: refused with the new text
    page.cores.btn_map.click()
    with pytest.raises(A.AnalysisError, match="not attached to the cores in the last Regions Save"):
        A.build_snapshot(pd, sid, seg_channels=["DAPI"])
    page.shutdown()


def _rows(page):
    return [(r["kind"], r["id"]) for r in page.cores.table._rows]


def test_a_core_has_one_row_before_and_after_numbering(app, env, monkeypatch, no_dialog):
    """User ruling 2026-11-18: one row per core, whatever the numbering. A
    map position no core holds is not listed while a found core still has
    no number (it would be that core a second time); once all are
    numbered, the positions left are listed (to mark them missing)."""
    page = _without_map(app, env, monkeypatch)
    page.cores.btn_map.click()                                              # map sheet, nothing numbered
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    d = page.holder.d
    assert _rows(page) == [("core", u) for u in d["cores"]]
    assert "5 map position(s) waiting for a core" in page.cores.table.counts.text()
    labels = [r["label"] for r in page.cores.records() if r["donor"]]
    uids = list(d["cores"])
    for uid, label in zip(uids[:2], labels[:2]):
        page.select(d["cores"][uid]["pieces"][0])
        page.cores.bind_single(label)
    rows = _rows(page)
    assert len(rows) == N_CORES                                             # 2 numbered + 3 not yet
    assert sorted(i for k, i in rows if k == "core") == sorted(uids[2:])
    for uid, label in zip(uids[2:], labels[2:]):
        page.select(d["cores"][uid]["pieces"][0])
        page.cores.bind_single(label)
    rows = _rows(page)
    assert all(k == "position" for k, _ in rows)
    assert len(rows) == f1_positions()                                      # the empty position is listed again
    assert "waiting for a core" not in page.cores.table.counts.text()
    page.shutdown()


def test_a_position_with_no_core_is_listed_when_it_can_be_marked_missing(app, env, monkeypatch, no_dialog):
    """A missing position is always listed; a position with a sample but no
    core found comes back as a row once every found core is numbered."""
    page = _without_map(app, env, monkeypatch)
    page.cores.btn_map.click()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    d = page.holder.d
    recs = [r for r in page.cores.records() if r["donor"]]
    for gone in list(d["cores"])[:2]:                                       # two cores never found
        page.edit(D.delete_pieces, list(d["cores"][gone]["pieces"]))
    uids = list(d["cores"])
    missing, unfound = recs[-1]["position_key"], recs[-2]["position_key"]
    page.edit(D.set_missing, missing, True, "")
    rows = _rows(page)
    assert ("position", missing) in rows                                    # listed while numbering is open
    assert ("position", unfound) not in rows                                # waiting: counted only
    assert sum(k == "core" for k, _ in rows) == len(uids)
    for uid, r in zip(uids, recs):
        page.select(d["cores"][uid]["pieces"][0])
        page.cores.bind_single(r["label"])
    rows = _rows(page)
    assert all(k == "position" for k, _ in rows) and len(rows) == f1_positions()
    assert ("position", unfound) in rows and ("position", missing) in rows
    page.shutdown()
