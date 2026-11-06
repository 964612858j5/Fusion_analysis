"""`QtWidgets` of the binding named in `block01.qt` (see there)."""

from PySide6.QtWidgets import *  # noqa: F401,F403

# Qt 6 bindings do not export the QWIDGETSIZE_MAX macro (probed on PySide6
# 6.11.3: neither QtWidgets nor QtCore has it); it is QWidget's own maximum.
QWIDGETSIZE_MAX = globals().get("QWIDGETSIZE_MAX") or 16777215

# Qt 6 moved these to QtOpenGLWidgets / QtGui; the entry point resolves them
# once, and re-exporting them here keeps every `QtWidgets.X` call site valid
# on both bindings.
from . import (  # noqa: F401,E402
    QAction,
    QActionGroup,
    QOpenGLWidget,
    QShortcut,
)
