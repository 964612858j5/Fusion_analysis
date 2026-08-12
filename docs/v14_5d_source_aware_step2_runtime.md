# v14.5d — Source-aware Step2 runtime + Step0 remap auto-apply (design, for review)

Status: **rev4** — A + B2-core + B1 + B3a + B3b (descriptor accept, worker re-resolve
+ exact cross-check) implemented & offscreen-tested (all local, flag default off,
guard intact, no live path). Remaining: B3c (run-loop wiring + per-tile per-channel
read) and B3d (launch orchestration + UI). rev2 incorporated the first ChatGPT review
(DAPI stays
in the Step0 save; promote only selected markers; B1 not shippable alone; ROI →
v14.5e; explicit applied/not-applied surfacing). rev3 added the two projection
invariants (coverage / mixture recompute), the B3 input-mode gate, and the
ENABLE_STEP2_SOURCE_AWARE_REMAP_RUNTIME flag.

## 1. Goal

Make "user saves a Step0 Channel Remap → HQ2/CSD segmentation automatically uses the
remapped channels" work for the **real** Step0 config (source-aware, many channels,
includes DAPI). Today it stably **skips** (no crash, runs un-remapped).

**Scope is narrow and must be stated as such:** this enables the **HQ2/CSD
selected-marker** path only, **homogeneous** source (all raw OR all corrected),
**full-image** runs only. It is NOT "all algorithms auto-remap":
- Cellpose whole-cell / DAPI / StarDist already get the remap via the Step1
  remapped fused/DAPI input (unchanged).
- Plain HQ / Mesmer prior apply-logic stays dormant under these promotion constraints.
- ROI runs are safe-skipped (→ v14.5e).

## 2. Why it skips today (verified)

The real saved config (`<roi>/step0/step0_channel_remap.json`, 29 channels) is
`config_is_source_aware = True` and includes a `DAPI` channel. Two independent guards
refuse it even with a perfect source:

1. **Source-aware hard guard** — `channel_remap_config.py:396`:
   `config_is_source_aware(cfg) and step2_ready==true` → refuse.
2. **DAPI reference-layer rejection** — `channel_remap_config.py:406`,
   `_is_reference_channel_name` (`:334`).

Pinned by `tests/test_step2_remap_integration.py::
test_promotion_refuses_realistic_source_aware_dapi_config`.

## 3. What already exists (5b/5c — do NOT rebuild)

- **Candidate promotion** `remap_promotion.py:330` `promote_source_aware_config_for_step2`
  / `:476` `promote_source_aware_from_sources`. Full per-channel recorded↔resolved
  identity + resolved↔runtime geometry, no-partial. Emits a validated **CANDIDATE**
  (`step2_ready=false`, `runtime_supported=false`, `source_alignment_mode=per_channel_native`,
  separate `source_mixture_mode`, per-channel `resolved_source_kind/path/group_name/shape`).
  **NOTE:** `promote_source_aware_from_sources` iterates `for ch in channels` over **every**
  config channel (`:497`) — feeding the full 29-channel config requires all 29 to resolve
  and still carries DAPI. This is why §5-A (marker-only projection) is mandatory.
- **Per-channel resolver** `hq_source_resolver.py:532` `resolve_per_channel_marker_sources`:
  `{channel → raw_ome|corrected_zarr}` → `PerChannelResolvedSource`
  (`.per_channel[ch]=ResolvedHQSource`, `.source_mixture_mode ∈ {homogeneous_raw,
  homogeneous_corrected, mixed_raw_corrected}`, no-partial: raises `PerChannelResolutionError`).
- **Candidate validator** `channel_remap_config.py:513`. **CLI** `scripts/promote_remap_config.py:221`.
- **Tests** `test_source_aware_promotion.py`, `test_per_channel_resolver.py`,
  `test_per_channel_resolved_source.py`, `test_source_identity_schema.py`.

## 4. The gap (what v14.5d must build)

The candidate stamps per-channel `resolved_source_*`, but **no Step2 consumer reads them**.
The chain is single-source: `segment_merge_worker._resolve_channel_remap` injects one
`_channel_remap_params` (transform values only); markers come from one
`_hq_resolved_source_path` → one `get_block` → source-blind `apply_channel_remap`
(`core/channel_remap.py:67`). HQ2 (`hq2_marker_segmentation.py:35`), lean_carve (`:42`),
CDS2 (`cds2_segmentation.py:86,111`) all read from one shared source.

## 5. Design

### Workstream A — Step2 marker-only projection  [do first; the input-shaping step]

**DAPI stays in the full Step0 saved config.** Step1 whole-cell/DAPI/StarDist remap and
the DAPI-input-zarr cache hash (`main_window._expected_dapi_input_meta`,
`FullFusionWorker._channel_norm` on the nucleus channel) both read the DAPI entry from
`_load_step0_remap_params()`. Removing DAPI at save would REGRESS those. The stale
"DAPI is reference-only" comment in `channel_workbench.py:798` is contradicted by the
actual save (which includes DAPI) — do not act on it.

Instead, build a **derived marker-only config** at Step2 promotion time
(`project_marker_only_config(saved_config, selected_channels)`), with two hard
invariants:

- **Invariant 1 — no silent drop (coverage).** Compute
  `selected_non_reference = selected_channels − reference_names`. If
  `selected_non_reference − saved_channels ≠ ∅`, **refuse** with an explicit
  `uncovered-marker` reason. Never rely on intersection alone — an intersection would
  silently drop a selected marker that is absent from the saved config, violating "every
  selected marker must be covered". `marker_channels = selected_non_reference` (all present
  once the refusal above passes).
- **Invariant 2 — recompute mixture from the selected set.** The projected config MUST NOT
  carry the full-config top-level `source_mixture_mode`: a 29-channel config may be
  `mixed_raw_corrected`, yet the user's two selected markers may both be corrected →
  that run is `homogeneous_corrected` and must be allowed. Drop any stale top-level
  `source_mixture_mode` on projection; the authoritative mixture is recomputed by the
  5c.2 resolver over ONLY the selected markers, and an advisory `intended_source_mixture_mode`
  is derived here from the selected markers' recorded `actual_source_kind`.
- Projected config = the saved config restricted to `marker_channels`, carrying each
  channel's recorded `calibration_source_identity` verbatim.
- Feed the PROJECTED config to promotion (`promote_source_aware_from_sources` then iterates
  only the marker set). Unselected channels and DAPI never block promotion.
- Testable offscreen (pure config shaping + monkeypatched resolver).

### Workstream B — Source-aware Step2 runtime  [B1+B2+B3 ship TOGETHER behind one flag]

ChatGPT constraint: B1 (guard relaxation) MUST NOT be independently launcher-attachable —
the worker is still single-source, so a step2_ready runtime config would be misused by the
old single-source path (values remapped, source not switched per channel). All three land
and enable together, gated by one feature switch.

**B2. Per-channel marker reads** (split into a testable core + a wiring step):
- **B2-core (DONE, offscreen-tested):** `read_per_channel_marker_blocks(per_channel_resolved,
  channels, bbox, read_block)` + `require_homogeneous_source(...)` in
  `workers/hq_source_resolver.py`. A pure dispatch over `PerChannelResolvedSource` with an
  INJECTED `read_block`, so per-channel source selection (raw vs corrected) is verifiable
  without worker/GPU state. `require_homogeneous_source` refuses `mixed_raw_corrected`.
  Not wired into any live path.
- **B2-wiring — two worker stages** (per the review constraint: never re-resolve at
  construction, where ROI/geometry/active source are not ready):
  - **Construction stage (B3b-1, DONE):** `_accept_source_aware_runtime_descriptor` in
    `_resolve_channel_remap` — detect a runtime descriptor
    (`created_by_source_aware_promotion` + `runtime_supported`), **hard-reject when the flag
    is off** (no single-source fallback), validate the descriptor SHAPE
    (`validate_source_aware_runtime_config`), and stash it in
    `self._pending_source_aware_runtime`. No source resolve, no ROI check here.
  - **Prep stage (B3b-2, DONE — method + cross-check; run-loop call-site lands with B3c; GPU):** `_prepare_source_aware_runtime` at the HQ
    marker-source preparation point (active source / ROI / input geometry known):
    worker-side full-image gate → `resolve_per_channel_marker_sources` from
    `_raw_channel_source_path()`/`_multichannel_source_path()` (never trust the config's
    handles) → `require_homogeneous_source` → cross-check kind/path/group/shape vs the
    descriptor → store opened handles on `self._source_aware_per_channel`. Any failure
    raises before output (all-or-nothing).
  - **Read (B2-core, B3c):** per tile, `read_per_channel_marker_blocks` reuses the stored
    handles via a channel-store-backed `read_block` (raw_ome/zarr dispatch, normalize=False).
    Full-image only, so the raw region offset is 0 and per-channel frames coincide.

  **B3c wiring — exact call-sites (two run-entry paths, neither may be missed):**
  `segment_merge_worker` has TWO paths that open the segmentation-input zarr and read
  markers: `~2167` (`z = zarr.open(zarr_path)`) and `run()`'s `~3037`
  (`z = zarr.open(self.zarr_path)`). In BOTH, right after `zarr.open` and before any tile:
  - if `self._pending_source_aware_runtime`: call `self._prepare_source_aware_runtime(z.shape[:2])`
    once; on failure it raises (hard-fail, no fallback).
  - HQ source selection (`~2218` / `~3085`): must NOT simply set `hq_group=None` and skip
    `_validate_hq_config` — that method also parses/normalizes mode + hq_channels + weights
    and does parameter-layer validation (empty/illegal selection). **Split it:**
    - `_validate_hq_selection()` — source-INDEPENDENT: normalize mode, parse `hq_channels`
      + weights, mutate `seg_config`, validate a non-empty legal selection. (lines ~1586-1593
      + the selection checks).
    - `_open_hq_single_source(...)` — source-DEPENDENT: `resolve_hq_marker_source` +
      `validate_hq_channels(vs available)` + return `group`. (the current remainder).
    - Existing callers = `_validate_hq_selection()` then `_open_hq_single_source()` (behavior
      identical). Source-aware branch = `_validate_hq_selection()` then validate the
      selection against the RE-RESOLVED per-channel map, skip `_open_hq_single_source`,
      `hq_group=None`.
  - **Exact-equality (not coverage):** the source-aware branch requires
    `set(selected non-reference markers) == set(runtime descriptor channels)`. Auto-projection
    already yields exact equality; a manual runtime config carrying extra markers is rejected
    (no "resolved-but-unused" ambiguity).
  - `_read_hq_marker_channels(group, channels, ...)`: when `self._source_aware_per_channel`
    is set, route to `read_per_channel_marker_blocks(self._source_aware_per_channel,
    channels, bbox, read_block=self._read_one_marker_block)`; else the existing group read.
  - Extract `_read_one_marker_block(group, ch, y0,y1,x0,x1)` from the current
    `_read_hq_marker_channels` branch bodies (raw_ome via channel-store `read_raw_ome`/
    `read_region` normalize=False; zarr via `read_zarr_channel`) — a behavior-preserving
    single-(group,channel) primitive reused by both the single-source loop and the
    per-channel reader. No per-tile reopen. If `_pending_source_aware_runtime` is set but
    `_source_aware_per_channel` is None (prepare didn't run/succeed) -> hard-fail.
  - **CSD lean-carve path too:** `_hq_block_loader` (used by CSD lean-carve, `~2355`/`~3217`)
    must also route through the per-channel reader when `hq_group=None` — a B3c test covers
    it, not just HQ2's one-shot tile reader.
  Guarded on `_source_aware_per_channel` (None in every current run) so existing behavior
  is byte-identical; the pixel-read extraction needs raw + corrected GPU acceptance.
- Per-channel intensity space is safe: the candidate verified recorded == resolved
  intensity_space per channel, so raw-unit `apply_channel_remap` acts on the matching
  native source.
- **Homogeneous only** in v14.5d: `source_mixture_mode ∈ {homogeneous_raw,
  homogeneous_corrected}`. `mixed_raw_corrected` → refuse (follow-up).

**B1. Runtime acceptance + guard relaxation** (pure validation; gated):
- **DONE (offscreen-tested, not wired):** `promote_candidate_to_runtime(candidate,
  per_channel_resolved, step2_input_shape)` in `remap_promotion.py` — homogeneous guard →
  re-verify the 3-way (delegates to `promote_source_aware_config_for_step2`) → stamp
  `runtime_supported=true` + `step2_ready=true` → re-validate. The persisted artifact stays
  the candidate; the runtime form is launch-only. `validate_source_aware_runtime_config` in
  `channel_remap_config.py` gates the runtime shape (runtime_supported, step2_ready,
  per_channel_native, homogeneous mixture, per-channel resolved_source_kind/path, no
  reference channel).
- **DEFERRED to B3 (with the flag):** relaxing the live guard
  `channel_remap_config.py:400` (`validate_step2_remap_config` still hard-rejects
  `source_aware + step2_ready=true`). The relaxation lands only when the worker can actually
  do per-channel reads — otherwise a runtime config would be misread by the single-source
  path. B1's functions have NO live callers today.

**B3. Launch wiring** (replace the single-source `_promote_step0_remap`):
- **Input-mode gate:** accept only `selected_channels_from_source` HQ2/CSD input mode.
  `step1_weighted_fusion` continues to reject source-aware marker remap (the fused signal
  is not a per-marker source), consistent with `validate_remap_covers_selected_channels`.
- HQ2/CSD + source-aware config → §5-A projection → `promote_source_aware_from_sources`
  (selected markers) → `promote_candidate_to_runtime` → attach runtime config. Refuse/error
  → attach nothing.
- **Feature flag** `ENABLE_STEP2_SOURCE_AWARE_REMAP_RUNTIME` (default **False**, internal
  release switch, not user-exposed). When False, do NOT attach any runtime config and show
  "NOT applied — source-aware runtime disabled". Flip to default True only after BOTH raw
  and corrected GPU acceptance pass.
- **Explicit surfacing (no silent fallback):** the UI/log must state, per run, either
  "Step0 remap applied (N markers, source=raw/corrected)" or "Step0 remap NOT applied —
  reason: <scope | source-mixed | ROI | uncovered-marker | resolution-failed>". Console-only
  is insufficient; surface in the Step2 status area.

### Workstream C → deferred to v14.5e — ROI coordinate contract

v14.5d supports **full-image runs only**; ROI runs are safe-skipped. ROI is NOT a
shape-equality problem: raw OME marker reads use full-image coordinates (need the ROI
offset), while a corrected ROI group is in LOCAL coordinates — equal shapes do NOT prove
an equal coordinate frame. v14.5e must define the coordinate contract (offset mapping
raw-full ↔ corrected-ROI-group ↔ segmentation-input) before enabling ROI.

## 6. Rollout order (revised)

1. **A** — Step2 marker-only projection (small, independent, offscreen-testable).
2. **B2** — per-channel marker reader (sensitive; GPU).
3. **B1** — runtime validation + gated guard relaxation (offscreen).
4. **B3** — launch wiring + UI surfacing.
   → **B1+B2+B3 enable together behind one feature flag.** No independent B1 release.
5. **v14.5e** — ROI coordinate contract (separate).

## 7. Test plan

- **A (key test):** a 29-channel saved config incl DAPI, only 2 markers selected → the
  projection resolves/reads ONLY those 2; unselected channels and DAPI do NOT block
  promotion; projected config carries no reference channels.
- **A (invariant 1):** a selected marker absent from the saved config → `uncovered-marker`
  refusal (not a silent drop).
- **A (invariant 2):** full config `mixed_raw_corrected`, but the 2 selected markers both
  corrected → `intended_source_mixture_mode == homogeneous_corrected`; stale top-level
  mixture dropped from the projection.
- A: projection drops reference names; carries recorded calibration identity per marker.
- B2: per-channel read selects corrected vs raw per channel (fake sources); homogeneous
  raw / homogeneous corrected; `mixed_raw_corrected` refused.
- B1: candidate→runtime acceptance; guard still rejects source-aware+step2_ready without
  `runtime_supported`; runtime config not attachable without the flag; tamper tests.
- Integration: real-shaped source-aware config, DAPI present but unselected → runtime
  config accepted for the selected markers only.
- Regression: DAPI stays in the saved config; Step1 DAPI remap + DAPI cache hash unchanged.
- **GPU acceptance (Ming):** HQ2/CSD, homogeneous-corrected markers → remap reflected;
  status line shows "applied"; an ROI run shows "NOT applied — ROI".

## 8. Risks

- B2 mutates the sensitive marker-read runtime; pixel reads not offscreen-verifiable → GPU.
- Guard relaxation must gate on `runtime_supported` + per-channel resolved fields + the flag,
  or it reopens the silent-ignore hole. Hence B1 never ships alone.
- Must not regress DAPI: keep DAPI in the saved config; filter only in the projection.
- Legacy single-source / DAPI-containing configs keep working (fall back to no-remap, with
  an explicit "not applied" reason).

## 9. Decisions (locked with ChatGPT) + remaining question

Locked:
1. **DAPI** — keep in the full Step0 saved config; filter reference channels ONLY in the
   Step2 marker-only projection and at consumption. Never strip at save.
2. **Scope** — homogeneous raw / homogeneous corrected, HQ2/CSD, full-image first.
3. **ROI** — v14.5e (separate coordinate contract), not v14.5d.
4. **Order** — A (marker-only projection) → B2 reader → B1 runtime validation → B3 wiring;
   B1+B2+B3 enabled together behind one flag.
5. **Surfacing** — UI/log explicitly shows applied vs not-applied(reason); no silent fallback.
6. **Coverage invariant** — `selected_non_reference − saved_channels ≠ ∅` → explicit
   `uncovered-marker` refusal (no silent intersection drop).
7. **Mixture invariant** — recompute `source_mixture_mode` from the selected markers, never
   inherit the full-config top-level mixture.
8. **B3 input mode** — only `selected_channels_from_source`; `step1_weighted_fusion` rejected.
9. **Feature flag** — `ENABLE_STEP2_SOURCE_AWARE_REMAP_RUNTIME`, default False, internal-only;
   off → status "NOT applied — source-aware runtime disabled"; default True only after raw
   + corrected GPU acceptance.
