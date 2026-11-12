# Step0 Save writes real windows, never the 0..1 placeholder (backlog 12) — plan v0.3

Found by the user (TG) 2026-11-11 on the 40-channel uint16 demo TMA
(backlog 10): in Step1 every channel is saturated, and after the next start
Step0 is too. "请写第12项的计划给codex审核".

## 1. Today (measured, read-only)

* **The data is fine.** `exemplar002_tma_4cores.ome.tif`, uint16: CD8A p1
  638, median 844, p99 3158; NCAM median 1664; DAPI p99 50194.
* **The saved windows are not.** The user's project
  (`tma_demo/out/rois/full_wsi_20261111_195208_4cb3`): the session's shared
  windows hold 40 channels, **39 of them `min 0, max 1`**; only DAPI has a
  real window (328-7350). The earlier workspace's
  `settings/step0/step0_channel_remap.json`: 39 of 40 at 0..1. On 16-bit
  data 0..1 maps every pixel above 1 to full brightness.
* **Where 0..1 comes from**: `ui/step0/step0_page.py:4843`
  `_step0_save_params` -- what Step0's Save writes per channel: Step0's
  record, or, for a channel without one, `window_params(0.0, 1.0)` ("a
  channel with no real window keeps the provisional placeholder the file has
  always carried"). A channel gets a real window only when its pixels are
  seeded (`seed_display_window` → `_seed_display_mapping`, the QuPath-style
  0.1 % / 99.9 % percentile over the whole slide's overview,
  `core/display_mapping.py:33`), which happens when the channel is shown.
  With 40 channels most are never shown before the Save.
* **How it spreads** (each copies the file's values as real windows):
  * a restore of the workspace (`_install_windows`, `step0_page.py:8714`,
    "the unsaved Step0 draft is restored" in the log) puts them into Step0's
    records and the shared state, so Step0 then shows them saturated too;
  * a Save's handoff (`_handoff_windows_to_shared`, `:11864`) copies Step0's
    records into the shared table Step1/2/3/Regions use;
  * Step1's fallback (`main_window.py:11423`) reads the file directly.
* The state itself already refuses the placeholder
  (`seed_display_window` returns None for "no pixels"; its doc: "0..1
  published over 8/16-bit data saturates every pixel"); only the Save's
  fill-in and the copies of its file bypass that.
* Why earlier data did not show it: fewer channels, and the ones used had
  been shown (seeded) before the Save.

## 2. Design

1. **Every channel's overview is asked for in the background after a
   dataset loads.** Step0 already reads each channel's whole-slide overview
   (the slide at `overview_downsample()`, ~1/32; the demo: 199×197 px)
   through the shared low-res store when a channel is shown
   (`display.ensure_lowres`, read on Block01's reader / a viewer, never on
   the GUI thread). After a load, the page asks that same store for every
   channel once, so by the time of a Save the overviews are resident. No new
   reader, queue or thread: the existing `ensure_lowres` and its one owner
   per (dataset, channel). Memory: one overview per channel (the demo: 40 ×
   157 KB).
2. **The Save seeds what is resident and writes nothing for the rest.** In
   `_step0_save_params`, a channel without a real record:
   * overview resident → its window is computed now by the same rule as an
     on-screen seed (`seed_display_range` over that array; a percentile over
     an array in memory, no read: `_slide_lowres_array(ch, blocking=False)`),
     filed through the existing seed path: `state.fill_seed` first (it never
     overwrites a stored window), whose persistence callback files Step0's
     record (`write_display_window(origin="seed")` → `put_seed`) and syncs
     the editor -- no direct `put_seed` beforehand, so records, state and
     editor stay one answer;
   * not resident yet (or unreadable) → **no entry** in what is saved (and
     `ensure_lowres` is asked again). The status line says
     "N channels had no window yet (still being read): Save again to store
     them". Their readers seed them when shown (Step0, Step1 through the
     shared state's seed port), never show 0..1.
   * The built config is limited to these entries: `build_config(params=…)`
     (`ui/widgets/channel_workbench.py:1165`) falls back to the workbench's
     own params for a channel the dict lacks, so both callers -- the Save
     (`_persist_step0_remap_config`, `:4775`) and the change check
     (`_intensity_settings_changed`, `:4834`) -- drop from `cfg["channels"]`
     every channel `_step0_save_params` did not return. One helper, used by
     both, so the check compares what the Save writes.
3. **A stored placeholder is not a window.** New
   `window_tables.is_placeholder(params)`: `min 0, max 1, gamma 1,
   brightness 0, contrast 1, auto False` -- exactly what
   `window_params(0.0, 1.0)` writes (a real automatic seed of an all-zero
   channel is also 0..1 but has `auto True`, so it is not one). Applied only
   when the slide's pixels are integers, judged from the **slide file's pixel
   type** (its first page's dtype, read once per dataset by the Step0 page),
   not from the float32 overview or workbench arrays:
   * `_install_windows` (a workspace / session restore): placeholder entries
     are skipped; `replace=True` still clears the table's old records,
     typed flags and nucleus role first, then installs the real entries;
   * `_handoff_windows_to_shared`: only real records are copied; the
     nucleus role window is copied as today (it comes from the state's
     Step0 table, which no longer holds a placeholder);
   * `main_window._load_step0_remap_params` (Step1's file fallback): the
     whole file's hash is checked first, unchanged; placeholders are dropped
     from the returned values only (not in the shared loader / normaliser).
   A user who really typed 0..1 on 8/16-bit data loses it on the next
   restore; it shows a saturated picture and nothing else; accepted.
4. **Existing projects** (like the user's): opening the workspace restores
   only the real windows; the others seed as their overviews arrive (§2.1)
   or are shown, and the next Step0 Save writes real windows for them. That
   Save counts as a change (the file's 0..1 entries differ from what is now
   written). Nothing on disk is rewritten without a Save.
5. **Not covered**: Step2's direct reader (`ui/step2_page.py:301`,
   `:3442-3465`), behind a runtime switch, keeps reading the file as it is.
   Files saved after this change hold no placeholder, so it reads real
   windows; an old file read there still holds 0..1 until a Step0 Save.
   Said in KNOWN_ISSUES only if the user wants it.

Not changed: the percentile rule, typed windows (a user's record is never
replaced), the Intensity window's UI, Step1's fusion maths, the handoff
schema and hash.

## 3. Files (closed list)

* `ui/step0/step0_page.py`: `_step0_save_params`, the helper that limits
  a built config to its entries (used by `_persist_step0_remap_config` and
  `_intensity_settings_changed`), `_install_windows`,
  `_handoff_windows_to_shared`, the slide pixel-type helper, the request of
  every channel's overview after a load.
* `ui/step0/window_tables.py`: `is_placeholder`.
* `ui/main_window.py`: `_load_step0_remap_params` (skip placeholders on an
  integer slide).
* Tests (new): `tests/test_step0_save_real_windows.py`.
* This plan.

## 4. Tests (synthetic, offscreen)

1. A synthetic uint16 slide with 6 channels, one shown, one typed: after
   the load every overview becomes resident without a GUI-thread read; the
   Save's **written JSON** has a real window (the percentile rule's
   numbers) for all 6, no placeholder; the shown and the typed ones are
   unchanged; Step0's records, the shared state and the editor agree.
2. The same with uint8.
3. A channel whose overview is not resident at the Save (its read held
   back): no entry for it in the written JSON (not the workbench's 0..1),
   the status line names the count; the change check agrees with the file
   (a second Save with nothing changed says "No changes"); once the
   overview arrives, the next Save writes it.
4. Restore of a workspace whose file / session `shared_windows` hold 0..1
   placeholders (written by today's code), uint16 slide with float32
   overviews: records and state do not take them; `replace=True` still
   clears old records, typed flags and the nucleus role; typed real entries
   come back typed; a full cycle Save → close → restore → Save keeps the
   typed set.
5. `_handoff_windows_to_shared` copies real records only and the nucleus
   role; Step1's file fallback: hash checked on the whole file, placeholders
   dropped on an integer slide, kept on a float slide; after a Save the
   handoff hash matches.
6. `is_placeholder`: exact match only (0..1 with gamma 0.8, or `auto`
   True, is not one).
7. Existing Step0 / Intensity / handoff / window-table tests: no new
   failures against a HEAD run on the Mac.

Acceptance (user, Mac):
* the demo project: open it, Step0 Save, Step1: no channel saturated;
  windows as on Step0;
* a fresh load of the 40-channel demo: the window stays responsive while
  the overviews arrive; a Save a minute later stores all 40.

## 5. Risks and rollback

* The Save adds only a percentile per unseeded channel over an array
  already in memory; the overview reads happen in the background after the
  load (§2.1). A Save made before they arrive leaves those channels out and
  says so (§2.2).
* Rollback: revert the commit; files saved meanwhile hold real windows,
  which today's code reads as before.

## 6. Plan review (codex astra low)

Round 1 FAIL (scratchpad `codex_g12_plan_r1.txt`), all taken in v0.2:
* P2: omitting a channel from `_step0_save_params` was not enough --
  `build_config` falls back to the workbench's 0..1 for it. §2.2: both the
  Save and the change check limit the built config to the returned
  entries; tests check the written JSON.
* P2: seeding at the Save with blocking reads would put 170-230 ms per
  channel back on the GUI thread. §2.1-2: every overview is requested in
  the background after a load through the existing store; the Save only
  computes percentiles over resident arrays and leaves the rest out.
* P3: "integer pixels" now comes from the slide file's pixel type, not the
  float32 overview (§2.3); tested.
* P3: Step2's direct reader is named as not covered (§2.5).
* P3: restore / handoff / hash details and tests added (§2.3, §4.3-5); the
  acceptance forbids placeholder records, not a real automatic 0..1.

Round 2 PASS (scratchpad `codex_g12_plan_r2.txt`). Two P3 wording fixes
taken in v0.3: the Save files a seed through `fill_seed` and its existing
persistence callback (no direct `put_seed`, so the editor is synced); the
risk line no longer says the Save reads overviews.

## 7. Implementation (uncommitted, 2026-11-11)

User (TG): "批准所有实施，验收延后".

* `ui/step0/window_tables.py`: `is_placeholder`.
* `ui/step0/step0_page.py`: `_step0_save_params` (a typed window is never a
  placeholder; otherwise a real record, or `_seed_for_save`, or left out
  into `_save_unwindowed`), `_seed_for_save` (resident overview only;
  `fill_seed`; an untyped placeholder still in the state is replaced there
  by `set_mapping(origin="seed")`), `_step0_save_config` (used by
  `_persist_step0_remap_config` and `_intensity_settings_changed`),
  `_slide_is_integer`, `_is_stored_placeholder`, `_request_all_overviews`
  (called at the end of `_reload_from_paths`), the warning after a Save
  that left channels out (`_warn_load_row`); `_install_windows` and
  `_handoff_windows_to_shared` skip untyped placeholders.
* `ui/main_window.py`: `_without_remap_placeholders`, applied after the
  hash check on every return of `_load_step0_remap_params`.
* Tests: `tests/test_step0_save_real_windows.py` (15).

Code review (codex astra low): round 1 FAIL -- P2: a typed 0..1 window on
an integer slide was re-seeded at Save while the state kept 0..1. Fixed:
a typed window is never a placeholder (Save, restore, handoff), and an
untyped placeholder still in the state is replaced there through the seed
path; P3 tests added (typed 0..1, placeholder in the state, the real
Save's JSON and warning). Round 2 PASS.

Regression (one process per test; 38 Step0 / Step1 / viewer files, the
working tree against a HEAD worktree): 804 tests each side; 12 fail on
HEAD and the same 12 on the change; one more on the change:
`test_step0_step1_handoff_contract.py::test_another_dataset_still_starts_from_fresh_defaults`,
exit 139. Its body passes; the crash is in shiboken6 `SbkDeallocWrapperCommon`
during the suite's forced between-test `gc` (`conftest.py:78`), the
documented PySide6 6.11.3 issue on macOS (KNOWN_ISSUES; none on 6.9.3 in
the backlog-6 check). Bisected (5 runs each): HEAD, 11, 12, 13 alone,
12+13, 12+14, 13+14 and 12+13+14 pass; 11+12 together crash 4 of 5 (a
later single run passed): a gc-timing effect of the larger object graph,
not a product path.
