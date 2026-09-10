"""The interactive timeline's instrument: what it records, and what it costs
when nobody asked for it.

Three complaints -- Intensity lagging the mouse, drawing two patches freezing
the window, the first seconds of Step1 stuttering -- are all "the GUI stopped
answering" from the outside. What separates them is which callback held the
thread and for how long, which cannot be reasoned out and has to be timed on
the machine that has the problem. This module pins the instrument that does
the timing, so a later fix can be argued from measurements rather than from
plausibility.

Own module: no page-heavy Qt fixtures, so it runs anywhere.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.utils import perf_trace  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(perf_trace.PERF_ENV, raising=False)
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    monkeypatch.setattr(perf_trace, "_LOG_FILE", None)


def _lines(err):
    return [ln for ln in err.splitlines() if ln.startswith("PERF ")]


# ── off by default, and then genuinely off ───────────────────────────────

def test_nothing_is_traced_unless_the_switch_is_on(capsys):
    perf_trace.mark("some.event", n=1)
    with perf_trace.span("some.region", n=1):
        pass

    assert _lines(capsys.readouterr().err) == []


def test_a_span_with_the_switch_off_takes_no_clock(capsys):
    """It is the same object every time, with no timestamps in it: a traced
    region that costs an allocation and two clock reads per call would be paid
    for on every frame of a program nobody is tracing."""
    first = perf_trace.span("a")
    second = perf_trace.span("b")

    assert first is second is perf_trace._NOTHING
    assert not hasattr(first, "_t0")
    with first as handle:
        handle.add(anything=1)          # accepted and dropped
    assert _lines(capsys.readouterr().err) == []


def test_the_switch_is_read_per_call_not_at_import(capsys, monkeypatch):
    """So one run can be traced without a code change, and a test can turn it
    on for three lines."""
    perf_trace.mark("before")
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    perf_trace.mark("during")
    monkeypatch.setenv(perf_trace.PERF_ENV, "0")
    perf_trace.mark("after")

    events = [ln.split(" ev=")[1].split(" ")[0]
              for ln in _lines(capsys.readouterr().err)]
    assert events == ["during"]


# ── what a line says ─────────────────────────────────────────────────────

def test_a_mark_carries_the_clock_and_its_fields(capsys, monkeypatch):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    perf_trace.mark("intensity.in", channel="CD3", rev=7)
    line = _lines(capsys.readouterr().err)[0]

    assert " ev=intensity.in " in line
    assert " channel=CD3 " in line
    assert line.endswith(" rev=7")
    stamp = float(line.split(" t=")[1].split(" ")[0])
    assert stamp > 0.0


def test_a_span_reports_what_the_region_cost(capsys, monkeypatch):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    with perf_trace.span("step1.frame", mode="overlay") as handle:
        handle.add(channels=3)
    line = _lines(capsys.readouterr().err)[0]

    assert " ev=step1.frame " in line
    assert " mode=overlay" in line
    assert " channels=3" in line
    assert float(line.split(" dur_ms=")[1].split(" ")[0]) >= 0.0


def test_a_raising_region_still_reports_and_does_not_swallow(capsys,
                                                             monkeypatch):
    """A diagnostic that hides an exception is worse than no diagnostic, and
    one that loses the span it was timing tells you nothing about the frame
    that broke."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    with pytest.raises(ValueError):
        with perf_trace.span("step1.frame"):
            raise ValueError("boom")
    line = _lines(capsys.readouterr().err)[0]

    assert " raised=ValueError" in line


def test_the_timeline_shares_its_clock_with_the_middle_drag_log(capsys,
                                                                monkeypatch):
    """The two diagnostics have to be readable side by side: the middle-drag
    lines say when an event arrived, these say what the thread was doing."""
    from block01.ui.step0 import overview_panel as ovp

    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(ovp.MID_PAN_DEBUG_ENV, "1")
    perf_trace.mark("first")
    ovp._mid_pan_log_sink("MIDPAN t=%.6f gid=1 what=probe" % __import__(
        "time").monotonic())
    perf_trace.mark("second")

    err = capsys.readouterr().err
    perf_stamps = [float(ln.split(" t=")[1].split(" ")[0])
                   for ln in _lines(err)]
    midpan = [float(ln.split(" t=")[1].split(" ")[0])
              for ln in err.splitlines() if ln.startswith("MIDPAN ")]
    assert perf_stamps[0] <= midpan[0] <= perf_stamps[1], \
        "the two logs are not on one clock, so their lines cannot be ordered"


# ── revisions: a frame can be compared with the input it came from ───────

def test_revisions_count_inputs_per_kind():
    revs = perf_trace.Revisions()

    assert revs.latest("display_mapping") == 0
    assert revs.bump("display_mapping") == 1
    assert revs.bump("display_mapping") == 2
    assert revs.bump("other") == 1
    assert revs.latest("display_mapping") == 2
    assert revs.latest("other") == 1


# ── the heartbeat: a hole in the log with a number on it ─────────────────

def test_no_heartbeat_exists_with_the_switch_off():
    assert perf_trace.start_heartbeat() is None


def test_the_heartbeat_reports_a_gap_longer_than_its_interval(capsys,
                                                              monkeypatch):
    from PyQt5 import QtWidgets

    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    beat = perf_trace.Heartbeat(gap_ms=0.0, label="test")
    try:
        beat.start()
        assert beat.is_active()
        beat._tick()
        line = [ln for ln in _lines(capsys.readouterr().err)
                if " ev=gui.gap " in ln][0]
        assert float(line.split(" gap_ms=")[1].split(" ")[0]) >= 0.0
        assert " where=test" in line
    finally:
        beat.stop()
        assert not beat.is_active()
    del app


def test_a_heartbeat_within_its_interval_says_nothing(capsys, monkeypatch):
    from PyQt5 import QtWidgets

    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    beat = perf_trace.Heartbeat(gap_ms=10_000.0)
    try:
        beat.start()
        beat._tick()
        assert [ln for ln in _lines(capsys.readouterr().err)
                if " ev=gui.gap " in ln] == []
    finally:
        beat.stop()
    del app


# ── the file sink: the run is driven by whoever has the mouse ────────────

def test_the_timeline_can_be_collected_from_a_file(capsys, monkeypatch,
                                                   tmp_path):
    path = tmp_path / "perf.log"
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(path))
    perf_trace.mark("intensity.in", rev=1)
    with perf_trace.span("step1.frame"):
        pass

    on_stderr = _lines(capsys.readouterr().err)
    in_file = _lines(path.read_text(encoding="utf-8"))
    assert in_file == on_stderr
    assert len(in_file) == 2


def test_an_unwritable_file_does_not_break_the_traced_code(capsys, monkeypatch,
                                                           tmp_path):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "no" / "x.log"))
    ran = []
    with perf_trace.span("step1.frame"):
        ran.append(1)

    assert ran == [1]
    err = capsys.readouterr().err
    assert "log-file-failed" in err
    assert _lines(err), "and the line still reached stderr"


# ── the instrumented paths: traced when asked, silent otherwise ──────────

def test_a_step1_frame_is_traced_end_to_end(capsys, monkeypatch):
    """The three numbers the Intensity complaint needs, from one redraw: what
    the frame cost, what the remap inside it cost, and which input revision
    it published."""
    from PyQt5 import QtWidgets
    import numpy as np

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_step1_channel_state_contract import _window

    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        QtWidgets.QApplication.instance().processEvents()
        capsys.readouterr()

        monkeypatch.setenv(perf_trace.PERF_ENV, "1")
        w._overlay_display_cache.clear()
        w._on_display_mapping_changed("CD3")
        w._apply_pending_preview_update()
        err = capsys.readouterr().err
    finally:
        w.close()
        del np

    events = [ln.split(" ev=")[1].split(" ")[0] for ln in _lines(err)]
    assert "step1.mapping_in" in events
    assert "step1.schedule" in events
    assert "step1.publish" in events
    assert "step1.frame" in events
    assert "step1.remap" in events, \
        "the per-channel remap the Intensity edit invalidates is not timed"
    published = [ln for ln in _lines(err) if " ev=step1.publish " in ln][0]
    assert " rev=" in published, \
        "a published frame has to name the input revision it drew"


def test_the_instrumented_paths_are_silent_with_the_switch_off(capsys):
    from PyQt5 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_step1_channel_state_contract import _window

    w = _window(app)
    try:
        w.config.set_channel_visible("CD3", True)
        w._on_display_mapping_changed("CD3")
        w._apply_pending_preview_update()
        assert w._perf_heartbeat is None, \
            "a 5 ms timer in a program nobody is tracing"
        assert _lines(capsys.readouterr().err) == []
    finally:
        w.close()
