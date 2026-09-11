"""Letting go of a patch: what the callback is allowed to do, and no more.

MEASURED before this, on the real desk: the callback that ends a patch gesture
read the published manifest and the correction config, read two geometry files
to compare against, wrote three JSON artifacts, rewrote the corrected zarr's
attributes, scanned it for a validity report, fsynced a manifest and replaced
it -- and then restarted a sweep of every patch x every conditioning channel
after emptying the cache it had just filled. Two independent stalls in one
callback, which is why drawing two patches froze the window.

Both halves are moved here, together, because moving one and leaving the other
still blocks the gesture on the one that stayed.

The writes in this module BLOCK: a barrier inside the writer, a slow fsync, a
read held open in the loader. A test that only measures a fast temporary-file
write would pass with the whole thing still synchronous.

Own module: page-heavy Step0 suites crash pyqtgraph offscreen when combined.
"""

import json
import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")
zarr = pytest.importorskip("zarr")

from PyQt5 import QtWidgets  # noqa: E402

from block01.core import step0_handoff  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _roi(bbox=(0, 32, 0, 32)):
    y0, y1, x0, x1 = bbox
    return {"name": "ROI_1", "bbox_fullres": [y0, y1, x0, x1],
            "polygon_fullres": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


class _Loader:
    shape = (128, 128)
    ch_map = {"DAPI": 0, "CD3": 1, "CD8": 2}

    def __init__(self, raw, hold=None):
        self.filepath = str(raw)
        self.reads = []
        self.read_threads = []
        self.hold = hold

    def channel_names(self):
        return ["DAPI", "CD3", "CD8"]

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True,
                    **kw):
        self.reads.append((ch, (y0, y1, x0, x1)))
        self.read_threads.append(threading.current_thread().name)
        if self.hold is not None:
            assert self.hold.wait(10.0), "a read was never released"
        return np.ones(((y1 - y0) or 1, (x1 - x0) or 1), np.float32)


def _page(app, tmp_path, patches=((0, 16, 0, 16),), hold=None, publish=True):
    """A Step0 page with ONE published handoff on disk, as after a real Save."""
    from block01.ui.step0 import step0_page as sp

    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)

    page = sp.Step0Page()
    page.loader = _Loader(raw, hold=hold)
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page._channel_order = ["DAPI", "CD3", "CD8"]
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 128, 128
    page.overview.full_wsi_mode = False
    page.overview._rois = [_roi()]
    page.overview._patches = [{"roi_idx": 0, "coords": tuple(p)}
                              for p in patches]
    page.patches = [tuple(p) for p in patches]

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
    if publish:
        # As after a real Save. `publish=False` is a page that came up on a
        # directory somebody else published -- a restart.
        page._write_step0_handoff(
            {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
             "channel_decisions": {}},
            str(step0_dir / "corrected_channels.zarr"))
    return page, str(step0_dir)


def _release_patch(page, coords):
    """The real end of a patch gesture: the navigator announces its patches.

    `patches_changed` is what a finished drag emits (drags preview silently),
    and it is connected to BOTH halves of the old freeze -- the preload
    restart and the persistence -- so a test that calls only one of them
    proves only half the contract.
    """
    page.overview._patches.append({"roi_idx": 0, "coords": tuple(coords)})
    page.overview.patches_changed.emit(page.overview._patch_coords())


def _bind_page(page, tmp_path, patches=((0, 16, 0, 16),)):
    """Give an existing page (a MainWindow's own Step0) a published handoff."""
    raw = tmp_path / "raw.ome.tif"
    raw.write_bytes(b"x" * 16)
    page.loader = _Loader(raw)
    page.ome_path = str(raw)
    page.output_dir = str(tmp_path)
    page.panel_csv_path = ""
    page.panel_groups = {}
    page.nucleus_channel = "DAPI"
    page._channel_order = ["DAPI", "CD3", "CD8"]
    page.overview.loader = page.loader
    page.overview.full_h, page.overview.full_w = 128, 128
    page.overview.full_wsi_mode = False
    page.overview._rois = [_roi()]
    page.overview._patches = [{"roi_idx": 0, "coords": tuple(p)}
                              for p in patches]
    page.patches = [tuple(p) for p in patches]
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
    return str(step0_dir)


def _settle(page, timeout=20.0):
    deadline = time.monotonic() + timeout
    worker = getattr(page, "_geometry_persist_worker", None)
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if worker is None or not worker.is_busy():
            break
        time.sleep(0.005)
    QtWidgets.QApplication.processEvents()
    return page.geometry_persist_state()


def _manifest(step0_dir):
    with open(os.path.join(step0_dir, "step0_roi_result.json"), "r",
              encoding="utf-8") as f:
        return json.load(f)


def _patch_bboxes(step0_dir):
    with open(os.path.join(step0_dir, "patch_config.json"), "r",
              encoding="utf-8") as f:
        return [p["bbox_fullres"] for p in json.load(f)]


# ── the callback returns; the write happens elsewhere ────────────────────

def test_the_release_callback_returns_while_the_write_is_blocked(
        app, tmp_path, monkeypatch):
    """The whole point. A write that has not finished -- has not even got past
    its first artifact -- must not be able to hold the gesture."""
    page, step0_dir = _page(app, tmp_path)
    inside = threading.Event()
    release = threading.Event()
    real = step0_handoff.commit_geometry_only

    def blocking(task, **kw):
        inside.set()
        assert release.wait(10.0)
        return real(task, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        page.overview._patches.append({"roi_idx": 0,
                                       "coords": (16, 32, 16, 32)})
        t0 = time.monotonic()
        assert page._persist_geometry_edit() is True
        elapsed = time.monotonic() - t0

        assert inside.wait(5.0), "the write never started"
        assert elapsed < 0.10, (
            f"the callback waited {elapsed * 1000:.0f} ms for the write")
        # And while it is blocked, the page says so rather than claiming the
        # edit is saved.
        assert page.geometry_persist_state() == "saving"
        assert page.geometry_persist_busy() is True
        assert page.persisted_geometry_revision() == 0
        assert _patch_bboxes(step0_dir) == [[0, 16, 0, 16]]

        release.set()
        assert _settle(page) == "published"
        assert _patch_bboxes(step0_dir) == [[0, 16, 0, 16], [16, 32, 16, 32]]
        assert page.persisted_geometry_revision() >= 1
    finally:
        release.set()
        _settle(page)
        page.deleteLater()


def test_a_slow_fsync_is_not_paid_by_the_gesture(app, tmp_path, monkeypatch):
    """Not a barrier this time: the real writer, with a real slow fsync.

    fsync is the part that cannot be made fast -- it is the durability the
    manifest publication is for -- so it is the honest thing to make slow.
    """
    page, step0_dir = _page(app, tmp_path)
    real_fsync = os.fsync
    fsync_threads = []

    def slow_fsync(fd):
        fsync_threads.append(threading.current_thread().name)
        time.sleep(0.30)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    try:
        page.overview._patches.append({"roi_idx": 0,
                                       "coords": (16, 32, 16, 32)})
        t0 = time.monotonic()
        page._persist_geometry_edit()
        elapsed = time.monotonic() - t0

        assert elapsed < 0.10, (
            f"the callback paid {elapsed * 1000:.0f} ms of fsync")
        assert _settle(page) == "published"
        assert fsync_threads, "nothing was fsynced; the test proves nothing"
        assert "MainThread" not in fsync_threads, fsync_threads
    finally:
        _settle(page)
        page.deleteLater()


def test_nothing_is_written_or_read_on_the_gui_thread_by_the_callback(
        app, tmp_path, monkeypatch):
    """Neither half. The persistence AND the preload both left the callback;
    a test that only watched one of them would pass with the other still in
    place, and the gesture would still freeze."""
    page, step0_dir = _page(app, tmp_path)
    gui = threading.current_thread().name
    touched = []
    real_replace = os.replace

    def watch_replace(src, dst, *a, **k):
        touched.append(("replace", threading.current_thread().name))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", watch_replace)
    monkeypatch.setattr(step0_handoff, "published_handoff",
                        _tracking_published(touched))
    try:
        _release_patch(page, (16, 32, 16, 32))

        on_gui = [entry for entry in touched if entry[1] == gui]
        assert on_gui == [], f"the callback did work on the GUI thread: {on_gui}"
        reads_on_gui = [name for name in page.loader.read_threads
                        if name == gui]
        assert reads_on_gui == [], (
            f"the callback read pixels on the GUI thread: {reads_on_gui}")
    finally:
        _settle(page)
        page.deleteLater()


def _tracking_published(touched):
    real = step0_handoff.published_handoff

    def tracked(step0_dir):
        touched.append(("read_manifest", threading.current_thread().name))
        return real(step0_dir)
    return tracked


# ── latest-only, and never backwards ────────────────────────────────────

def test_only_the_newest_of_a_burst_is_written(app, tmp_path, monkeypatch):
    """Ten patches drawn in a second are ten revisions of one geometry, and
    only the last one describes what is on screen."""
    page, step0_dir = _page(app, tmp_path)
    inside = threading.Event()
    release = threading.Event()
    real = step0_handoff.commit_geometry_only
    writes = []

    def blocking(task, **kw):
        writes.append(len(task["patches"]))
        inside.set()
        assert release.wait(10.0)
        return real(task, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        # The first edit occupies the worker...
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        page._persist_geometry_edit()
        assert inside.wait(5.0)

        # ...and four more arrive while it is blocked.
        for i in range(2, 6):
            page.overview._patches.append(
                {"roi_idx": 0, "coords": (16 * i, 16 * i + 16, 0, 16)})
            page._persist_geometry_edit()

        stats = page.geometry_persist_stats()
        assert stats["submitted"] == 5
        assert stats["replaced"] == 3, stats     # only the newest is kept

        release.set()
        assert _settle(page) == "published"

        # Two writes: the one that was in flight and the newest. Not five.
        assert writes == [2, 6], writes
        assert len(_patch_bboxes(step0_dir)) == 6
    finally:
        release.set()
        _settle(page)
        page.deleteLater()


def test_a_task_replaced_mid_write_publishes_nothing(app, tmp_path,
                                                     monkeypatch):
    """The rule the staged temporaries exist for: a revision that has been
    replaced may not publish, not even if it is already past its last
    artifact."""
    page, step0_dir = _page(app, tmp_path)
    before = _patch_bboxes(step0_dir)
    at_publish = threading.Event()
    go = threading.Event()
    real = step0_handoff.commit_geometry_only
    outcomes = []

    def blocking(task, superseded=None, **kw):
        def gate(phase=None):
            if phase != "publish":
                # Every earlier check passes: this test is about the LAST
                # one, with the revision file already written and the manifest
                # already fsynced. Holding at an earlier phase would pass even
                # if the final check were deleted.
                return False
            at_publish.set()
            go.wait(10.0)
            return superseded(phase) if superseded else False
        return real(task, superseded=gate, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    page._geometry_persist().skipped.connect(
        lambda o: outcomes.append(o.get("outcome")))
    try:
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        page._persist_geometry_edit()
        assert at_publish.wait(5.0)
        assert _patch_bboxes(step0_dir) == before, (
            "the task published before it was asked whether it still should")

        # A newer revision arrives, then the older one is let go.
        page.overview._patches.append({"roi_idx": 0, "coords": (32, 48, 0, 16)})
        page._persist_geometry_edit()
        go.set()

        assert _settle(page) == "published"
        assert "superseded" in outcomes, outcomes
        assert len(_patch_bboxes(step0_dir)) == 3   # the NEWER geometry
        # No temporary file of either revision is left behind.
        leftovers = [n for n in os.listdir(step0_dir) if ".tmp." in n]
        assert leftovers == [], leftovers
    finally:
        go.set()
        _settle(page)
        page.deleteLater()


def test_each_revision_is_published_as_its_own_immutable_file(app, tmp_path):
    """Publication is ONE atomic replace, of the manifest.

    A fixed `patch_config.json` cannot give that: while it is being replaced
    the manifest on disk still names it, so a reader following the published
    manifest would see the new patches under the old manifest. Each revision
    gets its own file, which no manifest points at until the manifest that
    names it is itself replaced.
    """
    page, step0_dir = _page(app, tmp_path)
    try:
        seen = []
        for i in range(1, 4):
            _release_patch(page, (16 * i, 16 * i + 16, 0, 16))
            assert _settle(page) == "published"
            manifest = _manifest(step0_dir)
            path = manifest["patch_config_path"]
            name = os.path.basename(path)
            assert name.startswith(f"patch_config.rev{i}."), name
            assert name.endswith(".json"), name
            assert manifest["geometry_revision"] == i
            with open(path, "r", encoding="utf-8") as f:
                assert len(json.load(f)) == i + 1
            seen.append(path)

        # The superseded revisions' files are gone; nothing points at them.
        assert [p for p in seen[:-1] if os.path.exists(p)] == []
        # And the fixed name is kept in step as a copy, for readers that still
        # open it by name -- written AFTER the manifest, never before.
        assert _patch_bboxes(step0_dir) == [
            p["bbox_fullres"] for p in json.load(open(seen[-1]))]
    finally:
        page.deleteLater()


def test_a_patch_edit_does_not_touch_the_corrected_zarr_at_all(app, tmp_path):
    """A patch is not a correction. The previous version rewrote the corrected
    zarr's attributes and rescanned it for a validity report -- before the last
    supersede check, so a task that published nothing had still written durable
    state, and a scan of a real corrected output is not free either."""
    page, step0_dir = _page(app, tmp_path)
    zarr_path = os.path.join(step0_dir, "corrected_channels.zarr")
    try:
        before = {}
        for root, _dirs, files in os.walk(zarr_path):
            for name in files:
                full = os.path.join(root, name)
                before[full] = os.stat(full).st_mtime_ns

        _release_patch(page, (16, 32, 16, 32))
        assert _settle(page) == "published"

        after = {}
        for root, _dirs, files in os.walk(zarr_path):
            for name in files:
                full = os.path.join(root, name)
                after[full] = os.stat(full).st_mtime_ns
        assert after == before, "the patch edit wrote into the corrected zarr"
    finally:
        page.deleteLater()


def test_a_superseded_task_leaves_nothing_of_itself_on_disk(app, tmp_path,
                                                            monkeypatch):
    """"It has not written anything durable" has to be literally true."""
    page, step0_dir = _page(app, tmp_path)
    at_publish = threading.Event()
    go = threading.Event()
    real = step0_handoff.commit_geometry_only
    before = sorted(os.listdir(step0_dir))

    def blocking(task, superseded=None, **kw):
        def gate(phase=None):
            if phase != "publish":
                return False
            at_publish.set()
            go.wait(10.0)
            return True                     # replaced, always
        return real(task, superseded=gate, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        _release_patch(page, (16, 32, 16, 32))
        assert at_publish.wait(5.0)
        go.set()
        _settle(page)
        assert sorted(os.listdir(step0_dir)) == before, (
            "a task that published nothing left files behind")
        assert _manifest(step0_dir)["n_patches"] == 1
    finally:
        go.set()
        _settle(page)
        page.deleteLater()


def test_the_published_geometry_is_read_through_the_manifest(app, tmp_path):
    """Which file IS the published geometry is the manifest's answer.

    Proved by making the two disagree: the compatibility copy is overwritten
    with nonsense, and the page must still see the real geometry -- and must
    still recognise an unchanged edit as unchanged, which is the comparison
    that decides whether anything is published at all.
    """
    page, step0_dir = _page(app, tmp_path)
    try:
        _release_patch(page, (16, 32, 16, 32))
        assert _settle(page) == "published"

        with open(os.path.join(step0_dir, "patch_config.json"), "w",
                  encoding="utf-8") as f:
            json.dump([{"name": "P9", "bbox_fullres": [1, 2, 3, 4]}], f)

        rois, patches = step0_handoff.published_geometry(step0_dir)
        assert patches == [[0, 16, 0, 16], [16, 32, 16, 32]], patches

        # The same geometry again: recognised as unchanged, which it only can
        # be if the comparison followed the manifest.
        page.overview.patches_changed.emit(page.overview._patch_coords())
        assert _settle(page) == "staged"
        assert page.geometry_persist_stats()["published"] == 1
    finally:
        page.deleteLater()


def test_a_reader_following_the_manifest_never_sees_a_torn_geometry(
        app, tmp_path):
    """Read the handoff exactly as Step1 does -- manifest first, then the file
    it names -- after every edit, and the two always agree."""
    page, step0_dir = _page(app, tmp_path)
    try:
        for i in range(1, 5):
            _release_patch(page, (16 * i, 16 * i + 16, 0, 16))
            assert _settle(page) == "published"
            manifest = _manifest(step0_dir)
            with open(manifest["patch_config_path"], "r",
                      encoding="utf-8") as f:
                patches = json.load(f)
            assert manifest["n_patches"] == len(patches) == i + 1
            assert manifest["geometry_revision"] == i
    finally:
        page.deleteLater()


def test_an_older_revision_cannot_overwrite_a_newer_one(app, tmp_path):
    """Publication is serial and monotonic. A task that is somehow older than
    what is on disk is refused rather than allowed to move it backwards."""
    from block01.workers.geometry_persist_worker import GeometryPersistWorker

    seen = []
    worker = GeometryPersistWorker(
        commit=lambda task, superseded=None: (
            seen.append(task["revision"]) or {"outcome": "committed"}))
    worker.start()
    try:
        worker.submit({"revision": 5, "dataset_gen": 1})
        deadline = time.monotonic() + 5.0
        while worker.published_revision() != 5 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert worker.published_revision() == 5

        worker.submit({"revision": 4, "dataset_gen": 1})
        deadline = time.monotonic() + 5.0
        while worker.is_busy() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert seen == [5], f"an older revision was written: {seen}"
        assert worker.stats()["skipped"] == 1
    finally:
        worker.stop()


def test_a_dataset_switch_kills_the_queued_geometry(app, tmp_path,
                                                    monkeypatch):
    """A task built for the previous slide would publish that slide's geometry
    into this slide's directory."""
    page, step0_dir = _page(app, tmp_path)
    inside = threading.Event()
    release = threading.Event()
    real = step0_handoff.commit_geometry_only
    written = []

    def blocking(task, **kw):
        written.append(len(task["patches"]))
        inside.set()
        assert release.wait(10.0)
        return real(task, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        page._persist_geometry_edit()
        assert inside.wait(5.0)
        page.overview._patches.append({"roi_idx": 0, "coords": (32, 48, 0, 16)})
        page._persist_geometry_edit()            # queued behind the blocked one

        page._dataset_gen += 1
        page._geometry_persist_worker.invalidate(int(page._dataset_gen))
        release.set()
        _settle(page)

        assert written == [2], written            # the queued task never ran
        assert page.geometry_persist_stats()["stale_dataset"] == 1
    finally:
        release.set()
        _settle(page)
        page.deleteLater()


# ── the preload half ────────────────────────────────────────────────────

def test_a_new_patch_does_not_restart_the_preload_or_clear_the_cache(
        app, tmp_path):
    """The other stall. An added patch used to empty the cache and re-read
    every patch x every channel; the arrays are keyed by bbox, so they are
    kept, and only the new patch is read."""
    page, step0_dir = _page(app, tmp_path)
    try:
        page._on_patches_changed([(0, 16, 0, 16)])
        scheduler = page._preload
        assert scheduler is not None
        assert scheduler.drain(15_000)
        first = list(page.loader.reads)
        assert first, "nothing was preloaded at all"
        assert scheduler.resident((0, 16, 0, 16), "CD3") is not None

        page.loader.reads.clear()
        page._on_patches_changed([(0, 16, 0, 16), (16, 32, 16, 32)])
        assert scheduler.drain(15_000)

        assert scheduler.resident((0, 16, 0, 16), "CD3") is not None, (
            "the first patch's pixels were thrown away")
        assert {bbox for _ch, bbox in page.loader.reads} == {(16, 32, 16, 32)}, (
            f"patches already read were read again: {page.loader.reads}")
    finally:
        page.deleteLater()


def test_a_read_in_flight_does_not_hold_the_next_patch(app, tmp_path):
    """A read already inside the loader cannot be cancelled, so the callback
    must not be waiting on one -- not even to start the next patch's."""
    hold = threading.Event()
    page, step0_dir = _page(app, tmp_path, hold=hold)
    try:
        page._on_patches_changed([(0, 16, 0, 16)])
        deadline = time.monotonic() + 5.0
        while not page.loader.reads and time.monotonic() < deadline:
            time.sleep(0.005)
        assert page.loader.reads, "no read started"

        t0 = time.monotonic()
        page._on_patches_changed([(0, 16, 0, 16), (16, 32, 16, 32)])
        elapsed = time.monotonic() - t0
        assert elapsed < 0.15, (
            f"the callback waited {elapsed * 1000:.0f} ms for a read")
    finally:
        hold.set()
        page._preload.stop()
        page.deleteLater()


def test_the_reads_a_patch_edit_makes_are_bounded(app, tmp_path):
    """Not "all patches x all channels" any more: a bounded number of readers,
    and the current patch first."""
    page, step0_dir = _page(app, tmp_path)
    try:
        page.current_channel = "CD3"
        page._on_patches_changed([(0, 16, 0, 16), (16, 32, 16, 32),
                                  (32, 48, 32, 48)])
        scheduler = page._preload
        assert scheduler.stats()["max_readers"] <= 2
        assert scheduler.drain(20_000)
        # Every tile is read at most once, however many edits arrive.
        assert len(page.loader.reads) == len(set(page.loader.reads))
    finally:
        page.deleteLater()


def test_drawing_patches_quickly_keeps_the_work_bounded(app, tmp_path):
    """Ten patches in a second: bounded tasks, bounded backlog, no re-reads.

    The old behaviour was the opposite of bounded -- each edit cancelled the
    preload, emptied the cache and queued every patch x every channel again,
    so the tenth edit queued ten patches' worth of reads for pixels the first
    nine edits had already read.
    """
    page, step0_dir = _page(app, tmp_path)
    try:
        for i in range(1, 11):
            _release_patch(page, (16 * i, 16 * i + 16, 0, 16))
            stats = page._preload.stats()
            assert (stats["foreground_pending"] + stats["background_pending"]
                    <= page._preload.max_pending), stats
            assert stats["in_flight"] <= stats["max_readers"], stats
        persist = page.geometry_persist_stats()
        # Eleven submissions, at most two of them written: the one in flight
        # and the newest.
        assert persist["submitted"] == 10
        assert persist["published"] + persist["skipped"] <= 10
        assert persist["replaced"] >= 1

        assert page._preload.drain(30_000)
        _settle(page)
        # Every tile read at most once across the whole burst.
        assert len(page.loader.reads) == len(set(page.loader.reads))
        assert _patch_bboxes(step0_dir) == [
            [0, 16, 0, 16]] + [[16 * i, 16 * i + 16, 0, 16]
                               for i in range(1, 11)]
    finally:
        page._preload.stop()
        _settle(page)
        page.deleteLater()


# ── the revision survives the application ───────────────────────────────

def test_a_restarted_page_never_writes_over_the_published_revision(
        app, tmp_path, monkeypatch):
    """The counter lives in a PAGE; the revisions live in a DIRECTORY.

    Page A publishes revision 1 and is destroyed. Page B comes up on the same
    manifest with a counter of zero, and its first edit would be "revision 1"
    again -- straight onto the file the published manifest is pointing at,
    which is the atomicity gone. Held at the publication so the old manifest
    can be read WHILE the new geometry is being written.
    """
    page_a, step0_dir = _page(app, tmp_path)
    _release_patch(page_a, (16, 32, 16, 32))
    assert _settle(page_a) == "published"
    first = _manifest(step0_dir)
    first_patch_path = first["patch_config_path"]
    assert first["geometry_revision"] == 1
    first_bytes = open(first_patch_path, "rb").read()
    page_a.stop_background_jobs()
    page_a.deleteLater()
    QtWidgets.QApplication.processEvents()

    # A NEW page, on the same directory, counting from zero.
    page_b, _dir = _page(app, tmp_path, publish=False,
                         patches=((0, 16, 0, 16), (16, 32, 16, 32)))
    assert page_b._geometry_revision == 0

    at_publish = threading.Event()
    go = threading.Event()
    real = step0_handoff.commit_geometry_only

    def blocking(task, superseded=None, **kw):
        def gate(phase=None):
            if phase == "publish":
                at_publish.set()
                go.wait(10.0)
            return superseded(phase) if superseded else False
        return real(task, superseded=gate, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        _release_patch(page_b, (32, 48, 0, 16))
        assert at_publish.wait(5.0)

        # Held one step before publication: the old manifest still reads out
        # the old geometry, and its file is byte-for-byte untouched.
        held = _manifest(step0_dir)
        assert held["geometry_revision"] == 1
        assert held["patch_config_path"] == first_patch_path
        with open(first_patch_path, "r", encoding="utf-8") as f:
            assert [p["bbox_fullres"] for p in json.load(f)] == [
                [0, 16, 0, 16], [16, 32, 16, 32]]
        assert open(first_patch_path, "rb").read() == first_bytes

        go.set()
        assert _settle(page_b) == "published"
        after = _manifest(step0_dir)
        assert after["geometry_revision"] > 1, after["geometry_revision"]
        assert after["patch_config_path"] != first_patch_path
        with open(after["patch_config_path"], "r", encoding="utf-8") as f:
            assert [p["bbox_fullres"] for p in json.load(f)] == [
                [0, 16, 0, 16], [16, 32, 16, 32], [32, 48, 0, 16]]
    finally:
        go.set()
        _settle(page_b)
        page_b.stop_background_jobs()
        page_b.deleteLater()


def test_a_save_keeps_the_geometry_baseline(app, tmp_path):
    """A Save republishes the whole manifest. Dropping `geometry_revision`
    there would hand the next patch edit a number a file already uses."""
    page, step0_dir = _page(app, tmp_path)
    try:
        _release_patch(page, (16, 32, 16, 32))
        assert _settle(page) == "published"
        assert _manifest(step0_dir)["geometry_revision"] == 1

        page._write_step0_handoff(
            {"method_params": {"tophat_radius": 25, "cucim_sigma": 30},
             "channel_decisions": {}},
            os.path.join(step0_dir, "corrected_channels.zarr"))
        assert _manifest(step0_dir)["geometry_revision"] == 1
        assert page._geometry_revision >= 1
    finally:
        page.deleteLater()


def test_a_manifest_without_a_revision_still_cannot_be_written_over(
        app, tmp_path, monkeypatch):
    """The baseline can be missing -- a handoff published before the field
    existed, or one somebody rewrote. The numbering then starts at 1 again,
    so the NAME is what keeps the published file safe: it carries the
    geometry's hash and a random token, and a path that already exists is
    refused rather than replaced.

    Held at the publication, because that is the window in which the old file
    is still the one the manifest names. (Afterwards it is unreferenced and
    is deleted, which is the point of deleting it.)
    """
    page_a, step0_dir = _page(app, tmp_path)
    _release_patch(page_a, (16, 32, 16, 32))
    assert _settle(page_a) == "published"
    first = _manifest(step0_dir)["patch_config_path"]
    first_bytes = open(first, "rb").read()
    page_a.stop_background_jobs()
    page_a.deleteLater()
    QtWidgets.QApplication.processEvents()

    # The baseline disappears; the file it points at does not.
    manifest = _manifest(step0_dir)
    manifest.pop("geometry_revision", None)
    with open(os.path.join(step0_dir, "step0_roi_result.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f)

    page_b, _dir = _page(app, tmp_path, publish=False,
                         patches=((0, 16, 0, 16), (16, 32, 16, 32)))
    at_publish = threading.Event()
    go = threading.Event()
    real = step0_handoff.commit_geometry_only

    def blocking(task, superseded=None, **kw):
        def gate(phase=None):
            if phase == "publish":
                at_publish.set()
                go.wait(10.0)
            return superseded(phase) if superseded else False
        return real(task, superseded=gate, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        _release_patch(page_b, (32, 48, 0, 16))
        assert at_publish.wait(5.0)

        # The new revision is written, the old manifest still names the old
        # file, and that file is byte-for-byte what it was.
        assert _manifest(step0_dir)["patch_config_path"] == first
        assert open(first, "rb").read() == first_bytes, (
            "the new revision was written over the file the published "
            "manifest was pointing at")

        go.set()
        assert _settle(page_b) == "published"
        assert _manifest(step0_dir)["patch_config_path"] != first
    finally:
        go.set()
        _settle(page_b)
        page_b.stop_background_jobs()
        page_b.deleteLater()


def test_a_restarted_page_keeps_publishing_after_its_first_edit(
        app, tmp_path):
    """The page has to ADOPT the number the worker published.

    Otherwise its counter stays behind the directory's: the first edit is
    bumped to 2 and published, the second is submitted as 2 again -- which the
    worker refuses as out of order, and refusing it is not harmless, because
    the geometry on screen is then not what is on disk and Step1 is locked.
    """
    page_a, step0_dir = _page(app, tmp_path)
    _release_patch(page_a, (16, 32, 16, 32))
    assert _settle(page_a) == "published"
    page_a.stop_background_jobs()
    page_a.deleteLater()
    QtWidgets.QApplication.processEvents()

    page_b, _dir = _page(app, tmp_path, publish=False,
                         patches=((0, 16, 0, 16), (16, 32, 16, 32)))
    try:
        _release_patch(page_b, (32, 48, 0, 16))
        assert _settle(page_b) == "published"
        assert page_b._geometry_revision >= 2, page_b._geometry_revision

        _release_patch(page_b, (48, 64, 0, 16))
        assert _settle(page_b) == "published", (
            "the second edit after a restart was refused as out of order")
        assert len(_patch_bboxes(step0_dir)) == 4
        assert page_b.geometry_ready_for_consumers() is True
    finally:
        page_b.stop_background_jobs()
        page_b.deleteLater()


def test_an_out_of_order_task_is_only_confirmed_if_the_disk_agrees(app,
                                                                   tmp_path):
    """"Older than what we published" says nothing about whether the file on
    disk describes the geometry on screen -- so the files are compared, and a
    consumer is refused when they differ."""
    from block01.workers.geometry_persist_worker import GeometryPersistWorker

    page, step0_dir = _page(app, tmp_path)
    try:
        _release_patch(page, (16, 32, 16, 32))
        assert _settle(page) == "published"

        worker = page._geometry_persist_worker
        published = page.persisted_geometry_revision()
        rois = page._standard_rois()

        # (a) an out-of-order task describing exactly what is published
        worker.submit({"revision": 1, "dataset_gen": int(page._dataset_gen),
                       "step0_dir": step0_dir, "rois": rois,
                       "patches": page._standard_patches(rois),
                       "roi_context_changed": False, "spec": {}})
        assert worker.wait_idle()
        assert worker.consumable(int(page._dataset_gen), published) is True

        # (b) one describing something else entirely
        worker.submit({"revision": 1, "dataset_gen": int(page._dataset_gen),
                       "step0_dir": step0_dir, "rois": rois,
                       "patches": [{"name": "P1",
                                    "bbox_fullres": [99, 115, 99, 115]}],
                       "roi_context_changed": False, "spec": {}})
        assert worker.wait_idle()
        assert worker.consumable(int(page._dataset_gen), published) is False
        assert worker.blocked()[1] == "stale_revision_unconfirmed"
    finally:
        page.stop_background_jobs()
        page.deleteLater()


# ── what a consumer is allowed to see ───────────────────────────────────

def test_step1_does_not_open_on_geometry_that_is_not_on_disk_yet(app):
    """Step1 reads the handoff from DISK. While the write is in flight, the
    file is one revision behind the screen, so entering would silently run on
    the old geometry."""
    from block01.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w._step0.geometry_ready_for_consumers = lambda: False
        w._step1_context_ready = True
        before = w._stack.currentIndex()
        w._go_to_step1()
        assert w._stack.currentIndex() == before, "Step1 opened anyway"
        assert "Saving the patch geometry" in w.prev_status.text()

        w._step0.geometry_ready_for_consumers = lambda: True
        w._go_to_step1()
        assert w._stack.currentIndex() == 1
    finally:
        w.close()


def test_step1_is_refused_before_the_page_hears_that_the_write_failed(
        app, tmp_path, monkeypatch):
    """The window between the worker finishing and the page being told.

    The outcome reaches the page through a QUEUED signal. If the gate asked
    "is the worker busy?", then in the moment after the worker finished and
    before that signal is delivered the answer would be "no" -- and Step1
    would open on a handoff that the write FAILED to update.

    So this test deliberately never lets the event loop run: the worker is
    driven to completion with `wait()`, and the page's handler has provably
    not run (its state is still "saving").
    """
    from block01.ui.main_window import MainWindow

    w = MainWindow()
    page = w._step0
    _bind_page(page, tmp_path)
    w._step1_context_ready = True
    w.step0_output = {"step0_manifest_path": os.path.join(
        page._roi_context["step_dirs"]["step0"], "step0_roi_result.json")}
    entered = []
    w._load_step0_roi_result = lambda **_kw: entered.append(True) or True
    try:
        def _fail(*_a, **_k):
            raise RuntimeError("disk is full")

        monkeypatch.setattr(step0_handoff, "commit_geometry_only", _fail)
        page.overview._patches.append({"roi_idx": 0,
                                       "coords": (16, 32, 16, 32)})
        assert page._persist_geometry_edit() is True
        worker = page._geometry_persist_worker
        assert worker.wait_idle()           # finished; nothing delivered yet
        assert worker.is_busy() is False
        assert page.geometry_persist_state() == "saving", (
            "the page was told; this test proves nothing")

        w._go_to_step1()
        assert w._stack.currentIndex() != 1, (
            "Step1 opened on a handoff whose write had failed")
        assert page.geometry_ready_for_consumers() is False
        assert page.geometry_blocked_reason() in ("failed", "write_failed")

        # And the successful case in the same window is safe to enter.
        monkeypatch.undo()
        page.overview._patches.append({"roi_idx": 0, "coords": (32, 48, 0, 16)})
        page._persist_geometry_edit()
        assert worker.wait_idle()
        assert page.geometry_persist_state() == "saving"   # still not told
        assert page.geometry_ready_for_consumers() is True
        w._go_to_step1()
        assert w._stack.currentIndex() == 1
    finally:
        page.stop_background_jobs()
        w.close()


def test_an_unchanged_geometry_counts_as_confirmed_not_as_pending(
        app, tmp_path):
    """Nothing to write is not the same as "not written yet": the file on
    disk already describes this geometry, so a consumer may read it."""
    page, step0_dir = _page(app, tmp_path)
    try:
        page.overview.patches_changed.emit(page.overview._patch_coords())
        worker = page._geometry_persist_worker
        assert worker.wait_idle()
        assert page.geometry_ready_for_consumers() is True
        assert page.geometry_blocked_reason() is None
        assert _settle(page) == "staged"
        assert page.geometry_ready_for_consumers() is True
    finally:
        page.deleteLater()


def test_the_page_reports_the_revision_that_is_actually_on_disk(
        app, tmp_path, monkeypatch):
    page, step0_dir = _page(app, tmp_path)
    inside = threading.Event()
    release = threading.Event()
    real = step0_handoff.commit_geometry_only

    def blocking(task, **kw):
        inside.set()
        assert release.wait(10.0)
        return real(task, **kw)

    monkeypatch.setattr(step0_handoff, "commit_geometry_only", blocking)
    try:
        page.overview._patches.append({"roi_idx": 0, "coords": (16, 32, 16, 32)})
        page._persist_geometry_edit()
        assert inside.wait(5.0)
        assert page.persisted_geometry_revision() == 0
        assert page.geometry_persist_state() == "saving"
        release.set()
        assert _settle(page) == "published"
        assert page.persisted_geometry_revision() == 1
    finally:
        release.set()
        _settle(page)
        page.deleteLater()


def test_the_release_is_instrumented_with_what_it_cost_and_what_was_dropped(
        app, tmp_path, monkeypatch):
    """An offline reader has to be able to answer "how long did the callback
    take, which revision was it, how many were merged away, and how many
    reads did it actually make" -- from the log, not from inference."""
    from block01.utils import perf_trace

    page, step0_dir = _page(app, tmp_path)
    records = []
    monkeypatch.setattr(perf_trace.WRITER, "submit",
                        lambda rec: records.append(rec) or True)
    os.environ["BLOCK01_PERF"] = "1"
    try:
        page.current_channel = "CD3"
        _release_patch(page, (16, 32, 16, 32))
        _settle(page)

        def _fields(name):
            hits = [rec for rec in records if rec[2] == name]
            assert hits, [rec[2] for rec in records]
            return dict(hits[-1][4] or {}), hits[-1]

        submit, raw = _fields("patch.persist_submit")
        # The span's own duration IS what the gesture paid.
        assert raw[3] is not None or "dur_ms" in submit
        assert int(submit.get("geometry_rev", 0)) >= 1
        for field in ("replaced", "published", "skipped", "published_rev",
                      "dataset_gen"):
            assert field in submit, (field, submit)

        request, _raw = _fields("step0.preload_request")
        for field in ("foreground", "background_queued", "cache_hits",
                      "cache_misses", "reads", "in_flight", "max_readers"):
            assert field in request, (field, request)
        # Every read is a JOB with both ends and an active count, so an
        # offline reader can say how many were in flight at once.
        reads = [rec for rec in records
                 if rec[2] == "read.end"
                 and (rec[4] or {}).get("source") == "step0.preload_read"]
        assert reads, [rec[2] for rec in records]
        for field in ("active_reads", "active_jobs", "dur_ms", "channel"):
            assert field in reads[0][4], (field, reads[0][4])
    finally:
        os.environ.pop("BLOCK01_PERF", None)
        _settle(page)
        page.deleteLater()
