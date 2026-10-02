# FusionFlux v16 — Pre-TMA Architecture Gate v2.4 (amendment)

**Status:** **v2.4 APPROVED** by the user on 2026-09-30 (r3; §18 items 0–5 all as recommended, cap option (α)). It amends the tracked base **consolidated v2.3 + v2.3.1** (`FusionFlux_v16_PreTMA_Architecture_Gate_v2.3_consolidated.md`, consolidation r2). From its commit, v2.4 + that base are the authoritative plan.
**Revises:** the consolidated v2.3 + v2.3.1 (consolidation r2). Only the items listed in "Changes from v2.3" change. **A2–A8, E1–E5, the block rules (§0.4) and the stop rules 1–9 (§13) are not touched.**
**Basis:** the user's instructions of 2026-09-30 ("A2b-probe 裁定落地、v2.4 修订草案、A2c 申请", and the supplement "v2.4 草案修正", which overrides items 1, 4 and 6 and the schedule of the first). Where v2.4 touches an approved v2.3 clause it does so only through the rulings recorded in §21 and the amended sections below (§0.3, §7.4, §9).

**How to read this document.** v2.4 is an amendment, written like v2.3. Every section of v2.3 (consolidated) and v2.3.1 that is not replaced or amended here stays in force unchanged. Section numbers follow the consolidated v2.3; new sections get new numbers (§20 A9, §21 conflicts).

### Revision history

- **r1** (2026-09-30): first draft — A9 as eight mechanisms with thresholds V1–V8.
- **r3** (2026-09-30), after the independent review of r2 ("conditionally approved"; three must-fix items, three recommended, plus C4 / C6 / C8), wording and structure only:
  1. **A9 stays behind `PixelSource`.** The probe's direct TIFF decoder becomes the implementation of `OmeTiffSource.read_native_tile()`, and the viewer reads through `PixelSource`. It never opens the TIFF itself (§20.4 item 1, §5.1a P1).
  2. **A9's thresholds are locked before any code changes**: read-only baseline profiling → the user sets the final thresholds → locked → implementation. The baseline may inform the thresholds; optimisation results may not (§20.3).
  3. **Coarse residency is active channels first**: the displayed channels' coarse frame is needed for A9-1; the other channels' coarse levels are warmed in the background; the "never black on any channel switch" guarantee holds once that warm-up has completed (§20.3).
  4. A9-2 is judged on a **tile coverage / residency mask**, not on black pixels.
  5. **Both mosaics**: the existing 3-level 2×2 mosaic (the product's structure) and a new 7-level 2×2 mosaic (deep-pyramid stress) (§20.3, §21 C5).
  6. P4 narrowed: FusionFlux's coordinate model can be represented losslessly by OME-NGFF / SpatialData transforms; not the reverse for arbitrary transforms (§5.1a).
  - A9-4 reworded: no perceptible long GUI-thread stall; no single blocking section > 32 ms (32 ms is not called a frame).
  - C4: gate 10 recommended; C6: two self-consistent cap options instead of "≈ 9 weeks"; C8: consolidating the base is a prerequisite of approving v2.4 (§21, §18).
- **r2** (2026-09-30), per the user's supplement: A9 becomes a **performance-gated viewer convergence stage** (done when its acceptance passes, independent of how many mechanisms were built; it stops the moment acceptance passes); the eight mechanisms become an approved **toolbox**, not a to-do list, applied cheapest first; new acceptance list and estimate (5–8 development days + 1–2 real-machine days); contract patch P1 renamed format-neutral (`native_tile_shape` / `read_native_tile`) and made A2c's first commit; P4 limited to a semantic mapping (no ome-zarr / spatialdata dependency, no conversion code); suggested cap ≈ 9 weeks; Rust-gate and firewall wording replaced.

---

## Changes from v2.3

| # | Change | Where | Why |
|---|---|---|---|
| 1 | A2b-probe outcome recorded: **(b)**, ruled by the user 2026-09-30. OME-TIFF stays the canonical raw store; zarr / NGFF for derived rasters only. A2b (`NgffSource` / ingest / `NgffScanReader`) is not built; no further NGFF testing, rescue or parameter search | §5.7, §12 | The pre-registered rule applied row by row (`docs/v16_A2b_probe_report.md`) |
| 2 | New stage **A9 — performance-gated viewer convergence**, after A8 and before the full regression and gates. Done = its UX / resource acceptance passes; it stops as soon as it does. Its thresholds are **locked before implementation** after read-only baseline profiling; every TIFF optimisation stays **behind `PixelSource`** | §20, §12 | The probe showed a ~2× read margin in the TIFF viewer path; TMA browsing needs a viewer whose responsiveness and memory are measured, not assumed; A2 must not be split again by a second viewer-only reader |
| 3 | §0.1 scope firewall gains: **"TMA core browsing requires passing A9's UX / resource acceptance gate"** | §0.1 | Otherwise A9 fails the firewall. The entry names the gate, not a list of mechanisms |
| 4 | New stop rule 10, **the Rust gate** | §13 | A small Rust module only for a profiled hotspot in the Python scheduling / IO path that numpy, numba and the existing architecture cannot solve |
| 5 | **Contract patches** P1–P4 carried by the A2c and A3 applications (not a stage); P1 is format-neutral and is A2c's first commit | §5.1a | A9 and TMA need storage-native tile reads, a raw-source descriptor, derived coarse levels in the artifact graph, and a SpatialData-compatible transform convention |
| 6 | §11 backlog: `export_to_spatialdata()` (after TMA) | §11 | Interoperability without changing the canonical stores |
| 7 | §12 schedule: A9 row inserted after A8, with an A9 hard cap; the overall cap is **the user's to set** from two self-consistent options (§12) | §12 | A9 is new work; stop rule 5 forbids extending the cap silently |
| 8 | §0.3 amended: Rust only through stop rule 10; GPU shaders may change for **display only** | §0.3 | Rulings C1, C2 |
| 9 | §7.4 amended: A9 toolbox items 8–9 are the only exception to "no adaptive tile scheduler", used only if the cheaper items cannot pass A9's gate | §7.4 | Ruling C3 |
| 10 | §9 gains **gate 10 = A9's UX / resource acceptance**; "gates 1–9" becomes "gates 1–10" | §9 | Ruling C4 |
| 11 | §18 rulings | §18 | — |

---

## 0.1 Scope firewall (amended)

Add, after the v2.3 entry for the five exit conditions:

- *(new, v2.4)* **TMA core browsing requires passing A9's UX / resource acceptance gate (§20.3).** It does not require implementing any particular mechanism.

## 0.3 Not done in v16 (amended)

- The entry "**Rust**" reads: *Rust, except one small module applied for through stop rule 10 (the Rust gate).*
- The entry "changing … **GPU shaders** or other verified scientific components" gains: *except a display-only change made by A9 (§20): display composition, intensity, contrast and gamma; never scientific pixel values, segmentation or quantification. The A9 inventory first establishes whether today's shaders already do this.*

## 7.4 Deferred (amended)

- The entry "an adaptive tile scheduler" gains: *except A9's toolbox items 8–9 (§20.4), implemented only if the cheaper toolbox items cannot pass A9's gate.*

## 9. TMA start gates (amended: nine → ten)

Add:

10. **Viewer engine (A9):** A9's UX / resource acceptance (§20.3), with the thresholds locked in the approved A9 application, passes on both test mosaics.

Wherever the consolidated plan says "gates 1–9" (§9, §12, §13 rules 4 and 5, §0.1), read **"gates 1–10"**; the E4 waiver clause reads "gates 1–7, 9 and 10 with the explicit E4 waiver for gate 8".

## 5.1a Contract patches (new; written into the A2c and A3 applications, not a stage)

| # | Patch | Carried by | Note |
|---|---|---|---|
| P1 | **Format-neutral native tiles.** `PixelSource` gains `native_tile_shape(level) -> (h, w)` and `read_native_tile(channel, level, tile_y, tile_x)`, which returns exactly one block of the source's own storage grid (edge blocks clipped). OME-TIFF maps it to the TIFF tile, zarr to the chunk. **In A9 the probe's direct TIFF decoder becomes `OmeTiffSource.read_native_tile()`'s implementation**, behind the contract | **A2c, as its own first commit**; A2a's bitwise invariance is shown still green before Step4 and fusion are migrated | A2a (`2a0a3e8`) has an optional `native_chunk_shape()` hint with no level argument and no caller. P1 replaces it with `native_tile_shape(level)` and adds one method; the A2c application states the effect on A2a's tests |
| P2 | The raw-source description gains `kind` (today only `"ome_tiff"`), the pyramid **level count** and the **coarsest level shape** | A3 (persisted in the project / provenance); the level count and shapes already exist on `PixelSource` | — |
| P3 | Derived coarse levels (§5.8) are written into `provenance.json` and enter `depends_on` | A3 (and §5.8's own application) | Keeps §5.8 off the critical path |
| P4 | **FusionFlux's coordinate model can be represented losslessly** by the corresponding OME-NGFF `axes` / `scale` and SpatialData coordinate transformations; the A3 application gives the mapping table. It does **not** claim that an arbitrary OME-NGFF / SpatialData transform maps back into FusionFlux | A3 | **Semantic correspondence only**: no ome-zarr / spatialdata dependency and no conversion code |

## 11. Deferred backlog (amended)

Add:

- **`export_to_spatialdata()`**, after TMA: images = the corrected / fused zarr; labels = the segmentation masks; tables = the cell table; shapes = region / core polygons. It supersedes the v2.2 entry "a SpatialData adapter" (Data / query).

The first instruction's "remove the TIFF optimised-reader entry from the backlog (now in A9)" has **nothing to remove**: the probe report proposed that entry (§6 item 4) but it was never added to §11. A9's toolbox (§20.4) now covers it.

## 12. Schedule (amended)

Insert after the A8 row (week numbers of the (b) branch):

| Week | Stage | Est. | Real machine (separate) | Why here |
|---|---|---|---|---|
| 7 | **A9** performance-gated viewer convergence (§20) | 5–8 development working days (**hard cap 8**) | 1–2 days of baseline profiling and acceptance (**hard cap 2**) | After A6 (the stale rejection it reuses) and A7 (the camera owner it must not write); before the full regression, so the gates see the final viewer |

**Progress** (a record, appended): **A2c done 2026-10-01** — `690ba08` (native-tile patch P1), `8da231c` (Step4 → PixelSource), `028480d` (FullFusionWorker → PixelSource); bitwise equal to the pre-migration path on test1, Step4 0.98×, no new regression failures; the two A2c blockers are resolved for the fusion path (`docs/v16_A2c_application.md` §11). Real-machine check passed 2026-10-01 (GUI fusion and Step4 bitwise equal to the pre-A2c code on the same settings). **A3 + A4 automatic acceptance passed 2026-10-01** — `07f57a7`, `f55be0f`, `6e4616c` (identity / coordinates / provenance / artifact graph v0), `eb1a575` (pyarrow), `d000579` (object layer v1); Step4 and fusion bitwise unchanged on test1, Step4 1.01×, no new regression failures (`docs/v16_A3A4_application.md` §13). Real-machine check passed 2026-10-01 (GUI Step0 → fusion with a corrected channel → Step2 → Step4; the artifact graph answers the Step4 lineage through `depends_on`; Step4 bytewise equal to the pre-A3 code; object tables from the result).

**User-inserted blocks** (ruled by the user 2026-10-01; not stages of this plan, recorded here instead of a plan revision, stop rule 9). They run after A3 + A4 and before A6, in this order:

| Block | What | Application | State |
|---|---|---|---|
| **S2T** | Step2 tile-status display fix; opt-in skipping of tiles without tissue; sharper Step2 overview | `docs/v16_Step2_tiles_application.md` (v2, approved) | **done 2026-10-02** — `4784401`, `935d809`; no new regression failures; real-machine check passed (`docs/v16_Step2_tiles_application.md` §11) |
| **S0P** | Step0 background correction (tophat / cucim) in parallel on the CPU; one backend per channel; backend in the attrs and the incremental-reuse signature | `docs/v16_Step0_cpu_parallel_application.md` (v2; rulings 3 / 4 after the §2.4 benchmark) | application |
| **A6 (extended)** | A6 of §15 (async lifecycle, gaps G1–G5) **plus** the user's workspace rules W1–W5: open an existing project's workspace (chooser when several), Save overwrites (warn when the geometry changed), Save as, "made with older settings" / "provenance unknown" marks, fusion staged while Step2 runs | `docs/v16_A6_application.md` (v1) | application; **estimate 5–6 working days instead of ~3, confirmed by the user 2026-10-02** (stop rule 5: the cap is not extended silently — recorded here) |
| S0G *(later, needs a GPU machine)* | GPU tophat with the same disk footprint as the CPU path | — | backlog |

The rows after it move by the time A9 actually takes. If A9 passes its gate early, it stops and the rest moves up.

**Cap (ruled, option (α)):** A9 hard cap = **8 development working days + 2 real-machine days**; the overall v16 cap = **10 weeks** from the approval of v2.3, as the upper decision point, with the aim of finishing clearly earlier. v2.3 §13 rule 5 still applies: TMA starts as soon as gates 1–10 pass; the cap is not extended silently. If A9 passes its gate early, it stops and the rest moves up.

## 13. Stop rules (amended)

Add:

10. *(new, v2.4)* **Rust gate.** Only when A9's profiling shows a **single hotspot in the Python scheduling / IO path** that numpy, numba and the existing architecture all cannot solve may **one small Rust module** be applied for, with a bitwise oracle against the Python path and a single-block rollback. *(See §21 C1: §0.3 lists Rust as "not done in v16".)*

---

## 20. Stage A9 — Performance-gated viewer convergence (new)

### 20.1 What A9 is

A9 is a **performance-gated convergence stage, not a feature stage.** It is **done when the acceptance in §20.3 passes**, whatever number of mechanisms that took, and it **stops as soon as the acceptance passes**. It is not a viewer rewrite.

### 20.2 Starting point

- The A2b-probe (`docs/v16_A2b_probe_report.md` §6 item 4) measured a tile-aligned direct TIFF decoder, with the same budget as the product (8 threads, 512 MiB block cache), against the product's `RawTileProvider` path:
  - cropped_region: ROI 2048² 14.8 → 7.4 ms; channel switch 64 → 29 ms; seq29 1166 → 675 ms;
  - mosaic: ROI 2048² 11.5 → 9.1 ms; channel switch 47.9 → 27.6 ms.
  
  It is probe code (`scripts/probe_v16_a2b_ngff.py`, `TifAdapted`), bitwise equal to the product reader (0 mismatches in 120 comparisons per run).
- Behaviour reference: Odon's tile engine (github.com/alexcoulton/odon), as a **design reference** only. No Odon code is copied unless its licence allows it and the A9 application names it.

### 20.3 Acceptance (the gate) — locked before implementation

**Order of work** (v2.3 §0.4 rule 1, pre-registered acceptance):

1. The A9 application is written with the criteria below and **proposed** thresholds.
2. **Read-only baseline profiling** of today's viewer on both test mosaics (below), with no code change: the measuring tools only.
3. The user sets the **final thresholds** from the proposals and the baseline. They are then **locked** in the approved application.
4. Implementation starts. The baseline may inform the thresholds; **results of the optimisation never do**. A threshold is not relaxed after the fact.

**Test data** (both, §21 C5): the existing **3-level 2×2 mosaic** (the product's real pyramid structure) and a new **7-level 2×2 mosaic** (deep-pyramid stress for the scheduler and caches). The viewer must be correct and within the thresholds on both; cropped_region is used for real tissue content.

| # | Criterion | Proposed threshold (locked in step 3) | How measured |
|---|---|---|---|
| A9-1 | **First usable coarse frame**: from opening a slide to the viewport covered by a coarse level of the **displayed (active) channels** | p95 ≤ **300 ms** | Open command → first frame whose viewport is fully covered, per the coverage mask of A9-2 |
| A9-2 | **No missing tile** in the visible viewport during navigation, once coarse coverage is established | **0** frames with a gap | A **tile coverage / residency mask**: a frame has a gap if any viewport pixel is covered by no resident tile of any level. Pixel colour is never used (real tissue can be black) |
| A9-3 | **Target level in place** after a drag / zoom stops, on an opened slide | p95 ≤ **150 ms** | Last input event → the coverage mask shows every visible tile at the target level |
| A9-4 | **No perceptible long GUI-thread stall**: no single blocking section on the GUI thread | > **32 ms** never (to be tightened if profiling shows < 16 ms is easy) | GUI watchdog / perf trace during the scripted runs |
| A9-5 | **Intensity / contrast / gamma** changes read nothing from disk | **0** reads (assertion) | Reader read counter |
| A9-6 | **Channel switch**: if the target channel's tiles for the current viewport are resident, **0** disk reads; otherwise the load is asynchronous and the GUI does not wait. **Once the coarse warm-up has completed**, the target channel's coarse level is always visible, so there is no gap | 0 reads when resident; 0 GUI waits; after warm-up, 0 frames with a gap | Read counter + A9-2's mask + A9-4's trace |
| A9-7 | **Resources, 12 GB tier**: a synthetic WSI of ≥ 30 k × 30 k, 29 channels, **≥ 7 pyramid levels** (the 7-level mosaic), and the 3-level mosaic | No OOM; every cache has a declared upper bound and stays within it | A5's recording procedure (RSS, commit, page file) |
| A9-8 | **No camera drift**: 50 × drag / zoom / channel switch | A1 zero-drift and A7 zero-write-back invariants still pass | Existing tests + real machine |

**Coarse residency** (part of the gate, not a separate mechanism): on opening a slide, the **active channels' coarse frame comes first** (A9-1); the **other channels' coarsest levels are warmed in the background** without delaying the first frame; when that warm-up has completed, every channel's coarsest level is resident, and A9-6's "never a gap on any channel switch" guarantee applies from then on. The warm-up's time to complete is recorded (not gated) so that high channel counts (60–80) stay visible in the data.

**Data path**: every pixel the viewer shows comes through `PixelSource` (`OmeTiffSource` for raw, `CorrectedZarrSource` / derived sources otherwise). Science is untouched throughout: pixels bitwise equal to the old reader at every level (the old path is the oracle, §0.4 rule 2).

### 20.4 Toolbox (approved for use; not a list that must be done)

Applied **cheapest first — do, measure, stop when the gate passes**. The A9 application orders them by expected cost after inventorying what today's viewer already has.

1. The **direct TIFF tile decoder** (validated in the probe), as the implementation of **`OmeTiffSource.read_native_tile()`**: the optimisation sits behind `PixelSource`; the viewer never opens the TIFF itself. This requires the viewer to read through `PixelSource` — the viewer-consumer migration that v2.2 §5.2 says needs its own P0 approval — and changes the viewer read path, so both approvals (`AGENTS.md` rule 5) are given in the A9 application.
2. **Coarsest level drawn underneath.**
3. **Coarse levels resident**, with memory accounting.
4. A **native-dtype decoded tile cache** (uint8 / uint16 as stored); the **GPU shader** does intensity and compositing.
5. **Viewport halo prefetch.**
6. Optional **warming of the next finer level.**
7. A **level floor when zooming out.**
8. **Centre-first ordering + active-keys expiry and cancellation**, reusing A6's generation.
9. **Adaptive degradation for high channel counts.**
10. **Main thread never waits on IO.**

### 20.5 Not done in A9

Changing the file format; changing N2 / N3 / Step4 mathematics; Rust (see stop rule 10); splitting `ui/main_window.py` / `ui/step0/step0_page.py`; **any viewer rewrite**. New code lives in its own modules (§0.4 rule 6); the two large files get only wiring lines.

### 20.6 Estimate

**5–8 development working days (hard cap 8) + 1–2 days of real-machine baseline profiling and acceptance (hard cap 2)**, not the best case (cap option (α), §12). The inventory, the measuring tools (coverage mask, read counters, stall trace) and the baseline profiling come first; the toolbox items are added one at a time until the gate passes.

### 20.7 Rollback

A9 lands as revertible blocks (§0.4 rule 3). A block that fails its acceptance and cannot be fixed within it is reverted to the A8 state; A1–A8 are not undone.

---

## 21. Conflicts with approved v2.3 clauses (ruled 2026-09-30, §18; applied in §0.3, §7.4, §9, §12)

| # | Approved clause | Conflict | Options |
|---|---|---|---|
| C1 | §0.3 "Not done in v16": **Rust**; §11 backlog "Rust: viewer IO; a PixelSource backend…" | Stop rule 10 lets one small Rust module be applied for inside v16 (after A9's profiling) | (i) amend §0.3 to "Rust only through stop rule 10"; (ii) keep §0.3 and make the Rust gate a v17 rule |
| C2 | §0.3: "changing … **GPU shaders** or other verified scientific components" | Toolbox item 4 has the GPU shader do intensity and compositing | If the A9 inventory finds today's shaders already do this, no conflict. Otherwise a **display-only exception**: display composition, intensity, contrast and gamma only; never scientific pixel values, segmentation or quantification |
| C3 | §7.4 Deferred: "**an adaptive tile scheduler**" | Toolbox items 8–9 (centre-first ordering with cancellation, adaptive degradation) are scheduler changes | (i) amend §7.4 to "except A9's toolbox items 8–9, implemented only if the cheaper items cannot pass the gate"; (ii) remove 8–9 from the toolbox |
| C4 | §13 stop rule 4: an optimisation that serves none of correctness, TMA compatibility, OOM safety, E1–E5 or gates 1–9 **cannot extend the schedule** | A9 extends the schedule and the §0.1 entry makes it a TMA prerequisite, but A9 has **no numbered gate**: the logic does not close | (i) add **gate 10 = A9's UX / resource acceptance (§20.3)** to §9 as a TMA start condition, and "gates 1–10" wherever v2.3 says "gates 1–9"; (ii) keep A9 outside the gates (then rule 4 argues against extending the cap for it) |
| C5 | §8: the representative mosaic has a **×4 pyramid with 3 levels**; larger mosaics (3×3 / 4×4) only with an external SSD | A9-7 asks for **≥ 7 levels** at ≥ 30 k × 30 k | **Keep both** (r3): the existing 3-level 2×2 mosaic stays the representative product case; a second 2×2 mosaic with a ×2 pyramid (≥ 7 levels, about +1 GB; `make_synthetic_mosaic.py` gains a pyramid-factor option in A9's application) is the deep-pyramid stress case. It is still 2×2, so §8's 3×3 / 4×4 rule is not touched |
| C6 | §13 stop rule 5: the cap is not extended silently | r2's "≈ 9 weeks" counted at most 7 effective days for A9 while the estimate says up to 8 + 2 | The user chooses **(α)** A9 ≤ 8 development + 2 real-machine days, overall cap 10 weeks, or **(β)** A9 ≤ 7 effective days in total, overall ≈ 9 weeks (§12). Only the chosen one is written into the approved text |
| C7 | v2.2 §5.6.6 (kept in v2.3): Odon "never gates TMA or the adoption rule" | A9 uses Odon as the behaviour reference | No real conflict (a design reference, not a gate); licence checked before any code reuse |
| C8 | Document state | The consolidated v2.3 is **untracked** (never committed) and predates v2.3.1 (it still says A2b-probe ≤ 2 days in §5.6 / §12 / §13 rule 7) and the A1b / A2a / A2b-probe execution records in the amendment | **Prerequisite of approving v2.4** (r3): first a complete, tracked base — consolidated v2.3 + v2.3.1 + the A1b / A2a / A2b-probe records, checked by the v2.3 §18 diff procedure and committed — then v2.4 is an amendment of that base. The consolidated file was produced by another window; merging into it needs the user's go-ahead |

---

## 18. Items for the user's ruling (v2.4)

0. **Prerequisite (C8):** authorise building the tracked base (consolidated v2.3 + v2.3.1 + execution records, diff-checked, committed) before v2.4 is approved.
1. **Approve v2.4** as an amendment of that base: A9 (§20), the §0.1 entry, stop rule 10, the contract patches P1–P4 (§5.1a), the §11 entry and the §12 row.
2. **A9's procedure and criteria** (§20.3): criteria A9-1 … A9-8, coarse residency (active channels first, background warm-up), both mosaics, and the rule that the **final thresholds are set by the user after the read-only baseline and locked before implementation**. The numbers in §20.3 are proposals for that step, not approved values.
3. **The cap** (§12): (α) A9 ≤ 8 + 2 days, overall 10 weeks; or (β) A9 ≤ 7 effective days, overall ≈ 9 weeks. Recommended (α).
4. **Conflicts C1–C7** (§21), one ruling each. Recommended: C1 (i), Rust only through the Rust gate; C2 display-only exception if needed; C3 (i), items 8–9 only if the cheaper items cannot pass; **C4 (i), gate 10**; C5 both mosaics; C6 with item 3; C7 no change.
5. **Odon licence**: A9 reimplements; any code reuse is named in the A9 application with its licence.

### Rulings (user, 2026-09-30)

"全部按建议，选α，授权建立基准版本并提交":

- **0** — the tracked base was built (consolidated v2.3 + v2.3.1 + execution records, consolidation r2, diff-checked) and committed with this amendment.
- **1** — v2.4 approved (r3).
- **2** — A9's procedure and criteria approved: read-only baseline first, final thresholds set by the user and locked before implementation; the §20.3 numbers stay proposals until then.
- **3** — cap option **(α)**: A9 ≤ 8 + 2 days, overall 10 weeks (§12).
- **4** — C1 (i) (§0.3 amended); C2 display-only exception (§0.3 amended); C3 (i) (§7.4 amended); **C4 (i), gate 10** (§9 amended); C5 both mosaics; C6 = (α); C7 no change.
- **5** — Odon is a design reference; any code reuse is named with its licence in the A9 application.
