"""The Regions exclusions (blur, fold, ...) over Step1, Step2 and Step3, hatched as
on the Regions page, with an "Exclusions" switch each (backlog 21,
docs/v17_acceptance_notes_plan.md B21). Synthetic documents and snapshots
only.
"""

import os

import pytest

from test_regions_r1 import SID, _finished, project  # noqa: F401

from block01.core.regions import analysis as A
from block01.core.regions import display as RD
from block01.core.regions import document as D
from block01.core.regions import lock as K

POLY = [[10, 10], [60, 10], [60, 60], [10, 60]]


def _saved_with_an_exclusion(project):
    s, d = _finished("identity")
    uid = D.add_exclusion(d, POLY, ["blur"])
    K.save(project, d, s)
    return s, d, uid


def test_a_snapshots_exclusions_are_hatched_as_in_regions(project):
    s, _d, uid = _saved_with_an_exclusion(project)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s, select=A.SELECT_ALL_ELIGIBLE)
    items = RD.exclusion_items(snap)
    assert [it["id"] for it in items] == [f"exclusion:{uid}"]
    it = items[0]
    assert it["polygon"] == POLY and it["color"] == RD.EXCLUSION
    assert it["hatch"] is True and it["hatch_style"] == "screen"       # B27's look
    assert it["width"] == RD.CORE_WIDTH and "label" not in it
    # the last Save gives the same items (Step2 before a run)
    assert RD.saved_exclusion_items(K.load_saved(project, SID)["document"]) == items
    assert RD.exclusion_items({}) == [] and RD.saved_exclusion_items({}) == []


def test_the_colour_is_the_regions_pages():
    from block01.ui.regions.exclusions_panel import EXCL_COLOR
    assert RD.EXCLUSION == EXCL_COLOR


# ── Step2 ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("block01.qt")
    from block01.qt import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


TRI = [[3040, 1080], [3080, 1080], [3080, 1120]]
TRI_ON_OVERVIEW = [[10.0, 20.0], [20.0, 20.0], [20.0, 30.0]]


def _page(monkeypatch, tmp_path):
    from block01.core import tile_tissue
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    page._zarr_path = str(tmp_path / "fused.zarr")
    page._ov_h, page._ov_w, page._ov_ds = 100, 100, 4
    monkeypatch.setattr(tile_tissue, "fused_region", lambda p: ((1000, 1400, 3000, 3400), "s", "DAPI"))
    page.saved_core_items = lambda: [{"id": "u#0", "polygon": TRI, "color": RD.GREEN}]
    return page


def test_step2_draws_the_exclusions_under_the_cores_with_their_switch(app, monkeypatch, tmp_path):
    page = _page(monkeypatch, tmp_path)
    page.saved_exclusion_items = lambda: RD.saved_exclusion_items({"exclusions": {"e1": {"polygon": TRI}}})
    page.refresh_core_outlines()
    ex, cores = page._exclusion_outlines.overlay, page._core_outlines.overlay
    assert [it["id"] for it in ex.items] == ["exclusion:e1"]
    assert ex.items[0]["polygon"] == TRI_ON_OVERVIEW and ex.items[0]["hatch_style"] == "screen"
    assert not page.btn_exclusions.isHidden() and page.btn_exclusions.isChecked()
    sibs = ex.parentWidget().children()
    assert sibs.index(cores) > sibs.index(ex)                          # the cores above
    # the running run's frozen snapshot wins over the last Save
    page._run_core_snapshot = {"display_cores": [], "exclusions": [{"uid": "f1", "polygon": TRI}]}
    page._run_active = True
    page.refresh_core_outlines()
    assert [it["id"] for it in ex.items] == ["exclusion:f1"]
    page._run_active = False                                           # ended: the last Save again
    page.refresh_core_outlines()
    assert [it["id"] for it in ex.items] == ["exclusion:e1"]
    page.btn_exclusions.setChecked(False)                              # the switch hides them
    assert ex.items == [] and cores.items                              # the cores stay
    page.btn_exclusions.setChecked(True)
    assert ex.items
    page.saved_exclusion_items = lambda: []
    page.refresh_core_outlines()
    assert page.btn_exclusions.isHidden()                              # nothing to draw: no switch
    assert not page.btn_tma_cores.isHidden()


def test_step2s_zoomable_viewer_draws_them_too(app, monkeypatch, tmp_path):
    from test_f5_core_display import _view
    from block01.ui.step2_viewer import Step2DapiViewer
    page = _page(monkeypatch, tmp_path)
    page.saved_exclusion_items = lambda: RD.saved_exclusion_items({"exclusions": {"e1": {"polygon": TRI}}})
    view = _view(app)

    class Shown(Step2DapiViewer):
        def shown(self):
            return True

        def view(self):
            return view
    viewer = page.dapi_viewer = Shown(None, page)
    page.refresh_core_outlines()
    assert viewer.exclusions_count == 1
    assert viewer.exclusions.overlay.items[0]["polygon"] == TRI      # slide pixels there
    sibs = view.graphics.viewport().children()
    assert sibs.index(viewer.cores.overlay) > sibs.index(viewer.exclusions.overlay)
    assert not page.btn_exclusions.isHidden()
    page.btn_exclusions.setChecked(False)
    assert viewer.exclusions.overlay.items == []
    viewer.close()
    assert viewer.exclusions.overlay is None and viewer.exclusions_count == 0


# ── the session ───────────────────────────────────────────────────────────

def test_the_switches_are_kept_in_the_session(app):
    from test_step1_overlay_preview import _window
    w = _window(app)
    try:
        payload = w._step1_session_payload()
        assert payload["exclusions_step2"] is True and payload["exclusions_step3"] is True
        assert payload["exclusions_step1"] is True
        w._btn_exclusions_step2.setChecked(False)
        assert w._step1_session_payload()["exclusions_step2"] is False
        w._btn_exclusions_step2.setChecked(True)
        w.step0_output = {"output_dir": "/tmp", "step1_dir": "/tmp"}
        payload = w._step1_session_payload()
        payload["exclusions_step2"] = False
        payload["exclusions_step3"] = False
        payload["exclusions_step1"] = False
        w._step1_session_timer.stop()
        w._step1_restore_active = True
        try:
            w._restore_step1_scientific_state(payload)
        finally:
            w._step1_restore_active = False
        assert not w._btn_exclusions_step2.isChecked()
        assert not w._btn_exclusions_step3.isChecked()                  # built with Step3's page
        assert not w._btn_exclusions_step1.isChecked()
        payload.pop("exclusions_step2")                                 # an older session: on
        w._step1_restore_active = True
        try:
            w._restore_step1_scientific_state(payload)
        finally:
            w._step1_restore_active = False
        assert w._btn_exclusions_step2.isChecked()
    finally:
        w.close()


# ── Step3 ─────────────────────────────────────────────────────────────────

def test_step3_draws_the_runs_frozen_exclusions(app, project, tmp_path, monkeypatch):
    import test_step3_viewer as sv
    s, _d, uid = _saved_with_an_exclusion(project)
    snap = A.build_snapshot(project, SID, ["DAPI"], study=s, select=A.SELECT_ALL_ELIGIBLE)
    run_dir = tmp_path / "run"
    A.save_snapshot(str(run_dir / "regions_snapshot.json"), snap)
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        sv._enter(rig, 3)
        w._step3_core_run_dir = str(run_dir)
        w._refresh_core_outlines("step3")
        ex = w._exclusion_outlines_step3.overlay
        assert ex is not None and [it["id"] for it in ex.items] == [f"exclusion:{uid}"]
        assert not w._btn_exclusions_step3.isHidden()
        cores = w._core_outlines("step3").overlay
        sibs = ex.parentWidget().children()
        assert sibs.index(cores) > sibs.index(ex)                      # the cores above
        w._btn_exclusions_step3.setChecked(False)                       # the switch
        assert ex.items == []
        w._step3_core_run_dir = None                                    # a run without a snapshot
        w._refresh_core_outlines("step3")
        assert w._btn_exclusions_step3.isHidden()
    finally:
        w.close()


# ── Step1 (the user, 2026-11-13: as in Step2 / Step3) ──────────────────────

def test_step1_draws_the_last_saves_exclusions_under_its_cores(app, tmp_path, monkeypatch):
    import test_step3_viewer as sv
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        excl = {"items": RD.saved_exclusion_items({"exclusions": {"e1": {"polygon": POLY}}})}
        w._saved_exclusion_items = lambda: excl["items"]                # the last Save's (synthetic)
        w._saved_core_items = lambda: [{"id": "u#0", "polygon": POLY, "color": RD.GREEN}]
        sv._enter(rig, 1)
        w._refresh_core_outlines("step1")
        ex = w._exclusion_outlines_step1.overlay
        assert ex is not None and [it["id"] for it in ex.items] == ["exclusion:e1"]
        assert ex.items[0]["hatch_style"] == "screen"
        btn = w._btn_exclusions_step1
        assert not btn.isHidden()
        assert btn.parentWidget() is w._btn_tma_cores_step1.parentWidget()   # beside "TMA cores"
        cores = w._core_outlines("step1").overlay
        sibs = ex.parentWidget().children()
        assert sibs.index(cores) > sibs.index(ex)                      # the cores above
        btn.setChecked(False)                                           # the switch
        assert ex.items == [] and cores.items
        excl["items"] = []                                              # a Save without any
        w._refresh_core_outlines("step1")
        assert btn.isHidden()                                           # nothing to draw: no switch
    finally:
        w.close()
