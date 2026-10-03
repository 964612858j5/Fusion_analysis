"""Block DV-D: deleting from Step0's chooser into a 30-day trash
(docs/v16_DV_delete_application.md).

Part 1 -- the records and the trash (`utils/data_versions.py`,
`utils/trash.py`): what a deletion moves, what it leaves byte-for-byte alone,
the records it drops, recovery from a deletion interrupted half-way, the
30-day purge and Empty trash, the A3 `deletion` entry.

Synthetic projects in the test's temporary directory only.
"""

import json
import os
import shutil
from datetime import datetime, timedelta

import pytest

from block01.utils import data_versions as dv
from block01.utils import trash


def _tree(path):
    out = []
    for base, _dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(base, name)
            with open(p, "rb") as f:
                out.append((os.path.relpath(p, path), f.read()))
    return sorted(out)


def _project(tmp_path):
    from block01.utils.roi_project import ensure_project_manifest
    proj = tmp_path / "proj"
    ws = proj / "rois" / "ws1"
    (ws / "step0").mkdir(parents=True)
    (ws / "roi_manifest.json").write_text(json.dumps({"roi_id": "ws1"}))
    ensure_project_manifest(str(proj))
    with open(proj / "roi_index.json", "w") as f:
        json.dump({"version": 1, "active_roi_id": "ws1",
                   "rois": [{"roi_id": "ws1"}, {"roi_id": "ws0"}]}, f)
    return str(proj), str(ws)


def _version(ws, corrected=None, n_regions=1):
    alloc = dv.new_version_folder(ws)
    if corrected is None:
        corrected = dv.new_corrected_folder(ws)
        os.makedirs(corrected)
        with open(os.path.join(corrected, "px.bin"), "wb") as f:
            f.write(os.urandom(64))
    regions = []
    for i in range(n_regions):
        z = os.path.join(alloc["path"], f"fused_R{i}.zarr")
        os.makedirs(z)
        regions.append({"roi_name": f"R{i}", "roi_id": f"r{i}", "bbox_fullres": [0, 5, 0, 5],
                        "polygon_fullres": None, "fused_zarr_path": z})
    return dv.commit_version(ws, alloc, {"corrected": {"path": corrected}, "regions": regions})


def _run(ws, version, name):
    run = os.path.join(ws, "step2", "segmentation_runs", name)
    os.makedirs(run)
    with open(os.path.join(run, "segmentation_meta.json"), "w") as f:
        json.dump({"run_id": name, "method": "stardist", "data_version": version,
                   "created_at": name}, f)
    quant = os.path.join(ws, "step4", "quantification_runs", name, "Full WSI")
    os.makedirs(quant)
    with open(os.path.join(quant, "cells.h5ad"), "wb") as f:
        f.write(b"x")
    index_path = os.path.join(ws, "roi_index.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    index.setdefault("segmentation_runs", {})[name] = {
        "run_id": name, "status": "done", "path": f"step2/segmentation_runs/{name}"}
    index["active_segmentation_run"] = name
    index.setdefault("latest_by_method", {})["stardist"] = name
    json.dump(index, open(index_path, "w"))
    return run


def _handoff(ws, corrected):
    with open(os.path.join(ws, "step0", "step0_roi_result.json"), "w") as f:
        json.dump({"corrected_zarr_path": corrected}, f)


# ── runs of a version ────────────────────────────────────────────────────

def test_the_runs_of_a_version_are_found(tmp_path):
    _proj, ws = _project(tmp_path)
    _version(ws)
    _version(ws)
    a = _run(ws, "v001", "seg_20261003_100000_a")
    b = _run(ws, "v001", "seg_20261003_110000_b")
    _run(ws, "v002", "seg_20261003_120000_c")
    assert [r.run_dir for r in dv.runs_of_version(ws, "v001")] == [
        os.path.realpath(b), os.path.realpath(a)]


# ── one segmentation run (the combination) ───────────────────────────────

def test_deleting_one_run_moves_only_it_and_its_step4_results(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    _handoff(ws, v1["corrected"]["path"])
    a = _run(ws, "v001", "seg_a")
    b = _run(ws, "v001", "seg_b")
    keep = _tree(dv.versions_dir(ws)), _tree(b)
    plan = dv.plan_delete(ws, run_dir=os.path.realpath(a))
    entry = dv.execute_delete(plan)
    assert not os.path.exists(a)
    assert not os.path.exists(os.path.join(ws, "step4", "quantification_runs", "seg_a"))
    assert os.path.isdir(os.path.join(entry["folder"], "rois", "ws1", "step2",
                                      "segmentation_runs", "seg_a"))
    assert (_tree(dv.versions_dir(ws)), _tree(b)) == keep     # version + other run untouched
    index = json.load(open(os.path.join(ws, "roi_index.json")))
    assert "seg_a" not in index["segmentation_runs"] and "seg_b" in index["segmentation_runs"]
    assert dv.get_version(ws, "v001") is not None
    assert entry["status"] == "complete"


def test_deleting_the_last_run_with_its_version(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    a = _run(ws, "v001", "seg_a")
    plan = dv.plan_delete(ws, run_dir=os.path.realpath(a), version_id="v001")
    dv.execute_delete(plan)
    assert dv.get_version(ws, "v001") is None and dv.current_version(ws)["version"] == "v002"
    assert not os.path.exists(dv.version_dir(ws, v1["folder"]))
    assert not os.path.exists(v1["corrected"]["path"])       # only v1 used it
    assert os.path.isdir(v2["corrected"]["path"])


def test_a_corrected_product_another_version_uses_stays(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    v2 = _version(ws, corrected=v1["corrected"]["path"])      # shared (§3.4)
    _handoff(ws, v2["corrected"]["path"])
    before = _tree(os.path.dirname(v1["corrected"]["path"]))
    dv.execute_delete(dv.plan_delete(ws, version_id="v001"))
    assert _tree(os.path.dirname(v1["corrected"]["path"])) == before


def test_the_current_versions_corrected_stays_unless_the_handoff_is_released(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    _handoff(ws, v1["corrected"]["path"])
    keep = dv.plan_delete(ws, version_id="v001")
    assert dv._corrected_folder(v1["corrected"]["path"]) not in keep["paths"]
    released = dv.plan_delete(ws, version_id="v001", release_handoff=True)
    assert dv._corrected_folder(v1["corrected"]["path"]) in released["paths"]
    dv.execute_delete(released)
    assert dv.list_versions(ws) == [] and dv.load_index(ws)["current"] == ""


def test_a_legacy_v1s_products_in_place_go_with_it(tmp_path):
    proj, ws = _project(tmp_path)
    corrected = os.path.join(ws, "step0", "corrected_channels.zarr")
    fused = os.path.join(ws, "step1", "fused_Full WSI.zarr")
    os.makedirs(corrected)
    os.makedirs(fused)
    alloc = dv.new_version_folder(ws)
    dv.commit_version(ws, alloc, {"corrected": {"path": corrected}, "legacy": True,
                                  "regions": [{"roi_name": "Full WSI", "fused_zarr_path": fused}]})
    _handoff(ws, os.path.join(ws, "versions", "corrected", "c001", "corrected_channels.zarr"))
    dv.execute_delete(dv.plan_delete(ws, version_id="v001"))
    assert not os.path.exists(corrected) and not os.path.exists(fused)


# ── the draft and the workspace ──────────────────────────────────────────

def test_discarding_the_draft_moves_only_its_own_corrected(tmp_path):
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    draft = dv.new_corrected_folder(ws)
    os.makedirs(draft)
    _handoff(ws, draft)
    plan = dv.plan_delete(ws, draft=True)
    assert plan["paths"] == [os.path.dirname(draft)]
    dv.execute_delete(plan)
    assert not os.path.exists(draft) and os.path.isdir(v1["corrected"]["path"])
    # a handoff naming a version's product is no draft product
    _handoff(ws, v1["corrected"]["path"])
    assert dv.plan_delete(ws, draft=True)["paths"] == []


def test_deleting_the_workspace_moves_it_whole_and_drops_it_from_the_project(tmp_path):
    proj, ws = _project(tmp_path)
    _version(ws)
    dv.execute_delete(dv.plan_delete(ws, workspace=True))
    assert not os.path.exists(ws)
    index = json.load(open(os.path.join(proj, "roi_index.json")))
    assert [r["roi_id"] for r in index["rois"]] == ["ws0"] and index["active_roi_id"] == ""


# ── interrupted, purge, empty, A3 ────────────────────────────────────────

def test_an_interrupted_deletion_is_finished_records_first(tmp_path, monkeypatch):
    proj, ws = _project(tmp_path)
    _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    plan = dv.plan_delete(ws, version_id="v001")
    entry = trash.begin(proj, "ws1", plan["what"], plan["paths"],
                        {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")})
    # ... the program stopped here: nothing dropped, nothing moved
    assert dv.get_version(ws, "v001") is not None
    done = dv.resume_deletions(proj)
    assert done == [entry["folder"]]
    assert dv.get_version(ws, "v001") is None
    assert all(not os.path.exists(p) for p in plan["paths"])
    assert trash.entries(proj)[0]["status"] == "complete"


def test_complete_entries_older_than_30_days_are_purged(tmp_path):
    proj, ws = _project(tmp_path)
    for name in ("old", "recent"):
        os.makedirs(os.path.join(ws, name))
    now = datetime(2026, 11, 10, 12, 0, 0)
    old = trash.finish(trash.begin(proj, "ws1", "old", [os.path.join(ws, "old")],
                                   now=now - timedelta(days=31)))
    recent = trash.finish(trash.begin(proj, "ws1", "recent", [os.path.join(ws, "recent")],
                                      now=now - timedelta(days=29)))
    assert trash.purge(proj, now=now) == [old["folder"]]
    assert not os.path.exists(old["folder"]) and os.path.isdir(recent["folder"])


def test_empty_trash_removes_every_complete_entry(tmp_path):
    proj, ws = _project(tmp_path)
    os.makedirs(os.path.join(ws, "a"))
    os.makedirs(os.path.join(ws, "b"))
    trash.finish(trash.begin(proj, "ws1", "a", [os.path.join(ws, "a")]))
    pending = trash.begin(proj, "ws1", "b", [os.path.join(ws, "b")])
    assert trash.size(proj) > 0
    trash.empty(proj)
    assert [e["folder"] for e in trash.entries(proj)] == [pending["folder"]]   # finished first


def test_the_trash_is_never_listed_as_a_workspace(tmp_path, monkeypatch):
    from block01.utils import workspace_session as wsess
    proj, ws = _project(tmp_path)
    _version(ws)
    dv.execute_delete(dv.plan_delete(ws, workspace=True))
    assert not os.path.isdir(os.path.join(proj, "rois", "ws1"))
    assert os.path.isdir(os.path.join(trash.trash_root(proj)))
    assert all(not trash.is_in_trash(proj, p) for p in
               [os.path.join(proj, "rois")])


def test_a_deletion_appends_an_a3_record(tmp_path):
    from block01.core import provenance as prov
    from block01.utils.roi_project import ensure_project_manifest
    proj, ws = _project(tmp_path)
    ensure_project_manifest(proj)
    _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    a = _run(ws, "v001", "seg_a")
    before = len(prov.load_entries(proj))
    dv.execute_delete(dv.plan_delete(ws, run_dir=os.path.realpath(a)))
    entries = prov.load_entries(proj)
    assert len(entries) == before + 1
    rec = [e for e in entries if e["kind"] == "deletion"][-1]
    assert rec["parameters"]["data_version"] == ""          # a run only: no version deleted
    assert rec["parameters"]["run_dir"].endswith("seg_a")


# ── part 2: the chooser's × (Step0) ──────────────────────────────────────

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets  # noqa: E402
from test_v16_a6_workspace import (  # noqa: E402,F401  (fixtures)
    app, slides, page, _project as _a6_project)
from test_v16_data_versions import _two_versions, _workspace_as  # noqa: E402


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    def _refuse(dlg, *a, **k):
        raise AssertionError(f"unexpected modal dialog: {dlg.windowTitle()!r}")
    monkeypatch.setattr(QtWidgets.QDialog, "exec_", _refuse)


def _answers(monkeypatch, confirm=True, also=False, load_other=None, whole=False):
    from block01.ui.step0.step0_page import Step0Page
    asked = []
    monkeypatch.setattr(Step0Page, "_dv_confirm",
                        lambda self, parent, title, text, checkbox=None: (
                            asked.append((title, text, checkbox)) or (confirm, also)))
    monkeypatch.setattr(Step0Page, "_dv_ask_load_other",
                        lambda self, parent, others: (
                            asked.append(("load other", [r["version"] for r in others]))
                            or load_other(others) if callable(load_other) else load_other))
    monkeypatch.setattr(Step0Page, "_dv_ask_keep_workspace",
                        lambda self, parent, ws: asked.append(("whole", ws.workspace_id)) or whole)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: asked.append(("info", a[2] if len(a) > 2 else "")))
    return asked


def _seg(ctx, version, name):
    return _run(ctx["roi_dir"], version, name)


def _rows(page, proj, slide):
    from block01.utils import workspace_session as wsess
    return page._workspace_rows(wsess.find_workspaces(proj, slide)[1])


def _row(rows, version, run_name=None):
    for r in rows:
        if r[1] is not None and r[1]["version"] == version:
            run = r[3] if len(r) > 3 else None
            if (run_name is None and run is None) or \
                    (run is not None and os.path.basename(run.run_dir) == run_name):
                return r
    raise AssertionError(f"no row {version} {run_name}")


@pytest.fixture
def two(page, tmp_path, slides):
    """v001 (2 runs) and v002 (current, 1 run); the workspace is v002."""
    proj, made = _a6_project(tmp_path, slides["a"])
    ctx = made[0]
    v1, v2 = _two_versions(ctx)
    _workspace_as(ctx, v2)
    _seg(ctx, "v001", "seg_20261003_100000_a")
    _seg(ctx, "v001", "seg_20261003_110000_b")
    _seg(ctx, "v002", "seg_20261003_120000_c")
    return proj, ctx, v1, v2


def test_the_delegate_draws_a_close_box_only_where_asked(app):
    from block01.ui.step3_mask_bar import (CLOSE_ROLE, TaggedItemDelegate, Step3MaskBar)
    lst = QtWidgets.QListWidget()
    delegate = TaggedItemDelegate(lst)
    lst.setItemDelegate(delegate)
    lst.addItem("row")
    lst.item(0).setData(CLOSE_ROLE, True)
    lst.resize(300, 100)
    lst.show()
    clicked = []
    delegate.close_clicked.connect(clicked.append)
    rect = lst.visualItemRect(lst.item(0))
    from PyQt5.QtTest import QTest
    QTest.mouseClick(lst.viewport(), QtCore.Qt.LeftButton, pos=QtCore.QPoint(
        rect.left() + 5, rect.center().y()))
    assert clicked == [0]
    QTest.mouseClick(lst.viewport(), QtCore.Qt.LeftButton, pos=QtCore.QPoint(
        rect.left() + 150, rect.center().y()))
    assert clicked == [0]                                   # the row itself: no ×
    bar = Step3MaskBar()
    bar.set_runs([("a", "/r/a", "v001")])
    assert not bar.run_combo.itemData(0, CLOSE_ROLE)        # Step3: never a ×
    lst.close()


def test_rows_are_one_per_version_and_segmentation(page, two, slides):
    proj, ctx, v1, v2 = two
    alloc = dv.new_version_folder(ctx["roi_dir"])                       # v003, no runs
    dv.commit_version(ctx["roi_dir"], alloc, dict(v2, version="v003", folder=alloc["folder"]),
                      make_current=False)
    rows = _rows(page, proj, slides["a"])
    got = [(r[2], os.path.basename(r[3].run_dir) if len(r) > 3 else None) for r in rows]
    assert got == [("v003", None),
                   ("v002  (current)", "seg_20261003_120000_c"),
                   ("v001", "seg_20261003_110000_b"), ("v001", "seg_20261003_100000_a")]
    assert "stardist" in page._row_text(rows[1]) and "(not segmented)" in page._row_text(rows[0])


def test_x_on_a_run_with_siblings_deletes_only_that_run(page, two, slides, monkeypatch):
    proj, ctx, v1, v2 = two
    asked = _answers(monkeypatch)
    row = _row(_rows(page, proj, slides["a"]), "v001", "seg_20261003_100000_a")
    assert page._dv_delete_row(row) == ("deleted",)
    assert asked[0][2] is None                                  # not the last: no question
    assert not os.path.exists(row[3].run_dir)
    assert dv.get_version(ctx["roi_dir"], "v001") is not None
    assert [os.path.basename(r.run_dir) for r in dv.runs_of_version(ctx["roi_dir"], "v001")] \
        == ["seg_20261003_110000_b"]


def test_the_last_run_asks_about_its_version_and_keeps_it_by_default(page, two, slides,
                                                                     monkeypatch):
    proj, ctx, v1, v2 = two
    rows = _rows(page, proj, slides["a"])
    _answers(monkeypatch)
    page._dv_delete_row(_row(rows, "v001", "seg_20261003_100000_a"))
    asked = _answers(monkeypatch, also=False)
    rows = _rows(page, proj, slides["a"])
    assert page._dv_delete_row(_row(rows, "v001", "seg_20261003_110000_b")) == ("deleted",)
    assert "Also delete data version v001" in asked[0][2]
    assert dv.get_version(ctx["roi_dir"], "v001") is not None          # kept
    assert ("v001", None) in [(r[2], None) for r in _rows(page, proj, slides["a"])
                              if len(r) == 3 and r[1] is not None]


def test_the_last_run_with_its_version_when_not_current(page, two, slides, monkeypatch):
    proj, ctx, v1, v2 = two
    _answers(monkeypatch)
    page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v001", "seg_20261003_100000_a"))
    _answers(monkeypatch, also=True)
    assert page._dv_delete_row(
        _row(_rows(page, proj, slides["a"]), "v001", "seg_20261003_110000_b")) == ("deleted",)
    assert dv.get_version(ctx["roi_dir"], "v001") is None
    assert not os.path.exists(v1["corrected"]["path"])
    assert dv.current_version(ctx["roi_dir"])["version"] == "v002"


def test_the_current_version_with_another_loaded_first(page, two, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    asked = _answers(monkeypatch, also=True, load_other=lambda others: others[-1])
    wrote = []
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path, remap_config_path=None: (
                            wrote.append(zarr_path) or (config, [], [], {})))

    def _choose(self, rows):
        out = self._dv_delete_row(_row(rows, "v002", "seg_20261003_120000_c"))
        assert out[0] == "load" and out[1][1]["version"] == "v001"
        assert dv.get_version(ctx["roi_dir"], "v002") is not None    # not before the load
        return out[1]
    monkeypatch.setattr(Step0Page, "_choose_workspace", _choose)
    page._open_existing_workspace()
    assert ("load other", ["v001"]) in asked
    assert wrote == [v1["corrected"]["path"]]                         # v001 loaded ...
    assert dv.get_version(ctx["roi_dir"], "v002") is not None         # ... not accepted yet
    page.handoff_accepted = lambda: True
    page._announce_opened_workspace()
    assert dv.get_version(ctx["roi_dir"], "v002") is None             # ... then v002 deleted
    assert dv.current_version(ctx["roi_dir"])["version"] == "v001"
    assert not os.path.exists(v2["corrected"]["path"])


def test_a_failed_load_deletes_nothing(page, two, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=lambda others: others[-1])
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: self._dv_delete_row(
                            _row(rows, "v002", "seg_20261003_120000_c"))[1])
    page._open_existing_workspace()
    assert dv.get_version(ctx["roi_dir"], "v002") is not None
    assert os.path.isdir(v2["corrected"]["path"])


def test_the_current_version_with_none_deletes_and_starts_over_empty(page, two, slides,
                                                                     monkeypatch):
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=None)
    out = page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v002",
                                   "seg_20261003_120000_c"))
    assert out == ("unload",)
    assert dv.get_version(ctx["roi_dir"], "v002") is None
    assert dv.load_index(ctx["roi_dir"])["current"] == ""
    assert not os.path.exists(v2["corrected"]["path"])               # handoff released
    sent = []
    page.unload_requested.connect(lambda: sent.append(1))
    assert page._dv_unload_if_requested() is True and sent == [1]
    assert page._dv_unload_if_requested() is False                   # once


def test_cancelling_the_load_other_question_deletes_nothing(page, two, slides, monkeypatch):
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=False)
    before = _tree(ctx["roi_dir"])
    assert page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v002",
                                    "seg_20261003_120000_c")) is None
    assert _tree(ctx["roi_dir"]) == before


def test_the_last_version_asks_about_the_workspace(page, tmp_path, slides, monkeypatch):
    proj, made = _a6_project(tmp_path, slides["a"], n=2)
    for ctx, whole in ((made[0], True), (made[1], False)):
        alloc = dv.new_version_folder(ctx["roi_dir"])
        dv.commit_version(ctx["roi_dir"], alloc, {"version": "v001", "regions": [],
                                                  "corrected": {"path": ""}})
        asked = _answers(monkeypatch, whole=whole, load_other=None)
        row = next(r for r in _rows(page, proj, slides["a"])
                   if r[0].workspace_id == ctx["roi_id"] and r[1] is not None)
        assert page._dv_delete_row(row) == ("unload",)
        assert ("whole", ctx["roi_id"]) in asked
        assert os.path.isdir(ctx["roi_dir"]) is (not whole)
    assert dv.list_versions(made[1]["roi_dir"]) == []
    assert [r[2] for r in _rows(page, proj, slides["a"])] == ["unknown"]


def test_x_on_a_workspace_without_versions_deletes_it(page, tmp_path, slides, monkeypatch):
    proj, made = _a6_project(tmp_path, slides["a"], n=2)
    _answers(monkeypatch)
    rows = _rows(page, proj, slides["a"])
    assert page._dv_delete_row(rows[0]) == ("deleted",)
    assert len(_rows(page, proj, slides["a"])) == 1


def test_x_on_the_draft_loads_the_current_version_then_discards(page, two, slides,
                                                               monkeypatch):
    from block01.ui.step0.step0_page import Step0Page, DRAFT_TAG
    proj, ctx, v1, v2 = two
    draft = dv.new_corrected_folder(ctx["roi_dir"])
    os.makedirs(draft)
    mpath = os.path.join(ctx["step_dirs"]["step0"], "step0_roi_result.json")
    m = json.load(open(mpath))
    m["corrected_zarr_path"] = draft
    json.dump(m, open(mpath, "w"))
    _answers(monkeypatch)
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path, remap_config_path=None: (config, [], [], {}))
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: self._dv_delete_row(
                            next(r for r in rows if r[2] == DRAFT_TAG))[1])
    page._open_existing_workspace()
    assert os.path.exists(draft)                         # the current version first
    page._announce_opened_workspace()
    assert not os.path.exists(draft)
    assert dv.get_version(ctx["roi_dir"], "v002") is not None


def test_nothing_is_deleted_while_something_runs(page, two, slides, monkeypatch):
    proj, ctx, v1, v2 = two
    asked = _answers(monkeypatch)
    page.deletion_blocker = lambda: "a Step2 segmentation is running"
    before = _tree(ctx["roi_dir"])
    assert page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v001",
                                    "seg_20261003_100000_a")) is None
    assert _tree(ctx["roi_dir"]) == before
    assert asked and asked[0][0] == "info" and "Step2" in asked[0][1]


def test_what_moves_is_released_first(page, two, slides, monkeypatch):
    proj, ctx, v1, v2 = two
    _answers(monkeypatch)
    released = []
    page.release_paths = lambda paths: released.append(
        [os.path.exists(p) for p in paths])
    page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v001", "seg_20261003_100000_a"))
    assert released and all(released[0])                   # still in place when released


def test_a_combination_row_opens_its_run_in_step3(page, two, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: _row(rows, "v002", "seg_20261003_120000_c"))
    page._open_existing_workspace()
    sent = []
    page.step0_complete.connect(sent.append)
    page._announce_opened_workspace()
    assert sent[0]["step3_run_dir"].endswith("seg_20261003_120000_c")


def test_empty_trash_asks_then_empties(page, two, slides, monkeypatch):
    proj, ctx, v1, v2 = two
    _answers(monkeypatch)
    page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v001", "seg_20261003_100000_a"))
    assert page._dv_trash_size() > 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.No)
    assert page._dv_empty_trash() is False and page._dv_trash_size() > 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.Yes)
    assert page._dv_empty_trash() is True and page._dv_trash_size() == 0


def test_opening_the_project_finishes_an_interrupted_deletion(page, two, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    plan = dv.plan_delete(ctx["roi_dir"], version_id="v001")
    trash.begin(proj, ctx["roi_id"], plan["what"], plan["paths"],
                {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")})
    monkeypatch.setattr(Step0Page, "_choose_workspace", lambda self, rows: (
        rows if not any(r[1] and r[1]["version"] == "v001" for r in rows) else
        pytest.fail("v001 is listed")) and None)
    page._open_existing_workspace()
    assert dv.get_version(ctx["roi_dir"], "v001") is None
    assert trash.entries(proj)[0]["status"] == "complete"


# ── part 3: the window ───────────────────────────────────────────────────

import test_step1_fusion_isolation as iso    # noqa: E402


def test_the_window_says_what_blocks_a_deletion(app, tmp_path):
    w = iso._window(app, tmp_path)
    try:
        assert not w._dv_deletion_blocker()

        class _Busy:
            def isRunning(self):
                return True
        w._step2._worker = _Busy()
        assert "Step2" in w._dv_deletion_blocker()
        w._step2._worker = None
        w._fusion_worker = _Busy()
        assert "Generate" in w._dv_deletion_blocker()
        w._fusion_worker = None
    finally:
        w.close()


def test_the_window_lets_go_of_a_run_before_it_moves(app, tmp_path, monkeypatch):
    from types import SimpleNamespace
    w = iso._window(app, tmp_path)
    try:
        cleared, step4 = [], []
        monkeypatch.setattr(w, "_step3_clear_masks", lambda: cleared.append(1))
        w._step3_mask_key = "/p/rois/ws1/step2/segmentation_runs/seg_a\x1fFull WSI"
        w._step3_mask_runs = []
        w._step4._job = SimpleNamespace(run_dir="/p/rois/ws1/step2/segmentation_runs/seg_a")
        monkeypatch.setattr(w._step4, "set_run", lambda run_dir, *a, **k: step4.append(run_dir))
        w._dv_release_paths(["/p/rois/ws1/step2/segmentation_runs/seg_b"])
        assert cleared == [] and step4 == []
        w._dv_release_paths(["/p/rois/ws1/step2/segmentation_runs/seg_a"])
        assert cleared == [1] and step4 == [""]
    finally:
        w._step4._job = None
        w.close()


def test_none_starts_a_new_empty_window(app, tmp_path, monkeypatch):
    from block01.ui.main_window import MainWindow
    w = iso._window(app, tmp_path)
    made = []
    real_init = MainWindow.__init__

    def _init(self, *a, **k):
        real_init(self, *a, **k)
        made.append(self)
    monkeypatch.setattr(MainWindow, "__init__", _init)
    try:
        w.show()
        fresh = w._dv_restart_empty()
        assert made == [fresh] and fresh.isVisible()
        assert fresh._step0.loader is None
        for _ in range(20):
            QtWidgets.QApplication.processEvents()
        assert not w.isVisible()                              # the old one closed
    finally:
        for win in made:
            win.close()
        MainWindow._live_windows.clear()
        w.close()


def test_the_opened_run_is_the_one_step3_shows(app, tmp_path, monkeypatch):
    w = iso._window(app, tmp_path)
    try:
        monkeypatch.setattr(w, "_load_step0_roi_result", lambda *a, **k: True)
        monkeypatch.setattr(w, "_dv_register_legacy", lambda: None)
        w._on_step0_complete(dict(w.step0_output, opened_workspace=True,
                                  step3_run_dir="/r/seg_c"))
        assert w._dv_step3_run == "/r/seg_c"
    finally:
        w.close()


# ── codex review 2 (astra low): findings 1-5, 7, 8 ─────────────────────────

def test_a_replacement_the_window_refuses_deletes_nothing(page, two, slides, monkeypatch):
    """#5: Step0 restored the other version, the window did not accept it."""
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=lambda others: others[-1])
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path, remap_config_path=None: (config, [], [], {}))
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: self._dv_delete_row(
                            _row(rows, "v002", "seg_20261003_120000_c"))[1])
    page.handoff_accepted = lambda: False
    page._open_existing_workspace()
    page._announce_opened_workspace()
    assert dv.get_version(ctx["roi_dir"], "v002") is not None
    assert os.path.isdir(v2["corrected"]["path"])


def test_a_draft_on_the_current_versions_product_keeps_it(page, two, slides, monkeypatch):
    """#4: an Intensity-only draft references the current version's
    corrected product; deleting the version with None keeps the product."""
    proj, ctx, v1, v2 = two
    with open(os.path.join(ctx["step_dirs"]["step0"], "step0_channel_remap.json"), "w") as f:
        json.dump({"version_marker": "intensity changed"}, f)          # the draft
    _answers(monkeypatch, also=True, load_other=None)
    assert page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v002",
                                    "seg_20261003_120000_c")) == ("unload",)
    assert dv.get_version(ctx["roi_dir"], "v002") is None
    assert os.path.isdir(v2["corrected"]["path"])
    # no current version: what the workspace holds is offered as the draft
    from block01.ui.step0.step0_page import DRAFT_TAG
    assert _rows(page, proj, slides["a"])[0][2] == DRAFT_TAG


def test_an_unfinished_deletion_is_never_cleaned_up_as_a_failed_generate(tmp_path):
    """#2: records dropped, the move failed: the version folder is the
    trash's and survives the incomplete-Generate cleanup."""
    proj, ws = _project(tmp_path)
    v1 = _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    plan = dv.plan_delete(ws, version_id="v001")
    trash.begin(proj, "ws1", plan["what"], plan["paths"],
                {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")})
    dv._drop_version_record(ws, "v001")                       # ... then the move failed
    assert dv.cleanup_incomplete(ws) == []
    assert os.path.isdir(dv.version_dir(ws, v1["folder"]))


def test_no_workspace_opens_while_an_unfinished_deletion_cannot_finish(page, two, slides,
                                                                      monkeypatch):
    """#2, #3: busy (or failing) -> the deletion waits and nothing opens."""
    proj, ctx, v1, v2 = two
    plan = dv.plan_delete(ctx["roi_dir"], version_id="v001")
    trash.begin(proj, ctx["roi_id"], plan["what"], plan["paths"],
                {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")})
    page.deletion_blocker = lambda: "a Step4 extraction is running"
    assert page._open_existing_workspace() is None
    assert dv.get_version(ctx["roi_dir"], "v001") is not None          # untouched
    page.deletion_blocker = lambda: None
    released = []
    page.release_paths = lambda paths: released.append(list(paths))
    monkeypatch.setattr(dv, "resume_deletions",
                        lambda project: (_ for _ in ()).throw(OSError("disk")))
    assert page._open_existing_workspace() is None
    assert released and set(released[0]) == set(plan["paths"])


def test_a_recovered_deletion_gets_its_a3_record(tmp_path):
    """#8."""
    from block01.core import provenance as prov
    proj, ws = _project(tmp_path)
    _version(ws)
    v2 = _version(ws)
    _handoff(ws, v2["corrected"]["path"])
    plan = dv.plan_delete(ws, version_id="v001")
    trash.begin(proj, "ws1", plan["what"], plan["paths"],
                {k: plan[k] for k in ("run_dir", "version", "draft", "delete_workspace")})
    dv.resume_deletions(proj)
    recs = [e for e in prov.load_entries(proj) if e["kind"] == "deletion"]
    assert len(recs) == 1 and recs[0]["parameters"]["data_version"] == "v001"
    dv.resume_deletions(proj)                                  # nothing twice
    assert len([e for e in prov.load_entries(proj) if e["kind"] == "deletion"]) == 1


def test_step4_results_in_another_folder_go_with_their_run(tmp_path):
    """#7: Step4 written to a custom Output dir inside the project, and the
    object tables; another run's results in the same folder stay."""
    from block01.core import provenance as prov
    proj, ws = _project(tmp_path)
    _version(ws)
    a = _run(ws, "v001", "seg_a")
    custom = os.path.join(ws, "step4", "customA")
    os.makedirs(custom)
    mine = {n: os.path.join(custom, n) for n in ("a.h5ad", "a.csv", "a_provenance.json")}
    theirs = os.path.join(custom, "b.h5ad")
    for p in list(mine.values()) + [theirs]:
        open(p, "w").write("x")
    with open(mine["a_provenance.json"], "w") as f:
        json.dump({"segmentation_run": {"run_id": "seg_a"}}, f)
    cells = os.path.join(proj, "objects", "seg_a", "cells.parquet")
    os.makedirs(os.path.dirname(cells))
    open(cells, "w").write("x")
    prov.register(proj, "step4_h5ad", prov.location(proj, mine["a.h5ad"]), "t1",
                  workspace_id="ws1",
                  parameters={"segmentation_run_id": "seg_a",
                              "files": {"h5ad": "a.h5ad", "csv": "a.csv",
                                        "provenance": "a_provenance.json"}})
    prov.register(proj, "step4_h5ad", prov.location(proj, theirs), "t2", workspace_id="ws1",
                  parameters={"segmentation_run_id": "seg_b", "files": {}})
    prov.register(proj, "cells_parquet", prov.location(proj, cells), "t3", workspace_id="ws1",
                  parameters={"segmentation_run_id": "seg_a"})
    dv.execute_delete(dv.plan_delete(ws, run_dir=os.path.realpath(a)))
    assert all(not os.path.exists(p) for p in mine.values())
    assert not os.path.exists(cells)
    assert os.path.exists(theirs)


def test_an_earlier_run_reads_the_legacy_versions_decisions(tmp_path):
    """#1: v1 (legacy) and v2 share the corrected product with other
    decisions; the earlier run is quantified with v1's."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from block01.core import quant_sources as qs
    proj, ws = _project(tmp_path)
    shared = os.path.join(ws, "step0", "corrected_channels.zarr")
    os.makedirs(shared)
    for legacy in (True, False):
        alloc = dv.new_version_folder(ws)
        dv.commit_version(ws, alloc, {"corrected": {"path": shared}, "legacy": legacy,
                                      "regions": []})
    assert qs._version_referencing(ws, shared)["version"] == "v001"


def test_one_row_still_shows_the_chooser_with_it_preselected(page, tmp_path, slides,
                                                            monkeypatch):
    """User ruling 4a: one row is not opened silently -- its × and Empty trash
    must be reachable; Enter opens the preselected row."""
    from block01.ui.step0.step0_page import Step0Page
    proj, made = _a6_project(tmp_path, slides["a"])
    shown = []

    def _choose(self, rows):
        shown.append(len(rows))
        return rows[self._default_row_index(rows)]
    monkeypatch.setattr(Step0Page, "_choose_workspace", _choose)
    opened = page._open_existing_workspace()
    assert shown == [1] and opened.workspace_id == made[0]["roi_id"]


# ── codex review 3 (astra low): findings 1-5 ───────────────────────────────

def test_results_since_replaced_by_another_run_stay(tmp_path):
    """#1: A3 still names run A's files, but run B's result has replaced them
    (same name): its own provenance file says B, so nothing moves."""
    from block01.core import provenance as prov
    proj, ws = _project(tmp_path)
    _version(ws)
    a = _run(ws, "v001", "seg_a")
    custom = os.path.join(ws, "step4", "shared")
    os.makedirs(custom)
    h5 = os.path.join(custom, "x.h5ad")
    open(h5, "w").write("B")
    with open(os.path.join(custom, "x_provenance.json"), "w") as f:
        json.dump({"segmentation_run": {"run_id": "seg_b"}}, f)
    prov.register(proj, "step4_h5ad", prov.location(proj, h5), "tA", workspace_id="ws1",
                  parameters={"segmentation_run_id": "seg_a",
                              "files": {"h5ad": "x.h5ad", "provenance": "x_provenance.json"}})
    dv.execute_delete(dv.plan_delete(ws, run_dir=os.path.realpath(a)))
    assert open(h5).read() == "B"


def test_the_replacement_needs_its_fused_products(page, two, slides, monkeypatch):
    """#2: the other version's fused product is gone -> nothing deleted."""
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=lambda others: others[-1])
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path, remap_config_path=None: (config, [], [], {}))
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: self._dv_delete_row(
                            _row(rows, "v002", "seg_20261003_120000_c"))[1])
    page.handoff_accepted = lambda: True
    rec = dv.get_version(ctx["roi_dir"], "v001")
    missing = os.path.join(ctx["roi_dir"], "gone.zarr")
    path = os.path.join(dv.version_dir(ctx["roi_dir"], rec["folder"]), "version.json")
    data = json.load(open(path))
    data["regions"][0]["fused_zarr_path"] = missing
    json.dump(data, open(path, "w"))
    page._open_existing_workspace()
    page._announce_opened_workspace()
    assert dv.get_version(ctx["roi_dir"], "v002") is not None


def test_an_earlier_run_never_borrows_a_later_versions_decisions(tmp_path):
    """#3: the legacy version was deleted; v2 shares the product -- nothing
    is inferred from it."""
    from block01.core import quant_sources as qs
    proj, ws = _project(tmp_path)
    shared = os.path.join(ws, "step0", "corrected_channels.zarr")
    os.makedirs(shared)
    alloc = dv.new_version_folder(ws)
    dv.commit_version(ws, alloc, {"corrected": {"path": shared}, "regions": []})
    assert qs._version_referencing(ws, shared) is None


def test_discarding_a_draft_without_a_current_version_loads_a_version(page, two, slides,
                                                                       monkeypatch):
    """#4: delete current v002 with None, then × on the resulting draft row:
    the workspace goes back to the version chosen (here v001)."""
    from block01.ui.step0.step0_page import Step0Page, DRAFT_TAG
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=None)
    page._dv_delete_row(_row(_rows(page, proj, slides["a"]), "v002", "seg_20261003_120000_c"))
    rows = _rows(page, proj, slides["a"])
    assert rows[0][2] == DRAFT_TAG
    asked = _answers(monkeypatch, load_other=None)
    assert page._dv_delete_row(rows[0]) is None                     # None keeps the draft
    _answers(monkeypatch, load_other=lambda others: others[0])
    out = page._dv_delete_row(rows[0])
    assert out[0] == "load" and out[1][1]["version"] == "v001"


def test_a_recovery_that_stops_half_way_still_records_what_it_finished(tmp_path,
                                                                       monkeypatch):
    """#5."""
    from block01.core import provenance as prov
    proj, ws = _project(tmp_path)
    for name in ("a", "b"):
        os.makedirs(os.path.join(ws, name))
    trash.begin(proj, "ws1", "a", [os.path.join(ws, "a")])
    trash.begin(proj, "ws1", "b", [os.path.join(ws, "b")])
    real = trash.finish
    calls = []

    def _finish(entry):
        calls.append(entry["what"])
        if entry["what"] == "b":
            raise OSError("disk")
        return real(entry)
    monkeypatch.setattr(trash, "finish", _finish)
    with pytest.raises(OSError):
        dv.resume_deletions(proj)
    recs = [e for e in prov.load_entries(proj) if e["kind"] == "deletion"]
    assert [r["parameters"]["what"] for r in recs] == ["a"]


# ── codex review 4 (astra low): findings 2, 3 ──────────────────────────────

def test_an_unreadable_replacement_fused_deletes_nothing(page, two, slides, monkeypatch):
    """#2: the folder is there, the Zarr does not open."""
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, v1, v2 = two
    _answers(monkeypatch, also=True, load_other=lambda others: others[-1])
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path, remap_config_path=None: (config, [], [], {}))
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: self._dv_delete_row(
                            _row(rows, "v002", "seg_20261003_120000_c"))[1])
    page.handoff_accepted = lambda: True
    fused = v1["regions"][0]["fused_zarr_path"]
    shutil.rmtree(fused)
    os.makedirs(fused)                                    # a folder, no Zarr in it
    page._open_existing_workspace()
    page._announce_opened_workspace()
    assert dv.get_version(ctx["roi_dir"], "v002") is not None


def test_an_earlier_run_without_its_version_is_refused_not_guessed(tmp_path):
    """#3: the legacy version is gone; today's config corrects nothing, but
    the product the run read was made with CD3 corrected -> refused."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_quant_sources import build_project, _edit_json
    from block01.core import quant_sources as qs
    p = build_project(tmp_path)
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d.setdefault("paths", {}).update(corrected_channels_zarr=p["zarr"]))
    _edit_json(p["cfg"], lambda d: d.update(channel_decisions={
        k: "original" for k in d.get("channel_decisions") or {}}))
    _edit_json(os.path.join(p["ws"], "step0", "step0_roi_result.json"),
               lambda d: d.pop("corrected_decisions", None))
    with pytest.raises(qs.QuantSourceError, match="cannot be resolved"):
        qs.resolve_quant_job(p["run_dir"], open_slide=p["slide"])


def test_an_earlier_run_whose_product_was_deleted_is_refused(tmp_path):
    """codex review 5: the product the run read was this workspace's and is
    gone -> refused, not quantified with today's (raw) config."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_quant_sources import build_project, _edit_json
    from block01.core import quant_sources as qs
    p = build_project(tmp_path)
    gone = os.path.join(p["ws"], "step0", "deleted_corrected.zarr")
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d.setdefault("paths", {}).update(corrected_channels_zarr=gone))
    _edit_json(p["cfg"], lambda d: d.update(channel_decisions={
        k: "original" for k in d.get("channel_decisions") or {}}))
    _edit_json(os.path.join(p["ws"], "step0", "step0_roi_result.json"),
               lambda d: d.pop("corrected_decisions", None))
    with pytest.raises(qs.QuantSourceError, match="cannot be resolved"):
        qs.resolve_quant_job(p["run_dir"], open_slide=p["slide"])
