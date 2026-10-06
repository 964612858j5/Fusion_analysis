"""How many pixel reads the viewers made, and of what (block A9-M).

A9-5 / A9-6 assert "an Intensity change reads nothing", "a resident channel
switch reads nothing": that needs a count at the place a read really
happens, and only there. Two boundaries, each counted once:

  * ``raw``       -- `RawTileProvider.read_region` (its `read_tile` calls it,
                     and the viewers' `SourceTileProvider` reaches it through
                     `OmeTiffSource`, so one read is one count);
  * ``corrected`` / ``coarse_plane`` -- `ArrayRegionSource.read_region`, the
                     only way Step1 reads a corrected region or its persisted
                     coarse plane.

The count is always kept (one locked integer add); a `read` perf mark is
emitted only when tracing is on (BLOCK01_PERF=1).
"""

import collections
import threading

from ..utils import perf_trace

_LOCK = threading.Lock()
_TOTAL = collections.Counter()            # stage -> reads
_BY_LEVEL = collections.Counter()         # (stage, level) -> reads
_PIXELS = collections.Counter()           # stage -> pixels
_IN_FLIGHT = [0]                          # reads started and not finished


def in_flight_reads(fn):
    """Decorator for a read boundary: counts the reads under way, so "no new
    read for a while" is not mistaken for "nothing is reading" while one
    slow read is still running (codex A9-M)."""
    import functools

    @functools.wraps(fn)
    def reading(*args, **kwargs):
        with _LOCK:
            _IN_FLIGHT[0] += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with _LOCK:
                _IN_FLIGHT[0] -= 1
    return reading


def in_flight():
    with _LOCK:
        return _IN_FLIGHT[0]


def note(stage, level, pixels, ms=None, channel=None):
    """One read at a boundary."""
    stage = str(stage)
    with _LOCK:
        _TOTAL[stage] += 1
        _BY_LEVEL[(stage, int(level))] += 1
        _PIXELS[stage] += int(pixels)
    if perf_trace.enabled():
        perf_trace.mark("read", stage=stage, level=int(level), px=int(pixels),
                        ms=None if ms is None else round(float(ms), 3),
                        channel=None if channel is None else str(channel))


def snapshot():
    """{stage: reads}, plus 'total'."""
    with _LOCK:
        out = dict(_TOTAL)
    out["total"] = sum(v for k, v in out.items())
    return out


def delta(before):
    """Reads since `before` (a `snapshot()`), per stage and 'total'."""
    now = snapshot()
    keys = set(now) | set(before)
    return {k: now.get(k, 0) - before.get(k, 0) for k in keys}


def by_level():
    with _LOCK:
        return dict(_BY_LEVEL)


def reset():
    """Tests only."""
    with _LOCK:
        _TOTAL.clear()
        _BY_LEVEL.clear()
        _PIXELS.clear()


__all__ = ["note", "snapshot", "delta", "by_level", "reset", "in_flight", "in_flight_reads"]
