"""Annotate needs something to number: with no map sheet or no core found,
it says what to do first instead of a silent "Selected 0" (user 2026-10-25)."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402

from block01.ui.regions.page import RegionsPage  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _page(study, objects):
    return SimpleNamespace(cores=SimpleNamespace(study=study), numbering_objects=lambda: objects,
                           annotate_blocker=lambda: RegionsPage.annotate_blocker(p))


def test_the_blocker_names_the_missing_step(app):
    global p
    p = _page(None, [])
    assert "map sheet" in RegionsPage.annotate_blocker(p)
    p = _page({"map_sheet": {}}, [])
    assert "Find cores" in RegionsPage.annotate_blocker(p)
    p = _page({"map_sheet": {}}, [("u1", 10.0, 20.0)])
    assert RegionsPage.annotate_blocker(p) == ""


def test_choosing_line_without_cores_shows_the_message_and_sets_no_tool(app, monkeypatch):
    global p
    shown, tools = [], []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a: shown.append(a[2])))
    p = _page({"map_sheet": {}}, [])
    p.commit_transform = lambda: None
    p.ctl = SimpleNamespace(set_tool=tools.append)
    p._show_tool = lambda: None
    RegionsPage._set_tool(p, "line")
    assert shown and "Find cores" in shown[0] and tools == []


def test_the_annotate_menu_closes_and_stays_closed_after_the_message(app, monkeypatch):
    """The real menu: opening it with nothing to number closes it, then shows
    the message once; it is not open again afterwards."""
    from PyQt5 import QtCore
    shown = []

    def real_modal(parent, title, text):
        # a REAL modal message (its own event loop, where the old bug lived),
        # dismissed by a timer
        shown.append(text)
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Information, title, text, parent=parent)
        QtCore.QTimer.singleShot(50, box.accept)
        return box.exec_()
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(real_modal))
    page = RegionsPage()
    page.show()
    btn = page.family_buttons["Annotate"]
    btn.setEnabled(True)
    menu = btn.menu()
    menu.popup(btn.mapToGlobal(QtCore.QPoint(0, btn.height())))
    for _ in range(40):
        app.processEvents()
        QtCore.QThread.msleep(5)
    assert len(shown) == 1 and "map sheet" in shown[0]
    assert not menu.isVisible()
    page.close()
