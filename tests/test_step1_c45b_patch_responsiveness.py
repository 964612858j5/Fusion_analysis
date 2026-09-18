"""C4.5b: a Step1 patch edit must not freeze Tissue Preview pan."""

import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

from block01.ui.step0 import step0_page as step0_module  # noqa: E402
from test_overview_patch_editing import _panel as _real_panel  # noqa: E402
from test_step0_patch_release import _page, _settle  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _prepare_panel(panel):
    panel.ds = 1
    panel.ov_h, panel.ov_w = 128, 128
    panel.full_h, panel.full_w = 128, 128
    owner = panel.window()
    if owner is not panel and hasattr(owner, "_roi_patch_section"):
        owner._roi_patch_section.setVisible(True)
    owner.resize(1000, 700)
    owner.show()
    panel.setVisible(True)
    QtTest.QTest.qWaitForWindowExposed(owner)
    assert panel.isVisible()
    panel.vb.setRange(QtCore.QRectF(0, 0, panel.ov_w, panel.ov_h), padding=0)


def _send(panel, kind, pos, button=Qt.LeftButton, buttons=None):
    event = QtGui.QMouseEvent(
        kind, QtCore.QPointF(pos), button,
        buttons if buttons is not None else button, Qt.NoModifier)
    QtWidgets.QApplication.instance().sendEvent(panel.gview.viewport(), event)
    QtWidgets.QApplication.processEvents()


def _drag_patch(panel):
    p0 = QtCore.QPoint(100, 100)
    p1 = QtCore.QPoint(300, 300)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, Qt.LeftButton)
    for i in range(1, 4):
        pos = QtCore.QPoint(
            p0.x() + (p1.x() - p0.x()) * i // 3,
            p0.y() + (p1.y() - p0.y()) * i // 3)
        _send(panel, QtCore.QEvent.MouseMove, pos, Qt.NoButton, Qt.LeftButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease, p1, Qt.LeftButton,
          Qt.NoButton)


def _middle_drag(panel):
    before = panel.vb.viewRange()
    p0 = QtCore.QPoint(250, 250)
    _send(panel, QtCore.QEvent.MouseButtonPress, p0, Qt.MiddleButton)
    for i in range(1, 5):
        pos = QtCore.QPoint(p0.x() - 60 * i // 4,
                            p0.y() - 40 * i // 4)
        _send(panel, QtCore.QEvent.MouseMove, pos, Qt.NoButton,
              Qt.MiddleButton)
    _send(panel, QtCore.QEvent.MouseButtonRelease,
          QtCore.QPoint(p0.x() - 60, p0.y() - 40), Qt.MiddleButton,
          Qt.NoButton)
    return before, panel.vb.viewRange()


def _step1_scope(page):
    page.display = SimpleNamespace(
        state=SimpleNamespace(scope=lambda: "step1"),
        navigator=lambda: None)


def _visible_overview(page):
    panel = _real_panel("patch")
    panel.set_edit_policy(patch_edit=True)
    panel.patches_changed.connect(lambda _patches:
                                  page._reconcile_roi_edit(panel))
    return panel


def test_step1_patch_edit_routes_real_pan_while_geometry_worker_is_busy(
        app, tmp_path, monkeypatch):
    page, _step0_dir = _page(app, tmp_path)
    panel = _visible_overview(page)
    _step1_scope(page)

    hidden_display = []
    hidden_preload = []
    monkeypatch.setattr(page, "_show_channel_from_cache",
                        lambda channel: hidden_display.append(channel))
    monkeypatch.setattr(page, "_start_preload",
                        lambda: hidden_preload.append(True))

    started = threading.Event()
    release = threading.Event()
    real_commit = step0_module.step0_handoff.commit_geometry_only

    def blocked_commit(task, **kwargs):
        started.set()
        assert release.wait(10.0)
        return real_commit(task, **kwargs)

    monkeypatch.setattr(step0_module.step0_handoff,
                        "commit_geometry_only", blocked_commit)
    try:
        _drag_patch(panel)
        assert page.patches, "real viewport patch gesture did not reach Step0"
        assert len(panel._patches) > 0, "real viewport did not create a patch"
        assert started.wait(5.0), "geometry worker did not become busy"

        before, after = _middle_drag(panel)
        assert after != before, "real viewport middle pan did not move ViewBox"
        assert panel._mid_pan_last is None, "middle mouse grab remained open"
        assert hidden_display == [], "hidden Step0 redrew its cached image"
        assert hidden_preload == [], "hidden Step0 started preload"
        assert page.geometry_persist_busy() is True
    finally:
        release.set()
        _settle(page)
        panel.hide()
        page.deleteLater()


def test_step1_scope_keeps_patch_model_and_geometry_persist_submission(
        app, tmp_path, monkeypatch):
    page, _step0_dir = _page(app, tmp_path)
    _step1_scope(page)
    persisted = []
    monkeypatch.setattr(page, "_persist_geometry_edit",
                        lambda: persisted.append(True) or True)
    shown, preloaded = [], []
    monkeypatch.setattr(page, "_show_channel_from_cache",
                        lambda channel: shown.append(channel))
    monkeypatch.setattr(page, "_start_preload", lambda: preloaded.append(True))
    try:
        page.overview._patches.append({"roi_idx": 0,
                                       "coords": (16, 32, 16, 32)})
        page.overview.patches_changed.emit(page.overview._patch_coords())
        assert page.patches == [(0, 16, 0, 16), (16, 32, 16, 32)]
        assert persisted == [True]
        assert shown == []
        assert preloaded == []
    finally:
        page.deleteLater()


def test_returning_to_step0_resyncs_and_preloads_once(app, tmp_path,
                                                       monkeypatch):
    page, _step0_dir = _page(app, tmp_path)
    calls = []
    page.display = SimpleNamespace(
        state=SimpleNamespace(scope=lambda: "step0",
                              display_visible=lambda _ch: False,
                              selected_channel=lambda: "CD3"))
    page._channel_rows = {}
    monkeypatch.setattr(page, "_on_channel_selected_by_id",
                        lambda channel: calls.append(("channel", channel)))
    monkeypatch.setattr(page, "_start_preload",
                        lambda: calls.append(("preload", None)))
    try:
        page.resync_display_from_state()
        assert calls == [("channel", "CD3"), ("preload", None)]
    finally:
        page.deleteLater()


def test_pending_session_autosave_does_not_precede_immediate_middle_pan(
        app, tmp_path, monkeypatch):
    from block01.ui.main_window import MainWindow

    page = _page(app, tmp_path)[0]
    panel = page.overview
    _prepare_panel(panel)
    saves = []
    window = MainWindow.__new__(MainWindow)
    window._step1_restore_active = False
    window.loader = object()
    window._step1_session_timer = QtCore.QTimer()
    window._step1_session_timer.setSingleShot(True)
    window._step1_session_timer.timeout.connect(
        lambda: saves.append(time.monotonic()))
    try:
        window._schedule_step1_session_save()
        before, after = _middle_drag(panel)
        assert after != before
        assert saves == [], "500 ms autosave ran before immediate pan events"
        QtTest.QTest.qWait(550)
        QtWidgets.QApplication.processEvents()
        assert len(saves) == 1
    finally:
        window._step1_session_timer.stop()
        panel.hide()
        page.deleteLater()
