# v17 viewer release on rebuild — plan v2.1 (2026-11-01)

v2: codex plan review round 1 (2 P2 + 1 P3, no P1) answered — see §5.
v2.1: round 2 — no P1/P2; its P3 (standby wording) and advisory
(in-flight reads after close) applied.

Source: whole-code review 2026-10-31, codex finding 3 (P2, "viewer source
rebuilds retain retired Rust cores and their host-memory tiles"; top-10 #3).
User (TG 2026-11-01): next item after the error guard — plan first, codex
review, then the user's authorization.

## 0. Problem, measured

The Step1/Step3 viewer mount (`ui/step1_viewer_mount.py`) builds its drawing
binding with `parent=self` (the long-lived mount):

* Rust path: `Step1CoreBinding(parent=self, **args)` (`_start_rust_backend`,
  line 424). It holds strong references to the stack, its controller, the
  layer and the `ViewerCore`.
* Python GPU path: `Step1GpuBinding(..., parent=self)` (`_start_gpu_backend`,
  line 601). It holds the provider, scheduler, controller and layer.

`_stop_gpu_backend` (line 722) calls `binding.dispose()` and drops
`self.gpu_binding`, but never detaches the binding from the mount. Qt's
parent keeps the Python wrapper alive, and with it every object above, for
the life of the mount. (`_stop_label_binding` already does
`binding.setParent(None)`; the layer is already `setParent(None)` +
`deleteLater()`.)

In Rust, `ViewerCore::close()` (`viewer.rs:271`) stops the pool, empties the
chunk cache and closes the label pool and notifier, but keeps `held` (the
`Arc<Tile>` map of delivered tiles) and the label store's plane copies.

Measured on the real mount (synthetic 3-channel slide, scratch probe on top
of `scripts/sraw_lifecycle_probe.py`, 3 whole-slide region changes):

| after 3 source changes | today |
|---|---|
| retired `Step1CoreBinding`s still alive (weakref) | 3 of 3 |
| `Step1CoreBinding` children of the mount | 4 (3 retired + current) |
| `held_tiles` of each retired core after `close()` | 6, 6, 6 (unchanged) |

Every SOURCE/BACKEND rebuild — a region change, a product or dataset
change (`_gpu_source_changed`), the R3 run-time hand-over to Python
(`_rust_unsupported`) — adds one retired binding + stack + core. Switching
Step3 results on the same source is NOT a rebuild: it calls
`set_mask_slots()` on the live viewer (`main_window.py:2655`,
`step1_viewer_mount.py:673`) and retires nothing; its live label cache is
out of scope and unchanged. On a
real slide a core holds up to `max_fine_tiles + max_coarse_tiles` tiles, so
this is host memory that only comes back when the mount is closed.

Step0 is not affected: `gl_host._retire` already detaches each slot's
session (`slot.session.setParent(None)`) and drops the slot; the
full-image panel's parked standby core is kept on purpose (S0-C; compare
panels keep no standby, `compare_engine.py:85`).

## 1. Fix (Python, the cause)

In `_stop_gpu_backend`, after `binding.dispose()`:

1. `binding.setParent(None)` — the mount no longer owns it (same as
   `_stop_label_binding` already does). No `deleteLater()`: the binding is
   a plain QObject owned by Python once detached (never two owners). It is
   not freed instantly by refcount: the binding and its layer reference
   each other (layer callbacks), and a queued `QTimer.singleShot` (e.g.
   `_on_unsupported`) may hold it until it has run, so it is freed by the
   cycle collector once those callbacks have finished. That is the
   intended end state: nothing PERMANENT owns it any more.
2. Clear the remaining back-references that would keep a retired binding
   alive from outside:
   * `stack.scheduler.idle_probe` is set to `binding.idle` at start; it is
     reset to `None` in `_stop_gpu_backend` when it still points at the
     retiring binding (today only `_rust_unsupported` resets it).
   * `binding.on_status_changed = None` (the mount's bound method; harmless
     in direction but cleared so a stray late call is a no-op).
3. **Detach only** (codex round 1 answer to the former open question):
   `dispose()` of both binding classes is unchanged. Nulling
   `stack/controller/layer` is not needed for the measured leak and would
   spread beyond `dispose()` (`stats()` and `publication_count` read the
   stack and layer after dispose).

No change to when or how a rebuild happens, to the reading order, to the
planner, or to any scientific code.

## 2. Fix (Rust, defence in depth)

`ViewerCore::close()` also releases what it still holds in host memory:

* `self.held.clear()`;
* the label store's plane copies: `Labels::set` only replaces `layers`
  and keeps `copies`, so a small `Labels::clear()` (empties `layers`,
  `copies`, `order`, `bytes = 0`) is added — no new structure.

`close()` is already the "nothing new after this" point: no new read is
started and no notice is sent; a read already in flight finishes on its
reader thread asynchronously and its result is dropped.
What still runs after it — diagnostics (`stats()`, `gpu_bytes()`, the
layer's dispose report), GL release or forget, trace draining — reads
counts or GL names, not tile/label payloads, and stays valid (0 held, 0
label copy bytes). Label binding disposal already precedes core close.
Step0: `gl_host._retire` closes cores it is about to drop; the full-image
standby uses pause/resume (not close); compare panels disable standby
(`compare_engine.py:85`) — so no kept core is ever closed and reused.

Rust unit test (in `viewer`'s tests): a core populated with held tiles AND
label plane copies (nonzero `held` and `labels.copy_bytes()`) reports both
0 after `close()`; a second `close()` is harmless; `stats()` and
`gpu_bytes()` stay callable. A `label_store` test: `clear()` empties copies
and bytes.

This costs one wheel rebuild (same commands as the error guard).

## 3. Tests

* `scripts/sraw_lifecycle_probe.py` new scenario `repeated_source_change`:
  3 whole-slide region changes on the real mount; asserts
  * no retired binding is alive (weakrefs, checked after pumping events so
    queued callbacks have run, then `gc.collect()`),
  * exactly one drawing binding among the mount's children,
  * a retired core (kept only by the probe) reports `held_tiles == 0` and
    `label_copy_bytes == 0`,
  * the picture is stable after each change and reader threads ≤ 6
    (the existing `source_change` checks).
  Wired into `tests/test_v16_5_sraw_lifecycle.py::test_b_lifecycle`'s
  scenario list (GPU, `BLOCK01_REQUIRE_STEP1_GPU=1`).
* A Python GPU path variant: the same scenario with the core refused
  (`BLOCK01_RUST_TEST_FAULT=init` makes the mount start on Python) —
  retired `Step1GpuBinding`s are not alive either.
* Rust unit test (§2).
* Mutation check: without the `setParent(None)` the scenario fails
  (bindings alive 3); without the Rust clear, held_tiles stays 6.
* Regression (targeted): sraw lifecycle, r3 fallback, r3/r3b labels,
  r3 memory, step1 viewer mount, regions r3, Step3 multi results, and —
  because `close()` is shared with Step0 — the Step0 engine mount, full
  image (standby) and compare suites.

## 4. Scope (closed whitelist, AGENTS.md rule 1)

* `ui/step1_viewer_mount.py` (`_stop_gpu_backend` only)
* `native/fusion_viewer_core/src/viewer.rs` (`close()`),
  `native/fusion_viewer_core/src/label_store.rs` (`clear()`), their unit
  tests
* `scripts/sraw_lifecycle_probe.py`, `tests/test_v16_5_sraw_lifecycle.py`
* this plan

No new cache, scheduler or state machine. Real-machine acceptance after the
automated checks: switch regions several times in Step1, and products /
regions in Step3; the picture behaves as before, and memory (system
monitor) no longer grows with each switch. (Switching results on the same
source never retired anything and is not part of this check.)

## 5. Codex plan review round 1 — answers

* P2 Step3 result switches are not rebuilds → §0 and acceptance narrowed
  to source/backend rebuilds; live label cache untouched.
* P2 label-copy release not tested → Rust unit test with nonzero label
  copies → 0 after close, repeated close, diagnostics callable; probe also
  checks `label_copy_bytes == 0`; Step0 suites added to regression.
* P3 not an "empty shell" / not instant refcount → §1.1 wording corrected
  (freed by the cycle collector after queued callbacks); §1.3 = detach only
  plus the `idle_probe` reset; `step1_core_binding.py` leaves the
  whitelist.
* Note on what runs after `close()` corrected in §2 (GL release/forget and
  trace draining too).
* Advisory (queued `_on_unsupported` keeps a binding briefly): accepted as
  temporary; tests drain events before checking.

## 6. Implementation notes (authorized 2026-11-01)

* §1 as planned: `_stop_gpu_backend` resets a matching `idle_probe`,
  clears `on_status_changed` and detaches the binding (`setParent(None)`).
* §2 as planned: `ViewerCore::close()` clears `held` and calls the new
  `Labels::clear()`.
* Tests — two differences from §3, found while building them:
  * No Rust test builds a `ViewerCore` (it needs a slide file), so the
    unit test is on `Labels::clear()` (copies and bytes to 0, twice is
    harmless); the held-tile release is checked end to end by the probe
    (retired cores 6 held before close, 0 after).
  * In the probe the label copies of a retired core were already 0 after
    close WITHOUT the Rust clear: the label binding's disposal (which runs
    first) has emptied them. So `Labels::clear()` is defence in depth,
    covered by its unit test; the probe still checks copies > 0 before
    and 0 after.
  * Scenarios (codex code review round 1, P2: the first probe kept the
    bbox, so only the drawing backend was rebuilt on the same stack):
    `repeated_source_change` / `_python` change to a new region (new bbox,
    a new source identity) through the product's gate `sync_source()` and
    check the stack is replaced each time; `repeated_rebuild` / `_python`
    keep the bbox and rebuild only the backend on the same stack (Step3
    masks on the Rust one).
  * After a real source change no retired binding is alive on either path
    (the old scheduler is shut down with its stack). Only on the same stack
    with the Python GPU path may one retired binding stay: an idle
    `viewer/scheduler.py` reader thread keeps the last entry it served in
    its loop local `entry` (whose waiter callbacks reference the binding)
    until its next read. Never one per change; that scenario asserts
    ≤ 1. Clearing those loop locals is outside this whitelist — a separate
    small item for the user to decide.
* Mutation checks: without the detach every scenario keeps 3 retired
  bindings (same stack: 4 children; new source: 3 alive on both paths);
  without the Rust clear the retired cores keep 6 tiles.
