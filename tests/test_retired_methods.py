"""HQ / HQ2 / CSD do not appear in Block01 (user ruling 2026-10-31,
docs/v17_review_fixes_plan.md §1):

  * neither Step2's nor the legacy Step1 panel's method box lists them;
  * a parameter file, an index, a draft naming one is refused and changes
    nothing on screen; any other unlisted method is refused the same way;
  * a Step1 session naming one restores without its segmentation parameters
    and says so once; Step2's Run refuses one.

Synthetic folders in the test's temporary directory only.
"""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from block01.utils import segmentation_config as sc  # noqa: E402

RETIRED = list(sc.RETIRED_METHODS)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def warnings(monkeypatch):
    shown = []
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else "")))
    return shown


def _listed(combo):
    return [combo.itemData(i) for i in range(combo.count())]


def test_the_registry_keeps_them_but_no_box_lists_them(app):
    from block01.ui.step0.search_ctrl import SearchCtrlPanel
    from block01.ui.step2_page import Step2Page
    assert all(m in sc.SEGMENTATION_METHODS for m in RETIRED)
    assert not set(RETIRED) & set(sc.available_segmentation_methods())
    assert not set(RETIRED) & set(_listed(Step2Page()._method_combo))
    assert not set(RETIRED) & set(_listed(SearchCtrlPanel()._method_combo))


@pytest.mark.parametrize("method", RETIRED + ["no_such_method"])
def test_step2_refuses_a_parameter_file_and_a_draft(app, tmp_path, warnings, method):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    before = page._method_combo.currentData()
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"method": method, "params": {}}))
    if method == "no_such_method":
        # an unknown name is refused by the normaliser itself (plan §6b)
        assert page._load_seg_params_file(str(path), silent=False) is False
    else:
        assert page._load_seg_params_file(str(path), silent=False) is False
        assert any("no longer available" in w for w in warnings)
    assert page._method_combo.currentData() == before and page._seg_param_file == ""
    warnings.clear()
    assert page.apply_draft({"segmentation_config": {"method": method}}) is False
    assert page._method_combo.currentData() == before and len(warnings) == 1


def test_step2_index_skips_retired_methods(app, tmp_path, warnings):
    from block01.ui.step2_page import Step2Page
    folder = tmp_path / "segmentation_params"
    folder.mkdir()
    index = folder / "segmentation_params_index.json"
    index.write_text(json.dumps({
        "active_method": RETIRED[0],
        "methods": {RETIRED[0]: {"latest": "hq.json", "history": ["hq.json"]},
                    sc.STARDIST_NUCLEI_EXPANSION: {"latest": "sd.json", "history": ["sd.json"]}}}))
    page = Step2Page()
    assert page._load_seg_params_index(str(index), silent=False) is True
    assert _listed(page._index_method_combo) == [sc.STARDIST_NUCLEI_EXPANSION]
    index.write_text(json.dumps({"active_method": RETIRED[1],
                                 "methods": {RETIRED[1]: {"latest": "x.json"}}}))
    assert page._load_seg_params_index(str(index), silent=False) is False
    assert "no longer available" in warnings[-1]
    warnings.clear()
    page.set_segmentation_params_path(str(index), load=True)     # browsing to it
    assert len(warnings) == 1 and "no longer available" in warnings[0]
    warnings.clear()
    assert page._load_seg_params_index(str(index), silent=True) is False   # quiet path
    assert len(warnings) == 1, "a retired method is said even on a quiet path"


def test_step2_run_refuses_a_retired_method(app, tmp_path, warnings, monkeypatch):
    from block01.ui.step2_page import Step2Page
    page = Step2Page()
    zarr = tmp_path / "fused.zarr"
    zarr.mkdir()
    page._zarr_path = str(zarr)
    page._roi_dir = ""
    monkeypatch.setattr(page, "_stardist_grid_ok_for_run", lambda: True)
    monkeypatch.setattr(page, "get_seg_config", lambda: {"method": RETIRED[2]})
    started = []
    monkeypatch.setattr(page, "_check_preseg_contract", lambda cfg: started.append(cfg) or False)
    page._run()
    assert warnings and "no longer available" in warnings[-1] and not started


def test_the_legacy_panel_refuses_before_changing_anything(app):
    from block01.ui.step0.search_ctrl import SearchCtrlPanel
    panel = SearchCtrlPanel()
    before = (panel._method_combo.currentData(), panel._man_diam.value())
    with pytest.raises(ValueError, match="no longer available"):
        panel.apply_seg_config_to_ui({"method": RETIRED[0], "diameter": 33})
    assert (panel._method_combo.currentData(), panel._man_diam.value()) == before


def test_a_step1_session_restores_without_retired_parameters(app, warnings, monkeypatch):
    from block01.ui import main_window as mw
    from block01.ui.step0.search_ctrl import SearchCtrlPanel

    class Fake:
        loader = None
        step0_output = {}

        def __init__(self):
            self.search = SearchCtrlPanel()
        _step1_session_without_unoffered_method = \
            mw.MainWindow._step1_session_without_unoffered_method

        def _refuse_session(self, msg):
            raise AssertionError(msg)

        def _load_step0_roi_result(self, auto=False):
            self.step0_output = {"source_identity": "s", "output_dir": "/o"}
            return True

        def _apply_step1_session_fields(self, sess, *a, **k):
            self.applied = sess
            return True

        def _rm_restore_preseg(self, preseg):
            pass

    fake = Fake()
    sess = {"source_identity": "s", "channel_weights": {"DAPI": 1.0},
            "p2_params": {"method": RETIRED[1], "diameter": 20},
            "active_segmentation_method": RETIRED[1]}
    assert mw.MainWindow._restore_step1_session_v2(fake, sess, "/o/step0_roi_result.json") is True
    assert "p2_params" not in fake.applied and "active_segmentation_method" not in fake.applied
    assert fake.applied["channel_weights"] == {"DAPI": 1.0}
    assert len(warnings) == 1 and "no longer available" in warnings[0]
