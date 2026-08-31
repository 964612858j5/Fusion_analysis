# Channel-switch latency — manual measurement, real 57-channel slide

Date: 2026-09-01
Dataset: `/sda1/Albert/fusion/20260210/20260210_Ming_Albert_HCC_test_01_Scan1.tiff.ome.tif`
Harness: `scripts/explore_demo.py` `[switch]` diagnostics (commit 7377dae),
sampling the actual scene every 16ms — visibility and blitted keys, not
delivery signals. Sampling cost `monitor_p95` 0.5–1.4ms (single-sample rows
report the cold first scan, 8–10ms, which is not a recurring cost).

Run (manual clicks, human-driven):
```
python scripts/explore_demo.py --path <slide> --channel DAPI \
    --method tophat --param 25 --hot --coverage \
    --corrected-cache-gb 8 --start-view 25606,15360,1400
```
Every row below is `level=0`, `visible_tiles=24`, `wrong=0`, `raw=0`.

## Measured

| switch | class | overview_before | tile_ready | full_precise | blank |
|---|---|---|---|---|---|
| DAPI → CD3 | far | no | no | 242ms | 5 (64ms) |
| CD3 → Podo | far | no | no | 141ms | 4 (49ms) |
| Podo → CD14 | **near** | yes | **yes** | **107ms** | 0 |
| CD14 → TCRD | far | no | no | 199ms | 4 (48ms) |
| TCRD → CD45RA | **near** | yes | **yes** | **114ms** | 0 |
| CD45RA → HsBAg | far | no | no | 246ms | 5 (64ms) |

`[channel] -> … in …ms` on the same runs: **98.4ms** and **103.7ms** for the
two near switches (2.2ms of it after an atomic cached swap on the far ones,
6.7–12.6ms otherwise) — i.e. the near switch is almost entirely the
SYNCHRONOUS cost of `set_selection` on the GUI thread, not I/O.

Follow-up automated run (`--auto-switch CD3,Podo,TCRD
--auto-switch-settle-ms 26000`, same settings, offscreen), which additionally
counts the target channel's cached tiles read-only BEFORE the switch:

| switch | cached before switch | full_precise |
|---|---|---|
| DAPI → CD3 | tophat 24/24, cucim 24/24 | 163ms |
| CD3 → Podo | tophat 24/24, cucim 24/24 | 225ms |
| Podo → TCRD | tophat 24/24, cucim 24/24 | 185ms |

## What this settles

**The overview fetch is the far switch, not the tiles.** In every far row
`overview == first_precise == full_precise` — all three milestones land on
the same 16ms sample — and the run above proves the tiles for both methods
were already cached (24/24) before the switch began. So COVERAGE's work IS
there; display is gated on the overview, because `_blocked_on_overview()`
cancels precise generations until the live channel's overview is installed.
That was inferred from the coincidence in the manual run and is now
measured.

**Decision: COVERAGE does NOT get overview prefetch.** Far switches are
141–246ms — all under the 500ms acceptance line, four of seven under the
200ms target — with no raw flash and no wrong-channel frame anywhere. A
background overview queue would buy roughly 50–140ms in exchange for 57 more
overview records in memory and another concurrent path. Not worth it at this
stage.

**Next optimisation target is the ~100ms synchronous atomic swap**, which
both classes pay and which dominates the near switch. Profiling target:
cache lookup, tile fetch/quantisation, creating or updating 24 ImageItems,
visibility/coverage update, Qt scene/layout. Goal: a cached-channel switch
under 50ms, with no change to the scientific computation or the cache
identity contract.

## Caveat on `tile_ready`

The name misleads. `MultiChannelPrefetchController.is_channel_ready` is
STRICT channel readiness: it requires an installed overview record as well
as the tiles, so a channel COVERAGE has fully prepared still reports False —
`tile_ready=no` in this table never meant the corrected tiles were missing.
A far channel therefore cannot report `tile_ready=yes` by construction, so
the "3 far switches with tile_ready=yes" cell is unreachable without the
overview prefetch this document declines to build. The `[switch]` line
reports `cached=` separately for exactly this reason. Suggested split for a
later revision:

* `overview_ready`
* `tiles_ready`
* `channel_ready = overview_ready and tiles_ready`
