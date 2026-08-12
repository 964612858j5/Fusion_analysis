"""v14.5d B3b-2 (marker-source prep stage): the worker re-resolves each marker,
cross-checks it EXACTLY against the stashed runtime descriptor, and only then commits
the per-channel handles. Any mismatch raises before output and leaves
_source_aware_per_channel / _channel_remap_params unset (no single-source fallback).

Offscreen via the unbound method on a stub + a monkeypatched per-channel resolver."""

import os
import types

import pytest

import block01.workers.hq_source_resolver as hqsr
from block01.workers.segment_merge_worker import SegmentMergeWorker


class _RS:
    def __init__(self, kind, path, group_name, shape):
        self.kind = kind
        self.source_path = path
        self.group_name = group_name
        self._shape = shape

    def channel_shape(self, _name):
        return self._shape


class _PC:
    def __init__(self, per, mixture):
        self.per_channel = per
        self.source_mixture_mode = mixture


def _descriptor(kind="corrected_zarr", path="/data/corrected.zarr",
                group="ROI_1", shape=(200, 200), channels=("CK19", "CD68")):
    def ch():
        return {"min": 0.0, "max": 1.0, "gamma": 1.0,
                "resolved_source_kind": kind, "resolved_source_path": path,
                "resolved_group_name": group, "resolved_source_shape": list(shape)}
    mixture = "homogeneous_corrected" if kind == "corrected_zarr" else "homogeneous_raw"
    return {"channels": {c: ch() for c in channels},
            "source_mixture_mode": mixture, "used_for": "segmentation_only"}


def _stub(descriptor, rois=None):
    s = types.SimpleNamespace(
        _pending_source_aware_runtime=descriptor,
        _source_aware_per_channel=None, _channel_remap_params=None,
        _manual_remap_enabled=False, _remap_provenance={},
        rois=rois or [], roi_id="", seg_config={"roi_id": ""}, param_file="",
        _raw_channel_source_path=lambda: "/data/raw.ome.tif",
        _multichannel_source_path=lambda: "/data/corrected.zarr",
        _requested_roi_names=lambda: set(), _abs=os.path.abspath,
        _write_remap_provenance=lambda cfg: None)
    return s


def _patch_resolver(monkeypatch, fn):
    monkeypatch.setattr(hqsr, "resolve_per_channel_marker_sources", fn)


def _prep(stub, step2=(200, 200)):
    SegmentMergeWorker._prepare_source_aware_runtime(stub, step2)


def _assert_unset(stub):
    assert stub._source_aware_per_channel is None
    assert stub._channel_remap_params is None


# ── success (raw + corrected homogeneous) ────────────────────────────────────

def test_prepare_success_corrected(monkeypatch):
    d = _descriptor(kind="corrected_zarr")
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("corrected_zarr", "/data/corrected.zarr", "ROI_1", (200, 200))
         for c in requests}, "homogeneous_corrected"))
    s = _stub(d)
    _prep(s)
    assert s._source_aware_per_channel is not None
    assert set(s._channel_remap_params) == {"CK19", "CD68"}
    assert s._manual_remap_enabled is True


def test_prepare_success_raw(monkeypatch):
    d = _descriptor(kind="raw_ome", path="/data/raw.ome.tif", group="raw_ome")
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("raw_ome", "/data/raw.ome.tif", "raw_ome", (200, 200))
         for c in requests}, "homogeneous_raw"))
    s = _stub(d)
    _prep(s)
    assert set(s._channel_remap_params) == {"CK19", "CD68"}


# ── hard failures -> attrs stay unset ────────────────────────────────────────

def test_prepare_rejects_roi_run(monkeypatch):
    _patch_resolver(monkeypatch, lambda *a, **k: pytest.fail("resolver must not run"))
    s = _stub(_descriptor(), rois=[{"name": "r1", "bbox_fullres": [0, 100, 0, 100]}])
    with pytest.raises(ValueError, match="full-image only"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_marker_set_mismatch(monkeypatch):
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {"CK19": _RS("corrected_zarr", "/data/corrected.zarr", "ROI_1", (200, 200))},
        "homogeneous_corrected"))                       # missing CD68
    s = _stub(_descriptor())
    with pytest.raises(ValueError, match="marker-set mismatch"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_kind_mismatch(monkeypatch):
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("raw_ome", "/data/corrected.zarr", "ROI_1", (200, 200)) for c in requests},
        "homogeneous_corrected"))
    s = _stub(_descriptor(kind="corrected_zarr"))
    with pytest.raises(ValueError, match="kind"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_path_mismatch(monkeypatch):
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("corrected_zarr", "/data/OTHER.zarr", "ROI_1", (200, 200)) for c in requests},
        "homogeneous_corrected"))
    s = _stub(_descriptor())
    with pytest.raises(ValueError, match="source_path"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_group_mismatch(monkeypatch):
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("corrected_zarr", "/data/corrected.zarr", "ROI_2", (200, 200)) for c in requests},
        "homogeneous_corrected"))
    s = _stub(_descriptor(group="ROI_1"))
    with pytest.raises(ValueError, match="group_name"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_shape_mismatch(monkeypatch):
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("corrected_zarr", "/data/corrected.zarr", "ROI_1", (100, 100)) for c in requests},
        "homogeneous_corrected"))
    s = _stub(_descriptor(shape=(200, 200)))
    with pytest.raises(ValueError, match="shape"):
        _prep(s)                                          # descriptor 200 != resolved 100
    _assert_unset(s)


def test_prepare_rejects_step2_input_shape_mismatch(monkeypatch):
    # resolved == descriptor (200) but the REAL Step2 input grid is 100 -> refuse
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("corrected_zarr", "/data/corrected.zarr", "ROI_1", (200, 200)) for c in requests},
        "homogeneous_corrected"))
    s = _stub(_descriptor(shape=(200, 200)))
    with pytest.raises(ValueError, match="frame mismatch"):
        _prep(s, step2=(100, 100))
    _assert_unset(s)


def test_prepare_rejects_unknown_step2_geometry(monkeypatch):
    _patch_resolver(monkeypatch, lambda *a, **k: pytest.fail("resolver must not run"))
    s = _stub(_descriptor())
    with pytest.raises(ValueError, match="unknown Step2 input geometry"):
        _prep(s, step2=None)
    _assert_unset(s)


def test_prepare_rejects_missing_descriptor_shape(monkeypatch):
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("corrected_zarr", "/data/corrected.zarr", "ROI_1", (200, 200)) for c in requests},
        "homogeneous_corrected"))
    d = _descriptor()
    for c in d["channels"].values():
        c["resolved_source_shape"] = None
    s = _stub(d)
    with pytest.raises(ValueError, match="resolved_source_shape"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_raw_group_not_raw_ome(monkeypatch):
    # raw marker but descriptor group name is not 'raw_ome'
    _patch_resolver(monkeypatch, lambda requests, **k: _PC(
        {c: _RS("raw_ome", "/data/raw.ome.tif", "raw_ome", (200, 200)) for c in requests},
        "homogeneous_raw"))
    s = _stub(_descriptor(kind="raw_ome", path="/data/raw.ome.tif", group="ROI_1"))
    with pytest.raises(ValueError, match="raw_ome"):
        _prep(s)
    _assert_unset(s)


def test_prepare_rejects_resolution_error(monkeypatch):
    def _boom(requests, **k):
        raise hqsr.PerChannelResolutionError(
            channel="CD68", requested_source="corrected_zarr", reason="not found")
    _patch_resolver(monkeypatch, _boom)
    s = _stub(_descriptor())
    with pytest.raises(ValueError, match="could not re-resolve"):
        _prep(s)
    _assert_unset(s)


def test_prepare_noop_without_descriptor():
    s = _stub(None)
    _prep(s)                                             # no descriptor -> no-op
    _assert_unset(s)
