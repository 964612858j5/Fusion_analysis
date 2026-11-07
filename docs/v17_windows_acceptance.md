# Windows / WSL acceptance — PySide6 build (v17, block RT2)

**Windows has not been run for this block.** Everything below is a checklist
for the person who runs it, plus the gaps the repository still has.

## 0. What actually exists for Windows today

| | State |
|---|---|
| `scripts/install_block01.sh` | bash only (a POSIX shell script) |
| `scripts/block01` | bash only; it sources `.block01/install.env` and `exec`s `$BLOCK01_PREFIX/bin/python` |
| `.ps1` / `.bat` launcher or installer | **does not exist** in this repository |
| CI | builds the native wheel for Windows; the GUI jobs are Linux/offscreen |

So there are two honest routes, and neither is "double-click":

* **WSL2 (Ubuntu)** — the supported Linux flow, unchanged: it is a Linux
  environment, so the Linux instructions apply. GL viewers run through WSLg
  (Windows 11) or an X server; this is **not** native Windows and does not
  test Windows font/DPI/WGL behaviour.
* **Native Windows** — requires a POSIX shell (Git Bash/MSYS2) to run the
  bash scripts *and* a Python 3.10 environment. **This is untested and not a
  supported entry point**: `scripts\block01` is not a PowerShell command, and
  `scripts/block01` assumes POSIX paths (`/`), `exec`, `ln -s` and
  `source`. Treat native Windows as a **gap**, not as a route to certify.

### Native-Windows gap list (what would have to be added first)

1. A PowerShell/CMD installer that creates the environment (or a documented
   `py -3.10` + `pip install -r envs\ci\requirements-cpu.txt` +
   `pip install PySide6-Essentials==6.11.3 shiboken6==6.11.3`), writes
   `install.env` and creates `.block01\pkg\block01` (a junction/symlink or a
   `.pth` file) — `ln -s` does not work without Developer Mode.
2. A launcher (`block01.cmd`/`.ps1`) equivalent to `scripts/block01`
   (`check_install`, the GPU check, `.block01\run` for CUDA logs).
3. The viewer core: `fusion_viewer_core` must be importable on Windows; the
   repo's install check refuses to start without an accepted core version.

Until 1–3 exist, a native-Windows run can only be an experiment, and its
result must not be reported as "Windows passes".

## 1. If you run it in WSL2 (Ubuntu 22.04+)

```bash
# inside WSL, a Linux checkout
sudo apt install libxcb-cursor0        # Qt >= 6.5 xcb plugin (new since PySide6)
bash scripts/install_block01.sh        # writes .block01/install.env for THIS checkout
scripts/block01
```
Check first: `python -c "import block01.qt as q; print(q.BINDING)"` prints
`PySide6`; `pip list | grep -i pyqt5` prints nothing.

## 2. If you experiment on native Windows (Git Bash)

```bash
# Git Bash, Python 3.10 on PATH as `python`
python -m venv /c/block01/envs/pyside6
/c/block01/envs/pyside6/Scripts/python -m pip install PySide6-Essentials==6.11.3 shiboken6==6.11.3
/c/block01/envs/pyside6/Scripts/python -m pip install -r envs/ci/requirements-cpu.txt
```
Then the launcher still needs the POSIX assumptions of §0; expect to fix
paths. Logs: `%USERPROFILE%\.block01\logs\errors.log` (the error hook writes
`~/.block01/logs`, which is `C:\Users\<you>\.block01\logs`).

## 3. Checks (both routes) — 100 % and 150 % scaling

Repeat the table once at **100 %** and once at **150 %**.

| # | Check | 100 % | 150 % |
|---|---|---|---|
| 3.1 | App starts, Step0 appears, no console traceback | | |
| 3.2 | Close the window; process ends within 5 s | | |
| 3.3 | Start again immediately; no "Qt platform plugin" error | | |
| 3.4 | Step0 rows readable; right-hand controls not clipped | | |
| 3.5 | Regions labels sit on their shapes; no half-pixel drift | | |
| 3.6 | Dialog buttons (Save/Apply/Cancel) fully visible | | |
| 3.7 | Step0 full image / Step1 / Step3 / Regions GL views are sharp | | |

## 4. Functional checks

| # | Check | OK |
|---|---|---|
| 4.1 | Open a **copy** of a project; thumbnails fill the channel list | |
| 4.2 | `Show all` / `Hide all` change display visibility only | |
| 4.3 | TopHat radius / cuCIM sigma change → one refresh, no 4–5 s pause | |
| 4.4 | Enter in a parameter box updates once, not twice | |
| 4.5 | Save: the product is written **and can be re-opened**; not just a new timestamp | |
| 4.6 | Step1 pans/zooms; GPU layers stay aligned | |
| 4.7 | Step1.5 checkboxes and `Select all` agree | |
| 4.8 | Step2 grid: change rows/cols twice (no leftovers, no crash) | |
| 4.9 | Step3 mask bar: colour, dash, alpha update live | |
| 4.10 | Regions: draw polygon/circle(Shift)/band(Alt); drag; zoom while dragging | |
| 4.11 | Regions: select several, Delete removes them, labels renumber | |
| 4.12 | Step1 ↔ Regions twice: no black picture, no crash | |
| 4.13 | Dark and light themes: text readable everywhere | |

## 5. GPU vs CPU

Before any timing check, print the backend:
`python -c "from block01.core import bg_correction as b; print(b.gpu_filter_backend())"`.
A CPU machine (or a failed GPU smoke test, see the startup `[GPU]` line) must
**not** be held to the GPU latency numbers measured on the A9 server.

## 6. Report back

For each failed line: page, action, what appeared instead, and
`~/.block01/logs/errors.log` (last 50 lines), plus a screenshot at 150 % for
layout problems. State explicitly whether the run was native Windows or WSL.
