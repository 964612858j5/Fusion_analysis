"""The Results boxes start at the top of the Results frame and go down
(user ruling 2026-09-26): with room to spare the space is below them, and
with too little room they scroll as before."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _panel(app, height):
    from block01.ui.step1_presegmentation.results_panel import ResultsPanel
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    panel = ResultsPanel()
    lay.addWidget(panel, 1)
    panel.set_combos([{"combo_id": f"c{i}", "method": "stardist_nuclei_dapi",
                       "params": {"prob_thresh": 0.5}} for i in range(3)])
    host.resize(300, height)
    host.show()
    for _ in range(5):
        QtWidgets.QApplication.processEvents()
    return host, panel


def _y(panel, w):
    return w.mapTo(panel, w.rect().topLeft()).y()


def test_the_boxes_start_at_the_top_when_there_is_room(app):
    host, panel = _panel(app, 700)
    try:
        rows = list(panel._by_combo.values())
        below_summary = _y(panel, panel.summary) + panel.summary.height()
        assert _y(panel, rows[0]) - below_summary < 30          # right under the header
        assert [_y(panel, r) for r in rows] == sorted(_y(panel, r) for r in rows)
        assert panel.height() - (_y(panel, rows[-1]) + rows[-1].height()) > 200  # room below
    finally:
        host.close()


def test_the_boxes_still_scroll_when_there_is_no_room(app):
    host, panel = _panel(app, 160)
    try:
        assert panel.scroll.verticalScrollBar().isVisible()
        assert panel.scroll.height() > 60
    finally:
        host.close()
