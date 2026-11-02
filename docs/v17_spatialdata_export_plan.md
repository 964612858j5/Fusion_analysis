# v17 Step4: SpatialData export — plan v2 (2026-11-02)

v2: codex plan review round 1 (1 P1, 6 P2, 1 P3) — §7; round 2 READY (P3
memory wording applied).

User (TG 2026-11-02): after the h5ad / CSV work, "接着就做spatialdata输出";
direction **B** (a standard SpatialData, not Step5's private dataset
folder); writing done by **Step5's environment** (option b), not by
installing spatialdata into Block01's.

## 0. Facts

* **Block01's environment cannot take spatialdata 0.5.0**: a dry run with
  every installed version pinned fails — spatialdata 0.5.0 → xarray-dataclass
  3.0.0 → NumPy ≥ 2; `fusion_mesmer` must keep NumPy 1.26.4 (TensorFlow /
  DeepCell Mesmer). Nothing was installed.
* **Step5's environment**: Step5 v10 runs in `fusion_test2`
  (`step5_v10/repo/README.md:22`; spatialdata 0.5.0, **anndata 0.10.9**
  (Block01 writes with 0.11.4), zarr 2.18.3, dask 2024.11.2 — imports
  cleanly). `step5_test` has
  spatialdata 0.5.0 too but cannot import it (`pkg_resources` missing in
  `xarray_schema`).
* **Step5 today** (read-only survey of `step5_v10/repo`): it opens only its
  own dataset folder (`step5_dataset.json` + `data.zarr` with Points
  `cells` and Table `root`; `application/dataset/store.py:116-125`), built
  from Block01 h5ads by its importer (`application/dataset/import_h5ad.py`,
  merged across samples, `tools/mutation.py:2454-2545`). Its only generic
  SpatialData reader (`application/spatial_statistics/spatialdata_entry.py`)
  is test-only. No Step5 code uses images, labels or shapes. **So Step5
  needs its own importer for this export; that is Step5's project, not this
  plan.** What the importer will need is written into the table below
  (cell id, sample, centroids in full-resolution pixels, `obsm['spatial']`,
  `uns` source paths).

## 1. What is written

One SpatialData zarr per Step4 output (one region of one segment run),
next to the h5ad: `<base>.spatialdata.zarr`, through
`<base>.spatialdata.zarr.partial` renamed when complete (as the h5ad).

| element | content |
|---|---|
| `labels/cell_labels` | the run's cell mask of the region (the Step2 LabelStore Step4 quantified, `job.label_path`), uint32, multiscale (factors 2, 4, 8, 16), chunked |
| `labels/nucleus_labels` | the nucleus mask, when the run has nuclei |
| `tables/table` | the h5ad's AnnData as written by Step4 (X, layers, obs, var, uns, obsm) plus `obs["region"]` = Categorical `"cell_labels"`, instance key = the label value (`cell_id`; `nucleus_id` for a nuclei-only run), `obsm["spatial"]` = (centroid_x, centroid_y) in full-resolution slide pixels, `uns["spatialdata_attrs"]` = {region `cell_labels`, region_key `region`, instance_key `cell_id`} |
| `shapes/regions` | the Regions snapshot the run applied: TMA cores / regions as polygons with their position key / sample, and measurement exclusions as polygons with their uid (absent when the run has no Regions) |
| images | **not copied** (a slide is tens of GB): `uns["source_images"]` records the slide path, its identity and, per channel, Step0's corrected product path (what Step4 measured). Optional later: link or copy the corrected / fused pyramid |

Coordinate systems:
* `global` — full-resolution slide pixels. Labels carry a Translation by
  the region's bbox origin (y0, x0); shapes and `obsm["spatial"]` are
  already in slide pixels.
* `micrometers` — `global` scaled by the slide's pixel size, when the slide
  records one (as Step4's Regions areas use, `quant_apply._pixel_size`).

`uns` also keeps Block01's provenance (`provenance_json`) and adds
`x_statistic` (lowercase, as Step5's discovery reads it), `ome_tiff`,
`mask_path` — the keys Step5's current h5ad importer looks for.

## 2. How Block01 runs it

* NEW `scripts/export_spatialdata.py` (Block01 repository, runs under the
  SpatialData Python, `-I`): reads a small job JSON (h5ad path, label
  paths + bbox, Regions snapshot path, pixel size, output path), builds
  the SpatialData with spatialdata's own models (`TableModel`,
  `Labels2DModel`, `ShapesModel`, `Translation` / `Scale`), writes it with
  `write`, then validates by reading it back (`read_zarr`: element names,
  table rows = h5ad rows, label shapes). Exit code + one JSON line.
* NEW `utils/spatialdata_python.py`: which interpreter. Order:
  `BLOCK01_SPATIALDATA_PYTHON`; else the value saved in Block01's settings
  (chosen once in Step4); a candidate is accepted only when
  `<python> -I -c "import spatialdata; print(spatialdata.__version__)"`
  succeeds (cached per run of the app). Nothing is guessed from env names.
* Step4 page: a third output tick **SpatialData** beside h5ad / CSV. Ticking
  it with no usable interpreter opens a file chooser for that Python (the
  check above runs on it; a failure says why and the tick goes back off).
  The file list under the outputs shows `<base>.spatialdata.zarr`.
* `workers/feature_extract_worker.py`: after the h5ad is complete and
  before publishing, when asked: write the job JSON, run the script with
  the Stop button able to terminate it, take its result; the zarr is
  published with the other outputs (rename order unchanged, the
  `.partial` and the job JSON removed on failure / Stop). Provenance
  records the interpreter path and spatialdata version.

## 3. Acceptance

* Unit (synthetic projects; skipped when no SpatialData Python is found):
  plain region, TMA with cores + exclusions, nuclei run, nuclei-only run,
  zero cells: `read_zarr` back in the SpatialData Python — elements as §1,
  table rows / ids / X equal to the h5ad, `obs["region"]` and
  `spatialdata_attrs` valid, label pixels equal to the LabelStore (all
  scales' level 0), transforms as §1; Stop during the export leaves no
  `.partial`; a broken interpreter path → clear refusal, the h5ad / CSV of
  that run still not half-published.
* `tests/test_step4_page.py`: the tick, the chooser path, the file list.
* Real-machine (user): Step4 with SpatialData ticked on a project the user
  chooses (the user's test projects are read only for me: I test on
  synthetic projects); the zarr opened in `fusion_test2`
  (`spatialdata.read_zarr`, napari if wanted).

## 4. Scope (closed whitelist)

* NEW `scripts/export_spatialdata.py`, NEW `utils/spatialdata_python.py`
* `workers/feature_extract_worker.py` (run the export, publish, provenance)
* `ui/step4_page.py` (the tick, the chooser, the file list)
* `UI_SURFACE_RULES.md`, `docs/user_guide.md`, `docs/用户指南.md` (the new
  output)
* NEW `tests/test_spatialdata_export.py`; `tests/test_step4_page.py`
* this plan

Not in scope: Step5 (its importer is Step5's project), images in the
zarr, Block01's environment (no install), the installer.

## 5. Questions for the user

1. Contents as §1: cell (and nucleus) masks + the cell table + Regions
   polygons, **no image copy** (paths recorded)? Copying the slide or the
   corrected images would add tens of GB per export.
2. One SpatialData per region (as the h5ad), Step5 merging samples as it
   does now — agreed?
3. The SpatialData Python chosen once in Step4 (file chooser, remembered),
   or only through the environment variable / launcher?

User (TG 2026-11-02): 1, 2 agreed; 3 agreed (chosen once in Step4,
remembered), noting that a file chooser only suits this server — a
large cross-platform deployment needs the SpatialData environment
integrated.

## 6. Follow-up: SpatialData environment in the installer (separate plan)

`scripts/install_block01.sh` creates a second locked environment next to
`fusion_mesmer-<lock>`: `block01_spatialdata-<lock>` (spatialdata 0.5.0
with its own NumPy 2 stack, nothing else), and records its Python in
`.block01/install.env`; `utils/spatialdata_python.py` reads that record
before asking, so an installed Block01 never shows the chooser. One
environment is not possible while TensorFlow (StarDist, DeepCell Mesmer)
keeps Block01 on NumPy 1.26; it becomes possible if those move off
TensorFlow. Windows: the same, through the native installer (native GUI
item). To be planned after this export works.

## 7. Codex round 1 — answers (these override §1–§3 where they differ)

* **P1 `x_statistic`**: SpatialData refuses `uns` keys equal up to case.
  The exported table RENAMES `X_statistic` to `x_statistic` (value kept);
  the h5ad is unchanged. `mask_path` is NOT added: Step5's montage reads a
  TIFF there (`tools/plotting.py:734`) and a LabelStore path would break
  it; the label sources go to `uns["source_labels"]` instead. `ome_tiff` =
  the slide path. Step5's montage on this export stays a documented
  downstream limitation.
* **P2 primary element per compartment**: a cell run → table annotates
  `labels/cell_labels`, instance_key `cell_id`; a nuclei-only run →
  `labels/nucleus_labels`, instance_key `nucleus_id` (the column Step4
  writes). A cell run with nuclei also writes `nucleus_labels`, not
  annotated (the table is per cell).
* **P2 zero objects**: SpatialData checks the region against observed
  values; a zero-row table is written WITHOUT annotation metadata (a plain
  table), labels still written; round trip tested.
* **P2 centroids without Morphology**: Step4's `centroid_x/_y` when present;
  otherwise the exporter computes them from the primary mask chunk by chunk
  (per label: sum of pixel y / x and count, + the bbox origin — Step4's
  definition), so `obsm["spatial"]` always exists; Morphology-off case in
  acceptance (and, Morphology on, the computed centroids equal Step4's in
  a test).
* **P2 shapes**: written only when there is at least one core or
  exclusion; cores from the snapshot's `display_cores[].polygons` (each
  piece its own ring) as one (Multi)Polygon per core, never the bridged
  analysis ring (`core/regions/geometry.py:7`); an older snapshot without
  `display_cores` → `split_keyhole` of the core outline (as
  `core/regions/display.py:85-98`); exclusions likewise; invalid rings
  repaired with shapely `make_valid` and counted in the provenance.
  Columns: `kind` (core / exclusion), `uid`, `position_key`, `name`, `run`.
* **P2 memory / size**: labels read lazily (`dask.array.from_zarr` on the
  LabelStore, its own chunks) and written by spatialdata in chunks; the
  pyramid's reduced levels are computed by multiscale-spatial-image one
  after another, each kept in memory once computed (round 2 advisory):
  the four reduced levels of a 46,800 × 26,880 uint32 mask hold ≈ 1.67 GB
  per mask before transient buffers (level 1 alone 1.26 GB); uncompressed
  totals 6.7 GB per mask before compression. The
  table is loaded whole in the export process (its size: n × channels ×
  4 B per matrix, X + layers); streaming it is the 20 M plan's job. The
  source AnnData is released before verification, and verification reads
  labels / shapes with `read_zarr(selection=...)` and the table's obs and
  shapes only, not a second full table. Acceptance measures peak RSS,
  time and output size on a synthetic 2 M-cell whole-slide-sized case and
  reports them; no bound is promised.
* **P2 AnnData 0.10.9**: acceptance reads the actual Step4 h5ad in
  `fusion_test2` and compares X, every layer (NaN included), the QC
  layer, obs (categoricals, text), var, uns and distribution `obsm` with
  Block01's read.
* **P3 pyramid**: `scale_factors=[2, 2, 2, 2]` (successive) for absolute
  2/4/8/16, cut short while the smaller side stays ≥ 256 px. **Lifecycle**:
  the worker runs the export on the completed `.partial` h5ad before any
  rename; on Stop it terminates the child AND waits for it, then removes
  the zarr `.partial` with a directory-aware removal (`shutil.rmtree`);
  publication renames the zarr with the other outputs.

## 8. Implementation notes (2026-11-02)

* As §1–§2 / §7. Files: `scripts/export_spatialdata.py`,
  `utils/spatialdata_python.py` (saved choice in `<repo>/.block01/settings.json`,
  ignored by git with `.block01/`), `workers/feature_extract_worker.py`
  (`_export_spatialdata`, `_spatialdata_shapes`, `_remove`,
  `SpatialDataExportError` — its reason reaches the user like a refusal),
  `ui/step4_page.py` (tick, chooser with a wait cursor while the first
  check imports spatialdata, draft, file lists), docs, tests.
* **Pyramid memory (measured, deviation in method, not in pixels)**:
  spatialdata's labels pyramid (multiscale-spatial-image, DASK_IMAGE_NEAREST)
  computes each reduced level through `dask_image.ndinterp.affine_transform
  (...).compute()`: **11.6 GB** for a 46,800 × 26,880 mask, independent of
  dask threads. Its result is exactly "every 2nd pixel, size rounded down"
  (checked on odd sizes), so the script builds the levels lazily by that
  slicing (`strided_multiscale`, swapped in for `models.to_multiscale` only
  during `Labels2DModel.parse`; spatialdata's own transformation handling
  is kept); a test proves every level's pixels and transformations equal
  the library's. Dask threads capped at min(8, cores): with one per core
  (96) writing held 6.6 GB, with 8 1.7 GB at the same speed.
* Whole-slide synthetic export (46,800 × 26,880 mask, 2,096,640 cells, 50
  channels, X + 3 layers; `s4exp/sd_bench_prep.py`), `/usr/bin/time`:

  | | peak RSS | time | output |
  |---|---|---|---|
  | library pyramid, default threads | 14.3 GB | 96 s | 1.6 GB |
  | **as written** | **4.3 GB** | **17 s** | 1.6 GB |

  Of the 4.3 GB, the table (read whole, written by anndata) is ~3.8 GB;
  streaming it belongs to the 20 M plan.
* Tests: `tests/test_spatialdata_export.py` (9: TMA cell run with nuclei,
  exclusion and distribution `obsm` compared with the h5ad read by
  AnnData 0.10.9 in `fusion_test2`; plain run without shapes and
  Morphology-off centroids equal to Step4's; nuclei-only; zero objects;
  Stop during the export; unusable Python refused before any output;
  pyramid factors; micrometers; lazy pyramid = library pyramid), 2 page
  tests; Step4 suites 107 + these passed.
* Codex code review round 1 (3 P2 + 1 P3), all fixed: (1) overlapping
  pieces of one core (Regions "Group") — each piece repaired on its own,
  then their union (area kept; test); (2) the interpreter check ran on the
  GUI thread — now a background `QThread`, Run disabled and the outputs line
  says "checking…" until the answer, the chooser opens only after a failed
  lookup (tests: check off the main thread, Run waits); (3)
  `uns["source_images"]` as §1: slide path + file identity (size, mtime_ns)
  and per channel the measured source (slide index, kind, path, array,
  recorded identity as JSON); (4) the read-back now checks label names and
  level-0 shapes against the sources and that `shapes/regions` exists iff
  any shape was given. The §7 fallback for snapshots without
  `display_cores` is not needed: `load_regions` already refuses them.
* Codex code review round 2 (2 P2), fixed: (1) the lookup outlived window
  close — the check is now a `QProcess` owned by the page (no thread),
  candidates tried one after another, a per-process 180 s limit, killed by
  `stop_background_jobs` (window close / dataset switch) and with the page;
  callbacks ignore a deleted page (tests with real stand-in scripts:
  usable / unusable / slow / closed during the check); `utils.spatialdata_python`
  gains `cached` / `remember` / `answer` / `candidates` for it, `check` stays
  for the worker; (2) `uns["source_images"]["channels"]` keyed by position
  with the name as a value (a name may hold "/"; AnnData 0.10.9 round trip
  test with `CD3/PD1`).
