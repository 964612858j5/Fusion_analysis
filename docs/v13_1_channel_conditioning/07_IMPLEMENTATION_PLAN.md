# 07 — Implementation Plan (v13.1)

Phased plan. Each phase is independently reviewable. Do not skip ahead; later
phases assume earlier ones landed and were validated.

## Phase 0 — Safety and docs (this phase)

- create backup of v13 (`block01_v13_backup`)
- create branch `v13.1-channel-conditioning`
- write the `docs/v13_1_channel_conditioning/*` design docs

Exit criteria: backup exists, branch exists, docs committed locally.

## Phase 1 — Core remap operator

- add `core/channel_remap.py` (backend-agnostic, NumPy first, no Qt / no I/O)
- add `utils/channel_remap_config.py` (load / save / validate config)
- implement **Min / Max / Brightness / Contrast / Gamma / Auto**
- follow the normative transform order in `03_CHANNEL_REMAP_SPEC.md`
- unit tests for the transform (identity params, clipping, gamma, order) and for
  Auto (percentile correctness, valid-pixel masking)

Exit criteria: transform + Auto unit tests pass; config round-trips through
save/load/validate.

## Phase 2 — Step3 visual prototype

- refactor Step3 layout into the three-column channel-first UI
  (`04_UI_REDESIGN_SPEC.md`)
- remove raw/corrected clutter from main UI (move to Advanced / Developer)
- **no horizontal scrolling** in the channel list
- add active channel inspector (histogram + Min/Max/Brightness/Contrast/Gamma/
  Auto/Reset/Save)
- add histogram panel
- add remap preview in center viewer (live)
- add save / load remap config

Exit criteria: operator can load a patch, condition channels, preview remap, and
save a `segmentation_preprocess_config`.

## Phase 3 — Reusable widgets

Extract Step3 viewer logic into reusable, host-agnostic widgets:

- `ui/widgets/channel_layer_list.py`
- `ui/widgets/channel_histogram_panel.py`
- `ui/widgets/channel_viewer_canvas.py`
- `ui/widgets/channel_workbench.py`

Exit criteria: Step3 is recomposed from the widgets; no behavior regression.

## Phase 4 — Step1.5 Channel Conditioning

- replace the old Step1.5 background correction page with a Channel Conditioning
  page (`ui/channel_conditioning_page.py`)
- reuse the Phase 3 widgets
- save the channel remap config from Step1.5

Exit criteria: Step1.5 authors and saves a remap config using the shared widgets.

## Phase 5 — Step2 integration

- make HQ2 / CDS / lean_carve consume the saved channel remap config
- ensure Step2 does **not** recompute Auto per tile (Auto is frozen in config)
- ensure Step2 is **headless and reproducible** (no GUI-state dependency)

Exit criteria: a whole-run segmentation reproduces from image path + saved config
with no GUI; identical config gives identical mask.

## Phase 6 — h5ad guard

- ensure feature extraction defaults to raw / biologically corrected intensity
- do **not** use remapped intensity as default h5ad expression
- save provenance to `adata.uns["segmentation_preprocess"]`
  (see `05_DATA_PROVENANCE_AND_H5AD.md`)

Exit criteria: `adata.X` is verified raw/bio-corrected; provenance block present
and complete; optional remapped layer (if written) is non-default.

## Phase 7 — Benchmark

- run baseline v13 vs experimental v13.1 (`06_BENCHMARK_PLAN.md`)
- region-stratified evaluation (AF-heavy, clean, necrotic, RBC-rich, tumor-rich,
  immune-rich, edge/low-tissue)
- write the benchmark report with a verdict against the interpretation rules

Exit criteria: benchmark report delivered with a clear adopt / keep-spatial-op /
either-way verdict, including an explicit call on AF-heavy regions.

## Dependency notes

- Phases 1 and 2 can proceed in parallel after Phase 0; Phase 2 may stub the
  operator until Phase 1 lands.
- Phases 5–7 must not start until 1–4 are stable; benchmarking an unstable
  operator wastes the comparison.
