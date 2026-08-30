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

---

# Clean rerun (handles pre-warmed, seeded random channel order)

Raw: `2026-08-31_57ch_clean_run_raw.json`, seed 20260831, same 20-tile
viewport and file. Every timed region starts with every I/O worker's TIFF
handle already built; warm-up wall time is reported separately and never
folded into a result. OS page cache state is stated per cell; only this
file's pages are evicted (`posix_fadvise DONTNEED`), never the machine's.

## Worker counts — the static-viewport sweep is NOT the answer

| io_workers (compute=4) | full coverage, static 20-tile viewport | handle warm-up |
|---|---|---|
| 1 | 96.1 ms | 76.9 ms |
| 2 | 133.1 ms | 176.4 ms |
| 4 | 94.9 ms | 393.5 ms |
| 8 | 101.1 ms | 785.3 ms |

Read on its own this says I/O workers do not matter, and it contradicts an
earlier live-viewer measurement where `io=1` was far worse. Both are real;
they measure different regimes. A static viewport whose pages are already
in the OS cache does not stress I/O at all, so the four configurations
above are within noise of each other (note 2 > 1 and 2 > 4, which is not a
monotonic signal).

Re-measured under the regime that actually matters — a 25-step drag,
sampling the fraction of the viewport already sharp at every step:

| io_workers (compute=4) | in-motion coverage |
|---|---|
| 1 | **46.0%** |
| 2 | **98.2%** |
| 4 | 98.4% |
| 8 | 98.4% |

So `io=1` is genuinely bad in motion, and **`io=2` already saturates**; 4
and 8 add nothing measurable. The current default of 8 buys no throughput
and costs 785 ms of GIL-serialised handle construction at startup against
176 ms for 2. **inferred**: 2–4 is the right range, and the choice between
them should be margin, not throughput.

| compute_workers (at the winning io) | full coverage |
|---|---|
| 1 | 160.1 ms |
| 2 | 107.3 ms |
| 4 | **98.4 ms** |
| 8 | 102.8 ms |

4 confirmed, consistent with the earlier warm re-measure (211.9 / 132.1 /
110.9 / 117.1).

## Channel batch size (10 reps, seeded random channels)

| batch | true wall p50 | wall p95 | aggregate service p50 | wall per channel (p50) |
|---|---|---|---|---|
| 1 | 195.6 ms | 323.5 ms | 63.8 ms | 195.6 ms |
| 2 | 270.8 ms | 652.7 ms | 115.3 ms | 135.4 ms |
| 4 | 554.5 ms | 716.0 ms | 189.6 ms | 138.6 ms |
| 8 | 829.9 ms | 1270.9 ms | 475.8 ms | **103.7 ms** |

Wall and aggregate service time are reported separately and never divided
into each other: summing per-task service across parallel workers inflates
the total. The trade is throughput against latency — batch 8 is the most
efficient per channel, batch 1 gets any single channel soonest. There is no
single "best"; it depends on whether the queue is serving HOT (latency) or
COVERAGE (throughput).

## Neighbours — the earlier asymmetry was an artefact

| | p95 | target | verdict |
|---|---|---|---|
| +-1 | 102.1 ms | 500 ms | MET |
| +-2 | 110.5 ms | 1000 ms | MET |

With randomised channel order and n=20 per cell, +-1 and +-2 are the same,
as they should be. The earlier `+-2 p50 105 ms` against `+-1 p50 238 ms`
was measurement order, not a property of the data. Do not quote the old
numbers.

## Other cells

| Measurement | Value | Target | Verdict |
|---|---|---|---|
| far channel clicked under load | 110.3 ms | 2000 ms | MET |
| background degradation of visible channel | -1.6% | <= 10% | MET |
| cancellation stop latency | 9.9 ms | — | — |
| shared vs independent raw staging | misses 84 -> 42 (-50%) | — | — |
| explicit CUDA streams vs shared default | 12.2 vs 7.0 ms | — | slower, but ~5 ms is near run-to-run spread; treat as **inferred** |
| cache: OS cold / OS warm+app cold / all warm | 184.8 / 108.4 / 0.9 ms | — | — |

## Peak memory — measured properly

The script's own peak-memory cell queued 180 tiles (~189 MB) and does not
exercise an 8 GB budget. Measured separately at the real working set, one
20-tile viewport across all 57 channels:

| Working set | corrected cache | peak process RSS | time |
|---|---|---|---|
| 57 ch x 1 method (1140 tiles) | 1.20 GB | 2242 MB | 6.3 s |
| 57 ch x 2 methods (2280 tiles) | 2.39 GB | 3406 MB | +2.3 s |

Zero evictions in both. The cache bytes match the design document's
predicted 1.20 / 2.39 GB exactly. Process RSS runs about 1 GB above the
corrected cache (raw cache, numpy, cupy). **inferred**: an 8 GB corrected
budget carries the full two-method 57-channel viewport with room for the
~2x precompute area and the level+1 fallback tiles; it is not tight.

Also **measured**, and useful for the COVERAGE queue: preparing every
channel's current viewport for both methods took 8.6 s in total.
