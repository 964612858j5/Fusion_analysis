"""Tests for the pure prefetch decision policy (viewer/prefetch_policy.py).

This module is not wired into the live viewer; these tests exercise it in
isolation, per the contract documented in prefetch_policy.py's module
docstring.
"""

import pytest

from viewer.prefetch_policy import (
    CameraState,
    MovementObservation,
    OUTSTANDING_CAP,
    Priority,
    PrefetchPolicy,
    RELOCATE_VIEWPORT_FRACTION,
    SETTLE_MS,
    coverage_order,
    hot_order,
    next_state,
)


VP = (0.0, 0.0, 10.0, 10.0)
BUF = (-5.0, -5.0, 15.0, 15.0)


def make_policy(n_channels=10, current_index=0):
    p = PrefetchPolicy(channels=list(range(n_channels)), current_index=current_index)
    p.viewport = VP
    p.buffer_region = BUF
    return p


# ── 1. State transition table ────────────────────────────────────────────────

def test_still_stays_settled():
    assert next_state(CameraState.SETTLED, MovementObservation(still_ms=0.0)) == CameraState.SETTLED


def test_still_to_moving():
    obs = MovementObservation(moved_distance=5.0)
    assert next_state(CameraState.SETTLED, obs) == CameraState.MOVING


def test_moving_stays_moving_before_settle_threshold():
    obs = MovementObservation(still_ms=SETTLE_MS - 1.0)
    assert next_state(CameraState.MOVING, obs) == CameraState.MOVING


def test_moving_to_settled_after_threshold():
    obs = MovementObservation(still_ms=SETTLE_MS)
    assert next_state(CameraState.MOVING, obs) == CameraState.SETTLED


def test_settled_to_relocating_on_jump():
    obs = MovementObservation(moved_distance=RELOCATE_VIEWPORT_FRACTION * 1400.0,
                              viewport_extent=1400.0)
    assert next_state(CameraState.SETTLED, obs) == CameraState.RELOCATING


def test_moving_to_relocating_on_jump():
    obs = MovementObservation(moved_distance=RELOCATE_VIEWPORT_FRACTION * 1400.0 + 1.0,
                              viewport_extent=1400.0)
    assert next_state(CameraState.MOVING, obs) == CameraState.RELOCATING


def test_relocating_to_moving():
    obs = MovementObservation(moved_distance=5.0)
    assert next_state(CameraState.RELOCATING, obs) == CameraState.MOVING


def test_relocating_to_settled():
    obs = MovementObservation(still_ms=SETTLE_MS)
    assert next_state(CameraState.RELOCATING, obs) == CameraState.SETTLED


def test_zoom_area_change_counts_as_moving():
    obs = MovementObservation(moved_distance=0.0, area_change_fraction=0.5)
    assert next_state(CameraState.SETTLED, obs) == CameraState.MOVING


# ── 2. P2_HOT order ──────────────────────────────────────────────────────────

# `hot_order` / `coverage_order` are the PUBLIC rule source the live
# `MultiChannelPrefetchController` calls (through the module, so a test can
# monkeypatch them -- see tests/test_multichannel_prefetch.py). They are
# therefore tested directly here, not through `PrefetchPolicy`, which no
# runtime code calls.
@pytest.mark.parametrize(
    ("center", "n", "expected"),
    [(5, 10, [4, 6, 3, 7]),          # middle: i-1, i+1, i-2, i+2
     (0, 10, [1, 2]),                # low end: out-of-range offsets dropped
     (9, 10, [8, 7]),                # high end: same, mirrored
     (1, 10, [0, 2, 3]),             # one in from the end
     (28, 57, [27, 29, 26, 30]),     # 57-channel slide, middle
     (0, 57, [1, 2]),
     (56, 57, [55, 54]),
     (0, 1, []),                     # a single channel has no neighbour
     (0, 2, [1]),
     (1, 2, [0])],
)
def test_hot_order_is_the_neighbour_sequence(center, n, expected):
    assert hot_order(center, n) == expected


def test_hot_order_on_a_57_channel_list_by_name():
    """Stable fake channel list -- no real WSI, no real channel names."""
    names = [f"ch{i:02d}" for i in range(57)]
    got = [names[i] for i in hot_order(28, len(names))]
    assert got == ["ch27", "ch29", "ch26", "ch30"]


def test_hot_order_still_drives_the_policys_own_p2_tier():
    """`PrefetchPolicy` is not wired into the runtime, but while it exists
    its HOT tier must come from the same function, not a second copy."""
    policy = make_policy(n_channels=10, current_index=5)
    policy.on_state_change(CameraState.SETTLED)
    hot = [w for w in policy.next_batch(100) if w.priority == Priority.P2_HOT]
    assert [w.channel for w in hot] == hot_order(5, 10)


# ── 3. P3_COVERAGE order ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("n", "center", "expected"),
    [(8, 0, [7, 1, 6, 2, 5, 3, 4]),
     (10, 4, [0, 9, 1, 8, 2, 7, 3, 6, 5]),
     (10, 0, [9, 1, 8, 2, 7, 3, 6, 4, 5]),
     (10, 9, [0, 1, 8, 2, 7, 3, 6, 4, 5]),
     (2, 0, [1]),
     (1, 0, [])],
)
def test_coverage_order_walks_from_both_ends(n, center, expected):
    """Pure order generation, straight through the public function so HOT's
    own exclusion (tested separately below) does not interact here."""
    assert coverage_order(n, center) == expected


def test_coverage_order_on_a_57_channel_list_by_name():
    names = [f"ch{i:02d}" for i in range(57)]
    got = [names[i] for i in coverage_order(len(names), 28)]
    assert got[:12] == ["ch00", "ch56", "ch01", "ch55", "ch02", "ch54",
                        "ch03", "ch53", "ch04", "ch52", "ch05", "ch51"]
    assert len(got) == 56 and "ch28" not in got


def test_coverage_skips_completed_and_hot_owned_channels():
    # Through the policy: coverage additionally skips (a) already-complete
    # channels and (b) channels HOT already owns for the current viewport
    # (documented in _coverage_items -- no point queuing the same
    # (channel, viewport) pair under two priorities).
    n = 8
    policy = make_policy(n_channels=n, current_index=0)
    policy.on_state_change(CameraState.SETTLED)
    policy.mark_complete(7, VP)
    coverage = [w for w in policy.next_batch(1000) if w.priority == Priority.P3_COVERAGE]
    got = [w.channel for w in coverage]
    # HOT for center=0 owns channels {1, 2} (clamped i-1/i-2 are invalid).
    assert 1 not in got
    assert 2 not in got
    assert 7 not in got
    assert got == [6, 5, 3, 4]


# ── 4. HOT/COVERAGE interleave ratio ─────────────────────────────────────────

def test_hot_coverage_interleave_ratio_over_long_run():
    n = 40
    policy = make_policy(n_channels=n, current_index=20)
    policy.on_state_change(CameraState.SETTLED)
    items = policy.next_batch(1000)
    background = [w for w in items if w.priority in (Priority.P2_HOT, Priority.P3_COVERAGE)]
    # First 4 (i-1,i+1,i-2,i+2) are HOT; enough coverage entries exist to
    # keep interleaving for a long run. Verify the exact repeating pattern:
    # HOT, HOT, HOT, COVERAGE, repeat (HOT_PER_COVERAGE == 3).
    tags = [w.priority for w in background]
    # Slot pattern is HOT,HOT,HOT,COVERAGE repeating (HOT_PER_COVERAGE == 3).
    # There are only 4 HOT entries (i-1,i+1,i-2,i+2), so they are consumed
    # across slots 0,1,2 (first three HOT slots) and then slot 4 (the next
    # HOT-slot in the repeating pattern, since slot 3 was COVERAGE) -- five
    # items in total before HOT is exhausted and everything else falls back
    # to COVERAGE.
    assert tags[:5] == [
        Priority.P2_HOT,
        Priority.P2_HOT,
        Priority.P2_HOT,
        Priority.P3_COVERAGE,
        Priority.P2_HOT,
    ]
    assert all(t == Priority.P3_COVERAGE for t in tags[5:])
    # Over this long run the realised ratio should be close to the
    # documented ~75/25 split (exact 3:1 while HOT still has entries, then
    # all-coverage once HOT is exhausted -- so the overall ratio is biased
    # toward coverage for a run this long, but the first segment is exact).
    hot_count = sum(1 for t in tags if t == Priority.P2_HOT)
    assert hot_count == 4


def test_interleave_cursor_continues_across_small_batches():
    # Two policies in identical starting state: one is asked for everything
    # in a single call, the other is asked repeatedly for small batches (each
    # call re-runs P0/P1 but the interleave cursor must keep advancing so the
    # background portion lines up item-for-item with the single big call).
    n = 40
    single = make_policy(n_channels=n, current_index=20)
    single.on_state_change(CameraState.SETTLED)
    single_background = [
        w for w in single.next_batch(1000)
        if w.priority in (Priority.P2_HOT, Priority.P3_COVERAGE)
    ]

    incremental = make_policy(n_channels=n, current_index=20)
    incremental.on_state_change(CameraState.SETTLED)
    incremental_background: list = []
    for _ in range(20):
        items = incremental.next_batch(3)
        for w in items:
            if w.priority in (Priority.P2_HOT, Priority.P3_COVERAGE):
                incremental_background.append(w)

    # Both must agree on the channel order of the background portion,
    # deduplicating consecutive repeats caused by P0/P1 re-occupying the
    # front of every small batch (P0/P1 are not part of this comparison).
    single_channels = [w.channel for w in single_background]
    incremental_channels = [w.channel for w in incremental_background]
    assert incremental_channels == single_channels[: len(incremental_channels)]
    assert len(incremental_channels) > 0


# ── 5. Foreground urgent ─────────────────────────────────────────────────────

def test_foreground_urgent_yields_only_p0():
    policy = make_policy(n_channels=10, current_index=3)
    policy.on_state_change(CameraState.SETTLED)
    policy.set_foreground_urgent(True)
    items = policy.next_batch(100)
    assert len(items) == 1
    assert items[0].priority == Priority.P0
    assert items[0].channel == 3


# ── 6. Channel switch regenerates HOT + bumps generation ────────────────────

def test_channel_switch_regenerates_hot_and_bumps_generation():
    policy = make_policy(n_channels=10, current_index=0)
    policy.on_state_change(CameraState.SETTLED)
    old_items = policy.next_batch(100)
    old_gen = old_items[0].generation

    result = policy.on_channel_switch(5)
    assert result == "incomplete"

    new_items = policy.next_batch(100)
    hot = [w for w in new_items if w.priority == Priority.P2_HOT]
    assert [w.channel for w in hot] == [4, 6, 3, 7]
    assert policy.is_stale(old_gen)
    assert not policy.is_stale(new_items[0].generation)


def test_channel_switch_reports_complete_when_already_done():
    policy = make_policy(n_channels=10, current_index=0)
    policy.mark_complete(5, VP)
    result = policy.on_channel_switch(5)
    assert result == "complete"


# ── 7. Rapid successive switches: latest wins ────────────────────────────────

def test_rapid_switches_latest_wins():
    policy = make_policy(n_channels=10, current_index=0)
    policy.on_channel_switch(3)
    gen_a = policy._generation
    policy.on_channel_switch(7)
    gen_b = policy._generation
    assert gen_b > gen_a
    assert policy.is_stale(gen_a)
    assert policy.current_index == 7
    items = policy.next_batch(1)
    assert items[0].channel == 7


# ── 8. Outstanding cap ────────────────────────────────────────────────────────

def test_next_batch_never_exceeds_outstanding_cap():
    policy = make_policy(n_channels=200, current_index=100)
    policy.on_state_change(CameraState.SETTLED)
    policy.set_long_settled(True)
    items = policy.next_batch(10_000)
    assert len(items) <= OUTSTANDING_CAP
    assert len(items) == OUTSTANDING_CAP


def test_next_batch_respects_smaller_n_than_cap():
    policy = make_policy(n_channels=200, current_index=100)
    policy.on_state_change(CameraState.SETTLED)
    items = policy.next_batch(3)
    assert len(items) == 3


# ── 9. MOVING emits only the current channel ─────────────────────────────────

def test_moving_emits_only_current_channel():
    policy = make_policy(n_channels=10, current_index=4)
    policy.on_state_change(CameraState.MOVING)
    items = policy.next_batch(100)
    assert len(items) > 0
    assert all(w.channel == 4 for w in items)
    assert all(w.priority in (Priority.P0, Priority.P1) for w in items)


# ── 10. RELOCATING emits no hover/neighbour prefetch ─────────────────────────

def test_relocating_emits_only_p0_no_hover():
    policy = make_policy(n_channels=10, current_index=4)
    policy.on_state_change(CameraState.RELOCATING)
    items = policy.next_batch(100)
    assert len(items) == 1
    assert items[0].priority == Priority.P0
    assert items[0].channel == 4


def test_relocating_after_settled_bumps_generation():
    policy = make_policy(n_channels=10, current_index=0)
    policy.on_state_change(CameraState.SETTLED)
    settled_items = policy.next_batch(100)
    settled_gen = settled_items[0].generation

    policy.on_state_change(CameraState.RELOCATING)
    reloc_items = policy.next_batch(100)
    assert policy.is_stale(settled_gen)
    assert reloc_items[0].generation != settled_gen


def test_jump_threshold_is_viewport_relative_not_absolute():
    """A drag is told from a jump by how far the camera moved RELATIVE TO
    what it can see, never by an absolute pixel count.

    An absolute threshold is wrong at every zoom but one. On this pyramid a
    zoomed-out viewport spans ~28800 level-0 pixels, so a single ordinary
    8% drag step moves ~2300 of them; under the original absolute 1000px
    rule that drag was classified as a navigator jump, which would cancel
    neighbour-channel work on every tick of an ordinary drag."""
    # An ordinary drag step at a zoomed-OUT view: large in pixels, small
    # relative to the viewport. Must be MOVING, not RELOCATING.
    wide = MovementObservation(moved_distance=2300.0, viewport_extent=28800.0)
    assert next_state(CameraState.SETTLED, wide) is CameraState.MOVING

    # The same pixel distance at a zoomed-IN view is more than a viewport
    # away, so it IS a jump.
    tight = MovementObservation(moved_distance=2300.0, viewport_extent=1400.0)
    assert next_state(CameraState.SETTLED, tight) is CameraState.RELOCATING

    # Without a viewport extent we cannot tell, so we must not guess a jump.
    unknown = MovementObservation(moved_distance=1e9, viewport_extent=0.0)
    assert next_state(CameraState.SETTLED, unknown) is CameraState.MOVING
