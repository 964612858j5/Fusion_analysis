# TMA demo data from published sources (backlog 10) — plan v0.3

Started by the user (TG) 2026-11-11: the German main server (all TMA data)
is down. For the boss demo of Step 0.5, find published human TMA images
(any disease) as TIFF; 4-6 cores are enough; a whole TMA that is downloaded
is cropped to 4-6 cores into a new TIFF and the full file deleted.

Nothing here goes into the repository: the data lives under
`work/data/block01/tma_demo/` (outside the repo), the scripts in the
scratchpad. This plan is the record.

## 1. What the app needs (measured on `cropped_region.ome.tif`)

* One OME-TIFF holding the whole array (Find cores looks for several cores
  on ONE slide image): axes CYX, BigTIFF, tiled 512, LZW, a pyramid
  (sub-resolutions), the channel names and `PhysicalSizeX` in the OME-XML.
  Our sample is uint8; uint16 is read as well (to be checked on the chosen
  file).
* A channel named exactly `DAPI` (`ui/regions/cores_actions.py find_cores`).
* For numbering and per-core analysis today, a map sheet workbook (backlog
  11 removes the need for Find cores only).

Per-core files (one core per image) therefore do not show Find cores
unless several are put side by side into one image.

## 2. Candidates (searched 2026-11-11; headers read remotely, nothing large downloaded)

| | Dataset | What it is | Size | Cores | Channels | Licence | Fit |
|---|---|---|---|---|---|---|---|
| **A** | MCMICRO exemplar-002 (CyCIF, Harvard LSP; also Zenodo 7622545) | a real 2×2 TMA: 2 meningioma, 1 GIST, 1 normal colon; **raw tiles** (10 cycles × 30 tiles, 1280×1080, uint16), not stitched | 2.7 GB zip / 3.3 GB | **4** | 40 (10 cycles × DNA + 3 markers: CD3D, CD4, CD8A, CD45, FOXP3, keratin, E-cad, SMA, CD31, CD163 …), 0.65 µm | CC BY 4.0 (Zenodo copy; mcmicro.org says CC BY-NC 4.0) | best: exactly one 4-core TMA, but needs stitching (ASHLAR) |
| **B** | HuBMAP OMAP-15 intestine, PhenoCycler TMA (Zenodo 10963121) | one whole-TMA `.qptiff`, tiled, 8-bit, 29760×51120 | 14.4 GB | ~7 tissue samples | DAPI confirmed; count not read yet | CC BY 4.0 | a real TMA in one file: download, crop 4-6 cores, delete — exactly the requested procedure |
| C | HuBMAP OMAP-7 lung, Cell DIVE (Zenodo 7731162) | one pyramidal OME-TIFF **per core** | 2.8-3.9 GB per core | 34 | 33 incl. DAPI, 0.325 µm | CC BY 4.0 | images are clean but one core per file: would have to be placed side by side (a composite, labelled as such) |
| D | Follicular lymphoma Vectra (Mendeley 274xbhc5rx) | inForm component TIFFs per core, float32, 2420² | ~1 GB for 4-6 | 349 | 8 incl. DAPI | CC BY 4.0 | small, but single fields, 8 channels, conversion needed |
| E | SPACEc tonsil TMA (Zenodo 12694429) | one TIFF | 1.1 GB | 2 | 58 + DAPI | CC0 | only 2 cores |

Not usable without an account or impractical: EMIT TMA11/22 (Synapse
login), Schürch CRC CODEX (2 TB via Aspera), HTAN (login), Akoya /
Lunaphore samples (on request).

## 3. Proposed: A, with B as the fallback

**A — exemplar-002, stitched.**
1. Download the 10 cycle files and `markers.csv` (3.3 GB) into the
   scratchpad.
2. Stitch and register the cycles with ASHLAR (Harvard LSP's stitcher, the
   one MCMICRO uses) in a separate scratch environment. It reads the tiles
   through Bio-Formats, so it needs a Java runtime: this Mac has none; one
   is installed into that scratch environment only (OpenJDK from
   conda-forge), nothing system-wide.
3. Write the app's format: one pyramidal OME-TIFF (tiled 512, LZW), 40
   channels named from `markers.csv`; the first cycle's DNA channel is
   named `DAPI`, the other cycles' DNA channels `DAPI_2` … `DAPI_10`.
   Pixel size 0.65 µm.
4. Check: tifffile reads it like the sample; the app opens it, Step0 shows
   it, Step 0.5 Find cores (with a demo map sheet, §4) finds 4 cores.
5. Delete the raw download.

If ASHLAR cannot be made to run here in reasonable time: B.

**B — OMAP-15, cropped.** Download the qptiff (14.4 GB; 109 GB free here),
read its channel names and pixel size, choose a window holding 4-6 cores
on the overview, write it as the OME-TIFF of A.3 (8-bit like our sample),
delete the qptiff.

## 4. A demo map sheet (optional, asked)

Find cores needs a map sheet until backlog 11 is done, and numbering and
per-core analysis need one anyway. A small workbook in the app's format
(2×2 for A), with the published tissue types as donor names and
`DEMO -- not from the source publication` in its Information sheet, would
let the whole Step 0.5 flow be shown. It describes the layout only; it
invents no clinical data.

## 5. Where things go, and what is deleted

* Result: `work/data/block01/tma_demo/<name>.ome.tif` (+ `SOURCE.txt`:
  dataset, DOI / URL, licence, citation, what was done to it; + the demo map
  sheet if wanted).
* Deleted after the check: the raw download (A) or the full qptiff (B), and
  the scratch environment.

## 6. Before the download: disk space and clean-up

User (TG) 2026-11-11: route A agreed; academic use; first check the free
space and clean up files this project's development left behind.

Free now: **109 GB** of 228 GB. A needs about 3.3 GB (raw) + ~3 GB (result)
+ ~1 GB (Java / ASHLAR scratch environment), all temporary except the
result.

Measured (read-only), what development left behind and can be removed
(everything is either regenerated on its own or was a one-off test copy):

| What | Size | Note |
|---|---|---|
| This session's scratchpad: test HOME `btn_inv` (1.1 GB), `g6` (6.9.3 wheels and test logs, 1.1 GB), project copies `proj_copy`/`proj_copy2` (0.4 GB), Mesmer download / probe copies `hf_mesmer`/`mesmer_s3`/`tfprobe` (0.6 GB), `maturin_venv`, `core_target`, `cmp`, `cn_diag`, `hy_diag` | **3.8 GB** | kept: the small scripts (`pertest.sh`, `g6/*.sh`, `g6/gnushim`) and the codex review outputs (a few KB) |
| Two git worktrees in the scratchpad (`wt_head` at 7361b9b, `sd_wt` at 53a8f51) | (in the 3.8 GB) | removed with `git worktree remove`, so the repository forgets them too |
| pytest's temporary folders (146 runs) | **0.9 GB** | pytest makes new ones each run |
| pip's download cache | **1.2 GB** | downloads again when needed |
| micromamba's package cache (`work/micromamba/pkgs`) | **2.4 GB** | `micromamba clean --all`; the environments themselves are untouched |
| `__pycache__` folders in the repo | small | regenerated |
| **Total** | **≈ 8.3 GB** | |

Not touched (not development leftovers, or not this project's):
* `work/data/block01/test` (9.9 GB): your test project with its full-slide
  workspace (`rois/full_wsi_20261109_…`) — your data; say if it may go.
* `work/data/block01/cropped_region.ome.tif` (0.9 GB): the sample.
* `work/envs/block01_mac` (2.8 GB), `work/models`: in use.
* `Fusion_analysis_step5N`, `envs/fusion-step5n` (2.3 GB),
  `data/step5n*` (1.3 GB), `envs/landscape_h`, `a2t`, `easytier`: other
  projects / tools.

## 7. Questions for the user

1. ~~A or B~~ — A (user, 2026-11-11).
2. ~~Demo map sheet~~ — yes (user, 2026-11-11).
3. ~~Licence~~ — academic use (user, 2026-11-11): CC BY-NC 4.0 fits;
   cited in `SOURCE.txt`.
4. ~~Clean-up~~ — done as listed (user, 2026-11-11); `data/block01/test`
   kept. Removed: the scratchpad's large folders (3.7 GB; the two worktrees
   with `git worktree remove`, `sd_wt`'s uncommitted diff kept as
   `sd_wt_uncommitted.patch`), pytest's temporary folders, pip's cache,
   micromamba's package cache, `__pycache__`. Free space 109 → 115 GB.
   Checked afterwards: `block01_mac` imports numpy, torch, PySide6, the
   viewer core and `block01.main`; `fusion-step5n` starts.

## 8. Done (2026-11-11)

* Downloaded the 10 cycle files (3.3 GB) and `markers.csv`; ASHLAR 1.20.0
  with OpenJDK 17 in a scratch environment stitched and registered them
  (3 min). Cycle 1: "could not align enough edges", stage positions used,
  faint tile seams; cycles 2-10 registered (DNA of cycles 1 and 10 overlap
  on a full-resolution crop).
* `work/data/block01/tma_demo/exemplar002_tma_4cores.ome.tif` (2.8 GB):
  CYX uint16 40 channels, 6365 × 6308, levels /4, tiles 512, LZW,
  0.65 µm, `DAPI`, `DAPI_2` … `DAPI_10` and the markers' names. Not cropped:
  the whole image is the 4 cores (+ a sliver of a neighbour at the right
  edge).
* Check: the app's `dearray.detect_cores` (diameter 1.4 mm) finds the 4
  cores at level 2 in 1 s (radii 1110-1270 px ≈ 1.5 mm).
* `exemplar002_DEMO_map_sheet.xlsx`: 2×2, donors `Core_A` … `Core_D`,
  diameter 1.5 mm, marked DEMO in its Information sheet; the app's parser
  reads it.
* `SOURCE.txt`: source, licence, citation, processing.
* Deleted: the raw download, ASHLAR's output, the scratch environment.
  Free space 112 GB.
