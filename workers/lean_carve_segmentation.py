"""Lean, constant-memory cytoplasm engine for CDS ("lean_carve").

Profile goal (MACSiQView-like): peak memory barely grows with image size; a
425 MP whole slide runs in seconds-to-minutes. The default CDS path materialises
~20 whole-image arrays (peak ~145 bytes/pixel -> ~113 GB OOM on 425 MP). This
engine never does that: it tiles the image and streams one marker channel of one
block at a time, so the resident set is only

    nuclei input  +  label output  +  a few small per-block temporaries.

Semantics are the same geometric territory + per-channel free-boundary
"outside-in" signal carve + multi-channel fusion as
``outside_in_segmentation``; the difference is purely the streaming, channel-at-a
-time, discard-immediately execution. With ``halo >= max_cell_radius`` every
block's interior pixel sees the full neighbourhood its territory/carve needs, so
the tiled result equals the whole-image result (asserted in tests).
"""

import time

import numpy as np
from scipy import ndimage as _ndi

from .constrained_donut_segmentation import (
    _params,
    _expand_labels,
    _keep_connected_to_seed,
    _uniform_filter,
    _cupy_hotspot_available,
    _cupy,
)
from .outside_in_segmentation import _voronoi_faces
from ..core.channel_remap import apply_channel_remap


def resolve_remap_gate(params):
    """Extract v13.1 remap gate settings from the engine params dict.

    Returns (remap_params|None, gate_mode, gate_thr). With no active remap config
    remap_params is None and callers fall back to the unchanged gi-based gate.
    """
    remap_params = (params or {}).get("_channel_remap_params") or None
    gate_mode = str((params or {}).get("_remap_gate_mode", "remap") or "remap").strip().lower()
    gate_thr = float((params or {}).get("_remap_gate_threshold", 0.05) or 0.05)
    if gate_mode not in ("remap", "gi", "remap_and_gi"):
        gate_mode = "remap"
    return remap_params, gate_mode, gate_thr

_FULL8 = np.ones((3, 3), dtype=bool)


def _expand_labels_gpu(labels, distance):
    """GPU territory via cupy EDT nearest-label; returns None to signal fallback.

    Mirrors skimage.segmentation.expand_labels (nearest label within ``distance``,
    label pixels kept). On well-separated cells (no equidistant ridges) this is
    pixel-identical to the CPU path.
    """
    if _cupy is None or not _cupy_hotspot_available():
        return None
    try:
        from cupyx.scipy import ndimage as _cndi
        lab = _cupy.asarray(np.asarray(labels, dtype=np.uint32))
        bg = lab == 0
        dist, inds = _cndi.distance_transform_edt(bg, return_indices=True)
        nearest = lab[tuple(inds)]
        out = _cupy.where(dist <= _cupy.float32(distance), nearest, _cupy.uint32(0))
        res = _cupy.asnumpy(out).astype(np.uint32, copy=False)
        del lab, bg, dist, inds, nearest, out
        _cupy.get_default_memory_pool().free_all_blocks()
        return res
    except Exception:
        return None


def _territory_expand(labels, distance, use_gpu):
    """_expand_labels on GPU when available, else the existing skimage CPU path."""
    if use_gpu:
        gpu = _expand_labels_gpu(labels, distance)
        if gpu is not None:
            return gpu
    return _expand_labels(labels, distance).astype(np.uint32, copy=False)


def _make_block_getter(marker_channels, channel_names):
    """Return a callable (name, y0, y1, x0, x1) -> 2D float32 block, or None.

    Accepts either a real lazy loader (already callable) or an in-memory list of
    whole-image arrays. In the list case we keep references (no copy, no stacking)
    and slice per request, so no extra whole-image array is created here.
    """
    if callable(marker_channels):
        return marker_channels
    arrs = {}
    for name, arr in zip(channel_names, marker_channels or []):
        arrs[name] = np.asarray(arr)
    def getter(name, y0, y1, x0, x1):
        a = arrs.get(name)
        if a is None:
            return None
        return np.asarray(a[y0:y1, x0:x1], dtype=np.float32)
    return getter


def _block_gi(raw, territory_mask, k, bg_k, use_gpu):
    """Per-channel Gi* z-map for one block (local mean vs large-kernel background)."""
    local_mean, b1 = _uniform_filter(raw, k, use_gpu)
    bg, b2 = _uniform_filter(raw, bg_k, use_gpu)
    sq, b3 = _uniform_filter(raw * raw, bg_k, use_gpu)
    var = np.maximum(sq - bg * bg, np.float32(0.0))
    std = np.sqrt(var)
    gi = (local_mean - bg) / (std + np.float32(1.0e-3))
    gi = np.where(territory_mask, gi, np.float32(0.0))
    gi = np.where(np.isfinite(gi), gi, np.float32(0.0))
    gi = np.maximum(gi, np.float32(0.0)).astype(np.float32, copy=False)
    used_cupy = "cupy" in (b1, b2, b3)
    return gi, used_cupy


def _block_geometry(sub_nuclei, max_radius, minimal_radius, use_gpu):
    """Geometric territory / minimal-keep / protect / outside / Voronoi faces."""
    terr = _territory_expand(sub_nuclei, max_radius, use_gpu)
    terr[sub_nuclei > 0] = sub_nuclei[sub_nuclei > 0]
    terr_mask = terr > 0

    minimal = _territory_expand(sub_nuclei, minimal_radius, use_gpu)
    minimal[~terr_mask] = 0
    minimal[sub_nuclei > 0] = sub_nuclei[sub_nuclei > 0]

    protect = (sub_nuclei > 0) | (minimal > 0)
    outside = ~terr_mask
    faces = _voronoi_faces(terr) & ~protect
    return terr, terr_mask, minimal, protect, outside, faces


def _channel_keep(gi, terr_mask, outside, faces, protect, tau):
    """One channel's outside-in keep mask (free-boundary carve through low-z bg)."""
    low = gi < tau
    carveable = outside | (low & terr_mask & ~faces & ~protect)
    reached = _ndi.binary_propagation(outside, mask=carveable, structure=_FULL8)
    return (terr_mask & ~(terr_mask & reached)) | (protect & terr_mask)


# v13.1 Phase 5d.1 — remap gate shape alignment. The manual-remap marker source
# (native/corrected, normalize=False) can be read on a slightly different pixel
# grid than the nuclei-derived block geometry (terr_mask/gi/protect/faces). When
# the gate source block is larger, center-crop it to the trusted geometry shape;
# never resize, pad, or alter intensities. One log line per run keeps it quiet.
_ALIGN_LOG_STATE = {"logged": False}


def _center_crop_to(arr, th, tw):
    """Center-crop a 2D array to (th, tw). Caller guarantees arr is >= target."""
    h, w = arr.shape[:2]
    y0 = (h - th) // 2
    x0 = (w - tw) // 2
    return arr[y0:y0 + th, x0:x0 + tw]


def _align_remap_gate_to_reference(cond, reference, *, channel_name="", context="",
                                   gate_mode="", block_coords=None,
                                   extra_shapes=None):
    """Align a remap gate source (`cond`) to the trusted geometry `reference`.

    Direction is one-way: cond -> reference. The reference is the existing
    lean_carve block geometry (terr_mask / gi), which the legacy gi gate already
    runs against, so it is the trusted shape. Behavior:

        cond.shape == reference.shape            -> return cond unchanged
        cond >= reference in BOTH dims           -> center-crop cond, return it
        cond smaller than reference in any dim   -> raise ValueError (diagnostic)

    Never resize/interpolate, never pad, never change intensity values.
    """
    rh, rw = reference.shape[:2]
    ch, cw = cond.shape[:2]
    if (ch, cw) == (rh, rw):
        return cond

    diag = (f"channel={channel_name!r} context={context!r} gate_mode={gate_mode!r} "
            f"block_coords={block_coords} raw/cond_shape={tuple(cond.shape)} "
            f"reference_shape={tuple(reference.shape)}")
    if extra_shapes:
        diag += " " + " ".join(f"{k}_shape={tuple(v)}" for k, v in extra_shapes.items())

    if ch >= rh and cw >= rw:
        aligned = _center_crop_to(cond, rh, rw)
        if not _ALIGN_LOG_STATE["logged"]:
            _ALIGN_LOG_STATE["logged"] = True
            print(f"[Step2-Remap] gate shape aligned channel={channel_name} "
                  f"raw={tuple(cond.shape)} cond={tuple(cond.shape)} "
                  f"reference={tuple(reference.shape)} aligned={tuple(aligned.shape)} "
                  f"mode={gate_mode}")
        return aligned

    raise ValueError(
        "remap gate source smaller than block geometry; cannot align by cropping "
        "(refusing to pad/resize). " + diag)


def _finalize_block(kept, terr, sub_nuclei, minimal, protect, separate_px,
                    y0, y1, x0, x1, sy0, sx0):
    """Connect-to-seed, inter-cell gap, nucleus reassert; return interior crop."""
    labels = np.where(kept, terr, 0).astype(np.uint32, copy=False)
    labels = _keep_connected_to_seed(labels, sub_nuclei, minimal, terr)
    if separate_px > 0:
        gap = _ndi.binary_dilation(_voronoi_faces(labels), iterations=int(separate_px))
        gap &= (labels > 0) & ~protect
        labels[gap] = 0
    labels[sub_nuclei > 0] = sub_nuclei[sub_nuclei > 0]
    iy0, iy1 = y0 - sy0, y1 - sy0
    ix0, ix1 = x0 - sx0, x1 - sx0
    return labels[iy0:iy1, ix0:ix1].copy()


def _carve_block(
    sub_nuclei,
    names,
    get_block,
    y0, y1, x0, x1,          # full-image interior bounds
    sy0, sx0,                # full-image origin of the sub (halo) block
    max_radius,
    minimal_radius,
    separate_px,
    tau,
    fusion_mode,
    vote_k,
    gi_k,
    gi_bg_k,
    use_gpu,
    remap_params=None,
    gate_mode="remap",
    gate_thr=0.05,
):
    """Segment one halo-padded sub-block; return (interior_labels, used_cupy).

    v13.1: when remap_params supplies a channel, the signal gate is derived from
    the manual remap 0–1 conditioning map (low = cond < gate_thr) instead of the
    gi-based gate. lean_carve has no camp, so gi is only needed for the default
    gate and is skipped when remap drives the gate. See
    docs/v13_1_channel_conditioning/11_STEP2_REMAP_INTEGRATION_AUDIT.md.
    """
    terr, terr_mask, minimal, protect, outside, faces = _block_geometry(
        sub_nuclei, max_radius, minimal_radius, use_gpu)

    union_mode = str(fusion_mode or "union").strip().lower() != "vote"
    fused = np.zeros(terr.shape, dtype=bool)
    votes = None if union_mode else np.zeros(terr.shape, dtype=np.uint16)
    used_cupy = False

    sh, sw = sub_nuclei.shape
    for name in names:
        raw = get_block(name, sy0, sy0 + sh, sx0, sx0 + sw)
        if raw is None:
            continue
        use_remap = bool(remap_params) and name in remap_params and gate_mode != "gi"
        if use_remap:
            # Align the gate source block to the trusted block geometry shape
            # (terr_mask) BEFORE remap/keep so cond — and, for remap_and_gi, gi —
            # are spatially consistent with terr_mask/outside/faces/protect.
            # Direction is cond -> reference only; geometry is never reshaped.
            raw = _align_remap_gate_to_reference(
                raw, terr_mask, channel_name=name, context="lean_carve",
                gate_mode=gate_mode, block_coords=(sy0, sx0),
                extra_shapes={"terr_mask": terr_mask.shape,
                              "protect": protect.shape,
                              "outside": outside.shape,
                              "faces": faces.shape})
            cond = apply_channel_remap(raw, remap_params[name])
            keep = _channel_keep(cond, terr_mask, outside, faces, protect, gate_thr)
            if gate_mode == "remap_and_gi":
                gi, uc = _block_gi(raw, terr_mask, gi_k, gi_bg_k, use_gpu)
                used_cupy = used_cupy or uc
                keep = keep & _channel_keep(gi, terr_mask, outside, faces, protect, tau)
                del gi
            del cond
        else:
            gi, uc = _block_gi(raw, terr_mask, gi_k, gi_bg_k, use_gpu)
            used_cupy = used_cupy or uc
            keep = _channel_keep(gi, terr_mask, outside, faces, protect, tau)
            del gi
        if union_mode:
            fused |= keep
        else:
            votes += keep.astype(np.uint16)
        del raw, keep
    if not union_mode:
        fused = votes >= max(1, int(vote_k))
        del votes

    kept = (fused & terr_mask) | (protect & terr_mask)   # anti-collapse floor
    interior = _finalize_block(kept, terr, sub_nuclei, minimal, protect, separate_px,
                               y0, y1, x0, x1, sy0, sx0)
    return interior, used_cupy


def run_lean_carve_segmentation(
    nuclei_labels,
    marker_channels_loader,
    channel_names,
    params=None,
    output_labels=None,
    logger=None,
    progress_callback=None,
    cancel_check=None,
):
    """Tiled, streaming outside-in carve. Same return contract as CDS."""
    return _run_streaming(
        nuclei_labels, marker_channels_loader, channel_names, params, output_labels,
        logger, progress_callback, cancel_check,
        carve_fn=_carve_block, engine_name="lean_carve",
    )


def _run_streaming(
    nuclei_labels,
    marker_channels_loader,
    channel_names,
    params=None,
    output_labels=None,
    logger=None,
    progress_callback=None,
    cancel_check=None,
    carve_fn=_carve_block,
    engine_name="lean_carve",
    extra_meta=None,
):
    """Shared block-streaming driver; ``carve_fn`` is the per-block segmenter."""
    started = time.perf_counter()
    p = _params(params)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    names = list(channel_names or [])

    max_radius = int(p.get("max_cell_radius") or p.get("donut_size") or 18)
    minimal_radius = int(p.get("minimal_radius", 3) or 0)
    block = int(p.get("lean_block_size", 4096) or 4096)
    halo = max_radius + int(p.get("lean_halo_margin", 8) or 0)
    separate_px = int(p.get("shrink_pixels", p.get("nucleus_shrink", 2)) or 0)
    tau = float(p.get("outside_in_z_threshold", 0.5) or 0.5)
    fusion_mode = str(p.get("fusion_mode", "union") or "union")
    vote_k = int(p.get("vote_k", 2) or 2)
    gi_k = max(3, int(p.get("gi_kernel_size", 7) or 7))
    gi_bg_k = max(gi_k + 2, int(p.get("gi_background_kernel_size", 31) or 31))
    use_gpu = str(p.get("use_gpu", True)).strip().lower() not in {"0", "false", "no", "off", "cpu"}
    remap_params, gate_mode, gate_thr = resolve_remap_gate(p)
    _ALIGN_LOG_STATE["logged"] = False  # one gate-align log line per run

    if output_labels is None:
        out = np.zeros(nuclei.shape, dtype=np.uint32)
    else:
        out = output_labels
        out[...] = 0

    get_block = _make_block_getter(marker_channels_loader, names)
    h, w = nuclei.shape
    nucleus_area = {}
    final_area = {}
    n_blocks = max(1, ((h + block - 1) // block) * ((w + block - 1) // block))
    done = 0
    backend = "numpy"

    if int(nuclei.max()) != 0:
        for y0 in range(0, h, block):
            for x0 in range(0, w, block):
                if cancel_check is not None and cancel_check():
                    raise RuntimeError("lean_carve cancelled")
                y1, x1 = min(h, y0 + block), min(w, x0 + block)
                sy0, sy1 = max(0, y0 - halo), min(h, y1 + halo)
                sx0, sx1 = max(0, x0 - halo), min(w, x1 + halo)
                sub_nuclei = nuclei[sy0:sy1, sx0:sx1]
                done += 1
                if int(sub_nuclei.max()) == 0:
                    continue
                interior, used_cupy = carve_fn(
                    sub_nuclei, names, get_block,
                    y0, y1, x0, x1, sy0, sx0,
                    max_radius, minimal_radius, separate_px, tau, fusion_mode, vote_k,
                    gi_k, gi_bg_k, use_gpu,
                    remap_params=remap_params, gate_mode=gate_mode, gate_thr=gate_thr,
                )
                out[y0:y1, x0:x1] = interior
                # Cheap per-cell areas accumulated per block (no whole-image QC layers).
                nlab, ncnt = np.unique(nuclei[y0:y1, x0:x1], return_counts=True)
                for lab, c in zip(nlab.tolist(), ncnt.tolist()):
                    if lab:
                        nucleus_area[lab] = nucleus_area.get(lab, 0) + int(c)
                flab, fcnt = np.unique(interior, return_counts=True)
                for lab, c in zip(flab.tolist(), fcnt.tolist()):
                    if lab:
                        final_area[lab] = final_area.get(lab, 0) + int(c)
                if used_cupy and _cupy is not None and _cupy_hotspot_available():
                    _cupy.get_default_memory_pool().free_all_blocks()
                    backend = "cupy"
                del interior
                if progress_callback is not None:
                    progress_callback(f"[{engine_name}] block {done}/{n_blocks}")

    qc_rows = []
    for lab in sorted(nucleus_area.keys()):
        nuc = int(nucleus_area.get(lab, 0))
        fin = int(final_area.get(lab, 0))
        qc_rows.append({
            "cell_id": int(lab),
            "nucleus_area": nuc,
            "final_area": fin,
            "refined_area": fin,
            "cell_to_nucleus_ratio": float(fin) / max(float(nuc), 1.0),
            "refinement_applied": bool(fin > nuc),
            "signal_mode": engine_name,
            "cytoplasm_engine": engine_name,
            "fusion_mode": str(fusion_mode).strip().lower(),
        })

    total_seconds = time.perf_counter() - started
    stats = {
        "cells_refined": int(sum(1 for r in qc_rows if r["refinement_applied"])),
        "cells_fallback": 0,
        "added_pixels_total": int(sum(max(0, r["final_area"] - r["nucleus_area"]) for r in qc_rows)),
        "cell_count": len(qc_rows),
    }
    metadata = {
        "engine": engine_name,
        "cytoplasm_engine": engine_name,
        "csd_mode": f"{engine_name}_streaming_outside_in",
        "hq2_mode": f"{engine_name}_streaming_outside_in",
        "lean_block_size": int(block),
        "lean_halo_margin": int(p.get("lean_halo_margin", 8) or 0),
        "lean_halo": int(halo),
        "max_cell_radius": int(max_radius),
        "minimal_radius": int(minimal_radius),
        "outside_in_z_threshold": float(tau),
        "fusion_mode": str(fusion_mode).strip().lower(),
        "vote_k": int(vote_k),
        "separate_px": int(separate_px),
        "csd_backend": backend,
        "gi_backend": backend,
        "timings": {"total_seconds": total_seconds},
        "channel_thresholds": {},
    }
    if extra_meta:
        metadata.update(extra_meta)
    if logger is not None:
        try:
            logger.info(f"[{engine_name}] cells={len(qc_rows)} seconds={total_seconds:.3f} backend={backend}")
        except Exception:
            pass

    return {
        "final_labels": out.astype(np.uint32, copy=False) if not isinstance(out, np.memmap) else out,
        "nuclei_labels": nuclei.astype(np.uint32, copy=False),
        "qc_rows": qc_rows,
        "stats": stats,
        "metadata": metadata,
    }
