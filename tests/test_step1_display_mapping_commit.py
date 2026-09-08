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


def test_committing_writes_the_mapping_and_republishes_the_manifest(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        wb = _seed_workbench(w._step0, {"CD3": {"min": 0.0, "max": 250.0}})
        seen = []
        w._step0.display_mapping_committed.connect(seen.append)

        ok, reason = w._step0.commit_display_mapping(["CD3"])
        assert ok is True, reason

        from block01.utils.channel_remap_config import (
            load_channel_remap_config, channel_remap_config_hash)
        json_path = os.path.join(step0_dir, "step0_channel_remap.json")
        on_disk = load_channel_remap_config(json_path)
        assert on_disk["channels"]["CD3"]["max"] == 250.0

        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            manifest = json.load(f)
        assert manifest["channel_remap_config_hash"] == channel_remap_config_hash(on_disk)
        assert len(seen) == 1 and "CD3" in seen[0]["channels"]
    finally:
        w.close()


def test_a_channel_nobody_tuned_gets_one_stable_window(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        pixels = np.linspace(0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)
        page._workbench_pixels = lambda ch, blocking=False: pixels

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
        page._workbench_pixels = lambda ch, blocking=False: np.linspace(
            0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)

        frozen = page.frozen_display_mapping(["CD3", "DAPI"])
        assert frozen["CD3"]["min"] == 3.0 and frozen["CD3"]["max"] == 7.0
        assert frozen["DAPI"]["auto"] is True          # the untouched one
    finally:
        w.close()


def test_a_save_whose_mapping_cannot_be_committed_fuses_nothing(app, tmp_path, monkeypatch):
    from block01.ui import main_window as mwmod

    w, step0_dir = _window(app, tmp_path)
    try:
        monkeypatch.setattr(mwmod, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(type(w._step0), "commit_display_mapping",
                            lambda self, channels=None, required=None:
                            (False, "disk is full"))
        warned = []
        monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: warned.append(a)))
        for name in ("information", "critical"):
            monkeypatch.setattr(QtWidgets.QMessageBox, name,
                                staticmethod(lambda *a, **k: None))
        started = []
        monkeypatch.setattr(type(w), "_start_fusion_worker",
                            lambda self, *a, **k: started.append(a))

        class _NoDialog:
            def __init__(self, *a, **k):
                pass

            def exec_(self):
                return QtWidgets.QDialog.Rejected
        monkeypatch.setattr(mwmod, "TileSelectDialog", _NoDialog)
        w._p2_params = {"method": "cellpose_wholecell_fusion", "diameter": 30}

        w._save()

        assert started == []
        assert warned and "disk is full" in str(warned[-1])
        # It stopped before producing anything: the config a Save writes on its
        # way to fusing is not there.
        assert not os.path.exists(os.path.join(str(tmp_path), "fusion_config.json"))
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


def test_a_draft_change_is_not_a_commit(app, tmp_path):
    w, step0_dir = _window(app, tmp_path)
    committed = []
    try:
        w._step0.display_mapping_committed.connect(committed.append)
        wb = _seed_workbench(w._step0, {"CD3": {"min": 0.0, "max": 1000.0}})
        wb._params["CD3"]["max"] = 400.0
        wb.params_changed.emit("CD3")
        QtWidgets.QApplication.processEvents()

        assert committed == []
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

        def _pix(ch, blocking=False):
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
        page._workbench_pixels = lambda ch, blocking=False: np.linspace(
            0, 5000, 64 * 64, dtype=np.float32).reshape(64, 64)
        first = page.display_mapping_for_preview(["CD3"])["CD3"]["max"]

        page._auto_window_cache = {}          # what loading a dataset does
        page._workbench_pixels = lambda ch, blocking=False: np.linspace(
            0, 50, 64 * 64, dtype=np.float32).reshape(64, 64)
        second = page.display_mapping_for_preview(["CD3"])["CD3"]["max"]

        assert second < first
    finally:
        w.close()


def test_a_weighted_channel_with_no_window_stops_the_save(app, tmp_path):
    """Refuse rather than fuse a channel the screen shows and the file omits.

    The worker leaves out any channel with no committed window. If the commit
    let that through, the marker would be visible in the preview and simply
    absent from fused.zarr, with nothing said. The refusal names the channels.
    """
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        page._workbench_pixels = lambda ch: None      # no pixels, no window

        ok, reason = page.commit_display_mapping(["CD3"], required=["CD3"])

        assert ok is False
        assert "CD3" in reason
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
        page._slide_lowres_array = lambda name: pixels

        window = page._auto_display_window("CD3")

        assert window is not None
        assert window["max"] > window["min"]
    finally:
        w.close()


def test_a_failed_republish_leaves_the_previous_handoff_valid(app, tmp_path):
    """Both writes or neither.

    The manifest is hashed over the remap config. Writing the config and then
    failing to republish left the published manifest naming a file that no
    longer existed in that form: a handoff that was valid before the Save and
    invalid after a Save that produced nothing.
    """
    from block01.utils.channel_remap_config import channel_remap_config_hash, \
        load_channel_remap_config

    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        _seed_workbench(page, {"CD3": {"min": 0.0, "max": 250.0}})
        assert page.commit_display_mapping(["CD3"])[0] is True

        json_path = os.path.join(step0_dir, "step0_channel_remap.json")
        with open(json_path, "rb") as f:
            before = f.read()

        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            published = json.load(f)
        named = published["channel_remap_config_path"]
        before_named = open(named, "rb").read()

        page._cond_workbench._params["CD3"]["max"] = 999.0
        page._write_step0_handoff = lambda *a, **k: (_ for _ in ()).throw(
            OSError("disk is full"))
        ok, reason = page.commit_display_mapping(["CD3"])

        assert ok is False and "disk is full" in reason
        with open(json_path, "rb") as f:
            assert f.read() == before                 # canonical untouched

        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            manifest = json.load(f)
        assert manifest["channel_remap_config_path"] == named
        assert open(named, "rb").read() == before_named
        assert manifest["channel_remap_config_hash"] == channel_remap_config_hash(
            load_channel_remap_config(named))         # still names a real file

        # And it left no half-committed mapping file lying around.
        strays = [f for f in os.listdir(step0_dir)
                  if f.startswith("step0_channel_remap.")
                  and f not in (os.path.basename(named), "step0_channel_remap.json")]
        assert strays == []
    finally:
        w.close()


def test_the_manifest_names_an_immutable_mapping_file(app, tmp_path):
    """Publishing the manifest is the only moment a consumer sees a change.

    The frozen mapping is written to a new file named after its own hash, so
    the file the published manifest points at is never rewritten in place. A
    commit that dies before the manifest lands leaves that file unreferenced
    and every reader still on the previous, consistent pair.
    """
    from block01.utils.channel_remap_config import channel_remap_config_hash, \
        load_channel_remap_config

    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        _seed_workbench(page, {"CD3": {"min": 0.0, "max": 250.0}})
        assert page.commit_display_mapping(["CD3"])[0] is True
        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            first = json.load(f)["channel_remap_config_path"]
        first_bytes = open(first, "rb").read()

        page._cond_workbench._params["CD3"]["max"] = 120.0
        assert page.commit_display_mapping(["CD3"])[0] is True
        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            manifest = json.load(f)

        assert manifest["channel_remap_config_path"] != first
        assert open(first, "rb").read() == first_bytes      # never rewritten
        assert manifest["channel_remap_config_hash"] == channel_remap_config_hash(
            load_channel_remap_config(manifest["channel_remap_config_path"]))
        assert load_channel_remap_config(
            manifest["channel_remap_config_path"])["channels"]["CD3"]["max"] == 120.0
    finally:
        w.close()


def test_recommitting_the_same_mapping_never_touches_the_published_file(app, tmp_path):
    """The file name is the mapping's own hash, so an unchanged mapping lands on
    the file the published manifest already names. Rewriting it would edit the
    authoritative file in place, and removing it when a later step fails would
    leave the manifest naming nothing at all."""
    from block01.utils.channel_remap_config import channel_remap_config_hash, \
        load_channel_remap_config

    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        _seed_workbench(page, {"CD3": {"min": 0.0, "max": 250.0}})
        assert page.commit_display_mapping(["CD3"])[0] is True
        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            named = json.load(f)["channel_remap_config_path"]
        before = open(named, "rb").read()
        before_mtime = os.path.getmtime(named)

        # Same mapping, and the republish fails.
        page._write_step0_handoff = lambda *a, **k: (_ for _ in ()).throw(
            OSError("disk is full"))
        ok, reason = page.commit_display_mapping(["CD3"])

        assert ok is False and "disk is full" in reason
        assert os.path.exists(named)                    # not deleted
        assert open(named, "rb").read() == before       # not rewritten
        assert os.path.getmtime(named) == before_mtime

        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            manifest = json.load(f)
        assert manifest["channel_remap_config_path"] == named
        assert manifest["channel_remap_config_hash"] == channel_remap_config_hash(
            load_channel_remap_config(named))
    finally:
        w.close()


def test_a_mapping_file_with_the_wrong_contents_is_not_trusted(app, tmp_path):
    """A file left half-written by a killed process has the right name and the
    wrong contents; publishing a manifest hashed over it would be a lie."""
    w, step0_dir = _window(app, tmp_path)
    try:
        page = w._step0
        _seed_workbench(page, {"CD3": {"min": 0.0, "max": 250.0}})
        assert page.commit_display_mapping(["CD3"])[0] is True
        with open(os.path.join(step0_dir, "step0_roi_result.json")) as f:
            named = json.load(f)["channel_remap_config_path"]

        with open(named, "w", encoding="utf-8") as f:
            f.write("{}")                               # truncated leftover

        assert page.commit_display_mapping(["CD3"])[0] is True
        from block01.utils.channel_remap_config import load_channel_remap_config
        assert load_channel_remap_config(named)["channels"]["CD3"]["max"] == 250.0
    finally:
        w.close()
