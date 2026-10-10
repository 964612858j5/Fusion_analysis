"""Block A9 §44 (step 0), measurement only: one record per stage of every
tile's way from the request to the screen, default off.

    BLOCK01_A9_TILETRACE=<path>     record, and write <path> (JSON lines)
                                    when the process exits

Why not `perf_trace`. Its writer is a Python thread that formats lines while
the app runs; at 240 tiles x 6 stages per level switch that thread would take
the GIL from the very readers being measured. Here a stage costs one clock
read and one `list.append` (atomic under the GIL) on the calling thread, and
nothing else runs until exit. Times are `time.monotonic()`, the clock of the
perf log and of the injector, so the records line up with both.

Stages (`stage`, key = (channel, level, tx, ty)):
    req      the binding asked the scheduler for the tile
    start    a reader thread took it off the queue
    read     the pixels are in hand (+ wall_ms, cpu_ms, and the reader's own
             split: pread_ms, decode_ms, copy_ms, wrap_ms)
    done     the scheduler has cached and delivered it
    recv     the GUI thread applied it (ok=0: rejected as late/stale)
    gpu      its texture was uploaded
and, with no key: present (a frame reached the screen), cov (how many of the
admitted target tiles are on the GPU, written when it changes).
"""

import atexit
import json
import os
import threading
import time

PATH = os.environ.get("BLOCK01_A9_TILETRACE") or None
ON = PATH is not None
LIMIT = 4_000_000

_records = []
_local = threading.local()


def key_of(key):
    tile = key.tile
    return (key.channel, int(tile.level), int(tile.tx), int(tile.ty))


def stamp(stage, key=None, **extra):
    if len(_records) < LIMIT:
        _records.append((time.monotonic(), stage, None if key is None else key_of(key), extra))


def split():
    """The reader thread's own timing split for the tile it is reading."""
    parts = getattr(_local, "parts", None)
    if parts is None:
        parts = _local.parts = {}
    return parts


def take_split():
    parts = getattr(_local, "parts", None)
    _local.parts = None
    return parts or {}


def add(name, ms):
    parts = split()
    parts[name] = parts.get(name, 0.0) + ms


def dump(path=None):
    path = path or PATH
    if not path:
        return
    with open(path, "w") as out:
        for t, stage, key, extra in list(_records):
            row = {"t": round(t, 6), "s": stage}
            if key is not None:
                row["k"] = key
            if extra:
                row.update(extra)
            out.write(json.dumps(row) + "\n")


if ON:
    atexit.register(dump)
