# Cross-platform desktop UI in the DeepSeek Harness style (v17)

Status: plan v0.3, 2026-11-03 (user rulings §6, review §8). Phase 1 starting. User rulings: the new desktop UI
is the highest priority; ONE interface for Windows and Ubuntu (all systems);
model it first on DeepSeek Harness — colours, button feel (TG 2026-11-03).

## 1. Where Block01 is now (measured)

* PyQt5, `app.setStyle("Fusion")` + a hand-made dark `QPalette` (`main.py`).
* 436 `setStyleSheet` calls in 32 files under `ui/`; colours are literals
  (`#aaa` ×44, `#555` ×38, `#ddd` ×32, `#333`, `#1a1a1a`, One-Dark accents
  `#61afef`, `#e5c07b`, `#2a5` …). Top five files: step0_page 76,
  main_window 52, step2_page 50, search_ctrl 45, step1_5_bg_page 41.
* No theme layer: every widget styles itself; there is no light theme.
* Viewers draw with the Rust core in QOpenGLWidgets (Step0/1/2/3) and
  pyqtgraph; they are content, not chrome, and keep their black canvas.
* It already runs on Ubuntu; Windows wheels of the Rust core exist
  (`core-v0.2.1`, `wheels.lock`).

## 2. DeepSeek Harness design language (from its source, 2026-11-03)

Repo `deepseek-ai/deepseek-harness`, `packages/client/ui-theme/src/styles/`
(`design-platform.css`, `base.css`), `docs/ui-radius.md`,
`packages/client/ui-layout/README.md`.

| Token | Light | Dark |
|---|---|---|
| canvas | #FFFFFF | #151517 |
| layer 1 / 2 / 3 | #FFFFFF | #232324 / #2C2C2E / #353638 |
| sidebar | #F9FAFB | #1B1B1C |
| sidebar active item | #EBEEF2 | #43454A |
| border l1…l4 | black 4/10/12/16 % | white 6/12/16/20 % |
| text primary / secondary / tertiary / caption | #0F1115 / #61666B / #81858C / #ADB2B8 | #F9FAFB / #CFD3D6 / #ADB2B8 / #81858C |
| primary button | #0F1115 (hover #43454A) | #F9FAFB (hover #EBEEF2), inverted |
| accent / link / focus | #4176E6 | #7AAAFF |
| hover / active fill | rgba(38,49,72,.06 / .10) | white .08 / .14 |
| danger | #EC1313 | #F25A5A |
| success / warning | #22C55E / #F59E0B | same |

* Buttons: variants primary, ghost, outline, toolbar; sm H28/R8, md H36/R12;
  inputs H32/R12; icon buttons R8; hover/pressed never change size or radius.
* Radius scale 4/8/12/16/20/28; menus R16 outer, R12 items; cards R20,
  0.5 px hairline borders; focus ring 2 px accent.
* Type: system UI stack (Segoe UI on Windows, PingFang/Microsoft YaHei for
  Chinese), body 14 px, code 11 px; weight 500 for labels.
* Layout: three columns — left sidebar 280 px (264–420), collapsing to a
  56 px rail; centre; optional right panel. Light theme is the default, dark
  and "system" selectable.
* The primary colour is near-black (light) / near-white (dark), not blue;
  blue is only for links, focus and "business" state.

## 3. Options

**A. Stay on Qt, add a theme layer (recommended).** One `ui/theme/` module
holds the tokens above (light + dark), builds ONE application stylesheet and
palette from them, and widgets take roles (`setProperty("variant",
"primary")`, object names) instead of literal colours. Inline
`setStyleSheet` calls are replaced file by file. Viewers, the Rust core, all
logic and every test stay. Cost: medium, incremental, every step visible.

**B. Move to PySide6 (Qt 6) under A.** Same code style, LGPL instead of
PyQt5's GPL — needed for the commercial licences the user plans
(PolyForm Noncommercial + per-company licences); better high-DPI and
Windows behaviour. Mechanical but wide (signals, enums, `exec_`,
QOpenGLWidget module). A separate block after A's first phase.

**C. Rewrite the UI as a web app in Electron/Tauri (DeepSeek's own stack).**
Pixel-identical style possible, but the whole UI (~25 k lines in the two
largest files alone) and the GPU viewers (QOpenGLWidget + Rust GL) would be
rebuilt. Not recommended.

## 4. Proposed phases (A, then B)

1. **Theme layer + global sheet** — tokens, light/dark (+ follow system),
   buttons (primary/ghost/outline/toolbar, sm/md), inputs, tabs, lists,
   scrollbars, menus, tooltips, group frames, the step bar. No layout change,
   no new control. Inline styles that fight the sheet are removed in the
   files touched.
2. **Page by page** — Step0, Step1, Step2/3/4, Regions, floating windows
   (Tissue Preview, Intensity, Step3 results): literal colours → roles.
   Channel colours, outlines and image canvases are data and stay as they are.
3. **Layout in the DeepSeek manner** — needs the user's ruling first
   (UI_SURFACE_RULES §2): e.g. the step bar becomes a left sidebar (steps,
   project/workspace), collapsible to a rail. Shown as a mock before any code.
4. **PySide6** (option B) — its own plan and review.
5. **Packaging and check on Windows and Ubuntu** — one installer per system,
   the same screens on both.

## 5. Settled (user, TG 2026-11-03) — see §6

## 6. User rulings 2026-11-03

1. Both themes; **dark is the default** (light selectable; "follow system"
   is not asked for).
2. **Keep the top step bar** — no sidebar; phase 3 (layout) drops out.
3. **PySide6: yes.** Order: phase 1 (theme layer) on PyQt5 first, so the
   look is accepted on the current code; then the PySide6 move; then the
   page-by-page phase on PySide6.
4. Step0's Panel CSV box goes (done with this plan's first change: the box,
   its Browse, the parser and the template `panel.csv` it wrote into the
   output folder on every load; the nucleus channel is found from the slide,
   Step1 starts with one "markers" group).

## 7. Phase 1 in detail (theme layer, PyQt5)

* `ui/theme/tokens.py`: the §2 tokens for `dark` and `light`, plus radius,
  sizes, font stack — one dict per theme, nothing else.
* `ui/theme/sheet.py`: builds ONE application stylesheet + QPalette from a
  token dict (QWidget base, QPushButton by role property `variant` =
  primary / ghost / outline / toolbar, size `sm` / `md`; QLineEdit,
  QComboBox, QSpinBox, QCheckBox, QRadioButton, QTabBar/QTabWidget,
  QListWidget/QTreeView, QScrollBar, QMenu, QToolTip, QGroupBox as card,
  QProgressBar, QSlider, QMessageBox).
* `main.py`: `apply_theme(app, name)` instead of the hand-made palette;
  the choice is kept in the user settings (QSettings), default dark; the
  switch itself lives in an existing menu/place only after the user says
  where (UI_SURFACE_RULES: no new control without a ruling) — until then
  an environment variable / settings key.
* Inline `setStyleSheet` that set chrome colours (frames, buttons, labels'
  greys) are removed where they fight the sheet, starting with the step bar,
  Step0's load row and Save, the channel list rows; data colours (channel
  swatches, outlines, viewer canvases, overlays) are left alone.
* Acceptance: screenshots of every step in dark and light, side by side with
  today's; the user's real-machine check.

## 8. Plan review round 1 (astra, low) — CHANGES, taken (v0.3)

* **Geometry is kept.** DeepSeek's colours, radii, borders and hover/press
  feel are adopted; its heights (H28/H36 buttons, H32 inputs) are NOT —
  tool rows stay 25 px (`ui/step_frame.py`), channel rows 26 px
  (`ui/widgets/channel_dock/template.py`), and every fixed size stays.
  Radii scale down to fit (6 px on 25 px controls).
* **Scoped roles only.** No rule on bare `QWidget`; the sheet styles
  widgets that carry a role property (`ds` = step / stepsep / bar / field /
  primary / outline / ghost …) plus QToolTip, QMenu and scroll bars. Viewer
  canvases, GL widgets, pyqtgraph and overlays get nothing. The palette
  carries the base colours for unstyled chrome.
* **Order inside phase 1:** step bar (construction AND
  `_set_step_active`'s restyling) → Step0 load row, Load, Tissue Navigator,
  Save and Save menu, plus the places that copy their style sheets
  (`main_window` copies Step0's `styleSheet()` onto Step1/3 buttons) →
  shared channel rows. Each component's local rules go together, dynamic
  properties re-polished on change.
* **Closed file list (phase 1):** `ui/theme/__init__.py`,
  `ui/theme/tokens.py`, `ui/theme/sheet.py` (new), `main.py`,
  `ui/main_window.py` (step bar + style copies only),
  `ui/step0/step0_page.py` (load row, Save/menu only),
  `ui/widgets/channel_dock/template.py` (row chrome only), tests, this doc.
* **Acceptance boundary:** dark (default) — these components themed, the
  rest unchanged and still readable. Light — these components themed; other
  pages still carry dark literals (phase 3 work); screenshots of every step
  in both themes document what remains. Real-machine check of pixels after
  load, resize, navigation and overlays in both themes.
* **PySide6 inventory** (separate block): pyqtSignal/pyqtSlot →
  Signal/Slot; `sip.isdeleted` → shiboken validity, `sip.setdestroyonexit`
  assessed; enum/flag int conversions (`ui/regions/tools.py`); three
  QOpenGLWidget subclasses → `QtOpenGLWidgets`; QActionGroup/QShortcut →
  QtGui; `exec_()` → `exec()`; select PySide6 before pyqtgraph imports;
  GL function-pointer loading, context, framebuffer, DPI and teardown
  validated on both OSes.
* §3/§4's sidebar, follow-system and old order are superseded by §6.

## 9. Phase 1 as implemented (2026-11-03)

* `ui/theme/` (tokens, scoped sheet, palette, `apply` / `role` / `set_flag` /
  `adopt`); `main.py` applies it (default dark; `BLOCK01_THEME` or the
  `ui/theme` setting pick light).
* Themed: step bar (flag `on`), Step0 load row (bar, fields, Browse, Load,
  status, Tissue Navigator — and its copies on Step1/Step3), Save + menu,
  channel rows / list / dock (colours from the tokens), Step0's Channels
  frame (the template's colour). Geometry stays inline on each widget.
* Code review round 1 (astra, low): two P2, taken — menus and scroll bars
  are left to the palette (a sheet rule changed their metrics); the nucleus
  channel is found by name (DAPI, then DAPI*/Hoechst*) now that the panel
  file is gone.
* Regression vs 9ce6a46, 186 UI test files: no difference but two known
  flakes (an h5 read race under parallel runs; an old-side flake).

## 10. In-step buttons, tabs, titles, dialogs — proposal (Fable 5.1 medium + Claude, 2026-11-03)

User asks (TG 2026-11-03): every in-step button and tab hovers; in-panel
buttons not white like Load/Save but one style fitting the coloured panels;
dialogs (Yes/No/OK), step titles and tabs redesigned; mocks first.

* Roles: primary (Load/Save/Continue), run (#22AA55 green: Generate fused,
  Run Segmentation & Merge, Extract, Pre-seg Run), panel (THE in-panel
  button, neutral grey #2C2C2E, hover #353638), outline (title-bar level),
  ghost (tool rows over viewers), toggle (on/off, checked #43454A bold),
  seg (one-of-N), danger (Stop/Delete: red text + red hairline), icon.
* Option A (recommended) neutral in-panel buttons; option B tinted with the
  panel colour (7 enumerated tints). Panels keep their colours, border at
  45 % alpha (light: 7 darker accents).
* Tabs: "attached" (selected joins the pane) recommended, "pill" alternative;
  geometry from FRAME_TAB_QSS kept, no bold on selected.
* Step title: header band `bar`, "Step 2" grey + "—" + name white semibold.
* Dialogs: one sheet rule for QMessageBox (default button = primary, others
  outline), destructive choices via a small helper (danger button, safe
  default); button order follows the platform.
* Keep their colour (data/meaning): P2–P5 patch buttons, montage layer
  toggles, swatches, ROI colours, the green run buttons. Proposed changes of
  meaning needing a ruling: only run buttons green (Apply Selected Params,
  Load zarr info drop to `panel`); Overlay/Fusion/Smooth/TMA cores and
  Step0 Marker/DAPI checked = neutral grey instead of green;
  Original/TopHat/cuCIM checked = grey instead of blue.
* Mocks: TG outbox `样图1/2/3` (dark + light).

### 10.1 User rulings (TG 2026-11-03): all six recommendations accepted
1 A — neutral in-panel buttons; 2 attached tabs; 3 only run buttons green
(Apply Selected Params, Load zarr info → `panel`); 4 toggles checked neutral
grey (Overlay/Fusion/Smooth/TMA cores, Step0 Marker/DAPI), Original/TopHat/
cuCIM checked grey; 5 dialogs as mock 3 (default = primary, others outline,
destructive = danger and never default, platform order); 6 step titles as
mock 2. Also: the Load row's hidden line — problems in a non-modal box (done).
Order: sheet roles → Step2 → Step4 → tabs + titles → Step0 → Step1 →
pre-seg → Regions → dialogs → Step3 results / search_ctrl / step1_5.
