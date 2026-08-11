"""v14.5d Workstream B1 — candidate→runtime promotion + runtime-config validator.

Pure/offscreen. promote_candidate_to_runtime re-verifies a source-aware candidate at
launch (delegating to the 5c.3 checks, here monkeypatched) and stamps it
runtime_supported=true + step2_ready=true; validate_source_aware_runtime_config gates
the runtime shape. NEITHER is wired into any live path — B1 + B2-wiring + B3 land
together behind ENABLE_STEP2_SOURCE_AWARE_REMAP_RUNTIME (default off)."""

import copy

import block01.utils.remap_promotion as rp
from block01.utils.channel_remap_config import validate_source_aware_runtime_config


def _runtime_config(mixture="homogeneous_corrected", with_resolved=True,
                    step2_ready=True, runtime_supported=True, dapi=False):
    ch = {"min": 0.0, "max": 1.0, "gamma": 1.0, "step2_compatible": True,
          "calibration_source_matches_step2": True}
    if with_resolved:
        ch["resolved_source_kind"] = "corrected_zarr"
        ch["resolved_source_path"] = "/data/corrected.zarr"
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
