# TMA Study Mode — design of record (v1)

Status: **DESIGN, for review** (not implemented). 2026-08-18. Converged across
Ming + ChatGPT + Claude review. Reference platform: Enable Medicine (data index →
annotation → analysis → discovery, each layer traceable + versioned; spatial outputs
= cell frequency, cell interaction, cellular neighborhoods).

## 1. Framing

The block01 pipeline targets large-block WSI OME-TIFF. TMA support is **additive, not a
fork**: reuse the existing correction / remap / fusion / segmentation / QC engines; add
a traceable **study-cohort data model** + orchestrator + queue-QC layer on top. TMA is
NOT "slice one big image into many ROIs" — it is a versioned cohort of independent
samples. Single acquisition (this project's target) → **no cycle registration in v1**,
which removes a major risk class.

## 2. Data model (traceable, versioned)

```
TMA Study / Slide
└─ Core (grid row/col, core_id e.g. B03, patient/sample metadata, QC status)
   └─ Cell (segmentation, phenotype, spatial coords, per-core neighborhood)
```

- **Study** = { slide ome.tiff, marker panel, map sheet, FROZEN correction/remap/seg
  configs (versioned), dearray result (versioned) }.
- **Core** = { core_id, (row,col), core_bbox, core_mask, tissue_mask, status
  (present / empty / folded / fused / edge-truncated / low-tissue / seg-failed),
  sample metadata from the map sheet }.
- Extend `RoiContextModel`: a core IS an ROI + grid coords + sample annotation, so the
  per-ROI pipeline runs per core. Every layer's result is traceable + version-stamped.

## 3. The four load-bearing corrections (baked in)

1. **"Study-locked params" ≠ a single global Min/Max.**
   - Background/shading correction: per WHOLE SLIDE (single acquisition).
   - The **quantification** remap (the intensity space used for segmentation +
     feature extraction + cohort comparison) is estimated ONCE from a **stratified
     sample across all cores**, then FROZEN + versioned and applied uniformly.
   - **Display** contrast may be adjusted per core for viewing, but it MUST NOT touch
     the frozen intensity space — otherwise cores are not comparable. Decouple the
     Step0 preview/display remap from the frozen quantification remap.
   - Ties to v14.5d: the study remap is one source-aware config, frozen + versioned;
     the source-aware Step2 runtime applies it uniformly per core. [[step2-remap-promotion-architecture]]

2. **Dearray is map-sheet DRIVEN, not detect-then-guess-grid.**
   Read the map's expected row/col FIRST; detect candidate core centers; use a robust
   affine / lattice fit to assign candidates to expected positions. Missing / empty /
   folded / fused cores are kept as EXPLICIT states. Fast manual correction (add /
   delete / split / merge / renumber) → lock → version.

3. **Do NOT treat cross-core cells as one spatial tissue.**
   Build the adjacency graph + neighborhood / interaction / proximity metrics
   INDEPENDENTLY per core. Across cores: aggregate METRICS only — never cross-core
   neighbors. Statistics use **patient as the primary analysis unit, core as a nested
   replicate**; do not treat millions of cells as independent samples (pseudoreplication).

4. **Large runs = QUEUE SCHEDULING, not one-shot parallel.**
   Not 300 concurrent tasks. A resumable state machine with GPU/CPU/IO caps,
   retry-on-failure, per-core provenance, batch progress. Emit per-core h5ad/zarr
   FIRST, then a study-level manifest / lazy AnnData collection — avoid one physical
   concat (memory + file bottleneck).

## 4. Dearray algorithm (image version)

Input: multichannel OME-TIFF (dearray runs BEFORE cell segmentation).

1. Low-res DAPI / total-signal / tissue foreground mask.
2. Initial core candidates: connected components OR density clustering.
3. **Watershed ONLY for touching / fused candidates** — markers from distance-transform
   local peaks OR map-sheet-inferred expected centers (the latter is more robust at
   80–300 cores). Watershed is geometric separation only — it does NOT solve empty /
   folded / detached / edge-truncated / missing cores.
4. Shape QC filter: area, circularity, equivalent radius, edge truncation, low tissue area.
5. **Grid fit + numbering from the map sheet's row/col** (not auto row detection).
6. Manual QC: delete / add / split / merge / renumber → lock → version.

Keep TWO masks per core:
- `core_bbox` / `core_mask` — from dearray, for cropping + batch;
- `tissue_mask` — real tissue extent within the core, for area QC + downstream stats.

Identity assignment: watershed = geometry; grid-fit + map-sheet = identity; manual QC =
final confirmation.

## 5. Reference: TMASplit (borrow ideas, not code)

`github.com/UTS-Bioinformatics/TMASplit` (input is Xenium/Visium cell/spot coordinates,
a Seurat object — NOT images, so it cannot be run directly; **no clear open-source
license → do not copy code, only adapt the algorithm pattern**). Worth adopting:
- **normal + hybrid mode**: candidates first, watershed only for touching cores
  (avoid over-splitting);
- **shape QC** metrics;
- **grid numbering** from expected columns / map row-col;
- **visual review** with delete / add / split / merge / renumber.

## 6. Implementation order

1. **Foundation**: TMA manifest + map-sheet validation + dearray + manual correction +
   grid QC. Reuse the existing per-core pipeline (manual run per core to start).
   → You can process a TMA at all.
2. **Batch**: FROZEN study params (per-slide correction, stratified frozen remap) +
   queue scheduler (resumable, capped, retrying) + per-core output + failure recovery +
   per-core provenance.
3. **Cohort**: study AnnData collection + phenotyping (gating or clustering) + per-core
   neighborhood / interaction / frequency metrics + patient/condition statistical
   aggregation (patient = unit, core = nested replicate).

## 7. Reuse vs new

- **Reuse**: correction, remap (incl. v14.5d source-aware), fusion, HQ2/CSD/Mesmer
  segmentation, Step3 viewer + QuPath/MACSiQView navigator (backlog #3 synergy for a
  core-grid navigator).
- **New**: the study orchestrator, data contracts (Study/Core/Cell), dearray, the
  queue-QC layer, cohort AnnData + spatial-stats layer. Do NOT fork TMA-specific
  segmentation algorithms.

## 8. Top risks (large + auto)

- Dearray accuracy at 300 cores → mandatory fast manual-correction UI + grid-snap +
  a QC gate BEFORE batch.
- Metadata integrity → strict map-sheet validation (count, grid dims, unmapped cores),
  hard-fail on mismatch.
- Param consistency → quantification intensity space truly frozen at study level (no
  per-core drift), or cohort stats are invalid.
- Pseudoreplication → patient-level stats, core as nested replicate.
