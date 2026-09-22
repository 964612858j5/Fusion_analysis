"""G3.2b.4B2C: the hidden controller stops READING once the GPU layer is
the picture -- and goes on being the one camera.

`Step1GpuLayer` takes the picture over, and `set_marker_visible(False)` puts
the controller's own raw/marker/precise ImageItems at opacity 0. It kept
READING for them: measured on the real slide (G3.2b.4B2), 73 % of a cold
Patch's provider reads and 79 % of a cold Tissue landing's were for tiles
that can never reach the screen, competing with the binding's own
multi-channel tiles for the one scheduler.

`ExploreController.set_viewport_requests_enabled(False)` is what stops them.
What it must NOT stop is everything the viewer still depends on that
controller for: the ViewBox, the mouse, `jump_to`, both timers,
`interaction_event`, `gesture_quiet`, `selection_context_changed`, and the
view rectangle the Tissue Preview reads.

The rig is `tests/test_step1_gpu_takeover.py`'s -- the REAL production mount
over a real `ExploreController`, real `TileScheduler` and the real
`Step1GpuLayer` on its own GL context. Requests are told apart by their
GENERATION, which is how the scheduler itself tells them apart:

    ("step1-gpu-binding", ...)  the binding's, and the only ones that can
                                reach the screen while the GPU layer is up
    ("raw", n) / ("precise", n) the controller's own viewport supply
    ("dapi_raw", n)             the nucleus overlay's copy of it
"""

import importlib.util
import os
import pathlib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

_RIG_PATH = pathlib.Path(__file__).with_name("test_step1_gpu_takeover.py")
_spec = importlib.util.spec_from_file_location("g3_takeover_rig", _RIG_PATH)
RIG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RIG)

from block01.ui.step1_viewer_mount import (  # noqa: E402
    BACKEND_CPU_FALLBACK, BACKEND_GPU)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ── telling the two producers apart, the way the scheduler does ───────

BINDING = "step1-gpu-binding"
CONTROLLER_KINDS = ("raw", "precise", "dapi_raw")


def _generation_kind(generation):
    if isinstance(generation, tuple) and generation:
        if generation[0] == BINDING:
            return "binding"
        if generation[0] in CONTROLLER_KINDS:
            return "controller"
    return "other"


def _requests(rig):
    out = []
    for scheduler in rig.schedulers:
        out.extend(scheduler.public_requests)
    return out


def _counts(rig):
    kinds = {"binding": 0, "controller": 0, "other": 0}
    for request in _requests(rig):
        kinds[_generation_kind(request.generation)] += 1
    return kinds


def _controller(rig):
    return rig.mount.host.stack.controller


def _settle(rig, rounds=40):
    RIG._settle(rig.app, rounds=rounds)


# ══ A. taking over turns the controller's own supply off ═════════════

def test_a_successful_gpu_takeover_stops_the_controllers_own_tile_reads(app):
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        controller = _controller(rig)
        view_box = controller.view.view_box

        assert rig.mount.backend == BACKEND_GPU
        assert controller.viewport_requests_enabled is False, (
            "the controller is still reading for layers nobody can see")
        assert rig.mount.coordinator is None, (
            "the CPU composition was built beside the GPU backend")
        # The SAME controller and the SAME ViewBox: this is a supply switch,
        # not a second viewer.
        assert _controller(rig) is controller
        assert controller.view.view_box is view_box
    finally:
        RIG._close(rig)


def test_the_switch_defaults_to_on_so_nothing_else_changes(app):
    """Step0's full image, the compare strip and the CPU path never call it."""
    from block01.viewer.explore_view import ExploreController
    import inspect

    signature = inspect.signature(
        ExploreController.set_viewport_requests_enabled)
    assert "enabled" in signature.parameters
    rig = RIG._mount(app, gpu=False)
    try:
        assert rig.mount.backend != BACKEND_GPU
        assert _controller(rig).viewport_requests_enabled is True, (
            "a non-GPU mount turned the controller's own supply off")
    finally:
        RIG._close(rig)


# ══ B. the camera, the signals and the view rectangle all survive ════

def test_panning_still_moves_the_camera_and_asks_only_for_what_is_shown(app):
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)
        view_box = controller.view.view_box

        interactions, quiets = [], []
        controller.interaction_event.connect(
            lambda kind, snap: interactions.append(kind))
        controller.gesture_quiet.connect(lambda snap: quiets.append(snap))
        rects = []
        rig.mount.view_rect_published.connect(lambda *a: rects.append(a)) \
            if hasattr(rig.mount, "view_rect_published") else None

        before = _counts(rig)
        rect_before = view_box.viewRect()

        # A real camera move through the real ViewBox.
        controller.jump_to(300, 300, 400, 400)
        _settle(rig, rounds=60)
        controller.jump_to(500, 500, 300, 300)
        _settle(rig, rounds=80)

        after = _counts(rig)
        rect_after = view_box.viewRect()

        assert (rect_after.x(), rect_after.y(), rect_after.width()) != \
            (rect_before.x(), rect_before.y(), rect_before.width()), \
            "the camera did not move"
        assert interactions, "interaction_event stopped firing"
        assert quiets, "gesture_quiet stopped firing"
        assert rig.mount.view_rect_l0() is not None, (
            "the Tissue Preview lost its view rectangle")
        assert after["binding"] > before["binding"], (
            "the GPU binding stopped asking for the viewport it must draw")
        assert after["controller"] == before["controller"], (
            f"the hidden controller issued "
            f"{after['controller'] - before['controller']} viewport requests")
    finally:
        RIG._close(rig)


def test_the_picture_that_arrives_is_still_the_c1_reference(app):
    """The gate may not change a single pixel."""
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=80)
        RIG._wait(rig.app, lambda: rig.mount.gpu_binding.stats()["coarse_channels"],
                  timeout=20.0)
        _settle(rig, rounds=40)
        shown = RIG._grab(rig)
        reference = RIG._screen_reference(rig)
        import numpy as np
        diff = np.abs(shown[..., :3].astype(int) - reference[..., :3].astype(int))
        assert diff.max() <= 1, f"the picture moved by {diff.max()} LSB"
    finally:
        RIG._close(rig)


# ══ C / D. the two landings, through the real public entries ═════════

@pytest.mark.parametrize("entry", ["patch", "tissue"])
def test_a_landing_asks_only_for_the_tiles_that_will_be_drawn(app, entry):
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        RIG._wait(rig.app, lambda: rig.mount.gpu_binding.stats()["coarse_channels"],
                  timeout=20.0)
        _settle(rig, rounds=40)
        controller = _controller(rig)
        view_box = controller.view.view_box

        before = _counts(rig)
        if entry == "patch":
            bbox = (256, 768, 256, 768)
            moved = rig.mount.show_patch(bbox)
        else:
            moved = rig.mount.jump_to_point(900, 900, 320)
        assert moved is True
        _settle(rig, rounds=100)

        after = _counts(rig)
        rect = view_box.viewRect()
        assert rect.width() > 0 and rect.height() > 0
        if entry == "patch":
            # STILL THE WHOLE BBOX: the camera semantics are untouched.
            assert rect.width() >= 512 - 1e-6
            assert rect.height() >= 512 - 1e-6
        assert after["binding"] > before["binding"], (
            "the binding stopped asking for the landing's own tiles")
        assert after["controller"] == before["controller"], (
            f"{entry}: the hidden controller issued "
            f"{after['controller'] - before['controller']} viewport requests")
    finally:
        RIG._close(rig)


# ══ E. the CPU fallback keeps its own supply ═════════════════════════

def test_a_gpu_that_cannot_start_leaves_the_controller_supplying_itself(app):
    def _refuses(_stack):
        raise RuntimeError("no GPU for this test")

    rig = RIG._mount(app, gpu_layer_factory=_refuses)
    try:
        assert rig.mount.backend == BACKEND_CPU_FALLBACK, rig.mount.backend
        controller = _controller(rig)
        assert controller.viewport_requests_enabled is True, (
            "a failed GPU start left the visible picture unsupplied")
        before = _counts(rig)
        controller.jump_to(300, 300, 400, 400)
        _settle(rig, rounds=80)
        after = _counts(rig)
        assert after["controller"] > before["controller"], (
            "the CPU fallback's own layers were never asked for")
    finally:
        RIG._close(rig)


# ══ F. handing the picture back ══════════════════════════════════════

def test_restoring_the_legacy_view_leaves_the_controller_quiet(app):
    """`restore_legacy` shows the old PATCH widget and HIDES this host.

    An earlier revision of this block re-enabled the controller here, on the
    reading that "the old renderer is the picture again". It is not: the
    widget that comes back is the legacy patch view, and `restore_legacy`
    hides the whole-slide host -- ViewBox and all -- on the very next line.
    Waking the controller up would buy a round of reads for a widget nobody
    can see, which is the waste this block removes.
    """
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)
        assert controller.viewport_requests_enabled is False

        before = _counts(rig)
        # Kept alive on purpose: the layout belongs to this container, and a
        # container collected mid-test takes the layout's C++ side with it.
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        legacy = QtWidgets.QWidget()
        layout.addWidget(legacy)
        rig.mount.install(layout, legacy)
        assert rig.mount.restore_legacy() is True
        _settle(rig, rounds=60)

        # `isHidden()` and not `isVisible()`: these widgets live in a
        # container this test never shows, so `isVisible()` is False for
        # both regardless. The explicit hidden flag is what `restore_legacy`
        # actually sets, and it is the premise being checked -- the
        # whole-slide host goes away and the legacy patch widget comes back.
        assert rig.mount.host.isHidden() is True, (
            "the whole-slide host was not hidden; this test's premise is wrong")
        assert legacy.isHidden() is False
        assert controller.viewport_requests_enabled is False, (
            "the hidden whole-slide controller was woken up again")
        after = _counts(rig)
        assert after["controller"] == before["controller"], (
            f"a hidden viewer issued "
            f"{after['controller'] - before['controller']} requests")
    finally:
        RIG._close(rig)


def test_a_source_rebuild_never_wakes_the_outgoing_controller(app):
    """The controller `_stop_gpu_backend` is leaving is about to be destroyed.

    `_gpu_source_changed` stops the GPU backend and then rebuilds the whole
    stack. Turning the outgoing controller's supply back on first spends a
    round of reads on a stack that is already on its way out -- against the
    same scheduler the new one is about to need.
    """
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)
        assert controller.viewport_requests_enabled is False

        before = _counts(rig)
        rig.mount._stop_gpu_backend(restore_controller=False)
        _settle(rig, rounds=60)

        assert controller.viewport_requests_enabled is False
        after = _counts(rig)
        assert after["controller"] == before["controller"], (
            f"the outgoing controller issued "
            f"{after['controller'] - before['controller']} requests on its "
            "way out")
    finally:
        RIG._close(rig)


def test_handing_over_to_the_cpu_whole_slide_renderer_restores_the_supply(app):
    """The one case that DOES restore it: the same stack, CPU picture.

    No caller in this mount reaches it today -- `open()` and
    `_gpu_source_changed` both arrive at the CPU renderer with a brand-new
    stack whose controller is on by default. It is kept, and gated, because
    `restore_controller=True` is the documented meaning of the parameter and
    a future hand-over on the SAME stack must not have to rediscover it.
    """
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)
        assert controller.viewport_requests_enabled is False

        before = _counts(rig)
        rig.mount._stop_gpu_backend(restore_controller=True)
        _settle(rig, rounds=60)

        assert controller.viewport_requests_enabled is True
        after = _counts(rig)
        assert after["controller"] > before["controller"], (
            "the hand-over needed the user to move the camera first")
    finally:
        RIG._close(rig)


def test_the_switch_is_idempotent_and_re_issues_only_once(app):
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)

        assert controller.set_viewport_requests_enabled(False) is False, (
            "turning it off twice reported a change")
        before = _counts(rig)
        assert controller.set_viewport_requests_enabled(True) is True
        _settle(rig, rounds=60)
        once = _counts(rig)["controller"] - before["controller"]
        assert once > 0

        marker = _counts(rig)
        assert controller.set_viewport_requests_enabled(True) is False, (
            "turning it on twice reported a change")
        _settle(rig, rounds=40)
        assert _counts(rig)["controller"] == marker["controller"], (
            "a second enable issued a second round of the same requests")
    finally:
        RIG._close(rig)


# ══ I. leaving, coming back and closing ══════════════════════════════

def test_leaving_and_returning_leaves_the_controller_quiet(app):
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)

        rig.mount.deactivate()
        before = _counts(rig)
        _settle(rig, rounds=40)
        assert _counts(rig)["controller"] == before["controller"], (
            "a hidden Step1 kept its controller reading")
        assert controller.viewport_requests_enabled is False

        rig.mount.activate()
        _settle(rig, rounds=60)
        assert controller.viewport_requests_enabled is False, (
            "coming back to Step1 restarted the invisible reads")
        assert rig.mount.backend == BACKEND_GPU
    finally:
        RIG._close(rig)


def test_closing_asks_for_nothing_on_the_way_out_or_after(app):
    """Teardown is not a reason to start reading.

    `close()` stops the GPU backend and then tears the stack down. An
    earlier revision restored the controller's supply inside that stop, so
    closing the window issued one last round of viewport requests.
    """
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)

        before = _counts(rig)
        rig.mount.close()
        _settle(rig, rounds=60)
        during = _counts(rig)
        assert during["controller"] == before["controller"], (
            f"closing issued {during['controller'] - before['controller']} "
            "controller requests on the way out")
        assert controller.viewport_requests_enabled is False

        _settle(rig, rounds=40)
        assert _counts(rig) == during, "something kept asking after close"
        assert rig.mount.gpu_binding is None
        assert rig.mount.gpu_layer is None
    finally:
        for raw in rig.raws:
            raw.release_all()
        QtWidgets.QApplication.processEvents()


# ══ the two things the gate must NOT become ══════════════════════════

def test_closing_the_supply_does_not_lock_the_camera_or_post_a_badge(app):
    """It is not `suspend_for_production`, and must never be built from it.

    That method is the tempting shortcut -- it already makes
    `_issue_raw_requests` issue nothing. It also locks the mouse, writes a
    status badge, joins the floor thread and waits the scheduler idle, and
    every one of those is forbidden here: the user goes on panning, zooming
    and landing while the GPU layer draws.
    """
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)
        view_box = controller.view.view_box

        assert controller.viewport_requests_enabled is False
        assert controller.suspended is False, (
            "the supply gate was implemented as a production suspend")
        assert view_box.state["mouseEnabled"] == [True, True], (
            "the camera was locked when the supply was closed")
        status = getattr(controller.view, "status_text", None)
        if callable(status):
            assert not status(), "a status badge appeared"
    finally:
        RIG._close(rig)


def test_closing_the_supply_leaves_a_pending_gesture_quiet_alone(app):
    """The camera timers are the binding's own clock -- do not stop them.

    `gesture_quiet` comes from the controller's settle timer and is what
    `Step1GpuBinding` catches up on. Stopping the timers when the supply is
    closed would swallow the quiet that was already pending.
    """
    rig = RIG._mount(app)
    try:
        RIG._require_gpu(rig)
        _settle(rig, rounds=60)
        controller = _controller(rig)
        controller.set_viewport_requests_enabled(True)
        _settle(rig, rounds=40)

        quiets = []
        controller.gesture_quiet.connect(lambda snap: quiets.append(snap))
        # Start a gesture, then close the supply while its settle is pending.
        controller.view.view_box.setRange(xRange=(200, 700), yRange=(200, 700),
                                          padding=0)
        controller.set_viewport_requests_enabled(False)
        RIG._wait(rig.app, lambda: bool(quiets), timeout=5.0)

        assert quiets, (
            "closing the supply swallowed the gesture_quiet that was already "
            "pending; the binding never learns the camera stopped")
    finally:
        RIG._close(rig)
