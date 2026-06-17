# 04 — UI Redesign Spec (v13.1)

This is a core deliverable. The current Step3 layout does not scale to high-plex
panels and is not channel-first. v13.1 rebuilds it into a channel-first viewer.

## Problems with current Step3 layout

- It is **not channel-first**. The mental model should be "a stack of channels I
  condition", but the UI does not center on that.
- Channel names and intensity controls are **spatially separated**. The user sees
  a name in one place and its sliders elsewhere.
- Intensity controls require **horizontal scrolling** to reach. A user can see
  channel names but cannot adjust intensity without scrolling sideways.
- **Raw/corrected source controls occupy too much space** in the main interface.
- Raw/corrected display is **not needed** in the main interface.
- The layout is **unworkable for 40+ marker** CODEX / MACSima panels.

## Target layout — three-column, channel-first

```text
+-------------------+-----------------------------+--------------------+
|                   |                             |                    |
|   Channel layer   |     Large high-quality      |  Active channel    |
|   list (LEFT)     |     viewer (CENTER)         |  inspector (RIGHT) |
|                   |                             |                    |
|                   |                             |                    |
+-------------------+-----------------------------+--------------------+
|   Patch selector / run preview / save config  (BOTTOM)              |
+---------------------------------------------------------------------+
```

- **Left:** channel layer list.
- **Center:** large high-quality viewer.
- **Right:** active channel inspector.
- **Bottom:** patch selector / run preview / save config.

## Left — channel layer list

- **Vertical scrolling only. No horizontal scrolling.** This is a hard rule.
- Each channel row shows only:
  - visibility checkbox,
  - color swatch,
  - channel name,
  - one small value (opacity **or** weight mini value).
- **Do not** put the full intensity slider set (min/max/brightness/contrast/
  gamma) into each row. Rows stay compact so 40+ channels fit in a vertical
  scroll without horizontal overflow.
- Selecting a row makes that channel the **active channel**, which drives the
  right inspector.

## Right — active channel inspector

Shows controls for the single active channel:

- active channel name,
- **histogram** of the active channel (from current patch / calibration set),
- **Min**,
- **Max**,
- **Brightness**,
- **Contrast**,
- **Gamma**,
- **Auto** (QuPath-style; see `03_CHANNEL_REMAP_SPEC.md`),
- **Reset**,
- **Save / Apply**.

This is where the heavy per-channel editing lives, so the left list can stay
compact. One channel edited at a time; the histogram and sliders are always
visible for the active channel without scrolling.

## Center — viewer

- Large patch viewer.
- Layer stack behavior similar to napari / MACSiQView (without depending on
  napari). Built on PyQtGraph.
- Can show: DAPI, current channel, remapped channel, fusion map, mask overlay.
- Supports per-layer **opacity**.
- Supports mask rendering as **outline** or **filled transparent mask**.
- Supports **raw-vs-remapped split view** if feasible (side-by-side or wipe), to
  let the operator judge what the remap does before committing.
- **Do not** show raw/corrected source text in the main viewer.

## Raw/corrected source controls

- **Hide from main UI.**
- Move to **Advanced / Developer options** only.
- Do not spend main interface space on them. The main interface is for channel
  conditioning, not source selection.

## Prototype-then-extract

- Step3 first becomes the **prototype** host for this viewer.
- Later, extract the viewer into **reusable widgets** (see
  `02_PIPELINE_ARCHITECTURE.md`):
  - `channel_layer_list`
  - `channel_histogram_panel`
  - `channel_viewer_canvas`
  - `channel_workbench`
- Those widgets are then reused for **Step1.5 Channel Conditioning** and **Step3
  QC**. Keep widgets host-agnostic: data + config in, edited config out, no Step3
  coupling.

## Interaction summary

```text
click channel in left list  -> becomes active channel
active channel              -> right inspector shows histogram + sliders
edit sliders / Auto         -> center viewer updates remapped preview live
Save / Apply               -> write into segmentation_preprocess_config
bottom bar                 -> pick patch, preview run, save config
```
