"""Multi-channel prefetch decision policy — pure rules, no Qt, no I/O.

This module is a POLICY LAYER ONLY. It decides WHAT to compute next and in
WHAT ORDER for a multi-channel (50+), movement-driven precompute scheduler.
It never computes a tile, never touches Qt, never touches numpy image data,
and never spawns a thread -- it is pure Python (dataclasses + typing) so it
can be unit-tested in isolation from `TileScheduler` and `ExploreView`, and
it imports nothing from `viewer.explore_view` or `viewer.scheduler`.

## What is LIVE and what is not (read this before editing)

LIVE -- the single source of these rules for the running viewer:

* `hot_order(center, n)` -- HOT's neighbour order.
* `coverage_order(n, center)` -- COVERAGE's both-ends-inward order.
* `ChannelCorrectionSpec` -- per-channel parameters for both methods.

`MultiChannelPrefetchController` calls the two ordering functions THROUGH
this module (`prefetch_rules.hot_order(...)`), not through a bound import,
so a test can monkeypatch them and prove the runtime consults this module
rather than carrying a copy of the same logic.

NOT LIVE -- `PrefetchPolicy` and its state machine (`CameraState`,
`MovementObservation`, `next_state`, `Priority`, `WorkItem`, the P0-P4
tiers, the `HOT_PER_COVERAGE` 3:1 interleave and their constants) have no
caller outside `tests/test_prefetch_policy.py`. The live controller
implements strict HOT priority instead of the 3:1 interleave, and has no
camera state machine. That divergence is deliberate for now and is
scheduled for removal in the next commit of this clean-up; do not build on
it, and do not "fix" the runtime to match it.

## Contract

### Camera states (see `CameraState` / `next_state`)

- RELOCATING: a navigator jump to a far location. Not-yet-started work at
  the old location is cancelled (a new generation is opened); the new
  location becomes the new origin. Only P0 (current channel, current
  viewport) is emitted. No hover/neighbour prefetch.
- MOVING: the user is dragging or zooming. Only the CURRENT channel is
  emitted -- current viewport, the movement buffer ahead, and whatever
  pyramid levels the zoom needs. Neighbour-channel and far-channel (P2/P3)
  work from the old origin is cancelled. Work already started is allowed
  to finish and land in cache (this module never cancels started work --
  see `is_stale`).
- SETTLED: the user has been still for `SETTLE_MS`. This establishes a new
  origin and is the ONLY state that produces P1/P2/P3/P4 background work.

### Priority classes (produced only in SETTLED)

- P0: current channel, current viewport.
- P1: current channel, enlarged movement buffer around the viewport.
- P2_HOT: neighbouring channels in list order i-1, i+1, i-2, i+2 (clamped
  at the ends of the channel list), each computing the CURRENT VIEWPORT
  region first.
- P3_COVERAGE: every remaining channel, walked from both ends of the list
  toward the middle (0, N-1, 1, N-2, ...), skipping channels already
  complete.
- P4: only after the user has been still for `LONG_SETTLE_MS` -- the
  surrounding region (movement-buffer-sized) of the OTHER channels.

### Budget split

HOT items and COVERAGE items are interleaved deterministically at ratio
`HOT_PER_COVERAGE` : 1 (three HOT items then one COVERAGE item, by
default) rather than randomised, so the split is exactly testable and the
ratio is a single named constant to retune.

### Foreground urgent

`set_foreground_urgent(True)` restricts `next_batch` to P0 only, for the
case where the user is actively interacting or has clicked a channel that
is not yet complete and needs every cycle it can get.

### Channel switching

Switching to a channel already complete is reported to the caller as
"complete" (see `on_channel_switch`'s return value) so the caller can
display the corrected result immediately; the policy itself never renders
anything. Switching to an incomplete channel promotes it to the new
centre `i` and to P0; the HOT order is regenerated around it. Rapid
switching is latest-request-wins: each switch bumps the generation, so an
older switch's still-queued work is stale by the time `is_stale` is
checked.

### Generations and cancellation

Every emitted `WorkItem` carries a generation token (an opaque int,
monotonically increasing). Opening a new origin (RELOCATING, or a fresh
SETTLED) or a channel switch bumps the generation. `is_stale(token)`
reports whether a token is older than the current generation, so the
caller can drop QUEUED work for it. This module never claims to cancel
work already started -- the contract, matching `viewer.scheduler`'s own
cancellation contract, is that started work always finishes and always
lands in cache; staleness only ever affects delivery/queueing decisions
made by the caller.

### Fixed budget

`next_batch(n)` never returns more than `min(n, OUTSTANDING_CAP)` items,
so the policy can never be asked to hand out an unbounded queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ── Named thresholds / constants ─────────────────────────────────────────────

# Time still (no movement) before MOVING settles into SETTLED. ~200ms is the
# contract's own figure -- short enough to feel responsive, long enough that
# a drag's natural per-frame pauses don't fire settle prematurely.
SETTLE_MS: float = 200.0

# Time still, continuing past SETTLE_MS, before P4 (surrounding region of
# OTHER channels) is allowed. This is a much longer dwell than SETTLE_MS
# because P4 is the lowest-value, highest-volume tier (every other channel's
# neighbourhood) and should only run when the session is genuinely idle.
LONG_SETTLE_MS: float = 3000.0

# A moved distance (in the same units as viewport bbox coordinates) at or
# above this, observed between two samples, means the camera is MOVING
# rather than merely jittering while settled.
MOVE_DISTANCE_THRESHOLD: float = 1.0

# A fractional viewport-area change (abs(new_area / old_area - 1.0)) at or
# above this counts as a zoom, i.e. movement, even with zero pan distance.
ZOOM_AREA_FRACTION_THRESHOLD: float = 0.02

# A single-sample move of at least this MANY VIEWPORTS is a far jump
# (RELOCATING) rather than a drag (MOVING) -- a navigator click across the
# slide, not a mouse drag.
#
# This is deliberately RELATIVE, not an absolute pixel count. An absolute
# threshold is wrong at every zoom but one: on this pyramid a zoomed-out
# viewport spans ~28800 level-0 pixels, so one ordinary 8% drag step moves
# ~2300 of them and would be misread as a jump, cancelling neighbour-channel
# work on every drag tick; while at level 0 a genuine jump of a few hundred
# pixels would be missed. Measuring the move against what the user can
# actually see is scale-free and correct at every level.
#
# 1.5 viewports: far enough that no drag gesture plausibly covers it in one
# sample, close enough that a navigator click to an adjacent region still
# counts as a jump.
RELOCATE_VIEWPORT_FRACTION: float = 1.5

# Deterministic HOT:COVERAGE interleave ratio for background work. "About
# 75/25" from the contract, realised as 3 HOT items per 1 COVERAGE item
# (an exact, testable repeating pattern rather than a probability).
HOT_PER_COVERAGE: int = 3

# Hard cap on outstanding items `next_batch` will ever hand out in one call,
# so the policy can never be asked to produce an unbounded queue.
OUTSTANDING_CAP: int = 64


# ── Camera state machine ─────────────────────────────────────────────────────

class CameraState(Enum):
    RELOCATING = "relocating"
    MOVING = "moving"
    SETTLED = "settled"


@dataclass(frozen=True)
class MovementObservation:
    """One sample of camera motion since the previous observation.

    `moved_distance`: pixel/world distance the viewport center moved.
    `viewport_extent`: the viewport's width (or max(w, h)) in the SAME
        units as `moved_distance`. Required to tell a drag from a jump:
        the two are distinguished by how far the camera moved RELATIVE TO
        what it can see, never by an absolute pixel count. Zero or absent
        means "unknown", and no jump is inferred.
    `area_change_fraction`: abs(new_area / old_area - 1.0); zero if unknown.
    `still_ms`: milliseconds since the last observation that counted as
        movement (i.e. cleared MOVE_DISTANCE_THRESHOLD or
        ZOOM_AREA_FRACTION_THRESHOLD). Only meaningful when this
        observation itself is not movement.
    """

    moved_distance: float = 0.0
    viewport_extent: float = 0.0
    area_change_fraction: float = 0.0
    still_ms: float = 0.0


# Transition table (documented explicitly, not just implied by the code
# below): from each state, an observation goes to exactly one of these.
#
#   RELOCATING -> RELOCATING  : another far jump before settling
#   RELOCATING -> MOVING      : a drag/zoom right after a jump, before settle
#   RELOCATING -> SETTLED     : still for >= SETTLE_MS
#   MOVING     -> RELOCATING  : a far jump interrupts a drag
#   MOVING     -> MOVING      : still moving
#   MOVING     -> SETTLED     : still for >= SETTLE_MS
#   SETTLED    -> RELOCATING  : a far jump
#   SETTLED    -> MOVING      : a drag/zoom (below jump threshold)
#   SETTLED    -> SETTLED     : still (jitter under the thresholds)
def next_state(current: CameraState, obs: MovementObservation) -> CameraState:
    """Map (current state, observation) -> next state. Pure; no side effects.

    A far jump always wins regardless of current state -- a RELOCATING
    target is never partially applied. Below that, any distance/area change
    clearing the MOVING thresholds means MOVING. Otherwise the state is
    SETTLED once `still_ms` has cleared SETTLE_MS, and otherwise unchanged
    (a MOVING observation with still_ms < SETTLE_MS stays MOVING; a SETTLED
    one with sub-threshold jitter stays SETTLED).
    """
    # A jump is judged relative to the viewport, never in absolute pixels
    # (see RELOCATE_VIEWPORT_FRACTION). With no viewport extent supplied we
    # cannot tell a jump from a drag, so we do not guess: the sample falls
    # through to the ordinary movement test below.
    if (obs.viewport_extent > 0.0
            and obs.moved_distance >= RELOCATE_VIEWPORT_FRACTION * obs.viewport_extent):
        return CameraState.RELOCATING

    is_moving_sample = (
        obs.moved_distance >= MOVE_DISTANCE_THRESHOLD
        or obs.area_change_fraction >= ZOOM_AREA_FRACTION_THRESHOLD
    )
    if is_moving_sample:
        return CameraState.MOVING

    if obs.still_ms >= SETTLE_MS:
        return CameraState.SETTLED

    # Not moving, not yet settled: RELOCATING/MOVING remain themselves until
    # settle fires; SETTLED remains SETTLED under sub-threshold jitter.
    return current


# ── Work items / priority classes ────────────────────────────────────────────

class Priority(Enum):
    P0 = 0
    P1 = 1
    P2_HOT = 2
    P3_COVERAGE = 3
    P4 = 4


Region = Tuple[float, float, float, float]  # opaque (x0, y0, x1, y1) bbox


@dataclass(frozen=True)
class WorkItem:
    """One unit of prefetch work. The policy never computes it -- it only
    describes it. `region` is an opaque bbox (see `Region`); callers that
    prefer tile-address sets are free to substitute their own type as long
    as it stays a plain, hashable-or-not value carried through untouched.
    """

    channel: int
    priority: Priority
    generation: int
    region: Region


def _clamp(i: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, i))


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


# ── Per-(channel, region) completion bookkeeping ─────────────────────────────

CompletionKey = Tuple[int, Region]


@dataclass
class PrefetchPolicy:
    """Decision layer only -- see module docstring for the full contract.

    `channels`: ordered list of channel indices this session knows about
        (typically `range(N)`, but kept explicit so callers can pass a
        filtered/reordered list).
    `current_index`: index INTO `channels` (not a channel id) of the
        current/centre channel.
    `viewport`: opaque current-viewport bbox.
    `buffer_region`: opaque enlarged movement-buffer bbox around
        `viewport` (P1's region; also P4's region for other channels).
    """

    channels: List[int]
    current_index: int = 0
    viewport: Region = (0.0, 0.0, 0.0, 0.0)
    buffer_region: Region = (0.0, 0.0, 0.0, 0.0)

    state: CameraState = CameraState.SETTLED
    _generation: int = field(default=0, init=False)
    _foreground_urgent: bool = field(default=False, init=False)
    _long_settled: bool = field(default=False, init=False)
    _complete: Dict[CompletionKey, bool] = field(default_factory=dict, init=False)
    # Running cursor into the deterministic HOT/COVERAGE interleave so
    # repeated next_batch calls continue the pattern rather than restarting
    # it (restarting every call would make the *documented* ratio true only
    # per-call, not over a long run, which is what the contract asks for).
    # `_interleave_pos` picks HOT-vs-COVERAGE at each step; `_hot_consumed`
    # / `_coverage_consumed` remember how far into EACH of those two
    # sequences we've already handed out, so a small `next_batch(n)` call
    # resumes mid-sequence next time instead of re-emitting the same first
    # HOT/COVERAGE item over and over.
    _interleave_pos: int = field(default=0, init=False)
    _hot_consumed: int = field(default=0, init=False)
    _coverage_consumed: int = field(default=0, init=False)

    # -- generation / staleness --------------------------------------------

    def _bump_generation(self) -> int:
        self._generation += 1
        return self._generation

    def is_stale(self, token: int) -> bool:
        """A token is stale once a newer origin/switch has superseded it.

        Only ever used by the caller to decide whether to drop QUEUED work;
        already-started work is never affected by this (see module
        docstring "Generations and cancellation").
        """
        return token < self._generation

    # -- completion bookkeeping ----------------------------------------------

    def mark_complete(self, channel: int, region: Region) -> None:
        self._complete[(channel, region)] = True

    def _is_complete(self, channel: int, region: Region) -> bool:
        return self._complete.get((channel, region), False)

    # -- state / mode changes -------------------------------------------------

    def on_state_change(self, new_state: CameraState) -> None:
        """Apply a state transition (typically the output of `next_state`).

        RELOCATING and a fresh SETTLED both open a new origin -- bump the
        generation so any queued P1-P4 work from before is reported stale.
        MOVING does NOT bump the generation: the contract is explicit that
        work already started during MOVING is allowed to land in cache, and
        re-entering MOVING from SETTLED should not orphan the in-flight P0
        work the caller just issued for the same channel/viewport.
        """
        entering_new_origin = new_state == CameraState.RELOCATING or (
            new_state == CameraState.SETTLED and self.state != CameraState.SETTLED
        )
        self.state = new_state
        if entering_new_origin:
            self._bump_generation()
            self._interleave_pos = 0
            self._hot_consumed = 0
            self._coverage_consumed = 0
        if new_state != CameraState.SETTLED:
            self._long_settled = False

    def set_long_settled(self, long_settled: bool) -> None:
        """Mark whether the SETTLED dwell has passed LONG_SETTLE_MS (P4 gate).

        Kept as an explicit setter (rather than a timer inside this pure
        module) so the caller owns the actual clock; this module only ever
        reacts to facts it is told.
        """
        self._long_settled = long_settled

    def set_foreground_urgent(self, urgent: bool) -> None:
        self._foreground_urgent = urgent

    def on_channel_switch(self, index: int) -> str:
        """Switch the centre channel to `channels[index]`.

        Returns "complete" if the CURRENT viewport of the new channel is
        already marked complete (caller should display corrected
        immediately), else "incomplete" (caller shows raw immediately; the
        channel is promoted to P0 by virtue of becoming `current_index`).
        Always bumps the generation -- rapid switching is latest-request-
        wins, so an older switch's queued work is stale as soon as this
        returns.
        """
        self.current_index = index
        self._bump_generation()
        self._interleave_pos = 0
        self._hot_consumed = 0
        self._coverage_consumed = 0
        channel = self.channels[index]
        return "complete" if self._is_complete(channel, self.viewport) else "incomplete"

    # -- work generation --------------------------------------------------

    def _p0_items(self) -> List[WorkItem]:
        channel = self.channels[self.current_index]
        return [WorkItem(channel, Priority.P0, self._generation, self.viewport)]

    def _p1_items(self) -> List[WorkItem]:
        channel = self.channels[self.current_index]
        return [WorkItem(channel, Priority.P1, self._generation, self.buffer_region)]

    def _hot_items(self) -> List[WorkItem]:
        n = len(self.channels)
        items = []
        for idx in hot_order(self.current_index, n):
            channel = self.channels[idx]
            if not self._is_complete(channel, self.viewport):
                items.append(WorkItem(channel, Priority.P2_HOT, self._generation, self.viewport))
        return items

    def _coverage_items(self) -> List[WorkItem]:
        n = len(self.channels)
        # "Every REMAINING channel" (contract wording) -- HOT already owns
        # the current viewport for i-1/i+1/i-2/i+2, so coverage skips those
        # too; otherwise the same (channel, viewport) pair would be queued
        # twice under two different priorities for no benefit.
        hot_idx = set(hot_order(self.current_index, n))
        items = []
        for idx in coverage_order(n, self.current_index):
            if idx in hot_idx:
                continue
            channel = self.channels[idx]
            if not self._is_complete(channel, self.viewport):
                items.append(
                    WorkItem(channel, Priority.P3_COVERAGE, self._generation, self.viewport)
                )
        return items

    def _p4_items(self) -> List[WorkItem]:
        n = len(self.channels)
        items = []
        for idx, channel in enumerate(self.channels):
            if idx == self.current_index:
                continue
            if not self._is_complete(channel, self.buffer_region):
                items.append(
                    WorkItem(channel, Priority.P4, self._generation, self.buffer_region)
                )
        return items

    def _interleave(
        self, hot: List[WorkItem], coverage: List[WorkItem], max_items: int
    ) -> List[WorkItem]:
        """Deterministic HOT_PER_COVERAGE:1 interleave, continuing BOTH the
        HOT/COVERAGE slot pattern (`_interleave_pos`) and each sequence's own
        read position (`_hot_consumed` / `_coverage_consumed`) across calls.

        Both `hot` and `coverage` are recomputed fresh on every call (so a
        `mark_complete` between calls is picked up immediately), but we still
        resume reading them at the previously-consumed offset rather than
        from the start -- otherwise a caller that polls with a small `n`
        would be handed the same first HOT item on every single call instead
        of progressing through the list (this was caught by a test: a run of
        small `next_batch(3)` calls kept re-emitting hot[0]).

        Only ever advances `_interleave_pos` for items actually returned
        (bounded by `max_items`) -- otherwise a capped call would silently
        "consume" interleave slots it never handed to the caller, throwing
        off the ratio over a long run of small/capped batches.
        """
        result: List[WorkItem] = []
        hi = min(self._hot_consumed, len(hot))
        ci = min(self._coverage_consumed, len(coverage))
        pos = self._interleave_pos
        while (hi < len(hot) or ci < len(coverage)) and len(result) < max_items:
            slot = pos % (HOT_PER_COVERAGE + 1)
            is_coverage_slot = slot == HOT_PER_COVERAGE
            if is_coverage_slot and ci < len(coverage):
                result.append(coverage[ci])
                ci += 1
                pos += 1
            elif not is_coverage_slot and hi < len(hot):
                result.append(hot[hi])
                hi += 1
                pos += 1
            elif ci < len(coverage):
                result.append(coverage[ci])
                ci += 1
                pos += 1
            elif hi < len(hot):
                result.append(hot[hi])
                hi += 1
                pos += 1
            else:
                pos += 1
        self._interleave_pos = pos
        self._hot_consumed = hi
        self._coverage_consumed = ci
        return result

    def next_batch(self, n: int) -> List[WorkItem]:
        """Return up to `min(n, OUTSTANDING_CAP)` ordered work items.

        Deterministic given the same policy state (no randomness, no
        wall-clock reads) -- callers may call this repeatedly for the same
        state and get the same answer, which is what makes the ratio/order
        tests below exact rather than statistical.
        """
        cap = min(n, OUTSTANDING_CAP)
        if cap <= 0:
            return []

        if self._foreground_urgent:
            return self._p0_items()[:cap]

        if self.state == CameraState.RELOCATING:
            # Contract: only P0, no hover/neighbour prefetch at all.
            return self._p0_items()[:cap]

        if self.state == CameraState.MOVING:
            # Contract: only the current channel -- P0 then P1, nothing else.
            items = self._p0_items() + self._p1_items()
            return items[:cap]

        # SETTLED: P0, P1, then the HOT/COVERAGE interleave, then P4 once
        # the long-settle dwell has been reported.
        items = self._p0_items() + self._p1_items()
        remaining = max(0, cap - len(items))
        items += self._interleave(self._hot_items(), self._coverage_items(), remaining)
        remaining = max(0, cap - len(items))
        if self._long_settled and remaining > 0:
            items += self._p4_items()[:remaining]
        return items[:cap]
