"""v14.4: Step0 Background Correction is a formal tab, separate from Channel
Conditioning / Remap, and never writes corrected output on navigation/show.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import os

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture(scope="module")
def app():
    from PyQt5 import QtWidgets
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return a


def _no_corrected_zarr_anywhere(root):
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            if d == "corrected_channels.zarr":
                return False
    return True


# ── 1 + 2. Two separate tabs ─────────────────────────────────────────────────
def test_step0_has_background_correction_tab(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    tabs = [s._step0_tabs.tabText(i) for i in range(s._step0_tabs.count())]
    assert "Background Correction" in tabs
    assert any("Channel Conditioning" in t for t in tabs)
    # they are distinct tab indices
    assert tabs.index("Background Correction") != next(
        i for i, t in enumerate(tabs) if "Channel Conditioning" in t)


# ── 3. BG tab has tophat/cuCIM controls, not remap controls ──────────────────
def test_bg_tab_has_no_remap_controls_as_primary(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # Background-correction controls exist
    assert hasattr(s, "_tophat_slider")
    assert hasattr(s, "_cucim_slider")
    assert hasattr(s, "_btn_start_bg")
    # Channel-conditioning (remap) controls are NOT Step0Page attrs — they live
    # inside the shared ChannelWorkbench (gamma/brightness/contrast/save remap).
    for forbidden in ("_gamma_slider", "_brightness_slider", "_contrast_slider"):
        assert not hasattr(s, forbidden), forbidden


# ── 4. Channel Conditioning tab still hosts the shared ChannelWorkbench ──────
def test_conditioning_tab_hosts_channel_workbench(app):
    from block01.ui.widgets.channel_workbench import ChannelWorkbench
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert isinstance(s._cond_workbench, ChannelWorkbench)


# ── 5. Opening Step0 / switching tabs creates no corrected_channels.zarr ─────
def test_navigation_creates_no_corrected_zarr(app, tmp_path):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.output_dir = str(tmp_path)
    # switch through tabs
    for i in range(s._step0_tabs.count()):
        s._step0_tabs.setCurrentIndex(i)
    assert _no_corrected_zarr_anywhere(str(tmp_path))


# ── 6. Showing TissueNavigatorPopup creates no corrected_channels.zarr ───────
def test_show_popup_creates_no_corrected_zarr(app, tmp_path):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.output_dir = str(tmp_path)
    s.toggle_tissue_navigator()
    s.toggle_tissue_navigator()
    assert _no_corrected_zarr_anywhere(str(tmp_path))


# ── 11. corrected-output status starts honest (not "written") ────────────────
def test_corrected_status_starts_not_written(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert hasattr(s, "_bg_corrected_status")
    assert "not written" in s._bg_corrected_status.text().lower()


# ── status helper flags empty vs valid ───────────────────────────────────────
def test_corrected_status_flags_empty_vs_valid(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s._refresh_bg_corrected_status(
        {"exists": True, "non_empty": False, "n_channel_arrays": 0})
    assert "empty" in s._bg_corrected_status.text().lower()
    s._refresh_bg_corrected_status(
        {"exists": True, "non_empty": True, "n_channel_arrays": 2})
    txt = s._bg_corrected_status.text().lower()
    assert "written" in txt and "2 channel" in txt


# ── step0-move-roi-patch-to-navigator (#10): Section B relocated ─────────────
def _under(container, target):
    if container is target:
        return True
    return target in container.findChildren(type(target))


def test_roi_patch_section_not_in_background_correction(app):
    from block01.ui.step0.step0_page import Step0Page
    from block01.ui.step0.overview_panel import OverviewPanel
    s = Step0Page()
    ms = s._main_split
    # the BG splitter now holds only Section C (correction + Preview Patch).
    kids = [ms.widget(i) for i in range(ms.count())]
    # Section B's overview is NOT rendered in the BG area...
    assert not any(_under(k, s.overview) for k in kids)
    # ...it lives in the (hidden) relocated section, kept as a model view.
    assert s._roi_patch_section not in kids
    assert _under(s._roi_patch_section, s.overview)


def test_tissue_navigator_hosts_roi_patch(app):
    from block01.ui.step0.step0_page import Step0Page
    from block01.ui.step0.overview_panel import OverviewPanel
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    assert isinstance(pop.overview, OverviewPanel)        # navigator hosts ROI/patch
    # both overviews are views over the single RoiContextModel (v14.2b)
    assert set(s._registered_roi_overviews()) == {s.overview, pop.overview}


def test_preview_patch_still_in_background_correction(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # "Preview Patch" box (per-patch BG-correction view) is UNCHANGED + still in BG.
    boxes = [b for b in s.findChildren(QtWidgets.QGroupBox)
             if (b.title() or "") == "Preview Patch"]
    assert boxes, "Preview Patch box missing"
    # it is under the BG splitter (Section C), not the relocated Section B
    ms = s._main_split
    kids = [ms.widget(i) for i in range(ms.count())]
    assert any(_under(k, boxes[0]) for k in kids)
    # its per-patch switching widget is still present
    assert hasattr(s, "_patch_info")


def test_roi_edit_in_navigator_reaches_step0_view_via_model(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # draw a ROI on the navigator overview -> single model -> step0 overview mirrors
    pop.overview._rois.append(
        {"name": "RN", "polygon_display": [(0, 0), (1, 0), (1, 1)], "patch_indices": []})
    pop.overview.rois_changed.emit(list(pop.overview._rois))
    assert [r["name"] for r in s._roi_model.rois] == ["RN"]
    assert [r["name"] for r in s.overview.get_rois()] == ["RN"]


# ── step0-autoopen-navigator: navigator auto-opens once on data load ─────────
class _FakeLoader:
    def __init__(self):
        self.shape = (1000, 1200)
        self.ch_map = {"DAPI": 0, "CD68": 1}
        self.filepath = "/x.ome.tif"

    def channel_names(self):
        return ["DAPI", "CD68"]


def test_navigator_auto_opens_on_load(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert s._tissue_navigator_popup is None          # not before load
    s.loader = _FakeLoader()
    s._navigator_auto_opened = False                  # re-armed at load start
    s._auto_open_tissue_navigator()                   # load-completion call
    assert s._tissue_navigator_popup is not None
    assert s._tissue_navigator_popup.isVisible()      # opened via the existing path
    assert s._navigator_auto_opened is True


def test_auto_open_fires_once_refresh_does_not_repop(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.loader = _FakeLoader()
    s._navigator_auto_opened = False
    s._auto_open_tissue_navigator()
    s._tissue_navigator_popup.hide()                  # user closes it
    # a refresh (does NOT re-arm) must not re-pop the navigator
    s._auto_open_tissue_navigator()
    assert s._tissue_navigator_popup.isVisible() is False
    # a NEW load re-arms -> opens again
    s._navigator_auto_opened = False
    s._auto_open_tissue_navigator()
    assert s._tissue_navigator_popup.isVisible() is True


def test_auto_open_no_loader_no_popup(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.loader = None
    s._navigator_auto_opened = False
    s._auto_open_tissue_navigator()
    assert s._tissue_navigator_popup is None           # no empty popup
