# v16 块 A3 + A4 — 坐标与身份契约、出处记录与 artifact graph v0、对象层 v1：实施申请 v2

日期：2026-10-01。分支 `v16`，调查基于 HEAD `37b11db`（与 `origin/v16` 一致；工作区只有两份未跟踪的旧计划及其 `:Zone.Identifier`，不动）。

依据：
- 合并基准 v2.3 + v2.3.1（`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.3_consolidated.md`）：
  - §2.4 最小布局 v1（「A0 起草，A3 冻结」）；
  - §6 A3 / A4、§6.8 `project_schema_version` / `artifact_id` / `depends_on` / `operates_on`；
  - 退出条件 E2、E3，门 4、5、7；
  - v2.2 变更表第 9、10、13–15、17 行；
  - §12 排期（A3 + A4 约 3–4 天，artifact graph v0 从 `provenance.json` 读取）；
  - §18 第 5 项：A3 可提的最小机制是 artifact graph v0（只读）。
- v2.4（已批准）§5.1a 的契约补丁 P2、P3、P4（P1 已在 A2c 完成）。
- A0 契约草案 `docs/v16_contracts_draft.md`（§2 坐标、§3 身份与对象表，均「在 A3 冻结」）。
- A2a / A2c 的执行记录（`docs/v16_A2a_application.md`、`docs/v16_A2c_application.md` §11）。

状态：**申请 v2，用户 2026-10-01 批准**（§12：13 题全部按建议）。按 v2.3 §0.4 规则 1 开始实施。

修订记录：
- v1（2026-10-01）：初稿。
- v2（2026-10-01）：按独立审核意见逐条评估后修订（评估见 §11；审核意见本身不构成授权）。改动包括：
  - `cells/regions_parquet` 的 `depends_on` 只放 artifact id（§3.5）；
  - 写明 `corrected_channel` 的 `operates_on` 几何，以及 `type` 的枚举（§3.1、§3.5）；
  - 规定多边形的规范化方法（§3.1）；
  - manifest 和 `transforms.json` 改为原子写（§3.4）；
  - 写明 `segmentation_run_id` 冲突时的行为，以及 `location.path` 统一用 `/`（§3.1、§3.5）；
  - 写明 A4-G3 失败时的处理方式（§7.3）；
  - §8 第一条风险改写得更直白。
  - 只有一条建议**未采纳**：持久化 `native_tile_shape_per_level`，理由见 §11。

---

## 1. 必要性与目标

**E2「空间与身份收敛」**：
- 对 `slide_id`、`region_id`、`segmentation_run_id`、`cell_id` 只有一种解释；
- 对 `global_pixel`、`viewer_world`、`physical_um`、`region_local`、`pyramid_level` 之间的变换和 bbox 约定只有一种解释；
- 加上 `project_schema_version`。

这样 TMA 不会再造第二套坐标或身份系统。

**E3「出处可由机器追溯」**：每个重要产物都有 `artifact_id`、`depends_on`、`schema_version` 和参数 / 软件出处。机器能回答：「这个 Step4 结果用了哪个分割、哪个 fusion、哪些校正通道？」

**A4「对象层 v1」**：从一个真实结果生成 `cells.parquet` 和 `regions.parquet`，键和坐标与 LabelStore、viewer 一致。

用一个例子说明要达到的效果。今天 test1 副本里有一个 Step4 结果：
`rois/full_wsi_20260927_121444_6bad/step4/quantification_runs/seg_20260927_124316_stardist_nuclei_expansion/Full_WSI/cell_features.h5ad`。

要回答「它用了哪个 fusion」，今天得人工串起四个文件的绝对路径：
1. Step4 出处里的 `segmentation_run.run_dir`；
2. 该运行的 `segmentation_meta.json` 里的 `rois[0].fused_zarr_path`；
3. 该 fused zarr 的 attrs；
4. `step0_roi_result.json` 的 `corrected_zarr_path`。

这四个文件的版本互不约束：fused zarr 是共享目标，重新 fusion 会把它覆盖掉。A3 之后，这条链由 `depends_on` 中的 artifact id 构成，`scripts/artifact_graph.py` 可以直接查询。

**A3 + A4 不改任何科学算法、不改 Step0–4 的输出文件内容、不改 UI、不移动现有的落盘目录。**

---

## 2. 只读调查结果

### 2.1 已有的身份与坐标契约（A2a / A2c 留下的）

| 事实 | 位置 |
|---|---|
| `PixelSourceIdentity` 是运行时 / 存储身份（路径 + `size:mtime_ns` + stage），`slide_id` 字段**保留未定义**，「never inferred here」 | `core/pixel_source.py:58-72` |
| `level_downsample(level)` = `(H0/H_L, W0/W_L)`，逐轴、不取整 | `core/pixel_source.py:111-115` |
| `valid_bounds(level)`：源真正拥有的范围；`read_region` 返回交集，没有交集就抛 `OutOfBounds` | `core/pixel_source.py:118`、模块说明 |
| `physical_size()` 返回 `(dy_um, dx_um)` 或 None，从不猜测 | `core/pixel_source.py:181-184`；OME 解析在 `sources/ome_tiff.py:53-72` |
| `native_tile_shape` / `native_tile_origin` / `read_native_tile`（P1） | `core/pixel_source.py:160-172`；OME-TIFF `sources/ome_tiff.py:141-143`；校正 zarr 的网格从 bbox 原点起算 `sources/corrected_zarr.py:130-132` |
| 通道名规则 (c) | `sources/ome_tiff.py:37-50` |
| `legacy_level_downsample_rounded` 只用作缓存键 | `sources/ome_tiff.py:145-149` |
| 校正 zarr：全局坐标，bounds = ROI bbox | `sources/corrected_zarr.py:47-56`、`:135-142` |
| `tests/test_v16_pixel_source.py` 的允许名单 `MIGRATED` | `tests/test_v16_pixel_source.py:96-102` |

实测 test1 切片 `cropped_region.ome.tif`：
- 29 页，uint8；
- 3 层：(15437, 16215) → (3859, 4053) → (964, 1013)；
- `PhysicalSizeX/Y` = 0.50686 µm；
- OME-XML 里有 `UUID="urn:uuid:4e2590e9-…"`（Bio-Formats 7.0.1 写入）；
- 文件 929 834 060 字节，完整 sha256 约 1.9 s（页缓存热）。

### 2.2 项目与工作区（`utils/roi_project.py`）

- `roi_id`：
  - ROI 模式：`roi_<YYYYmmdd_HHMMSS>_<4 位十六进制>`（`:41-42`）；
  - Full WSI 模式：`full_wsi_<…>`（`:45-46`）；
  - 按本地时间生成，不检查冲突。
- `create_roi_context` / `create_full_wsi_context`（`:149-210`）每次都**新建**一个工作区目录。
  - 写入 `roi_manifest.json` 和工作区级的 `roi_index.json`，再更新项目级的 `roi_index.json`，并把 `active_roi_id` 设成新工作区（`:143`）。
  - 唯一的产品调用方是 Step0 Save（`ui/step0/step0_page.py:11069-11073`）。同一会话里分析区域不变就复用（`:11045-11058`，签名见 `:6398-6407`）；新会话第一次 Save 一定新建。
  - 所以 test1 副本的 `roi_index.json` 有 **34 个工作区**，全部叫 "Full WSI"，bbox 都是 `[0, 15437, 0, 16215]`。
- **一个工作区可以有多个 ROI，但只有第一个有 `roi_id`**：`_standard_rois` 只给 `idx == 1` 的 ROI 写 `roi_id` / `roi_dir`（`ui/step0/step0_page.py:11328`、`:11344-11346`）。其余 ROI 只有 `name`。
  - 运行内部按显示名做键：`label_store["Full WSI"]`、`global_mask_Full WSI.zarr`、`segmentation_meta.rois[i].roi_name`。
  - 结论：**`roi_id` 实际上是「工作区 id」，不是「区域 id」**。A0 草案 §3.1 写的「`region_id` ← `roi_id`」在多 ROI 工作区里不成立，在 Full WSI 上还会把同一个区域拆成 34 个。
- `project_manifest.json`（`ensure_project_manifest`，`:93-104`）：
  - 已经有一个 **`"version": 1`**（`setdefault`，从来没人检查）；
  - 每次新建工作区都会把 `source_ome` **覆盖**成当前切片（`:101-102`）。所以同一个项目目录里可能有来自不同切片的工作区：每个 `roi_manifest.json` 记着自己的 `source_ome`。
  - 读-改-写方式会保留未知键，所以旧代码遇到新增字段不会把它删掉。
- `save_json`（`:87-90`）不是原子写。

### 2.3 现有的版本字段与读取者（子代理调查，已抽查）

| 字段 | 写入 | 读取者遇到未知值时 |
|---|---|---|
| `project_manifest.version = 1`、`roi_index.version = 1` | `utils/roi_project.py:97`、`:142` | 没有读取者检查 |
| `step0_roi_result.handoff_schema_version = 2` | `core/step0_handoff.py:242-297` | `ui/main_window.py:2963-2967`：任何整数都接受，`≥ 2` 按 v2 处理；不是整数就拒绝 |
| `step1_fusion_settings.version = 2` | `ui/main_window.py:8478` | 精确匹配，不等就拒绝（`:8409`） |
| `preseg_contract.version = 1` | `core/preseg_contract.py:73-86` | 不等于 1 就抛 `ContractError`（`:95-101`） |
| Step4 出处 `schema_version = 1` | `workers/feature_extract_worker.py:151-206` | 产品里没有读取者 |

计划要求「`project_schema_version` 遇到未知版本时显式报错」，现在没有任何项目级的检查。

### 2.4 产物生产者清单（今天的出处信息）

| 产物 | 生产者（提交点） | 今天记录的上游 | 原子性 |
|---|---|---|---|
| 原始切片 | 无（外部输入） | `project_manifest.source_ome`（路径）、`step0_roi_result.source_identity`（`size:mtime_ns`，`core/step0_handoff.py:213-225`） | — |
| 校正通道 `corrected_channels.zarr/<group>/<channel>` | `core/step0_handoff.write_handoff`（`:109`；发布在 `:318-323`） | 每个数组的 attrs：`source_identity`（uuid 令牌）、`written_at`、方法、参数、`bg_correction_algo_version`（`core/bg_correction.py:408+`）；根 attrs `source_ome` | handoff 的 JSON 先写临时文件再 `os.replace`；数组由增量 Save 写 |
| Step1 fused zarr `fused_<roi>.zarr` | `FullFusionWorker`（`ui/step0/overview_panel.py:242`；attrs 在 `:576-588`，`complete` 在 `:708`，`_publish_store` 在 `:711`）；`fusion_meta.json` 由 worker（`:733-737`）和 `ui/main_window.py:8992-9039` 两处写 | attrs：`created_at`、`config_hash`、`fusion_formula_version`；meta 里的 `source_path`（路径）。**不记录读了哪些校正通道的哪个版本** | 可恢复发布（先挪开再 `os.replace`），meta 不是原子写 |
| Step2 分割运行 `segmentation_runs/<run_id>/` | `workers/segment_merge_worker.py`：ROI 模式在 `:3484-3495`，单区域路径在 `:4206-4220`；`run_id = seg_<时间>_<方法>[_NN]`（`:258-275`） | `segmentation_meta.json`：`rois[i].{roi_id, roi_name, bbox_fullres, fused_zarr_path}`、`seg_engine.{identity, provenance}`（A0.5）、`seg_config.preseg_contract`。上游只有**路径**，没有令牌 | meta 不是原子写 |
| Step4 `cell_features.{h5ad,csv}` + `_provenance.json` | `workers/feature_extract_worker.run_extraction`（`:208-289`；`*.partial` → `os.replace` 在 `:262-275`） | `_provenance`（`:151-206`）：`segmentation_run.run_id/run_dir`、`label_store.path`、`slide`、`step0.{handoff, corrected_product, decisions}`、`channels[]`（校正通道带 `identity`，即数组的 `source_identity` 令牌，`core/quant_sources.py:377-383`）。**不记录 fused**（Step4 不读 fused） | 是 |
| Step1 预分割运行 `presegmentation_runs/<id>/` | `core/preseg_run.py:127-176`（`publish_json`） | `engine_identity` / `engine_provenance`；它的契约哈希进入 Step2 的 `seg_config.preseg_contract` | 是 |
| `corrected_coarse.zarr` | **只由脚本** `scripts/build_corrected_coarse_l3.py` 写（`:97`），viewer 读（`viewer/step1_source.py:77`） | — | — |
| Step3 | 没有落盘生产者 | — | — |

两个关键事实：
- **Step4 h5ad 与 fused 之间本来就没有直接依赖**。链是 Step4 → 分割运行 → fused → 校正通道。
- **fused zarr 是共享目标**（一个工作区一个 `fused_<roi>.zarr`，重新 fusion 会覆盖）。旧的分割运行指向的路径之后可能装着另一份 fusion。这属于 E5 / A6 的问题，A3 不修。但出处记录必须能把它**如实说出来**，而不是按路径猜。

### 2.5 比较工具与 Step4 输出的逐位约束

- `scripts/diagnose_v16_a2c_oracle.py` 的 `step4` / `fusion` 子命令在副本上用当前检出的代码重跑；`same-step4` / `same-fused` 复用 `scripts/probe_v16_a2b_ngff.py:615-641`、`:715-736`。
- `same-step4` 比较 h5ad 里**所有数据集的集合**以及每个数据集的值，只排除 `uns/provenance_json`（`PROVENANCE_ONLY`，`:612`）；CSV 整表比较。
  - 所以 h5ad 里**新增任何数据集**（哪怕在 `uns` 下）都会被报为不同。
  - `cell_features_provenance.json` 旁车文件不在比较范围内。
- `same-fused` 只比较数组，不比较 `.zattrs`。
- Step4 的质心是 `float64(Σy_local / n) + oy`（`core/quant_engine.py:439-440`、`:466-467`）；bbox 的 max 是最后一个下标 + 1，即半开区间（`:470-471`）。

### 2.6 依赖

- `fusion_mesmer` 环境里**没有 pyarrow**（也没有 polars / fastparquet）。
- `pip install --dry-run pyarrow` 只会装 `pyarrow 25.0.1` 一个包，不动 numpy 1.26.4 / pandas 2.3.3 / anndata 0.11.4。
- A0.5 已经完成：装新包只改变记录用的 `env_lock_hash`，不改变比较用的引擎身份（v2.3 §3）。

### 2.7 GUI 的错误路径（与「显式报错」相关）

- Step4 页把 `QuantSourceError` 显示给用户（`ui/step4_page.py:69`、`:89`；worker 在 `workers/feature_extract_worker.py:351`）。
- Step0 Save 调用 `create_*_context` 时**没有 try**（`ui/step0/step0_page.py:11067-11073`）。主程序也没有安装 `sys.excepthook`。PyQt5 遇到槽函数里未处理的异常会直接终止进程。所以**不能在 Save 路径上抛新异常**。

---

## 3. 设计（冻结的契约）

本节是 A3 冻结后的契约文本。实施时写入 `docs/v16_contracts_draft.md`（改名为冻结版，§2、§3 的「草案」字样去掉），代码里只实现它。

### 3.1 身份

| ID | 定义 | 例子（test1） | 谁生成 |
|---|---|---|---|
| `slide_id` | `slide_` + sha256(**切片签名 v1**) 的前 16 位十六进制。签名 v1 依次包含：文件字节数、OME-XML 原文、各层形状与 dtype、文件**开头和结尾各 4 MiB** 的字节。算法名 `slide_sig_v1` 和 `slide_id` 一起记录 | `slide_3f…`（实施时实测） | `core/project_identity.slide_id(path)`；按 `size:mtime_ns` 缓存在 `project_manifest.sources` 里，指纹不变就不重算 |
| `workspace_id` | **就是今天的 `roi_id`**（`full_wsi_20260927_121444_6bad`）。它是一次 Step0 Save 建出的工作目录，不是区域 | `full_wsi_20260927_121444_6bad` | 不变（`utils/roi_project.py:41-46`） |
| `region_id` | `reg_` + sha256(`slide_id`, `type`, `bbox_fullres`, 规范化的 `polygon_fullres`) 的前 16 位十六进制。**由几何确定，不用显示名，也不用工作区** | 34 个 "Full WSI" 工作区 → **同一个** `reg_…`；画两个 ROI 的工作区 → 两个 `reg_…` | `core/project_identity.region_id(...)`；对旧项目也能**只读地算出来**，不需要写入 |
| `segmentation_run_id` | **就是今天的 `run_id`**（`seg_<时间>_<方法>[_NN]`），不改名，Step3 / 4 照常工作。要求在项目内唯一。冲突时的处理见表下 | `seg_20260927_124316_stardist_nuclei_expansion` | 不变 |
| `cell_id` | LabelStore 里的细胞标签 1..N | `1`…`56874` | 不变 |
| 稳定的细胞键 | **`(segmentation_run_id, region_id, cell_id)`** | — | — |

**`type` 的取值**（照 §6.5 冻结）：
- 今天会出现的：`full_wsi`、`roi`（来自 `roi_manifest.type` / `analysis_region_type`；没有 `type` 的旧 ROI 按 `roi` 处理）。
- 保留给以后的：`TMA_core`、`tumor_region`、`blur`、`fold`、`niche`、`manual_annotation`。
- 不认识的值一律报错，不归入别的类。
- **没有 `correction_group` 这一类**：校正组不是区域，见 §3.5 的 `corrected_channel` 一行。

**多边形的规范化**（`region_id` 哈希的输入；`bbox_fullres` 按整数 `[y0, y1, x0, x1]` 进入哈希）：
1. `null`、`[]`、少于 3 个不同顶点的多边形，一律视为「没有多边形」，记作 `null`。（今天同一个 Full WSI 区域在 manifest 里写 `null`，在校正组 attrs 里写 `[]`：`utils/roi_project.py:195`、`core/step0_handoff.py:97`。)
2. 顶点是全分辨率下的 `(x, y)`（`ui/step0/overview_panel.py:457`），先转成 float64，再四舍五入到 1e-3 px。
3. 去掉连续重复的顶点。末尾如果有与第一个顶点相同的闭合点，也去掉。
4. 方向统一为逆时针（在 (x, y) 坐标系中有符号面积 > 0）；顺时针的就反转顶点顺序。
5. 起点取字典序最小的 `(x, y)` 顶点，再循环移位到它。
6. 把结果序列化为 JSON：数字固定写成 3 位小数，不加空格。

例子：同一个三角形，按 A→B→C、B→C→A、C→B→A（反向）三种画法，或带一个闭合点，规范化后完全相同，`region_id` 也相同。只要移动一个顶点超过 0.0005 px，`region_id` 就会变。

**`segmentation_run_id` 冲突**（项目里另一个工作区已经有同一个 `run_id`；运行按秒命名、依次启动，实际上几乎不会发生）：
- 照常登记。`artifact_id` 本身用 uuid，不会冲突；条目里加 `flags: ["segmentation_run_id_collision"]`，同时记日志。
- graph 报告这个标记；`build_object_tables` 遇到有冲突的 run id 时拒绝生成 `cells.parquet`，因为这时细胞键 `(segmentation_run_id, region_id, cell_id)` 不再唯一。
- 不会悄悄变成「未登记」。

为什么 `region_id` 由几何确定：
- **问题**：34 个 Full WSI 工作区描述的是同一块组织，但今天它们各有一个 `roi_id`；而多 ROI 工作区里，除第一个外的 ROI 根本没有 id（§2.2）。
- **几何哈希的好处**：同一个地方只有一个 id；旧项目不用迁移就能算出来；改显示名不会改 id。
- **TMA**：core 是 `type = "TMA_core"` 的区域。core 的边界被手工修正后，几何变了，于是得到新的 `region_id`。这是正确的：旧的分割是在旧边界上做的。core 的「名字」（例如网格位置 A3）是 `name` 属性，由 TMA Foundation 的 core identity 处理，不属于像素身份。

层级 `Study └─ Sample └─ Slide └─ Region` 照 §6.1 冻结：`sample_id` / `study_id` / `patient_id` 是队列元数据，A3 只保留 schema 位置，不生成值。

### 3.2 坐标（照 §6.2 和 A0 草案 §2 冻结）

| 名称 | 定义 |
|---|---|
| `global_pixel` | 切片第 0 层的像素下标；**整数下标 = 像素中心** |
| `pyramid_level` | 第 L 层像素下标。L 层像素 i 的中心在 global 中位于 `(i + 0.5) · s_L − 0.5`，其中 `s_L = (H0/H_L, W0/W_L)` 逐轴取真实比例。test1：L1 = (4.000259, 4.000740)，L2 = (16.013485, 16.006910) |
| `physical_um` | `global_pixel · (dy_um, dx_um)`，只来自 OME `PhysicalSizeY/X`；没有就标为**不可用**（`null`），绝不假设 mpp |
| `region_local` | `global_pixel − (bbox_y0, bbox_x0)` |
| `tile_local` | 分块内部下标 = global − 分块原点（分块原点用 `native_tile_origin` + 块号 × `native_tile_shape`） |
| `viewer_world` | `world = global_pixel + 0.5`（像素 j 覆盖 [j, j+1)）；拾取 `global = floor(world)`。这是**适配规则**，不改旧 viewer 代码 |

其余约定：
- 轴顺序：数组 (y, x)，点 (x, y)。schema 每个字段写明是哪一种。
- bbox 半开：`[x0, x1) × [y0, y1)`；旧字段 `bbox_fullres = [y0, y1, x0, x1]` 不改顺序，映射为 `bbox_x0 = [2]`、`bbox_y0 = [0]`、`bbox_x1 = [3]`、`bbox_y1 = [1]`。
- 约束范围：「不准悄悄写 `x / 2**level`、`x − bbox_x0`、`x × mpp`」只约束新代码和 TMA 代码（裁定表第 9 行）。A3 / A4 的新代码一律经过 `core/project_identity` 的变换函数。
- 已知的不一致（只列出、不改）：`ui/step0/step0_page.py:11364` 判断 patch 中心时用闭区间；`image_mpp = 0.5` 写死（A0 草案 §2.5）。

`transforms.json`（项目根，**按 `slide_id` 分组**，因为一个项目目录里可能有不同切片，§2.2）：

```json
{
  "schema_version": 1,
  "slides": {
    "slide_3f…": {
      "global_pixel": {"shape_yx": [15437, 16215], "center_convention": "integer index = pixel centre"},
      "physical_um": {"available": true, "size_yx_um": [0.50686…, 0.50686…], "source": "OME PhysicalSizeY/X"},
      "pyramid_levels": [
        {"level": 0, "shape_yx": [15437, 16215], "scale_yx": [1.0, 1.0]},
        {"level": 1, "shape_yx": [3859, 4053], "scale_yx": [4.000259134490801, 4.000740192449297]},
        {"level": 2, "shape_yx": [964, 1013], "scale_yx": [16.013485477178424, 16.006910167818362]}
      ],
      "viewer_world": "world = global_pixel + 0.5"
    }
  },
  "region_local": "global_pixel - (bbox_y0, bbox_x0) of the region (regions.parquet / the workspace's roi_config)"
}
```

和 A0 草案相比的一处改动：区域原点**不再**复制进 `transforms.json`。区域的 bbox 只有一处真相（Step0 提交的 `roi_config` / `roi_manifest`，A4 的 `regions.parquet` 是它的表格形式）。

### 3.3 P2 — 原始数据源描述（持久化）

`project_manifest.json` 新增 `sources`（按 `slide_id` 分组）；同样的内容也进入该切片 `raw_slide` 产物的出处条目：

```json
"sources": {
  "slide_3f…": {
    "kind": "ome_tiff",
    "path": "/home/ming/fusion_data/cropped_region.ome.tif",
    "fingerprint": "929834060:1790343262479724500",
    "slide_id_method": "slide_sig_v1",
    "level_count": 3,
    "level_shapes_yx": [[15437, 16215], [3859, 4053], [964, 1013]],
    "coarsest_level_shape_yx": [964, 1013],
    "dtype": "uint8",
    "channel_count": 29,
    "physical_size_yx_um": [0.50686…, 0.50686…]
  }
}
```

- 层数、层形状、物理尺寸都通过 `OmeTiffSource` 读取（`PixelSource` 上已有）。`kind` 由描述函数按源的类型决定，今天只有 `"ome_tiff"`；遇到不认识的源类型就报错，不写默认值。
- 旧字段 `source_ome` 保留不动（旧读取者用它）。

### 3.4 `project_schema_version`

- 写在 `project_manifest.json`，键名 **`project_schema_version`**，值为整数 `1`。旧的 `"version": 1` 保留、不改含义（它从来没人检查）。
- 读取规则（`core/project_identity.read_project_schema(manifest_path)`）：

| 文件里 | 结果 |
|---|---|
| 没有这个键（旧项目） | `LEGACY`：照今天的方式读。不期待出处记录；graph 报告「旧项目，没有出处记录」 |
| `1` | 当前版本 |
| 其他整数（如 `2`）、字符串 `"1"`、布尔值、负数 | 抛 `ProjectSchemaError`，错误信息写明文件路径、读到的值和本程序支持的版本，**不猜** |
| `project_manifest.json` 本身不是合法 JSON | 抛 `ProjectSchemaError` |

- **写入规则（旧项目「采用」，不迁移；裁定 5）**：新代码第一次向项目写入（`ensure_project_manifest`）时：
  - 键不存在 → 写入 `project_schema_version: 1`；
  - 值是 1 → 不变；
  - 值未知 → **不覆盖、不降级**，本次不登记出处，并记日志。
  - 已有的产物一律不改写、不补登记，它们在 graph 里是「未登记的旧输入」（§3.5）。
- **原子写（v2）**：
  - `ensure_project_manifest` 写 `project_manifest.json`，以及新增的 `transforms.json`，都改用新函数 `core/provenance.write_json_atomic`：先写同目录临时文件，`flush` + `fsync`，再 `os.replace`。这与 `core/step0_handoff._write_json_staged`（`:62-69`）的做法相同。
  - Save 路径中途崩溃时，旧文件保持完整，不会留下半个 manifest。
  - `utils/roi_project.save_json` 本身和它的其他调用方（`roi_index.json`、`roi_manifest.json`）不改。
  - 今天的 manifest 如果已经损坏，`load_json` 会抛异常；这个行为维持原样，A3 不处理。
- 在哪里显式报错（裁定 10）：
  - Step4：`resolve_quant_job` 增加一条检查，未知版本时抛 `QuantSourceError`，经现有的 Step4 页错误显示给用户，不新增 UI；
  - `scripts/artifact_graph.py`、`scripts/build_object_tables.py`：退出码非 0，并打印错误；
  - 出处登记（各生产者）：拒绝登记，记日志；
  - Step0 Save 路径**不抛异常**（§2.7）：`ensure_project_manifest` 遇到未知版本时只是不改这个字段并记日志，Save 本身的行为与今天一样。
- Step0 交接里已有的 `handoff_schema_version = 2` 保持不变（§6.8）。

### 3.5 出处记录与 artifact graph v0（§6.8，P3）

**存放（裁定 3）**：项目根下的 `provenance/` 目录，每个产物一个文件 `provenance/<artifact_id>.json`。
- 写法：先写临时文件再 `os.replace`。只新增，从不改写，所以不需要锁。几个生产者可以同时登记（例如 fusion 与 Step4 同时跑），在 Windows 上也一样安全。
- 计划里的「`provenance.json`」指的就是这组记录。不用单个文件的原因：多个生产者对同一个文件做读-改-写，必须加跨进程锁；单文件原子替换也挡不住两个写者互相丢条目。

**条目 schema（`schema_version: 1`）**：

```json
{
  "schema_version": 1,
  "artifact_id": "art_fused_9c1e…",
  "kind": "fused",
  "created_at": "2026-10-02T10:11:12",
  "workspace_id": "full_wsi_20260927_121444_6bad",
  "slide_id": "slide_3f…",
  "location": {"path": "rois/full_wsi_…/step1/fused_Full WSI.zarr", "relative_to": "project"},
  "token": "2026-09-27T12:42:53.210575|<config_hash>",
  "operates_on": ["reg_…"],
  "depends_on": ["slide_3f…", "art_corrected_channel_…"],
  "unresolved_inputs": [],
  "parameters": {"fusion_formula_version": 2, "config_hash": "…"},
  "software": {"git_commit": "…", "engine_provenance": null}
}
```

- `artifact_id`：`art_<kind>_<uuid4 前 12 位十六进制>`。`raw_slide` 例外，它的 `artifact_id` 就是 `slide_id`（同一张切片在哪个工作区登记都是同一个节点）。
- `token`：产物自己的版本令牌，用于**按「位置 + 令牌」查找**并保证登记幂等：同一个位置、同一个令牌再登记一次，返回已有的 id。每种产物的令牌见下表。
- `depends_on` 只放 artifact id。校验器拒绝任何不是已登记 artifact id 的值（例如 `reg_…`、路径、空串）。
- `operates_on` 只放 `region_id`（`reg_…`）。两者从不混用。
- `unresolved_inputs: [{role, path}]`：消费者读了一个**没有登记过**的上游（例如 A3 之前做的分割运行）。这时只如实写下角色和路径，**不猜它是哪个 artifact**。
- 位置在项目内时写相对项目根的路径，在项目外（切片）时写绝对路径。这样复制项目后记录依然有效。
  - 相对路径**一律用 `/` 分隔**，Windows 上也一样：写入时 `PurePath(...).as_posix()`，查找时按同样规则规范化后再比较。
  - 切片的绝对路径按原样记录，只用于显示。查找切片按 `slide_id`，不按路径。

**哪些生产者写条目（特别要求）**：

| kind | 生产者（插入点） | 令牌 | `depends_on` | `operates_on` |
|---|---|---|---|---|
| `raw_slide` | `ensure_project_manifest`（`utils/roi_project.py:93`），首次见到该切片时 | `slide_id` | — | — |
| `corrected_channel`（每个通道一条） | `core/step0_handoff.write_handoff`，在发布（`:318-323`）之后 | 数组 attrs 的 `source_identity`（uuid） | `raw_slide` | 该组所属 ROI 的 `region_id`（v2，见表下） |
| `fused` | `FullFusionWorker`，在 `_publish_store`（`ui/step0/overview_panel.py:711`）之后 | zarr attrs 的 `created_at` + `config_hash` | `raw_slide` + **实际读到的**校正通道（按 zarr 路径 + 数组名 + `source_identity` 查找） | 该区域的 `region_id` |
| `segmentation_run` | `segment_merge_worker`，写完 `segmentation_meta.json`（`:3488`、`:4212`）之后，放在已有的 `if self.roi_dir and self.roi_id` 分支里（`update_roi_segmentation_run` 所在的 `:3492`、`:4217`） | `run_id` | 输入的 fused（按路径 + 该 zarr 当时的 `created_at` + `config_hash` 查找）；找不到就写进 `unresolved_inputs` | `rois[i]` 各自的 `region_id` |
| `step4_h5ad` | `run_extraction`，在 `os.replace` 循环（`workers/feature_extract_worker.py:262-275`）之后 | 输出路径 + `_provenance.created_at` | 该分割运行 + `raw_slide` + 实际用到的校正通道（来自 `job.channels` 的 `identity`） | 该区域的 `region_id` |
| `cells_parquet` | A4 的 `core/object_tables`（§3.7） | 文件 sha256 | **只有该 `segmentation_run`**（v2） | 该运行的各区域 id |
| `regions_parquet` | 同上 | 文件 sha256 | **所涉切片的 `raw_slide`**（v2）；读了哪些工作区（`workspace_id` 列表）和哪些 `roi_config` / `roi_manifest`（路径 + sha256）写进 `parameters`，因为工作区不是产物、没有 artifact id | 表里所有区域 id |
| `corrected_coarse_levels`（**P3**） | **schema 在 A3 定义并有测试**；登记由 §5.8 自己的申请实现 | 层的令牌 | 对应的 `corrected_channel` | 与对应的 `corrected_channel` 相同 |

**`corrected_channel` 的 `operates_on`（v2）**：
- **规则**：校正组不是区域，所以不新增 `type`。`operates_on` 是这个组**所属 ROI** 的 `region_id`：
  1. 用组的 `roi_name` 在同一工作区 Step0 提交的 `roi_config.json` 里找到那个 ROI；
  2. 按 §3.1 用它的 `type`、`bbox_fullres` 和 `polygon_fullres` 计算。
- **为什么组的几何与 ROI 相同**：组的 `bbox_fullres` / `polygon_fullres` 就是从同一个 ROI 字典写进去的（`core/step0_handoff.py:96-97`、`:194-197`）。所以同一块组织不会出现两个 `reg_…`。
- **组自己的覆盖范围**：写进条目的 `parameters.valid_bounds`（就是 `CorrectedZarrSource.valid_bounds(0)`）。
- **与 A2c 越界问题的关系**：越界说的是**读请求**超出了组的 bbox（例如 fusion 区域超出校正组），不是组的 bbox 与它自己的 ROI 不同。
- **对不上的情况**：如果组 attrs 里的 bbox 与 `roi_config` 的不一致，或者找不到那个 ROI，就不猜：
  - `operates_on` 留空；
  - 加 `flags: ["region_mismatch"]`，同时记日志；
  - graph 报告这个标记。
  - 有测试覆盖。

**暂不写的，及理由**：

| 不写 | 理由 |
|---|---|
| A3 之前已经存在的所有产物（旧项目、test1 里现有的运行） | §2.4 / §6.8：「旧项目不迁移」。补登记需要从路径推断上游，而共享目标（fused）可能已被覆盖，推断出的依赖可能是错的。它们只作为 `unresolved_inputs` 出现 |
| Step1 预分割运行（`presegmentation_runs/`） | 只是参数试跑，不是下游科学输入。它和 Step2 的关系已经记在 `seg_config.preseg_contract`（契约哈希、`preseg_run_id`），会作为 `segmentation_run` 的**参数**原样进入条目，不作为依赖边 |
| 现有的 `corrected_coarse.zarr` 旁车 | 只由脚本 `scripts/build_corrected_coarse_l3.py` 写，是 viewer 的加速层，不是科学输入。§5.8 落地时由它自己的生产者登记（P3） |
| label pyramid、`global_mask_*.zarr`、`global_dapi*.ome.tiff`、Step2 的日志 / 剖析文件 | 属于 `segmentation_run` 这个产物的组成部分，列在它的 `parameters.files` 里，不单独成为节点 |
| Step4 的 CSV、`_provenance.json` 旁车 | 属于 `step4_h5ad` 产物，列在 `files` 里 |
| `fusion_meta.json`、`dapi_input_meta.json`、各种 config / `panel.csv` | 是参数或描述，不是产物。它们的路径和 sha256 进入相关条目的 `parameters` |
| Step3 | 没有落盘生产者（§2.4） |
| `step1_5_bg_page` 的 `correction_config.json` | 旧页面，不在 Step0–4 的公开路径上；以后有块碰它时再登记（§6.8 最后一段） |
| `segment_merge_worker` 中没有工作区（`roi_dir` 为空）的旧路径 | 没有项目根，无处登记 |

**登记失败的处理（裁定 6）**：登记发生在产物提交**之后**，是辅助记录，与 `mark_roi_step` 的处理方式一样（`core/step0_handoff.py:336-343`）。登记失败（磁盘满、项目 schema 未知等）只记日志，不删、不改产物，也不让 GUI 报错退出；graph 会把该产物显示为「未登记」。

**artifact graph v0**（`core/artifact_graph.py`，只读）：
- 读 `provenance/*.json`，边**只有** `depends_on`。
- 能回答：
  - 「X 依赖什么」（递归）；
  - 「什么依赖 X」；
  - `step4_lineage(h5ad)`：返回该结果的分割运行、fused 和校正通道。
- 另外报告三类情况：
  - `unresolved_inputs`；
  - 引用了不存在 id 的悬空边；
  - 同一位置被后来的产物覆盖（**按位置 + 创建时间报告**，例如「`fused_Full WSI.zarr` 现在装的是 `art_fused_b2…`，不是你依赖的 `art_fused_9c…`」）。
- **不做**：标记过期、重建、删除、修改任何文件（§0.3、§6.8）。空间范围从 `operates_on` 单独读取，不是图的边。
- 命令行：`scripts/artifact_graph.py PROJECT {lineage H5AD | deps ID | dependents ID | list}`。

### 3.6 P4 — 与 OME-NGFF / SpatialData 的语义对应（只写对应表）

**结论**：FusionFlux 的每一个坐标变换都是**轴对齐的缩放加平移**（没有旋转、剪切、非线性），所以都能无损写成 OME-NGFF 0.4 的 `scale` + `translation`，以及 SpatialData 的 `Scale` / `Translation` / `Sequence`。**反过来不成立**：任意的 NGFF / SpatialData 变换（仿射、旋转、z / t 轴、非公制单位、多重序列）**不能**映射回 FusionFlux。A3 不声称可以，也不引入 ome-zarr / spatialdata 依赖，不写转换代码（导出见 §11 backlog 的 `export_to_spatialdata()`）。

| FusionFlux | OME-NGFF 0.4 | SpatialData | 说明 |
|---|---|---|---|
| 数组轴 (c, y, x) | `axes: [{name:"c",type:"channel"}, {name:"y",type:"space",unit:"micrometer"}, {name:"x",type:"space",unit:"micrometer"}]` | 图像的 dims `("c","y","x")` | 没有物理尺寸时 `unit` 省略，单位就是像素 |
| 第 L 层 → `physical_um`：`(i + 0.5)·s_L − 0.5` 再乘 `d` | 该层 `coordinateTransformations: [{type:"scale", scale:[1, s_y·d_y, s_x·d_x]}, {type:"translation", translation:[0, (s_y−1)/2·d_y, (s_x−1)/2·d_x]}]` | `Sequence([Scale([s_y·d_y, s_x·d_x]), Translation(...)])` | `s_L` 用每轴真实比例（float64），不是 2、4；平移项来自「整数下标 = 像素中心」 |
| 第 0 层 → `physical_um` | `scale: [1, d_y, d_x]`，没有平移 | `Scale([d_y, d_x])` | — |
| `physical_um` 不可用 | 像素单位的 scale（没有 `unit`） | 坐标系 `global` 的单位是像素 | 不填 0.5 µm |
| `region_local = global − (y0, x0)` | 区域级 / 标签的 `translation: [y0·d_y, x0·d_x]` | `Translation([y0, x0])` 接到 global | 平移量是整数像素 |
| `viewer_world = global + 0.5` | 不需要（它只属于显示） | 内在坐标系的像素角点约定：像素 i 覆盖 [i, i+1)，所以 `viewer_world` 正好等于 SpatialData 的内在像素坐标；`global_pixel` 到它是 `Translation(+0.5)` | 约定在实现 `export_to_spatialdata()` 时，再对照 SpatialData 文档逐条核实；这里只是语义对应 |
| bbox 半开 `[x0, x1) × [y0, y1)` | — | 形状 / 多边形的外接框 | 导出时再换算 |
| `cells.parquet` / Step4 h5ad | — | `tables`（AnnData），`region` / `instance_key` = `region_id` / `cell_id` | 稳定键见 §3.1 |
| LabelStore 栅格 | NGFF `labels` | `labels` | LabelStore 仍是语义权威（§5.3） |

「无损」的含义：每个变换的参数都是 float64，NGFF / SpatialData 存的也是同样的 float64；层的整数形状另有数组形状可查。所以往返得到的是同一组数，不发生舍入。

### 3.7 A4 — 对象层 v1

**布局（裁定 4）**：A3 把 §2.4 的最小布局 v1 冻结为**逻辑布局 + 对照表**。

- 物理上只**新增**这些项目根下的文件：`transforms.json`、`provenance/`、`objects/`。
- 现有的 `rois/<workspace_id>/stepN/…` 一律不动。§2.4 的 `segmentations/<run>/…` 是逻辑名，按 A0 草案 §3.5 的对照表映射到今天的路径。
- 不搬目录：搬目录要同时改 Step2 的写入以及 Step3 / Step4 的读取，超出 3–4 天，也违反「旧项目不迁移」。

| 文件 | 位置 | 内容 |
|---|---|---|
| `cells.parquet` | `objects/<segmentation_run_id>/cells.parquet` | 一个分割运行的全部区域 |
| `regions.parquet` | `objects/regions.parquet`（**项目级**，裁定 8） | 项目里所有工作区的区域。`region_id` 由几何确定，同一区域只出现一次 |

**`cells.parquet` v1**（列名、类型照 §6.4 和 A0 草案 §3.2，不多不少）：

| 列 | 类型 | 定义 |
|---|---|---|
| `segmentation_run_id` | string | `run_id` |
| `region_id` | string | §3.1，由该运行 `rois[i]` 的 bbox 计算 |
| `cell_id` | uint32 | LabelStore 细胞标签 |
| `slide_id` | string | §3.1 |
| `x_global`, `y_global` | float64 | 质心，**与 Step4 的公式相同**：`float64(Σx_local / n) + ox`（整数之和、整数相除，正确舍入）。所以和 h5ad 的 `centroid_x/y` 逐位相同 |
| `bbox_x0`, `bbox_y0`, `bbox_x1`, `bbox_y1` | int32 | 半开，global_pixel |
| `cell_area` | int64 | 像素数 |
| `nucleus_count` | int32 | `nucleus_to_cell` 里指向该细胞的核数；LabelStore 没有核层时为 null（不猜） |
| `nucleus_area` | int64 | 这些核的像素总数；没有核层时为 null |

- 行 = LabelStore 里像素数 > 0 的细胞标签；空标签不出现（与 Step4 相同，test1 有两个空标签：29630、53238）。
- **没有 `qc_valid`**；不复制 Step4 的表达量；不存多边形。
- Parquet 的 schema 元数据里写入 `schema = "block01.cells"`、`schema_version = 1`、`artifact_id`。
- 计算方法：按块扫描 LabelStore 的细胞、核栅格和 `nucleus_to_cell`，用 `np.bincount` 累加。只读，不读 h5ad 的表达量。

**`regions.parquet` v1**：`region_id, slide_id, type, name, bbox_x0, bbox_y0, bbox_x1, bbox_y1, parent_region_id`。
- 来源：每个工作区 Step0 提交的 `roi_config.json`，没有它就用 `roi_manifest.json`。
- `name` 取显示名，同一区域有多个显示名时取最早的那个。
- `parent_region_id` 今天总是 null。
- **没有 `qc_status`**。

**权威边界**（裁定表第 10 行，写明 Parquet 与 h5ad 谁是权威）：

| 内容 | 权威 | 其他副本 |
|---|---|---|
| 哪些像素属于哪个细胞、核↔细胞关系 | **LabelStore**（§5.3） | — |
| 细胞的身份键、质心、bbox、面积、核数 / 核面积 | **`cells.parquet`**（由 LabelStore 确定性地生成；两者不一致时，判 Parquet 无效并重建） | Step4 h5ad 的 `obs` 里的 `centroid_*` / `bbox_*` / `area` 是 Step4 自己算的副本：不改它，不回写；A4 的验收要求两者逐位相同 |
| 表达矩阵、区室统计、Step4 的其他形态特征 | **Step4 h5ad**（S4-3 输出） | Parquet 不复制 |
| 区域几何 | Step0 提交的 `roi_config` / `roi_manifest`（Pre-TMA） | `regions.parquet` 是它的表格形式，可以重建。TMA core 不经过 Step0 工作区，它的权威由 TMA Foundation 决定；A4 只冻结 schema |
| 聚类、注释、嵌入、分析状态 | `analysis/<segmentation_run_id>/analysis.h5ad` | 本块**没有任何代码写分析状态**，所以这一条只冻结路径，不产生文件 |

**生成方式（裁定 7）**：A4 只提供库函数 `core/object_tables.build_object_tables(project, segmentation_run_id)` 和命令行 `scripts/build_object_tables.py`，**不接入 GUI**。这样 Step2 / Step4 的产品路径和耗时都不变。TMA 代码会直接调用这个库。

**依赖（裁定 9）**：在 `fusion_mesmer` 环境里安装 `pyarrow==25.0.1`（dry-run 确认只装这一个包），写入 `envs/fusion_mesmer/requirements-pip.txt`，并按现有方式更新 `conda-linux-64.lock` / `README` 的说明。
- `pyarrow` 只在 `core/object_tables` 里导入，其他模块不受影响。
- 如果真机使用的是另一个环境，要在那里同样安装。

---

## 4. 做法与提交切分（每段都可单独回退）

| 段 | 内容 | 回退的影响 |
|---|---|---|
| **A3-1** 契约与只读机制 | 新模块 `core/project_identity.py`：`slide_id`、`region_id`、项目 schema 的读 / 检查、P2 描述、`transforms.json` 的生成与读取、坐标变换函数。新模块 `core/provenance.py`：条目 schema、校验、原子登记、按「位置 + 令牌」查找。新模块 `core/artifact_graph.py`（只读）与 `scripts/artifact_graph.py`。测试。契约文档冻结（`docs/v16_contracts_draft.md` §2、§3 改为冻结版，加 P4 对应表）。**不碰任何生产者** | 新模块没有调用方，删除即可 |
| **A3-2** 项目级写入 + Step0 | `utils/roi_project.ensure_project_manifest`：`project_schema_version`、`sources`（P2）、`transforms.json`、`raw_slide` 条目。`core/step0_handoff.write_handoff`：发布之后登记 `corrected_channel`。`core/quant_sources.resolve_quant_job`：**只加**未知项目版本的拒绝 | 回退后已写入的字段留在 manifest 里，旧代码会忽略未知键（§2.2），项目照常可读 |
| **A3-3** fusion / Step2 / Step4 登记 | `FullFusionWorker` 发布之后登记 `fused`；`segment_merge_worker` 两处登记 `segmentation_run`；`run_extraction` 之后登记 `step4_h5ad`。每处是「调用一次 `provenance.register(...)`，并用 try / 记日志包住」 | 回退后不再产生新条目；已有条目只是不再增加 |
| **A4-0** 依赖 | 安装 `pyarrow==25.0.1`，更新环境清单 | `pip uninstall pyarrow`，清单还原 |
| **A4-1** 对象层 | 新模块 `core/object_tables.py`、`scripts/build_object_tables.py`；登记 `cells_parquet` / `regions_parquet`；测试 | 删除新模块；已生成的 `objects/` 只是旁路文件 |

顺序：A3-1 → A3-2 → A3-3 → A4-0 → A4-1。每段提交前，该段的验收门全部通过（§7），单调绿色规则（§0.4 规则 4）在 A3-3 末和 A4-1 末各检查一次。

A3-1 是纯新增，A3-2 / A3-3 对现有文件只**追加**「提交之后登记」的几行，不改变已有的任何行为和输出。

---

## 5. 封闭白名单

**新增文件：**
- `core/project_identity.py`、`core/provenance.py`、`core/artifact_graph.py`、`core/object_tables.py`
- `scripts/artifact_graph.py`、`scripts/build_object_tables.py`
- `scripts/diagnose_v16_a3a4_acceptance.py`：验收用，在副本上建临时项目、运行 oracle、做反向注入
- `tests/test_v16_project_identity.py`、`tests/test_v16_provenance.py`、`tests/test_v16_artifact_graph.py`、`tests/test_v16_object_tables.py`

**修改，并限定范围：**
- `utils/roi_project.py`：**只改 `ensure_project_manifest`**（schema 版本、`sources`、`transforms.json`、`raw_slide` 登记）。`create_*_context`、`roi_id` 的生成、`roi_index` 都不改。
- `core/step0_handoff.py`：**只在 `write_handoff` 发布之后**追加 `corrected_channel` 登记（与 `mark_roi_step` 同一位置、同样的 try）。
- `ui/step0/overview_panel.py`：**只改 `FullFusionWorker`**，在 `_publish_store` 之后追加 `fused` 登记。
- `workers/segment_merge_worker.py`：**只在 `:3492` 与 `:4217` 两处** `update_roi_segmentation_run` 所在的分支里追加 `segmentation_run` 登记。
- `workers/feature_extract_worker.py`：**只在 `run_extraction`** 的 `os.replace` 循环之后追加 `step4_h5ad` 登记。
- `core/quant_sources.py`：**只在 `resolve_quant_job`** 读取 handoff 之前追加一条项目 schema 检查（未知版本 → `QuantSourceError`）。
- `tests/test_v16_pixel_source.py`：只更新允许名单 `MIGRATED`，加入 `core/project_identity.py`（P2 通过 `OmeTiffSource` 读层数和形状）和 `scripts/diagnose_v16_a3a4_acceptance.py`。
- 环境：`envs/fusion_mesmer/requirements-pip.txt`（加 `pyarrow==25.0.1`）、`envs/fusion_mesmer/conda-linux-64.lock` / `README.md`（只在需要时）。
- 文档：本申请的执行记录；`docs/v16_contracts_draft.md`（§2、§3 冻结，加 P4 表）；合并基准 §12 / v2.4 §12 的进度行。

**`ui/main_window.py`、`ui/step0/step0_page.py` 一行都不改。**

**UI**：不改。没有新控件、新对话框，也没有改动的文案。唯一能被用户看到的变化是：项目 schema 未知时，Step4 页通过已有的错误显示给出一条新的错误文字（裁定 10）。所以 `UI_SURFACE_RULES.md`、`docs/user_guide.md`、`docs/用户指南.md` **不需要更新**。如果裁定 10 选 (b)，就要把这三份文档加进白名单。

## 6. 不改的范围

- 科学算法：QuantEngine / S4-3、fusion 公式、N2 / N3、分割引擎、LabelStore 语义。
- Step0–4 的输出文件内容：h5ad、CSV、`_provenance.json` 旁车、fused zarr 的数组和 attrs、`segmentation_meta*.json`、`step0_roi_result.json`、`roi_index.json`、`roi_manifest.json`。
- 读取器与 viewer 读取路径（`AGENTS.md` 规则 5），`PixelSource` 契约本身（P2 只是读取已有方法）。
- 工作区的建立方式（`roi_id` 的生成、Step0 Save 每次新建工作区的行为）。A3 只给它们定义含义，不改行为。
- 现有目录布局；旧项目不迁移、不补登记。
- 「过期」传播、自动重建、产物回收（§0.3）。
- 已知偏差：`image_mpp = 0.5` 写死、`step0_page.py:11364` 的闭区间。只列出，不改。

---

## 7. 预注册验收门

**判定规则**：每一条都由测试或脚本自动给出 PASS / FAIL。每一条都有对应的**反向注入**：在临时副本上故意改错，对应的测试必须变红。反向注入的结果写进执行记录；任何一条注入没有让测试变红，都算该门不通过，必须先修好测试再继续。

**Oracle**：A3 之前的 HEAD（`37b11db` 的 `git archive`），在同一个 test1 副本上运行（§0.4 规则 2）。

### 7.1 先证明比较工具能发现差异（重复 A2c 的做法）

- [ ] 复制一份 h5ad，把 `X` 里一个值改一个 float32 最小步长 → `same-step4` 报「不同」；只改 `uns/provenance_json` → 报「相同」；在 `uns` 下**加一个数据集** → 报「不同」（证明「新增字段不得进 h5ad」这条有工具守着）。
- [ ] 复制一份 fused zarr，改一个像素 → `same-fused` 报「不同」；空目录 → 报失败。

### 7.2 A3 门

| # | 门（自动） | 反向注入（必须变红） |
|---|---|---|
| A3-G1 | **项目 schema**：新项目写入 `project_schema_version: 1`；没有该键的旧项目读为 LEGACY，行为与今天相同；值为 `2`、`"1"`、`true`、`-1` 和损坏的 JSON 都抛 `ProjectSchemaError`，信息里有路径和读到的值；`ensure_project_manifest` 遇到未知版本时不覆盖它；`resolve_quant_job` 遇到未知版本时抛 `QuantSourceError`；登记被拒绝且没有文件写入；**原子写**（v2）：在 `os.replace` 之前注入异常，旧的 `project_manifest.json` / `transforms.json` 逐字节不变，目录里不留临时文件 | ① 读取者把 `≥ 1` 都当作已知 → 红；② 写入者遇到未知版本时覆盖成 1 → 红；③ 把检查从 `resolve_quant_job` 里拿掉 → 红；④ manifest 改回直接 `open("w")` 写 → 原子写测试红 |
| A3-G2 | **`slide_id`**：同一个文件多次计算结果相同；复制到另一路径、`touch` 之后结果不变；另一张切片结果不同；指纹不变时不重算（计数器）；签名在 test1 上 < 100 ms | ① 把路径加进签名 → 「复制后不变」变红；② 不检查指纹、每次都重算 → 计数器测试变红 |
| A3-G3 | **`region_id`**：test1 副本两个 Full WSI 工作区 → 同一个 `region_id`；改显示名后不变；合成的双 ROI 工作区 → 两个不同的 id，第二个 ROI 没有 `roi_id` 也能算出；bbox 差 1 px → 不同；对旧项目计算时没有写入任何文件（目录树指纹前后一致）；**多边形**（v2）：同一个多边形经过顶点循环移位、反转方向、加闭合点、加连续重复点之后，id 不变；`null` 与 `[]` 得到同一个 id；顶点移动 > 0.0005 px → id 不同；`type` 不在枚举里 → 报错；**校正组**：`corrected_channel` 的 `operates_on` 等于所属 ROI 的 `region_id`，组 bbox 与 `roi_config` 不一致时 `operates_on` 为空，并带 `region_mismatch` 标记 | ① 把 `roi_name` 加进哈希 → 红；② 用 `roi_id` 当 `region_id` → 「两个工作区同一个 id」变红；③ 去掉规范化、直接哈希原始顶点 → 循环移位测试红；④ 用组自己的 bbox 加一个新 `type` 计算 → 「同一块组织一个 id」测试红 |
| A3-G4 | **P2**：test1 的描述为 `kind = "ome_tiff"`、`level_count = 3`、`coarsest_level_shape_yx = [964, 1013]`、三层形状、`physical_size_yx_um` = OME 的值；没有 `PhysicalSize` 的合成切片 → `null`；描述同时出现在 `project_manifest.sources` 和 `raw_slide` 条目里，两者相同；不认识的源类型 → 报错 | ① 物理尺寸缺失时填 0.5 → 红；② 最粗层取 `level_shape(0)` → 红 |
| A3-G5 | **坐标**：合成的非整数比例金字塔（例如 1001 × 999 → 251 × 250），global ↔ level ↔ physical ↔ region_local ↔ viewer_world 往返一致；test1 上用到的 L1 比例为 4.000259 / 4.000740；`viewer_world = global + 0.5`，`floor(world) = global`；一个合成的 TMA core 区域只用同一组函数就能表示（门 4「core 是普通区域」） | ① 用 `2**level` → 红；② 用 `legacy_level_downsample_rounded` → 红；③ 去掉 +0.5 → 红 |
| A3-G6 | **出处条目**：每个登记的生产者都写出带全部必填字段的条目（逐个生产者测试，在合成项目上走真实的生产者函数）；`depends_on` 里只有已登记的 artifact id，校验器拒绝 `reg_…`、路径、空串、未知 id；`operates_on` 里只有 `reg_…`；同一位置 + 令牌重复登记返回同一个 id；登记中途抛异常不留下半个文件；登记失败不影响产物（产物文件与不登记时逐位相同）；`location.path` 只含 `/`（v2：在 Windows 风格的路径输入上测试）；`segmentation_run_id` 冲突时照常登记并带 `segmentation_run_id_collision` 标记，`build_object_tables` 遇到它就拒绝生成 | ① 往 `depends_on` 里放一个 `region_id` → 校验测试红；② 去掉某一个生产者的登记调用 → 该生产者的测试红；③ 登记的异常不被捕获 → 「登记失败不影响产物」红；④ 用 `os.path.relpath` 而不做 `as_posix()` → 分隔符测试红；⑤ 冲突时直接拒绝登记 → 冲突测试红 |
| A3-G7 | **graph v0**：合成项目上跑完整链（Step0 交接 → `FullFusionWorker` → 分割 worker → `run_extraction`，都是真实的生产者函数；分割用测试里已有的小引擎或桩），`step4_lineage` 只通过 `depends_on` 就返回正确的分割运行、fused 和校正通道；`dependents(corrected_channel)` 能找到 fused 和 Step4；A3 之前做的分割运行出现在 `unresolved_inputs` 里，而不是被猜成某个 id；重新 fusion 之后，graph 报告「位置已被覆盖」；graph 运行前后项目目录树指纹不变 | ① graph 沿 `operates_on` 走边 → 红；② graph 在没有边时按路径去猜 → 红；③ graph 写文件 → 指纹测试红 |
| A3-G8 | **Step4 / fusion 逐位不变**：test1 副本上，新代码与 oracle 的 Step4 → `same-step4` 相同（h5ad 全部数据集 + CSV）；fusion → `same-fused` 相同且不是全零；Step4 输出目录里的文件集合不变；新增的内容只出现在 `provenance/`、`transforms.json`、`project_manifest.json` 的新键里 | 在 `run_extraction` 里把 `artifact_id` 写进 h5ad 的 `uns` → `same-step4` 红（「数据集集合不同」） |
| A3-G9 | **性能**：Step4 端到端，新旧交替各 4 次取中位数，≤ 1.05×；登记本身每个产物 < 200 ms | 在登记里加 2 s 的延时 → 性能门红 |

### 7.3 A4 门

| # | 门（自动） | 反向注入（必须变红） |
|---|---|---|
| A4-G1 | **键与 LabelStore 一致**：test1 真实结果（分割 `seg_20260927_124316_…`）的 `cells.parquet`：行数 = LabelStore 里像素数 > 0 的标签数（56 872）；`(segmentation_run_id, region_id, cell_id)` 唯一；每个键都在 LabelStore 里，LabelStore 的每个非空标签都在表里；缺的恰好是 Step4 记录的空标签（29630、53238） | ① 丢掉一行 → 红；② 把空标签也写进表 → 红 |
| A4-G2 | **坐标与 Step4、标签一致**：按 `cell_id` 对齐，`x_global/y_global` 与 h5ad 的 `centroid_x/y` **逐位相同**；`bbox_*`、`cell_area` 与 h5ad 的 `bbox_min/max_*`、`area` 完全相同；随机 200 个细胞，从栅格重算的 bbox 与表相同 | ① bbox 用闭区间（max 不 +1）→ 红；② 质心加 0.5（viewer 坐标）→ 红；③ x / y 对调 → 红 |
| A4-G3 | **与 viewer 一致**：同样 200 个细胞，用 Step3 现有的标签取样函数 `core/step3_masks.read_label_tile`（`:702`）在第 0 层读出包含该细胞的分块；在 `world = global + 0.5` 处取到的标签值就是该 `cell_id`。**这条门检验的是 §3.2 的适配规则本身**（旧 viewer 让像素 j 覆盖 [j, j+1)）：如果它红了，说明契约文本写错了，正确的做法是停下来报告，并修改 §3.2 的 `viewer_world` 规则（要先得到用户确认），**不改 viewer 代码** | 换算时漏掉 +0.5 / 用 `round` 代替 `floor` → 红 |
| A4-G4 | **核**：`nucleus_count` / `nucleus_area` 与另一种独立算法相同（逐核遍历 `nucleus_to_cell` 和核栅格）；没有核层的合成运行 → 两列为 null | ① 把 `nucleus_to_cell[0]` 算进去 → 红；② 没有核层时填 1 → 红 |
| A4-G5 | **schema**：`cells.parquet` 的列名、顺序、类型与 §3.7 完全一致；没有 `qc_valid`；schema 元数据有 `schema_version = 1`；`regions.parquet` 同理，没有 `qc_status` | 加一列 `qc_valid` → 红；`cell_id` 改成 int64 → 红 |
| A4-G6 | **`regions.parquet`**：test1 副本的每个工作区的每个区域都出现；两个 Full WSI 工作区合成一行；bbox 映射正确（`bbox_x0 = bbox_fullres[2]` …）；合成的双 ROI 工作区 → 两行 | 映射时 x / y 对调 → 红 |
| A4-G7 | **权威与只读**：生成 Parquet 前后，h5ad、CSV、LabelStore、`segmentation_meta.json` 的 sha256 不变；没有 `analysis/` 目录产生；`cells_parquet` 条目的 `depends_on` 是该分割运行，`operates_on` 是区域 id | 生成器顺手往 h5ad 的 `obs` 写 `region_id` → sha256 测试红 |
| A4-G8 | **依赖**：装 pyarrow 之前与之后 `pip freeze` 的差异只有 `pyarrow==25.0.1`；A0.5 的引擎身份测试（`tests/test_engine_identity.py`、`tests/test_preseg_contract.py`）全绿；只有 `core/object_tables.py` 导入 pyarrow（测试检查） | 在别的模块里导入 pyarrow → 导入范围测试红 |

### 7.4 单调绿色规则（§0.4 规则 4）

- [ ] A1 零漂移（`test_v16_zero_drift`）、A1b 布局锁（`test_v16_frame_lock`）、A2a / A2c（`test_v16_pixel_source`、`test_v16_a2c_migration`）全部通过。
- [ ] v15 科学回归：每个模块单独起进程，离屏模块加 GL 模块；失败的模块在 `git archive HEAD` 的副本上重跑，去掉 ANSI 颜色码后逐条对比失败的测试名；没有新增失败。已知的不稳定测试（`test_step0_compare_tiles::test_the_dapi_mapping_is_its_own_channels`、GL 下 `test_step3_label_render` 的 139）按 A2c 的方式重跑判定。
- [ ] 开始全量回归前，C: 盘剩余 ≥ 20 GB（今天 51 GB）；长任务用 `setsid nohup` 启动，并写完成标记文件。

### 7.5 真机（另计时间）

在一份**新复制并改写过路径**的 test1 副本上（原副本保留给 oracle），用户用新代码在界面里依次做：Step0 Save → fusion（决定至少一个校正通道参与融合，补上 A2c 真机没有覆盖的校正路径）→ Step2 → Step4。之后：
- [ ] `scripts/artifact_graph.py PROJECT lineage <h5ad>` 列出该次的分割运行、fused 和参与的校正通道（门 7）；
- [ ] 用 A3 之前的代码（`37b11db` 存档）在同一组设置上重跑 Step4，`same-step4` 相同；
- [ ] `scripts/build_object_tables.py` 在这次的结果上生成两张表，A4-G1/G2 的检查在它上面也通过（门 5）；
- [ ] 用 A3 之前的代码打开这个副本：Step0–4 都能照常打开（旧代码忽略新键）；
- [ ] 界面没有变化。

---

## 8. 风险

| 风险 | 对策 |
|---|---|
| **`slide_sig_v1` 是身份，不是校验**：文件中间的像素被改了，只要大小、OME-XML 和头尾 4 MiB 不变，`slide_id` 就不变，**这种改动不会被发现** | 接受这一点：它回答的是「这是哪张切片」，而不是「文件有没有被改过」。算法名随 id 一起记录（`slide_id_method`）。以后升级到完整 sha256 时，新算法生成新的 id，**旧 id 照样有效**，因为每条记录都写着自己用的算法。裁定 1 也可以直接选完整哈希 |
| 登记失败让 E3 出现缺口 | 登记失败只在磁盘满等异常情况下发生；graph 把产物报告为「未登记」，不会悄悄把缺口补成错误的边 |
| 共享目标被覆盖（fused）导致依赖指向已经不存在的版本 | graph 如实报告「位置已被覆盖」；根本修复属于 E5 / A6 的运行级目标 |
| 在 GUI 的 Save 路径上抛新异常导致进程退出（§2.7） | Save 路径上的 schema 处理只记日志、不抛异常；登记都包在 try 里；A3-G6 ③ 守着这一点 |
| pyarrow 改动环境 | dry-run 确认只装一个包；A4-G8 对比 `pip freeze`；只有一个模块导入它；可以卸载回退 |
| 34 个工作区的项目里，`regions.parquet` 去重时显示名冲突 | `name` 只用于显示；去重按 `region_id`，取最早的显示名，规则写在 schema 里 |
| Step4 变慢 | 登记在输出之后，耗时 ms 级；有性能门 |
| 一个项目目录里出现两张切片 | `sources` / `transforms.json` 按 `slide_id` 分组（§2.2 的事实） |
| 回退 | 五段提交各自可以回退；新键和新文件对旧代码不可见 |

## 9. 估计

- A3-1：1 天；A3-2 + A3-3：1 天；A4-0 + A4-1：1–1.5 天；回归与反向注入：0.5 天。
- 合计约 **3.5–4 个工作日**，与 §12 的「A3 + A4 约 3–4 天」一致。
- 另需约 0.5 天真机时间（§7.5，主要是 Step2 的 3–4 分钟加整条链的操作）。

---

## 10. 请用户裁定（每题给出推荐项）

1. **`slide_id` 的算法**：
   - (a) `slide_sig_v1`：大小 + OME-XML + 各层形状 / dtype + 头尾各 4 MiB，毫秒级，按指纹缓存；
   - (b) 完整文件 sha256：本切片约 2 s，30 GB 的 WSI 约 1 分钟，按指纹缓存，只算一次；
   - (c) OME-XML 里的 UUID：有的文件没有；转换一次就变；
   - (d) 在项目里登记一个随机 UUID：同一张切片在两个项目里会得到两个 id。
   - **建议 (a)**，并把算法名一起记录，TMA 批次需要时再升级到 (b)。
2. **`region_id` 的定义**：
   - (a) 由几何确定（`slide_id` + 类型 + bbox + 多边形的哈希），`roi_id` 改称 `workspace_id`，含义不变；
   - (b) 照 A0 草案用 `roi_id`：多 ROI 工作区里第二个以后的 ROI 没有 id，34 个 Full WSI 会成为 34 个区域。
   - **建议 (a)**。
3. **出处记录的存放**：
   - (a) `provenance/<artifact_id>.json`，每个产物一个文件，只新增、原子写、不需要锁；
   - (b) 照计划字面写成单个 `provenance.json`，需要加跨进程锁。
   - **建议 (a)**。计划里的「`provenance.json`」解释为这组记录。
4. **最小布局 v1 怎么冻结**：
   - (a) 冻结逻辑布局 + 对照表；物理上只新增 `transforms.json`、`provenance/`、`objects/`；
   - (b) 新项目改用新的物理布局：要搬 Step2 的输出，并改 Step3 / Step4 的读取，超出本块。
   - **建议 (a)**。
5. **旧项目**：
   - (a) 新代码第一次写入时加上 `project_schema_version: 1`（「采用」）；已有的产物不改写、不补登记，作为未登记的输入出现；
   - (b) 旧项目永远不加版本号，只有全新的项目才有出处记录。这样 test1 副本就不能用于真机门，需要建一个新项目。
   - **建议 (a)**。
6. **登记失败时**：
   - (a) 只记日志，产物保留，graph 显示为「未登记」；
   - (b) 让产物失败。
   - **建议 (a)**（与 `mark_roi_step` 的处理相同）。
7. **`cells.parquet` 何时生成**：
   - (a) 只提供库函数和命令行，不接入 GUI；
   - (b) Step2 或 Step4 完成后自动生成（改产品路径，增加耗时）。
   - **建议 (a)**。
8. **`regions.parquet` 的位置**：
   - (a) 项目级 `objects/regions.parquet`；
   - (b) 每个分割运行一份 `objects/<run>/regions.parquet`。
   - **建议 (a)**：`region_id` 是项目级的。
9. **安装 `pyarrow==25.0.1`**（§6.6；A0.5 已经完成），并记入环境清单。**建议同意。**
10. **未知项目版本在 GUI 中如何呈现**：
    - (a) 只走已有的错误路径（Step4 页显示 `QuantSourceError`；Step0 Save 只记日志、不改版本号、不登记），不改 UI；
    - (b) 打开项目时弹出新对话框：要改 `ui/main_window.py` 或 `step0_page.py`，并更新 `UI_SURFACE_RULES.md`、`docs/user_guide.md`、`docs/用户指南.md`。
    - **建议 (a)**：项目的打开流程归 A8（项目状态的唯一持有者）。
11. **生产者覆盖范围**：照 §3.5 的两张表：写 7 种（`raw_slide`、`corrected_channel`、`fused`、`segmentation_run`、`step4_h5ad`、`cells_parquet`、`regions_parquet`），P3 的 `corrected_coarse_levels` 只定 schema，暂不写的有 9 类。**建议同意。**
12. **把 A0 草案 §2、§3 改成冻结版**（含 §3.2 的一处改动：区域原点不复制进 `transforms.json`），并加上 P4 对应表。**建议同意。**
13. **v2 的修订**（§11）：采纳独立审核的 4 处必修和 3 处补充定义，其中第 2 处必修按修改后的方式采纳；不采纳持久化 `native_tile_shape_per_level`。**建议同意。**

---

## 11. 独立审核意见的逐条评估（2026-10-01）

审核意见本身不构成授权（`AGENTS.md` 规则 2）。下面是逐条的评估，以及 v2 的处理。审核同意 §10 第 1–12 题的建议项，这里不重复。

| # | 审核意见 | 评估 | v2 的处理 |
|---|---|---|---|
| 必修 1 | `regions.parquet` 的 `depends_on` 写成了「工作区」，而工作区不是产物 | **成立**：违反了 §3.5 自己的规则，A3-G6 的校验器会拒绝它 | `cells_parquet` → `[segmentation_run]`；`regions_parquet` → 所涉切片的 `raw_slide`，工作区列表和读到的配置文件写进 `parameters`（§3.5 表） |
| 必修 2 | `corrected_channel` 的 `operates_on` 几何没定义；建议 `type = "correction_group"` | **前一半成立**：v1 只写了「该 ROI 组的 `region_id`」，没写怎么算。**后一半不采纳**：组的几何就是从同一个 ROI 字典写进去的（`core/step0_handoff.py:96-97`、`:194-197`）。A2c 的越界说的是读请求超出了组，不是组与它的 ROI 不一致。新增 `correction_group` 类型，恰恰会让同一块组织出现两个 `reg_…`，也就是审核自己担心的情况 | `operates_on` = 所属 ROI 的 `region_id`；组的范围写进 `parameters.valid_bounds`；对不上时 `operates_on` 留空并带 `region_mismatch` 标记；在 §3.1 列出 `type` 的枚举（§3.1、§3.5，A3-G3 ④） |
| 必修 3 | 多边形规范化没定义 | **成立** | 6 步规则 + 例子（§3.1）；A3-G3 加了循环移位 / 反向 / 闭合点 / `null` 与 `[]` 的测试和注入 ③ |
| 必修 4 | manifest / `transforms.json` 的写入原子性 | **成立**：Save 路径中途崩溃会留下半个 manifest | `write_json_atomic`（临时文件 + fsync + `os.replace`），只用于这两个新写入点，`save_json` 的旧调用方不变（§3.4）；A3-G1 加了注入 ④ |
| 补充 1 | `segmentation_run_id` 冲突不能无声消失 | **成立**；但「拒绝登记」本身就会让运行消失。`artifact_id` 用 uuid，本来不会冲突，冲突的只是细胞键 | 照常登记 + `segmentation_run_id_collision` 标记 + 日志；graph 报告；`build_object_tables` 拒绝生成（§3.1、A3-G6 ⑤） |
| 补充 2 | `location.path` 统一用 `/` | **成立** | §3.5；A3-G6 ④ |
| 补充 3 | A4-G3 红了要改契约文本，不改 viewer | **成立**，也符合 `AGENTS.md` 规则 5 | 写进 A4-G3 的说明：先停下报告，改 §3.2 前要得到用户确认 |
| 补充 4 | P2 里加 `native_tile_shape_per_level`，给 A9 用 | **不采纳**：<br>① 它超出了 v2.4 批准的 P2（`kind`、层数、最粗层形状），属于新需求；<br>② A9 打开的是活的 `PixelSource`，`native_tile_shape(level)` 随时可读。持久化反而多出第二份真相：文件被换掉而指纹没变时，记录就是错的；<br>③ A9 需要时由 A9 的申请自己决定。<br>你如果要加，它只是 `sources` 里多一列，A3-G4 加一条对照测试即可 | 不改 |
| 风险措辞 | §8 第一条要更直白 | **成立** | 已改写：「身份，不是校验」，中间像素被改不会被发现，升级后旧 id 仍然有效 |
| 确认 | C: 盘空间、P4 的平移项 | 同意 | 回归前照 §7.4 再查一次磁盘空间 |

v2 的这些改动没有扩大白名单（§5），也没有改变估计（§9）。

---

## 12. 用户裁定（2026-10-01）

「13 题全部按建议项，授权提交，然后开始执行」：

1. `slide_id` = `slide_sig_v1`（大小 + OME-XML + 各层形状 / dtype + 头尾各 4 MiB），算法名一起记录；
2. `region_id` 由几何确定，`roi_id` 即 `workspace_id`；
3. 出处记录放在 `provenance/<artifact_id>.json`，每个产物一个文件；
4. 最小布局 v1 冻结为逻辑布局 + 对照表，物理上只新增 `transforms.json`、`provenance/`、`objects/`；
5. 旧项目在新代码第一次写入时加上 `project_schema_version: 1`，不补登记；
6. 登记失败只记日志，产物保留；
7. `cells.parquet` 只提供库函数和命令行，不接入 GUI；
8. `regions.parquet` 放在项目级 `objects/regions.parquet`；
9. 安装 `pyarrow==25.0.1`，并记入环境清单；
10. 未知项目版本只走已有的错误路径，不改 UI；
11. 生产者覆盖范围照 §3.5；
12. A0 草案 §2、§3 改为冻结版，并加上 P4 对应表；
13. v2 的修订（§11）。
