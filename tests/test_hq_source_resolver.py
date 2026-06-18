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

def _worker_shell(monkeypatch, corrected_path, raw_path, raw_channels, hq_channels):
    """A SegmentMergeWorker built via __new__ with only the attrs _validate_hq_config
    touches, and OMETIFFLoader monkeypatched to the fake loader."""
    from block01.workers import segment_merge_worker as smw
    _FakeOMELoader.REGISTRY[raw_path] = list(raw_channels)
    monkeypatch.setattr(smw, "OMETIFFLoader", _FakeOMELoader)
    w = smw.SegmentMergeWorker.__new__(smw.SegmentMergeWorker)
    w.seg_config = {"hq_channels": list(hq_channels),
                    "hq_input_mode": "selected_channels_from_source"}
    w.method = "cellpose_nuclei_csd"
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
