"""v16_5 R1 (plan §2.6, §7 gate 4): cancellation and the reader limits.

* A tile whose read ENDS after the plan has left its view is dropped
  before the cache and the result queue (`stats()["stale"]`): reads made
  slow on purpose (`test_read_delay_ms`) are under way when the camera
  moves on, and the cache grows by exactly the tiles still wanted.
* A tile read while wanted but waiting for room in a full result queue is
  not handed over once the plan has left it (a tiny queue bound, no
  draining). It stays in the cache: it was wanted when it was read.
* Closing while the readers wait on a full queue ends every reader thread
  within a second.
* A whole-region coarse reduction stops within 100 ms of its cancel token
  firing, and answers None (nothing is made of a cancelled read).
No GL: the core drains its results in `paint` before it would draw.
"""

import os
import threading
import time

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
zarr = pytest.importorskip("zarr")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.ui.step1_core_binding import core_config  # noqa: E402
from block01.ui.step1_viewer_mount import demo_budgets  # noqa: E402
from block01.viewer import step1_source as sources  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

SIDE = 4096


def os_readers():
    count = 0
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                count += fh.read().startswith("fvc-read")
        except OSError:
            pass
    return count


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(scope="module")
def slide(tmp_path_factory):
    path = tmp_path_factory.mktemp("r1c") / "s.ome.tif"
    rng = np.random.default_rng(1)
    data = rng.integers(0, 255, size=(3, SIDE, SIDE), dtype=np.uint8)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=3, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b", "c"]}})
        cur = data
        for _ in range(3):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _core(slide, result_bytes, delay_ms=0.0):
    raw = RawTileProvider(slide)
    config = dict(core_config(demo_budgets()), threads=2, result_bytes=result_bytes,
                  test_read_delay_ms=delay_ms)
    c = core.ViewerCore(build_raw_manifest(raw), config)
    c.set_display([(i, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(3)], False)
    return raw, c


def _drain_until_idle(c, view):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        c.paint(0, 1, 1, view)
        if c.idle():
            return True
        time.sleep(0.005)
    return False


def test_a_reads_ending_after_the_view_moved_are_not_cached(slide):
    raw, c = _core(slide, result_bytes=64 * 1024 * 1024, delay_ms=300.0)
    try:
        c.set_view(0, [0, 0, 1024, 1024], 1, False)
        assert _drain_until_idle(c, [0.0, 1024.0, 0.0, 1024.0])
        s0 = c.stats()
        c.set_view(0, [2048, 2048, 3072, 3072], 2, False)  # two reads start, slowly
        time.sleep(0.1)
        c.set_view(0, [3072, 0, 4096, 1024], 3, False)     # ... and end after this
        assert _drain_until_idle(c, [0.0, 1024.0, 3072.0, 4096.0])
        s1 = c.stats()
        stale = s1["stale"] - s0["stale"]
        assert stale >= 2
        # every read is either cached or dropped as stale, never both
        assert s1["cached_tiles"] - s0["cached_tiles"] == (s1["reads"] - s0["reads"]) - stale
    finally:
        c.close()
        raw.close()


def test_a2_tiles_waiting_for_room_are_not_handed_over_once_left(slide):
    raw, c = _core(slide, result_bytes=300 * 1024)      # about one 512x512 u8 tile
    try:
        c.set_view(0, [0, 0, 1024, 1024], 1, False)        # (y0, x0, y1, x1): coarse + a view, drained
        assert _drain_until_idle(c, [0.0, 1024.0, 0.0, 1024.0])
        before = c.stats()          # (the plan may already have left a tile behind)
        c.set_view(0, [2048, 2048, 3072, 3072], 2, False)  # another view: not drained
        # one tile in the queue, each of the two readers waiting with one
        assert wait_for(lambda: c.stats()["reads"] >= before["reads"] + 3)
        held = c.stats()
        assert held["pending_results"] == 1
        c.set_view(0, [3072, 0, 4096, 1024], 3, False)     # the plan moves on
        assert wait_for(lambda: c.stats()["stale"] >= held["stale"] + 2)
        assert c.stats()["pending_results"] == 1           # only the one handed over before
    finally:
        c.close()
        raw.close()


def test_b_closing_with_a_full_queue_ends_every_reader(slide):
    raw, c = _core(slide, result_bytes=1)
    try:
        c.set_view(0, [0, 0, 2048, 2048], 1, False)
        assert wait_for(lambda: c.stats()["reads"] >= 3)     # readers now wait on the queue
        assert os_readers() == 2
        t0 = time.monotonic()
        c.close()
        assert wait_for(lambda: os_readers() == 0, 1.0), os_readers()
        assert time.monotonic() - t0 < 1.0
    finally:
        raw.close()


def test_c_a_long_reduction_stops_when_cancelled(tmp_path, slide):
    """A product the size of the slide, reduced whole at stride 64 with a
    small slab budget: many slabs, seconds of work, cancelled mid-way."""
    raw = RawTileProvider(slide)
    try:
        step0 = tmp_path / "step0"
        root = zarr.open_group(str(step0 / "corrected_channels.zarr"), mode="w")
        arr = root.require_group("ROI_1").create_dataset("b", shape=(SIDE, SIDE), chunks=(512, 512),
                                                          dtype=np.float32)
        arr[:, :] = np.random.default_rng(2).normal(0, 9, size=(SIDE, SIDE)).astype(np.float32)
        arr.attrs.update({"roi_bbox_fullres": [0, SIDE, 0, SIDE], "roi_name": "ROI_1",
                          "source_identity": "t", "correction_method": "cucim"})
        table = sources.Step1SourceTable(decisions={"b": "cucim"},
                                         corrected_zarr_path=str(step0 / "corrected_channels.zarr"),
                                         roi_name="ROI_1", roi_bbox=[0, SIDE, 0, SIDE])
        manifest = build_corrected_manifest(table, raw, [1, 2, 4, 8])
        manifest["budget"] = 4096                       # one slab = one 64x64 block
        reader = core.Step1Reader(build_raw_manifest(raw), manifest)
        t0 = time.monotonic()
        full = reader.read_tile("b", 3, 0, 64, 0, 64, 64, 512)
        whole_s = time.monotonic() - t0
        assert full is not None and full["rows"] == 64
        assert whole_s > 0.4, f"too quick to cancel mid-way ({whole_s:.2f} s)"
        token = core.CancelToken()
        got, done = {}, threading.Event()

        def run():
            got["tile"] = reader.read_tile("b", 3, 0, 64, 0, 64, 64, 512, token)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        time.sleep(whole_s / 3)
        assert not done.is_set()
        fired = time.monotonic()
        token.cancel()
        assert done.wait(5.0)
        stopped_ms = (time.monotonic() - fired) * 1000.0
        assert got["tile"] is None
        assert stopped_ms <= 100.0, stopped_ms
    finally:
        raw.close()
