# Step1 预分割（Method & Parameters / Patch Results）重设计 — 项目计划

日期：2026-09-23（第二版，吸收独立审核意见）　分支 `v15-interactive-channel-workspace`，起点 `c9f80df`。
状态：**计划，未启动。** 提交本文档不代表批准任何生产实施。每块须用户单独启动；模块级改动（块 C）须另行批准。

修订记录：
- v1：初稿。
- v2：采纳独立审核的第 1、3、4、5、6 条，以及「先做契约准备块、A 拆成 A1/A2、选定的基本约束随 C 落地」的建议。第 2 条（同名方法合并）按用户裁定**维持 v1 方案**。另补上可核验的测试基线（附录）。

---

## 一、目标

把 Step1 的预分割从「一次只能搜一种方法的参数网格 + patch × 参数的格子结果」改为：

1. **上半部 Patches**
   - 已有 patch 以紧凑网格列出，默认全选，可以取消。
   - 没有 patch 时，提示去画，或按指定数量和长宽**随机生成**。
2. **下半部 Methods**
   - 初始为空，用 `+` 弹窗逐个添加「方法 + 参数列表」。
   - 每个方法成为一个块，可以查看、可以删除。
   - 多方法、多参数一次跑完。
3. **结果**
   - 参考 step5_v8 的 montage viewer：所有勾选的 patch 排进一张画布，用分隔线隔开，统一缩放和平移。
   - 所有参数组合的细胞 mask 和核 mask 叠加显示；每个组合各自有开关，另有全开/全关。
   - 线宽、线型、颜色可调。
   - 通道和 Fusion 的呈现由 Step1 的 Channels 栏控制。
4. **选定**：用户比较后选定**唯一一个方法 + 唯一一组参数**，Save 后进入全量分割。**Step2 实际执行的方法和参数必须就是这一组**（见块 E）。

## 二、用户裁定（2026-09-23，必须继承）

| # | 裁定 |
|---|---|
| R1 | 随机 patch：有 ROI 就在当前 ROI 内生成，没有就在组织内生成。**空白面积超过 40% 的候选直接丢弃并重新生成。** 生成结果要在 Tissue Navigator 可见，也就是成为 Step0 的正式 patch。 |
| R2 | HQ / HQ2 / CDS 只是**在新界面不可见**，后台代码和 Step2 兼容**全部保留**。 |
| R3 | 可以写成列表的参数：<br>• Cellpose：diameter、flow、cellprob<br>• StarDist：prob、nms、expand<br>• Mesmer：主要阈值<br>模型名这类参数只允许单值。**取消原来的两阶段流程**（Phase1 定直径 → Phase2 扫 flow × cellprob），改为每个方法对各参数列表取笛卡尔积。 |
| R4 | 任务数（勾选 patch 数 × 全部组合数）**超过 10 个时，开始前弹窗提示**；用户坚持就照常运行。**某个结果一出来就可以在 Step1 看**，不必等全部结束。 |
| R5 | 纯核方法只有核 mask，纯全细胞方法只有细胞 mask，expansion 类两种都有。**没有的那一种，开关置灰。** |
| R6 | 结果视图参考 step5_v8 的 montage viewer：所有 patch 都可以 zoom in/out，可以查看通道和分割情况。 |
| R7 | **只允许一个方法、一组唯一参数组合**进入最终分割。 |
| R8 | 执行层的模块级改动单独成块申请（块 C），批准后才动手。 |
| R9 | 同名方法：询问是否合并；合并时每个参数取两边取值的并集，单值参数冲突时让用户二选一；选「不合并」就取消这次添加，同一个方法只保留一个块。**不做展开后的过滤去重。**（审核第 2 条，用户裁定维持原方案。） |

同时继续遵守 `AGENTS.md`、`UI_SURFACE_RULES.md`、`docs/P0_SCOPE_RULES.md`。和本计划最相关的几条：
- 不擅自增加可见界面。
- 科研数据和 committed 边界要保护：任务只在 committed fusion snapshot 上运行，结果带 `fusion_settings_hash`，Save 时校验。
- 测试只写合成项目。
- 自动化测试通过不等于真机验收。

**设计选择与裁定分开标注。** 下文凡是标为「设计选择」的内容，都不是用户裁定，可以在 A0 或对应块开工时再讨论。包括：
- 随机种子固定；
- 新 patch 不重叠；
- 尝试次数上限；
- 结果身份字段；
- 默认线型和颜色。

## 三、现状（重设计要替换或绕开的部分）

以下行号均以 `c9f80df` 为准。

- **方法注册表**：`utils/segmentation_config.py:23-314`，共 11 个方法。按 R2，新界面只列其中 8 个：
  - Cellpose ×3：whole-cell、nuclei、nuclei + expansion
  - StarDist ×2：nuclei、nuclei + expansion
  - Mesmer ×3：whole-cell、nuclei、nuclear-guided
- **界面**：`ui/step0/search_ctrl.py` 的 `SearchCtrlPanel` 只有一个方法下拉框。真正的参数网格只有 Cellpose Phase 2，由 `_run_p2` 取笛卡尔积。
- **执行**：`_launch_worker` 起一个 `multiprocessing.Process`，按**第一个任务**的方法决定执行目标；结果通过 `mp.Queue` 回传，由 `ui/main_window.py:6532` 一带的轮询逻辑直接读取队列里的 `masks`。
  - 每个任务只回传一张 label mask。
  - HQ 系列和 Mesmer nuclear-guided 算出的核 mask 被丢掉。
  - **Cellpose / StarDist expansion 在 `workers/cellpose_worker.py:584` 和 `:715` 用 `expand_labels` 的结果直接覆盖了核 mask，扩张前的核标签没有保留。**
- **结果**：`ui/step0/result_grid.py` 的 `ResultGridPanel`，行 = patch、列 = 参数组合。区分列的 `_pkey` 只认 Cellpose 参数。
- **交接**：`_save` 调用 `save_segmentation_params` 写出参数文件和 `segmentation_params_index.json`，Step2 的 `load_step1_active_params` 再把它们填进 Step2 自己的控件。
  - **Step2 的运行入口（`ui/step2_page.py:2268`）调用的是 `get_seg_config()`，读的是 Step2 自己的控件**，不是 Step1 写出的文件本身。
- **ROI**：ROI 记录带 `polygon_fullres`（手绘多边形，level-0 坐标）；Step1 的 GPU 多边形裁切已经在用它。
- **组织掩膜**：初查没有找到现成可复用的组织掩膜，只找到背景校正内部用的 Otsu。**A0 要确认这一点。**
- **patch**：用 level-0 坐标 `(y0, y1, x0, x1)`，编号按位置排；数量一变，全部预览历史都会被清空。patch 由 Step0 的几何发布通道写出。

**必须修掉的现有缺陷**（新设计下一定会暴露）：
1. 每到达一个结果就会把它设为当前参数，于是用户还没选，Save 就解锁了。
2. 同一个 patch 的多个组合写进同一个 npz 文件，互相覆盖。
3. 结果列的 key 在非 Cellpose 方法之间会撞。
4. 预览结果和参数写到两个不同的目录。

## 四、设计

### 4.1 页面结构

Step1 左侧的 `Method & Parameters` 标签页改为上下两部分：

```
┌ Patches ──────────────────────────────────────────┐
│ [✓P1][✓P2][✓P3][✓P4][ P5][✓P6] …（紧凑网格，自动换行）│
│ [全选] [全不选]            [随机生成…]  已选 5/6      │
└───────────────────────────────────────────────────┘
┌ Methods ──────────────────────────────────────────┐
│ ┌Cellpose whole-cell ──────────── 6 组合 [查看][×]┐ │
│ └ diameter 30 · flow 0.2,0.4 · cellprob -1,0,0.5 ┘ │
│ ┌StarDist nuclei ─────────────── 2 组合 [查看][×]┐  │
│ [+]                                                │
│ [保存方案] [加载方案…]      总任务：5×8 = 40  [Run] [Stop] │
└───────────────────────────────────────────────────┘
```

- **Patch 块**：每个 patch 一个可勾选的小块，颜色沿用 `PATCH_COLORS`；鼠标悬停显示尺寸和坐标；默认全选。没有 patch 时显示提示，并给出随机生成入口（A2）。
- **方法块**：显示方法名、各参数的取值摘要和组合数。「查看」重新打开弹窗编辑，「×」删除。
- **`+` 弹窗**：
  - 方法下拉框只列 8 个方法；
  - 可列表参数用逗号分隔，其他参数只接受单值；
  - 默认值来自方法注册表，逐项校验，并实时显示本方法的组合数；
  - Save 保存并关闭，Cancel 放弃。
- **同名方法**：按 R9 处理。
- **总任务数**：实时显示；超过 10 个时，Run 之前先弹窗确认（R4）。
- **运行前置**：fusion 草稿未保存时拒绝运行，沿用 `_require_committed_fusion_settings`。
- **旧界面**：原来的 Phase1/Phase2 控件在块 E 退场，代码保留，清理另行申请。

### 4.2 随机生成 patch（R1，块 A2）

- **输入**：数量 N、宽 W、高 H（level-0 像素）。
- **ROI 约束**：
  - 有 ROI 时，ROI 的 **bbox 只用来抽候选位置**；
  - 每个候选 patch 必须**完整落在真实 ROI 多边形（`polygon_fullres`）之内**；凹进去的部分、多边形外的部分都不能生成 patch。
  - 没有多边形的 ROI 按矩形处理。
- **组织和空白**：「空白超过 40%」的计算方式在 A0 定稿。
  - **不能**把 DAPI 阈值分出的核像素直接当成组织面积，否则细胞之间的间隙会被算成空白。
  - A0 先核查有没有现成的组织掩膜可以复用；没有的话，再定一个在 overview 层级计算的组织掩膜，例如对平滑后的信号做阈值，再做形态学闭运算和填洞，把核间隙也算作组织。然后在合成图和真实切片上各验证一次。
- **设计选择（不是裁定）**：
  - 新 patch 之间、以及新 patch 和已有 patch 之间不重叠；
  - 随机种子默认固定，并写进生成记录；
  - 最多尝试 `N × 200` 次，凑不够就如实提示「只找到 k 个」，不放宽标准。
- **写入**：通过 Step0 已有的 patch 写入和发布通道，和手画 patch 走同一条路径，所以 Tissue Navigator、Step0、Step1 都能看到。不另建第二套 patch 模型。

### 4.3 方案（method plan）的保存和加载

- **文件**：与最终参数分开存放。
  - `<step1_dir>/segmentation_search_plans/plan_<YYYYmmdd_HHMMSS>.json`
  - `<step1_dir>/segmentation_search_plans/index.json`
- **内容**：`{version, created_at, source_identity, methods: [{method, params}], selected_patches: [bbox...]}`。
- **加载**：列出本项目的历史方案。已经不存在的 patch 自动忽略，并提示忽略了几个。
- 保存和加载都**不触发任何计算**。

### 4.4 结果记录、运行快照与运行中编辑规则（A0 定稿，C 实施）

**结果记录**：每个任务一条，字段如下。不新建复杂的身份框架，复用已有的来源身份和 committed snapshot。
- `source`：复用 `_handoff_identity()` 或 provider 的 `source_identity`，说明结果属于哪张切片、哪个 Step0 发布版本。
- `run_id`：一次 Run 一个。
- `patch_bbox`：level-0 坐标。在同一来源下，它就是 patch 的稳定标识；界面上的 P 编号只是显示用的。
- `method` 和规范化后的 `params`，由此得到组合 ID。
- `fusion_settings_hash`：取启动时 committed snapshot 的值。
- `status`，三者之一：
  - `ok`（其中 `cells = 0` 表示成功但零细胞）；
  - `failed`（附错误信息）；
  - 该方法本身没有某种 mask 时，那种 mask 记为 `not_produced`。
- 两张 mask 各自的文件路径和细胞数。

**运行快照规则**（正常操作下）：
- 点 Run 时，冻结以下内容作为这次运行的快照：
  - patch 列表（bbox）；
  - 方案，即方法和组合；
  - committed fusion snapshot。
- 正在运行的任务**始终使用启动时的快照**。运行中如果有人增删 patch、编辑方法块、重新保存 Fusion Settings：
  - 已经派发的任务照常跑完；
  - 新的设置只作用于下一次 Run。
- **迟到的结果**按 `patch_bbox` 和 `run_id` 归位，**不按当前的 P 编号**挂到别的 patch 上。
  - 来源或 fusion hash 和当前不一致的结果，标为过期，照常显示但不能被选为最终。
- **任何结果都不会自动成为最终选择。**

### 4.5 各方法的输出表（A0 定稿，C 实施）

| 方法 | 细胞 mask | 核 mask | C 需要做的 |
|---|---|---|---|
| Cellpose whole-cell | ✓ | —（置灰） | 无 |
| Cellpose nuclei | — | ✓ | 无 |
| Cellpose nuclei + expansion | ✓（扩张后） | ✓（**扩张前**） | 在 `expand_labels` 之前保留核标签 |
| StarDist nuclei | — | ✓ | 无 |
| StarDist nuclei + expansion | ✓（扩张后） | ✓（**扩张前**） | 同上 |
| Mesmer whole-cell | ✓ | A0 核查 Mesmer 是否同时给出核输出 | 视核查结果 |
| Mesmer nuclei | — | ✓ | 无 |
| Mesmer nuclear-guided | ✓ | ✓ | 回传目前被丢弃的 `nuclei_mask` |

每个格子都要能区分三种状态：「该方法没有这种输出」（开关置灰）、「成功但零细胞」、「运行失败」。

### 4.6 结果视图：montage（R5、R6，块 D）

借鉴 step5_v8 montage viewer 的核心做法，在 pyqtgraph 上实现：

- **一张画布、一个相机**：所有勾选的 patch 按行装箱排进同一个 `ViewBox`，patch 之间留固定间隙并画分隔线，统一缩放和平移。
  - 双击或按 F 适配全部。
  - 点击某个 patch 只选中它，视图不移动。
  - 保留一张布局表，记录每个 patch 在画布上的矩形。
  - 每个 patch 左上角有一个 P 编号标签，作为独立图层。
- **显示供给**：块 D 在施工前必须先在 A0 写明以下三点，否则执行时很容易以「复用合成路径」为名扩改 Viewer。
  - **复用哪个接口**：初步候选是 Step1 现有的 patch 合成路径（Overlay/Fusion，跟随模式按钮、Channels 勾选、Intensity 和 fusion 权重）。它具体从哪一层读像素、按缩放选哪一级金字塔、能不能不经过整张切片的 viewer 直接取 patch，都要在 A0 查实。
  - **缓存归谁**：patch 底图缓存归结果视图自己所有，有容量上限，不借用、不扩展 viewer/scheduler 的缓存。
  - **何时释放**：切换数据集、离开 Step1、patch 被取消勾选、新的一次 Run 开始时释放。
  - 如果必须改 viewer、scheduler 或缓存层，停下申请。
- **mask 图层**：每个（组合 × 细胞/核）是一个图层。
  - **先验证矢量方案**：用 cosmetic `QPen` 画轮廓，线宽按屏幕像素计，原生支持实线、虚线和颜色。
  - 路径的颗粒度（每个 patch 一条、每个图层一条，或者分块）**不预先锁定**，由实测决定。
  - **栅格方案不是自动等价的回退**：膨胀出来的线宽会随缩放变化，也不天然支持虚线。
  - 性能不达标时，先报告具体瓶颈（细胞数、路径数、帧时间）和可选的功能取舍，由用户裁定。
- **控制栏**：每个组合一行，包括细胞开关、核开关（按 4.5 置灰）、颜色、线宽、线型、状态和进度（如 `3/5 patch`、失败数）；另有全开/全关，细胞和核分开控制。
  - 默认值（设计选择）：细胞轮廓实线、核轮廓虚线，同一组合共用一种颜色。
- **渐进显示（R4）**：每个任务一完成，它的轮廓立即加入对应图层。

### 4.7 选定 → 全量分割（R7）

- **选定资格**（设计选择，A0 请用户确认）：
  - 一个组合的所有 patch 任务都已结束，并且至少有一个 `ok`，才能被选为最终；
  - 有失败的 patch 要在控制栏标出；
  - 过期结果（4.4）不能选。
- **基本约束随块 C 落地，不拖到最后**：
  - 没选组合时，Save 保持禁用；
  - 结果到达不会自动选中；
  - hash 不一致时拒绝 Save。
- **写出格式**：与今天同构，即 `normalize_segmentation_config` 加 `save_segmentation_params`。
- **交接目标**：从 Step1 Save 进入 Step2，**不做任何额外编辑直接运行**时，Step2 实际提交给执行器的方法和参数（`get_seg_config()` 的返回值）必须和所选组合一致。
  - 如果不一致，就是本计划交接目标的缺口，在块 E 里修复，不能记成 advisory。
  - 修复只限于让 Step2 正确装载所选参数，不重做 Step2 的参数界面。

## 五、分块、白名单与验收门

- 每块单独启动，真机验收通过后才进入下一块。
- 所有测试从 `/tmp` 启动，带 `-p no:cacheprovider --confcutdir=/`，只写合成项目。
- 每块结束时按附录的方法跑全量回归：新失败一律先和 HEAD 对比；**本块改动涉及的路径上的失败，不能用基线来豁免。**

### 块 A0 — 契约准备（只做复核和设计定稿，不改生产代码）
- **产出**：在本文档中补齐以下内容，供用户确认：
  1. 8 个方法的参数表：哪些可以写列表、类型、范围、默认值，包括 Mesmer 的「主要阈值」具体是哪几个；
  2. 8 个方法的输出表（4.5），包括 Mesmer whole-cell 是否有核输出；
  3. 任务格式和结果记录格式（4.4）、结果文件布局；
  4. 来源绑定：复用哪个身份，以及运行快照和运行中编辑的规则（4.4）；
  5. 组织掩膜：有无现成可复用的，以及「空白 40%」的计算方式；ROI 多边形包含判定的实现位置；
  6. montage 的显示供给：复用哪个接口、缓存归属、释放时机（4.6）；
  7. 选定资格规则（4.7）；
  8. Step2 交接的实测：在合成项目上从 Step1 Save 进入 Step2，记录 `get_seg_config()` 和所选参数是否一致。
- **白名单**：只写本文档，以及 scratchpad 里的只读诊断脚本。
- **验收**：用户逐条确认。

### 块 A1 — Patches 区（已有 patch 的勾选）
- **白名单**：
  - 新文件 `ui/step1_presegmentation/patches_panel.py`（包目录名开工时确认）；
  - `ui/main_window.py` 中 Method & Parameters 标签页的装配处和 patch 列表同步处；
  - 测试：新增 `tests/test_step1_patches_panel.py`。
- **验收门**：
  - 紧凑网格布局正确；
  - 默认全选，全选/全不选、逐个取消后的勾选集合正确；
  - patch 增删后，按 bbox 保留原有的勾选状态；
  - 没有 patch 时显示提示；
  - 真机验收。

### 块 A2 — 随机生成 patch（A0 的组织和 ROI 判定确认之后）
- **白名单**：
  - 新文件 `core/random_patches.py`（纯函数，不依赖界面）；
  - `patches_panel.py` 中的入口和弹窗；
  - 调用 Step0 **现有的** patch 写入接口。只调用、不改接口时不算扩围；需要改接口就停下申请。
  - 测试：新增 `tests/test_random_patches.py`。
- **验收门**：
  - 每个 patch 完整落在 ROI 多边形内（包括凹多边形的反例）；
  - 空白比例按 A0 定义不超过 40%（合成组织图上验证，核之间有间隙的组织不被误判为空白）；
  - 没有 ROI 时落在组织内；
  - 设计选择项：不重叠、同一种子结果相同、凑不够时如实提示；
  - Tissue Navigator 能看到新 patch；
  - 真机上生成一次。

### 块 B — Methods 区、`+` 弹窗、方案的保存和加载
- **白名单**：
  - 新文件 `ui/step1_presegmentation/method_editor.py`、`method_blocks.py`、`plan_store.py`；
  - `ui/main_window.py` 中的装配；
  - 隐藏 HQ/HQ2/CDS 通过「界面可见方法列表」实现，**不改** `SEGMENTATION_METHODS`；
  - 测试：新增 `tests/test_step1_method_plan.py`。
- **验收门**：
  - 参数列表的解析和校验（非法值、空值、重复值），按 A0 的参数表；
  - 组合数和总任务数正确；
  - R9 的合并（并集规则、单值冲突二选一、不合并则取消）；
  - 方案保存后再加载能完整还原；
  - 下拉框里没有 HQ/HQ2/CDS，但加载含这些方法的旧参数不报错；
  - 这一块不触发任何计算。

### 块 C — 执行层与结果接入（**模块级，须单独批准**）
- **必要性**：
  - 现有 worker 按第一个任务决定执行目标，一次运行不能混合多种方法；
  - 核 mask 被丢弃，expansion 方法的核标签被覆盖，满足不了 R5；
  - 结果文件互相覆盖；
  - 结果到达会自动变成当前参数。
- **拟改文件和范围**：
  - **发送端**
    - `workers/cellpose_worker.py`：
      - 按每个任务的方法分派；
      - 按 4.5 回传细胞和核两种 mask，expansion 方法在扩张前保留核标签；
      - 结果写到唯一路径；
      - 队列只传结果记录（路径和摘要），不传整张 mask。
    - `workers/mesmer_worker.py`：同上，另外回传 nuclear-guided 的核 mask。
  - **调度**
    - `ui/main_window.py` 的 `_launch_worker`：按方法族把任务分组派发，按 4.4 冻结运行快照。
  - **接收端**：`ui/main_window.py`，以下几处都纳入：
    - 结果轮询（`:6532` 一带），改为按结果记录读取；
    - 进度和结束的处理；
    - 旧结果视图的适配，在块 D 上线前维持可用；
    - `_record_segmentation_preview_result` 不再自动设置当前参数。
  - **基本选定约束**（4.7）：未选则 Save 禁用，hash 不一致则拒绝。
  - 可选的第二步（**另行申请**）：同一个 patch 的核分割阶段和 patch 读取在组合之间复用。
- **不改**：
  - 各方法的算法实现（cellpose/stardist/mesmer 的调用方式、HQ 系列的实现）；
  - fusion 合成算子；
  - 调度器和缓存基础设施。
- **风险**：进程和显存占用随方法族切换而变化。缓解办法是 mask 落盘、队列只传记录。
- **回退**：发送端、调度、接收端各自独立，可以按文件 revert；新旧结果路径并存。
- **验收门**：
  - 合成 patch 上三种方法族混合的任务都正确分派；
  - 每个任务的细胞和核 mask 与单独运行该方法的结果逐像素一致，expansion 方法的核 mask 等于扩张前的结果；
  - 三种状态可以区分：没有这种输出、成功但零细胞、失败；
  - 结果文件不互相覆盖；
  - 运行中增删 patch、编辑方法块、重新保存 Fusion，都不影响正在运行的任务；迟到的结果按 bbox 归位；
  - 结果不会自动成为选中项，未选组合时 Save 保持禁用；
  - 停止和关闭时没有残留进程；
  - 所有结果带正确的来源和 hash；
  - 真机上跑一个小方案（不超过 10 个任务）。

### 块 D — montage 结果视图（包含显示供给）
- **白名单**：
  - 新文件 `ui/step1_presegmentation/montage_view.py`、`mask_layers.py`、patch 底图缓存（归结果视图所有）；
  - `ui/main_window.py` 中 Patch Results 标签页的装配和结果回调；
  - A0 定稿的复用接口，**只调用不修改**；需要改 viewer、scheduler 或缓存层就停下申请；
  - 测试：新增 `tests/test_step1_montage_view.py`。
- **验收门**：
  - 尺寸不一的 patch 布局和点击命中正确；
  - 统一缩放和平移，适配全部；
  - Channels、Intensity、Overlay/Fusion 调节后底图跟随；
  - 每个组合的细胞和核开关、全开/全关、颜色、线宽（屏幕像素恒定）、虚实线都生效；
  - 按 4.5 置灰；失败和零细胞标注正确；
  - 渐进显示：结果一到就出现；
  - 缓存按 4.6 释放；
  - 在真实切片上实测性能（patch 数、组合数、细胞数、帧时间）；不达标时先报告瓶颈和取舍，由用户裁定；
  - 画面证据在真实 X display 上取。

### 块 E — 完整交接验收与旧界面退场
- **白名单**：
  - `ui/main_window.py` 的选定和 Save 相关状态；
  - `ui/step0/search_ctrl.py`、`ui/step0/result_grid.py` 只做「不再上屏」；
  - 如果 A0 或本块实测出 Step2 装载不一致，修复 `ui/step2_page.py` 中装载所选参数的部分，只限装载，不改 Step2 的界面；
  - 测试：更新 `tests/test_step1_fusion_settings_commit.py`、`tests/test_step1_to_step2_handoff.py`，新增端到端交接测试。
- **验收门**：
  - 从 Step1 Save 进入 Step2、不作任何编辑直接运行时，`get_seg_config()`（也就是实际提交给执行器的方法和参数）和所选组合**一致**，8 个方法各覆盖一次；
  - 选定资格规则（4.7）生效；
  - 旧的 Phase1/Phase2 控件不再上屏；
  - 用户在真机上完成一次「预分割 → 选定 → 全量分割」全流程。

## 六、未决与 advisory

- A0 各项产出（见块 A0）。
- 同一个 patch 的核分割阶段复用：块 C 的第二步，另行申请。
- Step2 的重复参数界面：本计划只保证「不作编辑直接运行时与所选组合一致」，不重做它的界面。
- 附录中的已有失败与本计划无关，不在范围内。但它们**不能豁免**本计划涉及路径上的任何失败。

---

## 附录：测试基线（`c9f80df`，2026-09-23）

**命令**：逐模块运行。多个界面模块放在同一个进程里跑会出现 segfault，所以必须一个模块一个进程。强制 GPU 时，renderer 须为 RTX 4090。

```bash
T=/sda1/Fusion/analysis_pipline/block01_v14/tests
cd /tmp && for f in $(cd $T && ls test_*.py); do
  BLOCK01_REQUIRE_STEP1_GPU=1 PYTHONPATH=/sda1/Fusion/analysis_pipline/block01_v14 \
    timeout 1500 python -m pytest $T/$f -p no:cacheprovider --confcutdir=/ -q -rfEs
done
```

需要 `PYTHONPATH` 的原因：部分模块用顶层的 `viewer.` 导入。

**结果**：169 个模块，3734 passed，16 failed，0 skipped；`cufile.log` 零增长（4449898 B）。

**与 HEAD 对比的方法**：
1. `git archive <commit> | tar -x -C <scratch>/headtree`；
2. 在 `<scratch>` 下对失败的模块运行 `PYTHONPATH=<headtree> python -m pytest <headtree>/tests/<f>.py --rootdir=<headtree> --confcutdir=<headtree> -p no:cacheprovider -q -rf`。

2026-09-23 以 `4c1a418` 的导出树为对照，下面 16 条在对照树上**逐条同样失败**。

| 模块 | 失败用例 |
|---|---|
| test_step0_channel_conditioning.py | test_patch_switch_reads_only_active_channel<br>test_unloaded_channel_lazy_loads_on_switch<br>test_dapi_lazy_loads_like_a_marker<br>test_sync_passes_the_active_channel_eagerly_and_the_rest_lazily<br>test_unvisited_patch_does_not_inherit_zoom<br>test_patches_keep_independent_viewports |
| test_step0_process_incremental.py | test_a_finished_run_leaves_its_channel_up_to_date<br>test_a_sigma_change_makes_only_that_channel_stale<br>test_a_changed_patch_list_invalidates_every_channel<br>test_a_recompute_always_runs_its_own_channel<br>test_dataset_reset_forgets_the_signatures |
| test_hq_marker_segmentation.py | TestHqMarkerSegmentation::test_hq_roi_only_source_does_not_silently_fallback_to_first_group<br>TestHqMarkerSegmentation::test_hq_roi_only_source_uses_saved_roi_not_first_group |
| test_step0_no_process_button.py | test_save_still_builds_its_config_from_the_ticked_rows |
| test_preview_source_provider.py | test_save_invalidation_repulls_into_workbench |
| test_tissue_navigator_viewport_sync.py | test_mapping_slide_local_to_full |

**批量运行时偶发失败、单独运行 3/3 通过**：`test_step0_channel_conditioning.py::test_loaded_channel_switch_is_cache_hit`。

**使用规则**：
- 这张表只用来说明「这条失败不是本块引入的」，而且必须在当时重新和 HEAD 对比过；
- 本计划涉及的路径上，任何失败都要修复或单独报告，不能引用这张表来豁免；
- 块 C 涉及 `test_hq_marker_segmentation` 所覆盖的 worker 路径，届时要重新评估这两条失败。
