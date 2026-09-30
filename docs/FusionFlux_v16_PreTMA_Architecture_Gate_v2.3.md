# FusionFlux v16 — Pre-TMA Architecture Gate v2.3

**Status:** **v2.3 APPROVED (r3)** by the user on 2026-09-30 — the authoritative v16 architecture / pre-TMA plan. It supersedes v2.2 (`FusionFlux_v16_PreTMA_Architecture_Gate_v2.2.md`), which is kept as the historical record, like v2.1. The rulings are recorded in §18. This file is the approved **amendment**; the self-contained consolidated v2.3 follows the §18 workflow. Every stage still needs its own application + whitelist (P0 rules: `AGENTS.md`, `docs/P0_SCOPE_RULES.md`).
**Revisions:** r1 — first draft. r2 — after the independent review of r1 (2026-09-30), wording only: A7 rollback no longer satisfies gate 8 (E4 stays open unless the user grants an explicit waiver); `active_source_id` has one owner, ProjectState (A8), and A7 does not touch the source binding; `operates_on` (scope) and `depends_on` (lineage) are separated; A6's inventory is limited to public Step0–4 paths with a per-artifact completion / commit protocol; the approval → consolidation workflow is added (§18). Direction, the 8-week cap, E1–E5 and the stage order are unchanged. r3 — after the independent review of r2 (2026-09-30, "r2 can be approved; fix three wordings and mark APPROVED"), wording only: E4's active-source clause closes with §17.2 and gate 8 states that it does not test source ownership; §0.4 rule 6 exempts A6's generation checks inside existing callbacks; gate 9's test granularity matches §15.4; §16.2's renderer-state sentence rewritten. Approved by the user with r3.
**Basis:** the user's ruling of 2026-09-30 ("time does not matter as long as it is not half a year; 1–2 months is acceptable; reshape Block01 in v16, and enter TMA only after the critical hazards are solved"), and the Fable / ChatGPT review rounds recorded in `~/fusionflux/意见.txt`. Those reviews are **not authorisation** (`AGENTS.md` rule 2); this draft states what the user is asked to approve (§18).
**Line:** v16 = this architecture gate (now including the ownership and lifecycle convergence) + TMA Foundation + QualityMask (blur / fold / low tissue). v15 is frozen at `f93f195` (tag `v15-final`).
**Time budget:** **at most 8 weeks** from the approval of v2.3. It is a **cap, not a schedule to fill**: as soon as the exit conditions (§0.2) and the start gates (§9) pass, TMA Foundation starts.
**Hard stop:** as in v2.2 — no further architecture work between the last gate and TMA Foundation unless a new scientific-correctness problem appears.

> **Core principle of v16:** v16 restructures **ownership and contracts**; it does not restructure the verified **scientific implementation**.

**How to read this document.** v2.3 is written as an amendment. Every section of v2.2 that is not replaced or amended here **stays in force unchanged** as part of v2.3; that includes all execution records (A0, A0.5, A1, the mosaic), §5.1–§5.8 (PixelSource, the A2b-probe and its adoption rule), §6 (A3 / A4 schemas), §7 (A5), §8 (test data and disk policy), §11 (backlog) and §14 (version control). Section numbers below follow v2.2; new sections have new numbers.

---

## Changes from v2.2

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

## 0. Scope principle (amended)

v2.2 §0 (what is frozen before TMA) stays. Added:

### 0.1 Scope firewall (amended)

A newly found requirement enters this gate only if at least one holds:

- *(v2.2, unchanged)* TMA would otherwise create a second, incompatible data model; changing it later would require rewriting stored TMA results; it affects scientific correctness or coordinate identity; it stops a representative large image from running without OOM; it causes a visible geometric inconsistency between Step0, Step1 and Step3;
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

## 4. Stage A1 / A1b (amended)

The A1 record in v2.2 stands. **A1b** (one page frame for Step0–4) is part of v2.3's critical path:

- S0 design approved (`docs/v16_A1b_application.md`); S1 (StepFrame + Step1 / Step3) committed `c3b70b1`; S2 (Step0) application v2 approved (`docs/v16_A1b_S2_application.md`), in implementation at the time of this draft; S3 (Step2) and S4 (Step4) follow, each with its own application.
- A1b's F1–F4 frame-lock tests (`tests/test_v16_frame_lock.py`) join the monotonic green rule as soon as each stage lands.
- A1 + A1b together are the **baseline A7 is measured against**: A7 may not make any F1–F4 or zero-drift invariant worse.

### 4.x A1b execution record (2026-09-30) — DONE

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
- **Open:** the untracked consolidated v2.3 (`..._v2.3_consolidated.md`) predates this record and needs the same paragraph when it is committed.

---

## 6.8 A3 additions: schema version and artifact dependencies (new)

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

§6.7 acceptance gains:

- [ ] `project_schema_version` written; an unknown version is refused with an explicit error (test).
- [ ] For one real result, the graph answers which segmentation run, which `fused.zarr` and which corrected channels a Step4 h5ad used, through `depends_on` only.
- [ ] No `depends_on` list contains a `region_id` (test); the segmentation run's regions come from `operates_on`.

---

## 9. TMA start gates (amended: six → nine)

Gates 1–6 are unchanged from v2.2 §9. Added:

7. **Provenance (E3):** for the representative dataset, every artifact produced by the gate run has an `artifact_id` and `depends_on`; the artifact graph answers the Step4 → segmentation run → fused → corrected question through `depends_on` (artifact lineage only; the run's regions come from `operates_on`, which is not a graph edge); `project_schema_version` is written and an unknown version is refused.
8. **Viewer-state ownership (E4):** the authoritative camera is written only by user actions (pan, zoom, jump, Navigator jump) and explicit programmatic navigation; an automatic test logs every write during Step0 ↔ 1 ↔ 3 switches, resizes, source refresh to the same source, coarse / fine / mask arrival and channel enable, and asserts **zero write-backs from those events**. The A0 camera-log script (`scripts/diagnose_v16_a0_camera.py`) becomes this assertion. Real machine: 50 × Step0 ↔ 1 ↔ 3 without visible drift. **Gate 8 passes only when A7 passes.** If A7 is rolled back (§16.4), gate 8 does **not** pass and E4 stays **NOT PASS**; what happens next is the user's explicit ruling (§16.4: waiver A or no waiver B). Gate 8 tests camera and display-state ownership only; it does **not** test active-source ownership, which is covered by §17.3.
9. **Async lifecycle (E5):** for A6's inventory (§15.2 — public Step0–4 paths only), the controlled-delay tests of §15.4 (P0 rule 5: a normal asynchronous late arrival, not a hostile test) show that a result from the old slide / dataset / run arriving after the switch leaves the new state unchanged: **every row A6 changed** has its own test; rows only confirmed as already compliant are covered by **one test per completion / commit protocol type**. Each disk writer in the inventory writes to a run-scoped destination with its explicit completion / commit protocol (E5 (b)); a writer to a shared target also has the current-run check.

**TMA starts when gates 1–9 pass**, or when gates 1–7 and 9 pass and the user has granted the explicit E4 waiver for gate 8 (§16.4 A). No other gate can be waived by this plan.

---

## 11. Deferred backlog (amended)

v2.2 §11 stands. Added as **v17 candidates**, to be designed with real TMA workloads:

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

## 12. Schedule (replaced)

Estimates are working days. The weeks are an **order and a budget**; if the exit conditions pass early, TMA starts early. Real-engine regressions run module by module (memory `wsl-disk-and-memory`), so about a quarter of each week is regression and real-machine time — the estimates below include it.

| Week | Stage | Est. | Why here |
|---|---|---|---|
| 1 | **A1b** S2 (Step0) → S3 (Step2) → S4 (Step4) | ~4.5 d | In progress; A7 needs a baseline that does not move |
| 1–2 | **A2a** PixelSource contract + `OmeTiffSource` adapters, zero behaviour change | ~3 d | Data plane first; A8's `active_source_id` must name a source defined by the PixelSource contract |
| 2 | **A2b-probe** (≤ 2 d, v2.2 §5.6) → decision §5.7 (a) / (b) | ≤ 2 d | Unchanged rule and time box |
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

**Progress** (a record, appended): A1b done 2026-09-30 (§4.x). A2a done 2026-09-30 — `core/pixel_source.py` + `sources/` (`docs/v16_A2a_application.md` §10).

**Off the critical path** (unchanged): v2.2 §5.8 corrected coarse levels — opportunistic, own application, never delays a gate.

**Before each week starts**, the acceptance conditions of the blocks of that week are in their approved applications (§0.4 rule 1).

---

## 13. Stop rules (amended)

1. ~~If zero-drift is fixed locally, no global camera framework.~~ **Void in v2.3.** Replaced by rule 8.
2. *(unchanged)* If NGFF requires rewriting scientific algorithms, stop and redesign the adapter boundary.
3. *(unchanged)* Anything needed only for a hypothetical 10 M-cell Step5 goes to the backlog.
4. *(amended)* An optimisation or restructuring that serves none of scientific correctness, TMA compatibility, OOM safety, the five exit conditions or the nine gates cannot extend the schedule.
5. *(replaced)* **The 8-week cap is a decision point.** If gates 1–9 pass earlier (or gates 1–7 and 9 with the explicit E4 waiver, §9), TMA starts then; "optimising v16 further" is not a reason to stay. If they have not passed at the cap, the user decides what is dropped or deferred; the cap is not silently extended.
6. *(unchanged)* After two review rounds with no new public-path defect, hardening stops (P0 rule).
7. *(amended wording)* **A2b-probe:** stops at 2 working days whatever it has measured, or early with §5.7 (b) when Step4 end to end stays at about 1.5× TIFF or worse. Nothing in the scientific backend is changed to pass it. (The v2.2 phrase "uses the day-15 buffer" is replaced by the 8-week cap.)
8. *(new)* **A7 rollback gate** (§16.4): if at the end of A7's week the real-machine 50-switch test drifts, or the zero-write-back assertion fails and cannot be fixed within the block, A7 is reverted to the A1 + A1b state and the plan continues with A8. The rollback is a safety mechanism, not a failure of the plan — but it does **not** complete E4 or gate 8; they stay open until the user rules on a waiver (§16.4).
9. *(new)* **No new stage without a plan revision.** A stage not in §12 enters v16 only through a v2.x revision approved by the user; a review comment or a block application cannot add one.

---

## 15. Stage A6 — Async lifecycle (new)

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

## 16. Stage A7 — Viewer-state ownership (new)

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

## 17. Stage A8 — Project-state ownership cleanup + A5 resource tiers (new)

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
