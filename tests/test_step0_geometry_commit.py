"""A finished ROI/patch edit is published through the ONE Step0 handoff writer.

Patch geometry lives in patch_config.json, which only Step0 writes. Before this,
a patch edited outside the Save flow lived in memory (and, from Step1, in
step1_session.json), so re-reading the manifest silently discarded it.

A geometry-only commit reuses the ONE handoff writer verbatim: same artifacts,
same schema, manifest published last via tmp + fsync + os.replace. It never
recomputes background correction, and it refuses outright when the analysis
region itself changed, because a different ROI needs its own corrected output.

It is also ASYNCHRONOUS now: the callback that ends a patch gesture submits a
task and returns, so every test here submits and then SETTLES -- the outcome
is the worker's answer, delivered on this thread, not the return value of the
call. What each outcome means is unchanged; `test_step0_patch_release.py`
covers the scheduling itself.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined.
"""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")
import numpy as np  # noqa: E402

from PyQt5 import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _roi(bbox=(0, 32, 0, 32)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _page(app, tmp_path, patches=((0, 16, 0, 16),)):
    """A Step0 page with ONE published handoff on disk, as after a real Save."""
    from block01.ui.step0 import step0_page as sp

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)

    class _Loader:
        shape = (64, 64)
        ch_map = {"DAPI": 0, "CD3": 1}
        filepath = str(raw)

        def channel_names(self):
            return ["DAPI", "CD3"]

    page = sp.Step0Page()
    page.loader = _Loader()
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 64, 64
    page.overview.full_wsi_mode = False
    page.overview._rois = [_roi()]
    page.overview._patches = [{"roi_idx": 0, "coords": tuple(p)} for p in patches]

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
    config = {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
              "channel_decisions": {}}
    zarr_path = str(step0_dir / "corrected_channels.zarr")
    page._write_step0_handoff(config, zarr_path)
    return page, str(step0_dir), zarr_path


def _published(step0_dir, name):
    with open(os.path.join(step0_dir, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _settle(page, timeout=15.0):
    """Pump events until the persistence worker is idle, then say what happened.

    The outcome used to be `_persist_geometry_edit`'s return value; it is now
    announced by the worker through a queued signal, so a test has to let the
    event loop deliver it.
    """
    import time
    deadline = time.monotonic() + timeout
    worker = getattr(page, "_geometry_persist_worker", None)
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if worker is None or not worker.is_busy():
            QtWidgets.QApplication.processEvents()
            break
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    return page.geometry_persist_state()


def _patch_bboxes(step0_dir):
    """The published patches, through the MANIFEST'S own path.

    Not through `patch_config.json`: a geometry revision is published as a
    file named by its revision and the fixed name is refreshed afterwards as a
    copy, so reading it by name would answer with the copy rather than with
    what the current handoff points at.
    """
    manifest = _published(step0_dir, "step0_roi_result.json")
    with open(manifest["patch_config_path"], "r", encoding="utf-8") as f:
        return [p["bbox_fullres"] for p in json.load(f)]


def test_an_added_patch_reaches_patch_config_and_the_manifest(app, tmp_path):
    page, step0_dir, _z = _page(app, tmp_path)
    try:
        seen = []
        page.geometry_committed.connect(seen.append)
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        assert page._persist_geometry_edit() is True     # submitted
        assert _settle(page) == "published"

        assert _patch_bboxes(step0_dir) == [[0, 16, 0, 16], [16, 32, 16, 32]]
        assert _published(step0_dir, "step0_roi_result.json")["n_patches"] == 2
        assert len(seen) == 1
        assert len(seen[0]["patches"]) == 2
    finally:
        page.deleteLater()


def test_a_moved_patch_is_re_read_from_disk_not_from_memory(app, tmp_path):
    page, step0_dir, _z = _page(app, tmp_path)
    try:
        page.overview._patches = [{"roi_idx": 0, "coords": (8, 24, 8, 24)}]
        assert page._persist_geometry_edit() is True
        assert _settle(page) == "published"

        # Re-read the published manifest exactly as the Step1 reader does.
        manifest = _published(step0_dir, "step0_roi_result.json")
        with open(manifest["patch_config_path"], "r", encoding="utf-8") as f:
            on_disk = [p["bbox_fullres"] for p in json.load(f)]
        assert on_disk == [[8, 24, 8, 24]]
        assert manifest["handoff_schema_version"] == 2
    finally:
        page.deleteLater()


def test_a_deleted_patch_disappears_from_the_published_geometry(app, tmp_path):
    page, step0_dir, _z = _page(app, tmp_path,
                                patches=((0, 16, 0, 16), (16, 32, 16, 32)))
    try:
        page.overview._patches = [{"roi_idx": 0, "coords": (0, 16, 0, 16)}]
        assert page._persist_geometry_edit() is True
        assert _settle(page) == "published"
        assert _patch_bboxes(step0_dir) == [[0, 16, 0, 16]]
    finally:
        page.deleteLater()


def test_an_unchanged_geometry_publishes_nothing(app, tmp_path):
    page, step0_dir, _z = _page(app, tmp_path)
    try:
        seen = []
        page.geometry_committed.connect(seen.append)
        manifest_path = os.path.join(step0_dir, "step0_roi_result.json")
        before = os.stat(manifest_path).st_mtime_ns

        assert page._persist_geometry_edit() is True      # submitted...
        assert _settle(page) == "staged"                 # ...and published nothing
        assert seen == []
        assert os.stat(manifest_path).st_mtime_ns == before
    finally:
        page.deleteLater()


def test_a_changed_roi_is_refused_and_says_so(app, tmp_path):
    page, step0_dir, _z = _page(app, tmp_path)
    try:
        seen = []
        page.geometry_committed.connect(seen.append)
        before = _published(step0_dir, "roi_config.json")

        page.overview._rois = [_roi((0, 48, 0, 48))]
        assert page._persist_geometry_edit() is True
        assert _settle(page) == "failed"

        # A different analysis region cannot reuse the corrected output that was
        # computed for the old one, so nothing is published and nothing claims
        # the edit was saved.
        assert seen == []
        assert _published(step0_dir, "roi_config.json") == before
        assert "no longer valid" in page._load_status.text()
    finally:
        page.deleteLater()


def test_a_geometry_commit_neither_recomputes_nor_rewrites_corrected_channels(
        app, tmp_path, monkeypatch):
    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        root = zarr.open_group(zarr_path, mode="a")
        grp = root.require_group("ROI_1")
        grp["DAPI"] = np.arange(16, dtype="float32").reshape(4, 4)
        before = np.asarray(zarr.open_group(zarr_path, mode="r")["ROI_1"]["DAPI"][:])

        from block01.ui.step0 import search_ctrl

        def _boom(*_a, **_k):
            raise AssertionError("a background-correction worker was started")

        monkeypatch.setattr(search_ctrl, "BatchProcessWorker", _boom)
        monkeypatch.setattr(search_ctrl, "WsiCorrectionWorker", _boom, raising=False)

        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        assert page._persist_geometry_edit() is True
        assert _settle(page) == "published"

        after = np.asarray(zarr.open_group(zarr_path, mode="r")["ROI_1"]["DAPI"][:])
        assert np.array_equal(before, after)
        # The manifest is still the last thing published.
        assert _published(step0_dir, "step0_roi_result.json")["n_patches"] == 2
    finally:
        page.deleteLater()


def test_a_failed_write_publishes_no_manifest_and_reports_the_failure(
        app, tmp_path, monkeypatch):
    page, step0_dir, _z = _page(app, tmp_path)
    try:
        seen = []
        page.geometry_committed.connect(seen.append)
        manifest_path = os.path.join(step0_dir, "step0_roi_result.json")
        before_manifest = _published(step0_dir, "step0_roi_result.json")
        before_mtime = os.stat(manifest_path).st_mtime_ns

        def _fail(*_a, **_k):
            raise RuntimeError("disk is full")

        from block01.core import step0_handoff
        monkeypatch.setattr(step0_handoff, "commit_geometry_only", _fail)
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        assert page._persist_geometry_edit() is True
        assert _settle(page) == "failed"

        assert seen == []
        assert _published(step0_dir, "step0_roi_result.json") == before_manifest
        assert os.stat(manifest_path).st_mtime_ns == before_mtime
        assert "was not published" in page._load_status.text()
    finally:
        page.deleteLater()


def test_nothing_is_published_before_the_first_save(app, tmp_path):
    page, _dir, _z = _page(app, tmp_path)
    try:
        seen = []
        page.geometry_committed.connect(seen.append)
        page._roi_context = None            # as before any Step0 Save
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})

        assert page._persist_geometry_edit() is True
        assert _settle(page) == "staged"
        assert seen == []
    finally:
        page.deleteLater()


def test_the_load_row_goes_back_to_the_project_after_a_save(app, tmp_path):
    """The Load row belongs to the PROJECT, not to a passing save.

    A patch save borrows it for `Saving patch geometry…` while it runs. The
    success line that used to replace that was removed by user ruling
    (2026-09-15); removing it alone left the row saying "Saving…" for ever,
    with the work long finished.
    """
    page, step0_dir, zarr_path = _page(app, tmp_path)
    try:
        project_line = page._project_status_text()
        assert "Loaded:" in project_line and "channels" in project_line
        page._load_status.setText(project_line)

        page.overview._patches = [{"roi_idx": 0, "coords": (0, 32, 0, 32)}]
        assert page._persist_geometry_edit() is True
        # while it runs, the row says so
        assert "Saving patch geometry" in page._load_status.text()

        assert _settle(page) == "published"

        text = page._load_status.text()
        assert "Saving patch geometry" not in text, text
        assert "Patch geometry saved" not in text, text
        assert text == project_line, text
    finally:
        page.deleteLater()
