"""One monotonic timeline for the interactive paths, default off, and cheap
enough on the calling thread that it does not become the thing it measures.

Why a module rather than prints. Three complaints -- Intensity lagging the
mouse, drawing two patches freezing the window, the first seconds of Step1
stuttering -- all look the same from the outside: the GUI stops answering.
What separates them is WHICH callback was on the thread and for how long, and
that cannot be reasoned out. It has to be timed, on the machine that has the
problem, with one clock so the spans can be laid beside each other.

WHY THE WRITER IS A THREAD. The first version of this printed each line to
stderr and wrote it to the file with a flush, on whichever thread produced it.
A slider drag, a mouse move and a 5 ms heartbeat are high-frequency events, so
that put terminal rendering and file I/O inside the callbacks being measured:
it could inflate `gui.gap`, and it could record the logging as the cost of the
work. A measurement that changes what it measures is not evidence.

So the calling thread does the least that is still correct: read the clock,
build a small tuple, append it to a bounded deque under a lock, and return.
One background writer formats, sorts each batch by the producer's own clock,
and writes. When the queue is full lines are DROPPED and counted rather than
made to wait -- a diagnostic may lose a line, it may not stall the GUI.

Cost when off: `enabled()` reads one environment variable and returns. `span`
returns a shared do-nothing object, `mark` returns immediately, and no thread,
timer or file exists. The switch is read per call rather than captured at
import so a single run can be traced without a code change -- the same choice
`BLOCK01_MIDPAN_DEBUG` made, and for the same reason.

    BLOCK01_PERF=1                   trace
    BLOCK01_PERF_LOG=<path>          write the lines to this file, and NOT to
                                     stderr: a run being measured must not
                                     pay for a terminal
    BLOCK01_PERF_STDERR=1            write to stderr as well (or instead,
                                     with no file). Explicit, because it is
                                     the expensive one
    BLOCK01_PERF_QUEUE=<n>           queue bound, default 20000 lines

Line shape, one event per line:

    PERF t=1234.567890 ev=step1.overlay dur_ms=41.83 channels=3 patch=0

`t` is `time.monotonic()` taken on the producing thread, so every line in a
run -- and every MIDPAN line from the middle-drag diagnostic, which shares
this sink -- is comparable and ordered by when it happened rather than by when
it reached the disk.
"""

import atexit
import collections
import os
import sys
import threading
import time

PERF_ENV = "BLOCK01_PERF"
PERF_LOG_ENV = "BLOCK01_PERF_LOG"
PERF_STDERR_ENV = "BLOCK01_PERF_STDERR"
PERF_QUEUE_ENV = "BLOCK01_PERF_QUEUE"

# A heartbeat silence longer than this is the GUI thread being unavailable:
# the timer should tick 200 times a second, so a gap IS the thread busy
# elsewhere, measured from inside the event loop.
HEARTBEAT_INTERVAL_MS = 5
HEARTBEAT_GAP_MS = 25.0

DEFAULT_QUEUE = 20000       # lines; ~2 MB of text at worst
WRITER_IDLE_S = 0.02        # how often the writer looks for work
WRITER_BATCH = 2000         # lines formatted and written in one go
SHUTDOWN_DRAIN_S = 0.25     # bounded: closing must not wait on the disk


def enabled():
    return os.environ.get(PERF_ENV, "") not in ("", "0", "false", "no")


def _truthy(name):
    return os.environ.get(name, "") not in ("", "0", "false", "no")


# Field names whose value is a POINT IN TIME rather than a measurement.
# `%.4g` on a monotonic clock reading of 759147.127727 writes 7.591e+05 and
# throws away tens of seconds -- the first real run lost every span's start
# that way, and the intervals had to be recovered from `t - dur_ms`. Times
# keep microseconds; durations and counts stay short.
_TIME_FIELDS = ("t_begin", "t_end", "t_input", "t_publish")


def _fields(items):
    out = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, float):
            value = (f"{value:.6f}" if key in _TIME_FIELDS
                     else f"{value:.4g}")
        out.append(f"{key}={value}")
    return out


def _format(record):
    """A queued record becomes its line HERE, on the writer thread.

    The producer paid for a clock read and a tuple; the string work, which is
    most of the cost, is the writer's.
    """
    kind, stamp, event, dur, fields = record
    if kind == "raw":
        return event
    head = ["PERF", f"t={stamp:.6f}", f"ev={event}"]
    if dur is not None:
        head.append(f"dur_ms={dur:.2f}")
    return " ".join(head + _fields(fields.items()))


class _Writer:
    """The single consumer: bounded queue in, batched file writes out."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._limit = DEFAULT_QUEUE
        self._dropped = 0
        self._reported_drops = 0
        self._path = None
        self._handle = None
        self._to_stderr = False
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._final = False
        self._start_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()

    # ── producer side: called from the GUI thread and from workers ──
    def submit(self, record):
        """Bounded, non-blocking, and never the caller's problem.

        Drops instead of waiting: a full queue means the writer is behind, and
        making a slider callback wait for a disk would be measuring the
        measurement. Drops are counted and reported, so a timeline is never
        silently incomplete.
        """
        with self._lock:
            if self._final:
                # After the final shutdown there is no consumer, and starting
                # one would mean a second writer on a file the first has
                # closed. Loader lines that arrive after teardown are lost
                # deliberately -- the alternative is a thread outliving the
                # program that asked for it.
                return False
            if len(self._queue) >= self._limit:
                self._dropped += 1
                return False
            self._queue.append(record)
        self._wake.set()
        return True

    def configure(self):
        """Read the environment and make sure a writer exists if one is due.

        Called from `mark`/`span`, which are already reading the switch, so a
        run that turns tracing on mid-flight gets a writer without anything
        else having to know.
        """
        path = os.environ.get(PERF_LOG_ENV, "") or None
        to_stderr = _truthy(PERF_STDERR_ENV) or path is None
        try:
            limit = int(os.environ.get(PERF_QUEUE_ENV, "") or DEFAULT_QUEUE)
        except ValueError:
            limit = DEFAULT_QUEUE
        with self._lock:
            self._to_stderr = bool(to_stderr)
            self._limit = max(1, limit)
            changed = path != self._path
            if changed:
                self._path = path
        self._ensure_writer()

    def _ensure_writer(self):
        """Exactly one writer, ever: two would both own the file handle and
        both drain the queue, which is how a timeline loses its order."""
        with self._start_lock:
            if self._final:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._run,
                                      name="perf-trace-writer", daemon=True)
            self._thread = thread
            thread.start()
            atexit.register(_atexit_shutdown)

    # ── consumer side ──
    def _drain(self, limit=WRITER_BATCH):
        with self._lock:
            take = min(len(self._queue), limit)
            batch = [self._queue.popleft() for _ in range(take)]
            dropped, self._dropped = self._dropped, 0
            path, to_stderr = self._path, self._to_stderr
        if dropped:
            self._reported_drops += dropped
            batch.append(("mark", time.monotonic(), "perf.dropped", None,
                          {"lines": dropped, "total": self._reported_drops,
                           "limit": self._limit}))
        if not batch:
            return 0
        # Ordered by the PRODUCER's clock, not by arrival: two threads
        # enqueueing at once must not make the timeline claim one happened
        # after the other because its lock came second.
        batch.sort(key=lambda rec: rec[1])
        text = "".join(_format(rec) + "\n" for rec in batch)
        if path:
            try:
                if self._handle is None or getattr(self._handle, "name",
                                                   None) != path:
                    if self._handle is not None:
                        try:
                            self._handle.close()
                        except Exception:               # noqa: BLE001
                            pass
                    self._handle = open(path, "a", encoding="utf-8")
                self._handle.write(text)
                self._handle.flush()
            except Exception as exc:                    # noqa: BLE001
                self._handle = None
                with self._lock:
                    self._path = None
                    self._to_stderr = True
                try:
                    print(f"PERF log-file-failed path={path!r} err={exc!r}",
                          file=sys.stderr, flush=True)
                except Exception:                       # noqa: BLE001
                    pass
        if to_stderr:
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except Exception:                           # noqa: BLE001
                pass
        return len(batch)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(WRITER_IDLE_S)
            self._wake.clear()
            try:
                while self._drain():
                    pass
            except Exception:                           # noqa: BLE001
                pass

    def shutdown(self, timeout_s=SHUTDOWN_DRAIN_S, final=False):
        """Bounded drain, and only one at a time.

        Closing a window must not wait on a disk, so what is left after the
        deadline is lost -- and the next line of the log says how much. The
        lock matters because the caller and the writer thread must not drain
        or close the same handle at once; `final` is the program ending, after
        which nothing is accepted and no writer is started, so a loader
        reporting `job.end` during teardown cannot resurrect the sink.
        """
        with self._shutdown_lock:
            if final:
                with self._lock:
                    self._final = True
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            self._stop.set()
            self._wake.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                # Let the ONE consumer finish what it has; draining from here
                # at the same time would interleave two batches into the file.
                thread.join(max(0.0, deadline - time.monotonic()))
            try:
                while time.monotonic() < deadline:
                    if not self._drain():
                        break
            except Exception:                           # noqa: BLE001
                pass
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:                       # noqa: BLE001
                    pass
                self._handle = None

    # ── introspection, for the tests ──
    def pending(self):
        with self._lock:
            return len(self._queue)

    def dropped_total(self):
        with self._lock:
            return self._dropped + self._reported_drops

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()


WRITER = _Writer()


def shutdown(timeout_s=SHUTDOWN_DRAIN_S, final=False):
    WRITER.shutdown(timeout_s, final=final)


def _atexit_shutdown():
    WRITER.shutdown(SHUTDOWN_DRAIN_S, final=True)


def mark(event, **fields):
    """One instant, no duration: an input arriving, a decision taken."""
    if not enabled():
        return
    try:
        stamp = time.monotonic()
        WRITER.configure()
        WRITER.submit(("mark", stamp, event, None, fields))
    except Exception:                                   # noqa: BLE001
        pass


def emit_raw(line):
    """A pre-formatted line from another diagnostic, on the same sink.

    The middle-drag log builds its own line (its field set is pinned by
    tests); it still belongs in the same queue, so the two diagnostics share
    one clock, one file and one writer thread rather than each flushing from
    the GUI thread.
    """
    try:
        stamp = time.monotonic()
        WRITER.configure()
        return WRITER.submit(("raw", stamp, line, None, None))
    except Exception:                                   # noqa: BLE001
        return False


class _Nothing:
    """What `span` returns with the switch off: no clock, no allocation of
    consequence, no line."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def add(self, **_fields):
        return self


_NOTHING = _Nothing()


class _Span:
    """A timed region. Fields may be added while it runs -- the count of
    channels composited, the patch it was for -- and are written with the
    duration when it ends, so one line carries what the work was and what it
    cost."""

    __slots__ = ("_event", "_fields", "_t0")

    def __init__(self, event, fields):
        self._event = event
        self._fields = dict(fields)
        self._t0 = time.monotonic()

    def add(self, **fields):
        self._fields.update(fields)
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        try:
            end = time.monotonic()
            dur = (end - self._t0) * 1000.0
            if exc_type is not None:
                self._fields["raised"] = exc_type.__name__
            self._fields.setdefault("t_begin", float(self._t0))
            WRITER.configure()
            WRITER.submit(("span", end, self._event, dur, self._fields))
        except Exception:                               # noqa: BLE001
            pass
        return False


def span(event, **fields):
    if not enabled():
        return _NOTHING
    try:
        return _Span(event, fields)
    except Exception:                                   # noqa: BLE001
        return _NOTHING


class Revisions:
    """Numbers for the inputs, so a published frame can be compared with the
    input it was drawn from.

    "The picture lags the mouse" is a statement about two clocks: when the
    slider moved and when the screen showed that move. Counting the input
    events gives the second number a name -- a frame that publishes revision
    17 while revision 31 has already arrived is 14 events behind, and that is
    a fact rather than an impression.
    """

    def __init__(self):
        self._counters = {}
        self._lock = threading.Lock()

    def bump(self, kind):
        with self._lock:
            value = self._counters.get(kind, 0) + 1
            self._counters[kind] = value
            return value

    def latest(self, kind):
        with self._lock:
            return self._counters.get(kind, 0)


REVISIONS = Revisions()


class _Actives:
    """How many background jobs and reads are in flight, right now.

    The question the timeline has to answer is "when the GUI stalled for
    300 ms, what was reading?" -- and a log of starts cannot answer it,
    because a cancel is a flag and a thread already inside one `read_region`
    keeps going. So every job and every read reports both ends, and each line
    carries the counts as they were at that instant.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = 0
        self._reads = 0
        self._seq = 0

    def next_job_id(self):
        with self._lock:
            self._seq += 1
            return self._seq

    def job_begin(self):
        with self._lock:
            self._jobs += 1
            return self._jobs, self._reads

    def job_end(self):
        with self._lock:
            self._jobs = max(0, self._jobs - 1)
            return self._jobs, self._reads

    def read_begin(self):
        with self._lock:
            self._reads += 1
            return self._jobs, self._reads

    def read_end(self):
        with self._lock:
            self._reads = max(0, self._reads - 1)
            return self._jobs, self._reads

    def snapshot(self):
        with self._lock:
            return self._jobs, self._reads


ACTIVES = _Actives()


def new_job_id():
    return ACTIVES.next_job_id()


class job:
    """A background loader's whole life, both ends, every exit path.

    Used as a context manager around a worker's `run()`: the job's end is
    reported on a normal return, an exception, and a cancel alike, because a
    timeline that cannot close a job counts it as still reading forever.
    """

    def __init__(self, source, job_id=None, **fields):
        self.job_id = job_id if job_id is not None else new_job_id()
        self._source = source
        self._fields = dict(fields)
        self._t0 = None
        self._on = False

    def add(self, **fields):
        self._fields.update(fields)
        return self

    def __enter__(self):
        self._on = enabled()
        if not self._on:
            return self
        self._t0 = time.monotonic()
        jobs, reads = ACTIVES.job_begin()
        mark("job.begin", source=self._source, job=self.job_id,
             active_jobs=jobs, active_reads=reads, **self._fields)
        return self

    def __exit__(self, exc_type, exc, _tb):
        if not self._on:
            return False
        jobs, reads = ACTIVES.job_end()
        fields = dict(self._fields)
        if exc_type is not None:
            fields["raised"] = exc_type.__name__
        mark("job.end", source=self._source, job=self.job_id,
             dur_ms=(time.monotonic() - self._t0) * 1000.0,
             active_jobs=jobs, active_reads=reads, **fields)
        return False

    def read(self, **fields):
        """One `read_region()`, both ends, with the counts at each end."""
        return _Read(self._source, self.job_id, fields)


class _Read:
    __slots__ = ("_source", "_job", "_fields", "_t0", "_on")

    def __init__(self, source, job_id, fields):
        self._source = source
        self._job = job_id
        self._fields = dict(fields)
        self._t0 = None
        self._on = False

    def add(self, **fields):
        self._fields.update(fields)
        return self

    def __enter__(self):
        self._on = enabled()
        if not self._on:
            return self
        self._t0 = time.monotonic()
        jobs, reads = ACTIVES.read_begin()
        mark("read.begin", source=self._source, job=self._job,
             active_jobs=jobs, active_reads=reads, **self._fields)
        return self

    def __exit__(self, exc_type, exc, _tb):
        if not self._on:
            return False
        jobs, reads = ACTIVES.read_end()
        fields = dict(self._fields)
        fields["outcome"] = ("error" if exc_type is not None
                             else fields.pop("outcome", "ok"))
        if exc_type is not None:
            fields["raised"] = exc_type.__name__
        mark("read.end", source=self._source, job=self._job,
             dur_ms=(time.monotonic() - self._t0) * 1000.0,
             active_jobs=jobs, active_reads=reads, **fields)
        return False


def read(source, job_id, **fields):
    """A read outside a `job` context (a loader that owns its own id)."""
    return _Read(source, job_id, fields)


class Heartbeat:
    """Ask the GUI thread every few milliseconds whether it is still there.

    A callback that runs for 400 ms and an input that was never delivered are
    the same hole in a log of callbacks. This closes that: the timer should
    tick at a known rate, so a silence is the thread being unavailable, and
    the gap is reported with the background counts at that moment.

    Created only when the switch is on, and owned by its host, which stops it
    on teardown. Never started with the switch off, so a program that is not
    being traced has no 5 ms timer in it.
    """

    def __init__(self, parent=None, interval_ms=HEARTBEAT_INTERVAL_MS,
                 gap_ms=HEARTBEAT_GAP_MS, label=""):
        from PyQt5 import QtCore

        self._gap_ms = float(gap_ms)
        self._label = str(label or "")
        self._last = time.monotonic()
        self._timer = QtCore.QTimer(parent)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._last = time.monotonic()
        self._timer.start()
        return self

    def stop(self):
        try:
            self._timer.stop()
        except Exception:                               # noqa: BLE001
            pass

    def is_active(self):
        try:
            return bool(self._timer.isActive())
        except Exception:                               # noqa: BLE001
            return False

    def _tick(self):
        now = time.monotonic()
        last, self._last = self._last, now
        gap = (now - last) * 1000.0
        if gap > self._gap_ms:
            jobs, reads = ACTIVES.snapshot()
            mark("gui.gap", gap_ms=gap,
                 expected_ms=self._timer.interval(),
                 t_begin=float(last),
                 active_jobs=jobs, active_reads=reads,
                 where=self._label or None)


def start_heartbeat(parent=None, label=""):
    """A heartbeat if the switch is on, else None -- the caller keeps whatever
    it gets and stops it if it is not None."""
    if not enabled():
        return None
    try:
        return Heartbeat(parent=parent, label=label).start()
    except Exception:                                   # noqa: BLE001
        return None


def run_id():
    """A name for this run, written once, so lines appended to a log that
    already holds an older run can be told apart."""
    return f"{int(time.time())}-{os.getpid()}"


def announce_run(**fields):
    if not enabled():
        return
    mark("run.begin", run=run_id(), pid=os.getpid(), **fields)
