# Hybrid: Cellpose whole cells + StarDist nuclei — plan v1.1, 2026-11-10 (codex round 1, §7)

Requested by the user (TG 2026-11-10, after accepting `cellpose_wholecell_nuclei`,
0f7575d): Cellpose's nuclei come out larger than the DAPI signal (the extra
area has hardly any DAPI); StarDist's nuclei follow the DAPI signal closely.
A hybrid method: cells from Cellpose (as `cellpose_wholecell_fusion`), nuclei
from StarDist (as `stardist_nuclei_dapi`), made one by the SAME keep / drop /
share rules (`core/nuclei_pairing.py combine_cells_and_nuclei`, plan
`docs/v17_cellpose_cell_nucleus_plan.md` v1.3). Plan only: codex review, then
the user's approval, then implementation.

## 1. Today (read-only, HEAD 0f7575d)

* One engine per method, from the method name's prefix:
  `seg_runner/engines.py:38 METHOD_ENGINE`, `core/preseg_run.py:199 ps_engine`.
  Everything keyed on it: the runner's `--engine` (one engine object per
  process, `runner.py:138-173`), the hello's single identity / provenance /
  device, Step1's per-engine grouping (`run_job.py:31, 110-127`, one process
  at a time by design), the record's `engine_identity` (`preseg_run.py:185`),
  `reusable()` (219-243), the contract (`preseg_contract.py:65-75, 136-138`),
  Step2's single `EngineProcess` and identity check
  (`segment_merge_worker.py:2608-2667`), `seg_engine` in the meta and
  provenance (`core/provenance.py:494, 535-540`), Step2's
  `_confirm_engine_identity` (`step2_page.py:3721-3756`).
* One input kind per method (`core/preseg_input.py:23 INPUT_KIND`); one runner
  task = one input array (`protocol.py:14`, `runner.py:110-135`).
* Both engines run on PyTorch since 8b3c2ae: `CellposeEngine` (cpsam) and
  `StardistEngine` (the torch port behind StarDist's own `predict_instances`;
  TensorFlow's GPU hidden before its Keras model is built,
  `engines.py:219-237, 301`). `engines.run` has a two-pass branch for
  `CELL_AND_NUCLEUS_METHODS` that today calls ONE engine twice (456-460).
* StarDist input: the 2-D `nucleus` kind = f1 (`preseg_input.py:125-137`),
  normalised by StarDist itself (1–99.8 percentiles over the input,
  `engines.py:348-352`); no `n_tiles`; Step2's page caps a StarDist read area
  at 60 MP (`step2_page.py:1987-2089`, `_is_stardist`, the grid check at Run).
  `fusion_fusion_nucleus` = `[f0, f0, f1]`: its last channel IS the `nucleus`
  input.
* Two engine processes on one GPU would mean two CUDA contexts and two models
  resident with nothing coordinating them. (An 11 GB-card OOM was seen with a
  Cellpose model held next to a Cellpose subprocess,
  `docs/stardist_torch_plan.md:642-646`; its cause was left unproven there.)

## 2. Proposed design: one hybrid ENGINE, not two processes

* A third engine kind **`hybrid`** in the engine process: `HybridEngine` holds a
  `CellposeEngine` and a `StardistEngine` in the SAME process (one torch device,
  one CUDA context; the StarDist part keeps its TF-GPU hiding, done before its
  Keras model is built — Cellpose imports no TensorFlow). Its identity:
  `BEHAVIOR_VERSION["hybrid"] = 1`, `MODEL_ID["hybrid"] =
  "cellpose_cpsam+stardist_2D_versatile_fluo"`; its provenance = both parts'
  (lib versions, manifest models of both engines, StarDist's converted-weights
  `model_artifact`); `device`: both parts are checked to sit on the same torch
  device at start (else the engine refuses to start), and it is reported in
  ONE form — StarDist's `gpu` / `mps` / `cpu` (Cellpose's `cuda:0` normalised
  to `gpu`). `cpu_only` reaches both parts through `torch_device()`. Any change
  that alters either part's output bumps `BEHAVIOR_VERSION["hybrid"]` (as
  for the single engines).
* New method **`hybrid_cellpose_stardist`** ("Cellpose whole-cell + StarDist
  nuclei (Fusion + DAPI)"), listed after `cellpose_wholecell_nuclei`. Its name
  prefix makes `METHOD_ENGINE` / `ps_engine` = `hybrid` with no override, so
  every one-engine-per-method path (Step1 grouping, records, reuse, contract,
  Step2 process and identity check, meta, provenance) works unchanged.
* Input: `fusion_fusion_nucleus` (one task, one array). `engines.run`: cells =
  the Cellpose part on `[f0, f0, f1]` (exactly `cellpose_wholecell_fusion`);
  nuclei = the StarDist part on the last channel (exactly
  `stardist_nuclei_dapi`'s input and normalisation). The hybrid branch comes
  BEFORE the existing `CELL_AND_NUCLEUS_METHODS` two-pass branch (which feeds
  the Cellpose nucleus-pass parameters). Raw masks back; the
  method joins `CELL_AND_NUCLEUS_METHODS`, so Step1 (`combine_window` →
  Regions → ownership), Step2 (`combine_cells_and_nuclei` per tile,
  `require_nucleus`, Recovery refused, the label store's `pairing`) and the
  Results line apply as for `cellpose_wholecell_nuclei`, with no change.
* Parameters: the cell part's Cellpose set (diameter, flow, cellprob listable
  in Step1, min size — as whole-cell) + StarDist's `prob_thresh`,
  `nms_thresh` (auto allowed) as **single values, not grid-searched** (as the
  user ruled for the nucleus pass of `cellpose_wholecell_nuclei`), and the
  fixed `model_name`. The keys do not clash (each engine part reads only its
  own).
* Step2 page: the Cellpose rows and the StarDist rows both shown; the 60 MP
  StarDist read-area cap applies (`_is_stardist` covers the hybrid);
  `get_seg_config` already carries `prob_thresh` / `nms_thresh`.
* Flat keys (codex P2): `prob_thresh`, `nms_thresh`, `model_name` join
  `normalize_segmentation_config`'s flat-key list (today a flat file's values
  would be replaced by the registry defaults; this also fixes it for the two
  StarDist methods). A loaded hybrid file asking for another StarDist model is
  told so before the run: `preseg_contract.ignored_settings` checks
  `model_name` for the hybrid as for `stardist_*` (codex P2).
* Registered where methods are listed: `segmentation_config` (entry), `segmentation_param_schema` (`UI_METHODS`, `METHOD_PARAMS`),
  `engines` (`METHOD_OUTPUTS`, `CELL_AND_NUCLEUS_METHODS`, the engine kind),
  `runner` (`--engine hybrid`), `preseg_input.INPUT_KIND`, Step1
  `ENGINE_ORDER`, `utils/tile_strategy.py`, `core/step3_masks.py`.
* Not changed: the protocol, the combine rules, Step4, the Viewer, the other
  methods and engines; the installer self-test stays per engine (the hybrid
  only composes the two checked engines) — Q2.

## 3. Closed whitelist

`seg_runner/engines.py` (`HybridEngine`, identity, `run` branch, tables),
`core/preseg_contract.py` (`ignored_settings` only), the flat-key list in
`utils/segmentation_config.py`,
`seg_runner/runner.py` (engine choice, provenance of a composite engine),
`utils/segmentation_config.py`, `utils/segmentation_param_schema.py`,
`core/preseg_input.py`, `ui/step1_presegmentation/run_job.py`
(`ENGINE_ORDER`), `ui/step2_page.py` (rows, StarDist cap), `utils/tile_strategy.py`,
`core/step3_masks.py`; tests: new `tests/test_hybrid_cellpose_stardist.py`,
method tables (`test_seg_runner_engines.py`, `test_preseg_run.py`,
`test_step1_method_plan.py`, `test_preseg_contract.py` COMBOS); docs: both user
guides (method list).

## 4. Tests and acceptance

* Engine (real, the Mac's device AND `cpu_only`): the hybrid's cell mask
  equals `cellpose_wholecell_fusion`'s and its nucleus mask equals
  `stardist_nuclei_dapi`'s on the same input / params, with default and
  non-default / auto thresholds (wiring); several tasks in one process give
  the same masks (alternating passes leave no state behind); one device in
  one form; identity and provenance carry both parts.
* Parameters: a flat and a nested hybrid file load the same values; a file
  with another `model_name` is warned about (`ignored_settings`).
* Step2 identity: a hybrid Step1 result with another hybrid behaviour version
  is asked about; Cancel runs nothing; the saved meta / provenance carry the
  composite identity.
* Step1 (engine process): a run of the method — records `engine_identity` =
  the hybrid's, device, the combine events; reuse refused on another engine
  identity.
* Step2 (stand-in engine as in `test_cellpose_cell_nucleus.py`): every cell has
  a nucleus; the StarDist cap applies to the hybrid; the page round-trips the
  StarDist values; the contract hand-over (Use → Generate → Step2) runs
  unchanged.
* Real machine (Mac): Step1 with the method on the test project → the nuclei
  follow DAPI (visually as `stardist_nuclei_dapi`), cells as whole-cell;
  Use → Generate → Step2 → Step3 → Step4.

## 5. Risks

* Memory: both models resident in one process; the weights' sizes (cpsam
  ~1 GB, StarDist's net a few MB) are not peak inference memory, and one
  process plus the 60 MP cap do not prove memory safety — peak memory is
  measured on the Mac, and on CUDA if the user wants a CUDA run (Q3).
* Normalisation differs per part (Cellpose's own, StarDist's percentiles over
  the input) — as in the two single methods; results can depend on the tile
  for the StarDist part, as today.
* Time: one Cellpose pass + one StarDist pass per tile (StarDist is much
  faster than a second Cellpose pass).

## 6. Questions for the user

1. StarDist's `prob_thresh` / `nms_thresh` single values (recommended, as the
   nucleus pass of `cellpose_wholecell_nuclei`) — or listable in Step1?
2. The installer self-test: keep per engine (recommended; the hybrid is two
   checked engines in one process; its composition is covered by the tests
   above) — or add a hybrid check?
3. A CUDA run (the Ubuntu window) as part of acceptance — both parts on one
   NVIDIA GPU, peak memory on a Step2 tile — or the Mac only?

## 7. Codex plan review round 1 (astra low) — FAIL (P2 ×2), taken in v1.1

* P2: flat `prob_thresh` / `nms_thresh` / `model_name` were not normalised →
  added to the flat-key list (§2), tested flat and nested (§4).
* P2: `ignored_settings` checked `model_name` for `stardist_*` only → the
  hybrid too; `core/preseg_contract.py` whitelisted for that (§3).
* Taken: one device in one form, both parts checked to share it; the hybrid
  branch before the generic two-pass one; the behaviour-version bump rule;
  §1/§5 wording (OOM cause unproven; weight sizes are not peak memory); §4
  CPU-only, repeated tasks, thresholds, fixed-model warning, identity
  confirm / cancel, saved provenance; Q3 (CUDA).
* The single `hybrid` engine design: judged sound (prefix routing; records,
  reuse, contract, Step2 confirmation, meta and provenance accept a composite
  identity unchanged).

## 8. Codex plan review round 2 (astra low) — PASS

Both round-1 P2s resolved; no new public-path gap; hardening stops (AGENTS rule 7). Waits for the user's answers to §6 Q1–Q3 and approval.

## 9. User rulings (TG 2026-11-10)

1. StarDist thresholds: single values, not grid-searched.
2. No hybrid installer self-test (per engine stays).
3. CUDA acceptance: run on the Ubuntu window.

## 10. Implementation (2026-11-10, on 0f7575d)

* `utils/segmentation_config.py`: `HYBRID_CELLPOSE_STARDIST`, its entry
  (Cellpose whole-cell defaults + `model_name` / `prob_thresh` /
  `nms_thresh`); those three in the flat-key list.
* `utils/segmentation_param_schema.py`: in `UI_METHODS` (10 methods) after
  `cellpose_wholecell_nuclei`; `_hybrid()` = whole-cell specs + StarDist
  thresholds (single values, auto allowed) + the fixed model.
* `seg_runner/engines.py`: `METHOD_OUTPUTS`, `CELL_AND_NUCLEUS_METHODS`,
  `HYBRID_METHODS`, `ENGINE_PARTS`, `BEHAVIOR_VERSION["hybrid"] = 1`,
  `MODEL_ID["hybrid"]`; `HybridEngine` (StarDist built first, then Cellpose;
  `_device_form` → one device or refuse to start); `load("hybrid")`; the
  `run` branch before the generic two-pass one.
* `seg_runner/runner.py`: `--engine hybrid`; provenance takes the manifest
  models of `ENGINE_PARTS`.
* `core/preseg_input.py` (`fusion_fusion_nucleus`),
  `ui/step1_presegmentation/run_job.py` (`ENGINE_ORDER`),
  `core/preseg_contract.py` (`ignored_settings` checks the hybrid's
  `model_name`), `utils/tile_strategy.py`, `core/step3_masks.py`.
* `ui/step2_page.py`: the hybrid shows the Cellpose and StarDist rows (not the
  Cellpose nucleus-pass rows); `_is_stardist` covers it (60 MP cap, no Auto
  re-size); `get_seg_config` writes `model_name` / `prob_thresh` /
  `nms_thresh` at the top level too (with the flat keys normalised, a loaded
  file's top-level values would otherwise win over the page's — they were
  already mirrored there by normalisation, so no other method changes).
* Docs: both user guides' Appendix A list the hybrid AND
  `cellpose_wholecell_nuclei` (missing since 0f7575d).
* Tests: new `tests/test_hybrid_cellpose_stardist.py` (12: wiring with default
  and non-default thresholds, repeats, device form, refusal on two devices,
  identity / provenance, `cpu_only` through the engine process, flat / nested
  files, model warning, page rows / cap / own values, identity question and
  Cancel, Step2 on a stand-in engine); method tables extended
  (`test_seg_runner_engines.py`, `test_preseg_run.py`,
  `test_step1_method_plan.py` = 10, `test_preseg_contract.py` COMBOS / IDENT);
  `test_cellpose_cell_nucleus._step2` takes the method.
* Mac: the 12 new tests pass (the Mac's GPU, `mps`; `cpu_only` → `cpu`).

### 10.1 Codex implementation review (astra low) — PASS

No P1/P2. Advisory taken: `test_preseg_contract.py` exempts the hybrid from the "no Cellpose parameters" check (its cells are Cellpose), keeping the parameter-equality checks.

Regression (Mac, 2 tests at a time): 22 modules + the two new files — no new failure against HEAD after two test fixes (`test_engine_identity.py`: a composite engine's MODEL_ID is checked as its parts' ids joined, not as a manifest entry; `test_preseg_contract.py` as in 10.1). Waits for the real-machine acceptance (Mac) and the CUDA run (Ubuntu).
