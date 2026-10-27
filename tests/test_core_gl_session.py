"""`CoreGlSession` (docs/v17_step0_engine_plan.md §10.2): reading a paint
report, the notice order (drain -> ack -> callback) and the end of life.
The GL paths are the Step1 / Step3 / Regions engine tests' (real GL)."""

import socket

from PyQt5 import QtWidgets

from block01.ui.core_gl_session import FRAME_MS, CoreGlSession


class _Core:
    def __init__(self):
        self.calls = []

    def ack(self):
        self.calls.append("ack")

    def close(self):
        self.calls.append("close")

    def stats(self):
        return {"reads": 3}


def _session(core, sockets=None, on_notice=lambda: None):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return CoreGlSession(core, sockets, owner=None, on_notice=on_notice, tag=lambda: "t",
                         channel_name=str)


def test_failures_and_follow_ups_are_read_from_the_report():
    assert CoreGlSession.failure_of({"error": "boom"}) == "paint failed: boom"
    assert CoreGlSession.failure_of({"read_failure": "gone"}) == "reads failed: gone"
    assert CoreGlSession.failure_of({"capacity_exceeded": True}) == ""
    assert CoreGlSession.again_ms({"deferred": 2}) == FRAME_MS
    assert CoreGlSession.again_ms({"reserving": True}) == FRAME_MS
    assert CoreGlSession.again_ms({"retry_ms": 199.6}) == 200
    assert CoreGlSession.again_ms({}) is None


def test_a_notice_is_drained_then_acknowledged_then_handed_on():
    core = _Core()
    read_end, write_end = socket.socketpair()
    seen = []
    s = _session(core, (read_end, write_end), on_notice=lambda: seen.append(list(core.calls)))
    write_end.send(b"xxxx")
    s._on_notice()
    assert seen == [["ack"]]
    read_end.setblocking(False)
    try:
        assert read_end.recv(16) == b""                # drained before the ack
    except BlockingIOError:
        pass
    s.close()
    s.close_sockets()
    assert core.calls[-1] == "close"
    assert s._sockets is None


def test_reads_are_counted_once():
    s = _session(_Core())
    s.note_reads()
    s.note_reads()
    assert s._reads_seen == 3
