"""The GUI watchdog names the function the GUI thread is stuck in.

It exists for a freeze that could not be reproduced offscreen (the first
Save after start-up, reported from manual testing). The one thing it must
get right is the stack: a report without the GUI thread's frames is no
better than the desktop's "not responding" dialog.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5")

from PyQt5 import QtTest, QtWidgets  # noqa: E402

from block01.utils import gui_watchdog as gw  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _block_the_gui_thread_for(seconds):
    """A deliberately named frame the report has to contain."""
    time.sleep(seconds)


def _wait(predicate, ms=3000):
    for _ in range(ms // 10):
        if predicate():
            return True
        QtTest.QTest.qWait(10)
    return predicate()


def test_a_stall_is_reported_once_with_the_gui_threads_stack(app):
    reports = []
    dog = gw.GuiWatchdog(stall_seconds=0.3, beat_ms=20, check_ms=30,
                         report=reports.append).start()
    try:
        QtTest.QTest.qWait(100)                    # heartbeats flowing
        assert reports == []

        _block_the_gui_thread_for(0.8)             # no events processed

        assert _wait(lambda: len(reports) >= 2)
        assert len(reports) == 2, reports
        first, second = reports
        assert first.startswith("GUI event loop has not run for")
        assert "_block_the_gui_thread_for" in first, first
        assert "time.sleep" in first or "sleep(seconds)" in first
        assert second.startswith("GUI event loop responsive again after")
        assert dog.stalls and dog.stalls[0][0] >= 0.3
    finally:
        dog.stop()


def test_a_responsive_loop_reports_nothing(app):
    reports = []
    dog = gw.GuiWatchdog(stall_seconds=0.3, beat_ms=20, check_ms=30,
                         report=reports.append).start()
    try:
        QtTest.QTest.qWait(700)
        assert reports == []
    finally:
        dog.stop()


def test_two_stalls_are_two_reports_each(app):
    reports = []
    dog = gw.GuiWatchdog(stall_seconds=0.2, beat_ms=20, check_ms=30,
                         report=reports.append).start()
    try:
        _block_the_gui_thread_for(0.5)
        assert _wait(lambda: len(reports) >= 2)
        _block_the_gui_thread_for(0.5)
        assert _wait(lambda: len(reports) >= 4)
        assert len(reports) == 4
    finally:
        dog.stop()


def test_the_environment_variable_disables_it(app, monkeypatch):
    monkeypatch.setenv(gw.ENV_DISABLE, "0")
    assert gw.start_gui_watchdog() is None

    monkeypatch.setenv(gw.ENV_DISABLE, "1")
    dog = gw.start_gui_watchdog(stall_seconds=5)
    try:
        assert dog is not None
    finally:
        dog.stop()
