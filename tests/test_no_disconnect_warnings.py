"""Backlog 23 (user ruling 2026-11-15, item 7): closing the app printed
PySide6's "libpyside: Failed to disconnect" RuntimeWarning -- every
`disconnect` of a slot that was not (or no longer) connected: the montage's
camera slot when it never had the GPU layer, Step1's core binding paused and
then disposed, the display's background readers' unconnected `failed`.
Each now disconnects only what it connected; any such warning here fails."""
import os
import warnings
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("block01.qt")

from block01.qt import QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _no_disconnect_warning(record):
    bad = [str(w.message) for w in record if "Failed to disconnect" in str(w.message)]
    assert not bad, bad


def test_a_window_opened_and_closed_warns_nothing(app):
    from block01.ui.main_window import MainWindow
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        w = MainWindow()
        w.show()
        app.processEvents()
        w._stop_montage_gpu()              # the montage never had its GPU layer
        w.close()
        app.processEvents()
    _no_disconnect_warning(record)


def test_the_core_binding_paused_then_disposed_warns_nothing(app):
    from test_step1_ahead import _binding
    b, _core, _names = _binding(app, 3)
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        b.pause()
        b.dispose()
        b.resume()                          # after dispose: nothing to (re)connect
    _no_disconnect_warning(record)


def test_a_paused_binding_comes_back_connected(app):
    from test_step1_ahead import _binding
    b, _core, _names = _binding(app, 3)
    b.pause()
    b.resume()
    seen = []
    b._set_view = lambda snapshot, input_: seen.append(input_)
    b.controller.gesture_quiet.emit(SimpleNamespace())
    assert seen == [False]                  # the controller reaches the binding again, once
