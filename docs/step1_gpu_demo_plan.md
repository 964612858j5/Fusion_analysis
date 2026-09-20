# Step1 七天 GPU 演示任务计划

状态：用户批准 GPU 优先方向及本文档编制；生产实施未启动。每块必须获得用户单独的执行指令。块间不得交叉实施，复核不得扩写验收标准。

基线：2026-09-20，分支 `v15-interactive-channel-workspace`，HEAD `5fc5a9b`。C4.5c 三笔已撤销。演示窗口为连续七天，D1 是用户启动 G0 的当天；G4 功能冻结后预留一天。

前置阅读：根目录 `AGENTS.md`、`docs/P0_SCOPE_RULES.md`、`UI_SURFACE_RULES.md`。本文仅替代本次演示有关的后续 Intensity/显示性能实施顺序，不重写已完成的 A/B/C、Save、相机和科学契约。旧 `step1_rework_plan.md` 保留历史记录。

## 1. 目标与诚实边界

- 在演示数据及批准的通道规模上，消除新增通道信号由中心向外出现；新通道使用完整粗层出现，再逐渐细化。
- GPU 实时执行全局 Min/Max/Gamma、颜色、权重与 Overlay/Fusion 显示合成，CPU C1 实现只作正确性参考与既有回退路径。
- 平移/缩放/patch/Tissue 空降使用正确来源的粗层承接，精层按需更新。
- 保持一个 Step1 相机，保留当前 UI、科学状态、ROI 来源和 Save/handoff 契约。
- 60 Hz / 16.7 ms 为热缓存目标，必须真机测量，不提前宣称达到 Odon 性能。
- 未读取过的数据无法零等待。完整粗层尚未就绪时的首次通道准备必须测量和如实报告；不得伪造通道信号、把旧状态冒充新状态或新增未经批准的提示控件。若无法在已批准交互内处理冷准备，停止提报产品裁定。

Odon 参考版本：本地 `/sda1/Fusion/analysis_pipline/odon`，`b01faef010b14ea03c33e92a14e438061d257e39`。重点是 `src/render/tiles_gl.rs` 的逐通道粗细层和 GPU 合成、`src/app.rs` 的多层承接及有界请求。只研究原理，不复制/翻译/嵌入其源码。

历史证据：`docs/benchmarks/2026-08-30_tonsil_g1_probe_v4_pertile_offscreen.md` 支持固定世界坐标瓦片；其 10.67 ms 为离屏时间桶成本，不是实屏 FPS。`docs/benchmarks/2026-08-31_57ch_multichannel_prefetch.md` 的 I/O 占比不能直接视为当前 Step1 瓶颈。

## 2. 统一范围与产品契约

### 允许的结构

既有 Step1 来源/provider/raw cache → 有效原始通道纹理（完整粗层和视口精层）→ GPU 逐通道映射与粗细层替换 → 既有公式合成 → 既有 Step1 Viewer 区域。

不做整视口 CPU 拼图、不等待全部高清瓦片再切换、不建设 Rust/image service，不新建事件总线、repository、通用全局模型。不得把 Qt 的 OpenGL viewport 开关当成已经实现 GPU shader 合成。

### 不可变契约

- 使用 Step1 最终决断及 B 来源规则；ROI 外透明，校正产物缺失拒绝该通道，既有提示保留。
- 校正浮点像素保留必要精度，不能照搬 Odon R16 无条件量化；粗层按既有网格和有效样本归约，透明掩码不能拉低边缘亮度。
- 共享 ChannelDisplayState 的全局 Intensity，手动值优先；不得按瓦片或视口重新求窗。
- Overlay/Fusion 对齐 C1，包括 gamma、权重、组内运算顺序、组间 max、nucleus 和零权。GPU 误差在 G0/G1 明确，禁止靠扩大容差掩盖公式变化。
- 首次启用默认 1.0、显式 0.0、异质组权重、勾选/当前通道作用域、draft/committed、颜色和 session 规则不变。
- 相机、分栏、Tissue Preview 视框、patch 响应及 Save 后进入 Step1 保持已验收行为。

### 全程禁止直接修改

`viewer/explore_view.py`、`viewer/scheduler.py`、`viewer/caches.py`、`viewer/raw_tile_provider.py`、`viewer/step1_source.py`、`ui/step0/**`、`core/**`、`workers/**`、共享 Channels/Intensity UI、Step2/3、Step5、Nexus、生产分割/融合写盘算子和 conftest 写保护。

发现必须修改上述任一项，先解释原因、具体函数、用户后果、风险和回退，再等待用户批准。通过测试不能替代这项批准。

## 3. 任务块与起止点

### G0 — GPU 接入可行性与测量基线（D1，0.5–1 天）

**起点：** 用户单独启动；核对 HEAD/status、保护文件清单；无前序实施依赖。

**工作：** 只读检查 Qt/GL 上下文、现有 ViewBox 变换与绘制入口；在独立测试窗口验证纹理采样、逐通道粗细层、最小 Overlay 和参数更新。用现有来源接口测冷/热读取、合成、上传、实屏呈现耗时。固定演示机、数据集、通道数、分辨率和冷热场景。确认 GPU 上下文创建/销毁方式与后续 G1 的准确接入函数。

**文件白名单：** 新增 `scripts/benchmark_step1_gpu_demo.py`、`tests/test_step1_gpu_probe.py`；本文及 `docs/benchmarks/step1_gpu_demo/` 下本任务报告。原型不得被生产 MainWindow 导入。

**退出门：** 真实 GL 上下文显示至少两个通道；参数拖动只更新显示参数且可见；有像素对照和分段耗时。报告保留现有相机所需的具体适配、GL 能力、纹理格式、资源预算。未能保留现有相机/绘制入口时，不得自行换 UI 或共享 viewer，提报裁定。

**终点：** 交付原型与 G1 精确接口选择，停止。失败当天停止，不连续数日扩改共享底层。

**回滚：** 撤销本块独立提交或仅移除本块新增文件；不恢复/覆盖用户文件。

### G1 — Step1 专用 GPU 显示层与公式（D2，约 1 天）

**起点：** G0 通过且其具体 GL 适配选择获用户批准；用户单独启动 G1。

**工作：** 实现 Step1 专用 GPU layer。允许其内部建立有字节上限的原始纹理缓存及必要离屏目标；复用现有源身份与世代，不新建一套权威状态或通用调度器。每个通道独立选粗/精层，再按 C1 公式合成。CPU C1 为参考。此块仍仅测试可达。

**文件白名单：** 新增 `ui/step1_gpu_layer.py`、`ui/shaders/step1_gpu.vert`、`ui/shaders/step1_gpu.frag`、`tests/test_step1_gpu_layer.py`；G0 脚本、本文和本任务报告。若 G0 表明须不同文件布局，必须先修订白名单并获准。

**退出门：** 真实 framebuffer readback 覆盖 Overlay/Fusion、Min/Max/Gamma、零权、异质组、组间 max、nucleus、浮点源、ROI 掩码；默认目标最终 RGBA 与 CPU 参考逐分量差 ≤1 LSB，关键零值/透明和分组逻辑严格正确，不能事后放宽。热纹理改参数零原始数据上传、零读取；粗精重叠不双计数，一通道变清晰不抹去其他通道。预算与关闭释放有真实证据。

**终点：** GPU 层独立可验证，生产界面仍不接入；停止审核。

**回滚：** 本块独立提交 revert，恢复 G0；不影响旧 CPU 显示。

### G2 — 完整粗层与既有瓦片供给适配（D3，约 1 天）

**起点：** G1 通过，用户单独启动。

**工作：** 使用现有 B provider、来源身份、raw cache、scheduler 的公开能力向 GPU 供给数据。允许新增一个 Step1 局部适配器；它不另建 scheduler/raw cache。完整粗层按来源版本准备并在批准预算内驻留；前台优先、后台低清准备有界。优先复用完整有效来源的已有低清数据，必须核对来源一致性，不能误用 Step0 预览图。精层逐通道覆盖粗层。raw 已缓存时复用。相机运动期间以有界频率提交当前视口需求，空降立即请求，不能只等 gesture_quiet。

**文件白名单：** 新增 `ui/step1_gpu_binding.py`、`tests/test_step1_gpu_sources.py`；G1 GPU 层、G0 脚本、本文及本任务报告。读取现有接口；任何现有共享接口缺口先提报，不私自改底层。

**退出门：** 延迟外围精瓦片时，已准备通道仍覆盖完整有效视口；中心仅清晰化而非首次出现信号。冷准备耗时独立报告。平移/跳转粗层承接、跨层全局同窗、ROI 边界与来源正确；重复通道/参数更新无重复读取；纹理淘汰后正常恢复。正常快速切片/ROI 更新和关闭不接受旧来源结果。限定测试正常异步乱序，不升级为同进程攻击防御。

**终点：** 独立测试载体满足供给和视觉门；停止。不得接管 MainWindow。

**回滚：** 撤销 G2，保留 G1 独立验证状态。

### G3 — 接入现有 Step1 产品路径（D4，约 1 天）

**起点：** G2 通过，用户单独启动。

**工作：** 只在现有 Step1 mount/生命周期接入 GPU 层，使用现有 draft spec、共享映射、相机、Viewer tab、patch 和 Tissue 导航。接管时停止旧 CPU 合成显示消费，避免双重工作。保留独立回退路径；GPU 初始化失败时诚实报告原因，不悄悄降级后声称 GPU 成功。不得新增用户可见开关/面板。

**文件白名单：** `ui/step1_viewer_mount.py`（渲染后端构建、激活、关闭、显示绑定）；`ui/step1_gpu_binding.py`、`ui/step1_gpu_layer.py`；`tests/test_step1_gpu_takeover.py`、`tests/test_step1_viewer_mount.py`。`ui/step1_viewer_takeover.py` 仅在 G0 明确批准的 widget 挂载适配需要时可改。本文及本任务报告。`ui/main_window.py` 不在默认白名单。

**退出门：** 真实公开产品手势验证：Overlay/Fusion、通道勾选/名称/权重、共享 Intensity、平移/缩放、patch、空降、Step0↔Step1 位置同步、视框、离开/返回和关闭。原有科学/Save 行为不变。真实 GPU 像素到屏，不拿假 controller 调用次数冒充。用户当日真机验收是进入 G4 的条件。

**终点：** 第一次生产可见版本，停止等待用户验收；不顺手修其他步骤。

**回滚：** 本块独立 revert 恢复原 CPU mount；不删 CPU 实现或恢复整仓历史。

### G4 — 演示性能收敛、彩排与冻结（D5–D6，1–2 天）

**起点：** G3 真机通过，用户单独启动。

**工作：** 用固定演示数据、通道规模和相同轨迹，测冷/热通道、Intensity 连拖、平移缩放、空降、步骤往返、内存压力。只调本任务已批准的纹理/上传/粗层准备预算；不引入新后端、新安全机制或共享调度重构。必要时同机同数据与 Odon 比较；格式/像素源不等价须说明，不能给伪等价 FPS 对比。

**文件白名单：** G0 benchmark、GPU layer/binding/shader 的预算与本块复现缺陷修复、GPU 专项测试、本文和本任务报告。其他文件须新批准。

**退出门：** 热缓存目标 60 Hz，报告实际 p50/p95/最大帧间隔及输入到显示延迟，不能仅以平均 FPS 验收。记录冷准备和首次全幅出现、精层到达时间、读取/上传/合成分段、峰值 RAM/VRAM；显存上限在调优前固定，超限不偷偷上调。用户确认无中心信号扩散、可接受粗到细、拖动跟手。完成针对变更的 Step0/相机/Tissue/Save/科学状态保护回归；既有失败按相同运行方式对照，不为追求全绿修改无关测试。

**终点：** D6 冻结演示提交、依赖/启动方式、实测数据、限制及回退提交；D7 只作彩排缓冲。未达标明确失败项，由用户裁定缩小演示场景或延后，不能自行改公式、精度、ROI、勾选语义。

**回滚：** 调优逐笔可撤销；保留 G3 已验收状态。新机制要求直接退回范围审核。

## 4. 每块共同交付要求

1. 起始 HEAD/status、文件白名单、受保护文件记录；仅本块自己的临时进程可停止。
2. 实际修改文件/函数与授权对应，公开复现手势、测试与真机证据分别报告。
3. 不把测试内部攻击升级为 blocker。变异仅验证批准的产品契约，所有探针对合成数据无害。
4. 新问题分 blocker/advisory，复核意见不直接触发下一轮越界开发；遵守两轮停止门。
5. 每块独立提交/回退需按用户提交授权执行；本次文档编制不授权提交或 push。未授权不暂存、不提交、不 push。
6. 块结束必须停止，不因剩余时间、回归在跑或后续块“很小”而提前实施。

## 5. 当前状态

- G0：初次执行发现 PyQt5 `QOpenGLFunctions` Python binding 的**接入障碍**（不是性能瓶颈）；原历史证据保留于 `docs/benchmarks/step1_gpu_demo/2026-09-20_g0_blocked_report.md`。
- G0.1：已于 2026-09-20 获单独授权并完成。Qt-owned context 上的隔离 PyOpenGL `3.1.10` probe 在 RTX 4090 上完成两通道 R32F texture、shader/FBO readback、C1 `≤1 LSB` 比较、参数零原始重读/零 reupload、逐通道 fine/coarse、ViewBox sibling overlay 可行性和 context-current cleanup；真实只读 Tonsil level-3 crop 也完成 C1 `0 LSB` 对照。desktop Qt window 已 exposed，但 compositor/vsync/input-to-display、生产 tile 供给/预算未测；详见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g0_1_report.md`。G0.1 不授权 G1。
- G1：已于 2026-09-20 获单独授权并完成独立验证。新增仅测试可达的 `Step1GpuLayer`（Qt-owned context + fixed PyOpenGL 3.1.10），以 one-source-per-pass multi-pass 实现 Overlay/Fusion；在 RTX 4090 context 中通过 C1 `compose()` Overlay/Fusion `≤1 LSB` / alpha exact、per-channel coarse/fine、identity-only bounded LRU、hot display zero raw upload、current-context GL cleanup 和 ViewBox sibling adapter tests。它没有生产 mount、provider/scheduler/cache 或 UI 接线；详见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g1_gpu_layer_report.md`。G1 完成不授权 G2。
- G2：已于 2026-09-20 获单独授权并完成独立验证，状态为**实现完成、仅测试可达、等待审核**。新增 `Step1GpuBinding` 仅通过注入的既有公开 `Step1TileProvider`、`TileScheduler`、controller snapshot/signals 和 caller-built display/viewport snapshots，生成 immutable G1 `SourceDescriptor`；它不成为 production mount，也不拥有 scheduler/raw cache/source/camera/display authority。验证了每通道完整粗层原子发布、current viewport 完整精层事务、跨通道 coarse/fine 保留、source/revision/epoch 迟到拒绝、missing corrected 拒绝、ROI/校正来源规则、display-only 零供给请求和强制硬件 FBO。真实只读 Tonsil 单个最粗层瓦片的供给测量见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g2_report.md` 及 raw JSON；它不是 FPS、呈现延迟或完整生产性能。G2 完成不授权 G3/G4。
- G2.1：**收口完成、仍仅测试可达、等待复审；G3/G4 未开始。** 审核提出的两个 blocker 已在 `ui/step1_gpu_binding.py` 与 `tests/test_step1_gpu_sources.py` 内处理：(1) 完整粗层公平性——只用既有 `TileScheduler` 公开的 priority 最小堆/同层 FIFO/`cancel_generation` 语义，在仍有粗层事务未完成时把**此后新发出**的精层请求排到粗层之后（`0 / 100 / 200` 三层），粗层一完成即恢复；`viewer/scheduler.py` 零改动，无新增 scheduler/线程池/队列/仲裁器，不读取 scheduler 私有状态。真实 `TileScheduler`+`Step1TileProvider` 下，连续 24 次 PAN 中 B 的 9 块粗层全部先于任何精层被读取，未完成条目有界，`NAVIGATOR_JUMP` 仍立即请求目标。(2) 真实 RTX framebuffer 中间帧——中央粗层先到、外围后到，前 8 块交付后 framebuffer 与 A-only 基线逐像素一致，第 9 块到达后 B 一次覆盖全部有效像素；局部 fine 只改变对应区域，A 与 B 其余粗层不受影响，ROI 外 alpha 为 0、valid-dark 仍是不透明暗像素。证据见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g2_report.md` 的 G2.1 节与 `2026-09-20_g2_1_gates.json`。G2.1 完成不授权 G3/G4，也不代表 G2 正式关闭。
- G3/G4：未开始。
- G0/G0.1/G1 仅提出待后续批准的接口建议：未来 `Step1GpuLayer` 以既有 ViewBox 的只读 viewport snapshot、既有 source/display snapshots 工作，拥有局部 GL resources/有界纹理 cache；不拥有相机、provider、scheduler、raw cache、共享 UI 或新的权威状态。PyOpenGL 3.1.10 已仅按 G1 授权固定于 `fusion_test2` 演示环境；它不等同生产依赖/生产接入，任何下一块仍须用户单独批准。
- 本文新增不代表 CPU 回退基线已被接管。
- `AGENTS.md` 与 `P0_SCOPE_RULES.md` 为本项目持久规则；未写入平台级记忆、未修改其他项目。
