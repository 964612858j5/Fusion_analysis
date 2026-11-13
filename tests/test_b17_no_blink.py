"""No blink on Load (backlog 17, docs/v17_acceptance_notes_plan.md B17):

* the main window is put on GL composition before it is first shown, when
  Step0 will use its engine -- so the first GL host of a Load no longer
  makes Qt destroy and recreate the shown window (the blink, measured on
  the Mac mini 2026-11-13); without the engine nothing is done;
* a channel row's checkbox is made inside its row, never as a window of
  its own for an instant (it took the activation from the main window).

The GL case needs a real display (`BLOCK01_REQUIRE_STEP1_GPU=1`), as the
other GPU tests; the screen recording on the real machine is the
acceptance.
"""

import os

import pytest

pytest.importorskip("block01.qt")

from block01.qt import QtCore, QtWidgets  # noqa: E402
from block01.ui import gpu_warmup  # noqa: E402
from block01.ui.step0 import engine_mount  # noqa: E402

QE = QtCore.QEvent


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _TopLevelShows(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.shown = []

    def eventFilter(self, o, e):
        if e.type() == QE.Show and isinstance(o, QtWidgets.QWidget) and o.isWindow():
            self.shown.append(type(o).__name__)
        return False


def test_a_channel_rows_checkbox_is_never_a_window_of_its_own(app):
    from block01.ui.widgets.channel_dock import template
    from block01.ui.widgets.channel_dock.global_dock import GlobalChannelRow
    spy = _TopLevelShows()
    app.installEventFilter(spy)
    try:
        row = QtWidgets.QWidget()
        core = template.build_row_core(row, name="CD3")
        assert core.checkbox.parentWidget() is row and not core.checkbox.isWindow()
        assert not core.checkbox.isHidden()                       # shown with its row, as before
        hidden = QtWidgets.QWidget()
        assert template.build_row_core(hidden, show_visibility=False).checkbox.isHidden()
        GlobalChannelRow("CD8", name="CD8")                         # Step1's rows, built on Load
        app.processEvents()
    finally:
        app.removeEventFilter(spy)
    assert "QCheckBox" not in spy.shown


def test_nothing_is_done_without_the_engine_or_when_turned_off(app, monkeypatch):
    win = QtWidgets.QWidget()
    monkeypatch.setattr(engine_mount, "engine_refusal", lambda: "python")
    assert gpu_warmup.compose_with_gl_before_show(win) is None
    monkeypatch.setattr(engine_mount, "engine_refusal", lambda: "")
    monkeypatch.setenv("BLOCK01_GL_FIRST", "0")
    assert gpu_warmup.compose_with_gl_before_show(win) is None
    assert not win.findChildren(QtWidgets.QOpenGLWidget)


def test_a_failure_leaves_the_window_as_it_was(app, monkeypatch, capsys):
    win = QtWidgets.QWidget()
    monkeypatch.setattr(engine_mount, "engine_refusal", lambda: "")

    def boom(*a, **k):
        raise RuntimeError("no GL here")
    monkeypatch.setattr(QtWidgets, "QOpenGLWidget", boom)
    assert gpu_warmup.compose_with_gl_before_show(win) is None
    assert "not put on GL composition" in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("BLOCK01_REQUIRE_STEP1_GPU") != "1",
                    reason="GPU tests require BLOCK01_REQUIRE_STEP1_GPU=1")
def test_the_window_is_gl_composed_from_its_first_show_and_a_gl_host_does_not_recreate_it(
        app, monkeypatch):
    from block01.qt import QtGui
    monkeypatch.setattr(engine_mount, "engine_refusal", lambda: "")
    win = QtWidgets.QWidget()
    win.resize(400, 300)
    QtWidgets.QVBoxLayout(win).addWidget(QtWidgets.QLabel("Step0"))
    gl = gpu_warmup.compose_with_gl_before_show(win)
    assert gl is not None and gl.isHidden()
    events = []

    class Spy(QtCore.QObject):
        def eventFilter(self, o, e):
            if o is win and e.type() in (QE.WinIdChange, QE.PlatformSurface, QE.Hide):
                events.append(e.type())
            return False
    win.show()
    for _ in range(10):
        app.processEvents()
    assert win.windowHandle().surfaceType() == QtGui.QSurface.SurfaceType.OpenGLSurface
    spy = Spy()
    win.installEventFilter(spy)
    host = QtWidgets.QOpenGLWidget(win)                             # as engine_mount.start()
    host.resize(1, 1)
    host.show()
    host.grabFramebuffer()
    host.hide()
    for _ in range(10):
        app.processEvents()
    win.removeEventFilter(spy)
    assert events == [], "the shown window was recreated"
    win.close()
