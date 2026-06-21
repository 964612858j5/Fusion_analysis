"""v14.5b: preview-time CalibrationSourceIdentity reader (pure, Qt-free).

Covers utils/calibration_source: SourceRequest vs CalibrationSourceIdentity,
Strategy B fallback, corrected identity grounded in the ACTUAL zarr array object,
and source_mixture_mode derived from actual identities.
"""

import numpy as np
import pytest

zarr = pytest.importorskip("zarr")

from block01.utils.calibration_source import (
    resolve_channel_calibration, source_mixture_mode_from_identities,
    open_corrected_channel_array, build_corrected_calibration_identity,
    REASON_USER_SELECTED, REASON_DEFAULT_RAW, REASON_CORRECTED_FALLBACK)
from block01.core.bg_correction import stamp_corrected_channel_identity


def _corrected_zarr(tmp_path, channel="CD68", shape=(80, 100), attr_shape=None,
                    method="tophat", channel_index=0):
    path = str(tmp_path / "corrected_channels.zarr")
    root = zarr.open_group(path, mode="w")
    g = root.create_group("ROI_1")
    g.attrs["bbox_fullres"] = [10, 90, 20, 120]
    ds = g.create_dataset(channel, data=np.ones(shape, dtype=np.float32))
    stamp_corrected_channel_identity(
        ds, channel, channel_index=channel_index, correction_method=method,
        roi_name="ROI_1", roi_bbox_fullres=[10, 90, 20, 120])
    if attr_shape is not None:                  # deliberately wrong attr shape
        ds.attrs["source_shape"] = list(attr_shape)
    return path


def _read_raw(ch):
    return np.ones((64, 64), dtype=np.float32)


# 2. Raw request -> raw identity.
def test_raw_request_raw_identity(tmp_path):
    req, csi = resolve_channel_calibration(
        "CD68", "raw_ome", read_raw=_read_raw, raw_path="/x.ome.tif",
        channel_index=0, patch_bbox=[0, 64, 0, 64],
        corrected_zarr_path=str(tmp_path / "missing.zarr"))
    assert req["requested_source"] == "raw_ome"
    assert req["reason"] == REASON_DEFAULT_RAW
    assert csi["actual_source_kind"] == "raw_ome"
    assert csi["source_shape"] == [64, 64]
    assert csi["dtype"] == "float32"
    assert csi["intensity_space"] == "raw_ome_native_float"
    assert csi["channel_index"] == 0


def test_raw_request_user_selected_reason(tmp_path):
    req, _ = resolve_channel_calibration(
        "CD68", "raw_ome", read_raw=_read_raw, raw_path="/x", channel_index=0,
        patch_bbox=None, corrected_zarr_path=None, user_selected=True)
    assert req["reason"] == REASON_USER_SELECTED


# 3. Corrected request + available channel -> corrected identity from actual array.
def test_corrected_request_available(tmp_path):
    cz = _corrected_zarr(tmp_path, shape=(80, 100), method="cucim", channel_index=3)
    req, csi = resolve_channel_calibration(
        "CD68", "corrected_zarr", read_raw=_read_raw, raw_path="/x.ome.tif",
        channel_index=99, patch_bbox=[0, 64, 0, 64], corrected_zarr_path=cz)
    assert req["requested_source"] == "corrected_zarr"
    assert req["reason"] == REASON_USER_SELECTED
    assert csi["actual_source_kind"] == "corrected_zarr"
    # grounded in the ACTUAL zarr array, not the raw read or the passed index
    assert csi["source_shape"] == [80, 100]
    assert csi["dtype"] == "float32"
    assert csi["intensity_space"] == "background_corrected_marker_image"
    assert csi["channel_index"] == 3            # from array attrs
    assert csi["correction_method"] == "cucim"
    assert csi["channel_key"] == "CD68"


# 4 + 5. Corrected request + missing channel -> Strategy B raw fallback (honest).
def test_corrected_request_missing_channel_falls_back_to_raw(tmp_path):
    cz = _corrected_zarr(tmp_path, channel="CD68")   # CK19 absent
    req, csi = resolve_channel_calibration(
        "CK19", "corrected_zarr", read_raw=_read_raw, raw_path="/x.ome.tif",
        channel_index=1, patch_bbox=[0, 64, 0, 64], corrected_zarr_path=cz)
    assert req["requested_source"] == "corrected_zarr"   # asked for corrected
    assert req["reason"] == REASON_CORRECTED_FALLBACK
    assert csi["actual_source_kind"] == "raw_ome"        # actually read raw
    assert csi["source_shape"] == [64, 64]               # from raw read
    # request and identity legitimately differ
    assert req["requested_source"] != csi["actual_source_kind"]


def test_corrected_request_missing_zarr_falls_back_to_raw(tmp_path):
    req, csi = resolve_channel_calibration(
        "CD68", "corrected_zarr", read_raw=_read_raw, raw_path="/x",
        channel_index=0, patch_bbox=None,
        corrected_zarr_path=str(tmp_path / "nope.zarr"))
    assert req["reason"] == REASON_CORRECTED_FALLBACK
    assert csi["actual_source_kind"] == "raw_ome"


# 6 + 7. source_mixture_mode derived from ACTUAL identities.
def test_mixture_mode_from_actual_identities(tmp_path):
    cz = _corrected_zarr(tmp_path, channel="CD68")
    # CD68 corrected available, CK19 requested corrected but missing -> raw fallback
    _, c1 = resolve_channel_calibration(
        "CD68", "corrected_zarr", read_raw=_read_raw, raw_path="/x",
        channel_index=0, patch_bbox=None, corrected_zarr_path=cz)
    _, c2 = resolve_channel_calibration(
        "CK19", "corrected_zarr", read_raw=_read_raw, raw_path="/x",
        channel_index=1, patch_bbox=None, corrected_zarr_path=cz)
    # all requested corrected, but partial fallback -> MIXED, not homogeneous_corrected
    assert source_mixture_mode_from_identities([c1, c2]) == "mixed_raw_corrected"
    assert source_mixture_mode_from_identities([c1]) == "homogeneous_corrected"
    assert source_mixture_mode_from_identities([c2]) == "homogeneous_raw"
    assert source_mixture_mode_from_identities([]) is None


# 8. Corrected shape/dtype grounded in array; wrong attr shape does not override.
def test_corrected_shape_grounded_in_array_not_attr(tmp_path):
    cz = _corrected_zarr(tmp_path, shape=(80, 100), attr_shape=[999, 999])
    array = open_corrected_channel_array(cz, "CD68", roi_name="ROI_1")
    assert array is not None
    csi = build_corrected_calibration_identity(array, cz, "CD68")
    assert csi["source_shape"] == [80, 100]      # actual array, NOT [999,999]
    assert csi["attr_source_shape"] == [999, 999]
    assert csi["attr_source_shape_mismatch"] is True
