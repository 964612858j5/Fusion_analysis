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
| **Step 3** | QC Viewer | Check the cells against the tissue | Confirm / not confirm |
| **Step 4** | Feature Extraction | Measure each cell's value in each channel, export the table | `cell_features.h5ad` (+ CSV when ticked) |

> **Core idea**: the earlier steps are all "preparation and trial". The truly heavy lifting is Step 2 (processing the entire big image), and the final Step 4 produces the table.

<!-- image placeholder: images/01_top_step_bar.png — a real screenshot of the "Step 0 → Step 1 → ... → Step 4" navigation bar at the top, currently highlighting Step 0 -->

---

## 3. Installation and launch

### 1. Install (only needed once)

Linux x86_64 with an NVIDIA card. In a terminal, from the Block01 folder:

```bash
bash scripts/install_block01.sh --check-only   # optional: only check this computer
bash scripts/install_block01.sh
```

It checks the computer, installs the Python environment and the prebuilt
viewer core, a second small environment for Step 4's SpatialData export
(`--no-spatialdata` skips it), shows each segmentation model's licence and downloads it after
you type `yes` (Mesmer comes from DeepCell's public model storage; no account or token is needed),
tests the three engines and prints the start command. Everything goes under
`~/.local/share/block01` (`--root DIR` to choose another place); nothing
already on the computer is changed. If it stops, it says which step failed
and what to do; running it again continues. Details:
`envs/fusion_mesmer/README.md`. A coding agent (Claude Code, Codex) can do it
for you with `docs/install_with_an_ai_agent.md`.

### 2. Launch the software

```bash
scripts/block01
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

1. **Load the image**: enter the path to the raw `.ome.tif`, or the native PhenoCycler Fusion `.qptiff` (channel names come from each channel's Biomarker; a qptiff and an ome.tif converted from it are two different sources, with separate workspaces), and the output folder, click load, and the software shows a **downsampled overview**.
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

**Workspaces — coming back to a project:** every Save writes into a **workspace** (a folder under `<output>/rois/`). When you load a slide into an output folder that already holds workspaces of **the same slide**, Step 0 opens one (it always asks which one; the last one is preselected, so Enter opens it) and restores its region, patches, channel methods, parameters and Intensity. The Load line then ends with `Workspace: <id>`.
- **Save** writes back into the open workspace; with nothing changed it says *No changes* and rewrites nothing.
- **Save ▾ › Save as new workspace** always starts a new workspace; the old one is left as it is and the next steps use the new one.
- If you redrew the analysis region, Save asks first: *Save as new workspace* (recommended) or *Overwrite this workspace*.

**Results (runs):** every button that computes something keeps what it made as a **result** of its own, with the parameters of that moment and the result it was made from: **Step 0 Save** (a Step 0 result, only when the corrected pixels change), Step 1 pre-segmentation, **Save Config & Generate fused.zarr**, **Step 2 Run** and **Step 4 Extract**. A result's science (pixels, labels, the parameters it used) is never rewritten afterwards; only display helpers such as zoomed-out mask levels may be added to it later. Choosing a result in Step 3 (`Use`) brings the chain it was made from with it.
- With parameters changed but not yet generated, Step 2 can still run on an existing fused image; it records the parameters it actually used.
- An output folder written by an earlier version of this program (before results) is not opened; choose another output folder. Nothing in it is changed.

**Deleting (into the trash):** the chooser at Load lists each workspace (newest first) and under it its Step 0 results. The red **×** at a Step 0 result's start deletes it **and every result made from it** (Step 1, Step 2, Step 4); a Step 2 result can also be deleted with the **×** of its row in Step 3's *Segmentation results* window, together with its Step 4 results.
- a confirmation lists everything that moves first; deleting the Step 0 result you are editing says that Step 1 and Step 2 cannot be used until Step 0 is saved again.
- deleted results move to `<output>/.trash/` and are removed automatically 30 days later (checked each time a project is opened). There is no Restore.
- nothing can be deleted while a Step 0 Save, a pre-segmentation, a Generate, Step 2 or Step 4 is running.
- if a move fails half-way, the deletion stops there: the message says what was moved and what is still in place, and what stays is whole.

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

**How to operate:** the parameters are in the left tab **Parameters**, the tile overview and progress in the right tab **Tile Status**; **← Back to Step 1**, **▶ Run Segmentation & Merge** and **⏹ Stop** are the row at the bottom. The left column is as wide as the channel column of Step 0 / 1 / 3 — dragging it on any page moves all of them.

1. Confirm the inputs in **Input Data** (fusion image / parameter file, usually carried over automatically). You can click **Load zarr info & overview** to take a look.
2. **Tile Grid**: set the tile size (how big each cell of the grid is). Smaller tiles use less GPU memory, but the seams need more care.
3. **Segmentation Parameters**: confirm the parameters; you can also **Apply Selected Params** to reuse a previously chosen set. **GPU: Use GPU if available** runs Cellpose and Mesmer on an NVIDIA GPU, or on a Mac with Apple silicon (macOS 14 or newer) on the Mac's GPU; when neither is there, or the box is unticked, they run on the CPU (much slower). Results on GPU and CPU agree closely but not pixel for pixel.
4. Choose the **Output** location, then click **▶ Run Segmentation & Merge** to start. The progress bar shows which tile is being processed; in **Tile Status** the running tile is yellow and finished tiles are green. Changing **Rows / Cols** during a run does not change the run: the new grid is shown with no state (it would point at other parts of the image), and switching back to the run's grid (named under the tile line, `Running grid: …`) shows all its states again.
5. To stop midway, click **⏹ Stop**; if it gets interrupted, **Recovery Mode** can resume merging from the saved intermediate files instead of starting over.

**Skip tiles without tissue** (optional, off by default): tick it under **Tile Grid** to leave out the tiles that hold no tissue — they are not segmented, which saves time and memory on slides with a lot of empty glass. Tissue is found on a coarse (about 1/16) view of the slide's nucleus channel, and a tile is proposed only when its whole window, including the overlap, holds no tissue at all. Ticking the box skips nothing by itself: every tile starts segmented, the line under the buttons says how many tiles have no tissue, and **Skip all empty** skips them. A skipped tile is filled with translucent purple and has a thick purple border, in **Tile Status** and in the zoomable DAPI picture; a tile with tissue that you chose to skip is also hatched. During the run the running (yellow) and finished (green) borders are drawn over the purple. Click a tile to change it at once: a skipped tile goes back to being segmented; to skip a tile that does hold tissue you are asked first (its tissue share is shown) — a skipped tile has **no cells**. To change several tiles, hold **Shift**: while it is held nothing is applied and nothing is asked — **Shift**+click picks a tile (click it again to drop it) and **Shift**+drag picks a box of tiles (all to be skipped if the tile where you started was segmented, all restored if it was skipped). Picked tiles get a white frame and already show the result. When you let go of **Shift**, everything is applied at once, and you are asked one question listing the picked tiles that hold tissue (answer No and those stay segmented). **Skip all empty** applies the automatic proposal (your changes are cleared) and **Skip none** segments every tile. Changing Rows / Cols / Overlap makes a new proposal and starts again from nothing skipped. The run records which tiles were left out, and which of them you chose although they held tissue. The **Tile Status** picture is now sharper (up to 4096 px on its long side).

**Engine changed since Step 1** (2026-09-29): when the segmentation behaviour or the model of the method has changed since the Step 1 result was made, a **Segmentation engine** warning lists what changed before the run — the Step 1 preview was made the old way, so the whole-image result may differ from it. **Run anyway** runs it and records the difference with the result; **Cancel** does not run. Nothing is asked when nothing changed, or for a Step 1 result from before this update (it is only recorded). Installing an unrelated package no longer counts as a change.

**Output:** the whole-image mask `global_mask` (a huge uint32 array / OME-TIFF). In the mask, each object has a unique number (1, 2, 3…), and the background is 0. For whole-cell and expansion methods it holds the cells; for nuclei-only methods it holds the nuclei.

**Nuclei are kept too** by the methods that compute them next to the cells (`Cellpose nuclei (DAPI) + expansion`, `StarDist nuclei + expansion`, `Mesmer nuclear-guided whole-cell`): `global_nuclei_mask` holds the nuclei, each with its own number, and `global_nuclei_cell` says which cell each nucleus belongs to. A nucleus is kept only when it lies wholly inside one cell; a cell may have several nuclei. The terminal and the run's metadata report how many nuclei were predicted, kept and dropped (over several cells, partly on background, outside any cell). Results made before this update have no nuclei file — re-run Step 2 to get them.

**Tile seams** (Step 2 update, 2026-09-27): each tile is segmented with an overlap band, so a cell near a tile border is usually predicted by two (at a corner up to four) tiles. Step 2 now matches these versions and keeps **one**: versions that share at least half of the smaller one's pixels are the same cell, and the version lying deepest inside its own tile wins (if one tile saw one cell where the other saw two, that tile's whole version wins — both cells or neither). A cell that both tiles would have dropped is kept; a cell that would keep less than half its pixels next to a better one is dropped rather than left as a sliver; a nucleus that no longer lies wholly inside its cell is dropped (counted as "on tile seams"). Every cell number now has pixels. Earlier Step 2 results let the later tile overwrite the earlier one's cells on the seams (cut cells, small fragments, nuclei outside their cell) — re-run Step 2 to get the new merge. The terminal and the run's metadata (`seam_merge`, `seam_reconciliation`) report the counts. The overlap must stay wider than the largest cell (Step 1's halo, 200 px by default).

**Oversized objects** (Step 2 update, 2026-09-27): an object far larger than the method makes on real tissue is a prediction artefact — e.g. StarDist can return one huge polygon over a tile that is almost all background — and Step 2 drops it before merging (it would otherwise take the pixels of real cells). The limit is 50 × the median object area of the Step 1 pre-segmentation this run came from (at least 10 000 px); without a usable Step 1 result (fewer than 20 objects, or parameters set by hand) it is 100 000 px. The terminal and the run's metadata (`size_filter`) give the limit, where it came from and how many objects (and their nuclei, "in oversized objects") were dropped.

<!-- image placeholder: images/09_step2_tiles_and_run.png — Step 2's Tile Grid settings + Run button + progress bar -->
<!-- image placeholder: images/10_step2_mask_result.png — a cell mask for a region (each cell a differently colored blob) -->

> ⚠️ **This is the most time-consuming and memory-hungry step.** Make sure the Step 1 parameters are truly satisfactory before running. When resources are tight, make the tiles smaller.

---

### Step 3 · QC Viewer

**What it does:**
Check the Step 2 result against the tissue: the whole slide with the cell mask on top, pan and zoom, jump by Tissue Navigator or patch. It looks and works like Step 1.

**How to use it:**

1. The left column is Step 1's `Channels` frame: `Show all`, `Intensity…`, `Reset weights`, `Load weights` and the channel list with weights.
2. **Step 3 and Step 1 share the same channels, weights and fusion**: a tick or a weight changed here is the same in Step 1 (and is saved in the session like any Step 1 change). Your saved Fusion Settings only change when you save them in Step 1.
3. `Overlay` / `Fusion` switch the mode for Step 1 and Step 3 together; the Tissue Navigator follows what you set here.
4. The right side shows the **whole slide**, like Step 1: drag to pan, scroll to zoom, click in the Tissue Navigator to jump there. Step 0, Step 1 and Step 3 show the same place on the slide at the same zoom when you switch between them (2026-09-30); Step 0, Step 1 and Step 3 also share one page layout, so the picture, the channel panel and the tabs stay exactly where they are on screen. In Step 0 the work area has two tabs, **Background Correction** (the channels) and **Viewer**; Per-Channel Decision and **Save** are the row at the bottom. The channel column has the same width in all three pages; dragged very narrow, the right end of each channel row (the weight box, or Step 0's method box) is hidden under the column's edge until you widen it again. Resizing the window keeps the zoom in Step 1 and Step 3; you just see more or less of the slide.
5. In the Tissue Navigator opened from Step 3, **ROIs are locked** (they come from Step 0 / Step 1 and cannot be drawn, deleted or changed here), while **patches can be added, moved, renamed and deleted** — patch changes are saved and show up in Step 0 and Step 1 too.
6. **Patches**: the row above the picture starts with Step 1's patch buttons (`Patch ▾` lists them all). Click one and the picture jumps there. The chosen patch is the same in Step 1 and Step 3.
7. **Segmentation results on the whole slide.** **`Results…`**, at the right of the `Viewer` tab's row, opens one floating **Segmentation results** window (it stays above the main window; closing it only hides it, what is ticked stays drawn):
   - **one row per result**: the Step 2 results of every workspace of this project on this slide (date, time and a summary of the parameters; the current workspace's first; results of other workspaces say which in `[ ]`), each on its own region, plus any result brought in with **`Load…`** (a result folder from anywhere, accepted only if it was made on this slide — otherwise the hint says why);
   - in each row, **`Cells`** and **`Nuclei`** ticks: every ticked mask of every row is drawn, outlines only, the top row on top. A tick is greyed out when the result has no such mask (a nuclei-only method has no cell mask, a whole-cell method no nucleus mask). The swatch after each tick opens its settings: a preset colour or `Custom…`, `Opacity`, `Width` (1–4) and `Solid` / `Dashed`. Each result gets its own colours, kept for the session;
   - **`Use`** marks the one result Step 4 measures (framed green, `In use`); it does not change what is drawn. The first time Step 3 opens, only the `Use` result's cells are ticked (its nuclei if it has no cell mask); coming back keeps your ticks. A result brought in with `Load…` is ticked the same way;
   - at the top, "all Cells" / "all Nuclei" ticks and a hint for the whole viewer (e.g. no Step 2 result yet, `Masks need the GPU display`); each row has its own status line (zoomed-out masks being prepared, cannot be shown zoomed out, part of the result outside the current ROI — Step 3 shows only the current ROI);
   - the red **×** of a row deletes that result (see *Deleting* in Step 0).
   Masks follow pan, zoom and every jump. Your mask settings are kept while the program runs; nothing is written to your project, except that an older result without zoomed-out levels gets them the first time (in its own run folder).

**Output:** no new files (except the zoomed-out mask levels of an older Step 2 result, once, in its run folder).

---

### Step 4 · Feature Extraction

**What it does:**
Time to **produce the table.** Step 4 goes through every cell of one Step 2 result and measures, in one pass over the image, **its brightness in each channel** — for the whole cell and, when the result kept nuclei, separately for its **nucleus** and its **cytoplasm** — **and its shape**. It saves one table ready for analysis.

**Where the numbers come from:** each channel is read from where Step 0 decided — a channel kept `original` from the raw slide; a channel corrected with TopHat / cuCIM **only** from the correction Step 0 saved. If that saved correction is missing or does not match (another region, other parameters, another slide), Step 4 **stops before measuring anything** and says why — it never falls back to raw pixels and never recomputes the correction. Re-save Step 0's correction in that case.

**How to operate:** the page has the other steps' title bar and bottom row (**← Back to Step 3**, **Batch...**, **⏹ Stop**, **▶ Extract Features**); its settings are one column in between.

1. **Run** and **Region**: Step 4 opens on the result you chose in Step 3 (or the latest Step 2 result). `Browse` picks another run folder; a run with several regions lets you choose one. The **Slide** is the run's own and cannot be changed. The line under it says how many cells and how many channels are read raw or from Step 0's correction. A red line says why a result cannot be measured — e.g. a result from before Step 2 kept a label store (re-run Step 2), a result made on another slide, or a missing Step 0 correction.
   - A nuclei-only result (e.g. StarDist nuclei) has no cell mask: its nuclei are measured as the objects.
2. **What to quantify** — four groups (click the arrow to fold one); only what is ticked is computed:
   - **Statistics**: Mean, Sum, Std dev, Min, Max (all ticked by default). Under them, **Distribution (slow)**: Median, P90, P95, Gini — unticked; ticking one shows a list of markers, tick the few you need (e.g. FOXP3, Ki67). They need every pixel value of each cell, so they take a further pass per marker; `▶ Extract Features` stays disabled until at least one marker is ticked.
   - **Expression regions** (for the expression values): Whole cell (always), **Nucleus**, **Cytoplasm**. Nucleus = the pixels of the cell's own nuclei; cytoplasm = the rest of the cell. They are available (and ticked) when the result kept nuclei beside its cells (the expansion and nuclear-guided methods). Useful e.g. for transcription factors (FOXP3, Ki67 → nucleus) next to membrane markers (CD3 → whole cell / cytoplasm).
   - **Features**: Expression (always), Morphology (ticked), Nuclear summary (per cell: number of nuclei, their area in the cell, mean / largest nucleus, nuclear fraction, cytoplasm area).
   - **Outputs**: h5ad (always), **CSV** (only when you tick it), **SpatialData** (only when you tick it). SpatialData is written by a Python that has the `spatialdata` package: the installer sets one up, so it just works. Only a checkout not installed with `scripts/install_block01.sh` (or installed with `--no-spatialdata`) asks, the first time you tick it, for such a Python (e.g. Step5's environment, `…/envs/fusion_test2/bin/python`) and remembers it.
   - An orange **Risk** line appears for a result made before Step 2's tile-seam fix (2026-09-27) when you measure nuclei / cytoplasm: a few nuclei on tile seams may lie partly outside their cell, so those cells' nucleus / cytoplasm values carry that error; the numbers are still produced and the provenance file counts the affected pixels. Re-run Step 2 to remove it.
3. **Output dir** defaults to `<workspace>/step4/quantification_runs/<run>/<region>/`; you can change it. The line under it names the files and what `X` holds (e.g. `X = cell mean`). Click `▶ Extract Features`.
   - While it computes, the page is frozen: only **⏹ Stop** works. Closing the **Batch** window during a run stops the current sample first, then closes. A run you choose in Step 3 meanwhile is opened when the extraction ends (a line says so).

**Output:**
- `cell_features.h5ad` — **one** AnnData for scanpy, one row per cell (per nucleus for a nuclei-only result):
  - `X`: the whole cell's first ticked statistic, in the order mean → sum → std → min → max (`uns["X_statistic"]` says which);
  - `layers`: every ticked combination, named `<region>_<statistic>` — `cell_mean`, `nucleus_mean`, `cytoplasm_max` … (X's own one included, so the raw values survive a normalisation of X); a region without pixels (a cell without a nucleus) gives NaN;
  - `obs`: `cell_id`, the shape columns and the nuclear summary; `var`: each channel with its source (raw / Step 0's correction, method and parameter); `uns`: what X is, the regions, and the full provenance.
  - `obsm` (only with distribution statistics): one table per region × statistic — `obsm["cell_median"]`, `obsm["nucleus_p90"]` … — whose columns are the ticked markers, e.g. `adata.obsm["nucleus_median"]["FOXP3"]`. Median and percentiles follow numpy (`np.median`, `np.percentile` with linear interpolation); Gini is computed on the sorted pixel values (NaN when every pixel is 0).
- `cell_features.csv` (only when ticked): the same table, one row per cell; the whole-cell columns are named `<channel>_<statistic>`, the others `<channel>_<region>_<statistic>`.
- `cell_features.spatialdata.zarr` (only when ticked): one SpatialData for napari, squidpy or Step5 — `labels/cell_labels` (and `nucleus_labels`) = the segmentation masks with zoom levels; `tables/table` = the same table as the h5ad, linked to the cell labels by `cell_id`, with `obsm["spatial"]` = the cell centres; `shapes/regions` = the TMA cores and measurement exclusions as outlines (when the run has Regions). Coordinates: `global` = slide pixels, plus `micrometers` when the slide records its pixel size. The images are not copied (tens of GB); `uns` names the slide and the corrected channels. In this file `X_statistic` is spelled `x_statistic` (SpatialData does not allow two keys that differ only in case).
- Shape columns: `area`, `centroid_y` / `centroid_x` and the bounding box `bbox_min_y`, `bbox_min_x`, `bbox_max_y`, `bbox_max_x` (max exclusive) in slide pixels, `major_axis`, `minor_axis`, `eccentricity`, `orientation` (as in scikit-image `regionprops`), `equivalent_diameter`, `aspect_ratio` (major / minor; empty when the minor axis is 0), `extent`, `boundary_pixel_count` — the cell's pixels that touch another cell or the background in their 3 × 3 neighbourhood (a QC count, **not** the old `perimeter` column, which missed the border between two touching cells) — then `perimeter_crofton`, the cell's perimeter length in pixels estimated by the Crofton formula (as scikit-image `perimeter_crofton`, 4 directions), and `circularity` = 4π × area / perimeter_crofton² (1 for a perfect disc; very small cells can come out slightly above 1 because their perimeter is hard to estimate — the value is kept as computed).
- `cell_features_provenance.json`: what was measured and from where — the run and region, every channel's source, the label numbers without pixels, nucleus pixels outside their cell (for older results), and the time taken.
- Earlier versions' axis lengths and eccentricity had a rounding error far from the image origin; values from this version differ from old tables there (by design).
- **Batch...** writes the h5ad for every sample; tick `Also write CSV` there for the CSV too.

<!-- image placeholder: images/12_step4_feature_options.png — Step 4's statistics checkboxes + path settings -->
<!-- image placeholder: images/13_step4_output_table.png — what cell_features.csv looks like when opened: rows = cells, columns = per-channel means -->

> 🎉 **That's the entire pipeline done.** You went from a tens-of-GB image to a clean "cell × marker" data table.

---

## 5. Quickest path to get started (read this if you're in a hurry)

```
1. scripts/block01                     # launch
2. Step 0: pick image → draw ROI → set DAPI as nucleus → remove background → pick a few preview patches → Process
3. Step 1: adjust channel weights for clear outlines → choose method → try params with Phase1+Phase2 → save
4. Step 2: confirm params → set tile size → Run, let it finish
5. Step 3: spot-check a few regions, confirm circling is accurate
6. Step 4: check the run → ▶ Extract Features → get cell_features.h5ad
```

> There are also **Skip → Step 2 / 3 / 4** buttons at the top: if some intermediate results are already done, you can jump straight ahead.

---

## 6. FAQ

| Problem | Likely cause / fix |
|---------|--------------------|
| `scripts/block01` says *cannot start* | It names the reason and the fix; usually: run `bash scripts/install_block01.sh` (again) |
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
