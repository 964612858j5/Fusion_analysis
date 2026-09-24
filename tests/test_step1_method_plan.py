"""The Methods part of the Pre-segmentation tab (plan block B).

The parameter table (plan 7.1): list parameters, ranges, precision, `auto`;
combination counts and the task total; the `+` dialog (eight methods, Mesmer
disabled without DeepCell, fields checked as typed); blocks with Edit and ×;
same-name merge by R9 as revised 2026-09-24 (union, single-value conflicts
decided, No keeps a block of its own); Edit may change the method; plans keep
every patch and the ticks, and loading one can bring the patches back --
alone, replacing the current ones, or beside them; nothing is computed.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.utils import segmentation_param_schema as ps  # noqa: E402

CP = "cellpose_wholecell_fusion"
CPX = "cellpose_nuclei_expansion"
SDX = "stardist_nuclei_expansion"
MW = "mesmer_whole_cell"


def _spec(method, key):
    return next(s for s in ps.specs(method) if s.key == key)


# ── the parameter table ─────────────────────────────────────────────────────
def test_the_ui_lists_the_eight_methods_and_hides_hq():
    assert len(ps.UI_METHODS) == 8
    assert not set(ps.HIDDEN_METHODS) & set(ps.UI_METHODS)
    for m in ps.UI_METHODS:                       # every default is a valid value
        for s in ps.specs(m):
            ps.parse_values(s, ps.format_values(s, [s.default]))


def test_list_parameters_take_lists_and_the_rest_one_value():
    assert ps.parse_values(_spec(CP, "flow_threshold"), "0.2, 0.4") == ([0.2, 0.4], "")
    assert ps.parse_values(_spec(CP, "diameter"), "auto, 30") == ([None, 30.0], "")
    assert ps.parse_values(_spec(CP, "diameter"), "0") == ([None], "")      # 0 = auto
    assert ps.parse_values(_spec(SDX, "expand_distance"), "4, 8")[0] == [4.0, 8.0]   # R3
    assert ps.parse_values(_spec(MW, "maxima_threshold"), "0.05, 0.075")[0] == [0.05, 0.075]
    for key, method in (("min_size", CP), ("expand_distance", CPX), ("model_name", SDX)):
        with pytest.raises(ps.ParamError, match="one value only"):
            ps.parse_values(_spec(method, key), "1, 2")


@pytest.mark.parametrize("text,why", [
    ("", "enter a value"), ("0.2,,0.4", "empty value"), ("x", "not a number"),
    ("3.5", "outside"), ("0.375", "more than 2 decimals"), ("auto", "not allowed")])
def test_a_bad_value_is_refused_with_the_reason(text, why):
    with pytest.raises(ps.ParamError, match=why):
        ps.parse_values(_spec(CP, "flow_threshold"), text)


def test_a_repeated_value_is_dropped_and_said():
    vals, note = ps.parse_values(_spec(CP, "flow_threshold"), "0.4, 0.2, 0.4")
    assert vals == [0.4, 0.2] and note == "1 repeated value dropped"


def test_combinations_are_the_cartesian_product_with_stable_ids():
    v = ps.default_values(CP)
    v.update(flow_threshold=[0.2, 0.4], cellprob_threshold=[-1.0, 0.0, 0.5])
    combos = ps.combinations(CP, v)
    assert ps.combo_count(CP, v) == len(combos) == 6
    ids = [ps.combo_id(CP, c) for c in combos]
    assert len(set(ids)) == 6
    assert ids == [ps.combo_id(CP, c) for c in ps.combinations(CP, v)]


def test_same_name_merge_is_the_union_and_names_single_value_conflicts():
    old = ps.default_values(CPX)
    old.update(flow_threshold=[0.2, 0.4], expand_distance=[8.0])
    new = ps.default_values(CPX)
    new.update(flow_threshold=[0.4, 0.6], expand_distance=[5.0])
    merged, conflicts = ps.merge(CPX, old, new)
    assert merged["flow_threshold"] == [0.2, 0.4, 0.6]
    assert conflicts == {"expand_distance": (8.0, 5.0)}


# ── the dialog ──────────────────────────────────────────────────────────────
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _save_enabled(dlg):
    return dlg.buttons.button(QtWidgets.QDialogButtonBox.Save).isEnabled()


def test_the_dialog_offers_eight_methods_and_greys_mesmer_without_deepcell(app):
    from block01.ui.step1_presegmentation.method_editor import MethodEditorDialog
    dlg = MethodEditorDialog(mesmer_available=False)
    model = dlg.method_combo.model()
    methods = [dlg.method_combo.itemData(i) for i in range(dlg.method_combo.count())]
    assert methods == ps.UI_METHODS
    for i, m in enumerate(methods):
        assert model.item(i).isEnabled() == (m not in ps.MESMER_METHODS)
    assert "DeepCell is not installed" in dlg.method_combo.itemText(methods.index(MW))
    dlg2 = MethodEditorDialog(mesmer_available=True)
    assert all(dlg2.method_combo.model().item(i).isEnabled() for i in range(8))


def test_the_dialog_checks_each_field_and_counts_combinations(app):
    from block01.ui.step1_presegmentation.method_editor import MethodEditorDialog
    dlg = MethodEditorDialog(method=CP, mesmer_available=True)
    assert _save_enabled(dlg) and dlg.count_label.text() == "1 combination"
    dlg.field("flow_threshold").setText("0.2, 0.4")
    dlg.field("cellprob_threshold").setText("-1, 0, 0.5")
    assert dlg.count_label.text() == "6 combinations"
    dlg.field("flow_threshold").setText("0.2, 9")
    assert not _save_enabled(dlg) and "outside" in dlg.field_error("flow_threshold")
    dlg.field("flow_threshold").setText("0.2, 0.2")               # a tidy-up, not a refusal
    assert _save_enabled(dlg) and dlg.field_error("flow_threshold") == ""
    assert dlg.field_note("flow_threshold") == "1 repeated value dropped"
    assert dlg.values()["flow_threshold"] == [0.2]


# ── blocks and merging ──────────────────────────────────────────────────────
class _Editor:
    """Stands in for the dialog: accepts with preset method and values."""
    queue = []

    def __init__(self, parent=None, method=None, values=None, lock_method=False):
        self._method, self._values = self.queue.pop(0)
        self.opened_with = (method, values, lock_method)
        _Editor.last = self

    def exec_(self):
        return QtWidgets.QDialog.Accepted

    def method(self):
        return self._method

    def values(self):
        return self._values


def _panel(app, monkeypatch):
    from block01.ui.step1_presegmentation import method_blocks as mb
    panel = mb.MethodsPanel()
    monkeypatch.setattr(mb.MethodsPanel, "editor_class", _Editor)
    return panel, mb


def _vals(method, **kw):
    v = ps.default_values(method)
    v.update(kw)
    return v


def test_add_edit_remove_and_the_task_total(app, monkeypatch):
    panel, _ = _panel(app, monkeypatch)
    _Editor.queue = [(CP, _vals(CP, flow_threshold=[0.2, 0.4])),
                     (SDX, _vals(SDX, prob_thresh=[None, 0.5]))]
    panel.add_method()
    panel.add_method()
    assert [b.method for b in panel.blocks()] == [CP, SDX]
    assert panel.block(CP).count.text() == "2 combinations"
    assert "flow 0.2, 0.4" in panel.block(CP).summary.text()
    panel.set_patch_count(3)
    assert panel.total.text() == "Total: 3 patches × 4 combinations = 12 tasks"
    _Editor.queue = [(CP, _vals(CP, flow_threshold=[0.2, 0.4, 0.6]))]
    panel.block(CP).btn_edit.click()
    assert panel.block(CP).count.text() == "3 combinations"
    # the dialog opened on this block's method and values, method unlocked
    assert _Editor.last.opened_with[0] == CP and _Editor.last.opened_with[2] is False
    panel.block(SDX).btn_remove.click()
    assert [b.method for b in panel.blocks()] == [CP]
    assert panel.total.text() == "Total: 3 patches × 3 combinations = 9 tasks"


def test_a_same_name_method_merges_on_yes_and_is_kept_apart_on_no(app, monkeypatch):
    panel, mb = _panel(app, monkeypatch)
    first = panel.adopt(CPX, _vals(CPX, flow_threshold=[0.2], expand_distance=[8.0]))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No))
    second = panel.adopt(CPX, _vals(CPX, flow_threshold=[0.4]))
    # R9 as revised: No keeps the new values as a block of their own.
    assert [b.method for b in panel.blocks()] == [CPX, CPX]
    assert second is not first and second.values["flow_threshold"] == [0.4]
    assert first.values["flow_threshold"] == [0.2]
    panel._remove(second.uid)

    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Yes))

    class _Pick:
        def __init__(self, method, conflicts, parent=None):
            self.conflicts = conflicts

        def exec_(self):
            return QtWidgets.QDialog.Accepted

        def chosen(self):
            return {k: new for k, (old, new) in self.conflicts.items()}
    monkeypatch.setattr(mb.MethodsPanel, "conflict_class", _Pick)
    assert panel.adopt(CPX, _vals(CPX, flow_threshold=[0.4, 0.2], expand_distance=[5.0]))
    assert panel.block(CPX).values["flow_threshold"] == [0.2, 0.4]    # union
    assert panel.block(CPX).values["expand_distance"] == [5.0]        # the choice


def test_edit_can_change_the_method_and_asks_to_merge_onto_an_existing_one(app, monkeypatch):
    panel, _ = _panel(app, monkeypatch)
    a = panel.adopt(CP, _vals(CP))
    b = panel.adopt(SDX, _vals(SDX))
    _Editor.queue = [(CPX, _vals(CPX, flow_threshold=[0.3]))]         # CP -> CPX
    a.btn_edit.click()
    assert a.method == CPX and a.title.text() == ps.display_name(CPX)
    assert a.values["flow_threshold"] == [0.3]
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a_, **k: QtWidgets.QMessageBox.Yes))
    _Editor.queue = [(CPX, _vals(CPX, flow_threshold=[0.5], expand_distance=[8.0]))]
    b.btn_edit.click()                                                 # SDX -> CPX: merge
    assert [x.method for x in panel.blocks()] == [CPX]
    assert panel.blocks()[0].values["flow_threshold"] == [0.3, 0.5]


def test_the_conflict_dialog_lists_every_conflict(app):
    from block01.ui.step1_presegmentation.method_blocks import ConflictDialog
    dlg = ConflictDialog(SDX, {"model_name": ("2D_versatile_fluo", "2D_paper_dsb2018")})
    assert dlg.chosen() == {"model_name": "2D_versatile_fluo"}         # current by default
    dlg.choice("model_name").setCurrentIndex(1)
    assert dlg.chosen() == {"model_name": "2D_paper_dsb2018"}


# ── plans ───────────────────────────────────────────────────────────────────
def test_a_plan_keeps_every_patch_and_the_ticks(tmp_path):
    from block01.ui.step1_presegmentation import plan_store
    methods = [{"method": CP, "values": _vals(CP, flow_threshold=[0.2, 0.4])}]
    patches = [{"id": 1, "name": "P1", "bbox": [0, 16, 0, 16]},
               {"id": 2, "name": "P2", "bbox": [16, 32, 0, 16]},
               {"id": 3, "name": "edge", "bbox": [16, 32, 16, 32]}]
    path = plan_store.save_plan(str(tmp_path), methods, patches, [1, 3], {"raw_ome_path": "/x"})
    assert os.path.dirname(path) == plan_store.plans_dir(str(tmp_path))
    entries = plan_store.list_plans(str(tmp_path))
    assert entries[0]["n_methods"] == 1 and entries[0]["n_patches"] == 3
    plan = plan_store.load_plan(str(tmp_path), entries[0]["file"])
    assert plan["methods"] == json.loads(json.dumps(methods))
    assert plan["patches"] == patches and plan["selected_ids"] == [1, 3]
    assert plan_store.resolve_patches(plan, [1, 4]) == ([1], 1)
    assert not os.path.exists(os.path.join(str(tmp_path), "segmentation_params"))


# ── in the application ──────────────────────────────────────────────────────
def _window(app, tmp_path):
    from block01.ui.main_window import MainWindow
    from block01.ui.step0.roi_context_model import Patch
    w = MainWindow()
    w.step0_output = {"step1_dir": str(tmp_path / "step1")}
    w._on_patches([Patch((0, 16, 0, 16), 1), Patch((16, 32, 16, 32), 2),
                   Patch((32, 48, 32, 48), 3)])
    return w


def test_the_methods_part_sits_under_the_patches_and_counts_the_ticked(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        lay = w.method_params_tab.layout()
        order = [lay.itemAt(i).widget() for i in range(lay.count())]
        assert order[:3] == [w._preseg_patches, w._preseg_methods,
                             w._step1_method_params_scroll]
        w._preseg_methods.adopt(CP, _vals(CP, flow_threshold=[0.2, 0.4]))
        assert w._preseg_methods.total.text() == "Total: 3 patches × 2 combinations = 6 tasks"
        w._preseg_patches.tiles()[0].set_checked(False)
        assert w._preseg_methods.total.text() == "Total: 2 patches × 2 combinations = 4 tasks"
        assert getattr(w, "cellpose_process", None) is None          # nothing ran
    finally:
        w.close()


def _published_window(app, tmp_path, patches):
    """MainWindow on a Step0 page with a published handoff, as a real session:
    patch edits go through Step0 and come back to Step1 when published."""
    from block01.ui.main_window import MainWindow
    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)

    class _Loader:
        shape = (128, 128)
        ch_map = {"DAPI": 0, "CD3": 1}
        filepath = str(raw)

        def channel_names(self):
            return ["DAPI", "CD3"]

        def read_region(self, channel, y0, y1, x0, x1, downsample=1):
            ds = max(1, int(downsample))
            return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)

    roi = {"name": "ROI_1", "bbox_fullres": [0, 128, 0, 128],
           "polygon_fullres": [[0, 0], [128, 0], [128, 128], [0, 128]]}
    w = MainWindow()
    page = w._step0
    page.loader = _Loader()
    page.ome_path, page.output_dir = str(raw), str(tmp_path)
    page.panel_csv_path, page.panel_groups, page.nucleus_channel = "", {}, "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 128, 128
    page.overview.full_wsi_mode = False
    page.overview.set_rois_and_patches([roi], patches)
    page._on_patches_changed(page.overview._patch_coords())
    step0_dir = tmp_path / "roi1" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    page._roi_context = {
        "roi_id": "roi1", "roi_dir": str(tmp_path / "roi1"), "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir), "step1": str(tmp_path / "roi1" / "step1"),
                      "step2": str(tmp_path / "roi1" / "step2")}}
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    page._roi_model.adopt(rois=[roi], patches=patches, full_wsi_mode=False,
                          loader=page.loader, nucleus_channel="DAPI")
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30}, "channel_decisions": {}},
        str(step0_dir / "corrected_channels.zarr"))
    w.loader = _Loader()
    w.step0_output = {"step0_manifest_path": str(step0_dir / "step0_roi_result.json"),
                      "step0_dir": str(step0_dir), "step1_dir": str(tmp_path / "roi1" / "step1"),
                      "output_dir": str(step0_dir)}
    w.step0_done = w._step1_context_ready = True
    w._current_step = 1
    w._rois = [roi]
    w._active_roi = roi
    w._on_patches(list(patches))
    return w


def _settle(w, timeout=20.0):
    import time
    deadline = time.monotonic() + timeout
    worker = getattr(w._step0, "_geometry_persist_worker", None)
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if worker is None or not worker.is_busy():
            break
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    return w._step0.geometry_persist_state()


def _state(w):
    return [(p.id, p.name, tuple(p)) for p in w._all_patches]


def _load(w, monkeypatch, restore, mode=None):
    told = []
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem",
                        staticmethod(lambda *a, **k: (a[3][0], True)))
    answers = {"Replace the methods in the plan now?": QtWidgets.QMessageBox.Yes}
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: answers.get(a[2], QtWidgets.QMessageBox.Yes if restore
                                                  else QtWidgets.QMessageBox.No)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a[2])))
    monkeypatch.setattr(type(w), "_choose_patch_restore_mode", lambda self, n: mode)
    assert w._on_load_preseg_plan() is True
    _settle(w)
    return told


def _saved_plan(app, tmp_path):
    """A plan saved over P1, P2, P3='edge' with P1 and P3 ticked."""
    from block01.ui.step0.roi_context_model import Patch
    w = _published_window(app, tmp_path, [Patch((0, 16, 0, 16), 1), Patch((16, 32, 16, 32), 2),
                                          Patch((32, 48, 32, 48), 3, "edge")])
    w._preseg_methods.adopt(SDX, _vals(SDX, prob_thresh=[0.4, 0.6]))
    w._preseg_patches.tiles()[1].set_checked(False)
    path = w._on_save_preseg_plan()
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    assert [(p["id"], p["name"], p["bbox"]) for p in saved["patches"]] == [
        (1, "P1", [0, 16, 0, 16]), (2, "P2", [16, 32, 16, 32]), (3, "edge", [32, 48, 32, 48])]
    assert saved["selected_ids"] == [1, 3]
    return w


def test_loading_with_no_patch_restores_the_plans_patches_and_ticks(app, tmp_path, monkeypatch):
    w = _saved_plan(app, tmp_path)
    try:
        w._step0.delete_patches([1, 2, 3])
        assert _settle(w) == "published" and w._all_patches == []
        w._preseg_methods.set_methods([])
        _load(w, monkeypatch, restore=True)
        assert _state(w) == [(1, "P1", (0, 16, 0, 16)), (2, "P2", (16, 32, 16, 32)),
                             (3, "edge", (32, 48, 32, 48))]
        assert w._preseg_patches.selected_ids() == [1, 3]
        assert [b.method for b in w._preseg_methods.blocks()] == [SDX]
    finally:
        w.close()


def test_replace_puts_back_exactly_the_plans_patches(app, tmp_path, monkeypatch):
    w = _saved_plan(app, tmp_path)
    try:
        w._step0.delete_patches([2])
        _settle(w)
        w._step0.add_patches([(64, 80, 64, 80)])                    # P4, not in the plan
        _settle(w)
        _load(w, monkeypatch, restore=True, mode="replace")
        assert _state(w) == [(1, "P1", (0, 16, 0, 16)), (2, "P2", (16, 32, 16, 32)),
                             (3, "edge", (32, 48, 32, 48))]
        assert w._preseg_patches.selected_ids() == [1, 3]
    finally:
        w.close()


def test_keep_both_adds_what_is_missing_and_keeps_names_unique(app, tmp_path, monkeypatch):
    w = _saved_plan(app, tmp_path)
    try:
        w._step0.delete_patches([2, 3])                              # P2 and 'edge' gone
        _settle(w)
        w._step0.add_patches([(64, 80, 64, 80)])                    # P4 ...
        _settle(w)
        w._step0.rename_patch(4, "edge")                            # ... now named 'edge'
        _settle(w)
        told = _load(w, monkeypatch, restore=True, mode="both")
        assert _state(w) == [(1, "P1", (0, 16, 0, 16)), (4, "edge", (64, 80, 64, 80)),
                             (2, "P2", (16, 32, 16, 32)), (3, "P3", (32, 48, 32, 48))]
        assert "P3 → P3" in told[0]                                  # the renamed one is said
        assert w._preseg_patches.selected_ids() == [1, 3]
    finally:
        w.close()


def test_not_restoring_ticks_only_what_exists(app, tmp_path, monkeypatch):
    w = _saved_plan(app, tmp_path)
    try:
        w._step0.delete_patches([3])
        _settle(w)
        told = _load(w, monkeypatch, restore=False)
        assert [p.id for p in w._all_patches] == [1, 2]
        assert w._preseg_patches.selected_ids() == [1]
        assert "1 of the plan's ticked patches do not exist" in told[0]
    finally:
        w.close()


def test_the_methods_part_does_not_widen_the_channel_column(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        for m in (CP, SDX, CPX):
            w._preseg_methods.adopt(m, _vals(m))
        tabs = w.method_params_tab.parentWidget().parentWidget()
        QtWidgets.QApplication.processEvents()
        shown = tabs.minimumSizeHint().width()
        w._preseg_methods.hide()
        QtWidgets.QApplication.processEvents()
        assert tabs.minimumSizeHint().width() == shown
    finally:
        w.close()
