# Block01 — Spatial Proteomics Cell Segmentation & Quantification Pipeline

An end-to-end, GUI-driven pipeline for **highly multiplexed tissue imaging** (CODEX / PhenoCycler-style
multiplexed immunofluorescence). It converts a multi-gigabyte, dozens-of-marker whole-slide OME-TIFF
into a **single-cell expression matrix** — a cell-by-marker table suitable for downstream spatial
single-cell analysis (phenotyping, clustering, and neighborhood/niche characterization).

In short: **raw multiplexed image → segmented cells → per-cell marker quantification → AnnData (`.h5ad`) / CSV.**

<!-- image placeholder: docs/images/00_goal_overview.png — multiplexed WSI → single-cell expression matrix -->

---

## Background

Highly multiplexed imaging platforms (CODEX/PhenoCycler, IMC, MIBI, multi-cycle IF) capture **dozens of
protein markers** on a single tissue section, registered into one multi-channel whole-slide image (WSI).
Extracting biological meaning requires turning pixels into cells: each cell must be **segmented**, and the
**intensity of every marker** quantified within its boundary. The resulting single-cell spatial proteome
is the substrate for cell-type calling, spatial neighborhood analysis, and tissue architecture studies.

This pipeline addresses the practical bottlenecks of that workflow at WSI scale: autofluorescence/background
correction, nuclear- and membrane-guided segmentation, memory-bounded **tiled inference with cross-tile
label reconciliation**, and reproducible single-cell marker quantification.

---

## Pipeline overview

A single application drives a five-stage workflow:

```
Step 0          Step 1              Step 2                Step 3        Step 4
 Setup     →     Segment + Tune  →   Segmentation+Merge →  QC        →   Quantification
```

| Stage | Function | Key output |
|-------|----------|-----------|
| **0 · Setup** | ROI definition on the WSI overview, marker-to-lineage grouping, nuclear-channel assignment, and background/autofluorescence correction (top-hat or GPU Gaussian subtraction) | `corrected_channels.zarr`, ROI/correction configs |
| **1 · Segmentation + Fusion Tuning** | Select and tune the **cell-segmentation backend** — Cellpose, Mesmer, or watershed-variant cytoplasm carving — on representative patches. Inputs may be the nuclear channel alone, individual markers, or a weighted membrane/cytoplasm **fusion composite** paired with the nuclear channel | `cellpose_params.json`, `fused.zarr` |
| **2 · Segmentation + Merge** | Whole-ROI instance segmentation via memory-bounded tiled inference, followed by cross-tile label stitching into one global cell-label mask | global cell mask (`uint32`) |
| **3 · QC** | Visual QC of the segmentation overlaid on the nuclear (DAPI) channel | manual sign-off |
| **4 · Quantification** | Per-cell marker intensity statistics + morphology over the global mask | `cell_features.csv`, `cell_features.h5ad` |

---

## Segmentation backends

The pipeline wraps multiple instance-segmentation backends behind a unified configuration:

- **Cellpose whole-cell (fusion + nuclear)** — *default / recommended.* Nuclear-anchored whole-cell
  segmentation driven by the membrane/cytoplasm fusion composite.
- **Cellpose nuclei (DAPI)** and **nuclei + expansion** — nuclear segmentation, optionally dilated to
  approximate cell boundaries when membrane signal is weak.
- **StarDist** — star-convex nuclear segmentation, well suited to dense, round nuclei.
- **Mesmer (DeepCell)** — whole-cell / nuclear / nuclear-guided segmentation tuned for tissue imaging
  with membrane markers.

> ⚠️ The **HQ / HQ2 / CDS** cytoplasm-carving backends are **experimental and not yet validated** —
> not recommended for production output. Use Cellpose whole-cell for routine analysis.

See **Appendix A** of the user manual for backend selection guidance.

---

## Documentation

Step-by-step user manuals (written to onboard non-specialists):

- **English** → [`docs/user_guide.md`](docs/user_guide.md)
- **中文** → [`docs/用户指南.md`](docs/用户指南.md)

---

## Quick start

```bash
# 1. Create the environment (once)
conda env create -f ../environment.yml      # env: fusion
#   or: bash ../setup_env.sh                 # env: fusion_test2

# 2. Launch (run from the PARENT directory, as a module)
conda activate fusion
cd /sda1/Fusion/analysis_pipline
python -m block01.main
```

The application opens on Step 0 (`CODEX Pipeline | Fusion + Segmentation`); proceed Step 0 → 4 in the UI.
Default input/output paths can be preset in [`config.py`](config.py) (`OME_TIFF_FILE`, `OUTPUT_DIR`).

**Requirements:** NVIDIA GPU recommended (CUDA 12.x), Python 3.10. Full dependency list in `../environment.yml`.

---

## Inputs & outputs

- **Input:** multi-channel OME-TIFF whole-slide image (lazily read per-ROI/per-tile via the `zarr` interface
  to bound I/O and memory).
- **Output:**
  - `global_mask` — whole-ROI instance label image (`uint32`, one ID per cell, 0 = background).
  - `cell_features.csv` — single-cell table: rows = cells, columns = per-marker statistics (mean / median /
    sum / std / min / max / 90th percentile) plus morphology.
  - `cell_features.h5ad` — **AnnData** matrix for downstream single-cell / spatial analysis (e.g. scanpy, squidpy).

---

## Repository layout

```
block01/
├── main.py            # entry point (python -m block01.main)
├── config.py          # default paths & constants
├── ui/                # Step 0–4 pages (PyQt5)
├── core/              # OME-TIFF lazy loader, channel fusion, background correction
├── workers/           # segmentation / tile-merge / quantification workers
├── utils/             # tiling, segmentation config, helpers
├── tests/             # pytest suite
└── docs/              # user manuals (EN + 中文) + figures
```
