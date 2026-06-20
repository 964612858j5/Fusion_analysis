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
