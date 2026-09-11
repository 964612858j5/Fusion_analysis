"""Persisting a finished patch edit, off the callback that ended the gesture.

WHY. Letting go of a dragged patch ran `_persist_geometry_edit` inline: read
the published manifest and config, read two geometry files, write three JSON
artifacts, open the corrected zarr and rewrite its attributes, scan it for a
validity report, then write and fsync the manifest. On the real desk that is
the length of a freeze, and it happened once per finished patch -- which is
half of "drawing two patches locks the window" (the other half is the preload
storm, see `PreloadScheduler`).

WHAT THIS GUARANTEES, and each is a rule the old inline write got for free by
being synchronous:

* ONE worker, ONE task in flight, ONE pending task -- the newest. Ten patches
  drawn in a second are ten revisions of the same geometry and only the last
  one describes what is on screen, so the middle eight are dropped, counted,
  and never written.
* Every task carries the DATASET GENERATION and the GEOMETRY REVISION it was
  built from. A task whose dataset is no longer current is dead on arrival: it
  would publish the previous slide's geometry into the current slide's
  directory.
* A task replaced by a newer revision does not publish. It is asked "have you
  been superseded?" before it touches anything durable -- everything it has
  written so far is a revision-tagged temporary file -- so the newer revision
  publishes instead of racing it.
* Publication is SERIAL and monotonic: one thread, and a task whose revision
  is not newer than what has already been published is refused outright, so an
  older revision can never overwrite a newer one's files.

Signals carry outcomes, never widgets: `published`, `skipped`, `failed`. The
GUI thread decides what any of them means for the screen.
"""

import threading

from PyQt5.QtCore import QObject, pyqtSignal

from ..core import step0_handoff
from ..utils import perf_trace


class GeometryPersistWorker(QObject):
    """Serial, latest-only persistence of Step0 geometry.

    A plain daemon thread, not a QThread, for the same reason as
    `PreviewComposeWorker`: it spends its life waiting, and a QThread still
    waiting when its owner is deleted aborts the process.
    """

    published = pyqtSignal(object)     # {"task", "outcome", "result"}
    skipped = pyqtSignal(object)       # {"task", "outcome", "reason"}
    failed = pyqtSignal(object)        # {"task", "error"}

    def __init__(self, commit=None, parent=None):
        super().__init__(parent)
        # Injected so a test can block inside the write and prove the GUI
        # thread was not waiting for it. Left as None for the production path,
        # which is looked up when it is CALLED -- so a test may replace the
        # module function without having to exist before this worker does.
        self._commit = commit
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending = None
        self._current = None
        self._stopping = False
        self._thread = None
        self._dataset_gen = None
        self._published_rev = 0
        # What a CONSUMER of the handoff may rely on, updated on this thread
        # the moment an outcome is known -- BEFORE the queued signal that
        # tells the page about it. A gate that waited for the signal would
        # have a window in which the worker is no longer busy, the page still
        # believes the write is in flight, and Step1 opens on the old file.
        self._confirmed_rev = 0
        self._confirmed_gen = None
        self._blocked = None           # (revision, outcome) or None
        self._stats = {"submitted": 0, "replaced": 0, "published": 0,
                       "skipped": 0, "failed": 0, "stale_dataset": 0}

    # ── producer side (GUI thread) ──────────────────────────────────
    def submit(self, task):
        """Take the newest task, drop any pending older one. Never blocks."""
        with self._wake:
            if self._stopping:
                return False
            if self._pending is not None:
                self._stats["replaced"] += 1
            self._pending = dict(task)
            self._stats["submitted"] += 1
            self._dataset_gen = task.get("dataset_gen")
            self._wake.notify()
        return True

    def invalidate(self, dataset_gen):
        """A dataset switch: everything queued describes the old slide."""
        with self._wake:
            self._dataset_gen = dataset_gen
            if self._pending is not None:
                self._pending = None
                self._stats["stale_dataset"] += 1
            self._published_rev = 0
            self._confirmed_rev = 0
            self._confirmed_gen = dataset_gen
            self._blocked = None
            self._wake.notify_all()

    def stats(self):
        with self._lock:
            out = dict(self._stats)
            out["pending"] = self._pending is not None
            out["busy"] = self._current is not None
            out["published_revision"] = self._published_rev
            return out

    def is_busy(self):
        with self._lock:
            return self._current is not None or self._pending is not None

    def pending_revision(self):
        """The newest revision this worker has been given and not finished."""
        with self._lock:
            newest = 0
            for task in (self._current, self._pending):
                if task:
                    newest = max(newest, int(task.get("revision") or 0))
            return newest

    def published_revision(self):
        with self._lock:
            return self._published_rev

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            thread = threading.Thread(target=self.run,
                                      name="geometry-persist", daemon=True)
            self._thread = thread
        thread.start()

    def isRunning(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def wait(self, timeout_ms=2000):
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, float(timeout_ms) / 1000.0))
        return not thread.is_alive()

    def wait_idle(self, timeout_ms=10_000):
        """Block until nothing is pending or in flight.

        For TESTS and for teardown reporting -- never for a callback the user
        is waiting on, which is the whole point of this class.
        """
        import time
        deadline = time.monotonic() + max(0.0, float(timeout_ms) / 1000.0)
        while time.monotonic() < deadline:
            if not self.is_busy():
                return True
            time.sleep(0.002)
        return not self.is_busy()

    def stop(self, timeout_ms=2000):
        """Ask the thread to finish. Request only -- nothing is reached into.

        A task already inside `os.replace` finishes it: the alternative is a
        half-published handoff. It emits nothing after being asked to stop.
        """
        with self._wake:
            self._stopping = True
            self._pending = None
            self._wake.notify_all()
        return self.wait(timeout_ms)

    # ── consumer side (this thread) ─────────────────────────────────
    def run(self):
        while True:
            with self._wake:
                while self._pending is None and not self._stopping:
                    self._wake.wait(0.05)
                if self._stopping:
                    return
                task = self._pending
                self._pending = None
                self._current = task
                gen = self._dataset_gen
                published_rev = self._published_rev
            try:
                outcome = self._run_one(task, gen, published_rev)
            except Exception as exc:                        # noqa: BLE001
                outcome = {"task": task, "error": str(exc),
                           "outcome": "failed"}
            with self._lock:
                self._current = None
                stopping = self._stopping
                name = outcome.get("outcome")
                # The revision the WRITE used, which is not necessarily the
                # one the task asked for: a commit numbers above what is on
                # disk, so after a restart a task submitted as revision 1 is
                # published as 2. Booking the task's number here would leave
                # the page asking for 2 while this worker confirmed 1, and
                # every consumer refused until the next edit happened to
                # catch up.
                revision = int(outcome.get("revision")
                               or task.get("revision") or 0)
                if name == "committed":
                    self._published_rev = max(self._published_rev, revision)
                    self._stats["published"] += 1
                elif name == "failed":
                    self._stats["failed"] += 1
                else:
                    self._stats["skipped"] += 1
                self._note_consumable(task, revision, name)
            if stopping:
                continue
            if outcome.get("outcome") == "committed":
                self.published.emit(outcome)
            elif outcome.get("outcome") == "failed":
                self.failed.emit(outcome)
            else:
                self.skipped.emit(outcome)

    def _note_consumable(self, task, revision, name):
        """Record whether the handoff on disk now describes THIS revision.

        Called with the lock held, on this thread, before anything is
        emitted. Three groups, and the difference matters to whoever is about
        to read the handoff:

        * "committed" -- the new geometry is published; that revision is what
          a consumer gets.
        * "unchanged" -- nothing was written because the file on disk already
          describes this geometry (which `commit_geometry_only` established by
          COMPARING them). Confirmed, rather than left pending forever.
        * "stale_revision_confirmed" -- an out-of-order task whose geometry
          was checked against the files and found to be what they say.
          "Superseded" is deliberately NOT here: a newer task is still to run
          and it is that one's outcome that says what is on disk.
        * everything else (a failed write, a refused ROI change, no published
          handoff at all) -- the file does NOT describe the geometry in
          memory. It is recorded as BLOCKED, and a consumer must be refused
          rather than quietly handed the previous geometry.
        """
        gen = task.get("dataset_gen")
        if name in ("committed", "unchanged", "stale_revision_confirmed"):
            if self._confirmed_gen != gen:
                self._confirmed_gen = gen
                self._confirmed_rev = 0
            self._confirmed_rev = max(self._confirmed_rev, revision)
            if self._blocked is not None and self._blocked[0] <= revision:
                self._blocked = None
            return
        if name == "stale_dataset":
            return                      # it belonged to a slide nobody reads
        self._blocked = (revision, name)

    def consumable(self, dataset_gen, revision):
        """Is a handoff describing `revision` of `dataset_gen` on disk?

        The question a step downstream has to ask before reading the file.
        Answered from the worker's own state, so it is true as soon as the
        worker knows it -- not once a queued signal has been delivered.
        """
        with self._lock:
            if self._blocked is not None:
                return False
            if self._current is not None or self._pending is not None:
                return False
            if self._confirmed_gen is not None and (
                    self._confirmed_gen != dataset_gen):
                return False
            return self._confirmed_rev >= int(revision)

    def blocked(self):
        with self._lock:
            return self._blocked

    def _superseded(self, task, gen, phase=None):
        """Asked at each phase of a write, and last immediately before it
        would publish -- see `step0_handoff.write_handoff`."""
        with self._lock:
            if self._stopping:
                return True
            if self._pending is not None:
                return True
            if self._dataset_gen != gen:
                return True
        return False

    def _run_one(self, task, gen, published_rev):
        revision = int(task.get("revision") or 0)
        if task.get("dataset_gen") != gen:
            return {"task": task, "outcome": "stale_dataset",
                    "reason": "stale_dataset"}
        if revision and revision <= published_rev:
            # Older than what this worker has already published. Writing it
            # would move the published handoff backwards -- but whether a
            # CONSUMER may read the handoff is a different question, and a
            # revision number cannot answer it: the files are compared
            # instead, here, on this thread.
            matches = False
            try:
                matches = step0_handoff.geometry_matches(
                    task.get("step0_dir") or "", task.get("rois") or [],
                    task.get("patches") or [])
            except Exception:                               # noqa: BLE001
                matches = False
            outcome = ("stale_revision_confirmed" if matches
                       else "stale_revision_unconfirmed")
            return {"task": task, "outcome": outcome, "reason": outcome}
        with perf_trace.span("patch.persist_worker", revision=revision,
                             dataset_gen=task.get("dataset_gen")) as _sp:
            try:
                commit = self._commit or step0_handoff.commit_geometry_only
                result = commit(
                    task,
                    superseded=lambda phase=None: self._superseded(
                        task, gen, phase))
            except step0_handoff.Superseded:
                _sp.add(outcome="superseded")
                return {"task": task, "outcome": "superseded",
                        "reason": "superseded"}
            _sp.add(outcome=str(result.get("outcome") or ""))
        result = dict(result)
        result["task"] = task
        return result
