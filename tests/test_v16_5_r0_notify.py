"""v16_5 R0: the "please redraw" notice from Rust to the GUI thread.

A socket pair Python owns; Rust borrows the write end. Notices coalesce
(one byte while one is pending), never block, are never lost when the GUI
side follows drain -> ack -> work, and stop for good at `close()`.
No display needed: QCoreApplication + QSocketNotifier.
"""

import socket
import threading
import time

import pytest

core = pytest.importorskip("fusion_viewer_core")
from PyQt5 import QtCore  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])


class Gui:
    """The GUI side, as plan §5.1/§6 prescribe."""

    def __init__(self):
        self.r, self.w = socket.socketpair()
        self.r.setblocking(False)
        self.notifier = core.Notifier(self.w.fileno())
        self.wakeups = 0
        self.work = []
        self.done = []
        self.sn = QtCore.QSocketNotifier(self.r.fileno(), QtCore.QSocketNotifier.Read)
        self.sn.activated.connect(self.on_readable)

    def on_readable(self):
        try:                                             # 1. drain until it would block
            while self.r.recv(4096):
                pass
        except BlockingIOError:
            pass
        self.notifier.ack()                              # 2. then ack
        self.wakeups += 1
        while self.work:                                 # 3. then pull the work
            self.done.append(self.work.pop())

    def close(self):
        self.notifier.close()
        self.sn.setEnabled(False)
        self.r.close()
        self.w.close()


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QtCore.QEventLoop.AllEvents, 10)


def test_a_a_storm_of_notices_writes_at_most_one_byte_per_wakeup(app):
    gui = Gui()
    gui.notifier.storm(4, 2000, 20)
    pump(app, 1.5)
    writes, coalesced = gui.notifier.counts()
    assert writes + coalesced == 8000
    # one byte at most per ack, and every wake-up drained at least one;
    # how many notices share a byte depends on how fast the GUI turns
    assert 0 < gui.wakeups <= writes <= gui.wakeups + 1
    assert coalesced > 0
    gui.close()


def test_b_no_notice_is_lost(app):
    """Producers add work then notify; the GUI never polls. Every item must
    be taken, the last one included."""
    gui = Gui()
    items = 20000

    def produce(k):
        for i in range(items // 4):
            gui.work.append((k, i))
            gui.notifier.notify()

    threads = [threading.Thread(target=produce, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 10
    while (any(t.is_alive() for t in threads) or len(gui.done) < items) and time.monotonic() < deadline:
        app.processEvents(QtCore.QEventLoop.AllEvents, 10)
    assert len(gui.done) == items
    gui.close()


def test_b2_the_interleaving_found_in_review_loses_nothing(app):
    """Review case, step by step on one thread: A notifies, the GUI drains
    and acks, B notifies and the GUI pulls B's work, C notifies. C's notice
    must still be visible (an unread byte), not swallowed."""
    import select
    r, w = socket.socketpair()
    r.setblocking(False)
    n = core.Notifier(w.fileno())

    def drain():
        try:
            while r.recv(4096):
                pass
        except BlockingIOError:
            pass

    n.notify()                                           # A
    drain()
    n.ack()
    n.notify()                                           # B: writes (pending was clear)
    # ... the GUI pulls the work here, B's included ...
    n.notify()                                           # C: coalesced with B
    readable, _, _ = select.select([r], [], [], 0)
    assert readable, "C's notice was lost"
    drain()
    n.ack()
    # the old order (ack, THEN drain) on the same steps does lose C:
    n.notify()                                           # A
    n.ack()
    n.notify()                                           # B writes after the ack ...
    drain()                                              # ... and the drain swallows it
    n.notify()                                           # C: "pending" -> no byte
    assert not select.select([r], [], [], 0)[0]
    n.close()
    r.close()
    w.close()


def test_c_without_an_ack_only_one_byte_is_ever_written(app):
    r, w = socket.socketpair()
    n = core.Notifier(w.fileno())
    for _ in range(100000):
        n.notify()
    assert n.counts() == (1, 99999)
    assert r.recv(10) == b"\x01"
    n.close()
    r.close()
    w.close()


def test_d_nothing_is_written_after_close(app):
    gui = Gui()
    gui.notifier.storm(2, 100000, 1)                     # still running at close
    pump(app, 0.2)
    gui.notifier.close()
    before = gui.notifier.counts()[0]
    gui.notifier.ack()
    gui.notifier.notify()
    time.sleep(0.05)
    assert gui.notifier.counts()[0] == before
    gui.sn.setEnabled(False)
    gui.r.close()
    gui.w.close()                                        # the storm keeps calling notify: harmless
    time.sleep(0.05)
