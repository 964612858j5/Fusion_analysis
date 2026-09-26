"""Block 2c-2: Step3's own whole-slide viewer, and its read-only navigator.

  * Step3's navigator (block S5): the ROIs are Step0/Step1's and frozen --
    no ROI edit lands, roi_config.json is unchanged -- while patches are
    added, moved, renamed and deleted there and written like Step1's, so
    Step1 follows; Step1 keeps its own rights (the 2c-1 button once handed
    Step1's rights to Step3);
  * entering Step3 opens a second Step1 mount in Step3's slot, the notice
    hidden; a failed open says why in the slot and is not retried for the
    same slide; a failed rebind falls back the same way and is retried after
    the handoff moves;
  * both viewers are resident and only the one on screen reads: the other is
    paused and issues no new request while away, and catches up on return;
  * the Overlay / Fusion buttons reach both viewers (a paused one records);
  * one camera across Step1 <-> Step3;
  * a navigator click moves the viewer of the step on screen, once -- also
    when Step1's viewer was never built and when the popup already existed;
  * a dataset switch closes Step3's viewer; closing the window closes both.

CPU composition over the synthetic pyramid of `test_step1_viewer_mount.py`
(real GPU pixels need a real OpenGL context and are checked on the machine).
"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

import test_downstream_display_seed as ds  # noqa: E402
import test_step1_navigator_policy as nav  # noqa: E402
import test_step1_viewer_mount as vm  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: None))


# ── Step3's navigator: ROIs frozen, patches edited and synced (block S5) ──

def _settle(w):
    return nav._settle(w)


@pytest.mark.parametrize("entry", ["step", "button"])
def test_step3_s_navigator_freezes_rois_and_syncs_patches(app, tmp_path, entry):
    w, step0_dir = nav._window(app, tmp_path)
    try:
        ov = w._step0._tissue_navigator_popup.overview
        w._stack.setCurrentIndex(3)
        w._set_step_active(3)
        if entry == "button":
            w._btn_step3_tissue_nav.click()
        assert ov.edit_policy() == {"roi_create": False, "roi_delete": False,
                                    "patch_edit": True}
        # ROIs: every edit refused, on the canvas and in the ROI list
        rois = [dict(r) for r in ov._rois]
        roi_disk = nav._published(step0_dir, "roi_config.json")
        nav._draw_roi(ov)
        ov._delete_last_roi()
        w._step0._roi_selected_indices = [0]
        w._step0._delete_selected_rois()
        assert [dict(r) for r in ov._rois] == rois
        assert nav._published(step0_dir, "roi_config.json") == roi_disk
        # patches: added, moved, renamed and deleted -- and written, like Step1's
        ov._add_patch(16, 28, 16, 28, 16, 28, 16, 28, 0)
        assert _settle(w) == "published"
        boxes = [p["bbox_fullres"] for p in nav._published(step0_dir, "patch_config.json")]
        assert boxes == [[0, 16, 0, 16], [16, 28, 16, 28]]
        assert ov._commit_patch_geometry(1, (18, 30, 18, 30)) is True
        assert _settle(w) == "published"
        assert ov.rename_patch(1, "Mark A") is not False
        assert _settle(w) == "published"
        published = nav._published(step0_dir, "patch_config.json")
        assert [p["bbox_fullres"] for p in published] == [[0, 16, 0, 16], [18, 30, 18, 30]]
        assert "Mark A" in [p.get("name") for p in published]
        # ...and Step1 follows
        assert [tuple(p) for p in w.step0_output["patches"]][-1] == (18, 30, 18, 30)
        ov._remove_patch(1)
        assert _settle(w) == "published"
        assert [p["bbox_fullres"] for p in nav._published(step0_dir, "patch_config.json")] \
            == [[0, 16, 0, 16]]
        assert nav._published(step0_dir, "roi_config.json") == roi_disk
        # Step1 keeps its own rights
        nav._enter_step1(w)
        w._btn_step1_tissue_nav.click()
        assert ov.edit_policy()["roi_delete"] is True and ov.edit_policy()["patch_edit"] is True
    finally:
        w.close()


# ── two resident viewers ──────────────────────────────────────────────

def _rig(app, tmp_path, monkeypatch):
    """A real window whose mounts open the synthetic pyramid (CPU path)."""
    from block01.ui import step1_viewer_mount as mount_module
    w = ds._window(app, tmp_path)
    w.loader.filepath = "/x/c4.ome.tif"
    w._corrected_decisions = {}
    w._corrected_zarr_path = "/tmp/c4-none.zarr"
    w._active_roi = {"name": "ROI_1", "bbox_fullres": list(vm.ROI)}
    w.step0_output["channel_remap_config_hash"] = "rev-1"
    w._step1_context_ready = True
    raws = {}
    original = mount_module.Step1WholeSlideMount.__init__

    def _init(self, window, host=None, parent=None, **kwargs):
        mine = raws.setdefault(kwargs.get("camera_reason", "step1"), [])
        original(self, window, host=vm.Step1ViewerHost(stack_factory=vm._stack_factory(mine)),
                 parent=parent, gpu=False, **kwargs)

    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "__init__", _init)
    return SimpleNamespace(w=w, raws=raws)


def _reads(rig, which):
    return sum(len(r.reads) for r in rig.raws.get(which, []))


def _enter(rig, step):
    w = rig.w
    {1: w._go_to_step1, 3: w._go_to_step3, 2: w._go_to_step2, 0: w._go_to_step0}[step]()
    _pump(40)


def _pump(rounds=40):
    import time
    for _ in range(rounds):
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


def test_step3_opens_its_own_viewer_in_its_slot(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        _enter(rig, 3)
        m3 = w._step3_mount
        assert m3 is not w.__dict__.get("_step1_mount") and m3.host.stack is not None
        assert w._step3.viewer_layout().indexOf(m3.host) >= 0
        assert m3.host.isVisibleTo(w._step3) and not w._step3.viewer_notice().isVisibleTo(w._step3)
        assert m3._camera_reason == "step3"
        assert _reads(rig, "step3") > 0
    finally:
        w.close()


def test_only_the_viewer_on_screen_reads(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        _enter(rig, 1)
        _enter(rig, 3)
        m1, m3 = w._step1_mount, w._step3_mount
        assert m1.paused and not m3.paused and m3.active
        before1 = _reads(rig, "step1")
        # a draft edit and a camera move while Step3 is on screen
        w._display.fusion.edit_channel_weight("CD8", 0.9, origin="test")
        m3.host.stack.view.view_box.setRange(xRange=(0, vm.SLIDE / 3), yRange=(0, vm.SLIDE / 3),
                                             padding=0)
        _pump(30)
        assert _reads(rig, "step1") == before1, "Step1's paused viewer read tiles"
        _enter(rig, 1)
        assert m3.paused and not m1.paused and m1.active
        before3 = _reads(rig, "step3")
        m1.host.stack.view.view_box.setRange(xRange=(0, vm.SLIDE / 4), yRange=(0, vm.SLIDE / 4),
                                             padding=0)
        w._display.fusion.edit_channel_weight("CD8", 0.2, origin="test")
        _pump(30)
        assert _reads(rig, "step3") == before3, "Step3's paused viewer read tiles"
        _enter(rig, 2)
        assert m1.paused and m3.paused
    finally:
        w.close()


def test_the_mode_buttons_reach_both_viewers(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        _enter(rig, 1)
        _enter(rig, 3)
        w._btn_step3_mode_fusion.click()
        _pump(20)
        assert w._step3_mount._mode == vm.compose_core.MODE_FUSION
        assert w._step1_mount._mode == vm.compose_core.MODE_FUSION     # recorded while paused
        _enter(rig, 1)
        w._btn_mode_overlay.click()
        _pump(20)
        assert w._step1_mount._mode == vm.compose_core.MODE_OVERLAY
        assert w._step3_mount._mode == vm.compose_core.MODE_OVERLAY
    finally:
        w.close()


def test_one_camera_across_step1_and_step3(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        _enter(rig, 1)
        _enter(rig, 3)
        w._step3_mount.jump_to_point(vm.SLIDE // 3, vm.SLIDE // 4, vm.SLIDE // 8)
        _pump(20)
        cx3, cy3, _s3 = w._step3_mount.current_camera()
        _enter(rig, 1)
        cx1, cy1, _s1 = w._step1_mount.current_camera()
        assert abs(cx1 - cx3) < vm.SLIDE * 0.02 and abs(cy1 - cy3) < vm.SLIDE * 0.02
        # ...and the other way: moved in Step1, Step3 lands on the same place
        w._step1_mount.jump_to_point(vm.SLIDE * 2 // 3, vm.SLIDE // 2, vm.SLIDE // 8)
        _pump(20)
        cx1, cy1, _s1 = w._step1_mount.current_camera()
        _enter(rig, 3)
        cx3, cy3, _s3 = w._step3_mount.current_camera()
        assert abs(cx1 - cx3) < vm.SLIDE * 0.02 and abs(cy1 - cy3) < vm.SLIDE * 0.02
    finally:
        w.close()


class _Overview(QtCore.QObject):
    navigate_requested = QtCore.pyqtSignal(int, int)

    def set_current_view_rect(self, rect):
        pass

    def clear_current_view_rect(self):
        pass


def _count_jumps(mount):
    jumps = []
    real = mount.jump_to_point
    mount.jump_to_point = lambda y, x, size: (jumps.append((y, x)), real(y, x, size))[1]
    return jumps


def test_a_navigator_click_moves_step3_without_step1_ever_built(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        _enter(rig, 3)                                # straight to Step3
        assert w.__dict__.get("_step1_mount") is None
        popup = SimpleNamespace(overview=_Overview())
        w._display._navigator = popup
        w._display.navigator_created.emit(popup)      # the popup appears now
        jumps = _count_jumps(w._step3_mount)
        popup.overview.navigate_requested.emit(900, 700)
        _pump(10)
        assert jumps == [(900, 700)]
    finally:
        w._display._navigator = None
        w.close()


def test_a_click_moves_the_viewer_on_screen_once_after_round_trips(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    popup = SimpleNamespace(overview=_Overview())
    try:
        w._display._navigator = popup                 # already open before Step3
        _enter(rig, 3)
        _enter(rig, 1)
        _enter(rig, 3)
        _enter(rig, 1)
        _enter(rig, 3)
        j1, j3 = _count_jumps(w._step1_mount), _count_jumps(w._step3_mount)
        popup.overview.navigate_requested.emit(500, 400)
        _pump(10)
        assert j3 == [(500, 400)] and j1 == []
        _enter(rig, 1)
        popup.overview.navigate_requested.emit(600, 300)
        _pump(10)
        assert j1 == [(600, 300)] and j3 == [(500, 400)]
    finally:
        w._display._navigator = None
        w.close()


def test_a_failed_open_says_why_and_is_not_retried(app, tmp_path, monkeypatch):
    from block01.ui import step1_viewer_mount as mount_module
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    opens = []
    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "open",
                        lambda self, channel="": (opens.append(1), None)[1])
    try:
        _enter(rig, 3)
        notice = w._step3.viewer_notice()
        assert notice.isVisibleTo(w._step3) and "could not open" in notice.text()
        assert not w._step3_mount.host.isVisibleTo(w._step3)
        _enter(rig, 2)
        _enter(rig, 3)
        assert opens == [1], "the same slide was tried again"
    finally:
        w.close()


def test_a_failed_rebind_falls_back_and_is_retried_after_the_handoff_moves(app, tmp_path,
                                                                             monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        _enter(rig, 3)
        m3 = w._step3_mount
        real_sync = m3.sync_source

        def broken(reason="source"):
            raise RuntimeError("the new ROI cannot be read")
        m3.sync_source = broken
        assert w._step3_sync_whole_slide_source("handoff") is False
        notice = w._step3.viewer_notice()
        assert notice.isVisibleTo(w._step3) and "the new ROI cannot be read" in notice.text()
        assert not m3.host.isVisibleTo(w._step3) and m3.paused
        before = _reads(rig, "step3")
        _pump(20)
        assert _reads(rig, "step3") == before
        # the handoff moves again, and this time the rebind works
        m3.sync_source = real_sync
        w._step3_sync_whole_slide_source("handoff")
        _enter(rig, 2)
        _enter(rig, 3)
        assert m3.host.isVisibleTo(w._step3) and not notice.isVisibleTo(w._step3)
        assert not m3.paused
    finally:
        w.close()


def test_a_dataset_switch_closes_step3_and_the_window_closes_both(app, tmp_path, monkeypatch):
    rig = _rig(app, tmp_path, monkeypatch)
    w = rig.w
    _enter(rig, 1)
    _enter(rig, 3)
    m1, m3 = w._step1_mount, w._step3_mount
    w._discard_step1_dataset_state()
    assert m3.host.stack is None
    from block01.ui.step3_page import VIEWER_PENDING_TEXT
    assert w._step3.viewer_notice().text() == VIEWER_PENDING_TEXT
    w.close()
    assert m1.host.stack is None
    assert w.__dict__.get("_step3_mount") is None
