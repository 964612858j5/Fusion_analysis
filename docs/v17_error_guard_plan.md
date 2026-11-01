# v17 error guard — plan v2.1 (2026-10-31)

v2: codex plan review round 1 (4 P2 + P3s) answered — see §4.
v2.1: round 2 — no P1/P2; its three P3 wording/scope notes applied.

Source: whole-code review 2026-10-31 (Fable C8 + R1; codex noted the same
`unwrap`/panic surface). User (TG 2026-10-31): after the review fixes, item 7
— a global exception hook and protection when a Rust reader thread panics —
plan first, codex review, then the user's authorization.

Problem today:

* `main.py` installs no `sys.excepthook`. With PyQt5 >= 5.5 an exception
  that escapes a slot, a timer or a paint while the default hook is in place
  ends the process (`qFatal` / abort). One unexpected error anywhere closes
  Block01 with unsaved work.
* A Rust panic inside a `#[pymethods]` call reaches Python as PyO3's
  `PanicException`, which derives from `BaseException`. The Python fallbacks
  around the engine catch `Exception` ("reported, Python picture"), so a
  panic skips them and falls through to the (fatal) hook.
* A panic inside a reader thread (`pool/worker.rs`, `label_pool.rs`) ends
  that thread silently. Its key stays in `reading` forever: the planner never
  queues the tile again, the picture keeps a hole, and idle never returns.

Scope is a closed whitelist (AGENTS.md rule 1). No new cache, scheduler or
state machine. No change to the tile planner, the reading order or any
scientific code.

---

## 1. One global exception hook (Python)

New small module `utils/error_hook.py`, installed once by `main.py`'s
`main()` only (tests and scripts that import the package keep Python's
default; hook tests restore both hooks or run in a subprocess):

1. `sys.excepthook` AND `threading.excepthook` both: the traceback goes to
   stderr as today and is appended to `~/.block01/logs/errors.log`
   (`Path.home()`; works on Windows). Writes are serialised by a lock and the
   file is opened and closed per entry; past 5 MB it is rotated with
   `os.replace(errors.log, errors.log.1)` (replaces an old `.1`, also on
   Windows). If the home folder is missing or not writable, stderr only, and
   the notice does not claim a log was saved. The process keeps running.
2. The notice: both hooks only EMIT a signal of one GUI-owned QObject
   (created by `install()` on the application thread before any worker
   starts, kept alive by the module), connected with an explicit
   `Qt.QueuedConnection` -- `sys.excepthook` also runs on worker threads
   (an exception escaping `QThread.run()`), so neither hook builds widgets.
   The slot, on the GUI thread, builds and shows a `QMessageBox` with
   `setWindowModality(Qt.NonModal)` and `show()` (no `exec_()`, no nested
   loop), keeps a reference until it is closed: "Something went wrong in
   Block01: `<Type>: <message>`. Block01 kept running. If something looks
   wrong, save your work and restart. Details: `<log path>`." At most one
   notice per distinct error (type + file + line of the innermost frame)
   per session and never two at once (the guard is set when the signal is
   emitted, cleared when the box closes); repeats are only logged. The slot
   itself is wrapped: a failure there is logged to stderr, never re-raised.
3. `KeyboardInterrupt` / `SystemExit` go to `sys.__excepthook__` as Python's
   default (no notice, nothing logged). Note: under PyQt5 5.15 the previous
   abort of the whole process on ANY unhandled exception is what changes.
4. A failure inside the hook itself never raises; it falls back to
   `sys.__excepthook__` printing.

Tests: a slot exception (offscreen) is logged and gives one non-modal notice
while the main window stays interactive; the same error twice gives one
notice; a different one a second; an exception in a Python `Thread` and one
escaping a `QThread.run()` are logged and their notices arrive on the GUI
thread; the log rotates past its size (an existing `.1` replaced); an
unwritable home gives stderr only; `KeyboardInterrupt` is passed on;
installing twice installs once.

## 2. A Rust panic falls back like any other engine error (Python side)

1. The Rust module exposes PyO3's panic type:
   `m.add("PanicException", m.py().get_type::<pyo3::panic::PanicException>())`
   in `src/py/mod.rs`.
2. `ui/step1_core_adapter.py`: `CORE_ERRORS = (Exception, PanicException)`
   (just `(Exception,)` with an older wheel that lacks the attribute).
3. **The shared paint boundary** (`ui/core_gl_session.py`, used by Step0,
   Step0 compare and Step1): `CoreGlSession.paint` catches `CORE_ERRORS`
   from `core.paint(...)` and returns `{"error": "<Type>: <message>"}`, so
   the callers' existing route (`failure_of` -> `_fail`, deferred, Python
   picture: `step1_core_layer.py:243`, `step0/gl_host.py:1116`) takes it.
   The measurement/housekeeping calls after a paint, `stats()` in
   `note_reads` (`:121`) and `drain_growths()` in `note_frame` (`:146`),
   catch `CORE_ERRORS` too: logged once and that reporting call skipped;
   any subsequent paint failure follows the existing fallback route.
4. The engine fallbacks that catch `Exception` and fall back to the Python
   picture catch `CORE_ERRORS` instead -- exactly these, each around a call
   into the Rust core:
   * `ui/step0/engine_mount.py`: GL host start (`:175`), source setup
     (`:212`), `ViewerCore(...)` (`:396`), `add_candidate` (`:403`);
   * `ui/step0/gl_host.py`: `attach_gl` failure cleanup of the candidate
     (`:251`, closes core and sockets, then re-raises) and context
     re-initialisation (`:822`);
   * `ui/step1_core_binding.py:408` (`ViewerCore(...)`);
   * `ui/step1_core_layer.py:202` (GL initialisation);
   * `ui/step1_core_labels.py:90` (`draw_labels`, falls back);
   * `ui/step1_viewer_mount.py:424` and `:593` (mount start);
   * `ui/step3_core_labels.py:366` (`set_label_slots([])` on a closed core).
5. Classified and NOT changed in this block: best-effort cleanup catches
   (`engine_mount.py:657,672`, `gpu_memory.py:57,71`) and `gpu_memory.py:44`
   (reaches Rust only indirectly, `set_smooth -> refresh_display ->
   core.set_display`) -- a panic there goes to the global hook, which keeps
   the app running; the Rust fusion job
   (`overview_panel.py:636`, `FuseJob`) is a separate compute path, not the
   viewer engine (a later block if wanted); `compare_strip.py:1887` wraps
   setup that does not call Rust (compare is covered by the paint boundary).
   All other `except Exception` sites are not engine calls.

Tests: a `PanicException` from a stubbed `ViewerCore` (Step0 mount, Step1
binding), from `draw_labels`, and from `core.paint` (Step1 layer, Step0 host
incl. compare) takes the same fallback as a `RuntimeError`; `attach_gl`
panicking closes the candidate's core and sockets; a re-initialisation that
panics with resident cores fails over; with a wheel lacking the attribute,
`CORE_ERRORS` is `(Exception,)`.

## 3. A panicking reader read becomes a read error (Rust side)

1. `src/pool/worker.rs`: the read of one tile (`read_tile(...)`, run with
   no outer worker lock held; its `still` callback takes the queue lock only
   briefly) runs inside
   `std::panic::catch_unwind`; a panic becomes
   `Err(CoreError::Io("reader panicked: <message>"))` and goes down the
   existing failure path: the key leaves `reading`, the failure is counted
   per key, `last_error` says it, the GUI is notified and retries or falls
   back (R3 plan §2.5). The thread lives on.
2. `src/label_pool.rs`: the same around `read_label_tile(...)`: a panic is
   that slot's per-stamp error (Step3's existing error path); `take()`
   releases its `reading` and pending bytes, and the tile is read again
   when it is planned again (no automatic retry is added).
3. `src/fault.rs` (test-only injection, unset in the product): `panic:N`
   (the first N tile reads panic) and `labelpanic:N` (the first N label
   reads panic), beside the existing `tile:N` / `label:N`. The injected
   panic is raised INSIDE the caught closure, as a real decode panic would.
4. Locks: the catch covers the read only; queue/cache work stays outside
   it, as today. A panic in the read cannot poison the worker's locks. The 65 `lock().unwrap()` stay (a poisoned lock needs a
   panic WHILE holding a lock; recovering from that is not in this block).
5. Not covered: the optional decoded-chunk cache (`chunk_cache.rs`, only
   with `BLOCK01_STEP1_CHUNK_CACHE=1`, off by default): a decode panic there
   would leave its reservation behind. The recovery guarantee is for the
   default configuration; that mode stays an experiment.
6. The wheel is rebuilt and installed (`maturin build --release`, pip
   `--force-reinstall --no-deps`); crate version unchanged (versioning is a
   later review item).

Tests: Rust unit tests — with `panic:1` the first read fails as an error,
the key is released and read again successfully, the worker count is
unchanged, `errors` counts 1; `labelpanic:1` gives a per-stamp error, the worker
lives on, and after a new plan the tile is read successfully. Python: a viewer core under
`BLOCK01_RUST_TEST_FAULT=panic:1` draws the full picture after the retry
(the existing fault tests' pattern).

---

## Verification

* New tests per item; `cargo test`; the wheel rebuilt and installed.
* Targeted regression: Step0 engine mount / compare, Step1 viewer mount /
  core binding / labels, Step3 labels, the R3 fallback suites; 4 in
  parallel, GPU display `:1`.
* Codex astra high review of this plan, then of the code.
* Real-machine acceptance (user): the app behaves as before; no visible
  change unless something fails. Optional check: start with
  `BLOCK01_RUST_TEST_FAULT=panic:3` and see the picture fill in normally.

## 4. Codex plan review round 1 — answers

| finding | answer |
|---|---|
| P2 paint panics bypass the fallback | §2.3: `CoreGlSession.paint` turns `CORE_ERRORS` into the error report; `stats` / `drain_growths` classified |
| P2 Step0 inner cleanup / re-init catches | §2.4: `gl_host.py:251` and `:822` added; tests for cleanup and re-init |
| P2 `show()` is not non-modal | §1.2: `setWindowModality(Qt.NonModal)`, reference kept, built in the GUI slot, guarded |
| P2 `sys.excepthook` on worker threads | §1.2: both hooks only emit a queued signal of a GUI-owned QObject; QThread and Thread tested |
| P3 inventory scope | §2.5: cleanup catches, `FuseJob`, compare setup classified, not changed |
| P3 reader boundary / label retry wording | §3.1, §3.2, §3.3 |
| P3 logging robustness / hook tests / KeyboardInterrupt | §1.1, §1.3, §1 opening |

Whitelist (AGENTS.md rule 1): new `utils/error_hook.py` and its notice UI;
`main.py`; `ui/step1_core_adapter.py`; `ui/core_gl_session.py`; the sites of
§2.4; `native/fusion_viewer_core/src/{py/mod.rs,pool/worker.rs,label_pool.rs,fault.rs}`, plus (implementation, reported to the user) the new small helper `src/panic_guard.rs` and its `lib.rs` declaration -- one place for `catch_unwind` and the panic message, per the small-modules rule;
tests.
