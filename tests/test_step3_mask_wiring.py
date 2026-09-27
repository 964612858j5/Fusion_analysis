"""Block 4c: Step3's mask row wired to the ROI workspace and the viewer.

The real window of `test_step3_viewer.py` (CPU picture over the synthetic
pyramid of `test_step1_viewer_mount.py`) with a synthetic ROI workspace
whose Step2 runs are written as Step2 writes them (`test_step3_masks.py`):

  * the choice on entry: the active run; from Step2's finished dialog, that
    run; a run chosen in the list; a new Step2 result while Step3 is on
    screen joins the list and the choice is kept -- and the masks are NOT
    set again;
  * classification reaches the buttons: nuclei-only -> only `Nucleus mask`;
    nuclear-guided -> both; a run whose ROI does not match -> neither, and
    the hint says why and to re-run Step2;
  * no runs -> the list is one disabled item and the hint says so; the CPU
    picture -> "Masks need the GPU display";
  * a dataset switch empties the list and drops the viewer's masks;
  * the panels' settings reach the mount, and a rebuilt mount's label
    binding gets them again (GPU; skipped without one);
  * the hint is printed once per change.

Synthetic projects in the test's temporary directory only.
"""

import importlib.util
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

import test_step1_viewer_mount as vm  # noqa: E402
import test_step3_viewer as sv  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_masks_helpers", os.path.join(os.path.dirname(__file__), "test_step3_masks.py"))
mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mh)

SHAPES = [(vm.SLIDE, vm.SLIDE), (vm.SLIDE // 4, vm.SLIDE // 4)]
BBOX = tuple(vm.ROI)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: None))


def _project(tmp_path, runs=(("seg_old", "cellpose_wholecell_fusion", "2026-09-26T08:00:00"),
                             ("seg_new", "cellpose_wholecell_fusion", "2026-09-26T09:00:00")),
             active=None, **kw):
    rdir = mh._workspace(tmp_path / "proj", bbox=BBOX)
    made = {}
    for run_id, method, created in runs:
        made[run_id] = mh._run(rdir, run_id, method, bbox=BBOX, shapes=SHAPES,
                               created_at=created, **kw)
    if active:
        mh._index(rdir, active_segmentation_run=active)
    return rdir, made


def _window(app, tmp_path, monkeypatch, rdir):
    rig = sv._rig(app, tmp_path, monkeypatch)
    rig.w.step0_output["roi_dir"] = rdir
    return rig


def _counting(mount):
    calls = []
    original = mount.set_mask_sources

    def counted(sources):
        calls.append(sources)
        return original(sources)
    mount.set_mask_sources = counted
    return calls


def _cell_path(w):
    sources = w._step3_mount.mask_sources() or {}
    return sources.get("cell").mask_path if sources.get("cell") else None


def test_entry_chooses_the_active_run_and_the_row_shows_it(app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, active="seg_old")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        assert bar.run_combo.count() == 2 and bar.run_combo.isEnabled()
        assert bar.current_run_dir() == os.path.realpath(runs["seg_old"].dir)
        assert "(active)" in bar.run_combo.currentText()
        assert bar.run_combo.itemText(0).startswith("cellpose_wholecell_fusion · 2026-09-26 09:00")
        assert _cell_path(w) == os.path.realpath(runs["seg_old"].mask)
        assert bar.buttons["cell"].isEnabled() and not bar.buttons["nucleus"].isEnabled()
        assert bar.hint.full_text() == "Masks need the GPU display"          # the CPU picture
    finally:
        w.close()


def test_the_dialogs_run_and_a_chosen_run_are_shown(app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, active="seg_old")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        w._go_to_step3(runs["seg_new"].dir)                    # Step2's finished dialog
        sv._pump(40)
        assert bar.current_run_dir() == os.path.realpath(runs["seg_new"].dir)
        assert _cell_path(w) == os.path.realpath(runs["seg_new"].mask)
        bar.run_chosen.emit(os.path.realpath(runs["seg_old"].dir))   # chosen in the list
        assert _cell_path(w) == os.path.realpath(runs["seg_old"].mask)
        # breadcrumb back into Step3: the choice is kept
        sv._enter(rig, 1)
        sv._enter(rig, 3)
        assert bar.current_run_dir() == os.path.realpath(runs["seg_old"].dir)
    finally:
        w.close()


def test_a_new_step2_result_joins_the_list_and_the_masks_are_not_set_again(
        app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, active="seg_new")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        calls = _counting(w._step3_mount)
        shown = _cell_path(w)
        newest = mh._run(rdir, "seg_newest", "cellpose_wholecell_fusion", bbox=BBOX,
                         shapes=SHAPES, created_at="2026-09-26T11:00:00")   # becomes active
        w._step2.segmentation_done.emit(newest.dir)
        assert bar.run_combo.count() == 3
        assert bar.current_run_dir() == os.path.realpath(runs["seg_new"].dir)  # kept
        assert calls == [] and _cell_path(w) == shown
        sv._enter(rig, 1)
        sv._enter(rig, 3)                                          # a plain re-entry too
        assert calls == []
        # ...but not on another page: the list waits for the next entry
        sv._enter(rig, 1)
        mh._run(rdir, "seg_later", "cellpose_wholecell_fusion", bbox=BBOX, shapes=SHAPES,
                created_at="2026-09-26T12:00:00")
        w._step2.segmentation_done.emit("")
        assert bar.run_combo.count() == 3
    finally:
        w.close()


@pytest.mark.parametrize("method,nuclei,cell,nucleus", [
    ("cellpose_nuclei_dapi", False, False, True),
    ("mesmer_nuclear_guided", True, True, True),
    ("stardist_nuclei_expansion", False, True, False),
])
def test_the_method_decides_which_buttons_work(app, tmp_path, monkeypatch, method, nuclei,
                                                cell, nucleus):
    rdir, _ = _project(tmp_path, runs=(("seg_x", method, "2026-09-26T08:00:00"),),
                       nuclei=nuclei)
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        assert bar.buttons["cell"].isEnabled() is cell
        assert bar.buttons["nucleus"].isEnabled() is nucleus
    finally:
        w.close()


def test_a_run_of_another_roi_disables_both_and_says_why(app, tmp_path, monkeypatch):
    rdir, _ = _project(tmp_path, runs=(("seg_x", "cellpose_wholecell_fusion",
                                        "2026-09-26T08:00:00"),), roi_name="ROI_9")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        assert not bar.buttons["cell"].isEnabled() and not bar.buttons["nucleus"].isEnabled()
        hint = bar.hint.full_text()
        assert hint.startswith("Mask not shown:") and "ROI_1" in hint and "re-run Step2" in hint
        assert w._step3_mount.mask_sources() is None
    finally:
        w.close()


def test_no_runs_and_a_dataset_switch(app, tmp_path, monkeypatch, capsys):
    rdir, _ = _project(tmp_path, runs=())
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        assert not bar.run_combo.isEnabled()
        assert bar.hint.full_text() == "No segmentation results for this ROI — run Step2"
        out = capsys.readouterr().out
        assert out.count("[Step3] mask: No segmentation results") == 1
        w._step3_update_mask_hint()
        w._step3_update_mask_hint()
        assert "[Step3] mask:" not in capsys.readouterr().out            # once per change
        # a run appears; then the dataset moves
        mh._run(rdir, "seg_x", "cellpose_wholecell_fusion", bbox=BBOX, shapes=SHAPES)
        w._step3_refresh_masks()
        assert bar.run_combo.isEnabled() and w._step3_mount.mask_sources()
        w._close_step3_viewer()
        assert not bar.run_combo.isEnabled() and w._step3_mount.mask_sources() is None
        assert not bar.buttons["cell"].isEnabled()
    finally:
        w.close()


def test_the_panels_reach_the_mount(app, tmp_path, monkeypatch):
    rdir, _ = _project(tmp_path)
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w, bar = rig.w, rig.w._step3_mask_bar
    try:
        sv._enter(rig, 3)
        panel = bar.buttons["cell"].panel
        panel.opacity.setValue(30)
        panel.fill.setChecked(True)
        bar.buttons["nucleus"].panel.show_box.setChecked(False)
        assert w._step3_mount.mask_styles() == {"cell": {"alpha": 0.3, "mode": "fill"},
                                                "nucleus": {"visible": False}}
    finally:
        w.close()


def test_a_rebuilt_mount_gets_the_settings_again(app, tmp_path, monkeypatch):
    """GPU: the label binding's ACTUAL style, before and after a dataset
    switch rebuilt the mount's backend."""
    from block01.ui import step1_viewer_mount as mount_module
    rdir, _ = _project(tmp_path)
    w = sv.ds._window(app, tmp_path)
    w.loader.filepath = "/x/c4.ome.tif"
    w._corrected_decisions = {}
    w._corrected_zarr_path = "/tmp/c4-none.zarr"
    w._active_roi = {"name": "ROI_1", "bbox_fullres": list(vm.ROI)}
    w.step0_output["channel_remap_config_hash"] = "rev-1"
    w.step0_output["roi_dir"] = rdir
    w._step1_context_ready = True
    original = mount_module.Step1WholeSlideMount.__init__

    def _init(self, window, host=None, parent=None, **kwargs):
        original(self, window, host=vm.Step1ViewerHost(stack_factory=vm._stack_factory([])),
                 parent=parent, **kwargs)
    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "__init__", _init)
    rig = sv.SimpleNamespace(w=w, raws={})
    try:
        sv._enter(rig, 3)
        mount = w._step3_mount
        if mount.label_binding is None:
            if os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") == "1":
                pytest.fail(f"GPU backend unavailable: {mount.gpu_status()['reason']}")
            pytest.skip("GPU backend unavailable")
        bar = w._step3_mask_bar
        bar.buttons["cell"].panel.width.setValue(3)
        bar.buttons["cell"].panel.set_colour((1.0, 1.0, 1.0))
        assert mount.label_binding.style("cell").width == 3.0
        w._close_step3_viewer()                         # the dataset moved
        sv._enter(rig, 1)
        sv._enter(rig, 3)
        style = w._step3_mount.label_binding.style("cell")
        assert style.width == 3.0 and style.color == (1.0, 1.0, 1.0)
        assert w._step3_mount.label_binding.mask_status()["cell"]["source"] is True
    finally:
        w.close()
