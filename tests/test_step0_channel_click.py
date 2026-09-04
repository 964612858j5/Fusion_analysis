"""Clicking a channel row in Step 0 switches the page's channel.

Reported from manual testing: after a multi-channel Process only the first
channel could be viewed. Measured: a mouse click moved the list's current
row, but the page's `current_channel` did not follow. The click goes
`ChannelRowBase.mousePressEvent` -> `model.select` ->
`ChannelDock._on_model_selection`, which sets the list's current item with
signals BLOCKED, so the page's `currentRowChanged` slot never ran. The page
now listens to the model's `selection_changed`, which both a click and a
programmatic `setCurrentRow` reach.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined
with the background-correction module in one process.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401


def _page(app):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.resize(1600, 1000)
    page.show()
    QtTest.QTest.qWait(50)
    return page


def _click_row(page, ch):
    """A real mouse click on the channel's NAME, as a user does."""
    row = page._channel_rows[ch]["row_widget"]
    target = next((c for c in row.findChildren(QtWidgets.QLabel)
                   if c.text().startswith(ch)), row)
    QtTest.QTest.mouseClick(target, QtCore.Qt.LeftButton, pos=target.rect().center())
    QtTest.QTest.qWait(30)


def _finish_run(page, channels):
    disp = np.zeros((32, 32), np.float32)
    m = {"snr": 1.0, "bg_cv": 0.1}
    for ch in channels:
        page._set_channel_computing(ch)
        page._on_batch_patch_done(ch, 0, {
            "original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
            "original_metrics": m, "tophat_metrics": m, "cucim_metrics": m,
            "nucleus_disp": None})
        page._set_channel_done(ch)
    page._on_batch_all_done()


def test_a_mouse_click_on_a_row_switches_the_channel(app):
    page = _page(app)
    assert page.current_channel == "DAPI"           # the landing channel

    _click_row(page, "CD3")
    assert page.current_channel == "CD3"

    _click_row(page, "CD20")

    assert page.current_channel == "CD20"
    assert page._channel_list.currentRow() == page._channel_order.index("CD20")


def test_rows_stay_clickable_after_a_multi_channel_run(app):
    """The reported case: several channels corrected, then only the first
    one viewable."""
    page = _page(app)
    _finish_run(page, ["CD3", "CD20"])
    _click_row(page, "CD3")
    assert page.current_channel == "CD3"

    _click_row(page, "CD20")
    assert page.current_channel == "CD20"
    assert "CD20" in page._preview_status.text()
    assert page._last_payload is page._preview_cache[("CD20", 0)]

    _click_row(page, "CD3")
    assert page.current_channel == "CD3"
    assert page._last_payload is page._preview_cache[("CD3", 0)]


def test_a_programmatic_selection_still_reaches_the_page_exactly_once(app):
    page = _page(app)
    calls = []
    real = page._on_channel_row_changed
    page._on_channel_row_changed = lambda row: (calls.append(row), real(row))[1]
    # Route through the same connection the page made at construction.
    page._dock_adapter.model.selection_changed.disconnect(page._on_channel_selected_by_id)
    page._dock_adapter.model.selection_changed.connect(page._on_channel_selected_by_id)

    page._channel_list.setCurrentRow(page._channel_order.index("CD20"))
    QtTest.QTest.qWait(30)

    assert page.current_channel == "CD20"
    assert calls == [page._channel_order.index("CD20")]


def test_clicking_the_current_row_again_does_not_rerun_the_handler(app):
    """The model de-duplicates: a second click on the selected channel must
    not, for instance, start a second on-demand computation."""
    page = _page(app)
    _click_row(page, "CD20")
    calls = []
    real = page._on_channel_row_changed
    page._on_channel_row_changed = lambda row: (calls.append(row), real(row))[1]
    page._dock_adapter.model.selection_changed.disconnect(page._on_channel_selected_by_id)
    page._dock_adapter.model.selection_changed.connect(page._on_channel_selected_by_id)

    _click_row(page, "CD20")

    assert calls == []
    assert page.current_channel == "CD20"


def test_clicking_the_nucleus_row_keeps_the_displayed_channel(app):
    """DAPI is a reference channel: its row hands the Intensity window DAPI's
    mapping and changes nothing else -- the page keeps showing the marker."""
    page = _page(app)
    _click_row(page, "CD20")

    _click_row(page, "DAPI")

    assert page._dock_adapter.model.selected() == "DAPI"
    assert page.current_channel == "CD20"
    assert page._inspector_channel == "DAPI"


def test_the_page_no_longer_depends_on_current_row_changed(app):
    """If the list's own signal were still the page's source, blocking it
    (as the dock does on a click) would lose the selection."""
    page = _page(app)
    page._channel_list.blockSignals(True)
    try:
        page._dock_adapter.model.select("CD20")
    finally:
        page._channel_list.blockSignals(False)
    assert page.current_channel == "CD20"
