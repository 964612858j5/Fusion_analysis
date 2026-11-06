"""One Enter must refresh the Step0 parameter view exactly once, on the REAL page.

Block RT (docs/v17_step0_runtime_kernels.md §5). This module drives a real
`Step0Page`, its real `QSpinBox`es and **real key events**: the earlier
version of these tests replaced `_on_dec_param_changed` on a stub, which
proved the signal wiring but not what the product does with an Enter.

What is measured
----------------
* the number of *view refreshes* (`_sync_full_image_param` / `_sync_compare_params`),
* the number of *compute-path* entries (`_process_current_channel`),
* that the row's state bookkeeping still runs for a duplicate commit,
* that TopHat and cuCIM editing stay independent.

The keyboard path matters: `QTest.keyClick(box, Return)` sends the key to the
spin box (what a focused spin box receives from a real keyboard). Sending it
to the box's LINE EDIT instead makes QLineEdit and QAbstractSpinBox both emit
`returnPressed` -- a harness artefact that does not happen on the real path
(measured, see docs §5).

The final-picture identity of these commits is covered end to end by the A9
runs (`a9.s0_first` requires the engine to publish the wanted selection), not
here: this page has no viewer.
"""

import pytest

from block01.qt import QtCore, QtWidgets

from block01.ui.step0.step0_page import Step0Page

from block01.qt import QtTest

QTest = QtTest.QTest


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["pytest-step0-enter"])
    return app


class _Page:
    """A real page with its real spin boxes, its collaborators recorded."""

    def __init__(self):
        self.page = Step0Page()
        self.page.current_channel = "CD3"
        self.page.ome_path = "/tmp/s0-test.ome.tif"
        self.page._channel_params.clear()
        self.page._dec_param_applied = None
        self.calls = []
        self.page._sync_full_image_param = (
            lambda method=None: self.calls.append(("sync_full", method)))
        self.page._sync_compare_params = (
            lambda method=None: self.calls.append(("sync_cmp", method)))
        self.page._process_current_channel = (
            lambda method=None: self.calls.append(("process", method)))
        self.states = []
        self.page._refresh_channel_state = (
            lambda ch: self.states.append(ch))

    @property
    def radius(self):
        return self.page._dec_radius

    @property
    def sigma(self):
        return self.page._dec_sigma

    def type_and_enter(self, box, text):
        """Type into the box's editor and press Enter on the BOX (real path)."""
        box.setFocus()
        box.selectAll()
        QTest.keyClicks(box, str(text))
        QTest.keyClick(box, QtCore.Qt.Key_Return)

    def press_enter(self, box):
        QTest.keyClick(box, QtCore.Qt.Key_Return)


def test_enter_with_a_new_value_refreshes_the_view_once():
    p = _Page()
    p.type_and_enter(p.radius, 63)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]
    assert p.radius.value() == 63


def test_a_second_enter_with_the_same_value_refreshes_nothing_but_still_computes():
    p = _Page()
    p.type_and_enter(p.radius, 63)
    p.calls.clear()
    p.press_enter(p.radius)
    # no second viewport request; the channel's compute path is still reached
    assert p.calls == [("process", "tophat")]


def test_a_duplicate_commit_still_refreshes_the_row_state():
    p = _Page()
    p.type_and_enter(p.radius, 63)
    p.states.clear()
    p.press_enter(p.radius)
    assert p.states == ["CD3"]


def test_the_arrow_keys_refresh_the_view_without_a_batch():
    p = _Page()
    p.radius.setValue(15)
    p.calls.clear()
    p.radius.stepUp()
    assert p.calls == [("sync_full", "tophat")]
    assert p.radius.value() == 16


def test_editing_radius_does_not_touch_the_gaussian_preview():
    p = _Page()
    p.type_and_enter(p.radius, 40)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]
    p.calls.clear()
    p.type_and_enter(p.sigma, 90)
    assert p.calls == [("sync_full", "cucim"), ("process", "cucim")]
    assert p.radius.value() == 40


def test_another_channel_with_the_same_numbers_is_a_new_state():
    p = _Page()
    p.type_and_enter(p.radius, 63)
    p.calls.clear()
    p.page.current_channel = "CD8"
    p.type_and_enter(p.radius, 63)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]
    p.calls.clear()
    p.page.current_channel = "CD3"
    p.type_and_enter(p.radius, 63)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]


def test_a_global_parameter_change_is_not_mistaken_for_a_duplicate():
    """The channel has no override; the global default moves underneath it."""
    p = _Page()
    p.type_and_enter(p.radius, 63)
    p.page._channel_params.pop("CD3", None)     # back to inheriting the global
    p.page._tophat_slider.setValue(21)
    p.calls.clear()
    p.press_enter(p.radius)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]


def test_the_same_channel_and_numbers_in_another_dataset_refresh():
    p = _Page()
    p.type_and_enter(p.radius, 63)
    p.calls.clear()
    p.page.ome_path = "/tmp/s0-other.ome.tif"
    p.press_enter(p.radius)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]


def test_with_patches_enter_reaches_the_batch_entry_once():
    p = _Page()
    p.page.patches = [(0, 0, 256, 256)]
    p.type_and_enter(p.radius, 63)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]


def test_without_patches_enter_reaches_the_same_entry_once():
    p = _Page()
    p.page.patches = []
    p.type_and_enter(p.radius, 63)
    assert p.calls == [("sync_full", "tophat"), ("process", "tophat")]
