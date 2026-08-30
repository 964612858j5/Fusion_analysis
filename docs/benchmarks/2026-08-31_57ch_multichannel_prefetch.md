# 57-channel PCF benchmark — multi-channel precompute

Data: `/sda1/Albert/fusion/20260210/20260210_Ming_Albert_HCC_test_01_Scan1.tiff.ome.tif`
— 57 channels, level 0 31680x26880, 4 levels at 4x, uint8. Real PCF data, not
synthetic and not the 29-channel tonsil slide.

Viewport: 20 level-0 tiles (5x4 at a 512 grid) over the tissue window chosen by
`explore_view.py::_pick_calibration_windows`, at level-0 (25600, 17920).
Channel index 1 = `CD15`. Machine: RTX 4090 (48 GB), 125 GB RAM.

Raw script output: `2026-08-31_57ch_multichannel_prefetch_raw.json`.
Everything below is **measured** unless marked otherwise.

## READ THIS FIRST — a contamination that inverted several conclusions

The scheduler-path cells in the raw JSON create a fresh `RawTileProvider` per
cell, so every I/O worker thread pays its own first-read cost. Measured
directly:

    main thread, 1st read on a fresh provider : 168.3 ms
    main thread, 2nd read                     :   0.9 ms
    8 fresh threads, 1st read each            : 126, 218, 295, 401, 517,
                                                 648, 748, 865 ms  (wall 883 ms)

Per-thread TIFF handle construction costs ~110-170 ms and is GIL-serialised —
`tifffile` parses the OME-XML and page table in pure Python, and the lock is
inside tifffile, not in `RawTileProvider` (`_registry_lock` only guards a list
append). With `io_workers=8` that is ~880 ms of serial setup before any real
work. `TiffFile` open+series-parse alone is 42.9 ms for the 29-channel tonsil
file and 62.2 ms for this 57-channel one; the rest is building the per-level
zarr store.

Any raw-JSON number where a provider was fresh is inflated by this. The
corrected values are below; where the two disagree, the corrected one stands.

## Corrected: worker sweep (handles pre-warmed, 20 tiles, tophat)

| compute_workers | first tile | full coverage |
|---|---|---|
| 1 | 42.0 ms | 211.9 ms |
| 2 | 23.7 ms | 132.1 ms |
| **4** | 27.8 ms | **110.9 ms** |
| 8 | 36.8 ms | 117.1 ms |

Compute workers DO scale, 1 -> 4 is 1.9x, and 4 is the optimum; 8 is slightly
worse. The raw JSON's flat 916 / 955 / 986 ms across 1/2/4 was entirely the
cold-handle cost swamping a ~200 ms job. The existing `compute_workers=4`
default is confirmed by this, not by the contaminated run.

## Corrected: neighbour channel preparation (pre-warmed, n=10)

| | p50 | p95 | max | target | verdict |
|---|---|---|---|---|---|
| +-1 | 238.3 ms | 323.8 ms | 339.7 ms | 500 ms | **MET** |
| +-2 | 105.2 ms | 151.3 ms | 157.3 ms | 1000 ms | **MET** |

The raw JSON's `+-1 p95 = 921.7 ms NOT MET` was the cold-handle artefact; its
two samples were `[965, 96]` — one channel paying setup, the next not.

## Uncontaminated results from the raw run

| Measurement | Value | Target | Verdict |
|---|---|---|---|
| sequential baseline, tophat, 20 tiles | first 115.1 ms / full 214.5 ms | — | — |
| sequential baseline, cucim, 20 tiles | first 90.8 ms / full 200.5 ms | — | — |
| warm raw cache, full coverage | 38.7 ms (tophat) / 29.8 ms (cucim) | — | — |
| far channel clicked under HOT+8 background | 354.4 ms | 2000 ms | MET |
| background degradation of the visible channel | -3.1% | <= 10% | MET |
| cancellation stop latency | 124.2 ms | — | — |
| shared vs independent raw staging | misses 84 -> 42 (-50%) | — | — |
| explicit per-thread CUDA streams | 11.1 ms vs 7.7 ms shared default | — | slower |
| correctness, every batched/parallel cell vs sequential | max abs diff 0.0 | byte-identical | MET |
| process RSS at end | 1604 MB | — | — |
| GPU mempool at end | 118.9 MB | — | — |

Batch-size sweep aggregates (1/2/4/8 channels) all carry the cold-handle cost
and are not quoted here as steady-state; per-channel first-tile arrivals within
them were 105-334 ms.

## Profiling breakdown, per 20-tile viewport (sequential baseline)

| Stage | tophat | cucim |
|---|---|---|
| TIFF read | 162.1 ms (76%) | 143.7 ms (72%) |
| halo staging | 0.0 ms (assembler hit) | 0.0 ms |
| GPU kernel (transfers not separable) | 36.4 ms (17%) | 39.1 ms (20%) |
| Python scheduling overhead | 15.8 ms (7.4%) | 17.4 ms (8.7%) |

Host-device transfer is not separable: `CorrectionCompute.compute()` reports
`kernel_includes_transfers: True`. Not guessed, not split.

## Rust

Python scheduling overhead is 7.4-8.7% of wall time. Three quarters of the time
is TIFF reading and a further fifth is the GPU kernel. Rewriting the scheduler
in Rust would target under a tenth of the cost and would not change the pixel
volume, the decode work, or the GPU load. There is no profiling evidence here
for a Rust rewrite. The one place a native path could matter is TIFF decode
itself, and that is a library choice, not a scheduler rewrite.

## Open items, honestly not measured

- CUDA streams: explicit per-thread streams measured slower, but at this tile
  size and batch the difference is within run-to-run spread for some cells; the
  script labels its own streams verdict INCONCLUSIVE. Treat "do not use
  explicit streams" as **inferred**, not settled.
- Batch-size sweep needs re-running with pre-warmed handles before any
  conclusion about the best channel batch size.
- Nothing here measures the live viewer; no integration was done.
