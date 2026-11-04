"""The ONE place that names a Qt binding (docs/v17_pyside6_plan.md §4).

Everything else imports Qt from here -- `from ..qt import QtCore, QtWidgets`,
`from block01.qt.QtCore import Qt, Signal` -- and a test forbids naming the
binding anywhere else. The submodules re-export the binding's module of the
same name and absorb the Qt 5 → Qt 6 moves, so callers keep one spelling:

* `QtCore.Signal` / `QtCore.Slot` (PyQt5's pyqtSignal / pyqtSlot);
* `is_deleted(obj)`: the C++ object is gone (sip.isdeleted on PyQt5).
* `flag_value(flag)`: a Qt enum/flag as an int (Qt 6 refuses `int()` on flags);
* `image_bytes(image)`: a QImage's pixel bytes;
* `delete_cpp(obj)`, `keep_wrappers_at_exit()`: sip's delete / setdestroyonexit.

There is no switch (user ruling 2026-11-04): this file is changed once, when
the binding changes, and git is the way back.
"""

BINDING = "PyQt5"

# Before pyqtgraph is imported anywhere, so it binds to the same Qt.
import os as _os

_os.environ.setdefault("PYQTGRAPH_QT_LIB", BINDING)

from PyQt5 import sip as _sip  # noqa: E402


def is_deleted(obj) -> bool:
    """True when `obj`'s C++ side has been destroyed."""
    return _sip.isdeleted(obj)


def keep_wrappers_at_exit() -> None:
    """Interpreter exit leaves Qt objects to the OS instead of destroying
    them from Python (utils/perf_dispatch.py; sip.setdestroyonexit(False))."""
    _sip.setdestroyonexit(False)


def flag_value(flag) -> int:
    """A Qt enum or flag combination as a plain int (modifiers, buttons).

    PyQt5's enums are ints already; Qt 6 bindings' are Python enums, whose
    `int()` is refused for flags -- `.value` is the spelling both accept."""
    if flag is None:
        return 0
    return int(getattr(flag, "value", flag))


def image_bytes(image) -> bytes:
    """A QImage's pixel bytes, read-only copy (`bytesPerLine() * height()`)."""
    ptr = image.constBits()
    if hasattr(ptr, "setsize"):                     # PyQt5: a sized-less voidptr
        ptr.setsize(image.sizeInBytes())
    return bytes(ptr)


def delete_cpp(obj) -> None:
    """Destroy `obj`'s C++ side now (tests: "the page is gone")."""
    _sip.delete(obj)
