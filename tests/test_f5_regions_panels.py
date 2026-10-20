"""Regions looks like Step1 (docs/f5_tma_display_plan.md §13.10, user
2026-10-20): its `Channels` frame from Step1's sources (same frame QSS, same
column background and insets, pixel-compared offscreen), and Step1's / Step3's
patch strip first in its viewer's tool row -- the same names and colours,
the one selection, a click lands the Regions viewer (the shared camera)."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")
pytest.importorskip("pyqtgraph")

from PyQt5 import QtWidgets  # noqa: E402

from test_regions_r2 import _enter_regions, _pump  # noqa: E402
from test_step0_compare_tiles import app  # noqa: E402,F401
from test_step1_shared_camera import _close, _in, _window  # noqa: E402

PATCHES = [(100, 200, 100, 200), (500, 700, 900, 1100), (1500, 1600, 300, 400)]
SHOTS = os.environ.get("F5_PANEL_SHOTS", "")


def _grab(widget, name):
    img = widget.grab().toImage()
    if SHOTS:
        img.save(os.path.join(SHOTS, name))
    ptr = img.convertToFormat(img.Format_RGB32)
    b = ptr.bits()
    b.setsize(ptr.byteCount())
    return np.frombuffer(b, np.uint8).reshape(ptr.height(), ptr.bytesPerLine() // 4, 4)[:, :ptr.width(), :3].copy()


def test_the_channels_frame_is_step1s(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        page = w._regions
        assert page.channels_box.styleSheet() == w._step1_channels_box.styleSheet()
        assert page.channels_box.title() == w._step1_channels_box.title() == "Channels"
        assert page.channels_box.parentWidget().styleSheet() == w._step1_left_panel.styleSheet()
        # pixels: the frame's border and title row look the same
        size = w._step1_channels_box.size()
        page.channels_box.resize(size)
        _enter_regions(rig)
        page.channels_box.resize(size)
        _pump()
        a = _grab(w._step1_channels_box, "step1_channels.png")
        b = _grab(page.channels_box, "regions_channels.png")
        h = min(a.shape[0], b.shape[0])
        assert np.array_equal(a[0:20, 0:100], b[0:20, 0:100])        # the `Channels` title, same pixels
        assert np.array_equal(a[24:h - 24, :3], b[24:h - 24, :3])    # the left border (corners aside)

        def right_border(img):                                       # the light-blue frame's colour
            col = img[30:img.shape[0] - 30, -2]
            vals, counts = np.unique(col.reshape(-1, 3), axis=0, return_counts=True)
            return tuple(vals[counts.argmax()])
        assert right_border(a) == right_border(b)
    finally:
        _close(rig)


def test_the_patch_strip_leads_the_viewer_row_and_follows_the_list(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        strip = w._regions_patch_strip
        assert w._regions.tools_row.itemAt(0).widget() is strip.holder()
        w._on_patches(PATCHES)
        assert [b.text() for b in strip.buttons] == ["P1", "P2", "P3"]
        assert [b.styleSheet() for b in strip.buttons] == [b.styleSheet() for b in w._patch_strip.buttons]
        w._on_patches([])
        assert strip.buttons == []
    finally:
        _close(rig)


def test_a_regions_patch_click_lands_the_shared_camera_and_is_the_selection(app, monkeypatch, tmp_path):
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        w._on_patches(PATCHES)
        _enter_regions(rig)
        _pump(10)
        w._regions_patch_strip.buttons[1].click()
        _pump(20)
        assert w._preview_patch_idx == 1
        assert [b.isChecked() for b in w._patch_strip.buttons] == [False, True, False]
        cam = w._regions_view.mount.current_camera()
        y0, y1, x0, x1 = PATCHES[1]
        assert abs(cam[0] - (x0 + x1) / 2) < 5 and abs(cam[1] - (y0 + y1) / 2) < 5
        shot = w._camera_owner.current(w._camera_dataset(w.REGIONS_STEP))
        assert shot is not None and abs(shot.camera[0] - cam[0]) < 1          # the shared camera
        w._stack.setCurrentIndex(1)
        _in(rig, 1)
        _pump(10)
        c1 = w._step1_mount.current_camera()
        assert abs(c1[0] - cam[0]) < 1 and abs(c1[1] - cam[1]) < 1           # Step1 lands there too
        w._patch_strip.buttons[2].click()                                     # a Step1 choice ...
        assert [b.isChecked() for b in w._regions_patch_strip.buttons] == [False, False, True]
    finally:
        _close(rig)


def test_regions_column_holds_step1s_content_width(app, monkeypatch, tmp_path):
    """codex §13.10 P2: the one content-width floor covers Regions' column too."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        w._hold_step1_channel_floor()
        assert w._regions.left_panel().minimumWidth() == w._step1_left_panel.minimumWidth() > 0
    finally:
        _close(rig)


def test_a_regions_patch_choice_survives_step1s_first_restore(app, monkeypatch, tmp_path):
    """codex §13.10 P2: reopened workspace -> Regions -> choose a patch ->
    first Step1 entry: the restored draft does not replace the choice."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        w._on_patches(PATCHES)
        w._preview_patch_idx = 0
        w._rm_auto_step1 = True                                    # an opened workspace, not restored yet
        saved = []
        monkeypatch.setattr(w, "_schedule_step1_session_save", lambda: saved.append(w._preview_patch_idx))
        w._select_regions_patch(2)
        assert w._rm_patch_after_restore == 2

        def restore(auto=False):                                   # the old draft says patch 0
            w._preview_patch_idx = 0
            w._sync_patch_selection_marks(0)
        monkeypatch.setattr(w, "_load_previous_step1_session", restore)
        monkeypatch.setattr(w, "_handoff_ready", lambda *a: "ready")
        w._go_to_step1()
        _pump()
        assert w._preview_patch_idx == 2 and saved[-1] == 2
        assert [b.isChecked() for b in w._patch_strip.buttons] == [False, False, True]
        cam = w._step1_mount.current_camera() if w._step1_mount.host.stack is not None else None
        if cam is not None:                                        # the camera is on P3 too
            y0, y1, x0, x1 = PATCHES[2]
            assert abs(cam[0] - (x0 + x1) / 2) < 5 and abs(cam[1] - (y0 + y1) / 2) < 5
    finally:
        _close(rig)


def test_a_pending_regions_choice_is_dropped_with_its_dataset(app, monkeypatch, tmp_path):
    """codex §13.10 round 2: a choice pending for workspace A never reaches B."""
    rig = _window(app, monkeypatch, tmp_path)
    try:
        w = rig.w
        w._on_patches(PATCHES)
        w._rm_auto_step1 = True
        monkeypatch.setattr(w, "_schedule_step1_session_save", lambda: None)
        w._select_regions_patch(2)
        assert w._rm_patch_after_restore == 2
        w._discard_step1_dataset_state()
        assert "_rm_patch_after_restore" not in w.__dict__
    finally:
        _close(rig)
