"""Plan step 3: Step3's patch strip, the shared component, wired.

The real window of `test_step3_viewer.py` (CPU picture over a synthetic
pyramid):

  * ONE row over Step3's viewer: patch strip, `Cell mask ▾`, `Nucleus mask ▾`,
    the hint, `Overlay` / `Fusion`; the run drop-down is the tab bar's corner
    widget -- beside the `Viewer` tab, not inside its content;
  * Step3's strip shows the same names and colours as Step1's and follows
    every change of the one patch list;
  * a click in Step3 lands Step3's viewer on the patch (the same camera as
    Step1's viewer landing on it), and it is THE selection: Step1's strip
    marks it too and the session is saved like a Step1 choice; a Step1
    choice is marked in Step3's strip.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

import test_step3_viewer as sv  # noqa: E402

PATCHES = [(600, 700, 600, 700), (900, 1100, 1000, 1200), (1200, 1300, 700, 800)]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: None))


def _row_widgets(page):
    lay = page.viewer_layout().itemAt(0).layout()
    out = []
    for i in range(lay.count()):
        item = lay.itemAt(i)
        if item.widget() is not None:
            out.append(item.widget())
    return out


def test_one_row_and_the_run_list_beside_the_tab(app, tmp_path, monkeypatch):
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        bar, strip = w._step3_mask_bar, w._step3_patch_strip
        row = _row_widgets(w._step3)
        assert row == [strip.holder(), bar.buttons["cell"], bar.buttons["nucleus"], bar.hint,
                       w._btn_step3_mode_overlay, w._btn_step3_mode_fusion]
        tabs = w._step3._right_tabs
        corner = tabs.cornerWidget()
        assert corner is bar.corner()                                    # run list + Load… (B3)
        assert corner.isAncestorOf(bar.run_combo) and corner.isAncestorOf(bar.load_button)
        assert not tabs.widget(0).isAncestorOf(bar.run_combo)            # not in the tab's content
    finally:
        w.close()


def test_the_same_names_and_colours_as_step1_and_it_follows_the_list(app, tmp_path, monkeypatch):
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        w._on_patches(PATCHES)
        s1, s3 = w._patch_strip, w._step3_patch_strip
        assert [b.text() for b in s3.buttons] == ["P1", "P2", "P3"]
        assert [b.styleSheet() for b in s3.buttons] == [b.styleSheet() for b in s1.buttons]
        w._on_patches(PATCHES[:2] + [(1300, 1400, 1300, 1400)] * 8)
        assert len(s3.buttons) == 7 and len(s3.menu_actions) == 10
        w._on_patches([])
        assert s3.buttons == [] and not s3.menu_button.isEnabled()
    finally:
        w.close()


def test_a_step3_click_lands_there_and_is_the_selection(app, tmp_path, monkeypatch):
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        w._on_patches(PATCHES)
        sv._enter(rig, 3)
        m3 = w._step3_mount
        shown, saves = [], []
        original = m3.show_patch
        monkeypatch.setattr(m3, "show_patch", lambda bbox: (shown.append(tuple(bbox)),
                                                            original(bbox))[1])
        monkeypatch.setattr(w, "_schedule_step1_session_save", lambda: saves.append(1))
        w._step3_patch_strip.buttons[1].click()
        sv._pump(20)
        assert shown == [tuple(PATCHES[1])] and saves
        assert w._preview_patch_idx == 1 and w._selected_step1_patch_idx == 1
        assert [b.isChecked() for b in w._patch_strip.buttons] == [False, True, False]
        assert [b.isChecked() for b in w._step3_patch_strip.buttons] == [False, True, False]
        cam3 = m3.current_camera()
        # the same landing as Step1's viewer for the same patch
        sv._enter(rig, 1)
        m1 = w._step1_mount
        m1.show_patch(PATCHES[1])
        sv._pump(20)
        cam1 = m1.current_camera()
        assert cam3 is not None and cam1 is not None
        assert cam3[:2] == pytest.approx(cam1[:2], abs=1.0)
        # a Step1 choice is marked in Step3's strip
        w._patch_strip.buttons[2].click()
        assert [b.isChecked() for b in w._step3_patch_strip.buttons] == [False, False, True]
    finally:
        w.close()


def test_a_click_before_the_viewer_opened_is_still_the_selection(app, tmp_path, monkeypatch):
    rig = sv._rig(app, tmp_path, monkeypatch)
    w = rig.w
    try:
        w._on_patches(PATCHES)
        w._step3_patch_strip.menu_actions[2].trigger()
        assert w._preview_patch_idx == 2
        assert w._patch_strip.menu_actions[2].isChecked()
    finally:
        w.close()
