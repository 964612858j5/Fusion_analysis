"""The pre-segmentation run in the background (plan block C, step 3).

No Qt: a plain thread, so it can be driven and tested without a window; the
page turns its callbacks into signals. One run:

  1. publish run.json -- the frozen snapshot the caller built at Run: patch
     boxes, combinations and tasks, the committed fusion snapshot, the source
     (pixel key), the HALO and the analysis region (plan 7.4);
  2. per engine, in a fixed order and one engine process at a time (plan
     7.10.5): build every input that engine's tasks need -- one per (patch,
     input kind), shared by the combinations -- then hand the tasks over;
  3. per settled task: keep what the patch owns (`core.label_ownership`),
     crop it to the patch, publish masks then the record (plan 7.3), report.

Every task gets exactly one record: ok, failed (with the reason), or
cancelled (Stop). Nothing here chooses a result.
"""

import os
import shutil
import threading
import traceback

import numpy as np

from ...core import label_ownership, preseg_input, preseg_run
from ...seg_runner import engines as seg_engines
from ...seg_runner.client import EngineProcess, EngineStartError

ENGINE_ORDER = ("cellpose", "stardist", "mesmer")


def open_loader(spec):
    """A loader of its own for the run's thread: the slide, and the corrected
    channels as the Step0 handoff decided them."""
    from ...core.io_loader import OMETIFFLoader
    loader = OMETIFFLoader(spec["ome_path"], spec.get("name_map") or {},
                           correction_config=spec.get("correction_config"))
    loader.set_corrected_zarr_store(spec.get("corrected_zarr_path"),
                                    spec.get("corrected_decisions") or {})
    return loader


class PresegRunJob:
    def __init__(self, step1_dir, run, loader_factory, python=None,
                 on_record=None, on_progress=None, on_finished=None):
        self.step1_dir = step1_dir
        self.run = run
        self.loader_factory = loader_factory
        self.python = python
        self.on_record = on_record
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.rdir = preseg_run.run_dir(step1_dir, run["run_id"])
        self.records = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._engine = None
        self._thread = None
        self.error = ""

    # ── control ──────────────────────────────────────────────────────
    def start(self):
        self._thread = threading.Thread(target=self._main, name="preseg-run", daemon=True)
        self._thread.start()

    def stop(self):
        """User Stop: the task in flight and every later one end cancelled."""
        self._stop.set()
        ep = self._engine
        if ep is not None:
            ep.stop()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_running()

    def close(self, timeout=30.0):
        """Stop and make sure no engine process is left (window close)."""
        self.stop()
        if not self.wait(timeout):
            ep = self._engine
            if ep is not None:
                ep.terminate(grace=1.0)
            self.wait(5.0)

    # ── the run ──────────────────────────────────────────────────────
    def _main(self):
        try:
            preseg_run.write_run(self.step1_dir, self.run)
            tasks = list(self.run.get("tasks") or [])
            by_engine = {}
            for t in tasks:
                by_engine.setdefault(seg_engines.METHOD_ENGINE[t["method"]], []).append(t)
            loader = None
            for engine in ENGINE_ORDER:
                group = by_engine.get(engine) or []
                if not group:
                    continue
                if self._stop.is_set():
                    self._settle_all(group, preseg_run.CANCELLED, "stopped by user")
                    continue
                if loader is None:
                    try:
                        loader = self.loader_factory()
                    except Exception as exc:  # noqa: BLE001 -- every task is told
                        self._settle_all(tasks, preseg_run.FAILED, f"could not open the slide: {exc}")
                        break
                self._run_engine(engine, group, loader)
        except Exception:  # noqa: BLE001 -- the run reports, it does not crash the app
            self.error = traceback.format_exc()[-4000:]
            self._settle_all(self.run.get("tasks") or [], preseg_run.FAILED, self.error)
        finally:
            self._engine = None
            for sub in ("inputs", "raw"):
                shutil.rmtree(os.path.join(self.rdir, sub), ignore_errors=True)
            if self.on_finished is not None:
                self.on_finished(dict(self.records))

    def _run_engine(self, engine, group, loader):
        inputs_dir = os.path.join(self.rdir, "inputs")
        raw_dir = os.path.join(self.rdir, "raw")
        os.makedirs(inputs_dir, exist_ok=True)
        os.makedirs(raw_dir, exist_ok=True)
        halo = int(self.run.get("halo_px") or 0)
        bounds = self.run["bounds"]
        roi = self.run.get("roi") or {}
        snapshot = self.run["fusion"]

        poly = roi.get("polygon_fullres")
        roi_mask = (preseg_input.RoiMask(poly, roi["bbox_fullres"])
                    if poly is not None and len(poly) >= 3 else None)
        windows, prepared, runner_tasks = {}, {}, []
        for t in group:
            if self._stop.is_set():
                break
            key = preseg_run.bbox_key(t["patch_bbox"])
            kind = preseg_input.INPUT_KIND[t["method"]]
            try:
                if key not in windows:
                    win = preseg_input.read_window(t["patch_bbox"], halo, bounds)
                    fused = preseg_input.fused_window(loader, win, snapshot, roi_mask)
                    windows[key] = (win, fused)
                win, fused = windows[key]
                path = os.path.join(inputs_dir, f"{key}.{kind}.npy")
                if path not in prepared:
                    np.save(path, preseg_input.model_input(kind, fused))
                    prepared[path] = True
            except Exception as exc:  # noqa: BLE001 -- this patch fails, the rest run
                self._settle(t, preseg_run.FAILED, f"input: {exc}")
                continue
            runner_tasks.append({"task_id": t["task_id"], "input": path, "out_dir": raw_dir,
                                 "params": dict(t["params"], method=t["method"]),
                                 "_task": t, "_window": win})
        windows.clear()
        if self._stop.is_set():
            self._settle_all(group, preseg_run.CANCELLED, "stopped by user")
            return
        if not runner_tasks:
            return

        ep = EngineProcess(engine, python=self.python,
                           log_path=os.path.join(self.rdir, f"engine_{engine}.log"))
        self._engine = ep
        if self._stop.is_set():                          # stopped while inputs were built
            self._engine = None
            self._settle_all(group, preseg_run.CANCELLED, "stopped by user")
            return
        try:
            hello = ep.start()
        except (EngineStartError, OSError) as exc:
            self._engine = None
            self._settle_all(group, preseg_run.FAILED, f"the {engine} engine did not start: {exc}")
            return
        self.run.setdefault("engines", {})[engine] = hello.get("identity")
        self.run.setdefault("devices", {})[engine] = hello.get("device")
        preseg_run.write_run(self.step1_dir, self.run)          # run.json gains the engine

        by_id = {rt["task_id"]: rt for rt in runner_tasks}

        def settled(tid, state, detail):
            rt = by_id[tid]
            if state == preseg_run.OK:
                self._finish_ok(rt, detail)
            else:
                self._settle(rt["_task"], state, detail)

        try:
            ep.run([{k: v for k, v in rt.items() if not k.startswith("_")} for rt in runner_tasks],
                   on_settle=settled)
        finally:
            if ep.proc is not None and ep.proc.poll() is None:
                ep.close()
            self._engine = None
        # Anything the client never settled (it always does) ends failed.
        self._settle_all(group, preseg_run.FAILED, "the engine gave no answer")

    def _finish_ok(self, rt, runner_record_path):
        import json
        t = rt["_task"]
        try:
            with open(runner_record_path, encoding="utf-8") as f:
                rrec = json.load(f)
            raw = {k: (np.load(rrec[k]["path"]) if (rrec.get(k) or {}).get("status") == "ok" else None)
                   for k in ("cell", "nucleus")}
            own = preseg_input.own_local(t["patch_bbox"], rt["_window"])
            masks, paired = own_masks(t["method"], raw, own)
            for k in ("cell", "nucleus"):
                p = (rrec.get(k) or {}).get("path")
                if p and os.path.exists(p):
                    os.remove(p)
            self._settle(t, preseg_run.OK, "", masks=masks, device=rrec.get("device_used", ""),
                         runtime_s=rrec.get("runtime_s"), paired=paired)
        except Exception:  # noqa: BLE001
            self._settle(t, preseg_run.FAILED, "result: " + traceback.format_exc()[-2000:])

    # ── settling ─────────────────────────────────────────────────────
    def _settle(self, task, status, error, **kw):
        with self._lock:
            if task["task_id"] in self.records:          # one record per task, final
                return
            rec = preseg_run.publish_result(self.rdir, self.run, task, status, error=error, **kw)
            self.records[task["task_id"]] = rec
            done = len(self.records)
        if self.on_record is not None:
            self.on_record(rec)
        if self.on_progress is not None:
            self.on_progress(done, len(self.run.get("tasks") or []))

    def _settle_all(self, tasks, status, error):
        for t in tasks:
            self._settle(t, status, error)


def own_masks(method, raw, own):
    """What the patch owns, cropped to it (plan 7.11.3).

    Expansion: the cells decide and the nuclei (same ids) follow through the
    same LUT. Mesmer nuclear-guided: two independent outputs, each decides
    for itself, `paired` False. One output: it decides alone.
    Returns ({cell, nucleus}, paired or None).
    """
    cell, nuc = raw.get("cell"), raw.get("nucleus")
    out = {"cell": None, "nucleus": None}
    paired = None
    if cell is not None and nuc is not None and method in seg_engines.EXPANSION_METHODS:
        c, n, _ = label_ownership.apply_ownership(cell, own, shared=nuc)
        out["cell"], out["nucleus"] = c, n
        paired = True
    else:
        for k, arr in (("cell", cell), ("nucleus", nuc)):
            if arr is not None:
                out[k], _, _ = label_ownership.apply_ownership(arr, own)
        if cell is not None and nuc is not None:
            paired = False
    return ({k: (None if v is None else label_ownership.crop(v, own)) for k, v in out.items()},
            paired)


def summary_line(records, total):
    n = {preseg_run.OK: 0, preseg_run.FAILED: 0, preseg_run.CANCELLED: 0}
    for r in records.values():
        n[r["status"]] = n.get(r["status"], 0) + 1
    return (f"{len(records)}/{total} done · {n[preseg_run.OK]} ok · "
            f"{n[preseg_run.FAILED]} failed · {n[preseg_run.CANCELLED]} cancelled")
