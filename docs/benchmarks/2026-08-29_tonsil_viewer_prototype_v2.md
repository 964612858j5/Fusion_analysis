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
- kernel first-touch (init cost): 4.51 ms

## Channel: TOX (index 1)

dtype=uint8 min=0.00 mean=0.68 p99=6.00 max=161.00

Cache budgets: raw=537 MB, corrected=537 MB (512 = provisional default candidate)

## Per-config results

### tile=256 level=0 (downsample=1)

random_seed=942762448 method_order=['cucim_100', 'tophat_50', 'tophat_25', 'cucim_50']

- decoder-cold (NOT OS-cold): first tile io_ms=1637.29 (cucim_100)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_100 | 100 | 100 | 81 | 10814.80 | 60.40/336.64 | 4.01/5.53 | 81 |
| app-cold | tophat_50 | 50 | 50 | 81 | 81.54 | 0.00/0.00 | 0.65/0.73 | 81 |
| app-cold | tophat_25 | 25 | 25 | 81 | 62.87 | 0.00/0.00 | 0.45/0.53 | 81 |
| app-cold | cucim_50 | 50 | 50 | 81 | 124.55 | 0.00/0.00 | 1.00/1.30 | 81 |
| warm | cucim_100 | 100 | 100 | 81 | 4.56 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 50 | 81 | 4.11 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 25 | 81 | 4.13 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 50 | 81 | 4.07 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_100 | 100 | 100 | 81 | 4.05 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 50 | 81 | 3.98 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 25 | 81 | 4.04 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 50 | 81 | 4.41 | n/a/n/a | n/a/n/a | 81 |

G1-data-cache: cache-hit lookup (render path untested): 198.18ms total, 2.45ms/tile over 81 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 2.45ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=1.34ms p95=1.37ms max=1.37ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 4043, 'misses': 169, 'evictions': 0, 'bytes': 11075584, 'items': 169} corrected={'hits': 1241, 'misses': 324, 'evictions': 0, 'bytes': 84934656, 'items': 324}
RSS: current=755252KB peak(ru_maxrss)=1244508KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49592532992, 'total_bytes': 51006472192}

### tile=256 level=1 (downsample=4)

random_seed=912439642 method_order=['cucim_100', 'tophat_25', 'tophat_50', 'cucim_50']

- decoder-cold (NOT OS-cold): first tile io_ms=287.49 (cucim_100)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_100 | 100 | 25 | 81 | 4917.76 | 39.26/121.39 | 0.77/0.93 | 81 |
| app-cold | tophat_25 | 25 | 6 | 81 | 1568.81 | 0.00/0.00 | 0.41/0.48 | 81 |
| app-cold | tophat_50 | 50 | 12 | 81 | 60.02 | 0.00/0.00 | 0.43/0.48 | 81 |
| app-cold | cucim_50 | 50 | 12 | 81 | 785.80 | 0.00/0.00 | 0.43/0.52 | 81 |
| warm | cucim_100 | 100 | 25 | 81 | 3.22 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 6 | 81 | 49.26 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 12 | 81 | 1.82 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 12 | 81 | 1.83 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_100 | 100 | 25 | 81 | 1.75 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_25 | 25 | 6 | 81 | 1.69 | n/a/n/a | n/a/n/a | 81 |
| warm | tophat_50 | 50 | 12 | 81 | 1.69 | n/a/n/a | n/a/n/a | 81 |
| warm | cucim_50 | 50 | 12 | 81 | 1.69 | n/a/n/a | n/a/n/a | 81 |

G1-data-cache: cache-hit lookup (render path untested): 1.67ms total, 0.02ms/tile over 81 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.02ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=1.32ms p95=1.43ms max=1.43ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 2795, 'misses': 121, 'evictions': 0, 'bytes': 7929856, 'items': 121} corrected={'hits': 1241, 'misses': 324, 'evictions': 0, 'bytes': 84934656, 'items': 324}
RSS: current=944060KB peak(ru_maxrss)=1244508KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49592532992, 'total_bytes': 51006472192}

### tile=256 level=2 (downsample=16)

random_seed=888516408 method_order=['cucim_100', 'tophat_50', 'cucim_50', 'tophat_25']

- decoder-cold (NOT OS-cold): first tile io_ms=99.58 (cucim_100)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_100 | 100 | 6 | 64 | 3240.21 | 38.74/82.02 | 0.67/0.75 | 64 |
| app-cold | tophat_50 | 50 | 3 | 64 | 1489.61 | 0.00/0.00 | 0.40/0.51 | 64 |
| app-cold | cucim_50 | 50 | 3 | 64 | 41.25 | 0.00/0.00 | 0.39/0.41 | 64 |
| app-cold | tophat_25 | 25 | 2 | 64 | 45.33 | 0.00/0.00 | 0.40/0.41 | 64 |
| warm | cucim_100 | 100 | 6 | 64 | 3.42 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_50 | 50 | 3 | 64 | 3.21 | n/a/n/a | n/a/n/a | 64 |
| warm | cucim_50 | 50 | 3 | 64 | 3.14 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_25 | 25 | 2 | 64 | 3.18 | n/a/n/a | n/a/n/a | 64 |
| warm | cucim_100 | 100 | 6 | 64 | 3.08 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_50 | 50 | 3 | 64 | 34.10 | n/a/n/a | n/a/n/a | 64 |
| warm | cucim_50 | 50 | 3 | 64 | 1.81 | n/a/n/a | n/a/n/a | 64 |
| warm | tophat_25 | 25 | 2 | 64 | 1.50 | n/a/n/a | n/a/n/a | 64 |

G1-data-cache: cache-hit lookup (render path untested): 1.31ms total, 0.02ms/tile over 64 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.02ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=1.33ms p95=1.73ms max=1.73ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 1872, 'misses': 64, 'evictions': 0, 'bytes': 3533400, 'items': 64} corrected={'hits': 1065, 'misses': 256, 'evictions': 0, 'bytes': 56534400, 'items': 256}
RSS: current=1067996KB peak(ru_maxrss)=1244508KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49592532992, 'total_bytes': 51006472192}

### tile=512 level=0 (downsample=1)

random_seed=47028090 method_order=['tophat_25', 'cucim_50', 'cucim_100', 'tophat_50']

- decoder-cold (NOT OS-cold): first tile io_ms=325.16 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 25 | 25 | 2253.57 | 48.38/181.58 | 0.96/1.84 | 25 |
| app-cold | cucim_50 | 50 | 50 | 25 | 150.27 | 0.00/0.00 | 1.73/2.02 | 25 |
| app-cold | cucim_100 | 100 | 100 | 25 | 183.86 | 0.00/0.00 | 5.35/6.15 | 25 |
| app-cold | tophat_50 | 50 | 50 | 25 | 50.73 | 0.00/0.00 | 1.14/1.36 | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.41 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.39 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.17 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.16 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.19 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.14 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.14 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 1.11ms total, 0.04ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.04ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=0.72ms p95=0.77ms max=0.77ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 851, 'misses': 49, 'evictions': 0, 'bytes': 12845056, 'items': 49} corrected={'hits': 353, 'misses': 100, 'evictions': 0, 'bytes': 104857600, 'items': 100}
RSS: current=1248948KB peak(ru_maxrss)=1329544KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49558978560, 'total_bytes': 51006472192}

### tile=512 level=1 (downsample=4)

random_seed=60656877 method_order=['tophat_25', 'tophat_50', 'cucim_50', 'cucim_100']

- decoder-cold (NOT OS-cold): first tile io_ms=328.96 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 6 | 25 | 2149.40 | 43.86/192.61 | 0.85/1.53 | 25 |
| app-cold | tophat_50 | 50 | 12 | 25 | 28.08 | 0.00/0.00 | 0.53/0.55 | 25 |
| app-cold | cucim_50 | 50 | 12 | 25 | 38.53 | 0.00/0.00 | 0.54/0.63 | 25 |
| app-cold | cucim_100 | 100 | 25 | 25 | 53.77 | 0.00/0.00 | 0.89/1.21 | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 1.39 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 1.21 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 1.18 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 1.16 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 1.17 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 1.14ms total, 0.05ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=0.71ms p95=0.90ms max=0.90ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 851, 'misses': 49, 'evictions': 0, 'bytes': 12845056, 'items': 49} corrected={'hits': 353, 'misses': 100, 'evictions': 0, 'bytes': 104857600, 'items': 100}
RSS: current=1430516KB peak(ru_maxrss)=1520872KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49558978560, 'total_bytes': 51006472192}

### tile=512 level=2 (downsample=16)

random_seed=380078163 method_order=['cucim_50', 'tophat_50', 'cucim_100', 'tophat_25']

- decoder-cold (NOT OS-cold): first tile io_ms=117.96 (cucim_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_50 | 50 | 3 | 16 | 630.50 | 26.53/117.96 | 0.83/1.04 | 16 |
| app-cold | tophat_50 | 50 | 3 | 16 | 13.85 | 0.00/0.00 | 0.51/0.69 | 16 |
| app-cold | cucim_100 | 100 | 6 | 16 | 15.90 | 0.00/0.00 | 0.54/0.70 | 16 |
| app-cold | tophat_25 | 25 | 2 | 16 | 24.77 | 0.00/0.00 | 0.83/1.32 | 16 |
| warm | cucim_50 | 50 | 3 | 16 | 0.98 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_50 | 50 | 3 | 16 | 0.82 | n/a/n/a | n/a/n/a | 16 |
| warm | cucim_100 | 100 | 6 | 16 | 0.80 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_25 | 25 | 2 | 16 | 0.79 | n/a/n/a | n/a/n/a | 16 |
| warm | cucim_50 | 50 | 3 | 16 | 0.78 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_50 | 50 | 3 | 16 | 0.78 | n/a/n/a | n/a/n/a | 16 |
| warm | cucim_100 | 100 | 6 | 16 | 0.78 | n/a/n/a | n/a/n/a | 16 |
| warm | tophat_25 | 25 | 2 | 16 | 0.78 | n/a/n/a | n/a/n/a | 16 |

G1-data-cache: cache-hit lookup (render path untested): 0.74ms total, 0.05ms/tile over 16 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=0.75ms p95=0.77ms max=0.77ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 384, 'misses': 16, 'evictions': 0, 'bytes': 3533400, 'items': 16} corrected={'hits': 261, 'misses': 64, 'evictions': 0, 'bytes': 56534400, 'items': 64}
RSS: current=1488784KB peak(ru_maxrss)=1520872KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49558978560, 'total_bytes': 51006472192}

### tile=1024 level=0 (downsample=1)

random_seed=138811799 method_order=['cucim_50', 'tophat_50', 'cucim_100', 'tophat_25']

- decoder-cold (NOT OS-cold): first tile io_ms=707.16 (cucim_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_50 | 50 | 50 | 9 | 1548.98 | 116.34/160.80 | 4.24/5.06 | 9 |
| app-cold | tophat_50 | 50 | 50 | 9 | 49.01 | 0.00/0.00 | 3.65/3.69 | 9 |
| app-cold | cucim_100 | 100 | 100 | 9 | 154.59 | 0.00/0.00 | 11.25/14.27 | 9 |
| app-cold | tophat_25 | 25 | 25 | 9 | 40.75 | 0.00/0.00 | 2.24/2.44 | 9 |
| warm | cucim_50 | 50 | 50 | 9 | 0.60 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_50 | 50 | 50 | 9 | 0.48 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 100 | 9 | 0.44 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 25 | 9 | 0.44 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 50 | 9 | 0.42 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_50 | 50 | 50 | 9 | 0.43 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 100 | 9 | 0.42 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 25 | 9 | 0.43 | n/a/n/a | n/a/n/a | 9 |

G1-data-cache: cache-hit lookup (render path untested): 0.40ms total, 0.04ms/tile over 9 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.04ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=0.19ms p95=0.20ms max=0.20ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 299, 'misses': 25, 'evictions': 0, 'bytes': 26214400, 'items': 25} corrected={'hits': 113, 'misses': 36, 'evictions': 0, 'bytes': 150994944, 'items': 36}
RSS: current=924376KB peak(ru_maxrss)=1521036KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49466703872, 'total_bytes': 51006472192}

### tile=1024 level=1 (downsample=4)

random_seed=896521416 method_order=['tophat_50', 'cucim_50', 'tophat_25', 'cucim_100']

- decoder-cold (NOT OS-cold): first tile io_ms=345.10 (tophat_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_50 | 50 | 12 | 9 | 1047.03 | 95.50/187.59 | 1.61/1.62 | 9 |
| app-cold | cucim_50 | 50 | 12 | 9 | 28.50 | 0.00/0.00 | 1.55/1.62 | 9 |
| app-cold | tophat_25 | 25 | 6 | 9 | 34.64 | 0.00/0.00 | 1.34/1.36 | 9 |
| app-cold | cucim_100 | 100 | 25 | 9 | 44.87 | 0.00/0.00 | 2.27/2.96 | 9 |
| warm | tophat_50 | 50 | 12 | 9 | 0.61 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 12 | 9 | 0.53 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 6 | 9 | 0.50 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 25 | 9 | 0.47 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_50 | 50 | 12 | 9 | 0.63 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_50 | 50 | 12 | 9 | 0.47 | n/a/n/a | n/a/n/a | 9 |
| warm | tophat_25 | 25 | 6 | 9 | 0.47 | n/a/n/a | n/a/n/a | 9 |
| warm | cucim_100 | 100 | 25 | 9 | 0.49 | n/a/n/a | n/a/n/a | 9 |

G1-data-cache: cache-hit lookup (render path untested): 0.45ms total, 0.05ms/tile over 9 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=0.20ms p95=0.24ms max=0.24ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 299, 'misses': 25, 'evictions': 0, 'bytes': 26214400, 'items': 25} corrected={'hits': 113, 'misses': 36, 'evictions': 0, 'bytes': 150994944, 'items': 36}
RSS: current=1028524KB peak(ru_maxrss)=1521036KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49466703872, 'total_bytes': 51006472192}

### tile=1024 level=2 (downsample=16)

random_seed=668826868 method_order=['tophat_25', 'tophat_50', 'cucim_100', 'cucim_50']

- decoder-cold (NOT OS-cold): first tile io_ms=246.82 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 2 | 4 | 257.74 | 0.00/246.82 | 1.48/1.93 | 4 |
| app-cold | tophat_50 | 50 | 3 | 4 | 9.13 | 0.00/0.00 | 1.24/1.65 | 4 |
| app-cold | cucim_100 | 100 | 6 | 4 | 8.71 | 0.00/0.00 | 1.18/1.37 | 4 |
| app-cold | cucim_50 | 50 | 3 | 4 | 8.48 | 0.00/0.00 | 1.23/1.24 | 4 |
| warm | tophat_25 | 25 | 2 | 4 | 0.31 | n/a/n/a | n/a/n/a | 4 |
| warm | tophat_50 | 50 | 3 | 4 | 0.25 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_100 | 100 | 6 | 4 | 0.22 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_50 | 50 | 3 | 4 | 0.21 | n/a/n/a | n/a/n/a | 4 |
| warm | tophat_25 | 25 | 2 | 4 | 0.21 | n/a/n/a | n/a/n/a | 4 |
| warm | tophat_50 | 50 | 3 | 4 | 0.23 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_100 | 100 | 6 | 4 | 0.21 | n/a/n/a | n/a/n/a | 4 |
| warm | cucim_50 | 50 | 3 | 4 | 0.21 | n/a/n/a | n/a/n/a | 4 |

G1-data-cache: cache-hit lookup (render path untested): 0.20ms total, 0.05ms/tile over 4 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.05ms (render path untested)

Pan (8-step, quarter-tile, alternating x/y): p50=0.20ms p95=0.21ms max=0.21ms, boundary-crossing=2, non-crossing=6

Cache stats: raw={'hits': 60, 'misses': 4, 'evictions': 0, 'bytes': 3533400, 'items': 4} corrected={'hits': 63, 'misses': 16, 'evictions': 0, 'bytes': 56534400, 'items': 16}
RSS: current=928208KB peak(ru_maxrss)=1521036KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49466703872, 'total_bytes': 51006472192}


## Conclusions v2 (revised methodology; supersedes the v1 file's conclusions)

Measured on RTX 4090 / CuPy 13.3 / CUDA 12.6, channel TOX (uint8), module
paths in the env block prove this worktree's code ran. All numbers
measured-only on this machine/dataset.

1. **Raw-tile assembly pays off (Compare implication).** Within a config,
   only the FIRST method (randomized order) pays the raw I/O
   (~2.3 s cold for 25×512² tiles, serialized); the other three methods
   reuse the shared raw cache and fill the whole viewport in 50–184 ms
   including kernels. Compare mode's second unique correction is therefore
   near-free once raw tiles are resident.
2. **Kernels remain milliseconds**: 0.45–6.2 ms/tile incl. transfers,
   backend confirmed GPU (no fallback mid-run), first-touch init 4.5 ms.
3. **G1-data-cache: ≤0.05 ms/tile cache-hit lookup holds for the 512/1024
   candidate configs.** The 256/L0 config is an outlier: ~2.45 ms/tile
   (198 ms / 81 tiles) — anomaly noted, unexplained (not yet root-caused;
   do not extrapolate it to other configs). **G1-render: UNTESTED** (no
   renderer in the prototype).
4. **Pan trajectory measured cache-hit only in this run** (n_new_tiles=0
   for every step in the tables above — a coverage bug in this run's pan
   test, fixed in the next run). Do NOT claim boundary-crossing performance
   from this file's pan tables; that number does not exist yet.
5. **Interactive quality now scales params per level** (e.g. base 25 →
   effective 6 at downsample 4; base 25 → effective 2 at downsample 16, for
   the base-25 tiles shown above) and records base+effective.
6. **CPU golden seam passed; GPU seam parity pending.** The CPU tiled path
   pins stitched == whole-image within 1e-4 for both methods (gaussian halo
   corrected to 4σ; tophat stays 2r). GPU seam parity was not measured in
   this run. Pre-existing open question: GPU tophat (cucim/cupyx morphology)
   uses a SQUARE structuring element vs. the CPU disk-based tophat's
   circular one — GPU tophat is therefore not expected to numerically match
   CPU tophat; GPU parity testing (when run) checks GPU-tiled ==
   GPU-whole-image self-consistency for tophat, not GPU-vs-CPU parity.
7. **512 remains the provisional default candidate** (not frozen).
8. **Memory**: RSS peak ~1.5 GB under 512+512 MB budgets, zero evictions in
   these configs; raw cache now native dtype (uint8). GPU pool peak sampled
   as 0 (pool freed between samples — sampling limitation, not proof of
   zero); device free/total shows ample headroom.
9. **Remaining bottleneck**: the first-method cold fill is still serialized
   (assembler I/O runs inside the single compute worker, io p50 ~48 ms ×
   25 tiles ≈ 2.2 s wall). Next candidate optimization — prefetching raw
   tiles through the I/O pool before compute — must be MEASURED before any
   speedup is claimed.
10. **Production gaussian halo now aligned to 4·sigma**
    (`BG_CORRECTION_ALGO_VERSION = "2"`). Saved outputs produced under
    algorithm version 1 (halo = 2·sigma) may differ from version-2 outputs
    near internal tile borders for the gaussian ("cucim") method; tophat is
    unaffected (halo unchanged at 2·radius).
