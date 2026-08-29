# v15 Viewer Foundation — interface design (step 2 of plan §13.5)

Status: design for review / implementation not started
Date: 2026-08-29
Depends on: `v15_interactive_channel_workspace_plan.md` §13 (decisions of record)

Scope: the typed contracts for the viewport-driven viewer plane and the
correction compute layer. Pure contracts — no tile loading, no GPU code.
Everything here is sized for the benchmark prototype (§13.4); names are
final, numeric defaults are provisional until benchmarked.

## 1. Object model

```
Qt UI (Explore / Compare / Navigator / checkpoints)
        │ pan/zoom/jump
        ▼
ViewportController ── owns ViewportGeneration
        │ wanted tiles (canonical grid ∩ viewport, center-first)
        ▼
RequestScheduler ── priority · dedup · cancellation · budgets
        │                                   │
        ▼                                   ▼
RawTileProvider (pyramid I/O)      CorrectionCompute (tile+halo, GPU)
        │                                   │
   RawTileCache (CPU LRU)          CorrectedTileCache (LRU, CorrectionKey)
        └───────────────┬───────────────────┘
                        ▼
              TextureCache (GPU) → Display compositor
              (min/max/gamma/color/weights = display-side only,
               never invalidates the caches above)
```

## 2. Identity types

### SourceIdentity
Identifies WHAT pixels mean. Any field change ⇒ every cache below is cold.

```python
@dataclass(frozen=True)
class SourceIdentity:
    dataset_path: str          # OME-TIFF / OME-Zarr path (resolved, absolute)
    dataset_fingerprint: str   # size+mtime hash or OME UUID
    stage: str                 # "raw" | "corrected_saved"
    corrected_artifact: str | None   # zarr path + config hash when stage=corrected_saved
```

### TileGridSpec / TileAddress
Canonical grid. Tile size is a per-session experiment parameter (benchmark
compares 256/512/1024; prototype default 512 — 256 refills edges faster but
multiplies requests and halo overhead, 1024 lowers request count but raises
first-compute and cancellation cost).

```python
@dataclass(frozen=True)
class TileGridSpec:
    tile_size: int             # prototype default 512
    source_chunk_shape: tuple  # underlying zarr/TIFF chunking (alignment)
    grid_version: str          # bump when the gridding rule changes

@dataclass(frozen=True)
class TileAddress:
    grid: TileGridSpec
    level: int                 # pyramid level, 0 = full resolution
    tx: int                    # tile column (x // tile_size at this level)
    ty: int                    # tile row
```

### CorrectionKey
Cache key for a corrected tile. Equality = reusable result.

```python
@dataclass(frozen=True)
class CorrectionKey:
    source: SourceIdentity
    channel: str
    tile: TileAddress
    method: str                # "tophat" | "cucim"
    params: tuple              # method params, canonical order, ints
    algorithm_version: str     # bump on any numeric change of the method impl
    boundary_mode: str         # halo/edge semantics, e.g. "halo_crop_reflect"
    quality: str               # QualityLevel (interactive params are level-scaled)
```

### QualityLevel

```python
INTERACTIVE = "interactive"  # computed at displayed level, scale-adjusted
                             # params; labeled approximation in the UI
NATIVE      = "native"       # level-0-local; numerically aligned with production
PRODUCTION  = "production"   # full deterministic tiled run; writes artifacts;
                             # NOT served by this scheduler (separate engine)
```

Rule: NATIVE must pass golden tests proving alignment with PRODUCTION within
a stated tolerance (same dtype, halo, edge padding, algorithm implementation
and crop; float error bound declared per method). "Numeric equality" is never
claimed unconditionally. INTERACTIVE results must carry the approximation
label through to the UI.

### Generations (two, not one)
Owned by ViewportController:

- **view_generation** — bumped on EVERY effective camera change (each drag
  frame counts). The render path uses it to draw whatever caches already
  hold for the current camera.
- **settled request (precision generation)** — issued when the camera rests
  (~60-120 ms) or on an explicit jump; enqueues precise compute for that
  viewport. A precise result is delivered to the UI only if its viewport
  still matches; otherwise it is dropped FROM DELIVERY but still lands in
  the caches (its identity keys are camera-independent).

```python
@dataclass(frozen=True)
class ViewportState:
    view_generation: int
    bbox: tuple[float, float, float, float]   # x0, y0, w, h in level-0 pixels
    level: int                 # chosen pyramid level for display
```

## 3. Request / result

```python
@dataclass(frozen=True)
class TileRequest:
    key: CorrectionKey | RawKey   # RawKey = (source, channel, tile)
    generation: int            # delivery token only — NOT part of dedup/cache
    priority: int              # 0 = visible-center … higher = prefetch ring
    deadline_ms: int | None    # advisory; scheduler may drop past-deadline work

@dataclass(frozen=True)
class PixelBuffer:
    residency: str             # "cpu" | "cuda" | "gl"
    dtype: str
    shape: tuple
    handle: object             # np.ndarray / cupy array / GL texture id
    # prototype uses residency="cpu" (NumPy); the interface does not force a
    # GPU->host round-trip, keeping CUDA-GL interop possible later

@dataclass
class TileResult:
    request: TileRequest
    pixels: "PixelBuffer | None"   # halo already cropped; None on error
    quality: str               # QualityLevel actually delivered
    provisional: bool          # True when a coarser stand-in was substituted
    timing: dict               # io_ms, h2d_ms, kernel_ms, d2h_ms (benchmark hooks)
    error: str | None
```

## 4. Scheduler contract

- **Dedup by identity key ONLY** (CorrectionKey/RawKey — never generation):
  a tile in flight serves every generation that wants it; results always
  enter the caches under their camera-independent identity, so a small pan
  never recomputes the same tile.
- **Generation = delivery token**: it gates only which UI views receive the
  result, never whether the computation runs or is cached.
- **Cancellation**: `cancel_generation(gen)` drops queued not-yet-started
  work wanted only by stale generations; running GPU work completes and its
  result lands in the caches (delivery to stale views is skipped).
- **Priority**: visible tiles center-out first; visible channels only.
  Budgets (bounded queues with backpressure, plus a byte budget on queued
  I/O): in-flight I/O provisional 4 (verify the OME-TIFF reader's
  concurrency safety first); compute = **1 active GPU pipeline + 1 queued
  ready** — promote to 2 active only if benchmarks show dual CUDA streams
  actually overlap transfer/compute.
- **Prefetch**: OFF for the baseline benchmark. When enabled later: only on
  idle, only visible channels, direction-of-motion first, hard tile/byte
  budget, at most one ring beyond the viewport.
- **Adaptive cadence** (replaces fixed 100 ms): during fast drag serve only
  cache hits + coarse fallback; on settle (~60-120 ms, tunable) issue the
  precise settled request; Navigator/checkpoint jumps issue immediately;
  param edits use a short debounce (~150 ms, existing).
- **Fallback chain** on cache miss (honest about cold start):
  pinned overview/thumbnail level (loaded eagerly at dataset open) →
  coarser cached tile (corrected, else raw) → target raw → neutral
  placeholder + loading indicator. "Never blank" holds only AFTER the
  pinned overview is resident; dataset open must therefore load it first.

## 5. Cache contract

| Cache | Key | Evict | Notes |
|---|---|---|---|
| RawTileCache (CPU) | (source, channel, tile) | LRU by bytes | decode output, float32 |
| CorrectedTileCache | CorrectionKey | LRU by bytes | center tile only (halo cropped) |
| Background intermediate (optional, post-benchmark) | key minus subtraction step (e.g. per-sigma blur) | LRU | makes sigma-only edits cheap |
| TextureCache (GPU) | pixel identity key + dtype/upload format + backend identity | LRU by VRAM budget | camera generation and display params (min/max/gamma/color/weights) are NOT part of texture identity — they apply at draw time, so pan/zoom reuses textures |

Invalidation rules:
- remap/color/weight changes invalidate **nothing** here (display-side);
- correction param/method change invalidates CorrectedTileCache entries whose
  key differs (i.e. nothing is *deleted*; new keys simply miss);
- Save/artifact change ⇒ new SourceIdentity ⇒ all caches naturally cold;
- explicit `clear(source)` for dataset switch.

## 6. Compare (2×2) contract

- One camera / ViewportGeneration, one scheduler, one raw cache, one
  corrected cache, one texture pool for all four cells.
- Cells = {original, tophat, cucim, final}; original = raw reference,
  final = alias of one of the other three ⇒ at most 2 unique corrections.
- **Atomicity**: a cell set may display only (a) the last complete
  generation, or (b) the new generation with cells individually marked
  provisional. Mixed positions without marking are forbidden. The switch to
  "complete" happens when all four cells have non-provisional results for
  the current generation.
- Remap multi-checkpoint compare reuses the same machinery: same channel,
  shared remap params, one cell per pinned checkpoint (≤4), stage =
  corrected_saved.

## 7. Benchmark prototype (step 3) — acceptance gates

Measure on the real machine, real OME dataset, cold and warm caches, tile
sizes 256/512/1024, typical radius/sigma ranges, levels L0–L2, with per-stage
timing from `TileResult.timing` (io / H2D / kernel / D2H+texture):

- G1: drag at 60 FPS render (cache-hit path) — hard gate.
- G2: settle→precise single-method viewport fill; record, don't promise.
- G3: Compare fill (2 unique methods); record.
- G4: peak RAM/VRAM within budget on the largest real slide.

Outcomes decide: tile size, halo strategy, background-intermediate cache,
CUDA–GL interop, custom kernels. If G1 fails in pyqtgraph, escalate the
display path (GL widget) before touching compute.

## 8. Explicit non-goals

No Odon code copying (GPL-3.0), no Rust rewrite, no full-slide recompute on
slider moves, no production writes from the preview scheduler, no change to
saved artifact formats or Step2 validation semantics.
