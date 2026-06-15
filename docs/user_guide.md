# Block01 User Guide — CODEX Multi-Channel Imaging Analysis Pipeline

> This document is written for **someone using this software for the very first time**. Even with no biology or programming background, you can get it running by following along step by step.
> Wherever you see `<!-- image placeholder -->`, a screenshot needs to be added there. Put the images in the `docs/images/` folder.

---

## 1. What does this software do? (Understand the goal first)

Imagine you have a **gigantic photo of a tissue slice** (a microscope image of a tumor or a patch of skin).
This is no ordinary photo — it was taken with about **a dozen to several dozen different "stains"**, and each stain lights up only one kind of thing:

- Some stains light up only the **cell nucleus** (e.g. DAPI);
- Some light up only **immune cells** (e.g. CD3, CD8, CD68);
- Some light up **epithelial cells** or **blood vessels** (e.g. CK19, CD31).

Each stain = one **channel**. A single image stacks dozens of channels, and the file can be tens of GB in size.

**Our goal, in one sentence:**

> Turn this huge multi-channel photo into a **spreadsheet** —
> where each row is **one cell**, and each column is **that cell's brightness in a given channel**.

With this table, scientists can answer questions like: "How many immune cells are in this tissue? How close are they to the tumor cells?"

It's like taking a photo of a crowd of ten thousand people and **first circling each person, then recording what color clothes each one wears** — except here the "people" are cells and the "clothing colors" are the brightness of each channel.

<!-- image placeholder: images/00_goal_overview.png — left: a big multi-channel tissue image; right: a "cell × channel" table; an arrow in between. Helps the reader grasp input → output at a glance -->

---

## 2. The overall workflow (5 steps, follow in order)

The software splits the whole job into **5 steps**. There is a step navigation bar at the top; just click through it from left to right:

```
Step 0          Step 1          Step 2          Step 3        Step 4
 Setup     →     Fusion+Tune  →   Segment+Merge →   QC       →    Features
```

| Step | Name | What it does (one line) | Output |
|------|------|------------------------|--------|
| **Step 0** | Setup | Pick the region to analyze, organize channels, remove background noise | Corrected image, ROI config |
| **Step 1** | Fusion + Tuning | Merge channels into one "cell-outline image"; tune the best segmentation parameters on small patches | Segmentation parameter file |
| **Step 2** | Segmentation + Merge | Use the chosen parameters to circle **every cell in the whole region** | Whole-image cell mask |
| **Step 3** | QC Viewer | Visually check whether the cells are circled correctly | Confirm / not confirm |
| **Step 4** | Feature Extraction | Measure each cell's value in each channel, export the table | `cell_features.csv` / `.h5ad` |

> **Core idea**: the earlier steps are all "preparation and trial". The truly heavy lifting is Step 2 (processing the entire big image), and the final Step 4 produces the table.

<!-- image placeholder: images/01_top_step_bar.png — a real screenshot of the "Step 0 → Step 1 → ... → Step 4" navigation bar at the top, currently highlighting Step 0 -->

---

## 3. Installation and launch

### 1. Prepare the runtime environment (only needed once)

This project relies on a GPU (NVIDIA card recommended) and a Python environment. The environment is defined in files one directory up:

- `environment.yml` (a conda environment named `fusion`)
- or `setup_env.sh` (a one-shot script that builds an environment named `fusion_test2`)

For the first install, run one of these in a terminal:

```bash
# Option A: use the ready-made environment.yml
conda env create -f /sda1/Fusion/analysis_pipline/environment.yml

# Option B: use the one-shot script
bash /sda1/Fusion/analysis_pipline/setup_env.sh
```

> If you need to run a login-type or interactive command yourself, you can type `! your-command` in this session's input box to run it directly.

### 2. Launch the software

After activating the environment, **go to the parent directory of the project** and start block01 as a Python module:

```bash
conda activate fusion          # or: micromamba activate fusion_test2
cd /sda1/Fusion/analysis_pipline
python -m block01.main
```

On success, a **dark-themed window** pops up titled `CODEX Pipeline | Fusion + Segmentation`, defaulting to Step 0.

<!-- image placeholder: images/02_main_window_on_launch.png — a screenshot of the whole window right after opening, sitting on Step 0 -->

### 3. Set up your config first (optional but recommended)

The file `block01/config.py` holds a few commonly used default values. On first use, fill in your own data paths:

```python
OME_TIFF_FILE = ".../xxx.ome.tif"   # your raw multi-channel big image
OUTPUT_DIR    = ".../pipeline_v2"   # output folder for results
```

> You don't have to edit it — you can also pick files with the "Browse" buttons in the UI.

---

## 4. Step-by-step instructions

Each step below follows the pattern **"What it does → How to operate → Things to watch for".**

---

### Step 0 · Setup

**What it does:**
Read in the raw big image, **circle the region you actually care about (ROI)**, organize channel groups, and **remove background noise**. Think of it as "wiping the lens and aiming at the people before taking the photo".

**How to operate:**

1. **Load the image**: enter the path to the raw `.ome.tif` and the output folder, click load, and the software shows a **downsampled overview**.
2. **Draw an ROI**: frame the region to analyze on the overview (you can draw several, named ROI_1, ROI_2…). Processing only the ROI **saves a lot of time and memory**.
3. **Set channel groups and the nucleus channel**:
   - In the **Channels** panel, categorize each channel (epithelial / immune / vascular…) and set weights;
   - In **Nucleus Channel**, pick the nucleus channel (usually `DAPI`).
4. **Background correction (denoising)**: in **Method Parameters**, choose a background-removal method:
   - **TopHat**: classic morphological background removal, adjustable radius;
   - **cucim**: GPU Gaussian background subtraction, faster (requires GPU support).
   - In the **Patch Preview — Original | TopHat | cucim** panel you can **compare side by side** the original and both denoising results, and pick whichever is cleaner.
5. **Choose preview patches**: tick a few representative small regions. Step 1 parameter tuning later uses these patches for quick trials, instead of running the whole image each time.
6. Click "Process", and the software generates the corrected image and config files.

**Output:** `corrected_channels.zarr` (corrected image), `roi_config.json`, `patch_config.json`, `correction_config.json`, `step0_roi_result.json`. These are read automatically by all later steps.

<!-- image placeholder: images/03_step0_overview_and_roi.png — the overview in Step 0 with an ROI box already drawn -->
<!-- image placeholder: images/04_step0_channel_groups.png — the Channels / Nucleus Channel grouping and weights panel -->
<!-- image placeholder: images/05_step0_bg_correction_compare.png — the Original | TopHat | cucim three-way comparison preview -->

> 💡 **Tip**: with background correction, "too gentle beats too aggressive". Over-subtracting wipes out real signal too, dims the cells, and worsens segmentation.

---

### Step 1 · Fusion + Segmentation Tuning

**What it does:**
Two things:

1. **Fusion**: take the channels you grouped in Step 0 and **merge them into one "cytoplasm/outline image"**, paired with a "nucleus image". In the preview, **red = cytoplasm, blue = nucleus**, updated in real time.
   - The reasoning is simple: a single channel usually lights up only part of the cells. Stacking them (taking the max) lights up almost every cell boundary, so the segmentation algorithm can see clearly.
2. **Tuning (Grid Search)**: trial repeatedly on the **small patches** chosen in Step 0 to find the **segmentation method and parameters** that circle cells most accurately.

**How to operate:**

1. After entering Step 1, click **Load Step0 ROI Result** (usually loads automatically).
2. Pick a preview patch on the left, watch the fusion preview in the middle, and **adjust channel weights** until the cell outlines are clear.
3. On the right, in the **Method & Parameters** tab, **choose a segmentation method** (see "Appendix A" for descriptions).
4. Use the two-phase search to quickly pin down parameters:
   - **Phase 1 — Auto-diameter**: auto-estimate the cell diameter (try a set of diameters, see which fits best);
   - **Phase 2 — Fine search**: grid-search over the two parameters `flow × cellprob`.
   - Results show up in the **Patch Results** tab as a table/thumbnails; click one to apply that parameter set.
5. When satisfied, click **💾 Save Config & Generate fused.zarr** at the bottom to save the parameters.

**Output:** segmentation parameter file (`cellpose_params.json` etc.), `fused.zarr`, `step1_session.json` (next time you can "Load Previous Step1 Session" and keep tuning).

<!-- image placeholder: images/06_step1_fusion_preview.png — the fusion preview in the middle (red = cytoplasm, blue = nucleus), patch list on the left -->
<!-- image placeholder: images/07_step1_method_and_params.png — the Method & Parameters tab on the right, with the method dropdown + Phase1/Phase2 -->
<!-- image placeholder: images/08_step1_tuning_result_grid.png — the Patch Results tab showing segmentation thumbnails for different parameters -->

> 💡 **Why tune on small patches first?** Running the whole big image once can take tens of minutes or even hours. Get the parameters right on a fingernail-sized patch first, then run the big image — it saves time and effort.

---

### Step 2 · Segmentation & Merge

**What it does:**
Take the parameters tuned in Step 1 and **actually circle every single cell in the entire ROI**. Because the whole image is too big to fit in GPU memory, the software **cuts it into a grid of small tiles, processes each one, then stitches (merges) them into one whole-image mask**.

**How to operate:**

1. Confirm the inputs in **Input Data** (fusion image / parameter file, usually carried over automatically). You can click **Load zarr info & overview** to take a look.
2. **Tile Grid**: set the tile size (how big each cell of the grid is). Smaller tiles use less GPU memory, but the seams need more care.
3. **Segmentation Parameters**: confirm the parameters; you can also **Apply Selected Params** to reuse a previously chosen set.
4. Choose the **Output** location, then click **▶ Run Segmentation & Merge** to start. The progress bar shows which tile is being processed.
5. To stop midway, click **⏹ Stop**; if it gets interrupted, **Recovery Mode** can resume merging from the saved intermediate files instead of starting over.

**Output:** the whole-image cell mask `global_mask` (a huge uint32 array / OME-TIFF). In the mask, each cell has a unique number (1, 2, 3…), and the background is 0.

<!-- image placeholder: images/09_step2_tiles_and_run.png — Step 2's Tile Grid settings + Run button + progress bar -->
<!-- image placeholder: images/10_step2_mask_result.png — a cell mask for a region (each cell a differently colored blob) -->

> ⚠️ **This is the most time-consuming and memory-hungry step.** Make sure the Step 1 parameters are truly satisfactory before running. When resources are tight, make the tiles smaller.

---

### Step 3 · QC Viewer

**What it does:**
**Visually check whether the Step 2 result is accurate.** The software **overlays the freshly generated cell mask onto the DAPI (nucleus) image** — good segmentation should look like "exactly one cell outline wrapped around each nucleus", with nothing over-circled, missed, or two cells fused into one.

**How to operate:**

1. **Frame a small region** on the DAPI thumbnail to inspect closely.
2. The software loads the raw image + mask overlay for that region; zoom in to check the boundaries.
3. Checkboxes let you toggle the mask overlay and adjust transparency, comparing against the raw signal to see if the circling is correct.
4. If circling is generally poor → **go back to Step 1 and re-tune**, then re-run Step 2. If satisfied → move on to Step 4.

**Output:** no new files; this step is a **manual confirmation** gate.

<!-- image placeholder: images/11_step3_qc_overlay.png — a zoomed-in view of cell mask outlines overlaid on DAPI, showing "outline wrapping the nucleus" -->

> 💡 **What counts as "well circled"?** Check three things: ① most nuclei are circled; ② each outline contains basically one nucleus; ③ outlines hug the real cells, with no large gaps or overflow.

---

### Step 4 · Feature Extraction

**What it does:**
Time to **produce the table.** The software goes through every cell in the whole-image mask, **measures its brightness in each channel plus its shape info** (area, position, etc.), and finally saves a table ready for analysis.

**How to operate:**

1. Confirm three paths: the **cell mask** `mask`, the **raw OME-TIFF**, and the **output folder** (usually carried over from earlier steps).
2. Tick the statistics you want (each is computed per channel):
   - **Mean** (default, most common), **Median**, **Sum (total intensity)**, **Std dev**, **Min / Max**, **90th percentile**.
   - The UI shows which statistic the main matrix X in `.h5ad` uses (the first one ticked, e.g. mean).
3. Click run and wait for the progress bar.

**Output:**
- `cell_features.csv`: a table **anyone can open in Excel**, one cell per row, one "channel × statistic" per column.
- `cell_features.h5ad`: a standard format for downstream Python single-cell analysis (e.g. scanpy) to use directly.

<!-- image placeholder: images/12_step4_feature_options.png — Step 4's statistics checkboxes + path settings -->
<!-- image placeholder: images/13_step4_output_table.png — what cell_features.csv looks like when opened: rows = cells, columns = per-channel means -->

> 🎉 **That's the entire pipeline done.** You went from a tens-of-GB image to a clean "cell × marker" data table.

---

## 5. Quickest path to get started (read this if you're in a hurry)

```
1. python -m block01.main              # launch
2. Step 0: pick image → draw ROI → set DAPI as nucleus → remove background → pick a few preview patches → Process
3. Step 1: adjust channel weights for clear outlines → choose method → try params with Phase1+Phase2 → save
4. Step 2: confirm params → set tile size → Run, let it finish
5. Step 3: spot-check a few regions, confirm circling is accurate
6. Step 4: tick Mean → Run → get cell_features.csv
```

> There are also **Skip → Step 2 / 3 / 4** buttons at the top: if some intermediate results are already done, you can jump straight ahead.

---

## 6. FAQ

| Problem | Likely cause / fix |
|---------|--------------------|
| Launch error `No module named block01` | Not run from the **parent directory** `analysis_pipline`, or not using `python -m block01.main` |
| Cells circled fuzzily / fused | Go back to Step 1 and tune channel weights and segmentation params; background may be under- or over-subtracted (Step 0) |
| Out of GPU memory midway through Step 2 | Make the tiles smaller; confirm you're using the GPU, not the CPU |
| Step 2 got interrupted — re-run? | Use Step 2's **Recovery Mode** to resume merging from saved intermediate files |
| Want to redo with a different ROI | Go back to Step 0 and redraw the ROI; each ROI's results are saved in separate folders and won't overwrite each other |
| Are params kept after closing the software? | Yes — Step 1 has **Save / Load Session**, which writes `step1_session.json` |

---

## Appendix A · How to choose a segmentation method? (if unsure, use the default)

The software offers several algorithms for "circling cells". **Beginners can just use the default Cellpose.** Below is a simple description of the differences:

| Method | Plain explanation | When to use |
|--------|-------------------|-------------|
| **Cellpose whole-cell (Fusion + DAPI)** | Uses the "fused outline image + nucleus" to circle **the whole cell** directly | Default first choice, most situations |
| **Cellpose nuclei (DAPI)** | Circles only the **nucleus** | Only care about nuclei, or membrane stains are poor |
| **Cellpose nuclei + expansion** | Circle the nucleus first, then "expand" outward a ring as the cell | An approximation when there's no good membrane signal |
| **Cellpose nuclei + HQ / HQ2** ⚠️ | Circle the nucleus first, then use structural channels for finer cytoplasm expansion | **Still in testing, not yet mature** — use with caution for real analysis |
| **Cellpose nuclei + CDS** ⚠️ | Circle the nucleus first, then do a "donut-style" signal-constrained expansion | **Still in testing, not yet mature** — use with caution for real analysis |
| **StarDist nuclei (+expansion)** | Another nucleus-circling algorithm (good for dense round nuclei) | When nuclei are dense and round |
| **Mesmer (whole-cell / nuclei / nuclear-guided)** | Whole-cell segmentation designed specifically for tissue imaging | Clear membrane channels, want whole-cell segmentation |

> ⚠️ **Note**: the methods marked ⚠️ — **HQ / HQ2 / CDS — are still in testing and not yet mature**. Results may be unstable; do not use them for final output. Prefer `Cellpose whole-cell`.
>
> Simple rule: **run `Cellpose whole-cell` once first and look at the result**, then switch if unsatisfied. Each method has its own detailed parameters; leaving them untouched usually works fine.

---

## Appendix B · Mini glossary

- **Channel**: one kind of thing lit up by one stain, corresponding to one "layer" in the big image.
- **ROI (Region of Interest)**: the region you frame for analysis, to avoid processing the whole giant image.
- **DAPI**: a common nucleus stain; the software uses it as the "nucleus channel" by default.
- **Fusion**: stacking multiple channels into one outline image so the algorithm can find cell boundaries.
- **Segmentation**: the process of circling each individual cell in the image.
- **Mask**: a "numbering image" the same size as the original, where each cell gets a number and the background is 0.
- **Tile**: the small grid cells the big image is cut into, processed in blocks to save memory.
- **Feature**: the measured values for each cell (per-channel brightness, area, etc.).
- **`.zarr`**: an array format that supports "reading only the small chunk you need", saving memory.
- **`.h5ad`**: a standard table format common in single-cell analysis (read directly by scanpy etc.).

---

*Document version: v1 (2026-06-15). Images to be added; placeholders under `docs/images/`.*
