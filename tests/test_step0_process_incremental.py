"""▶ Process is INCREMENTAL: it recomputes only what is new or changed.

Pressing Process again with nothing changed used to rerun every ticked
channel from scratch (minutes of GPU work for an identical result). The page
now records, per channel, the signature that PRODUCED its cached result --
(method, tophat_radius, cucim_sigma, patches) -- and at Process time skips
every channel whose would-be signature matches and whose patches are all
cached.

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
    for ch in channels:
        page._channel_rows[ch]["checkbox"].setChecked(True)


def _finish(page, *channels):
    """Drive the page's own slots the way a finished worker would."""
    for ch in channels:
        for p_idx in range(len(page.patches)):
            page._on_batch_patch_done(ch, p_idx, _payload())
        page._on_batch_channel_done(ch)
    page._on_batch_all_done()


def _last_channels(page):
    """The channels dict of the worker created by the last Process, or None."""
    workers = _FakeBatchWorker.created
    return dict(workers[-1].channels) if workers else None


def _run_process(page):
    """Press Process; return the channels the new worker got (None if none)."""
    before = len(_FakeBatchWorker.created)
    page._on_process_clicked()
    if len(_FakeBatchWorker.created) == before:
        return None
    return _last_channels(page)


# ── the cases ─────────────────────────────────────────────────────────────

def test_second_process_with_no_change_starts_no_worker(page):
    _tick(page, "CD3", "CD20")
    assert _run_process(page) == {"CD3": "both", "CD20": "both"}
    _finish(page, "CD3", "CD20")

    assert _run_process(page) is None, (
        "Process recomputed channels that nothing had changed")
    assert "up to date" in page._proc_status.text().lower()
    assert "2" in page._proc_status.text()
    # and the cached results are untouched
    assert page._computed_channels == {"CD3", "CD20"}
    assert set(page._preview_cache) == {("CD3", 0), ("CD3", 1),
                                        ("CD20", 0), ("CD20", 1)}
    assert page._btn_process.text() == "▶ Process"
    assert page._btn_process.isEnabled()
    assert not page._btn_stop_process.isEnabled()


def test_changing_one_channels_sigma_recomputes_only_that_channel(page):
    _tick(page, "CD3", "CD20")
    _run_process(page)
    _finish(page, "CD3", "CD20")

    tr, cs = page._resolve_channel_params("CD3")
    page._channel_params["CD3"] = {"tophat_radius": tr, "cucim_sigma": cs + 7}

    assert _run_process(page) == {"CD3": "both"}, (
        "a sigma change on CD3 must recompute CD3 and nothing else")
    # CD20's cached pixels survived; CD3's were dropped for the rerun
    assert ("CD20", 0) in page._preview_cache
    assert ("CD3", 0) not in page._preview_cache
    assert page._computed_channels == {"CD20"}


def test_changing_one_channels_method_recomputes_only_that_channel(page):
    """A "both" result covers a later TopHat or cuCIM request (same params,
    same patches), so narrowing the method is NOT a recompute. Widening it --
    a channel computed as TopHat only, now asked for cuCIM -- is, and only
    that channel runs."""
    _tick(page, "CD3", "CD20")
    page._channel_rows["CD20"]["method_cb"].setCurrentText("TopHat")
    assert _run_process(page) == {"CD3": "both", "CD20": "tophat"}
    _finish(page, "CD3", "CD20")

    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")   # covered by "both"
    assert _run_process(page) is None

    page._channel_rows["CD20"]["method_cb"].setCurrentText("cucim")   # not covered
    assert _run_process(page) == {"CD20": "cucim"}

def test_newly_checked_channel_computes_only_itself(page):
    _tick(page, "CD3")
    _run_process(page)
    _finish(page, "CD3")

    _tick(page, "CD20")

    assert _run_process(page) == {"CD20": "both"}, (
        "adding a channel must not rerun the ones already computed")
    assert ("CD3", 0) in page._preview_cache


def test_adding_a_patch_recomputes_every_selected_channel(page):
    _tick(page, "CD3", "CD20")
    _run_process(page)
    _finish(page, "CD3", "CD20")

    page.patches = list(page.patches) + [(0, 32, 32, 64)]

    assert _run_process(page) == {"CD3": "both", "CD20": "both"}, (
        "a changed patch list invalidates every channel's cached result")
    assert page._preview_cache == {}


def test_apply_still_forces_its_channel(page):
    _tick(page, "CD3")
    _run_process(page)
    _finish(page, "CD3")
    n_before = len(_FakeBatchWorker.created)

    page.current_channel = "CD3"
    page._process_current_channel()

    assert len(_FakeBatchWorker.created) == n_before + 1, (
        "Apply must always recompute its channel, even when unchanged")
    assert _last_channels(page) == {"CD3": "both"}
    assert ("CD3", 0) not in page._preview_cache


def test_apply_result_is_then_up_to_date_for_process(page):
    _tick(page, "CD3")
    _run_process(page)
    _finish(page, "CD3")

    page.current_channel = "CD3"
    page._process_current_channel()
    _finish(page, "CD3")

    assert _run_process(page) is None, (
        "the result Apply just produced must count as up to date")


def test_dataset_reset_forgets_the_signatures(page):
    _tick(page, "CD3", "CD20")
    _run_process(page)
    _finish(page, "CD3", "CD20")
    assert page._computed_signatures

    page._reset_dataset_view_state()

    assert page._computed_signatures == {}
    assert page._pending_signatures == {}
    # a channel of the NEW dataset with the same name is computed again
    _tick(page, "CD3", "CD20")
    assert _run_process(page) == {"CD3": "both", "CD20": "both"}


def test_a_both_result_covers_a_later_tophat_or_cucim_request(page):
    """Apply computes "both"; a later Process asking for tophat (same params,
    same patches) needs nothing new -- the "both" result already holds it."""
    _tick(page, "CD3")
    page._channel_rows["CD3"]["method_cb"].setCurrentText("TopHat")
    tr, cs = page._resolve_channel_params("CD3")
    page._pending_signatures["CD3"] = page._channel_signature("CD3", "both", params=(tr, cs))
    _finish(page, "CD3")

    assert _run_process(page) is None
    assert "up to date" in page._proc_status.text()

    # A different radius is a real change, "both" or not.
    page._channel_params["CD3"] = {"tophat_radius": tr + 5, "cucim_sigma": cs}
    assert _run_process(page) == {"CD3": "tophat"}
