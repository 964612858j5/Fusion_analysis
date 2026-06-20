"""v14.3: HighQualityImageViewer extraction + viewport API + zoom/pan preservation.

These tests pin the EXTRACTED core viewer and its stable viewport API, plus the
audited fixed behavior: display-parameter changes must not reset zoom/pan. They
read the ACTUAL ViewBox.viewRange() (not just the _need_fit flag) before/after a
parameter change.

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


def _mk_viewer(app):
    from block01.ui.widgets.high_quality_image_viewer import HighQualityImageViewer
    v = HighQualityImageViewer()
    v.resize(320, 320)        # give the ViewBox a definite geometry
    return v


def _img(h=64, w=64, val=0.5):
    return np.full((h, w), float(val), dtype=np.float32)


def _viewrange(v):
    (x0, x1), (y0, y1) = v._vb.viewRange()
    return [float(x0), float(x1), float(y0), float(y1)]


# ── 1. Construct offscreen ───────────────────────────────────────────────────
def test_hqiv_constructs_offscreen(app):
    import pyqtgraph as pg
    v = _mk_viewer(app)
    assert v is not None
    assert isinstance(v._img_item, pg.ImageItem)
    assert isinstance(v._vb, pg.ViewBox)


# ── 2. ChannelViewerCanvas is a HighQualityImageViewer (back-compat) ─────────
def test_channel_viewer_canvas_is_hqiv(app):
    from block01.ui.widgets.high_quality_image_viewer import HighQualityImageViewer
    from block01.ui.widgets.channel_viewer_canvas import (
        ChannelViewerCanvas, display_normalize)
    c = ChannelViewerCanvas()
    assert isinstance(c, HighQualityImageViewer)
    # re-exported helper still importable from the legacy module path
    out = display_normalize(np.arange(16, dtype=np.uint16).reshape(4, 4))
    assert out.dtype == np.float32 and 0.0 <= out.min() and out.max() <= 1.0


# ── Rendering backend confirmation ──────────────────────────────────────────
def test_backend_is_pyqtgraph_imageitem(app):
    import pyqtgraph as pg
    v = _mk_viewer(app)
    assert isinstance(v._glw, pg.GraphicsLayoutWidget)
    assert isinstance(v._vb, pg.ViewBox)
    assert isinstance(v._img_item, pg.ImageItem)


# ── 5. request_fit() triggers fit behavior ───────────────────────────────────
def test_request_fit_triggers_fit(app):
    v = _mk_viewer(app)
    v.set_images(remapped=_img())          # first load fits, clears _need_fit
    assert v._need_fit is False
    # zoom into a sub-region, then ask for a fit
    v.set_view_region({"coordinate_space": "image_local_pixels",
                       "x0": 10, "x1": 20, "y0": 10, "y1": 20})
    zoomed = _viewrange(v)
    v.request_fit()
    assert v._need_fit is True
    v.set_images(remapped=_img())          # same shape, but fit was requested
    assert v._need_fit is False
    fitted = _viewrange(v)
    assert fitted != zoomed                 # fit changed the view back
    # fitted view spans (at least) the full image extent
    assert fitted[0] <= 0.0 and fitted[1] >= 64.0
    assert fitted[2] <= 0.0 and fitted[3] >= 64.0


# ── 6. Shape change triggers refit ───────────────────────────────────────────
def test_shape_change_triggers_refit(app):
    v = _mk_viewer(app)
    v.set_images(remapped=_img(64, 64))
    assert v._prev_shape == (64, 64)
    v.set_view_region({"coordinate_space": "image_local_pixels",
                       "x0": 5, "x1": 15, "y0": 5, "y1": 15})
    v.set_images(remapped=_img(128, 128))   # shape change -> refit
    assert v._prev_shape == (128, 128)
    vr = _viewrange(v)
    assert vr[0] <= 0.0 and vr[1] >= 128.0   # refitted to new bounds


# ── 7. Parameter change does NOT reset actual ViewBox.viewRange() ────────────
def test_param_change_preserves_actual_viewrange(app):
    v = _mk_viewer(app)
    v.set_images(remapped=_img(64, 64, 0.5))
    # establish a non-trivial zoom/pan
    v.set_view_region({"coordinate_space": "image_local_pixels",
                       "x0": 8, "x1": 40, "y0": 12, "y1": 44})
    before = _viewrange(v)
    # simulate Min/Max/gamma change: SAME shape, different pixel values
    v.set_images(remapped=_img(64, 64, 0.9))
    after_param = _viewrange(v)
    assert np.allclose(before, after_param, atol=1e-6), (before, after_param)
    # opacity / overlay toggles must also preserve the view
    v.set_layer_opacity(dapi=0.3)
    v.set_layer_visibility(dapi=True)
    after_toggle = _viewrange(v)
    assert np.allclose(before, after_toggle, atol=1e-6), (before, after_toggle)


# ── 8. get_viewport_rect() returns image-local pixel coordinates ─────────────
def test_get_viewport_rect_image_local_pixels(app):
    v = _mk_viewer(app)
    assert v.get_viewport_rect() is None     # nothing displayed yet
    v.set_images(remapped=_img(48, 72))      # H=48, W=72
    rect = v.get_viewport_rect()
    assert rect["coordinate_space"] == "image_local_pixels"
    assert rect["image_shape"] == [48, 72]
    for k in ("x0", "x1", "y0", "y1"):
        assert isinstance(rect[k], float)


# ── 9. set_view_region(rect) restores the same region (aspect-aware) ─────────
def test_set_view_region_round_trip(app):
    v = _mk_viewer(app)
    v.set_images(remapped=_img(100, 100))
    req = {"coordinate_space": "image_local_pixels",
           "x0": 20.0, "x1": 60.0, "y0": 30.0, "y1": 70.0}
    v.set_view_region(req)
    got = v.get_viewport_rect()
    # Aspect-lock may expand the smaller axis, so the restored region must
    # CONTAIN the request and share its center (centers preserved exactly).
    cx_req, cy_req = (req["x0"] + req["x1"]) / 2, (req["y0"] + req["y1"]) / 2
    cx_got, cy_got = (got["x0"] + got["x1"]) / 2, (got["y0"] + got["y1"]) / 2
    assert abs(cx_got - cx_req) < 1e-3
    assert abs(cy_got - cy_req) < 1e-3
    assert got["x0"] <= req["x0"] + 1e-6 and got["x1"] >= req["x1"] - 1e-6
    assert got["y0"] <= req["y0"] + 1e-6 and got["y1"] >= req["y1"] - 1e-6


def test_set_view_region_rejects_wrong_coordinate_space(app):
    v = _mk_viewer(app)
    v.set_images(remapped=_img())
    with pytest.raises(ValueError):
        v.set_view_region({"coordinate_space": "global_wsi",
                           "x0": 0, "x1": 1, "y0": 0, "y1": 1})


# ── 10. No TissueNavigatorPopup created or imported in v14.3 ──────────────────
def test_no_tissue_navigator_in_viewer_module():
    # The popup is a later phase (v14.2/v14.x): the viewer must not import or
    # instantiate it. Prose mentions of the future use in docstrings are fine;
    # we check for an actual import or construction.
    import inspect
    from block01.ui.widgets import high_quality_image_viewer as mod
    src = inspect.getsource(mod)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "tissue_navigator" not in stripped.lower(), stripped
            assert "TissueNavigator" not in stripped, stripped
    assert "TissueNavigatorPopup(" not in src   # no construction
    # (the popup module may exist as a sibling from v14.2a, but the viewer must
    # not depend on it — covered by the import/construction checks above.)


# ── 11. No promotion / resolver / Step2 runtime imports in the viewer ────────
def test_no_forbidden_imports_in_viewer_module():
    import inspect
    from block01.ui.widgets import high_quality_image_viewer as mod
    src = inspect.getsource(mod)
    for forbidden in ("hq_source_resolver", "remap_promotion",
                      "promote_remap_config", "validate_step2_remap_config",
                      "segment_merge_worker"):
        assert forbidden not in src, forbidden
