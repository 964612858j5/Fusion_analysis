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
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from block01.utils import perf_trace  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with the switch off, no file named, and no writer --
    the state a program that nobody is tracing is in."""
    monkeypatch.delenv(perf_trace.PERF_ENV, raising=False)
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    monkeypatch.delenv(perf_trace.PERF_STDERR_ENV, raising=False)
    monkeypatch.delenv(perf_trace.PERF_QUEUE_ENV, raising=False)
    perf_trace.shutdown(0.0)
    perf_trace.WRITER._queue.clear()
    perf_trace.WRITER._dropped = 0
    perf_trace.WRITER._reported_drops = 0
    perf_trace.WRITER._path = None
    # `final` is the program ending; a test that exercises it must not leave
    # the sink closed for the next one.
    perf_trace.WRITER._final = False
    yield
    perf_trace.shutdown(0.0)
    perf_trace.WRITER._final = False


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
    _drain()

    events = [ln.split(" ev=")[1].split(" ")[0]
              for ln in _lines(capsys.readouterr().err)]
    assert events == ["during"]


# ── what a line says ─────────────────────────────────────────────────────

def test_a_mark_carries_the_clock_and_its_fields(capsys, monkeypatch):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    perf_trace.mark("intensity.in", channel="CD3", rev=7)
    _drain()
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
    _drain()
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
    _drain()
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
    _drain()

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
        _drain()
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
        _drain()
        assert [ln for ln in _lines(capsys.readouterr().err)
                if " ev=gui.gap " in ln] == []
    finally:
        beat.stop()
    del app


# ── the sink must not become the thing it measures ───────────────────────
#
# The first version printed each line to stderr AND wrote the file with a
# flush, on whichever thread produced it. A slider drag, a mouse move and a
# 5 ms heartbeat are high-frequency events, so that put terminal rendering and
# file I/O inside the callbacks being measured: it can inflate `gui.gap` and
# record the logging as the cost of the work. These pin the shape that fixes
# it -- bounded enqueue on the caller, one background writer, drops counted
# rather than waited for.

def _drain(timeout_s=2.0):
    """Wait for the writer to catch up, in the TEST -- never in the app."""
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline and perf_trace.WRITER.pending():
        _time.sleep(0.005)
    perf_trace.shutdown()


def test_the_calling_thread_never_writes_the_file(monkeypatch, tmp_path):
    """The producer's whole job is: read the clock, build a tuple, append it.
    Any file write it does itself is a disk in a mouse callback."""
    path = tmp_path / "perf.log"
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(path))
    monkeypatch.delenv(perf_trace.PERF_STDERR_ENV, raising=False)
    perf_trace.shutdown()

    writers = []
    real_open = open

    def spy_open(*a, **k):
        writers.append(threading.current_thread().name)
        return real_open(*a, **k)

    monkeypatch.setattr("builtins.open", spy_open)
    caller = threading.current_thread().name
    perf_trace.mark("intensity.in", rev=1)
    _drain()

    assert writers, "nothing opened the file at all"
    assert caller not in writers, \
        f"the producing thread opened the log itself: {writers}"
    assert all(name.startswith("perf-trace-writer") for name in writers), \
        writers


def test_a_slow_sink_does_not_block_the_producer(monkeypatch, tmp_path):
    """A writer stuck on a disk must cost the GUI an enqueue, not a wait."""
    import time as _time

    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "perf.log"))
    perf_trace.shutdown()
    slow = []

    def crawl(*_a, **_k):
        slow.append(1)
        _time.sleep(0.05)
        return 0

    monkeypatch.setattr(perf_trace.WRITER, "_drain", crawl)
    perf_trace.mark("warm")                     # starts the writer

    costs = []
    for i in range(200):
        t0 = _time.perf_counter()
        perf_trace.mark("intensity.in", rev=i)
        costs.append((_time.perf_counter() - t0) * 1000.0)
    perf_trace.WRITER.shutdown(0.0)

    costs.sort()
    p99 = costs[int(0.99 * len(costs))]
    assert p99 < 5.0, f"enqueue p99 was {p99:.2f} ms with a crawling writer"


def test_a_full_queue_drops_and_says_so(monkeypatch, tmp_path):
    """Bounded on purpose. What it must not do is grow without limit or make
    the caller wait; what it must not do EITHER is lose lines silently."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "perf.log"))
    monkeypatch.setenv(perf_trace.PERF_QUEUE_ENV, "10")
    perf_trace.shutdown()
    monkeypatch.setattr(perf_trace.WRITER, "_drain", lambda *a, **k: 0)

    for i in range(100):
        perf_trace.mark("flood", i=i)

    assert perf_trace.WRITER.pending() <= 10
    assert perf_trace.WRITER.dropped_total() >= 89
    monkeypatch.undo()
    # the drop is reported, not swallowed
    perf_trace.WRITER._drain()
    perf_trace.shutdown()


def test_concurrent_producers_leave_whole_lines_in_clock_order(monkeypatch,
                                                               tmp_path):
    """Workers and the GUI thread trace at once. A line may not be torn, and
    the file must be ordered by when things happened, not by which thread got
    the lock first."""
    path = tmp_path / "perf.log"
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(path))
    perf_trace.shutdown()

    def produce(tag):
        for i in range(200):
            perf_trace.mark("read.begin", job=tag, i=i)

    threads = [threading.Thread(target=produce, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _drain()

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.startswith("PERF ")]
    assert len(lines) == 800, f"{len(lines)} lines of 800"
    for ln in lines:
        assert ln.count(" ev=") == 1 and " job=" in ln and " i=" in ln, ln
    # Ordered within each batch the writer takes, and every line carries the
    # stamp its producer read -- so the file is near-ordered and the analyser,
    # which sorts by that stamp, is exact. What must not happen is a line
    # landing a whole batch out of place, which would mean the sort key was
    # taken at write time.
    stamps = [float(ln.split(" t=")[1].split(" ")[0]) for ln in lines]
    worst = max((prev - cur for prev, cur in zip(stamps, stamps[1:])),
                default=0.0)
    assert worst < 0.05, f"a line is {worst * 1000:.1f} ms out of order"
    assert sorted(stamps)[0] == min(stamps)


def test_the_timeline_can_be_collected_from_a_file(capsys, monkeypatch,
                                                   tmp_path):
    """The run is driven by whoever has the mouse and read by someone else --
    and naming a file turns stderr OFF, because a terminal is the expensive
    half."""
    path = tmp_path / "perf.log"
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(path))
    monkeypatch.delenv(perf_trace.PERF_STDERR_ENV, raising=False)
    perf_trace.shutdown()
    perf_trace.mark("intensity.in", rev=1)
    with perf_trace.span("step1.frame"):
        pass
    _drain()

    in_file = _lines(path.read_text(encoding="utf-8"))
    assert len(in_file) == 2
    assert _lines(capsys.readouterr().err) == [], \
        "a run being measured must not also pay for a terminal"


def test_stderr_is_an_explicit_choice(capsys, monkeypatch, tmp_path):
    path = tmp_path / "perf.log"
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(path))
    monkeypatch.setenv(perf_trace.PERF_STDERR_ENV, "1")
    perf_trace.shutdown()
    perf_trace.mark("intensity.in", rev=1)
    _drain()

    assert _lines(capsys.readouterr().err) == _lines(
        path.read_text(encoding="utf-8"))


def test_shutdown_is_bounded(monkeypatch, tmp_path):
    """Closing a window may not wait on a disk. Whatever is left after the
    deadline is lost, and that is the right trade."""
    import time as _time

    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "perf.log"))
    perf_trace.shutdown()
    monkeypatch.setattr(perf_trace.WRITER, "_drain",
                        lambda *a, **k: (_time.sleep(0.02), 1)[1])
    for i in range(500):
        perf_trace.mark("flood", i=i)

    t0 = _time.perf_counter()
    perf_trace.shutdown(0.1)
    assert (_time.perf_counter() - t0) < 1.0


def test_nothing_is_created_with_the_switch_off(monkeypatch, tmp_path):
    monkeypatch.delenv(perf_trace.PERF_ENV, raising=False)
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "perf.log"))
    perf_trace.shutdown()

    perf_trace.mark("nothing")
    with perf_trace.span("nothing"):
        pass

    assert not perf_trace.WRITER.is_running()
    assert perf_trace.WRITER.pending() == 0
    assert not (tmp_path / "perf.log").exists()


def test_an_unwritable_file_does_not_break_the_traced_code(capsys, monkeypatch,
                                                           tmp_path):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "no" / "x.log"))
    perf_trace.shutdown()
    ran = []
    with perf_trace.span("step1.frame"):
        ran.append(1)
    _drain()

    assert ran == [1]
    assert "log-file-failed" in capsys.readouterr().err


# ── background work: both ends, and the counts at each end ───────────────

def test_a_job_reports_both_ends_and_its_reads(capsys, monkeypatch):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    perf_trace.shutdown()

    with perf_trace.job("step1.preview", patch=0, channels=2) as jb:
        with jb.read(patch=0, channel="CD3"):
            pass
        with jb.read(patch=0, channel="CD8"):
            pass
    _drain()
    events = [ln.split(" ev=")[1].split(" ")[0]
              for ln in _lines(capsys.readouterr().err)]

    assert events == ["job.begin", "read.begin", "read.end", "read.begin",
                      "read.end", "job.end"]


@pytest.mark.parametrize("how", ["return", "raise", "cancel"])
def test_a_job_closes_on_every_exit_path(capsys, monkeypatch, how):
    """A timeline that cannot close a job counts it as reading forever, and
    then every later stall looks like disk contention."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    perf_trace.shutdown()

    def work():
        with perf_trace.job("step0.preload") as jb:
            if how == "raise":
                raise ValueError("read failed")
            if how == "cancel":
                jb.add(cancelled=1)
                return
            jb.add(reads=1)

    if how == "raise":
        with pytest.raises(ValueError):
            work()
    else:
        work()
    _drain()
    lines = _lines(capsys.readouterr().err)

    assert sum(1 for ln in lines if " ev=job.begin " in ln) == 1
    ends = [ln for ln in lines if " ev=job.end " in ln]
    assert len(ends) == 1, "the job never closed"
    if how == "raise":
        assert " raised=ValueError" in ends[0]
    if how == "cancel":
        assert " cancelled=1" in ends[0]


def test_the_active_counts_are_carried_on_each_line(capsys, monkeypatch):
    """So "what was reading when the GUI stalled" is answerable from the log
    rather than from a guess."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    perf_trace.shutdown()

    with perf_trace.job("step0.preload") as a:
        with perf_trace.job("step1.preview") as b:
            with a.read(channel="CD3"), b.read(channel="CD8"):
                pass
    _drain()
    lines = _lines(capsys.readouterr().err)

    deepest = [ln for ln in lines if " ev=read.begin " in ln][-1]
    assert " active_jobs=2 " in deepest
    assert " active_reads=2 " in deepest
    last_end = [ln for ln in lines if " ev=job.end " in ln][-1]
    assert " active_jobs=0 " in last_end
    assert last_end.endswith(" active_reads=0")


def test_a_read_cancelled_while_reading_is_named(capsys, monkeypatch):
    """The overlap that makes a second patch freeze the window: the old
    generation was told to stop and is still inside `read_region`."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    perf_trace.shutdown()

    with perf_trace.job("step0.preload") as jb:
        with jb.read(channel="CD3") as rd:
            rd.add(outcome="cancelled_after_read")
    _drain()

    end = [ln for ln in _lines(capsys.readouterr().err)
           if " ev=read.end " in ln][0]
    assert " outcome=cancelled_after_read" in end


def test_background_tracing_is_off_with_the_switch_off(capsys, monkeypatch):
    monkeypatch.delenv(perf_trace.PERF_ENV, raising=False)
    with perf_trace.job("step0.preload") as jb:
        with jb.read(channel="CD3"):
            pass

    assert _lines(capsys.readouterr().err) == []
    assert perf_trace.ACTIVES.snapshot() == (0, 0)


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
        _drain()
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
        _drain()
        assert w._perf_heartbeat is None, \
            "a 5 ms timer in a program nobody is tracing"
        assert _lines(capsys.readouterr().err) == []
    finally:
        w.close()


# ── one consumer, and it closes last ─────────────────────────────────────

def test_a_final_shutdown_accepts_nothing_and_starts_no_writer(monkeypatch,
                                                               tmp_path):
    """Teardown order: the loaders report their own `job.end`, and only then
    the sink closes. After that a late line is dropped rather than starting a
    second writer on a file the first one closed."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "perf.log"))
    perf_trace.mark("before", n=1)
    _drain()

    perf_trace.shutdown(final=True)
    perf_trace.mark("after", n=2)

    assert not perf_trace.WRITER.is_running()
    assert perf_trace.WRITER.pending() == 0
    text = (tmp_path / "perf.log").read_text(encoding="utf-8")
    assert " ev=before " in text
    assert " ev=after " not in text


def test_only_one_writer_ever_runs(monkeypatch, tmp_path):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.setenv(perf_trace.PERF_LOG_ENV, str(tmp_path / "perf.log"))
    perf_trace.mark("one")
    first = perf_trace.WRITER._thread
    for _ in range(20):
        perf_trace.mark("more")
    assert perf_trace.WRITER._thread is first

    names = [t.name for t in threading.enumerate()
             if t.name.startswith("perf-trace-writer")]
    assert len(names) == 1, names
    _drain()


def test_time_fields_keep_their_microseconds(monkeypatch, capsys):
    """`%.4g` on a monotonic reading of 759147.127727 writes 7.591e+05 and
    loses tens of seconds; the first real run lost every span's start that
    way."""
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    perf_trace.mark("probe", t_begin=759147.127727, dur_ms=7493.66,
                    count=29)
    _drain()
    line = _lines(capsys.readouterr().err)[0]

    assert " t_begin=759147.127727" in line
    assert " count=29" in line


def test_a_span_start_survives_the_formatter(monkeypatch, capsys):
    monkeypatch.setenv(perf_trace.PERF_ENV, "1")
    monkeypatch.delenv(perf_trace.PERF_LOG_ENV, raising=False)
    with perf_trace.span("step1.frame"):
        pass
    _drain()
    line = _lines(capsys.readouterr().err)[0]

    begin = float(line.split(" t_begin=")[1].split(" ")[0])
    end = float(line.split(" t=")[1].split(" ")[0])
    dur = float(line.split(" dur_ms=")[1].split(" ")[0])
    assert end - begin == pytest.approx(dur / 1000.0, abs=1e-4), \
        f"the span's own start does not match its duration: {line}"
