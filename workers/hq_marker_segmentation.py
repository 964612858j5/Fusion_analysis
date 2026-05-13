"""
Nucleus-seeded high-quality marker segmentation helpers.

The functions in this module are intentionally UI-free so Step2 can use them
from the merge worker and tests can exercise the consensus logic directly.
"""

import csv
import json
import os
from collections import defaultdict

import numpy as np


CONSENSUS_MODES = ("adaptive_best_channel", "weighted_vote", "majority_vote")


def parse_hq_channels(text):
    """Parse semicolon-separated HQ channel names."""
    if isinstance(text, (list, tuple)):
        return [str(part).strip() for part in text if str(part).strip()]
    return [part.strip() for part in str(text or "").split(";") if part.strip()]


def validate_hq_channels(input_channels, available_channels):
    """Validate HQ channels and return a normalized channel list.

    Raises
    ------
    ValueError
        If no channels are selected or any requested channel is unavailable.
    """
    channels = list(input_channels or [])
    if not channels:
        raise ValueError("Cellpose nuclei + HQ requires at least one hq_channels entry.")
    available = set(available_channels or [])
    missing = [ch for ch in channels if ch not in available]
    if missing:
        raise ValueError(
            "Missing HQ channel(s): "
            + "; ".join(missing)
            + "\nAvailable channels: "
            + "; ".join(sorted(available))
        )
    return channels


def parse_channel_weights(text, channels):
    """Parse optional channel weights.

    Accepts either "PanCK=1;CD45=0.8" or "1;0.8;1" in channel order.
    Missing weights default to 1.0.
    """
    channels = list(channels or [])
    weights = {ch: 1.0 for ch in channels}
    text = str(text or "").strip()
    if not text:
        return weights
    parts = [p.strip() for p in text.split(";") if p.strip()]
    keyed = any("=" in p for p in parts)
    if keyed:
        for part in parts:
            if "=" not in part:
                continue
            name, value = [x.strip() for x in part.split("=", 1)]
            if name in weights:
                weights[name] = float(value)
        return weights
    for ch, value in zip(channels, parts):
        weights[ch] = float(value)
    return weights


def percentile_normalize(channel, low=1.0, high=99.5):
    arr = np.asarray(channel, dtype=np.float32)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype=np.float32)
    lo, hi = np.percentile(finite, [float(low), float(high)])
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.float32)
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0).astype(np.float32, copy=False)


def _expand_labels(labels, distance):
    try:
        from skimage.segmentation import expand_labels
        return expand_labels(labels, distance=distance).astype(np.uint32, copy=False)
    except Exception:
        from scipy import ndimage as ndi
        labels = np.asarray(labels, dtype=np.uint32)
        if distance <= 0 or labels.max() == 0:
            return labels.copy()
        _, indices = ndi.distance_transform_edt(labels == 0, return_indices=True)
        nearest = labels[tuple(indices)]
        dist = ndi.distance_transform_edt(labels == 0)
        out = nearest.astype(np.uint32, copy=True)
        out[dist > float(distance)] = 0
        return out


def _seeded_watershed(norm, nuclei_labels, max_cell_radius, min_signal_threshold):
    nuclei_labels = np.asarray(nuclei_labels, dtype=np.uint32)
    bounded = _expand_labels(nuclei_labels, max_cell_radius)
    mask = (bounded > 0) & (np.asarray(norm) >= float(min_signal_threshold))
    mask[nuclei_labels > 0] = True
    try:
        from skimage.segmentation import watershed
        labels = watershed(-np.asarray(norm, dtype=np.float32), markers=nuclei_labels, mask=mask)
        labels[bounded == 0] = 0
        return labels.astype(np.uint32, copy=False)
    except Exception:
        out = bounded.copy()
        out[~mask] = 0
        return out.astype(np.uint32, copy=False)


def _label_stats(labels, values, n_labels, min_signal_threshold):
    flat_l = np.asarray(labels, dtype=np.uint32).ravel()
    flat_v = np.asarray(values, dtype=np.float32).ravel()
    counts = np.bincount(flat_l, minlength=n_labels + 1).astype(np.float32)
    sums = np.bincount(flat_l, weights=flat_v, minlength=n_labels + 1).astype(np.float32)
    means = sums / np.maximum(counts, 1.0)
    pos = np.bincount(
        flat_l,
        weights=(flat_v >= float(min_signal_threshold)).astype(np.float32),
        minlength=n_labels + 1,
    ).astype(np.float32)
    pos_frac = pos / np.maximum(counts, 1.0)
    top = np.zeros(n_labels + 1, dtype=np.float32)
    for lab in range(1, n_labels + 1):
        vals = flat_v[flat_l == lab]
        if vals.size:
            top[lab] = np.percentile(vals, 90)
    bg = flat_v[flat_l == 0]
    bg_mean = float(bg.mean()) if bg.size else 0.0
    bg_std = float(bg.std()) if bg.size else 1.0
    snr = np.maximum(0.0, (means - bg_mean) / max(bg_std, 1e-6))
    score = 0.45 * means + 0.35 * top + 0.15 * pos_frac + 0.05 * np.clip(snr, 0, 5) / 5
    score[counts <= 0] = 0.0
    return score.astype(np.float32), means.astype(np.float32), pos_frac.astype(np.float32)


def _nearest_nucleus_labels(nuclei_labels):
    try:
        from scipy import ndimage as ndi
        _, indices = ndi.distance_transform_edt(nuclei_labels == 0, return_indices=True)
        return np.asarray(nuclei_labels, dtype=np.uint32)[tuple(indices)]
    except Exception:
        return np.asarray(nuclei_labels, dtype=np.uint32)


def _resolve_claims(label_claims, label_scores, nearest_labels, shape):
    final = np.zeros(shape, dtype=np.uint32)
    score_map = np.full(shape, -np.inf, dtype=np.float32)
    for lab, claim in label_claims.items():
        if not np.any(claim):
            continue
        score = float(label_scores.get(lab, 0.0))
        better = claim & (score > score_map)
        tie = claim & np.isclose(score, score_map) & (nearest_labels == lab)
        update = better | tie
        final[update] = lab
        score_map[update] = score
    return final


def segment_nuclei_hq(
    nuclei_labels,
    marker_channels,
    channel_names,
    max_cell_radius=12,
    normalization_low=1.0,
    normalization_high=99.5,
    consensus_mode="adaptive_best_channel",
    channel_weights=None,
    min_signal_threshold=0.08,
):
    """Create final whole-cell labels from fixed nuclei and marker channels."""
    if consensus_mode not in CONSENSUS_MODES:
        raise ValueError(f"Unknown consensus mode: {consensus_mode}")
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    if n_labels == 0:
        empty_qc = []
        return np.zeros(nuclei.shape, dtype=np.uint32), nuclei.copy(), empty_qc

    channel_names = list(channel_names or [])
    weights = {ch: 1.0 for ch in channel_names}
    weights.update(dict(channel_weights or {}))
    norm_by_name = {}
    candidate_by_name = {}
    support_by_name = {}
    pos_by_name = {}
    for ch, arr in zip(channel_names, marker_channels):
        norm = percentile_normalize(arr, normalization_low, normalization_high)
        cand = _seeded_watershed(norm, nuclei, max_cell_radius, min_signal_threshold)
        score, _mean, pos_frac = _label_stats(cand, norm, n_labels, min_signal_threshold)
        norm_by_name[ch] = norm
        candidate_by_name[ch] = cand
        support_by_name[ch] = score * float(weights.get(ch, 1.0))
        pos_by_name[ch] = pos_frac

    bounded = _expand_labels(nuclei, max_cell_radius)
    nearest = _nearest_nucleus_labels(nuclei)
    label_claims = {}
    label_scores = {}
    main_channel = {}

    for lab in range(1, n_labels + 1):
        scores = {ch: float(support_by_name[ch][lab]) for ch in channel_names}
        best_ch = max(channel_names, key=lambda ch: scores[ch])
        main_channel[lab] = best_ch
        if consensus_mode == "adaptive_best_channel":
            main = candidate_by_name[best_ch] == lab
            if scores[best_ch] < float(min_signal_threshold):
                claim = bounded == lab
                label_scores[lab] = float(min_signal_threshold) * 0.5
            else:
                claim = main.copy()
                for ch in channel_names:
                    if ch == best_ch:
                        continue
                    if scores[ch] >= scores[best_ch] * 0.55:
                        claim |= candidate_by_name[ch] == lab
                claim &= bounded == lab
                label_scores[lab] = scores[best_ch]
        elif consensus_mode == "weighted_vote":
            vote = np.zeros(nuclei.shape, dtype=np.float32)
            for ch in channel_names:
                vote += (candidate_by_name[ch] == lab).astype(np.float32) * float(weights.get(ch, 1.0))
            total = max(sum(float(weights.get(ch, 1.0)) for ch in channel_names), 1e-6)
            claim = (vote >= (0.5 * total)) & (bounded == lab)
            label_scores[lab] = max(scores.values()) if scores else 0.0
        else:
            votes = np.zeros(nuclei.shape, dtype=np.uint16)
            for ch in channel_names:
                votes += (candidate_by_name[ch] == lab).astype(np.uint16)
            claim = (votes >= ((len(channel_names) // 2) + 1)) & (bounded == lab)
            label_scores[lab] = max(scores.values()) if scores else 0.0
        claim[nuclei == lab] = True
        label_claims[lab] = claim

    claim_count = np.zeros(nuclei.shape, dtype=np.uint16)
    for claim in label_claims.values():
        claim_count += claim.astype(np.uint16)
    conflict_count_by_label = defaultdict(int)
    conflict_pixels = claim_count > 1
    for lab, claim in label_claims.items():
        conflict_count_by_label[lab] = int(np.count_nonzero(claim & conflict_pixels))

    final = _resolve_claims(label_claims, label_scores, nearest, nuclei.shape)

    nucleus_area = np.bincount(nuclei.ravel(), minlength=n_labels + 1)
    cell_area = np.bincount(final.ravel(), minlength=n_labels + 1)
    qc_rows = []
    for lab in range(1, n_labels + 1):
        per_support = {ch: float(support_by_name[ch][lab]) for ch in channel_names}
        area = int(cell_area[lab]) if lab < len(cell_area) else 0
        narea = int(nucleus_area[lab]) if lab < len(nucleus_area) else 0
        support_fraction = 0.0
        if area > 0:
            supported = np.zeros(nuclei.shape, dtype=bool)
            for ch in channel_names:
                supported |= candidate_by_name[ch] == lab
            support_fraction = float(np.count_nonzero((final == lab) & supported)) / float(area)
        qc_rows.append({
            "cell_id": lab,
            "nucleus_area": narea,
            "cell_area": area,
            "cell_to_nucleus_ratio": float(area) / max(float(narea), 1.0),
            "main_channel": main_channel.get(lab, ""),
            "per_channel_support_score": json.dumps(per_support, sort_keys=True),
            "consensus_support_fraction": support_fraction,
            "conflict_pixel_count": int(conflict_count_by_label.get(lab, 0)),
            "low_confidence_flag": bool(label_scores.get(lab, 0.0) < float(min_signal_threshold)),
        })
    return final.astype(np.uint32, copy=False), nuclei.copy(), qc_rows


def write_hq_qc_table(path, rows):
    fieldnames = [
        "cell_id",
        "nucleus_area",
        "cell_area",
        "cell_to_nucleus_ratio",
        "main_channel",
        "per_channel_support_score",
        "consensus_support_fraction",
        "conflict_pixel_count",
        "low_confidence_flag",
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows or []:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
