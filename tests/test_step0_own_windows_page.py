"""Step0 keeps its own Min/Max/Gamma: the page, the ONE Intensity window and
the workspace records.

User ruling 2026-10-27 (docs/v17_step0_own_ranges_plan.md): Step0's windows
(STEP0) are its own; Step1/2/3/Regions share one set (SHARED). The Intensity
window edits the table of the step on screen. An Intensity-only Step0 Save
does not touch SHARED; Step1's uncommitted windows survive a restart.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import display_identity as di  # noqa: E402
from block01.core.display_identity import SHARED_TABLE, STEP0_TABLE  # noqa: E402
from block01.core.display_mapping import seed_display_range  # noqa: E402
from block01.ui.step0 import step0_page as sp  # noqa: E402
from block01.utils import channel_remap_config as crc  # noqa: E402
from block01.utils import run_store as rs  # noqa: E402

from test_step0_background_correction_tab import _GpuPathLoader, app  # noqa: E402,F401

A = di.DatasetIdentity("/tmp/own_windows_page_A.ome.tiff", "1:1")

_RNG = np.random.default_rng(7)
PIXELS = {
    "DAPI": _RNG.uniform(10, 200, (64, 64)).astype(np.float32),
    "CD3": _RNG.uniform(5, 900, (64, 64)).astype(np.float32),
    # a float channel whose whole range is below one: the canonical seed must
    # not be widened to `lo + 1` the way the editor's own seed is
    "CD20": _RNG.uniform(0.2, 0.8, (64, 64)).astype(np.float32),
}


def _page(app):
    page = sp.Step0Page()
    page.loader = _GpuPathLoader()
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page.display.state.bind(A)
    page._slide_lowres_array = (lambda ch, blocking=False, resident_only=False:
                                PIXELS.get(ch))
    page._sync_step0_to_workbench()
    page._cond_workbench.set_pixel_provider(lambda ch: PIXELS.get(ch))
    return page


def _win(params):
    return (float(params["min"]), float(params["max"]), float(params.get("gamma", 1.0)))


def _record(page, table, ch):
    p = page._records(table)["params"].get(ch)
    return None if p is None else _win(p)


def _type(wb, ch, lo=None, hi=None):
    """Type into the Intensity window's spin boxes, as a user does."""
    wb.set_active_channel(ch)
    if hi is not None:
        wb._sp_max.setValue(float(hi))
    if lo is not None:
        wb._sp_min.setValue(float(lo))
    QtWidgets.QApplication.processEvents()


# ── Step0's own writes ─────────────────────────────────────────────────────

def test_a_step0_window_never_reaches_the_shared_table(app):
    page = _page(app)
    state = page.display.state
    state.set_mapping("CD3", 50.0, 400.0, origin="step1")

    page.set_display_mapping("CD3", 100.0, 900.0, 1.2)

    assert state.mapping("CD3", table=STEP0_TABLE) == (100.0, 900.0, 1.2)
    assert _record(page, STEP0_TABLE, "CD3") == (100.0, 900.0, 1.2)
    assert state.mapping("CD3") == (50.0, 400.0, 1.0)
    assert _record(page, SHARED_TABLE, "CD3") == (50.0, 400.0, 1.0)


# ── the ONE Intensity window ───────────────────────────────────────────────

def test_the_intensity_window_edits_the_table_of_the_step_on_screen(app):
    page = _page(app)
    state = page.display.state
    wb = page._cond_workbench
    state.set_scope("step0")
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)
    state.set_mapping("CD3", 50.0, 400.0, origin="step1")

    wb.set_active_channel("CD3")
    assert _win(wb._params["CD3"]) == (100.0, 900.0, 1.0)

    state.set_scope("step1")                      # Step1 on screen
    assert _win(wb._params["CD3"]) == (50.0, 400.0, 1.0)
    _type(wb, "CD3", hi=300.0)
    assert state.mapping("CD3")[1] == 300.0
    assert _record(page, SHARED_TABLE, "CD3")[1] == 300.0
    assert "CD3" in page._records(SHARED_TABLE)["user_adjusted"]
    assert state.mapping("CD3", table=STEP0_TABLE) == (100.0, 900.0, 1.0)

    state.set_scope("regions")                    # Regions shares Step1's
    assert _win(wb._params["CD3"])[1] == 300.0

    state.set_scope("step0")                      # back: Step0's own again
    assert _win(wb._params["CD3"]) == (100.0, 900.0, 1.0)
    _type(wb, "CD3", hi=800.0)
    assert state.mapping("CD3", table=STEP0_TABLE)[1] == 800.0
    assert state.mapping("CD3")[1] == 300.0


def test_a_lazy_load_seeds_every_table_with_the_canonical_window(app):
    page = _page(app)
    wb = page._cond_workbench
    expected = seed_display_range(PIXELS["CD20"])
    assert expected[1] - expected[0] < 1.0        # the case the clamp got wrong

    wb.set_active_channel("CD20")                 # the provider delivers here
    QtWidgets.QApplication.processEvents()

    for table in (STEP0_TABLE, SHARED_TABLE):
        got = _record(page, table, "CD20")
        assert got == pytest.approx((expected[0], expected[1], 1.0)), table
        assert "CD20" not in page._records(table)["user_adjusted"], table
    assert _win(wb._params["CD20"]) == pytest.approx((expected[0], expected[1], 1.0))


def test_pixels_arriving_while_the_other_table_is_shown_seed_both(app):
    page = _page(app)
    state = page.display.state
    wb = page._cond_workbench
    page.set_display_mapping("CD20", 0.25, 0.5, 1.0)   # STEP0 typed
    state.set_scope("step1")

    wb.set_active_channel("CD20")
    QtWidgets.QApplication.processEvents()

    lo, hi = seed_display_range(PIXELS["CD20"])
    assert _record(page, SHARED_TABLE, "CD20") == pytest.approx((lo, hi, 1.0))
    assert _record(page, STEP0_TABLE, "CD20") == (0.25, 0.5, 1.0)
    assert _win(wb._params["CD20"]) == pytest.approx((lo, hi, 1.0))


def test_a_rebuild_of_the_window_loses_neither_table(app):
    page = _page(app)
    state = page.display.state
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)
    state.set_mapping("CD3", 50.0, 400.0, origin="step1")

    page._sync_step0_to_workbench()               # re-feed, as a store swap does

    assert _record(page, STEP0_TABLE, "CD3") == (100.0, 900.0, 1.0)
    assert _record(page, SHARED_TABLE, "CD3") == (50.0, 400.0, 1.0)
    assert _win(page._cond_workbench._params["CD3"]) == (100.0, 900.0, 1.0)


def test_step0s_save_and_step1s_draft_each_export_their_own_table(app):
    page = _page(app)
    state = page.display.state
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)
    state.set_mapping("CD3", 50.0, 400.0, origin="step1")
    state.set_scope("step1")                      # the window shows SHARED

    saved = page._step0_save_params()
    assert _win(saved["CD3"]) == (100.0, 900.0, 1.0)
    cfg = page._cond_workbench.build_config(params=saved)
    assert _win(cfg["channels"]["CD3"]) == (100.0, 900.0, 1.0)

    draft = page.display_mapping_draft()
    assert _win(draft["CD3"]) == (50.0, 400.0, 1.0)


# ── the workspace: drafts, reopen, Q2a, handoff ────────────────────────────

SAVED = {"CD3": {"min": 10.0, "max": 100.0, "gamma": 1.0},
         "CD20": {"min": 0.3, "max": 0.6, "gamma": 1.0}}


def _workspace(page, tmp_path, monkeypatch, run="rois/ws/runs/correct_a",
               saved=None):
    ws = tmp_path / "proj" / "rois" / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "roi_manifest.json").write_text("{}")
    (tmp_path / "proj" / "project_manifest.json").write_text("{}")
    sess = rs.load_session(str(ws))
    sess["editing"] = {"correct_run": run, "geometry_revision": 0}
    rs.save_session(str(ws), sess)
    page._roi_context = {"roi_dir": str(ws)}
    remap = {"channels": dict(saved if saved is not None else SAVED)}
    monkeypatch.setattr(crc, "load_channel_remap_config", lambda _p: remap)
    return str(ws), remap


def test_reopen_without_a_shared_record_starts_step1_from_the_handoff(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)

    page._restore_workspace_windows(ws, {"intensity": {"CD3": {"min": 7.0, "max": 70.0}}})

    state = page.display.state
    assert state.mapping("CD3", table=STEP0_TABLE)[:2] == (7.0, 70.0)     # Step0's draft
    assert state.mapping("CD3")[:2] == (10.0, 100.0)                      # the handoff
    assert page._shared_origin == "handoff"
    assert "CD3" in page._records(STEP0_TABLE)["user_adjusted"]
    assert "CD3" not in page._records(SHARED_TABLE)["user_adjusted"]


def test_step1s_committed_windows_fill_shared_once_and_never_over_an_edit(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    state = page.display.state
    state.set_mapping("CD20", 0.4, 0.7, origin="regions")      # typed in Regions

    assert page.adopt_intensity({"CD3": {"min": 1.0, "max": 11.0},
                                 "CD20": {"min": 0.1, "max": 0.2}}, origin="t")
    assert state.mapping("CD3")[:2] == (1.0, 11.0)
    assert state.mapping("CD20")[:2] == (0.4, 0.7)              # the edit stands
    assert state.mapping("CD3", table=STEP0_TABLE)[:2] == (10.0, 100.0)

    # The deferred Q2a on entering Step1 is the same binding: nothing again.
    assert page.adopt_intensity({"CD3": {"min": 2.0, "max": 22.0}}, origin="t") is False
    assert state.mapping("CD3")[:2] == (1.0, 11.0)


def test_the_shared_record_comes_back_and_q2a_then_adopts_nothing(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    page.display.state.set_mapping("CD3", 33.0, 333.0, origin="step1")
    assert page.rm_save_shared_windows() is True
    record = rs.load_session(ws)["drafts"]["shared_windows"]
    assert record["edited_against"] == {"correct_run": "rois/ws/runs/correct_a"}
    assert set(record["params"]) >= {"CD3", "CD20"}            # the WHOLE table
    assert record["user_adjusted"] == ["CD3"]

    again = _page(app)
    _workspace(again, tmp_path, monkeypatch)
    again._restore_workspace_windows(ws, None)
    state = again.display.state
    assert again._shared_origin == "record"
    assert state.mapping("CD3")[:2] == (33.0, 333.0)
    assert state.mapping("CD20")[:2] == (0.3, 0.6)
    assert again.adopt_intensity({"CD3": {"min": 1.0, "max": 11.0}}, origin="t") is False
    assert state.mapping("CD3")[:2] == (33.0, 333.0)


def test_a_shared_record_of_another_correct_run_is_not_restored(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    page.display.state.set_mapping("CD3", 33.0, 333.0, origin="step1")
    page.rm_save_shared_windows()

    again = _page(app)
    _workspace(again, tmp_path, monkeypatch, run="rois/ws/runs/correct_b")
    again._restore_workspace_windows(ws, None)

    assert again._shared_origin == "handoff"
    assert again.display.state.mapping("CD3")[:2] == (10.0, 100.0)


def test_an_intensity_only_save_does_not_reach_step1_across_a_restart(app, tmp_path, monkeypatch):
    """R2: a channel Step1 never touched keeps the handoff's window even
    after Step0 moved it and saved without new pixels, then a restart."""
    page = _page(app)
    ws, remap = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    page.rm_save_shared_windows()                      # a step change / close
    page.set_display_mapping("CD3", 500.0, 800.0, 1.0)
    remap["channels"]["CD3"] = {"min": 500.0, "max": 800.0, "gamma": 1.0}   # the Save

    again = _page(app)
    _workspace(again, tmp_path, monkeypatch, saved=remap["channels"])
    again._restore_workspace_windows(ws, None)

    assert again.display.state.mapping("CD3")[:2] == (10.0, 100.0)
    assert again.display.state.mapping("CD3", table=STEP0_TABLE)[:2] == (500.0, 800.0)


@pytest.mark.parametrize("regions_edit", [False, True])
def test_an_old_project_keeps_its_committed_windows_across_close_and_reopen(
        app, tmp_path, monkeypatch, regions_edit):
    """Migration: committed fusion settings and NO shared record. Reopen
    adopts the committed windows at once, so a close before Step1 (or after a
    Regions edit) records them rather than the handoff's."""
    committed = {"CD3": {"min": 1.0, "max": 11.0}, "CD20": {"min": 0.1, "max": 0.2}}
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    page.adopt_intensity(committed, origin="reopen")
    if regions_edit:
        page.display.state.set_mapping("CD20", 0.4, 0.7, origin="regions")
    page.rm_save_shared_windows()                      # close before Step1

    again = _page(app)
    _workspace(again, tmp_path, monkeypatch)
    again._restore_workspace_windows(ws, None)
    again.adopt_intensity(committed, origin="reopen")
    again.adopt_intensity(committed, origin="Step1 entry")

    state = again.display.state
    assert state.mapping("CD3")[:2] == (1.0, 11.0)
    assert state.mapping("CD20")[:2] == ((0.4, 0.7) if regions_edit else (0.1, 0.2))
    assert state.mapping("CD3", table=STEP0_TABLE)[:2] == (10.0, 100.0)


def test_the_step0_draft_holds_step0s_windows_only(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)
    page.display.state.set_mapping("CD3", 50.0, 400.0, origin="step1")
    monkeypatch.setattr(page, "_build_config", lambda: {"channel_decisions": {}})
    monkeypatch.setattr(page, "_clean_correction_config", lambda c: c)

    assert page.rm_save_draft() is True

    intensity = rs.load_session(ws)["drafts"]["step0"]["intensity"]
    assert (intensity["CD3"]["min"], intensity["CD3"]["max"]) == (100.0, 900.0)


def test_a_new_correct_run_starts_step1_from_step0s_windows(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch)
    page._restore_workspace_windows(ws, None)
    state = page.display.state
    page.set_display_mapping("CD3", 100.0, 900.0, 1.0)
    state.set_mapping("CD3", 50.0, 400.0, origin="step1")
    state.set_mapping("CD8", 5.0, 40.0, origin="step1")        # Step1 only

    page._handoff_windows_to_shared()

    assert state.mapping("CD3") == state.mapping("CD3", table=STEP0_TABLE)
    assert state.mapping("CD8") is None                         # old pixels' window
    assert page._records(SHARED_TABLE)["user_adjusted"] == set()
    assert _record(page, SHARED_TABLE, "CD3") == (100.0, 900.0, 1.0)
    # ...and Step1's committed windows of the old run cannot come back over it
    assert page.adopt_intensity({"CD3": {"min": 1.0, "max": 11.0}}, origin="t") is False


def test_only_a_new_correct_run_counts_as_a_new_handoff(app, tmp_path, monkeypatch):
    page = _page(app)
    ws, _ = _workspace(page, tmp_path, monkeypatch, run="")

    def correct_run():
        run = rs.new_run(ws, "correct")
        os.makedirs(os.path.join(run, "corrected_channels.zarr"))
        rs.write_params(run, {"kind": "correct", "corrected": {}})
        rs.write_inputs(run, None, slide_id="s1")
        rs.publish(run)
        return {"corrected_zarr_path": os.path.join(run, "corrected_channels.zarr")}

    first = correct_run()
    assert page._rm_note_editing(first) is True                 # first handoff
    assert page._rm_note_editing(first) is False                # Intensity-only
    second = correct_run()
    assert page._rm_note_editing(second) is True                # new pixels
    assert rs.load_session(ws)["editing"]["correct_run"].endswith(
        os.path.basename(os.path.dirname(second["corrected_zarr_path"])))


def test_another_workspace_on_the_same_slide_starts_from_its_own_windows(app, tmp_path, monkeypatch):
    """A workspace is a boundary. The slide's namespace is reused when the
    same slide is opened in workspace B; A's windows and typed flags must
    not survive into B -- else B's committed windows are skipped as if typed,
    and A's numbers are saved under B's correct run."""
    page = _page(app)
    ws_a, _ = _workspace(page, tmp_path / "a", monkeypatch)
    page._restore_workspace_windows(ws_a, None)
    state = page.display.state
    state.set_mapping("CD3", 33.0, 333.0, origin="step1")           # typed in A
    state.set_mapping("CD8", 5.0, 50.0, origin="step1")             # only in A
    page.set_display_mapping("CD8", 6.0, 60.0, 1.0)                 # Step0, only in A

    ws_b, _ = _workspace(page, tmp_path / "b", monkeypatch,
                         run="rois/ws/runs/correct_b")
    page._restore_workspace_windows(ws_b, None)

    assert state.mapping("CD3")[:2] == (10.0, 100.0)                # B's handoff
    assert state.mapping("CD8") is None
    assert state.mapping("CD8", table=STEP0_TABLE) is None
    assert page._records(SHARED_TABLE)["user_adjusted"] == set()
    assert page.adopt_intensity({"CD3": {"min": 1.0, "max": 11.0}}, origin="t")
    assert state.mapping("CD3")[:2] == (1.0, 11.0)                  # B's committed
