"""Correction runs are INCREMENTAL: what is already computed is not redone.

The page records, per channel, the signature that PRODUCED its cached result
-- (method, tophat_radius, cucim_sigma, patches) -- so it can answer "is this
channel up to date?" without rerunning it. That answer drives the row glyphs
and keeps a recompute to the channel that actually changed.

The ▶ Process button that used to read this is gone: a parameter change
recomputes the channel it belongs to (Enter), the compare panels and HOT fetch
what they need, and Save writes the corrected zarr. The signature machinery
below is what all of those still rely on.

Own module (not appended to test_step0_background_correction_tab.py): that
file already builds enough `Step0Page` instances to sit at the edge of the
known pyqtgraph/offscreen segfault.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore  # noqa: E402

from block01.ui.step0 import step0_page as sp  # noqa: E402

from test_step0_background_correction_tab import (  # noqa: E402
    _GpuPathLoader,
    app,            # noqa: F401  (pytest fixture)
)


# ── fakes ─────────────────────────────────────────────────────────────────

class _Signal:
    def connect(self, *_a, **_k):
        pass


class _FakeBatchWorker(QtCore.QThread):
    """Records the `channels` dict it was constructed with; never runs.

    A REAL QThread because `_watch_production_worker` binds
    `QThread.finished` off the base class explicitly.
    """

    created = []          # every instance, in construction order

    def __init__(self, loader, patches, channels, nucleus_channel,
                 tophat_radius, cucim_sigma, channel_params=None,
                 max_gpu_workers=4, parent=None):
        super().__init__()
        self.channels = dict(channels)
        self.patches = list(patches)
        self.channel_params = dict(channel_params or {})
        self.started_flag = False
        _FakeBatchWorker.created.append(self)

    def __getattr__(self, name):
        # channel_patch_done / channel_done / all_done / progress /
        # error_signal / canceled are all connected before start().
        return _Signal()

    def isRunning(self):
        return False

    def start(self):
        self.started_flag = True

    def stop(self):
        pass


@pytest.fixture
def page(tmp_path, monkeypatch, app):
    _FakeBatchWorker.created = []
    monkeypatch.setattr(sp, "BatchProcessWorker", _FakeBatchWorker)
    p = sp.Step0Page()
    p.loader = _GpuPathLoader()
    p.ome_path = str(tmp_path / "A.tif")
    p.output_dir = str(tmp_path)
    p.patches = [(0, 32, 0, 32), (32, 64, 0, 32)]
    p.current_patch_idx = 0
    p.nucleus_channel = "DAPI"
    p._rebuild_channel_list()
    p.current_channel = "CD3"
    return p


# ── helpers ───────────────────────────────────────────────────────────────

def _payload():
    disp = np.zeros((32, 32), np.float32)
    metrics = {"snr": 4.0, "bg_cv": 0.25}
    return {"original_disp": disp, "tophat_disp": disp, "cucim_disp": disp,
            "original_metrics": metrics, "tophat_metrics": metrics,
            "cucim_metrics": metrics, "nucleus_disp": None}


def _tick(page, *channels):
    """Show the channels. DISPLAY only, since B4-A."""
    for ch in channels:
        page._channel_rows[ch]["checkbox"].setChecked(True)


def _assign(page, method, *channels):
    """Give the channels a FINAL correction decision, through the combo the
    user uses. A channel with no decision is `original`, and Original has no
    parameters to go stale against."""
    for ch in channels:
        page._channel_rows[ch]["method_cb"].setCurrentText(method)


def _finish(page, *channels):
    """Drive the page's own slots the way a finished worker would."""
    for ch in channels:
        for p_idx in range(len(page.patches)):
            page._on_batch_patch_done(ch, p_idx, _payload())
        page._on_batch_channel_done(ch)
    page._on_batch_all_done()


def _last_channels(page):
    """The channels dict of the worker created by the last run, or None."""
    workers = _FakeBatchWorker.created
    return dict(workers[-1].channels) if workers else None


def _recompute(page, channel, method="both"):
    """The surviving entry point: Enter in a parameter box recomputes the
    channel the user is on. Returns the channels dict the worker got."""
    before = len(_FakeBatchWorker.created)
    page.current_channel = channel
    page._process_current_channel(method)
    if len(_FakeBatchWorker.created) == before:
        return None
    return _last_channels(page)


def _up_to_date(page, channel, method=None):
    method = method or page._channel_row_method(channel)
    return page._channel_is_up_to_date(
        channel, page._channel_signature(channel, method))


# ── the cases ─────────────────────────────────────────────────────────────

def test_a_finished_run_leaves_its_channel_up_to_date(page):
    _tick(page, "CD3")
    assert _recompute(page, "CD3") == {"CD3": "both"}
    _finish(page, "CD3")

    assert _up_to_date(page, "CD3") is True
    assert page._computed_channels == {"CD3"}
    assert set(page._preview_cache) == {("CD3", 0), ("CD3", 1)}
    assert not page._btn_stop_process.isEnabled()


def test_a_sigma_change_makes_only_that_channel_stale(page):
    _tick(page, "CD3", "CD20")
    _assign(page, "cucim", "CD3", "CD20")   # sigma is cuCIM's parameter
    _recompute(page, "CD3")
    _finish(page, "CD3")
    _recompute(page, "CD20")
    _finish(page, "CD20")

    tr, cs = page._resolve_channel_params("CD3")
    page._channel_params["CD3"] = {"tophat_radius": tr, "cucim_sigma": cs + 7}

    assert _up_to_date(page, "CD3") is False
    assert _up_to_date(page, "CD20") is True


def test_recomputing_one_channel_keeps_the_others_pixels(page):
    _tick(page, "CD3", "CD20")
    _recompute(page, "CD3")
    _finish(page, "CD3")
    _recompute(page, "CD20")
    _finish(page, "CD20")

    assert _recompute(page, "CD3") == {"CD3": "both"}

    assert ("CD20", 0) in page._preview_cache      # untouched
    assert ("CD3", 0) not in page._preview_cache   # dropped for the rerun
    assert page._computed_channels == {"CD20"}


def test_a_both_result_covers_a_later_tophat_or_cucim_request(page):
    """A channel computed as "both" is up to date for a narrower request with
    the same parameters and patches; widening it is a real change."""
    _tick(page, "CD3")
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")
    tr, cs = page._resolve_channel_params("CD3")
    page._pending_signatures["CD3"] = page._channel_signature(
        "CD3", "both", params=(tr, cs))
    _finish(page, "CD3")

    assert _up_to_date(page, "CD3", "tophat") is True
    assert _up_to_date(page, "CD3", "cucim") is True

    page._channel_params["CD3"] = {"tophat_radius": tr + 5, "cucim_sigma": cs}
    assert _up_to_date(page, "CD3", "tophat") is False


def test_a_changed_patch_list_invalidates_every_channel(page):
    _tick(page, "CD3", "CD20")
    _recompute(page, "CD3")
    _finish(page, "CD3")
    _recompute(page, "CD20")
    _finish(page, "CD20")

    page.patches = list(page.patches) + [(0, 32, 32, 64)]

    assert _up_to_date(page, "CD3") is False
    assert _up_to_date(page, "CD20") is False


def test_a_recompute_always_runs_its_own_channel(page):
    """Pressing Enter is an instruction, not a question: it recomputes the
    channel even when nothing about it has changed."""
    _tick(page, "CD3")
    _recompute(page, "CD3")
    _finish(page, "CD3")
    assert _up_to_date(page, "CD3") is True

    assert _recompute(page, "CD3") == {"CD3": "both"}
    assert ("CD3", 0) not in page._preview_cache


def test_dataset_reset_forgets_the_signatures(page):
    _tick(page, "CD3", "CD20")
    _recompute(page, "CD3")
    _finish(page, "CD3")
    assert page._computed_signatures

    page._reset_dataset_view_state()

    assert page._computed_signatures == {}
    assert page._pending_signatures == {}
    assert _up_to_date(page, "CD3") is False


def test_no_process_button_survives(page):
    """The entry the user pressed is gone, and nothing kept a hidden one."""
    assert not hasattr(page, "_btn_process")
    assert not hasattr(page, "_on_process_clicked")
    assert not hasattr(page, "_reset_process_button")
