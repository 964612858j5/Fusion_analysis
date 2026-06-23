"""v14.5c.3: source-aware per-channel promotion tests.

Two axes, never conflated:
  IDENTITY (recorded↔resolved): kind/path/intensity_space/channel_index/ROI-group.
  GEOMETRY (resolved↔runtime):  resolved channel_shape == step2_input_shape.
The recorded patch source_shape is a calibration window, NEVER a geometry gate
(the Q3 trap). Output is a CANDIDATE (step2_ready=false; runtime is v14.5d).
"""

import copy
import os

import numpy as np
import pytest

zarr = pytest.importorskip("zarr")

from block01.workers.hq_source_resolver import (
    resolve_per_channel_marker_sources, PerChannelResolvedSource)
from block01.core.bg_correction import stamp_corrected_channel_identity
from block01.utils.remap_promotion import (
    promote_source_aware_config_for_step2, promote_source_aware_from_sources,
    _resolved_channel_identity)
from block01.utils.channel_remap_config import (
    validate_source_aware_promoted_candidate, validate_step2_remap_config)

_CSD = "cellpose_nuclei_csd"


class _FakeLoader:
    def __init__(self, path, names=("CD68", "CK19", "DAPI"), shape=(200, 200)):
        self.shape = shape
        self.filepath = os.path.abspath("/data/raw.ome.tif")
        self.ch_map = {n: i for i, n in enumerate(names)}
        self._names = list(names)

    def channel_names(self):
        return list(self._names)


def _csi(kind, path, idx, shape, ispace, roi=None):
    d = {"actual_source_kind": kind, "actual_source_path": os.path.abspath(path),
         "channel_name": "x", "channel_index": idx, "source_shape": shape,
         "dtype": "float32", "intensity_space": ispace}
    if roi:
        d["roi_name"] = roi
    return d


def _raw_channel_cfg(ch="CD68", idx=0, patch_shape=(64, 64)):
    return {"min": 0.0, "max": 1.0, "gamma": 1.3,
            "calibration_source_identity": _csi(
                "raw_ome", "/data/raw.ome.tif", idx, list(patch_shape),
                "raw_ome_native_float"),
            "source_request": {"requested_source": "raw_ome", "channel_name": ch}}


def _raw_preview(channels=("CD68", "CK19")):
    return {
        "channels": {c: _raw_channel_cfg(c, i) for i, c in enumerate(channels)},
        "source_policy": {"preview_only": True, "step2_ready": False},
        "source_mixture_mode": "homogeneous_raw",
    }


def _corrected_zarr(tmp_path, channels=("CD68",), shape=(80, 100), roi_id="r1"):
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


def _corrected_channel_cfg(ch, idx, cz, shape=(80, 100)):
    return {"min": 0.0, "max": 1.0, "gamma": 1.1,
            "calibration_source_identity": _csi(
                "corrected_zarr", cz, idx, list(shape),
                "background_corrected_marker_image", roi="ROI_1"),
            "source_request": {"requested_source": "corrected_zarr",
                               "channel_name": ch}}


# ── 1. Homogeneous raw -> candidate ──────────────────────────────────────────
def test_homogeneous_raw_promotes_to_candidate():
    cfg = _raw_preview(("CD68", "CK19"))
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome", "CK19": "raw_ome"},
        raw_channel_source_path="/data/raw.ome.tif", loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is not None, rep["failures"]
    sp = prom["source_policy"]
    assert sp["source_alignment_mode"] == "per_channel_native"
    assert prom["source_mixture_mode"] == "homogeneous_raw"
    assert sp["step2_ready"] is False
    assert prom["source_aware_promotion_ready"] is True
    assert prom["created_by_source_aware_promotion"] is True
    assert prom["runtime_supported"] is False
    # remap params byte-identical
    for ch in ("CD68", "CK19"):
        assert prom["channels"][ch]["min"] == 0.0
        assert prom["channels"][ch]["max"] == 1.0
        assert prom["channels"][ch]["gamma"] == 1.3
    assert validate_source_aware_promoted_candidate(prom) == []


# ── 2. Homogeneous corrected -> candidate ────────────────────────────────────
def test_homogeneous_corrected_promotes(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68", "CK19"))
    cfg = {"channels": {"CD68": _corrected_channel_cfg("CD68", 0, cz),
                        "CK19": _corrected_channel_cfg("CK19", 1, cz)},
           "source_policy": {"preview_only": True, "step2_ready": False},
           "source_mixture_mode": "homogeneous_corrected"}
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "corrected_zarr"},
        raw_channel_source_path="/data/raw.ome.tif", corrected_zarr_path=cz,
        roi_id="r1", loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [80, 100], active_method=_CSD)
    assert prom is not None, rep["failures"]
    assert prom["source_policy"]["source_alignment_mode"] == "per_channel_native"
    assert prom["source_mixture_mode"] == "homogeneous_corrected"
    assert prom["source_policy"]["step2_ready"] is False


# ── 3. Mixed -> two axes are SEPARATE ────────────────────────────────────────
def test_mixed_axes_are_separate(tmp_path):
    # corrected CD68 (80x100) + raw CK19 must share the SAME step2_input frame;
    # use a corrected array sized to the raw whole-image so geometry is consistent.
    cz = _corrected_zarr(tmp_path, channels=("CD68",), shape=(200, 200))
    cfg = {"channels": {"CD68": _corrected_channel_cfg("CD68", 0, cz, shape=(200, 200)),
                        "CK19": _raw_channel_cfg("CK19", 1)},
           "source_policy": {"preview_only": True, "step2_ready": False},
           "source_mixture_mode": "mixed_raw_corrected"}
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "raw_ome"},
        raw_channel_source_path="/data/raw.ome.tif", corrected_zarr_path=cz,
        roi_id="r1", loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is not None, rep["failures"]
    # alignment axis = per_channel_native; mixture axis = mixed_raw_corrected
    assert prom["source_policy"]["source_alignment_mode"] == "per_channel_native"
    assert prom["source_mixture_mode"] == "mixed_raw_corrected"
    assert prom["source_policy"]["source_alignment_mode"] != prom["source_mixture_mode"]


# ── 4. Q3 TRAP: raw patch [64,64] != resolved/step2 [200,200] -> PROMOTES ────
def test_q3_trap_patch_shape_is_not_a_geometry_gate():
    cfg = _raw_preview(("CD68",))
    assert cfg["channels"]["CD68"]["calibration_source_identity"]["source_shape"] == [64, 64]
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    assert pcr.per_channel["CD68"].channel_shape("CD68") == (200, 200)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is not None, rep["failures"]   # [64,64] vs [200,200] must NOT fail


# ── 5. recorded=corrected unresolvable -> REFUSE, no substitution ────────────
def test_corrected_unresolvable_refuses_no_substitution(tmp_path):
    cz = _corrected_zarr(tmp_path, channels=("CD68",))   # CK19 absent in corrected
    cfg = {"channels": {"CK19": _corrected_channel_cfg("CK19", 1, cz)},
           "source_policy": {"preview_only": True, "step2_ready": False},
           "source_mixture_mode": "homogeneous_corrected"}
    prom, rep = promote_source_aware_from_sources(
        cfg, step2_input_shape=[80, 100], raw_channel_source_path="/data/raw.ome.tif",
        corrected_zarr_path=cz, roi_id="r1", loader_factory=_FakeLoader,
        active_method=_CSD)
    assert prom is None
    assert any("CK19" in f for f in rep["failures"])


# ── 6. recorded=raw promotes as raw (not a substitution failure) ─────────────
def test_recorded_raw_promotes_as_raw():
    cfg = _raw_preview(("CD68",))
    prom, rep = promote_source_aware_from_sources(
        cfg, step2_input_shape=[200, 200], raw_channel_source_path="/data/raw.ome.tif",
        corrected_zarr_path="", loader_factory=_FakeLoader, active_method=_CSD)
    assert prom is not None, rep["failures"]
    assert prom["channels"]["CD68"]["resolved_source_kind"] == "raw_ome"


# ── 7. geometry-guard refusal rows ───────────────────────────────────────────
def test_7a_resolved_shape_ne_step2_input_refuses():
    cfg = _raw_preview(("CD68",))
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [100, 100], active_method=_CSD)
    assert prom is None
    assert any("geometry guard" in f for f in rep["failures"])


def test_7b_cross_channel_differing_shapes_refuse():
    # CD68 loader 200x200, CK19 loader 128x128 -> cannot both equal step2_input
    class _L1(_FakeLoader):
        def __init__(self, p): super().__init__(p); self.shape = (200, 200)
    cfg = _raw_preview(("CD68", "CK19"))
    # resolve each channel against a different-shaped loader by separate calls,
    # then assemble a heterogeneous-shape PerChannelResolvedSource.
    a = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/a.ome.tif",
        loader_factory=lambda p: _FakeLoader(p))  # 200x200
    b = resolve_per_channel_marker_sources(
        {"CK19": "raw_ome"}, raw_channel_source_path="/b.ome.tif",
        loader_factory=lambda p: _FakeLoader(p, shape=(128, 128)))
    pcr = PerChannelResolvedSource(
        {"CD68": a.per_channel["CD68"], "CK19": b.per_channel["CK19"]})
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is None
    assert any("geometry guard" in f for f in rep["failures"])  # CK19 128!=200


def test_7c_missing_recorded_identity_refuses():
    cfg = _raw_preview(("CD68", "CK19"))
    del cfg["channels"]["CK19"]["calibration_source_identity"]  # no-partial
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome", "CK19": "raw_ome"},
        raw_channel_source_path="/data/raw.ome.tif", loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is None
    assert any("CK19" in f and "identity" in f for f in rep["failures"])


def test_7d_corrected_different_roi_groups_refuse(tmp_path):
    # two corrected channels resolving to DIFFERENT group_name -> refuse
    cz = _corrected_zarr(tmp_path, channels=("CD68",), shape=(200, 200))
    a = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr"}, raw_channel_source_path="/data/raw.ome.tif",
        corrected_zarr_path=cz, roi_id="r1", loader_factory=_FakeLoader)
    cd68 = a.per_channel["CD68"]
    # synthesize a second corrected channel with a DIFFERENT group_name
    import copy as _c
    ck19 = _c.copy(cd68)
    ck19.group_name = "/ROI_2"
    cfg = {"channels": {"CD68": _corrected_channel_cfg("CD68", 0, cz, shape=(200, 200)),
                        "CK19": _corrected_channel_cfg("CK19", 0, cz, shape=(200, 200))},
           "source_policy": {"preview_only": True, "step2_ready": False},
           "source_mixture_mode": "homogeneous_corrected"}
    pcr = PerChannelResolvedSource({"CD68": cd68, "CK19": ck19})
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is None
    assert any("different ROI groups" in f for f in rep["failures"])


def test_7e_unknown_step2_input_shape_refuses():
    cfg = _raw_preview(("CD68",))
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, None, active_method=_CSD)
    assert prom is None
    assert any("step2_input_shape is unknown" in f for f in rep["failures"])


def test_7f_full_wsi_raw_plus_roi_corrected_refuses(tmp_path):
    # raw resolves to whole-image (200x200); corrected resolves to ROI crop
    # (80x100). They cannot both equal step2_input_shape -> refuse.
    cz = _corrected_zarr(tmp_path, channels=("CD68",), shape=(80, 100))
    cfg = {"channels": {"CD68": _corrected_channel_cfg("CD68", 0, cz, shape=(80, 100)),
                        "CK19": _raw_channel_cfg("CK19", 1)},
           "source_policy": {"preview_only": True, "step2_ready": False},
           "source_mixture_mode": "mixed_raw_corrected"}
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "corrected_zarr", "CK19": "raw_ome"},
        raw_channel_source_path="/data/raw.ome.tif", corrected_zarr_path=cz,
        roi_id="r1", loader_factory=_FakeLoader)
    # CD68 corrected = (80,100), CK19 raw = (200,200): no single step2_input matches both
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [80, 100], active_method=_CSD)
    assert prom is None
    assert any("geometry guard" in f for f in rep["failures"])  # CK19 200!=80,100


# ── 8. identity mismatch -> refuse, channel+field named ──────────────────────
def test_identity_mismatch_kind_refuses():
    # recorded kind raw, resolved kind corrected -> mismatch
    cfg = _raw_preview(("CD68",))
    cfg["channels"]["CD68"]["calibration_source_identity"]["actual_source_kind"] = "corrected_zarr"
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is None
    assert any("CD68" in f and "kind" in f for f in rep["failures"])


def test_identity_mismatch_path_refuses():
    cfg = _raw_preview(("CD68",))
    cfg["channels"]["CD68"]["calibration_source_identity"]["actual_source_path"] = "/other/raw.ome.tif"
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is None
    assert any("CD68" in f and "source_path" in f for f in rep["failures"])


# ── 9. no-partial: one failing channel -> NO candidate ───────────────────────
def test_no_partial_one_failing_channel():
    cfg = _raw_preview(("CD68", "CK19"))
    del cfg["channels"]["CK19"]["calibration_source_identity"]
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome", "CK19": "raw_ome"},
        raw_channel_source_path="/data/raw.ome.tif", loader_factory=_FakeLoader)
    prom, rep = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    assert prom is None
    assert rep["promoted"] is False


# ── 11. v14.5a.1 guard intact; 5c.3 never emits step2_ready=true ─────────────
def test_a1_guard_intact_and_no_step2_ready_emitted():
    cfg = _raw_preview(("CD68",))
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    prom, _ = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    # candidate never step2_ready
    assert prom["source_policy"]["step2_ready"] is False
    # manually flip -> v14.5a.1 guard hard-rejects (unchanged)
    tampered = copy.deepcopy(prom)
    tampered["source_policy"]["step2_ready"] = True
    tampered["source_policy"]["preview_only"] = False
    errs, _ = validate_step2_remap_config(tampered, allow_preview_remap=False)
    assert any("source-aware" in e for e in errs)


# ── 12. INDEPENDENCE: promotion does not call calibration_source ─────────────
def test_promotion_does_not_call_calibration_source(monkeypatch):
    import block01.utils.calibration_source as cs
    called = []
    for name in ("resolve_channel_calibration", "open_corrected_channel_array",
                 "build_corrected_calibration_identity", "build_raw_calibration_identity"):
        monkeypatch.setattr(cs, name,
                            lambda *a, _n=name, **k: called.append(_n), raising=True)
    cfg = _raw_preview(("CD68",))
    promote_source_aware_from_sources(
        cfg, step2_input_shape=[200, 200], raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader, active_method=_CSD)
    assert called == []


# ── 13. dual-implementation consistency: preview Strategy B == shared decision ─
def test_dual_implementation_consistency():
    from block01.utils.source_identity import (
        decide_source_resolution, RESOLUTION_USE_RAW, RESOLUTION_USE_CORRECTED,
        RESOLUTION_FAIL, REQUESTED_SOURCE_RAW_OME, REQUESTED_SOURCE_CORRECTED_ZARR)
    # the preview reader's Strategy B (allow=True) truth table, by inspection of
    # utils/calibration_source.resolve_channel_calibration:
    #   raw req      -> raw (if raw read ok)
    #   corrected req + corrected available -> corrected
    #   corrected req + corrected unavailable + raw -> raw (fallback)
    cases = [
        (REQUESTED_SOURCE_RAW_OME, True, True, RESOLUTION_USE_RAW),
        (REQUESTED_SOURCE_RAW_OME, False, True, RESOLUTION_USE_RAW),
        (REQUESTED_SOURCE_CORRECTED_ZARR, True, True, RESOLUTION_USE_CORRECTED),
        (REQUESTED_SOURCE_CORRECTED_ZARR, False, True, RESOLUTION_USE_RAW),   # fallback
        (REQUESTED_SOURCE_CORRECTED_ZARR, False, False, RESOLUTION_FAIL),
    ]
    for req, corr, raw, expected in cases:
        assert decide_source_resolution(
            req, corrected_available=corr, raw_available=raw,
            allow_corrected_to_raw_fallback=True) == expected
    # promotion tolerance (allow=False): corrected-unavailable -> FAIL, not raw
    assert decide_source_resolution(
        REQUESTED_SOURCE_CORRECTED_ZARR, corrected_available=False,
        raw_available=True, allow_corrected_to_raw_fallback=False) == RESOLUTION_FAIL


# candidate-state validator rejects a mixture value used as alignment mode.
def test_candidate_validator_rejects_mixture_as_alignment():
    cfg = _raw_preview(("CD68",))
    pcr = resolve_per_channel_marker_sources(
        {"CD68": "raw_ome"}, raw_channel_source_path="/data/raw.ome.tif",
        loader_factory=_FakeLoader)
    prom, _ = promote_source_aware_config_for_step2(
        copy.deepcopy(cfg), pcr, [200, 200], active_method=_CSD)
    bad = copy.deepcopy(prom)
    bad["source_policy"]["source_alignment_mode"] = "mixed_raw_corrected"
    errs = validate_source_aware_promoted_candidate(bad)
    assert errs and any("alignment" in e.lower() for e in errs)
