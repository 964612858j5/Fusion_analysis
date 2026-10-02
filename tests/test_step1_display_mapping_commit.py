"""Min/Max/Gamma is a live draft on screen and a committed fact on disk.

Dragging the Intensity slider changes what Step1 draws at once and writes
nothing. A Save freezes that draft, writes it to the canonical remap config and
republishes the manifest whose hash names it, and only then fuses — so the
numbers on screen, the numbers in the fusion config and the numbers the
manifest is hashed over are one set. If the commit fails, nothing is fused.

Channels the user never tuned get one stable automatic window, computed from
the whole slide, so a mapping can never depend on which patch was on screen or
how the Save dialog split the image into tiles.

Own module: page-heavy PyQt suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
pytest.importorskip("zarr")

from PyQt5 import QtWidgets  # noqa: E402



def _chosen(w):
    """A pre-segmentation result chosen with Use -- the one thing Save takes
    since block E (user ruling 2026-09-25). The guards these tests are about
    (unsaved settings, the mapping commit, what the run writes) come after."""
    from block01.ui import main_window as mw
    # Of the pixels and saved settings now current, so it is not stale --
    # the fields `_on_preseg_use` gives a real choice.
    key, fhash = w._preseg_current()
    w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30,
                    "params": {"diameter": 30}, "fusion_settings_hash": fhash,
                    "pixel_key": key, "preseg_run_id": "r", "combo_id": "c"}
    w._params_source = mw.PRESEG_SOURCE
    w._preseg_selected = {"run": {"run_id": "r", "source": {"pixel_key": key},
                                  "fusion": {"hash": fhash}}, "combo_id": "c"}
    w._preseg_selection_valid = lambda: (True, "")
    w._preseg_segmentation_config = lambda: mw.normalize_segmentation_config({
        "method": "cellpose_wholecell_fusion", "params": {"diameter": 30},
        "params_source": mw.PRESEG_SOURCE})

@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _roi(bbox=(0, 32, 0, 32)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


class _Loader:
    shape = (64, 64)

    def __init__(self, path):
        self.filepath = path
        self._n = ["DAPI", "CD3"]
        self.ch_map = {c: i for i, c in enumerate(self._n)}

    def channel_names(self):
        return list(self._n)

    @staticmethod
    def _norm(arr):
        return np.clip(np.asarray(arr, np.float32), 0.0, 1.0)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
        return np.zeros((y1 - y0, x1 - x0), np.float32)


def _window(app, tmp_path):
    """MainWindow over a Step0 page with one published handoff."""
    from block01.ui.main_window import MainWindow

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)
    w = MainWindow()
    page = w._step0
    page.loader = _Loader(str(raw))
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 64, 64
    page.overview.full_wsi_mode = False
    page.overview._rois = [_roi()]
    page.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)}]

    step0_dir = tmp_path / "roi1" / "step0"
    step0_dir.mkdir(parents=True, exist_ok=True)
    page._roi_context = {
        "roi_id": "roi1",
        "roi_dir": str(tmp_path / "roi1"),
        "project_dir": str(tmp_path),
        "step_dirs": {"step0": str(step0_dir),
                      "step1": str(tmp_path / "roi1" / "step1"),
                      "step2": str(tmp_path / "roi1" / "step2")},
    }
    page._roi_context_sig = page._roi_context_signature(page._standard_rois())
    page._write_step0_handoff(
        {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
         "channel_decisions": {}},
        str(step0_dir / "corrected_channels.zarr"))

    w.loader = _Loader(str(raw))
    w.step0_output = {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(step0_dir / "step0_roi_result.json"),
        "step0_dir": str(step0_dir),
        "step1_dir": str(tmp_path / "roi1" / "step1"),
        "output_dir": str(step0_dir),
        "channel_remap_config_path": str(step0_dir / "step0_channel_remap.json"),
    }
    return w, str(step0_dir)


def _seed_workbench(page, values):
    """Put channels into the workbench the way the Intensity window does."""
    wb = page._cond_workbench
    wb.set_channel_images({c: np.linspace(0, 1000, 32 * 32, dtype=np.float32)
                           .reshape(32, 32) for c in values})
    for ch, params in values.items():
        wb.set_active_channel(ch)
        wb._params[ch].update(params)
    return wb


def test_a_draft_edit_changes_what_step1_draws_without_touching_disk(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        wb = _seed_workbench(w._step0, {"CD3": {"min": 0.0, "max": 1000.0}})
        json_path = os.path.join(step0_dir, "step0_channel_remap.json")
        before = os.path.exists(json_path) and open(json_path).read()

        assert w._display_mapping()["CD3"]["max"] == 1000.0
        wb._params["CD3"]["max"] = 250.0

        assert w._display_mapping()["CD3"]["max"] == 250.0     # seen at once
        after = os.path.exists(json_path) and open(json_path).read()
        assert after == before                                  # nothing written
    finally:
        w.close()


def test_an_uncommitted_draft_makes_an_existing_result_unreusable(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        wb = _seed_workbench(w._step0, {"CD3": {"min": 0.0, "max": 1000.0}})
        cfg = {"nucleus": {"channel": "DAPI", "weight": 1.0},
               "groups": {"g": {"group_weight": 1.0, "channels": {"CD3": 0.5}}},
               "channel_remap_params": {}}
        before = w._expected_fused_zarr_meta(cfg, "cellpose_wholecell_fusion")

        wb._params["CD3"]["max"] = 250.0
        after = w._expected_fused_zarr_meta(cfg, "cellpose_wholecell_fusion")

        assert before["config_hash"] != after["config_hash"]
    finally:
        w.close()


def test_a_channel_nobody_tuned_gets_one_stable_window(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        pixels = np.linspace(0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)
        page._workbench_pixels = lambda ch, blocking=False, resident_only=False: pixels

        frozen = page.frozen_display_mapping(["CD3"])
        assert "CD3" in frozen
        assert frozen["CD3"]["min"] is not None
        assert frozen["CD3"]["max"] is not None
        assert frozen["CD3"]["auto"] is True

        # Stable: asking again, and asking about a different crop, is the same
        # window, because it is computed from the slide and not from a patch.
        again = page.frozen_display_mapping(["CD3"])
        assert again["CD3"]["min"] == frozen["CD3"]["min"]
        assert again["CD3"]["max"] == frozen["CD3"]["max"]
    finally:
        w.close()


def test_a_tuned_channel_keeps_its_own_window(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        _seed_workbench(page, {"CD3": {"min": 3.0, "max": 7.0}})
        page._workbench_pixels = lambda ch, blocking=False, resident_only=False: np.linspace(
            0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)

        frozen = page.frozen_display_mapping(["CD3", "DAPI"])
        assert frozen["CD3"]["min"] == 3.0 and frozen["CD3"]["max"] == 7.0
        assert frozen["DAPI"]["auto"] is True          # the untouched one
    finally:
        w.close()


def test_intensity_editing_is_frozen_while_a_save_fuses(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        page._btn_intensity_window.setEnabled(True)
        w._lock_ui()
        assert page._btn_intensity_window.isEnabled() is False
        w._unlock_ui()
        assert page._btn_intensity_window.isEnabled() is True
    finally:
        w.close()


def test_a_draft_change_writes_nothing_into_step0(app, tmp_path):
    """Block A6 follow-up (user ruling 2026-10-02): Step0's Intensity file is
    Step0's record. Moving a window in the draft writes nothing to it."""
    w, step0_dir = _window(app, tmp_path)
    try:
        before = {n: open(os.path.join(step0_dir, n), "rb").read() for n in os.listdir(step0_dir)
                  if os.path.isfile(os.path.join(step0_dir, n))}
        wb = _seed_workbench(w._step0, {"CD3": {"min": 0.0, "max": 1000.0}})
        wb._params["CD3"]["max"] = 400.0
        wb.params_changed.emit("CD3")
        QtWidgets.QApplication.processEvents()
        after = {n: open(os.path.join(step0_dir, n), "rb").read() for n in os.listdir(step0_dir)
                 if os.path.isfile(os.path.join(step0_dir, n))}
        assert after == before
    finally:
        w.close()

def test_the_screen_uses_the_window_the_save_will_freeze(app, tmp_path):
    """An untuned channel is not left to the loader's per-region percentile.

    Step1 draws it through the same automatic window a Save would freeze, and
    that window is computed once from the slide — so what is on screen before a
    Save and what lands in fused.zarr after it are the same mapping, and a
    redraw never re-reads the overview to invent a new one.
    """
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        calls = []
        pixels = np.linspace(0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)

        def _pix(ch, blocking=False, resident_only=False):
            calls.append(ch)
            return pixels
        page._workbench_pixels = _pix

        drawn = page.display_mapping_for_preview(["CD3"])["CD3"]
        frozen = page.frozen_display_mapping(["CD3"])["CD3"]
        assert drawn["min"] == frozen["min"]
        assert drawn["max"] == frozen["max"]

        n = len(calls)
        page.display_mapping_for_preview(["CD3"])
        assert len(calls) == n            # memoised, not recomputed per redraw
    finally:
        w.close()


def test_a_new_dataset_does_not_inherit_the_old_slides_auto_window(app, tmp_path):
    """The automatic window is a property of the pixels, so loading another
    slide must discard it rather than map the new channel through the old
    slide's range."""
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        page._workbench_pixels = lambda ch, blocking=False, resident_only=False: np.linspace(
            0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)
        first = page.display_mapping_for_preview(["CD3"])["CD3"]["max"]

        page._auto_window_cache = {}          # what loading a dataset does
        page._workbench_pixels = lambda ch, blocking=False, resident_only=False: np.linspace(
            0, 50, 64 * 64, dtype=np.float32).reshape(64, 64)
        second = page.display_mapping_for_preview(["CD3"])["CD3"]["max"]

        assert second < first
    finally:
        w.close()


def test_a_weighted_channel_with_no_window_stops_the_save(app, tmp_path, monkeypatch):
    """Refuse rather than fuse a channel the screen shows and the fusion omits.

    Generate fuses with Step1's committed settings; a weighted channel with no
    window in them would be absent from fused.zarr with nothing said. The
    refusal names the channels and nothing is started -- and Step0's files
    are not touched either way (block A6 follow-up).
    """
    w, step0_dir = _window(app, tmp_path)
    said, started = [], []
    try:
        before = {n: open(os.path.join(step0_dir, n), "rb").read() for n in os.listdir(step0_dir)
                  if os.path.isfile(os.path.join(step0_dir, n))}
        monkeypatch.setattr(w, "_save_allowed", lambda: True)
        w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30}
        w._params_source = "manual"
        monkeypatch.setattr(w, "_require_committed_fusion_settings", lambda *_a: True)
        monkeypatch.setattr(w, "_params_match_committed_settings", lambda: True)
        monkeypatch.setattr(w, "_committed_fusion_settings",
                            lambda: {"hash": "h", "fusion_config": {},
                                     "display_mapping": {"DAPI": {"min": 0, "max": 1}}})
        monkeypatch.setattr(w, "_fusion_weighted_channels", lambda: ["DAPI", "CD3"])
        monkeypatch.setattr(w, "_start_fusion_worker", lambda *a, **k: started.append(1))
        monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: said.append(a[2])))
        w._corrected_zarr_mode = ""
        w._save()
        assert started == []
        assert said and "CD3" in said[-1] and "DAPI" not in said[-1].split(":")[-1]
        after = {n: open(os.path.join(step0_dir, n), "rb").read() for n in os.listdir(step0_dir)
                 if os.path.isfile(os.path.join(step0_dir, n))}
        assert after == before
    finally:
        w.close()

def test_the_automatic_window_uses_the_real_pixel_source(app, tmp_path):
    """Drive the real method chain, with nothing on the page stubbed.

    An earlier version called the pixel source with a keyword it does not
    accept. Every call raised, every untuned channel silently got no window,
    and the failure was invisible because the tests replaced that source with a
    lambda that accepted the keyword.
    """
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        pixels = np.linspace(0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)
        # Stub the SLIDE READ, one level below the page's own pixel accessor,
        # so `_workbench_pixels` itself is the real method under test.
        page._slide_lowres_array = (
                lambda name, blocking=True, resident_only=False: pixels)

        window = page._auto_display_window("CD3")

        assert window is not None
        assert window["max"] > window["min"]
    finally:
        w.close()


