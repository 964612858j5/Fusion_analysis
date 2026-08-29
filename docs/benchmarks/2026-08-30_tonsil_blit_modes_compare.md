# G1-render blit-mode comparison (measured-only)

Dataset: `/sda1/Fusion/benchmark/tonsil/2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`  Channel: `TOX`

Provider/scheduler/compute stack reused across modes: caches are warm equally for every mode from mode 2 onward (not a cold-cache comparison).

| mode | blit_tick p50/p95 (ms) | set_image p50/p95 (ms) | tile_convert p50/p95 (ms) | window-agg p95 (ms) | windows over budget | rgba_canvas_allocs |
|---|---|---|---|---|---|---|
| float_full | 76.098/154.663 | n/a/n/a | n/a/n/a | 153.192 | 68/106 | 0 |
| uint8_full | 57.075/116.690 | 0.158/1.282 | 46.292/59.578 | 138.702 | 82/97 | 64 |
| uint8_incremental | 0.192/1.730 | 0.123/0.980 | 1.128/2.329 | 137.411 | 32/86 | 64 |

(measured-only; window-agg is 16.7ms-bucket-summed cost, not exact vsync frames.)
