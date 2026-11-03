# Block01 architecture (short)

What lives where, and the one rule for each layer. Details are in the plans
under `docs/` (index: `docs/README.md`); the binding UI spec is
`UI_SURFACE_RULES.md`; the working rules are `AGENTS.md`.

## The steps

| Step | Page | What it makes |
|---|---|---|
| 0 Setup & Preprocessing | `ui/step0/step0_page.py` | region (ROI), patches, per-channel background correction and Intensity; Save → the workspace's **correct run**, rewritten in place (only changed channels, staged then swapped: `core/product_swap.py`; 2026-11-02) |
| 0.5 Regions | `ui/regions/` | TMA cores and exclusion areas, saved per slide (`core/regions/`) |
| 1 Fusion | `ui/step1_*`, `ui/step1_presegmentation/` | fusion settings, patch pre-segmentation (**preseg run**), Generate → a **fuse run** |
| 2 Segmentation & Merge | `ui/step2_page.py`, `workers/segment_merge_worker.py` | whole-region segmentation in tiles → a **segment run** (label store) |
| 3 QC Viewer | `ui/step3_*` | no product: several results drawn over the slide, `Use` picks Step4's |
| 4 Feature Extraction | `ui/step4_page.py`, `workers/feature_extract_worker.py`, `core/quant_engine.py` | per-cell table → a **quant run** (h5ad) |

`ui/main_window.py` owns the pages, the hand-overs between them and the
project state.

## Layers

* **ui/** — interaction and presentation only. Only `main.py` (the
  application bootstrap) imports `ui/`; `core/`, `workers/`, `viewer/`,
  `utils/`, `sources/` and `seg_runner/` never do (checked by
  `tests/test_core_api_check.py`).
* **workers/** — complete job lifetimes (start, progress, cancel, the last
  write). A job's result is published by its `.done` marker, written last.
* **core/** — scientific computation and publication rules (correction,
  fusion, regions, quantification, run identity, provenance). No widgets.
* **viewer/**, **sources/** — the Python tile pipeline and the slide /
  corrected-product readers (also the fallback when the Rust core is
  refused).
* **utils/** — run store (`run_store.py`), trash, logging, guards, shared
  helpers.
* **seg_runner/** — each segmentation engine (Cellpose, StarDist, Mesmer) in
  its own subprocess, one protocol, self-test (`python -m seg_runner.selftest`).
* **native/fusion_viewer_core/** — the Rust core (PyO3, abi3 wheel): tile
  reading and decoding, caches, GPU upload and drawing for the viewers, the
  Rust fusion job. Small single-purpose modules; `src/py/` is a thin binding
  layer. `ui/step1_core_adapter.py` is the one compatibility check
  (`CORE_API`, minimum version) every user of the core asks.
* **Three viewer paths** — the Rust core (default), the Python GPU path (`ui/step1_gpu_layer.py`, `ui/step1_gpu_binding.py`: the fallback for what the core refuses, and the montage's picture) and the CPU composer; the display types they share are in `ui/step1_display_types.py`; removing the Python GPU path waits for a Rust montage.

## Data on disk: runs (block RM)

```
<project>/rois/<roi>/runs/correct_* → preseg_* / fuse_* → segment_* → quant_*
```

A run = the folder one button made, its `params.json` (what it used),
`inputs.json` (its one pixel upstream) and `.done`. Its scientific products
and records are never rewritten (display helpers such as mask pyramids may
be added later); deleting one moves it and everything made from it to
`<project>/.trash/` (30 days). Draft state is `session.json`; the last saved
settings are `settings/`. See `docs/v16_run_model_application.md`.

## Install and run

`bash scripts/install_block01.sh`, then `scripts/block01`
(`docs/v17_install_plan.md`). Tests: `tests/` (synthetic data only, never a
real project); CI: `.github/workflows/` (CPU lane = `tests/ci_cpu_modules.txt`;
GL / GPU / engine tests stay local).
