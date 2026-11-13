# The user's acceptance notes, 11 items (backlog 17-27) — plan v0.2

Status: the user's rulings 2026-11-12 (below, "User ruling"); order B19,
B26, B27, B24, B25, B18, B21, B20, B22, B17; B23 its own plan. Codex plan review round 1
FAIL (P2s in B17, B18, B19, B21, B22, B24, B27), fixed in v0.2;
round 2 FAIL (B27 whitelist), fixed; round 3 PASS. Each
item is its own block (closed file list, behaviour, tests); a block starts
only when the user starts it. Facts come from reading the code (file:line
as of 0d4e9e6 plus the uncommitted 15B in `ui/step2_page.py`); where only a
run can confirm the cause, the block starts with that check.

Order proposed (smallest risk / most visible first): B19 (item 3), B26
(10), B27 (11), B24 (8, after the discussion), B25 (9), B18 (2), B21 (5),
B20 (4), B22 (6), B17 (1), B23 (7, its own plan).

---

## B17 — Step0: the window blinks once after a Load (item 1)

**Likely cause (not yet observed):** the first OpenGL widget in the main
window. `ui/step0/engine_mount.py:163-174` `start()` creates a
`Step0GlHost`, attaches it to the view, `show()` / `grabFramebuffer()` /
`hide()`. The first `QOpenGLWidget` in a top-level window makes Qt switch
the window to GL composition, which re-creates its native window: a blink
on X11 and macOS. `ui/gpu_warmup.py:5-17` records this step and why a
start-up 1x1 GL widget was removed earlier (its CPU repaint cost). Every
Load disposes the old host (`engine_mount.py:670-677`) and makes a new one.

Other candidates: the auto-opened Tissue Preview, a stay-on-top window
raised and activated (`step0_page.py:6557` -> `block01_display.py:2511`,
`:3247-3253`): a focus flicker; `_discard_stack` detaches a view without
hiding it first (`step0_explore_tab.py:958-959`), the pattern of 6f25186.

**Step 1 (diagnosis, no code change):** on the Mac, Load with
`BLOCK01_STEP1_BACKEND=python` (no engine, no GL host) and without;
screen recording (`screencapture`) of both. Also Load twice in one
session (does the blink repeat?).
**Step 2 (fix, depends on step 1):**
* GL switch: the window is switched to GL composition once at start-up,
  hidden (one persistent `Step0GlHost`, created with the window, reused by
  every Load), not per Load. Today a host refuses a second attach
  (`ui/step0/gl_host.py:664`): reuse needs a lifecycle change there
  (detach from the old view, attach to the new one) -- part of this block. The cost `gpu_warmup.py` recorded is measured
  again before deciding (frame time of a CPU repaint with the window in GL
  composition).
* Tissue Preview: when it is opened automatically after a Load, it is
  shown and raised but not activated (no `activateWindow()` /
  `WindowActive` in that path, `block01_display.py:3249-3253`;
  `WA_ShowWithoutActivating`); opened by the user it activates as today --
  only if the recording shows the focus flicker.
* `_discard_stack`: `view.hide()` before `setParent(None)` (cheap).
**Files:** `ui/step0/engine_mount.py`, `ui/step0/gl_host.py`,
`ui/step0/step0_explore_tab.py`, `ui/block01_display.py` (only for the
cause(s) the recording confirms; the user is told which before the fix). **Tests:** a host is
created once and reused across two synthetic Loads; real-machine
recording is the acceptance (the blink is not testable offscreen).
**Risk:** GL composition from start-up for the whole window (CPU repaint
cost); rollback: revert.

**Diagnosis (2026-11-13, Mac mini, real display; no product change).** A
script opened the real window as `main.py` does, loaded two synthetic
slides (A, then B) and logged every window event, with a screen recording.
* Default backend: on Load A the main window is hidden, its native window
  destroyed and recreated (raster -> OpenGL surface), shown again --
  267 ms after the click; the recording shows it half transparent in one
  frame (macOS fades the window out and in). Load B: nothing (the window
  stays GL-composed, though a new host is made). With
  `BLOCK01_STEP1_BACKEND=python` no recreation at all. So: the first GL
  host's initialization (`engine_mount.start`: show, `grabFramebuffer`,
  hide), once per session.
* Also on Load, both backends: Step1's channel rows
  (`widgets/channel_dock/template.py` `build_row_core`) create a
  `QCheckBox()` without a parent and `setVisible` it before it is laid
  out: for an instant each is its own window and takes the activation from
  the main window (three times for three channels).
* Not observed: `_discard_stack`. The Tissue Preview, opened on Load, takes
  the activation (as designed today).
* Fix tried in the script only: a 1x1 `QOpenGLWidget` child shown,
  `grabFramebuffer()`, hidden -- before the window is first shown: the
  window is created GL-composed (57 ms at start, off screen), no
  recreation on Load A or B, no faded frame. A hidden or merely shown GL
  child, or `QT_WIDGETS_RHI=1`, does not switch it. Cost: a whole-window
  update before the first Load 2 ms -> 10.7 ms (after a Load it is ~10 ms
  today anyway). The plan's persistent, reused host is not needed.

**Implemented (2026-11-13, the user approved fixes 1 and 2; uncommitted).**
`ui/gpu_warmup.py` `compose_with_gl_before_show(win)`, called in `main.py`
before `win.show()`: when `engine_refusal()` is "" (and not
`BLOCK01_GL_FIRST=0`), a 1x1 `QOpenGLWidget` child shown,
`grabFramebuffer()`, hidden; a failure prints and leaves the window as it
was. `ui/widgets/channel_dock/template.py`: the checkbox made with its row
as parent. Checked on the real display with the product code: default
backend -- GL surface from the first show, no recreation on Load A or B,
no checkbox window; Python backend -- nothing done, raster as before.
Tests `tests/test_b17_no_blink.py` (the GL one on the real display; without
the fix that sequence recreates the window). Codex review PASS. Not
changed: the Tissue Preview still takes the activation when it opens on a
Load (explained to the user; their choice).

## B18 — Step0: Save reloads the view (item 2)

**Cause:** Save of a correction run releases the Explore view for
production: `step0_page.py:12709` `_rm_prepare_save` -> `:12117` ->
`:3839` -> `step0_explore_tab.py:875` `engine.release_for_production()`
without `keep_picture`: the engine pauses, drops the live sources
(`engine_mount.py:558-565`, `:762`) and gives the GPU tiles back to the
coarse level. When the run ends, `_on_production_worker_finished`
(`step0_page.py:3326`) resumes it: every tile is requested again, DAPI
first (a raw picture), the marker after (its live source recomputed); the
shift is coarse tiles drawn before fine ones. One change is real and by
design: once saved, the selection is drawn from the **saved product**
instead of being computed live (`engine_mount.py:265-291` `_saved`,
`identity_for`, `:365-380`; `viewer/correction_plan.py:108`
`saved_product`) -- another source, so its tiles must be read. The
SHARED window table Save writes is ignored by Step0
(`block01_display.py:1419-1422`).

**User question (2026-11-12):** the picture is already in the GPU before
Save -- why load it again? Answer: today Save gives the tiles back to make
room for the correction run and asks for them again after it; and after
Save the channel is read from the saved product instead of computed live
(by design, cheaper panning later), which is new data for the GPU though
the pixels are the same. The fix keeps the tiles during the run and makes
the source switch invisible (below).
**Fix:**
1. During the run, Save keeps the picture, as the patch correction
   already does (`engine_mount.py:549-557`: requests suspended, the
   published tiles kept and presented): `step0_explore_tab.py:862`
   `begin_release_for_production(keep_picture=...)`;
   `step0_page.py:3821`, `:3839` pass `keep_picture=True` for Save.
2. After the run, the switch to the saved product goes through the
   panel's candidate core (the old picture stays on screen until the new
   core has the tiles of the view, then swaps -- the mechanism the engine
   uses for a method switch, `drop_candidate` / standby, `:365-380`), not a
   blank-and-reload. The camera is untouched.
3. Diagnosis first for the shift: compare, at one view, the live picture
   and the saved product's (same channel, method, parameters). Equal
   pixels: the shift was coarse-before-fine, solved by 2. A real offset:
   a separate defect, reported to the user before any fix.
**Trade-off:** the screen's GPU tiles stay held during the run (the run
has that much less GPU memory), as with the patch correction today. No
headroom fallback (no measurement of free GPU memory exists).
**Files:** `ui/step0/step0_explore_tab.py`, `ui/step0/step0_page.py`,
`ui/step0/engine_mount.py` (the switch after the run).
**Tests:** a Save with a correction run on a synthetic project: the
engine receives `keep_picture=True`; during the run the published tiles
stay; after it the saved product is a candidate and the old picture is
presented until the candidate is ready; the camera range is unchanged.
Real machine: no blank, no DAPI-then-channel, no shift.

## B19 — the Regions tool is not kept (item 3)

The user wrote "Step1"; the tools Select / Mask / Annotate exist only on
the Regions page (Step 0.5: `ui/regions/page.py:36-52`, `:161-182`,
`_set_tool` `:428-440`; the tool lives in `ToolController.tool`,
`ui/regions/tools.py:50`). **To confirm with the user** that this is the
page meant.

Reading the code, nothing on a step change resets the tool
(`_go_to_regions` `main_window.py:5963-5986`, `set_context`
`page.py:258-283`, the mount `ui/regions/mount.py:84-131`). The writers:
`_set_tool`, `_escape` (`page.py:487-497`, the Esc shortcut) and
`_tab_changed` (`ui/regions/shapes.py:22-35`).
**Step 1:** reproduce on the Mac with a print in `ToolController.set_tool`
(its caller) on leaving and re-entering. **Step 2:** the page remembers the
tool when hidden (`hideEvent`) and re-applies it through `_set_tool` when
shown again (its Exclusions-tab guards kept), or the one caller found in
step 1 stops resetting it. No persistence across restarts.
**User ruling:** Step 0.5 is meant (written Step1 by mistake).
**Files:** `ui/regions/page.py`. If the reproduction finds the reset in
another file, that file is named to the user and approved before it is
changed. **Tests:** choose
Annotate ▸ Line, switch to Step1 and back: still Annotate ▸ Line; the
Exclusions-tab guard still applies.

## B20 — Step2: leading skipped tiles walked one by one (item 4)

**Cause:** both loops treat a skipped tile like any other
(`segment_merge_worker.py:3131-3195`, `:4070-4130`): a progress emit,
`scheduler.load_tile(i)` through the prefetcher (queue 2, so skipped tiles
take its slots ahead of the first real tile), **a DAPI read of its own
box** (`_prepare_tile_payload`, `:2253-2263`), the DAPI write, then
`_skip_tile` (`:2238-2249`) with **`gc.collect()` per tile**. The page
recolours per signal.

**User ruling:** authorized (the prefetcher's feed included), but the
start must not wait longer than today.
**Fix (outputs identical):** the loop starts at once on the first tile to
segment: the skipped tiles' metadata is recorded and their `tile_skipped`
emitted together before it (no read, no `gc.collect` each -- no wait);
their DAPI is written in one bounded pass **after** the segmented tiles
(block reads, as `_fill_dapi` `:920` does for a cores run), before the
global outputs; the loop does not load them (the
prefetcher is given the tiles to segment only). **Authorization needed:**
giving the prefetcher a subset changes how the scheduler is fed (AGENTS.md
rule 4): `TileScheduler` keeps its indices; only the order of
`load_tile` calls skips the skipped ones -- to be confirmed by the user.
**Files:** `workers/segment_merge_worker.py` (both loops),
`utils/tile_scheduler.py` (only if the prefetcher needs a list of
indices). **Tests:** a synthetic run with the first k tiles skipped: the
same `global_mask`, `global_dapi`, metadata and skipped lists as before
(byte-compared against the current code); no engine call and no
`load_tile` for a skipped tile; the first engine call is on tile k.

**Started by the user 2026-11-13**, widened: (a) the start of a Step2 run
waits too long -- shorten it; (b) skipped tiles: start at the first tile to
run; (c) the same for TMA cores.

**Measured (2026-11-13, synthetic fused zarr, the real worker, a fake model
that stops at its first call; cProfile):** from `run()` to the first engine
call 1.0 s at 6400 x 6400 (4 x 4, 8 tiles skipped), 1.2 s at 20000 x 20000,
0.85 s for a cores run of 40 cores at 20000 x 20000 (`AnalysisRaster`
0.01 s). Of that, 0.5-0.8 s is `_mem_snapshot` importing torch only to
report CUDA memory (none on the Mac). The skipped tiles: ~0.02 s each here
(cached synthetic reads); on real data each reads its DAPI from the
compressed input and runs a full `gc.collect()`, holding the prefetcher's
two slots ahead of the first real tile. The user's TMA demo log
(2026-11-11, grid 3 x 4): worker start to the engine process ready 3 s
(Cellpose, MPS), first tile 16 s (as every tile: no first-call penalty).
Not measurable without a real workspace: the page's work between the Run
click and the worker's start (`step2_page.py` `_run`: the snapshot freeze
`manual_regions` -> `build_snapshot`, the engine-identity and
fusion-settings checks, the skip plan, the tile suggestion). No Step2 log
of the user's newer runs exists on disk.
* (c): a cores run already starts at its first core (its units are the
  selected cores only; the DAPI is filled after them). In a grid run that
  follows TMA cores, the tiles without a selected core (`regions_skip`)
  are walked like the skip plan's -- the fix covers them (`skip_all`).
* The prefetcher submits tiles in index order (`tile_prefetch.py`
  `_next_submit`): to start at the first tile to run it takes the list of
  tiles to segment -- `utils/tile_prefetch.py` joins the file list.

**Implemented (2026-11-13, the user approved 1-3; uncommitted).**
1. `workers/segment_merge_worker.py`, both loops: `_skip_ahead` records and
   tells every skipped tile (plan and `regions_skip`) in tile order before
   the loop, without a read or a `gc.collect`; the loop `continue`s over
   them; the prefetcher gets `order` = the tiles to segment
   (`utils/tile_prefetch.py`, `utils/tile_scheduler.py`); after the loop
   `_fill_skipped_dapi` writes their DAPI with the same read as before
   (not in a cores run, whose fill covers everything). `ui/step2_page.py`
   `_on_progress`: a skipped tile announced as "next" stays skipped.
   Byte-compared against the previous code (scratch script, 8 cases: first
   tiles, scattered, Regions, Regions + plan; both loops): every output
   array, JSON and the skipped lists identical.
2. `_mem_snapshot` uses torch only if already imported.
3. `[Step2 timing]` lines: the page's steps in `_run` (printed, and handed
   to the worker as `page_timings` for the run log -- codex round 1 P2),
   the worker's (`_timing`: worker started, Regions checked, engine ready,
   first tile read, first tile done), all "ms after Run".
Tests: `tests/test_step2_skip_ahead.py`; `test_step2_skip_empty_tiles.py`
`test_the_prefetcher_keeps_up` updated (the skipped tiles are no longer
read ahead). Codex review round 1 FAIL (the P2 above), round 2 PASS.
Regression (74 files, 1755 tests): 110 failures also fail on HEAD; the
rest are the known teardown crashes (139), passing on rerun.

**The user's acceptance, round 1 (2026-11-13):** on Run the purple went
away for a moment and the running highlight sat on tile 0 first. Fixed in
`ui/step2_page.py`: `_run` computes the worker's skip set from the plan
(none on a cores run or in Recovery Mode) and `_begin_run_progress`
records those tiles skipped in the same call that resets the grid; the
first tile that runs is marked running, not tile 0. Test added; codex PASS.
The user's timed run (test2, StarDist, 2026-11-13): the page 42 ms, the
worker at 44 ms, **the engine process ready at 4304 ms** (a new process
loading torch, StarDist and its weights), the first tile read at 4877 ms,
segmented in 19.6 s. The remaining cold start is the engine's start: only
a pre-started engine removes it. **The user, 2026-11-13: keep it as it is.**
**Acceptance round 2 (the user, 2026-11-13): passed;** one more ruling:
every skipped tile hatched, not only the tissue tiles the user chose
(`ui/step2_page.py` `HATCHED_STATES = SKIP_STATES`; the overview and the
zoomable viewer). Three assertions of `test_step2_skip_empty_tiles.py` that
said an empty skipped tile is not hatched updated.

## B21 — Exclusions in Step2 and Step3, with a switch; Smooth removed (item 5)

**Today:** Step2 and Step3 draw cores only (`core/regions/display.py`
`saved_items` / `snapshot_items`); the snapshot holds the exclusions
(`core/regions/analysis.py:332-354`).
**Add:** `display.exclusion_items(snapshot)` -> one item per exclusion
(polygon, the Regions page's exclusion colour `#e040fb`, hatched as on
the Regions page -- the look of B27). A second `CoreOutlines` per view
(Step2 overview, Step2 viewer, Step3) with its own checkable button
"Exclusions" next to "TMA cores", on by default, hidden while there is
nothing to draw, its state in the session (`exclusions_stepN`), copying
`_tma_cores_button` (`main_window.py:2037-2051`) and Step2's
`btn_tma_cores`. Step2 and Step3 draw the exclusions of the snapshot they
follow (Step2: as its cores, the running run's snapshot else the last
Save; Step3: the run shown).
**Colour question for the user:** Step2 draws unticked cores in the
skipped-tile purple `#c678dd` (15A); the exclusion magenta `#e040fb` is
close to it. Proposal: exclusions hatched (unticked cores are outlines
only), which keeps them apart.
**Smooth:** the only Smooth button is Step1's `_btn_smooth`
(`main_window.py:1212-1222`, handler `_set_smooth_pixels` `:8850`,
session `smooth_pixels` saved `:4787`, restored `:5016-5021`, rollback
`:5047-5048`). Removed: no session value is written or applied any more
(an old session's `smooth_pixels: false` is ignored), so smooth is what
`gpu_memory` starts with (`ui/gpu_memory.py:30`: on, unless the developer
switch `BLOCK01_SMOOTH=0`). No `set_smooth(True)` call that would override
that switch.
**Files:** `core/regions/display.py`, `ui/step2_page.py`,
`ui/step2_viewer.py`, `ui/main_window.py`. **Tests:** exclusion items
from a synthetic snapshot; the switch hides them and is restored from the
session; Step3 draws them; no Smooth button, and an old session with
`smooth_pixels: false` leaves smooth on.

**Implemented (2026-11-12, uncommitted):** `display.exclusion_items`
(a snapshot) and `saved_exclusion_items` (the last Save's document), colour
`EXCLUSION = #e040fb` (test: equal to `EXCL_COLOR`), B27's screen hatch, 3
px, no label. Step2: `btn_exclusions` beside "TMA cores", a second
`CoreOutlines` over the overview and over the zoomable viewer, drawn
before the cores (cores on top); source as the cores'. Step3:
`_exclusions_button("step3")` beside its "TMA cores", drawn from the run's
`regions_snapshot.json` in `_refresh_core_outlines("step3")`. Session
`exclusions_step2` / `exclusions_step3` (on when absent). Smooth: the
button, `_set_smooth_pixels`, the session write, restore and rollback
removed. The colour question is moot: since B28 unticked cores are yellow,
not purple. Tests: `tests/test_step23_exclusions.py`; the Smooth tests in
`test_step0_step1_surface_details.py` / `test_step1_overlay_preview.py`
rewritten (no button, an old session's value ignored);
`test_step3_patch_strip.py` row; `test_f5_step2_viewer.py`'s two page stubs
get the new attributes. Codex code review round 1 PASS. Regression (52
files, 948 tests): every failure also fails on HEAD, except
`test_f5_step2_viewer::test_the_grid_is_drawn_and_clicked_in_slide_pixels`,
which passes but crashes (139) in the conftest's teardown `gc.collect()`:
the garbage is Step2's old tile rectangles (`_TileRect`, pyqtgraph ROI
handles, a QMenu), not B21's objects; HEAD crashes there too (1 of 9), the
extra object only moves when the collector runs. Advisory, not fixed here.
**Accepted by the user 2026-11-13 (with B28).** Addendum the same day (the
user: Step1 as Step2 / Step3): Step1 gets an "Exclusions" switch beside its
"TMA cores" (`_exclusions_button("step1")`, session `exclusions_step1`), the
last Regions Save's exclusions drawn under its cores in
`_refresh_core_outlines("step1")`; `EXCLUSIONS_TIPS`. Test added to
`tests/test_step23_exclusions.py`. Codex review of the addendum PASS.

## B22 — FusionFlux icon and name (item 6)

**Today:** no icon file in the repo; `main.py` never sets the application
name or icon; the Mac launcher (`work/envs/run_block01_mac.sh`) runs the
bare interpreter, so the Dock shows python's blank icon and "python3.10";
the Ubuntu `.desktop` entry (`scripts/block01 --desktop`) has
`Name=Block01`, no `Icon=`, no `StartupWMClass=`.
**Add:**
* An icon: `assets/fusionflux.svg` (a simple mark designed here: overlapping
  fluorescence channels as coloured discs over a dark tile) and the
  generated `fusionflux.png` (1024), `.icns` (macOS `iconutil`) and `.ico`
  (Windows, for B23); a script `scripts/make_icons.py` regenerates them.
  The user approves the design before anything else uses it.
* `main.py`: `setApplicationName("FusionFlux")`,
  `setApplicationDisplayName("FusionFlux")`, `setWindowIcon(...)`,
  `setDesktopFileName("fusionflux")`. The window title becomes
  "FusionFlux" (today "CODEX Pipeline | Fusion + Segmentation",
  `ui/main_window.py:581`) -- to confirm with the user.
* macOS Dock name: a `FusionFlux.app` wrapper (Info.plist `CFBundleName`,
  `CFBundleIconFile`, its executable starts the env's python with the
  launcher's environment) made by `scripts/make_mac_app.sh` into
  `work/envs/` (outside the repo, like the launcher). Whether the Dock
  keeps the bundle's name after the script `exec`s python is checked on
  the Mac; if not, the bundle's executable is a copy of / link to the
  env's python (the bundle owns the process).
* Ubuntu: `scripts/block01 --desktop` writes `fusionflux.desktop`
  (matching `setDesktopFileName("fusionflux")`; today `block01.desktop`,
  `scripts/block01:49`) with `Name=FusionFlux`, `Icon=<abs png>`,
  `StartupWMClass=fusionflux`, and removes the `block01.desktop` it wrote
  earlier; `tests/test_install_scripts.py` updated.
**Files:** `assets/` (new), `scripts/make_icons.py` (new),
`scripts/make_mac_app.sh` (new), `main.py`, `ui/main_window.py` (title),
`scripts/block01`, `tests/test_install_scripts.py`.

## B23 — a native Windows version (item 7): its own plan

Too large for one block. Readiness found: POSIX-only code that breaks
every engine run (`seg_runner/client.py:69` `start_new_session`,
`:83-85` `os.killpg`/`SIGKILL`, `:17`/`:187-188` `selectors` on a pipe --
select works on sockets only on Windows; `segment_merge_worker.py:1439`
`os.killpg`); `seg_runner/mesmer_torch/prepare.py:17` `import fcntl` at
module top; `ui/step0/gl_host.py:95` loads `libGL.so.1`; soft ones with
fallbacks (`/proc`, `resource`, `nvidia-smi`, symlink). Dependencies:
cuCIM and TF-GPU have no Windows route; torch cu121, cellpose, cupy,
numba, PySide6 do; Mesmer and StarDist through their Torch conversions
(as on the Mac). The viewer core already has a `win_amd64` wheel. No
installer exists (Linux bash only).
**User ruling:** agreed (its own plan). **TensorFlow:** all inference runs in
PyTorch, but TF (CPU only) is still imported for two things: StarDist's
`StarDist2D` object, whose Keras model is swapped for the Torch network
(`seg_runner/engines.py:290-330`, its GPUs hidden), and the one-time
Mesmer conversion from DeepCell's weights
(`seg_runner/mesmer_torch/convert.py:77`); Mesmer's post-processing is
`deepcell_toolbox` (no TF). TF 2.8 has CPU wheels for Windows, so phase 2
can keep it; removing it (StarDist's own pre/post without the Keras
shell; reading the Mesmer weights with `h5py`) is an option the Windows
plan weighs.
**Proposal:** `docs/v17_windows_native_plan.md`, phased: (1) run from
source on Windows (the blockers behind `sys.platform` guards; the
`docs/v17_windows_acceptance.md` checklist), (2) engines (a Windows
requirements file, Torch Mesmer/StarDist, `seg_runner/selftest`),
(3) a PowerShell installer + launcher + Windows CI, (4) a packaged
installer (PyInstaller/Briefcase, `.ico`, AppUserModelID).
**Needs from the user:** a Windows machine with an NVIDIA GPU (or a
Windows CI runner) for every phase's acceptance.

## B24 — Regions: an exclusion inside a larger core cannot be clicked (item 8) — for discussion

**Today:** the click picks the smallest shape under the pointer
(`ToolController._smallest_at`, `tools.py:112-122`), but among the open
tab's shapes only: `shapes()` is `d["pieces"]` in the Cores tab and
`d["exclusions"]` in the Exclusions tab (`:86-91`, `shape_at`
`:124-133`). In the Cores tab exclusions are never candidates, though
they are drawn on top (`page.py:521-522`): they look clickable but are
not. In the Exclusions tab an exclusion is found first.
**Options:**
* (a) Across kinds: in either tab the smallest shape under the pointer
  wins, cores and exclusions alike; clicking an exclusion while in Cores
  opens the Exclusions tab with it selected (the operations are typed per
  tab: delete, transform, vertices). Touches `shape_at`, the page's
  selection pruning (`page.py:503-506`), the tab switch.
* (b) Exclusions always before cores, in every tab (same files, a fixed
  priority).
* (c) A repeated click at the same spot cycles through the stack under
  the pointer, smallest first (a small "last click and stack" state in
  `ToolController.click`).
* (d) Alt+click picks the shape under the selected one.
**Kept whatever is chosen:** in the Exclusions tab a click on a core mask
selects it for conversion (tick a reason -> it becomes an exclusion;
`UI_SURFACE_RULES.md:601-604`): (a) applies in the Cores tab only; the
Exclusions tab keeps its rule (exclusions first, then core masks), and
(c) lets a repeated click there reach the core mask under an exclusion.
**Recommendation:** (a, in the Cores tab) + (c, both tabs): the natural
click selects what is visibly on top and smallest; clicking again at the
same place reaches the shape under it. The user decides; this section is
a discussion, not an authorization.

## B25 — Regions: Shift+click adds to the selection (item 9)

**Today:** Ctrl+click toggles one shape in the selection
(`tools.py:392-394`, `shapes.py:84-93`); marquees add with Ctrl; Shift is
ignored on a click (it means a constrained drag for circle / transform).
**Change:** Shift+click acts as Ctrl+click (toggle) on a click; Shift
during a drag keeps its meaning; Ctrl on a transform-box corner keeps
"distort". Tooltip and the numbering hint (`page.py:36`,
`ui/tma/numbering_panel.py:83`) say "Shift or Ctrl + click".
**Files:** `ui/regions/overlay.py` (pass shift with ctrl to `click`),
`ui/regions/tools.py`, `ui/regions/shapes.py` (`ctrl_held`),
`ui/regions/page.py`, `ui/tma/numbering_panel.py`. **Tests:** Shift+click
adds and removes; a Shift-drag of a circle still constrains; delete /
union act on a Shift-made selection.

## B26 — Regions: thicker outlines (item 10)

**Today:** every core and exclusion is 1.5 px, 3.0 px when selected
(`overlay.py:354-357`); Regions sets no width. Pen widths are screen
pixels (points are mapped to the widget by hand, the painter has no
scale), so they do not change with zoom.
**Change:** bound (assigned) cores and exclusions 3.0 px
(`cores_panel.py` `draw_parts` for a core with a sheet position;
`exclusions_panel.py:273`); a selected shape 3.0 px + a white halo
(drawn under it, 2 px wider) so it still stands out. Unbound and free
pieces keep 1.5 / dashed.
**Files:** `ui/regions/cores_panel.py`, `ui/regions/exclusions_panel.py`,
`ui/regions/overlay.py`. **Tests:** the item widths; the halo for a
selected shape (the paint is read from an offscreen render).

## B27 — Regions: run cores hatched green; hatches legible at any zoom (item 11)

**Today:** hatch = `QBrush(QColor(color), Qt.BDiagPattern)`
(`overlay.py:366`, `:388`): Qt's 8x8 pattern of 1 px aliased lines, in
device space; on a HiDPI pixmap and on small shapes it breaks up and
fades against the image -- the outline is already constant in screen
pixels. Run cores have no look of their own on the Regions page.
**Change (the overlay's paint only):**
* A hatch of its own in screen space: diagonal lines at a fixed spacing
  (10 px) and width (1.5 px, antialiased), clipped to the shape: the same
  thickness at every zoom; fewer lines as the shape gets smaller on
  screen.
* Under it a translucent fill of the hatch colour (alpha ~ 0.18) so the
  colour stays even when only a few lines fit.
* A new item key `hatch_color` (the hatch's colour may differ from the
  outline's): exclusions purple as today; run cores (`D.core_runs` / the
  position's run) light green `#66bb6a`, fill alpha ~ 0.12 ("not too
  strong").
* Only for items that ask for it: a new key `hatch_style: "screen"` set by
  the Regions page's exclusions and run cores, the live transform preview
  of those, and B21's exclusions in Step2 / Step3. Everything else that
  passes `hatch=True` keeps Qt's pattern (Step2's skipped tissue tiles,
  `ui/step2_page.py:3018`; the overview's own painting `:183` untouched).
**Files:** `ui/regions/overlay.py`, `ui/regions/cores_panel.py`,
`ui/regions/exclusions_panel.py` (the exclusions' items opt in).
**Tests:** a rendered offscreen overlay at two zooms: the hatch line width
in pixels the same, the line count lower at the lower zoom, the fill
alpha the same; a run core has the green hatch, an unticked one none.

---

## Common rules
AGENTS.md: closed file lists per block; tests synthetic; real-machine
acceptance for every look; no new registry / cache / scheduler / state
machine unless named above and approved (B20's prefetch order is the only
one).

---

## Implementation log

* **B19** (2026-11-12): not reproduced offscreen. With the page tests'
  synthetic project, leaving Regions for Step0 / Step1 / Step2 through the
  step bar's handlers and coming back keeps the tool (`select_rect`,
  `circle`); `ToolController.set_tool` is not called on the way. Waiting
  for the user's exact steps (tool, tab, how Regions was left and
  re-entered, a Save or Load in between).
* **B26** (uncommitted): `overlay.py` `BOUND_WIDTH` / `SELECTED_WIDTH` 3.0,
  `SELECTED_HALO` 2.0 (a white halo under a selected shape);
  `cores_panel.py` a bound core `width` 3.0; `exclusions_panel.py` an
  exclusion `width` 3.0. Tests `tests/test_regions_outline_look.py`.
* **B27** (uncommitted): `overlay.py` `paint_screen_hatch` (a translucent
  fill, then 1.5 px lines every 10 px clipped to the shape, anchored at its
  on-screen corner) for items with `hatch_style: "screen"`, in the static
  paint and the live preview; `cores_panel.py` run cores (a position's run
  and not QC-excluded; a map-less core's tick) `#66bb6a`, fill alpha 0.12;
  exclusions opt in. Items without the style (Step2's skipped tiles) keep
  Qt's pattern. Tests in the same file.
* Codex code review B26+B27 round 1: P3 (halo 3 px, plan 2 px) -> fixed.
  Regression (26 files, 545 tests) vs HEAD: no new failure (one known Mac
  gc crash, intermittent on HEAD too).
* **B24** (uncommitted; user ruling: (a) in the Cores tab + (c) both
  tabs): `tools.py` `click_stack` (Cores: every piece and exclusion under
  the point, smallest first; Exclusions: its exclusions, then the core
  masks it converts), `_pick` (a click again within the click slop --
  Manhattan, screen px -- while one of the stack is selected picks the
  next, round); a Select click on an exclusion in the Cores tab opens the
  Exclusions tab with it selected; `press` claims a click where the stack
  is not empty; the rotate-zone guard `_other_shape_at` uses the stack. A
  still click (no transform change) whose press lies inside the selected
  shape is no longer swallowed by the transform release (a handle can
  cover a shape small on screen); a still click on a handle outside the
  shape still does nothing.
* **B25** (uncommitted): `shapes.py` `shift_held`; `tools.py` press / click
  treat Shift like Ctrl for a Select click (the overlay still passes Ctrl
  only: Step2's grid reads its own modifiers); Shift / Ctrl on the
  transform box unchanged. Hints in `page.py` and `numbering_panel.py`.
* Tests `tests/test_regions_click_stack.py` (7), driving press ->
  drag(finish) -> click -> released as the overlay does.
* Codex code review B24+B25: round 1 FAIL (P2 the still click swallowed;
  P2 Shift+click could not deselect; P3 per-axis slop), round 2 FAIL (P2
  the rotate-zone guard), round 3 PASS.
* **B18** (uncommitted): `engine_mount.py` `release_for_production(...,
  prepare=True)` (`prepare=False`: keep without the blocking prepare);
  `step0_explore_tab.py` `begin_release_for_production(keep_picture=...)`;
  `step0_page.py` `_begin_release_explore_for_production` (Save only)
  keeps the picture. After the run the saved product is a candidate core
  (the existing mechanism); the old picture stays until it is published.
  Test `tests/test_step0_save_keeps_picture.py` (GPU, real display): no
  prepare, nothing retired or given back to the coarse level; every frame
  after the run is the old picture whole or the saved product whole; a
  hand-off without keep still gives the picture back. Codex code PASS.
  Regression: Step0 files offscreen (2030 tests) -- first run found the
  test stubs of the Save hand-off with the old signature (the keep moved
  inside the Save-only method: fixed, no stub changed); GPU engine files:
  one timing test fails on HEAD as well (2 of 3 full runs).
* **15A follow-up** (found by B18's regression; 15A is pushed): three
  `tests/test_v16_rm2_chain.py` tests used a stand-in worker without 15A's
  `_cores_run` / `_requests_cores_run`; the stub now has them (False: a grid
  run). The file now matches HEAD (its 6 timeouts and 1 gc crash are HEAD's).
