"""Step1's core outlines without a pause (docs/f5_tma_display_plan.md §10):
the ids come without opening the slide; one result per key is shared by the
Step1 viewer and the Tissue preview; a Regions Save computes it off the GUI
thread and a stale answer (a later Save, another region) is dropped."""

import os
import time
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

from test_regions_r1 import SID, _finished, project  # noqa: F401,E402

from block01.core.regions import document as D  # noqa: E402
from block01.core.regions import lock as K  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _window(project, monkeypatch, calls):
    from block01.core import provenance as prov
    from block01.core.tma import study as st
    from block01.ui import main_window as mw
    from block01.ui.main_window import MainWindow
    s, d = _finished()
    for key in sorted(d["positions"])[:2]:
        D.set_run(d, key, True)
    K.save(project, d, s)
    monkeypatch.setattr(st, "load_study", lambda pd: s)
    monkeypatch.setattr(prov, "ensure_raw_slide", lambda pd, slide: SID)
    real = MainWindow._compute_step1_core_items

    def counted(pd, sid, parent):
        calls.append(parent)
        return real(pd, sid, parent)
    monkeypatch.setattr(MainWindow, "_compute_step1_core_items", staticmethod(counted))

    class W(QtCore.QObject):
        _step1_cores_ready = QtCore.pyqtSignal(object)
    w = W()
    for name in ("_regions_ids", "_step1_core_parent", "_saved_core_items", "_on_regions_saved",
                 "_on_step1_cores_ready"):
        setattr(w, name, types.MethodType(getattr(MainWindow, name), w))
    w._step1_core_key = MainWindow._step1_core_key
    w._compute_step1_core_items = MainWindow._compute_step1_core_items
    w.step0_output = {"roi_dir": os.path.join(project, "rois", "ws")}
    w.loader = types.SimpleNamespace(filepath="/x/slide.qptiff")
    w._regions_files_present = lambda: True
    w._current_step = 1
    w.redrawn = []
    w._refresh_core_outlines = lambda which: w.redrawn.append(which)
    w._refresh_tissue_core_outlines = lambda: w.redrawn.append("tissue")
    w._step1_cores_ready.connect(w._on_step1_cores_ready)
    monkeypatch.setattr(mw.run_store, "project_dir_of", lambda p: project)
    return w, s, d


def _wait(app, pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_one_computation_is_shared_until_the_key_moves(app, project, monkeypatch):
    calls = []
    w, s, d = _window(project, monkeypatch, calls)
    a = w._saved_core_items()
    b = w._saved_core_items()                                  # the Tissue preview, same entry
    assert a and a == b and len(calls) == 1
    w._active_roi = {"bbox_fullres": [0, 900, 0, 900]}         # another region: computed again
    w._saved_core_items()
    assert len(calls) == 2 and calls[-1]["bbox_fullres"] == [0, 900, 0, 900]
    time.sleep(0.01)
    K.save(project, d, s)                                      # the Save file changes
    w._saved_core_items()
    assert len(calls) == 3


def test_a_save_computes_in_advance_and_entry_does_not(app, project, monkeypatch):
    calls = []
    w, s, d = _window(project, monkeypatch, calls)
    assert w._on_regions_saved()
    assert _wait(app, lambda: w.__dict__.get("_step1_cores_held") is not None)
    assert w.redrawn == ["step1"]                               # drawn when ready (not on the Tissue preview)
    n = len(calls)
    items = w._saved_core_items()                               # entering Step1: nothing computed
    assert items and len(calls) == n


def test_a_stale_answer_is_dropped(app, project, monkeypatch):
    calls = []
    w, s, d = _window(project, monkeypatch, calls)
    w._step1_cores_seq = 5
    pd, slide, sid = w._regions_ids()
    key = w._step1_core_key(pd, slide, sid, None)
    assert w._on_step1_cores_ready((4, key, ["old"], None, 0.1)) is False      # a later Save came
    assert w._on_step1_cores_ready((5, ("other",), ["x"], None, 0.1)) is False  # the key moved
    assert w._on_step1_cores_ready((5, key, None, RuntimeError("boom"), 0.1)) is False
    assert w.__dict__.get("_step1_cores_held") is None
    assert w._on_step1_cores_ready((5, key, ["it"], None, 0.1)) is True
    assert w._step1_cores_held == (key, ["it"])


def test_the_ids_never_open_the_slide(app, project, monkeypatch):
    from block01.sources import ome_tiff
    calls = []
    w, s, d = _window(project, monkeypatch, calls)
    monkeypatch.setattr(ome_tiff, "OmeTiffSource", lambda *a, **k: pytest.fail("the slide was opened"))
    assert w._regions_ids() == (project, "/x/slide.qptiff", SID)


def test_two_saves_in_a_row_keep_only_the_seconds_answer(app, project, monkeypatch):
    calls = []
    w, s, d = _window(project, monkeypatch, calls)
    held = []
    real = w._on_step1_cores_ready
    w._on_step1_cores_ready = lambda r: held.append((r[0], real(r)))
    w._step1_cores_ready.disconnect()
    w._step1_cores_ready.connect(w._on_step1_cores_ready)
    w._on_regions_saved()                                       # A
    time.sleep(0.01)
    K.save(project, d, s)
    w._on_regions_saved()                                       # B, while A may still run
    assert _wait(app, lambda: len(held) == 2)
    assert dict(held) == {1: False, 2: True}                    # A dropped, B kept


def test_a_failed_advance_falls_back_to_computing_on_entry(app, project, monkeypatch):
    from block01.ui.main_window import MainWindow
    calls = []
    w, s, d = _window(project, monkeypatch, calls)
    w._compute_step1_core_items = staticmethod(lambda *a: (_ for _ in ()).throw(OSError("disk")))
    w._on_regions_saved()
    assert _wait(app, lambda: w.__dict__.get("_step1_cores_seq") == 1 and not w.redrawn and True, 1.0)
    w._compute_step1_core_items = MainWindow._compute_step1_core_items
    assert w._saved_core_items()                                # computed now, on entry
