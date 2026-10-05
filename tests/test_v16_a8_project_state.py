"""Block A8: the project state is the one owner of the current segmentation
run (application §3, §6; user ruling 2026-10-05, constraints C-a / C-b)."""

import dataclasses
import os

import pytest

from block01.core.project_state import ProjectState, SegmentationRun
from block01.utils import run_store as rs
from project_state_audit import mask_key_reads, state_writes  # noqa: F401
from test_v16_a8_characterization import BBOX, _seg, chain  # noqa: F401
from test_v16_rm1_run_store import app, _rm_window, _correct_run, _fuse_run  # noqa: F401
from test_v16_rm2_chain import _t

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_WINDOW = os.path.join(HERE, "..", "ui", "main_window.py")


# ── the holder itself ─────────────────────────────────────────────────

def test_two_named_writers_and_no_general_setter():
    public = {n for n in dir(ProjectState) if not n.startswith("_")}
    assert public == {"active_segmentation_run", "choose_segmentation_run",
                      "clear_segmentation_run"}


def test_the_snapshot_is_immutable():
    state = ProjectState()
    state.choose_segmentation_run("/p/rois/ws/runs/segment_x", "R1")
    snap = state.active_segmentation_run
    assert isinstance(snap, SegmentationRun)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.run_dir = "/elsewhere"


def test_a_step2_result_never_overrides_a_deliberate_choice():
    state = ProjectState()
    assert state.choose_segmentation_run("/a", origin="step2")
    assert state.choose_segmentation_run("/b", origin="step2")       # C10: newest
    assert state.active_segmentation_run.run_dir == os.path.realpath("/b")
    assert state.choose_segmentation_run("/c", "R1", origin="step3")
    assert not state.choose_segmentation_run("/d", origin="step2")   # C9: kept
    assert state.active_segmentation_run.run_dir == os.path.realpath("/c")
    state.clear_segmentation_run()
    assert state.active_segmentation_run is None


# ── C-a: the mask key is a display cache only ─────────────────────────

def test_the_mask_key_is_never_read_to_decide_the_current_run():
    assert mask_key_reads(MAIN_WINDOW) == []


# ── every writer, exactly once; nothing else writes ───────────────────

def test_step2_three_and_four_use_the_one_field(chain, state_writes):
    w, ws, f, a, b = chain
    state_writes.clear()
    w._on_step2_complete(b)
    assert state_writes.count("choose") == 1
    w._step3_refresh_masks()                           # Step3 shows it: becomes Step3's
    assert w._project_state.active_segmentation_run.run_dir == os.path.realpath(b)
    run_dir, roi = w._step4_choice()
    assert os.path.realpath(run_dir) == os.path.realpath(b) and roi == "R1"
    state_writes.clear()
    w._step3_on_run_chosen(f"{a}\x1fR1")
    assert state_writes.count("choose") == 1, state_writes
    assert os.path.realpath(w._step4_choice()[0]) == os.path.realpath(a)


def test_showing_step3_again_writes_nothing(chain, state_writes):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    state_writes.clear()
    for _ in range(3):
        w._step3_refresh_masks()
    assert state_writes.count() == 0, state_writes


def test_a_run_finished_after_moving_to_another_workspace_is_not_current(chain, tmp_path,
                                                                         state_writes):
    """A6: Step2's result of the workspace the user has LEFT does not become
    the current run of the one on screen."""
    w, ws, f, a, b = chain
    other = tmp_path / "proj" / "rois" / "ws2"
    (other / "settings" / "step0").mkdir(parents=True)
    (other / "roi_manifest.json").write_text("{}")
    w.step0_output = dict(w.step0_output, roi_dir=str(other), roi_id="ws2")
    state_writes.clear()
    w._on_step2_complete(b)                             # b belongs to ws1
    assert state_writes.count("choose") == 0
    assert w._project_state.active_segmentation_run is None


def test_deleting_the_current_run_clears_it(chain, state_writes):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=b)
    state_writes.clear()
    w._rm_release_paths([b])
    assert state_writes.count("clear") >= 1
    assert w._project_state.active_segmentation_run is None
    entry = w._step3_refresh_masks()                    # the list's next becomes current
    assert os.path.realpath(entry.run.run_dir) == os.path.realpath(a) or \
        os.path.realpath(entry.run.run_dir) == os.path.realpath(b)


def test_an_opened_workspace_brings_its_viewed_run_back_at_once(chain, state_writes):
    """§3.2: deliberately unlike C7 -- Step4 has the restored run before Step3
    is shown."""
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)            # writes `viewing` = a
    w._project_state.clear_segmentation_run()
    state_writes.clear()
    w._a8_follow_workspace(opened=True)
    assert state_writes.count("choose") == 1
    assert os.path.realpath(w._step4_choice()[0]) == os.path.realpath(a)


def test_a_dataset_switch_clears_the_current_run(chain, state_writes):
    w, ws, f, a, b = chain
    w._step3_refresh_masks(requested_dir=a)
    state_writes.clear()
    w._step3_clear_masks()                              # what a committed switch does
    assert state_writes.count("clear") == 1
    assert w._step4_choice() == ("", None)


def test_open_qc_names_the_run(chain, state_writes, monkeypatch):
    w, ws, f, a, b = chain
    monkeypatch.setattr(type(w), "_step3_entry_ready", lambda self, out=None: True)
    monkeypatch.setattr(type(w), "_set_step_active", lambda self, step: None)
    state_writes.clear()
    w._go_to_step3(a)
    assert state_writes.events[0][:2] == ("choose", os.path.realpath(a))
    assert os.path.realpath(w._step4_choice()[0]) == os.path.realpath(a)


def test_a_save_in_the_same_workspace_keeps_a_run_from_elsewhere(chain, tmp_path):
    """codex A8: an outside / other-workspace run chosen in Step3 survives an
    (Intensity-only) Save in the same workspace; a real workspace change
    drops it."""
    w, ws, f, a, b = chain
    w._a8_follow_workspace(opened=False)                     # the workspace on screen
    outside = str(tmp_path / "elsewhere" / "rois" / "wsX" / "runs" / "segment_x")
    w._project_state.choose_segmentation_run(outside, "R1", origin="step3")
    w._a8_follow_workspace(opened=False)                     # a Save, same workspace
    assert w._project_state.active_segmentation_run is not None
    other = tmp_path / "proj" / "rois" / "ws2"
    (other / "settings" / "step0").mkdir(parents=True)
    (other / "roi_manifest.json").write_text("{}")
    w.step0_output = dict(w.step0_output, roi_dir=str(other), roi_id="ws2")
    w._a8_follow_workspace(opened=False)                     # another workspace
    assert w._project_state.active_segmentation_run is None
