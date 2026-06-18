#!/usr/bin/env python3
"""Automated numerical QC for a Step2 manual-remap segmentation run.

Reads only the run directory's existing outputs (metadata, logs, provenance,
and the global mask / nuclei zarr arrays) and writes:

    <run_dir>/step2_remap_qc_report.json
    <run_dir>/step2_remap_qc_report.md

No segmentation output is modified. No GUI / napari. Mask arrays are scanned
block-wise so the full WSI mask is never materialised in RAM at once.

Usage:
    python scripts/qc_step2_remap_run.py --run-dir <RUN_DIR> [--thumbnails]

This is a read-only reporting tool; it does NOT touch core segmentation code.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re

import numpy as np

try:  # zarr is a project dependency; degrade gracefully if absent
    import zarr
except Exception:  # pragma: no cover - exercised only without zarr
    zarr = None


# ── thresholds (tunable; kept as constants so QC verdicts are explicit) ──────
SCAN_BLOCK_ROWS = 1024          # full-width strip height for block-wise scans
LABEL_SAMPLE_N = 5000           # max labels sampled for percentile summaries
SAMPLE_SEED = 0
RATIO_FAIL_LOW = 1.0            # median cell/nucleus ratio below this -> fail
RATIO_WARN_HIGH = 6.0          # median ratio above this -> warning
NUCLEI_WITHOUT_CELL_FAIL = 0.30
CELLS_WITHOUT_NUCLEUS_FAIL = 0.30
NUCLEUS_INSIDE_CELL_FAIL = 0.50
TINY_CELL_AREA = 10
HUGE_CELL_AREA = 5000
TINY_CELL_FRAC_WARN = 0.10
HUGE_CELL_FRAC_WARN = 0.05
P99_OVER_MEDIAN_WARN = 50.0
SEAM_STRIP_PX = 64
SEAM_DROP_FACTOR = 0.5         # seam frac < 0.5 * adjacent -> warn
SEAM_SPIKE_FACTOR = 2.0        # seam frac > 2.0 * adjacent -> warn


# ── small IO helpers ─────────────────────────────────────────────────────────
def _load_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        return {"_qc_parse_error": str(exc)}


def _first_existing(run_dir, *names):
    for n in names:
        p = os.path.join(run_dir, n)
        if os.path.exists(p):
            return p
    return None


def _glob_one(run_dir, pattern, exclude_substr=None):
    """Return one real path matching pattern, deduping symlinks; None if absent."""
    hits = sorted(glob.glob(os.path.join(run_dir, pattern)))
    seen = set()
    for h in hits:
        if exclude_substr and exclude_substr in os.path.basename(h):
            continue
        real = os.path.realpath(h)
        if real in seen:
            continue
        seen.add(real)
        return h
    return None


def _resolve_mask_paths(run_dir):
    """Find the global cell-mask and nuclei-mask zarr arrays (names carry the
    ROI display name and spaces, e.g. 'global_mask_Full WSI.zarr')."""
    nuclei = _glob_one(run_dir, "global_nuclei_mask*.zarr")
    mask = _glob_one(run_dir, "global_mask*.zarr", exclude_substr="nuclei")
    return mask, nuclei


def _open_zarr(path):
    if path is None or zarr is None:
        return None
    try:
        return zarr.open(path, mode="r")
    except Exception:
        return None


# ── 1. runtime / log QC ──────────────────────────────────────────────────────
def qc_runtime(run_dir):
    out = {"failures": [], "warnings": [], "metrics": {}}
    m = out["metrics"]
    runtime = (_load_json(_first_existing(run_dir, "runtime_partial.json"))
               or _load_json(_first_existing(run_dir, "run_metadata.json")) or {})
    status = str(runtime.get("status", "unknown"))
    m["run_dir"] = run_dir
    m["method"] = runtime.get("method", "unknown")
    m["status"] = status
    m["elapsed"] = runtime.get("elapsed") or runtime.get("elapsed_seconds")

    log_path = _glob_one(run_dir, "segmentation_*.log")
    m["log_file"] = os.path.basename(log_path) if log_path else None
    n_error = n_traceback = n_failed = 0
    tile_labels = []
    if log_path and os.path.isfile(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                low = line.lower()
                if "traceback" in low:
                    n_traceback += 1
                if "[error]" in low or " error " in low or low.startswith("error"):
                    n_error += 1
                if "tile" in low and "failed" in low:
                    n_failed += 1
                mt = re.search(r"\bkept=(\d+)", line)
                if mt:
                    tile_labels.append(int(mt.group(1)))
    m["n_error_lines"] = n_error
    m["n_traceback_lines"] = n_traceback
    m["n_failed_tile_lines"] = n_failed
    m["tile_label_counts"] = tile_labels
    all_zero = bool(tile_labels) and all(v == 0 for v in tile_labels)
    m["all_tile_labels_zero"] = all_zero
    m["finished_but_all_tiles_failed"] = bool(
        status.lower() in ("finished", "success", "done") and all_zero)

    if n_traceback:
        out["failures"].append(f"log has {n_traceback} Traceback line(s)")
    if n_error:
        out["failures"].append(f"log has {n_error} ERROR line(s)")
    if all_zero:
        out["failures"].append("all tile label counts are zero")
    if status.lower() in ("failed", "error", "cancelled"):
        out["failures"].append(f"runtime status is '{status}'")
    return out


# ── 2. remap provenance QC ───────────────────────────────────────────────────
def qc_provenance(run_dir):
    out = {"failures": [], "warnings": [], "metrics": {}}
    m = out["metrics"]
    prov = _load_json(_first_existing(run_dir, "channel_remap_provenance.json")) or {}
    used = _load_json(_first_existing(run_dir, "channel_remap_config.used.json")) or {}
    run_meta = _load_json(_first_existing(run_dir, "run_metadata.json")) or {}

    gate_mode = prov.get("gate_mode") or prov.get("remap_gate_mode")
    sp = prov.get("source_policy") or used.get("source_policy") or {}
    m["manual_remap_enabled"] = prov.get("manual_remap_enabled")
    m["allow_preview_remap"] = prov.get("allow_preview_remap")
    m["gate_mode"] = gate_mode
    m["channel_remap_config_hash"] = prov.get("channel_remap_config_hash")
    m["used_for"] = prov.get("used_for") or used.get("used_for")
    m["channel_remap_config_path"] = prov.get("channel_remap_config_path")
    m["source_intensity_space"] = sp.get("intensity_space")
    m["source_preview_only"] = sp.get("preview_only")
    m["source_step2_pre_remap_source"] = sp.get("step2_pre_remap_source")
    m["source_alignment_mode"] = sp.get("source_alignment_mode")
    hq = run_meta.get("hq_channels")
    if hq is None:
        hq = ((run_meta.get("seg_config") or {}).get("params") or {}).get("hq_channels")
    m["hq_channels"] = hq

    if m["manual_remap_enabled"] is not True:
        out["failures"].append("manual_remap_enabled is not true")
    if m["allow_preview_remap"] is not True:
        out["failures"].append("allow_preview_remap is not true")
    if str(gate_mode) not in ("remap", "remap_and_gi"):
        out["failures"].append(f"gate mode is '{gate_mode}' (not remap/remap_and_gi)")
    if m["used_for"] != "segmentation_only":
        out["failures"].append(f"used_for is '{m['used_for']}' (not segmentation_only)")

    cfg_path = m["channel_remap_config_path"] or ""
    folder = _test_folder(run_dir)
    cfg_folder = _test_folder(cfg_path)
    if cfg_path and folder and cfg_folder and folder != cfg_folder:
        out["warnings"].append(
            f"channel_remap_config_path is from '{cfg_folder}' but run is in "
            f"'{folder}' (config from a different test folder)")
    if sp.get("preview_only") is True:
        out["warnings"].append("source_policy.preview_only is true (preview config)")
    note = (str(sp.get("alignment_note", "")) + " " + str(sp.get("note", ""))).lower()
    if "runtime integration is implemented" not in note and "not implemented" in note \
            or "step2_ready=false" in note or "until step2 runtime integration" in note:
        out["warnings"].append(
            "alignment_note still flags Step2 runtime integration as not yet implemented")
    return out


def _test_folder(path):
    """Extract the 'testN' segment of a path if present (for cross-folder check)."""
    if not path:
        return None
    mt = re.search(r"(test\d+)", str(path))
    return mt.group(1) if mt else None


# ── 3. source fallback QC ────────────────────────────────────────────────────
_FALLBACK_PATTERNS = [
    ("raw_ome_fallback", re.compile(r"falling back to raw ome", re.I)),
    ("raw_ome_fallback", re.compile(r"using raw ome channel source", re.I)),
    ("corrected_missing", re.compile(r"corrected zarr missing", re.I)),
    ("using_corrected", re.compile(r"using corrected zarr", re.I)),
]


def qc_source_fallback(run_dir):
    out = {"failures": [], "warnings": [], "metrics": {}}
    matched = []
    raw_fallback = False
    log_path = _glob_one(run_dir, "segmentation_*.log")
    if log_path and os.path.isfile(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for kind, pat in _FALLBACK_PATTERNS:
                    if pat.search(line):
                        matched.append({"kind": kind, "line": line.strip()[:300]})
                        if kind == "raw_ome_fallback" or kind == "corrected_missing":
                            raw_fallback = True
    out["metrics"]["matched_source_lines"] = matched
    out["metrics"]["raw_ome_fallback_detected"] = raw_fallback
    if raw_fallback:
        out["warnings"].append(
            "log indicates raw-OME fallback / corrected zarr missing "
            "(remap calibration source may differ from Step2 input)")
    return out


# ── 4+5. mask / nuclei zarr + label consistency QC ───────────────────────────
def summarize_masks(mask_arr, nuclei_arr, scan_block_rows=SCAN_BLOCK_ROWS,
                    sample_n=LABEL_SAMPLE_N, seed=SAMPLE_SEED, want_bbox=True):
    """Single block-wise pass over the cell-mask and nuclei-mask arrays.

    Returns a metrics dict with shape/dtype/label/consistency stats. Works on
    zarr arrays or numpy arrays (anything supporting .shape/.dtype/slicing).
    The full array is never read at once: only `scan_block_rows`-high, full-width
    strips. Per-label accumulators are sized to the max label id (cheap).
    """
    info = {}
    mshape = tuple(int(v) for v in mask_arr.shape)
    nshape = tuple(int(v) for v in nuclei_arr.shape)
    info["mask_shape"] = mshape
    info["nuclei_shape"] = nshape
    info["mask_dtype"] = str(mask_arr.dtype)
    info["nuclei_dtype"] = str(nuclei_arr.dtype)
    info["shape_match"] = (mshape == nshape)

    H, W = mshape
    # First light pass: max label across both, so accumulators are sized once.
    cell_max = 0
    nuc_max = 0
    for y0 in range(0, H, scan_block_rows):
        y1 = min(H, y0 + scan_block_rows)
        mb = np.asarray(mask_arr[y0:y1, :])
        cell_max = max(cell_max, int(mb.max()) if mb.size else 0)
        if nshape == mshape:
            nb = np.asarray(nuclei_arr[y0:y1, :])
            nuc_max = max(nuc_max, int(nb.max()) if nb.size else 0)
    if nshape != mshape:
        for y0 in range(0, nshape[0], scan_block_rows):
            y1 = min(nshape[0], y0 + scan_block_rows)
            nb = np.asarray(nuclei_arr[y0:y1, :])
            nuc_max = max(nuc_max, int(nb.max()) if nb.size else 0)
    L = int(max(cell_max, nuc_max))
    info["mask_max_label"] = cell_max
    info["nuclei_max_label"] = nuc_max
    info["max_label_match"] = (cell_max == nuc_max)

    cell_area = np.zeros(L + 1, dtype=np.int64)
    nuc_area = np.zeros(L + 1, dtype=np.int64)
    inside = np.zeros(L + 1, dtype=np.int64)
    if want_bbox:
        min_y = np.full(L + 1, H, dtype=np.int64)
        max_y = np.full(L + 1, -1, dtype=np.int64)
        min_x = np.full(L + 1, W, dtype=np.int64)
        max_x = np.full(L + 1, -1, dtype=np.int64)
    mask_nonzero = 0
    nonzero_blocks = 0

    aligned = (nshape == mshape)
    for y0 in range(0, H, scan_block_rows):
        y1 = min(H, y0 + scan_block_rows)
        mb = np.asarray(mask_arr[y0:y1, :])
        if mb.size:
            cell_area += np.bincount(mb.ravel(), minlength=L + 1)
        nz = mb > 0
        nzc = int(nz.sum())
        mask_nonzero += nzc
        if nzc:
            nonzero_blocks += 1
        if aligned:
            nb = np.asarray(nuclei_arr[y0:y1, :])
            if nb.size:
                nuc_area += np.bincount(nb.ravel(), minlength=L + 1)
                eq = (nb > 0) & (mb == nb)
                if eq.any():
                    inside += np.bincount(nb[eq].ravel(), minlength=L + 1)
        if want_bbox and nzc:
            ys, xs = np.nonzero(nz)
            labs = mb[ys, xs]
            gy = ys + y0
            np.minimum.at(min_y, labs, gy)
            np.maximum.at(max_y, labs, gy)
            np.minimum.at(min_x, labs, xs)
            np.maximum.at(max_x, labs, xs)

    if not aligned:  # count nuclei areas separately when shapes differ
        for y0 in range(0, nshape[0], scan_block_rows):
            y1 = min(nshape[0], y0 + scan_block_rows)
            nb = np.asarray(nuclei_arr[y0:y1, :])
            if nb.size:
                nuc_area += np.bincount(nb.ravel(), minlength=L + 1)

    total_px = H * W
    info["mask_nonzero_px"] = int(mask_nonzero)
    info["mask_nonzero_fraction"] = float(mask_nonzero) / float(total_px) if total_px else 0.0
    info["nonzero_blocks_sampled"] = int(nonzero_blocks)

    # Per-label consistency. label id 0 is background.
    ids = np.arange(L + 1)
    has_cell = cell_area > 0
    has_nuc = nuc_area > 0
    has_cell[0] = has_nuc[0] = False
    present = has_cell | has_nuc
    n_present = int(present.sum())
    n_cells = int(has_cell.sum())
    n_nuclei = int(has_nuc.sum())

    info["sampled_labels"] = n_present
    info["n_cell_labels"] = n_cells
    info["n_nuclei_labels"] = n_nuclei
    info["labels_missing_in_cell_mask"] = int((has_nuc & ~has_cell).sum())
    info["labels_missing_in_nuclei_mask"] = int((has_cell & ~has_nuc).sum())

    ca = cell_area[has_cell].astype(np.float64)
    na = nuc_area[has_nuc].astype(np.float64)
    both = has_cell & has_nuc
    ratio = (cell_area[both].astype(np.float64)
             / np.maximum(nuc_area[both].astype(np.float64), 1.0))

    # Uniform sampling for percentile summaries (memory bound honored even though
    # the exact per-label arrays are already cheap at this label count).
    rng = np.random.default_rng(seed)

    def _sample(a):
        if a.size > sample_n:
            idx = rng.choice(a.size, size=sample_n, replace=False)
            return a[idx]
        return a

    ca_s, na_s, ratio_s = _sample(ca), _sample(na), _sample(ratio)

    def _pct(a, q):
        return float(np.percentile(a, q)) if a.size else None

    info["median_cell_area"] = float(np.median(ca_s)) if ca_s.size else None
    info["median_nucleus_area"] = float(np.median(na_s)) if na_s.size else None
    info["median_cell_to_nucleus_area_ratio"] = (
        float(np.median(ratio_s)) if ratio_s.size else None)
    info["cell_area_p01"] = _pct(ca_s, 1)
    info["cell_area_p05"] = _pct(ca_s, 5)
    info["cell_area_p95"] = _pct(ca_s, 95)
    info["cell_area_p99"] = _pct(ca_s, 99)
    info["nucleus_area_p01"] = _pct(na_s, 1)
    info["nucleus_area_p05"] = _pct(na_s, 5)
    info["nucleus_area_p95"] = _pct(na_s, 95)
    info["nucleus_area_p99"] = _pct(na_s, 99)

    tot_nuc_px = float(nuc_area[has_nuc].sum())
    info["fraction_nucleus_pixels_inside_cell"] = (
        float(inside[has_nuc].sum()) / tot_nuc_px if tot_nuc_px else None)
    info["fraction_cells_without_nucleus"] = (
        float((has_cell & ~has_nuc).sum()) / n_cells if n_cells else None)
    info["fraction_nuclei_without_cell"] = (
        float((has_nuc & ~has_cell).sum()) / n_nuclei if n_nuclei else None)
    if n_cells:
        info["fraction_tiny_cells"] = float((cell_area[has_cell] < TINY_CELL_AREA).sum()) / n_cells
        info["fraction_huge_cells"] = float((cell_area[has_cell] > HUGE_CELL_AREA).sum()) / n_cells
    else:
        info["fraction_tiny_cells"] = info["fraction_huge_cells"] = None

    if want_bbox and n_cells:
        h = (max_y[has_cell] - min_y[has_cell] + 1)
        w = (max_x[has_cell] - min_x[has_cell] + 1)
        h = h[max_y[has_cell] >= 0]
        w = w[max_x[has_cell] >= 0]
        info["median_bbox_height"] = float(np.median(h)) if h.size else None
        info["median_bbox_width"] = float(np.median(w)) if w.size else None
    return info


def check_masks(info):
    """Apply fail/warn rules to the mask + label-consistency metrics."""
    failures, warnings = [], []
    if not info.get("shape_match"):
        failures.append(
            f"mask/nuclei shape mismatch: {info.get('mask_shape')} vs {info.get('nuclei_shape')}")
    if int(info.get("mask_max_label") or 0) == 0:
        failures.append("mask max_label == 0 (empty mask)")
    if float(info.get("mask_nonzero_fraction") or 0.0) == 0.0:
        failures.append("mask nonzero_fraction == 0 (no labelled pixels)")
    if not info.get("max_label_match"):
        failures.append(
            f"mask max_label != nuclei max_label "
            f"({info.get('mask_max_label')} vs {info.get('nuclei_max_label')})")

    ratio = info.get("median_cell_to_nucleus_area_ratio")
    if ratio is not None and ratio < RATIO_FAIL_LOW:
        failures.append(f"median cell/nucleus ratio {ratio:.3f} < {RATIO_FAIL_LOW}")
    fnwc = info.get("fraction_nuclei_without_cell")
    if fnwc is not None and fnwc > NUCLEI_WITHOUT_CELL_FAIL:
        failures.append(
            f"{fnwc:.1%} of nuclei have no matching cell label (> {NUCLEI_WITHOUT_CELL_FAIL:.0%})")
    fcwn = info.get("fraction_cells_without_nucleus")
    if fcwn is not None and fcwn > CELLS_WITHOUT_NUCLEUS_FAIL:
        failures.append(
            f"{fcwn:.1%} of cells have no nucleus (> {CELLS_WITHOUT_NUCLEUS_FAIL:.0%})")
    fin = info.get("fraction_nucleus_pixels_inside_cell")
    if fin is not None and fin < NUCLEUS_INSIDE_CELL_FAIL:
        failures.append(
            f"only {fin:.1%} of nucleus pixels fall inside their cell label "
            f"(< {NUCLEUS_INSIDE_CELL_FAIL:.0%})")

    if ratio is not None and ratio > RATIO_WARN_HIGH:
        warnings.append(f"median cell/nucleus ratio {ratio:.2f} is high (> {RATIO_WARN_HIGH})")
    med = info.get("median_cell_area")
    p99 = info.get("cell_area_p99")
    if med and p99 and med > 0 and p99 > P99_OVER_MEDIAN_WARN * med:
        warnings.append(
            f"p99 cell area {p99:.0f} >> median {med:.0f} ({p99/med:.0f}x) — large outliers")
    ftiny = info.get("fraction_tiny_cells")
    if ftiny is not None and ftiny > TINY_CELL_FRAC_WARN:
        warnings.append(f"{ftiny:.1%} of cells are tiny (< {TINY_CELL_AREA} px)")
    fhuge = info.get("fraction_huge_cells")
    if fhuge is not None and fhuge > HUGE_CELL_FRAC_WARN:
        warnings.append(f"{fhuge:.1%} of cells are huge (> {HUGE_CELL_AREA} px)")
    return failures, warnings


def qc_masks(run_dir, want_bbox=True):
    out = {"failures": [], "warnings": [], "metrics": {}}
    mask_path, nuclei_path = _resolve_mask_paths(run_dir)
    out["metrics"]["mask_path"] = os.path.basename(mask_path) if mask_path else None
    out["metrics"]["nuclei_path"] = os.path.basename(nuclei_path) if nuclei_path else None
    mask = _open_zarr(mask_path)
    nuclei = _open_zarr(nuclei_path)
    if mask is None or nuclei is None:
        out["failures"].append(
            f"could not open mask/nuclei zarr (mask={mask_path}, nuclei={nuclei_path})")
        return out, None
    info = summarize_masks(mask, nuclei, want_bbox=want_bbox)
    out["metrics"].update(info)
    f, w = check_masks(info)
    out["failures"].extend(f)
    out["warnings"].extend(w)
    return out, (mask, nuclei, info)


# ── 6. tile seam QC ──────────────────────────────────────────────────────────
def _infer_grid(run_dir):
    """Return (n_rows, n_cols) inferred from the log, or None."""
    log_path = _glob_one(run_dir, "segmentation_*.log")
    if not log_path or not os.path.isfile(log_path):
        return None
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    mt = re.search(r"actual=(\d+)x(\d+)", text) or re.search(r"grid=(\d+)x(\d+)", text)
    if mt:
        return int(mt.group(1)), int(mt.group(2))
    return None


def qc_tile_seam(run_dir, mask):
    out = {"failures": [], "warnings": [], "metrics": {}}
    grid = _infer_grid(run_dir)
    out["metrics"]["inferred_grid"] = grid
    if mask is None or grid is None:
        out["metrics"]["seam_checked"] = False
        return out
    n_rows, n_cols = grid
    H, W = (int(mask.shape[0]), int(mask.shape[1]))
    half = SEAM_STRIP_PX // 2

    def _vertical_strip_fraction(xc):
        x0, x1 = max(0, xc - half), min(W, xc + half)
        band = np.asarray(mask[:, x0:x1])
        return float(np.count_nonzero(band)) / float(band.size or 1), int(
            np.unique(band[band > 0]).size)

    def _horizontal_strip_fraction(yc):
        y0, y1 = max(0, yc - half), min(H, yc + half)
        band = np.asarray(mask[y0:y1, :])
        return float(np.count_nonzero(band)) / float(band.size or 1), int(
            np.unique(band[band > 0]).size)

    if n_cols >= 2 and n_rows == 1:
        axis, seam = "vertical", W // 2
        seam_frac, seam_lab = _vertical_strip_fraction(seam)
        left_frac, _ = _vertical_strip_fraction(max(half, seam - 3 * SEAM_STRIP_PX))
        right_frac, _ = _vertical_strip_fraction(min(W - half, seam + 3 * SEAM_STRIP_PX))
        seam_coord = seam
    elif n_rows >= 2 and n_cols == 1:
        axis, seam = "horizontal", H // 2
        seam_frac, seam_lab = _horizontal_strip_fraction(seam)
        left_frac, _ = _horizontal_strip_fraction(max(half, seam - 3 * SEAM_STRIP_PX))
        right_frac, _ = _horizontal_strip_fraction(min(H - half, seam + 3 * SEAM_STRIP_PX))
        seam_coord = seam
    else:
        out["metrics"]["seam_checked"] = False
        out["metrics"]["seam_note"] = f"grid {n_rows}x{n_cols} not a simple 2-tile split"
        return out

    out["metrics"].update({
        "seam_checked": True,
        "seam_axis": axis,
        "seam_coord": int(seam_coord),
        "seam_strip_nonzero_fraction": seam_frac,
        "left_strip_nonzero_fraction": left_frac,
        "right_strip_nonzero_fraction": right_frac,
        "seam_strip_distinct_labels": seam_lab,
    })
    adj = [v for v in (left_frac, right_frac) if v is not None]
    if adj:
        lo, hi = min(adj), max(adj)
        if seam_frac < SEAM_DROP_FACTOR * lo:
            out["warnings"].append(
                f"seam strip nonzero fraction {seam_frac:.4f} drops vs adjacent "
                f"({lo:.4f}/{hi:.4f}) — possible seam gap")
        elif seam_frac > SEAM_SPIKE_FACTOR * hi:
            out["warnings"].append(
                f"seam strip nonzero fraction {seam_frac:.4f} spikes vs adjacent "
                f"({lo:.4f}/{hi:.4f}) — possible seam double-count")
    return out


# ── 7. optional thumbnails ───────────────────────────────────────────────────
def write_thumbnails(run_dir, mask, nuclei, target=512):
    """Max-pool downsample to ~target px on the long side; save .npz. Optional."""
    paths = {}
    for name, arr in (("mask", mask), ("nuclei", nuclei)):
        if arr is None:
            continue
        H, W = int(arr.shape[0]), int(arr.shape[1])
        factor = max(1, max(H, W) // target)
        oh, ow = (H + factor - 1) // factor, (W + factor - 1) // factor
        thumb = np.zeros((oh, ow), dtype=np.uint8)
        for y0 in range(0, H, SCAN_BLOCK_ROWS):
            y1 = min(H, y0 + SCAN_BLOCK_ROWS)
            band = np.asarray(arr[y0:y1, :]) > 0
            for ry in range(y0, y1, factor):
                ry1 = min(y1, ry + factor)
                row = band[ry - y0:ry1 - y0]
                # column max-pool
                cols = ow
                for cx in range(cols):
                    x0, x1 = cx * factor, min(W, (cx + 1) * factor)
                    if row[:, x0:x1].any():
                        thumb[ry // factor, cx] = 1
        p = os.path.join(run_dir, f"{name}_downsampled_{target}.npz")
        np.savez_compressed(p, thumb=thumb, factor=factor, shape=(H, W))
        paths[name] = os.path.basename(p)
    return paths


# ── orchestration ────────────────────────────────────────────────────────────
def run_qc(run_dir, want_bbox=True, want_thumbnails=False):
    run_dir = os.path.abspath(run_dir)
    report = {"run_dir": run_dir, "overall_status": "pass",
              "failures": [], "warnings": [], "metrics": {}, "sections": {}}

    sections = {
        "runtime": qc_runtime(run_dir),
        "provenance": qc_provenance(run_dir),
        "source_fallback": qc_source_fallback(run_dir),
    }
    masks_out, mask_bundle = qc_masks(run_dir, want_bbox=want_bbox)
    sections["masks"] = masks_out
    mask_arr = mask_bundle[0] if mask_bundle else None
    nuclei_arr = mask_bundle[1] if mask_bundle else None
    sections["tile_seam"] = qc_tile_seam(run_dir, mask_arr)

    if want_thumbnails and mask_arr is not None:
        try:
            report["metrics"]["thumbnails"] = write_thumbnails(run_dir, mask_arr, nuclei_arr)
        except Exception as exc:
            sections["masks"]["warnings"].append(f"thumbnail generation failed: {exc}")

    for name, sec in sections.items():
        report["sections"][name] = sec
        for f in sec.get("failures", []):
            report["failures"].append(f"[{name}] {f}")
        for w in sec.get("warnings", []):
            report["warnings"].append(f"[{name}] {w}")
        for k, v in sec.get("metrics", {}).items():
            report["metrics"][f"{name}.{k}"] = v

    if report["failures"]:
        report["overall_status"] = "fail"
    elif report["warnings"]:
        report["overall_status"] = "warning"
    return report


def _interpretation(report):
    st = report["overall_status"]
    if st == "fail":
        return ("FAIL: " + "; ".join(report["failures"][:5]))
    if st == "warning":
        return ("WARNING: " + "; ".join(report["warnings"][:5]))
    return "PASS: numerical QC did not detect obvious failure"


def render_markdown(report):
    m = report["metrics"]
    lines = ["# Step2 Remap QC Report", "", f"- run_dir: `{report['run_dir']}`",
             f"- overall_status: **{report['overall_status'].upper()}**", ""]

    lines.append("## Verdict")
    lines.append("")
    lines.append(_interpretation(report))
    lines.append("")

    if report["failures"]:
        lines.append("## Failures")
        lines += [f"- {x}" for x in report["failures"]] + [""]
    if report["warnings"]:
        lines.append("## Warnings")
        lines += [f"- {x}" for x in report["warnings"]] + [""]

    def row(label, key):
        if key in m and m[key] is not None:
            lines.append(f"- {label}: {m[key]}")

    lines.append("## Runtime")
    for lbl, k in (("method", "runtime.method"), ("status", "runtime.status"),
                   ("elapsed", "runtime.elapsed"),
                   ("tile label counts", "runtime.tile_label_counts"),
                   ("ERROR lines", "runtime.n_error_lines"),
                   ("Traceback lines", "runtime.n_traceback_lines")):
        row(lbl, k)
    lines.append("")

    lines.append("## Remap provenance")
    for lbl, k in (("manual_remap_enabled", "provenance.manual_remap_enabled"),
                   ("allow_preview_remap", "provenance.allow_preview_remap"),
                   ("gate_mode", "provenance.gate_mode"),
                   ("used_for", "provenance.used_for"),
                   ("config_hash", "provenance.channel_remap_config_hash"),
                   ("config_path", "provenance.channel_remap_config_path"),
                   ("intensity_space", "provenance.source_intensity_space"),
                   ("preview_only", "provenance.source_preview_only"),
                   ("step2_pre_remap_source", "provenance.source_step2_pre_remap_source"),
                   ("hq_channels", "provenance.hq_channels")):
        row(lbl, k)
    lines.append("")

    lines.append("## Mask / nuclei")
    for lbl, k in (("mask_shape", "masks.mask_shape"), ("nuclei_shape", "masks.nuclei_shape"),
                   ("dtype", "masks.mask_dtype"), ("mask_max_label", "masks.mask_max_label"),
                   ("nuclei_max_label", "masks.nuclei_max_label"),
                   ("nonzero_fraction", "masks.mask_nonzero_fraction"),
                   ("nonzero_blocks", "masks.nonzero_blocks_sampled"),
                   ("shape_match", "masks.shape_match"),
                   ("max_label_match", "masks.max_label_match")):
        row(lbl, k)
    lines.append("")

    lines.append("## Label consistency")
    for lbl, k in (("sampled_labels", "masks.sampled_labels"),
                   ("median_cell_area", "masks.median_cell_area"),
                   ("median_nucleus_area", "masks.median_nucleus_area"),
                   ("median_cell_to_nucleus_ratio", "masks.median_cell_to_nucleus_area_ratio"),
                   ("cell_area_p01/p99", None),
                   ("fraction_nucleus_pixels_inside_cell", "masks.fraction_nucleus_pixels_inside_cell"),
                   ("fraction_cells_without_nucleus", "masks.fraction_cells_without_nucleus"),
                   ("fraction_nuclei_without_cell", "masks.fraction_nuclei_without_cell")):
        if k is None:
            p1 = m.get("masks.cell_area_p01"); p99 = m.get("masks.cell_area_p99")
            if p1 is not None:
                lines.append(f"- cell_area p01/p99: {p1} / {p99}")
            continue
        row(lbl, k)
    lines.append("")

    if m.get("tile_seam.seam_checked"):
        lines.append("## Tile seam")
        for lbl, k in (("grid", "tile_seam.inferred_grid"), ("axis", "tile_seam.seam_axis"),
                       ("seam_coord", "tile_seam.seam_coord"),
                       ("seam_strip_nonzero_fraction", "tile_seam.seam_strip_nonzero_fraction"),
                       ("left_strip_nonzero_fraction", "tile_seam.left_strip_nonzero_fraction"),
                       ("right_strip_nonzero_fraction", "tile_seam.right_strip_nonzero_fraction")):
            row(lbl, k)
        lines.append("")
    return "\n".join(lines)


def write_reports(run_dir, report):
    json_path = os.path.join(run_dir, "step2_remap_qc_report.json")
    md_path = os.path.join(run_dir, "step2_remap_qc_report.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    return json_path, md_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, help="Step2 segmentation run directory")
    ap.add_argument("--no-bbox", action="store_true", help="skip per-label bbox stats")
    ap.add_argument("--thumbnails", action="store_true",
                    help="also write mask/nuclei downsampled .npz summaries")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.run_dir):
        print(f"ERROR: run-dir not found: {args.run_dir}")
        return 2
    if zarr is None:
        print("WARNING: zarr not importable; mask QC will be skipped/failed.")

    report = run_qc(args.run_dir, want_bbox=not args.no_bbox,
                    want_thumbnails=args.thumbnails)
    json_path, md_path = write_reports(os.path.abspath(args.run_dir), report)
    print(f"overall_status: {report['overall_status'].upper()}")
    print(_interpretation(report))
    print(f"JSON: {json_path}")
    print(f"MD:   {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
