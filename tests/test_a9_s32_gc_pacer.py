"""Block A9 §32: full garbage collections wait for idle input."""

import gc

import pytest

pytest.importorskip("PyQt5")
from PyQt5 import QtCore, QtWidgets  # noqa: E402

from block01.utils import gc_pacer  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_automatic_full_collection_is_off_and_restored(app):
    before = gc.get_threshold()
    pacer = gc_pacer.GcPacer(app, clock=_Clock())
    try:
        assert gc.get_threshold()[:2] == before[:2]
        assert gc.get_threshold()[2] == gc_pacer.OFF
    finally:
        pacer.stop()
    assert gc.get_threshold() == before


def test_a_full_collection_waits_for_idle_input(app, monkeypatch):
    clock = _Clock()
    pacer = gc_pacer.GcPacer(app, clock=clock)
    pacer.last_thorough = clock.t                    # (the thorough pass is tested below)
    calls = []
    monkeypatch.setattr(gc, "collect", lambda generation=2: calls.append(generation) or 0)
    try:
        clock.t += gc_pacer.MIN_GAP_S + 1
        pacer.last_input = clock.t - 0.1                  # the user is busy
        pacer.tick()
        assert calls == []
        clock.t += gc_pacer.IDLE_S                         # ...and stops
        pacer.tick()
        assert calls == [2]
        pacer.tick()                                       # not again so soon
        assert calls == [2]
    finally:
        pacer.stop()


def test_an_input_event_counts_as_activity(app):
    clock = _Clock()
    pacer = gc_pacer.GcPacer(app, clock=clock)
    try:
        clock.t += 50
        event = QtCore.QEvent(QtCore.QEvent.KeyPress)
        pacer.eventFilter(None, event)
        assert pacer.last_input == clock.t
    finally:
        pacer.stop()


def test_a_session_that_never_idles_still_collects(app, monkeypatch):
    clock = _Clock()
    pacer = gc_pacer.GcPacer(app, clock=clock)
    pacer.last_thorough = clock.t
    calls = []
    monkeypatch.setattr(gc, "collect", lambda generation=2: calls.append(generation) or 0)
    try:
        clock.t += gc_pacer.MAX_GAP_S + 1
        pacer.last_input = clock.t
        pacer.tick()
        assert calls == [2]
    finally:
        pacer.stop()


def test_survivors_are_frozen_and_a_thorough_pass_unfreezes_first(app, monkeypatch):
    clock = _Clock()
    pacer = gc_pacer.GcPacer(app, clock=clock)
    log = []
    monkeypatch.setattr(gc, "collect", lambda generation=2: log.append("collect") or 0)
    monkeypatch.setattr(gc, "freeze", lambda: log.append("freeze"))
    monkeypatch.setattr(gc, "unfreeze", lambda: log.append("unfreeze"))
    try:
        clock.t += gc_pacer.THOROUGH_IDLE_S + 1           # idle long enough, never thorough
        pacer.last_input = clock.t - gc_pacer.THOROUGH_IDLE_S
        pacer.tick()
        assert log == ["unfreeze", "collect", "freeze"]
        log.clear()
        clock.t += gc_pacer.MIN_GAP_S + 1                 # an ordinary idle pass
        pacer.last_input = clock.t - gc_pacer.IDLE_S
        pacer.tick()
        assert log == ["collect", "freeze"]
    finally:
        monkeypatch.undo()
        pacer.stop()


def test_a_never_idle_session_still_gets_a_thorough_pass(app, monkeypatch):
    clock = _Clock()
    pacer = gc_pacer.GcPacer(app, clock=clock)
    log = []
    monkeypatch.setattr(gc, "collect", lambda generation=2: log.append("collect") or 0)
    monkeypatch.setattr(gc, "freeze", lambda: log.append("freeze"))
    monkeypatch.setattr(gc, "unfreeze", lambda: log.append("unfreeze"))
    try:
        clock.t += 2 * gc_pacer.THOROUGH_GAP_S + 1
        pacer.last_input = clock.t                         # busy, right now
        pacer.tick()
        assert log == ["unfreeze", "collect", "freeze"]
    finally:
        monkeypatch.undo()
        pacer.stop()


def test_a_cycle_frozen_by_one_pass_is_reclaimed_by_the_thorough_one(app):
    import weakref

    class Node:
        pass
    pacer = gc_pacer.GcPacer(app, clock=_Clock())
    try:
        a, b = Node(), Node()
        a.other, b.other = b, a
        alive = weakref.ref(a)
        pacer.collect()                       # survivors (the cycle) frozen
        del a, b
        pacer.collect()                       # ordinary pass: frozen, kept
        assert alive() is not None
        pacer.collect(thorough=True)          # unfreeze first: reclaimed
        assert alive() is None
    finally:
        pacer.stop()
