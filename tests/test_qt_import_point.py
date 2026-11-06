"""One import point for Qt (docs/v17_pyside6_plan.md §4): no file outside
`block01/qt/` names a Qt binding, so the binding changes in one place."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_BINDING = re.compile(r"^\s*(from|import)\s+(PyQt5|PyQt6|PySide2|PySide6|sip)\b", re.M)


def test_only_the_import_point_names_the_binding():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"qt", "native", "docs"} or "__pycache__" in rel.parts:
            continue
        if _BINDING.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(rel))
    assert offenders == []


def test_the_import_point_offers_the_shared_spellings():
    from block01.qt import QtCore, is_deleted, keep_wrappers_at_exit  # noqa: F401
    from block01.qt.QtCore import Signal, Slot  # noqa: F401
    from block01.qt import QtGui, QtTest, QtWidgets  # noqa: F401
    assert callable(is_deleted)


def test_the_qt6_module_moves_are_resolved_in_the_import_point():
    """Qt 6 moves QOpenGLWidget/QOpenGLFramebufferObject/QShortcut/QActionGroup
    to other modules; the entry point resolves them once, and both shims
    re-export them so every existing call site keeps its spelling."""
    from block01.qt import (QtGui, QtWidgets, QAction, QActionGroup,
                            QOpenGLFramebufferObject,
                            QOpenGLFramebufferObjectFormat, QOpenGLWidget,
                            QShortcut)
    assert QtWidgets.QOpenGLWidget is QOpenGLWidget
    assert QtGui.QOpenGLFramebufferObject is QOpenGLFramebufferObject
    assert QtGui.QOpenGLFramebufferObjectFormat is QOpenGLFramebufferObjectFormat
    assert QtWidgets.QShortcut is QShortcut
    assert QtWidgets.QActionGroup is QActionGroup
    assert QtWidgets.QAction is QAction


def test_is_checked_accepts_the_int_and_the_enum():
    """`stateChanged` delivers an int; a Qt 6 `Qt.Checked` is an enum that
    does not equal it (probed). `is_checked` is the one comparison."""
    from block01.qt import QtCore, is_checked
    assert is_checked(2) is True                    # what stateChanged sends
    assert is_checked(QtCore.Qt.Checked) is True    # what checkState() returns
    assert is_checked(QtCore.Qt.Unchecked) is False
    assert is_checked(0) is False


def test_thread_finished_is_the_physical_signal():
    """The workers expose their payload as `job_finished`; `finished` is the
    thread's own signal again. A Qt 6 binding cannot reach past a shadowing
    subclass signal (descriptor lookup returns the subclass's -- probed on
    PySide6 6.11.3), which is why the business signal was renamed."""
    from block01.qt import QtCore, QtWidgets, thread_finished
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["pytest-qt-point"])

    class _Worker(QtCore.QThread):
        job_finished = QtCore.Signal(str)          # business payload

        def run(self):
            return

    worker = _Worker()
    assert worker.finished is not worker.job_finished
    assert thread_finished(worker) is worker.finished
    seen = []
    thread_finished(worker).connect(lambda: seen.append("physical"))
    worker.start()
    assert worker.wait(5000)
    for _ in range(20):
        app.processEvents()
        if seen:
            break
        QtCore.QThread.msleep(10)
    assert seen == ["physical"]


def test_pyqtgraph_is_told_the_binding_before_it_is_imported():
    """A second interpreter: import the entry point first, then pyqtgraph,
    and the library must have picked the same binding."""
    import os
    import subprocess
    import sys

    code = ("import block01.qt as q, pyqtgraph; "
            "print(pyqtgraph.Qt.QT_LIB, q.BINDING)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=dict(os.environ), timeout=180)
    assert out.returncode == 0, out.stderr
    lib, binding = out.stdout.split()
    assert lib == binding, f"pyqtgraph bound to {lib}, entry point says {binding}"
