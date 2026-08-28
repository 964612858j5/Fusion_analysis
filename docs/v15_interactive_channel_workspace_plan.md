# Block01 v15 — Unified Channel Workspace and Interactive Whole-Slide Preview

Status: design of record / implementation not started  
Date: 2026-08-29  
Target branch: a new v15 branch created from the accepted v14 baseline  

## 1. Decision summary

Block01 will first improve the existing large-tissue workflow. Development of the
TMA module is deferred until the v15 channel workspace and viewer foundation are
stable.

The agreed v15 direction has four parts:

1. Merge Step0 Background Correction and Channel Remap into one coherent channel
   conditioning workspace.
2. Use one fixed, reusable channel-sidebar component in Step0, Step1, and Step3.
3. Evolve the display system toward an Odon-inspired, viewport-driven whole-slide
   viewer with pyramid tiles, cancellation, caching, progressive refinement, and
   eventually GPU compositing.
4. Replace mandatory fixed-patch preview with free whole-slide navigation and
   viewport-local preview. Existing patches will be retained as optional pinned
   comparison locations rather than removed immediately.

This work constitutes v15. It must not be developed or pushed as additional work
on the current v14 branch. After this design is accepted, implementation should
start on a dedicated v15 branch from the accepted v14 baseline.

## 2. Scope and priorities

| Workstream | Scope | Priority | Delivery horizon |
|---|---|---:|---|
| A. Shared channel sidebar | One reusable channel dock and state contract for Step0/1/3 | P0 | Short term |
| B. Unified Step0 conditioning | One UI for background correction plus remap, with explicit source/order semantics | P0 | Short term |
| C. Interactive viewer foundation | Viewport tile provider, pyramid access, request scheduler, caches, progressive display | P1 | Medium/long term |
| D. Patch-to-viewport migration | Free navigation plus pinned comparison locations | P2 | After C is stable |
| TMA Study Mode | Dearray, study/core model, batch processing, cohort analysis | Deferred | After v15 foundation |

Recommended implementation order: **A -> B -> C -> D**.

Workstream A comes first because B, C, and D all need a stable channel control
surface. Workstream D must not precede C because the current preview path loads
patch arrays, not arbitrary whole-slide viewport tiles.

## 3. Workstream A — fixed shared channel sidebar

### 3.1 Goal

Step0, Step1, and Step3 should present the same channel-list structure, styling,
width behavior, selection behavior, colors, search, and visibility controls.

"The same component" means separate instances of the same reusable class and
shared state contract. A single QWidget instance must not be moved between pages.

### 3.2 Shared surface

The proposed reusable component is provisionally named `ChannelDock` and should
provide:

- channel search/filter;
- stable channel ordering;
- active-channel selection;
- visibility toggle;
- display color;
- source/status badges such as raw, corrected, remapped, fusion, or unavailable;
- fixed styling, spacing, width policy, and scroll behavior;
- a page-specific control slot.

Page-specific behavior remains outside the shared core:

- Step0: background-correction method and conditioning status;
- Step1: fusion group, inclusion, and weight;
- Step3: opacity, mask/label overlays, and QC controls.

The shared component must not force Step0, Step1, and Step3 to have identical
scientific semantics. It standardizes channel identity and interaction, while
each host supplies its own page-specific adapter.

### 3.3 Existing foundations to reuse

The current code already provides useful building blocks:

- `ui/widgets/channel_layer_list.py`;
- `ui/widgets/channel_workbench.py`;
- `ui/widgets/high_quality_image_viewer.py`;
- `ui/widgets/channel_viewer_canvas.py` as a compatibility wrapper.

The v15 task is to extract and stabilize their shared channel-state/UI contract,
not to create a fourth independent channel list.

### 3.4 Acceptance criteria

- Step0, Step1, and Step3 use the same sidebar class and visual style.
- Channel order, selected channel, color, and visibility have defined transfer
  rules between steps.
- Page-specific controls remain functional and do not leak into other pages.
- Existing v14 configs and sessions remain loadable.
- No processing algorithm or saved numerical output changes as part of this
  workstream.

## 4. Workstream B — unified Step0 channel conditioning

### 4.1 Goal

Step0 Background Correction and Channel Remap will become one channel-oriented
workspace rather than two separate tabs that require the user to infer the
relationship between their parameters.

The processing order remains explicit and deterministic:

```text
raw channel
    -> optional background correction
    -> channel remap (Min/Max, brightness, contrast, gamma)
    -> fusion / segmentation preview
```

"Combined" does not mean that both operations are mathematically simultaneous.
It means the user configures and previews the complete ordered channel pipeline
in one place.

### 4.2 Proposed layout

```text
+----------------------+------------------------------+------------------------+
| Shared ChannelDock   | Main preview                 | Active-channel inspector|
|                      |                              |                        |
| Search               | Raw / Corrected / Remapped  | Background correction  |
| Channel list         | / Fusion display modes      | Remap intensity        |
| Color / visibility   |                              | Histogram / Auto / Reset|
| Source/status badge  | Zoom / pan / navigator      |                        |
+----------------------+------------------------------+------------------------+
```

For each channel, the UI must disclose which intensity space feeds remap:

- correction disabled -> raw native intensity;
- correction enabled and completed/previewed -> corrected native intensity.

The remap Min/Max values must never silently move between raw and corrected
intensity spaces. A source change must trigger an explicit revalidation or
reseeding policy, with provenance recorded in the saved config.

### 4.3 Saving and provenance

The unified UI may expose one primary Save/Apply workflow, but correction and
remap remain independently identifiable artifacts with a reproducible dependency:

- correction parameters and corrected-source identity;
- remap parameters and the exact source intensity space on which they were
  calibrated;
- configuration hashes and dataset/channel identity;
- an explicit ordered-pipeline record.

Preview and production must share the same algorithms and parameter meanings.
Preview is a local execution of the production pipeline, not an unrelated display
filter.

### 4.4 Acceptance criteria

- Users can configure correction and remap without switching Step0 tabs.
- The preview can show raw, corrected, remapped, and fused states without losing
  zoom/pan.
- Saved configs identify the calibrated source per channel.
- Switching correction on/off cannot silently reuse invalid remap Min/Max values.
- Existing background-correction and remap numerical tests remain green.
- v14 output layouts remain readable or have a documented migration path.

## 5. Workstream C — interactive whole-slide viewer foundation

### 5.1 Reference and legal boundary

Odon is a useful architectural reference because it provides viewport-driven
OME-Zarr tile loading, GPU-backed high-plex compositing, overlays, and mosaic
viewing:

- https://github.com/alexcoulton/odon

Odon is licensed under GPL-3.0-only. Block01 may study its product and
architectural ideas, but must not copy, vendor, link, or translate Odon source
code unless the project deliberately accepts the resulting GPL obligations.

v15 should initially preserve the existing Python/PyQtGraph implementation and
change the data-loading and preview architecture before considering a language
rewrite.

### 5.2 Target architecture

```text
Navigator / pan / zoom / jump
              |
              v
       Viewport Manager
              |
      +-------+--------+
      |                |
      v                v
Pyramid Tile       Request Scheduler
Provider           debounce / cancel stale
      |                |
      +-------+--------+
              v
       Preview Pipeline
      +-------+--------+
      |                |
      v                v
background          raw source
correction          tile
with halo             |
      +-------+--------+
              v
       display compositor
   remap / weights / colors
              |
              v
            viewer
```

### 5.3 Meaning of "whole-image real-time preview"

The goal is **real-time preview at any location in the whole slide**, not
recomputation of every full-resolution WSI pixel whenever a slider moves.

Only the current viewport, at the appropriate pyramid level, plus the halo needed
by background correction should be processed.

- Remap, color, opacity, and fusion weights are pointwise/display operations and
  are candidates for a later GPU compositor.
- Gaussian subtraction and top-hat require neighboring pixels and therefore use
  a padded viewport tile followed by halo cropping.
- Zoomed-out correction may use a scale-adjusted parameter at a lower pyramid
  level; native-resolution settled preview remains the accuracy reference.

### 5.4 Required runtime behavior

- latest-request-wins cancellation;
- pan/zoom debounce;
- immediate raw/low-resolution display while processing;
- progressive low-resolution to settled-resolution refinement;
- LRU raw-tile and corrected-tile caches;
- cache keys include dataset, channel, tile, pyramid level, correction method,
  and correction parameters;
- parameter-aware invalidation: remap/weight/color changes must not invalidate
  raw or corrected tiles;
- no blank viewer while dragging;
- navigator jumps use full-resolution coordinates and select a suitable pyramid
  level automatically.

### 5.5 Preview and production contract

Preview and production may have different schedulers and scopes, but they must
share algorithm definitions, parameter units, source identity, coordinate rules,
and edge/halo semantics.

```text
same validated config
    +-> Preview Engine: viewport-local, cancellable, latency-oriented
    +-> Production Engine: full ROI/WSI, deterministic, complete, tiled
```

Any scale-aware approximation must be visibly labeled as interactive preview and
must refine to the production-equivalent result when the view settles at the
required resolution.

### 5.6 Technology order

Performance work should follow measurement:

1. viewport-driven architecture using current Python/PyQtGraph;
2. optimized OME-Zarr pyramid/chunk access and caching;
3. existing CuPy/cupyx paths for local correction;
4. GPU compositor/shader for remap and fusion;
5. custom CUDA or native extension only if profiling proves it necessary;
6. no planned Rust rewrite in the initial v15 scope.

## 6. Workstream D — remove mandatory patches, retain pinned comparisons

The current patch model must not be removed before Workstream C can supply
arbitrary viewport tiles. Existing code and caches assume a current patch array;
removing patches first would either force large full-image loads or break preview
source/coordinate guarantees.

The desired end state is:

```text
Primary exploration: current whole-slide viewport

Optional pinned comparisons:
  A — tumor
  B — edge
  C — normal
  D — necrosis
```

Pinned locations are optional scientific comparison anchors. They preserve the
useful ability to compare heterogeneous tissue regions while removing the rule
that tuning is possible only inside four preselected patches.

Acceptance criteria:

- users can pan, zoom, and jump anywhere without defining a patch;
- the current viewport drives correction/remap/fusion preview;
- up to a small configurable number of locations may be pinned for comparison;
- pinned locations store global coordinates and dataset identity;
- parameters remain channel-global unless explicitly designed otherwise;
- existing patch/session data can migrate to pinned locations;
- production processing remains ROI/WSI-based and deterministic.

## 7. Deferred TMA work

TMA Study Mode is intentionally deferred. The future module should reuse the v15
viewer, channel dock, viewport preview, pinned locations, and batch-safe processing
contracts.

The deferred TMA scope remains:

- dearray / image splitter with automatic detection and manual correction;
- map-sheet linkage and Study/Core/Cell metadata model;
- study-locked processing configurations;
- per-core batch orchestration and QC;
- cohort AnnData export, phenotype analysis, and within-core neighborhood analysis.

No TMA implementation should be mixed into Workstreams A or B.

## 8. Compatibility and scientific safeguards

- Do not change h5ad biological expression values through display remap.
- Do not silently mix raw, normalized, and corrected intensity spaces.
- Keep source identity and coordinate space explicit in every preview request and
  saved config.
- Preserve v14 session/config loading where practical; otherwise supply an
  explicit migration with tests.
- Existing Step2 source-aware remap guards remain intact unless separately
  reviewed.
- Viewer improvements must not alter saved production pixels without a deliberate,
  tested algorithm change.
- UI refactors must be separated from numerical-processing changes in commits and
  tests whenever possible.

## 9. Git and delivery policy

- The accepted v14 branch is the baseline and should receive only critical v14
  maintenance fixes.
- Create a dedicated v15 development branch before implementation.
- Do not push v15 work to the current v14 branch.
- Keep Workstreams A, B, C, and D in reviewable commits/series.
- Keep feature flags or compatibility paths for incomplete C/D runtime work until
  acceptance is complete.
- Do not flip production defaults solely because a prototype looks responsive;
  verify numerical equivalence, cancellation correctness, memory behavior, and
  representative WSI performance first.

## 10. First implementation milestone

The first v15 milestone should contain only:

1. a shared `ChannelDock` contract and reusable component;
2. adapters for Step0, Step1, and Step3 that preserve their existing semantics;
3. visual/state regression tests for channel ordering, selection, colors,
   visibility, and page-specific controls;
4. no viewer-backend rewrite and no numerical-processing changes.

After that milestone is accepted, Workstream B can assemble the unified Step0
Channel Conditioning workspace on top of the stable shared sidebar.

## 11. Confirmed UI contract (accepted 2026-08-29)

### 11.1 Sidebar visual reference

Visual and interaction reference (visual ideas only — not a code source, since it
is a Web implementation that does not map 1:1 onto Qt):

```
/sda1/Fusion/analysis_pipline/deepseek/step5_v8/agentic/montage_viewer_web.py
```

Adopted visual/interaction ideas:

- dark, fixed-width dock;
- top toolbar with search box, "Show all", "Hide all";
- compact, scrollable channel list;
- per-row: color swatch, visibility toggle, channel name;
- blue highlight on the selected row;
- three-column alignment for label / slider / numeric value;
- top tools stay fixed while the list scrolls independently.

### 11.2 Shared ChannelDock shell, page-specific content

One shared `ChannelDock`/`ChannelSidebar` shell class; each page instantiates its
own instance with page-specific row and editor content. The shared shell must NOT
become one giant class switched by `if step == ...`; the design is
shared shell + page-specific row/editor/controller.

Shared across all pages:

- visual shell (dock styling, width policy, spacing);
- search and filtering;
- visibility toggle and display color;
- selected-channel state;
- scroll behavior;
- channel ordering;
- the common channel data model and signal contract.

Per-page content:

**Step0 rows**: channel color; channel name; currently previewed background
method; user's final selected background method; status badge (e.g. Original,
Top-hat, cuCIM, selected / unsaved). **Step0 selected-channel tool area**: Min,
Max, Gamma; background-method parameters; the Compare entry point. Parameters are
NOT expanded inside every channel row — only in the selected-channel tool area.

**Step1 rows**: channel color; channel name; fusion weight slider; aligned
numeric weight input. Step1 shows NO background-correction method and does NOT
duplicate Step0's Min/Max/Gamma editors.

**Step3 rows**: visibility, color, and name only. **Step3 selected-channel tool
area**: Min, Max, Gamma, explicitly labeled **display/QC only**. Step3 display
parameters must never modify completed segmentation or biological quantification
results, and must never be written into processing/segmentation configs.

### 11.3 Shared channel state model

Fields of the shared per-channel state (signal contract follows these fields):

- channel identity (stable id + display name);
- visible;
- color;
- selected;
- display min / max / gamma;
- background preview method (Step0 semantic);
- background final (selected) method (Step0 semantic);
- fusion weight (Step1 semantic);
- semantic scope tag: `processing` vs `display-only` (Step3 uses display-only).

Pages consume only the fields in their semantic scope; the model carries all of
them so channel identity, order, color, visibility, and selection transfer
consistently between steps.

## 12. Compare mode contract (accepted 2026-08-29)

Future whole-slide interaction must not lose v14's background-method comparison
capability. Two viewer modes are defined.

### 12.1 Explore mode

- single window; free pan, zoom, and Navigator jumps;
- previews ONLY the final selected background method by default;
- drag smoothness has priority: debounce, cancel stale requests,
  latest-request-wins.

### 12.2 Compare mode

Entering Compare locks the current viewport by default and shows the SAME
location simultaneously in four synchronized views:

```
Original | Top-hat
cuCIM    | Final selected
```

All four views MUST share: global coordinates; viewport bbox; zoom; pyramid
level; channel selection; Min/Max/Gamma; colors; halo/crop rules. Panning or
zooming any one compare view synchronizes the others.

### 12.3 Compare scope

1. **Current viewport** — default when entering Compare.
2. **Navigator selection** — the user may draw an arbitrary rectangular
   Comparison ROI in the Navigator; drawing it switches Compare to that ROI.
   The ROI supports move, resize, clear, and redraw, and displays its
   whole-slide coordinates and size. Background algorithms may read an external
   halo, but only the selected rectangle is displayed.

Legacy Patch 1–4 are no longer a preview prerequisite; they are demoted to
optional pinned comparison locations / bookmarks.

### 12.4 Scheduling rules

Never recompute all background methods continuously during a drag:

- while dragging, show cached / raw / low-resolution results;
- after ~80–120 ms of rest, execute only the latest request;
- then refresh the four compare results.

### 12.5 Phase 1B deliverable (contract only)

Phase 1B establishes only the UI/state contract — no full OME pyramid, GPU
shader, or whole-slide background computation:

- Explore/Compare mode switch state;
- Current-viewport / Navigator-selection scope state;
- Comparison ROI data structure (whole-slide coordinates, not patch-local);
- interface for the 2×2 synchronized views;
- pinned-location data structure.

Mode switching must never modify production parameters.

