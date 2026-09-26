"""Block 2a: a viewer that is not on screen asks for nothing.

The GPU binding (`Step1GpuBinding.pause/resume`) and the whole-slide mount
(`Step1WholeSlideMount.pause_requests/resume_requests`):

  * paused, no new read is issued: camera events, a display change (a tick,
    a colour, an Intensity window, a weight), a mode switch and a source
    replacement all ask for nothing; a read already under way may land and
    is kept, but a coarse that completes does not start its fine;
  * pausing and resuming twice connects nothing twice;
  * resuming catches up once from the state as it now reads: the missing
    coarse and fine are asked for, the latest mode is drawn, a source that
    moved meanwhile is rebuilt;
  * only an active, unpaused viewer draws its rectangle on the shared
    navigator; the camera reasons carry the viewer's own prefix.

GPU pixels on real hardware are not covered here (they need a real OpenGL
context); these gates use the recording layer and the CPU composition.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

import test_step1_gpu_sources as gs  # noqa: E402
import test_step1_viewer_mount as vm  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── the GPU binding ───────────────────────────────────────────────────

def _gpu_rig(active=("A", "B"), tiles=((0, 0), (1, 0))):
    provider = gs._Provider(level_shape=(8, 8), levels=1)
    controller = gs._Controller(provider, visible_tiles=tiles)
    scheduler = gs._Scheduler()
    layer = gs._RecordingLayer()
    binding, display = gs._binding(provider, scheduler, controller, layer,
                                   display=gs._display(active))
    return SimpleNamespace(provider=provider, controller=controller, scheduler=scheduler,
                           layer=layer, binding=binding, display=display)


def _settled(app, rig):
    """Coarse delivered, fine for the viewport delivered: nothing pending."""
    rig.binding.source_changed()
    gs._deliver_all(app, rig.scheduler, list(rig.scheduler.requests),
                    value=np.full((4, 4), 0.2, np.float32))
    marker = len(rig.scheduler.requests)
    rig.binding.update_viewport(rig.controller.snapshot())
    gs._deliver_all(app, rig.scheduler, rig.scheduler.requests[marker:],
                    value=np.full((4, 4), 0.9, np.float32))


def test_paused_camera_events_plan_nothing(app):
    rig = _gpu_rig()
    try:
        _settled(app, rig)
        assert rig.binding.pause() is True
        marker = len(rig.scheduler.requests)
        moved = rig.controller.set_snapshot(visible_tiles=((1, 1),), epoch=7)
        rig.controller.interaction_event.emit("PAN", moved)
        rig.controller.interaction_event.emit("NAVIGATOR_JUMP", moved)
        rig.controller.gesture_quiet.emit(moved)
        rig.binding.update_viewport(moved)
        gs._events(app)
        assert rig.scheduler.requests[marker:] == []
    finally:
        rig.binding.dispose()


def test_a_coarse_that_lands_while_paused_starts_no_fine(app):
    rig = _gpu_rig()
    try:
        rig.binding.source_changed()
        coarse = list(rig.scheduler.requests)
        assert coarse and rig.binding.coarse_pending_channels() == ("A", "B")
        rig.binding.pause()
        marker = len(rig.scheduler.requests)
        gs._deliver_all(app, rig.scheduler, coarse, value=np.full((4, 4), 0.2, np.float32))
        stats = rig.binding.stats()
        assert stats["coarse_channels"] == ("A", "B"), "the landed coarse was not kept"
        assert rig.scheduler.requests[marker:] == [], "a landed coarse started its fine"
    finally:
        rig.binding.dispose()


def test_display_and_draft_changes_while_paused_ask_for_nothing(app):
    rig = _gpu_rig(active=("A",))
    try:
        _settled(app, rig)
        rig.binding.pause()
        marker = len(rig.scheduler.requests)
        # a new tick (B has no coarse at all), a colour / Intensity / weight change
        rig.display[0] = gs._display(("A", "B"))
        rig.binding.refresh_display()
        rig.display[0] = gs.DisplaySnapshot(gs.MODE_OVERLAY, {"A": (0.1, 0.8, 1.2), "B": (0.0, 1.0, 1.0)},
                                            {"A": 0.3, "B": 1.0},
                                            {"A": (0.0, 0.0, 1.0), "B": (0.0, 1.0, 0.0)})
        rig.binding.refresh_display()
        gs._events(app)
        assert rig.scheduler.requests[marker:] == []
    finally:
        rig.binding.dispose()


def test_pausing_and_resuming_twice_connects_nothing_twice(app):
    rig = _gpu_rig()
    try:
        _settled(app, rig)
        c = rig.controller
        before = (c.receivers(c.interaction_event), c.receivers(c.gesture_quiet))
        assert rig.binding.pause() is True and rig.binding.pause() is False
        assert (c.receivers(c.interaction_event), c.receivers(c.gesture_quiet)) == (
            before[0] - 1, before[1] - 1)
        assert rig.binding.resume() is True and rig.binding.resume() is False
        assert (c.receivers(c.interaction_event), c.receivers(c.gesture_quiet)) == before
    finally:
        rig.binding.dispose()


def test_resume_catches_up_from_the_latest_state(app):
    rig = _gpu_rig(active=("A",))
    try:
        _settled(app, rig)
        rig.binding.pause()
        rig.display[0] = gs._display(("A", "B"))                      # B ticked meanwhile
        moved = rig.controller.set_snapshot(visible_tiles=((1, 1), (0, 1)), epoch=9)
        rig.binding.refresh_display()
        marker = len(rig.scheduler.requests)
        assert rig.binding.resume() is True
        new = rig.scheduler.requests[marker:]
        assert gs._coarse_requests(rig.scheduler, "B"), "the new channel's coarse was not asked for"
        a_fine = [r for r in new if r.key.channel == "A" and r.priority != gs.PRIORITY_COARSE]
        assert {(r.key.tile.tx, r.key.tile.ty) for r in a_fine} == {(1, 1), (0, 1)} - {(0, 0), (1, 0)}
        # the rest lands and the picture follows the latest state
        gs._deliver_all(app, rig.scheduler, new, value=np.full((4, 4), 0.5, np.float32))
        later = rig.scheduler.requests[marker + len(new):]
        gs._deliver_all(app, rig.scheduler, later, value=np.full((4, 4), 0.5, np.float32))
        stats = rig.binding.stats()
        assert stats["coarse_channels"] == ("A", "B")
        assert stats["fine_tiles"].get("A", 0) > 0 and stats["fine_tiles"].get("B", 0) > 0
        drawn = {s.channel for s in rig.layer.calls[-1][0].channels}
        assert drawn == {"A", "B"}
        assert moved.epoch == 9
    finally:
        rig.binding.dispose()


def test_a_source_moved_while_paused_is_taken_up_on_resume(app):
    rig = _gpu_rig()
    try:
        _settled(app, rig)
        rig.binding.pause()
        rig.provider.move_source("two")
        rig.controller.set_snapshot(visible_tiles=((0, 0),), epoch=11)
        marker = len(rig.scheduler.requests)
        rig.binding.source_changed()
        assert rig.scheduler.requests[marker:] == []
        rig.binding.resume()
        coarse = [r for r in rig.scheduler.requests[marker:] if r.priority == gs.PRIORITY_COARSE]
        assert coarse and {r.key.source for r in coarse} == {rig.provider.source_identity()}
    finally:
        rig.binding.dispose()


# ── the mount, CPU composition ────────────────────────────────────────

class _Overview:
    def __init__(self):
        self.rects, self.cleared = [], 0

    def set_current_view_rect(self, rect):
        self.rects.append(rect)

    def clear_current_view_rect(self):
        self.cleared += 1


def _with_navigator(rig):
    overview = _Overview()
    rig.window._display.navigator = lambda: SimpleNamespace(overview=overview)
    return overview


def _reads(rig):
    return sum(len(raw.reads) for raw in rig.raws)


def test_paused_mount_reads_and_composes_nothing(app):
    rig = vm._mount(app)
    try:
        assert vm._wait_for_tiles(app, rig)
        vm._settle(app, rounds=20)
        assert rig.mount.pause_requests() is True
        assert rig.mount.pause_requests() is False
        generation = rig.mount.coordinator.generation
        reads = _reads(rig)
        # the camera, the draft, the shared display state, the mode
        view_box = rig.mount.host.stack.view.view_box
        view_box.setRange(xRange=(0, vm.SLIDE / 3), yRange=(0, vm.SLIDE / 3), padding=0)
        rig.domain.edit_channel_weight("CD8", 0.9, origin="user")
        with rig.state.using_scope(vm.STEP1_SCOPE):
            rig.state.set_color("CD3", "#123456")
            rig.state.set_display_visible("CD8", False)
            rig.state.set_mapping("CD3", 0.1, 0.5, 1.0)
        assert rig.mount.set_mode(vm.compose_core.MODE_FUSION) is True
        rig.mount.show_patch([0, vm.SLIDE // 4, 0, vm.SLIDE // 4])
        vm._settle(app, rounds=30)
        assert rig.mount.coordinator.generation == generation, "a paused viewer composed"
        assert _reads(rig) == reads, "a paused viewer read tiles"
    finally:
        vm._close(rig)


def test_resuming_the_mount_draws_the_latest_mode(app):
    rig = vm._mount(app)
    try:
        assert vm._wait_for_tiles(app, rig)
        rig.mount.pause_requests()
        rig.mount.set_mode(vm.compose_core.MODE_FUSION)
        generation = rig.mount.coordinator.generation
        assert rig.mount.resume_requests() is True and rig.mount.resume_requests() is False
        assert rig.mount.active and not rig.mount.paused
        assert vm._wait_for_tiles(app, rig)
        vm._settle(app, rounds=40)
        assert rig.mount.coordinator.generation != generation
        checked = False
        for (level, tx, ty), entry in sorted(vm._composed(rig).items()):
            expected = vm._reference(rig, level, tx, ty, vm.compose_core.MODE_FUSION)
            if expected is None:
                continue
            row, col = vm._roi_pixel(level)
            assert vm._pixel(entry.item, row, col) == tuple(int(v) for v in expected[row, col])
            checked = True
            break
        assert checked
    finally:
        vm._close(rig)


def test_a_source_change_while_paused_waits_for_resume(app):
    rig = vm._mount(app)
    try:
        assert vm._wait_for_tiles(app, rig)
        rig.mount.pause_requests()
        opened = []
        real_open = rig.mount.viewer.open
        rig.mount.viewer.open = lambda *a, **k: (opened.append(1), real_open(*a, **k))[1]
        reads = _reads(rig)
        assert rig.mount.source_changed("test") is None
        vm._settle(app, rounds=10)
        assert opened == [] and _reads(rig) == reads, "a paused viewer rebuilt its source"
        rig.mount.resume_requests()
        vm._settle(app, rounds=20)
        assert opened == [1], "the source change was not taken up on resume"
    finally:
        vm._close(rig)


def test_only_an_active_unpaused_viewer_draws_the_navigator_rectangle(app):
    rig = vm._mount(app)
    try:
        overview = _with_navigator(rig)
        rig.mount.activate()
        assert rig.mount.publish_view_rect() is True and overview.rects
        count = len(overview.rects)
        rig.mount.deactivate()
        assert rig.mount.publish_view_rect() is False
        rig.mount.activate()
        rig.mount.pause_requests()
        rig.mount.host.stack.view.view_box.setRange(xRange=(0, 10), yRange=(0, 10), padding=0)
        vm._settle(app, rounds=5)
        assert rig.mount.publish_view_rect() is False
        assert len(overview.rects) == count + 1           # the one from re-activating only
        rig.mount.resume_requests()
        assert len(overview.rects) > count + 1
    finally:
        vm._close(rig)


@pytest.mark.parametrize("prefix", [None, "step3"])
def test_the_camera_reasons_carry_the_viewers_prefix(app, prefix):
    raws = []
    state = vm._state(app)
    domain = vm._domain()
    window = vm._Window(state, domain, corrected_path="/tmp/c4-none.zarr")
    host = vm.Step1ViewerHost(stack_factory=vm._stack_factory(raws))
    kwargs = {} if prefix is None else {"camera_reason": prefix}
    mount = vm.Step1WholeSlideMount(window, host=host, gpu=False, **kwargs)
    reasons = []
    mount.camera_sink = lambda camera, reason: reasons.append(reason)
    try:
        mount.open("CD3")
        vm._settle(app, rounds=20)
        mount.show_patch([0, vm.SLIDE // 4, 0, vm.SLIDE // 4])
        mount.jump_to_point(vm.SLIDE // 2, vm.SLIDE // 2, vm.SLIDE // 4)
        want = prefix or "step1"
        assert f"{want}-patch" in reasons and f"{want}-preview" in reasons
        assert all(r == want or r.startswith(want + "-") for r in reasons)
    finally:
        mount.close()
