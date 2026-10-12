"""v16_5 R3: Step3's masks on the REAL mount, Rust backend (plan §2.5-2.6).

    r3_label_mount_probe.py SCENARIO --slide PATH --mask-dir DIR -> one JSON line

  lifecycle  the label binding reads a cell mask and the Rust layer shows
             it; pause / resume reach the binding; a source change drops
             the binding (a new one starts without masks); close ends the
             label thread (as the program's exit waits for it)
  fallback   (the caller sets BLOCK01_RUST_TEST_FAULT=paint:N) the Rust
             layer fails a paint; the Python GPU path takes over and shows
             the masks the mount carried over -- with the zoomed-out
             pyramid the Rust-time binding may have built meanwhile
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _wait(rig, cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        layer = rig.mount.gpu_layer
        if layer is not None:
            layer.update()
        rig.pump(0.05)
        if cond():
            return True
    return cond()


def _source(mask_dir, shape):
    """A cell mask over the whole slide, no pyramid yet (the binding builds it)."""
    import numpy as np
    import zarr
    from block01.core import label_pyramid as lp
    from block01.core import step3_masks as sm
    h0, w0 = shape
    ids = np.zeros((h0, w0), np.uint32)
    ids[100:400, 100:500] = 2 ** 24 + 5
    ids[600:900, 300:380] = 7
    path = os.path.realpath(os.path.join(mask_dir, "global_mask_ROI_1.zarr"))
    z = zarr.open(path, mode="w", shape=ids.shape, chunks=(256, 256), dtype="<u4")
    z[:] = ids
    return {"cell": sm.MaskSource(kind="cell", mask_path=path, bbox=(0, h0, 0, w0),
                                  pyramid_path=lp.pyramid_path_for(path), pyramid_kind="cell",
                                  pyramid=None, pyramid_reason="no label pyramid"),
            "nucleus": None}


def _shown(rig):
    layer = rig.mount.gpu_layer
    return layer is not None and bool(layer.label_stats().get("shown"))


def s_lifecycle(rig, out, mask_dir):
    from block01.utils import closing_threads
    mount = rig.mount
    binding = mount.label_binding
    out["binding_made"] = binding is not None
    mount.set_mask_sources(_source(mask_dir, rig.shape))
    out["shown"] = _wait(rig, lambda: _shown(rig))
    mount.pause_requests()
    out["paused"] = binding.paused
    mount.resume_requests()
    out["resumed"] = not binding.paused
    mount.source_changed("test")
    rig.pump(0.5)
    out["new_binding"] = mount.label_binding is not binding and mount.label_binding is not None
    closing_threads.wait_all(5.0)
    out["old_thread_ended"] = binding.stats()["thread_alive"] is False
    last = mount.label_binding
    mount.close()
    closing_threads.wait_all(5.0)
    out["last_thread_ended"] = last.stats()["thread_alive"] is False
    out["ok"] = all(out[k] for k in ("binding_made", "shown", "paused", "resumed", "new_binding",
                                     "old_thread_ended", "last_thread_ended"))


def s_fallback(rig, out, mask_dir):
    mount = rig.mount
    sources = _source(mask_dir, rig.shape)
    mount.set_mask_sources(sources)
    out["handed_over"] = _wait(rig, lambda: type(mount.gpu_binding).__name__ == "Step1GpuBinding", 60.0)
    out["rust_refused"] = mount._rust_refused
    # nobody sets the masks again: the mount carries them over (Step3's page
    # only sets them when its selection changes)
    out["shown_python"] = _wait(rig, lambda: _shown(rig))
    out["python_layer"] = type(mount.gpu_layer).__name__
    out["ok"] = (out["handed_over"] and "paint failed" in str(out["rust_refused"])
                 and out["shown_python"] and out["python_layer"] == "Step1GpuLayer")
    mount.close()


SCENARIOS = {n[2:]: f for n, f in globals().items() if n.startswith("s_")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--slide", required=True)
    ap.add_argument("--mask-dir", required=True)
    args = ap.parse_args()
    from sraw_rig import open_rig
    out = {"scenario": args.scenario}
    try:
        rig = open_rig("rust", args.slide, labels=True)
        out["binding_at_start"] = type(rig.mount.gpu_binding).__name__
        if out["binding_at_start"] != "Step1CoreBinding":
            raise RuntimeError(f"not the Rust viewer: {out['binding_at_start']} {rig.mount._rust_refused}")
        SCENARIOS[args.scenario](rig, out, args.mask_dir)
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
