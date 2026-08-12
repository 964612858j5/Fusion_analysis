"""v14.5d Workstream B1 — candidate→runtime promotion + runtime-config validator.

Pure/offscreen. promote_candidate_to_runtime re-verifies a source-aware candidate at
launch (delegating to the 5c.3 checks, here monkeypatched) and stamps it
runtime_supported=true + step2_ready=true; validate_source_aware_runtime_config gates
the runtime shape. NEITHER is wired into any live path — B1 + B2-wiring + B3 land
together behind ENABLE_STEP2_SOURCE_AWARE_REMAP_RUNTIME (default off)."""

import copy

import block01.utils.remap_promotion as rp
from block01.utils.channel_remap_config import (
    validate_source_aware_runtime_config, validate_step2_remap_config)
from block01.utils.segmentation_config import (
    step2_source_aware_runtime_enabled, STEP2_SOURCE_AWARE_RUNTIME_ENV,
    CELLPOSE_NUCLEI_HQ2)

_SOURCE_AWARE_GUARD = "source-aware remap config with step2_ready"


def _runtime_config(mixture="homogeneous_corrected", with_resolved=True,
                    step2_ready=True, runtime_supported=True, dapi=False):
    ch = {"min": 0.0, "max": 1.0, "gamma": 1.0, "step2_compatible": True,
          "calibration_source_matches_step2": True}
    if with_resolved:
        ch["resolved_source_kind"] = "corrected_zarr"
        ch["resolved_source_path"] = "/data/corrected.zarr"
        ch["resolved_group_name"] = "ROI_1"
        ch["resolved_source_shape"] = [200, 200]
    channels = {"CK19": dict(ch), "CD68": dict(ch)}
    if dapi:
        channels["DAPI"] = dict(ch)
    return {
        "channels": channels,
        "source_policy": {"step2_ready": step2_ready,
                          "source_alignment_mode": "per_channel_native",
                          "preview_only": False,
                          "calibration_source_matches_step2": True},
        "source_mixture_mode": mixture,
        "created_by_source_aware_promotion": True,
        "source_aware_promotion_ready": True,
        "runtime_supported": runtime_supported,
        "used_for": "segmentation_only",
    }


# ── validate_source_aware_runtime_config ─────────────────────────────────────

def test_runtime_validator_accepts_wellformed():
    assert validate_source_aware_runtime_config(_runtime_config()) == []


def test_runtime_validator_requires_runtime_supported():
    errs = validate_source_aware_runtime_config(_runtime_config(runtime_supported=False))
    assert any("runtime_supported" in e for e in errs)


def test_runtime_validator_requires_step2_ready():
    errs = validate_source_aware_runtime_config(_runtime_config(step2_ready=False))
    assert any("step2_ready=true" in e for e in errs)


def test_runtime_validator_rejects_mixed_mixture():
    errs = validate_source_aware_runtime_config(_runtime_config(mixture="mixed_raw_corrected"))
    assert any("homogeneous" in e for e in errs)


def test_runtime_validator_requires_resolved_source_per_channel():
    errs = validate_source_aware_runtime_config(_runtime_config(with_resolved=False))
    assert any("resolved_source_kind" in e for e in errs)
    assert any("resolved_source_path" in e for e in errs)


def test_runtime_validator_requires_resolved_group_name():
    cfg = _runtime_config()
    for c in cfg["channels"].values():
        c.pop("resolved_group_name", None)
    errs = validate_source_aware_runtime_config(cfg)
    assert any("resolved_group_name" in e for e in errs)


def test_runtime_validator_requires_valid_resolved_shape():
    cfg = _runtime_config()
    for c in cfg["channels"].values():
        c["resolved_source_shape"] = None                # missing/invalid -> hard fail
    errs = validate_source_aware_runtime_config(cfg)
    assert any("resolved_source_shape" in e for e in errs)


def test_runtime_validator_rejects_reference_channel():
    errs = validate_source_aware_runtime_config(_runtime_config(dapi=True))
    assert any("DAPI" in e and "reference" in e for e in errs)


def test_runtime_validator_requires_promotion_ready():
    cfg = _runtime_config()
    cfg["source_aware_promotion_ready"] = False
    errs = validate_source_aware_runtime_config(cfg)
    assert any("source_aware_promotion_ready" in e for e in errs)


def test_runtime_validator_rejects_kind_inconsistent_with_mixture():
    # top mixture says corrected, but a channel resolved to raw_ome
    cfg = _runtime_config(mixture="homogeneous_corrected")
    cfg["channels"]["CK19"]["resolved_source_kind"] = "raw_ome"
    errs = validate_source_aware_runtime_config(cfg)
    assert any("inconsistent with source_mixture_mode" in e for e in errs)


def test_runtime_validator_rejects_unknown_kind():
    cfg = _runtime_config()
    cfg["channels"]["CK19"]["resolved_source_kind"] = "somewhere"
    errs = validate_source_aware_runtime_config(cfg)
    assert any("raw_ome|corrected_zarr" in e for e in errs)


# ── feature flag (default off) ───────────────────────────────────────────────

def test_flag_default_off_and_env_toggle(monkeypatch):
    monkeypatch.delenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, raising=False)
    assert step2_source_aware_runtime_enabled() is False
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "1")
    assert step2_source_aware_runtime_enabled() is True
    monkeypatch.setenv(STEP2_SOURCE_AWARE_RUNTIME_ENV, "off")
    assert step2_source_aware_runtime_enabled() is False


# ── explicit allow_source_aware_runtime param on validate_step2_remap_config ─

def test_validate_step2_still_rejects_runtime_config_by_default():
    # default (no explicit flag) MUST keep the hard source-aware guard
    errs, _ = validate_step2_remap_config(_runtime_config())
    assert any(_SOURCE_AWARE_GUARD in e for e in errs)


def test_validate_step2_lifts_guard_only_with_explicit_flag_and_valid_runtime():
    cfg = _runtime_config()                       # well-formed runtime config
    errs, _ = validate_step2_remap_config(cfg, allow_source_aware_runtime=True)
    assert not any(_SOURCE_AWARE_GUARD in e for e in errs)   # guard lifted


def test_validate_step2_keeps_guard_for_malformed_runtime_even_with_flag():
    cfg = _runtime_config(runtime_supported=False)   # not a valid runtime config
    errs, _ = validate_step2_remap_config(cfg, allow_source_aware_runtime=True)
    assert any(_SOURCE_AWARE_GUARD in e for e in errs)       # not lifted


# ── promote_candidate_to_runtime ─────────────────────────────────────────────

class _Resolved:
    def __init__(self, mixture):
        self.per_channel = {}
        self.source_mixture_mode = mixture


def _candidate():
    # what promote_source_aware_config_for_step2 emits: step2_ready=false,
    # runtime_supported=false, per-channel resolved fields present.
    c = _runtime_config(step2_ready=False, runtime_supported=False)
    c["source_policy"]["step2_ready"] = False
    return c


def test_promote_to_runtime_stamps_and_validates(monkeypatch):
    cand = _candidate()
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2",
                        lambda *a, **k: (copy.deepcopy(cand), {"promoted": True, "failures": []}))
    runtime, report = rp.promote_candidate_to_runtime(
        cand, _Resolved("homogeneous_corrected"), [200, 200])
    assert runtime is not None
    assert runtime["runtime_supported"] is True
    assert runtime["source_policy"]["step2_ready"] is True
    assert report["runtime_supported"] is True
    assert validate_source_aware_runtime_config(runtime) == []


def test_promote_to_runtime_refuses_mixed_before_reverify(monkeypatch):
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return (_candidate(), {"promoted": True, "failures": []})
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2", _spy)
    runtime, report = rp.promote_candidate_to_runtime(
        _candidate(), _Resolved("mixed_raw_corrected"), [200, 200])
    assert runtime is None
    assert called["n"] == 0                       # homogeneous guard fires first
    assert any("homogeneous" in f for f in report["failures"])


def test_promote_to_runtime_propagates_reverify_refusal(monkeypatch):
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2",
                        lambda *a, **k: (None, {"promoted": False,
                                                "failures": ["geometry guard: ..."]}))
    runtime, report = rp.promote_candidate_to_runtime(
        _candidate(), _Resolved("homogeneous_raw"), [200, 200])
    assert runtime is None
    assert any("geometry" in f for f in report["failures"])


def test_promote_to_runtime_rejects_when_stamped_config_invalid(monkeypatch):
    # re-verify "passes" but returns a candidate missing per-channel resolved source
    bad = _runtime_config(with_resolved=False, step2_ready=False, runtime_supported=False)
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2",
                        lambda *a, **k: (copy.deepcopy(bad), {"promoted": True, "failures": []}))
    runtime, report = rp.promote_candidate_to_runtime(
        _candidate(), _Resolved("homogeneous_corrected"), [200, 200])
    assert runtime is None
    assert any("runtime config failed validation" in f for f in report["failures"])


# ── boundary: only a COMPLETED candidate may enter runtime ───────────────────

def _reverify_should_not_run(*_a, **_k):
    raise AssertionError("re-verify must not run for a non-candidate input")


def test_promote_to_runtime_refuses_plain_preview(monkeypatch):
    # a bare source-aware preview (not created_by/ not promotion_ready) must refuse
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2", _reverify_should_not_run)
    preview = _candidate()
    preview["created_by_source_aware_promotion"] = False
    preview["source_aware_promotion_ready"] = False
    runtime, report = rp.promote_candidate_to_runtime(
        preview, _Resolved("homogeneous_corrected"), [200, 200])
    assert runtime is None
    assert any("not a valid source-aware candidate" in f for f in report["failures"])


def test_promote_to_runtime_refuses_missing_promotion_ready(monkeypatch):
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2", _reverify_should_not_run)
    cand = _candidate()
    cand["source_aware_promotion_ready"] = False
    runtime, report = rp.promote_candidate_to_runtime(
        cand, _Resolved("homogeneous_corrected"), [200, 200])
    assert runtime is None
    assert any("not a valid source-aware candidate" in f for f in report["failures"])


def test_promote_to_runtime_refuses_already_step2_ready(monkeypatch):
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2", _reverify_should_not_run)
    cand = _candidate()
    cand["source_policy"]["step2_ready"] = True     # already ready -> not a fresh candidate
    runtime, report = rp.promote_candidate_to_runtime(
        cand, _Resolved("homogeneous_corrected"), [200, 200])
    assert runtime is None
    assert any("not a valid source-aware candidate" in f for f in report["failures"])


def test_promote_to_runtime_refuses_already_runtime_supported(monkeypatch):
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2", _reverify_should_not_run)
    cand = _candidate()
    cand["runtime_supported"] = True                # already runtime -> refuse re-entry
    runtime, report = rp.promote_candidate_to_runtime(
        cand, _Resolved("homogeneous_corrected"), [200, 200])
    assert runtime is None
    assert any("already runtime_supported" in f for f in report["failures"])


# ── promote_source_aware_runtime_from_sources (launch helper) ────────────────

def _preview(kinds=(("CK19", "corrected_zarr"), ("CD68", "corrected_zarr"))):
    return {"channels": {ch: {"min": 0.0, "max": 1.0,
                              "calibration_source_identity": {"actual_source_kind": k}}
                         for ch, k in kinds},
            "source_policy": {}, "used_for": "segmentation_only"}


def test_runtime_from_sources_success(monkeypatch):
    import block01.workers.hq_source_resolver as hqsr
    monkeypatch.setattr(hqsr, "resolve_per_channel_marker_sources",
                        lambda requests, **k: ("PC", requests))
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2",
                        lambda cfg, per, shape, **k: ({"candidate": True}, {"promoted": True}))
    monkeypatch.setattr(rp, "promote_candidate_to_runtime",
                        lambda cand, per, shape, **k: ({"runtime": True}, {"runtime_supported": True}))
    out, rep = rp.promote_source_aware_runtime_from_sources(
        _preview(), step2_input_shape=[200, 200], active_method=CELLPOSE_NUCLEI_HQ2)
    assert out == {"runtime": True}


def test_runtime_from_sources_resolution_error(monkeypatch):
    import block01.workers.hq_source_resolver as hqsr

    def _boom(requests, **k):
        raise hqsr.PerChannelResolutionError(
            channel="CD68", requested_source="corrected_zarr", reason="not found")
    monkeypatch.setattr(hqsr, "resolve_per_channel_marker_sources", _boom)
    out, rep = rp.promote_source_aware_runtime_from_sources(
        _preview(), step2_input_shape=[200, 200], active_method=CELLPOSE_NUCLEI_HQ2)
    assert out is None and any("CD68" in f for f in rep["failures"])


def test_runtime_from_sources_missing_identity():
    preview = {"channels": {"CK19": {"min": 0.0, "max": 1.0}},
               "used_for": "segmentation_only"}
    out, rep = rp.promote_source_aware_runtime_from_sources(
        preview, step2_input_shape=[200, 200], active_method=CELLPOSE_NUCLEI_HQ2)
    assert out is None and any("calibration_source_identity" in f for f in rep["failures"])


def test_runtime_from_sources_candidate_refused(monkeypatch):
    import block01.workers.hq_source_resolver as hqsr
    monkeypatch.setattr(hqsr, "resolve_per_channel_marker_sources", lambda requests, **k: object())
    monkeypatch.setattr(rp, "promote_source_aware_config_for_step2",
                        lambda *a, **k: (None, {"promoted": False, "failures": ["geometry"]}))
    out, rep = rp.promote_source_aware_runtime_from_sources(
        _preview(), step2_input_shape=[200, 200], active_method=CELLPOSE_NUCLEI_HQ2)
    assert out is None and any("geometry" in f for f in rep["failures"])
