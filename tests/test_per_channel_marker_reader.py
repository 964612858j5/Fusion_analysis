"""v14.5d Workstream B2 (core) — read each marker from its OWN resolved source.

The reader is a pure dispatch over a PerChannelResolvedSource with an INJECTED
read_block, so the per-channel source selection (raw_ome vs corrected_zarr) is
verifiable offscreen without any worker/GPU state. The worker supplies the real
channel-store-backed read_block at B3 wiring time; nothing here is wired live."""

import numpy as np
import pytest

from block01.workers.hq_source_resolver import (
    read_per_channel_marker_blocks, require_homogeneous_source)
from block01.utils.source_identity import (
    SOURCE_MIXTURE_HOMOGENEOUS_RAW, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED,
    SOURCE_MIXTURE_MIXED)


class _RS:
    """Fake ResolvedHQSource — only .group matters to the reader."""
    def __init__(self, group):
        self.group = group


class _Resolved:
    def __init__(self, per, mixture=None):
        self.per_channel = per
        self.source_mixture_mode = mixture


def _corrected(name):
    return _RS({"kind": "corrected_zarr", "group_name": name})


def _raw():
    return _RS({"kind": "raw_ome"})


def test_reads_each_channel_from_its_own_group():
    per = {"PanCK": _corrected("roi1"), "CD45": _corrected("roi1")}
    seen = []

    def read_block(group, ch, y0, y1, x0, x1):
        seen.append((group["kind"], ch, (y0, y1, x0, x1)))
        return np.full((y1 - y0, x1 - x0), 7.0, np.float32)

    out = read_per_channel_marker_blocks(
        _Resolved(per, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED),
        ["PanCK", "CD45"], (0, 4, 0, 6), read_block)
    assert list(out) == ["PanCK", "CD45"]                 # order preserved
    assert all(v.shape == (4, 6) for v in out.values())
    assert seen == [("corrected_zarr", "PanCK", (0, 4, 0, 6)),
                    ("corrected_zarr", "CD45", (0, 4, 0, 6))]


def test_mixed_group_dispatch_per_channel():
    # each channel routed to ITS group handle (raw vs corrected)
    per = {"PanCK": _corrected("roi1"), "CD45": _raw()}
    routed = {}

    def read_block(group, ch, *_bbox):
        routed[ch] = group["kind"]
        return np.zeros((2, 2), np.float32)

    read_per_channel_marker_blocks(_Resolved(per), ["PanCK", "CD45"],
                                   (0, 2, 0, 2), read_block)
    assert routed == {"PanCK": "corrected_zarr", "CD45": "raw_ome"}


def test_unresolved_channel_raises():
    per = {"PanCK": _corrected("roi1")}
    with pytest.raises(KeyError):
        read_per_channel_marker_blocks(
            _Resolved(per), ["PanCK", "CD45"], (0, 2, 0, 2),
            lambda *a: np.zeros((2, 2)))


def test_require_homogeneous_allows_raw_and_corrected():
    assert require_homogeneous_source(
        _Resolved({}, SOURCE_MIXTURE_HOMOGENEOUS_RAW)) == SOURCE_MIXTURE_HOMOGENEOUS_RAW
    assert require_homogeneous_source(
        _Resolved({}, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED)) == SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED


def test_require_homogeneous_rejects_mixed():
    with pytest.raises(ValueError):
        require_homogeneous_source(_Resolved({}, SOURCE_MIXTURE_MIXED))


def test_require_homogeneous_rejects_none_empty_unknown():
    # allowlist: absent/garbage mixture must NOT slip past as "not mixed"
    for bad in (None, "", "homogeneous", "corrected", "raw", "whatever"):
        with pytest.raises(ValueError):
            require_homogeneous_source(_Resolved({}, bad))


def test_reader_dedups_repeated_channels():
    per = {"PanCK": _corrected("roi1")}
    calls = []

    def read_block(group, ch, *_bbox):
        calls.append(ch)
        return np.zeros((2, 2), np.float32)

    out = read_per_channel_marker_blocks(
        _Resolved(per, SOURCE_MIXTURE_HOMOGENEOUS_CORRECTED),
        ["PanCK", "PanCK"], (0, 2, 0, 2), read_block)
    assert list(out) == ["PanCK"]
    assert calls == ["PanCK"]        # read once despite duplicate selection
