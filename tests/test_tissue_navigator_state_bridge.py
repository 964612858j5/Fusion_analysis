"""v14.2b: single authoritative ROI/context model shared by Step0 + popup overviews.

Step0 owns ONE RoiContextModel. Both the Step0 main OverviewPanel and the
TissueNavigatorPopup OverviewPanel are views/editors over it: an edit on either
panel is adopted into the model, then both panels re-render from the model.

The interleaved-edit test goes only through the route
    overview edit -> single model -> both overviews refresh,
never pushing state directly between the two widgets. It distinguishes a real
single-model implementation from fragile two-store syncing (which would drop an
edit via stale overwrite).

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def _names(rois):
    return sorted(r["name"] for r in rois)


def _add_roi(panel, name):
    """Simulate an ROI edit on a panel the way OverviewPanel does: mutate its
    render cache and emit rois_changed (the panel's authoritative signal)."""
    panel._rois.append({
        "name": name,
        "polygon_display": [(0, 0), (1, 0), (1, 1)],
        "patch_indices": [],
    })
    panel.rois_changed.emit(list(panel._rois))


# ── 1 + 2. Step0 owns one model + one popup instance ─────────────────────────
def test_step0_owns_single_model_and_popup(app):
    from block01.ui.step0.step0_page import Step0Page
    from block01.ui.step0.roi_context_model import RoiContextModel
    s = Step0Page()
    assert isinstance(s._roi_model, RoiContextModel)
    s.toggle_tissue_navigator()
    first = s._tissue_navigator_popup
    s.toggle_tissue_navigator()
    s.show_tissue_navigator()
    assert s._tissue_navigator_popup is first   # same single instance
    assert s._roi_model is s._roi_model


# ── 3. Step0 pushes rois / patches / full_wsi_mode into both views ───────────
def test_step0_pushes_context_into_both_views(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s._roi_model.adopt(
        rois=[{"name": "A", "polygon_display": [(0, 0), (1, 0), (1, 1)]}],
        patches=[(0, 32, 0, 32)], full_wsi_mode=True)
    s.toggle_tissue_navigator()             # popup fed from the model on create
    pop = s._tissue_navigator_popup
    assert _names(pop.overview.get_rois()) == ["A"]
    assert pop.is_full_wsi_mode() is True
    assert pop.roi_count() == 1


# ── 4. Step0-side edit updates model and popup overview ──────────────────────
def test_step0_edit_updates_model_and_popup(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    _add_roi(s.overview, "A")
    assert _names(s._roi_model.rois) == ["A"]
    assert _names(pop.overview.get_rois()) == ["A"]


# ── 5. Popup-side edit updates the same model and the Step0 overview ─────────
def test_popup_edit_updates_model_and_step0(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    _add_roi(pop.overview, "P")
    assert _names(s._roi_model.rois) == ["P"]
    assert _names(s.overview.get_rois()) == ["P"]


# ── 6. Interleaved popup + Step0 edits preserve the union (no stale overwrite) ─
def test_interleaved_edits_preserve_union(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup

    _add_roi(s.overview, "A")            # model: A ; both views: A
    _add_roi(pop.overview, "B")          # popup edit -> model A,B ; both refresh
    _add_roi(s.overview, "C")            # step0 edit builds on A,B -> A,B,C

    assert _names(s._roi_model.rois) == ["A", "B", "C"]
    assert _names(s.overview.get_rois()) == ["A", "B", "C"]
    assert _names(pop.overview.get_rois()) == ["A", "B", "C"]


# ── 7. Repeated refresh updates existing popup + model, not a new model ──────
def test_repeated_refresh_does_not_create_new_model(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    model_id = id(s._roi_model)
    s.toggle_tissue_navigator()
    popup_id = id(s._tissue_navigator_popup)
    _add_roi(s.overview, "A")
    s._feed_popup_from_model()
    s._feed_popup_from_model()
    assert id(s._roi_model) == model_id
    assert id(s._tissue_navigator_popup) == popup_id


# ── 8. Sync creates no files ─────────────────────────────────────────────────
def test_sync_creates_no_files(app, tmp_path):
    import os
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.output_dir = str(tmp_path)
    before = set(os.listdir(tmp_path))
    s.toggle_tissue_navigator()
    _add_roi(s.overview, "A")
    _add_roi(s._tissue_navigator_popup.overview, "B")
    s._feed_popup_from_model()
    assert set(os.listdir(tmp_path)) == before


# ── 9. Existing OverviewPanel mouse behavior not globally broken ─────────────
def test_overview_panel_mouse_behavior_unchanged(app):
    from block01.ui.step0.overview_panel import OverviewPanel
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    fresh = OverviewPanel(None, "", lazy=True)
    assert fresh.vb.state["mouseEnabled"] == [False, False]


# ── 10. Popup still rejects invalid coordinate_space ─────────────────────────
def test_popup_still_rejects_bad_coordinate_space(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    with pytest.raises(ValueError):
        pop.set_viewport_rect({"coordinate_space": "global_wsi",
                               "x0": 0, "x1": 1, "y0": 0, "y1": 1})
    assert pop.current_viewport_rect() is None


# ── 11. Popup still has no channel-overlay / marker-preview widgets ──────────
def test_popup_still_has_no_channel_widgets(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    for forbidden in ("_canvas", "_workbench", "_cond_workbench",
                      "_histogram", "_layer_list"):
        assert not hasattr(pop, forbidden), forbidden


# ── 12. No forbidden dependency-wall imports in the model / bridge ───────────
def test_no_forbidden_imports_in_model_module():
    import inspect
    from block01.ui.step0 import roi_context_model as mod
    src = inspect.getsource(mod)
    for forbidden in ("hq_source_resolver", "remap_promotion",
                      "promote_remap_config", "validate_step2_remap_config",
                      "segment_merge_worker"):
        assert forbidden not in src, forbidden
