# v16 块 DV — 同一个 session 内的数据版本与分割版本管理：实施申请 v2.1

日期：2026-10-02 夜。分支 `v16`，调查基于 HEAD `a256f05`。

依据：用户 2026-10-02 对 Intensity 审核 Q3 的裁定（`docs/v16_intensity_records_review.md` §9）。用户原话要点：
- 每个 session 只对应一个 OME-TIFF，以创建时间戳区分；
- 一个 session 内会出现多个**数据版本**：背景校正、Intensity、用于融合的通道及其权重；
- 每个数据版本对应一个或多个**分割版本**；
- 数据版本变化**不会**让已有的分割版本变化；
- 所有数据版本都能**加载回 Step0 和 Step1 重新浏览**；
- 所有分割版本都能在 viewer 里叠加在 OME-TIFF 上查看；Step3 右上角下拉框的每条记录，最右侧标出它对应的数据版本（label 或 id），可以盖住记录名字的右端。

状态：**申请 v2.1**。§13 两个阻断问题已由用户裁定（§14），两处文字残留已改正。审核结论：「可以升级成 v2.1 并批准实施，不建议再继续扩设计」。没有改代码。

修订记录：v2.1 写入 §13 的裁定：corrected 写时复制的精确规则（§3.15）、A3 来源登记推迟到版本正式提交之后（§3.12），并改正 §3.6 和 §4 的残留；v2 按独立审核意见最小化修订（整份 corrected 共享、ROI 几何冻结、Generate 事务、多 ROI fused、dirty draft、去掉固定路径的残留说法），见 §12；v0 草稿；v1 写入用户审阅：§3.6 改为在 Load 的工作区选择框里按「工作区 + 版本」逐条列出，样式和 Step3 一致；§3.7 没有版本记录的旧数据显示「unknown」；第 1、3–9 题裁定。

---

## 1. 为什么要做（用实际场景说明）

**场景**：你在 test1 的工作区 `…_6bad` 里，
1. 9 月 27 日：TopHat 半径 25，CD3 权重 1.0 → Generate fused → Step2 分割 A；
2. 9 月 30 日：改成 TopHat 半径 40，CD3 权重 0.5 → 再 Generate → Step2 分割 B。

**今天会发生什么**：
- 每个工作区只有**一份**校正结果 `step0/corrected_channels.zarr` 和**一份** fused 结果 `step1/fused_Full WSI.zarr`，路径固定，第二次保存会**覆盖**第一次；
- 分割 A 和分割 B 的记录里，fused 的路径都指向同一个文件 `step1/fused_Full WSI.zarr`（`…_6bad` 里两次 Step2 运行就是这样），所以**分割 A 当时用的输入已经没有了**；
- 你想回去看 9 月 27 日那套参数下的校正结果和融合图：做不到，只剩最后一次的；
- Step3 下拉框里能看到 A 和 B 两条记录，但看不出它们各自用的是哪套参数。

**这一块要做到**：上面的第 1、2 步各自成为一个「数据版本」（v1、v2）。分割 A 属于 v1，分割 B 属于 v2。做完 v2 不会动 v1 和分割 A。随时可以把 v1 加载回 Step0 / Step1 浏览。Step3 下拉框里，分割 A 那一行右侧显示「v1」，分割 B 显示「v2」。

## 2. 现状（只读调查）

| 东西 | 现在存在哪里 | 会被覆盖吗 |
|---|---|---|
| 背景校正参数 | `step0/correction_config.json` | 每次 Step0 Save 覆盖 |
| 校正后的像素 | `step0/corrected_channels.zarr`（`…_6bad` 里约 221 MB） | 每次 Step0 Save 覆盖（增量 Save 只重算有变化的通道，但仍在同一个文件里） |
| Step0 的 Intensity | `step0/step0_channel_remap.json`（另有按内容命名的版本文件） | 每次 Step0 Save 覆盖规范文件 |
| Step0 交接 | `step0/step0_roi_result.json` | 每次 Step0 Save 覆盖 |
| 融合通道、权重、Step1 的 Intensity | `step1/step1_fusion_settings.json` | 每次 Save Fusion Settings 覆盖 |
| fused 像素 | `step1/fused_<区域>.zarr`（约 164 MB） | 每次 Generate 覆盖 |
| 预分割结果 | `step1/presegmentation_runs/<run>/` | 不覆盖，每次一个新文件夹 |
| 分割结果 | `step2/segmentation_runs/<run>/` | 不覆盖，每次一个新文件夹；但只记录了 fused 和校正结果的**路径** |
| A3 来源记录 | `<项目>/provenance/<artifact>.json` | 不覆盖。每个 fused、校正通道、分割运行各有一条，分割运行的记录里写明了它依赖哪个 fused（按 token） |

关键：分割结果本身已经按运行分开存了；**数据（校正像素、fused 像素和它们的参数）没有按版本分开**。A3 的来源记录能说明「分割 A 用的是哪个 fused」，但那个 fused 文件可能已被覆盖。

## 3. 做法

### 3.1 名词

- **session** = 今天的工作区 `rois/<workspace_id>/`。它只对应一张切片（A6 W1 已经按 slide_id 判断），id 里带创建时间戳。名字不变，不重新命名。
- **数据版本**：一次「Step0 校正 + Step0 Intensity + Step1 融合通道 / 权重 / Intensity」的组合，以及由它算出来的校正像素和 fused 像素。
- **分割版本**：一次 Step2 运行，即今天的 `segmentation_runs/<run>/`。每个分割版本**属于且只属于**一个数据版本。

### 3.2 数据版本的编号（不新造哈希）

- 在 session 内按顺序编号：`v1`、`v2`、`v3`……；文件夹名带时间戳：`v003_20261002_231500`。
- 用户可以给版本起一个 label（例如「TopHat 40，CD3 0.5」）。没起时，label 就是编号。
- 两个版本是否「相同」，按今天已有的字段逐项比较：每个通道的校正签名（方法、参数、算法版本、后端）、Intensity 数值、融合配置，**以及 ROI 几何**（每个区域的名字 / id、`bbox_fullres`、polygon）和切片身份（`slide_id`）。**不新造哈希**，只读取、比较已有字段。

### 3.3 什么时候产生一个新的数据版本（第 1 题：(b)）

- **只有 Step1 Generate 时**才开新版本。开版本时，同时冻结 Step0 这一半（校正、Step0 的 Intensity）和 Step1 这一半（融合配置、Step1 的 Intensity），以及这次产出的 fused。
- Step0 Save 只更新「当前草稿」：它写的校正像素和参数，要等下一次 Generate 才被冻结进一个版本。
- 和当前最新版本完全相同（参数没变，Generate 复用已有 fused），就不开新版本。
- 场景：调 TopHat 半径 → Step0 Save → 改 CD3 权重 → Save Fusion Settings → Generate → 产生 v2；再直接 Generate 一次（什么都没改）→ 仍是 v2。

### 3.4 存放（v2：整份 corrected 共用、每版一组 fused；第 3 题：(b) 不再使用固定路径）

每个版本一个文件夹 `versions/<版本>/`，里面放这个版本的参数文件（`correction_config.json`、`step0_channel_remap.json`、`step1_fusion_settings.json`）和像素产品。像素产品是存储成本的大头：
- **校正像素（v2 修订）**：一个数据版本引用**一份完整的** corrected Zarr（今天的 `CorrectedZarrSource` 和 Step1 / Step4 的读取都以一个 `corrected_zarr_path` 为单位）。只有当**整份**产品完全相同时，两个版本才共用同一份：全部通道的校正签名、ROI 几何、source identity 都一致。只要有一个通道或者 ROI 几何不同，就生成新的 corrected Zarr。逐通道去重是以后的优化，**不属于本块**。
- **fused 像素（v2 修订）**：每个版本**一组** fused 产品，每个区域一个 fused Zarr（`FullFusionWorker` 本来就是每个 ROI 一份；Full WSI 时这一组只有一个）。

今天的固定路径（`step0/corrected_channels.zarr`、`step1/fused_….zarr`）**不再使用**。所有读取都通过版本记录找到当前版本的产品路径（第 3 题 (b)）。读这些路径的地方（Step1、Step2、Step3、Step4、provenance）在实施第一天先用 grep 全部列出，逐个改，并在执行记录里写明。

### 3.5 分割版本记录它的数据版本

- Step2 开始时，把「当前数据版本」写进运行记录（`segmentation_meta.json` 加 `data_version` 字段）。每个区域的输入 fused，取自这个版本 `regions[]` 里对应的 `fused_zarr_path`（v2）。dirty draft 状态下不允许开始（§3.13）。
- 之后再出新的数据版本，这个分割的记录不变，它的输入也不会被覆盖。

### 3.6 加载回 Step0 / Step1 浏览（按用户审阅修改）

- **不加单独的「数据版本 ▾」下拉**。点 `▶ Load` 后弹出的「Open a workspace」选择框（A6 W1）改为**每一行是「工作区 + 一个数据版本」**：
  - 例如 `Full WSI · 2026-09-27 12:14 · Step0 + Step1, Step2 (full_wsi_…_6bad)`，行尾右对齐显示 `v2`；同一个工作区的 v1 是另一行；
  - 行的样式和 Step3 下拉框的记录**一致**：名字在左，版本号右对齐在行尾，可以盖住名字右端（复用同一个 delegate）；
  - 没有版本记录的旧工作区，行尾显示 `unknown`；
  - 最后一行仍是「Start a new workspace (open none)」。
- 选一行：Step0 恢复它的校正方法、参数和 Intensity（复用 A6 W1 的恢复代码），读它自己的校正像素；Step1 恢复它的融合设置和 Intensity，读它自己的 fused。
- 浏览旧版本时点 Save（第 4 题 (a)；v2.1 改正）：在这个旧版本的基础上，建立或更新一个 **dirty draft**，旧版本不变。下一次 Generate 成功之后，才产生新的正式版本（§3.3、§3.13）。

### 3.7 Step3 下拉框的版本标记

- 每条分割记录的最右侧，右对齐显示它的**数据版本号**（如 `v2`），可以盖住名字右端；复用 B9 的 delegate。
- 非常早期、没有版本记录的数据显示 **`unknown`**。B9 现有的「provenance unknown」字样改为「unknown」（第 5 题）。
- 和 §3.6 的工作区选择框用**同一种行样式**。

### 3.8 旧的工作区（第 6 题：(b)）

已有的工作区（今天之前做的）没有版本记录。第一次打开时，把它现有的状态登记为 v1；它已有的分割不挂到任何版本，在 Step3 下拉框里显示 `unknown`。

### 3.9 打开工作区时 Step1 自动恢复（第 8 题：(a)）

打开工作区（或者在选择框里选中某个版本）时，Step1 自动恢复这个版本的融合设置和 Intensity，不再需要点「Load Previous Step1 Session」。

### 3.10 磁盘占用（第 7 题）

本块不做删除。选择框的每一行显示这个版本独占的磁盘大小（共用的 corrected Zarr 不重复计算）。

### 3.11 `version.json` 记录什么（v2 修订）

每个版本冻结自己生成时的全部空间语义，不依赖工作区当前的 `roi_manifest.json`：
- `version`、`label`、`created_at`；
- `slide_id` 和切片路径（`raw_ome_path`）；
- `corrected_zarr_path`（这个版本引用的那份完整 corrected Zarr）；
- 参数文件：`correction_config.json`、`step0_channel_remap.json`、`step1_fusion_settings.json` 的副本；
- `regions[]`：每个区域一条，包含 `roi_name`、`roi_id`、`bbox_fullres`、`polygon_fullres`（没有就写 null）、`fused_zarr_path`。Full WSI 时只有一条。

工作区以后即使通过 `rewrite_geometry()`（A6 W2 的「Overwrite」）改了 ROI，也**不会**反过来改变旧版本：旧版本按它自己的 `regions[]` 解释。加载旧版本时，Step0 / Step1 按这个版本自己的 ROI 几何恢复浏览。Step2 从当前数据版本的 `regions[]` 取每个区域的输入 fused。

### 3.12 Generate 的事务边界（v2 修订）

一个新的数据版本，只有在 Step1 Generate **全部成功完成**之后才正式存在。流程：
1. 冻结本次的 Step0 + Step1 配置和 ROI 几何；
2. 在版本自己的文件夹里生成产品（见下面的说明）；
3. 所有区域的 fused 和 meta 都成功完成；
4. 最后写 `version.json`，`complete: true` 最后写；
5. 最后才更新 `versions/index.json` 和「当前版本」（原子写入）；
6. **此时才登记**这个正式版本的 corrected 和 fused 的 A3 来源记录，以这个版本冻结的 `regions[]` 为准（不依赖工作区之后可能改变的 ROI 几何）；
7. 然后 Step2 才可以使用这个版本。

**为什么 A3 登记要推迟（用户裁定，v2.1）**：今天 `FullFusionWorker` 每发布一个区域的 fused，就立即 `_register_fused()`；Step0 Save 完成交接后也立即 `register_corrected_channels()`。A3 的记录写下就不再改。场景：v3 有 4 个 ROI，ROI1、ROI2 成功并已登记，ROI3 报错。按规则 v3 不存在，下次打开时它的文件夹被清掉，但 A3 里已经留下了指向被删文件的记录。推迟到第 6 步之后，在第 1–5 步任何地方出错，都是「没有版本、没有 index、没有 A3」，整个文件夹直接清掉即可。dirty draft 里的 corrected 和 fused **一律不登记** A3。

Cancel、报错或程序中断时：
- 不产生正式版本；
- 不修改「当前版本」；
- 不修改任何已有版本；
- 留下的未完成文件夹（不在 `index.json` 里，或者 `version.json` 没有 `complete: true`），在下次打开工作区时清理。

说明：审核意见建议的是先在 `versions/.pending_<id>/` 里生成，再改名成正式名字。但 fused 发布后会立即登记 A3 的来源记录，记录里写的是**当时的路径**；如果之后再改名，来源记录就指向一个不存在的路径。所以改为**直接在正式文件夹名下生成**，用「`complete` 最后写 + `index.json` 最后更新」作为发布点。这正是 §6 协议 B 已经验证过的「标记最后写」做法，效果相同：没有完成，就不存在版本。

### 3.13 dirty draft（v2 新增）

- Step0 Save 或 Save Fusion Settings 之后，如果参数（含 ROI 几何）和当前已提交的数据版本不同，而又还没有 Generate，状态就是 **dirty draft**。
- dirty draft **不是**数据版本。
- dirty draft 状态下，Step2 **不允许开始新的运行**，因为那会把新参数和旧 fused 混在一起。Step2 页面显示一行原因，Run 按钮不可用。
- 只有在 Generate 成功产生正式版本之后，或者用户重新加载某个已有版本之后，Step2 才能继续。
- 例子：在 v2 上把 CD3 权重从 0.5 改成 0.8 并 Save Fusion Settings，没有 Generate，就去 Step2 点 Run → 不允许，提示「先 Generate，或者重新加载 v2」。

### 3.14 已有版本只读（v2 新增）

已发布的版本文件夹（参数文件、corrected Zarr、fused Zarr、`version.json`）此后**不再被任何 Save 或 Generate 写入**。Step0 增量 Save 不能在已发布版本的 corrected Zarr 上原地修改，见 §3.15。

### 3.15 corrected 的写时复制（v2.1，用户裁定 §13 第 1 条 (a)）

一句话：**已发布的版本是只读母版；只有真的要改 corrected 像素时，才复印一份出来继续改。**

| 这次 Step0 / Step1 改了什么 | corrected 怎么处理 |
|---|---|
| 只改了 Step1 的权重 / Intensity，或 Step0 的 Intensity；corrected 像素本身没变 | **不复制**，继续引用当前版本的 corrected |
| ROI 和 source 完全相同，只改了部分通道的背景校正（例如 CD8 的 TopHat 半径 25 → 40） | 第一次需要改时，把已发布的 corrected 整份**复制成新的 draft corrected**，再用现有的增量算法只重算有变化的通道 |
| ROI 几何或 source identity 改了 | **不复制**旧的；直接建立新的 draft corrected，重新计算需要校正的全部通道（旧 Zarr 的空间结构已经不对应新的 ROI） |

一旦 draft corrected 已经存在，之后的 Save 都只改这份 draft，**绝不再碰**任何已发布版本的产品。下一次 Generate 成功时，draft corrected 随新版本一起发布；如果它和某个已有版本的 corrected 整份完全相同，就改为引用那一份，草稿丢弃（§3.4）。

## 4. 封闭白名单

**原则**：只改「产品路径从哪里来」的接线，不改校正、融合、分割的计算。今天读取校正 / fused 产品的地方大约有 25 个文件，其中大多数是从 Step0 交接（manifest 的 `corrected_zarr_path`）或分割运行记录里**拿到路径再读**，自己不拼固定路径。只要交接和运行记录写的是版本自己的路径，这些读取方会自动跟着走，**不改**。实施第一天用 grep 把每一处读取逐条核对，结论写进执行记录：哪些不改、为什么；哪些自己拼了固定路径、要改。要改的如果超出下面的清单，先停下，报给用户。

- **新模块 `utils/data_versions.py`**：版本的建立、列出、读取、切换「当前版本」，以及每个版本的磁盘占用。记录写在 `<工作区>/versions/index.json`（原子写入）和每个版本自己的 `version.json`。按块规则 6，新逻辑放在两个大文件之外。
- **`utils/workspace_session.py`**：列出每个工作区的数据版本，供选择框使用。
- **`ui/step0/step0_page.py`**：
  - 选择框改为「工作区 + 版本」逐条列出，行尾右对齐显示版本号，与 Step3 同样式；
  - 选中某个版本后，恢复它的 Step0 内容（复用 W1 的恢复）；
  - Save 把校正像素写到「当前草稿」的位置。
- **`ui/step0/search_ctrl.py`**：支持 draft corrected 的输出路径，以及写时复制之后的增量更新（§3.15）；不改背景校正算法。
- **`core/step0_handoff.py`**：交接里的 `corrected_zarr_path` 写成当前版本（或草稿）的路径（只改这一个字段的来源）；Step0 Save 时**不再立即**登记 corrected 的 A3 来源记录，改为在版本正式提交时登记（§3.12，只改调用时机）。
- **`ui/main_window.py`**：
  - Generate 时建立 / 复用版本；
  - fused 的输出路径指向版本；
  - Step2 交接带上当前版本；
  - 打开工作区或选中某个版本时，Step1 自动恢复这个版本的融合设置和 Intensity（第 8 题）；
  - Step3 下拉框标记的接线。
- **`ui/step0/overview_panel.py`**：`FullFusionWorker` 的输出路径（只改路径）；每个区域发布后**不再立即**调用 `_register_fused()`，改为在版本正式提交时统一登记（§3.12，只改调用时机）。
- **`ui/step2_page.py`**：输入 fused 的路径来自当前版本（只改路径来源）。
- **`workers/segment_merge_worker.py`**：`segmentation_meta.json` 记录 `data_version`。
- **`ui/step3_mask_bar.py`**：
  - 版本标记；
  - B9 的 delegate 移到两处都能用的地方，供工作区选择框共用；
  - `PROVENANCE_UNKNOWN` 的字样改为 `unknown`。
- **`core/provenance.py`**：fused、校正通道、分割运行的来源记录加上 `data_version` 字段（只加字段）。
- **测试**：新增 `tests/test_v16_data_versions.py`；已有测试凡是写死了固定路径的，只改读取点，并逐条记入执行记录。
- **文档**：`UI_SURFACE_RULES.md`（选择框行样式、Step3 标记）、两份用户指南、本申请的执行记录、v2.4 §12「用户插入块」表新增一行。

## 5. 不做的

不改校正、融合、分割的算法；不做跨 session 的版本比较；不删除旧版本（清理留到以后，见第 7 题）；不改 viewer 的读取、调度和缓存（规则 5）；不新造哈希。

## 6. 预注册验收门

- **自动**：
  - 两次参数不同的 Save + Generate 产生 v1、v2；v1 的参数文件和像素逐字节不变；
  - 分割 A（v1）、分割 B（v2）各自的输入路径指向自己版本的 fused，文件都在；
  - 加载 v1 回 Step0 / Step1：参数和 Intensity 与 v1 一致，读的是 v1 的像素；
  - 内容相同的 Save（No changes）不产生新版本；
  - 两个版本的整份 corrected 产品完全相同时，共用同一份 corrected Zarr；有任何一个通道不同时，生成新的一份；
  - **参数相同、ROI 几何不同**：一定产生不同的数据版本，corrected 不得错误复用；
  - **Cancel 不产生版本；报错不产生版本**；v1 已经存在、v2 生成失败时，「当前版本」仍是 v1，v1 逐字节不变；留下的未完成文件夹在下次打开时被清理；
  - **多 ROI**：一个版本的 `regions[]` 正确记录每个区域的 fused Zarr；Step2 按 `regions[]` 取每个区域的输入；
  - **加载旧版本时用的是这个版本自己的 ROI 几何**：工作区在之后用 Overwrite 改了 ROI，旧版本的 `regions[]` 和加载结果都不变；
  - **dirty draft 状态下不能开始新的 Step2**（Run 不可用，并说明原因）；Generate 成功或重新加载某个已有版本之后才可以；
  - **所有已有版本只读**：之后的 Save、增量 Save、Generate，都不改动已发布版本文件夹里的任何文件（逐字节比较）；
  - **写时复制**：只改 Intensity / 权重时不复制 corrected；只改部分通道的校正时，复制一次，然后只重算变化的通道；改了 ROI 几何时，新建 draft 并全部重算；
  - **A3 登记推迟**：多 ROI 的 Generate 在中途某个区域报错时，A3 里没有这次的任何 corrected / fused 记录；版本正式提交之后，A3 记录齐全，并且和 `regions[]` 一致；dirty draft 不登记 A3；
  - Step3 下拉框：分割 A 行尾显示 v1，分割 B 显示 v2；没有版本记录的旧分割显示 `unknown`；
  - Load 的选择框：每个「工作区 + 版本」一行，行尾右对齐显示版本号，和 Step3 用同一个 delegate；旧工作区登记为 v1；
  - 选中 v1 后，Step1 自动恢复 v1 的融合设置和 Intensity，不用点 Load Previous Step1 Session；
  - 只改了草稿、没有 Generate：不产生新版本；Generate 后什么都不改再 Generate：仍是同一个版本；
  - 在 v1 上 Save + Generate：产生新版本 v3，v1 逐字节不变；
  - 固定路径 `step0/corrected_channels.zarr`、`step1/fused_<区域>.zarr` 不再被写入；
  - 每条测试先在改动前的代码上变红。
- **针对性回归**：校正、交接、fusion、Step2 交接、Step3 下拉框相关的模块。全量回归到里程碑再跑（用户 2026-10-02 裁定）。
- **真机**：在 test1 的验收副本上，重复第 1 节的场景：两套参数 → 两个版本 → 两次分割；然后加载 v1 浏览、在 Step3 看两条记录的标记。

## 7. 风险

| 风险 | 对策 |
|---|---|
| 磁盘占用：每个版本一组 fused，加上整份不同时的一份 corrected（真实切片可能几 GB） | 整份完全相同时才共用 corrected；本块不做删除（第 7 题），选择框显示每个版本的占用 |
| 现有读取固定路径的代码很多（Step1、Step2、Step3、Step4、provenance） | 实施前先用 grep 列出所有读取固定路径的地方。通过 manifest 或版本记录间接读取的，保持不动；自己写死了固定路径的，必须改。发现需要改的文件超出已批准的白名单时，停下来报告 |
| 和 A7 / A8 的关系 | A8（项目状态的唯一持有者）会接管「当前打开的是哪个 session、哪个数据版本」；本块的 `utils/data_versions.py` 就是那份状态，A8 只是把它提升为 ProjectState，不另造一份 |
| 打开工作区时，Step1 不会自动恢复上次的设置（今天的现状：要点 Load Previous Step1 Session） | 第 8 题 |

## 8. 估计

约 4–6 个工作日：版本记录和存放 1.5 天；Step0 / Step1 接线和加载旧版本 1.5–2 天；Step2 / Step3 / provenance 0.5–1 天；测试和反向注入 1 天；加上真机。比较大，建议排在 A7 之前还是之后，见第 9 题。

## 9. 与计划的关系

- 这是用户插入的新块，记进 v2.4 §12 的「用户插入块」表。
- 它和 A6 的 W 系列同属「工作区」的规则。W4 剩下的部分（「made with older settings」标记）可能被它取代：分割记录上直接显示数据版本，比「旧设置」更具体（第 5 题）。

## 10. v0 的原题（已裁定，见 §11；留作记录）

1. **什么时候产生新版本**：
   - (a) Step0 Save 和 Step1 Generate 各自在内容变化时开新版本（一次「Step0 改了 + Step1 改了 + Generate」会产生 2 个版本）；
   - (b) 只有 Generate 时才开新版本，同时冻结 Step0 + Step1 两半（Step0 Save 只更新「当前草稿版本」）；
   - (c) 由用户手动「Save as new data version」。
   - **建议 (b)**：一个数据版本 = 一次真正产出 fused 的参数组合，正好对应「调一次参数做一次分割」。Step0 Save 只是为下一个版本做准备。
2. **像素怎么存**：
   - (a) 每个版本完整复制一份校正像素和 fused；
   - (b) 校正像素按「通道 + 校正签名」共用；fused 每个版本一份。
   - **建议 (b)**。
3. **今天的固定路径**：
   - (a) 保留为「当前版本」的位置，切换版本时把那个版本的产品放到这里（复制或链接）；
   - (b) 所有读取改为通过版本记录找路径，固定路径不再使用。
   - **建议 (b)**，改动大但干净；如果想控制工作量，可以先 (a)，以后再 (b)。请你定。
4. **浏览旧版本时点 Save**：
   - (a) 在旧版本的基础上产生一个新版本（旧版本永远不变）；
   - (b) 不允许，先切回当前版本。
   - **建议 (a)**。
5. **Step3 下拉框里，版本标记和「provenance unknown」同时出现**：
   - (a) 只显示版本；没有版本记录的旧运行显示「provenance unknown」；
   - (b) 两个都显示。
   - **建议 (a)**。W4 剩下的「made with older settings」不再单独做，由版本标记取代。
6. **已有的旧工作区**：
   - (a) 第一次打开时，把它现有的状态登记为 v1；它已有的分割都标为「属于 v1（推定）」；
   - (b) 登记为 v1，但旧分割不挂到任何版本，仍显示「provenance unknown」。
   - **建议 (b)**：旧分割的输入可能已被覆盖，挂到 v1 就是猜测。
7. **旧版本的清理**：本块不做删除；在版本下拉里显示每个版本占用的磁盘大小。以后再加「删除这个版本（会同时删掉它的分割吗？）」。**建议同意**。
8. **打开工作区时，Step1 是否自动恢复当前版本的融合设置**（今天要手动点 Load Previous Step1 Session）：
   - (a) 自动恢复；
   - (b) 维持现状。
   - **建议 (a)**：版本化之后，「打开 session = 回到当前版本」更自然。
9. **排期**：排在 A7 之前，还是之后？A7 依赖 W1–W3 的真机验收；本块也要改 `step0_page.py` 和 `main_window.py`，两块不宜同时进行。**建议先做本块**，再做 A7：本块改的是工作区和数据，A7 改的是相机，先把数据结构定下来，A7 的回退也更干净。

## 11. 用户裁定（2026-10-02 夜，对 v0 的审阅）

- **§3.6**：不加单独的版本下拉。Load 后的工作区选择框，每一行是「工作区 + 数据版本」，行尾右对齐显示版本号，样式和 Step3 下拉框的每条记录一致。
- **§3.7**：正常记录行尾显示数据版本号；非常早期的数据显示 `unknown`。
- 第 1 题：**(b)**，只有 Generate 时才开新版本。
- 第 2 题：用户先确认了 (b)（逐通道共用）；**v2 按独立审核意见改为整份共用**（§12 第 1 条）：只有整份 corrected 产品完全相同才共用，逐通道去重以后再做。fused 每个版本一组，不共用。
- 第 3 题：**(b)**，所有读取改为通过版本记录找路径，固定路径不再使用。
- 第 4 题：**(a)**，在旧版本上 Save，会产生一个新版本，旧版本不变。
- 第 5 题：**(a)**，只显示版本号；没有版本记录的显示 `unknown`（不再是「provenance unknown」）。
- 第 6 题：**(b)**，旧工作区第一次打开时登记为 v1，但它已有的分割不挂到任何版本，显示 `unknown`。用户说明：现在是开发阶段，不用担心旧数据。
- 第 7、8、9 题：**同意**。不做删除，显示每个版本的磁盘占用；打开工作区时，Step1 自动恢复当前版本的融合设置；先做本块，再做 A7。

## 12. v2 修订：独立审核意见（2026-10-03，由用户转来）→ 修改后的规则

| # | 原问题 | 修改后的规则 | 位置 |
|---|---|---|---|
| 1 | 方案写的是不同版本之间按单个通道共用 corrected 像素，但 `CorrectedZarrSource` 和 Step1 / Step4 的读取都以一个 `corrected_zarr_path` 为单位 | 一个版本引用一份完整的 corrected Zarr；只有整份完全相同（全部通道签名、ROI 几何、source identity）才共用；任何不同都生成新的一份。逐通道去重是以后的优化，不属于本块 | §3.4 |
| 2 | 版本没有冻结 ROI 几何，只依赖工作区当前的 `roi_manifest.json` | `version.json` 记录 `slide_id` 和 `regions[]`（`roi_name`、`roi_id`、`bbox_fullres`、`polygon_fullres`）。比较两个版本是否相同时，ROI 几何也参与比较。加载旧版本时按它自己的几何恢复。工作区之后 `rewrite_geometry()` 不影响旧版本 | §3.2、§3.11 |
| 3 | Generate 没有事务边界 | 产品全部成功之后才写 `version.json`（`complete` 最后写），最后再更新 `index.json` 和「当前版本」。Cancel、报错、中断都不产生版本，不改「当前版本」，也不改已有版本；未完成的文件夹在下次打开时清理。审核建议的 `.pending_<id>` 加改名，改为「直接写在正式文件夹名下，用标记最后写来发布」，原因见 §3.12 | §3.12 |
| 4 | 「每个版本一份 fused」与多 ROI 不符 | 每个版本一组 fused：`regions[]` 里每个区域一条 `fused_zarr_path`，Full WSI 只有一条。Step2 从当前版本的 `regions[]` 取输入。不实现 TMA | §3.4、§3.11 |
| 5 | 风险表里还留着「保留固定路径作为当前版本」的旧方案 | 删除。改为：先 grep 所有读取固定路径的地方；通过 manifest 或版本记录间接读取的不动；写死固定路径的必须改；超出白名单就停下报告 | §7 |
| 6 | 没有「改了参数但还没 Generate」的状态 | dirty draft：它不是版本；dirty draft 下 Step2 不能开始新运行；Generate 成功或重新加载某个已有版本之后才可以 | §3.13 |
| 7 | 没有写明已有版本只读 | 已发布的版本文件夹此后不再被任何 Save 或 Generate 写入 | §3.14 |

白名单（§4）不变；验收门（§6）按上表补齐。

## 13. 修订时发现的阻断级问题（需要用户裁定）

1. **Step0 增量 Save 和「已有版本只读」冲突。**
   - 今天的增量 Save，是在**同一份** corrected Zarr 上原地改写有变化的通道（`mode="a"`）。版本发布以后，这份 Zarr 已经属于某个版本。下一次 Step0 Save 如果还在它上面原地改，就违反了 §3.14 的只读规则；如果每次都从头算所有通道，又失去了增量 Save。
   - 场景：v2 已经发布，你只把 CD8 的 TopHat 半径从 25 改成 40，然后 Step0 Save。
   - 最小规则（推荐）：**写时复制**。版本发布之后的第一次 Step0 Save，先把当前版本的 corrected Zarr 整份复制成新的草稿，再在草稿上只重算 CD8。之后同一个草稿上的 Save 照旧增量。下一次 Generate 时，这份草稿随新版本一起发布。
   - 代价：每产生一个新版本，就多一份完整的 corrected Zarr（`…_6bad` 里约 221 MB）。这和 §12 第 1 条「整份共用、不逐通道去重」是一致的。
   - 另一种做法：草稿每次都从头重算所有被校正的通道。不复制，但每次 Save 都慢。
   - 请选：(a) 写时复制（推荐）；(b) 每次重算全部。

2. **Generate 的发布方式和审核建议不同**（§3.12 的说明）。审核建议 `.pending_<id>` 再改名；但 fused 发布时立即登记的 A3 来源记录里写的是当时的路径，改名之后来源记录就指向不存在的路径。所以改为「直接写在正式文件夹名下 + 标记最后写」。效果相同，但和审核原文不一样，请确认。

## 14. 用户对 §13 的裁定（2026-10-03）

1. **(a) 写时复制**，规则按 §3.15 的表：只改 Intensity / 权重时不复制；ROI 和 source 相同、只改部分通道的校正时，复制一次后增量；ROI 几何或 source 改了，就新建并全部重算；draft 存在后，只改 draft。
2. **有条件同意**：不需要 `.pending` 加改名，采用「最终目录写入 → `complete` 最后发布 → index / 当前版本最后切换」。附加条件：**未提交的 draft 不登记永久的 A3；corrected 和 fused 的 A3 登记，全部推迟到版本正式提交之后**，以冻结的 `regions[]` 为准（§3.12 第 6 步）。
3. 顺带改正两处文字残留：§3.6（浏览旧版本时 Save 只更新 dirty draft）、§4 `search_ctrl.py` 那一行（不再写逐通道存放）。

## 15. 批准实施与白名单扩展（2026-10-03，用户「授权批准执行」）

- 用户批准按 v2.1 实施；完成后跑回归（每段只跑相关模块，全部完成后跑一次全量）。
- 实施前的只读核对（路径盘点）发现，白名单之外还有 2 个文件必须改，已获用户批准加入，**只限于改「产品路径从哪里来」**：
  - **`core/quant_sources.py`**：Step4 定量一次分割运行时，corrected 产品改为从这次运行自己的 `segmentation_meta.json`（`paths.corrected_channels_zarr`）读；没有这个字段时，才退回读工作区当前的 Step0 交接。场景：分割 A 属于 v1，当前版本是 v2，定量 A 时必须读 v1 的 corrected。
  - **`core/tile_tissue.py`**：Step2「跳过空 tile」不再用「fused 路径往上两层」去猜工作区，改为向上查找 `roi_manifest.json`（与 `provenance.find_workspace` 同样的做法），并读取对应版本的设置文件。
- 不改、只记录：
  - `viewer/step1_source.py`：coarse sidecar 本来就和 corrected 产品放在同一个文件夹，二者一起搬进版本文件夹后，读写两端自动一致，用测试确认；
  - `utils/segmentation_registry.py` 的 `register_legacy_result`：遗留代码，写死了路径，实际几乎不会命中。
- 白名单内的高风险点：`step0_page._handoff_spec` 用 corrected 产品路径反推「step0 文件夹」。改为从工作区上下文取，否则参数文件和交接会被写进版本文件夹。

## 16. 执行记录（2026-10-03）

分 6 段实施，每段都做了反向注入（新测试放进上一段的代码树里确认变红，在新代码上变绿）和只跑相关模块的针对性回归。

| 段 | 提交 | 内容 |
|---|---|---|
| 1 | `44ef5ef` | `utils/data_versions.py`：版本只有「`version.json` 写了 `complete: true` 且在 `index.json` 里」才存在；编号 = 计数 + 时间戳（无哈希）；按已有字段（含 ROI 几何）比较两个版本是否相同；整份 corrected 共用规则；清理未完成的文件夹；已发布产品只读的识别 |
| 2 | `be76f12`、`498cb14` | Step0 Save 的写时复制（§3.15）：draft 是 `versions/corrected/cNNN_<时间>/` 里自己的一份 corrected，版本原地引用它，不移动、不重发交接；交接和参数文件留在工作区的 `step0/`；draft 不登记 A3 |
| 3 | `b1f2b1a` | Generate 是一个事务（§3.12）：fused 写进新版本自己的文件夹，所有区域都成功后才写 `version.json`（`complete` 最后）、最后更新 index 和当前版本，然后才登记 A3；取消、报错、某区域缺产品都不产生版本；什么都没改就回到原版本；打开时清理未完成的文件夹 |
| 4 | `c3caca8` | Step2 只在当前版本上运行，每个区域的 fused 取自 `regions[]`，`segmentation_meta.json` 记 `data_version`；dirty draft 时拒绝新运行并说明原因；Step4 定量一次运行时读这次运行自己版本的 corrected 和配置；「跳过空 tile」向上查找 `roi_manifest.json` |
| 5 | （本段） | Load 的选择框改为「工作区 + 版本」逐行列出，版本号右对齐在行尾，和 Step3 用同一个 delegate（`step3_mask_bar.TaggedItemDelegate`），当前版本预选，只有一行时直接打开；加载非当前版本：按该版本的 `regions[]`（只有 bbox 的区域按矩形画）、`correction_config.json`、`step0_channel_remap.json` 和 corrected 恢复 Step0，重发 Step0 交接并设为当前版本；主窗口把该版本的 `step1_fusion_settings.json` 和 `step1_session.json` 装回工作区的 `step1/`，重新绑定到刚重发的交接（内容不变）；打开工作区后第一次进入 Step1 自动恢复一次（第 8 题）；Generate 提交时把 Step1 会话一起存进版本；Step3 下拉框每行显示这次运行的 `data_version`，没有的显示 `unknown`（第 5 题 (a)） |
| 6 | （本段） | 数据版本之前做的工作区，第一次打开时把现状登记为 `v1`（`label` = `v1 (registered from an earlier workspace)`，`legacy: true`），产品原地引用（此后只读），每个区域都必须有 fused，否则不登记；已有的分割不挂到任何版本，显示 `unknown`（第 6 题 (b)） |

测试：`tests/test_v16_data_versions.py` 共 41 条（第 5、6 段新增 9 条）；`tests/test_v16_a6_workspace.py` 中「多个工作区时询问」一条改为新的行格式。第 5、6 段的 10 条在 `c3caca8` 上全部失败、在新代码上全部通过。

改动的文件全部在白名单内（§4，加 §15 的两个文件）。`MainWindow._step3_runs_with_provenance` 不再用于 Step3 下拉框的标记（第 5 题 (a)：只看版本），函数和它的测试保留，未删除。

第 5、6 段的针对性回归（20 个调用了被改函数的模块 + DV、A6 工作区、A6 异步三个模块）：全部通过。`test_step0_step1_display_isolation::test_step0_work_does_not_make_step1_load_or_redraw` 在后台批量运行中失败过一次，单独在新、旧代码上各重跑两次都通过，判为偶发（与本段无关的定时器时序）。

下一步：codex（gpt-6-astra，low）代码审核 → 全量回归（新 = DV 最终提交，旧 = `905079f`）→ 真机验收。

### 16.1 codex 审核（gpt-6-astra，low，2026-10-03）与修正

codex 提了 6 条，逐条对照代码核实，6 条都是真问题。原文见 `~/fusionflux/bench_a6/dv_review/codex_low.txt`。

| # | 问题 | 修正 |
|---|---|---|
| 1 | Step0 交接写入会把属性写进已发布的 corrected 产品（只改 Intensity 的 Save、加载旧版本时都会写） | 交接写入跳过已发布产品（`corrected_read_only`）；Save 改了区域、只改外形不改外框时，先复制成 draft 再写进 draft；外框也改了时，新建一份空的 draft |
| 2 | 通道从校正改回原图后，旧数组还留在产品里，版本被误判为「没变化」，复用了旧的 fused | 版本内容比较加上各通道的校正决定（`channel_decisions`，已有字段） |
| 3 | 有 dirty draft 时选当前版本，打开的是 draft | 用户裁定 (a)：draft 单独一行，行尾标 `draft`，排在最前并默认选中；选任何版本（包括当前版本）都加载那个版本，dirty draft 结束；只改了 Step1 融合设置也算 draft |
| 4 | 存进版本的 Step1 会话指向上一次的 fused | 提交时把会话副本里的 fused / corrected 路径改成本版本自己的 |
| 5 | Cancel 点在最后一次停止检查之后，任务跑完仍发布了版本 | 完成时已点过 Cancel：丢弃本次的版本文件夹，当前版本不变 |
| 6 | Step4 定量旧运行时读当前版本的 corrected | 先按运行记录的 corrected 路径找引用它的版本，找不到时才用当前交接 |

新增测试 12 条（`test_v16_data_versions.py` 共 53 条）。第 1、2、4、5、6 条的 8 条测试在 `8bc8263` 上全部失败；draft 行的 4 条和依赖新 Step0 代码的 3 条，在旧 Step0 代码上失败；新代码上全部通过。测试模块加了自动拦截：没有被回答的模态对话框直接让测试失败，不再卡住。

针对性回归（35 个引用了被改函数的模块）：全部通过；`test_step0_channel_conditioning` 超时，这是新旧代码上都有的已知情况；`test_v16_data_versions` 第一次超时，原因是测试数据造出了 draft 行、弹出了没人回答的选择框，修正测试数据后通过。

### 16.2 真机验收第一部分的发现与修正（2026-10-03）

1. **验收数据准备错误（我的失误）**：验收副本是用 `cp -a` 从 `bench_a0/test1_copy` 拷来的，里面的交接文件、fusion_meta 记的是指向 test1_copy 的绝对路径，程序因此在 14:18–14:24 把旧工作区的 v1 登记、Step0 Save 重算的 3 个通道（CD3D、HsBAg、CD163）、Step1 会话写进了 test1_copy。已按用户同意，用 A0 的拷贝工具（`diagnose_v16_a0_camera copy-project`，会改写路径）从 test1 原件重新生成了 test1_copy 和验收副本，原件核对未变。
2. **程序缺陷（DV 引入）**：Step0 Save 直接在交接文件指向的 corrected 上计算，即使它在当前工作区以外。修正：交接指向工作区以外时，改用工作区里对应位置的文件，没有就新建 draft（`Step0Page._dv_inside_workspace`）；打开工作区时读取的 corrected 也一样；主窗口判断「当前工作区」以交接文件实际所在的文件夹为准；旧工作区登记 v1 只用工作区自己的 `step0/`、`step1/`。
3. **Step2 没有用 Step1 的方法（用户裁定：修复，Step2 全部组件自动同步）**：Generate（以及旧工作区登记 v1）时，把 Step1 当时的分割方法和参数文件存进版本的 `segmentation_params/`；进入 Step2 时，fused 和分割参数都来自当前版本，参数来源显示 `From data version vNNN`，覆盖之前手动填的内容；同一个版本再次进入 Step2 时，保留用户之后的修改。
4. **Step2 Input Data 只显示 fused.zarr（用户裁定 (a)）**：「Index」那一行隐藏。

新增测试 6 条，在 `dc76891` 上全部失败、新代码上全部通过。

未解决：Step0 改参数按回车时出现的黑色 `main.py` 窗口。离屏模式下按回车前后没有任何新的 Qt 部件或原生窗口，复现不出来；用户说以前没有、每次回车都会出现、关掉不影响程序，怀疑与 WSL 有关。

**codex 第七轮审核**（`codex_review7.txt`）提了 3 条，都已处理：
1. Step1 的会话、融合设置和 Step2 的输出目录，仍然跟着交接文件里记的别的项目的文件夹 → 接受交接后，这些文件夹统一换成交接文件实际所在的工作区的（`MainWindow._dv_rebase_to_workspace`）。
2. 从别处拷来的、已有版本的工作区，版本记录里写的是原项目的路径，「已发布、只读」的判断认不出本工作区里的那份 → 地址换算统一放进 `data_versions.localize`，只读判断、删除计划、Step2 绑定都用它；删除只挪本工作区里的东西。
3. 只改分割参数、融合没变时，Generate 复用旧版本，Step2 就拿不到新参数 → 按用户原话「使用 step1 **最新**使用的方法」重新理解：Step2 的分割参数来自工作区 `step1/segmentation_params` 里**最新**的那份，不来自版本里冻结的副本（副本只作记录）；分割参数不参与「两个版本是否相同」的判断，所以只改分割参数不需要重新 Generate；Step1 的参数变了，再进 Step2 时会重新加载。
新增测试 2 条，修改 1 条，在 `be409c1` 上失败、新代码上通过。
