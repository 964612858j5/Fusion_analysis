# Block01 — CODEX Multi-Channel Imaging Analysis Pipeline

A desktop (PyQt5) pipeline that turns a **giant multi-channel CODEX tissue image**
(tens of GB, dozens of stain channels) into a **per-cell data table** —
one row per cell, one column per channel intensity — ready for single-cell analysis.

> 把一张几十 GB 的多通道组织大图，变成一张「每行一个细胞、每列一个通道亮度」的数据表。

<!-- image placeholder: docs/images/00_goal_overview.png — big multi-channel image → cell × channel table -->

---

## What it does

The work is split into **5 steps**, driven from a single window:

```
Step 0          Step 1          Step 2            Step 3        Step 4
 Setup     →     Fusion+Tune  →   Segment+Merge  →   QC        →   Features
```

| Step | Does | Output |
|------|------|--------|
| **0 · Setup** | Pick region (ROI), group channels, remove background | corrected image + configs |
| **1 · Fusion + Tuning** | Merge channels into a cell-outline image; tune segmentation params on small patches | `cellpose_params.json`, `fused.zarr` |
| **2 · Segmentation + Merge** | Segment every cell across the whole ROI (tiled, then stitched) | whole-image cell mask |
| **3 · QC Viewer** | Visually verify the mask over DAPI | manual confirmation |
| **4 · Feature Extraction** | Measure each cell's per-channel intensity + morphology | `cell_features.csv` / `.h5ad` |

---

## 📖 User manual / 用户手册

Full beginner-friendly walkthroughs (written so a high-schooler can follow along):

- **English** → [`docs/user_guide.md`](docs/user_guide.md)
- **中文** → [`docs/用户指南.md`](docs/用户指南.md)

Start there if you've never used this tool before.

---

## Quick start

```bash
# 1. Create the environment (once)
conda env create -f ../environment.yml      # env name: fusion
#   or: bash ../setup_env.sh                 # env name: fusion_test2

# 2. Launch (run from the PARENT directory, as a module)
conda activate fusion
cd /sda1/Fusion/analysis_pipline
python -m block01.main
```

A dark-themed window titled `CODEX Pipeline | Fusion + Segmentation` opens on Step 0.
Then follow Step 0 → 4 in the UI (see the user manual for each step).

Optional: edit default paths in [`config.py`](config.py) (`OME_TIFF_FILE`, `OUTPUT_DIR`).

**Requirements:** NVIDIA GPU recommended (CUDA 12.x), Python 3.10. See `../environment.yml`.

---

## Segmentation methods

Default and recommended: **Cellpose whole-cell (Fusion + DAPI)**.
Also available: Cellpose nuclei (DAPI) / +expansion, StarDist, Mesmer.

> ⚠️ **HQ / HQ2 / CDS** methods are **still in testing and not yet mature** — avoid for final output.

See Appendix A in the user manual for how to choose.

---

## Repo layout

```
block01/
├── main.py            # entry point (python -m block01.main)
├── config.py          # default paths & constants
├── ui/                # Step 0–4 pages (PyQt5)
├── core/              # OME-TIFF loader, fusion, background correction
├── workers/           # segmentation / merge / feature-extraction workers
├── utils/             # tiling, segmentation config, helpers
├── tests/             # pytest suite
└── docs/              # 📖 user guides (EN + 中文) + images
```
