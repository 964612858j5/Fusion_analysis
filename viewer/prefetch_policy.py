"""Multi-channel prefetch ordering rules — pure functions, no Qt, no I/O.

The SINGLE source of the two orders the running viewer prefetches in. It
decides only WHAT ORDER to prepare channels in; it never computes a tile,
never touches Qt or numpy, never spawns a thread, and holds no state — no
timer, no generation, no queue, no in-flight accounting. Those belong to
`viewer.multichannel_prefetch.MultiChannelPrefetchController`, which owns
the runtime and calls the functions here THROUGH this module
(`prefetch_rules.hot_order(...)`), never through a bound import, so a test
can patch a rule and observe the runtime's request order follow it.

Contents:

* `hot_order(center, n)` — HOT: the neighbouring channels, i-1, i+1, i-2,
  i+2, clamped at the ends of the list.
* `coverage_order(n, center)` — COVERAGE: every other channel, walked from
  both ends of the list toward the middle (0, N-1, 1, N-2, ...).
* `ChannelCorrectionSpec` — per-channel parameters for both correction
  methods, which a `PrefetchSnapshot` cannot express (it describes the
  current method only).

## What used to be here

An unwired experimental policy layer: a three-state camera machine
(RELOCATING / MOVING / SETTLED with `next_state`), P0–P4 priority tiers, a
deterministic 3:1 HOT:COVERAGE interleave, per-item generations and an
outstanding cap. None of it had a caller outside its own test file, and it
had drifted away from the runtime it was meant to describe: the live
controller implements STRICT HOT PRIORITY (COVERAGE issues only while HOT
has nothing queued and nothing in flight, including its one-at-a-time
overview fetch), integer priority bands rather than an enum, a single
settled flag rather than a state machine, and per-channel batching rather
than an item interleave. Two rule sets, two test suites, one runtime —
so the unused one is gone. It is recoverable from git history if the 3:1
interleave, P1 (movement buffer) or P4 (long-dwell surroundings) ever
become real work.
"""

from dataclasses import dataclass
from typing import List


def hot_order(center: int, n: int) -> List[int]:
    """i-1, i+1, i-2, i+2 ... clamped at both ends, each channel only once.

    Clamping means an out-of-range offset is dropped rather than folded
    back into range -- near either end of the channel list, HOT simply has
    fewer than 4 entries (there is no channel to stand in for the missing
    neighbour; padding with a duplicate would just waste a compute slot on
    a channel already covered).
    """
    order: List[int] = []
    seen = {center}
    for offset in (1, 2):
        for cand in (center - offset, center + offset):
            if 0 <= cand < n and cand not in seen:
                order.append(cand)
                seen.add(cand)
    return order


def coverage_order(n: int, center: int) -> List[int]:
    """0, N-1, 1, N-2, ... skipping `center` (HOT/P0 already own it).

    Walking from both ends toward the middle means coverage reaches the
    slide's extremes first -- on the (common) assumption that a user who
    has settled near the middle of the channel list is more likely to
    scroll outward soon than to have already seen the far ends.
    """
    order: List[int] = []
    lo, hi = 0, n - 1
    take_low = True
    while lo <= hi:
        cand = lo if take_low else hi
        if take_low:
            lo += 1
        else:
            hi -= 1
        if cand != center:
            order.append(cand)
        take_low = not take_low
    return order


@dataclass(frozen=True)
class ChannelCorrectionSpec:
    """Per-channel parameters for the two HOT correction methods.

    ``PrefetchSnapshot`` describes the CURRENT method only and cannot
    express that both tophat and cuCIM are prepared for this channel.  The
    per-channel parameters for BOTH methods therefore have to be supplied
    explicitly by the host rather than inferred.
    """

    channel: str
    tophat_radius: int
    cucim_sigma: int
