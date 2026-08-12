"""v14.5d B3c (selection layer): _validate_hq_config split + source-aware selection.

_validate_hq_selection is the source-INDEPENDENT parse/normalize shared by the
single-source path and the source-aware path. _hq_source_aware_selection validates the
selection against the re-resolved descriptor map with EXACT set equality (reference
names excluded), returning group=None (markers come from the per-channel handles).

Offscreen via unbound methods on a stub."""

import types

import pytest

from block01.workers.segment_merge_worker import SegmentMergeWorker
from block01.utils.segmentation_config import CELLPOSE_NUCLEI_HQ2


def _stub(method=CELLPOSE_NUCLEI_HQ2, hq_channels=("CK19", "CD68"),
          hq_input_mode="selected_channels_from_source",
          descriptor_channels=("CK19", "CD68")):
    desc = ({"channels": {c: {} for c in descriptor_channels}}
            if descriptor_channels is not None else None)
    s = types.SimpleNamespace(
        method=method,
        seg_config={"hq_input_mode": hq_input_mode, "hq_channels": list(hq_channels)},
        _pending_source_aware_runtime=desc)
    s._validate_hq_selection = lambda: SegmentMergeWorker._validate_hq_selection(s)
    return s


def test_validate_hq_selection_parses_mode_and_channels():
    mode, channels, requested, weights = SegmentMergeWorker._validate_hq_selection(_stub())
    assert mode == "selected_channels_from_source"
    assert channels == ["CK19", "CD68"]
    assert requested == ["CK19", "CD68"]


def test_source_aware_selection_success_group_none():
    s = _stub()
    channels, group = SegmentMergeWorker._hq_source_aware_selection(s)
    assert group is None                                  # no single hq_group
    assert set(channels) == {"CK19", "CD68"}
    assert s.seg_config["hq_channels"] == channels        # normalized selection stored


def test_source_aware_selection_rejects_extra_descriptor_marker():
    # descriptor has an extra 'resolved-but-unused' marker -> not exactly equal
    s = _stub(descriptor_channels=("CK19", "CD68", "EXTRA"))
    with pytest.raises(ValueError, match="exactly"):
        SegmentMergeWorker._hq_source_aware_selection(s)


def test_source_aware_selection_rejects_selected_not_in_descriptor():
    s = _stub(hq_channels=("CK19", "CD68"), descriptor_channels=("CK19",))
    with pytest.raises(ValueError, match="Missing HQ channel"):
        SegmentMergeWorker._hq_source_aware_selection(s)


def test_source_aware_selection_rejects_empty_selection():
    s = _stub(hq_channels=())
    with pytest.raises(ValueError):
        SegmentMergeWorker._hq_source_aware_selection(s)


def test_split_methods_exist():
    # the split must expose both halves; old callers use _validate_hq_config unchanged
    for name in ("_validate_hq_selection", "_open_hq_single_source",
                 "_validate_hq_config", "_hq_source_aware_selection"):
        assert callable(getattr(SegmentMergeWorker, name))
