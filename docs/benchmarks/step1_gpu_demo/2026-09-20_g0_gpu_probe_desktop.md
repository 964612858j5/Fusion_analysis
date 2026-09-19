# Step1 G0 GPU Probe

- Raw data: [2026-09-20_g0_gpu_probe_desktop.json](2026-09-20_g0_gpu_probe_desktop.json)
- Result: **blocked or failed; inspect raw measurements**
- Blocker: **this PyQt5 build lacks the Qt QOpenGLFunctions binding needed to submit the G0 shader draw; no PyOpenGL/ctypes fallback is authorized**
- Mode: synthetic float32 source; it is not a real project-provider I/O benchmark.
- Qt platform: `desktop/default`
- OpenGL renderer: `unavailable`
- GPU vendor: `unavailable`
- OpenGL / GLSL: `unavailable` / `unavailable`
- Software renderer detected: `unavailable`

## Evidence and limits

Qt created the standalone QOpenGLWidget context, but this PyQt5 build could
not expose a Qt-native QOpenGLFunctions draw table.  The probe stopped before
texture upload, shader compilation/linking, FBO readback, or CPU pixel comparison.
All timings are raw JSON samples/summary values when a shader draw is reached;
upload and draw values are CPU submission costs, while FBO readback is not
on-screen presentation. No compositor/vsync input-to-photon value is claimed.
Source I/O, decode, RAM, and VRAM are deliberately marked unmeasured where the
isolated synthetic probe has no truthful instrument.

## Exit gates



## G1 interface recommendation (not implemented)

Use a self-contained `ui/step1_gpu_layer.py` later, with immutable source
(generation/channel/tile/world-geometry/coarse+fine raw planes/mask), display,
and ViewBox viewport snapshots plus GUI-thread `attach()`/idempotent
`dispose()`.  It should own only GL resources and a byte-bounded internal
texture cache.  Existing Step1 ViewBox/controller must remain the sole camera
and input owner; production mount integration remains G3-only.

G0 已停止；生产界面未接管；G1 未启动；未提交、未 push。
