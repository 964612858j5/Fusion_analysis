# v16 A9 申请 v1.3：以性能门槛收敛视图

日期：2026-10-06。依据：架构基准 v2.4 §20（A9）、§21 裁定（C1–C7，2026-09-30）；用户 2026-10-05 / 10-06 裁定（PA-5b：视图类卡顿交给 A9）。

**本文件是 §20.3 工作顺序的第 1 步：写明判据、建议门槛和测量方法。** 之后按以下顺序进行：
- 第 2 步：只读基线测量。只允许加测量工具，见 §3 的 A9-M 块。
- 第 3 步：用户根据基线定下最终门槛并锁定。
- 第 4 步：开始实现。优化的结果不能反过来修改门槛。

用户批准之前不改任何代码。

---

## 0. 结论摘要

1. **今天的视图已经具备工具箱里的大部分机制**（§1）。最粗层垫底、粗层常驻加内存核算、代际取消、GPU 着色器做亮度/伽马/合成，这些都已有。缺的主要是三样：
   - 视图仍然绕过 `PixelSource`，直接读 `RawTileProvider`。基准 v2.4 §20.3 规定"视图显示的每个像素都经 `PixelSource`"，所以**这次迁移是 A9 的必做项**，不是可选优化，需要 P0 和 AGENTS.md 第 5 条两项批准（§6、§7）；
   - 未激活通道的粗层没有后台预热；
   - GPU 请求按光栅顺序发出，不是从中心向外。
2. **冲突 C2 不存在**：Step1/Step3 的 GPU 片元着色器（`ui/shaders/step1_gpu.frag`）已经负责窗口、伽马和合成，不需要新增"仅显示"例外。
3. **门槛目前无法测量**（§2）：
   - 没有覆盖/驻留掩膜（A9-1/2/3 依赖它）；
   - 读取器没有读取计数（A9-5/6 依赖它）；
   - `viewer/` 里没有任何计时点；
   - 合成拼图只有 3 层，A9-7 要求的 7 层拼图还不存在。
   - 因此第 2 步之前先要做一个只加测量工具的小块 **A9-M**（§3），需要单独批准。
4. **已有的真实测量**（PA-5a，合成图，2026-10-05）可以作为预览：
   - 进入 Step3：0.5 s，之后约 1.5 s 无响应；
   - 再次进入 Step1：0.7 s，之后约 2 s；
   - 其余时间里另有 20 多次 200–500 ms 的界面卡顿。
   - 这些都远超 A9-4 建议的 32 ms。

## 1. 现状盘点：工具箱逐项（只读，2026-10-06）

### 1.1 视图栈

一共有 4 套栈，都由同一组类构成：`ExploreView` / `ExploreController`、`TileScheduler` 和 `RawTileProvider`。

| 栈 | 建立位置 | 读取器 / 缓存 | 共享 |
|---|---|---|---|
| Step0 全图 | `ui/step0/step0_explore_tab.py:277` | 自己的 `RawTileProvider` + 原始 / 校正 LRU | 与 compare 共享缓存、概览和底图 |
| Step0 compare（3 个面板） | `ui/step0/compare_strip.py:223` | 1 个新 `RawTileProvider` + 1 个调度器 | 借用全图的缓存 |
| Step1 | `ui/step1_viewer_host.py:400`（`Step1TileProvider` 包装） | 自己的一整套 + GPU 层 | 无 |
| Step3 | 同一个类的第二个实例（`labels=True`） | 又一整套 + GPU 层 | 无 |

Step0 不使用 GPU 路径。

### 1.2 工具箱逐项

| # | 机制 | 现状 | 证据 | 若要做的代价 |
|---|---|---|---|---|
| 1 | 直接 TIFF 图块解码器作为 `OmeTiffSource.read_native_tile()`；视图经 `PixelSource` 读 | **部分**：契约已有（`core/pixel_source.py:160–167`，`OmeTiffSource.native_tile_shape` 在 `sources/ome_tiff.py:141`），但 `read_native_tile` 走默认的 `read_region`（zarr 切片），**视图全部直接读 `RawTileProvider`** | `explore_view.py:3231/3577/3599/4120`、`scheduler.py:534`、`step1_viewer_host.py:122/149` | 解码器移植属中等（来自探针 `scripts/probe_v16_a2b_ngff.py:234` `TifAdapted`，已验证逐位相等、约 2 倍速）；4 套栈改走 `PixelSource` 是**最大**的一项 |
| 2 | 最粗层垫底 | **已有**：GPU 路径每通道有完整的最粗层平面集（`step1_gpu_binding.py:12–24, 463–470`）；Step0 用"上一层 + 概览底图" | | — |
| 3 | 粗层常驻 + 内存核算 | **已有，但只覆盖激活通道**：GPU 粗层每通道 32 MB、细层 48 MB、纹理总量 512 MB（`core/resource_tiers.py:74–76`），超预算的通道拒绝绘制并在角标里提示；**未激活通道的粗层没有后台预热** | `step1_gpu_binding.py:459–475` | 后台预热属中等，可复用 `multichannel_prefetch` 的排序 |
| 4 | 原生 dtype 的解码缓存；着色器做亮度和合成 | **部分**：着色器已做窗口、伽马和合成（**C2 不存在**）；Step0 缓存是原生 dtype，但 **Step1/Step3 的缓存和纹理都是 float32**（`step1_viewer_host.py:122–131`、`step1_gpu_layer.py:355–367` `GL_R32F`），用 NaN 表示"缺"，所以占用是原生数据的 4 倍（uint8 切片） | | 中等（约 1–2 天）：整数纹理加单独的有效掩膜，改上传代码和着色器读取，结果逐位不变 |
| 5 | 视口外圈预取 | **部分**：Step0 有 1 圈原始预取和平移方向预取；GPU 路径没有 | `explore_view.py:1717–1719, 897–913` | 低到中 |
| 6 | 预热更细一层 | **无** | | 低到中 |
| 7 | 缩小时的层级下限 | **部分**：Step0 有校正底图；GPU 的最粗层覆盖整张切片 | `explore_view.py:3487, 840–841` | — |
| 8 | 中心优先 + 过期与取消 | **部分**：Step0 和调度器已有中心优先和代际取消；**GPU 的 `_key_order` 按光栅顺序**（`step1_gpu_binding.py:935`） | | **低**（一个函数） |
| 9 | 高通道数时自适应降级 | **部分**：超预算时拒绝绘制（失败即关闭），没有自适应 | `step1_gpu_binding.py:141, 564–572` | 低到中 |
| 10 | 主线程从不等 IO | **部分**：建栈时同步读整层概览（`explore_view.py:3323`，文档实测 p95 293 ms）、compare 和全图建栈时的 `load_overview`、`RawTileProvider(...)` 在界面线程打开 TIFF 并解析 OME-XML（`step1.entry.raw_provider`），CPU 回退路径的 `_load_overview_for_the_cpu_picture` | | 低到中 |

另外：`ui/step1_gpu_binding.py:1–4` 的文档仍写着"test-only adapter"，但生产代码已经在用（`step1_viewer_mount.py:56, 442`）。A9 会顺带改正这处文档。

## 2. 测量工具：已有的和缺的

| 判据 | 已有 | 缺 |
|---|---|---|
| A9-1 首个可用粗帧 | `step1.entry.*` 计时点（建栈、GPU 启动、GL 初始化） | 覆盖掩膜（"视口被激活通道的粗层完全覆盖"）；从打开命令到首个完全覆盖帧的计时；多次重复以求 p95 |
| A9-2 无缺块 | Step0 的回退面积统计（`_visible_tiles`） | 每帧的覆盖/驻留掩膜（跨所有层级）、每次绘制时评估、缺块帧计数 |
| A9-3 拖动 / 缩放后目标层到位 | 交互基准 `scripts/benchmark_step1_gpu_interaction.py`（离屏） | 最后一次输入的时间戳，以及掩膜报告"可见块全部到目标层" |
| A9-4 无 > 32 ms 阻塞 | `perf_trace` 5 ms 心跳，阈值 25 ms，记为 `gui.gap`；分析器 `docs/perf_timeline/analyze_perf_log.py --gaps` | 视图代码里没有计时点，卡顿无法归因；`start_heartbeat` 不能设阈值；`[gui-watchdog]` 是 2 s 级，不适用 |
| A9-5 调亮度 0 次读取 | `intensity.*` 计时点 | 读取器上的读取计数（`RawTileProvider.open_count` 只统计打开文件的次数） |
| A9-6 切换通道 | 交互基准的 `new_channel_timeline`、缓存命中统计 | 同上的读取计数和掩膜；预热完成时间 |
| A9-7 资源（12 GB 档） | `scripts/measure_a5.py`、GPU 纹理预算统计 | **7 层拼图**（`make_synthetic_mosaic.py` 没有金字塔倍率选项）；各缓存声明上限的汇总表（A5 清单已有大部分） |
| A9-8 无镜头漂移 | `test_v16_zero_drift.py`（50 次步骤往返）、`test_v16_a7_camera_owner.py` | 50 次拖动 / 缩放 / 切换通道的脚本 |

**测量方式**：
- 离屏基准收不到真实的帧交换，所以 A9-1、A9-3、A9-4 必须在**真实显示**（WSLg）上测。
- 合成图的 OME 金字塔目前是 3 层（L0 30874×32430、L1 7718×8107、L2 1929×2026，×4 倍率）。

## 3. A9-M：测量工具块（需单独批准，不改视图行为）

只加测量，视图的行为和像素不变。默认关闭，只有 `BLOCK01_PERF=1` 时生效。

1. **覆盖掩膜**（新模块 `viewer/coverage_probe.py`）：判断**真正画出来的那一帧**，而不是"已驻留"（codex 意见 2）。
   - 按激活通道分别判断，只算当前数据源、当前代际的块；过期或隐藏的块不算。
   - GPU 路径要计入平面的有效掩膜（NaN 表示缺，`step1_gpu_binding.py:756`，着色器 :26–27）。
   - CPU 路径把概览和底图的合法回退算作"已覆盖"。
   - 只在切片 / ROI 的有效范围内判断。
   - 时间戳取"呈现"：GPU 路径用 `frameSwapped`，CPU 路径用绘制完成，而不是上传或开始绘制的时刻。
   - 输出每帧的 `coverage` 标记：粗层是否完全覆盖、是否全部在目标层、缺块数。
   - 只读这些数据结构，不改动它们。
2. **读取计数**：放在**真正的读取边界上**，不重复计数（codex 意见 3）。
   - 原始图：只计在 `RawTileProvider.read_region`（`read_tile` 内部也调用它，:336）。
   - 校正 / 派生产物：计在 `viewer/step1_source.py:593、632` 的读取处，以及 `CorrectedZarrSource` 的读取处。
   - 每次读取标明来源（原始 / 校正、通道、层级）。
   - 累计计数器只是整数；计时开启时另发 `read` 标记。读取本身不变。
3. **视图计时点**：在建栈、概览读取、GPU 上传、首帧绘制和切换通道处加 `perf_trace.span`，方式与 PA-5a 相同，开启时才生效。
4. **心跳阈值**：`start_heartbeat` 接受 `gap_ms` 参数，A9 测量时用 16 ms，以便看清 32 ms 附近的情况。
5. **脚本化测量驱动**（`scripts/a9_drive.py`，加环境变量 `BLOCK01_A9_SCRIPT`）：
   - 在真实程序、真实显示上，用 QTimer 按固定顺序执行：打开切片 → 首帧 → 平移 / 缩放序列 → 切换通道 → 调亮度 → 切换步骤；每组重复 N 次（建议 20 次）以求 p95。
   - 每个阶段都打标记，结果写进 perf 日志。
   - 用户只需启动一次，不必手动操作。
   - 测量语义（codex 意见 4）：
     - QTimer 会被卡顿推迟，所以计划时刻和实际送达时刻**分开记录**，延迟从实际送达算起；
     - 冷打开指程序启动后第一次打开切片，热打开指再次打开（WSL 里不清操作系统页缓存，这一点写进报告）；
     - 断言"0 次读取"之前，先等后台读取全部结束；
     - A9-8 统计镜头的"回写"次数（复用 A7 的审计），不比较最终位置——拖动本身就会移动镜头。
     - 现有的步骤切换不变量继续保留。
6. **分析脚本**（扩展 `docs/perf_timeline/analyze_perf_log.py`）：直接输出 A9-1 到 A9-6 的 p50/p95、缺块帧数、读取次数和最大卡顿。
7. **7 层拼图**：`make_synthetic_mosaic.py` 现在的层数取自源图（:136–139），所以只加倍率选项仍然只有 3 层（codex 意见 6）。
   - 需要增加 `--pyramid-factor 2` 和层数规则：一直减半，直到最长边小于 512。
   - `verify` 也要按新的层数检查。
   - 层尺寸统一用 `(n + 1) // 2` 取整：30874×32430 → … 共 7 层以上，具体数值以生成器的输出为准。
   - 新文件约 4.6 GB（原文件 3.7 GB，基准 C5 估算约多 1 GB）。生成前先检查 C: 的剩余空间。
8. **A9-8 脚本**：并入第 5 项的驱动：50 次拖动、缩放和切换通道，之后检查 A1 / A7 的不变量（复用现有审计）。

9. **测量本身的开销**（codex 意见 7）：
   - 每帧的掩膜和每次读取的记录都限量、轻量；
   - 日志丢弃的条数写进报告；
   - 计时开 / 关的像素逐位比较之外，再比较一次耗时。

**白名单（A9-M）**：
- 新增：`viewer/coverage_probe.py`、`scripts/a9_drive.py`；测试 `tests/test_v16_a9m_coverage_probe.py`、`tests/test_v16_a9m_read_counters.py`、`tests/test_v16_a9m_neutral.py`。
- 修改：
  - `viewer/raw_tile_provider.py`、`viewer/step1_source.py`、`sources/corrected_zarr.py`（只加计数器）、`utils/perf_trace.py`（`gap_ms` 参数）；
  - `viewer/explore_view.py`、`ui/step1_gpu_layer.py`、`ui/step1_gpu_binding.py`、`ui/step1_viewer_mount.py`（只加计时点和掩膜挂钩）；
  - `docs/perf_timeline/analyze_perf_log.py`、`scripts/make_synthetic_mosaic.py`；
  - `ui/main_window.py` / `ui/step0/step0_page.py` 只加接线行。

**验收（A9-M）**：
- 计时关闭时，所有现有测试结果不变。
- 计时开启时，程序行为不变：视图的像素逐位相同（关 / 开对比），镜头测试全部通过。
- 新的 7 层拼图通过 `make_synthetic_mosaic.py verify`。

预计 1.5–2 天，计入 A9 的上限（开发 8 天 + 真机 2 天）。

## 4. 基线测量（第 2 步）

- **数据**：
  - 3 层合成拼图（现有，代表真实产品）；
  - 7 层合成拼图（新建，深金字塔压力测试）；
  - `cropped_region` 作为真实组织内容。
- **范围**（建议，请裁定）：
  - Step1 / Step3 的 GPU 视图（A9 的主战场）；
  - Step0 全图（CPU 路径）；
  - Step0 compare 面板只记录，不设门槛。
- **方式**：用户用 `BLOCK01_A9_SCRIPT` 启动一次，驱动自动跑完；我读日志，出基线报告。每份拼图大约 10–15 分钟。
- **资源**：同时挂 `measure_a5.py`（A9-7）。

## 5. 建议门槛（第 3 步由用户定稿锁定）

| # | 判据 | 建议门槛（基准 v2.4 原值） | 说明 |
|---|---|---|---|
| A9-1 | 首个可用粗帧 | p95 ≤ 300 ms | 从打开切片到视口被激活通道的粗层完全覆盖 |
| A9-2 | 导航中无缺块 | 0 帧 | 以覆盖掩膜判断，不看像素颜色 |
| A9-3 | 拖动 / 缩放后目标层到位 | p95 ≤ 150 ms | |
| A9-4 | 界面线程单次阻塞 | 从不 > 32 ms（若基线显示 < 16 ms 容易做到，就收紧） | **建议把步骤切换也纳入**：PA-5a 实测的进入 Step1/Step3 后 1.5–2 s 无响应，归在这一项 |
| A9-5 | 调亮度 / 对比度 / 伽马 | 0 次磁盘读取 | 断言 |
| A9-6 | 切换通道 | 已驻留时 0 次读取；界面线程不等待；预热完成后 0 缺块帧 | 另记录预热完成时间（29 通道），不设门槛 |
| A9-7 | 资源（12 GB 档） | 不 OOM；每个缓存不超过声明的上限 | 两份拼图都测 |
| A9-8 | 镜头漂移 | A1 零漂移、A7 零回写的不变量仍然成立 | 50 次拖动 / 缩放 / 切换通道 |

## 6. 实施顺序（第 4 步）

### 6.1 必做：视图改走 `PixelSource`（A9-P）
- 基准 v2.4 §20.3 的"数据路径"一节要求视图显示的每个像素都经过 `PixelSource`（codex 意见 1）。这是必做项，与工具箱第 1 项的"直接解码器"优化分开。
- 做法：新建一个提供方适配器，把 4 套栈的 `read_tile` / `read_region` / `level_shape` / `level_downsample` / `channel_names` / `warm_thread_handle` 接到 `OmeTiffSource`、`CorrectedZarrSource` 上。
- 门槛：每一层的像素都与旧读取器逐位相等，旧路径作为对照（基准 §0.4 规则 2）。
- 需要两项批准：
  - v2.2 §5.2 规定的视图消费者迁移需单独批准（P0）；
  - AGENTS.md 第 5 条：改视图的读取路径需要明确授权。
  - 两项都在 §7 请用户裁定。
- 放在优化之前做，后面的优化都建在它之上。

### 6.2 工具箱：先便宜的，每做一项测一次，达标即停
1. #10 把剩余的同步读取移出界面线程：建栈时的概览读取、`RawTileProvider` / 数据源的构造。
2. #3 未激活通道粗层的后台预热（A9-6 要求"切换通道永不缺块"，基线若不达标就必做）。
3. #4 原生 dtype 纹理（显存和内存约降到 1/4）。
4. #5 / #6 外圈预取和预热更细一层：先测清它们的内存和 IO 代价再决定，不做投机预取（codex 意见 5）。
5. #1 直接 TIFF 解码器，作为 `OmeTiffSource.read_native_tile()` 的实现（建在 6.1 之上）。
6. #8 / #9（中心优先排序、按通道数自适应降级）：按裁定 C3，**只有前面各项不能达标时才做**。GPU 光栅顺序这一项虽然便宜，也一样遵守这条（codex 意见 5）。

不做：
- 换文件格式；
- 改科学计算；
- Rust（只能走停止规则 10）；
- 拆分大文件；
- 重写视图。

## 7. 请用户裁定

1. 批准 **A9-M 测量工具块**（§3）的范围和白名单。
2. 基线范围：Step1/Step3 GPU 视图加 Step0 全图（建议）；compare 面板只记录。
3. 基线方式：脚本驱动（建议），还是人工按固定顺序操作。
4. 生成 7 层拼图（约 4.6 GB）。
5. A9-4 把步骤切换纳入（建议）。
6. 门槛在基线测完后再定（§20.3 规定的流程，不需要现在裁定）。
7. 实施顺序（§6）是否同意。
8. **批准视图改走 `PixelSource`**（§6.1）：v2.2 §5.2 的 P0 批准，以及 AGENTS.md 第 5 条对改视图读取路径的授权。实施前另交一份细化方案（适配器接口和逐位对照测试）。

## 8. 审核记录

- 2026-10-06 codex（gpt-6-astra low）审核 v1：结论 approve with changes。盘点结论核对无误（视图直读 `RawTileProvider`、`read_native_tile` 走默认实现、float32 / `GL_R32F`、着色器负责映射与合成、GPU 光栅顺序、建栈时同步读取）；把步骤切换纳入 A9-4 与 §20.3 一致；没有新增哈希。7 条意见已全部并入 v1.1：
  1. 高：`PixelSource` 迁移是必做项 → §0、§6.1、§7 第 8 项。
  2. 高：覆盖掩膜要判断真正画出的那一帧 → §3 第 1 项。
  3. 读取计数要放在真正的读取边界上 → §3 第 2 项及白名单。
  4. 驱动的测量语义 → §3 第 5 项。
  5. 实施顺序要遵守 C3，不做投机预取 → §6.2。
  6. 7 层拼图要改层数规则 → §3 第 7 项。
  7. 测量开销 → §3 第 9 项，以及白名单中的测试文件名。


## 9. 用户裁定（2026-10-07）

- 批准 **A9-M 测量块**、**脚本驱动的基线**、**视图改走 `PixelSource`（A9-P，必做）**。这一条同时给出 v2.2 §5.2 的 P0 批准和 AGENTS.md 第 5 条对改视图读取路径的授权。
- 本节的实施计划经 codex 审核、按意见修改后**直接执行**，不再交用户过目。
- **尚未裁定**：7 层拼图（约 4.6 GB）、各判据的门槛、A9-4 是否纳入步骤切换。因此在用户跑完基线、锁定门槛之前，**不做任何工具箱优化**（§20.3 第 4 条）。
- 用户最在意的是：进入 Step1 不够顺、拖动很卡。PA-5 交接过来的三个遗留点作为基线的头号场景：
  1. 进入 Step1 后约 3 s 卡在 Qt 内部，没有任何 Python 调用；
  2. 拖动时 GPU 绘制卡在 `glCheckError`，记录到一次 4.8 s；
  3. 组织预览的融合合成每次 2.2–2.7 s，期间界面卡 1–2 s。

## 10. 实施计划（v1.2）

### 10.1 A9-M：测量工具（行为中立）

所有新增内容只在 `BLOCK01_PERF=1` 时生效；关闭时不发事件，也不增加明显开销（计数器只是一次整数加一）。

1. **读取计数**（新模块 `viewer/read_ledger.py`）：
   - 进程内的累计计数，按来源分：原始图（`raw`）、校正产物（`corrected`）、整图概览（`overview`）；同时按通道和层级累计。
   - 计数点只放在真正的读取边界：
     - `RawTileProvider.read_region`（`read_tile` 会调用它，所以只计这里一次）；
     - `CorrectedRegion.read`（`viewer/step1_source.py`）。
   - 计时开启时另发 `read` 标记（来源、通道、层级、像素数、耗时）。
   - 提供 `snapshot()` 和 `delta(before)`，供测试和驱动断言"0 次读取"。
2. **视图计时点**（`perf_trace.span`，方式同 PA-5a）：
   - `Step1GpuLayer`：`submit`、`_render_overlay` / `_render_fusion`、纹理上传（`_TextureLru.prepare`）、`paintGL`；并用 `frameSwapped` 发呈现标记 `gpu.present`；
   - `Step1GpuBinding`：`_accept_result`、`_publish_current`、`refresh_display`；
   - `ExploreController`（Step0 全图）：`load_overview`、图块上屏；
   - `TissueComposeWorker`：一次合成的总耗时（在工作线程里）；
   - `TileScheduler`：读取、计算。
3. **心跳**：`start_heartbeat(parent, label, gap_ms=None)`，主窗口用环境变量 `BLOCK01_PERF_GAP_MS`（默认 25）。
4. **覆盖探针**（新模块 `viewer/coverage_probe.py`）：
   - 只读当前数据结构，判断**画出来的那一帧**：
     - GPU 路径：每个参与合成的通道，用当前代际已发布的平面（粗层完整集和细层平面），对照视口（裁到切片 / ROI 范围），算出"粗层覆盖率"和"目标层覆盖率"。平面的有效掩膜（NaN 表示缺）计为未覆盖。
     - CPU 路径：图元池中当前代际的块，加上概览和底图回退。
   - 在每次 `gpu.present`（GPU）或绘制完成（CPU）时评估，发 `coverage` 标记（粗层是否全覆盖、目标层覆盖率、缺块数）。
   - 评估成本有上限：视口块数 ≤ 几百，只做集合运算。
5. **驱动**（`scripts/a9_drive.py`，由 `BLOCK01_A9_SCRIPT=<场景文件>` 在主窗口启动后加载）：
   - 用 QTimer 按场景执行：等待打开项目、切换步骤、平移 / 缩放（直接调用视图的公开相机接口模拟拖动，每一步都是一次真实的相机变化）、切换通道、调亮度、在组织预览开着时拖动。
   - 每个动作都记录计划时刻和实际送达时刻。
   - 断言"0 次读取"之前，先等后台读取全部结束（调度器空闲，设上限）。
   - 场景用 JSON 描述，自带默认场景 `a9_default.json`，覆盖 §5 的各项，每组重复 20 次。
   - 驱动只在设置了环境变量时加载；没设置时完全不存在。
6. **分析**：`docs/perf_timeline/analyze_perf_log.py` 加 `--a9`，输出：
   - 各判据的 p50 / p95；
   - 卡顿列表及每次卡顿当时在跑的计时段；
   - 读取次数；
   - 缺块帧数。
7. **A9-8**：驱动里的 50 次拖动 / 缩放 / 切换通道，统计相机的写入次数，复用 A7 的审计计数：用户导航每次只写一次，程序的跳转只经跳转通道。
8. **中立性检查**（测试 `tests/test_v16_a9m_neutral.py`）：
   - 计时开 / 关两种情况下，视图读出的像素逐位相同（覆盖 Step0 和 Step1 的读取路径）；
   - 计时关闭时不产生任何事件；
   - 读取计数在开 / 关时都正确。

不做：7 层拼图，等用户裁定。

**白名单（A9-M）**：
- 新增：`viewer/read_ledger.py`、`viewer/coverage_probe.py`、`scripts/a9_drive.py`、`scripts/a9_default.json`；测试 `tests/test_v16_a9m_read_ledger.py`、`tests/test_v16_a9m_coverage_probe.py`、`tests/test_v16_a9m_neutral.py`、`tests/test_v16_a9m_drive.py`。
- 修改（只加计数、计时和挂钩，不改行为）：
  - `viewer/raw_tile_provider.py`、`viewer/step1_source.py`、`viewer/explore_view.py`、`viewer/scheduler.py`；
  - `ui/step1_gpu_layer.py`、`ui/step1_gpu_binding.py`、`ui/step1_viewer_mount.py`、`workers/tissue_compose_worker.py`；
  - `utils/perf_trace.py`、`docs/perf_timeline/analyze_perf_log.py`；
  - `ui/main_window.py`：只加启动驱动和心跳参数的接线行。

### 10.2 A9-P：视图改走 `PixelSource`（必做，用户已批准）

**原始图**：
1. `OmeTiffSource` 增加可选参数 `provider`：传入时直接用这个读取器，不再自己打开第二套句柄。TIFF 的元数据（原生图块尺寸、OME 通道名、物理尺寸）改为第一次用到时才读。不传时行为不变。
2. 新模块 `viewer/source_tile_provider.py`：`SourceTileProvider(source)`。
   - 对视图提供与 `RawTileProvider` 相同的接口（`read_tile` / `read_region` / `level_shape` / `level_downsample(_yx)` / `num_levels` / `num_channels` / `channel_names` / `channel_index` / `source_identity` / `warm_thread_handle` / `close` / `path` / `open_count` / `describe`）。
   - **像素一律经 `source.read_region`**。先按 `RawTileProvider` 完全相同的规则把请求裁到层级范围内（`raw_tile_provider.py:357–361`）；裁完为空时，返回同样形状和 dtype 的空数组，不去调用数据源（因为 `intersect` 遇到空范围会抛 `OutOfBounds`）；通道按 `RawTileProvider.channel_index` 换成整数后再交给数据源，保证通道命名规则不变。
   - 元数据和句柄预热交给数据源内部的读取器（数据源自己的事，视图不再直接打开 TIFF）。
3. 工厂函数 `open_viewer_source(path)`：
   - `raw = raw_tile_provider.RawTileProvider(path)`，按模块属性查找，测试里对 `RawTileProvider` 的替换仍然生效；
   - 返回 `SourceTileProvider(OmeTiffSource(path, provider=raw))`。
4. 三处生产代码的构造改用这个工厂：
   - `ui/step0/step0_explore_tab.py:277`
   - `ui/step0/compare_strip.py:223`
   - `ui/step1_viewer_host.py:400`

**校正产物（Step1/Step3）**：
5. `Step1SourceTable` 的默认打开方式改为 `CorrectedZarrSource`。
   - `CorrectedRegion` 改为经 `source.read_region(channel, 0, …)` 读取，返回值保持 float32，读取范围的裁剪规则不变。
   - **语义变化（记录在案）**：`CorrectedZarrSource` 按 Step4 的严格规则找组——只认这个 ROI 自己的组。原来的打开方式在找不到时会借用"第一个含有这个通道的组"。
   - 改后，视图与 Generate、Step4 一致：在 A2c 之后它们都只认这个 ROI 的组。找不到时显示"缺少产物"的提示，而不是借用别的 ROI 的像素。
   - 测试替身（`open_corrected` 参数）保留。

**对照门槛（逐位）**：
6. 新测试 `tests/test_v16_a9p_source_parity.py`：
   - 在小型合成 OME-TIFF（3 层）上，`SourceTileProvider` 与 `RawTileProvider` 的 `read_region` / `read_tile` 结果（数组、dtype、原点）逐位相同。覆盖每一层、边缘、越界、空范围、整数通道和名字通道。
   - 校正区域在新旧两种打开方式下逐位相同（ROI 自己的组存在时）。
7. 真实数据：在合成拼图和 proj4 上，对随机 200 个区域做新旧逐位比较（脚本，结果写进执行记录）。
8. `scripts/` 里的基准脚本不改，它们仍直接读 TIFF；它们不是视图。

**白名单（A9-P）**：
- 新增：`viewer/source_tile_provider.py`；测试 `tests/test_v16_a9p_source_parity.py`。
- 修改：`sources/ome_tiff.py`（可选读取器参数、元数据延迟读取）、`viewer/step1_source.py`（校正产物的默认打开方式）、`ui/step0/step0_explore_tab.py`、`ui/step0/compare_strip.py`、`ui/step1_viewer_host.py`（各改一行构造）。
- 若测试因构造方式的变化需要调整，只改环境搭建部分，不改断言。

### 10.3 顺序、审核与回归

1. 先做 A9-P，后做 A9-M，好让计数点落在最终的读取边界上。每一项都先过 codex 代码审核，再跑定向回归。
2. 两项都完成后跑一次全量回归，与基线比较。
3. 写执行记录和增长报告。
4. 准备好用户的基线测量：一条启动命令，场景文件和预计时长写清楚。

### 10.4 codex 审核计划 v1.2 → v1.3（2026-10-07，approve with changes，7 条全部采纳）

1. **高：校正路径不止读文件**。`_open` 同时带出产品身份（attrs）和已保存的粗层；而 `CorrectedZarrSource` 选组的规则不同，粗层附属文件的几何也不同。
   → **改为不切换到 `CorrectedZarrSource`**。新增派生数据源 `viewer/array_pixel_source.py` `ArrayRegionSource(PixelSource)`：
   - 包住**已经打开的**那个数组（一个通道、一个层级、带它在切片上的原点）；
   - `CorrectedRegion.read` 和 `CoarsePlane.tile` 都经它的 `read_region` 读像素；
   - 选组规则、身份和粗层校验全部不变（**不做语义改变**）；
   - 符合基准 §20.3 "校正或派生数据源"的写法。
2. **中：注入读取器时，整数通道不能要求读 TIFF**。`OmeTiffSource(provider=…)` 时：
   - 通道换算交给 `provider.channel_index`（读取器没有这个方法时原样传递），保留原始图的别名规则和整数通道行为；
   - 不读 TIFF 元数据，除非真的用到原生图块尺寸 / OME 名称 / 物理尺寸；
   - 关闭后即使是空范围的读取也照样拒绝。
   - 测试替身若不完整，只补环境搭建部分，文件已列入白名单。
3. **中：读取计数漏了粗层**。计数点改为放在 `ArrayRegionSource.read_region`，分别标 `corrected` / `coarse`，加上原始图的 `RawTileProvider.read_region`；每次读取只计一次。另加用途标记（例如 `overview`），但不重复计数。
4. **高：覆盖要对应真正画出的那一帧**。
   - GPU：在 `Step1GpuLayer.submit` 用**实际提交**的描述（每个通道选中的平面）计算覆盖；每个平面到达时一次性算一个 16×16 的有效格（NaN 算无效），之后不再重复算；视口按 64×64 的格子判断覆盖。每帧代价有上限：平面数 × 256。
   - 帧编号随提交递增，下一次 `frameSwapped` 发呈现标记时带上这个编号，覆盖结果就对应到这一帧。
   - CPU：只算可见、不透明、当前代际的图元，加上已载入的概览和底图，同样按 64×64 格判断；呈现时刻取视口的绘制事件之后。
5. **中：驱动的拖动要走真实输入**。
   - 拖动和滚轮场景向视图的视口**投递真实的鼠标和滚轮事件**，走用户手势的同一条路径；"程序直接设置相机"的场景单独标明。
   - "画面稳定"的定义：调度器空闲，没有待送达的结果，最后一次提交之后已经呈现过一次，并且覆盖到了目标层（或超时）。仅仅调度器空闲不算稳定。
6. **中：中立性检查要更全面**。
   - CPU 视图在计时开 / 关时截屏逐像素比较；
   - GPU 画面的截屏比较放进硬件开关测试（`BLOCK01_REQUIRE_STEP1_GPU=1`），由驱动在真机上再做一次；
   - 镜头不变量照常测试；
   - 计数和覆盖探针的单次开销用微基准测试断言有上限（每帧小于 1 ms）。
7. **低：白名单封闭**：
   - 新增：`viewer/array_pixel_source.py`、`scripts/a9p_parity.py`；
   - 可能需要调整环境搭建的现有测试：`test_step0_explore_tab.py`、`test_step0_floor_prefetch.py`、`test_step0_cache_sharing.py`、`test_step0_method_prefetch.py`、`test_step1_gpu_overview_skip.py`、`test_step0_original_after_cached_switch.py`、`test_step0_compare_tiles.py`、`test_step1_source*.py`（只改替身，不改断言）。

§10.2 第 5 条（改用 `CorrectedZarrSource`）以本节第 1 条为准，原写法作废。
- 补充（codex 审核 A9-P 代码）：白名单加入 `tests/test_v16_pixel_source.py`——只在 `MIGRATED` 名单里登记这次批准的三个新使用者，断言不变。

## 11. 执行记录（2026-10-07）

### 11.1 A9-P：视图改走 `PixelSource`
- 新增 `viewer/source_tile_provider.py`：`SourceTileProvider`，以及工厂函数 `open_viewer_source`。
  - 像素读取经 `OmeTiffSource.read_region`；切片读取经 `OmeTiffSource.read_provider_tile` 交给读取器自己的 `read_tile`，对 `RawTileProvider` 来说就是裁剪后的 `read_region`。
  - 裁剪、空范围（包括起止颠倒的范围）、已关闭和非法通道的处理，都与 `RawTileProvider` 相同。
- `sources/ome_tiff.py` 增加注入模式：`provider=` 参数；元数据延迟读取；通道原样交给读取器，由读取器按自己的规则解析。
- 新增 `viewer/array_pixel_source.py` `ArrayRegionSource`：`CorrectedRegion.read` 和 `CoarsePlane.tile` 经它读取。选组规则、身份和粗层校验都不变。
- 3 处视图构造改为调用 `open_viewer_source`。`tests/test_v16_pixel_source.py` 的 `MIGRATED` 名单登记这 3 个新使用者。
- 逐位对照：
  - 单元测试覆盖每一层、边缘、越界、空范围、颠倒范围、整数和名字通道、切片、校正区域和粗层；
  - 真实数据（合成拼图、cropped_region）共 1200 个随机区域，0 处不同。修正通道传递和切片路由之后又重跑一次，仍是 0 处不同。
- 回归中发现并修正（测试替身暴露的语义差异）：
  1. 切片读取原本被转成了区域读取，测试替身分别统计这两种读取，因此计数对不上 → 改走读取器自己的 `read_tile`；
  2. 通道原本先换算成整数再交给读取器，名字式的测试替身因此出错 → 改为原样传递。
- codex：两轮。第一轮 2 条（颠倒范围、白名单登记），已修；第二轮无新意见。

### 11.2 A9-M：测量工具
- 新增：
  - `viewer/read_ledger.py`：按来源累计读取次数，并统计正在进行中的读取；
  - `viewer/coverage_probe.py`：抽样式覆盖判断（256×256 个采样点归到 64×64 个格子，判断范围裁到 ROI 的矩形和多边形；画面应合成却还没有数据源的通道算作未覆盖；目标层取当前选中的层）；
  - `scripts/a9_drive.py` 和 `scripts/a9_default.json`：脚本驱动和默认场景；
  - `docs/perf_timeline/analyze_perf_log.py --a9`：分析报告。
- 接线（计时关闭时只多一次环境变量判断）：
  - 读取计数挂在 `RawTileProvider.read_region` 和 `ArrayRegionSource.read_region`；
  - GPU 层计时 `gpu.submit` / `gpu.upload` / `gpu.paint`，按每次提交实际合成的内容计算覆盖，在 `frameSwapped` 时连同帧号一起发出；
  - GPU 绑定计时 `gpu.accept` / `gpu.publish` / `gpu.refresh`；调度器计时 `sched.read` / `sched.compute`；
  - Step0 的 `ExploreController` 计时 `explore.load_overview`，绘制之后发覆盖标记；
  - 心跳阈值可用 `BLOCK01_PERF_GAP_MS` 设置；主窗口在开启计时并设置了场景时加载驱动。
- 驱动：
  - 拖动和滚轮向视图中心下方的控件投递真实的鼠标、滚轮事件，并记录 Qt 实际处理的时刻；
  - 判定"稳定"：没有正在进行的读取、300 ms 内没有新读取、所有视图的调度器空闲，并且这次动作之后，当前视图有一帧已经呈现、并在目标层完全覆盖；超时单独记为超时；
  - 读取次数和相机写入次数都相对动作执行之前计算；
  - 相机写入在类层面计数（`CameraOwner` 使用了 `__slots__`），只在驱动运行时这样做。
- 分析报告把"呈现延迟"（最后一次被处理的输入 → 目标视图第一帧完整画面）和"稳定延迟"（含 300 ms 静默）分开给出，另外列出超时、每次动作的读取次数、手势过程中的程序跳转、超过阈值的卡顿，以及完全覆盖之后又出现缺块的帧数。
- codex：两轮。第一轮 6 条（稳定判定、粗层目标、缺失通道、覆盖精度与裁剪、延迟口径、场景覆盖面），第二轮 4 条（调度器查找路径、基线时点、按视图过滤、亮度动作），全部已修。
- **已知局限（如实记录）**：
  1. 覆盖是抽样判断，窄于视口 1/256 的缺口可能漏掉；
  2. Step1 和 Step3 的 GPU 帧都记为 `gpu`，要靠"只有屏幕上的那个视图在提交"来区分；
  3. WSL 里不清操作系统的页缓存，所以"冷打开"指的是程序冷启动后第一次打开。

### 11.3 基线测量（等用户执行）
- 启动：`! ~/fusionflux/bench_a9/run_baseline.sh`；
- 打开项目 `bench_rm/accept/a5`，选工作区 `full_wsi_20261005_163058_e65b`，然后不要再动鼠标和键盘；
- 看到 `[A9] scenario finished` 后关闭程序；
- 分析：`python3 docs/perf_timeline/analyze_perf_log.py bench_a9/baseline_<时间>.log --a9`；
- 7 层拼图（需用户裁定）之后再测一次。

### 11.4 全量回归（2026-10-07，提交 `7fe0e93`，250 个模块，`bench_a9/full_reg/`）
- 共 19 个失败用例，与 PA / RM / A7 / A8 回归的失败清单逐条比较，**没有新失败**。
- `test_step0_channel_conditioning` 超时、`test_step1_montage_view` 崩溃，都与基线相同；GPU 硬件开关测试照例跳过。

## 12. 基线（第 2 步，2026-10-07，3 层合成拼图，项目 a5，`bench_a9/baseline_final.log`）

测量条件：
- 脚本驱动、真实显示（WSLg）；窗口在测量开始时已确认"显示在屏幕上、处于活动状态"；
- 513 个动作，0 个错误，全程约 3.5 min；
- 覆盖探针自身的开销：每帧 p95 2.0 ms，全程合计 3.2 s。

前两次基线作废，原因记录在案：
1. 第一次：覆盖探针每帧 27 ms，占掉了大部分测量时间，测量本身就不中立；Step1 底层的 CPU 视图又被误算成"缺块"。
2. 第二、三次：WSL 进入异常状态，所有 `sync` 卡死，窗口也无法显示，没有产生任何画面帧，重启 WSL 后恢复。

| # | 判据 | 实测（p50 / p95 / 最大） | 基准建议门槛 | 现状 |
|---|---|---|---|---|
| A9-1 | 进入步骤后第一帧完整画面（呈现） | 539 / **1930** / 4019 ms；进入 Step1/Step3 后有 3 帧空白画面 | p95 ≤ 300 ms | ✗ |
| A9-2 | 导航过程中缺块 | 0 帧（3 帧空白都发生在刚进入步骤时，归入 A9-1） | 0 | ✓ |
| A9-3 | 拖动 / 缩放后目标层到位（呈现） | 拖动 19 / **44** / 55；滚轮 21 / **29** / 35 ms | p95 ≤ 150 ms | ✓ |
| A9-4 | 界面线程单次阻塞 | 超过 32 ms 的 890 次，p95 **494 ms**，最大 **3984 ms** | 从不 > 32 ms | ✗ |
| A9-5 | 调亮度不读盘 | 0 次读取 | 0 | ✓（但每次界面卡 0.77 s，见下） |
| A9-6 | 切换通道：已驻留时不读盘、界面不等待 | 已驻留 0 次读取；呈现 p95 **525 ms**，每次界面卡约 0.5 s | 0 次读取 / 不等待 | 读取 ✓ / 等待 ✗ |
| A9-7 | 资源（12 GB 档） | 本次没有同时记录资源 | 不 OOM，各缓存不超上限 | 未测 |
| A9-8 | 镜头不漂移 | 拖动 / 缩放期间程序跳转 0 次；全程跳转 2 次（都是切换步骤时的正常跳转） | 不变量成立 | ✓ |

卡顿归因（按各段累计卡顿时长）：
1. **组织预览（Tissue Preview）合成**：`tissue.compose` p50 994 ms、`preview.compose` p50 659 ms。每次切换通道、调亮度、切换步骤都会触发，界面跟着卡 0.5–0.8 s。这是切换通道、调亮度、切换步骤卡顿的主因，合计约 30 s。
2. **Step1 拖动**：610 次卡顿，p50 36 ms、p95 56 ms，几乎全落在 `gpu.submit` 里（每帧 p50 8 ms，但帧与帧之间排得很满）。偶有上传或提交耗时 0.4–0.5 s 的尖峰。
3. **第一次进入 Step1 / Step3**：最长 3–4 s，GPU 视图启动；第二次及以后进入 p50 约 110 ms。
4. **打开项目**：约 1 s 的同步概览读取（`explore.load_overview`），以及交接载入。

Step2 有 4 个测试在 `sync` 卡死期间超时，WSL 重启后单独重跑全部通过（18 + 25 + 21 + 32 个用例），确认是环境问题。

## 13. 门槛锁定（第 3 步，用户裁定 2026-10-07）

以下数值**已锁定**。之后的优化结果不能反过来修改它们（基准 §20.3 第 4 条）。

| # | 锁定值 |
|---|---|
| A9-1 | 再次进入步骤：第一帧完整画面 p95 ≤ 300 ms；**每次启动后第一次进入 ≤ 1 s**（用户把建议的 1.5 s 收紧到 1 s）；不显示空白帧，先显示粗层。用户目标：切换"丝滑无卡顿" |
| A9-2 | 导航过程中 0 帧缺块 |
| A9-3 | 拖动 / 缩放后目标层到位，p95 ≤ 100 ms |
| A9-4 | 切换通道、调亮度、切换模式、切换步骤时，界面线程单次阻塞 ≤ 100 ms；拖动过程中卡顿 p95 ≤ 50 ms、最长 ≤ 100 ms |
| A9-5 | 调亮度 / 对比度 / 伽马 0 次读盘 |
| A9-6 | 切换已驻留的通道 0 次读盘；到出画面 p95 ≤ 150 ms；期间单次阻塞 ≤ 100 ms |
| A9-7 | 3 层和 7 层两份拼图上：不 OOM，各缓存不超过声明的上限（用 `measure_a5.py` 记录） |
| A9-8 | 拖动 / 缩放 / 切换通道过程中程序跳转 0 次；A1 / A7 的不变量成立 |

用户同时裁定：生成 7 层拼图；推送 PA 和 A9 的本地提交；下一步的方案先与 codex 协商，再交用户批准。

## 14. 下一步优化方案（草案 v0，待 codex 协商后定稿）

依据：§12 的基线、§13 的锁定门槛。规则：按工具箱的顺序先做代价最小的；每改一项都重跑相关场景的测量，达标即停；不改科学计算；所有新代码放在新模块里，两个大文件只加接线。

已知的热点（基线证据）：
- H1 **组织预览合成**拖累界面：`tissue.compose` p50 994 ms（在后台线程里），`preview.compose` p50 659 ms，`preview.gray` 每个通道 23 ms。每次切换通道、调亮度、切换步骤、切换模式都会触发，界面卡 0.5–0.8 s。合成本来就在后台线程里，界面还是被卡住，推测原因是 GIL 争用，以及合成用的分辨率远高于屏幕上这个小预览所需。
- H2 **进入步骤**：第一次进入 Step1 / Step3 要 3–4 s（GPU 视图启动、第一次加载 OpenGL、GL 初始化、会话恢复约 1 s）；再次进入 p50 约 110 ms，但会先闪几帧空白，并被 H1 拖住。
- H3 **拖动**：卡顿 p95 56 ms（门槛 50 ms），偶有 0.4–0.5 s 的尖峰（`gpu.upload` / `gpu.submit` 最长 379 / 516 ms）。纹理是 float32（toolbox #4），每帧都做 `glGetError` 同步。
- H4 **打开项目**：约 1 s 的同步概览读取（`explore.load_overview`，toolbox #10）。

候选措施（待 codex 排序和取舍）：
1. H1：组织预览改为按屏幕需要的分辨率合成；同一轮里的多次触发合并为一次；交互进行中推迟合成；减轻工作线程对 GIL 的占用（或改到子进程）。
2. H2：程序空闲时预热 GPU 视图（提前加载 OpenGL、预先创建 Step1 视图）；进入步骤时先显示粗层，不出空白帧；会话恢复中不必在界面线程上做的部分推迟执行。
3. H3：限制每帧的纹理上传量；热路径上关闭 PyOpenGL 的自动错误检查；原生 dtype 纹理（toolbox #4）。
4. H4：建栈时的概览读取改到后台（toolbox #10）。
5. 7 层拼图的基线和 A9-7 资源测量（`measure_a5.py`）。

## 15. 优化方案 v1（与 codex 协商后定稿，2026-10-07，待用户批准）

### 15.1 codex 对热点归因的更正（依据基线日志和代码）
- **H1 不能直接认定是 GIL 争用**：`tissue.compose` 在后台线程里跑，它和界面卡顿时间重叠，不能证明是它造成的；把合成结果交回界面线程只花 0.35 ms（最长 12.6 ms）。**已确认的界面线程开销是亮度工作台在界面线程上的同步合成**（`ui/widgets/channel_workbench.py:1590、1686`）：每次 p50 235 ms、最长 314 ms。在 Step1 / Step3 时这个工作台根本不在屏幕上，却照样合成。现有的合并机制已经够用，不需要再加一个调度器。
- **H2 第一次进入 Step1 时卡 3.98 s**，拆开：
  - 建栈 52 ms；`gl_show` 661 ms；GL 初始化 603 ms；GPU 启动合计 1.60 s；
  - 会话恢复 1.18 s，其中套用 Fusion 设置 805 ms；
  - 处理函数结束之后还有约 1 s 无响应。
  - 再次进入时处理函数 p50 113 ms，但这不等于第一帧画面：进入步骤到出完整画面的 p95 仍是 1.93 s。
- **H3**：上传最长 379 ms、提交最长 516 ms，只能定位卡在哪里，不能证明是 float32 带宽或 `glGetError` 造成的；每到一批结果就整幅重画一次（`ui/step1_gpu_binding.py:789`）也可能是原因。
- **H4**：`load_overview` 实测 225 ms，不是打开项目时那 1.09 s 卡顿的全部。

### 15.2 执行顺序（每块独立、可单独回退；每块之后重跑相同场景；全部门槛达标即停）
- **O1 去掉多余的界面线程合成，并查清 H1 剩下的部分**
  - 先加细粒度计时：快照构建、工作台合成、事件分发；并在 0.5–0.8 s 的卡顿期间采集界面线程的调用栈。
  - `ui/widgets/channel_workbench.py`：工作台不在屏幕上时推迟重新合成，回到屏幕上时一次补上（复用它已有的"预览待刷新"机制）。先核实"是否在屏幕上"的判断：现有判断只看独立弹出的检查器。
  - 新代码放在新模块；白名单加入 `ui/widgets/channel_workbench.py`，必要时加 `ui/block01_display.py`。
  - 门槛：切换通道 / 调亮度 / 切换模式时，单次阻塞 ≤ 100 ms；已驻留通道切换 p95 ≤ 150 ms 且 0 次读盘；屏幕上的像素不变；回到屏幕时正确刷新。
  - 暂不降低合成分辨率（先降分辨率再做伽马和融合，像素不等价），也暂不改用子进程（目前的归因不支持这么做）。
- **O2 异步打开 + 完整的粗层画面交接**（工具箱 #10 / #2 / #3）
  - 新增打开流程的辅助模块，经 `ui/step1_viewer_mount.py`、`ui/step1_viewer_host.py`、`viewer/explore_view.py` 接线：读取器打开和概览读取移出界面线程（沿用已有的生命周期检查），结果在界面线程上安装。
  - 新视图在显示之前先准备好正确的粗层画面；再次进入时保留仍有效的驻留资源；**绝不显示另一个数据集留下的画面**。
  - 单独测"加载 OpenGL"的耗时；启动时预先加载 OpenGL 只作为小实验。GL 上下文的创建仍放在界面线程。
  - 门槛：**冷启动后**第一次进入 ≤ 1 s；再次进入 p95 ≤ 300 ms；0 帧空白；无 > 100 ms 的阻塞。
  - 风险：拆除时的竞态、额外的驻留内存。处理：遵守已有的缓存上限，保留旧的打开路径以便回退。
- **O3（只在拖动或切换通道的门槛仍不达标时才做）减少多余的 GPU 提交**
  - `ui/step1_gpu_binding.py`：已经到达的结果合并成一次待处理的界面发布，沿用现有的代际和取消规则；完整的粗层回退画面始终可见。
  - 先把上传、渲染、自动错误检查、显式 `_check_gl` 各自的耗时分开测清，再决定改哪一项。
  - 门槛：拖动卡顿 p95 ≤ 50 ms、最长 ≤ 100 ms；目标层到位 p95 ≤ 100 ms；覆盖完整。
- **O4（只在测量仍证明有必要时才做）原生 dtype 纹理**（工具箱 #4）：保留校正产物的 float32 数据和显式的缺失像素有效性；渲染像素逐位对照。
- **目前判断不需要**：直接 TIFF 解码、预取、预热更细一层、工具箱第 8–9 项。导航延迟已经达标；第 8–9 项按裁定 C3 只在其余都不达标时才考虑。

### 15.3 每块之后的验收
- 重跑同一个 3 层场景，并分开报告冷启动后第一次进入、再次进入、已驻留通道切换、调亮度、切换模式、拖动卡顿；对比计时开 / 关。
- 7 层拼图跑基线，并用 `measure_a5.py` 测 A9-7。
- A1 / A7 的不变量、调亮度 0 次读盘、科学输出逐位不变。
- 两个大文件只加接线；不新增哈希；守住 A9 开发 8 天的上限。

## 16. 方案 v2（2026-10-07，用户已批准并授权执行）

- **用户问 Rust**：现在不用。理由：Rust 闸门的条件不满足；第一次进入的耗时在 Qt / 显卡驱动和 Python 的流程调度里，换 Rust 解决不了；将来如果实测出单一的读取或调度热点，再按停止规则 10 申请。
- **用户问亮度工作台**：它是 Step0 "Intensity…" 背后的参数部件，**不是** Tissue Navigator。它内部旧的多通道预览画布一直隐藏，却在界面线程上同步合成（每次 215–314 ms）。
- **Odon**（GPL-3.0，只参考设计，不复制代码）。要采用的设计：到达的图块先放进待处理区，**每帧统一合成和重画一次**（`src/app.rs` 的 update 循环）。不能移植的部分：它原生的渲染循环；我们的 GL 上下文属于 Qt 的界面线程。

用户裁定（2026-10-07）：
1. **GPU 预热**：程序启动、用户填写路径和 Load 这段时间里提前做好 GPU 准备。程序一启动就在后台线程加载 OpenGL；界面空闲时在界面线程上预建 GL 上下文、编译着色器。放进 O3。（Step0 不用 OpenGL；它用的 cuCIM 是 CUDA 计算，与此无关。）
2. **删除亮度工作台旧的多通道预览画布**，并排查所有"没用但仍在运行"的组件，逐一确认后停掉或删除。放进 O1。
3. 其余同意 v2，授权执行。

执行顺序：
- **O1**：工作台旧画布 + 无用组件排查 + 细粒度计时。
- **O2**：参考 Odon，拖动时攒批、每帧重画一次。
- **O3**：异步打开 + 粗层交接 + GPU 预热。
- **O4**：有条件做——整数纹理、错误检查开销。

每块都过 codex 代码审核，跑定向回归，并由我自己在真实显示上用 3 层和 7 层两份拼图重测；全部门槛达标即停。

## 17. O1 记录（2026-10-07）

**改动**（白名单内）：
- `channel_workbench`：Step0 亮度面板宿主的旧画布只要不可见就不合成（不再看是否分离），渐进加载 tick 不可见即停。
- `TissuePreviewCoordinator`：开关 `hold_hidden_frames`（应用内开启，测试台默认关）。非 Step0 步骤下没有可见面板时，照常取快照（渲染规格照常发布），但不派发合成；欠一帧，面板 Show / 打开导航器 / 切换上下文时补画。
- `Step0Page`：别的步骤在屏时，映射/颜色变更照常发信号、推工作台，但自己屏外的视图不重画；记下欠账，回到 Step0 时补画一次。
- `main_window`：whole-slide 视图遮住旧 patch 预览时，不取快照不合成；回退到旧视图时补画。

**审查**：codex 无发现，审查期间工作树未变。

**定向回归**：506 个测试，505 过。唯一失败的 `test_v16_pa_switch::test_the_gpu_asked_is_cuda_s_first_visible_one` 与执行顺序有关：前面的测试已导入 torch 时，torch 路径读到真实显卡的 5.9995 GB。单独运行通过，与 O1 无关，修复待用户批准。

**真机实测**（3 层，`bench_a9/run_o1_3lv.log`，对比 `baseline_final.log`）：

| 指标 | 基线 | O1 后 |
|---|---|---|
| 进入 Step max | 4.0 s | 2.9 s |
| Intensity p50 | — | 69 ms |
| GUI 卡顿次数 | 890 | 721 |

**新发现：WSL GPU 休眠卡顿**
- 现象：GPU 空闲约 250 ms 后开始休眠，过程约 500 ms。此时任何 GL 同步（上屏、交换、读回）都会把 GUI 线程堵住约 500 ms。
- 复现：最小程序 `bench_a9/glmicro/micro.py` 可复现，与本项目代码无关。
- 已排除：vblank、EGL、swap interval、原生 Wayland。
- 候选修复：每 200 ms 做一次 1×1 保温。真机（测试包装，未改仓库）空闲后首帧 max 504 → 97 ms，卡顿 p95 498 → 120 ms。
- 状态：作为新项待用户批准。

**增长**：

| 文件 | 增加 | 删除 | 位置 |
|---|---|---|---|
| `step0_page.py` | +48 | -0 | `_owe_own_redraw`、`_replay_owed_redraws`，两处扇出守卫 |
| `main_window.py` | +23 | -0 | `_apply_pending_preview_update` 守卫、`_repay_legacy_preview`、开关 |

## 18. O2 记录（2026-10-07）

**改动**（白名单内）：
- `step1_gpu_binding`：参考 Odon 的更新循环。后台到达的图块结果立即收下，但最多每帧（`PUBLISH_FRAME_MS`=16 ms）发布一次。用户驱动的发布（视口、显示变更）仍然立即进行，并顺带带走已到达的结果。
- `step1_gpu_layer`：新增细粒度计时 `gpu.prepare` / `gpu.render`，仅用于测量。
- 测试：`_events` 泵会跑过一个帧槽；新增"一批图块到达只提交一次"的测试。关掉攒批后该测试失败，证明它确实能抓住旧行为。

**审查**：codex 无发现，审查期间工作树未变。

**定向回归**：330 过，0 失败。

**真机实测**（3 层，`bench_a9/run_o2_3lv_c.log`，有效运行）：

| 指标 | O1 | O2 |
|---|---|---|
| tick p50 | 509 ms | 70 ms |
| tick p95（剔除 WSL 唤醒后） | — | 108 ms |
| Intensity p95（剔除后） | — | 68 ms |
| 单次结果堆积峰值 | 405 ms | 181 ms |
| 不与 WSL 唤醒重叠的 GUI 卡顿 p95 | 66 ms | 67 ms |
| 进入 Step | 2.7 s | 2.7 s（留给 O3） |

**用户裁定（2026-10-07）**：不加 GPU 保温，部署目标是原生 Windows。WSL 的 GPU 唤醒帧由分析器单独剔除并另行报告。

**测量工具**：
- 驱动在连续 3 次 settle 都没有新帧，或窗口被最小化/未显示时，把这次运行标为 INVALID 并停止。
- 起因：一次运行中途窗口停止重绘，不加检查的话这种无效数据会被当成正常结果。

**新发现，待用户决定**：
- Tissue Preview 每次整片合成约 650 ms，原因是 16 个通道 × 约 2048² 的逐通道临时数组。
- 改成一次矩阵乘法可降到 147 ms，结果差异 ≤2e-7。
- 合成过程本身不占用 GIL，所以只影响 Tissue Preview 自身的刷新速度，不影响主视图。

**7 层拼图**：还没有能打开的 Step1 项目，需要先跑完 Step0 + Step1。是否搭建由用户决定。
