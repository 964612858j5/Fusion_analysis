# v15 Step0 Explore integration — one-page design (for review)

Status: design for review / implementation not started
Date: 2026-08-30
Depends on: `v15_viewer_foundation_interfaces.md`, handle-mode benchmark
(`docs/benchmarks/2026-08-30_tonsil_handle_modes.md`).

Scope: ONE Explore window inside Step0's Background Correction workspace,
driven by the existing viewer stack (provider/assembler/scheduler/caches).
Deliberately light: pyqtgraph only, no OpenGL custom renderer, no Compare
2×2 yet, no changes to correction math or configs.

## 1. Three display layers (one pyqtgraph ViewBox, three ImageItems)

```
Layer 2 (top)    precise tiles     corrected (or raw pre-Save) tiles at the
                                   display level, generation-checked,
                                   flicker-free replacement
Layer 1          raw quick-fill    raw tiles of the current level, shown the
                                   moment they land, under layer 2
Layer 0 (bottom) pinned overview   whole-slide coarse level (smallest pyramid
                                   level ≥ ~1k px), loaded EAGERLY at dataset
                                   open, never evicted -> never a black screen
```

- Layer 0 is one static full-extent image per channel mix; loaded once
  (~490×450 px for the tonsil L3) before the window is interactive.
- Layers 1–2 are mosaics: one numpy canvas per layer sized to the current
  viewport's tile cover; tiles are blitted into the canvas and the
  ImageItem updated at most once per frame tick (a 16 ms QTimer coalesces
  arrivals — no per-tile setImage storms).
- Zoom picks the display pyramid level by nearest resolution (like the
  benchmark); level switches clear layers 1–2 canvases but layer 0 always
  covers the gap.

### 1.1 One world coordinate system (review addition)

All three ImageItems live in the SAME full-resolution WSI pixel coordinate
system. Every layer/tile image is placed via an explicit rect/transform:
an image at pyramid level L with top-left (y0, x0) in level-L pixels is
drawn at rect (x0*ds, y0*ds, w*ds, h*ds) where ds = level_downsample(L).
Zoom-driven level switches therefore never shift the image, and Navigator
jumps address level-0 coordinates directly.

### 1.2 Precise-layer identity (review addition)

Each blitted precise tile stores its full CorrectionKey (source identity,
channel, method, params, level, quality). A displayed tile counts as a
VALID precise result only while its key matches the CURRENT selection.
On any change of channel / method / params / source (e.g. a Save):
old precise pixels may remain on screen briefly but the view immediately
enters a visible "updating…" provisional state (and the raw layer shows
through where possible); they are never presented as current results.
Flicker-free overwrite applies only within one unchanged key context.

## 2. Interaction loop (ExploreController)

- ViewBox sigRangeChanged → update view_generation, recompute the visible
  tile set (`tiles_covering`), and immediately:
  - request RAW tiles for cache misses (they fill layer 1 as they arrive);
  - draw whatever layers already hold (never blocked).
- Settle timer (~80 ms after the last range change) → issue the precise
  settled request set: CorrectionKeys for the final selected method of the
  active channel(s) at INTERACTIVE quality for the display level
  (`effective_param`), via the existing scheduler (staging + shared caches).
- Delivery: results carry the request generation; the controller drops
  results whose settled generation is stale (`cancel_generation` on each
  new settle). Accepted tiles blit into layer 2 with no intermediate clear
  — old precise pixels stay until the new ones overwrite them (flicker-free
  by construction).
- Navigator / checkpoint jump = same path with the settle request issued
  immediately (no 80 ms wait).

## 3. Data / lifecycle contract

- Source: `Step0PreviewSourceProvider` semantics are respected — Explore
  previews the FINAL SELECTED method only (Explore mode contract §12.1);
  channels without a single saved/assigned method render raw with the
  existing honest label.
- One stack per dataset: RawTileProvider(handle_mode="per_thread"),
  RawTileAssembler, CorrectionCompute, TileScheduler(io_workers=1,
  compute_workers=1), caches raw 512 MB / corrected 512 MB (byte budgets
  enforced; benchmark showed zero evictions at viewport scale).
- Teardown (dataset switch / window close), strictly ordered:
  `scheduler.shutdown()` (joins workers) → `provider.close()` → drop
  caches. Reads after close raise (pinned by test).
- Explore is ADDITIVE UI: mounted as a new "Explore (preview)" tab or
  button in the BG workspace; the existing patch preview stays untouched.
  No production parameter is written by anything in this design.

## 4. G1-render measurement (the actual gate)

A small instrumented harness (offscreen-unfriendly, so measured in a real
session) records, during scripted pan/zoom sequences:
- frame prep time p50/p95 (range-change handler + blit + setImage),
- dropped-frame count vs the 16.7 ms budget,
- time-to-first-pixel on jump (layer 0), raw fill latency (layer 1),
  precise fill latency (layer 2).
Numbers go into docs/benchmarks as G1-render vX — measured-only; only then
may "smoothness" claims be made. If pyqtgraph's setImage cannot hold the
frame budget at viewport mosaic sizes, THEN we evaluate a GL path — not
before.

## 5. Explicit non-goals (this phase)

Compare 2×2; multi-checkpoint remap compare; idle prefetch (after G1-render
baseline); OME-Zarr display cache; OpenGL renderer; any change to Save,
configs, or correction numerics.

## 6. Test plan

- Controller unit tests (offscreen Qt): tile-set recompute on range change;
  settle debounce fires once; stale-generation results dropped; layer-2
  blit never clears before overwrite (canvas diff); teardown order enforced
  (mock scheduler/provider record call order).
- Reuse of existing 41 viewer tests unchanged.
- Manual acceptance: user loads the tonsil WSI, pans/zooms/jumps; then the
  scripted G1-render measurement run.
