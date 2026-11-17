"""The wheel zooms the Step0 thumbnail and the result zoom window (Qt 6).

Qt 6 took ``pos()`` off ``QWheelEvent``; both event filters still called it,
so every turn of the wheel raised AttributeError inside the filter and
zoomed nothing (seen on Windows in the Tissue Navigator, 2026-11-17).
A real wheel event is delivered to each viewport here. Synthetic data only.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("block01.qt")

from block01.qt import QtCore, QtGui, QtTest  # noqa: E402
from block01.qt.QtCore import Qt  # noqa: E402

from block01.ui.step0 import overview_panel as ovp  # noqa: E402
from block01.ui.step0 import result_grid  # noqa: E402

from test_step0_background_correction_tab import app  # noqa: E402,F401


def _wheel(widget, steps=1):
    centre = QtCore.QPointF(widget.width() / 2, widget.height() / 2)
    event = QtGui.QWheelEvent(
        centre, widget.mapToGlobal(centre), QtCore.QPoint(0, 0),
        QtCore.QPoint(0, 120 * steps), Qt.NoButton, Qt.NoModifier,
        Qt.NoScrollPhase, False)
    QtCore.QCoreApplication.sendEvent(widget, event)


def _width(vb):
    (x0, x1), _ = vb.viewRange()
    return x1 - x0


class _Loader:
    shape = (8000, 6000)
    ch_map = {"DAPI": 0}

    def channel_names(self):
        return ["DAPI"]

    def read_channel(self, *_a, **_kw):
        return np.zeros((800, 600), "float32")


def test_the_wheel_zooms_the_tissue_thumbnail(app):
    panel = ovp.OverviewPanel(_Loader(), "DAPI", lazy=True)
    panel.ds = 10
    panel.ov_h, panel.ov_w = 800, 600
    panel.full_h, panel.full_w = 8000, 6000
    panel.resize(500, 500)
    panel.show()
    QtTest.QTest.qWaitForWindowExposed(panel)
    panel.vb.setRange(QtCore.QRectF(0, 0, 600, 800), padding=0)
    QtTest.QTest.qWait(20)
    before = _width(panel.vb)
    _wheel(panel.gview.viewport())
    assert _width(panel.vb) < before * 0.95
    panel.close()


def test_the_wheel_zooms_the_result_zoom_window(app):
    rgb = np.zeros((200, 300, 3), np.uint8)
    masks = np.zeros((200, 300), np.uint32)
    dlg = result_grid.ImageZoomDialog(rgb, masks)
    dlg.show()
    QtTest.QTest.qWaitForWindowExposed(dlg)
    QtTest.QTest.qWait(20)
    before = _width(dlg.vb)
    _wheel(dlg.gv.viewport())
    assert _width(dlg.vb) < before * 0.95
    dlg.close()
