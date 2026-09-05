"""Enter in a Per-Channel Decision param box recomputes THAT channel.

The two boxes -- TopHat radius and cuCIM sigma -- are where a channel's own
correction parameters are tuned. Typing in them records the number and marks
the row stale; that is deliberate, so that a half-typed value or one being
tuned by eye against the compare panels never puts the GPU to work behind
the user's back. Enter is the keystroke that says the tuning is finished,
and it had stopped meaning anything at all: `_on_dec_param_entered` recorded
the value and returned, so the box said "press Enter or Process" and Enter
did nothing.

These drive the keyboard, not the handler. `QTest.keyClicks` types into the
real, shown, enabled `QSpinBox` the user sees and `QTest.keyClick` presses
the real Return and the real keypad Enter, because the failure this module
exists for lives in exactly that gap: the signal was connected, the handler
was reachable, and calling it directly proved nothing about what the
keystroke did. What is asserted is what the worker was CONSTRUCTED with --
the new number, once, through the page's one authoritative single-channel
path.

Own module, and it tears its pages down: every test here builds a shown
`Step0Page`, which is the pyqtgraph/offscreen abort this suite already
knows about (see `test_step0_intensity_window.py`).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtTest, QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

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
    """Records what it was constructed with; never runs.

    A REAL QThread because `_watch_production_worker` binds
    `QThread.finished` off the base class explicitly.
    """

    created = []
    events = []           # the release -> watch -> start ordering

    def __init__(self, loader, patches, channels, nucleus_channel,
                 tophat_radius, cucim_sigma, channel_params=None,
                 max_gpu_workers=4, parent=None):
        super().__init__()
        self.channels = dict(channels)
        self.patches = list(patches)
        self.channel_params = dict(channel_params or {})
        self.global_radius = tophat_radius
        self.global_sigma = cucim_sigma
        self.started_flag = False
        _FakeBatchWorker.created.append(self)

    def __getattr__(self, name):
        return _Signal()

    def isRunning(self):
        return False

    def start(self):
        self.started_flag = True
        _FakeBatchWorker.events.append("start")

    def stop(self):
        pass


_LIVE_PAGES = []


@pytest.fixture(autouse=True)
def _tear_pages_down_before_the_flush():
    """Close this module's shown pages BEFORE the repo-wide fixture flushes
    the event loop -- the same deterministic teardown the dataset-switch and
    intensity-window modules use, and for the same abort."""
    yield
    pages, _LIVE_PAGES[:] = list(_LIVE_PAGES), []
    for page in pages:
        try:
            teardown = getattr(page, "teardown", None)
            if callable(teardown):
                teardown()
            page.close()
            page.deleteLater()
        except RuntimeError:
            pass
    import gc
    gc.collect()


@pytest.fixture
def page(tmp_path, monkeypatch, app):
    _FakeBatchWorker.created = []
    _FakeBatchWorker.events = []
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
    p._update_decision_ui()

    # The keystroke has to go to a widget that is on screen and enabled:
    # a test that types into a hidden box proves nothing about the box the
    # user is looking at.
    p.resize(1500, 1000)
    p.show()
    QtTest.QTest.qWaitForWindowExposed(p)
    _LIVE_PAGES.append(p)

    # release -> watch -> start, recorded in order.
    orig_release = p._release_explore_for_production
    orig_watch = p._watch_production_worker

    def release(*a, **kw):
        _FakeBatchWorker.events.append("release")
        return orig_release(*a, **kw)

    def watch(*a, **kw):
        _FakeBatchWorker.events.append("watch")
        return orig_watch(*a, **kw)

    p._release_explore_for_production = release
    p._watch_production_worker = watch
    return p


# ── helpers ───────────────────────────────────────────────────────────────

def _type(box, text):
    """Type `text` into the real spin box, replacing what is there.

    No Enter: this is the edit alone, which is the state the contract is
    about -- the number is on screen and nothing has been computed yet.
    """
    assert box.isVisible(), "the box the user types into is not on screen"
    assert box.isEnabled(), "the box the user types into is disabled"
    box.setFocus()
    box.selectAll()
    QtTest.QTest.keyClicks(box, text)


def _enter(box, key=Qt.Key_Return):
    QtTest.QTest.keyClick(box, key)


def _workers():
    return _FakeBatchWorker.created


def _last_params(page, ch="CD3"):
    """The per-channel params the last worker was CONSTRUCTED with."""
    assert _workers(), "no worker was ever started"
    return dict(_workers()[-1].channel_params.get(ch) or {})


# ── the two boxes, the two Enters ─────────────────────────────────────────

def test_enter_in_the_radius_box_recomputes_with_the_new_radius(page):
    _type(page._dec_radius, "77")
    assert not _workers(), "typing alone started a run"

    _enter(page._dec_radius)

    assert page._dec_radius.value() == 77
    assert page._channel_params["CD3"]["tophat_radius"] == 77
    assert len(_workers()) == 1, "Enter did not start exactly one run"
    assert _last_params(page)["tophat_radius"] == 77, \
        "the worker got the value from before the edit"
    assert _workers()[0].started_flag is True
    assert _workers()[0].channels == {"CD3": "both"}


def test_enter_in_the_sigma_box_recomputes_with_the_new_sigma(page):
    _type(page._dec_sigma, "31")
    assert not _workers()

    _enter(page._dec_sigma)

    assert page._dec_sigma.value() == 31
    assert page._channel_params["CD3"]["cucim_sigma"] == 31
    assert len(_workers()) == 1
    assert _last_params(page)["cucim_sigma"] == 31


@pytest.mark.parametrize("key", [Qt.Key_Return, Qt.Key_Enter])
def test_both_enter_keys_recompute(page, key):
    """The keypad's Enter and the main Return are the same request."""
    _type(page._dec_radius, "64")
    _enter(page._dec_radius, key)

    assert len(_workers()) == 1
    assert _last_params(page)["tophat_radius"] == 64


# ── ...and nothing else does ──────────────────────────────────────────────

def test_editing_without_enter_starts_nothing(page):
    """The existing contract, unchanged: a number being tuned is not a
    request to compute.

    Both halves of "tuning": the number still sitting uncommitted in the
    box, and the number after Qt has committed it (the boxes have
    `keyboardTracking` off, so leaving the box is what commits an edit that
    was not ended with Enter). Neither is a run.
    """
    _type(page._dec_radius, "90")
    QtWidgets.QApplication.instance().processEvents()
    assert page._dec_radius.lineEdit().text() == "90"
    assert not _workers(), "an uncommitted edit started a run"

    # Leaving the box commits it -- and records it, so the row goes stale
    # and a later Process sees the new number.
    page._dec_sigma.setFocus()
    QtWidgets.QApplication.instance().processEvents()
    assert page._dec_radius.value() == 90
    assert page._channel_params["CD3"]["tophat_radius"] == 90
    assert not _workers(), "a committed edit started a run"


def test_one_enter_starts_exactly_one_run(page):
    """`returnPressed` and `editingFinished` both fire on Return. Only one
    of them may reach the worker, or every Enter costs two runs of GPU
    work and the second one races the first."""
    _type(page._dec_radius, "55")
    _enter(page._dec_radius)
    assert len(_workers()) == 1

    # A second Enter with nothing retyped is a second deliberate request,
    # and one run -- not two.
    _enter(page._dec_radius)
    assert len(_workers()) == 2


def test_enter_keeps_the_release_watch_start_ordering(page):
    """The GPU is handed over before the worker is watched, and watched
    before it is started: a worker started before it is watched can finish
    unobserved."""
    _type(page._dec_sigma, "21")
    _enter(page._dec_sigma)

    assert _FakeBatchWorker.events == ["release", "watch", "start"]


def test_enter_while_a_production_run_is_busy_starts_nothing(page, monkeypatch):
    """The existing busy guard is not bypassed."""
    monkeypatch.setattr(page, "production_correction_busy",
                        lambda: "patch background correction")
    shown = []
    monkeypatch.setattr(sp.QMessageBox, "information",
                        lambda *a, **kw: shown.append(a[1:]))

    _type(page._dec_radius, "44")
    _enter(page._dec_radius)

    assert not _workers()
    assert shown, "the busy guard said nothing"


def test_enter_with_no_patches_starts_nothing(page, monkeypatch):
    shown = []
    monkeypatch.setattr(sp.QMessageBox, "information",
                        lambda *a, **kw: shown.append(a[1:]))
    page.patches = []

    _type(page._dec_radius, "44")
    _enter(page._dec_radius)

    assert not _workers()
    assert shown


def test_enter_on_the_nucleus_channel_starts_nothing(page):
    page.current_channel = page.nucleus_channel
    _enter(page._dec_radius)
    assert not _workers()


def test_an_out_of_range_number_is_clamped_before_the_worker_sees_it(page):
    """The validator's rule, not a half-edited string. Whatever the box
    would accept is what the run uses, and it is always a number."""
    lo, hi = page._dec_radius.minimum(), page._dec_radius.maximum()
    _type(page._dec_radius, str(hi + 500))
    _enter(page._dec_radius)

    used = _last_params(page)["tophat_radius"]
    assert isinstance(used, int)
    assert lo <= used <= hi
    assert used == page._dec_radius.value()


def test_the_recompute_leaves_the_camera_and_the_patch_selection_alone(page):
    """A parameter run is not a navigation. The thumbnail must not move and
    the selected patch must not change under it."""
    panel = page.overview
    panel.ds = 10
    panel.ov_h, panel.ov_w = 800, 600
    panel.full_h, panel.full_w = 8000, 6000
    panel.vb.setRange(QtCore.QRectF(100, 100, 200, 200), padding=0)
    QtWidgets.QApplication.instance().processEvents()
    camera = tuple(tuple(a) for a in panel.vb.viewRange())
    patch_idx = page.current_patch_idx

    _type(page._dec_radius, "66")
    _enter(page._dec_radius)

    assert len(_workers()) == 1
    assert tuple(tuple(a) for a in panel.vb.viewRange()) == camera
    assert page.current_patch_idx == patch_idx


def test_the_new_parameters_are_what_compare_and_full_image_preview(page):
    """The compare panels preview the row's numbers, so the recompute and
    the pictures beside it are talking about the same parameters."""
    _type(page._dec_radius, "73")
    _type(page._dec_sigma, "29")
    _enter(page._dec_sigma)

    assert page._channel_params["CD3"] == {"tophat_radius": 73,
                                           "cucim_sigma": 29}
    assert _last_params(page) == {"tophat_radius": 73, "cucim_sigma": 29}


def test_enter_goes_through_the_pages_one_recompute_path(page, monkeypatch):
    """`_process_current_channel` owns the busy guard, the cache
    invalidation, the pending signature and the hand-off ordering. Enter
    calls it; it does not build a worker of its own beside it."""
    calls = []
    orig = page._process_current_channel
    monkeypatch.setattr(page, "_process_current_channel",
                        lambda: (calls.append(1), orig())[1])

    _type(page._dec_radius, "58")
    _enter(page._dec_radius)

    assert calls == [1], "Enter did not use the authoritative path exactly once"
    assert len(_workers()) == 1


def test_enter_invalidates_the_channels_cached_result(page):
    """The old result was made with other numbers, so it may not be shown
    as this run's answer."""
    page._preview_cache[("CD3", 0)] = object()
    page._computed_channels.add("CD3")

    _type(page._dec_radius, "81")
    _enter(page._dec_radius)

    assert ("CD3", 0) not in page._preview_cache
    assert "CD3" not in page._computed_channels
    assert page._pending_signatures.get("CD3") is not None
