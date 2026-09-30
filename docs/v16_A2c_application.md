# v16 块 A2c — Step4 与 Step1 fusion 的图像读取迁移到 PixelSource：实施申请 v2

日期：2026-09-30。分支 `v16`，调查基于 HEAD `6a92ce2`（工作区另有 A2b-probe 报告与记录，未提交）。依据：
- v2.3 §0.2 E1、§0.4 块规则、§12 的 (b) 分支；
- v2.2 §5.2 A2c；
- A2a（`2a0a3e8`，`docs/v16_A2a_application.md`）；
- A2b-probe 结论 (b)（用户 2026-09-30 裁定）；
- v2.4 草案 r2 §5.1a 的契约补丁 P1（草案，**未批准**；见 §9 裁定 1）；
- v2.3 §11 记下的两个 **A2c 迁移阻断项**。

状态：**申请 v2，用户 2026-09-30 批准**（§9 全部按建议）。**尚未开始实施**，按 v2.3 §0.4 规则 1 与 v2.4 的顺序执行。

修订记录：
- v1：初稿。
- v2：按用户增补指令修订（2026-09-30）：
  - 契约补丁 P1 改为与格式无关的命名 `native_tile_shape(level)` / `read_native_tile(channel, level, tile_y, tile_x)`，并单独作为第一个提交，先证明 A2a 逐位不变性继续全绿，再迁移 Step4 和 fusion；
  - 提交切分固定为三段；
  - P2（原始源描述）移到 A3，因为提交切分固定后，本块不再有它的位置。

---

## 1. 必要性与目标

E1「数据平面收敛」：Step4 和 Step1 fusion 不再各自解释「源是什么」，改为通过 A2a 的 `PixelSource` 契约读取；各自仍用自己的最优读法（Step4 用分块并行解码，fusion 用区域读）。

A2c 只迁移这两个消费者，**不改算法、不合并读取器、不碰 viewer 读取路径、不改工作线程模型**。

## 2. 调查结果（只读）

### 2.1 Step4（`core/quant_sources.py` 的 `JobReader`）

- 原始通道：`self._raw = TiffTileReader(job.slide, threads)`，`channels()` 里调用 `self._raw.read([s.index …], by + y0, …)`。坐标先加上区域原点，再按**页序号**选通道。
- 校正通道：`zarr.open(ch.path, mode="r")[ch.array]`，读 `arr[oy + y0 : …]`（ROI 局部坐标加偏移）。线程池借用的是 `self._raw._pool`。
- `raw_mode`（写进来源记录）取自 `TiffTileReader.mode`。
- 上游的 `resolve_quant_job` 是 fail-closed 的来源契约（严格组查找、方法、参数、dtype、来源切片都要核对）。**A2c 不改它。**

### 2.2 Step1 fusion（`ui/step0/overview_panel.py` 的 `FullFusionWorker`）

- 8 线程池，逐通道调用 `_read_one_channel(loader, …)` → `loader.read_region(ch, …, downsample=1, normalize=False)`，返回 float32。
- 这里的 `loader` 是 main_window 的 `self.loader`：在 `:3229` / `:4509` 通过 `set_corrected_zarr_store(path, decisions)` 进入校正模式，所以**校正通道读的是 Step0 保存的校正产品**，其余通道读原始切片。
- 构造 worker 的位置：`ui/main_window.py:9839`。决策与路径已经保存在 `self._corrected_zarr_path` / `self._corrected_decisions`（`:3214`、`:3226`）。

### 2.3 两个 A2c 迁移阻断项（v2.3 §11）与本块的处理

| 阻断项 | 与本块的关系 | 处理（裁定 2） |
|---|---|---|
| (1) loader 的校正通道请求不完全落在一个已保存 ROI 里时，**悄悄改读原始像素** | fusion 走的正是这个 loader，直接相关；Step4 不相关（它本来就 fail-closed） | fusion 改用 `CorrectedZarrSource`：按交集返回，没有交集时报错，**永远不会用原始像素去填**。风险：如果某个真实工作流里 fusion 区域超出了校正组的 bbox，行为会从「悄悄用原始像素」变成「报错」。验收时在代表性数据集上证明请求都在组内（逐位相同），并对超出的情况写一条测试 |
| (2) loader 的 `_corrected_store` 延迟创建、没有加锁，GUI 线程修改它时，读线程可能正在读 | fusion 的 8 线程正是读者之一，直接相关；Step4 不相关 | 迁移后 fusion 在任务开始时建一组**自己的、不可变的**源，不再读 loader 的可变状态，所以这个竞态与 fusion **无关**（由构造保证）；另加一条测试：任务进行中改 loader，不影响输出 |

Step0 本身、预读、预览这些路径仍然走 loader，阻断项对它们依然存在，留待以后迁移它们的块处理。

### 2.4 需要注意的差异

- dtype：loader 返回 float32，`OmeTiffSource` 返回原生 uint8。fusion 在调用处转成 float32；uint8 转 float32 是精确的，所以逐位不变。
- 性能：A2a 的 `OmeTiffSource.read_regions` 是逐通道调用 `RawTileProvider`，比 Step4 现在用的 `TiffTileReader`（按分块并行解码）慢。Step4 迁移需要第 0 层的 `read_regions` 委托给 `TiffTileReader.read`（放在第 2 段提交里，§3.2），否则 Step4 会变慢。

## 3. 做法与提交切分（固定三段，每段都可以单独回退）

### 3.1 第 1 段 — native-tile 契约补丁（`core/pixel_source.py`、`sources/ome_tiff.py`、`sources/corrected_zarr.py`）

- **P1，与格式无关**：
  - `native_tile_shape(level) -> (h, w)`：存储本身的块大小。OME-TIFF 对应该层的 TIFF tile，zarr 对应 chunk；
  - `read_native_tile(channel, level, tile_y, tile_x)`：返回存储原生网格上的**一个块**（边缘块截短），等于对应矩形的 `read_region`。
  - 在 A2c 里，`OmeTiffSource.read_native_tile` 的实现就是 `RawTileProvider` 读该块矩形（行为不变）。到 A9，probe 验证过的 TIFF 直接分块解码器会成为它的实现：优化始终留在 `PixelSource` 契约后面，viewer 不会自己打开 TIFF（v2.4 草案 r3 §5.1a P1、§20.4）。
- **会改动 A2a 已落地的接口**：A2a 有一个可选提示 `native_chunk_shape()`，没有 level 参数，适配器返回 None，产品里没有调用方（A2a 的测试断言过）。P1 用 `native_tile_shape(level)` 取代它，并新增 `read_native_tile`。
- **对 A2a 逐位测试的影响：没有。** A2a 的 43 条测试没有调用 `native_chunk_shape`；新增方法不改变已有方法的行为。
- 这一段**单独提交**：提交前重跑 `tests/test_v16_pixel_source.py`（43 条应继续全绿）和 A2a 的验收探针（真实切片上 0 处差异），再加上 P1 的新测试（§6）。之后才开始第 2 段。
- P2（原始源描述：`kind`、层数、最粗层形状）移到 **A3**：层数与层形状 `PixelSource` 上已有，持久化的描述属于 A3 的项目 / 来源记录。

### 3.2 第 2 段 — Step4 → PixelSource（`core/quant_sources.py` 的 `JobReader`，只改这个类；`sources/ome_tiff.py` 第 0 层的 `read_regions`）

- `OmeTiffSource` 第 0 层的 `read_regions` 委托给 `TiffTileReader.read`（与 A2a 的 `scan` 用同一个读取器）；其他层仍是基类的逐通道实现。
- 原始通道：`OmeTiffSource(job.slide)`，`read_regions(indices, 0, by + y0, …)`。坐标与页序号的语义不变。
- 校正通道：每个校正组一个 `CorrectedZarrSource(zpath, roi_name)`，读 `read_region(name, 0, by + y0, by + y1, bx + x0, bx + x1)`，也就是全局坐标，不再手算组内偏移。仍用 JobReader 自己的线程池并行。
- `raw_mode` 仍取底层 `TiffTileReader.mode`，来源记录的含义不变。
- `resolve_quant_job` 不动，所以 Step4 的 fail-closed 来源契约不变。

### 3.3 第 3 段 — FullFusionWorker → PixelSource（`ui/step0/overview_panel.py` 的 `FullFusionWorker`；`ui/main_window.py` 只加接线）

- `FullFusionWorker` 新增一个可选参数 `sources`（通道名 → `PixelSource`）。给了就用它读、转 float32；没给就走原来的 loader，这样旧调用方和测试不受影响。
- main_window 在 `:9839` 构造 worker 时，按 `self._corrected_decisions` / `self._corrected_zarr_path` 建好这组源：校正通道用 `CorrectedZarrSource`，其他用 `OmeTiffSource`；任务结束时关闭。这是 v2.3 §0.4 规则 6 允许的接线。
- 读取方式、8 线程池、区块划分、`_channel_norm`、写 zarr 都不变。

## 4. 白名单

- `core/pixel_source.py`（P1 的方法与默认实现）
- `sources/ome_tiff.py`、`sources/corrected_zarr.py`
- `core/quant_sources.py`：**只改 `JobReader`**
- `ui/step0/overview_panel.py`：**只改 `FullFusionWorker`**（构造参数与 `_read_one_channel` 的分支）
- `ui/main_window.py`：只改构造 `FullFusionWorker` 的接线（`:9839` 附近）
- 测试：`tests/test_v16_pixel_source.py`（P1、`read_regions` 委托）、新增 `tests/test_v16_a2c_migration.py`，以及 `tests/test_quant_sources.py`、`tests/test_step4_worker.py`、`tests/test_step1_fusion_core.py`、`tests/test_step1_fusion_isolation.py`（只在确有必要时更新语义，不删行为断言）
- 脚本：新增 `scripts/diagnose_v16_a2c_oracle.py`，复用 `scripts/probe_v16_a2b_ngff.py` 的 `same-step4` / `same-fused` 比对程序，不改它
- 文档：本申请的执行记录、v2.3 §12 进度、`docs/v16_contracts_draft.md` §1（P1）

## 5. 不改的范围

- 算法：QuantEngine / S4-3、fusion 的 `_channel_norm` / 融合公式、N2 / N3、分割引擎。
- 读取器本身：`TiffTileReader`、`RawTileProvider`、`OMETIFFLoader` 不改（不合并）。
- viewer 读取路径（`AGENTS.md` 规则 5）。
- 工作线程模型：`FeatureExtractWorker` / `FullFusionWorker` 的 QThread、线程池大小、生产者队列。
- 其他仍走 loader 的消费者：Step0、预读、预览（`PreviewLoaderThread`）、预分割的 `fused_window`、Cellpose / Mesmer 子进程预览（裁定 3）。
- `resolve_quant_job` 的来源契约。

## 6. 验收门（预注册）

**Oracle**：迁移前的路径，即同一输入上的 `git archive HEAD`（v2.3 §0.4 规则 2）。v15-final 的结果作为参照另外报告，不作判定依据：它和 HEAD 之间还隔着 v16 的其他改动。

**先证明比对程序能发现差异（反向注入）：**
- [ ] 复制一份 h5ad，改动 `X` 中一个值 → `same-step4` 报告不同；只改 `uns/provenance_json` → 报告相同（它被排除）
- [ ] 复制一份 fused zarr，改动一个像素 → `same-fused` 报告不同；空目录或全零 → 报告失败

**逐位（代表性数据集：test1 副本上的 cropped_region）：**
- [ ] Step4：h5ad 的 47 个数据集逐位相同（只排除 `uns/provenance_json`），CSV 相同
- [ ] `FullFusionWorker`：fused 像素逐位相同，而且不是全零；包含校正通道（test1 有 CD3D、HsBAg 两个校正通道，其中 HsBAg 参与融合）

**新测试（合成数据）：**
- [ ] 第 0 层 `read_regions` 与 `TiffTileReader.read` 逐位相同
- [ ] 第 1 段提交前：A2a 的 43 条测试继续全绿，A2a 验收探针在真实切片上 0 处差异
- [ ] P1：每层每个原生块，`read_native_tile` 等于对应矩形的 `read_region`；`native_tile_shape(level)` 等于该层 TIFF tile / zarr chunk 的大小；边缘块截短
- [ ] 阻断项 (1)：fusion 区域超出校正组 bbox 时报错，而不是用原始像素填
- [ ] 阻断项 (2)：fusion 进行中调用 loader 的 `set_corrected_zarr_store` / `set_correction_config`，输出不受影响
- [ ] Step4 性能：端到端耗时不超过迁移前的 1.05 倍（由第 2 段的委托保证）

**单调绿色规则（v2.3 §0.4 规则 4）：**
- [ ] A1 零漂移（`test_v16_zero_drift`）、A1b F1–F4（`test_v16_frame_lock`）、A2a 逐位不变性（`test_v16_pixel_source`）全部通过
- [ ] v15 科学回归：相关模块逐个进程运行；失败的模块放到 HEAD 上重跑，逐条对比失败的测试名；没有新增失败

**反向注入（实施后）：** 以下三种改法都应让逐位测试或性能门变红——第 1 段让 `read_native_tile` 越过块边界；第 2 段让校正读漏掉组偏移，或把第 0 层委托改回逐通道读；第 3 段忘了转 float32。

**真机：** fusion 生成一次、Step4 跑一次，结果与迁移前一致。没有界面变化。

## 7. 风险

| 风险 | 对策 |
|---|---|
| 校正通道的坐标换算错误（全局坐标与组内偏移） | 逐位 oracle；合成数据上专门测一个 ROI 不从原点开始的组 |
| 阻断项 (1) 的行为变化：原来悄悄用原始像素，现在报错 | 代表性数据集上证明没有越界；越界单独测试；是否接受「报错」由裁定 2 决定 |
| Step4 变慢 | 第 2 段把第 0 层的 `read_regions` 委托给同一个 `TiffTileReader`；有性能门 |
| 回退 | 三段提交各自可回退；第 3 段的 worker 在没有 `sources` 时仍走 loader |

## 8. 估计

约 2 个工作日，另加约 0.5 天真机时间（fusion 一次、Step4 一次）。

## 9. 请用户裁定

1. **契约补丁 P1 作为 A2c 的第 1 段提交**（v2.4 草案 r2 §5.1a；v2.4 还没有批准）。它用 `native_tile_shape(level)` 取代 A2a 的可选提示 `native_chunk_shape()`，并新增 `read_native_tile`；对 A2a 的逐位测试没有影响。
   - (a) 在本块提前实施；
   - (b) 等 v2.4 批准后再做（那样 A2c 只剩第 2、3 段）。
   - **建议 (a)**：改动小，只取代一个没有调用方的可选提示并新增一个方法，而且第 1 段独立、可以单独回退。
2. **阻断项 (1)**：fusion 的校正通道越界时，改为 fail-closed（报错），不再悄悄用原始像素。**建议同意**，这正是 A2a 契约要消除的科学正确性问题。
3. **范围**：只迁移 `FullFusionWorker`；预览、预分割的 `fused_window`、子进程预览不在本块。**建议同意**，每个消费者单独一块（v2.2 §5.2）。
4. **Oracle 用迁移前的 HEAD**，v15-final 只作参照。**建议同意。**
5. **Step4 性能门 ≤ 1.05×。** **建议同意。**

## 10. 用户裁定（2026-09-30）

「全部按建议」：
1. P1 作为第 1 段提交，提前实施 (a)；
2. 阻断项 (1)：fusion 的校正通道越界时 fail-closed；
3. 只迁移 `FullFusionWorker`；
4. Oracle 用迁移前的 HEAD，v15-final 只作参照；
5. Step4 性能门 ≤ 1.05×。
