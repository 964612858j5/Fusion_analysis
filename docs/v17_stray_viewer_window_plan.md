# The stray "No image loaded" window (backlog 13) — plan v0.2

Seen by the user (TG) 2026-11-11 on the Mac: after Step 0.5 Save, a click on
Step 1 opened Step1 normally, and a separate untitled window (about 930 ×
680) stood in front of it showing only "No image loaded / Load an OME-TIFF
in Step 0. Step 1 then shows the whole slide, …". The user asked for the
cause to be found with codex astra high.

## 1. Cause (found and reproduced)

* The text is `PLACEHOLDER_NO_DATASET`, the placeholder label of
  `Step1ViewerHost` (`ui/step1_viewer_host.py`).
* `Step1ViewerHost._show(widget)` (`:443`) swaps what the host shows: it
  takes every other child out with `child.setParent(None)` and makes
  `widget` visible.
* A rebuild of the viewer's source while the host is on screen runs
  `teardown()` → `_show(placeholder)` (the placeholder is added to the
  visible host's layout; Qt queues a "show if not hidden" for a widget added
  to a layout of a visible widget, `QLayout::addChildWidget`) and, at once,
  `open()` → `_show(new view)`, which detaches the placeholder with
  `setParent(None)`. When the queued show runs, the placeholder has no
  parent, so it becomes a window of its own, of the host's size.
  Reparenting hides a widget implicitly, which does not cancel the queued
  show; an explicit `hide()` does.
* In the app: entering Step1 makes the page visible, then `_step1_follow_step`
  syncs the source and resumes; a source change still pending since the
  dataset switches of the session (the log shows `gen=1`, `gen=2` and a new
  handoff) rebuilds the visible Step1 viewer exactly this way.
* **Reproduced** with the real `Step1ViewerHost` (offscreen, synthetic
  stacks: a host in a shown window, `open(A)`, events, `open(B)`, events):
  the placeholder `QLabel` ends up a visible top-level window, 908 × 658,
  **on HEAD 43e18b7 and on the uncommitted stage-1 tree alike** — it is not
  caused by backlog 11. The offscreen rig of `test_regions_r2` missed it
  because it replaces `_source_moved()` and skips the handoff reload.
* codex astra high (scratchpad `codex_g13_high.txt`) found the same chain
  and the same fix; a lower-confidence alternative (a host never installed
  in a layout, shown later) has no public path that leaves the layout None.

## 2. Fix

In `_show`, hide each outgoing child before detaching it:

```python
child.hide()
child.setParent(None)
```

The same probe with this change: no stray window; the host shows the new
view. Nothing else changes (the placeholder is shown again by the next
`_show(placeholder)`, which calls `setVisible(True)` on it).

## 3. Files (closed list)

* `ui/step1_viewer_host.py`: `_show` (one line).
* Tests (new): `tests/test_step1_viewer_host_no_stray_window.py`.
* This plan.

## 4. Tests

1. A host in a shown window (offscreen), synthetic stacks: `open(A)`,
   events, `open(B)`, events → no visible top-level widget other than the
   window; the host shows B's view; the placeholder has no parent and is
   hidden. Fails without the fix (checked when written).
2. `teardown()` alone, then events → the placeholder is shown inside the
   host (it is the host's child, visible), not a window.
3. Existing Step1 viewer / mount / Regions viewer tests: no new failures
   against a HEAD run on the Mac.

The committed tests assert these conditions (the scratchpad probes only
printed them).

Acceptance (user, Mac): the session of 2026-11-11 again (Step0 Save,
Regions, Save, Step 1): no extra window; after the rebuild Step1's picture
draws (GPU path) and pan / zoom work.

## 5. Risks and rollback

* All four whole-slide viewers (Step1, Step2's small one, Step3, Regions)
  use this host; the change only hides what is being removed.
* Rollback: revert the commit.

## 6. Plan review (codex astra low)

Round 1 PASS (scratchpad `codex_g13_plan_r1.txt`): cause and fix correct;
no new reparenting (the hide only precedes the existing one; the incoming
view is never hidden); the mount's other detaches are QObjects, and its GPU
widget is already hidden first. One P3 taken in v0.2: the tests assert, and
the acceptance checks the GPU picture and pan / zoom after the rebuild.

## 7. Implementation (uncommitted, 2026-11-11)

`ui/step1_viewer_host.py` `_show`: `child.hide()` before
`child.setParent(None)`. `tests/test_step1_viewer_host_no_stray_window.py`
(2): both pass with the fix and fail without it (checked). Code review
(codex astra low, with backlog 12): round 2 PASS.

Regression (one process per test; 38 Step0 / Step1 / viewer files, the
working tree against a HEAD worktree): 804 tests each side; 12 fail on
HEAD and the same 12 on the change; one more on the change:
`test_step0_step1_handoff_contract.py::test_another_dataset_still_starts_from_fresh_defaults`,
exit 139. Its body passes; the crash is in shiboken6 `SbkDeallocWrapperCommon`
during the suite's forced between-test `gc` (`conftest.py:78`), the
documented PySide6 6.11.3 issue on macOS (KNOWN_ISSUES; none on 6.9.3 in
the backlog-6 check). Bisected (5 runs each): HEAD, 11, 12, 13 alone,
12+13, 12+14, 13+14 and 12+13+14 pass; 11+12 together crash 4 of 5 (a
later single run passed): a gc-timing effect of the larger object graph,
not a product path.
