"""The global exception hook (docs/v17_error_guard_plan.md §1).

  * in a real event loop (a subprocess, so this session's hooks stay
    Python's): an exception escaping a timer slot no longer ends the
    process; it is logged; the same error twice gives one notice, another
    error a second one once the first is closed; exceptions in a Python
    thread and escaping a `QThread.run()` are logged and their notices are
    shown on the GUI thread;
  * the notice is non-modal; the log rotates past its size; an unwritable
    home gives stderr only and the notice says so; `KeyboardInterrupt`
    goes to Python's default; installing twice installs once.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtWidgets, flag_value  # noqa: E402
from block01.qt.QtCore import Qt  # noqa: E402

from block01.utils import error_hook as eh  # noqa: E402

@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


SCRIPT = textwrap.dedent(r'''
    import importlib.util, json, os, sys, threading
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # THIS checkout as `block01`, whatever folder it sits in (the parent of
    # the repository may hold another `block01`)
    repo = os.environ["BLOCK01_TEST_REPO"]
    spec = importlib.util.spec_from_file_location(
        "block01", os.path.join(repo, "__init__.py"), submodule_search_locations=[repo])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["block01"] = pkg
    spec.loader.exec_module(pkg)
    from block01.qt import QtCore, QtWidgets
    from block01.utils import error_hook as eh
    assert os.path.dirname(os.path.dirname(eh.__file__)) == repo, eh.__file__
    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)       # the real app has its main window
    shown = []
    real_show = QtWidgets.QMessageBox.show
    def show(box):
        shown.append({"text": box.text(), "modal": int(box.windowModality()),
                      "gui": threading.current_thread() is threading.main_thread()})
        real_show(box)
        QtCore.QTimer.singleShot(30, box.close)      # the user closes it
    QtWidgets.QMessageBox.show = show
    assert eh.install(app) is eh.install(app)          # once

    def boom_same():
        raise ValueError("same place")
    def boom_other():
        raise KeyError("another place")
    class Worker(QtCore.QThread):
        def run(self):
            raise RuntimeError("in a QThread")
    worker = Worker()
    steps = [lambda: boom_same(), lambda: boom_same(), lambda: boom_other(),
             lambda: threading.Thread(target=lambda: 1 / 0, name="py-worker").start(),
             lambda: worker.start()]
    for i, step in enumerate(steps):
        QtCore.QTimer.singleShot(100 + 150 * i, step)
    QtCore.QTimer.singleShot(1400, app.quit)
    app.exec()
    worker.wait()
    print("RESULT " + json.dumps(shown))
''')


def test_a_real_event_loop_survives_logs_and_notifies(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path),
               BLOCK01_TEST_REPO=os.path.dirname(os.path.dirname(os.path.abspath(eh.__file__))))
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]           # no abort
    shown = json.loads(out.stdout.split("RESULT ", 1)[1])
    texts = [s["text"] for s in shown]
    assert len(texts) == 4, texts                              # same error once
    assert "ValueError: same place" in texts[0] and "KeyError" in texts[1]
    assert "ZeroDivisionError" in texts[2] and "RuntimeError: in a QThread" in texts[3]
    assert all(s["gui"] and s["modal"] == flag_value(Qt.NonModal) for s in shown)
    log = (tmp_path / ".block01" / "logs" / "errors.log").read_text()
    assert log.count("ValueError: same place") == 2            # every one is logged
    assert "in thread py-worker" in log and "RuntimeError: in a QThread" in log
    assert str(tmp_path / ".block01" / "logs" / "errors.log") in texts[0]


def test_the_notice_is_non_modal_and_one_at_a_time(app):
    notifier = eh.Notifier()
    window = QtWidgets.QMainWindow()
    window.show()
    try:
        assert notifier.ask(("E", "f", 1), "E: one", True) is True
        assert notifier.ask(("E", "f", 1), "E: one", True) is False      # the same error
        assert notifier.ask(("F", "g", 2), "F: two", True) is False      # one open already
        app.processEvents()
        box = notifier._box
        assert isinstance(box, QtWidgets.QMessageBox) and box.isVisible()
        assert box.windowModality() == Qt.NonModal
        assert QtWidgets.QApplication.activeModalWidget() is None and window.isEnabled()
        box.close()
        app.processEvents()
        assert notifier._box is None
        assert notifier.ask(("F", "g", 2), "F: two", True) is True
        app.processEvents()
        notifier._box.close()
        app.processEvents()
    finally:
        window.close()


def test_the_log_rotates_and_an_unwritable_home_is_stderr_only(tmp_path, monkeypatch, app):
    log = eh.ErrorLog(tmp_path / "logs" / "errors.log", limit=100)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "errors.log.1").write_text("older")
    assert log.write("x" * 150)
    assert log.write("next")                                    # past the limit: rotated
    assert (tmp_path / "logs" / "errors.log.1").read_text() == "x" * 150
    assert (tmp_path / "logs" / "errors.log").read_text() == "next"
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert eh.ErrorLog(blocked / "logs" / "errors.log").write("x") is False

    asked = []

    class N:
        def ask(self, sig, text, logged):
            asked.append(logged)
    try:
        raise ValueError("no home")
    except ValueError:
        eh.handle(*sys.exc_info(), log=eh.ErrorLog(blocked / "errors.log"), notifier=N())
    assert asked == [False]


def test_keyboard_interrupt_goes_to_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: seen.append(a[0]))

    class N:
        def ask(self, *a):
            raise AssertionError("no notice for a KeyboardInterrupt")
    eh.handle(KeyboardInterrupt, KeyboardInterrupt(), None, notifier=N())
    assert seen == [KeyboardInterrupt]


def test_the_hook_never_raises(monkeypatch):
    class Broken:
        def write(self, text):
            raise RuntimeError("the log broke")
    seen = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: seen.append(a[0]))
    eh.handle(ValueError, ValueError("x"), None, log=Broken())
    assert seen == [ValueError]
