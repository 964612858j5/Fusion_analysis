# v17 Step2 prefetch cleanup — plan v2.1 (2026-11-01)

v2: codex plan review round 1 (1 P2 + 3 P3, no P1) answered — see §4.
v2.1: round 2 — no P1/P2; its P3 (acceptance wording) applied.

Source: whole-code review 2026-10-31, codex finding 4 (P2, "Step2 completion
does not encompass all of its prefetch work"). User (TG 2026-11-01): next
item — plan first, codex review, then the user's authorization.

## 0. Problem

Step2 reads its next tiles ahead (`enable_tile_prefetch`, on by default,
queue of 2) through `utils/tile_prefetch.py::TilePrefetcher`: one
`ThreadPoolExecutor` thread (`step2-tile-prefetch`).

1. `TilePrefetcher.close()` calls `shutdown(wait=False, cancel_futures=True)`:
   queued reads are cancelled, but a read already running goes on after
   `close()` returns.
2. The worker (`workers/segment_merge_worker.py`) closes the tile scheduler
   (which closes the prefetcher) on the normal end and on its Stop paths,
   but NOT when an exception leaves a tile loop elsewhere (a tile read, the
   merge, a write): `_segment_one_zarr` (ROI loop, from line 2869) and the
   full-slide loop in `run()` (from line 3790). `run()`'s `except`/`finally`
   (line 4469 on) do not close it either.
3. One Stop path emits before it closes: the full-slide loop's
   `self.error.emit('Stopped by user.')` comes BEFORE
   `self._close_tile_scheduler(scheduler)` (line 3884).

A normal run that reaches its end has already consumed every prefetched
payload; the outstanding read is a problem of Stop and of abnormal exits:
the page can hear "stopped" / "error" while a tile read is still under way
on the prefetch thread — and after an exception, up to two queued reads
too. On HDD / external drives one tile read can take seconds. The page
re-enables Run from its `finished` / `error` handlers (`_on_error`,
`ui/step2_page.py:3772`; line 3551 is only the window-close poll), so a new
run can start reading the same files meanwhile. (No link
to the known quit segfault was found; this is not claimed to fix it.)

## 1. Fix

### 1.1 `TilePrefetcher.close()` settles its work (on the caller's thread)

```
def close(self, wait_s=30.0):
    """Cancel queued reads, wait (here, on the calling worker thread) for
    the one under way. True when nothing is left running."""
```

* idempotent (a second call returns the first answer at once);
* `_closed = True`; `shutdown(wait=False, cancel_futures=True)` as today
  (queued reads never start);
* then `concurrent.futures.wait(<futures not done>, timeout=wait_s)`; past
  the budget one warning is logged ("a tile read was still running after
  30 s; the run ends without waiting for it") and `False` is returned;
* when everything has finished, the executor is shut down with
  `wait=True` — its single thread then exits at once (a finished future
  does not yet mean the thread has consumed the shutdown), so on `True`
  no `step2-tile-prefetch` thread is alive;
* a read's own exception at this point is ignored (the run is ending).

**30 s is a cleanup-wait budget, not a bound on Stop.** Out of scope and
unchanged (each would need a separate scope decision): the worker may be
blocked before it reaches `close()` — `TilePrefetcher.get()` waits on the
current read (and its synchronous retry) without a timeout — and after
cleanup `run()` still reads the channel store's metrics and closes it
(`utils/channel_cache.py:66` holds the store lock during `zarr.open()`). A
read stuck on a dead drive can therefore still delay Stop as it does
today; this plan only makes sure that a read which does finish has
finished before the run answers.

It is only ever called from the worker thread (all current callers are in
the worker), never from the GUI thread — no GUI wait is added.
`TileScheduler.close()` passes it through unchanged.

### 1.2 The worker always closes its active tile scheduler before it answers

* `self._active_tile_scheduler`: set where each loop attaches its payload
  loader / prefetcher (both loops), cleared by `_close_tile_scheduler` when
  it closes that scheduler.
* `self._active_tile_scheduler = None` is set at the very start of
  `run()` (before anything can fail), assigned in both loops BEFORE the
  prefetcher is attached, and cleared when that scheduler is closed.
* `run()`: `self._close_tile_scheduler(self._active_tile_scheduler)` at the
  top of the `except` block and right before BOTH `self.finished.emit(...)`
  calls (lines 3765 and 4466); and once more in `finally` (idempotent; the
  cached first answer means no second 30 s wait).
* The full-slide Stop path closes first, then emits (swap at line 3884).

This is a cleanup helper on existing objects: no new scheduler, cache or
state machine; no change to tile order, the number of reads ahead, what is
segmented, or any output.

## 2. Tests

* `tests/test_tile_prefetch_close.py` (new, offscreen, no GPU):
  * queued reads are cancelled: with a slow first read, reads 2.. never run;
  * the running read has finished when `close()` returns (`True`), and no
    `step2-tile-prefetch` thread is alive afterwards;
  * a read that outlasts `wait_s` (small value in the test): `close()`
    returns `False` after about `wait_s` and logs once; the read finishing
    later does no harm;
  * `close()` twice is harmless; a prefetcher with queue 0 closes at once.
  (the running-read test checks thread exit with a join, not an instant
  snapshot, though with the `wait=True` shutdown it is already gone.)
* Worker level, synthetic project (the `test_step2_legacy_stop.py`
  harness's data, real StarDist, skipped when not installed), the worker
  started with `worker.start()` (its own QThread) and the signals observed
  on the GUI thread; parametrized over the ROI loop and the full-slide loop
  (so a missing `_active_tile_scheduler` assignment in either is caught):
  * tile 2's payload read raises in BOTH the prefetched read and the
    synchronous retry, while tile 3's prefetched read is slow (an event
    confirms it has started before tile 2 fails): when `error` is emitted,
    tile 3's read has finished and no `step2-tile-prefetch` thread is alive;
  * a Stop while a prefetched read is running (start synchronized): the
    same at `error('Stopped by user.')`;
  * the worker's budget branch: a read that outlasts a small test budget
    (`wait_s` lowered for the test) — the run still answers, after about
    the budget, with the warning logged;
  * a normal run: `finished` as before, no prefetch thread alive.
* Mutation checks: with `wait=False` restored, the "finished when close
  returns" test fails; without the `run()` cleanup, the exception test fails.
* Regression (targeted): tile scheduler, Step2 engine perf/unified/runner
  path, legacy stop, skip empty tiles, keeps nuclei, preseg contract,
  remap promotion, seam merge, f4/f5 Step2.

## 3. Scope (closed whitelist, AGENTS.md rule 1)

* `utils/tile_prefetch.py` (`close()`)
* `utils/tile_scheduler.py` (`close()` pass-through only, if needed)
* `workers/segment_merge_worker.py` (`_active_tile_scheduler`, the two
  loops' attach points, the full-slide Stop order, `run()`'s cleanup)
* `tests/test_tile_prefetch_close.py` (new), one worker-level test file
  (new, `tests/test_step2_prefetch_settles.py`)
* this plan

Real-machine acceptance: run Step2 on a slide on the HDD and press Stop
mid-run; it says stopped as before (when the read under way finishes
within the 30 s cleanup budget: at most that read's time later), and a new
run started right after works normally. A read that outlasts the budget is
left running, as today, with a warning in the log. A normal run finishes as
before.

## 4. Codex plan review round 1 — answers

* P2 30 s does not bound Stop → §1.1 now calls it a cleanup-wait budget and
  lists what stays out of scope (`get()` waiting without a timeout, the
  channel store's metrics/close after cleanup).
* P3 future done ≠ thread gone → on success the executor is shut down with
  `wait=True`; the test joins the thread.
* P3 worker tests → both reads of tile 2 fail; ROI and full-slide loops;
  start synchronized with an event; the worker's budget branch tested;
  `worker.start()`.
* P3 diagnosis → a normal end has no outstanding read (Stop / abnormal exits
  only); Run is re-enabled by the finished/error handlers.
* Advisory: `segment_merge_worker.py:4014` emits before cleanup on the
  retired inference path the UI refuses — recorded, not in scope.

## 5. Implementation notes (authorized 2026-11-01)

* As planned; `utils/tile_scheduler.py` needed no change (its `close()`
  uses the default budget). The budget is the module constant
  `tile_prefetch.CLOSE_WAIT_S` (tests lower it).
* `_close_active_tile_scheduler()` (new small worker method) closes and
  clears `_active_tile_scheduler`; called at the top of `run()`'s
  `except`, before both `finished` emits and in `finally`.
* Tests: `tests/test_tile_prefetch_close.py` (4) and
  `tests/test_step2_prefetch_settles.py` (8: failed read, Stop, budget
  branch, normal run × ROI / full-slide; the worker started with
  `start()`).
* Mutation checks: old `close()` → 8 fail; no cleanup in `run()`'s
  `except` → the failed-read tests fail (both modes); no assignment in the
  full-slide loop → its failed-read and budget tests fail.
