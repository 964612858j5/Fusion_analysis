"""A first-frame log for a REAL slide. Silent unless it is asked for.

Why it exists: the Tissue Preview's first frame is produced by a chain of
low-frequency events -- a dataset bind, a context activation, a snapshot that
cannot draw yet, a whole-slide read, a display-window seed, a frame request,
a publish, and a panel that accepts or refuses the pixels. On synthetic data
that chain completes; a report that it does not complete on a real slide can
only be acted on if the chain says WHERE it stopped.

Enable with an environment variable naming the file::

    BLOCK01_TISSUE_LOG=/tmp/block01_tissue.log python main.py

With the variable unset nothing is formatted, nothing is opened and nothing
is written: `note()` returns on its first line.

What is never done here: reading, scanning or copying an array. A record
carries an array's shape, dtype and a CHEAP fingerprint (four corner values
and the size), never its contents.
"""

import os
import threading
import time

_ENV = "BLOCK01_TISSUE_LOG"

_lock = threading.Lock()
_handle = None
_path = None
_resolved = False
_t0 = time.monotonic()


def enabled():
    """Is the log switched on? Resolved once, from the environment."""
    global _resolved, _path
    if not _resolved:
        _resolved = True
        _path = (os.environ.get(_ENV) or "").strip() or None
    return _path is not None


def path():
    return _path if enabled() else None


def reset_for_test(new_path=None):
    """Point the log somewhere else (or switch it off). Tests only."""
    global _handle, _path, _resolved
    with _lock:
        if _handle is not None:
            try:
                _handle.close()
            except Exception:                               # noqa: BLE001
                pass
        _handle = None
        _path = new_path
        _resolved = True


def fingerprint(array):
    """A cheap identity for an array: never a scan.

    Four corners and the size. Enough to say "this is the same array I saw
    before" or "this is a different one" in a log a person reads, without
    touching a whole-slide buffer.
    """
    try:
        if array is None:
            return ""
        shape = getattr(array, "shape", None)
        if not shape:
            return ""
        if len(shape) < 2 or min(shape[0], shape[1]) == 0:
            return f"size={getattr(array, 'size', '?')}"
        h, w = int(shape[0]), int(shape[1])
        corners = (array[0, 0], array[0, w - 1],
                   array[h - 1, 0], array[h - 1, w - 1])
        return "%s/%s" % (
            getattr(array, "size", "?"),
            ",".join(f"{float(np_scalar):.4g}" for np_scalar in corners))
    except Exception:                                       # noqa: BLE001
        return "?"


def note(event, **fields):
    """One line, one low-frequency event. A no-op while the log is off."""
    if not enabled():
        return
    global _handle
    parts = [f"{(time.monotonic() - _t0) * 1000.0:9.1f}ms", event]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    line = " ".join(str(p) for p in parts)
    with _lock:
        try:
            if _handle is None:
                _handle = open(_path, "a", encoding="utf-8")
            _handle.write(line + "\n")
            _handle.flush()
        except Exception:                                   # noqa: BLE001
            # A log that cannot be written must never take the application
            # down with it.
            pass


def image_note(event, image, **fields):
    """An image record that says WHICH KIND of picture it is.

    The navigator draws two different things into the same item: its own
    plain 2-D overview array, and the composed 3-D RGB frame the coordinator
    publishes. A user looking at "a picture" cannot tell them apart, and a
    composed frame that is later overwritten by a plain overview looks
    exactly like a frame that never arrived.
    """
    try:
        shape = tuple(getattr(image, "shape", ()) or ())
    except Exception:                                       # noqa: BLE001
        shape = ()
    ndim = len(shape)
    kind = ("none" if image is None else
            "channel_rgb" if (ndim == 3 and shape[-1] in (3, 4)) else
            "overview_arr" if ndim == 2 else f"ndim{ndim}")
    note(event, kind=kind, ndim=ndim, shape=shape,
         dtype=str(getattr(image, "dtype", "")) or None,
         fingerprint=fingerprint(image), **fields)
