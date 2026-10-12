"""v16_5 R3 plan §2.6: the two places a segmentation starts give a hidden
Rust-drawn viewer's GPU blocks back (`gpu_memory.release_hidden`).

    r3_release_entry_probe.py ENTRY --slide PATH --work DIR -> one JSON line

  step2   Step2's real `_run()` (its segmentation worker replaced by one
          that does nothing, as `test_v16_a6_async` does)
  preseg  the pre-segmentation job's real `start()` (its run body replaced
          by one that returns at once)
The whole-slide viewer is Rust, has drawn, and is hidden (paused) first.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))


def _step2_run(work):
    import numpy as np
    import zarr
    from PyQt5 import QtWidgets
    from block01.ui import step2_page as s2
    zp = os.path.join(work, "fused_Full WSI.zarr")
    z = zarr.open(zp, mode="w", shape=(64, 64, 2), chunks=(64, 64, 2), dtype=np.uint16)
    z[...] = 7
    from test_v16_a6_async import _FakeStep2Run    # the worker that does nothing
    s2.SegmentMergeWorker = _FakeStep2Run
    s2.mark_roi_step = lambda *a, **k: None
    s2.QMessageBox.exec_ = lambda self: 0
    s2.QMessageBox.critical = lambda *a, **k: None
    p = s2.Step2Page()
    p._promote_step0_remap = lambda cfg: None
    p._zarr_edit.setText(zp)
    p._load_zarr_info()
    p._set_param_source("manual")
    a_dir = os.path.join(work, "proj", "rois", "roi_A")
    p.set_roi_context(roi_id="roi_A", roi_dir=a_dir, step2_dir=os.path.join(a_dir, "step2"))
    p._run()
    QtWidgets.QApplication.processEvents()
    return p


def _preseg_start(work):
    from block01.ui.step1_presegmentation.run_job import PresegRunJob
    job = PresegRunJob(work, {"run_id": "probe"}, loader_factory=lambda: None)
    job._main = lambda: None
    job.start()
    job._thread.join(5)
    return job


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entry", choices=("step2", "preseg"))
    ap.add_argument("--slide", required=True)
    ap.add_argument("--work", required=True)
    args = ap.parse_args()
    out = {"entry": args.entry}
    try:
        from sraw_rig import open_rig, stable
        rig = open_rig("rust", args.slide)
        out["binding"] = type(rig.mount.gpu_binding).__name__
        layer = rig.mount.gpu_layer
        _shot, out["stable"] = stable(rig)
        rig.mount.pause_requests()
        before = layer.gpu_bytes()
        from block01.ui import gpu_memory
        seen = []
        real = gpu_memory.release_hidden
        gpu_memory.release_hidden = lambda reason: seen.append(reason) or real(reason)
        keep = (_step2_run if args.entry == "step2" else _preseg_start)(args.work)
        out["reasons"] = seen
        out["bytes"] = (before, layer.gpu_bytes())
        out["ok"] = (out["binding"] == "Step1CoreBinding" and out["stable"] and seen == ["segmentation"]
                     and out["bytes"][1] < out["bytes"][0])
        del keep
        rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
