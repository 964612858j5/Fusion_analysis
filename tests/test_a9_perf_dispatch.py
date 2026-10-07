"""Block A9: the slow-dispatch tracer is on only with both switches, and when
on it attributes a slow event delivery to its receiver and event type.

Each case runs in its own process: a QApplication is one per process."""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROG = r"""
import time
from PyQt5 import QtCore
from block01.utils import perf_dispatch, perf_trace

app = perf_dispatch.make_application([])
print("CLASS", type(app).__name__)

class Slow(QtCore.QObject):
    def event(self, ev):
        if ev.type() == QtCore.QEvent.User:
            time.sleep(0.05)
            return True
        return super().event(ev)

slow = Slow()
slow.setObjectName("slowpoke")
QtCore.QCoreApplication.postEvent(slow, QtCore.QEvent(QtCore.QEvent.User))
QtCore.QTimer.singleShot(200, app.quit)
app.exec_()
perf_trace.shutdown(final=True)
"""


def _run(tmp_path, **env):
    log = tmp_path / "perf.log"
    full = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=_ROOT,
                BLOCK01_PERF_LOG=str(log))
    for key in ("BLOCK01_PERF", "BLOCK01_PERF_DISPATCH"):
        full.pop(key, None)
    full.update(env)
    out = subprocess.run([sys.executable, "-c", _PROG], env=full, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    text = log.read_text() if log.exists() else ""
    return out.stdout, text


def test_dispatch_tracer_records_the_slow_receiver(tmp_path):
    stdout, log = _run(tmp_path, BLOCK01_PERF="1", BLOCK01_PERF_DISPATCH="1")
    assert "CLASS TimedApplication" in stdout
    lines = [l for l in log.splitlines() if "ev=qt.dispatch" in l]
    assert any("recv=Slow:slowpoke" in l and "etype=User" in l for l in lines), lines


def test_off_without_its_own_switch(tmp_path):
    stdout, log = _run(tmp_path, BLOCK01_PERF="1")
    assert "CLASS QApplication" in stdout
    assert "ev=qt.dispatch" not in log
