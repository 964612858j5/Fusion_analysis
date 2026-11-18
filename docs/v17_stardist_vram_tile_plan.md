# StarDist 的 tile 大小按显卡显存定（backlog 29）

v0.3，2026-11-18。用户裁定：按显存自动调整；不用 stardist 内部切块（`n_tiles`）。
v0.3（用户裁定 2026-11-18，"同意 A+C+D"，并要求 CUDA 实测逐位相同）：在 v0.2 的上限之外，
- A：网络改为省显存的写法，结果逐位相同（§2.5）；
- C：hybrid 在 StarDist 之前释放 Cellpose 用完的缓存（§2.6）；
- D：每次调用 StarDist 写一行诊断（§2.7）。
v0.2 的显存上限保留，作为最后一道保险，按 A 之后的峰值重算（§2.1）。
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

StarDist 引擎的数值不变：`n_tiles=None`，权重、归一化、阈值、fp32、cuDNN deterministic、不用 TF32 都不动。网络的运算顺序也不变，只是换了更省显存的写法（§2.5），输出必须和原写法逐位相同。Step2 外层网格允许的单块大小按显存定。外层切块、200 px 重叠和接缝合并都是现有功能，`docs/stardist_torch_plan.md` §2.4 的保证范围不变。

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
- 3×3 卷积的补零放进 `F.conv2d(padding=1)`，不再用 `F.pad` 另拷贝一份；
- ReLU 原地计算（`F.relu_`）；
- U-Net 的 skip 用完就释放（`skips.pop()`）；
- 两层全分辨率卷积（`conv2d_1`、`conv2d_2`）按行分条计算，每条在图像内部上下各多读 2 行（两层 3×3 卷积正好需要 2 行），算完马上做 2×2 池化，只保留池化后的结果。
  - 条高约 512 行，各条高度大致相等、都是偶数，所以和池化的网格对齐。
  - 不能在末尾留下只有十几行的窄条：Mac 实测，MPS 对 18 行高的窄条会选别的内核，最后一位不同。
  - 原来的写法保留为 `graph.forward_reference`，产品不调用它，只给测试和 CUDA 实测脚本做对照。

峰值估计（codex 与 Fable 的推算）：从约 388 B/px 降到约 165–200 B/px（瓶颈移到 `features` 层和输出头）。

逐位相同的验证：
- Mac：CPU 和 MPS 上 30 种尺寸（含真实权重、真实 DAPI）全部 `torch.equal`。
- CUDA：§4 的实测脚本。

如果 CUDA 上不逐位相同，要先停下，由用户裁定：是只差浮点舍入，还是 `BEHAVIOR_VERSION["stardist"]` 要加 1（`seg_runner/engines.py:87`；Mesmer 改用 torch 时就因为舍入差异加过 1）。逐位相同则 identity 不变，Step1 的结果照常可以交给 Step2。

### 2.6 C：hybrid 在 StarDist 之前释放 Cellpose 的缓存（`seg_runner/engines.py`）

- hybrid 跑完 Cellpose、开始 StarDist 之前，调用一次 `torch.cuda.empty_cache()`，只在 CUDA 已经初始化时调用。
- 它只把 torch 缓存着、已经不用的显存还给驱动，好让 cuDNN 为 StarDist 申请工作区（hybrid 那次 "unable to find an engine" 报错很可能就是这里不够）。
- 活着的张量和权重都不动，结果不变。

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
- **网络逐位相同**（`tests/test_stardist_torch_graph.py`）：每台机器上的每个设备（CPU，有就加 CUDA、MPS）、多种尺寸（含 2 条加零头的高度），`graph.forward` 与 `graph.forward_reference` 输出 `torch.equal`。
- **CUDA 实测**（`scripts/stardist_vram_probe.py`，在笔记本上运行）：真实权重、真实 DAPI 窗口，1024²–3072² 两种写法比较是否逐位相同；3584²–4800² 只跑新写法，测峰值显存和 B/px，作为定 `STARDIST_BYTES_PER_PX` 的依据。
- **Windows 笔记本实测**（定系数）：本地 agent 在同一张 Full WSI 上跑 stardist_nuclei_expansion 和 hybrid。
  - 引擎在 CUDA 上时，每次调用 StarDist 前，在实际设备上执行 `reset_peak_memory_stats`；调用后把 `max_memory_allocated` 和 `max_memory_reserved` 写一行到 engine 日志。只是诊断输出，不清缓存，计算不变。
  - 用实测的 bytes/px 回填常量。
  - 判定标准：两种方法都跑完全图，没有 OOM。

## 5. 文件白名单

- 本计划文件
- `seg_runner/stardist_torch/graph.py`（A）
- `seg_runner/stardist_torch/network.py`（D：记录网络计算的秒数）
- `scripts/stardist_vram_probe.py`（新，CUDA 实测）
- `tests/test_stardist_torch_graph.py`
- `utils/step2_units.py`
- `ui/step2_page.py`
- `workers/segment_merge_worker.py`：只改两处，core 运行拆分时读取记录的上限，并把上限写进运行参数
- `seg_runner/engines.py`：C 的一行释放，以及 D 的诊断行
- `tests/test_step2_stardist_tile_cap.py`
- `tests/test_step2_core_units.py`
- `docs/user_guide.md`：Step2 tile 一节
- `docs/v17_backlog.md`

## 6. 风险与回退

- **估计偏高**：tile 更小，速度略慢；结果像手动换网格一样有细小差别（§3）。
- **估计偏低**：仍可能 OOM。笔记本实测后修正系数。
- **回退**：`stardist_cap_px` 直接返回 60 MP，就恢复今天的行为。
