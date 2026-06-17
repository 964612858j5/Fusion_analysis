#!/usr/bin/env python3
"""
Small-ROI runtime smoke test for the Step2 manual channel remap (Phase 5c).

Validates a channel_remap_config against Step2 rules, prints the exact seg_config
settings for each gate mode (baseline / gi / remap / remap_and_gi), and — when
--execute is passed and a zarr ROI is given — drives the existing Step2
SegmentMergeWorker (no parallel pipeline is invented). Default is a DRY RUN:
it validates + prints settings + commands and never touches segmentation runtime.

It compares region-stratified ROIs (clean vs AF-heavy) so the operator can decide
whether `remap` over-expands in AF-heavy regions while `remap_and_gi` suppresses
it. See docs/v13_1_channel_conditioning/12_SMALL_ROI_REMAP_SMOKE_TEST.md.

This script changes NO segmentation algorithm, _block_gi, camp threshold,
feature extraction / h5ad, napari, or top-hat/cuCIM behavior.

Pure helpers (build_mode_seg_config / check_remap_provenance /
format_smoke_summary_table) are import-safe and unit-tested.
"""

from __future__ import annotations

import argparse
import json
import os

# Gate modes compared by the smoke test. "baseline" = no remap config at all;
# "gi" = remap config present but gate stays on the legacy gi path.
SMOKE_MODES = ("baseline", "gi", "remap", "remap_and_gi")


def build_mode_seg_config(base_seg_config, mode, remap_config_path=None,
                          allow_preview_remap=True):
    """Return a seg_config copy configured for one smoke gate mode.

    Pure: no I/O, no segmentation. The returned dict is exactly what would be
    passed to SegmentMergeWorker.
    """
    cfg = dict(base_seg_config or {})
    # Always start clean so a reused base config never leaks remap keys.
    for k in ("channel_remap_config_path", "allow_preview_remap",
              "remap_gate_mode", "remap_gate_threshold"):
        cfg.pop(k, None)

    if mode == "baseline":
        return cfg  # legacy v13 path, manual remap off

    if mode not in SMOKE_MODES:
        raise ValueError(f"unknown smoke mode: {mode!r} (use {SMOKE_MODES})")
    if not remap_config_path:
        raise ValueError(f"mode '{mode}' requires --remap-config")

    cfg["channel_remap_config_path"] = os.path.abspath(remap_config_path)
    cfg["allow_preview_remap"] = bool(allow_preview_remap)
    # gi: config loaded/validated but the engines keep the gi gate; remap /
    # remap_and_gi switch the signal gate accordingly.
    cfg["remap_gate_mode"] = {"gi": "gi", "remap": "remap",
                              "remap_and_gi": "remap_and_gi"}[mode]
    return cfg


def check_remap_provenance(out_dir, expect_remap=True):
    """Inspect a run output dir for remap provenance artifacts.

    Returns a dict describing which provenance files exist and the key fields.
    For baseline runs pass expect_remap=False (provenance files are expected
    absent / manual_remap_enabled false).
    """
    used_path = os.path.join(out_dir, "channel_remap_config.used.json")
    prov_path = os.path.join(out_dir, "channel_remap_provenance.json")
    info = {
        "out_dir": out_dir,
        "used_json_exists": os.path.isfile(used_path),
        "provenance_json_exists": os.path.isfile(prov_path),
        "manual_remap_enabled": None,
        "allow_preview_remap": None,
        "remap_gate_mode": None,
        "channel_remap_config_hash": None,
        "used_for": None,
        "source_policy_present": False,
    }
    if info["provenance_json_exists"]:
        try:
            with open(prov_path, "r", encoding="utf-8") as f:
                prov = json.load(f)
            info["manual_remap_enabled"] = prov.get("manual_remap_enabled")
            info["allow_preview_remap"] = prov.get("allow_preview_remap")
            info["remap_gate_mode"] = prov.get("gate_mode")
            info["channel_remap_config_hash"] = prov.get("channel_remap_config_hash")
            info["used_for"] = prov.get("used_for")
            info["source_policy_present"] = bool(prov.get("source_policy"))
        except Exception as exc:
            info["error"] = str(exc)

    if expect_remap:
        info["ok"] = bool(
            info["used_json_exists"] and info["provenance_json_exists"]
            and info["manual_remap_enabled"] is True
            and info["used_for"] == "segmentation_only"
            and info["source_policy_present"])
    else:
        # Baseline: no remap provenance, or explicitly disabled.
        info["ok"] = (not info["provenance_json_exists"]
                      or info["manual_remap_enabled"] in (False, None))
    return info


_SUMMARY_COLUMNS = (
    ("roi", 14), ("mode", 13), ("status", 7), ("runtime", 9),
    ("cells", 8), ("mask_area", 11), ("mean_area", 10), ("provenance", 11),
)


def format_smoke_summary_table(rows):
    """Format collected smoke rows into a fixed-width text table.

    Each row is a dict with keys matching _SUMMARY_COLUMNS (missing -> '-').
    """
    out = []
    header = "  ".join(name.ljust(width) for name, width in _SUMMARY_COLUMNS)
    out.append(header)
    out.append("  ".join("-" * width for _name, width in _SUMMARY_COLUMNS))
    for row in rows:
        cells = []
        for name, width in _SUMMARY_COLUMNS:
            val = row.get(name, "-")
            cells.append(str("-" if val is None else val).ljust(width))
        out.append("  ".join(cells))
    return "\n".join(out)


def _validate_config(remap_config_path, selected_channels, hq_input_mode,
                     allow_preview_remap):
    """Validate the remap config for Step2 use; returns (ok, messages)."""
    from ..utils.channel_remap_config import (
        validate_step2_remap_config, validate_remap_covers_selected_channels)
    if not os.path.isfile(remap_config_path):
        return False, [f"remap config not found: {remap_config_path}"]
    with open(remap_config_path, "r", encoding="utf-8") as f:
        raw_cfg = json.load(f)
    errors, resolved = validate_step2_remap_config(
        raw_cfg, allow_preview_remap=allow_preview_remap)
    if errors:
        return False, errors
    cov = validate_remap_covers_selected_channels(
        resolved, selected_channels, hq_input_mode)
    if cov:
        return False, cov
    return True, [f"valid; {len(resolved)} marker channels: {sorted(resolved)}"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", help="OME-TIFF / project / zarr path (execute mode)")
    ap.add_argument("--roi", default="roi", help="ROI label for the summary")
    ap.add_argument("--remap-config", help="channel_remap_config.json path")
    ap.add_argument("--method", default="cds2",
                    help="segmentation method (cds2 primary; hq2 secondary)")
    ap.add_argument("--channels", default="",
                    help="selected marker channels (';' or ',' separated)")
    ap.add_argument("--hq-input-mode", default="")
    ap.add_argument("--gate-mode", default="remap", choices=SMOKE_MODES)
    ap.add_argument("--allow-preview-remap", action="store_true")
    ap.add_argument("--out", default="", help="run output dir (for provenance check)")
    ap.add_argument("--execute", action="store_true",
                    help="actually run Step2 (default: dry run / validate + print)")
    args = ap.parse_args(argv)

    print("== Step2 remap small-ROI smoke (Phase 5c) ==")
    print(f"roi={args.roi} method={args.method} gate-mode={args.gate_mode} "
          f"execute={args.execute}")

    if args.remap_config and args.gate_mode != "baseline":
        ok, msgs = _validate_config(
            args.remap_config, args.channels, args.hq_input_mode or None,
            args.allow_preview_remap)
        print("config validation:", "OK" if ok else "REJECTED")
        for m in msgs:
            print("  -", m)
        if not ok:
            return 2

    base = {"method": args.method, "hq_channels": args.channels,
            "hq_input_mode": args.hq_input_mode}
    cfg = build_mode_seg_config(base, args.gate_mode, args.remap_config,
                                args.allow_preview_remap)
    print("seg_config remap keys:")
    for k in ("channel_remap_config_path", "allow_preview_remap",
              "remap_gate_mode", "remap_gate_threshold"):
        if k in cfg:
            print(f"  {k} = {cfg[k]}")

    if args.out and os.path.isdir(args.out):
        prov = check_remap_provenance(
            args.out, expect_remap=(args.gate_mode != "baseline"))
        print("provenance check:", "OK" if prov.get("ok") else "MISSING/INVALID")
        print("  ", {k: prov[k] for k in (
            "used_json_exists", "provenance_json_exists",
            "manual_remap_enabled", "remap_gate_mode")})

    if not args.execute:
        print("\nDRY RUN — no segmentation executed. Re-run with --execute and a "
              "real --input zarr to drive SegmentMergeWorker. See "
              "docs/v13_1_channel_conditioning/12_SMALL_ROI_REMAP_SMOKE_TEST.md")
        return 0

    # Execute path: reuse the existing Step2 worker; never a parallel pipeline.
    if not args.input:
        print("ERROR: --execute requires --input")
        return 2
    try:
        from ..workers.segment_merge_worker import SegmentMergeWorker
    except Exception as exc:
        print(f"ERROR: cannot import SegmentMergeWorker: {exc}")
        return 2
    worker = SegmentMergeWorker(
        args.input, seg_config=cfg,
        output_dir=args.out or os.path.join(os.getcwd(), "smoke_out"))
    print(f"output_dir = {worker.output_dir}")
    worker.run()  # QThread body; runs synchronously here
    prov = check_remap_provenance(
        worker.output_dir, expect_remap=(args.gate_mode != "baseline"))
    print("post-run provenance:", "OK" if prov.get("ok") else "MISSING/INVALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
