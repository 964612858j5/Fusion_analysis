"""Step1's Viewer tab, in the real window.

Block C4 of `docs/step1_rework_plan.md`, the structural half: the whole-slide
viewer is mounted in the tab that was already there, the two mode buttons
drive the ONE compose binding, a patch button and a click on the shared Tissue
Preview are the two gestures that reach the ONE camera, and Step1 composes
only while it is the step on screen.

A REAL `MainWindow`, so what is asserted is the product's own wiring rather
than a hand-built page. The stack itself is a stub: what this module is about
is which object is connected to which, not the pixels -- those are
`test_step1_viewer_mount.py`'s, on the real stack.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from block01.ui import step1_viewer_mount as mount_module  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _no_dialogs(monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))


class _Loader:
    shape = (256, 256)
    name_map = {}
    correction_config = {}

    def __init__(self, path="/tmp/c4-takeover.ome.tiff"):
        self.filepath = path
        self._names = ["DAPI", "CD3", "CD8"]
        self.ch_map = {c: i for i, c in enumerate(self._names)}

    def channel_names(self):
        return list(self._names)

    def read_region(self, channel, y0, y1, x0, x1, downsample=1,
                    normalize=True):
        return np.zeros(((y1 - y0) or 1, (x1 - x0) or 1), np.float32)


class _Layer:
    def __init__(self, *a, **k):
        self.cleared = 0
        self.attached = False

    def attach(self):
        self.attached = True
        return True

    def detach(self):
        self.attached = False
        return True

    def on_tile_composed(self, *a, **k):
        return None

    def clear(self):
        self.cleared += 1

    def teardown(self):
        self.attached = False


class _Stack(SimpleNamespace):
    pass


def _stub_stack(app):
    """A stack with the shape the mount reads, and no pixels."""
    from block01.viewer.tile_types import (
        SourceIdentity, TileGridSpec,
    )

    grid = TileGridSpec(tile_size=512, source_chunk_shape=(),
                        grid_version="v1")
    identity = SourceIdentity(dataset_path="/tmp/c4-takeover.ome.tiff",
                              dataset_fingerprint="1:1", stage="step1",
                              corrected_artifact="tok")
    view_box = SimpleNamespace(viewRect=lambda: QtWidgets.QApplication
                               .instance() and _Rect(),
                               addedItems=[])
    view = SimpleNamespace(view_box=view_box,
                           set_status_text=lambda text: None)
    controller = SimpleNamespace(grid=grid, level=0, _visible_tiles={(0, 0)},
                                 gesture_quiet=_Signal(),
                                 set_marker_visible=lambda visible: None)
    scheduler = SimpleNamespace(request=lambda req, cb: None,
                                cancel_generation=lambda gen: None,
                                _cache_for=lambda key: None)
    provider = SimpleNamespace(source_identity=lambda: identity,
                               num_levels=1,
                               level_downsample_yx=lambda level: (1.0, 1.0))
    return _Stack(view=view, controller=controller, scheduler=scheduler,
                  provider=provider)


class _Rect:
    def width(self):
        return 512.0

    def height(self):
        return 512.0

    def x(self):
        return 0.0

    def y(self):
        return 0.0


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *a):
        self.slots.append(slot)

    def disconnect(self, slot=None):
        self.slots = []

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


def _window(app, monkeypatch, tmp_path):
    from block01.ui.main_window import MainWindow

    stack = _stub_stack(app)
    jumps = []
    opens = []

    class _Host(QtWidgets.QWidget):
        def __init__(self, *a, **k):
            super().__init__()
            self._stack = None
            self.channel = "CD3"
            self.status = ""
            self.missing_notice = []

        @property
        def stack(self):
            return self._stack

        def open(self, *a, **k):
            opens.append((a, k))
            self._stack = stack
            return stack

        def jump_to(self, y0, x0, width, height):
            jumps.append((y0, x0, width, height))
            return True

        def refresh_status(self):
            return self.status

        def set_channel(self, channel):
            return False

        def apply_display_mapping(self, *a, **k):
            return True

        def teardown(self, **k):
            self._stack = None

    monkeypatch.setattr(mount_module, "Step1ComposedLayer", _Layer)
    monkeypatch.setattr(mount_module.Step1ViewerBinding, "_source_moved",
                        lambda self, **k: False)

    w = MainWindow()
    w._schedule_step1_session_save = lambda: None
    w._save_step1_session = lambda *a, **k: None
    w.loader = _Loader()
    w._step0.loader = w.loader
    w._step0.nucleus_channel = "DAPI"
    w._step0._rebuild_channel_list()
    w.config.set_channels(w.loader.channel_names())
    w.config.load_panel({"markers": {"CD3": 1.0, "CD8": 0.5}}, "DAPI")
    w.config.set_nucleus("DAPI", 1.0)
    manifest = tmp_path / "step0_roi_result.json"
    manifest.write_text(json.dumps(
        {"handoff_schema_version": 2,
         "channel_remap_config_hash": "remap-1",
         "source_identity": {"dataset_path": w.loader.filepath,
                             "stage": "raw"}}), encoding="utf-8")
    w.step0_output = {"step1_dir": str(tmp_path), "output_dir": str(tmp_path),
                      "step0_manifest_path": str(manifest),
                      "channel_remap_config_hash": "remap-1"}
    w._corrected_decisions = {}
    w._corrected_zarr_path = ""
    w._active_roi = None
    # The mount's host is the stub; everything else is the product's.
    w._step1_mount_host_factory = _Host
    original = mount_module.Step1WholeSlideMount.__init__

    def _init(self, window, host=None, parent=None):
        original(self, window, host=_Host(), parent=parent)

    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "__init__", _init)
    return SimpleNamespace(w=w, jumps=jumps, opens=opens, stack=stack)


def _close(rig):
    rig.w._display.shutdown("test")
    rig.w.deleteLater()
    QtWidgets.QApplication.processEvents()


def _in(rig, step):
    rig.w._set_step_active(step)
    QtWidgets.QApplication.processEvents()


# ── 1. the tab ────────────────────────────────────────────────────────

def test_entering_step1_mounts_the_whole_slide_viewer_in_the_viewer_tab(
        app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        assert getattr(rig.w, "_step1_mount", None) is None

        _in(rig, 1)

        mount = rig.w._step1_mount
        layout = rig.w.viewer_tab.layout()
        assert layout.indexOf(mount.host) >= 0, (
            "the whole-slide viewer is not in the Viewer tab")
        assert rig.w.prev_gv.isVisibleTo(rig.w.viewer_tab) is False
        assert layout.indexOf(rig.w.prev_gv) >= 0, (
            "the old patch view was removed rather than kept as a rollback")
        assert rig.opens, "the slide was never opened"
    finally:
        _close(rig)


def test_no_new_entry_point_was_added(app, monkeypatch, tmp_path):
    """UI_SURFACE_RULES: the tab, the buttons and the popup are the ones
    that were already there."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        tabs = rig.w._step1_right_tabs
        before = [tabs.tabText(i) for i in range(tabs.count())]

        _in(rig, 1)

        after = [tabs.tabText(i) for i in range(tabs.count())]
        assert after == before == ["Viewer", "Patch Results"]
        mount = rig.w._step1_mount
        assert not mount.host.findChildren(QtWidgets.QPushButton)
        assert not mount.host.findChildren(QtWidgets.QCheckBox)
    finally:
        _close(rig)


# ── 2. the two buttons, the one binding ───────────────────────────────

def test_the_mode_buttons_drive_the_one_compose_binding(app, monkeypatch,
                                                        tmp_path):
    from block01.viewer import step1_compose as compose_core

    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        mount = rig.w._step1_mount
        assert mount.compose.mode == compose_core.MODE_OVERLAY

        rig.w._btn_mode_fusion.click()

        assert mount.compose.mode == compose_core.MODE_FUSION
        assert mount.layer.cleared >= 1, (
            "the old mode's tiles were not cleared")

        rig.w._btn_mode_overlay.click()
        assert mount.compose.mode == compose_core.MODE_OVERLAY
    finally:
        _close(rig)


# ── 3. the two gestures, the one camera ───────────────────────────────

def test_a_patch_button_is_an_anchor_on_the_one_camera(app, monkeypatch,
                                                       tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        rig.w._all_patches = [(100, 356, 200, 456)]
        rig.jumps.clear()

        rig.w._select_preview_patch(0)

        assert rig.jumps == [(100, 200, 256, 256)], rig.jumps
    finally:
        _close(rig)


def test_a_tissue_preview_click_reaches_the_same_camera(app, monkeypatch,
                                                        tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        rig.jumps.clear()

        moved = rig.w._on_step1_tissue_navigate(700, 900)

        assert moved is True
        assert rig.jumps and rig.jumps[0][2] == rig.jumps[0][3], rig.jumps
        y0, x0, size, _h = rig.jumps[0]
        assert (y0, x0) == (700 - size // 2, 900 - size // 2)
    finally:
        _close(rig)


def test_a_tissue_preview_click_is_ignored_from_another_step(
        app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        _in(rig, 0)
        rig.jumps.clear()

        assert rig.w._on_step1_tissue_navigate(700, 900) is False
        assert rig.jumps == []
    finally:
        _close(rig)


# ── 4. only while it is on screen ─────────────────────────────────────

def test_leaving_step1_stops_it_composing_and_returning_refreshes_once(
        app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        mount = rig.w._step1_mount
        assert mount.active is True

        _in(rig, 0)
        assert mount.active is False
        before = mount.coordinator.generation

        _in(rig, 1)
        assert mount.active is True
        assert mount.coordinator.generation != before, (
            "coming back to Step1 composed nothing")
    finally:
        _close(rig)


def test_step0_does_not_build_the_step1_viewer(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 0)
        assert getattr(rig.w, "_step1_mount", None) is None
    finally:
        _close(rig)


# ── 5. a slide the viewer cannot open ─────────────────────────────────

def test_a_slide_that_cannot_be_opened_keeps_the_old_patch_view(
        app, monkeypatch, tmp_path):
    """FAIL CLOSED. The whole-slide viewer needs a real pyramid; a project
    it cannot open must leave the OLD picture on screen -- which is what the
    rollback path is kept for -- and must not take the step down with it."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        def _explode(*a, **k):
            raise FileNotFoundError("no such pyramid")

        monkeypatch.setattr(mount_module.Step1WholeSlideMount, "open",
                            _explode)

        _in(rig, 1)

        # `isVisibleTo`, not `isVisible`: the window is never shown in a
        # headless run, so every widget in it reports invisible.
        assert rig.w.prev_gv.isVisibleTo(rig.w.viewer_tab) is True, (
            "the old patch view was not put back")
        assert rig.w._step1_mount.host.isVisibleTo(rig.w.viewer_tab) is False
    finally:
        _close(rig)


def test_an_unopenable_slide_is_not_retried_on_every_step_change(
        app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        tries = []

        def _explode(self, *a, **k):
            tries.append(1)
            raise FileNotFoundError("no such pyramid")

        monkeypatch.setattr(mount_module.Step1WholeSlideMount, "open",
                            _explode)

        _in(rig, 1)
        _in(rig, 0)
        _in(rig, 1)

        assert len(tries) == 1, tries
    finally:
        _close(rig)


# ── 6. C4.3: the four things the window still owes the viewer ─────────

def test_a_moved_handoff_rebinds_the_source(app, monkeypatch, tmp_path):
    """The ROI, the corrected product and the handoff revision all fold into
    one source identity. When it moves, the viewer must rebind: the frames
    on screen were composed from tiles of the OLD identity."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        mount = rig.w._step1_mount
        rebinds = []
        monkeypatch.setattr(mount_module.Step1WholeSlideMount, "source_changed",
                            lambda self, reason="source": rebinds.append(reason))
        monkeypatch.setattr(mount.viewer.__class__, "source_moved",
                            lambda self: True)

        _in(rig, 0)
        _in(rig, 1)
        assert rebinds, "coming back with a moved source did not rebind"

        rebinds.clear()
        rig.w._step1_sync_whole_slide_source("handoff")
        assert rebinds == ["handoff"], (
            "a handoff applied while standing in Step1 did not rebind")
    finally:
        _close(rig)


def test_an_unmoved_source_is_not_rebound(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        mount = rig.w._step1_mount
        rebinds = []
        monkeypatch.setattr(mount_module.Step1WholeSlideMount, "source_changed",
                            lambda self, reason="source": rebinds.append(reason))
        monkeypatch.setattr(mount.viewer.__class__, "source_moved",
                            lambda self: False)

        _in(rig, 0)
        _in(rig, 1)

        assert rebinds == [], "an unmoved source was rebound anyway"
    finally:
        _close(rig)


def test_closing_the_window_closes_the_viewer(app, monkeypatch, tmp_path):
    """The mount owns a compose executor, a scheduler and a raw handle; the
    loaders the window stops below are not any of them."""
    rig = _window(app, monkeypatch, tmp_path)
    _in(rig, 1)
    mount = rig.w._step1_mount
    closed = []
    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "close",
                        lambda self: closed.append(True))

    rig.w.close()
    QtWidgets.QApplication.processEvents()

    assert closed == [True], "the whole-slide viewer outlived the window"
    assert getattr(rig.w, "_step1_mount", None) is None


def test_the_hidden_patch_path_stops_reading_while_the_slide_is_shown(
        app, monkeypatch, tmp_path):
    """The old renderer is behind the new picture. Reading a panel's worth
    of patch channels for a widget nobody can see is the cost C4 removes --
    and the path itself stays, so it reads again when it is back on top."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        rig.w._all_patches = [(0, 256, 0, 256)]
        reads = []
        monkeypatch.setattr(rig.w, "_needed_channels",
                            lambda: reads.append("asked") or ["CD3"])

        rig.w._ensure_channels_cached(0)
        assert reads == [], "the hidden patch path read behind the new picture"
        assert rig.w._refresh_patch_preview() is None

        rig.w._step1_mount.restore_legacy()
        rig.w._ensure_channels_cached(0)
        assert reads == ["asked"], "the rollback path stopped working"
    finally:
        _close(rig)


def test_the_shared_tissue_preview_routes_to_step1_only_there(
        app, monkeypatch, tmp_path):
    """THE REAL shared signal, not the handler called by hand: one popup,
    one `navigate_requested`, and the step decides who answers it."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        # THE PRODUCT'S OWN lazy creation: the popup does not exist until
        # someone opens the Tissue Preview, which is how the routing came to
        # be wired against a popup that was not there.
        popup = rig.w._display.ensure_navigator()
        QtWidgets.QApplication.processEvents()
        overview = popup.overview
        rig.jumps.clear()

        overview.navigate_requested.emit(700, 900)
        QtWidgets.QApplication.processEvents()
        assert rig.jumps, "the shared preview did not reach Step1's camera"

        rig.jumps.clear()
        _in(rig, 0)
        overview.navigate_requested.emit(400, 400)
        QtWidgets.QApplication.processEvents()

        assert rig.jumps == [], (
            "the shared preview moved Step1's camera from another step")
    finally:
        _close(rig)


def test_the_handoff_reader_syncs_the_viewer_on_success(app, monkeypatch,
                                                        tmp_path):
    """A handoff applied while the user is standing in Step1 has to reach the
    viewer. Driving the whole reader needs a published project on disk, so
    what is checked here is that its SUCCESS path calls the sync -- the sync
    itself is gated above, on the real mount."""
    import ast
    import inspect
    import textwrap

    from block01.ui.main_window import MainWindow

    source = textwrap.dedent(inspect.getsource(
        MainWindow._load_step0_roi_result))
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "_step1_sync_whole_slide_source"]
    assert calls, (
        "the handoff reader does not tell the whole-slide viewer its source "
        "moved")


# ── 7. C4.4: closing late, and one camera per click ───────────────────

def test_a_refused_close_leaves_the_viewer_working(app, monkeypatch, tmp_path):
    """`closeEvent` can still be REFUSED -- a fusion job that has not stopped,
    a live overview read -- and a window that goes on living must go on
    drawing. The viewer is closed in the irreversible half, with the shared
    windows, not before the refusals."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        mount = rig.w._step1_mount
        closed = []
        monkeypatch.setattr(mount_module.Step1WholeSlideMount, "close",
                            lambda self: closed.append(True))
        # One patch loader still running is one of the window's own refusals.
        class _Loader:
            def isRunning(self):
                return True

            def stop(self):
                return None

            def wait(self, _ms=0):
                return False

            def __getattr__(self, name):
                return lambda *a, **k: None

        rig.w._patch_loaders = {0: _Loader()}
        monkeypatch.setattr(QtCore.QTimer, "singleShot",
                            staticmethod(lambda ms, fn: None))

        event = QtGui.QCloseEvent()
        rig.w.closeEvent(event)

        assert event.isAccepted() is False, "the close was not refused"
        assert closed == [], (
            "the viewer was torn down for a close that did not happen")
        assert rig.w._step1_mount is mount
        assert mount.host.stack is not None
    finally:
        rig.w._patch_loaders = {}
        _close(rig)


def test_an_accepted_close_still_closes_the_viewer(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    _in(rig, 1)
    closed = []
    monkeypatch.setattr(mount_module.Step1WholeSlideMount, "close",
                        lambda self: closed.append(True))

    event = QtGui.QCloseEvent()
    rig.w.closeEvent(event)
    QtWidgets.QApplication.processEvents()

    assert event.isAccepted() is not False
    assert closed == [True], "the viewer outlived an accepted close"
    assert getattr(rig.w, "_step1_mount", None) is None


def test_one_click_moves_one_camera(app, monkeypatch, tmp_path):
    """THE REAL shared signal again, now with BOTH listeners on it: Step0's
    page and Step1's mount hear the same `navigate_requested`, and the step
    on screen decides which camera moves."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        _in(rig, 1)
        popup = rig.w._display.ensure_navigator()
        QtWidgets.QApplication.processEvents()
        step0_jumps = []
        monkeypatch.setattr(rig.w._step0, "_compare_mode",
                            lambda: step0_jumps.append("asked") or False)

        rig.jumps.clear()
        popup.overview.navigate_requested.emit(700, 900)
        QtWidgets.QApplication.processEvents()

        assert rig.jumps, "Step1's camera did not move from Step1"
        assert step0_jumps == [], (
            "Step0's camera answered a click meant for Step1")

        _in(rig, 0)
        rig.jumps.clear()
        step0_jumps.clear()
        popup.overview.navigate_requested.emit(300, 300)
        QtWidgets.QApplication.processEvents()

        assert step0_jumps == ["asked"], "Step0's camera did not answer in Step0"
        assert rig.jumps == [], (
            "Step1's camera answered a click meant for Step0")
    finally:
        _close(rig)
