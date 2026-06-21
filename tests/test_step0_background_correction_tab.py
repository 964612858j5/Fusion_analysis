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
