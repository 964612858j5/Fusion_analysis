"""One line per dataset-identity event, and nothing while it is off.

WHY A SECOND TRACE. `perf_trace` answers "what took the time"; this answers
"which slide is this picture". They are different questions with different
frequencies: the timeline records thousands of mouse and frame events a
minute, while a dataset is bound, cleared and loaded a handful of times an
hour. Putting the identity events on the timeline would mean arming the
high-frequency collector to answer a question that does not need it -- and
the last round's lesson was that arming a collector can manufacture the stall
being measured.

WHAT IT RECORDS. The transitions a stale thumbnail has to pass through: a
panel being bound to a dataset, its pixels being dropped, a read starting, a
read RESULT being accepted or rejected, and a host-pushed picture being
accepted or rejected. Each line names the page generation, the dataset, the
panel, the loader, the panel's read generation and which store the picture
came from, so "the popup is showing slide A" can be traced to the call that
installed it instead of inferred.

OFF BY DEFAULT, and read per event so it can be turned on in a running
session: `BLOCK01_DATASET_DEBUG=1` for stderr, `BLOCK01_DATASET_LOG=<path>`
for a file. With neither set this costs one environment lookup.
"""

import os
import time

DEBUG_ENV = "BLOCK01_DATASET_DEBUG"
LOG_ENV = "BLOCK01_DATASET_LOG"


def enabled():
    return bool(os.environ.get(DEBUG_ENV, "") not in ("", "0", "false", "no")
                or os.environ.get(LOG_ENV, "").strip())


def note(event, **fields):
    """Record one identity transition. A no-op when nothing asked for it."""
    if not enabled():
        return
    try:
        parts = [f"{time.monotonic():.6f}", str(event)]
        parts.extend(f"{key}={_short(value)}" for key, value in fields.items())
        line = " ".join(parts)
        path = os.environ.get(LOG_ENV, "").strip()
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if os.environ.get(DEBUG_ENV, "") not in ("", "0", "false", "no"):
            import sys
            print(f"[dataset] {line}", file=sys.stderr)
    except Exception:                                       # noqa: BLE001
        pass                       # a diagnostic may never break a load


def _short(value):
    if value is None:
        return "-"
    if isinstance(value, (tuple, list)):
        return ",".join(_short(v) for v in value)
    text = str(value)
    if len(text) > 96:
        return "…" + text[-95:]
    return text.replace(" ", "_")


def ident(obj):
    """A stable, short identity for an object -- for reading a log, not for
    deciding anything: `id()` is reused once an object is released, which is
    exactly the mistake this project made in the preview cache."""
    return "-" if obj is None else f"{type(obj).__name__}#{id(obj) & 0xFFFFFF:x}"
