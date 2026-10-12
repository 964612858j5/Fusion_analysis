"""Threads told to stop but not waited for (R3 plan §2.5).

A viewer that closes -- or falls back from the Rust path mid-session --
signals its reader threads and moves on; nothing on the GUI thread joins
them. They are listed here, and the program's exit waits for all of them,
Python threads and the Rust core's readers alike, within ONE shared
deadline (`wait_all`), then leaves what is still running to the OS.
"""

import threading
import time
from typing import List

#: the program's exit waits this long in all
EXIT_WAIT_S = 1.0

_threads: List[threading.Thread] = []
_lock = threading.Lock()


def register(thread: threading.Thread) -> None:
    with _lock:
        _threads.append(thread)


def wait_all(timeout_s: float = EXIT_WAIT_S) -> dict:
    """Join every listed thread, then the Rust readers, until `timeout_s`
    has passed in all. Returns what is still running."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    with _lock:
        threads, _threads[:] = list(_threads), []
    left = []
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            left.append(thread.name)
    rust_left = 0
    try:
        import fusion_viewer_core
        rust_left = int(fusion_viewer_core.wait_closing(max(0.0, deadline - time.monotonic())))
    except (ImportError, AttributeError):
        pass
    with _lock:
        _threads.extend(t for t in threads if t.is_alive())
    return {"python_left": left, "rust_left": rust_left}
