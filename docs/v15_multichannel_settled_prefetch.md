# v15 — Movement-driven multi-channel precompute (design contract)

Status: **design only.** Nothing in this document is wired into the live
viewer. No Save path, no production correction maths, and no worker-count
default is changed by it. The policy layer it specifies lives in
`viewer/prefetch_policy.py` as pure logic with its own tests, imported by
nothing yet; the numbers it needs come from
`scripts/benchmark_multichannel_prefetch.py`.

Every claim below is tagged **measured** (observed on real data in this
repo), **inferred** (derived from a measurement, stated as such), or
**proposed** (a design choice still to be validated).

---

## 1. Why not "generate a corrected pyramid after Save"

Save is the user's final confirmation. Everything this design is for —
free browsing, channel switching, comparing background-correction methods —
happens *before* Save. A post-Save pyramid would make saved data browse
like Odon and would do nothing for the workflow that actually hurts. It
stays on the roadmap; it is not this.

## 2. Three camera states

The scheduler's behaviour is driven by what the camera is doing, not by a
timer alone.

| State | Trigger | What runs | What is cancelled |
|---|---|---|---|
| `RELOCATING` | navigator jump to a far location | move immediately, show raw; current channel + current viewport at top priority | the old location's not-yet-started work |
| `MOVING` | drag / zoom in progress | current channel only: visible viewport, movement buffer, the levels the zoom needs | neighbour- and far-channel work from the old origin |
| `SETTLED` | still for ~200 ms | establishes a new origin, starts the background queues in §3 | nothing; this is the state that *creates* work |

Rules that hold in every state:

- Work already **started** is never cancelled. It finishes and its result
  is kept in cache. **measured**: `TileScheduler._run_compute` writes every
  completed corrected tile into `corrected_cache` even when all its waiters
  have gone stale, so a cancelled generation still leaves usable data.
- Cancellation only drops **queued** work. **measured**: the scheduler's
  worker drops a queued item only when every waiter's generation is stale.
- No hover prefetch in `RELOCATING`. A jump is a deliberate move; guessing
  around it wastes the very resources the jump needs.

**Known trap, do not repeat it.** A previous change drove display state off
a `_viewport_zooming` flag that is only recomputed when a range event
arrives. When the user stops moving no further event comes, the flag stays
at whatever the last event set, and anything reading it as "a gesture is in
progress" stays wrong indefinitely. That shipped as a visible fault and was
reverted (`c505f65`). The state machine here must derive `SETTLED` from an
explicit timer that fires *after* input stops, never from the absence of
events, and `viewer/explore_view.py::_clear_zoom_gesture_state` is the
existing precedent.

## 3. Priority classes (produced only in `SETTLED`)

| Class | Content |
|---|---|
| `P0` | current channel, current viewport |
| `P1` | current channel, enlarged movement buffer |
| `P2_HOT` | neighbouring channels in order `i-1, i+1, i-2, i+2`, current viewport first |
| `P3_COVERAGE` | remaining channels from both ends inward — `0, N-1, 1, N-2, …` — skipping completed ones |
| `P4` | surrounding regions of other channels, only after a long dwell |

**proposed** budget split: `P2_HOT` receives ~75% of background compute
opportunities, `P3_COVERAGE` ~25%, implemented as a deterministic
interleave (three HOT, one COVERAGE) rather than anything randomised, so it
is testable. When the user interacts, or clicks a channel that is not
ready, `P0` may take everything.

These are **logical priorities inside the one existing scheduler**, not new
processes and not new threads. `TileScheduler` already serves two
priority min-heaps and dedups by identity key, which is the whole mechanism
needed. **inferred** from the current design: adding a process would
duplicate the raw cache and the CUDA context for no measured benefit; the
benchmark is what would justify one, and until it does, one process stands.

## 4. Channel switching

- Neighbour already complete → show corrected immediately.
- Not complete → raw shows immediately, that channel is promoted to `P0`,
  and the whole viewport is replaced by corrected once it is covered, not
  tile by tile. **measured** rationale: a corrected tile computed at a
  coarser level differs from its finer neighbours by 15–25% and no scalar
  gain or radius choice fixes it, so mixed-source viewports read as blocks.
- The clicked channel becomes the new centre `i`; the HOT order is
  regenerated around it.
- Rapid switching is latest-request-wins.

## 5. Sharing raw between methods

Top-hat and cuCIM differ only in the kernel; both consume the same
halo-padded raw region. **proposed**: for the same channel and region,
stage the raw once and run both kernels from it, and never queue the same
raw tile twice to serve two methods. **measured** context: a corrected tile
costs ~5.8 ms cold, of which ~3.4 ms is I/O and ~0.9 ms is the GPU kernel,
so the shared input is the expensive half. Whether sharing actually reduces
I/O is item 11 of the benchmark and is **not** assumed here.

Constraint: this must not change the production algorithm, its parameter
semantics, the halo width, or cache identity. `method_overlap()` remains
the single source of halo width, and `BG_CORRECTION_ALGO_VERSION` remains
part of tile identity.

## 6. Cache identity and invalidation

A corrected tile's identity already carries source, channel, tile address,
method, level-scaled effective params, algorithm version and quality. Any
change to source, channel, method, params or level must invalidate exactly
the affected tiles and nothing more — the existing
`_precise_key_current_for_level` predicate is the single definition of
"this tile is current" and must not be duplicated.

## 7. Memory budget

**measured** on this machine: 125 GB system RAM with 106 GB available, and
an RTX 4090 with 48 GB VRAM of which 1.9 GB is in use.

**measured** tile sizes at a 512 grid: corrected `float32` 1.05 MB, display
`uint8` 0.26 MB, raw `uint8` 0.26 MB.

**inferred** working-set sizes:

| Set | float32 | uint8 |
|---|---|---|
| 57 ch x 1 method x 20 tiles | 1.20 GB | 0.30 GB |
| 57 ch x 2 methods x 20 tiles | 2.39 GB | 0.60 GB |
| 57 ch x 2 methods x 36 tiles | 4.30 GB | 1.08 GB |

The premise that 2 GB "will not fit" assumed a tight budget. It is not
tight here: the full two-method 57-channel viewport set is 2.39 GB against
106 GB available. **proposed**: raise the corrected cache from its current
512 MB to a configured budget in the 4–8 GB range and keep `float32`
throughout, rather than introducing a hot/cold split whose only
justification would be a memory pressure that this machine does not have.

If a tiered cache is wanted anyway (for smaller machines), the boundary
must be explicit:

- current channel and the ±2 HOT channels stay `float32`;
- other prepared channels may hold display `uint8` only;
- **`uint8` is preview-only and must never reach a Save result.** The
  quantisation carries the per-level display gain, which is an interactive
  display artefact by construction and is not production numerics.

**measured** and relevant: the corrected cache never evicted during 60–100
step drags, peaking at 165 MB of its 512 MB — so eviction is not currently
a live problem at one channel. Fifty-seven channels is a different regime
and is exactly what the benchmark exists to measure.

## 8. Acceptance targets

These are the reviewer's targets. They are **not** claimed to be met, and
nothing in this round may report them as met without a measurement behind
it.

- ±1 neighbours: p95 <= 500 ms
- ±2 neighbours: p95 <= 1 s
- clicked far channel: raw immediately, corrected viewport p95 <= 2 s
- background work must not degrade the visible tile's first-tile or
  full-coverage time by more than 10%
- no unbounded queue, no unbounded memory growth
- once the user moves again, the old background queue must stop consuming
  the GPU

## 9. What this round does NOT do

No live-viewer integration, no Save changes, no production correction maths
changes, no Rust, no extra process, no corrected pyramid. The deliverables
are this contract, the pure policy layer with tests, and the benchmark.
Integration is decided after the benchmark is reviewed.
