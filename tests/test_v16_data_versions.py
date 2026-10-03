"""Block DV: data versions of one session (docs/v16_data_versions_application.md).

Part 1 -- the version record (`utils/data_versions.py`): a version exists only
when its record is complete AND it is in the index; ids are a counter plus a
timestamp; what makes two versions the same is compared from existing fields
(ROI geometry included); incomplete folders are cleaned; published versions
are recognised as read-only.

Synthetic workspaces in the test's temporary directory only.
"""

import json
import os
from datetime import datetime

import pytest

from block01.utils import data_versions as dv


def _ws(tmp_path):
    ws = tmp_path / "proj" / "rois" / "ws1"
    ws.mkdir(parents=True)
    (ws / "roi_manifest.json").write_text("{}")
    return str(ws)


def _record(bbox=(0, 100, 0, 200), sigs=None, remap="r1", fusion="f1", poly=None,
            slide="slide_a"):
    return {
        "slide_id": slide,
        "regions": [{"roi_name": "Full WSI", "roi_id": "ws1", "bbox_fullres": list(bbox),
                     "polygon_fullres": poly, "fused_zarr_path": "/x/fused.zarr"}],
        "corrected": {"path": "/x/corrected.zarr",
                      "signatures": sigs if sigs is not None else
                      {"CD3": ["tophat", 25, "2", "cpu", "disk"]},
                      "bboxes": [list(bbox)], "source_identity": {"dataset_path": "/s"}},
        "step0_remap_hash": remap,
        "fusion_settings_hash": fusion,
        "method": "stardist_nuclei_expansion",
    }


def _commit(ws, record=None, when=None):
    alloc = dv.new_version_folder(ws, now=when)
    return dv.commit_version(ws, alloc, record or _record())


# ── existence ────────────────────────────────────────────────────────────

def test_a_committed_version_exists_and_is_current(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws, when=datetime(2026, 10, 3, 12, 0, 0))
    assert rec["version"] == "v001" and rec["folder"] == "v001_20261003_120000"
    assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
    assert dv.current_version(ws)["version"] == "v001"
    assert dv.load_index(ws)["current"] == "v001"


def test_an_allocated_but_uncommitted_version_does_not_exist(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    alloc = dv.new_version_folder(ws)              # a Generate that then failed
    open(os.path.join(alloc["path"], "partial.bin"), "wb").write(b"x")
    assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
    assert dv.current_version(ws)["version"] == "v001"   # current not moved


def test_a_record_without_complete_is_not_a_version(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws)
    path = os.path.join(dv.version_dir(ws, rec["folder"]), dv.RECORD)
    data = json.load(open(path))
    data["complete"] = False
    json.dump(data, open(path, "w"))
    assert dv.list_versions(ws) == [] and dv.current_version(ws) is None


def test_incomplete_folders_are_cleaned_and_numbers_never_reused(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    failed = dv.new_version_folder(ws)
    draft = dv.new_corrected_folder(ws)
    removed = dv.cleanup_incomplete(ws)
    assert removed == [failed["folder"]]
    assert os.path.isdir(os.path.dirname(draft))          # corrected folders are kept
    # the failed folder is gone, so v001 is the highest number on disk
    assert dv.new_version_folder(ws)["version"] == "v002"


def test_numbers_of_failed_folders_still_on_disk_are_not_reused(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    failed = dv.new_version_folder(ws)                     # v002, not cleaned yet
    nxt = dv.new_version_folder(ws)
    assert failed["version"] == "v002" and nxt["version"] == "v003"


def test_set_current_only_to_an_existing_version(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    _commit(ws, _record(remap="r2"))
    assert dv.current_version(ws)["version"] == "v002"
    assert dv.set_current(ws, "v001") is True
    assert dv.current_version(ws)["version"] == "v001"
    assert dv.set_current(ws, "v009") is False


# ── comparing ────────────────────────────────────────────────────────────

def test_same_parameters_and_geometry_is_the_same_version(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    assert dv.find_same(ws, _record())["version"] == "v001"


@pytest.mark.parametrize("change", [
    {"bbox": (0, 100, 0, 150)},                                  # geometry
    {"poly": [[0, 0], [10, 0], [10, 10]]},                       # polygon
    {"sigs": {"CD3": ["tophat", 40, "2", "cpu", "disk"]}},       # a channel's correction
    {"remap": "r2"},                                             # Step0 Intensity
    {"fusion": "f2"},                                            # Step1 settings
    {"slide": "slide_b"},                                        # another slide
])
def test_any_difference_is_another_version(tmp_path, change):
    ws = _ws(tmp_path)
    _commit(ws)
    assert dv.find_same(ws, _record(**change)) is None


def test_corrected_is_shared_only_when_the_whole_product_is_the_same(tmp_path):
    ws = _ws(tmp_path)
    _commit(ws)
    # only Step1 / Intensity changed: the corrected product is the same one
    assert dv.find_same_corrected(ws, _record(remap="r2", fusion="f2"))["version"] == "v001"
    # same parameters, other ROI geometry: never shared
    assert dv.find_same_corrected(ws, _record(bbox=(0, 100, 0, 150))) is None
    # one channel differs: never shared
    assert dv.find_same_corrected(
        ws, _record(sigs={"CD3": ["tophat", 25, "2", "cpu", "disk"],
                          "CD8": ["cucim", 30, "2", "cpu", None]})) is None


# ── read-only recognition ────────────────────────────────────────────────

def test_products_of_published_versions_are_recognised(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws)
    inside = os.path.join(dv.version_dir(ws, rec["folder"]), "corrected_channels.zarr")
    assert dv.is_published_product(ws, inside)
    assert dv.is_published_product(ws, "/x/corrected.zarr")   # a referenced legacy product
    assert not dv.is_published_product(ws, dv.new_corrected_folder(ws))


def test_a_versions_own_folder_size_excludes_what_it_references(tmp_path):
    ws = _ws(tmp_path)
    rec = _commit(ws)
    with open(os.path.join(dv.version_dir(ws, rec["folder"]), "blob"), "wb") as f:
        f.write(b"0" * 1000)
    assert dv.version_size(ws, rec) >= 1000


# ── part 2: Step0's corrected draft and copy-on-write (§3.15) ─────────────

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_v16_a6_workspace import (  # noqa: E402,F401  (fixtures)
    app, slides, page, _project, _commit_step0, _tree)


from PyQt5 import QtCore, QtWidgets  # noqa: E402
from block01.ui.step0.step0_page import Step0Page as _Step0Page  # noqa: E402

#: the real chooser (the A6 `page` fixture answers it the way Enter does)
_REAL_CHOOSE = _Step0Page._choose_workspace


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """A dialog nobody answers fails the test instead of hanging it."""
    def _refuse(dlg, *a, **k):
        raise AssertionError(f"unexpected modal dialog: {dlg.windowTitle()!r}")
    monkeypatch.setattr(QtWidgets.QDialog, "exec_", _refuse)


class _FakeWsi(QtCore.QThread):
    """WsiCorrectionWorker's contract (a QThread whose business `finished`
    shadows the base one); records what it was asked to do, runs nothing."""
    progress = QtCore.pyqtSignal(int, int, int, int, str, str, int)
    finished = QtCore.pyqtSignal(str, dict)
    canceled = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    made = []

    def __init__(self, loader, output_dir, config, rois=None, parent=None,
                 process_channels=None, incremental=False):
        super().__init__()
        self.output_dir, self.process_channels = output_dir, set(process_channels or ())
        self.incremental = incremental
        _FakeWsi.made.append(self)

    def stop_after_current_channel(self):
        pass

    def start(self):
        pass


def _published_with_tophat(tmp_path, slides):
    """A workspace whose published handoff references a corrected product
    that holds CD3 and CD8 (TopHat r=25) -- the product a version owns."""
    proj, made = _project(tmp_path, slides["a"], commit=False)
    ctx = made[0]
    _commit_step0(ctx, decisions={"CD3": "tophat", "CD8": "tophat"})
    z = os.path.join(ctx["step_dirs"]["step0"], "corrected_channels.zarr")
    with open(os.path.join(z, "marker.bin"), "wb") as f:
        f.write(b"published pixels")
    sidecar = os.path.join(ctx["step_dirs"]["step0"], "corrected_coarse.zarr")
    os.makedirs(sidecar)
    # a version references it: from now on it is read-only (§3.14)
    alloc = dv.new_version_folder(ctx["roi_dir"])
    # (no Step1 settings saved: the version records none, so no draft)
    dv.commit_version(ctx["roi_dir"], alloc, dict(_record(), corrected={"path": z},
                                                  fusion_settings_hash=""))
    return proj, ctx, z


def _drafts(ws):
    root = dv.corrected_root(ws)
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


@pytest.fixture
def dv_page(page, monkeypatch, tmp_path, slides):
    import block01.ui.step0.step0_page as sp
    _FakeWsi.made = []
    monkeypatch.setattr(sp, "WsiCorrectionWorker", _FakeWsi)
    monkeypatch.setattr(sp, "_WsiCorrectionProgressDialog",
                        lambda parent: type("D", (), {"cancel_requested": type(
                            "S", (), {"connect": lambda *a, **k: None})(),
                            "show": lambda self: None, "exec_": lambda self: 0})())
    return page


def _sigs(page, config_mp, decisions):
    cfg = {"method_params": config_mp, "channel_params": {}}
    return {ch: page._save_signature(cfg, ch, m) for ch, m in decisions.items()}


def test_changing_one_channel_copies_the_published_product_once_and_corrects_only_it(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    before = _tree(published)
    dv_page._channel_params = {"CD8": {"tophat_radius": 40}}     # CD8 25 -> 40
    dv_page._save_and_continue()
    w = _FakeWsi.made[-1]
    (folder,) = _drafts(ctx["roi_dir"])
    draft_dir = os.path.join(dv.corrected_root(ctx["roi_dir"]), folder)
    assert w.output_dir == draft_dir                              # written into the draft
    assert w.incremental is True and w.process_channels == {"CD8"}
    assert os.path.isfile(os.path.join(draft_dir, "corrected_channels.zarr", "marker.bin"))
    assert os.path.isdir(os.path.join(draft_dir, "corrected_coarse.zarr"))   # sidecar too
    assert _tree(published) == before                             # read-only


def test_a_save_with_no_corrected_change_references_the_published_product(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    dv_page._save_and_continue()
    assert _FakeWsi.made == []                                    # nothing recomputed
    assert _drafts(ctx["roi_dir"]) == []                          # nothing copied


def test_another_roi_makes_a_fresh_draft_and_recomputes_every_channel(
        dv_page, tmp_path, slides, monkeypatch):
    import block01.ui.step0.step0_page as sp
    from block01.ui.step0.step0_page import Step0Page
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    monkeypatch.setattr(Step0Page, "_ask_region_changed", lambda self: "overwrite")
    roi = {"name": "R1", "bbox_fullres": [100, 400, 200, 600], "type": "roi",
           "polygon_fullres": [[200, 100], [600, 100], [600, 400]]}
    monkeypatch.setattr(type(dv_page.overview), "get_rois", lambda self: [dict(roi)])
    monkeypatch.setattr(Step0Page, "_roi_count", lambda self: 1)
    before = _tree(published)
    dv_page._save_and_continue()
    w = _FakeWsi.made[-1]
    (folder,) = _drafts(ctx["roi_dir"])
    draft_dir = os.path.join(dv.corrected_root(ctx["roi_dir"]), folder)
    assert w.output_dir == draft_dir
    assert w.incremental is False and w.process_channels == {"CD3", "CD8"}
    assert not os.path.exists(os.path.join(draft_dir, "corrected_channels.zarr"))  # not copied
    assert _tree(published) == before


def test_once_a_draft_exists_saves_only_touch_the_draft(dv_page, tmp_path, slides,
                                                         monkeypatch):
    """The handoff references the draft (no version references it yet):
    the next Save corrects it in place and copies nothing."""
    import json as _json
    import block01.ui.step0.step0_page as sp
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    draft = dv.new_corrected_folder(ctx["roi_dir"])
    os.makedirs(draft)
    manifest = os.path.join(ctx["step_dirs"]["step0"], "step0_roi_result.json")
    m = _json.load(open(manifest))
    m["corrected_zarr_path"] = draft
    _json.dump(m, open(manifest, "w"))
    from block01.ui.step0.step0_page import Step0Page, DRAFT_TAG
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: next(r for r in rows if r[2] == DRAFT_TAG))
    dv_page._open_existing_workspace()
    held = _sigs(dv_page, {"tophat_radius": 25, "cucim_sigma": 30},
                 {"CD3": "tophat", "CD8": "tophat"})
    monkeypatch.setattr(sp, "read_corrected_zarr_state",
                        lambda path: (dict(held), [(0, 1001, 0, 999)]))
    before = _tree(published)
    dv_page._channel_params = {"CD3": {"tophat_radius": 30}}
    dv_page._save_and_continue()
    w = _FakeWsi.made[-1]
    assert w.output_dir == os.path.dirname(draft) and w.process_channels == {"CD3"}
    assert w.incremental is True
    assert len(_drafts(ctx["roi_dir"])) == 1                     # no second copy
    assert _tree(published) == before

def test_the_handoff_stays_in_the_workspaces_step0_and_registers_nothing(
        dv_page, tmp_path, slides):
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    spec = dv_page._handoff_spec({}, dv.new_corrected_folder(ctx["roi_dir"]))
    assert spec["step0_dir"] == ctx["step_dirs"]["step0"]
    assert spec["manifest_path"] == os.path.join(ctx["step_dirs"]["step0"],
                                                 "step0_roi_result.json")
    assert spec["register_corrected"] is False


# ── part 3: the Generate transaction and the deferred A3 (§3.12) ──────────

import test_step1_fusion_isolation as iso    # noqa: E402
import test_v16_a6_async as a6               # noqa: E402
import test_step1_result_publication as pub  # noqa: E402


def _dv_window(app, tmp_path):
    w = iso._window(app, tmp_path)
    ws = tmp_path / "proj" / "rois" / "ws1"
    (ws / "step0").mkdir(parents=True)
    (ws / "roi_manifest.json").write_text("{}")
    w.step0_output = dict(w.step0_output, roi_dir=str(ws), roi_id="ws1",
                          step0_dir=str(ws / "step0"))
    return w, str(ws)


class _JobsWorker:
    def __init__(self):
        self.provenance_jobs = [("/f1", "/raw", "R1", (0, 1, 0, 1), ["CD3"])]
        self.registered = []

    def _register_fused(self, *job):
        self.registered.append(job)


def _pending(w, ws, regions, write_meta_for):
    alloc = dv.new_version_folder(ws)
    metas = []
    for name in write_meta_for:
        z = os.path.join(alloc["path"], f"fused_{name}.zarr")
        os.makedirs(z)
        metas.append({"roi_name": name, "zarr_path": z})
    with open(os.path.join(alloc["path"], "fusion_meta.json"), "w") as f:
        json.dump({"regions": metas}, f)
    rec = dict(_record(), regions=[{"roi_name": n, "roi_id": n, "bbox_fullres": [0, 10, 0, 10],
                                    "polygon_fullres": None} for n in regions])
    worker = _JobsWorker()
    w._dv_pending = {"workspace": ws, "alloc": alloc, "record": rec, "worker": worker}
    return alloc, worker


def test_a_successful_generate_publishes_then_registers_a3(app, tmp_path, monkeypatch):
    from block01.core import provenance as prov
    w, ws = _dv_window(app, tmp_path)
    seen = []
    monkeypatch.setattr(prov, "register_corrected_channels",
                        lambda *a, **k: seen.append(("corrected", dv.load_index(ws)["current"])))
    try:
        alloc, worker = _pending(w, ws, ["R1", "R2"], ["R1", "R2"])
        committed = w._dv_commit_pending()
        assert committed["version"] == "v001" and dv.current_version(ws)["version"] == "v001"
        regions = dv.current_version(ws)["regions"]
        assert [r["roi_name"] for r in regions] == ["R1", "R2"]          # multi-ROI
        assert all(os.path.isdir(r["fused_zarr_path"]) for r in regions)
        # A3 after the index named the version, never before
        assert worker.registered == [("/f1", "/raw", "R1", (0, 1, 0, 1), ["CD3"])]
        assert w._dv_pending is None
    finally:
        w.close()


def test_a_region_without_its_product_publishes_nothing_and_registers_nothing(
        app, tmp_path, monkeypatch):
    from block01.core import provenance as prov
    w, ws = _dv_window(app, tmp_path)
    first = dv.commit_version(ws, dv.new_version_folder(ws), _record())     # v1
    calls = []
    monkeypatch.setattr(prov, "register_corrected_channels", lambda *a, **k: calls.append(1))
    errors = []
    monkeypatch.setattr(w, "_on_fusion_error", lambda msg: errors.append(msg))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    try:
        alloc, worker = _pending(w, ws, ["R1", "R2"], ["R1"])            # R2 failed
        w._on_fusion_done("/whatever")
        assert errors and "could not be published" in errors[0]
        assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
        assert dv.current_version(ws)["version"] == first["version"]
        assert not os.path.exists(alloc["path"])                          # folder gone
        assert worker.registered == [] and calls == []                    # no A3
    finally:
        w.close()


def test_an_error_or_cancel_leaves_no_version_and_the_current_one(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *a, **k: None)
    w, ws = _dv_window(app, tmp_path)
    dv.commit_version(ws, dv.new_version_folder(ws), _record())         # v1
    worker = a6._SlowStop()
    try:
        alloc = dv.new_version_folder(ws)
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w._dv_pending = {"workspace": ws, "alloc": alloc, "record": _record(),
                         "worker": worker}
        worker.release()                                   # error("stopped") from run()
        worker.exit_gate.set()
        worker.wait(5000)
        a6._pump()
        assert not os.path.exists(alloc["path"])
        assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
        assert dv.current_version(ws)["version"] == "v001"
    finally:
        worker.release()
        worker.exit_gate.set()
        worker.wait(5000)
        a6._pump()
        w.close()


def test_generate_with_nothing_changed_returns_to_the_same_version(app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    done = []
    monkeypatch.setattr(w, "_on_fusion_done", lambda path: done.append(path))
    try:
        alloc = dv.new_version_folder(ws)
        fused = os.path.join(alloc["path"], "fused_R1.zarr")
        os.makedirs(fused)
        rec = dict(_record(), regions=[{"roi_name": "R1", "roi_id": "R1",
                                        "bbox_fullres": [0, 10, 0, 10], "polygon_fullres": None,
                                        "fused_zarr_path": fused}])
        dv.commit_version(ws, alloc, rec)
        dv.commit_version(ws, dv.new_version_folder(ws), _record(fusion="other"))  # v2 current
        assert w._dv_reuse_same(ws, dict(rec)) is True
        assert dv.current_version(ws)["version"] == "v001" and done == [fused]
        assert w._dv_reuse_same(ws, _record(fusion="third")) is False
    finally:
        w.close()


def test_the_worker_keeps_the_a3_entries_for_later(tmp_path, monkeypatch):
    from block01.ui.step0 import overview_panel as op
    called = []
    monkeypatch.setattr(op.FullFusionWorker, "_register_fused",
                        lambda self, *a: called.append(a))
    wk = pub._worker(tmp_path)
    wk.register_provenance = False
    wk.run()
    assert called == []
    assert len(wk.provenance_jobs) == 1 and wk.provenance_jobs[0][2] == "full"


# ── part 4: Step2 / Step4 read the run's own data version ─────────────────

import shutil  # noqa: E402


def test_a_dirty_draft_refuses_a_new_step2_run_and_says_why(app, tmp_path, monkeypatch):
    from block01.ui import step2_page as s2
    p = s2.Step2Page()
    said = []
    monkeypatch.setattr(s2.QMessageBox, "warning", lambda *a, **k: said.append(a[2]))
    started = []
    monkeypatch.setattr(s2, "SegmentMergeWorker", lambda **kw: started.append(kw))
    try:
        p.set_dirty_draft("settings changed since v001 (dirty draft)")
        assert not p._btn_run.isEnabled() and not p._dirty_lbl.isHidden()
        p._run()
        assert started == [] and "dirty draft" in said[-1]
        p.set_dirty_draft("")
        assert p._btn_run.isEnabled() and p._dirty_lbl.isHidden()
    finally:
        p.deleteLater()


def test_a_run_carries_its_data_version_and_region_inputs(app, tmp_path, monkeypatch):
    import test_v16_a6_async as a6m
    from block01.ui import step2_page as s2
    import numpy as np
    zarr = pytest.importorskip("zarr")
    monkeypatch.setattr(s2, "SegmentMergeWorker", a6m._FakeStep2Run)
    p = s2.Step2Page()
    monkeypatch.setattr(p, "_promote_step0_remap", lambda cfg: None)
    zp = str(tmp_path / "fused_R1.zarr")
    z = zarr.open(zp, mode="w", shape=(64, 64, 2), chunks=(64, 64, 2), dtype=np.uint16)
    z[...] = 1
    p._zarr_edit.setText(zp)
    p._load_zarr_info()
    p._set_param_source("manual")
    try:
        p.set_data_version("v003", {"R1": zp})
        p._run()
        cfg = p._worker.kw["seg_config"]
        assert cfg["data_version"] == "v003" and cfg["region_fused_paths"] == {"R1": zp}
    finally:
        p.deleteLater()


def test_the_window_marks_a_dirty_draft_against_the_current_version(app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    try:
        cur, why = w._dv_step2_state()
        assert cur is None and "No data version" in why
        dv.commit_version(ws, dv.new_version_folder(ws), _record())
        monkeypatch.setattr(w, "_dv_candidate_record", lambda method: _record())
        cur, why = w._dv_step2_state()
        assert cur["version"] == "v001" and why == ""
        monkeypatch.setattr(w, "_dv_candidate_record", lambda method: _record(fusion="f9"))
        cur, why = w._dv_step2_state()
        assert "dirty draft" in why and "v001" in why
    finally:
        w.close()


def test_the_worker_reads_each_regions_fused_from_its_version(tmp_path):
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    ws = tmp_path / "proj" / "rois" / "ws1"
    (ws / "step1" / "fused_R1.zarr").mkdir(parents=True)        # another version's fixed path
    (ws / "step2").mkdir()
    (ws / "roi_manifest.json").write_text("{}")
    vfused = ws / "versions" / "v002_x" / "fused_R1.zarr"
    vfused.mkdir(parents=True)
    wk = SegmentMergeWorker(str(vfused), seg_config={
        "method": "cellpose_wholecell_fusion", "data_version": "v002",
        "region_fused_paths": {"R1": str(vfused)}}, output_dir=str(ws / "step2"))
    assert wk._fusion_source_path("R1") == os.path.abspath(vfused)


def test_step4_quantifies_a_run_with_its_own_versions_corrected_product(tmp_path):
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_quant_sources import build_project, _edit_json
    from block01.core import quant_sources as qs
    p = build_project(tmp_path)
    ws = p["ws"]
    # v001 owns a copy of the corrected product and its correction config
    alloc = dv.new_version_folder(ws)
    vz = os.path.join(alloc["path"], "corrected_channels.zarr")
    shutil.copytree(p["zarr"], vz)
    shutil.copy2(p["cfg"], os.path.join(alloc["path"], "correction_config.json"))
    dv.commit_version(ws, alloc, dict(_record(), corrected={"path": vz}))
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d.update(data_version="v001"))
    # a later Step0 Save replaced the workspace's product
    shutil.rmtree(p["zarr"])
    job = qs.resolve_quant_job(p["run_dir"], open_slide=p["slide"])
    cd3 = next(c for c in job.channels if c.name == "CD3")
    assert cd3.kind == "corrected" and os.path.abspath(cd3.path) == os.path.abspath(vz)


def test_skip_empty_tiles_finds_the_workspace_of_a_versioned_fused_product(tmp_path):
    from block01.core import tile_tissue
    ws = tmp_path / "proj" / "rois" / "ws1"
    v = ws / "versions" / "v001_x"
    v.mkdir(parents=True)
    (ws / "roi_manifest.json").write_text("{}")
    assert tile_tissue._workspace_of(str(v / "fused_R1.zarr")) == str(ws)


# ── part 5: loading a workspace + version (§3.6, §3.7, §3.9) ───────────────

def _two_versions(ws_ctx):
    """v001 (a ROI, TopHat r=10, its own corrected product) and v002 (current)."""
    ws = ws_ctx["roi_dir"]
    made = []
    for n, (bbox, radius) in enumerate((([0, 10, 0, 20], 10), ([0, 30, 0, 40], 40)), 1):
        alloc = dv.new_version_folder(ws)
        corrected = os.path.join(ws, "versions", "corrected", f"c00{n}", "corrected_channels.zarr")
        os.makedirs(corrected)
        with open(os.path.join(alloc["path"], "correction_config.json"), "w") as f:
            json.dump({"method_params": {"tophat_radius": radius, "cucim_sigma": 30},
                       "channel_decisions": {"CD3": "tophat", "CD8": "original"},
                       "channel_params": {}}, f)
        with open(os.path.join(alloc["path"], "step0_channel_remap.json"), "w") as f:
            json.dump({"version_marker": n}, f)
        fused = os.path.join(alloc["path"], f"fused_ROI {n}.zarr")
        import zarr as _zarr
        _zarr.open_group(fused, mode="w")
        rec = dict(_record(bbox=bbox), corrected={"path": corrected},
                   regions=[{"roi_name": f"ROI {n}", "roi_id": f"r{n}", "bbox_fullres": bbox,
                             "polygon_fullres": None, "fused_zarr_path": fused}])
        made.append(dv.commit_version(ws, alloc, rec))
    return made


def test_the_chooser_lists_one_row_per_workspace_and_version(page, tmp_path, slides,
                                                              monkeypatch):
    from block01.utils import workspace_session as wsess
    from block01.ui.step3_mask_bar import TAG_ROLE, TaggedItemDelegate
    proj, made = _project(tmp_path, slides["a"], n=2)
    _v1, v2 = _two_versions(made[0])
    _workspace_as(made[0], v2)                 # no draft: see the draft-row tests
    found = wsess.find_workspaces(proj, slides["a"])[1]
    rows = page._workspace_rows(found)
    tags = {(r[0].workspace_id, r[2]) for r in rows}
    assert (made[0]["roi_id"], "v002  (current)") in tags
    assert (made[0]["roi_id"], "v001") in tags
    assert (made[1]["roi_id"], "unknown") in tags and len(rows) == 3
    # newest version of a workspace first
    own = [r[2] for r in rows if r[0].workspace_id == made[0]["roi_id"]]
    assert own == ["v002  (current)", "v001"]
    # the dialog: Step3's delegate, tags right-aligned, the current row chosen
    shown = {}

    def _exec(dlg):
        lst = dlg.findChild(QtWidgets.QListWidget)
        shown["delegate"] = type(lst.itemDelegate())
        shown["tags"] = [lst.item(i).data(TAG_ROLE) for i in range(lst.count())]
        return QtWidgets.QDialog.Accepted
    monkeypatch.setattr(QtWidgets.QDialog, "exec_", _exec)
    chosen = _REAL_CHOOSE(page, rows)
    assert shown["delegate"] is TaggedItemDelegate
    assert shown["tags"][:3] == [r[2] for r in rows] and shown["tags"][3] is None
    assert chosen[2] == "v002  (current)"


def test_loading_an_older_version_restores_it_and_republishes_the_handoff(
        page, tmp_path, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, made = _project(tmp_path, slides["a"])
    v1, v2 = _two_versions(made[0])
    ws = made[0]["roi_dir"]
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: next(r for r in rows if r[1] and r[1]["version"] == "v001"))
    wrote = []

    def _write(self, config, zarr_path, remap_config_path=None):
        wrote.append((dict(config), zarr_path, list(self._standard_rois())))
        return config, wrote[-1][2], [], {"roi_id": made[0]["roi_id"]}
    monkeypatch.setattr(Step0Page, "_write_step0_handoff", _write)
    page._open_existing_workspace()
    # the version's own parameters, geometry and corrected product
    assert page._tophat_slider.value() == 10
    assert len(wrote) == 1 and wrote[0][1] == v1["corrected"]["path"]
    assert [r.get("bbox_fullres") for r in wrote[0][2]] == [[0, 10, 0, 20]]
    with open(os.path.join(made[0]["step_dirs"]["step0"], "step0_channel_remap.json")) as f:
        assert json.load(f) == {"version_marker": 1}
    assert dv.current_version(ws)["version"] == "v001"
    sent = []
    page.step0_complete.connect(sent.append)
    assert page._announce_opened_workspace() is True
    assert sent[0]["data_version_loaded"] == "v001" and sent[0]["opened_workspace"] is True
    # nothing of any version was written
    assert [v["version"] for v in dv.list_versions(ws)] == ["v001", "v002"]


def _workspace_as(ctx, rec):
    """The workspace's Step0 says exactly what version `rec` says."""
    ws, step0 = ctx["roi_dir"], ctx["step_dirs"]["step0"]
    vdir = dv.version_dir(ws, rec["folder"])
    for name in ("correction_config.json", "step0_channel_remap.json"):
        shutil.copy2(os.path.join(vdir, name), os.path.join(step0, name))
    mpath = os.path.join(step0, "step0_roi_result.json")
    with open(mpath) as f:
        manifest = json.load(f)
    manifest["corrected_zarr_path"] = rec["corrected"]["path"]
    with open(mpath, "w") as f:
        json.dump(manifest, f)
    step1 = os.path.join(ws, "step1")
    os.makedirs(step1, exist_ok=True)
    with open(os.path.join(step1, "step1_fusion_settings.json"), "w") as f:
        json.dump({"hash": rec.get("fusion_settings_hash") or ""}, f)


def test_opening_the_current_version_rewrites_nothing(page, tmp_path, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page
    proj, made = _project(tmp_path, slides["a"])
    _v1, v2 = _two_versions(made[0])
    _workspace_as(made[0], v2)
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: next(r for r in rows if r[1] and r[1]["version"] == "v002"))
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda *a, **k: pytest.fail("the current version is not republished"))
    before = _tree(made[0]["roi_dir"])
    page._open_existing_workspace()
    assert _tree(made[0]["roi_dir"]) == before
    sent = []
    page.step0_complete.connect(sent.append)
    page._announce_opened_workspace()
    assert sent[0]["data_version_loaded"] == "" and sent[0]["opened_workspace"] is True


def test_a_loaded_versions_step1_files_are_installed_and_bound_again(app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    try:
        step1 = os.path.join(ws, "step1")
        os.makedirs(step1)
        manifest = os.path.join(ws, "step0", "step0_roi_result.json")
        with open(manifest, "w") as f:
            json.dump({"source_identity": {"dataset_path": "/now"}}, f)
        w.step0_output = dict(w.step0_output, step1_dir=step1, step0_manifest_path=manifest)
        alloc = dv.new_version_folder(ws)
        with open(os.path.join(alloc["path"], "step1_fusion_settings.json"), "w") as f:
            json.dump({"hash": "h1", "fusion_config": {"a": 1},
                       "handoff_identity": {"manifest_digest": "old"}}, f)
        with open(os.path.join(alloc["path"], "step1_session.json"), "w") as f:
            json.dump({"weights": {"CD3": 2}, "step0_manifest_path": "/old",
                       "source_identity": {"dataset_path": "/old"}}, f)
        dv.commit_version(ws, alloc, _record())
        monkeypatch.setattr(w, "_handoff_identity", lambda: {"manifest_digest": "new"})
        assert w._dv_install_step1_files("v001") is True
        with open(os.path.join(step1, "step1_fusion_settings.json")) as f:
            snap = json.load(f)
        assert snap["handoff_identity"] == {"manifest_digest": "new"}
        assert snap["hash"] == "h1" and snap["fusion_config"] == {"a": 1}
        with open(os.path.join(step1, "step1_session.json")) as f:
            sess = json.load(f)
        assert sess["step0_manifest_path"] == manifest and sess["weights"] == {"CD3": 2}
        assert sess["source_identity"] == {"dataset_path": "/now"}
        # the version's own copies are untouched
        with open(os.path.join(alloc["path"], "step1_session.json")) as f:
            assert json.load(f)["step0_manifest_path"] == "/old"
    finally:
        w.close()


def test_an_opened_workspace_restores_step1_by_itself_once(app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    try:
        calls = []
        monkeypatch.setattr(w, "_load_step0_roi_result", lambda *a, **k: True)
        monkeypatch.setattr(w, "_dv_register_legacy", lambda: calls.append("legacy"))
        monkeypatch.setattr(w, "_dv_install_step1_files", lambda v: calls.append(("install", v)))
        monkeypatch.setattr(w, "_load_previous_step1_session",
                            lambda *a, **k: calls.append(("restore", k.get("auto"))))
        w._on_step0_complete(dict(w.step0_output, opened_workspace=True,
                                  data_version_loaded="v002"))
        assert calls == ["legacy", ("install", "v002")]
        w._go_to_step1()
        w._go_to_step1()
        assert calls[2:] == [("restore", True)]                       # once
        # a plain Save does not restore Step1 by itself
        calls.clear()
        w._on_step0_complete(dict(w.step0_output, opened_workspace=False,
                                  data_version_loaded=""))
        w._go_to_step1()
        assert calls == []
    finally:
        w.close()


def test_step3_rows_show_their_runs_data_version():
    from types import SimpleNamespace
    from block01.ui.main_window import MainWindow
    assert MainWindow._step3_run_tag(SimpleNamespace(meta={"data_version": "v002"})) == "v002"
    assert MainWindow._step3_run_tag(SimpleNamespace(meta={"data_version": None})) == "unknown"
    assert MainWindow._step3_run_tag(SimpleNamespace(meta={})) == "unknown"


def test_a_generate_keeps_step1s_session_in_the_version(app, tmp_path, monkeypatch):
    from block01.core import provenance as prov
    w, ws = _dv_window(app, tmp_path)
    try:
        step1 = os.path.join(ws, "step1")
        os.makedirs(step1)
        w.step0_output = dict(w.step0_output, step1_dir=step1)
        monkeypatch.setattr(prov, "register_corrected_channels", lambda *a, **k: None)

        def _save():
            with open(os.path.join(step1, "step1_session.json"), "w") as f:
                json.dump({"saved": "at generate"}, f)
        monkeypatch.setattr(w, "_save_step1_session", _save)
        alloc, _worker = _pending(w, ws, ["R1"], ["R1"])
        w._dv_commit_pending()
        with open(os.path.join(alloc["path"], "step1_session.json")) as f:
            assert json.load(f)["saved"] == "at generate"
    finally:
        w.close()


# ── part 6: an earlier workspace becomes v1 (§3.8) ─────────────────────────

def _legacy(w, ws, fused_regions):
    step1 = os.path.join(ws, "step1")
    os.makedirs(step1)
    w.step0_output = dict(w.step0_output, step1_dir=step1)
    for name in fused_regions:
        os.makedirs(os.path.join(step1, f"fused_{name}.zarr"))
    with open(os.path.join(step1, "fusion_meta.json"), "w") as f:
        json.dump({"regions": [{"roi_name": n, "zarr_path": "/moved/elsewhere"}
                               for n in fused_regions]}, f)
    with open(os.path.join(step1, "step1_fusion_settings.json"), "w") as f:
        json.dump({"hash": "h_legacy"}, f)
    corrected = os.path.join(ws, "step0", "corrected_channels.zarr")
    os.makedirs(corrected)
    return step1, corrected


def test_an_earlier_workspace_is_registered_as_v1_in_place(app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    try:
        step1, corrected = _legacy(w, ws, ["R1"])
        rec = dict(_record(), corrected={"path": corrected},
                   regions=[{"roi_name": "R1", "roi_id": "r1", "bbox_fullres": [0, 5, 0, 5],
                             "polygon_fullres": None}])
        monkeypatch.setattr(w, "_dv_candidate_record", lambda method: dict(rec))
        before = _tree(step1)
        v1 = w._dv_register_legacy()
        assert v1["version"] == "v001" and v1["label"] == dv.LEGACY_LABEL
        assert dv.current_version(ws)["version"] == "v001"
        assert v1["regions"][0]["fused_zarr_path"] == os.path.join(step1, "fused_R1.zarr")
        assert v1["fusion_settings_hash"] == "h_legacy"
        # referenced where they are: read-only from now on, nothing moved
        assert dv.is_published_product(ws, corrected)
        assert dv.is_published_product(ws, os.path.join(step1, "fused_R1.zarr"))
        assert _tree(step1) == before
        assert w._dv_register_legacy() is None                         # once
        assert len(dv.list_versions(ws)) == 1
    finally:
        w.close()


def test_an_earlier_workspace_without_every_fused_region_is_not_registered(
        app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    try:
        _step1, corrected = _legacy(w, ws, ["R1"])
        rec = dict(_record(), regions=[
            {"roi_name": n, "roi_id": n, "bbox_fullres": [0, 5, 0, 5], "polygon_fullres": None}
            for n in ("R1", "R2")])
        monkeypatch.setattr(w, "_dv_candidate_record", lambda method: dict(rec))
        assert w._dv_register_legacy() is None
        assert dv.list_versions(ws) == []
        assert not os.path.isdir(dv.versions_dir(ws)) or \
            os.listdir(dv.versions_dir(ws)) == []
    finally:
        w.close()


# ── codex review (astra, low) 2026-10-03: findings 1-6 ─────────────────────

def test_a_handoff_write_never_touches_a_published_corrected_product(tmp_path):
    """Finding 1: the writer stamps attributes into the corrected product;
    a published one is read-only."""
    import test_v16_artifact_graph as ag_t
    slide = ag_t._slide(tmp_path)
    project, ctx = ag_t._workspace(tmp_path, slide)
    step0 = ctx["step_dirs"]["step0"]
    os.makedirs(step0, exist_ok=True)
    z = ag_t._corrected_product(step0)
    alloc = dv.new_version_folder(ctx["roi_dir"])
    dv.commit_version(ctx["roi_dir"], alloc, dict(_record(), corrected={"path": z}))
    before = _tree(z)
    from block01.core import step0_handoff
    real = step0_handoff.write_handoff

    def _ro(spec, **kw):
        return real(dict(spec, corrected_read_only=True), **kw)
    step0_handoff.write_handoff = _ro
    try:
        ag_t._handoff(project, ctx, slide)
    finally:
        step0_handoff.write_handoff = real
    assert _tree(z) == before


def test_the_page_marks_a_published_product_read_only_for_the_writer(dv_page, tmp_path,
                                                                       slides):
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    assert dv_page._handoff_spec({}, published)["corrected_read_only"] is True
    draft = dv.new_corrected_folder(ctx["roi_dir"])
    assert dv_page._handoff_spec({}, draft)["corrected_read_only"] is False


def test_a_new_outline_with_the_same_boxes_goes_into_a_copy(dv_page, tmp_path, slides):
    proj, ctx, published = _published_with_tophat(tmp_path, slides)
    dv_page._open_existing_workspace()
    path = dv_page._dv_draft_for_geometry(published, copy=True)
    assert path != published and os.path.isfile(os.path.join(path, "marker.bin"))
    fresh = dv_page._dv_draft_for_geometry(published, copy=False)
    assert fresh != published and not os.path.exists(fresh)
    assert dv_page._dv_draft_for_geometry(path, copy=True) == path    # a draft: in place


def test_a_channel_switched_back_to_raw_is_another_version(tmp_path):
    """Finding 2: the old array stays in the product, so its signature does
    too; the decision is what changed."""
    a = dict(_record(), channel_decisions={"CD3": "tophat", "CD8": "original"})
    b = dict(_record(), channel_decisions={"CD3": "original", "CD8": "original"})
    assert not dv.same_content(a, b)
    assert dv.same_content(a, dict(a))


def test_the_candidate_record_carries_the_correction_decisions(app, tmp_path, monkeypatch):
    w, ws = _dv_window(app, tmp_path)
    try:
        with open(os.path.join(ws, "step0", "correction_config.json"), "w") as f:
            json.dump({"channel_decisions": {"CD3": "tophat"}}, f)
        monkeypatch.setattr(w, "_dv_regions", lambda: [])
        assert w._dv_candidate_record("m")["channel_decisions"] == {"CD3": "tophat"}
    finally:
        w.close()


def test_the_frozen_session_names_the_versions_own_products(app, tmp_path, monkeypatch):
    """Finding 4: the session is saved before the new fused path reaches the
    window; the version's copy names its own products."""
    from block01.core import provenance as prov
    w, ws = _dv_window(app, tmp_path)
    try:
        step1 = os.path.join(ws, "step1")
        os.makedirs(step1)
        w.step0_output = dict(w.step0_output, step1_dir=step1)
        monkeypatch.setattr(prov, "register_corrected_channels", lambda *a, **k: None)

        def _save():
            with open(os.path.join(step1, "step1_session.json"), "w") as f:
                json.dump({"fusion_zarr_path": "/the/previous/fused.zarr"}, f)
        monkeypatch.setattr(w, "_save_step1_session", _save)
        alloc, _worker = _pending(w, ws, ["R1"], ["R1"])
        committed = w._dv_commit_pending()
        with open(os.path.join(alloc["path"], "step1_session.json")) as f:
            sess = json.load(f)
        assert sess["fusion_zarr_path"] == committed["regions"][0]["fused_zarr_path"]
        assert sess["corrected_zarr_path"] == "/x/corrected.zarr"
    finally:
        w.close()


class _FinishesAnyway(iso._FakeFusion):
    """Cancel arrives after the last stop check: the job completes."""

    def run(self):
        self._release.wait(10)
        self.finished.emit("/fused/anyway.zarr")


def test_a_cancel_before_the_completion_publishes_no_version(app, tmp_path, monkeypatch):
    """Finding 5."""
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, lambda *a, **k: None)
    w, ws = _dv_window(app, tmp_path)
    dv.commit_version(ws, dv.new_version_folder(ws), _record())         # v1
    worker = _FinishesAnyway()
    try:
        alloc, _jobs = _pending(w, ws, ["R1"], ["R1"])
        pending = w._dv_pending
        w._start_fusion_worker(worker, job_name="fusion", n_rows=1, n_cols=1)
        w._dv_pending = pending
        w._fusion_dialog.findChild(QtWidgets.QPushButton).click()        # Cancel
        a6._pump()
        assert w._fusion_stopping
        worker.release()
        worker.wait(5000)
        a6._pump()
        assert not os.path.exists(alloc["path"])
        assert [v["version"] for v in dv.list_versions(ws)] == ["v001"]
        assert dv.current_version(ws)["version"] == "v001"
        assert w.config.isEnabled()
    finally:
        worker.release()
        worker.wait(5000)
        a6._pump()
        w.close()


def test_step4_reads_the_corrected_product_an_earlier_run_recorded(tmp_path):
    """Finding 6: a run made before data versions read the product that is
    now v1's; a later version's product must not be used for it."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_quant_sources import build_project, _edit_json
    from block01.core import quant_sources as qs
    p = build_project(tmp_path)
    ws = p["ws"]
    old = p["zarr"]
    # v1 = the earlier workspace, its product in place; the run recorded it
    alloc = dv.new_version_folder(ws)
    shutil.copy2(p["cfg"], os.path.join(alloc["path"], "correction_config.json"))
    dv.commit_version(ws, alloc, dict(_record(), corrected={"path": old}))
    _edit_json(os.path.join(p["run_dir"], "segmentation_meta.json"),
               lambda d: d.setdefault("paths", {}).update(corrected_channels_zarr=old))
    # v2 made another product, and the handoff now names it
    newer = os.path.join(ws, "versions", "corrected", "c002", "corrected_channels.zarr")
    shutil.copytree(old, newer)
    handoff = os.path.join(ws, "step0", "step0_roi_result.json")
    _edit_json(handoff, lambda d: d.update(corrected_zarr_path=newer))
    job = qs.resolve_quant_job(p["run_dir"], open_slide=p["slide"])
    cd3 = next(c for c in job.channels if c.name == "CD3")
    assert os.path.abspath(cd3.path) == os.path.abspath(old)


# ── user ruling 2026-10-03 (finding 3, option a): the `draft` row ─────────

def _draft_saved(ctx):
    """Step0 saved after the current version, no Generate."""
    with open(os.path.join(ctx["step_dirs"]["step0"], "correction_config.json"), "w") as f:
        json.dump({"method_params": {"tophat_radius": 77, "cucim_sigma": 30},
                   "channel_decisions": {"CD3": "tophat", "CD8": "original"},
                   "channel_params": {}}, f)


def test_a_dirty_draft_is_its_own_row_first_and_preselected(page, tmp_path, slides,
                                                             monkeypatch):
    from block01.utils import workspace_session as wsess
    from block01.ui.step0.step0_page import DRAFT_TAG
    proj, made = _project(tmp_path, slides["a"])
    _v1, v2 = _two_versions(made[0])
    _workspace_as(made[0], v2)
    found = wsess.find_workspaces(proj, slides["a"])[1]
    assert [r[2] for r in page._workspace_rows(found)] == ["v002  (current)", "v001"]
    _draft_saved(made[0])
    rows = page._workspace_rows(found)
    assert [r[2] for r in rows] == [DRAFT_TAG, "v002  (current)", "v001"]
    monkeypatch.setattr(QtWidgets.QDialog, "exec_", lambda dlg: QtWidgets.QDialog.Accepted)
    assert _REAL_CHOOSE(page, rows)[2] == DRAFT_TAG


def test_the_draft_row_continues_the_draft(page, tmp_path, slides, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page, DRAFT_TAG
    proj, made = _project(tmp_path, slides["a"])
    _v1, v2 = _two_versions(made[0])
    _workspace_as(made[0], v2)
    _draft_saved(made[0])
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: next(r for r in rows if r[2] == DRAFT_TAG))
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda *a, **k: pytest.fail("the draft is not republished"))
    page._open_existing_workspace()
    assert page._tophat_slider.value() == 77
    sent = []
    page.step0_complete.connect(sent.append)
    page._announce_opened_workspace()
    assert sent[0]["data_version_loaded"] == ""
    assert dv.current_version(made[0]["roi_dir"])["version"] == "v002"


def test_choosing_the_current_version_over_a_draft_loads_the_version(
        page, tmp_path, slides, monkeypatch):
    """Reloading a version ends the dirty draft (§3.13)."""
    from block01.ui.step0.step0_page import Step0Page
    proj, made = _project(tmp_path, slides["a"])
    _v1, v2 = _two_versions(made[0])
    _workspace_as(made[0], v2)
    _draft_saved(made[0])
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: next(r for r in rows if r[1] and r[1]["version"] == "v002"))
    wrote = []
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda self, config, zarr_path, remap_config_path=None: (
                            wrote.append(zarr_path) or (config, [], [], {})))
    page._open_existing_workspace()
    assert page._tophat_slider.value() == 40 and wrote == [v2["corrected"]["path"]]
    sent = []
    page.step0_complete.connect(sent.append)
    page._announce_opened_workspace()
    assert sent[0]["data_version_loaded"] == "v002"


def test_step1_settings_saved_since_the_version_are_a_draft_too(page, tmp_path, slides,
                                                                 monkeypatch):
    from block01.utils import workspace_session as wsess
    from block01.ui.step0.step0_page import Step0Page, DRAFT_TAG
    proj, made = _project(tmp_path, slides["a"])
    _v1, v2 = _two_versions(made[0])
    _workspace_as(made[0], v2)
    step1 = os.path.join(made[0]["roi_dir"], "step1")
    os.makedirs(step1, exist_ok=True)
    with open(os.path.join(step1, "step1_fusion_settings.json"), "w") as f:
        json.dump({"hash": "saved-after-v002"}, f)
    found = wsess.find_workspaces(proj, slides["a"])[1]
    assert page._workspace_rows(found)[0][2] == DRAFT_TAG
    monkeypatch.setattr(Step0Page, "_choose_workspace",
                        lambda self, rows: next(r for r in rows if r[1] and r[1]["version"] == "v002"))
    monkeypatch.setattr(Step0Page, "_write_step0_handoff",
                        lambda *a, **k: pytest.fail("Step0 is the version's: not republished"))
    page._open_existing_workspace()
    sent = []
    page.step0_complete.connect(sent.append)
    page._announce_opened_workspace()
    assert sent[0]["data_version_loaded"] == "v002"            # Step1 comes back
