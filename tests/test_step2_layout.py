"""Block L2 (user ruling 2026-09-25): Step2's parameter panel is the left
column, sharing the channel-column width.

  * Step2's left column is the parameter panel, its right column the
    tile overview; its width is Step0's and Step1's one share, and a drag on
    Step2's handle moves the other two; no label is ever cut, the controls
    give way; the public Channels panel is not on screen in Step2 but is kept
    (still mounted there, shown again in Step1).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1920, 1080)
    w.show()
    yield w
    w.hide()
    w.close()


def _go(w, step, page):
    w._set_step_active(step)
    w._stack.setCurrentWidget(page)
    for _ in range(5):
        QtWidgets.QApplication.processEvents()


def _left(split):
    return split.sizes()[0]


def test_step2_parameters_are_the_left_column_and_the_overview_the_right(win):
    s2 = win._step2
    split = s2.channel_column_splitter()
    _go(win, 2, s2)
    assert split.count() == 2
    left, right = split.widget(0), split.widget(1)
    assert isinstance(left, QtWidgets.QScrollArea)
    titles = [b.title() for b in left.widget().findChildren(QtWidgets.QGroupBox)]
    assert "Segmentation Parameters" in titles and "Input Data" in titles
    assert s2._ov_gv.parentWidget() is right and s2._prog_bar.parentWidget() is right


def test_the_three_pages_share_one_channel_column(win):
    s2 = win._step2
    split2 = s2.channel_column_splitter()
    _go(win, 0, win._step0)
    _go(win, 1, win._stack.widget(1))
    _go(win, 2, s2)
    assert abs(_left(split2) - _left(win._step1_main_split)) <= 2
    # a drag on Step2's handle moves Step0 and Step1
    split2.setSizes([300, split2.width() - 300 - split2.handleWidth()])
    win._on_channel_column_dragged(split2)
    _go(win, 0, win._step0)
    s0 = _left(win._step0._bg_c_split)
    _go(win, 1, win._stack.widget(1))
    s1 = _left(win._step1_main_split)
    _go(win, 2, s2)
    assert abs(s0 - 300) <= 2 and abs(s1 - 300) <= 2 and abs(_left(split2) - 300) <= 2


def _cut_labels(scroll):
    inner = scroll.widget()
    cut = []
    for lbl in inner.findChildren(QtWidgets.QLabel):
        if (type(lbl) is QtWidgets.QLabel and lbl.isVisibleTo(inner) and lbl.text()
                and not lbl.wordWrap()
                and lbl.sizePolicy().horizontalPolicy() == QtWidgets.QSizePolicy.Fixed):
            m = lbl.contentsMargins()
            need = lbl.fontMetrics().horizontalAdvance(lbl.text()) + m.left() + m.right()
            if lbl.width() < need:
                cut.append((lbl.text(), lbl.width(), need))
    return cut


def test_no_label_is_cut_and_the_default_column_needs_no_scroll_bar(win):
    s2 = win._step2
    scroll = s2.channel_column_splitter().widget(0)
    _go(win, 0, win._step0)       # the app opens on Step0: its rule sets the share
    _go(win, 2, s2)
    for i in range(s2._method_combo.count()):
        s2._method_combo.setCurrentIndex(i)
        QtWidgets.QApplication.processEvents()
        assert _cut_labels(scroll) == [], s2._method_combo.itemData(i)
    s2._method_combo.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()
    # a fresh start at 1920: the shared column (Step0's opening width) holds
    # the whole panel -- no scroll bar (user ruling 2026-09-25)
    assert scroll.widget().minimumSizeHint().width() <= _left(s2.channel_column_splitter())
    assert not scroll.horizontalScrollBar().isVisible()


@pytest.mark.parametrize("size", [(1500, 950), (1920, 1080), (2560, 1440)])
def test_a_fresh_start_shows_no_scroll_bar(app, size):
    from block01.ui.main_window import MainWindow
    w = MainWindow()
    try:
        w.resize(*size)
        w.show()
        _go(w, 0, w._step0)
        _go(w, 2, w._step2)
        scroll = w._step2.channel_column_splitter().widget(0)
        assert not scroll.horizontalScrollBar().isVisible()
    finally:
        w.hide()
        w.close()


def test_the_short_names(win):
    s2 = win._step2
    labels = {lbl.text() for lbl in s2.findChildren(QtWidgets.QLabel)}
    assert {"Index:", "Source:", "Version:"} <= labels
    assert not labels & {"Segmentation Index:", "Parameter Source:", "Index method:",
                         "Parameter version:"}
    assert s2._rec_box.title() == "Recovery from .npy"
    s2._full_h, s2._full_w = 4000, 5000
    s2._update_tile_info()
    assert "VRAM" not in s2._tile_ram_lbl.text()


def test_a_row_control_gives_way_but_keeps_its_items(win):
    s2 = win._step2
    combo = s2._method_combo
    longest = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i))
                  for i in range(combo.count()))
    assert combo.minimumSizeHint().width() < longest          # the box may elide
    assert combo.view().minimumWidth() >= combo.view().sizeHintForColumn(0)  # the list does not
    assert combo.itemText(combo.currentIndex())               # the choice itself unchanged


def test_channels_are_hidden_in_step2_but_kept(win):
    s2 = win._step2
    dock = win._channel_dock
    _go(win, 2, s2)
    assert s2._channels_box.isHidden()
    assert not dock.isVisible()
    host = win._channels_host_for(2)
    assert host is not None and dock.parentWidget() is host.parentWidget()   # still mounted
    _go(win, 1, win._stack.widget(1))
    assert dock.isVisible()                                   # and shown again in Step1


def test_one_method_row_from_the_index(win):
    s2 = win._step2
    _go(win, 2, s2)
    s2._set_param_source("index")
    QtWidgets.QApplication.processEvents()
    assert s2._index_method_combo.isVisible() and not s2._method_combo.isVisible()
    s2._set_param_source("manual")
    QtWidgets.QApplication.processEvents()
    assert s2._method_combo.isVisible() and not s2._index_method_combo.isVisible()
