# 06 — Benchmark Plan (v13.1)

## Goal

Test whether the v13.1 manual remap path can replace the top-hat / cuCIM path in
the segmentation preprocessing pipeline for the current HCC data.

This benchmark is what accepts or rejects the hypothesis in
`00_PROJECT_VISION.md`. It is not a vibe check; it is region-stratified.

## Comparison

```text
Baseline v13:
    top-hat / cuCIM path → HQ2/CDS

Experimental v13.1:
    manual channel remap → HQ2/CDS
```

Hold everything else fixed: same source image, same tiles, same HQ2/CDS/lean_carve
parameters, same feature extraction source. Only the preprocessing operator
changes. This isolates the operator as the independent variable.

## Metrics

Quantitative:

- **cells found** (count)
- **total mask area**
- **mean cell area**
- **mask area distribution** (histogram / quantiles, not just mean)
- **boundary smoothness**
- **runtime**
- **GPU / CPU memory**

Qualitative / scored (rubric, 0–N per region, scored blind where possible):

- **visual QC score**
- **background swallowing score** (does it absorb background into cells?)
- **weak-signal preservation score** (does it keep faint true signal?)
- **over-expansion / under-expansion score**

## Region-stratified benchmark (mandatory)

```text
Global metrics are not enough.
```

A method can win on whole-slide averages while failing badly in specific tissue
contexts. Evaluate each region class separately:

```text
AF-heavy region
clean region
necrotic region
RBC-rich region
tumor-rich region
immune-rich region
edge / low tissue region
```

For each region class, compute the metrics above for both baseline and
experimental, and record per-region deltas. Select representative tiles per class
ahead of time and freeze that tile set so both methods see identical inputs.

## Key question

```text
Does manual remap fail specifically in AF-heavy or spatially uneven
background regions?
```

This is the decisive question. Top-hat is a spatial background estimator; its
main theoretical advantage is exactly in AF-heavy / spatially uneven background.
If manual intensity remap holds up there too, top-hat's advantage may not matter
for this dataset.

## Interpretation rules

```text
If manual remap works globally AND in AF-heavy regions:
    top-hat/cuCIM may be redundant for this dataset after existing
    upstream/background handling.  -> adopt v13.1 path.

If manual remap fails in AF-heavy regions while clean regions look good:
    top-hat/cuCIM or another spatial background operator is still needed.
    -> keep a spatial operator for hard regions; do not fully replace.

If both work similarly:
    choose the simpler / faster / more controllable path  -> v13.1.
```

## Deliverable

A benchmark report containing:

- the frozen tile set per region class,
- per-region metric tables (baseline vs v13.1, with deltas),
- the qualitative scores with the rubric used,
- runtime / memory comparison,
- a clear verdict mapped to the interpretation rules above,
- explicit call on AF-heavy regions (pass / fail and what it implies).
