"""v16_5 S-Raw, the user's question: how much GUI-thread time does the
Python still left in the viewer take per gesture?

Reads a perf log written with BLOCK01_A9_ATTR=1 (and its .xwheel.jsonl).
Per gesture (runs of injections; the stop = the last injection) and in
two windows -- DURING the gesture and the 400 ms AFTER the stop -- the
GUI-thread spans are cut into exclusive segments (each moment belongs to
the innermost span open then, so nested spans are not counted twice) and
summed by group:

  wheel    pyqtgraph's ViewBox.wheelEvent (attr.vb_wheel)
  drag     pyqtgraph's ViewBox.mouseDragEvent (attr.vb_drag)
  camera   ExploreController._on_range_changed (attr.ctl_range) and the
           mount's range slots (attr.mount_range / attr.mount_user)
  rect     the Tissue Preview rectangle (attr.view_rect)
  core     the binding's call into Rust (attr.core_set_view)
  paint    the layer's paintGL (gpu.paint / attr.core_paint; the Rust
           work inside it is the core's, the shell around it Python)
  scene    pyqtgraph's scene paint (attr.scene_paint)
  other    every other span (planning / publication on the Python path)

and per input event (notch or drag step): the mean of wheel+drag+camera+
rect+core, the Python a single input costs before anything is drawn.

Usage: sraw_python_share.py PERF_LOG [LABELS_COMMA]
"""

import json
import re
import sys

LINE = re.compile(r"^PERF t=(\S+) ev=(\S+)(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")
WORKER = {"sched.read", "tissue.compose", "preview.compose", "preview.gray", "tissue.seed"}
GROUPS = {"attr.vb_wheel": "wheel", "attr.vb_drag": "drag", "attr.ctl_range": "camera",
          "attr.mount_range": "camera", "attr.mount_user": "camera", "attr.view_rect": "rect",
          "attr.core_set_view": "core", "gpu.paint": "paint", "attr.core_paint": "paint",
          "attr.scene_paint": "scene"}
ORDER = ["wheel", "drag", "camera", "rect", "core", "paint", "scene", "other"]
AFTER_S = 0.4


def spans_of(path):
    out = []
    for line in open(path, errors="replace"):
        m = LINE.match(line.strip())
        if not m:
            continue
        t, ev, rest = float(m.group(1)), m.group(2), dict(FIELD.findall(m.group(3)))
        if "dur_ms" in rest and ev not in WORKER and not ev.startswith("gui."):
            out.append((t - float(rest["dur_ms"]) / 1000.0, t, ev))
    return out


def exclusive(spans):
    """[(begin, end, event)] -> [(begin, end, event)] innermost only."""
    marks = sorted([(b, 0, -e, ev) for b, e, ev in spans] + [(e, 1, -e, ev) for b, e, ev in spans])
    out, stack, cursor = [], [], None
    for t, kind, neg_end, ev in marks:
        if stack and cursor is not None and t > cursor:
            out.append((cursor, t, stack[-1][1]))
        cursor = t
        if kind == 0:
            stack.append((-neg_end, ev))
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][1] == ev and abs(stack[i][0] - t) < 1e-9:
                    del stack[i]
                    break
    return out


def gestures(injections):
    runs, current = [], []
    for r in injections:
        if r.get("seq") == 1 and current:
            runs.append(current)
            current = []
        current.append(r)
    if current:
        runs.append(current)
    return [(run[0]["t"], run[-1]["t"], len(run)) for run in runs]


def window_sum(segments, start, end):
    sums = dict.fromkeys(ORDER, 0.0)
    for b, e, ev in segments:
        lo, hi = max(b, start), min(e, end)
        if hi > lo:
            sums[GROUPS.get(ev, "other")] += (hi - lo) * 1000.0
    return sums


def main(path, labels=""):
    segments = exclusive(spans_of(path))
    injections = [json.loads(l) for l in open(path + ".xwheel.jsonl") if l.strip()]
    names = labels.split(",") if labels else []
    report = []
    for i, (start, stop, n) in enumerate(gestures(injections)):
        during = window_sum(segments, start, stop)
        after = window_sum(segments, stop, stop + AFTER_S)
        per_input = sum(during[g] for g in ("wheel", "drag", "camera", "rect", "core")) / max(1, n)
        span = (stop - start) * 1000.0
        label = names[i] if i < len(names) else f"g{i + 1}"
        report.append({"gesture": label, "inputs": n, "during_ms": round(span, 1),
                       "during": {k: round(v, 1) for k, v in during.items()},
                       "after": {k: round(v, 1) for k, v in after.items()},
                       "python_per_input_ms": round(per_input, 2)})
        print(f"{label:11s} n={n:3d} span={span:7.1f} | during " +
              " ".join(f"{k}={during[k]:.0f}" for k in ORDER) +
              f" | per input {per_input:.2f} ms | after " +
              " ".join(f"{k}={after[k]:.0f}" for k in ORDER if after[k] >= 0.5))
    return report


if __name__ == "__main__":
    main(*sys.argv[1:])
