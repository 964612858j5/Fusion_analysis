# StarDist 的 tile 大小按显卡显存定（backlog 29）

v0.4，2026-11-19（v0.3，2026-11-18）。用户裁定：按显存自动调整；不用 stardist 内部切块（`n_tiles`）。
v0.3（用户裁定 2026-11-18，"同意 A+C+D"，并要求 CUDA 实测逐位相同）：在 v0.2 的上限之外，
- A：网络改为省显存的写法，结果逐位相同（§2.5）；
- C：hybrid 在 StarDist 之前释放 Cellpose 用完的缓存（§2.6）；
- D：每次调用 StarDist 写一行诊断（§2.7）。
v0.2 的显存上限保留，作为最后一道保险，按 A 之后的峰值重算（§2.1）。
v0.4（用户裁定 2026-11-19，"同意方案，1ulp用B"；方案经 codex astra low 审核）：
- C 恢复为只在 hybrid 的 Cellpose 之后清缓存；另给 torch 的分配器设上限，为显卡的 85%（§2.6）；
- CUDA 上分条可能差 1 ulp，接受，`BEHAVIOR_VERSION` stardist 2→3、hybrid 1→2（§2.5）；
- 系数 200 不变（实测 192.1 B/px）。
讨论依据：Fable 5.2 high 在 Mac 上的实测，以及 codex astra high 的分析（2026-11-18）。
v0.1 经 codex astra low 第 1 轮审核，结论 FAIL：
- Auto 建议只控制每块自己的面积，不控制读取窗口；
- 查到的显存不一定是引擎用的那块卡。

v0.2 因此修改了 §2.1–2.4、§3、§4、§5、§6。v0.2 经第 2 轮审核，结论 PASS；3 条建议已写进 §2.1、§2.2 和 §4。

## 1. 问题

Windows 笔记本（RTX 3060 Laptop 6 GB，16 GB RAM），Full WSI 15437×16215：

| 方法 | 网格 | 单块读取窗口 | 结果 |
|---|---|---|---|
| hybrid_cellpose_stardist | 3×4 | 5346×4454（23.8 MP） | 引擎退出 0xC0000409，崩在 `nvcuda64.dll` |
| hybrid_cellpose_stardist | 4×4 | 4060×4454（18.1 MP） | StarDist `F.conv2d`：cuDNN "GET was unable to find an engine" |
| stardist_nuclei_expansion | 4×4 | 第 1 块成功（15.6 s，7229 个核），第 2 块 4260×4254 | `CUDA error: out of memory` |

原因（codex astra low 同意）：`StardistEngine.predict` 不传 `n_tiles`（`seg_runner/engines.py:370-374`），整块 tile 一次送进 torch UNet（`seg_runner/stardist_torch/graph.py`）。
- 全分辨率的 32 通道 float32 激活值每像素 128 B。
- 算第二层卷积时，输入激活、`F.pad` 的副本和输出同时存在，峰值约 3×128 B/px，加上输入，约 **390 B/px**。
- 18.1 MP 约需 6.6 GiB，6 GB 显卡装不下。

现有的上限只有两处，都是 60 MP：
- `Step2Page.STARDIST_MAX_TILE_PX`（`ui/step2_page.py:1539`）；
- TMA 按 core 运行时的 `step2_units.CAP_PX["stardist"]`。

两者都是 2026-10-25 按主机内存（`std::bad_alloc`）定的，和显存无关。

## 2. 做法

StarDist 引擎的数值不变：`n_tiles=None`，权重、归一化、阈值、fp32、cuDNN deterministic、不用 TF32 都不动。网络的运算顺序也不变，只是换了更省显存的写法（§2.5），输出在 CPU 和 MPS 上和原写法逐位相同；CUDA 上可能差浮点舍入（v0.4，用户裁定接受，§2.5）。Step2 外层网格允许的单块大小按显存定。外层切块、200 px 重叠和接缝合并都是现有功能，`docs/stardist_torch_plan.md` §2.4 的保证范围不变。

### 2.1 一个函数决定 StarDist 单块上限

`utils/step2_units.py` 新增：

```python
STARDIST_BYTES_PER_PX = 200          # A 之后的峰值估计（§2.5），笔记本实测后定稿
CUDA_RESERVE_GIB = 1.0               # CUDA 上下文 + 权重 + cuDNN 工作区
HYBRID_RESERVE_GIB = 1.5             # hybrid：Cellpose (cpsam) 的权重一直在显存里
VRAM_USABLE = 0.75                   # 只按总显存的 3/4 规划

def stardist_cap_px(method, vram_gib, use_gpu) -> int:
    """StarDist（含 hybrid 的 StarDist 部分）单次引擎调用的读取面积上限：
    min(60 MP 主机内存上限, 显存上限)；不用 GPU 或显存未知时只有 60 MP。"""
```

- 单位一律用 GiB。显存上限 = (vram_gib × 0.75 − 保留量) × 1024³ / 200，最少 1 MP。保留量：StarDist 1.0 GiB；hybrid 是 1.0 + 1.5 = 2.5 GiB。
- 6 GiB 卡：StarDist 约 18.8 MP，hybrid 约 10.7 MP。在笔记本那张 WSI 上，分别是 4×5 和 6×6 的网格（v0.2 是 6×6 和 8×9）。
- 11 GiB 约 38.9 MP，24 GiB 及以上仍是 60 MP。
- 这些系数是计算值。第 4 节在笔记本上实测后再定稿；常量都集中在这一处。

### 2.2 谁用它

1. **Step2 网格**（`ui/step2_page.py`）：`STARDIST_MAX_TILE_PX` 改为方法 `_stardist_cap_px()`，参数取自当前方法、引擎那块卡的显存（§2.3）和 `_cp_gpu.isChecked()`。
   - 原来用到 60 MP 的地方都改用它：`_stardist_safe_grid`、`_stardist_grid_ok_for_run`、`_keep_stardist_tiles_safe`，以及提示文字里的 "tiles ≤ N MP"。
   - 勾选或取消 "Use GPU if available" 时，再跑一次 `_keep_stardist_tiles_safe`，因为上限会跟着变。
   - 选方法、换输入、改行列时的现有触发点不变。
2. **Auto 建议**：`suggest_tile_strategy` 不改。它按每块自己的面积定网格，不算重叠，还允许超出 20%（codex 第 1 轮）。
   - 用 StarDist 方法时，`_refresh_tile_suggestion` 把建议的网格再过一遍现有的 `_stardist_safe_grid`。这个函数按 `TileScheduler` 的真实读取窗口检查。
   - 提示行显示的、Auto 应用的，都是检查过的网格。检查用 Auto 将要设置的重叠值，网格超过行列输入框的上限时就把上限调高（`_set_grid` 已有这个做法），提示行里的单块大小按检查后的网格重新算。
   - worker 里的 `_record_tile_strategy`（`workers/segment_merge_worker.py:1619`）只记录建议值，不决定网格，不改。
3. **TMA 按 core 运行**（`utils/step2_units.core_units`）：用运行配置里记录的上限代替 `engine_cap_px`（见第 4 条）。大 core 照现有逻辑拆分。
4. **记录**：
   - 每次 Run 都在 GUI 里重新算上限。加载的旧配置或别的机器的配置里的值一律不用。
   - 上限写进运行配置：`stardist_cap_px`，以及来源 `stardist_cap_source`（vram_gib、use_gpu、卡是怎么查到的）。worker 和 `run_segmentation_params.json` 都能看到。
   - worker 拿到的配置里没有这个字段时（旧路径），按 60 MP 处理。
   - 上限不进引擎 identity，引擎行为不变。

### 2.3 引擎那块卡的显存

`_detect_vram_gb()` 原样保留，Cellpose / Mesmer 的建议在用它，不动。

新增 `Step2Page._engine_vram_gib()`，同样按进程缓存，也不在 GUI 线程 import torch：
- **torch 已加载且 CUDA 可用**：读 `torch.cuda.get_device_properties(0)`。引擎子进程继承同样的环境，它的 `cuda:0` 就是这块卡。
- **否则用 `nvidia-smi` 查**：
  - `CUDA_VISIBLE_DEVICES` 是 UUID：查那张卡。
  - 是数字序号，或者没有设置：查**所有**卡，**取最小的显存**（数字序号是 CUDA 的编号，不一定是 nvidia-smi 的，codex 代码审核 r1）。CUDA 和 nvidia-smi 给卡编号的顺序可能不同，取最小是保守做法；单卡机器（比如这台笔记本）结果一样。
- **查不到**（Mac，或者没有 NVIDIA 卡）：返回 None，上限为 60 MP。

### 2.5 A：省显存的网络写法（`seg_runner/stardist_torch/graph.py`）

运算和原来完全一样，只是同一时刻在显存里的大张量少了：
- 3×3 卷积仍先 `F.pad` 再卷积（b37de93 曾改成 `F.conv2d(padding=1)`，数学上相同，但笔记本验收发现 CUDA 和 Windows CPU 上选了别的内核，不逐位相同，已改回）。全分辨率两层的补边拷贝只有一条那么大；
- ReLU 原地计算（`F.relu_`）；
- U-Net 的 skip 用完就释放（`skips.pop()`）；
- 两层全分辨率卷积（`conv2d_1`、`conv2d_2`）按行分条计算，每条在图像内部上下各多读 2 行（两层 3×3 卷积正好需要 2 行），算完马上做 2×2 池化，只保留池化后的结果。
  - 条高约 512 行，各条高度大致相等、都是偶数，所以和池化的网格对齐。
  - 不能在末尾留下只有十几行的窄条：Mac 实测，MPS 对 18 行高的窄条会选别的内核，最后一位不同。
  - 原来的写法保留为 `graph.forward_reference`，产品不调用它，只给测试和 CUDA 实测脚本做对照。

峰值：从约 388 B/px 降到 192.1 B/px（笔记本实测 2439b9f，按峰值已分配算；b37de93 的隐式补边为 162.5 B/px）。`STARDIST_BYTES_PER_PX = 200` 不变。
注意：预留显存约为已分配的 2.3 倍（原写法约 1.0 倍），见 §2.6。

逐位相同的验证：
- Mac：CPU 和 MPS 上 30 种尺寸（含真实权重、真实 DAPI）全部 `torch.equal`。
- CUDA（笔记本 RTX 3060，cuDNN 9.1，2439b9f）：实测脚本 1024²–3072² 真实权重全部逐位相同；但测试里 1040×80 稳定差 1 ulp（911/51200 个值，最大 5.96e-8）。不分条时相同，所以是 cuDNN 按条的形状选了另一种算法。

用户裁定 2026-11-19（B）：接受。`BEHAVIOR_VERSION["stardist"]` 2→3，hybrid 1→2（hybrid 的任一部分输出变了，它都要变）。Step1 用旧版本做的 StarDist 结果交给 Step2 时，会照常提示"行为已变"。测试：CPU 和 MPS 仍要求 `torch.equal`；CUDA 上要求两个输出都是有限值，并且和原写法的差在 1e-5 以内（错的 halo 或条偏移会差得多）。这只是这台机器上观察到的情况，不是普遍的 1 ulp 保证。

### 2.6 C：缓存与分配器上限（`seg_runner/engines.py`）

- **释放缓存**：只在 hybrid 里，Cellpose 之后、StarDist 之前，调用一次 `torch.cuda.empty_cache()`（只在 CUDA 已初始化时）。它把 torch 缓存着、已经不用的显存还给驱动，好让 cuDNN 为 StarDist 申请工作区。活着的张量和权重不动，结果不变。
  - 2439b9f 曾把它扩展到每次 StarDist 调用（用户裁定 2026-11-18），笔记本实测是负收益：每块都要重新申请并溢出，单独 StarDist 每块网络时间从约 0.7 s 变为 2.3–4.5 s，进程内存峰值从 3.8 GB 升到 6.9 GB，预留显存也没降（6.18–6.73 GiB）。v0.4 撤回。
- **分配器上限**：引擎进程在 CUDA 上创建 StarDist 引擎时，调用一次 `torch.cuda.set_per_process_memory_fraction(0.85)`（`CUDA_MEMORY_FRACTION`）。
  - 原因：省显存写法的预留显存约是已分配的 2.3 倍。在 Windows（WDDM）上，预留超过物理显存时不会报 OOM，而是借用系统内存，慢约 10 倍（实测脚本 3584² 0.40 s，4096² 预留 7.5 GiB 时 3.95 s）。
  - 有了上限，分配器要越过它时，会先把缓存的空闲块还回去再重试（torch 2.5.1 源码，Windows 同样）。只影响何时还缓存，不改任何数值。
  - 0.85 而不是 0.95：CUDA 上下文本身在分配器之外占 300–500 MiB，6 GiB 卡上 0.95 只留约 300 MiB（astra low 审核）。具体值以笔记本实测为准。
  - 作用于整个引擎进程。hybrid 先建 StarDist 引擎，所以 Cellpose 也受同一上限约束，两部分都要验证。引擎只在单独的引擎进程里创建（`seg_runner/runner.py`），GUI 进程不受影响。
  - 不保证"不溢出"和"每块回到 0.7 s"，要实测。仍有可能因活着的张量切出的碎块而 OOM，届时再看实测脚本的碎块统计。
  - 暂不用 `garbage_collection_threshold`（只是补充）、`max_split_size_mb`（可能明显变慢）和 `expandable_segments`（Windows 不支持）。

### 2.7 D：诊断行（`seg_runner/engines.py`、`seg_runner/stardist_torch/network.py`）

每次调用 StarDist，engine 日志里写一行：
- tile 尺寸；
- 网络计算的秒数，以及其余部分（去重、多边形）的秒数；
- 在 CUDA 上时，本次调用的峰值已分配和峰值保留显存（调用前在实际设备上 `reset_peak_memory_stats`）。

只读不改，不清缓存。

### 2.8 不做的事

- 不用 stardist 的 `n_tiles` / `predict_instances_big`（§2.4 范围外，用户裁定）。
- 不用 fp16 / bf16。
- 不改 runner 的文件传输，也不做 CPU 去重和 GPU 计算的流水线并行（两边都评估为收益小或范围大）。
- 不在引擎里捕获 OOM 后重试，引擎行为不变。
- 不改 `suggest_tile_strategy`，也不改 Cellpose 和 Mesmer 的网格建议。
- Mac（MPS，内存与显存共用）不检测显存，仍是 60 MP，和今天一样。

## 3. 用户能看到的变化

- 选 StarDist 或 hybrid、并且勾选 GPU 时，网格会自动加密。提示行显示类似 `StarDist: grid 4×4 → 7×7 (tiles ≤ 5 MP, 6.0 GiB GPU)`。
- 手动改成过大的网格时，Run 前的询问和今天一样，只是上限按显存算。
- 结果：引擎的计算和合并逻辑都不变，但 tile 边界变了，所以结果和用大 tile 时不会每个细胞都完全相同。原因有三：每块按自己的像素做百分位归一化；视野边缘的上下文不同；接缝位置不同。这和今天手动改网格的效果一样。
- tile 更多，总时间可能略增。

## 4. 验证

- **单元测试**（扩展 `tests/test_step2_stardist_tile_cap.py`）：
  - `stardist_cap_px` 在 6 / 11 / 24 / 48 GiB、hybrid 和非 hybrid、CPU、显存未知时的取值；
  - 6 GiB 卡上，4×4 网格（18 MP）会被拆分，拆分后每块读取窗口都不超过上限；
  - Auto 打开时，StarDist 建议网格的读取窗口不超过上限（codex 第 1 轮 12000² / 6 MP 的例子）；
  - `_engine_vram_gib`：UUID、数字序号、多卡取最小、没有 nvidia-smi 四种情况；
  - 每次 Run 都重新算上限，不用配置里的旧值；
  - 切换 GPU 勾选时，上限和网格跟着变；
  - Cellpose 不受影响；
  - 按 core 运行时，拆分用记录下来的上限（`tests/test_step2_core_units.py`）。
- **Mac**：相关 Step2 模块和 HEAD 对比。
- **网络与原写法一致**（`tests/test_stardist_torch_graph.py`）：每台机器上的每个设备（CPU，有就加 CUDA、MPS）、多种尺寸（含 2 条加零头的高度），`graph.forward` 与 `graph.forward_reference` 在 CPU 和 MPS 上 `torch.equal`；CUDA 上两个输出都是有限值，且差在 1e-5 以内（v0.4，§2.5）。
- **CUDA 实测**（`scripts/stardist_vram_probe.py`，在笔记本上运行）：真实权重、真实 DAPI 窗口，1024²–3072² 两种写法比较是否逐位相同；3584²–4800² 只跑新写法，测峰值显存和 B/px，作为定 `STARDIST_BYTES_PER_PX` 的依据。v0.4 起默认和引擎一样设分配器上限（`--no-memory-limit` 不设），并记录碎块峰值（`inactive_split_bytes`）和分配重试次数。
- **Windows 笔记本实测**（定系数）：本地 agent 在同一张 Full WSI 上跑 stardist_nuclei_expansion 和 hybrid。
  - 引擎在 CUDA 上时，每次调用 StarDist 前，在实际设备上执行 `reset_peak_memory_stats`；调用后把 `max_memory_allocated` 和 `max_memory_reserved` 写一行到 engine 日志。只是诊断输出，不清缓存，计算不变。
  - 用实测的 bytes/px 回填常量。
  - 判定标准：两种方法都跑完全图，没有 OOM。
  - v0.4 追加：预留显存低于 6 GiB；单独 StarDist 每块网络时间回到约 1 s 以内；hybrid 的 Cellpose 部分在上限下不变慢、不 OOM。

## 5. 文件白名单

- 本计划文件
- `seg_runner/stardist_torch/graph.py`（A）
- `seg_runner/stardist_torch/network.py`（D：记录网络计算的秒数）
- `scripts/stardist_vram_probe.py`（新，CUDA 实测）
- `tests/test_stardist_torch_graph.py`
- `utils/step2_units.py`
- `ui/step2_page.py`
- `workers/segment_merge_worker.py`：只改两处，core 运行拆分时读取记录的上限，并把上限写进运行参数
- `seg_runner/engines.py`：C 的一行释放和分配器上限，D 的诊断行，`BEHAVIOR_VERSION`（v0.4）
- 固定版本号的测试（v0.4）：`tests/test_stardist_torch_engine.py`、`tests/test_stardist_torch_product.py`、`tests/test_preseg_run.py`、`tests/test_hybrid_cellpose_stardist.py`
- `tests/test_step2_stardist_tile_cap.py`
- `tests/test_step2_core_units.py`
- `docs/user_guide.md`：Step2 tile 一节
- `docs/v17_backlog.md`

## 6. 风险与回退

- **估计偏高**：tile 更小，速度略慢；结果像手动换网格一样有细小差别（§3）。
- **估计偏低**：仍可能 OOM。笔记本实测后修正系数。
- **回退**：`stardist_cap_px` 直接返回 60 MP，就恢复今天的行为。
- **分配器上限过低**：引擎 OOM，或分配反复重试变慢。回退：`CUDA_MEMORY_FRACTION` 调高，或删去 `limit_cuda_memory` 的调用。
