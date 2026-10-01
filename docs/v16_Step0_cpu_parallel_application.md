# v16 块 S0P — Step0 背景校正（tophat / cucim）在 CPU 上多线程并行：实施申请 v2

日期：2026-10-01。分支 `v16`，调查基于 HEAD `935d809`。

依据：
- 用户 2026-10-01 的问题：「对于 step0 的 tophat 和 cucim，当无法使用 GPU 时，能否使用 CPU 多线程并行计算？」
- 用户 2026-10-01 的指令「S0P 申请 v2 修订 → §2.4 基准测量 → 按实测填裁定 3/4 → 实施」（§9 记录全文要点）。

状态：**申请 v2，裁定 1–5 已定**（§9）。裁定 3 / 4 和 P6 阈值按用户授权，由 Fable 5.1 整合两份独立审核后给出，本窗口执行；§9 末尾列出用户醒来后要确认的 3 点。

修订记录：
- v1：草稿。
- v2：按用户指令修订：
  - 新增 §3.2：同一通道内禁止混用两种后端；
  - 新增 §3.3：后端写进 attrs，并进入增量复用的签名；
  - 新增 §3.4：进程后端的 spawn 约定；
  - 并行数预案改为从 4 起；
  - 验收门 P2 拆为 P2a / P2b，修改 P3、P6，新增 P7、P8；
  - §6 风险表改了一处措辞；
  - v2.4 §12 新增「用户插入块」表。

---

## 1. 必要性与目标

- **现状**：机器上没有可用 GPU 时，Step0 Save 对每个校正通道的每个 4096 tile，**串行**调用单线程的 skimage。
  - **本机正是这样**：CuPy 能导入，但冒烟测试失败（缺 `libnvrtc.so.12`），所以 tophat 和 cucim 都走 CPU。
  - 本机有 16 个逻辑核，只用到 1 个。
- **目标**：在 CPU 路径上并行处理 tile，让 Save 变快，**结果与串行逐位相同**。
- **不做**：
  - 改两条路径的数值实现、参数、tile 大小或 halo；
  - 改 GPU 的结构元素（裁定 5：方形 / 圆盘的统一，等有 GPU 的机器时另开块 S0G）；
  - 改 Step0 的读取路径或预览路径；
  - 改 UI。

## 2. 只读调查结果

### 2.1 整片校正循环（Save 的主要耗时）

`ui/step0/search_ctrl.py`：`WsiCorrectionWorker`，`:2300-2460`。

- **结构**：对每个 ROI、每个校正通道：
  1. 用 `_tile_slices(roi_h, roi_w, 4096, overlap)` 切 tile。halo = `method_overlap`：tophat 是 2r，cucim 是 ⌈4σ⌉（`core/bg_correction.py:49-63`）。
  2. 对每个 tile **依次**做：
     - 读：`self.loader._read_roi_zarr(...)`；
     - 算：`_apply_tophat_cpu(raw, param)` 或 `_apply_cucim_or_cpu(raw, param, prefer_gpu=…)`；
     - 裁剪，有多边形时把多边形外置零；
     - 写：`ds[y0:y1, x0:x1] = out`；
     - 累加粗层平面：`accumulator.add(out, y0, x0)`；
     - 发出进度。
- **取消**：逐 tile 检查（`:2394-2406`），丢弃写了一半的通道。
- **通道写完后**：写入 uuid 令牌 `source_identity` 和 `written_at`，再发布粗层平面（`:2440-2455`）。

### 2.2 能不能并行，怎样逐位相同

- **每个 tile 的计算彼此独立**：输入是它自己带 halo 的窗口，函数和参数都相同。所以交给任何一个线程或进程来算，输出都逐位不变。
- **读取是线程安全的**：`_read_roi_zarr`（`core/io_loader.py:259-284`）每次调用都自己打开一个 `TiffFile`，不碰 loader 的可变校正状态。
- **写入和粗层累加要保持顺序**：
  - `_CoarsePlaneAccumulator.add`（`:1979-2030`）把值累加进 float64 的块和。
  - ROI 原点不对齐 stride 时，一个块会跨两个 tile，累加顺序一变，最后几位就可能不同。
  - 所以方案是**计算并行，写入和累加仍按 tile 顺序做**。
- 线程能带来多少加速，要以实测为准（§2.4）。不从加速比去推断原因。

### 2.3 后端在通道内可能混用（v2 新增）

- `core/bg_correction.py:303-315`：每个 tile **先试 GPU**；出了异常，就调用 `_disable_gpu_morph`，然后**改走 CPU**。
- 后果：一个通道可能前半段是 GPU 的方形结构元素，后半段是 CPU 的圆盘。数组本身看不出这一点，attrs 里也没有记录。
- cucim 的两条路径算法相同，差别只在浮点舍入。但同一通道里混用两种后端，同样不再是一种可以复现的计算。

### 2.4 基准测量（S2T 回归结束后做，只读）

在 test1 上取一条真实 tophat 通道和一条真实 cucim 通道（15437×16215，16 个 4096 tile），每条都测：
- 串行；线程 2 / 4 / 8；进程 2 / 4 / 8；
- 每一项记录：
  - 端到端耗时；
  - 峰值 RSS（主进程与子进程合计）；
  - 单 tile 耗时分布（包括最慢的一个）；
  - 取消延迟（在第 3 个 tile 之后发出取消）。

测量前确认 C 盘剩余 ≥ 20 GB。用 `setsid nohup` 启动，并写完成标记。结果以表格报告；不按「加速不到 2 倍就是 GIL 的原因」这类规则推断。

### 2.5 其他用到同一批函数的地方（本块不改）

Step0 的预览：`:1611`、`:1759`（已经按 patch 并行）、`:1882`。都是小图。

## 3. 做法与提交切分

提交固定为三段，每段都可以单独回退：① 执行器和它的测试（不接线）；② 通道级后端冻结 + attrs + 签名（§3.2、§3.3）；③ tile 循环接到执行器上。

### 3.1 第 ③ 段：tile 级并行（只改 `WsiCorrectionWorker` 的 tile 循环）

1. 对一个通道的 tile 序列，用一个容量有限的执行器，**预先计算后面 N 个 tile**。读取、计算和**裁剪**都在任务里完成，任务只返回 4096² 的核心区。线程和进程两种后端走同一条裁剪路径。
2. worker 线程**按 tile 顺序**取结果，然后做多边形置零、写 `ds`、`accumulator.add`、发出进度。这些都与今天完全相同。
3. **取消**：
   - 每取一个结果检查一次；
   - 取消后不再启动新任务，未开始的 future 全部 cancel，等正在跑的任务结束；
   - 然后照今天的方式丢弃写了一半的通道；
   - 取消之后不再发生任何落盘（P3）。
4. **只用于 CPU 后端**：通道的后端被冻结为 GPU 时（§3.2），保持今天的串行（P5）。
5. 新模块 `core/bg_parallel.py`：放一个「按顺序交付结果的有界执行器」和任务函数，不依赖 Qt，可以单独测试。`search_ctrl.py` 只改 tile 循环的几行接线。

### 3.2 第 ② 段之一：同一通道内禁止混用后端（v2 新增，必做）

- **每个通道开始时冻结后端**（`cpu` | `gpu`），整个通道只用这一种：
  - `GPU_MORPH_AVAILABLE` 为真就是 `gpu`，否则是 `cpu`；
  - cucim 另外受今天的 `CUCIM_AVAILABLE` 约束（`prefer_gpu=CUCIM_AVAILABLE`），规则不变。
- **GPU 在通道中途失败**：
  1. 丢弃该通道已经写入的部分（与取消时同一个方法）；
  2. 从 tile 0 开始用 CPU 重跑**整个**通道；
  3. 记日志。

  不允许接着上一个 tile 往下用 CPU 算（P7）。
- 只有在这个前提下，attrs 里才可以写单一的 `bg_compute_path`。
- **对 `core/bg_correction.py` 的最小改动**：把「选哪个后端」从每个 tile 的内部移到通道级的入口。
  - 新增一个按指定后端计算的入口；指定 GPU 时，失败就抛出异常，而不是悄悄改走 CPU；
  - 原来的 `_apply_tophat_gpu_or_cpu` / `_apply_cucim_or_cpu` 保留给预览路径，行为不变；
  - **两条路径的数值实现一行不改**。

### 3.3 第 ② 段之二：后端进 attrs 与增量复用的签名（v2 新增，必做）

- 每个校正数组的 attrs 新增两个键：
  - `bg_compute_path` ∈ {`"cpu"`, `"gpu"`}；
  - `tophat_footprint` ∈ {`"disk"`, `"square"`}，cucim 填 `null`。
- **增量复用的签名**：
  - `read_corrected_zarr_state`（`ui/step0/search_ctrl.py:1920-1976`）读出的签名，从 `(method, param_value, algo_version)` 扩为 `(method, param_value, algo_version, bg_compute_path, tophat_footprint)`，并更新 docstring。
  - 本机「当前签名」由 `Step0Page._cur_sig`（`ui/step0/step0_page.py:11135-11142`）给出，也要相应扩充。为了不在大文件里写逻辑，计算放进 `core/bg_correction.py` 的一个小函数（例如 `current_compute_signature(method)`）；`step0_page.py` 只改 `_cur_sig` 返回值那一行接线（v2.3 §0.4 规则 6 允许的接线）。
- **旧产品**：缺这两个键时视为不相等，下次 Save 重新计算。这与今天 `param_value=None` 的处理方式一致。
- **测试**：`tests/test_step0_correction_param_inheritance.py`、`tests/test_step0_process_incremental.py` 中涉及签名的断言，按新签名补充（只加，不删）。
- **A3-3 的 `corrected_channel` 登记**：它把数组 attrs 中选定的键写进 `parameters`。新增的两个键不在那份清单里，所以不会自动带入。A3 的代码不改，只在执行记录里注明；之后若需要，由碰到 provenance 的块再加。

### 3.4 进程后端的 spawn 约定（v2 新增，条件适用：裁定 3 选进程时）

- 任务必须是 `core/bg_parallel.py` 的**模块顶层纯函数**；参数只包含可以 pickle 的值：`slide_path`、`channel_index`、`padded_bbox`、`crop`、`method`、`param`、`backend` 等。
- **禁止**捕获 `self`、loader、QThread 或任何绑定方法。
- 子进程自己打开 TIFF、读带 halo 的窗口、计算、裁剪，**只返回核心区**。读取方式与 `_read_roi_zarr` 相同（`tifffile.aszarr` 的同一层、同一页）。
- 多边形置零、写 `ds`、粗层累加和进度，仍在 worker 线程里按 tile 顺序做。

## 4. 封闭白名单

- `ui/step0/search_ctrl.py`：**只改** `WsiCorrectionWorker` 的 tile 循环与通道级后端冻结 / 重跑（`:2370-2440` 一带），以及 `read_corrected_zarr_state` 的签名和 docstring。
- `ui/step0/step0_page.py`：**只改** `_cur_sig` 返回值那一行接线。
- `core/bg_correction.py`：只做 §3.2 和 §3.3 需要的最小改动（通道级的按后端计算入口，以及 `current_compute_signature`）。两条路径的数值实现不改；`_apply_*_gpu_or_cpu` 的现有行为不改。
- 新增 `core/bg_parallel.py`。
- 测试：新增 `tests/test_step0_bg_parallel.py`；`tests/test_step0_coarse_plane_write.py`、`tests/test_bg_correction_halo.py`、`tests/test_step0_correction_param_inheritance.py`、`tests/test_step0_process_incremental.py` 只加，不删已有的断言。
- 文档：本申请的执行记录；v2.4 §12 的「用户插入块」表和进度行。
- **UI 不改**：进度条和取消的界面不变。如果需要新增设置项，先停下来问。

## 5. 不改的范围

- tophat / cucim 的数值实现、GPU 的结构元素（S0G 另做）、参数、tile 大小、halo。
- Step0 的读取路径、预览路径、校正产品的格式（只多两个 attrs）。
- 粗层平面的算法与发布方式。

## 6. 预注册验收门（每条都配反向注入）

| # | 门（自动） | 反向注入 |
|---|---|---|
| P1 | **逐位相同**：合成切片（多个 tile、ROI 原点不是 stride 的整数倍、带多边形），tophat 和 cucim，各种并行数，与串行相比：校正数组、粗层平面逐位相同；attrs 只有 `source_identity` / `written_at` 不同 | ① 改成按完成顺序写入和累加 → 原点不对齐的用例上，粗层平面变红；② 任务里改用别的 tile 的窗口 → 红 |
| P2a | **test1 tophat**：test1 副本上一条真实 tophat 通道，新旧代码各 Save 一次，`np.array_equal`；attrs 的差异只能是 `source_identity`、`written_at`，以及新代码多出的两个键 | — |
| P2b | **test1 cucim**：同上，一条真实 cucim 通道 | — |
| P3 | **取消**：取消后不再启动新任务，未开始的 future 全部 cancel，不再发生落盘；worker 停止的延迟 ≤ 1.5 × §2.4 实测的最慢单 tile 耗时；写了一半的通道照今天的方式丢弃，增量 Save 已完成的通道保留 | 取消后不撤销排队中的任务 → 红 |
| P4 | **内存有上限**：同时在算的 tile 数从不超过上限（执行器自己计数），峰值 RSS ≤ 预算（裁定 4） | 去掉上限 → 红 |
| P5 | **GPU 后端不走并行**：后端冻结为 `gpu` 时（测试里模拟），调用顺序与今天相同 | 让 GPU 也走并行 → 红 |
| P6 | **加速**：测量前只设 go / no-go。最佳的安全方案加速 < 1.5× 时，本块不实施，保留串行。正式阈值在实测后由用户填入（裁定 4） | 并行数强制为 1 → 红 |
| P7 | **GPU 中途失败**：模拟 GPU 在第 k 个 tile 抛异常 → 该通道最终的 attrs 为 `bg_compute_path="cpu"`、`tophat_footprint="disk"`，数组与纯 CPU 串行逐位相同，磁盘上没有残留的 GPU tile | 允许接着用 CPU 往下算 → 红 |
| P8 | **增量复用**：旧 zarr（没有新 attrs）在增量 Save 时被重新计算；带 `"gpu"` / `"square"` 的 zarr 在 CPU 机器上不被复用；签名相同的通道照旧跳过 | 签名不包含新字段 → 红 |

**单调绿色规则**：Step0 相关模块，以及全部离屏模块和 GPU 模块（`BLOCK01_REQUIRE_STEP1_GPU=1`），在当前代码和 HEAD 上各跑一遍，逐条对比失败的测试名，没有新增失败。

**真机**：在界面里 Save 一条 tophat 通道和一条 cucim 通道，结果与旧代码逐位相同；运行中取消一次，行为符合 P3。

## 7. 风险

| 风险 | 对策 |
|---|---|
| **CPU 圆盘形态学的成本随 footprint 明显增长**；并行只缩短 wall time，不改变单个 tile 的算法复杂度 | 算法层面的优化（例如 footprint 分解）不保证逐位相同，必须另开块；本块只做并行 |
| 线程拿不到加速 | §2.4 实测；用进程池（§3.4） |
| 内存：同时算 N 个 tile，每个 tile 加 halo 后 float32 约 80 MB，中间结果还要乘几倍 | 并行数受内存预算约束；P4 门 |
| 取消变慢 | 最多再等 N 个正在算的 tile；P3 的延迟上限 |
| 结果因为写入顺序而不同 | 按 tile 顺序写入和累加；P1 专门测原点不对齐的情况 |
| GPU 中途失败导致一个通道混用两种后端 | §3.2：丢弃这个通道，整个通道用 CPU 重跑；P7 |
| 旧产品因为签名变化而被重新计算一次 | 这是预期的：旧产品没有记录后端，无法证明它与本机的计算一致 |

## 8. 估计

约 2 个工作日，另加 0.5 天测量与回归、0.5 天真机。

## 9. 裁定

**已定（用户 2026-10-01）：**
1. S0P 插在 S2T 之后、A6 之前。v2.4 §12 新增「用户插入块」表，S2T 和 S0P 都列在里面，不另写计划修订版。
2. 范围只限整片 Save 的 tile 循环（`WsiCorrectionWorker`），预览路径不改。
5. GPU 方形 / CPU 圆盘的不一致：本块选 (a)，不改科学算法。GPU 圆盘化等有 GPU 的机器时，另开 S0G 块。

**§2.4 实测（2026-10-02 01:41，test1，每个配置测 1 次，所有并行结果与串行逐位相同）**：

| 通道 | 方式 | 并行数 | 耗时 | 加速 | 峰值 RSS | 最慢 tile | 取消延迟 |
|---|---|---|---|---|---|---|---|
| HsBAg tophat r=15 | 串行 | 1 | 317.7 s | 1.00× | 1446 MB | 21.5 s | 0 |
| | 线程 | 2 / 4 / 8 | 166.9 / 93.4 / 57.8 s | 1.90 / 3.40 / 5.50× | 2636 / 3788 / 4336 MB | 22.5 / 25.0 / 29.6 s | 0 / 22.3 / 22.4 s |
| | 进程 | 2 / 4 / 8 | 170.6 / 94.8 / 61.5 s | 1.86 / 3.35 / 5.16× | 4441 / 5026 / 6260 MB | 22.6 / 25.1 / 30.0 s | 0 / 21.2 / 22.6 s |
| CD68 cucim σ=50 | 串行 | 1 | 61.4 s | 1.00× | 2795 MB | 4.5 s | 0 |
| | 线程 | 2 / 4 / 8 | 31.7 / 18.3 / 13.6 s | 1.94 / 3.36 / 4.53× | 2959 / 4217 / 4532 MB | 4.4 / 4.9 / 7.6 s | 0 / 4.4 / 4.3 s |
| | 进程 | 2 / 4 / 8 | 34.8 / 20.2 / 15.6 s | 1.77 / 3.04 / 3.94× | 4589 / 5181 / 6277 MB | 4.6 / 5.1 / 7.4 s | 0 / 4.3 / 4.7 s |

关于内存一栏：测量脚本为了做逐位比对，持有整片数组（最多 3 份）；cucim 那几行还带着 tophat 测完后残留的内存。所以 P4 改用真实 worker 重新测量。

**审核与整合（用户 2026-10-01 夜间授权：Fable 5.1 与 codex gpt-6-astra 各自独立审核，由 Fable 整合，交本窗口执行）**：

两份审核和整合结论的原文存放在 `~/fusionflux/bench_s0p/review/`（`codex_review.md`、`fable_review.md`、`fable_integration.md`）。

3. **线程**（`ThreadPoolExecutor`）。每个通道一个执行器，通道结束时 shutdown；结果按 tile 顺序消费，提交窗口有界。不启用 §3.4 的 spawn 约定。
4. **并行数**由纯函数 `choose_workers(cpu, n_tiles, avail_bytes)` 决定：
   - `n_cpu = max(1, cpu//2 − 1)`；
   - `n_mem = floor((MemAvailable − 1.0 GB) / 0.75 GB)`，从 `/proc/meminfo` 读取，读不到时不施加这一项；
   - `n = min(4, n_cpu, n_tiles, n_mem)`；`n < 2` 时走串行，也就是今天的代码路径。

   不升到 6 / 8 个。
   - **P4**：(a) 已提交、尚未消费的 tile 数（包括已经算完、还没写入的）从不超过 n；(b) test1 tophat r=15、n=4、新进程、经过真实 worker：峰值 RSS 减去通道开始前的 RSS ≤ 3.0 GB；(c) 记录 Save 前后的 SwapUsed，以及 Save 之后的 RSS。交换区不算在预算里。
- **P6 正式阈值**：tophat 和 cucim **各自 ≥ 2.0×**，不取平均。在 test1 副本上，新代码 n=4 对比 HEAD 的串行，经过真实 worker 做完整的通道 Save，强制重算，各跑 3 次取中位数。< 1.5× 不实施；1.5–2.0× 记为「未达正式阈值」，交给用户。
- **P3（修订）**：
  - 取消后不再启动新任务，未开始的 future 全部 cancel；
  - 延迟从取消请求算到 worker 发出停止信号，包括执行器 shutdown 和删除半成品通道；测量时，在第 3 个结果消费之后、正有 n 个任务在算时发出取消；
  - 上限：tophat 37.5 s，cucim 7.4 s（1.5 × §2.4 中 n=4 的最慢 tile）；
  - 「不再落盘」指不再写任何新的 tile，也不写 attrs 令牌；允许已经开始的那一次 tile 写入完成，也允许删除半成品。
- **实施上的一处补充**（本窗口，为了不改动白名单外 `tests/test_wsi_cancel.py` 既有的断言「取消发生在第 1 个 tile 读取期间时，只读 1 次」）：下一个 tile 要等前一个 tile **读取完成**之后才提交，提交前先检查取消。读取（约 0.1–0.3 s）因此变成串行，计算（tophat 每个 tile 约 20 s）仍然并行。

**用户醒来后需要确认的**：
1. 是否保留 `n_mem` 这一项（删掉的话就只剩 `min(4, cpu//2−1, n_tiles)`）；
2. 真机 Save 时如果 swap 增长，是否算作硬性不通过（目前只记录）；
3. 6 / 8 个并行不在本块，以后另开。

**原定待裁定事项（已按上面的整合结论落地）：**

3. 线程还是进程。
4. 并行数（预案：`max_workers` 从 4 起，按实测决定是否升到 6 / 8）和内存预算（2 GB，受 P4 约束）；以及 P6 的正式阈值。

**实施阶段的约束（获批后）**：三段提交，每段可单独回退；GPU 后端不走并行；单调绿色规则；真机验收；不改 UI，如需新增设置项先停下来问。
