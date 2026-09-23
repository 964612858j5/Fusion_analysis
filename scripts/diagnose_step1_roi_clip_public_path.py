"""G3.2c follow-up: does the drawn ROI actually reach the GPU's clip?

The pixel gates drive `Step1GpuLayer` directly. The user's failure is on the
PRODUCT path, so this walks that path instead and records, in ONE run:

  * the handoff's mode, ROI list and bbox;
  * `MainWindow._active_roi`;
  * `Step1ViewerBinding.roi_bbox()`;
  * the live stack's `provider.source_table.roi_bbox()`;
  * every GPU submission's `ViewportSnapshot.roi_world_rect` and the
    `roi_scissor` the layer answered with;
  * which backend is actually on screen;
  * the final FBO's pixels outside the bbox, and the same for the visible
    widget (`grabFramebuffer` of the GL layer is the FBO; `QWidget.grab()`
    of the viewer is what Qt composites).

The user's own Step0 output is mirrored into /tmp: the two zarr stores are
SYMLINKS (read-only) and the small JSONs are copies with their paths
rewritten, so the real project is never written to.

Usage: python scripts/diagnose_step1_roi_clip_public_path.py OUT.json
"""

import importlib.util
import json
import os
import pathlib
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _mod
    _spec.loader.exec_module(_mod)
sys.path.insert(0, str(_ROOT.parent))

from PyQt5 import QtCore, QtWidgets                        # noqa: E402

MANIFEST = pathlib.Path("/tmp/g32c_link/roi_20260922_184714_45dd/step0/"
                        "step0_roi_result.json")

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
RECORD = {"steps": [], "submissions": []}


def note(kind, **fields):
    RECORD["steps"].append({"kind": kind, **fields})
    print(f"[{kind}] " + " ".join(f"{k}={v!r}" for k, v in fields.items()),
          flush=True)


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else "/tmp/g32c_public_path.json")
    manifest = json.loads(MANIFEST.read_text())
    note("handoff.manifest", mode=manifest.get("mode"),
         active_roi=manifest.get("active_roi"),
         bbox_fullres=manifest.get("bbox_fullres"),
         analysis_region_type=manifest.get("analysis_region_type"),
         schema=manifest.get("handoff_schema_version"))
    roi_config = json.loads((MANIFEST.parent / "roi_config.json").read_text())
    note("handoff.roi_config",
         rois=[(r.get("name"), r.get("type"), r.get("bbox_fullres")) for r in roi_config])

    from block01.ui.main_window import MainWindow
    from block01.ui.step1_gpu_layer import Step1GpuLayer

    # ── record every submission the layer is given ──
    real_submit = Step1GpuLayer.submit

    def submit(self, source, display, viewport):
        stats = real_submit(self, source, display, viewport)
        RECORD["submissions"].append({
            "t": time.time(),
            "world_rect": list(viewport.world_rect),
            "roi_world_rect": (None if viewport.roi_world_rect is None
                               else list(viewport.roi_world_rect)),
            "physical_size": list(viewport.physical_size),
            "roi_scissor": (None if stats.get("roi_scissor") is None
                            else list(stats["roi_scissor"])),
            "mode": stats.get("mode"),
            "channels": [c.channel for c in source.channels],
        })
        return stats

    Step1GpuLayer.submit = submit

    window = MainWindow()
    window.resize(1500, 950)
    window.show()
    APP.processEvents()

    # THE PUBLIC HANDOFF: exactly what Step0 hands Step1 after Save.
    window.step0_output = {
        "handoff_schema_version": 2,
        "step0_manifest_path": str(MANIFEST),
        "output_dir": str(MANIFEST.parent),
        "step1_dir": "/tmp/g32c_link/step1",
    }
    window.loader = None
    accepted = window._load_step0_roi_result(auto=True)
    note("handoff.accepted", accepted=accepted)
    window.step0_done = bool(accepted)
    window._step1_context_ready = bool(accepted)

    note("main_window.active_roi",
         active_roi=(window._active_roi or {}).get("name"),
         bbox=(window._active_roi or {}).get("bbox_fullres"),
         n_rois=len(window._rois or []))

    # ENTER STEP1 THE WAY THE PRODUCT DOES: the whole-slide mount is built
    # by `_set_step_active(1)`, not by swapping the stacked widget -- a
    # first version of this bench only swapped the widget and found no
    # mount at all.
    window._stack.setCurrentWidget(window._step1_page_widget)
    APP.processEvents()
    window._set_step_active(1)
    APP.processEvents()
    window.right_tabs.setCurrentWidget(window.viewer_tab)
    for _ in range(40):
        APP.processEvents()
        time.sleep(0.02)

    mount = getattr(window, "_step1_mount", None)
    note("mount", exists=mount is not None,
         backend=getattr(mount, "backend", None),
         gpu_reason=(mount.gpu_status().get("reason") if mount else None))

    binding = getattr(mount, "viewer", None)
    note("viewer_binding.roi_bbox",
         value=(None if binding is None else binding.roi_bbox()))

    stack = getattr(getattr(mount, "host", None), "stack", None)
    table = getattr(getattr(stack, "provider", None), "source_table", None)
    note("source_table.roi_bbox",
         value=(None if table is None else table.roi_bbox()),
         table_present=table is not None)
    if mount is not None and stack is not None:
        note("mount._gpu_roi_world_rect",
             value=type(mount)._gpu_roi_world_rect(stack))

    # tick a corrected channel through the product's own command
    from block01.ui.step1_draft_spec import STEP1_SCOPE
    state = window._display.state
    decisions = window._corrected_decisions or {}
    corrected = [c for c, m in decisions.items() if m in ("cucim", "tophat")]
    note("decisions", corrected=corrected)
    if corrected:
        # THE PRODUCT'S TICK IS ONE COMMAND: show the channel AND put it in
        # the fusion (UI_SURFACE_RULES section 4). Setting only the display
        # flag leaves the draft's weight at zero, the GPU's active set
        # empty, and the framebuffer black -- which is what a first version
        # of this bench measured and nearly reported as "clipped".
        channel = corrected[0]
        domain = window._display.fusion
        domain.set_fusion_enabled(channel, True, origin="g32c-bench")
        domain.edit_channel_weight(channel, 1.0, origin="g32c-bench")
        with state.using_scope(STEP1_SCOPE):
            state.set_display_visible(channel, True)
            state.set_mapping(channel, 0.0, 255.0, 1.0)
        note("ticked", channel=channel,
             weight=domain.stored_weights(channel),
             enabled=domain.fusion_enabled(channel))
    for _ in range(200):
        APP.processEvents()
        time.sleep(0.02)

    if mount is not None:
        try:
            snap = mount._gpu_display_snapshot()
            note("display_snapshot", mode=snap.mode,
                 weights={k: v for k, v in snap.weights.items() if v},
                 mappings=sorted(snap.mappings))
        except Exception as exc:                            # noqa: BLE001
            note("display_snapshot.error", error=repr(exc))
        binding_obj = mount.gpu_binding
        if binding_obj is not None:
            note("binding.stats", coarse=binding_obj.stats()["coarse_channels"],
                 fine=binding_obj.stats()["fine_channels"],
                 last_error=binding_obj.stats()["last_error"])
    RECORD["submission_count"] = len(RECORD["submissions"])
    RECORD["last_submissions"] = RECORD["submissions"][-3:]

    # ── pixels: the layer's own FBO, and what Qt composites on screen ──
    layer = getattr(mount, "gpu_layer", None)
    if layer is not None:
        frame = layer.readback_rgba_for_test()
        viewport = mount._gpu_viewport_snapshot()
        roi = viewport.roi_world_rect
        height, width = frame.shape[:2]
        wx0, wx1, wy0, wy1 = viewport.world_rect
        xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
        ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
        if roi is None:
            inside = np.ones((height, width), bool)
        else:
            bx0, bx1, by0, by1 = roi
            inside = ((ys[:, None] >= by0) & (ys[:, None] < by1)
                      & (xs[None, :] >= bx0) & (xs[None, :] < bx1))
        RECORD["fbo"] = {
            "shape": [height, width],
            "world_rect": list(viewport.world_rect),
            "roi_world_rect": None if roi is None else list(roi),
            "opaque_outside_bbox": int(np.count_nonzero((frame[..., 3] > 0) & ~inside)),
            "opaque_inside_bbox": int(np.count_nonzero((frame[..., 3] > 0) & inside)),
            "nonblack_outside_bbox": int(np.count_nonzero(
                (frame[..., :3].max(axis=-1) > 0) & ~inside)),
        }
        note("fbo", **RECORD["fbo"])

        # what the user's widget shows: grab the VIEWER, not the GL layer
        widget = mount.host
        image = widget.grab().toImage().convertToFormat(
            QtWidgets.QApplication.instance() and 5 or 5)  # Format_RGB32
        buffer = image.constBits()
        buffer.setsize(image.byteCount())
        shot = np.frombuffer(buffer, np.uint8).reshape(
            (image.height(), image.bytesPerLine() // 4, 4))[:, :image.width(), :]
        RECORD["visible"] = {
            "shape": [int(image.height()), int(image.width())],
            "nonblack_pixels": int(np.count_nonzero(shot[..., :3].max(axis=-1) > 8)),
            "total": int(image.width() * image.height()),
        }
        note("visible_widget", **RECORD["visible"])

    # ── WHAT ELSE CAN PAINT IN THIS TAB while the GPU layer is up? ──
    # If the framebuffer is clipped but the user still sees pixels outside
    # the region, they are coming from something ELSE that is still drawing
    # -- the controller's own items, the CPU composed layer, or the legacy
    # patch view behind the overlay.
    if mount is not None and stack is not None:
        view = getattr(stack, "view", None)
        items = []
        try:
            for item in view.view_box.addedItems:
                items.append({
                    "type": type(item).__name__,
                    "visible": bool(item.isVisible()),
                    "opacity": float(item.opacity()),
                    "has_image": bool(getattr(item, "image", None) is not None),
                })
        except Exception as exc:                            # noqa: BLE001
            items = [{"error": repr(exc)}]
        RECORD["viewbox_items"] = items
        painting = [i for i in items if i.get("visible")
                    and i.get("opacity", 0) > 0 and i.get("has_image")]
        note("viewbox_items", total=len(items), still_painting=len(painting),
             detail=painting[:6])
        RECORD["legacy_widget"] = {
            "composed_layer_present": mount.layer is not None,
            "compose_binding_present": mount.compose is not None,
            "legacy_widget_visible": bool(
                mount._legacy is not None and mount._legacy.isVisible()),
            "gpu_layer_visible": bool(mount.gpu_layer is not None
                                      and mount.gpu_layer.isVisible()),
            "gpu_layer_geometry": (None if mount.gpu_layer is None else
                                   [mount.gpu_layer.x(), mount.gpu_layer.y(),
                                    mount.gpu_layer.width(),
                                    mount.gpu_layer.height()]),
            "host_geometry": [mount.host.x(), mount.host.y(),
                              mount.host.width(), mount.host.height()],
        }
        note("legacy_widget", **RECORD["legacy_widget"])
        # the GL widget's OWN composited output (what Qt shows), not the FBO
        if mount.gpu_layer is not None:
            image = mount.gpu_layer.grabFramebuffer()
            image = image.convertToFormat(image.Format_RGBA8888)
            pointer = image.constBits()
            pointer.setsize(image.byteCount())
            shown = np.frombuffer(pointer, np.uint8).reshape(
                (image.height(), image.bytesPerLine() // 4, 4))[:, :image.width(), :]
            height, width = shown.shape[:2]
            viewport2 = mount._gpu_viewport_snapshot()
            wx0, wx1, wy0, wy1 = viewport2.world_rect
            bx0, bx1, by0, by1 = viewport2.roi_world_rect or (wx0, wx1, wy0, wy1)
            xs2 = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
            ys2 = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
            inside2 = ((ys2[:, None] >= by0) & (ys2[:, None] < by1)
                       & (xs2[None, :] >= bx0) & (xs2[None, :] < bx1))
            RECORD["gl_widget_grab"] = {
                "shape": [height, width],
                "nonblack_outside_bbox": int(np.count_nonzero(
                    (shown[..., :3].max(axis=-1) > 8) & ~inside2)),
                "nonblack_inside_bbox": int(np.count_nonzero(
                    (shown[..., :3].max(axis=-1) > 8) & inside2)),
            }
            note("gl_widget_grab", **RECORD["gl_widget_grab"])

    out_path.write_text(json.dumps(RECORD, indent=1, default=str))
    print("wrote", out_path)
    window.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
