# v16 块 A0 — 只读诊断与契约草案：申请 v2

日期：2026-09-29。分支 `v16`，调查基于 HEAD `bf3af2c`。依据：`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.1.md` §2（A0）、§8（测试数据）、§13（停止规则）。
状态：**v2，用户 2026-09-29 批准**（v1 获独立审核「有条件批准」，用户完全同意审核意见；5 项修订已写入本版，按审核结论无需再审即可启动）。§8 的 5 项裁定均按建议通过。

修订记录：
- v1：初稿。
- v2：按独立审核修订，5 项均核实属实或已按事实回答：
  ① 基准工作负载预注册（§3 A0-2 第 2 步）：尺寸、次数、种子、并发、统计量、主判定指标都写死；NGFF 用同等并发的合理读取实现。
  ② NGFF `coordinateTransformations.scale` 带物理单位：`[1, PhysicalSizeY × 层比例 y, PhysicalSizeX × 层比例 x]`；自检项写全。
  ③ C1 的图像验证改为：平移用相位相关；比例直接比较相机 / ViewBox 变换数值；视觉确认锁定同一原始单通道、同一显示窗口，不开 fusion 和 mask，用叠加图和差分图。相位相关不用来估算比例。
  ④ 合成大图**已经生成**（选项 A）：本会话按 v2.1 §8 生成并核对，脚本与记录已入库 `bf3af2c`，身份见 §3 A0-2 第 0 步。
  ⑤ 验收门第 4 条改为 `git status --short` + `git diff --name-only` + 提交前 `git diff --cached --name-only`，要求改动与未跟踪路径 ⊆ 白名单。
  另记两条不阻塞的建议：`model_checksum` 不是权重哈希，A0.5 必须重新讨论模型文件的真实身份；Odon 参考基线可选（§3 A0-2 第 4 步）。

---

## 1. 必要性

v2.1 的 A1（viewer 零漂移）、A2（PixelSource / NGFF）、A3/A4（坐标、身份、对象表）都要以 A0 的证据为前提：

- A1 只允许改 A0 点名的函数。没有日志证据，就不知道该改哪一处。
- A2b（NGFF）是否做，由 §2.3 事先写死的采纳规则决定，需要实测数字。
- A3 要冻结的约定，必须先弄清今天的代码实际怎么做。本次调查已发现几处与计划写法不一致，见 §2.3。

## 2. 只读调查结论

以下行号都已对照 `bf3af2c` 核实；标「离屏实测」的是本次用临时脚本测得的，没有写任何文件。

### 2.1 viewer 位移：已经能从代码里看到的候选原因

相机的定义（`ui/shared_camera.py:10-66`）：
- `CameraSnapshot(dataset, cx, cy, scale, origin)`；
- `(cx, cy)` 是屏幕中心对应的第 0 层坐标；
- `scale` = `1 / ViewBox.viewPixelSize()[0]`，单位是**逻辑像素**每个第 0 层像素，不含 devicePixelRatio；
- `origin` 只用于诊断，不参与比较。

切换顺序（`ui/main_window.py`）：
1. `_go_to_stepN` 先调用 `_stack.setCurrentIndex(N)`（Step0 在 :1497-1501，Step1 在 :4710，Step3 在 :4763），再调用 `_set_step_active(N)`（:5180）。
2. `_set_step_active` 内部依次做：
   - 采集离开页的相机（:5184-5186）；
   - 显示范围 resync（:5191-5213）；
   - `_step1_whole_slide_step_changed`：可能触发 `sync_source` 重绑或首次 `open()`（:5218）；
   - **套用共享相机后立即回读**（:5227-5229 → :2306-2334）。
3. 套用**之后**还会发生：
   - 通道 dock 重新挂到新页面（:5234-5241）；
   - Step1 底栏执行 `setFixedHeight/Width`（:5274）；
   - `QTimer.singleShot(0, _hold_step1_channel_floor)` 执行 `setMinimumWidth`（:5276）。

   这些布局变化在下一轮事件循环才传到 ViewBox。pyqtgraph 的 `ViewBox.resizeEvent` 会按新的宽高比重新拟合 targetRange：中心不变，比例会变。变化后的值再经 live sink（:2261-2277）写回共享相机。

逐条候选：

| # | 候选机制 | 证据 | 状态 |
|---|---|---|---|
| C1 | **GPU 层与 ViewBox 的几何不一致**：GPU 层铺满整个 graphics viewport（`ui/step1_gpu_layer.py:585`、`:1433-1436`），但画的世界矩形是 ViewBox 的 `viewRange()`（`ui/step1_viewer_mount.py:784`；着色器把 `u_view_rect` 映射到整个输出，`step1_gpu_layer.py:955`）。pyqtgraph `ci.layout` 默认四边各留 9 px，仓库里没有清零。 | 离屏实测：viewport 800×600，ViewBox 782×582，位于 (9, 9)，边距 (9, 9, 9, 9)。同一相机下，Step1/3 的 GPU 画面比 Step0 的 CPU 画面横向放大约 2.3 %、纵向约 3.1 %，画面中心对齐，越靠边偏得越多，边缘处约 9 px。 | 代码 + 离屏证实；真实 GL 下是否可见待 A0 测 |
| C2 | **Step1/3 套用相机时取整并重新拟合**：`apply_camera`（`step1_viewer_mount.py:1076-1101`）把矩形 `int(round(...))` 后交给 `jump_to` → `setRange(rect=…)`（`viewer/explore_view.py:4633-4641`），锁宽高比的 ViewBox 会重新拟合。Step0 走 `set_view_rect_l0`（:4643-4663），用浮点、两个轴分别设定、不拟合。回读的结果成为新的共享值，来回切换时误差会像随机游走一样累积。 | 代码 | 待日志量化 |
| C3 | **套用之后的布局变化触发 ViewBox 重新拟合**（见上面的第 3 步），结果又被 live sink 记下。 | 代码 + 离屏布局探测：sibling 设 `setMinimumWidth` 后，ViewBox 要到 `processEvents` 之后才改变尺寸 | 待日志证实 |
| C4 | **重绑源时截断**：`Step1ViewerBinding._viewport()` 用 `int()` 截断后再 `jump_to`（`ui/step1_viewer_binding.py:142`、`:172-179`）；新建的 ExploreView 先 `setRange` 到整张切片（`ui/step1_viewer_host.py:418`）。 | 代码 | 待日志 |
| C5 | **Navigator 跳转**用整数正方形矩形（`main_window.py:2418-2431` → binding :187-191）。 | 代码 | 只在用户点击时发生，不属于切换本身 |
| C6 | **DPR ≠ 1 时 `paintGL` 的 blit 目标用逻辑尺寸**：FBO 按物理尺寸分配，blit 却画到 `self.width()/height()`（`step1_gpu_layer.py:840-850`）。 | 仅从代码推断，本机 DPR = 1 | 只能在 Windows 125 % / 150 % 真机上看 |
| C7 | **现有测试覆盖不到上述情况**：`tests/test_step1_shared_camera.py` 的 `_in()` 只调用 `_set_step_active`，不调用 `setCurrentIndex`（:192-196）；用 CPU 假 stack，没有 GPU 层；容差 12 个第 0 层像素 / 6 %（:47-48）。`tests/test_step3_viewer.py:210-230` 只检查中心、容差 2 %，不检查比例。 | 代码 | 说明为何测试一直是绿的 |

没有发现 autoRange 在切换时触发：ExploreView 调了 `disableAutoRange()`（`explore_view.py:1627-1634`）。在 ui/ 与 viewer/ 里 grep `autoRange|setRange|fit` 共 156 处，绝大多数是 QSpinBox / QSlider 的 `setRange`。与相机有关、在切换路径上能走到的，只有上表所列。

### 2.2 存储基准：读取路径与环境

| 工作负载 | 产品路径 | 读法 |
|---|---|---|
| viewer（Step0 / 1 / 3） | `viewer/raw_tile_provider.py` + `viewer/scheduler.py` | 512² 分块、按显示层级；每线程一个句柄；8 个 IO 线程；原始 LRU 512 MiB，校正 LRU 2 GiB |
| Step1 校正通道的粗层级 | `viewer/step1_source.py:553` `reduce_corrected` | 第 1 层每次**运行时**从第 0 层归约；第 2 层有 sidecar 时用 sidecar。**这是 NGFF 金字塔最可能带来收益的地方** |
| Step1 fusion | `ui/step0/overview_panel.py:242` `FullFusionWorker` | 按分块读，每块用 8 线程并行读各通道；`loader.read_region` 在**每次调用时重新打开 TiffFile**，只读第 0 层（`core/io_loader.py:259-291`） |
| Step2 | `workers/segment_merge_worker.py:2543` | 主输入是 Step1 的 `fused.zarr`。HQ 类方法还会读原始切片或校正 zarr（`utils/channel_cache.py:82-111`） |
| Step4 | `core/quant_sources.py:389-491` `TiffTileReader` | 4096² 分块、按行扫描；每个 TIFF 分块 `seek + read` 后用 `page.decode`；8 线程；队列深度 2 |
| Step0 校正写出 | `ui/step0/search_ctrl.py:2087` | 4096 块加 halo，单线程；写 float32 zarr，1024 分块，默认 Blosc lz4 |

环境与数据：
- **版本**：zarr 2.18.3、numcodecs 0.13.1（有 Blosc lz4 / zstd、Zstd）、imagecodecs 2025.3.30（有 LZW 编解码器，但要调用 `register_codecs()`，其他工具读不了）。代码里没有任何 NGFF / multiscales 相关代码。
- **冷缓存**：本机实测，`posix_fadvise(DONTNEED)` 能把 3.7 GB 的合成图完全清出 Linux 页面缓存（`fincore` 显示 3.5G → 0B）。清空后顺序读 7.2 s，热读 3.3 s。**需要注明**：Windows 宿主机可能缓存 vhdx 本身，所以「冷」只指 WSL 客户机层面。
- 旧基准脚本 `scripts/benchmark_step4_baseline.py` 已经跟不上现在的 worker 接口，不能直接复用。可以复用的是：`benchmark_viewer_prototype.py`（视口填充、平移）、`benchmark_multichannel_prefetch.py`（已有 `evict_os_cache`）、`benchmark_step1_corrected_reduction.py`、`probe_step4_kernels.py`。

### 2.3 身份与坐标：今天的实际做法（契约草案的起点）

- **bbox 顺序**：`bbox_fullres` 在全仓库都是 **`[y0, y1, x0, x1]`**，半开区间，第 0 层像素（例如 `utils/roi_project.py:49-53`、`core/label_ownership.py:43-49`、`core/seam_merge.py:61-66`）。计划 §6.4 的列名是 `bbox_x0, bbox_y0, bbox_x1, bbox_y1`。两者语义相同、写法不同，**草案里要显式写明两者的映射**。
- **像素中心约定不统一**：
  - Step4 的质心是整数下标的平均（`core/quant_engine.py:229-250`、`:427-482`），整数下标就是像素中心；标签金字塔按中心取样（`core/label_pyramid.py:9-17`）。
  - viewer 的世界坐标把第 0 层像素 j 画在 [j, j+1)（`ui/step1_gpu_binding.py:912-918`、`core/step3_masks.py:702-738`），所以像素中心在世界坐标里是 j + 0.5。
  - 各模块内部是自洽的，但两者之间有半个像素的系统差。A3 的「整数下标 = 像素中心」必须写明 viewer 世界坐标 = global_pixel + 0.5。
- **层级比例**：
  - 用每层真实比例的：`level_downsample_yx`、标签金字塔、GPU 绑定。
  - 用取整比例或固定倍数的：`level_downsample` = round、Step1 校正通道的 stride、`corrected_coarse.zarr` 的 `stride: 16`（真实比例是 16.013 / 16.007）、`OVERVIEW_DOWNSAMPLE = 32` 等。
  - 按 v2.1 §6.2，这些旧代码不回改，只约束新代码。
- **物理尺寸**：产品里**从不读取** `PhysicalSizeX`。Mesmer / 参数表里写死了 `image_mpp = 0.5`（`utils/segmentation_param_schema.py:108`、`seg_runner/engines.py:185` 等），而本切片实际是 0.50686 µm。这条列入草案，是否修正由用户另定，**不属于 A0**。
- **身份**：
  - 今天没有 slide / sample / patient ID。切片靠路径 + `size:mtime_ns` 指纹识别（`core/step0_handoff.py:216`）。
  - 区域在运行内部按显示名 `roi_name` 做键（`label_store["Full WSI"]`、文件名 `global_mask_Full WSI.zarr`），`roi_id` 只出现在 `rois[i]` 和工作区里。
  - 运行 ID：`seg_<时间>_<方法>`（`workers/segment_merge_worker.py:254-270`）。
  - Step4 h5ad 的 `obs` 只有 `cell_id`，没有运行 ID 和区域 ID。
- **引擎身份（供 A0.5 用，这里只记录）**：
  - `lock_hash` 对整个环境 lock 文件取哈希（`seg_runner/runner.py:23-49`）；`model_checksum` 是对 `models.json` 里条目的哈希，**不是模型权重文件的哈希**。
  - Step2 只在引擎**种类**不同时才中止（`workers/segment_merge_worker.py:2313-2316`）；`lock_hash` 等其他字段不同只打警告并记录（:2317-2326）。
  - Step1 的 `preseg_contract.build()` 要求所有 patch 记录的身份**整体完全相等**（`core/preseg_contract.py:62-72`）。所以装上 pyarrow 之后，新旧 patch 混在一个 Step1 运行里会被拒绝。

## 3. 做法

A0 分三部分，都不改产品代码。

### A0-1 viewer 位移诊断

新脚本 `scripts/diagnose_v16_a0_camera.py`。插桩只存在于脚本进程内：包装方法后调用原方法，只记日志、不改变任何行为。

1. **离屏 / 非 GL（自动）**
   - 用 `tests/test_step1_shared_camera.py` 的装配方式构造真实的 `MainWindow`，但**调用真实的 `_go_to_step0/1/3`**，也就是包含 `setCurrentIndex`。
   - 在以下函数前后记录：
     - `_set_step_active`、`_capture_camera_of`、`_apply_shared_camera_to`、`_remember_camera`；
     - `Step1WholeSlideMount.apply_camera / current_camera / source_changed / open`；
     - `Step0Page.apply_camera_snapshot`；
     - `ExploreController.jump_to / set_view_rect_l0`；
     - `pg.ViewBox.resizeEvent / updateViewRange`；
     - dock 重挂、底栏、`_hold_step1_channel_floor`。
   - 每条记录的字段：来源与 ROI、drawable 宽高与 DPR、ViewBox 几何、viewport 几何、世界中心 x / y、每设备像素对应的世界单位、可见世界矩形、当前金字塔层级、触发来源与原因、时间戳。
   - 采样时刻：切换后立即、0 ms、50 ms、500 ms。
   - 转场：Step0→1、1→3、3→1、1→0、0→3→0，外加 50 × (0→1→3→1→0) 的累积漂移。
2. **真实 GL（自动，每个进程最多约 4 个 GL 上下文）**
   - 用 xcb + D3D12 环境变量，每组转场单独一个进程。
   - 记录 GPU 层的 `ViewportSnapshot`、GPU 层几何、graphics viewport 几何、ViewBox 几何。
   - **比例**：直接比较两页的相机值（`scale`）与 ViewBox → 屏幕的变换数值，以及 GPU 层实际的输出尺寸与 ViewBox 尺寸之比。不用图像估算比例。
   - **平移**：同一相机下截取 Step0 与 Step1/3 的画面，用相位相关测平移。
   - **视觉确认**：截图时锁定**同一个原始单通道**（不经校正）和**同一个显示窗口**（同样的最小值、最大值和 gamma），关闭 fusion 和 mask，存叠加图与差分图。这样两页不同的合成方式不会污染比较结果。
   - 这一步用来证实或排除 C1。
3. **真机（需用户操作，见 §8 裁定 1）**
   - 仿照 `scripts/run_with_roi_clip_probe.py`：包装后启动真实应用，只写日志到 scratch。
   - 用户在真机上完成转场，并在 Windows 125 % / 150 % 缩放下各做一遍，用来看 C6 和 DPR。
   - 探针不写项目；应用本身的正常行为照旧。

产出：`docs/v16_A0_viewer_shift_report.md`。对每个转场给出 Δ中心 x / y、Δ比例、**第一个改动相机的调用**，以及根因。只有证据表明局部修改不够时，才提通用 CameraState（v2.1 §2.2、停止规则 1）。

### A0-2 存储决策基准

新脚本 `scripts/bench_v16_a0_storage.py`。

0. **数据集身份（已存在，不再生成）**：
   - 合成大图：`~/fusionflux/synthetic/synthetic_2x2_mirror.ome.tif`，3 716 983 574 B，sha256 `b5f73eec43e0d096cd5027beea130dc7911388e110d981a6da6e950945f1d164`，由 `scripts/make_synthetic_mosaic.py make` 生成（`bf3af2c`，v2.1 §8 执行记录）；`verify --full` 21/21 通过。
   - 源：`~/fusion_data/cropped_region.ome.tif`，929 834 060 B，sha256 `fe3be2ba8794808396236db09d9c14cb42fe0846d66bf851e2e41e7494c175e5`（只读）。
   - 基准开跑前重新核对两个 sha256，不一致就停。

1. **生成 NGFF 0.4 副本**（写到 `~/fusionflux/bench_a0/`，用现有 zarr 2.18，不装新包）：
   - 轴 `c`（type `channel`，无单位）、`y`、`x`（type `space`，unit `micrometer`）；三层形状与 TIFF 完全相同；
   - 第 k 层的 `coordinateTransformations` = `[{"type": "scale", "scale": [1, PhysicalSizeY × H0 / Hk, PhysicalSizeX × W0 / Wk]}]`，即物理尺寸乘以该层的真实比例（比如 cropped_region 第 1 层 y = 0.50686 × 15437 / 3859），**不写成无量纲的比例又声称单位是 µm**；读不到 PhysicalSize 的源就不声明单位，并在报告中注明；
   - 带 `omero` 通道元数据；
   - 分块 (1, 512, 512)；
   - 压缩方案见裁定 2。
   - 对 `cropped_region`（真实组织）和合成大图各做一份，并记录入库时间、峰值 RSS 和体积比。
   - 这是基准用的一次性写法，**不是**产品的 `NgffSource` 或入库操作（那属于 A2b）。
   - 附一个最小的 NGFF 0.4 元数据自检（脚本内，不加依赖），至少锁住：
     - `multiscales[0].version == "0.4"`；
     - `axes` 的 name、type、unit，以及顺序；
     - `datasets[].path` 与实际的数组一一对应；
     - 每层只有一个 `scale` 变换，长度等于轴数；
     - 每层数组的形状等于 TIFF 对应层的形状；
     - scale 语义：`scale[k] / scale[0]` = 该层的真实比例（容差 1e-12），`scale[0]` = PhysicalSize；
     - `omero.channels` 的数量与名称等于 OME 的通道。
2. **测量项（预注册，开跑后不改；要改须先报用户）**
   - **公共规则**：
     - 随机种子 16；
     - 每项先做 1 次不计时的预热，再**重复 3 次**，冷、热各 3 次；
     - **冷**：每次运行前对所涉全部文件（TIFF 本身、zarr 的每个分块文件）做 `posix_fadvise(DONTNEED)`，用 `fincore` 确认驻留为 0；**热**：紧接一次完整运行之后；
     - **统计量**：每次读取的耗时取中位数与 p95，报告 3 次运行各自的值及其中位数；
     - **记录**：线程数、Blosc 内部线程设置、进程 CPU 时间 ÷ 墙钟时间、峰值 RSS。
   - **公平的读取实现（两种格式同等并发）**：
     - TIFF 一律用产品现有的读取器；
     - NGFF 用同样线程数的候选读取器：数组只打开一次；按分块对齐把请求拆成 512² 块，放进 8 线程池并行读；Blosc 内部线程关闭（`numcodecs.blosc.use_threads = False`），总并发同为 8；
     - 不拿串行的 `zarr[:]` 与 8 线程 TIFF 比。
   - **随机 ROI**：
     - 第 0 层，尺寸 512²、2048²、4096² 三档，每档 **30 次**；
     - 位置：在第 2 层 DAPI > 0 的组织掩膜内均匀抽中心，用固定种子，ROI 不越界；
     - 每次随机抽 1 个通道，用固定序列；
     - TIFF 走 `RawTileProvider.read_region`（per_thread 句柄），读完拆成 512 分块，用 8 线程；NGFF 走候选读取器。
   - **视口加载（平移 / 缩放）**：
     - 固定视口 1600 × 1000 逻辑像素；
     - 三个缩放档：第 0 层 scale 1.0、第 1 层 scale 0.25、第 2 层 scale 0.0625，层级按 `pick_display_level` 的规则选；
     - 固定平移路径：从组织中心出发，右 10 步、下 10 步，每步移动视口宽 / 高的 25 %；
     - 每步只读新进入视野的 512 分块，前面读过的分块视为已缓存，模拟应用 LRU；
     - 指标：每步耗时，以及整条路径的总耗时。
   - **通道切换**：
     - 第 1 层固定视口（组织中心）；
     - 固定通道序列 `[0, 5, 12, 17, 3, 22, 8, 26, 14, 1]`；
     - 指标：每次切换读完全部可见分块的耗时。
   - **29 通道顺序访问**：组织中心的 4096² 窗口，第 0 层，按 0..28 的顺序逐个通道读完。
   - **Step1 fusion 读**：
     - 按 `FullFusionWorker` 的读法：区域取组织中心 8192²，切成 2 × 2 块；
     - 每块并行读 6 个通道（DAPI + 固定 5 个 marker），8 线程；
     - TIFF 走产品的 `OMETIFFLoader.read_region(normalize=False)`，每次调用重开 TiffFile；NGFF 走候选读取器。
   - **Step4 顺序扫描**：
     - 整张图，第 0 层，4096² 块按行扫描，29 个通道；
     - TIFF 走产品的 `TiffTileReader`，8 线程；NGFF 用候选读取器按同样的 4096 块读，8 线程。
   - **另记**：压缩后体积、NGFF 体积 ÷ 源体积、入库时间、入库峰值 RSS。
   - **两个数据集**：合成大图跑全部项目；cropped_region 跑全部项目，并额外跑一遍 Blosc-zstd 副本。
   - **替代方案「原始数据保持 OME-TIFF，只有衍生品用 NGFF」**：
     - 数据来自 test1 已有的校正 zarr，**先复制到 bench 目录再读**，原项目只读；
     - 比较今天「第 1 层运行时归约」（`reduce_corrected`）与「预先存好的第 1 层」读取的差别。
3. **采纳规则**：按 v2.1 §2.3 事先写死的规则判定，报告里逐条列出结论。主判定用合成大图的 Blosc-lz4 副本：
   - 收益：**冷随机 ROI 2048²** 的中位数之比，或**第 0 层视口平移的冷总耗时**之比，至少一项 ≥ 1.5×（TIFF ÷ NGFF）；
   - 不回退：**Step4 顺序扫描**、**Step1 fusion 读**的冷、热中位数，NGFF 都不慢于 TIFF 的 1.10 倍；
   - 体积：报告 NGFF ÷ 源的比例，由用户判断能否接受；
   - 其他尺寸与项目只作参考。
4. **Odon 参考基线（可选，不参与判定）**：
   - 只在 Odon 已经可用、且能直接打开同一个 NGFF 副本时才做：记录热打开到可用的时间、未缓存平移是否清晰、缩放是否清晰、峰值 RAM；
   - 为此不安装任何包、不改任何东西；做不到就在报告里写「未做」及原因。

产出：`docs/v16_A0_storage_benchmark.md`，含原始数字 JSON 的路径。

### A0-3 三份契约草案

写入 `docs/v16_contracts_draft.md`，**只是草案，在 A3 冻结**：

- **PixelSource**：
  - v2.1 §5.1 的最小接口，逐条对应今天各消费者实际用到的调用（§2.2 表）；
  - `level_downsample` 分为「几何用的每轴真实比例」和「仅作键用的取整值」两种；
  - `source_identity` 沿用今天的路径 + 指纹，另留 `slide_id` 位置。
- **坐标**：
  - 五个坐标系的名称与变换；
  - 整数下标 = 像素中心，viewer 世界坐标 = global_pixel + 0.5；
  - bbox 半开，并写明与今天 `[y0, y1, x0, x1]` 的对应；
  - 层级按真实比例；
  - µm 从 OME 读取，读不到就标为不可用；
  - `transforms.json` 的草稿格式。
- **对象表**：
  - `cells.parquet` / `regions.parquet` v1 的列与类型；
  - 从今天的 manifest、roi_index、运行 meta、`label_store`、Step4 h5ad 映射到新 ID 的对照表：`region_id` ← `roi_id`（不是 `roi_name`）；`segmentation_run_id` ← `run_id`；`slide_id` 另定；
  - 新旧布局对照（v2.1 §2.4）；**不迁移旧项目**。

## 4. 白名单（全部是新文件，外加一处计划文档记录）

- 新：`scripts/diagnose_v16_a0_camera.py`
- 新：`scripts/bench_v16_a0_storage.py`
- 新：`docs/v16_A0_viewer_shift_report.md`
- 新：`docs/v16_A0_storage_benchmark.md`
- 新：`docs/v16_contracts_draft.md`
- 改：`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.1.md`，只加 A0 执行记录
- 本申请文件本身

输出数据（不入库）：`~/fusionflux/bench_a0/`、会话 scratch。

## 5. 不改的范围

- **产品代码一行都不改**：ui/、viewer/、core/、workers/、seg_runner/、utils/ 都不动；tests/ 也不改。
- 不装任何包（pyarrow、ome-zarr-py 等都不装），不升级 zarr。
- 不写 `~/fusion_data`。test1 的校正 zarr 只复制出来读；复制件里如果有绝对路径的元数据，不经过产品路径打开。
- 不修 C1–C6 中的任何一条，修复属于 A1，要单独审批。
- `image_mpp = 0.5` 和半像素约定只在草案里列出，不改。
- 不做 A0.5；它要另写申请。

## 6. 风险

| 风险 | 对策 |
|---|---|
| D3D12 的 GL 上下文上限 | 每组转场单独一个进程，每个进程不超过约 4 个上下文 |
| 离屏测试里弹出模态框 | 沿用现有测试装配方式；遇到对话框就记为失败，不点击 |
| 磁盘 | NGFF 副本约 4–5 GB（合成图）+ 约 1 GB（原图）；开跑前后执行 `df -h /mnt/c`，C: 低于 30 GB 就停；报告写完后按裁定 4 删除 |
| 内存 | 基准一次只读一个通道的块，峰值目标 < 4 GB，并记录下来；不和真实引擎回归同时运行 |
| 「冷」读不够冷 | 用 `fincore` 验证客户机层面已清空；报告注明宿主机缓存的影响，不把它说成物理盘冷读 |
| 插桩改变时序 | 包装只记日志；会拿有插桩和无插桩的截图结果对照 |
| 断网导致后台任务被杀 | 长任务用 `setsid nohup` 启动，写完成标记文件 |

## 7. 验收门（v2.1 §2.5）

- [ ] 用日志证明 viewer 位移的根因：每个转场的 Δ中心 / Δ比例 / 第一个改动相机的调用；C1–C4 逐条证实或排除
- [ ] 基准报告存在，并已按采纳规则判定
- [ ] PixelSource、坐标、对象表三份草案存在
- [ ] 没有改产品代码：`git status --short`（含未跟踪文件）+ `git diff --name-only` + 提交前 `git diff --cached --name-only`，改动与未跟踪路径 ⊆ 白名单（用户本地保留的 v1 / v2 Lean 计划文件及其 `:Zone.Identifier` 除外，它们从 A0 开始前就在）

## 8. 用户裁定（2026-09-29，全部按建议批准）

1. **真机日志（A0-1 第 3 步）**：需要你在真机上按清单做一遍转场，并在 125 % / 150 % 缩放下各做一遍。建议做，因为 C6 只能在真机上看到。可以放在自动部分之后再做。
2. **NGFF 的压缩方案**：建议合成大图只做 **Blosc-lz4**，与今天所有 zarr 产品的默认设置一致；`cropped_region` 额外做一份 **Blosc-zstd**，看体积和速度的取舍。不用 imagecodecs 的 LZW（需要注册，别的工具读不了）。
3. **分块**：建议 (1, 512, 512)，与 TIFF 分块和 viewer 的 512 分块一致。Step4 的 4096 块会跨 64 个分块，这正是要测的代价。
4. **NGFF 副本的去留**：建议报告写完后删除 NGFF 副本，只保留数字 JSON 和报告。合成大图本身保留，后面 A2 / Gate 3 还要用。
5. **三份报告和草案**是否随 A0 一起入库：建议入库。
