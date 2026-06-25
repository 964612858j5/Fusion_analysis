"""v14.2c: live viewport sync — viewer image-local rect -> tissue current-view rect.

Audited mapping (Case A): the Step0 viewer displays the raw current-patch crop
with geometry preserved (intensity-only display pipeline), so the viewer's
image-local pixels are PATCH-LOCAL. Full-image px = patch origin + viewer px.
Patch convention is (y0, y1, x0, x1). Overview display coords = full-image / ds.

These tests encode those audit assumptions and verify the mapping, patch-change
remap, clearing, split-view disable, coordinate-space rejection, and that the
current-view rectangle is NOT an ROI and does not disturb the v14.2b ROI bridge.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def _img_local(x0, x1, y0, y1, shape=(100, 100)):
    return {"coordinate_space": "image_local_pixels",
            "x0": float(x0), "x1": float(x1), "y0": float(y0), "y1": float(y1),
            "image_shape": [shape[0], shape[1]]}


# ── 1. Audit assumptions encoded ─────────────────────────────────────────────
def test_audit_assumptions(app):
    from block01.ui.widgets.high_quality_image_viewer import (
        HighQualityImageViewer, _to_display)
    # viewer rect coordinate_space tag
    v = HighQualityImageViewer()
    v.resize(200, 200)
    raw = np.full((40, 60), 0.5, dtype=np.float32)   # H=40, W=60
    v.set_images(remapped=raw)
    rect = v.get_viewport_rect()
    assert rect["coordinate_space"] == "image_local_pixels"
    assert rect["image_shape"] == [40, 60]           # [H, W]
    # display pipeline preserves geometry (intensity only, no resize)
    disp = _to_display(raw)
    assert disp.shape == raw.shape
    # split-view geometry: _compose_split keeps the same H x W array
    out = v._compose_split(raw, raw * 0.3)
    assert out.shape == raw.shape


# ── 2. Mapping: viewer image-local rect + patch origin -> full-image rect ────
def test_mapping_patch_local_to_full(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # patch (y0,y1,x0,x1): rows 100..200, cols 300..400
    s.patches = [(100, 200, 300, 400)]
    s.current_patch_idx = 0
    full = s._map_viewport_to_full(_img_local(10, 60, 20, 70))
    # full = (y0+vy, y1+vy, x0+vx, x1+vx) → (120,170,310,360)
    assert full == (120.0, 170.0, 310.0, 360.0)


# ── 3. Changing current patch changes the mapped rectangle ───────────────────
def test_patch_change_changes_rect(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.patches = [(0, 100, 0, 100), (500, 600, 700, 800)]
    s.current_patch_idx = 0
    vp = _img_local(10, 20, 10, 20)
    a = s._map_viewport_to_full(vp)
    s.current_patch_idx = 1
    b = s._map_viewport_to_full(vp)
    assert a == (10.0, 20.0, 10.0, 20.0)
    assert b == (510.0, 520.0, 710.0, 720.0)
    assert a != b


# ── 4. Clearing viewer rect clears the popup current-view rectangle ──────────
def test_clear_rect(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    pop.overview.set_current_view_rect((10, 20, 30, 40))
    assert pop.overview.current_view_rect() is not None
    # no patches → update clears
    s.patches = []
    s._update_tissue_view_rect()
    assert pop.overview.current_view_rect() is None
    assert pop.current_viewport_rect() is None


# ── 5. Invalid coordinate_space rejected; prior valid rect not overwritten ───
def test_invalid_space_rejected_keeps_prev(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    pop.set_viewport_rect(_img_local(1, 2, 3, 4))
    prev = pop.current_viewport_rect()
    # mapping rejects a wrong space (returns None, no store)
    assert s._map_viewport_to_full(
        {"coordinate_space": "global_wsi", "x0": 0, "x1": 1, "y0": 0, "y1": 1}) is None
    # popup API itself still raises and keeps prev
    with pytest.raises(ValueError):
        pop.set_viewport_rect({"coordinate_space": "global_wsi",
                               "x0": 0, "x1": 1, "y0": 0, "y1": 1})
    assert pop.current_viewport_rect() == prev


# ── 6. Split view disables/clears the current-view rectangle ─────────────────
def test_split_view_clears_rect(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.patches = [(0, 50, 0, 50)]
    s.current_patch_idx = 0
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    pop.overview.set_current_view_rect((0, 50, 0, 50))
    # enable split on the conditioning viewer. The "Split raw|remapped" UI toggle
    # was removed (step0-conditioning-declutter #5); split is now driven only on
    # the canvas, but is_split_view() + the v14.2c clear-on-split sync still work.
    s._cond_workbench._canvas.set_split(True)
    assert s._cond_workbench.is_split_view() is True
    s._update_tissue_view_rect()
    assert pop.overview.current_view_rect() is None
    assert pop.current_viewport_rect() is None


# ── 7. Current-view rectangle is NOT an ROI ──────────────────────────────────
def test_current_view_rect_is_not_an_roi(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    before = len(s._roi_model.rois)
    pop.overview.set_current_view_rect((1, 2, 3, 4))
    assert len(s._roi_model.rois) == before          # model untouched
    assert pop.overview.get_rois() == []             # not added to panel rois
    assert pop.roi_count() == 0


# ── 8. v14.2b ROI bridge still works (interleaved edits union) ───────────────
def test_roi_bridge_interleaved_still_works(app):
    from block01.ui.step0.step0_page import Step0Page

    def add(panel, name):
        panel._rois.append({"name": name,
                            "polygon_display": [(0, 0), (1, 0), (1, 1)],
                            "patch_indices": []})
        panel.rois_changed.emit(list(panel._rois))

    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    add(s.overview, "A")
    add(pop.overview, "B")
    add(s.overview, "C")
    got = sorted(r["name"] for r in s._roi_model.rois)
    assert got == ["A", "B", "C"]
    assert sorted(r["name"] for r in pop.overview.get_rois()) == ["A", "B", "C"]


# ── 9. Popup still has no channel-overlay / marker-preview widgets ───────────
def test_popup_no_channel_widgets(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    for forbidden in ("_canvas", "_workbench", "_cond_workbench",
                      "_histogram", "_layer_list"):
        assert not hasattr(pop, forbidden), forbidden


# ── 10. No forbidden dependency-wall imports in the changed viewer/overview ──
def test_no_forbidden_imports():
    import inspect
    from block01.ui.widgets import high_quality_image_viewer as v
    from block01.ui.widgets import tissue_navigator_popup as p
    blobs = [inspect.getsource(v), inspect.getsource(p)]
    for src in blobs:
        for forbidden in ("hq_source_resolver", "remap_promotion",
                          "promote_remap_config", "validate_step2_remap_config",
                          "segment_merge_worker"):
            assert forbidden not in src, forbidden
