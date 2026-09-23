"""G3.2c: WHICH LAYER paints the pixel outside the ROI, on the real screen.

The offscreen rig says the framebuffer is clipped. The user's machine says
the picture is not. This runs the real application on the real X display
and, for one point the user would call "outside the rectangle", asks three
questions at the same instant:

  1. what is in the GPU layer's own final FBO there?
  2. what is in the GL widget's composited output there?
  3. what is on the SCREEN there (the window as the user sees it)?

plus the state that decides it: backend, the live source table's ROI, the
`roi_world_rect` and `roi_scissor` of the last submission, and every item
in the ViewBox with its visibility, opacity and z-order.

The user's project is mirrored read-only into /tmp (zarrs are symlinks),
so nothing is written to it. The window is opened and closed by this
script; no other window is touched.

Usage:
    DISPLAY=:1 python scripts/diagnose_step1_roi_clip_onscreen.py OUT.json
"""

import importlib.util
import json
import os
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

from PyQt5 import QtCore, QtGui, QtWidgets                  # noqa: E402

MANIFEST = pathlib.Path(os.environ.get(
    "G32C_MANIFEST",
    "/tmp/g32c_link/roi_20260922_184714_45dd/step0/step0_roi_result.json"))
OUT_DIR = pathlib.Path("/tmp/claude-0/-sda1-Fusion-analysis-pipline/"
                       "369ac340-1686-45d2-a009-a7e17fdcb39e/scratchpad")
APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
REPORT = {"display": os.environ.get("DISPLAY"), "steps": [], "submissions": []}


def note(kind, **fields):
    REPORT["steps"].append({"kind": kind, **fields})
    print(f"[{kind}] " + " ".join(f"{k}={v!r}" for k, v in fields.items()),
          flush=True)


def to_array(image):
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    pointer = image.constBits()
    pointer.setsize(image.byteCount())
    return np.frombuffer(pointer, np.uint8).reshape(
        (image.height(), image.bytesPerLine() // 4, 4))[:, :image.width(), :].copy()


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else str(OUT_DIR / "g32c_onscreen.json"))
    from block01.ui.main_window import MainWindow
    from block01.ui.step1_gpu_layer import Step1GpuLayer
    from block01.ui.step1_draft_spec import STEP1_SCOPE

    real_submit = Step1GpuLayer.submit

    def submit(self, source, display, viewport):
        stats = real_submit(self, source, display, viewport)
        REPORT["submissions"].append({
            "world_rect": list(viewport.world_rect),
            "roi_world_rect": (None if viewport.roi_world_rect is None
                               else list(viewport.roi_world_rect)),
            "physical_size": list(viewport.physical_size),
            "dpr": float(viewport.device_pixel_ratio),
            "roi_scissor": (None if stats.get("roi_scissor") is None
                            else list(stats["roi_scissor"])),
            "channels": [c.channel for c in source.channels]})
        return stats

    Step1GpuLayer.submit = submit

    window = MainWindow()
    window.resize(1600, 1000)
    window.show()
    for _ in range(40):
        APP.processEvents()
        time.sleep(0.02)

    window.step0_output = {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(MANIFEST),
        "output_dir": str(MANIFEST.parent),
        "step1_dir": "/tmp/g32c_link/step1",
    }
    window.loader = None
    accepted = window._load_step0_roi_result(auto=True)
    note("handoff", accepted=accepted,
         active_roi=(window._active_roi or {}).get("name"),
         bbox=(window._active_roi or {}).get("bbox_fullres"))

    window._stack.setCurrentWidget(window._step1_page_widget)
    APP.processEvents()
    window._set_step_active(1)
    APP.processEvents()
    window.right_tabs.setCurrentWidget(window.viewer_tab)
    for _ in range(60):
        APP.processEvents()
        time.sleep(0.02)

    mount = window._step1_mount
    note("backend", backend=getattr(mount, "backend", None),
         reason=(mount.gpu_status().get("reason") if mount else None),
         gl_renderer=(mount.gpu_layer.environment_report().get("gl_renderer")
                      if mount and mount.gpu_layer else None))

    # the product's own tick, with the draft bound and its groups built
    window._bind_fusion_dataset(str(window.loader.filepath), reason="g32c")
    # A corrected channel if the project has one, otherwise whatever it DOES
    # have: the 20:20 project on the machine is entirely `original`, and a
    # first version of this script ticked nothing there and then reported
    # "0 pixels outside" from an empty frame.
    decisions = window._corrected_decisions or {}
    names = list(window.loader.channel_names())
    channel = next((c for c, m in decisions.items()
                    if m in ("cucim", "tophat")), None)
    if channel is None:
        channel = "DAPI" if "DAPI" in names else (names[0] if names else None)
    assert channel, "the project has no channels to tick"
    if not window._display.fusion.is_initialized():
        window.config.set_channels(names)
        window.config.load_panel({"markers": [channel]}, "DAPI")
        window.config.set_nucleus("DAPI", 1.0)
    window._display.fusion.set_fusion_enabled(channel, True, origin="g32c")
    window._display.fusion.edit_channel_weight(channel, 1.0, origin="g32c")
    with window._display.state.using_scope(STEP1_SCOPE):
        window._display.state.set_display_visible(channel, True)
        window._display.state.set_mapping(channel, 0.0, 255.0, 1.0)
    note("tick", channel=channel,
         weight=window._display.fusion.stored_weights(channel))

    binding = mount.gpu_binding
    deadline = time.time() + 120.0
    while time.time() < deadline:
        APP.processEvents()
        if binding is not None:
            stats = binding.stats()
            if (channel in stats["coarse_channels"]
                    and binding._viewport_fine_ready(channel)):
                break
        time.sleep(0.02)
    for _ in range(60):
        APP.processEvents()
        time.sleep(0.02)
    note("binding", coarse=list(binding.stats()["coarse_channels"]),
         fine=list(binding.stats()["fine_channels"]))

    layer = mount.gpu_layer
    viewport = mount._gpu_viewport_snapshot()
    world = viewport.world_rect
    roi = viewport.roi_world_rect
    note("viewport", world_rect=list(world),
         roi_world_rect=None if roi is None else list(roi),
         physical=list(viewport.physical_size), dpr=viewport.device_pixel_ratio,
         layer_geometry=[layer.x(), layer.y(), layer.width(), layer.height()])

    fbo = layer.readback_rgba_for_test()
    shown = to_array(layer.grabFramebuffer())
    screen = QtWidgets.QApplication.primaryScreen()
    window_shot = to_array(screen.grabWindow(window.winId()).toImage())

    # G3.2c.1: the DRAWN SHAPE, judged on the same three layers.
    polygon = viewport.roi_polygon_world
    if polygon:
        from matplotlib.path import Path
        height, width = fbo.shape[:2]
        wx0, wx1, wy0, wy1 = world
        xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
        ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
        gx, gy = np.meshgrid(xs, ys)
        in_poly = Path(np.asarray(polygon, float)).contains_points(
            np.column_stack([gx.ravel(), gy.ravel()])).reshape(height, width)
        from scipy.ndimage import binary_dilation
        strict_out = ~binary_dilation(in_poly, np.ones((3, 3), bool))
        layer_tl = layer.mapTo(window, QtCore.QPoint(0, 0))
        win_poly = np.zeros(window_shot.shape[:2], bool)
        win_out = np.zeros(window_shot.shape[:2], bool)
        y0, x0 = layer_tl.y(), layer_tl.x()
        win_poly[y0:y0 + height, x0:x0 + width] = in_poly
        win_out[y0:y0 + height, x0:x0 + width] = strict_out
        REPORT["polygon"] = {
            "points": len(polygon),
            "pixels_inside_polygon": int(np.count_nonzero(in_poly)),
            "fbo_opaque_inside": int(np.count_nonzero((fbo[..., 3] > 0) & in_poly)),
            "fbo_opaque_outside": int(np.count_nonzero((fbo[..., 3] > 0) & strict_out)),
            "gl_widget_nonblack_outside": int(np.count_nonzero(
                (shown[..., :3].max(axis=-1) > 8) & strict_out)),
            "window_nonblack_outside": int(np.count_nonzero(
                (window_shot[..., :3].max(axis=-1) > 8) & win_out)),
            "window_nonblack_inside": int(np.count_nonzero(
                (window_shot[..., :3].max(axis=-1) > 8) & win_poly)),
        }
        note("polygon", **REPORT["polygon"])

    def mask_for(shape):
        height, width = shape[:2]
        wx0, wx1, wy0, wy1 = world
        bx0, bx1, by0, by1 = roi
        xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
        ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
        return ((ys[:, None] >= by0) & (ys[:, None] < by1)
                & (xs[None, :] >= bx0) & (xs[None, :] < bx1))

    inside_fbo = mask_for(fbo.shape)
    inside_shown = mask_for(shown.shape)
    REPORT["fbo"] = {
        "shape": list(fbo.shape),
        "opaque_outside": int(np.count_nonzero((fbo[..., 3] > 0) & ~inside_fbo)),
        "opaque_inside": int(np.count_nonzero((fbo[..., 3] > 0) & inside_fbo)),
    }
    REPORT["gl_widget"] = {
        "shape": list(shown.shape),
        "nonblack_outside": int(np.count_nonzero(
            (shown[..., :3].max(axis=-1) > 8) & ~inside_shown)),
        "nonblack_inside": int(np.count_nonzero(
            (shown[..., :3].max(axis=-1) > 8) & inside_shown)),
    }
    note("fbo", **REPORT["fbo"])
    note("gl_widget", **REPORT["gl_widget"])

    # ── ONE point the user would call "outside the rectangle" ──
    bx0, bx1, by0, by1 = roi
    probes = {
        "right_of_bbox": ((bx1 + min(400.0, (world[1] - bx1) / 2)),
                          (by0 + by1) / 2),
        "below_bbox": ((bx0 + bx1) / 2,
                       (by1 + min(400.0, (world[3] - by1) / 2))),
        "left_of_bbox": (max(world[0] + 10.0, bx0 - 400.0), (by0 + by1) / 2),
    }
    points = {}
    for name, (wx, wy) in probes.items():
        if not (world[0] < wx < world[1] and world[2] < wy < world[3]):
            points[name] = {"skipped": "outside the camera"}
            continue
        entry = {"world": [wx, wy],
                 "inside_bbox": bool(bx0 <= wx < bx1 and by0 <= wy < by1)}
        for label, image in (("fbo", fbo), ("gl_widget", shown)):
            height, width = image.shape[:2]
            col = int((wx - world[0]) / (world[1] - world[0]) * width)
            row = int((wy - world[2]) / (world[3] - world[2]) * height)
            col = max(0, min(width - 1, col))
            row = max(0, min(height - 1, row))
            entry[label] = {"pixel": [row, col],
                            "rgba": [int(v) for v in image[row, col]]}
        # the same world point on the SCREEN: layer -> window coordinates
        local = QtCore.QPoint(entry["gl_widget"]["pixel"][1],
                              entry["gl_widget"]["pixel"][0])
        in_window = layer.mapTo(window, local)
        row = max(0, min(window_shot.shape[0] - 1, in_window.y()))
        col = max(0, min(window_shot.shape[1] - 1, in_window.x()))
        entry["window"] = {"pixel": [row, col],
                           "rgba": [int(v) for v in window_shot[row, col]]}
        points[name] = entry
        note(f"probe.{name}", **{k: v for k, v in entry.items() if k != "world"},
             world=[round(wx, 1), round(wy, 1)])
    REPORT["probes"] = points

    # ── what is in the ViewBox, and could any of it paint there? ──
    stack = mount.host.stack
    items = []
    for item in getattr(stack.view.view_box, "addedItems", []):
        items.append({"type": type(item).__name__,
                      "visible": bool(item.isVisible()),
                      "opacity": float(item.opacity()),
                      "z": float(item.zValue()),
                      "has_image": bool(getattr(item, "image", None) is not None)})
    REPORT["viewbox_items"] = items
    note("viewbox_items", n=len(items),
         painting=[i for i in items if i["visible"] and i["opacity"] > 0
                   and i["has_image"]])
    REPORT["widgets"] = {
        "gl_layer_visible": bool(layer.isVisible()),
        "legacy_visible": bool(mount._legacy is not None
                               and mount._legacy.isVisible()),
        "composed_layer": mount.layer is not None,
        "host_geometry": [mount.host.x(), mount.host.y(),
                          mount.host.width(), mount.host.height()],
        "viewer_tab_geometry": [window.viewer_tab.x(), window.viewer_tab.y(),
                                window.viewer_tab.width(),
                                window.viewer_tab.height()],
    }
    note("widgets", **REPORT["widgets"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QtGui.QImage(fbo.data, fbo.shape[1], fbo.shape[0], fbo.shape[1] * 4,
                 QtGui.QImage.Format_RGBA8888).save(str(OUT_DIR / "g32c_fbo.png"))
    QtGui.QImage(shown.data, shown.shape[1], shown.shape[0], shown.shape[1] * 4,
                 QtGui.QImage.Format_RGBA8888).save(str(OUT_DIR / "g32c_gl.png"))
    QtGui.QImage(window_shot.data, window_shot.shape[1], window_shot.shape[0],
                 window_shot.shape[1] * 4, QtGui.QImage.Format_RGBA8888).save(
        str(OUT_DIR / "g32c_window.png"))
    REPORT["last_submissions"] = REPORT["submissions"][-3:]
    out_path.write_text(json.dumps(REPORT, indent=1, default=str))
    print("wrote", out_path, flush=True)
    window.close()
    for _ in range(20):
        APP.processEvents()
    return 0


if __name__ == "__main__":
    sys.exit(main())
