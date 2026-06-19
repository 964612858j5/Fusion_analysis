"""Tests for utils/remap_promotion.py + scripts/promote_remap_config.py (2.1c-b).

Part B: source-identity promotion of Step1.5 preview configs. Promotion calls the
2.1c-a shared resolver (resolve_hq_marker_source) to build the ResolvedHQSource;
these tests construct real ResolvedHQSource objects via that resolver with a fake
OME loader / tiny zarr, never a reimplementation.
"""

import json
import os

import numpy as np
import pytest
import zarr

from block01.utils.remap_promotion import (
    promote_step1_5_config_for_step2,
    PROMOTION_CREATED_FROM,
    NON_HQ2_CSD_REFUSAL,
)
from block01.utils.channel_remap_config import validate_step2_remap_config
from block01.workers.hq_source_resolver import (
    resolve_hq_marker_source, SOURCE_MODE_RAW_ONLY)


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeOME:
    REG = {}      # path -> channel names
    SHAPES = {}   # path -> (H, W)

    def __init__(self, path, *a, **k):
        self.path = path
        self._names = list(self.REG.get(path, []))
        self.shape = self.SHAPES.get(path, (16, 24))

    def channel_names(self):
        return list(self._names)


def _raw_resolved(raw_path, channels, shape=(16, 24)):
    _FakeOME.REG[raw_path] = list(channels)
    _FakeOME.SHAPES[raw_path] = shape
    return resolve_hq_marker_source(
        requested_channels=list(channels),
        multichannel_source_path="", raw_channel_source_path=raw_path,
        roi_id="", requested_roi_names=set(),
        abs_fn=os.path.abspath, loader_factory=_FakeOME)


def _corrected_resolved(tmp_path, channels, shape=(16, 24), attrs=None):
    cp = str(tmp_path / "corrected.zarr")
    root = zarr.open(cp, mode="w")
    for name in channels:
        root.create_dataset(name, data=np.zeros(shape, dtype=np.float32))
    for k, v in (attrs or {}).items():
        root.attrs[k] = v
    return resolve_hq_marker_source(
        requested_channels=list(channels),
        multichannel_source_path=cp, raw_channel_source_path="",
        roi_id="", requested_roi_names=set(),
        abs_fn=os.path.abspath, loader_factory=_FakeOME)


def _preview(cal_path, cal_shape, channels=("CD45", "CK19"),
             cal_kind="raw_ome", cal_ispace="raw_ome_native_float",
             bbox=None, idx=0, mn=10.0, mx=200.0, gamma=0.9):
    bbox = [0, 32, 0, 32] if bbox is None else bbox
    chans = {}
    for c in channels:
        chans[c] = {
            "enabled": True, "min": mn, "max": mx,
            "brightness": 0.1, "contrast": 1.2, "gamma": gamma,
            "intensity_space": cal_ispace, "step2_compatible": False,
            "step2_pre_remap_source": "unknown",
            "calibration_source_matches_step2": False,
        }
    return {
        "version": "v13.1", "mode": "manual_remap", "auto_saturation": 0.1,
        "used_for": "segmentation_only", "channels": chans,
        "source_policy": {
            "source": "step1_5_loader", "intensity_space": cal_ispace,
            "normalization": "none", "scope": "step1_5_pre_segmentation",
            "preview_only": True, "step2_ready": False,
            "step2_pre_remap_source": "unknown",
            "calibration_source_matches_step2": False,
            "source_alignment_mode": "partial_or_preview_fallback",
            "calibration_source_path": cal_path,
            "calibration_source_kind": cal_kind,
            "calibration_source_shape": cal_shape,
            "calibration_intensity_space": cal_ispace,
            "calibration_patch_bbox": bbox, "calibration_patch_index": idx,
        },
    }


# ── success ──────────────────────────────────────────────────────────────────

def test_aligned_preview_promotes(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["DAPI", "CD45", "CK19"], shape=(16, 24))
    preview = _preview(raw, [16, 24])
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert report["promoted"] is True
    assert promoted is not None
    sp = promoted["source_policy"]
    assert sp["preview_only"] is False
    assert sp["step2_ready"] is True
    assert sp["calibration_source_matches_step2"] is True
    assert sp["step2_ready_scope"] == "source_identity_only"
    assert sp["representativeness_validated"] is False
    assert promoted["created_from_step"] == PROMOTION_CREATED_FROM


def test_promoted_passes_step2_validation_without_preview_flag(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    promoted, _ = promote_step1_5_config_for_step2(_preview(raw, [16, 24]), res, [16, 24])
    errors, resolved = validate_step2_remap_config(promoted, allow_preview_remap=False)
    assert errors == []
    assert set(resolved) == {"CD45", "CK19"}


def test_minmax_gamma_preserved_byte_for_byte(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24], mn=12.5, mx=233.0, gamma=0.77)
    promoted, _ = promote_step1_5_config_for_step2(preview, res, [16, 24])
    for c in ("CD45", "CK19"):
        assert promoted["channels"][c]["min"] == 12.5
        assert promoted["channels"][c]["max"] == 233.0
        assert promoted["channels"][c]["gamma"] == 0.77
        assert promoted["channels"][c]["brightness"] == 0.1
        assert promoted["channels"][c]["contrast"] == 1.2


def test_audit_trace_block_present(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24], bbox=[0, 32, 64, 96], idx=3)
    promoted, _ = promote_step1_5_config_for_step2(preview, res, [16, 24])
    audit = promoted["calibration_source"]
    assert audit["source_path"] == raw
    assert audit["source_kind"] == "raw_ome"
    assert audit["source_shape"] == [16, 24]
    assert audit["intensity_space"] == "raw_ome_native_float"
    assert audit["patch_bbox"] == [0, 32, 64, 96]
    assert audit["patch_index"] == 3


def test_patch_bbox_mismatch_does_not_block(tmp_path):
    # patch bbox is record-only; an "off" bbox must not block promotion.
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24], bbox=[999, 1000, 999, 1000])
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert report["promoted"] is True and promoted is not None


# ── canonical path / [H, W] convention ──────────────────────────────────────

def test_canonical_path_symlink_matches(tmp_path):
    real = tmp_path / "real_raw.ome.tiff"
    real.write_text("x")  # real file so realpath resolves the symlink
    link = tmp_path / "link_raw.ome.tiff"
    os.symlink(str(real), str(link))
    res = _raw_resolved(str(real), ["CD45", "CK19"])
    # calibration recorded via the symlink; must still match the real resolved path
    preview = _preview(str(link), [16, 24])
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert report["promoted"] is True and promoted is not None


def test_hw_convention_transposed_is_real_mismatch(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"], shape=(16, 24))
    # both calibration and resolved follow [H, W]; a transposed [W, H] is a real mismatch
    assert res.channel_shape("CD45") == (16, 24)
    preview_ok = _preview(raw, [16, 24])
    assert promote_step1_5_config_for_step2(preview_ok, res, [16, 24])[1]["promoted"] is True
    preview_t = _preview(raw, [24, 16])  # transposed
    promoted_t, report_t = promote_step1_5_config_for_step2(preview_t, res, [16, 24])
    assert promoted_t is None
    assert any("calibration_source_shape" in f for f in report_t["failures"])


# ── refusals ─────────────────────────────────────────────────────────────────

def test_refuse_missing_calibration_path(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24])
    preview["source_policy"]["calibration_source_path"] = None
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert promoted is None
    assert any("calibration_source_path" in f for f in report["failures"])


def test_refuse_missing_calibration_shape(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, None)
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert promoted is None
    assert any("calibration_source_shape" in f for f in report["failures"])


def test_refuse_channel_absent_from_resolved(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["DAPI", "CD45"])  # no CK19
    preview = _preview(raw, [16, 24], channels=("CD45", "CK19"))
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert promoted is None
    assert any("CK19" in f and "not present" in f for f in report["failures"])


def test_refuse_intensity_space_mismatch_raw_cal_vs_corrected_resolved(tmp_path):
    # Step1.5 calibrated raw_ome; Step2 resolves corrected_zarr (intensity unknown).
    res = _corrected_resolved(tmp_path, ["CD45", "CK19"], shape=(16, 24))
    assert res.kind == "corrected_zarr"
    preview = _preview(str(tmp_path / "raw.ome.tiff"), [16, 24])  # raw_ome calibration
    promoted, report = promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert promoted is None
    assert any("intensity_space" in f for f in report["failures"])


def test_refuse_geometry_guard_marker_vs_step2_input(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"], shape=(16, 24))
    preview = _preview(raw, [16, 24])
    # marker source is (16,24) but Step2 input geometry is (32,24) -> guard fails
    promoted, report = promote_step1_5_config_for_step2(preview, res, [32, 24])
    assert promoted is None
    assert any("geometry guard" in f for f in report["failures"])


def test_refuse_when_step2_input_shape_unknown(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24])
    promoted, report = promote_step1_5_config_for_step2(preview, res, None)
    assert promoted is None
    assert any("step2_input_shape is unknown" in f for f in report["failures"])


def test_preview_config_not_mutated(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24])
    before = json.dumps(preview, sort_keys=True)
    promote_step1_5_config_for_step2(preview, res, [16, 24])
    assert json.dumps(preview, sort_keys=True) == before


# ── CLI: parser reuse + geometry derivation + end-to-end ────────────────────

def test_cli_requested_channels_uses_parse_hq_channels():
    from block01.scripts.promote_remap_config import requested_channels_from_seg
    from block01.workers.hq_marker_segmentation import parse_hq_channels
    seg = {"hq_channels": "CD45, CK19; CD68"}
    assert requested_channels_from_seg(seg) == parse_hq_channels(seg["hq_channels"])


def test_cli_derive_step2_input_shape_none_when_absent(tmp_path):
    from block01.scripts.promote_remap_config import derive_step2_input_shape
    assert derive_step2_input_shape({}) is None


def test_cli_derive_step2_input_shape_explicit():
    from block01.scripts.promote_remap_config import derive_step2_input_shape
    assert derive_step2_input_shape({"step2_input_shape": [16, 24]}) == [16, 24]


def test_cli_end_to_end_promotes(tmp_path, monkeypatch):
    import block01.core.io_loader as io_loader
    from block01.scripts import promote_remap_config as cli
    raw = str(tmp_path / "raw.ome.tiff")
    _FakeOME.REG[raw] = ["DAPI", "CD45", "CK19"]
    _FakeOME.SHAPES[raw] = (16, 24)
    monkeypatch.setattr(io_loader, "OMETIFFLoader", _FakeOME)
    seg = {"method": "cellpose_nuclei_csd", "hq_channels": ["CD45", "CK19"],
           "raw_ome_path": raw, "step2_input_shape": [16, 24]}
    seg_path = tmp_path / "seg.json"
    seg_path.write_text(json.dumps(seg))
    prev_path = tmp_path / "step1_5_channel_remap_x.json"
    prev_path.write_text(json.dumps(_preview(raw, [16, 24])))
    rc = cli.main(["--seg-params", str(seg_path), "--preview-config", str(prev_path),
                   "--out-dir", str(tmp_path)])
    assert rc == 0
    out = tmp_path / "step1_5_channel_remap_x_step2ready.json"
    assert out.exists()
    promoted = json.loads(out.read_text())
    assert promoted["source_policy"]["step2_ready"] is True
    assert not (tmp_path / "promotion_report.json").exists()


def test_cli_end_to_end_refuses_without_geometry(tmp_path, monkeypatch):
    import block01.core.io_loader as io_loader
    from block01.scripts import promote_remap_config as cli
    raw = str(tmp_path / "raw.ome.tiff")
    _FakeOME.REG[raw] = ["DAPI", "CD45", "CK19"]
    _FakeOME.SHAPES[raw] = (16, 24)
    monkeypatch.setattr(io_loader, "OMETIFFLoader", _FakeOME)
    seg = {"method": "cellpose_nuclei_csd", "hq_channels": ["CD45", "CK19"],
           "raw_ome_path": raw}  # no step2_input_shape -> geometry refusal
    seg_path = tmp_path / "seg.json"
    seg_path.write_text(json.dumps(seg))
    prev_path = tmp_path / "prev.json"
    prev_path.write_text(json.dumps(_preview(raw, [16, 24])))
    rc = cli.main(["--seg-params", str(seg_path), "--preview-config", str(prev_path),
                   "--out-dir", str(tmp_path)])
    assert rc == 2
    assert (tmp_path / "promotion_report.json").exists()


# ── 2.1c-b.1: algorithm-derived source policy in promotion ───────────────────

def _raw_only_resolved_with_complete_corrected(tmp_path, raw_path, channels,
                                               shape=(16, 24)):
    """Resolve under raw_ome_only WHILE a complete corrected_zarr also exists.

    Proves the structural guarantee: a complete corrected_zarr does NOT divert an
    HQ2/CSD (raw_ome_only) resolution away from raw OME.
    """
    import zarr
    cp = str(tmp_path / "corrected.zarr")
    root = zarr.open(cp, mode="w")
    for name in channels:
        root.create_dataset(name, data=np.zeros(shape, dtype=np.float32))
    _FakeOME.REG[raw_path] = list(channels)
    _FakeOME.SHAPES[raw_path] = shape
    return resolve_hq_marker_source(
        requested_channels=list(channels),
        multichannel_source_path=cp,           # complete corrected present
        raw_channel_source_path=raw_path,
        roi_id="", requested_roi_names=set(),
        abs_fn=os.path.abspath, loader_factory=_FakeOME,
        source_mode=SOURCE_MODE_RAW_ONLY)


def test_promotes_hq2_csd_even_with_complete_corrected(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_only_resolved_with_complete_corrected(tmp_path, raw, ["DAPI", "CD45", "CK19"])
    assert res.kind == "raw_ome" and res.fell_back_to_raw is False  # raw by policy
    preview = _preview(raw, [16, 24])
    promoted, report = promote_step1_5_config_for_step2(
        preview, res, [16, 24], active_method="cellpose_nuclei_csd")
    assert report["promoted"] is True and promoted is not None
    sp = promoted["source_policy"]
    assert sp["step2_ready_scope"] == "source_identity_only"
    assert sp["representativeness_validated"] is False
    errors, resolved = validate_step2_remap_config(promoted, allow_preview_remap=False)
    assert errors == []
    assert set(resolved) == {"CD45", "CK19"}


def test_refuse_non_hq2csd_method(tmp_path):
    raw = str(tmp_path / "raw.ome.tiff")
    res = _raw_resolved(raw, ["CD45", "CK19"])
    preview = _preview(raw, [16, 24])
    promoted, report = promote_step1_5_config_for_step2(
        preview, res, [16, 24], active_method="cellpose_nuclei_dapi")
    assert promoted is None
    assert NON_HQ2_CSD_REFUSAL in report["failures"]


def test_refuse_non_hq2csd_method_short_circuits_without_resolved(tmp_path):
    # active_method gate fires before any source check; resolved_source may be None.
    preview = _preview(str(tmp_path / "raw.ome.tiff"), [16, 24])
    promoted, report = promote_step1_5_config_for_step2(
        preview, None, [16, 24], active_method="mesmer_whole_cell")
    assert promoted is None
    assert NON_HQ2_CSD_REFUSAL in report["failures"]


def test_cli_end_to_end_refuses_non_hq2csd(tmp_path, monkeypatch):
    import block01.core.io_loader as io_loader
    from block01.scripts import promote_remap_config as cli
    raw = str(tmp_path / "raw.ome.tiff")
    _FakeOME.REG[raw] = ["DAPI", "CD45", "CK19"]
    _FakeOME.SHAPES[raw] = (16, 24)
    monkeypatch.setattr(io_loader, "OMETIFFLoader", _FakeOME)
    seg = {"method": "cellpose_nuclei_dapi", "hq_channels": ["CD45", "CK19"],
           "raw_ome_path": raw, "step2_input_shape": [16, 24]}
    seg_path = tmp_path / "seg.json"; seg_path.write_text(json.dumps(seg))
    prev = tmp_path / "prev.json"; prev.write_text(json.dumps(_preview(raw, [16, 24])))
    rc = cli.main(["--seg-params", str(seg_path), "--preview-config", str(prev),
                   "--out-dir", str(tmp_path)])
    assert rc == 2
    report = json.loads((tmp_path / "promotion_report.json").read_text())
    assert NON_HQ2_CSD_REFUSAL in report["failures"]


def test_cli_end_to_end_promotes_hq2_csd(tmp_path, monkeypatch):
    import block01.core.io_loader as io_loader
    from block01.scripts import promote_remap_config as cli
    raw = str(tmp_path / "raw.ome.tiff")
    _FakeOME.REG[raw] = ["DAPI", "CD45", "CK19"]
    _FakeOME.SHAPES[raw] = (16, 24)
    monkeypatch.setattr(io_loader, "OMETIFFLoader", _FakeOME)
    # complete corrected also present; CSD must still resolve raw and promote
    import zarr
    cp = str(tmp_path / "corrected.zarr")
    root = zarr.open(cp, mode="w")
    for n in ["DAPI", "CD45", "CK19"]:
        root.create_dataset(n, data=np.zeros((16, 24), dtype=np.float32))
    seg = {"method": "cellpose_nuclei_csd", "hq_channels": ["CD45", "CK19"],
           "raw_ome_path": raw, "multichannel_source_path": cp,
           "step2_input_shape": [16, 24]}
    seg_path = tmp_path / "seg.json"; seg_path.write_text(json.dumps(seg))
    prev = tmp_path / "step1_5_x.json"; prev.write_text(json.dumps(_preview(raw, [16, 24])))
    rc = cli.main(["--seg-params", str(seg_path), "--preview-config", str(prev),
                   "--out-dir", str(tmp_path)])
    assert rc == 0
    out = tmp_path / "step1_5_x_step2ready.json"
    assert out.exists()
    promoted = json.loads(out.read_text())
    assert promoted["source_policy"]["source_path"] == os.path.abspath(raw)
