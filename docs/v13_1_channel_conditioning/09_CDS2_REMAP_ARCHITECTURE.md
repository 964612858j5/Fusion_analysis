# 09 — CDS2 / Remap Architecture (frozen before Step2/CDS integration)

This document freezes the architecture for how the v13.1 manual channel remap
relates to CDS2 camp arbitration **before** any Step2/CDS integration (Phase
2.1c and later). It is a design contract, not an implementation. Nothing here
changes CDS2, Step2, h5ad, top-hat/cuCIM, or adds napari.

Read alongside `02_PIPELINE_ARCHITECTURE.md`, `03_CHANNEL_REMAP_SPEC.md`, and
`05_DATA_PROVENANCE_AND_H5AD.md`.

---

## 1. Core decision

```text
Manual channel remap replaces the signal-gating role of Gi/hotspot logic.
It does NOT remove camp / mutual-exclusion.
The local contrast role of Gi remains required for camp arbitration.
```

Stated precisely:

- The **global signal-gating** role of Gi/hotspot logic (deciding *where
  reliable marker signal exists*) is **replaced** by manual remap, which
  produces clean 0–1 conditioning maps.
- The **local contrast** role of Gi (the `_block_gi` local-z statistics used to
  decide *camp identity / lineage ownership*) is **retained** and remains a
  required input to camp arbitration.

Do not summarize this as "Gi is optional." Gi's two roles are split: one role is
replaced, the other is kept.

---

## 2. Dual-path architecture (v13.1 first version)

```text
native/corrected marker channels
        ├── manual remap → 0–1 conditioning maps → signal gate / structural fusion
        └── existing _block_gi → local contrast maps → camp arbitration / mutual-exclusion
```

The two paths merge inside CDS2:

```text
CDS2 uses:
1. remap 0–1 conditioning maps  → decide WHERE reliable structural signal exists
2. _block_gi / local contrast    → decide camp IDENTITY and prevent false
                                    boundary ownership
```

Key point: both paths start from the **same native/corrected marker channel
data**. Remap is one transform of it (clip/gamma/brightness/contrast windowing);
`_block_gi` is a different transform of it (local-z / contrast statistics). They
are computed independently and consumed for different decisions.

---

## 3. Why camp cannot be removed

```text
Manual remap can suppress low background and produce clean signal maps,
but it cannot reliably solve lineage/camp ownership in mixed or adjacent cells.
```

Remap is a per-channel intensity windowing operator. It has no notion of
neighbouring-cell identity or competing lineages. It can make a channel look
clean while leaving genuinely ambiguous ownership unresolved.

Failure modes camp/mutual-exclusion exists to handle:

```text
CK+ epithelial/tumor cells adjacent to CD68+ macrophages
CD68 spillover / tails near CK+ regions
endothelial CD31 structures touching immune/tumor areas
AF-heavy HCC regions where background intensity overlaps weak real signal
```

Camp/mutual-exclusion is retained to avoid:

```text
epithelial cells swallowing macrophage signal
myeloid cells absorbing epithelial boundary
false double-positive boundary expansion
```

A clean remap map can even *worsen* these cases if used alone, because a
confident-looking gate over an ambiguous region still has to be arbitrated by
local contrast to assign the correct camp.

---

## 4. Why existing `_block_gi` is retained in v13.1

Decision:

```text
v13.1 keeps the existing _block_gi computed on native/corrected channel data.
remap-after-local-z is deferred to a future ablation / v13.2 experiment.
```

Reasons:

1. **Single-variable discipline.** v13.1 changes only the signal gate (Gi/hotspot
   → manual remap) and keeps camp contrast behaviour stable. One variable
   changes at a time.

2. **Interpretability.** If segmentation improves or regresses, we can attribute
   the change to remap signal-gating rather than to simultaneously altered camp
   contrast statistics.

3. **Contrast preservation.** Remap uses clipping / gamma / brightness /
   contrast and may saturate or reshape local distributions. Computing local-z /
   Gi *after* remap could disturb camp arbitration (the local statistics would
   no longer reflect native signal spread).

4. **HCC AF defense.** AF-heavy regions can contain spatially uneven high
   background. The retained local-contrast path is the main defense against
   diffuse autofluorescence being mistaken for true local marker enrichment. A
   remap gate alone, tuned globally, cannot distinguish spatially-varying AF from
   real signal as well as local-z can.

---

## 5. Camp thresholds are isolated from remap parameters

This is critical and binding:

```text
camp thresholds such as tau, cds2_area_frac, cds2_strength_lo/hi are calibrated
on native/corrected _block_gi z-score space.
They should not be automatically changed when remap Min/Max/Gamma/Brightness/
Contrast changes.
```

Why:

```text
remap parameters control signal gating / structural map generation.
camp thresholds control local contrast arbitration.
They live in different intensity spaces and should remain decoupled.
```

Explicit warning:

```text
Changing gamma does not imply tau should change.
Changing Min/Max does not imply cds2_strength thresholds should change.
```

Implementation consequence: the `segmentation_preprocess_config` (remap
parameters) and the camp/CDS2 threshold config are **separate configs** (see
`02_PIPELINE_ARCHITECTURE.md`). No code path may derive camp thresholds from
remap parameters, or vice versa.

---

## 6. Known design cost: dual-path channel use

```text
Channels used for camp may be used in two paths:
    - remap path for 0–1 signal map
    - _block_gi path for local contrast / camp arbitration
```

This has I/O and compute cost in tile streaming: a camp channel may be consumed
twice per tile (once for remap conditioning, once for local-z).

Framing:

```text
This is intentional in v13.1 for correctness and interpretability.
It is not a bug.
```

Future optimization direction (not v13.1):

```text
Future optimization should share one native channel read and feed both remap and
Gi computation, rather than physically reading the same tile twice.
```

i.e. read the native/corrected tile once, then fan it out to (a) the remap
operator and (b) the `_block_gi` operator in memory. This is a performance
refactor only; it must not change numerical results.

---

## 7. Relation to Phase 2.1c

```text
Phase 2.1c aligns Step3 calibration source with the future Step2 pre-remap source.
```

Correct chain:

```text
Step3:
same native/corrected source
    ↓
manual remap config
    ↓
preview 0–1 conditioning map

Step2:
same native/corrected source
    ↓
same saved remap config
    ↓
runtime 0–1 conditioning map
    ↓
CDS2
```

The alignment target is the **pre-remap source**, not the remap output:

```text
The alignment target is the pre-remap source, not the remap output.
```

Rationale: remap is deterministic given (source, config). If Step3 calibration
and Step2 runtime read the *same native/corrected pre-remap source* and apply the
*same saved config*, the 0–1 conditioning map is reproducible by construction.
Trying to align on the remap *output* instead would be fragile — it would couple
reproducibility to display/normalization details rather than to the source +
config contract.

This is exactly why the Phase 2.1b provenance guard records `intensity_space`
and marks Step3 configs `preview_only=true` until the source is confirmed to
match the Step2 pre-remap source (see `05_DATA_PROVENANCE_AND_H5AD.md`).

---

## 8. Future ablations

```text
Ablation A:
    existing _block_gi on native/corrected channels        (v13.1 baseline)

Ablation B:
    lightweight local-z on remapped 0–1 maps               (v13.2 candidate)

Ablation C:
    no local contrast / pure remap gate                    (stress test)
```

Each ablation changes only the camp-contrast input while holding the remap
signal-gate fixed, preserving single-variable discipline.

Expected warning:

```text
Ablation C is expected to be risky in AF-heavy or mixed-lineage regions.
```

Ablation C removes the very mechanism that defends against diffuse AF and
adjacent-lineage spillover (Sections 3–4). It is included only as a stress test
to quantify how much camp arbitration contributes, not as a shipping candidate.

---

## 9. Non-goals

```text
This document does not implement CDS2 changes.
This document does not integrate Step2.
This document does not change h5ad.
This document does not remove top-hat/cuCIM.
This document does not introduce napari.
```

It records the frozen architecture so that Phase 2.1c and any later Step2/CDS2
integration proceed against a fixed contract: remap replaces the signal gate,
`_block_gi` local contrast is retained for camp arbitration, the two configs stay
decoupled, and reproducibility is anchored on the shared pre-remap source.
