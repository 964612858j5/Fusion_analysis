# v16 QP：直接读取 PhenoCycler Fusion 原生 QPTIFF（v2，已实现，用户真机验收通过 2026-10-09）

基线：`origin/v16 = ef66476`。本文件只是方案，未改任何代码。

## 1. 只读盘点（2026-10-08）

### 1.1 样本数据
`/sda1/Fusion/Chaofan/2025.12.21_Final_28127_22_Slice2/Scan1/*.qptiff`，旁边的 `.ome.tif` 是 Bio-Formats 8.2 从同一次扫描转出来的。

- **文件结构**
  - BigTIFF 格式，扫描软件为 Fusion 2.4.0。
  - 只用到 `tifffile` 的两个标志：`is_qpi=True`，`ome_metadata=None`。
  - 主图（第 0 组数据）：29 个通道 × 52560 × 28800，uint8。
  - 金字塔共 6 层，每层缩一半；最粗一层是 1642×900，比例约 32.01。
  - 图块是 512×512、LZW 压缩；最粗一层按条带存，不分图块。
  - 另有缩略图、玻片全景、标签三组彩色图，都不使用。
- **通道名**
  - 每个通道的图页各自带一段 XML 说明（声明为 UTF-16），标签是 `PerkinElmer-QPI-ImageDescription`。
  - `<Biomarker>` 是标志物名，例如 DAPI、TOX、TIM3。
  - `<Name>` 是滤光片名，例如 DAPI、ATTO 550、Cy5，不能当通道名。
  - Bio-Formats 转出的 OME 文件里的通道名，与 Biomarker 逐一相同。
- **像素尺寸**
  - 只记在 TIFF 的 `XResolution/YResolution` 标记里，单位是厘米。
  - 换算为 0.50686067 µm，与 OME 文件里的 `PhysicalSizeX` 相同。
- 另外查过 4 个 qptiff，分别有 29、69、56、57 个通道。结构都相同，都没有重名的 Biomarker。

### 1.2 现有代码直接打开 qptiff 的结果（实测，未改代码）

| 接入点 | 结果 |
|---|---|
| `RawTileProvider` / `OmeTiffSource`（A2 契约、查看器按层读图块） | 能打开：6 层，每层图块 512；最粗一层按条带读 |
| 同上，像素对照 | 第 0 层的 `read_region` 与 `read_regions`（`TiffTileReader`，`tiff_tiles` 模式）和 OME 文件逐像素相同；qptiff 第 2 层与 OME 第 1 层也逐像素相同 |
| 同上，元数据 | 通道名全是 `ch_00…ch_28`；`physical_size()` 为 None |
| `OMETIFFLoader`（`core/io_loader.py:41`） | `ET.fromstring(None)` 抛 TypeError。第 0 步加载、第 1 步恢复、Mesmer/Cellpose 工作进程、第 2 步 HQ 全部打不开 |
| `OMETIFFLoader` 按区域读（绕过 `_parse` 的探针） | 全分辨率与 OME 逐像素相同 |
| `read_region_lowres(ds=64)` | 输出形状相同，数值略有差异：qptiff 取到约 32 倍的那一层，OME 取到约 16 倍的那一层（见 §4） |
| `quant_sources._slide_channels`（`:176`） | 报 QuantSourceError "no OME channel names"，第 4 步不能用 |
| `project_identity.describe_slide` / `source_kind` | 能运行：kind 记成 `ome_tiff`（不真实），`physical_size_yx_um=None`，`channel_count` 正确 |
| `workspace_session.find_workspaces` | 按 `slide_id_of → describe_slide` 匹配，本身不依赖格式。`describe_slide` 正确后即可用，无需改动 |
| `ui/batch_step4_dialog.py` | 已能列出 `.qptiff`，后面接 `quant_sources`，随它一起修好 |
| 第 0 步“Browse”对话框（`step0_page.py:6021-6025`） | 过滤条件是 `*.tif *.tiff`，看不到 `.qptiff` |

## 2. 方案（接入现有统一读取路径，不另起数据模型）

原则：
- 只补“认出 qptiff，读出通道名和像素尺寸”。
- 像素读取、查看器调度、缓存、线程、科学算子一律不动。
- 四处 OME 解析逐字保留，只在旁边加 QPI 分支。
- 不加任何新哈希，slide 签名保持 v1 原样。

1. **新模块 `sources/qpi_metadata.py`**（纯函数，不依赖 Qt）
   - `is_qpi(tf)`：只认 `tf.is_qpi and not tf.is_ome`。
   - `qpi_channel_names(tf)`：
     - 取主图第 0 层每个通道页的说明，去掉 XML 声明后解析；
     - 名字取 Biomarker；没有时取 Name；再没有时取 `ch_NN`。
   - `qpi_physical_size_um(tf)`：从第 0 页的分辨率标记读 `(dy, dx)`。单位是厘米或英寸时换算；没有单位时返回 None。
   - `check_qpi_layout(tf)`：
     - 主图轴必须是 CYX，每页单色；
     - 每层都是“每通道一页”；
     - 有任何通道重名时抛 `QpiLayoutError`，并列出重名项；
     - 明场或彩色 qptiff 报出能看懂的错误。
2. **四处元数据接入点**（都是 `if qpi: … else: 原 OME 代码`）
   - `core/io_loader.py` `OMETIFFLoader._parse`：用 QPI 名字生成 `ch_map`，`name_map` 照常叠加；shape 仍取第 0 页。
   - `viewer/raw_tile_provider.py` `_parse_channel_names`：QPI 分支放在现有的宽泛 try/except 之外，结构错误或重名必须报错，不能被吞成 `ch_NN`。
   - `sources/ome_tiff.py` `_load_meta`：通道名和物理尺寸走 QPI 分支。类名不改。
   - `core/quant_sources.py` `_slide_channels`：走 QPI 分支。
3. **项目身份**（`core/project_identity.py`）
   - `source_kind`：数据源是 qptiff 时返回 `"qptiff"`。
   - `transforms_entry` 的来源说明写 `"QPI XResolution/YResolution"`。
   - `slide_signature` 不改，现有 OME 项目的 slide_id 不变。
4. **界面文字**（需用户确认，见 §5）
   - 第 0 步的标签、对话框标题、过滤条件、找不到文件的提示。
   - 不加控件。
5. **测试**（只用合成文件）
   - 新的合成 QPI 生成器：用 `tifffile` 写 `Software=PerkinElmer-QPI`、每页 QPI XML 说明、分辨率标记，页序为“通道页 → 缩略图 → 缩小层 → 全景 → 标签”。生成后先确认 `tifffile` 能认出 `is_qpi`。
   - 覆盖：
     - 名字、物理尺寸；
     - 重名报错、彩色报错；
     - `OMETIFFLoader` 能加载并按区域读；
     - `OmeTiffSource` 与 `RawTileProvider` 名字一致；
     - `_slide_channels`；
     - `describe_slide` 的 kind；
     - `find_workspaces` 能找回 qptiff 的工作区。
   - 现有 OME 测试必须全部照常通过。
6. **文档**：README 和中英文用户指南里写输入格式的那一行，以及本文件的执行记录。

**白名单**：
- 新增：`sources/qpi_metadata.py`
- 修改：`core/io_loader.py`、`viewer/raw_tile_provider.py`、`sources/ome_tiff.py`、`core/quant_sources.py`、`core/project_identity.py`、`ui/step0/step0_page.py`（仅 §5 的文字）、`README.md`、`docs/user_guide.md`、`docs/用户指南.md`、本文件
- 测试：`tests/test_v16_qpi_source.py`（新）+ `tests/test_v16_project_identity.py`（加 qptiff 用例）

预计改动行数：新模块约 100 行，四处接入点各约 5–10 行，`step0_page.py` 约 4 行，`main_window.py` 0 行。

## 3. 回归与验收

- **定向回归**（新旧代码对照）：
  - `test_v16_pixel_source.py`、`test_io_loader_lowres.py`、`test_quant_sources.py`
  - `test_v16_project_identity.py`、`test_v16_a9p_source_parity.py`、`test_v16_a9m_read_ledger.py`
  - workspace_session 相关测试、`test_batch_step4_dialog.py`，以及新测试
- **真实文件对照脚本**（只读）：同一个 qptiff 与 OME，对照通道名、物理尺寸、第 0 层随机区域、Step4 读取器读出的值。
- **真机验收**：
  - 第 0 步用 Browse 选 qptiff，29 个通道名正确，自动选中 DAPI。
  - 概览和浏览正常。
  - 第 1 步预分割（Mesmer）、第 2 步、第 4 步对一个小区域定量。
  - 在同一区域用同一掩膜、同一校正设置和通道，与 OME 项目的第 4 步结果对照，应当一致。
  - 重开项目后，能按 slide_id 找回 qptiff 的工作区。

## 4. 已有问题（不在本块修，列为 advisory，纳入真机检查）

1. 概览取层：`overview_downsample` 对两种文件都给 64。OME 最粗一层的比例约 64.02，超过 64，所以落到 16 倍层；qptiff 落到约 32 倍层。概览的初始对比度会略有不同，但没有出错。
2. 第 1 步按层四舍五入（`step1_viewer_host.py:108`、`step1_source.py:699`）：两种文件最粗层都只覆盖到 52544 行（图像共 52560 行），底边约 16 行可能对不齐原图与校正图。OME 现在就有这个问题。
3. 同一张片子，用 qptiff 打开时 slide_id 和 OME 不同，是一个新的数据源，旧工作区和校正结果不能复用。这是预期行为，写进用户指南。

## 5. 请用户裁定

1. 按 §2 的范围和白名单启动。
2. 第 0 步界面文字（界面是英文）：
   - 标签 `OME-TIFF:` → `Slide:`
   - 对话框标题 `Select OME-TIFF` → `Select slide image`
   - 过滤条件 `OME-TIFF (*.tif *.tiff)` → `Slide images (*.qptiff *.tif *.tiff)`
   - 找不到文件的提示 `OME-TIFF not found` → `Slide image not found`
   - 也可以只改过滤条件，其他文字不动。

## 6. codex 方案审查与修订（v2，2026-10-08）

codex（astra low）结论：只补元数据的方向符合 HEAD；四处元数据接入点已经齐全，生产代码不需要扩围；也不需要新的哈希。以下各条已逐一对照代码核实。

**必须修订（已并入方案）**

1. **合成 QPI 文件要做成真正的 QPI 金字塔。** `tifffile` 识别 QPI 的规则（`_series_qpi`）是：
   - 先把开头形状相同的连续页当作主图；
   - 紧跟的下一页当作缩略图；
   - 主图是图块存储时，才往后按 `//2` 依次找缩小层，而且每层页数必须等于通道数。

   所以合成文件要满足：普通顶层 IFD（不用 SubIFD）、`metadata=None`、通道页都是单色图块、必须有一张不同形状的彩色缩略图。缺了缩略图，第一张缩小层会被当成缩略图吃掉。codex 已在内存里验证过一个“2 通道 × 3 层”的样例。
2. **验收要覆盖已有契约，不只是名字。** 这些都是已有契约，不是新增机制：
   - `OMETIFFLoader`：shape、通道顺序、`name_map`、不归一化的区域读、`read_region_lowres`；
   - A2：每一层的区域读和图块读，包括奇数尺寸、裁到边界的图块、按条带存的最粗层；注入 provider 的模式；第 0 层 `read_regions`/`scan` 与 Step4 读取器一致；
   - `describe_slide` 的完整输出：fingerprint、通道数、各层 shape、物理尺寸、slide_id 稳定，且 OME 的 slide_id 不变；
   - `find_workspaces` 按 slide_id 匹配：把 qptiff 复制或移到另一个路径后，仍能找回原来的工作区。
3. **下游路径显式加测试，不改代码。**
   - `utils/channel_cache.py:98,119`
   - `workers/hq_source_resolver.py:207,293`
   - `workers/mesmer_worker.py:117`、`workers/cellpose_worker.py:426`（工作进程初始化 Loader）
   - 批量第 4 步：用一个工作区记录的是 qptiff 的 run 来提取
4. **细化范围。**
   - 新测试文件定名为 `tests/test_v16_qpi_source.py`，合成文件生成器放在同一文件里。
   - 结构检查和重名检查统一由 `check_qpi_layout` 执行，四个入口都调用。重名按最终名字判断，也就是回退到 Name 或 `ch_NN` 之后再比。
   - `transforms_entry` 只在 `desc["kind"] == "qptiff"` 时写 QPI 来源说明，OME 的写法不变。

**advisory（不改代码）**

- **批量第 4 步对话框**：`_find_ome_tiff` 按文件名排序后取第一个匹配。同一个 Scan 目录里同时有 `.ome.tif` 和 `.qptiff` 时，显示的是 OME 那个。不过真正提取用的是掩膜所在 run 记录的源图（`batch_step4_dialog.py:491`、`quant_sources.py:269`），所以只影响显示。要改选择规则需另行批准。
- **缓存的数据源描述**：`describe_slide` 命中缓存时直接返回旧描述。qptiff 在本块之前无法完成第 0 步保存，不会有旧的 qptiff 描述，所以不做迁移。
- **工作区查找吞掉错误**：`workspace_session.slide_id_of` 会捕获异常。重名的 qptiff 在这里只会打印一行并返回“没有工作区”，真正的报错出现在第 0 步加载时。
- **诊断和基准脚本**：例如 `scripts/probe_step4_kernels.py`，里面也有只认 OME 的读法。它们不在产品路径上，本块不顺手改。

## 7. 执行记录（2026-10-08，用户授权“先跑任务1”）

**用户裁定**：按 §2 范围启动。界面文字用户未选，按较小的改法：只改过滤条件，变为 `OME-TIFF / QPTIFF (*.tif *.tiff *.qptiff)`；标签、标题和提示不动。

**与方案的偏差**（codex 审查认可）：
1. 新模块放在 `core/qpi_metadata.py`，而不是 `sources/`。按现有分层，`viewer/` 和 `core/` 只依赖 `core/`，`sources/` 依赖它们；放在 `sources/` 会造成反向依赖。
2. 结构检查和重名检查没有单独对外的 `check_qpi_layout`，并在 `qpi_channel_names` 里统一执行（先回退取名，再判重名）。三个入口都通过它：Loader、RawTileProvider、`_slide_channels`；`OmeTiffSource` 也调用它。
3. `OmeTiffSource` 新增公开方法 `source_format()`，返回 `"ome_tiff"` 或 `"qptiff"`，只供 `project_identity.source_kind` 使用。

**改动**：
- 新增 `core/qpi_metadata.py`（103 行）
- `core/io_loader.py` +7/−1
- `viewer/raw_tile_provider.py` +3
- `sources/ome_tiff.py` +18/−2
- `core/quant_sources.py` +3
- `core/project_identity.py` +6/−4
- `ui/step0/step0_page.py` +1/−1（第 6025 行，过滤条件）
- `ui/main_window.py` 0
- `README.md`、`docs/user_guide.md`、`docs/用户指南.md` 各 1 行
- 新测试 `tests/test_v16_qpi_source.py`（18 项）

**codex 代码审查**：没有必须修的问题。5 条测试类 advisory 已补上：
- `ch_NN` 回退；
- 回退后造成的重名；
- 每层被裁到边界的角图块；
- 工作进程同款的 Loader 初始化；
- OME 的 slide_id 前后对照（见下）。

“批量第 4 步完整提取”仍只测发现和 `JobReader` 读值，因为提取走的是同一个 `resolve_quant_job` + `JobReader`。

**定向回归**：
- 新代码：12 个相关测试文件全部通过（pixel_source 49、io_loader_lowres 28、quant_sources 29、project_identity 37+2 skip、a9p 8、a9m 2、hq_resolver 23、batch_step4 6、a6_workspace 19、artifact_graph 21、object_tables 15、label_pyramid 12），新测试 18 项通过。
- 旧代码（ef66476 的副本）：同样 12 个文件，结果相同。只有 artifact_graph 6 个 error、object_tables 1 个 failed，原因是旧代码副本不在 git 仓库里，测试调用 git 时出错，与本块无关。

**真实文件对照**（只读，`Chaofan/2025.12.21_Final_28127_22_Slice2`，qptiff 与 OME 对比）：
- Loader 的 29 个通道名和 shape 相同；
- 6 个随机 1500×1500 区域的像素相同；
- `OmeTiffSource` 的名字和物理尺寸（0.50686 µm）相同；
- 第 4 步批量读取相同；
- `_slide_channels` 相同；
- `describe_slide`：kind=qptiff，6 层，用时 0.2 s。
- OME 文件的 slide_id，新旧代码完全相同：真实文件 `slide_b4ea09028512c807`，合成文件也相同；物理尺寸来源说明也不变。

**真机验收**：2026-10-09 用户验收通过，并授权推送。
