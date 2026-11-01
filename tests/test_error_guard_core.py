"""The engine's errors, a Rust panic included (docs/v17_error_guard_plan.md
§2), offscreen and without GL:

  * `CORE_ERRORS` holds the wheel's `PanicException` (a BaseException), and
    is `(Exception,)` with a wheel that lacks it;
  * the shared paint boundary turns a raised paint -- a panic too -- into
    the frame's error report, so `failure_of` routes it to the fallback;
  * the reporting calls after a paint (`stats`, `drain_growths`) that raise
    are skipped and said once.

The real mounts' fallbacks are tested on GL: `test_step0_engine_mount.py`
(attach, build, paint) and `test_v16_5_r3_fallback.py` (Step1 build, paint,
reader panics); label reader panics in `test_v16_5_r3b_labels.py`.
"""

import sys
import types

import pytest

fvc = pytest.importorskip("fusion_viewer_core")

from block01.ui import step1_core_adapter as adapter  # noqa: E402
from block01.ui.core_gl_session import CoreGlSession  # noqa: E402


def test_core_errors_hold_the_panic_type_and_an_old_wheel_falls_back(monkeypatch):
    assert issubclass(fvc.PanicException, BaseException)
    assert not issubclass(fvc.PanicException, Exception)
    assert adapter.CORE_ERRORS == (Exception, fvc.PanicException)
    monkeypatch.setitem(sys.modules, "fusion_viewer_core", types.ModuleType("fusion_viewer_core"))
    assert adapter._core_errors() == (Exception,)


class _Core:
    def __init__(self, paint=None, stats=None, growths=None):
        self._paint, self._stats, self._growths = paint, stats, growths

    def paint(self, *a):
        if isinstance(self._paint, BaseException):
            raise self._paint
        return self._paint

    def stats(self):
        if isinstance(self._stats, BaseException):
            raise self._stats
        return self._stats

    def drain_growths(self):
        if isinstance(self._growths, BaseException):
            raise self._growths
        return self._growths


def _session(core):
    session = CoreGlSession.__new__(CoreGlSession)     # no sockets, no GL
    session.core = core
    session._reads_seen = 0
    return session


@pytest.mark.parametrize("error", [fvc.PanicException("paint broke"), RuntimeError("paint broke")])
def test_a_raised_paint_is_the_frames_failure(error):
    report = _session(_Core(paint=error)).paint(0, 10, 10, None)
    assert report == {"error": f"{type(error).__name__}: paint broke"}
    assert CoreGlSession.failure_of(report) == f"paint failed: {type(error).__name__}: paint broke"


def test_a_normal_paint_is_unchanged():
    assert _session(_Core(paint={"frame": 3})).paint(0, 10, 10, None) == {"frame": 3}


def test_reporting_calls_that_raise_are_skipped_and_said_once(capsys):
    session = _session(_Core(stats=fvc.PanicException("stats broke")))
    session.note_reads()
    session.note_reads()
    out = capsys.readouterr().out
    assert out.count("stats() failed and is skipped: PanicException: stats broke") == 1
    assert session._reads_seen == 0


def test_drain_growths_that_raises_is_skipped(capsys):
    session = _session(_Core(growths=fvc.PanicException("growths broke")))
    session.frame = 0
    session._pressure_pending = False
    session.note_frame({"channels": 1, "shown": 1, "wanted": 1}, expected=1)
    assert "drain_growths() failed and is skipped" in capsys.readouterr().out
    assert session._pressure_pending is False and session.frame == 1
