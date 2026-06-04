"""Outside-in (territory-shrinking) cytoplasm segmentation for CDS.

Design contract
---------------
1. Each nucleus owns a Voronoi-bounded territory (the geometric upper bound,
   reused from CDS: ``build_nucleus_owned_territory``).
2. For every *selected high-quality channel* we shrink that territory from the
   OUTSIDE inward, eating only pixels that are confident background for that
   channel and that are reachable from true tissue background (territory == 0).
   We never cross a Voronoi face between two cells, and we never eat into the
   nucleus or the minimal keep ring -> no collapse, no cross-cell spill.
3. The per-channel keeps are fused (union / vote / distance-graded). The final
   cell extent is the fused envelope, clipped to the territory.

Why this fixes the two CDS failure modes
-----------------------------------------
- "Collapses onto the nucleus": the inside-out flood fill gated cytoplasm on a
  GLOBAL ``raw >= auto_low`` threshold, so weak cells lost everything. Here the
  default is to KEEP; we only remove pixels we are confident are background, so
  a faint-but-visible cell keeps its visible extent.
- "Blows up into a circle": an isolated cell's territory is a disk of radius
  max_cell_radius with no neighbour to bound it. Inside-out fill flooded the
  whole disk. Outside-in starts from the disk and carves inward through the
  low-signal rim until it hits the visible contour, so the disk becomes the
  real cell shape.

The local-contrast criterion is the per-channel Gi* z-map already produced by
``build_multichannel_gi_star`` (local_mean vs local background, in sigma units),
which is exactly the "is this pixel clearly above its local background" test the
eye performs -- not an absolute intensity threshold.
"""

import numpy as np
from scipy import ndimage as _ndi

# Reuse CDS helpers / conventions so this is a true drop-in.
from .constrained_donut_segmentation import (  # noqa: F401
    _params,
    _expand_labels,
    _keep_connected_to_seed,
    _drop_small_per_cell,
    build_multichannel_gi_star,
)

_FULL8 = np.ones((3, 3), dtype=bool)


def _voronoi_faces(base_territory):
    """Pixels sitting on a boundary between two different territories."""
    labels = np.asarray(base_territory, dtype=np.uint32)
    maxf = _ndi.maximum_filter(labels, size=3)
    minf = _ndi.minimum_filter(labels, size=3)
    # A territory pixel whose 3x3 neighbourhood touches a *different* positive id.
    face = (labels > 0) & (maxf != labels) & (maxf > 0) & (minf != maxf)
    # Also catch the symmetric case where the neighbour is the smaller id.
    face |= (labels > 0) & (minf != labels) & (minf > 0)
    return face


def build_outside_in_keep_per_channel(
    base_territory,
    nuclei_labels,
    minimal_keep_labels,
    per_channel_gi,
    params=None,
):
    """Carve each channel's keep mask from the free boundary inward.

    Returns ``{channel_name: bool_keep_mask}`` where each mask is a subset of
    the territory (cytoplasm + nucleus footprint for that channel).
    """
    p = _params(params)
    base = np.asarray(base_territory, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    minimal = np.asarray(minimal_keep_labels, dtype=np.uint32)

    base_mask = base > 0
    outside = ~base_mask                      # true tissue background = carve seed
    protect = (nuclei > 0) | (minimal > 0)    # never eat below the minimal donut
    faces = _voronoi_faces(base) & ~protect   # never carve across shared faces

    tau = float(p.get("outside_in_z_threshold", 0.5) or 0.5)
    # Optionally derive tau per channel from the in-territory z distribution
    # (keeps behaviour stable across markers with different SNR).
    auto_tau = bool(p.get("outside_in_auto_threshold", False))
    auto_pct = float(p.get("outside_in_auto_percentile", 35.0) or 35.0)

    keeps = {}
    for name, gi in (per_channel_gi or {}).items():
        gi = np.asarray(gi, dtype=np.float32)
        if auto_tau:
            vals = gi[base_mask]
            t = float(np.percentile(vals, auto_pct)) if vals.size else tau
            t = max(t, 1.0e-3)
        else:
            t = tau
        low = (gi < t)                        # confident background for this channel
        # Pixels the "outside" is allowed to flow through:
        carveable = outside | (low & base_mask & ~faces & ~protect)
        # Morphological reconstruction: how far outside-ness reaches inward.
        reached = _ndi.binary_propagation(outside, mask=carveable, structure=_FULL8)
        removed = base_mask & reached
        keep = base_mask & ~removed
        keep |= protect & base_mask           # guarantee nucleus + minimal ring kept
        keeps[name] = keep
    return keeps


def fuse_outside_in_keeps(
    keeps_by_channel,
    base_territory,
    nuclei_labels,
    minimal_keep_labels,
    dist_from_nuc=None,
    params=None,
):
    """Fuse per-channel keeps into a single labelled cytoplasm mask.

    fusion_mode:
      - "union"  : keep where ANY channel supports (max sensitivity).
      - "vote"   : keep where >= vote_k channels support (suppresses single-
                   channel bleed-through / artefacts).
      - "graded" : union within ``inner_union_radius`` of the nucleus, vote_k
                   beyond it -> sensitive core, conservative rim.
    """
    p = _params(params)
    base = np.asarray(base_territory, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    minimal = np.asarray(minimal_keep_labels, dtype=np.uint32)
    base_mask = base > 0

    keeps = list((keeps_by_channel or {}).values())
    if not keeps:
        kept = (nuclei > 0) | (minimal > 0)
    else:
        votes = np.zeros(base.shape, dtype=np.uint8)
        for k in keeps:
            votes += np.asarray(k, dtype=bool).astype(np.uint8)
        mode = str(p.get("fusion_mode", "union") or "union").strip().lower()
        vote_k = max(1, int(p.get("vote_k", 2) or 2))
        if mode == "vote":
            kept = votes >= vote_k
        elif mode == "graded":
            if dist_from_nuc is None:
                dist_from_nuc = _ndi.distance_transform_edt(nuclei == 0)
            r_inner = float(p.get("inner_union_radius", 6.0) or 6.0)
            near = np.asarray(dist_from_nuc, dtype=np.float32) <= r_inner
            kept = ((votes >= 1) & near) | ((votes >= vote_k) & ~near)
        else:  # union
            kept = votes >= 1

    kept = kept & base_mask
    kept |= (nuclei > 0) | (minimal > 0)      # anti-collapse floor

    labels = np.where(kept & base_mask, base, 0).astype(np.uint32, copy=False)
    labels = _keep_connected_to_seed(labels, nuclei, minimal, base)
    min_area = int(p.get("weak_min_component_area", 8) or 0)
    if min_area > 1:
        labels = _drop_small_per_cell(labels, min_area)
    labels[nuclei > 0] = nuclei[nuclei > 0]
    return labels.astype(np.uint32, copy=False)


def build_outside_in_segmentation(
    base_territory,
    nuclei_labels,
    minimal_keep_labels,
    marker_channels,
    channel_names,
    params=None,
    per_channel_gi=None,
    dist_from_nuc=None,
):
    """Convenience wrapper: (reuse or compute Gi*) -> per-channel carve -> fuse."""
    p = _params(params)
    if per_channel_gi is None:
        _gi_map, per_channel_gi, _backend = build_multichannel_gi_star(
            marker_channels, channel_names, base_territory, p
        )
    keeps = build_outside_in_keep_per_channel(
        base_territory, nuclei_labels, minimal_keep_labels, per_channel_gi, p
    )
    labels = fuse_outside_in_keeps(
        keeps, base_territory, nuclei_labels, minimal_keep_labels, dist_from_nuc, p
    )
    return labels, keeps
