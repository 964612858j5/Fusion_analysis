"""v16_5 R7c step 2 (docs/v16_5_r7c_odon_alignment_plan.md §3): read-ahead
around the view into the CPU cache, `BLOCK01_STEP1_PREFETCH=off|odon|limited`
(unset: limited, the default the user chose 2026-10-16).

A core with no GL on a synthetic 4-level slide (tmp_path), driven by views:

* off: nothing is asked beyond the view;
* odon: the ring around the view is read (trace "req" fine=2), cached and
  never handed to the planner; after a one-tile move the newly visible
  tiles come from the cache (no new read);
* limited: never more than one read-ahead read at a time.
"""

import time

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
core = pytest.importorskip("fusion_viewer_core")

from block01.ui.step1_core_adapter import build_raw_manifest  # noqa: E402
from block01.ui.step1_core_binding import core_config  # noqa: E402
from block01.ui.step1_viewer_mount import demo_budgets  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402

SIDE = 4096


@pytest.fixture
def slide(tmp_path):
    data = np.random.default_rng(1).integers(0, 255, (2, SIDE, SIDE), dtype=np.uint8)
    path = tmp_path / "s.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=3, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b"]}})
        cur = data
        for _ in range(3):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


def _run(slide, monkeypatch, mode, delay_ms=0.0, views=((1024, 1024),)):
    if mode is None:
        monkeypatch.delenv("BLOCK01_STEP1_PREFETCH", raising=False)
    else:
        monkeypatch.setenv("BLOCK01_STEP1_PREFETCH", mode)
    raw = RawTileProvider(slide)
    c = core.ViewerCore(build_raw_manifest(raw), dict(core_config(demo_budgets()), threads=4,
                                                      test_read_delay_ms=delay_ms), trace=True)
    recs = []
    try:
        c.set_display([(i, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(2)], False)
        for epoch, (y, x) in enumerate(views, start=1):
            box = [y, x, y + 1024, x + 1024]                 # (y0, x0, y1, x1): 2 x 2 level-0 tiles
            c.set_view(0, box, epoch, False)
            end = time.monotonic() + 30
            while time.monotonic() < end:
                c.paint(0, 1, 1, [float(x), float(x + 1024), float(y), float(y + 1024)])
                if c.idle():
                    break
                time.sleep(0.005)
            recs.append(c.drain_trace())
        return recs
    finally:
        c.close()
        raw.close()


def _ahead(recs):
    return {r[2] for r in recs if r[1] == "req" and r[3].get("fine") == 2.0}


def test_off_asks_nothing_ahead(slide, monkeypatch):
    (recs,) = _run(slide, monkeypatch, "off")
    assert not _ahead(recs)


def test_odon_reads_the_ring_and_a_move_finds_it_cached(slide, monkeypatch):
    first, second = _run(slide, monkeypatch, "odon", views=((1024, 1024), (1024, 1536)))
    ahead = _ahead(first)
    assert {(k[2], k[3]) for k in ahead if k[1] == 0} >= {(1, 1), (4, 1), (1, 4), (4, 4)}   # the pad1 ring
    read = {r[2] for r in first if r[1] == "read" and not r[3].get("stale")}
    assert ahead & read                                    # read, not only asked
    assert not {r[2] for r in first if r[1] == "recv"} & ahead     # never handed to the planner
    # one tile right: column 4 becomes visible; its tiles were read ahead
    newly = {(ch, 0, 4, ty) for ch in (0, 1) for ty in (2, 3)}
    assert newly <= ahead & read
    started = {r[2] for r in second if r[1] == "start"}
    assert not newly & started                             # no second read: from the cache
    assert newly <= {r[2] for r in second if r[1] == "recv" and r[3].get("ok") == 1.0}


@pytest.mark.parametrize("mode", ["limited", None])
def test_limited_reads_one_ahead_at_a_time(slide, monkeypatch, mode):
    """limited, and the default (the variable unset) is limited."""
    (recs,) = _run(slide, monkeypatch, mode, delay_ms=60.0)
    ahead = _ahead(recs)
    assert ahead and len(ahead) <= 48
    busy, worst = set(), 0
    for r in sorted(recs, key=lambda r: r[0]):
        if r[2] in ahead and r[1] == "start":
            busy.add(r[2])
            worst = max(worst, len(busy))
        elif r[2] in ahead and r[1] == "read":
            busy.discard(r[2])
    assert worst == 1


def _core(slide, monkeypatch, mode, delay_ms=0.0, fault=None):
    monkeypatch.setenv("BLOCK01_STEP1_PREFETCH", mode)
    if fault:
        monkeypatch.setenv("BLOCK01_RUST_TEST_FAULT", fault)
    raw = RawTileProvider(slide)
    c = core.ViewerCore(build_raw_manifest(raw), dict(core_config(demo_budgets()), threads=4,
                                                      test_read_delay_ms=delay_ms), trace=True)
    c.set_display([(i, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(2)], False)
    return raw, c


def _drain(c, box, seconds=30):
    y, x = box[0], box[1]
    end = time.monotonic() + seconds
    reports = []
    while time.monotonic() < end:
        reports.append(c.paint(0, 1, 1, [float(x), float(x + 1024), float(y), float(y + 1024)]))
        if c.idle():
            break
        time.sleep(0.005)
    return reports


def test_limited_keeps_one_place_across_a_move(slide, monkeypatch):
    """A read-ahead still being read when the view moves keeps its place:
    the new view's read-ahead waits for it."""
    raw, c = _core(slide, monkeypatch, "limited", delay_ms=250.0)
    try:
        c.set_view(0, [1024, 1024, 2048, 2048], 1, False)
        _drain(c, [1024, 1024])
        c.set_view(0, [1024, 1536, 2048, 2560], 2, False)     # one tile right ...
        time.sleep(0.1)                                      # ... its read-ahead starts
        c.set_view(0, [1536, 1536, 2560, 2560], 3, False)     # moved again mid-read
        _drain(c, [1536, 1536])
        recs = c.drain_trace()
    finally:
        c.close()
        raw.close()
    ahead = {r[2] for r in recs if r[1] == "req" and r[3].get("fine") == 2.0}
    busy, worst = set(), 0
    for r in sorted(recs, key=lambda r: r[0]):
        if r[2] in ahead and r[1] == "start":
            busy.add(r[2])
            worst = max(worst, len(busy))
        elif r[2] in ahead and r[1] == "read":
            busy.discard(r[2])
    assert ahead and worst == 1


def test_failing_read_ahead_does_not_fail_the_picture(slide, monkeypatch):
    raw, c = _core(slide, monkeypatch, "odon", fault="ahead:1000")
    try:
        c.set_view(0, [1024, 1024, 2048, 2048], 1, False)
        reports = _drain(c, [1024, 1024], seconds=10)
        st = c.stats()
        recs = c.drain_trace()
    finally:
        c.close()
        raw.close()
    ahead = {r[2] for r in recs if r[1] == "req" and r[3].get("fine") == 2.0}
    failed = {r[2] for r in recs if r[1] == "read" and r[3].get("err") == 1.0}
    assert failed and failed <= ahead                         # only read-ahead reads failed
    assert st["read_errors"] >= len(failed)
    assert not any(r.get("read_failure") for r in reports)   # the picture never gave up
    visible = {(ch, 0, tx, ty) for ch in (0, 1) for tx in (2, 3) for ty in (2, 3)}
    assert visible <= {r[2] for r in recs if r[1] == "recv" and r[3].get("ok") == 1.0}


@pytest.fixture
def two_level(tmp_path):
    """Level 0 8 x 8 tiles, level 1 (the coarsest) 4 x 4 tiles."""
    data = np.random.default_rng(2).integers(0, 255, (2, SIDE, SIDE), dtype=np.uint8)
    path = tmp_path / "two.ome.tif"
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=1, tile=(512, 512), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": ["a", "b"]}})
        tw.write(np.ascontiguousarray(data[:, ::2, ::2]), subfiletype=1, tile=(512, 512), compression="zlib")
    return str(path)


@pytest.mark.parametrize("mode", ["odon", "limited"])
def test_read_ahead_never_takes_the_coarse_level_from_the_picture(two_level, monkeypatch, mode):
    """The target level is the coarsest: its ring overlaps the whole-level
    coarse request, which must still reach the planner (the channel is drawn)."""
    raw, c = _core(two_level, monkeypatch, mode)
    try:
        c.set_view(1, [0, 0, 1024, 1024], 1, False)        # one level-1 tile visible
        _drain(c, [0, 0])
        st = c.stats()
        recs = c.drain_trace()
    finally:
        c.close()
        raw.close()
    got = {r[2] for r in recs if r[1] == "recv" and r[3].get("ok") == 1.0}
    assert {(ch, 1, tx, ty) for ch in (0, 1) for tx in range(4) for ty in range(4)} <= got
    assert sorted(st["coarse_complete"]) == [0, 1]


def test_a_read_ahead_that_comes_into_view_and_fails_asks_for_a_redraw(slide, monkeypatch):
    """A read started ahead, in view by the time it fails: the GUI must hear
    of it (it retries), even with no other read left to wake it."""
    import socket
    monkeypatch.setenv("BLOCK01_STEP1_PREFETCH", "odon")
    monkeypatch.setenv("BLOCK01_RUST_TEST_FAULT", "ahead:1000")
    r, w = socket.socketpair()
    r.setblocking(False)
    raw = RawTileProvider(slide)
    c = core.ViewerCore(build_raw_manifest(raw), dict(core_config(demo_budgets()), threads=1,
                                                      test_read_delay_ms=300.0),
                        notify_handle=w.fileno(), trace=True)
    try:
        c.set_display([(0, 0.0, 255.0, 1.0, 1.0, 1.0, 1.0, 1.0)], False)
        c.set_view(0, [1024, 1024, 1536, 1536], 1, False)   # one tile: (2, 2)
        recs, first, end = [], None, time.monotonic() + 20
        while first is None and time.monotonic() < end:
            c.paint(0, 1, 1, [1024.0, 1536.0, 1024.0, 1536.0])
            recs += c.drain_trace()
            ahead = {x[2] for x in recs if x[1] == "req" and x[3].get("fine") == 2.0}
            first = next((x[2] for x in recs if x[1] == "start" and x[2] in ahead and x[2][1] == 0), None)
            time.sleep(0.01)
        assert first is not None
        _, _, tx, ty = first
        y0, x0 = ty * 512, tx * 512
        c.set_view(0, [y0, x0, y0 + 512, x0 + 512], 2, False)   # only that tile in view, still being read
        time.sleep(0.05)
        try:
            while r.recv(4096):
                pass
        except BlockingIOError:
            pass
        c.ack()
        end = time.monotonic() + 5
        failed = False
        while not failed and time.monotonic() < end:
            recs += c.drain_trace()
            failed = any(x[1] == "read" and x[2] == first and x[3].get("err") == 1.0 for x in recs)
            time.sleep(0.01)
        assert failed
        time.sleep(0.05)
        assert r.recv(4096)                                   # the notice: no paint() needed to see it
    finally:
        c.close()
        raw.close()
        r.close()
        w.close()
