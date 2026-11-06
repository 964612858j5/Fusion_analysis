"""Enter on a parameter, with patch bookmarks: ONE batch, with the right numbers.

Block RT (docs/v17_step0_runtime_kernels.md §5). `_on_dec_param_entered`
reaches `_process_current_channel`, and with patch bookmarks that is the
batch path (`BatchProcessWorker`). The Enter de-duplication must not change
that: one Enter == one batch, and the batch must be handed the channel's
effective `(tophat_radius, cucim_sigma)` pair, not the global sliders.

The worker is replaced by a QThread stub so the test does not correct
anything (and does not need a GPU): what is verified is that the product
starts exactly one worker, with what arguments, and that the no-patch case
still says "the viewport preview does not need a patch" instead of starting
one.
"""

import pytest

from block01.qt import QtCore, QtWidgets

from block01.ui.step0 import step0_page as sp
from block01.ui.step0.step0_page import Step0Page

from block01.qt import QtTest

QTest = QtTest.QTest


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["pytest-step0-patch"])
    return app


class _BatchSpy(QtCore.QThread):
    """The production worker's signal surface, with nothing to do."""

    channel_patch_done = QtCore.Signal(object)
    channel_done = QtCore.Signal(object, object)
    all_done = QtCore.Signal()
    progress = QtCore.Signal(int, int)
    error_signal = QtCore.Signal(str)
    canceled = QtCore.Signal()

    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.args = args
        self.kwargs = kwargs
        _BatchSpy.instances.append(self)

    def run(self):                       # nothing to correct in this test
        return


@pytest.fixture()
def page(monkeypatch):
    p = Step0Page()
    p.current_channel = "CD3"
    p.nucleus_channel = "DAPI"
    p.loader = object()
    p._channel_order = ["CD3", "DAPI"]
    p.base_params_seen = []
    _BatchSpy.instances = []
    monkeypatch.setattr(sp, "BatchProcessWorker", _BatchSpy)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: pytest.fail("a Busy dialog was shown"))
    yield p
    for w in _BatchSpy.instances:
        w.wait(2000)


def _enter(p, box, text):
    box.setFocus()
    box.selectAll()
    QTest.keyClicks(box, str(text))
    QTest.keyClick(box, QtCore.Qt.Key_Return)


def test_enter_with_patches_starts_exactly_one_batch(page):
    page.patches = [(0, 0, 256, 256)]
    page._tophat_slider.setValue(15)
    page._cucim_slider.setValue(50)
    _enter(page, page._dec_radius, 63)
    assert len(_BatchSpy.instances) == 1, "one Enter must start one batch"
    worker = _BatchSpy.instances[0]
    # the effective pair the page resolved, not the global sliders
    assert worker.kwargs.get("channel_params") == {"CD3": {"tophat_radius": 63,
                                                           "cucim_sigma": 50}}
    assert worker.args[2] == {"CD3": "tophat"}          # one method, not both
    assert worker.args[0] is page.loader
    assert worker.args[1] == [(0, 0, 256, 256)]


def test_enter_without_patches_starts_no_batch_and_says_why(page):
    page.patches = []
    _enter(page, page._dec_radius, 63)
    assert _BatchSpy.instances == []
    text = page._decision_status.text()
    assert "does not require a patch" in text


def test_editing_the_gaussian_box_batches_the_gaussian_method(page):
    page.patches = [(0, 0, 128, 128)]
    page._tophat_slider.setValue(15)
    page._cucim_slider.setValue(50)
    _enter(page, page._dec_sigma, 90)
    assert len(_BatchSpy.instances) == 1
    assert _BatchSpy.instances[0].args[2] == {"CD3": "cucim"}
    assert _BatchSpy.instances[0].kwargs["channel_params"] == {
        "CD3": {"tophat_radius": 15, "cucim_sigma": 90}}
