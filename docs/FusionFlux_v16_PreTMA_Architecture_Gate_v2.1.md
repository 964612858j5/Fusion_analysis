# FusionFlux v16 — Pre-TMA Architecture Gate v2.1

**Status:** **v2.1 APPROVED** by the user on 2026-09-29 — the authoritative v16 architecture / pre-TMA plan. Every stage below still needs its own application + whitelist (P0 rules: `AGENTS.md`, `docs/P0_SCOPE_RULES.md`). Also authorised on 2026-09-29: generating the 2×2 synthetic mosaic (§8). **Done 2026-09-29** (execution record in §8).
**Line:** v16 = this architecture gate + TMA Foundation + QualityMask (blur / fold / low tissue). v15 is frozen at `f93f195` (tag `v15-final`; §14).
**Time budget:** 10–15 effective working days.
**Hard stop:** TMA Foundation starts as soon as the six start gates (§9) pass.

### Changes from v2 Lean

| # | Change | Why |
|---|---|---|
| 1 | NGFF (A2b, Gate 2) is conditional on the A0 benchmark; a pre-written adoption rule decides | v2 required NgffSource before A0 had decided |
| 2 | `SegmentationRun` is its own analysis dimension; a cell is identified by `(segmentation_run_id, region_id, cell_id)` | cell IDs are 1..N per region per Step2 run; a rerun renumbers |
| 3 | "real WSI" → "representative large-image dataset"; a real WSI is a supplementary check later | no real WSI available (server down) |
| 4 | New A0.5: engine identity covers only what changes segmentation, done before pyarrow is installed | `engine_identity.lock_hash` hashes the whole environment lock |
| 5 | NGFF 0.4 with the installed zarr 2.18; no `ome-zarr-py`, no zarr v3 | zarr v3 would touch every zarr reader/writer |
| 6 | A2 split into A2a / A2b / A2c; consumers migrate along their real read boundary | Step2 reads Step1's `fused.zarr`, not the slide |
| 7 | Gate 3 invariance is tiered (bitwise / exact / established tolerance) | whole-file bitwise equality would fail correct code on a changed float summation order |
| 8 | A1: no "rotation"; test design fits the GL-context limit; Windows DPR is a real-machine check | the viewer has no rotation; ~5 GL contexts per process abort under D3D12 |
| 9 | A3 conventions: integer pixel index = pixel centre; bbox half-open; the transform rule binds new code and TMA code only | keeps Step4 outputs unchanged; avoids retrofitting the whole codebase |
| 10 | A4: `qc_valid` removed; Parquet vs h5ad authority stated | QC semantics belong to QualityMask, not yet designed |
| 11 | A5 has two tiers: the current dev machine (bounded, no OOM, record commit/page file) and the future 16 GB edge profile | this machine has ~10 GB RAM |
| 12 | Test data and disk policy (§8) | C: has ~40 GB free |
| 13 | `analysis.h5ad` lives per segmentation run: `analysis/<segmentation_run_id>/analysis.h5ad` | a Step2 rerun renumbers cells; one root h5ad would be ambiguous |
| 14 | Pre-TMA Parquet is canonical only for identity, coordinates, bbox and basic morphology; expression and compartment statistics stay canonical in the Step4 h5ad | §6.3 and §6.4 contradicted each other |
| 15 | `regions.parquet.qc_status` removed | same reason as `qc_valid`: QC semantics belong to QualityMask |
| 16 | NGFF 0.4 compliance check on what we write (A2b) | writing without `ome-zarr-py` must still be spec-conformant, not just "zarr opens" |
| 17 | Pyramid transforms use each level's real shape ratio; missing `PhysicalSize` means `physical_um` unavailable | the current slide's levels are 15437→3859→964 (level-1 scale 4.0003 y / 4.0007 x), not exactly 4; never guess mpp |

---

## 0. Scope principle

Freeze only what would be expensive or scientifically dangerous to change after TMA work starts:

- pixel/data-source abstraction;
- coordinate systems;
- object identity;
- viewer geometric stability;
- LabelStore compatibility;
- a basic object table;
- bounded resource behaviour.

### Scope firewall

A newly found requirement enters this gate only if at least one holds:

- TMA would otherwise create a second, incompatible data model;
- changing it later would require rewriting stored TMA results;
- it affects scientific correctness or coordinate identity;
- it stops a representative large image from running without OOM;
- it causes a visible geometric inconsistency between Step0, Step1 and Step3.

Everything else is deferred (§11).

---

## 1. Target architecture frozen before TMA

```text
                ┌─────────────────┐
                │   PixelSource   │
                │ OME-TIFF        │
                │ OME-NGFF (if A0)│
                └────────┬────────┘
          ┌──────────────┼──────────────┐
       Viewer        Step1 fusion     Step4
     Step0/1/3     (→ fused.zarr     quantification
                    → Step2)
          └───────┬──────┴──────┬───────┘
             LabelStore     Object tables (Parquet)
                  └──────┬──────┘
                    analysis.h5ad
                         │
                   Step5 / NEXUS
```

### Frozen principles

- **Pixels:** one `PixelSource` contract. OME-NGFF / OME-Zarr is the storage direction only if A0 supports it (§2.3). OME-TIFF stays supported either way.
- **LabelStore:** the scientific authority for segmentation identity and the cell↔nucleus relation.
- **Parquet:** the canonical object table (§6.3).
- **AnnData:** the canonical analysis state (§6.3).

### Not frozen

`expression.zarr`; DuckDB / Polars / DataFusion; FlowSOM / microclusters; a Rust Step5 core; a Rust viewer; a Rust Step2; the NEXUS redesign.

---

## 2. Stage A0 — Read-only diagnosis and contract drafts

**Budget:** 1.5–2 days. **Production code:** none. Instrumentation lives in scratch copies or `scripts/`.

### 2.1 Questions

1. What causes the Step0 / Step1 / Step3 viewer shift?
2. Does OME-Zarr materially improve the workloads FusionFlux runs?
3. What is the smallest stable `PixelSource` contract?
4. Which coordinate and identity fields must be frozen before TMA?

### 2.2 Viewer shift diagnosis

Start from what exists: `ui/shared_camera.py` (`CameraSnapshot(dataset, cx, cy, scale, origin)`, block C4.5a) and MainWindow's `_capture_camera_of` / `_apply_shared_camera_to`, which apply the camera and read it back.

For every transition record:

- source and ROI identity;
- drawable width / height and devicePixelRatio;
- world centre x / y;
- world units per device pixel;
- visible world rect;
- active pyramid level;
- camera update source / reason;
- timestamp.

Transitions to test at minimum: Step0→1, Step1→3, Step3→1, Step1→0, Step0→3→0.

Candidate mechanisms to confirm or exclude:

- the target layout is not final when the camera is applied;
- the read-back after apply captures a clamped or pre-layout value;
- an automatic `fit` / `autoRange` / `setRange` (Step0 is a pyqtgraph ViewBox);
- a second restore;
- integer rounding;
- different drawable sizes;
- a pyramid-level change;
- a source rebind;
- a Navigator callback;
- coarse / fine publication;
- Qt high-DPI mapping.

Output: a short report giving, for each transition, Δcentre x/y, Δscale, the first call that changes the camera, and the root cause. **No generic CameraState framework unless the evidence shows that a local fix cannot work.**

### 2.3 Storage decision benchmark

Dataset: the representative large-image dataset (§8).

Compare:

- **Current:** OME-TIFF plus the existing corrected zarr.
- **Candidate:** NGFF 0.4, written with the installed zarr 2.18.

Measure:

- cold and warm random ROI read;
- viewport pan / zoom;
- channel switch;
- 29-channel sequential access;
- Step1 fusion reads;
- Step4 sequential scan;
- compressed size;
- ingest time;
- peak RAM;
- CPU utilisation.

External SSD numbers are taken only once an external SSD is available.

**Adoption rule, written before the run:** NGFF becomes the default pixel plane for new projects only if it gives:

- at least a 1.5× gain on cold random ROI reads or on viewport loading, **and**
- no regression greater than 10 % on the Step4 scan or on Step1 fusion reads, **and**
- a disk cost the user accepts: NGFF copy size ÷ source size is reported.

"Raw stays OME-TIFF; only derived products use NGFF" is evaluated as an explicit alternative. If the rule fails, A2b and the NGFF half of Gate 2 move to the backlog.

### 2.4 Minimum layout v1 (drafted in A0, frozen in A3)

```text
<project>/
├── image.ome.zarr/                  # only if A0 adopts NGFF (new projects)
├── segmentations/
│   └── <segmentation_run_id>/
│       ├── metadata.json            # engine identity, operates_on: [region_id, ...]
│       └── <region_id>/
│           ├── cells                # LabelStore raster
│           ├── nuclei
│           └── relation             # nucleus_to_cell
├── objects/
│   └── <segmentation_run_id>/
│       ├── cells.parquet
│       └── regions.parquet          # or one project-level regions.parquet (decided in A3)
├── analysis/
│   └── <segmentation_run_id>/
│       └── analysis.h5ad            # an analysis_run_id level is added only when needed
├── transforms.json
└── provenance.json
```

The mapping from today's layout (`rois/<workspace>/step2/segmentation_runs/<run>/`) is part of the draft. **Old projects are not migrated.**

### 2.5 A0 acceptance

- [ ] Viewer shift root cause demonstrated with logs.
- [ ] Benchmark report exists, with the adoption rule applied.
- [ ] PixelSource, coordinate and object-schema drafts exist.
- [ ] No production code changed.

#### Execution record: A0 (2026-09-29; application `docs/v16_A0_application.md` v2, approved)

**Automatic part done. The real-machine camera log (ruling 1) is still pending.**

- **Viewer shift (A0-1)** — `docs/v16_A0_viewer_shift_report.md`, `scripts/diagnose_v16_a0_camera.py`. Runs used a path-rewritten copy of the test1 workspace; the original project's tree fingerprint was unchanged before and after every run.
  - **C1 confirmed.** The Step1/3 GPU layer fills the whole graphics viewport but draws the ViewBox range, and the ViewBox is inset 9 px per side. Compared with Step0 at the same camera, the picture is magnified 1.4 % in x and 2.3 % in y. Phase correlation in real GL measured the corner shifts within 0.5 px of the prediction.
  - **C2 confirmed.** `Step1WholeSlideMount.apply_camera` rounds the rectangle to integers, then refits it through `setRange(rect)`. The error is ±0.5 L0 px per entry, and it drifts systematically: cy moved −52 px after 50 × 0→1→3→1→0.
  - **C3 confirmed.** Layout changes after the apply (dock, bottom bar, channel floor) shrink the ViewBox, pyqtgraph refits, and the live sink writes the new scale back. The scale changes by −2.49 % offscreen and −1.72 % in real GL, on the first Step1 entry only.
  - **C4 excluded** for a same-source reload. **C6** (HiDPI) is left for the real machine.
  - Entering Step0 is exact. Each cause is local, so no CameraState framework is needed (stop rule 1).
- **Storage (A0-2)** — `docs/v16_A0_storage_benchmark.md`, `scripts/bench_v16_a0_storage.py`. Workloads were pre-registered.
  - **The adoption rule fails.** Gains: cold ROI 2048² 1.30×, level-0 viewport 1.46×, both under the 1.5× threshold. Regression: the Step4 scan is 1.95× slower (cold), against a 10 % limit. NGFF size is 2.01× the source with lz4.
  - **A2b and the NGFF half of Gate 2 move to the backlog.**
  - The alternative of storing derived products is worth weighing in A2: a stored corrected level 1 is 22× faster than today's runtime reduction and bitwise equal to it.
  - A supplementary, non-gating run with a direct-byte NGFF reader shows the Step4 gap follows compressed size and decode cost, not zarr-python overhead.
  - The NGFF copies were deleted. "Cold" means cold only for the WSL guest.
- **Contracts (A0-3)** — `docs/v16_contracts_draft.md`: PixelSource, coordinates (`world = global_pixel + 0.5` adapter rule, `bbox_fullres` `[y0, y1, x0, x1]` ↔ `bbox_x0..y1`, real per-axis level ratios, µm only from OME) and object tables. `region_id` ← `roi_id`, never `roi_name`. `slide_id` does not exist yet. Two findings are listed, not fixed: `image_mpp = 0.5` is hard-coded while the slide is 0.50686 µm, and `model_checksum` is not a weight-file hash (input to A0.5).
- **Product code unchanged**: `git status` / `git diff` list only whitelist paths.

---

## 3. Stage A0.5 — Engine identity scope fix

**Budget:** ≤ 0.5 day. **Must land before any new package (pyarrow) is installed.**

Today `seg_runner/runner.py:engine_identity` includes `lock_hash`, which hashes all of `conda-linux-64.lock` + `requirements-pip.txt`. Any package added to the environment, even one unrelated to segmentation, changes every engine's identity. Step2 then reports "engine identity changed" against the Step1 contract.

**Target:** identity covers only what changes segmentation output:

- the runner code hash (already present);
- the engine package versions (`lib_versions`, already present);
- the versions of the preprocessing libraries the engine actually uses (numpy, scipy, scikit-image, TensorFlow / torch, as applicable);
- the model checksum (already present);
- the relevant device / CUDA mode, reported as today.

Old pre-segmentation runs whose identity still carries `lock_hash` must keep a defined behaviour. The application states which: accepted with a note, or reported once.

Tests: `tests/test_preseg_contract.py` and the Step2 contract check.

---

## 4. Stage A1 — Viewer zero-drift fix

**Budget:** 1–2 days. **TMA blocker:** yes. Viewer changes need their own P0 approval, with a whitelist narrowed to the functions A0 names.

### 4.1 Requirement

Switching between Step0, Step1 and Step3 must be visually stationary: no visible jump, drift, zoom or resize.

### 4.2 Implementation rule

Fix the diagnosed cause with the smallest change. Preferred order:

1. remove an unwanted second camera update or read-back;
2. apply the camera only after the target drawable geometry is final;
3. remove premature integer rounding;
4. prevent `fit` / `autoRange` after a restore;
5. keep source / coarse / fine / mask updates from recentring.

A formal shared CameraState is introduced only if these are insufficient (stop rule 1).

### 4.3 Invariants

Same source + same ROI + same user camera ⇒ same world anchor and same world-units-per-device-pixel. Drawable sizes may differ between pages.

### 4.4 Forbidden post-switch camera mutations

These may change content but never camera geometry:

- coarse plane arrival;
- fine tile arrival;
- mask tile arrival;
- pyramid-level replacement;
- GPU texture upload;
- channel enable;
- Intensity change;
- mask show / hide;
- a source refresh that resolves to the same source;
- Navigator redraw.

### 4.5 Tests

**Automatic:**
- Transform-level assertions on the camera state: exact equality, or float tolerance where arithmetic is involved.
- A long switching loop, 50 × (Step0→1→3→1→0), on offscreen / non-GL paths.
- Real-GL checks are split into separate processes so that none opens more than about 4 GL contexts, because of the known D3D12 limit.
- Gate: no cumulative drift, no systematic centre shift, no scale drift.

**Real machine (user):**
- Overlay, Fusion, Intensity;
- cell mask, nucleus mask;
- patch jump, Navigator jump;
- coarse→fine;
- a newly enabled channel;
- Step1↔Step3 hot switching.
- Windows 125 % / 150 % display scaling can only be checked here.
- Acceptance: no visible movement.

**Known issues:** the Step1 viewer that is occasionally black, and the ~6.5 s GUI stall on the first Step3 GPU `resizeGL`, are noted. They enter A1 only if A0 shows a shared cause; otherwise they go to the backlog.

---

## 5. Stage A2 — PixelSource (+ NGFF if adopted)

**Budget:** 4–5 days in total. **TMA blocker:** A2a yes; A2b only if A0 adopts NGFF.

### 5.1 Contract (smallest interface existing consumers need)

```text
source_identity()
channel_names()
dtype(channel)
level_count()
level_shape(level)
level_downsample(level)
read_region(channel, level, y0, y1, x0, x1)
read_tile(...)
```

Optional performance hints are allowed; scientific behaviour never depends on backend-specific fields.

### 5.2 Split

- **A2a — contract + adapters for existing sources; zero behaviour change.**
  - `OmeTiffSource` absorbs today's readers, including Step4's `TiffTileReader`.
  - Step0's committed corrected zarr is exposed through the same interface.
  - Step4's fail-closed source contract (`core/quant_sources.py`) is kept unchanged.
- **A2b — `NgffSource` + ingest; conditional on A0.**
  - One ingest operation turns an OME-TIFF into NGFF 0.4.
  - The source image is left untouched.
  - A physical merge of raw + corrected into one hierarchy is not required: raw is uint8, corrected is float32, covers only some channels and is stored per ROI.
  - **Compliance check:** what we write must satisfy NGFF 0.4, not merely open in zarr: `multiscales` (version, axes with type/unit, datasets with `coordinateTransformations` scale per level), `omero` channel metadata if written, labels metadata if labels are written. It is verified by a minimal in-repo schema / reference check, with no new dependency.
- **A2c — consumer migration along real read boundaries, one consumer per application:**
  - **Step4:** reads raw slide pixels + the corrected zarr.
  - **Step1 fusion:** reads raw slide pixels and writes `fused.zarr`.
  - **Step2:** reads Step1's `fused.zarr`, which is a derived product, not a slide. It is left as is unless the fused product itself moves.
  - **Viewers (Step0 / 1 / 3):** separate P0 approval; Step0 may keep internal compatibility code.

### 5.3 LabelStore compatibility (unchanged semantics)

- Cell IDs are contiguous 1..N; nucleus IDs are contiguous 1..M.
- Nucleus → cell is unique; a cell has 0, 1 or many nuclei.
- `nucleus_to_cell[0] = 0`.

NGFF labels may become the raster representation; LabelStore remains the semantic authority.

### 5.4 Invariance rule (used by Gate 3)

| What | Required |
|---|---|
| uint8 / uint32 source pixels and labels | bitwise identical |
| integer-derived morphology (moments, areas, bbox, Crofton counts) | exact |
| float features (corrected channels, accumulated statistics) | bitwise if the execution order is unchanged; otherwise within the tolerances already established in S4-1 / S4-3 |

### 5.5 A2 acceptance

On the representative dataset, via each available route:

- [ ] viewer pixels are correct;
- [ ] A1 still passes;
- [ ] Step2 completes;
- [ ] N3 passes;
- [ ] the LabelStore contract passes;
- [ ] Step4 S4-3 outputs meet §5.4;
- [ ] an old OME-TIFF project still opens.

---

## 6. Stage A3 — Coordinate and identity contract; A4 — Object layer v1

**Budget:** A3 1–2 days; A4 1–2 days. **TMA blocker:** yes.

### 6.1 Identity

Biological / physical hierarchy:

```text
Study └─ Sample └─ Slide └─ Region
```

A TMA core will be `Region.type = TMA_core`.

`SegmentationRun` is an analysis dimension, not a node in that hierarchy:

```text
SegmentationRun { segmentation_run_id, slide_id, operates_on: [region_id, ...] }
```

- The stable cell key is **`(segmentation_run_id, region_id, cell_id)`**; `cell_id` alone is not unique.
- `patient_id` is cohort metadata, not pixel identity.
- A3 maps today's project manifest, ROI index and workspace names onto these IDs.

### 6.2 Coordinates

Names:

- `global_pixel`
- `physical_um`
- `pyramid_level`
- `tile_local`
- `region_local` (a TMA core's local frame)

Conventions to freeze:

- axis order (y, x) for arrays and (x, y) for points, stated in the schema;
- **an integer pixel index is the pixel centre** (today's convention, so Step4 outputs are unchanged);
- **bbox is half-open, [x0, x1) × [y0, y1)**;
- µm from the OME `PhysicalSizeX/Y`. The current slide has 0.50686 µm. **If the source has no physical size, `physical_um` is marked unavailable; an mpp is never assumed.**
- **Pyramid transforms use each level's actual shape: scale_y = H0 / HL, scale_x = W0 / WL**, non-integer and possibly different in x and y. Never `2**level` or a nominal factor. The current slide's levels, 15437 × 16215 → 3859 × 4053 → 964 × 1013, give level 1 scales of 4.0003 (y) / 4.0007 (x) and level 2 scales of 16.013 (y) / 16.007 (x), not 4 and 16.

`transforms.json` holds the explicit transforms: `global_pixel ↔ physical_um`, `↔ region_local`, `↔ pyramid_level`.

The rule "no silent `x / 2**level`, `x - bbox_x0` or `x * mpp`" binds **new code and TMA code**. Existing code is not retrofitted in this block.

### 6.3 Authority boundary

- **Parquet object tables are canonical (Pre-TMA)** for segmentation identity, coordinates, bbox and basic morphology / topology (areas, nucleus count).
- **The Step4 h5ad (S4-3 output) stays canonical** for expression matrices and compartment statistics.
- **`analysis/<segmentation_run_id>/analysis.h5ad` is canonical** for clusters, annotations, embeddings and analysis state.
- Copies between them (e.g. Parquet columns in h5ad `obs` for Scanpy) are rebuildable snapshots, not a second truth. Whether Step4 statistics move into Parquet is decided with the Step5 edge redesign, not now.

### 6.4 cells.parquet v1

```text
segmentation_run_id, region_id, cell_id, slide_id,
x_global, y_global,
bbox_x0, bbox_y0, bbox_x1, bbox_y1,      # half-open, global_pixel
cell_area, nucleus_count, nucleus_area
```

No `qc_valid`: QC semantics come with QualityMask. Step4 expression / statistics columns are not copied here (§6.3). Polygons are not stored: the LabelStore raster is authoritative, and shapes are rebuilt on demand.

### 6.5 regions.parquet v1

```text
region_id, slide_id, type, name,
bbox_x0, bbox_y0, bbox_x1, bbox_y1,      # same half-open convention as cells
parent_region_id
```

No `qc_status`: whether quality lives in the region table, a separate quality table or a raster mask is decided when QualityMask is designed.

Future types include ROI, TMA_core, tumor_region, blur, fold, niche and manual_annotation. Only schema compatibility is frozen now.

### 6.6 Dependencies and query engine

- Parquet needs `pyarrow`, installed only after A0.5, and recorded in the `envs/fusion_mesmer` manifests.
- No DuckDB / Polars / DataFusion benchmark; the query engine is replaceable.

### 6.7 Acceptance

- [ ] A3 conventions written and frozen.
- [ ] A TMA core fits without a second coordinate model.
- [ ] cells.parquet generated from one real result; every key matches LabelStore.
- [ ] Coordinates match the viewer and the labels.
- [ ] regions.parquet represents the existing ROIs.
- [ ] `analysis/<segmentation_run_id>/` path used by anything that writes analysis state in this block.

---

## 7. Stage A5 — Resource boundedness gate

**Budget:** 1–2 days. The gate is about **boundedness**, not about proving the 16 GB product target now.

### 7.1 Record, for viewer, Step2 and Step4

- peak RSS;
- peak VRAM;
- tile size;
- queue depth;
- cache bytes;
- time.

On Windows, also:

- working set;
- commit size;
- system commit;
- page-file growth.

### 7.2 Requirement

No new path contains an intentionally unbounded image queue, decoded-tile queue, segmentation staging queue, label queue, cache, or an object list proportional to total WSI pixels. Existing buffers with a practical maximum are documented, as an inventory.

### 7.3 Two tiers

- **Current dev machine** (WSL2, ~10 GB RAM, RTX 3060 Laptop 6 GB): bounded, no OOM. Peak RSS, WSL / Windows commit and page-file growth are recorded.
- **Future edge profile** (16 GB RAM, 6 GB VRAM, external SSD):
  - RAM: target ≤ 12 GB, hard goal < 14 GB;
  - VRAM: target ≤ 5.0 GB, hard goal < 5.5 GB.
  - Missing this tier does not delay TMA if the architecture is bounded and the bottleneck is named.

### 7.4 Deferred

- a read / infer / write overlap redesign;
- an adaptive tile scheduler;
- CPU StarDist + GPU Cellpose as a product path;
- Rust IO;
- a 10 M-cell Step5 engine.

---

## 8. Test data and disk policy

C: has ~40 GB free. WSL frees space on C: only after the user compacts `ext4.vhdx`. Real-engine runs grow the Windows page file by ~15 GB.

### Representative large-image dataset (now)

**2×2 mirrored synthetic mosaic** of `~/fusion_data/cropped_region.ome.tif`: about 31 k × 32 k px per channel, 29 channels uint8, about 3.5 GB compressed.

- Tile size (512), LZW compression, ×4 pyramid, channel names and physical size are the same as the source.
- "synthetic" appears in the filename and the OME description.
- Written streaming, so memory stays bounded.
- It lives outside the repo and outside `~/fusion_data` (which is never written to).

It is used together with the original `cropped_region` (real tissue content).

**Limits:** repeated content means compression ratio and cell statistics are not representative. The mosaic tests IO, boundedness, identity and invariance, not biology.

#### Execution record: mosaic generated (2026-09-29)

- **Script:** `scripts/make_synthetic_mosaic.py` (`make` / `verify [--full]`; `--crop H W C --crop-origin Y X` for smoke runs). Not imported by the product; the source is only read.
- **Output:** `~/fusionflux/synthetic/synthetic_2x2_mirror.ome.tif`, 3.72 GB (source 0.93 GB), with `.done.json`, `.verify.json` and the logs next to it.
  - 29 × 30874 × 32430 uint8, CYX, BigTIFF, pyramid as SubIFDs: 30874 × 32430 → 7718 × 8107 → 1929 × 2026.
  - Same as the source: 512² tiles, LZW, no predictor, 3 levels, channel names and colours, `PhysicalSizeX/Y = 0.5068606698203042 µm`. Written little-endian; the source is big-endian, which makes no difference for uint8.
  - Quadrants: source, flipped in x, flipped in y, flipped in both. Each seam repeats the edge row / column once, so the image is continuous.
  - OME `Name` and `Description` say synthetic.
- **Pyramid rule:** level k is floor(mean) of the level-0 `4^k × 4^k` blocks, with trailing partial blocks dropped.
  - The source's level 1 follows the same rule: 0.99998 of its pixels are equal.
  - The source's level 2 does not: only 0.79 equal. No block mean, nearest-neighbour or cv2 resampling of its level 0 or level 1 reproduces it. The mosaic keeps the explicit rule.
- **Cost:** 243 s with 14 compression threads, peak RSS 2.75 GB. One source channel plane is held at a time; levels 1–2 are accumulated in RAM, about 1.9 GB.
- **Verification, `verify --full`: 21 / 21 pass.**
  - Level 0: every pixel of 29 channels × 4 quadrants is bitwise equal to the mirrored source (1.69 × 10⁹ non-zero source pixels).
  - Levels 1–2: every pixel equals floor(mean) of level 0.
  - Metadata: shapes, tiling, compression, OME names, colours and physical size all match.
  - Product readers: `OMETIFFLoader`, `RawTileProvider` and Step4's `TiffTileReader` (fast `tiff_tiles` path) read a window that straddles both seams bitwise correctly.
  - Reverse injection: a one-row mirror shift, round-instead-of-floor pyramid levels and one wrong seam column all turn the check red.
- **Pitfall found:** the source's borders are background, so a top-left crop, and the full mosaic's seams, lie in zeros. Sampled windows alone are weak evidence there. Smoke runs must pick an origin inside tissue, and acceptance uses `--full`.
- **Disk:** C: had 42 GB free before and 63 GB after. Both readings include Windows page-file fluctuation.

**Routes run one after another:**
1. TIFF route end to end.
2. Keep the comparison set (h5ad, label hashes, reports); delete the bulky intermediates.
3. NGFF route end to end.

Target: the disk peak stays well below the free space, with `df -h /mnt/c` checked before and during each run.

### Larger mosaics (3×3 / 4×4)

Only once an external SSD is available, and only for read-only IO / viewer / cache stress. No full Step2→4 runs.

### Real WSI

When the server is back, one real WSI is run as a **supplementary validation**. It does not block TMA. It reopens a gate only if it finds:

- a scientific mismatch;
- a coordinate mismatch;
- OOM or unbounded behaviour;
- an NGFF reader correctness issue.

---

## 9. Six TMA start gates

1. **Viewer stability:** Step0 ↔ Step1 ↔ Step3 is visually stationary, with no cumulative drift (automatic + real machine).
2. **PixelSource:** consumers read through one contract with `OmeTiffSource`. If A0 adopted NGFF, `NgffSource` implements the same contract and the representative dataset runs through it.
3. **Scientific invariance:** the representative dataset completes PixelSource → Step2 → N3 → LabelStore → Step4 S4-3, meeting §5.4 against the accepted v15 implementation.
4. **Coordinate / identity freeze:** hierarchy, SegmentationRun key, coordinate names and conventions frozen. A TMA core is an ordinary region.
5. **Object layer:** cells.parquet + regions.parquet from a real result, with keys and coordinates agreeing with LabelStore and the viewer.
6. **Resource boundedness:** viewer / Step2 / Step4 runs do not OOM on the dev machine, use bounded queues and caches, report peaks, and name the remaining bottleneck.

---

## 10. After the gates

TMA Foundation → core detection / dearray → manual grid and core correction → core identity + sample / patient mapping → QualityMask (blur, fold, low tissue) → TMA Batch → real HCC TMA.

No further architecture polishing between Gate 6 and TMA Foundation unless a new scientific-correctness problem appears.

---

## 11. Deferred backlog

- **Data / query:** `expression.zarr`; a DuckDB / Polars / DataFusion benchmark; GeoParquet; spatial indexing; a SpatialData adapter.
- **Step5:** a 10 M × 80 edge runtime; streaming PCA; microclusters / FlowSOM; Rust HNSW / Leiden; UMAP redesign; out-of-core Step5.
- **Segmentation:** CPU StarDist + GPU Cellpose; topology-guided reconciliation; adaptive tiling.
- **Rust:** viewer IO; a PixelSource backend; Step2 streaming; a Step4 backend.
- **NEXUS:** object-query tools; Parquet-native evidence queries; edge orchestration.
- **Out of scope throughout:** the unmaintained HQ / HQ2 / CDS methods.

---

## 12. Schedule

| Days | Stage |
|---|---|
| 1–2 | A0 diagnosis + benchmark + drafts (the synthetic mosaic is generated first) |
| 2 | A0.5 engine identity scope |
| 3–4 | A1 zero-drift |
| 5–9 | A2a (+ A2b if adopted) + A2c consumer migrations |
| 10 | A3 |
| 11–12 | A4 |
| 13–14 | A5 |
| 15 | buffer / regression / real-machine acceptance |

If A0 shows weak benefit for a planned change, the change is removed rather than consuming the buffer.

---

## 13. Stop rules

1. If zero-drift is fixed locally, no global camera framework.
2. If NGFF requires rewriting scientific algorithms, stop and redesign the adapter boundary.
3. Anything needed only for a hypothetical 10 M-cell Step5 goes to the backlog.
4. An optimisation that affects none of scientific correctness, TMA compatibility, OOM safety or the six gates cannot extend the schedule.
5. Day 15 is a decision point. If the six gates pass, TMA starts.
6. After two review rounds with no new public-path defect, hardening stops (P0 rule).

---

## 14. Version control

- **Done 2026-09-29:** annotated tag `v15-final` on `f93f195`, pushed to `origin`. The branch `v15-interactive-channel-workspace` is kept unchanged, local and remote.
- **Done 2026-09-29:** `v16` branched from `f93f195` as the integration line and pushed. Its first commit is this plan (v2.1 only; v1 / v2 Lean are not committed). Short feature branches (`v16-a0-diagnosis`, `v16-viewer-zero-drift`, `v16-pixelsource`, `v16-tma-foundation`, `v16-qualitymask`) are opened as needed.
- `origin/main` is **not an ancestor** of `f93f195`: it is an unrelated history of 18 GitHub web-upload commits, from `4d97b96 initial commit` to `ee6f2cd`, dated 2026-05-19, with no common ancestor. It cannot be fast-forwarded. It is left untouched in this block; what to do with it (keep as is, replace, or change the remote default branch) is a separate decision by the user. No force push.

---

## 15. Definition of success

One pixel-source contract, one coordinate contract, one segmentation identity contract, one minimal object table, stable Step0/1/3 camera behaviour, and bounded resources — not every future subsystem implemented.

> Freeze the interfaces and scientific truth now; implement future consumers only when real TMA / Step5 workloads justify them.
