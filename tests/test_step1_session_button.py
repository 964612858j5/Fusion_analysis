"""Block S, first part (user ruling 2026-09-26): `Load Previous Step1 Session`
always opens a file dialog, starting in the current ROI's Step1 folder, and
says on screen what happened -- opened, or why not. A session of another ROI
or project is refused with that reason: opening another project is not
supported yet (block S2, frozen until the project/session architecture block).
The automatic restore when a ROI is opened is unchanged: it only prints.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, monkeypatch):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    said = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _n=name, **k: said.append((_n, a[2]))))
    w._said = said
    yield w
    w.close()


def _dialog(monkeypatch, answer):
    asked = []

    def get_open(parent, title, start, filt):
        asked.append(start)
        return answer, ""
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(get_open))
    return asked


def test_the_button_always_asks_even_with_a_session_of_its_own(win, tmp_path, monkeypatch):
    step1 = tmp_path / "roi" / "step1"
    step1.mkdir(parents=True)
    (step1 / "step1_session.json").write_text("{}", encoding="utf-8")
    win.step0_output = {"step1_dir": str(step1)}
    asked = _dialog(monkeypatch, "")                       # the user cancels
    loads = []
    monkeypatch.setattr(type(win), "_load_previous_step1_session",
                        lambda self, *a, **k: loads.append(k) or True)
    assert win._on_load_previous_session_clicked() is False
    assert asked == [str(step1)] and loads == [] and win._said == []


def test_the_chosen_file_is_loaded_and_success_is_said(win, tmp_path, monkeypatch):
    chosen = tmp_path / "step1_session.json"
    chosen.write_text("{}", encoding="utf-8")
    _dialog(monkeypatch, str(chosen))
    loads = []
    monkeypatch.setattr(type(win), "_load_previous_step1_session",
                        lambda self, *a, **k: loads.append(k) or True)
    assert win._on_load_previous_session_clicked() is True
    assert loads == [{"path": str(chosen)}]
    assert win._said and win._said[-1][0] == "information" and str(chosen) in win._said[-1][1]


def test_a_session_of_another_roi_is_refused_with_the_reason(win, tmp_path, monkeypatch):
    """Through the real loader: a handoff is bound to ROI A, the session is
    ROI B's. Nothing changes, and the reason is on screen."""
    a = tmp_path / "A" / "step1"
    b = tmp_path / "B" / "step1"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    other = b / "step1_session.json"
    other.write_text(json.dumps({"handoff_schema_version": 2, "version": 2}), encoding="utf-8")
    win.step0_output = {"step1_dir": str(a)}
    before = dict(win.step0_output)
    _dialog(monkeypatch, str(other))
    assert win._on_load_previous_session_clicked() is False
    assert win._said and win._said[-1][0] == "warning"
    assert "another ROI or project" in win._said[-1][1]
    assert win.step0_output == before


def test_the_automatic_restore_still_only_prints(win, tmp_path, capsys):
    a = tmp_path / "A" / "step1"
    b = tmp_path / "B" / "step1"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    other = b / "step1_session.json"
    other.write_text(json.dumps({"handoff_schema_version": 2, "version": 2}), encoding="utf-8")
    win.step0_output = {"step1_dir": str(a)}
    assert win._load_previous_step1_session(auto=True, path=str(other)) is False
    assert win._said == []
    assert "another ROI or project" in capsys.readouterr().out
