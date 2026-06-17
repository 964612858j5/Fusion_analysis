# 01 — Design Decisions (v13.1)

These are agreed, binding decisions for the v13.1 branch. Later agents should
treat them as constraints, not suggestions. If a decision must change, record the
change here with a rationale rather than silently diverging.

## 1. Manual channel adjustment is required

A manual per-channel intensity conditioning operator is in scope and is the core
deliverable of v13.1. It is not optional polish.

## 2. Remove top-hat / cuCIM from the active segmentation preprocessing path

In the v13.1 default / UI path, top-hat and cuCIM-based correction are removed
from the **active** segmentation preprocessing path. The active conditioning
operator becomes manual channel remap.

This applies to the v13.1 path only. v13 remains intact on its own branch and in
the backup.

## 3. Do not physically delete legacy modules yet

Legacy top-hat / cuCIM modules are **not** physically deleted unless absolutely
necessary. They are disconnected from the active v13.1 path but remain in the
tree for:

- benchmark comparison (baseline v13 vs experimental v13.1),
- rollback safety,
- the possibility that AF-heavy regions still need a spatial operator.

## 4. Manual adjustment functions for v13.1

Exactly these per-channel functions are in scope:

- **Min**
- **Max**
- **Brightness**
- **Contrast**
- **Gamma**
- **Auto** (QuPath-style percentile auto contrast)

## 5. Explicitly NOT implemented in v13.1

- **Smart** adjustment
- MACSiQView **Light / Dark** controls
- complex **tone curve editor**
- **blink / smooth fade / highlight range** effects
- **napari** dependency

These are out of scope. Do not add them "while we're here."

## 6. Auto is QuPath-style percentile auto contrast

Auto sets the window from intensity percentiles of valid pixels, e.g.
`min = percentile(valid, 0.1)`, `max = percentile(valid, 99.9)`. Exact defaults
in `03_CHANNEL_REMAP_SPEC.md`. It is not a histogram-equalization or adaptive
local method.

## 7. Auto is computed at ROI / calibration / whole-run level — not per tile

Auto percentiles are computed **once** from a selected calibration ROI / patch
set / whole-run reference region, then frozen into the config. Auto must **not**
be recomputed independently per Step2 tile, because per-tile Auto would make the
remap non-stationary across the slide and break reproducibility and tile
seams.

## 8. Remapped channels are used for segmentation only

The remap output feeds fusion / HQ2 / CDS / lean_carve. It does not feed
expression quantification.

## 9. h5ad expression defaults to raw or biologically corrected intensity

`adata.X` defaults to raw or biologically corrected intensity. Remapped
intensity is never the default expression matrix. Optional remapped per-cell
intensity may live in a clearly named non-default layer. See
`05_DATA_PROVENANCE_AND_H5AD.md`.

## 10. Step3 is the first viewer prototype, then widgets are extracted

Step3 is used first as the prototype host for the channel-first viewer. The
long-term design extracts the viewer into reusable widgets so the same viewer
serves Step1.5 Channel Conditioning and Step3 QC. Do not hard-couple viewer
logic to Step3 internals.

## 11. napari is a possible later optional advanced viewer only

napari may be reconsidered later as an optional, advanced, opt-in viewer. It is
**not** a v13.1 core dependency and nothing in the core path may import it.
