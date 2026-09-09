# The Tissue Preview middle drag, measured on a real X server

Seven attempts at this gesture were each reasoned from a symptom, and the
seventh (`c6a7631`) said so in its own message: the root cause was
unconfirmed because no real X server had been measured. This directory
holds the measurement that replaced the guessing, so the numbers quoted
in `673f44c`, `7ed2960` and `f7dcbf4` can be recomputed by anyone.

## What is here

| File | What it is |
|---|---|
| `run1_before_the_fix.log.gz` | 4312 log lines from a real desktop run of the loaded application, before any behaviour change. 106 gestures, 816 moves, three `OverviewPanel` instances. |
| `run2_after_the_fix.log.gz` | 2157 lines from a later real run with `673f44c` in place. 48 gestures, 686 moves. |
| `analyze_midpan_log.py` | One gesture per line, plus the A/B/C/D classification. Reads either file directly. |

Both logs are anonymised: every memory address is replaced by a stable
label (`0x0001`, `0x0002`, …) so the same object stays recognisable
within a run, and the monotonic clock is rebased to the run's first
line. The lines contain no paths, file names or slide identifiers -- only
event fields, timings and object types.

## How they were produced

```bash
cd /sda1/Fusion/analysis_pipline
BLOCK01_MIDPAN_DEBUG=1 BLOCK01_MIDPAN_LOG=/tmp/midpan.log \
    python -m block01_v14.main 2>&1 | tee /tmp/midpan_stderr.log
```

The switch is default-off and read per event (`MID_PAN_DEBUG_ENV`), so a
single run can be instrumented without a code change. `BLOCK01_MIDPAN_LOG`
appends the same lines to a file, which is what makes a run drivable by
the person with the mouse and readable by someone else. The `tee` also
captures `[gui-watchdog]` stall traces.

Independent of the application, a harness drives the REAL
`TissueNavigatorPopup` on the running X server with XTEST-injected
pointer events (`libXtst` via ctypes; no `xdotool` needed), which is the
one path the offscreen tests cannot exercise. It found the gesture
correct in all four activation states, including after
`Step0Page._bring_to_front` -- where mutter answers with
`isActiveWindow() == False` and panning still works -- so window
activation is not a precondition, and the first move's `buttons()` is
`MiddleButton` on this machine, which is what `c6a7631` had guessed
otherwise.

## Recomputing the numbers

```bash
python docs/midpan_real_x_evidence/analyze_midpan_log.py \
    docs/midpan_real_x_evidence/run1_before_the_fix.log.gz
python docs/midpan_real_x_evidence/analyze_midpan_log.py \
    docs/midpan_real_x_evidence/run2_after_the_fix.log.gz
```

| Quantity | run 1 (before) | run 2 (after) |
|---|---|---|
| gestures / moves | 106 / 816 | 48 / 686 |
| moves reporting `buttons=NoButton` | 38 | 14 |
| camera steps exactly undone by the next move | 31 pairs, in 26 gestures | 0 |
| … of which undid the gesture's FIRST step | 31 of 31 | — |
| camera step cost | p50 0.42 ms, p99 1.77 ms | p50 ~0.4 ms |
| first repaint after a step | p50 0.67 ms, p99 9.35 ms | p50 0.55 ms, p99 2.22 ms |
| 5 ms heartbeat gaps | 24 lines, worst 77.6 ms | 3 lines, worst 81.1 ms |
| move-to-move arrival gap | p50 8.3, p90 46, p99 148, max 308 ms | p50 7.7, p90 45, p99 151, max 259 ms |
| `late_ms` (arrival gap minus the platform's own stamp gap) | not recorded | p50 0.0, p90 3.4, p99 12.3, max 76.2 ms |

`31 of 31` is the count that named the cause. An exactly negated step is
not a hand and not a coincidence: it says the following move carried a
position already visited. That it was ALWAYS the first step being undone
says which position -- the press.

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
and 4 ms after that a move reporting no buttons puts the picture back,
`dx` and `dy` negated to the last digit. The camera did move both times;
what the hand gets is the first fraction of the drag thrown away. Because
the anchor moves back with it, the drag still ENDS in the right place,
which is why every test that checked the final range passed while the
desk reported a middle drag that does nothing at first.

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

Every move is handled once, in under a millisecond, and reaches the
screen within two. Nothing is undone. The gaps between moves (80 ms,
35 ms, 7 ms) are the hand's, not the code's -- see D.

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
replayed position is refused, the step stands. Across run 2 that happened
14 times and produced 672 camera steps from 686 moves, with zero undone
pairs.

The refusal is deliberately narrow -- no button reported AND the position
bit-identical to this gesture's press AND not already there. A platform
that fails to report buttons while giving a real new position keeps
panning and keeps its anchor; refusing that whole class would rebuild the
same complaint from the other side, and nothing measured here licenses
it.

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
before Qt ever sees it, and this log cannot separate those. Saying "the
hand paused" would be a claim beyond the instrument.

## Still unexplained

* The failure originally reported -- a middle drag doing nothing at the
  start, in Step1 especially -- has NOT been reproduced with the
  diagnostic on. `673f44c` fixes a defect the instrument proved is there
  and which has that exact shape; it is not confirmation that what the
  user saw is gone.
* 8 of run 1's 106 presses arrived within 50 ms of the previous press, so
  duplicate presses do occur on this machine and restart the gesture.
  With the anchor taken at the new press the picture does not move when
  it happens, and nothing measured says it is felt, so it is untouched.
* Whatever opens the pre-timestamp holes in D is outside this log.
  Answering it needs an instrument below Qt (XInput event stamps, or the
  device's own report rate).

## Cost of leaving the diagnostic in

Not zero, low: with `BLOCK01_MIDPAN_DEBUG` unset, each middle-button
event and each viewport repaint costs one `os.environ` read and returns.
No timer is created, no timestamps are taken, no state is kept. Whether
to keep roughly 300 lines of diagnostic in production code should be
re-decided once this gesture's remaining question is closed.
