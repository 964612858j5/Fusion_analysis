"""One colour scheme for the TMA cores, Step 0.5 to Step 3 (backlog 28,
docs/v17_core_colours_plan.md): green analysed, yellow not, 3 px; Step 0.5's
two numbering reminders (orange review, grey dashed piece of no core); the
documented differences between Step 0.5 (the ticks) and Step 1 to 3 (what
the analysis of the workspace takes). Synthetic documents only.
"""

import copy

import pytest

from test_regions_r1 import SID, _finished, project  # noqa: F401

from block01.core.regions import analysis as A
from block01.core.regions import display as RD
from block01.core.regions import document as D
from block01.core.regions import lock as K


def _selected_by_the_analysis(d, s):
    if s is not None:
        return {c["uid"] for c in A.select_cores(d, s)[0]}
    return {c["uid"] for c in A.select_cores_without_map(d, A._parent(None, tuple(d["slide_shape"])))[0]}


def _ticked_with_every_reason():
    """A mapped document: every core ticked, then one each not ticked,
    QC-excluded, review, unbound, bound twice."""
    s, d = _finished("identity")
    keys = sorted(d["positions"])
    for key in keys:
        D.set_run(d, key, True)
    bound = {c["position_key"]: u for u, c in d["cores"].items() if c["position_key"]}
    k_unticked, k_qc, k_review, k_unbound, k_twice, k_other = [k for k in keys if k in bound][:6]
    D.set_run(d, k_unticked, False)
    d["positions"][k_qc]["qc"] = "excluded"
    d["cores"][bound[k_review]]["mapping"] = "review"
    D.unbind(d, bound[k_unbound])
    d["cores"][bound[k_other]]["position_key"] = k_twice              # a second core bound there
    return s, d, {"unticked": bound[k_unticked], "qc": bound[k_qc], "review": bound[k_review],
                  "unbound": bound[k_unbound], "twice": (bound[k_twice], bound[k_other])}


def test_the_rule_is_the_analysis_selection_with_a_map_sheet():
    s, d, which = _ticked_with_every_reason()
    got = RD.analysed_uids(d, s)
    assert got == _selected_by_the_analysis(d, s)
    assert got and not got & {which["unticked"], which["qc"], which["review"], which["unbound"],
                              *which["twice"]}


def test_the_rule_is_the_analysis_selection_without_a_map_sheet():
    _s, d0 = _finished("identity")
    d = copy.deepcopy(d0)
    d["map_sha256"] = None
    for c in d["cores"].values():
        c["position_key"] = None
    uids = sorted(d["cores"])
    d["cores"][uids[0]]["run"] = False
    d["cores"][uids[1]].pop("run", None)                               # an older document: on
    got = RD.analysed_uids(d, None)
    assert got == _selected_by_the_analysis(d, None)
    assert uids[0] not in got and uids[1] in got


def test_a_mapped_document_without_its_sheet_analyses_nothing():
    s, d = _finished("identity")
    for key in d["positions"]:
        D.set_run(d, key, True)
    assert RD.analysed_uids(d, None) == set()
    other = copy.deepcopy(s)
    other["map_sheet"]["sha256"] = "cd" * 32
    assert RD.analysed_uids(d, other) == set()


def test_step1_shows_the_same_colours_and_width_for_a_whole_slide_workspace(project):
    s, d, _which = _ticked_with_every_reason()
    d["cores"][_which["twice"][1]]["position_key"] = None              # no position bound twice
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    items = RD.snapshot_items(snap)
    green = {it["id"].split("#")[0] for it in items if it["color"] == RD.GREEN}
    assert green == RD.analysed_uids(d, s)
    assert all(it["width"] == RD.CORE_WIDTH == 3.0 for it in items)


def test_the_exceptions_a_degenerate_core_and_overlapping_cores(project):
    s, d = _finished("identity")
    keys = sorted(d["positions"])
    bound = {c["position_key"]: u for u, c in d["cores"].items() if c["position_key"]}
    a, b = [k for k in keys if k in bound][:2]
    D.set_run(d, a, True)
    D.set_run(d, b, True)
    flat = d["cores"][bound[b]]["pieces"][0]
    p0 = d["pieces"][flat]["polygon"][0]
    d["pieces"][flat]["polygon"] = [list(p0), [p0[0] + 5, p0[1]], [p0[0] + 10, p0[1]]]   # no area
    assert bound[b] in RD.analysed_uids(d, s)                          # Step 0.5: ticked, green
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s)
    assert bound[b] not in {c["uid"] for c in snap["cores"]}           # Step 1: left out, yellow
    # overlapping ticked cores: green in Step 0.5; the analysis refuses the snapshot
    s2, d2 = _finished("identity")
    bound2 = {c["position_key"]: u for u, c in d2["cores"].items() if c["position_key"]}
    a2, b2 = [k for k in sorted(d2["positions"]) if k in bound2][:2]
    D.set_run(d2, a2, True)
    D.set_run(d2, b2, True)
    d2["pieces"][d2["cores"][bound2[b2]]["pieces"][0]]["polygon"] = copy.deepcopy(
        d2["pieces"][d2["cores"][bound2[a2]]["pieces"][0]]["polygon"])
    assert {bound2[a2], bound2[b2]} <= RD.analysed_uids(d2, s2)
    K.save(project, d2, s2)
    with pytest.raises(A.AnalysisError, match="overlap"):
        A.build_snapshot(project, SID, ["DAPI"], study=s2)


def test_run_all_eligible_is_green_in_the_run_and_yellow_in_step_0_5(project):
    s, d = _finished("identity")
    assert RD.analysed_uids(d, s) == set()                             # no tick
    K.save(project, d, s)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s, select=A.SELECT_ALL_ELIGIBLE)
    green = {it["id"].split("#")[0] for it in RD.snapshot_items(snap) if it["color"] == RD.GREEN}
    assert green and green == {c["uid"] for c in snap["cores"]}


def test_the_core_width_is_the_regions_pages():
    from block01.ui.regions.overlay import BOUND_WIDTH
    assert RD.CORE_WIDTH == BOUND_WIDTH


# ── Step 0.5: the page draws the same rule ───────────────────────────────

def test_the_regions_page_draws_the_scheme(app, env, monkeypatch):
    from test_tma_f3_page import _line, _page, _set_target
    from block01.ui.regions.cores_panel import COLOR
    from block01.ui.regions.overlay import BOUND_WIDTH
    page = _page(app, env, monkeypatch)
    _set_target(page, "X1-Y01", "X1-Y03", "Along column")
    _line(page, (1, 1), (1, 3))
    page.cores.numbering.btn_confirm.click()
    d = page.holder.d
    bound = [(u, c["position_key"]) for u, c in d["cores"].items() if c["position_key"]]
    assert len(bound) >= 3
    (u_on, k_on), (u_off, k_off), (u_rev, k_rev) = bound[:3]
    page.holder.do(D.set_run, k_on, True)
    page.holder.do(D.set_run, k_off, False)
    page.holder.do(D.set_run, k_rev, True)
    d["cores"][u_rev]["mapping"] = "review"                            # a numbering to check
    items = {it["id"]: it for it in page.cores.draw_parts()[0]}
    first = lambda u: items[d["cores"][u]["pieces"][0]]               # noqa: E731
    assert first(u_on)["color"] == COLOR["analysed"] == RD.GREEN
    assert first(u_off)["color"] == COLOR["not_analysed"] == RD.YELLOW
    assert first(u_rev)["color"] == COLOR["review"]
    unbound = [u for u, c in d["cores"].items() if not c["position_key"]]
    assert unbound and all(first(u)["color"] == RD.YELLOW for u in unbound)   # not analysed
    owned = [it for pid, it in items.items() if d["pieces"][pid]["owner"]]
    assert all(it["width"] == BOUND_WIDTH for it in owned)
    for pid, p in d["pieces"].items():
        if p["dropped"]:
            assert items[pid]["color"] == RD.YELLOW and items[pid]["dash"]
        elif p["owner"] is None:
            assert items[pid]["color"] == COLOR["piece"] and items[pid]["dash"]
    page.shutdown()


def test_the_regions_page_without_a_map_sheet_follows_the_run_tick(app, env, monkeypatch):
    from test_tma_f3_page import _page
    page = _page(app, env, monkeypatch)
    d = page.holder.d
    page.cores.study = None
    d["map_sha256"] = None
    for c in d["cores"].values():
        c["position_key"] = None
    uids = sorted(d["cores"])
    d["cores"][uids[0]]["run"] = False
    d["cores"][uids[1]]["run"] = True
    items = {it["id"]: it for it in page.cores.draw_parts()[0]}
    assert items[d["cores"][uids[0]]["pieces"][0]]["color"] == RD.YELLOW
    assert items[d["cores"][uids[1]]["pieces"][0]]["color"] == RD.GREEN
    page.shutdown()


from test_tma_f3_page import app, env  # noqa: E402,F401
