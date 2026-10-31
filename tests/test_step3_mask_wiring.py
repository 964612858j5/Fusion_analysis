"""Step3's results window wired to the ROI workspace and the viewer (block
4c; several results at once, docs/v17_step3_multi_results_plan.md §3).

The real window of `test_step3_viewer.py` (CPU picture over the synthetic
pyramid of `test_step1_viewer_mount.py`) with a synthetic ROI workspace
whose Step2 runs are written as Step2 writes them (`test_step3_masks.py`):

  * on entry: the active record is `In use` and its cells are ticked (the
    default, once); from Step2's finished dialog, that run; `Use` changes
    the active record and no tick; a new Step2 result while Step3 is on
    screen joins the list, the choice kept -- and the masks are NOT set
    again;
  * classification reaches the ticks: nuclei-only -> only `Nuclei`;
    nuclear-guided -> both; a run of another ROI is drawn on its region;
  * several ticked records are drawn at once, the top row on top, cells
    before nuclei; a row's panel reaches its slot;
  * `Load…` adds a run with its cells ticked and does not make it active;
  * no runs -> the hint says so; the CPU picture -> "Masks need the GPU
    display"; a dataset switch empties the list and drops the masks;
  * a rebuilt mount's label binding gets the row's settings again (GPU;
    skipped without one);
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
    original = mount.set_mask_slots

    def counted(slots):
        calls.append(slots)
        return original(slots)
    mount.set_mask_slots = counted
    return calls


def _drawn(w):
    """[(entry run folder, kind, mask path)] drawn, in drawing order."""
    out = []
    for key, src, _style in w._step3_mount.mask_slots() or []:
        entry, _sep, kind = key.partition("\x1e")
        out.append((entry.partition("\x1f")[0], kind, src.mask_path))
    return out


def _cell_path(w):
    cells = [path for _run, kind, path in _drawn(w) if kind == "cell"]
    return cells[-1] if cells else None


def _key(w, run):
    real = os.path.realpath(run.dir)
    return next(k for k in w._step3_results.keys() if k.partition("\x1f")[0] == real)


def test_entry_uses_the_active_run_and_ticks_its_cells(app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, active="seg_old")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        sv._enter(rig, 3)
        assert len(win.keys()) == 2
        old, new = win.row(_key(w, runs["seg_old"])), win.row(_key(w, runs["seg_new"]))
        assert old.btn_use.text() == "In use" and new.btn_use.text() == "Use"
        assert "(active)" in old.title.text()
        assert win.row(win.keys()[0]).title.text().startswith("cellpose_wholecell_fusion · 2026-09-26 09:00")
        assert win.ticked() == [(_key(w, runs["seg_old"]), "cell")]
        assert _cell_path(w) == os.path.realpath(runs["seg_old"].mask)
        assert old.ticks["cell"].isEnabled() and not old.ticks["nucleus"].isEnabled()
        assert win.hint.full_text() == "Masks need the GPU display"          # the CPU picture
    finally:
        w.close()


def test_use_changes_the_active_record_and_no_tick(app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, active="seg_old")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        w._go_to_step3(runs["seg_new"].dir)                    # Step2's finished dialog
        sv._pump(40)
        new = _key(w, runs["seg_new"])
        assert win.row(new).btn_use.text() == "In use"
        assert _cell_path(w) == os.path.realpath(runs["seg_new"].mask)   # the default, once
        win.row(_key(w, runs["seg_old"])).btn_use.click()        # `Use`
        old = _key(w, runs["seg_old"])
        assert w._project_state.active_segmentation_run.run_dir == os.path.realpath(runs["seg_old"].dir)
        assert win.row(old).btn_use.text() == "In use"
        assert win.ticked() == [(new, "cell")], "Use ticked or unticked something"
        # breadcrumb back into Step3: the choice and the ticks are kept
        sv._enter(rig, 1)
        sv._enter(rig, 3)
        assert win.row(old).btn_use.text() == "In use" and win.ticked() == [(new, "cell")]
    finally:
        w.close()


def test_a_new_step2_result_joins_the_list_and_the_masks_are_not_set_again(
        app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, active="seg_new")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        sv._enter(rig, 3)
        calls = _counting(w._step3_mount)
        shown = _cell_path(w)
        newest = mh._run(rdir, "seg_newest", "cellpose_wholecell_fusion", bbox=BBOX,
                         shapes=SHAPES, created_at="2026-09-26T11:00:00")   # becomes active
        w._step2.segmentation_done.emit(newest.dir)
        assert len(win.keys()) == 3
        assert win.row(_key(w, runs["seg_new"])).btn_use.text() == "In use"   # kept
        assert calls == [] and _cell_path(w) == shown
        sv._enter(rig, 1)
        sv._enter(rig, 3)                                          # a plain re-entry too
        assert calls == []
        # ...but not on another page: the list waits for the next entry
        sv._enter(rig, 1)
        mh._run(rdir, "seg_later", "cellpose_wholecell_fusion", bbox=BBOX, shapes=SHAPES,
                created_at="2026-09-26T12:00:00")
        w._step2.segmentation_done.emit("")
        assert len(win.keys()) == 3
    finally:
        w.close()


@pytest.mark.parametrize("method,nuclei,cell,nucleus", [
    ("cellpose_nuclei_dapi", False, False, True),
    ("mesmer_nuclear_guided", True, True, True),
    ("stardist_nuclei_expansion", False, True, False),
])
def test_the_method_decides_which_ticks_work(app, tmp_path, monkeypatch, method, nuclei,
                                             cell, nucleus):
    rdir, _ = _project(tmp_path, runs=(("seg_x", method, "2026-09-26T08:00:00"),),
                       nuclei=nuclei)
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        sv._enter(rig, 3)
        row = win.row(win.keys()[0])
        assert row.ticks["cell"].isEnabled() is cell
        assert row.ticks["nucleus"].isEnabled() is nucleus
        # the default: its cells, else its nuclei
        assert [k for _key_, k in win.ticked()] == (["cell"] if cell else ["nucleus"])
    finally:
        w.close()


def test_a_run_of_another_roi_is_shown_on_its_own_region(app, tmp_path, monkeypatch):
    """Block B3: Step3 is a general result viewer -- a run of another ROI
    (same slide) is shown where ITS region is, not refused."""
    rdir, runs = _project(tmp_path, runs=(("seg_x", "cellpose_wholecell_fusion",
                                           "2026-09-26T08:00:00"),), roi_name="ROI_9")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    try:
        sv._enter(rig, 3)
        assert _cell_path(w) == os.path.realpath(runs["seg_x"].mask)
        assert tuple(w._step3_mount.mask_slots()[0][1].bbox) == BBOX
    finally:
        w.close()


def test_several_records_are_drawn_the_top_row_on_top(app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, runs=(
        ("seg_a", "mesmer_nuclear_guided", "2026-09-26T08:00:00"),
        ("seg_b", "mesmer_nuclear_guided", "2026-09-26T09:00:00")), nuclei=True, active="seg_a")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        sv._enter(rig, 3)
        a, b = _key(w, runs["seg_a"]), _key(w, runs["seg_b"])
        assert win.keys() == [b, a]                          # newest first
        win.row(b).ticks["nucleus"].setChecked(True)
        win.row(a).ticks["nucleus"].setChecked(True)
        real = {r: os.path.realpath(runs[r].dir) for r in runs}
        assert [(run, kind) for run, kind, _p in _drawn(w)] == [
            (real["seg_a"], "cell"), (real["seg_a"], "nucleus"), (real["seg_b"], "nucleus")]
        # a row's panel reaches its own slot
        win.row(b).swatches["nucleus"].panel.opacity.setValue(30)
        style = dict((k, st) for k, _s, st in w._step3_mount.mask_slots())
        assert style[f"{b}\x1enucleus"].alpha == pytest.approx(0.3)
        assert style[f"{a}\x1enucleus"].alpha == pytest.approx(0.75)
        win.row(a).ticks["cell"].setChecked(False)
        assert [(run, kind) for run, kind, _p in _drawn(w)] == [
            (real["seg_a"], "nucleus"), (real["seg_b"], "nucleus")]
    finally:
        w.close()


def test_load_adds_a_ticked_run_that_is_not_active(app, tmp_path, monkeypatch):
    rdir, runs = _project(tmp_path, runs=(("seg_x", "cellpose_wholecell_fusion",
                                           "2026-09-26T08:00:00"),), active="seg_x")
    far_dir, far = _project(tmp_path / "far", runs=(("seg_far", "cellpose_wholecell_fusion",
                                                     "2026-09-26T10:00:00"),))
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    monkeypatch.setattr(type(w), "_step3_slide_path", lambda self: "")
    from block01.core import step3_masks
    monkeypatch.setattr(step3_masks, "same_slide", lambda a, b: True)
    try:
        sv._enter(rig, 3)
        w._step3_pick_run_folder = lambda: far["seg_far"].dir
        assert w._step3_load_run() is True
        far_key = _key(w, far["seg_far"])
        assert (far_key, "cell") in win.ticked()
        assert win.row(_key(w, runs["seg_x"])).btn_use.text() == "In use"
        assert w._project_state.active_segmentation_run.run_dir == os.path.realpath(runs["seg_x"].dir)
        assert os.path.realpath(far["seg_far"].mask) in [p for _r, _k, p in _drawn(w)]
    finally:
        w.close()


def test_no_runs_and_a_dataset_switch(app, tmp_path, monkeypatch, capsys):
    rdir, _ = _project(tmp_path, runs=())
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        sv._enter(rig, 3)
        assert win.keys() == [] and win.empty.isVisibleTo(win)
        assert win.hint.full_text() == "No segmentation results for this ROI — run Step2"
        out = capsys.readouterr().out
        assert out.count("[Step3] mask: No segmentation results") == 1
        w._step3_update_mask_hint()
        w._step3_update_mask_hint()
        assert "[Step3] mask:" not in capsys.readouterr().out            # once per change
        # a run appears; then the dataset moves
        mh._run(rdir, "seg_x", "cellpose_wholecell_fusion", bbox=BBOX, shapes=SHAPES)
        w._step3_refresh_masks()
        assert win.keys() and w._step3_mount.mask_slots()
        w._close_step3_viewer()
        assert win.keys() == [] and w._step3_mount.mask_slots() is None
    finally:
        w.close()


def test_a_rebuilt_mount_gets_the_settings_again(app, tmp_path, monkeypatch):
    """GPU: the label binding's ACTUAL style, before and after the mount's
    backend was rebuilt (a new source)."""
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
        win = w._step3_results
        (key, kind), = win.ticked()
        panel = win.row(key).swatches[kind].panel
        panel.width.setValue(3)
        panel.set_colour((1.0, 1.0, 1.0))
        slot = f"{key}\x1e{kind}"
        assert mount.label_binding.style(slot).width == 3.0
        mount.source_changed("test")                    # the backend is rebuilt
        sv._enter(rig, 1)
        sv._enter(rig, 3)
        style = w._step3_mount.label_binding.style(slot)
        assert style.width == 3.0 and style.color == (1.0, 1.0, 1.0)
        assert w._step3_mount.label_binding.mask_status()[slot]["source"] is True
    finally:
        w.close()


def test_load_into_an_empty_list_uses_nothing_and_ticks_only_its_cells(app, tmp_path, monkeypatch):
    """Codex review 2: Load never makes a record active by itself, and the
    default tick is by availability (cells when it has them, nuclei alone
    only when it has no cells)."""
    rdir, _ = _project(tmp_path, runs=())
    far_dir, far = _project(tmp_path / "far", runs=(("seg_far", "mesmer_nuclear_guided",
                                                     "2026-09-26T10:00:00"),), nuclei=True)
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    monkeypatch.setattr(type(w), "_step3_slide_path", lambda self: "")
    from block01.core import step3_masks
    monkeypatch.setattr(step3_masks, "same_slide", lambda a, b: True)
    try:
        sv._enter(rig, 3)
        assert win.keys() == []
        w._step3_pick_run_folder = lambda: far["seg_far"].dir
        assert w._step3_load_run() is True
        key = _key(w, far["seg_far"])
        assert win.row(key).btn_use.text() == "Use", "Load made the record active"
        assert w._project_state.active_segmentation_run is None
        assert win.ticked() == [(key, "cell")]
        # untick, then `Use`: no tick comes back (codex review 3)
        win.row(key).ticks["cell"].setChecked(False)
        win.row(key).btn_use.click()
        assert win.row(key).btn_use.text() == "In use"
        assert win.ticked() == []
    finally:
        w.close()


def test_a_pyramid_built_since_is_resolved_again(app, tmp_path, monkeypatch):
    """Codex review 2: the resolve cache does not keep a mask's 'no
    pyramid' once one has been built on disk (a rebuilt viewer's new
    binding would otherwise get the stale source)."""
    rdir, runs = _project(tmp_path, runs=(("seg_x", "cellpose_wholecell_fusion",
                                           "2026-09-26T08:00:00"),), pyramid=False, active="seg_x")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    from block01.core import step3_masks
    try:
        sv._enter(rig, 3)
        entry = next(iter(w._step3_entries.values()))
        provider = w._step3_mount.host.stack.provider
        shapes = [provider.level_shape(l) for l in range(provider.num_levels)]
        before = w._step3_resolved(entry, shapes)["cell"]
        assert before.pyramid is None
        assert step3_masks.ensure_pyramid(before, shapes).status == "ready"
        assert w._step3_resolved(entry, shapes)["cell"].pyramid is not None
    finally:
        w.close()


def test_the_results_window_can_be_minimized_and_follows_step3(app, tmp_path, monkeypatch):
    """User acceptance: the window stayed on Step4 and could not be
    minimized. It has a minimize button, hides on another step and comes
    back on Step3 only if it was open."""
    from PyQt5.QtCore import Qt
    rdir, _runs = _project(tmp_path, active="seg_old")
    rig = _window(app, tmp_path, monkeypatch, rdir)
    w = rig.w
    win = w._step3_results
    try:
        flags = win.windowFlags()
        assert flags & Qt.WindowMinimizeButtonHint and (flags & Qt.WindowType_Mask) == Qt.Window
        assert flags & Qt.WindowStaysOnTopHint
        sv._enter(rig, 3)
        w._step3_show_results()
        assert win.isVisible()
        w._stack.setCurrentWidget(w._step4)
        assert not win.isVisible()
        w._stack.setCurrentWidget(w._step3)
        assert win.isVisible()
        win.showMinimized()                             # Results… brings it back
        assert win.windowState() & Qt.WindowMinimized
        w._step3_show_results()
        assert not win.windowState() & Qt.WindowMinimized
        win.close()                                     # closed on Step3: stays closed
        w._stack.setCurrentWidget(w._step4)
        w._stack.setCurrentWidget(w._step3)
        assert not win.isVisible()
    finally:
        w.close()
