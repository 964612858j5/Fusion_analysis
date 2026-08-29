# G1-render probe (measured-only)

Dataset: `/sda1/Fusion/benchmark/tonsil/2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`  Channel: `TOX`

## Environment

- hostname: `ubuntu-H12D-8D`
- python: `3.10.20`
- qt_qpa_platform: `offscreen`
- offscreen: `True`
- offscreen_note: `offscreen: excludes real compositor/vsync -- numbers measure frame PREP cost only`

**offscreen: excludes real compositor/vsync — numbers measure frame PREP cost only**

## Frame prep timing

- n_frames: 40
- p50: 8.924 ms
- p95: 17.374 ms
- max: 18.311 ms
- frames over 16.7ms budget: 4

## Fill latencies

- time-to-first-overview-pixel: 117.039 ms
- raw fill latency p50 (19 samples): 20.253 ms
- precise fill latency p50 (19 samples): 81.796 ms

## Controller stats

- frames_prepared: 40
- raw_tiles_blitted: 230
- precise_tiles_blitted: 924
- stale_precise_dropped: 16
- mismatched_key_dropped: 0

## Conclusions (first G1-render probe, offscreen, measured-only)

1. Frame-prep p50 8.9 ms / p95 17.4 ms / max 18.3 ms; 4 of 40 frames over
   the 16.7 ms budget. VERDICT: near-target, NOT yet a pass — p95 sits at
   the budget line before any real compositor/vsync cost is added. The
   blit/setImage path is the optimization candidate if the real-display
   run confirms overruns.
2. Layer latencies healthy: overview first pixel 117 ms at dataset open;
   raw quick-fill p50 20 ms; precise corrected fill p50 82 ms
   (request→blit, includes staging+GPU). Stale-generation dropping
   observed working during motion (16 drops).
3. G1-render FINAL verdict requires a real-display run:
   `python scripts/g1_render_probe.py --path <wsi> --channel 1`
   (without --offscreen) in a desktop session.
