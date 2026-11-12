"""A viewer rebuilt while it is on screen leaves no window behind
(docs/v17_stray_viewer_window_plan.md, backlog 13): `Step1ViewerHost._show`
used to detach the placeholder it had just put into the visible layout, and
Qt's queued show then made it a window of its own ("No image loaded").

Synthetic stacks (a label as the view); the host sits in a shown window.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")

from block01.qt import QtWidgets  # noqa: E402
from block01.ui import step1_viewer_host as H  # noqa: E402


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Stack:
    def __init__(self, name):
        self.view = QtWidgets.QLabel(name)

    def teardown(self, wait_for_floor=False):
        pass


class _Table:
    def missing(self):
        return []

    def identity_token(self):
        return ""


def _pump(app, n=10):
    for _ in range(n):
        app.processEvents()


@pytest.fixture
def host(app, monkeypatch):
    monkeypatch.setattr(H.sources, "Step1SourceTable", lambda **k: _Table())
    win = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(win)
    h = H.Step1ViewerHost(stack_factory=lambda path, ch, table, host: _Stack(path))
    h.refresh_status = lambda: None
    lay.addWidget(h)
    win.resize(930, 680)
    win.show()
    _pump(app)
    yield h, win
    win.close()
    h._placeholder.deleteLater()
    _pump(app)


def _stray(win):
    return [w for w in QtWidgets.QApplication.topLevelWidgets() if w is not win and w.isVisible()]


def test_a_rebuild_on_screen_leaves_no_window(app, host):
    h, win = host
    h.open("/a.ome.tif", "DAPI")
    _pump(app)
    stack = h.open("/b.ome.tif", "DAPI")          # another dataset: teardown, placeholder, the new view
    _pump(app)
    assert _stray(win) == []
    assert stack.view.isVisible() and stack.view.parent() is h
    assert h._placeholder.parent() is None and not h._placeholder.isVisible()


def test_the_placeholder_alone_is_shown_inside_the_host(app, host):
    h, win = host
    h.open("/a.ome.tif", "DAPI")
    _pump(app)
    h.teardown()
    _pump(app)
    assert _stray(win) == []
    assert h._placeholder.parent() is h and h._placeholder.isVisible()
