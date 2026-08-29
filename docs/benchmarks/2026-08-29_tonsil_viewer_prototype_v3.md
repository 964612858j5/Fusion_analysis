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
- kernel first-touch (init cost): 4.31 ms

## Channel: TOX (index 1)

dtype=uint8 min=0.00 mean=0.68 p99=6.00 max=161.00

Cache budgets: raw=537 MB, corrected=537 MB (512 = provisional default candidate)

## Per-config results

### tile=256 level=0 (downsample=1)

random_seed=984544207 method_order=['tophat_50', 'tophat_25', 'cucim_100', 'cucim_50']

- decoder-cold (NOT OS-cold): first tile io_ms=452.51 (tophat_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_50 | 50 | 50 | 81 | 5751.82 | 42.20/123.42 | 0.87/1.36 | 81 |
| app-cold | tophat_25 | 25 | 25 | 81 | 61.00 | 0.00/0.00 | 0.44/0.58 | 81 |
| app-cold | cucim_100 | 100 | 100 | 81 | 2681.15 | 0.00/45.30 | 3.72/5.28 | 81 |
| app-cold | cucim_50 | 50 | 50 | 81 | 116.96 | 0.00/0.00 | 1.03/1.25 | 81 |
| warm | tophat_50 | 50 | 50 | 81 | 2.66 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 25 | 81 | 1.83 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_100 | 100 | 100 | 81 | 1.87 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 50 | 81 | 1.83 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 50 | 81 | 1.77 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 25 | 81 | 1.75 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_100 | 100 | 100 | 81 | 1.76 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 50 | 81 | 8.07 | n/a/n/a | n/a/n/a | 81 |

G1-data-cache: cache-hit lookup (render path untested): 1.74ms total, 0.02ms/tile over 81 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.02ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=1.66ms p95=1.85ms max=1.85ms
- crossing, new column: n=2 wall p50=1.46ms p95=13.79ms max=13.79ms
- crossing, new row: n=1 wall p50=12.10ms p95=12.10ms max=12.10ms
- new-tile fill (overall): n=2 wall p50=12.10ms p95=13.79ms max=13.79ms

Cache stats: raw={'hits': 4196, 'misses': 169, 'evictions': 0, 'bytes': 11075584, 'items': 169} corrected={'hits': 1666, 'misses': 341, 'evictions': 0, 'bytes': 89391104, 'items': 341}
RSS: current=807780KB peak(ru_maxrss)=1254372KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49585717248, 'total_bytes': 51006472192}

### tile=256 level=1 (downsample=4)

random_seed=22019917 method_order=['tophat_25', 'cucim_50', 'cucim_100', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=260.03 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 6 | 81 | 4147.60 | 25.51/107.66 | 0.66/0.79 | 81 |
| app-cold | cucim_50 | 50 | 12 | 81 | 58.63 | 0.00/0.00 | 0.42/0.49 | 81 |
| app-cold | cucim_100 | 100 | 25 | 81 | 70.95 | 0.00/0.00 | 0.53/0.78 | 81 |
| app-cold | tophat_50 | 50 | 12 | 81 | 55.23 | 0.00/0.00 | 0.40/0.44 | 81 |
| warm | tophat_25 | 25 | 6 | 81 | 2.06 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 12 | 81 | 1.80 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_100 | 100 | 25 | 81 | 1.74 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 12 | 81 | 1.74 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 6 | 81 | 1.67 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 12 | 81 | 18.92 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_100 | 100 | 25 | 81 | 1.82 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 12 | 81 | 1.76 | n/a/n/a | n/a/n/a | 81 |

G1-data-cache: cache-hit lookup (render path untested): 1.64ms total, 0.02ms/tile over 81 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.02ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=10 wall p50=1.70ms p95=66.80ms max=66.80ms
- crossing, new column: n=1 wall p50=384.39ms p95=384.39ms max=384.39ms
- crossing, new row: n=1 wall p50=292.80ms p95=292.80ms max=292.80ms
- new-tile fill (overall): n=2 wall p50=292.80ms p95=384.39ms max=384.39ms

Cache stats: raw={'hits': 2935, 'misses': 143, 'evictions': 0, 'bytes': 9371648, 'items': 143} corrected={'hits': 1683, 'misses': 342, 'evictions': 0, 'bytes': 89653248, 'items': 342}
RSS: current=1116540KB peak(ru_maxrss)=1254372KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49585717248, 'total_bytes': 51006472192}

### tile=256 level=2 (downsample=16)

random_seed=601142228 method_order=['cucim_100', 'tophat_25', 'cucim_50', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=100.16 (cucim_100)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_100 | 100 | 6 | 64 | 2440.73 | 25.47/100.16 | 0.66/0.73 | 64 |
| app-cold | tophat_25 | 25 | 2 | 64 | 41.59 | 0.00/0.00 | 0.38/0.41 | 64 |
| app-cold | cucim_50 | 50 | 3 | 64 | 38.16 | 0.00/0.00 | 0.37/0.39 | 64 |
| app-cold | tophat_50 | 50 | 3 | 64 | 44.17 | 0.00/0.00 | 0.40/0.41 | 64 |
| warm | cucim_100 | 100 | 6 | 64 | 3.35 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_25 | 25 | 2 | 64 | 3.05 | n/a/n/a | n/a/n/a | 64 |
| warm | cucim_50 | 50 | 3 | 64 | 2.93 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_50 | 50 | 3 | 64 | 2.94 | n/a/n/a | n/a/n/a | 64 |
| warm | cucim_100 | 100 | 6 | 64 | 2.90 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_25 | 25 | 2 | 64 | 2.87 | n/a/n/a | n/a/n/a | 64 |
| warm | cucim_50 | 50 | 3 | 64 | 3.34 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_50 | 50 | 3 | 64 | 2.90 | n/a/n/a | n/a/n/a | 64 |

G1-data-cache: cache-hit lookup (render path untested): 2.83ms total, 0.04ms/tile over 64 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.04ms (render path untested)

WARNING: pan trajectory fetched ZERO new tiles in every step (n_new_tiles=0 for all steps) — this run measured cache-hit lookups only; it says NOTHING about boundary-crossing / new-tile-fill performance.

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=12 wall p50=2.03ms p95=2.91ms max=2.95ms
- crossing, new column: n=0 wall p50=n/ams p95=n/ams max=n/ams
- crossing, new row: n=0 wall p50=n/ams p95=n/ams max=n/ams
- new-tile fill (overall): n=0 wall p50=n/ams p95=n/ams max=n/ams

Cache stats: raw={'hits': 1872, 'misses': 64, 'evictions': 0, 'bytes': 3533400, 'items': 64} corrected={'hits': 1344, 'misses': 256, 'evictions': 0, 'bytes': 56534400, 'items': 256}
RSS: current=770508KB peak(ru_maxrss)=1312940KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49585717248, 'total_bytes': 51006472192}

### tile=512 level=0 (downsample=1)

random_seed=1011641865 method_order=['tophat_50', 'tophat_25', 'cucim_100', 'cucim_50']

- decoder-cold (NOT OS-cold): first tile io_ms=275.52 (tophat_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_50 | 50 | 50 | 25 | 1762.98 | 44.30/147.44 | 1.46/1.60 | 25 |
| app-cold | tophat_25 | 25 | 25 | 25 | 27.88 | 0.00/0.00 | 0.65/0.95 | 25 |
| app-cold | cucim_100 | 100 | 100 | 25 | 170.63 | 0.00/0.00 | 5.78/6.32 | 25 |
| app-cold | cucim_50 | 50 | 50 | 25 | 65.54 | 0.00/0.00 | 1.67/2.32 | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.36 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.38 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.22 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.20 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.23 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.19 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.21 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.19 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 1.18ms total, 0.05ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=1.16ms p95=1.29ms max=1.29ms
- crossing, new column: n=1 wall p50=317.61ms p95=317.61ms max=317.61ms
- crossing, new row: n=2 wall p50=262.72ms p95=317.58ms max=317.58ms
- new-tile fill (overall): n=3 wall p50=317.58ms p95=317.61ms max=317.61ms

Cache stats: raw={'hits': 965, 'misses': 70, 'evictions': 0, 'bytes': 18350080, 'items': 70} corrected={'hits': 510, 'misses': 115, 'evictions': 0, 'bytes': 120586240, 'items': 115}
RSS: current=953740KB peak(ru_maxrss)=1312940KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49552162816, 'total_bytes': 51006472192}

### tile=512 level=1 (downsample=4)

random_seed=848790584 method_order=['tophat_25', 'cucim_50', 'cucim_100', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=289.93 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 6 | 25 | 1995.46 | 43.35/203.64 | 0.83/0.98 | 25 |
| app-cold | cucim_50 | 50 | 12 | 25 | 23.61 | 0.00/0.00 | 0.52/0.57 | 25 |
| app-cold | cucim_100 | 100 | 25 | 25 | 32.38 | 0.00/0.00 | 0.80/0.92 | 25 |
| app-cold | tophat_50 | 50 | 12 | 25 | 34.61 | 0.00/0.00 | 0.57/0.83 | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 0.67 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 0.58 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 0.56 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 0.53 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 0.54 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 0.52 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 0.51 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 0.52 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 0.50ms total, 0.02ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.02ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=0.97ms p95=1.36ms max=1.36ms
- crossing, new column: n=1 wall p50=285.05ms p95=285.05ms max=285.05ms
- crossing, new row: n=2 wall p50=209.39ms p95=355.40ms max=355.40ms
- new-tile fill (overall): n=3 wall p50=285.05ms p95=355.40ms max=355.40ms

Cache stats: raw={'hits': 965, 'misses': 70, 'evictions': 0, 'bytes': 18350080, 'items': 70} corrected={'hits': 510, 'misses': 115, 'evictions': 0, 'bytes': 120586240, 'items': 115}
RSS: current=1213340KB peak(ru_maxrss)=1312940KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49552162816, 'total_bytes': 51006472192}

### tile=512 level=2 (downsample=16)

random_seed=599852472 method_order=['cucim_100', 'cucim_50', 'tophat_50', 'tophat_25']

- decoder-cold (NOT OS-cold): first tile io_ms=104.22 (cucim_100)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_100 | 100 | 6 | 16 | 592.67 | 25.60/104.22 | 0.84/1.11 | 16 |
| app-cold | cucim_50 | 50 | 3 | 16 | 15.24 | 0.00/0.00 | 0.55/0.77 | 16 |
| app-cold | tophat_50 | 50 | 3 | 16 | 15.91 | 0.00/0.00 | 0.56/0.86 | 16 |
| app-cold | tophat_25 | 25 | 2 | 16 | 13.25 | 0.00/0.00 | 0.50/0.58 | 16 |
| warm | cucim_100 | 100 | 6 | 16 | 0.92 | n/a/n/a | n/a/n/a | 16 |
| warm | cucim_50 | 50 | 3 | 16 | 0.79 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_50 | 50 | 3 | 16 | 0.76 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_25 | 25 | 2 | 16 | 0.75 | n/a/n/a | n/a/n/a | 16 |
| warm | cucim_100 | 100 | 6 | 16 | 0.78 | n/a/n/a | n/a/n/a | 16 |
| warm | cucim_50 | 50 | 3 | 16 | 0.75 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_50 | 50 | 3 | 16 | 0.75 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_25 | 25 | 2 | 16 | 0.74 | n/a/n/a | n/a/n/a | 16 |

G1-data-cache: cache-hit lookup (render path untested): 0.71ms total, 0.04ms/tile over 16 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.04ms (render path untested)

WARNING: pan trajectory fetched ZERO new tiles in every step (n_new_tiles=0 for all steps) — this run measured cache-hit lookups only; it says NOTHING about boundary-crossing / new-tile-fill performance.

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=12 wall p50=0.72ms p95=0.73ms max=0.73ms
- crossing, new column: n=0 wall p50=n/ams p95=n/ams max=n/ams
- crossing, new row: n=0 wall p50=n/ams p95=n/ams max=n/ams
- new-tile fill (overall): n=0 wall p50=n/ams p95=n/ams max=n/ams

Cache stats: raw={'hits': 384, 'misses': 16, 'evictions': 0, 'bytes': 3533400, 'items': 16} corrected={'hits': 336, 'misses': 64, 'evictions': 0, 'bytes': 56534400, 'items': 64}
RSS: current=1230752KB peak(ru_maxrss)=1332832KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49552162816, 'total_bytes': 51006472192}

### tile=1024 level=0 (downsample=1)

random_seed=654889540 method_order=['cucim_100', 'cucim_50', 'tophat_25', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=367.00 (cucim_100)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_100 | 100 | 100 | 9 | 1182.75 | 97.19/181.32 | 9.96/12.09 | 9 |
| app-cold | cucim_50 | 50 | 50 | 9 | 58.07 | 0.00/0.00 | 4.21/5.06 | 9 |
| app-cold | tophat_25 | 25 | 25 | 9 | 42.73 | 0.00/0.00 | 2.23/2.32 | 9 |
| app-cold | tophat_50 | 50 | 50 | 9 | 59.52 | 0.00/0.00 | 3.70/4.30 | 9 |
| warm | cucim_100 | 100 | 100 | 9 | 0.58 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 50 | 9 | 0.48 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 25 | 9 | 0.43 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_50 | 50 | 50 | 9 | 0.44 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 100 | 9 | 0.42 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 50 | 9 | 0.43 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 25 | 9 | 0.42 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_50 | 50 | 50 | 9 | 0.43 | n/a/n/a | n/a/n/a | 9 |

G1-data-cache: cache-hit lookup (render path untested): 0.40ms total, 0.04ms/tile over 9 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.04ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=10 wall p50=0.42ms p95=0.57ms max=0.57ms
- crossing, new column: n=1 wall p50=250.55ms p95=250.55ms max=250.55ms
- crossing, new row: n=1 wall p50=181.89ms p95=181.89ms max=181.89ms
- new-tile fill (overall): n=2 wall p50=181.89ms p95=250.55ms max=250.55ms

Cache stats: raw={'hits': 343, 'misses': 35, 'evictions': 0, 'bytes': 36700160, 'items': 35} corrected={'hits': 183, 'misses': 42, 'evictions': 0, 'bytes': 176160768, 'items': 42}
RSS: current=1439664KB peak(ru_maxrss)=1444352KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49493442560, 'total_bytes': 51006472192}

### tile=1024 level=1 (downsample=4)

random_seed=692470151 method_order=['tophat_50', 'cucim_50', 'tophat_25', 'cucim_100']

- decoder-cold (NOT OS-cold): first tile io_ms=390.45 (tophat_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_50 | 50 | 12 | 9 | 1082.41 | 96.78/137.83 | 1.66/1.70 | 9 |
| app-cold | cucim_50 | 50 | 12 | 9 | 31.74 | 0.00/0.00 | 1.62/2.07 | 9 |
| app-cold | tophat_25 | 25 | 6 | 9 | 34.22 | 0.00/0.00 | 1.37/1.39 | 9 |
| app-cold | cucim_100 | 100 | 25 | 9 | 49.17 | 0.00/0.00 | 2.38/3.80 | 9 |
| warm | tophat_50 | 50 | 12 | 9 | 0.60 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 12 | 9 | 0.45 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 6 | 9 | 0.46 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 25 | 9 | 0.43 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_50 | 50 | 12 | 9 | 0.44 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 12 | 9 | 0.42 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 6 | 9 | 0.44 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 25 | 9 | 0.42 | n/a/n/a | n/a/n/a | 9 |

G1-data-cache: cache-hit lookup (render path untested): 0.42ms total, 0.05ms/tile over 9 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=8 wall p50=0.43ms p95=0.61ms max=0.61ms
- crossing, new column: n=2 wall p50=240.21ms p95=267.30ms max=267.30ms
- crossing, new row: n=2 wall p50=177.21ms p95=226.94ms max=226.94ms
- new-tile fill (overall): n=4 wall p50=240.21ms p95=267.30ms max=267.30ms

Cache stats: raw={'hits': 387, 'misses': 45, 'evictions': 0, 'bytes': 40365504, 'items': 45} corrected={'hits': 177, 'misses': 48, 'evictions': 0, 'bytes': 201326592, 'items': 48}
RSS: current=1628924KB peak(ru_maxrss)=1715592KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49493442560, 'total_bytes': 51006472192}

### tile=1024 level=2 (downsample=16)

random_seed=345255701 method_order=['tophat_25', 'cucim_100', 'cucim_50', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=116.30 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 2 | 4 | 125.11 | 0.00/116.30 | 1.22/1.52 | 4 |
| app-cold | cucim_100 | 100 | 6 | 4 | 8.56 | 0.00/0.00 | 1.22/1.36 | 4 |
| app-cold | cucim_50 | 50 | 3 | 4 | 8.57 | 0.00/0.00 | 1.18/1.34 | 4 |
| app-cold | tophat_50 | 50 | 3 | 4 | 9.11 | 0.00/0.00 | 1.26/1.89 | 4 |
| warm | tophat_25 | 25 | 2 | 4 | 0.34 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_100 | 100 | 6 | 4 | 0.22 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_50 | 50 | 3 | 4 | 0.21 | n/a/n/a | n/a/n/a | 4 |
| warm | tophat_50 | 50 | 3 | 4 | 0.21 | n/a/n/a | n/a/n/a | 4 |
| warm | tophat_25 | 25 | 2 | 4 | 0.22 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_100 | 100 | 6 | 4 | 0.20 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_50 | 50 | 3 | 4 | 0.20 | n/a/n/a | n/a/n/a | 4 |
| warm | tophat_50 | 50 | 3 | 4 | 0.20 | n/a/n/a | n/a/n/a | 4 |

G1-data-cache: cache-hit lookup (render path untested): 0.19ms total, 0.05ms/tile over 4 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

WARNING: pan trajectory fetched ZERO new tiles in every step (n_new_tiles=0 for all steps) — this run measured cache-hit lookups only; it says NOTHING about boundary-crossing / new-tile-fill performance.

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=12 wall p50=0.19ms p95=0.20ms max=0.21ms
- crossing, new column: n=0 wall p50=n/ams p95=n/ams max=n/ams
- crossing, new row: n=0 wall p50=n/ams p95=n/ams max=n/ams
- new-tile fill (overall): n=0 wall p50=n/ams p95=n/ams max=n/ams

Cache stats: raw={'hits': 60, 'misses': 4, 'evictions': 0, 'bytes': 3533400, 'items': 4} corrected={'hits': 84, 'misses': 16, 'evictions': 0, 'bytes': 56534400, 'items': 16}
RSS: current=1628924KB peak(ru_maxrss)=1715592KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49493442560, 'total_bytes': 51006472192}


## Conclusions v3 (pan-coverage fix; supersedes v2's pan section)

Same environment as v2 (RTX 4090, module paths in env block). Adds to v2:

1. **Pan now actually fetches new tiles** (floating unaligned bbox, 12
   quarter-tile steps; per-config the report shows four separate buckets).
   Measured boundary-crossing costs:
   - non-crossing steps: cache-hit only, p50 ~1–2 ms;
   - crossing into a WARM region (raw tiles resident from fill halos):
     new-tile fill ~12–14 ms;
   - crossing into a COLD region (first decode of that area):
     ~260–385 ms per step — this is the number the raw-I/O staging work
     must attack, and the baseline it will be measured against.
   Small pyramid levels that fit entirely in cache emit an explicit
   WARNING instead of claiming boundary performance (3 such configs).
2. **Production gaussian halo aligned** (BG_CORRECTION_ALGO_VERSION "2",
   4*sigma) with production seam regression tests; GPU golden-seam tests
   ran on this machine and passed (cucim GPU vs GPU-whole, tophat GPU
   self-consistency; square-vs-disk structuring element remains a recorded
   pre-existing question).
3. Report/JSON written UTF-8 (v3 rerun after an ascii-locale crash).
