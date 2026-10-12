"""v16_5 R3b: Step3's masks planned and read by the core, on the REAL mount.

    r3b_label_probe.py SCENARIO --slide PATH --work DIR  -> one JSON line

A Step3-kind viewer (Rust layer, `CoreLabelBinding`) on `--slide`; masks are
written into `--work`. One scenario per process (R3b plan §4):
  follow         reads follow the camera; with slow reads (the caller sets
                 BLOCK01_RUST_TEST_LABEL_DELAY_MS) a moved camera drops what
                 it no longer wants; at rest everything held is wanted
  pyramid        a mask without a pyramid: nothing is read while it is built,
                 then the zoomed-out level shows masks
  level0_only    a foreign pyramid in the way ("kept"): level 0 reads and
                 shows, zoomed out nothing is asked and the status says why
  pause          paused, the camera moves and nothing is asked; resumed, it
                 catches up; a pause during a build cancels it, the resume
                 builds again
  switch_build   new sources while a build runs: the old build's end is not
                 news, the new masks show
  close_reading  (slow reads) closing while a read waits: the exit's wait is
                 at most one second, the reader ends later
  close_build    closing during a build: the exit's wait is at most one second
  errors         (the caller sets BLOCK01_RUST_TEST_FAULT=label:2) the error
                 is in the status at once; new sources clear it
  late_error     (label:9 and slow reads) an old source's error arriving after
                 new sources is dropped
  hide_show      a hidden mask, the camera moved, shown again: its tiles are
                 read; a colour change reads nothing
  l1             a pyramid only in memory, then a zstd mask: Python reads them
                 (L1), the status says why; a readable mask goes back to the core
  ratio          a new device pixel ratio gives the core a new outline radius
  gl_state       R0's full glGet snapshot around every paint while the core
                 draws its own masks across camera moves: no state differs
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _mask(work, name, shape, compressor=None, base=0):
    import numpy as np
    import zarr
    ids = np.zeros(shape, np.uint32)
    ids[100:400, 100:500] = 2 ** 24 + 5 + base
    ids[600:900, 300:380] = 7 + base
    ids[1200:1500, 900:1300] = 11 + base
    path = os.path.realpath(os.path.join(work, name, "global_mask_ROI_1.zarr"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kw = {} if compressor is None else {"compressor": compressor}
    z = zarr.open(path, mode="w", shape=ids.shape, chunks=(256, 256), dtype="<u4", **kw)
    z[:] = ids
    return path


def _source(path, shape, pyramid=None, reason="no label pyramid"):
    from block01.core import label_pyramid as lp
    from block01.core import step3_masks as sm
    return {"cell": sm.MaskSource(kind="cell", mask_path=path, bbox=(0, shape[0], 0, shape[1]),
                                  pyramid_path=lp.pyramid_path_for(path), pyramid_kind="cell",
                                  pyramid=pyramid, pyramid_reason=None if pyramid else reason),
            "nucleus": None}


class Probe:
    def __init__(self, rig, work):
        self.rig, self.work = rig, work
        self.mount = rig.mount
        self.box = rig.mount.host.stack.view.view_box
        self.h0, self.w0 = rig.shape

    @property
    def binding(self):
        return self.mount.label_binding

    def stats(self):
        return dict(self.binding.stats())

    def core(self):
        return dict(self.mount.gpu_layer.core.core_label_stats())

    def wait(self, cond, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.mount.gpu_layer is not None:
                self.mount.gpu_layer.update()
            self.rig.pump(0.05)
            if cond():
                return True
        return cond()

    def shown(self):
        return bool(self.mount.gpu_layer.label_stats().get("shown"))

    def settled(self):
        s = self.stats()
        if not (s["queued"] == 0 and s["reading"] == 0 and s["pending_bytes"] == 0 and self.shown()
                and not s.get("building")):
            return False
        if getattr(self.binding, "l1_reason", None) is None and hasattr(self.binding, "_core_stats"):
            # the core read them: every tile it wants held and uploaded
            cs = self.binding._core_stats()
            if not set(map(tuple, cs.get("wanted_keys", []))) <= set(map(tuple, cs.get("held_keys", []))):
                return False
            if (self.mount.gpu_layer.last_report or {}).get("label_deferred", 0):
                return False
        return True

    def core_wants(self):
        """The core has planned tiles for the current view."""
        return bool(self.binding._core_stats().get("wanted_keys"))

    def view(self, x0, x1, y0, y1):
        self.box.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)

    def zoom_out(self):
        self.view(0, self.w0, 0, self.h0)

    def zoom_in(self):
        self.view(80, 600, 80, 600)


def s_follow(p, out):
    path = _mask(p.work, "a", (p.h0, p.w0))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    out["shown"] = p.wait(p.settled, 60)
    before = p.stats()
    for x in (700, 1100, 300, 900):                    # moves faster than the slow reads
        p.view(x, x + 400, x * 0.8, x * 0.8 + 400)
        p.rig.pump(0.12)
    out["settled_after"] = p.wait(p.settled, 60)
    after, core = p.stats(), p.core()
    out["stale"] = after["stale_dropped"] - before["stale_dropped"]
    held, wanted = {tuple(k) for k in core["held_keys"]}, {tuple(k) for k in core["wanted_keys"]}
    out["held_not_wanted"] = len(held - wanted)
    out["budget_ok"] = after["resident_bytes"] + after["pending_bytes"] <= after["budget_bytes"]
    out["ok"] = (out["shown"] and out["settled_after"] and out["stale"] > 0 and out["held_not_wanted"] == 0
                 and out["budget_ok"])


def s_pyramid(p, out):
    path = _mask(p.work, "b", (p.h0, p.w0))
    p.zoom_out()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    building = p.binding.mask_status()["cell"]["building"]
    out["building_at_first"] = building
    out["read_while_building"] = p.stats()["requested"] if building else 0
    out["built"] = p.wait(lambda: p.stats()["pyramids_built"] == 1, 60)
    out["shown_zoomed_out"] = p.wait(p.settled, 60) and (p.core()["target_level"] or 0) > 0
    out["ok"] = (out["read_while_building"] == 0 and out["built"] and out["shown_zoomed_out"])


def s_level0_only(p, out):
    from block01.core import label_pyramid as lp
    path = _mask(p.work, "c", (p.h0, p.w0))
    foreign = _mask(p.work, "c_other", (p.h0, p.w0))     # a pyramid of ANOTHER mask, in the way
    sm_src = _source(foreign, (p.h0, p.w0))["cell"]
    from block01.core import step3_masks as sm
    assert sm.ensure_pyramid(sm_src, p.rig.mount.host.stack.provider and
                             [p.mount.host.stack.provider.level_shape(l)
                              for l in range(p.mount.host.stack.provider.num_levels)]).status == "ready"
    shutil.copytree(lp.pyramid_path_for(foreign), lp.pyramid_path_for(path))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    out["level0_shown"] = p.wait(p.settled, 60)
    p.zoom_out()
    p.wait(lambda: (p.core()["target_level"] or 0) > 0, 20)
    p.rig.pump(1.0)
    status = p.binding.mask_status()["cell"]
    out["status"] = status
    out["coarse_wanted"] = len(p.core()["wanted_keys"])
    out["ok"] = (out["level0_shown"] and status["unavailable_here"] and "kept" in str(status["coarse_unavailable"])
                 and out["coarse_wanted"] == 0)


def s_pause(p, out):
    path = _mask(p.work, "d", (p.h0, p.w0))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    p.mount.pause_requests()                            # during the build
    out["cancelled_build"] = p.wait(lambda: not p.stats()["building"], 20)
    out["built_while_paused"] = p.stats()["pyramids_built"]
    requested = p.stats()["requested"]
    p.view(900, 1300, 900, 1300)
    p.rig.pump(0.5)
    out["asked_while_paused"] = p.stats()["requested"] - requested
    p.mount.resume_requests()
    out["built_after_resume"] = p.wait(lambda: p.stats()["pyramids_built"] == 1, 60)
    out["caught_up"] = p.wait(p.settled, 60)
    out["ok"] = (out["cancelled_build"] and out["built_while_paused"] == 0 and out["asked_while_paused"] == 0
                 and out["built_after_resume"] and out["caught_up"])


def s_rapid_pause(p, out):
    """Pause and resume at once, twice, while the pyramid builds: never two
    writers of it at a time; it ends built and readable, masks shown."""
    import threading
    from block01.core import label_pyramid as lp
    path = _mask(p.work, "n", (p.h0, p.w0))
    p.zoom_out()
    most = [0]

    def writers():
        n = sum(1 for t in threading.enumerate() if t.name == "step3-pyramid" and t.is_alive())
        most[0] = max(most[0], n)
        return n
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    for _ in range(2):
        p.mount.pause_requests()
        writers()
        p.mount.resume_requests()
        writers()
    out["built"] = p.wait(lambda: writers() is not None and p.stats()["pyramids_built"] >= 1 and p.settled(), 60)
    out["most_writers"] = most[0]
    out["readable"] = lp.read(lp.pyramid_path_for(path)) is not None
    out["ok"] = out["built"] and out["most_writers"] <= 1 and out["readable"]


def s_switch_build(p, out):
    first = _mask(p.work, "e1", (p.h0, p.w0))
    second = _mask(p.work, "e2", (p.h0, p.w0))
    p.zoom_out()
    p.mount.set_mask_sources(_source(first, (p.h0, p.w0)))
    p.mount.set_mask_sources(_source(second, (p.h0, p.w0)))   # while the first builds
    out["shown"] = p.wait(lambda: p.stats()["pyramids_built"] >= 1 and p.settled(), 60)
    src = p.binding.sources()["cell"]
    out["source_is_second"] = src is not None and src.mask_path == second
    out["ok"] = out["shown"] and out["source_is_second"]


def _close_and_wait(p, out):
    from block01.utils import closing_threads
    p.mount.close()
    started = time.monotonic()
    left = closing_threads.wait_all(1.0)
    out["exit_wait_s"] = round(time.monotonic() - started, 3)
    out["left_at_exit"] = left
    later = closing_threads.wait_all(10.0)
    out["left_later"] = later
    return out["exit_wait_s"] <= 1.1 and not later["python_left"] and later["rust_left"] == 0


def s_close_reading(p, out):
    path = _mask(p.work, "f", (p.h0, p.w0))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    out["reading"] = p.wait(lambda: p.stats()["reading"] > 0, 30)
    out["ok"] = out["reading"] and _close_and_wait(p, out)


def s_close_build(p, out):
    path = _mask(p.work, "g", (p.h0, p.w0))
    p.zoom_out()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    out["building"] = p.binding.mask_status()["cell"]["building"]
    out["ok"] = out["building"] and _close_and_wait(p, out)


def s_errors(p, out):
    path = _mask(p.work, "h", (p.h0, p.w0))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    out["error_shown"] = p.wait(lambda: bool(p.binding.mask_status()["error"]), 30)
    out["error"] = p.binding.mask_status()["error"]
    again = _mask(p.work, "h2", (p.h0, p.w0))
    p.mount.set_mask_sources(_source(again, (p.h0, p.w0)))
    out["cleared"] = p.binding.mask_status()["error"] is None
    out["shown_after"] = p.wait(p.settled, 60)
    out["ok"] = out["error_shown"] and "injected" in str(out["error"]) and out["cleared"] and out["shown_after"]


def s_late_error(p, out):
    path = _mask(p.work, "i", (p.h0, p.w0))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    p.wait(lambda: p.stats()["pyramids_built"] == 1, 60)
    out["reading"] = p.wait(lambda: p.stats()["reading"] > 0, 30)
    late = p.stats()["late_dropped"]
    p.mount.set_mask_sources(None)                      # the failing reads now belong to an old source
    p.wait(lambda: p.stats()["reading"] == 0, 30)
    p.rig.pump(1.0)
    out["late_dropped"] = p.stats()["late_dropped"] - late
    out["error"] = p.binding.mask_status()["error"]
    out["ok"] = out["reading"] and out["late_dropped"] > 0 and out["error"] is None


def s_hide_show(p, out):
    path = _mask(p.work, "j", (p.h0, p.w0))
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    p.wait(p.settled, 60)
    p.mount.set_mask_style("cell", visible=False)
    p.view(1000, 1400, 1300, 1700)
    p.rig.pump(0.5)
    hidden_req = p.stats()["requested"]
    p.mount.set_mask_style("cell", visible=True)
    out["read_on_show"] = p.wait(lambda: p.stats()["requested"] > hidden_req and p.settled(), 30)
    req = p.stats()["requested"]
    p.mount.set_mask_style("cell", color=(1.0, 0.0, 0.0))
    p.rig.pump(0.5)
    out["read_on_colour"] = p.stats()["requested"] - req
    out["ok"] = out["read_on_show"] and out["read_on_colour"] == 0


def s_l1(p, out):
    import zarr
    from numcodecs import Zstd
    from block01.core import step3_masks as sm
    shape = (p.h0, p.w0)
    path = _mask(p.work, "k", shape)
    built = sm.ensure_pyramid(_source(path, shape)["cell"],
                              [p.mount.host.stack.provider.level_shape(l)
                               for l in range(p.mount.host.stack.provider.num_levels)])
    memory = built.source.pyramid.__class__(group=zarr.group(), attrs=built.source.pyramid.attrs, where="memory")
    import dataclasses
    p.zoom_in()
    p.mount.set_mask_sources({"cell": dataclasses.replace(built.source, pyramid=memory), "nucleus": None})
    out["l1_memory"] = p.binding.l1_reason
    out["shown_l1"] = p.wait(p.shown, 30)
    zstd = _mask(p.work, "k2", shape, compressor=Zstd(level=1))
    p.mount.set_mask_sources(_source(zstd, shape))
    p.rig.pump(0.5)
    out["l1_format"] = p.binding.l1_reason
    good = _mask(p.work, "k3", shape, base=1000)       # other ids: no stale texture may show
    p.mount.set_mask_sources(_source(good, shape))
    out["back_to_core"] = p.binding.l1_reason is None
    out["shown_core"] = p.wait(lambda: p.core_wants() and p.settled(), 60)
    p.mount.gpu_layer.update()
    p.rig.pump(0.3)                                     # a paint with every tile uploaded
    import numpy as np
    seen = set(np.unique(p.mount.gpu_layer.readback_label_ids_for_test()).tolist()) - {0}
    out["ids_seen"] = sorted(seen)
    new_ids = {2 ** 24 + 5 + 1000, 7 + 1000, 11 + 1000}  # the old mask's are 2^24+5, 7, 11
    out["only_new_ids"] = bool(seen) and seen <= new_ids
    out["ok"] = ("memory" in str(out["l1_memory"]) and out["shown_l1"] and "zstd" in str(out["l1_format"])
                 and out["back_to_core"] and out["shown_core"] and out["only_new_ids"])


def s_ratio(p, out):
    path = _mask(p.work, "l", (p.h0, p.w0))
    p.mount.set_mask_style("cell", width=2.0)
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    p.wait(p.settled, 60)
    layer = p.mount.gpu_layer
    out["radius_1"] = p.core()["radius"][0]
    layer.devicePixelRatioF = lambda: 2.0               # a move to a denser screen
    layer.update()
    p.rig.pump(0.3)
    out["radius_2"] = p.core()["radius"][0]
    out["ok"] = out["radius_1"] == 2 and out["radius_2"] == 4


def s_gl_state(p, out):
    import r0_gl_probe as r0
    path = _mask(p.work, "m", (p.h0, p.w0))
    layer = p.mount.gpu_layer
    record = {"paints": 0, "diffs": []}
    paint = layer.paintGL

    def wrapped():
        before = r0.snapshot()
        paint()
        d = r0.diff(before, r0.snapshot())
        record["paints"] += 1
        if d:
            record["diffs"].append({k: [str(v[0]), str(v[1])] for k, v in d.items()})
    layer.paintGL = wrapped
    p.zoom_in()
    p.mount.set_mask_sources(_source(path, (p.h0, p.w0)))
    p.wait(p.settled, 60)
    for x in (300, 700, 0):
        p.view(x, x + 500, x, x + 500)
        p.wait(p.settled, 30)
    p.zoom_out()
    p.wait(p.settled, 30)
    out["paints"], out["diffs"] = record["paints"], record["diffs"][:2]
    out["ok"] = record["paints"] > 5 and not record["diffs"]


SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--slide", required=True)
    ap.add_argument("--work", required=True)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario}
    try:
        rig = open_rig("rust", args.slide, labels=True)
        p = Probe(rig, args.work)
        out["label_binding"] = type(p.binding).__name__
        if out["label_binding"] != "CoreLabelBinding":
            raise RuntimeError(f"not the core's labels: {out['label_binding']}")
        SCENARIOS[args.scenario](p, out)
        if args.scenario not in ("close_reading", "close_build"):
            rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
