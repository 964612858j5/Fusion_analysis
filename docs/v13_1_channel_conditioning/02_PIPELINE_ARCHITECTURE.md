# 02 — Pipeline Architecture (v13.1)

## Current v13 path

```text
image channels
    ↓
top-hat / cuCIM / existing correction path
    ↓
fusion / HQ2 / CDS
    ↓
mask
    ↓
feature extraction
    ↓
h5ad
```

## v13.1 experimental path

```text
image channels
    ↓
manual channel remap
    ↓
fusion / HQ2 / CDS / lean_carve
    ↓
mask
    ↓
raw or biologically corrected intensity extraction
    ↓
h5ad
```

The only changed stage in the segmentation chain is the preprocessing operator:
top-hat / cuCIM is replaced by manual channel remap. Critically, the feature
extraction stage is **decoupled** — segmentation may run on remapped images while
extraction reads from raw / biologically corrected images. See
`05_DATA_PROVENANCE_AND_H5AD.md`.

## Configuration separation (mandatory)

Three configs are kept strictly separate. They have different lifetimes,
different consumers, and must not be merged into one blob.

```text
segmentation_preprocess_config
    - per-channel remap params (min/max/brightness/contrast/gamma/auto/...)
    - which channels feed HQ2/CDS/lean_carve
    - frozen Auto reference (percentiles computed at ROI/run level)
    - consumed by: Step2 segmentation (headless)

feature_extraction_config
    - which intensity source feeds quantification (raw / bio-corrected)
    - per-cell aggregation settings
    - consumed by: feature extraction / h5ad writer

viewer_config
    - display-only state: colors, opacity, layer order, current channel
    - NEVER consumed by Step2 or feature extraction
    - consumed by: viewer UI only
```

Rule: a parameter that affects the segmentation mask lives in
`segmentation_preprocess_config`. A parameter that only affects how pixels look
on screen lives in `viewer_config`. If a parameter is in both worlds (e.g. a
window used both to display and to segment), it is owned by
`segmentation_preprocess_config` and the viewer reads from it.

## Step2 / whole-run constraint

```text
Step2 whole-run must NOT depend on GUI state.
It must consume saved config files.
```

The GUI is a config editor and previewer. The whole-run segmentation is headless
and reproducible: given an image path and a saved `segmentation_preprocess_config`,
it produces a deterministic mask, with no reference to live slider values,
window geometry, or interactive selection. Auto is already frozen into the config
(decision 7 in `01_DESIGN_DECISIONS.md`), so Step2 never recomputes it.

## Proposed modules

New / refactored modules for the implementation phases. Names are proposals;
final placement may adjust but the boundaries should hold.

```text
core/channel_remap.py             # backend-agnostic remap operator (NumPy first)
utils/channel_remap_config.py     # load/save/validate remap config
ui/widgets/channel_layer_list.py  # left column: scrollable channel list
ui/widgets/channel_histogram_panel.py  # right column: histogram for active channel
ui/widgets/channel_viewer_canvas.py    # center: layered PyQtGraph viewer
ui/widgets/channel_workbench.py        # composed three-column workbench
ui/step3_page.py                  # Step3 hosts the prototype workbench
ui/channel_conditioning_page.py   # later Step1.5 page reusing the widgets
```

### Module responsibilities

- `core/channel_remap.py` — pure transform. Input: float image + per-channel
  params. Output: float32 0–1. No Qt, no I/O. Backend-agnostic (NumPy now,
  optional CuPy later). This is the operator Step2 and the viewer both call.
- `utils/channel_remap_config.py` — serialization + validation of
  `segmentation_preprocess_config`. Single source of truth for the schema.
- `ui/widgets/*` — reusable, host-agnostic widgets. No assumptions about being
  inside Step3.
- `ui/step3_page.py` — composes the widgets into the Step3 prototype.
- `ui/channel_conditioning_page.py` — later, composes the same widgets into the
  Step1.5 Channel Conditioning page.

## Step3 as prototype, then extraction

Step3 initially hosts the prototype directly to move fast. The long-term design
extracts the viewer/control logic into the `ui/widgets/*` modules so the exact
same viewer serves:

- Step1.5 Channel Conditioning (author the remap config),
- Step3 QC (inspect masks against conditioned channels).

Do not bake Step3-specific assumptions (page layout, navigation, run state) into
the widgets. Keep widget inputs/outputs explicit: image data in, config in,
edited config out.
