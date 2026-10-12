"""v16_5 R3 plan §2.4: the Rust-drawn whole-slide viewer beside the
montage's Python GPU layer, in ONE process (each with its own GL context).

    r3_montage_probe.py --slide PATH -> one JSON line

The montage layer is built as the montage builds it
(`montage_gpu.build_layer`, over its own canvas) and shows a picture of
its own. Checked:
  * both draw; `gpu_memory` counts both (its total is their sum);
  * Smooth reaches both: each redraws, and back to the start each gives
    its first picture again;
  * hiding the whole-slide viewer and showing the montage, then back (as
    the tabs do), leaves the whole-slide picture identical;
  * `release_hidden` gives back the hidden whole-slide viewer's blocks and
    leaves the montage's alone;
  * no GL error on either side.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slide", required=True)
    args = ap.parse_args()
    out = {}
    try:
        import numpy as np
        import pyqtgraph as pg
        from sraw_rig import open_rig, stable
        rig = open_rig("rust", args.slide, smooth=0)
        from block01.ui import gpu_memory
        from block01.ui.step1_presegmentation import montage_gpu
        from test_step3_label_render import WORLD, _picture
        out["binding"] = type(rig.mount.gpu_binding).__name__
        h0, w0 = rig.shape
        box = rig.mount.host.stack.view.view_box
        box.setRange(xRange=(w0 * 0.4, w0 * 0.4 + 120), yRange=(h0 * 0.4, h0 * 0.4 + 120), padding=0)
        whole, out["stable_whole"] = stable(rig)
        rust = rig.mount.gpu_layer

        canvas = pg.GraphicsLayoutWidget()
        canvas.resize(300, 240)
        view = type("MontageView", (), {})()
        view.gv, view.vb = canvas, canvas.addViewBox()
        view.vb.setRange(xRange=WORLD[:2], yRange=WORLD[2:], padding=0)
        canvas.show()
        rig.pump(0.3)
        montage, why = montage_gpu.build_layer(view)
        out["montage_built"] = montage is not None, why

        def montage_shot():
            descriptor, display = _picture()
            montage.submit(descriptor, display, montage_gpu.viewport_snapshot(view, montage))
            rig.pump(0.2)
            return montage.readback_rgba_for_test()
        first_montage = montage_shot()
        out["montage_draws"] = bool(first_montage.size and (first_montage[..., 3] > 0).any())
        out["memory_sum"] = (gpu_memory.allocated_bytes(), rust.gpu_bytes() + montage.gpu_bytes())

        gpu_memory.set_smooth(True)
        smooth_whole, _ = stable(rig)
        smooth_montage = montage_shot()
        out["smooth_both"] = (rust.smooth and montage.smooth,
                              not np.array_equal(smooth_whole, whole))
        gpu_memory.set_smooth(False)
        back_whole, _ = stable(rig)
        out["smooth_back"] = (bool(np.array_equal(back_whole, whole)),
                              bool(np.array_equal(montage_shot(), first_montage)))
        del smooth_montage

        # the tabs: the viewer hidden while the montage is shown, then back
        rig.mount.pause_requests()
        rig.mount.host.hide()
        rig.pump(0.3)
        montage_bytes = montage.gpu_bytes()
        whole_bytes = rust.gpu_bytes()
        released = gpu_memory.release_hidden("probe")
        out["release"] = (released > 0, rust.gpu_bytes() < whole_bytes, montage.gpu_bytes() == montage_bytes)
        rig.mount.host.show()
        rig.mount.resume_requests()
        again, out["stable_again"] = stable(rig)
        out["same_whole_after_tabs"] = bool(np.array_equal(again, whole))
        out["failed"] = rust.failed
        out["ok"] = (out["binding"] == "Step1CoreBinding" and out["stable_whole"] and out["montage_built"][0]
                     and out["montage_draws"] and out["memory_sum"][0] == out["memory_sum"][1]
                     and all(out["smooth_both"]) and all(out["smooth_back"]) and all(out["release"])
                     and out["stable_again"] and out["same_whole_after_tabs"] and not out["failed"])
        montage.dispose()
        rig.mount.close()
    except Exception as exc:                                # noqa: BLE001 -- reported
        import traceback
        out.update(ok=False, error=repr(exc), trace=traceback.format_exc()[-2000:])
    print(json.dumps(out, default=str), flush=True)
    os._exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
