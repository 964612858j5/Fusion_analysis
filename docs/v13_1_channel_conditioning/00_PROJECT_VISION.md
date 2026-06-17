# 00 — Project Vision (v13.1 Channel Conditioning)

## Status

v13.1 is an **experimental branch**. It is not a committed replacement of the
v13 segmentation pipeline. It exists to test one specific hypothesis and to
prototype a MACSiQView / QuPath-like manual channel conditioning workflow.

Branch: `v13.1-channel-conditioning`
Backup of v13 baseline: `/sda1/Fusion/analysis_pipline/block01_v13_backup`

## What v13.1 tests

v13.1 tests whether a **manual channel remap** operator can replace the current
top-hat / cuCIM preprocessing path in the **segmentation preprocessing** stage
for HQ2 / CDS segmentation on the current HCC data.

This does **not** claim that manual remap mathematically reproduces top-hat in
all cases. Top-hat is a spatial morphological background estimator; manual remap
is an intensity windowing + tone operator. They are different operators.

## The real hypothesis

```text
After the existing upstream/background handling, residual background in the
target HCC data may be sufficiently separable by intensity that manual channel
remap can provide a cleaner, faster, and more controllable
segmentation-conditioning image for HQ2/CDS.
```

In words: most heavy background work may already be done upstream. What remains
may be separable by a per-channel intensity window (min/max) plus tone controls
(brightness/contrast/gamma). If so, a per-tile spatial morphological operator
(top-hat) is unnecessary overhead for this dataset, and a manually tuned,
reproducible intensity remap is cleaner, faster, and easier to control.

The benchmark plan (`06_BENCHMARK_PLAN.md`) defines how this hypothesis is
accepted or rejected, including region-stratified failure analysis.

## Motivation — MACSiQView-style workflow

The workflow model comes from MACSiQView-style manual channel adjustment, where
an operator interactively sets per-channel min/max/brightness/contrast/gamma to
produce a clean visual rendering of a high-plex panel.

The key conceptual move in v13.1:

```text
MACSiQView-like manual channel adjustment is NOT just a display transform.
In this design it becomes a segmentation preprocessing operator.
```

The same parameters that an operator tunes to make a channel "look clean" are
persisted as a config and applied headlessly to every tile during whole-run
segmentation. The image the human tunes is the image the segmenter consumes.

## Target operating regime

- **WSI-scale.** Whole-slide images, not single fields of view.
- **Tile-based.** Processing happens per tile; the remap operator runs per tile
  but its parameters (especially Auto) are fixed globally, not recomputed per
  tile. See `03_CHANNEL_REMAP_SPEC.md`.
- **Reproducible.** Segmentation must be driven by saved config files, not GUI
  state. A run is fully described by its image path + remap config + segmentation
  parameters. See `05_DATA_PROVENANCE_AND_H5AD.md`.

## First targets

- Segmentation methods: **HQ2 / CDS / lean_carve**.
- First UI prototype: redesigned **Step3 viewer**, rebuilt into a channel-first
  high-quality viewer (see `04_UI_REDESIGN_SPEC.md`).

## Scope boundaries (hard)

- Manual-remapped channels are for **segmentation only**.
- h5ad quantification still defaults to **raw or biologically corrected**
  intensity, never tone-mapped / remapped intensity. See
  `05_DATA_PROVENANCE_AND_H5AD.md`.
- **No napari** as a v13.1 core dependency. PyQtGraph-based visualization first.
- This phase is documentation and planning. No remap algorithm, UI redesign, or
  Step2 integration is implemented in this phase.
