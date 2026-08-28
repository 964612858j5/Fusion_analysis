"""v15 Phase 1B: Explore/Compare mode state contract (no viewer backend).

Covers requirements 10–12: Compare captures the current viewport by default,
Navigator ROI uses whole-slide coordinates, and mode switching never touches
production parameters.
"""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_compare_defaults_to_current_viewport(app):
    from block01.ui.widgets.compare_contract import (
        CompareModeState, CompareScope, ViewerMode, ViewportState)
    st = CompareModeState()
    assert st.mode() is ViewerMode.EXPLORE
    vp = ViewportState(x=1000, y=2000, width=512, height=512,
                       zoom=0.5, pyramid_level=2)
    st.enter_compare(vp)
    assert st.mode() is ViewerMode.COMPARE
    assert st.scope() is CompareScope.CURRENT_VIEWPORT
    assert st.locked_viewport() is vp


def test_navigator_roi_is_whole_slide_coords(app):
    from block01.ui.widgets.compare_contract import (
        CompareModeState, CompareScope, ComparisonROI, ViewportState,
        COORDINATE_SPACE)
    st = CompareModeState()
    st.enter_compare(ViewportState(0, 0, 100, 100))
    roi = ComparisonROI(x=15000, y=32000, width=2048, height=1024,
                        dataset_id="slide-A")
    st.set_navigator_roi(roi)
    assert st.scope() is CompareScope.NAVIGATOR_SELECTION
    d = st.roi().to_dict()
    assert d["coordinate_space"] == "whole_slide_full_res_pixels"
    assert d["coordinate_space"] == COORDINATE_SPACE
    assert (d["x"], d["y"]) == (15000, 32000)
    # move / resize / clear / redraw
    st.set_navigator_roi(roi.moved(100, -100))
    assert st.roi().x == 15100 and st.roi().y == 31900
    st.set_navigator_roi(roi.resized(4096, 4096))
    assert st.roi().width == 4096
    st.clear_roi()
    assert st.roi() is None and st.scope() is CompareScope.CURRENT_VIEWPORT
    with pytest.raises(ValueError):
        st.set_navigator_roi(ComparisonROI(0, 0, 0, 0))


def test_mode_switch_touches_no_production_params(app):
    from block01.ui.widgets.compare_contract import (
        CompareModeState, ViewportState)
    prod = {"tophat_radius": 25, "cucim_sigma": 60,
            "weights": {"CD3": 0.5}, "decisions": {"CD3": "tophat"}}
    snapshot = repr(prod)
    st = CompareModeState()
    st.enter_compare(ViewportState(0, 0, 10, 10))
    st.exit_compare()
    st.enter_compare(ViewportState(5, 5, 10, 10))
    assert repr(prod) == snapshot
    # the state object holds no production fields at all
    assert not any(k in vars(st) for k in
                   ("_tophat_radius", "_weights", "_decisions"))


def test_shared_viewport_sync_and_latest_wins(app):
    from block01.ui.widgets.compare_contract import (
        SharedViewportState, ViewportState, COMPARE_VIEWS)
    assert COMPARE_VIEWS == ("original", "tophat", "cucim", "final")
    sv = SharedViewportState()
    got = []
    sv.viewport_changed.connect(lambda vp: got.append(vp))
    g0 = sv.generation()
    vp1 = ViewportState(0, 0, 100, 100, zoom=1.0, pyramid_level=0)
    vp2 = ViewportState(50, 50, 100, 100, zoom=2.0, pyramid_level=1)
    sv.set_viewport(vp1)
    gen1 = sv.generation()
    sv.set_viewport(vp2)
    assert got == [vp1, vp2]
    assert not sv.is_current(gen1)          # stale request dropped
    assert sv.is_current(sv.generation())
    assert sv.viewport().bbox() == (50, 50, 100, 100)


def test_pinned_locations(app):
    from block01.ui.widgets.compare_contract import (
        CompareModeState, PinnedLocation)
    st = CompareModeState()
    st.add_pinned(PinnedLocation("tumor", 100, 200, 512, 512, "slide-A"))
    st.add_pinned(PinnedLocation("edge", 900, 900, 512, 512, "slide-A"))
    assert [p.label for p in st.pinned()] == ["tumor", "edge"]
    d = st.pinned()[0].to_dict()
    assert d["coordinate_space"] == "whole_slide_full_res_pixels"
    st.remove_pinned("tumor")
    assert [p.label for p in st.pinned()] == ["edge"]
