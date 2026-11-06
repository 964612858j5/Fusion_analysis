"""`QtGui` of the binding named in `block01.qt` (see there)."""

from PySide6.QtGui import *  # noqa: F401,F403

# Qt 6 moved these to QtOpenGL / QtGui; resolved once in the entry point and
# re-exported here so `QtGui.X` call sites work on both bindings.
from . import (  # noqa: F401,E402
    QAction,
    QActionGroup,
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
    QShortcut,
)
