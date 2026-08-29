# G1-render probe (measured-only)

Dataset: `/sda1/Fusion/benchmark/tonsil/2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`  Channel: `TOX`

## Environment

- hostname: `ubuntu-H12D-8D`
- python: `3.10.20`
- qt_qpa_platform: `offscreen`
- offscreen: `True`
- offscreen_note: `offscreen: excludes real compositor/vsync -- numbers measure frame PREP cost only`

**offscreen: excludes real compositor/vsync — numbers measure frame PREP cost only**

## Frame prep timing (range_handler + blit_tick)

- n_range_handler_samples: 43, n_blit_tick_samples: 64
- range_handler_ms: p50=12.968 p95=19.544 max=22.346
- blit_tick_ms: p50=74.967 p95=156.635 max=171.676
- frame_prep worst-case estimate (p95 range + p95 blit, upper bound): 176.178 ms
- over 16.7ms budget: range_handler 7, blit_tick 60 (counted per distribution; blit samples exclude idle ticks)

## Fill latencies

- time_to_first_observed_overview_ms: 144.113 ms
- time_to_first_observed_raw_fill_ms p50 (19 samples): 175.231 ms
- time_to_first_observed_precise_fill_ms p50 (19 samples): 375.865 ms

## Viewport-first/full (issue -> first/all visible tiles matching)

- viewport_first_raw_tile_ms p50: 8.148 ms
- viewport_full_raw_tile_ms p50: 9.048 ms
- viewport_first_precise_tile_ms p50: 2.826 ms
- viewport_full_precise_ms p50: 2.778 ms

## Controller stats

- frames_prepared: 43
- raw_tiles_blitted: 185
- precise_tiles_blitted: 1059
- stale_precise_dropped: 95
- mismatched_key_dropped: 0
- mismatched_raw_dropped: 0

## Conclusions (probe v2, corrected instrumentation — supersedes the first
## G1 probe file, whose timing MISSED the blit/setImage path entirely)

1. **G1-render: FAIL at current implementation** (offscreen frame-prep
   cost only; a real compositor adds more). The first probe's
   "p95 17.4 ms" measured only the range handler. With the blit tick
   instrumented: range_handler p50 13 / p95 19.5 ms; **blit_tick p50 75 /
   p95 157 ms** (60 of 64 working ticks over budget).
2. **The cost is the full-canvas RGBA compose + setImage**: every dirty
   tick rebuilds and re-uploads the ENTIRE mosaic canvas (e.g. 2560² × 4
   float32 ≈ 100 MB) even when one tile changed. Known optimization
   candidates for the next round (to be measured, not promised):
   uint8 RGBA canvases (4× smaller, pyqtgraph fast path), persistent
   canvas with in-place tile writes + partial update, or per-tile
   ImageItems. Only after those fail would a GL path be considered.
3. Transparent-mask layering now verified by tests (unfilled regions
   alpha=0, overview shows through); late-raw identity checks and real
   camera-moving jump_to in place; display levels fixed at load (no
   brightness jumps).
4. Fill latencies (time-to-first-observed): overview 144 ms, raw 175 ms,
   precise 376 ms — inflated vs v1 numbers by the RGBA compose cost in the
   delivery path; expected to drop with the blit optimization.
