"""G3.2b.4E: the first enable of a corrected channel, with and without the plane.

One product, two states: the sidecar in place, and the sidecar moved aside so
the same product falls back to the runtime reduction. Everything else --
window, mount, GPU layer, binding, scheduler, provider -- is the production
stack, entered through the product's own gesture (`set_display_visible`).

Recorded per run: time to a complete, sharp first appearance; HOW MANY
LEVEL-0 PIXELS of the corrected product were read (the whole-region scan is
the thing being removed); provider reads; texture uploads; GPU submits;
natural paints; the GUI's longest unresponsive interval; and, after the
channel is up, a pan and a zoom-out with the coarse plane still resident.

Synthetic /tmp project only. This is a bench, not a real-machine acceptance.

Usage: python scripts/diagnose_step1_coarse_plane_endtoend.py OUT.json [rounds]
"""

import importlib.util
import json
import pathlib
import shutil
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
TICK_TIMEOUT = 90.0


def count_level0_reads(table, channel):
    """Wrap the product array so every level-0 pixel read is counted."""
    region = table.region(channel)
    if region is None:
        return None
    array = region.array
    state = {"reads": 0, "pixels": 0}

    class _Counted:
        attrs = getattr(array, "attrs", {})
        shape = array.shape
        dtype = array.dtype

        def __getitem__(self, item):
            out = array[item]
            state["reads"] += 1
            state["pixels"] += int(np.asarray(out).size)
            return out

    region.array = _Counted()
    return state


def one_run(with_plane, round_index, ledger):
    order = [CHANNEL, "CH_B"]
    built = rig.build_rig(order, ledger=ledger)
    if built.mount.backend != BACKEND_GPU:
        built.mount.close()
        return {"error": built.mount.gpu_status().get("reason")}
    heart = rig4.Heartbeat()
    paints = rig4.PaintCounter(built.mount.gpu_layer)
    binding = built.mount.gpu_binding
    layer = built.mount.gpu_layer

    table = built.table
    plane = table.coarse_plane(CHANNEL, 64)
    counted = count_level0_reads(table, CHANNEL)

    uploads0 = layer.cache_stats().get("uploads", 0)
    submits0 = len(binding.descriptor_history)
    paints0 = len(paints.stamps)
    ledger.reset_marks()
    heart.reset()
    t0 = time.perf_counter()
    with built.state.using_scope(STEP1_SCOPE):
        built.state.set_display_visible(CHANNEL, True)
    tick_ms = (time.perf_counter() - t0) * 1000.0

    complete_at = None
    deadline = time.perf_counter() + TICK_TIMEOUT
    while time.perf_counter() < deadline:
        rig.APP.processEvents()
        stats = binding.stats()
        if (CHANNEL in stats["coarse_channels"]
                and binding._viewport_fine_ready(CHANNEL)):
            complete_at = time.perf_counter()
            break
        time.sleep(0.002)
    complete_ms = None if complete_at is None else (complete_at - t0) * 1000.0

    rig4.settle(0.3)
    paint_after = None
    for stamp in paints.stamps[paints0:]:
        if complete_at is not None and stamp >= complete_at:
            paint_after = (stamp - t0) * 1000.0
            break

    requests, reads, _cache = ledger.since()
    corrected_reads = [r for r in reads if r["source"] == "corrected"]

    # ── and then move the camera: a pan and a zoom-out ──
    after = {}
    try:
        built.mount.show_patch([1000, 2088, 800, 2112])
        rig4.settle(0.4)
        after["after_pan_coarse_channels"] = list(
            binding.stats()["coarse_channels"])
        camera = built.mount.current_camera()
        if camera:
            cx, cy, scale = camera
            built.mount.apply_camera(cx, cy, scale / 8.0)
            rig4.settle(0.4)
        after["after_zoom_out_coarse_channels"] = list(
            binding.stats()["coarse_channels"])
        # `descriptor_history` holds `(descriptor, display, viewport, stats)`
        # tuples, not bare descriptors -- an earlier version of this bench
        # assumed otherwise and lost the whole zoom-out check to a silently
        # caught AttributeError.
        entry = binding.descriptor_history[-1]
        descriptor = entry[0] if isinstance(entry, tuple) else entry
        channels = {c.channel: c for c in descriptor.channels}
        source = channels.get(CHANNEL)
        after["zoom_out_has_coarse_plane"] = bool(source and source.coarse)
        after["zoom_out_fine_planes"] = len(source.fine) if source else 0
    except Exception as exc:                                # noqa: BLE001
        after["error"] = repr(exc)

    result = {
        "with_plane": with_plane,
        "round": round_index,
        "plane_resolved": plane is not None,
        "tick_returned_ms": round(tick_ms, 2),
        "complete_ms": None if complete_ms is None else round(complete_ms, 1),
        "first_natural_paint_after_complete_ms": (
            None if paint_after is None else round(paint_after, 1)),
        "gui_worst_gap_ms": heart.worst_ms(),
        "natural_paints": len(paints.stamps) - paints0,
        "level0_pixels_read": counted["pixels"] if counted else None,
        "level0_reads": counted["reads"] if counted else None,
        "provider_reads": len(reads),
        "provider_reads_corrected": len(corrected_reads),
        "requests": len(requests),
        "gpu_uploads": layer.cache_stats().get("uploads", 0) - uploads0,
        "gpu_submits": len(binding.descriptor_history) - submits0,
        **after,
    }
    heart.stop()
    built.mount.close()
    return result


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else "/tmp/g324e_endtoend.json")
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    rig.select_dataset("tmp_plane")
    ledger = rig4.Ledger()
    report = {"block": "G3.2b.4E end-to-end (synthetic /tmp project)",
              "project": str(PROJECT), "channel": CHANNEL,
              "os_page_cache_dropped": False, "runs": []}

    for index in range(rounds):
        for with_plane in (True, False):
            if with_plane and PARKED.exists() and not SIDECAR.exists():
                PARKED.rename(SIDECAR)
            if not with_plane and SIDECAR.exists():
                SIDECAR.rename(PARKED)
            result = one_run(with_plane, index, ledger)
            report["runs"].append(result)
            print(f"round {index} with_plane={with_plane}: "
                  f"plane={result.get('plane_resolved')} "
                  f"complete={result.get('complete_ms')} ms  "
                  f"L0_pixels_read={result.get('level0_pixels_read')}  "
                  f"uploads={result.get('gpu_uploads')} "
                  f"paints={result.get('natural_paints')} "
                  f"gui_gap={result.get('gui_worst_gap_ms')}", flush=True)
    if PARKED.exists() and not SIDECAR.exists():
        PARKED.rename(SIDECAR)

    def median(values):
        values = sorted(v for v in values if isinstance(v, (int, float)))
        return values[len(values) // 2] if values else None

    for state in (True, False):
        runs = [r for r in report["runs"] if r.get("with_plane") is state]
        report[f"median_complete_ms_with_plane_{state}"] = median(
            [r.get("complete_ms") for r in runs])
        report[f"median_level0_pixels_read_with_plane_{state}"] = median(
            [r.get("level0_pixels_read") for r in runs])
    out_path.write_text(json.dumps(report, indent=1, default=str))
    print("wrote", out_path)
    for key, value in report.items():
        if key.startswith("median_"):
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
