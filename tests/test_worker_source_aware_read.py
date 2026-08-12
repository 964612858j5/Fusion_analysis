"""v14.5d B3c-2 (read routing): _read_hq_marker_channels routes to the per-channel
handles when a source-aware runtime is prepared, via the SHARED _read_one_marker_block
primitive. Covers HQ2's eager reader AND CSD lean-carve's _hq_block_loader (which
delegates to _read_hq_marker_channels), plus the hard-fail when a descriptor is pending
but not prepared."""

import types

import numpy as np
import pytest

from block01.workers.segment_merge_worker import SegmentMergeWorker
from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2


class _RS:
    def __init__(self, group):
        self.group = group


class _PC:
    def __init__(self, per):
        self.per_channel = per


def _stub():
    return types.SimpleNamespace(
        _pending_source_aware_runtime=None, _source_aware_per_channel=None,
        seg_config={"hq_input_mode": "selected_channels_from_source"},
        method=CELLPOSE_NUCLEI_HQ2, _current_region_bbox=None, _channel_store=None)


def test_read_hq_routes_each_marker_to_its_own_group():
    s = _stub()
    reads = []
    s._read_one_marker_block = lambda g, ch, y0, y1, x0, x1: (
        reads.append((g["g"], ch)) or np.zeros((y1 - y0, x1 - x0), np.float32))
    s._source_aware_per_channel = _PC({"CK19": _RS({"g": "A"}), "CD68": _RS({"g": "B"})})
    out = SegmentMergeWorker._read_hq_marker_channels(s, None, ["CK19", "CD68"], 0, 4, 0, 6)
    assert len(out) == 2 and all(b.shape == (4, 6) for b in out)
    assert reads == [("A", "CK19"), ("B", "CD68")]        # each from ITS group


def test_read_hq_hard_fails_when_pending_not_prepared():
    s = _stub()
    s._pending_source_aware_runtime = {"channels": {"CK19": {}}}
    s._source_aware_per_channel = None                    # prepare didn't run/succeed
    with pytest.raises(RuntimeError, match="not prepared"):
        SegmentMergeWorker._read_hq_marker_channels(s, None, ["CK19"], 0, 4, 0, 4)


def test_hq_block_loader_routes_through_per_channel():
    # CSD lean-carve uses _hq_block_loader, which delegates to _read_hq_marker_channels
    s = _stub()
    reads = []
    s._read_one_marker_block = lambda g, ch, y0, y1, x0, x1: (
        reads.append(ch) or np.zeros((y1 - y0, x1 - x0), np.float32))
    s._read_hq_marker_channels = lambda *a: SegmentMergeWorker._read_hq_marker_channels(s, *a)
    s._source_aware_per_channel = _PC({"CK19": _RS({"g": "A"})})
    loader = SegmentMergeWorker._hq_block_loader(s, None, ["CK19"], 0, 0)
    block = loader("CK19", 0, 4, 0, 4)
    assert block is not None and reads == ["CK19"]         # per-channel path, not hq_group


def test_read_one_marker_block_raw_and_zarr_dispatch():
    s = _stub()

    class _Loader:
        def read_region(self, ch, y0, y1, x0, x1, downsample=1, normalize=True):
            assert normalize is False                      # raw native
            return np.full((y1 - y0, x1 - x0), 3.0, np.float32)

    raw = SegmentMergeWorker._read_one_marker_block(
        s, {"kind": "raw_ome", "loader": _Loader()}, "CK19", 0, 2, 0, 2)
    assert raw.shape == (2, 2) and float(raw[0, 0]) == 3.0

    grp = {"CK19": np.arange(16, dtype=np.float32).reshape(4, 4)}  # zarr-like (no kind)
    z = SegmentMergeWorker._read_one_marker_block(s, grp, "CK19", 0, 2, 0, 2)
    assert z.shape == (2, 2)
    np.testing.assert_array_equal(z, grp["CK19"][0:2, 0:2])


def test_read_hq_single_source_uses_shared_primitive():
    # no source-aware -> plain per-channel single-source read via _read_one_marker_block
    s = _stub()
    seen = []
    s._read_one_marker_block = lambda g, ch, *b: seen.append((g, ch)) or np.zeros((2, 2), np.float32)
    out = SegmentMergeWorker._read_hq_marker_channels(s, "GRP", ["CK19", "CD68"], 0, 2, 0, 2)
    assert len(out) == 2
    assert seen == [("GRP", "CK19"), ("GRP", "CD68")]     # same primitive, single group
