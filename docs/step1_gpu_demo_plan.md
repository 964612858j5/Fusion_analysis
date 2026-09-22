# Step1 七天 GPU 演示任务计划

状态：G3/G3.1 的首次真机验收未通过，随后 G3.2a 与 G3.2b 的主要修复已逐块实施和验收；G3.2b.4F 的 Step0 Save→Step1 入口体验于 2026-09-22 通过真机验收。用户已授权提交已验收的检查点并进入 G3.2c（ROI 精确裁切）；G3.2d（收口）与 G4 尚未开始。G3.2b.5A 的 Patch 晃动诊断线暂停、缺陷未宣称修复；性能 advisory 见对应报告。每块仍须单独执行、验收和停止，复核不得扩写验收标准。

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

### G3.2 — G3 真机失败后的封闭修复块（预计 3–5 天）

**起点：** G3/G3.1 自动化证据成立，但用户真机明确拒绝其交互画质与来源重绑响应。G3 尚未提交，不能以“自动化通过”替代真机失败；G4 的性能调优也不能先于这些公开产品路径 blocker。

**真机失败事实与已核实原因：**

1. 平移、缩放、patch 与 Tissue 空降会整屏退回模糊 coarse，停止后才清晰，部分倍率长期保持马赛克。`Step1GpuBinding._begin_fine_epoch()` 每次视口 epoch 都取消旧 fine、清空全部已发布 fine，并等待当前通道的整屏 fine 事务完整才发布；普通视口还可能超过每通道 16 MiB fine 上限而被永久拒绝。
2. Step0 将 CD22 保存为 cuCIM 后，返回 Step1 出现长时间卡顿、窗口至少两次无响应，且校正信号延迟约 2–3 秒。公开路径在 GUI 线程构造来源身份并打开校正产物元数据；来源移动后又在 GUI 线程 teardown stack，而 `TileScheduler.shutdown()` 会 join 正在执行的读取。新校正通道还须等待完整 ROI 的 coarse 归约后才首次出现。
3. ROI 边界外、但与 ROI 共处一个 coarse 单元的区域会泄出 DAPI。现有粗层按外扩网格保留“部分落在 ROI 内”的采样单元，GPU 最终输出没有再按 level-0 `roi_bbox_fullres` 精确裁切。

以上三项分别处理，不得用一次共享 viewer/scheduler 重构包办。任何发现必须修改统一禁止清单中的共享文件，先停止并取得用户批准。

#### G3.2a — 交互清晰度与多分辨率承接（先执行，0.5–1.5 天）

**工作：** 只修 GPU binding 的 coarse/fine 生命周期。新通道的完整 coarse 继续原子发布，守住“无中央扩散”；相机移动不得清空仍可覆盖当前视口的既有 fine。重叠 fine 立即复用，新露出的区域由最近可用的正确来源层承接并只请求缺失 fine；到达的 fine 在完整 coarse 背景上替换对应区域，不要求整屏 fine 全到才恢复清晰。普通演示视口不得因固定 16 MiB 单通道门而永久停在 coarse，但总 GPU raw texture 上限仍固定 512 MiB，不得静默提高。

**文件白名单：** `ui/step1_gpu_binding.py`；`ui/step1_viewer_mount.py` 仅限修正其传给 binding 的 fine 预算，不得改接管/生命周期；`tests/test_step1_gpu_sources.py`、`tests/test_step1_gpu_takeover.py`、本文及 `docs/benchmarks/step1_gpu_demo/` 下 G3.2a 报告。`ui/step1_gpu_layer.py` 默认不改；如果真实证据证明既有 `ChannelSource.coarse + fine` 无法按正确层级承接，必须先用通俗语言报告准确接口缺口并等待用户追加授权。

**明确禁止：** 不修改 `viewer/explore_view.py`、`viewer/scheduler.py`、provider、raw cache、Step0、来源/科学公式、Intensity UI；不新增 scheduler、线程池、事件总线、第二套缓存权威或通用 LOD 框架。不处理 cuCIM 重绑和 ROI 裁切。

**退出门：**

- 连续平移和跨金字塔层缩放时，已覆盖区域不会整屏退回全片最粗图；已有 fine 不因新 epoch 无条件消失。
- patch 与 Tissue 空降走真实公开入口；目标画面没有长期 coarse/马赛克，目标 fine 能到达。
- 构造超过旧 16 MiB 门的正常视口，最终仍到达目标层；若真实 512 MiB 总工作集无法容纳，须明确 fail closed，不能用降精度或增预算掩盖。
- 新启用通道仍在完整 coarse 到齐后一次覆盖有效区域；任何 partial coarse 到达时 framebuffer 与旧画面一致，中央扩散不得复发。
- Overlay/Fusion、Intensity、权重和零权规则、ROI alpha、来源世代、离开/返回及关闭保持现有门。
- 真机单独验收平移、缩放、patch、空降和新通道一次出现；未通过即停止，不进入 G3.2b。

**回滚：** 仅撤销 G3.2a 对 binding/测试/报告的改动，恢复 G3.1 未提交树；不动 G1/G2/G3 其他文件和受保护文件。

#### G3.2b — cuCIM 来源重绑不卡 GUI（G3.2a 通过后，1.5–2.5 天）

**工作：** 先在真实公开轨迹分段测量来源身份检查、旧 scheduler teardown/join、新 stack 构建、corrected coarse 读取/归约、GPU 上传和首次像素到屏；再只修占主导的已测瓶颈。目标是 Step0 Save 与进入 Step1 不冻结 GUI，新校正通道优先获得当前视口的完整有效画面，旧来源结果仍严格拒收。

**默认白名单：** `ui/step1_viewer_binding.py`、`ui/step1_viewer_host.py`、`ui/step1_viewer_mount.py`、`ui/step1_gpu_binding.py`，对应 Step1 专项测试、本文和本块报告。不得预设新 worker、退休队列、provider 热交换或另一套生命周期；计时证据确实要求其中一种机制时，先说明必要性、修改函数、用户后果、风险和回滚，获得用户追加授权后才能实施。

**禁止：** 不修改 Step0 Save、校正算子、Zarr 写盘格式、共享 scheduler/provider 实现，不以显示 raw 冒充迟到的 cuCIM，不降低来源身份或迟到结果门。

**退出门：** 真实 Step0 CD22→cuCIM→Save→Step1 轨迹中 GUI 事件持续响应；来源切换全程不显示旧 CD22/raw；当前视口校正信号到达时间和各分段耗时有真实记录；ROI/数据集快速变化、关闭和 CPU fallback 不泄漏线程/句柄。

#### G3.2c — ROI bbox 的 GPU 精确裁切（G3.2b 通过后，约 0.5 天）

**工作：** 将既有 level-0 `roi_bbox_fullres` 作为不可变显示参数交给 GPU 最终输出，按世界坐标裁切 Overlay/Fusion 的所有通道，包括 DAPI。粗层可以继续使用既有有效样本归约，但 coarse 单元跨过 bbox 的部分不得上屏。ROI 外 alpha 必须为 0；不采用“全片显示 DAPI、其他通道 ROI-only”的退让方案。

**文件白名单：** `ui/step1_gpu_layer.py`、必要的 Step1 GPU snapshot/mount 薄接线、GPU 专项测试、本文和本块报告。不得修改 `viewer/step1_source.py`、Step0 ROI、科学来源规则或 CPU fallback 像素公式。

**边界：** 本块只承诺现有产品契约中的矩形 `roi_bbox_fullres`。任意多边形精确裁切不在本块范围，若提出须重新裁定。

**退出门：** 在粗层、fine、二者混合、Overlay 与 Fusion 下，bbox 内边缘像素保持原值，bbox 外 DAPI 和 marker 均透明；相邻 tile、跨 level、平移缩放后边界不漂移。

#### G3.2d — G3 最终收口（前三块真机通过后，0.5–1 天）

**工作：** 重跑 G3 真机清单与针对性自动化；确认 GPU/CPU fallback、Step0↔Step1 相机、Tissue 视框、Save/handoff、科学状态和关闭生命周期。记录冷/热限制与 G4 advisory。只按白名单提交 G0–G3/G3.2 成果；`cufile.log`、`docs/tma_study_mode.md` 及其他窗口文件不得纳入。

**终点：** 用户明确确认 G3 真机通过后，G3 才能关闭；之后是否启动 G4仍需新指令。

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
- G3/G3.1：**自动化接管完成，但用户真机验收失败，未提交、不得关闭。** 已确认通过的是：新通道完整 coarse 一次出现、Overlay/Fusion、实时 Intensity、权重/零权、Step0↔Step1 相机、ROI/Save 基本轨迹和关闭；失败项是平移/缩放/patch/空降时整屏 coarse 模糊与部分倍率长期马赛克、Step0 发布新 cuCIM 后来源重绑导致 GUI 无响应及校正信号迟到、粗层 ROI 单元在 bbox 外泄出 DAPI。G3.1 已修复零权 Fusion 通道误触发 Intensity 求窗，直接复用 `viewer.step1_compose.overlay_channels/fusion_channels`。上述真机失败按 G3.2a–d 封闭处理，不能直接进入 G4。
- G3.2：计划已于 2026-09-20 获用户批准写入本文；当前只授权编制 G3.2a 执行提示词，实施以用户将提示词交给执行窗口为准。G3.2b/c/d 不因写入计划自动获得实施授权。
- G3.2a：**自动化退出门完成，等待用户真机验收；G3.2b/c/d 与 G4 未开始。** 只改 `Step1GpuBinding` 的 fine 生命周期与 mount 传给它的 fine 预算常量。相机移动不再清空已发布 fine：仍覆盖新视口且来源身份未变的 plane 原样保留并立即重提，只请求真正缺失的瓦片，每块 fine 一到就替换自己的世界坐标，不等整屏事务；保留有界（离开视口/来源变更即丢，其余按目标层优先、层级最近次之裁到单通道 fine 预算）；plane 按粗到细提交，细层最后覆盖，跨层缩放由手上最近的一层承接——全部由既有 `ChannelSource.coarse + fine` 顺序绘制完成，`ui/step1_gpu_layer.py` 未改。完整粗层原子发布与 G2.1 公平策略未动。实测演示数据集 tile 512（1 MiB/块）：完整 coarse 1 MiB/通道，目标层可见字节 1080p 12 MiB、1440p 15 MiB、4K 分栏 30 MiB、4K 全屏 40 MiB，故单通道 fine 上限由 16 MiB 提到 48 MiB；八通道 392 MiB 仍在固定 512 MiB 内，总上限未提高、超限仍 fail closed。22 次真实 PAN/ZOOM：旧行为保留 0、重取 324 块，修复后每步保留 16–26 块、重取 0 块，coarse 背景始终完整、无透明洞、无通道丢失；旧 16 MiB 门在实测 2182×1382 视口（目标层 40 块 = 40 MiB）上把通道永久钉在全片 coarse，修复后到达目标层且与 C1 差 ≤1 LSB、纹理峰值 60 MiB；patch 与 Tissue 空降立即复用 16 块、新请求 0；新通道中央扩散未复发。强制 GPU `69 passed / 0 skipped`，十个保护模块 `302 passed`。详见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g3_2a_report.md` 与 `2026-09-20_g3_2a_gates.json`。G3.2a.1 复核收口已完成：目标层预算先预留（承接旧层只用剩余额度，目标层自身放不下才 fail closed）、取消勾选即时释放该通道 fine 并取消其在途请求（活动集合只记一个小元组，颜色/权重/Intensity 仍零读取零请求）、修正一条恒真的 alpha 断言、补一个目标与已有 fine 完全不重叠的远距离空降门。实测相机不动时取消勾选：CD8 fine 瓦片 8 → 0、两通道 fine 字节 4,194,304 → 2,097,152，CD3 不受牵连；远距离空降：新请求 4 块且与旧瓦片零重叠、旧 plane 驻留 0、等待期间 coarse 背景 262,144 不透明像素无洞、只放行一块即有 1 块变清晰、最终到达目标层且与 C1 差 ≤1 LSB。收口后强制 GPU `75 passed / 0 skipped`、十个保护模块 `302 passed`，原五项变异闸门仍全红，另两项收口变异也转红。G3.2a.2 复核收口再修一个活动通道切换 blocker：活动集合为空时也记录（取消最后一个通道后原地重新勾选即自动恢复目标层，无需移动相机），并把 fine 规划拆成按通道进行——仅移除时只释放/取消被移除通道、不重规划幸存通道、不取消其在途请求；新增通道及其 coarse 落地后也只启动该通道自己的 fine。新增两条公开路径门与第 8、9 项变异闸门，九项变异全红；收口后强制 GPU `78 passed / 0 skipped`、十个保护模块 `302 passed`，三路场景证据数字不变。**G3.2a 真机仍未通过；G3.2a.3 完成拖动时机修复与剩余问题诊断，等待复核和真机。** G3.2a.3 唯一的生产改动是 `_interaction_event()` 的 motion timer 启动条件：复用 `viewer/explore_view.py` 既有的 THROTTLE-not-debounce 原理，已在运行的 33 ms timer 不再被每个事件重启，`NAVIGATOR_JUMP` 仍立即处理；未改共享 controller、未新增 timer/线程/coalescer/缓存/调度器。真实 Qt 事件循环驱动的 1.9 s 连续拖动：手势期间 GPU submit 由 `6` 增至 `108`、使用过的不同相机中心由 `1` 增至 `93`、最后一次 submit 由停在起点 `518` 变为跟到 `1622`，provider 读取增量不变（`7`）；热区来回拖动 1 s 内 140 次 submit、读取与上传增量均为 `0`；同一手势内三次标注过的强制取帧得到三张不同图像。其余真机问题本轮只诊断：patch A→B→A 与通道取消/重新启用均为 `0` 读盘、`0` 纹理上传（重复的是请求与 GPU 重绘，不是读盘或 cuCIM）；连续远距离空降在热缓存下未复现，生产 I/O 宽度（8 worker）下第二次比第一次慢约 `64 ms`，机制是已开始的读取无法取消；首次进入/永久模糊在合成台架未复现；校正归约成本因台架无 corrected product 而完全未测（属 G3.2b）。演示数据集只读实测冷读 `74.65 ms`（最粗层）/ `22.36 ms`（level 0）。详见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g3_2a_3_report.md` 与 `2026-09-20_g3_2a_3_interaction.json`。**G3.2a.3 持续拖动真机通过；G3.2a.4 连续空降、冷区域自动清晰、首次进入真机通过；G3.2a.5 完成热缓存 patch 原子恢复，等待复核和真机验收。G3.2b/c/d、G4 未开始。** G3.2a.5 唯一的生产改动是 binding 对『同一 GUI 线程、同一调用栈内同步返回的 scheduler cache hit』的接收方式：`_request()` 增加一个只属于调用方栈帧的局部 collector，仅当仍在 `request()` 调用内且当前线程就是 GUI 线程时才收集，其余一律保持既有 queued signal；`_plan_fine_for_channel()` 收齐后统一应用、由调用方发布一次。无新增缓存/timer/线程池/队列/coalescer/scheduler/状态机，无跨事件循环 pending 状态，未重新引入 C4.5c 的 waiting epoch。实测返回已缓存 patch：provider 读取 `0`、纹理上传 `0` 不变，GPU submit 由 `5` 降为 `1`，`show_patch()` 返回瞬间在屏目标瓦片由 **0/4** 变为 **4/4**，第一次自然 Qt paint 由 `0/4` 不完整变为 **完整**，coarse-only/partial 的自然 paint 由 `1` 变为 `0`，最终帧与离开前逐像素一致。冷 patch 仍由 worker 经 queued signal 逐块到达（探针确认每次 mutation 都在 GUI 线程）；混合视口的已缓存部分一次恢复、冷部分随后补齐。强制 GPU `88 passed / 0 skipped`，六个保护模块 `152 passed`，新增三条门，变异第 13–16 项转红。详见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g3_2a_5_report.md` 与 `2026-09-20_g3_2a_5_patch_return.json`。本轮只消除热缓存返回路径的视觉回放，不解决冷读、校正归约、新通道 coarse/fine 串行、G3.2b 来源重绑或 G3.2c ROI 边界。 G3.2a.4 唯一的生产改动是 jump/quiet 与既有 33 ms motion timer 的配合：`NAVIGATOR_JUMP` 先停掉仍在运行的 motion timer 再立即消费（遗留 timer 不会再规划同一落点），`_flush_motion()` 只在最新 snapshot 与上一次已规划的不同（比较 controller 既有的 `(source, epoch)`）时才规划。33 ms 间隔、throttle 语义、controller、scheduler、provider、cache、GPU layer、Tissue Preview、shared camera、coarse 原子发布与 coarse/fine 预算全部未动。实测：运动中空降由被规划 **3 次**（epoch 轨迹 `[4,7,7,7]`、7 次请求）降为 **1 次**（`[4,7]`、4 次请求）；两次相隔 15 ms 的远距离空降都被接收、第二次相机立即生效、各规划 1 次、68.7 ms 内在**没有任何额外输入**下 4/4 目标瓦片上屏、最终 descriptor 与像素属于第二落点、第一落点迟到结果被拒；patch A→B→A 返回 A 为 **0 读盘、0 纹理上传**；首次进入静置 2 秒在合成台架 9/9 目标瓦片上屏、未复现整幅模糊（真机差异在金字塔深度、冷读成本与 corrected product，本轮不据此扩改）。强制 GPU `85 passed / 0 skipped`，六个保护模块 `152 passed`，新增四条门，变异第 11、12 项转红。详见 `docs/benchmarks/step1_gpu_demo/2026-09-20_g3_2a_4_report.md` 与 `2026-09-20_g3_2a_4_landings.json`。
- **G3.2a：已于 2026-09-20 正式通过用户真机验收。** 逐项确认：G3.2a.3 连续拖动实时跟随（含未访问区域）与 Tissue Preview 视框丝滑；G3.2a.4 连续快速远距离空降、冷区域无需额外手势自动清晰、首次进入 Step1 自行清晰；G3.2a.5 热缓存 patch A→B→A 一次完整恢复。剩余的公开产品问题有两条：**（1）首次冷 patch / 首次启用通道的准备仍然太慢**；**（2）Step0 发布新的 cuCIM 结果后返回 Step1 出现长时间卡顿、Block01 无响应、校正信号延迟 2–3 秒**。
- G3.2b：已获用户授权，处理上述两条（首次冷准备时间 + cuCIM 来源重绑卡顿）。只允许改 `ui/step1_viewer_binding.py`、`ui/step1_viewer_host.py`、`ui/step1_viewer_mount.py`、`ui/step1_gpu_binding.py` 及对应测试/报告；先测量后最小修复，触及 scheduler/provider/corrected reduction/新 worker/退休队列等一律停止并申请追加授权。
- G3.2b：**测量完成，轨迹 A 已实施最小修复，轨迹 B 触发停止条件、未实施、等待用户裁定。**
  实测瓶颈：(A) 一个通道的当前视口 fine 只在它的完整 coarse 事务落地后才被请求——五次重复中
  `fine_requested_before_coarse_landed` 全为 false，串行的 fine 阶段中位数 `63.90 ms`；已按授权方向 3 在
  `Step1GpuBinding` 内调整时机（coarse 在途即规划当前视口 fine，仍以 G2.1 的 `200` 优先级排在 coarse 之后；
  首次出现的通道等齐 coarse 与视口 fine 才进 descriptor），中位数 channel-sharp `123.22 → 110.31 ms`（-10.5 %），
  coarse 之后的 fine 阶段 `63.90 → 0.03 ms`，新通道不再先糊后清两段式出现。诚实边界：coarse 阶段本身
  `59.18 → 110.15 ms`（与 fine 争 I/O），净收益约 10 %，台架读取远快于演示数据集。
  (B) cuCIM 来源重绑的主导 GUI 冻结是 `TileScheduler.shutdown()` 在 GUI 线程 join 在途读取：`sync_source()`
  GUI 线程耗时 `205.98 ms`，其中 teardown+join `116.98 ms`，事件循环最长 `227.3 ms` 无响应；`source_moved()`
  仅 `0.18 ms`、新 stack 建立 `33.42 ms`，原先「元数据打开慢」的假设被否定。消除它需要旧 scheduler 后台退休
  或改 `viewer/scheduler.py`，属停止条件 #1/#2/#5，**未实现**，最小方案见报告 §1 等待授权。
  未测缺口：台架无 corrected product，cuCIM/tophat 归约成本完全未测。强制 GPU `88 passed / 0 skipped`，
  六个保护模块 `152 passed`，变异第 17–21 项转红。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-20_g3_2b_report.md`。
  G3.2b.1 复核收口：首次通道的放行条件由「没有 fine 计划在途」改为真实数据条件
  （完整 coarse 就绪且当前视口全部目标层 RawKey 都已在 `_published_fine`），修正了
  fine 超预算或瓦片失败时会泄出 coarse 的 blocker；新增两条 fail-closed 门，变异第 22 项转红。
  强制 GPU `90 passed / 0 skipped`。
  G3.2b.2（用户单独授权）：用户真机反馈「每个 patch 第一次打开比 Tissue 空降慢很多」。只读核实确认
  `ui/main_window.py::_select_preview_patch()` 的**首次访问分支**是唯一没有 `_step1_whole_slide_active()`
  守卫的 patch 路径，GPU viewer 接管期间仍会为隐藏的旧渲染器构建 `PreviewLoaderThread`，以 downsample=1
  读取该 patch ROI 的全部所需通道，与 viewer 抢同一批 I/O。加上同一个既有守卫后，实测首次点击构建的
  loader 线程由 **1 降为 0**，相机锚点不变，Tissue 空降不变，回到旧渲染器后仍照常加载。新增
  `test_a_first_patch_click_starts_no_loader_behind_the_slide_viewer`，变异第 23 项转红；
  `test_step1_viewer_takeover.py` `20 passed`、其余保护套件 `149 passed`、强制 GPU `74 passed`。
  台架 loader 为替身，未测真机毫秒差，实际提速待用户真机确认。**不得记为 G3.2b 真机通过。**
- G3.2b.2 真机确认（2026-09-21）：删除首次 patch 背后的旧 `PreviewLoaderThread` 后，功能验收通过，但
  **首次 patch 与 Tissue 冷空降仍然都很慢**，说明那条隐藏的 loader 不是主瓶颈。该修复保留——它消除的是
  确定无用的隐藏读取；真实的冷准备性能移交 **G3.2b.4**。
- G3.2b.3（2026-09-21，已完成，等待复核与真机）：只修 Tissue Preview popup 里那行成功元数据
  （`Full image … | Overview …`）占据一整行布局、导致该区域无法点击空降的问题。`OverviewPanel.status`
  是面板布局里的真实 QLabel 而非画布覆盖，所以那一行的点击到不了 `gview.viewport()`。修复只在
  `TissueNavigatorPopup` 内对**它自己那一个实例**的 status 标签加显示策略：成功元数据隐藏并释放布局高度，
  `Loading` / `Loading overview` / `failed` / ROI-patch warning / 操作提示一律仍然显示，状态往返双向生效。
  `OverviewPanel` 的类、`_on_overview_loaded()`、文本生成与坐标/鼠标/ROI/patch 语义零改动，Step0 页面的
  状态行照常可见。实测：画布高度 `403 → 420`（+17 px，正好是让出的那一行），原状态行中心映射进 viewport
  局部坐标 `(201, 412)`，在该点发真实 `QTest.mouseClick` 得到 `navigate_requested(57810, 19680)` 恰好一次。
  新增六条门，变异第 24–27 项转红；Tissue/overview/相机保护套件共 `360 passed`，
  `test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full` 为**既有失败**（还原 popup 到
  HEAD 后同样失败），未修改。
  G3.2b.3.1 收口（真机验收失败后）：隐藏成功元数据行之后，它下面的 `_info_lbl` 会顶上来，且文本为空时仍占
  18 px，所以那条带仍然不是画布。第一轮的门之所以错误通过，是因为它只点了旧状态行中心、且没有挂载工具栏与
  ROI/Patch 列表。修复把 popup 的局部策略扩展为"管这个 popup 重复显示的两行"：挂载自己的 ROI/Patch 列表时
  隐藏面板的 `_info_lbl` 并释放高度，裸 popup 与 Step0 页面均保留。实测画布 viewport `261 → 282`，画布与
  列表之间不再有任何纯文本行（剩下的是面板自己的 patch 控制行，属控件，按 UI_SURFACE_RULES 保留）。
  新增三条门：挂列表时 info 行消失且同一 popup 还原后画布变矮、真实层级下画布最底部内侧 1 px 与 2 px 的真实
  点击各产生恰好一次落在切片底部的 `navigate_requested`、Step0 面板两行都不受影响；变异第 28、29 项转红。
  收口后 Tissue/overview/相机保护套件 `364 passed`。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_3_report.md`。
  G3.2b.3.2（2026-09-22，已实施，等待真机验收）：**真机确认 G3.2b.3.1 的底部点击已通过** ——
  Tissue Preview 最底部现在可以正常点击空降。**但同一轮验收发现 G3.2b.3 自己埋下的问题**：
  鼠标在画布上停留片刻仍会弹出 `Full image … | Overview …`。
  **准确来源：** `_apply_overview_status_policy()` 里那一行
  `panel.gview.setToolTip(label.text() if metadata else "")` —— 即 G3.2b.3 报告 §1 写的
  「成功元数据顺手挂到 tooltip 上供诊断，不新增任何可见 UI」。**那个判断是错的：**
  tooltip 就是一层会自己浮出来、盖住用户正要瞄准的组织的 UI。用户明确要求**彻底去掉，不是搬走**。
  **最小改动（只改那两行）：** 无条件清空，并且**连 viewport 一起清** —— Qt 给 `QGraphicsView`
  显示的 tooltip 取自指针实际悬停的那个 widget，只清 view 会在接收 hover 的 viewport 上留下旧值。
  **没有**把文字搬到 status tip / what's-this / 弹窗或任何别的入口，**没有**改共享 `OverviewPanel`，
  **没有**新增状态、事件过滤器、timer、缓存或组件，**没有**碰 Loading / failed / warning 的可见行为。
  **最终值：** 成功元数据写入后 `status` 仍隐藏、`gview.toolTip()` 与 `gview.viewport().toolTip()`
  均为 `""` 且不含 `Full image` / `Overview`；Loading → 成功 → failed → 成功 四步往返每一步两者都是 `""`，
  可见性各自照旧。底部点击无回归（1 px 与 2 px 真实 `QTest.mouseClick` 各恰好一次 `navigate_requested`，
  落点 `y > 0.8 × full_h`）。Step0 自己的 `OverviewPanel` 成功行仍可见、tooltip 与调用前逐项相同。
  新增**四条门**（含一条静态门：popup 内 `setToolTip` 只允许空串，且禁止 `setStatusTip` /
  `setWhatsThis`）；**三项变异全红**（tooltip 挂回 gview、改挂 viewport、成功行恢复可见），
  每次按 SHA-256 逐字节还原。回归：popup 32 / tissue_preview_contract 53 / full_image_jump 22 /
  overview_middle_drag_pan 101 / shared_camera 15 / step0_tissue_preview 25；
  `test_step1_gpu_takeover.py` 的 tissue/navigation/landing 六条在 `BLOCK01_REQUIRE_STEP1_GPU=1` 下
  **6 passed**（不设该变量时按既有硬件门 skip）。
  白名单外零改动：`ui/step0/overview_panel.py`、`ui/step0/**`、`viewer/**`、`ui/main_window.py`、
  Step1 GPU layer/binding/mount、shared camera、scheduler/cache/provider、科学算子、`conftest.py`、
  依赖与环境文件 SHA-256 全部未变。`cufile.log` 零增长。
  **不宣称真机通过** —— 等待用户确认「长时间悬停不再出现提示框」。详见同一份报告 §8c。
- G3.2b.5A（Step1 画新 Patch 时主 Viewer 短暂晃动，**用户已授权**）：
  **只读测量完成，晃动未在本环境复现，按 §八 未实施任何生产修复；已上报 blocker 等待追加授权。**
  **复现方式（真实公开路径）：** 真实 `MainWindow()` + 真实演示切片 `biopsy.ome.tif`（只读）
  使 Step1 whole-slide mount 起在**真实 GPU backend**（`backend = gpu`，真实 `ExploreStack`，
  `[GPU] cupy 13.3.0 ready`，**未用 CPU fallback 冒充**）→ 进入 Step1 页面与 Viewer tab
  （两步都要，否则 viewer 子树停在未映射的 640×480 且一次都不 paint —— 第一版测量就栽在这里，
  已如实记录）→ 打开共享 Tissue Preview → 热稳态静置 3.5 s → 在 popup 画布上发**真实
  `QMouseEvent`** 按下 / 6 段移动 / 释放。信号链完整走通：`patches_changed` → Step0 单一几何模型
  → geometry persist → `geometry_committed` → `_on_step0_geometry_committed` → `_on_patches`
  → `_rebuild_patch_buttons`（按钮 0 → 1）。**从未直接调用 `_on_patches`。**
  **实测结果：** 全部 14 次真实 paint 的相机逐次相同 —— 中心位移 **0.000000 屏幕像素**、
  scale 比值 **1.000000000000**；导航入口**零调用**（`show_patch` / `jump_to_point` /
  `host.jump_to` / `controller.jump_to` 全为 0）；splitter 恒 `[259, 1325]`、graphics viewport
  恒 `1323×737`、viewer tab 与 Step1 page 的 geometry/sizeHint/minimumSizeHint 在 58 个快照中
  逐项不变。**另做用例 B 直击最强候选**：把 Patch 选择栏从 **1 个按钮长到 8 个**
  （8 轮完整 `_on_patches` + `_rebuild_patch_buttons`、16 次 layout request），
  相机与几何**仍然一项未动**。
  **十项候选逐项结论：** 1/2/6/10 **排除**（导航与 range 变化全为 0）；3/4/5/8 **测量后未见影响**
  （`live_orphan_buttons` 恒 0、size hint 不变、连续 8 次 commit 无位移）；9 **部分排除**
  （session autosave 确实发生，相机未动；legacy loader 未单独插桩）；
  **7 未能测量**（本 rig 的 mount 不暴露独立 GPU widget，且未做 framebuffer 抓取）。
  **本环境够不着的三处（如实列出，不当作"没问题"）：**（a）**没有像素证据**，退出门 C 未尝试；
  （b）**Tissue Preview 与 Viewer 不是同一张切片** —— rig 的 `_Loader` 是 512×256 合成切片，
  而 mount 开的是真实 19480×21804 切片，画出的 Patch 坐标不在 Viewer 坐标系里，
  **这是最可能让复现落空的一点**；（c）用户真机的窗口尺寸、DPI 与合成器与 offscreen 不同。
  **两处 §五 允许范围内的控制点：** geometry persist 的**完成点**由测试驱动
  （本 rig 无可写项目，worker 不会自行发布；其下游全是产品自己的链）；
  `step0_output["step0_manifest_path"]` 作为测试数据绑定（不绑定则产品按设计直接 return，
  整条链不会走，那次比较本身仍在执行）。
  **未执行变异闸门** —— §十二 的前提是"若实施了修复"，本块未实施。
  **Blocker：** 要抓到这个晃动，需要追加授权其一：让 Tissue Preview 与 Viewer 用同一张真实切片；
  或补 framebuffer / GPU oracle 比对；或在用户真机上做一次一次性诊断采样。
  **Advisory：**（1）`_rebuild_patch_buttons()` 目前对每次提交都全删全建
  （`removeWidget` + `deleteLater`），即使列表逐项相同 —— 本测量下无可见影响，
  但若将来复现成功，这里是第一处该看的地方，**本块未改**；
  （2）未绑定 handoff 的会话里画 Patch，Step1 的 Patch 栏根本不会更新（产品的正确行为，仅记录）。
  **生产代码零改动**：`ui/main_window.py`、`ui/step1_viewer_mount.py`、`ui/step1_gpu_binding.py`、
  `ui/step1_gpu_layer.py`、`ui/step1_viewer_host.py`、`ui/widgets/tissue_navigator_popup.py`、
  `ui/step0/step0_page.py` SHA-256 全部逐字节未变；测试文件一个都没改；`cufile.log` 零增长。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_5a_report.md`。
  **未提交、未 push；不宣称真机通过，也不宣称问题已修复。**
- G3.2b.5A.1（同一真实切片的只读复诊，**用户已授权**）：
  **台架缺口已补齐，晃动仍未复现；仍是只读，未实施任何生产修复。**
  **两端切片身份一致（跑前硬断言，不一致就停）：** Step0 页面、其 overview 面板与 Step1 mount
  全部用**同一个真实 `OMETIFFLoader`** —— `loader.filepath` = `page.ome_path` =
  `/sda1/Fusion/benchmark/biopsy.ome.tif`；`loader.shape` = overview `full_h×full_w` =
  Viewer `level_shape(0)` = **19480×21804**；overview `ds = 32`、网格 **609×682（真实缩略图已加载）**；
  窗口 `1600×1000`、DPI `1.0`、backend **`gpu`**；Step1 页面与 Viewer tab 均真正显示（viewport `1323×737`）。
  **一次险些误报的假阳性，如实记录：** 修好切片身份后的**第一次**运行确实测到释放瞬间
  `d = (−402.389, −359.500)` 屏幕像素、中心从 `(10902, 9740)` 跳到 `(0, 0)`，并伴随
  `jump_to_point/host.jump_to/controller.jump_to` 各 +1。**但那不是画 Patch 的路径** ——
  当时缩略图还没加载（`overview_grid = 0×0`，status `Overview load failed: "Channel '' not found"`），
  拖拽在空网格上塌缩，走进了 overview 面板「太小 → 当成点击 → `_emit_navigate()`」的兜底分支。
  **我差一点把它当成根因报上来。** 修法：补真实 nucleus 通道、等到缩略图真有像素，
  并加硬断言「这次拖拽必须真的新增 1 个 Patch」，否则拒绝当作对画 Patch 路径的证据。
  **有效复现的结果：** `PATCHES ADDED BY THE DRAG: 1` 前提下，**全部 16 次自然 paint 的相机完全一致**
  （最大中心位移 **0.000000000 屏幕像素**，scale 最大相对偏差 **0.000e+00**）；
  导航入口**零调用**；splitter 恒 `[259, 1325]`、viewport 恒 `1323×737`、tab 与 page 几何逐项不变；
  选择栏 **0 → 8 个按钮**（8 轮 `_on_patches` + `_rebuild_patch_buttons`、16 次 layout request）
  仍一项未动 —— `_rebuild_patch_buttons()` 的全删全建在本环境**未造成**任何短暂布局变化。
  **像素证据及其限制：** `QWidget.grab()` 取手势前后各一帧（`1323×737`），SHA-256 **完全相同**；
  但这是**强制取帧**（`grab()` 强制立刻重绘），**不能**据此断言中间自然帧没有晃动 ——
  **中间自然帧的像素本块没取到，这仍是缺口。**
  **四种结论中的落点：** 相机真的移动 → **否**；widget 几何变化 → **否**；
  相机与几何稳定但像素晃动 → **未能判定**；**仍未复现 → 是（本环境）**。
  **与真机尚不一致的条件：**（1）中间自然帧像素未取；（2）offscreen 无合成器，
  没有真实 present/vsync 路径，GPU widget 也没有独立于 graphics viewport 的表面；
  （3）窗口尺寸与 DPI 不同；（4）geometry persist 完成点仍由测试驱动（下游全是产品自己的链）；
  （5）未绑定真实项目/handoff，manifest 路径是测试数据。
  **最小修复位置（仅建议，未实施）：** 未复现即无可指认根因。若真机再现，按序看：
  ① 缩略图未就绪时拖拽会走 `_emit_navigate()`（位置在 `ui/step0/overview_panel.py` 的
  patch-mode 释放分支，**该文件只读且不在白名单，需另行授权**；一行级回滚＝恢复该分支原样）；
  ② `MainWindow._rebuild_patch_buttons()` 的全删全建；③ 真机一次性采样。
  **Advisory：** 在缩略图尚未就绪的 overview 上拖拽不会创建 Patch 而会**导航** ——
  用户若在 Tissue Preview 刚打开时就去画 Patch，看到的「Viewer 跳一下」可能正是这条路径；
  **本块未验证这是否就是用户的场景，仅记录。**
  生产代码零改动（`ui/main_window.py`、`ui/step1_viewer_mount.py`、
  `ui/widgets/tissue_navigator_popup.py`、`ui/step0/step0_page.py`、`ui/step0/overview_panel.py`
  SHA-256 逐字节未变）；真实切片只读，未写任何项目/manifest/handoff/ROI/Patch；`cufile.log` 零增长。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_5a_report.md` 附录。
  **未提交、未 push；不宣称真机通过，也不宣称问题已修复。**
- G3.2b.5A.3（从代码调用链定位画 Patch 瞬变，**用户已授权**）：
  **只读诊断。找到一个能解释录像像素特征的真实机制，但本台架没有证明画 Patch 会触发它。**
  **先撤回两条我此前的错误：**（1）5A 报告说「本 rig 的 mount 不暴露独立 GPU widget」——
  **错的**，沿 mount 实际走下去 `Step1GpuLayer` 存在且可见（`attr:gpu_layer` 与 `findChildren`
  都找到，1323×737），真实 OpenGL 为 **NVIDIA GeForce RTX 4090 / 4.6.0 NVIDIA 535.309.01**
  （驱动自报字符串，不是 `backend="gpu"` 也不是 cupy 横幅）；（2）第一次运行**所有钩子一条未触发**，
  因为台架自建 `sys.modules["block01"]` 别名，而我把钩子装在 `block01_v14` 包的**另一个同名类对象**上；
  改为先加载台架再从同一别名包取类后才生效，报告现记录 `hooked_package` 以防再次静默。
  **自动手势成功：** 三次真实拖拽各新增 1 个 Patch，链路完整
  （`_add_patch → patches_changed → geometry persist → geometry-only commit published
  rev 2/4/6 → [Step1] adopted → _on_patches → _rebuild_patch_buttons`）。
  **持久化缺口如实记：** 台架未绑真实项目，persist 的**完成点由脚本驱动**（下游全是产品自己的链），
  「写盘」那一段未测；也因此**未在真实项目中增删任何 Patch**。
  **submit 与 paintGL 的第一处差异（两种窗口尺寸下均复现）：**
  `submit#4 @611×450 → FBO 611×450` → `resizeGL 611×450` → `resizeGL 1323×737`（widget 变大、
  FBO 未变）→ **`paintGL widget 1323×737 / FBO 611×450`，`blit_is_rescaled = True`** →
  `submit#5 @1323×737` 重建 FBO → 下一次 paintGL 恢复。生产代码里是明写的：
  `resizeGL()` **故意空实现**（FBO 尺寸只跟随 submit），而 `paintGL()` 用**当前 widget 尺寸**做
  blit 目标、用**上一次 submit 的 `_target_size`** 做源，`GL_NEAREST`。
  这条路径与 5A.2 录到的像素特征逐条吻合（整幅变化、结构不移位、均值保持、锐度不降、两三帧后逐位复原）。
  **但它没有发生在画 Patch 时（本块最重要的否定结论）：** 三次新建 Patch 的调用顺序在
  1600×1000 与**用户真机的 1400×900** 下完全一致 ——
  `_add_patch → _on_patches → _rebuild_patch_buttons → LayoutRequest×3`，
  **没有 qt.Resize、没有 resizeGL、没有 submit、没有 paintGL**。
  `_rebuild_patch_buttons()` 的全删全建只引发 LayoutRequest，GL widget 既未重新布局也未重绘。
  **按四分类落在「所有已记录状态相同，缺口在其他绘制阶段」**：画 Patch 期间台架里每一项记录状态
  都没变，而真机录像里同样手势确实产生了一次可见重绘 —— **两者在「画 Patch 是否让 GL 层重绘」
  这一步就分岔了**。
  **事实 vs 假设：** 事实是真实 GPU、`Step1GpuLayer` 存在、`resizeGL` 空实现、
  **paintGL 会用旧尺寸 FBO 拉伸绘制（实测两次）**、画 Patch 在台架不触发任何绘制状态变化；
  **仍是假设**的是真机那次 67–100 ms 瞬变是否由该 FBO 尺寸不匹配路径造成 —— **本块未证明**。
  **最小修复建议（不实施，且不建议现在改）：** 若日后证实，范围是
  `ui/step1_gpu_layer.py::paintGL()` 一处 —— 当 `_target_size` 与 `(w*dpr, h*dpr)` 不一致时
  不要把旧 FBO 拉伸铺满；影响面只限「尺寸已变而新 submit 未到」的窗口期，不动相机/Patch/导航；
  回滚即恢复该 blit 的目标矩形；验证方式是真实路径下 resize viewer 后、下一次 submit 前断言无拉伸帧。
  **剩余缺口只剩一条：** 查清**真机上画 Patch 为什么会让 Viewer 重绘而台架不会**
  （候选：真机已有 5 个 Patch 且 Viewer 可能绘制覆盖物、Tissue Preview 为独立窗口的遮挡/焦点交互、
  真机绑定真实项目走完整持久化与 session 保存）。**建议下一轮只查这一步，不要再扩观测面。**
  生产文件 SHA-256 逐字节未变；`cufile.log` 与 `docs/tma_study_mode.md` 起止相同；
  真实切片只读；只结束了本次自己启动并记录 PID 的临时进程。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_5a_report.md` 附三。
  **未提交、未 push；诊断脚本跑完不等于问题已修复。**
- **G3.2b.5A 线（Step1 画 Patch 时主 Viewer 短暂晃动）：2026-09-22 规划窗口裁定
  「诊断线暂停，缺陷保持未解决」。**
  真机录像已证明异常存在（15 s 无损录制，四次新建 Patch 各有一次 ~67–100 ms 的全幅
  瞬变后逐位复原）；台架**始终未复现**。
  最后一轮（5A.6）完整临时项目测试**有效**：在**同一次手势**内完成了 Patch 新增并进入
  权威 `_roi_model`、真实 worker 返回 `published` 并写出 `patch_config.rev1.*.json`、
  `geometry_committed` 自然触发且 MainWindow 采纳（`_on_patches` 被调用）、
  `_save_step1_session` 真的保存 —— 而手势窗口内 **GPU submit / paintGL / resizeGL /
  GL widget qt.Resize / mount 的来源重绑入口全部为 0**（重绑入口挂了钩子，一次未调用；
  未拿 `.zattrs` 被写当证据）。
  **结论：不能把几何写盘或来源重绑认定为晃动原因。**
  **本线暂停：不再追加台架机制，不凭猜测修改 GPU layer 或 Patch 按钮栏。**
  录像、逐帧分析与全部调用记录保留在
  `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_5a_report.md` 及同目录 JSON，
  待出现**能区分真机与台架的新证据**时再处理。
  **独立留档（非本缺陷的修复方向）：** `ui/step1_gpu_layer.py` 的 `resizeGL()` 空实现 +
  `paintGL()` 以当前 widget 尺寸 blit 上一次 submit 的 FBO，构成一个**已证实、可复现的
  拉伸窗口期**；**与本次晃动的相关性未证明**，且「不拉伸」不是完整方案（过渡帧该显示什么
  需另行确定）。
  **生产代码未因本问题改动；未提交、未 push。**
- **G3.2b.5B（Step1 Patch 选择器支持 P8 以上）：已实现，用户真机验收通过（2026-09-22）。**
  先复现：改动前按钮条每个 patch 固定 42+4 px，20 个 patch 时条宽 916 px，
  1000 px 窗口下最后一个按钮右缘 1005 px **已在窗口之外**；同批测量中 Viewer viewport
  不随 patch 数变化。**如实限定：** `QTest.mouseClick` 会把事件直接投给窗口外的按钮，
  所以该台架只能证明「按钮条无上界增长」，不能证明 P8 点不到或点得到。
  改动只在 `ui/main_window.py` 的 Step1 patch 选择器：新增 `STEP1_INLINE_PATCH_BUTTONS = 7`，
  标题 `Preview patch:` 换成固定高度、宽度只由 "Patch" 一词决定的 `QToolButton("Patch")` + `QMenu`
  （标题与下拉是同一个控件，
  **没有新增按钮**），菜单项按 `_all_patches` 全量重建、一律进入既有 `_select_preview_patch()`，
  勾选与 idle/loading/ready/error 标签由同一处写入，**没有第二份 Patch 模型、第二个选择权威
  或第二份状态表**。改动后条宽在 7/8/20 个 patch 时恒为 318 px。
  **下拉宽度收口（用户指出的缺口）：** 收口前菜单未设宽度，Qt 默认给 250 px（实测 20 项）；
  第一版 `setFixedWidth(318)` 被本块自己的门抓出是错的 —— 800×600 屏上 20 项菜单会被 Qt
  自动分栏（实测 `636×576`，P19/P20 在 `x=318`），硬设 318 px 会裁掉第二栏、
  **恰好让这两个 patch 点不到**。最终用 `setMinimumWidth(318)`：一栏放得下时正好 318 px，
  需要分栏时**每栏仍是 318 px**，所有菜单项都在弹出矩形内且完整落在屏幕内。
  新增 `tests/test_step1_patch_selector_menu.py` **32 门全绿**（含真实鼠标点击弹出菜单、
  真实点击选中 P8/P20 并带动相机、程序化重建不导航、7→20→7 视口与 splitter 不变、
  GPU 与 CPU 回退两路一致）；**变异 7/7 全红**
  （含「不设宽度下界」与「宽度写死成固定值」两条），每次按 SHA-256 逐字节还原。
  保护回归：geometry_sync 5、shared_camera 15、viewer_takeover 20、patch_responsiveness 4、
  camera_stability 6、viewer_mount 16、layout_block_a 23、display_isolation 21、
  navigator/dataset/handoff 四模块 36，全部 passed。
  `cufile.log` 零增长、`docs/tma_study_mode.md` 未变。
  **如实记录一次操作失误：** 为定位菜单分栏行为，我先在 pytest 之外直接实例化了 `MainWindow`，
  它按配置**自动打开了真实 OME-TIFF（只读，只发生读取，未写入任何文件）**；发现后立即删除该
  scratchpad 脚本，改在仓库测试内（合成 loader）完成同一测量。
  **Advisory：** `scripts/benchmark_step1_gpu_request_gate.py:456` 与
  `scripts/benchmark_step1_patch_vs_tissue.py:437` 直接索引 `_patch_sel_btns[arg]`，
  `arg ≥ 7` 时会越界；`scripts/` 不在本块白名单，未改动。
  **用户真机验收通过；未提交、未 push；未触碰 G3.2b.5A 的晃动问题。**
  用户反馈本次画 Patch 未出现短暂晃动；这是本次观察，不能推翻 G3.2b.5A 已录到的异常或宣布其根因已修复。
  **浏览性如实记录：** 菜单项高 32 px，20 项 640 px；屏幕放不下时 Qt 自动分栏，
  本块未加分页或滚动策略。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_5b_report.md`。
- **B1.2.3b（Full Image ↔ Compare 相机往返漂移）已通过用户真机验收**（2026-09-22）。
  同轮真机亦确认 **B1.2.3a（Step0 双方法后台准备）通过**。本块未顺带修改 B1.2.3b 的任何生产代码
  （`ui/step0/step0_page.py` SHA-256 与本块开始前逐字节相同）。
  **Advisory：** `ui/step0/step0_page.py` 中可能仍有描述旧退出语义的 docstring，属后续文档收口，
  **不在本块白名单内，未改。**
- G3.2b.4（真实冷准备性能）：**测量完成，未实施任何生产修复，触发停止条件 #1/#3，等待用户裁定。**
  首次在**真实演示级数据**上测量（真实 OME-TIFF `biopsy.ome.tif` 4 层金字塔 + Step0 真实写出的
  cuCIM/tophat 校正产物 + 生产 mount/host/stack/scheduler/controller/GPU layer/binding），入口只用公开
  `mount.show_patch()` 与 `mount.jump_to_point()`，演示项目全程只读（运行后该项目无任何文件被写）。
  **（1）冷 Patch 与冷 Tissue 确认共用同一条底层路径**——两者在 `host.jump_to()` 即合流到
  `controller.jump_to() → _move_camera()`，实测目标层、瓦片数、请求来源构成、读取/上传/submit 计数逐项同形。
  **（2）主导成本有两处，都不在白名单内。** 其一是**校正来源归约**（`viewer/step1_source.py::reduce_corrected`）：
  校正产物没有金字塔，一个 cuCIM 通道的完整粗层必须把整张 1.70 GB 的 level-0 数组按 stride 64 归约一遍，
  实测 **8 293.56 ms**，同片 raw 通道同位置只要 **0.03 ms**；四个 cuCIM 通道 **31 936 ms**——这才是
  「首次启用通道 / 首次进入准备过慢」的主因。其二是 **GPU 接管期间旧 `ExploreController` 仍然全速发请求**：
  `set_marker_visible(False)` 只改不透明度（其文档明写 "Visibility only"），`_issue_raw_requests()` 照常发出
  level+1 underlay（优先级 `0+i`）与 prefetch ring（`700+i`），每次冷空降多出 **20–24 个 binding 从未请求过的
  RawKey**、占单通道冷空降全部 provider 读取的 **62 %**，其像素因 opacity 0 永远上不了屏。
  **（3）白名单内没有可消除的实质浪费：** binding 一次冷空降是 `12 请求 → 12 唯一 RawKey → 12 次真实读取
  → 12 次上传`，重复真实读取字典在三种配置 × 26 次动作中**全部为空**；真热返回仍是 `0 读取 / 0 上传 /
  1 submit`。分段实测（中位）：单通道冷 Patch 目标层就绪 `239.2 ms`（corrected）/ `198.9 ms`（raw），
  冷 Tissue `294.3` / `208.3 ms`；四个 cuCIM 通道冷 Patch `425.4 ms`、冷 Tissue `557.2 ms`，GUI 最长无响应
  `288.8–398.2 ms`，binding 自己目标层瓦片的排队中位数由单通道 `10.8–33.8 ms` 涨到四通道 `96.7–219.5 ms`
  ——延后不是优先级策略问题而是**吞吐**问题。被否定的假设七条（含「两条路径不同」「瓶颈在上传/绘制」
  「存在重复读取」「删掉 G3.2b.2 的 loader 后已无隐藏读取」）。
  **最小追加授权方案三条（均未实现）：** A 校正粗层归约改等价 reshape 分块求和（已只读实测
  `4.51 s → 1.30 s`，`valid` 一致、`max|Δ|=7.63e-06`）；B `stride == 1` 的 corrected 瓦片不进归约直接切片
  （已只读实测每块 `7–15 ms → 1.2–2.7 ms`）；C GPU 接管期间停掉旧 controller 的不可见请求流（**未实测**，
  且必须改共用的 `viewer/explore_view.py`；现成的 `suspend_for_production()` **不可用**——它会锁相机、
  写可见状态文字并在 GUI 线程 join 线程，三条都违反已验收行为与 UI 规则）。
  本块**未改动任何生产文件**（两个白名单生产文件也未动），强制 GPU `90 passed / 0 skipped`、六个保护模块
  `161 passed`，与 G3.2b.1 基线逐数字一致；因未新增生产逻辑，**本块没有新增变异闸门**，前 29 项不受影响。
  `cufile.log` `60656 → 60674` 行（真实 GPU 校正栈首次在台架启动时 cuFile 自行追加，未截断/未恢复/未清理/
  未暂存），后续运行已改从 `/tmp` 启动。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4_report.md` 与 `2026-09-21_g3_2b_4_cold_landing.json`。
  **不得记为真机通过**——本块没有任何用户可见变化可以验收。
- G3.2b.4A（校正归约与 stride=1，**用户已授权**）：**已实施，等待复核与真机验收。**
  只改 `viewer/step1_source.py` 一个纯读取函数，消除 G3.2b.4 的第一号 blocker。
  **（1）`stride == 1` 快路径：** 一个 1×1 的格子就是那个像素，因此不再造 block index、不再
  `np.add.at`。先问 `region.overlaps()`——完全在 ROI 外时**零次 `region.read()`、不读盘**；有交集时只发
  一次读取，并按 `placed` 与**实际返回数组的形状**放回输出（`placed` 比请求交集小的防御情形不会凭空造
  像素）。返回 shape 与请求矩形完全一致，`float32`/`bool` 契约不变，数值 0 仍 `valid=True`，NaN/Inf 原样
  透传。矩形超过 `MAX_REDUCTION_ELEMENTS` 时退回有界步进，结果不变。
  **（2）`stride > 1` 有界向量化归约：** block index 用恒等式 `r // stride - y0 // stride`，网格仍锚定
  level-0 原点；游标按 block 边界步进，故一个轴上只有首尾两块可能非对齐；每个 slab 按**自己的 `placed`
  相位**补齐到整块（补 0，不进分母）后 `reshape(bh, stride, bw, stride).sum(axis=(1,3), dtype=float64)`；
  计数用 `rows_in[:,None] * cols_in[None,:]` 的外积，边块按**实际样本数**归一。`_slab_geometry()` 把预算
  花在 `(rows+stride)×(band+stride)` 而非 `rows×band`——否则超预算的会是补齐后的临时数组而产物侧的门
  永远看不见它。**`MAX_REDUCTION_ELEMENTS = 4 Mi` 未提高**，最大单次读取 3.94 M（stride 64）、最大临时
  数组 4 194 304 = 预算本身。
  **数值逐位相同：** 真实 1.70 GB CD22 cuCIM 产物完整粗层 `max|Δ| = 0.0`、**100 % 逐元素完全相等**、
  `valid` 一致；送进既有 C1 Overlay/Fusion 后 **RGBA 最大差 0 LSB、alpha 完全一致**（stride 64/4/1，
  四个真实 cuCIM 通道）。
  **性能（真实只读演示数据）：** CD22 完整粗层 viewer 墙钟 `8 293.56 → 2 108.87 ms`（**3.93×**）；
  四个 cuCIM 通道 `31 936.37 → 2 960.73 ms`（**10.79×**）；单线程 CPU 五次中位 `4 418.27 → 1 708.68 ms`
  （2.59×）、四通道合计 `17 550.9 → 5 872.8 ms`（2.99×）；stride=1 瓦片 12 块中位 `4.15 → 1.86 ms`
  （2.23×，已落到直接读取的 `1.2–2.7 ms` 区间）；冷 Tissue 空降目标层就绪 CD22 `294.3 → 142.7 ms`、
  四通道 `557.2 → 274.1 ms`；GUI 最长无响应四通道冷空降 `398.2 → 199.3 ms`。**最低倍数 2.03×，全部
  > 2×。** raw 通道（CD8）走另一条分支、一个字节未改，其完整粗层 `0.03 → 0.06 ms` 均为噪声量级，冷空降
  的下降属运行间波动，**不记作 raw 提速**。
  **request / read / upload / submit 计数一个都没变**（冷 Patch 12/32/12/13，四通道 48/68/48/49，
  热返回 0/0/1）——本块只改了一次读取里做多少算术。
  无新增 cache/scheduler/worker/后台任务/pyramid/sidecar/registry/状态机/GPU 算法/来源格式，未写回校正
  产物，未修改 Zarr/TIFF/handoff/manifest。新增 `tests/test_step1_corrected_reduction.py`（**74 条**，含
  逐像素独立参考算法，4 320 个参数化案例）、加强 `tests/test_step1_source_table.py`（`33 → 42`）、
  新增 `scripts/benchmark_step1_corrected_reduction.py`。强制 GPU `90 passed / 0 skipped`，七个保护模块
  `181 passed`。**八项变异闸门全红**（stride=1 退回归约、网格按 tile 锚定、边块除以 stride²、ROI 外判
  valid、忽略 slab 相位、单次读取超预算、数值 0 判无效、去掉后缘补齐），按 SHA-256 精确还原。
  `cufile.log` **本块零增长**（`60674 → 60674`，mtime 未变；全部运行从 `/tmp` 启动）。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4a_report.md` 与 `_4a_reduction.json` /
  `_4a_cold_landing.json` / `_4a_rgba_equivalence.json`。**不得记为真机通过**；真机需确认勾选 cuCIM 通道
  由约 8 秒降到约 2 秒、四通道由约 32 秒降到约 3 秒，且仍是一次完整清晰出现。
- G3.2b.4B1（Step0 当前通道的 TopHat + cuCIM 后台预计算，**用户已授权**）：**已实施，等待复核与真机验收。**
  只读核实的修复前事实：`MultiChannelPrefetchController(` 全仓**只有** `ui/step0/compare_strip.py` 一个构造点，
  `Step0ExploreTab` 从未挂载过它；而且即使挂上也不够——`prefetch_policy.hot_order()` 第一行就是
  `seen = {center}`，**按设计排除当前通道**。这在 Compare 是对的（三栏 Original/TopHat/cuCIM 前台已经在准备
  当前通道，实测中心 `CD3 tophat 4/4` + `cucim 4/4`、邻居 `CD20` 两法各 `4/4`，`hot_tiles_requested = 8`），
  在 **Full Image 是错的**：它只有一个面板，一次只显示一种方法，另一种没有任何人准备——实测
  `show_source("CD3","tophat")` 静置后 `CD3 tophat 8/8`、**`CD3 cucim 0/8`**。
  **修复（复用同一个生产协调器，不另造任何东西）：** (1) `viewer/multichannel_prefetch.py` 加
  `include_center`（**默认 False**，compare strip 的计划、邻居顺序、优先级逐项不变），为 True 时把当前通道
  **插在计划第一位**；(2) `ui/step0/step0_explore_tab.py` 把同一个 `MultiChannelPrefetchController` 挂到
  Full Image 自己的 stack 上（`include_center=True`），生命周期用 **Qt 自己的 `showEvent`/`hideEvent`**
  加 build/show_source/release/resume/discard 五处接线，不新增信号、step registry 或第二套 active；
  (3) `ui/step0/step0_page.py` 一行，把既有 `_compare_hot_specs` 装给 explore tab（与 Compare 同一个 provider，
  同一份 `_effective_correction_params`）。
  **顺带修掉一个真实公开路径缺陷：** 协调器的 generation token 是每实例从 0 开始的整数，而
  `cancel_generation` 在共享 scheduler 上**永久**标记过期——离开 Step0 再回来时新实例复用已过期 token，
  实测 `hot_tiles_requested = 12 / completed = 6 / cancelled = 6`、`settle_aborted = 0`、计划本身正确。
  已按 scheduler 自己的契约（token 是 opaque、应加命名空间）改为每实例唯一的 `("hot", n)`。
  **实测（真实只读演示切片 `biopsy.ome.tif`，level 0，35 块可见瓦片，CD22/CD4）：** 静置后由
  `CD22/tophat 35/35`、**`CD22/cucim 0/35`** 变为**四项全部 `35/35`**；**首次 TopHat→cuCIM 切换
  `434.5 → 103.9 ms`（-76 %）**；六次切换的 corrected tile 计算 `67 → 32`，corrected floor `80 → 45`，
  **provider 读取增量两侧都是 `0`**。合成台架同向但幅度小且噪声大（`123.0 → 86.3 ms`），不作为结论。
  **诚实边界：** 切换**不是零新计算**。当前视野自己的 corrected 瓦片确实不再重算，但控制器的
  **level+1 回退批次**（含一圈 ring）和**没有缓存的 corrected floor** 仍会重算——覆盖它们必须动
  四个页面共用的 `viewer/explore_view.py`（回退层策略 + floor 生命周期），**超出本块白名单，未实施**，
  最小方案见报告 §12。
  新增 `tests/test_step0_method_prefetch.py`（**17 条**门，覆盖 A–G 全部契约，含真实 `QStackedWidget`
  步骤切换、真实三面板 Compare、数据集隔离、重挂载不复用过期 token）；`tests/test_multichannel_prefetch.py`
  的 golden 只改**比较方式**（generation 按"第几个不同 token"归一），recording 文件一字未动。
  回归：新增门 `17 passed`，prefetch 三套 `67 passed`，Step0 十二个模块 `263 passed`，compare_tiles/handoff/patch 导航等六个模块 `329 passed`（合计 `659 passed`，0 失败 0 跳过）。
  **条件性白名单里的 `viewer/prefetch_policy.py` 与 `ui/step0/compare_strip.py` 均未改动**（SHA-256 不变）；
  未触碰 `viewer/explore_view.py`、`viewer/scheduler.py`、`viewer/caches.py`、Step1、`ui/main_window.py`、
  Step2/3/5/Nexus、科学算子、缓存容量与线程数；无新增可见 UI。`cufile.log` **本块零增长**
  （`60674 → 60674`，mtime 未变）。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b1_report.md` 与
  `_4b1_method_switch.json` / `_4b1_method_switch_real.json`。**不得记为真机通过。**
  本块**不解决** Step1 首个 cuCIM 通道的等待——Step0 的 HOT 缓存与 Step1 读取持久化 corrected Zarr
  的路径不是同一份。
- G3.2b.4B2（冷 Patch 与冷 Tissue 差异测量，**只读诊断块，已完成**）：**未修改任何生产行为，
  停止等待用户裁定。**
  真实 `MainWindow` + 真实 Step1 mount + 真实切片 `biopsy.ome.tif` + 真实 Step0 cuCIM 产物；
  Patch 用**真实 `QTest.mouseClick` 打在真实 P 按钮**上，Tissue 用真实 `navigate_requested` 槽。
  **结论：冷 Patch 慢的唯一原因是视口几何。** `show_patch()` 把相机设为**整个 patch bbox**
  （实测恒为 `1465×1088`、12–16 块瓦片，与用户当时缩放无关）；`_on_step1_tissue_navigate()`
  读当前 `viewRect()` 取 `min(宽,高)`，**保留用户的缩放**。用户点 patch 时通常是放大着的，
  于是 Tissue 只要 2–6 块而 Patch 一律 12 块。
  **决定性证据：让两者落到完全相同的最终视口后差距消失**——两轮 B2 各为
  `158.2 / 107.4 ms` 与 `160.9 / 168.6 ms`（均 12 块瓦片、44 vs 43 次读取、都真冷），
  Patch 并不更慢。**缩放扫描**量化了它：短边 300 时 Tissue `2 块 / 42.6 ms` vs Patch
  `12 块 / 136.6 ms`（**3.21×**）；短边 1500 时 Tissue 视口比 patch bbox 还大，**Tissue 反而更慢**
  （0.70×）——因果变量是视口面积，不是入口。自然路径五轮交替：Patch 目标层就绪中位
  **163.9 ms**（159.7–209.4），Tissue **91.3 ms**（80.5–115.0），比值 1.79×。
  **被否定的假设：** Patch 没有启动任何 legacy loader（14 次动作 `PreviewLoaderThread = 0`，
  G3.2b.2 守卫成立）、没有跑 `_ensure_channels_cached` / `_refresh_patch_preview`；session save
  每次排 1 次但 500 ms 去抖使其**在测量窗口内一次都没写盘**；两条路径的 hidden
  `ExploreController` 请求**按视口面积等比例**出现（Patch 32–37、Tissue 18–22，占各自读取的
  73 % / 79 %），不是 Patch 独有；无重复读取、无迟到拒收、无取消；两者**都在 level 0**，
  差别在瓦片数不在层级。
  **诚实缺口：** 本块用**单通道** CD22，绝对值（164 / 91 ms）**远小于真机的 2 s 与 <1 s**；
  能证明的是**比值 1.8–3.2×**，不能直接对上真机绝对值（真机多通道按通道数相乘）。
  **候选方案（只提出、未实施，按实测贡献排序）：** (C) 去掉 GPU 接管期间旧
  `ExploreController` 的不可见请求——对两条路径都有效、不改产品语义，但要动四页面共用的
  `viewer/explore_view.py`；(B) Patch 改为保留当前缩放、以 patch 中心空降——能直接降到
  Tissue 水平，但**是产品语义变更，必须用户裁定**（用户将不保证一次看见整个 patch）；
  (A) 保持现状——Patch 的成本是其语义的必然结果。**不打包成一次大改。**
  本块只新增 `scripts/benchmark_step1_patch_vs_tissue.py` 与
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b2_{report.md,patch_vs_tissue.json}`；
  **生产文件零修改**（已修改的跟踪文件集合与本块开始前逐项一致）。保护门
  `182 passed / 0 skipped`（强制 GPU，RTX 4090，`software_renderer = false`）。
  `cufile.log` 本块零增长（`60674 → 60674`，mtime 未变）；真实项目只读，Step1 会话自动保存
  保持开启但写入 `/tmp` scratch 目录。
- G3.2b.4B2C（GPU 接管期间停止旧 `ExploreController` 的不可见瓦片请求，**用户已授权方案 C**）：
  **已实施，等待复核与真机验收。**
  `ExploreController` 新增一个布尔与公开 `viewport_requests_enabled` /
  `set_viewport_requests_enabled()`，**默认 `True`**（Step0 Full Image、Compare、CPU fallback
  从不调用它，行为逐字节不变）。它只在与既有 `_suspended` **完全相同的判断位置**多加一个条件，
  挡住 controller 为**自己那些不透明度 0 的 ImageItem** 发出的请求：当前层可见批次、level+1
  underlay、prefetch ring、settled/precise 批次、方向性预取、corrected floor、已缓存精层的重提。
  **绝不碰**：ViewBox/相机、鼠标、`jump_to`、两个 timer、`interaction_event`、`gesture_quiet`、
  `selection_context_changed`、任何相机/view-rect 查询（Tissue 视框来源），以及别的消费者用自己
  generation 发出的请求。**它不是 `suspend_for_production`**——不锁相机、不写状态文字、不 join
  floor 线程、不排空 scheduler（有专门的门守着，变异 M5 把它改成用 suspend 实现即转红）。
  关闭时只取消自己的 generation（`("raw",n)` / `("precise",n)` / overlay 的 `("dapi_raw",n)`），
  与 binding 的 `("step1-gpu-binding", …)` 第一元素就不同，**不可能误伤**；重新开启对当前视口
  **恰好重规划一次**，幂等。mount 侧：GPU `initialized` 确认后才关闭；`_stop_gpu_backend()` 与
  `restore_legacy()` 重新开启；`activate`/`deactivate` 不动它；`ui/main_window.py` 未改。
  **实测（真实 MainWindow + 真实切片 + 真实 cuCIM 产物，两种 arm 顺序、1 通道与 4 通道）：**
  controller-only 请求 **`29–37`（Patch）/`18–22`（Tissue）→ 全部 `0`**；provider 读取
  1 通道 5 次冷 Patch 合计 **`224 → 64`（-71 %）**、冷 Tissue **`128 → 26`（-80 %）**，
  4 通道 **`416 → 256`（-38 %）** / **`206 → 104`（-50 %）**；**binding 请求、可见瓦片、
  view rect、target level、GPU 上传逐项不变**，Patch 仍显示完整 bbox、Tissue 仍保留当前缩放。
  4 通道热返回由 **`66`/`34` 次读取变为全部 `0`**——此前那些永不上屏的瓦片把有用瓦片挤出了
  512 MiB raw cache。最终像素由真实 `grabFramebuffer()` 对既有 C1 参考守 **≤1 LSB**。
  **诚实边界：本块不声称任何毫秒级加速。** 1 通道把 arm 顺序对调后墙钟无可辨差异
  （Patch `183.7 → 184.8 ms`、Tissue `90.3 → 92.3 ms`）——12–16 块瓦片根本没占满 8 个 I/O
  worker；4 通道中位数有改善（Patch `946.1 → 796.6 ms`、Tissue `538.7 → 382.0 ms`）但方差极大，
  且测量期间本机另有窗口在跑测试。能证明的是**读取量与浪费请求的确定性下降**。
  新增 `tests/test_step1_gpu_request_gate.py`（**14 条**门，覆盖 A–I 全部契约，含"不得变成
  suspend"与"不得吞掉待发 gesture_quiet"两条——第一轮变异 M2/M5 为绿正是因为缺这两条，已补齐后
  六项变异全红）。回归：新增门 `14 passed`，Step1 保护集 `257 passed / 0 skipped`，
  Step0 保护集 `351 passed`。Step0 保护集首轮有一次时序敏感的偶发失败
  （`test_hot_requests_never_outrank_the_foreground`），单独重跑 3/3 通过、整套重跑 351 通过，
  判定为机器争用，未改范围外文件。
  **B2C.1 收口（复核发现的生命周期 blocker，已修）：** 第一版把"停掉 GPU backend"一律当成
  "controller 的图层又是画面了"，三处都不成立且已逐条核实属实——(1) `_gpu_source_changed()` 唤醒
  旧 controller，**而下一行 `viewer.open()` 就把它销毁**，等于让正在下场的 stack 抢新 stack 的
  scheduler；(2) `close()` 在**关闭过程中又发起一轮读取**；(3) `restore_legacy()` 显示的是
  `self._legacy`（旧 **Patch** widget），并且**下一行就把 whole-slide host 连同 ViewBox 隐藏**，
  唤醒它只会产生不可见读取。收口：`_stop_gpu_backend(restore_controller=True)` 增加局部参数，
  来源重建与 `close()` 传 `False`，`restore_legacy()` 完全移除那次唤醒。
  `restore_controller=True` 保留为"同一 stack 真正交接给 CPU whole-slide renderer"的记录在案入口，
  **但今天没有任何产品调用方走它**（`open()` 与 `_gpu_source_changed()` 到达 CPU renderer 用的都是
  全新 stack，其开关本来就默认 `True`），报告如实说明。**第一版的三条门把错误行为写成了契约，已一并
  改正**；新增"来源重建不唤醒即将销毁的 controller"与"关闭时与关闭后都不发起读取"两条门。
  门 `14 → 15 条`，变异由六项增为七项（新增 M4b「`_stop_gpu_backend` 忽略 `restore_controller`」），
  **七项全红**。收口后回归 `291 passed / 0 skipped`（Step1 全套 + Step0 抽查）；B2C.1 只改
  `ui/step1_viewer_mount.py`，`viewer/explore_view.py` 与收口前逐字节一致，故 Step0 全套
  `351 passed` 仍然有效。
  只改 `viewer/explore_view.py` 与 `ui/step1_viewer_mount.py`；白名单内的
  `tests/test_step1_gpu_takeover.py`、`tests/test_step1_viewer_mount.py` **未改动**；
  17 个只读文件 SHA-256 与本块开始前逐项一致。`cufile.log` 零增长（`60674 → 60674`，mtime 未变），
  真实项目只读。详见 `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b2c_report.md`。
  **不得记为真机通过。**
- G3.2b.4B1.1（Step0 当前通道双方法准备的真实状态 + Full/Compare 缓存复用，**用户已授权**）：
  **已实施，等待复核与真机验收；其中 corrected floor 按 §八 停止并上报，未实施。**
  **路径 A（TopHat→点新通道→静止 2–3 s→点 cuCIM 仍重算）在本演示数据上不成立。** 九组测量
  （当前显示 Original / TopHat / cuCIM × 静止 1 / 2 / 3 s，真实 `biopsy.ome.tif`、真实 GPU
  `CorrectionCompute`、35 块可见瓦片）显示：HOT 每次都正确重规划（`centre_first` 九组全真、
  `hot_batches +1`、**点击时上一代占用槽位九组全为 0**），**两种方法的当前视野瓦片在点击后
  145–315 ms 内全部驻留**，方法切换时**当前视野 CorrectionKey 新计算为 0**、corrected cache
  命中 +70。因此 §七.A 的"若 gesture_quiet 已可靠重规划则不得再加第二次 replan"成立，
  **`viewer/multichannel_prefetch.py` 本块一字未改**。
  **真正被重算的是另外两样：** level+1 回退批次 **16 块/次**，以及**没有任何缓存的 corrected
  floor**（每次 13–31 个 `correct_array` 分块，76–284 ms，**中位 220.9 ms**）。切换墙钟中位
  **124.5 ms**。
  **路径 B（Full→Compare 重新准备）成立，已修。** 原因是两个模式各建一份 LRU，Full Image 算好的
  corrected tile 在 Compare 一次都命中不了。改为**借用**同一对 LRU（沿用既有 `overview_store`
  的"Full Image 拥有、Compare 借用"范式，新增 `ExploreStack.owns_caches` /
  `CompareStacks.owns_caches`，默认 `True`）：实测 raw/corrected **同一对象**，而
  scheduler/provider/controller **仍各自独立**；进入 Compare 的 corrected tile 计算
  **30 → 12（-60 %）**，消失的 18 块正是 Full Image 已持有的那一层，剩下 12 块是 Compare 自己的
  回退层（**只补真正缺失的 tile**）；**strip teardown 后 Full Image 仍驻留 45/45**（借来的不清）。
  预算不升反降：两个模式共用一份而不是各一份。**scheduler 仍分离**——共享会让
  `suspend_for_production` 的 "wait until idle" 变成等另一个模式（停止条件 #1/#6）。
  **一个回归，已修并加门：** 第一版把 `caches=` 无条件传给 `_stack_factory`，那是一个 seam，
  旧签名的 builder 会抛错并被报成 "Compare could not be opened"——实测使 **97 条**门转红。
  收口为"提供而非强加"（`inspect.signature` 判断，显式参数或 `**kwargs` 都算接受），
  新增两条门覆盖旧签名 builder 与 `**kwargs` builder。
  **按 §八 停止上报：** corrected floor 没有缓存，tile 共享解决不了它；最小复用接口、内存成本
  （本切片约 6.6 MB/(通道×方法)）、生命周期风险与回退方式见报告 §5，**未实现，等待追加授权**。
  新增 `tests/test_step0_cache_sharing.py`（**12 条**门）。回归 **`584 passed / 1 failed`**，
  该失败为既有时序敏感项 `test_hot_requests_never_outrank_the_foreground`（机器争用；单独重跑
  3/3 通过），与本块改动无关——该测试的 strip 经 `_page()` 的替身 stack 取不到 caches，走的是
  私有缓存旧路径。prefetch 三套 `67 passed`。
  只改 `ui/step0/compare_strip.py`、`ui/step0/step0_explore_tab.py`、`ui/step0/step0_page.py`；
  `viewer/multichannel_prefetch.py`、`tests/test_step0_method_prefetch.py`、
  `tests/test_multichannel_prefetch.py` 虽在白名单内但**未改动**；12 个只读文件 SHA-256 与本块
  开始前逐项一致。`cufile.log` 零增长（`60674 → 60674`，mtime 未变），真实项目只读，
  `/tmp/save_diag.log` 只读未改。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b11_report.md`。**不得记为真机通过。**
- G3.2b.4B1.2（复用既有 corrected floor cache + 后台准备当前通道双方法，**用户已授权**）：
  **已实施，等待复核与真机验收；两项退出门未达标，已如实记录。**
  **先更正一处事实：** B1.1 说 floor「没有任何缓存」是错的——`ExploreController._floor_cache`
  （8 entry `OrderedDict`，`7d7bb5a` / `a609c26`）早已存在，B1.1 报告已加 §0.1 更正。实测基线
  证明它本身完全好用：**同通道来回翻 TopHat↔cuCIM 四次，全部 0 tile、0 `correct_array`、
  每次命中 +1**。真正的缺口只有两个：**首次使用另一方法**（新通道的另一方法从未算过，
  实测 29 次 `correct_array`、命中 +0）与**跨 controller**（Full/Compare 各持一份，
  `floor_cache_same_object = [False,False,False]`，进入 Compare 重算 28 次）。
  **实现（无任何新 cache）：** `ExploreController(..., floor_cache=None, owns_floor_cache=True)`
  可注入既有 `OrderedDict`；floor job 的输入冻结为 `_FloorRequest`，`foreground=False` 的结果
  **只**写进 cache，不碰 `_floor_ctx`/`_floor_ready`/gain/ImageItem；新增公开
  `has_cached_floor()` / `prepare_floor_async()`（复用既有 key 规则、level/stride 选择、
  `correct_array` 与 gain calibration，**从不调用 `set_selection()`**）；HOT 在**空闲之后**
  为中心通道请求另一方法的 floor，并为中心通道排 level+1 回退层（坐标用宿主自己的
  `request_planning.bbox_to_level` + `tiles_covering`）。前台永远优先：一次最多一个 floor job，
  `_floor_pending` 先于**一个不可变的**后台请求值（不是队列）。`_handle_floor_result` 对历史
  8 元组 payload 兼容，**既有 floor 门一条未改**。
  **共享：** Full Image 拥有 `_floor_cache`，Compare 借用（`owns_floor_cache=False`，teardown 不清），
  scheduler/provider/compute/controller 仍各自独立；借用仍是「提供而非强加」
  （`_factory_takes("floor_cache")`）。`_floor_cache_limit` 仍 **8**（共享后是总共 8 张，
  不是每 controller 8 张），`FLOOR_MAX_PIXELS` 仍 4 000 000，**上限未提高**。
  **实测（真实 `biopsy.ome.tif`）：** 首次翻到另一方法 `correct_array` **29 → 12**、
  level+1 tile **16 → 12**、**floor cache 命中 +0 → +1**；Full→Compare
  `floor_cache_same_object` **`[False×3]` → `[True×3]`**、`correct_array` **28 → 7**；
  同通道来回翻前后都是 0/0/+1。
  **两项未达标，不记为通过：**（1）退出门 A 要求首次切换「floor `correct_array` 新调用 = 0」，
  实测仍有 **12** 次（floor 本身确实命中，这 12 次按时机看是切换后 HOT 为新的「另一方法」
  再做的一轮后台准备，但**未逐次归因**）；（2）level+1 fallback 由 16 降到 12 而非 0，
  HOT 准备的那批与前台切换瞬间用 `_current_bbox` 算的那批**未逐块比对**。
  **变异闸门（§十四 九项）本轮未执行**，因此本块不声称具备变异证据——这是缺口，不是通过。
  用户日志所指的更大切片 `…Slice2_Scan1.ome.tif` 不在可安全只读访问范围内，也无法确定其视口，
  **未伪造该切片数据**。
  回归：新增门 `8 passed`，`test_explore_controller.py` **`134 passed`**（既有 floor/cache/
  lifecycle 门全绿），合集 **`381 passed / 0 failed`**，prefetch 三套 `67 passed`。
  改动 `viewer/explore_view.py`、`viewer/multichannel_prefetch.py` 与三个 Step0 文件；
  白名单内的 `tests/test_step0_method_prefetch.py`、`tests/test_multichannel_prefetch.py`、
  `tests/test_step0_cache_sharing.py` **未改动**；12 个只读文件 SHA-256 与本块开始前逐项一致；
  `cufile.log` 零增长（`60674`，mtime 未变），真实项目与 `/tmp/save_diag.log` 只读。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b12_report.md`。**不得记为真机通过。**
- G3.2b.4B1.2.1（B1.2 两项退出门收口 + 逐次归因 + 九项变异，**用户已授权**）：
  **已实施，两项退出门与九项变异全部达标，等待复核与真机验收。**
  **B1.2 的两项「未达标」是两处计数口径错误，不是机制缺陷：**（1）那 12 次 `correct_array`
  **就是**那 12 块 level+1 瓦片本身——`CorrectionCompute.compute()` 内部调用
  `self.correct_array()`（`viewer/correction_compute.py:130`），同一事件被数了两遍；
  逐次归因（包住 `compute()` / `_calibrate_level_gains()` / `_run_floor_job()` 三个真入口，
  不用栈回溯）显示四条轨迹**切换窗口内 floor job = 0、floor cache 命中 +1、
  画面所需 key 的新增计算 = 0**，退出门 A 本来就成立。顺带解释了「12」为何会出现在两处：
  gain calibration 恰好是 `GAIN_WINDOWS(3) × num_levels(4) = 12`，与瓦片的 12 毫无关系。
  （2）那 12 块 level+1 **不是视口 fallback，是前台的一圈前瞻 ring**：
  `_issue_settled_request` 在 level+1 上要覆盖视口的 4 块 **外加** `FALLBACK_HALO_TILES = 1`
  的 12 块前瞻（优先级 `FALLBACK_RING_BASE_PRIORITY = 1000`，低于当前层的 100）。
  HOT 准备前者，**逐块相等**（`hot − 前台覆盖集 = ∅`、`前台覆盖集 − hot = ∅`）；
  后者按 §六C「禁止扩大为 ring」**故意不准备**。修的是 benchmark 的分段与归因，
  **未删除任何合法后台准备，未为了让数字变 0 而改生产逻辑**。
  **唯一的生产改动，由变异闸门 3 查出的真实缺陷：** `_handle_floor_result` 的后台分支
  直接调 `_start_next_floor_job()` 而不看 `_floor_pending`，于是（a）另一个后台准备会被排在
  用户正在等的前台 floor 前面，（b）更严重的是即使没有等待中的后台请求，那个前台 floor
  **根本不会被启动**（这条分支上 `_floor_pending` 没有任何消费者），用户会停在
  「Preparing corrected preview…」。直接违反契约「前台永远优先」。最小修复是同一函数
  前台分支**已经在用**的同两行（`if self._floor_pending: … _start_floor_job(…) else:
  _start_next_floor_job()`）——**不新增任何状态**，不新增 cache/scheduler/线程池/队列/
  registry/authority/token/journal/状态机，不改科学公式、UI、Step1、上限与所有权模型。
  **退出门 A–G 全部达标**（A 的「不重新读取 raw」与「逐像素一致」未独立测量，**不记为通过**）。
  **九项变异拆成 13 个具体变异逐项施加，13/13 全红**，每次用 SHA-256 确认五个生产文件逐字节还原。
  如实记录两点：6d 第一次为绿是**我选错了门**（`_floor_cache_key()` 只被 `_restore_cached_floor()`
  使用，而 `has_cached_floor()` 内联拼 key），换成真正经过它的门后为红，这不是冗余保护；
  变异 3 在**未变异的代码上就是红的**——那是上面那个真实缺陷。
  实测（真实 `biopsy.ome.tif`，只读，未清 page cache）：四条首次方法切换
  **floor job 0 / 命中 +1 / level+1 覆盖集计算 0 / ring 12**；Full→Compare
  **floor job 0**、三个 controller 同一个 floor cache 对象、scheduler/provider/controller 仍独立。
  回归：floor 门 `8 → 28 passed`；focused `191 passed` + `67 passed`；保护合集 `210 passed`。
  `test_step0_compare_tiles.py` 有一项**既有时序敏感**项间歇失败（修复后整文件 5 次中 2 次红、
  单独跑 3/3 绿、还原改动后 3/3 绿）；做了因果探针：整份文件里**后台 floor 结果落地 0 次**
  （`floor_jobs = 216`），本块改动的分支在该文件一次都没执行，且该断言只检查两档请求各出现过、
  **并不检查顺序**——与本块无因果关系，**如实列出**。
  `viewer/multichannel_prefetch.py` 与三个 Step0 生产文件**一字未改**；
  `tests/test_step0_method_prefetch.py`、`tests/test_multichannel_prefetch.py`、
  `tests/test_step0_cache_sharing.py` **未改动**；`cufile.log` 零增长（`60674`，mtime 未变）；
  `docs/tma_study_mode.md` SHA-256 未变；真实项目与 `/tmp/save_diag.log` 只读。
  **Advisory（不是扩围授权）：** 若将来批准，让 HOT 采用前台同一份补边规则可把那 12 块
  前瞻 ring 归零——本轮按 §六C 明文禁止，未做；另，未注入 floor cache 时三个 Compare panel
  仍各自持有 8 项（共 24），这是门 G 要求逐字节保留的历史行为，产品路径走不到（已加门）。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b12_1_report.md`。**不得记为真机通过。**
- G3.2b.4B1.2.2（复核要求的纯测试收口，**用户已授权**）：
  **已实施，退出门 A–G 全部达标，等待真机验收。生产代码全程冻结。**
  九个生产文件（五个白名单 + `scheduler`/`caches`/`correction_compute`/`raw_tile_provider`）
  起止 SHA-256 **逐字节相同**（`sha256sum -c` 九项全「成功」）；本块只改
  `tests/test_step0_floor_prefetch.py` 与三份文档。
  **缺口一：「不重新读取 raw」与「画面逐像素一致」此前未独立验证。** 两道新门各 4 条轨迹：
  （a）**raw 读取增量** —— 断言对象写死为**画面真正由之构成的**瓦片（当前层可见集 +
  level+1 覆盖集）；前瞻 ring 的 raw 确实会读（HOT 从不准备它、它不属于这一帧），
  **按构造排除，不是靠容忍值**。四条轨迹：帧内瓦片 raw 读取 **0**、该通道整层
  `read_region` **0**、floor job **0**。门内先证明同一个匹配器在 arrive 阶段**确实能找到**
  这些读取记录，所以「切换后为零」不是「匹配了个空」。
  （b）**逐像素一致** —— 参照系是 `open_full_image(with_hot=False)`（不装 specs provider 时
  `start_hot()` 直接返回、后台什么都不准备，即本条工作线之前的行为），只走公开 API；
  两次运行用**同一条数据集路径**使 source identity 相同，因此连瓦片完整 `CorrectionKey`
  都能断言；比较的是屏幕上的东西：每个可见瓦片 `ImageItem` 的像素数组、levels、颜色表、
  放置、可见性、key，加 corrected floor 的全部同类项，加 `_level_gain`、仍在透出的 RAW
  瓦片集合、钉住的 overview。四条轨迹**逐像素、逐放置、逐映射一致**。门内先断言参照运行
  至少 4 块带像素的入池瓦片、至少一块真的可见、floor 像素非空，**不能靠比空集通过**。
  **如实记录：** 初稿有一项 `raw_layer_visible` 读的是**根本不存在的**
  `ExploreView.raw_underlay_item`，两边都是 `False`、等于没比；已换成真正被
  `_update_layer_visibility()` 驱动的两个图层。
  **缺口二：计数窗口。** `set(tile_computes)` 的长度被用来切原始列表 —— 一旦有 key 被算过
  两次，切片起点偏早，**切换之前**的计算会被算到切换头上。已改为 `len(tile_computes)`。
  如实说明：今天这条门是**侥幸通过**的（该 rig 下确实无重复，但那是另一条门的断言，
  不该做本门的隐含前提），修正**未改变任何结论**；全仓复查确认没有第二处同样写法。
  **两道新门都用变异验证过能转红**（floor 不装回去 → raw 门红；恢复 floor 时丢 gain 表 →
  像素门红），还原后 SHA-256 逐字节一致。连同 B1.2.1 的 13 项，**本条工作线变异证据 15/15 全红**。
  回归：floor 门 `28 → 36 passed`；focused **266 passed**；保护合集 **210 passed**；
  `test_step0_compare_tiles.py` **190 passed**（B1.2.1 记录的那项既有时序敏感失败本次未复现，
  继续如实挂着）。`cufile.log` 零增长（`60674`，mtime 未变），`docs/tma_study_mode.md`
  SHA-256 未变，真实项目与 `/tmp/save_diag.log` 只读，未停止任何其他窗口进程。
  **诚实边界：** 逐像素门跑在 rig 的**合成**切片上，真实 `biopsy.ome.tif` 上的逐像素比对
  **未做、不声称**；未清 OS page cache。详见
  `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b12_2_report.md`。
  **未提交、未 push；不宣称真机通过。**
- G3.2b.4B1.2.3a（真机发现：cuCIM 在屏时 TopHat 未被后台准备，**用户已授权**）：
  **已实施，退出门与七项变异达标，等待复核与真机验收。**
  **根因：** `_prepare_centre_floors()` 无条件请求两种方法，而 floor 侧按设计只有**一个
  可替换的后台等待值**，于是**最后一个请求赢得等待位**，最后一个永远是 `cucim`。逐事件证据
  （真实切片，强制打开窗口）：`PREPARE tophat … slot: None -> tophat` →
  `PREPARE cucim cached=False running=cucim … slot: tophat -> cucim`（顶掉）→
  前台 cuCIM 落地并缓存 → `NEXT waiting=cucim already_cached=True`（正确丢弃）→
  **TopHat 从未准备**，用户点击时前台重算并停在「Preparing corrected preview…」。
  TopHat 在屏时同样被顶替，但幸存的恰好是前台没在算的方法，**正向通过是固定顺序的运气**；
  Original 没有前台 floor job，第一个请求立即开跑不占等待位，所以两种都准备。
  **为什么自动化此前没抓到：** `_prepare_centre_floors()` 只在 `_hot_idle()` 之后动手，
  本机上那时前台 floor 早已完成（实测 597 ms 完成、362 ms 后 HOT 才问）——
  **竞态窗口在这台机器上是关着的**。诊断用 `--slow-floor`（只在 floor worker 线程、
  只对 floor 自己的整层数组 sleep，不改任何决策/key/像素）+ `--dwell` 强制打开后复现，
  与真机四条结果一一对应。
  **最小修复（只改 `viewer/multichannel_prefetch.py` 一处）：** 屏幕上那个方法已经有主，
  HOT 不再请求它（`if method == snapshot.method: continue`）。不是换顺序、没有写死方法名
  （判断来自 `snapshot.method`，Original 仍两种都准备）、沿用既有 `HOT_METHODS`/spec 参数/
  `prepare_floor_async()`；**不新增任何状态**（无第二等待位、无队列、无 running-request
  registry、无 token/authority/journal/状态机）；floor key、`_floor_cache_limit`、
  `FLOOR_MAX_PIXELS`、前台 floor 生命周期、worker/scheduler/provider/算法一律未动。
  **相机 / Compare / UI 一个文件都没碰**（`ui/step0/step0_page.py`、`compare_strip.py`、
  `step0_explore_tab.py` 的 SHA-256 与起始逐字节相同）——相机漂移属 B1.2.3b。
  **强制窗口下同一组设置的前后对比：** cuCIM→TopHat 由「命中 +0 / 新 floor job 1 /
  `floor_ready_at_show_source_return=False`」变为「**命中 +1 / 新 job 0 / True**」；
  另外三条轨迹逐项不变。**自然时序下修复前后四条都是 +1 / 0**，本机碰不到窗口，
  **修复在这台机器上没有可观测变化，如实说明，不算成收益**。
  B1.2.2 的 raw 门与逐像素门（参数化含 cuCIM→TopHat）继续全绿：帧内瓦片 raw 读取 0、
  整层读取 0、floor job 0、与 `with_hot=False` 参照逐像素一致。
  **七项变异 7/7 全红**，每次 SHA-256 逐字节还原。如实记录：变异 5 有一个参数化分支为绿
  （写死 `"tophat"` 时「当前显示 TopHat」恰好同值，正因如此该门参数化三种起始状态，
  `[None]` 立刻转红，这不是冗余保护）；变异 7 临时改的是本块**只读**的
  `viewer/explore_view.py`，只施加→跑门→逐字节还原。
  回归：floor 门 `36 → 43`；focused **273 passed**；保护合集 **210 passed**；
  `compare_tiles` **190 passed**。`cufile.log` 零增长（`60674`，mtime 未变），
  `docs/tma_study_mode.md` SHA-256 未变，真实项目与 `/tmp/save_diag.log` 只读，
  未停止任何其他窗口进程。
  **Advisory：** Full Image→Compare 相机累计漂移（真机第 5 条）属 **B1.2.3b**，本块禁止处理；
  本机自然时序无法覆盖该窗口，CI 上要自然覆盖需要 floor 足够大的切片，本块未做。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b12_3a_report.md`。
  **未提交、未 push；不宣称真机通过。**
- G3.2b.4B1.2.3b（真机发现：Full Image / Compare 往返累计相机漂移，**用户已授权**）：
  **已实施，退出门 A–J 与九项变异达标，等待复核与真机验收。**
  **最新用户裁定（2026-09-21）覆盖旧判断：** 旧测试曾论证「漂移是 Compare here 的自然结果、不是 bug，
  要无漂移只能用那个已删除的按钮」——**该产品判断已撤回**。新契约：Compare here 保留；
  用户在 Compare 内**没动**过就返回进入前的 Full Image 相机、连续往返不累计；**动过**就采用移动后的相机；
  **不新增按钮**。
  **逐段实测（真实 ExploreView/ViewBox/GraphicsScene，真实 QMouseEvent，固定视口像素，十轮）：**
  漂移**不在**返回路径，也**不在**布局或 aspect lock —— 那四个接缝**全为 0.000000 屏幕像素**
  （Compare entry 落在点击点、面板自身不动、`full_after_exit` 精确等于 `compare_before_exit`、
  布局稳定后不变）。每轮位移**恒为 (−334, −115) 屏幕像素，正好等于鼠标相对视口中心的偏移**，
  十轮合计 **(−3340, −1150)**。所以 §四「若返回应用误差 > 1 屏幕像素则停止」未触发，
  `CompareStrip` 没有任何独立误差，§十二 的停止条件 1–8 一条都不适用。
  **最小修复（只改 `ui/step0/step0_page.py`）：** 离开时先问「用户动过没有」。新增**一个**不可变三元组
  `_compare_entry_panel_camera`，是面板**真正落位之后**从真实 ViewBox 的**回读**——
  拿进入时请求的 `(px, py, scale)` 去比会把每一次进入都判成「移动过」，因为三个面板各解各的矩形、
  `viewPixelSize()` 还带着 graphics view 的 device transform。它**不是第二套相机**：
  不驱动任何 ViewBox、从不被应用、只在 `_returning_full_camera()` 一处被读，进入时记录、
  离开时清空、数据集切换时清空（三处都有门）。判定在**屏幕空间**做：**先比 scale**
  （绕着不动的中心 zoom，中心一点没动，只比中心会把用户的 zoom 丢掉），再把中心差换算成屏幕像素，
  预算 1 屏幕像素 + 1e-6 相对 scale。**没有补偿常数、没有每轮减固定偏移、没有方向性修正、
  没有固定 level-0 阈值、没有依赖分辨率或切片尺寸、没有第二套 ViewBox/相机控制器/事件总线/状态机、
  没有新增按钮。**
  **修复前后十轮：** 固定偏心像素 **(−3340, −1150) → (0.0, 0.0)**；偏离中心半个屏幕像素
  **(10.0, 10.0) → (0.0, 0.0)**；每轮真实平移的对照组 **(−505.4, −47.1) 不变**；scale 无累积。
  Compare here 仍落在点击点；平移 / zoom（中心不变）/ Patch / Tissue 落点仍被带回；
  只改通道不动相机仍回进入前位置；右键与 Esc 一致；未移动的临时 Compare here 不再永久改写
  Step0 共享相机。
  **按裁定改写旧测试：** `test_step0_compare_toggle_drift.py` 的模块 docstring 与三条测试的结论撤回，
  **测量全部保留**（进入接缝的半像素量化仍作为上界断言存在，只是不再被带出 compare 模式）；
  `test_step0_compare_tiles.py` 两条记录旧契约的测试一并改写，并注明它们喂的是**固定 level-0 点**，
  这正是它们当初能通过而用户手势却漂移的原因。**没有恢复按钮、没有降低断言或减少轮数。**
  **九项变异 9/9 全红**，每次 SHA-256 逐字节还原。如实记录：变异 5 有一个门为绿
  （Patch 落点距离超过了变异塞进去的 4000 level-0 容差，照样被判为「移动过」），
  真正暴露 level-0 容差错误的是平移门。
  **用例 B 的诚实边界：** 本 rig 视口 1028×470，中心恰为整数像素，所以「最近中心整数像素」
  修复前就是 0、单独作门不可能失败；旧测试描述的 1091 宽视口在这里造不出来
  （实测页面 1200→1301 视口始终 1028），故改用**直接施加半屏幕像素偏移**的用例 D 覆盖。
  回归（逐模块，各模块顶部注明组合运行会在 offscreen pyqtgraph 下崩溃）：drift `13 → 27 passed`；
  compare_tiles 190；patch_compare_navigation 26；full_image_viewport 8；step1_shared_camera 15；
  compare_contract 5；overview_camera_ownership 33；B1.2 保护集九个模块全 passed。
  **`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full` 是既有失败**
  （把本块改动临时还原后同样失败，纯坐标映射断言，与相机往返无关），按「不为无关既有失败改范围外测试」
  未改动，**如实列出**。
  `ui/step0/compare_strip.py`、`step0_explore_tab.py`、`ui/shared_camera.py`、`ui/main_window.py`、
  `viewer/**` 全部**逐字节未变**；`cufile.log` 零增长（`60674`，mtime 未变）；
  `docs/tma_study_mode.md` SHA-256 未变；本块**一次都没读真实切片**（全部跑在 rig 的合成切片上）；
  未停止任何其他窗口进程。
  **Advisory：** 未移动的判定预算是 1 屏幕像素 + 1e-6 相对 scale，比这更小的亚像素真实拖动会被判为
  「未移动」——这是 §五 明确允许的量化余量，如实记录。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4b12_3b_report.md`。
  **未提交、未 push；未修改 CompareStrip/viewer/缓存/科学算子；未恢复 Compare 按钮；不宣称真机通过。**
- **G3.2b.4C（Step1 首个 cuCIM 通道约 6 秒等待：分段测量，**用户已授权**）：
  **测量完成，未实施任何生产修复，触发停止条件，等待用户裁定。**
  **两个真实数据集各五轮**：demo（Full WSI `19480×21804`）与**用户本人切片**
  （`/nvme0n1p1/2025.12.20_Final_17209_16_Slice4`，Full WSI `59040×35520`，
  用户 Step0 于 2026-09-21 写出的 CD22 cuCIM 产物），全程只读
  （两份 JSON 的 `project_readonly.changed` 均为 `[]`）。
  **用户切片中位：** 来源重绑 **9 729 ms**（GUI 无响应 9 732 ms），其中
  **`load_overview()` 9 497 ms（97.6 %）、`scheduler join` 1.3 ms（0.013 %）**；
  首次勾选 Patch 相机 **7 229 ms**，而**视口自己的 12–16 块瓦片 37.9 ms 就已到手
  （99.5 % 的等待与视口无关）**；原始通道对照 25.4 ms；热重勾 2.9 ms / 0 读取。
  **范围划清：** 测的是「**cuCIM 产物已在盘上的来源重绑**」，
  **不含 Step0 Save 本身**（校正计算 + 写盘，属 Step0，本块一次未运行，无任何数字归给它）；
  且 `load_overview()` 读的是**当时选中的通道**（本台架为 CD22），选中 original 通道的变体未测。
  **下列为 demo 数据集的分段（五轮中位）：**
  轨迹 `Step0 Save → sync_source() 重绑 → 勾选 CD22 → 当前视口完整清晰自然上屏` 合计约 **4.6 s**，
  由**两段同源成本**组成。
  **（1）重绑 2 232 ms，GUI 最长无响应 2 235 ms** —— 但**不是** `TileScheduler.shutdown()` 的
  GUI 线程 join（实测 **1.3 ms**，此前的假设在本轨迹**被否定**），而是
  `build_step1_stack()` 里同步调用的 `ExploreController.load_overview()` **2 063 ms**：
  当前通道是 cuCIM 通道，这一次「读最粗层」就是**整块 corrected 产物的归约**。
  **（2）首次勾选 2 342–2 461 ms**，其中 coarse 那**一块**读取 **2 387–2 448 ms**。
  **当前视口早就准备好了：** Patch 相机下 12 块目标层瓦片每块 3.7–23.7 ms、**27.4 ms 全部到手**，
  画面却等到 **2 421.6 ms** —— 等待的 **98.9 %** 与当前视口无关，在等整区域的粗层背景。
  这不是调度或 I/O 竞争（coarse 与 fine 在同一 scheduler 上并行，重复读取 `{}`、
  legacy controller 请求 `0`、迟到拒绝 `0`、上传 13/submit 14、完成到自然 paint 0.1 ms），
  而是 `Step1GpuBinding._publish_current()` 的**发布规则**——即「新通道一次完整清晰出现」
  验收契约的实现方式。对照：原始通道 **45 ms**；热重勾 **4.4 ms / 0 读取 / 0 上传**。
  **停止条件命中：** 主因是 corrected 产物的**整区域归约**，它落在
  `viewer/step1_source.py::reduce_corrected` 与 `viewer/explore_view.py::load_overview`，
  **两者都在本块白名单外**；白名单内（`ui/step1_gpu_binding.py`）**没有可消除的浪费**。
  因此**未改任何生产文件**（两个白名单生产文件逐字节未变，白名单外零修改）。
  **对既有判断的更正：** G3.2b.1「重绑 GUI 冻结主因是 `TileScheduler.shutdown()` 的 join」
  按本轨迹**更正为** `build_step1_stack()` 内同步的 `ExploreController.load_overview()`；
  旧判断不再作为后续方案依据。
  **用户 2026-09-22 裁定：** 报告初稿的方案 1（视口 fine 先上屏）**暂不采用** ——
  初稿把条件写成「fine 齐**或** coarse 齐」是错的（会放出只有 coarse 的模糊首帧，
  违反已验收契约）；即便按本意（保留 fine 齐、只去掉 coarse 前置），
  也会在随后平移时失去完整粗层背景保证。
  **余下两项分别裁定、不合成一次未经验证的修改：**
  **问题 A** —— GPU 接管时是否还需要同步读取那个不可见 controller 的 overview
  （事实：`build_step1_stack()` 无条件同步调用，且发生在 GPU 接管决定之前；接管后
  `overview_item` 不透明度为 0、像素从不显示；已存在不阻塞的
  `switch_overview_for_current_channel()`；**但 `_display_lo/_display_hi` 与
  `_ensure_corrected_floor()` 的依赖本块未核实**）；
  **问题 B** —— corrected 粗层如何避免每次整区域归约（每次新建 stack 都要重付一次）。
  本块无生产改动故**无新增变异闸门**；强制 GPU 套件 **`105 passed`**（probe/layer/sources/
  takeover/viewer_mount/request_gate）。`cufile.log` 零增长、`docs/tma_study_mode.md` 未变；
  两个真实项目运行前后全文件大小与 mtime 扫描**无任何变化**。**OS page cache 未清空，
  故本块没有任何数字可称为冷磁盘读取；未在真机验收，不宣称真机结论。**
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_4c_report.md` 与
  `2026-09-22_g3_2b_4c_first_enable.json`。**未提交、未 push。**
  起始 HEAD `9d78d1c`，分支 `v15-interactive-channel-workspace`，`git status` 118 项
  （其中 `cufile.log`、`docs/tma_study_mode.md` 为其他窗口/既有修改，本块只读不碰）。
  **任务：** 沿真实公开路径测量「Step0 把 CD22 存成 cuCIM → 进入 Step1 → 首次启用 CD22 →
  当前视口完整清晰像素自然上屏」，先定位 6 秒花在哪一段；**只有主因由同一条轨迹的证据确认、
  且修复完全落在白名单与既有机制内时**才实施最小修复。
  **白名单：** 生产仅 `ui/step1_gpu_binding.py` 与必要的 `ui/step1_viewer_mount.py` 薄接线；
  另可改/增 Step1 GPU 专项测试、独立诊断脚本、本文件与本块报告/证据 JSON。
  **禁止：** `viewer/step1_source.py`、`viewer/scheduler.py`、`viewer/explore_view.py`、Step0、
  校正算法、Zarr 格式、shader、GPU layer 渲染逻辑、共享相机、Patch/Tissue UI、`ui/main_window.py`；
  不新建缓存/scheduler/线程池/队列/预取权威/生命周期状态机；不显示 raw 冒充 cuCIM；
  不提前发布 partial coarse/fine；不提高 48 MiB/通道 fine 与 512 MiB GPU 预算。
  **停止条件：** 主因若在来源重绑的 GUI 线程 join、corrected 全片归约、provider I/O、Step0 Save，
  或修复必须动白名单外文件/新增生命周期机制 —— 停止实施，给分段证据与扩围方案等待裁定；
  真实公开轨迹若未复现 6 秒 —— 只交付诊断与复现条件。
  **本块不处理：** 画 Patch 晃动（G3.2b.5A，保持暂停）、来源后台退休、G3.2c/G3.2d/G4；
  G3.2b.5B 已通过真机验收，不得顺带重做。
- **G3.2b.4D（GPU 接管时跳过不可见 controller 的同步 overview 读取，**用户已授权，问题 A**）：
  **已实施并复测；Step0 Save→返回 Step1 的真机路径已由用户验收，规划复核已接受两处必要接线。**
  **实测（用户本人切片，各五轮中位）：** 来源重绑 `9 729.2 → 750.8 ms`、
  **GUI 最长无响应 `9 731.9 → 776.0 ms`（12.5×）**，
  且 `ExploreController.load_overview()` 在 GPU 路径的分段里**不再出现**——
  提速来自被消除的那一个等待，不是从总时长反推。
  demo 数据集同向：重绑 `2 232.3 → 296.7 ms`、GUI `2 234.9 → 351.8 ms`。
  **B 未动**：首次勾选仍约 7–8 s（用户切片）／约 2.5 s（demo），热重勾 3.0 ms / 0 读取，
  本块**不声称对 B 有任何改善**。
  用户随后真机反馈：**每个 cuCIM 通道首次启用仍需约 8–9 s**；这是 B 未解决的直接验收结果，
  不把 4D 的重绑提速误记为通道首次显示提速。
  **改动：** `build_step1_stack(..., *, load_overview=True)` 多一个关键字与一行 `if`；
  mount 在 `_gpu_wanted` 时包装 host 既有的 `stack_factory`（签名不认识该关键字则原样放行）；
  三个 CPU 回退点在 `_start_cpu_backend()` 前同步补读。
  无新增缓存/scheduler/线程池/队列/timer/状态机。
  **先证后改，五项依赖逐项证据成立**（`_display_lo/_hi` 只喂不透明度 0 的图元且 binding/layer/
  draft_spec 零命中；`_ensure_corrected_floor` 受 `method is not None` 门控而 Step1 无 method；
  overview 仓只有 Step0 读；相机/交互/视框不依赖；Step0 Full Image 有自己的调用点）。
  已知隐患「重建时 `host.jump_to` 先于补读」**由测试回答、不是 blocker**，未加任何重发机制。
  **两处偏离批准形状（规划复核已接受，非新增功能授权）：** ① `Step1ViewerHost.stack_factory` 读写属性
  （产品在白名单外的 `step1_viewer_binding.py` 里造 host，否则只能写私有属性）；
  ② `source_changed()` CPU 分支的第三个补读点（layer 在 open 阶段失败的 mount 不会走
  `_gpu_source_changed()`，否则永久 `_blocked_on_overview`；变异 C 有门守着）。
  新增 `tests/test_step1_gpu_overview_skip.py` **9 条门全绿**，**五项变异全红**，按 SHA-256 还原。
  回归：强制 GPU 六模块 **`105 passed`**（与基线逐数字一致，父会话独立复跑同样 105）；
  viewer_host 23 / viewer_binding 13 / dataset_switch 5 / handoff_invalidation 12 /
  shared_camera 15 / camera_stability 6 / display_isolation 21，全 passed。
  **如实记录三条：** ① 用户切片上 `TileScheduler.shutdown()` 的 join 由 1.3 ms 变为 **329 ms**，
  成为剩余 750 ms 中最大单项（demo 无此变化），**原因未查清、未改任何代码**；
  ② 用户切片「原始通道首勾」由 25.4 → 107.3 ms，但同一改动下 demo 控制组 45.0 → 48.8 ms
  基本不变，判为**环境差异**（当日多次运行已预热 page cache），**既不作为无回归的证明，
  也不作为回归的证据**；③ 关键字对旧签名工厂**静默放行**——我自己的诊断台架正是旧签名，
  已改为 `**kwargs` 透传，否则改后测量会走老路径；`scripts/benchmark_step1_gpu_cold_landing.py`
  仍是旧签名、未改（advisory）。
  `cufile.log` 零增长、`docs/tma_study_mode.md` 未变、两个真实项目 `changed = []`。
  **后续真机反馈：** 用户明确说明是在 **Step0 Save 后返回 Step1** 验收，
  新计算的 **TIM3、CXCR6** 均立即加载；此前再次要求测这条路径是重复要求。
  此反馈未给出重绑毫秒值，也未逐项报告相机、Intensity、Tissue 视框。
  **未提交、未 push。G3.2c 尚未启动。**
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_4d_report.md` 与
  `2026-09-22_g3_2b_4d_after_user_wsi.json` / `…_after_demo.json`。
  **（下面是本块开始时记录的范围，保留备查）**
  起始 HEAD `9d78d1c`，分支 `v15-interactive-channel-workspace`。
  **依据：** G3.2b.4C 五轮实测（用户本人切片）——重绑 9 729 ms 中
  `ExploreController.load_overview()` 占 **9 497 ms（97.6 %）**，`scheduler join` 仅 **1.3 ms**。
  **只做一件事：** Step1 **GPU 路径**不在建栈时同步读那个**不可见 controller** 的 overview；
  **不改** `viewer/explore_view.py::load_overview()` 本身或它的线程模型。
  **前置证明（先证后改）：** GPU 路径的像素、Intensity、相机、Tissue 视框都不依赖该次读取的副作用
  （`_display_lo/_hi`、`_ensure_corrected_floor`、overview 记录）；**GPU 初始化失败时 CPU 回退
  必须照旧拿到 overview**。
  **白名单：** 生产仅 `ui/step1_viewer_host.py`（用户本块新授）与 `ui/step1_viewer_mount.py`
  （4C 已授的薄接线，本块沿用；Fable 5.1 方案评估指出建栈早于 GPU 决定，缺它无法实现）；
  另可改/增 Step1 GPU 专项测试、独立诊断脚本、本文件与本块报告/证据 JSON。
  **禁止：** `viewer/explore_view.py`、`viewer/step1_source.py`、`viewer/scheduler.py`、Step0、
  `ui/main_window.py`、GPU layer 渲染逻辑、共享相机、Patch/Tissue UI；
  不新增缓存/scheduler/线程池/队列/生命周期状态机；不处理 B（首次勾选等完整 corrected coarse）。
  **验收：** 同一真实切片复测 GUI 心跳、来源隔离与画面；GPU 成功路径不得再发生该次 overview 读取；
  强制回退路径与 `gpu=False` 必须与今天逐项一致。
  **本块不关闭 G3.2b，也不进入 G3.2c。**
- **G3.2b.4E（首个 corrected 通道等待：持久化现有 L3 coarse）：Phase A 表示与读取验证通过；Phase B 写盘待执行。**
  用户真机反馈每个 cuCIM 通道首次启用仍需约 8–9 s；4C 在用户切片上的分段测量显示当前视口瓦片约 38 ms 就绪，等待主要来自每通道首次从 corrected L0 归约完整 L3 coarse。4D 只消除了 GPU 重绑时不可见 controller 的同步 overview，未改这条等待。
  **目标：** 保持 `Step1GpuBinding` 既有“完整 coarse + 当前视口 fine 才首次发布”契约，只将这份现有 L3 coarse 从交互时归约改为读取与 corrected L0 同源的持久化派生表示。缺失、过期或不完整时仍走原运行时归约，不显示 raw 冒充 corrected。
  **两阶段：** A 先在 `/tmp` 合成产物及真实产物只读副本/候选输出上确定现有 coarse 的空间网格、stride、ROI 边界和数值语义，验证持久化 L3 的 shape、valid 与 C1 显示等价、实际大小/读取时间；A 不成立即停止。B 才在 Step0 corrected tile 写 L0 的同时有界累计并发布 L3，Step1 优先读身份匹配的完整 L3。新 Save 不得写完 L0 后再全片重读；增量 Save 只处理变化通道并正确失效旧 L3。
  **Phase A 证据：** 候选 L3 在五类 ROI/边缘/stride 场景中 valid 零差、有限值逐位相同、NaN/Inf 模式相同，C1 Overlay/Fusion RGBA 差 0 LSB；用户现有只读产物的两个 corrected 通道，完整粗层 runtime 归约分别约 11.6/9.2 s，候选 plane 读取约 7.4/2.5 ms，plane 各约 1.5 MiB。候选 sidecar 在 `corrected_channels.zarr` 旁，避免现有产物报告把内置子组误认为通道；22 条专项门与受影响回归通过。OS page cache 未清空，这不是冷磁盘计时。
  **Phase B 必须补齐身份：** 同一通道即使用相同方法/参数/ROI/shape 重写 L0，旧 L3 也必须失效。写盘时为新 L0 与 L3 绑定同一新的每通道产物标识，并在开始覆写前使旧 plane 不可用；取消/崩溃/半写入只允许退回运行时归约。须有“同参重算但像素变化”门。不得用仅比较静态校正参数来推断像素相同。
  **旧产物：** 运行时保持只读 fallback；可提供显式、独立的离线补建脚本，但不得自动迁移或写入用户真实项目。真实项目补建须另获执行授权；仅修改新 Save 不会让现有项目立即变快。
  **白名单：** 生产仅 `ui/step0/search_ctrl.py` 与 `viewer/step1_source.py`；对应专项测试、独立诊断/显式补建脚本、本文及本块报告/证据。若必须修改 Zarr opener/schema helper、handoff、其他消费者或公共 provider，先停下说明扩围，不顺手改。
  **验收：** L0 科学像素与现有产物不变；L3 与现有运行时 coarse 使用相同全局 level-0 网格、ROI 边缘有效样本规则和数值语义，valid 精确一致，Overlay/Fusion RGBA ≤1 LSB、alpha 完全一致；首次发布仍完整且清晰、远跳/缩放无空白，旧产物 fallback/CPU fallback/original 通道不回归。报告实际 Save 增量、L3 压缩大小、首次勾选与 GUI 响应，不用预计百分比冒充实测。
  **禁止：** 不改 GPU binding 首帧规则，不加 L1/L2/完整 pyramid、通用 multiscale 框架、generation 目录、自动迁移、运行时持久缓存、scheduler/线程池/队列/状态机；329 ms join 另案记录，不在本块修。
  **状态（2026-09-22 更新）：Phase A + Phase B 均已实施并通过本块真机验收；G3.2b 未关闭，
  G3.2c/d 与 G4 未启动。**
  **Phase A（先定义再证明）：** 从代码读出现有 coarse 的精确语义——右/下瓦片不完整、
  `stride = round(level_downsample)` 取自切片自己的金字塔（用户切片 `52560→13140→3285→821`，
  有效 64.02 → 64，**不是 `2**level`**）、块锚定 level-0 原点、按**有效样本**归一、
  存储的 0.0 是样本、ROI 外 valid=False。独立候选实现（按定义重写、按 Step0 写盘顺序累计）
  与现有实现在五个用例（ROI 不对齐/对齐/都不整除/零值带/倍率 2）上：
  **valid 不一致 0、有限值逐位相同、`max|Δ|=0.0`、NaN/±Inf 模式一致**，
  经**现有 C1** Overlay 与 Fusion **RGBA 0 LSB、alpha 一致**。
  真实产物（用户切片，只读符号链接，sidecar 只写 `/tmp`）：全部 coarse 瓦片
  **CD22 `11 589.1 → 7.4 ms`、CD163 `9 188.6 → 2.5 ms`**，L3 各 **1.5 MiB**，
  经生产 `read_tile` **100.0000 % 逐位相同**。读侧全在 `viewer/step1_source.py`，复用既有 opener；
  sidecar 名 `corrected_coarse.zarr`，放在产品**旁边**——放进产品组会被
  `core/bg_correction.py::corrected_zarr_report` 递归当成通道，而该文件不在白名单内。
  **Phase B（Step0 写 L0 时顺带产出）：** `WsiCorrectionWorker` 在写每块 L0 的同一时刻，
  用**同一批（polygon mask 之后的）像素**累加 L3，**从不回头重扫 L0**；
  stride 由切片金字塔读出，读不到就不写 sidecar（不猜）。
  **身份缺口已补：** 每次真正重算才新铸 `source_identity`（uuid4）写入该通道 L0，同值写入 L3，
  Step1 两边逐项比对；**产品 token 为空则永不启用 plane**；覆写 L0 前先删旧 L3，
  `complete` 最后写；取消/异常/半写入一律被拒；增量 Save 跳过的通道其 token 与 L3 原样不动。
  **端到端（真实 GPU 栈，合成 `/tmp` 项目，同一产品两种状态，各 3 轮中位）：**
  coarse + 当前视口 fine 就绪 **568.9 → 252.6 ms**（离屏台架，不是屏幕呈现时间）；**读取的 corrected level-0 像素
  `111 149 056 → 57 671 680`，差值 `53 477 376` 恰等于该 ROI 面积**——
  「整区域扫描」在有 plane 时**一次都没发生**；上传/submit/provider 读取计数逐项不变；
  平移与 8× zoom-out 后完整 coarse 仍在、descriptor 同时带 coarse 与 25 块 fine。
  **更正（复核指出）：** 端到端 JSON 中有 plane 的三轮有两轮
  `first_natural_paint_after_complete_ms` 为 `null`，所以 **252.6 ms 是「coarse + 视口 fine 就绪」
  的时间，不能称为「已看见的清晰首帧」**；且「无空白」此前只核对了 descriptor。
  **补测（`…_4e_pixels.json`）：** 显式等待上限 3 s 时，两种状态在完成后 **0.1 ms 内**
  都确有一次自然 paint；随后在**同一相机**各取一次**明确标注的强制 framebuffer**，
  两帧**逐字节相同**（RGB 0 LSB、alpha 一致、不同像素 0），8× zoom-out 后同样逐字节相同；
  alpha 为 0 的像素为 0（无透明洞），纯黑区域与 ROI 之外的屏幕面积相符（≈32 % 非黑 vs ROI 占屏 31.6 %）。
  **这仍是 offscreen 台架，不能替代真机「首帧完整清晰」的验收。**
  **Save 代价：合成台架 `714.0 → 795.3 ms`，即 +81.3 ms（+11.4 %），仅此台架的测量**；
  真实 cuCIM Save 未测该项；累加器两个小数组（用户全片区域 8.2 MiB）；
  sidecar 占产品 0.02 %（真实区域 1.5 MiB/通道）。第一次配对测得 −149.7 ms 属噪声，已弃用。
  **旧产物限制：** 本功能实施前写出的产物**没有 token**；未经重算或显式补建时，
  **Step1 不会为它们启用 plane，首次启用仍然慢**；
  补建脚本对无 token 产品**主动跳过**，只有显式 `--stamp-product`（写 attrs、不碰像素）才能补建，
  **本块未对任何真实项目运行补建或 `--stamp-product`**；
  **Phase A 的真实产物加速数字不能当作 Phase B 给现有项目的现成提速。**
  门：读侧 24 + 写侧 12 全绿；**变异 7/7 全红**（含「同参数不同像素」「无 token 命中」
  「complete 写在像素之前」「polygon mask 之前累加」「猜 stride」），每次按 SHA-256 还原。
  回归 13 个模块全 passed。**修正了四处「门因错误原因而绿」**：加 token 后 Phase A 三条门失效、
  两条无 token 门只清一侧、两处 `or True` 恒真断言（设置比对改为同一 CH_A 前后逐字段比较并断言
  像素确实不同；polygon 门改为断言被遮蔽整块 `0.0` 且 valid）；另修正端到端台架把
  `descriptor_history` 四元组当成 descriptor 而吞掉 zoom-out 检查的缺陷。
  **`cufile.log` 在本块增长 `4449463 → 4449898` B**（一次在仓库目录下的导入自检使 cuFile 追加三行）；
  **未截断、未恢复、未清理、未暂存**，如实记录。
  详见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_4e_report.md` 与
  `…_4e_endtoend.json` / `…_4e_after_tmp_plane.json`。
  **真机验收：** 用户在 Step0 Save 后返回 Step1，新计算 **TIM3 与 CXCR6**，确认两者首次启用时
  **可立即加载**，并明确反馈「人工验收通过」。未提供真机毫秒数字；验收只覆盖这两个新产物，
  不把旧产物、真实 Save 增量或 G3.2b 其他待办一并记为通过。**未提交、未 push。**
- 方案 B（Patch 保持当前缩放）、G3.2b.4B3（Step1 coarse/fine 顺序）、
  corrected pyramid、轨迹 B 的旧 stack 后台退休、G3.2c（ROI bbox 外 DAPI 精确裁切）、
  G3.2d、G4：**均未开始，也均未获授权。**
- G4：未开始。
- G0/G0.1/G1 仅提出待后续批准的接口建议：未来 `Step1GpuLayer` 以既有 ViewBox 的只读 viewport snapshot、既有 source/display snapshots 工作，拥有局部 GL resources/有界纹理 cache；不拥有相机、provider、scheduler、raw cache、共享 UI 或新的权威状态。PyOpenGL 3.1.10 已仅按 G1 授权固定于 `fusion_test2` 演示环境；它不等同生产依赖/生产接入，任何下一块仍须用户单独批准。

- **G3.2b.4F（Step0 Save 后首次进入 Step1 时 GUI 冻结）：共享通道栏搬移的最小修复已通过本次真机体验验收。** 原真机入口 1919 ms / GUI 心跳 2104 ms；Save 后后台预导入 PyOpenGL 虽测得入口 1052 ms，却导致无图像，已撤回，回退后用户确认图像恢复。主窗口启动时的同线程预导入也在用户真机体验中失败：画面正常，但入口 2285 ms / GUI 心跳 2611 ms，GL `show()` 已降至 157 ms，反而是通道栏搬移涨至 1734 ms，已撤回。当前只在 dock 已有旧宿主时显式 `setParent(new_host)` 再 `addWidget()`；真实 GPU 台架六次交替使搬移从 461–537 ms 降至 48–88 ms，Step1 入口三次由 1745–1767 ms 到 999–1372 ms，TIM3 帧缓冲均有像素。产品代码台架入口 1298 ms，coarse/fine 到齐。保护合集 151 passed / 42 skipped / 1 failed，唯一固定像素几何门在完全撤掉本块修改后仍同值失败，非本块引入；其余跨 Step 身份、焦点、滚动门通过。用户最终真机反馈：图像正常、通道立刻加载、Step0 Save→Step1 不到 1 秒、窗口没有感到卡住；同次日志入口 **994.70 ms**、dock 搬移 **93.14 ms**。但 GUI 心跳仍记录跨切换的 **1712 ms** 间隔，留作性能 advisory，不宣称事件循环全程无长间隔。最初 5–6 秒未在本轮重现；预建 Viewer 会把 GUI 停顿挪到 Save 结束，未采用。证据与限制见 `docs/benchmarks/step1_gpu_demo/2026-09-22_g3_2b_4f_entry_freeze_report.md`。
- 本文新增不代表 CPU 回退基线已被接管。
- `AGENTS.md` 与 `P0_SCOPE_RULES.md` 为本项目持久规则；未写入平台级记忆、未修改其他项目。
