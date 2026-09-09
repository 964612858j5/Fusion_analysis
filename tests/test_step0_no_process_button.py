"""Step0 has no Process button, and everything it used to gate still works.

Background correction reaches the user three ways, none of which is a button
labelled Process: a parameter change recomputes the channel it belongs to, the
compare panels and their prefetch fetch what they need, and Save writes the
corrected zarr. Asking for a fourth, manual step only made those three easy to
mistake for previews.

What is removed here is the ENTRY and its own state. The worker, the per-channel
signatures, the progress bar, Stop and every automatic path are untouched, and
this module says so rather than assuming it.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


class _FakeWorker(QtCore.QThread):
    created = []

    def __init__(self, loader, patches, channels, *_a, **_k):
        super().__init__()
        self.channels = dict(channels)
        _FakeWorker.created.append(self)

    def __getattr__(self, name):
        return type("S", (), {"connect": lambda *a, **k: None})()

    def isRunning(self):
        return False

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def page(tmp_path, monkeypatch, app):
    _FakeWorker.created = []
    monkeypatch.setattr(sp, "BatchProcessWorker", _FakeWorker)
    p = sp.Step0Page()
    p.loader = _GpuPathLoader()
    p.ome_path = str(tmp_path / "A.tif")
    p.output_dir = str(tmp_path)
    p.patches = [(0, 32, 0, 32)]
    p.current_patch_idx = 0
    p.nucleus_channel = "DAPI"
    p._rebuild_channel_list()
    p.current_channel = "CD3"
    return p


def _buttons(page):
    return [b.text() for b in page.findChildren(QtWidgets.QPushButton)]


def test_no_button_says_process(page):
    assert not any("process" in text.lower() for text in _buttons(page))


def test_no_hidden_button_was_kept_for_the_tests(page):
    assert not hasattr(page, "_btn_process")
    assert not hasattr(page, "_on_process_clicked")
    assert not hasattr(page, "_reset_process_button")


def test_nothing_tells_the_user_to_press_it(page):
    page._on_channel_selected_by_id("CD20")
    text = (page._preview_status.text() + " " + page._proc_status.text()).lower()
    assert "press process" not in text
    assert "click process" not in text
    assert "re-process" not in text


def test_enter_still_recomputes_the_current_channel(page):
    """The authoritative manual entry: the parameter box the user typed in."""
    page._on_dec_param_entered("tophat")

    assert _FakeWorker.created, "Enter started no correction run"
    assert _FakeWorker.created[-1].channels == {"CD3": "tophat"}


def test_the_run_controls_that_are_left_still_work(page):
    page._process_current_channel("both")
    assert page._btn_stop_process.isEnabled()

    page._on_batch_all_done()
    assert not page._btn_stop_process.isEnabled()
    assert page._process_completed is True


def test_stop_still_stops(page):
    page._process_current_channel("both")
    page._on_stop_process()
    assert page._batch_worker is not None       # the handle is not dropped


def test_save_still_builds_its_config_from_the_ticked_rows(page):
    page._channel_rows["CD3"]["checkbox"].setChecked(True)
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")

    config = page._build_config()

    decisions = config.get("channel_decisions") or {}
    assert decisions.get("CD3") == "tophat"


def test_the_compare_panels_still_get_their_parameters(page):
    """HOT and the panels read the row's effective parameters, and that path
    never went through the button."""
    radius, sigma = page._effective_correction_params("CD3")
    assert page._compare_params_for("tophat") == (int(radius),)
    assert page._compare_params_for("cucim") == (int(sigma),)
    assert page._compare_params_for("original") == ()


def test_a_parameter_change_still_marks_the_channel_stale(page):
    page._process_current_channel("both")
    for p_idx in range(len(page.patches)):
        disp = np.zeros((32, 32), np.float32)
        page._on_batch_patch_done("CD3", p_idx, {
            "original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
            "original_metrics": {"snr": 4.0, "bg_cv": 0.25},
            "tophat_metrics": {"snr": 4.0, "bg_cv": 0.25},
            "cucim_metrics": {"snr": 4.0, "bg_cv": 0.25},
            "nucleus_disp": None})
    page._on_batch_channel_done("CD3")
    page._on_batch_all_done()
    assert page._channel_is_up_to_date(
        "CD3", page._channel_signature("CD3", "both")) is True

    tr, cs = page._resolve_channel_params("CD3")
    page._channel_params["CD3"] = {"tophat_radius": tr + 4, "cucim_sigma": cs}

    assert page._channel_is_up_to_date(
        "CD3", page._channel_signature("CD3", "both")) is False
