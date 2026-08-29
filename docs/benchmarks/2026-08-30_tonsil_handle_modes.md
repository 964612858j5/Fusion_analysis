# Viewer prototype benchmark (measured-only)

Dataset: `/sda1/Fusion/benchmark/tonsil/2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`

## Environment

- hostname: `ubuntu-H12D-8D`
- python: `3.10.20`
- viewer_module_file: `/sda1/Fusion/analysis_pipline/block01_v14/viewer/__init__.py`
- bg_correction_module_file: `/sda1/Fusion/analysis_pipline/block01_v14/core/bg_correction.py`
- gpu_morph_available_before: `True`
- cupy_version: `13.3.0`
- cuda_runtime_version: `12060`
- gpu_device_name: `NVIDIA GeForce RTX 4090`
- gpu_morph_available_after_run: `True`
- gpu_fallback_occurred_mid_run: `False`
- kernel wall time includes transfers; backend=GPU per env block
- kernel first-touch (init cost): 4.45 ms

## Channel: TOX (index 1)

dtype=uint8 min=0.00 mean=0.68 p99=6.00 max=161.00

Cache budgets: raw=537 MB, corrected=537 MB (512 = provisional default candidate)

## Per-config results

### tile=512 level=0 (downsample=1)

random_seed=67610716 method_order=['tophat_50', 'tophat_25', 'cucim_50', 'cucim_100']

- decoder-cold (NOT OS-cold): first tile io_ms=0.00 (tophat_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_50 | 50 | 50 | 25 | 3524.21 | 0.00/0.00 | 1.67/2.00 | 25 |
| app-cold | tophat_25 | 25 | 25 | 25 | 66.70 | 0.00/0.00 | 1.01/1.03 | 25 |
| app-cold | cucim_50 | 50 | 50 | 25 | 90.81 | 0.00/0.00 | 1.89/2.11 | 25 |
| app-cold | cucim_100 | 100 | 100 | 25 | 211.02 | 0.00/0.00 | 5.52/8.10 | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.43 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.30 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.25 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.25 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.22 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.22 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.22 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.17 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 1.13ms total, 0.05ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=0.58ms p95=1.44ms max=1.44ms
- crossing, new column: n=1 wall p50=853.76ms p95=853.76ms max=853.76ms
- crossing, new row: n=2 wall p50=412.26ms p95=492.37ms max=492.37ms
- new-tile fill (overall): n=3 wall p50=492.37ms p95=853.76ms max=853.76ms

Cache stats: raw={'hits': 2000, 'misses': 140, 'evictions': 0, 'bytes': 18350080, 'items': 70} corrected={'hits': 510, 'misses': 115, 'evictions': 0, 'bytes': 120586240, 'items': 115}
RSS: current=1032556KB peak(ru_maxrss)=2097592KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49535188992, 'total_bytes': 51006472192}

### tile=512 level=1 (downsample=4)

random_seed=815541864 method_order=['cucim_50', 'tophat_25', 'cucim_100', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=0.00 (cucim_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_50 | 50 | 12 | 25 | 3266.62 | 0.00/0.00 | 1.21/1.49 | 25 |
| app-cold | tophat_25 | 25 | 6 | 25 | 51.26 | 0.00/0.00 | 0.97/1.11 | 25 |
| app-cold | cucim_100 | 100 | 25 | 25 | 62.15 | 0.00/0.00 | 0.94/1.04 | 25 |
| app-cold | tophat_50 | 50 | 12 | 25 | 48.67 | 0.00/0.00 | 0.57/0.63 | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 1.39 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 1.44 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 1.24 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 1.21 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 1.22 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 1.21 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 1.23 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 1.21 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 1.23ms total, 0.05ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=1.22ms p95=12.28ms max=12.28ms
- crossing, new column: n=1 wall p50=515.93ms p95=515.93ms max=515.93ms
- crossing, new row: n=2 wall p50=433.66ms p95=513.60ms max=513.60ms
- new-tile fill (overall): n=3 wall p50=513.60ms p95=515.93ms max=515.93ms

Cache stats: raw={'hits': 2000, 'misses': 140, 'evictions': 0, 'bytes': 18350080, 'items': 70} corrected={'hits': 510, 'misses': 115, 'evictions': 0, 'bytes': 120586240, 'items': 115}
RSS: current=1296860KB peak(ru_maxrss)=2097592KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49535188992, 'total_bytes': 51006472192}

## I/O staging sweep (handle_mode x io_workers = 1/2/4)

tile=512 level=0 methods=['tophat_25', 'cucim_50'] (measured-only; fresh provider+caches+scheduler per cell; open_count = provider.open_count, verifies handle reuse)

| handle_mode | io_workers | open_count | cold fill s (method1) | 2nd method ms | pan new-tile fill p50/p95 ms | raw cache hits/misses |
|---|---|---|---|---|---|---|
| per_call | 1 | 71 | 2.51 (tophat_25) | 113.33 (cucim_50) | 342.29/362.53 | 1100/140 |
| per_call | 2 | 71 | 3.13 (tophat_25) | 87.14 (cucim_50) | 395.42/551.62 | 1100/140 |
| per_call | 4 | 71 | 3.44 (tophat_25) | 74.22 (cucim_50) | 539.04/592.57 | 1100/140 |
| per_thread | 1 | 2 | 0.19 (tophat_25) | 75.09 (cucim_50) | 40.71/42.64 | 1100/140 |
| per_thread | 2 | 3 | 0.29 (tophat_25) | 116.63 (cucim_50) | 40.73/41.52 | 1100/140 |
| per_thread | 4 | 5 | 0.46 (tophat_25) | 111.54 (cucim_50) | 30.20/36.44 | 1100/140 |
| shared_lock | 1 | 2 | 0.20 (tophat_25) | 81.76 (cucim_50) | 27.82/31.91 | 1100/140 |
| shared_lock | 2 | 2 | 0.28 (tophat_25) | 92.31 (cucim_50) | 46.86/47.37 | 1100/140 |
| shared_lock | 4 | 2 | 0.30 (tophat_25) | 113.64 (cucim_50) | 45.07/50.02 | 1100/140 |


## Conclusions (handle modes, 2026-08-30, measured-only)

1. **Handle churn was the bottleneck, not disk or decode.** Reusing open
   TiffFile/aszarr handles collapses cold viewport fill from 2.51 s
   (per_call) to 0.19–0.30 s and cold-region pan p50 from ~342 ms to
   28–47 ms — verified by open_count (71 opens per cell in per_call vs 2–5
   with reuse). Caveat: cells share OS page cache warmed by earlier cells,
   but the per_call cells themselves stayed ~2.5–3.4 s on equally warm
   data, so the ~13x gap is handle setup (TIFF structure re-parse), not
   disk.
2. **per_thread vs shared_lock: equivalent at these sizes** (0.19 vs
   0.20 s; pan 41 vs 28 ms at 1 worker). per_thread scales without a
   global lock and is now the provider DEFAULT; per_call stays available
   as the measurement baseline.
3. **More I/O workers still don't help** (cold fill 0.19 s @1 → 0.46 s @4
   under per_thread) — with handles fixed the residual work is
   GIL/decode-bound. io_workers stays a tunable; parallel-I/O digging
   stops here per the agreed decision rule.
4. **Experience targets vs measurements** (512 tiles, L0, this machine):
   cache-hit lookup ≤0.05 ms/tile (frame budget OK, render untested);
   cold-region pan 28–47 ms — under the 100–150 ms progressive-fill
   target and near the 50 ms precise-display target; first full precise
   viewport 0.19–0.30 s, to be masked by coarse/raw progressive display.
   Next work shifts to the render path: pinned coarse pyramid, raw/coarse
   immediate display with flicker-free precise replacement, idle prefetch
   — i.e. Step0 Explore integration (G1-render).
