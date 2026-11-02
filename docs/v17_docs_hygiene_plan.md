# v17 Documentation refresh and repository hygiene (2026-11-01)

Source: whole-code review 2026-10-31 — codex finding 8 (P3: module
descriptions no longer describe current ownership) and Fable M3 (P1: the UI
spec and the user guide are stale) for item 1; Fable M7 (P3: repository
hygiene) for item 2. User (TG 2026-11-01): items 1 and 2 are low risk —
"直接做" (do them directly); review by codex as usual; nothing is committed
until the user is back (the install item is also uncommitted).

No behaviour changes: text in documents, docstrings and comments, ignore
rules and two new overview files. One code change rides along: the exit
wait's core check moves from `utils/closing_threads.py` (which had started
importing `ui/`, against the layering) to its caller `main.py` (install
item, §1.1 of `v17_install_plan.md`).

## 1. Documentation refresh

* `UI_SURFACE_RULES.md`: Step3's run drop-down and `Cell mask ▾` /
  `Nucleus mask ▾` → the `Results…` window (block S3M); the workspace
  chooser of block DV (data versions, draft rows, `Empty trash`) → block RM
  (workspace rows with their Step0 results, × deletes the result and all
  made from it, 30-day purge, no Empty trash); Step3's run list "data
  version" tag → the upstream tag; Step4 takes Step3's `Use` record.
* `docs/user_guide.md`, `docs/用户指南.md`: the same three subjects
  ("Data versions" → "Results (runs)", "Deleting", Step3 item 7).
* Module docstrings / header comments whose status or ownership claims are
  false today (found by a read-only sweep, each verified against the actual
  importers): rewritten to the current responsibility. Historical rulings
  stay in their plans; the docstring names the plan briefly.

## 2. Repository hygiene

* `.gitignore`: `/codex_*.out`, `/codex_*.done`, `/mythos_*.out` (the review
  outputs stay on disk, untracked, just not listed); `cufile.log` and
  `.block01/` (install item).
* `cufile.log` (a CUDA runtime log, tracked by accident, 540 KB):
  `git rm --cached cufile.log` in the commit of this item — the file stays
  on disk. Not staged now, so the pending install item's commit cannot pick
  it up by accident.
* NEW `ARCHITECTURE.md` (steps, layers and their one rule, runs on disk,
  install), `KNOWN_ISSUES.md` (open problems known and accepted for now),
  `docs/README.md` (which documents are current, which are history; no file
  is moved or renamed, since code and plans cite them by path).

## 3. Checks

* Every statement in the rewritten sections checked against the code
  (file:line in the codex review prompt); the docstring sweep lists its
  evidence.
* `tests/test_core_api_check.py`: utils / core / workers / viewer / sources /
  seg_runner import nothing from `ui/`; main passes the check to the exit
  wait.
* Codex review of the diff.

## 4. Scope (closed whitelist)

`UI_SURFACE_RULES.md`, `docs/user_guide.md`, `docs/用户指南.md`,
module docstrings / header comments only (the files the sweep lists),
`.gitignore`, `ARCHITECTURE.md`, `KNOWN_ISSUES.md`, `docs/README.md`, this
plan; `utils/closing_threads.py` + `main.py` + `tests/test_core_api_check.py`
(the layering fix of the install item).

## 5. Implementation notes

* Done as §1-2. The docstring sweep (a read-only search agent, every claim
  checked against the importers) found 28 stale headers in 26 files; all
  rewritten, comment/docstring-only (codex: AST comparison). One stale text
  is USER-VISIBLE and left for the user: Step3's fallback notice "The
  whole-slide view is not connected yet." (`ui/step3_page.py`), shown when
  the viewer cannot draw — changing it is a UI change.
* The guides and the UI spec described three replaced features: data
  versions (block DV, replaced by runs, block RM), the old chooser with its
  `Empty trash` button, and Step3's run drop-down + `Cell mask ▾` (replaced
  by the `Results…` window, block S3M). Rewritten from the code.
* Codex review round 1: FAIL on 6 P3 (Step3 shows the weight editor; default
  ticks once per scene with the nuclei fallback, also for `Load…`; run
  immutability limited to the science; the layering rule names `main.py`
  as the one importer of `ui/`; core/tma consumers; result titles are the
  run's display name) + 1 advisory (`set_viewport_rect` has a guarded Step0
  caller) — all applied.
* Codex review round 2: PASS (all 6 P3 + advisory resolved; 26 files comment/docstring-only). Not committed (user away; the install item is also uncommitted).
