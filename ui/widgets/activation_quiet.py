"""Item views that do not repaint when their window gains or loses focus.

Qt's item views repaint their whole viewport on every window activation
change (`QAbstractItemView::viewportEvent`), whether or not any colour
changes. The channel lists hold one widget row per channel (checkbox,
slider, spin box, labels), so clicking back into the main window from the
Tissue Preview or Intensity window repainted every row: about 250 widgets,
30-60 ms on the GUI thread with 20 channels, before the click was handled
(A9 application §51).

The repaint is skipped only while the view's palette is the same in the
active and inactive groups -- then it would draw the same pixels. A theme
that tells the two apart gets Qt's behaviour back.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

_ACTIVATION = (QtCore.QEvent.WindowActivate, QtCore.QEvent.WindowDeactivate)


class _QuietActivation(QtCore.QObject):
    def __init__(self, view):
        super().__init__(view)
        self._view = view

    def eventFilter(self, obj, event):  # noqa: N802
        if (event.type() not in _ACTIVATION
                or not self._view.palette().isEqual(QtGui.QPalette.Active, QtGui.QPalette.Inactive)):
            return False
        # Only the viewport's own repaint is skipped: the rows still get the
        # event, as `QWidget::event` would pass it on (a line edit, say,
        # drops its selection on activation when it has no focus).
        for child in obj.children():
            if child.isWidgetType() and child.isVisible() and not child.isWindow():
                QtWidgets.QApplication.sendEvent(child, event)
        return True


def quiet_activation(view):
    """Stop `view` (a QAbstractItemView) repainting on window activation
    changes that leave its colours unchanged."""
    view.viewport().installEventFilter(_QuietActivation(view))
