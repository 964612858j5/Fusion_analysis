# v16 契约草案：PixelSource、坐标、对象表（A0-3）

日期：2026-09-29。**§1 在 A2a 冻结，§2、§3 在 A3 冻结（2026-10-01，`docs/v16_A3A4_application.md` v2，用户批准）**；冻结版的权威文本是代码 `core/project_identity.py`、`core/provenance.py`、`core/artifact_graph.py`、`core/object_tables.py` 的模块说明，本文件只作说明。原日期 2026-09-29 的草案文字保留，冻结时的改动写在各节开头。依据 v2.1 §2.4、§5.1、§6，以及 A0 申请 v2 §2.3 的调查结论（行号基于 `bf3af2c`）。
本文件不授权任何代码改动。「今天」一栏描述现有代码，按 v2.1 §6.2，旧代码不回改。

---

## 1. PixelSource

> **A2a 冻结（2026-09-30）**：契约的权威文本已经是代码 `core/pixel_source.py`（它的模块说明列出所有语义），适配器在 `sources/`；本节只作说明。与下表相比，冻结版有这些变化（A2a 申请 v2、独立审核）：
> - 身份是 core 自己的 `PixelSourceIdentity`（不再用 viewer 的 `SourceIdentity`）；它是运行时 / 存储身份，`slide_id` 另留字段；
> - 新增 `valid_bounds(level)`：`read_region` 返回请求与它的交集及实际起点，交集为空时抛出 `OutOfBounds`，任何源都不拿别的源的像素去填；
> - `read_tile(channel, level, ty, tx, tile_size)` 用普通整数，不用 `TileAddress`；
> - `scan(channels, level, tile_size)` 属于契约，预读深度不属于契约；
> - `close()` / `with` 与并发读语义属于契约；
> - `level_downsample_rounded` **不在契约里**，只作为 OME-TIFF 适配器的遗留兼容属性 `legacy_level_downsample_rounded`，只能作缓存键；
> - `channel_names()` 用规则 (c)：有名的保留，缺名的那一个记为 `ch_NN`，`Channel` 元素数与页数不符时全部 `ch_NN`；不套用 `name_map`；
> - `physical_size()` 返回 `(dy_um, dx_um)` 或 None。

### 1.1 最小接口（v2.1 §5.1）

| 方法 | 语义 | 今天对应的调用 |
|---|---|---|
| `source_identity()` | 这些像素**是什么**：路径 + 指纹 + 阶段（raw / corrected_saved）；任何字段变化都使缓存失效 | `RawTileProvider.source_identity()` → `SourceIdentity(dataset_path, dataset_fingerprint="size:mtime_ns", stage, corrected_artifact)`（`viewer/tile_types.py:37-43`） |
| `channel_names()` | OME 通道名，按页顺序；缺名或数量不符时统一用 `ch_NN` | `RawTileProvider._parse_channel_names`（`viewer/raw_tile_provider.py:98-112`）；`OMETIFFLoader._parse`；`quant_sources._slide_channels` |
| `dtype(channel)` | 源的原生 dtype，**不做 float 转换** | `RawTileProvider.read_region` 返回原生 dtype；`TiffTileReader.dtype` |
| `level_count()` | 层数 | `num_levels` |
| `level_shape(level)` | `(H_L, W_L)` | `level_shape` |
| `level_downsample(level)` | **几何用**：`(H0 / H_L, W0 / W_L)`，每轴、不取整 | `level_downsample_yx`（:181-195） |
| `read_region(channel, level, y0, y1, x0, x1)` | 半开区间，按该层像素；越界部分截掉，并返回实际起点 | `RawTileProvider.read_region` → `(array, (y0, x0))` |
| `read_tile(channel, TileAddress)` | 按分块网格读 | `RawTileProvider.read_tile` |

补充约定：

- **`level_downsample_rounded(level)` 只能作键**（参数缩放、缓存键），**不能用于几何计算**，与今天 `level_downsample` 的 docstring 一致（:169-179）。新代码和 TMA 代码的几何只用每轴真实比例。
- 性能提示是可选的，比如 `native_chunk_shape()` 和 `preferred_threads()`。任何科学结果都不能依赖某个后端特有的字段。
- 源的 `physical_size()` 返回 `(PhysicalSizeY, PhysicalSizeX, unit)`，读不到时返回 **None**，不猜测、不给默认值。今天产品不读这项（见 §2.5）。
- 多个通道一次读：`read_regions(channels, level, y0, y1, x0, x1)` 作为可选的批量接口，Step4 和 fusion 用得上。它必须与逐通道调用 `read_region` 的结果逐位相同。

### 1.2 实现与消费者（A2a / A2c）

- `OmeTiffSource`：**委托**给 `RawTileProvider`（区域 / 分块 / 多级读）和 `TiffTileReader`（第 0 层扫描，含快速分块路径与 aszarr 退路），不吸收、不合并它们（v2.2 §5.2；本行原来写的「吸收」已按 A2a 改正）。
- 校正 zarr（Step0 已提交的产品，float32、`roi_only`、按 ROI 分组）通过同一接口暴露，**只在它自己的 ROI 组里读**，沿用 `quant_sources` 的严格查找，不退到别的 ROI 组。Step4 的 fail-closed 来源契约不变。
- `NgffSource`：只有 A0 采纳 NGFF 时才实现（A2b）。

### 1.3 身份

`source_identity` 沿用今天的路径 + 指纹，**另留 `slide_id` 的位置**（§3）。指纹 `size:mtime_ns` 不是内容哈希；拷贝或 touch 过文件，指纹就会变。这一点写入契约，A3 决定是否补内容哈希。

---

## 2. 坐标

> **A3 冻结（2026-10-01）**：§2.1、§2.2 按原文冻结，变换由 `core/project_identity.SlideFrames` 实现：
> - 第 L 层像素 i 的中心在 global 中位于 `(i + 0.5) · s_L − 0.5`；
> - `viewer_world = global + 0.5`，拾取 `floor(world)`；
> - 物理尺寸缺失就不可用，不给默认值。
>
> 与草案相比有两处改动：
> - `transforms.json`（§2.3）**按 `slide_id` 分组**，因为一个项目目录里可能有不同切片；
> - 区域原点**不再**复制进 `transforms.json`：区域的 bbox 只有一处真相，即 Step0 提交的 `roi_config` / `roi_manifest`，`regions.parquet` 是它的表格形式。
>
> P4 的对应表见 §2.6。

### 2.1 坐标系

| 名称 | 定义 | 单位 |
|---|---|---|
| `global_pixel` | 切片第 0 层的像素下标 | px |
| `physical_um` | `global_pixel × PhysicalSize`；源没有物理尺寸就**不可用** | µm |
| `pyramid_level` | 第 L 层的像素下标 | 该层 px |
| `tile_local` | 某个分块内部的下标（分块原点 + 局部） | px |
| `region_local` | 区域（ROI / TMA core）自己的坐标：`global_pixel − region 原点` | px |

### 2.2 约定（待冻结）

1. **轴顺序**：数组按 (y, x)，点按 (x, y)。schema 的每个字段都写明是哪一种。
2. **整数下标 = 像素中心**（今天 Step4 的约定，`core/quant_engine.py:229-250`、`:427-482`；标签金字塔的取样也是中心，`core/label_pyramid.py:9-17`）。质心是整数下标的平均。
3. **viewer 适配层**：viewer 的世界坐标把第 0 层像素 j 画在 [j, j+1)（`ui/step1_gpu_binding.py:912-918`、`core/step3_masks.py:702-738`），所以
   `world = global_pixel + 0.5`（像素中心），`global_pixel = floor(world)`（拾取）。
   这是**适配规则**，不改旧 viewer 代码。新代码在测量坐标和显示坐标之间转换时必须显式经过它。
4. **bbox 半开**：[x0, x1) × [y0, y1)。
   - 今天的 `bbox_fullres` 一律是 **`[y0, y1, x0, x1]`**（`utils/roi_project.py:49-53`、`core/label_ownership.py:43-49`、`core/seam_merge.py:61-66`），同样是半开区间。
   - 对象表的列：`bbox_x0 = bbox_fullres[2]`、`bbox_y0 = bbox_fullres[0]`、`bbox_x1 = bbox_fullres[3]`、`bbox_y1 = bbox_fullres[1]`。**写法不同，语义相同**；旧字段不改顺序。
   - Step4 h5ad 的 `bbox_min_y / bbox_min_x / bbox_max_y / bbox_max_x` 中，max = 最后一个下标 + 1，也是半开的，可以直接对应。
   - 已知的一处不一致：`ui/step0/step0_page.py:11330` 判断 patch 中心时用了闭区间 `ry0 <= cy <= ry1`。只列出，不改。
5. **金字塔变换用每层真实形状**：`scale_y = H0 / H_L`、`scale_x = W0 / W_L`。本切片第 1 层为 4.000259 / 4.000740，第 2 层为 16.013485 / 16.006910。
   - 第 L 层像素 i 的中心在 global_pixel 中位于 `(i + 0.5) × scale − 0.5`。这与标签金字塔取 `floor((i + 0.5) × ds)` 作为代表像素是相容的。
   - 禁止 `2**level`、禁止取整后的名义倍数。
6. **µm 只来自 OME `PhysicalSizeX/Y`**。读不到就把 `physical_um` 标为不可用，**绝不假设 mpp**。
7. **规则的约束范围**：「不准悄悄写 `x / 2**level`、`x − bbox_x0`、`x × mpp`」只约束新代码和 TMA 代码。

### 2.3 `transforms.json` 草稿

```json
{
  "version": 1,
  "slide_id": "<slide_id>",
  "global_pixel": {"shape_yx": [15437, 16215], "center_convention": "integer index = pixel centre"},
  "physical_um": {"available": true, "size_yx_um": [0.5068606698203042, 0.5068606698203042],
                  "source": "OME PhysicalSizeY/X"},
  "pyramid_levels": [
    {"level": 0, "shape_yx": [15437, 16215], "scale_yx": [1.0, 1.0]},
    {"level": 1, "shape_yx": [3859, 4053], "scale_yx": [4.000259134490801, 4.000740192449297]},
    {"level": 2, "shape_yx": [964, 1013], "scale_yx": [16.013485477178424, 16.006910167818362]}
  ],
  "regions": {
    "<region_id>": {"origin_yx": [0, 0], "shape_yx": [15437, 16215],
                    "global_to_region": "subtract origin"}
  },
  "viewer_world": "world = global_pixel + 0.5 (pixel j covers [j, j+1))"
}
```

### 2.4 TMA core 如何放进来

- TMA core = `Region(type = "TMA_core")`，有自己的 `origin_yx` 和 `shape_yx`。
- `region_local` 与今天的 ROI 完全同构（`label_store` 的数组本来就是区域局部坐标，`core/label_pyramid.py:3`）。**不需要第二套坐标模型。**
- 若 core 不是轴对齐的矩形，就用 bbox + 多边形掩膜表示，变换仍然只是平移。

### 2.5 已知偏差（只列出，不在 A0/A3 修）

- `PhysicalSizeX` 在产品里从不读取。Mesmer / 参数表写死了 `image_mpp = 0.5`：`utils/segmentation_param_schema.py:108`、`seg_runner/engines.py:185`、`workers/mesmer_worker.py:85`、`:94`、`:196`、`:205`、`ui/step2_page.py:1732` 等。本切片实际是 0.50686（差 1.4 %）。**是否修改由用户另行裁定**；它会影响 Mesmer 的结果，不属于坐标契约本身。
- 用取整比例做几何的旧代码（`corrected_coarse.zarr` 的 `stride: 16`、`viewer/step1_source.py` 的整数 stride 块、`OVERVIEW_DOWNSAMPLE = 32` 等）在 v2.1 §6.2 下保持不动。

---

## 2.6 P4 — 与 OME-NGFF / SpatialData 的语义对应（A3）

FusionFlux 的每个变换都是轴对齐的缩放加平移，所以都能**无损**写成 OME-NGFF 0.4 的 `scale` + `translation`，以及 SpatialData 的 `Scale` / `Translation` / `Sequence`。**反过来不成立**：任意的 NGFF / SpatialData 变换（仿射、旋转、z / t 轴、多重序列）不能映射回 FusionFlux。这里只写语义对应：不引入依赖，不写转换代码（导出见 v2.4 §11 的 `export_to_spatialdata()`）。

| FusionFlux | OME-NGFF 0.4 | SpatialData |
|---|---|---|
| 轴 (c, y, x) | `axes`：c 为 channel，y、x 为 space，单位 micrometer（没有物理尺寸就不写单位，即像素） | dims `("c","y","x")` |
| 第 L 层 → µm | `scale [1, s_y·d_y, s_x·d_x]` + `translation [0, (s_y−1)/2·d_y, (s_x−1)/2·d_x]` | `Sequence([Scale, Translation])` |
| 第 0 层 → µm | `scale [1, d_y, d_x]` | `Scale([d_y, d_x])` |
| `region_local` | `translation [y0·d_y, x0·d_x]` | `Translation([y0, x0])` |
| `viewer_world = global + 0.5` | —（只属于显示） | 内在像素坐标（像素 i 覆盖 [i, i+1)）；实现导出时再对照文档核实 |
| 细胞表 | — | `tables`，`region` / `instance_key` = `region_id` / `cell_id` |
| LabelStore | `labels` | `labels`（LabelStore 仍是语义权威） |

---

## 3. 身份与对象表

> **A3 / A4 冻结（2026-10-01）**，与草案相比有这些改动：
> - **`region_id` 不再等于 `roi_id`**。原因：一个工作区可以有多个 ROI，但只有第一个有 `roi_id`；而且 Step0 Save 每次都会新建工作区，34 个 "Full WSI" 工作区描述的是同一块组织。
>   - 冻结后：`region_id = reg_` + sha256(`slide_id`, `type`, `bbox_fullres`, 规范化的多边形) 的前 16 位十六进制；
>   - `roi_id` 就是 `workspace_id`。
> - **`slide_id`** = `slide_` + sha256(切片签名 v1：大小、OME-XML、各层形状 / dtype、头尾各 4 MiB) 的前 16 位十六进制。它是身份，不是校验；算法名 `slide_sig_v1` 一起记录。
> - **`type` 的取值**：今天有 `full_wsi`、`roi`；保留 `TMA_core`、`tumor_region`、`blur`、`fold`、`niche`、`manual_annotation`。
> - **出处记录**放在 `provenance/<artifact_id>.json`，每个产物一个文件。只有 `depends_on` 是边，`operates_on` 只放 `region_id`。
> - **最小布局 v1** 冻结为逻辑布局 + §3.5 的对照表。物理上只新增 `transforms.json`、`provenance/`、`objects/`（`objects/<run>/cells.parquet`、项目级 `objects/regions.parquet`）。
> - **权威**：
>   - LabelStore 管像素归属；
>   - `cells.parquet` 管身份键、质心、bbox、面积、核数 / 核面积，与 Step4 h5ad 的同名列必须逐位相同；
>   - h5ad 管表达量和区室统计。

### 3.1 层级与 ID

```text
Study └─ Sample └─ Slide └─ Region          SegmentationRun { segmentation_run_id, slide_id, operates_on: [region_id] }
```

| 新 ID | 今天的来源 | 说明 |
|---|---|---|
| `slide_id` | **今天没有**；切片靠 `source_ome` 路径 + `size:mtime_ns` 指纹识别 | A3 定：建议 `slide_<内容哈希前 16 位>`，或在项目 manifest 里登记一个 UUID。**路径不能当 ID**（移动文件就变） |
| `region_id` | `roi_id`（`utils/roi_project.py:41-46`：`roi_<时间>_<4 位十六进制>` / `full_wsi_…`） | **不用 `roi_name`**：运行内部今天按显示名做键（`label_store["Full WSI"]`、`global_mask_Full WSI.zarr`），显示名不唯一，也可能改 |
| `segmentation_run_id` | `run_id`（`workers/segment_merge_worker.py:254-270`：`seg_<时间>_<方法>[_NN]`） | 运行只存在于一个工作区之内。`operates_on` 由 `segmentation_meta.json` 的 `rois[i].roi_id` 得出 |
| `cell_id` | LabelStore 标签值 1..N（每个运行、每个区域各自编号） | 稳定键 = **`(segmentation_run_id, region_id, cell_id)`** |
| `sample_id`、`study_id`、`patient_id` | 今天没有 | 队列元数据，不属于像素身份；TMA 批次时引入 |

### 3.2 `cells.parquet` v1（草案）

| 列 | 类型 | 来源 / 定义 |
|---|---|---|
| `segmentation_run_id` | string | 运行 meta 的 `run_id` |
| `region_id` | string | `rois[i].roi_id` |
| `cell_id` | uint32 | LabelStore 细胞标签，1..N |
| `slide_id` | string | 见 §3.1 |
| `x_global`, `y_global` | float64 | 质心，global_pixel，**整数 = 像素中心**；等于 Step4 的 `centroid_x / centroid_y` |
| `bbox_x0`, `bbox_y0`, `bbox_x1`, `bbox_y1` | int32 | 半开，global_pixel；等于 Step4 的 `bbox_min_x`、`bbox_min_y`、`bbox_max_x`、`bbox_max_y` |
| `cell_area` | int64 | 像素数 |
| `nucleus_count` | int32 | `nucleus_to_cell` 中指向该细胞的核数（纯核运行为 1） |
| `nucleus_area` | int64 | 这些核的像素总数 |

- 没有 `qc_valid`，也不复制 Step4 的表达量（v2.1 §6.3、§6.4）。
- 空标签（`count = 0`）不出现在表里，与 Step4 相同。
- 每个键都必须能在 LabelStore 里找到，反之亦然。A4 的验收会逐项核对。

### 3.3 `regions.parquet` v1（草案）

| 列 | 类型 | 今天的来源 |
|---|---|---|
| `region_id` | string | `roi_id` |
| `slide_id` | string | §3.1 |
| `type` | string | `roi_manifest.type` / `analysis_region_type`：`full_wsi`、`roi`；以后加 `TMA_core` 等 |
| `name` | string | `display_name`（只用于显示） |
| `bbox_x0`, `bbox_y0`, `bbox_x1`, `bbox_y1` | int32 | 由 `bbox_fullres` 按 §2.2 第 4 条映射 |
| `parent_region_id` | string / null | 今天总为 null |

没有 `qc_status`（v2.1 §6.5）。

### 3.4 权威边界（v2.1 §6.3，这里只重申）

- Parquet 是身份、坐标、bbox、基本形态的权威来源。
- Step4 h5ad 是表达量和区室统计的权威来源。今天它的 `obs` 里没有运行 ID 和区域 ID，provenance 里有 `segmentation_run.run_id` 和 `region.roi_name`，但**没有 `roi_id`**。A4 生成 Parquet 时从运行 meta 补齐，不回写 h5ad。
- `analysis/<segmentation_run_id>/analysis.h5ad` 是分析状态的权威来源。

### 3.5 新旧布局对照（v2.1 §2.4）

| 新布局 | 今天 |
|---|---|
| `<project>/` | 项目根：`project_manifest.json`（`source_ome`）、`roi_index.json`、`panel.csv` |
| `segmentations/<run>/metadata.json` | `rois/<roi_id>/step2/segmentation_runs/<run>/segmentation_meta.json`（含 `seg_engine`、`label_store`、`rois[]`） |
| `segmentations/<run>/<region_id>/cells` | 同一目录下的 `global_mask_<roi_name>.zarr`（uint32，区域局部坐标） |
| `…/nuclei` | `global_nuclei_mask_<roi_name>.zarr` |
| `…/relation` | `global_nuclei_cell_<roi_name>.zarr`（`nucleus_to_cell`，`[0] = 0`，长度 M + 1） |
| `objects/<run>/cells.parquet` | 今天没有 |
| `analysis/<run>/analysis.h5ad` | 今天没有；Step4 的 `rois/<roi_id>/step4/quantification_runs/<run>/<region folder>/cell_features.h5ad` 仍是表达量的权威来源 |
| `transforms.json`、`provenance.json` | 今天没有 |

**旧项目不迁移。** 新布局只用于新项目；读旧项目时由适配层把上表映射过去。

### 3.6 引擎身份（A0.5 的输入，这里只记录事实）

- `engine_identity` 包含 `engine`、`lock_hash`（整个环境 lock 文件的哈希）、`lib_versions`、`model_checksum`、`runner_version`（`seg_runner/runner.py:23-49`）。
- **`model_checksum` 是 `models.json` 条目的哈希，不是模型权重文件的哈希。** A0.5 必须重新讨论模型文件的真实身份（审核建议，用户同意）。
- 比较方式：
  - Step1 的 `preseg_contract.build()` 要求所有 patch 记录的身份整体完全相等（`core/preseg_contract.py:62-72`）。
  - Step2 只在引擎种类不同时中止，其他字段不同只警告（`workers/segment_merge_worker.py:2313-2326`）。
