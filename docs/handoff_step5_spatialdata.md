# 交接：Block01 的 SpatialData 导出 → Step5 需要的读取组件（2026-11-02）

**给谁**：Step5 开发窗口（代码 `/sda1/Fusion/analysis_pipline/deepseek/step5_v10/repo`，环境 `fusion_test2`）。
**什么时候做**：Step5 当前任务完成之后（用户 2026-11-02 指示）。
**用户已定**：Block01 写**标准 SpatialData**，Step5 加读取组件；不要求 Block01 写 Step5 自己的数据集目录格式。

本文件只描述事实和需求，不替 Step5 做设计。Step5 一侧怎么实现，按 Step5 自己的流程（计划 → 审核 → 用户批准）来定。

---

## 1. 背景：为什么 Step5 现在读不了

Block01 的 Step4 有一个新的输出选项 **SpatialData**（只读方式调查过 Step5 v10 的代码，没有改动）：

- Step5 只打开**自己的数据集目录**（`step5_dataset.json` + `data.zarr`，见 `application/dataset/store.py:116-125`）。目录里没有 `step5_dataset.json`，Step5 就拒绝打开。
- 打开时还会删除清单里没有登记的表格（`store.py:600-605`）。
- Step5 唯一一个接受普通 SpatialData 对象的入口是 `application/spatial_statistics/spatialdata_entry.py`，目前只有测试在调用。
- Step5 现在的正式入口是 **Block01 的 h5ad**：先导入（`application/dataset/import_h5ad.py`），再在 `tools/mutation.py:2454-2545` 里按样本合并。

所以 Step5 需要一个**新的导入组件**：读 Block01 的 SpatialData，生成 Step5 自己的数据集。

## 2. Block01 写出的内容（输入契约）

每个 Step4 结果写**一份**，即一个运行（run）的一个区域；它就放在 h5ad 旁边：

```
<…>/runs/quant_<时间>/<区域>/cell_features.spatialdata.zarr
                              cell_features.h5ad
                              cell_features_provenance.json
                              cell_features.csv        （勾选时才有）
```

如果设置了前缀，文件名是 `<前缀>_cell_features.*`。

写入工具：spatialdata **0.5.0**，在 `fusion_test2` 里运行。格式是 zarr v2。

### 2.1 元素

| 元素 | 内容 |
|---|---|
| `labels/cell_labels` | 细胞分割掩膜，uint32，0 = 背景，值 = `cell_id`。<br>每边都不小于 512 像素时带多级缩放（`scale0`…，每级 ×2，最多 4 级），否则只有一级。 |
| `labels/nucleus_labels` | 细胞核掩膜。<br>• 细胞运行带核时才有，此时不被表格注释（表格每行是一个细胞）。<br>• 纯核运行中，它是**主元素**。 |
| `tables/table` | 与 h5ad 内容相同的 AnnData（X、layers、obs、var、uns、obsm），见 2.2。 |
| `shapes/regions` | Regions 的轮廓（GeoDataFrame）。只在运行带 TMA 核心或测量排除区时才有。见 2.3。 |
| 图像 | **不包含**。原图与校正图的位置写在 `uns["source_images"]` 里。 |

### 2.2 表格 `tables/table`

- **注释关系**（`uns["spatialdata_attrs"]`）：

  | 运行类型 | region | region_key | instance_key |
  |---|---|---|---|
  | 细胞运行 | `"cell_labels"` | `"region"` | `"cell_id"` |
  | 纯核运行 | `"nucleus_labels"` | `"region"` | `"nucleus_id"` |

  - `obs["region"]` 是 Categorical，所有行都是同一个值。
  - `obs[instance_key]` 是 int64，等于掩膜里的像素值。
  - **0 个细胞时**：表格**不带注释**（没有 `spatialdata_attrs`），因为 SpatialData 不允许空表格注释一个区域。读取组件要能处理这种情况。
- **obs 索引**：字符串形式的 `cell_id`（例如 `"17"`）。**每个区域都从 1 开始编号**，所以合并多个样本时必须加前缀，Step5 的 `merge_samples` 已经这样做。
- **obs 主要列**：
  - **形状**：`area`、`centroid_y`、`centroid_x`、`bbox_min_y/_x`、`bbox_max_y/_x`、`major_axis`、`minor_axis`、`eccentricity`、`orientation`、`equivalent_diameter`、`aspect_ratio`、`extent`、`boundary_pixel_count`、`perimeter_crofton`、`circularity`。
    - 只在勾了 Morphology 时才有。
    - 都是**整张切片的全分辨率像素坐标**。
  - **核汇总列**：勾选时才有。
  - **TMA 运行的列**：`slide_id`、`segmentation_run_id`、`core_position_key`、`core_label`、`donor_block_id`、`tma_unique_sample_id`、`tma_annotation_name`、`core_qc`、`core_area_px/_um2/_mm2`、`cell_core_fraction`、`analysis_include`、`touches_region_edge` 等，文字列是 Categorical。
- **`obsm["spatial"]`**：(n, 2) float64，内容是 **(x, y)**，全分辨率切片像素。
  - 来源：勾了 Morphology 时取 Step4 的 `centroid_x/y`；没勾时由掩膜算出（定义相同：像素坐标的平均值）。
  - **这一项总是存在**。
- **`X`**：主区域的第一个统计量，float32 稠密矩阵（`uns["x_statistic"]` 写明是哪个）。
- **`layers`**：`<区域>_<统计量>`，float32，例如 `cell_mean`、`nucleus_max`。带 Regions 时还有 `measurement_qc`（uint8 代码，含义在 `uns["measurement_qc_codes"]`）。被测量排除的值是 NaN。
- **`obsm` 的分布统计**：`cell_median`、`nucleus_p90`……，是 DataFrame，列名是 marker。
- **`var`**：通道名作为索引，另有 `slide_index`、`decision`、`source`、`correction_method`、`correction_param`。
- **uns 里与 Step5 有关的键**：

  | 键 | 内容 |
  |---|---|
  | `x_statistic` | 小写，Step5 的 discovery 读的就是它。h5ad 里叫 `X_statistic`；SpatialData 不允许只差大小写的两个键，所以这里只有小写一个。 |
  | `ome_tiff` | 原始切片路径（qptiff / ome.tiff）。 |
  | `source_labels` | `{"cell": <掩膜 zarr 路径>, "nucleus": …}`，Step2 LabelStore 的位置。 |
  | `source_images` | `{"slide": {"path", "size", "mtime_ns"}, "channels": {"0": {"name": <通道名>, "slide_index", "source": "raw"或"corrected", "path", "array", "identity_json"}, "1": …}}`。<br>按序号作键，因为通道名里可能有 `/` |
  | `provenance_json` | Block01 完整的来源记录（JSON 字符串）。 |
  | `regions`、`tma_cores` | TMA 运行才有，是 Regions 快照与每个核心的信息。 |

- **刻意没有写 `mask_path`**：Step5 的 montage 把 `mask_path` 当 TIFF 打开（`tools/plotting.py:734`），而 Block01 的掩膜是 zarr。写进去会让 montage 出错。掩膜本身就在这个文件的 `labels/` 里。

### 2.3 形状 `shapes/regions`

- 每行一个 TMA 核心或一个测量排除区。几何是 Polygon 或 MultiPolygon（同一核心的多个碎片取并集），坐标是全分辨率切片像素 (x, y)。
- 列：

  | 列 | 内容 |
  |---|---|
  | `kind` | `core` 或 `exclusion` |
  | `uid` | |
  | `position_key` | 核心才有 |
  | `name` | 核心名，或排除区类型 |
  | `run` | 该核心是否参与了这次分析 |

### 2.4 坐标系

| 坐标系 | 含义 | 转换 |
|---|---|---|
| `global` | 整张切片的全分辨率像素 | • labels 带 `Translation(y0, x0)`，即区域左上角，因为掩膜只覆盖这个区域。<br>• shapes 与 `obsm["spatial"]` 本来就是切片像素。 |
| `micrometers` | 微米 | 仅当切片记录了像素尺寸时才有。<br>• labels：先平移再缩放。<br>• shapes：按 x、y 缩放。 |

## 3. 建议 Step5 新组件做的事（需求，不是设计）

1. **导入一个或多个 `cell_features.spatialdata.zarr`**，生成 Step5 数据集（`write_dataset` 那条路），效果相当于现在导入 h5ad：
   - 表格 → `root` 表；
   - `obsm["spatial"]` → Points `cells` 的 x、y。
2. **多样本合并**：与 `merge_samples` 同一规则。
   - `obs["sample"]` 取样本名。可选来源：路径（`<根>/<样本>/…`）、`tma_unique_sample_id`，或者让用户指定。
   - 索引加前缀。
   - `uns["sample_meta"]` 记录每个样本的 `ome_tiff` 与 zarr 路径。
3. **0 细胞的区域**（表格不带注释）不能让导入失败。
4. **labels 与 shapes**：Step5 现在不用它们，按需决定是否保留引用。将来 montage 要画细胞边界时，可以直接从 `labels/cell_labels` 读（zarr，按区域平移），不必再找 TIFF。
5. **版本差异**：Block01 用 anndata 0.11.4 写 h5ad；`fusion_test2` 里是 anndata 0.10.9。导出脚本用 0.10.9 读 Step4 的 h5ad，Block01 的测试已覆盖 X、layers、QC、类别列、分布 obsm 等。Step5 侧不需要再处理 h5ad。

## 4. 测试样本（合成数据，可随便读）

`/sda1/Fusion/test_data/block01_spatialdata_samples/`

| 文件夹 | 内容 |
|---|---|
| `sampleA_tma_cells_nuclei/Full_WSI/` | TMA，细胞 + 核，3 个核心外加 1 个测量排除区；有核 / 胞质 layers、`measurement_qc`、分布统计 obsm；还附带 h5ad 与 CSV，可以对照 |
| `sampleB_plain_cells/Full_WSI/` | 普通区域，只有细胞，没有 shapes |
| `sampleC_nuclei_only/Full_WSI/` | 纯核运行（主元素是 `nucleus_labels`，instance_key 为 `nucleus_id`） |

每个样本有 39 个对象，掩膜 130×130，区域左上角在切片坐标 (10, 20)，所以 labels 带平移。

真实规模参考：整张切片掩膜 46,800 × 26,880、2.1M 细胞、50 通道时，文件约 1.6 GB，写出约 17 秒。

## 5. 现状与联系方式

- **Block01 一侧**：
  - 计划：`block01_v16/docs/v17_spatialdata_export_plan.md`
  - 导出脚本：`block01_v16/scripts/export_spatialdata.py`（不依赖 Block01，Step5 也可以直接看）
  - 测试：`block01_v16/tests/test_spatialdata_export.py`
  - 状态：已实现，codex 代码审查三轮后通过，已提交并推送：分支 `v17`，提交 `0fe5255`。
- **以后可能的变化**：
  - Block01 的一键安装会另外建一个专门用于 SpatialData 的环境，不再借用 Step5 的环境。
  - 将来可能可选把校正图或融合图写进 `images/`。
  - 契约若有变化，会更新本文件。
