# 10 — Step3 ↔ Step2 Source Alignment (Phase 2.1c)

This document records how the Step3 ChannelWorkbench calibration source is
aligned with the source future Step2/HQ2/CDS2 will read **before** applying the
manual remap. It freezes the alignment contract from
`09_CDS2_REMAP_ARCHITECTURE.md §7`. No Step2 runtime code is changed by this
phase.

---

## The invariant

```text
Min/Max/Brightness/Contrast/Gamma are defined in PRE-REMAP source intensity space.
Remap output is a 0–1 conditioning image.
Step3 and Step2 must use the SAME pre-remap source for saved configs to be Step2-ready.
```

The alignment target is the **pre-remap source**, not the remap output. Remap is
deterministic given `(source, config)`; if Step3 and Step2 read the same native
pre-remap source and apply the same saved config, the 0–1 conditioning map is
reproducible by construction.

---

## What source does Step3 use for the ChannelWorkbench?

Step3 hands the workbench the **current ROI/patch** marker arrays only (never a
full WSI), reusing the existing patch loaders/cache. Per marker, in priority
order (`ui/step3_page.py: _marker_native_patch`):

1. **corrected-zarr native float** — when the channel exists in
   `corrected_channels.zarr`. Step3 reads this without normalization
   (`np.asarray(group[ch][...], float32)`), so it is already native. → aligned.
2. **raw-OME native float** — for raw markers, Step3 now re-reads the patch with
   `read_region(..., normalize=False)` specifically for conditioning, so the
   array is in native counts. → aligned.
3. **raw-OME normalized 0–1** — only if the native re-read fails; the normalized
   QC display array is used. → **preview-only fallback** (not aligned).
4. **unknown** — any other source. → **preview-only fallback**.

DAPI / mask / fusion are **reference layers only** and are excluded from marker
source determination (see below).

## What source should future Step2 use before remap?

Determined by static inspection of the segmentation workers
(`workers/segment_merge_worker.py`, HQ/CDS marker reads): Step2 reads marker
channels with **`normalize=False`** (native float), then applies its own
`_normalize01` / remap. The pre-remap source is:

- **corrected-zarr native float** if `corrected_channels.zarr` is present for the
  channel, else
- **raw-OME native float** (`normalize=False`).

`Step3._expected_step2_pre_remap_source()` reports the same: corrected-native if
a corrected store is configured, else raw-OME native, else unknown.

## Are these aligned?

| marker source in Step3 | Step2 pre-remap source | aligned? |
|---|---|---|
| corrected-zarr native float | corrected-zarr native float | **yes** |
| raw-OME native float (`normalize=False`) | raw-OME native float | **yes** |
| raw-OME normalized 0–1 (fallback) | raw-OME native float | no — preview-only |
| unknown | unknown | no — preview-only |

The previous gap (Step3 read raw OME with `normalize=True` → normalized 0–1,
while Step2 reads native) is closed in this phase by the native re-read path.

## What cases are still preview-only?

- A native re-read of a raw marker failed (loader/bbox issue) → normalized
  fallback.
- The marker source is unknown.
- Mixed marker sources where at least one marker is not Step2-compatible →
  `calibration_source_matches_step2 = false`.
- **All Step3 configs in this phase** — see next section.

## What must be true before `preview_only` can become `false`?

All of:

1. Every marker calibrated on the native/corrected source Step2 will read
   (`calibration_source_matches_step2 == true`).
2. **Step2 runtime integration is implemented** — Step2 actually consumes the
   saved `channel_remap_config` and applies the remap on the native source. This
   does not exist yet.
3. The ROI/patch geometry and channel set used for calibration are confirmed to
   match the Step2 run.

Until (2) exists, `step2_ready` stays **false** and `preview_only` stays
**true**, even when `calibration_source_matches_step2 == true`. The config
schema enforces the invariant `step2_ready=true ⇒ preview_only=false`
(`utils/channel_remap_config.py: validate_channel_remap_config`).

---

## source_policy alignment fields

Recorded in `channel_remap_config["source_policy"]`:

```json
{
  "intensity_space": "corrected_zarr_native_float",
  "normalization": "none",
  "scope": "roi_preview",
  "preview_only": true,
  "step2_pre_remap_source": "corrected_zarr_native_float",
  "calibration_source_matches_step2": true,
  "step2_ready": false,
  "alignment_note": "Markers calibrated on the native/corrected source expected by Step2. Config stays preview_only/step2_ready=false until Step2 runtime integration is implemented."
}
```

Conservative defaults: `step2_pre_remap_source="unknown"`,
`calibration_source_matches_step2=false`, `step2_ready=false`,
`preview_only=true`.

Per-marker metadata (in `channels[name]`) additionally records `step2_compatible`
plus `value_min_observed`/`value_max_observed`, so each channel is
self-describing.

## Per-channel source alignment (Phase 2.1d)

A top-level config may be source-aligned even when channels come from a **valid
per-channel native mix** — e.g. CD45 from `corrected_zarr_native_float` and CK19
from `raw_ome_native_float` (CK19 not present in the corrected store). Both match
the source Step2 would read for that channel, so the run is aligned; but the mix
must be recorded, never hidden behind a single top-level flag.

Each marker channel therefore records its own alignment in `channels[name]`:

```json
{
  "source": "raw_ome_native",
  "intensity_space": "raw_ome_native_float",
  "normalization": "none",
  "value_min_observed": 0.0,
  "value_max_observed": 65535.0,
  "step2_pre_remap_source": "raw_ome_native_float",
  "calibration_source_matches_step2": true,
  "step2_compatible": true,
  "fallback_reason": "channel_not_found_in_corrected_zarr"
}
```

`fallback_reason` is present only when a fallback occurred:
`channel_not_found_in_corrected_zarr` (used raw native because the corrected
store lacks the channel — still aligned), `native_source_unavailable` (native
re-read failed → normalized preview, NOT aligned), or `unknown_source`.

The top-level `source_policy.source_alignment_mode` summarizes the per-channel
picture without flattening it:

| mode | meaning | `calibration_source_matches_step2` |
|---|---|---|
| `single_native_source` | all markers, one native source | true |
| `per_channel_native` | valid per-channel native mix, each matched | true |
| `partial_or_preview_fallback` | ≥1 marker normalized/unknown | false |
| `none` | no markers | false |

`calibration_source_matches_step2` is true **only when every marker** matches its
own expected Step2 pre-remap source. A single preview-only/mismatched channel
flips it to false. `step2_ready` stays **false** and `preview_only` stays
**true** in all of these — until Step2 runtime actually consumes the config.

## Reference layers do not affect alignment

DAPI / mask / fusion are reference/viewer layers. They are excluded from the
marker list (`_marker_channels()` drops canonical DAPI/nuclei) and never enter
`channel_remap_config["channels"]`. Only marker remap channels determine
`source_policy.intensity_space`, `calibration_source_matches_step2`, and
`step2_ready`. A normalized DAPI reference can never make the marker source look
"mixed" or downgrade alignment.

## Non-goals

This phase does not: make Step2 consume the remap config; change Step2/HQ2/CDS2
runtime, `_block_gi`, camp thresholds, or h5ad; remove top-hat/cuCIM; or add
napari. It only aligns and reports the Step3 calibration source against the
expected Step2 pre-remap source, and records the alignment in provenance.
