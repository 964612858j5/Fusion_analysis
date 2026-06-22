"""v14.5c.2: resolve_per_channel_marker_sources (resolver only) tests.

Resolves each channel's REQUESTED source from real pixels into a
PerChannelResolvedSource. Corrected channels use a small real zarr (real array
shape); raw uses a fake loader returning a real shape. Proves: per-channel
resolution, corrected-unavailable hard fail with NO raw substitution, geometry
from real pixels, and INDEPENDENCE from the preview reader (calibration_source).
"""

import inspect
import os
import tempfile

import numpy as np
import pytest

zarr = pytest.importorskip("zarr")

from block01.workers.hq_source_resolver import (
    resolve_per_channel_marker_sources, PerChannelResolutionError,
    PerChannelResolvedSource)
from block01.core.bg_correction import stamp_corrected_channel_identity


class _FakeLoader:
    """Raw OME loader stub: whole-image shape + channel names."""
    def __init__(self, path, names=("CD68", "CK19", "DAPI"), shape=(200, 200)):
        self.shape = shape
        self._names = list(names)

    def channel_names(self):
        return list(self._names)


def _corrected_zarr(tmp_path, channels=("CD68",), shape=(80, 100), roi_id="roi_abc"):
    path = str(tmp_path / "corrected_channels.zarr")
    root = zarr.open_group(path, mode="w")
    root.attrs["mode"] = "roi_only"
    g = root.create_group("ROI_1")
    g.attrs["roi_id"] = roi_id
    g.attrs["roi_name"] = "ROI_1"
    g.attrs["bbox_fullres"] = [0, shape[0], 0, shape[1]]
    for i, ch in enumerate(channels):
        ds = g.create_dataset(ch, data=np.ones(shape, dtype=np.float32))
        stamp_corrected_channel_identity(
            ds, ch, channel_index=i, correction_method="tophat",
            roi_name="ROI_1", roi_bbox_fullres=[0, shape[0], 0, shape[1]])
    return path


# 1. All raw.
def test_all_raw(tmp_path):
    r = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome", "CK19": "raw_ome"},
        raw_channel_source_path="/x.ome.tif", loader_factory=_FakeLoader)
    assert isinstance(r, PerChannelResolvedSource)
    assert r.source_mixture_mode == "homogeneous_raw"
    for ch in ("CD68", "CK19"):
        assert r.per_channel[ch].kind == "raw_ome"
        assert r.per_channel[ch].channel_shape(ch) == (200, 200)   # real loader shape


# 2. All corrected (available).
def test_all_corrected_available(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68", "CK19"))
    r = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "corrected_zarr"},
        raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
        roi_id="roi_abc", loader_factory=_FakeLoader)
    assert r.source_mixture_mode == "homogeneous_corrected"
    for ch in ("CD68", "CK19"):
        assert r.per_channel[ch].kind == "corrected_zarr"
        assert r.per_channel[ch].channel_shape(ch) == (80, 100)   # real array shape


# 3. Mixed.
def test_mixed(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68",))
    r = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "raw_ome"},
        raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
        roi_id="roi_abc", loader_factory=_FakeLoader)
    assert r.source_mixture_mode == "mixed_raw_corrected"
    assert r.per_channel["CD68"].kind == "corrected_zarr"
    assert r.per_channel["CK19"].kind == "raw_ome"


# 4. Corrected requested, channel missing, no fallback -> raise, NO substitution.
def test_corrected_missing_channel_hard_fails_no_substitution(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68",))   # CK19 absent in corrected
    with pytest.raises(PerChannelResolutionError) as ei:
        resolve_per_channel_marker_sources(
            {"CK19": "corrected_zarr"},
            raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
            roi_id="roi_abc", loader_factory=_FakeLoader,
            allow_corrected_to_raw_fallback=False)
    assert ei.value.channel == "CK19"
    assert ei.value.requested_source == "corrected_zarr"


def test_corrected_zarr_missing_hard_fails(tmp_path):
    with pytest.raises(PerChannelResolutionError) as ei:
        resolve_per_channel_marker_sources(
            {"CD68": "corrected_zarr"},
            raw_channel_source_path="/x.ome.tif",
            corrected_zarr_path=str(tmp_path / "nope.zarr"),
            roi_id="roi_abc", loader_factory=_FakeLoader)
    assert ei.value.channel == "CD68"


# allow_corrected_to_raw_fallback=True (the shared-flag's other tolerance):
# corrected unavailable -> raw fallback, resolves (no raise). Proves one truth
# table, two tolerances.
def test_corrected_unavailable_with_fallback_resolves_raw(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68",))   # CK19 absent
    r = resolve_per_channel_marker_sources(
        {"CK19": "corrected_zarr"},
        raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
        roi_id="roi_abc", loader_factory=_FakeLoader,
        allow_corrected_to_raw_fallback=True)
    assert r.per_channel["CK19"].kind == "raw_ome"


# 5. resolved geometry from real pixels (corrected array shape != raw loader shape).
def test_resolved_geometry_from_real_pixels(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68",), shape=(80, 100))
    r = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "raw_ome"},
        raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
        roi_id="roi_abc", loader_factory=_FakeLoader)
    assert r.per_channel["CD68"].channel_shape("CD68") == (80, 100)   # corrected array
    assert r.per_channel["CK19"].channel_shape("CK19") == (200, 200)  # raw loader
    # not equal -> proves they come from different real sources, not a shared attr
    assert r.per_channel["CD68"].channel_shape("CD68") != r.per_channel["CK19"].channel_shape("CK19")


# 6. INDEPENDENCE: resolver does NOT call the preview reader (calibration_source).
def test_resolver_does_not_call_calibration_source(tmp_path, monkeypatch):
    import block01.utils.calibration_source as cs
    called = []
    for name in ("resolve_channel_calibration", "open_corrected_channel_array",
                 "build_corrected_calibration_identity", "build_raw_calibration_identity"):
        monkeypatch.setattr(
            cs, name,
            (lambda *a, _n=name, **k: called.append(_n)), raising=True)
    cz = _corrected_zarr(tmp_path, channels=("CD68",))
    resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr"},
        raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
        roi_id="roi_abc", loader_factory=_FakeLoader)
    assert called == [], f"resolver called preview reader: {called}"


# 7. Signature takes requests/handles, NOT a remap config / recorded identity.
def test_signature_takes_requests_not_config():
    sig = inspect.signature(resolve_per_channel_marker_sources)
    params = list(sig.parameters)
    assert params[0] == "per_channel_requests"
    blob = " ".join(params).lower()
    for forbidden in ("config", "remap", "recorded", "calibration_source_identity",
                      "step2_input_shape", "step2_ready"):
        assert forbidden not in blob, forbidden


# 8. No bare source_shape attribute; geometry via reused channel_shape.
def test_no_bare_source_shape(tmp_path):
    r = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/x.ome.tif",
        loader_factory=_FakeLoader)
    assert not hasattr(r, "source_shape")
    assert not hasattr(r.per_channel["CD68"], "source_shape")
    # geometry is available via the reused single-source accessor
    assert callable(r.per_channel["CD68"].channel_shape)


# 9. group/group_name carried per channel for 5c.1 identity homogeneity.
def test_group_name_carried(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68", "CK19"))
    r = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "corrected_zarr"},
        raw_channel_source_path="/x.ome.tif", corrected_zarr_path=cz,
        roi_id="roi_abc", loader_factory=_FakeLoader)
    # both corrected channels share one ROI group -> identity-homogeneous
    assert r.per_channel["CD68"].group_name == r.per_channel["CK19"].group_name
    assert r.is_homogeneous() is True
    single = r.homogeneous_single()
    assert single.kind == "corrected_zarr"
    # raw channels carry group_name "raw_ome"
    r2 = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/x.ome.tif",
        loader_factory=_FakeLoader)
    assert r2.per_channel["CD68"].group_name == "raw_ome"


# empty requests -> raise.
def test_empty_requests_raises():
    with pytest.raises(ValueError):
        resolve_per_channel_marker_sources({}, raw_channel_source_path="/x.ome.tif")
