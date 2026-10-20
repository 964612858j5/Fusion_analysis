# F5：用 Rust 生成 fused.zarr（方案 v1.3）

状态：用户 2026-10-19 已对 7 个问题作出裁定（§1）。本文件是实施方案，**不代表实施已获授权**；各段开始前按 AGENTS.md 由用户启动。

依据：2026-10-19 的调研和一次性原型（独立 worktree `/sda1/Fusion/rust_fuse_proto`，crate `native/fusion_fuse_proto`，未提交、未安装）。原型证明了按位一致和速度，见 §2。

## 0. 目标与范围

- **目标**：Step1 "生成 fused.zarr" 的像素与今天的 Python 结果**按位一致**，整片明显更快，特别是融合通道多的配置（例如 20 个通道）。
- **范围内**：
  1. 每个计算单元（`_compute_unit`）的读取、解码、映射、融合、多边形裁切交给 Rust；
  2. zarr 分块改为并行写入；
  3. 原始切片（raw）和 Step0 校正产物（corrected，float32 zarr）两种通道来源都走 Rust。
- **范围外（不变）**：
  - 公式本身（`FUSION_FORMULA_VERSION = 2`，`core/fusion_engine.py:25`）；
  - 输出布局与 attrs（`channel_0/1`、`cellpose_channels`、`roi_name`、`bbox_fullres`、`created_at`、`fusion_formula_version`、`artifact_kind`、`config_hash`、`complete`）、分块 `(1024, 1024, 2)`、Blosc lz4 压缩；
  - `.inprogress.<uuid>` 临时库、发布与恢复（`_publish_store` / `_recover_interrupted_publish`）、清理旧残留、`fusion_meta.json`、`roi_config.json`、来源记录（provenance）、细胞核概览图（nucleus overview）；
  - Step1 预分割的 `fused_window`（`core/preseg_input.py:104`，经 `FusionEngine.fuse_fullres`）仍用 Python。它和 fused.zarr 的一致性由 §7 的公式对拍保证；
  - QThread、进度信号、Stop 按钮、`main_window` 的启动和回调（`ui/main_window.py:10480`、`:10801`）。

## 1. 用户裁定（2026-10-19）

1. 第一期 = Rust 计算单元 **加** 并行写 zarr 分块。像素不变，写入顺序可以变；概览图和进度仍按顺序。
2. 一致性的标准 = Rust / 本机（无 AVX512）NumPy 的结果。Rust 在任何机器上结果都相同。AVX512 机器上 Python 自己会有 ±1 的差异，写进文档即可（§10 第 1 条）。
3. 校正通道（float32 zarr）的 Rust 路径本期就做。
4. Rust 在运行中途读取或解码失败，**整个运行失败**，和 Python 一样，不悄悄改用 Python 重算。只有在开始时建不起 Rust 任务的情况下，才按区域退回 Python，并写日志（§6）。
5. 不做 Python 查表提速。
6. 代码放进 `native/fusion_viewer_core`，按小的单一用途模块拆分。重装共享 wheel 要事先通知，用户在用软件时不装。
7. Rust 不读没有显示窗口（display window）的通道（今天的 Python 会读，但不使用）。

## 2. 实测基线（Slice1：58320×28800，57 通道，uint8，LZW，512² 块；设置 = DAPI + 一组 5 个标记；96 核机器）

### 2.1 时间花在哪里
- **条带**（行 8192–16384 × 列 0–16384，1.34 亿像素，64 个单元），安静的机器，16 个工作线程：
  - Python：墙钟 3.0 s。线程时间：读取+解码 10.9 s，融合 18.2 s，单元合计 29.1 s，zarr 写入 1.1 s；
  - Rust 原型（只换 `_compute_unit`，其余是原 worker）：墙钟 1.5–1.6 s（含 0.28 s 建 manifest），单元线程时间 8.4 s；
  - Rust 只计算、不写盘：0.27–0.7 s。
- **单个单元**（1024×2048，6 通道，单线程）：
  - Python：读取+解码 57 ms；6 次 `apply_channel_remap` 共 96 ms；每通道约 15 遍整数组运算（`core/channel_remap.py:95-147`）。
  - Rust：读取+LZW 解码 39 ms；整个单元 51 ms。解码约占 77%，融合本身约 12 ms。
- **写 zarr**（Blosc lz4，clevel 5，shuffle）：
  - 单线程约 500 MB/s，每个条带约 1.1 s；QThread 里不比主线程慢；
  - 4 个线程 0.30 s，8 个线程 0.17 s（不同分块并行写）；
  - 整片未压缩 6.7 GB，单个写线程约需 **13–14 s**。所以计算变快以后，单线程写盘就成了下限，必须并行写。
- **冷缓存的磁盘**：
  - 该 QPTIFF 每 1024 行条带里，每个通道连续存放 2 行图块（约 4.4–7 MB），57 个通道交错排列；
  - 6 个融合通道在第 0 层压缩后共 1.57 GB；
  - HDD（/sdb1，ST18000）用 O_DIRECT 测：今天这种按图块读、16 线程，110 MB/s，整片约 15 s；每通道每条带一次连续读，185–222 MB/s，约 12–13 s；
  - NVMe：1.57 GB 不到 1 s。

### 2.2 整片的上限与预期（暖缓存；"今天"一列是实测，其余是按条带推算）

| 配置 | 今天 Python | Rust + 单线程写 | Rust + 并行写 | 理论下限 |
|---|---|---|---|---|
| 6 通道（当前设置） | 35–38 s | 约 19 s | 约 5–8 s | 约 4–5 s（解码约 33 线程秒 / 16 ≈ 2 s，加压缩约 1.7 s） |
| 21 通道（重配置） | 约 40–80 s* | — | 约 10–12 s | — |

- *重配置整片逐单元对比（8 线程，不写盘）的线程时间：Python 646 s，Rust 80 s（约 8 倍）。
- 冷 HDD：受磁盘限制，6 通道约 13–16 s，20 通道约 25–50 s。冷 NVMe：几乎不受磁盘限制。

### 2.3 原型的一致性结果
- 合成金标准（golden）测试：
  - 2880 组映射参数，按位 f32 全部一致（含 gamma 1/0.5/2/0.7/1.3/2.2/0/−1/1e-50、对比度、亮度、min/max 为空、NaN/inf）；
  - 300 个随机融合方案全部一致。
- 条带经完整 worker 写出：0 / 268,435,456 个值不同，attrs（除 `created_at`）相同，概览图相同。
- 整片逐单元对比（855 个单元）：
  - 当前设置（6 通道）：0 差异；
  - 重配置（21 通道：两组、组权重 0.8/1.0、成员权重 0.33–1 和 0、细胞核权重 0.9、gamma 0.5/0.7/1.5/2.0、对比度 0.8/1.3、亮度 ±、一个开放窗口、一个无窗口通道、多边形裁切）：0 差异。

## 3. 必须逐位复现的算术（Rust 的规格）

`apply_channel_remap`（`core/channel_remap.py:67`）在 NumPy 1.26 下，逐步取整到 f32，不用 FMA：

1. `x = f32(raw)`；非有限值（NaN、inf）最后一律为 0。
2. 窗口：`min`、`max` 为 Python float；分母 `max - min` 先按 f64 算再转 f32；`max - min <= 0` 时整块为 0。
   - `min` 或 `max` 为空：用**本单元**有限像素的最小值和最大值（和 Python 按块计算相同）；没有有限像素时用 0 / 1。
3. `x = clip((x - f32(min)) / f32(denom), 0, 1)`
4. `x = clip(((x - 0.5) * f32(contrast) + 0.5) + f32(brightness), 0, 1)`。c=1、b=0 时**不是**恒等变换，必须照做。
5. `gamma <= 0` 时改为 1e-6；指数 `e = f32(gamma)`。照 NumPy 1.26 `fast_scalar_power` 的规则：
   - e == 1 → 原值；
   - 0.5 → `sqrt`；
   - 2 → `x*x`；
   - 0 → 1（gamma 极小、转 f32 后下溢时才会出现）；
   - 其他 → `powf`。**不用 Rust 的 `f32::powf`**：它调用平台的数学库，Linux 上是 glibc，Windows 上是 MSVC 的 CRT，各平台结果不保证相同，满足不了裁定 2。改为新模块 `fuse_powf.rs`：一个纯 Rust 的 `powf`，不依赖平台的数学库：
     - **代码来源（需用户批准，见 §12 第 1 条）**：把 ARM optimized-routines（固定到一个具体的上游提交）的 `powf.c` 及其常数表（`powf_log2_data.c`、`exp2f_data.c`）翻译成 Rust。glibc ≥ 2.28 的 `powf` 就来自这份代码，但 glibc 的副本是 LGPL，**不从 glibc 抄**，只从 ARM 上游取。ARM 上游的许可是 `MIT OR Apache-2.0 WITH LLVM-exception`，选用 MIT；
     - 翻译后的文件头保留 ARM 的版权声明和 MIT 许可全文，写明上游提交号和对应的原文件；另建 `native/fusion_viewer_core/THIRD_PARTY_NOTICES.md` 放同样的声明，在 `pyproject.toml` 里让它随 wheel 发布；README 的依赖与许可表加一行。这和 README 里 Odon "只作设计参考、不抄代码" 的情况不同，要单独写清楚；
     - **计算方式要固定**：本机 glibc 在有 FMA 的 CPU 上选用 FMA 版本（`__powf_fma`），编译器把一些 `a*b+c` 合成了一次取整的 FMA。Rust 版本先在可能被合并的位置用 `f64::mul_add`（FMA 结果在任何机器上都相同，因为它只取整一次），再用全量对比确认与本机 glibc 一致；如果不一致，就逐处调整哪些位置用 `mul_add`，直到全量一致，然后把这个选择写进代码注释。CPU 有 FMA 时用运行时检测走硬件指令，没有时 `mul_add` 走软件实现，结果相同，只是慢一些；
   - F5-1 验收：与本机 glibc `powf`（NumPy 1.26 在本机调用的就是它）对比，x 取 [0, 1] 内的**全部** f32 值，指数取常用值（0.3–3.0 每隔 0.1，加上随机 200 个），要求按位全同；另在 Windows 目标上做同样的检查（只比 Rust 自己在两个平台上的结果）；
   - 然后 `clip(0, 1)`。
6. **NaN 规则**（照 NumPy 1.26）：
   - 输入是有限值时，运算中间仍可能产生 NaN，例如：
     - `max - min` 在 f64 下 > 0、转成 f32 后下溢为 0，得到 `0/0`；
     - `min = -1e300` 转成 f32 是 `-inf`，分母也溢出为 `inf`，得到 `inf/inf`；
     - 其他 IEEE 规则产生 NaN 的情况。
   - 每一步都按 IEEE 和 NumPy 的规则处理，不提前把 NaN 变成 0：
     - 减法、除法、乘法、加法照常；
     - `np.clip` 保留 NaN；
     - 幂运算照 §3 第 5 步：指数为 0 时 `NaN**0 = 1`，指数为 1 时原样（NaN），`sqrt` 和 `x*x` 得到 NaN，`powf(NaN, e)` 得到 NaN；
     - 最后的 `where(finite, x, 0)` 只把**输入**是非有限值的像素置 0，不管中间产生的 NaN；
     - 融合时 `accum += NaN` 得到 NaN，`np.clip` 保留，`np.maximum` 也保留 NaN；
     - 量化：`astype(uint16)` 把 NaN 变成 0（本机 x86-64 实测）。
   - 所以 Rust 的 clip 和取最大值都要保留 NaN（`x.is_nan()` 时原样返回），量化时 NaN 记为 0。
   - 金标准里加上这些例子（本机 NumPy 1.26.4 的结果）：
     - 输入 0、`min=0`、`max=1e-50`、`brightness=0.1`、`gamma=1` → NaN，量化后为 0。原型在这里会给出约 6553，是错的；
     - 同上但 `gamma=1e-50` → 指数下溢为 0，结果为 1.0；
     - 输入 0、`min=-1e300`、`max=0` → NaN；
     - 再各加一个融合层面的例子：NaN 通道和正常通道在同一组、在不同组。

`fuse_channels`（`core/fusion_engine.py:28`）加量化（`_fuse_tile`，`ui/step0/overview_panel.py:457`）：
- 成员只在"有已提交的窗口且 w > 0"时参与；
- 权重先按 f64 裁到 [0, 1]，再作为 f32 参与乘法；
- 组权重缺省为 1；
- 每组：`acc = 0；acc += f32(signal * w)`（按配置里的顺序）；然后 `acc = clip(acc * gw)`，`cyto = max(cyto, acc)`；最后再 `clip` 一次；
- 细胞核：`clip(signal_n * w_n)`；
- 输出：`u16(cyto * 65535)`、`u16(nucleus * 65535)`，截断取整；
- 一个能用的通道都没有时，整块为 0。

**查表**：对 u8 和 u16 输入，"映射后再乘权重"按每个可能的值预先算一张 256 或 65536 项的表。这是精确的，因为逐元素运算同一输入在哪个位置都得到同一 f32。f32 输入（校正通道、浮点原始图）逐像素计算。

## 4. Rust 模块（放进 `native/fusion_viewer_core/src/`，每个模块只做一件事）

| 模块 | 职责 |
|---|---|
| `fuse_window.rs` | 一个通道的窗口参数，以及对单个值的映射（§3 第 1–6 步）；开放窗口按单元求范围 |
| `fuse_powf.rs` | 与平台无关的 f32 `powf`（§3 第 5 步），只此一个函数及其常数表（ARM 上游，MIT，保留声明） |
| `fuse_lut.rs` | 整数输入的"窗口 × 权重"表 |
| `fuse_plan.rs` | 融合方案：通道槽位（来源 + 窗口）、各组成员和权重、细胞核；§3 的权重规则只在这里应用一次 |
| `fuse_kernel.rs` | 一段像素（约 8192 个）的融合与量化，输出 (cyto, nucleus) 交错的 u16 |
| `fuse_source.rs` | 一个通道在一个单元上的样本：raw 经 `reader::RawSource::read_region`（u8/u16/i16/u32/i32/f32）；corrected 经 `reduce::CorrectedRegion::read`（不带缓存）。要求覆盖整个单元，否则报错（同 `_read_one_source`，`overview_panel.py:350`） |
| `fuse_unit.rs` | 一个单元：读出所有参与的通道（各读一次），逐段调用 kernel，应用多边形掩膜；读取之间检查取消标志 |
| `py/fuse.rs` | PyO3 绑定，只做字典与类型转换，不含逻辑 |

- 复用现有模块：`decode.rs`（含 `lzw_exact`，可读出带尾随字节的 45 个 LZW 块）、`reader.rs`、`manifest.rs`、`zarr_v2.rs`、`blosc.rs`、`reduce.rs`、`cancel.rs`、`error.rs`。
- 不新增 crate 依赖：线程用 Python 现有的池（§5），不引入 rayon。
- 不改查看器已有模块的行为。

## 5. PyO3 接口与 Python 接入

### 5.1 接口（新类 `fusion_viewer_core.FuseJob`）
```
FuseJob(raw_manifest: dict, plan: dict)
  plan = {"channels": [{"name": str,
                        "source": {"raw": index} | {"corrected": <_array_spec 字典>, "bbox": [y0,y1,x0,x1]},
                        "window": {min,max,brightness,contrast,gamma}}],   # 只放有窗口的通道（裁定 7）
          "groups":  [(group_weight, [(slot, weight), ...]), ...],          # 按配置顺序
          "nucleus": (slot, weight) | None}
  .fuse_unit(y0, y1, x0, x1, mask: bytes | None) -> bytes | None
        # h*w*2 个 u16（小端），与 zarr 分块同布局；释放 GIL；取消后返回 None
  .cancel()
fuse_remap_values(window, f32 bytes) -> f32 bytes          # 金标准测试用
fuse_values(plan, [f32 bytes per slot]) -> u16 bytes       # 金标准测试用
```
- 建立时就检查文件身份：raw 用 `RawSource::open`；corrected 用 `corrected::verify`，即 `.zarray`/`.zattrs` 与 Python 看到的一致。
- 布局不支持（`ArrayMeta::new` 拒绝的压缩、dtype、分隔符等）时，构造就失败，交给 §6 处理。
- **shuffle 预检**：`_array_spec`（`ui/step1_core_adapter.py:221`）没有带上 Blosc 的 `shuffle`，`ArrayMeta::new`（`zarr_v2.rs:36`）也不检查它，而 `blosc.rs:81` 解码时才拒绝 bitshuffle。这样一来，一个 Python 能读的校正产物会在运行中途失败，而不是在开始时退回 Python。所以 FullFusionWorker 建 job 前自己读 `.zarray` 的 `compressor.shuffle`，只接受 0（不 shuffle）和 1（按字节 shuffle），否则按 §6 在开始时退回 Python。这一步只改 `overview_panel.py`，不改 `step1_core_adapter.py`。
- 返回 `PyBytes`，多一次约 8 MB 的拷贝（约 1 ms），换来接口简单；Python 用 `np.frombuffer` 得到只读数组，写盘和概览图都只读它。

### 5.2 Python 接入点（`ui/step0/overview_panel.py`，行号以 de84279 为准）
1. `run()`（`:577`）里每个区域：在 `_region_sources`（`:360`，校正通道的"严格匹配、不回退 raw"检查保持不变）之后，在锁内**只建一次** `FuseJob`：
   - raw manifest 来自 `step1_core_adapter.build_raw_manifest(raw_source._provider)`（`ui/step1_core_adapter.py:153`）；
   - 校正通道的说明来自 `_array_spec`（`:196`），bbox 来自 `CorrectedZarrSource` 的有效范围；
   - 原型里 16 个线程同时建，浪费了 2.5 s，所以必须在锁内建一次。
2. `_compute_unit`（`:526`）：有 job 时调用 `job.fuse_unit`，多边形掩膜由 `_unpack_part` 转成 bytes 传入；没有 job 时走现有 Python 代码（不改）。
3. `stop()`（`:334`）：在原来的 `_stop = True` 之外，调用当前 job 的 `cancel()`。
4. `_unit_workers`（`:515`）：内存估算按引擎分开（§8）。
5. 写盘循环（`:729-750`）：改成 §5.3 的并行写。

### 5.3 并行写（第一期就做，裁定 1）
- **计算**：仍用 `bg_parallel.ordered_results`（`core/bg_parallel.py:82`），结果按单元顺序回到 QThread。
- **写入**：QThread 收到一个单元后：
  1. 立即按顺序做 `overview.add`（`:738`）；
  2. 把 `out_zarr[...] = fused` 交给一个**写线程池**（至多 8 个线程，且不超过 `choose_workers` 给出的数目）；
  3. 已提交、尚未写完的写入至多 N 个（建议 N = 写线程数），满了就等最早的那一个。
- **进度**：按顺序报告。"第 i 个单元已写完"只在它和它之前的单元都写完时发出（等队首）。
- **为什么安全**：
  - 单元按区域原点对齐到分块网格（1 块高 × 2 块宽，`_units`），所以每个分块只属于一个单元，任何两个单元不会写同一个分块。安全靠的是这一点；
  - 区域边缘被截短的分块，zarr 仍可能走"不完整分块"的处理（`zarr/core.py:2273` 按完整分块形状判断），但这个分块只有一个单元在写，所以不会互相覆盖；
  - zarr 2.18 的 `DirectoryStore.__setitem__` 先写 `*.partial` 临时文件，再 `os.replace`，不同分块并行写互不干扰；
  - numcodecs 的 Blosc 压缩会释放 GIL。
- **完成与发布**：所有写入完成并检查过异常之后，才写 `attrs["complete"] = True`、关闭并发布（`:763-767`）。任何一个写入出错，整个运行失败（同今天），临时库照常删除。
- **Stop / 出错 / 退出**：
  - 停止接收新单元（`ordered_results` 本来就会取消未开始的单元）；
  - 正在计算的单元等它算完（`ordered_results` 退出时本来就会等，`core/bg_parallel.py:130`）；
  - 已经在写的写入**等它写完**（写线程池 `shutdown(wait=True)`）；
  - 两个池都结束后，才进入 `finally` 删除 `.inprogress.<uuid>`（`:822`）。保证没有计算线程或写线程在临时库删除之后还在动它。
  - Stop 的等待时间 = 正在进行的读取、计算、写入中最慢的那一个：
    - Rust 只在两个通道之间检查取消（`RawSource::read_region` 内部不检查，`reader.rs:76`），一个通道的读取最多约 10–30 ms，一个单元最多约 50–200 ms；
    - 退回 Python 时，一个单元可能要 0.5 s 以上；
    - 写入每个约 8 MB，几十毫秒。
    - 实际数字在 §7.3 实测并记录。
- 并行写不依赖 Rust：没有 Rust 的回退路径（Python 计算）也用同样的并行写。像素相同，写入更快。

## 6. 回退规则（裁定 4）
- **开始时，按区域判断**：下列情况这个区域用 Python 计算，并打印一行日志（原因 + 区域名）：
  - `fusion_viewer_core` 导入失败，或没有 `FuseJob`（旧 wheel）；
  - manifest 建不起来（压缩方式、预测器、dtype 不支持，文件身份不符）；
  - 校正产物的布局不在 Rust 支持范围内（`ArrayMeta::new` 拒绝，或 §5.1 的 shuffle 预检不通过）。
- 每个区域实际用了哪种计算方式记在 worker 的一个普通属性上（例如 `engine_by_region`），并写进日志。测试靠它确认"这次确实走了 Rust"。不写进 zarr attrs，也不写进 `fusion_meta.json`。
- **运行中**：Rust 读取或解码失败，就抛出异常，运行失败（错误信息带通道、单元、原因），和 Python 遇到坏块时一样。不改用 Python 重算。
- 不新增配置开关和状态机。为了测试，可以设一个环境变量强制走 Python（只用于测试和对拍，用户界面上不出现）。

## 7. 一致性与测试

### 7.1 Rust 单元测试（`cargo test --lib`）
- `fuse_window`：边界值（0、1、min、max、负数、NaN、±inf、-0.0）；每种 gamma 快速路径；分母为 0、负、下溢（含 §3 第 6 步的 NaN 例子）；开放窗口。
- `fuse_powf`：与本机 glibc `powf` 的全量对比（§3 第 5 步），作为一个较慢的测试单独运行。
- `fuse_kernel`：空组、权重为 0 或负、组权重 0 或 >1、无细胞核、同一通道在两个组里或同时作细胞核、某一通道为 NaN 时的取最大值和量化。

### 7.2 Python 金标准测试（新文件 `tests/test_f5_rust_fusion.py`）
跳过规则分两档：
- 算术测试（第 1、2 项）只要求有 `fuse_remap_values` / `fuse_values`；
- 任务测试（第 3–8 项）要求有 `FuseJob`；
- 每一步验收时，这一步应有的绑定必须存在，**不能有意外的跳过**（验收命令带 `-rs`，检查跳过清单）。

1. 映射：上面那几千组参数 × (0..65535 + 随机浮点 + 特殊值 + §3 第 6 步的 NaN 例子)，与 `apply_channel_remap` 比较 f32 位模式。
2. 融合：随机方案与 `fuse_channels` + 量化逐值比较，包括会产生 NaN 的窗口。
3. **合成项目**端到端，都写在临时目录：
   - 合成 OME-TIFF：u8 / u16 / i16 / u32 / i32 / f32；LZW / deflate / 无压缩；有无预测器；
   - 合成校正 zarr：含 NaN 和缺失的分块；
   - raw 和校正通道混在一个方案里；
   - 每次各跑一遍 Rust 和 Python（强制），zarr 像素、attrs（除 `created_at`）、`fusion_meta.json`（除时间）和概览图完全相同；并用 `engine_by_region` 确认 Rust 那一遍确实用了 Rust；
   - 覆盖：多边形；多 ROI；区域原点不在分块网格上；区域边缘的窄单元；无窗口通道；开放窗口。
4. Stop：运行到一半 Stop，临时库被删除，正式路径保持旧结果，没有残留的计算线程或写线程。
5. 读取失败：合成文件里放一个坏块，Rust 路径运行失败、报出原因，不发布。
6. 写入失败：让某一个单元的写入抛出异常（测试里替换该写入调用），检查：错误传到界面信号、不发布、两个池都结束后才删临时库。
7. 回退：模拟没有 `FuseJob`；用 bitshuffle 压缩的合成校正产物。两种情况都在开始时退回 Python，结果和 Rust 相同，日志里有一行说明。
8. 现有测试保持通过：`tests/test_v16_pa_fusion_parity.py`（切分不改变像素）、`tests/test_step1_fusion_*.py`。

### 7.3 真实切片验收（只读，输出写到临时目录）
- Slice1 整片逐单元对比（不写盘）：当前设置 + 重配置，要求 0 差异。这个对比写成 `tests/test_f5_rust_fusion.py` 里的一个测试，只在设置了指向切片的环境变量时运行（平时跳过），不另建 `tools/` 脚本。
- 用户真机：在软件里点 Generate（整片），和事先用 Python 生成到临时目录的 fused.zarr 比对：0 差异，attrs 和概览图一致。
- 计时（按部署目标）：服务器 HDD 冷 / 暖、SSD 冷 / 暖；6 通道和约 20 通道；记录墙钟、峰值内存、Stop 的响应时间。

## 8. 内存估算的变化
- 今天：每个单元 `px × (40 + 8 × 通道数)` 字节（`UNIT_BYTES_PER_PX = 40`，`:280`）。6 通道约 176 MB。
- Rust：按阶段算**峰值**，同一时刻存在的内存才相加。记 `px` 为单元像素数（2M），`B` 为一个源块的解码后字节数（raw：图块，例如 512² × 样本字节；校正：分块 1024² × 4 = 4 MB）：
  1. 读取阶段：已读完的通道缓冲 + 正在读的通道缓冲 + 一个块的压缩数据和解码数据（≤ 2B，校正通道再加压缩帧）；
  2. 融合阶段：所有通道缓冲 + 输出 `px × 4` + 段缓冲和查表（约 0.1 MB；u16 表每个"通道 × 权重"256 KB）；
  3. 交回阶段：输出 Vec 与 `PyBytes` 同时存在（`2 × px × 4`），多边形掩膜 `px` 字节加上 Python 解包时的临时数组（约 `2 × px`）。
  - 每个单元的峰值 ≈ `px × (Σ 样本字节 + 8 + 3) + 2B`：
    - 6 个 u8 通道约 35 MB；
    - 20 个 u8 通道约 63 MB；
    - 20 个校正 f32 通道约 190 MB。
- 写入：每个进行中的写入约 24 MB（输入 8 MB + zarr 拼出的两个连续分块 8 MB + 压缩后的缓冲约 8 MB，`zarr/core.py:2204`），写队列另有已提交未写的 `N × 8 MB`。
- 选择并发数时，先从可用内存里扣掉写入部分（`写线程数 × 24 MB + N × 8 MB`），再用 `choose_workers` 给计算池定数（`core/bg_parallel.py:59` 的规则：≤ CPU/2，≤ 16，留 1 GB，不变）。
- `_unit_workers` 按实际引擎选择估算公式。F5-3 / F5-5 时实测峰值内存，与估算比较。

## 9. 实施步骤（每步交 codex 审核；回归按规则：每步只跑相关测试，最后一步跑全量）

| 步骤 | 内容 | 验收 |
|---|---|---|
| F5-1 | `fuse_powf`、`fuse_window`、`fuse_lut`、`fuse_plan`、`fuse_kernel` 及 Rust 单元测试；测试用绑定 `fuse_remap_values` / `fuse_values` | `cargo test` 通过（含 `powf` 全量对比和 Windows 目标检查）；7.2 第 1、2 项全部按位一致，没有跳过（wheel 构建到临时目录，用 PYTHONPATH 加载，不覆盖已安装的） |
| F5-2 | `fuse_source`、`fuse_unit`、`py/fuse.rs`（`FuseJob`） | Slice1 整片逐单元对比 0 差异（两种配置）；合成校正 zarr 对比 0 差异；单元耗时与原型相当 |
| F5-3 | Python 接入：建 job、shuffle 预检、`_compute_unit`、`stop`、内存估算、`engine_by_region`、回退日志 | 7.2 第 3、4、5、7、8 项（Stop 用现有写法）；条带墙钟 ≤ 原型 |
| F5-4 | 并行写（§5.3） | 7.2 第 3、4 项在并行写下重跑，加上第 6 项（写入失败）；条带写盘时间按线程数下降；Stop 测试无残留 |
| F5-5 | 事先通知用户，然后重装 wheel；真机验收 | 7.3 全部；全量回归和基线（16 个已知失败）比较 |

文件白名单（草案）：
- `native/fusion_viewer_core/src/` 新增上述 7 个模块与 `py/fuse.rs`，并在 `lib.rs`、`py/mod.rs` 登记；`native/fusion_viewer_core/README.md` 的依赖与许可表加一行；新建 `native/fusion_viewer_core/THIRD_PARTY_NOTICES.md`，并改 `native/fusion_viewer_core/pyproject.toml` 让它随 wheel 发布（§3 第 5 步）；
- `ui/step0/overview_panel.py`（只改 FullFusionWorker）；
- 新建 `tests/test_f5_rust_fusion.py`；
- 本文件。

`ui/step1_core_adapter.py` 只调用、不修改（如需导出 `_array_spec` 的公开名，另行说明）。

## 10. 风险
1. **Python 的结果本身随 CPU 而变**：在有 AVX512 的机器上，NumPy 1.26 的 float32 `power` 用 SVML，gamma 不是 1、0.5、2 时与 glibc `powf` 可能差 1 ulp，量化后偶尔差 1 级（u16）。按裁定 2，以本机 / Rust 为准。Rust 用自带的 `fuse_powf`（§3 第 5 步），所以在所有机器上相同。
2. **NumPy 升级**（NEP 50、`fast_scalar_power` 的改动）可能让 Python 偏离 Rust。§7.2 的金标准测试会发现；`fused_window`（Step1）仍是 Python，与 fused.zarr 的一致性依赖这组测试。
3. **线程竞争**：原型 16 线程时每单元线程时间从 59 ms 涨到约 170 ms（大块内存的分配与释放）。实施时每个工作线程复用缓冲区。
4. **冷 HDD 受磁盘限制**：今天这种访问方式 110 MB/s，按条带连续读可到约 200 MB/s。本期不做读取合并（可作为后续单独的块）。
5. **共享 wheel**：查看器和 Step1 也用同一个 `fusion_viewer_core`。重装前通知用户；`FuseJob` 是新增类，不改已有接口。
6. **裁定 7 的副作用**：没有窗口的通道如果文件坏了，Python 会报错，Rust 不读也就不报。像素不变。
7. **校正通道**：Rust 只支持 `ArrayMeta::new` 接受的布局（`<f4`、C 顺序、无 filter、blosc-lz4、`.` 分隔），加上 shuffle 为 0 或 1（§5.1 预检）。其他布局按 §6 退回 Python。
8. **`fuse_powf` 是自己写的数学函数**：靠全量对比（§3 第 5 步）保证与 glibc 一致。将来本机 glibc 的 `powf` 换了算法，NumPy 会跟着变，Rust 不会；§7.2 第 1 项会发现这种偏离。

## 11. 工作量估计
- F5-1：1.5 天（含 `fuse_powf` 和全量对比）；F5-2：1.5 天（含校正通道）；F5-3：1 天；F5-4：1 天（含写入失败测试）；F5-5：0.5 天，另加真机时间。
- 合计约 5.5 个工作日。

## 12. 需要用户决定的事（F5-1 开始前）
1. **`fuse_powf` 的来源**：要做到裁定 2（任何机器结果都相同，且与本机 NumPy 一致），需要一个与 glibc 算法相同的 `powf`。两种做法都等于引入第三方代码，按项目规则要用户批准：
   - A（建议）：把 ARM optimized-routines 的 `powf` 及常数表翻译成 Rust 放进本仓库，选 MIT 许可，保留版权和许可声明（§3 第 5 步）。约 200 行，没有新的 crate；
   - B：加 crate 依赖 `libm`（使用许可为 MIT）。**但它的 `powf` 不是 ARM / glibc 的算法**，而是较早的 FreeBSD / Sun 算法，用 f32 运算（`libm/src/math/powf.rs`），所以不能指望它与本机 glibc 一致。只能作为未经验证的候选：要固定版本，并先用全量对比证明与本机 glibc 一致；做不到就要自己维护一个分支或换实现，这时它就不比 A 省事。
   - 两种都不批准时，只能用平台的 `powf`：Linux 上结果与本机 NumPy 相同，Windows 上不保证，这就与裁定 2 冲突，需要用户另行裁定。

## 12a. 请审核者重点检查
1. §3 的算术描述是否和代码完全一致（尤其是 `fast_scalar_power`、f64 分母、权重的裁剪与 w > 0 判断、开放窗口按单元计算）。
2. §5.3 并行写的安全性：分块不共用、zarr 写入的原子性、Stop 和 `finally` 的先后、进度顺序。
3. §6 的回退边界是否符合裁定 4。
4. 内存估算、白名单是否有遗漏。

## 13. 修订记录
- **v1.1**（codex 第 1 轮 `codex_f5_plan_1.out`：APPROVE WITH CHANGES，无 P0，3 个 P1，4 个 P2，2 个 P3，全部采纳）：
  1. P1 `powf`：Rust 的 `f32::powf` 跨平台不保证相同 → 新模块 `fuse_powf.rs`（照 glibc 算法的纯 Rust 实现，不加 crate），全量对比验收（§3 第 5 步、§4、§9）。
  2. P1 bitshuffle：校正产物的 shuffle 在开始时预检，不支持就退回 Python（§5.1、§6、§10 第 7 条）。
  3. P1 测试跳过：算术测试与任务测试分开判断，验收时不允许意外跳过；测试确认确实走了 Rust（§6、§7.2）。
  4. P2 NaN：分母下溢时照 NumPy 保留 NaN，量化为 0；加金标准例子（§3 第 6 步，本机已复核：NumPy 1.26.4 给出 NaN → 0）。
  5. P2 内存：按阶段算峰值，并先扣掉写入部分的内存，再定计算并发数（§8）。
  6. P2 Stop：等待时间包括正在进行的读取、计算和写入，两个池都结束后才删临时库，实测记录（§5.3）。
  7. P2 测试覆盖：加写入失败测试、其他 raw 数据类型、raw 与校正通道混合（§7.2）。
  8. P3 边缘分块：安全性的理由改为"每个分块只属于一个单元"（§5.3）。
  9. P3 验收脚本：并进 `tests/test_f5_rust_fusion.py`，用环境变量控制是否运行（§7.3）。
- **v1.2**（codex 第 2 轮 `codex_f5_plan_2.out`：APPROVE WITH CHANGES，无 P0；第 1 轮的第 2、3、5、6、7、8、9 条已解决，`fuse_powf` 的做法符合裁定 2）：
  1. P1 许可：只从 ARM 上游（固定提交）翻译，不从 LGPL 的 glibc 抄；选 MIT，保留版权和许可声明，加 `THIRD_PARTY_NOTICES.md` 并随 wheel 发布，进白名单；引入第三方代码列为用户决定（§12 第 1 条）。同时固定计算方式：用 `mul_add` 对齐本机 glibc 的 FMA 版本，以全量对比为准。
  2. P2 NaN：去掉"只有分母下溢才会产生 NaN"的错误说法，逐个运算写清 NaN 的处理（含 `NaN**0 = 1`），金标准加 `min=-1e300` 和 `gamma=1e-50` 两个例子（§3 第 6 步）。
- **v1.3**（codex 第 3 轮 `codex_f5_plan_3.out`：APPROVE WITH CHANGES，无 P0 / P1 / P3；第 2 轮的问题已解决）：
  1. P2：§12 第 1 条的方案 B 写错了。`libm` crate 的 `powf` 是 FreeBSD / Sun 的旧算法，不是 ARM 算法；使用许可为 MIT。已改正，并写明它只是未经验证的候选。
