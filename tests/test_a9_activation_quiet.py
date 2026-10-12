"""A9 application §51: the channel lists do not repaint every row when the
window gains or loses focus.

Qt's item views repaint their viewport on every window activation change.
With one widget row per channel, clicking back into the main window from the
Tissue Preview repainted ~250 widgets (30-60 ms with 20 channels) before the
click was handled -- the long responses at the first drag of the A9 runs.
The repaint is skipped only while the list's colours are the same in both
states; with a palette that tells them apart, Qt repaints as before (which
also shows this test sees the repaint it guards against).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.core.fusion_domain import FusionDomainModel  # noqa: E402
from block01.ui.widgets.channel_dock.global_dock import GlobalChannelDock  # noqa: E402
from block01.ui.widgets.channel_layer_list import ChannelLayerList  # noqa: E402

CHANNELS = [f"CH{i}" for i in range(12)]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Paints(QtCore.QObject):
    """Paint events of widgets inside `viewport` (the rows and their parts)."""

    def __init__(self, viewport):
        super().__init__()
        self.viewport = viewport
        self.count = 0

    def eventFilter(self, obj, event):  # noqa: N802
        if (event.type() == QtCore.QEvent.Paint and isinstance(obj, QtWidgets.QWidget)
                and obj is not self.viewport and self.viewport.isAncestorOf(obj)):
            self.count += 1
        return False


def _settle(app):
    for _ in range(5):
        app.processEvents()
    QtTest.QTest.qWait(50)


def _row_paints_on_activation(app, window, view):
    """Row repaints caused by one deactivate + activate of `window` (on a
    real display only once it is on screen: a window not yet exposed paints
    nothing either way)."""
    assert QtTest.QTest.qWaitForWindowExposed(window, 5000)
    _settle(app)
    spy = _Paints(view.viewport())
    app.installEventFilter(spy)
    try:
        for kind in (QtCore.QEvent.WindowDeactivate, QtCore.QEvent.WindowActivate):
            QtWidgets.QApplication.sendEvent(window, QtCore.QEvent(kind))
            _settle(app)
    finally:
        app.removeEventFilter(spy)
    return spy.count


def _global_dock(app):
    dock = GlobalChannelDock(None, FusionDomainModel(), title="")
    dock.set_channels(list(CHANNELS))
    dock.resize(460, 500)
    dock.show()
    return dock


def _inactive_differs(view):
    pal = view.palette()
    pal.setColor(QtGui.QPalette.Inactive, QtGui.QPalette.Base, QtGui.QColor(1, 2, 3))
    view.setPalette(pal)


def test_a_the_channel_dock_rows_are_not_repainted_on_activation(app):
    dock = _global_dock(app)
    try:
        view = dock.list_widget
        assert view.palette().isEqual(QtGui.QPalette.Active, QtGui.QPalette.Inactive)
        assert _row_paints_on_activation(app, dock, view) == 0
    finally:
        dock.close()


def test_b_a_palette_that_differs_when_inactive_still_repaints(app):
    dock = _global_dock(app)
    try:
        _inactive_differs(dock.list_widget)
        assert _row_paints_on_activation(app, dock, dock.list_widget) >= len(CHANNELS)
    finally:
        dock.close()


def test_c_the_layer_list_rows_are_not_repainted_on_activation(app):
    layers = ChannelLayerList()
    layers.set_channels([{"name": n} for n in CHANNELS])
    layers.resize(300, 500)
    layers.show()
    try:
        view = layers._list
        assert _row_paints_on_activation(app, layers, view) == 0
        _inactive_differs(view)
        assert _row_paints_on_activation(app, layers, view) >= len(CHANNELS)
    finally:
        layers.close()


def test_d_the_rows_still_get_the_activation_event(app):
    """Only the list's own repaint is skipped: every row widget and every
    visible widget inside it still sees the event."""
    dock = _global_dock(app)
    try:
        assert QtTest.QTest.qWaitForWindowExposed(dock, 5000)
        _settle(app)
        seen = set()

        class _Seen(QtCore.QObject):
            def eventFilter(self, obj, event):  # noqa: N802
                if event.type() == QtCore.QEvent.WindowActivate:
                    seen.add(obj)
                return False

        spy = _Seen()
        app.installEventFilter(spy)
        try:
            QtWidgets.QApplication.sendEvent(dock, QtCore.QEvent(QtCore.QEvent.WindowActivate))
        finally:
            app.removeEventFilter(spy)
        rows = list(dock.rows().values())
        assert rows and all(r in seen for r in rows)
        parts = [w for r in rows for w in r.findChildren(QtWidgets.QWidget) if w.isVisible()]
        assert parts and all(w in seen for w in parts)
    finally:
        dock.close()
