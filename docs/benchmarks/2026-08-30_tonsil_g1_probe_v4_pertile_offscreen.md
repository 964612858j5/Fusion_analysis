# G1-render probe (measured-only)

Dataset: `/sda1/Fusion/benchmark/tonsil/2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`  Channel: `TOX`

## Environment

- hostname: `ubuntu-H12D-8D`
- python: `3.10.20`
- qt_qpa_platform: `offscreen`
- offscreen: `True`
- offscreen_note: `offscreen: excludes real compositor/vsync -- numbers measure frame PREP cost only`

**offscreen: excludes real compositor/vsync — numbers measure frame PREP cost only**

## Frame prep timing (range_handler + request_issue)

- n_range_handler_samples: 43, n_request_issue_samples: 43
- range_handler_ms: p50=0.155 p95=0.320 max=2.151
- request_issue_ms: p50=0.248 p95=1.045 max=1.377
- frame_prep worst-case estimate (p95 range + p95 request_issue, upper bound): 1.365 ms
- over 16.7ms budget: range_handler 0, request_issue 0

## Per-tile item update breakdown (measured-only)

- tile_item_update_ms (quantize + setImage/setRect per delivered tile): p50=1.018 p95=2.498
- window-aggregated (16.7ms buckets, summed cost): p50=3.116 p95=10.667, 0/157 windows over budget (window-aggregation, NOT exact vsync frames)
- items_created: 256, items_pruned: 0

## Fill latencies

- time_to_first_observed_overview_ms: 144.751 ms
- time_to_first_observed_raw_fill_ms p50 (9 samples): 46.725 ms
- time_to_first_observed_precise_fill_ms p50 (9 samples): 70.336 ms

## Viewport-first/full (issue -> first/all visible tiles matching)

- viewport_first_raw_tile_ms p50: 12.612 ms
- viewport_full_raw_tile_ms p50: 17.682 ms
- viewport_first_precise_tile_ms p50: 2.920 ms
- viewport_full_precise_ms p50: 2.546 ms

## Controller stats

- raw_tiles_blitted: 168
- precise_tiles_blitted: 324
- stale_precise_dropped: 9
- mismatched_key_dropped: 0
- mismatched_raw_dropped: 0
- late_raw_rejected: 9
- late_precise_rejected: 39
- items_created: 256
- items_pruned: 0

## Conclusions (probe v4: per-tile fixed-world-coordinate architecture)

| metric | mosaic canvas (v3) | per-tile items (v4) |
|---|---|---|
| range handler p50/p95 | 0.41 / 33 ms | 0.16 / 0.32 ms |
| heavy path p50/p95 | blit 0.12 / 1.6 ms (+realloc spikes) | tile update 1.0 / 2.5 ms |
| window-agg p95 / over budget | 12.8 ms, 5/100 | **10.7 ms, 0/157** |
| raw fill (first observed) p50 | — | 47 ms |
| precise fill (first observed) p50 | — | 70 ms |

Zero over-budget windows offscreen. Root-cause fixes this round (external
review): per-item axisOrder="row-major" (standalone use rendered TRANSPOSED
— the "random tissue blocks"); unrounded per-axis downsample geometry;
namespaced generation tokens (view/settled integers shared one stale-set
and killed each other's requests); GUI delivery guards re-check token +
wanted-set; camera never debounced (only request issuing); level switches
keep coarser tiles under finer ones (no clearing); scheduler shutdown now
DROPS queued work (teardown previously stalled up to 120 s per queued
compute entry staging onto exited raw workers). Real-display verdict still
requires a desktop run + manual acceptance.
