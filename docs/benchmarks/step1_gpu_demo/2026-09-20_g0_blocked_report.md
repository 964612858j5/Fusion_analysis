# Step1 G0 GPU 接入可行性与测量基线 — 阻塞报告

**日期：** 2026-09-20
**任务块：** G0（仅独立原型、测量与报告）
**结论：** **阻塞；未达到 G0 退出门。** 本机 PyQt5 绑定无法提供 Qt 原生 OpenGL 函数表以提交 shader draw。按 G0 范围，未安装 PyOpenGL、未使用 `ctypes`，也未改动生产 viewer/UI/调度/provider，因此没有替代后端或 G1 工作。

## 运行基线与保护状态

| 项目 | 值 |
| --- | --- |
| 起始 HEAD / 分支 | `5fc5a9bd9d05732144373dca22f3818866b01e49` / `v15-interactive-channel-workspace` |
| 结束 HEAD / 分支 | `5fc5a9bd9d05732144373dca22f3818866b01e49` / `v15-interactive-channel-workspace` |
| 起始时预存工作区内容 | `cufile.log`、`docs/tma_study_mode.md` 的修改；`AGENTS.md`、`docs/P0_SCOPE_RULES.md`、`docs/step1_gpu_demo_plan.md` 和其他任务文档未跟踪 |
| 本次保护措施 | 未清理、覆盖、暂存或回退任何上述内容；未提交、未 push。早期依赖导入曾触发环境的 CUDA/cuFile 诊断追加到已修改的 `cufile.log`；由于它可与其他进程交错且起始内容未知，未尝试截断/恢复。最终 capability-first 重跑前后该文件行数为 `60650`，delta 为 `0`。 |
| Odon 只读参考 | `b01faef010b14ea03c33e92a14e438061d257e39` (`main`)，其工作区已有未跟踪 `odon-app/`、`odon_0.1.5_amd64.deb`，未修改 |

## 机器与运行条件

- `DISPLAY=:1`；desktop 和 `QT_QPA_PLATFORM=offscreen` 两种入口均实际执行。
- Python `3.10.20`、PyQt `5.15.11`、Qt `5.15.14`；Linux `7.0.0-31-generic`。
- `nvidia-smi` 只读查询报 NVIDIA GeForce RTX 4090、驱动 `535.309.01`、显存 `49140 MiB`。
- `glxinfo` 不存在，因而未取得桌面 OpenGL renderer/driver 字符串；这不能用 NVIDIA 驱动查询替代 Qt context renderer 的确认。
- Qt 能创建独立 `QOpenGLWidget` context，但 `QOpenGLContext.versionFunctions()` 对实际 core profile 触发 `ModuleNotFoundError: No module named 'PyQt5._QOpenGLFunctions_3_3_Core'`。本 PyQt5 分发没有可用的 `QOpenGLFunctions`/`QOpenGLExtraFunctions` Python 绑定，无法从 Qt 原生 API 调用 `glDrawArrays`、framebuffer binding 和 capability query。

## 实测证据

| 模式 | 原始 JSON | 摘要 | 实际结果 |
| --- | --- | --- | --- |
| offscreen | [2026-09-20_g0_gpu_probe_offscreen.json](2026-09-20_g0_gpu_probe_offscreen.json) | [Markdown](2026-09-20_g0_gpu_probe_offscreen.md) | Qt context 建立后，在纹理上传、shader 编译/链接、FBO draw/readback 前停止；不代表真实桌面呈现。 |
| desktop | [2026-09-20_g0_gpu_probe_desktop.json](2026-09-20_g0_gpu_probe_desktop.json) | [Markdown](2026-09-20_g0_gpu_probe_desktop.md) | 同一绑定 blocker；没有声称窗口可见、swap、compositor/vsync 或输入到上屏已验证。 |

两份 probe 均记录同一明确 blocker：

> this PyQt5 build lacks the Qt QOpenGLFunctions binding needed to submit the G0 shader draw; no PyOpenGL/ctypes fallback is authorized

因此以下退出门均为 **未测/未通过**，不是失败后放宽的结论：至少两个真实 GPU 图像、Min/Max/Gamma/颜色/权重参数像素变化、热纹理零重读/零重上传、CPU C1 framebuffer 数值误差、逐通道粗/精层承接、现有 ViewBox 实际叠加兼容、GL 资源释放、真实上屏时间。

`tests/test_step1_gpu_probe.py` 依照 G0 停止条件将同一能力缺失标为精确 skip，而不悄悄换为 CPU 或其他 GL 后端：冷运行和三个独立 warm 进程均为 **5 skipped**，且均使用 `-p no:cacheprovider`。这不是 G0 通过证据。

## 耗时结论

### 已测事实

- 没有可提交的 shader draw，所以读取、解码、CPU C1 合成、R32F 上传、GPU 绘制、FBO readback 与实际呈现都**未测**。
- 不能把 Qt context 创建、OpenGL submission、offscreen frame preparation、worker 完成或 `nvidia-smi` 结果写成 GPU frame time/FPS。
- 离屏路径在 draw 前停止；即使它进入 FBO readback，也不可以作为真实桌面 FPS 或 compositor/vsync 成本。

### 可追溯但未外推的历史线索

已有 `docs/benchmarks/2026-08-31_57ch_multichannel_prefetch.md` 显示冷 I/O 可受 TIFF handle 初始化/GIL 等影响；这是历史 CPU/I/O 测量，**不是**本 G0 的当前 GPU 瓶颈。当前可确认的最早阻塞点是 Python Qt 绑定的 draw-function 接口，而非 RTX 4090、数据读取、CPU 合成或 GPU 性能。

## 原型与范围审计

本次新增的独立原型 `scripts/benchmark_step1_gpu_demo.py` 仅定义了计划中的 Qt-owned `QOpenGLWidget`、R32F 两通道/粗细层、Overlay shader、C1 oracle、ViewBox sibling-overlay adapter、timing/report 和 GUI-thread cleanup 路径。它未被生产模块导入。实际运行在函数表不可用处停止，未制造 GPU 像素或替代生产图层。

没有运行真实演示数据路径：该路径的 producer/provider/scheduler 是 G2 以后的只读适配边界；在 G0 不能提交 draw 的前提下继续接入或测量它不能回答 GPU 问题，且会无意义地扩展到受保护模块。报告因此诚实标为 synthetic-only/未测，而不是伪造冷/热数据性能。

## 仅供下一块审核的接口建议（未实施）

若用户先明确批准一个可用的 Qt/OpenGL function binding 或明确授权替代调用层，下一次 **重新执行 G0** 应首先重做 capability/proof gates，不应直接启动 G1。G0 验证通过后，建议的 G1 最小接口才是：

```text
Step1GpuLayer.attach(view_adapter)
Step1GpuLayer.submit(source_descriptor, display_snapshot, viewport_snapshot)
Step1GpuLayer.readback_rgba_for_test()
Step1GpuLayer.dispose()
```

- `source_descriptor`：不可变 channel/source/generation/tile/world geometry、每通道可独立存在的 coarse/fine raw plane 与 valid mask。
- `display_snapshot`：既有共享 Min/Max/Gamma、颜色、权重和现有公式的只读快照；GPU 层不计算新的显示窗口，也不建立权威 state。
- `viewport_snapshot`：既有 `ExploreView.view_box` 的 world range、物理 viewport、DPR 和 repaint 通知。生产 camera/input 仍只属于现有 ViewBox/controller。
- G1 内部才可在其获批的边界内拥有 Qt GL resources 与有字节上限的纹理 cache；不得拥有 provider、scheduler、raw cache、来源身份、共享 UI 或新 registry/authority/state machine。
- 未来 G3 mount 才可能把 GPU widget 作为 `ExploreView.graphics.viewport()` 的 sibling overlay 接入；G0 未修改也未授权该接入。

建议纹理候选格式为 `GL_R32F` / `GL_RED` / `GL_FLOAT`，理由是 C1 corrected pixel 的浮点精度；实际 capability/driver support、GPU resource budget 和每通道 coarse/fine 驻留上限均**尚未测得**，不得据此分配生产 VRAM 预算。

## 需要用户批准的事项 / advisory

1. **绑定/依赖决策：** 当前 PyQt5 绑定不能以 Qt-native wrapper 提交 draw。若要继续，需要用户选择并批准可用 Qt binding/发行版，或明确批准一个替代 OpenGL 调用层及其生命周期/部署影响；G0 不自行安装依赖或采用 `ctypes`。
2. **重新执行次序：** 该选择后先重新跑 G0 的 shader、pixel、resource-release 和 desktop gates；不能将此报告当作 G1 起点。
3. **真实数据测量：** 仅在 shader 路径建立之后，用只读且明确指定的现有演示数据路径进行冷/热读/解码/上传分段；当前没有这项测量结果。

**G0 已停止；生产界面未接管；G1 未启动；未提交、未 push。**
