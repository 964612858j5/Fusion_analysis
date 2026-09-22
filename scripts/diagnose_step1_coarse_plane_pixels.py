"""G3.2b.4E: what is actually ON SCREEN, with and without the plane.

The end-to-end bench measured when the data was ready. It did NOT show a
frame: two of its three with-plane runs recorded no natural paint after
completion at all. This closes that gap, and only that:

  1. after the channel is complete, WAIT for a natural Qt paint (no forced
     grab) and say plainly whether one arrives and when;
  2. then, at the SAME camera in both states, take a framebuffer -- clearly
     LABELLED as a forced grab -- and compare the two images pixel by pixel;
  3. count blank pixels inside the viewport at that camera and after a
     zoom-out, so "no blank background" is an assertion about the picture
     rather than about the descriptor.

Synthetic /tmp project, real GPU stack. A bench, not a real-machine
acceptance: an offscreen host may legitimately deliver few or no paint
events, and that is reported as a limit of the bench, not as a property of
the product.

Usage: python scripts/diagnose_step1_coarse_plane_pixels.py OUT.json
"""

import importlib.util
import json
import pathlib
import sys
import time

import numpy as np

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _mod
    _spec.loader.exec_module(_mod)
sys.path.insert(0, str(_ROOT.parent))
sys.path.insert(0, str(_ROOT / "scripts"))

import diagnose_step1_first_cucim_enable as rig            # noqa: E402
import benchmark_step1_gpu_cold_landing as rig4            # noqa: E402
from block01.ui.step1_draft_spec import STEP1_SCOPE        # noqa: E402
from block01.ui.step1_viewer_mount import BACKEND_GPU      # noqa: E402
from block01.viewer import step1_source as sources         # noqa: E402

PROJECT = pathlib.Path("/tmp/g324e_save_bench/with0")
SIDECAR = PROJECT / sources.COARSE_SIDECAR_DIRNAME
PARKED = PROJECT / "_parked_coarse.zarr"
CHANNEL = "CH_A"
#: One camera, used in BOTH states, so the two pictures are comparable.
CAMERA = (3072.0, 4352.0, 0.08)          # (cx, cy, scale) in level-0 pixels
ZOOM_OUT_SCALE = 0.01


def to_array(image):
    """A QImage from `grabFramebuffer()` as HxWx4 uint8 (RGBA)."""
    image = image.convertToFormat(image.Format_RGBA8888)
    width, height = image.width(), image.height()
    pointer = image.constBits()
    pointer.setsize(image.byteCount())
    return np.frombuffer(pointer, np.uint8).reshape(
        (height, image.bytesPerLine() // 4, 4))[:, :width, :].copy()


def blank_pixels(frame):
    """Pixels with nothing drawn on them: fully transparent, or pure black."""
    alpha_zero = int(np.count_nonzero(frame[..., 3] == 0))
    black = int(np.count_nonzero(np.all(frame[..., :3] == 0, axis=-1)))
    return {"alpha_zero": alpha_zero, "pure_black": black,
            "total": int(frame.shape[0] * frame.shape[1])}


def one_state(with_plane, ledger):
    if with_plane and PARKED.exists() and not SIDECAR.exists():
        PARKED.rename(SIDECAR)
    if not with_plane and SIDECAR.exists():
        SIDECAR.rename(PARKED)

    built = rig.build_rig([CHANNEL, "CH_B"], ledger=ledger)
    if built.mount.backend != BACKEND_GPU:
        built.mount.close()
        return {"error": built.mount.gpu_status().get("reason")}, None, None
    paints = rig4.PaintCounter(built.mount.gpu_layer)
    binding = built.mount.gpu_binding
    plane = built.table.coarse_plane(CHANNEL, 64)

    paints0 = len(paints.stamps)
    t0 = time.perf_counter()
    with built.state.using_scope(STEP1_SCOPE):
        built.state.set_display_visible(CHANNEL, True)

    complete_at = None
    deadline = time.perf_counter() + 90.0
    while time.perf_counter() < deadline:
        rig.APP.processEvents()
        stats = binding.stats()
        if (CHANNEL in stats["coarse_channels"]
                and binding._viewport_fine_ready(CHANNEL)):
            complete_at = time.perf_counter()
            break
        time.sleep(0.002)

    # ── 1. a NATURAL paint after completion, waited for honestly ──
    natural_after = None
    natural_deadline = time.perf_counter() + 3.0
    while time.perf_counter() < natural_deadline:
        rig.APP.processEvents()
        for stamp in paints.stamps[paints0:]:
            if complete_at is not None and stamp >= complete_at:
                natural_after = (stamp - complete_at) * 1000.0
                break
        if natural_after is not None:
            break
        time.sleep(0.005)

    result = {
        "with_plane": with_plane,
        "plane_resolved": plane is not None,
        "complete_ms": None if complete_at is None
                       else round((complete_at - t0) * 1000.0, 1),
        "natural_paints_total": len(paints.stamps) - paints0,
        "natural_paint_after_complete_ms": (None if natural_after is None
                                            else round(natural_after, 1)),
        "natural_paint_waited_ms": 3000,
    }

    # ── 2. the same camera in both states, then a FORCED grab ──
    built.mount.apply_camera(*CAMERA)
    rig4.settle(0.6)
    frame = to_array(built.mount.gpu_layer.grabFramebuffer())   # FORCED
    result["forced_grab_at_camera"] = list(CAMERA)
    result["frame_shape"] = list(frame.shape)
    result["blank_at_camera"] = blank_pixels(frame)

    cx, cy, _scale = CAMERA
    built.mount.apply_camera(cx, cy, ZOOM_OUT_SCALE)
    rig4.settle(0.6)
    zoomed = to_array(built.mount.gpu_layer.grabFramebuffer())  # FORCED
    result["blank_after_zoom_out"] = blank_pixels(zoomed)

    built.mount.close()
    return result, frame, zoomed


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else "/tmp/g324e_pixels.json")
    rig.select_dataset("tmp_plane")
    ledger = rig4.Ledger()
    report = {"block": "G3.2b.4E pixel check (synthetic /tmp project)",
              "note": "every framebuffer below is a FORCED grab, taken after "
                      "the natural-paint observation above it",
              "states": []}

    frames = {}
    for with_plane in (True, False):
        result, frame, zoomed = one_state(with_plane, ledger)
        report["states"].append(result)
        frames[with_plane] = (frame, zoomed)
        print(f"with_plane={with_plane}: complete={result.get('complete_ms')} ms "
              f"natural_paint_after_complete={result.get('natural_paint_after_complete_ms')} "
              f"paints={result.get('natural_paints_total')} "
              f"blank={result.get('blank_at_camera')}", flush=True)

    if PARKED.exists() and not SIDECAR.exists():
        PARKED.rename(SIDECAR)

    a, az = frames[True]
    b, bz = frames[False]
    if a is not None and b is not None and a.shape == b.shape:
        diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
        report["pixel_comparison_same_camera"] = {
            "shape": list(a.shape),
            "rgb_max_abs_diff_lsb": int(diff[..., :3].max()),
            "alpha_identical": bool(np.array_equal(a[..., 3], b[..., 3])),
            "differing_pixels": int(np.count_nonzero(diff.any(axis=-1))),
            "identical": bool(np.array_equal(a, b)),
        }
        dz = np.abs(az.astype(np.int16) - bz.astype(np.int16))
        report["pixel_comparison_zoomed_out"] = {
            "rgb_max_abs_diff_lsb": int(dz[..., :3].max()),
            "alpha_identical": bool(np.array_equal(az[..., 3], bz[..., 3])),
            "differing_pixels": int(np.count_nonzero(dz.any(axis=-1))),
            "identical": bool(np.array_equal(az, bz)),
        }
    out_path.write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report.get("pixel_comparison_same_camera"), indent=1))
    print(json.dumps(report.get("pixel_comparison_zoomed_out"), indent=1))
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
