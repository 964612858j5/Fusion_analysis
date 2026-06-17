# 11 — Step2 Remap Integration Audit (Phase 5a)

Read-only architecture audit of the current Step2 / HQ2 / CDS2 / lean_carve call
chain, producing a precise integration plan for applying the v13.1 manual channel
remap in a future phase (5b). **No runtime code is changed by this phase.**

Architectural contract (from `09_CDS2_REMAP_ARCHITECTURE.md`):

```text
native/corrected marker channels
        ├── manual remap → 0–1 conditioning maps → signal gate / structural fusion
        └── existing _block_gi → local contrast maps → camp arbitration / mutual-exclusion
```

Manual remap replaces the **signal-gating** role; `_block_gi` local contrast is
**retained** for camp arbitration. Step3/Step2 align on the **pre-remap source**.

Line numbers are as of HEAD `9c27e14` and are approximate anchors, not contracts.

---

## 1. Current Step2 data flow

```text
ui/step2_page.py  (Step2Page: params UI, segmentation_params index/selection)
    ↓ seg_config dict
workers/segment_merge_worker.py  (orchestrator QThread)
    ↓ per-ROI, per-tile
  _prepare_tile_payload()                 (~1136)  builds tile payload
  _read_hq_marker_channels()              (~1460)  reads marker channels (native)
  _make_block_getter / lazy loader()      (~1516)  per-block native channel getter
    ↓ method dispatch (~1685)
  HQ2:        run_hq2_segmentation()       workers/hq2_marker_segmentation.py
  CDS2:       run_cds2_segmentation()      workers/cds2_segmentation.py
  lean_carve: _run_streaming()             workers/lean_carve_segmentation.py
    ↓ labels
  mask output (zarr/tiff) + QC tables + metadata
    ↓ (separate, later)
workers/feature_extract_worker.py          per-cell intensity → table/h5ad
```

Key classes/functions:

- `ui/step2_page.py::Step2Page` — assembles `seg_config` from the
  `segmentation_params` index; passes it to the worker. Does **not** know about
  remap configs today.
- `workers/segment_merge_worker.py` — the orchestrator. Resolves channels
  (`resolve_hq_channels`/`validate_hq_channels`), reads marker tiles, dispatches
  to the segmentation engines, writes masks/QC/metadata.
- `workers/feature_extract_worker.py` — independent pass; reads native channels
  and computes per-cell intensity for the feature table / h5ad.

---

## 2. Current marker channel source

- **Where:** `segment_merge_worker._read_hq_marker_channels()` (~1460) is the
  single marker read path. Per-block reads reuse it through the lazy loader
  closure (`_make_block_getter`, ~1516–1528).
- **Corrected preferred:** reads go through `OMETIFFLoader.read_region`
  (`core/io_loader.py:77`), which returns the **corrected-zarr native float**
  when the channel is in `corrected_channels.zarr` / corrected decisions, else
  the raw OME page. So corrected is preferred, raw OME is the fallback — exactly
  the `step2_pre_remap_source` reported by Step3
  (`docs/.../10_STEP3_STEP2_SOURCE_ALIGNMENT.md`).
- **normalize=False:** marker reads use `normalize=False`
  (`segment_merge_worker.py:1475–1486,1496`; mesmer source 1551–1553; feature
  extract `feature_extract_worker.py:265–270`). → **native float**, no display
  normalization at read time.
- **Per tile/block:** yes. Tiles via the tile scheduler; lean_carve/CDS2 stream
  per **block** through `get_block(name, sy0,sy1,sx0,sx1)` — one native read per
  channel per block.
- **Display normalization:** none on the native path. The only normalization is
  `_normalize01()` (static, ~1556; percentile 1/99.8 on >0 pixels) and HQ2's
  `percentile_normalize()`, applied **inside** the engines for fusion/gating —
  not at source read. This is the operation manual remap will replace/augment.

---

## 3. Current HQ2 / CDS2 / lean_carve inputs

### HQ2 — `workers/hq2_marker_segmentation.py`

- Entry `run_hq2_segmentation()` → `run_level1_hq_proposal(nuclei,
  marker_channels, channel_names, ...)` (~137).
- **Input arrays:** a list of native marker channel arrays (`marker_channels`,
  from `_read_hq_marker_channels`, normalize=False) + nuclei labels.
- **Normalization/correction state:** native at entry; HQ2 applies its own
  `percentile_normalize(arr, norm_low, norm_high)` (~159) then
  `watershed(-norm, markers=nuclei, mask=...)` (~162).
- **Where markers enter:** the `marker_channels` list argument. HQ2 has **no
  camp / no `_block_gi`** (grep: 0 hits) — it is nucleus-seeded marker watershed.
  Remap insertion here is simple: replace/precede `percentile_normalize`.

### CDS2 — `workers/cds2_segmentation.py`

- Entry `run_cds2_segmentation(nuclei_labels, marker_channels_loader,
  channel_names, params, ...)` (175) → `_run_streaming(..., carve_fn=
  _make_cds2_carve(p))`.
- **Input arrays:** native blocks via `get_block(name, ...)` (carve, ~83).
- **Where `_block_gi` is computed:** inside the carve, per channel per block:
  `gi, uc = _block_gi(raw, terr_mask, gi_k, gi_bg_k, use_gpu)` (cds2:86;
  `_block_gi` defined `lean_carve_segmentation.py:90`). `gi` is the **local-z**
  contrast map.
- **Signal gate today:** `keep = _channel_keep(gi, terr_mask, outside, faces,
  protect, tau)` (cds2:88; lean_carve:121) — i.e. the gate is currently derived
  from `gi`, not from a remap.
- **Where camp arbitration uses local contrast:** `camp_z[cid]` is the per-camp
  max of the **same `gi`** (cds2:95). Ring fingerprint + pixel fingerprint +
  veto (cds2:104–163) all run on `best_z`/`camp_z` derived from `gi`. Thresholds:
  `tau`, `cds2_area_frac`, `cds2_strength_lo/hi` (cds2:61–63).
- **Crucial:** today `gi` serves **both** the signal gate and camp. Remap must
  take over the gate while `_block_gi`/`gi` stays for camp.

### lean_carve — `workers/lean_carve_segmentation.py`

- `_run_streaming()` (210) builds `get_block = _make_block_getter(...)` (247) and
  drives the per-block carve. Default carve uses `_block_gi` → `_channel_keep`
  (gate) with **no camp** (it is the "greedy union" CDS2 fixes).
- **Input arrays:** native blocks via `get_block`.
- **Gi/hotspot behavior:** the signal gate is `_channel_keep(gi, …, tau)`; there
  is no separate hotspot module — `gi` (local-z) *is* the gate statistic.

---

## 4. Where manual remap should be applied (proposed, not implemented)

The single native block read is the natural fan-out point. In CDS2/lean_carve
that is `raw = get_block(name, …)` inside the carve; in HQ2 it is the
`marker_channels` list before `percentile_normalize`.

```text
get_block(name) → native/corrected marker block  (read ONCE)
        ├── apply_channel_remap(raw, remap_cfg[name]) → 0–1 conditioning map
        │        → signal gate / structural fusion  (replaces gi-based keep)
        └── _block_gi(raw, …) → gi (local-z)
                 → camp_z / camp arbitration / mutual-exclusion   (UNCHANGED)
```

Concrete insertion points:

- **CDS2 / lean_carve:** inside the carve closure, right after
  `raw = get_block(name, …)`. Keep `gi = _block_gi(raw, …)` for `camp_z`
  **verbatim**; add `cond = apply_channel_remap(raw, cfg[name])` and derive the
  gate from `cond` (e.g. replace/AND with `_channel_keep`). Do **not** feed
  `cond` into `_block_gi` (doc 09 §4: no local-z after remap).
- **HQ2:** replace/precede `percentile_normalize(arr, …)` in
  `run_level1_hq_proposal` with `apply_channel_remap(arr, cfg[name])` (already
  0–1). No camp concerns here.
- **Operator:** `core/channel_remap.py::apply_channel_remap` (backend-agnostic,
  no Qt/IO) — already exists and is unit-tested.

This is a documentation of the insertion point only. **Do not implement now.**

---

## 5. Dual-path I/O and compute cost

- **Same tile twice?** No — if remap is inserted at the carve level. `get_block`
  already returns the native block **once**; both `apply_channel_remap(raw)` and
  `_block_gi(raw)` consume that same in-memory `raw`. No second disk/zarr read.
- **Shared read:** the existing block-streaming design (`_run_streaming` +
  `_make_block_getter`) is already the shared-read point. The cost added by remap
  is one extra per-block elementwise transform (window→contrast/brightness→gamma)
  — cheap relative to `_block_gi` (local mean/std) and geometry.
- **Where caching:** none new required for CDS2/lean_carve (per-block, discarded).
  For HQ2 (whole-tile `marker_channels` list), remap is applied once per tile in
  memory — also no extra read. `utils/channel_cache.py` already memoizes raw-OME
  region reads; remap output should **not** be cached there (it is config-derived
  and cheap; caching it would risk staleness across config edits).
- **Future optimization:** if a non-carve path ever reads a channel separately
  for remap and for `_block_gi`, unify on the single `get_block` fan-out (doc 09
  §6). Current design already satisfies this if insertion stays at carve level.

---

## 6. Remap config loading (proposed)

Where future Step2 should load `channel_remap_config.json`
(`utils/channel_remap_config.py::load_channel_remap_config`, which validates):

- **Step2 UI:** add an optional "Channel remap config" file selector in
  `ui/step2_page.py`, written into `seg_config["channel_remap_config_path"]`.
  Empty = current behavior (no remap), preserving back-compat.
- **segmentation params:** persist the path (and ideally a content hash) in the
  `segmentation_params` entry so a run is reproducible from the index alone.
- **Passed into workers:** `segment_merge_worker` loads + validates once per run,
  then passes the resolved per-channel params dict into
  `run_hq2_segmentation` / `run_cds2_segmentation` / `_run_streaming` (new
  optional kwarg, default None = no remap).
- **Output provenance:** copy the resolved config into the run output dir and
  record it in run metadata (and later `adata.uns["segmentation_preprocess"]`,
  doc 05). Never silently apply a remap without recording which config produced
  the mask (doc 05: "Min/Max/Gamma changes change masks").

All proposals — **do not implement now.**

---

## 7. Step2-ready source-policy validation (proposed)

Before running with a remap config, future Step2 should validate
(`validate_channel_remap_config` plus Step2-specific checks):

```text
used_for == "segmentation_only"                            (hard fail otherwise)
channels contain no reference layers (DAPI/mask/fusion)    (hard fail otherwise)
per-channel intensity_space == actual Step2 pre-remap source for that channel
per-channel calibration_source_matches_step2 == true
source_policy.calibration_source_matches_step2 == true     (all markers aligned)
remap min/max present and max > min per channel            (already validated)
```

**Recommendation on the preview_only vs step2_ready gate:**

Require `calibration_source_matches_step2 == true` (top-level **and**
per-channel), but **accept `preview_only == true` / `step2_ready == false`** for
the first integration, behind an explicit experimental flag
(e.g. `seg_config["allow_preview_remap"] = true`).

Rationale: `step2_ready` can only become `true` once Step2 actually consumes the
config — a chicken-and-egg if we require it before integration exists. Source
alignment (`calibration_source_matches_step2`) is the property that actually
guarantees the saved Min/Max live in Step2's intensity space; that is the real
safety gate. After 5b lands and is validated, a follow-up phase can define the
semantics that let a config be promoted to `step2_ready=true` (and flip
`preview_only=false`), and Step2 can then require it for non-experimental runs.

Until then: **experimental runs gate on source match; `step2_ready` stays false.**

---

## 8. h5ad / feature-extraction guard

Principle (doc 05):

```text
manual remap affects segmentation input only.
feature extraction / h5ad defaults to raw or biologically corrected intensity.
remapped 0–1 intensity must NOT become adata.X.
```

Current state is already safe and must stay that way:

- `workers/feature_extract_worker.py` reads its **own** channel source via
  `loader.read_region(ch, …, normalize=False)` (~265–270) and computes
  `nd_mean`/`nd_sum`/… per label (~277–290). It never receives the segmentation
  engine's remapped arrays — the two passes are decoupled.
- **Guard for 5b:** do **not** plumb remapped marker arrays into
  `feature_extract_worker`. Keep its source = native/biologically-corrected
  (corrected-zarr via `read_region`, normalize=False). Optionally add remapped
  per-cell means only as a **non-default** layer
  (`adata.layers["segmentation_remap_mean"]`) and write the remap config to
  `adata.uns["segmentation_preprocess"]` — never to `adata.X`.
- Files where the guard matters: `workers/feature_extract_worker.py` (source of
  X), and wherever the h5ad/AnnData is assembled downstream (must assert X comes
  from the native/corrected pass).

---

## 9. Minimal safe Phase 5b implementation plan

Smallest safe sequence (each a small commit; runtime-gated so absent config =
today's behavior):

1. **Config plumbing (no effect):** Step2 UI optional remap-config selector →
   `seg_config["channel_remap_config_path"]`; load+validate in
   `segment_merge_worker` (per §7); pass resolved params dict into engines as an
   optional kwarg defaulting to None. With None, behavior is byte-identical.
2. **HQ2 remap gate:** in `run_level1_hq_proposal`, when remap params present,
   use `apply_channel_remap(arr, cfg[name])` instead of `percentile_normalize`.
   Guard behind the experimental flag.
3. **CDS2 / lean_carve remap gate:** in the carve, after `raw = get_block(...)`,
   add `cond = apply_channel_remap(raw, cfg[name])` and derive the gate from
   `cond`. Keep `gi = _block_gi(raw)` and all camp logic **unchanged**.
4. **Provenance:** copy resolved config into run output + run metadata.
5. **No h5ad change:** feature extraction untouched; assert X stays native (§8).
6. **Benchmark hooks:** reuse `06_BENCHMARK_PLAN.md` region-stratified compare
   (baseline top-hat/cuCIM vs remap gate), no algorithm change to camp.

Explicitly out of 5b: changing `_block_gi`, camp thresholds, top-hat/cuCIM
removal, h5ad expression source, napari.

---

## 10. Open questions / risks

- **Source mismatch:** a config saved on a mismatched source must be refused
  (§7). The per-channel `calibration_source_matches_step2` (Phase 2.1d) is the
  gate; trust it, hard-fail otherwise.
- **Per-channel fallback:** `per_channel_native` mixes (corrected + raw-native)
  are valid; `partial_or_preview_fallback` (any normalized/unknown marker) must
  be rejected for runs. Surface the offending channel + `fallback_reason`.
- **Tile-level Auto must not happen:** Auto percentiles are frozen into config at
  calibration (doc 03 §"Auto scope rule"). Step2 must apply fixed Min/Max only;
  it must never recompute Auto per tile/block (non-stationary, seams).
- **Dual-path memory/I/O:** must keep insertion at the `get_block` fan-out so the
  native block is read once and feeds both remap and `_block_gi` (§5). A naive
  second read per channel would double tile I/O.
- **AF-heavy regions:** remap is a global intensity gate; spatially-uneven AF is
  exactly where `_block_gi` local contrast still earns its keep (doc 09 §4).
  Benchmark must stratify AF-heavy regions; do not remove the local-contrast
  defense.
- **Camp threshold isolation:** `tau`, `cds2_area_frac`, `cds2_strength_lo/hi`
  are calibrated on native `_block_gi` z-space and must **not** auto-change with
  remap Min/Max/Gamma (doc 09 §5). Keep the two configs decoupled.
- **top-hat/cuCIM coexistence:** during the experiment both paths may exist for
  baseline-vs-remap comparison (doc 01 §3: legacy not deleted). Ensure the remap
  gate and any legacy gate are mutually exclusive per run, selected by config —
  never silently stacked.
- **HQ2 vs CDS2 asymmetry:** HQ2 has no camp; CDS2 does. Remap semantics differ
  (HQ2 = watershed input; CDS2 = gate beside camp). Validate/benchmark them
  separately.

---

## Non-goals (this phase)

No runtime code changed. No remap consumption, no HQ2/CDS2/lean_carve edits, no
`_block_gi`/camp changes, no feature-extraction/h5ad changes, no top-hat/cuCIM
removal, no napari. Documentation only — the implementation contract for Phase 5b.
