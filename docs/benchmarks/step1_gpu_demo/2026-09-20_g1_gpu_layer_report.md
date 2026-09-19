# Step1 G1：专用 GPU 显示层与 Overlay/Fusion 公式验证

**日期：** 2026-09-20
**范围：** G1 仅实现独立、测试可达的 Step1 GPU layer。没有生产 mount、provider、scheduler、cache、ViewBox、MainWindow 或共享 UI 接线。
**结论：** 在 `fusion_test2` 的 Qt-owned RTX 4090 context 中，G1 multi-pass `GL_R32F` GPU layer 已通过 C1 Overlay/Fusion FBO readback 验证。它仍不是生产渲染路径，也不代表交互吞吐、真实呈现时间或 G2 准备完成。

## 起始 / 结束状态与保护

| 项目 | 值 |
| --- | --- |
| 起始 HEAD / 分支 | `5fc5a9bd9d05732144373dca22f3818866b01e49` / `v15-interactive-channel-workspace` |
| 结束 HEAD / 分支 | `5fc5a9bd9d05732144373dca22f3818866b01e49` / `v15-interactive-channel-workspace` |
| 预存工作区内容 | `cufile.log`、`docs/tma_study_mode.md` 已修改；AGENTS/P0/GPU 任务文档和其他任务文件未跟踪 |
| 本次修改 | `ui/step1_gpu_layer.py`、`ui/shaders/step1_gpu.vert`、`ui/shaders/step1_gpu.frag`、`tests/test_step1_gpu_layer.py`、`docs/step1_gpu_demo_plan.md` 和本目录 G1 证据 |
| 明确未改动 | `ui/main_window.py`、mount/takeover/composition modules、`viewer/**`、provider/scheduler/cache/source、Step0、shared UI、`core/**`、workers、`conftest.py` |
| Git | 未暂存、未提交、未 push |

## 固定依赖与实际 context

G1 前，目标解释器 `/root/micromamba/envs/fusion_test2/bin/python` 没有 PyOpenGL。按授权执行的唯一安装命令是：

```bash
/root/micromamba/envs/fusion_test2/bin/python -m pip install \
  --disable-pip-version-check --no-input --no-deps \
  --index-url https://pypi.org/simple PyOpenGL==3.1.10
```

安装后：

```text
PyOpenGL 3.1.10
/root/micromamba/envs/fusion_test2/lib/python3.10/site-packages/OpenGL/__init__.py
```

没有安装 `PyOpenGL_accelerate`，没有升级/替换 PyQt、Qt、NumPy、pyqtgraph、CUDA、驱动或其他包，也没有修改 requirements、锁文件、`setup_env.sh` 或生产启动脚本。

实际强制 GPU context：

| 项目 | 实测 |
| --- | --- |
| GL_VENDOR | NVIDIA Corporation |
| GL_RENDERER | NVIDIA GeForce RTX 4090/PCIe/SSE2 |
| GL_VERSION | 3.3.0 NVIDIA 535.309.01 |
| GLSL | 3.30 NVIDIA via Cg compiler |
| software renderer | false |
| `GL_MAX_TEXTURE_IMAGE_UNITS` | 32 |
| raw format | `GL_R32F / GL_RED / GL_FLOAT` |
| transient targets | `GL_RGBA32F`，final `RGBA8` |

## 层所有权与接口

`Step1GpuLayer` 是 `QOpenGLWidget`，Qt 保持唯一窗口/context/event lifecycle；PyOpenGL 仅在该 widget 的 Qt context current 时创建/上传/绘制/readback/delete GL names。模块 import 本身不创建窗口、context、线程或文件，也不 import C1 composer、MainWindow、provider、scheduler 或 domain/display-state owner。

公开边界是：

```text
attach(view_adapter)
submit(source_descriptor, display_snapshot, viewport_snapshot)
readback_rgba_for_test()
dispose()
```

- `source_descriptor` 仅接收调用方给出的 immutable raw plane identity、float payload、valid mask、world rect、channel coarse/fine 与 selected level。
- `display_snapshot` 仅接收调用方给出的 mode、C1 mappings、colors/weights 或 groups/group weights/nucleus。
- `viewport_snapshot` 仅接收 caller-captured world range、logical size、DPR 与可选 repaint callback。
- layer 不读取磁盘、推断/改变 source generation、保存 session、查询 FusionDomainModel/ChannelDisplayState、拥有相机，或成为新的权威状态源。

测试中的 `attach` 仅将 layer 在**首次 context 创建前**作为 `ExploreView.graphics.viewport()` child。它透明接收鼠标、观察既有 range/resize，不调用 `setRange` 或改变 existing ViewBox input/camera owner。该 attachment 没有导入到任何生产 mount。

## GPU 公式与像素门

唯一 CPU oracle 是未修改的：

```python
block01.viewer.step1_compose.compose(...)
```

GPU layer 内没有 import 或调用该 oracle，也没有 CPU final-RGBA fallback。每个 readback 只在测试中单次 `np.flipud` 后与 `compose()` expected RGBA 对比。

### Overlay

- 每 source pass 只绑定一个 raw sampler；没有固定 29/57 sampler array。
- raw pass 按 C1 执行 window/clip/neutral brightness+contrast/gamma，且 `gamma <= 0` 采用 C1 等价的 `max(gamma, 1e-6)`；valid mask 被编码为 raw `NaN` 以保持 valid-dark 与 invalid/transparent 的离散语义。
- Overlay pass 使用 `RGB add`、coverage `GL_MAX`，final pass 执行 clip 与严格 alpha `{0,1}`。
- 覆盖 float corrected-like、uint8/raw-like、gamma above/below/non-positive、weights `0/1/fractional/>1`、同色叠加/clip、missing mapping、absent plane、NaN/Inf、ROI mask 和 valid-dark。

### Fusion

- group 内先按 clipped channel weight 求和，再乘 clipped group weight、clip，group 间用 `GL_MAX`；nucleus 单独处理为蓝色。
- 覆盖两个异质组（max 与 sum 可区分）、nucleus、zero channel/group/nucleus、missing source/mapping、重复 group channel、ROI alpha。
- 零 weight 与缺 mapping source 在 cache/upload/pass planning 前排除；显式 zero draft 不会被 layer 改写。

原始像素结果：

| case | 最大分量误差 | >1 LSB components | alpha mismatch |
| --- | ---: | ---: | ---: |
| Overlay | 1 LSB | 0 | 0 |
| Fusion | 1 LSB | 0 | 0 |

详见 [pixel raw JSON](2026-09-20_g1_pixel_results.json)。RGB `≤1 LSB` 是既定验收；alpha 为严格精确。没有改变 CPU oracle 或放宽门槛。

## coarse/fine、geometry 和 texture cache

每 channel 的 selected level 是独立的。选 `fine` 时，先画该 channel 的 coarse，再画 fine；有效 fine 覆盖只替换同 channel 的 coarse signal。不同 channel 的 coarse/fine 绝不互相擦除或相加，同 channel coarse/fine 也不双计数。专门测试了 partial A-fine/B-coarse，fine 区只改变 A，而 B coarse 在全视口仍贡献颜色/alpha；另有反向组合验证。

layer-local cache 仅以 caller-provided raw plane identity 为 key；display mode/mapping/color/weight/group/nucleus/viewport 不进入 key。它不保留 NumPy payload，不发明 dataset authority/generation，并以构造参数注入 byte cap。

G1 raw cache 运行证据（64×64，cap `32768` bytes）：

| 场景 | uploads | hits | evictions | CPU submit | wall incl. readback |
| --- | ---: | ---: | ---: | ---: | ---: |
| cold upload | 2 | 0 | 0 | 3.225 ms | 4.757 ms |
| hot display-only update | 2 | 2 | 0 | 0.256 ms | 0.397 ms |
| new identities / eviction | 4 | 2 | 2 | 0.795 ms | 1.020 ms |
| evicted source re-upload | 6 | 2 | 4 | 0.409 ms | 0.616 ms |

峰值为 2 textures / `32768` bytes。若当前 active source working set 本身大于 budget，submission 在 upload 前 fail closed；不会删除当前 draw source、无界增长或读取 provider。`dispose()` 后 raw textures `0`、transient targets `0`、cache bytes `0`、threads `0`。

完整 cache/raw timing 证据：[G1 raw JSON](2026-09-20_g1_gpu_layer_raw.json)。这些是 CPU submission 或 blocking FBO readback，**不是** GPU completion、FPS、compositor/vsync 或实际呈现延迟。

## 专项验证

从 `/tmp` 使用强制 GPU mode：

```bash
BLOCK01_REQUIRE_STEP1_GPU=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  /root/micromamba/envs/fusion_test2/bin/python -m pytest -q -p no:cacheprovider \
  /sda1/Fusion/analysis_pipline/block01_v14/tests/test_step1_gpu_layer.py
```

结果：

```text
cold: 7 passed in 3.24s
warm: 7 passed in 3.46s
```

在 `BLOCK01_REQUIRE_STEP1_GPU=1` 下，PyOpenGL 缺失、context failure、shader/FBO failure 或 software renderer 都会失败而非 skip。普通未设置该 flag 的环境只作 module skip，不使用 CPU/其他后端替代。

G0.1 regression 也在安装后的 `fusion_test2` 环境中重新执行，desktop probe 成功，并产生 [regression JSON](2026-09-20_g0_1_regression_fusion_test2.json)。没有全仓回归。

## cufile.log

每个从 `/tmp` 启动的 install-check、G1 cold/warm tests、G0.1 regression 和 raw probe 都记录：

```text
60650 → 60650 lines, delta 0
```

没有截断、恢复、清理或修改受保护日志系统。

## 未测、blocker 与 advisory

没有 G1 blocker。以下是明确未测/未实施项，不代表它们通过：

- production provider/scheduler/raw cache source supply、完整 tile atlas 与后台预热（G2）；
- production mount/takeover、Step0/MainWindow/shared UI/patch/Tissue integration（G3）；
- input-to-display/compositor/vsync、真机交互性能、60 Hz 或生产 VRAM budget（G4/人工验收）；
- 为保留 C1 的 arbitrary-channel one-source-per-pass 策略有线性 pass 成本；G1 只验证正确性，未把它宣称为生产性能方案。

## G2 最小接口建议（仅建议，未实现）

G2 若单独获批，应只向该 layer 提供已有 owner 的 immutable plane descriptor：existing source identity/generation、channel、coarse/fine level、world rect、raw float payload、valid mask 和 selected level；同时提供 display/viewport snapshots。G2 不应让 layer 建立 provider、scheduler、raw cache authority 或新的 source-state system。需要的任何现有接口变化必须届时单独审查，不由本 G1 预实现。

**G1 已停止；GPU layer 仍仅测试可达；生产界面未接管；G2/G3/G4 未启动；未提交、未 push。**
