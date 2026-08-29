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

### tile=512 level=0 (downsample=1)

random_seed=899432935 method_order=['cucim_50', 'tophat_25', 'tophat_50', 'cucim_100']

- decoder-cold (NOT OS-cold): first tile io_ms=0.00 (cucim_50)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | cucim_50 | 50 | 50 | 25 | 3788.02 | 0.00/0.00 | 2.28/2.40 | 25 |
| app-cold | tophat_25 | 25 | 25 | 25 | 66.57 | 0.00/0.00 | 1.07/1.18 | 25 |
| app-cold | tophat_50 | 50 | 50 | 25 | 76.61 | 0.00/0.00 | 1.36/1.61 | 25 |
| app-cold | cucim_100 | 100 | 100 | 25 | 203.31 | 0.00/0.00 | 5.38/6.69 | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.35 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.43 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.15 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 50 | 25 | 1.14 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 25 | 25 | 1.11 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 50 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 100 | 25 | 1.13 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 1.09ms total, 0.04ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.04ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=1.12ms p95=1.52ms max=1.52ms
- crossing, new column: n=1 wall p50=784.98ms p95=784.98ms max=784.98ms
- crossing, new row: n=2 wall p50=444.64ms p95=530.68ms max=530.68ms
- new-tile fill (overall): n=3 wall p50=530.68ms p95=784.98ms max=784.98ms

Cache stats: raw={'hits': 2000, 'misses': 140, 'evictions': 0, 'bytes': 18350080, 'items': 70} corrected={'hits': 510, 'misses': 115, 'evictions': 0, 'bytes': 120586240, 'items': 115}
RSS: current=1110340KB peak(ru_maxrss)=2142124KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49537155072, 'total_bytes': 51006472192}

### tile=512 level=1 (downsample=4)

random_seed=860926554 method_order=['tophat_25', 'tophat_50', 'cucim_50', 'cucim_100']

- decoder-cold (NOT OS-cold): first tile io_ms=0.00 (tophat_25)

| phase | method | base | eff | tiles | wall ms | io p50/p90 | kernel p50/p90 | n |
|---|---|---|---|---|---|---|---|---|
| app-cold | tophat_25 | 25 | 6 | 25 | 3615.94 | 0.00/0.00 | 1.13/1.48 | 25 |
| app-cold | tophat_50 | 50 | 12 | 25 | 72.89 | 0.00/0.00 | 0.81/1.08 | 25 |
| app-cold | cucim_50 | 50 | 12 | 25 | 50.52 | 0.00/0.00 | 0.61/0.95 | 25 |
| app-cold | cucim_100 | 100 | 25 | 25 | 57.62 | 0.00/0.00 | 0.89/1.03 | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 0.77 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 0.76 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 0.55 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 0.67 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_25 | 25 | 6 | 25 | 0.53 | n/a/n/a | n/a/n/a | 25 |
| warm | tophat_50 | 50 | 12 | 25 | 0.52 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_50 | 50 | 12 | 25 | 0.53 | n/a/n/a | n/a/n/a | 25 |
| warm | cucim_100 | 100 | 25 | 25 | 0.53 | n/a/n/a | n/a/n/a | 25 |

G1-data-cache: cache-hit lookup (render path untested): 0.50ms total, 0.02ms/tile over 25 tiles (measured-only)

G1-data-cache: cache-hit lookup <= 0.02ms (render path untested)

Pan (12-step, quarter-tile, alternating x/y, floating bbox):

- non-crossing (cache-hit): n=9 wall p50=1.20ms p95=12.72ms max=12.72ms
- crossing, new column: n=1 wall p50=512.64ms p95=512.64ms max=512.64ms
- crossing, new row: n=2 wall p50=587.36ms p95=608.88ms max=608.88ms
- new-tile fill (overall): n=3 wall p50=587.36ms p95=608.88ms max=608.88ms

Cache stats: raw={'hits': 2000, 'misses': 140, 'evictions': 0, 'bytes': 18350080, 'items': 70} corrected={'hits': 510, 'misses': 115, 'evictions': 0, 'bytes': 120586240, 'items': 115}
RSS: current=1535100KB peak(ru_maxrss)=2142124KB
GPU mem pool peak: {'app-cold': 0, 'warm': 0}
GPU device mem_info (free/total): {'free_bytes': 49537155072, 'total_bytes': 51006472192}

## I/O staging sweep (io_workers = 1/2/4/8)

tile=512 level=0 methods=['tophat_25', 'cucim_50'] (measured-only; fresh provider+caches+scheduler per io_workers value)

| io_workers | cold fill s (method1) | 2nd method ms | pan new-tile fill p50/p95 ms | raw cache hits/misses |
|---|---|---|---|---|
| 1 | 2.48 (tophat_25) | 119.65 (cucim_50) | 353.32/355.82 | 1100/140 |
| 2 | 2.97 (tophat_25) | 185.50 (cucim_50) | 380.82/523.88 | 1100/140 |
| 4 | 3.47 (tophat_25) | 80.93 (cucim_50) | 567.45/582.92 | 1100/140 |
| 8 | 3.84 (tophat_25) | 105.31 (cucim_50) | 535.01/614.75 | 1100/140 |


## Conclusions (I/O staging sweep, 2026-08-30, measured-only)

1. **Staging works mechanically** (single-flight shared with external raw
   requests; assembler 100% cache hits after staging; 37 unit tests) — but
   **parallel I/O made things SLOWER on this stack**: cold fill 2.48 s at
   io_workers=1 rising monotonically to 3.84 s at 8; cold-region pan fill
   p50 353 ms → 535–567 ms. Note the sweep ran 1→8 in order, so later
   (slower) configs even benefited from a warmer OS page cache — the
   contention effect is understated, not an ordering artifact.
2. **Root cause hypothesis (unverified)**: every read_tile opens its own
   TiffFile + aszarr store; concurrent opens/seeks/decodes on one file
   contend (handle setup, GIL-bound decode). The parallelism lever is NOT
   more threads over per-call handles.
3. **Next candidate (must be measured)**: persistent per-thread (or
   locked-shared) TiffFile/zarr store in RawTileProvider so staging threads
   reuse open handles; alternatively evaluate OME-Zarr source layout.
   Until then, io_workers=1 is the best measured setting for this access
   pattern; the scheduler default stays 4 pending the handle fix + re-sweep.
4. Second-method fill (raw cache shared) remains cheap at every setting
   (81–186 ms) — the raw-tile-assembly sharing conclusion from v2/v3 holds.
