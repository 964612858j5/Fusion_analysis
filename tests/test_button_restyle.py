"""Button restyle, every step (docs/v17_button_restyle_plan.md §2-§4).

Each converted button BY NAME, in both themes: its look is its role's
(`button_qss(role, metrics)`), its size is what it was (the old sheet on a
twin button gives today's size hint), the data colours stay. The two
destructive confirmations: Enter and Esc (real key presses) keep things,
only the red Yes goes on, and the words stay Yes / No.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtWidgets  # noqa: E402

from block01.ui.theme import dialogs as theme_dialogs  # noqa: E402
from block01.ui.theme import widgets as W  # noqa: E402

THEMES = ("dark", "light")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(params=THEMES)
def theme(request, monkeypatch):
    monkeypatch.setenv("BLOCK01_THEME", request.param)
    return request.param


def _same_size(button, old_qss, cls=QtWidgets.QPushButton):
    """`button` is as large as the same button under its old sheet."""
    twin = cls()
    twin.setText(button.text())
    if cls is QtWidgets.QToolButton:
        twin.setPopupMode(button.popupMode())
        twin.setToolButtonStyle(button.toolButtonStyle())
    twin.setStyleSheet(old_qss)
    if button.minimumWidth() == button.maximumWidth():
        twin.setFixedWidth(button.minimumWidth())
    assert button.sizeHint() == twin.sizeHint(), button.text()


def _is(button, role, metrics, cls="QPushButton"):
    assert button.styleSheet().startswith(W.button_qss(role, metrics, cls)), button.text()


# ── Step0: ROI / Patch toolbar and lists, Channels → Method ▾ ────────────

def _step0(app):
    from block01.ui.step0 import step0_page as sp
    return sp.Step0Page()


def test_step0_buttons_take_their_roles_and_keep_their_size(app, theme):
    page = _step0(app)
    try:
        for b in (page._btn_del_roi, page._btn_del_patch):
            _is(b, "danger", "padding:1px 6px;font-size:10px;")
            _same_size(b, "QPushButton{color:#e06c75;border:1px solid #e06c75;border-radius:3px;"
                          "padding:1px 6px;font-size:10px;background:#161616;}")
        _is(page._btn_rename_roi, "panel", "padding:3px 7px;font-size:10px;")
        old_ts = ("QPushButton{{color:{c};border:1px solid {c};border-radius:3px;"
                  "padding:3px 7px;font-size:10px;background:#161616;}}")
        _same_size(page._btn_rename_roi, old_ts.format(c="#e5c07b"))
        # ROI / Patch modes: their colours are data and stay; hover is the theme's
        for b, c in ((page._btn_mode_roi, "#6bcb77"), (page._btn_mode_patch, "#4d96ff")):
            qss = b.styleSheet()
            assert f"color:{c};border:1px solid {c}" in qss and f"checked{{background:{c}" in qss
            assert f"hover{{background:{W.t()['hover']};}}" in qss
            assert "#161616" not in qss
            _same_size(b, old_ts.format(c=c))

        # Channels → Method ▾ (found by attribute: the viewer's seg buttons
        # cannot stand in for these)
        menu_btn = page._method_menu.parentWidget()
        assert isinstance(menu_btn, QtWidgets.QToolButton)
        _is(menu_btn, "panel", "padding:1px 6px;font-size:10px;", "QToolButton")
        assert "menu-indicator{image:none;}" in menu_btn.styleSheet()
        _same_size(menu_btn, "QToolButton{background:#1a1a1a;color:#ddd;border:1px solid #444;"
                             "border-radius:3px;padding:1px 6px;font-size:10px;}"
                             "QToolButton::menu-indicator{image:none;}", QtWidgets.QToolButton)
        for b in (page._method_tophat_btn, page._method_cucim_btn):
            _is(b, "toggle", "padding:2px 6px;font-size:11px;")
            assert "#e5c07b" not in b.styleSheet()                  # ruling 4: checked grey
            assert b.isCheckable() and b.minimumWidth() == b.maximumWidth() == 72
            _same_size(b, "QPushButton{color:#888;background:#161616;border:1px solid #3a3a3a;"
                          "border-radius:3px;padding:2px 6px;font-size:11px;}")
        panel = page._method_menu.actions()[0].defaultWidget()
        save = [b for b in panel.findChildren(QtWidgets.QPushButton) if b.text() == "Save"]
        assert len(save) == 1
        _is(save[0], "panel", "padding:2px 10px;font-size:11px;")
        _same_size(save[0], "QPushButton{color:#9bd0ff;background:#182230;border:1px solid #354a63;"
                            "border-radius:3px;padding:2px 10px;font-size:11px;}")
    finally:
        page.close()


# ── Tissue Navigator ──────────────────────────────────────────────────────

class _Loader:
    shape = (800, 600)
    ch_map = {"DAPI": 0}

    def channel_names(self):
        return ["DAPI"]


def test_tissue_navigator_buttons_take_their_roles_and_keep_their_size(app, theme):
    from block01.ui.step0 import overview_panel as ovp
    panel = ovp.OverviewPanel(_Loader(), "DAPI", lazy=True)
    try:
        by_text = {b.text(): b for b in panel.findChildren(QtWidgets.QPushButton)}
        want = {"✓ Close (Enter)": ("panel", "#6bcb77"), "Z Undo": ("panel", "#ffd93d"),
                "📂 Load ROIs": ("panel", "#4d96ff"), "D Del ROI": ("danger", "#c44"),
                "✕ Clear ROIs": ("danger", "#c44")}
        for text, (role, old) in want.items():
            _is(by_text[text], role, "font-size:10px;padding:2px 5px;")
            _same_size(by_text[text], f"QPushButton{{color:{old};font-size:10px;"
                                      f"border:1px solid {old};border-radius:3px;padding:2px 5px;}}")
        clr = by_text["✕ Clear Patches"]
        _is(clr, "danger", "font-size:11px;padding:2px;")
        _same_size(clr, "QPushButton{color:#c44;font-size:11px;border:1px solid #c44;"
                        "border-radius:3px;padding:2px;}")
    finally:
        panel.close()


# ── the whole-slide correction's progress box, Step4's batch, Step1's tile ─

def test_the_correction_cancel_is_danger_and_keeps_its_size(app, theme):
    from block01.ui.step0 import search_ctrl as sc
    dlg = sc._WsiCorrectionProgressDialog()
    try:
        _is(dlg._cancel, "danger", "padding:5px 17px;")
        _same_size(dlg._cancel, "QPushButton{background:#722;color:white;border-radius:4px;"
                                "padding:6px 18px;}")
    finally:
        dlg.close()


def test_run_batch_is_the_run_role(app, theme):
    from block01.ui.batch_step4_dialog import BatchStep4Dialog
    dlg = BatchStep4Dialog()
    try:
        _is(dlg._run_btn, "run", "padding:7px 22px;font-weight:bold;")
        _same_size(dlg._run_btn, "QPushButton{background:#2a5;color:white;border-radius:4px;"
                                 "padding:7px 22px;font-weight:bold;}")
    finally:
        dlg.close()


def test_the_patch_tiles_cross_turns_red_and_the_tile_keeps_its_colour(app, theme):
    from block01.ui.step1_presegmentation.patches_panel import PatchTile
    tile = PatchTile(1, "P1", "#61afef", checked=True)
    try:
        qss = tile.styleSheet()
        assert "QFrame#patchTile{background:#61afef" in qss               # data colour
        assert f"QToolButton:hover{{background:{W.t()['danger']};color:#FFFFFF;" in qss
        assert tile.close_button().size().width() == 12 == tile.close_button().size().height()
    finally:
        tile.close()


# ── the two destructive confirmations ─────────────────────────────────────

@pytest.mark.parametrize("key,want", [("enter", False), ("esc", False), ("yes", True)])
def test_a_yes_no_danger_question_goes_on_only_on_yes(app, key, want):
    """Real keys on the real box: Enter and Esc keep, only Yes goes on."""
    from block01.qt import QtCore, QtTest
    seen = {}

    def answer():
        box = app.activeModalWidget()
        if not isinstance(box, QtWidgets.QMessageBox):
            QtCore.QTimer.singleShot(10, answer)
            return
        seen["words"] = sorted(b.text() for b in box.buttons())
        seen["danger"] = [b.text() for b in box.buttons() if b.property("ds") == "danger"]
        if key == "yes":
            next(b for b in box.buttons() if b.text() == "Yes").click()
        else:
            QtTest.QTest.keyClick(box, QtCore.Qt.Key_Return if key == "enter"
                                  else QtCore.Qt.Key_Escape)
    QtCore.QTimer.singleShot(10, answer)
    got = theme_dialogs.confirm_danger(None, "Load plan", "Replace the methods in the plan now?",
                                       action="Yes", cancel="No")
    assert got is want
    assert seen["words"] == ["No", "Yes"] and seen["danger"] == ["Yes"]
