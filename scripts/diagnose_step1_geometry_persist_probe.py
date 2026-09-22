"""G3.2b.5A.5 step 1: why did the geometry worker not publish?

5A.4 bound a temp directory and still got zero files and no
`geometry_committed`. Pointing `_roi_context["step_dirs"]["step0"]` at an
empty `/tmp` folder is evidently not a valid project. This records WHY, from
the worker itself rather than by reading the code:

  * the exact task `Step0Page._geometry_persist_task()` builds;
  * what `GeometryPersistWorker.submit(task)` returns;
  * `worker.stats()` before and after;
  * and whichever of `published` / `skipped(reason)` / `failed(error)` the
    worker actually emits, with its full payload.

ONE gesture only -- enough to produce one task. No repeated dragging.

Read-only with respect to production: every wrapper calls the real function
and returns its real result. Writes, if the worker makes any, go to the temp
project under /tmp and nowhere else.

Usage: python scripts/diagnose_step1_geometry_persist_probe.py OUT.json
"""

import importlib
import json
import os
import pathlib
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.basename(_ROOT)
for extra in (os.path.dirname(_ROOT), os.path.join(_ROOT, "scripts"),
              os.path.join(_ROOT, "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

TMP = pathlib.Path("/tmp/g325a5_temp_project")
REC = []
T0 = time.monotonic()


def rec(kind, **fields):
    REC.append({"t_ms": round((time.monotonic() - T0) * 1000, 3),
                "kind": kind, **fields})
    return REC[-1]


def small(value, depth=0):
    """Bounded rendering: a task carries whole ROI/patch lists."""
    if depth > 3:
        return "<deep>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): small(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        head = [small(v, depth + 1) for v in list(value)[:6]]
        return head + ([f"<+{len(value) - 6} more>"] if len(value) > 6 else [])
    return str(value)[:400]


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/tmp/geometry_persist_probe.json")
    from PyQt5 import QtWidgets
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

    bench = importlib.import_module("diagnose_step1_patch_draw_wobble")
    pkg = "block01" if "block01" in sys.modules else PKG
    Step0Page = importlib.import_module(f"{pkg}.ui.step0.step0_page").Step0Page
    worker_mod = importlib.import_module(f"{pkg}.workers.geometry_persist_worker")
    WorkerCls = worker_mod.GeometryPersistWorker

    # ── record the task, the submit return, and the outcome signals ──
    real_task = Step0Page._geometry_persist_task
    real_submit = WorkerCls.submit
    real_stats = WorkerCls.stats

    def task_fn(self):
        try:
            task = real_task(self)
        except Exception as exc:                            # noqa: BLE001
            rec("task.raised", error=repr(exc))
            raise
        rec("task.built", task=small(task),
            task_keys=sorted(task.keys()),
            step0_dir=task.get("step0_dir"),
            step0_dir_exists=bool(task.get("step0_dir")
                                  and os.path.isdir(task["step0_dir"])),
            manifest_path=task.get("step0_manifest_path"),
            manifest_exists=bool(task.get("step0_manifest_path")
                                 and os.path.isfile(task["step0_manifest_path"])),
            n_rois=len(task.get("rois") or []),
            n_patches=len(task.get("patches") or []),
            revision=task.get("revision"), dataset_gen=task.get("dataset_gen"),
            roi_context_changed=task.get("roi_context_changed"))
        return task

    def submit_fn(self, task):
        before = real_stats(self)
        result = real_submit(self, task)
        rec("worker.submit", returned=small(result),
            stats_before=small(before), stats_after=small(real_stats(self)))
        return result

    Step0Page._geometry_persist_task = task_fn
    WorkerCls.submit = submit_fn

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    bench.APP = app
    bench.commit_geometry = lambda *_a, **_kw: None   # the WORKER must publish

    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "step0").mkdir(parents=True)

    window, loader = bench.real_window()
    window.resize(1400, 900)
    window.show()
    bench.pump(600)
    window._stack.setCurrentWidget(window._step1_page_widget)
    bench.pump(600)
    window.right_tabs.setCurrentWidget(window.viewer_tab)
    bench.pump(600)

    page = window._step0
    ctx = dict(getattr(page, "_roi_context", None) or {})
    ctx["step_dirs"] = dict(ctx.get("step_dirs") or {})
    ctx["step_dirs"]["step0"] = str(TMP / "step0")
    page._roi_context = ctx
    rec("bench.roi_context_set", roi_context=small(ctx))

    connect_worker_early = True
    popup = window._display.show_navigator()
    popup.set_overview_context(loader=loader, nuc_ch=page.nucleus_channel,
                               rois=[], patches=[],
                               dataset_token=str(loader.filepath))
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        bench.pump(250)
        panel = popup.overview
        if int(getattr(panel, "ov_h", 0) or 0) > 8 and panel.img_item.image is not None:
            break
    panel = popup.overview
    assert panel.img_item.image is not None, "no thumbnail; refusing to drag"

    # THE WORKER EXISTS ONLY AFTER FIRST USE, and it skipped before the
    # first run could connect -- so the stats showed `skipped: 1` with no
    # reason. Create it through the page's own accessor first, then connect,
    # so the reason string itself is captured.
    page._geometry_persist()
    connected = {"done": False}

    def connect_worker():
        worker = getattr(page, "_geometry_persist_worker", None)
        if worker is None or connected["done"]:
            return
        worker.published.connect(lambda p: rec("worker.published", payload=small(p)))
        worker.skipped.connect(lambda p: rec("worker.SKIPPED", payload=small(p),
                                             reason=small((p or {}).get("reason"))))
        worker.failed.connect(lambda p: rec("worker.FAILED", payload=small(p),
                                            error=small((p or {}).get("error"))))
        connected["done"] = True
        rec("bench.worker_signals_connected",
            is_running=bool(worker.isRunning()))

    connect_worker()
    rec("bench.connected_before_gesture", ok=connected["done"])

    # ── ONE gesture ────────────────────────────────────────────────
    n0 = len(panel._patches)
    rec("gesture.start")
    bench.draw_patch(popup, _Probe(window), fraction=(0.25, 0.25, 0.55, 0.55))
    connect_worker()
    for _ in range(60):
        bench.pump(500)
        connect_worker()
        if any(p.is_file() for p in TMP.rglob("*")):
            break
    rec("gesture.end", patches_added=len(panel._patches) - n0)

    worker = getattr(page, "_geometry_persist_worker", None)
    files = sorted(str(p.relative_to(TMP)) for p in TMP.rglob("*") if p.is_file())
    report = {
        "block": "G3.2b.5A.5 (step 1: why no publish)",
        "temp_project": str(TMP),
        "files_written": files,
        "worker_exists": worker is not None,
        "worker_running": bool(worker.isRunning()) if worker else None,
        "worker_stats_final": small(worker.stats()) if worker else None,
        "geometry_persist_state": getattr(page, "_geometry_persist_state", None),
        "records": REC,
    }
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {out}")
    print(f"files written: {files}")
    print(f"persist state: {report['geometry_persist_state']}")
    print(f"worker stats : {report['worker_stats_final']}")
    for r in REC:
        if r["kind"] in ("task.built", "task.raised", "worker.submit",
                         "worker.published", "worker.SKIPPED", "worker.FAILED",
                         "gesture.end"):
            print(f"  {r['t_ms']:9.1f} {r['kind']}")
            for k in ("step0_dir", "step0_dir_exists", "manifest_path",
                      "manifest_exists", "n_rois", "n_patches", "revision",
                      "dataset_gen", "roi_context_changed", "returned",
                      "reason", "error", "patches_added"):
                if k in r:
                    print(f"        {k} = {r[k]}")
    return 0


class _Probe:
    def __init__(self, window):
        self.w = window

    def mark(self, *_a, **_kw):
        return None


if __name__ == "__main__":
    sys.exit(main())
