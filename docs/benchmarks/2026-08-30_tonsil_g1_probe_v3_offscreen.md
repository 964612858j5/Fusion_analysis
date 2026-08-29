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

- n_range_handler_samples: 43, n_blit_tick_samples: 61
- range_handler_ms: p50=0.414 p95=33.419 max=51.195
- blit_tick_ms: p50=0.118 p95=1.621 max=2.402
- frame_prep worst-case estimate (p95 range + p95 blit, upper bound): 35.041 ms
- over 16.7ms budget: range_handler 5, blit_tick 0 (counted per distribution; blit samples exclude idle ticks)

## Blit-path breakdown (measured-only)

- blit_mode: `uint8_incremental`
- tile_convert_ms: p50=1.228 p95=2.366
- set_image_ms: p50=0.086 p95=0.831
- window-aggregated (16.7ms buckets, summed cost): p50=0.146 p95=12.830, 5/100 windows over budget (window-aggregation, NOT exact vsync frames)
- rgba_canvas_allocs: 18 (steady-state should be ~0 for uint8_incremental)

## Fill latencies

- time_to_first_observed_overview_ms: 146.694 ms
- time_to_first_observed_raw_fill_ms p50 (19 samples): 26.425 ms
- time_to_first_observed_precise_fill_ms p50 (19 samples): 309.423 ms

## Viewport-first/full (issue -> first/all visible tiles matching)

- viewport_first_raw_tile_ms p50: 12.096 ms
- viewport_full_raw_tile_ms p50: 22.621 ms
- viewport_first_precise_tile_ms p50: 4.454 ms
- viewport_full_precise_ms p50: 95.381 ms

## Controller stats

- frames_prepared: 43
- raw_tiles_blitted: 155
- precise_tiles_blitted: 1131
- stale_precise_dropped: 64
- mismatched_key_dropped: 0
- mismatched_raw_dropped: 0
- rgba_canvas_allocs: 18

## Conclusions (probe v3: uint8 incremental + canvas margin/hysteresis)

Progression on the same scripted sequence (offscreen frame-prep cost only):

| stage | range_handler p50/p95 | blit_tick p50/p95 | window-agg p95 | windows over budget |
|---|---|---|---|---|
| float_full (v2 baseline) | 13 / 19.5 | 75 / 157 | 153 (68/106) | 68 |
| uint8 incremental, no margin | 109 / 144 | 0.18 / 2.8 | 138 | 32 |
| + canvas margin/hysteresis | **0.41 / 33** | **0.12 / 1.6** | **12.8** | **5/100** |

1. **Offscreen frame-prep now under budget at p95** (12.8 ms window-agg;
   p50 0.15 ms). The 5 remaining over-budget windows coincide with the
   discrete realloc events (18 allocs across the whole run = zoom level
   switches + 3 jumps — the pinned overview covers those visually).
2. Chain of causes, each exposed by measuring the previous fix: full
   float RGBA recompose (155 ms) → uint8 conversion cost inside the tick
   (117 ms) → per-pan canvas realloc migrated into the range handler
   (109 ms) → margin+hysteresis leaves only jump/level-switch reallocs.
3. uint8 stays display-only (quantized via fixed levels); floats/native
   dtype preserved through the compute path; realloc carries rgba bytes
   without requantizing.
4. **Still pending for the final G1-render verdict: a real-display run**
   (`python scripts/g1_render_probe.py --path <wsi> --channel 1`) —
   offscreen excludes the compositor/vsync.
