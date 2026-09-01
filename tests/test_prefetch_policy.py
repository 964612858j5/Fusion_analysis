"""Tests for the prefetch ORDERING rules (viewer/prefetch_policy.py).

These are the rules the live `MultiChannelPrefetchController` actually
prefetches by -- it calls them through the module, and
`tests/test_multichannel_prefetch.py` proves that wiring by patching a rule
and watching the runtime's request order follow it.

The unwired experimental policy layer these tests used to also cover (the
RELOCATING/MOVING/SETTLED machine with `next_state`, P0-P4, the 3:1
HOT:COVERAGE interleave, `PrefetchPolicy.next_batch`, per-item generations
and the outstanding cap) was deleted along with the code it tested: it had
no caller outside this file and had drifted from the runtime, which uses
strict HOT priority. Runtime behaviour is covered where it lives, in
test_multichannel_prefetch.py.
"""

import pytest

from viewer.prefetch_policy import (
    ChannelCorrectionSpec,
    coverage_order,
    hot_order,
)


# ── HOT: the neighbour order ─────────────────────────────────────────────────

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


def test_hot_order_never_repeats_or_includes_the_centre():
    for n in (1, 2, 5, 10, 57):
        for center in range(n):
            order = hot_order(center, n)
            assert center not in order
            assert len(order) == len(set(order))
            assert all(0 <= i < n for i in order)


def test_hot_order_on_a_57_channel_list_by_name():
    """Stable fake channel list -- no real WSI, no real channel names."""
    names = [f"ch{i:02d}" for i in range(57)]
    got = [names[i] for i in hot_order(28, len(names))]
    assert got == ["ch27", "ch29", "ch26", "ch30"]


# ── COVERAGE: both ends inward ───────────────────────────────────────────────

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
    assert coverage_order(n, center) == expected


def test_coverage_order_covers_every_channel_but_the_centre_exactly_once():
    for n in (1, 2, 5, 10, 57):
        for center in range(n):
            order = coverage_order(n, center)
            assert sorted(order) == [i for i in range(n) if i != center]


def test_coverage_order_on_a_57_channel_list_by_name():
    names = [f"ch{i:02d}" for i in range(57)]
    got = [names[i] for i in coverage_order(len(names), 28)]
    assert got[:12] == ["ch00", "ch56", "ch01", "ch55", "ch02", "ch54",
                        "ch03", "ch53", "ch04", "ch52", "ch05", "ch51"]
    assert len(got) == 56 and "ch28" not in got


# ── The one data type ────────────────────────────────────────────────────────

def test_channel_correction_spec_carries_both_methods_parameters():
    """A `PrefetchSnapshot` describes the CURRENT method only, so both
    methods' parameters have to be supplied per channel."""
    spec = ChannelCorrectionSpec(channel="CD8", tophat_radius=25,
                                 cucim_sigma=30)
    assert (spec.channel, spec.tophat_radius, spec.cucim_sigma) == (
        "CD8", 25, 30)
    with pytest.raises(Exception):
        spec.tophat_radius = 1      # frozen
