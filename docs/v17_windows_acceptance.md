# Windows acceptance — PySide6 build (v17, block RT2)

Run on the Windows machine that will be used for real work, with a **copy** of
a project (not the only copy). Tick each line; note anything that differs.
Nothing here has been run by the developer: **Windows is unverified** until
this list is done.

## 0. What is installed

* Python 3.10 (same minor as Ubuntu: 3.10.x).
* `PySide6-Essentials==6.11.3`, `shiboken6==6.11.3`, `pyqtgraph==0.14.0`.
* No PyQt5 in the environment (`pip list | findstr PyQt5` prints nothing).
* Confirm the binding: `python -c "import block01.qt as q; print(q.BINDING)"`
  prints `PySide6`.

## 1. Start / stop (twice)

| # | Check | OK |
|---|---|---|
| 1.1 | `scripts\block01` starts; splash/Step0 appears without a console traceback | |
| 1.2 | Close the window; the process ends within 5 s (Task Manager) | |
| 1.3 | Start again immediately; same window, no "Qt platform plugin" error | |
| 1.4 | Close with a project open (no crash, no "destroyed while thread is running" box) | |

## 2. Display scaling

Repeat 2.1–2.4 once at **100 %** and once at **150 %** (Windows display settings,
sign out/in if needed).

| # | Check | 100 % | 150 % |
|---|---|---|---|
| 2.1 | Step0 channel rows are readable; no clipped text in the row's right-hand controls | | |
| 2.2 | Regions overlay labels sit on their shapes (no half-pixel drift, no label cut off) | | |
| 2.3 | Dialog buttons are not cut off (Save/Apply/Cancel reachable) | | |
| 2.4 | GL viewers (Step0 full image, Step1, Step3, Regions) are sharp, not blurry | | |

## 3. Step0

| # | Check | OK |
|---|---|---|
| 3.1 | Open a project copy; thumbnails appear; the channel list fills | |
| 3.2 | `Show all` / `Hide all` toggles display visibility (no method assignment) | |
| 3.3 | Change TopHat radius / cuCIM sigma → one refresh, no 4–5 s pause | |
| 3.4 | Enter in a parameter box: one update, not two | |
| 3.5 | Save writes the corrected product (check the output folder timestamp) | |

## 4. Step1 / Step1.5 / Step2 / Step3

| # | Check | OK |
|---|---|---|
| 4.1 | Step1 opens, pans, zooms; GPU layers stay aligned with the picture | |
| 4.2 | Step1.5 channel checkboxes tick/untick, `Select all` mirrors them | |
| 4.3 | Step2 grid appears; change rows/cols twice (no leftovers, no crash) | |
| 4.4 | Step3 mask bar, colours, dash/alpha update live | |

## 5. Regions

| # | Check | OK |
|---|---|---|
| 5.1 | Draw a polygon, a circle (Shift), a band (Alt); each takes the shown tool | |
| 5.2 | Drag a shape: handles follow, the shape does not lag behind the cursor | |
| 5.3 | Zoom in/out while dragging: labels stay readable, shapes stay on their cells | |
| 5.4 | Select several shapes; selected outline is thicker; Delete removes them | |
| 5.5 | Numbering/sequence overlay draws over the shapes | |
| 5.6 | Leave the viewer (Step1 ↔ Regions) twice: no black picture, no crash | |

## 6. Themes

| # | Check | OK |
|---|---|---|
| 6.1 | Dark and light theme: text stays readable in every page | |
| 6.2 | Switch theme while the Regions overlay is on screen; labels keep their colour | |

## 7. Report back

For every failed line: the page, the action, what appeared instead, and
`~/.block01/logs/errors.log` (last 50 lines). A screenshot at 150 % for any
layout problem.
