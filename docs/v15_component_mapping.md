# v15 component mapping — old channel UIs → shared ChannelDock

Date: 2026-08-29. Companion to `v15_interactive_channel_workspace_plan.md` §11–12.

## Old → new

| Old component | Location | New home | Migration status (Phase 1A) |
|---|---|---|---|
| Step0 BG hand-rolled channel `QListWidget` + row widgets | `ui/step0/step0_page.py:588`, `_rebuild_channel_list` `:2429` | `ChannelDock` + `Step0ChannelRow` via `ui/step0/step0_dock_adapter.py` | **Mounted through adapter**; legacy registry keys (`checkbox`, `method_cb`, `status_lbl`, `row_widget`, `item`) preserved so `_refresh_channel_row` / `_set_channel_computing` keep working |
| Step0 "Method Parameters" spinboxes (`_tophat_slider`, `_cucim_slider`) | `step0_page.py:~640` | `Step0Inspector` (selected-channel tool area) | Contract defined; page keeps existing global param widgets this round |
| `ChannelWeightRow` | `ui/step0/channel_weight_row.py` (+ duplicate in `overview_panel.py:613`) | `WeightChannelRow` | New row implemented; `ConfigPanel`/`GroupPanel` untouched; adapter `ui/step1_dock_adapter.py` mirrors weights two-way |
| `GroupPanel` / `ConfigPanel` (Step1 fusion) | `ui/step0/group_panel.py`, `config_panel.py`, mounted `main_window.py:389` | stays (group semantics); flat channel view supplied by Step1 dock adapter | **Adapter mounted additively** (no removal) |
| Step3 Channel Overlay panel (`_make_channel_overlay_panel`) | `ui/step3_page.py:1810`, `_channel_settings` | `ChannelDock` + `DisplayChannelRow` + `Step3Inspector` via `ui/step3_dock_adapter.py` | **Adapter + test contract only, not mounted** (Step3 overlay has zero legacy test coverage — mount after interface stabilizes) |
| `ChannelLayerList` | `ui/widgets/channel_layer_list.py` | superseded by `ChannelDock` long-term | Kept as-is (still used by `ChannelWorkbench`) |
| `ChannelWorkbench` channels column | `ui/widgets/channel_workbench.py` | future: compose `ChannelDock` internally | Untouched this round (5 host flags, tests assert `_chk_all`/`_search`) |
| `HighQualityImageViewer` / `ChannelViewerCanvas` | `ui/widgets/high_quality_image_viewer.py`, shim `channel_viewer_canvas.py` | unchanged; future viewer foundation (Workstream C) | Untouched |
| Patch 1–4 preview selectors | Step0 patch bar | `PinnedLocation` (`ui/widgets/compare_contract.py`) | Contract only; patches still functional |

## New modules

| Module | Contents |
|---|---|
| `ui/widgets/channel_dock/model.py` | `ChannelState` (identity, visible, color, selected, display min/max/gamma, bg preview/final method, status, weight, scope processing/display-only), `ChannelSetModel` signals |
| `ui/widgets/channel_dock/dock.py` | `ChannelDock` shell: search, Show all/Hide all, header-extra slot, scroll-per-pixel list, selected-channel tool area |
| `ui/widgets/channel_dock/rows.py` | `ChannelRowBase`, `Step0ChannelRow`, `WeightChannelRow`, `DisplayChannelRow` |
| `ui/widgets/channel_dock/editors.py` | `MinMaxGammaEditor` (label/slider/value aligned), `Step0Inspector` (+bg params, Compare entry), `Step3Inspector` (display/QC-only badge) |
| `ui/widgets/compare_contract.py` | `ViewerMode`, `CompareScope`, `ViewportState`, `ComparisonROI`, `PinnedLocation`, `SharedViewportState` (2×2 sync, latest-request-wins generation), `CompareModeState` |
| `ui/step0/step0_dock_adapter.py` | Step0 page ↔ dock adapter with legacy row-registry compatibility |
| `ui/step1_dock_adapter.py` | ConfigPanel weights ↔ dock two-way mirror |
| `ui/step3_dock_adapter.py` | `_channel_settings`-shaped display dict ↔ dock, display-only guarded |

## Known pre-existing issues recorded during audit (not fixed this round)

- Duplicate `_on_channel_checkbox_toggled` in `ui/step0/step0_page.py` (`:2944` shadowed by `:3081`).
- `ChannelWeightRow`/`GroupPanel`/`ConfigPanel` duplicated in `ui/step0/overview_panel.py`.
- Step3 overlay panel rebuilds all rows on each search keystroke; no test coverage.
- Host reach-through into `ChannelWorkbench._h_split` and `_canvas._opacity`.
