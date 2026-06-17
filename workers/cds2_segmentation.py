"""CDS2 cytoplasm engine: lean_carve + marker-fingerprint mutual-exclusion.

CDS2 reuses lean_carve's geometric Voronoi territory and per-channel free-boundary
outside-in carve verbatim (same block-streaming, constant memory), then adds ONE
extra veto layer to fix lean_carve/union being too greedy ("can't spit out"): an
epithelial (HsBAg/CK19) cell whose territory reaches a touching macrophage
(CD68/CD163) tail eats that tail, producing fake HsBAg+/CD68+ double-positives.

Fix = mutual-exclusion arbitration:
  - Each nucleus gets a single "camp" fingerprint from its 3 px peri-nuclear ring
    (area gate >= cds2_area_frac, then identity = ring-fraction x mid-percentile z).
  - Each candidate pixel gets a camp from its 3x3 local-z neighbourhood.
  - If the pixel's camp conflicts with the owning nucleus's camp (different,
    mutually-exclusive camps) the claim is vetoed -> pixel becomes a boundary.
  - Safety valve: nucleus with no fingerprint OR pixel with no dominant camp ->
    no constraint, behaviour is identical to lean_carve. Only a both-sides-clear
    conflict vetoes. This is the ONLY change vs lean_carve.

Everything stays inside the block (no whole-image intermediates): the camp z-maps,
dominant-camp maps and fingerprint dict are all per-block and discarded each block.
"""

import numpy as np
from scipy import ndimage as _ndi

from .constrained_donut_segmentation import _params
from .lean_carve_segmentation import (
    _run_streaming,
    _block_geometry,
    _block_gi,
    _channel_keep,
    _finalize_block,
)
from ..core.channel_remap import apply_channel_remap

# Mutually-exclusive camps: markers within a camp coexist; camps are pairwise
# exclusive. Channels not listed here (and DAPI) carry no fingerprint.
DEFAULT_CDS2_CAMPS = {
    "MACRO": {"CD68", "CD163"},
    "TCELL": {"CD3D", "CD4", "CD8", "CD45RA", "CD45RO"},
    "EPI": {"HsBAg", "CK19"},
}


def _norm(name):
    return str(name or "").upper().replace("-", "").replace("_", "")


def _camp_id_map(camps):
    """camp name -> small int id, and normalised-marker -> camp id."""
    camp_id = {}
    marker_to_id = {}
    for i, (cname, markers) in enumerate(camps.items(), start=1):
        camp_id[cname] = i
        for m in markers:
            marker_to_id[_norm(m)] = i
    return camp_id, marker_to_id


def _make_cds2_carve(p):
    camps = p.get("cds2_camps") or DEFAULT_CDS2_CAMPS
    area_frac = float(p.get("cds2_area_frac", 0.20) or 0.20)
    strength_lo = float(p.get("cds2_strength_lo", 0.50) or 0.50)
    strength_hi = float(p.get("cds2_strength_hi", 0.90) or 0.90)
    _camp_id, marker_to_id = _camp_id_map(camps)

    def carve(sub_nuclei, names, get_block, y0, y1, x0, x1, sy0, sx0,
              max_radius, minimal_radius, separate_px, tau, fusion_mode, vote_k,
              gi_k, gi_bg_k, use_gpu,
              remap_params=None, gate_mode="remap", gate_thr=0.05):
        terr, terr_mask, minimal, protect, outside, faces = _block_geometry(
            sub_nuclei, max_radius, minimal_radius, use_gpu)

        union_mode = str(fusion_mode or "union").strip().lower() != "vote"
        fused = np.zeros(terr.shape, dtype=bool)
        votes = None if union_mode else np.zeros(terr.shape, dtype=np.uint16)
        used_cupy = False

        # Per-camp max-z maps (only camps actually present among channels).
        present_ids = sorted({marker_to_id[_norm(n)] for n in names if _norm(n) in marker_to_id})
        camp_z = {cid: np.zeros(terr.shape, dtype=np.float32) for cid in present_ids}

        sh, sw = sub_nuclei.shape
        for name in names:
            raw = get_block(name, sy0, sy0 + sh, sx0, sx0 + sw)
            if raw is None:
                continue
            # gi is ALWAYS computed: camp arbitration requires the native local-z
            # contrast map (doc 09 — manual remap must not replace _block_gi/camp).
            gi, uc = _block_gi(raw, terr_mask, gi_k, gi_bg_k, use_gpu)
            used_cupy = used_cupy or uc
            # Signal gate: from the manual remap 0–1 conditioning map when active,
            # else the gi-based gate (identical to lean). camp still uses gi below.
            use_remap = bool(remap_params) and name in remap_params and gate_mode != "gi"
            if use_remap:
                cond = apply_channel_remap(raw, remap_params[name])
                keep = _channel_keep(cond, terr_mask, outside, faces, protect, gate_thr)
                if gate_mode == "remap_and_gi":
                    keep = keep & _channel_keep(gi, terr_mask, outside, faces, protect, tau)
                del cond
            else:
                keep = _channel_keep(gi, terr_mask, outside, faces, protect, tau)
            if union_mode:
                fused |= keep
            else:
                votes += keep.astype(np.uint16)
            cid = marker_to_id.get(_norm(name))
            if cid is not None:
                np.maximum(camp_z[cid], gi, out=camp_z[cid])  # camp: native gi, unchanged
            del raw, gi, keep
        if not union_mode:
            fused = votes >= max(1, int(vote_k))
            del votes

        kept = (fused & terr_mask) | (protect & terr_mask)   # anti-collapse floor

        # ---- mutual-exclusion arbitration (the only change vs lean_carve) ----
        if camp_z:
            # Per-pixel dominant camp (best z >= tau) for the ring fingerprint.
            best_z = np.zeros(terr.shape, dtype=np.float32)
            best_camp = np.zeros(terr.shape, dtype=np.uint8)
            for cid, z in camp_z.items():
                upd = z > best_z
                best_z = np.where(upd, z, best_z)
                best_camp = np.where(upd, np.uint8(cid), best_camp)
            dom = np.where(best_z >= tau, best_camp, np.uint8(0))

            # Nucleus fingerprint from its 3 px peri-nuclear ring.
            fp = {}
            labs = np.unique(sub_nuclei)
            for lab in labs.tolist():
                if lab == 0:
                    continue
                ring = (minimal == lab) & (sub_nuclei == 0)
                ring_total = int(np.count_nonzero(ring))
                if ring_total == 0:
                    continue
                ring_dom = dom[ring]
                ring_z = best_z[ring]
                best_score = -1.0
                best_cid = 0
                for cid in camp_z.keys():
                    sel = ring_dom == cid
                    cnt = int(np.count_nonzero(sel))
                    frac = cnt / float(ring_total)
                    if frac < area_frac:                       # area gate
                        continue
                    zz = ring_z[sel]
                    qlo = float(np.quantile(zz, strength_lo))
                    qhi = float(np.quantile(zz, strength_hi))
                    mids = zz[(zz >= qlo) & (zz <= qhi)]
                    strength = float(mids.mean()) if mids.size else float(zz.mean())
                    score = frac * strength
                    if score > best_score:
                        best_score = score
                        best_cid = cid
                if best_cid > 0:
                    fp[int(lab)] = best_cid

            if fp:
                # Pixel fingerprint from a 3x3 max-z neighbourhood per camp.
                pix_best_z = np.zeros(terr.shape, dtype=np.float32)
                pix_camp = np.zeros(terr.shape, dtype=np.uint8)
                for cid, z in camp_z.items():
                    z3 = _ndi.grey_dilation(z, size=3)
                    upd = z3 > pix_best_z
                    pix_best_z = np.where(upd, z3, pix_best_z)
                    pix_camp = np.where(upd, np.uint8(cid), pix_camp)
                pix_fp = np.where(pix_best_z >= tau, pix_camp, np.uint8(0))

                fp_map = np.zeros(terr.shape, dtype=np.uint8)
                for lab, cid in fp.items():
                    fp_map[terr == lab] = cid

                veto = (fp_map > 0) & (pix_fp > 0) & (pix_fp != fp_map) & terr_mask & ~protect
                kept &= ~veto
                del pix_best_z, pix_camp, pix_fp, fp_map, veto
            del best_z, best_camp, dom
        for cid in list(camp_z.keys()):
            del camp_z[cid]

        interior = _finalize_block(kept, terr, sub_nuclei, minimal, protect, separate_px,
                                   y0, y1, x0, x1, sy0, sx0)
        return interior, used_cupy

    return carve


def run_cds2_segmentation(
    nuclei_labels,
    marker_channels_loader,
    channel_names,
    params=None,
    output_labels=None,
    logger=None,
    progress_callback=None,
    cancel_check=None,
):
    """lean_carve streaming + marker-fingerprint mutual-exclusion. CDS return contract."""
    p = _params(params)
    camps = p.get("cds2_camps") or DEFAULT_CDS2_CAMPS
    extra_meta = {
        "cds2_camps": {k: sorted(v) for k, v in camps.items()},
        "cds2_area_frac": float(p.get("cds2_area_frac", 0.20) or 0.20),
        "cds2_strength_lo": float(p.get("cds2_strength_lo", 0.50) or 0.50),
        "cds2_strength_hi": float(p.get("cds2_strength_hi", 0.90) or 0.90),
    }
    return _run_streaming(
        nuclei_labels, marker_channels_loader, channel_names, p, output_labels,
        logger, progress_callback, cancel_check,
        carve_fn=_make_cds2_carve(p), engine_name="cds2", extra_meta=extra_meta,
    )
