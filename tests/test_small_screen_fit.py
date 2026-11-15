"""W2b: every step fits a 1080p screen at 150% (1280 x 720 logical px).

The Windows laptop acceptance (2026-11-14) showed each page's bottom row --
Step2's Run / Stop, Step0's Save, Step4's Extract -- below the screen: the
page stack's minimum was the TALLEST page's, so every step asked for 790 px
of page. Now the window asks for the page on screen, and Step4's column
scrolls with its buttons kept in view.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("block01.qt")

from block01.qt import QtWidgets  # noqa: E402

#: 720 logical px less the taskbar (48 / 1.5) and a title bar, rounded down
FITS = 640


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: None))


def test_every_reachable_step_fits_720_logical_px(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    try:
        w.resize(1280, FITS)
        w.show()
        app.processEvents()
        # the pages the step bar reaches: Step0, Regions, Step1..Step4
        for page in (w._step0, w._regions, w._stack.widget(1), w._step2, w._step3, w._step4):
            w._stack.setCurrentWidget(page)
            app.processEvents()
            hint = w.minimumSizeHint()
            assert hint.height() <= FITS, (type(page).__name__, hint.height())
            assert hint.width() <= 1280, (type(page).__name__, hint.width())
        # Step4's buttons stay in view while its column scrolls
        w._stack.setCurrentWidget(w._step4)
        app.processEvents()
        page = w._step4
        btn = page._btn_run
        assert btn.isVisible()
        bottom = btn.mapTo(page, btn.rect().bottomLeft()).y()
        assert 0 < bottom <= page.height()
        assert page._column_scroll.widget().minimumSizeHint().height() > \
            page._column_scroll.viewport().height()          # the column really scrolls here
    finally:
        w.close()
        w.deleteLater()
        app.processEvents()
