# 05 — Data Provenance and h5ad (v13.1)

This is a strict data provenance document. The central risk in v13.1 is mixing a
**segmentation-conditioning** image with a **biological expression** image. They
must never be confused.

## Core principle

```text
Manual-remapped images are segmentation-conditioning images,
not biological expression images.
```

Min/Max/Brightness/Contrast/Gamma are tuned to make masks good. They are not a
biologically meaningful transform of marker intensity. Using them as expression
would silently corrupt every downstream quantitative analysis.

## Default h5ad behavior

```text
adata.X = raw or biologically corrected expression matrix
```

Default expression is **raw** intensity or **biologically corrected** intensity
(the existing biologically meaningful correction). It is **never** the
gamma/brightness/contrast/min-max remapped intensity.

Even when segmentation was driven by remapped images, per-cell expression is
read from the raw / biologically corrected source over the resulting mask.

## Optional storage (non-default)

If remapped per-cell intensity is wanted for QC or comparison, store it in a
clearly named, non-default location:

```text
adata.layers["segmentation_remap_mean"]   # optional remapped per-cell intensity
adata.uns["segmentation_preprocess"]      # full remap config + segmentation provenance
```

- `adata.layers["segmentation_remap_mean"]` is **never** assigned to `adata.X`.
  It exists only as an inspectable secondary layer.
- `adata.uns["segmentation_preprocess"]` holds the full provenance block below.

## Required provenance metadata

Stored under `adata.uns["segmentation_preprocess"]` (and/or run-level sidecar):

- **segmentation run ID**
- **source image path**
- **channel remap config path**
- **per-channel min / max / brightness / contrast / gamma**
- **Auto algorithm and saturation** (e.g. QuPath percentile, 0.1 / 99.9)
- **channels used for HQ2 / CDS** (and lean_carve)
- **whether remap was used for segmentation** (boolean)
- **feature extraction source** (raw vs biologically corrected)

This block must be sufficient to reproduce the exact mask and to explain exactly
what produced both the mask and the expression matrix.

## Explicit warning

```text
Changing Min/Max/Gamma can change segmentation masks.
These parameters must be saved, versioned, and reported with each
segmentation run.
```

Corollary: two runs of "the same" sample with different remap configs are
**different segmentations** and must be traceable as such. Never overwrite a
remap config in place without versioning; never report a mask without its config.

## Quick checklist for the h5ad writer

- [ ] `adata.X` is raw or biologically corrected — confirm it is **not** remapped.
- [ ] If remapped per-cell values are stored, they are in
      `layers["segmentation_remap_mean"]`, not `X`.
- [ ] `uns["segmentation_preprocess"]` contains all required provenance fields.
- [ ] `feature extraction source` field matches what actually fed quantification.
- [ ] `whether remap was used for segmentation` is set correctly.
