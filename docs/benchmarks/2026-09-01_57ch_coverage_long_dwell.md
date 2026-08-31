# COVERAGE long dwell — 57 channels, cache budget decides the payoff

Date: 2026-09-01 (re-run of the 2026-08-31 measurement, for the record)
Host: this workstation, cupy 13.3.0 GPU morphology available
Dataset: `/sda1/Albert/fusion/20260210/20260210_Ming_Albert_HCC_test_01_Scan1.tiff.ome.tif`
— 57 channels, OME-TIFF pyramid, real slide (not synthetic)
Harness: `scripts/benchmark_coverage_dwell.py`, run as
`QT_QPA_PLATFORM=offscreen python scripts/benchmark_coverage_dwell.py --corrected-cache-gb 8 --dwell 90`
and again with `--corrected-cache-gb 0.5` (defaults supply path, centre and span)
Setup: camera parked at (y=25606, x=15360), span 1400 px, `method=tophat params=(25,)`,
`MultiChannelPrefetchController(..., coverage=True)`, raw cache 2 GB, HOT defaults
Definition of drained: HOT and COVERAGE queues empty, no physically in-flight
request (a slot is released only by a callback — ordinary, or the opt-in
terminal one for a stale generation — so this is physical, not
generation-based), the batch counter at zero, **and** the whole channel order
consumed. The last condition matters: between two batches the queues are
momentarily empty while channels still wait to be planned.

## What was measured

| | corrected cache 8 GB | corrected cache 512 MB |
|---|---|---|
| COVERAGE batches | 14 | 14 |
| COVERAGE tiles requested / completed / failed | 1728 / 1728 / 0 | 1728 / 1728 / 0 |
| HOT tiles requested / completed | 64 / 64 | 64 / 64 |
| time to fully drain | 21 s | 21 s |
| corrected cache items retained | 1817 | **512** |
| corrected cache bytes | 1.91 GB | 0.54 GB |
| corrected cache evictions | **0** | **1305** |
| RSS peak | 3544 MB | 2281 MB |
| channels reported ready (`is_channel_ready`) | **2 / 57** | **0 / 57** |

(The 2026-08-31 run of the same two arms gave 1728/1728, 1.91 GB, 0 evictions,
RSS 3574 MB, drained 25 s, 2/57 ready for the 8 GB arm and 1305 evictions /
512 items / 0-57 ready for the 512 MB arm. The numbers above reproduce it.)

## Conclusions

1. **COVERAGE completes the whole channel list and drains cleanly.** All 57
   channels' current viewport, both methods, 1728 tiles, zero failures, zero
   cancellations, and no leaked in-flight slot in either arm. The mechanism
   works.
2. **At 512 MB the work is mostly thrown away again.** 1305 of the 1817 tiles
   produced were evicted before anything could use them, and not one channel
   ended up ready. That is why COVERAGE now defaults to **OFF** in the
   controller and in `scripts/explore_demo.py` (opt in with `--coverage`):
   the demo's corrected cache is 512 MB, so enabling it by default would
   spend GPU and I/O on tiles the cache cannot keep.
3. **Even at 8 GB only 2 of 57 channels are "ready".** Not an eviction
   problem — the cache held everything, 0 evictions. `is_channel_ready`
   additionally requires an installed overview record, and COVERAGE
   deliberately does not fetch overviews. So COVERAGE alone prepares tiles,
   not switch-ready channels.
4. **Memory cost is bounded and modest** at this viewport size: peak RSS
   3.5 GB with an 8 GB budget the run never approached (1.91 GB used). The
   budget question is not "does it fit" but "is retention long enough to be
   worth the compute" — see (2).

## Open, deliberately not decided here

Whether COVERAGE should also queue low-priority overview records (which is
what would move the 2/57 number) is **deferred by the user** until far-channel
switch latency has been measured manually. Likewise the production cache
budget: 8 GB is a proposal in `docs/v15_multichannel_settled_prefetch.md`,
not a committed default, and this run does not settle whether it should be
fixed or hardware-adaptive.
