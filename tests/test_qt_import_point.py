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


def _pins(path):
    pins = {}
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            name, _, version = line.lstrip("- ").partition("==")
            pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


def test_the_installers_pip_list_installs_the_binding_the_import_point_names():
    """The installer builds the env from the conda lock + this pip list
    (scripts/install_block01.sh), not from environment.yml: it must carry
    the binding `qt/__init__.py` imports, at the version the other env files
    pin, and no other binding. Below glibc 2.34 the installer swaps in the
    older-Linux list's two pins (docs/v17_older_linux_qt_plan.md §3.3), so
    the running binding is one of the two lists' versions."""
    from block01 import qt
    assert qt.BINDING == "PySide6"
    pip = _pins("envs/fusion_mesmer/requirements-pip.txt")
    ci = _pins("envs/ci/requirements-cpu.txt")
    yml = {}
    for line in (ROOT / "envs/fusion_mesmer/environment.yml").read_text(encoding="utf-8").splitlines():
        if "==" in line:
            name, _, version = line.strip().lstrip("- ").partition("==")
            yml[name.strip().lower().replace("_", "-")] = version.strip()
    assert [n for n in pip if n.startswith(("pyqt5", "pyqt6", "pyside2"))] == []
    older = _pins("envs/fusion_mesmer/requirements-pip-older-linux.txt")
    assert sorted(older) == ["pyside6-essentials", "shiboken6"]
    assert len(set(older.values())) == 1
    for name in ("pyside6-essentials", "shiboken6"):
        assert pip.get(name) == ci.get(name) == yml.get(name), name
    assert qt.BINDING_VERSION in (pip["pyside6-essentials"], older["pyside6-essentials"])


def test_the_gpu_benchmark_records_the_binding_it_runs_on():
    """scripts/benchmark_step1_gpu_demo.py writes its environment before the
    result; it read PyQt5-only attributes and died there on PySide6."""
    import importlib.util
    import sys
    import types
    spec = importlib.util.spec_from_file_location(
        "_bench_step1_gpu_demo", ROOT / "scripts" / "benchmark_step1_gpu_demo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                    # its dataclasses look it up
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    from block01 import qt
    env = mod._environment(types.SimpleNamespace(platform="offscreen"))
    assert env["qt_binding"] == f"PySide6 {qt.BINDING_VERSION}"
    assert env["qt"] == qt.QtCore.qVersion()
    assert "pyqt" not in env
