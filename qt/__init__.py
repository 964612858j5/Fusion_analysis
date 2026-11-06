"""The ONE place that names a Qt binding (docs/v17_pyside6_plan.md §4).

Everything else imports Qt from here -- `from ..qt import QtCore, QtWidgets`,
`from block01.qt.QtCore import Qt, Signal` -- and a test forbids naming the
binding anywhere else. The submodules re-export the binding's module of the
same name and absorb the Qt 5 → Qt 6 moves, so callers keep one spelling:

* `QtCore.Signal` / `QtCore.Slot` (PyQt5's pyqtSignal / pyqtSlot);
* `is_deleted(obj)`: the C++ object is gone (sip.isdeleted on PyQt5);
* `delete_cpp(obj)`, `keep_wrappers_at_exit()`: sip's delete / setdestroyonexit;
* `flag_value(flag)`: a Qt enum/flag as an int (Qt 6 refuses `int()` on flags);
* `is_checked(state)`: a check state as a bool (a Qt 6 enum does not compare
  equal to the `int` that `stateChanged` delivers -- probed);
* `thread_finished(worker)`: the REAL `QThread.finished`, past a subclass
  signal that shadows it;
* `QOpenGLWidget`, `QOpenGLFramebufferObject(+Format)`, `QShortcut`,
  `QActionGroup`, `QAction`: Qt 6 moved these between modules; they are
  resolved here and re-exported by the two shims, so call sites keep the
  spelling they already had;
* `image_bytes(image)`: a QImage's pixel bytes.

There is no switch (user ruling 2026-11-04): this file is changed once, when
the binding changes, and git is the way back.
"""

BINDING = "PySide6"

# Before pyqtgraph is imported anywhere, so it binds to the same Qt.
import os as _os

_os.environ.setdefault("PYQTGRAPH_QT_LIB", BINDING)

import shiboken6 as _shiboken  # noqa: E402
from PySide6 import QtCore as _QtCore  # noqa: E402
from PySide6.QtCore import Qt as _Qt  # noqa: E402

# Qt 5 keeps these in QtWidgets/QtGui; Qt 6 moved them to QtOpenGLWidgets,
# QtOpenGL and QtGui. One resolution, below, is all any caller needs.
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QActionGroup,
    QShortcut,
)
from PySide6.QtOpenGL import (  # noqa: E402
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402


def is_deleted(obj) -> bool:
    """True when `obj`'s C++ side has been destroyed (shiboken6.isValid)."""
    return not _shiboken.isValid(obj)


def keep_wrappers_at_exit() -> None:
    """Kept as the one call site for "do not destroy wrappers at exit"
    (utils/perf_dispatch.py). PyQt5 needed sip.setdestroyonexit(False);
    PySide6 does not destroy the wrappers from Python at interpreter exit,
    so there is nothing to switch off and this is a deliberate no-op."""
    return None


def flag_value(flag) -> int:
    """A Qt enum or flag combination as a plain int (modifiers, buttons).

    PyQt5's enums are ints already; Qt 6 bindings' are Python enums, whose
    `int()` is refused for flags -- `.value` is the spelling both accept."""
    if flag is None:
        return 0
    return int(getattr(flag, "value", flag))


def is_checked(state) -> bool:
    """A check state as a bool, whether it arrives as an int or an enum.

    `QCheckBox.stateChanged` delivers an `int`. PyQt5's `Qt.Checked` is that
    same int, so `state == Qt.Checked` used to work; a Qt 6 binding's
    `Qt.Checked` is an enum that does NOT compare equal to the int (probed:
    `Qt.Checked == 2` is False), while `checkState()` returns the enum. Going
    through `flag_value` accepts both, which is what every call site needs.
    """
    return flag_value(state) == flag_value(_Qt.Checked)


def thread_finished(worker):
    """The thread's own `finished`, the one that fires when `run()` returns.

    It used to be shadowed by a business signal of the same name, which a
    Qt 6 binding CANNOT reach past (descriptor lookup returns the subclass's
    signal -- probed on PySide6 6.11.3). The workers therefore expose their
    payload as `job_finished`, and `worker.finished` is the physical signal
    again; this helper is the one place that names the intent."""
    return worker.finished


def image_bytes(image) -> bytes:
    """A QImage's pixel bytes, read-only copy (`bytesPerLine() * height()`)."""
    ptr = image.constBits()
    if hasattr(ptr, "setsize"):                     # PyQt5: a sized-less voidptr
        ptr.setsize(image.sizeInBytes())
    return bytes(ptr)


def delete_cpp(obj) -> None:
    """Destroy `obj`'s C++ side now (tests: "the page is gone")."""
    _shiboken.delete(obj)
