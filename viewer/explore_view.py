"""Step0 Explore: fixed-world-coordinate per-tile pyqtgraph view + controller.

See docs/v15_step0_explore_integration.md (authoritative design) and the
architecture-correction task that produced this rewrite. This module
implements ONLY: ExploreView (the per-tile-item widget) and
ExploreController (viewport tracking, tile requesting, settle debounce,
provisional-state management, teardown). Explicitly OUT of scope here:
Step0Page mounting, Compare 2x2, idle prefetch, OpenGL.

## Odon-style per-tile rendering (replaces the earlier mosaic-canvas design)

Every delivered tile becomes its OWN `pg.ImageItem`, placed ONCE in the
full-resolution (level-0) WORLD coordinate system via `setRect` computed
from its own (level, tx, ty) using UNROUNDED, PER-AXIS downsample factors
(`provider.level_downsample_yx`). Items never move and are never migrated
into a shared canvas; there is no mosaic reallocation. A `TileItemPool` per
layer (raw, precise) keys items by `(level, tx, ty)` and updates an
existing item's pixels in place (`setImage`) when a newer result lands for
the same address, instead of creating a new item. The pinned overview stays
a single always-resident `pg.ImageItem`.

## World coordinate system

A tile at pyramid level L with top-left (y0, x0) in level-L pixels is drawn
via `setRect(QRectF(x0*ds_x, y0*ds_y, w*ds_x, h*ds_y))` where
`(ds_y, ds_x) = provider.level_downsample_yx(L)` -- unrounded, independent
per axis, so non-square-ratio pyramids and long tile runs never accumulate
drift. Level switches therefore never shift already-drawn content, and
`jump_to()` addresses level-0 coordinates directly.

Every `pg.ImageItem` (overview, raw-pool, precise-pool) is constructed with
`axisOrder="row-major"` EXPLICITLY -- never relying on the process-global
`pg.setConfigOptions(imageAxisOrder=...)`, which a standalone script (or a
future host window) might not have set, producing a transposed render.

## Level switching without clearing; active draw set

Switching the displayed pyramid level does NOT clear previously-drawn
items from either pool -- their world rects remain correct (they were
computed from their own level's geometry) so they stay visually aligned;
only their z-order and VISIBILITY change relative to the new level's
tiles. Each pooled item's zValue is `layer_base_z + (num_levels - level)`,
so a FINER level (smaller `level` int, higher resolution) draws ABOVE a
coarser one within the same layer -- caching is not visibility, and this
z-order fact is exactly why an item finer than the current level can never
be allowed to stay visible: it would draw on top and show stale,
differently-corrected pixels (this was bug: a leftover fine-level tile
sitting at the viewport center after zoom-out never got pruned because it
was inside the viewport, and it stayed both cached AND visible forever).
`TileItemPool.apply_visibility(current_level, ...)` is the active-draw-set
policy that fixes this: an item strictly FINER than `current_level` is
ALWAYS hidden (never drawn, regardless of budget/viewport); an item AT
`current_level` follows the caller's `current_level_visible` flag; an item
COARSER than `current_level` (a fallback while the current level fills in)
follows `coarser_visible`. Hiding is independent of pooling/pruning --
a hidden item stays cached and is instantly re-shown the moment zooming
back in makes it the current (or a coarser-fallback) level again. Pruning
(eviction from the pool entirely) still only happens once the per-layer
budget is exceeded, for items both off-level and outside the viewport
(with margin).

## Anti-checkerboard for precise tiles (design doc §1.2 / cheap group gate)
## + corrected floor + single-stage motion guarantee

Corrected (precise) tiles are brightness-normalized per-tile relative to
their own local background; sitting a corrected tile next to a raw tile
produces a visible cross-stage seam. `_coverage_complete()` still computes
a single cheap boolean -- "every tile in the CURRENT wanted set (at the
current level, under the current selection context) has a matching,
current `CorrectionKey` recorded against it" -- and `_precise_visible` /
`view.precise_visible` still mean exactly that; `_maybe_exit_provisional`
still uses it for the provisional badge. What CHANGED is what that boolean
is used to gate.

An earlier round used `covered` as an ATOMIC visibility gate on the whole
current level: any single missing tile (e.g. one more tile entering the
wanted set mid-pan) hid EVERY current-level precise item at once, dropping
the whole viewport down to the coarser fallback and snapping back --
visible as a whole-viewport flash on every camera motion, worse than the
bug it was meant to prevent. That was only ever justified as anti-
checkerboard protection against a corrected tile sitting next to a RAW
tile (a hard cross-stage seam). It was NOT protection against a corrected
tile sitting next to another CORRECTED-stage image (a coarser precise tile
or the corrected floor) -- that is a sharpness difference, not a stage
seam, and was always fine to show.

So the atomic gate now applies ONLY while raw pixels can still reach the
screen, i.e. only until the corrected floor is ready (`floor_ok` below):

    current_level_visible = True if floor_ok else covered

Once the floor is ready the raw layer is forced entirely invisible (next
paragraph), so whatever sits under a missing current-level precise tile is
itself corrected-stage -- floor, or a coarser precise tile -- and current-
level tiles are shown PROGRESSIVELY, per tile, the moment each one lands
(still gated by `key_ok=_precise_key_current_for_level`, so a shown tile
can never be stale-method/stale-radius). This is a DELIBERATE trade made
after manual testing, not an oversight: newly-exposed regions may briefly
show the coarser floor beneath already-sharp neighboring tiles (a visible
sharpness boundary) in exchange for the viewport never flashing all at
once. Before the floor is ready, the old atomic behavior is unchanged
(anti-checkerboard against raw still holds), and COARSER-level precise
items are, as before, exempt from `covered` entirely and stay visible
underneath as a sharper-than-floor fallback while the current level fills
in -- they are the same (corrected) stage as the current-level tiles, so
there is no cross-stage seam, only a sharper/blurrier transition.

The raw layer holds a different, single-stage pixel pipeline than the
precise layer, so raw-next-to-precise is itself a seam. When no correction
method is selected the raw layer is simply always shown (there is nothing
else to show). When a method IS selected, the raw layer must never be the
thing the user sees mixed with corrected pixels during motion -- that was
the other bug: `_update_precise_visibility` used to hide only the ENTIRE
precise layer on incomplete coverage, which let the always-visible raw
layer show through and made the whole image flash brighter every time the
camera moved. The fix is a "corrected floor": `ExploreController`
maintains one extra always-covering item, `view.corrected_floor_item`
(z=`FLOOR_Z`, between the raw overview at z=0 and the raw tile pool at
`RAW_BASE_Z`) -- a whole-array correction of a coarse pyramid level
(`_pick_floor_level_and_stride`), computed off the GUI thread and requantized whenever
selection changes. Once that floor is ready for the current selection
context, it is shown and the ENTIRE raw layer (both current- and
coarser-level items) is forced invisible via `apply_visibility`, so the
screen shows only corrected-stage pixels: floor -> coarser corrected tiles
-> current-level corrected tiles, in increasing sharpness, and NEVER a
raw-stage pixel. The raw layer is the honest fallback only until the floor
is ready for the very first time after a selection change (better than a
black screen); the always-resident raw overview (z=0) is the ultimate
never-black-screen guarantee underneath everything, but once the floor
covers the world rect it is never actually visible.

The floor is an INTERACTIVE-quality DISPLAY PROXY ONLY -- it is never
claimed to match production numerics; it exists solely so the screen never
shows raw-stage pixels while in corrected mode. When no pyramid level is
both >= `FLOOR_MIN_MAX_DIM` on its long side and under the `FLOOR_MAX_PIXELS`
safety cap, `_pick_floor_level_and_stride` falls back to decimating the
coarsest big-enough level by an integer stride `k` -- via an area/box-mean
downsample (`_box_downsample`), not point-sampling, so a bright structure
that happens to land off the sample grid is never missed or over-weighted
-- so the correction kernel still runs on a bounded number of pixels; the
floor's
effective per-axis downsample becomes `(ds_y*k, ds_x*k)` and the
correction param is scaled by the same total factor, so the decimated
floor still lands exactly on the full world rect at the right physical
scale.

A stale coarser-level precise tile is exactly the same class of bug as a
stale raw tile: `TileItemPool.apply_visibility` accepts an optional
`key_ok(entry.key, entry.level)` predicate so the precise pool can ALSO
require a coarser entry's `CorrectionKey` to still match the current
selection context (recomputed at THAT entry's level, since
`effective_param` is level-dependent) before it is allowed to stay
visible -- an old-method/old-radius coarse tile is hidden immediately on
a selection change, never just faded back in as a stale fallback. The raw
pool needs no such predicate: `RawKey` carries no method/params, and
source/channel mismatches are already rejected at delivery time.

Only ONE corrected-floor computation ever runs at a time
(`_floor_job_running`). A selection change that arrives while a floor job
is in flight does not start a second one -- it bumps `_floor_gen` (so the
in-flight job's result is dropped as stale on arrival, by the same
generation/context guard as `_handle_precise_result`) and coalesces into a
single pending flag; when the stale result lands, the pending job (using
whatever selection is CURRENT at that point, i.e. always the latest) is
what actually starts. This guarantees the floor path never launches two
overlapping GPU/CPU-bound corrections.

## Camera contract: no debounce on the range handler itself

The ViewBox's native mouse pan/wheel interaction is completely unmodified
(pyqtgraph's default: wheel zooms about the mouse cursor). `sigRangeChanged`
runs a CHEAP handler on every single event: recompute the wanted tile set
and run the (cheap) item-pool prune check. Nothing here touches the
scheduler. Actual REQUEST ISSUING -- for BOTH the raw layer and the
interactive precise (corrected) layer -- is what gets debounced: a 30ms
single-shot "motion" timer (`_motion_timer`, `MOTION_MS`) coalesces a burst
of range-change events into one batch of requests, cancelling each layer's
previous now-stale generation token (`("raw", n)` / `("precise", n)`
respectively). This is what keeps a fast wheel-zoom or drag from flooding
the scheduler's ready-queue while never fighting the camera.

Earlier, interactive precise tiles were requested ONLY from a separate 80ms
`settle_ms` timer that `_on_range_changed` restarted on every single range
event -- so during a continuous drag the settle timer never fired, and no
corrected tile was computed until the user stopped moving entirely; a newly
exposed viewport edge showed only the coarse corrected floor for the whole
duration of the gesture.

Measured on the real slide (tonsil, channel index 1, tophat radius 25, a
20-tile viewport), time from drag-stop to full current-level corrected
coverage, with the number of corrected tiles computed DURING the drag and
the fraction of the final viewport already covered at the moment the drag
stopped:

    10-step drag: settle-gated 311ms (0 computed during, 8/20 covered)
                  this policy  222ms (3 computed during, 11/20 covered)
    40-step drag: settle-gated 561ms (0 computed during, 0/20 covered)
                  this policy  243ms (23 computed during, 7/20 covered)

The gain grows with drag length because the settle-gated policy computed
LITERALLY NOTHING while the camera moved, so a longer gesture just meant a
longer stall afterwards; this policy stays roughly flat.

One corrected tile costs ~5.8ms cold (3.4ms IO + 0.9ms GPU kernel) and
~1.1ms with its raw halo already cached, yet the end-to-end cost per tile
at drag-stop is ~24ms. The remainder is pipeline latency: a corrected
tile's halo is staged through the raw I/O pool before its kernel runs, so
the number of I/O workers, not the kernel, sets the pace.

CORRECTION, and do not quote the earlier claim: an earlier revision of
this docstring said raising `compute_workers` from 1 to 4 "changed the
measurement by ~1ms, so extra compute workers are not a lever". That was
measured with `io_workers=1`, where the single I/O worker starved the
compute workers, so it only proved that compute workers cannot help while
I/O is the bottleneck. Re-measured properly, on the fraction of the
viewport already sharp DURING a drag (see "Worker counts" below), both
knobs matter a great deal. So both layers now
issue from the SAME 30ms coalescing timer (`_issue_raw_requests`, which
also drives the precise layer's `_issue_settled_request`), and each issuing
pass requests only tiles that are actually MISSING -- for precise, a
visible tile whose pooled `CorrectionKey` does not match the live selection
context (`_key_matches_context`); for raw, a visible tile with no pool
entry at all. A tile that is already cached or already in flight needs no
special handling: the scheduler's cache check and single-flight dedup
(`TileScheduler._pending`) cover both, so requesting a tile redundantly is
harmless, just wasted call overhead.

`_settle_timer` / `settle_ms` still exist, but no longer gate any
interactive request issuing -- `_on_settle` is kept as an explicit,
documented hook for a FUTURE higher-quality / native (non-interactive)
refinement pass that has not been built yet. Do not route interactive
corrected-tile issuing back through the settle timer.

## Worker counts (`io_workers` / `compute_workers`)

These are the largest measured lever on perceived sharpness during motion,
and they are easy to get wrong, so the numbers are recorded here.

The metric is the fraction of the CURRENT viewport already covered by
current-level tiles, sampled at every step DURING a 25-step drag or a
12-step zoom -- i.e. "is the newly exposed edge already sharp as it comes
on screen". Drag-stop-to-full-coverage cannot show this. Real slide,
tonsil, channel index 1, tophat radius 25, 1400x1000 viewport:

    config          drag/tophat   zoom/tophat   drag/raw
    io=1  cw=1        52.4%         25.0%        67.5%
    io=4  cw=1        63.0%         60.9%          --
    io=4  cw=2        87.5%         53.1%        87.2%
    io=8  cw=4        89.8%         77.8%        86.8%

`TileScheduler`'s own default is already `io_workers=4`; it was the demo
and probe SCRIPTS that pinned `io_workers=1, compute_workers=1`, so every
manual test until now ran with one I/O thread. Raising both is what took
in-motion coverage from about half the viewport to about nine tenths.

Correctness of the parallel path was verified, not assumed: 24 corrected
level-0 tiles computed through the scheduler at `io=8 cw=4` are BYTE-
IDENTICAL to the same tiles at `io=1 cw=1`, with no errored results.

A symmetric halo prefetch (Odon's `prefetch_spec`/`prefetch_keys_for_level`
policy, ported faithfully including its load-gate tiers) was implemented
and MEASURED TO BE A REGRESSION at `io=1 cw=1`: drag/tophat 51.9% -> 39.7%,
drag/raw 70.1% -> 56.8%. Priority ordering only orders the QUEUE; it
cannot preempt work already started, and a prefetched corrected tile holds
the single compute worker while blocking on its own halo staging. A
raw-only variant was no better (drag/raw 60.2%). Prefetch was therefore
NOT merged. If it is revisited, it must be re-measured on top of the
raised worker counts, where there is genuine spare capacity.

## Generation namespaces

`view_generation` (raw layer) and `_settled_generation` (precise layer) are
independent counters. To guarantee they can never collide (a fast zoom
cancelling view-generation 5 must never accidentally cancel a live
precise-generation 5), every generation token handed to the scheduler is a
namespaced tuple: `("raw", n)` / `("precise", n)`. `TileScheduler` treats
generation tokens purely by hashable-equality, so this requires no
scheduler changes beyond the type hint/docstring already updated there.

## GUI-side delivery guard

A scheduler callback may fire on a worker thread; it is marshalled to the
GUI thread via a Qt QUEUED connection. A queued signal can therefore be
sitting in the event queue at the moment its generation gets cancelled (the
cancellation itself runs on the GUI thread, synchronously, before the
queued slot executes). Every delivery slot re-checks, on the GUI thread,
at handling time: (a) the token still matches the CURRENT generation (not
merely "not yet marked stale" -- an exact-match check against the live
counter, which is strictly stronger and immune to the queued-signal race),
(b) the tile address is still in the CURRENT wanted set for its layer, and
(c) source/channel/level still match. Any failure drops the result and
bumps a counter; nothing is ever forced onto a stale layer.

## Per-level display gain for CORRECTED pixels (interactive display only)

Background correction is a high-pass operation, and a downsampled pyramid
level has already lost the high frequencies it acts on -- so the SAME
tissue, corrected at different pyramid levels with the level-scaled radius
(`effective_param`), comes out at systematically different brightness.
Measured on the real tonsil slide (channel index 1, tophat radius 25, a
4096x4096 level-0 tissue window): corrected mean drops from 3.69 (level 0,
effective radius 25) to 2.29 (level 1, radius 6) to 1.33 (level 2, radius
2) to 0.46 (level 3, radius 1); corrected p99.5 drops from 16 to 13 to 9 to
5 over the same levels. Every corrected image (tiles and the floor) is
quantized through one fixed display range derived from the RAW overview,
so the floor's level renders noticeably darker than level 0 -- a brightness
jump whenever the displayed level changes or the floor shows through. No
radius tuning fixes this: the signal genuinely is not present at coarse
levels.

`ExploreController._level_gain` is a per-pyramid-level multiplicative gain,
calibrated once per selection (`_calibrate_level_gains`, module-level
constants `GAIN_WINDOW_L0`/`GAIN_WINDOWS`/`GAIN_PERCENTILE`/`GAIN_CLAMP`)
and applied ONLY when quantizing CORRECTED pixels (`_quantize_corrected_
uint8`, used by the precise-tile and floor delivery paths) -- never to raw
pixels, which vary only ~1.2x across levels and carry no correction. The
table is installed only alongside a successful, context-matching corrected-
floor result (`_handle_floor_result`); `_display_gain_for_level` returns
1.0 for every level whenever the live selection no longer matches the
context the table was calibrated for, so a stale or absent table never
silently scales pixels.

Read the following plainly, and do not extend these claims:

- This is an INTERACTIVE DISPLAY gain only. It never touches computed,
  cached, or saved pixel values -- the preview's brightness therefore does
  NOT match production output.
- It aligns HIGHLIGHTS (a p99.5 ratio between level 0 and level L), not the
  whole distribution. Coarse levels stay darker in the midtones because the
  signal is genuinely absent there -- measured: level 2's corrected p50 is
  0 where level 0's is 3.
- Expected effect: the ~2.8x mean mismatch between the floor level and
  level 0 drops to roughly 1.2-1.5x. It is NOT eliminated.
- The calibrated gain varies by about 25% depending on which tissue window
  is sampled; the median over `GAIN_WINDOWS` (3) windows is a variance
  reduction, not a guarantee.

Calibration runs on the SAME worker thread as the existing corrected-floor
job (`_start_floor_job`'s `work()` closure) -- there is no second thread or
second single-flight mechanism; the floor's generation/context guard
(`_floor_gen` / `_floor_job_running` / `_floor_pending`) covers calibration
too. A calibration failure is caught, leaves the gain table empty (all
1.0), increments `stats["gain_calibration_failed"]`, and never costs the
floor itself -- a successful floor is installed regardless of whether
calibration succeeded.

## Intermediate corrected fallback

Measured during a level-0 tophat drag, on-screen composition was
current-level 91.2%, coarser-level 0.0%, floor 8.8% (fallback p95 20.0%);
raw mode's equivalent was current 88.6%, coarser 0.0%, floor 11.4%
(fallback p95 20.0%). The fallback AREA is the same in both modes, so the
difference is fallback QUALITY: with no intermediate level pooled, an
uncovered corrected tile drops straight from level 0 to the level-2 floor
-- 16x blur, and a 1.35x-1.72x brightness mismatch even after the per-level
display gain. Raw mode's fallback (the raw overview) is the same 16x blur
but only ~1.24x off, and corrected images are dark and sparse, so a
brighter blurry patch on a dark field is far more visible than the
equivalent raw mismatch.

A level-1 fallback is measurably closer on both axes: 4x blur instead of
16x, and after its own display gain the mismatch against level 0 is
1.14x-1.21x in the mean and 1.03x-1.11x at p90 (versus level 2's
1.35x-1.45x and 1.19x-1.26x). Covering a 20-tile level-0 viewport takes
only about 5-9 tiles at level 1.

`ExploreController.intermediate_corrected_fallback` (default True,
settable via the constructor for A/B testing, mirrored by
`scripts/explore_demo.py --intermediate-fallback` / `--no-intermediate-
fallback`) requests, on every motion-timer tick, corrected tiles at
`self.level + 1` covering the same viewport IN ADDITION to the current
level's tiles -- letting them be drawn as the coarser fallback the
existing visibility policy (`TileItemPool.apply_visibility`) already
supports. There is no two-level ladder: when `self.level + 1 >=
provider.num_levels` there is no fallback level and this is a no-op. Only
meaningful when `_wants_precise()` -- raw mode already keeps coarser raw
tiles.

`_issue_settled_request` issues, in order: (1) the intermediate fallback
batch at `level + 1`, priorities starting at 0, then (2) the current
level's precise batch, priorities starting at `PRECISE_CURRENT_BASE_
PRIORITY = 100`. The fallback batch is only 5-9 tiles, so putting it first
costs the current level almost nothing, and it buys a complete, visually
consistent underlay in roughly 15ms at 4 compute workers instead of
letting 20 current-level tiles uncover the floor one at a time. No
dedicated compute slot is reserved for the fallback batch: it does not
need one, since it is issued first at strictly better (lower-numbered)
priority and is only a handful of tiles, so the scheduler's min-heap
cannot starve it behind the much larger current-level batch.

Both batches keep the existing "missing tiles only" filter, each against
its OWN level: the fallback level uses `_precise_key_current_for_level`
(which recomputes `effective_param` for that level); the current level
keeps `_key_matches_context` as before. `_make_correction_key(tx, ty,
level=...)` takes an explicit level (defaulting to `self.level` so
existing call sites are unchanged) and derives `effective_param` from THAT
level's downsample -- the fallback level's tiles carry the same source
identity, channel, method, algorithm version and quality as the current
level's; only the level and the level-scaled effective params differ.
Both batches share the existing `("precise", n)` generation and its
cancel/bump discipline; there is no new namespace.

On delivery, `_handle_precise_result` accepts a tile at `self.level + 1`
(in addition to `self.level`, its previous only acceptance) when
`intermediate_corrected_fallback` is enabled, subject to ALL of: the
generation still matches the live `_settled_generation` exactly; the key
is current FOR ITS OWN LEVEL via `_precise_key_current_for_level(key,
tile.level)` (not `_key_matches_context`, which is bound to the current
level); and the tile is a member of `self._fallback_visible_tiles` (the
fallback-level tile set computed the same way the issuing path computed
it, at issue time, and kept on the controller for exactly this membership
test) with `tile.level == self._fallback_level == self.level + 1`. An
accepted fallback tile is quantized with `_quantize_corrected_uint8(arr,
tile.level)` -- its OWN level's display gain -- placed at its own level's
`world_rect`, and pooled at its own level, counted in
`stats["mid_tiles_blitted"]` (fallback requests issued are counted in
`stats["mid_requests_issued"]`). Anything rejected still lands in the
scheduler's cache (automatic) but is never blitted.

`selection_key_context()`, `_coverage_complete()`, `_precise_visible` /
`view.precise_visible` and the provisional badge stay defined against the
CURRENT level only -- a fallback tile never counts toward "the viewport is
covered". The active-draw-set rule is likewise unchanged: a fallback tile
is coarser than the current level, so it can never draw above a
current-level item (module docstring "Level switching without clearing";
`TileItemPool`'s z-order guarantees this, unchanged here). The corrected
floor, the raw layer suppression rule, and the gain machinery are
untouched.

One welcome consequence: after a zoom OUT, the fallback tiles computed for
`level + 1` become the new current level's tiles and are already valid,
since `_precise_key_current_for_level` validates per level -- no extra
work is required to make a zoom-out land on already-sharp tiles.

## Synthesized coarse fallback

Top-hat is a non-linear morphological operation, so downsampling and
correcting do not commute:

    A   = tophat computed AT level 1            (what the fallback shows today)
    REF = tophat computed at level 0, then box-downsampled to level-1 scale
          (what an adjacent level-0 tile looks like at the same scale)

Measured on the real slide (channel index 1, radius 25, 2048-px level-0
windows, three windows per group, both put through the viewer's own
per-level display gain):

    tissue interior: mean brightness gap 18.4%, per-pixel |A-REF| 19.5%, p95 38.8%
    tissue edge:     mean brightness gap 26.4%, per-pixel |A-REF| 27.1%, p95 49.9%

Edges are worse, matching the user's report that blockiness is worst near
tissue borders, but the gap is large everywhere. It cannot be fixed by
tuning the display gain: giving each window its OWN optimal scalar gain
(1.36-1.63, against the calibrated 1.14) left |A-REF| at 18.4% interior /
24.8% edge -- essentially unchanged -- and made p95 WORSE (46.3% / 61.5%).
The difference is structural, not a brightness offset.

But `REF` is, by construction, exactly what a level-0 tile shows. So a
fallback tile SYNTHESIZED by downsampling already-computed level-0 (or,
generally, level `L-1`) corrected tiles matches its neighbours exactly,
with no seam at all -- it IS a downsample of what is already on screen.

`_try_synthesize_fallback_tile(fallback_level, tx, ty)` sources ONLY from
`fallback_level - 1` (no recursion through multiple levels). It requires
EVERY finer-level tile tiling the fallback tile's world area to be present
in `_precise_pool` with a key that is current for that finer level
(`_precise_key_current_for_level`, the same predicate the visibility path
already uses) -- if even one is missing or stale, it returns None: partial
synthesis would leave holes, which is worse than the computed tile the
caller falls back to requesting instead.

The pool stores QUANTIZED uint8 pixels, not the float32 corrected values.
Downsampling the quantized pixels (via `_box_downsample`, requiring an
EXACT integer ratio between the two levels' downsample factors -- the
tonsil pyramid is exactly 4x per level, so a non-integer ratio only
matters for other layouts, and is declined rather than approximated) is
fine here, and is in fact what keeps the result identical to what the
neighbouring tiles display: the synthesized tile must NOT be re-quantized
or re-gained, since it already carries the finer level's calibrated
display gain baked in. Re-gaining it would double-apply the per-level
gain (module docstring "Per-level display gain for CORRECTED pixels").

Used in two places: (1) in `_issue_settled_request`'s intermediate-fallback
batch, before issuing a request for a missing fallback-level tile --
success pools the result directly (with a `CorrectionKey` built for that
level via `_make_correction_key(level=...)`, so it is accepted by
`_precise_key_current_for_level` and invalidated by a selection change
exactly like a computed tile) and skips the request; failure issues the
request exactly as before. (2) in `_on_range_changed`, whenever a range
event's display level INCREASES (a zoom-out or a jump landing on a
coarser level) -- the newly-current level's tiles are attempted via
synthesis from the level that was just current (guaranteed resident,
having just been on screen) before falling through to the existing
cache-serve-or-request path.

HONEST LIMIT: a PAN exposes world area that has no finer tiles at all --
there is nothing to downsample -- so synthesis cannot help there, and the
computed fallback (with its measured 18-27% mismatch above) is still what
shows in the roughly 4% of the viewport the directional prefetch has not
covered. This change targets zoom-out and revisits, not panning.

Stats: `fallback_synthesized` (a synthesis that produced a tile),
`fallback_synthesis_declined` (a synthesis attempt where at least one
source tile was missing or stale, or the level ratio was not an exact
integer).

## Directional prefetch (pan only)

The intermediate corrected fallback (above) removed the harsh level-2
floor from view during a drag, but manual testing found a residual: during
a drag the user still sees level-1 fallback tiles being replaced by level-0
tiles as the camera catches up -- measured, coarser (level-1) fallback
occupies ~14.6% of the screen on average during a 25-step drag. The goal
of this feature is to have level-0 corrected tiles ready BEFORE a pan
brings them on screen, rather than only after they enter the wanted set.

This is PAN-ONLY, gated by `not self._viewport_zooming` (an extension of
the existing `_viewport_shrinking` bookkeeping in `_on_range_changed`: the
viewport's world area changed by more than 0.5% in either direction since
the previous range event). A zoom-in exposes no new level-0 world area
along a predictable line the way a pan does -- there is no direction to
prefetch toward -- and the intermediate fallback batch already handles
zoom's coarser-underlay problem. It is also skipped outright when no
correction method is selected (raw mode already keeps coarser raw tiles
visible; this is not the complaint it addresses).

Direction is estimated from the viewport centre in level-0 world
coordinates, tracked every `_on_range_changed` call. Each motion-timer tick
computes the displacement since the previous tick and smooths it with an
exponential moving average (`DIRECTIONAL_PREFETCH_EMA`); the direction is
only considered valid once the smoothed displacement exceeds
`DIRECTIONAL_PREFETCH_MIN_TILES` current-level tiles. Below that threshold
nothing is issued at all -- deliberately NOT a fallback to a symmetric
ring; see below for why a ring-shaped prefetch is exactly the design that
was already tried and rejected.

The candidate corridor is the current viewport's tile-space rect at
`self.level`, translated forward along the smoothed direction by
`DIRECTIONAL_PREFETCH_CORRIDOR` viewports, unioned with the original rect
and covered by tiles; already-visible tiles and tiles already covered under
the live selection (`_key_matches_context` against `_precise_pool`, the
same predicate the visible path uses) are subtracted, the remainder is
sorted by distance from the leading edge of the viewport along the
direction of travel, and truncated to `DIRECTIONAL_PREFETCH_BUDGET`
candidates.

THE PART THAT MATTERS MOST: a symmetric current-level halo prefetch was
already implemented once, faithfully porting Odon's `prefetch_spec`
policy including its load-gate tiers, and it was MEASURED TO BE A
REGRESSION (module docstring "Worker counts"): drag/tophat coverage
dropped from 51.9% to 39.7%. The reason is structural, not a tuning
mistake: request PRIORITY only orders the scheduler's ready-QUEUE -- it
cannot preempt work that has already STARTED, and a started corrected-tile
job holds a compute worker while it blocks in
`TileScheduler._stage_raw_for` waiting on raw I/O. With as few as one
compute worker, that single stalled prefetch job starved every visible
tile behind it in the queue, priority notwithstanding.

So this feature is bounded by an IN-FLIGHT CAP
(`DIRECTIONAL_PREFETCH_INFLIGHT`, default 1), not by priority alone.
Priority (`DIRECTIONAL_PREFETCH_BASE_PRIORITY`, above every other request
class issued in the same tick) keeps prefetch requests out of the way in
the queue; the cap is what actually keeps prefetch from ever occupying
more than one compute worker at any instant, no matter how fast the camera
moves or how many candidates are queued. Requests are issued one at a
time: each carries a dedicated callback that discards its pixels (this
path is CACHE-ONLY -- a directional-prefetch result is NEVER blitted or
pooled) and, on the GUI thread, issues the next candidate from the list if
its generation is still current. This is deliberate back-pressure, not an
oversight: the queue can never grow faster than the compute path drains
it, unlike the rejected halo design where the whole prefetch batch was
enqueued at once.

Its own generation namespace, `("dirprefetch", n)`, is bumped and
cancelled (dropping the pending candidate list) whenever the smoothed
direction's angle turns by more than 90 degrees, the direction becomes
invalid, the selection changes, or the display level changes -- but work
already dispatched to the scheduler under the OLD generation is left to
run to completion: `TileScheduler._run_compute` writes every completed
corrected tile to `corrected_cache` regardless of whether its waiters are
stale, so a reversed drag re-uses whatever the abandoned prefetch already
computed instead of wasting it.

`ExploreController.directional_prefetch` (default True, settable via the
constructor, mirrored by `scripts/explore_demo.py --directional-prefetch /
--no-directional-prefetch`) is the A/B switch. Stats: `dir_prefetch_issued`,
`dir_prefetch_completed`, `dir_prefetch_cancelled`,
`dir_prefetch_direction_changes`.
"""

import threading
import time
from dataclasses import dataclass, field
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QRectF

# Called through the module, never bound at import time, so a test can
# patch a rule and watch the controller's output follow -- the same
# discipline `multichannel_prefetch` uses for `prefetch_policy`.
from . import request_planning as planning
from .tile_types import (
    CorrectionKey,
    QualityLevel,
    RawKey,
    TileAddress,
    TileGridSpec,
    TileRequest,
    effective_param,
    tiles_covering,
)
from ..core.bg_correction import BG_CORRECTION_ALGO_VERSION


# Sentinel distinguishing "argument not passed" from "argument passed as
# None" for ExploreController.set_selection (an explicit method=None must
# still take effect -- it means "no precise layer").
_UNSET = object()

# Z-order bases: overview (single item) sits at 0; every pooled raw item
# gets `RAW_BASE_Z + (num_levels - level)`, every pooled precise item gets
# `PRECISE_BASE_Z + (num_levels - level)` -- so within a layer a finer
# level always draws above a coarser one, and the precise layer as a whole
# always draws above the raw layer.
RAW_BASE_Z = 100
PRECISE_BASE_Z = 200

# Corrected-floor item z: between the pinned raw overview (0) and the raw
# tile pool (RAW_BASE_Z) -- it must draw above the overview (it is the
# thing that replaces the overview as the never-black-screen fallback in
# corrected mode) but always below any raw tile, in case the raw layer
# is ever forced visible as the honest fallback (module docstring).
FLOOR_Z = 50

# Default per-layer pooled-item budget (design doc: "e.g. 400 per layer").
DEFAULT_ITEM_BUDGET = 400

# Prune-eligibility viewport margin, in TILE units (matches the canvas-era
# cover margin) -- an item within this many tiles of the current viewport,
# even off-level, is kept a little longer to absorb small pans.
PRUNE_MARGIN_TILES = 1

# Corrected-floor level selection (module docstring "corrected floor"):
# among pyramid levels, pick the COARSEST one whose max(h, w) still meets
# FLOOR_MIN_MAX_DIM and whose pixel count stays under the safety cap. If no
# level satisfies both, the coarsest level meeting FLOOR_MIN_MAX_DIM alone
# is decimated by an integer stride to bring it under the cap (see
# `_pick_floor_level_and_stride`).
FLOOR_MIN_MAX_DIM = 1024
FLOOR_MAX_PIXELS = 4_000_000

# Per-level display-gain calibration (module docstring "Per-level display
# gain for CORRECTED pixels"): 3 tissue-dense, non-overlapping level-0
# windows, GAIN_WINDOW_L0 pixels square, calibrated against the p99.5
# highlight percentile, gain clamped to GAIN_CLAMP.
GAIN_WINDOW_L0 = 2048
GAIN_WINDOWS = 3
GAIN_PERCENTILE = 99.5
GAIN_CLAMP = (1.0, 8.0)

# Intermediate corrected fallback (module docstring): priority floor for the
# CURRENT level's precise batch, so the fallback batch at `level + 1`
# (priorities starting at 0) always sorts ahead of it in the scheduler's
# min-heap without needing a reserved worker slot.
PRECISE_CURRENT_BASE_PRIORITY = 100

# Look-ahead ring, in FALLBACK-LEVEL tiles, around the viewport at the
# intermediate fallback level (module docstring "Intermediate corrected
# fallback"). One tile is enough because a fallback-level tile already
# covers several times the world area of a current-level tile.
FALLBACK_HALO_TILES = 1

# Priority base for the SPECULATIVE part of the fallback batch (the
# look-ahead ring). Strictly above PRECISE_CURRENT_BASE_PRIORITY, so the
# ring can never delay a tile the user is looking at right now.
FALLBACK_RING_BASE_PRIORITY = 1000

# Directional prefetch (module docstring "Directional prefetch (pan
# only)"): candidate tiles per direction, tunable.
DIRECTIONAL_PREFETCH_BUDGET = 48
# At most this many directional-prefetch requests in flight at any instant
# -- the mechanism that actually bounds compute-worker occupancy (priority
# alone cannot preempt started work; see module docstring).
DIRECTIONAL_PREFETCH_INFLIGHT = 4
# Movement threshold, in CURRENT-LEVEL tiles, below which the smoothed
# direction is considered invalid and nothing is issued.
DIRECTIONAL_PREFETCH_MIN_TILES = 0.15
# Exponential-moving-average smoothing factor for the per-tick displacement.
DIRECTIONAL_PREFETCH_EMA = 0.5
# Priority base for directional-prefetch requests -- strictly above every
# other request class issued in the same tick (raw, intermediate-fallback
# urgent/ring, current-level precise).
DIRECTIONAL_PREFETCH_BASE_PRIORITY = 2000
# How far ahead the candidate corridor is swept, in VIEWPORTS, along the
# smoothed direction of travel.
DIRECTIONAL_PREFETCH_CORRIDOR = 1.0


def _pick_calibration_windows(overview_arr: np.ndarray, ds_y: float, ds_x: float,
                               window_l0: int = GAIN_WINDOW_L0,
                               n_windows: int = GAIN_WINDOWS):
    """Pick up to `n_windows` tissue-dense, NON-OVERLAPPING `window_l0`
    (level-0 pixels) square windows using block means over the already-
    resident `overview_arr`. Blocks are laid out on a simple non-
    overlapping grid at the window size expressed in overview-level pixels
    (`window_l0 / ds_*`), scored by mean, and the highest-scoring blocks are
    returned as level-0 top-left corners `(y0, x0)`. Falls back to one
    window, centred and clamped to the image, when the window does not fit
    the overview at all (a tiny image) or the overview array is empty."""
    if overview_arr is None or overview_arr.size == 0:
        return []
    h, w = overview_arr.shape
    win_h = max(1, min(h, int(round(window_l0 / ds_y))))
    win_w = max(1, min(w, int(round(window_l0 / ds_x))))
    n_by = h // win_h
    n_bx = w // win_w

    if n_by < 1 or n_bx < 1:
        y0 = max(0, (h - win_h) // 2)
        x0 = max(0, (w - win_w) // 2)
        return [(int(round(y0 * ds_y)), int(round(x0 * ds_x)))]

    scores = []
    for by in range(n_by):
        for bx in range(n_bx):
            y0, x0 = by * win_h, bx * win_w
            block = overview_arr[y0:y0 + win_h, x0:x0 + win_w]
            scores.append((float(block.mean()), by, bx))
    scores.sort(key=lambda t: t[0], reverse=True)

    windows_l0 = []
    for _score, by, bx in scores[:n_windows]:
        y0_l0 = int(round(by * win_h * ds_y))
        x0_l0 = int(round(bx * win_w * ds_x))
        windows_l0.append((y0_l0, x0_l0))
    return windows_l0


def _box_downsample(arr: np.ndarray, k: int) -> np.ndarray:
    """Area/box-mean downsample by integer stride `k` on both axes: crop to
    a `(h // k * k, w // k * k)` multiple, reshape to `(h//k, k, w//k, k)`,
    and mean over the two `k`-sized axes -- unlike point-sampling
    (`arr[::k, ::k]`), this cannot miss or over-weight a bright structure
    that happens to land off the sample grid. `k == 1` returns `arr`
    unchanged (no-op). Always returns a C-contiguous float32 array.

    NOTE for the real tonsil pyramid this path is INACTIVE: the floor
    level there is 1963x1800 = 3.53 MP, under `FLOOR_MAX_PIXELS`
    (4,000,000), so `_pick_floor_level_and_stride` picks `k == 1` and this
    function is a no-op. This change is robustness for OTHER pyramid
    layouts (where no level is small enough to skip decimation), not a fix
    for any currently-observed artifact on the tonsil data."""
    if k <= 1:
        return np.ascontiguousarray(arr, dtype=np.float32)
    arr = arr.astype(np.float32, copy=False)
    h, w = arr.shape
    h_crop, w_crop = (h // k) * k, (w // k) * k
    cropped = arr[:h_crop, :w_crop]
    reshaped = cropped.reshape(h_crop // k, k, w_crop // k, k)
    return np.ascontiguousarray(reshaped.mean(axis=(1, 3), dtype=np.float32))


# Overview-record cache budget. A record on the real 57-channel slide is
# 1980x1680 float32 = 13.3 MB, so all 57 would be ~758 MB; 256 MB holds
# about 19 of them -- the current channel plus a +-2 HOT neighbourhood with
# room to spare. Budgeted in BYTES rather than a record count on purpose: a
# very large WSI with few pyramid levels can have an overview level far
# bigger than 13.3 MB, and a fixed count would silently blow the memory
# budget there. At least one record is always kept, whatever its size.
OVERVIEW_CACHE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class OverviewRecord:
    """One channel's overview level, with the display range derived FROM it.

    Identity is `(source, channel, level)`. The display range travels with
    the pixels deliberately: `display_lo`/`display_hi` come from this
    array's own percentile, so installing a record means installing a
    matched (pixels, range) pair. Installing pixels and keeping a previous
    channel's range would misrepresent brightness just as surely as showing
    the wrong pixels.

    Why it exists: a channel switch has to know this channel's display range
    before it can show anything, and reading the level costs p50 132.7 ms /
    p95 292.9 ms on the real 57-channel slide -- a visible GUI stall. Having
    corrected tiles cached does NOT avoid it, so "a neighbour channel is
    ready" is only true if its overview record is ready too.
    """

    source: object
    channel: str
    level: int
    arr: object
    display_lo: float
    display_hi: float
    shape: Tuple[int, int]


# ── Interaction snapshot ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrefetchSnapshot:
    """An IMMUTABLE picture of the selection + viewport at the instant an
    interaction event fired.

    Background consumers (the multi-channel prefetch controller) must plan
    from one of these and never read the controller's private fields while
    they run: by the time a background batch starts, the live fields may
    describe a different channel, method, level or viewport, and work
    planned against the live state would carry the wrong identity. Every
    Every field needed to build a `CorrectionKey` for the CURRENT method is
    here, so a consumer can construct keys for other channels without
    touching the controller.

    LIMIT, stated because an earlier version of this docstring overstated
    it: this carries ONE method and one parameter set. It cannot express
    "both tophat and cuCIM are prepared for this channel". A consumer that
    needs to reason about several methods at once needs a richer, still
    immutable spec (a `ChannelCorrectionSpec`), which is not defined yet.

    `epoch` increments on every interaction event and every selection
    change. A consumer that captured epoch N must re-check it before
    starting deferred work and abandon the work if it has moved.
    """

    epoch: int
    source: object
    channel: str
    method: Optional[str]
    params: Tuple[int, ...]
    level: int
    quality: object
    algorithm_version: str
    bbox_l0: Optional[Tuple[int, int, int, int]]
    visible_tiles: frozenset = field(default_factory=frozenset)
    # None, not a stale number, while this channel's overview record is not
    # installed: until it is, the display range for this channel is simply
    # unknown, and a consumer must not act as though it had one.
    overview_ready: bool = False
    display_lo: Optional[float] = None
    display_hi: Optional[float] = None


# ── TileItemPool ─────────────────────────────────────────────────────────────

class _PoolEntry:
    __slots__ = ("item", "level", "tx", "ty", "rect", "key")

    def __init__(self, item, level, tx, ty, rect):
        self.item = item
        self.level = level
        self.tx = tx
        self.ty = ty
        self.rect = rect
        self.key = None  # RawKey / CorrectionKey last successfully blitted


class TileItemPool:
    """One `pg.ImageItem` per delivered tile, keyed by `(level, tx, ty)`.

    Items are created once and updated in place thereafter (`setImage`);
    their `setRect` placement is computed ONCE at creation and never
    changed. Pruning removes items that are both off the current level and
    outside the current viewport (with margin), farthest-first, only once
    the pool exceeds `budget`.
    """

    def __init__(self, view_box, base_z: int, num_levels: int,
                 budget: int = DEFAULT_ITEM_BUDGET):
        self.view_box = view_box
        self.base_z = base_z
        self.num_levels = max(1, num_levels)
        self.budget = budget
        self.entries: Dict[Tuple[int, int, int], _PoolEntry] = {}
        self.items_created = 0
        self.items_pruned = 0
        # The pool owns the colour table, not the caller: items are created
        # lazily as tiles arrive, so a caller that only walked `entries`
        # would leave every tile that lands afterwards grey.
        self._lut = None
        # Layer opacity, owned here for the same reason as the LUT: items
        # created after the switch must come up in the same state.
        self._layer_opacity = 1.0
        # Composition mode, same ownership rule. None = pyqtgraph's default
        # (SourceOver), which is what every existing caller gets: this is
        # only ever set by a layer that must ADD to what is underneath.
        self._composition_mode = None

    def set_composition_mode(self, mode) -> None:
        """Apply a QPainter.CompositionMode to every item, now and on every
        item created later. `None` restores pyqtgraph's default
        (SourceOver), which is what all existing layers use."""
        self._composition_mode = mode
        for entry in self.entries.values():
            entry.item.setCompositionMode(mode)

    def set_layer_opacity(self, alpha: float) -> None:
        """Fade the whole layer in/out without touching `setVisible`, which
        the per-level visibility policy owns and rewrites on every range
        change."""
        self._layer_opacity = float(alpha)
        for entry in self.entries.values():
            entry.item.setOpacity(self._layer_opacity)

    def set_lookup_table(self, lut) -> None:
        """Apply `lut` (or None for greyscale) to every item, now and on
        every item created later. Pure display: the stored uint8 pixels are
        untouched, so this costs no re-quantisation and no re-read."""
        self._lut = lut
        for entry in self.entries.values():
            entry.item.setLookupTable(lut)

    def _z_for_level(self, level: int) -> float:
        return self.base_z + (self.num_levels - level)

    def get(self, level: int, tx: int, ty: int) -> Optional[_PoolEntry]:
        return self.entries.get((level, tx, ty))

    def put(self, level: int, tx: int, ty: int, rect: QRectF,
            arr_uint8: np.ndarray, key) -> _PoolEntry:
        """Create (once) or update the item at `(level, tx, ty)`."""
        coord = (level, tx, ty)
        entry = self.entries.get(coord)
        if entry is None:
            # NOT `pg.ImageItem(arr_uint8, ...)`: passing the image to the
            # constructor runs pyqtgraph's autoLevels path, which scans the
            # array with `quickMinMax` (measured: 17 nanmin + 17 nanmax per
            # channel switch) only for levels we immediately overwrite with
            # the fixed (0, 255).
            item = pg.ImageItem(axisOrder="row-major")
            item.setZValue(self._z_for_level(level))
            item.setImage(arr_uint8, autoLevels=False, levels=(0, 255))
            if self._lut is not None:
                item.setLookupTable(self._lut)
            if self._layer_opacity != 1.0:
                item.setOpacity(self._layer_opacity)
            if self._composition_mode is not None:
                item.setCompositionMode(self._composition_mode)
            item.setRect(rect)
            self.view_box.addItem(item)
            entry = _PoolEntry(item, level, tx, ty, rect)
            self.entries[coord] = entry
            self.items_created += 1
        else:
            entry.item.setImage(arr_uint8, autoLevels=False, levels=(0, 255))
            # rect/level are immutable after creation (design contract):
            # entries never move.
        entry.key = key
        return entry

    def apply_visibility(self, current_level: int, *,
                         current_level_visible: bool = True,
                         coarser_visible: bool = True,
                         key_ok=None) -> None:
        """Per-level visibility policy. An item FINER than `current_level`
        (entry.level < current_level) is NEVER visible -- it would draw
        above the current level (higher z) and show stale, differently-
        corrected pixels. Items stay in the pool (cached, instantly
        re-shown on zoom back in); only their visibility changes.

        `key_ok`, when given, is called as `key_ok(entry.key, entry.level)`
        and must return True for the entry to be eligible at all; an entry
        whose key does not match the caller's current context is hidden
        regardless of level. This keeps selection-context knowledge (e.g.
        "does this CorrectionKey still match the live method/params?") out
        of this dumb item-container class -- the caller supplies it."""
        for entry in self.entries.values():
            if entry.level < current_level:
                entry.item.setVisible(False)
                continue
            if key_ok is not None and not key_ok(entry.key, entry.level):
                entry.item.setVisible(False)
                continue
            if entry.level == current_level:
                entry.item.setVisible(current_level_visible)
            else:
                entry.item.setVisible(coarser_visible)

    def clear(self):
        """Remove every item (used only for a hard invalidation, e.g. a
        channel/source change -- normal level switches do NOT call this)."""
        for entry in self.entries.values():
            self.view_box.removeItem(entry.item)
        self.entries.clear()

    def prune(self, current_level: int, viewport_world_rect: QRectF,
              margin_world: float, keep_coords):
        """Enforce `self.budget`: if over budget, remove items that are
        (not at `current_level`) AND (outside `viewport_world_rect`
        expanded by `margin_world`), farthest-from-viewport-center first.
        `keep_coords` (a set of `(level, tx, ty)`) is NEVER pruned even if
        it would otherwise qualify (defensive -- normally the wanted set is
        already at `current_level` and inside the viewport)."""
        if len(self.entries) <= self.budget:
            return
        expanded = viewport_world_rect.adjusted(
            -margin_world, -margin_world, margin_world, margin_world)
        cx = viewport_world_rect.center().x()
        cy = viewport_world_rect.center().y()

        candidates = []
        for coord, entry in self.entries.items():
            if coord in keep_coords:
                continue
            if entry.level == current_level:
                continue
            if expanded.intersects(entry.rect):
                continue
            rc = entry.rect.center()
            dist2 = (rc.x() - cx) ** 2 + (rc.y() - cy) ** 2
            candidates.append((dist2, coord))

        if not candidates:
            return
        candidates.sort(reverse=True)  # farthest first
        n_over = len(self.entries) - self.budget
        for _dist2, coord in candidates[:n_over]:
            entry = self.entries.pop(coord)
            self.view_box.removeItem(entry.item)
            self.items_pruned += 1


# ── ExploreView: the widget ─────────────────────────────────────────────────

class ExploreView(QtWidgets.QWidget):
    """pyqtgraph GraphicsLayoutWidget hosting one ViewBox: one pinned
    overview ImageItem plus two per-tile TileItemPools (raw, precise), all
    positioned in the full-resolution world coordinate system.

    Z-order (bottom to top): overview (0) < raw pool (RAW_BASE_Z..) <
    precise pool (PRECISE_BASE_Z..).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.graphics = pg.GraphicsLayoutWidget()
        layout.addWidget(self.graphics)

        self.view_box: pg.ViewBox = self.graphics.addViewBox()
        self.view_box.setAspectLocked(True)
        self.view_box.invertY(True)
        # The controller drives the viewport explicitly (pan/zoom/jump); a
        # pyqtgraph ViewBox's default auto-range-to-content behavior would
        # otherwise fire a SPURIOUS sigRangeChanged whenever an ImageItem's
        # bounding rect changes (e.g. load_overview()'s first setImage),
        # picking an unrelated display level out from under the caller.
        # Native mouse pan/wheel-zoom-about-cursor interaction is untouched
        # (pyqtgraph ViewBox default) -- disabling auto-RANGE does not
        # disable mouse interaction.
        self.view_box.disableAutoRange()

        # Every ImageItem constructed with axisOrder="row-major" EXPLICITLY
        # (module docstring) -- never relying on a process-global config.
        self.overview_item = pg.ImageItem(axisOrder="row-major")
        self.overview_item.setZValue(0)
        self.view_box.addItem(self.overview_item)

        # Corrected floor (module docstring "corrected floor + single-stage
        # motion guarantee"): a whole-array corrected preview at a coarse
        # pyramid level, shown while precise tiles fill in so raw-stage
        # pixels never reach the screen in corrected mode.
        self.corrected_floor_item = pg.ImageItem(axisOrder="row-major")
        self.corrected_floor_item.setZValue(FLOOR_Z)
        self.view_box.addItem(self.corrected_floor_item)
        self.corrected_floor_item.setVisible(False)

        # Always-available in-view status badge (e.g. "Preparing corrected
        # preview…") -- a window-title suffix is easy to miss, and a future
        # Step0 host may not even own a window title to append to. Parented
        # to the widget itself (not the graphics scene) so it floats over
        # the graphics area in WIDGET coordinates, unaffected by camera
        # pan/zoom. Transparent-for-mouse so it never eats camera input.
        self.status_label = QtWidgets.QLabel(self)
        self.status_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: #eeeeee;"
            "padding: 4px 8px; border-radius: 3px; font-size: 11px;")
        self.status_label.move(8, 8)
        self.status_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.status_label.setVisible(False)
        self.status_label.adjustSize()
        self.status_label.raise_()

    def set_status_text(self, text: Optional[str]):
        """Show `text` in the in-view status badge, or hide it entirely
        when `text` is empty/None."""
        if not text:
            self.status_label.setVisible(False)
            return
        self.status_label.setText(text)
        self.status_label.adjustSize()
        self.status_label.setVisible(True)
        self.status_label.raise_()

    @staticmethod
    def world_rect(y0: float, x0: float, h: float, w: float,
                    ds_y: float, ds_x: float) -> QRectF:
        """World-space rect for a level-L tile/region, using UNROUNDED,
        per-axis downsample factors (module docstring)."""
        return QRectF(x0 * ds_x, y0 * ds_y, w * ds_x, h * ds_y)


# ── RawOverlayLayer ──────────────────────────────────────────────────────────

# Priority base for overlay raw requests. What this does and does not
# guarantee, stated exactly:
#
# The scheduler keeps TWO heaps -- one for RawKey work and one for
# CorrectionKey work -- and this value orders the raw heap only. Marker
# visible-raw requests start at 0 and there are as many of them as the
# viewport holds tiles, which in practice is far fewer than 500, so a marker
# tile the user is looking at normally sorts ahead of an overlay tile. That
# is a consequence of the numbers, not a guarantee: nothing enforces the
# viewport tile count, and priority cannot preempt work already started.
#
# It says nothing at all about corrected work. CorrectionKey requests live
# in the other heap and are not comparable with this number, and the raw
# STAGING a corrected tile needs is enqueued at raw priority 0 whatever
# asked for it -- so marker correction work can and does overtake overlay
# tiles. Do not describe this as DAPI being strictly below (or above) all
# marker work.
OVERLAY_RAW_BASE_PRIORITY = 500

# Z base for the overlay's tile pool. Above PRECISE_BASE_Z plus the largest
# per-level offset any pool can add (`base_z + num_levels - level`), so an
# overlay tile always paints after every marker layer. Asserted against the
# real pools at attach time rather than assumed.
OVERLAY_BASE_Z = 300


class RawOverlayLayer(QtCore.QObject):
    """A second CHANNEL drawn on top of the marker layers, added to them.

    Deliberately narrow. It owns one `TileItemPool`, its own channel, tint,
    enabled flag, display range, calibration state and generation counter,
    and its own queued delivery signal. It owns NOTHING else: no overview
    item, no corrected floor, no precise pool, no gain calibration, no
    motion/settle timer, no directional prefetch, no interaction epoch --
    and, above all, no shutdown rights over the scheduler or the provider,
    which it merely borrows from the main stack.

    It is not a second camera. The marker `ExploreController` remains the
    only planner of viewport, display level and visible-tile set; this layer
    is handed those three values and requests exactly the tiles it is told
    are visible. There is no second viewport state machine, and the two
    channels are never waited for in pairs: each arrives and paints on its
    own.

    Generation tokens are namespaced `("dapi_raw", n)`. `TileRequest`
    documents the generation as an opaque token precisely so independent
    counters cannot collide, and `TileScheduler.cancel_generation` matches
    tokens by value in one global stale-set -- so a bare integer, or the
    marker's own `("raw", n)`, would let either side's cancel drop the
    other side's in-flight work.

    Priority: overlay requests start at `OVERLAY_RAW_BASE_PRIORITY`, which
    orders the scheduler's RAW heap only -- see that constant for what it
    does and does not guarantee. It is not a claim about corrected work,
    which lives in a different heap.

    Composition is ADDITIVE (`CompositionMode_Plus`), which is what makes
    this an overlay rather than an occluder: a dark overlay pixel adds zero
    and leaves the marker exactly as it was, and where both are bright the
    result is the per-channel saturating sum -- the same thing the compare
    panels compute as `marker_colour * i + nucleus_colour * j`. Measured on
    a real ExploreView, not assumed. Only THIS layer composes that way; the
    marker layers keep SourceOver and keep occluding each other, because two
    additive layers of the same channel would double their own brightness
    where they overlap.
    """

    _delivered = QtCore.pyqtSignal(object)
    _calibrated = QtCore.pyqtSignal(object)

    def __init__(self, provider, scheduler, grid, view, channel, *,
                 item_budget: int = DEFAULT_ITEM_BUDGET,
                 base_z: int = OVERLAY_BASE_Z, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.scheduler = scheduler
        self.grid = grid
        self.view = view
        self.channel = channel

        self._pool = TileItemPool(view.view_box, base_z,
                                  getattr(provider, "num_levels", 1),
                                  item_budget)
        self._pool.set_composition_mode(
            QtGui.QPainter.CompositionMode_Plus)

        # ── enable state: the user's switch and the suppression are kept
        # SEPARATE. `_enabled` is what the button says and is never
        # overwritten by the code; `_suppressed` is set when the marker is
        # already showing this very channel, so it would otherwise be added
        # to itself. The effective state is the AND, so moving off the
        # nucleus channel restores the layer without the user touching the
        # button and without a per-channel switch map to remember.
        self._enabled = False
        self._suppressed = False

        # ── display range: this channel's OWN. Borrowing the marker's would
        # quantise nucleus pixels against a range computed from a different
        # channel's histogram, i.e. an arbitrary brightness.
        self._display_lo = None
        self._display_hi = None
        self._calibrating = False
        self._calibration_failed = False

        self._generation_n = 0
        self._generation = ("dapi_raw", 0)
        self._torn_down = False

        self.stats = {
            "requests_issued": 0,
            "tiles_blitted": 0,
            "late_rejected": 0,
            "mismatched_dropped": 0,
            "calibration_failures": 0,
        }

        self._delivered.connect(self._handle_result, QtCore.Qt.QueuedConnection)
        self._calibrated.connect(self._handle_calibration,
                                 QtCore.Qt.QueuedConnection)

    # ── state, readable by the host and by tests ──────────────────────
    @property
    def enabled(self):
        """What the SWITCH says -- never overwritten by suppression."""
        return self._enabled

    @property
    def suppressed(self):
        return self._suppressed

    @property
    def effective_enabled(self):
        return self._enabled and not self._suppressed and not self._torn_down

    @property
    def calibrated(self):
        return self._display_lo is not None and self._display_hi is not None

    @property
    def calibration_failed(self):
        return self._calibration_failed

    @property
    def generation(self):
        return self._generation

    @property
    def pool(self):
        return self._pool

    # ── switches ──────────────────────────────────────────────────────
    def set_enabled(self, enabled: bool, *, host=None):
        """The user's switch. Turning it OFF cancels the current generation
        and stops issuing; turning it back ON resumes from the host's
        CURRENT viewport, so the user does not have to pan to get pixels
        back. Tiles already pooled and cached are kept either way, so a
        toggle is instant and reads nothing when the view has not moved.
        """
        self._enabled = bool(enabled)
        self._apply_enabled_state(host=host)

    def set_suppressed(self, suppressed: bool, *, host=None):
        """Set when the marker layer is ALREADY showing this channel.

        Additive composition would otherwise add the nucleus to itself,
        doubling its brightness and misrepresenting the pixel values. The
        user's switch is untouched, so leaving that channel restores the
        overlay by itself.
        """
        self._suppressed = bool(suppressed)
        self._apply_enabled_state(host=host)

    def _apply_enabled_state(self, *, host=None):
        on = self.effective_enabled
        self._pool.set_layer_opacity(1.0 if on else 0.0)
        if not on:
            # Stop the in-flight work as well as the drawing: an overlay
            # nobody is looking at must not keep the IO workers busy.
            self._bump_generation()
            return
        if self.calibrated and host is not None:
            self._resync_from(host)
        elif not self.calibrated and not self._calibrating:
            self.start_calibration(host)

    def set_tint(self, rgb):
        """Colour, as a lookup table. Display only -- no re-read, no
        re-quantisation, no request."""
        self._pool.set_lookup_table(
            None if rgb is None else ExploreController.build_tint_lut(rgb))

    # ── calibration ───────────────────────────────────────────────────
    def start_calibration(self, host):
        """Read ONE coarse level of this channel, only to derive its fixed
        display range. The pixels are discarded: this layer never shows an
        overview, and installing one would need a second occluding image
        under an additive layer, which is exactly the doubling this design
        avoids.

        Single-flight, and it never retries in a loop: a failure leaves the
        layer with no pixels and a recorded failure, and does NOT fall back
        to the marker's display range -- which would be a different
        channel's histogram presented as this one's.
        """
        if (self._torn_down or self._calibrating or self.calibrated
                or host is None):
            return
        self._calibrating = True
        provider = self.provider
        source = provider.source_identity()
        level = host._pick_overview_level()
        channel = self.channel

        def work():
            try:
                rec = ExploreController._read_overview_record(
                    provider, source, channel, level)
                self._calibrated.emit(rec)
            except Exception as exc:        # noqa: BLE001 - reported, not raised
                self._calibrated.emit(exc)

        host._overview_pool.submit(work)

    def _handle_calibration(self, rec_or_exc):
        self._calibrating = False
        if self._torn_down:
            return
        if isinstance(rec_or_exc, Exception):
            self._calibration_failed = True
            self.stats["calibration_failures"] += 1
            return
        rec = rec_or_exc
        if (rec.channel != self.channel
                or rec.source != self.provider.source_identity()):
            self.stats["mismatched_dropped"] += 1
            return
        self._display_lo, self._display_hi = rec.display_lo, rec.display_hi
        host = self.parent()
        if self.effective_enabled and host is not None:
            self._resync_from(host)

    # ── the two mount points ──────────────────────────────────────────
    def apply_visibility(self, current_level: int):
        """Called right after the host has settled `level` for this frame.

        `coarser_visible=False` is a hard contract here, not a tuning
        choice: under additive composition a coarse tile left visible under
        a current-level tile adds its brightness to it, so the same pixels
        would read twice as bright wherever the two overlap.
        """
        self._pool.apply_visibility(
            current_level,
            current_level_visible=self.effective_enabled,
            coarser_visible=False,
        )

    def sync(self, level: int, ordered_visible_tiles):
        """Request this channel's missing tiles for the viewport the HOST
        has already planned.

        `level` and the ORDER of `ordered_visible_tiles` both come from the
        marker controller (`_visible_tiles_center_out`); nothing here
        recomputes either, and no bbox is passed because nothing here needs
        one. The overlay filters that sequence down to what its OWN pool is
        missing and preserves the order. Called after the marker's own
        requests have been submitted.
        """
        if self._torn_down or not self.effective_enabled:
            return
        if not self.calibrated:
            # Not an error: the calibration callback resyncs when it lands.
            return
        self._bump_generation()
        gen = self._generation
        source = self.provider.source_identity()
        missing = [coord for coord in ordered_visible_tiles
                   if self._pool.get(level, *coord) is None]
        for i, (tx, ty) in enumerate(missing):
            addr = TileAddress(grid=self.grid, level=level, tx=tx, ty=ty)
            req = TileRequest(key=RawKey(source=source, channel=self.channel,
                                         tile=addr),
                              generation=gen,
                              priority=OVERLAY_RAW_BASE_PRIORITY + i)
            self.scheduler.request(req, self._on_result)
            self.stats["requests_issued"] += 1

    def _resync_from(self, host):
        """Re-issue for the host's CURRENT viewport, without waiting for the
        user to move the camera.

        Goes through the host's ordering entry point, exactly like the
        range-driven path -- the two must not be able to drift into
        different orders.
        """
        if host is None:
            return
        ordered = host._visible_tiles_center_out()
        if not ordered:
            return
        self.sync(host.level, ordered)
        self.apply_visibility(host.level)

    def _bump_generation(self):
        self.scheduler.cancel_generation(self._generation)
        self._generation_n += 1
        self._generation = ("dapi_raw", self._generation_n)

    # ── delivery ──────────────────────────────────────────────────────
    def _on_result(self, result):
        """Scheduler callback -- worker thread, or synchronous on a cache
        hit. Marshal to the GUI thread."""
        self._delivered.emit(result)

    def _handle_result(self, result):
        """GUI-side delivery guard.

        Every check is load-bearing, and `disconnect` is NOT one of them: a
        queued signal already sitting in Qt's event queue is delivered after
        a disconnect, so teardown and the off switch have to be testable
        here rather than assumed away upstream.
        """
        if self._torn_down or not self.effective_enabled:
            return
        if result.error is not None or result.pixels is None:
            return
        req = result.request
        key = req.key
        if not isinstance(key, RawKey):
            return
        if req.generation != self._generation:
            self.stats["late_rejected"] += 1
            return
        if key.channel != self.channel:
            self.stats["mismatched_dropped"] += 1
            return
        if key.source != self.provider.source_identity():
            self.stats["mismatched_dropped"] += 1
            return
        if not self.calibrated:
            self.stats["mismatched_dropped"] += 1
            return
        host = self.parent()
        tile = key.tile
        if host is not None:
            if tile.level != host.level:
                self.stats["mismatched_dropped"] += 1
                return
            if (tile.tx, tile.ty) not in host._visible_tiles:
                self.stats["late_rejected"] += 1
                return

        arr = result.pixels.handle
        gray = self._quantize(arr)
        ds_y, ds_x = self.provider.level_downsample_yx(tile.level)
        rect = ExploreView.world_rect(
            tile.ty * self.grid.tile_size, tile.tx * self.grid.tile_size,
            arr.shape[0], arr.shape[1], ds_y, ds_x)
        self._pool.put(tile.level, tile.tx, tile.ty, rect, gray, key)
        self.stats["tiles_blitted"] += 1
        self.apply_visibility(tile.level)

    def _quantize(self, arr):
        """Same arithmetic and same ORDER as the host's raw quantisation,
        against THIS channel's own display range."""
        span = max(self._display_hi - self._display_lo, 1e-6)
        buf = np.array(arr, dtype=np.float32, copy=True)
        buf -= self._display_lo
        buf /= span
        np.clip(buf, 0.0, 1.0, out=buf)
        buf *= 255.0
        np.round(buf, out=buf)
        return buf.astype(np.uint8)

    # ── teardown ──────────────────────────────────────────────────────
    def teardown(self):
        """Release what this layer owns, and NOTHING it borrows.

        No `scheduler.shutdown()`, no `provider.close()`: the main stack
        owns those and closes them exactly once, after this. Physical reads
        already started for this layer are drained by that single shutdown
        -- there is deliberately no second drain/idle mechanism here.
        Idempotent.
        """
        if self._torn_down:
            return
        self._torn_down = True
        self._enabled = False
        self._bump_generation()
        for signal, slot in ((self._delivered, self._handle_result),
                             (self._calibrated, self._handle_calibration)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._pool.clear()


# ── ExploreController ────────────────────────────────────────────────────────

class ExploreController(QtCore.QObject):
    """Drives an ExploreView from a provider/scheduler/compute stack.

    Selection state (channel/method/params) plus the viewport define the
    current wanted tile set. Both raw and precise (corrected) tiles are
    requested from the same 30ms motion-coalescing timer (module docstring
    "Camera contract") -- immediately via `jump_to`/`set_selection`, or
    after a burst of camera motion settles for `MOTION_MS`. `settle_ms` is
    retained only as a future-refinement hook (`_on_settle`); it no longer
    gates any interactive request.
    """

    provisional_changed = QtCore.pyqtSignal(bool)
    floor_preparing_changed = QtCore.pyqtSignal(bool)
    # Emitted from `_handle_floor_result`: True when a floor result was
    # accepted (current generation/context, no error), False when it was
    # dropped as stale or failed to compute. Lets a host (or this class
    # itself, for the in-view status badge) know the floor's outcome
    # without polling `_floor_ready`.
    floor_ready_changed = QtCore.pyqtSignal(bool)

    # ── public, read-only interaction contract ──
    #
    # A background consumer subscribes to these; the controller never
    # depends on a consumer, so the dependency stays one-directional and a
    # consumer can be deleted without touching this class.
    #
    # `interaction_event(kind, snapshot)` -- kind is one of
    # "NAVIGATOR_JUMP" / "PAN" / "ZOOM" / "CHANNEL_SWITCH". The source is
    # EXPLICIT, supplied by whoever caused it; consumers must not infer a
    # jump from displacement. (`prefetch_policy`'s viewport-fraction rule
    # is a fallback for callers that genuinely cannot say, not this path.)
    #
    # `gesture_quiet(snapshot)` fires from the EXISTING `settle_ms` timer
    # (80ms by default), and means only "the display system considers the
    # camera gesture over" -- it is what clears `_viewport_zooming`. It is
    # deliberately NOT called "settled": a background policy's SETTLED is a
    # different, longer judgement ("the user is really staying here, it is
    # worth computing other channels") and belongs to the consumer, which
    # confirms its own additional quiet period on top of this one. The
    # display timing that manual testing validated stays at 80ms and must
    # not be stretched to serve background work.
    #
    # `selection_context_changed(snapshot)` fires whenever channel, method
    # or params change.
    interaction_event = QtCore.pyqtSignal(str, object)
    gesture_quiet = QtCore.pyqtSignal(object)
    selection_context_changed = QtCore.pyqtSignal(object)

    # Internal cross-thread delivery signals (scheduler callbacks fire on
    # worker threads; Qt widgets must only be touched on the GUI thread).
    _raw_delivered = QtCore.pyqtSignal(object)
    _precise_delivered = QtCore.pyqtSignal(object)
    _floor_delivered = QtCore.pyqtSignal(object)
    _dirprefetch_delivered = QtCore.pyqtSignal(object)
    _overview_delivered = QtCore.pyqtSignal(object)

    # Public: an overview record for (source, channel, level) finished
    # preparing; the bool is success. A background consumer needs this to
    # know a neighbour is genuinely switch-ready, and must not read the
    # cache dict directly.
    overview_prepared = QtCore.pyqtSignal(object, str, int, bool)

    def __init__(self, provider, scheduler, compute, grid: TileGridSpec,
                 view: ExploreView, channel: str, settle_ms: int = 80,
                 probe: bool = False, item_budget: int = DEFAULT_ITEM_BUDGET,
                 intermediate_corrected_fallback: bool = True,
                 directional_prefetch: bool = True):
        super().__init__()
        self.provider = provider
        self.scheduler = scheduler
        self.compute = compute
        self.grid = grid
        self.view = view
        self.settle_ms = settle_ms
        self.probe = probe
        # Intermediate corrected fallback (module docstring): A/B switch.
        self.intermediate_corrected_fallback = intermediate_corrected_fallback
        # Fallback-level tile set computed at issue time, kept for the
        # delivery-side membership test (module docstring "Intermediate
        # corrected fallback"). None/empty when disabled or no fallback
        # level exists.
        self._fallback_level: Optional[int] = None
        self._fallback_visible_tiles = set()
        self._prev_world_area = None
        self._viewport_shrinking = False
        self._viewport_zooming = False

        # Directional prefetch (module docstring "Directional prefetch
        # (pan only)"): A/B switch plus all direction-estimation and
        # in-flight-cap bookkeeping.
        self.directional_prefetch = directional_prefetch
        self._viewport_center_l0: Optional[Tuple[float, float]] = None
        self._dirprefetch_prev_center: Optional[Tuple[float, float]] = None
        self._dirprefetch_velocity: Tuple[float, float] = (0.0, 0.0)
        self._dirprefetch_last_direction: Optional[Tuple[float, float]] = None
        self._dirprefetch_candidates: list = []
        self._dirprefetch_inflight: int = 0
        self._dirprefetch_gen_n = 0
        self._dirprefetch_generation = ("dirprefetch", self._dirprefetch_gen_n)

        # ── selection state ──
        self.channel = channel
        self.method: Optional[str] = None
        self.params: Tuple[int, ...] = ()
        self.quality = QualityLevel.INTERACTIVE

        # ── generations (namespaced tuples -- see module docstring) ──
        self._raw_gen_n = 0
        self._settled_gen_n = 0
        self.view_generation = ("raw", self._raw_gen_n)
        self._settled_generation = ("precise", self._settled_gen_n)

        # ── display level / viewport bookkeeping ──
        self.level = 0
        self._current_bbox = None  # (y0, x0, y1, x1) in level-0 coords
        self._visible_tiles = set()  # {(tx, ty)} at self.level

        # ── item pools ──
        num_levels = getattr(provider, "num_levels", 1)
        self._raw_pool = TileItemPool(view.view_box, RAW_BASE_Z, num_levels, item_budget)
        self._precise_pool = TileItemPool(view.view_box, PRECISE_BASE_Z, num_levels, item_budget)

        # ── provisional state ──
        self._provisional = False
        self._precise_visible = False
        self.view.precise_visible = False  # for tests/introspection

        # ── corrected floor state (module docstring) ──
        self._floor_level: Optional[int] = None
        self._floor_stride: int = 1
        self._floor_ready = False
        self._floor_ctx = None
        self._floor_gen = 0
        self._floor_job_running = False
        self._floor_pending = False
        self._floor_threads = []

        # Identity of the pixels currently held in `_overview_arr` and shown
        # in `view.overview_item`: (source, channel). WITHOUT this, a channel
        # switch left the old channel's overview on screen and, worse, let
        # the corrected floor and the display-gain calibration be computed
        # FROM the old channel's pixels and registered under the NEW
        # channel's identity -- pixels from one channel claiming to be
        # another. See `_overview_matches_selection`.
        self._overview_identity = None
        # (source, channel, level) -> OverviewRecord. Lets a prepared
        # channel be installed with a memcpy instead of a p95-293ms disk
        # read on the GUI thread; see `OverviewRecord`.
        self._overview_cache = OrderedDict()
        # Single-flight: one persistent worker, and one in-flight read per
        # (source, channel, level). Spawning a thread per call duplicated
        # reads whenever a settle, a rapid switch and a prefetch all wanted
        # the same channel, and each new thread makes `RawTileProvider`
        # parse the TIFF again for its own per-thread handle, which then
        # lives until the provider closes. One worker is enough: five
        # records measured 708ms in total.
        self._overview_inflight = set()
        self._overview_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="explore-overview")
        # Bumped on every interaction event and every selection change; see
        # PrefetchSnapshot.epoch.
        self._interaction_epoch = 0

        # ── per-level display gain for corrected pixels (module docstring
        # "Per-level display gain for CORRECTED pixels") ──
        self._level_gain: Dict[int, float] = {}
        self._gain_ctx = None

        # ── stable display levels (finding 6, carried forward): fixed at
        # load_overview, reapplied identically to overview/raw/precise. New
        # tile arrivals must never rescale brightness.
        self._display_lo = 0.0
        self._display_hi = 1.0
        self._overview_arr: Optional[np.ndarray] = None

        # ── display tint (module docstring "Channel tint") ──
        # None = greyscale, which is what every layer draws as until a host
        # sets a colour. Kept as controller state so a channel switch, a
        # newly pooled tile and a floor rebuild all pick up the same table.
        self._tint = None
        self._marker_visible = True

        # ── optional additive overlay (RawOverlayLayer) ──
        # Attached by the host after construction, never created here: this
        # controller stays the single planner of viewport/level/tiles, and
        # the overlay is a consumer of that plan, not a second camera.
        self._overlay = None

        # ── teardown bookkeeping ──
        self._teardown_order = []
        self._torn_down = False

        # ── stats (exposed for tests / probe) ──
        self.stats = {
            "raw_tiles_blitted": 0,
            "precise_tiles_blitted": 0,
            "mid_tiles_blitted": 0,
            "mid_requests_issued": 0,
            "fallback_synthesized": 0,
            "fallback_synthesis_declined": 0,
            "stale_precise_dropped": 0,
            "mismatched_key_dropped": 0,
            "mismatched_raw_dropped": 0,
            "late_raw_rejected": 0,
            "late_precise_rejected": 0,
            "items_created": 0,
            "items_pruned": 0,
            "floor_compute_failed": 0,
            "floor_level": None,
            "floor_stride": 1,
            "level_display_gain": {},
            "gain_calibrated": False,
            "gain_calibration_failed": 0,
            "dir_prefetch_issued": 0,
            "dir_prefetch_completed": 0,
            "dir_prefetch_cancelled": 0,
            "dir_prefetch_direction_changes": 0,
        }
        # probe-only timing samples (populated only when probe=True).
        self.timings = {
            "range_handler_ms": [],
            "request_issue_ms": [],
            "tile_item_update_ms": [],
            "viewport_first_raw_tile_ms": [],
            "viewport_full_raw_tile_ms": [],
            "viewport_first_precise_tile_ms": [],
            "viewport_full_precise_ms": [],
            # (timestamp, cost_ms) pairs for range_handler/request_issue/
            # tile_item_update samples -- consumed by g1_render_probe.py's
            # window-aggregation post-processing (bucketed 16.7ms windows,
            # NOT exact vsync frames).
            "frame_events": [],
        }
        self._raw_probe_batch = None
        self._precise_probe_batch = None
        # guard against a jump's manual settle firing twice.
        self._jumping = False

        # ── timers ──
        self._settle_timer = QtCore.QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(self.settle_ms)
        self._settle_timer.timeout.connect(self._on_settle)

        # Coalescing "motion" timer: request ISSUING only (the range handler
        # itself runs on every sigRangeChanged, cheaply -- module docstring
        # "Camera contract").
        self.MOTION_MS = 30
        self._motion_timer = QtCore.QTimer(self)
        self._motion_timer.setSingleShot(True)
        self._motion_timer.setInterval(self.MOTION_MS)
        self._motion_timer.timeout.connect(self._issue_raw_requests)

        # Level-switch hysteresis threshold (fraction) -- avoids z-order /
        # request thrash when the zoom sits right at a level boundary.
        self.LEVEL_HYSTERESIS = 0.2

        self._raw_delivered.connect(self._handle_raw_result, QtCore.Qt.QueuedConnection)
        self._precise_delivered.connect(self._handle_precise_result, QtCore.Qt.QueuedConnection)
        self._floor_delivered.connect(self._handle_floor_result, QtCore.Qt.QueuedConnection)
        self._dirprefetch_delivered.connect(self._handle_dirprefetch_result, QtCore.Qt.QueuedConnection)
        self._overview_delivered.connect(self._handle_overview_result, QtCore.Qt.QueuedConnection)

        self.view.view_box.sigRangeChanged.connect(self._on_range_changed)

        # Drive the in-view status badge ourselves (module docstring
        # "make the floor's state observable") -- a host need not connect
        # anything for the badge to work.
        self.floor_preparing_changed.connect(self._on_floor_preparing_changed_for_badge)

    # ── source identity / correction key helpers ─────────────────────────

    def selection_key_context(self):
        """(source, channel, method, effective params, level, quality) —
        the identity tuple a precise tile must match to be considered
        current (design doc §1.2)."""
        source = self.provider.source_identity()
        ds = self.provider.level_downsample(self.level)
        eff_params = tuple(
            effective_param(p, self.level, ds) for p in self.params
        )
        return (source, self.channel, self.method, eff_params, self.level, self.quality)

    def _wants_precise(self) -> bool:
        return self.method is not None

    def _make_correction_key(self, tx: int, ty: int, level: Optional[int] = None) -> CorrectionKey:
        """`level` defaults to `self.level` (existing call sites unchanged).
        An explicit `level` (module docstring "Intermediate corrected
        fallback") derives `effective_param` from THAT level's own
        downsample -- the fallback level's tiles carry the same source
        identity, channel, method, algorithm version and quality as the
        current level's; only the level and the level-scaled effective
        params differ."""
        if level is None:
            level = self.level
        source = self.provider.source_identity()
        ds = self.provider.level_downsample(level)
        eff_params = tuple(effective_param(p, level, ds) for p in self.params)
        addr = TileAddress(grid=self.grid, level=level, tx=tx, ty=ty)
        return CorrectionKey(
            source=source, channel=self.channel, tile=addr, method=self.method,
            params=eff_params, algorithm_version=BG_CORRECTION_ALGO_VERSION,
            quality=self.quality,
        )

    def _visible_tiles_center_out(self):
        """The LIVE visible tiles, in centre-out order.

        The single source of that order. It sorts `self._visible_tiles` --
        it does not recompute which tiles are visible -- using the viewport
        this controller has already settled, and both the marker's own raw
        batch and the overlay's read it, so the two channels can never end
        up fetching the same viewport in different orders.

        Returns an empty tuple when there is no viewport yet.
        """
        bbox_l0 = self._current_bbox
        if bbox_l0 is None:
            return ()
        ds = self.provider.level_downsample(self.level)
        bbox_level = planning.bbox_to_level(bbox_l0, ds)
        cy = (bbox_level[0] + bbox_level[2]) / 2.0
        cx = (bbox_level[1] + bbox_level[3]) / 2.0
        tile_size = self.grid.tile_size

        def dist(coord):
            tx, ty = coord
            tcy = ty * tile_size + tile_size / 2.0
            tcx = tx * tile_size + tile_size / 2.0
            return (tcy - cy) ** 2 + (tcx - cx) ** 2

        # `sorted` is stable and the distance formula is unchanged, so the
        # order -- ties included -- is exactly what the marker batch had
        # before this was factored out.
        return sorted(self._visible_tiles, key=dist)

    def _make_raw_key(self, tx: int, ty: int) -> RawKey:
        source = self.provider.source_identity()
        addr = TileAddress(grid=self.grid, level=self.level, tx=tx, ty=ty)
        return RawKey(source=source, channel=self.channel, tile=addr)

    # ── selection ─────────────────────────────────────────────────────────

    def attach_overlay(self, overlay):
        """Attach an additive `RawOverlayLayer`. It is parented to this
        controller so it can read the live level/viewport/tile set, and it
        is asserted to draw ABOVE every layer this controller owns --
        additive composition under an occluding layer would be invisible.
        """
        if overlay is not None:
            top_marker_z = max(
                self._raw_pool.base_z + self._raw_pool.num_levels,
                self._precise_pool.base_z + self._precise_pool.num_levels)
            assert overlay.pool.base_z > top_marker_z, (
                f"overlay base_z {overlay.pool.base_z} is not above every "
                f"marker layer (max {top_marker_z})")
            overlay.setParent(self)
        self._overlay = overlay

    @property
    def overlay(self):
        return self._overlay

    # ── channel tint / layer visibility ─────────────────────────────────

    @staticmethod
    def build_tint_lut(rgb):
        """A 256-entry uint8 RGB colour table for `rgb` (floats 0..1).

        Entry i is `i * rgb`, i.e. the same "grey value scales one colour"
        mapping the compare panels use (`Step0Page._make_colored_rgb`), so
        the full image and the patch preview agree on what a channel looks
        like. No alpha: at this stage there is nothing underneath to show
        through, and a transparent dark end would only reveal the ViewBox
        background.
        """
        ramp = np.arange(256, dtype=np.float32)
        scaled = ramp[:, None] * np.asarray(rgb, dtype=np.float32)[None, :3]
        return np.clip(scaled, 0, 255).astype(np.uint8)

    def set_tint(self, rgb):
        """Colour every layer this controller owns. `None` = greyscale.

        Display only: the stored uint8 pixels are unchanged, so this is a
        lookup-table swap -- no re-read, no re-quantisation, no scheduler
        traffic. Applied to the overview, the corrected floor and BOTH tile
        pools, and remembered so tiles that arrive later are coloured too.
        """
        lut = None if rgb is None else self.build_tint_lut(rgb)
        self._tint = lut
        self.view.overview_item.setLookupTable(lut)
        self.view.corrected_floor_item.setLookupTable(lut)
        self._raw_pool.set_lookup_table(lut)
        self._precise_pool.set_lookup_table(lut)

    def set_marker_visible(self, visible: bool):
        """Show/hide every layer this controller owns.

        Visibility only -- the tiles stay pooled and cached, so toggling
        back is instant and issues no request. Per-level and per-key
        visibility inside the pools is unaffected: this gates on top of it,
        so re-showing does not resurrect a tile the level policy hides.
        """
        self._marker_visible = bool(visible)
        alpha = 1.0 if self._marker_visible else 0.0
        # OPACITY, not `setVisible`: the pools' per-level and per-key
        # visibility policy owns `setVisible` and rewrites it on every range
        # change, so a layer switch expressed that way would be undone by
        # the next pan. Opacity is written by nobody else, so the two
        # mechanisms compose instead of fighting.
        self.view.overview_item.setOpacity(alpha)
        self.view.corrected_floor_item.setOpacity(alpha)
        self._raw_pool.set_layer_opacity(alpha)
        self._precise_pool.set_layer_opacity(alpha)

    def set_selection(self, channel=_UNSET, method=_UNSET, params=_UNSET):
        """Change channel/method/params. Marks the precise layer
        PROVISIONAL and re-issues the settled request for the current
        viewport immediately (no wait). A channel change also invalidates
        both pools outright (a channel change means every already-drawn
        pixel is simply the wrong data -- unlike a method/param change,
        there is no "still technically raw and fine" fallback)."""
        channel_changed = channel is not _UNSET and channel != self.channel
        atomic_swapped = False
        overview_ready = True
        if channel is not _UNSET:
            self.channel = channel
        if method is not _UNSET:
            self.method = method
        if params is not _UNSET:
            self.params = params

        if channel_changed:
            # ORDER MATTERS, and it is a correctness order, not a cosmetic
            # one. The overview is dealt with FIRST, because everything
            # after it either displays or consumes overview pixels: leaving
            # the old channel's overview up would show one channel's pixels
            # while the UI claims another, and it would also feed the
            # corrected floor and the gain calibration (see
            # `_overview_matches_selection`). The record also carries
            # `_display_lo`/`_display_hi` for the new channel, which the
            # quantisation in the swap below depends on.
            #
            # Never a synchronous read here: it costs p95 292.9ms on the
            # real 57-channel slide. A resident record installs as a
            # memcpy; otherwise the old pixels are cleared at once and the
            # read runs on a worker (`switch_overview_for_current_channel`).
            overview_ready = self.switch_overview_for_current_channel()

            if overview_ready:
                # Then try to swap in a fully-cached corrected viewport
                # within THIS SAME GUI event -- clear, fill, and one
                # visibility update, with no queued signal in between. The
                # normal request path would leave at least one painted
                # frame showing raw before the corrected tiles land.
                atomic_swapped = self._try_atomic_cached_channel_swap()
                if not atomic_swapped:
                    self._raw_pool.clear()
                    self._precise_pool.clear()
            else:
                # Cold switch: this channel's display range is UNKNOWN until
                # its overview record lands. Quantisation happens once, when
                # a tile arrives, and a pooled tile keeps it -- so anything
                # drawn now would be quantised against the PREVIOUS
                # channel's range and would keep that wrong brightness for
                # good. Draw nothing and ask for nothing; the empty,
                # provisional state is the honest one, and
                # `_handle_overview_result` resumes the work once the range
                # is known.
                self._raw_pool.clear()
                self._precise_pool.clear()

        # Directional prefetch (module docstring): a selection change makes
        # every pending candidate's CorrectionKey stale, so cancel outright
        # rather than let it compute against the old selection.
        self._cancel_directional_prefetch()

        self._enter_provisional()
        # Withheld on a cold switch (see above): the floor and every tile
        # request would quantise against a display range that does not
        # belong to this channel.
        # Readiness, not "did the channel change": a method or param change
        # arriving while a cold switch is still waiting for its record must
        # be withheld too, for exactly the same reason.
        withhold = not self._overview_matches_selection()
        if self._wants_precise() and not withhold:
            self._ensure_corrected_floor()
        if self._current_bbox is not None and not withhold:
            # `_issue_raw_requests` issues the precise batch itself, at the
            # end, so calling both would bump and cancel the precise
            # generation twice and register duplicate waiters for the same
            # keys. Harmless -- the scheduler is single-flight -- but it
            # muddles the raw-before-precise ordering the tick relies on.
            need_raw = channel_changed and not atomic_swapped
            if need_raw:
                # Do not wait for the next motion tick: the new channel has
                # nothing pooled, so its raw tiles must be asked for now.
                # This does not make them arrive instantly; until they do,
                # the honest display is the NEW channel's coarse overview
                # (a provisional state), never the old channel's pixels.
                #
                # Skipped when the atomic swap succeeded: the corrected
                # viewport is already complete and the raw layer is hidden,
                # so these reads could never be seen. They would only burn
                # I/O and contend with background channel preparation.
                self._issue_raw_requests()
            else:
                # After an atomic swap the viewport is ALREADY complete at
                # the current level, so a synthesized coarse underlay cannot
                # be seen -- but building it costs ~30ms of `np.mean` on the
                # GUI thread, inside the switch. Request the same tiles
                # asynchronously instead; the underlay is ready for the next
                # pan either way.
                self._issue_settled_request(
                    allow_local_synthesis=not atomic_swapped)

        # The atomic cached swap fills the pool BEFORE `_enter_provisional`
        # above, so no later delivery arrives to clear the flag: without
        # this the view would sit in a provisional state for a switch that
        # is in fact already complete. Cheap and idempotent otherwise.
        self._maybe_exit_provisional()

        self._interaction_epoch += 1
        snap = self.snapshot()
        self.selection_context_changed.emit(snap)
        if channel_changed:
            self.interaction_event.emit("CHANNEL_SWITCH", snap)
            # A switch is a discrete event with no camera motion behind it,
            # so nothing else would ever start the quiet period. Without
            # this the 80ms `gesture_quiet` never fires and a background
            # consumer can never reach its own SETTLED.
            self._settle_timer.start(self.settle_ms)

    def _try_atomic_cached_channel_swap(self) -> bool:
        """If every visible tile of the NEW selection is already in the
        corrected cache, replace the display in one GUI event: clear the old
        channel's layers and pool the cached results synchronously, so the
        single `_update_layer_visibility()` at the end of `set_selection`
        publishes them. Returns True when it did.

        Why synchronous: the normal path issues requests and receives them
        through a queued signal, so at least one frame can be painted with
        the raw layer showing before the corrected tiles arrive -- a raw
        flash on a switch to a channel that was already fully prepared.

        Restricted to `self.level == 0` deliberately. These tiles are
        quantised now and a pooled tile keeps its quantisation; the new
        channel's gain table is not calibrated yet, so
        `_display_gain_for_level` returns 1.0 for every level. At level 0
        that is also the FINAL answer -- level 0's calibrated gain is 1.0 by
        construction -- so nothing shifts later. At a coarser level the
        eventual gain would differ and these tiles would sit at a different
        brightness from their neighbours, so there we fall through to the
        normal path.

        SCOPE, stated so it is not over-claimed: "switching to a fully
        cached corrected channel shows no raw flash" holds at LEVEL 0 ONLY.
        At a coarser level the switch takes the ordinary raw/provisional
        path, which is the safe behaviour, not the seamless one. Any
        acceptance run that exercises coarser levels must say so.
        """
        if not self._wants_precise() or self.level != 0:
            return False
        if not self._overview_matches_selection():
            # The range these would be quantised against is not this
            # channel's; see `_blocked_on_overview`.
            return False
        if not self._visible_tiles or self._current_bbox is None:
            return False
        cache = getattr(self.scheduler, "corrected_cache", None)
        if cache is None:
            return False

        pending = []
        for tx, ty in self._visible_tiles:
            key = self._make_correction_key(tx, ty)
            arr = cache.get(key)
            if arr is None:
                return False
            pending.append((tx, ty, key, arr))

        self._raw_pool.clear()
        self._precise_pool.clear()
        ts = self.grid.tile_size
        ds_y, ds_x = self._downsample_yx(self.level)
        for tx, ty, key, arr in pending:
            gray = self._quantize_corrected_uint8(arr, self.level)
            rect = ExploreView.world_rect(
                ty * ts, tx * ts, arr.shape[0], arr.shape[1], ds_y, ds_x)
            self._precise_pool.put(self.level, tx, ty, rect, gray, key)
        self.stats["precise_tiles_blitted"] += len(pending)
        self.stats["atomic_channel_swaps"] = self.stats.get("atomic_channel_swaps", 0) + 1
        return True

    def _enter_provisional(self):
        self._provisional = True
        self.provisional_changed.emit(True)
        self._update_layer_visibility()

    def _maybe_exit_provisional(self):
        """Restore full visibility / clear provisional once every visible
        tile has a matching, current precise key."""
        if not self._provisional:
            return
        if not self._visible_tiles:
            return
        if not self._wants_precise():
            return
        if self._coverage_complete():
            self._provisional = False
            self.provisional_changed.emit(False)
        self._update_layer_visibility()

    def _coverage_complete(self) -> bool:
        ctx = self.selection_key_context()
        for tx, ty in self._visible_tiles:
            entry = self._precise_pool.get(self.level, tx, ty)
            if entry is None or entry.key is None or not self._key_matches_context(entry.key, ctx):
                return False
        return True

    def _update_layer_visibility(self):
        """Display-policy gate (module docstring: anti-checkerboard +
        corrected floor + single-stage motion guarantee + progressive
        per-tile corrected coverage).

        `covered` is the CURRENT-LEVEL precise coverage boolean (unchanged
        meaning/contract from the old `_update_precise_visibility` -- it
        still drives `_precise_visible` / `view.precise_visible` and
        `_maybe_exit_provisional`, exactly as before). `floor_ok`
        additionally requires a ready corrected-floor image that matches
        the live selection context. While `wants_precise()` is true and the
        floor is not yet ready, the raw layer stays visible as the honest
        (if brighter) fallback; once the floor IS ready, the raw layer is
        forced entirely invisible so no raw-stage pixel can ever appear
        alongside corrected pixels.

        `covered` is NOT used to gate current-level precise visibility any
        more (module docstring): once the floor is ready, anything under a
        missing current-level tile is itself corrected-stage, so tiles are
        shown progressively, per tile, as they land -- the atomic
        all-or-nothing gate is kept only for the floor-not-ready window,
        where a corrected tile next to raw would still be a hard seam."""
        wants = self._wants_precise()
        covered = wants and bool(self._visible_tiles) and self._coverage_complete()
        floor_ctx = self._current_floor_ctx(self._floor_level, self._floor_stride)
        floor_ok = wants and self._floor_ready and self._floor_ctx == floor_ctx

        self._precise_visible = covered
        self.view.precise_visible = covered

        self.view.corrected_floor_item.setVisible(floor_ok)

        raw_on = (not wants) or (not floor_ok)
        self._raw_pool.apply_visibility(
            self.level, current_level_visible=raw_on, coarser_visible=raw_on)

        current_level_visible = True if floor_ok else covered
        self._precise_pool.apply_visibility(
            self.level, current_level_visible=current_level_visible, coarser_visible=True,
            key_ok=self._precise_key_current_for_level)

    # Backward-compatible alias (pre-rename name).
    _update_precise_visibility = _update_layer_visibility

    @staticmethod
    def _key_matches_context(key: CorrectionKey, ctx) -> bool:
        source, channel, method, eff_params, level, quality = ctx
        return (
            key.source == source and key.channel == channel and key.method == method
            and key.params == eff_params and key.tile.level == level and key.quality == quality
        )

    def _precise_key_current_for_level(self, key, level) -> bool:
        """Like `_key_matches_context` but for an arbitrary (typically
        coarser) pooled level: source/channel/method/quality must match the
        LIVE selection and `key.params` must equal the effective params for
        `level` specifically -- `effective_param` is level-dependent, so a
        coarser entry's expected params are NOT the current level's. Used
        as the precise pool's `apply_visibility(key_ok=...)` predicate so a
        stale-method/stale-radius coarse tile is hidden immediately on a
        selection change rather than shown as a fallback."""
        if key is None:
            return False
        source = self.provider.source_identity()
        ds = self.provider.level_downsample(level)
        eff_params = tuple(effective_param(p, level, ds) for p in self.params)
        return (
            key.source == source and key.channel == self.channel and key.method == self.method
            and key.params == eff_params and key.tile.level == level and key.quality == self.quality
        )

    # ── interaction contract ─────────────────────────────────────────────

    def snapshot(self) -> PrefetchSnapshot:
        """An immutable picture of the live selection + viewport. See
        `PrefetchSnapshot`."""
        ready = self._overview_matches_selection()
        return PrefetchSnapshot(
            epoch=self._interaction_epoch,
            source=self.provider.source_identity(),
            channel=self.channel,
            method=self.method,
            params=tuple(self.params),
            level=self.level,
            quality=self.quality,
            algorithm_version=BG_CORRECTION_ALGO_VERSION,
            bbox_l0=self._current_bbox,
            visible_tiles=frozenset(self._visible_tiles),
            overview_ready=ready,
            display_lo=self._display_lo if ready else None,
            display_hi=self._display_hi if ready else None,
        )

    def _emit_interaction(self, kind: str):
        self._interaction_epoch += 1
        self.interaction_event.emit(kind, self.snapshot())

    def _overview_matches_selection(self) -> bool:
        """Do `_overview_arr` / `view.overview_item` actually hold pixels
        for the LIVE (source, channel)? Anything that consumes the overview
        -- the corrected floor's input and the display-gain calibration's
        tissue-window search -- must check this first. Reusing a mismatched
        overview produces a result whose pixels come from one channel while
        its recorded identity claims another."""
        if self._overview_arr is None or self._overview_identity is None:
            return False
        return self._overview_identity == (self.provider.source_identity(), self.channel)

    # ── startup ───────────────────────────────────────────────────────────

    def _pick_overview_level(self) -> int:
        """Smallest pyramid level with max(h, w) >= 512, else the coarsest."""
        num_levels = self.provider.num_levels
        chosen = num_levels - 1
        for level in range(num_levels - 1, -1, -1):
            h, w = self.provider.level_shape(level)
            if max(h, w) >= 512:
                chosen = level
                break
        return chosen

    @staticmethod
    def _read_overview_record(provider, source, channel, level) -> OverviewRecord:
        """Read one channel's overview level and derive its display range.
        Pure with respect to controller state, so it is safe to call from a
        worker thread."""
        h, w = provider.level_shape(level)
        arr, _offset = provider.read_region(channel, level, 0, h, 0, w)
        arr = arr.astype(np.float32, copy=False)
        lo, hi = ExploreController._compute_display_levels(arr)
        return OverviewRecord(source=source, channel=channel, level=level,
                              arr=arr, display_lo=lo, display_hi=hi, shape=(h, w))

    def _overview_cache_get(self, source, channel, level):
        rec = self._overview_cache.get((source, channel, level))
        if rec is not None:
            self._overview_cache.move_to_end((source, channel, level))
        return rec

    @staticmethod
    def _record_bytes(rec: OverviewRecord) -> int:
        arr = rec.arr
        return int(getattr(arr, "nbytes", 0))

    def _overview_cache_put(self, rec: OverviewRecord):
        key = (rec.source, rec.channel, rec.level)
        self._overview_cache[key] = rec
        self._overview_cache.move_to_end(key)
        # Evict oldest-first until inside the byte budget, but never drop
        # the last record: a single oversized overview must still be usable.
        total = sum(self._record_bytes(r) for r in self._overview_cache.values())
        while total > OVERVIEW_CACHE_BYTES and len(self._overview_cache) > 1:
            _k, dropped = self._overview_cache.popitem(last=False)
            total -= self._record_bytes(dropped)

    def has_overview_record(self, channel: str, level: Optional[int] = None,
                            source=None) -> bool:
        """Public: is `channel` switch-ready? Having its corrected tiles
        cached is not enough -- without its overview record a switch still
        has to read, and the display range for it is unknown."""
        src = source if source is not None else self.provider.source_identity()
        lvl = self._pick_overview_level() if level is None else level
        return (src, channel, lvl) in self._overview_cache

    def _install_overview_record(self, rec: OverviewRecord):
        """Install a record as the live overview. GUI thread only, and no
        I/O: pixels and the display range that was derived FROM them are
        installed together, so brightness can never be interpreted against
        another channel's range."""
        ds_y, ds_x = self._downsample_yx(rec.level)
        h, w = rec.shape
        self._display_lo, self._display_hi = rec.display_lo, rec.display_hi
        self._overview_arr = rec.arr
        self._overview_identity = (rec.source, rec.channel)
        self._overview_level = rec.level
        self._overview_shape = rec.shape
        # Whole level read at once, so always fully valid -- plain
        # grayscale, never masked; fixed levels only, never autoLevels.
        self.view.overview_item.setImage(
            rec.arr, autoLevels=False, levels=(rec.display_lo, rec.display_hi))
        self.view.overview_item.setRect(ExploreView.world_rect(0, 0, h, w, ds_y, ds_x))
        self.view.overview_item.setVisible(True)
        self._overview_cache_put(rec)

    def _clear_overview(self):
        """Drop the live overview and hide it. Used the instant a switch
        lands on a channel whose overview is NOT resident: showing nothing
        is correct, showing the previous channel's pixels is not."""
        self._overview_arr = None
        self._overview_identity = None
        self.view.overview_item.setVisible(False)

    def load_overview(self, ensure_floor: bool = True):
        """Read this channel's overview level SYNCHRONOUSLY and install it.

        Used at startup, where a blocking read is fine because there is
        nothing on screen yet. A channel switch must not call this: the read
        costs p50 132.7ms / p95 292.9ms on the real 57-channel slide, which
        is a visible GUI stall. See `switch_overview_for_current_channel`.
        """
        source = self.provider.source_identity()
        level = self._pick_overview_level()
        rec = (self._overview_cache_get(source, self.channel, level)
               or self._read_overview_record(self.provider, source, self.channel, level))
        self._install_overview_record(rec)

        # `ensure_floor=False` is used by the channel-switch path, which
        # calls `_ensure_corrected_floor()` itself once, at the end. Starting
        # it here as well would launch a floor job and then immediately
        # supersede it: the second call bumps `_floor_gen`, so the first
        # job's whole read + correction + gain calibration is computed and
        # then thrown away as stale, and the pending job re-does all of it.
        if ensure_floor and self._wants_precise():
            self._ensure_corrected_floor()

    def switch_overview_for_current_channel(self) -> bool:
        """Make the live overview belong to `self.channel`, without ever
        blocking the GUI thread on a disk read.

        Returns True when a resident record was installed synchronously (a
        memcpy), False when the read had to be started in the background --
        in which case the old pixels are cleared IMMEDIATELY, so the gap is
        an honest empty/provisional state rather than another channel's
        image, and `_handle_overview_result` installs the result when it
        arrives.
        """
        source = self.provider.source_identity()
        level = self._pick_overview_level()
        rec = self._overview_cache_get(source, self.channel, level)
        if rec is not None:
            self._install_overview_record(rec)
            self.stats["overview_cache_hits"] = self.stats.get("overview_cache_hits", 0) + 1
            return True

        self.stats["overview_cache_misses"] = self.stats.get("overview_cache_misses", 0) + 1
        self._clear_overview()
        self.prepare_overview_async(self.channel)
        return False

    def _overview_ready_for_current(self) -> bool:
        return self._overview_matches_selection()

    def _blocked_on_overview(self) -> bool:
        """THE invariant: while the live channel's overview record is not
        installed, its display range is unknown, so NOTHING may be drawn or
        requested.

        This is a global guard, not a decision taken once inside
        `set_selection`. During the tens of milliseconds a cold switch waits
        for its record, a pan, a zoom, a navigator jump, a method or param
        change, or simply the 30ms motion timer would otherwise reach
        `_issue_raw_requests` / `_issue_settled_request` / the range
        handler's cache-serve path and quantise tiles against the PREVIOUS
        channel's range -- and quantisation happens once, at arrival, so a
        pooled tile keeps it for good.

        Also cancels the live generations, so work already queued for the
        old range is dropped rather than left to land.
        """
        if self._overview_matches_selection():
            return False
        self.scheduler.cancel_generation(self.view_generation)
        self.scheduler.cancel_generation(self._settled_generation)
        self._cancel_directional_prefetch()
        self.stats["blocked_on_overview"] = self.stats.get("blocked_on_overview", 0) + 1
        return True

    def prepare_overview_async(self, channel: str):
        """Read `channel`'s overview record on the single overview worker
        and cache it.

        Public because this is exactly what a background consumer must call
        for a neighbour channel: having that channel's corrected tiles
        cached is NOT enough to switch to it instantly -- without its
        overview record the switch still stalls on a read, and this
        channel's display range is unknown until it lands.

        Single-flight per (source, channel, level): a repeat call while one
        is in flight is a no-op, so a settle, a rapid switch and a prefetch
        all wanting the same channel cause one read, not three.
        """
        source = self.provider.source_identity()
        level = self._pick_overview_level()
        key = (source, channel, level)
        if self._overview_cache_get(source, channel, level) is not None:
            return
        if key in self._overview_inflight or self._torn_down:
            return
        self._overview_inflight.add(key)
        provider = self.provider

        def work():
            try:
                rec = self._read_overview_record(provider, source, channel, level)
                err = None
            except Exception as exc:  # noqa: BLE001 -- reported, never raised on a worker
                rec, err = None, exc
            self._overview_delivered.emit((key, rec, err))

        try:
            self._overview_pool.submit(work)
        except RuntimeError:
            # Pool already shut down (teardown raced us) -- drop the claim
            # so nothing is left permanently marked in flight.
            self._overview_inflight.discard(key)

    def _handle_overview_result(self, payload):
        """GUI-thread delivery. Caches every successful read -- a record for
        a channel the user has since left is still worth keeping, that is
        the point of the cache -- but only INSTALLS one that still matches
        the live selection.

        Installing is also what releases the work that a cold switch
        deliberately withheld. Until the record lands, this channel's
        display range is unknown, and anything quantised against the
        PREVIOUS channel's range would keep that quantisation for the life
        of the pooled tile (`_quantize_*` runs once, at arrival). So the
        cold path draws nothing and asks for nothing; it resumes here.
        """
        key, rec, err = payload
        self._overview_inflight.discard(key)
        source_k, channel_k, level_k = key
        if rec is None:
            self.stats["overview_read_failed"] = self.stats.get("overview_read_failed", 0) + 1
            if not self._torn_down:
                self.overview_prepared.emit(source_k, channel_k, level_k, False)
            return
        if self._torn_down:
            return
        self._overview_cache_put(rec)
        self.overview_prepared.emit(source_k, channel_k, level_k, True)

        source = self.provider.source_identity()
        if (rec.source, rec.channel) != (source, self.channel):
            return
        self._install_overview_record(rec)

        # The display range is known now, so the withheld work can start.
        atomic_swapped = self._try_atomic_cached_channel_swap()
        # ALWAYS, not only when the swap failed. A successful swap fills the
        # CURRENT level-0 viewport and nothing else; without a floor and a
        # gain table for this channel, the first zoom-out has no
        # corrected-stage fallback at all.
        if self._wants_precise():
            self._ensure_corrected_floor()
        if not atomic_swapped and self._current_bbox is not None:
            # Raw only: it issues the precise batch itself (see above).
            self._issue_raw_requests()
        self._maybe_exit_provisional()
        self._update_layer_visibility()
        self.selection_context_changed.emit(self.snapshot())

    # ── corrected floor (module docstring "corrected floor + single-stage
    # motion guarantee") ────────────────────────────────────────────────

    def _pick_floor_level_and_stride(self) -> Tuple[int, int]:
        """(level, stride) for the corrected floor. Prefers the COARSEST
        pyramid level whose max(h, w) >= FLOOR_MIN_MAX_DIM and whose pixel
        count stays under FLOOR_MAX_PIXELS (stride 1). If no level
        satisfies both, falls back to the coarsest level meeting
        FLOOR_MIN_MAX_DIM alone, decimated by the smallest integer stride
        `k` making `(h // k) * (w // k) <= FLOOR_MAX_PIXELS` (module
        docstring: the floor is an interactive-quality display proxy
        only). If every level is below FLOOR_MIN_MAX_DIM (a tiny image),
        the coarsest level is used at stride 1."""
        num_levels = self.provider.num_levels
        qualifying = []
        for level in range(num_levels):
            h, w = self.provider.level_shape(level)
            if max(h, w) >= FLOOR_MIN_MAX_DIM and h * w <= FLOOR_MAX_PIXELS:
                qualifying.append(level)
        if qualifying:
            return max(qualifying), 1

        big_enough = [
            level for level in range(num_levels)
            if max(self.provider.level_shape(level)) >= FLOOR_MIN_MAX_DIM
        ]
        if not big_enough:
            return num_levels - 1, 1

        level = max(big_enough)
        h, w = self.provider.level_shape(level)
        k = 1
        while (h // k) * (w // k) > FLOOR_MAX_PIXELS:
            k += 1
        return level, k

    def _current_floor_ctx(self, floor_level: Optional[int], stride: int = 1):
        """(source, channel, method, eff_params_at_floor_level) -- the
        identity tuple a ready floor image must match to be considered
        current for the LIVE selection (same scaling the tile path uses,
        via `effective_param`, but against the floor's TOTAL downsample
        `level_downsample(floor_level) * stride`)."""
        if floor_level is None:
            return None
        ds = self.provider.level_downsample(floor_level) * stride
        eff_params = tuple(effective_param(p, floor_level, ds) for p in self.params)
        source = self.provider.source_identity()
        return (source, self.channel, self.method, eff_params)

    def _display_gain_for_level(self, level: int) -> float:
        """Per-level display gain for CORRECTED pixels (module docstring).
        Returns 1.0 for EVERY level whenever `_gain_ctx` does not match the
        LIVE selection context -- a stale or never-calibrated table must
        never silently scale pixels."""
        if self._gain_ctx is None or self._gain_ctx != self._current_floor_ctx(
                self._floor_level, self._floor_stride):
            return 1.0
        return self._level_gain.get(level, 1.0)

    def _calibrate_level_gains(self, provider, compute, channel: str, method: str,
                                base_params: Tuple[int, ...],
                                overview_arr: np.ndarray, overview_level: int) -> Dict[int, float]:
        """Calibrate a per-level display gain (module docstring) by
        comparing the p99.5 highlight percentile of the SAME tissue window,
        corrected at every pyramid level with its level-scaled param,
        against level 0's. Runs entirely on the caller's thread (the floor
        worker thread -- see `_start_floor_job`). Returns `{level: gain}`;
        level 0 is always 1.0, a level with no non-degenerate contribution
        from any window is 1.0, every other gain is the MEDIAN ratio across
        `GAIN_WINDOWS` windows, clamped to `GAIN_CLAMP`."""
        num_levels = provider.num_levels
        base_param = int(base_params[0]) if base_params else 0

        # `overview_arr` is None when the resident overview does not belong
        # to THIS channel (see `_start_floor_job`). Read the level for the
        # right channel instead of degrading to an uncalibrated table: this
        # runs on the floor worker thread, so the blocking read is off the
        # GUI thread, and the provider hands out per-thread handles.
        if overview_arr is None:
            try:
                h, w = provider.level_shape(overview_level)
                overview_arr, _off = provider.read_region(
                    channel, overview_level, 0, h, 0, w)
                overview_arr = overview_arr.astype(np.float32, copy=False)
            except Exception:
                # Calibration is best-effort; an unusable overview yields an
                # all-1.0 table rather than a wrong one.
                return {}

        ds_y, ds_x = self._downsample_yx_for(provider, overview_level)
        windows_l0 = _pick_calibration_windows(overview_arr, ds_y, ds_x)

        ratios_by_level: Dict[int, list] = {L: [] for L in range(num_levels)}
        for (y0_l0, x0_l0) in windows_l0:
            p995_by_level: Dict[int, float] = {}
            for L in range(num_levels):
                ds_L = provider.level_downsample(L)
                h_L, w_L = provider.level_shape(L)
                win_L = max(1, int(round(GAIN_WINDOW_L0 / ds_L)))
                y0 = max(0, min(int(y0_l0 / ds_L), max(0, h_L - 1)))
                x0 = max(0, min(int(x0_l0 / ds_L), max(0, w_L - 1)))
                y1 = min(h_L, y0 + win_L)
                x1 = min(w_L, x0 + win_L)
                arr, _off = provider.read_region(channel, L, y0, y1, x0, x1)
                arr = arr.astype(np.float32, copy=False)
                param = effective_param(base_param, L, ds_L)
                corrected = compute.correct_array(arr, method, param)
                p995_by_level[L] = float(np.percentile(corrected, GAIN_PERCENTILE)) if corrected.size else 0.0

            p0 = p995_by_level.get(0, 0.0)
            for L in range(num_levels):
                pL = p995_by_level[L]
                if p0 <= 0 or pL <= 0:
                    continue  # degenerate -- this window contributes nothing for level L
                ratios_by_level[L].append(p0 / pL)

        gain_lo, gain_hi = GAIN_CLAMP
        gains: Dict[int, float] = {}
        for L in range(num_levels):
            if L == 0:
                gains[0] = 1.0
                continue
            vals = ratios_by_level[L]
            if not vals:
                gains[L] = 1.0
                continue
            gains[L] = float(np.clip(np.median(vals), gain_lo, gain_hi))
        return gains

    @staticmethod
    def _downsample_yx_for(provider, level: int) -> Tuple[float, float]:
        fn = getattr(provider, "level_downsample_yx", None)
        if fn is not None:
            return fn(level)
        ds = provider.level_downsample(level)
        return ds, ds

    def _on_floor_preparing_changed_for_badge(self, preparing: bool):
        self.view.set_status_text("Preparing corrected preview…" if preparing else None)

    def _ensure_corrected_floor(self):
        """(Re)request the corrected floor for the current selection.
        Called whenever `_wants_precise()` is true from `set_selection()`
        and from `load_overview()` (if a method is already set).
        Immediately hides the floor and marks it not-ready (module
        docstring: never show a stale-context floor).

        At most one floor computation runs at a time
        (`_floor_job_running`); a request that arrives while one is in
        flight is coalesced into a single pending flag rather than
        starting a second worker thread -- the in-flight job's result gets
        dropped as stale on arrival (its `_floor_gen` token no longer
        matches), and `_handle_floor_result` starts the pending job then,
        always against whatever selection is CURRENT at that point."""
        self._floor_gen += 1
        gen = self._floor_gen
        self._floor_ready = False
        self.view.corrected_floor_item.setVisible(False)
        self.floor_preparing_changed.emit(True)
        self._update_layer_visibility()

        if self._overview_arr is None:
            # `_display_lo/_display_hi` are fixed by `load_overview()`.
            # Quantizing a floor before that would bake in the placeholder
            # (0.0, 1.0) range and paint a saturated-white floor over the
            # whole slide. `load_overview()` re-enters here once the levels
            # are set, so deferring is safe -- and `floor_preparing_changed`
            # has already been emitted True, which is honest: the floor IS
            # pending.
            return

        if self._floor_job_running:
            self._floor_pending = True
            return
        self._start_floor_job(gen)

    def _start_floor_job(self, gen: int):
        """Read the floor array (GUI thread; reuses `_overview_arr` when
        levels match) and dispatch `compute.correct_array` on a worker
        thread. Exactly one such job is ever in flight (enforced by
        `_ensure_corrected_floor`/`_handle_floor_result`)."""
        self._floor_job_running = True
        floor_level, stride = self._pick_floor_level_and_stride()
        self._floor_level = floor_level
        self._floor_stride = stride
        self.stats["floor_level"] = floor_level
        self.stats["floor_stride"] = stride
        ctx = self._current_floor_ctx(floor_level, stride)

        # Reuse the already-resident overview array when the floor lands on
        # the same level (the common case -- both pickers land on the
        # coarsest level above their min-dimension threshold). Otherwise the
        # read happens on the WORKER thread below: a whole-level
        # `read_region` is blocking I/O and must never run on the GUI
        # thread. The provider hands out per-thread persistent handles, so
        # reading from the worker is safe.
        # Reuse the resident overview ONLY when it is the same level AND
        # holds pixels for the live (source, channel). Without the identity
        # test a channel switch fed the OLD channel's pixels into this
        # correction and then registered the result under the NEW channel's
        # context -- pixels from one channel, identity claiming another.
        # When it does not match, `work()` reads the level itself, on the
        # worker thread.
        overview_arr = None
        if (floor_level == getattr(self, "_overview_level", None)
                and self._overview_matches_selection()):
            overview_arr = self._overview_arr

        method = self.method
        eff_params = ctx[3]
        param = int(eff_params[0]) if eff_params else 0
        compute = self.compute
        provider = self.provider
        channel = self.channel
        k = stride
        base_params = self.params
        # Same hazard, second site: the gain calibration picks its
        # tissue-dense sampling windows by block means over this array. Fed
        # a stale channel's overview it would choose windows by the WRONG
        # channel's intensity distribution and calibrate the whole per-level
        # gain table against it. None here means "no usable overview" and
        # the calibration falls back to reading what it needs.
        cal_overview_arr = self._overview_arr if self._overview_matches_selection() else None
        cal_overview_level = getattr(self, "_overview_level", floor_level)

        def work():
            try:
                arr_in = overview_arr
                if arr_in is None:
                    h, w = provider.level_shape(floor_level)
                    arr_in, _off = provider.read_region(channel, floor_level, 0, h, 0, w)
                    arr_in = arr_in.astype(np.float32, copy=False)
                arr_in = _box_downsample(arr_in, k)
                result_arr = compute.correct_array(arr_in, method, param)
                error = None
            except Exception as exc:  # noqa: BLE001 -- reported via signal, never raised on worker thread
                result_arr = None
                error = exc

            # Calibration (module docstring "Per-level display gain for
            # CORRECTED pixels"): folded into this same floor job/thread --
            # no second thread, no second single-flight mechanism. A
            # calibration failure must never cost the user their floor, so
            # it is caught independently of the floor computation above.
            gains: Dict[int, float] = {}
            gain_error = None
            try:
                gains = self._calibrate_level_gains(
                    provider, compute, channel, method, base_params,
                    cal_overview_arr, cal_overview_level)
            except Exception as exc:  # noqa: BLE001 -- reported via signal
                gains = {}
                gain_error = exc

            self._floor_delivered.emit(
                (gen, ctx, floor_level, stride, result_arr, error, gains, gain_error))

        t = threading.Thread(target=work, daemon=True, name="explore-floor-compute")
        # Drop already-finished threads so a long session with many
        # selection changes does not retain one dead Thread per change.
        self._floor_threads = [x for x in self._floor_threads if x.is_alive()]
        self._floor_threads.append(t)
        t.start()

    def _handle_floor_result(self, payload):
        """GUI-side delivery guard (module docstring), same discipline as
        `_handle_precise_result`: drop the result if its generation token
        no longer matches the live `_floor_gen`, or if the selection
        context has since changed. Then, if a newer request was coalesced
        in while this job ran, start it -- never two jobs in flight."""
        gen, ctx, floor_level, stride, result_arr, error, gains, gain_error = payload
        self._floor_job_running = False
        if self._torn_down:
            self._floor_pending = False
            return

        current = gen == self._floor_gen and ctx == self._current_floor_ctx(floor_level, stride)
        accepted = False
        if current:
            if gain_error is not None:
                self.stats["gain_calibration_failed"] += 1
                gains = {}
            # Install the gain table under the same context guard as the
            # floor -- independent of whether the floor computation itself
            # succeeded (module docstring: "a failed calibration must not
            # cost the user their floor", and symmetrically a failed floor
            # must not cost the user a successful calibration).
            self._level_gain = gains
            self._gain_ctx = ctx if gains else None
            if error is not None or result_arr is None:
                self.stats["floor_compute_failed"] += 1
            else:
                # NOTE (module docstring): when stride > 1 the floor's
                # effective downsample exceeds its level's, so
                # gain[floor_level] slightly under-corrects the floor
                # specifically -- accepted, and inactive on the real data
                # (stride is 1 there).
                gray = self._quantize_corrected_uint8(result_arr, floor_level)
                ds_y, ds_x = self._downsample_yx(floor_level)
                ds_y, ds_x = ds_y * stride, ds_x * stride
                h, w = result_arr.shape
                rect = ExploreView.world_rect(0, 0, h, w, ds_y, ds_x)
                self.view.corrected_floor_item.setImage(gray, autoLevels=False, levels=(0, 255))
                self.view.corrected_floor_item.setRect(rect)
                self._floor_ready = True
                self._floor_ctx = ctx
                accepted = True
        self.stats["level_display_gain"] = dict(self._level_gain)
        self.stats["gain_calibrated"] = bool(self._level_gain)
        self.floor_ready_changed.emit(accepted)

        if self._floor_pending:
            self._floor_pending = False
            self._start_floor_job(self._floor_gen)
        else:
            self.floor_preparing_changed.emit(False)

        self._update_layer_visibility()

    def _downsample_yx(self, level: int) -> Tuple[float, float]:
        """(ds_y, ds_x) for `level`, using the provider's unrounded
        per-axis API when available, else falling back to the single
        rounded factor (for minimal fakes in older tests)."""
        fn = getattr(self.provider, "level_downsample_yx", None)
        if fn is not None:
            return fn(level)
        ds = self.provider.level_downsample(level)
        return ds, ds

    @staticmethod
    def _compute_display_levels(arr: np.ndarray) -> Tuple[float, float]:
        """(0, 99.5th percentile), guarding the degenerate all-zero/constant
        case so span-based normalization never divides by ~0."""
        lo = 0.0
        hi = float(np.percentile(arr, 99.5)) if arr.size else 0.0
        if not np.isfinite(hi) or hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def set_display_levels(self, lo: float, hi: float):
        """Public: reapply fixed display levels to overview/raw/precise.
        Never triggered automatically by new tile arrivals. Re-quantizes
        every currently-pooled item's pixels from its LAST delivered raw
        array is not tracked (only the quantized uint8 is kept) -- so this
        only affects the overview immediately; already-pooled tile items
        keep their existing quantization until next re-delivery. (This
        mirrors the fixed-levels contract: brightness is fixed at arrival
        time and does not silently rescale existing pixels.)"""
        self._display_lo = float(lo)
        self._display_hi = float(hi)
        if self._overview_arr is not None:
            self.view.overview_item.setImage(
                self._overview_arr, autoLevels=False,
                levels=(self._display_lo, self._display_hi))

    # ── level selection ───────────────────────────────────────────────────

    def _pick_display_level_with_hysteresis(self, screen_px_per_world_px: float) -> int:
        return planning.apply_level_hysteresis(
            ideal_level=self._pick_display_level(screen_px_per_world_px),
            current_level=self.level,
            current_downsample=self.provider.level_downsample(self.level),
            screen_px_per_world_px=screen_px_per_world_px,
            threshold=self.LEVEL_HYSTERESIS)

    def _pick_display_level(self, screen_px_per_world_px: float) -> int:
        """Nearest-below choice: for a given zoom (screen pixels per WORLD
        pixel), the ideal pyramid level has downsample ~= 1 /
        screen_px_per_world_px. Picks the level whose downsample is the
        largest one that does not exceed that ideal ratio, falling back to
        the finest level if none qualifies, and the coarsest if the ideal
        ratio is smaller than every available downsample."""
        downsamples = [self.provider.level_downsample(level)
                       for level in range(self.provider.num_levels)]
        return planning.pick_display_level(downsamples, screen_px_per_world_px)

    # ── viewport / range-change handling ─────────────────────────────────

    def _on_range_changed(self, *_args):
        """Runs on EVERY sigRangeChanged (no debounce here -- module
        docstring "Camera contract"). Cheap-only work: recompute the wanted
        tile set + z-order/visibility bookkeeping and run the prune check.
        Actual scheduler request issuing is what's debounced (below)."""
        t0 = time.perf_counter() if self.probe else None

        vb = self.view.view_box
        (x0, x1), (y0, y1) = vb.viewRange()
        screen_w = max(1, vb.width())
        world_w = max(1e-9, x1 - x0)
        screen_px_per_world_px = screen_w / world_w
        new_level = self._pick_display_level_with_hysteresis(screen_px_per_world_px)

        # Directional prefetch (module docstring): viewport centre in
        # level-0 WORLD coordinates, tracked on every range event (the
        # motion-timer tick reads this to compute per-tick displacement).
        self._viewport_center_l0 = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

        h0, w0 = self.provider.level_shape(0)
        bbox_l0 = planning.clamp_viewport_to_level0(y0, x0, y1, x1, h0, w0)
        self._current_bbox = bbox_l0

        old_level = self.level
        self.level = new_level
        if new_level != old_level:
            # Directional prefetch (module docstring): a display-level
            # change makes every pending candidate's level/effective-params
            # stale -- cancel rather than let it compute against the old
            # level.
            self._cancel_directional_prefetch()
        ds = self.provider.level_downsample(self.level)
        # `_viewport_shrinking` is the cheap zoom-direction signal for the
        # fallback look-ahead ring (see `_issue_settled_request`);
        # `_viewport_zooming` disqualifies the tick from directional
        # prefetch, which is pan-only.
        world_area = max(0.0, (x1 - x0)) * max(0.0, (y1 - y0))
        self._viewport_shrinking, self._viewport_zooming = (
            planning.classify_zoom(self._prev_world_area, world_area))
        self._prev_world_area = world_area

        prev_visible = self._visible_tiles
        prev_level = getattr(self, "_prev_level_for_serve", None)
        self._visible_tiles = planning.visible_tiles_for_viewport(
            bbox_l0, ds, self.grid.tile_size)
        # Serve a newly-visible tile IMMEDIATELY when its corrected result
        # is already in the cache, instead of waiting for the next 30ms
        # motion tick to issue the request (module docstring "Serving
        # prefetched tiles without tick latency"). `TileScheduler.request`
        # returns a cache hit synchronously, so this is a dict lookup plus
        # a queued signal per newly-exposed tile -- typically one tile
        # column, about four -- which keeps this handler cheap.
        #
        # Without it the directional prefetch could not show up in the
        # metric at all: every configuration measured coarser-fallback p95
        # pinned at exactly 20.0% (4 of 20 visible tiles, i.e. one full
        # column), because the leading column was computed and cached in
        # time but not BLITTED until the next tick. Prefetch alone moved
        # mean coarser fallback 14.6% -> 11.4%; with this, the same
        # prefetch reaches 1.6%.
        # A level switch is exactly when EVERY visible tile is new, and the
        # previous visible set is in a different level's tile coordinates,
        # so it is not comparable -- treat the whole set as newly exposed.
        # An earlier revision guarded this whole block with
        # `prev_level == self.level`, which switched the fast path off at
        # precisely the moment it is most useful: measured, coverage of the
        # new level was 0.0% at the instant of a switch even for tiles
        # already computed and resident in the corrected cache.
        if self._wants_precise() and not self._blocked_on_overview():
            level_switched = prev_level is not None and prev_level != self.level
            newly = (set(self._visible_tiles) if level_switched
                     else self._visible_tiles - prev_visible)
            if newly:
                # Synthesized coarse fallback (module docstring): a level
                # INCREASE (zoom-out, or a jump landing on a coarser level)
                # means the level that was just current is guaranteed
                # resident -- try building the new level's tiles from it
                # before falling through to the cache-serve-or-request path
                # below. Tiles this fails for (finer source missing/stale,
                # e.g. most of a wide zoom-out) fall through unchanged.
                zoom_out = level_switched and self.level > prev_level
                synthesized_now = set()
                if zoom_out:
                    for tx, ty in newly:
                        if self._synthesize_and_pool_fallback_tile(self.level, tx, ty):
                            synthesized_now.add((tx, ty))
                cache = getattr(self.scheduler, "corrected_cache", None)
                if cache is not None:
                    gen = self._settled_generation
                    for tx, ty in newly:
                        if (tx, ty) in synthesized_now:
                            continue
                        k = self._make_correction_key(tx, ty)
                        if cache.get(k) is not None:
                            self.scheduler.request(
                                TileRequest(key=k, generation=gen, priority=0),
                                self._on_precise_cache_hit)
        self._prev_level_for_serve = self.level
        self._update_layer_visibility()
        # Mount point 1 (module docstring "RawOverlayLayer"): `self.level`,
        # `self._current_bbox` and `self._visible_tiles` are all settled for
        # this frame, so the overlay's per-level visibility can be brought
        # in line IMMEDIATELY -- before its new tiles are even asked for.
        # Without this, a level change would leave the previous level's
        # overlay tiles on screen, and under additive composition they would
        # ADD to the incoming ones rather than being covered by them.
        if self._overlay is not None:
            self._overlay.apply_visibility(self.level)

        viewport_rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        margin_world = self.grid.tile_size * ds * PRUNE_MARGIN_TILES
        keep = {(self.level, tx, ty) for tx, ty in self._visible_tiles}
        self._raw_pool.prune(self.level, viewport_rect, margin_world, keep)
        self._precise_pool.prune(self.level, viewport_rect, margin_world, keep)
        self.stats["items_created"] = self._raw_pool.items_created + self._precise_pool.items_created
        self.stats["items_pruned"] = self._raw_pool.items_pruned + self._precise_pool.items_pruned

        # PAN vs ZOOM is decided here, where the viewport geometry that
        # distinguishes them is already computed, and handed to consumers
        # explicitly rather than left to be guessed from displacement.
        # Suppressed while `jump_to` is driving the camera: the contract is
        # that the source is EXPLICIT, and a jump would otherwise announce
        # itself twice -- once as PAN/ZOOM from `setRange`, then again as
        # NAVIGATOR_JUMP -- advancing the epoch twice and making a consumer
        # cancel and restart for no reason.
        if not self._jumping:
            self._emit_interaction("ZOOM" if self._viewport_zooming else "PAN")

        # Debounced: ISSUING requests (both raw and precise, from
        # `_issue_raw_requests`) waits for motion to settle for MOTION_MS
        # (module docstring "Camera contract"). `_settle_timer` is also
        # (re)started here but no longer gates any interactive request --
        # it is a future-refinement hook only (see `_on_settle`).
        # THROTTLE, not debounce: restarting the timer on every range event
        # means it never fires at all while the camera keeps moving (events
        # arrive faster than MOTION_MS), which is the same class of bug as
        # the settle gate this replaced -- a continuous drag would still
        # compute nothing. Leaving an already-running timer alone makes it
        # fire every MOTION_MS *during* motion instead.
        if not self._motion_timer.isActive():
            self._motion_timer.start(self.MOTION_MS)
        self._settle_timer.start(self.settle_ms)

        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["range_handler_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))

    def _issue_raw_requests(self):
        """The 30ms motion-timer callback (module docstring "Camera
        contract") -- the single place both layers' requests are issued
        from. Cancels the previous raw generation and issues center-out
        requests for the current wanted set's cache misses, then does the
        same for the precise (corrected) layer via `_issue_settled_request`
        (missing-tiles-only; a no-op when no method is selected). Despite
        the name (kept for the timer connection / existing call sites),
        this is no longer raw-only."""
        t0 = time.perf_counter() if self.probe else None

        if self._blocked_on_overview():
            return

        self.scheduler.cancel_generation(self.view_generation)
        self._raw_gen_n += 1
        self.view_generation = ("raw", self._raw_gen_n)

        bbox_l0 = self._current_bbox
        if bbox_l0 is None:
            return
        ds = self.provider.level_downsample(self.level)
        bbox_level = planning.bbox_to_level(bbox_l0, ds)
        visible = self._visible_tiles

        if self.probe:
            self._raw_probe_batch = {
                "start": time.perf_counter(), "visible": set(visible),
                "first": False, "full": False,
            }

        # ONE centre-out ordering, computed here and reused by the overlay
        # (module docstring "RawOverlayLayer"): two copies of this sort is
        # how the two channels would end up fetching in different orders.
        ordered = self._visible_tiles_center_out()
        missing = [coord for coord in ordered
                   if self._raw_pool.get(self.level, *coord) is None]
        gen = self.view_generation
        for i, (tx, ty) in enumerate(missing):
            key = self._make_raw_key(tx, ty)
            req = TileRequest(key=key, generation=gen, priority=i)
            self.scheduler.request(req, self._on_raw_result)

        # Mount point 2 (module docstring "RawOverlayLayer"): AFTER the
        # marker batch is submitted, and handed the SAME ordered sequence
        # the marker just filtered its own missing set out of -- the overlay
        # runs no request planning and no ordering of its own.
        if self._overlay is not None:
            self._overlay.sync(self.level, ordered)

        self._maybe_exit_provisional()

        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["request_issue_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))

        self._issue_settled_request()

        # Directional prefetch (module docstring): issued LAST, after the
        # visible raw batch, the intermediate-fallback urgent batch, the
        # current-level precise batch, and the fallback ring -- its
        # priority base sits above all of them regardless of order, but
        # issuing last keeps this call site's ordering self-documenting.
        self._issue_directional_prefetch()

    def jump_to(self, y0: int, x0: int, w: int, h: int):
        """Navigator / checkpoint jump: level-0 coordinates. Actually moves
        the camera (ViewBox.setRange with the world rect, padding=0) so the
        real range-changed handling path runs. Both the raw and precise
        request batches are then fired immediately (bypassing the motion
        timer) via `_issue_raw_requests`, which now issues both layers
        (module docstring "Camera contract")."""
        rect = QRectF(float(x0), float(y0), float(w), float(h))
        self._jumping = True
        try:
            self.view.view_box.setRange(rect=rect, padding=0)
        finally:
            self._jumping = False
        self._motion_timer.stop()
        self._issue_raw_requests()
        # The navigator KNOWS it jumped; it does not have to be inferred
        # from displacement. This is the explicit source a background
        # consumer consumes (module docstring / `interaction_event`).
        self._emit_interaction("NAVIGATOR_JUMP")
        # RESTART, do not stop. A jump ends with the camera stationary and
        # no further range events coming, so if the settle timer is left
        # stopped here `gesture_quiet` never fires after a jump and a
        # background consumer can never reach its own SETTLED.
        self._settle_timer.start(self.settle_ms)

    # ── settle / precise request ─────────────────────────────────────────

    def _clear_zoom_gesture_state(self):
        """`_viewport_zooming` is only recomputed when a range event
        arrives, so once the user stops moving it keeps whatever the LAST
        event set -- typically True at the end of a zoom. Anything that
        reads it as "a zoom is in progress" therefore stays wrong until the
        next camera event. This is called from the settle timer, i.e. after
        `settle_ms` with no range events, which is exactly "the gesture is
        over".

        A previous revision drove an atomic display gate off this flag and
        shipped that latent staleness as a visible fault: the gate held the
        new level hidden and re-based its own timeout on every delivery, so
        after the user stopped zooming the screen stayed on the blurrier
        coarse level indefinitely. That gate has been reverted; the flag is
        reset here so nothing else inherits the same trap."""
        self._viewport_zooming = False
        self._viewport_shrinking = False

    def _on_settle(self):
        """`_settle_timer`'s timeout callback. Interactive corrected-tile
        issuing no longer happens here (module docstring "Camera contract")
        -- it moved onto the 30ms motion timer alongside raw, because
        gating it on this 80ms-by-default quiet period meant a continuous
        drag never requested a single corrected tile until the user
        stopped moving (measured on the real slide: a 40-step drag went
        from 561ms drag-stop-to-full-coverage with 0 tiles computed during
        the drag, to 243ms with 23 computed during it).

        This hook is kept deliberately as a place for a FUTURE pass -- e.g.
        requesting a higher-quality / native-resolution refinement of the
        settled viewport once the camera has been still for a while -- that
        has not been built yet; do not route interactive precise-tile
        issuing back through it. It does do one small thing: mark the end
        of a camera gesture, since `settle_ms` with no range events is the
        only signal we get that the user stopped moving.

        `gesture_quiet` is emitted here, AFTER the gesture state is
        cleared, and means exactly that and nothing more. It is not a
        background policy's SETTLED: deciding the user is staying put long
        enough to be worth computing other channels is a longer, separate
        judgement that belongs to the consumer, which confirms its own
        additional quiet period on top of this one. The 80ms display timing
        that manual testing validated must not be stretched to serve
        background work."""
        self._clear_zoom_gesture_state()
        self.gesture_quiet.emit(self.snapshot())

    def _issue_settled_request(self, allow_local_synthesis: bool = True):
        """Cancel the previous precise generation, start a new one, and
        issue `CorrectionKey` requests for visible tiles that are MISSING
        under the current selection context -- i.e. `self._precise_pool`
        has no entry at `(self.level, tx, ty)` whose key matches
        `selection_key_context()` (`_key_matches_context`, the same
        predicate `_coverage_complete` uses -- one definition of "current"
        shared by both). A tile that already has a matching pooled entry,
        or is already in flight, needs no special handling here: the
        scheduler's cache check and single-flight dedup
        (`TileScheduler._pending`) cover both, so this only needs to filter
        out tiles that are demonstrably already covered.

        Called from `_issue_raw_requests` (the 30ms motion-timer callback)
        for coalesced, in-motion issuing, and directly from `set_selection`
        for immediate re-issuing on a selection change. A selection change
        needs no separate "force all" path: every visible tile's pooled key
        (if any) was computed against the OLD selection context, so it
        stops matching `selection_key_context()` the instant the selection
        changes, and every visible tile becomes "missing" here naturally.

        Skips entirely (after bumping/cancelling the generation) when no
        method is selected or there is no current viewport yet.

        `allow_local_synthesis=False` forbids the SYNCHRONOUS local
        construction of missing fallback-level tiles from pooled finer
        tiles; those tiles are still requested from the scheduler, so the
        fallback layer is still filled, just asynchronously. Used by the
        atomic cached channel swap, where the whole viewport is already
        covered by current-level corrected tiles: the synthesis is invisible
        at that moment and measured at ~30ms of GUI-thread `np.mean` inside
        a switch that costs ~90ms in total. Every other caller (motion
        ticks, pans, zooms) keeps the synchronous path, where the synthesized
        underlay IS what the user sees instead of the floor."""
        if self._blocked_on_overview():
            return
        self.scheduler.cancel_generation(self._settled_generation)
        self._settled_gen_n += 1
        self._settled_generation = ("precise", self._settled_gen_n)
        gen = self._settled_generation

        # Reset every call -- a disabled switch, or no fallback level
        # existing for the current level, must leave no stale membership
        # set behind for `_handle_precise_result` to accept against.
        self._fallback_level = None
        self._fallback_visible_tiles = set()

        if not self._wants_precise() or self._current_bbox is None:
            return

        ds = self.provider.level_downsample(self.level)
        bbox_level = planning.bbox_to_level(self._current_bbox, ds)
        tile_size = self.grid.tile_size
        visible = tiles_covering(bbox_level, tile_size)

        cy = (bbox_level[0] + bbox_level[2]) / 2.0
        cx = (bbox_level[1] + bbox_level[3]) / 2.0

        def dist(coord):
            tx, ty = coord
            tcy = ty * tile_size + tile_size / 2.0
            tcx = tx * tile_size + tile_size / 2.0
            return (tcy - cy) ** 2 + (tcx - cx) ** 2

        if self.probe:
            self._precise_probe_batch = {
                "start": time.perf_counter(), "visible": set(visible),
                "first": False, "full": False,
            }

        # ── (1) intermediate corrected fallback batch, at level + 1,
        # priorities starting at 0 (module docstring "Intermediate
        # corrected fallback") -- issued BEFORE the current level's batch
        # below, so it costs the current level almost nothing while buying
        # a complete, visually consistent underlay quickly. ──
        # The fallback batch is for PANNING, which exposes world area the
        # viewport has not shown before. A zoom-IN exposes none: the
        # viewport shrinks, and the level being left is already pooled and
        # already serving as the coarser fallback -- measured, the floor
        # stayed at 0.0% of the screen through a 12-step zoom with this
        # batch OFF as well, so it buys nothing there. Skipped while the
        # viewport is shrinking, purely to avoid pointless work.
        #
        # HONEST LIMIT: this guard does what it says (measured, zero
        # fallback requests are issued during a zoom) but it did NOT
        # recover the coverage gap that motivated it. With the fallback
        # enabled, current-level coverage during a zoom measured 64-67%
        # against 75-80% with it disabled, across three repeats, even
        # though both start from 100% coverage at the same level and
        # neither issues a fallback request during the zoom itself. The
        # cause is not identified. It is not the floor -- that stays at
        # 0.0% either way -- so the visible difference is more of the
        # screen showing 4x-blurred level+1 instead of sharp current-level
        # during the gesture. `intermediate_corrected_fallback` exists so
        # this can be A/B'd by eye (`--no-intermediate-fallback`).
        if self.intermediate_corrected_fallback and not self._viewport_shrinking:
            fallback_level = self.level + 1
            if fallback_level < self.provider.num_levels:
                fds = self.provider.level_downsample(fallback_level)
                fbbox_level = (
                    int(self._current_bbox[0] / fds), int(self._current_bbox[1] / fds),
                    int(self._current_bbox[2] / fds), int(self._current_bbox[3] / fds),
                )
                # One-tile look-ahead RING at the fallback level (module
                # docstring "Intermediate corrected fallback"). Without it
                # the fallback is only requested once the viewport already
                # needs it, so every crossing of a fallback-level tile
                # boundary reopens a window where the floor shows through:
                # measured, floor stayed at 6.4% of the screen during a
                # drag (p95 20.0%) with no ring, and 0.0% (p95 0.0%) with
                # it. It is cheap precisely because it is at the COARSER
                # level -- one fallback tile spans FALLBACK_HALO_TILES
                # times more world area than a current-level tile, so the
                # ring is a handful of tiles (measured 3 -> 15 requests
                # over a 25-step drag) and current-level throughput was
                # unchanged (36 tiles blitted either way). This is NOT the
                # current-level halo prefetch that measured as a
                # regression; see "Worker counts".
                fh, fw = self.provider.level_shape(fallback_level)
                pad = FALLBACK_HALO_TILES * tile_size
                fbbox_pad = (
                    max(0, fbbox_level[0] - pad), max(0, fbbox_level[1] - pad),
                    min(fh, fbbox_level[2] + pad), min(fw, fbbox_level[3] + pad),
                )
                fvisible = tiles_covering(fbbox_pad, tile_size)
                self._fallback_level = fallback_level
                self._fallback_visible_tiles = set(fvisible)

                fcy = (fbbox_level[0] + fbbox_level[2]) / 2.0
                fcx = (fbbox_level[1] + fbbox_level[3]) / 2.0

                def fdist(coord):
                    tx, ty = coord
                    tcy = ty * tile_size + tile_size / 2.0
                    tcx = tx * tile_size + tile_size / 2.0
                    return (tcy - fcy) ** 2 + (tcx - fcx) ** 2

                def is_missing_fallback(coord):
                    tx, ty = coord
                    entry = self._precise_pool.get(fallback_level, tx, ty)
                    return (entry is None or entry.key is None
                            or not self._precise_key_current_for_level(entry.key, fallback_level))

                # The batch splits by urgency. Fallback tiles that cover
                # the viewport RIGHT NOW are what keeps the floor off the
                # screen, so they go first, above the current level. The
                # look-ahead RING is speculative -- it only pays off on a
                # pan that has not happened yet -- so it queues BELOW the
                # current level, at FALLBACK_RING_BASE_PRIORITY, and gets
                # computed in the slack between motion ticks.
                #
                # Measured why this split is necessary: with the whole
                # ring at top priority, a zoom regressed current-level
                # coverage from 81.9% to 65.6%, because during a zoom the
                # previous level's own tiles are ALREADY serving as the
                # coarser fallback (floor was 0.0% either way), so the ring
                # was pure competition for the current level.
                finner = tiles_covering(fbbox_level, tile_size)
                fmissing = [c for c in fvisible if is_missing_fallback(c)]
                furgent = sorted((c for c in fmissing if c in finner), key=fdist)
                fring = sorted((c for c in fmissing if c not in finner), key=fdist)

                # Synthesized coarse fallback (module docstring): before
                # requesting a missing fallback-level tile from the
                # scheduler, try building it locally from already-pooled
                # finer (level - 1) tiles. Success pools it directly and
                # the tile is skipped from the request batch entirely;
                # failure (a source tile missing/stale, or a non-integer
                # level ratio) falls through to the request exactly as
                # before.
                if allow_local_synthesis:
                    furgent_to_request = [
                        c for c in furgent
                        if not self._synthesize_and_pool_fallback_tile(fallback_level, *c)
                    ]
                    fring_to_request = [
                        c for c in fring
                        if not self._synthesize_and_pool_fallback_tile(fallback_level, *c)
                    ]
                else:
                    # Not skipped, DEFERRED: every one of these is still
                    # requested below, so the fallback layer still fills --
                    # off the GUI thread.
                    furgent_to_request, fring_to_request = furgent, fring
                    self.stats["fallback_synthesis_deferred"] = (
                        self.stats.get("fallback_synthesis_deferred", 0)
                        + len(furgent) + len(fring))
                for i, (tx, ty) in enumerate(furgent_to_request):
                    key = self._make_correction_key(tx, ty, level=fallback_level)
                    req = TileRequest(key=key, generation=gen, priority=i)
                    self.scheduler.request(req, self._on_precise_result)
                for i, (tx, ty) in enumerate(fring_to_request):
                    key = self._make_correction_key(tx, ty, level=fallback_level)
                    req = TileRequest(key=key, generation=gen,
                                      priority=FALLBACK_RING_BASE_PRIORITY + i)
                    self.scheduler.request(req, self._on_precise_result)
                self.stats["mid_requests_issued"] += len(furgent_to_request) + len(fring_to_request)

        # ── (2) current level's precise batch, priorities starting at
        # PRECISE_CURRENT_BASE_PRIORITY (strictly above every fallback
        # priority above). ──
        ctx = self.selection_key_context()

        def is_missing(coord):
            tx, ty = coord
            entry = self._precise_pool.get(self.level, tx, ty)
            return (entry is None or entry.key is None
                    or not self._key_matches_context(entry.key, ctx))

        missing = [coord for coord in visible if is_missing(coord)]
        ordered = sorted(missing, key=dist)
        for i, (tx, ty) in enumerate(ordered):
            key = self._make_correction_key(tx, ty)
            req = TileRequest(key=key, generation=gen, priority=PRECISE_CURRENT_BASE_PRIORITY + i)
            self.scheduler.request(req, self._on_precise_result)

    # ── synthesized coarse fallback (module docstring "Synthesized coarse
    # fallback") ─────────────────────────────────────────────────────────

    def _try_synthesize_fallback_tile(self, fallback_level: int, tx: int, ty: int) -> Optional[np.ndarray]:
        """Try to build the `fallback_level` tile at `(tx, ty)` locally by
        downsampling the already-pooled, already-quantized `fallback_level
        - 1` tiles that tile its world area, instead of asking the
        scheduler to compute it.

        Returns the assembled uint8 array on success, or None (and
        increments `stats["fallback_synthesis_declined"]`) when:
        - there is no finer level (`fallback_level <= 0`);
        - the ratio between the two levels' downsample factors is not an
          exact integer (a guard for non-4x-per-level pyramids -- the real
          tonsil pyramid is exactly 4x, so this path is inactive there);
        - ANY finer-level tile covering the fallback tile's area is
          missing from `_precise_pool`, or present with a key that is not
          current for the finer level (`_precise_key_current_for_level`) --
          partial synthesis would leave holes, worse than the computed
          tile the caller falls back to requesting.

        The source tiles are QUANTIZED uint8 pixels (already carrying the
        finer level's own calibrated display gain), and the result is a
        plain box-downsample of them -- NEVER re-quantized or re-gained,
        since it is by construction a downsample of what is already
        correctly on screen at the finer level (module docstring). A source
        taken from the corrected CACHE instead of the pool is float32, so
        it IS quantized once, with the finer level's gain -- which produces
        exactly the pixels that tile would display.

        MEASURED HIT RATE, so nobody assumes this carries the zoom-out
        case: over a 15-step level-crossing zoom-out it fired 2 times and
        declined 65; over a 25-step pan, 1 and 38. The reason is geometry,
        not staleness -- one fallback tile needs a complete, GRID-ALIGNED
        k-by-k block of finer tiles (k=4 on this pyramid, so 16 of them),
        and a viewport at the finer level is barely wider than a single
        fallback tile, so a browsing path covers strips rather than whole
        aligned blocks. Sourcing from the cache as well as the pool roughly
        doubled the rate and left it small. It is kept because when it does
        fire the result is EXACT, and an "explore an area, then zoom out"
        pattern is the case it is built for."""
        finer_level = fallback_level - 1
        if finer_level < 0:
            self.stats["fallback_synthesis_declined"] += 1
            return None

        ds_finer = self.provider.level_downsample(finer_level)
        ds_fallback = self.provider.level_downsample(fallback_level)
        if ds_finer <= 0:
            self.stats["fallback_synthesis_declined"] += 1
            return None
        ratio = ds_fallback / ds_finer
        k = int(round(ratio))
        if k < 1 or abs(ratio - k) > 1e-6:
            self.stats["fallback_synthesis_declined"] += 1
            return None

        finer_tx0 = tx * k
        finer_ty0 = ty * k
        rows = []
        for fty in range(finer_ty0, finer_ty0 + k):
            row_arrs = []
            for ftx in range(finer_tx0, finer_tx0 + k):
                entry = self._precise_pool.get(finer_level, ftx, fty)
                if (entry is not None and entry.key is not None
                        and self._precise_key_current_for_level(entry.key, finer_level)):
                    row_arrs.append(entry.item.image)
                    continue
                # The pool only holds what has been BLITTED; the
                # corrected cache holds everything computed, including
                # prefetched tiles and previously-visited ones, so it is a
                # far larger source. A cached tile is float32 corrected
                # values, so it must be quantized with the FINER level's
                # gain -- which is exactly what that tile would display.
                cache = getattr(self.scheduler, "corrected_cache", None)
                cached = None
                if cache is not None:
                    cached = cache.get(self._make_correction_key(ftx, fty, level=finer_level))
                if cached is None:
                    self.stats["fallback_synthesis_declined"] += 1
                    return None
                row_arrs.append(self._quantize_corrected_uint8(cached, finer_level))
            rows.append(row_arrs)

        try:
            assembled = np.block(rows)
        except ValueError:
            # Mismatched constituent shapes (e.g. an edge tile truncated to
            # less than a full tile_size) -- decline rather than guess.
            self.stats["fallback_synthesis_declined"] += 1
            return None

        downsampled = _box_downsample(assembled, k)
        result = np.clip(np.round(downsampled), 0, 255).astype(np.uint8)
        self.stats["fallback_synthesized"] += 1
        return result

    def _synthesize_and_pool_fallback_tile(self, fallback_level: int, tx: int, ty: int) -> bool:
        """Attempt `_try_synthesize_fallback_tile`; on success, pool the
        result directly at `fallback_level` with a `CorrectionKey` built
        for that level (`_make_correction_key(level=fallback_level)`), so
        `_precise_key_current_for_level` accepts it later and a selection
        change invalidates it exactly like a computed tile. Returns True on
        success (the caller must then skip requesting this tile)."""
        arr_u8 = self._try_synthesize_fallback_tile(fallback_level, tx, ty)
        if arr_u8 is None:
            return False
        ds_y, ds_x = self._downsample_yx(fallback_level)
        ts = self.grid.tile_size
        rect = ExploreView.world_rect(ty * ts, tx * ts, arr_u8.shape[0], arr_u8.shape[1], ds_y, ds_x)
        key = self._make_correction_key(tx, ty, level=fallback_level)
        self._precise_pool.put(fallback_level, tx, ty, rect, arr_u8, key)
        return True

    # ── directional prefetch (module docstring "Directional prefetch
    # (pan only)") ───────────────────────────────────────────────────────

    def _cancel_directional_prefetch(self):
        """Bump the `("dirprefetch", n)` generation, cancel it on the
        scheduler, and drop the pending candidate list. Work already
        dispatched under the OLD generation is left to run to completion
        (module docstring: `TileScheduler._run_compute` caches it
        regardless), so this only stops FUTURE issuing under the stale
        generation -- it never reaches back into the scheduler's already-
        running jobs. `stats["dir_prefetch_cancelled"]` only counts a
        cancellation that actually had pending candidates or in-flight
        requests to drop, not routine bumps against an already-empty
        state (e.g. the very first call)."""
        had_activity = bool(self._dirprefetch_candidates) or self._dirprefetch_inflight > 0
        self.scheduler.cancel_generation(self._dirprefetch_generation)
        self._dirprefetch_gen_n += 1
        self._dirprefetch_generation = ("dirprefetch", self._dirprefetch_gen_n)
        self._dirprefetch_candidates = []
        self._dirprefetch_inflight = 0
        if had_activity:
            self.stats["dir_prefetch_cancelled"] += 1

    def _compute_dirprefetch_candidates(self, direction: Tuple[float, float]) -> list:
        """Candidate corridor (module docstring): the current viewport's
        tile-space rect at `self.level`, translated forward along
        `direction` (a unit vector, level-0-world axes) by
        `DIRECTIONAL_PREFETCH_CORRIDOR` viewports, unioned with the
        original rect and covered by tiles. Already-visible tiles and
        tiles already covered under the live selection are subtracted;
        the remainder is sorted by (a proxy for) distance from the
        leading edge of the viewport along the direction of travel --
        projecting each candidate's centre onto `direction` relative to
        the viewport CENTRE, which differs from the leading edge only by
        a per-tick constant offset and so preserves ordering -- nearest
        first, truncated to `DIRECTIONAL_PREFETCH_BUDGET`."""
        ux, uy = direction
        bbox_l0 = self._current_bbox
        if bbox_l0 is None:
            return []
        ds = self.provider.level_downsample(self.level)
        y0 = int(bbox_l0[0] / ds)
        x0 = int(bbox_l0[1] / ds)
        y1 = int(bbox_l0[2] / ds)
        x1 = int(bbox_l0[3] / ds)
        width = max(1, x1 - x0)
        height = max(1, y1 - y0)

        trans_x = ux * DIRECTIONAL_PREFETCH_CORRIDOR * width
        trans_y = uy * DIRECTIONAL_PREFETCH_CORRIDOR * height
        translated = (y0 + trans_y, x0 + trans_x, y1 + trans_y, x1 + trans_x)

        union_bbox = (
            min(y0, translated[0]), min(x0, translated[1]),
            max(y1, translated[2]), max(x1, translated[3]),
        )
        h_level, w_level = self.provider.level_shape(self.level)
        clamped = (
            max(0, int(union_bbox[0])), max(0, int(union_bbox[1])),
            min(h_level, int(round(union_bbox[2]))), min(w_level, int(round(union_bbox[3]))),
        )
        tile_size = self.grid.tile_size
        corridor_tiles = tiles_covering(clamped, tile_size)

        ctx = self.selection_key_context()

        def already_covered(coord) -> bool:
            tx, ty = coord
            entry = self._precise_pool.get(self.level, tx, ty)
            return (entry is not None and entry.key is not None
                    and self._key_matches_context(entry.key, ctx))

        candidates = [
            c for c in corridor_tiles
            if c not in self._visible_tiles and not already_covered(c)
        ]

        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0

        def advancement(coord):
            tx, ty = coord
            tcx = tx * tile_size + tile_size / 2.0
            tcy = ty * tile_size + tile_size / 2.0
            return (tcx - cx) * ux + (tcy - cy) * uy

        candidates.sort(key=advancement)
        return candidates[:DIRECTIONAL_PREFETCH_BUDGET]

    def _issue_directional_prefetch(self):
        """Motion-timer-tick entry point (called last from
        `_issue_raw_requests`, module docstring "Directional prefetch
        (pan only)"). Pan-only, cache-only, bounded by an in-flight cap
        rather than by priority alone -- see the module docstring for why
        priority alone regressed the earlier (rejected) symmetric halo
        design."""
        gate_ok = (
            self.directional_prefetch and self._wants_precise()
            and not self._viewport_zooming and self._current_bbox is not None
        )
        if not gate_ok:
            self._cancel_directional_prefetch()
            self._dirprefetch_velocity = (0.0, 0.0)
            self._dirprefetch_prev_center = None
            self._dirprefetch_last_direction = None
            return

        center = self._viewport_center_l0
        prev = self._dirprefetch_prev_center
        dx = dy = 0.0
        if prev is not None and center is not None:
            dx = center[0] - prev[0]
            dy = center[1] - prev[1]
        self._dirprefetch_prev_center = center

        ema = DIRECTIONAL_PREFETCH_EMA
        vx = ema * dx + (1.0 - ema) * self._dirprefetch_velocity[0]
        vy = ema * dy + (1.0 - ema) * self._dirprefetch_velocity[1]
        self._dirprefetch_velocity = (vx, vy)

        ds = self.provider.level_downsample(self.level)
        tile_world = max(1e-9, self.grid.tile_size * ds)
        mag_tiles = ((vx * vx + vy * vy) ** 0.5) / tile_world
        valid = mag_tiles > DIRECTIONAL_PREFETCH_MIN_TILES

        direction = None
        if valid:
            norm = (vx * vx + vy * vy) ** 0.5
            direction = (vx / norm, vy / norm)

        last_dir = self._dirprefetch_last_direction
        material_change = False
        if last_dir is not None:
            if not valid:
                material_change = True
            else:
                dot = direction[0] * last_dir[0] + direction[1] * last_dir[1]
                if dot < 0.0:
                    material_change = True

        if material_change:
            self._cancel_directional_prefetch()
            self.stats["dir_prefetch_direction_changes"] += 1

        self._dirprefetch_last_direction = direction

        if not valid:
            self._dirprefetch_candidates = []
            return

        self._dirprefetch_candidates = self._compute_dirprefetch_candidates(direction)
        self._dirprefetch_fill_inflight()

    def _dirprefetch_fill_inflight(self):
        """Issue candidates one at a time until `DIRECTIONAL_PREFETCH_
        INFLIGHT` are outstanding under the CURRENT generation -- the
        in-flight cap (module docstring), not priority, is what bounds
        compute-worker occupancy."""
        gen = self._dirprefetch_generation
        while (self._dirprefetch_inflight < DIRECTIONAL_PREFETCH_INFLIGHT
               and self._dirprefetch_candidates):
            tx, ty = self._dirprefetch_candidates.pop(0)
            priority = DIRECTIONAL_PREFETCH_BASE_PRIORITY + self._dirprefetch_inflight
            key = self._make_correction_key(tx, ty)
            req = TileRequest(key=key, generation=gen, priority=priority)
            self.scheduler.request(req, self._on_dirprefetch_result)
            self._dirprefetch_inflight += 1
            self.stats["dir_prefetch_issued"] += 1

    def _on_dirprefetch_result(self, result):
        """Scheduler callback -- fires on a worker thread (or synchronously
        on a cache hit). Marshal to the GUI thread via a queued signal,
        same discipline as every other delivery path."""
        self._dirprefetch_delivered.emit(result)

    def _handle_dirprefetch_result(self, result):
        """GUI-side handler: pixels are discarded unconditionally -- a
        directional-prefetch result is CACHE-ONLY and must never be
        blitted or pooled (module docstring). If the request's generation
        still matches the live one, free its in-flight slot and refill
        from the pending candidate list (the natural back-pressure that
        keeps at most `DIRECTIONAL_PREFETCH_INFLIGHT` outstanding). A
        stale-generation result (the generation was cancelled while this
        was in flight) still counts as completed -- the scheduler already
        cached it regardless -- but does not touch the (already-reset)
        in-flight counter or candidate list for the new generation."""
        self.stats["dir_prefetch_completed"] += 1
        req = result.request
        if req.generation != self._dirprefetch_generation:
            return
        self._dirprefetch_inflight = max(0, self._dirprefetch_inflight - 1)
        self._dirprefetch_fill_inflight()

    # ── delivery: raw ─────────────────────────────────────────────────────

    def _on_raw_result(self, result):
        """Scheduler callback — fires on a worker thread (or synchronously
        on a cache hit). Marshal to the GUI thread via a queued signal."""
        self._raw_delivered.emit(result)

    def _handle_raw_result(self, result):
        """GUI-side delivery guard (module docstring): the generation must
        match the LIVE view_generation exactly (not merely "not stale" --
        immune to the queued-signal race), the tile must still be in the
        current wanted set at the current level, and channel/source/level
        must match."""
        if result.error is not None or result.pixels is None:
            return
        req = result.request
        key = req.key
        if not isinstance(key, RawKey):
            return
        if not self._overview_matches_selection():
            # Defence in depth behind `_blocked_on_overview`: a result that
            # was already in flight when the channel changed would be
            # quantised against a range that is not this channel's, and a
            # pooled tile keeps its quantisation.
            self.stats["mismatched_raw_dropped"] += 1
            return
        if req.generation != self.view_generation:
            self.stats["late_raw_rejected"] += 1
            return
        tile = key.tile
        current_source = self.provider.source_identity()
        if key.channel != self.channel or key.source != current_source or \
                tile.level != self.level:
            self.stats["mismatched_raw_dropped"] += 1
            return
        if (tile.tx, tile.ty) not in self._visible_tiles:
            self.stats["late_raw_rejected"] += 1
            return

        t0 = time.perf_counter() if self.probe else None
        arr = result.pixels.handle
        rgba_or_gray = self._quantize_tile_uint8(arr)
        ds_y, ds_x = self._downsample_yx(tile.level)
        rect = ExploreView.world_rect(
            tile.ty * self.grid.tile_size, tile.tx * self.grid.tile_size,
            arr.shape[0], arr.shape[1], ds_y, ds_x)
        self._raw_pool.put(tile.level, tile.tx, tile.ty, rect, rgba_or_gray, key)
        self.stats["raw_tiles_blitted"] += 1
        self.stats["items_created"] = self._raw_pool.items_created + self._precise_pool.items_created
        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["tile_item_update_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))
            self._probe_note_raw_progress(tile.tx, tile.ty)

        self._update_layer_visibility()

    # ── delivery: precise ─────────────────────────────────────────────────

    def _on_precise_cache_hit(self, result):
        """Callback for the range handler's cache-serve path. A cache hit
        resolves SYNCHRONOUSLY inside `TileScheduler.request`, i.e. still on
        the GUI thread inside `_on_range_changed`, so it is handled inline.

        Routing it through the usual queued signal would lose it: the 30ms
        motion tick bumps `_settled_generation`, and a queued delivery that
        lands after that bump fails `_handle_precise_result`'s exact
        generation check and is dropped. Most hits survived that race,
        which is why the improvement still measured, but some were silently
        discarded. Anything that does NOT resolve synchronously (a miss
        that completes on a worker thread) still goes through the queued
        path, which is the only thread-safe option there."""
        if QtCore.QThread.currentThread() is self.thread():
            self._handle_precise_result(result)
        else:
            self._precise_delivered.emit(result)

    def _on_precise_result(self, result):
        self._precise_delivered.emit(result)

    def _handle_precise_result(self, result):
        """GUI-side delivery guard (module docstring). Accepts a tile at
        `self.level` (as before) OR, when `intermediate_corrected_fallback`
        is enabled, at `self.level + 1` (module docstring "Intermediate
        corrected fallback") -- each validated against its OWN level's
        identity/membership, never the current level's. A fallback tile
        never touches `_visible_tiles` / `selection_key_context()` / the
        coverage machinery, so it can never count toward "the viewport is
        covered"."""
        req = result.request
        if req.generation != self._settled_generation:
            self.stats["stale_precise_dropped"] += 1
            return
        if result.error is not None or result.pixels is None:
            return
        if not self._overview_matches_selection():
            # Same defence as the raw path: this channel's display range is
            # not installed, so nothing may be quantised yet.
            self.stats["mismatched_key_dropped"] += 1
            return
        key = req.key
        if not isinstance(key, CorrectionKey):
            return
        tile = key.tile

        is_fallback = False
        if tile.level == self.level:
            current_ctx = self.selection_key_context()
            if not self._key_matches_context(key, current_ctx):
                self.stats["mismatched_key_dropped"] += 1
                return
            if (tile.tx, tile.ty) not in self._visible_tiles:
                self.stats["late_precise_rejected"] += 1
                return
        elif (self.intermediate_corrected_fallback
              and self._fallback_level is not None
              and tile.level == self._fallback_level
              and self._fallback_level == self.level + 1):
            if not self._precise_key_current_for_level(key, tile.level):
                self.stats["mismatched_key_dropped"] += 1
                return
            if (tile.tx, tile.ty) not in self._fallback_visible_tiles:
                self.stats["late_precise_rejected"] += 1
                return
            is_fallback = True
        else:
            self.stats["late_precise_rejected"] += 1
            return

        t0 = time.perf_counter() if self.probe else None
        arr = result.pixels.handle
        gray = self._quantize_corrected_uint8(arr, tile.level)
        ds_y, ds_x = self._downsample_yx(tile.level)
        rect = ExploreView.world_rect(
            tile.ty * self.grid.tile_size, tile.tx * self.grid.tile_size,
            arr.shape[0], arr.shape[1], ds_y, ds_x)
        self._precise_pool.put(tile.level, tile.tx, tile.ty, rect, gray, key)
        if is_fallback:
            self.stats["mid_tiles_blitted"] += 1
        else:
            self.stats["precise_tiles_blitted"] += 1
        self.stats["items_created"] = self._raw_pool.items_created + self._precise_pool.items_created
        if self.probe and t0 is not None:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.timings["tile_item_update_ms"].append(dt_ms)
            self.timings["frame_events"].append((time.perf_counter(), dt_ms))
            if not is_fallback:
                self._probe_note_precise_progress(tile.tx, tile.ty)

        self._maybe_exit_provisional()
        self._update_layer_visibility()

    # ── quantization (once, at arrival) ─────────────────────────────────

    def _quantize_tile_uint8(self, arr: np.ndarray) -> np.ndarray:
        """uint8 grayscale quantization under the FIXED display levels,
        performed exactly ONCE per delivered tile (module docstring).
        `rgb = round(clip((v-lo)/(hi-lo), 0, 1) * 255)`; no alpha channel
        is needed since each tile item is fully opaque within its own rect
        (there is no shared canvas with unfilled holes any more)."""
        span = max(self._display_hi - self._display_lo, 1e-6)
        # Same arithmetic, same ORDER, one scratch buffer. The chain used to
        # allocate a fresh 512x512 float32 array per operation (subtract,
        # divide, clip, scale, round) -- six temporaries per tile, and an
        # atomic channel swap quantises the whole viewport at once, measured
        # at ~11ms for 16 tiles inside a ~90ms switch. Folding the constants
        # into a single multiply-add would be faster still, but it changes
        # the floating-point rounding, and this function's output is the
        # pixels the user compares between channels -- so the operations are
        # kept identical and only the allocations removed.
        buf = np.array(arr, dtype=np.float32, copy=True)
        buf -= self._display_lo
        buf /= span
        np.clip(buf, 0.0, 1.0, out=buf)
        buf *= 255.0
        np.round(buf, out=buf)
        return buf.astype(np.uint8)

    def _quantize_corrected_uint8(self, arr: np.ndarray, level: int) -> np.ndarray:
        """Like `_quantize_tile_uint8`, but for CORRECTED pixels only
        (precise tiles and the floor): multiplies by the calibrated
        per-level display gain (module docstring "Per-level display gain
        for CORRECTED pixels") BEFORE normalizing/clipping against the same
        fixed display range. `_display_gain_for_level` returns 1.0 for an
        uncalibrated or stale table, so this is a no-op difference from
        `_quantize_tile_uint8` in that case."""
        gain = self._display_gain_for_level(level)
        if gain == 1.0:
            # The overwhelmingly common case (level 0's calibrated gain is
            # 1.0 by construction): skip a full multiply and a temporary.
            return self._quantize_tile_uint8(arr)
        gained = arr.astype(np.float32, copy=False) * gain
        return self._quantize_tile_uint8(gained)

    # ── probe-only viewport-first/full progress tracking ─────────────────

    def _probe_note_raw_progress(self, tx, ty):
        batch = self._raw_probe_batch
        if batch is None or (tx, ty) not in batch["visible"]:
            return
        now = time.perf_counter()
        blitted = {(e.tx, e.ty) for e in self._raw_pool.entries.values() if e.level == self.level}
        if not batch["first"]:
            batch["first"] = True
            self.timings["viewport_first_raw_tile_ms"].append((now - batch["start"]) * 1000.0)
        if not batch["full"] and batch["visible"] <= blitted:
            batch["full"] = True
            self.timings["viewport_full_raw_tile_ms"].append((now - batch["start"]) * 1000.0)

    def _probe_note_precise_progress(self, tx, ty):
        batch = self._precise_probe_batch
        if batch is None or (tx, ty) not in batch["visible"]:
            return
        now = time.perf_counter()
        ctx = self.selection_key_context()
        covered = {
            (e.tx, e.ty) for e in self._precise_pool.entries.values()
            if e.level == self.level and e.key is not None and self._key_matches_context(e.key, ctx)
        }
        if not batch["first"]:
            batch["first"] = True
            self.timings["viewport_first_precise_tile_ms"].append((now - batch["start"]) * 1000.0)
        if not batch["full"] and batch["visible"] <= covered:
            batch["full"] = True
            self.timings["viewport_full_precise_ms"].append((now - batch["start"]) * 1000.0)

    # ── teardown ──────────────────────────────────────────────────────────

    def teardown(self, shutdown_backend: bool = True, *,
                 wait_for_floor: bool = False):
        """Stop timers, disconnect signals, then (if `shutdown_backend`)
        scheduler.shutdown(), then provider.close(). Order recorded in
        `_teardown_order`.

        `wait_for_floor` decides what happens to a floor computation that is
        still running, and the two answers are for two different callers:

        * False (default) -- the ordinary path, e.g. closing the window.
          Each floor thread is joined with a TIMEOUT and teardown proceeds
          either way. A late result is harmless (`_handle_floor_result`
          drops it once `_torn_down` is set), so finishing quickly matters
          more than finishing tidily.
        * True -- a HAND-OFF, where something else is about to use the GPU
          this floor job is on. Here the join must NOT time out: returning
          while the floor thread is still computing is exactly the double
          GPU use the hand-off exists to prevent. It blocks for as long as
          the job takes (measured 0.36s on the real slide; jobs run
          0.4-1.1s), on the caller's thread, deliberately -- correctness
          before responsiveness, and no event re-entry to keep a spinner
          alive.
        """
        if self._torn_down:
            return
        self._torn_down = True

        # The overlay goes FIRST and it releases only what it owns -- its
        # generation, its signals, its pool. It never shuts the scheduler or
        # the provider down: those are shared, this controller owns them,
        # and they are closed exactly once, below. Physical reads already
        # started for the overlay are drained by that one shutdown; there is
        # deliberately no second drain mechanism.
        if self._overlay is not None:
            self._teardown_order.append("overlay.teardown")
            self._overlay.teardown()

        self._settle_timer.stop()
        self._motion_timer.stop()
        try:
            self.view.view_box.sigRangeChanged.disconnect(self._on_range_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            self._raw_delivered.disconnect(self._handle_raw_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self._precise_delivered.disconnect(self._handle_precise_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self._floor_delivered.disconnect(self._handle_floor_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self._dirprefetch_delivered.disconnect(self._handle_dirprefetch_result)
        except (TypeError, RuntimeError):
            pass
        try:
            self.floor_preparing_changed.disconnect(self._on_floor_preparing_changed_for_badge)
        except (TypeError, RuntimeError):
            pass

        # Floor-compute worker threads are plain daemon threads (not owned
        # by Qt); join them here so teardown leaves nothing running behind
        # it. With `wait_for_floor` false this is best-effort cleanup and
        # not a correctness requirement -- a result arriving after
        # `_torn_down = True` is a no-op (`_handle_floor_result`'s guard
        # above). With it true the join is the whole point and must not
        # give up: see this method's docstring.
        floor_join_timeout = None if wait_for_floor else 2.0
        for t in self._floor_threads:
            if t.is_alive():
                t.join(timeout=floor_join_timeout)
        # Before `provider.close()` below: an overview read in flight would
        # otherwise touch a closed provider.
        pool = getattr(self, "_overview_pool", None)
        if pool is not None:
            pool.shutdown(wait=True)

        if not shutdown_backend:
            return

        self._teardown_order.append("scheduler.shutdown")
        self.scheduler.shutdown()
        self._teardown_order.append("provider.close")
        self.provider.close()
