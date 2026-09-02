"""v14.4: Step0 Background Correction is a formal tab, separate from Channel
Conditioning / Remap, and never writes corrected output on navigation/show.

Qt tests need an offscreen platform (env: QT_QPA_PLATFORM=offscreen).
"""

import os

import pytest
from PyQt5 import QtCore

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
    assert any("Channel Remap" in t for t in tabs)
    # they are distinct tab indices
    assert tabs.index("Background Correction") != next(
        i for i, t in enumerate(tabs) if "Channel Remap" in t)


# ── 3. BG tab has tophat/cuCIM controls, not remap controls ──────────────────
def test_bg_tab_has_no_remap_controls_as_primary(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # Background-correction controls exist
    assert hasattr(s, "_tophat_slider")
    assert hasattr(s, "_cucim_slider")
    # (#5) the standalone "Run BG correction" button was merged into the BG-tab
    # Save (_btn_continue); the separate _btn_start_bg no longer exists.
    assert hasattr(s, "_btn_continue")
    assert not hasattr(s, "_btn_start_bg")
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
    # 'no channels assigned' is a valid choice, not an error (neutral message).
    assert "no channels assigned" in s._bg_corrected_status.text().lower()
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


# ── step0-fix-title-occlusion (#3): _box_style reserves room for the title ────
def test_box_style_title_not_occluded_by_body(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page

    gb = QtWidgets.QGroupBox("Quantitative Metrics")
    gb.setStyleSheet(Step0Page._box_style("#56b6c2"))
    v = QtWidgets.QVBoxLayout(gb)
    v.setContentsMargins(0, 0, 0, 0)        # worst case: body hugs the frame top
    child = QtWidgets.QLabel("Original  -> SNR: --")
    v.addWidget(child)
    gb.resize(220, 120)
    gb.ensurePolished()
    gb.show()
    app.processEvents()

    # the body content area (and the first child) must start BELOW the title band
    # (title font ~11px + padding ≈ 14-17px), i.e. not riding up over the title.
    assert gb.contentsRect().top() >= 14, gb.contentsRect().top()
    assert child.geometry().top() >= 14, child.geometry().top()
    # the style carries the ::title sub-control rule (the structural fix)
    style = Step0Page._box_style("#56b6c2")
    assert "QGroupBox::title" in style
    assert "subcontrol-origin:margin" in style
    assert "margin-top:16px" in style


# ── step0-layout-and-save-restructure (#4 layout + #5 one-tab-one-Save) ───────
def _gb(s):
    from PyQt5 import QtWidgets
    return {b.title(): b for b in s.findChildren(QtWidgets.QGroupBox)}


def test_one_bg_tab_save_button_no_run_no_page_save(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # standalone "Run BG correction" gone; one merged Save remains
    assert not hasattr(s, "_btn_start_bg")
    assert not hasattr(s, "_on_start_bg_correction")
    assert hasattr(s, "_btn_continue")
    assert s._btn_continue.text() == "Save"
    # the BG Save lives inside the BG tab (under main_split), not a page footer
    w, in_tab = s._btn_continue, False
    p = w.parent()
    while p is not None:
        if p is s._main_split:
            in_tab = True
            break
        p = p.parent()
    assert in_tab
    # it is wired to the full save/handoff pipeline
    assert s._btn_continue.receivers(s._btn_continue.clicked) >= 1


def test_save_button_runs_full_pipeline_handler(app):
    # the merged Save handler IS _save_and_continue (which runs WsiCorrectionWorker
    # + writes configs/step0_roi_result + emits step0_complete). Proven by the
    # outputs + no-autojump suites; here assert the wiring + signal presence.
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    assert hasattr(s, "_save_and_continue")
    assert hasattr(s, "step0_complete")     # the Step0->Step1 handoff signal


def test_preview_patch_relocated_to_c_right(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    g = _gb(s)
    pp, met, dec = g["Preview Patch"], g["Quantitative Metrics"], g["Per-Channel Decision"]
    # The former standalone "Process" box is now folded into "Method Parameters"
    # (run button + Stop + progress + status live there).
    ch, mp = g["Channels"], g["Method Parameters"]
    assert "Process" not in g                      # no separate Process box anymore
    assert _under(mp, s._btn_process)              # run controls under Method Parameters
    # Preview Patch now shares the bottom_row container with Metrics + Decision...
    assert pp.parentWidget() is met.parentWidget() is dec.parentWidget()
    # ...and is NO LONGER in c_left with Channels/Method Parameters
    assert ch.parentWidget() is mp.parentWidget()
    assert pp.parentWidget() is not ch.parentWidget()
    # P-button row + info still wired (relocation kept the widgets)
    assert hasattr(s, "_patch_buttons_row") and hasattr(s, "_patch_info")


def test_tab2_save_renamed(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    labels = [b.text() for b in s._cond_tab.findChildren(QtWidgets.QPushButton)]
    assert "Save" in labels                 # formalized Tab2 Save
    assert "Save remap config (Step0)" not in labels


# ── step0-restore-region-selector: analysis-region selector -> popup ─────────
def test_region_selector_in_navigator_popup(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # the combo (+ button + msg + its container) live inside the popup now
    assert _under(pop, s._analysis_region_combo)
    assert _under(pop, s._btn_use_full_wsi)
    assert _under(pop, s._region_selector)
    assert s._region_selector.isVisible()


def test_region_selector_not_in_background_correction(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # before the navigator exists, the selector is not shown in the BG tab and
    # sec_b (which used to host it) stays hidden as a model-only view.
    assert not s._roi_patch_section.isVisible()
    assert not _under(s._roi_patch_section, s._analysis_region_combo)
    # and it is NOT under the BG splitter (Section C)
    ms = s._main_split
    kids = [ms.widget(i) for i in range(ms.count())]
    assert not any(_under(k, s._analysis_region_combo) for k in kids)


def test_region_selector_handler_still_fires(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    # changing the combo still drives the region-mode handler (signal preserved
    # across reparenting into the popup)
    s._analysis_region_combo.setCurrentIndex(1)     # Full WSI
    assert s._analysis_region_mode == "full_wsi"
    assert s._is_full_wsi_mode() is True
    s._analysis_region_combo.setCurrentIndex(0)     # ROI
    assert s._analysis_region_mode == "roi"
    assert s._is_full_wsi_mode() is False


def test_use_full_wsi_button_still_works_from_popup(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    s._analysis_region_combo.setCurrentIndex(0)
    s._btn_use_full_wsi.click()                     # button -> combo index 1
    assert s._analysis_region_combo.currentIndex() == 1
    assert s._is_full_wsi_mode() is True


def test_navigator_still_hosts_overview_after_relocation(app):
    from block01.ui.step0.step0_page import Step0Page
    from block01.ui.step0.overview_panel import OverviewPanel
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # the ROI/patch overview is undisturbed by adding the region selector
    assert isinstance(pop.overview, OverviewPanel)
    assert _under(pop, pop.overview)


# ── step0-restore-roi-patch-toolbar: drawing toolbar -> navigator popup ──────
def test_roi_patch_toolbar_in_navigator_popup(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # toolbar container + its mode/rename buttons + ROI/patch lists live in popup
    assert _under(pop, s._roi_patch_toolbar)
    assert _under(pop, s._btn_mode_roi)
    assert _under(pop, s._btn_mode_patch)
    assert _under(pop, s._btn_rename_roi)
    assert _under(pop, s._roi_list)
    assert _under(pop, s._patch_list)
    assert s._roi_patch_toolbar.isVisible()


def test_roi_patch_toolbar_not_in_background_correction(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # toolbar is not shown in the BG tab; sec_b (model views) stays hidden
    assert not s._roi_patch_section.isVisible()
    assert not _under(s._roi_patch_section, s._btn_mode_roi)
    ms = s._main_split
    kids = [ms.widget(i) for i in range(ms.count())]
    assert not any(_under(k, s._btn_mode_roi) for k in kids)


def test_mode_switch_drives_popup_overview(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # the toolbar drives the VISIBLE popup overview's draw mode
    assert s._drawing_overview() is pop.overview
    s._set_draw_mode("roi")
    assert pop.overview._mode == "roi"
    assert s._btn_mode_roi.isChecked() and not s._btn_mode_patch.isChecked()
    s._set_draw_mode("patch")
    assert pop.overview._mode == "patch"
    assert s._btn_mode_patch.isChecked() and not s._btn_mode_roi.isChecked()


def test_region_selector_and_toolbar_both_hosted(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # the earlier region-selector relocation (eab9e39) still holds alongside this
    assert _under(pop, s._analysis_region_combo)
    assert _under(pop, s._roi_patch_toolbar)
    # the popup still hosts the overview (drawing surface) undisturbed
    from block01.ui.step0.overview_panel import OverviewPanel
    assert isinstance(pop.overview, OverviewPanel)
    assert _under(pop, pop.overview)


# ── step0-navigator-layout-refinements: overview on top, lists below ─────────
def test_navigator_overview_above_lists_with_proportions(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    outer = pop._outer
    i_region = outer.indexOf(s._region_selector)
    i_modebar = outer.indexOf(s._roi_patch_toolbar)
    i_over = outer.indexOf(pop._overview)
    i_lists = outer.indexOf(s._roi_patch_lists)
    # controls above the overview, lists below it
    assert i_region < i_over and i_modebar < i_over
    assert i_lists > i_over
    # overview is the dominant element (3) vs lists (2): 3/5 vs 2/5
    assert outer.stretch(i_over) == 3
    assert outer.stretch(i_lists) == 2
    # inside the lists container: ROI list 1/5, Patch list 1/5, ROI above Patch
    ll = s._roi_patch_lists.layout()
    def _li(w):
        return next(k for k in range(ll.count())
                    if ll.itemAt(k).widget() is w)
    assert ll.stretch(_li(s._roi_list)) == 1
    assert ll.stretch(_li(s._patch_list)) == 1
    assert _li(s._roi_list) < _li(s._patch_list)


def test_navigator_no_full_wsi_message(app):
    from PyQt5 import QtWidgets
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # the redundant "Full WSI mode" banner label is gone entirely
    assert not hasattr(s, "_analysis_region_msg")
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # switching to Full WSI does not surface any such banner (and does not crash)
    s._analysis_region_combo.setCurrentIndex(1)
    labels = [l.text() for l in pop.findChildren(QtWidgets.QLabel)]
    assert not any("entire image will be processed" in t for t in labels)
    assert s._analysis_region_mode == "full_wsi"


def test_navigator_lists_uncapped_for_proportional_growth(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    # the old 80px max-height caps were removed so stretch governs the 1/5 share
    assert s._roi_list.maximumHeight() > 1000
    assert s._patch_list.maximumHeight() > 1000


def test_navigator_drawing_and_controls_intact_after_reorder(app):
    from block01.ui.step0.step0_page import Step0Page
    s = Step0Page()
    s.toggle_tissue_navigator()
    pop = s._tissue_navigator_popup
    # mode switch still drives the popup overview; lists still hosted + functional
    s._set_draw_mode("roi")
    assert pop.overview._mode == "roi"
    assert _under(pop, s._roi_list) and _under(pop, s._patch_list)
    assert _under(pop, s._btn_mode_roi) and _under(pop, s._btn_mode_patch)


# ── production correction vs the Explore stack ───────────────────────────────

def test_busy_probe_checks_running_not_presence(app, monkeypatch):
    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page.__new__(Step0Page)          # attributes only, no Qt init

    class _W:
        def __init__(self, running):
            self._running = running

        def isRunning(self):
            return self._running

    page._batch_worker = None
    page._ondemand_workers = []
    page._wsi_worker = None
    assert Step0Page.production_correction_busy(page) is None

    page._batch_worker = _W(True)
    assert Step0Page.production_correction_busy(page) == (
        "patch background correction")

    page._batch_worker = _W(False)
    page._ondemand_workers = [_W(False), _W(False)]
    assert Step0Page.production_correction_busy(page) is None, (
        "finished on-demand workers are never removed from the list, so "
        "their presence must not read as busy"
    )
    page._ondemand_workers = [_W(False), _W(True), _W(False)]
    assert Step0Page.production_correction_busy(page) == (
        "on-demand background correction")

    page._ondemand_workers = []
    page._wsi_worker = _W(True)
    assert Step0Page.production_correction_busy(page) == (
        "whole-slide correction (Save)")


def test_a_running_preview_or_preload_worker_does_not_read_as_busy(app):
    """The contract, not the source text.

    `PreloadWorker` only reads -- it corrects nothing, so it must not block
    the full image. `BackgroundPreviewWorker` is unreachable today (its only
    trigger, `_queue_preview`, has no caller anywhere in the repo), so
    guarding it would be code for a path that cannot run; whoever
    reconnects `_queue_preview` must add it to the probe in that change.
    Both are installed here as RUNNING and the probe must still say free.
    """
    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page.__new__(Step0Page)          # attributes only, no Qt init

    class _Running:
        def isRunning(self):
            return True

    page._batch_worker = None
    page._ondemand_workers = []
    page._wsi_worker = None
    page._preview_worker = _Running()
    page._preload_worker = _Running()

    assert Step0Page.production_correction_busy(page) is None


# ── the four GPU paths actually release before starting ──────────────────────
#
# Behaviour, not source order: each path is DRIVEN, with the worker classes
# replaced by recorders and the release patched to record too, both onto one
# shared timeline. A source scan could not see an early return, an
# exception, or a release aimed at the wrong object.

@pytest.fixture(scope="module")
def gpu_path_page(app, tmp_path_factory):
    """ONE page for every GPU-path case in this section.

    Deliberately module-scoped: this file already builds 32 `Step0Page`
    instances, and adding one per parametrised case tipped a whole-file run
    into the known pyqtgraph/offscreen segfault. Each test re-patches the
    worker classes and gets a fresh timeline, so sharing the page costs
    nothing in isolation.
    """
    return _build_gpu_path_page(app, tmp_path_factory.mktemp("gpu_paths"))


def _install_gpu_path_recorders(monkeypatch, timeline):
    """Replace the worker classes and the release with recorders."""
    from block01.ui.step0 import step0_page as mod
    from block01.ui.step0.step0_page import Step0Page

    class _RecordingWorker(QtCore.QThread):
        """A REAL QThread, never started.

        `_watch_production_worker` binds `QThread.finished` off the base
        class explicitly (the whole-slide worker shadows that name with a
        business signal), so a plain stand-in cannot be connected to any
        more -- and every production worker really is a QThread.
        """

        def __init__(self, *a, **k):
            super().__init__()

        def __getattr__(self, name):
            # progress/error/all_done/... are all connected before start();
            # hand back something connectable for each. `finished` is NOT
            # routed here: QThread defines it, so it never reaches
            # __getattr__.
            return _Signal()

        def isRunning(self):
            return False

        def start(self):
            timeline.append(("worker.start", id(self)))

        def stop(self):
            pass

        def stop_after_current_channel(self):
            pass

    class _Signal:
        def connect(self, *_a, **_k):
            pass

    class _Dialog:
        cancel_requested = _Signal()

        def __init__(self, *_a, **_k):
            pass

        def exec_(self):
            timeline.append("dialog.exec_")

        def set_progress(self, *_a, **_k):
            pass

        def allow_close(self):
            pass

        def accept(self):
            pass

    # Modal dialogs would block forever with nobody to click them, and
    # several of these paths pop one on a validation miss. Silence them and
    # keep a record, so a path that bailed out early is visible rather than
    # looking like a pass.
    class _Msg:
        @staticmethod
        def information(*a, **k):
            timeline.append(f"dialog:information:{a[2] if len(a) > 2 else ''}")

        @staticmethod
        def warning(*a, **k):
            timeline.append(f"dialog:warning:{a[2] if len(a) > 2 else ''}")

        @staticmethod
        def critical(*a, **k):
            timeline.append(f"dialog:critical:{a[2] if len(a) > 2 else ''}")

        @staticmethod
        def question(*a, **k):
            timeline.append("dialog:question")
            return getattr(mod.QMessageBox, "Yes", 16384)

    monkeypatch.setattr(mod, "QMessageBox", _Msg)
    monkeypatch.setattr(mod, "BatchProcessWorker", _RecordingWorker)
    monkeypatch.setattr(mod, "WsiCorrectionWorker", _RecordingWorker)
    monkeypatch.setattr(mod, "_WsiCorrectionProgressDialog", _Dialog)
    monkeypatch.setattr(
        Step0Page, "_release_explore_for_production",
        lambda self, reason: timeline.append(f"release:{reason}"))
    # The watcher records the OBJECT it was handed, so a path that watched
    # a different worker than the one it started -- or watched after
    # starting -- is visible rather than merely absent.
    real_watch = Step0Page._watch_production_worker
    monkeypatch.setattr(
        Step0Page, "_watch_production_worker",
        lambda self, worker: (timeline.append(("watch", id(worker))),
                              real_watch(self, worker))[0])
    return mod


def _build_gpu_path_page(app, tmp_path):
    """A page prepared just enough for the four GPU paths to reach start()."""
    import numpy as np

    from block01.ui.step0.step0_page import Step0Page

    page = Step0Page()
    page.loader = _GpuPathLoader()
    page.ome_path = str(tmp_path / "fake.ome.tif")
    page.output_dir = str(tmp_path)
    page.patches = [(0, 32, 0, 32)]
    page.current_patch_idx = 0
    page.nucleus_channel = "DAPI"
    page._rebuild_channel_list()
    page.current_channel = "CD3"
    page._process_completed = True
    # `_save_and_continue` needs a region to work on; full-WSI mode supplies
    # one from the loader's shape without a drawn ROI.
    page._analysis_region_mode = "full_wsi"
    page._preload_cache = {0: {ch: np.zeros((32, 32), np.float32)
                               for ch in ("DAPI", "CD3", "CD20")}}
    row = page._channel_rows.get("CD3")
    if row is not None:
        row["checkbox"].setChecked(True)
        row["method_cb"].setCurrentText("TopHat")
    return page


class _GpuPathLoader:
    _CHANNELS = ["DAPI", "CD3", "CD20"]

    def __init__(self):
        self._corrected_zarr_path = None
        self._corrected_decisions = {}

    def channel_names(self):
        return list(self._CHANNELS)

    @property
    def ch_map(self):
        return {c: i for i, c in enumerate(self._CHANNELS)}

    def set_corrected_zarr_store(self, path, decisions):
        self._corrected_zarr_path = path
        self._corrected_decisions = dict(decisions or {})

    def set_correction_config(self, _cfg):
        pass

    shape = (256, 256)

    def read_region(self, ch, y0, y1, x0, x1, **_kw):
        import numpy as np
        return np.zeros((y1 - y0, x1 - x0), np.float32)


@pytest.mark.parametrize(
    ("path_name", "driver"),
    [("_on_process_clicked", lambda p: p._on_process_clicked()),
     ("_start_ondemand", lambda p: p._start_ondemand("CD3")),
     ("_process_current_channel", lambda p: p._process_current_channel()),
     ("_save_and_continue", lambda p: p._save_and_continue())],
)
def test_a_gpu_path_releases_explore_before_starting(gpu_path_page,
                                                     monkeypatch, path_name,
                                                     driver):
    timeline = []
    _install_gpu_path_recorders(monkeypatch, timeline)
    page = gpu_path_page

    driver(page)

    starts = [i for i, e in enumerate(timeline)
              if isinstance(e, tuple) and e[0] == "worker.start"]
    assert starts, (
        f"{path_name} never reached the worker -- this test proves nothing "
        f"about it; timeline={timeline}")
    releases = [i for i, e in enumerate(timeline)
                if isinstance(e, str) and e.startswith("release:")]
    assert releases, f"{path_name} did not release Explore: {timeline}"
    assert releases[0] < starts[0], (
        f"{path_name} released Explore AFTER starting the worker: {timeline}")

    # ── and the SAME worker is watched, before it is started ──
    watches = [i for i, e in enumerate(timeline)
               if isinstance(e, tuple) and e[0] == "watch"]
    assert watches, (
        f"{path_name} started a production worker without watching it -- "
        f"the full image would stay released for good; timeline={timeline}")
    assert watches[0] < starts[0], (
        f"{path_name} watched the worker AFTER starting it: a worker that "
        f"finished first would never announce it; timeline={timeline}")
    assert releases[0] < watches[0] < starts[0], (
        f"{path_name}: expected release -> watch -> start; timeline={timeline}")
    assert timeline[watches[0]][1] == timeline[starts[0]][1], (
        f"{path_name} watched a different object than it started; "
        f"timeline={timeline}")


def test_a_failing_release_stops_the_worker_from_starting(gpu_path_page,
                                                          monkeypatch):
    """If the hand-off cannot be completed, production must NOT run: two
    users on the GPU is the outcome the gate exists to prevent, and a
    half-released Explore is exactly that."""
    from block01.ui.step0.step0_page import Step0Page

    timeline = []
    _install_gpu_path_recorders(monkeypatch, timeline)
    page = gpu_path_page
    monkeypatch.setattr(
        Step0Page, "_release_explore_for_production",
        lambda self, reason: (_ for _ in ()).throw(RuntimeError("release failed")))

    with pytest.raises(RuntimeError, match="release failed"):
        page._on_process_clicked()

    assert not [e for e in timeline
                if isinstance(e, tuple) and e[0] == "worker.start"], (
        "the worker started even though releasing Explore failed")
    assert not [e for e in timeline
                if isinstance(e, tuple) and e[0] == "watch"], (
        "the worker was watched even though releasing Explore failed -- "
        "nothing past the failure point may run")


def test_the_reader_path_does_not_release_explore(gpu_path_page, monkeypatch):
    """`PreloadWorker` corrects nothing, so preloading must not cost the
    user their full-image view."""
    timeline = []
    mod = _install_gpu_path_recorders(monkeypatch, timeline)
    page = gpu_path_page
    monkeypatch.setattr(mod, "PreloadWorker", _preload_recorder(timeline))

    page._start_preload()

    assert "preload.start" in timeline, "the preload path never ran"
    assert not [e for e in timeline if e.startswith("release:")], (
        f"preloading released Explore: {timeline}")


def _preload_recorder(timeline):
    class _Preload:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            class _S:
                def connect(self, *_a, **_k):
                    pass
            return _S()

        def isRunning(self):
            return False

        def start(self):
            timeline.append("preload.start")

        def stop(self):
            pass

    return _Preload
