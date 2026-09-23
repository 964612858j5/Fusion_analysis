"""Run the real application with a Step1 ROI-clip probe attached.

G3.2c. The clip works in every scenario this machine can construct --
the user's own project, their full-WSI-then-ROI sequence, raw DAPI,
Overlay and Fusion, offscreen and on the real X display. The failure is
real but only happens in their session, so this records that session
instead of asking for another special test.

    DISPLAY=:1 python scripts/run_with_roi_clip_probe.py

WHAT IT RECORDS, under /tmp/g32c_probe (override with
`BLOCK01_ROI_PROBE_DIR`):

  * `submissions.jsonl` -- one line per GPU submission: backend, the live
    source table's ROI, `roi_world_rect`, `roi_scissor`, world rect,
    physical size, DPR, channels;
  * `state_<n>.json` every few seconds while Step1's viewer is up, and
    `fbo_<n>.png` / `viewer_<n>.png` beside it. EVERY sampled state is
    written, not only the leaking ones, and each of these three cases is
    recorded distinctly:
      - `case: "no_roi"`        -- the source has no ROI, so nothing is
                                   clipped by design;
      - `case: "not_gpu"`       -- backend is `cpu-fallback`/`legacy`, so
                                   the G3.2c clip never ran at all. The
                                   viewer widget is still captured, since
                                   that is the picture the user sees;
      - `case: "gpu"`           -- with `fbo_opaque_outside` and
                                   `viewer_nonblack_outside` side by side,
                                   so "framebuffer clean but screen dirty"
                                   is visible as such.
  * `viewbox_items` inside each state file -- every item in the ViewBox
    with visibility, opacity and z-order, so "something else is painting"
    can be proven or ruled out.

CAPTURING THE FRAME YOU ARE LOOKING AT, rather than waiting for the timer:

  * press **Ctrl+Alt+R** in the application window, or
  * `touch /tmp/g32c_probe/CAPTURE` from a terminal.

Either writes the CURRENT frame immediately, tagged `"trigger": "manual"`.

BOUNDED: it stops sampling after `BLOCK01_ROI_PROBE_MAX_MINUTES` (default
60), `BLOCK01_ROI_PROBE_MAX_CAPTURES` (default 240) or
`BLOCK01_ROI_PROBE_MAX_MB` (default 512) of output, whichever comes first,
and says so in the log. Manual captures are still honoured after the
periodic sampling stops, up to the same disk cap.

WHAT IT DOES AND DOES NOT TOUCH. The probe itself only observes: it wraps
`Step1GpuLayer.submit` to read what was passed and returns the real
result, and reads back pixels; it changes no source, no ROI, no camera and
no display state, and it writes only under its own /tmp directory. It is
NOT a read-only session: **the application behaves exactly as usual**, so
Step0 Save still writes projects, Step1 still autosaves its session, and
any other normal write still happens. Two observational side effects worth
naming: `grabFramebuffer()` asks the GL widget to render one extra frame,
and reading pixels back makes its context current briefly.
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

OUT = pathlib.Path(os.environ.get("BLOCK01_ROI_PROBE_DIR", "/tmp/g32c_probe"))
PERIOD = float(os.environ.get("BLOCK01_ROI_PROBE_PERIOD", "5.0"))
MAX_MINUTES = float(os.environ.get("BLOCK01_ROI_PROBE_MAX_MINUTES", "60"))
MAX_CAPTURES = int(os.environ.get("BLOCK01_ROI_PROBE_MAX_CAPTURES", "240"))
MAX_BYTES = int(float(os.environ.get("BLOCK01_ROI_PROBE_MAX_MB", "512"))
                * 1024 * 1024)
TRIGGER = OUT / "CAPTURE"

OUT.mkdir(parents=True, exist_ok=True)
_probe = {"n": 0, "started": time.time(), "periodic_stopped": False,
          "submissions": 0, "lines": None}


def _bytes_used():
    return sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())


def _budget_left(kind):
    """`(ok, why)` -- whether another capture of `kind` may be written."""
    if _bytes_used() >= MAX_BYTES:
        return False, f"disk cap reached ({MAX_BYTES // (1024 * 1024)} MiB)"
    if kind == "manual":
        return True, ""
    if _probe["n"] >= MAX_CAPTURES:
        return False, f"capture cap reached ({MAX_CAPTURES})"
    if (time.time() - _probe["started"]) > MAX_MINUTES * 60:
        return False, f"time cap reached ({MAX_MINUTES:g} min)"
    return True, ""


def _array(image):
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
    pointer = image.constBits()
    pointer.setsize(image.byteCount())
    return np.frombuffer(pointer, np.uint8).reshape(
        (image.height(), image.bytesPerLine() // 4, 4))[:, :image.width(), :].copy()


def _save(array, path):
    """Write an HxWx4 uint8 array as a PNG.

    `QImage` is built from `tobytes()`, not from the array's buffer: PyQt5
    refuses a numpy memoryview (`TypeError: arguments did not match any
    overloaded call`), and a QImage that borrows a buffer would also need
    that buffer kept alive until `save()` returns.
    """
    height, width = array.shape[:2]
    payload = np.ascontiguousarray(array).tobytes()
    image = QtGui.QImage(payload, width, height, width * 4,
                         QtGui.QImage.Format_RGBA8888)
    image.save(str(path))


def _outside_mask(shape, world, roi):
    height, width = shape[:2]
    wx0, wx1, wy0, wy1 = world
    bx0, bx1, by0, by1 = roi
    xs = wx0 + (np.arange(width) + 0.5) * (wx1 - wx0) / width
    ys = wy0 + (np.arange(height) + 0.5) * (wy1 - wy0) / height
    inside = ((ys[:, None] >= by0) & (ys[:, None] < by1)
              & (xs[None, :] >= bx0) & (xs[None, :] < bx1))
    return ~inside


def _viewbox_items(mount):
    stack = getattr(getattr(mount, "host", None), "stack", None)
    items = []
    try:
        for item in stack.view.view_box.addedItems:
            items.append({"type": type(item).__name__,
                          "visible": bool(item.isVisible()),
                          "opacity": float(item.opacity()),
                          "z": float(item.zValue()),
                          "has_image": bool(getattr(item, "image", None) is not None)})
    except Exception:                                       # noqa: BLE001
        pass
    return items


def _capture(window, trigger="periodic"):
    ok, why = _budget_left(trigger)
    if not ok:
        if not _probe["periodic_stopped"]:
            _probe["periodic_stopped"] = True
            print(f"[roi-probe] sampling stopped: {why}", flush=True)
        if trigger != "manual":
            return
        if _bytes_used() >= MAX_BYTES:
            return
    mount = getattr(window, "_step1_mount", None)
    if mount is None:
        return
    index = _probe["n"]
    entry = {"trigger": trigger, "t": time.time(),
             "backend": getattr(mount, "backend", None),
             "gpu_reason": (mount.gpu_status().get("reason")
                            if hasattr(mount, "gpu_status") else None),
             "viewbox_items": _viewbox_items(mount),
             "legacy_visible": bool(getattr(mount, "_legacy", None) is not None
                                    and mount._legacy.isVisible()),
             "composed_layer": getattr(mount, "layer", None) is not None}
    layer = getattr(mount, "gpu_layer", None)

    # CASE 1: the GPU layer is not the picture. The clip never ran; capture
    # what IS on screen so the report is about the user's picture, not about
    # a layer that is not there.
    if layer is None or not layer.isVisible():
        entry["case"] = "not_gpu"
        entry["why"] = ("there is no GPU layer at all" if layer is None
                        else "the GPU layer exists but is not visible, so it "
                             "is not what is on screen")
        try:
            shot = _array(mount.host.grab().toImage())
            _save(shot, OUT / f"viewer_{index:03d}.png")
            entry["viewer_nonblack_pixels"] = int(np.count_nonzero(
                shot[..., :3].max(axis=-1) > 8))
            entry["viewer_shape"] = list(shot.shape)
        except Exception as exc:                            # noqa: BLE001
            entry["viewer_error"] = repr(exc)
        print(f"[roi-probe] state_{index:03d}: the GPU clip did NOT run for "
              f"this frame -- backend={entry['backend']}, {entry['why']}",
              flush=True)
    else:
        try:
            viewport = mount._gpu_viewport_snapshot()
        except Exception as exc:                            # noqa: BLE001
            entry["case"] = "no_viewport"
            entry["error"] = repr(exc)
            viewport = None
        if viewport is not None:
            entry["world_rect"] = list(viewport.world_rect)
            entry["physical_size"] = list(viewport.physical_size)
            entry["dpr"] = float(viewport.device_pixel_ratio)
            entry["roi_world_rect"] = (None if viewport.roi_world_rect is None
                                       else list(viewport.roi_world_rect))
            try:
                fbo = layer.readback_rgba_for_test()
                shown = _array(layer.grabFramebuffer())
            except Exception as exc:                        # noqa: BLE001
                fbo = shown = None
                entry["pixel_error"] = repr(exc)
            if fbo is not None:
                _save(fbo, OUT / f"fbo_{index:03d}.png")
                _save(shown, OUT / f"viewer_{index:03d}.png")
                entry["fbo_shape"] = list(fbo.shape)
                if viewport.roi_world_rect is None:
                    # CASE 2: no ROI on this source -- nothing is clipped, by
                    # design. Recorded so a full-slide project is never read
                    # as "the clip failed".
                    entry["case"] = "no_roi"
                    entry["fbo_opaque_pixels"] = int(np.count_nonzero(fbo[..., 3] > 0))
                    entry["viewer_nonblack_pixels"] = int(np.count_nonzero(
                        shown[..., :3].max(axis=-1) > 8))
                else:
                    # CASE 3: the clip ran. Both numbers are recorded side by
                    # side, so "framebuffer clean, screen dirty" is visible.
                    entry["case"] = "gpu"
                    out_fbo = _outside_mask(fbo.shape, viewport.world_rect,
                                            viewport.roi_world_rect)
                    out_shown = _outside_mask(shown.shape, viewport.world_rect,
                                              viewport.roi_world_rect)
                    entry["fbo_opaque_outside"] = int(np.count_nonzero(
                        (fbo[..., 3] > 0) & out_fbo))
                    entry["fbo_opaque_inside"] = int(np.count_nonzero(
                        (fbo[..., 3] > 0) & ~out_fbo))
                    entry["viewer_nonblack_outside"] = int(np.count_nonzero(
                        (shown[..., :3].max(axis=-1) > 8) & out_shown))
                    entry["viewer_nonblack_inside"] = int(np.count_nonzero(
                        (shown[..., :3].max(axis=-1) > 8) & ~out_shown))
                    if entry["fbo_opaque_outside"] or entry["viewer_nonblack_outside"]:
                        print(f"[roi-probe] LEAK CAPTURED in state_{index:03d}.json: "
                              f"fbo_outside={entry['fbo_opaque_outside']} "
                              f"viewer_outside={entry['viewer_nonblack_outside']}",
                              flush=True)
    (OUT / f"state_{index:03d}.json").write_text(
        json.dumps(entry, indent=1, default=str))
    _probe["n"] = index + 1
    if trigger == "manual":
        print(f"[roi-probe] manual capture -> state_{index:03d}.json "
              f"(case={entry.get('case')})", flush=True)


def _tick(window):
    if TRIGGER.exists():
        try:
            TRIGGER.unlink()
        except OSError:
            pass
        _capture(window, trigger="manual")
        return
    if _probe["periodic_stopped"]:
        return
    if (time.time() - _probe.get("last", 0.0)) >= PERIOD:
        _probe["last"] = time.time()
        _capture(window, trigger="periodic")


def _install(window):
    from block01.ui.step1_gpu_layer import Step1GpuLayer

    lines = (OUT / "submissions.jsonl").open("a", buffering=1)
    _probe["lines"] = lines
    real_submit = Step1GpuLayer.submit

    def submit(self, source, display, viewport):
        stats = real_submit(self, source, display, viewport)
        _probe["submissions"] += 1
        if _bytes_used() < MAX_BYTES:
            mount = getattr(window, "_step1_mount", None)
            provider = getattr(getattr(getattr(mount, "host", None), "stack", None),
                               "provider", None)
            try:
                table_roi = (provider.source_table.roi_bbox()
                             if provider is not None else None)
            except Exception:                               # noqa: BLE001
                table_roi = None
            try:
                lines.write(json.dumps({
                    "t": time.time(),
                    "backend": getattr(mount, "backend", None),
                    "table_roi_bbox": None if table_roi is None else list(table_roi),
                    "roi_world_rect": (None if viewport.roi_world_rect is None
                                       else list(viewport.roi_world_rect)),
                    "roi_scissor": (None if stats.get("roi_scissor") is None
                                    else list(stats["roi_scissor"])),
                    "world_rect": list(viewport.world_rect),
                    "physical_size": list(viewport.physical_size),
                    "dpr": float(viewport.device_pixel_ratio),
                    "mode": stats.get("mode"),
                    "channels": [c.channel for c in source.channels],
                }) + "\n")
            except Exception:                               # noqa: BLE001
                pass
        return stats

    Step1GpuLayer.submit = submit

    timer = QtCore.QTimer(window)
    timer.setInterval(1000)                 # 1 s, so the trigger file is quick
    timer.timeout.connect(lambda: _tick(window))
    timer.start()
    window._roi_probe_timer = timer

    shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Alt+R"), window)
    shortcut.setContext(QtCore.Qt.ApplicationShortcut)
    shortcut.activated.connect(lambda: _capture(window, trigger="manual"))
    window._roi_probe_shortcut = shortcut

    print(f"[roi-probe] writing to {OUT}", flush=True)
    print(f"[roi-probe] press Ctrl+Alt+R (or `touch {TRIGGER}`) to capture "
          f"the frame you are looking at", flush=True)
    print(f"[roi-probe] caps: {MAX_MINUTES:g} min / {MAX_CAPTURES} captures / "
          f"{MAX_BYTES // (1024 * 1024)} MiB", flush=True)
    print("[roi-probe] the APPLICATION still behaves normally: Save writes "
          "projects and Step1 autosaves its session as usual", flush=True)


def main():
    from block01 import main as app_main
    from block01.ui.main_window import MainWindow

    real_init = MainWindow.__init__

    def patched(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        _install(self)

    MainWindow.__init__ = patched
    return app_main.main()


if __name__ == "__main__":
    sys.exit(main())
