"""After a restart Step2 comes back with the parameters it had (user
2026-10-25): a draft that ran a saved parameter file is restored as that file
(source index); a manual draft stays manual; a missing file falls back to the
manual copy; with nothing saved, nothing is loaded."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _saved(tmp_path):
    from block01.utils.segmentation_params import save_segmentation_params
    cfg = {"method": "stardist_nuclei_expansion",
           "params": {"prob_thresh": 0.55, "nms_thresh": None, "expand_distance": 7.0,
                      "model_name": "2D_versatile_fluo"}}
    path, _ = save_segmentation_params(str(tmp_path / "settings"), cfg)
    return path


def test_an_index_draft_comes_back_as_its_file(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    path = _saved(tmp_path)
    first = Step2Page()
    assert first._restore_index_file(path, "stardist_nuclei_expansion")
    draft = {"segmentation_config": first.get_seg_config(), "param_file": path,     # as Step2 saves it
             "parameter_source": "index"}
    p = Step2Page()
    assert p.apply_draft(draft) is True
    assert p._parameter_source == "index"
    assert os.path.abspath(p._seg_param_file) == os.path.abspath(path)
    assert p._method_combo.currentData() == "stardist_nuclei_expansion"


def test_a_missing_file_falls_back_to_the_manual_copy(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    p = Step2Page()
    draft = {"segmentation_config": {"method": "stardist_nuclei_expansion"},
             "param_file": str(tmp_path / "gone.json"), "parameter_source": "index"}
    assert p.apply_draft(draft) is True
    assert p._parameter_source == "manual"


def test_a_manual_draft_stays_manual(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    path = _saved(tmp_path)
    p = Step2Page()
    draft = {"segmentation_config": {"method": "stardist_nuclei_expansion"}, "param_file": path,
             "parameter_source": "manual"}
    assert p.apply_draft(draft) is True
    assert p._parameter_source == "manual"


def test_nothing_saved_loads_nothing(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    p = Step2Page()
    assert p._resolve_seg_params_index_path(str(tmp_path)) == ""     # the entry guard: no index, no load


def test_an_index_draft_edited_after_choosing_comes_back_as_edited(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    path = _saved(tmp_path)
    p = Step2Page()
    assert p._restore_index_file(path, "stardist_nuclei_expansion")
    edited = p.get_seg_config()
    edited["params"] = dict(edited.get("params") or {}, prob_thresh=0.65)      # changed by hand
    q = Step2Page()
    assert q.apply_draft({"segmentation_config": edited, "param_file": path, "parameter_source": "index"})
    assert q._parameter_source == "manual"
    assert (q.get_seg_config().get("params") or {}).get("prob_thresh") == 0.65


def test_another_workspaces_index_is_cleared(app, tmp_path):
    from block01.ui.step2_page import Step2Page
    path = _saved(tmp_path)
    p = Step2Page()
    assert p._restore_index_file(path, "stardist_nuclei_expansion")
    p.clear_params_index()
    assert p._seg_params_edit.text() == "" and p._parameter_source == "manual" and not p._seg_params_index


def _window(tmp_path, monkeypatch, roi):
    import types
    from block01.ui.main_window import MainWindow
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    w = types.SimpleNamespace(_step2=page, _rm_roi_dir=lambda: roi)
    return w, page, (lambda s0, s1: MainWindow._step2_workspace_params(w, s0, s1))


def _workspace(tmp_path, name, with_params=True):
    from block01.utils.segmentation_params import save_segmentation_params
    roi = tmp_path / name
    (roi / "settings").mkdir(parents=True)
    if with_params:
        save_segmentation_params(str(roi / "settings"), {"method": "stardist_nuclei_expansion",
                                                        "params": {"prob_thresh": 0.5, "nms_thresh": None,
                                                                   "expand_distance": 5.0,
                                                                   "model_name": "2D_versatile_fluo"}})
    return str(roi)


def test_after_save_as_step2_takes_the_new_workspaces_params(app, tmp_path, monkeypatch):
    a = _workspace(tmp_path, "A")
    b = _workspace(tmp_path, "B")
    w, page, go = _window(tmp_path, monkeypatch, b)
    page.load_step1_active_params(os.path.join(a, "settings"))          # A's, from before Save as
    s0, s1 = {"roi_id": "B"}, {"roi_id": "A"}                            # step1_output still A's
    assert go(s0, s1) is True
    assert os.path.realpath(page._seg_params_edit.text()).startswith(os.path.realpath(b))


def test_after_save_as_into_a_workspace_without_params_the_old_index_goes(app, tmp_path, monkeypatch):
    a = _workspace(tmp_path, "A")
    b = _workspace(tmp_path, "B", with_params=False)
    w, page, go = _window(tmp_path, monkeypatch, b)
    page.load_step1_active_params(os.path.join(a, "settings"))
    go({"roi_id": "B"}, {"roi_id": "A"})
    assert page._seg_params_edit.text() == "" and page._parameter_source == "manual"


def test_the_same_workspace_keeps_what_the_page_holds(app, tmp_path, monkeypatch):
    a = _workspace(tmp_path, "A")
    w, page, go = _window(tmp_path, monkeypatch, a)
    page.load_step1_active_params(os.path.join(a, "settings"))
    held = page._seg_params_edit.text()
    assert go({"roi_id": "A"}, {}) is False and page._seg_params_edit.text() == held


def test_during_a_run_the_new_workspaces_params_wait_for_the_switch(app, tmp_path, monkeypatch):
    """Save as while a segmentation runs (A6 §12.12): Step2 stays on the run's
    workspace and keeps its parameters; the new workspace's come with the
    switch when the run ends."""
    import types
    from block01.ui.main_window import MainWindow
    a = _workspace(tmp_path, "A")
    b = _workspace(tmp_path, "B")
    w, page, _ = _window(tmp_path, monkeypatch, b)
    w.step0_output, w.step1_output = {"roi_id": "B"}, {"roi_id": "A"}
    w._step2_workspace_params = types.MethodType(MainWindow._step2_workspace_params, w)
    page.load_step1_active_params(os.path.join(a, "settings"))
    held = page._seg_params_edit.text()
    page._run_active = True
    page._run_progress = {"token": {"roi_id": "A", "roi_dir": a}}
    page.set_roi_context(roi_id="B", roi_dir=b)                        # as _go_to_step2 does
    assert w._step2_workspace_params(w.step0_output, w.step1_output) is False
    assert page._seg_params_edit.text() == held and page._roi_id != "B"
    page._run_active = False
    page._apply_pending_context()                                      # the run ended
    assert page._roi_id == "B"
    assert os.path.realpath(page._seg_params_edit.text()).startswith(os.path.realpath(b))
    assert page._after_pending_context is None
