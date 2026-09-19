# Step1 G0.1 PyOpenGL Probe

- Raw data: [2026-09-20_g0_1_regression_fusion_test2.json](2026-09-20_g0_1_regression_fusion_test2.json)
- Result: **G0.1 core feasibility passed; desktop presentation remains separately limited**
- Blocker: **none**
- Mode: synthetic float32 raw planes; this is not a provider/scheduler/real-project I/O benchmark.
- PyOpenGL: `3.1.10` from `/root/micromamba/envs/fusion_test2/lib/python3.10/site-packages/OpenGL/__init__.py`
- Qt platform: `desktop/default`
- GL_VENDOR / GL_RENDERER: `NVIDIA Corporation` / `NVIDIA GeForce RTX 4090/PCIe/SSE2`
- GL_VERSION / GLSL: `3.3.0 NVIDIA 535.309.01` / `3.30 NVIDIA via Cg compiler`
- Software renderer detected: `False`

## Evidence limits

Qt owns the sole window/context/event lifecycle. PyOpenGL makes texture,
shader, draw, FBO/readback and deletion calls only while that Qt context is
current. FBO readback is a CPU/GPU pixel-correctness boundary; it is neither
actual desktop presentation nor compositor/vsync/input-to-display evidence.
Upload and draw submission durations are explicitly not GPU-completion claims.

## Exit gates

- `two_real_raw_texture_channels`: **pass**
- `hardware_renderer`: **pass**
- `initial_c1_match_le_1_lsb`: **pass**
- `parameter_c1_match_le_1_lsb`: **pass**
- `parameter_pixels_changed`: **pass**
- `parameter_zero_source_read_and_upload`: **pass**
- `independent_mixed_lod`: **pass**
- `viewbox_camera_adapter`: **pass**
- `context_current_gl_cleanup`: **pass**
- `desktop_presentation`: **pass**

## G1 interface recommendation (not implemented)

If a future separately approved G1 starts, its `Step1GpuLayer` should accept
immutable source descriptors (channel/source/generation/tile/world geometry,
independently available coarse/fine planes and valid mask), existing display
snapshots, and an existing ViewBox viewport snapshot. It may own only Qt GL
resources and a bounded local texture cache. It must not own the camera,
provider, scheduler, raw cache, shared UI, source authority or a new state
system. Future product mounting remains G3-only.

G0.1 已停止；G1 未启动；生产界面未接管；未提交、未 push。
