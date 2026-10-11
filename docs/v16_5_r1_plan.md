# v16_5 R1：完整读图（计划第二版，2026-10-12 用户批准）

- 依据：总方案 `docs/v16_5_rust_viewer_plan.md` v2.1 的 R1 行、§4（SourceManifest）、§5.1 第 1、3、4、5 条、§6（构建）、§7（精度契约）、§10 第 7 条（已批准新增自动构建工作流）。
- 前一阶段：S-Raw 和显存修正已推送（a64e9e9，A9 文档 §47、§48）。
- 原则：
  - R1 只做"读得对、读得稳"；
  - 新读图能力只经测试和探针调用，产品画面不变（Step1 仍只把整片 ROI、原始 8 位、Overlay 交给 Rust）；
  - 接进画面是 R2 的事（GPU 三族格式、校正通道上屏）。

## 1. 现状（已核对代码）

| 项目 | Python 现在怎么做 | Rust 现状 |
|---|---|---|
| 原始 TIFF 直读 | `raw_tile_provider._build_chunk_plan`：只取 CYX、每通道一页、分块、单样本、整字节宽；压缩 1/5/8/32946/50000（无、LZW、deflate、zstd），预测器 1/2；dtype 是 u/i/f。其余走 zarr | R0 已做无、LZW、deflate，大小端，条带。清单 `step1_core_adapter.build_raw_manifest` 本来就收更宽的样本和条带级别，只接 8 位是产品准入（`_unsupported_reason`）的限制。**缺 zstd**；预测器 2 只支持 1/2/4 字节整数，绑定层拒绝浮点 |
| 原始块大小 | 我查过的目录里（`/sda1/Fusion/benchmark`、`a9_bench`、`/nvme0n1p1`、Kevin）Step1 能用的真片：TMA3 512，biopsy/扁桃体等 256（LZW）。这些目录里没有 Step1 可用的 16 位、浮点或 zstd 真片（不是全服务器普查） | 读取按文件自己的块，不依赖 512 |
| 校正产物 | `root[<roi>][<channel>]`（`open_corrected_channel_array` 优先找 ROI 名，再找第一个含该通道的组），zarr v2，`<f4`，1024×1024 分块，Blosc（lz4，clevel 5，按字节重排），`fill_value` 0；属性有 `roi_bbox_fullres` 等。新产品有 `source_identity`；服务器上这份真实产品（2026-08）**没有**，所以它没有可用的 sidecar 粗图，身份令牌退回用 `written_at` | 无 |
| 校正的粗级别 | 步长由 `Step1TileProvider._level_rect` 给出：`round(level_downsample)`，而 `level_downsample` 本身是 `round(h0/hk)`（Python 的四舍六入五成双）。`step1_source.read_tile`：有合格的 sidecar 粗图（`corrected_coarse.zarr`，身份字段逐项核对、`complete`、形状和起点一致）就切片，否则 `reduce_corrected`：第 0 级原点对齐的块，按有效样本数求平均，float64 累加，分片读（每片 ≤ 4M 个第 0 级像素），步长 1 时直接拷贝 | 无 |
| ROI 和有效性 | 原始通道：ROI 换算到第 k 级时向外取整，只读 ROI 内的部分。`read_tile_native` 里只有 u8/u16 保留整数、有效区域是矩形；其他原始类型（i16/i32/浮点）转成 f32，按"数值有限"判有效。校正通道：f32，按"数值有限"判有效（产品里的 NaN、无穷都算无效）；现算平均时，NaN 样本照样计数、和变成 NaN。产品打不开：整块缺失，并说明原因 | S-Raw 只支持整片 ROI |
| 身份 | `Step1SourceTable.identity_token()`：ROI、交接修订、每通道决定、每个产品的形状/类型/方法/ROI/`source_identity` | R0：原始文件的大小、修改时间、文件编号，和 provider 指纹核对 |

## 2. R1 要做的（第二版：每项写明要对齐的规则）

1. **原始 TIFF 补齐**
   - zstd（压缩 50000）；
   - 16 位、32 位整数、浮点的读取；
   - 预测器 2 用于浮点时，按同宽整数的位模式做差分还原（和 imagecodecs 一致，codex 已用 imagecodecs 实测确认）；
   - 256 等非 512 的块大小。
   - 清单范围：Python 也走直读的分块级别，加上 R0 已支持的条带级别（保留）。其他布局 Rust 不接，照旧走 Python。
2. **校正产物读取（zarr v2）**
   - 只接这一种：`zarr_format` 2、`order` "C"、`filters` 空、分隔符 "."（或缺省）、dtype `<f4`（小端 f32）、Blosc 压缩器。其余一律拒绝并回退。
   - 缺的分块用 `fill_value` 填，不是一律填 0。
   - 边缘分块按完整分块的存储形状解压后再裁。
   - **Blosc 帧**：
     - 按帧头里的块大小解（`.zarray` 写的 `blocksize: 0` 只表示由压缩端自动选）；
     - 覆盖整帧直接拷贝（memcpy）、拆分 / 不拆分的块、未压缩的拆分段、最后一个不满的块、帧头里的元素宽度、字节重排余下的尾巴；
     - 编码只接 lz4，其他拒绝。
3. **校正的现算层级**：逐项复现 `step1_source.read_tile` 的校正分支：
   - 步长 = `round(round(h0/hk))`（四舍六入五成双）；
   - 第 k 级的块换回第 0 级坐标 = 乘以步长；
   - 有合格的 sidecar 就切片，切片失败退回现算；
   - 现算 `reduce_corrected`：
     - 块以第 0 级原点对齐，相位来自分片实际落下的位置；
     - 分片尺寸按 `_slab_geometry` 计算（含"预留一块的边"）；
     - 有效计数按 `_span_per_block` 计算；
     - float64 累加；NaN 计数照算、和照样变 NaN；
     - 步长 1 且在额度内时直接拷贝，超出额度时走分片。
   - 验收按 §7 的误差，掩膜完全一致；无穷、负值、NaN、全无效块、边缘不满的块都要测。
4. **Step1 读块规则**：复现 `Step1TileProvider.read_tile` 和 `read_tile_native` 的输出：
   - u8/u16 原始：整数值，有效区域是 ROI 裁剪后的矩形；
   - 其他原始类型：f32，有效 = 数值有限；
   - 校正：f32，有效 = 数值有限；
   - 缺失：整块无效，附原因。
   - ROI 换算到第 k 级时向外取整。局部 ROI 在读图层面支持（画面上何时放开另由用户批准）。
5. **身份核对**（第二版）：
   - Python 生成清单时，用打开函数实际解析到的数组目录（不是自己拼路径）记下：
     - `.zarray`、`.zattrs` 的大小、修改时间、文件编号；
     - 产品身份令牌（沿用 `_product_token`）。
     - sidecar 同样记下。
   - Rust 打开时核对，不一致就拒绝。
   - **假设**：产品在一次查看期间不被原地改写（保存校正会走"换来源"重建清单）。只改分块、不改元数据的原地改写，和打开之后的改动，都**检测不到**；Python 现在的 zarr 读法也一样。R1 不为此逐块核对，只把这个假设写进接口文档。
   - 缓存键带清单修订，换清单后旧块全部失效。
   - 用合成产品测：整体替换、sidecar 过期、sidecar 不完整。
6. **取消和资源上限**（第二版，把现有读图线程池的问题也算进来）：
   - 现在的线程池（`pool.rs`）会把已经过期的读完结果也放进缓存。R1 改成：读完后、放进缓存或交出之前，再核对一次"还要不要"，不要就丢掉，不算错误；
   - 现算层级在每个分片之间核对，取消后 **≤ 1 个分片的时间内停下**（目标 ≤ 100 ms，实测报告）；
   - 关闭时结果队列满了也不能卡住：读图线程等队列时也要能看到"关闭"；
   - 内存上限按总量算，不只是分片大小：
     - 每个线程同时最多持有一个分片的解压分块、补边、累加器和掩膜；
     - 6 个线程合计的上限写进接口文档，并实测峰值；
     - 结果队列里的块按字节计入 CPU 缓存额度；
     - 线程数取 min(6, 核数)。
7. **Windows 自动构建（§10 第 7 条已批准）**：`.github/workflows/fusion_viewer_core.yml`：
   - 用 maturin 编 wheel：Linux（manylinux）和 Windows（MSVC，Python 3.10），Rust 用固定版本；
   - 装好的 wheel 上跑 `tests/test_v16_5_r1_ci.py`：导入、合成 TIFF 解码（各压缩方式和类型）、合成 zarr + Blosc 解码、和 tifffile/numcodecs 比对、通知冒烟。不需要 GL，也不需要服务器数据；
   - wheel 保存为构建产物；
   - 触发条件：推送到 `v16_5`、发往 `v16_5` 的 PR，且改动涉及 `native/**`、`ui/step1_core_adapter.py`、`tests/test_v16_5_*`、`Cargo.lock` 或工作流本身。

**不做**：GPU 的 16 位 / 浮点、Fusion、校正通道上屏（R2），标签（R3），`requirements-pip.txt` 登记（等 R2 接进产品、wheel 由自动构建产出时再登记，届时报告）。

## 3. 关键取舍（请留意）

- **Blosc 用纯 Rust 实现**（只解 Blosc1 帧 + lz4 + 字节重排，LZ4 用 `lz4_flex` 库，MIT 许可），不用总方案 §9 里写的 `blosc-src`（C 库）。
  - 原因：免去 Windows 上的 C 编译；我们只需要这一种组合。
  - 风险：自己写的解析器要和 numcodecs 逐字节一致。所以用真实产品的**全部分块**比对，也测合成的边角情况：最后一块不满、memcpy 模式、拆分模式。
- **zstd 用纯 Rust 解码器 `ruzstd`**（MIT），同样为了免去 C 编译。它的文档自己承认比 C 库慢。服务器上没有 zstd 真片，所以用合成文件测：Rust 单块解码比 Python（imagecodecs）慢 1.5 倍以上，就改用 `zstd` 库（C）。
- Blosc 的兼容风险更大：真实产品全量比对不能代替各种帧的合成样例，两者都要做。
- 现算层级的累加次序和 numpy 不同（numpy 是成对求和），所以按 §7 的误差 |a−b| ≤ 1e-6 + 1e-6·|b| 验收，有效掩膜必须完全一致。

## 4. 文件范围

Rust（`native/fusion_viewer_core/src/`，每个模块只做一件事）：
- 改 `decode.rs`：加 zstd；
- 新 `blosc.rs`：Blosc1 lz4 解压；
- 新 `zarr_v2.rs`：一个 zarr v2 数组（元数据、分块路径、缺块、读矩形）；
- 新 `reduce.rs`：按有效样本求块平均（分片、可取消）；
- 新 `step1_tile.rs`：Step1 读块规则（原始 / 校正 / 缺失 → 值 + 有效区域）；
- 新 `corrected.rs`：校正清单及其身份核对；
- 改 `manifest.rs`：放开 16 位和浮点；
- 新 `cancel.rs`：取消令牌（如果并入 `pool.rs` 更自然就并入，不超过 300 行）；
- 新 `py/corrected.rs`：测试用的绑定；
- 改 `pool.rs`、`cache.rs`：交出和放缓存前再核对"还要不要"；关闭时不卡在满队列；按字节计入队列中的块；
- 改 `lib.rs`、`py/mod.rs`、`py/reader.rs`：登记新模块，放开浮点预测器；
- `Cargo.toml`、`Cargo.lock` 加 `ruzstd`、`lz4_flex`。

Python 和其他：
- 改 `ui/step1_core_adapter.py`：16 位 / 浮点 / zstd 的原始清单；新增 `build_corrected_manifest(table, ...)`，从 `Step1SourceTable` 生成；
- 新 `scripts/r1_parity_probe.py`：全量和抽样比对；
- 新 `scripts/r1_speed_probe.py`；
- 新 `tests/test_v16_5_r1_decode.py`、`test_v16_5_r1_corrected.py`、`test_v16_5_r1_step1_tile.py`、`test_v16_5_r1_cancel.py`；
- 新 `tests/test_v16_5_r1_ci.py`：自动构建跑的子集，不依赖 GL 和服务器数据；
- 新 `.github/workflows/fusion_viewer_core.yml`；
- 文档：本文件、`docs/v16_A9_application.md` §49（结果）。

不改：
- 产品读图路径；
- `Step1SourceTable` 的规则；
- 已保存的校正产物；
- Step0；
- 现有测试的断言。

## 5. 关口（不过就停下报告）

1. **原始**：Rust 和 Python 直读逐字节一致：
   - TMA3（deflate，512）、biopsy（LZW，256）两张真片，每级各抽 2000 块；
   - 合成文件全组合：zstd / LZW / deflate / 无 × 预测器 1/2 × u8/u16/i16/u32/i32/f32 × 大小端 × 边缘不整块 × 分块 / 条带。
2. **校正分块**：
   - 真实产品（biopsy 全片，3 个通道）的**全部分块**，和 zarr/numcodecs 逐字节一致；
   - 合成样例覆盖 §2 第 2 条列出的每一种 Blosc 帧和 zarr 情况，以及应当拒绝的变体。
3. **Step1 读块**：和 `Step1TileProvider.read_tile` / `read_tile_native` 比：
   - 值在 §7 误差内，有效区域完全一致；
   - 覆盖每一级（含整片粗级别）、ROI 边缘、局部 ROI、缺失产品、NaN / 无穷 / 负值、全无效块、有效 / 过期 / 不完整的 sidecar；
   - 真实产品加合成项目。
4. **取消**：整片粗级别现算进行中取消，从发出到线程停下 ≤ 100 ms（不超过一个分片）；取消的结果不进缓存、不交出。关闭时队列满也能在 1 s 内全部退出。
5. **内存**：现算整片粗级别时，6 个线程的峰值临时内存不超过接口文档写的上限（实测报告）。
6. **读速**（按块/毫秒报告）：
   - 校正块：冷读 6 线程 Rust 不低于 Python 的 90%，热读不低于 Python；
   - 整片粗级别现算：报告 Python 对 Rust 的耗时（只报告，不设门槛）；
   - "界面忙"时 Rust 掉速不超过 10%。
7. **自动构建**：Linux 和 Windows 两个任务在 GitHub 上实际跑通一次。
8. **回归**：S-Raw 的 4 个测试文件、R0 的 3 个，加上 §48 跑过的 37 个模块，以及 Python 读图规则本身的 `test_step1_source*` 和 `test_v16_a9p_source_parity`，通过和失败的数量和改前一致（已知的 2 条失败除外）。codex 代码审核没有必须修的问题。

## 6. 工期

5–7 个工作日。比总方案多约 1 天，因为第二版加了线程池的取消和内存上限，以及更全的编码样例。如果 Blosc 的帧变体比预想的多，可能再多 0.5–1 天。

## 7. 审核记录

- 第一版：codex（astra 高强度）结论是"实现前先修改"。必须改的 5 条（§1 事实、数值规则、编码规则、浮点预测器、文件清单和取消 / 内存）和应该改的 3 条（身份假设、自动构建和关口、两处偏离的门槛）已全部写进第二版。
- 用户批准时（2026-10-12）补两项：单独的取消测试文件 `test_v16_5_r1_cancel.py`；回归加入 `test_step1_source*` 和 `test_v16_a9p_source_parity`。授权范围包括 §2 第 6 条对 `pool.rs`、`cache.rs` 的改动（AGENTS 规则 4）和新增工作流文件。
