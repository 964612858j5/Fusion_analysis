# Step1 预分割（Method & Parameters / Patch Results）重设计 — 项目计划

日期：2026-09-23（第三版，块 A0 产出）　分支 `v15-interactive-channel-workspace`，起点 `c9f80df`，A0 核查基于 `e655409`。
状态：**已执行：块 P、A1、A2、B、C、D、V0。本文档记录的验收：B「用户验收总体通过」、C 第 4 步「用户人工测试通过」、D「块 D 验收通过（2026-09-25）」；P、A1、A2、V0 的执行记录仍写「待用户验收」，文档中没有后续验收记录。V2（代码中称「Step2 hook-up」）第 1、2 步已提交（`54e825d`、`1adfe4e`），第 3 步已提交（`dcaca2c`），真机上 Cellpose 路径跑通，Mesmer 未验收；块 L 已提交（`1d14801`、`2294bc7`）并通过真机验收；块 F 已提交（`5bc65bc`）并通过真机验收；块 E 已提交（`7ee98fc`）并通过真机验收（Mesmer 除外）；U1、Results 顺序、块 S 已提交；块 K 已实施并通过真机验收；块 M 已实施并通过真机验收；S2 冻结；后续计划见第六节。** 提交本文档不代表批准任何生产实施。每块须用户单独启动；模块级改动须另行批准。

修订记录：
- v1：初稿。
- v2：采纳独立审核的第 1、3、4、5、6 条，以及「先做契约准备块、A 拆成 A1/A2、选定的基本约束随 C 落地」的建议。第 2 条（同名方法合并）按用户裁定**维持 v1 方案**。另补上可核验的测试基线（附录）。
- v3：块 A0 的 8 项产出，见**第七节**。第一至六节的正文不改，凡被第七节更正或细化的地方，以第七节为准（4.5 的 Mesmer 两行、4.4 的来源字段、4.7 的资格规则）。第七节里标「待确认」的条目，确认前不算定稿。
- v3.1：按独立审核意见修订第七节：F1、P1–P3、O1、O2、L1、S1、T1、T2、E1、E2 已裁定，另外明确块 D 缓存和线程的授权边界。新增 7.10 块 V（分割方法运行沙箱，用户提出，**未批准**）。
- v3.2：块 V 的方向通过独立审核。7.10 按审核意见重写，分为 V0 / V1/C / V2 三个阶段，只有 V0 可以申请启动。
- v3.29：块 N v2 获批；Step3 裁定 8a（补生成失败时的退路）。
- v3.28：Step3 重设计的只读调查、Odon 参考与用户裁定；块 N 申请（Step2 生成标签金字塔，已按独立审核修订为 v2）；后续计划加入 Step4 优化（`.dat` 清理）与长期的 NGFF 评估。
- v3.27：块 M 已实施并通过真机验收；新增已有问题（StarDist 偶发不一致、Cellpose CPU 极慢）。
- v3.26：块 M 申请与用户裁定（Step2 的 8 个方法统一走引擎子进程）。
- v3.25：块 K 真机验收通过并提交（`bc280d1`）；记录「旧路径 Stop 不能立即停止」的调查结论和用户决定（不改）。
- v3.24：块 K 已实施（旧路径 ROI 模式中途 Stop 不再登记成功），写入执行记录，待真机验收；新增已有问题：运行资源监控器 `NameError`。
- v3.23：用户裁定 HQ / HQ2 / CDS 这类不经 Step1 交接的方法不再维护（R2 加注，第六节新增「用户裁定」）。写入块 K 申请（Step2 旧路径 ROI 模式中途 Stop 不再登记成功，待批准），已按独立审核意见修订。
- v3.22：块 U1、Results 顺序、块 S（每次弹对话框、拒绝原因上屏）已提交；S2 冻结，记录调查结论；新增「后续计划」和「已知的已有问题」。
- v3.21：块 F、块 E 已提交（`5bc65bc`、`7ee98fc`），都通过真机验收。
- v3.20：记录 V2 第 3 步的提交（`dcaca2c`）和真机情况；新增块 L（Step1 Save 进度框、Step2 布局，计划外，用户 2026-09-25 提出）的申请、执行记录和真机验收。
- v3.19：补记 V2（Step2 hook-up）第 1、2 步的执行记录和用户裁定 A（写在 7.10.7 的 V2 下）；更新状态行。第 3 步的范围另行申请。
- v3.18：块 B 已执行（Methods 部分、参数表、R9 合并、方案保存与加载）。
- v3.17：Step1 patch 按钮统一用 patch 色（与 Step0 共用样式）。
- v3.16：左栏标签页 Channels 改名为 Fusion。
- v3.15：A2 后续：Delete（删除勾选的 patch）、标签页改名为 Pre-segmentation。
- v3.14：A2 已执行（实测、用户裁定层级为固定 16×、实现、测试）。
- v3.13：A1 已执行，写入执行记录；用户接受「重启后 Step0 模型为空」的风险，暂不另立块处理。
- v3.12：块 P 已执行，写入执行记录和 advisory（重启后 Step0 模型为空）。
- v3.11：新增块 P（patch 稳定编号与同步：编号永久不变、不复用；只显示一个名字；Navigator 可重命名），排在 A1 之前。A1 按用户意见修订：悬停时不显示信息；已勾选的小块可以用 × 删除、双击重命名；可修改规则文件。
- v3.10：V0 已执行，新增 7.10.8，记录执行结果、实测数据、执行中的发现（Cellpose 原地改写输入；conda 和 pip 同名包；子进程工作目录）和输入归属表。
- v3.9：块 V 按用户裁定，由「每个引擎一个环境」改为「一个环境 `fusion_mesmer`，每个引擎一个子进程」；记录这个环境的实际改动和核验结果；写入经用户审定的 V0 范围（导出清单、照清单临时重建后删除、模型清单与断网验证、子进程原型放进仓库 `seg_runner/`、输入归属表）。
- v3.8：按审核意见收口。R13 改为「按相同规则参与构造」；「只定标一次」改为「应用侧不额外拉伸，每个引擎只执行一套标准预处理」；Mesmer nuclei 的第二个通道为 0，并记录 Step2 现在给的是 fusion；多余定标改为「新流程不再调用」，旧参数保持原语义；搬迁和新行为分开验收；7.11.4 写明「读取区域相同」的定义，输入数组按方法构造后再比较；几处过强的说法改为待验证。
- v3.7：用户裁定 Mesmer 的膜通道用 Fusion、CLAHE 保留。写定 Mesmer 的输入为 `[fusion 核通道, fusion]`，并列出 Step1 的两处修改：膜通道要 ÷ 65535，核通道要改为带权重的 fusion 核通道。新界面只提供 Fusion 当膜通道（已裁定）。
- v3.6：R11 按用户裁定修订：保留模型的自动定标，按局部做，每条路径严格只做一次；I0–I3 作废；7.11.5 改写为定标规则和逐处排查表；验收第 2 条改为「自动定标只做一次」。
- v3.5：新增用户裁定 R13（权重在两边都生效）和 R14（一套轮子、方法模块化），新增 7.12 架构大纲。7.11.2 更正 Mesmer 两层定标的事实描述，补充膜通道为空时输入全为 0。用户对「是否关闭模型内部归一化」有新的考虑，I1、I2 **重新开放**，改为统一的 I0 决策。
- v3.4：按审核意见修订 7.11：I1、I2、H1 已裁定；I3 改写（Mesmer 有三步局部预处理）；新增 N1（纯核输入的核权重和量化）；写定 H2；更正细胞和核的配对规则，以及 Step2 实际生效的归属路径；验收拆成两条。
- v3.3：新增用户裁定 R10–R12（通道排法、全局亮度、Step1 HALO）；新增 7.11 模型输入契约；按讨论意见修正 7.10.4 和 7.10.5（终态登记、引擎身份与实际设备分开）。

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
| R2 | HQ / HQ2 / CDS 只是**在新界面不可见**，后台代码和 Step2 兼容**全部保留**。<br>**2026-09-26 用户补充裁定：HQ / HQ2 / CDS 这类不经 Step1 交接的方法不再维护**（见第六节「用户裁定」）。 |
| R3 | 可以写成列表的参数：<br>• Cellpose：diameter、flow、cellprob<br>• StarDist：prob、nms、expand<br>• Mesmer：主要阈值<br>模型名这类参数只允许单值。**取消原来的两阶段流程**（Phase1 定直径 → Phase2 扫 flow × cellprob），改为每个方法对各参数列表取笛卡尔积。 |
| R4 | 任务数（勾选 patch 数 × 全部组合数）**超过 10 个时，开始前弹窗提示**；用户坚持就照常运行。**某个结果一出来就可以在 Step1 看**，不必等全部结束。 |
| R5 | 纯核方法只有核 mask，纯全细胞方法只有细胞 mask，expansion 类两种都有。**没有的那一种，开关置灰。** |
| R6 | 结果视图参考 step5_v8 的 montage viewer：所有 patch 都可以 zoom in/out，可以查看通道和分割情况。 |
| R7 | **只允许一个方法、一组唯一参数组合**进入最终分割。 |
| R8 | 执行层的模块级改动单独成块申请（块 C），批准后才动手。 |
| R9 | 同名方法：询问是否合并；合并时每个参数取两边取值的并集，单值参数冲突时让用户二选一。**不做展开后的过滤去重。**（审核第 2 条，用户裁定维持原方案。）**2026-09-24 用户修订**：选「不合并」时**保留新添加的块**，不再取消这次添加，因此同一方法可以有多个块；选「合并」时并入该方法的第一个块。另外 `Edit` 允许更换方法。 |
| R10 | （2026-09-23）Cellpose whole-cell 的模型输入，Step1 和 Step2 **统一为 `[fusion, fusion, DAPI]`**，并显式指定 `channel_axis=-1`。不再测量旧的两种通道排法哪个更好。fused.zarr 可以继续存 `[fusion, DAPI]` 两通道，送进模型前再复制 fusion。纯核方法按方法定义只用 DAPI。 |
| R11 | （2026-09-23，**当日修订**，以修订版为准）亮度分两层：①用户手调的显示窗口（min/max/gamma）和 fusion 权重，这是用户的设置，**不算自动定标**，在全局上生效，随运行快照冻结；②模型的**自动定标**：**保留，按局部进行**，也就是在当前送进模型的那张图上计算。应用侧**不再额外做任何自动拉伸**，每个引擎只执行**一套**明确列出的标准预处理流程，而且只执行一次（见 7.11.5）。同一个细胞在不同 patch 或切块里定标后**可能不同，幅度尚未实测**，用户**接受**。（原版要求「全局固定、禁止局部估计」，已被本修订取代。） |
| R12 | （2026-09-23）Step1 采用和 Step2 相同的 **HALO** 做法：在 patch 四周多读一圈参与计算，推理和后处理都在带 HALO 的区域上完成，然后只保留和统计中央的 patch。 |
| R13 | （2026-09-23；v3.8 按审核意见改写措辞）fusion 的通道窗口（min/max/gamma）和权重（通道权重、组权重、核权重），在 Step1 和 Step2 都必须**按相同规则参与 fusion 输入的构造**。这**不等于**「调了权重，mask 就一定会变」：局部定标可能把整体亮度拉回去（7.11.5「已知的后果」），验收时不能这样要求。Step1 纯核方法绕过 fusion、直接读 DAPI，是**设计错误**，要改正。N1 据此定为 (a)：两边都读 fusion 的核通道，核权重为 0 时拒绝运行。 |
| R14 | （2026-09-23）**一套轮子**：Step1 和 Step2 能共用的组件一律共用，尽量不为同一功能造不同的轮子。分割方法**模块化**，Step1 和 Step2 只是调用方法模块的基座，便于维护。大纲见 7.12。 |

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

### 块 P — patch 稳定编号与同步（用户裁定 2026-09-24；排在 A1 之前；**跨模块，须单独启动**）

**用户裁定**：
- **P-1 编号稳定**：删掉 P2 后，P3 及以后的编号**不变**。新 patch 的编号是「历史上用过的最大号 + 1」，**不回填空号**，已删除的号永不复用。例如有 P1、P2、P3，删掉 P3 再新增，新的 patch 叫 **P4**。随机生成的 patch 也遵守这条规则。
- **P-2 显示**：每个 patch 只显示**一个名字**，默认就是它的 `Px`，不附加尺寸、坐标或其他任何内容。重命名之后，显示的就是新名字（这是对用户「只显示 Px」的理解，按单一名字执行；用户如有不同意见，另行更正）。内部始终用永久编号来识别 patch。
- **P-3 同步与编辑**：以下各处显示的 patch 编号和名字必须一致：Tissue Navigator / Tissue Preview、Step0、Step1 viewer 顶部的 patch 选择器、Method & Parameters 里的 Patches 栏。Tissue Navigator 可以移动、调整大小、删除和**重命名** patch；重命名的方式是在 patch 列表中**双击**，和现在 ROI 的改名方式一样。

**现状**（2026-09-24 核查）：
- 代码里**没有**任何稳定的 patch 编号，所有 `P{i+1}` 都是按列表位置临时算出来的：
  - `overview_panel.py:2663/2724`；
  - `step0_page.py:6413/9079/9113/11237`；
  - `main_window.py:4594-4646/3039/3075/3131`；
  - `step1_5_bg_page.py:536`。
- Step0、Navigator、Step1 之间**只传坐标**。`geometry_committed` 携带的名字在 `main_window.py:2068-2101` 被丢掉了。
- 判断 patch 是否变化**只比较坐标**（`core/step0_handoff.py:369/445/525`），所以只改名字不会被发布出去。
- Step1 会先按 ROI 的 bbox 过滤 patch，再按位置编号（`main_window.py:4548`），导致**现在** Step0 的 P3 在 Step1 可能显示为 P2。
- Navigator 编辑后，要等 Step0 保存过至少一次才会发布到 Step1（`step0_handoff.py:516`）。这条行为保持不变。

**做法**：
- **数据**：每个 patch 记录增加 `id`（整数，永久不变）和 `name`（默认 `P{id}`）。patch 配置里另记 `next_patch_id`。
- **旧数据迁移**：没有 `id` 的旧记录，第一次读取时按原来的顺序补上 1…n，这样和用户以前看到的编号一致。
- **变化比较**：改为比较 `(id, name, bbox)` 三项。
- **Step1**：接收 `id` 和 `name`。工具栏按钮、Patch 菜单、结果历史（`_seg_preview_history`）、session 里的 `selected_patch`，都改为按 `id` 来记。
  - 按下标索引的缓存（通道缓存、加载状态等）保持现在的失效规则，只影响性能，不影响正确性。
- **Navigator 重命名**：在 Step0 的 patch 列表中双击改名，名字不能为空，也不能和其他 patch 重复。
- **不改**：分割 worker 的 npz 命名（`cellpose_worker.py:491`、`mesmer_worker.py:162`），以及旧的结果网格。这两处会在块 C 和块 E 里整体重写。

**白名单**（启动时再确认一次）：
- `ui/step0/overview_panel.py`
- `ui/step0/step0_page.py`
- `ui/step0/roi_context_model.py`
- `core/step0_handoff.py`
- `ui/main_window.py`：只改 patch 接收、显示和历史相关的部分
- `ui/step1_5_bg_page.py`：只改按钮显示
- 测试：
  - 新增 `tests/test_patch_stable_ids.py`；
  - 更新按位置写死 `P1..Pn` 的现有测试：`test_step1_patch_selector_menu.py`、`test_step0_step1_surface_details.py`、`test_step0_channel_conditioning.py`（`:300/:473`）、`test_step1_geometry_sync.py`、`test_step1_dataset_switch.py`、`test_tissue_navigator_popup.py:452` 等。每条修改都在报告里逐条说明。

**验收门**：
- 删掉中间的 patch 后，其余 patch 的编号和名字都不变；删掉最大号后再新增，编号顺延，不复用。
- 重命名之后，Navigator、Step0 画布和列表、Step1 工具栏和菜单、Patches 栏同时更新，并且写进磁盘、能被发布出去。
- 旧项目读取后的编号和原来一致。
- 在 Step1 打开 Navigator 做的编辑，Step1 能收到，编号不错位（包括 ROI 过滤之后）。
- 全量回归；真机验收。

**风险与回退**：
- 数据格式只增加字段，旧文件可以读取，旧代码读取新文件时会忽略新字段。
- 可以按文件回退。

**执行记录**（2026-09-24 启动，待用户验收）：
- **用户补充裁定**：重命名后显示用户起的名字，不必再是 Px。内部永久编号不变。
- **做法**：
  - `ui/step0/roi_context_model.py` 新增 `Patch` 类型。它仍然是 `(y0, y1, x0, x1)` 这个 4 元组，现有代码照常拆包、转换和比较；在此基础上多带 `id` 和 `name`，所以能原样经过 `patches_changed`、Navigator 弹窗（`tissue_navigator_popup.py` 不用改）、Step0 列表和 handoff。
  - 编号统一由模型的 `allocate_patch_id` 分配，只增不减。换数据集时重置；绑定到已发布的项目时，从 manifest 的 `next_patch_id` 取起点。旧 manifest 没有这个字段，就取 `n_patches + 1`。
- **Step0**：
  - 画布标签、信息行、提示、patch 列表、按钮和菜单都显示名字；
  - 按钮通过 `patch_index` 属性来匹配，不再按文字匹配；
  - 在 patch 列表中**双击可以重命名**，名字不能为空、不能重复，改名走 `patches_changed` 通道发布；
  - 写入磁盘的记录带上 `id` 和 `name`。
- **handoff**：
  - `patch_identities` 按 `(id, name, bbox)` 比较，所以只改名字也会发布；
  - manifest 带上 `next_patch_id`，全量 Save 和只改几何的发布都写，而且这个值只增不减。
- **Step1**：
  - 从 Step0 提交、磁盘、session 读入 patch 时都保留 `id` 和 `name`；
  - ROI 过滤之后编号不重排；
  - 工具栏、菜单、状态文字都显示名字；
  - 历史记录（`_seg_preview_history`）和 session 的 `selected_patch` 改为按编号记，键写成 `P{id}`，和旧 session 兼容；
  - 删除一个 patch 时，只清掉被删掉的或矩形变了的那几个 patch 的历史，其余保留。按下标索引的缓存仍用原来的失效规则。
- **Step1.5**：按钮显示名字，按下标匹配。
- **Step0 patch 列表的行格式**：保持原来的 `名字  ROI  [HxWpx]`，只把 `P{idx+1}` 换成了名字。P-2 说的「只显示一个名字」，这里理解为指 patch 标签本身；列表这一行要不要去掉 ROI 和尺寸，**请用户确认**。
- **测试**：
  - `tests/test_patch_stable_ids.py` 共 11 条。
  - 在 HEAD 导出树上运行会报收集错误。
  - 另做了两处反向注入，对应的测试都会失败：handoff 只比较 bbox；ROI 过滤时丢掉 id。
  - 与 patch 相关的现有模块全部通过，只剩附录基线里原有的 7 条失败。
- **全量回归**（2026-09-24，171 个模块，每个模块单独一个进程）：
  - 结果：3757 passed / 16 failed / 1 skipped。回归期间代码冻结，结束后逐个核对哈希，都没有变化。
  - 16 条失败和附录基线的清单**逐条相同**。
  - 其中 `test_step0_channel_conditioning` 落在本块改过的 Step0 patch 路径上，所以不能直接拿基线豁免，另外做了核对：
    - `test_dapi_lazy_loads_like_a_marker` 的断言信息和上一轮不同：上一轮是 `[] == ['DAPI']`，这一轮是 `None is not None`；
    - 在 HEAD 导出树上把整个模块跑两次、单条跑三次，都是 `None is not None`，而且其中一次整模块运行还多了一条已知的偶发失败；
    - 当前代码的表现和 HEAD 完全相同。
    - 结论：这条用例的断言信息本来就随运行状态变化，**不是本块引入的**。
  - 其余 5 个有失败的模块，断言信息和上一轮逐条相同。
- **Advisory**（本块之前就存在，**未实测**）：
  - Step0 重启后不会从磁盘把 patch 读回模型：`_roi_model` 只会通过 Step0 自己的编辑填充。
  - 所以重启之后，如果直接在 Step1 打开 Navigator 编辑，Navigator 看到的可能是一个空模型，发布出去的几何可能会覆盖磁盘上原有的 patch。
  - 块 P 只保证编号不冲突，也就是 `next_patch_id` 从 manifest 取起点。这个问题本身需要另外核实，并单独立块处理。

### 块 A1 — Patches 区（已有 patch 的勾选；依赖块 P；用户 2026-09-24 修订）
- **内容**：
  - 在 Method & Parameters 页的**顶部**放一个 Patches 栏，旧控件原样留在它下面，到块 E 再退场。
  - 每个 patch 是一个可勾选的小块，显示它的名字（P-2），边框颜色用 `PATCH_COLORS`。
  - **鼠标悬停时不显示任何信息**（用户裁定）。
  - 小块自动换行，最多显示 3 行，超出时在栏内滚动。
  - 下方一行：`Select all`、`Select none`、已选 k/n。
  - 默认全选，新 patch 也默认勾选；勾选状态**按 patch 编号保留**。
  - 没有 patch 时，显示提示 `No patches yet — draw them in Step0 or the Tissue Navigator`。
- **编辑**（用户裁定）：
  - **已勾选的**小块右上角有一个小 `×`，点击即删除该 patch；
  - **双击**小块可以重命名。
  - 这两个操作走的是和 Navigator 编辑**同一条**发布通道（`_reconcile_roi_edit` → `_persist_geometry_edit`），不另建一条路径。所以编辑结果会同步到 Navigator、Step0 和 Step1 工具栏。
- **勾选结果目前不接入任何计算**，要到块 C 才接上；也不写进 session。
- **规则文件**：用户同意在 `UI_SURFACE_RULES.md` 第 4 节补充 Patches 栏的描述。
- **白名单**：
  - 新文件 `ui/step1_presegmentation/__init__.py`、`ui/step1_presegmentation/patches_panel.py`（控件本身和自动换行布局）；
  - `ui/main_window.py`：Method & Parameters 页的装配处（约 `:1176-1203`）、`_on_patches`（约 `:4710`），以及删除和重命名接到发布通道的接线；
  - `UI_SURFACE_RULES.md` 第 4 节；
  - 测试：新增 `tests/test_step1_patches_panel.py`。
- **验收门**：
  - 布局正确，能自动换行，高度有上限；
  - **不会抬高左栏的最小宽度**：最小宽度不超过一个小块加上边距；
  - 默认全选；全选、全不选、逐个取消后，勾选集合都正确；
  - patch 增删之后，按编号保留勾选状态；
  - 没有 patch 时显示提示；
  - 点击 `×` 删除、双击重命名之后，Navigator、Step0、Step1 工具栏都同步更新；
  - 新测试先在 HEAD 导出树上跑，确认会失败；
  - 全量回归；真机验收。

**A1 执行记录**（2026-09-24 启动，待用户真机验收）：
- **范围内的用户裁定**：
  - Navigator 的 patch 列表每行只显示名字，去掉 ROI 和尺寸，所以本块改动了 `ui/step0/step0_page.py`。
  - 块 P 列出的 advisory「重启后 Step0 模型为空，可能覆盖 patch」：用户**接受这个风险，暂不另立块处理**，原因是当前使用的是测试数据。
- **实现**：
  - 新增 `ui/step1_presegmentation/patches_panel.py`，内容是 `PatchesPanel`、`PatchTile` 和 `FlowLayout`：
    - `FlowLayout` 的最小宽度等于一个 tile 的宽度，内容自动换行；
    - 高度随行数增长，最多 3 行，超过后在内部滚动；
    - 条带纵向不拉伸，多出的空间留给下面的方法控件；
    - 计数标签不计入最小宽度；
    - 双击时，把勾选状态恢复成本组点击开始之前的状态。
  - `ui/step0/step0_page.py`：新增公开的 `delete_patch(pid)` 和 `rename_patch(pid, name)`，都经由画布的 `patches_changed` 发布；列表行改为只显示名字。
  - `ui/main_window.py`：在 Method & Parameters 页的顶部装配这个条带；在 `_on_patches` 里把 patch 同步过去；把 `×` 和重命名接到 Step0。
  - `UI_SURFACE_RULES.md` 第 4 节新增两条：Patches 条带、patch 名称规则。
- **测试**（`tests/test_step1_patches_panel.py`，13 条）：
  - 在 HEAD 导出树上运行，报收集错误。
  - 五处反向注入都会让对应的测试失败：
    - FlowLayout 按整行宽度要最小宽度；
    - 双击时不恢复勾选状态；
    - 列表行重新带上 ROI 和尺寸；
    - 条带纵向可以被拉伸；
    - 计数标签被弹性空白挤掉。
  - 其中「双击」这条，第一版测试用的是 `QTest.mouseDClick`，它会多发一次按下事件，把缺陷掩盖了，反向注入时测试仍然通过。已改成手动发送 Qt 5 真实的事件序列：按下、松开、双击、松开。
  - 另外两条屏幕布局问题（条带被拉伸、计数被挤成宽度 0）是在离屏 `grab()` 截图里发现的，之前的自动测试没有覆盖。已补上窗口显示后的实测门：`test_on_screen_...`。截图是离屏渲染的，**不是物理屏幕截图**。
- **全量回归**（2026-09-24，共 172 个模块，每个模块单独一个进程）：
  - 结果：3769 passed / 17 failed / 1 skipped。
  - 第一轮跑到中途，我发现布局问题并修改了代码，所以那一轮作废、中止，改完后重跑。重跑期间代码冻结，结束后核对哈希一致。
  - 17 个失败中，16 条与附录基线**逐条相同**。
  - 第 17 条是 `test_overview_patch_editing.py::test_an_edit_on_the_thumbnail_reaches_the_patch_list`：
    - 原因：它断言列表行包含尺寸 `1000x1000px`，这与用户「列表只显示名字」的裁定冲突。
    - 处理：坐标断言（`page.patches`）保持不变；列表断言改为「只有一行，且等于 `P1`」。
    - 验证：改后这个模块 37 条全部通过；在 HEAD 导出树上，新断言会失败（旧代码的列表行带 ROI 和尺寸），说明它确实在检验新规则。
    - 只动了这一个测试文件，其他模块不受影响，所以没有重跑整个回归。
- **左栏宽度**：
  - 在离屏窗口中对比：显示条带和隐藏条带时，左侧标签页的最小宽度相同。第一版比隐藏时宽了 3 px，原因是计数标签计入了最小宽度，已经修正。
  - 已有测试门：patch 数量多时，最小宽度不变。

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

**A2 执行记录**（2026-09-24 启动，待用户真机验收）：
- **第一阶段实测**（真实切片，只读）：
  - 16× 层：耗时 2.0 s，峰值内存 0.7 GB；
  - 4× 层：耗时 52.8 s，峰值内存 6.3 GB；
  - 256、512、1024 px 三种候选，16× 层与 4× 层的取舍判断有 99.7% 一致，空白比例平均相差 0.2–0.3%；
  - 空白比例几乎全部集中在两端：约 97% 的候选空白比例不到 10% 或超过 90%，所以 40% 这个阈值放在哪里影响很小；
  - 接受率约 71%；
  - 这张切片只有 7 个洞，全部是小洞，都被填成组织，因此「大腔隙保持为空白」这条规则在这张切片上**没有得到检验**。
- **用户裁定**（2026-09-24）：
  - 掩膜层级改为**固定选用「最接近 16× 且不比它更细」的现有层**，**取代 T2 原来按 patch 短边选层的规则**；
  - 参数（level-0 单位）：σ 128、闭运算半径 256、最小组织块 819 200 px²、填洞上限 2 048 000 px²，都是经验值；
  - 后台线程：批准；
  - 随机种子：固定；
  - 生成记录：只打印到终端。
- **实现**：
  - `core/random_patches.py`：纯函数，包括选层、组织掩膜（距离变换实现精确圆盘闭运算）、面积加权的积分图空白比例、凹多边形的精确包含判定（四角都在多边形内，且没有任何边接触矩形轮廓）、带重试上限的生成过程。
  - `ui/step1_presegmentation/random_job.py`：一次性后台线程，用 tifffile 只读取出层级信息，再用 `read_region_lowres` 读核通道。
  - Patches 条带新增单独一行 `Random…`，点开是数量、宽、高对话框。放在单独一行是为了不把左栏撑宽。
  - `OverviewPanel.add_patch_rects` 和 `Step0Page.add_patches`：批量加入，只发布一次，编号接着现有的顺延。
  - `ui/main_window.py`：负责接线、结束后的处理，以及数量不够时如实提示。
  - `UI_SURFACE_RULES.md`：补充 `Random…` 的说明。
- **真实切片端到端**（只读，`run_generation`）：没有 ROI 时生成 10 个 512 px 的 patch，耗时 2.46 s，抽了 14 次候选就全部找到，层级 16×。
- **测试**（`tests/test_random_patches.py`，11 条）：
  - 在 HEAD 导出树上会报收集错误；
  - 四处反向注入都会让对应的测试变红：多边形只判四个角、掩膜不做平滑和闭运算、不检查重叠、数量不够时不提示；
  - 相关的现有模块（Patches 条带、稳定编号、overview 编辑、UI 契约）全部通过。
- 离屏 `grab()` 截图显示 `Random…` 单独占一行，布局正常。这是离屏渲染，**不是物理屏幕截图**。
- **全量回归**（2026-09-24，173 个模块，每个模块单独一个进程）：3781 passed / 16 failed / 1 skipped。16 个失败与附录基线的清单**逐条相同**，没有新增失败。回归期间代码冻结，结束后核对哈希一致。

**A2 后续小改**（2026-09-24，用户裁定；A2 已提交为 `44b1eb2`）：
- **`Delete` 按钮**：放在 `Random…` 右边，只删除**勾选**的 patch。配合 `Select all` 就是全部删除，只勾几个就只删那几个。不单独设 "Delete all" 按钮。
  - 没有勾选时按钮是灰的；删除前要确认一次；一次删除多个 patch 只算一次编辑、只发布一次（`OverviewPanel.remove_patches`、`Step0Page.delete_patches`）。
  - 删除前的确认框是我加的设计选择，用户没有专门表态；如果不需要，可以去掉。
- **标签页改名**：`Method & Parameters` 改为 **`Pre-segmentation`**。代码、`UI_SURFACE_RULES.md`、检查标签名的 3 个测试同步更新。
  - 历史计划 `docs/step1_rework_plan.md` 保持不动。
  - 用户指南 `docs/user_guide.md` 和 `docs/用户指南.md` 暂不改：它们描述的还是旧的 Phase1/Phase2 界面，等块 E 旧界面退场时一起更新。
- **测试**：
  - `test_step1_patches_panel.py` 增加到 16 条；
  - 两处反向注入都会失败：Delete 删除全部 patch、按 patch 逐个发布；
  - 相关模块全部通过。
- **全量回归**（2026-09-24，173 个模块）：3784 passed / 16 failed / 1 skipped，16 条失败与基线**逐条相同**。
- **离屏截图**：左栏最小宽度仍是 191。离屏窗口的左栏只有 257 px，两个标签名都会被省略显示，这是标签栏允许的行为，而且旧名字更长。真机上的显示请用户验收时确认。

**标签页改名 Fusion**（2026-09-24，用户裁定；上一批改动已提交为 `5269553`）：
- 左栏第一个标签页从 `Channels` 改名为 **`Fusion`**。原因：标签页里的框标题 `Channels` 与 Step0 保持一致，标签页再叫 Channels 就重复了。这个页面定义的是 fusion（勾选、权重、Save Fusion Settings），与 `Pre-segmentation` 放在一起，正好对应 Step1 标题的前后两半。
- 框标题 `Channels` 和 Step0 都**不改**。
- 按用户要求只跑相关测试，**不做全量回归**：`test_step1_layout_block_a`、`test_ui_surface_contract`、`test_step1_channel_panel`、`test_step1_patches_panel`、`test_global_channel_dock`、`test_step0_step1_surface_details` 全部通过。

**Step1 patch 按钮配色**（2026-09-24，用户裁定；上一批改动已提交为 `0cf9f04`）：
- **原因**：Step1 viewer 顶部的 patch 按钮按预载状态上色，只有 ready 才显示 patch 色，其余状态是灰色；而整张切片 viewer 从不预载，所以按钮一直是灰的。
- **改为**：
  - 按钮一律用 patch 自己的颜色，样式与 Step0 按钮**完全相同**。
  - 样式由同一个 `patch_button_qss(color)`（`ui/step0/step0_page.py`）生成，Step0 和 Step1 共用，符合 R14「一套轮子」。
  - 加载状态只由标签上的 ⟳ / ✓ / ✗ 表示。
- **取色方式**：与 Step0 按钮、Pre-segmentation 条带、Navigator 画布一样，按 patch 在列表中的位置取 `PATCH_COLORS`，所以四处颜色一致。
  - **已按用户裁定修改（2026-09-24）**：改为按 patch 的**永久编号**取色，`P<n>` 用第 n-1 号颜色，所以删掉 P2 之后 P3 仍保持原来的颜色。从没编辑过的列表，编号与位置一致，颜色和改之前完全相同。
    - 取色统一用 `patch_color` / `patch_color_for_id`（`ui/step0/roi_context_model.py`）。
    - 使用这两个函数的地方：Navigator 画布与 Step0 画布（标签框和信息行）、Step0 按钮、Step1 按钮、Pre-segmentation tile、Step1.5 按钮。
    - 旧的结果网格 `result_grid.py` 仍按行号取色，它会在块 E 退场。
    - 新增 `test_a_patch_keeps_its_colour_in_every_view_when_another_is_deleted`。把取色反向注入为按位置后，这条测试会失败。
    - 相关模块全部通过。`test_step0_channel_conditioning` 第一次运行多出 1 条失败，重跑整个模块后只剩基线的 6 条；相关测试单独跑 3 次都通过，属于附录记录过的偶发失败。
    - 按用户要求**没有做全量回归**。
- **测试**：
  - 新增 `test_every_patch_button_wears_its_patch_colour_in_every_state`：四种状态下，按钮样式都等于 Step0 样式，且与条带 tile 同色。
  - 在旧代码上，idle、loading、error 三种状态会失败。
  - 相关模块通过：patch selector、Step0/Step1 surface、patches 条带、稳定编号、几何同步。`test_step0_channel_conditioning` 的 6 条失败与基线相同。

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

**B 执行记录**（2026-09-24 启动；用户验收总体通过，验收后修改已人工复验通过）：
- **用户裁定**：
  - 本块不放 Run / Stop，留给块 C（界面上不放点了没反应的按钮）；
  - 合并时，单值参数的冲突在同一个弹窗里逐项二选一。
- **实现**：
  - `utils/segmentation_param_schema.py`：纯数据加纯函数，内容是 8 个方法的参数表（按 7.1），以及解析与校验、笛卡尔积、`combo_id`、R9 合并、摘要。**不改** `SEGMENTATION_METHODS`。
  - `ui/step1_presegmentation/method_editor.py`：`+` 弹窗，只列 8 个方法。没有 DeepCell 时，Mesmer 置灰并写明原因。每个字段在输入时就校验，红字表示被拒绝，黄字只提示「去掉了重复值」、不阻止保存；组合数实时显示。
  - `method_blocks.py`：
    - 方法块：标题可换行，组合数单独一行，下面是摘要，右侧是 `Edit` 和 `×`；
    - 总任务数；
    - R9 合并：选 Yes 取并集，单值冲突用 `ConflictDialog` 选择；选 No 取消这次添加（验收后已改为保留成单独一块，见下）。
  - `plan_store.py`：方案文件放在 `segmentation_search_plans/`，另有 `index.json`，都是原子写入。patch 按永久编号记录，加载时忽略已经不存在的 patch 和隐藏的方法，并告诉用户忽略了几个。
  - `patches_panel.set_selected_ids`：加载方案时恢复勾选状态。
  - `ui/main_window.py`：负责装配，以及 Save plan、Load plan 的处理。
  - `UI_SURFACE_RULES.md` 补充了 Methods 部分的说明。
- **离屏截图发现并修正的问题**（`grab()` 是离屏渲染，**不是物理屏幕截图**）：
  - 方法块标题和「Methods」字号过大，而且标题和组合数互相挤压、被截断：已统一字号，组合数移到单独一行；
  - 按钮默认最小宽度为 80，导致左栏变宽（191→212，由宽度门发现）：`+` 单独放一行，`Edit` 设了最小宽度，组合数让出宽度；
  - 弹窗里的 `Method:` 标签宽度为 0（两个 QFormLayout 各自计算标签列宽）：改成普通的横向行；
  - 提示文字里出现了内部编号「(plan P2)」：改成直白的说明。
- **测试**：
  - `tests/test_step1_method_plan.py` 共 20 条；
  - 在 HEAD 导出树上会报收集错误；
  - 五处反向注入都会让测试变红：合并时不取并集、选 No 仍然合并、小数位不校验、不忽略已不存在的 patch、Mesmer 不置灰；
  - A1 的页面顺序测试改为 Patches → Methods → 旧控件。
- 按 R4，任务数超过 10 时的提醒放在块 C 点 Run 的时候做。
- **全量回归**（2026-09-24，共 174 个模块）：3808 passed / 17 failed / 1 skipped。16 条与基线相同。另外 1 条是 `test_step0_compare_tiles::test_hot_requests_never_outrank_the_foreground`（「no foreground request」，属于时序问题）：把整个模块单独重跑，当前代码 3/3、HEAD 3/3 都是 190 条全部通过，而且块 B 没有碰 Step0 的分块读取，判定为**偶发**失败，不是本块引入的。
- **验收后修改**（2026-09-24，用户「验收总体通过」后提出三点）：
  - 同名合并选 No：**保留**这张新的方法卡，单独成块，不再取消（R9 已同步改写）；
  - `Edit`：弹窗里的方法下拉框可以更换方法；换成已有的方法时，同样询问是否合并，选 Yes 就并进已有的那块，本块去掉；
  - `Save plan`：保存**全部** patch（编号、名称、坐标和大小），以及哪些被勾选；
  - `Load plan`：先问是否恢复 patch。当前没有 patch 就直接恢复；有的话再问「覆盖当前 patch」还是「共存」。共存时，编号已存在的 patch 跳过；名称重复的改名，并告诉用户改了哪些。恢复的 patch 保留原编号（按此复验通过；`overview_panel.restore_patch_records`，一次编辑、一次发布），勾选等 Step0 发布后再加上。
  - 测试增加到 24 条（含覆盖、共存、不恢复三种加载流程，都在已发布 handoff 的窗口里跑）；七处反向注入都会变红：选 No 仍取消、Edit 锁住方法、只存勾选的 patch、覆盖不删旧 patch、重名不改、编号重复、勾选不等发布；相关 7 个模块全部通过。
  - **修改后全量回归**（2026-09-24，174 个模块，运行期间代码冻结并核对哈希）：3813 passed / 16 failed / 1 skipped，16 条与基线逐条相同，无新增失败。

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

**C 申请与批准**（2026-09-24 用户批准；和 V1 合并执行，新流程走 `seg_runner` 子进程，旧 worker 不改）：
- **分四步，每步单独测试、验收、提交**：
  1. **抽共用零件（纯搬迁，不改行为）**：把 Step2 生效的归属与重编号内联代码（`segment_merge_worker.py:2538-2575`）原样抽成 `core/label_ownership.py`；门：同一张 label 图、同一个 own_bbox 上，与内联代码的保留集合和新标签完全一致。Step2 改为调用放到 V2。
  2. **补全引擎**：`seg_runner` 加 expansion（保留扩张前的核）、Mesmer `postprocess_mask`、nuclear-guided 回传核；Mesmer 模型路径改为读模型清单，缺失时明确报错、不退回本机绝对路径；门：与同环境直接调用逐像素相同。
  3. **后台运行流程（无界面）**：按 7.3、7.4 冻结 `run.json`（patch、组合、fusion 快照、来源和 pixel_key、HALO，HALO 与 Step2 overlap 同值，默认 200）；按 7.10 的归属表构造输入；引擎串行；按 7.3 原子发布；Stop 记 cancelled、崩溃记 failed、关闭无残留；核权重为 0 或 DAPI 无 committed 窗口时拒绝运行。
  4. **界面接入**：Methods 区总任务数下方放 Run / Stop 和进度；超过 10 个任务先提示（R4）；结果列表（每个组合一行：状态、细胞数、失败数、「选用」），过期可看不可选（7.7）；修掉结果到达自动设为当前参数（现有缺陷 1）；选定后按现有格式写参数文件。
- **用户裁定（a）**：块 C 不做图像结果视图，只有结果列表；轮廓显示留给块 D。旧结果网格不改造。
- **新增可见界面已授权**：Run、Stop、进度、结果列表、选用按钮，都在 Pre-segmentation 页。
- **拟新增**：`core/label_ownership.py`、`core/preseg_input.py`、`core/preseg_run.py`、`ui/step1_presegmentation/run_job.py`、`ui/step1_presegmentation/results_panel.py` 及测试。**拟修改**：`seg_runner/engines.py`、`runner.py`、`client.py`、`envs/fusion_mesmer/models.json`、`ui/main_window.py`、`method_blocks.py`、`UI_SURFACE_RULES.md`、本文档。
- **不改**：Step2 全部代码、`workers/cellpose_worker.py`、`workers/mesmer_worker.py`、fusion 算法、viewer / 调度器 / 缓存。
- **测试环境**：默认环境没有 deepcell，Mesmer 相关测试另用 `fusion_mesmer` 跑。

**C 执行记录**（2026-09-24 启动）：
- **第 1 步：抽共用零件（完成，待提交）**
  - `core/label_ownership.py`：`centroids`、`kept_labels`、`ownership_lut`、`apply_ownership`（共享标签的第二张 mask 走同一张 LUT，超出主输出最大标签的置 0），从 Step2 生效的内联代码原样搬出；Step2 没改。
  - 门：`tests/test_label_ownership.py` 让 **Step2 真实的 `_segment_one_zarr`** 在合成 fused.zarr 上跑（模型换成固定的标注函数），它写出的全局 mask 与用本模块逐块归属、按 Step2 同样方式粘贴的结果逐像素相同（4 种切块和 overlap）；质心与 `_centroids_vectorised` 逐元素相同。8 条测试；把半开区间改成闭区间，3 条变红。
- **第 2 步：补全引擎（完成，待提交）**
  - `seg_runner/engines.py`：
    - expansion 两个方法输出细胞和核：核是扩张前的预测，细胞是 `expand_labels(核, expand_distance)`，距离 0 时细胞等于核；
    - Mesmer：列表阈值按 P2 进入主输出的 `postprocess_kwargs_*`（whole-cell、nuclear-guided 的细胞；nuclei 的核），nuclear-guided 的副核用库默认值；`postprocess_min_size` 只作用于主输出，和现有 Step1 worker 一致；不传 `preprocess_kwargs`；
    - Mesmer 模型路径改为读模型清单（可被清单里写明的环境变量覆盖），逐个核对文件存在和大小，缺失或不符明确报错，不再退回写死的路径；
    - `postprocess_mask` 在 runner 里有一份拷贝（runner 不导入主程序），测试保证与 `utils/mesmer_utils.postprocess_mask` 相同，Step2 改用 runner（V2）后只剩一份。
  - `tests/test_seg_runner_engines.py` 共 10 条：两种 expansion 在子进程里与直接调用逐像素相同；阈值到达 `app.predict` 的 kwargs（不需要 DeepCell 的替身测试）；真实 Mesmer 上阈值改变了结果，且子进程与直接调用相同；模型清单的路径与核对。`fusion_test2` 20 passed / 3 skipped（Mesmer），`fusion_mesmer` 23 passed（含原 `test_seg_runner.py`）。四处反向注入都变红：核被扩张结果覆盖、阈值不传、副核也做后处理、清单路径失效时退回别处。
  - **与 P1 裁定的出入（2026-09-24 用户同意）**：P1 原写「块 C 在 `mesmer_utils.run_mesmer_prediction` 和 `mesmer_worker` 的 Step1、Step2 两条路径里透传阈值」。按本块批准的范围（不改 Step2、不改旧 worker），新流程的阈值在 runner 里透传；Step2 的透传随 Step2 改用同一个 runner（V2）一起做，旧路径不加。
- **第 3 步：后台运行流程（完成，待提交）**
  - `core/config_hash.py`：规范化哈希从 `MainWindow._step1_config_hash` 搬出，窗口改为调用它；测试用搬迁前算出的 6 个摘要把两者钉住。
  - `core/preseg_input.py`：读取范围（patch ± HALO，与分析区域取交集，不补边）、`fuse_fullres` 融合成 uint16、多边形置 0、÷65535、按方法组装输入（7.11.4 的表）；核通道没设、核权重为 0、核通道没有已确认的显示窗口时拒绝。
    - **实测发现**：`cv2.fillPoly` 在画布边缘会裁剪多边形，逐个窗口栅格化时边缘有少量像素和 Step2 不同（测试里 53 个像素）。改为 `RoiMask`：和 Step2 一样在整个 ROI bbox 上栅格化一次，存成按位压缩的数组，再按窗口切出；测试与 Step2 的 `_poly_mask` 逐像素相同。内存和 Step2 写 fused.zarr 时相同（短暂地每个 ROI 像素 1 字节）。
  - `core/preseg_run.py`：`run_id`、任务展开（组合 × patch）、`run.json`、结果记录（先 mask 后记录，原子替换）、`pixel_key`（结构化身份，7.4 的「必须变 / 必须不变」各项都有测试）、选定资格（7.7 的五条）和过期判断。
    - **设计选择**：两个方法块列出了同一个组合时，每个 patch 只跑一次（同样的方法、参数和像素，结果文件名也相同），按第一次出现的顺序。
  - `ui/step1_presegmentation/run_job.py`：普通线程，不依赖 Qt。引擎按 Cellpose → StarDist → Mesmer 串行；同一 patch 同一种输入只准备一次，各组合共用；每个任务收到结果后按共用归属函数保留中央区域（expansion 的核跟随细胞用同一张 LUT，nuclear-guided 各自归属、`paired: false`），裁成 patch 大小再发布；每个任务正好一条记录。Stop：正在跑和后面的任务记为已取消，后面的引擎不再启动；引擎起不来：它的任务记为失败并写明原因，其他引擎照常；patch 在分析区域外：只有这个 patch 失败。
  - `tests/test_preseg_run.py` 共 17 条，其中端到端用真实 StarDist 进程，与手工逐步计算逐像素相同。`fusion_test2`、`fusion_mesmer` 都 17 passed。另在 `fusion_mesmer` 里实跑 Cellpose whole-cell + Mesmer nuclear-guided（两个阈值）×2 个 patch：全部 ok，Cellpose 用 GPU、Mesmer 用 CPU，run 目录只剩 run.json、records、masks 和引擎日志。
  - 反向注入：HALO 置 0、跳过归属、expansion 不共用 LUT、Stop 不设停止标记（第一次没测出，已加强测试：Stop 后下一个引擎不得启动）、pixel_key 混入 patch 数、忽略已取消、Mesmer nuclei 第二通道不为 0，都会变红。
- **第 3 步已提交**：`3f179ca`。
- **第 4 步：界面接入（完成，待真机验收）**
  - `method_blocks.py`：总任务数下方加 `Run`、`Stop` 和进度行；运行中 Run 禁用、Stop 可用（不再静默返回）。
  - `results_panel.py`（新）：`Results` 标题、「正在使用」一行、每个组合一行（方法、参数、`k/n patches`、细胞数、失败 / 取消数、`out of date`、`Use` 按钮）；不能选时原因写在行里。
  - `ui/main_window.py`：
    - Run：要求已保存的 Fusion 设置，按 `check_fusion` 拒绝；超过 10 个任务先问（R4）；冻结 run（HALO 暂取 Step2 Tile Grid 的默认 overlap 200，与 Step2 设置本身绑定放到块 E）；运行用自己的 loader，在后台线程里；记录经 Qt 信号回到界面线程。
    - Use：按 7.7 判断；有失败时先问；选中后 `_p2_params` 是该组合自己的方法和参数，带 `fusion_settings_hash`、`pixel_key`、`preseg_run_id`、`combo_id`；Mesmer 另写 `normalize_input: false` 和 `threshold_target`。Save 写参数文件时不再混入旧面板的参数。
    - 过期：Step0 发布或保存新的 Fusion 设置后重新判断；选中的结果过期就取消选中、Save 重新禁用；Save 时再核对一次（记录或文件缺失、过期都拒绝）。
    - 现有缺陷 1 修掉：旧流程里结果到达不再成为当前参数；结束后的自动选中只选 Phase 1 的列（它只是把直径交给 Phase 2）。
    - 关闭窗口、切换数据集时结束运行，不留引擎进程。
  - `UI_SURFACE_RULES.md` 同步。离屏截图（`grab()`，**不是物理屏幕截图**）检查了布局：左栏宽度不变；`Results` 标题字号改成和 `Methods` 一致。
  - `tests/test_step1_preseg_run_ui.py` 共 9 条（真实 StarDist 进程）；八处反向注入都变红：结果到达自动成为参数、自动选中 Phase 2 列、过期不取消选中、Save 不再核对、超过 10 个不问、关窗不结束运行、运行中 Run 仍可点、结束后自动选用。
  - 布局顺序测试更新为 Patches → Methods → Results → 旧控件。
  - **全量回归**（2026-09-24，178 个模块，运行期间代码冻结并核对哈希）：3854 passed / 17 failed / 3 skipped。16 条与基线逐条相同；第 17 条是已知偶发的 `test_step0_compare_tiles::test_hot_requests_never_outrank_the_foreground`，整个模块单独重跑 3/3 全部通过。
  - **真机验收**：用户人工测试通过（2026-09-24）。
  - **验收后修改**（用户要求）：Step1 的 `Save Fusion settings` 和 `Save plan` 保存成功后弹窗说明（文件位置；方案里有几个方法、几个 patch）；失败时原有的警告不变；只有按钮会弹窗，代码内部的保存不弹。新增 1 条测试；相关 6 个模块通过；按新的测试规则不再跑全量。
- **测试规则（用户裁定，2026-09-24）**：每一步只跑相关测试；全量回归只在一个块收尾、或某一步大改共用文件时跑，并行 4 组。

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

**D 申请与批准**（2026-09-24 用户批准）：
- **分三步，每步单独测试、验收、提交**：① 画布与底图（按行装箱布局、分隔线、P 编号、点击选中不移动、统一缩放平移、双击/F 适配；底图走 7.6 的复用接口，只调用不修改；按画布缩放选层级；读取和合成在 montage 自己的线程里）；② 轮廓图层与控制栏（每个组合一行：细胞/核开关、颜色、线宽、实线/虚线、状态与进度；全开/全关分细胞和核；按 4.5 置灰；失败和零细胞标注；默认细胞实线、核虚线、同组合同色、线宽按屏幕像素；矢量 cosmetic QPen，路径粒度按实测）；③ 真实切片只读实测性能（patch 数、组合数、细胞数、帧时间；不达标先报告瓶颈与取舍）和释放门、viewer 与 montage 同时读取的实测；最后一次全量回归（并行 4 组）。
- **用户裁定**：
  - 结果视图放在右侧**新标签页**「Pre-seg Results」，旧「Patch Results」不动，块 E 一起退场；
  - `Use` 只留在左侧结果列表；
  - 控制栏是标签页右侧**可收起的侧栏**（约 220 像素，默认展开），不做悬浮窗；行的顺序和名称与左侧结果列表一一对应。
- **授权项（AGENTS.md 第 4 条）**：两层缓存（通道块 1 GiB，合成结果 256 MiB，归 montage 所有）、一个 montage 工作线程（最新请求优先）、释放时机按 7.6（切换数据集或 pixel_key 变化、离开 Step1、关窗：线程退出、缓存清空、无迟到回调；取消勾选的 patch 只清它的；新的 Run 只清轮廓；Channels 或 Intensity 变化只清合成层）。
- **显示哪些 patch**：有运行时显示该次运行冻结的 patch；还没运行时显示当前勾选的 patch（只有底图）。
- **拟新增**：`ui/step1_presegmentation/montage_view.py`、`mask_layers.py`、`montage_supply.py` 及测试。**拟修改**：`ui/main_window.py`（装配、结果回调、释放时机）、`UI_SURFACE_RULES.md`、本文档。**不改**：viewer、scheduler、已有缓存层、Step2、分割流程；需要改就停下申请。
- **画面证据**：只用离屏截图，不在用户的 DISPLAY 上弹测试窗口，最终画面以用户真机验收为准。

**D 执行记录**（2026-09-24 启动）：
- **第 1 步：画布与底图（完成，2026-09-25 真机验收通过）**
  - `montage_view.py`：`pack_rows` 按行装箱（顺序和尺寸不变、互不重叠、间隙为中位边长的 6%，至少 16）；布局表与命中判定（间隙不属于任何 patch）；画布单位是 level-0 像素，所以之后的 level-0 mask 和任何层级的底图天然对齐；分隔框和选中框用 cosmetic 笔；P 名称是独立的 `TextItem`，不随缩放；双击或 F 适配全部；点击只选中、视图不动；按画布缩放用 `pick_display_level` 选层级（120 ms 去抖）。
  - `montage_supply.py`：`build_spec` 由页面在界面线程取好再交出；`spec_channels` 调 `overlay_channels` / `fusion_channels`；像素调 Step1 provider 的 `read_region`（不经过 scheduler）；合成调 `step1_compose.compose`；两层按字节计的 LRU（通道块 1 GiB、合成结果 256 MiB）；一个工作线程，新的请求取代所有还没开始的；过时的代计数结果不报告；缺显示窗口的通道经 `missing` 交给页面调用 `request_mapping_seed`，缺窗口时的半成品不进缓存；`close()` 结束线程、清空两层缓存，之后不再报告。
  - `ui/main_window.py`：右侧新增「Pre-seg Results」标签页；没有运行时显示勾选的 patch，Run 开始后显示那次运行冻结的 patch；只在 Step1、标签页可见时合成；勾选、颜色、显示窗口、fusion 草稿、Overlay/Fusion 模式变化后只清合成层再合成；取消勾选的 patch 清掉它的缓存；`pixel_key` 变化清空全部；离开 Step1、切换数据集、关闭窗口都结束线程并清空缓存。
  - 右侧标签页的约定测试（4 个模块）和 `UI_SURFACE_RULES.md` 改为三个标签页。
  - `tests/test_step1_montage_view.py` 共 12 条：底图与用同一条链手工合成的结果逐像素相同（Overlay 和 Fusion 各一，跨过 NaN 边界）；改 Intensity 或模式不重新读盘；释放规则；关闭后没有迟到的结果。七处反向注入都变红（通道缓存失效、取消勾选不清、关闭不清、层级恒为 0、离开 Step1 不释放、不跟随设置变化、点击移动视图）；其中「不跟随设置变化」第一次没测出——测试里重新适配后的层级检查顺带重画了一次，已让测试先等它结束。
  - 离屏截图（**不是物理屏幕截图**）：三个大小不一的 patch 按行排开，名称在左上角，选中的 patch 有黄框。
  - **真机验收通过后的修改**（用户 2026-09-24）：
    - 画布上方加 `Overlay` / `Fusion` 两个按钮，和 Viewer 的两个按钮是同一个命令，互相同步；
    - 双击某个 patch 让它单独占满画布，再双击（或双击间隙）回到全部；F 仍是全部；
    - **卡顿**（启用新通道、调 Intensity）：合成数据上复现不出（8 个 1024 px patch，读图加合成 66 ms，界面线程最长停顿 1 ms）；真实切片只读实测原始通道读 4 个通道：level 0 为 256 ms，level 1 为 11 ms，不是瓶颈；嫌疑是 corrected 通道没有粗层平面时要从 level 0 归约，以及同一时刻其他界面部分的重算。已做：按屏幕精度合成（`stride`，从层级数据里隔点取样，不比屏幕更细）；只合成屏幕上看得见的 patch，近中心的先做，其余进入画面时再做；每次请求结束在终端打印一行 `[Montage] … reads … ms, compose … ms, … total … ms`，真机复测时据此定位。
    - 第二个工作线程：实测 8 个 patch 合成 52 ms → 27 ms；**用户 2026-09-25 同意**，已改为两个。
  - **真机复测（2026-09-25）**：用户贴出的日志是调 Intensity 时的，每次 4 个 patch（level 0、stride 1），0 次读盘，合成 100–160 ms。用户指出 Intensity 要等拖动停顿才画——这是我加的 80 ms 去抖计时器：每次变化都重新计时，拖动时永远不触发。改为**一帧接一帧**：没有正在画的帧就立刻画；有的话记下「有新变化」，这一帧一上屏就按最新设置画下一帧；拖动期间按两倍 stride（像素数四分之一）画，静止 150 ms 后画一次全精度；拖动时不打印耗时行。合成数据上模拟一秒拖动：拖动期间持续出帧（约每 17 ms 一帧），第一帧 0.2 s 内，全精度在停下后 1 s 内。
  - 画布关掉自动范围、图像不参与范围计算——**这不是卡顿的原因**（反向注入去掉它测试照样通过，`setRange` 本身已关掉自动范围），只是保护；之前测试里拖动中出现的全精度请求，实为测试窗口尺寸还在变化引起的层级检查，已让测试先等窗口稳定。
  - 反向注入：恢复去抖（两条变红）、帧上屏后不补画最新设置（一条变红）。
  - **启用新通道仍卡**：用户的日志里没有那一次的数据；等用户贴出启用新通道时的 `[Montage]` 行再定位（嫌疑：corrected 通道没有粗层平面时从 level 0 归约）。
  - 如果 Intensity 在真机上仍不够流畅，下一步是改用 Step1 viewer 的 GPU 显示层（`Step1GpuLayer`，只调用不修改）：原始像素一次上传到显卡，改 Intensity 只改参数、每帧重画。它是覆盖在画布上的独立 GL 窗口，会盖住 patch 名称、选中框和第 2 步的轮廓，需要另外设计叠放，并新增显存缓存，**要先征得用户同意**。
  - **真机复测（2026-09-25）**：卡顿解决，用户验收通过；但启用/关闭通道和调 Intensity 时整个画面细微抖动。原因（我的修复引入）：① 拖动时半精度、停下后全精度，来回切换，清晰度跳变；② 半精度图边长向上取整后拉伸回原尺寸，边长不能被 2 整除时比例差一点点，最远处偏差约 1 像素，每次切换都错位。Step0/Step1 不抖，是因为 viewer 用 GPU 显示层按屏幕精度从同一份原始数据每帧重画，没有过渡。
  - **用户裁定（2026-09-25）：直接复用 Step1 的成像机制**，全部同意：
    - 复用 `Step1GpuLayer`（着色器、Overlay/Fusion 合成）和 `build_spec`，只调用不修改；viewer 的 `Step1GpuBinding` 按整张切片的调度器设计，不适合多个 patch 的画布，montage 自己供原始平面（只供数据，不合成）；画布坐标就是显示层的世界坐标。
    - 新增 montage 自己的显存缓存，上限 512 MiB（与 viewer 相同），离开 Step1、切换数据集、关闭窗口时释放。
    - GL 显示层盖在画布最上面，patch 名称、选中框和第 2 步的轮廓画在它上面一层透明的绘制层里，鼠标穿透到下面的画布。
    - 显卡不可用时退回 CPU 合成（去掉半精度过渡，不抖但慢），终端打印原因，与 viewer 的处理一致。
    - 去掉 CPU 合成结果缓存和半精度过渡；原始通道缓存保留，只存裁切后的数据。
  - **超大 patch 与 512 MiB**（用户问）：与 viewer 同样两层——每个 patch 一块整块的粗平面（边长有上限），再加一块只覆盖屏幕可见部分的细平面；每次提交前按字节算总量，超过预算就这一帧改用更粗的层级，所以用量取决于屏幕和通道数，与 patch 大小无关；显示层自己也在超过上限时拒绝提交，不会溢出。
  - **GPU 复用的实施**：
    - `montage_gpu.py`（新）：`display_snapshot`（与 viewer mount 的 `_gpu_display_snapshot` 是同一份拷贝，测试逐项相等）；`plan_planes`（粗平面整块、边长上限 1024 并在总量超过 128 MiB 时减半；细平面只覆盖可见部分，对齐到细层级的 256 像素块，总量超过 256 MiB 时这一帧退到更粗的层级）；平面在画布上的位置按它实际读到的像素范围算，不拉伸到 patch 边框；`read_key`（像素）与 `identity`（像素加画布位置）分开——patch 在画布上移动时 GPU 层不会报「同一身份不同几何」，也不重新读盘；`build_layer` 与 mount 一样当场强制初始化，失败给出原因。
    - `montage_supply.py`：新增按平面读取（只读、不合成），存进原有的 1 GiB 通道缓存；CPU 合成保留为退回路径。
    - `montage_view.py`：边框、名称、选中框从场景移到 `MontageOverlay`（盖在 GL 层上面的透明窗口，鼠标穿透，坐标经同一个 ViewBox 换算）。
    - `ui/main_window.py`：supply 建立时尝试 GPU 层，失败打印原因并用 CPU 画面；设置变化直接规划并提交（同步，一次提交）；平面到达时同一轮事件合并成一次提交；相机移动时每次都从显卡重画，细平面最多每 60 ms 重新规划一次；提交失败打印原因并退回 CPU；离开 Step1、切换数据集、关闭窗口时 `dispose` 掉 GL 名称和纹理。CPU 退回路径去掉半精度过渡。
    - 离屏测试可以用真实的 RTX 4090（EGL，不在用户的 DISPLAY 上弹窗）。`tests/test_step1_montage_view.py` 共 26 条：CPU 退回会说明原因；快照与 viewer 相同；超大 patch 的预算；平面精确落位；调 Intensity 时同步提交、不读盘、画面变化；勾选从未读过的通道时先读再画；叠放顺序；移动不报错不重读；释放。七处反向注入六处变红，没变红的一处是「设置变化时直接走 GPU 分支」——跳过它后另一条路径同样同步提交，行为相同。
    - 离屏截图（`grab()`，**不是物理屏幕截图**）：GPU 画面，名称、边框和黄色选中框在上面。

- **第 1 步已提交**：`4cdcaff`（2026-09-25）。
- **第 2 步：轮廓图层与控制（用户裁定 2026-09-25）**：
  - 控制**合并进左侧结果列表的每个组合框**，不做右侧侧栏：每个框新增一行「Cells / Nuclei 开关、颜色块、▾ 菜单（线宽 1 / 1.5 / 2 / 3 像素，细胞线、核线各自实线或虚线）」；Results 标题下加细胞、核各自的 All / None；方法没有的那种 mask 开关置灰；左栏宽度门继续把关。
  - 轮廓默认全关；按组合顺序用固定配色；缩得很小（细胞中位直径不足约 3 个屏幕像素）时只画边框，放大后自动出现。
  - 轮廓在 montage 已授权的两个工作线程里提取（不新增线程），画在 GL 层上面的透明层里（cosmetic 笔，线宽按屏幕像素）；失败的 patch 角上标 `failed`，成功但零细胞标 `0 cells`；新的 Run 只清轮廓，底图保留。
  - **执行记录（完成，2026-09-25 真机验收通过，含 Membrane / 核通道开关与按钮外观）**：
    - `mask_layers.py`（新）：`outlines`（按 `find_objects` 逐个标签取外轮廓，点在边界像素中心，给出细胞数和中位直径）；`qpath`（每个多边形闭合、互不相连，一条路径）；固定配色、线宽档、默认样式。
    - `montage_supply.py`：描轮廓任务排在读平面之后，在原有两个线程里做；新的 Run 清空轮廓；关闭时清空。
    - `results_panel.py`：每个框新增一行 Cells / Nuclei（按方法输出置灰）、颜色块（`QColorDialog`）、▾ 菜单（线宽、细胞线虚线、核线虚线）；标题下 Cells / Nuclei 的 All / None（置灰的不动）；组合多时框不再被压扁，改为可滚动（离屏截图发现：矮的标签页会把框压到不可读）。
    - `montage_view.py`：透明层按每个组合的样式画轮廓（cosmetic 笔、虚线），细胞中位直径不足 3 个屏幕像素时不画；`failed` / `0 cells` 标签；路径按布局缓存。**测试发现并修正**：`mapFromScene` 把坐标取整，用 (0,0)、(1,1) 推缩放时误差被放大，轮廓整体缩放偏了（最多约 15 像素）；改为浮点换算，缩放取视图两个对角。
    - `ui/main_window.py`：新的 Run 调 `_start_results_for_run`（框、清轮廓，底图保留）；记录到达时失败立刻标注，成功的交给后台描轮廓，描好即画；montage 没有 supply 时到达的记录在它出现后补上；框里的样式变化直接送到画布。
    - `tests/test_step1_montage_outlines.py` 共 10 条；七处反向注入六处变红，没变红的一处是把坐标换算改回取整——缩放已改用视图对角推算，取整只让边框和名称偏不到 1 像素。相关 9 个模块并行 4 组全部通过。
    - 离屏截图（**不是物理屏幕截图**）：GPU 底图上，细胞实线、核虚线同色，`failed` 标签在角上；左侧框的开关行、颜色块和 ▾；10 个组合时可滚动、每个框完整高度。
    - **真机验收通过后的修改**（用户 2026-09-25）：Fusion 模式下画布上方加 `Fusion signal` 和核通道（显示通道名，如 `DAPI`）两个开关，默认都开，只在 Fusion 模式显示；关掉哪个，结果视图这一帧就不画哪个——只改 `build_spec` 结果的一份拷贝（`groups` 置空 / `nucleus` 置为 `("", 0.0)`），Fusion 设置、分割数据和 Viewer 都不受影响（测试核对模型不变）；由 GPU 当场重画。新增 2 条测试，两处反向注入都变红。
    - **外观（用户 2026-09-25）**：montage 的 Overlay / Fusion 与 Viewer 的两个按钮用同一份样式（抽出为 `ui/step1_button_styles.MODE_BUTTON_QSS`，Viewer 改为引用它，字符串不变）；`Fusion signal` 改名 `Membrane`；Membrane / 核通道开关与模式按钮同形状、字号、边框和圆角，按下时分别亮成 Fusion 画面里该层的颜色（细胞质红、核蓝），弹起时暗底灰字——风格一致，但不与模式按钮重复。

- **第 2 步已提交**：`9cab8cc`（2026-09-25）。
- **第 3 步：真实切片实测与收尾（2026-09-25）**
  - 只读：`RawTileProvider` 读真实 OME-TIFF（59040×35520，29 通道，层级 1/4/16/64），脚本放在 scratchpad，不写真实项目，`cufile.log` 不变；GPU 为离屏 EGL 下的 RTX 4090。
  - **底图**（13 个 patch：8 个 1024 px、4 个 2048 px、1 个 10000 px；4 通道）：第一块平面 160 ms 到达，全部 300 ms；首次上传纹理的一次提交 184 ms；**拖 Intensity 50 次：每帧中位 1.9 ms、最慢 4.4 ms、不读盘**；切到 Fusion 4.9 ms；放大到 10000 px 的 patch 后平移：每帧中位 5.7 ms、最慢 12 ms。显存峰值 121 MiB / 512 MiB，内存平面缓存 156 MiB / 1 GiB。三个线程模拟 viewer 与 montage 同时读同一张切片：无错误。
  - **轮廓**（8 个 1024 px patch，每个 1521 个细胞，3 个组合 × 细胞加核 = 48 层、约 72000 个轮廓）：提取每个 mask 约 36 ms（后台）。**瓶颈**：① 路径在界面线程里生成，每条 18 ms，一次打开 48 层时首帧卡约 0.9 s；② 每帧重画 98 ms（全部实线也要 94 ms，虚线不是主因，负担是点数与数量）。按用户裁定（2026-09-25）做 ① ②：
    - ① 路径改在 montage 已授权的两个线程里生成（`request_path` / `path_ready`），按 patch 自己的像素保存，画时平移到画布位置——换位置不重建；
    - ② 按屏幕精度分档简化（`lod_bucket`：误差不超过半个屏幕像素；Douglas-Peucker），缩放后需要的那档还没好时先用最近的一档，好了自动换上；
    - 复测：一次打开 48 层时界面线程最长一帧 65 ms（原约 0.9 s）；显示全部时每帧 55 ms（原 98 ms）；放大到一个 patch 每帧 23 ms（原 28 ms）。这是最重的情形；平时开 1–2 个组合约 10–20 ms。③（移动时用快照）、④（GPU 画轮廓）暂不做。
  - 测试：`tests/test_step1_montage_outlines.py` 增至 13 条（简化误差、路径在后台线程生成、缩小时要更简的一档并先用最近的）；三处反向注入都变红。
  - **真机验收通过（2026-09-25）后的修改**：Results 标题下的 All / None 按钮改为 `Cells`、`Nuclei` 两个勾选框（用户裁定）：勾选＝所有框都显示这一种，不勾＝都不显示，默认不勾；各框不一致时显示半勾，从半勾点一下变为全部显示；没有任何组合能产生这种 mask 时置灰。新增 1 条测试，两处反向注入都变红。
  - 结果列表每个组合框加上与 Methods 方法块相同的灰色矩形边框（用户 2026-09-25）：边框样式抽成 `method_blocks.block_frame_qss`，两处共用；「In use」时该框边框为绿色。新增 1 条测试。
  - **块 D 验收通过（2026-09-25）**。
  - **分区（用户 2026-09-25，方案 A）**：Pre-segmentation 标签页的三部分各装进一个带标题的框——`Patches`（新加的标题）、`Methods`、`Results`——样式与下方旧控件的「Segmentation Method」框相同（`ui/step1_button_styles.SECTION_BOX_QSS`，测试核对与 `search_ctrl.py` 的字符串一致）；Methods、Results 原来写在里面的标题去掉；面板内边距相应缩小，每侧总边距不变，左栏宽度门通过。布局顺序测试改为检查三个框。
  - **块 D 收尾全量回归**（2026-09-25，180 个模块，并行 4 组，代码冻结并核对哈希与 HEAD）：3897 passed / 17 failed / 3 skipped。16 条与基线逐条相同；第 17 条 `test_step0_channel_conditioning::test_loaded_channel_switch_is_cache_hit`（基线记录的偶发项）：单独各跑 10 次，当前 8/10、块 D 之前的 `dcf6244` 同样 8/10 通过——既有的偶发失败，与块 D 无关（它测的是 Step0 通道调节，块 D 没有触及）。

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

**E 申请与批准**（2026-09-25 用户批准第 1–3 项）：
1. 旧的 Phase1/Phase2 面板（`self.search`）和右侧「Patch Results」标签页**不再显示、不删除**（与 R2 处理 HQ/HQ2/CDS 的方式一致）；
2. **Save 只认预分割「Use」选定的结果**（4.7「未选则 Save 禁用」）：旧面板和旧会话里的旧参数都不能解锁 Save，要重新预分割再选定；
3. 交接验收：真实窗口的 Use → `_save` → `_go_to_step2` → `get_seg_config()`，8 个方法各一次（只核对参数，不需要引擎，Mesmer 也覆盖）。
- 白名单：`ui/main_window.py`（旧面板和 Patch Results 的可见性、Save 的解锁条件和 `_save` 入口的拒绝）、`UI_SURFACE_RULES.md`、测试、本文档。`search_ctrl.py`、`result_grid.py` 不需要改（在容器层隐藏）；Step2 装载实测一致，`step2_page.py` 不需要改。

**E 执行记录**（2026-09-25，真机验收通过）：
- `ui/main_window.py`：新增 `_save_allowed()`（有 `Use` 选定的结果才为真），`_check_save_unlock`、`_unlock_ui` 和 `_save` 入口都只认它；`_save` 被拒时提示「Choose a pre-segmentation result first: Pre-segmentation tab → Results → Use.」。旧面板的滚动区 `setVisible(False)`；Patch Results 标签页 `setTabVisible(False)`；旧的 `_show_step1_patch_results_tab` 调用在标签页隐藏时不做任何事。
- 新增 `tests/test_step1_step2_handoff_e2e.py` 10 条：8 个方法走真实的 Use → `_save`（写出真实参数文件，fusion 作业开始前停下）→ `_go_to_step2`，Step2 不作编辑时 `get_seg_config()` 通过契约核对、没有不一致，`runner_params` 正好是所选组合加方法的固定规则；旧 Phase 2 参数不能解锁 Save；旧面板和 Patch Results 标签页不再显示、仍保留。
- 更新因旧界面退场而失效的 12 条测试：Save 相关的 guard 测试（未保存设置、显示映射提交、重绑、运行写入的快照）改为经「选定的结果」到达 Save，原有断言不变；「Phase 2 参数在别的设置上搜出来」那条改为断言 Save 直接拒绝；旧面板 Save 的文件钉住测试改为断言拒绝、不写文件；两条 Patch Results 标签页测试和一条 720p 布局测试改为断言标签页和旧面板不显示。
- 回归（Step1 全部 55 个模块 + 契约 + 界面约定，逐模块单独进程）：与已提交的 HEAD 逐条对比，没有新增失败。仍失败的都在 HEAD 上同样失败：`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`（字体差异）；`test_step1_montage_view.py` 在第 27 条后 Qt 异常中止（本机 WSL 的 GPU/EGL，HEAD 同样）；5 个 GPU 模块在本机不收集测试。
- **真机验收通过（用户 2026-09-25）**：「预分割 → 选定 → Save → Step2 全量分割」（Mesmer 除外，本机无模型）。
- 提交：`7ee98fc`。

### 块 L — Step1 Save 进度框与 Step2 布局（计划外；用户 2026-09-25 提出并批准）
- **L1**：Step1 的 Save 生成 fused.zarr 时，模态进度弹窗和页面底部的旧进度条同时出现。用户裁定只保留弹窗（它有 Cancel）；底部进度条保留但不再显示，它的文字改为打到终端（前缀 `[Step1-Fusion]`），完成和出错仍各有消息框。
- **L2**：Step2 的参数面板放到左栏，宽度与 Step0、Step1 的通道列共用一份（三页的分隔条联动）；Tile Status Overview 和进度放到右栏；Step2 不显示全局 Channels 组件（框和组件保留，只是隐藏）。
- **白名单**：`ui/main_window.py`（底部进度条的显示；通道列同步机制加入 Step2 的分隔条，同步的接线挪到 Step2 建好之后）、`ui/step2_page.py`（两栏摆放、Channels 框隐藏、参数行和提示文字的显示方式、改名）、`UI_SURFACE_RULES.md`、测试。不改 Step2 的运行逻辑、控件的取值和含义、Step0/Step1 的布局、Step3。
- **实施中的停止与裁定**（均为用户 2026-09-25 裁定）：
  - 实测 Step2 参数面板最窄 543 px，放不进共用列宽 → 停下申请。用户选「压缩」：参数行的标签按文字原宽显示、**绝不截断**，由后面的控件让出宽度（下拉框省略显示当前选项，展开时仍为完整列表；数值框变窄）。先试过把标签列缩到 110 px，离屏测出大多数标签会被截断，已放弃。
  - 刚启动时共用列宽只有约 300 px（1920 宽窗口；Step0 按自身内容定宽，不随窗口变化）。用户要求刚启动绝不能出现横向滚动条，并裁定改名：`Segmentation Index` → `Index`，`Parameter Source` → `Source`，`Index method` → `Method`，`Parameter version` → `Version`，Recovery 框标题 → `Recovery from .npy`（原标题放进鼠标提示）；去掉 Tile Grid 里不准确的 VRAM 估计；Output、Recovery 的说明文字自动换行。
  - 从 index 取参数时只显示一行 `Method:`（index 的方法）；Step2 自己的方法框隐藏但保留，仍保存实际应用的方法。
- **结果**（离屏）：面板最窄 257 px；刚启动时共用列宽为 271 / 300 / 401 px（1500 / 1920 / 2560 宽窗口），都没有横向滚动条；11 个方法下都没有被截断的标签。
- **测试**：`tests/test_step1_save_progress.py`（L1）、`tests/test_step2_layout.py`（L2，12 条）。相关 11 个模块 264 passed / 2 failed；这 2 条在已提交的 HEAD 上同样失败，不在附录的已有失败清单里，是这台新机器上的环境差异：`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`（逐像素几何，字体不同）、`test_step0_step1_display_isolation.py::test_step0_work_does_not_make_step1_load_or_redraw`。
- **提交**：L1 `1d14801`，L2 `2294bc7`。**真机验收通过（用户 2026-09-25）。**
- **Advisory**：
  - 用户主动 Stop 后，Step2 用标题为「Error」的 `QMessageBox.critical` 报告「Stopped by user.」（现有行为）；
  - 离屏 1500 宽窗口下 Step0 与 Step1 的通道列宽度不一致（365 vs 271 px），已提交的 HEAD 上一样，可能只是离屏现象；
  - 旧路径 ROI 模式中途 Stop 仍会登记成功（见 V2 第 3 步的 advisory）。

### 块 F — `Load weights` 只读 Step1 会话（计划外；用户 2026-09-25 报告并批准）
- **缺陷**（真机报告）：Step1 的 `Load weights` 选中 `step1_session.json` 后，通道勾选和权重都没有恢复；之后再勾选通道，viewer 只显示 DAPI；Save Fusion Settings 被拒绝。
- **原因**（已有问题，不是块 L 引入的）：`_load_weights_from_file` 按 `fusion_config.json` 的格式读取（最外层直接有 `groups`），而会话文件的权重放在 `fusion_config` 里面。代码不检查就调用 `apply_full_config`，装入一个没有任何组的草稿：所有 marker 移出 fusion，只剩 DAPI。之后勾选的通道只记了权重，没有组可加入，所以始终进不了 fusion；fusion 里没有 marker，Save 因此被拒绝。
- **用户裁定**：`Load weights` 只读 `step1_session.json`，减少复杂度；其他文件一律拒绝。
- **做法**：窗口新增 `load_weights_from_step1_session(path)`：按会话特有的字段识别会话文件（`fusion_draft`、`channel_visibility`、`channel_weights`、`patches`、`preview_mode`、`p2_params` 至少有一个；`fusion_config.json` 和 `step1_fusion_settings.json` 都没有）；先用 `fusion_domain.migrate_session` 检查会话里至少有一个当前切片的 marker 通道（兼容 `{members: [...]}` 和 `{channels: {...}}` 两种组格式）；再调用和 `Load Previous Step1 Session` 相同的两个函数（`_restore_step1_scientific_state` → `_apply_step1_display_state`），一次性恢复权重、参与、勾选、颜色、当前通道和显示模式；patch、路径、分割参数等字段不恢复。`config_panel._load_weights_from_file` 改为打开文件对话框（默认过滤 `step1_session.json`）后交给窗口，拒绝时弹出原因。
- **白名单**：`ui/step0/config_panel.py` 的 `_load_weights_from_file`、`ui/main_window.py` 新增的入口、`tests/test_step1_load_weights.py`。不改 `apply_full_config`、fusion 模型、会话加载、Save。
- **测试**：`tests/test_step1_load_weights.py` 6 条：同一个真实会话文件，`Load weights` 恢复的权重、参与、组和勾选与 `Load Previous Step1 Session` 的恢复路径完全相同；`fusion_config.json`、`step1_fusion_settings.json`、其他 JSON 被拒绝且状态不变；没有当前切片 marker 的会话被拒绝且状态不变；加载后新勾选的通道进入组、参与 fusion。相关 9 个模块 215 passed / 1 failed（`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`，按钮高 19 px 而非 20 px，HEAD 上同样失败，是这台机器的字体差异）。测试写的会话文件都在沙箱临时目录，没有写到 `~/fusion_data`。
- **真机验收通过（用户 2026-09-25）**。提交：`5bc65bc`。
- **Advisory**：Step1 目前写三个 JSON——`fusion_config.json`（`Save` 时写，Step3 从中读原始切片路径，Step2 通过 `step1_output` 拿到它的路径）、`step1_fusion_settings.json`（`Save Fusion Settings` 的已确认快照，预分割用它的 hash 判断是否过期）、`step1_session.json`（会话）。用户希望 Step1 只生成一个统一的 JSON；这会牵涉 Step2、Step3 和预分割的读取方，另立块处理。

### 块 U1 — Step1 不再写 `fusion_config.json`（计划外；用户 2026-09-26 裁定）
- **背景**：Step1 目录里有 `fusion_config.json`（Save 时写）、`step1_fusion_settings.json`（已确认的设置）、`step1_session.json`（会话）和 `fusion_meta.json`（fused.zarr 的产物说明）。用户希望 Step1 只生成一个统一的 JSON。只读核查：`fusion_config.json` 几乎没人读内容——Step2 只在信息栏显示文件名，Step3 查找原始切片路径时把它当备选之一；`step1_fusion_settings.json` 是已确认设置的权威来源（hash、身份核对、预分割过期判断都靠它），并入会话风险高。
- **用户裁定**：先做 U1（去掉 `fusion_config.json`）；`fusion_meta.json` 保留；Step2 信息栏那一行去掉。U2（把 `step1_fusion_settings.json` 并入会话）另行决定。
- **做法**：
  - Save 不再写 `fusion_config.json`；它原来写的内容（已确认的 fusion 配置、`ome_tiff`、`output_dir`、`norm_low/high`、`channel_remap_params`、`saved_at`）改为存进 `step1_session.json` 的 `last_save`，并立即请求一次会话保存。fusion 作业用的配置不变。
  - `last_save` 随会话恢复（两条恢复路径），切换数据集时清空。
  - `step1_output` 去掉 `fusion_config_path`；Step2 信息栏不再追加「Config: …」。
  - Step3 查找原始切片路径时先读 `step1_session.json` 的 `raw_ome_path`；旧项目的 `fusion_config.json` 仍作为备选读取，**不删不改**。
- **白名单**：`ui/main_window.py`（Save 写文件的一段、会话 payload 和恢复、`step1_output`、进入 Step2 时的信息栏）、`ui/step3_page.py`（`_resolve_raw_ome_path`）、`UI_SURFACE_RULES.md`、测试、本文档。`step2_page.py` 不需要改。
- **测试**：`tests/test_step1_step2_handoff_e2e.py` 新增 2 条（真实 Save 后没有 `fusion_config.json`，会话 payload 带着同一份 `last_save`，Step2 信息栏没有「Config:」；Step3 能从会话找到原始切片）；两条读回或断言 `fusion_config.json` 的旧测试改为读 `last_save`。回归 62 个模块（Step1 全部、Step3、会话和写保护、界面约定），与 HEAD 逐条对比没有新增失败。
- **真机验收通过（用户 2026-09-26）**：Step1 目录不再新生成 `fusion_config.json`，会话里有 `last_save`，Step2 信息栏不再显示 Config，Step3 正常打开。

### 块 S — `Load Previous Step1 Session` 每次弹对话框、打开所选场景（计划外；用户 2026-09-26 报告并批准）
- **问题**（真机）：点 `Load Previous Step1 Session` 没有弹窗、没有反应。终端显示：程序自己找到了当前 ROI 的 `step1_session.json`，所以不弹文件对话框；这个文件随每次改动自动保存，恢复后「changed nothing」；成功提示只打到终端。这是原有设计（`7c54a10`），不是 E 或 U1 的回归。另外，手动选择别的 ROI 的会话时，被拒绝的原因也只打到终端。
- **用户裁定**：`Load weights` 只恢复通道权重；`Load Previous Step1 Session` 完全复原当时的场景（ROI、patch、通道权重），每次都弹文件对话框；选中别的 ROI 或项目的会话时，直接切换到那个工作目录。
- **只读核实（第 1 步）**：数据集和工作目录归 Step0 所有——Step0 的 Load 事务式切换并发出 `dataset_committed`，Step1 丢弃自己的状态并退回 Step0，进入 Step1 时再从新 ROI 的 manifest 读取交接；Step1 的读取器不碰 Step0 页面；Step0 每次加载似乎新建一个 ROI 工作区，能否重新打开已有工作区尚未确认。跨 ROI 打开因此需要改 Step0 页面，触发申请里的停止条件。
- **用户裁定（2026-09-26）**：先交付缩小的一版（本块），同时做 S2 的只读调查。
- **本块执行记录**（2026-09-26 提交）：
  - `ui/main_window.py`：按钮改接 `_on_load_previous_session_clicked`：每次都弹文件对话框，默认在当前 ROI 的 step1 目录（没有时用输出目录）；取消则什么都不做；加载成功弹窗「Opened the Step1 session」；被拒绝时弹窗说明原因。加载函数和 v2 恢复里原来只打印的拒绝点改为 `_refuse_session(reason)`（打印并记下原因）；别的 ROI 或项目的会话提示「This session belongs to another ROI or project. Opening another project is not supported yet; this session was not loaded.」（用户 2026-09-26：Step0 没有「打开已有项目」的入口，原先让用户去 Step0 打开的提示会误导，改为如实说明）。程序打开 ROI 时的自动恢复仍然只打印，行为不变。
  - `tests/test_step1_session_button.py` 4 条：已有自己的会话时也弹对话框、取消什么都不做；选中的文件被加载并提示成功；别的 ROI 的会话走真实加载函数被拒绝、原因上屏、状态不变；自动恢复仍只打印。相关模块（交接契约、Load weights、会话、写保护、布局、端到端交接等）213 条全部通过。
- **拒绝时的「失败即锁」**：新格式会话被拒绝时，Step1 的就绪标志在核对之前就先清掉（有意设计，`test_step0_step1_handoff_contract.py:724` 锁定）；下次进入 Step1 会重新读交接，项目、ROI 和数据都不变。
- **S2（跨 ROI/项目打开整个场景）——冻结（用户 2026-09-26）**：用户认为「Load Previous Step1 Session」的定位是打开别的项目并切换一切（工作目录、每个 step 的结果，可能包括 Step2、Step3），相当于新开会话或历史复盘，属于全局 / 架构级；先完成 Step2、Step3 的优化，最后统一调整架构（见第六节「后续计划」）。只读调查结论（2026-09-26）：
  - Step0 目前**没有**重新打开已有 ROI 工作区的路径：`Load`（`_reload_from_paths`）每次清空 ROI、patch 和校正决定，不绑定已有的 `corrected_channels.zarr`；之后的第一次 Save 一定新建工作区（`utils/roi_project.py` 的 `create_roi_context` / `create_full_wsi_context`）。
  - 可复用的零件：`build_roi_context`（只算路径）、`OverviewPanel.set_rois_and_patches`（不触发编辑）、`_apply_corrected_store`、`Block01DisplayServices.adopt_mappings`（尚无调用方）、Step1 的权威读取器 `_load_step0_roi_result`。
  - 缺：可带参数调用的 Step0 Load、把场景装进 Step0 且不触发保存的入口、Step3 按 roi_id 选 ROI、切换时重置 Step2/Step3、Step2 分割运行时的忙碌检查、逐通道校正决定的恢复策略（v15 有意不在 Load 时预填）。
  - 建议顺序：纯函数「读取场景」→ Step0 Load 参数化（行为不变）→ Step0「装入场景」→ 忙碌检查 → 主窗口编排 → Step2/Step3 重置与按 roi_id 选择 → 两个合成项目的写入隔离测试。
  - 风险：Step0 提交切换之后再失败就回不到原项目；打开后若 Save 且没有恢复校正决定，会把该 ROI 的决定改写为 original；原始切片被改动（大小/mtime）会被拒绝；会话和 manifest 用绝对路径，项目移动后失效。

### 块 K — Step2 旧路径 ROI 模式中途 Stop 不再登记成功（用户 2026-09-26 批准；已实施，**真机验收通过**）
- **缺陷**（只读核实，未复现运行）：参数没有契约时（手动模式、旧参数文件），ROI 模式中途 Stop，`_segment_one_zarr` 在 Stop 检查点 `return 0`，但 ROI 外层的收尾检查（`workers/segment_merge_worker.py:3113`）只拦截契约路径：外层照常写 `segmentation_meta.json`、`_register_completed_result()` 登记为 `completed`、`update_roi_segmentation_run` 记 `done`、发 `finished`。界面随之显示「✓ Done!」、弹「Segmentation Complete」（带 Step3/Step4 按钮）、把该 ROI 的 Step2 标成 done。多 ROI 时，被停下的 ROI 还会沿用上一个 ROI 的 `_last_region_meta` 路径（`:3084`）。
- **影响面**：现在的交接把全图工作区也作为一个 ROI（`Full WSI`）交给 Step2（`ui/main_window.py:4163-4165`；`~/fusion_data/test1` 只读核对），所以 ROI 外层循环是日常主路径。
- **做法**：把 `:3113` 的 `if self._contract is not None and self._stop:` 改为 `if self._stop:`，旧路径复用已有的 `_ContractStopped` 收尾（不写汇总、不登记、不改 roi_index、不发 `finished`，只发 `error('Stopped by user.')`）；`run()` 的 `except` 里那条日志去掉「(Step1 hand-over)」，改为不区分路径的措辞。类名不改，不新增机制。
- **白名单**：
  - `workers/segment_merge_worker.py`：`run()` 中 ROI 外层收尾的这一处条件；`run()` 的 `except` 里 `_ContractStopped` 分支的那一行日志。
  - 新增 `tests/test_step2_legacy_stop.py`。
  - 本文档（执行记录）。
- **不改的范围**：`_segment_one_zarr` 及其各 Stop 检查点；全图分支（本块只修已定位的 ROI 收尾问题，全图分支不在本块处理）；契约路径；Step2 页面（包括 Stop 后标题为「Error」的对话框，已知 advisory）；输出目录里的文件清理；HQ / HQ2 / CDS（不再维护）。
- **测试**（只写合成项目的临时目录；真实引擎 Cellpose 或 StarDist，缺模型则跳过并注明，不用 mock）：参数不带契约，
  1. 单 ROI，推理中 Stop；
  2. 两个 ROI，第二个 ROI 推理中 Stop；
  3. 两个 ROI，第一个 ROI 完成后、第二个开始前 Stop；
  4. 不 Stop，正常完成。
  1–3：`finished` 为空，`error == ["Stopped by user."]`，结果登记和 roi_index 里没有本次运行，先前已登记的结果保持不变。4：`finished` 发出，结果登记为 `completed`，roi_index 记 `done`。
- **回归**：Step2 相关模块（含 `test_step2_runner_path.py`、`test_step2_ownership_move.py`、`test_step2_layout.py`、交接端到端）每模块单独进程，与 `git archive HEAD` 导出件逐条对比，只看新增失败。
- **真机验收**（用户）：在已有项目里用手动模式或旧参数跑一次 ROI 分割，中途 Stop：没有「Segmentation Complete」对话框；结果列表里没有本次成功登记；原有结果保持不变。
- **风险**：
  - Stop 在最后一个 ROI 刚写完输出、到达检查点之前才生效时，本次结果被丢弃（契约路径已是如此）。
  - 本改动只阻止整次运行被登记为成功，**不回收文件**：多 ROI 时前面已完成 ROI 的产物（`global_mask_<ROI>.zarr`、`global_mask_<ROI>.ome.tiff`、`global_dapi_<ROI>.ome.tiff`、`segmentation_meta_<ROI>.json`、`tile_masks/`）以及被停下 ROI 的半成品（`.dat`、部分切块）都留在本次输出目录里，未登记、不被下游读取。
- **回退**：恢复 `:3113` 的条件（加回 `self._contract is not None and`）和 `except` 里那行日志的原措辞，两处。
- **Advisory（不在本块处理）**：全图分支只在切块循环入口检查 Stop；最后一个切块推理期间或合并、写出阶段按 Stop，运行仍会完成并登记（结果是完整的，但与用户的 Stop 意图不符）。
- **执行记录**（2026-09-26，未提交）：
  - `workers/segment_merge_worker.py`：`:3113` 条件改为 `if self._stop:`（紧邻的注释同步改措辞）；`except` 的 `_ContractStopped` 分支日志改为「Stopped by user; nothing registered.」。别处未改。
  - `tests/test_step2_legacy_stop.py` 4 条（合成 ROI 工作区、真实 StarDist、参数不带契约）：改后单独运行 4 passed（连续两次）。同一测试放在 `git archive HEAD` 导出件上：3 条 Stop 场景失败——单 ROI 中途 Stop 仍发 `finished`（0 个细胞），另两种发 `finished`（60，即 ROI A 的数目）；不 Stop 的一条通过。缺陷由此复现并被锁住。
  - 回归：24 个模块（Step2 worker/页面/交接、契约、归属、remap、HQ 选择、Tissue Navigator、写保护等），每模块单独进程、4 个并行，改后与 HEAD 导出件逐条对比，已有模块**没有新增失败**。两边相同的失败：`test_hq_marker_segmentation.py` 2 条（HQ，不再维护）、`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`。只在 HEAD 一侧出现的 `test_step2_runner_path.py::test_a_hand_over_equals_the_runner_with_shared_ownership[stardist_nuclei_dapi-full]`（1 像素差），两边单独重跑都通过，是负载下的偶发。
  - 并行时新测试有 3 条失败，原因是下面的已有问题（运行资源监控器的 `NameError`），不是本块改动：单独运行全部通过。
  - **真机验收通过（用户 2026-09-26）**：手动模式 ROI 分割中途 Stop，不再弹完成对话框、不登记。用户反馈：点 Stop 后不能立即停止（旧路径要等当前切块推理结束），另行调查。

### 块 M — Step2 的 8 个方法统一走引擎子进程（用户 2026-09-26 批准；已实施，**真机验收通过**）
- **背景**：块 K 之后，用户发现手动模式和 Step1 继承走两套流程（进程内推理 vs 引擎子进程）：Stop 快慢不同、失败处理不同、Cellpose whole-cell 输入不同（旧路径 `[fusion, DAPI]` 不指定 `channel_axis`，契约为 `[fusion, fusion, DAPI]`），违背 R14。用户选择完全统一。
- **用户裁定（2026-09-26）**：
  1. 范围覆盖 Cellpose、StarDist、Mesmer 共 8 个方法（HQ / HQ2 / CDS 不维护，留在旧路径；从 .npy 恢复不跑模型，不受影响）。手动、继承、无契约的旧参数文件都走引擎。
  2. 接受 Cellpose whole-cell 手动结果改变。
  3. Step2 保留「Use GPU」：手动和继承都可以选择 CPU（GPU 部署失败或用户想强制 CPU）；Step1 预分割不加，仍由引擎自动选择。StarDist 模型名固定为 `2D_versatile_fluo`，Step1、Step2 都不能修改。
  4. 失败处理同契约路径：切块推理失败时整次运行报错、不登记。
  5. Step2 手动界面里契约之外的 Mesmer 控件删除（nuclear_channel、membrane_channels、input_mode、use_gpu、tile_size、overlap、batch_size、normalize_input、percentile_low/high）；保留 image_mpp、postprocess_min_size；新增 maxima_threshold、interior_threshold（与 Step1 参数表一致）。
  6. 旧参数文件里引擎不用的设置：点 Run 时弹窗逐条说明原因，按钮只有「按契约运行 / 取消」，强制按契约执行。
  7. **引擎身份不再作为拒绝理由**：只核对引擎种类（`engine`）和能否启动；Step1 那次运行的身份与本次身份都记进运行元数据，不同时只写日志和终端。原因：Step1 定下的是参数，参数正确就复用；`runner_version` 按整个 `seg_runner/` 目录哈希，改启动代码也会误拒。
  8. 旧路径中这 8 个方法走不到的推理分支删除（Cellpose 进程内加载留给 HQ）。
  9. Mesmer 3 个方法本机无模型，推理标「未验收」，不用 mock。
- **白名单**：
  - `seg_runner/client.py`：`EngineProcess` 增加 CPU 选项（子进程环境 `CUDA_VISIBLE_DEVICES=""`）。
  - `workers/segment_merge_worker.py`：`run()` 开头的路径选择与元数据；`_init_segmentation_backend`；`_start_contract_engine`（身份只核对种类、记录身份与设备）；`_segment_tile`（8 个方法走引擎，删除其旧分支）；`_segment_tile_contract`（无契约时按参数表取参数）；`_validate_mesmer_config`；两个切块循环 `except` 的判断。
  - `ui/step2_page.py`：统一的 Use GPU 行（两种模式都显示）；Mesmer 控件删除 / 新增；StarDist 模型名只读；读写配置（`get_seg_config`、两处套用配置到界面）；`_run` 的旧设置弹窗。
  - `utils/segmentation_param_schema.py`：`ParamSpec` 增加「固定值」，StarDist 模型名固定；`ui/step1_presegmentation/method_editor.py`：固定值只读。
  - 纯函数（列出旧参数文件里引擎不用的设置）放在 `core/preseg_contract.py`。
  - 测试、`UI_SURFACE_RULES.md`、本文档。
- **不改的范围**：`seg_runner` 的 engines / runner / protocol（引擎行为不变）；Step1 预分割的运行；HQ 系；从 .npy 恢复；输出写出阶段；Step2 其他控件与布局。
- **风险**：手动 Cellpose whole-cell 结果改变（已接受）；每块写一个临时 `.npy`（契约路径已如此）；旧 Mesmer 设置被强制忽略（弹窗说明）；Mesmer 未验收。
- **验收门**：手动配置下 Cellpose 3 个、StarDist 2 个方法 × 两个循环，全局 mask 与「同一 runner 逐块跑 + Step2 方式粘贴」逐像素相同；手动模式推理中 Stop 在 2 s 内结束、不登记；切块失败整次报错；不勾 Use GPU 时引擎设备为 CPU（继承和手动）；引擎种类不同仍拒绝、身份其他键不同照常运行并记录；界面：Mesmer 控件删除 / 新增、模型名只读、两种模式都有 Use GPU、旧文件弹窗列出原因且「取消」不运行；Step1 模型名只读。回归与 HEAD 逐条对比无新增失败。真机验收：手动模式 Stop 立即生效；取消 Use GPU 后用 CPU 跑通。
- **执行记录**（2026-09-26，未提交）：
  - `seg_runner/client.py`：`EngineProcess(..., cpu_only=False)`；为 True 时子进程环境 `CUDA_VISIBLE_DEVICES=""`，torch 和 TensorFlow 都看不到 GPU。engines / runner / protocol 未改。
  - `workers/segment_merge_worker.py`：新增 `_runs_on_engine()`（8 个方法且非 .npy 恢复）、`_wants_gpu()`、`_engine_params()`（有契约取契约参数；无契约按 `segmentation_param_schema` 的参数表从配置取值，加方法固定规则；固定参数一律取固定值）；`_init_segmentation_backend`、`_segment_tile`、两个切块循环的 `except` 改按 `_runs_on_engine()` 判断；`_start_contract_engine` 传 CPU 选项，引擎身份只核对种类，其余不同则打印并写日志，身份、设备、Use GPU 记入 `self._engine_meta`，写进 `segmentation_meta.json` 的 `seg_engine`；`_validate_mesmer_config` 只剩「读 fused 切块」；删除 8 个方法的旧推理分支（Cellpose whole-cell / nuclei / expansion、StarDist、Mesmer 的进程内推理和加载），以及随之不用的 `_mesmer_uses_selected_channels` 和 4 个导入。Cellpose 进程内加载只留给 HQ 系。
  - `ui/step2_page.py`：`GPU:` 行对所有方法、两种参数来源都显示；删除 Mesmer 的 10 个控件和只对 Mesmer 显示的 `tile size` / `batch size`，新增 `maxima_threshold`、`interior_threshold`；`get_seg_config` 对 Mesmer 写入契约的固定规则和该方法的 `input_mode`，去掉引擎不用的键；旧的 Mesmer `use_gpu`（auto / gpu / cpu）套用到 Use GPU；StarDist 模型名只读、恒为 `2D_versatile_fluo`；`_check_preseg_contract` 不把固定参数算作不一致；新增 `_confirm_ignored_settings`，在 index 来源 Run 时弹窗。
  - `core/preseg_contract.py`：新增 `STARDIST_MODEL`、`mesmer_input_mode()`、`ignored_settings()`（按写在文件里的内容判断，`params` 优先；契约文件只检查模型名）。
  - `utils/segmentation_param_schema.py`：`ParamSpec.fixed`；StarDist `model_name` 固定；`parse_values` 拒绝非固定值；`combinations`、`summary` 对固定参数取固定值（旧方案里别的模型名按固定值运行）。`method_editor.py`：固定参数只读、显示固定值。
  - 白名单外的小改动：`_check_preseg_contract`（固定参数不算不一致，属于「旧设置弹窗」的一部分）、`schema.summary`；已在上面列出。
  - 测试：新增 `tests/test_step2_engine_unified.py`（38 条：手动配置 8 方法 × 两个循环与 runner 逐像素相同，Mesmer 6 条因本机无模型跳过并注明「未验收」；手动推理中 Stop 2 s 内结束、不登记；引擎失败整次报错；不勾 Use GPU 时 StarDist 手动 / 继承都在 CPU 上跑，Cellpose 仅验证 CPU 模式启动报告 `cpu`（本机 Cellpose-SAM 在 CPU 上一个 120×120 块约 230 s，不放进常规测试）；勾选时设备由引擎决定；模型名固定；HQ 不走引擎；`ignored_settings` 纯函数；界面各项；Step1 模型名只读）。改写 2 条旧测试：`test_step2_runner_path.py::test_another_engine_is_refused` → `test_another_engine_version_runs_and_is_recorded`（裁定 7），`test_preseg_contract.py::test_ticking_normalize_input_in_step2_is_a_mismatch` → `test_step2_mesmer_keeps_the_fixed_rules_and_checks_its_thresholds`（控件已按裁定 5 删除）。
  - 回归：31 个模块，与 `git archive HEAD` 导出件逐条对比。轻量模块 4 个并行；真实引擎模块在 4 并行下超时或崩溃（10 GB 内存 / 6 GB 显存不够），改为逐个顺序运行。**已有模块没有新增失败**。两边相同的失败：`test_hq_marker_segmentation.py` 2 条（HQ 不维护）、`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`、`test_seg_runner_engines.py` 3 条（2 条 Mesmer 缺模型，1 条 StarDist expansion）、`test_seg_runner.py::test_mesmer_in_subprocess_equals_direct_call`（缺模型）。`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call` 与新测试的 StarDist ROI 一条各失败过一次，重跑 3 次：HEAD 上失败 2 次、改后失败 1 次、新测试 3 次通过——StarDist 同机多次运行偶发 1 像素差，HEAD 上已有。
  - **真机验收通过（用户 2026-09-26）**。

### Step3 重设计 — 只读调查与用户裁定（2026-09-26，未启动实施）
- **现状**（只读调查）：`ui/step3_page.py` 3705 行。左栏：项目 / ROI / 结果选择、1/32 DAPI 缩略图、Channels 与 Channel Overlay 面板；右栏：矩形框出的 patch 放大视图（Alpha、Show Outline、Reset View）；另有「Channel Remap Review / QC」标签页。显示为 pyqtgraph + CPU 合成，无 GPU、无金字塔读取；只统计 ROI 内细胞数。写 `step3_input_files.json` 和 `step3_channel_overlay_config.json`（后者每次勾选都写，落在 Step2 运行目录里），无人读取；不向 Step4 传任何东西。已有缺陷：离开到 Step0/1 不停后台线程；切换数据集不重置。核心功能无测试。
- **Step1 组件复用评估**：显示服务、全局通道面板、Navigator、相机快照为全局单实例，可直接用；`Step1ViewerHost` + 读取栈、`Step1GpuLayer` 可再建实例（montage 已有第二个 GPU 层）；`Step1WholeSlideMount` / `Step1ViewerBinding` 写死 `STEP1_SCOPE`、Step1 草稿、`window._active_roi` 和 step==1 判断，需参数化；patch 按钮条是主窗口代码，需抽成组件；Navigator 的 ROI 编辑经 `_persist_geometry_edit` 写 Step0 的 `roi_config.json` 等文件；仓库里没有整张图的 mask 叠加。
- **Odon 参考**（https://github.com/alexcoulton/odon ，提交 `b01faef010b14ea03c33e92a14e438061d257e39`，GPL-3.0，只参考设计不复制代码；只读了 `src/render/labels*.rs`、`src/masks/layers.rs` 与 `src/app.rs` 的相关段落，其余功能未逐项核实）：标签图用 OME-NGFF 多级金字塔；mask 层级锁定为图像当前层级；按视野计算所需分块，LRU 块缓存 + 一个后台读取线程，快速平移时丢弃过期请求；每块带 1 像素 halo 以 R32UI 整数纹理上传，片元着色器比较相邻编号求边，线宽 0–4 px，单色 + 透明度（默认绿、0.75），CPU 不做轮廓或多边形。
- **用户裁定（2026-09-26）**：
  1. 重建 Step3：取消「画矩形 → 只看这块」，改为整张图的 mask 浏览；排版与外观同 Step1，是 Step1 的简化版（只有通道面板和 viewer），尽可能复用 Step1 组件。
  2. 交互：Tissue Navigator 空降、patch 空降、缩放拖动；保留 Intensity、Overlay / Fusion。
  3. 可以在 Tissue Navigator 上画 ROI，但不产生下游影响（Navigator 在 Step3 为沙盒：只在 Step3 内存，不写文件，不影响 Step0/Step1）。
  4. 通道权重保留、可调整，但不保存：Step3 有自己的 fusion 草稿，**首次进入、以及 Step1 每次新的确认之后**从 Step1 已确认的设置复制（与现有的勾选播种规则一致），其余时候保留 Step3 自己的临时调整；需要修改 `UI_SURFACE_RULES.md`「Step3 行只有勾选、颜色、名称」一条。
  5. mask 选择：默认当前 ROI 最新（`roi_index` 的 active run），面板上方一个小下拉框切换。
  6. mask 控件放在 viewer 上方一行：显示开关、透明度、线宽、颜色；默认只画轮廓（单色、0.75、线宽 1），「填充」为选项（按细胞编号的固定随机色）。
  7. 按 Odon 的做法：在 GPU 显示层加标签渲染（R32UI 纹理 + 求边 / 填充着色器）——用户批准修改 GPU 层。
  8. 标签金字塔在 Step2 分割结束时生成（块 N）；旧结果没有金字塔时，Step3 打开时调用同一个生成函数现场补一次，存进该运行目录。
  8a. 补生成也失败时的退路（用户 2026-09-26 同意；第 ④ 步实施）：金字塔只负责缩小时的粗层级，第 0 级就是 Step2 的 mask 本身。① 写不进运行目录（磁盘满、无权限）→ 在内存里生成、只在本次打开期间用、不写文件（粗层约为 mask 像素的 6.6%）；② 内存生成也失败 → 只在第 0 级显示 mask，缩小时不画，mask 控件行显示「缩小时无法显示 mask：原因」；③ mask 本身读不出 → 不显示 mask，状态栏写明原因并建议到 Step2 重跑。其余功能照常；原因同时打到终端；`read` 不接受未完成或第 0 级不吻合的金字塔。
  9. 删除「Channel Remap Review / QC」标签页；不再写 Step2 运行目录里的两个配置文件。
  10. Step3 的 viewer 常驻，离开 Step3 不释放（目标部署在资源充足的服务器，避免每次重新准备）；隐藏时停止视野请求，换数据集时关闭并按新数据重开，退出程序时释放——常驻不等于继续读旧数据。
  11. patch 按钮条抽成 Step1 / Step3 共用组件，允许修改 Step1 并回归。
- **拟分步**（每步单独申请、单独验收）：① 块 N（标签金字塔，先做）；② Step3 骨架（复用 viewer 与通道面板、Step3 的 fusion 草稿、删除旧功能与标签页）；③ patch 按钮条组件化 + Navigator 空降；④ GPU 标签渲染与 mask 控件；⑤ Navigator 沙盒 ROI。在 ⑤ 完成之前，Step3 的 Navigator 保持现在的只读策略，不开放任何会写 Step0 文件的编辑。

### 块 N — Step2 生成标签金字塔（申请 v2，按独立审核修订，**用户 2026-09-26 批准**）
- **必要性**：Step3 按 Odon 的做法浏览整张图的 mask，需要与图像金字塔同级的标签金字塔；Step2 的 mask 只有一层（uint32 zarr，1024² 分块，ROI 坐标）。用户裁定在 Step2 生成，用户只等一次。
- **实测**（单个样本，只读真实 mask，输出写临时目录）：15437×16215 的 mask 生成两级 1.6 s；同尺寸合成 mask 0.7 s。不代表全切片耗时。
- **坐标契约**（与 Viewer 一致，`viewer/raw_tile_provider.py:169-193`：几何一律用每轴不取整的比例）：
  - 标签第 L 级与原始切片第 L 级使用**同一网格**：该级尺寸 `(H_L, W_L)` 取自原始切片金字塔，比例 `ds_y = H_0/H_L`、`ds_x = W_0/W_L`（浮点、两轴独立，不取整）。
  - 采样规则（像素中心最近邻）：第 L 级像素 `(i, j)` 的值取第 0 级切片坐标 `(floor((i+0.5)·ds_y), floor((j+0.5)·ds_x))` 处的 mask；该点落在 ROI 外时为 0。ROI 局部坐标 = 切片坐标 − ROI bbox 起点。
  - 每级数组只覆盖 ROI：行 `i ∈ [floor(y0/ds_y), ceil(y1/ds_y))`，列同理；数组名为级号（`1`、`2`…），不是倍数。
  - 元数据（`.zattrs`）：版本；第 0 级 mask 的相对路径与形状（第 0 级不复制）；原始切片各级尺寸；每级的 `ds_y`、`ds_x`、在该级网格中的起点 `(i0, j0)` 与数组形状；ROI bbox；mask 种类（cell / nucleus）；采样规则的文字说明；`complete` 标志。
  - 原始切片拿不到时不生成（记录原因），Step3 退回现场补生成。
- **做法**：新增纯函数模块 `core/label_pyramid.py`（无 Qt）：`level_grid(raw_level_shapes, roi_bbox)`、`build(level0_zarr, out_path, raw_level_shapes, roi_bbox, kind, cancel_check=None)`、`read(out_path)`（只返回 `complete` 且第 0 级路径与形状吻合的金字塔，否则 None）。Step3 现场补生成调用同一个 `build`。
- **停止与完整性**：
  - 开始前已按 Stop：跳过。
  - 生成中按块检查 Stop：写在临时目录 `label_pyramid_<ROI>.zarr.partial`，全部层级写完后先写 `complete: true`，再原子改名为正式名；Stop 或失败时删除临时目录。
  - 之前已完成的 ROI 的金字塔保留（与块 K 一致：已完成 ROI 的产物留在目录，整次运行不登记）。
  - `read` 不接受缺少 `complete` 或第 0 级不吻合的目录，未完成的金字塔不会被 Step3 当作完整产物。
- **失败**：金字塔是可重建的显示派生产物。mask 已写成、金字塔因 I/O 等失败时：保留 mask 和分割结果，终端与日志明确报告，不写金字塔路径；Step3 打开时现场补生成（用户 2026-09-26 同意；补生成也失败时见 Step3 裁定 8a）。
- **meta**：每个 ROI、每种 mask 分别记录：内层 ROI meta 写 `label_pyramid: {"cell": 路径或 null, "nucleus": 路径或 null}`；外层逐字段构造的 ROI 记录（`segment_merge_worker.py` 的 `roi_meta_all`）显式带上该字段；汇总 `segmentation_meta.json` 按 ROI 列出；全图模式同样写入。
- **接入**：两个循环在 mask 写完、调用 `_record_step2_geometry(...)` 之后（`:2790`、`:3649`）调用 `_write_label_pyramids(...)`。
- **白名单**：`core/label_pyramid.py`（新）；`workers/segment_merge_worker.py`（新增 `_write_label_pyramids`，两处调用，内层 ROI meta、外层 `roi_meta_all` 与汇总的 `label_pyramid` 字段）；`tests/test_label_pyramid.py`（新）；本文档。
- **不改的范围**：mask 本身与其他输出、`.dat` 等现有文件（清理归 Step4 优化）；Step3（现场补生成在 Step3 块里接）；分割、归属、拼接逻辑；Viewer。
- **空间**：两级下采样层的**未压缩像素量**约为第 0 级的 1/16 + 1/256 ≈ 6.6%（本数据按比例折算约 1 MB 级）；压缩后的实际增量随 mask 内容变化，不作保证。
- **风险**：每次运行多几秒（单样本 2 s 以内，全切片未测）；原子改名在同一文件系统内。回退：删除两处调用。
- **验收门**：
  - 纯函数：奇数尺寸、非整数比例、两轴比例不同、ROI 起点非倍数、ROI 贴边；对照**用 Viewer 的坐标映射**（`RawTileProvider.level_downsample_yx` 读同一个合成 OME-TIFF 金字塔）独立算出每个标签像素的中心，而不是复用自身公式；各级形状等于该级网格上的 ROI 覆盖范围；0 与空 mask。
  - 停止与完整性：生成中取消后无正式目录、无临时目录；缺 `complete` 或第 0 级不吻合时 `read` 返回 None。
  - 失败：模拟普通 I/O 失败，分割照常完成并登记，meta 里没有金字塔路径，日志有报告。
  - Step2 真实运行（StarDist，ROI 两个 + 全图）：每个 ROI、每种 mask 的路径都记录在内层、外层与汇总 meta 中，且能用 `read` 打开；第二个 ROI 中途 Stop 时，第一个 ROI 的金字塔保留、第二个不存在、整次不登记。
  - 回归与 HEAD 逐条对比无新增失败。

## 六、未决与 advisory

### 后续计划（用户 2026-09-26 排定；都未启动，每块须单独申请）
1. **Step2：旧路径 ROI 模式中途 Stop 仍登记成功**（没有契约的参数文件；契约路径已在 V2 第 3 步修好）。
2. **Step3 重设计**（调查与裁定见第五节「Step3 重设计」；先做块 N）：做成和 Step1 一样的布局和设置——左侧通道面板，右侧组织图像，可叠加分割 mask，可拖动、缩放，Tissue Navigator 空降和 patch；用户可以画 ROI，但不产生任何下游影响。
3. **项目 / 会话架构**（最后做）：打开别的项目并切换一切（S2 的调查结论见块 S）；切换数据集后 Step2 仍留着上一个项目的 fused.zarr 和参数路径、正在跑的分割不停止——这一条随会话恢复一起治理。
4. **Step4 优化**（用户 2026-09-26 排定，在 Step3 之后）：Step2 每次运行留下两个未压缩的临时内存映射 `global_mask_<ROI>.dat`、`global_dapi_<ROI>.dat`（本项目一次运行 955 MB + 478 MB，占运行目录约 95%），另有重复的 `global_mask_*.ome.tiff`（float32）和 `global_dapi_*.ome.tiff`。Step4 旧代码仍把 `global_mask.dat` 当作 mask 路径的备选（`ui/main_window.py:4313/4322`、`workers/feature_extract_worker.py:70`、`ui/batch_step4_dialog.py:78`），须先让 Step4 改读 zarr，再清理这些文件。
5. **长期**（Step4 优化完成后再议，用户 2026-09-26）：原始切片与 Step0 / Step1 的图像是否改用 OME-NGFF 多级 zarr（Odon 的数据结构）。评估：原始切片本身已是 1/4/16 金字塔（512 分块、LZW）；可能的收益是 LZ4 解压更快、更多粗层级（大切片的总览）；代价是一次转换（本机约一两分钟、多占约一倍磁盘，全切片更久）和所有读取方（Step0/1 viewer、Step2、Step3、Step4）改为支持 zarr，属于 P0 规则须单独审批的 Viewer 读取与缓存范围。建议先用现有基准测出瓶颈（解压 / 合成 / 上传）再立项。
6. **暂缓**：Mesmer 3 个方法的真机验收（本机无模型，DeepCell token 申请网站不可用）；U2（`step1_fusion_settings.json` 并入会话）。

### 用户裁定（2026-09-26）
- **HQ / HQ2 / CDS 这类不经 Step1 交接的方法不再维护。** 它们在新界面本来就不可见（R2）。此后各块不为它们修缺陷、不为它们补测试，也不把它们放进验收门；只有这些方法自身的测试失败不阻塞其他块——共用路径（例如两个切块循环、归属与合并）上仍维护方法的回归照常算失败。代码暂不删除；删除须另行申请。

### 已知的已有问题（advisory，未排期）
- **StarDist 同一输入多次运行偶发不一致**（块 M 回归中确认，HEAD 上已有）：偶尔约 0.5% 像素的标签差 1，使逐像素相等的测试（`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call` 等）时过时不过。
- **Cellpose 用 CPU 极慢**：本机 Cellpose-SAM 在 CPU 上一个 256×256 内部块约 230 s；全图（15437×16215）按此外推约两周。Use GPU 关闭只适合作为兜底或很小的 ROI。
- **旧路径 Stop 不能立即停止**这一条已由块 M 解决（8 个方法都走引擎）。
- **旧路径（手动模式、旧参数文件）点 Stop 不能立即停止**：推理在 Step2 worker 线程里直接调用模型，要等当前切块推理结束才走到 Stop 检查点；契约路径在引擎子进程里跑，约 0.2 秒停止。**用户 2026-09-26 决定不改（范围太大）**。只读调查结论留作参考：
  - 可行做法是手动模式的 Cellpose 3 个、StarDist 2 个方法改走 `seg_runner` 引擎子进程，复用契约路径的启动、逐块、Stop 机制；Mesmer 手动模式（膜通道、`selected_channels`、额外拉伸、`tile_size`）引擎表达不了，HQ 系不维护。
  - 合成图像实测（同参数，旧路径 vs 引擎逐块 + Step2 拼接）：Cellpose nuclei、nuclei + expansion、StarDist ×2 逐像素相同；Cellpose whole-cell 不同（72 vs 69 个细胞，前景 IoU 0.97），因为旧路径给 `[fusion, DAPI]` 且不指定 `channel_axis`，引擎按 7.11 契约给 `[fusion, fusion, DAPI]`。
  - 还需裁定的点：引擎不理会 Cellpose 的 use GPU 勾选和 StarDist 的模型名；切块推理失败会从「零 mask 后继续」变为整次运行报错。
  - 临时办法：Tile Grid 分得更细，Stop 最多等一个小切块。
- **运行资源监控器在判定「推理退回 CPU」时崩溃**（块 K 回归中发现，2026-09-26）：`utils/runtime_resource_monitor.py:336` 的 `_cpu_fallback_reasons()` 用到 `likely_gpu_inference`、`gpu_peak_util`，它们只是 `diagnose()` 的局部变量，抛 `NameError`。只在 `likely_cpu_fallback` 为真时走到（推理期间 GPU 显存增长 ≥128 MB、GPU 利用率 <10%、CPU ≥70%）；Step2 在收尾 `_finish_runtime_monitor()` 时调用，此时输出已写完，运行却以错误结束、不登记。离屏 4 个并行进程时可复现；真机上 GPU 繁忙或推理退回 CPU 时可能遇到。未修，等用户裁定。
- 用户主动 Stop 后，Step2 用标题为「Error」的对话框报告「Stopped by user.」。
- Step1 读交接时把 Step0 界面上的「Output」输入框改写为 `<roi>/step1`（`_set_gui_work_dir`）；之后在 Step0 直接 Load，可能把 step1 当成项目根目录、在其下再建 `rois/`。
- 仓库的测试写保护只保护 `config.py` 里的两个路径，不保护 `~/fusion_data`；在真实项目上做探测时只能用复制件。
- 「没有任何组时勾选的通道进不了 fusion」（块 F 修好后 `Load weights` 走不到这个状态）。
- 离屏 1500 宽窗口下 Step0 与 Step1 的通道列宽不一致（HEAD 上一样）。
- 本机（WSL2、RTX 3060）上与 HEAD 相同的失败：`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`、`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`、`test_step0_step1_display_isolation.py::test_step0_work_does_not_make_step1_load_or_redraw`（字体/几何差异），`test_step1_montage_view.py` 在第 27 条后 Qt 中止（WSL 的 GPU/EGL），5 个 GPU 模块不收集测试。

- A0 各项产出（见块 A0）。
- 同一个 patch 的核分割阶段复用：块 C 的第二步，另行申请。
- Step2 的重复参数界面：本计划只保证「不作编辑直接运行时与所选组合一致」，不重做它的界面。
- 附录中的已有失败与本计划无关，不在范围内。但它们**不能豁免**本计划涉及路径上的任何失败。

---

## 七、块 A0 产出（v3，2026-09-23，待用户逐条确认）

核查基于 `e655409`，没有改生产代码。诊断脚本都放在会话 scratchpad，只读：
- `a0_step2_handoff_probe.py`：⑧，Qt offscreen，只写临时目录；
- `a0_tissue_mask_probe.py`：⑤，**只读**打开真实切片的 overview 层；
- `a0_mesmer_both_probe.py`：②，合成图，`fusion_mesmer` 环境。

三次运行前后，`cufile.log` 都保持 4449898 B；真实切片的 size 和 mtime 也没有变。

v3.1 起，各条的裁定结果标在原位，汇总见 7.9。「设计选择」是我的建议，可以改。7.10 块 V 的 V1–V4 已裁定。

### 7.0 A0 查出的、影响全计划的事实

1. **Mesmer 在真机环境里跑不起来。**
   - 用户正在运行的程序用的是 `fusion_test2`：进程 `python -m block01_v14.main`，exe 为 `/root/micromamba/envs/fusion_test2/bin/python3.10`。这个环境装了 cellpose 4.1.1 和 stardist 0.9.2，**没有 deepcell**。
   - deepcell 0.12.10 只在 `fusion_mesmer` 环境里，而这个环境**没有 stardist**。
   - 目前没有一个环境能同时跑这 8 个方法。Step1 在 `fusion_test2` 里选 Mesmer，会直接走 `status.mesmer_available=False` 报错。
   - **GPU 实测**（2026-09-23，驱动 535.309.01）：
     - `fusion_test2`：TF 2.21 是 CPU 版（`is_built_with_cuda=False`），torch 能用 CUDA。
     - `fusion_mesmer`：TF 2.8.4 是 CUDA 版，但看不到 GPU；torch 能用 CUDA。
     - 所以目前**只有 Cellpose 真正用上了 GPU**。
     - StarDist 每个任务都会先起一个 GPU 子进程。这个子进程报「TensorFlow sees no GPU」失败后，再起一个 CPU 子进程重跑（`workers/cellpose_worker.py:236-372`，两次都用 `sys.executable`）。
     - Mesmer 只能跑 CPU。
   - **F1 裁定（审核，2026-09-23）**：
     - Mesmer 保留在界面上。当前环境没有 deepcell 时置灰，并明确显示「缺少 deepcell」。
     - 运行环境**不是 advisory**：在块 C 的 Mesmer 验收之前，必须另立环境块解决（见 7.10 块 V）。否则不能宣称 8 个方法已经交付。
   - **v3.9 状态**：`fusion_mesmer` 已经同时具备三个引擎，用户已在真机上验证主程序可以运行（7.10.1）。按 F1 的要求，块 C 的 Mesmer 验收仍然以 V0 通过为前提。
2. **Mesmer 的阈值目前没有接入。**
   - `utils/mesmer_utils.py:331 run_mesmer_prediction` 调用 `app.predict` 时，只传了 `image_mpp`、`compartment`、`batch_size`，没有传 `postprocess_kwargs_*`。
   - 要让 Mesmer 的阈值可以写成列表（R3），就必须改这一处调用，Step1 和 Step2 两条路径都要改。
   - **P1 裁定**：同意把这项加进块 C 的范围（见 7.1）。

### 7.1 ① 8 个方法的参数表

**来源**：
- 默认值：`utils/segmentation_config.py:23-314`；
- worker 读参：`workers/cellpose_worker.py:568-577`、`:297-307`、`:713`，以及 `workers/mesmer_worker.py`；
- Step2 控件范围：`ui/step2_page.py:388-460`、`:855-870`。

**精度约束**（⑧ 实测）：
- Step2 的 `QDoubleSpinBox` 默认只有 2 位小数。3 位小数的值会被四舍五入：0.375→0.38，-0.125→-0.13，0.475→0.47，0.325→0.33。
- Step2 的 diameter 上限是 300，Step1 的是 500，超过 300 会被 Step2 截断。这一条是静态阅读得出的，Qt `setRange` 的行为，没有实测。
- **设计选择**：新弹窗的范围和精度与 Step2 控件一致，也就是按下表校验。这样块 E 不用改 Step2 就能保证一致。Mesmer 阈值是例外，见下。

**可列表参数**。「auto」表示传 `None`，由库自己决定，可以作为列表里的一项。

| 方法 | 参数 | 类型 / 范围 / 精度 | 默认 | 说明 |
|---|---|---|---|---|
| Cellpose ×3 | `diameter` | float，0–300，1 位小数；0 = auto | auto（`None`） | cpsam 自动估计 |
| | `flow_threshold` | float，0–3，2 位小数 | 0.4 | |
| | `cellprob_threshold` | float，-6–6，2 位小数 | 0.0 | |
| StarDist ×2 | `prob_thresh` | float，0–1，2 位小数；auto | auto（模型自带） | 只有非 None 时才传给 `predict_instances` |
| | `nms_thresh` | float，0–1，2 位小数；auto | auto | 同上 |
| StarDist expansion | `expand_distance` | float，0–200，1 位小数 | 8 | 只有 expansion 方法有这个参数；StarDist nuclei 没有 |
| Mesmer ×3 | `maxima_threshold` | float，0–1，3 位小数 | whole-cell 0.075；nuclear 0.1 | 见下 |
| | `interior_threshold` | float，0–1，3 位小数 | 0.2 | 见下 |

**Mesmer 的「主要阈值」定为 `maxima_threshold` 和 `interior_threshold` 两个。**
- 依据：deepcell 0.12.10 的 `deepcell/applications/mesmer.py:273-290`。`deep_watershed` 的后处理参数里，只有这两个是阈值：前者决定种子，后者决定前景。
- 其余参数单值隐藏，沿用库的默认值：`maxima_smooth=0`、`interior_smooth=2`、`small_objects_threshold=15`、`fill_holes_threshold=15`、`radius=2`。
- 实测：合成图上把两个值调到 0.3 / 0.5，细胞数从 173 变成 151，说明阈值确实起作用。
- 3 位小数是因为 whole-cell 的默认值 0.075 本身就是 3 位。
- 这两个值要能到达 Step2，前提是：
  - 块 C 在 `run_mesmer_prediction` 里增加 `postprocess_kwargs` 的透传；
  - 块 E 让 Step2 带上这两个值。Step2 没有对应控件，目前它们只能靠 `get_seg_config()` 里的 `data = dict(self._seg_config)` 留在顶层，⑧ 实测是这样；`params` 里没有，也没有代码读取。
- **P1 裁定（同意）**：块 C 的范围扩大，接入 `maxima_threshold` 和 `interior_threshold`。明确包括以下几处：
  - `utils/mesmer_utils.py:331 run_mesmer_prediction`：透传 `postprocess_kwargs_whole_cell` 和 `postprocess_kwargs_nuclear`；
  - `workers/mesmer_worker.py`：Step1 预览和 Step2 tile 两条路径；
  - Step2 保留这两个参数：进入 `params`，并随 `get_seg_config()` 一起提交；
  - Step2 实际执行时透传。
  - **验收门**：测试要证明两件事。第一，参数到达了 DeepCell `app.predict` 的 kwargs。第二，结果确实改变，用合成图上的细胞数或 mask 差异来证明。
- **P2 裁定（同意）**：
  - Mesmer whole-cell 和 nuclear-guided 的列表阈值**只作用于细胞输出**（`postprocess_kwargs_whole_cell`）；
  - Mesmer nuclei 的列表阈值作用于核输出（`postprocess_kwargs_nuclear`）；
  - nuclear-guided 的副核输出用库的默认值。
  - 这个语义要在 `+` 弹窗的参数说明里写清楚，结果记录和参数文件的 metadata 里也要写，比如 `threshold_target: "whole_cell" | "nuclear"`。

**单值参数**（弹窗里显示，只接受单值；默认值来自注册表）：
- Cellpose：`min_size`，int，1–10000，默认 15。注意 worker 里是 `int(x or 15)`，所以 0 会变成 15，校验下限定为 1。`model_type` 固定为 cpsam，不显示。`use_gpu`、`tile_size`、`batch_size` 不显示，用默认值。
- Cellpose expansion：`expand_distance`，0–200，1 位小数，默认 8。**只允许单值。**
  - P3 裁定：R3 只授权了 StarDist 的 expand 可以写成列表，没有授权 Cellpose 的。
- StarDist：`model_name`，默认 `2D_versatile_fluo`；`device_preference`，不显示。
- Mesmer：
  - `image_mpp`，0.01–10，3 位小数，默认 0.5。本切片 OME 记录的像素尺寸是 0.5069 µm；
  - `postprocess_min_size`，int，≥0；
  - `use_gpu`、`batch_size`、`tile_size`、`overlap` 不显示。
  - **v3.7 起不再显示**：
    - `nuclear_channel`、`membrane_channels`、`input_mode`：输入固定下来，膜通道只能用 Fusion（7.11.1、7.11.5）。按方法区分：
      - whole-cell 和 nuclear-guided：`[fusion 核通道, fusion]`；
      - **nuclei：`[fusion 核通道, 0]`**，第二个通道为 0。
      - 参数文件和共用的构造函数都要按这个区分执行；
    - `normalize_input`、`percentile_low`、`percentile_high`：这是应用侧的那一次多余定标，要删掉（7.11.5 第 3、4 处）。
    - 这些键仍然写进参数文件，值固定为：`input_mode="step1_weighted_fusion"`，`normalize_input=False`。这样 Step2 按现有的 `fused_zarr` 路径执行，并且不再做那一次多余的拉伸。具体键值在 V1/C 实施时，对照 Step2 的读取代码最终确定。

**列表校验**（设计选择，块 B 的验收门按这条写）：
- 用逗号分隔，去掉空格；
- 每一项都在范围内，精度不超过上表；
- 重复值去重后保留原顺序，并提示；
- 空列表不能保存；
- 「auto」只能出现一次；
- 单值参数不接受逗号。
- 注意：这里的去重是**单个参数列表内**的去重，和 R9 说的「不做展开后的过滤去重」不冲突。

### 7.2 ② 8 个方法的输出表（更正 4.5）

| 方法 | 细胞 mask | 核 mask | 依据 / C 需要做的 |
|---|---|---|---|
| Cellpose whole-cell | ✓ | —（置灰） | 无 |
| Cellpose nuclei | — | ✓ | 无 |
| Cellpose nuclei + expansion | ✓（扩张后） | ✓（扩张前） | `cellpose_worker.py:578-585` 直接覆盖了核标签，要在 `expand_labels` 之前保留一份 |
| StarDist nuclei | — | ✓ | 无 |
| StarDist nuclei + expansion | ✓（扩张后） | ✓（扩张前） | `:711-715`，处理同上 |
| Mesmer whole-cell | ✓ | **见 O1** | |
| Mesmer nuclei | — | ✓ | 无 |
| Mesmer nuclear-guided | ✓ | ✓ | 目前算了两次推理（`mesmer_worker.py:193-208`），核的结果被丢弃，没有放进队列 |

**核查结果**：
- **Mesmer 模型每次推理都同时输出 whole-cell 和 nuclear 两个 head。** `compartment` 只决定后处理的是哪一个（`deepcell/applications/mesmer.py:136-150`）。
- 合成图实测（CPU）：`compartment='both'` 返回 `(1, H, W, 2)`，其中第 0 层和单独调用 `whole-cell` 的结果**逐像素相同**，第 1 层和单独调用 `nuclear` 的结果**逐像素相同**。
- 这次 CPU 计时没有体现出节省（both 3.39 s，whole-cell 1.71 s，nuclear 1.61 s，受首次调用影响），所以**不声称能提速**。
- 现状：
  - nuclear-guided 的细胞 mask 与 Mesmer whole-cell 完全相同，因为 `method_to_mesmer_mode` 对二者都返回 `whole_cell`；
  - nuclear-guided 只是多做一次 nuclear 推理；
  - Step2 的 `get_seg_config` 对 nuclear-guided 写的是 `compartment: "whole-cell"`，注册表里写的是 `"both"`。这个字段目前没有代码读取，只是记录在这里。

**O1 裁定（同意）**：Mesmer whole-cell 只显示细胞 mask，核开关置灰。

**O2 裁定（同意）**：保留现在的两次调用，只把已经算出来的核 mask 回传。本计划不做 `compartment="both"` 优化。

**三种状态**（块 C 实施）：
- `not_produced`：方法本身没有这种输出，开关置灰；
- `ok` 且 `count = 0`：成功，但零细胞；
- `failed`：运行失败，附错误信息。

另外增加 `cancelled`：Stop 时还没执行的任务记为这个状态（见 7.7）。

### 7.3 ③ 任务格式、结果记录格式、结果文件布局

**组合 ID**：
- `combo_id = sha256(canonical({method, params}))` 的前 12 位。
- 规范化复用 `_step1_config_hash` / `_canonical_step1_config_value`（`ui/main_window.py:7255-7281`）：浮点数保留 6 位，并去掉时间类字段。
- `params` 只包含方法注册表里的键，去掉 `device_used` 等运行后才写入的字段。

**任务**（进程之间传的内容，全部是可 pickle 的普通类型）：
```
{task_id, run_id, combo_id, method, params,     # params 已展开，不含列表
 patch_bbox: [y0,y1,x0,x1], patch_label: "P3"}  # label 只用于显示和日志
```
- 按方法族分组派发，这一点在块 C 实施：
  - Cellpose / StarDist 进 `run_cellpose_process`；
  - Mesmer 进 `run_mesmer_patch_preview`。
- 现在的 worker 本来就按每个任务的方法分派（`cellpose_worker.py:486`、`:559`、`:692`）；只有「选哪个进程入口」是按第一个任务决定的（`main_window.py:6463-6469`）。
- 所以要改的是 `_launch_worker` 的分组，以及两个 worker 回传内容的改造。

**结果记录**（每个任务一条；队列里只传这条记录，不传 mask）：
```
{schema: 1, run_id, task_id, combo_id, method, params,
 patch_bbox, patch_label,
 source: {pixel_key, manifest_digest, manifest_path, raw_ome_path},   # 见 7.4
 fusion_settings_hash,
 status: ok|failed|cancelled, error: "",
 cell:    {status: ok|not_produced, path, count},
 nucleus: {status: ok|not_produced, path, count},
 device, runtime_s, created_at}
```

**文件布局**（设计选择）：
```
<step1_dir>/presegmentation_runs/<run_id>/
    run.json                       # 点 Run 时冻结的快照，见 7.4
    records/<combo_id>__<bboxkey>.json
    masks/<combo_id>__<bboxkey>.cell.npy      # uint32，level-0 分辨率
    masks/<combo_id>__<bboxkey>.nucleus.npy
```
- `<bboxkey> = y0_y1_x0_x1`。
- `run_id = YYYYmmdd_HHMMSS_<4 位十六进制>`。
- 同一次 Run 里，`(combo, bbox)` 唯一；不同 Run 在不同目录。因此现有缺陷 2（npz 互相覆盖，`cellpose_worker.py:733-737` 的文件名里没有组合信息）不会再出现。
- `<step1_dir>` 就是现在的 `OUTPUT_DIR`（`main_window.py:2290`、`:2746`），和 `segmentation_params/`、`patch_preview_results/` 在同一目录。这样现有缺陷 4 也解决了。
- 旧的 `patch_preview_results/` 保留，新路径不写那里。
- 方案文件放在 `segmentation_search_plans/`（4.3），和结果目录分开。
- **L1 裁定（同意）**：旧 Run 目录不自动删除。新的一次 Run 开始时，只从视图里清掉旧结果。磁盘清理另立任务。
- **原子发布**（E1 裁定的要求，块 C 实施）：
  - mask 文件先写成 `*.tmp`，再用 `os.replace` 原子替换成正式文件名；
  - 结果记录**最后**发布，同样先写临时文件再原子替换；
  - 记录发布成功后，才往队列发送这条记录。
  - 这样只要一条记录存在，它引用的 mask 文件就一定完整。

### 7.4 ④ 来源绑定、运行快照、运行中编辑

**已有身份**：
- `_handoff_identity()`，`ui/main_window.py:6700-6731`，给出 `manifest_path`、`manifest_digest`、`channel_remap_config_hash`、`handoff_schema_version`、`source_identity`、`raw_ome_path`。
  - `manifest_digest` 是对整个 manifest 做的 hash；时间类字段不算在内。
  - **Step0 每次发布都会变**，包括只改几何的发布（增删 patch 时 `geometry_revision` 和 `n_patches` 会变，`core/step0_handoff.py:291-293`）。
- 视图 provider 的 `source_identity()`（`ui/step1_viewer_host.py:75-90`）：只改几何的发布不会让它变化，而且它只在视图里有效。**不采用。**
- `fusion_settings_hash`（`main_window.py:6750`）只对 `fusion_config` 和 `display_mapping` 做 hash，**和几何无关**。

**问题**：如果用 `manifest_digest` 判断结果是否过期，那么在 A2 随机生成 patch，或者手画一个新 patch，都会让**所有**已有结果变成过期，哪怕那些 patch 的像素完全没变。

**S1 裁定（同意采用 pixel_key，定义按审核意见修订）**：记录里 `pixel_key`、`manifest_digest`、`fusion_settings_hash` 三个都保存，各管一件事：
- `pixel_key`：判断**像素是否过期**；
- `manifest_digest`：只用于审计追溯，不参与过期判断；
- `fusion_settings_hash`：独立校验，和 pixel_key 分开比较，不并入 pixel_key。

**pixel_key 的定义**：它和 **patch 列表无关**，不能笼统地说成「和几何无关」，因为分析 ROI 本身属于几何，而且会影响像素。
- 做法：先生成一个**结构化**的身份字典，再用 `_step1_config_hash` 做规范化哈希。**不从 `identity_token()` 字符串里解析后删字段。**
- 至少包含：
  ```
  {raw:        {dataset_path, dataset_fingerprint},         # manifest.source_identity
   roi:        {bbox_fullres, polygon_fullres | None},      # 当前分析 ROI；full_wsi 两项都为 None
   remap:      channel_remap_config_hash,
   channels:   {<ch>: {decision: original|<corrected 决定>,
                       product: None | {shape, dtype, correction_method,
                                        roi_name, source_identity, written_at}}},
   handoff_schema_version}
  ```
- **排除**：patch 列表、P 编号、`n_patches`、只改几何时的 `geometry_revision`、`patch_config_path`，以及各种发布时间和时间戳。
- **取值来源**（块 C 实施，不改 viewer）：
  - `raw`、`remap`、`handoff_schema_version`、各通道的 `decision` 取自 manifest，也就是 `_handoff_identity()` 已经读出的那份（`core/step0_handoff.py:220-283`）；
  - `roi` 取自当前 `_active_roi`，或 manifest 的 `roi_config`；
  - `product` 由块 C 的新代码用 `utils/calibration_source.open_corrected_channel_array` **只读**打开 corrected 数组，读取它的 shape、dtype 和 attrs。字段和 `viewer/step1_source.py:_product_token` 用的一致，但是结构化的。
  - 不调用、也不修改 `Step1SourceTable` 的私有成员。
- patch 本身由 `patch_bbox` 标识。bbox 变了，就是另一个 patch。
- **块 C 的门**（在 HEAD 导出树上要先确认会红）：以下几种情况，pixel_key 必须**变化**：
  - 重新生成 corrected 产物；
  - 改某个通道的 original/corrected 决定；
  - 改分析 ROI 的 polygon；
  - 改 remap。

  以下几种情况，pixel_key 必须**不变**：
  - 增加或删除 patch；
  - 只改几何的发布；
  - 单纯重新发布。

**点 Run 时冻结的内容**（写入 `run.json`，内存里也保留一份）：
- 勾选的 `patch_bbox` 列表，以及它们当时的 P 编号；
- 展开后的任务表，也就是方法、组合和 `combo_id`；
- committed fusion snapshot 的 `hash`、`fusion_config` 和 `display_mapping`。现在的 `_launch_worker` 已经只从 snapshot 取值（`main_window.py:6424-6444`），保持不变；
- `source`，也就是 pixel_key 和 `_handoff_identity()` 的相关字段。

**运行中编辑**（继承 4.4，补充以下几条）：
- 正在运行时，Run 按钮禁用。现在 `_launch_worker` 在进程还活着时会**静默返回**（`:6419`），新界面要把这个状态明确显示出来。
- 运行中增删 patch、编辑方法块、保存 Fusion，都不影响已经派发的任务，只作用于下一次 Run。
- 迟到的结果按 `(run_id, combo_id, patch_bbox)` 归位，不看当前的 P 编号。
  - Step1 `_on_patches` 在 patch 数量变化时会清空 `_seg_preview_history`（`main_window.py:4740-4746`）。新的结果存储不能挂在这个按位置编号的结构上。
- 结果的 `pixel_key` 或 `fusion_settings_hash` 和当前不一致时，照常显示，但标为过期，不能选为最终。
- **任何结果都不会自动成为选中项。**
  - 现有缺陷 1 的位置在 `_record_segmentation_preview_result`（`:3128-3151`）：只要结果的 `_phase != 1`，它就会写 `self._p2_params`。
  - 轮询代码随后设置 `_params_source="patch_preview"`，并调用 `_check_save_unlock`（`:6541-6543`）。
  - 这几处在块 C 里一起改。

### 7.5 ⑤ 组织掩膜、「空白超过 40%」、ROI 包含判定

**确认：没有可以复用的组织掩膜。** 全仓搜索过 tissue、otsu、foreground、background_mask、fill_holes、TMA。同名的东西都是别的用途：
- `core/tissue_compose.py` 只做显示合成；
- `core/bg_correction.py:236 _safe_otsu` 只算 SNR 标量；
- `workers/hq2_marker_segmentation.py:201` 是细胞级的二值化；
- `utils/mesmer_utils.py:280 postprocess_mask` 处理的是细胞 label。

所以要在 `core/random_patches.py` 里新写（A2 白名单已经包含这个文件）。

**算法（设计选择，已在真实切片上只读验证）**：
1. **读取**：`OMETIFFLoader.read_region_lowres(DAPI, 0,H,0,W, loader.overview_downsample(), normalize=False)`（`core/io_loader.py:132`），走 TIFF 金字塔。
   - 真实切片是 59040×35520，`ds=64`，overview 为 923×555。
   - 读全部 29 个通道共 4.2 s（实测）；只读 DAPI 的耗时没有单独测。
2. **信号**：`log1p`，再按 p1–p99.5 做稳健归一化。
3. **平滑、阈值、形态学**：`gaussian(σ=2 px)` → Otsu → `closing(disk(4))` → `binary_fill_holes` → `remove_small_objects(200 px)`。约 0.56 s。
   - 这组数值是 A0 可行性核查时在 ds=64 上用的。正式定义改用 level-0 单位，见下面的 T1、T2。
4. **组织掩膜**就是上一步的结果。在 ds=64 的 overview 上，1 个像素对应 64×64 个 level-0 像素。
5. **空白比例** = 1 −（候选 patch 覆盖范围内组织像素的占比）。空白比例 > 0.4 的候选直接丢弃（R1）。计算方式见 T2。

**实测（真实切片）**：
- 直接对 DAPI 做 Otsu，也就是**不允许的定义**：只有 **15.1%** 的像素算作组织。核之间的间隙全被当成空白，任何 patch 都会被判为空白超过 40%。
- 上面的算法：只用 DAPI 时组织占 **71.8%**，用全部通道取最大值时占 **72.2%**，两者 **98.9%** 的像素一致，所以**默认只用 DAPI**。
- 对照图在会话 scratchpad 的 `tissue_probe/side.png`（左边是直接 Otsu，右边是本算法）：轮廓贴合组织外缘，核之间的间隙被算作组织。

**T1、T2 裁定（按审核意见修订）**

**T1：只填小洞，洞的面积阈值用 level-0 面积来定义。**
- `binary_fill_holes` 会把大腔隙（血管、撕裂）也算作组织。改成只填面积小于 `max_hole_area_l0` 的洞，大腔隙仍然算空白。
- 所有形态学参数都用 **level-0 像素单位**定义，在所选的掩膜层级上按 `ds` 换算：
  - 长度 ÷ ds；
  - 面积 ÷ ds²。
- 像素尺寸确认之后，同时记录对应的 µm² 值。
- A0 在 ds=64 上用过的值，换算成 level-0 分别是：

  | 参数 | ds=64 上的值 | level-0 值 |
  |---|---|---|
  | σ | 2 px | 128 px |
  | closing 半径 | 4 px | 256 px |
  | 最小组织块 | 200 px | 819 200 px² |
  | 洞面积阈值 | 500 px | 2 048 000 px² |

- 这些都是**这张切片上的像素空间经验值，不是最终常量**。A2 实测之后才能固定（见 T2 最后一条）。
- 不能直接固定为「500 个 mask 像素」，因为换一个金字塔层级，同样的像素数对应的面积就不同。

**T2：每次随机生成，只建一张定义固定的组织掩膜。不对每个候选窗口单独归一化、单独做 Otsu。**
- 如果每个窗口各算各的，同一个组织位置会因为候选窗口不同而得到不同的判断。
- **层级**：根据 patch 的最短边，选一个足够细的**现有**金字塔层，目标是最短边至少覆盖约 32 个掩膜像素，也就是 `ds ≤ 最短边 / 32` 时取最大的那一层。例如 512 px 的 patch 用 ds=16。
  - 层级只来自 TIFF 已有的金字塔（`read_region_lowres` 会取 ds 不超过所请求值的最粗一层），不另外重采样。
- **范围**：一次性算出整个生成范围的掩膜。生成范围是：有 ROI 时为 ROI 的 bbox，没有 ROI 时为整张切片。
- 同一批候选**共享**同一组归一化、阈值和形态学参数，这组参数按 T1 从 level-0 换算过来。
- **组织占比**：在掩膜上建积分图（`cumsum` 两次），每个候选的组织像素数用 O(1) 查表得到。候选边界和掩膜像素不对齐时，按覆盖面积加权；在 A2 的门里用合成图验证这一点。
- **先实测，后定阈值**：A2 开工后，先在真实切片上实测，报告给用户，再固定常量。实测内容：
  - 所选层级；
  - 掩膜内存；
  - 读盘和计算的耗时；
  - 组织外缘的表现（出对照图）。
- 内存估算（按算术，**未实测**）：ds=16 时整张切片约 3690×2220 ≈ 8.2 M 像素，float32 约 33 MB；ds=8 时约 131 MB。
- 生成记录里写明：掩膜层级 `ds`、level-0 单位的参数、Otsu 阈值和随机种子。
- 合成图上的验证（核之间有间隙的组织不被判为空白）放在 A2 的验收门里，本块只做了真实切片的只读核查。

**ROI 包含判定**：
- **现有工具**：
  - `cv2.fillPoly` 栅格化，在 `ui/step0/overview_panel.py:411`、`ui/step0/search_ctrl.py:2167`；
  - 射线法点判定 `OverviewPanel._point_in_polygon`（`overview_panel.py:2033`，overview 坐标，UI 类的 staticmethod）；
  - Step1 只做 bbox 包含判定（`main_window.py:4540 _patch_inside_roi_bbox`）；
  - 仓库里没有「矩形完整落在多边形内」的判定。
- **设计选择**：在 `core/random_patches.py` 里写纯函数 `rect_inside_polygon(bbox_l0, polygon_fullres)`，用 level-0 坐标精确判定：
  - 矩形 4 个角都在多边形内（偶奇射线法）；
  - 并且多边形的每条边都不与矩形内部相交（线段与矩形求交）。
  - 这对凹多边形是精确的。
  - 不依赖 UI 类，也不引入 shapely。
- **坐标约定**：`polygon_fullres` 是 `(x, y)`，bbox 是 `(y0, y1, x0, x1)`（`overview_panel.py:2135-2149`）。函数内部统一换算，测试里要覆盖。
- 没有多边形的 ROI（`polygon_fullres=None`，full_wsi）：只按组织判定。有 ROI 但没有多边形时，按 `bbox_fullres` 矩形判定。
- 有 ROI 时，候选只在 ROI 的 bbox 内抽取，而且**同样要满足空白比例不超过 40%**。R1 的空白规则对两种情况都适用。

**写入 Step0 的正式 patch**（A2 实施，只调用现有接口）：
- `OverviewPanel.add_patch_rect(y0,y1,x0,x1, roi_idx)`（`overview_panel.py:2310`）每调用一次发一次 `patches_changed`。之后走 `_on_patches_changed` → `_reconcile_roi_edit` → `_persist_geometry_edit` → `GeometryPersistWorker` → `commit_geometry_only` → `geometry_committed` → Step1 `_on_patches`（`step0_page.py:737-742`、`:4610`、`:5189`、`:5237`；`main_window.py:2068`）。
- 连续调用 N 次时，持久化任务会合并成最后一次（`geometry_persist_worker.py:91-102`），结果正确。
- 前提：Step0 至少发布过一次 handoff，否则只存在内存里（`step0_page.py:4648-4676`）。
- **注意**：
  - `add_patch_rect` 不会自动计算 `roi_idx`，要传入；
  - Step1 会把 patch 过滤到 ROI 的 bbox（`main_window.py:4548`）；
  - 增加 patch 会改变 `manifest_digest`，这正是 7.4 建议改用 pixel_key 的原因。

### 7.6 ⑥ montage 的显示供给

**复用接口**（只调用，不修改）：用整张切片 viewer 现在的合成链，而不是旧的 patch 预览链。
1. `spec = build_spec(window._display.fusion, window._display.state, mode, scope="step1")`（`ui/step1_draft_spec.py:95`）。
   - 它已经包含 Channels 勾选、Overlay/Fusion 模式、Intensity 映射 `mappings={ch:(lo,hi,gamma)}`，以及 fusion 权重和颜色。
2. 需要读的通道：`viewer/step1_compose.py:84 overlay_channels` 和 `:94 fusion_channels`。
3. 层级：用 `viewer/request_planning.py:45 pick_display_level` 按 montage 自己的缩放来选，用 `:116 bbox_to_level` 换算坐标。
4. 像素：`window._step1_mount.host.stack.provider.read_region(ch, level, y0,y1,x0,x1)`（`ui/step1_viewer_host.py:148`）。
   - 它是同步调用，返回 float32，缺失的地方是 NaN。
   - 它会遵守 original 和 corrected 的决定、corrected 的 coarse 平面，以及 ROI 裁切。
5. 合成：`viewer/step1_compose.py:193 compose(mode, tiles, **spec)`，纯 numpy，不依赖 Qt，不带缓存。
6. `missing_windows` 里的通道，调用 `window._display.request_mapping_seed(ch)`（`ui/block01_display.py:1964`）。这是现有的共享服务；映射到达后会发 `state.mapping_changed`，montage 收到后重新合成。

**不采用旧的 patch 预览链**，原因有三：
- 整张切片 viewer 在屏幕上时，它不会填缓存（`main_window.py:5706` 提前返回）；
- 没有映射的通道，它会退回 patch 百分位或 `loader._norm`，画出来和 viewer 不一样；
- 它读的是全分辨率，不用金字塔。

**缓存归属**：montage 自己持有两层 LRU，**不借用、不扩展** viewer 或 scheduler 的缓存。
- **授权边界**：按 `AGENTS.md` 第 4 条，**新增这两层缓存和 montage 工作线程，属于块 D 必须明确授权的范围**。它们不涉及修改 viewer 或 scheduler，但块 D 启动时要由用户明确批准三件事：
  - 两层缓存的容量；
  - 缓存和线程的生命周期，也就是下面的释放时机；
  - 取消和关闭的门：切换数据集、离开 Step1、关闭窗口时，线程都要退出，缓存都要清空，而且没有迟到的回调。
- 下面的数值是建议，不是已经批准的值。
- **通道块**：键为 `(patch_bbox, level, channel, pixel_key)`，值为 float32，建议上限 1 GiB。Intensity、模式或勾选变化时，不用重新读盘，只需重新合成。
- **合成结果**：键为 `(patch_bbox, level, spec_hash)`，值为 RGBA uint8，建议上限 256 MiB。
- 读取和合成在 montage 自己的工作线程里执行，最新请求优先；GUI 线程只上传图像。

**对现有缓存的影响**（已核实）：
- 直接调用 `provider.read_region`，**不经过** `TileScheduler.request`，所以不会写入或挤掉 viewer 的 raw 512 MiB 缓存和 corrected 2 GiB 缓存（`viewer/scheduler.py:206`、`:532-539`）；
- 不会碰合成 LRU，也不会碰 GPU 纹理；
- 唯一会被填充的是 `Step1SourceTable` 的元数据备忘，也就是打开的数组句柄，不含像素。

**线程安全（advisory）**：
- `Step1SourceTable` 的惰性填充没有加锁（`viewer/step1_source.py:252-258`、`:325-348`）。
- scheduler 的多个 tile-io 线程已经在这样共用它，montage 线程只是多一个同类调用方，不引入新的风险类型。
- 块 D 的门里加一条「viewer 和 montage 同时读取不出错」的实测。如果发现问题，停下来申请，不擅自给 source table 加锁。

**释放时机**（继承 4.6）：
- 切换数据集，或 `pixel_key` 变化：清空；
- 离开 Step1：清空；
- 某个 patch 被取消勾选：清掉这个 patch 的条目；
- 新的一次 Run 开始：只清结果图层，底图缓存保留，因为像素没变；
- Channels 或 Intensity 变化：只清合成层。

**不需要改 viewer、scheduler 或已有的缓存层。** 但 montage 自己新增的两层缓存和工作线程，要作为块 D 的明确授权项申请（见上文「授权边界」），不能算作「不需要申请」。

**step5_v8 参考（更正 R6 的前提描述）**：
- step5_v8 的 montage viewer 是**浏览器 WebGL**（`deepseek/step5_v8/agentic/montage_viewer_web.py`），不是 Qt。
- 它的底图是预先拼好的整张 montage，patch 尺寸统一；分隔线是一个栅格通道；mask 是栅格的填充图；画布上**没有矢量轮廓**，也**没有文字标签**。
- 本计划借用的是它的思路：一张画布、一个相机、一张布局表、分隔线作为独立图层、按行列命中判定。
- 本计划不同的地方：
  - patch 尺寸不一，采用按行装箱的布局；
  - 底图实时合成；
  - mask 用 cosmetic `QPen` 画矢量轮廓（4.6，块 D 实测）；
  - P 编号是独立图层。
- 这些都是本计划自己的设计，参考里没有现成实现。

**坐标**：mask 是 level-0 分辨率，底图是 level-k。每个 patch 的图元都要设置 level-k → 画布的变换。轮廓路径直接用 level-0 坐标乘以画布缩放，不跟随底图层级。

### 7.7 ⑦ 选定资格规则（E1、E2 已裁定）

**状态的含义**：
- `ok`、`failed`、`cancelled` 都是**运行终态**：任务不会再有结果。
- `pending`、`running` 是非终态。
- **E2 裁定**：`cancelled` 是终态，但会让整个组合**不具备选定资格**。

一个组合可以被选为最终结果，需要**同时**满足以下五条（**E1 裁定**：同意主体规则，并补上第 2 条的文件完整性条件）：
1. 在它所属 Run 的**冻结 patch 集合**里，每个任务都已到达终态，而且没有一个是 `cancelled`。
2. **文件完整**：这些任务的结果记录，以及记录要求的 mask 文件，都已经完整发布。也就是说，`status=ok` 的输出，`path` 必须存在，并且是原子替换后的正式文件（7.3 原子发布）。有记录缺失或文件缺失的组合，不能选。
3. 至少有一个 `ok`。`ok` 且零细胞也算 `ok`。
4. 不过期：`pixel_key` 和 `fusion_settings_hash` 分别都和当前一致。过期的结果**可以查看，但不能选定**。
5. 有 `failed` 的 patch 时，按原草案处理：控制栏里标出失败数，选定前弹窗提示「k/n 个 patch 失败」，用户坚持就可以选。

其他规则：
- 选定之后再运行新的 Run，旧的选定保留。但如果它变成过期，就自动取消选定，Save 重新禁用。
- 基本约束随块 C 落地（4.7）：没选组合时 Save 禁用；结果到达不会自动选中；hash 不一致时拒绝 Save。
- 现有的 `_params_match_committed_settings`（`main_window.py:6681-6696`）比较的是 `_p2_params["fusion_settings_hash"]`。**Step1 Save 写出的参数文件里没有这个 hash**，`cpcfg` 由显式的键构造（`:8057-8069`）。
  - 块 E 建议把 `fusion_settings_hash` 和 `pixel_key` 写进参数文件，只作追溯用。
  - Step2 不读这两个字段，这条不改 Step2 的行为。

### 7.8 ⑧ Step2 交接实测

**方法**：
- 脚本 `a0_step2_handoff_probe.py`，Qt offscreen，不在 DISPLAY :1 上弹窗。
- 对 8 个方法，各用非默认值构造一个「所选组合」，经过 `normalize_segmentation_config` → `save_segmentation_params`，写入临时目录。这是现有交接契约，也是 4.7 规定的写出格式。
- 然后走公开路径 `MainWindow._go_to_step2()`（`main_window.py:3913` 起，`:3924-3940` → `step2.load_step1_active_params`）。
- 最后读 `step2.get_seg_config()`，也就是 `step2_page.py:2268` 实际提交给执行器的内容，逐个键比较顶层和 `params` 两处。
- 这条路径和现有测试 `tests/test_step1_to_step2_handoff.py` 的做法一致。

**结果**：

| 方法 | 方法名一致 | 所选参数一致 |
|---|---|---|
| Cellpose whole-cell（d 17.5，flow 0.35，prob -0.5） | ✓ | ✓ |
| Cellpose nuclei（d 12，flow 0.6，prob 0.5） | ✓ | ✓ |
| Cellpose nuclei + expansion（同上 + expand 5） | ✓ | ✓ |
| StarDist nuclei（prob 0.55，nms 0.35） | ✓ | ✓ |
| StarDist expansion（同上 + expand 6） | ✓ | ✓ |
| Mesmer ×3（maxima、interior、mpp 0.65） | ✓ | mpp ✓；**maxima 和 interior 只在顶层，`params` 里没有**，而且目前没有任何代码读取（见 7.0 第 2 条） |
| 精度探针：flow 0.375，prob -0.125，sd prob 0.475，nms 0.325 | ✓ | **✗，被四舍五入为 0.38、-0.13、0.47、0.33** |

**结论**：
- 在 7.1 的范围和精度之内，Cellpose 和 StarDist 这 5 个方法从 Step1 Save 到 Step2 `get_seg_config()` **一致**，块 E 不需要改 Step2 的装载。
- 缺口有两处：
  - 超出精度或范围的值，由块 B 的弹窗校验挡住；
  - Mesmer 阈值，按 P1 由块 C 和块 E 处理。
- **测量范围**：只测了「交接文件 → Step2」这一段，没有测 Step1 `_save` 自身怎样组装 `cpcfg`。
  - 现有的 `_save` 对非 Cellpose 方法也会把 `diameter`、`flow_threshold`、`cellprob_threshold` 写到顶层（`:8062-8069`）。
  - 新界面的 Save 在块 E 里重写组装逻辑，块 E 的端到端门会覆盖这一段（8 个方法各一次）。
- 另记：`_apply_seg_config_to_ui` 对 Mesmer 强制把 `tile_size` 和 `overlap` 设为 0，`get_seg_config` 再转成 `None`（`step2_page.py:1665`、`:1769`、`:1799-1800`）。
  - 所以 Step1 的 Mesmer `tile_size=2048` 和 `overlap=128` 不会到达 Step2。
  - 这是 Step2 有意的设计（控件已禁用，并提示「from Step2 Tile Grid」），而且 7.1 已经把它们定为不显示，不算缺口。

### 7.9 裁定汇总（独立审核，2026-09-23）

| 编号 | 裁定 | 落在哪里 |
|---|---|---|
| F1 | Mesmer 保留在界面上，缺少 deepcell 时置灰并明确提示；环境问题**另立块 V**，块 C 的 Mesmer 验收以块 V 为前提 | 7.0、7.10 |
| R10–R14 | whole-cell 输入 `[fusion, fusion, DAPI]`；用户窗口和权重全局生效，模型自动定标按局部做，每个引擎只执行一套标准预处理；Step1 采用 HALO；权重在两边按相同规则参与构造（N1 选 a）；一套轮子、方法模块化。这几条是用户裁定。H1、H2 已裁定；R11 已修订为「局部自动定标，应用侧不额外拉伸，每个引擎只执行一套标准预处理」，I0–I3 作废 | 二、7.11、7.12 |
| V1–V4 | v3.9 改为一个环境 `fusion_mesmer`，每个引擎一个子进程（只做进程隔离）；micromamba 按平台锁定；Step2 分步接入；顺序为 V0 → V1/C → V2 → E | 7.10 |
| P1 | 同意扩大块 C：`mesmer_utils`、Mesmer worker、Step2 保留参数、实际执行透传；测试要证明参数到达 DeepCell kwargs，并且改变结果 | 7.1 |
| P2 | whole-cell / nuclear-guided 的阈值作用于细胞，nuclei 的阈值作用于核，副核输出用默认值；界面和 metadata 里写清楚 | 7.1 |
| P3 | Cellpose `expand_distance` 保持单值 | 7.1 |
| O1 | Mesmer whole-cell 只显示细胞，核开关置灰 | 7.2 |
| O2 | 保留两次调用，只回传核 mask；不做 `both` 优化 | 7.2 |
| L1 | 旧 Run 不自动删除；磁盘清理另立任务 | 7.3 |
| S1 | 采用 pixel_key：和 patch 列表无关，结构化生成后再哈希；manifest_digest 只用于审计；fusion hash 独立校验 | 7.4 |
| T1 | 只填小洞；阈值用 level-0 面积定义，属于经验值，A2 实测后才固定 | 7.5 |
| T2 | 每次生成只建一张固定定义的掩膜：最短边覆盖约 32 个掩膜像素的现有层级，参数共享，用积分图计算；先实测，后定阈值 | 7.5 |
| E1 | 同意主体规则，并加上文件完整性条件，写盘采用原子发布 | 7.3、7.7 |
| E2 | `cancelled` 是终态，但会让该组合不具备选定资格 | 7.7 |
| 另 | montage 的两层 LRU 和工作线程，是块 D 的明确授权项 | 7.6 |

**审核结论**：
- A0 的调查证据可以接受。按上面修订后，A0 定稿。
- A1 可以随后单独申请。
- A2 要等 S1、T1、T2 的定义写实之后才能启动。本版已经写入，待用户确认。
- 文档暂时不提交，没有 push 授权。

### 7.10 块 V：统一运行环境，每个引擎一个子进程（v3.9 按用户裁定由「每个引擎一个环境」改为单一环境；**只有 V0 可以申请启动**）

**动机**：用户提出，每种分割方法彼此隔离，并且能随项目部署到其他电脑上。F1 要求另立的环境块，就用这个方案落实。

**v3.9 变更（用户裁定，2026-09-23）**：
- 用户不想再增加 micromamba 环境，所以改为**一个环境**：`fusion_mesmer`，同时装主程序和三个引擎。
- 隔离只保留在**进程**这一层：每个引擎在同一个环境里各启动一个独立子进程，跑完就退出并释放显存，一个引擎崩溃不会拖垮主程序。
- 放弃的是**依赖隔离**：各个库只能共用同一套版本。比如 deepcell 把 TensorFlow 锁在 2.8，StarDist 也只能跟着用 2.8。
- 下表 V1 的「分 3 个引擎环境」由此作废；V2 中「按引擎各自锁定依赖」改为锁定这一个环境；V3、V4 不变。

**审核裁定**：

| 项 | 裁定 |
|---|---|
| V1 | ~~分 3 个引擎环境~~（v3.9 作废）→ **一个环境 `fusion_mesmer`**，里面有 Cellpose、StarDist、Mesmer 三个**引擎**，每个引擎一个子进程；8 个方法作为引擎内部的配置。 |
| V2 | micromamba，按平台锁定依赖（v3.9 起只锁定这一个环境），引擎作为独立子进程运行。**只提供依赖隔离和进程隔离，不提供文件权限隔离或 GPU 资源隔离。** 下文不再使用「沙箱」一词暗示更强的隔离。 |
| V3 | Step2 最终和 Step1 使用同一套引擎、同一个模型、同一种参数解释。分步接入，**保留 Step2 现有的切块、合并和恢复机制**。 |
| V4 | 协议和环境验证（V0）现在就先做。Step1 接入与块 C 合并（V1/C）。Step2 接入单独成块（V2），在块 E 的全流程验收之前完成。这样可以避免块 V 和块 C 重复改派发代码。 |

#### 7.10.1 已核实的事实与尚未证明的推断

**已核实**：
- 同一个 conda 环境里只能装一个 TensorFlow 版本。原来 StarDist 装在 `fusion_test2`（TF 2.21，CPU 版），deepcell 装在 `fusion_mesmer`（TF 2.8.4，CUDA 版）。
- **v3.9，2026-09-23，经用户批准，已对 `fusion_mesmer` 做了以下改动**（每一步前后的 `pip freeze` 都存在会话 scratchpad 里）：
  - 新增：stardist 0.9.2、csbdeep 0.8.2、numba 0.67.0、llvmlite 0.49.0、PyOpenGL 3.1.10、pynvml 13.0.1、nvidia-ml-py 13.610.43、cucim-cu12 25.6.0、click 8.5.0、nvidia-nvimgcodec-cu12 0.7.0.11；
  - 降级：cupy-cuda12x，从 13.6.0 降到 13.3.0。
  - 没有改动其他任何已有包，`pip check` 显示没有冲突。
- **改动后的核验**（合成数据，同机）：
  - StarDist 在 TF 2.8.4 下的结果，和 `fusion_test2`（TF 2.21）**逐像素相同**；
  - Mesmer 的输出和改动前相同；
  - Cellpose 在 GPU 上正常运行；
  - GPU 背景校正（tophat、cucim）的结果和 `fusion_test2` **逐像素相同**；
  - 离屏跑 Step1→Step2 交接探针，10 个用例的结果和 `fusion_test2` 相同；
  - 用户已在真机上用 `/root/micromamba/envs/fusion_mesmer/bin/python -m block01_v14.main` 测试通过；
  - Mesmer 和 StarDist 在这个环境里只能用 CPU，用户**接受**。
- **修 cupy 时的发现**：cupy 13.6.0 现场编译 `cupyx.ndimage` 时会用到系统里 `/usr/local/cuda-12.2` 的头文件，编译报错（`cuda_fp8.h`：`__nv_bfloat16_raw` 未定义）。结果是 GPU 背景校正悄悄退回 CPU，tophat 的结果最多相差 1.98。降到 13.3.0 后恢复正常。**这说明 GPU 背景校正依赖系统里 CUDA 头文件的版本**，这一条要写进部署要求。
- `fusion_test2` 里 Keras 默认用 torch 后端，StarDist 必须设置 `KERAS_BACKEND=tensorflow` 才能导入。项目代码里已经设置了。
- **在这两个环境里，TF 都看不到 GPU**，只有 torch（Cellpose）能用 CUDA。
- 仓库里没有任何环境描述文件，主程序自己的环境也没有。
- Mesmer 模型路径硬编码在 `utils/mesmer_utils.py:302 _default_mesmer_model_path`，里面有本机的绝对路径 `/sda1/Fusion/benchmark/...`。
- StarDist 在 Step1 走子进程（`workers/cellpose_worker.py:236-372`，用 `sys.executable`），在 Step2 走主进程（`workers/segment_merge_worker.py:1974-1976` 调用 `load_stardist_model`）。两条路径已经不同。

**尚未证明，不能写成理由或承诺**：
- ~~「StarDist 必须用 TF 2.21」没有证明~~：v3.9 已实测 StarDist 在 TF 2.8.4 下可用，结果一致。
- CUDA 版本**不预先写死**（包括 cu121），按各引擎在 V0 实测通过的组合来锁定。
- 老版本 TF 在 RTX 4090 上能否用 GPU，**必须实测**，不能因为缺少 sm_89 就断言靠 PTX 一定能跑或一定不能跑。驱动版本和 GPU 架构是部署约束，要写进部署说明。

#### 7.10.2 部署目标

- **第一版只支持 Linux x86_64。** 目标机器还要满足操作系统、系统库（glibc 等）和 NVIDIA 驱动的兼容条件，具体版本在 V0 实测后写明。
- **不承诺跨平台。** conda-pack 不是跨平台打包工具，要求源平台和目标平台兼容。不承诺同一个包能直接在 Linux、Windows、macOS 上运行。
- 要提供**这一个环境的规格文件和锁文件**，主程序和三个引擎都包含在里面。
- **系统依赖**，写进部署说明：
  - NVIDIA 驱动：本机是 535.309.01，兼容的下限在 V0 里记录；
  - GPU 背景校正需要系统里有 CUDA 12.2 的头文件（`/usr/local/cuda-12.2`），原因见 7.10.1；
  - `KERAS_BACKEND=tensorflow`。
- 验收层级分开说清楚：
  - 在同一台机器上用新用户测试，只能证明不依赖原用户的配置；
  - 它**不能代替**在另一台电脑上的部署验收。
  - 另一台电脑的验收是否需要、何时做，由用户指定机器。

#### 7.10.3 没有隐式回退

- **产品运行时不回退到主环境**。引擎环境缺失，或者引擎身份和结果记录、参数文件里记的不一致时，这个方法**明确不能运行**：界面置灰，显示原因。
  - 旧的进程内代码路径可以保留，作为**开发用的回退**，必须通过显式的开发开关才能启用，产品默认关闭，并在结果记录里标明。
- 部署配置**不自动退回**本机的 Mesmer 绝对路径。
  - 模型文件太大（cpsam 1.2 GB），不放进仓库。仓库里只放**模型清单**：路径、大小、SHA-256。部署时按清单放到约定的位置，并核对校验值；缺失或校验不符就明确报错。
  - 把 Mesmer 的路径改成可配置，属于修改生产代码，放在 V1/C 做；V0 只做记录。
- **CPU 回退**可以作为明确的策略：由引擎配置显式允许，实际设备如实写进结果记录。**CPU 自检通过不等于 GPU 验收通过**，两者分开报告。

#### 7.10.4 输入准备与科学后处理的归属

**原则**：
- 「主程序负责读像素和做 fusion」，指的是**应用侧的后台任务**，不能把大数组的读取和合成搬到 GUI 线程上。
- 引擎进程只负责：模型推理，以及列在下表「引擎侧」一栏里的步骤。
- 每一步只在一侧执行一次。

**现状**（静态阅读，**尚未实测**）。Step1 和 Step2 的输入准备已经不一致：

| 方法 | Step1 输入（`cellpose_worker.py` / `mesmer_worker.py`） | Step2 输入（`segment_merge_worker.py`） | 不一致之处 |
|---|---|---|---|
| Cellpose whole-cell | `fuse_fullres` 的结果 /65535，拼成 `[cyto, cyto, nuc]` 的 3 通道图，`channel_axis=-1`（`:511-527`、`:575-576`） | fused.zarr 切块 /65535，直接传 `[cyto, nuc]` 2 通道图，**不传 `channel_axis`**（`:2003-2015`） | 通道布局不同，有没有 `channel_axis` 也不同 |
| Cellpose nuclei（含 expansion） | `loader.read_region(DAPI)`，默认已归一化，再按 **patch 做 min-max**（`fusion._normalize_intensity`，`:540-545`） | fused.zarr 的第 1 通道 /65535（`:2018`） | 归一化的范围不同：一个按 patch，一个是 fusion 产物 |
| StarDist（含 expansion） | 和 Cellpose nuclei 的 DAPI 相同，再在子进程里做 `normalize(1, 99.8)` | fused.zarr 的第 1 通道，再按切块做 `normalize(1, 99.8)`（`:2132`） | 同上，另外百分位是按窗口计算的 |
| expansion 后处理 | 主进程 worker 里的 `expand_labels`（`:578-585`、`:711-715`） | 切块内的 `expand_labels`（`:2031-2038`、`:2142-2146`） | Step2 在切块边界上扩张，由现有的合并机制处理 |
| Mesmer | `build_mesmer_input(loader, ...)` 按参数里的百分位归一化，再调用 `postprocess_mask` | `run_mesmer_on_channel_source` 或 `run_mesmer_on_fused_tile`，再调用 `postprocess_mask` | 输入来源有两种 |

**块 V 的要求**：
- V0 要为 8 个方法各出一张归属表，列出以下各项分别在哪一侧执行、怎样执行：
  - 输入通道；
  - 数组布局（HW / HWC，通道顺序）；
  - dtype；
  - 归一化（范围、百分位、按什么窗口）；
  - 像素尺寸（Mesmer 的 `image_mpp`）；
  - 模型推理；
  - 扩张（expansion）；
  - 后处理（`min_size`、`postprocess_mask`、`fill_holes` 等）。
- 表中「引擎侧」的步骤只在引擎里执行；「应用侧」的步骤只在应用后台任务里执行。
- 上表里 Step1 和 Step2 的不一致，已由用户裁定 R10–R12 统一处理，契约写在 **7.11**。
  - 统一规则必须在 **V1/C 接入之前**写定，否则 Step1 接完后再改就要返工。
  - V0 只负责记录并复现现有的两条路径，**不自行改变科学处理**。
  - 长期保留两套输入语义并各自记录，**不能**作为预览有效性的最终验收。
- 搬迁时，expansion 和 Mesmer 后处理**不能遗漏，也不能执行两次**。V1 和 V2 的门都要逐项检查。

#### 7.10.5 进程模型与通信协议（第一版保持简单）

- **按引擎串行**：所有引擎进程都用同一个环境里的解释器。一次 Run 里，同一时刻只运行一个引擎进程。当前引擎加载模型，完成自己的全部任务，然后退出并释放显存，再启动下一个引擎。**不同时保留三个模型进程。**
- **协议**：stdin 和 stdout 上传 JSON 行，但 **stdout 只用于协议**，库的日志一律重定向到 stderr 或日志文件，不得混进 stdout。
  - 每条消息都带 `protocol_version`。
  - 消息类型：`hello`（引擎身份、设备）→ `task`（task_id，输入 `.npy` 路径，参数）→ `result`（task_id，记录路径，只在记录原子发布之后发送）| `error`（task_id，错误信息）→ `done`（完成）。
  - **终态登记**：
    - 进程被杀掉或崩溃时，没法保证 runner 还能发出消息。所以规则是：正常执行时，由 runner 用 `result` 或 `error` 回报；异常退出或被取消时，由**应用侧**给每个还没结束的任务登记唯一的终态。
    - 每个任务**有且只有一个**终态，登记之后不能覆盖。
    - 用户主动 Stop 的任务记为 `cancelled`，**不能**被通用的崩溃处理覆盖成 `failed`。应用侧要先记下「这是用户发起的停止」，再去结束进程。
- **取消、崩溃、关闭**：
  - 引擎进程用单独的进程组启动（`start_new_session`）；
  - Stop 或关闭窗口时，先发 `cancel`，超时后对整个进程组依次发 SIGTERM、SIGKILL，确保本应用启动的进程**及其子进程**全部结束；
  - 引擎在没有收到 Stop 的情况下崩溃时，由应用侧把还没完成的任务记为 `failed`，并附上退出码和 stderr 的末尾。
- **引擎身份**和**实际设备**分开记录，两者都写进每条结果记录，也都写进 Step1 保存的参数文件：
  - `engine_identity = {engine, lock_hash, lib_versions, model_checksum, runner_version}`：用来判断引擎是否匹配。`lock_hash` 是这一个环境的锁文件的哈希。不匹配就拒绝运行（7.10.3）。
    - `runner_version` 用 runner 代码的内容哈希。原因是：锁文件和模型都不变时，参数的解释代码仍可能改变。
  - `device_used`（cpu / gpu，以及 GPU 型号）：**不属于身份**，只记录这一次实际用的是哪个设备。
    - 设备变化按已经批准的回退和验收策略处理（7.10.3、7.10.6），不触发「身份不同就拒绝」。
    - 这样「显式允许 CPU 回退」和「身份不同就拒绝」两条规则不会互相冲突。

#### 7.10.6 验收原则：「差异可以解释」不能作为通过条件

- 同环境、同模型、同输入：先核对迁移前后的结果，要求**逐像素一致**。
  - 做法：在旧路径和新引擎进程上，用同一个 `.npy` 输入各跑一次。
- 设备不同（CPU 对 GPU）可能导致结果不完全一致。这种情况要**事先**定义比较指标和接受条件，由用户接受后才算数，不能事后拿解释来代替验收。
  - 例如：label 数目的差、匹配后的 IoU 分布、不匹配对象的比例。
- Step1 的小 patch 和 Step2 的全量切块，**不能仅凭用了同一个引擎就承诺逐像素一致**，因为切块边界、按窗口计算的归一化和合并都会带来差异。
  - 能承诺的只是：方法相同、模型相同、参数解释相同，输入准备按 7.10.4 的表执行。

#### 7.10.7 三个交付阶段

**V0：运行环境验证**（范围已经用户审定；2026-09-23 已启动并执行，结果见 7.10.8，**待用户验收**）

- **目标**：证明三件事：
  1. `fusion_mesmer` 能**照清单重建**；
  2. 三个引擎都能**在独立子进程里、不联网**运行，结果和直接调用**逐像素相同**；
  3. 定下主程序和子进程之间的通信方式，供 V1/C、V2 直接复用。
- **不改**：主程序、现有的分割代码、界面，以及 `fusion_test2` 和 `fusion_mesmer` 这两个环境本身。

**五项工作**：
1. **导出环境清单**，写入仓库新目录 `envs/fusion_mesmer/`：
   - conda 部分：`micromamba env export`，163 个包，锁定版本，另外附一份 linux-64 的 explicit 锁文件；
   - pip 部分：精确版本，约 217 个包。已确认没有从本地路径安装的包；
   - 部署说明：Linux x86_64、驱动、CUDA 12.2 头文件、`KERAS_BACKEND`（7.10.2）。
2. **照清单临时重建，然后删除**（用户同意）：
   - 在临时路径里照清单新建一个环境，和 `fusion_mesmer` 逐项比较包列表，要求完全一致；
   - 在新建的环境里跑一遍第 4 项的检查；
   - **验证完立即删除这个临时环境**。
   - 预计占用约 12 GB 临时磁盘，需要联网，耗时没有实测。
   - 这只证明清单在本机可用，不能代替在另一台电脑上的验收（7.10.2）。
3. **模型清单与断网验证**：
   - 记录三个模型的路径、大小和 SHA-256：cpsam 在 `~/.cellpose/models`，1.2 GB；StarDist `2D_versatile_fluo` 在 `~/.keras/models/StarDist2D`，17 MB；Mesmer 在 `/sda1/Fusion/benchmark/spacec/models/Mesmer_model`，104 MB。
   - 在没有网络的命名空间（`unshare -n`）里启动三个引擎，确认它们都只从本地加载模型。
4. **子进程运行原型**，放进仓库新目录 `seg_runner/`（用户同意），配测试，**主程序不调用它**：
   - 按 7.10.5 的协议实现：版本号、stdout 只走协议、唯一终态、Stop 记为 `cancelled`、进程组清理；
   - **正常运行**：三个引擎各跑一次合成图，结果和同一环境里直接调用**逐像素相同**；
   - **中途 Stop**：已完成的任务保留，其余任务登记为 `cancelled`；
   - **崩溃**：用 `kill -9` 杀掉子进程，应用侧登记为 `failed`，不会卡死；
   - **关闭**：没有残留进程；
   - **实测**：每个引擎的模型加载时间、单个 patch 的耗时、内存和显存占用。
5. **输入归属表**：把 7.10.4 和 7.11 整理成最终表格，列出 8 个方法各自的输入通道、布局、dtype、定标（由哪一侧、做几步）、扩张和后处理，每一项都附代码位置。

**拟新增的文件**（具体白名单在启动时再确认一次）：
- `envs/fusion_mesmer/`：规格文件、锁文件、pip 清单、部署说明、模型清单；
- `seg_runner/`：协议模块和三个引擎的 runner，只依赖 numpy 和对应的引擎库；
- `scripts/`：导出清单和重建验证用的脚本；
- `tests/test_seg_runner*.py`。

**验收门**：
1. 照清单在临时环境里重建成功，包列表和 `fusion_mesmer` 完全一致；重建完已删除。
2. 断网时，三个引擎都能加载模型并完成分割。
3. 子进程里的结果和直接调用的结果**逐像素相同**。
4. Stop、崩溃、关闭三种情况下，终态登记都正确，没有残留进程。
5. 受保护文件不变，两个现有环境不变，主程序行为不变。
6. 新增的测试先在 HEAD 导出树上运行，确认会失败，避免断言写空。

**V1/C：Step1 接入**（和块 C 合并申请）
- 多方法任务按引擎串行派发，细胞和核两种 mask，结果记录和原子发布，取消和关闭。
- 7.2 至 7.4、7.7 的要求都由这一块落地。

**V2：Step2 接入**（单独成块，在块 E 之前）
- 复用同一个 runner。保留 Step2 现有的切块、合并和恢复机制，只替换每个切块的推理调用。
- 验证：参数语义一致；输入语义按 7.10.4 和用户的裁定执行；引擎身份和 Step1 保存的参数文件一致，不一致时拒绝运行。

**V2 执行记录**（代码和提交说明里称「Step2 hook-up」；本节于 2026-09-25 按提交说明和代码补记，第 1、2 步执行时没有同步写进本文档）：
- **用户裁定（2026-09-25；待用户确认的补记）**：
  - 参数文件以版本号区分新旧：顶层有 `preseg_contract` 的是新格式；没有的是旧文件，**继续走旧路径**，行为不变。新格式里版本未知或缺字段一律报错，不猜测、不退回。
  - **裁定 A**：在新的执行路径接通之前，Step2 遇到有效的契约也**拒绝运行**，绝不在旧路径上运行 Step1 交来的结果。
  - 手动模式（参数来源不是 index）的参数属于用户自己，运行时丢掉契约，按旧路径执行。
  - 以上措辞依据 `1adfe4e` 的提交说明和代码注释整理，原始裁定文字没有留存；用户确认前不作为定稿。
- **第 1 步：纯搬迁（`54e825d`，已提交）**
  - `workers/segment_merge_worker.py` 的两个切块循环（`_segment_one_zarr` 和 `run()` 里的全图循环）改为从 `core.label_ownership` 取质心归属和重编号 LUT（`kept_labels`、`ownership_lut`），不再用内联拷贝；HQ 核、HQ2 各层和 QC 行仍走同一张 LUT；Step2 粘贴整个读取窗口的做法不变。
  - 影子对比（merge-policy shadow compare）关闭：删去 5 处调用和两行 `shadow_compare=enabled` 日志，引擎元数据记为 disabled；对比函数本身保留。这对应 7.12「V2：Step2 改为调用；停掉影子对比」。
  - 性能统计注意：选取归属标签的耗时从 `relabel` 阶段移到了 `postprocess` 阶段，跨这次提交不要比较这两项。
  - 门：`tests/test_step2_ownership_move.py`，两个循环（whole-cell、HQ、HQ2；跨切块边界的细胞、空切块）与冻结的旧内联代码逐项相同：主输出、核、HQ2 各层、QC id、HQ2 切块元数据和总数。
- **第 2 步：版本化交接契约（`1adfe4e`，已提交）**
  - `core/preseg_contract.py`（新，无 Qt）：`build`、`validate`、`runner_params`、`mismatches`、`fixed_rules`。契约块（版本 1）包含：`method`；`params`（组合自己的参数加方法的固定规则，Mesmer 为 `normalize_input: false` 和 `threshold_target`）；`halo_px`；`pixel_key`；`fusion_settings_hash`；`preseg_run_id`；`combo_id`；**那一次运行的**引擎身份——取自该组合成功的结果记录，各记录之间以及与 `run.json` 必须一致，从不取保存时所在环境的身份。
  - Step1 Save（选定结果的分支）：只写组合自己的参数（不再混入固定的 `min_size 15`、Cellpose 默认值、`phase1_diameter`），并在写任何文件之前先构造契约，被拒时不留下半成品。旧面板的 Save 不变（测试用本次提交之前的代码钉住它写出的文件）。
  - Step2：装载参数文件时校验契约；运行前再核对一次——控件里的参数与契约不一致按 mismatch 拒绝；一致也按裁定 A 拒绝（提示「新的运行方式尚未接通」）。worker 同样拒绝（双保险）。手动模式丢掉契约。旧文件不受影响。
  - Step2 的 `image_mpp` 输入框改为 3 位小数，和 Step1 编辑器一致（0.325 不再被四舍五入）。
  - 测试：`tests/test_preseg_contract.py`（新）、`tests/test_step1_save_params_file.py`。
- **复核（2026-09-25，另一台机器：WSL2、RTX 3060 Laptop，按锁文件重建的 `fusion_mesmer`，离屏）**：相关 7 个模块（契约、Save 参数文件、归属搬迁、label_ownership、Step1→Step2 交接、preseg_run、preseg 界面）95 passed；没有跑全量回归；该机器没有 Mesmer 模型和真实数据。
- **Q2 核实（2026-09-25）**：`fusion_settings_hash` 是 fusion 配置和 display mapping 的摘要（`MainWindow._fusion_settings_hash`，`ui/main_window.py:7716`）；fused.zarr 的 `config_hash` 另含来源、区域、方法和产物版本等字段（产物身份构造，`ui/main_window.py:8270` 起）。两者**不能直接比较**。fused 数据来源一致性的校验留待块 E 裁定；第 3 步不声称已验证。
- **第 3 步：Step2 切块推理改用 `seg_runner`（申请第四版，2026-09-25 用户批准；经三轮独立审核）**
  - **行为范围**：只有带契约的参数文件走新路径；旧文件、HQ/HQ2/CDS、手动模式行为不变。唯一例外见「关窗」。
  - **白名单**：
    - `workers/segment_merge_worker.py`：backend 初始化的契约分支（启动引擎子进程、核对引擎身份与 HALO）；`_segment_tile` 的契约分支；两个切块循环 `except` 里的契约判断（失败向外传播）；取消退出分支（全图循环和 ROI 外层，只限契约路径）；`stop()`；`run()` 的 `finally` 清理；去掉裁定 A 的拒绝。
    - `ui/step2_page.py`：`_check_preseg_contract`（去掉裁定 A 的拒绝，加 HALO 核对）；新增 `stop_background_jobs()`（只调 `worker.stop()`，返回 worker 是否仍在运行）。
    - `ui/main_window.py`：`closeEvent` 加一个 Step2 分支，按 fusion job / patch loader / overview read 的现有做法「仍在运行则暂缓关闭，500 ms 后重试」。
    - 测试：`tests/test_preseg_contract.py` 只改 `:176`、`:218` 两条临时拒绝测试；新增 `tests/test_step2_runner_path.py`。
    - 本文档。
  - **不改**：`seg_runner/`（包括 client）、`core/preseg_input.py`、`core/label_ownership.py`、切块、归属、合并、恢复、fused.zarr 写入、Step2 控件。不新增 registry 或状态机，只复用现有协议。
  - **输入**：`preseg_input.INPUT_KIND` + `model_input`，与 Step1 同一函数——Cellpose whole-cell `[f,f,n]`；Cellpose/StarDist nuclei 与 expansion 单通道核图；Mesmer whole-cell 与 nuclear-guided `[n,f]`；Mesmer nuclei `[n,0]`。参数取 `runner_params`。
  - **输出映射（Q1 裁定）**：whole-cell、expansion、Mesmer whole-cell 取 `cell`（expansion 只接回扩张后的细胞）；nuclei 类取 `nucleus` 作主输出；Mesmer nuclear-guided 取 `cell` 作主输出，`nucleus` 仍按原 `nuclei` 字段交回。
  - **runner 对接**：临时目录 `runner_io/` 归本次运行所有，每块读完即删，结束、Stop、出错时整目录删除；任务 ID `{out_prefix}_r{r}_c{c}`。
  - **失败**：契约路径的推理失败向外传播到 `run()` 最外层，发出 `error`，不登记结果、不发布成功结果；旧路径保持「零 mask 后继续」。
  - **取消**：契约路径下不再写入取消的那一块、不发布成功结果；ROI 内层返回后外层 `run()` 也退出，不进入汇总、别名写入、`_register_completed_result()` 和 `finished`；沿用 `error.emit('Stopped by user.')` 后返回。之前的切块可能已留下中间文件，如实说明。
  - **Stop 不阻塞界面**：`worker.stop()` 只置标志、调用 `ep.stop()`；引擎还在加载模型时再对进程组发 SIGKILL，不等待。等待都在 worker 线程里（client 每 0.2 s 检查标志后自己 `terminate()`；加载期间 `start()` 读到 EOF 抛 `EngineStartError`，按用户停止处理）。
  - **关窗**：界面线程不等待；worker 未结束时暂缓关闭并重试，不设总超时。**申请例外**：旧路径关窗也会先停 Step2 worker 再关（以前不理会），旧路径推理不能中途打断，可能要等当前切块结束。
  - **验收门**：
    1. 旧文件：无契约时两个循环的输出与改动前逐像素相同，含原有多输出方法的核。
    2. 等价：合成 fused.zarr 上，契约运行的全局 mask 与「同一 runner 逐块跑 + 共用归属函数粘贴」逐像素相同；两个循环都覆盖；nuclear-guided 比对核。
    3. 输入与 Step1 同一窗口的构造一致（见上面的输入表）。
    4. 引擎身份不符、HALO 与 overlap 不一致、参数不一致都拒绝运行并写明原因。
    5. 加载期间 Stop、推理期间 Stop、kill -9 引擎子进程、运行中关主窗口：登记正确，界面线程不阻塞（Stop 调用 < 50 ms），结束后无残留进程；关窗在加载和推理期间验证暂缓关闭，任务恰好已结束时直接关闭也算通过。
    6. 取消：单 ROI、多 ROI 中途 Stop、全图中途 Stop，结果索引里没有本次运行，没有 `finished`，无残留进程。
    7. 8 个方法各用真实引擎跑一次；Mesmer 在缺模型的机器上记为「未验收」，不用 mock 或 skip 顶替。反向注入只用来证明测试有效，不引出新的产品防御要求。
    8. 真机：用户在原机器上完成一次「Step1 选定 → Save → Step2 运行」。
    9. 交付时写明：块 E 之前没有验证 fused 数据来源一致（见 Q2 核实）。
  - **说明**：父进程意外退出时 runner 读到 stdin EOF，会在当前任务返回后退出；这不保证立即无残留，不作为验收依据。
  - **Advisory（不在本块处理）**：旧路径 ROI 模式中途 Stop 同样会汇总并登记成功——现有行为，等用户裁定。
  - **实施中的扩围（用户 2026-09-25 批准）**：Mesmer whole-cell 和 nuclear-guided 的契约文件经 normalize 后 `input_mode` 为默认的 `selected_channels`（离屏实测），worker 会去打开 corrected 通道组并逐块读取。按批准，在 `_validate_mesmer_config` 开头加一个分支：契约路径直接记 `mesmer_input_source="fused_zarr"` 并返回，不打开通道组；旧路径不变。
  - **第 3 步执行记录（2026-09-25，待用户真机验收；代码未提交）**：
    - `workers/segment_merge_worker.py`：
      - `run()`：用 `preseg_contract.validate` 取得契约（裁定 A 的拒绝去掉）；HALO 与 overlap 不同即报错；`finally` 里 `_close_contract_engine()`（正常结束先 shutdown，Stop 或出错时结束进程组；删除 `runner_io/`）；`except` 里 `_ContractStopped` 发 `error('Stopped by user.')`，不写汇总、不登记、不发 `finished`。
      - `_start_contract_engine`：按方法启动 `EngineProcess`，hello 里的引擎身份必须与契约的 `engine_identity` 完全相同，否则报错并写明不同的键；加载期间被 Stop 时按用户停止处理。
      - `_segment_tile_contract`：`INPUT_KIND` + `model_input` 构造输入 → `runner_params` → runner；`cancelled` 抛 `_ContractStopped`，其他非 ok 状态抛错；主输出按 Q1 映射，nuclear-guided 另交回 `nuclei`；输入、mask 和记录读完即删。任务 ID 为 `tile_` + Step2 的 `tile_id`（`{ROI 名}:{序号}` 或序号，非字母数字换成 `_`），每次运行内唯一——与申请里写的 `{out_prefix}_r{r}_c{c}` 字面不同，唯一性要求相同。
      - 两个切块循环的 `except`：契约路径关 scheduler 后向外抛；ROI 外层循环之后：契约路径被 Stop 时抛 `_ContractStopped`。
      - `stop()`：只置标志、调 `ep.stop()`；hello 未到时对进程组发 SIGKILL，不等待。`__init__` 加 `_contract`、`_engine`、`_runner_io` 三个默认值。
    - `ui/step2_page.py`：`_check_preseg_contract` 去掉「未接通」拒绝、加 HALO 核对；新增 `stop_background_jobs()`。
    - `ui/main_window.py`：`closeEvent` 在「close is CERTAIN」之前加 Step2 分支（暂缓关闭、状态栏提示、500 ms 后重试）。
    - 测试：`tests/test_preseg_contract.py` 两条临时拒绝测试改为「放行」「worker 启动契约的引擎进程」；新增 `tests/test_step2_runner_path.py`（36 条）。
  - **第 3 步验收结果**（WSL2、RTX 3060 Laptop、`fusion_mesmer`，离屏）：
    - 门 1 旧路径：不带契约时，HEAD（`6049fe9`，`git archive` 导出到 scratchpad）与改动后，Cellpose whole-cell、Cellpose expansion、StarDist nuclei、StarDist expansion × ROI / 全图共 16 项 mask 和细胞数逐像素相同；HQ/HQ2 的合并由 `test_step2_ownership_move.py` 覆盖。Mesmer 旧路径没有测（缺模型）。
    - 门 2 等价：Cellpose 3 个、StarDist 2 个方法 × 两个循环，全局 mask 与「同一 runner 逐块跑 + 共用归属函数按 Step2 方式粘贴」逐像素相同；**Mesmer 3 个方法 × 2 = 6 条未验收**（本机无 Mesmer 模型，测试显式跳过并注明）。
    - 门 3 输入：8 个方法送进 runner 的数组与测试里按 7.11.4 表独立写出的构造逐元素相同（这条测试替换了引擎进程，只查输入）；Mesmer 契约不打开通道组。
    - 门 4 拒绝：引擎身份不符（真实 StarDist 进程）、worker 和页面的 HALO 不一致都拒绝并写明原因；参数不一致由 `test_preseg_contract.py` 覆盖。
    - 门 5、6 生命周期与取消：加载期间 / 推理期间 Stop（ROI 与全图）、两个 ROI 之间 Stop、kill -9 引擎子进程、运行中关主窗口——Stop 调用 < 50 ms，加载期间 Stop 后 2 s 内结束；没有 `finished`、结果索引里没有本次运行、进程组已不存在、`runner_io/` 已删；关窗第一次被暂缓并登记 500 ms 重试，worker 结束后关闭。
    - 门 7：反向注入 10 处都变红（不核对引擎身份、worker 不核对 HALO、输入种类用错、失败被吞掉、ROI 外层仍登记、加载期间不发信号、Mesmer 仍打开通道组、关窗不暂缓、页面不核对 HALO、结束不关引擎）。「加载期间 Stop 后 2 s 内结束」这条门是为了让「不发信号」能被测出而加的。
    - 相关模块回归（23 个模块）：420 passed / 3 failed / 6 skipped；3 条失败都在附录基线清单里，HEAD 上同样失败。`cufile.log` 不变。没有跑全量回归。
    - 门 8 真机（2026-09-25，原机器已不可用，改在本机 WSL2 上做；数据 `~/fusion_data/cropped_region.ome.tif`，29 通道、uint8）：
      - 「Step1 选定 → Save → Step2 运行」跑通（用户确认）；
      - 中途 Stop 立即停止，并能再次启动。用户第一次以为不能再启动，实际是「Stopped by user.」对话框在 WSL 下弹在屏幕最左上角、没有看到，属操作问题，不是缺陷；
      - 运行中关主窗口：未做真机验收（离屏测试已覆盖）；
      - **Mesmer 3 个方法未验收**：DeepCell 申请 token 的网站不可用，本机没有 Mesmer 模型；用户同意暂时跳过。
    - 第 3 步已提交：`dcaca2c`。
    - 门 9：块 E 之前**没有**验证 fused 数据来源一致（Q2）。


#### 7.10.8 V0 执行结果（2026-09-23，待用户验收）

**新增文件**（都没有接入主程序）：
- `envs/fusion_mesmer/`：
  - `conda-linux-64.lock`：161 个包，带 md5；
  - `requirements-pip.txt`：216 个包，精确版本；
  - `environment.yml`；
  - `models.json`；
  - `README.md`：部署说明。
- `scripts/`：
  - `export_fusion_mesmer_env.sh`：导出环境清单；
  - `rebuild_fusion_mesmer_env_check.sh`：照清单临时重建，比对后删除；
  - `model_manifest.py`：写入或核对模型清单。
- `seg_runner/`：
  - `protocol.py`：消息格式、原子写盘；
  - `engines.py`：三个引擎；
  - `runner.py`：子进程入口；
  - `client.py`：父进程侧的启动、派发、终态登记和清理；
  - `selftest.py`：自检；
  - `synthetic.py`：合成输入。
- `tests/test_seg_runner.py`：13 条测试。

**验收门的结果**：

| # | 验收门 | 结果 |
|---|---|---|
| 1 | 照清单临时重建 | conda 部分 161 个包逐条相同；pip 的 216 个版本锁定全部满足；`pip check` 没有冲突。耗时 3 分 20 秒（包缓存是热的），体积 12 GB。重建出的环境里自检全部通过，13 条测试全部通过。**已删除。** |
| 2 | 断网 | 在 `unshare -n` 里先确认连域名都解析不了，再跑自检：三个引擎都能加载模型，都能完成分割，退出码都是 0 |
| 3 | 子进程结果和直接调用相同 | Cellpose、StarDist、Mesmer（nuclear-guided 的细胞和核）都**逐像素相同** |
| 4 | Stop / 崩溃 / 关闭 | Stop 时，已完成的任务保留为 ok，其余登记为 cancelled，进程组清空；`kill -9` 后，所有任务登记为 failed，退出码 -9，不会卡住；正常关闭时退出码为 0，进程组清空；任务报错后，下一个任务照常运行 |
| 5 | 不改现有的东西 | 没有改主程序和现有 worker；`fusion_test2`、`fusion_mesmer` 两个环境没有变化；`cufile.log` 始终是 4449898 B，SHA-256 `04c8a602…` |
| 6 | 测试不是空断言 | 新测试在 HEAD 导出树上会报收集错误。另外做了两处**反向注入**，各自对应的测试都会失败：去掉 Cellpose 的输入拷贝；去掉「stdout 只走协议」的重定向。恢复代码后测试通过 |

**全量回归**（2026-09-24，按附录的方法：170 个模块，每个模块单独一个进程，`fusion_test2`）：
- 结果：**3745 passed / 17 failed / 1 skipped**。回归期间代码冻结，运行结束后逐个核对文件哈希，都没有变化。
- 17 个失败中，有 16 条和附录基线的清单**逐条相同**，而且不涉及本块的任何路径。
  - `git diff c9f80df HEAD` 显示，生产代码和基线相同，只有本计划文档有改动。
  - 附录里提到的偶发失败 `test_loaded_channel_switch_is_cache_hit`，这一次通过了。
- 第 17 条是**本块新引入的**：`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call`。
  - 原因：这条测试的「直接调用」参照在 pytest 进程里导入 StarDist，而回归命令没有设置 `KERAS_BACKEND`。`fusion_test2` 里 Keras 默认用 torch 后端，csbdeep 会拒绝。这是测试自身的问题：主程序在 `workers/cellpose_worker.py:181` 设置了这个变量，引擎子进程也由客户端设置了。
  - 修复：测试模块开头改为 `os.environ.setdefault("KERAS_BACKEND", "tensorflow")`。
  - 修复后，按回归命令（清空 `KERAS_BACKEND`）重跑这个模块：`fusion_test2` 12 passed / 1 skipped（Mesmer 因为缺 deepcell 而跳过），`fusion_mesmer` 13 passed。
  - 这次修改只改了这一个测试文件，其他模块不受影响，所以没有重跑整个回归。

**实测数据**（384×384 的合成图）：

| 引擎 | 设备 | 加载模型 | 一个任务 | 峰值内存 | 显存 |
|---|---|---|---|---|---|
| cellpose | cuda:0 | 6.1 s | 1.3 s | 1.8 GiB | 3.2 GiB（进程退出后释放） |
| stardist | cpu | 2.5 s | 1.3 s | 0.7 GiB | 0 |
| mesmer | cpu | 9.4 s | 3.1 s | 1.4 GiB | 0 |

**执行中的发现**：
1. **Cellpose 4.1.1 在 `eval` 时会原地改写多通道的输入数组**，改动幅度最大 0.04（在 [0,1] 的数据上）。同一个数组传进去两次，第二次分割的其实是被改过的图，结果就会不同。
   - 单通道输入不受影响。
   - 排查时我一度以为这是「第一次调用效应」或 GPU 不确定性，**这个判断是错的**。每次传入一份新拷贝，结果就完全确定。
   - `seg_runner` 的做法是传拷贝。已核对：现有的 Step1（`cellpose_worker.py:577`）和 Step2（`segment_merge_worker.py:2008-2016`）在 `eval` 之后都**没有复用**那个数组，不受影响。
   - 在 7.12 的共用输入构造里，这一条要作为约束写进去。
2. **环境导出时 conda 和 pip 有同名包**：conda 的 `tzdata` 是时区数据库，pip 的 `tzdata` 是 Python 包。第一版导出脚本按名字区分两者，漏掉了 pip 的 `tzdata`，第一次重建因此 `pip check` 报错。改成按 pip 自己记录的安装者（`INSTALLER`）区分后，第二次重建全部通过。
3. **子进程的工作目录不能是仓库**：CUDA 库会把 `cufile.log` 这类文件写到当前工作目录，所以引擎进程的工作目录改为系统临时目录。相应地，协议里的路径一律由客户端转成绝对路径。
4. 在 `fusion_test2` 里跑 `test_seg_runner.py`，结果是 12 条通过、1 条跳过（Mesmer，因为那个环境里没有 deepcell）。

**输入归属表**（交付第 5 项；这是 V1/C 和 V2 的新流程。旧参数按 7.11.5 保持原有语义）：

**所有方法都由应用侧完成的公共步骤**，按顺序：
1. `read_bbox = patch ± HALO`，并与 ROI 的 bbox 取交集（7.11.3）；
2. fusion：committed 窗口（min/max/gamma）和权重（R13），`fuse_fullres` 或 fused.zarr；
3. 量化为 uint16；
4. 多边形以外置 0；
5. ÷ 65535 得到 `F`；
6. 按下表构造数组，交给引擎（**必须交拷贝**，见发现 1）；
7. 引擎返回之后：按 Step2 生效的归属代码（7.11.3）裁出中央区域，重新编号，统计细胞数。

| 方法 | 输入（应用侧构造） | dtype / 值域 | 定标（引擎侧，一套、一次） | 推理参数 | 扩张 | 其他后处理 | 输出 |
|---|---|---|---|---|---|---|---|
| Cellpose whole-cell | `stack([F0, F0, F1], -1)`，HWC，`channel_axis=-1` | float32 [0,1] | `eval(normalize=True)`：逐通道取 1–99 百分位（`cellpose/models.py:274-292`） | diameter、flow、cellprob、min_size | — | 在 `eval` 内部完成（min_size） | 细胞 |
| Cellpose nuclei | `F1`，HW | 同上 | 同上 | 同上 | — | 同上 | 核 |
| Cellpose nuclei + expansion | `F1`，HW | 同上 | 同上 | 同上 | **引擎侧**：`expand_labels(distance)`，同时**保留扩张前的核 mask** | 同上 | 细胞（扩张后）、核（扩张前） |
| StarDist nuclei | `F1`，HW | 同上 | csbdeep `normalize(1, 99.8)`，只做一次（`seg_runner/engines.py`） | prob、nms（None 时用模型默认值） | — | — | 核 |
| StarDist nuclei + expansion | `F1`，HW | 同上 | 同上 | 同上 | **引擎侧**：同 Cellpose expansion | — | 细胞、核 |
| Mesmer whole-cell | `stack([F1, F0], -1)`，HW2 | 同上 | DeepCell 默认预处理：99.9% 截断、rescale、CLAHE 128（`mesmer.py:62-70`） | image_mpp、compartment=whole-cell；P1 的阈值给 whole_cell kwargs | — | **引擎侧**：`postprocess_mask`（min_size 等） | 细胞 |
| Mesmer nuclei | `stack([F1, 0], -1)` | 同上 | 同上 | compartment=nuclear；P1 的阈值给 nuclear kwargs | — | 同上 | 核 |
| Mesmer nuclear-guided | `stack([F1, F0], -1)` | 同上 | 同上，两次调用（O2） | 细胞用 P1 的阈值，核用默认值（P2） | — | 同上 | 细胞、核（`paired: false`） |

- **原型的边界**：扩张和 `postprocess_mask` 目前还**没有**写进 `seg_runner`（V0 只覆盖推理）。按上表，它们放在引擎侧，由 V1/C 实现，届时由 7.10.6 的逐像素对比来把关。
- 表中「引擎侧」的步骤只在引擎里做，「应用侧」的步骤只在应用后台任务里做，两边都不能重复做（7.10.4）。

### 7.11 模型输入契约（R10–R12；V1/C 和 V2 接入之前必须写定）

**契约**：Step1 和 Step2 共用同一套模型输入规则。
- whole-cell 的输入是 `[fusion, fusion, DAPI]`，`channel_axis=-1`；
- 亮度：用户的窗口和权重在全局上生效；应用侧不额外做自动拉伸；每个引擎只执行一次它的那一套标准预处理（R11 修订版，见 7.11.5）；
- Step1 采用和 Step2 一致的 HALO 与边界处理，再裁出中央的结果。

旧的通道排法对比实验取消。验证的内容改为：**读取区域相同时**，两边的输入数组、参数和输出是否一致；并且应用侧不额外拉伸、每个引擎只执行它那一套预处理（7.11.4）。

#### 7.11.1 R10 通道

| 方法 | 送进模型的数组（两个 Step 相同） |
|---|---|
| Cellpose whole-cell | `np.stack([fusion, fusion, DAPI], -1)`，`channel_axis=-1`。Step2 在模型入口从 fused.zarr 的 `[fusion, DAPI]` 构造，**不改** fused.zarr 的存储 |
| Cellpose nuclei / nuclei + expansion | 单通道 DAPI（cpsam 内部会补成 `[DAPI, 0, 0]`，`cellpose/transforms.py:609-614`） |
| StarDist ×2 | 单通道 DAPI |
| Mesmer whole-cell / nuclear-guided | **`[fusion 的核通道, fusion 通道]`**，两个 Step 相同，都取自 fusion 输出 ÷ 65535。用户平时就是用 Fusion 当膜通道（2026-09-23）。Step2 的「DAPI + Fusion channel」模式（`step1_weighted_fusion`）本来就走 fused.zarr 切块，也就是这种排法（`segment_merge_worker.py:1722-1725` → `mesmer_worker.run_mesmer_on_fused_tile`） |
| Mesmer nuclei | `[fusion 的核通道, 0]`，与现有的「DAPI only」模式一样，第二个通道为 0 |

#### 7.11.2 R11 全局亮度：应用层和模型内部都要检查

**应用层**（已核实）：
- **已经符合**：fusion 和 fusion 里的核通道。
  - Step1 用 `fuse_fullres`（`core/fusion_engine.py:109-147`），Step2 的 fused.zarr 由 `FullFusionWorker._fuse_tile`（`ui/step0/overview_panel.py:373-405`，`_channel_norm` 在 `:308`）写出。
  - 两边都只用 committed 的窗口；没有窗口的通道直接不参与，不会按区域估计。
  - 两边都通过 `fuse_channels` 乘上 group/nucleus 权重，裁剪到 [0,1]，再**量化成 uint16**。
  - V0 要核实一点：`apply_channel_remap(raw, p)` 和 `_channel_norm(ch, arr)` 在同一个窗口下**逐元素相等**。
- **不符合**：
  - Step1 的纯核方法先调用 `loader.read_region(DAPI, normalize=True)`，这会走 `_norm`，对当前区域取 p1–p99.5（`core/io_loader.py:294-302`）；然后又对 patch 做 min-max（`workers/cellpose_worker.py:544`）。这是两次局部归一化。
  - Mesmer 的 `build_mesmer_input`（`utils/mesmer_utils.py:229-258`）分两种情况：
    - 有 committed 窗口的通道，用 `apply_channel_remap`，也就是**全局窗口**（`:232-234`）；
    - 没有窗口的通道，才按当前区域做 1–99.8 百分位（`:235-236`）。
    - 膜通道按 `weights` 加权后取最大值（`:250-253`）。**没有设置膜通道时，第二个输入通道全为 0**（`:254-255`），而注册表里 `membrane_channels` 的默认值就是空列表。
    - 核通道没有乘核权重，违反 R13。

**纯核方法的输入（审核指出：只有「同一个 DAPI 窗口」还不够）**：
- Step2 读到的核通道是：`clip(apply_window(DAPI) × nuc_w, 0, 1)`，量化为 uint16，再 /65535（`core/fusion_engine.py:66-68`）。
- 如果 Step1 只是把原始 DAPI 按全局窗口映射成 float，就少了**核权重**和**uint16 量化**这两步，两边的输入仍然不同。
- 契约要写明：窗口（包括 gamma）、核权重、裁剪、uint16 量化，每一步都做，还是每一步都不做。**两边必须一样。**
- **待裁定 N1**：
  - (a) 两边都用 fusion 的核通道，也就是 Step2 现有的 fused.zarr 第 1 通道。Step1 取 `fuse_fullres(...)[:, :, 1] / 65535`。这样核权重和量化都参与，Step2 不用改。
    - 副作用：核权重 < 1 会压低纯核方法的输入亮度；`nuc_w = 0` 时输入全为 0，应当拒绝运行。
  - (b) 两边都忽略核权重，只用「窗口 → 裁剪 → 量化」。这样 Step2 就不能直接用 fused.zarr 的核通道，要单独读 DAPI，改动更大。
  - **N1 裁定（R13）：选 (a)**，外加 `nuc_w = 0` 时拒绝运行。
  - Mesmer 也一样：纯核输入和核通道都从 fusion 结果中取，不再在 `build_mesmer_input` 里另读原始通道。膜通道的来源由块 B/C 按 R13 另外写定。

**模型内部**（已核实）：
- **Cellpose**：`eval` 默认 `normalize=True`，会对每张输入图的每个通道单独做 1–99 百分位拉伸（`cellpose/models.py:157`、`:174`、`:274-292`）。
- **StarDist**：归一化是我们自己的代码调用 `csbdeep.normalize(1, 99.8)`（`cellpose_worker.py:300`、`segment_merge_worker.py:2132`）。
- **Mesmer**：DeepCell 的 `mesmer_preprocess`（`deepcell/applications/mesmer.py:62-70`）默认对**当前输入**做三步局部处理：
  1. `percentile_threshold(99.9)`，百分位截断；
  2. `histogram_normalization` 里的 `rescale_intensity(out_range=(0,1))`，按当前输入的最小值和最大值重新拉伸（`deepcell_toolbox/processing.py:78-79`）；
  3. `equalize_adapthist(kernel_size=128)`，也就是 CLAHE（`:80`）。
  - **现状：两层定标都开着。** 第一层是上面的全局窗口，或者局部百分位；第二层是 DeepCell 的这三步。代码调用 `app.predict` 时没有传 `preprocess_kwargs`（`mesmer_utils.py:337`），所以用的是默认值。
  - **会不会「过度拉伸」**（按代码推理，**未实测**）：
    - 第一层已经把数值映射到 [0,1] 并截断。第二层的 99.9% 截断，会让最亮的 0.1% 再饱和一次。
    - 最主要的是 `rescale_intensity` 按当前输入的最小值和最大值重新拉伸：在信号很弱的区域（比如只有背景和少量暗细胞），最大值本身就小，拉到 0–1 之后，**背景噪声被放大成满幅**。
    - CLAHE 再增强局部对比度。
    - 原始数据是 uint8（OME `Type="uint8"`，只有 256 级）。多次拉伸**可能**带来色阶断层，但这只是待验证的可能影响，不能仅凭数据是 uint8 就下结论。
    - 所以在弱信号的 patch 上，确实可能过度放大噪声。至于这是不是用户看到「Mesmer 表现不佳」的原因，还没有证据。
  - **其他可能拖累 Mesmer 的因素**：
    - 已核实：膜通道为空时，whole-cell 的第二个输入全为 0；
    - 像素尺寸：OME 的 `PhysicalSizeX` 是 0.5069 µm，和默认的 `image_mpp=0.5` **接近**，但这**不能**说明像素尺寸的影响已经排除，仍然是待验证的可能影响。
  - 局部处理**不只是 CLAHE**：前两步取决于整个输入窗口里有没有更亮的信号。**HALO 宽度不能解决这个问题**，128 也不是 HALO 的充分条件，因为还涉及模型的输入缩放和 CLAHE 网格的位置。这一版删掉了「HALO ≥ 128」的说法。按 R11 修订版，这种随窗口变化的差异用户已经接受。

**裁定**：I0–I3 已被 R11 修订版取代，I1（关掉 Cellpose 内部归一化）、I2（删掉 StarDist 的 `normalize`）、I3（关掉 Mesmer 预处理）**全部作废**。定标规则见 7.11.5。

**用户窗口与权重的冻结**（不属于自动定标）：
- 使用 committed fusion snapshot 里的 `display_mapping`，也就是 `_launch_worker` 现在已经冻结的那份（`main_window.py:6424-6444`），另加上核权重（N1）。
- 不要求每个任务重新扫描整张图。
- DAPI 没有 committed 窗口时，明确拒绝运行，不退回局部百分位。

#### 7.11.3 R12 Step1 HALO、ROI 边界与归属

**Step2 的实际做法**（已核实）：
- **HALO**：默认 `overlap_px=200`（`workers/segment_merge_worker.py:113`）。`read_bbox = own_bbox ± overlap`，在 fused.zarr 的边界（也就是 ROI 的 bbox）处截断，不补边（`utils/tile_scheduler.py:37-60`）。
- **多边形以外**：在**写 fused.zarr 的时候**，先 fusion，再量化成 uint16，然后把多边形以外的像素，两个通道都**置 0**（`ui/step0/overview_panel.py:606-622`，用 `cv2.fillPoly` 做掩膜）。
  - 分割阶段没有再做多边形处理：`_segment_one_zarr` 接收了 `poly_fullres` 参数（`:2194`、`:2969`），但函数内部**没有使用**。
  - 所以 Step2 的模型输入和全部预处理，看到的都是「多边形以外为 0」的图像。
- **归属规则**：生效的路径是 `segment_merge_worker.py:2538-2575` 里的内联代码，**不是** `CentroidOwnershipMergePolicy`。后者在 `:2584` 附近只做影子比较，注释写的是「legacy remains authoritative」。
  - 质心用 `_centroids_vectorised` 计算（`:2877`，bincount）。
  - 质心落在半开区间 `[own_y0, own_y1) × [own_x0, own_x1)` 内的细胞保留，按原标签顺序通过 LUT 重新编号。
  - HQ 方法的核用**同一个 LUT** 重新编号（`:2559-2562`）。

**H1（审核同意）**：
- HALO 宽度和 Step2 Tile Grid 的 overlap 用**同一个配置**，冻结进运行快照，写进参数文件。
- Step2 实际执行时必须用这个值。如果改了它，旧的预览就**不能**再被说成是同一执行配置，要在界面上标出来。

**H2（审核同意统一；具体规则写定如下）**：
1. **读取范围**：`read_bbox = patch_bbox ± H`，然后与**分析 ROI 的 bbox** 取交集（full_wsi 时与整张切片取交集）。超出的部分不读、不补边，和 Step2 一样。
2. **先 fusion，再量化**：和 fused.zarr 的写出顺序一样，得到 uint16 的 `[fusion, DAPI]`。
3. **多边形掩膜**：在量化**之后**、构造模型输入**之前**，把多边形以外的像素，两个通道都置 0。
   - 用和 `_poly_mask` 同一种栅格化方式（`cv2.fillPoly`，坐标为 level-0 `(x, y)`）。
   - 这样 Step2 在 fused.zarr 里看到的 0，和 Step1 看到的 0，是同一批像素。
4. **哪些像素参与预处理**：`read_bbox` 里的全部像素，包括多边形以外的 0，都送进模型，参与模型那唯一一次局部定标。Step2 的 fused.zarr 在同样的位置也是 0，所以读取区域相同时，两边参与定标的像素完全一样。
5. 最后按下面的归属规则裁出中央区域。
- 没有多边形的 ROI，跳过第 3 步。

**归属与细胞 / 核的配对**（审核指出，原来的写法是错的）：
- **不能**给细胞和核分别按各自的质心筛选、分别重新编号，然后声称配对自然保留。扩张后的细胞，质心可能在 patch 内，而它的核质心在 patch 外；分开编号还可能让本来不对应的对象拿到同一个 ID。
- **共享标签的输出**（Cellpose / StarDist 的 nuclei + expansion：`expand_labels` 保留原标签，细胞和它的核 ID 相同）：
  - 只按**主输出**决定归属。主输出是细胞 mask（扩张后的那张）。
  - 用主输出的质心生成**一张** LUT，细胞 mask 和核 mask 都用这张 LUT 重新编号。
  - 这和 Step2 对 HQ 核的做法一样（`:2559-2562`）。
- **独立生成的输出**（Mesmer nuclear-guided：细胞和核来自两次独立的后处理，标签没有对应关系）：
  - 细胞和核各自按自己的质心决定归属，各自重新编号。
  - 结果记录里写明 `paired: false`，界面和下游都**不能**把「编号相同」当作「配对」。
- **只有一种输出的方法**：只按这一种输出决定归属。
- **细胞数**用保留下来的不同标签的个数，不用最大标签值。

**复用方式**：
- 归属和重编号的**规则**以 Step2 生效的内联代码为准：用 bincount 算质心，半开区间，按原标签顺序生成 LUT。
- Step1 可以调用 `_centroids_vectorised`（静态方法），或者调用 `CentroidOwnershipMergePolicy`。但无论调用哪个，**V1/C 都要有一道门证明**：在同一张 label 图和同一个 own_bbox 上，它给出的保留集合和新标签，与 `segment_merge_worker.py:2538-2575` 的结果**完全一致**。
- 没有这个证明，就不能写「调用这个类就等于照搬 Step2」。

#### 7.11.4 验收：分成两个独立的问题

**「读取区域相同」的定义**：以下各项都相同，才算读取区域相同：
- 同一个 level-0 坐标区域 `read_bbox`；
- 同一个 ROI 多边形掩膜；
- 同一个来源版本，也就是 `pixel_key`；
- 同一份冻结配置：fusion 快照、HALO、引擎身份、参数。

比较最终归属之后的 mask 时，还要用**同一个中央区域**，也就是 `own_bbox`。

1. **执行路径一致**：读取区域相同时，Step1 和 Step2 一致吗？
   - 比较两条路径实际送进模型的**输入数组**（逐元素相等）、实际传给库的**参数**（kwargs 相等），以及**输出**。
   - 输出比较以设备相同为前提。设备不同时，按 7.10.6 事先定下的指标来比较。
2. **应用侧没有额外拉伸，引擎只执行它那一套预处理**：
   - **输入数组要按方法分别构造后再比较**，不能一律写成「等于两通道的 fusion 数组」。设 `F` = fusion 输出（uint16，已经做完多边形置 0）÷ 65535，`F[...,0]` 是 fusion 通道，`F[...,1]` 是核通道：

     | 方法 | 送进引擎的数组必须正好等于 |
     |---|---|
     | Cellpose whole-cell | `stack([F0, F0, F1], -1)`：fusion 复制一份 |
     | Cellpose nuclei（含 expansion）、StarDist ×2 | `F1` |
     | Mesmer whole-cell、nuclear-guided | `stack([F1, F0], -1)`：通道顺序对调 |
     | Mesmer nuclei | `stack([F1, 0], -1)` |

   - **引擎内部的预处理，检查具体的处理步骤和参数，不能只数 `normalize()` 被调用了几次**：
     - Cellpose：`normalize=True`，没有额外的 `lowhigh` 或 `percentile` 覆盖；
     - StarDist：只有一次 `normalize(img, 1, 99.8, axis=(0,1))`，作用在上表的输入上；
     - Mesmer：调用 `app.predict` 时不传 `preprocess_kwargs`，也就是按 DeepCell 的默认流程：`threshold=True, percentile=99.9, normalize=True, kernel_size=128`。
   - 同一个细胞在不同的窗口或 HALO 下，定标后**可能不同，幅度尚未实测**。用户已经接受（R11 修订版），**不作为验收项**。
- Step2 跨切块合并后的接缝区域，另外做针对性验收，不提前保证所有像素完全一样。


#### 7.11.5 定标规则（R11 修订版，用户裁定 2026-09-23）

**两层，分清楚**：
1. **用户的设置**：显示窗口（min/max/gamma），以及通道、组、核的权重。它们在 fusion 里全局生效（R13），随运行快照冻结。**这一层不算自动定标。**
2. **模型的自动定标**：保留，在**当前送进模型的那张图**上计算（Step1 是 patch 加 HALO，Step2 是切块加 overlap）。应用侧**不再额外自动拉伸**；每个引擎只执行**一套**下表列出的标准预处理流程，而且只执行一次。这里说的是「一套流程执行一次」，不是「只允许一次数学变换」：Mesmer 的那一套流程本身就包含截断、拉伸和 CLAHE 三步。

**每个引擎保留的那一套标准预处理**：

| 方法 | 保留的那一套预处理 | 位置 |
|---|---|---|
| Cellpose ×3 | 模型自带：`eval(normalize=True)`，每个通道取 1–99 百分位 | 引擎内部（`cellpose/models.py:274-292`） |
| StarDist ×2 | 库本身不做定标，保留**我们代码里唯一的一次** `normalize(1, 99.8)` | 引擎模块内，只保留一处 |
| Mesmer ×3 | DeepCell 自带的默认预处理：99.9% 截断，按最小/最大值拉到 0–1，再做 CLAHE（`mesmer.py:62-70`） | 引擎内部，调用时不传 `preprocess_kwargs` |

**新流程里不再调用的多余定标**（逐处排查的结果；在 V1/C、V2 里落实，由验收第 2 条把关）：
- 要求是**新流程不再经过这些分支**，**不是**把旧代码整个删掉。
- 旧的参数文件（比如手选膜通道、`normalize_input=True`）在 Step2 里**仍然按原来的语义执行**。
- 如果要改变旧项目的行为，需要用户另外明确决定。

| # | 位置 | 现在做了什么 | 处理 |
|---|---|---|---|
| 1 | Step1 纯核方法：`loader.read_region(DAPI, normalize=True)` → `_norm`（`core/io_loader.py:294-302`，`workers/cellpose_worker.py:540`） | 按当前区域取 p1–p99.5 | 新流程不再调用：纯核方法改为从 fusion 结果里取核通道（R13） |
| 2 | Step1 纯核方法：`fusion._normalize_intensity`（`cellpose_worker.py:544`） | 按 patch 做 min-max | 新流程不再调用 |
| 3 | Mesmer：`build_mesmer_input` 对没有窗口的通道调用 `normalize_percentile`（`utils/mesmer_utils.py:235-236`） | 按区域取百分位，之后 DeepCell 又会再做一次 | 新流程不再调用 `build_mesmer_input`。旧的手选膜通道参数仍然走它，行为不变 |
| 4 | Mesmer Step2：`build_mesmer_input_from_fused_tile(normalize=True)`（`mesmer_utils.py:103-112`） | 对 fused 切块按百分位拉伸，之后 DeepCell 又会再做一次 | 新流程写出的参数带 `normalize_input=False`，所以不会进入这一步；旧参数文件里没有这个键或值为 True 的，行为不变 |
| 5 | `FusionEngine.compute(prenormalized=False)`（`core/fusion_engine.py:87-108`） | 每个通道做 min-max | 目前模型输入路径上没用到，因为 `fuse_fullres` 传的是 `prenormalized=True`。在 V0 的归属表里确认没有其他调用方 |

**不算定标的操作**：fusion 输出 ÷ 65535 只是换一下单位，保留。多边形以外置 0 也保留。

**Mesmer 的 CLAHE**：用户裁定**保留**（2026-09-23）。它和截断、拉伸一起，算作 DeepCell 自带的那一次预处理。

**Mesmer 的「Fusion 当膜通道」路径，现状与要改的地方**（已核实）：
- **Step2**（`_validate_mesmer_config` → `fused_zarr` → `build_mesmer_input_from_fused_tile`，`utils/mesmer_utils.py:103-112`）：
  - 通道排法已经是 `[核, fusion]`。对新流程来说，只需要不进入它自己做的那次百分位拉伸，也就是上表第 4 处。
  - **已核实的新发现**：Mesmer nuclei 的 `input_mode="DAPI only"` 不在 `_mesmer_uses_selected_channels` 的集合里（`segment_merge_worker.py:1713-1720`），所以也会走 `fused_zarr` 路径，第二个通道拿到的是 **fusion，而不是 0**。Step1 的「DAPI only」给的却是 0（`mesmer_utils.py:243-244`）。两边现在就不一致。
  - 新流程的共用构造函数按 7.11.4 的表执行：nuclei 的第二个通道为 0。旧参数文件在 Step2 里保持原来的行为。
- **Step1**（`workers/mesmer_worker.py:172-190` → `build_mesmer_input`）：
  - 膜通道取 `fuse_fullres(...)[:, :, 0]`。这是**没有 ÷ 65535 的 uint16**，之后靠 `normalize_percentile` 拉到 0–1。去掉这次拉伸时，**必须补上 ÷ 65535**，否则 DeepCell 拿到的就是 0–65535 的数值。
  - 核通道是单独按窗口读的原始 DAPI，**没有乘核权重**，违反 R13。改为取 fusion 的核通道。
  - 改完以后，Step1 和 Step2 走的是同一个构造函数。按 R14，只保留 `build_mesmer_input_from_fused_tile` 这一条路，Step1 把 `fuse_fullres` 的结果交给它。
- **新界面**（**用户裁定，2026-09-23**）：Mesmer 的膜通道**只提供「Fusion」一种**。「手选膜通道」（`selected_channels`）模式要单独读原始通道，绕开 fusion，和 R13、R14 冲突。它在新界面里不显示，代码按 R2 的做法保留，Step2 仍然兼容。

**已知的后果**（推理，**未实测**，用户已知悉）：
- 局部定标会抵消「整体亮度倍数」。比如核权重 0.6 这种对整张图的统一缩放，对纯核方法基本不起作用。
- 但 gamma 的曲线形状，以及通道之间、组之间的相对权重，都会保留，因为它们是在定标之前混合进去的。

### 7.12 架构大纲：一套轮子，方法模块化（R14）

**目标**：
- Step1 和 Step2 都只是**基座**：Step1 驱动「patch × 组合」，Step2 驱动「切块 × 一个组合」加合并和恢复。
- 分割方法是**模块**，两个基座调用的是同一批模块。
- 同一个功能只保留一份实现。

**共用组件**（每一项只有一份实现，Step1 和 Step2 都调用它）：

| 组件 | 内容 | 现状（重复的轮子） | 落在哪一块 |
|---|---|---|---|
| 方法注册表与参数模式 | 方法 → 引擎、参数的类型、范围、精度和默认值、参数校验、`combo_id` | 注册表只有默认值；Step1 和 Step2 各有一套控件和读参逻辑（7.1、7.8） | B（Step1），E（Step2 装载） |
| 输入准备 | 带 HALO 的读取范围 → fusion（窗口、权重、gamma）→ uint16 → 多边形置 0 → 按方法构造模型输入（R10）（不做任何自动定标，定标只在引擎内做一次，见 7.11.5） | Step1 用 `fuse_fullres`，Step2 用 `FullFusionWorker._fuse_tile`；纯核方法在 Step1 里绕过 fusion（R13）；Mesmer 另有 `build_mesmer_input` 和 `build_mesmer_input_from_fused_tile` 两条路 | V1/C，V2 |
| 引擎模块（方法插件） | 每个引擎一个 runner，方法是引擎内部的配置；推理和引擎侧后处理（expansion、`min_size`、`postprocess_mask`） | Step1 的 `cellpose_worker` 和 `mesmer_worker`，Step2 的 `segment_merge_worker._segment_tile`，各自调用一遍模型；StarDist 在 Step1 走子进程，在 Step2 走主进程 | V0（原型），V1/C，V2 |
| 归属与重编号 | 质心、半开区间、LUT、共享标签的输出共用同一张 LUT | Step2 生效的内联代码（`:2538-2575`），加上一份只做影子对比的 `CentroidOwnershipMergePolicy` | V1/C（抽出），V2（Step2 改为调用；停掉影子对比） |
| 结果记录与原子发布 | 7.3 的记录格式和写盘顺序 | Step1 写 npz，Step2 有自己的输出 | V1/C；Step2 的输出格式不在本计划里统一，只增加引擎身份字段 |
| 引擎身份与设备 | 7.10.5 | 没有 | V0 |

**做法**：
- **共用组件由搬迁得到，不重写。** 以 Step2 生效的代码为准，原样抽成共用函数，Step2 改为调用它。
- **验收分成两类，不能混在一起**：
  1. **纯搬迁**（只是把代码抽成共用函数，不改行为）：配回归测试，要求搬迁前后 Step2 的输出**完全相同**。
  2. **落实新的输入契约**（R10–R13：通道排法、输入来源、去掉多余定标）：这会**有意改变**结果，**不和旧结果比较**，不能让旧结果的基线挡住新规则。改为验证：读取区域、配置和设备都相同时，Step1 和 Step2 一致（7.11.4）。
  - 每次提交只做其中一类。先纯搬迁、测试通过，再改行为。这样一旦结果变了，能分清是搬迁出了错，还是新规则带来的预期变化。
  - 这就是 7.11.3 里「不需要证明两份实现一致」的前提：本来就只剩一份实现。
- **影子对比**：V2 里停掉 `_merge_policy_shadow_compare` 的调用（`segment_merge_worker.py:2584`）。
  - `CentroidOwnershipMergePolicy` 类还被 `utils/tile_scheduler.py` 和 `tests/test_merge_policy.py` 引用，是否删除另立清理任务。
- **边界**（遵守 `AGENTS.md`）：
  - R14 是**方向**，不是一次性大重构的授权。每一个组件的抽取和替换，都在上表对应的块里做，按块的白名单单独批准。
  - HQ、HQ2、CDS 按 R2 保留原样，不纳入这次模块化。
  - Step2 的切块、合并和恢复机制保留（V3 裁定）。

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
