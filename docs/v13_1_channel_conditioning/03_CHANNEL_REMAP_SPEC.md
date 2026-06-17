# 03 — Channel Remap Spec (v13.1)

Defines the channel remap operator: the segmentation-conditioning transform that
replaces top-hat / cuCIM in the v13.1 path.

## Per-channel parameters

```json
{
  "enabled": true,
  "min": 300.0,
  "max": 8000.0,
  "brightness": 0.0,
  "contrast": 1.0,
  "gamma": 1.0,
  "opacity": 1.0,
  "weight": 1.0,
  "auto": false
}
```

| Param | Meaning | Affects mask? | Notes |
|-------|---------|---------------|-------|
| `enabled` | channel participates | yes | disabled channel contributes nothing to fusion |
| `min` | window low (raw intensity units) | yes | values ≤ min map to 0 |
| `max` | window high (raw intensity units) | yes | values ≥ max map to 1 |
| `brightness` | additive shift after window | yes | range typically [-0.5, 0.5] |
| `contrast` | multiplicative gain around 0.5 | yes | 1.0 = identity |
| `gamma` | power curve | yes | 1.0 = identity; <1 lifts dark, >1 crushes dark |
| `opacity` | display blend only | **no** | viewer-only; never reaches segmentation |
| `weight` | fusion contribution weight | yes | multiplies channel in fusion stage |
| `auto` | window set by Auto, not manual | yes (via min/max) | when true, min/max come from frozen Auto reference |

Note: `opacity` is a **display** parameter and belongs conceptually to
`viewer_config` (see `02_PIPELINE_ARCHITECTURE.md`). It is listed here only so the
viewer and config share one channel record shape. The segmentation path must
ignore `opacity`.

## Canonical transform

```python
x = image.astype(float32)

# window
x = (x - min_value) / (max_value - min_value)
x = clip(x, 0, 1)

# contrast / brightness
x = (x - 0.5) * contrast + 0.5 + brightness
x = clip(x, 0, 1)

# gamma
x = x ** gamma
x = clip(x, 0, 1)
```

### Order is normative

The order is **window → contrast/brightness → gamma**, with a clip after each
stage. Do not reorder. Reordering changes results (e.g. gamma before windowing
operates on a different domain). The double clip is intentional: it bounds the
domain before gamma so `x ** gamma` never sees negatives or values > 1.

### Edge cases

- `max <= min`: invalid. Validator must reject or clamp to a minimum window
  width; the operator must not divide by zero.
- NaN / masked pixels: excluded from Auto percentiles (see below); in the
  transform they should be treated as out-of-tissue (commonly mapped to 0).

## Output

```text
float32, range 0–1
```

This is the segmentation-conditioning image. Downstream fusion / HQ2 / CDS /
lean_carve consume this float32 0–1 image (optionally scaled by `weight`).

## Auto (QuPath-style percentile auto contrast)

```text
min = percentile(valid_pixels, 0.1)
max = percentile(valid_pixels, 99.9)
```

`valid_pixels` excludes NaN / out-of-tissue / masked pixels. The saturation
percentiles (default 0.1 / 99.9) are themselves config and must be recorded with
the run (see `05_DATA_PROVENANCE_AND_H5AD.md`).

### Auto scope rule (critical)

```text
Auto must be computed once from selected calibration ROI / patch set /
whole-run reference region.
Auto must NOT be recomputed independently for each Step2 tile.
```

Workflow: operator (or a calibration pass) computes Auto percentiles on the
reference region → resulting `min`/`max` are written into the config as concrete
numbers → Step2 applies those frozen numbers to every tile identically. Per-tile
Auto would make the operator non-stationary across the slide, create tile seams,
and destroy reproducibility.

## Backend policy

- **Start with NumPy.** Phase 1 ships a NumPy implementation.
- **CuPy optional later.** A CuPy path may be added for acceleration.
- **Backend-agnostic API.** The core API should accept and return array-like data
  and dispatch on array type where feasible, so the same `channel_remap` function
  works for NumPy now and CuPy later without changing callers. No Qt and no I/O in
  `core/channel_remap.py`.

## Reference API sketch (non-binding)

```python
# core/channel_remap.py

def remap_channel(image, params) -> "float32 array in [0,1]":
    """Apply window -> contrast/brightness -> gamma. Backend-agnostic."""

def auto_window(valid_pixels, low_pct=0.1, high_pct=99.9) -> (min_value, max_value):
    """QuPath-style percentile window from a reference pixel set."""

def fuse_channels(remapped_channels, weights) -> "fused float32 image":
    """Weighted combine of remapped channels for HQ2/CDS/lean_carve."""
```
