"""Step1 patch loaders are retired, never waited for
(docs/v17_patch_reader_versions_plan.md §1.2, §4):

  * a REAL `PreviewLoaderThread` held inside corrected product A while a
    Save sets B and the patches are asked again: the stop returns at once,
    the patch ends up holding B's pixels, and the retired read's late
    answers change nothing;
  * a retired loader is released on its `finished` and touches nothing of
    its replacement; a new request for the same patch starts at once;
  * close refuses and retries by itself while a retired loader lives;
  * the accepted handoff asks the selected patch again AFTER the viewer
    rebind (a failed rebind puts the patch view back).
"""

import os
import threading

import numpy as np
import pytest

pytest.importorskip("PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from block01.core import io_loader  # noqa: E402
from block01.core.io_loader import OMETIFFLoader  # noqa: E402
from test_step1_fusion_isolation import _window  # noqa: E402

H, W = 64, 80
A, B = 111.0, 222.0


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    root = tmp_path_factory.mktemp("retire")
    path = root / "s.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(np.full((2, H, W), 7, np.uint16), tile=(32, 32),
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD3"]}})
    stores = {}
    for name, value in (("A", A), ("B", B)):
        z = zarr.open_group(str(root / f"{name}.zarr"), mode="w")
        z.attrs["mode"] = "full"
        z.create_dataset("CD3", data=np.full((H, W), value, np.float32), chunks=(32, 32))
        stores[name] = str(root / f"{name}.zarr")
    return str(path), stores


def _until(app, cond, seconds=8.0):
    t = QtCore.QElapsedTimer()
    t.start()
    while not cond() and t.elapsed() < seconds * 1000:
        QtTest.QTest.qWait(20)
    app.processEvents()
    return cond()


def _patch_window(app, tmp_path, monkeypatch, slide_path):
    w = _window(app, tmp_path)
    w.loader = OMETIFFLoader(slide_path)
    w._all_patches = [(0, H, 0, W)]
    w._preview_patch_idx = 0
    w._active_roi = None
    monkeypatch.setattr(w, "_needed_channels", lambda: ["CD3"])
    monkeypatch.setattr(w, "_step1_whole_slide_active", lambda: False)
    # what the patch view is asked to draw, with the pixels it would draw
    w.drawn = []
    monkeypatch.setattr(w, "_refresh_patch_preview", lambda **k: w.drawn.append(
        {ch: np.array(a) for ch, a in (w._patch_channel_cache.get(w._preview_patch_idx) or {}).items()}))
    return w


def test_a_save_during_a_held_read_shows_B_and_never_waits(app, tmp_path, monkeypatch, slide):
    path, stores = slide
    w = _patch_window(app, tmp_path, monkeypatch, path)
    w.loader.set_corrected_zarr_store(stores["A"], {"CD3": "tophat"})
    inside, release = threading.Event(), threading.Event()
    real_open = io_loader.zarr.open

    def held_open(p, *a, **k):
        if p == stores["A"]:
            inside.set()
            assert release.wait(20)
        return real_open(p, *a, **k)
    monkeypatch.setattr(io_loader.zarr, "open", held_open)
    waited = []
    try:
        w._ensure_channels_cached(0)
        old = w._patch_loaders[0]
        old_wait = old.wait
        old.wait = lambda *a: waited.append(a) or old_wait(*a)
        assert inside.wait(10), "the A read did not start"

        # Step0 Save: B, then the handoff accept's reset and re-ask
        w.loader.set_corrected_zarr_store(stores["B"], {"CD3": "tophat"})
        clock = QtCore.QElapsedTimer()
        clock.start()
        w._stop_all_loaders()
        assert clock.elapsed() < 200 and waited == [], "the stop waited for the read"
        assert old in w._retired_loaders and 0 not in w._patch_loaders
        w._patch_channel_cache.clear()
        w._patch_load_ready.clear()
        w._ensure_channels_cached(0)
        new = w._patch_loaders[0]
        assert new is not old, "the same patch must get a new loader at once"
        assert _until(app, lambda: "CD3" in (w._patch_channel_cache.get(0) or {}))
        assert np.all(w._patch_channel_cache[0]["CD3"] == B)
        assert w.drawn and np.all(w.drawn[-1]["CD3"] == B), "the patch view did not draw B"
        drawn_before = len(w.drawn)

        release.set()                                   # the old read ends now
        assert _until(app, lambda: old not in w._retired_loaders and not old.isRunning())
        assert np.all(w._patch_channel_cache[0]["CD3"] == B), "a late A answer landed"
        assert len(w.drawn) == drawn_before, "the retired A read made the view redraw"
        assert 0 not in w._patch_loaders or w._patch_loaders[0] is not old
    finally:
        release.set()
        for t in [*w._patch_loaders.values(), *w._retired_loaders]:
            QtCore.QThread.wait(t, 5000)
        w._patch_loaders, w._retired_loaders = {}, set()
        w.close()


class _Held(QtCore.QThread):
    """A loader blocked in a read; emits a late `done` when let go."""
    done = QtCore.pyqtSignal(int, dict)
    progress = QtCore.pyqtSignal(int, int, int, str)
    error = QtCore.pyqtSignal(int, str)

    def __init__(self, idx):
        super().__init__()
        self.idx = idx
        self.go = threading.Event()
        self.stopped = False

    def stop(self):
        self.stopped = True

    def run(self):
        self.go.wait(20)
        self.done.emit(self.idx, {"CD3": np.zeros((2, 2), np.float32)})
        self.error.emit(self.idx, "late")


def test_a_retired_loader_is_released_and_touches_nothing_of_its_replacement(app, tmp_path):
    w = _window(app, tmp_path)
    old = _Held(0)
    w._connect_patch_loader(old, 0)
    w._patch_loaders[0] = old
    old.start()
    try:
        w._stop_loader_for(0)
        assert old.stopped and old in w._retired_loaders and 0 not in w._patch_loaders
        new = _Held(0)
        w._connect_patch_loader(new, 0)
        w._patch_loaders[0] = new
        w._loader_channels[0] = {"CD3"}
        w._pending_channel_demand[0] = {"CD8"}
        new.start()
        old.go.set()
        assert _until(app, lambda: old not in w._retired_loaders)
        assert w._patch_loaders.get(0) is new
        assert w._loader_channels[0] == {"CD3"} and w._pending_channel_demand[0] == {"CD8"}
        assert "CD3" not in (w._patch_channel_cache.get(0) or {}), "a retired answer landed"
        assert not w._failed_channels.get(0), "a retired error blamed the replacement"
    finally:
        old.go.set()
        new.go.set()
        QtCore.QThread.wait(old, 5000)
        QtCore.QThread.wait(new, 5000)
        _until(app, lambda: False, 0.2)
        w._patch_loaders, w._retired_loaders = {}, set()
        w.close()


def test_an_already_finished_loader_is_dropped_not_retired(app, tmp_path):
    w = _window(app, tmp_path)
    done = _Held(0)
    done.go.set()
    done.start()
    QtCore.QThread.wait(done, 5000)
    w._patch_loaders[0] = done
    w._stop_all_loaders()
    assert w._patch_loaders == {} and w._retired_loaders == set()
    w.close()


def test_close_retries_while_a_retired_loader_lives(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path)
    w._regions = None
    old = _Held(0)
    w._connect_patch_loader(old, 0)
    w._patch_loaders[0] = old
    old.start()
    answers, finalized = [], []
    real_close_event = type(w).closeEvent

    def close_event(event):
        real_close_event(w, event)
        answers.append(event.isAccepted())
    monkeypatch.setattr(w, "closeEvent", close_event)
    real_finalize = w._display.finalize_close
    monkeypatch.setattr(w._display, "finalize_close",
                        lambda reason: finalized.append(reason) or real_finalize(reason))
    w.show()
    w.close()                                           # retires it, refuses, retries
    assert answers == [False] and old in w._retired_loaders
    QtCore.QTimer.singleShot(700, old.go.set)
    assert _until(app, lambda: bool(finalized))
    assert len(answers) >= 2 and answers[-1] is not False and not w.isVisible()
    QtCore.QThread.wait(old, 5000)


def test_the_handoff_asks_the_patch_again_after_the_viewer_rebind(monkeypatch, tmp_path, app):
    """The real accept path (`_load_step0_roi_result`): the rebind fails and
    puts the patch view back; the selected patch is asked right after it."""
    import block01.ui.main_window as mw
    from test_step0_step1_handoff_contract import Root, make_run, make_window
    monkeypatch.setattr(mw.zarr, "open", lambda *_a, **_k: Root())
    run = make_run(tmp_path)
    w = make_window(run)
    order = []
    shown = {"whole_slide": True}

    def rebind(reason):
        order.append(("rebind", reason))
        shown["whole_slide"] = False                    # the fallback: patch view back
    monkeypatch.setattr(w, "_step1_sync_whole_slide_source", rebind, raising=False)
    monkeypatch.setattr(w, "_step1_whole_slide_active",
                        lambda: shown["whole_slide"], raising=False)
    monkeypatch.setattr(w, "_ensure_channels_cached",
                        lambda idx: order.append(("ask", idx, shown["whole_slide"])),
                        raising=False)
    assert w._load_step0_roi_result(auto=True) is True
    assert ("rebind", "handoff") in order
    # whole-slide viewer showing and its rebind WORKS: nothing extra asked
    order.clear()
    shown["whole_slide"] = True
    monkeypatch.setattr(w, "_step1_sync_whole_slide_source",
                        lambda reason: order.append(("rebind", reason)), raising=False)
    assert w._load_step0_roi_result(auto=True) is True
    assert [o for o in order if o[0] == "ask"] == [], "an extra ask on a working rebind"
    # and again with a failing rebind
    order.clear()
    shown["whole_slide"] = True
    monkeypatch.setattr(w, "_step1_sync_whole_slide_source", rebind, raising=False)
    assert w._load_step0_roi_result(auto=True) is True
    asks = [o for o in order if o[0] == "ask"]
    assert asks and order.index(asks[-1]) > order.index(("rebind", "handoff"))
    assert asks[-1] == ("ask", w._preview_patch_idx, False), \
        "the patch must be asked with the patch view back on screen"
