"""Random patches (plan block A2, rulings R1 and 2026-09-24).

Inside the ROI polygon -- concave ones included -- or in the tissue without an
ROI; never more than 40 % blank, measured on a tissue REGION in which the gaps
between nuclei are tissue; never overlapping; the same seed gives the same
patches; a shortfall is reported, not made up. Generated patches go through
Step0 like drawn ones and show up in the navigator and in Step1.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.core import random_patches as rp  # noqa: E402

DS = 16
SLIDE = 4096                       # level-0 pixels; the grid at 16x is 256 x 256


def _tissue_signal(seed=0):
    """A nucleus channel at 16x: a block of tissue made of separate nuclei
    (gaps between them), on an empty background with a little noise."""
    from skimage.draw import disk
    rng = np.random.default_rng(seed)
    g = SLIDE // DS
    sig = rng.normal(5, 1, (g, g)).clip(0)
    for cy in range(40, 200, 6):
        for cx in range(40, 200, 6):
            rr, cc = disk((cy, cx), 2, shape=(g, g))
            sig[rr, cc] = rng.uniform(150, 250)
    return sig.astype(np.float32)


@pytest.fixture(scope="module")
def mask():
    return rp.tissue_mask(_tissue_signal(), DS)


def _brute_blank(mask, bbox):
    full = np.repeat(np.repeat(mask, DS, 0), DS, 1)
    y0, y1, x0, x1 = bbox
    return 1.0 - full[y0:y1, x0:x1].mean()


# ── pure pieces ─────────────────────────────────────────────────────────────
def test_the_mask_level_is_the_one_closest_to_16x_and_not_finer():
    assert rp.pick_mask_level([1, 4, 16, 64]) == 2
    assert rp.pick_mask_level([1, 2, 8, 32]) == 3        # no 16x: 32x, not 8x
    assert rp.pick_mask_level([1, 4, 8]) == 2            # all finer: the coarsest


def test_a_rectangle_is_inside_a_concave_polygon_only_if_no_edge_cuts_it():
    u = [(0, 0), (100, 0), (100, 100), (60, 100), (60, 40), (40, 40), (40, 100), (0, 100)]
    assert rp.rect_inside_polygon((10, 30, 10, 90), u)           # the base of the U
    assert rp.rect_inside_polygon((50, 90, 5, 35), u)            # one arm
    # All four corners are inside the U, but the notch cuts the rectangle.
    assert all(rp._point_in_polygon(x, y, u)
               for x, y in [(10, 50), (90, 50), (90, 90), (10, 90)])
    assert not rp.rect_inside_polygon((50, 90, 10, 90), u)
    assert not rp.rect_inside_polygon((50, 90, 110, 130), u)     # outside


def test_the_blank_share_is_area_weighted_on_the_mask_grid():
    rng = np.random.default_rng(1)
    m = rng.random((40, 50)) > 0.5
    ii = rp.integral_image(m)
    for _ in range(200):
        y0, x0 = int(rng.integers(0, 600)), int(rng.integers(0, 750))
        h, w = int(rng.integers(10, 40)), int(rng.integers(10, 40))
        bbox = (y0, y0 + h, x0, x0 + w)
        assert abs(rp.blank_fraction(ii, DS, bbox) - _brute_blank(m, bbox)) < 1e-9


def test_the_gaps_between_nuclei_are_tissue_not_blank(mask):
    sig = _tissue_signal()
    nuclei = sig > 50
    block = (slice(40, 200), slice(40, 200))
    assert nuclei[block].mean() < 0.5              # a nucleus mask would say mostly blank
    assert mask[block].mean() > 0.95               # the tissue region does not
    assert mask[:20, :].mean() == 0 and mask[:, 230:].mean() == 0   # background stays out


# ── generation ──────────────────────────────────────────────────────────────
def _check(gen, mask, polygon=None, existing=()):
    for p in gen.patches:
        assert _brute_blank(mask, p) <= rp.MAX_BLANK + 1e-9
        if polygon:
            assert rp.rect_inside_polygon(p, polygon)
    everything = list(existing) + list(gen.patches)
    for i, a in enumerate(everything):
        for b in everything[i + 1:]:
            assert not rp._overlap(a, b)


def test_patches_stay_inside_a_concave_roi_and_in_tissue(mask):
    # A U-shaped ROI over the tissue block (level-0 coordinates).
    u = [(640, 640), (3200, 640), (3200, 3200), (2300, 3200), (2300, 1600),
         (1500, 1600), (1500, 3200), (640, 3200)]
    existing = [(700, 1212, 700, 1212)]
    gen = rp.generate(6, 256, 256, mask=mask, ds=DS, region=(640, 3200, 640, 3200),
                      polygon=u, existing=existing)
    assert len(gen.patches) == 6
    _check(gen, mask, polygon=u, existing=existing)


def test_without_an_roi_patches_land_in_the_tissue(mask):
    gen = rp.generate(8, 512, 512, mask=mask, ds=DS, region=(0, SLIDE, 0, SLIDE))
    assert len(gen.patches) == 8
    _check(gen, mask)


def test_the_same_seed_gives_the_same_patches(mask):
    kw = dict(mask=mask, ds=DS, region=(0, SLIDE, 0, SLIDE))
    assert rp.generate(5, 256, 256, **kw).patches == rp.generate(5, 256, 256, **kw).patches
    assert rp.generate(5, 256, 256, seed=7, **kw).patches != rp.generate(5, 256, 256, **kw).patches


def test_a_shortfall_is_reported_not_made_up(mask):
    # Far more 1024 px patches than the tissue block can hold without overlap.
    gen = rp.generate(40, 1024, 1024, mask=mask, ds=DS, region=(0, SLIDE, 0, SLIDE))
    assert 0 < len(gen.patches) < 40
    assert gen.shortfall == 40 - len(gen.patches)
    assert gen.tries == 40 * rp.TRIES_PER_PATCH
    _check(gen, mask)


# ── the strip's entry ───────────────────────────────────────────────────────
pytest.importorskip("PyQt5")
from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_random_is_there_with_no_patch_and_asks_with_the_dialog_values(app, monkeypatch):
    from block01.ui.step1_presegmentation import patches_panel as pp
    panel = pp.PatchesPanel()
    panel.set_patches([])
    asked = []
    panel.random_requested.connect(lambda n, h, w: asked.append((n, h, w)))

    def _accept(dlg):
        dlg.count.setValue(3)
        dlg.width_px.setValue(640)
        dlg.height_px.setValue(384)
        return QtWidgets.QDialog.Accepted
    monkeypatch.setattr(pp.RandomPatchesDialog, "exec_", _accept)
    try:
        assert panel._btn_random.isEnabled()
        panel._btn_random.click()
        assert asked == [(3, 384, 640)]
        panel.set_random_busy(True)
        assert not panel._btn_random.isEnabled()
        panel.set_random_busy(False)
        assert panel._btn_random.isEnabled()
    finally:
        panel.close()


# ── end to end ──────────────────────────────────────────────────────────────
def _roi(bbox=(0, SLIDE, 0, SLIDE)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _window(app, tmp_path):
    from block01.ui.main_window import MainWindow
    from block01.ui.step0.roi_context_model import Patch

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)                      # not a pyramid: 16x is used
    signal = _tissue_signal()

    class _Loader:
        shape = (SLIDE, SLIDE)
        ch_map = {"DAPI": 0, "CD3": 1}
        filepath = str(raw)

        def channel_names(self):
            return ["DAPI", "CD3"]

        def read_region(self, channel, y0, y1, x0, x1, downsample=1):
            ds = max(1, int(downsample))
            return np.zeros(((y1 - y0) // ds or 1, (x1 - x0) // ds or 1), np.float32)

        def read_region_lowres(self, channel, y0, y1, x0, x1, downsample, normalize=True):
            assert int(downsample) == DS and (y0, y1, x0, x1) == (0, SLIDE, 0, SLIDE)
            return signal.copy()

    first = Patch((704, 1216, 704, 1216), 1)
    w = MainWindow()
    page = w._step0
    page.loader = _Loader()
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = SLIDE, SLIDE
    page.overview.full_wsi_mode = False
    page.overview.set_rois_and_patches([_roi()], [first])
    page._on_patches_changed(page.overview._patch_coords())
    step0_dir = tmp_path / "roi1" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    page._roi_context = {
        "roi_id": "roi1", "roi_dir": str(tmp_path / "roi1"), "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir), "step1": str(tmp_path / "roi1" / "step1"),
                      "step2": str(tmp_path / "roi1" / "step2")},
    }
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    page._roi_model.adopt(rois=[_roi()], patches=[first], full_wsi_mode=False,
                          loader=page.loader, nucleus_channel="DAPI")
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30}, "channel_decisions": {}},
        str(step0_dir / "corrected_channels.zarr"))
    w.loader = _Loader()
    w.step0_output = {
        "step0_manifest_path": str(step0_dir / "step0_roi_result.json"),
        "step0_dir": str(step0_dir), "step1_dir": str(tmp_path / "roi1" / "step1"),
        "output_dir": str(step0_dir),
    }
    w.step0_done = True
    w._step1_context_ready = True
    w._current_step = 1
    w._rois = [_roi()]
    w._active_roi = w._rois[0]
    w._on_patches([first])
    return w


def _finish_job(w, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if not w._random_patch_job.is_running():
            break
        time.sleep(0.01)
    QtWidgets.QApplication.processEvents()
    worker = getattr(w._step0, "_geometry_persist_worker", None)
    while worker is not None and worker.is_busy() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    QtWidgets.QApplication.processEvents()


def test_random_patches_join_the_patch_list_everywhere(app, tmp_path):
    w = _window(app, tmp_path)
    try:
        w._show_tissue_navigator()
        nav = w._step0._tissue_navigator_popup.overview
        w._on_random_patches_requested(3, 512, 512)
        assert not w._preseg_patches._btn_random.isEnabled()      # busy while running
        _finish_job(w)
        assert w._preseg_patches._btn_random.isEnabled()
        assert w._step0.geometry_persist_state() == "published"
        names = [nav.patch_name(i) for i in range(len(nav._patches))]
        assert names == ["P1", "P2", "P3", "P4"]                  # numbered after P1
        assert [t.name() for t in w._preseg_patches.tiles()] == names
        assert [a.text() for a in w._patch_menu_actions] == names
        m = rp.tissue_mask(_tissue_signal(), DS)
        new = [tuple(p) for p in w._all_patches[1:]]
        for p in new:
            assert _brute_blank(m, p) <= rp.MAX_BLANK + 1e-9
        everything = [tuple(p) for p in w._all_patches]
        for i, a in enumerate(everything):
            for b in everything[i + 1:]:
                assert not rp._overlap(a, b)
    finally:
        w.close()


def test_a_shortfall_is_announced(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path)
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a[2])))
    try:
        w._on_random_patches_requested(40, 1024, 1024)
        _finish_job(w)
        assert len(told) == 1 and "The rules were not loosened" in told[0]
        found = len(w._all_patches) - 1
        assert 0 < found < 40 and f"Only {found} of 40" in told[0]
    finally:
        w.close()
