# v16 块 A2a — PixelSource 契约 + 现有数据源的适配器（行为零变化）：实施申请 v2

日期：2026-09-30。分支 `v16`，调查基于 HEAD `c171d72`。依据：v2.3（修订本 `2cb496c`）§0.2 E1、§12 第 1–2 周；v2.2 §5.1–§5.5（v2.3 保留原文）；`docs/v16_contracts_draft.md` §1、§2。
状态：**申请 v2，用户 2026-09-30 批准**（§8 全部按建议；授权提交后实施）。

修订记录：
- v1：初稿。
- v2：按独立审核意见修订（结论为「条件批准」，要求开工前修 5 条，另补 3 个细节）。每条都核对过代码：
  ① 契约不引用 viewer 的类型，契约与遗留适配器分成两处（§3.1、裁定 4）；
  ② `read_region` 的越界语义统一：每个源声明 `valid_bounds(level)`，读到的是请求与它的交集，交集为空时报错（§3.1、§3.3）；
  ③ `close()`、上下文管理器、并发读语义写进契约（§3.1）；
  ④ 删掉契约里的实现细节：`scan` 的预读深度不写进契约；`level_downsample_rounded` 移出契约，降为 OME-TIFF 适配器的遗留兼容属性（§3.1、§3.2）；
  ⑤ 验收改为能力矩阵，每种比较只在两边都支持的层上做（§7）；
  补充：`source_identity` 与 `slide_id` 的区别写明；`physical_size()` 返回格式固定，并测试「有元数据时读对」；`channel_names` 改用第三种规则 (c)；§2.2 第 4、5 条改为「A2c 迁移阻断项」。

---

## 1. 必要性与目标

E1「数据平面收敛」要求：图像数据有**一个**明确的读取契约 `PixelSource`。viewer、Step1 融合、Step4 不再各自解释「源是什么」，但各自仍可以用自己优化过的读法（viewer 随机 / 多级读，Step4 顺序扫描）。

A2a 只做第一步：**定义契约，并给现有数据源写适配器；不迁移任何消费者，行为零变化。** 消费者的迁移放在 A2c，每个消费者单独申请。A8 的 `active_source_id` 也要指向一个由这个契约定义的源，所以 A2a 必须排在前面。

## 2. 调查结果（只读）

### 2.1 今天读原始切片像素的有 5 条路径

| 路径 | 位置 | 返回 | 层级 | 句柄 |
|---|---|---|---|---|
| A. `OMETIFFLoader.read_region` | `core/io_loader.py:78` | **总是 float32**。默认 `normalize=True`，按**所读区域**的百分位归一到 [0,1]，同一像素换一个区域读，值就不同 | 只读第 0 层；`read_region_lowres` 从较粗的层取最近邻 | 每次调用新开 `TiffFile` |
| B. `RawTileProvider`（viewer） | `viewer/raw_tile_provider.py` | 原生 dtype（test1 是 uint8） | 任意层 | 每线程一个句柄 |
| C. `TiffTileReader`（Step4 扫描） | `core/quant_sources.py:389` | 原生 dtype | 只读第 0 层；快速分块路径，另有 aszarr 退路 | 一个共享 `TiffFile`，每个线程池线程一个文件句柄 |
| D. `SharedChannelStore`（Step2 标记通道） | `utils/channel_cache.py:86-132` | 由 A 的逻辑重写而来 | 第 0 层 | 一个句柄，读取用 RLock 串行 |
| E. `Step1TileProvider` | `ui/step1_viewer_host.py:56` | 包在 B 外面：float32，ROI 外为 NaN | 任意层 | 同 B |

派生栅格（`corrected_channels.zarr`、`fused.zarr`、标签库）各有自己的读法，坐标是 ROI 局部坐标。仅 `corrected_channels.zarr` 的 ROI 组查找规则就有 4 种写法（loader、`calibration_source`、`quant_sources` 的严格查找、`hq_source_resolver`）。

### 2.2 已发现的不一致（只记录，A2a 不修）

1. **dtype 与数值语义**：A 的默认归一化结果依赖所读区域；B、C 是原生 dtype；E 是 float32 加 NaN。
2. **层级几何**：
   - B 同时提供取整的 `level_downsample` 和真实比例的 `level_downsample_yx`，两个都在用；
   - E 和 `reduce_corrected` 用取整步长（第 1 层 4，第 2 层 16），而 test1 金字塔的真实比例是 4.0003 / 16.013。所以同一层上，校正通道和原始通道的几何有细微偏差；
   - loader 的 `overview_downsample()` 假设 2 的幂，与 viewer 实际挑选的概览层是两套独立的算法。
3. **通道名**：三处规则不同。
   - loader：套用 `name_map`，缺名的通道**逐个**记为 `ch_NN`；
   - B：忽略 `name_map`，名字数与通道数不符时**全部**改成 `ch_NN`；
   - Step4 的 `_slide_channels`：逐个 `ch_NN`，不套 `name_map`。
   - 今天 `CHANNEL_NAME_MAP = {}`，所以实际没有差别。
4. **校正通道静默退回原始像素**：请求不完全落在某一个已保存的 ROI 里时，loader 的 `_read_corrected_roi_only` 返回 None，然后**悄悄读原始像素**（`io_loader.py:92-99`、`:216-234`）。Step1 在这种情况下拒绝读，Step4 fail-closed，只有 Step0 / loader 这条路径会退回。
5. **竞态**：loader 的 `_corrected_store` 延迟创建，没有加锁；`set_corrected_zarr_store` 在 GUI 线程上改它，而预读线程、8 线程的 `FullFusionWorker` 同时在读。
6. **私有 API 被跨层调用**：`_read_roi_zarr`、`_norm`、`_apply_configured_correction`、`_corrected_decisions` 在 `search_ctrl`、`main_window`、`channel_cache`、`preview_source_provider` 里被直接使用。

第 4、5 条可能影响结果的正确性，但都不在 A2a 的范围内（A2a 行为零变化）。按 P0 规则 2，它们作为 **advisory** 列入待办，在 A2c 迁移对应消费者时由用户裁定是否处理（§8.5）。

## 3. 做法

### 3.1 契约（`core/pixel_source.py`，只依赖 numpy）

一个抽象基类 `PixelSource`，外加 core 自己定义的几个类型。**这个模块不 import `viewer/`、`ui/` 或任何 Qt 模块**，由测试检查。

| 方法 | 语义 |
|---|---|
| `source_identity()` | 返回 core 自己的 `PixelSourceIdentity`（路径、指纹 `size:mtime_ns`、阶段、产品身份）。它是**运行时 / 存储身份**：用来判断「本地这份文件还是不是我刚打开的那份」、做缓存和过期检测。它**不是**科学身份：科学 / 项目身份是 `slide_id`，由 A3 / A8 定义，契约只留字段位置，两者不能混用 |
| `channel_names()` | 裁定 1 的规则 (c)。**不套用** UI / 用户的 `name_map`（那是显示别名，属于配置，不是源本身） |
| `dtype(channel)` | 原生 dtype，**不做 float 转换** |
| `level_count()` / `level_shape(level)` | 层数；`(H_L, W_L)` |
| `level_downsample(level)` | `(H0/H_L, W0/W_L)`，每轴真实比例、不取整。**契约只认这一个比例** |
| `valid_bounds(level)` | 这个源在该层**真正拥有**的像素范围 `(y0, y1, x0, x1)`，半开区间。原始源是整层；校正源是它那个 ROI 的 bbox |
| `read_region(channel, level, y0, y1, x0, x1)` | 返回 `(array, (y0, x0))`：array 是请求与 `valid_bounds` 的**交集**，第二项是交集的实际起点。**交集为空时抛出 `OutOfBounds`**。任何情况下都不会拿别的源的像素去填 |
| `read_tile(channel, level, ty, tx, tile_size)` | 按分块网格读，等价于对应矩形的 `read_region`；参数都是普通整数，不使用 viewer 的 `TileAddress` |
| `scan(channels, level, tile_size)` | 按行优先产出 `(y0, x0, block)`，block 的形状为 `(len(channels), h, w)`，通道顺序与请求一致；像素与同一窗口的 `read_region` 逐位相同。预读多少、用几个线程由后端决定，不属于契约。不支持的层抛出 `NotImplementedError` |
| `close()`，以及 `with source:` | 生命周期属于契约。`close()` 之后任何读都报错 |
| 可选：`read_regions(...)`、`native_chunk_shape()`、`preferred_threads()` | 批量读必须与逐通道 `read_region` 逐位相同 |
| 可选：`physical_size()` | 返回 `(dy_um, dx_um)`，单位固定为 µm，由 OME 的 `PhysicalSizeY/X` 和单位换算而来；读不到或单位不认识时返回 None，不猜测、不给默认值 |

**并发语义（契约条文）：** 同一个源实例的所有读方法都可以被多个线程**同时**调用。后端如果做不到，必须在内部自己串行化，不能让调用方去猜。每个 `scan` 迭代器只供一个调用方使用；多个迭代器可以并存。`preferred_threads()` 只是性能提示。

**不变量**（v2.2 §5.1）：三种能力（区域读、多级读、顺序扫描）对同一个 `(channel, level, region)` 返回逐位相同的像素。

### 3.2 `OmeTiffSource`（遗留适配器，只委托）

- 区域读、分块读、多级读委托给 `RawTileProvider`（默认 per_thread 句柄，线程安全）。在 `valid_bounds` 之内，它今天的截取行为与契约一致；交集为空时，由适配器抛出 `OutOfBounds`（`RawTileProvider` 今天返回空数组，但目前没有消费者经过适配器，所以没有行为变化）。
- `scan` 委托给 Step4 的 `TiffTileReader.read`，包括它的 aszarr 退路。`TiffTileReader` 的每个池线程用自己的文件句柄，各块写入不同的输出数组。它能否被多个调用方同时使用，由测试证明；如果证明不了，适配器用一把锁串行化。只支持第 0 层（裁定 3）。
- `channel_names()`：适配器自己解析 OME 元数据，按规则 (c)，不照搬 `RawTileProvider` 的写法（它在某一个通道缺名时会把全部名字改成 `ch_NN`）。两者只在「有通道缺名」这种边缘情况下不同，今天的数据上不会出现；执行记录里会写明。
- **遗留兼容属性** `legacy_level_downsample_rounded(level)`：等于今天 `RawTileProvider.level_downsample`。**只能作缓存键**，不得用于任何几何坐标、物理距离或科学参数换算。它不在契约里，只在 OME-TIFF 适配器上。
- 不改 `RawTileProvider`、`TiffTileReader`、`OMETIFFLoader` 的任何一行（v2.2 §5.2：读取器合并不在 A2a 范围内）。

契约草案 §1.2 写的是「吸收」这几个读取器，与 v2.2 §5.2 的「委托、不合并」有矛盾。以 v2.2 为准，并改正草案里这句（§4 白名单）。

### 3.3 `CorrectedZarrSource`（Step0 已提交的校正产品）

- 打开 `corrected_channels.zarr`，**只读一个 ROI 组**，查找规则沿用 `quant_sources` 的严格查找：这个 ROI 恰好一个组，而且组里包含该通道。
- 坐标用**切片第 0 层的全局坐标**，与原始源**坐标兼容**。注意两者并**不能互换**：原始源是整张切片、全部通道、多层、原生 dtype；校正源只有一个 ROI、部分通道、一层、float32。
- `valid_bounds(0)` 是这个 ROI 的 bbox。`read_region` 返回请求与 bbox 的交集；交集为空时抛出 `OutOfBounds`。**绝不会用原始像素去填 ROI 以外的部分。** §2.2 第 4 条（悄悄退回原始像素）的问题因此在契约层面不再可能出现。
- 只有 1 层（持久化的粗层属于 v2.2 §5.8，另行申请）；dtype 为 float32。
- `source_identity()`：阶段为 `corrected_saved`，产品身份为组名加上 zarr 的 `source_identity` 属性。

### 3.4 验收探针（新脚本，只读）

`scripts/diagnose_v16_a2a_pixel_source.py`：在 test1 副本（`diagnose_v16_a0_camera.py copy-project` 做出来的那份）上随机选区，把 `OmeTiffSource` / `CorrectedZarrSource` 的每种读法与今天各消费者的读法逐位比较，输出对照表。原始项目只读，前后做指纹核对（与 A0 / A1b 的探针相同）。

## 4. 白名单

- **新增**：
  - `core/pixel_source.py`（契约和 core 自己的类型，只依赖 numpy）；
  - `sources/__init__.py`、`sources/ome_tiff.py`（`OmeTiffSource`）、`sources/corrected_zarr.py`（`CorrectedZarrSource`）：遗留适配器（裁定 4）；
  - `tests/test_v16_pixel_source.py`；
  - `scripts/diagnose_v16_a2a_pixel_source.py`。
- **文档**：
  - `docs/v16_contracts_draft.md` §1：把契约改为「A2a 冻结」版本，改正 §1.2 里「吸收」一词，并补上 §2.2 的已知不一致；
  - 本申请的执行记录；v2.3 §12 进度一行。
- **不改动任何现有产品文件。**

## 5. 不改的范围

- 所有消费者：viewer、Step0、Step1 融合、Step2、Step4、预分割、子进程预览。
- `OMETIFFLoader`、`RawTileProvider`、`TiffTileReader`、`SharedChannelStore`、`Step1TileProvider`、`quant_sources` 的来源契约。
- §2.2 列出的所有不一致（留给 A2c 和用户裁定）。
- UI：没有界面变化，所以 `UI_SURFACE_RULES.md` 和两份用户指南都不需要改。

## 6. 风险

| 风险 | 对策 |
|---|---|
| 契约依赖 viewer 层，分层反了 | 契约只依赖 numpy，用自己的类型；只有 `sources/ome_tiff.py` 这个遗留适配器 import `viewer/raw_tile_provider.py`（该文件本身不依赖 Qt）。有测试检查 `core/pixel_source.py` 不 import `viewer/`、`ui/` 和 Qt |
| 并发读的语义没写清 | 并发语义写进契约（§3.1）；对 `OmeTiffSource` 的区域读和 `scan` 各做一个多线程并发测试，与串行结果逐位比较 |
| 契约定得不对，A2c 用不上 | 契约只包含今天消费者实际用到的方法（调查 §4 已逐条列出）；A2c 如需补充，在 A2c 的申请里改 |
| 以为「行为零变化」，其实有人 import 了新模块 | 测试断言：`core/pixel_source.py` 和 `sources/` 都不被任何产品模块 import（A2a 结束时应当如此） |
| 测试依赖真实数据 | 单元测试用临时目录里合成的小金字塔 OME-TIFF 和一个合成的校正 zarr；真实数据只在验收探针里用，而且只读 |

## 7. 验收门

**能力矩阵**：每种比较只在两边都支持的层上做。

| 比较 | 层 |
|---|---|
| `OmeTiffSource.read_region` / `read_tile` 对 `RawTileProvider.read_region` | 所有层 |
| `OmeTiffSource.read_region` / `scan` 对 `TiffTileReader.read` | 只比第 0 层 |
| `OmeTiffSource.read_region` 对 `OMETIFFLoader.read_region(normalize=False)`（loader 返回 float32，比较时还原为原生 dtype） | 只比第 0 层 |
| `CorrectedZarrSource.read_region` 对 `quant_sources` 的校正读 | 第 0 层（它只有这一层） |

**自动（`tests/test_v16_pixel_source.py`，合成数据）：**
- [ ] 按能力矩阵逐位相同；包含部分越界（返回交集和实际起点）和完全越界（`OutOfBounds`）
- [ ] `read_tile` 等于对应矩形的 `read_region`；`scan` 的每一块等于同一窗口的 `read_region`，通道顺序与请求一致；批量读等于逐通道读
- [ ] `level_downsample` 是每轴真实比例；`legacy_level_downsample_rounded` 等于 `RawTileProvider.level_downsample`；`level_shape`、`level_count` 与现有值相同
- [ ] 并发：8 个线程同时 `read_region`、同时跑 2 个 `scan`，结果与串行逐位相同
- [ ] 生命周期：`close()` 之后读报错；`with` 退出时自动关闭
- [ ] `CorrectedZarrSource`：`valid_bounds` 等于 ROI bbox；部分越界返回交集；完全越界抛出 `OutOfBounds`；没有这个 ROI 组或通道时报错；**ROI 外的像素永远不会从原始源读出来**
- [ ] `channel_names` 按规则 (c)：全部有名、某一个缺名（只有那一个变成 `ch_NN`）、数量不符（全部 `ch_NN`）三种情况都对
- [ ] `physical_size()`：有元数据时返回正确的 `(dy_um, dx_um)`，包括 nm 换算成 µm；没有元数据或单位不认识时返回 None
- [ ] `core/pixel_source.py` 不 import `viewer/`、`ui/`、Qt；没有产品模块 import `core/pixel_source.py` 或 `sources/`
- [ ] 反向注入（都会变红）：适配器偷偷转 float32；`level_downsample` 返回取整值；`CorrectedZarrSource` 在 ROI 外用原始像素填；越界时不报错；契约模块 import 了 viewer

**实测（验收探针，test1 副本，按能力矩阵）：**
- [ ] 每个通道、每层 20 个随机窗口：所有层对 `RawTileProvider`；第 0 层对 `TiffTileReader` 和 loader
- [ ] 校正通道：对 `quant_sources` 的读法逐位相同
- [ ] 原始项目指纹不变

**回归**：A2a 只新增文件，所以只跑相关模块（io_loader、quant_sources、raw_tile_provider / viewer 原型、Step4 引擎），失败的模块放到 HEAD 上对比。

**真机**：没有界面变化，**不需要真机验收**（裁定 5）。

## 8. 请用户裁定

1. **`channel_names()` 采用哪种规则**：
   - (a) 与 viewer 的 `RawTileProvider` 相同：只要名字数与通道数不符，全部改成 `ch_NN`；
   - (b) 与 loader 相同：套用 `name_map`，缺名的逐个 `ch_NN`；
   - (c) **（审核提出，v2 采用）**：OME 里有名字的通道保留名字，缺名的那个通道单独记为 `ch_NN`（NN 是它的页序号）；**不套用 `name_map`**。
     我补充一条：当 OME 的 `Channel` 元素数量与页数不一致时，名字和页已经对不上了，这时仍然全部用 `ch_NN`，不去猜。
   - **建议 (c)**：现在是冻结契约的时机，不应把 `RawTileProvider` 的历史行为一起冻结；在今天的数据上三种规则的结果相同（所有通道都有名，`CHANNEL_NAME_MAP` 为空）。
2. **`CorrectedZarrSource` 的坐标与边界**：切片第 0 层的全局坐标，与原始源坐标兼容（但不能互换）；返回请求与 ROI bbox 的交集，交集为空时抛出 `OutOfBounds`，绝不用原始像素填。这与原始源服从同一条越界规则（§3.1）。**建议同意。**
3. **`scan` 只支持第 0 层**，其他层抛出 `NotImplementedError`。**建议同意**：今天只有 Step4 用扫描，而且只读第 0 层。
4. **模块位置**：契约放在 `core/pixel_source.py`（只依赖 numpy）；遗留适配器放在新的小包 `sources/`（`ome_tiff.py`、`corrected_zarr.py`）。`sources/` 是组合层，可以依赖 core 和 `viewer/raw_tile_provider.py`（该文件不依赖 Qt）；将来的 `NgffSource` 也放在这里。这样依赖方向是「消费者 → 契约 ← 适配器」，而不是「core → viewer」。**建议同意。**
5. **不需要真机验收**：没有界面变化，也没有消费者迁移。**建议同意。**
6. **§2.2 第 4、5 条**（校正通道静默退回原始像素、`_corrected_store` 竞态）：A2a 不修，但标为 **「A2c 迁移阻断项」**。A2c 在迁移用到这两条路径的消费者时，必须在它的申请里明确二选一：修掉，或证明与该消费者无关。不能拖到 v17。**建议同意。**

## 9. 用户裁定（2026-09-30）

批准 v2，§8 的 1–6 全部按建议：通道名规则 (c)；校正源用全局坐标、按交集返回、没有交集时报错；`scan` 只支持第 0 层；契约放在 `core/pixel_source.py`，适配器放在 `sources/`；不需要真机验收；§2.2 第 4、5 条列为 A2c 迁移阻断项。
