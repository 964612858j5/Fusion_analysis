# FusionFlux v16 — Pre-TMA Architecture Gate v2.3 (consolidated)

**Status:** **v2.3 APPROVED** by the user on 2026-09-30 (amendment r3, commit `2cb496c`), **with v2.3.1** (A2b-probe revision, approved 2026-09-30, `c171d72`) and the **execution records** appended to the amendment after approval (A1b, A2a, A2b-probe, A2c migration blockers). This is the **self-contained consolidated v2.3 + v2.3.1** — the tracked base that v2.4 amends — and the primary reading version for execution windows. It merges the approved amendment (`FusionFlux_v16_PreTMA_Architecture_Gate_v2.3.md`), v2.3.1 (`FusionFlux_v16_PreTMA_Architecture_Gate_v2.3.1.md`) and every v2.2 clause that stays in force. If this file and the amendments ever disagree, the amendments govern and this file is corrected. v2.2 (`FusionFlux_v16_PreTMA_Architecture_Gate_v2.2.md`) and v2.1 are kept unchanged as historical records. Every stage still needs its own application + whitelist (P0 rules: `AGENTS.md`, `docs/P0_SCOPE_RULES.md`).
**Basis:** the user's ruling of 2026-09-30 ("time does not matter as long as it is not half a year; 1–2 months is acceptable; reshape Block01 in v16, and enter TMA only after the critical hazards are solved"), and the Fable / ChatGPT review rounds recorded in `~/fusionflux/意见.txt`. Those reviews are **not authorisation** (`AGENTS.md` rule 2); this draft states what the user is asked to approve (§18).
**Line:** v16 = this architecture gate (now including the ownership and lifecycle convergence) + TMA Foundation + QualityMask (blur / fold / low tissue). v15 is frozen at `f93f195` (tag `v15-final`).
**Time budget:** **at most 8 weeks** from the approval of v2.3. It is a **cap, not a schedule to fill**: as soon as the exit conditions (§0.2) and the start gates (§9) pass, TMA Foundation starts.
**Hard stop:** as in v2.2 — no further architecture work between the last gate and TMA Foundation unless a new scientific-correctness problem appears.

> **Core principle of v16:** v16 restructures **ownership and contracts**; it does not restructure the verified **scientific implementation**.

**Merge conventions.** v2.2 sections are copied verbatim. v2.3.1 is merged the same way: a replaced clause is struck through and followed by a *(v2.3.1: …)* note, and v2.3.1's own text is carried in §5.6.7; execution records are copied verbatim from the amendment. Where the amendment replaces a v2.2 clause inside a copied section, the old text is struck through and followed by a *(v2.3: …)* note naming the governing clause; nothing else in the copied sections is reworded. References of the form "v2.2 §n" inside v2.3 text point to §n of this file, which carries that v2.2 text. Sections §15–§17 (A6–A8) and §18 (rulings) come from the amendment; v2.2's "Definition of success" (its §15) is kept as §19 because the amendment already used §15–§18.

### Revision history

- **v2.3** (2026-09-30): amendment r1 → r2 → r3, approved with r3. r1 — first draft. r2 — after the independent review of r1 (2026-09-30), wording only: A7 rollback no longer satisfies gate 8 (E4 stays open unless the user grants an explicit waiver); `active_source_id` has one owner, ProjectState (A8), and A7 does not touch the source binding; `operates_on` (scope) and `depends_on` (lineage) are separated; A6's inventory is limited to public Step0–4 paths with a per-artifact completion / commit protocol; the approval → consolidation workflow is added (§18). Direction, the 8-week cap, E1–E5 and the stage order are unchanged. r3 — after the independent review of r2 (2026-09-30, "r2 can be approved; fix three wordings and mark APPROVED"), wording only: E4's active-source clause closes with §17.2 and gate 8 states that it does not test source ownership; §0.4 rule 6 exempts A6's generation checks inside existing callbacks; gate 9's test granularity matches §15.4; §16.2's renderer-state sentence rewritten. Approved by the user with r3.
- **v2.3.1** (2026-09-30): A2b-probe revision, approved with r1 — format-adapted IO on both sides, the 4-day time box with checkpoints C0–C2, the symmetric TIFF baseline, a closed optimisation list (§5.6.7). Merged here.
- **Consolidation r2** (2026-09-30): v2.3.1 and the execution records appended to the amendment after approval (A1b §4.6, A2c migration blockers §11, progress §12) merged; diff-checked against amendment + v2.3.1 + v2.2 (the §18 procedure). Base of v2.4.

### Changes from v2.2 (v2.3)

| # | Change | Why |
|---|---|---|
| 1 | Time budget: 10–15 days → **≤ 8 weeks, a cap** (header, §12) | User ruling 2026-09-30: 1–2 months is acceptable; v16 reshapes Block01 before TMA |
| 2 | **Five exit conditions** (§0.2) define "critical hazards solved". TMA starts when they and the gates pass; nothing else is chased | Without a closed list, 8 weeks becomes platform engineering |
| 3 | Exit conditions 4 and 5 are **goals, not prescribed implementations**: one authoritative camera owner with no write-back (not "a ViewerSession"); stale-result rejection where results reach UI / shared state (not "a unified cancellation framework") | A1 showed C1–C3 were locally fixable; the smallest structure that meets the goal wins |
| 4 | **Stop rule 1 is void** (§13). Replaced by the A7 rollback gate (§16.4) | Rule 1 ("fixed locally ⇒ no camera framework") forbids exit condition 4 |
| 5 | §0 scope firewall gains one entry criterion: the five exit conditions | Otherwise every new stage fails the firewall |
| 6 | New stages **A6** async lifecycle (§15), **A7** viewer-state ownership (§16), **A8** project-state ownership cleanup + A5 resource tiers (§17) | Implement exit conditions 4 and 5, and make "which run is authoritative" explicit |
| 7 | A3 gains `project_schema_version`, `artifact_id`, `depends_on` (§6.8) | Exit condition 3; the machine can answer "which segmentation / fusion / corrected inputs did this Step4 use" |
| 8 | Three new start gates **7–9** (§9) | Each exit condition needs a testable gate |
| 9 | **Block rules** (§0.4): acceptance pre-registered before the block starts; the old path is the oracle; every replacement revertible within one block; **monotonic green rule** per week | Prevents a half-migrated state and "three weeks in, results differ, nobody knows which week caused it" |
| 10 | **Schedule** (§12) replaced by an ordered week plan with a **pre-written branch** on the A2b-probe outcome | The probe result changes weeks 3–5; the branch is fixed now, not improvised later |
| 11 | Explicit **"not done in v16"** list (§0.3); new v17 backlog entries (§11) | Keeps the scope from growing silently |
| 12 | **New orchestration code for TMA and QualityMask starts in its own modules**, never inside `ui/main_window.py` or `ui/step0/step0_page.py` (§0.4) | Cheap rule that stops the two largest files from growing further without splitting them |

---

### Changes from v2.1 (v2.2, after A0)

| # | Change | Why |
|---|---|---|
| 1 | A0's storage result is restated: **the NGFF v0 implementation failed the canonical-store gate** — chunk (1, 512, 512), the generic zarr reader, the raw Step4 scan. **NGFF itself is not rejected.** | The user's ruling: A0 tested one layout with one reader, not the architecture. The viewer side was 1.3–1.5× faster; Step4 was 2× slower. |
| 2 | New stage **A2b-probe** (§5.6): a strictly time-boxed (≤ 2 days) NGFF layout + scan probe, with its **own adoption rule, written into this plan before the probe runs** | One targeted chance for a unified NGFF data plane, bounded so that it cannot delay TMA. |
| 3 | A2 is split into **A2a → A2b-probe → A2b (only if the probe passes) → A2c** | NgffSource / ingest is only built if the probe passes. |
| 4 | PixelSource unifies the **scientific source / storage contract**, not one read algorithm: region access, multiscale access and a **sequential-scan capability** (§5.1). Step4 may use a dedicated scan reader on the same canonical store. | Viewer tiles and the Step4 scan have opposite access patterns. |
| 5 | **Exactly one active canonical raster store** in the long term (§5.7): either NGFF (source TIFF ingested with a checksum, then archived) or TIFF (Zarr / NGFF only for derived rasters). Long-term TIFF + NGFF duplication is not the goal. | Two raw copies double the disk and create two sources of truth. |
| 6 | **Persisting corrected-channel coarse levels** becomes its own item (§5.8), independent of the raw-NGFF decision | A0 measured 22× on the Step1 corrected level 1, bitwise equal. |
| 7 | **Odon reference**, non-gating (§5.6.6) | Answers how far FusionFlux is from Odon, and where the gap is (storage / scheduler / decode / renderer). |
| 8 | Schedule (§12): A0 real machine → push → A0.5 → A1 → A2a → A2b-probe → decision → A2b / A2c → A3 / A4 / A5 → TMA. The probe comes **after** A0.5 and A1. | A0.5 and A1 are deterministic, already diagnosed work; finish them first. |
| 9 | Stop rule 7 (§13) for the probe | The probe stops at its time box, or when Step4 end-to-end stays about 1.5× or more slower. |
| 10 | *(independent review of the v2.2 draft, 2026-09-29)* A0.5 hashes the real model artifact where one exists (§3); A2a's `OmeTiffSource` delegates to the existing readers (§5.2); the probe compares the scientific payload, not whole files (§5.6.3); §5.8 is off the critical path (§12) | `model_checksum` is only a metadata-entry hash; A2a promises zero behaviour change; provenance fields differ by design; a 22× win must not eat Pre-TMA days |

**Method note.** The A0 rule was pre-registered and failed. The A2b-probe rule (§5.6.4) is less strict and is written **after** A0's data was seen. That is a known weakness. It is acceptable only because the new rule binds only the new probe, is fixed in this plan before the probe runs, and the probe measures new configurations (layouts, a scan reader, end-to-end Step4) rather than re-scoring A0's numbers.

### Changes from v2 Lean (v2.1)

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

### 0.1 Scope firewall

A newly found requirement enters this gate only if at least one holds:

- TMA would otherwise create a second, incompatible data model;
- changing it later would require rewriting stored TMA results;
- it affects scientific correctness or coordinate identity;
- it stops a representative large image from running without OOM;
- it causes a visible geometric inconsistency between Step0, Step1 and Step3;
- *(new)* **it is needed to meet one of the five exit conditions (§0.2), in the smallest form that meets it.**

Everything else is deferred (§11).

### 0.2 The five exit conditions (the "critical hazards")

v16 is finished when these five hold, and not later. "Done" means the matching gate in §9 passes. The only exception this plan allows is an explicit user waiver of E4 after an A7 rollback (§16.4 A); a waived E4 is recorded as **waived, not passed**.

| # | Exit condition | What "done" means | Stage(s) | Gate |
|---|---|---|---|---|
| E1 | **Data plane converges** | Image data has one explicit access contract, `PixelSource` (v2.2 §5.1). Viewer, Step1 fusion and Step4 no longer each interpret "what the source is"; each may still use its optimised capability (viewer → random / multiscale; Step4 → sequential scan). The A2b-probe decides whether NGFF is the canonical raster (v2.2 §5.6 / §5.7). | A2a, A2b-probe, (A2b), A2c | 2, 3 |
| E2 | **Space and identity converge** | One interpretation of `slide_id`, `region_id`, `segmentation_run_id`, `cell_id`; of `global_pixel`, `viewer_world`, `physical_um`, `region_local`, `pyramid_level` transforms and the bbox convention; plus `project_schema_version`. TMA never creates a second coordinate or identity system. | A3, A4 | 4, 5 |
| E3 | **Provenance is machine-traceable** | Every important artifact has `artifact_id`, `depends_on`, `schema_version` and its parameters / software provenance. The machine can answer "which segmentation, which fusion, which corrected inputs did this Step4 result use?" **Not done:** automatic stale propagation, automatic rebuild, artifact GC, a workflow engine. | A3 (§6.8) | 7 |
| E4 | **Viewer state has a single owner** | The camera and the display state that must survive a page switch each have exactly **one authoritative owner** on the viewer side (A7). Pages are consumers / renderers. The **active source identity is not viewer state**. It has one authoritative owner: ProjectState (`active_source_id`) after A8 (§17.1), or today's source binding if A8 takes the §17.2 minimum — never a second one. Renderers never own it (see §16.2 for the A7 → A8 order). Source ownership is verified by §17.3, not by gate 8. **Renderers may read and apply the authoritative camera; layout, resize, source rebinds, coarse / fine / mask arrival and every other event of v2.2 §4.4 must never write back to it.** Page switches no longer go through capture → apply → read-back. Whether the owner is a small `CameraStateOwner`, a `ViewerState`, or something smaller is decided by A7's code, not by this plan. | A7 | 8 |
| E5 | **Async results cannot cross over** | A task started for slide / dataset / page / run A that finishes after the user moved to B must not change B. **(a) Callbacks that write UI or shared state** carry a generation / current identity and are rejected when stale. **(b) Disk writers on the public Step0–4 paths that a late old task could overwrite or pollute** write to a **run-scoped destination with an explicit completion / commit protocol** — for a single file the existing temp file + `os.replace`; for a multi-file product (directory, Zarr, LabelStore) its existing transaction semantics or a completion / metadata-complete marker, with no filesystem-level atomic replace of the whole directory. If such a writer can write a **shared target** (e.g. one `current_result.h5ad` or one `fused.zarr` for several runs), it gets the same current-run validation as (a). A writer that already meets this is only recorded (§15.2). Workers that can be cancelled safely also support cancel; the others are not rewritten for it. **Not done:** a unified TaskManager. | A6 | 9 |

**Companion items**, not separate systems and not separate weeks:

- `project_schema_version: 1` — an integer; a reader that meets an unknown version fails with an explicit error (A3).
- Resource tier constants — the two A5 tiers (v2.2 §7.3) written as one constant configuration that each module reads for its own limits; **no dynamic arbitration** (A8).
- Invariant tests — one per exit condition and per gate, written in the stage that creates the invariant (§0.4).

### 0.3 Not done in v16

- a full Session framework (DatasetSession / PipelineSession / ViewerSession as prescribed classes);
- an artifact workflow engine: stale propagation, automatic rebuild, GC;
- a dynamic ResourceManager (e.g. "the viewer shrinks its cache while Step2 runs");
- a unified TaskManager / converting every QThread or worker to one task framework;
- splitting `ui/main_window.py` (~10.0 k lines) or `ui/step0/step0_page.py` (~11.6 k lines). Only the **ownership** of camera, dataset and run moves out of them (A7, A8); widgets and business logic stay;
- changing N2 / N3, Step4 mathematics, segmentation engines, GPU shaders or other verified scientific components;
- Rust; forking the repository.

### 0.4 Block rules (bind every v16 block from A1b on)

1. **Pre-registered acceptance.** Each block's acceptance conditions — automatic invariants, real-machine checks and the oracle — are written into its application, and approved, **before the block starts**. They are not decided half-way.
2. **Old path as oracle.** Every architectural replacement is compared against the v15 / pre-block path on the same input (bitwise, exact or the established tolerance, v2.2 §5.4).
3. **Revertible within one block.** Each block lands as commits that can be reverted on their own. If a block fails its acceptance, it is reverted, not patched forward into the next block.
4. **Monotonic green rule** (checked at the end of every week and of every block): **(i)** every gate / invariant completed earlier still passes, **(ii)** the current block's new invariants pass, **(iii)** the v15 scientific regression shows no new failure against `git archive HEAD` (procedure as in the A0.5 / A1 records). Gates that belong to later stages are not required yet.
5. **Cycle per block:** pre-register → implement → scientific / invariant comparison → PASS ⇒ keep; FAIL ⇒ revert.
6. **New orchestration outside the two largest files.** TMA, QualityMask and the new A6–A8 owners live in their own modules. `ui/main_window.py` and `ui/step0/step0_page.py` only get the wiring lines needed to use them. **Exception:** the generation / current-identity checks that A6 adds inside existing callbacks in these files (§15.2 step 2, reusing e.g. the display generation in `ui/main_window.py`) are not new orchestration and are allowed there.
7. **P0 rules unchanged.** Approving v2.3 approves the *direction* and names the smallest mechanism each stage may propose (§15–§17). It does **not** start any block. Each stage still needs its own application with a whitelist; `AGENTS.md` rule 4 is met by that application naming the mechanism explicitly. Viewer read / schedule / cache / thread changes still need their separate approval (`AGENTS.md` rule 5).

---

---

## 1. Target architecture frozen before TMA
```text
                ┌─────────────────┐
                │   PixelSource   │
                │ OME-TIFF        │
                │ OME-NGFF (if §5.6)│
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

- **Pixels:** one `PixelSource` contract (§5.1). **Exactly one active canonical raster store** per project (§5.7): OME-NGFF if the A2b-probe passes (§5.6), otherwise OME-TIFF with Zarr / NGFF for derived rasters only. OME-TIFF stays readable either way (old projects, and the ingest source).
- **LabelStore:** the scientific authority for segmentation identity and the cell↔nucleus relation.
- **Parquet:** the canonical object table (§6.3).
- **AnnData:** the canonical analysis state (§6.3).

### Not frozen

`expression.zarr`; DuckDB / Polars / DataFusion; FlowSOM / microclusters; a Rust Step5 core; a Rust viewer; a Rust Step2; the NEXUS redesign.

---

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

Output: a short report giving, for each transition, Δcentre x/y, Δscale, the first call that changes the camera, and the root cause. **No generic CameraState framework unless the evidence shows that a local fix cannot work.** *(v2.3: this A0-era rule is superseded for v16 by exit condition E4 and stage A7; stop rule 1 is void, §13.)*

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

"Raw stays OME-TIFF; only derived products use NGFF" is evaluated as an explicit alternative. If the rule fails, A2b and the NGFF half of Gate 2 move to the backlog. *(v2.2: superseded. The rule failed for the NGFF v0 implementation; the decision moves to the A2b-probe, §5.6.)*

### 2.4 Minimum layout v1 (drafted in A0, frozen in A3)

```text
<project>/
├── image.ome.zarr/                  # only if the A2b-probe adopts NGFF (§5.6, §5.7 (a); new projects)
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
  - **The NGFF v0 implementation failed the §2.3 rule** — chunk (1, 512, 512), Blosc-lz4 / zstd clevel 5, the generic zarr reader, the raw Step4 read scan. Gains: cold ROI 2048² 1.30×, level-0 viewport 1.46×, both under 1.5×. Regression: the raw Step4 scan was 1.95× slower (cold), against a 10 % limit. NGFF size was 2.01× the source with lz4 and 1.01× with zstd.
  - **v2.2 reading (user's ruling): NGFF itself is not rejected.** A time-boxed layout + scan probe decides (§5.6).
  - A supplementary, non-gating run with a direct-byte NGFF reader (the NGFF analogue of `TiffTileReader`) left the Step4 scan unchanged (45.9 s vs 45.6 s), while random ROI reads got faster (1.87× vs TIFF). **The Step4 gap therefore followed compressed size and decode cost, not zarr-python overhead.** The probe must vary layout and codec, not only the reader.
  - A0 measured **raw reads only**. Real Step4 overlaps reads with compute (a producer thread, queue depth 2). On `cropped_region` the TIFF read alone was 5.8 s, of a whole Step4 of about 12–13 s (S4-1), so an end-to-end measurement is required (§5.6.3).
  - Persisting corrected coarse levels is worth doing whatever the raw decision (§5.8): a stored corrected level 1 was 22× faster than today's runtime reduction and bitwise equal to it.
  - The NGFF copies were deleted. "Cold" means cold only for the WSL guest.
- **Contracts (A0-3)** — `docs/v16_contracts_draft.md`: PixelSource, coordinates (`world = global_pixel + 0.5` adapter rule, `bbox_fullres` `[y0, y1, x0, x1]` ↔ `bbox_x0..y1`, real per-axis level ratios, µm only from OME) and object tables. `region_id` ← `roi_id`, never `roi_name`. `slide_id` does not exist yet. Two findings are listed, not fixed: `image_mpp = 0.5` is hard-coded while the slide is 0.50686 µm, and `model_checksum` is not a weight-file hash (input to A0.5).
- **Product code unchanged**: `git status` / `git diff` list only whitelist paths.

---

---

## 3. Stage A0.5 — Engine identity scope fix
**Budget:** ≤ 0.5 day. **Must land before any new package (pyarrow) is installed.**

Today `seg_runner/runner.py:engine_identity` includes `lock_hash`, which hashes all of `conda-linux-64.lock` + `requirements-pip.txt`. Any package added to the environment, even one unrelated to segmentation, changes every engine's identity. Step2 then reports "engine identity changed" against the Step1 contract.

**Target** *(amended by the approved A0.5 application v3, `docs/v16_A05_application.md`, 2026-09-29; the earlier list — runner code hash, library versions, preprocessing libraries, a run-time hash of the model artifact — was an over-built fingerprint and is replaced)*. Three separate concerns:

- **Compatibility identity**, compared Step1 → Step2:
  - `engine`, an explicit per-engine **`behavior_version`** and **`model_id`** (the `models.json` key);
  - `behavior_version` is bumped **only** for a change that can alter the segmentation output: normalisation, threshold semantics, the expansion algorithm, the input-channel arrangement, post-processing, another default model. A package, a log line, the protocol or the UI never bumps it.
- **Provenance**, recorded, never compared: git commit, the runner code hash, library versions (engine + pre-/post-processing), `env_lock_hash` (the old `lock_hash`), device / CUDA mode, `model_manifest_entry_hash` (the old `model_checksum` — a hash of the manifest entry, **not** a weight checksum) and the resolved model path.
- **Model file integrity**: the deployment check `scripts/model_manifest.py --verify` against the manifest's per-file sha256. No weight file is hashed at run time.

Step2: another engine kind is refused (as today). A behaviour or model difference is asked in a dialog before the run and is recorded once the user confirms it; an unconfirmed difference is refused by the worker.

Old pre-segmentation runs whose identity still carries `lock_hash` are **accepted and only recorded** (user ruling, A0.5 v3): compared on the engine kind, never on `lock_hash`.

Tests: `tests/test_preseg_contract.py` and the Step2 contract check.

#### Execution record: A0.5 (2026-09-29; application `docs/v16_A05_application.md` v3, approved)

**Implemented and accepted.** Automatic acceptance passed. The user accepted the dialog on the real machine on 2026-09-30, using a synthetic params file with `behavior_version = 0` against the test1 copy.

- **`seg_runner/engines.py`:**
  - `IDENTITY_VERSION = 2`, `BEHAVIOR_VERSION` (all engines at 1, with the bump rule next to it) and `MODEL_ID` (the `models.json` keys);
  - `behavior_identity(engine)`, which is pure and importable without any engine library, and `compare_identity(step1, current)`, which marks a pre-v2 identity as legacy;
  - `lib_versions` now also records numpy, scipy and scikit-image, plus csbdeep for StarDist;
  - each engine reports `model_resolved_path`. `predict` is untouched.
- **`seg_runner/runner.py`:** `engine_identity` is the small v2 identity. The new `engine_provenance` holds git commit, `runner_version`, `lib_versions`, `env_lock_hash`, device, `model_manifest_entry_hash` and `model_resolved_path`. `hello` carries `provenance`.
- **`ui/step1_presegmentation/run_job.py`:** `run.json["engine_provenance"][engine]` (one line).
- **`workers/segment_merge_worker.py` `_start_contract_engine`:**
  - another engine kind is refused (unchanged);
  - a legacy Step1 identity is accepted and only recorded;
  - a behaviour or model difference runs only when the user confirmed exactly those differences, and is refused otherwise;
  - `seg_engine` gains `provenance` and `identity_comparison`.
- **`ui/step2_page.py` `_confirm_engine_identity`:** before a Step1 hand-over runs, a `Segmentation engine` warning (`Run anyway` / `Cancel`) lists each behaviour or model difference. The confirmation goes to the worker. There is no dialog when nothing differs, and none for a legacy identity.
- **`envs/fusion_mesmer/models.json`:** the three StarDist `2D_versatile_fluo_extracted/…` entries were removed. `scripts/model_manifest.py --verify` now reports only the known missing Mesmer model (4 files).
- **Docs:** `UI_SURFACE_RULES.md`, `docs/user_guide.md` and `docs/用户指南.md` describe the dialog; §3 above was amended.
- **Tests:**
  - new `tests/test_engine_identity.py` (13 cases);
  - 4 dialog cases in `tests/test_step2_engine_unified.py`;
  - in `tests/test_step2_runner_path.py`, the old "another engine version runs" case is replaced by three real-StarDist cases: legacy recorded only, confirmed difference recorded, unconfirmed difference refused.
- **Regression:** 17 modules, one process each, run sequentially on the current tree and on a `git archive HEAD` copy. **No new failure.** The five failures on both sides are known: Mesmer model missing ×3, and the StarDist 1-px flake ×2; `test_stardist_expansion_returns_the_nuclei_from_before_expanding` alternated pass / fail on re-runs.
- **Reverse injection** (scratch copy): each of these turns the tests red — `lock_hash` back in the identity, `lib_versions` in the identity, `model_id` not compared, legacy compared field by field, the dialog skipped, and an unconfirmed difference not refused.

---

---

## 4. Stage A1 — Viewer zero-drift fix; A1b — one page frame
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

~~A formal shared CameraState is introduced only if these are insufficient (stop rule 1).~~ *(v2.3: stop rule 1 is void; single camera ownership is exit condition E4, delivered by A7, §16.)*

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

#### Execution record: A1 (2026-09-30; application `docs/v16_A1_application.md` v2, approved)

**A1a (camera) implemented and accepted.** Automatic acceptance passed. On 2026-09-30 the user accepted the camera part on the real machine: no zoom on switching, and the same slide position (Step3 = Step0).

**Real-machine acceptance of the full requirement FAILED for layout reasons.** The viewer frames, the channel panels and the tabs occupy different window rectangles in Step0 / 1 / 3 (e.g. viewer centre 982.5 / 947.5 / 937.5 px at 1600 × 1000), so the same slide point lands on different screen pixels.
- The user ruled that this is solved by **block A1b**: one page frame for Step0–4 — a top slot (title + tab sub-slots), a small left slot, a large right slot and a bottom slot — refactoring Step0 if needed. The extra time is accepted.
- Application `docs/v16_A1_application.md` v3 §9 (geometry patching) is superseded by A1b; its rulings 1–5 carry over.

**Deferred:**
- C8 (zoom stall) → the Ubuntu server; the user suspects WSL.
- The black Step1 viewer after maximising the window reproduces on pre-A1 code too, so it is not A1. It is the known "Step1 occasionally black" issue: the fine / working-set budget refuses a large viewport and fails closed ("The display could not be composed"). → backlog, own block.

- **C1, `ui/step1_gpu_layer.py`:** the layer covers the ViewBox (`mapRectToScene(rect())`, not the padded `sceneBoundingRect`) and follows `sigResized` as well as viewport resizes. It no longer covers the whole viewport.
- **C6, same file:** the `paintGL` blit target is scaled by `devicePixelRatioF()`. At DPR 1 it is unchanged.
- **C2, `ui/step1_viewer_mount.py`:** `apply_camera` sets the float rectangle through `controller.set_view_rect_l0`, the same entry Step0 uses, instead of rounding and refitting through `jump_to`.
- **C3, same file:** on a ViewBox `sigResized`, the last non-resize (centre, scale) is put back with a plain `setRange`, so the request timing is the same as the refit it replaces.
  - Step1 / Step3 therefore keep their zoom when the window or a splitter changes size. Step0 is unchanged.
  - No state machine is added: only a cached (camera, size) pair.
- **Measured.**
  - Offscreen, the real switch path: every transition and 50 × 0→1→3→1→0 give Δcentre = 0 and Δscale = 0 (6 decimals). Before the fix: −52 px and −2.5 %.
  - Real GL: the transitions are Δ = 0 and FBO = ViewBox size.
  - Real GL, `gl-image` (shifts recomputed in the layer's own coordinates, because the script assumed a full-viewport layer): the six tissue patches are within ±0.5 px (were 4–7 px); the aligned mean absolute difference fell from 43.4 to 6.9.
- **Tests:**
  - new `tests/test_v16_zero_drift.py` (real `setCurrentIndex` order; exact transitions; 50 loops; resize keeps the camera; a zoom followed by a resize);
  - `tests/test_step1_gpu_layer.py`: the layer covers the ViewBox;
  - `tests/test_step3_viewer.py`: Step1 ↔ Step3 camera check made exact, scale included;
  - `tests/test_step1_gpu_takeover.py`: the test host is 530 × 530 (user-approved whitelist extension, 2026-09-30). With a 512 ViewBox no pixel centre lands exactly on a texel edge; before this, float64 numpy and the float32 shader picked neighbouring texels in one column (difference 2 against a tolerance of 1). The oracle and the tolerance are unchanged.
- **Regression:** 70 offscreen + 11 real-GL modules, one process each, current tree vs `git archive HEAD`. **No new failure.**
  - Seen on both sides: font ×2, montage Qt abort, StarDist 1-px flake, GL-context-limit aborts at the same points, and the GPU vendor-name assert.
  - `test_step0_step1_display_isolation` is intermittent on both sides (3/16 each).
- **Reverse injection:** each of the following turns tests red — rounding back in `apply_camera`, no resize handler, and the layer back on the whole viewport. C6 cannot be injected at DPR 1.
- **Docs:** `UI_SURFACE_RULES.md`, `docs/user_guide.md`, `docs/用户指南.md`.

---

### 4.6 A1b (v2.3)

The A1 record in v2.2 stands. **A1b** (one page frame for Step0–4) is part of v2.3's critical path:

- S0 design approved (`docs/v16_A1b_application.md`); S1 (StepFrame + Step1 / Step3) committed `c3b70b1`; S2 (Step0) application v2 approved (`docs/v16_A1b_S2_application.md`), in implementation at the time of this draft; S3 (Step2) and S4 (Step4) follow, each with its own application.
- A1b's F1–F4 frame-lock tests (`tests/test_v16_frame_lock.py`) join the monotonic green rule as soon as each stage lands.
- A1 + A1b together are the **baseline A7 is measured against**: A7 may not make any F1–F4 or zero-drift invariant worse.

#### 4.6.1 A1b execution record (2026-09-30) — DONE

Appended after approval; a record, not a change of plan.

| Stage | Commit | What landed |
|---|---|---|
| S0 | `3436953` | Design: one `StepFrame` for Step0–4 (title slot, one tab row, left / right columns, tool row, bottom slot); rulings 1–6 |
| S1 | `59473b3` (application), `c3b70b1` | `ui/step_frame.py` (`StepFrame`, `StepFrameMetrics`); Step1 + Step3 on it; one pixel column width W = max(W_user, floor), a widened W written back |
| S2 | `9ae89f6` (application v2), `9a82a32` | Step0 on the frame (tabs `Background Correction` / `Viewer`, Decision + Save in the bottom slot); metrics measured once by Step0; opening width = Step0's 4/3 rule on every page; **one row-independent floor** (the columns' own content, user ruling: a row wider than the column is covered from its right edge); `GlobalChannelRow.hold_own_width()` (whitelist extension approved) |
| S3 + S4 | `0b5f8bf` (application), `242064d` | Step2 on the frame (tabs `Parameters` / `Tile Status`) and in the shared width + floor; Step4 in the frame's **wide mode** (one real tab widget with an invisible tab: blank tab row, one slot = left column + handle + right column) |

- **Result:** title slot, tab row and bottom slot identical on all five pages; columns and tool row identical on Step0–3; channel panel, graphics viewport, ViewBox and (real GL) GPU layer identical on Step0 / 1 / 3 — to the pixel, offscreen and under real GL, at 1600 × 1000 and 2050 × 1330 (`scripts/diagnose_v16_a1b_frame.py`). Real-machine acceptance passed for S1, S2 and S3 + S4.
- **Tests:** `tests/test_v16_frame_lock.py` (25 tests) joins the monotonic green rule. Every stage's reverse injections turned it red.
- **Regressions:** one per stage, module by module (current code; failed modules re-run on HEAD, compared by failing test name): no new failures. One real regression was found and fixed during S2 (the opening-width block is load-bearing when Step1 is shown first). Known pre-existing failures and flaky tests are listed in each application's execution record.
- **Deviations** from the applications are recorded there: S1 (the widened width is written back), S2 (the row-width hold in the dock; the opening block restored), S3 + S4 (the wide slot built from a real tab widget instead of a probe + look-alike pane).
- **Open (closed by consolidation r2):** the consolidated v2.3 predated this record; it is merged here.

---

---

## 5. Stage A2 — PixelSource (+ NGFF if the probe passes)
**Budget:** 4–5 days in total for A2a + A2c, plus **at most 2 days** for the A2b-probe. A2b (1–2 days) only if the probe passes. **TMA blocker:** A2a yes; A2b only if the probe passes.

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

**What is unified is the scientific source / storage contract, not one read algorithm.** A source offers three capabilities over the same pixels:

```text
PixelSource
├─ region access        read_region / read_tile           (viewer, fusion)
├─ multiscale access    level_count / level_shape / level_downsample
└─ sequential scan      scan(channels, level, tile) → iterator of blocks, with read-ahead   (Step4)
```

The scan capability may be implemented by a dedicated reader (e.g. `NgffScanReader`, the analogue of today's `TiffTileReader`) over the same canonical store. Every capability must return bitwise the same pixels for the same (channel, level, region).

### 5.2 Split

- **A2a — contract + adapters for existing sources; zero behaviour change.**
  - `OmeTiffSource` is an **adapter that delegates to the existing optimised readers**; it does not merge or rewrite them:
    - `read_region` / `read_tile` → `RawTileProvider` (viewer), or the reader each consumer uses today;
    - `scan` → Step4's `TiffTileReader`, including its aszarr fallback.

    Zero behaviour change is the A2a promise, so reader consolidation is out of scope.
  - Step0's committed corrected zarr is exposed through the same interface.
  - Step4's fail-closed source contract (`core/quant_sources.py`) is kept unchanged.
- **A2b-probe — NGFF layout + scan probe; ≤ 2 days (§5.6).** Decides between §5.7 (a) and (b).
- **A2b — `NgffSource` + ingest; only if the probe passes.**
  - One ingest operation turns an OME-TIFF into NGFF 0.4.
  - The source image is left untouched.
  - A physical merge of raw + corrected into one hierarchy is not required: raw is uint8, corrected is float32, covers only some channels and is stored per ROI.
  - **Compliance check:** what we write must satisfy NGFF 0.4, not merely open in zarr: `multiscales` (version, axes with type/unit, datasets with `coordinateTransformations` scale per level), `omero` channel metadata if written, labels metadata if labels are written. It is verified by a minimal in-repo schema / reference check, with no new dependency.
- **A2c — consumer migration along real read boundaries, one consumer per application:**
  - **Step4:** reads raw slide pixels + the corrected zarr.
  - **Step1 fusion:** reads raw slide pixels and writes `fused.zarr`.
  - **Step2:** reads Step1's `fused.zarr`, which is a derived product, not a slide. It is left as is unless the fused product itself moves.
  - **Viewers (Step0 / 1 / 3):** separate P0 approval; Step0 may keep internal compatibility code.

### 5.2b Corrected coarse levels (see §5.8)

Independent of the probe; its own application.

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

### 5.6 A2b-probe — NGFF layout + scan optimisation (~~≤ 2 days~~ *(v2.3.1: ≤ 4 working days, §5.6.7)*)

Runs after A2a. It is a probe: scripts and scratch copies only, no product code. Its own application gives the whitelist and freezes §5.6.2–§5.6.4 again before running.

#### 5.6.1 Hard boundaries

- ~~**At most 2 working days**, then stop and decide on whatever has been measured.~~ *(v2.3.1: at most 4 working days with checkpoints C0–C2, §5.6.7 §4.)*
- No Rust. No new packages without the user's approval; zarr 2.18, numcodecs and imagecodecs as installed.
- **QuantBackend / QuantFinalizer and the S4-3 scientific logic are not touched.** Only the reader, the chunk scheduling (read-ahead, concurrency) and the on-disk layout may vary.
- **No scientific algorithm may be changed to make a benchmark pass.**
- The 2×2 synthetic mosaic is not enlarged; no 3×3 / 4×4 mosaics.
- ~~The probe must not push TMA past the v2.1 time budget (§12): it uses the day-15 buffer.~~ *(v2.3: replaced by the 8-week cap, §12 and §13 rule 7; the ≤ 2-day time box is unchanged.)* *(v2.3.1: the time box becomes ≤ 4 working days; the extra ≤ 2 days come out of the week-8 buffer.)*

#### 5.6.2 Matrix (small on purpose)

| Variable | Values |
|---|---|
| Level-0 chunk | (1, 512, 512), (1, 1024, 1024), (1, 2048, 2048); levels ≥ 1 stay (1, 512, 512) |
| Codec | Blosc-lz4 and Blosc-zstd, clevel 5 (as in A0) |
| Reader | the generic zarr reader (A0) vs a **parallel chunk scan + read-ahead** reader (direct chunk bytes, decode on the pool, the next scan tile read while the current one is consumed) *(v2.3.1: the pre-registered IO optimisation list of §5.6.7 §3, applied to NGFF and the same classes to the TIFF side)* |

The six layouts × two readers run on **`cropped_region`**, one copy at a time (1–2 GB each), each deleted after its runs. Only the best one or two layouts get a confirmation run on the synthetic mosaic (about 4–8 GB each), also one at a time.

#### 5.6.3 Measurements

- **Viewer:** random ROI 512² / 2048² / 4096², viewport pan L0 / L1 / L2 and channel switch, as pre-registered in A0 (same seeds and paths).
- **Step4 end to end:** the product's quantification (`QuantEngine`, S4-3 settings, the test1 copy's segmentation run on `cropped_region`) with **only the raw-pixel reader swapped** in the probe script. The TIFF baseline is the unmodified product path on the same data.
  - **The scientific payload must be bitwise identical**:
    - every numeric array and table (h5ad `X`, `layers`, `obs` numeric columns, `obsm`; the CSV's numeric values);
    - the cell ordering;
    - categorical and string biological fields (channel names, decisions, compartments).

    uint8 pixels are identical by construction, so any difference is a bug. **Provenance fields that intentionally record the reader, backend, paths, timings or commit may differ** and are excluded from the comparison; the comparison lists which fields it excluded.
- **Raw Step4 read scan:** as in A0, on `cropped_region` and in the mosaic confirmation. The mosaic has no segmentation, so no end-to-end run there.
- **Also:** Step1 fusion reads, disk size ÷ source, ingest time, peak RSS.
- **Cold / warm:** as in A0 (sync + fadvise + fincore; cold only for the WSL guest).

#### 5.6.4 Adoption rule for NGFF as the canonical store (proposed; fixed before the run)

All of the following, on the best layout, measured on `cropped_region` and confirmed on the mosaic where the workload exists there:

| # | Condition | Proposed threshold |
|---|---|---|
| 1 | Viewer, primary: cold ROI 2048² and level-0 viewport pan | NGFF **≥ 1.2× faster** on at least one, and **not slower** (≤ 1.05× TIFF time) on the other |
| 2 | Viewer, every other pre-registered workload | NGFF ≤ 1.05× TIFF time |
| 3 | **Step4 end to end** (cold and warm medians) | NGFF ≤ **1.2×** TIFF time |
| 4 | Step1 fusion reads | NGFF ≤ 1.1× TIFF time |
| 5 | Disk | NGFF ≤ **1.25×** the source TIFF, i.e. no near-doubling |
| 6 | Scientific output | bitwise identical (§5.6.3) |

- If it passes: §5.7 (a), and A2b builds `NgffSource`, ingest and `NgffScanReader`.
- If it fails, or if **Step4 end to end stays about 1.5× or more slower after optimisation**: stop the raw-NGFF direction; §5.7 (b). *(v2.3.1: the 1.5× line is judged only at checkpoint C1, after the adapted NGFF reader is complete; every row's baseline is the better of the TIFF product path and TIFF with the same IO classes, §5.6.7.)*
- Thresholds 1, 3 and 5 are the user's to confirm in the probe's application.

#### 5.6.5 Deliverable

A report with the matrix, the rule applied row by row and the decision (a) or (b). Raw JSON is kept; NGFF copies are deleted.

#### 5.6.6 Odon reference (non-gating)

- Done only if Odon is available and opens the same NGFF data; nothing is installed for it without the user's approval.
- Record: warm open → usable, uncached pan, zoom → sharp, channel switch, peak RAM.
- It answers where FusionFlux is slower (storage / scheduler / decode / renderer). It never gates TMA or the adoption rule.

#### 5.6.7 v2.3.1 — A2b-probe revision (in force; copied verbatim, headings demoted)

##### 1. The question the probe answers (clarified)

Not "which format is faster in theory", but: **is switching the canonical raw store to NGFF before TMA worth it**, when both formats get a fair, bounded, format-adapted IO layer and the scientific computation is identical?

- **IO layer — adapted per format, may be optimised:** traversal order aligned to the format's own blocks (NGFF chunks, TIFF tiles / strips), parallel decode, read-ahead, a block-level cache, and (NGFF only) the chunk shape and codec chosen at ingest.
- **Science layer — identical, never touched:** Step4's mathematics, QuantBackend / QuantFinalizer, the S4-3 logic, N2 / N3, the segmentation engines. Both readers hand the same pixel arrays to the same code. v2.3 §13 rules 2 and 7 (no scientific change to pass a benchmark) are unchanged.

##### 2. Changes

| # | Clause | v2.3 / v2.2 text | v2.3.1 |
|---|---|---|---|
| 1 | v2.2 §5.6 title, §5.6.1 time box; v2.3 §12 row "A2b-probe" | ≤ 2 working days | **≤ 4 working days** (ruling 2). The extra ≤ 2 days come out of the week-8 buffer; the 8-week cap (v2.3 §13 rule 5) is unchanged |
| 2 | v2.2 §5.6.1 / §5.6.2: what may vary | reader, chunk scheduling, on-disk layout; readers = generic zarr vs one "parallel chunk scan + read-ahead" reader | A **pre-registered IO optimisation list** (§3), applied to NGFF, and — ruling 3 — the same classes applied to the TIFF side where the format allows |
| 3 | v2.3 §13 rule 7 and v2.2 §5.6.4: early stop | stop early with §5.7 (b) when Step4 end to end stays ≈ 1.5× TIFF or worse | The 1.5× early-stop line is judged **only after the adapted NGFF reader is complete** (checkpoint C1, §4), on its best layout — never on the generic first version |
| 4 | v2.2 §5.6.4: adoption rule | six rows; viewer rows 1–2, Step4 end to end row 3 | **Rows unchanged**; the comparison baseline of every row is made explicit (§4): NGFF's best adapted configuration vs **the best of** (TIFF product path, TIFF with the same IO classes). Thresholds 1, 3, 5 are still the user's to confirm in the probe's application |
| 5 | v2.3 §12 pre-written branch | probe in week 2 | probe in weeks 2–3; branch (a) and (b) otherwise as written, shifted by at most 2 days out of the buffer |

Unchanged and restated for clarity: no Rust; no new packages without approval (zarr 2.18, numcodecs, imagecodecs as installed); scripts and scratch copies only, no product code; the 2×2 mosaic is not enlarged; **oracle unchanged** — NGFF pixels bitwise equal to TIFF, and the Step4 scientific payload bitwise identical (v2.2 §5.6.3); one NGFF copy on disk at a time, deleted after its runs; disk policy v2.2 §8.

##### 3. Pre-registered IO optimisation list (closed)

Only these; anything else needs the user's ruling during the probe.

1. **Block-aligned traversal** — the scan visits whole NGFF chunks (TIFF: whole tiles / strips) in storage order; no request straddles blocks it does not need.
2. **Parallel decode** — compressed block bytes read directly, decoded on a thread pool (size fixed per run and recorded).
3. **Read-ahead** — the next scan tile's blocks are fetched while the current one is consumed (depth fixed per run, recorded).
4. **Block-level cache** — a bounded LRU of decoded blocks, sized to the scan's working set (size recorded; counts toward peak RSS).
5. **Layout (NGFF only)** — level-0 chunk (1, 512, 512), (1, 1024, 1024), (1, 2048, 2048) × Blosc-lz4 / Blosc-zstd clevel 5 (v2.2 §5.6.2's six layouts); levels ≥ 1 stay (1, 512, 512).

A viewer-side counterpart uses items 2 and 4 for random ROI / pan reads (the viewer already has its own scheduler; the probe only swaps the source reads in a script, as v2.2 §5.6.3).

##### 4. Procedure and checkpoints

- **C0 (≈ day 1):** generic readers on both formats + the adapted NGFF reader written; oracle checks green (pixels bitwise, Step4 payload bitwise on `cropped_region`).
- **C1 (end of day 2 at the latest):** the six layouts measured with the adapted reader on `cropped_region`. **The 1.5× early-stop line is applied here**, on the best layout: if Step4 end to end (cold and warm medians) is still ≥ 1.5× the TIFF baseline, stop → §5.7 (b).
- **C2 (days 3–4):** the TIFF side with the same IO classes (ruling 3); the best one or two NGFF layouts confirmed on the synthetic mosaic (one copy at a time); the full adoption rule (v2.2 §5.6.4) applied row by row.
- **Hard stop at 4 working days** whatever has been measured; the decision uses what exists.
- **Baseline of each row:** NGFF best adapted vs **min(TIFF product path, TIFF with the same IO classes)**. Both TIFF numbers are reported. If the TIFF optimisations beat the product path, that is recorded as a backlog item (a possible TIFF reader improvement), not implemented in v16.
- **Deliverable:** as v2.2 §5.6.5, plus a table of which optimisation classes were used on each side and their parameters.

##### 5. Items for the user's ruling

1. **Approve v2.3.1** as a revision of the A2b-probe clauses only (§2); everything else in v2.3 / v2.2 unchanged.
2. **Time box:** ≤ 4 working days (Fable's suggestion 3–4). *Recommendation: 4, with the checkpoints of §4 — the 1.5× line at C1 still ends the probe early when NGFF is clearly behind.*
3. **Symmetric fairness:** apply the same IO classes (block-aligned traversal, parallel decode, read-ahead, block cache) to the TIFF side too, and compare NGFF against the better TIFF number. *Recommendation: yes.* Reason: otherwise a gain from parallel decode would be credited to NGFF although TIFF gets it too; the decision question is whether to switch the format, and a switch is only worth it for what the TIFF path cannot also get. Cost: about one day, inside the 4.
4. **The closed optimisation list of §3** (and that anything outside it needs a ruling during the probe). *Recommendation: approve.*
5. **Viewer and Step4 both decide:** the adoption rule's rows are unchanged (viewer rows 1–2, Step4 row 3, fusion row 4, disk row 5, science row 6); thresholds 1, 3 and 5 are confirmed in the probe's own application, as v2.2 already says. *Recommendation: approve.*
6. **Commit** this revision after approval, together with the A1b execution record appended to v2.3 §4; the untracked consolidated v2.3 then needs the A1b record and a pointer to v2.3.1 before it is committed (v2.3 §18 workflow).

##### Rulings (user, 2026-09-30)

Items 1–6 **approved as recommended**: time box ≤ 4 working days with checkpoints C0–C2; symmetric IO classes on the TIFF side, NGFF compared with the better TIFF number; the closed optimisation list of §3; the adoption rule's rows unchanged, thresholds 1 / 3 / 5 confirmed in the probe's application; commit and push together with the A1b execution record.

#### 5.6.8 Execution record (2026-09-30)

A2b-probe completed in about 2 hours; **outcome (b), ruled by the user 2026-09-30** — OME-TIFF stays the canonical raw store, zarr / NGFF for derived rasters only, A2b not built, no further NGFF testing. Report `docs/v16_A2b_probe_report.md`, record `docs/v16_A2b_probe_application.md` §11.

### 5.7 One active canonical raster store (end state)

**(a) NGFF wins:**

```text
source OME-TIFF ──ingest + checksum──▶ canonical NGFF (in the project)
                                        source TIFF archived / moved out of the project
```

The ingest records the source's sha256 and verifies the NGFF against it (every level, bitwise) before the TIFF may be archived. Old projects are not migrated (§2.4).

**(b) NGFF does not win:**

```text
OME-TIFF = canonical raw
Zarr / NGFF = derived rasters only (corrected channels and their levels, labels, fused)
```

Long-term TIFF + NGFF duplication of the raw pixels is not an end state in either case.

### 5.8 Corrected-channel coarse levels (independent of §5.6)

- **Today:** Step1 reduces the corrected level 1 from level 0 at runtime (`viewer/step1_source.py`, `reduce_corrected`); only a coarse sidecar exists.
- **A0:** a stored level 1 built with the same `reduce_corrected` was bitwise equal and 22× faster on the viewport_l1 path, and cost 12 MB per channel (levels 1 + 2).
- **Proposal:** Step0 writes the corrected levels once, with NGFF 0.4 multiscales metadata and the same per-axis-ratio scales. Step1 reads a stored level when it is valid and keeps the runtime reduction as the fallback.
- It touches the Step0 writer and the Step1 viewer read path, so it needs its own application with a P0 whitelist (viewer read changes need separate approval, `AGENTS.md` rule 5). It comes after A1, and it is **off the critical path** (§12): not a TMA blocker, and it never delays A3, the gates or TMA.

---

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

*(v2.3 additions, §6.8:)*

- [ ] `project_schema_version` written; an unknown version is refused with an explicit error (test).
- [ ] For one real result, the graph answers which segmentation run, which `fused.zarr` and which corrected channels a Step4 h5ad used, through `depends_on` only.
- [ ] No `depends_on` list contains a `region_id` (test); the segmentation run's regions come from `operates_on`.

---

---

### 6.8 A3 additions: schema version and artifact dependencies (v2.3)

Added to A3 (v2.2 §6.1–§6.2), written together with `transforms.json` / `provenance.json`:

- **`project_schema_version: 1`** in the project-level metadata. A reader that meets a version it does not know refuses with an explicit error; it never guesses. Old projects without the field are read as today (legacy), never migrated. The existing `handoff_schema_version = 2` of the Step0 hand-off stays as it is.
- **Per artifact, in `provenance.json`**: `artifact_id`, `kind` (e.g. `corrected_channel`, `fused`, `segmentation_run`, `step4_h5ad`, `cells_parquet`), `schema_version`, `depends_on: [artifact_id, ...]`, parameters and software provenance (reusing A0.5's `engine_provenance` where it applies).
- **`operates_on` and `depends_on` are different things and are never mixed:**
  - `operates_on: [region_id, ...]` (v2.2 §6.1) = **spatial / biological scope** — which Regions a run acts on;
  - `depends_on: [artifact_id, ...]` = **artifact lineage** — which upstream artifacts a product was made from. It contains artifact ids only, never a `region_id`.

  ```text
  seg_022:
    operates_on: [core_A3]        # scope
    depends_on:  [fusion_018]     # lineage
  ```
- **Artifact graph v0** = a read-only view built from `provenance.json`, whose edges are **only** `depends_on` (artifact_id → artifact_id). It answers "what does X depend on" and "what depends on X". Spatial scope is read from `operates_on` separately and is not part of the graph. It does not mark anything stale, rebuild anything or delete files.
- Written by new code and by the paths A3 / A4 already touch; producers that A3 does not touch get their entry when a later block touches them. The acceptance lists which producers write entries.

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

---

## 9. TMA start gates (nine)
1. **Viewer stability:** Step0 ↔ Step1 ↔ Step3 is visually stationary, with no cumulative drift (automatic + real machine).
2. **PixelSource:** consumers read through one contract with `OmeTiffSource`. The A2b-probe has decided the canonical store (§5.7). If NGFF won, `NgffSource` (with its scan capability) implements the same contract and the representative dataset runs through it.
3. **Scientific invariance:** the representative dataset completes PixelSource → Step2 → N3 → LabelStore → Step4 S4-3, meeting §5.4 against the accepted v15 implementation.
4. **Coordinate / identity freeze:** hierarchy, SegmentationRun key, coordinate names and conventions frozen. A TMA core is an ordinary region.
5. **Object layer:** cells.parquet + regions.parquet from a real result, with keys and coordinates agreeing with LabelStore and the viewer.
6. **Resource boundedness:** viewer / Step2 / Step4 runs do not OOM on the dev machine, use bounded queues and caches, report peaks, and name the remaining bottleneck.

---
7. **Provenance (E3):** for the representative dataset, every artifact produced by the gate run has an `artifact_id` and `depends_on`; the artifact graph answers the Step4 → segmentation run → fused → corrected question through `depends_on` (artifact lineage only; the run's regions come from `operates_on`, which is not a graph edge); `project_schema_version` is written and an unknown version is refused.
8. **Viewer-state ownership (E4):** the authoritative camera is written only by user actions (pan, zoom, jump, Navigator jump) and explicit programmatic navigation; an automatic test logs every write during Step0 ↔ 1 ↔ 3 switches, resizes, source refresh to the same source, coarse / fine / mask arrival and channel enable, and asserts **zero write-backs from those events**. The A0 camera-log script (`scripts/diagnose_v16_a0_camera.py`) becomes this assertion. Real machine: 50 × Step0 ↔ 1 ↔ 3 without visible drift. **Gate 8 passes only when A7 passes.** If A7 is rolled back (§16.4), gate 8 does **not** pass and E4 stays **NOT PASS**; what happens next is the user's explicit ruling (§16.4: waiver A or no waiver B). Gate 8 tests camera and display-state ownership only; it does **not** test active-source ownership, which is covered by §17.3.
9. **Async lifecycle (E5):** for A6's inventory (§15.2 — public Step0–4 paths only), the controlled-delay tests of §15.4 (P0 rule 5: a normal asynchronous late arrival, not a hostile test) show that a result from the old slide / dataset / run arriving after the switch leaves the new state unchanged: **every row A6 changed** has its own test; rows only confirmed as already compliant are covered by **one test per completion / commit protocol type**. Each disk writer in the inventory writes to a run-scoped destination with its explicit completion / commit protocol (E5 (b)); a writer to a shared target also has the current-run check.

**TMA starts when gates 1–9 pass**, or when gates 1–7 and 9 pass and the user has granted the explicit E4 waiver for gate 8 (§16.4 A). No other gate can be waived by this plan.

---

---

## 10. After the gates
TMA Foundation → core detection / dearray → manual grid and core correction → core identity + sample / patient mapping → QualityMask (blur, fold, low tissue) → TMA Batch → real HCC TMA.

No further architecture polishing between ~~Gate 6~~ the last gate *(v2.3: gates 7–9 added, §9)* and TMA Foundation unless a new scientific-correctness problem appears.

---

---

## 11. Deferred backlog
- **Data / query:** `expression.zarr`; a DuckDB / Polars / DataFusion benchmark; GeoParquet; spatial indexing; a SpatialData adapter.
- **Step5:** a 10 M × 80 edge runtime; streaming PCA; microclusters / FlowSOM; Rust HNSW / Leiden; UMAP redesign; out-of-core Step5.
- **Segmentation:** CPU StarDist + GPU Cellpose; topology-guided reconciliation; adaptive tiling.
- **Rust:** viewer IO; a PixelSource backend; Step2 streaming; a Step4 backend.
- **NEXUS:** object-query tools; Parquet-native evidence queries; edge orchestration.
- **Out of scope throughout:** the unmaintained HQ / HQ2 / CDS methods.

---

Added in v2.3 as **v17 candidates**, to be designed with real TMA workloads:

- Session-layer split of `ui/main_window.py` and `ui/step0/step0_page.py`;
- stale propagation / automatic rebuild over the artifact graph;
- dynamic resource arbitration between viewer, Step2 and Step4;
- a unified task manager;
- the E4 debt, if the user grants the E4 waiver after an A7 rollback (§16.4 A), with the recorded reason;
- A8 fields left in place under §17.2.

**A2c migration blockers** (recorded by A2a, `docs/v16_A2a_application.md` §2.2 items 4–5, user ruling 2026-09-30). They are not deferred to v17: the A2c application that migrates a consumer using one of these paths must rule explicitly, *fix it* or *prove it irrelevant to that consumer*:

- `OMETIFFLoader._read_corrected_roi_only` returns None for a request not fully inside one saved ROI and the loader then **silently serves raw pixels** for a corrected channel (`core/io_loader.py:92-99`, `:216-234`); Step1 refuses and Step4 fails closed, the loader path does not;
- the loader's lazily created `_corrected_store` has no lock while `set_corrected_zarr_store` / `set_correction_config` run on the GUI thread and preload / fusion / random-patch threads read.

Still in the backlog, not in v16 unless A0-style evidence shows a shared cause with a v16 stage: the occasionally black Step1 viewer (fine / working-set budget refusal), the ~6.5 s first Step3 `resizeGL` stall, C8 zoom stall, the "→ Feature Extraction (Step 4)" button that emits `open_qc_requested`.

---

---

## 12. Schedule

Estimates are working days. The weeks are an **order and a budget**; if the exit conditions pass early, TMA starts early. Real-engine regressions run module by module (memory `wsl-disk-and-memory`), so about a quarter of each week is regression and real-machine time — the estimates below include it.

| Week | Stage | Est. | Why here |
|---|---|---|---|
| 1 | **A1b** S2 (Step0) → S3 (Step2) → S4 (Step4) | ~4.5 d | In progress; A7 needs a baseline that does not move |
| 1–2 | **A2a** PixelSource contract + `OmeTiffSource` adapters, zero behaviour change | ~3 d | Data plane first; A8's `active_source_id` must name a source defined by the PixelSource contract |
| 2 | **A2b-probe** (~~≤ 2 d~~ *(v2.3.1: ≤ 4 d)*, v2.2 §5.6) → decision §5.7 (a) / (b) | ~~≤ 2 d~~ *(v2.3.1: ≤ 4 d)* | ~~Unchanged rule and time box~~ *(v2.3.1: adapted IO on both sides, §5.6.7)* |
| 3 | **A2c** Step4 + Step1 fusion migration; (A2b if (a), see branch) | ~2 d (+1–2 d) | — |
| 3–4 | **A3 + A4** identity, coordinates, Parquet, `transforms.json`, `provenance.json`, `project_schema_version`, `artifact_id` / `depends_on` | ~3–4 d | Artifact graph v0 is read from `provenance.json` |
| 4 | **A6** async lifecycle (§15) | ~3 d | Must precede A7: renderer attach / detach relies on stale-result rejection |
| 5 | **A7** viewer-state ownership (§16), **with the rollback gate at the end of the week** | ~4–5 d | The riskiest week; the A0 camera log runs as a test every day |
| 6 | **A8** project-state ownership cleanup + **A5** resource tiers and boundedness inventory (§17) | ~4 d | Moves ownership only; no widget or layout change |
| 7 | Full regression; gates 1–9; one invariant test per exit condition; real-machine acceptance; every gate compared with `v15-final` | ~4–5 d | — |
| 8 | **Buffer.** If the server is back, one real WSI (v2.2 §8, supplementary) | ≤ 5 d | The buffer is not for new features |

**Pre-written branch on the A2b-probe outcome** (decided now, applied without re-planning):

- **(b) NGFF not adopted:** the table above as written.
- **(a) NGFF adopted:** week 3 builds `NgffSource`, ingest and `NgffScanReader` (A2b) before A2c; **A3 / A4 move to week 4, A6 to week 5, A7 to week 6, A8 + A5 to week 7, regression + gates to week 8**; the buffer shrinks to what is left of week 8. If that leaves no buffer, the user decides at the end of week 6 whether to extend within the 8-week cap by dropping the real-WSI run, or to trim A8 to its minimum (§17.2).

**Progress** (a record, appended): A1b done 2026-09-30 (§4.x). A2a done 2026-09-30 — `core/pixel_source.py` + `sources/` (`docs/v16_A2a_application.md` §10). **A2b-probe PASS (probe completed) 2026-09-30, outcome (b)** — ruled by the user on 2026-09-30: OME-TIFF stays the canonical raw store, zarr / NGFF for derived rasters only, A2b not built. About 2 hours of the ≤ 4-day box (v2.3.1); every NGFF copy deleted; raw data unchanged (size / mtime equal to the recorded fingerprint). Report `docs/v16_A2b_probe_report.md`, record `docs/v16_A2b_probe_application.md` §11. The (b) branch of this section applies: A2c next.

**Off the critical path** (unchanged): v2.2 §5.8 corrected coarse levels — opportunistic, own application, never delays a gate.

**Before each week starts**, the acceptance conditions of the blocks of that week are in their approved applications (§0.4 rule 1).

---

---

## 13. Stop rules

1. ~~If zero-drift is fixed locally, no global camera framework.~~ **Void in v2.3.** Replaced by rule 8.
2. *(unchanged)* If NGFF requires rewriting scientific algorithms, stop and redesign the adapter boundary.
3. *(unchanged)* Anything needed only for a hypothetical 10 M-cell Step5 goes to the backlog.
4. *(amended)* An optimisation or restructuring that serves none of scientific correctness, TMA compatibility, OOM safety, the five exit conditions or the nine gates cannot extend the schedule.
5. *(replaced)* **The 8-week cap is a decision point.** If gates 1–9 pass earlier (or gates 1–7 and 9 with the explicit E4 waiver, §9), TMA starts then; "optimising v16 further" is not a reason to stay. If they have not passed at the cap, the user decides what is dropped or deferred; the cap is not silently extended.
6. *(unchanged)* After two review rounds with no new public-path defect, hardening stops (P0 rule).
7. *(amended wording)* **A2b-probe:** stops at ~~2~~ *(v2.3.1: 4)* working days whatever it has measured, or early with §5.7 (b) when Step4 end to end stays at about 1.5× TIFF or worse *(v2.3.1: judged at checkpoint C1, after the adapted reader is complete)*. Nothing in the scientific backend is changed to pass it. (The v2.2 phrase "uses the day-15 buffer" is replaced by the 8-week cap.)
8. *(new)* **A7 rollback gate** (§16.4): if at the end of A7's week the real-machine 50-switch test drifts, or the zero-write-back assertion fails and cannot be fixed within the block, A7 is reverted to the A1 + A1b state and the plan continues with A8. The rollback is a safety mechanism, not a failure of the plan — but it does **not** complete E4 or gate 8; they stay open until the user rules on a waiver (§16.4).
9. *(new)* **No new stage without a plan revision.** A stage not in §12 enters v16 only through a v2.x revision approved by the user; a review comment or a block application cannot add one.

---

---

## 14. Version control
- **Done 2026-09-29:** annotated tag `v15-final` on `f93f195`, pushed to `origin`. The branch `v15-interactive-channel-workspace` is kept unchanged, local and remote.
- **Done 2026-09-29:** `v16` branched from `f93f195` as the integration line and pushed. Its first commit is this plan (v2.1 only; v1 / v2 Lean are not committed). Short feature branches (`v16-a0-diagnosis`, `v16-viewer-zero-drift`, `v16-pixelsource`, `v16-tma-foundation`, `v16-qualitymask`) are opened as needed.
- `origin/main` is **not an ancestor** of `f93f195`: it is an unrelated history of 18 GitHub web-upload commits, from `4d97b96 initial commit` to `ee6f2cd`, dated 2026-05-19, with no common ancestor. It cannot be fast-forwarded. It is left untouched in this block; what to do with it (keep as is, replace, or change the remote default branch) is a separate decision by the user. No force push.

---

---

## 15. Stage A6 — Async lifecycle

**Budget:** ~3 days. **Exit condition:** E5. **Gate:** 9.

### 15.1 Starting point

Generation mechanisms already exist and are reused, not replaced: Block01's display generation (bumped on dataset transitions, `ui/main_window.py` ~5235), the Step0 dataset-generation invalidation (~597, ~2729), the Step1 compose coordinator's owner / generation checks, the viewer's stale-drop, and atomic `os.replace` commits in `workers/feature_extract_worker.py`, `workers/segment_merge_worker.py`, `core/label_pyramid.py`, `core/preseg_run.py`, `seg_runner/protocol.py`, `core/step0_handoff.py`.

### 15.2 Work

1. **Inventory (read-only, first day).** Scope: **only the current v16 Step0–Step4 public product paths**, and on them only
   - asynchronous tasks whose result reaches UI or shared state, and
   - disk writers whose output a late old task could overwrite or pollute for the current run.

   **Excluded:** `scripts/`; benchmark and diagnostic scripts; background tools unrelated to the public Step0–4 paths; workers with no cross-over / overwrite risk.

   Each row records: what identity the task was started for, whether its callback checks that identity, where it writes, whether that destination is run-scoped, and which completion / commit protocol it uses. Expected in scope: viewer loading, the Step1 loader, the Step2 completion callback, Step4 progress / completion. The application lists the actual rows.
2. **UI / shared-state callbacks without a check:** add the check using the existing generation where one applies; where none applies, **one small helper** (a generation value captured at start, compared on arrival). This helper is the only new mechanism A6 may propose.
3. **Disk writers:** the contract is **run-scoped destination + an explicit completion / commit protocol**, implemented per artifact type:
   - single file: temp file + `os.replace` (the existing atomic replace);
   - multi-file / directory / Zarr / LabelStore: the existing transaction semantics, a completion marker or a metadata-complete marker (e.g. `core/preseg_run.py`: masks first, the record last). A filesystem-level atomic replace of a whole directory is **not** required.

   A writer that already meets the contract is only recorded as confirmed, not rewritten. A writer that can write a **shared target** gets the current-run / generation check of step 2.
4. **Cancel:** only where the worker already has a safe stop point. No worker is rewritten to become cancellable.

### 15.3 Not done

No TaskManager, TransactionManager or new storage framework; no retrofit of workers outside the inventory; no change to worker computation.

### 15.4 Acceptance (pre-registered in the A6 application)

- **Every row A6 changed:** a controlled-delay test that fails on the pre-A6 code (the gap is real; reverse injection) and passes after.
- **Rows only confirmed as compliant:** one controlled-delay test per completion / commit protocol type (e.g. one single-file `os.replace` writer, one marker-based multi-file writer), not one per row.
- The monotonic green rule.

---

---

## 16. Stage A7 — Viewer-state ownership

**Budget:** ~4–5 days. **Exit condition:** E4. **Gate:** 8. Viewer changes: separate P0 approval (`AGENTS.md` rule 5).

### 16.1 Starting point

`ui/shared_camera.py` (`CameraSnapshot`) and MainWindow's `_capture_camera_of` / `_apply_shared_camera_to` (`ui/main_window.py` ~2276 / ~2293): each switch captures the leaving page's camera, applies it to the target and reads it back. A1 made this exact, but the feedback path (target resize → read-back → shared camera) still exists structurally; A1 C3 guards it with a cached (camera, size) pair.

### 16.2 Target

```text
Authoritative camera  (written only by user navigation and explicit programmatic jumps)
        │
        ├──→ Step0 renderer
        ├──→ Step1 renderer
        └──→ Step3 renderer

forbidden:  renderer resize / layout / source / tile arrival ──readback──▶ authoritative camera
```

- The same for the display state that must survive a switch (the A7 application lists which fields). State that is private to one renderer's view and does not need to survive a switch stays in that renderer; the owner does not take it over.
- The smallest owner that achieves this; the application proposes its form and the user approves it.
- **Active source is out of A7's scope.** During A7 the active source / source binding stays exactly where it is today; it is not moved, and A7 creates **no** temporary active-source owner. `active_source_id` moves into ProjectState only in A8 (§17). A7 therefore does not depend on ProjectState existing. After A8, renderers resolve / receive their source from `ProjectState.active_source_id`.
- `_capture_camera_of` / `_apply_shared_camera_to` and the C3 cached pair are removed once the owner covers them.

### 16.3 Acceptance (pre-registered)

- Gate 8's zero-write-back assertion (automatic), plus every A1 zero-drift test and every A1b F1–F4 test unchanged.
- Real machine: 50 × Step0 ↔ 1 ↔ 3, including resize, Navigator jump, coarse → fine, channel enable; no visible drift.

### 16.4 Rollback gate

At the end of A7's week:

- **A7 PASS** (§16.3 met): E4 PASS, gate 8 PASS.
- **A7 FAIL** (§16.3 not met and not fixable within the block): revert A7 (stop rule 8) and keep the stable A1 + A1b state. The plan continues with A8. **E4 stays NOT PASS and gate 8 does not pass automatically.** The user rules explicitly:
  - **A. Grant an E4 waiver.** Gate 8 is waived as an explicit exception; the E4 debt goes into the v17 backlog (§11); the waiver and its reason are recorded in this plan's execution record and in the project provenance record. TMA may proceed once the other gates pass.
  - **B. No waiver.** Gate 8 stays open, and the architecture gate is not closed.

The rollback is a safety mechanism, not a failure: the architecture is not allowed to hold the viewer hostage. It is also not a completion of E4.

---

---

## 17. Stage A8 — Project-state ownership cleanup + A5 resource tiers

**Budget:** ~4 days. **Supports:** E2 / E4 (explicit active identity), A5.

### 17.1 Work

- **Explicit project state** — ProjectState is the **authoritative owner** of `active_slide_id`, `active_region_id`, `active_segmentation_run_id` and `active_source_id`, read by the pages that need them. A8 moves `active_source_id` in from where the source binding lives today (it stayed there through A7, §16.2); renderers then resolve / receive their source from it and never own it. The camera and display state stay with A7's viewer-side owner. Today `active_segmentation_run_id` is implied by the Step2 → Step3 hand-off and the `open_qc_requested` signal; A8 makes it an explicit field and Step2 / Step3 / Step4 read the same one.
- **No prescribed classes.** Whether this is one `ProjectState` next to A7's owner, or needs a split, is decided by the code: a second object is introduced only if the code shows two genuinely independent lifecycles.
- **A5** (v2.2 §7, unchanged requirements) + the resource tier constants (§0.2).
- Ownership moves only: no widget, layout or behaviour change.

### 17.2 Minimum if time is short

The explicit `active_segmentation_run_id` read by Step2 / 3 / 4, plus A5. The other fields (including `active_source_id`) may stay where they are and are recorded for v17; in that case there is still exactly one place that holds the active source — today's source binding — and no second owner is created.

### 17.3 Acceptance (pre-registered)

Step2 → Step3 → Step4 on the representative dataset use the same run id from the one field (test); A5's v2.2 §7 records exist; the monotonic green rule.

---

---

## 18. Items for the user's ruling

1. **Approve v2.3** as the authoritative v16 plan (supersedes v2.2): the ≤ 8-week cap, the five exit conditions (§0.2), the void stop rule 1, and the core principle.
2. **Gates 7–9** (§9) as TMA start conditions. Gate 8 passes only with A7; after an A7 rollback it stays open unless the user grants the explicit E4 waiver.
3. **A7 rollback criterion and waiver rule** (§16.4): real-machine 50 switches + the zero-write-back assertion, decided at the end of A7's week; on rollback, the user chooses A (waiver, debt to v17, recorded) or B (gate 8 stays open).
4. **The pre-written A2b-probe branch** (§12), including the end-of-week-6 decision if (a) leaves no buffer.
5. **The smallest mechanisms** each new stage may propose in its application: A6 one generation helper; A7 one authoritative owner (form decided in its application); A8 one explicit project-state holder; A3 the artifact graph v0 (read-only).
6. **Block rules** (§0.4), in particular rule 6 (new orchestration outside `main_window.py` / `step0_page.py`).

### Rulings (user, 2026-09-30)

Items 1–6 **approved** with r3 ("完成修改后通过": apply the r2 review's three wording fixes, then approved; no further external review).

### Approval and consolidation workflow

This is document governance, not a stage; it adds no architecture scope.

1. The user approves this **amendment** v2.3. The status line becomes "APPROVED" and this section records the rulings.
2. After approval, a **self-contained consolidated v2.3** is produced: every v2.2 clause that stays in force is merged in full, so the document can be read without v2.2.
3. v2.2 is kept unchanged as the historical record.
4. The consolidated version is checked against amendment + v2.2 by a semantic / manual diff, confirming that it adds no rule, drops no clause that is in force and changes no existing meaning. **§5, §6 and §7 (the scientific contracts merged from v2.2) are checked line by line: no "harmonised wording" there.** The diff result is reported to the user.
5. The consolidated document then becomes the primary reading version for execution windows.

No consolidation is done before the user formally approves v2.3. Commits (of the amendment and of the consolidated version) happen only when the user authorises them.

---

## 19. Definition of success *(v2.2 §15)*

One pixel-source contract, one coordinate contract, one segmentation identity contract, one minimal object table, stable Step0/1/3 camera behaviour, and bounded resources — not every future subsystem implemented.

> Freeze the interfaces and scientific truth now; implement future consumers only when real TMA / Step5 workloads justify them.
