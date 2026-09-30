# FusionFlux v16 — Pre-TMA Architecture Gate v2.3.1 (A2b-probe revision)

**Status:** **APPROVED** by the user on 2026-09-30 (r1; §5 items 1–6 all as recommended). In force from its commit.
**Revises:** v2.3 (amendment `2cb496c`, APPROVED 2026-09-30) and, through it, v2.2 §5.6. Only the A2b-probe clauses listed below change; **every other clause of v2.3 / v2.2 stays in force unchanged**. v2.3 §13 rule 9 requires a plan revision for this: the time box and the early-stop line are approved plan text, not block-application text.
**Basis:** the user's ruling of 2026-09-30 — the probe must compare the two formats **each with a reader adapted to its own layout**, because Step4's read path is already tuned to TIFF and a generic NGFF reader would measure the mismatch, not the format — and Fable's review of that ruling (the IO-layer / science-layer split, a bounded optimisation budget, the early-stop line judged after the adapted reader, viewer and Step4 both as pre-registered criteria). The review is not authorisation (`AGENTS.md` rule 2); §5 states what the user is asked to approve.

---

## 1. The question the probe answers (clarified)

Not "which format is faster in theory", but: **is switching the canonical raw store to NGFF before TMA worth it**, when both formats get a fair, bounded, format-adapted IO layer and the scientific computation is identical?

- **IO layer — adapted per format, may be optimised:** traversal order aligned to the format's own blocks (NGFF chunks, TIFF tiles / strips), parallel decode, read-ahead, a block-level cache, and (NGFF only) the chunk shape and codec chosen at ingest.
- **Science layer — identical, never touched:** Step4's mathematics, QuantBackend / QuantFinalizer, the S4-3 logic, N2 / N3, the segmentation engines. Both readers hand the same pixel arrays to the same code. v2.3 §13 rules 2 and 7 (no scientific change to pass a benchmark) are unchanged.

## 2. Changes

| # | Clause | v2.3 / v2.2 text | v2.3.1 |
|---|---|---|---|
| 1 | v2.2 §5.6 title, §5.6.1 time box; v2.3 §12 row "A2b-probe" | ≤ 2 working days | **≤ 4 working days** (ruling 2). The extra ≤ 2 days come out of the week-8 buffer; the 8-week cap (v2.3 §13 rule 5) is unchanged |
| 2 | v2.2 §5.6.1 / §5.6.2: what may vary | reader, chunk scheduling, on-disk layout; readers = generic zarr vs one "parallel chunk scan + read-ahead" reader | A **pre-registered IO optimisation list** (§3), applied to NGFF, and — ruling 3 — the same classes applied to the TIFF side where the format allows |
| 3 | v2.3 §13 rule 7 and v2.2 §5.6.4: early stop | stop early with §5.7 (b) when Step4 end to end stays ≈ 1.5× TIFF or worse | The 1.5× early-stop line is judged **only after the adapted NGFF reader is complete** (checkpoint C1, §4), on its best layout — never on the generic first version |
| 4 | v2.2 §5.6.4: adoption rule | six rows; viewer rows 1–2, Step4 end to end row 3 | **Rows unchanged**; the comparison baseline of every row is made explicit (§4): NGFF's best adapted configuration vs **the best of** (TIFF product path, TIFF with the same IO classes). Thresholds 1, 3, 5 are still the user's to confirm in the probe's application |
| 5 | v2.3 §12 pre-written branch | probe in week 2 | probe in weeks 2–3; branch (a) and (b) otherwise as written, shifted by at most 2 days out of the buffer |

Unchanged and restated for clarity: no Rust; no new packages without approval (zarr 2.18, numcodecs, imagecodecs as installed); scripts and scratch copies only, no product code; the 2×2 mosaic is not enlarged; **oracle unchanged** — NGFF pixels bitwise equal to TIFF, and the Step4 scientific payload bitwise identical (v2.2 §5.6.3); one NGFF copy on disk at a time, deleted after its runs; disk policy v2.2 §8.

## 3. Pre-registered IO optimisation list (closed)

Only these; anything else needs the user's ruling during the probe.

1. **Block-aligned traversal** — the scan visits whole NGFF chunks (TIFF: whole tiles / strips) in storage order; no request straddles blocks it does not need.
2. **Parallel decode** — compressed block bytes read directly, decoded on a thread pool (size fixed per run and recorded).
3. **Read-ahead** — the next scan tile's blocks are fetched while the current one is consumed (depth fixed per run, recorded).
4. **Block-level cache** — a bounded LRU of decoded blocks, sized to the scan's working set (size recorded; counts toward peak RSS).
5. **Layout (NGFF only)** — level-0 chunk (1, 512, 512), (1, 1024, 1024), (1, 2048, 2048) × Blosc-lz4 / Blosc-zstd clevel 5 (v2.2 §5.6.2's six layouts); levels ≥ 1 stay (1, 512, 512).

A viewer-side counterpart uses items 2 and 4 for random ROI / pan reads (the viewer already has its own scheduler; the probe only swaps the source reads in a script, as v2.2 §5.6.3).

## 4. Procedure and checkpoints

- **C0 (≈ day 1):** generic readers on both formats + the adapted NGFF reader written; oracle checks green (pixels bitwise, Step4 payload bitwise on `cropped_region`).
- **C1 (end of day 2 at the latest):** the six layouts measured with the adapted reader on `cropped_region`. **The 1.5× early-stop line is applied here**, on the best layout: if Step4 end to end (cold and warm medians) is still ≥ 1.5× the TIFF baseline, stop → §5.7 (b).
- **C2 (days 3–4):** the TIFF side with the same IO classes (ruling 3); the best one or two NGFF layouts confirmed on the synthetic mosaic (one copy at a time); the full adoption rule (v2.2 §5.6.4) applied row by row.
- **Hard stop at 4 working days** whatever has been measured; the decision uses what exists.
- **Baseline of each row:** NGFF best adapted vs **min(TIFF product path, TIFF with the same IO classes)**. Both TIFF numbers are reported. If the TIFF optimisations beat the product path, that is recorded as a backlog item (a possible TIFF reader improvement), not implemented in v16.
- **Deliverable:** as v2.2 §5.6.5, plus a table of which optimisation classes were used on each side and their parameters.

## 5. Items for the user's ruling

1. **Approve v2.3.1** as a revision of the A2b-probe clauses only (§2); everything else in v2.3 / v2.2 unchanged.
2. **Time box:** ≤ 4 working days (Fable's suggestion 3–4). *Recommendation: 4, with the checkpoints of §4 — the 1.5× line at C1 still ends the probe early when NGFF is clearly behind.*
3. **Symmetric fairness:** apply the same IO classes (block-aligned traversal, parallel decode, read-ahead, block cache) to the TIFF side too, and compare NGFF against the better TIFF number. *Recommendation: yes.* Reason: otherwise a gain from parallel decode would be credited to NGFF although TIFF gets it too; the decision question is whether to switch the format, and a switch is only worth it for what the TIFF path cannot also get. Cost: about one day, inside the 4.
4. **The closed optimisation list of §3** (and that anything outside it needs a ruling during the probe). *Recommendation: approve.*
5. **Viewer and Step4 both decide:** the adoption rule's rows are unchanged (viewer rows 1–2, Step4 row 3, fusion row 4, disk row 5, science row 6); thresholds 1, 3 and 5 are confirmed in the probe's own application, as v2.2 already says. *Recommendation: approve.*
6. **Commit** this revision after approval, together with the A1b execution record appended to v2.3 §4; the untracked consolidated v2.3 then needs the A1b record and a pointer to v2.3.1 before it is committed (v2.3 §18 workflow).

### Rulings (user, 2026-09-30)

Items 1–6 **approved as recommended**: time box ≤ 4 working days with checkpoints C0–C2; symmetric IO classes on the TIFF side, NGFF compared with the better TIFF number; the closed optimisation list of §3; the adoption rule's rows unchanged, thresholds 1 / 3 / 5 confirmed in the probe's application; commit and push together with the A1b execution record.
