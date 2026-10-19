# F4：Regions 结果在下游的使用（草案 v1，待 codex / Mythos 审核）

用户 2026-10-18 的想法：
1. TMA：Step1 预分割、Step2 正式分割只在每个核心的区域里做，不包括排除区。
2. 排除区默认全通道排除。
3. Step4 生成 h5ad 时，同时输出每个核心属于哪个样本，以及该样本打孔时记录的 TMA 编号（例如 TMA123）。
4. 想法不一定完善，欢迎提出更好的意见。

## 0. 现状（代码事实，file:line 见调研报告）
- 下游目前**完全不读** `regions.json` / `tma_cores.parquet`；分割和定量都没有"排除"机制。
- 一个"区域"目前就是 Step0 `roi_config.json` 里的一条：一个外框加一个多边形（单环、无洞）。工作区实际上只用 `rois[0]`。
- Step1：读窗口 = patch + HALO，裁到外框内，多边形外的像素置 0（`core/preseg_input.py` `RoiMask` / `fused_window`）。像素身份 `pixel_key` 包含 ROI 几何（`core/preseg_run.py:52-78`），Step1→Step2 合同靠它判断"同一输入"。
- Step2：每个 ROI 一份 `fused_<roi>.zarr`、一张标签图（外框大小，编号只在该区域内唯一）。所有区域共用同一套 n_rows × n_cols 网格。多 ROI 时不允许跳过空白小块。不按多边形过滤细胞。
- Step4：`workers/feature_extract_worker.py` 每个 (run, roi) 写一个 h5ad。obs 里只有形态学列，没有 slide / region / 样本列；区域信息只在 `uns.provenance_json` 里。
- 对应表（真实文件 TMA138.1.xlsx）：
  - `Lookup` 表的列：Unique TMA sample ID（位置序号 1..n）、TMA Block ID（RB_1）、TMA Block Name（TMA138.1）、Donor Block ID（样本，如 K2024_30401_2-7；位置 1 是 Liver 对照）、Donor Core Height、Annotation 等；
  - `Information` 表：TMA block name = TMA138.1，Project name = TMA138。
  - 现有 `tma_cores` 表只有 position_key / position_id / x / y / label / occupied / donor_block_id / sample_id（等于位置序号）/ region_id / status（中文显示文字）/ geometry / layout_version，没有 block name / project / block id / annotation。

## 1. 要定的核心问题：约 150 个核心怎么进入"一个区域 = 一个 ROI"的流水线
- **方案 A：每个核心一个区域**（工作区里约 150 个 ROI）。
  - 每个核心一份 fused zarr、一张标签图、一个 h5ad；
  - 要改：多 ROI 的工作区 / 交接 / Step1 当前 ROI、跳过空白小块、每个区域的网格等。
  - 现有的"区域必须是 Step0 提交过的"规则也要放宽。
- **方案 B（我倾向）：整片一个区域，加一张"分析掩膜"**。
  - TMA 只在整片工作区里编号；Step0 对整片做了校正，已有整片的校正数据。
  - 分析掩膜 = 选中的核心的并集 − 排除区（ROI 项目：ROI 多边形 − 排除区）。
  - 掩膜外的像素置 0（和现在 ROI 外置 0 是同一种做法），同时让 Step2 跳过掩膜内一个像素都没有的小块（TMA 大部分是玻璃，速度主要靠这个）。
  - 分割得到一张整片的标签图；每个细胞再按重叠最多的原则归到一个核心。
  - Step4 输出**一个** h5ad，用 obs 列区分核心和样本，方便直接按样本分析。
  - 好处：不碰"一个工作区一个 ROI"的结构，改动集中在掩膜和归属两处。
  - 风险：整片标签图很大（58320×28800 uint32，约 6.7 GB），要确认 Step2 / Step3 / Step4 能处理整片大小的标签图（现有 full_wsi 模式本来就是整片）。

## 2. 掩膜规则（两种方案都适用）
- 像素级：在核心多边形内、且不在任何排除区内，才参与分割。
- **排除区的通道**：
  - 分割是按像素做的，没法只对某些通道分割，所以 Step1/Step2 一律整块排除（不管排除区勾了哪些通道），这和用户"默认全通道排除"一致。
  - 提案：排除区如果只勾了部分通道，在 Step4 里，落在该区域的细胞照常保留，只把那几个通道的数值设为缺失（NaN），并在 obs 里标出来。需要用户确认要不要做。
- **边缘细胞**：掩膜外的像素置 0，可能把细胞切掉一半。提案：Step4 给每个细胞记 `in_mask_fraction`（细胞面积里落在掩膜内的比例），低于某个阈值（例如 0.5）的标为边缘细胞，不删除。
- **哪些核心参与**：
  - 只要有编号、有区域的核心；
  - 质控 Exclude 的不参与；
  - Regions 里的 "Run" 勾选：勾了就只做勾选的，一个都没勾就做全部？需要用户确认。
  - 没编号的核心跳过并给出提示；`regions.json` 里有未完成事项（`complete=false`）时提示，但不阻止。
- **非 TMA 的 ROI 项目**：排除区同样生效（掩膜 = ROI 多边形 − 排除区）。

## 3. 一致性与来源
- 掩膜要进入 Step1 的 `pixel_key`：记录用了哪一份 Regions 结果（`content_sha256`）、选了哪些核心、掩膜规则版本。Step1 和 Step2 必须置 0 同样的像素。
- Step1 / Step2 开始时冻结所用的 `regions.json` 内容。之后如果用户又 Save 了，提示"结果已过期"，不自动重算。
- 来源记录：`segmentation_run` 依赖 `regions_layout`；`operates_on` 写核心的 region_id。
- `table_matches()` 不一致时拒绝使用，提示用户重新 Save。

## 4. Step1 预分割
- 随机小块：只在选中的核心内取（小块要完全落在"核心 − 排除区"内，或者掩膜内的比例超过阈值）；Step0 手动选的小块落在掩膜外的过滤掉。
- 预览照常；掩膜外显示为 0。

## 5. Step4 h5ad 的输出
- obs 每个细胞增加：
  - `slide_id`
  - `region_id`（核心的区域编号）
  - `tma_position`（X1-Y02）、`tma_position_id`
  - `sample_id` = Donor Block ID（样本，如 K2024_30401_2-7）
  - `tma_sample_number` = Unique TMA sample ID
  - `tma_block_name` = TMA138.1、`tma_project` = TMA138、`tma_block_id` = RB_1
  - `core_qc`
  - `in_mask_fraction`、`edge_cell`
  - 与部分通道排除区重叠的标记
- uns：对应表的 sha256、Regions 结果的 `content_sha256`、掩膜规则，以及每个核心一行的汇总（细胞数、面积、样本）。
- 需要扩展 `tma_cores` 表（结构版本升到 2）：加 block_name / project / block_id / annotation / core_height，并把中文 status 改成稳定的英文代码（中文只用于显示）。
- 需要用户确认："打孔时记录的 TMA 编号"指的是 TMA Block Name（TMA138.1）还是 Project（TMA138），还是对应表里的其他字段（比如 Annotation）？草案是两列都输出。

## 6. 分段（草案）
- F4-1：分析掩膜与核心选择（core 层，纯函数 + 测试）；扩展 tma_cores 表到第 2 版。
- F4-2：Step1 接入（掩膜、pixel_key、随机小块）。
- F4-3：Step2 接入（掩膜、跳过空白小块、来源记录）。
- F4-4：Step4 接入（细胞归属、obs / uns 列、部分通道排除）。
- 每段交 codex 审核，用户真机验收后进入下一段。

## 7. 请审核者重点讨论
1. 方案 A 和方案 B 哪个更好？有没有第三种方案？
2. 置 0 掩膜对 Mesmer / Cellpose 等模型的影响：边缘伪影，以及置 0 区域旁的细胞会不会被切出假边界？
3. 细胞归属到核心的规则，以及边缘细胞的阈值。
4. 部分通道排除放在 Step4 用 NaN 处理是否合理。
5. h5ad 字段命名、一个文件还是多个文件、AnnData 惯例（obs 分类类型、uns 结构）。
6. 有没有遗漏的风险：内存、整片标签图、跨核心的细胞编号、和现有 full_wsi 流程的兼容。

---

# 第二版（定稿；codex 和 Mythos 两轮讨论后，用户 2026-10-18 裁定；本节优先于前文）

## A. 用户裁定
1. "打孔时记录的 TMA 编号" = 对应表的 `Annotation Name`（TMA 1 … TMA 10）。TMA Block Name（TMA138.1）和项目名（TMA138）也一并输出。
2. 不置 0。模型照常看原图，分割后只保留在选中核心内、且在排除区外的细胞。
   - Find cores 的轮廓每边比组织多 6% 核心直径的玻璃圈，所以核心外缘看原图和置 0 几乎没有区别；在折叠、模糊这类排除区的边上，不会出现人为硬边。
   - 原来用于比较两种做法的小实验不再需要。
3. 碰到排除区的细胞，直接不输出。
4. 参与运行的核心：勾了 Run、编号已确认（不是"待复核"）、质控没被 Exclude、轮廓有面积、这个位置只绑定一颗核心。
   - 质控"未复核"可以参与，h5ad 里如实记录。
   - 一个都没勾就不运行。
   - 勾了但不符合条件的核心，按原因列出。
   - 其他没做完的事项不阻止运行。
5. 只勾了部分通道的排除区，这次一起做。
6. 授权：改为本版，并开始第一段（快照与规则）。

## B. 定稿设计
1. **结构**：一个父区域（整片工作区，或 Step0 的 ROI），一份 fused、一张标签图、一个 h5ad。父区域的 `region_id` 和细胞编号保持原样。没有 Regions 结果的旧流程，结果一字节都不变；TMA 选择为空时，绝不回退到整片分割。
2. **冻结快照**：Step1 交接时根据最后一次 Save 和对应表建立一份快照，Step2 复制进运行目录，Step4 读运行目录里的那份。之后再 Save，只提示"有更新的 Regions 结果"。快照包含：
   - 版本；
   - Regions 结果（`save_count`、`content_sha256`）；
   - 对应表（sha256、block_id、block_name、project_name）；
   - 每颗选中核心：position_key、position_id、位置标签、region_id、轮廓、外框、donor_block_id、打孔序号、Annotation Name、core_height、质控；
   - 排除区：类型、原因、通道、轮廓、是否影响分割；
   - 分割用到的通道 S；
   - 没参与的核心及原因；
   - 各项规则。
   身份校验用"科学内容"的哈希（几何、选择、编号与质控、通道范围、规则），不含编辑记录和时间。
3. **细胞规则**（在 Step2 接缝合并之后、发布结果之前统一判定；Step3、对象表和 Step4 看到的是同一批细胞）：
   - 归属：一个细胞超过一半的像素落在某颗核心内，就归这颗核心；并列或不足一半则不归属，不输出。
   - 碰到"影响分割"的排除区（任何一个像素），不输出。
   - 输出的细胞带上：`cell_core_fraction`（落在核心内的比例）、`cell_touches_core_edge`、`analysis_include`（完全在核心内）。默认全部写进 h5ad，由分析时用这一列筛选。
   - 比例在接缝裁剪之前计算。
4. **排除区和通道**：
   - "影响分割"指通道为全部，或者与 S 有交集。S 由所选方法的实际输入通道决定，不看显示勾选。
   - 其余排除区只影响测量：细胞保留，只要细胞和排除区有一个像素重叠，这个细胞在这些通道上的所有测量都设为 NaN（X、各个表达层、分布统计、CSV 一致处理）。另外写 `layers["measurement_qc"]`（细胞 × 通道的代码），代码含义写在 `uns` 里。
5. **切块**：沿用现有网格。读窗口里没有任何选中核心像素的小块一律跳过，不受"跳过空白小块"勾选影响。
6. **像素一致**：每颗核心、每个排除区都在自己的外框上栅格化一次（和 `RoiMask` 同样的做法），窗口只取其中一片，所以各步骤判到的边界像素相同。不建立 57 张整片掩膜。
7. **h5ad**：
   - obs 增加：`slide_id`、`segmentation_run_id`、`core_position_key`、`core_region_id`、`core_position_id`、`core_label`、`donor_block_id`、`tma_unique_sample_id`、`tma_annotation_name`、`tma_block_id`、`tma_block_name`、`tma_project_name`、`core_height`、`core_qc`，以及第 3 条的细胞列。重复的字符串用 categorical，obs 索引不变。
   - uns：`regions`（快照与规则）、`tma_cores`（对应表每个位置一行，包括没参与的位置及原因、细胞数）。
8. **来源记录**：`segmentation_run` 和 `step4_h5ad` 的 `depends_on` 加上 `regions_layout`；参数里记快照哈希和 position_key。不新建登记机制。

## C. 分段与文件白名单（每段完成交 codex 审核，用户验收后进入下一段）
- **F4-1 快照与规则（core 层，不接界面）**——本次授权：
  - 改 `core/tma/map_sheet.py`：读 Information 的 Project name。旧的 `tma_study.json` 没有这一项时，如果对应表文件还在且 sha256 一致，就从文件里补出来；
  - 新建 `core/regions/analysis.py`：核心选择及原因、快照、科学内容哈希；
  - 新建 `core/regions/raster.py`：每颗核心、每个排除区栅格化一次，提供窗口查询和"窗口里有没有选中核心"；
  - 新建 `core/regions/objects.py`：每个细胞的归属、比例、排除区判定、测量 NaN 的代码；
  - 新建 `tests/test_f4_analysis.py`；更新本文件和决策日志。
  - 不改 Step1/2/4，不改 Regions 的 Save 文件格式和 `tma_cores` 表。
- **F4-2 Step1**：`core/preseg_input.py`、`core/preseg_run.py`、`core/preseg_contract.py`、`core/random_patches.py`、`ui/main_window.py`、`ui/step1_presegmentation/run_job.py`、`ui/step1_presegmentation/random_job.py`、`ui/step0/overview_panel.py`；新建 `tests/test_f4_step1.py`。
- **F4-3 Step2**：`ui/step2_page.py`、`workers/segment_merge_worker.py`、`core/seam_merge.py`、`core/tile_tissue.py`、`core/provenance.py`；新建 `tests/test_f4_step2.py`。要求在真机上整片跑一次，量时间、内存、临时磁盘，以及 Step3 的流畅度。
- **F4-4 Step4**：`core/quant_sources.py`、`core/quant_engine.py`、`workers/feature_extract_worker.py`、`core/provenance.py`；新建 `tests/test_f4_step4.py`。
- 按 AGENTS.md 第 4 条需要单独授权的（各段开始前说明）：
  - 交接合同扩展（F4-2）；
  - 强制跳过小块、接缝后按规则删细胞（F4-3）；
  - 来源记录接入（F4-3/4）。
  - 按核心切块、稀疏存储等不在本方案内。

## D. 第 2.1 版修订（codex 审第二版 `codex_f4_plan_3.out`：修改后可实施）
1. **冻结时机**：快照在 **Step1 预分割开始时**建立。每个方法组合按自己实际读的通道 S 判定排除区；交接时带上选中组合的那份快照。之后再 Save 只提示，结果仍属于原来那份。Step2 要明确比对它收到的快照和输入（现在只检查参数和 HALO）。
2. **三种情况**：
   - 没有 Regions 结果：旧流程不变。
   - TMA：选中的核心与父区域的交集；选择为空就不运行。
   - 没有对应表的项目（普通 ROI / 整片）：父区域即分析范围，只应用排除区，不要求归属核心。
   "不置 0 / 看原图"指 **不额外置 0**；现有的校正数据、fused 和父 ROI 的置 0 照旧。
3. **细胞编号**：在分配最终编号之前删掉不输出的细胞，所以发布的编号仍是连续的 1..N（满足 `seam_merge` 的现有检查）。"保持原样"指父区域的 `region_id` 和编号方式，不是删之前的数字。细胞核跟随它所在的细胞；只分割细胞核的运行，细胞核本身就是对象。
4. **裁剪前的比例**：`objects.evaluate_object(原始对象掩膜, 全局位置, raster, 父区域外框)` 按每个对象的**原始预测形状**判定。
   - Step2 在接缝合并选中候选之后、裁剪之前调用，并把结果带到它的最终编号上；Step4 直接读这些结果，不再从裁剪后的标签重算。
   - Step1 在把结果裁到小块之前调用。
5. **归属规则**：严格超过一半（因此至多一颗）。选中核心之间互相重叠时拒绝运行，并列出重叠的核心（栅格不按绘制顺序取舍）。
6. **快照包含对应表的全部位置**（每个位置的字段和是否参与的原因），历史运行不依赖当时的对应表文件。
7. **边缘**：`analysis_include` 只表示"完全在核心内"；另加 `touches_region_edge`（碰到父区域边界，可能被切）。Find cores 的 6% 外扩在相邻组织块之间、以及切片边上会变窄，人工修改过的轮廓也不保证有空白圈。
8. **Step1 小块**：保持现有规则（完全在区域内、空白不超过 40%、互不重叠）。手动小块落在分析范围外的只报告，不删除、不改勾选。
9. **旧 study 缺 Project name**：只在新快照里补；对应表文件不在或 sha256 不同就留空。不改写 `tma_study.json`。
10. F4-1 白名单补全：`docs/f4_regions_downstream_plan.md`、`docs/tma_f3_decisions_log.md`。

## E. 第三段（Step2 接入）实施细节（用户 2026-10-19 授权；按 Step2 代码调研写成）
1. **何时启用**：只有参数文件的预分割合同里带 `regions`（完整快照）时启用。手动参数、没有合同的旧文件、HQ / HQ2 / CSD 旧路径一律不变（结果一字节不变）。
2. **开始时的检查**（`SegmentMergeWorker.run`，在现有跳块检查之后）：
   - 快照随合同已核对过身份；
   - 复制一份到运行目录 `regions_snapshot.json`，Step4 读这份；
   - 父区域必须和本次分割的区域一致：整片模式比外框 `[0, H, 0, W]`，ROI 模式比该 ROI 的外框；
   - 带 Regions 的多 ROI 运行一律拒绝，并提示原因（快照只有一个父区域）；
   - 不一致就拒绝，并说明原因。
3. **强制跳块**：每个区域单独算出"读窗口里没有任何选中核心像素"的小块，在挂预取器之前定下来，与现有的组织跳块计划取并集；不受勾选影响，恢复模式也适用。
   - 跳过的小块照常走现有 `_skip_tile`（页面显示 skipped），但另记一份清单；
   - 运行元数据加 `regions.skipped_tiles`，不混进组织跳块的统计。
   - 两个重复的小块循环（`_segment_one_zarr` 和整片模式的循环）都要改。
4. **按细胞判定**（`core/seam_merge.py`）：
   - `SeamMerger` 增加可选的 `judge(tile, local) -> {label: Verdict}`，由 worker 提供：在每个小块的读窗口上用 `ObjectTally` 一次算完（`AnalysisRaster` 的窗口查询，加上区域原点换成整片坐标），并逐个对象判断是否碰到父区域边界；
   - 每个候选带上自己的判定。接缝候选的判定写进溢出文件的元数据列，`resolve` 选赢家时**不看**判定（不改变哪一块胜出）；
   - `_write` 在分配编号**之前**：判定为不保留的直接丢弃，不占编号，计入 `regions_dropped_<原因>`，它的细胞核计入细胞核丢弃统计（`regions_cell`）。因此发布的编号仍是 1..N，`validate` 不变；
   - 保留的细胞在分配编号后按编号顺序记下：核心序号、`core_fraction`、`touches_core_edge`、`analysis_include`、`touches_region_edge`，以及"只影响测量的排除区"位掩码（最多 52 个，超出则拒绝运行并说明）。
   - 运行结束写 `global_cell_regions[_<区域>].npz`（先写 `.partial` 再改名，失败时清理），并在 `label_store["regions"]` 里引用：快照文件、身份、细胞表、丢弃统计、跳块数。
   - Mesmer 两路输出：细胞核在小块内已与唯一的细胞配对，直接跟随该细胞；只分割细胞核的方法，细胞核本身被判定。
5. **来源记录**（`core/provenance.py`）：`register_segmentation_run` 在快照存在时：
   - `depends_on` 加上 `regions_layout`（按 Save 文件的位置和快照里的 `content_sha256` 查找）；
   - 找不到就记入 `unresolved_inputs`；
   - `parameters` 加快照身份和参与核心的 position_key。
6. **不做**：按核心切块、稀疏存储、改接缝合并的排序或判定规则、改模型输入（不置 0）。
7. **测试**（新建 `tests/test_f4_step2.py`）：
   - 两个循环下的强制跳块和不受勾选影响；
   - 接缝两侧细胞的判定与编号连续；
   - 被丢细胞的细胞核计数；
   - 细胞表与编号对齐；
   - 父区域不一致、多 ROI 时拒绝；
   - 没有 Regions 时输出完全不变；
   - 来源记录的依赖。
   - 另需真机整片跑一次，量时间、内存、临时磁盘和 Step3 流畅度。

### E.8 按 codex 方案审查修订（`codex_f4_step2_plan_1.out`，修改后批准）
1. **开始时的核对**（带 Regions 才做）：
   - 页面：所选 fuse 运行记录的 Fusion 设置哈希（`params["fusion_settings"]["hash"]`）必须等于合同的 `fusion_settings_hash`，否则拒绝（快照里的分割通道 S 取自这份设置）；
   - 合同方法的输入种类必须等于快照的 `input_kind`；
   - worker 端核对：
     - 切片：用 `project_identity.describe_slide` 由工作区清单的 `source_ome` 算出 slide_id，和快照的 slide_id 一致（不登记任何东西）；
     - 父区域：整片模式要求父区域外框 = `[0, H, 0, W]`、没有多边形，且原点为 `(0, 0)`（不依赖 fused 的 `bbox_fullres` 属性）；ROI 模式要求 ROI 的外框和多边形与快照父区域一致，外框尺寸与输入数组一致；
     - 带 Regions 的多 ROI 运行拒绝，与是否启用组织跳块无关。
   - 冻结快照不会被最新的 Save 替换。
2. **跳块记账**：Regions 清单独立初始化；在组织计划（包括恢复模式"不跳"）处理之后再并入；两个循环的读块函数都使用预先定好的集合；`_skip_tile` 显示不变，按原因分别记数。
3. **事实表文件**：
   - 写 `global_cell_regions[_<区域>].npz.partial` 时用文件对象，避免 NumPy 自动加 `.npz`；
   - 成功后改名，失败、Stop、异常时删除这个文件；
   - 表完成之前不发布结果。
4. **表的编码**：
   - 行数严格等于最终对象数，第 i 行对应编号 i+1；
   - `core` 用 int32，无核心为 -1（ROI 项目也是 -1）；
   - `core_fraction` 用 float32；
   - `flags` 用 uint8：第 0 位 touches_core_edge，第 1 位 analysis_include，第 2 位 touches_region_edge；
   - `meas_bits` 用 uint64：快照中"只影响测量"的排除区按出现顺序编号 0..k-1，超过 52 个拒绝；
   - 表里另存编号到排除区 uid 的对照。
5. `touches_region_edge` 完全按 `evaluate_object` 的条件：碰到父区域外框边界，或有像素落在父多边形之外。
6. 被拒绝的细胞只统计最终被选中的那个版本，它的细胞核按不同编号计入 `regions_cell`，进入 `label_store` 的丢弃汇总。没有 Regions 时，元数据不新增任何字段。
