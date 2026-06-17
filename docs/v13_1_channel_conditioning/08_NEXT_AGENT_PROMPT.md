# 08 — Next Agent Prompt (v13.1 implementation kickoff)

Copy the block below as the prompt for the next implementation agent.

---

You are continuing the v13.1 channel conditioning work on the Block01 project.

Working directory: `/sda1/Fusion/analysis_pipline/block01`
Branch: `v13.1-channel-conditioning` (already created; do not create a new branch)
Design docs: `docs/v13_1_channel_conditioning/00..08`. Read them first.

## Scope for this run — Phase 1 and Phase 2 ONLY

Do exactly Phase 1 and Phase 2 from `07_IMPLEMENTATION_PLAN.md`. Nothing else.

### Phase 1 — Core remap operator

- Implement `core/channel_remap.py`: backend-agnostic (NumPy first), no Qt, no
  I/O. Functions: per-channel remap (Min/Max/Brightness/Contrast/Gamma), Auto
  (QuPath-style percentile window), and channel fusion.
- Implement `utils/channel_remap_config.py`: load / save / validate the
  `segmentation_preprocess_config` schema.
- Follow the **normative transform order** and parameter shapes in
  `03_CHANNEL_REMAP_SPEC.md` exactly (window → contrast/brightness → gamma, clip
  after each stage; output float32 in [0,1]).

### Phase 2 — Step3 UI prototype

- Redesign the Step3 layout into the three-column channel-first viewer per
  `04_UI_REDESIGN_SPEC.md`: left channel list (vertical scroll only, no
  horizontal scroll), center large viewer, right active channel inspector,
  bottom patch/run/save bar.
- Move raw/corrected source controls out of the main UI into Advanced / Developer
  options.
- Add the active channel inspector (histogram + Min/Max/Brightness/Contrast/
  Gamma/Auto/Reset/Save), live remap preview, and save/load of the remap config.
- Build visualization on **PyQtGraph**.

## Hard constraints — do NOT

- Do **not** touch Step2 / whole-run integration (that is Phase 5).
- Do **not** touch h5ad / feature extraction (that is Phase 6).
- Do **not** add **napari** or any new heavy dependency.
- Do **not** implement Smart, MACSiQView Light/Dark, tone curve editor, or
  blink/fade/highlight effects (out of scope — see `01_DESIGN_DECISIONS.md`).
- Do **not** physically delete legacy top-hat / cuCIM modules; disconnect them
  from the active path only.
- Do **not** compute Auto per tile; Auto is computed at ROI/run level and frozen
  into config (see `03_CHANNEL_REMAP_SPEC.md`).

## Quality requirements

- Preserve existing behavior where possible; this is a prototype layered on the
  current Step3, not a rewrite of unrelated functionality.
- Add unit tests if test infrastructure exists (`tests/`): cover the transform
  (identity, clipping, gamma, stage order) and Auto (percentiles, valid-pixel
  masking), plus config round-trip.
- Keep `core/channel_remap.py` free of Qt and I/O so it stays reusable by Step2
  later.
- Before finishing, show a **file diff summary** (`git status` + `git diff --stat`)
  and a short description of what changed.

## Definition of done for this run

- `core/channel_remap.py` and `utils/channel_remap_config.py` exist with tests
  passing (if test infra exists).
- Step3 shows the three-column channel-first prototype, no horizontal scroll in
  the channel list, raw/corrected controls moved to Advanced.
- A remap config can be authored, previewed, saved, and reloaded.
- Diff summary shown. No Step2 / h5ad / napari changes.
