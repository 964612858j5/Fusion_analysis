"""Tests for workers/hq_source_resolver.py — shared HQ marker source resolver.

Covers the full Step2 HQ source DECISION: the corrected-vs-raw initial choice
AND the "corrected present but missing a requested channel; raw OME has all of
them -> fall back the WHOLE source to raw OME" rule, plus the negative case
(corrected missing + raw missing) which must NOT be swallowed.

Pure-resolver tests use a tiny real zarr group and a fake OME loader factory; no
full WSI, no GUI. The worker-delegation test builds a SegmentMergeWorker shell to
confirm the refactor is behavior-preserving (same source path + seg_config).
"""

import os

import numpy as np
import pytest
import zarr

from block01.workers.hq_source_resolver import (
    resolve_hq_marker_source,
    ResolvedHQSource,
    RAW_OME_INTENSITY_SPACE,
    UNKNOWN_INTENSITY_SPACE,
    SOURCE_MODE_CORRECTED_THEN_RAW,
    SOURCE_MODE_RAW_ONLY,
    SOURCE_MODE_CORRECTED_ONLY,
)


# ── fakes ──────────────────────────────────────────────────────────────────

class _FakeOMELoader:
    """Minimal OMETIFFLoader stand-in: channel_names() + shape."""

    REGISTRY = {}  # path -> list of channel names

    def __init__(self, path, *a, **k):
        self.path = path
        self._names = list(self.REGISTRY.get(path, []))
        self.shape = (16, 24)

    def channel_names(self):
        return list(self._names)


def _make_corrected_zarr(path, channel_names, *, attrs=None, shape=(16, 24)):
    """Write a flat (root-group) corrected_channels.zarr with given channels."""
    root = zarr.open(str(path), mode="w")
    for name in channel_names:
        root.create_dataset(name, data=np.zeros(shape, dtype=np.float32))
    for k, v in (attrs or {}).items():
        root.attrs[k] = v
    return str(path)


def _resolve(requested, corrected_path, raw_path, raw_channels=None, **kw):
    if raw_channels is not None:
        _FakeOMELoader.REGISTRY[raw_path] = raw_channels
    return resolve_hq_marker_source(
        requested_channels=requested,
        multichannel_source_path=corrected_path,
        raw_channel_source_path=raw_path,
        roi_id="", requested_roi_names=set(),
        param_file="", abs_fn=os.path.abspath,
        loader_factory=_FakeOMELoader, **kw)


# ── scenario 1: corrected present with ALL requested channels ───────────────

def test_corrected_present_all_channels(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    res = _resolve(["CD45", "CK19"], cp, "")
    assert isinstance(res, ResolvedHQSource)
    assert res.kind == "corrected_zarr"
    assert res.fell_back_to_raw is False
    assert res.missing_channels == []
    assert set(res.resolved_channels) == {"CD45", "CK19"}
    assert res.source_path == os.path.abspath(cp)
    assert res.channel_shape("CD45") == (16, 24)


# ── scenario 2: corrected absent, raw OME present with all channels ─────────

def test_corrected_absent_raw_present(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45", "CK19"], "", raw, raw_channels=["DAPI", "CD45", "CK19"])
    assert res.kind == "raw_ome"
    assert res.fell_back_to_raw is False  # corrected was never the candidate
    assert res.missing_channels == []
    assert res.source_path == raw
    assert res.intensity_space == RAW_OME_INTENSITY_SPACE
    assert res.channel_shape("CD45") == (16, 24)


def test_corrected_nonexistent_path_falls_to_raw(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45"], str(tmp_path / "missing.zarr"), raw,
                   raw_channels=["DAPI", "CD45"])
    assert res.kind == "raw_ome"


# ── scenario 3: corrected MISSING a channel, raw has all -> whole-source raw ─

def test_corrected_missing_channel_falls_back_to_raw(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])  # no CK19
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45", "CK19"], cp, raw,
                   raw_channels=["DAPI", "CD45", "CK19"])
    assert res.kind == "raw_ome"
    assert res.fell_back_to_raw is True
    assert res.missing_channels == []
    assert set(res.resolved_channels) == {"CD45", "CK19"}
    assert res.source_path == os.path.abspath(raw)


# ── scenario 4 (NEGATIVE): corrected missing AND raw missing -> not swallowed ─

def test_corrected_missing_and_raw_missing_reports_missing(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])  # no CK19
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45", "CK19"], cp, raw,
                   raw_channels=["DAPI", "CD45"])  # raw also lacks CK19
    # Must NOT return a valid raw_ome source; must report the still-missing channel
    # so the worker raises exactly as before.
    assert res.kind == "corrected_zarr"
    assert res.fell_back_to_raw is False
    assert res.missing_channels == ["CK19"]


# ── intensity-space rules ───────────────────────────────────────────────────

def test_raw_intensity_space_is_native_float(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45"], "", raw, raw_channels=["CD45"])
    assert res.intensity_space == RAW_OME_INTENSITY_SPACE


def test_corrected_intensity_space_unknown_when_undeterminable(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])
    res = _resolve(["CD45"], cp, "")
    assert res.intensity_space == UNKNOWN_INTENSITY_SPACE


def test_corrected_intensity_space_from_explicit_metadata(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"],
                              attrs={"intensity_space": "corrected_zarr_native_float"})
    res = _resolve(["CD45"], cp, "")
    assert res.intensity_space == "corrected_zarr_native_float"


# ── requires requested_channels ─────────────────────────────────────────────

def test_requires_requested_channels(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])
    with pytest.raises(ValueError):
        resolve_hq_marker_source(
            requested_channels=None,
            multichannel_source_path=cp, raw_channel_source_path="",
            roi_id="", requested_roi_names=set(),
            abs_fn=os.path.abspath, loader_factory=_FakeOMELoader)


def test_no_source_at_all_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        _resolve(["CD45"], "", "")


# ── worker-delegation behavior preservation ────────────────────────────────

def _worker_shell(monkeypatch, corrected_path, raw_path, raw_channels, hq_channels,
                  method="cellpose_nuclei_hq"):
    """A SegmentMergeWorker built via __new__ with only the attrs _validate_hq_config
    touches, OMETIFFLoader monkeypatched to the fake loader, and _hq_source_mode set
    from the method exactly as __init__ would. Default method is a non-HQ2/CSD HQ
    method (corrected_then_raw_fallback) so the corrected-path delegation behaves as
    in 2.1c-a; pass a CSD/HQ2 method to exercise raw_ome_only."""
    from block01.workers import segment_merge_worker as smw
    from block01.workers.hq_source_resolver import (
        SOURCE_MODE_RAW_ONLY as _RAW, SOURCE_MODE_CORRECTED_THEN_RAW as _CR)
    _FakeOMELoader.REGISTRY[raw_path] = list(raw_channels)
    monkeypatch.setattr(smw, "OMETIFFLoader", _FakeOMELoader)
    w = smw.SegmentMergeWorker.__new__(smw.SegmentMergeWorker)
    w.seg_config = {"hq_channels": list(hq_channels),
                    "hq_input_mode": "selected_channels_from_source"}
    w.method = method
    w._hq_source_mode = _RAW if smw.is_hq2_csd_method(method) else _CR
    w.roi_id = ""
    w.roi_display_name = ""
    w.param_file = ""
    w._logger = None
    w._hq_resolved_source_path = ""
    w._multichannel_source_path = lambda: corrected_path
    w._raw_channel_source_path = lambda: raw_path
    return w


def test_worker_delegates_corrected_present(tmp_path, monkeypatch):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    raw = str(tmp_path / "raw.ome.tiff")
    w = _worker_shell(monkeypatch, cp, raw, ["DAPI", "CD45", "CK19"], ["CD45", "CK19"])
    channels, group = w._validate_hq_config()
    assert w._hq_resolved_source_path == os.path.abspath(cp)
    assert sorted(w.seg_config["hq_channels"]) == ["CD45", "CK19"]


def test_worker_delegates_fallback_to_raw(tmp_path, monkeypatch):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])  # no CK19
    raw = str(tmp_path / "raw.ome.tiff")
    w = _worker_shell(monkeypatch, cp, raw, ["DAPI", "CD45", "CK19"], ["CD45", "CK19"])
    channels, group = w._validate_hq_config()
    assert w._hq_resolved_source_path == os.path.abspath(raw)
    assert isinstance(group, dict) and group.get("kind") == "raw_ome"
    assert sorted(w.seg_config["hq_channels"]) == ["CD45", "CK19"]


def test_worker_delegates_negative_raises_as_before(tmp_path, monkeypatch):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])  # no CK19
    raw = str(tmp_path / "raw.ome.tiff")
    w = _worker_shell(monkeypatch, cp, raw, ["DAPI", "CD45"], ["CD45", "CK19"])  # raw lacks CK19
    with pytest.raises(ValueError):  # validate_hq_channels raises on missing CK19
        w._validate_hq_config()


# ── 2.1c-b.1: three-valued source_mode ──────────────────────────────────────

def test_default_mode_resolves_corrected_when_complete(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    res = _resolve(["CD45", "CK19"], cp, "",
                   source_mode=SOURCE_MODE_CORRECTED_THEN_RAW)
    assert res.kind == "corrected_zarr"
    assert res.source_mode == SOURCE_MODE_CORRECTED_THEN_RAW
    assert res.source_selected_by == "corrected_primary"
    assert res.fell_back_to_raw is False


def test_default_mode_corrected_missing_falls_back_to_raw(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])  # no CK19
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45", "CK19"], cp, raw, raw_channels=["DAPI", "CD45", "CK19"],
                   source_mode=SOURCE_MODE_CORRECTED_THEN_RAW)
    assert res.kind == "raw_ome"
    assert res.fell_back_to_raw is True
    assert res.source_selected_by == "fallback"


def test_raw_only_resolves_raw_even_when_corrected_complete(tmp_path):
    # corrected exists AND is channel-complete; raw_ome_only must STILL pick raw.
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45", "CK19"], cp, raw, raw_channels=["DAPI", "CD45", "CK19"],
                   source_mode=SOURCE_MODE_RAW_ONLY)
    assert res.kind == "raw_ome"
    assert res.source_path == os.path.abspath(raw)
    assert res.fell_back_to_raw is False           # policy, not fallback
    assert res.source_selected_by == "policy"
    assert res.source_mode == SOURCE_MODE_RAW_ONLY
    assert res.intensity_space == RAW_OME_INTENSITY_SPACE


def test_raw_only_raw_missing_reports_missing_no_corrected_fallback(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    raw = str(tmp_path / "raw.ome.tiff")
    res = _resolve(["CD45", "CK19"], cp, raw, raw_channels=["DAPI", "CD45"],  # raw lacks CK19
                   source_mode=SOURCE_MODE_RAW_ONLY)
    assert res.kind == "raw_ome"          # never corrected
    assert res.missing_channels == ["CK19"]
    assert res.fell_back_to_raw is False


def test_corrected_only_mode_raises_not_implemented(tmp_path):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45"])
    with pytest.raises(NotImplementedError) as exc:
        _resolve(["CD45"], cp, "", source_mode=SOURCE_MODE_CORRECTED_ONLY)
    assert "corrected_zarr_only" in str(exc.value)


def test_unknown_source_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        _resolve(["CD45"], "", "", source_mode="bogus_mode")


# ── 2.1c-b.1: worker stores source_mode by algorithm (single source of truth) ─

def test_is_hq2_csd_method_predicate():
    from block01.workers import segment_merge_worker as smw
    from block01.utils.segmentation_config import (
        CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD, CELLPOSE_NUCLEI_HQ,
        CELLPOSE_NUCLEI_DAPI)
    assert smw.is_hq2_csd_method(CELLPOSE_NUCLEI_HQ2) is True
    assert smw.is_hq2_csd_method(CELLPOSE_NUCLEI_CSD) is True
    assert smw.is_hq2_csd_method(CELLPOSE_NUCLEI_HQ) is False
    assert smw.is_hq2_csd_method(CELLPOSE_NUCLEI_DAPI) is False
    assert smw.is_hq2_csd_method("mesmer_whole_cell") is False


def test_worker_hq2_csd_uses_raw_only_even_if_corrected_complete(tmp_path, monkeypatch):
    # corrected exists AND complete; CSD method must STILL resolve raw OME.
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    raw = str(tmp_path / "raw.ome.tiff")
    w = _worker_shell(monkeypatch, cp, raw, ["DAPI", "CD45", "CK19"], ["CD45", "CK19"],
                      method="cellpose_nuclei_csd")
    assert w._hq_source_mode == SOURCE_MODE_RAW_ONLY
    channels, group = w._validate_hq_config()
    assert isinstance(group, dict) and group.get("kind") == "raw_ome"
    assert w._hq_resolved_source_path == os.path.abspath(raw)


def test_worker_non_hq2csd_uses_corrected_then_raw(tmp_path, monkeypatch):
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    raw = str(tmp_path / "raw.ome.tiff")
    w = _worker_shell(monkeypatch, cp, raw, ["DAPI", "CD45", "CK19"], ["CD45", "CK19"],
                      method="cellpose_nuclei_hq")
    assert w._hq_source_mode == SOURCE_MODE_CORRECTED_THEN_RAW
    channels, group = w._validate_hq_config()
    assert w._hq_resolved_source_path == os.path.abspath(cp)  # corrected, unchanged


def test_worker_open_hq_group_raw_only_ignores_corrected(tmp_path, monkeypatch):
    # _open_hq_channel_group (used by mesmer path) must also honor raw_ome_only:
    # a CSD-mode worker must not open corrected even when complete.
    cp = _make_corrected_zarr(tmp_path / "corrected.zarr", ["DAPI", "CD45", "CK19"])
    raw = str(tmp_path / "raw.ome.tiff")
    w = _worker_shell(monkeypatch, cp, raw, ["DAPI", "CD45", "CK19"], ["CD45", "CK19"],
                      method="cellpose_nuclei_csd")
    group = w._open_hq_channel_group()
    assert isinstance(group, dict) and group.get("kind") == "raw_ome"
    assert w._hq_resolved_source_path == os.path.abspath(raw)
