"""v16_5 S-Raw: the Rust-drawn Step1 viewer's lifecycle on the REAL mount.

    sraw_lifecycle_probe.py SCENARIO --slide PATH   -> one JSON line

One scenario per process (one GL widget per process):
  pause_resume    paused: no read, the camera may move; resumed: the
                  picture catches up and is complete again
  fusion_stays_rust  switching to Fusion and back keeps the Rust picture
                  (R2), stable both ways; the Python scheduler never starts
  source_change   a rebuilt viewer (whole-slide region) is Rust again
  partial_roi_stays_rust  a region inside the slide is drawn by a rebuilt
                  Rust viewer (R2)
  capacity_fallback  (with --project: corrected channels) a picture that
                  needs more blocks of one format than one pass binds hands
                  over to the Python GPU path, which then starts its
                  scheduler; the Rust readers end (R2 plan §2.3)
  format_switch   (with --project; the caller sets a small GPU budget and
                  three samplers) raw channels first, then corrected ones
                  only: the raw blocks are given back and the samplers move
                  to the float family; the picture stays Rust, no core
                  error (codex R2 review: reclaim, split change)
  close           closing ends the Rust reader threads and leaves no layer
  repeated_source_change  three changes to another region (new bbox)
                  through `sync_source()`: each replaces the stack; no retired
                  binding stays alive, the mount owns one drawing binding,
                  each retired core held tiles before and none after its
                  close, every picture is stable (viewer release plan §3)
  repeated_source_change_python  (the caller sets BLOCK01_RUST_TEST_FAULT=init:
                  the Python GPU path draws) the same: no retired binding
  repeated_rebuild  (a Step3-kind viewer drawing masks) three backend
                  rebuilds on the same stack (same bbox, `source_changed()`):
                  as above, and each retired core held label copies before
                  and none after
  repeated_rebuild_python  (fault init) the same on the Python GPU path:
                  retired bindings do not pile up (at most the one an idle
                  scheduler thread last served stays until its next read)
(R2 migration, user-authorised 2026-10-12: fusion_fallback and partial_roi
asserted the hand-over the thin slice needed.)
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def readers():
    return sum(1 for t in threading.enumerate() if t.name.startswith("fvc-read"))


def os_readers():
    """Rust threads are not Python threads: count them in /proc."""
    count = 0
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/comm") as fh:
                if fh.read().startswith("fvc-read"):
                    count += 1
        except OSError:
            pass
    return count


def wait_for(rig, predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        rig.pump(0.05)
    return predicate()


def s_pause_resume(rig, out):
    from sraw_rig import stable
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    _shot, out["stable_before"] = stable(rig)
    rig.mount.pause_requests()
    reads0 = rig.mount.gpu_layer.core.stats()["reads"]
    box.setRange(xRange=(w0 * 0.3, w0 * 0.3 + 500), yRange=(h0 * 0.3, h0 * 0.3 + 500), padding=0)
    rig.pump(1.0)
    out["reads_while_paused"] = rig.mount.gpu_layer.core.stats()["reads"] - reads0
    out["hidden_while_paused"] = rig.mount.gpu_layer.hidden
    rig.mount.resume_requests()
    _shot, out["stable_after"] = stable(rig)
    report = rig.mount.gpu_layer.last_report
    out["complete_after"] = report.get("shown") == report.get("wanted") and report.get("wanted", 0) > 0
    out["ok"] = (out["stable_before"] and out["reads_while_paused"] == 0 and out["hidden_while_paused"]
                 and out["stable_after"] and out["complete_after"])


def s_fusion_stays_rust(rig, out):
    from sraw_rig import stable
    from block01.ui.step1_viewer_mount import LEGACY_FUSION, LEGACY_OVERLAY
    _shot, out["stable_overlay"] = stable(rig)
    scheduler = rig.mount.host.stack.scheduler
    first_core = rig.mount.gpu_layer.core
    rig.mount.set_mode(LEGACY_FUSION)
    rig.pump(0.5)
    out["binding_fusion"] = type(rig.mount.gpu_binding).__name__
    _shot, out["stable_fusion"] = stable(rig)
    rig.mount.set_mode(LEGACY_OVERLAY)
    rig.pump(0.5)
    out["binding_back"] = type(rig.mount.gpu_binding).__name__
    _shot, out["stable_back"] = stable(rig)
    out["same_core"] = rig.mount.gpu_layer is not None and rig.mount.gpu_layer.core is first_core
    out["scheduler_started"] = scheduler.started
    out["ok"] = (out["stable_overlay"] and out["binding_fusion"] == "Step1CoreBinding"
                 and out["stable_fusion"] and out["binding_back"] == "Step1CoreBinding"
                 and out["stable_back"] and not out["scheduler_started"])


def s_capacity_fallback(rig, out):
    """BLOCK01_RUST_TEST_BLOCK_UNITS=2 (set by the caller): one integer and
    one float block; the corrected channels' tiles at level 0 need more."""
    from sraw_rig import stable
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    scheduler = rig.mount.host.stack.scheduler
    out["scheduler_started_before"] = scheduler.started
    box.setRange(xRange=(w0 * 0.2, w0 * 0.2 + 1400), yRange=(h0 * 0.2, h0 * 0.2 + 1400), padding=0)
    ok = wait_for(rig, lambda: type(rig.mount.gpu_binding).__name__ == "Step1GpuBinding", 30.0)
    out["handed_over"] = ok
    out["rust_refused"] = rig.mount._rust_refused
    _shot, out["stable_python"] = stable(rig)
    out["scheduler_started_after"] = scheduler.started
    out["readers_after"] = (wait_for(rig, lambda: os_readers() == 0, 5.0), os_readers())
    out["ok"] = (ok and not out["scheduler_started_before"] and "one pass" in str(out["rust_refused"])
                 and out["stable_python"] and out["scheduler_started_after"] and out["readers_after"][0])


def s_format_switch(rig, out):
    from sraw_rig import stable
    from block01.ui.step1_draft_spec import STEP1_SCOPE
    box = rig.mount.host.stack.view.view_box
    h0, w0 = rig.shape
    errors = []
    layer = rig.mount.gpu_layer
    paint = layer.paintGL

    def watched():
        paint()
        if (layer.last_report or {}).get("error"):
            errors.append(str(layer.last_report["error"]))
    layer.paintGL = watched

    def core():
        return dict(rig.mount.gpu_binding.stats().get("core") or {})

    def show(channels):
        with rig.state.using_scope(STEP1_SCOPE):
            for c in rig.names:
                rig.state.set_display_visible(c, c in channels)
    raw = [c for c in rig.names if rig.decisions.get(c) == "original"]
    corrected = [c for c in rig.names if rig.decisions.get(c) not in ("original", None) and c != "d"]
    show(raw)
    for k in range(3):                              # level 0 in a few places: the raw blocks fill
        box.setRange(xRange=(w0 * (0.1 + 0.25 * k), w0 * (0.1 + 0.25 * k) + 900),
                     yRange=(h0 * 0.3, h0 * 0.3 + 900), padding=0)
        _shot, out[f"stable_raw_{k}"] = stable(rig)
    before = core()
    out["before"] = {k: before.get(k) for k in ("sampler_split", "gpu_live_blocks")}
    show(corrected)
    _shot, out["stable_corrected"] = stable(rig, timeout=60.0)
    after = core()
    out["after"] = {k: after.get(k) for k in ("sampler_split", "gpu_live_blocks")}
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["errors"] = errors[:3]
    split_b, split_a = tuple(before.get("sampler_split") or ()), tuple(after.get("sampler_split") or ())
    out["ok"] = (all(out[f"stable_raw_{k}"] for k in range(3)) and out["stable_corrected"]
                 and out["binding"] == "Step1CoreBinding" and not errors
                 and split_b and split_a and split_b[0] >= 2 and split_a[1] > split_b[1]
                 and (after.get("gpu_live_blocks") or {}).get("u8", 0) < (before.get("gpu_live_blocks") or {}).get("u8", 0))


def s_source_change(rig, out):
    from sraw_rig import stable
    _shot, out["stable_before"] = stable(rig)
    first_core = rig.mount.gpu_layer.core
    h0, w0 = rig.shape
    rig.window._active_roi = {"name": "ROI_2", "bbox_fullres": [0, h0, 0, w0]}
    rig.mount.source_changed("roi")
    rig.pump(0.5)
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["new_core"] = rig.mount.gpu_layer is not None and rig.mount.gpu_layer.core is not first_core
    _shot, out["stable_after"] = stable(rig)
    out["readers"] = os_readers()
    out["ok"] = (out["stable_before"] and out["binding"] == "Step1CoreBinding" and out["new_core"]
                 and out["stable_after"] and out["readers"] <= 6)


def s_partial_roi_stays_rust(rig, out):
    """A region that is not the whole slide is drawn by a rebuilt Rust
    viewer (R2)."""
    from sraw_rig import stable
    h0, w0 = rig.shape
    first_core = rig.mount.gpu_layer.core
    rig.window._active_roi = {"name": "ROI_3", "bbox_fullres": [100, h0 - 100, 80, w0 - 80]}
    rig.mount.source_changed("roi")
    rig.pump(0.5)
    out["binding"] = type(rig.mount.gpu_binding).__name__
    out["new_core"] = rig.mount.gpu_layer is not None and rig.mount.gpu_layer.core is not first_core
    _shot, out["stable"] = stable(rig)
    out["ok"] = out["binding"] == "Step1CoreBinding" and out["new_core"] and out["stable"]


def s_close(rig, out):
    from sraw_rig import stable
    _shot, out["stable"] = stable(rig)
    out["readers_open"] = os_readers()
    rig.mount.close()
    out["readers_closed"] = (wait_for(rig, lambda: os_readers() == 0, 5.0), os_readers())
    out["layer_after"] = rig.mount.gpu_layer
    out["ok"] = (out["stable"] and out["readers_open"] == 6 and out["readers_closed"][0]
                 and out["layer_after"] is None)


def _repeated_source_change(rig, out, expect, masks, new_source):
    """Three rebuilds; the retired bindings (weakrefs) and cores (kept here
    on purpose) are looked at once events have run.

    `new_source`: each change is another region (a new bbox, so a new
    source identity) through the product's gate, `sync_source()`: the
    stack, its scheduler and caches are replaced. Otherwise the region keeps
    its bbox and `source_changed()` rebuilds only the drawing backend on
    the same stack."""
    import gc
    import tempfile
    import weakref
    from sraw_rig import stable
    work = tempfile.mkdtemp(prefix="release_probe_")
    h0, w0 = rig.shape
    if masks:
        from r3b_label_probe import _mask, _put, _source
        path = _mask(work, "res", (h0, w0))
        source = _source(path, (h0, w0))
    bindings, cores, before, stable_after, replaced = [], [], [], [], []
    try:
        for i in range(4):
            if masks:
                _put(rig.mount, source)
            _shot, ok = stable(rig)
            stable_after.append(ok)
            binding = rig.mount.gpu_binding
            if type(binding).__name__ != expect:
                raise RuntimeError(f"drawn by {type(binding).__name__}, not {expect}")
            if i == 3:
                break
            bindings.append(weakref.ref(binding))
            old_stack = rig.mount.host.stack       # held across this change only
            if expect == "Step1CoreBinding":
                core = rig.mount.gpu_layer.core
                if masks:
                    # drawn, and the zoomed-out pyramid built: the next viewer
                    # is given that pyramid, as the product's owner does
                    def built():
                        cell = rig.mount.label_binding.sources().get("cell")
                        return cell is not None and cell.pyramid is not None
                    wait_for(rig, lambda: core.stats()["label_copy_bytes"] > 0 and built(), 20.0)
                    source = {"cell": rig.mount.label_binding.sources()["cell"], "nucleus": None}
                stats = core.stats()
                before.append((stats["held_tiles"], stats["label_copy_bytes"]))
                cores.append(core)
            del binding
            if new_source:
                m = 16 * (i + 1)
                rig.window._active_roi = {"name": f"ROI_r{i}", "bbox_fullres": [m, h0 - m, m, w0 - m]}
                if not rig.mount.sync_source("roi"):
                    raise RuntimeError("sync_source() saw no new source")
            else:
                rig.window._active_roi = {"name": f"ROI_r{i}", "bbox_fullres": [0, h0, 0, w0]}
                rig.mount.source_changed("roi")
            replaced.append(rig.mount.host.stack is not old_stack)
            del old_stack
            rig.pump(0.5)
        rig.pump(0.5)                         # queued callbacks run first
        gc.collect()
        out["stable"] = stable_after
        out["retired_alive"] = sum(ref() is not None for ref in bindings)
        out["drawing_bindings"] = sum(type(c).__name__ == expect for c in rig.mount.children())
        out["before_close"] = before
        out["after_close"] = [(c.stats()["held_tiles"], c.stats()["label_copy_bytes"]) for c in cores]
        out["readers"] = os_readers()
        out["stacks_replaced"] = replaced
        # same stack, Python path: its scheduler stays, and an idle reader
        # thread keeps the last entry it served in a loop local
        # (`_raw_worker`'s `entry`), so the binding whose reads were served
        # last may stay until that thread's next read -- never one per
        # change (`viewer/scheduler.py` is out of this plan's scope). A new
        # source shuts that scheduler down: none stays.
        allowed = 1 if (expect == "Step1GpuBinding" and not new_source) else 0
        ok = (all(stable_after) and out["retired_alive"] <= allowed and out["drawing_bindings"] == 1
              and replaced == [new_source] * 3)
        if expect == "Step1CoreBinding":
            ok = (ok and out["readers"] <= 6 and len(before) == 3
                  and all(held > 0 and (copies > 0 or not masks) for held, copies in before)
                  and all(after == (0, 0) for after in out["after_close"]))
        out["ok"] = ok
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def s_repeated_source_change(rig, out):
    _repeated_source_change(rig, out, "Step1CoreBinding", masks=False, new_source=True)


def s_repeated_source_change_python(rig, out):
    _repeated_source_change(rig, out, "Step1GpuBinding", masks=False, new_source=True)


def s_repeated_rebuild(rig, out):
    _repeated_source_change(rig, out, "Step1CoreBinding", masks=True, new_source=False)


def s_repeated_rebuild_python(rig, out):
    _repeated_source_change(rig, out, "Step1GpuBinding", masks=False, new_source=False)


LABEL_SCENARIOS = {"repeated_rebuild"}

SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--slide", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--gpu-bytes", type=int, default=0)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario}
    try:
        if args.project:
            project = json.load(open(args.project))
            rig = open_rig("rust", project["slide"], channels=[c for c in project["names"] if c != "d"],
                           roi=project["roi"],
                           decisions=project["decisions"], zarr=project["zarr"], size=1500,
                           gpu_bytes=args.gpu_bytes or None)
            rig.decisions = project["decisions"]
        else:
            rig = open_rig("rust", args.slide, labels=args.scenario in LABEL_SCENARIOS)
        out["binding_at_start"] = type(rig.mount.gpu_binding).__name__
        SCENARIOS[args.scenario](rig, out)
        if args.scenario != "close":
            rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
