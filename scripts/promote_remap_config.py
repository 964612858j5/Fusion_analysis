#!/usr/bin/env python
"""block01/scripts/promote_remap_config.py — headless 2.1c-b promotion CLI.

Promote a Step1.5 preview remap config to a Step2-ready config IF its recorded
calibration source identity matches the source Step2 will actually resolve.

It resolves the Step2 HQ source via the 2.1c-a SHARED resolver
(workers.hq_source_resolver.resolve_hq_marker_source) using the active Step2
seg-params, then calls utils.remap_promotion.promote_step1_5_config_for_step2.

On success writes ``<preview-name>_step2ready.json``; on refusal writes
``promotion_report.json``. It does NOT wire anything into Step2 auto-loading
(that is phase 5f-b).

Usage:
    python -m block01.scripts.promote_remap_config \
        --seg-params /path/to/run_segmentation_params.json \
        --preview-config /path/to/step1_5_channel_remap_*.json \
        [--out-dir /path/to/out]
"""

from __future__ import annotations

import argparse
import json
import os

from ..workers.hq_marker_segmentation import parse_hq_channels
from ..utils.remap_promotion import promote_step1_5_config_for_step2, _as_hw
from ..utils.segmentation_config import is_hq2_csd_method


def active_method_of(seg_config):
    """Active Step2 algorithm id from the seg params."""
    return str(seg_config.get("method") or "")


def source_mode_for_seg(seg_config):
    """HQ marker source_mode derived from the active algorithm (never a config field).

    Mirrors the worker: HQ2/CSD -> raw_ome_only; everything else ->
    corrected_then_raw_fallback. Imported lazily to avoid pulling the resolver at
    module import (keeps the parser helpers cheaply importable in tests).
    """
    from ..workers.hq_source_resolver import (
        SOURCE_MODE_RAW_ONLY, SOURCE_MODE_CORRECTED_THEN_RAW)
    return (SOURCE_MODE_RAW_ONLY if is_hq2_csd_method(active_method_of(seg_config))
            else SOURCE_MODE_CORRECTED_THEN_RAW)


def requested_channels_from_seg(seg_config):
    """Active Step2 HQ marker channels, via the SAME parser Step2/resolver use.

    Reuses parse_hq_channels so the promotion verifies exactly the channel set
    Step2 requests — never an ad-hoc split that could diverge.
    """
    return parse_hq_channels(seg_config.get("hq_channels") or [])


def _requested_roi_names(seg_config):
    names = [seg_config.get("roi_name"), seg_config.get("roi_display_name")]
    return {str(n) for n in names if str(n or "").strip()}


# Geometry-derivation method labels (recorded in the promotion report).
DERIV_EXPLICIT_SHAPE = "explicit_step2_input_shape"
DERIV_NUCLEI_PATH = "explicit_nuclei_mask_path"
DERIV_GLOBAL_PATH = "explicit_global_mask_path"
DERIV_SCAN_NUCLEI = "fallback_scan_global_nuclei_mask"
DERIV_SCAN_GLOBAL = "fallback_scan_global_mask"
DERIV_UNKNOWN = "unknown"
FALLBACK_SCAN_WARNING = (
    "step2_input_shape was derived by fallback run-directory scan; recommend "
    "writing explicit geometry provenance in future runs.")


def _zarr_shape_hw(path):
    """[H, W] of a zarr array at path, or None. Never crops/transposes — reads
    the array's real shape and takes its last two dims as-is."""
    if not path or not os.path.exists(path):
        return None
    try:
        import zarr
        arr = zarr.open(path, mode="r")
        return _as_hw(getattr(arr, "shape", None))
    except Exception:
        return None


def _scan_run_dir_shape(run_dir, pattern):
    """Newest zarr matching pattern under run_dir -> [H, W], or (None, '')."""
    import glob
    matches = sorted(glob.glob(os.path.join(run_dir, pattern)),
                     key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                     reverse=True)
    for p in matches:
        hw = _zarr_shape_hw(p)
        if hw is not None:
            return hw, os.path.abspath(p)
    return None, ""


def derive_step2_geometry(seg_config, run_metadata=None, provenance=None, run_dir=None):
    """Resolve this run's Step2 segmentation-input geometry [H, W], structurally.

    Priority (structured provenance over filename guessing):
      1. explicit step2_input_shape (seg params / run_metadata / provenance)
      2. explicit nuclei mask zarr path -> its geometry
      3. explicit global mask zarr path -> its geometry
      4. fallback scan run dir for global_nuclei_mask*.zarr
      5. fallback scan run dir for global_mask*.zarr
      6. unknown -> caller REFUSES (geometry guard never skipped)

    Shapes are read as-is in [H, W]; nothing is cropped/resized/transposed to force
    a match. Returns {"shape", "method", "path", "warning"}.
    """
    sources = [d for d in (seg_config, run_metadata, provenance) if isinstance(d, dict)]

    for d in sources:                                   # 1
        hw = _as_hw(d.get("step2_input_shape"))
        if hw is not None:
            return {"shape": hw, "method": DERIV_EXPLICIT_SHAPE, "path": "", "warning": ""}

    for d in sources:                                   # 2
        for key in ("nuclei_mask_path", "global_nuclei_mask_path", "global_nuclei_mask"):
            p = d.get(key)
            hw = _zarr_shape_hw(p)
            if hw is not None:
                return {"shape": hw, "method": DERIV_NUCLEI_PATH,
                        "path": os.path.abspath(p), "warning": ""}

    for d in sources:                                   # 3
        for key in ("global_mask_path", "mask_path"):
            p = d.get(key)
            hw = _zarr_shape_hw(p)
            if hw is not None:
                return {"shape": hw, "method": DERIV_GLOBAL_PATH,
                        "path": os.path.abspath(p), "warning": ""}

    if run_dir and os.path.isdir(run_dir):              # 4, 5
        hw, path = _scan_run_dir_shape(run_dir, "global_nuclei_mask*.zarr")
        if hw is not None:
            return {"shape": hw, "method": DERIV_SCAN_NUCLEI, "path": path,
                    "warning": FALLBACK_SCAN_WARNING}
        hw, path = _scan_run_dir_shape(run_dir, "global_mask*.zarr")
        if hw is not None:
            return {"shape": hw, "method": DERIV_SCAN_GLOBAL, "path": path,
                    "warning": FALLBACK_SCAN_WARNING}

    return {"shape": None, "method": DERIV_UNKNOWN, "path": "", "warning": ""}


def derive_step2_input_shape(seg_config, run_metadata=None, provenance=None,
                             run_dir=None):
    """Back-compat wrapper: returns only the [H, W] shape (or None)."""
    return derive_step2_geometry(seg_config, run_metadata, provenance, run_dir)["shape"]


def build_resolved_source(seg_config):
    """Run the SHARED 2.1c-a resolver with the active Step2 params."""
    from ..workers.hq_source_resolver import resolve_hq_marker_source
    from ..core.io_loader import OMETIFFLoader

    multichannel = (seg_config.get("multichannel_source_path")
                    or seg_config.get("hq_source_zarr")
                    or seg_config.get("corrected_channels_zarr") or "")
    raw = (seg_config.get("raw_channel_source_path")
           or seg_config.get("raw_ome_path") or "")
    return resolve_hq_marker_source(
        requested_channels=requested_channels_from_seg(seg_config),
        multichannel_source_path=multichannel,
        raw_channel_source_path=raw,
        roi_id=str(seg_config.get("roi_id") or ""),
        requested_roi_names=_requested_roi_names(seg_config),
        param_file=str(seg_config.get("param_file") or ""),
        abs_fn=os.path.abspath, loader_factory=OMETIFFLoader,
        source_mode=source_mode_for_seg(seg_config))


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_json_safe(path):
    try:
        return _load_json(path)
    except Exception:
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Promote a Step1.5 preview remap config to Step2-ready (2.1c-b).")
    ap.add_argument("--seg-params", required=True, help="active Step2 segmentation params JSON")
    ap.add_argument("--preview-config", required=True, help="Step1.5 preview remap config JSON")
    ap.add_argument("--out-dir", default="", help="output dir (default: alongside preview config)")
    args = ap.parse_args(argv)

    seg_config = _load_json(args.seg_params)
    preview = _load_json(args.preview_config)
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.preview_config))
    os.makedirs(out_dir, exist_ok=True)

    # Geometry is derived structurally from run-level provenance in the Step2 run
    # folder (run_metadata.json / channel_remap_provenance.json), with a fallback
    # scan of the run dir. The run folder is where the seg params live.
    run_dir = os.path.dirname(os.path.abspath(args.seg_params))
    run_metadata = _load_json_safe(os.path.join(run_dir, "run_metadata.json"))
    provenance = _load_json_safe(os.path.join(run_dir, "channel_remap_provenance.json"))
    geo = derive_step2_geometry(seg_config, run_metadata, provenance, run_dir)
    step2_shape = geo["shape"]

    method = active_method_of(seg_config)

    # Step1.5 remap configs are HQ2/CSD-only: refuse non-HQ2/CSD methods up front
    # (no need to resolve any source for them).
    if not is_hq2_csd_method(method):
        promoted, report = promote_step1_5_config_for_step2(
            preview, None, step2_shape, active_method=method)
    else:
        # Resolve Step2's HQ source via the shared resolver under the algorithm-
        # derived source_mode (raw_ome_only). A resolution error is itself a refusal.
        try:
            resolved = build_resolved_source(seg_config)
        except Exception as exc:
            report = {"promoted": False,
                      "failures": [f"could not resolve Step2 HQ source: {exc}"]}
            promoted = None
        else:
            promoted, report = promote_step1_5_config_for_step2(
                preview, resolved, step2_shape, active_method=method)

    report["step2_input_shape_derived"] = step2_shape
    report["geometry_derivation"] = {
        "method": geo["method"], "path": geo["path"], "warning": geo["warning"]}
    if geo["warning"]:
        report.setdefault("warnings", []).append(geo["warning"])

    print(f"[promote] step2_input_shape={step2_shape} "
          f"(derived: {geo['method']}{', ' + geo['path'] if geo['path'] else ''})")
    if geo["warning"]:
        print(f"[promote] WARNING: {geo['warning']}")

    if promoted is not None:
        base = os.path.splitext(os.path.basename(args.preview_config))[0]
        out_path = os.path.join(out_dir, f"{base}_step2ready.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(promoted, f, indent=2, sort_keys=False)
        print(f"[promote] PROMOTED -> {out_path}")
        print(f"[promote] source: {report.get('source_kind')} {report.get('source_path')}")
        return 0

    report_path = os.path.join(out_dir, "promotion_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=False)
    print(f"[promote] REFUSED ({len(report.get('failures', []))} failure(s)) -> {report_path}")
    for fail in report.get("failures", []):
        print(f"[promote]   - {fail}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
