"""Step1 prepared ahead (docs/v17_save_inplace_step1_ahead_plan.md §9).

While Step0 is on screen and still, Step1's viewer is built once and its view
read, so entering Step1 shows a sharp picture at once. Here, with stand-ins
for the native core, the stack and the window:

* the binding prepares only while PAUSED, at most 15 channels (the user's
  cap) without touching the ticks, and pauses the core again when it ends;
  the entry's `resume` ends it;
* the window's owner runs ONE piece per turn -- build the stack, build the
  backend (each only after 2 s of quiet), rebuild a pending source, then
  off-screen paints until the core is idle -- and gives way to production
  runs, to Step0's reads, to another step and to another slide;
* a Step0 camera write re-arms it, and the entry stops it.

The real-machine replay (TMA1 copy, HDD) is the acceptance: entry 0.23-0.31 s
instead of ~1.2 s, the first frame sharp.
"""

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.qt import QtWidgets  # noqa: E402

from block01.ui import step1_core_binding as cb  # noqa: E402
from block01.ui.main_window import MainWindow  # noqa: E402
from block01.ui.step1_display_types import MODE_OVERLAY  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the binding ─────────────────────────────────────────────────────────────

class _Core:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **_k):
            self.calls.append((name, a))
            return True if name == "idle" else {}
        return call


class _Controller:
    def __init__(self):
        from block01.qt import QtCore

        class _Sig(QtCore.QObject):
            interaction_event = QtCore.Signal(object, object)
            gesture_quiet = QtCore.Signal(object)
        self._s = _Sig()
        self.interaction_event = self._s.interaction_event
        self.gesture_quiet = self._s.gesture_quiet

    def snapshot(self):
        return SimpleNamespace(level=1, bbox_l0=(0, 100, 0, 100), epoch=3)


def _binding(app, n_channels=20):
    names = tuple(f"CH{i:02d}" for i in range(n_channels))
    display = SimpleNamespace(mode=MODE_OVERLAY, weights={c: 1.0 for c in names},
                              mappings={c: (0.0, 1.0, 1.0) for c in names},
                              colors={c: (1.0, 1.0, 1.0) for c in names})
    stack = SimpleNamespace(controller=_Controller(),
                            provider=SimpleNamespace(level_shape=lambda _l: (100, 100)))
    layer = SimpleNamespace(smooth=False, hidden=False, update=lambda: None)
    core = _Core()
    b = cb.Step1CoreBinding(stack=stack, layer=layer, core=core, channel_names=names,
                            build_display_snapshot=lambda: display,
                            build_viewport_snapshot=lambda: SimpleNamespace(
                                roi_polygon_world=None, roi_world_rect=None),
                            on_unsupported=lambda _w: None)
    b._drawable = lambda _ch: True
    return b, core, names


def test_prepares_only_while_paused_and_at_most_15_channels(app):
    b, core, names = _binding(app)
    assert b.prepare_ahead() is False                 # on screen: not the ahead's
    b.pause()
    core.calls.clear()
    assert b.prepare_ahead() is True and b.ahead
    calls = dict((name, args) for name, args in core.calls)
    assert "resume" in calls and "set_view" in calls
    rows = calls["set_display"][0]
    assert len(rows) == 15                             # the user's cap
    assert {names[r[0]] for r in rows} == set(names[:15])
    assert calls["set_view"] == (1, (0, 100, 0, 100), 3, False)
    core.calls.clear()
    assert b.end_ahead() is True and not b.ahead
    assert [n for n, _ in core.calls] == ["pause"]     # paused again, tiles kept
    assert b.end_ahead() is False


def test_the_entry_ends_a_preparation(app):
    b, core, _names = _binding(app, 3)
    b.pause()
    b.prepare_ahead()
    b.resume()
    assert not b.ahead and not b.paused
    assert b.end_ahead() is False                      # nothing to pause on screen


# ── the window's owner ──────────────────────────────────────────────────────

class _Mount:
    def __init__(self):
        self.host = SimpleNamespace(stack=None)
        self.ahead_stack_open = False
        self.paused = True
        self.source_pending_while_paused = False
        self.done = []
        self.gpu_binding = SimpleNamespace(
            prepare_ahead=lambda: self.done.append("prepare") or True,
            end_ahead=lambda: self.done.append("end"), idle=lambda: self.idle)
        self.gpu_layer = SimpleNamespace(paint_ahead=self._paint,
                                         release_to_coarse=lambda: 0)
        self.idle = False
        self.paints = 0

    def close(self):
        return True

    def pause_requests(self):
        return False

    def open_ahead_stack(self):
        self.done.append("stack")
        self.ahead_stack_open = True
        return True

    def open_ahead_backend(self):
        self.done.append("backend")
        self.ahead_stack_open = False
        self.host.stack = object()
        return True

    def set_mode(self, _mode):
        pass

    def rebuild_paused(self):
        self.done.append("rebuild")
        self.source_pending_while_paused = False
        return True

    def _paint(self):
        self.paints += 1
        if self.paints >= 3:
            self.idle = True
        return {}


def _window(app, monkeypatch, mount):
    w = MainWindow()
    monkeypatch.setattr(w, "_step1_whole_slide", lambda: setattr(w, "_step1_mount", mount) or mount)
    monkeypatch.setattr(w, "_wire_step1_tissue_navigation", lambda: None)
    monkeypatch.setattr(w, "_apply_owner_camera_to", lambda step: mount.done.append(f"camera{step}"))
    monkeypatch.setattr(w, "_step1_ahead_gate", lambda: ("", False))
    w._step1_ahead_armed_at = time.monotonic() - 10.0          # long quiet
    w._step1_mount = mount if mount.host.stack is not None else None
    return w


def _run(w, turns=20):
    """The timer's turns, one after another, until it asks for none."""
    for _ in range(turns):
        timer = w.__dict__.get("_step1_ahead_timer")
        if timer is None or not timer.isActive():
            return
        timer.stop()
        w._step1_ahead_step()


def test_one_piece_per_turn_then_paints_until_idle(app, monkeypatch):
    mount = _Mount()
    w = _window(app, monkeypatch, mount)
    try:
        w._step1_ahead_step()
        assert mount.done == ["stack"]                         # one piece
        w._step1_ahead_timer.stop()
        w._step1_ahead_step()
        assert mount.done == ["stack", "backend"]
        _run(w)
        assert mount.done[2:4] == ["camera1", "prepare"]
        assert mount.paints == 3 and mount.done[-1] == "end"   # idle: the round ends
        assert not w._step1_ahead_timer.isActive()             # and asks for no more
        assert w.__dict__.get("_step1_ahead_round") is None
    finally:
        w.close()


def test_a_build_piece_waits_for_two_seconds_of_quiet(app, monkeypatch):
    mount = _Mount()
    w = _window(app, monkeypatch, mount)
    try:
        w._step1_ahead_armed_at = time.monotonic()             # Step0 just moved
        w._step1_ahead_step()
        assert mount.done == []
        assert w._step1_ahead_timer.isActive()                 # it asks again later
    finally:
        w.close()


def test_a_source_that_moved_is_rebuilt_ahead(app, monkeypatch):
    mount = _Mount()
    mount.host.stack = object()
    mount.source_pending_while_paused = True
    w = _window(app, monkeypatch, mount)
    try:
        w._step1_ahead_step()
        assert mount.done == ["rebuild"]
    finally:
        w.close()


@pytest.mark.parametrize("why,wait", [("a Step2 segmentation is running", True),
                                      ("Step0 is reading", True),
                                      ("Step0 is not on screen", False)])
def test_it_gives_way(app, monkeypatch, why, wait):
    mount = _Mount()
    w = _window(app, monkeypatch, mount)
    try:
        monkeypatch.setattr(w, "_step1_ahead_gate", lambda: (why, wait))
        w._arm_step1_ahead("test")
        w._step1_ahead_timer.stop()
        w._step1_ahead_step()
        assert mount.done == []
        assert w._step1_ahead_timer.isActive() is wait         # waited out, or not
    finally:
        w.close()


def test_the_real_gate(app, monkeypatch):
    w = MainWindow()
    try:
        w._current_step = 1
        assert w._step1_ahead_gate() == ("Step0 is not on screen", False)
        w._current_step = 0
        w.loader = None
        assert w._step1_ahead_gate()[0] == "no Step0 result for Step1 yet"
        w.loader = SimpleNamespace(filepath="/a.tif")
        w.step0_output = {"step0_manifest_path": "/m.json"}
        monkeypatch.setattr(w, "_camera_dataset", lambda step: "/a.tif" if step == 1 else "/b.tif")
        assert w._step1_ahead_gate()[0] == "Step0 shows another slide than Step1"
        monkeypatch.setattr(w, "_camera_dataset", lambda step: "/a.tif")
        monkeypatch.setattr(w, "_busy_reason", lambda: "a Step4 extraction is running")
        assert w._step1_ahead_gate() == ("a Step4 extraction is running", True)
        monkeypatch.setattr(w, "_busy_reason", lambda: None)
        monkeypatch.setattr(w._step0, "production_correction_busy", lambda: None)
        monkeypatch.setattr(w._step0, "reads_busy", lambda: True)
        assert w._step1_ahead_gate() == ("Step0 is reading", True)
        monkeypatch.setattr(w._step0, "reads_busy", lambda: False)
        assert w._step1_ahead_gate() == ("", False)
    finally:
        w.close()


def test_a_step0_camera_write_arms_it_and_the_entry_stops_it(app, monkeypatch):
    w = MainWindow()
    try:
        armed = []
        monkeypatch.setattr(w, "_arm_step1_ahead", lambda why="": armed.append(why))
        w._current_step = 0
        monkeypatch.setattr(w, "_camera_dataset", lambda step: "/a.tif")
        w._camera_write(0, (10.0, 10.0, 1.0), "drag", jump=False)
        assert armed == ["step0-camera"]
        stopped = []
        monkeypatch.setattr(w, "_stop_step1_ahead", lambda: stopped.append(True))
        w._step1_follow_step(0)
        assert stopped == [True]
    finally:
        w.close()


def test_an_entry_between_the_two_build_halves_finishes_the_build(app, monkeypatch):
    """Codex review: entered after the stack and before the backend, the
    entry builds the backend itself -- never a stack with no picture."""
    mount = _Mount()
    w = _window(app, monkeypatch, mount)
    try:
        w._step1_ahead_step()
        assert mount.done == ["stack"] and mount.ahead_stack_open
        w._step1_mount = mount
        w.loader = None                      # (the rest of the entry is not this test's)
        w._step1_follow_step(1)
        assert "backend" in mount.done and not mount.ahead_stack_open
        assert mount.host.stack is not None
    finally:
        w.close()


def test_the_session_is_written_after_the_switch_not_during_it(app, monkeypatch):
    """User ruling 2026-11-03: leaving a step writes its drafts AFTER the new
    step is on screen (several session writes flushed to an HDD took 0.3-1 s
    of the switch). Two quick switches write once, with the last step; a
    close writes at once itself and drops the pending write."""
    from block01.qt import QtTest
    w = MainWindow()
    try:
        wrote = []
        monkeypatch.setattr(w, "_rm_save_view", lambda step: wrote.append(step) or [])
        w._current_step = 0
        w._queue_rm_save_view(1)
        w._queue_rm_save_view(2)
        assert wrote == []                                 # not inside the switch
        QtTest.QTest.qWait(w.RM_SAVE_VIEW_AFTER_MS + 100)
        assert wrote == [2]                                # once, the step shown last
        w._queue_rm_save_view(3)
        w.__dict__.pop("_rm_save_view_pending", None)      # what closeEvent does
        QtTest.QTest.qWait(w.RM_SAVE_VIEW_AFTER_MS + 100)
        assert wrote == [2]
    finally:
        w.close()


def test_the_entry_gives_a_viewer_built_ahead_the_current_mode(app, monkeypatch):
    """User report (2026-11-04): Step1 opened with Fusion lit but an Overlay
    picture. The viewer was built ahead while the window still said Overlay;
    the session then restored Fusion without a reconcile (it tells no viewer),
    and the entry reused the built viewer without telling it either."""
    from block01.ui.main_window import STEP1_PREVIEW_FUSION, STEP1_PREVIEW_OVERLAY
    mount = _Mount()
    modes = []
    mount.set_mode = modes.append
    mount.sync_source = lambda _why: None
    mount.resume_requests = lambda: mount.done.append("resume")
    w = _window(app, monkeypatch, mount)
    try:
        w._step1_preview_mode = STEP1_PREVIEW_OVERLAY
        w._step1_ahead_step()
        w._step1_ahead_step()                          # stack, then backend
        assert mount.host.stack is not None and modes == [STEP1_PREVIEW_OVERLAY]
        w.set_preview_mode(STEP1_PREVIEW_FUSION, force=True, reconcile=False)
        assert modes == [STEP1_PREVIEW_OVERLAY]       # the restore told no viewer
        w.loader = SimpleNamespace(filepath="/x/slide.ome.tiff")
        w._step1_mount_refused_for = None
        w._step1_follow_step(1)
        assert modes[-1] == STEP1_PREVIEW_FUSION
        assert mount.done[-1] == "resume"              # told before it is shown
    finally:
        w.close()


def test_a_finished_restore_tells_every_resident_viewer_the_mode(app, monkeypatch):
    """Codex review: Load weights with a session in the other mode moved the
    buttons but not the viewers already built (Step1, Step3, Regions)."""
    from block01.ui.main_window import STEP1_PREVIEW_FUSION
    w = MainWindow()
    try:
        told = {}
        for name in ("_step1_mount", "_step3_mount", "_regions_view"):
            w.__dict__[name] = SimpleNamespace(
                set_mode=lambda m, n=name: told.__setitem__(n, m))
        for stub in ("_restore_fusion_settings", "_ensure_channels_cached",
                     "_refresh_patch_preview", "_schedule_step1_session_save"):
            monkeypatch.setattr(w, stub, lambda *a, **k: None)
        w.set_preview_mode(STEP1_PREVIEW_FUSION, force=True, reconcile=False)
        assert told == {}
        w._on_display_state_restored()
        assert told == dict.fromkeys(told, STEP1_PREVIEW_FUSION) and len(told) == 3
    finally:
        for name in ("_step1_mount", "_step3_mount", "_regions_view"):
            w.__dict__.pop(name, None)
        w.close()
