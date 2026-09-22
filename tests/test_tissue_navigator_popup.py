"""v14.2a: TissueNavigatorPopup shell — window state + viewer-compatible viewport API.

The popup is for spatial navigation + ROI management only (reuses OverviewPanel);
it is NOT a second image viewer. These tests pin the shell behavior and the
viewport-rect API that matches v14.3 HighQualityImageViewer, including explicit
rejection of a mismatched coordinate_space.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def _mk(app):
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
    return TissueNavigatorPopup()


def _img_local_rect(x0=5, x1=25, y0=10, y1=30, shape=(64, 64)):
    return {
        "coordinate_space": "image_local_pixels",
        "x0": float(x0), "x1": float(x1),
        "y0": float(y0), "y1": float(y1),
        "image_shape": [shape[0], shape[1]],
    }


# ── 1. Construct offscreen ───────────────────────────────────────────────────
def test_popup_constructs_offscreen(app):
    p = _mk(app)
    assert p is not None
    assert p.windowTitle().startswith("Tissue Preview")


# ── 2. show / hide ───────────────────────────────────────────────────────────
def test_popup_show_hide(app):
    p = _mk(app)
    p.show()
    assert p.isVisible()
    p.hide()
    assert not p.isVisible()


# ── 3. minimize / restore state ──────────────────────────────────────────────
def test_popup_minimize_restore(app):
    p = _mk(app)
    assert p.is_minimized() is False
    p.minimize_to_bar()
    assert p.is_minimized() is True
    assert p.overview.isVisible() is False         # body hidden when minimized
    p.restore_from_bar()
    assert p.is_minimized() is False
    # toggle path
    p.toggle_minimized()
    assert p.is_minimized() is True
    p.toggle_minimized()
    assert p.is_minimized() is False


# ── 4 + 5. viewport API present + stores image-local rect ────────────────────
def test_popup_viewport_api_stores_image_local_rect(app):
    p = _mk(app)
    assert p.current_viewport_rect() is None
    ok = p.set_viewport_rect(_img_local_rect())
    assert ok is True
    got = p.current_viewport_rect()
    assert got["coordinate_space"] == "image_local_pixels"
    assert got["x0"] == 5.0 and got["x1"] == 25.0
    assert got["y0"] == 10.0 and got["y1"] == 30.0
    assert got["image_shape"] == [64, 64]
    p.clear_viewport_rect()
    assert p.current_viewport_rect() is None


# ── 6. explicit rejection of a mismatched coordinate_space ───────────────────
def test_popup_rejects_wrong_coordinate_space(app):
    p = _mk(app)
    p.set_viewport_rect(_img_local_rect(x0=1, x1=2, y0=3, y1=4))
    before = p.current_viewport_rect()
    bad = {"coordinate_space": "global_wsi",
           "x0": 0.0, "x1": 1.0, "y0": 0.0, "y1": 1.0}
    with pytest.raises(ValueError):
        p.set_viewport_rect(bad)
    # the invalid rect was NOT stored (and did not drop the prior valid one)
    after = p.current_viewport_rect()
    assert after == before
    assert after["coordinate_space"] == "image_local_pixels"


# ── 7. owned by Step0 without creating files ─────────────────────────────────
def test_step0_owns_popup_without_creating_files(app, tmp_path):
    import os
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.output_dir = str(tmp_path)
    before = set(os.listdir(tmp_path))
    s.toggle_tissue_navigator()          # create + show
    assert s._tissue_navigator_popup is not None
    s.toggle_tissue_navigator()          # hide
    s.show_tissue_navigator()            # show again — still one instance
    assert isinstance(s._tissue_navigator_popup,
                      type(s._tissue_navigator_popup))
    after = set(os.listdir(tmp_path))
    assert before == after, "popup toggling must not create files"


# ── 8. no channel-overlay / marker / remap widgets in the popup ──────────────
def test_popup_has_no_channel_overlay_widgets(app):
    p = _mk(app)
    # the popup must not become a second image viewer
    for forbidden in ("_canvas", "_workbench", "_cond_workbench",
                      "_histogram", "_layer_list", "channel_remap"):
        assert not hasattr(p, forbidden), forbidden
    import inspect
    from block01.ui.widgets import tissue_navigator_popup as mod
    src = inspect.getsource(mod)
    # check actual imports/construction (prose mentions in docstrings are fine)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            for bad in ("ChannelWorkbench", "ChannelViewerCanvas",
                        "HighQualityImageViewer", "ChannelHistogramPanel",
                        "channel_workbench", "channel_viewer_canvas",
                        "high_quality_image_viewer", "channel_histogram"):
                assert bad not in stripped, stripped
    for bad in ("ChannelWorkbench(", "ChannelViewerCanvas(",
                "HighQualityImageViewer(", "ChannelHistogramPanel("):
        assert bad not in src, bad


# ── 9. OverviewPanel reuse (no forked ROI logic) ─────────────────────────────
def test_popup_reuses_overview_panel(app):
    from block01.ui.step0.overview_panel import OverviewPanel
    p = _mk(app)
    assert isinstance(p.overview, OverviewPanel)
    # ROI API is delegated to the reused panel
    assert p.roi_count() == len(p.overview.get_rois())


# ── 10. existing OverviewPanel mouse/ROI behavior not globally broken ────────
def test_overview_panel_default_mouse_behavior_unchanged(app):
    # The popup must not globally flip OverviewPanel's existing mouse settings.
    # A fresh standalone OverviewPanel (as used in Step0) keeps mouse disabled on
    # its ViewBox (custom ROI drawing goes through its eventFilter, not ViewBox
    # mouse). Constructing a popup must not change that for other instances.
    from block01.ui.step0.overview_panel import OverviewPanel
    _ = _mk(app)  # construct a popup (its own OverviewPanel instance)
    fresh = OverviewPanel(None, "", lazy=True)
    state = fresh.vb.state["mouseEnabled"]
    assert state == [False, False]


# ── 11. no forbidden dependency-wall imports ─────────────────────────────────
def test_no_forbidden_imports_in_popup_module():
    import inspect
    from block01.ui.widgets import tissue_navigator_popup as mod
    src = inspect.getsource(mod)
    for forbidden in ("hq_source_resolver", "remap_promotion",
                      "promote_remap_config", "validate_step2_remap_config",
                      "segment_merge_worker"):
        assert forbidden not in src, forbidden


# ── step0-fix-tissue-navigator: thumbnail load + window min/max + drop toolbar min ─
class _FakeLoader:
    def __init__(self, shape=(2000, 2200)):
        self.shape = shape
        self.ch_map = {"DAPI": 0}

    def channel_names(self):
        return ["DAPI"]


# A: set_overview_context triggers the SAME OverviewPanel load path, once per loader.
def test_set_overview_context_triggers_thumbnail_load(app, monkeypatch):
    import block01.ui.step0.overview_panel as ovp
    calls = {"n": 0}
    monkeypatch.setattr(ovp.OverviewPanel, "_load_overview",
                        lambda self: calls.__setitem__("n", calls["n"] + 1))
    p = _mk(app)
    ld = _FakeLoader()
    p.set_overview_context(loader=ld, nuc_ch="DAPI")
    assert calls["n"] == 1                          # load triggered
    p.set_overview_context(rois=[], patches=[])     # ROI reconcile, same loader
    assert calls["n"] == 1                          # NOT reloaded on every edit


def test_no_loader_does_not_trigger_load(app, monkeypatch):
    import block01.ui.step0.overview_panel as ovp
    calls = {"n": 0}
    monkeypatch.setattr(ovp.OverviewPanel, "_load_overview",
                        lambda self: calls.__setitem__("n", calls["n"] + 1))
    p = _mk(app)
    p.set_overview_context(rois=[], patches=[])     # no loader
    assert calls["n"] == 0


# A: completion clears the "Loading..." state and sets the thumbnail image.
def test_overview_completion_clears_loading(app):
    import numpy as np
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
    p = TissueNavigatorPopup(loader=_FakeLoader(), nuc_ch="DAPI")
    ov = p.overview
    assert "Loading" in ov.status.text()            # starts in loading state
    ov.full_h, ov.full_w = 2000, 2200
    ov._t0 = 0
    ov._on_overview_loaded(np.ones((50, 55), dtype=np.float32))
    assert ov.img_item.image is not None            # thumbnail set
    assert "Loading" not in ov.status.text()         # loading cleared


# B: native window controls — windowType is a real Window (not Tool) with min/max/close.
def test_window_has_native_min_max_close(app):
    from PyQt5.QtCore import Qt
    p = _mk(app)
    assert p.windowType() == Qt.Window               # NOT Qt.Tool (no min/max on X11)
    f = int(p.windowFlags())
    assert f & int(Qt.WindowMinimizeButtonHint)
    assert f & int(Qt.WindowMaximizeButtonHint)
    assert f & int(Qt.WindowCloseButtonHint)


# C: redundant toolbar minimize button removed; header content (label) kept.
def test_toolbar_minimize_button_removed(app):
    from PyQt5 import QtWidgets
    p = _mk(app)
    assert not hasattr(p, "_btn_min")
    # no push-buttons left in the header bar
    assert p._bar.findChildren(QtWidgets.QPushButton) == []
    # the real header content remains
    assert "Tissue Preview" in p._bar_label.text()
    # collapse-to-bar API still callable (no _btn_min crash)
    p.minimize_to_bar()
    assert p.is_minimized() is True
    p.restore_from_bar()
    assert p.is_minimized() is False


# ── G3.2b.3: the success line must not eat a strip of the canvas ────────────

class _JumpLoader:
    """A slide the size the user actually reported."""
    shape = (59040, 42000)

    def channel_names(self):
        return ["DAPI"]


def _shown_popup(app):
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
    popup = TissueNavigatorPopup(_JumpLoader(), "DAPI")
    popup.resize(420, 520)
    popup.show()
    for _ in range(6):
        app.processEvents()
    return popup


def _load_overview(app, panel, rows=48, cols=34):
    """The REAL success path: the panel installs a finished overview read."""
    import numpy as np
    panel.ds = max(1, int(round(panel.full_h / float(rows))))
    panel._on_overview_loaded(np.zeros((rows, cols), np.float32))
    for _ in range(6):
        app.processEvents()


def _band_in_popup(popup, widget):
    """A widget's rectangle in the popup's own coordinates."""
    from PyQt5 import QtCore
    rect = widget.geometry()
    top_left = widget.mapTo(popup, QtCore.QPoint(0, 0))
    return QtCore.QRect(top_left, rect.size())


def test_the_overview_success_line_is_hidden_inside_the_popup(app):
    """Gate 1: the metadata line is not on screen in the Tissue Preview."""
    popup = _shown_popup(app)
    try:
        panel = popup.overview
        assert panel.status.isVisible(), "the Loading line must start visible"
        _load_overview(app, panel)
        text = " ".join(panel.status.text().split())
        assert text.startswith("Full image ") and "| Overview " in text, text
        assert not panel.status.isVisible(), \
            f"the success line is still on screen: {text!r}"
    finally:
        popup.close()


def test_hiding_the_success_line_gives_the_canvas_its_room_back(app):
    """Gate 2: the row is RELEASED, not merely blanked."""
    popup = _shown_popup(app)
    try:
        panel = popup.overview
        before_status = _band_in_popup(popup, panel.status)
        before_view = _band_in_popup(popup, panel.gview)
        assert before_status.height() > 0, "the status line had no height"

        _load_overview(app, panel)

        after_view = _band_in_popup(popup, panel.gview)
        assert after_view.height() > before_view.height(), (
            f"the canvas did not grow: {before_view.height()} -> "
            f"{after_view.height()}")
        # The band the status line used to own is now canvas.
        viewport = panel.gview.viewport()
        band_centre = before_status.center()
        local = viewport.mapFrom(popup, band_centre)
        assert viewport.rect().contains(local), (
            f"the old status band {before_status} is still not inside the "
            f"canvas viewport {_band_in_popup(popup, viewport)}")
    finally:
        popup.close()


def test_a_real_click_on_the_old_status_band_asks_to_navigate(app):
    """Gate 3: a real Qt click there, through the product's own event path."""
    from PyQt5 import QtCore, QtTest

    popup = _shown_popup(app)
    try:
        panel = popup.overview
        panel._set_mode("patch")
        before_status = _band_in_popup(popup, panel.status)
        _load_overview(app, panel)

        seen = []
        panel.navigate_requested.connect(lambda y, x: seen.append((y, x)))
        viewport = panel.gview.viewport()
        local = viewport.mapFrom(popup, before_status.center())
        assert viewport.rect().contains(local)

        QtTest.QTest.mouseClick(viewport, QtCore.Qt.LeftButton,
                                QtCore.Qt.NoModifier, local)
        for _ in range(4):
            app.processEvents()

        assert len(seen) == 1, f"one click, {len(seen)} navigation requests"
        y, x = seen[0]
        assert 0 <= y < panel.full_h and 0 <= x < panel.full_w, (y, x)
    finally:
        popup.close()


@pytest.mark.parametrize("message", [
    "Loading...",
    "Loading overview, please wait...",
    "Overview load failed: no such file",
    "⚠ Patch centre is outside all ROIs",
    "Patch editing is not available in this step.",
])
def test_everything_the_user_has_to_read_stays_visible(app, message):
    """Gate 4: only the pure metadata line is hidden."""
    popup = _shown_popup(app)
    try:
        panel = popup.overview
        _load_overview(app, panel)
        assert not panel.status.isVisible()

        panel.status.setText(message)
        for _ in range(4):
            app.processEvents()
        assert panel.status.isVisible(), f"{message!r} was hidden"
        assert panel.status.text() == message
    finally:
        popup.close()


def test_the_status_line_comes_and_goes_with_the_message(app):
    """Gate 5: a round trip, with the canvas following it each way."""
    from PyQt5 import QtCore

    popup = _shown_popup(app)
    try:
        panel = popup.overview
        viewport = panel.gview.viewport()
        band = _band_in_popup(popup, panel.status)
        assert panel.status.isVisible()          # Loading...

        _load_overview(app, panel)               # success -> hidden
        assert not panel.status.isVisible()
        grown = panel.gview.height()
        assert viewport.rect().contains(viewport.mapFrom(popup, band.center()))

        panel.status.setText("Overview load failed: disk went away")
        for _ in range(6):
            app.processEvents()
        assert panel.status.isVisible(), "a failure must come back on screen"
        assert panel.gview.height() < grown, "the canvas kept the room"

        panel.status.setText("Full image 59040×42000 px  |  "
                             "Overview 48×34 px  (0.3s)")
        for _ in range(6):
            app.processEvents()
        assert not panel.status.isVisible()
        assert panel.gview.height() == grown
        assert viewport.rect().contains(viewport.mapFrom(popup, band.center()))
        del QtCore
    finally:
        popup.close()


def test_step0_s_own_overview_panel_still_shows_the_line(app):
    """Gate 6: the policy belongs to this popup, not to the shared class."""
    from block01.ui.step0.overview_panel import OverviewPanel

    panel = OverviewPanel(_JumpLoader(), "DAPI", lazy=True)
    panel.resize(420, 420)
    panel.show()
    for _ in range(6):
        app.processEvents()
    try:
        _load_overview(app, panel)
        text = " ".join(panel.status.text().split())
        assert text.startswith("Full image ") and "| Overview " in text
        assert panel.status.isVisible(), \
            "Step0's own panel lost its status line: the class was changed"
    finally:
        panel.close()


# ── G3.2b.3.1: the row UNDER the status line must not take its place ───────

def _real_shaped_toolbar():
    """The ROI/patch toolbar's shape: mode switches, as Step0 mounts them."""
    from PyQt5 import QtWidgets
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for text in ("Draw ROI", "Delete ROI", "Draw patch", "Clear patches"):
        lay.addWidget(QtWidgets.QPushButton(text))
    return w


def _real_shaped_lists():
    """The ROI/Patch lists' shape: the two lists Step0 mounts under the view."""
    from PyQt5 import QtWidgets
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for title, rows in (("ROIs", ["ROI_1  4 pts  12000×9000px  patches:3"]),
                        ("Patches", ["P1", "P2", "P3"])):
        lay.addWidget(QtWidgets.QLabel(title))
        listing = QtWidgets.QListWidget()
        for row in rows:
            listing.addItem(row)
        lay.addWidget(listing)
    return w


def _step1_popup(app, with_lists=True):
    """The popup as Step1 actually shows it: delete_only + patch_editable."""
    from block01.ui.widgets.tissue_navigator_popup import TissueNavigatorPopup
    popup = TissueNavigatorPopup(_JumpLoader(), "DAPI")
    popup.set_roi_toolbar(_real_shaped_toolbar())
    if with_lists:
        popup.set_roi_lists(_real_shaped_lists())
    popup.resize(420, 620)
    popup.show()
    for _ in range(8):
        app.processEvents()
    panel = popup.overview
    panel.set_edit_policy(roi_create=False, roi_delete=True, patch_edit=True)
    panel._set_mode("patch")
    return popup


def _with_real_roi_and_patches(app, panel):
    """Real ROI/Patch information, so the panel's info row has content."""
    _load_overview(app, panel)
    panel._rois = [{"name": "ROI_1", "color": "#88ccff",
                    "polygon_display": [(0, 0), (33, 0), (33, 47), (0, 47)],
                    "bbox_fullres": [0, panel.full_h, 0, panel.full_w],
                    "patch_indices": [0, 1, 2]}]
    # The panel's own record shape: {"roi_idx", "coords"} in full-res pixels,
    # placed in the upper half so they are nowhere near the bottom edge the
    # click gate uses.
    panel._patches = [
        {"roi_idx": 0, "coords": (2000, 6000, 2000, 6000)},
        {"roi_idx": 0, "coords": (8000, 12000, 9000, 13000)},
        {"roi_idx": 0, "coords": (14000, 18000, 3000, 7000)},
    ]
    panel._rebuild_patch_artists()
    panel._update_info()
    for _ in range(8):
        app.processEvents()


def test_the_popup_drops_the_info_row_the_lists_already_show(app):
    """The row under the status line keeps 18 px even when its text is empty.

    Hiding the success line alone just lets this one move up into the strip
    it freed, so the band beside the bottom of the tissue is still not
    canvas. It is dropped only while the popup's own ROI/Patch lists -- which
    say the same thing -- are mounted.
    """
    from PyQt5 import QtCore, QtWidgets

    bare = _step1_popup(app, with_lists=False)
    try:
        _with_real_roi_and_patches(app, bare.overview)
        assert bare.overview._info_lbl.isVisible(), \
            "a popup with no lists of its own must keep the info row"
    finally:
        bare.close()

    popup = _step1_popup(app, with_lists=True)
    try:
        panel = popup.overview
        _with_real_roi_and_patches(app, panel)
        assert not panel.status.isVisible()
        assert not panel._info_lbl.isVisible(), \
            "the duplicated info row is still between canvas and click"

        # NOTHING BUT CONTROLS IS LEFT BELOW THE CANVAS. The panel's own
        # patch-control row stays -- it is a control, not a caption, and
        # removing controls is not what this fixes. What must be gone is
        # every pure TEXT row between the canvas and the lists.
        viewport = panel.gview.viewport()
        canvas_bottom = viewport.mapTo(
            popup, QtCore.QPoint(0, viewport.height())).y()
        lists_top = popup._roi_lists.mapTo(popup, QtCore.QPoint(0, 0)).y()
        layout = panel.layout()
        below = []
        for index in range(layout.count()):
            child = layout.itemAt(index).widget()
            if child is None or not child.isVisible():
                continue
            top = child.mapTo(popup, QtCore.QPoint(0, 0)).y()
            if canvas_bottom <= top < lists_top:
                below.append(child)
        assert all(not isinstance(child, QtWidgets.QLabel) for child in below), (
            "a text row is still sitting between the canvas and the lists: "
            f"{[type(c).__name__ for c in below]}")
        tall = viewport.height()

        # Put the row back the way it was and the canvas loses that height
        # again -- the same popup, so this compares like with like.
        popup._roi_lists = None
        popup._apply_overview_status_policy()
        for _ in range(6):
            app.processEvents()
        assert panel._info_lbl.isVisible()
        assert panel.gview.viewport().height() < tall, (
            f"the row was costing the canvas nothing: "
            f"{panel.gview.viewport().height()} vs {tall}")
    finally:
        popup.close()


def test_the_bottom_edge_of_the_canvas_navigates_in_the_real_hierarchy(app):
    """The gate the first round got wrong: the REAL popup, clicked at its
    very bottom edge, with the toolbar, the lists, Step1's edit policy and
    real ROI/Patch information all in place."""
    from PyQt5 import QtCore, QtTest

    popup = _step1_popup(app, with_lists=True)
    try:
        panel = popup.overview
        _with_real_roi_and_patches(app, panel)
        assert panel.edit_policy() == {"roi_create": False, "roi_delete": True,
                                       "patch_edit": True}
        viewport = panel.gview.viewport()

        for inset in (1, 2):
            seen = []
            handle = panel.navigate_requested.connect(
                lambda y, x, out=seen: out.append((y, x)))
            point = QtCore.QPoint(viewport.width() // 2,
                                  viewport.height() - inset)
            assert viewport.rect().contains(point)
            QtTest.QTest.mouseClick(viewport, QtCore.Qt.LeftButton,
                                    QtCore.Qt.NoModifier, point)
            for _ in range(4):
                app.processEvents()
            panel.navigate_requested.disconnect(handle)

            assert len(seen) == 1, \
                f"{inset} px inside the bottom edge: {len(seen)} requests"
            y, x = seen[0]
            assert 0 <= y < panel.full_h and 0 <= x < panel.full_w, (y, x)
            assert y > panel.full_h * 0.8, \
                f"a click at the bottom edge landed at y={y} of {panel.full_h}"
    finally:
        popup.close()


def test_step0_s_own_panel_keeps_both_its_status_and_info_rows(app):
    """The policy is this popup's. Step0's page shows both rows as before."""
    from block01.ui.step0.overview_panel import OverviewPanel

    panel = OverviewPanel(_JumpLoader(), "DAPI", lazy=True)
    panel.resize(420, 520)
    panel.show()
    for _ in range(6):
        app.processEvents()
    try:
        _with_real_roi_and_patches(app, panel)
        assert panel.status.isVisible(), "Step0 lost its status line"
        assert panel._info_lbl.isVisible(), "Step0 lost its info row"
        assert panel._info_lbl.text(), "the info row should have content here"
    finally:
        panel.close()


# ── G3.2b.3.2: the hidden success line must not come back as a TOOLTIP ──
#
# G3.2b.3 hid the "Full image … | Overview …" line and, reasoning that a
# reader should still be able to find it, wrote it into the canvas's
# tooltip. Real-machine acceptance: resting the pointer anywhere on the
# Tissue Preview popped it up a moment later, over the picture the user is
# aiming at. A tooltip IS a surface. The user asked for it to go
# (2026-09-22), so these gates require it to be GONE, not relocated.

def _tooltips(panel):
    """Both places Qt can take a QGraphicsView's tooltip from."""
    viewport = panel.gview.viewport()
    return (panel.gview.toolTip(),
            "" if viewport is None else viewport.toolTip())


def test_the_hidden_success_line_leaves_no_tooltip_behind(app):
    """Gate 1: hidden means hidden -- not moved into a hover."""
    popup = _shown_popup(app)
    try:
        panel = popup.overview
        _load_overview(app, panel)
        text = " ".join(panel.status.text().split())
        # the line really was written, and really is hidden...
        assert text.startswith("Full image ") and "| Overview " in text, text
        assert not panel.status.isVisible()
        # ...and it is nowhere in a tooltip.
        view_tip, viewport_tip = _tooltips(panel)
        assert view_tip == "", f"the canvas kept a tooltip: {view_tip!r}"
        assert viewport_tip == "", (
            f"the canvas's viewport kept a tooltip: {viewport_tip!r}")
        for tip in (view_tip, viewport_tip):
            assert "Full image" not in tip and "Overview" not in tip
    finally:
        popup.close()


def test_no_state_round_trip_ever_restores_the_tooltip(app):
    """Gate 2: Loading -> success -> failed -> success, tooltip empty at
    every step, and the VISIBLE behaviour unchanged at every step."""
    popup = _shown_popup(app)
    try:
        panel = popup.overview

        # 1. Loading -- visible, no tooltip.
        assert panel.status.isVisible()
        assert _tooltips(panel) == ("", "")

        # 2. the success metadata -- hidden, no tooltip.
        _load_overview(app, panel)
        assert not panel.status.isVisible()
        assert _tooltips(panel) == ("", ""), _tooltips(panel)

        # 3. a failure -- visible again, still no tooltip.
        panel.status.setText("Overview load failed: disk went away")
        for _ in range(6):
            app.processEvents()
        assert panel.status.isVisible()
        assert _tooltips(panel) == ("", ""), _tooltips(panel)

        # 4. success again -- hidden again, and still no tooltip.
        panel.status.setText("Full image 59040×42000 px  |  "
                             "Overview 48×34 px  (0.3s)")
        for _ in range(6):
            app.processEvents()
        assert not panel.status.isVisible()
        assert _tooltips(panel) == ("", ""), _tooltips(panel)
    finally:
        popup.close()


def test_the_popup_module_writes_no_metadata_tooltip_at_all(app):
    """Gate 1 (static): no second entry point was added in its place.

    A status tip, a "what's this" or a second `setToolTip` would satisfy the
    two gates above on the canvas while putting the same text somewhere else
    the pointer can reach.
    """
    import inspect
    from block01.ui.widgets import tissue_navigator_popup as mod

    source = inspect.getsource(mod)
    for banned in ("setStatusTip", "setWhatsThis", "setToolTipDuration"):
        assert banned not in source, f"{banned} appeared in the popup"
    # `setToolTip` may only ever be called with an empty string here.
    for line in source.splitlines():
        stripped = line.strip()
        if "setToolTip(" in stripped and not stripped.startswith("#"):
            assert 'setToolTip("")' in stripped, (
                f"a non-empty tooltip is set in the popup: {stripped!r}")


def test_step0_s_own_panel_is_untouched_by_the_tooltip_policy(app):
    """Gate 4: the policy is this popup's, not the shared class's."""
    from block01.ui.step0.overview_panel import OverviewPanel

    panel = OverviewPanel(_JumpLoader(), "DAPI", lazy=True)
    panel.resize(420, 420)
    panel.show()
    for _ in range(6):
        app.processEvents()
    try:
        before = _tooltips(panel)
        _load_overview(app, panel)
        # its status line is still the visible one it always was...
        text = " ".join(panel.status.text().split())
        assert text.startswith("Full image ") and "| Overview " in text
        assert panel.status.isVisible(), (
            "Step0's own panel lost its status line: the class was changed")
        # ...and the popup's local policy did not reach in and change its
        # tooltips either way.
        assert _tooltips(panel) == before
    finally:
        panel.close()
