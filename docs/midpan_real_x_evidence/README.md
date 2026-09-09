# The Tissue Preview middle drag, measured on a real X server

Seven attempts at this gesture were each reasoned from a symptom, and the
seventh (`c6a7631`) said so in its own message: the root cause was
unconfirmed because no real X server had been measured. This directory
holds the measurement that replaced the guessing. Every number quoted
here is printed by the script shipped beside the logs, from the logs
shipped here -- run it rather than trusting the table.

## What is here

| File | What it is |
|---|---|
| `run1_before_the_fix.log.gz` | A real desktop run of the loaded application before any behaviour change: 125 gestures over three `OverviewPanel` instances, 1313 moves. |
| `run2_after_the_fix.log.gz` | A later real run with `673f44c` in place (its first, over-general form -- see below): 48 gestures, 686 moves. |
| `xtest_{cold,warm,raise_cold,raise_warm}.log.gz` | Four XTEST-driven runs of the harness against the narrowed rule, 2 gestures each: thumbnail just shown, after a left click, after `Step0Page._bring_to_front`, and that plus a click. |
| `analyze_midpan_log.py` | Recomputes everything below from any of those files. `--gestures` adds one line per gesture with its A/B/C/D verdict. |
| `harness/midpan_realx.py`, `harness/xinject.py` | The harness: hosts the REAL `TissueNavigatorPopup` on the running X server and drives the middle button with XTEST-injected pointer events. |

All logs are anonymised: memory addresses are replaced by stable labels
(`0x0001`, `0x0002`, …) so an object stays recognisable within a run, and
the monotonic clock is rebased to each run's first line. The lines carry
no paths, file names or slide identifiers -- event fields, timings and
object types only.

A gesture's identity is `(panel, gid)` and never `gid` alone: every panel
counts its own gestures from 1, and the page's own thumbnail and the
Tissue Navigator popup's are two panels in one process. Grouping by `gid`
merges unrelated drags -- it produces "gestures" with three presses
spanning minutes, and totals that are simply wrong.

## Reproducing

```bash
python docs/midpan_real_x_evidence/analyze_midpan_log.py \
    docs/midpan_real_x_evidence/run1_before_the_fix.log.gz \
    docs/midpan_real_x_evidence/run2_after_the_fix.log.gz
```

New logs come from the application itself:

```bash
cd /sda1/Fusion/analysis_pipline
BLOCK01_MIDPAN_DEBUG=1 BLOCK01_MIDPAN_LOG=/tmp/midpan.log \
    python -m block01_v14.main 2>&1 | tee /tmp/midpan_stderr.log
```

The switch is default-off and read per event, so a single run can be
instrumented without a code change; `BLOCK01_MIDPAN_LOG` appends the same
lines to a file, which is what makes a run drivable by the person with
the mouse and readable by someone else. The `tee` also captures
`[gui-watchdog]` stall traces. Note that the file is opened once per path:
renaming it mid-run does not start a new file, the process keeps writing
to the same inode (see "corrections" below).

The harness needs no installation -- `libXtst` through ctypes, no
`xdotool`:

```bash
DISPLAY=:1 HARNESS_X=2350 HARNESS_Y=420 \
BLOCK01_MIDPAN_LOG=/tmp/xtest_cold.log \
    python docs/midpan_real_x_evidence/harness/midpan_realx.py cold
```

It refuses to inject unless the window under the pointer belongs to it
(`XQueryPointer`'s child against its own window's ancestor chain -- a
reparenting window manager puts its frame in between), so a run on a
live desktop cannot click someone else's windows.

## The numbers

| Quantity | run 1 (before) | run 2 (after) |
|---|---|---|
| gestures `(panel, gid)` / panels | 125 / 3 | 48 / 1 |
| moves / camera steps | 1313 / 1313 | 686 / 672 |
| moves reporting `buttons=NoButton` | 38 | 14 |
| … of those, with `QApplication.mouseButtons() == Middle` | 38 | 14 |
| moves reporting some other button / stray moves | 0 / 0 | 0 / 0 |
| camera steps exactly undone by the next move | **31** | **0** |
| … undoing the gesture's FIRST step / a later step | **31 / 0** | 0 / 0 |
| camera step cost | p50 0.42, p99 1.77, max 8.06 ms | p50 0.35, p99 0.77 ms |
| first repaint after a step | p50 0.67, p99 9.35, max 42.3 ms | p50 0.55, p99 2.22, max 21.8 ms |
| move-to-move arrival gap | not recorded | p50 7.7, p90 45.1, p99 150.7, max 258.5 ms |
| `late_ms` = arrival gap − platform stamp gap | not recorded | p50 0.0, p90 3.4, p99 12.3, max 76.2 ms |
| 5 ms heartbeat gaps | 24 lines, worst 77.6 ms | 3 lines, worst 81.1 ms |
| gestures ended by a release / by a new press | 101 / 24 | 38 / 10 |

`31 / 0` is the count that names the cause. An exactly negated step is
neither a hand nor a coincidence: it says the later move carried a
position already visited. That it was ALWAYS the FIRST step being undone
says which position -- the press -- and when: while the pointer grab is
being established.

run 2 shows `refused as a grab replay: 0` because it ran the first,
over-general form of the fix, which returned before naming the refusal;
its 14 refusals are visible as 686 moves producing 672 camera steps. The
narrowed rule logs each one as `stale-grab-move`.

The four XTEST runs (24 moves each, 96 in total) all read: 24 moves, 24
camera steps, 0 blank moves, 0 refusals, 0 undone pairs, step cost ~0.2
ms, first repaint ~0.4 ms. Their `arr_gap` p99 of 1000 ms is the
harness's own deliberate one-second hold between the press and the first
move, not a stall.

## A. A failing gesture (run 1, before the fix)

```
      0.00 ms  press            buttons=Middle   app_buttons=Middle
      0.31 ms  move             buttons=Middle   app_buttons=Middle
      1.02 ms  move-applied     dx=-69.9880   dy=54.4351    took=0.56ms moved=True
      2.04 ms  paint            after_step=1.04ms
      5.49 ms  move             buttons=NoButton app_buttons=Middle
      6.08 ms  move-applied     dx=69.9880    dy=-54.4351   took=0.35ms moved=True
      6.56 ms  cancel           closed_by=a new press arrived on an open gesture
```

Press taken, first move handled in 0.56 ms, on screen 1.04 ms later --
and 4 ms after that a move reporting no buttons, while the application
still reads middle-down, puts the picture back with `dx` and `dy` negated
to the last digit. The camera did move both times; what the hand gets is
the first fraction of the drag thrown away. Because the anchor moves back
with it the drag still ENDS in the right place, which is why every test
that checked the final range passed while the desk reported a middle drag
that does nothing at first.

Classification: **B** -- the press arrived, the moves were handled, and
an event this code accepted destroyed the step. Not A (the press is
here), not C (repaint 1.04 ms), not D (no heartbeat gap).

## B. A normal gesture in the same run

```
      0.00 ms  press            buttons=Middle   app_buttons=Middle
     80.75 ms  move             buttons=Middle   app_buttons=Middle
     81.89 ms  move-applied     dx=-46.6587   dy=-7.7764    took=0.70ms moved=True
     83.02 ms  paint            after_step=1.15ms
    115.39 ms  move             buttons=Middle   app_buttons=Middle
    117.26 ms  move-applied     dx=-69.9880   dy=-54.4351   took=0.59ms moved=True
    119.23 ms  paint            after_step=1.99ms
    122.93 ms  move             buttons=Middle   app_buttons=Middle
    124.10 ms  move-applied     dx=-132.1996  dy=-132.1996  took=0.59ms moved=True
```

Every move handled once, in under a millisecond, on screen within two,
nothing undone. The 80 ms and 35 ms gaps between moves are not the code's
-- see D.

## C. The same replayed move, refused (run 2, after the fix)

```
      0.00 ms  press            buttons=Middle   app_buttons=Middle
      0.29 ms  move             buttons=Middle   app_buttons=Middle  arr_gap=0.3   ts_gap=1.0   late_ms=-0.7
      1.04 ms  move-applied     dx=-92.4069   dy=-53.9040   took=0.53ms moved=True
      1.79 ms  paint            after_step=0.76ms
      4.91 ms  move             buttons=NoButton app_buttons=Middle  arr_gap=4.6   ts_gap=-1.0  late_ms=5.6
      5.51 ms  cancel           closed_by=a new press arrived on an open gesture
```

Same shape, same 5 ms, and this time no second `move-applied`: the
replayed position is refused and the step stands. Across run 2 that
happened 14 times, giving 672 camera steps from 686 moves and zero undone
pairs.

The refusal matches the measured signature and nothing wider. All five
clauses are observations:

* the event reported no button (38 of 38 such moves in run 1);
* the application still reads middle-down (38 of 38);
* this gesture has made exactly ONE camera step, so the refusal is
  bounded to the grab-establishment phase where all 31 were seen -- by a
  fact, rather than by inventing a 4-17 ms window;
* the position is bit-identical to this gesture's press;
* we are not already sitting there, where the move is a no-op anyway.

So a hand that drags out over several steps and legitimately comes back
to the press position -- on a move the platform failed to label -- keeps
panning, as does a blank move at a real new position at any point in the
drag. Refusing either would rebuild the reported complaint from the other
side.

## D. What the stutter is, and what it is not

```
      0.00 ms  move             buttons=Middle   app_buttons=Middle  arr_gap=3.4   ts_gap=0.0   late_ms=3.4
      0.64 ms  move-applied     dx=2.5669     dy=0.0000     took=0.28ms moved=True
      1.16 ms  paint            after_step=0.53ms
    258.50 ms  move             buttons=Middle   app_buttons=Middle  arr_gap=258.5 ts_gap=262.0 late_ms=-3.5
    259.55 ms  move-applied     dx=-10.2674   dy=-7.7006    took=0.69ms moved=True
```

A quarter-second hole in the middle of a drag. The platform's own stamps
on those two events are 262 ms apart, so the event was not waiting: it
was produced 262 ms after its predecessor and handled 3.5 ms EARLY
relative to that. Across run 2, of 37 holes longer than 80 ms, exactly 1
had the event itself late by more than 40 ms.

What that supports, and all it supports:

> The stutter is not in this application's current handling or drawing
> path. The long holes open before the event receives the platform's
> timestamp, or the input is not produced continuously.

It does NOT identify the hand as the cause. A mouse, its driver, the X
server, or event coalescing upstream of the timestamp can all open a gap
before Qt ever sees it, and this log cannot separate those. The analyzer
therefore leaves such holes unclassified.

## Corrections to earlier reports

* An earlier version of this README said run 1 was "106 gestures, 816
  moves". That was a snapshot taken while the application was still
  appending to the file: the log had been renamed, but the process holds
  the handle it opened, so it kept writing to the same inode. The
  committed file has 125 gestures and 1313 moves. The conclusion is
  unchanged and stronger on the full file: 31 undone steps, 31 of 31 of
  them the gesture's first.
* An earlier analyzer grouped by `gid` alone and so merged the three
  panels' gestures. It now groups by `(panel, gid)` and prints every
  summary this README states.
* "A hand that paused" was a claim beyond the instrument; see D.

## Still unexplained

* The failure originally reported -- a middle drag doing nothing at the
  start, in Step1 especially -- has NOT been reproduced with the
  diagnostic on. The fix addresses a defect the instrument proved is
  there and which has that exact shape; it is not confirmation that what
  the user saw is gone.
* Duplicate presses do occur: 24 of run 1's gestures and 10 of run 2's
  ended because another press arrived on an open gesture. With the anchor
  taken at the new press the picture does not move when it happens, and
  nothing measured says it is felt, so it is untouched.
* Whatever opens the pre-timestamp holes in D is outside this log.
  Answering it needs an instrument below Qt -- XInput event stamps, or
  the device's own report rate.
* The refusal itself has not been exercised by XTEST, and cannot be:
  injected moves always carry `buttons=Middle`, while the signature needs
  a move with no button at the press position, which only the platform's
  own grab replay produces. The next real-application run with the
  diagnostic on will show it as `stale-grab-move` lines, with
  `moves - camera steps` equal to their count.

## Cost of leaving the diagnostic in

Not zero, low: with `BLOCK01_MIDPAN_DEBUG` unset, each middle-button
event and each viewport repaint costs one `os.environ` read and returns.
No timer is created, no timestamps are taken, no state is kept. Whether
to keep roughly 300 lines of diagnostic in production code should be
re-decided once this gesture's remaining question is closed.
