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

## 19. O3 记录（2026-10-07）

**方案**：与 codex 协商后定稿，codex 提出 6 条修改，均已采纳。

**改动**：
- O3-1 GPU 预热（用户裁定：程序一开始就启动 GPU）：`ui/gpu_warmup.py`，在窗口显示后由后台线程导入 PyOpenGL。
  - 也试过一个 1×1 GL 预热控件，实测后撤掉。它带来的收益与只导入相同；而且会让整个窗口改走 GL 合成，WSL 下 Step0 滚轮从 6 ms 恶化到 403 ms。
- O3-2：`refresh_display` 在同一事件循环轮次内，第一次立即发布，其余合并为该轮结束后的一次发布。按轮次而不是按时间判断，这是 codex 第 1 条意见。
- `_schedule_preview_update` 和 O1 的 whole-slide 让路路径中，融合设置状态（一次设置哈希，约 11 ms）每轮只算一次。
  - 不影响安全保证：开始搜索的点击只可能发生在之后的轮次，开始搜索时 `_require_committed_fusion_settings` 还会再检查一遍。
- 细粒度计时：`gpu.init` / `gpu.init.import` / `gpu.init.compile`。

**审查**：codex 无发现，工作树未变。

**定向回归**：526 过，0 失败。

**真机实测**（3 层，`run_o3c_3lv.log`）：

| 首次进入 Step1 的 GUI 时间 | O2 | O3 |
|---|---|---|
| `switch.go_step1` | 1708 ms | 1063 ms |
| `gl_show` | 653 ms | 132 ms |
| 恢复会话字段 | 742 ms | 468 ms |

- Step0 滚轮 p95 38 ms，没有恶化。
- 剔除 WSL 后：GUI 卡顿 p95 64 ms，Intensity p95 68 ms。
- 再次进入 Step1 时 `show_page` 仍为 404 ms。疑似 WSL，需在原生 Windows 上确认。

**待用户决定**：
1. 粗层交接（codex 提出）：首次显示时先显示粗层，还是保持"清晰后再显示"（G3.2b）。
2. 7 层拼图的项目怎么搭建。
3. Tissue Preview 合成改为矩阵乘法（约 650 → 150 ms）。

## 20. O3-5 粗层交接 + Tissue Preview 合成提速（2026-10-07）

**用户裁定**：
1. 首次显示参考 Odon 的设计。Odon 每帧按"粗 → 中 → 目标层"的顺序绘制，细层覆盖粗层，并对每个通道分别回退。
2. 7 层拼图由脚本搭建，但等用户通知后再做。
3. Tissue Preview 只要保持实时动态显示就做提速。

**改动**：
- `_publish_current`：通道的整片粗层一到就显示，之后随细图块到达逐块变清晰。G3.2b 的"首次显示即清晰"规则作废；锁定旧规则的 3 个测试已按新规则改写。粗层本身仍然原子化：不完整的粗层不显示。
- 覆盖探针按视口应得的层级（`ChannelSource.target_level`）判断帧是否完整，而不是按已绘制的层级。这是 codex 的发现：否则先显示的粗层会被算作"完整帧"，让指标虚高。
- `tint_and_sum_grays`：按行分块做一次矩阵乘法。16 通道 × 2048² 从 668 ms 降到约 130 ms，峰值内存 168 → 90 MB，结果差异 ≤1.2e-7。

**审查**：codex 提出 1 条（探针目标层级），已修复。

**回归**：涉及合成的测试共 1390 个。
- 865 + 前半段通过。
- `test_step0_channel_conditioning.py::test_bg_hotswap_updates_corrected_only` 会卡死，是旧问题：今晨的全量回归也卡在这里，所以那次的汇总缺少这个文件。
- `step0_channel_conditioning` 有 7 个旧失败，`test_save_invalidation_repulls_into_workbench` 是已知旧失败。
- `test_a_first_tick_composes_once...` 只在高负载下超时（它只等 200 ms），单独运行 5/5 通过。

**真机实测**（3 层，`run_o35_3lv.log`）：

| 项目 | 结果 |
|---|---|
| 勾选通道 p50 | 62 ms |
| 勾选通道 p95（剔除 WSL） | 79 ms |
| Intensity p95（剔除 WSL） | 75 ms |
| 拖动 p95 | 40 ms |
| 滚轮 p95 | 33 ms |
| GUI 卡顿 p95（剔除 WSL） | 68 ms |
| Tissue Preview 16 通道合成 | 约 100 ms（原约 650 ms） |

**首次进入 Step1 的时间线**：
- GUI 线程从进入起连续阻塞 1.9 s：进入处理 1.18 s，其中打开查看器约 0.53 s、恢复会话 0.47 s；处理结束后还有约 0.72 s 尚未细分的工作。
- 1.9 s 时粗层数据早已就绪，随即发出首次重画。
- 2.4 s 时画面上屏，中间约 0.5 s 疑似 WSL 的 GPU 唤醒。
- 结论：瓶颈是 GUI 线程忙，不是数据慢。下一步要拆分那 0.72 s 和恢复会话的 0.47 s。

## 21. 7 层项目、7 层基线、首次进入细粒度计时（A9-T1），以及 O3-6 方案草稿（2026-10-07）

### 21.1 7 层项目（用户授权）
- 脚本 `bench_a9/build_7lv_project.py`，不在仓库内，全部调用产品函数。项目 `bench_rm/accept/a9_7lv`，工作区 `full_wsi_20261007_211227_de37`，约 70 s 建成。
- 内容：真实 Step0 校正（CD4 cucim 50、CD45RA tophat 15，同 a5）、`write_handoff`、Step1 融合设置与草稿取自 a5 并改绑。
- 不含预分割、融合、分割，所以场景 `a9_7lv.json` 去掉了 Step3 段。
- 真机核对：工作区能打开；融合设置被采用（`fusion settings restored`）。

### 21.2 7 层基线（`bench_a9/run_7lv_base2.log`，剔除 WSL 后）

| 项目 | 7 层 | 3 层（O3-5） |
|---|---|---|
| 拖动 p95 | 36 ms | 40 ms |
| 滚轮 p95 | 27 ms | 33 ms |
| Intensity p95 | 31 ms | 75 ms |
| 勾选通道 | 20 次全部撞上 WSL 唤醒，无数据 | 79 ms |
| GUI 卡顿 p95 | 220 ms | 68 ms |

A9-7 资源（`measure_a5`）：内存峰值 7.2 GB，最低可用 2.9 GB，交换区约 0，显存峰值 0.9 GB，没有 OOM。

第一次运行无效：Step0 的一次拖动画出了 patch P14。项目已重建；驱动改为记录每次手势落点（`a9.target`）。重跑没有复现，原因未查明。

### 21.3 A9-T1 细粒度计时（14792cf，本地）

**改动**：
- `utils/perf_dispatch.py`：同时设置 `BLOCK01_PERF=1` 和 `BLOCK01_PERF_DISPATCH=1` 时，应用的 `notify` 记录慢的事件分发（接收者、事件类型、嵌套深度）。
- 恢复会话的子计时段。
- 驱动的 `a9.target` 标记。

**审查与回归**：codex 无发现；定向回归 224 个全部通过。首次诊断运行在退出时出现 SIGSEGV，已在退出前停止计时并关闭 `setdestroyonexit`，复测正常退出。

**首次进入 Step1 的拆分**（`run_entry_7lv_b.log` 未碰上 WSL 唤醒；a5 见 `run_entry_a5.log`；单位 ms）：

| 段 | 7 层 | a5 | 性质 |
|---|---|---|---|
| `restore.scientific_state.owners` | 233 | 217 | 我们的代码 |
| `restore.fusion_settings.adopt_intensity`（Step0 工作台同步 29 通道，Step0 不在屏） | 174 | 351 | 我们的代码，对屏幕无用 |
| `restore.preseg`（只有 a5 有预分割） | — | 170 | 我们的代码，视图不在屏 |
| `restore.select_preview_patch` + 进入时的旧 patch 预览合成 | 95 + 59–92 | 93 + 59 | 我们的代码，旧视图随后被整片查看器遮住 |
| GPU 层第一次 Resize（Qt 建上下文 / FBO） | 139–286 | 159 | Qt / 驱动 |
| `grabFramebuffer` | 40；碰上 WSL 时 602 | 54 | Qt / 驱动 |
| 处理函数返回后的整窗重绘 `UpdateRequest` | 188–297 | 256–699 | Qt / 驱动，疑似 WSL |
| 第一次整幅上传 | 122–168 | 392 | 粗层大小决定 |
| **到第一帧完整画面** | **1.53 s（无 WSL）** / 2.6 s | 3.0 s | 门槛 1 s |

未被覆盖的时间：20–70 ms。

**再次进入**：`show_page` 约 380 ms。原因是页面重新显示时，Resize 逐级传到 `Step1GpuLayer`（QOpenGLWidget），每次都重建 FBO。之后还有一次整窗重绘，约 100–330 ms。

### 21.4 O3-6 方案草稿（待 codex 审、用户批准）

两个根因：
1. 不在屏幕上的组件在进入时被立即做了全套工作。
2. "整片查看器就是画面"的判断（`_step1_whole_slide_active`，`main_window.py:2504`）在恢复期间为假，因为挂载在恢复之后才打开。于是旧 patch 预览被合成 2–3 次。

措施按收益与风险排序，每项独立、可单独回退，每项之后重测，首次进入达标即停。

- **P1 Step0 工作台不在屏时不同步**
  - 不在屏的判断：当前步骤不是 Step0，且 Intensity 窗口不存在或不可见。此时 `adopt_intensity` 只把窗口存入 `_workspace_intensity`（已有的延迟路径），不调用 `_engage_conditioning_workbench`。
  - 29 个映射用 `DisplayState.adopt_mappings` 一次事务写入共享状态，扇出一次，而不是逐通道 `set_mapping` 29 次。
  - 回到 Step0 或打开 Intensity 时，由已有的 `_engage_conditioning_workbench` → `_apply_workspace_intensity` 补上。
  - 实现前先核实：Step1 只从共享状态读映射，不从工作台的 `_params` 读。
  - 预计省 170–350 ms。
- **P2 旧 patch 预览在整片挂载"在开或将开"时不合成**
  - 进入 Step1、准备挂载整片查看器时置一个标记；`_refresh_patch_preview`、`_select_preview_patch` 的合成部分、`_ensure_channels_cached` 遵守这个标记，只记欠账。
  - 挂载被拒绝、退回旧视图时，由已有的 `_repay_legacy_preview` 补画。
  - 预计省 150–180 ms。
- **P3 预分割恢复拆成"状态"和"视图"两部分**
  - 状态部分照旧在进入时完成：`read_run`、记录、选中项、`_check_save_unlock`。
  - 视图部分（结果网格、montage）推迟到对应标签页第一次显示时，沿用 `_request_montage_images` 已有的"montage 不在屏就不画"规则。
  - 预计省约 170 ms（只在有预分割时）。
- **P4（先测再定）两路状态安装**
  - 先给 `config.set_channels`、`prepare_restore`（深拷贝回滚快照）、提交 / `_announce_install`（逐通道信号）、`sync_after_restore` 加子计时段。
  - 候选做法：通道列表不变时不重建 29 行；逐通道信号合并成一次扇出（已有 `install_fanout_active`）。
  - 测量之后再定，不预先改。
- **P5 再次进入时 GPU 层不重复 Resize**
  - `_sync_attached_geometry`：矩形不变就不调 `setGeometry`。
  - Step1 页面不可见时不同步几何，显示后同步一次。
  - `_apply_channel_column_fraction` 只在尺寸真的变化时调 `setSizes`。
  - 目标：再次进入 `show_page` 从 380 ms 降到 < 100 ms。

**不做**：
- Qt 内部的上下文 / FBO 创建、`grabFramebuffer` 的同步、整窗 GL 合成重绘。它们受 WSL 影响，按裁定分开报告，留给原生 Windows 确认。
- 预建 GL 控件：O3 已实测，会让整窗改走 GL 合成，Step0 变慢。

**门槛**（剔除 WSL 唤醒）：
- 冷启动后首次进入到第一帧完整画面 ≤ 1 s（3 层和 7 层都要达标）。
- 再次进入 p95 ≤ 300 ms。
- 像素不变。
- 回到 Step0 时，Intensity 与工作台显示正确的窗口。
- 预分割标签页显示正确。
- GPU 不可用时旧视图仍能正确显示。

**白名单**：
- 新模块 `ui/step1_entry_defer.py`（标记和欠账），放置推迟逻辑。
- 接线：`ui/main_window.py`、`ui/step0/step0_page.py`、`ui/step1_gpu_layer.py`、`ui/step1_viewer_mount.py`，必要时 `ui/block01_display.py`。
- 对应测试。
- 两个大文件只加接线。预计 `main_window.py` 约 +40 行、`step0_page.py` 约 +15 行。

### 21.5 codex 审查（方案草稿）与修订（方案 v2，待用户批准）

codex 共 7 条意见，已逐条对照代码核实。第 1、2 条已确认：
- `display_mapping_draft()`（`step0_page.py:4861`）读的是工作台的 `_params`；
- `adopt_mappings`（`block01_display.py:812`）仍逐通道发 `mapping_changed`。

**修订后的措施**：

- **P1′ 参数照装，展示推迟**
  - `adopt_intensity` 照常把参数和"已确定"标记同步写入工作台的 `_params`，保证 Save、脏检查、Step0 草稿、融合设置读到的都是新值。
  - 只推迟展示部分，包括 29 行列表重建、直方图、控件装载。推迟到 Step0 或 Intensity 窗口可见时再做；工作台已有数据时也要显式补上。
  - 核通道的映射角色和"已确定"标记的行为保持不变（`_adopt_workbench_window`）。
  - 先加子计时段，分清 174–351 ms 里有多少是展示、多少是逐通道映射扇出，再决定扇出要不要动。
- **P2′ 只推迟像素工作**
  - patch 选择和会话更新照常进行，只推迟旧预览的合成和读取。
  - 欠账在挂载被拒绝、出异常、进入中途被打断时补上；补上时要包括跳过的 patch / 通道加载，不只是安排重画。整片查看器挂载成功时清掉欠账。
  - CPU 整片挂载成功也算成功，不算退回旧视图。
- **P3′ 状态照装，视图先测再推迟**
  - 照常在进入时安装：方法、选中的 patch、run 和记录、可选性校验、`_preseg_selected`、`_check_save_unlock`。
  - 结果网格、montage 图像、轮廓先测出各自的耗时，再决定是否推迟。
  - 结果标签页已经可见时必须立即填好。
- **P4** 不变：先测再定，单独一项。
- **P5′ 先记录再改**
  - 先记录每次几何同步前后的矩形，确认矩形不变时是否真会重建 FBO，再决定是否跳过。
  - 不可见时如果推迟同步，必须在第一次可见提交之前显式同步一次。视口的 Show / Move / Resize 处理和 ViewBox 的 resize 处理都保留。
  - 分栏尺寸要先比较实际值，没变才跳过写入，并保持跨步骤的列对齐。

**顺序**：先做 P1′ 和 P2′ 并重测，再测 P3′ 和 P4，最后评估 P5′。

**能否达标**：按现有测量，P1–P3 全部生效后，7 层约 1.0–1.2 s，a5（3 层）约 2.3 s。a5 里剩下的大头是：
- 整窗重绘 256 + 443 ms；
- 第一次整幅上传 392 ms（3 层的最粗层 1929×2026，比 7 层的大 16 倍）；
- GPU 层第一次 Resize 159 ms。

所以只靠这一块，首次进入 ≤ 1 s 在 3 层上达不到。另外，这些 Qt / GL 开销不能只因为疑似 WSL 就全部剔除，要在原生 Windows 上确认。

### 21.6 O3-6 P1′ + P2′ 执行记录（2026-10-07，用户批准 v2，先做 P1′、P2′）

**改动**：
- **P2′**（`ui/main_window.py` +23/−2）：进入 Step1 时，`_resync_step1_display_from_state` 只先做"采用共享选择"那一半；读通道、合成旧 patch 预览的那一半（`_resync_step1_pixels`）挪到整片查看器跟随步骤之后。
  - 查看器打开时，已有的 `_step1_whole_slide_active` 判断会让旧预览不再合成。
  - 查看器被拒绝时，这一半照原样执行。
  - 没有另建新模块。用户同意了这一偏差。
- **P1′**（`ui/widgets/channel_workbench.py` +48/−3，用户同意加入白名单）：
  - 剖析结果：`adopt_intensity` 的 68% 花在 29 行 `ChannelLayerList` 的构造上，约 15% 是直方图。Step0 的工作台本身从不上屏，只有分离出去的检查器会上屏。
  - Step0 宿主下：工作台不可见时不建列表，`showEvent` 时补建；检查器不可见时不画直方图，检查器 Show 时补画。
  - 参数、"已确定"标记、用户调过的标记照旧立即写入。其他宿主不受影响。

**审查**：codex 只提了白名单这一条（用户已同意），无正确性问题；审查期间工作树未变。

**定向回归**：51 个文件，1240 个通过，10 个失败。10 个全部在已知旧失败里：4 个在 `full_reg/failed.txt`，6 个是 `step0_channel_conditioning` 的旧失败（原来 7 个，少了 1 个）。没有新失败。

**真机前后对比**：改动前的代码（14792cf，单独的 git 工作树）与改动后交替运行，7 层和 a5 各 3 次，不开分发计时。中位数：

| | 7 层 改动前 → 改动后 | a5 改动前 → 改动后 |
|---|---|---|
| 首次进入 → 第一帧完整画面 | 1619 → **990 ms** | 2246 → **1659 ms** |
| `adopt_intensity` | 244 → 77 | 291 → 100 |
| `restore.session_fields` | 464 → 237 | 584 → 365 |
| 再次进入 | 233 → 232 | 341 → 289 |

- 7 层首次进入已在 1 s 门槛内，但只差 10 ms，余量很小。
- a5 仍是 1.66 s。剩下的大头有：
  - 第一次整幅上传约 0.4 s：3 层的最粗层 1929×2026；
  - 整窗重绘约 0.25–0.45 s；
  - 预分割恢复 0.17–0.26 s；
  - 两路状态安装约 0.22–0.27 s。
- a5 改动后有一次是 3.1 s，那次 GL 段卡了 889 ms，属于 WSL 唤醒。

**诊断工具的已知问题**：打开 `BLOCK01_PERF_DISPATCH` 时，程序退出可能 SIGSEGV（6 次中 4 次）。关掉它的运行全部正常退出，包括含本次改动的 15 次。只影响诊断，不影响产品，也不影响测得的数据。

### 21.7 O3-6 P3′、P4、P5′ 执行记录（2026-10-07 夜，用户批准）

**先测量。** 用仓库外的 cProfile 包装测真实首次进入，并记录 GPU 层的几何（`bench_a9/prof/`）。

- **P3′**：剖析时 `_rm_restore_preseg` 共 226 ms。
  - 其中 134 ms 是 `_preseg_current` 被调用了两次，每次都打开校正产物的 zarr 数组来计算像素标识。
  - 结果视图本身只有约 6 ms，推迟它不值得。
  - 改法：恢复时只算一次，再通过 `_refresh_preseg_results(current=...)` 传给刷新。不改行为。
- **P4**：剖析时两路状态安装约 250 ms，几乎都来自 `_announce_install` 的逐通道 `color_changed` / `mapping_changed` 信号。
  - 调用链：`Step1WholeSlideMount._on_gpu_color` / `_on_gpu_mapping` → 33 次 `_refresh_gpu` → 462 次 `_request_gpu_seed` → 约 2 万次 `_overview_read_pending`。
  - 改法：这两个回调在 `install_fanout_active()` 期间直接返回。安装时最先发出的是 `state_installed`，此时状态已经完整，由它刷新一次。Block01 的帧时钟已经用的是同一规则。
- **P5′**：几何记录显示，矩形不变时 `setGeometry` 不会触发 QOpenGLWidget 改尺寸。再次进入时唯一一次 GL 改尺寸，是第一次再进入时宽度从 1018 真的变成了 979，温状态下只要 1.7 ms。之前测到的 375 ms，是撞上了 WSL 的 GPU 唤醒。**所以不改代码。**
  - 遗留：首次进入时查看器宽 1018 px，之后都是 979 px，属于布局不一致。待用户决定是否处理。

**改动**：
- `ui/main_window.py` +11/−5（P3′）。
- `ui/step1_viewer_mount.py` +16（P4）。
- 新增测试 `tests/test_a9_o36_entry.py`，共 7 个，覆盖 P1′、P3′、P4。撤掉 P3′/P4 的改动后，对应的 2 个测试失败。
- `tests/test_v16_rm2_chain.py:593`：替身改为接受关键字参数（只改环境搭建）。

**审查**：codex 无发现，审查期间工作树未变。

**定向回归**（42 个文件）：
- 除下面这一个文件外全部通过。
- `test_step1_montage_view.py` 在 `test_leaving_out_the_signal_or_the_nucleus_changes_only_the_montages_picture` 处整个进程崩溃（abort 或 SIGSEGV）。在 b5cfbea 和 14792cf 上同样崩溃，是旧问题。这个测试单独运行能通过；跳过它，其余 28 个也通过。

**真机前后对比**：改动前 b5cfbea，与改动后交替运行，7 层和 a5 各 3 次。中位数：

| | 7 层 改动前 → 改动后 | a5 改动前 → 改动后 |
|---|---|---|
| `restore.scientific_state.owners` | 123 → 35 | 118 → 39 |
| `restore.preseg` | — | 196 → 109 |
| `restore.session_fields` | 274 → 165 | 392 → 247 |
| 首次进入 → 第一帧完整画面 | 1641 → 1157 | 2185 → 1853 |
| 再次进入 | 199 → 195 | 280 → 334 |

这一轮各次之间波动很大：改动前有一次跑出 5.7 s，改动后的 7 层是 0.9–1.7 s。所以首次进入的总时间只能看出方向，计时段上的节省（110–145 ms）是可靠的。

**O3-6 合计**（相对 14792cf）：
- `adopt_intensity`：约 245 / 290 → 70 / 107 ms；
- 两路状态安装：约 230 → 35–40 ms；
- 预分割恢复：约 200 → 109 ms；
- 旧预览合成：60–90 → 0 ms。

GUI 线程上合计省约 0.45 s（7 层）和 0.55 s（a5）。

**3 层 ≤ 1 s**：等原生 Windows 实测后再定（用户裁定）。

## 22. 首次进入 Step1 的查看器宽度与以后不一致（W1，方案草稿，待 codex 审、用户批准）

### 22.1 现象与测量
记录方式：仓库外包装 `bench_a9/prof/run_prof.py`，开 `PROF_LAYOUT=1`；日志 `run_lay_7lv.log`。

- 第一次进入 Step1：共享列宽比例为 0.25，Step1 分栏 346 / 1038，GPU 层 1018 px。
- 第一次回到 Step0：`_apply_channel_column_fraction` 在 Step0 可见时测到它的列被撑到 385 px，按"撑宽即写回"把比例写成 0.2782。
- 之后每次进入 Step1：385 / 999，GPU 层 979 px。
- 后果：
  - 第一次再进入时画面缩放一下，GL 层改尺寸（温状态 1.7 ms；撞上 WSL 唤醒约 375 ms）；
  - 第一次访问 Step1 时，通道列比 Step0 窄 39 px，违背"各页一个列宽"的裁定。

### 22.2 根因
- Step0 的 `apply_channel_column_width` 同时设置可见分栏 `_bg_c_split` 和隐藏同伴 `_cond_workbench._h_split`，然后取两者实际宽度的较大值（`step0_page.py:3670` 起）。
- 隐藏工作台的左栏里装着尚未分离的 Intensity 检查器，最小宽度约 383 px。
- `opening_channel_column_width` 本来就把隐藏同伴的最小宽度算进去了（"never under the hidden peer's"）。但全局统一下限 `_hold_step1_channel_floor` 只取各页 `left_panel()` 的 `minimumSizeHint`，约 256 px。
- 所以 Step0 的真实下限（约 383）只有在 Step0 可见、按比例应用一次时才写进共享比例，而 Step1 的第一次访问早于这一刻。

### 22.3 方案
- **A（推荐）统一下限包含 Step0 的隐藏同伴**
  - `_hold_step1_channel_floor` 取下限时，再加上 Step0 隐藏同伴左栏的 `minimumSizeHint().width()`，用 Step0 已有的取值方式，与 `opening_channel_column_width` 一致。
  - `_apply_channel_column_fraction` 已经保证 `W = max(W_user, floor)`，所以从第一次进入起各页都是同一列宽，等于现在的稳态（约 385 px）。
  - 行为变化：Step1 / Step2 / Step3 的通道列最窄从 256 px 变成约 383 px。现状下，在这些页面拖窄后，一回到 Step0 也会被撑回去，所以实际可保持的最窄值本来就是 383。
  - 改动：`ui/main_window.py` 约 +6 行，加测试。
- **B 隐藏同伴不再撑宽 Step0**
  - 把隐藏同伴从取较大值的那一步、以及开场宽度里去掉。
  - 各页下限统一为 256，Step0 开场和稳态都会变窄约 37 px。
  - 这会改变 v15 用户确认过的 Step0 开场宽度规则，而且检查器重新放回工作台时，同伴的布局会被压窄。所以不推荐。
- **C 只让共享比例提前收敛**：Step0 第一次以全尺寸可见时补一次应用。只修启动这一段；如果 Intensity 检查器分离或放回会改变同伴的最小宽度，仍可能再次不一致。不推荐。

**门槛**：
- 7 层、a5 的首次进入、第二次、第三次，GPU 层宽度都相同。
- Step0 与 Step1 的通道列宽相同。
- 拖动任一页的分隔条，各页一起变。
- 现有布局测试不变（`test_step1_layout_block_a.py`、`test_global_channel_dock.py`、`test_ui_surface_contract.py` 等）。

**白名单**：`ui/main_window.py`、新测试，必要时 `ui/step0/step0_page.py`（只读同伴最小宽度的访问器）。

### 22.4 codex 审查与修订（W1 方案 v2，待用户批准）

**codex 意见**（6 条，已核实）：
- 根因成立。
- A 改变了统一下限的定义，与 `UI_SURFACE_RULES.md:108` 的规则冲突（下限只取 Channels 列自身的内容），`test_v16_frame_lock.py:230` 也会被打破。
- B 是更大的行为变化。
- 关键追问：初始的 0.25 从哪来？

**追查结果**（`run_frac_7lv.log`，记录每次写入共享比例时的调用栈）：
- 窗口第一次 `showEvent` 时还没加载数据集，`channel_column_fraction()` 按 `opening_channel_column_width()` 算出 0.25 并记住。那时隐藏工作台是空的，最小宽度小。
- 加载数据后，工作台检查器装上了内容，最小宽度长到约 383 px。可共享比例只在"可见分栏被撑宽"时才会更新，Step0 在场时没有再应用过一次，所以一直停在 0.25。
- 直到第一次从 Step1 回到 Step0（`_go_to_step0` → `_fix_step1_split_ratio`），才写回 0.2782。

**推荐方案 C′：把 Step0 的实际下限当作"撑宽"规则的一部分，Step0 不在屏时也算。**
- `Step0Page` 加一个只读访问器 `channel_column_minimum()`，返回 Step0 自己的取宽规则实际会撑到的宽度：`max(可见列最小宽度, 隐藏同伴左栏最小宽度)`，与 `apply_channel_column_width` 的取较大值一致。
- `_apply_channel_column_fraction`：`wanted = max(wanted, step0.channel_column_minimum())`，撑宽了就写回共享比例。这和现有"可见分栏被撑宽即写回"是同一条规则，只是不再要求 Step0 在屏。
- **不改**统一下限：各面板的 `minimumWidth` 不变，`test_v16_frame_lock` 不受影响，Step1 的拖动下限仍是 256 px。现状下拖窄后一回到 Step0 就会被撑回，这一点也照旧。
- 效果：第一次进入 Step1 就是稳态列宽，约 385 px，各次进入的 GPU 层宽度相同。
- 检查器分离后，隐藏同伴的最小宽度会变小。已写回的比例不会自动变窄，与现有"撑宽即写回"的语义一致。
- 改动：`ui/main_window.py` 约 +5 行，`ui/step0/step0_page.py` 约 +8 行（访问器），加测试。

### 22.5 W1 执行记录（2026-10-08，用户裁定，取代 22.3 / 22.4 的方案）

**用户裁定**：
1. 各页通道栏开场取一个固定比例 0.278，不再在启动时按控件内容计算。如果内容需要更宽，栏的右边界直接盖住内容的右侧。
2. 宽度只跟随用户的拖动。任何一页的内容都不撑宽栏，也不写回共享比例。在任一页拖窄，各页一起变；内容从栏的右边界被盖住。这条适用于所有有通道栏的步骤（Step0、Step1、Step2、Step3）。

用户确认：开场比例 0.278；硬下限 120 px；Step2 的参数面板同样适用；`ui/step_frame.py` 加入白名单。

**改动**：
- `ui/step_frame.py`：
  - 常量 `CHANNEL_COLUMN_OPENING_FRACTION = 0.278`、`CHANNEL_COLUMN_HARD_FLOOR = 120`；
  - `release_column_floor(splitter)`：栏本身的最小宽度设为 120；
  - `hold_content_width(panels)`：各页面板取同一个内容宽度（各页自身最小宽度中的最大值）。它只约束内容不被挤压，被盖住的布局在各页都一样，不约束栏宽。
- `ui/main_window.py`：
  - `channel_column_fraction()`：返回已记住的比例；用户还没拖动过时，返回固定值；
  - `_apply_channel_column_fraction()`：去掉"撑宽即写回"；
  - `_hold_step1_channel_floor()`：改为调用上面两个函数。
- `ui/step0/step0_page.py`：
  - `apply_channel_column_width`、`_on_left_split_dragged`：去掉"取两者较大值"，隐藏同伴仍然跟随，但不再撑宽 Step0；
  - `_wire_left_column_sync`：主窗口已设过宽度时，不再用旧的开场规则覆盖。
- `UI_SURFACE_RULES.md`：两处规则改为新裁定（框边、滚动条、`Save Fusion Settings` 都可被盖住）；Step1 页面的比例说明同步更新。
- 测试（按新裁定改写期望值，场景不变）：
  - `test_v16_frame_lock` 6 个：统一下限 = 硬下限；开场 = 固定比例；内容比栏宽时栏和比例都不变；
  - `test_step1_layout_block_a` 1 个：拖到 Step0 内容以下，Step0 跟随并被盖住；
  - `test_step0_step1_surface_details` 1 个：`..._is_never_covered` 改为 `..._is_covered_not_squeezed`。

**审查**：
- codex 第一次审查：只提了白名单（用户已同意）。
- 回归查出 Step1 面板被挤压到 118 px（违反"盖住"），已用 `hold_content_width` 修复。
- codex 复审：无发现。两次审查期间工作树都未变。

**定向回归**（26 个布局 / 界面 / 页面 / 停靠栏相关文件）：583 个通过，4 个失败：
- 3 个是已知旧失败：`global_channel_dock` baseline panel、`step1_channel_panel` look、`tissue_navigator_viewport_sync` mapping；
- `test_step0_step1_display_isolation::test_step0_work_does_not_make_step1_load_or_redraw` 一次偶发失败：上一轮通过，单独连跑 3 次都通过。原因与时序有关，进入 Step1 时排队的重画落在了 Step0 的事件处理里。

**真机**（`run_w1_7lv.log`，7 层，进入 Step1 三次）：
- 比例始终 0.278，Step1 分栏每次都是 385 / 999；
- GPU 层第一次就是 979 px，再次进入不改尺寸。

## 23. 原生 Ubuntu 测量（任务二，方案草稿，2026-10-08）

**目的**：在没有 WSL 的机器上重测 A9 时间，判断剩下的门槛是不是 WSL 造成的假象。只测量，不改产品代码。

### 23.1 服务器环境（已核实）
- CPU AMD EPYC 7R32（48 核），内存 125 GB；显卡 RTX 4090 48 GB，驱动 535.309.01。
- 显示：Xorg 跑在 NVIDIA 卡上，HDMI 接真实显示器（1920×1080，display :1，GNOME 会话）。屏保和 DPMS 都关着，显示器开着。
- 渲染器：用程序自己的 Qt 环境（fusion_mesmer：Qt 5.15.14、PyQt 5.15.11，平台插件 xcb）建 GL 上下文，读到 `NVIDIA GeForce RTX 4090/PCIe/SSE2`，`4.6.0 NVIDIA 535.309.01`。服务器没有装 glxinfo，这一步替代 `glxinfo -B`。
- 和目标机（RTX 3060 6 GB + 16 GB）差别很大，绝对时间会偏乐观。这里只用来判断 WSL 的影响，不能当作目标机的达标证据。

### 23.2 准备
- 被测代码：`ef66476`，用干净的 git 工作树 `/sda1/Fusion/a9_bench/root/block01`，不含任务一未提交的 qptiff 改动。
- 数据放在本地盘 `/sda1/Fusion/a9_bench/synthetic/`：
  - 3 层拼图已复制，sha256 校验通过；
  - 7 层拼图在服务器上用 `make_synthetic_mosaic repyramid --pyramid-factor 2` 生成。
- 测量脚本复制到 `/sda1/Fusion/a9_bench/bench_a9/`，只改路径：
  - `/home/ming/fusionflux` 换成 `/sda1/Fusion/a9_bench`；
  - Python 换成 `/root/micromamba/envs/fusion_mesmer/bin/python`；
  - 加上 `DISPLAY=:1` 和 `XAUTHORITY`。
- 项目：`build_7lv_project.py` 改成可传参数（`--ome`、`--a5`、`--out`），`--a5` 指向拷来的 `a5_settings`。用它重建 3 层（a5）和 7 层（a9_7lv）两个项目，场景里的工作区 id 换成新建的。

### 23.3 与 WSL 数据的可比性（关键限制）
- **7 层**：WSL 上的 7 层项目本来就是用同一个脚本搭的，没有预分割和融合 run，所以两边可以直接对比。
- **3 层**：WSL 上的 a5 是在 GUI 里搭的，有预分割、融合、分割 run；服务器上重建的没有这些。
  - 首次进入时 WSL 多一段 `restore.preseg`（约 109 ms），Step1 安装的内容也可能更多。
  - 处理：3 层场景去掉 Step3 段，和 7 层场景一样；报告里注明 3 层不是同一个项目，只作参考。
  - 如果要严格对比 3 层，需要在服务器上用 GUI 补跑预分割、融合和分割，需另行批准。

### 23.4 测量
- 每个场景先跑 1 次热身（不计入，让文件进入页缓存），再跑 3 次取中位数：`a9_entry_7lv`、`a9_entry_a5`、`a9_7lv`、`a9_a5`（无 Step3）。
- 分析：
  - `analyze_perf_log.py --a9`：日志里 `a9.platform` 应标出不是 WSL，应该一帧都不剔除；如果有剔除，要查原因。
  - `first_entry.py`：拆分首次进入的时间。
- A9-7 资源：两个完整场景运行时，挂上 `scripts/measure_a5.py --pid`。
- 不开 `BLOCK01_PERF_DISPATCH`。如果首次进入仍超过门槛，再单独跑一次带分发计时的拆分。
- 报告：
  - 首次进入到第一帧完整画面（门槛 ≤1 s）；
  - 再次进入 p95（≤300 ms）；
  - 拖动、滚轮、勾选通道、Intensity 的呈现延迟；
  - GUI 卡顿 p95（≤50 ms）；
  - 资源峰值；
  - 与 WSL 中位数逐项对照。

### 23.5 codex 审查（已并入）
- 结论只能当旁证：服务器硬件与 WSL 机器不同，不能单凭这次测量断定“是 WSL 造成的”。
- 重建的 3 层项目缺少预分割、融合和分割的 run，首次进入时少装的内容远超 109 ms，所以 3 层只作参考，能直接对比的是 7 层。
- 每次的原始值都列出；首次完整画面必须真的测到，不能用工具估出的值顶替。
- 不用 `run_entry.sh`（它会强制打开分发计时）。
- 日志必须标明 `a9.platform wsl=False`。

### 23.6 执行记录与结果（2026-10-08）

**环境问题（已处理）**
1. **第二个显示输出拖慢刷新**：服务器另有一个输出 `VGA-2-1`（1024×768，走 `modesetting`，不在 NVIDIA 卡上）。有它在时，合成器每秒只出 1 帧：最小 GL 窗口在关掉垂直同步、swapInterval=0 的情况下，也是每 1000 ms 换一次缓冲。用户同意后执行 `xrandr --output VGA-2-1 --off`，之后变为 16.7 ms（60 Hz）。之前的 16 次运行全部作废，日志移到 `bench_a9/invalid_1hz/`。
2. **空闲锁屏**：GNOME 空闲 300 s 后锁屏并关闭显示器，之后又回到每秒 1 帧。脚本模拟的操作不算用户活动，所以 A9-7 资源那两次运行发生在锁屏之后，计时作废，只保留内存和显存峰值。f7_3 运行中 fusion 导航段的末尾有几秒也出现每秒 1 帧，该段剔除。

**有效数据**：20:02–20:28，60 Hz，窗口可见。每组 1 次热身加 3 次正式运行，日志都标为 `wsl=False`，没有剔除任何 WSL 帧，也没有 error 或 invalid。

| 指标（门槛） | 7 层：3 次原始值 | 7 层 WSL 中位 | 3 层：3 次原始值（无预分割，仅参考） | 3 层 WSL 中位 |
|---|---|---|---|---|
| 首次进入 Step1 到第一帧完整画面（≤1 s） | 1311 / 1132 / 1204 ms | 1.0–1.16 s | 1748 / 1754 / 1712 ms | 1.85 s |
| 再次进入 Step1（p95 ≤300 ms，每次运行 2 次） | 312, 509 / 262, 209 / 311, 291 ms | 195–232 ms | 213, 302 / 290, 309 / 314, 304 ms | 289–334 ms |
| 全场景 step 呈现 p95（含进出） | 296 / 363 / 325 ms | | 381 / 375 / 350 ms | |
| 拖动呈现 p95 | 30 / 32 / 31 ms | | 40 / 40 / 39 ms | |
| 滚轮呈现 p95 | 20 / 19 / 29 ms | | 25 / 21 / 21 ms | |
| 勾选通道呈现 p95 | 26 / 27 / 29 ms | | 37 / 34 / 38 ms | |
| Intensity 呈现 p95 | 23 / 18 / 20 ms | | 18 / 22 / 36 ms | |
| GUI 卡顿 p95（全程，分析器口径，>32 ms 的卡顿） | 243 / 288 / 341 ms | 220 ms | 214 / 229 / 207 ms | 68 ms |

**按场景段拆的 GUI 卡顿 p95（>32 ms 的卡顿）**：
- 7 层：Step0 导航 66–94；Step1 overlay 导航 77–79；fusion 导航 80–83（剔除 f7_3）；Intensity 段 234–327（每次 3–4 次卡顿）；进出切换 541–671 ms。
- 3 层：Step0 导航 58–93；overlay 导航 71–73；fusion 导航 59–72；勾选通道 72–82；Intensity 段 213–244；进出切换 733–761 ms。
- 启动阶段（打开项目到场景开始）各有 1.9–3.3 s 的卡顿，不在 A9 的门槛内。

**首次进入拆分**（`first_entry.py`）：
- 7 层（e7_2，1132 ms）：
  - `go_step1` 594 ms，其中查看器挂载 274 ms、GL 显示 133 ms、恢复 167 ms；
  - 之后两次整幅上传，各约 96 ms 和 43 ms；
  - 未计入的空洞 219 ms。
- 3 层（ea5_2，1754 ms）：
  - `go_step1` 661 ms；
  - 接着约 630 ms 的 GUI 空闲，在等后台读最粗层；
  - 然后上传 95 ms，加上 Tissue Preview 合成之后的一次整幅上传 251 ms；
  - 未计入的空洞 409 ms，其中最大的一个约 255 ms。

**A9-7 资源（补测，2026-10-09 00:35–00:40）**：
- 补测前关闭了 GNOME 空闲黑屏（idle-delay 0）、自动锁屏和变暗，并再次关闭 VGA-2-1。最小 GL 窗口确认为 16.7 ms，即 60 Hz。
- 两次全场景运行的计时都正常：拖动 p95 分别为 32 ms（7 层）和 39 ms（3 层）。
- 7 层：进程树 RSS 峰值 4.97 GB，显存峰值 662 MB；
- 3 层：5.44 GB，显存 902 MB；
- swap 0，系统可用内存最低 116 GB。
- 锁屏期间那两次运行作废，移到 `bench_a9/invalid_locked/`。

注意：在 16 GB 的目标机上数值会不同，这里只说明没有失控增长。各缓存是否不超过声明上限，需要另外对照 A5 清单。

**结论（初步）**：
- 在原生 Ubuntu、60 Hz、RTX 4090 上，A9 剩下的几处没过门槛的指标都依然存在：
  - 7 层首次进入 1.13–1.31 s，超过 1 s；
  - 再次进入最大 509 ms；
  - 导航中的 GUI 卡顿 p95 为 60–90 ms，超过 50 ms；
  - Intensity 段有约 250 ms 的卡顿。
- 这些不是 WSL 的假象。7 层（同一种项目）在 Ubuntu 上的首次进入和再次进入并不比 WSL 快。
- WSL 的唤醒帧只影响 WSL 机器上的个别帧，不是主要原因。
- 硬件更强也没有改善这些段，说明瓶颈在 GUI 线程上的工作量（挂载、恢复、整幅上传、合成），不在 GPU 算力。

## 24. 任务三：A9 剩余门槛的优化方案（草案 v0 → 见 24.7 的 v1，2026-10-09）

### 24.1 依据：服务器上的诊断测量（只读，不改代码）
- 被测代码：`7ac1081`。服务器条件同 §23（关闭 VGA-2-1，确认 60 Hz）。测完已恢复 VGA-2-1。
- 7 层、3 层全场景各跑 1 次：一次开分发计时（`BLOCK01_PERF_DISPATCH=1`，阈值 15 ms），一次用 py-spy 采样界面线程（250 Hz）。
- 日志：`bench_a9/run_d7.log`、`run_da5.log`、`spy_s7.json`、`spy_sa5.json`。

**更正 §23.6 的一处归因**：Intensity 段那约 250 ms 的卡顿，其实是场景末尾"回 Step1 → 回 Step0"两次切换步骤造成的。场景在 Intensity 之后没有新的分段标记，所以被算进了 Intensity 段。调 Intensity 本身没有超过门槛的卡顿：呈现 p95 为 18–36 ms，0 次读盘。

### 24.2 门槛现状（Ubuntu，§23.6 数据）

| 门槛 | 现状 | 是否达标 |
|---|---|---|
| A9-1 冷启动后首次进入 ≤1 s | 7 层 1.13–1.31 s；3 层 1.71–1.75 s（参考） | 否 |
| A9-1 再次进入 p95 ≤300 ms | 7 层 209–509 ms；3 层 213–314 ms | 否（贴线） |
| A9-3 目标层到位 p95 ≤100 ms | 拖动 30–40 ms，滚轮 19–29 ms | 是 |
| A9-4 切换步骤，单次阻塞 ≤100 ms | 每次切换约 230 ms（两个方向都是） | 否 |
| A9-4 拖动卡顿 p95 ≤50 ms、最长 ≤100 ms | overlay 导航 p95 77–79 ms、最长 124–211 ms | 否 |
| A9-4 / A9-6 切换通道、调亮度、切换模式 | 呈现 p95 均 ≤40 ms | 是 |
| A9-2 导航 0 帧缺块 | 每次运行 2–3 帧（1/16 个通道的粗层未到），出现在步骤切换前后 | 待核 |
| A9-5 / A9-8 / A9-7 | 调亮度 0 次读盘，0 次程序跳转，资源正常 | 是 |

### 24.3 归因

**(a) 拖动和滚轮的卡顿。** 每次卡顿通常是 2–3 次"出一帧"背靠背执行，每次约 20 ms：
- 输入事件（MouseMove / Wheel）里立即发布一次；
- 帧定时器（O2 攒批的 `PUBLISH_FRAME_MS`）随后又为新到的图块发布一次。
- 两者相隔不到一帧，心跳插不进去，分析器就把它们连成一次 40–60 ms 的卡顿。

每次发布的 `gpu.submit` p50 为 8–12 ms，其中：
- 渲染 3–6 ms；
- 覆盖探针 2.5 ms，仅测量时存在；
- 其余是准备、上传和 GL 错误检查。

输入事件里除了提交，还有约 7 ms 其他工作。py-spy 能看到的包括：
- pyqtgraph 的范围变化；
- Step1 → Tissue Preview 的视口矩形发布；
- **Step0 隐藏的整图查看器跟随相机并更新视口矩形**（`_on_full_image_camera_moved` → `_update_full_image_view_rect`，`step0_page.py:5787、5900`）。

结论：每个显示帧最多应只提交一次；不在屏幕上的视图不该跟着每次输入做事。

**(b) 切换步骤，每次约 230 ms。** 分发计时的中位数：
- `switch.rm_save_view` 83 ms（最长 108），每次切换都执行（`main_window.py:7055`）。一次切换里会多次读写 67 KB 的 session.json：Step0 草稿、Step1 会话、Step2/4 草稿、当前步骤。
- `step1.entry.dock_mount` 96 ms（最长 182），每次切换都执行（`main_window.py:5422`）：把同一个通道停靠栏挂到新页面。
- `activate` 27 ms，`dock_set_step` 14 ms，`camera` 10 ms。
- 回到 Step0（`switch.go_step0`）也是 227 ms。

**(c) 首次进入 7 层（e7_2，1132 ms）。**
- `go_step1` 594 ms：会话保存 65、查看器挂载 274（其中 GL 显示 133）、停靠栏 49、恢复 167；
- 处理函数返回后约 130 ms 的空洞（整窗重绘等）；
- 两次整幅上传 96 + 43 ms。

上面 (b) 里的两项也在这条路径上。

**(d) 首次进入 3 层（1754 ms，仅参考）。** 比 7 层多出两段：
- 约 630 ms 界面线程空等后台读取最粗层（13 个通道 × 1929×2026）；
- 一次 251 ms 的整幅上传，发生在 Tissue Preview 合成之后。

### 24.4 措施（每项独立、可单独回退；每项先测再改；每项之后用同一场景新旧交替各 3 次重测；全部达标即停）

- **T1 切换步骤时的会话保存**：
  - 先给 `_rm_save_view` 的四个部分各加计时。
  - 改法：一次切换只读一次、写一次 session.json，把四份内容合并写入。内容和时机不变，仍在切换时同步完成。
  - 关窗时的保存路径不动。
  - 不改成后台写，也不推迟：会话属于受保护的数据，延后写会扩大丢失窗口，需要另行裁定。
  - 预计每次切换省 40–60 ms（首次进入和再次进入都受益）。
- **T2 停靠栏挂载**：
  - 先测 `_mount_channels_dock` 里各段的时间（重设父控件、布局、列宽同步）。
  - 只在测出明确的冗余时才改，例如目标页面和尺寸都没变时跳过重新布局。
  - 不改"一个停靠栏、同一批行"的设计（W1 和 UI_SURFACE_RULES）。
- **T3 每个显示帧最多提交一次**（`ui/step1_gpu_binding.py`，参考 Odon 的帧循环）：
  - 用户输入仍然立即更新视口；
  - 但如果本帧已经提交过（输入或帧定时器），下一次提交就合并到下一个帧槽，最多晚 16 ms。
  - 沿用 O2 的代际和取消规则；粗层回退画面始终完整。
  - 门槛：拖动卡顿 p95 ≤50 ms、最长 ≤100 ms；A9-3 到位 p95 ≤100 ms 仍达标；覆盖不变。
- **T4 不在屏幕上的 Step0 整图不跟随每次输入**：
  - Step1 / Step3 在屏时，Step0 整图的相机跟随和视口矩形更新只记一笔欠账，回到 Step0 时补一次。
  - 沿用 O1 的"欠账 / 补画"机制（`_owe_own_redraw`、`_replay_owed_redraws`）。
  - A1 / A7 的相机不变量必须保持：回到 Step0 时，相机与 Step1 一致。
  - 先测这条路径在每次输入里的实际耗时，太小就不做。
- **T5（有条件）提交开销**：
  - T3、T4 之后拖动仍不达标时，再把 `gpu.submit` 中非渲染的部分拆开测（PyOpenGL 自动错误检查、显式 `_check_gl`、上传）。
  - 再决定是否在热路径上关闭自动错误检查（原 O4 候选）。
- **T6（只针对 3 层，有条件）首次进入的粗层**：
  - 先核实 Step0 是否已经在内存里有这些通道的最粗层（概览记录、显示种子）。
  - 如果有，首次进入直接复用，省掉约 630 ms 的读盘等待。
  - 如果没有，先上传降采样后的粗层，再换成完整粗层（交接清单里的"3 层首次上传先用降采样粗层"），可减少 251 ms 的上传。
  - 3 层是否 ≤1 s，以原生 Windows 实测为准（用户裁定）；本项只在 7 层已达标后再评估。

**不做**：
- GPU 保温（用户裁定）；
- Rust；
- 改变 Tissue Preview 实时跟随；
- 把会话写入改为异步；
- 预建 GL 控件（O3 实测有害）。

### 24.5 验收
- 服务器同 §23 的条件。新旧代码交替运行，每个场景各 3 次，报告每次的原始值。
- 门槛：
  - 7 层首次进入 ≤1 s；
  - 再次进入 p95 ≤300 ms；
  - 切换步骤单次阻塞尽量 ≤100 ms：GL 显示等 Qt / 驱动部分单列，不计入代码优化的目标；
  - 拖动卡顿 p95 ≤50 ms、最长 ≤100 ms；
  - 其余门槛不退步。
- 不变量：
  - 调亮度 0 次读盘；
  - 0 次程序跳转；
  - A1 / A7 相机一致；
  - 像素不变；
  - 会话内容与改动前逐字段一致（T1）。
- 每块：codex 代码审查、定向回归（新旧对照），以及用户真机验收。

### 24.6 白名单（预计）
- T1：`ui/main_window.py`（`_rm_save_view` 一处，约 +15/−10）、`utils/run_store.py`（如需一次性写入的辅助函数）、测试。
- T2：先测再定。
- T3：`ui/step1_gpu_binding.py`、测试。
- T4：`ui/step0/step0_page.py`（约 +10，接线）、测试。
- T5、T6：先测再定，另行报批。
- 两个大文件只加接线；不新增哈希、调度器、缓存或状态机。

### 24.7 codex 审查与修订（方案 v1；已被 §25 取代）

codex 的意见已逐条对照代码和日志核实，结论如下。

**测量与归因的更正**
1. **门槛不改**：A9-4 仍按 §13 锁定的端到端门槛判定。Qt / 驱动部分只单列报告，不从门槛里扣除。A9-7 还缺各缓存逐项对照声明上限这一步，补进本块的测量项。
2. **测量工具自身造成的卡顿**：驱动 `scripts/a9_drive.py:75` 的 `_viewer_widget` 在每个手势开始时遍历全部控件找查看器，每次约 147 ms，记在 `a9.action` 到 `a9.target` 之间（例：`run_d7.log:15049`）。这些被算成了产品卡顿。
   - 扣除含驱动这一步的卡顿后，overlay 导航的卡顿次数少了约 40%，但 p95 仍为 64–87 ms，最长 102–211 ms。产品自身的卡顿是真实的。
3. **拖动卡顿不全是两次背靠背发布**：
   - d7 overlay 段超过 32 ms 的卡顿里：61 次只含 1 次发布，17 次含 2 次，5 次一次也没有；da5 为 132 / 30 / 1。
   - 另有个别单次 `gpu.render` 长达 168 ms（`run_d7.log:6012`）。
   - 所以"背靠背发布"只是原因之一。主体是每次发布约 20 ms，加上输入事件里的其他工作，再加上偶发的长渲染。
4. **T4 撤销**：`_update_full_image_view_rect` 不在 Step0 时已经直接返回（`step0_page.py:5900`），另一分支什么也不做（`:3514`）。没有证据表明隐藏的 Step0 在持续跟随相机。
5. **切换步骤的数字因运行而异**：正式运行（f*）里会话保存约 107–118 ms，停靠栏挂载约 51–60 ms。T2 设想的"同一宿主跳过"已经存在（`global_dock.py:603`）。
6. **3 层那约 630 ms 未证实全是等读盘**：`first_entry.py` 整段只有 409 ms 无归属。

**修订后的块与顺序**

- **M0 测量先行**（只改测量工具，不改产品行为）：
  - `scripts/a9_drive.py`：找查看器时先复用上一次的结果，失效时才全量遍历；驱动自己的工作记为 `a9.driver` 计时段，分析器单独报告。
  - `docs/perf_timeline/analyze_perf_log.py`：卡顿统计排除驱动段，并同时给出原始值。
  - 给 `_rm_save_view` 的各部分、`_mount_channels_dock`、`gpu.submit` 中非渲染部分（上传、`_check_gl`、PyOpenGL 自动检查），以及导航时隐藏的 `cpu-step1` 覆盖工作加细计时。
  - 补 A9-7 的逐缓存对照。
  - 之后在服务器上重测基线，再按数据决定下面各项的取舍。
- **T1 切换步骤的会话保存：一次读、一次有序变换、一次写**：
  - 严格保持原有顺序：Step0 草稿 → Step1 会话 → Step2/4 草稿 → `view.current_step`。
  - 每个写入方的目标位置和启用条件不变，包括：
    - Step1 的恢复守卫；
    - Step2/4 的待恢复守卫；
    - `edited_against`、项目相对路径、未知字段、`view` 的其他成员；
    - `put_draft` 的 `_kept` 顺序。
  - Step0 工作区与 `_rm_roi_dir()` 的取法不同：两者不一致时不合并，分开写。
  - 已知边界：最终内容逐字段相同，但中途失败时的效果和原来不同。原来是写一半算一半，现在要么全写、要么全不写。这一点交用户确认。
  - 关窗时同样走这个保存函数，同样按此规则。
  - 测试：新旧代码在各种草稿 / 守卫组合下，写出的 session.json 逐字节或逐字段一致。
  - 白名单：`ui/main_window.py`（`_rm_save_view` 及其三个保存函数改为"对传入的会话做变换"）、`ui/step0/step0_page.py`（`rm_save_draft` 拆出变换部分）、`utils/run_store.py`（如需辅助函数）、新测试 `tests/test_a9_t1_session_save.py`。
- **T2 停靠栏挂载**：按 M0 的子计时找出冗余，再报批。可能涉及 `ui/widgets/channel_dock/global_dock.py`。
- **T3 每帧最多提交一次（实验）**：
  - 按 M0 的数据，确认背靠背发布的占比值得做时才做。
  - 调度规则：
    - 视口计划始终立即执行；
    - 提交的截止时间从上一次提交**结束**算起，不因每次输入重置；
    - 与 O3 的"同一事件轮次合并刷新"（`step1_gpu_binding.py:275`）统一成一个规则。
  - 这会修改现有测试 `tests/test_step1_gpu_sources.py:1340、1349` 中"相机变化立即提交、首次显示刷新立即提交"的约定，需要用户批准这一行为变化。A9-3 到位 p95 ≤100 ms 用实测保证。
- **T5、T6**：仍有条件做，按 M0 数据另行报批。
  - T6 要区分原始概览和显示种子，与校正后、按 ROI 遮罩的科学平面不能混用。
  - 降采样的回退画面要保持映射、有效区域、来源标识和目标层覆盖统计。

**请用户裁定**
1. 先批 M0（测量工具 + 细计时，产品行为不变），做完后带着数据再批 T1 / T2 / T3。
2. T1 的"要么全写、要么全不写"语义是否可以接受。
3. T3 会改变"相机一变就立即提交"的约定（最多晚一帧），是否允许试验。

## 25. 任务三方案 v2：以 Odon 为蓝本的流畅缩放/拖动 + Step1 复用 Step0 数据（2026-10-09，待 codex 审、用户批准）

### 25.1 用户要求（2026-10-09）
1. 缩放（zoom in/out）和拖动要“绝对流畅”。现在缩放有时会在某个倍数卡住。
2. 从 Step0 进入 Step1 时，Step1 要直接复用 Step0 已加载的数据。
3. 所有修改都必须尽可能参考 Odon 的设计思路。Odon 是 GPL-3.0，只参考设计，不复制、不翻译代码。

### 25.2 调研结论（只读；Odon `b01faef`；本仓库 `7ac1081`）

**A. “在某个倍数卡住”的根因（代码确认，现场条件待测量确认）**
- 选层规则：`viewer/request_planning.py:45` 的 `pick_display_level` 选的是“不超过理想值的最大下采样”，也就是偏细的一层。在每一层缩放范围的末端，要读的图块最多是 1:1 所需的“倍率²”倍：4 倍金字塔是 16 倍，2 倍金字塔是 4 倍。
- Step1 的细层预算：`ui/step1_gpu_binding.py:599-612` 规定，每个通道的目标层超过 256 块，或超过 `GPU_FINE_BYTES_PER_CHANNEL` 48 MB（float32 下等于 48 块 512² 图块）时，**拒绝**加载细层，并且不改用更粗的一层。画面停在全片粗层上，在这一带怎么缩放都不会变清楚，角标还写着 "fine budget refused"。`_last_error` 只在换数据源时才清除（`:210`），所以离开这一带后提示也可能还留着。
- 按 512 图块、4 倍金字塔估算：视图 997×695 时，每层约 7% 的缩放范围会被拒绝；1380×900 时约 27%；1840×1250 时约 50%。2 倍金字塔要到约 2760×1900 才开始被拒。
- 自动测量的窗口只有 997×695，而且 Step1 的缩放一直在第 0 层附近，所以从来没进入过这个区间，复现不出来。用户在大窗口里看 4 倍金字塔的 OME 文件时会遇到。
- Odon 的做法：按 `|ln(zoom·downsample)|` 最小来选层，即最接近 1:1 的一层，屏幕上每个像素对应 0.7–1.4 个图像像素（`imaging/tiling.rs:16-33`）。它从不拒绝目标层，只按每帧请求预算排队。

**B. 其他让缩放和拖动不顺的因素**（日志与代码）
- Step0 跨层时，到达的图块逐块在界面线程上处理：每块都新建一个 `ImageItem`，并对整个池做一遍可见性检查（`viewer/explore_view.py:5256、5326`），没有每帧上限。跨层那一下界面会卡 97–106 ms；画面完全变清晰要 120–200 ms，期间显示放大的粗层。
- Step1 每次提交都会把**最粗层的每一块**当作一个平面绘制，不管它在不在视口内（`ui/step1_gpu_layer.py:1033`）：3 层是 240 个平面，渲染约 5.6 ms；7 层是 75 个平面，约 3.5 ms。这个开销随通道数线性增长，29 个通道约翻倍。
- Step1 的 GL 画面只在提交时更新，手势期间最多约 30 Hz（`motion_interval_ms=33`）。每个滚轮格的界面线程开销：7 层约 16 ms，3 层约 27 ms。这包含测量用的覆盖探针约 4 ms。
- 每个事件都会把 Tissue Preview 的视口框删掉再新建一个 `PlotDataItem`（`overview_panel.py:3043`），约 1 ms。
- **测量工具自身**：驱动每个手势开始前遍历全部控件，平均 18 ms，最长 117–181 ms。这部分不该算到产品头上。
- 已排除：缩放限制（全仓库没有 `setLimits`）、层级来回跳动（有 0.2 的滞回）。

**C. Step1 为什么没有复用 Step0 的数据**
- Step1 自建了一套：新的读取器、两个各自的 LRU（原始 512 MB、校正后 2 GB）、调度器和 GPU 纹理（`ui/step1_viewer_host.py:400-405`）。
- 缓存键不同：Step1 是 `stage="step1"` 加 ROI / 决策令牌，Step0 是 `stage="raw"`。即使共用同一个缓存对象，也永远对不上。
- 内容也不同：Step1 存的是按 ROI 裁剪、ROI 外为 NaN 的 float32；校正通道用的是保存的生产产物，而 Step0 是交互预览质量。Step0 的校正结果**永远不能**给 Step1 用。
- Step0 一次只显示一个通道，实测只看过 DAPI。所以按现状直接复用，首次进入只能省 3–5% 的读取；Step1 要的是 14 个通道的粗层和视口细层。
- Step1 首次进入时自己内部还有重复：3 层项目里，Tissue Preview 为 14 个通道各读一整张粗层平面，占这次进入读取像素的 42%，同时 Step1 又按图块读了一遍同一层。
- Odon 的做法：缓存是唯一数据源，键为 `(数据集/平面, 层, 块, 通道)`，**不含任何显示状态**。颜色、对比度、可见性都在着色器里做（`render/tiles_raw.rs:19-26`）。Mosaic 模式下，多个对象共用一个缓存和一组读取线程（`mosaic/io.rs`）。

### 25.3 采用的 Odon 设计原则 → 我们的措施

| Odon 原则（文件:行） | 我们现在的状况 | 措施 |
|---|---|---|
| 选层取最接近 1:1 的一层（`tiling.rs:16-33`） | 偏细一层，最多 16 倍图块 | Z1 |
| 从不拒绝目标层，只按每帧预算排队 | 细层预算拒绝，画面卡在粗层 | Z1 |
| 粗层兜底 + 放大时保留中间层阶梯 + 缩小时短暂保留原细层（`app.rs:12927-12967`，400 ms / 80%） | Step1 有粗层兜底和替身保留；Step0 只有粗层底图 | Z2 |
| 只画可见块，从粗到细逐通道覆盖（`tiles_gl.rs:246-278`） | 粗层所有块每帧都画 | Z3 |
| 每帧统一处理到达结果，一帧一次（`app.rs:5594`） | Step0 逐块处理；Step1 已有 16 ms 攒批（O2） | Z4 |
| 视口小时预取下一细层的周边一圈（TargetAndFinerHalo） | 只预取本层外一圈 | Z5 |
| 缓存键不含显示状态，多视图共用一个缓存（`tiles_raw.rs:19-26`，mosaic） | Step0、Step1 各自缓存，键不同 | R1、R2 |
| 每帧上传预算（Odon 没有，调研建议补上） | Step1 上传无预算 | Z4 |

### 25.4 分块（每块单独报批；先测量、再改、再按同一场景新旧对照重测；达标即停）

- **M0 测量先行**（只改测量工具，产品行为不变）。在 §24.7 M0 的基础上再加：
  - 窗口最大化（1920×1080）下的 Step1 缩放场景：从全片一路放大到第 0 层再缩回；一次每格都等稳定，一次连续 20 ms 一格。
  - 计时标记：被预算拒绝的通道、层、块数和字节数；每次发布的层级和平面数；滚轮事件从产生到被处理的排队时间。
  - 驱动查找控件时复用上次结果；覆盖探针的耗时单列。
  - 目标：在服务器上**复现“卡住”**，并给出以下各块的基线。
- **Z1 选层改为最接近 1:1，不再拒绝目标层**：
  - `pick_display_level` 增加一种 Odon 式的选法，Step0 和 Step1 共用；保留现有滞回。
  - Step1 的细层预算按视口推算：最接近 1:1 时，视口所需的块数有上界。超出时退到更粗的一层显示，并照常请求，不再“拒绝并停在粗层”。
  - 提示文字随状态清除。
  - 影响：中间缩放倍数下读取量下降，最多降到约 1/16。画面在 0.7–1.4 倍像素密度内是清晰的，偏细一层的“超清”不再有，与 Odon 一致。
  - 文件：`viewer/request_planning.py`、`viewer/explore_view.py`（调用处）、`ui/step1_gpu_binding.py`、测试。
- **Z2 放大保留阶梯、缩小保留原细层**（参考 Odon 的 sticky ceiling 和 zoom-out floor）：
  - Step0 和 Step1 在新目标层到位之前，画面由“粗层 + 上一个目标层”组成，不会退回全片粗层。
  - 缩小时，原细层继续画最多 400 ms，或者等新层到位 80% 为止。
  - 先核实 Step1 现有的替身保留（`_retain_fine`）已覆盖哪些情况，只补缺口。
- **Z3 Step1 只画可见的粗层块**：对粗层平面做视口裁剪，每帧平面数和平面像素随视口大小而定，不再随整张片子大小增长。像素不变。
- **Z4 每帧统一收结果，并设上传预算**：
  - Step0 到达的图块先放进待处理区，每帧统一安装一次，按时间设上限，可见性只重算一次。
  - Step1 每帧最多提交一次（即 §24.7 的 T3），每帧上传字节数设上限，超出的部分下一帧再传。
  - Tissue Preview 的视口框改为更新已有对象，不再每次删除重建，并按帧合并。
- **Z5 放大方向的预取**：可见块较少时，预取下一细层中心附近的一圈（Odon 的 TargetAndFinerHalo），每帧有预算。跨层后清晰得更快。
- **R1 Step1 进入时去掉内部重复读取**：Tissue Preview 需要的整张粗层平面与 Step1 的粗层图块共用一次读取。只在层级相同、并且该通道在 Step1 用原始数据时共用。
- **R2 Step0 和 Step1 共用一个原始图块缓存**（Odon 原则：缓存是唯一数据源，键不含显示状态）：
  - 原生 dtype 的原始图块缓存，键为 `(路径, 指纹, 通道, 层, tx, ty)`，Step0 整图、比较条、Step1、Step3、montage 共用。
  - Step1 的 ROI 遮罩和 float32 转换移到缓存之后再做，校正产物不进这个缓存。
  - Step0 空闲时，在后台把**所有通道**的最粗层预读进这个缓存。这样从 Step0 进入 Step1 时，14 个通道的粗层都已在内存里，这才是“复用 Step0 的数据”真正省时间的做法。
  - 两个 512 MB 的原始缓存合并成一个，上限降低 512 MB。
  - 这是新增的跨模块缓存，按 AGENTS 第 4 条需要明确批准：换数据集时清空，生命周期归主窗口。
- **T1、T2**（来自 §24.7）：切换步骤时会话只读一次、写一次；停靠栏挂载先测再定。这两项影响切换步骤和首次进入的门槛。

**建议顺序**：M0 → Z1（直接解决“卡住”）→ Z3、Z4（每格和每帧的开销）→ Z2、Z5（跨层的平滑度）→ R1 → R2 → T1、T2。

### 25.5 流畅的验收标准（新增，与 §13 锁定门槛同时生效）
- 在 1920×1080 最大化窗口和 997×695 两种尺寸下，从全片一路缩放到第 0 层再缩回：
  - 不出现 fine budget refused；
  - 每格到完整画面 p95 ≤100 ms，到首帧 ≤50 ms；
  - 缩放过程中界面卡顿 p95 ≤50 ms、最长 ≤100 ms。
- 拖动卡顿 p95 ≤50 ms、最长 ≤100 ms（§13 A9-4）。
- 从 Step0 进入 Step1：所有勾选通道的粗层 0 次读盘（R2 之后）；首次进入 ≤1 s。
- 像素：在同一层级下渲染结果与改动前相同。Z1 改变的是“同一缩放下选哪一层”，这是有意的行为变化，单独验收。

### 25.6 codex 审查与修订（方案 v3，待用户批准）

codex 的意见已对照代码核实，采纳如下。

**诊断更正**
- "卡住"是**画面不再变清楚**，相机本身照常移动。画面是"粗层 + 之前保留下来的细图块"，不一定只有粗层（`_retain_fine`，`step1_gpu_binding.py:644`）。M0 必须把"相机在动"和"细节没出来"分开测。
- 预算单位按 MiB 计：每个通道 48 MiB，每个视口最多 256 块（`step1_viewer_mount.py:104-114`）；全部纹理上限 512 MiB（`resource_tiers.py:74`）。
- 窗口 1920×1080、4 倍金字塔时，在每一层缩放范围的粗端，要读的块约 135–160 块，远超 48 块，会被拒绝。实际图像视口比窗口小，以 M0 实测为准。
- Step0 本来就有中间层兜底（`explore_view.py:4578、4950、1468`），也没有类似的拒绝。
- Odon 的"最接近 1:1"在 4 倍金字塔上，屏幕像素对应约 0.5–2 个图像像素；在 2 倍金字塔上约为 0.7–1.4。

**关键约束（新增）**
- 按"最接近 1:1"选层后，1920×1080 视口每个通道约 40–54 块。
- 15 个通道按 float32 计算，约需 600–810 MiB 纹理，超过 512 MiB 的总上限；29 个通道更多。
- Odon 用 16 位整数纹理（R16），在着色器里做窗口和颜色映射。
- 所以要做到"任何倍数都不卡"，必须二选一或两者都做：
  - (a) 改用原生 dtype 纹理：uint8 或 uint16，按 Odon 的做法，这就是原 O4。纹理占用降到 1/4 或 1/2。
  - (b) 提高纹理上限：在 6 GB 显卡档位里调整 `resource_tiers`。
- 校正通道是 float32 产物，仍需 float 纹理，或者按产物范围量化。量化会改变像素，不建议，所以校正通道保持 float。

**分块修订**
- **Z1 只先改 Step1**：
  - 选层策略做成每个查看器各自设置。Step0 的校正预览、比较条和 montage 都不改：Step0 的预览是在所选层上按缩放后的参数计算的，换层会改变预览本身。
  - 按 1:1 重新设计滞回：现有的 0.2 阈值在 4 倍层边界两侧都会越过，挡不住来回跳。
  - 细层预算按视口推算。预算不够时，退到更粗一层作为**临时**画面，照常请求目标层，不能把降级当作"已到位"。
  - 重新计算回退层的图块坐标；保留全部纹理的总上限。
- **Z1b（新）原生 dtype 纹理**：
  - 原始通道按 uint8 或 uint16 上传，着色器里归一化后再套用同样的窗口和伽马。
  - 校正通道仍用 float32。
  - 同一层级下渲染结果逐像素对照：允许量化误差为 0，因为 uint8 / uint16 转 float 是精确的。
- **Z2**：先复用 Step0 现有的兜底和 Step1 的保留机制。保留的像素保持原来的层级、来源和校正身份，绝不当作目标层图块存进缓存。按通道限制覆盖范围和内存。
- **Z3**：按平面的实际矩形和当前相机做裁剪，边缘相交、绘制顺序、ROI 透明度都保持不变；"粗层完整驻留"和"本帧画哪些"分开处理。
- **Z4**：改变"立即提交"的约定需要批准。待处理数组和上传字节都设上限；过期的结果走现有的生命周期检查；首个粗层不能只装一半就显示。
- **Z5**：只在测到收益时才做。预取要排在可见块和粗层之后，遵守缓存上限；它会产生真实读盘，调亮度和切换通道期间的 0 次读盘门槛要把它算进去。
- **R1**：共用的是**未遮罩的原始数据**，各自再做变换。Step1 那份按 ROI 裁剪、带 NaN 的平面不能拿给 Tissue Preview 用。
- **R2**：需单独批准，并给出具体文件和行为白名单。
  - 缓存键保留数据集、平面、网格身份；共享的原始数组只读不改，ROI 遮罩输出另外生成。
  - Step0 的交互校正永远不进 Step1。
  - "进入 Step1 时所有勾选通道的粗层 0 次读盘"只适用于原始通道，而且以预读已完成、未被淘汰为前提。
  - "省下 512 MiB"要先盘点比较条等其他缓存的归属（`compare_strip.py:235`）。
- 必须同步更新的现有测试：
  - `test_request_planning.py:65、69、78-128、239`；
  - `test_step1_gpu_sources.py:940、999、1220、1318、1348`；
  - 同时保留 `:458、497、574、897` 的约定。
  - 改这些测试必须对应已批准的行为变化，不能只是放宽断言。

**"绝对流畅"的验收（建议，请用户定）**
- 现有门槛（卡顿 p95 ≤50 ms、最长 ≤100 ms）仍可能看到顿挫。建议增加以下指标：
  - 手势进行中帧间隔 p95 ≤20 ms，最长 ≤50 ms；
  - 每格滚轮到完整清晰画面 p95 ≤150 ms。
- 场景包括：持续拖动和缩放、换向、冷数据和热数据、少通道和多通道（15 / 29 个）、3 层和 7 层、窗口最大化。
- 最终以用户真机目测为准。

**顺序**：M0 → Z1 + Z1b（解决卡住）→ Z3、Z4 → Z2 → R1 → R2（单独批准）→ T1、T2；Z5 视测量结果再定。

### 25.7 独立审核（用户转交，2026-10-09）与方案 v4（待 codex 审、用户批准）

**审核结论**：有条件批准，v3 小修后进入实施。下列关键事实已对照代码核实：
- `_TextureLru.prepare` 在当前活动集超过 512 MiB 时直接抛错（`step1_gpu_layer.py:333`）。所以不能简单取消拒绝、继续请求全部目标层，否则画质问题会变成提交失败。
- 图块在进入 GPU 之前已经被转成 float32，有效区由 `np.isfinite` 决定（`step1_gpu_binding.py:809`）；着色器靠 NaN / Inf 丢弃无效像素（`step1_gpu.frag:26`）。整数纹理必须另带一张有效掩膜。
- 3 层拼图的最粗层每个通道约 15.6 MiB（float32），29 个通道约 450 MiB，接近 512 MiB 的总预算。
- Z3 只裁剪绘制，不会减少已经驻留的纹理。
- R2 要求原始图块在 Step1 做 ROI 和来源处理**之前**共享。Step1 的 `stage="step1"` 身份带有科学来源和决策信息，不能和 Step0 的 RawKey 命名空间合并。

**v4 执行路线**（每块单独授权；每块只改一类东西，选层、纹理格式、提交调度不放在同一次提交里）：

1. **M0 测量与复现**：先降低测量本身的开销，再进行测量。
   - 测量开销：驱动查找控件改为复用上次结果；覆盖探针单列。
   - 场景：窗口最大化；3 层和 7 层；连续放大、缩小；15 个和 29 个通道。
   - 记录：
     - 第一帧和完整清晰画面分开计时；
     - 帧间隔，以及与 `frameSwapped` 帧号的对应；
     - Qt 事件排队时间；
     - 预算拒绝：通道、层、块数、字节数；
     - 上传平面数、绘制平面数、驻留显存、渲染时间，四者分开统计。
   - 目标：复现"细节停止更新"，并分清它与"整个界面冻结"。
2. **Z1a 目标层选择，只用于 Step1 / Step3 的查看器策略**（Step0 的交互校正、比较条、montage 都不变）：
   - 采用 Odon 式的对数距离选层，并按新的切换边界重新设计滞回。
   - **三个集合分开**：
     - 理想层：最接近 1:1 的那一层；
     - 准入层：从理想层往粗走，第一个能让**全部活动通道的粗层加上目标细层**都放进 GPU 总预算的层；
     - 显示层：当前已驻留、实际画出来的内容。
   - 准入层就是请求的目标。它总能放得下，所以 `prepare` 不会抛错。
   - 准入层比理想层粗时，画面照常更新到准入层的清晰度，状态标为"受显存限制的分辨率"。覆盖统计不能把它算成"理想层已完成"。
   - 29 个通道长期超出预算时，采用的策略就是"自动降一层并明确标注"：不分批渲染，不报错。
   - 提示文字随状态变化，并能清除。
   - 文件：`viewer/request_planning.py`（新增策略函数，现有函数不变）、`ui/step1_gpu_binding.py`、`viewer/explore_view.py`（只在 Step1 / Step3 的控制器上接入策略）、测试。
   - 现有测试 `test_request_planning.py:65、69、78-128、239` 保持不变，因为现有函数没改。`test_step1_gpu_sources.py:999、1220` 的"拒绝"约定改为"降层并标注"，需用户批准这一行为变化。
3. **Z3 可见粗层绘制裁剪（小实验）**：只减少绘制，不碰驻留。分开报告绘制平面数、渲染时间和显存。
4. **Z4 一帧一次（有条件）**：M0 证明冗余提交占主要比例时才做。
   - 只保留一个 GPU 提交时钟，把 O2 的 16 ms 攒批、O3 的事件轮次合并、33 ms 运动节流统一成一个规则，沿用现有的代际取消，不新增调度器或状态机。
   - 用户输入立即更新相机，绘制最多晚一个帧槽。
   - 首个完整粗层不能被拆开显示。
5. **Z1b 整数纹理（只在显存仍是瓶颈时做）**：
   - 先拿一个 uint16 原始通道做硬件原型，与 GL_R32F 对照：值和有效掩膜分开传；校正通道保持 float32。
   - 验证 ROI 透明度、显示映射、纹理计费、性能，以及渲染结果逐像素对照。
6. **R1 → R2**：
   - R1：先验证 Step1 内部的重复读取能低风险消除。
   - R2：按实测命中率再评估。必须在 Step1 处理之前共享原生数组（只读），保留 Step1 的科学身份、校正产物验证和失效逻辑；优先预读当前需要的通道，不无条件预热全部 29 个通道；需要单独审批所有权、生命周期、容量和回退方式。
- **暂缓**：Z2（现有机制已有大部分能力）、Z5（会增加读盘和内存）、T1 / T2。

**验收**：
- §13 的锁定门槛 A9-1 到 A9-8 不变。
- §25.6 的流畅度目标作补充：帧间隔 p95 ≤20 ms、最长 ≤50 ms，每格到完整清晰 p95 ≤150 ms。
- 第一帧和完整目标层分开判定；降层显示不算完成。
- 用 `frameSwapped` 和帧号核对屏幕上实际显示的画面，而不只看提交调用。
- 最终在目标档位验证（3060 6 GB、16 GB 内存）。不因为开发机资源充足就提高固定的 GPU 预算。

### 25.8 codex 对 v4 的契约补充（已核实，并入 v4）

**M0**
- **白名单**：
  - `scripts/a9_drive.py`、`docs/perf_timeline/analyze_perf_log.py`；
  - `ui/step1_gpu_binding.py`、`ui/step1_gpu_layer.py` 中只加计时；
  - 如需修正"完成"的判定，加入 `viewer/coverage_probe.py`。

  不改调度或渲染策略。
- **事件归因**：每个事件都记录投递时间和处理时间、相机 / 视口代际、提交号和最终呈现的帧号。分析器不能再只保留每个动作的最后一次输入时间。定时器迟到和事件排队分开统计。
- **"完成"的定义**：现在的覆盖探针把任何保留下来的细层平面都算作覆盖目标（`coverage_probe.py:219`）。改为记录目标层和显示层的层号。`frameSwapped` 只证明画面上屏了，不证明细节到位。
- **指标含义**：
  - 上传量取缓存计数的差值；
  - 平面绘制次数和全部渲染遍数分开；
  - 原始纹理驻留量和 GPU 总显存分开；
  - `gpu.render` 是 CPU 提交时间，不是 GPU 执行时间。
- **计时本身的开销**：不在测量中做数组扫描、控件遍历、回读、`glFinish` 或阻塞式 GPU 查询；计算量较大的计时参数先判断计时是否开启再计算。

**Z1a**
- **准入条件**：
  - 本帧要用到的平面（粗层、准入的目标层、保留的替身）去重后，总字节数 ≤ 原始纹理预算 512 MiB；
  - 同时满足每个通道的细层上限（48 MiB / 256 块）；
  - 先给完整目标层预留空间，替身只用剩下的总量和单通道余量。
- **可行范围**：
  - 粗层上限是每个通道 32 MiB / 1024 块，多通道时仅粗层就可能超过 512 MiB。所以要写明支持的范围，以及连粗层都放不下时怎么处理：明确提示，不抛错。
  - 降层可以连降多层，直到只用粗层。
- **字节计算**：按实际图块尺寸 × 4 字节计算，ROI 边缘的块更小，NaN 像素同样占纹理空间。
- **融合模式**：按正权重的通道、组和核通道计算；显存按去重后的平面算，绘制遍数按实际重复绘制算。
- **中途勾选通道**：准入要按全部活动通道重新计算，包括还在路上的粗层；沿用现有的代际取消。只改显示的操作仍须 0 次读盘。
- **其他显存**：Step3 标签纹理（256 MiB）和渲染目标另计。Z1a 只保证原始纹理不超预算。
- **滞回**：
  - 对数距离相等时，固定往一个方向选；
  - 升层和降层用不同的阈值；
  - 滞回不能把一个已经放不下的准入层留住。
- **完成状态分两种**：
  - "准入层已完成"：按它判定画面稳定，避免一直超时；
  - "理想层已完成"：单独报告。

  显示内容按通道、按区域记录，是不同层的混合。
- 限制提示用现有的状态显示位置，不新增控件。

### 25.9 M0 执行记录（2026-10-09，用户授权第一步）

**改动**（只改测量，不改产品行为）：
- `scripts/a9_drive.py`：
  - 查找查看器的结果按"步骤 + 视图代际"复用；查找本身记为 `a9.driver` 计时段。
  - 每个输入事件记录 `a9.post`，`a9.handled` 增加 `seq`，两者按序号配对，得到每个事件的排队时间。
  - 拖动时记录定时器迟到时间 `late_ms`。
  - 新增动作 `winstate`：窗口最大化，并记录窗口和视口的实际尺寸、DPR。
- `viewer/coverage_probe.py`：`gpu_frame` 增加可选参数 `target_index`。传入时额外输出：
  - `exact_fraction`：只统计目标层号的平面；
  - `fine_levels`。

  原有字段的含义不变。
- `ui/step1_gpu_layer.py`：把目标层号传给探针，只在计时开启时生效。
- `ui/step1_gpu_binding.py`（只加计时点）：
  - `gpu.frame`：每帧的层级、平面数、遍数、上传数、驻留显存、拒绝数；
  - `gpu.fine_refused`；
  - `gpu.submit_failed`：提交失败原本只记在内部，不进日志，这里补上。
- `docs/perf_timeline/analyze_perf_log.py`：新增 `--m0` 报告，`--a9` 的输出不变。
  - 每格滚轮的窗口在下一次输入或下一个动作处截止；
  - 只统计目标查看器的帧，并且只统计该格滚轮之后提交的帧；
  - 单独报告"在下一次输入前没有完成"的格数；
  - 帧间隔的窗口只跨越 `pause` 合并；
  - 最后一个手势的窗口截止到 `a9.end`。
- 测试：
  - `test_v16_a9m_coverage_probe.py` +2：替身层不算目标层完成；不传参数时结果完全不变。
  - `test_v16_a9m_analyze.py` +2：事件配对和完成判定；不同查看器的帧、旧的待提交帧不计入。

**审查**：codex 第一轮提出 2 条必须修改，都在分析器里：每格的窗口过长、跨了查看器；最后一个窗口的截止时间。已修复，并补了测试。

**定向回归**（新旧对照）：相关 11 个文件结果完全一致；新增测试通过。

**服务器实测**：60 Hz，窗口最大化，查看器 1325×806，DPR 1。日志为 `bench_a9/run_m07b.log`、`run_m0a5b.log`。场景：先把 Step1 缩到全片，再每格等稳定 / 连续各缩放 14 格，然后拖动；之后勾选全部 29 个通道再做一遍。

**结论：复现了"缩放卡住"，直接原因是 GPU 提交失败，不是细层拒绝。**
- 7 层、15 个通道：GPU 提交失败 141 次，报错为 `active raw working set 564,677,792 bytes exceeds cache budget 536,870,912`（`_TextureLru.prepare`，`step1_gpu_layer.py:333`）。
  - 提交失败时这一帧不画，屏幕停在上一帧。
  - 每格等稳定缩小的 14 格只出了 2 帧；连续放大 14 格出 3 帧，缩小 14 格出 2 帧；拖动段有 106 次失败，同时也有 55 次成功上屏的帧，且都停在第 6 层（最粗层）：冻结与粗层更新交替出现。
- 3 层、15 个通道：从缩到全片那一步就开始失败（"zoom settled in"之前已出现），共失败 20 次，所需显存 635,798,088 字节。整个 Step1 只出了 4 帧，驱动判定为"没有新帧"并中止运行。这是画面完全冻结。
- 细层拒绝：两次运行都是 0。所以 §25.2-A 推测的根因不对，至少在这两个场景里不成立。真正的根因是：
  - 粗层、目标细层、为过渡保留的替身三者相加，超过了 512 MiB 的总纹理预算；
  - 超出后既不降层也不减替身，整帧提交失败。
- 7 层、29 个通道：没有提交失败，但放大时绝大多数帧的目标层停在第 6 层，原因待查（Z1a 实施前先核实）。
- 每格滚轮的首帧（7 层）：Step0 p50 30–40 ms、p95 38–83 ms；Step1 29 个通道约 21–34 ms。
- 日志能证明总量超出，但还拆不出每次失败中粗层、目标层、替身各占多少字节；Z1a 实施时补上这项计时。
- 帧间隔（拖动）：p50 46–50 ms，p95 51–72 ms，约 20 fps，离流畅目标（≤20 ms）还很远。
- 输入事件排队：拖动 p95 ≤16 ms；滚轮 p95 约 32–79 ms。拖动定时器迟到 p95 14–23 ms。

**对 Z1a 的含义**：§25.8 的准入契约正好针对这个问题：粗层、目标层、替身去重后总字节数不超过 512 MiB；放不下就降层，并且先保证完整目标层，替身只用剩余空间。v4 的路线不变，但 Z1a 的验收必须包含：上面两个场景里 `gpu.submit_failed` 为 0，并且每格都有新帧。

### 25.10 用户补充：2 个通道也会卡（2026-10-09）——已复现

**用户描述**：卡住和通道数无关，只开 2 个通道，zoom in 到一定位置也会卡住。

**测量**：场景 `bench_a9/z2_7lv.json` 和 `z2_a5.json`；日志 `run_z27b.log`、`run_z2a5b.log`。
- 窗口最大化，查看器 1325×806。
- Step0 只显示 1 个标记加 DAPI；Step1 只保留 DAPI 和 CD8。
- 先缩到约全片，再放大 25 格到约 73 倍，然后缩回；每格等稳定一遍，连续缩放一遍。
- 驱动新增两项：记录每次用户相机写入的 `a9.camera`（scale、cx、cy）；查找查看器时优先主窗口内的控件，只缓存主窗口内找到的结果。上一轮运行曾选中 Tissue Navigator 弹窗里 350×68 的视图，并在缓存里一直沿用。

**结果**：
- 相机每一格都照常变化，从未停住。
- 7 层的 Step0 和 Step1，3 层的 Step0：每格首帧 10–60 ms；跨层时 0.2–0.3 s 后全部清晰，期间界面卡一次约 0.1 s。
- **3 层 Step1 复现了"卡住"**（`run_z2a5b.log` 动作 339）：
  - 从 scale 0.3485 缩小一格到 0.259。滞回让目标层仍停在第 0 层，此时视口每个通道需要 77 块，即 80.7 MB，超过每个通道 48 MiB 的细层预算。
  - DAPI 和 CD8 同时被 `gpu.fine_refused` 拒绝。之后画面停在：目标层覆盖 67%，其余用旧层的替身补上，这些替身来自第 1、2 层，比目标层模糊。
  - 接下来 15 秒内 0 次读盘，结算超时（target_fraction 0.9031）。
- 这就是 §25.2-A 推断的机制：选层偏细加上细层预算拒绝。它**与通道数无关**，取决于查看器大小、缩放位置和金字塔倍率。
  - 4 倍金字塔在这个窗口下，缩小时由于滞回，会在第 0 层停留到每个通道需要 77 块的位置。
  - 更大的窗口或屏幕在放大方向也会碰到。
  - 2 倍金字塔（例如真实的 qptiff）在大窗口下也会碰到。
- 与 §25.9 的问题是两回事：§25.9 是通道多时总显存 512 MiB 超限，整帧提交失败，画面完全冻结。
- Step0 没有这种拒绝，未复现卡住。

**结论**：两种卡住都由 Z1a 的准入契约（§25.7、§25.8）解决。
- 每个通道的预算和总显存预算都在准入时检查。
- 放不下就选更粗的一层作为目标并照常请求，不再拒绝后停住；只有在没有任何可行层时才明确提示。
- 选层改为最接近 1:1，单个视口需要的块数本身就有上限。

验收增加一条：本节场景，以及把窗口和屏幕放大后的同类场景，`gpu.fine_refused` 为 0，每格都能到达准入层的完整画面。

### 25.11 §25.9 的更正（2026-10-09）

依据：子代理核对日志，`gpu.frame` 中的 `channels` 字段如下：
- `run_m07b.log`：从第 5 帧起始终是 **29 个通道**。场景里的"15ch / tick all 29"标签不准确：7 层项目的 Step1 草稿本来就把 29 个通道全开着，所以那次"全部勾上 29 个"的勾选没有任何变化。
- `run_m0a5b.log`：**13 个通道**。

因此，§25.9 中"7 层、15 个通道失败 141 次"应改为 **7 层、29 个通道**；"3 层、15 个通道"应改为 **3 层、13 个通道**。

另外，7 层"29ch"各段放大时停在第 6 层，**是测量场景本身的问题，不是产品缺陷**：
- 前面的拖动每轮让相机净移 +120 px，又没有回中，之后放大时以滑片外的一点为锚点。
- 放大约 5 格后视口离开滑片，可见图块集为空。于是不规划细层、不读盘，探针也走了"无几何"的分支（`coverage_probe.py:177-180`）。

M0 之后的场景要在放大前回中：先缩到全片，再放大。

## 26. 第二步：Z1a 修复缩放卡住——实施方案（v1，待 codex 审、用户批准）

### 26.1 要解决的两个实测问题
1. **细层拒绝导致画面停住**（§25.10，与通道数无关）：
   - Step1 选的是"不超过理想下采样的最细层"（`request_planning.pick_display_level`）。在 4 倍金字塔中，每层缩放范围的末端，所需图块最多可达 1:1 时的 16 倍。
   - 实例：视口 1325×806、scale 0.259 时，仍然选第 0 层，每个通道需要 77 块、80.7 MB，超过 48 MiB，被 `gpu.fine_refused` 拒绝。
   - 拒绝后既不降层也不再请求：画面停在 67% 目标层加旧层替身，之后 0 次读盘。
2. **总纹理预算超限导致整帧失败**（§25.9、§25.11：29 / 13 个通道）：
   - 预算按通道分别计算：每个通道细层 48 MiB，保留替身的预算也按通道算。
   - 加起来超过 `_TextureLru` 的 512 MiB 总预算后，`prepare` 抛错，整帧不画，画面冻结。

### 26.2 改法（只改 Step1 / Step3，Step0 的选层和预览完全不变）

**A. 选层：最接近 1:1（参考 Odon `imaging/tiling.rs:16-33`）**
- `viewer/request_planning.py` 新增 `pick_display_level_nearest(downsamples, screen_px_per_world_px)`：取 `|ln(scale·ds)|` 最小的层；相等时取较粗的一层（数据更少）。现有的 `pick_display_level` 和 `apply_level_hysteresis` 一字不改。
- 新增 `apply_level_hysteresis_log(...)`：
  - 当前层的对数误差不超过"半个层间距 + 0.15"（自然对数单位）时，保持当前层；
  - 新层必须比当前层的对数误差小至少 0.15 才切换。
  - 这样在层边界附近不会来回跳；切换点只偏离几何中点约 ±16%。
- `viewer/explore_view.py`：控制器新增属性 `level_policy`，默认 `"nearest_below"`，即现行规则。`_pick_display_level_with_hysteresis` 按这个属性分派，属性为默认值时完全走旧代码。
- `ui/step1_viewer_host.py`：`build_step1_stack` 建好控制器后设为 `"nearest_log"`。Step1 和 Step3 共用这个工厂；Step0、比较条、montage 不经过这里。
- 效果：在 1325×806 视口下，每个通道最多约 12–20 块（4 倍金字塔中，图像像素与屏幕像素之比为 0.5–2），远低于 48 块。

**B. 准入：放得下才请求，放不下就降层并照常请求，绝不停住或失败**（`ui/step1_gpu_binding.py`，`_begin_fine_epoch`）
- 每个视口代际，都对**全部活动通道**做一次准入：
  - 粗层 `C` 按已发布和在途粗层平面的实际字节累计（每块按实际宽 × 高 × 4 字节）。
  - 从理想层 `Lt`（即 snapshot.level）开始往粗走，尝试每个候选层 `L`。用 `snapshot.bbox_l0` 和 `planning.visible_tiles_for_viewport` 重新算出该层的可见块。条件：
    - 每个通道 `F_c(L)` ≤ 48 MiB，且块数 ≤ 256；
    - `C + Σ_c F_c(L)` ≤ 总预算 512 MiB（`DEMO_GPU_RAW_TEXTURE_BYTES`）。
  - 第一个满足条件的就是**准入层 `La`**，按它请求细层。准入层之下没有更粗的细层时，只显示粗层。
  - 连粗层都放不下时，由现有的"粗层预算拒绝"规则处理，不改。
- **保留替身**只能用剩余空间：
  - 每个通道剩余 = 48 MiB − F_c(La)；
  - 全局剩余 = 512 MiB − C − Σ F_c(La)，平均分给各通道；
  - `_retain_fine` 的 `carry_budget` 取两者中较小的。
- 这样每次提交的活动集合一定放得进 512 MiB，`prepare` 不会再因为总量抛错。
- `gpu.fine_refused`（"拒绝并停住"）这条路径不再出现。准入层比理想层粗时，记一条 `gpu.fine_admitted`（含 ideal、admitted、字节数）。
- **状态与完成**：
  - 绑定新增只读属性 `ideal_level` 和 `admitted_level`。
  - 两者不同时，在现有的查看器角标位置显示"分辨率受显存限制（第 La 层）"，不新增控件。
  - 覆盖探针的 `target_index` 取 `La`：准入层全部到位，就算"准入层完成"，避免一直超时。
  - `gpu.frame` 同时记录 ideal 和 admitted，"理想层完成"单独统计。
  - 这些提示随状态变化而清除；`_last_error` 也会在准入成功时清除。
- **勾选通道 / 改变模式**：
  - 活动通道变化会触发重新准入，用的是现有的 `_begin_fine_epoch` 路径和代际取消。
  - 只改显示（颜色、窗口）不重新准入，也不读盘：现有路径本来就不进 `_begin_fine_epoch`，实施时核实。

### 26.3 不做
- 不改纹理格式（Z1b）、不改提交时钟（Z4）、不加预取（Z5）。
- 不提高任何预算。
- Step0 / 比较条 / montage 不变。

### 26.4 测试（新增或更新，均为合成数据）
- `tests/test_request_planning.py`：新函数——取 1:1 最近的层；相等时取较粗层；2 倍和 4 倍金字塔；对数滞回不来回跳。现有测试不动。
- `tests/test_step1_gpu_sources.py`：
  - `:999`、`:1220` 的"拒绝"约定改为"降层准入，照常请求并标注"（需用户批准的行为变化）；
  - 新增：
    - 总预算下 29 个通道的准入不超 512 MiB，`prepare` 不抛错；
    - 替身只用剩余空间；
    - 准入层到位即完成；
    - 勾选通道触发重新准入。
- `tests/test_a9_z1a_level_policy.py`（新）：Step1 控制器使用 `nearest_log`，Step0 的控制器仍是 `nearest_below`。
- 保留 `:458、497、574、897` 的约定（粗层拒绝、迟到结果、ROI、代际）。

### 26.5 验收
- 回归：相关测试新旧对照。
- 服务器实测（§23 的条件、窗口最大化，场景在放大前先回中）：
  - `z2_a5` / `z2_7lv`（2 个通道）和 `m0_7lv`（29 个通道）、`m0_a5`（13 个通道）：`gpu.fine_refused` = 0，`gpu.submit_failed` = 0；
  - 每格都有新帧，并在 p95 ≤150 ms 内达到准入层完整；
  - 报告理想层与准入层不同的比例。
- §13 的门槛不退步。
- 用户真机目测：放大缩小全程不再停住。

### 26.6 白名单
`viewer/request_planning.py`、`viewer/explore_view.py`（`level_policy` 分派，约 10 行）、`ui/step1_viewer_host.py`（设置策略，1–2 行）、`ui/step1_gpu_binding.py`（准入）、`ui/step1_viewer_mount.py`（角标文字）、`viewer/coverage_probe.py` 和 `docs/perf_timeline/analyze_perf_log.py`（如需报告 ideal / admitted），以及上述测试。`step0_page.py` 和 `main_window.py` 0 行。

### 26.7 codex 审查与修订（方案 v2，待用户批准）

codex 的结论是方向可行，但 v1 把"保证"说过头了，受影响的路径也算少了。以下各条已对照代码核实并采纳：

1. **总粗层可能超出预算**：
   - 现有的粗层拒绝只检查单个通道（`step1_gpu_binding.py:507-512`），多个通道合起来仍可能超过 512 MiB。例如 3 层合成数据的粗层每个通道 15.6 MiB，29 个通道约 453 MiB；真实切片每个通道约 2 MB，不会碰到。
   - 规则：准入时，先汇总所有活动通道**完整的预期粗层**，包括还在路上的块。
   - 汇总超出总预算时，**不提交超额集合**：按勾选顺序，能放下的通道照常显示；放不下的通道在现有角标处标注"显存不足，未显示：…（需 X MB / 上限 512 MB），请减少勾选通道"。
   - 这是一处需要用户批准的行为变化。它取代了现在的结果：`prepare` 抛错，整帧冻结。
2. **按身份去重求并集，并预留未到的部分**：
   - 准入时统计 `粗层 ∪ 准入目标层 ∪ 保留替身` 这个 `RawKey` 集合的字节数。准入层恰好是最粗层时，目标层与粗层重合，不重复计算。
   - 活动通道用 `_active_channels`：正权重的组、通道，以及核通道；融合模式下同一通道被多组引用，只预留一份纹理。
   - 字节数 = 高 × 宽 × 4。切片边缘的块较小；ROI 边界上的块仍是完整尺寸、外部填 NaN（`step1_source.py:676-686`），所以更正 §25.8 关于"ROI 边缘块更小"的说法。
   - LRU 里不在本帧集合中的纹理不需要预留，`prepare` 会把它们淘汰。
3. **勾选通道和粗层到达这两条路径也必须经过准入**：
   - `refresh_display` 只为新增的活动通道规划，不走 `_begin_fine_epoch`（`:237-272`）；粗层到达时，直接调 `_plan_fine_for_channel`（`:823-829`）。
   - 两条路径都改为先做准入，再规划。准入结果变化时，收缩已有通道的替身，取消不兼容的在途请求；准入结果不变时，保留不受影响的请求。
4. **替身、完成判定统一使用准入层**：
   - `_retain_fine` 无条件保留的 `target_keys` 改为准入层的块。`_visible_keys`、`_viewport_fine_ready`、发布时的 `wants_fine` 一律按准入层。
   - 准入层就是粗层时，"只有粗层"本身就是完成：探针按 coarse 目标处理。
   - 报告分两个口径："准入层完成"和"理想层完成"。后者在理想层与准入层相同时才有意义；不同时只报告"受显存限制"。
5. **对数滞回的精确定义**：
   - 设当前层为 `Lc`，理想层（最近 1:1）为 `Li`。`Li ≠ Lc` 时，只在满足下面条件时切换：
     `|ln(s·ds(Li))| + 0.15 < |ln(s·ds(Lc))|`
     也就是新层的对数误差必须比当前层小 0.15 以上。
   - 跨多层时直接切到 `Li`，相邻层间距不规则时同样适用。端点层没有更细或更粗的选择时按端点处理；下采样倍数相同的层，取编号较小的。
   - 对规则金字塔，这等价于在几何中点两侧各留 ±0.075 的保持带。
   - `snapshot.level` 是"带滞回的请求层"，报告中与严格的最近层分开命名。准入总是可以把一个放不下的请求层往粗推。

**受影响路径（明确列出）**：
- **Step1 的 CPU 回退视图**：它的合成协调器读取 `controller.level` 和 `_visible_tiles`（`step1_compose_coordinator.py:250-264`），会随新策略改为"最近 1:1"，读取量下降。纳入回归。
- **Step3 标签**：标签规划用的是 `snapshot.level`（`step3_label_binding.py:320-329`），会跟随新策略。图像准入层可能比标签层粗，要做对齐和过渡的回归。标签有自己的 256 MiB 预算，不受图像准入影响。
- 不受影响：`step1_source`、相机、预分割执行、montage（有自己的选层，`montage_view.py:429`）。
- 测试：`tests/test_step1_gpu_takeover.py:1339、1451` 中按 `controller.level` 判定完成的辅助函数需要同步更新。
- 测量：驱动按 `target_fraction` 判定稳定，这个口径把替身层也算进去了（`a9_drive.py:485`）。
  - 白名单加入 `scripts/a9_drive.py`：有 `exact_fraction` 时按它判定稳定。
  - 探针的 `target_index` 改为准入层。

**几何**：候选层的可见块用与控制器相同的取整下采样、bbox 截断和块尺寸重算，与控制器发出的请求完全一致。放置位置仍用精确的 `level_downsample_yx`，这个现有区别保持不变。测试覆盖非整数、各向异性的金字塔和切片边缘。

**补充测试**：
- 延迟到达的粗层；
- 总粗层溢出；
- 粗层与目标层去重；
- 融合模式下重复成员加核通道；
- ROI NaN 和切片边缘；
- LRU 已满，且装着不活动的纹理；
- 相机不动时勾选通道、切换模式；
- 取消勾选后恢复；
- CPU 回退合成；
- Step3 标签对齐；
- GPU 接管。

**白名单（修订）**：
- 在 §26.6 的基础上加入 `scripts/a9_drive.py`、`tests/test_step1_gpu_takeover.py`；
- Step3、CPU 回退的回归测试文件按需加入；
- `ui/step1_compose_coordinator.py`、`ui/step3_label_binding.py` **不改**，只回归。

### 26.8 用户裁定（2026-10-09）

1. §26 的第 1、2 条同意：Step1 / Step3 改为"最接近 1:1"选层；先准入后加载，放不下就降层并标注，不再拒绝停住。
2. 原第 3 条（通道太多时只显示放得下的那部分）**撤回**。改为：所有勾选通道始终显示，参照 QuPath；放不下时只降层并标注。
3. **设计基准改为真实大切片** `/sdb1/cop/Kevin/FINALRUN_updated_TMA3_Scan1.qptiff`：
   - 69 个通道，50400×31680，uint8，6 层（每层缩小 2 倍），512 像素 LZW 图块，最粗层 1575×990 按条带存储。
   - 一切设计都按这个量级做。
4. 三项新增，均已同意：
   - **原生 dtype 存储为必做**：GPU 纹理和 CPU 内存缓存都改为 uint8 / uint16，有效区另外存一张掩膜；
   - **GPU 原始纹理预算提高到约 1.5 GB**，以目标机 3060 6 GB 实测确认；
   - **以 Kevin qptiff 为主验收数据**。
5. 方案必须尽可能复用 Odon 的全部设计：继续盘点 Odon 中能提高使用体验的设计，更新方案后交 codex 审核。

**69 个通道的量级估算（Kevin TMA3）**：

| 项目 | float32 | uint8 |
|---|---|---|
| 最粗层底图（1575×990），常驻 | 约 430 MB | 约 108 MB |
| 1080p 最大化、最接近 1:1 时一屏清晰块（每通道约 15–20 块） | 约 1.4 GB | 约 350 MB |
| 4K 屏同上 | 约 6 GB | 约 1.5 GB |

另外：Step1 的 CPU 原始缓存目前是 512 MB、float32，只够约 500 块，而一屏需要约 1400 块，会反复读盘，所以这个缓存也要改为原生 dtype 并重定容量。跨层时 69 个通道要读约 1400 块，需要 Odon 的多通道请求顺序：中心优先、目标层优先、回退层限流。

## 27. 方案 v3：以 Kevin TMA3（69 通道）为基准、最大化复用 Odon 设计（2026-10-09，待 codex 审、用户批准）

依据：
- §26.8 的用户裁定；
- 两份 Odon 调研（只读，Odon `b01faef`，只参考设计，不复制代码）：数据和渲染管线、用户交互；
- 设计基准为 69 通道、50400×31680、uint8、6 层（每层缩小 2 倍）。

### 27.1 Odon 的做法：采用什么、不采用什么

**采用**（标注 Odon 出处）：
1. **选层取最接近 1:1 的一层**（`imaging/tiling.rs:16-33`）。
2. **多通道请求顺序**（`app.rs:13012-13034、13186-13311`）：目标层优先；同一层内按离中心的距离排序（`:14984-15022`）；可见通道 ≥16 个时，周边预取圈设为 0，桥接层和粗层限流。**但保留粗层底图**：Odon 在多通道时会丢掉底图，我们不学这一点。
3. **粗层常驻内存（pinned levels）**（`imaging/pinned_levels.rs:323-380`，`app.rs:15138-15151`）：每个通道的最粗层固定在 RAM 中，不进入 LRU 淘汰；读取请求先查这里。Kevin 第 5 层的 69 个通道约 108 MB，第 4 层约 430 MB。
4. **实时内存监控与风险门槛**（`app_support/memory.rs:58-107`）：用 psutil 读取可用内存，占用超过 75% 时提示，超过上限时不再扩大常驻层。
5. **每个请求都有确定的结局**：吸取 Odon 的反面教训（`app.rs:15568`、`tiff_pyramid.rs:1440`）。读取失败记入负缓存，延后重试，并在角标中计数；粗层缺块时，用"缺块标为无效"的不完整粗层先显示，不再一直等完整粗层。
6. **陈旧请求在出队时丢弃**（`tiles_raw.rs:~160`）：效果上等同于我们现有的代际取消。可选优化是改为按集合比对，避免每个视口代际都全部取消再重发。
7. **对比度种子取自粗层**（`app.rs:5972-5987`、`channel_max.rs`）：用已驻留的粗层计算。uint8 用 `bincount` 加累积和代替 `np.percentile`。用户手动设过的窗口一律不覆盖（我们已经如此）。
8. **线性插值显示**（`tiles_gl.rs:761`）：最接近 1:1 时缩小倍数 ≤ 约 2，不需要 mipmap。前提是有效区已与数值分开（见 S2c）。图块边缘用半个纹素内缩，或 1 px 光环，避免接缝。
9. **工作集公式**（`app.rs:6368-6391`）：缓存容量 ≈ 可见块数 × 活动通道数 × 每块字节数 × 1.25，用来推算 CPU 缓存和纹理预算是否够用。
10. **用户体验**（不新增可见控件的部分）：
    - **模糊搜索**：大小写和连字符都不敏感，"pdl1"能找到 PD-L1；子串匹配优先，其次子序列（`channels_panel.rs:570-610`）。
    - **键盘切换上一个 / 下一个通道**（`top_bar.rs:184-198`）：做成快捷键，不加按钮。
    - **F 键和双击：整片适配**（`app.rs:5951、12388`）。
    - **视图内的加载提示**（`canvas_overlays.rs:594-645`）：在现有角标位置显示"正在加载清晰图块 n / m"。这是新的可见文字，需要用户同意。

**不采用**（Odon 在多通道下的弱点）：
- 每个通道一张全屏离屏图：69 个通道在 1080p 下约 570 MB。
- 把 uint8 扩成 u16。
- 加法叠加：69 个通道会饱和成白色。我们用 MAX，更合适。
- 每帧不限量上传；失败后请求永远挂着。
- 在界面线程上做常驻层查找和重采样。
- 合成出的粗块存进真实图块的键里。

### 27.2 第二步拆成子块（每块独立提交、可单独回退；按顺序做；每块都过 codex 审 + 定向回归 + 服务器实测）

- **S2a 选层**：§26 和 §26.7 中的 A 部分，即最接近 1:1 的选层加对数滞回，只用于 Step1 / Step3。
- **S2b 准入**：§26 和 §26.7 中的 B 部分。
  - 按 `RawKey` 去重求并集；完整的预期粗层也要预留；勾选通道、粗层到达这两条路径也必须走准入。
  - 放不下就降层并标注，所有通道始终显示。
  - 粗层合计超出预算时：底图按通道等比缩小（只缩小底图）。只有极端情况才给出提示。
- **S2c 原生 dtype 纹理**：
  - 原始通道上传 R8 / R16 值纹理，外加一张 R8 有效掩膜；着色器里归一化后再套用同样的窗口和伽马。
  - 校正通道保持 R32F。
  - 纹理计费按实际字节数。
  - 原始纹理预算提高到约 1.5 GB（`resource_tiers`，用户已批准），在目标机 3060 6 GB 上确认。
  - 同一层级下，渲染结果与改动前逐像素对照。
- **S2d 原生 dtype 的 CPU 缓存**：
  - Step1 的 provider 先缓存原生图块，ROI 遮罩和 float 转换移到缓存之后；遮罩输出另外生成，共享数组只读。
  - 容量按 27.1-9 的公式定，并落在 RAM 档位以内。
  - 校正产物仍走原来的路径。
- **S2e 多通道请求顺序**：即 27.1-2。
- **S2f 粗层常驻与内存门槛**：即 27.1-3、27.1-4。
  - 打开 Step1 后，先画第一帧，然后在后台把粗层常驻：先可见通道，再其余通道。
  - RAM 档位是：第 5 层必做，第 4 层视可用内存而定。
- **S2g 读取失败有结局**：即 27.1-5。
- **S2h 加载提示文字**：即 27.1-10 的最后一项，需用户同意文字内容。

### 27.3 第二步之后（另行报批）
- Z3 只画可见的粗块；改用逐平面的 `glScissor`，保留着色器里的 discard。
- 中间目标改用较窄的格式（RG16F / RGBA16F）。改之前先用 GL 计时查询测出真实的 GPU 时间。
- Z4 每帧一次提交，加上上传预算。
- Z2 放大方向的阶梯，以及缩小方向保留原细层。
- 线性插值开关（27.1-8）。
- 图块极值剔除（超出 Odon 的做法）：解码时记下每块的最小、最大值，最大值不高于窗口下限时不上传、不绘制；TMA 中芯与芯之间的空白跳过。
- R1、R2：与 Step0 共享原始缓存。
- 用户体验：模糊搜索、上一个 / 下一个通道快捷键、F 键与双击适配（没有新控件，但会改变现有行为，需批准）；按"可见优先"排序、保存标记物面板、比例尺、带图例的截图（这几项是新的可见控件，需批准）。

### 27.4 验收（以 Kevin TMA3 为主）
- **准备**：在服务器上为 Kevin qptiff 建一个 Step1 项目，用产品函数完成 Step0 保存；不做校正，只用原始通道。场景在放大之前先回中。
- **场景**：
  - 窗口最大化。
  - 通道数：2 / 13 / 29 / 69。
  - 从全片放大到第 0 层再缩回：每格等稳定一遍，连续不停一遍。
  - 拖动；勾选和取消通道；融合模式。
- **门槛**：
  - `gpu.fine_refused` = 0，`gpu.submit_failed` = 0；
  - 每一格都有新帧，并在 p95 ≤150 ms 内达到准入层完整；
  - 不丢通道；
  - 拖动时帧间隔 p95 ≤20 ms、最长 ≤50 ms；
  - 内存和显存不超过档位上限；
  - §13 的门槛不退步。
- **最终**：用户真机目测，并在目标机 3060 6 GB + 16 GB 上确认。

### 27.5 codex 审查与修订（v3.1，待用户批准）

codex 的意见已逐条对照代码核实，采纳如下。

**1. 先定接口，再按依赖排序**
- S2c（纹理）依赖 S2d 提供的原生数据和有效性接口。所以先定义一个"原生值 + 有效性"的交付接口，以及一套字节计费约定，然后按下面的顺序做：
  1. **S2a** 选层；
  2. **S2b** 准入：先按现有 float32 计费；
  3. **S2d** 原生 CPU 交付和缓存；
  4. **S2c** 原生纹理：纹理格式和计费在同一次提交里一起切换；
  5. **S2e** 请求顺序；
  6. **S2f** 粗层常驻；
  7. **S2g** 失败有结局；
  8. **S2h** 加载提示文字。
- 每块报批时都给出具体的文件和行为白名单；回退时，依赖它的后续块一起回退。

**2. S2b 的缩小底图另立契约**
- 重采样：2×2 平均（或更大的整数倍）。有效性取"块内全部有效"（AND），不能把无效区平均成有效。
- 几何：世界矩形不变。
- 最小尺寸：每个通道 ≥ 256 像素。
- 预留余量：总预算的 5%。
- 完成状态："底图已缩小"如实报告。
- 合成出的底图使用**独立的显示身份**（不是某一层真实的 `RawKey`），不进入任何科学计算输入，也照样遵守现有的单通道粗层上限。

**3. 内存与掩膜**（按 GB 计，不是 GiB）
- 每块都配一张 R8 掩膜会让内存翻倍：69 个通道、一屏 15–20 块，值约 0.27–0.36 GB，加上掩膜约 0.54–0.72 GB；4K 屏连掩膜约 3 GB。
- **改为按几何判定有效性**：
  - 原始通道在 ROI 内、切片内的像素一律有效。这个范围本来就以 ROI 矩形、多边形和切片边界的形式传给了着色器（`ViewportSnapshot.roi_world_rect / roi_polygon_world`）。
  - 所以原始通道**不需要逐像素掩膜**，掩膜只在确实存在不规则无效像素时才建。
  - 校正通道保持 R32F 加 NaN，现有规则不变。
- 预算要覆盖：同时驻留的 Step1 和 Step3 两个查看器、标签纹理（256 MiB）、渲染目标。
- 按上述规则：1.5 GB 能保证 1080p 最大化时 69 个通道都在理想层；4K 屏会降层并标注，不承诺理想层。
- CPU 侧按 RAM 档位分开计：常驻层（第 5 层 0.11 GB，第 4 层 0.43 GB）、原生缓存（约 0.34–0.45 GB）、解码缓冲、上传中转。

**4. 科学有效性和强度的约定**
- 无效永远是"缺失"：不能变成有效的黑，也不能在校正 ROI 外换成原始像素。CPU 端的使用方保持 NaN 行为。
- 原生缓存和常驻层里，原始数据和校正数据、不同修订版本都必须分开。
- R8 / R16 的单位：着色器取出归一化值后，先乘回原始量程（uint8 乘 255，uint16 乘 65535），再套用现有以原始单位表示的窗口（`step1_gpu.frag:25`）。
- 逐像素对照覆盖：uint8 和 uint16、窄窗口、伽马、极值、校正与原始混排、ROI 边界。整数还原必须**逐位相同**；涉及浮点运算的环节规定容差 ≤1/255，并写进测试。

**5. 调度要和现有机制对齐**
- 现有绑定规则：粗层事务还在途时，细层排在后面；粗层完整发布后才开始细层（`step1_gpu_binding.py:64、817`）。S2e 要定义：
  - 冷启动时，先粗层，再中心的目标层；
  - 稳定后，目标层优先；
  - 通道之间轮流（公平）；
  - 后台常驻的读取有上限，并且让位于前台。
- 已排队的任务被前台请求重复请求时，现在**不会被提升优先级**（`scheduler.py:241`）。要么在白名单内补上提升，要么由绑定先取消再重发。
- 保留现有的单飞行（同一块只读一次）和代际机制；换数据源或关闭时清理干净。
- S2g：重试有上限、退避有上限，负缓存会过期并能恢复；"最终失败"和"成功完成"分开报告。

**6. 验收按块分级，并且可信**
- 分开测量四项：
  - 冷加载；
  - 热回访；
  - 首个可用画面：粗层或常驻底图，目标 ≤50 ms；
  - 准入层完整：热回访 p95 ≤150 ms。冷加载**先实测再定门槛**：69 个通道一屏约 1,000–1,400 块，150 ms 意味着每秒要处理约 7–9 千块，暂不承诺。
- 每次记录：帧缓冲尺寸和 DPR、存储介质、缓存状态（冷 / 热）、重复次数；连续运动要另测稳定所需时间。
- 统计降层的频率，以及理想层的画质，防止靠降层"轻松过关"。
- 补充合成测试：uint16、校正通道、ROI、读取失败、Step3 对齐。
- 验收项目单独建，不改动 Kevin 的科学产出。

**7. 更正 §27.1 的 Odon 描述**
- 内存监控用的是 Rust `sysinfo`，不是 psutil。判定方式是"常驻 + 将要请求的字节"与"可用内存"比较：达到 75% 警告，超过可用内存为危险（`memory.rs:58`）。我们用 psutil 实现同一规则。
- 常驻层是用户手动选的层和通道（`pinned_levels.rs:323`），读取前先查（`app.rs:15138`）。**自动常驻最粗层是我们的改进**，不是 Odon 原有的做法。
- 多通道模式下，Odon 仍保留一条"只绘制、不请求"的回退底面（`app.rs:13278`）；说它"丢掉底图"不准确。
- 线性插值在 Odon 里是可选的，默认开启。只把有效性分开还不够安全：需要按有效性加权插值，并剔除 ROI 外的部分。半纹素内缩不等于邻块光环。"缩小 ≤2 所以不需要 mipmap"没有考虑滞回和端点。线性插值留到 27.3 另行报批。
- 工作集公式和"对比度取自已驻留数据"都是我们的改编，不是 Odon 原文。百分位要写明精确定义（与 `np.percentile` 默认的线性插值一致，或明确改为最近秩），并用测试锁定。

**8. 新增机制清单**（AGENTS 第 4 条，需在各块报批时逐项批准）
- S2a：选层策略状态。
- S2b：准入状态、缩小底图。
- S2c：纹理缓存的格式和计费。
- S2d：CPU 缓存的表示方式和容量。
- S2e：调度策略。
- S2f：常驻层、后台加载、内存门槛。
- S2g：负缓存、重试、部分发布状态。
- S2h：只是可见文字。

不引入新的 registry / authority / token 框架。

**9. 最大的未解风险**：每帧的上传突发、全屏逐块渲染的开销、第 5 层按条带存储导致的重复解码。这几项先剖析，再按 27.3 另行报批，不在第二步中承诺 20 ms 帧间隔。

### 27.6 独立审核（用户转交，2026-10-09）与方案 v3.2（待用户批准）

**审核结论**：附条件批准。关键事实已对照代码核实：
- `_render_overlay` 的 RGB 用 `GL_FUNC_ADD`，只有 alpha 用 `GL_MAX`（`step1_gpu_layer.py:1003`）；融合模式是组内相加、组间取 MAX（`:1016、1023`）。
  - 所以 §27.1 里"加法叠加会饱和，我们用 MAX，更合适"一句**与实现不符，予以更正**。
  - 本轮严格保留现有的合成数学；69 个通道的亮度饱和另作体验问题处理。
- Step1 的 `raw_cache` 名字叫 raw，实际缓存的是经过来源选择、ROI 裁剪、float32 / NaN 处理之后的科学显示数据（`step1_viewer_host.py:117-164`、`scheduler.py:534-567`）。CPU 合成依靠 NaN 判断有效性（`step1_compose_coordinator.py:382-455`）。
- `_published_coarse` 已经保留了完整的粗层（`step1_gpu_binding.py:822`）。

**v3.2 的四条修订（锁定）**：
1. **S2b 不做合成的缩小底图**，只在真实的金字塔层之间降级。Kevin 的粗层原生只有约 108 MB，用不着缩小。底图缩小以后遇到真实需要时再单独申请。
2. **S2d 先交付数据与有效性的接口契约，再实施**：
   - 原生缓存只存只读、未遮罩的原始图块，键只表达真实的数据身份（数据集、平面、层、块）。
   - Step1 的来源选择（原始 / 校正）、ROI 有效性、修订版本照常生效。
   - GPU 端读取原生像素，有效性按几何判定，或另附信息。
   - **CPU 合成和 CPU 回退仍然得到与原来等价的 float32 / NaN 结果**，按消费者逐个迁移。
   - 过渡期如果两种表示并存，额外的内存如实计入，不提前宣称节省。
   - 契约另行送审，实施白名单单独报批。
3. **S2c 的数值契约与全局显存**：
   - 明确用归一化格式（R8 / R16，可以线性插值）还是整数格式（R8UI / R16UI，用整数采样器）；无论哪种，都要用独立的数值测试锁定原始强度逐位还原，再做可见像素对照，覆盖 ROI 多边形、原始与校正混排、uint16 极值、窄窗口。
   - 1.5 GB 是**单个查看器**的原始纹理上限。另加一项产品级验收：在 Step1 和 Step3 之间切换、后台 CUDA 任务还在跑时，6 GB 目标显卡不能 OOM。优先利用现有的暂停、释放、重新进入机制，实测确实不够再考虑别的办法。
4. **S2g 保持完整粗层的原子性**：只做有限次重试、失败计数和明确提示。失败的通道不能当作已完整；也不能借别的来源（例如原始像素）去填补校正产物。部分粗层发布另行论证。

**其他修订**：
- **S2e**：
  - 冷加载时，先保证所有可用通道都拿到完整粗层；
  - 稳定导航时，目标层的可见块优先，各活动通道轮流分配读盘份额，不按通道名依次处理；
  - 必须明确解决"重复请求不提升优先级"（`scheduler.py:241`）。
- **S2f**：
  - 先核实能否直接把 `_published_coarse` 或现有缓存当作常驻来源，避免存第二份全量拷贝；
  - 第 5 层优先，第 4 层按需，不在打开 Step1 时无条件预读全部通道的第 4 层；
  - 后台预读要与"调亮度 0 次读盘"的验收隔离开。
- **S2h**：复用现有角标，不增加界面元素。

**v3.2 执行顺序**：
1. **S2a + S2b-basic**：画面永远不会因预算而冻结。
   - 选层取最接近 1:1 的一层；预算不足时降到真实的更粗层。
   - 验收：`gpu.submit_failed` = 0，`gpu.fine_refused` = 0；所有来源有效的勾选通道都能显示；降层不算理想画质完成。
2. **S2d 接口契约**（送审）→ S2d 实施 → S2c。两块可以分开提交，但按同一套接口迁移来设计。
3. 用 Kevin 69 个通道复测：1080p 下的内存、拖动帧率、跨层、首次进入、ROI 正确性，以及是否还有预算失败。
4. 只有实测不达标时，才推进 S2e、S2f。
5. S2g、S2h 放在最后。

**两层验收**：
- **第一层（必须完成）**：缩放不冻结；不会因预算导致整帧失败；不丢来源有效的通道；不产生新的透明缺口。
- **第二层（交互性能）**：
  - 冷加载、热回访分开统计；热回访准入层完整 p95 ≤150 ms；拖动帧间隔 p95 ≤20 ms；
  - A9-1 到 A9-8 不变；
  - 69 个通道冷加载到完整目标层的时间先测量，不承诺数字。
- 最终在目标机（3060 6 GB + 16 GB）上确认，不能只凭 4090 服务器的结果判定达标。

**时间**：这一轮已超出 A9 原定的 8 天开发加 2 天真机验收。第一层必须完成，第二层按实测逐块推进。

## 28. 第二步：原生 dtype 迁移——S2d 接口契约（v1，待 codex 审；用户 2026-10-09 授权连续执行第 2 步，需要用户裁定的事项交 codex astra high）

### 28.1 现状（只读核实）
- 调度器 `_run_raw`（`scheduler.py:533`）调用 `provider.read_tile(channel, tile)`，结果按 `RawKey` 放进 `raw_cache`（`LRUByteCache` 按 `value.nbytes` 计费，`caches.py:36-49`）。
- Step1 的 `Step1TileProvider.read_tile`（`step1_viewer_host.py:126-143`）通过 `sources.read_tile` 做来源选择：
  - **原始通道**：用 `table.clip_to_roi` 裁成矩形，矩形内读金字塔的原生像素并转为 float32，矩形外为 NaN（`step1_source.py:671-685`）。**有效区永远是一个矩形。**
  - **校正通道**：从保存的产物缩减得到 float32；ROI 外为 NaN。
- 所有使用方（GPU 绑定、CPU 合成、CPU 回退、montage 经由 provider）都依赖 float32 + NaN。

### 28.2 契约
1. **新的只读表示 `NativeTile`**（新模块 `viewer/native_tile.py`，不可变）：
   - `values`：原始通道为 provider 的原生 dtype（uint8 / uint16），整块尺寸，有效矩形外填 0；校正通道为 float32 + NaN，与现在相同。
   - `valid_rect`：块内有效矩形 `(y0, y1, x0, x1)`；为 None 表示"整块无效"（与全 NaN 等价）。校正通道为 `"nan"`，表示有效性按 NaN 判断。
   - `kind`：`"raw"` 或 `"corrected"`。
   - `nbytes` = `values.nbytes`，供 LRU 计费。
2. **独立的缓存命名空间**：
   - `Step1TileProvider.native_source_identity()` = `SourceIdentity(stage="step1-native", corrected_artifact=<与 step1 相同的源表令牌>)`。
   - 决策、ROI、产物、修订中任何一项变化，都会让两个命名空间的键一起失效。原生块和 float 块永远不会共用一个键。
3. **读取入口**：`Step1TileProvider.read_tile_key(key)`
   - `key.source.stage == "step1-native"` 时返回 `NativeTile`；
   - 其他情况走现有的 `read_tile`，结果逐位不变。
4. **调度器**（`scheduler.py:533` 一处）：provider 有 `read_tile_key` 时按 key 读取，否则照旧。其余逻辑不变：单飞行、代际、取消、缓存都不动。
5. **原始通道原生值的来源**：`raw_provider.read_region(channel, level, clipped)` 本来就返回原生 dtype（`raw_tile_provider.py:391`，不做 float 转换），切片后放进整块尺寸的零数组。
6. **CPU 使用方一律不变**：CPU 合成、CPU 回退、montage 继续用 float32 + NaN 的 `read_tile`。只有 GPU 绑定在 S2c 中改为请求原生键。
7. **内存**：
   - GPU 模式下，原始缓存里只有原生块，uint8 占 float32 的 1/4。
   - CPU 回退只在 GPU 不可用时启用，所以正常情况下不会同时存两种表示。
   - Step3 的 GPU 查看器有自己的栈和缓存，照旧各算各的。

### 28.3 S2d 的范围
- 只做接口：`viewer/native_tile.py`（新）、`ui/step1_viewer_host.py`（新增方法）、`viewer/scheduler.py`（一处分派），以及测试。
- **不改任何使用方**，所以产品行为、像素和内存都不变。
- 测试（合成数据）：
  - uint8 / uint16：原生值转成 float 后，在有效矩形内与现有 float 块**逐位相等**；有效矩形以外正好对应 NaN 区；
  - 有效矩形和 ROI 边界（取整方式与 `clip_to_roi` 一致）；
  - 校正通道的原生块与 float 块逐位相同；
  - 两个命名空间的键互不命中；
  - 没有 `read_tile_key` 的旧 provider 照常工作；
  - 决策变化时，两边的键一起失效。

### 28.4 S2c（下一块）的预定做法
- 绑定在同一次提交里改为请求原生键，`RawPlane` 携带原生值和 `valid_rect`。
- 计费改为实际 `nbytes`，按 provider 的 dtype 估算原始通道，校正通道仍按 float32。
- 纹理格式：原始通道用 **R8UI / R16UI 整数纹理**，在着色器中按整数取值、转成 float 后套用现有的原始单位窗口，**逐位精确**；有效矩形作为每个平面的 uniform，矩形外丢弃。校正通道保持 R32F + NaN。
- 线性插值不在本轮做：整数纹理不支持线性插值，以后要做时再评估归一化格式。
- 验收：
  - 同一层级下，GPU 读回逐像素对照，覆盖 uint8 / uint16、窄窗口、伽马、校正与原始混排、ROI 矩形和多边形；
  - Kevin 69 个通道的显存。

### 28.5 codex 审查与修订（契约 v2，开始实施）

codex 的意见已逐条对照代码核实，采纳如下：
1. **`NativeTile` 必须能经过调度器**：新鲜交付和命中缓存两条路径都会读 `arr.dtype` 和 `arr.shape`（`scheduler.py:405、536、562`）。
   - `NativeTile` 提供 `dtype`、`shape`、`nbytes` 三个属性，返回约定为 `(NativeTile, io_ms)`。
   - `PixelBuffer.handle` 就是这个 `NativeTile`。
   - `values` 设为只读（`setflags(write=False)`）。
2. **两种身份分开**：`provider.source_identity()`（显示用的源身份）**保持不变**，所有现有使用方都继续用它。新增 `native_source_identity()`，只用于 GPU 绑定在 S2c 中构造的图块键。绑定内部同时保存两者：快照比对用显示身份；保留替身、到达校验用原生身份。这一部分留到 S2c 实施。
3. **不支持的 dtype 与缺失来源**：
   - 原生路径只接受 uint8 / uint16 的原始通道。其他 dtype（例如原始数据本身就是 float，里面可能有 NaN / Inf）照旧走 float32 表示，`kind="raw-float"`，有效性按有限值判断。
   - `SOURCE_MISSING` 返回整块无效（`valid_rect=None`），**绝不读原始数据来顶替**。
   - 校正通道的有效性是"有限值"（排除 NaN 和 ±Inf），与现在的绑定和着色器一致。
4. **S2c 的诊断有效性**：`RawPlane` 携带 `valid_rect`，覆盖探针按矩形判断有效区，零填充的边界不能算作有效。最终的 ROI 多边形裁剪仍在最后一遍绘制里做。
5. **禁止原生身份进入 CPU 校正或预取**：
   - `CorrectionCompute` 会继承 `key.source` 去构造原始依赖（`correction_compute.py:63`）。调度器的计算路径遇到原生身份时直接报错，不会悄悄出错。
   - `RawTileAssembler` 的直接回退路径（`assembler.py:54-80`）只接受普通身份。
6. **补充测试**：
   - 调度器冷交付和命中缓存；原生块与 float 块同时存在、各自淘汰；
   - 校正产物缺失；float 原始数据中的 NaN / ±Inf；
   - 图像边缘的块、非整数下采样的金字塔；
   - 原生身份进入计算路径时报错。
7. "GPU 模式下只缓存原生块"是 **S2c 的测量目标**，不是 S2d 的结果。保留的 float 块、montage 数组、掩膜、已发布的平面都要如实计入内存。

### 26.9 S2a + S2b 执行记录（2026-10-09）

**改动**：
- `viewer/request_planning.py`：新增 `pick_display_level_nearest` 和 `apply_level_hysteresis_log`（容差 0.15），以及策略常量。原有函数一字未改。
- `viewer/explore_view.py`：`level_policy` 分派，默认仍是 nearest_below。
- `ui/step1_viewer_host.py`：`build_step1_stack` 把 Step1 / Step3 的控制器设为 nearest_log。
- `ui/step1_gpu_binding.py`：准入。
  - `_admit` 按 (修订, 视口, 活动集合, 粗层状态) 缓存结果。还没有任何规划时，取显示中的整个活动集合；预留全部活动通道完整的预期粗层，包括还在路上的块。
  - 候选层的可见块用 `bbox_l0` 按控制器自己的公式计算。
  - 逐通道检查块数和字节上限，同时检查总预算。
  - `coarse_only` 和保留额度（单通道余量与全局余量的较小者）。
  - 三条规划路径都按准入层：相机移动、勾选通道（准入变化时，所有活动通道一起重新规划）、粗层到达。发布和就绪判断也按准入层。
  - 相同的计划保留；已部分交付的相同计划也保留。准入层已全部驻留时，取消残留的其他层请求。
  - `_fit_total_budget` 安全网：先丢替身，基础层超出时不提交。
  - 统计项 `ideal_level`、`admitted_level`、`resolution_limited`、`base_overflow_channels`，以及 `on_status_changed` 钩子。
- `ui/step1_viewer_mount.py`：总预算接入，新增两句角标文字：
  - "Resolution limited by GPU memory: showing pyramid level X instead of Y."
  - "GPU memory budget exceeded by the base layers of N channels. View not updated."
- `core/resource_tiers.py`：`GPU_RAW_TEXTURE_BYTES` 512 → 1536 MiB（受托决定，见下）。
- `scripts/a9_drive.py`：有 `exact_fraction` 时，按它判定稳定。
- 测试：
  - 新增 `tests/test_a9_s2_level_admission.py`（18 项）；
  - `test_step1_gpu_sources.py` 中两项"拒绝"约定改为"降层并标注"，用户已批准这一行为变化；
  - `test_v16_a5_resource_tiers.py`、`test_step1_gpu_takeover.py` 的预算常量随之更新。

**受托决定**（用户睡前授权，codex astra high）：基础层合计超出预算时选方案 D——提前把 1.5 GB 提高到 1536 MiB；只有连这个都超出时，才不提交、保留上一帧并显示提示。这一情况记为未解决的容量限制，不能算作验收通过。

**审查**：
- codex（low）首轮：4 条必须修。初始准入逐通道进行；候选几何与控制器不一致；部分交付的计划被重启；基础层溢出未处理（交 astra high 决定）。
- 第二轮：1 条，重新准入回到已驻留的层时，没有取消更细一层的请求。补测试时，先确认它在修复前会失败。
- 全部已修。

**回归**：
- 21 个相关文件全部通过。新旧对照中，旧代码的结果除了被有意修改的约定以外都相同。
- 真实 GPU 测试（`BLOCK01_REQUIRE_STEP1_GPU=1`，真实显示）：takeover 42 过；1 个失败在旧代码上同样失败，是既有问题（`test_a_cold_patch_still_arrives_tile_by_tile_from_its_worker`）。Step3 的 viewer 和 label 测试全部通过。

**服务器实测**（60 Hz、窗口最大化 1325×806；日志 `run_s2z2a5 / s2z27 / s2m0a5 / s2m07`）：
- 四次运行：`gpu.fine_refused` = 0，`gpu.submit_failed` = 0，`gpu.base_overflow` = 0。之前 7 层 29 个通道有 141 次提交失败，3 层 13 个通道有 20 次，并且画面冻结。
- 2 个通道（3 层）：放大、缩小各 25 格，每格都到达准入层完整。放大 p95 101 ms、最长 140 ms；缩小 p95 39 ms。§25.10 中卡住的那一格（scale 0.259）现在正常完成。
- 7 层 29 个通道：原始纹理驻留峰值从 511 MiB 降到 62 MiB（最接近 1:1 的选层需要的块少得多）；每格首帧 15–33 ms。
- 结算超时只出现在切换步骤或模式之后的那次 settle，与改动前相同，与缩放无关。
- 还未达标（留给后续块）：拖动帧间隔 p50 47–78 ms（3 层 29 个通道最差，p95 135 ms）；本轮没有测到降层（数据量在新预算以内）。

### 28.6 S2d 执行记录（2026-10-09）

**改动**（只加接口，没有任何使用方改动，产品行为、像素、内存都不变）：
- 新模块 `viewer/native_tile.py`：
  - `NativeTile` 数据类：值只读；提供 `dtype`、`shape`、`nbytes` 属性，以及 `valid_mask()` 和 `to_float()`；
  - 常量 `NATIVE_STAGE="step1-native"`、`FINITE`、各种 kind，以及 `is_native()`。
- `ui/step1_viewer_host.py`：
  - `native_source_identity()`；
  - `read_tile_key(key)`：原生键走原生读取，其余键走原 `read_tile`；
  - `read_tile_native()`：
    - 原始通道是 uint8 / uint16 时：取金字塔整数值，有效矩形与 `clip_to_roi` 一致，矩形外为 0 并视为无效；
    - 原始通道是其他 dtype 时：用 `read_tile` 的 float 结果，按有限值判断有效；
    - 校正通道：照搬 `read_tile` 的结果；
    - 缺失来源：整块无效，不读原始数据。
- `viewer/scheduler.py`：`_run_raw` 优先按 key 读取；`request()` 遇到原生身份的 `CorrectionKey` 时抛 `ValueError`，docstring 中已注明这一例外。

**审查**：codex 提出两条必须修，都已修复。
1. 缺失来源原先返回 `FINITE`，按契约应为 `valid_rect=None`；
2. 补齐测试：校正直通、原始数据中的 NaN / ±Inf、截断的边缘块、非整数下采样与不规则 ROI、两种块并存的缓存计费、level-1 的矩形值。

**测试**：新增 `tests/test_a9_s2d_native_tile.py`，25 项全部通过。新旧对照的 11 个文件结果一致：scheduler、viewer_host、gpu_sources、compose、mount、pause、explore、compare_tiles、quant_sources、pixel_source、scheduler_idle。

### 28.7 S2c 执行记录（2026-10-09）

**改动**：
- `ui/shaders/step1_gpu.frag`：新增 `PASS_SOURCE_UINT`。
  - 用 `usampler2D` 取整数，`float()` 转换后套用与 `PASS_SOURCE` 完全相同的窗口和伽马。
  - `u_valid_rect` 以外的像素丢弃；原有的平面矩形判断照旧。
- `ui/step1_gpu_layer.py`：
  - `RawPlane.valid_rect`；`INTEGER_PLANE_DTYPES`。
  - `plane_bytes` 按 dtype 计费：uint8 为 1，uint16 为 2，其余为 4。
  - uint8 / uint16 上传为 R8UI / R16UI（`GL_RED_INTEGER`、NEAREST）。
  - `_TextureRecord.integer`、`is_integer`。
  - `_render_signal` 逐平面切换程序，绘制顺序不变。
  - `_valid_world_rect` 支持各向异性，空矩形不画。
  - 校验：整数平面不接受逐像素掩膜，`valid_rect` 必须在值的范围内。
  - float 平面若带 `valid_rect`，矩形外置为 NaN。
- `viewer/coverage_probe.py`：`_valid_cells` 支持 `valid_rect`，零填充的部分不算覆盖。
- `ui/step1_gpu_binding.py`：
  - `_key_source` 取 provider 的原生命名空间（没有时就用源身份本身）。所有 `RawKey` 和替身保留都用它；快照、计划、统计仍用显示用的 `_source`。
  - `_native_pixel_bytes` 在 `source_changed` 时确定一次，`_pixel_bytes` 用于准入估算：原始通道按存储的整数大小，校正通道和原始 float 按 4 字节。
  - `_plane_bytes` 按实际 dtype 计算。
  - `_apply_result` 收到 `kind=="raw"` 的 `NativeTile` 时，生成整数 `RawPlane`（有效区为 `valid_rect`，`None` 视为空矩形）；其他情况仍生成 float 平面，有效区按有限值判断。
- 测试：
  - 新增 `tests/test_a9_s2c_integer_textures.py`（真实 GPU，12 项）：uint8 / uint16 在多种窗口和伽马下，整数路径与 float 路径的读回**逐位相同**；有效矩形；空矩形；原始与校正混排的叠加和融合；ROI 多边形；各向异性加截断的边缘块；校验与计费。
  - `test_step1_gpu_takeover.py`：源身份断言改为原生命名空间；读回的参考结果考虑 `valid_rect`（codex 建议）。

**审查**：codex 无必须修；两条建议（takeover 参考结果、各向异性和边缘块测试）已补上。

**回归**：
- 无界面：24 个相关文件全部通过。
- 真实 GPU（`BLOCK01_REQUIRE_STEP1_GPU=1`）：layer、roi_clip、roi_polygon_clip、overview_skip、step3 label render / mount / viewer 新旧一致。takeover 和 gpu_sources 各有 1 个失败，在旧代码上同样失败，是既有问题。

**确认**：用产品的读取路径对合成 OME 和 Kevin qptiff 各读一块，都是 `NativeTile kind=raw uint8`，存储 dtype 解析为 uint8，准入按 1 字节估算。

**服务器实测**（`run_s2cz2a5 / s2cz27 / s2cm0a5 / s2cm07`）：四次运行 `fine_refused` = 0、`submit_failed` = 0、`base_overflow` = 0，都没有判为无效。

**Kevin TMA3 69 通道验收**（`run_k69b.log`，项目 `bench_rm/accept/kevin_tma3`，由 `bench_a9/build_kevin_project.py` 用产品函数搭建，不做校正，原始 qptiff 只读；60 Hz，窗口最大化）：
- **功能（第一层验收）通过**：
  - 69 个通道从全片一路缩放到第 0 层再缩回，经过第 5 → 0 层的每一层；
  - `fine_refused` = 0、`submit_failed` = 0、`base_overflow` = 0，不丢通道，不冻结；
  - 1536 MiB 以内一次也没有降层；原始纹理驻留峰值约 1047 MiB。
- **2 个通道**：每格到准入层完整 p50 26–30 ms，p95 30–55 ms；拖动帧间隔 p50 47 ms。
- **69 个通道的性能（第二层）未达标**：
  - 放大时每格到准入层完整 p50 217 ms、p95 457 ms；
  - 缩小时 p50 465 ms、p95 1.5 s，最长 3.6–3.8 s（冷读取）；
  - 手势中帧间隔 250–450 ms，拖动 p50 276 ms（约 3–4 fps）。
- **主要原因**：
  - 每帧绘制的平面 p50 1239、p95 4071、最多 4899，其中替身层平面 p50 559、p95 3312；
  - 粗层没有裁剪；
  - 每个平面一遍全屏绘制。
- 这是下一块的工作：Z3 可见裁剪加 scissor、替身绘制预算、Z4 每帧一次提交，按 §27.3 另行报批。

## 29. 下一块草案：69 个通道的每帧开销（P3，v0，待 codex 审、用户批准；本文件只是方案）

**依据**：`run_k69b.log`（Kevin 69 个通道）。功能已经达标，性能是瓶颈：
- 手势中帧间隔 250–450 ms；
- 每帧平面 p50 1239、p95 4071、最多 4899，其中替身 p50 559、p95 3312；
- 每个平面都是一遍全屏绘制（`_render_signal`，`step1_gpu_layer.py`）；
- 另外，每个通道还有一遍贡献绘制和一次清屏。

**参照 Odon**（`app.rs:12954-12978`、`tiles_gl.rs:246-278`）：每帧每个通道只画三类东西：最粗层的可见块、目标层的上一层（桥接层）的可见块、目标层的可见块。并且只画与视口相交的块。

**措施**（每项独立，先测后改）：
1. **P3a 替身绘制预算**（Odon 的桥接层规则）：
   - 保留下来的旧层平面中，只**绘制**与视口相交、并且覆盖目标层尚缺区域的那些，按与目标层距离由近到远排序；
   - 每个通道最多画到目标层加一层（桥接层）为止。
   - 只改绘制，不改驻留。
   - 预计替身绘制从 p95 3312 降到每通道约十块以内。
2. **P3b 粗层可见裁剪**（原 Z3）：只绘制与视口相交的粗块。
3. **P3c 逐平面 `glScissor`**：把全屏三角形的片元着色限制在平面所在的屏幕矩形里，向外取整，着色器里的 discard 保留，像素不变。
4. **P3d（有条件）**：GL 计时查询测出真实 GPU 时间以后，再评估以下两项是否值得做：
   - 合并"清屏 + 信号 + 贡献"三遍；
   - 中间目标改用较窄的格式。

**验收**：
- 同一层级下，像素与改动前逐位相同（各项都只减少无效的绘制）；
- Kevin 69 个通道：手势中帧间隔，以及每格到准入层完整的时间，按前后对照报告；
- 2 个通道和 §13 的门槛不退步。

**白名单（预计）**：`ui/step1_gpu_layer.py`（绘制循环、scissor）、`ui/step1_gpu_binding.py`（发布时挑选替身）、测试。

### 29.1 codex 审查与修订（P3 方案 v1，待用户批准）

1. **测量本身是 69 个通道慢帧的主要来源，必须先修测量。**
   - 第 189 帧：提交 401.7 ms，其中准备 56.1 ms、渲染提交 108.6 ms、**覆盖探针 235.9 ms**；第 190 帧的探针也占了 241 ms。上传都是 0。
   - 探针的耗时随平面数线性增长（每个平面都要在采样网格上涂一遍），所以 §28.7 里"69 个通道约 3–4 fps"**被测量本身严重放大**，不能当作产品的真实数据。
   - 统计口径也要更正：全部 312 个 `channels=69` 的帧，平面数 p50 3243、p95 4761。§28.7 里的 1239 / 4071 混入了少通道的帧。
   - 第一步（属于测量，M0 范围内）：
     - 加一个开关，计时开启时可以关掉覆盖探针，只保留轻量计时；
     - 同样的手势，探针开、关各跑一遍；
     - 把准备、渲染提交、探针、上屏间隔分开报告；
     - 再用异步的 GL 计时查询（不阻塞读取）测出真实的 GPU 时间。
2. **P3a 按 v0 的写法不能保证像素相同。** 信号那一遍关闭了混合，最后画的有效片元胜出。缩小时，保留下来的更细的平面会覆盖目标层；有效矩形和 NaN 留下的洞，也不能只靠图块矩形证明被遮挡。
   - 修订：**保持现有的绘制顺序**，只去掉可以证明画不出任何像素的平面：完全在视口外的，或者被同一顺序里后面的完整有效平面完全覆盖的。
   - "只画到桥接层"会改变过渡时的画质，要单独作为体验变化报批，不放进像素不变的块。
3. **Odon 的对比更正**：
   - `app.rs:12954-12978` 是放大方向的中间层阶梯加缩小方向的保底层；多通道时对桥接层和目标层的限制是对**请求**的限制，不是普遍的"三层绘制规则"。
   - `tiles_gl.rs:246-278` 体现的是：视口外不画、关闭混合由后者覆盖、按图块大小的几何体绘制（不是全屏三角形）。
4. **范围边界**：过滤放在 layer 的绘制循环里做，传给 `cache.prepare` 的集合不变。不能在 binding 里过滤描述符，否则会改变上传、淘汰保护和 LRU 的访问记录。
5. **P3b / P3c 的实施条件**：
   - 只排除不可能落在视口内的平面；
   - scissor 在物理 FBO 像素上向外取整，正确换算到左下角为原点的 Y，并做夹取；
   - 在清屏、贡献、分组、最终合成这几遍之前关闭或恢复 scissor；
   - ROI 多边形和模板裁剪不变。
   - 验证：部分到达、缩放换向、有洞的平面、视口外的平面、小数相机位置、整数与 float 混排、叠加与融合、Step3 标签，最终 RGBA 都必须逐位相同。
6. **P3d** 另行报批：合并多遍时必须保留"先覆盖、后贡献"的顺序；较窄的中间格式不能假定逐位相同。

**修订后的顺序**：
1. P3-M：测量修正——探针开关、拆分计时、GPU 计时查询；
2. 重测 Kevin 69 个通道，看真实瓶颈在哪里；
3. 按数据做 P3b、P3c、P3a′（只去掉画不出像素的平面）；
4. 改变画质的项另行报批。

### 29.2 P3-M：关掉探针后的真实开销（2026-10-09）

**改动**（只改测量，M0 范围内）：`ui/step1_gpu_layer.py` 的 `_a9_note_submission` 在 `BLOCK01_A9_PROBE=0` 时不做覆盖探测，但仍然给帧编号并标记上屏。

**运行**：`run_k69np.log`，场景 `k69_noprobe.json`。因为没有探针就无法判断是否完整，场景只用 pause、不用 settle，所以只看帧和计时。

**结果**（按场景段的中位数和 p95，单位 ms）：

| 段 | 每帧平面数 | 准备 | 渲染提交 | 发布合计 |
|---|---|---|---|---|
| 69 个通道拖动 | 3243 / 3450 | 37 / 41 | 70 / 75 | **119 / 131** |
| 69 个通道连续放大 | 1242 / 2118 | 13 / 254 | 33 / 898 | 58 / 933 |
| 69 个通道连续缩小 | 3795 / 4899 | 52 / 87 | 97 / 565 | 168 / 633 |
| 2 个通道拖动 | 94 / 100 | 1 / 3 | 3 / 5 | 6 / 10 |

- 提交失败 0、拒绝 0。
- 关掉探针后，69 个通道拖动的真实开销约为**每帧 119 ms，也就是约 8 fps**（开着探针时是 276 ms）。
- 2 个通道每帧只要 6–12 ms。
- 帧间隔统计中，"2ch drag"的 p95 和最大值把 1.5 秒的 pause 也算了进去：现在的手势窗口会跨 pause 合并。这是分析器在这个场景下的局限，以中位数为准。
- **结论**：69 个通道的瓶颈是平面数量。准备和渲染提交都随平面数线性增长，约 33 µs 一个平面。拖动时的 3243 个平面大致由三部分组成：
  - 粗层 552 个：69 个通道 × 8 块，没有裁剪；
  - 目标层约 1380 个；
  - 替身约 1300 个。
- **P3 按以下顺序推进**：
  - P3b：粗层可见裁剪，预计 552 → 约 70；
  - P3a′：目标层已经完整的区域，不再画被它完全覆盖的替身；
  - 然后评估"按图块大小的几何体，或一次批量绘制"，也就是 Odon 那样的按块绘制，取代每个平面一遍全屏三角形。

## 30. 真机验收不通过：滚轮缩放"卡住 2–5 格"（2026-10-09，用户真人测试）

**用户反馈**：
- 在 Step1 滚动滚轮时，经常不是每一格都流畅缩放，尤其是从最小放大到最大的过程中，常常卡住 2 格，甚至 4–5 格。
- 这是严重缺陷。**最终验收标准改为"绝对流畅丝滑"**。

### 30.1 为什么之前的测试没测出来（已核实）
1. **测量驱动与产品共用同一个界面线程**。真实鼠标的滚轮事件由系统不断送来，不管程序忙不忙；我们的驱动却要等自己的定时器触发才发送下一格，而界面线程一忙，这个定时器也跟着推迟。结果卡顿表现成"两格之间的间隔被拉长"，而不是"事件在排队"。所以之前测到的排队时间都接近 0，`a9.post` 到 `a9.handled` 的统计完全看不出问题。
2. **把卡顿算到了别的地方**：每格的时延从"程序开始处理这一格"算起，排队和前一格的处理都被排除在外。
3. **用格间实际间隔重新统计已有日志**（场景本身每格间隔 40 ms，下面是多出来的等待）：
   - 2 个通道：每格多 15–25 ms；
   - 29 个通道：多 28–156 ms；
   - **69 个通道（Kevin）：p50 75–463 ms，p95 0.45–1.6 s，最长 3.5 s**，21 格里有 8–20 格晚了 100 ms 以上。
   - 这就是用户看到的卡住 2–5 格：程序忙的时候，鼠标送来的几格要等它空下来才一起处理。
4. 通道少的合成数据看起来很流畅，但真实使用的多通道切片恰恰会卡。之前的验收没有把"真实节奏的输入"和"多通道"放在一起测。

### 30.2 每格滚轮在界面线程上同步做的事（69 个通道）
- 准入：Python 循环遍历全部通道 × 候选层的图块键；
- 逐通道规划，命中缓存的结果立即应用（数组转换）；
- 发布：`prepare` 中逐块 `glTexImage2D` 同步上传。跨层时有上千块，`prepare` p95 254 ms；
- 渲染：每个平面一遍全屏绘制，3000–4900 个平面，渲染提交 p95 565–898 ms；
- 测量时还有探针。

这些都发生在处理滚轮的同一个界面线程上，滚轮事件只能排队。

### 30.3 方案 v0（参考 Odon 的帧循环，待 codex 协商）
- **F1 输入与相机彻底解耦**（Odon `app.rs:12399-12416`）：滚轮和拖动只改相机（微秒级），不在输入处理中做任何规划、上传或渲染；每个显示帧最多画一次最新的相机。连续的多格滚轮因此一格一格地流畅显示，不会排队后跳变。
- **F2 每帧工作预算**（Odon 的帧内 try_recv，再加上 Odon 自己缺少的上传预算）：规划、收结果、上传都有每帧的时间上限（例如合计 ≤6 ms），剩下的留到下一帧。画面先用已驻留的粗层或桥接层，**界面线程绝不被一次性的大量工作卡住**。
- **F3 降低每帧渲染开销**：
  - 只画视口内的平面；
  - 不画被完整目标层完全覆盖的替身；
  - 逐平面 scissor，或改为按图块大小的几何体（Odon `tiles_gl.rs:246-278`）；
  - 目标：69 个通道的渲染提交 ≤5 ms。
- **F4 准入和规划改为增量计算**：按层缓存，向量化，规模不随"通道 × 层"爆炸。
- **F5 上传路径**：纹理对象池复用（`glTexSubImage2D`）。异步上传（PBO 或共享上下文线程）作为备选，先测再定。
- **F6 测量改为与界面线程无关的真实输入**：用 X 服务器注入系统级滚轮事件（XTest），模拟人的节奏：
  - 每格 8–40 ms；
  - 一次快速连拨 5–10 格。

  记录每格从系统事件时间（`QWheelEvent.timestamp`）到包含新相机的那一帧上屏的时延，以及帧间隔和界面卡顿。

**"绝对流畅"的验收标准（建议，交用户确认）**：
- 窗口最大化 1080p；2 / 13 / 29 / 69 个通道；所有层级；冷数据和热数据；人类节奏的连续滚轮和快速连拨。
- **每一格**：从输入到显示出新相机，p99 ≤33 ms、最长 ≤50 ms。不出现"几格合在一起才动"，也就是同一帧最多吸收 1 格。
- **连续手势中帧间隔**：p99 ≤20 ms、最长 ≤33 ms。
- **界面线程卡顿**：不超过 33 ms。
- 细节清晰度可以先粗后细（Odon 的做法），但**运动本身绝不停顿**。
- 最终以用户真机目测为准，并在目标机 3060 上复验。

### 30.4 codex 审核（astra low）结论与方案 v1
**codex 认可的部分**：
- 主诊断成立：滚轮 → 视图范围变化 → 同步规划 → 发布 → 同步提交，全部在界面线程上。
- 已有日志可以复算：69 个通道每格多等 75/208 ms（中位数），p95 1.1–1.6 s；2 个通道多等约 20 ms；29 个通道多等约 63 ms。

**codex 的修正，已核实接受**：
1. **33 ms 节流在工作做完之后才开始计时**，既不限制单次工作时长，也不能保证每秒 30 帧。相机变化会直接调用 `_publish_current()`，不经过 16 ms 的门。
2. **准入已经按视口做了缓存**，所以不是最大开销。`prepare` 即使没有上传，也会扫描全部平面；254 ms 的尾部时延不能全部算在上传上。整数原始图块的应用路径本来就没有数组转换。
3. **F1 必须覆盖整条链**，包括 `ExploreController._on_range_changed`（选层、可见图块枚举、底层规划、池修剪）、快照、`_publish_current` 每次重建显示规格和排序，以及导航小图在每次范围变化时销毁再重建 `PlotDataItem`（`overview_panel.py:3043`）。只把 `update_viewport` 推迟到定时器里，卡顿只是换了个地方。
4. **F2 和 F3 必须一起做**：
   - 预算要覆盖全部细化工作，包括缓存命中、收结果、取消、扫描和上传；
   - 一次阻塞的图形调用无法被打断；
   - scissor 能减少像素填充，但不能减少几千次 Python/图形调用，所以必须先剔除平面；
   - "≤5 ms" 是实验目标，不是已经证明的上限。
5. **F4 和 F5 等实测之后再定**。不要从后台线程调用 `submit()`；共享上下文和 PBO 留作后备。
6. **F6 先做，并且要能对上号**：
   - 用独立进程按绝对时刻注入 XTest 滚轮，不等界面确认；
   - 记录这一整条链：输入时刻 → 界面收到（含 delta）→ 相机编号 → 渲染帧编号 → `paintGL` 实际画出的编号 → `frameSwapped`；
   - 时钟要校准；
   - 被覆盖、没有上屏的提交单独计数；
   - 没有变化的交换不算前进；
   - 测时延时关闭探针。
7. **验收阈值要修正**：60 Hz 的屏幕每秒只能显示 60 帧，8 ms 一格的连拨不可能每格单独占一帧。Odon 也是按帧合并滚动量。

**方案 v1，顺序按 codex 建议**：
1. **F6 真实输入测量基线**：先在服务器上复现用户的卡顿，并给出数字。
2. **F1 加上必要的 F2**：滚轮和拖动只改相机，然后请求一帧。每帧只用已驻留的资源画最新的相机。选层、准入、规划、上传、导航小图更新都移到有时间预算的帧后分片里执行。
3. **F3 降低渲染开销**：剔除视口外的平面和被完整覆盖的替身，再减少填充面积和调用次数。
4. **其余瓶颈 F2/F4**，按实测结果处理。
5. **F5**：只有在上传停顿仍然存在时才做。
6. **F6 回归测试 + 用户真机目测**。

**修正后的验收标准（建议）**：
- **间隔 ≥40 ms 的单格滚动**：每一格都单独上屏一帧，并且从输入到上屏 ≤33 ms。
- **8–16 ms 的快速连拨**：
  - 允许几格合并到同一帧，但滚动量不能丢；
  - 每格从输入到第一次体现在画面上 p99 ≤33 ms，最长 ≤50 ms；
  - 积压不能越来越多。
- **连续手势中帧间隔**（60 Hz）：p99 ≤20 ms，最长 ≤33 ms。
- **界面线程**：单次卡顿不超过 33 ms。
- **覆盖范围**：2 / 13 / 29 / 69 个通道，窗口最大化，冷数据和热数据。细节可以先粗后细，但运动不停顿。
- 最终以用户在目标机 3060 上真机目测为准。

## 31. 复现、Odon 全量排查与方案 v2（2026-10-09，用户已批准改动；用户条件：Kevin，DAPI/HER2/CK20）

### 31.1 真实输入复现
- **测量工具**：
  - `scripts/a9_xwheel.py`：独立进程，用 XTest 按绝对时刻注入系统级滚轮。
  - 驱动新动作 `xwheel`：记录每个滚轮事件被收到的时刻，并在每个上屏帧上标出它包含了几格。
  - `scripts/a9_xwheel_report.py`：逐格统计。
- **服务器结果**：Kevin，最大化，实际点亮 4 个通道（DAPI、HER2、CK20，外加默认勾选的 CD45RO），关闭探针。

  | 手势 | 每格从注入到上屏 p50 / p95 / 最长 | 超过 50 ms 的格数 | 同一帧合并的最多格数 |
  |---|---|---|---|
  | 慢速，200 ms 一格 | 34–36 / 38–40 / 42–45 ms | 0 | 1 |
  | 人手速度（25–40 ms 一格，分组） | 36–60 / 61–157 / 67–187 ms | 7–12 / 22 | 2–4 |
  | 快速连拨（12 ms 一格） | 64–77 / 97–111 / 107–123 ms | 16–18 / 22 | 6 |

  - **VGA 开和关结果相同**（另一次运行 `k3x_novga`），所以排除副显示输出的影响。
  - **"卡住 2–5 格"已复现**：同一帧里合并了 4–6 格。

### 31.2 已证实的根因（按影响排序）
1. **读图线程和界面线程争抢 Python 全局锁（GIL）**。默认有 8 个读图线程（`viewer/scheduler.py:117`）和 4 个计算线程。
   - **应用内**：跨层的那一格，`gpu.render` 从 4 ms 涨到 31–47 ms，正好与几十个 `sched.read` 同时运行。
   - **独立实验**（`scratchpad/gilbench.py`）：模拟 600 次图形调用的绘制循环，单独运行 2 ms。
     - 有 8 个读图线程时，p50 14 ms、p95 47 ms；
     - 有 4 个线程时，p95 2.6 ms；
     - 改为 8 个读图**进程**时，p95 5.6 ms。
   - **原因**：每次图形调用都会释放并重新抢 GIL，要和读图线程排队。这是 Python 特有的问题（"护航效应"）。Odon 用 Rust 写，没有 GIL，所以在它的设计里看不到。
   - **调高 GIL 切换频率**（`setswitchinterval`）只能缓解一点：p95 31 ms。
2. **新图块在界面线程上同步上传**：跨层后的第一帧要上传 32–44 块，花 17–27 ms。
3. **每一格滚轮都在界面线程上同步做完规划、上传和合成**。还有 33 ms 的节流，它在工作做完之后才开始计时。
4. **每个平面都画一遍全屏三角形**，开销是平面数 × 视口像素。
5. **滚轮一格放大 1.346 倍**（pyqtgraph 默认值，`ViewBox.py:164,1309`）。Odon 的公式是 `exp(scroll×0.0015)`（`app.rs:12405`）。按 egui 每格 40–50 点计算，约 1.06–1.08 倍，这个值是推算，egui 源码不在本地。因为一格放大这么多，大约每两格就要跨一层。
6. **泄漏**：`_descriptor_history` 每发布一次就追加一条，从不清理（`ui/step1_gpu_binding.py:1159`）。它持有图块数组，时间越长内存越大，Python 垃圾回收的暂停也越长。
7. **PyOpenGL 默认开启错误检查**：每次图形调用都额外调用一次 `glGetError`，图形调用次数翻倍，抢 GIL 的次数也翻倍。

### 31.3 Odon 全量排查（4 路 sonnet + codex 独立排查）
- **原始清单**：`scratchpad/odon_audit/{a_app1,b_app2,c_pipeline,d_ux,codex_audit}.md`，约 190 行，每行都标了 Odon 和 v16 的文件:行号。
- 下面的状态以 v16 代码为准，不以文档为准。

**已采用**：
- 以光标为中心缩放；
- 选最接近 1:1 的层，并加了对数滞回；
- 每个通道由粗到细绘制，细层覆盖粗层；
- 原始缓存的键不含显示状态，改对比度或颜色不重读；
- 读取去重；
- 每个工作线程有自己的读取句柄；
- 迟到的结果按代次丢弃；
- 原生整数纹理（比 Odon 的 u8→u16 更省）；
- 渲染目标复用；
- 自动对比度在后台计算，且不覆盖用户手动设的值；
- 结果每 16 ms 最多发布一次；
- 隐藏的直方图不刷新。

**之前漏掉、这次要采用的（按对流畅度的影响排序）**：

| 编号 | Odon 设计 | Odon 出处 | v16 现状 |
|---|---|---|---|
| O1 | 每帧一次：先收结果，再处理输入，然后用**当前相机**画一次；输入处理里不做规划、不做渲染 | `app.rs:5547-5950, 12380-12416` | 没有：每格同步规划和提交，`paintGL` 只贴旧图 |
| O2 | 一帧内的滚轮量累加起来一次性应用 | `app.rs:12401` | 部分：每个事件都触发下游全部工作 |
| O3 | 每块一个小四边形，视口外的直接剔除 | `render/tiles_gl.rs:246-278, 1064-1101` | 没有：每个平面一个全屏三角形 |
| O4 | 只在画到时才上传，每帧有上传上限（Odon 的线条渲染器每帧 64 块） | `tiles_gl.rs:212,815`；`line_bins_gl.rs:146,201` | 没有：`prepare` 一次性全部上传 |
| O5 | 桥接层：总是同时请求并绘制"最粗层 + 目标层的上一层 + 目标层" | `imaging/tiling.rs:37-52, 359-374` | 没有：只有最粗层和目标层 |
| O6 | 放大时保留中间层级（粘性上限），缩小时保留原细层 400 ms | `app.rs:12925-12980, 13090` | 部分：只沿用已驻留的，不主动请求 |
| O7 | 从视口中心向外请求；同一位置的各通道一起请求 | `app.rs:14984-15022, 15117` | 没有：GPU 路径按光栅顺序、逐通道请求 |
| O8 | 可见范围四周各多读 1 块；目标层光环预取，以及下一细层的光环预取 | `app.rs:13035, 15159` | 没有，GPU 路径里关闭了 |
| O9 | 用"仍然需要的键集合"做裁剪，还需要的请求不取消 | `render/tiles_raw.rs:55-88, 191`；`app.rs:13335` | 部分：每次换视图都整代取消，再重新排队 |
| O10 | 每帧最多发起 256 个新请求；可见通道 ≥16 时进入自适应模式 | `app.rs:13012, 13186, 13294` | 部分 |
| O11 | 显示状态是普通字段，每帧直接读，不重建 | 多处 | 部分：每个事件都重建规格和排序 |
| O12 | 被淘汰的纹理延迟删除，在绘制时统一处理 | `tiles_gl.rs:766,803` | 部分：在准备阶段同步删除 |
| O13 | 辅助工作（直方图、状态）放到导航停下后再做：300 ms 去抖、200 ms 节流 | `app.rs:127, 15400, 15036` | 导航小图在每次范围变化时重建（`overview_panel.py:3043`） |
| O14 | 滚轮细步长 `exp(scroll×0.0015)`，约 1.07 倍/格 | `app.rs:12405` | 1.346 倍/格，**属于行为改动，交用户决定** |
| O15 | 默认线性插值，可切换为最近邻 | `tiles_gl.rs:761,823`；`top_bar.rs:233` | 只有最近邻，**属于显示改动，交用户决定** |
| O16 | 体验类：缩放上下限、F 键或双击适配全图、加载中提示、比例尺、HUD、上一个/下一个单通道浏览、对可见通道批量调对比度、触控板捏合、常驻 RAM 层、可见区域直方图 | 见各清单 | 没有或部分，**交用户逐项决定** |

**不采用**：
- 多视角平面；
- 通道仿射变换；
- mosaic 专用的阶段；
- 把 u8 扩成 u16（我们的整数纹理更好）；
- Odon 的无预算抽取和静默失败。

### 31.4 方案 v2：实施块，按实测影响排序
- **块 1：GIL 争抢（实测第一根因）**
  - 先实测"读图线程 8→4，计算线程 4→2"对应用内滚轮的影响；
  - 根本方案是把读图和解码放进子进程。实验显示界面 p95 从 47 ms 降到 5.6 ms，吞吐还要优化，例如一次任务读多块、用共享内存；
  - 同时关闭 PyOpenGL 的错误检查，只在出错的路径上显式检查；
  - 修复 `_descriptor_history` 泄漏：只保留最近 N 条，或只在测试开关打开时保留。
- **块 2（O1、O2、O11、O13）**：
  - 输入只改相机，然后请求一帧；`paintGL` 用当前相机加已驻留的纹理直接绘制；
  - 规划、准入、收结果、导航小图更新都放到帧后的分片里，每帧限时。
- **块 3（O3、O4、O12）**：
  - 每块一个四边形加剔除；
  - 只上传要画的，且每帧有上传预算；
  - 纹理延迟删除。
- **块 4（O5–O10）**：
  - 桥接层和中间层级；
  - 中心优先、各通道一起请求；
  - 多读一圈并预取下一细层；
  - 用需要的键集合做裁剪；
  - 每帧请求上限。
- **块 5**：用户对 O14、O15、O16 的决定。

**每块都要经过**：codex 审方案 → 实施 → codex 审代码 → 定向回归 → 用 xwheel 复测（2 / 3 / 13 / 29 / 69 个通道）→ 本地提交。最后由用户真机验收。

### 31.5 codex 审核方案 v2（astra low）后的更正，已核实接受
- **测量工具**：
  - 逐格对应没有错位；被覆盖、没有上屏的提交处理也正确。
  - 指标含义是"注入到交换完成"，不是显示器真正扫出的时刻。
  - 报告里"空等间隔"的算法有错，已改为"有欠显示的格时，两帧之间的最长等待"。手势窗口改为以下一个手势开始为界，并写出真实标签。
  - 修正后，人手速度时这个等待最长 51–142 ms，快速连拨时 80–97 ms。
- **"探针已关"不完整**：CPU 路径的绘制探针仍在运行，每次 0.4–2.4 ms（`viewer/explore_view.py:2192`）。
- **CPU 控制器每次范围变化仍在运行**：选层、可见性、池修剪（`explore_view.py:4400`）。
- **提交本身是节流的**，不是每一格都做一遍完整合成。
- **GIL 证据有力，但还不是隔离后的排名**：
  - 模拟实验用的是 getpid 循环，"47→5.6 ms"是合成实验的结果，不代表应用内的改善；
  - 计算线程在本次运行中没有活动，所以暂不调整；
  - GC 暂停没有记录，"泄漏导致停顿"目前只是推测，没有证实。
- **块 1 改为**：先单独对比"读图线程 8→4"，同时记录变清晰的时间。子进程方案等块 2、3 做完后再评估是否需要。
- **关闭 PyOpenGL 自动检查**：
  - 必须在导入 OpenGL 子模块之前设置，影响整个进程；
  - 保留现有的显式 `_check_gl`（分配、上传、提交）；
  - 提供一个打开检查的诊断开关。
- **历史泄漏的修复**：要保留"累计发布次数"这个计数。读取它的有 `tests/test_step1_gpu_sources.py:1105`、`tests/test_step1_gpu_takeover.py:947` 和若干 benchmark 脚本。历史记录本身只保留最近 N 条，或只在测试时开启，同时更新这些读取方。
- **§31.3 清单补漏**。以下项目**不影响滚轮流畅度**，列入"体验类，待用户决定"或"不采用"：
  - 按原生存储块读取，避免重复解码。Kevin 的 0–4 层原生块就是 512，不重复，所以先不做；
  - 低带宽渲染目标格式。必须像素等价，放入块 3 评估；
  - 按需求自动调整缓存容量；
  - 按工作类型分开线程池，线程数按 CPU 自动并可调；
  - 忙时和空闲时的重绘策略、可选帧率上限；
  - 常驻内存层的可用 RAM 预估和风险提示；
  - 整数直方图百分位、可选的自动对比度方法、精细调节、模糊搜索、排序、分组、拖拽排序、RGB 预设；
  - 按住空格平移、调参面板、忙碌原因快照、扩展的视图状态保存、手动和自动选层、交互帮助、截图图例；
  - mosaic 共享线程池（不采用：不是单张切片的问题）。

## 32. 块 2+3 设计：画面跟随相机，每帧只做有限的工作（2026-10-09，待 codex 审）

### 32.1 归因实测（`BLOCK01_A9_ATTR=1`，Kevin 4 通道，真实滚轮，每格中位数 / p95）
| 环节 | 中位数 | p95 | 说明 |
|---|---|---|---|
| `ViewBox.wheelEvent` 合计 | 14.4 ms | 31.5 ms | |
| 控制器 `_on_range_changed` | 11.1 ms | 27 ms | 含同步发布 |
| 发布 `gpu.publish` | 15.0 ms | 26 ms | 其中 prepare 4.2 ms（主要是全平面扫描），render 8.1 ms |
| 导航小图矩形 `mount._on_range_changed` | 3.2 ms | 3.6 ms | |
| pyqtgraph 场景绘制 | 0.7 ms | | |
| `paintGL` | 0.1 ms | | |

- 慢速单格时，一帧有 276 个平面，其中 220 个是之前更细层留下的替身。prepare 6.5 ms，render 12 ms。没有任何读取时也要约 40 ms。
- 读图线程 8→4 的对比几乎没有差别：p95 都约 100 ms，见 `k3x_io4b` 和 `k3x_io8`。所以降线程数不能作为主修复。

### 32.2 设计（参考 Odon O1–O4、O11–O13）
**层（`ui/step1_gpu_layer.py`）**：
1. **画面跟随相机**：
   - 层已经监听 `sigRangeChanged`（`_attached_range_changed`）。改为记录新的视图矩形，然后 `update()`。
   - `paintGL` 用最后一次提交的场景（通道平面、显示、视口），把视图矩形换成当前相机，重新合成后再贴图。
   - 合成只用已驻留的纹理，绝不上传；缺纹理的平面直接跳过，由更粗的层顶上。
   - 有标注层时也按新视口重画。
   - 测量用的帧序号在这次重合成时记录。
2. **每块一个四边形**：顶点着色器按 `u_plane_rect` 与视图的交集生成两个三角形。片元代码不变，仍由 `v_screen_uv` 换算世界坐标，所以像素结果不变，用像素对比测试锁定。全屏 pass（贡献、合并、最终）保留原来的大三角形。
3. **剔除**：与视图不相交的平面不画。
4. **上传预算**：`prepare` 每次调用最多上传 N 块或 M 毫秒，先上传粗的；放不下的平面在本帧跳过，并返回"有待上传"，让绑定在下一帧继续。测试和首帧可以传入不限额。
5. **关闭 PyOpenGL 自动错误检查**：
   - 在 `gpu_warmup` 和层里，导入 `OpenGL.GL` 之前设置 `OpenGL.ERROR_CHECKING=False`；
   - 诊断开关 `BLOCK01_GL_CHECK=1` 可以恢复检查；
   - 保留现有的显式 `_check_gl`。

**绑定（`ui/step1_gpu_binding.py`）**：

6. **滚轮和拖动不再同步规划和发布**：`_interaction_event` 只记下最新的快照，并启动一个合并计时器。计时器到点后，在下一轮事件循环里执行 `update_viewport` 和发布，最多每 16 ms 一次。画面本身已经由第 1 条跟随相机，不用等规划。
7. **发布只交出"场景 + 有预算的上传"**。有待上传时，16 ms 后再发布一次。
8. **修复泄漏**：`_descriptor_history` 只保留最近 8 条，另加 `_publication_count` 累计计数；`stats()["descriptor_publications"]` 改读计数，并更新读取它的测试和脚本。

**挂载（`ui/step1_viewer_mount.py`）**：

9. **导航小图矩形改为节流发布**，每 50 ms 最多一次，手势结束后补发最后一次（Odon O13）。

**不在本块**：桥接层、请求顺序、多读一圈、键集合裁剪（这些在块 4）；控制器 `explore_view` 的逐格计算（共享代码，先实测它独立的开销）。

### 32.3 验证
- **定向测试**：步骤 1 的 GPU 相关测试，加新增的四边形与全屏像素对比、剔除、上传预算、相机重合成、泄漏计数测试。
- **测量**：用 xwheel 复测 3 / 13 / 29 / 69 个通道。目标：
  - 每格从注入到上屏 p99 ≤33 ms；
  - 有欠显示的格时，等待最长 ≤50 ms；
  - 同一帧最多合并 3 格（12 ms 一格的连拨时）。

### 32.4 实施结果（未提交，等用户真机验收）
**改动**：
- `ui/step1_gpu_layer.py`、两个着色器、`ui/step1_gpu_binding.py`、`ui/gpu_warmup.py`；
- `viewer/raw_tile_provider.py`（直接解码文件自己的图块，Odon 的做法）；
- `utils/gc_pacer.py`（新增）和 `main.py`（加 4 行）；
- `ui/step0/overview_panel.py`：导航框改为原地移动，不再销毁重建；
- 测量用：`viewer/scheduler.py` 的环境变量开关，`scripts/a9_drive.py` / `a9_xwheel*.py`；
- 测试：新增 3 个文件，另改了 2 个测试的等待条件（"已发布"改为"已上显卡"）和 1 个测试替身的签名。
- `step0_page.py`、`main_window.py`：0 行。

**设计之外、实测后追加的根因**：
1. PyOpenGL 每次调用都释放并重新抢 GIL，界面线程要排在读图线程后面。改为让 GL 调用不释放 GIL（`PYFUNCTYPE`）。
2. zarr 读一个图块要 11 ms，期间都持有 GIL。改为 `pread` + imagecodecs，只要 1.8 ms，结果与 zarr 逐像素一致；所有层级的读表在线程预热时就建好。
3. Python 的全量垃圾回收会暂停 80–250 ms。改为只在输入空闲时回收，回收后把存活对象冻结；另有兜底的彻底回收。
4. 用户正在操作时，上传给输入让路：两次发布至少间隔 33 ms，每次最多上传 1.5 ms。

**codex 代码审核提出的 5 条必修**（已全部修复，并补了测试）：
1. 打包存储的样本（例如 12 位）不能走直接读取；
2. 替身只有在替代它的图块真正上了显卡后才不画，而且要完全覆盖；
3. 窗口尺寸或像素比变化时要重新合成；
4. 末尾一次规划之后要重新打开节流窗口；
5. 回收器要有兜底的彻底回收。

**定向回归**：
- 66 个相关模块，再加 27 个导航小图相关模块。
- 失败的都是旧代码本来就失败的：`cold_patch`、`centre_first`、`viewport_sync`、`channel_panel`、`artifact_graph` 的 6 个错误。
- 另有一个 `overview_middle_drag_pan` 只在并行时偶发，单独重跑 3/3 通过。

**xwheel 实测**（Kevin，最大化，系统级真实滚轮和拖动；测试前先取消其余通道，因为项目会恢复上次勾选的通道）：
- **3 个通道**：每格从注入到上屏 p50 19.6 ms，p99 43 ms，最长 53 ms。
  - 冷数据快速放大，最长 40 ms；慢速 14 ms；12 ms 一格的快速连拨最长 50 ms；拖动 p95 39 ms，最长 53 ms。
- **13 / 29 / 69 个通道**（`kx_r1`，当时的版本）：p99 分别为 84 / 149 / 301 ms。
  - 每帧要逐块画 1000–5000 个平面，每块约 12 µs 的 Python 开销；
  - 规划约 88 ms（29 个通道），prepare 扫描约 19 ms。
  - 这需要下一块来解决：纹理数组 + 实例化绘制（每个通道每一层一次调用），以及规划改为 Odon 的每帧请求配额加"需要的集合"。

## 33. 下一块方案：多通道流畅 + 边缘机内存有界（2026-10-09，待 codex astra high 审核、用户批准）

### 33.0 基准与目标
- **基准**：`78aa746`，标签 `v16-a9-smooth-baseline`。用户已真机验收：Kevin 3 个通道流畅。
- **流畅目标**（xwheel 实测，规则同 §32）：2 / 13 / 29 / 69 个通道都满足"每格从注入到上屏 p99 ≤ 50 ms、最长 ≤ 70 ms，正常速度时不出现多格合并"。最终以用户在 3060 上真机目测为准。
- **当前差距**：13 / 29 / 69 个通道时 p99 分别为 84 / 149 / 301 ms。
- **内存目标**：边缘档（`core/resource_tiers.EDGE`：16 GB 内存、6 GB 显存）下，69 个通道场景的显存峰值 ≤ 5 GB、进程内存峰值 ≤ 12 GB，用 `nvidia-smi` 和 RSS 实测。

### 33.1 实测根因（29 个通道，`kx29_at`，带归因开销）
- **逐平面绘制太多**：每帧 1000–2000 个平面，`gpu.render` p50 26 ms；每个平面约 12 µs 的 Python 开销（绑定纹理、设 uniform、绘制）。
- **`prepare` 每次扫描全部平面**：p50 19 ms。
- **`update_viewport` 规划**：p50 88 ms，逐通道逐块地生成请求和键对象。
- **结果交付**：29 个通道一轮下来约 1.6 万次 `gpu.accept`，单次很便宜。
- 显存峰值：69 个通道 728 MB，29 个通道 414 MB，3 个通道 145 MB。

### 33.2 方案
**M1 纹理数组 + 实例化绘制**（Odon 每块一个四边形，在 Python 里成批提交）：
- 每种像素格式（R8UI / R16UI / R32F）各用若干 `GL_TEXTURE_2D_ARRAY` 块，每层放一个 512×512 图块，每块 64 层。
  - 用 `glTexStorage3D` 按需分配；不足 512 的边缘图块只占层内的一个子矩形。
  - 空槽用 LRU 管理，循环复用，不删纹理。
- 总字节仍受 `GPU_RAW_TEXTURE_BYTES` 约束，按实际分配的层计数。
- **绘制**：每个通道按"层级由粗到细 × 数组块"分组，每组调用一次 `glDrawArraysInstanced`。
  - 每个实例的数据：平面世界矩形、有效矩形、uv 缩放、层号，用一个 numpy 缓冲一次上传。
  - 同一层级的平面互不重叠，所以组内顺序无关；层级之间由粗到细，保持"细层覆盖粗层"的语义。
- 片元逻辑与现在逐像素一致，用对比测试锁定：新旧两种绘制路径必须位级相同。
- 大于 512 或其他形状的平面（测试、montage）继续走原来的单纹理路径。
- **目标**：69 个通道每帧绘制调用约 69×3 次，render ≤ 5 ms。

**M2 增量 `prepare`**：
- 已驻留的平面只做一次字典查找，不再比较几何、不再验证。
- 只处理新增的平面；上传预算和"粗层必须完整"的规则不变。

**M3 规划改为 Odon 的"需要的集合 + 每帧请求配额"**（`app.rs:13186、15103、13335`）：
- 每次规划先计算所有通道需要的键集合，再与在途和已驻留的集合求差。
- 只为新增的部分发请求，每次最多发 N 个（Odon 是 256），按从视口中心向外、同一位置所有通道一起的顺序。
- 不再需要的在途请求按键取消，不再整代取消再重排。
- 缓存命中的结果按每帧预算分批应用。
- 先用 py-spy 的低采样率确认 88 ms 的分布，再定实现细节。

**M4 显存按机器分档，隐藏时释放**：
- `GPU_RAW_TEXTURE_BYTES` 改为按机器决定：`min(1.5 GB, 可用显存 × 25%)`。可用显存在启动时用 GL（NVX_gpu_memory_info）读取，读不到时按 EDGE 档的 6 GB 计。可以用环境变量覆盖。
- **查看器被隐藏（pause）或开始分割时**：释放细层纹理，只保留粗层；恢复时按需重新上传。这里需要用户确认：恢复后要重新变清晰，会比现在慢一些。

**M5 内存（RAM）有界**：
- **Step0 和 Step1 共用一个原生 dtype 的原始图块缓存**（Odon"缓存是唯一来源"），键不含显示状态。
  - 修正（codex 审核）：Step0 读的是 `RawTileProvider`，与 Step1 的 `step1-native` 是不同的命名空间，所以共享的范围只能是原始源读出的整数图块，校正图块各自保留。
- **各 CPU 缓存的上限按档决定**，EDGE 档下合计 ≤ 4 GB。
- 加一个启动时的内存账：各缓存上限之和、以及实测的峰值，写进性能日志。

**M6 边缘机验收**：在服务器上用 EDGE 档的上限运行 69 个通道的 xwheel 场景，记录 `nvidia-smi` 显存峰值、进程 RSS 峰值和流畅指标，三项都必须达标。

### 33.3 顺序和工作量（估计）
| 步骤 | 内容 | 工作量 |
|---|---|---|
| 1 | M1 + M2（绘制和准备） | 约 1 天 |
| 2 | M3（规划） | 约 1 天 |
| 3 | M4 + M5（内存） | 约 1 天 |
| 4 | M6 验收 + 真机 | 半天 |

每一步都走"codex 审代码 → 定向回归 → xwheel 测 3 / 13 / 29 / 69 个通道 → 本地提交"。推送需要用户授权。

### 33.4 需要用户决定
1. M4 中"隐藏时释放细层"带来的恢复时间代价。
2. M5 中 EDGE 档的 CPU 缓存总上限（建议 4 GB）。
3. 之前留下的三项：滚轮步长、线性插值、体验类功能，不在本块之内。

### 33.5 codex astra high 审核结论（2026-10-09）
**结论**：方向正确，但现在的方案还证明不了"边缘机内存有界"和"69 个通道流畅"。原文见 `scratchpad/codex_high33.md`。

**要点**：
1. **测量更正**：`kx29_at` 实际是 69 个通道（项目恢复了上次的勾选）。88 ms 里有 57 ms 是其中嵌套的发布，规划本身约 30 ms。显存的 728 MB 只是图块数据本身，不是整个程序的显存。
2. **纹理数组可行，但有附加条件**：
   - `glTexStorage3D` 要 GL 4.2，需要回退到 `glTexImage3D`；
   - 数组层数上限要在运行时查询，规范只保证 256 层；
   - 显存按整块计：64 层一块，u8 16 MiB、u16 32 MiB、f32 64 MiB；
   - 碎片会削弱合批效果，"每通道每层一次调用"只是目标，不是结论。
3. **正确性合同**：边缘图块和有效矩形、NaN、跨数组块以及新旧路径之间由粗到细的顺序、Fusion 模式的组语义、ROI、标签、槽位复用的保护、GL 状态的隔离。同一块 GPU 上新旧输出必须字节相同。
4. **规划**：保留"需要的集合"，但除了"发多少请求"，还要限制"在途请求"和"已到达未应用的结果"。调度器需要一个按键取消的最小接口。准入、候选键、保留、`_drawn_fine`、预算检查都要改为增量计算。
5. **完整的内存账**：
   - 渲染目标：1080p 约 137–152 MiB，4K 约 546–609 MiB；
   - Step1 和 Step3 两个查看器同时保留时，各自有一套；
   - montage 有自己的 512 MiB；
   - 进程内有 CuPy；Mesmer 等分割大多在子进程里，TF 没有硬上限；
   - CPU 缓存上限合计 7.41 GiB；
   - 上限之外还有：历史记录 64 条、上传计时列表无限增长、调度器的过期代次记录、在途或排队中的结果、浮点有效掩码等。
6. **压力策略**：
   - 空槽不等于释放了显存，必须整块删除才释放；
   - 现在 `pause` 之后迟到的结果还会写回，必须阻止；
   - NVX 读到的只是近似的空闲显存，读不到时不能直接当作 6 GB；
   - 共享缓存只能共享未校正的源图块，`step1-native` 的语义（ROI 裁剪）必须保留。
7. **验收要分开测**：
   - 流畅：从输入到上屏、是否多格合并；
   - 清晰：多久到达准入的层级，以及多久到达理想的层级；
   - 内存：整个进程树的内存和整卡显存。
   - 4K 下 69 个通道的目标层，u8 约 0.93 GiB，u16 约 1.86 GiB，f32 约 3.7 GiB。放不下时会降一层，这是清晰度上的取舍。
   - 在服务器上用 EDGE 的上限测试，只能证明分配策略对，最终必须在真实的边缘硬件上测。

**修订后的顺序**：
| 步骤 | 内容 |
|---|---|
| M0 | 更正基线：重置勾选、分段独占计时、内存账、旧路径的像素参照 |
| M1 | 纹理数组原型 |
| M2 | 增量 prepare |
| M3 | 增量规划，在途量有界 |
| M4 + M5a | 生命周期和硬上限，覆盖所有查看器实例、渲染目标、标签、montage、CPU 端的保留 |
| M5b | 共享源缓存：只有实测到重复占用时才做 |
| M6 | 服务器上限测试，加真实 3060 / 边缘硬件测试 |

3.5 天的估计不成立。

**需要用户决定**：
1. 12 GiB / 5 GiB 是只算查看，还是包括分割子进程？
2. 支持的分辨率和缩放比、像素格式、边缘硬件的类型；
3. 是否允许降低清晰度，以及清晰度恢复的时间目标；
4. 隐藏时是否释放显存，以及可以接受的恢复延迟；
5. CPU 缓存总额（4 GiB 只是起点）以及跨模块改动的授权。

### 33.6 用户裁决（2026-10-09）
1. "内存 ≤ 12 GiB、显存 ≤ 5 GiB"只约束图像浏览。分割可以用得更多，但整机不得超过 16 GB 内存和 6 GB 显存。
2. **按 4K 设计、按 8 位数据设计**（PhenoCycler Fusion 默认输出 8 位）。
   - 边缘设备是游戏笔记本：16 GB 内存、RTX 3060 6 GB、i7-12700H，数据放在外接 1–4 TB SSD 上（雷电或 USB 3.0）。
   - **验收以笔记本内置的 500 GB SSD 为准。**
3. "降一级清晰度"的含义已向用户解释，等用户决定。
4. 离开 Step1 时允许释放显存。重新进入时分块加载，当前区域和当前通道优先，用户希望 1 秒以内。
5. 同意 CPU 缓存总额 4 GiB 作为起点；授权跨模块改动（Step0 / Step3 查看器、montage）。

## 34. M0 更正后的基线（2026-10-09，只测量）
**测量条件**：
- 场景 `kx{3,13,29,69}.json`：先取消其余所有通道，再只勾选 N 个，然后用 xwheel 做快速放大和缩小、慢速、快速连拨、拖动。
- 服务器屏幕 1920×1080，缩放比 1，窗口最大化。

**新增的测量工具**：
- `scripts/a9_memwatch.py`：每 0.5 秒采样一次，记录整个进程树的 RSS、`nvidia-smi` 报告的本进程显存和整卡显存。
- `scripts/a9_exclusive.py`：按 `t_begin` 嵌套关系计算界面线程上每个环节的独占时间。
- 驱动修正：真实输入会落到屏幕上最顶层的窗口；Tissue Navigator 弹窗有时盖住视图中心，导致滚轮进了弹窗的列表。现在改为选离中心最近、且没有被其他窗口盖住的点。

| 通道数 | 每格上屏 p50 / p99 / 最长 (ms) | 界面线程忙碌占比 | render | upload（prepare） | update_viewport | 显存（本进程）峰值 | RSS 峰值 |
|---|---|---|---|---|---|---|---|
| 3 | 20.9 / 76.9 / 92.9 | 7 % | 2.3 | 1.2 | 2.7 | 548 MiB | 6.70 GiB |
| 13 | 31.0 / 84.7 / 96.7 | 16 % | 6.8 | 4.5 | 8.4 | 702 MiB | 6.53 GiB |
| 29 | 54.5 / 162.8 / 170.8 | 24 % | 12.3 | 9.4 | 15.2 | 944 MiB | 6.46 GiB |
| 69 | 118 / 425 / 500 | 41 % | 25.3 | 16.2 | 27.7（p95 92） | 1177 MiB | 6.47 GiB |

（render、upload、update_viewport 三列是独占时间的 p50，单位 ms。）带归因的运行本身有开销；3 个通道不带归因时 p99 约 43 ms（§32.4）。

**观察**：
1. **随通道数线性增长**：render 和 upload 约 0.37 ms / 通道 / 帧，规划约 0.4 ms / 通道，每次发布还要做几万次 `_visible_drawn` 和 `_drawn_fine` 调用。M1 和 M2 针对 render 和 upload，M3 针对规划。
2. **upload 远超 4 ms 预算**（69 个通道 p50 16 ms）：预算没有覆盖强制上传的粗层、验证和扫描（codex 已指出）。
3. **读图**：69 个通道一共 5270 次读取，p50 26 ms、p95 160 ms，8 个线程都在等 I/O。冷数据下多久变清晰，要单独测。
4. **内存**：
   - RSS 峰值约 6.5 GiB，出现在打开项目、Step0 加载时（约 2 s 内从 1 GiB 升到 6.7 GiB，然后回落到 3.0 GiB），与通道数无关。进入 Step1 后稳定在约 3.6 GiB。
   - 这个打开时的瞬时峰值是边缘机内存的第一风险，要在 M4/M5 里查清来源。
   - 显存（本进程）在 Step0 就有 388 MiB；69 个通道、1080p 下峰值 1.18 GiB；整卡最多 2.0 GiB（其中包括桌面）。

## 35. M1 + M2 详细设计：纹理数组 + 实例化绘制，增量 prepare（待 codex 审）

### 35.1 存储（`ui/step1_gpu_layer.py`，新增 `_TileArrayStore`，与现有 `_TextureLru` 并存）
- **适用范围**：二维、`shape ≤ (512, 512)` 的平面，格式为 u8→R8UI、u16→R16UI、其余→R32F（R32F 仍按现在的方式处理 NaN 和有效掩码）。不适用的平面（更大的、测试或 montage 的特殊尺寸）继续走 `_TextureLru`，行为不变。
- **块**：一个 `GL_TEXTURE_2D_ARRAY`，层数 L = min(256, 运行时查询的 `GL_MAX_ARRAY_TEXTURE_LAYERS`)，每层 512×512。
  - 用 `glTexStorage3D` 分配（GL ≥ 4.2 或有 `ARB_texture_storage` 时），否则用 `glTexImage3D(..., None)`。
  - 不生成 mipmap；NEAREST 采样；CLAMP_TO_EDGE；`UNPACK_ALIGNMENT` 设为 1。
- **记账**：整块按全部层计入，u8 一块 64 MiB、u16 128 MiB、f32 256 MiB。`已分配块的字节 + _TextureLru 的字节 ≤ max_raw_texture_bytes`，是同一个预算。
  - 与 binding 准入的关系：准入按实际字节计算，整块分配最多多占一块的余量，因此预算检查以"实际分配"为准；放不下时按现有规则拒绝，由准入改用更粗一层。
- **槽位**：身份 → (块, 层)。
  - 按格式维护空槽表；按通道"亲和"分配：优先放进该通道已经用过的块，减少碎片。通道名由 prepare 随平面一起传入。
  - 没有空槽时：如果预算允许就新建一块；否则淘汰最久未用、且不在本次所需集合里的身份，复用它的槽。
  - 所需集合里的平面（正在显示的场景）永远不被淘汰。
- **上传**：`glTexSubImage3D` 写入层的左上角 w×h 子矩形。
  - 层内其余部分可能留着旧内容，但不会被采样：uv 映射在 [0, w/512) × [0, h/512) 之内，片元按平面世界矩形的半开区间判断。
- **重建上下文**：块和槽位一起失效，所有身份视为不驻留。

### 35.2 绘制
- **新程序 `array_uint` / `array_float`**：
  - 顶点着色器读取每实例属性（divisor 1）：平面矩形 vec4、有效世界矩形 vec4、uv 缩放 vec2、层号（`glVertexAttribIPointer` 整数）。
  - 在着色器里算出与 `_plane_quad_ndc` 相同的保守四边形（外扩 1 像素并截断），用 `flat` 变量传给片元。
- **片元**：逻辑与现在的 `PASS_SOURCE` / `PASS_SOURCE_UINT` 逐行一致：用 `gl_FragCoord` 换算世界坐标、半开区间、有效矩形、NaN 和 Inf 剔除、窗口和 gamma；只是把 `texture(u_raw, uv)` 换成 `texture(u_arr, vec3(uv*scale, layer))`。
- **每个通道的顺序**（保持"由粗到细、细层覆盖粗层"）：
  - 把 `selected_planes()` 按给定顺序切成连续的段，相邻平面"每像素世界尺寸相同"且同属数组存储的归入同一段。
  - 只有身份都是 RawKey（来自 binding 的同一网格、同层互不重叠）时，段内才允许按块重新排序。
  - 每段按块各调用一次 `glDrawArraysInstanced(GL_TRIANGLES, 0, 6, n)`；走 `_TextureLru` 的平面仍逐个绘制，并按原顺序穿插在各段之间。
- **每实例数据**：每个身份缓存一行 11 个 float32（槽位变化时作废），每次调用 `glBufferData` 一次；用独立的 VAO 和 VBO，画完解绑，不影响全屏 pass 和标签。
- 贡献、合并、最终 pass，以及 ROI、标签的逻辑一律不动。

### 35.3 M2 增量 prepare
- 已驻留的身份只做一次字典查找（世界矩形在进入时已校验，身份决定几何）。新身份才做完整校验。
- **上传预算覆盖全部上传**：本帧的强制粗层也计入。超出时，粗层优先、其余顺延。
- 粗层完整性的保护：一个通道只要还有粗层平面没有驻留，就只画它已经驻留的部分，并且下一帧继续上传；通道"已显示"的语义不变。
- 修正 codex 指出的两处：`upload_submit_ms` 改为只保留最近 256 条；`stats()` 不再复制整个列表。

### 35.4 验证
- **像素**：在同一块 GPU 上，新路径与旧路径（每个平面一个纹理）逐字节相同。覆盖：
  - u8 / u16 / f32 混合，NaN，边缘的不满 512 图块，`valid_rect`；
  - 数组路径与旧路径混合，跨块；
  - Overlay 和 Fusion（组、权重、核通道），ROI 矩形和多边形，标签；
  - 小数相机位置；槽位复用之后（淘汰、再上传）。
- **xwheel**：3 个通道不能退步（p99 ≤ 50 ms），并测 13 / 29 / 69 个通道；同时记录显存（`memwatch`）。
- 相关模块做旧代码与新代码的定向回归。

### 35.5 M1 第一版的实测结果，以及设计升级（2026-10-09）
**结果**：纹理数组加实例化绘制已经实现，像素与旧路径逐字节相同（`tests/test_a9_s35_arrays.py`，17 项）。绘制调用大幅减少：13 个通道从约 200 次降到 27 次。但帧时间几乎没变（xwheel `m1_*`）：

| 通道数 | 每格上屏 p99 | render p50 |
|---|---|---|
| 3 | 58 ms | 2.3 ms |
| 13 | 89 ms | 6.8 ms |
| 29 | 151 ms | 13.4 ms |
| 69 | 409 ms | 23.8 ms |

**微基准**（`scratchpad/renderbench.py`，4090）：

| 场景 | CPU 发出命令 | 加上 GPU 执行 |
|---|---|---|
| 69 个通道，1080p | 15 ms | 17 ms |
| 69 个通道，4K | 22 ms | 49 ms |

**根因**：
- 每个通道都要做"清空自己的信号缓冲 → 画图块 → 全屏叠加到总和"，即 3 次 RGBA32F 全屏操作，再加上每个通道约 0.22 ms 的 Python/PyOpenGL 固定开销。
- 4K 下这是 GPU 带宽瓶颈。3060 笔记本的带宽约为 4090 的 1/3，估计要 150 ms，这个结构做不到"4K × 69 个通道"。

**原型**（`scratchpad/vtbench.py`）：一次全屏 pass 内循环所有通道，经由每个通道的索引表从 u8 图块数组取值并合成。

| 场景 | 每帧耗时（4090） |
|---|---|
| 69 个通道，1080p | 0.39 ms |
| 29 个通道，4K | 0.65 ms |
| 3 个通道，4K | 0.08 ms |

3060 估计慢 4–5 倍，69 个通道 4K 约 7 ms。

### 35.6 M1b：虚拟纹理 + 单 pass 合成（待 codex 审）
1. **存储**：沿用 §35.1 的 `_TileArrayStore`（u8 / u16 / f32 数组块）。
   - 每个 (格式, 块) 是一个 sampler。着色器能同时绑定的 sampler 个数有限：3.3 core 保证 16 个，实际查询 `GL_MAX_TEXTURE_IMAGE_UNITS`。因此块要少而大：u8 每块最多 2048 层，Kevin 69 个通道 4K 约需 3.8k 个图块，即 2 块。
2. **索引表**：每个金字塔层级一张 `R32I` 2D 数组纹理，宽高等于该层的图块网格（Kevin 第 0 层 62×99），层数等于通道槽位数。
   - 每个格子的值是 (块号 × 4096 + 层号)，-1 表示不驻留。
   - 只在图块上传或淘汰时用 `glTexSubImage3D` 更新单个格子，不按帧重建。
   - 每个图块的世界矩形、有效矩形和宽高存在一张 RGBA32F 的"图块元数据"纹理里（按块和层寻址），与旧路径的 uniform 是同样的 float32 数值，所以逐像素判断的算术可以完全一致。
3. **每通道参数纹理**：窗口（lo、hi、gamma）、颜色乘以权重、组号、组权重、是否核通道、Overlay 还是 Fusion、该通道参与绘制的层级顺序（每个通道最多 4 个层级）。
4. **单 pass 片元**：对每个像素，按通道顺序循环。每个通道按层级顺序（目标层 → 更细的替身 → 更粗的桥接层和粗层）找到第一个有效样本（在平面矩形和有效矩形之内、不是 NaN），算出信号值。
   - **Overlay**：`acc.rgb += (s × w) × color`，按通道顺序做 float32 加法；只要有任意一个有效样本，alpha 就是 1。
   - **Fusion**：先算每组的 Σ(s × w)，再 clamp(Σ × 组权重) 取最大写入 R；核通道取最大写入 B。
   - 结果写入现在的 `accum` / `fusion`，之后的 `_finalize`（ROI 矩形和多边形）以及标签都不变。
5. **binding 不变**：准入、请求、驻留、预算照旧。layer 根据提交的平面集合把 store 和索引表同步到"本次所需"，从描述符得到每个通道的层级顺序，所以 `_drawn_fine` 的语义变成按像素"目标层优先"。
6. **回退**：通道数超过参数纹理容量、或者 sampler 不够时，退回 §35 的逐通道路径；两条路径共用同一个 store。
7. **验证**：
   - 与逐通道旧路径比较：稳态逐字节相同；替身过渡期（目标层只覆盖一部分时）以"按像素目标层优先"为准，单独写测试；
   - Overlay 和 Fusion、ROI、标签、u8 / u16 / f32、NaN、`valid_rect`、边缘图块、小数相机位置、淘汰和复用；
   - 微基准在 4K 下 3 / 29 / 69 个通道；xwheel 测 3 / 13 / 29 / 69 个通道；记录显存。

### 35.7 codex 审 M1b 后的修订设计（实施依据）
1. **算术与参与条件和现在完全一致**：
   - 权重和颜色分开存放，按 `value = s × w`、`rgb += value × color` 计算；
   - 权重 ≤ 0 的通道不参与，权重按现有规则截到 1；分母 ≤ 0 时信号为 0；gamma 下限 1e-6；
   - 有效的黑色像素也算 alpha；
   - Fusion：组内按成员顺序求 Σ，再 clamp(Σ × 组权重) 取最大写入 R，核通道取最大写入 B。
   - 新旧路径继续逐字节对比，包括小数颜色和小数权重。
2. **每像素的取值规则 = 当前绘制顺序取反**：
   - 每个通道在提交的平面里，取"按提交顺序最后一个、且在该像素有有效样本"的平面。等价于按层级从细到粗探查提交集合，无效像素向下穿透。
   - binding 的 `_drawn_fine` 不变，所以与现在的覆盖语义逐像素相同。
   - 若某个通道的提交顺序不是按层级单调由粗到细，或者同一层有重叠或非网格的平面，这一帧退回逐通道路径。
3. **索引表表示"本次提交要画的图块"**：
   - 每个通道每个层级在 CPU 上保留一份 numpy 表，按提交集合求差，只上传变化的行区间。
   - 槽位复用时，先把旧格子置 -1，新像素和元数据写好后才填入新格子。
   - 块号固定、不重编号：块号就是 sampler 槽位号，删除块时这个槽位只会空出来，不会挪动。
4. **元数据纹理**（RGBA32F，按 (块, 层) 寻址）：平面世界矩形、整数格式的有效世界矩形、宽高。
   - 整数格式用半开区间的有效矩形判断；浮点格式按 NaN 和 Inf 剔除，不另外判断有效矩形。
   - 先用该层级的网格原点和 ds 定位格子并做边界检查，再取索引；纹素按 `floor(uv × size)` 截断。
5. **sampler**：
   - 固定命名：4 个 usampler2DArray（u8 / u16 块）、2 个 sampler2DArray（f32 块），用显式的 if 分支选择；
   - 所有层级的索引表拼成 1 张 isampler2DArray（各层级横向拼接，层号对应通道槽位）；元数据 1 张，参数 1 张；
   - 合计 9 个，低于 3.3 规范保证的 16 个。
6. **块**：每块 256 MiB（u8 1024 层，u16 512 层，f32 256 层），层数受运行时查询的上限约束。u8/u16 最多 4 块，f32 最多 2 块，与 1.5 GiB 预算对齐。
   - 预算较小、超出容量、着色器编译失败、或者遇到非网格平面时：退回逐通道路径（同一个 store 可用时继续用，否则逐平面纹理）。
7. **不变的部分**：`_finalize`（ROI）、标签、`shown`、只在相机变化时重新合成（不上传）。
   - 上传预算的问题（强制粗层绕过预算、校验发生在计时之前）留在 M2 处理，M1b 不宣称满足预算保证。

## 36. M2–M5 实施记录（2026-10-09/10，本地提交，未推送）

### 36.1 多通道（M2/M3 及收尾）
**新增或修改**：
- **准入**：每个层级的图块几何只算一次，各通道按纹素字节数计算预算。
- **显示快照复用**：显示变化时失效，另有 0.25 s 的过期兜底。
- **数组块**：
  - 每块 64 MiB；新块的首次使用要付出约 0.3–1.3 ms/MiB 的驱动代价（实测：256 MiB 约 90–290 ms，64 MiB 约 22–70 ms）；
  - 新块创建时立即预热：清空、写入、GPU 读回、再写入（`scratchpad/blockbench2.py`）；
  - 始终保留一个空的预热块作为储备，在 1.5 s 没有输入时补充；用户正在操作时不新建块（粗层除外）。
- **单 pass 的 sampler**：数量按 `GL_MAX_TEXTURE_IMAGE_UNITS` 生成（3060 和 Intel 都是 32）；空的 sampler 绑定 1×1 占位纹理。
- **索引表**：按格子写入，只上传变化的行；每个平面的网格位置按对象缓存。
- **对象缓存全部改为弱引用（`ObjectCache`）**：原来这些缓存持有已经丢弃的图块，69 个通道下 RSS 每分钟增长约 1 GiB。
- 发布历史保留 8 条，计数用 `publication_count`。

### 36.2 内存（M4/M5a）
- **Step0 概览**：`OMETIFFLoader.read_region(downsample>1)` 对不需要校正的通道改为按步长直接读 zarr。
  - 结果与"整幅读入再取 `[::ds, ::ds]`"逐元素相同，已验证；
  - 打开 Kevin 时 RSS 峰值从 6.8 GiB 降到 5.2 GiB。
- **`ui/gpu_memory.py`：按需释放**（用户裁决：离开后保留，需要时才释放）。
  - 隐藏的查看器在以下两种情况下退回到粗层，并释放组成目标：分割启动时（Step2、预分割），或者所有查看器合计超过 EDGE 档显存目标时。
  - 隐藏期间不再发布新画面。
- **实测**（Kevin，1080p）：
  - 69 个通道：RSS 峰值 6.3 GiB；本进程显存 1.4 GiB；整卡 2.6 GiB（含桌面和其他进程）。
  - 释放后回到 Step1：13 个通道 0.45 s 出画面、0.51 s 全部清晰；69 个通道 0.49 s 出画面、2.1 s 全部清晰。
  - M5b（Step0 与 Step1 共享缓存）：没有测到重复读取，暂不做。

### 36.3 流畅度现状（xwheel，各通道数分别取最近一次或最近两次运行）
| 通道数 | p50 | p99 | 最长 | 时间 |
|---|---|---|---|---|
| 3 | 17 ms | 44 ms | 69 ms | 当天 m3 |
| 13 | 18 ms | 55 ms | 89 ms | 当天 m3 |
| 29 | 22–24 ms | 68–83 ms | 99–134 ms | |
| 69 | 30–35 ms | 109–160 ms | 190–270 ms | |

69 个通道仍超出"p99 ≤ 50"的目标，剩下的耗时分散在发布、规划和等待垂直同步上；3 个通道与用户验收时的基线相当或更好。

## 37. 滚轮步长（2026-10-10，b03275b，已推送）
- 每格缩放从 ×1.35 改为 Odon 的 ×1.07（pyqtgraph `wheelScaleFactor = -1/35`，`ui/step1_viewer_host.py: WHEEL_SCALE_FACTOR`），Step1 与 Step3 一致。
- 同样 22 格的手势，缩放范围变小，与此前数字不可直接比较：69 通道 p99 72 ms / 最长 92 ms；3 通道 p99 70 / 最长 77 ms。

## 38. Rust 可行性实验、平滑显示、规划提速（2026-10-10，本地提交，未推送）

### 38.1 实验：读图与送达是否值得用 Rust 重写
- 开关（仅测量用，默认关）：`BLOCK01_A9_REPLAY=1`：读过一次的图块从内存直接交回，不解码不读盘；`BLOCK01_A9_BATCH=1`：把"图到了"的信号攒成一批再送。场景 `kx69rr.json`：69 通道，同一组手势连做两轮。
- 结果（显示延迟 p99 / 最长）：正常 69 / 79 ms；重放 77 / 98 ms（这一轮受同屏图形测试干扰，作废）；批量 103 / 131 ms；两者都开 90 / 122 ms。
- 关键发现：**手势期间没有一次读盘，也没有一次图块送达**，需要的图块都已在 4 GiB 内存缓存里；读图全发生在开头加载阶段。所以这两个实验在该场景下不可能有收益，差异只是每次运行的波动。
- 界面线程的时间几乎都在两段 Python 记账上：规划（每个通道要哪些图块）8.6 s、发布（哪些图块交给显卡）5.6 s。结论：读图部分改写成 Rust（方案 B）在此场景下没有收益；先剖析规划和发布（见 38.3）。

### 38.2 平滑显示（Odon 的 smooth_pixels，默认开）
- 着色器：只在"一个原始像素在屏幕上比一个屏幕像素宽"（任一方向）时启用；该像素的原始值由周围 4 个原始像素按双线性权重混合，然后再做强度映射。用哪一级图的规则不变（仍按像素中心是否有效决定）。
- 无效取样一律丢弃、权重重新归一：有效矩形之外、NaN/Inf、同级邻块缺失；**绝不从更粗一级取样**。
- 单 pass 合成器会去相邻图块取样，所以图块边界没有接缝；多 pass 后备路径只在本图块内混合（边界处最多半个像素宽的硬边）。
- 优化：一次选定纹理、一次取 4 个点（`fetch_raw4`）。
- 代价（RTX 4090，4K，放大 4 倍，每帧 GPU 时间）：3 通道 +0.15 ms，13 通道 +0.8 ms，69 通道 +4.3 ms；未放大时为 0。
- 界面：Overlay / Fusion 右侧的可按下按钮 "Smooth"（用户 2026-10-10 同意），默认按下；随 Step1 会话保存，旧会话没有该项时保持当前设置；恢复失败时一起回滚。所有 Step1 画面（查看器、预分割结果）同时生效（`gpu_memory.set_smooth`）。
- 验证：
  - 关闭时，与 b03275b 用 80 张测试图逐字节比对，完全一致；
  - `tests/test_a9_s38_smooth.py` 17 项；会话保存/恢复/回滚 4 项；按钮位置 1 项；
  - 图形回归与基线一致。
- 测试默认用最近邻：`conftest.py` 设 `BLOCK01_SMOOTH=0`，各项对比测试的基准不变。

### 38.3 规划与发布的剖析和提速
- 剖析（py-spy，只看界面线程）：规划的大头在"准入"计算里。每次镜头移动都把 69 个通道粗图的全部图块逐个重算字节数，缩到最粗一级时还要把精细图块与粗图合并成大集合再逐块计算；每个通道的规划又各自重算一遍和其他通道完全相同的范围与字节数。
- 改动（`ui/step1_gpu_binding.py`）：
  - 粗图的合并集合、字节数，以及每个通道已有的最粗级图块，缓存到粗图变化为止；
  - 最粗一级的合并字节数，改为"粗图字节 + 各通道不在粗图里的精细图块"；
  - 准入时一次算出各通道的字节数与世界范围，供各通道的规划直接使用；
  - 各通道的图块清单改为用到时才建立（`_LazyKeys`）；
  - 发布缓存不再按"准入对象"判断是否过期，只看它实际用到的三项：层级、是否只用粗图、该通道是否要精细图。
- 等价验证：临时断言版本在 21 个相关测试模块中做了 677 次规划比对和 142 次合并比对，结果与旧算法完全相同；另新增 2 项针对性测试。104 个相关模块的回归结果与基线相同。
- 效果（69 通道，手势期间）：准入每次 11.4 → 1.2 ms；规划单次 p95 16–20 → 4.3 ms（正好落在 4 ms 切片内），总计 8.6 → 3.9 s；发布单次 p95 25 → 21 ms，最长 45 → 30 ms。
- 慢格（> 50 ms）归因（`scripts/a9_slow_notches.py`，配合 `scripts/a9_threadstate.py` 线程状态采样），每格平均：发布约 19–22 ms，规划约 6–7 ms，绘制约 3 ms；剩下约 29–32 ms 不在任何计时段里，其中约一半界面线程在运行没有计时的代码（Qt / pyqtgraph），约一半睡在事件循环的 poll 里（等定时器或下一帧）。
- 端到端（Smooth 开，69 通道，两轮）：显示延迟 p99 78 / 83 ms，最长 92 / 99 ms。改动前同场景单次 p99 69 / 最长 79（Smooth 关），几次运行之间的波动在 69–103 ms。**规划提速没有明显改善端到端的最慢帧**，最慢帧主要卡在发布和 Qt 内部的未计时部分。3 通道：p99 64 / 最长 85 ms。
- 下一步选项：（1）发布做增量化，只处理变化的通道；（2）查清 Qt 内部未计时的那部分在做什么、等待在等什么（节流定时器、垂直同步）；（3）方案 C（Rust 规划/发布核心）。按剖析，C 最多能拿回发布和规划的约 25 ms，前提是这些记账逻辑整体迁移过去。

## 39. 与 Odon 的实测对比，以及目标（2026-10-10）

### 39.1 方法：对两个程序完全相同的外部测量
- 同一个注入器（`scripts/a9_xwheel.py`，XTest 真实滚轮/拖动）、同一组手势（kx69 的全部手势，外加"单格"手势：每格间隔 400 ms，各 15 格）。
- 画面变化：`scripts/a9_pixwatch.py` 每 0.26 ms 用 XGetImage 读取窗口中心附近 5 行像素，记录每次变化的时刻（与注入器同一个 CLOCK_MONOTONIC 时钟）；报告：`scripts/a9_pixreport.py`。
- Odon：`scripts/a9_odon_bench.py`，经它自带的控制接口（127.0.0.1:17870）收起左右面板，设 69 / 3 个通道、相机与我们手势开始时相同（中心 15850,25220，缩放 0.007477），画布约 1300×785（我们 1307×788）。
- 数据：Odon 打不开 qptiff（"mixed TIFF channel layouts"），用同一张片子的 OME-TIFF 版本 `TMA3/FINALRUN_updated_TMA3_Scan1_bgcorrected.ome.tiff`（69 通道、尺寸、6 级、512 分块均相同）；我们用 qptiff。手势期间我们不读盘，数据格式不影响我们的结果。
- 指标：
  - 单格响应：注入到画面开始变化；
  - 快速手势（连发、甩动、拖动）：每格的同一指标，以及超过 50 ms 的格数（每轮 420 格）。

### 39.2 结果（每行一轮）
| 程序 | 通道 | 单格 中位 / 95% / 最慢 (ms) | 快速 中位 / 95% / 99% / 最慢 (ms) | > 50 ms |
|---|---|---|---|---|
| Odon | 69 | 39 / 53 / 102 | 10.5 / 46 / 53 / 167 | 9 |
| Odon | 69 | 44 / 54 / 112 | 9.5 / 48 / 61 / 153 | 16 |
| Odon | 3 | 37 / 47 / 53 | 10.9 / 46 / 51 / 56 | 5 |
| 我们（现状 c224038） | 69 | 28 / 53 / 58 | 19.2 / 48 / 67 / 83 | 19 |
| 我们（现状） | 69 | 33 / 50 / 65 | 16.7 / 51 / 72 / 91 | 22 |
| 我们（现状） | 3 | 29 / 36 / 46 | 10.5 / 33 / 59 / 75 | 7 |
| 我们 + 交换不等刷新 + 停底层重画 | 69 | 17 / 26 / 30 | 16.1 / 48 / 62 / 78 | 20 |
| 同上 | 69 | 17 / 21 / 23 | 15.1 / 46 / 61 / 69 | 19 |

- 3 通道：我们与 Odon 基本持平，单格响应和 95% 线更好。
- 69 通道：单格响应和最慢一帧，我们已比 Odon 好；差距在快速手势的中位数（我们约 15–19 ms，Odon 约 10 ms）和超过 50 ms 的格数（我们约 20，Odon 9–16）。

### 39.3 目标（"与 Odon 一样丝滑"的量化定义，69 通道，本方法）
- 快速手势：中位数 ≤ 11 ms；99% ≤ 60 ms；每轮 420 格中超过 50 ms 的 ≤ 12 格；最慢 ≤ 100 ms。
- 单格响应：中位数 ≤ 30 ms，最慢 ≤ 60 ms（已达到）。

## 40. 交换画面不等刷新、GPU 接管时底层场景不重画（2026-10-10，本地提交）
- 诊断（`scripts/a9_notch_split.py`、`a9_event_attr.py` + `a9_drive` 的 `BLOCK01_A9_EVENTS` 事件时间线、`a9_gdb_poll.sh`）：
  - 慢格约 62 ms：Qt 送达滚轮之前约 42 ms，送达之后约 20 ms。
  - 送达前有约 15 ms 是"每帧第一次 Paint"：窗口合成加交换缓冲，按 Qt 默认的 swap interval 1 等待屏幕刷新。
  - 关掉底层场景重画后，这 15 ms 原样转到了 GPU 图层的 Paint 上，证明它是交换时的等待，而不是绘制本身。
  - gdb 采样确认：线程睡着时是在 Qt 事件循环里正常空等。
- 改动：
  - `ui/gpu_warmup.configure_surface_format()`：在 QApplication 之前请求 swap interval 0，`main.py` 调用。这只是向系统"请求"，驱动可以不理；`BLOCK01_VSYNC=1` 时保持 Qt 默认。
  - GPU 图层沿用应用的 interval（Qt5 会把控件的 interval 传回窗口）。
  - `Step1WholeSlideMount._hold_scene_repaint` / `_release_scene_repaint`：GPU 接管成功后，底层 QGraphicsView 设为 NoViewportUpdate；每一种释放（交还 CPU、重建、关闭）都恢复原模式。暴露和改变大小时仍会重画。
- 验证：
  - `tests/test_a9_s40_swap.py` 4 项、`test_step1_gpu_takeover` 新增 4 项；
  - 106 个相关模块回归与 a1f81e4 一致（唯一差异是已知的偶发失败 `test_the_real_start_path_records_what_the_worker_is_reading`，新旧代码各 10 次里都挂 3 次）。
- 实测（与 Odon 同一方法）：

| 运行 | 单格 中位 / 95% / 最慢 | 快速 中位 / 95% / 99% / 最慢 | > 50 ms |
|---|---|---|---|
| 69 通道 第 1 轮 | 19.9 / 25 / 30 | 17.3 / 75 / 171 / 203 | 44 |
| 69 通道 第 2 轮 | 17.0 / 29 / 39 | 15.7 / 46 / 59 / 72 | 16 |
| 3 通道 | 7.5 / 12 / 18 | 7.4 / 19 / 40 / 64 | 2 |

- 3 通道全面优于 Odon（Odon：快速中位 10.9、99% 51、超过 50 ms 5 格；单格中位 37）。
- 69 通道第 1 轮的长卡顿全部在第二组拖动：它紧跟在回归测试之后，文件缓存是冷的，拖到新区域时有 27 次读盘、191 次图块到达，界面线程连续多次各卡 40–50 ms。图块在移动中到达时，每帧的处理时间没有上限，这是下一步（第 3 项"记账让路"）要解决的。
- 实机验收待做：放大缩小窗口后边缘是否正常、Step1 与 Step3 来回切换、掩膜、CPU 后备的第一帧。Windows 上是否不等刷新、是否撕裂，需要在笔记本上确认。

## 41. "记账让路"实验：手势进行中推迟发布 / 规划（2026-10-11，实验开关，默认关闭）
- 现状：每次镜头移动后，`update_viewport` 立刻做一段规划（约 4 ms）和一次完整发布（69 通道 10–20 ms）。在这期间，下一格滚轮只能排队。画面跟随相机本身不需要这次发布：GPU 图层每帧都按新相机重画已有图块。
- 开关 `BLOCK01_A9_PUBMODE`：
  - `sched`：手势进行中（输入 60 ms 内），镜头移动后不立即发布，按每 33 ms 一次的节奏发布；
  - `quiet`：手势进行中不发布，停手 60 ms 后一次补上；
  - `quiet2`：手势进行中连规划也推迟，停手后一次规划加发布。
- 实测（69 通道，与 Odon 同一方法，每种两轮）：

| 方式 | 快速手势 中位 / 95% / 99% / 最慢 (ms) | > 50 ms | 单格中位 | 停手到画面稳定（中位 / 最慢） |
|---|---|---|---|---|
| 现状（§40） | 15–17 / 44–46 / 58–66 / 69–85 | 10–18 | 17–20 | 23–24 / 37–45 |
| sched | 13.2–13.8 / 38–40 / 50–53 / 59–66 | 4–6 | 16–17 | — |
| quiet | 12.6–12.8 / 26–27 / 38–41 / 59–65 | 1–3 | 18–19 | 22–26 / 32–39 |
| quiet2 | 10.3–12.0 / 22–23 / 30–46 / 48–62 | 0–3 | 17–18 | 21–23 / 24–37 |
| Odon | 9.5–10.5 / 46–48 / 53–61 / 153–167 | 9–16 | 39–44 | 45–46 / 321–2900 |

- quiet2 达到 §39.3 的目标：中位与 Odon 持平，其余各项明显更好。
- 代价：手势进行中不再加载和交换更清晰的图块，停手约 60 ms 后才补上。在这些已加载过的区域，"停手到画面稳定"并没有变长。在从未看过的新区域，移动中会比现在更模糊，需要实机目测确认。
- 测量工具：
  - `scripts/a9_settle.py`：停手到画面稳定的时间；
  - `scripts/a9_drop_file_cache.py`：只丢掉一个文件的系统缓存，用于冷读测试。
- 冷读复测（丢掉片子文件的系统缓存后各一轮，现状代码）没有复现 §40 中第 1 轮的 150–200 ms 长卡顿。那一轮更可能是机器上其他负载所致。

## 42. 细胞级实测：停手时能否无感切换（2026-10-11）

### 42.1 之前测试的局限
§39–§41 的全部手势都是在"看全片"的缩放级别（0.0075，放大至多约 4 倍）下做的。那里用到的只是金字塔最粗的一两级，打开时就已完整加载，所以手势中从来没有新图块要补，"停手后变清晰"根本没有被测到。为此新增了细胞级场景：
- `bench_a9/kxdeep.json`（69 通道）、`kxdeep3.json`（3 通道），以及 Odon 用的 `odon_bench/gestures_deep.json`；
- 起点：`a9_drive` 新增 `camera` 动作，中心 15035,28944，缩放 0.5；
- 手势：三段拖进新区域，缩放 +10/−20/+10 格，甩动，单格；
- 每段手势停下后截图（`scripts/a9_stopshots.py`），用 `scripts/a9_shotdiff.py` 算与最终画面相差超过 8 个灰度的像素占比。
- 注意：截图要先确认 50 ms 内没有新输入才开始，所以标为"20 ms"的那张，实际是停手后约 52 ms（脚本会打印真实时间）。
- 69 通道叠加后画面饱和成白色，无法目测清晰度，所以停手切换只用 3 通道判断。

### 42.2 结果
- 停手切换（3 通道，与最终画面不同的像素占比）：
  - 现状：每段手势在约 52 ms 时都是 0 %，无感；
  - Odon：同样 0 %，无感；
  - quiet2：缩放时 13–45 %，直到 100–160 ms 才稳定，是可见的跳变；
  - sched：除斜向拖动有 22 %、100 ms 内稳定外，其余为 0 %。
- 3 通道快速手势（n=222）：
  - 现状：中位 6.5–7.1 / 95% 18–20 / 99% 32 / 最慢 40 ms，超过 50 ms 0 格；
  - Odon：中位 8.7–9.0 / 95% 29–31 / 99% 45–46 / 最慢 51–55 ms，超过 50 ms 1 格。
  - 即 3 通道在细胞级已经同时做到比 Odon 更顺、停手无感。
- 69 通道快速手势：

| 方式 | 中位 / 95% / 99% / 最慢 (ms) | > 50 ms |
|---|---|---|
| 现状（冷 / 热） | 16 / 22；95% 62–64；99% 85–124；最慢 101–140 | 18–28 |
| quiet2 | 10.6 / 59 / 126 / 142 | 14 |
| sched | 15.6 / 69 / 96 / 112 | 21 |
| Odon（冷 / 热） | 10.5 / 10.9；95% 56–81；99% 192–222；最慢 222–282 | 17–21 |

- 结论：quiet2 做不到无感切换，不做成正式功能。

### 42.3 codex（astra high）讨论结论
- 推荐 (a) 增量发布加 (b) 统一的每帧时间预算：
  - 每帧只处理真正变化的通道、图块和页表格子；
  - 规划、接收、上传、页表同步和发布共用一份每帧约 4 ms 的预算；
  - 手势进行中持续补清晰图块（不等停手）；
  - 结果队列按数量和字节有界；
  - 优先级：可见区域缺覆盖的 → 当前目标层 → 各通道公平推进 → 替身 → 预取。
- 估计工作量约 1–2 周。
- 若做完后测出预算内的处理能力仍不够，再把记账核心迁到 Rust（第二阶段）。不用 quiet2，也不用"淡入"去掩盖。
- 建议的细胞级 69 通道验收目标（尚未批准）：中位 ≤ 11、95% ≤ 50、99% ≤ 80、最慢 ≤ 100 ms，超过 50 ms 的约 ≤ 5 %；停手约 50 ms 时与最终画面差异 ≤ 1 %，此后保持不变。
- 用 69 通道全开但强度不饱和的显示设置做清晰度验收；3 通道不能因 69 通道的优化而退步。
- 如果 69 通道只算压力测试（日常 2–10 通道），codex 建议只做消除无效失效、约束到达突发、局部增量这几项，不为 69 通道立即上 Rust。

## 43. 10 通道：流畅已超过 Odon，停手无感切换未达到（2026-10-11）
- 用户裁决：69 通道全开只作压力测试；日常要求是至少 10 个通道达到 Odon 水平（手势中顺滑，停手时已清晰）。
- 场景：`kxdeep10.json`（qptiff）和 `kxdeep10bg.json`（与 Odon 同一份 OME-TIFF，测试项目 `bench_rm/accept/kevin_bg`）。10 个通道：DAPI、HER2、CK20、CD45RO、KI-67、TCRb、Vimentin、TROP2、CD31、CD8。
- 流畅度：
  - 我们：中位 7.9–9.4 / 95% 23–31 / 99% 32–49 / 最慢 36–74 ms，超过 50 ms 0–2 格（共 222 格）；
  - Odon：中位 8.3–9.4 / 95% 33–41 / 99% 49–55 / 最慢 63–69 ms，超过 50 ms 2–5 格。
  - 我们更好。
- 停手切换（同一文件，约 52 ms 时与最终画面相差的像素占比）：
  - Odon：0–1.1 %；
  - 我们：拖动约 30 %，到 80–160 ms 归零；放大 41–47 %，160 ms 时仍有 30–40 %，250 ms 才归零；缩小 46–78 %，160–250 ms 归零。
  - 3 通道时我们是 0 %：问题随每次跨级要换的图块数增长。
- 放大跨级的时间线：
  - 跨级发生在停手前 35–42 ms，需要 120 块图块；
  - 读取的开始时刻分散在 −47 到 +117 ms，最后一块在 +99 到 +128 ms 读完；
  - 每块在程序里实际耗时 8–11 ms（单独测 1.7 ms，8 线程约每秒 3700 块）；有效吞吐约每毫秒 0.7–0.8 块。
- 调参开关（默认值不变，仅用于测量）：`BLOCK01_A9_HOT_UPLOAD_BUDGET_MS` / `BLOCK01_A9_HOT_PUBLISH_MS` / `BLOCK01_A9_PUBLISH_DUTY`。
  - 手势中上传 4 ms、发布 16 ms、系数 1.5 对拖动略有帮助，对缩放无效；
  - 16 个读图线程、1 ms 的锁切换间隔也都无效。
- codex（astra high）结论：缺口在目标层的数据供应速度，其次是图块到达后上屏的尾延迟。按小到大排序：
  1. 分段量清楚程序内读图为何比单独测慢 4–5 倍，并消除发放请求和复制上已证实的浪费，争取 10 通道的请求在 4 ms 内全部发出；
  2. 接近换级阈值前预读相邻一级（放大、缩小两个方向，按滚轮速度决定提前量），不改变显示用哪一级的规则；
  3. 最小版"增量发布 + 每帧统一预算"，让已到达的图块每帧都上屏；提前准备好显存块，避免停手后才扩容；
  4. 若读图仍达不到每毫秒 2–3 块，再做小型原生读图器（读取、解压、复制全程不占 Python 全局锁），或改用常驻进程池加共享内存。
- 建议验收（待批准）：
  - 10 通道：中位 ≤ 10、95% ≤ 33、99% ≤ 50、最慢 ≤ 75 ms，超过 50 ms ≤ 2 格（共 222 格）；停手 50–55 ms 时差异 ≤ 1.2 %，并保持；
  - 3 通道不退步；
  - 69 通道压力测试：持续输入中不得有超过 100 ms 的停顿。
