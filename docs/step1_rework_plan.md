# Step1 改造行动计划（审核版）

- 状态（2026-09-16）：计划审核通过。**块 A 已实现并提交 `69ecc75`，自动门与变异闸门全绿，真机验收待用户完成**；块 B/C/D 未开工。执行一块一停；B 开工须遵 B.3 的来源身份收紧条款，C 接管须落实 C.3 门 7 的保护回归；Step1.5 单列。
- 块 A 的真机验收项（用户执行，尚未通过）：缩放窗口时 Viewer 宽度随窗口增长且左右约 1:2；切到 Patch Results 时 Viewer 完全隐藏、切回恢复；左侧两 tab 来回切换后通道勾选、当前名称、显式 0.0 与其他权重不变；720p 高度下 Method & Parameters 可滚动且 Save/Generate 可达。
- 基线：HEAD = `309c9db`（`v15-interactive-channel-workspace`）。
  - 基线数字 **18 个测试套件 / 881 passed / 0 failed** 是**执行窗口在 309c9db 上跑出并已确认的结果**，覆盖的是该窗口指定的 Block01 套件集合，**不等于仓库全部测试通过**。其中 `test_controller_trajectory` 1 passed、`test_step0_full_image_recovery` 19 passed、`test_step0_compare_tiles` 190 passed、`test_explore_controller` 134 passed。
  - 本轮不重跑基线。执行时以同一套件集合对比，判据是**无新增回归**。
  - **历史挂账的处置（据本会话提交记录）**：`FusionDomainModel.deferred_notices()` 的异常回滚已在 `b120032` 修复；`test_step0_compare_tiles` 的旧契约、`test_controller_trajectory`、`test_step0_full_image_recovery` 三条长红已在 `309c9db` 收口（仅改测试，未动生产代码）。因此它们**不再是未决挂账**。若执行时发现与此记录不符，按"无新增回归"处理并单列上报，不纳入本轮修复。
- 本文件是 A–D 四块的唯一权威计划。执行以块为单位，一块一停，每块独立评审、独立验收、独立回滚。
- 最高规则仍是 `UI_SURFACE_RULES.md`：组件全局化只授权复用既有组件的实例/状态/样式/生命周期，不授权任何新的可见控件。本计划中唯一获准新增的可见信息是 **B.4 的 viewer 内缺失提示**（用户明确授权，非按钮、非窗口）。

---

## 0. 已核实的事实（本计划的地基）

| 事实 | 出处 |
|---|---|
| Step1 页面在 `main_window.py` 内构建，不是 `step1_5_bg_page.py` | `ui/main_window.py:604-995` |
| 现为三栏 `setSizes([320,450,450])`、factor 2:3:4，整页包在 `QScrollArea` 内 | `ui/main_window.py:936-939, 988-995` |
| Step0 的 Channels:viewer 比例为 1:2 | `ui/step0/step0_page.py:1304` |
| Step0 的 Intensity 入口在 Channels 框顶行右侧 | `ui/step0/step0_page.py:829-845` |
| Step1 现用裸 `pg.ImageItem` 画 patch，无金字塔/相机/调度 | `ui/main_window.py:851-861` |
| Step0 viewer 栈由 Step0 页自持，含预览方法与 GPU 让渡生命周期 | `ui/step0/step0_explore_tab.py:314-364`, `:586-624` |
| `build_default_stack` 组装的下层通用件：RawTileProvider / TileScheduler / LRUByteCache / TileGridSpec / ExploreView / ExploreController / RawOverlayLayer | `ui/step0/step0_explore_tab.py:239-247` |
| 选源判据是**最终决断**（仅 tophat/cucim）+ 产物可读，不是"数组能打开" | `core/io_loader.py:66-76, :88` |
| roi_only 产物要求请求矩形**完整落在一个 ROI bbox 内**，否则返回 None | `core/io_loader.py:216-234` |
| 现行回落会走原始 OME 并可能实时校正 | `core/io_loader.py:109-111` |
| 校正通道**没有金字塔**，现行代码对其回落全分辨率读取 | `core/io_loader.py:151-156` |
| Step1 patch 读取是 `normalize=False`；自动窗口发生在**合成层** | `ui/main_window.py:3933`；`core/preview_compose.py:180, :194-195` |
| Overlay 合成含权重，`weight<=0` 是**丢弃通道**，非乘零 | `core/preview_compose.py:227-231` |
| Fusion 是组内加权后 **组间取 max**，nucleus 独立；缺席通道跳过而非当 0 | `core/fusion_engine.py:54-64` |
| `RawOverlayLayer` 是刻意限定的单层、独立到达、Qt 加法绘制 | `viewer/explore_view.py:1712` |
| 显示窗的真正规则：共享状态 `mapping_or_seed`，缺失时用**整片低分辨率数组**走 `seed_display_range`，配 `ensure_lowres` 后台到达 | `ui/step0/step0_page.py:7984, :8099`；`core/display_mapping.py:33-51` |
| `_pick_calibration_windows` 是校正增益标定用的组织密集窗口选择，**与显示窗无关** | `viewer/explore_view.py:889-926` |
| `MultiChannelPrefetchController` 现职是视图稳定后预取邻近通道的校正缓存，**不是**可见通道合成预取器 | `viewer/multichannel_prefetch.py:27` |

---

## 1. 贯穿约束

1. **不动 Step0**：`Step0ExploreTab`、`build_default_stack`、预览方法（both/original/tophat/cucim）、`release_for_production` / `resume_from_production` / `teardown` 生命周期、floor 缓存、Tissue Preview、Intensity、最终 Per-Channel Decision 的解耦，一律不改。Step1 自建宿主，复用的是**下层通用件**。
2. **证据规则**：契约按性质给证据——配置、调度、实际绘制各给各的。信号计数或假 controller 的调用记录**不能替代**像素重画证据；反之也不为凑"每块都有像素门"把配置类契约强行像素化。
3. **回归门**：以基线为准判**无新增回归**。已单列的既有失败保持单列，不作为本轮隐含修复任务。
4. **表面同步**：凡动 `UI_SURFACE_RULES.md` §4 记录的表面，规则文件与 `tests/test_ui_surface_contract.py` 同笔更新。
5. **纪律**：不 push、不 amend、不 squash、不 rebase、不 `reset --hard`；不碰受保护文件；人工验收由用户在真机完成。
6. Step1.5 清理**单列**（见第 7 节），不作为任何块的前置，不在本轮实施。

---

## 2. 块 A — 布局与入口

**前置**：无。

### 范围（全部在 `ui/main_window.py:604-995`）

| 改动 | 位置 |
|---|---|
| 三栏降两栏，`setStretchFactor(0,1); (1,2)`，删 `setSizes([320,450,450])` | :936-939 |
| 左栏改 `QTabWidget`：`Channels` \| `Method & Parameters` | :620-745 / :870-925 |
| 右栏改 `QTabWidget`：`Viewer` \| `Patch Results`（切换显示，不堆叠） | :865-925 |
| 删外层 `page1_scroll` | :988-995 |
| 标题 → `Step 1 — Channel Fusion + Preliminary Segmentation` | :610 |
| Tissue Preview 按钮移到标题行最右 | :635 |
| Intensity 移入 Channels tab 内，对齐 Step0 位置 | :648 → 参照 :829-845 |
| 删 `① ROI / Patch Overview` | :630 |
| 删 `_preview_title` 整行及两处 `setText`（含 `Red=cyto Blue=nucleus` 图例） | :763 / :4225 / :4227 |

viewer 内容本块不动：`prev_gv` / `prev_img` 原样搬进右栏 `Viewer` tab。

### 验收门
1. **比例**：`show()` 后等布局稳定，在 1280 / 1600 / 1920 实测左右实宽比落在 **1:2 ±10%** 且随宽度单调；不要求与 Step0 像素级相等。
2. **dock**：切到 `Method & Parameters` 后，dock **未被显式 `setVisible(False)`、未被 unmount**，`parent()` 与通道行数/勾选/权重不变；切回 `Channels` 后 `isVisible()` 恢复为真。非当前 tab 的 `isVisible()` 不作判据。
3. **矮窗**：720p 高度下 `Method & Parameters` 内控件与底部 Save/Generate 仍可达。
4. **契约**：Intensity 在 Channels 内；标题行仅一个 Tissue Preview；无预览子标题。

### 变异闸门
- 故意设错两栏比例（如 1:1）→ 门 1 红
- tab 切换处插显式隐藏/卸载 → 门 2 红
- 去掉 Method 面板自身滚动 → 门 3 红
- Intensity 挪回左栏裸位 → 门 4 红

### 真机检查
缩放窗口，viewer 面积随宽度单调增长且不被 Patch Results 抢走；两侧 tab 来回切换后通道行、勾选、权重不变。

### 回滚
单笔 `git revert`，无数据迁移、无新文件。

---

## 3. 块 B — 全片 viewer、选源与导航

**前置**：块 A 真机验收通过。

**新增文件**：`ui/step1_viewer_host.py`。自组 provider / scheduler / caches / grid / `ExploreView` / `ExploreController`，外加来源解析器。`Step0ExploreTab` 一行不动。

### B.1 来源表（用户裁定）

选源判据对齐 `io_loader`：通道算"校正"当且仅当**最终决断 ∈ {tophat, cucim}** 且**校正产物可读**。

| 瓦片与 ROI 的关系 | 通道状态 | 画什么 |
|---|---|---|
| 完整落在 ROI 内 | 校正（决断 + 产物可读） | 校正产物像素 |
| 完整落在 ROI 内 | 决断为 original | 原始 OME 像素 |
| 完整落在 ROI 内 | 决断为校正、产物不可读 | **该通道不画**，viewer 内提示（B.4） |
| 完整落在 ROI 外 | 任意 | **不画**（无像素；非黑底填充，非原始补齐） |
| 跨 ROI 边界 | 校正 | **只画 ROI 内部分**，ROI 外留空；不整块退化、不补原始 |
| 跨 ROI 边界 | 决断为 original | ROI 内部分画原始，ROI 外留空 |

- **坐标与导航**：viewer 的坐标系与可导航范围仍是整张切片（`RawTileProvider.level_shape(0)`）。ROI 外只是没有像素；相机可以去，画面为空，状态行如实说明，不报错不崩。
- **明确不做**：瓦片路径**不调用** `_apply_configured_correction`；不改写 Step0 预览方法或现有执行链。

### B.2 粗层与边界算法约束
1. **统一下采样网格**：盒式平均对齐**全局网格原点**（level-0 原点 + 倍率 k），不得从瓦片或 ROI 的局部左上角起算。
2. **空白不参与平均**：ROI 外无像素，**不得以 0 混入**边缘块均值；值与有效掩码分别累积，每个粗层像素按**有效样本数**归一；计数为 0 的输出像素标为无像素。
3. **绘制裁切**：最终绘制严格限制在 ROI 内，部分有效的边缘粗层像素按裁切规则处理，不外溢。
4. 粗层是对**校正像素自身**下采样并按 level 缓存，不拿原始金字塔充数。

### B.3 Intensity 契约
> Step1 以 `ChannelDisplayState` 中**有效的 Intensity 映射为权威**，手动值与已求得的自动值一律照用，读点与 Step0 相同（`display_window` → `mapping_or_seed`）。缺少有效映射时，沿用现有全局求窗规则与后台初始化机制（`_seed_display_mapping` / `seed_display_range` / `ensure_lowres` 到达路径）：输入是**已确定来源的完整有效图像或其固定全局代表图**（ROI-only 校正产物 → 其完整校正 ROI；原始 → 整片低分辨率代表图），绝不取当前视口。
>
> Step1 宿主**不另存**任何与 Intensity 面板不一致的自动窗口；不新建全局模型，复用现有共享服务。
>
>
> **来源身份的作用范围（收紧）**：来源身份用于**校验自动求窗结果与像素缓存**，不是清窗开关。**切换步骤、视口或 patch 一律不重置已有有效窗口**。既有**手动 Min/Max/Gamma 不得被自动初始化覆盖**。自动窗口的来源失效处理须**经共享状态统一完成**，不在 Step1 宿主另设窗口，也不得改变 Step0 的窗口行为。
>
> **写回优先级**：后台求窗结果**不得覆盖用户刚输入的新值**——写回前比对共享状态当前值的来源与修订号，用户值优先。

禁止：`_norm` 的按区域百分位、`compute_qupath_auto_minmax(当前瓦片)`、任何以视口内容为输入的窗口估计。

**缓存键**：底层瓦片键 = 数据集身份 + 通道 + **来源身份**（raw / 某 ROI 的校正产物 + 产物身份戳）+ 来源版本 + level + 瓦片坐标；显示层键另含 Intensity 参数（手动值，或自动窗口的来源标识 + 求得值）。

### B.4 缺失产物提示（本轮唯一获准新增的可见信息）
- 位置：大 viewer 内的信息层，列出「通道名 + 缺失原因」（产物不可读 / 该 ROI 无此通道数组）。
- **不新增按钮、不新增窗口、不新增 dock**。其他来源有效的通道照常显示。
- `UI_SURFACE_RULES.md` §4 同笔登记；契约测试断言：该提示不是按钮、不可点击、不产生新顶层窗口。

### B.5 模式边界
B **不引入**任何用户可见的新模式。Step1 用户可见模式仍只有 Overlay / Fusion，由旧路径服务；新宿主在 B 阶段**界面不可达、仅测试可达**。切换在 C 完成。

### B.6 退出门（全部在真实 `ExploreView` 的测试宿主内；**B 不设真机门**）
1. **三类瓦片**：完整 ROI 内 / 完整 ROI 外 / 跨边界——ROI 内像素等于指定来源直读值；ROI 外无像素项（不是全零图）；跨边界瓦片 ROI 内子区等于校正直读值、ROI 外子区无像素。
2. **缺失产物**：决断为校正、产物不可读的通道，**无任何该通道像素上屏**，提示项存在且含原因；同场景另一有效通道正常上屏。
3. **Intensity 稳定性**（拆三条）：
   a. 同一 level、同一源像素，在不同视口与 patch 跳转后显示值一致；
   b. 各 level 使用同一组 Min/Max/Gamma，给定相同输入数值映射输出一致（直接施于映射函数，不经采样）；
   c. 粗层像素符合 B.2 的下采样公式。
4. **求窗次数**：同一来源、同一有效缓存周期内最多初始化一次；共享状态已有有效窗口时新 viewer 求窗次数为 **0**；来源变化或用户主动重求时允许再算。
5. **手动窗口往返**：Step0 手动设置 Intensity → 进入 Step1 → 返回 Step0，数值**保持不变**，两个 viewer 均服从该数值；期间自动初始化不得写入覆盖。
6. **粗层边界专项**：`ROI 起点不能被下采样倍率整除`的跨瓦片案例——(a) 粗层网格相位与全局一致，不随瓦片偏移；(b) ROI 边缘无暗线（等于"有效样本均值"手算值）；(c) 绘制不越界。
7. **无 patch 可显示**：已有有效数据源与 ROI、**patch 列表为空**时，仍能显示有效范围并可经 Tissue Preview 导航。
8. **导航**：patch 跳转 / Tissue Preview 空降落位正确且视口内瓦片像素到位；跳到 ROI 外画面为空且状态行说明。
9. **生命周期**：数据集切换先完全 teardown，无线程/句柄泄漏。
10. **无新增回归**，Step0 保护门全绿。

### B.7 变异闸门
- 来源表任一行判据反转 → 门 1 红
- 产物缺失改画原始 → 门 2 红
- 自动窗口改为逐瓦片估计 → 门 3a / 门 4 红
- 自动初始化覆盖已有手动值 → 门 5 红
- 粗层改读原始金字塔 → 门 3c 红
- 边缘均值把 ROI 外当 0 混入 → 门 6b 红
- **注入错误坐标 / 遗漏必要的导航状态更新**（不更新当前 bbox 或不发起请求批次）→ **B.6 门 8** 红。任一必要契约被破坏测试即失败，**不要求**相机与像素同时出错

### B.8 回滚
撤销本块的提交及其引入的文件（含 `ui/step1_viewer_host.py`），不机械执行"删文件 + revert"以免误伤其后的工作。旧路径始终在位，Step1 可原样退回。

---

## 4. 块 C — Overlay / Fusion 真实像素

**前置**：块 B 退出门通过。

### C.1 合成公式（不重写）
- **Overlay** 复用 `preview_compose.overlay_rgb_u8` 语义：`weight<=0` **丢弃通道**、`gray*weight`、上色相加、clip。
- **Fusion** 复用 `fusion_engine.fuse_channels`：组内 `clip(gw · Σ w·signal)`，**组间取 max**，nucleus 独立；缺席通道跳过而非当 0。优先直接调用该函数，不另写一份。
- `RawOverlayLayer` 推广到 N 层前，先用像素门证明 Qt 加法绘制与 CPU 合成同输入一致；不一致则改为 CPU 合成后上屏，性能问题留给 D。

### C.2 Intensity 与缓存
- 合成输入信号一律来自 **B.3 的全局映射**；**不在合成层**重新调用 `compute_qupath_auto_minmax(当前数组)` 或 `_norm`。
- 底层瓦片键不含权重、颜色、模式（同 B.3）。
- 合成键 = 参与的底层键集合 + 模式（overlay/fusion）+ 勾选集合 + 各通道权重 + 组与组权重 + nucleus 权重 + 各通道 Intensity 参数 + 颜色。
- **世代号**：旧世代的迟到结果一律拒收，不上屏。

### C.3 门
1. **手动窗口下**：新 viewer 合成结果与 `overlay_rgb_u8` / `fuse_channels` 同输入逐像素一致。覆盖：权重 0.0（丢弃）、权重 1.0、同色两通道叠加、异质组权重、构造使组间 max 与 sum 结果不同的场景、某通道瓦片缺失。
2. **自动窗口下**：**不设新旧逐像素相等的硬门**。验收 (a) 全局一致性（同层同源跨视口显示值一致；求窗次数符合 B.6 门 4）；(b) 公式符合性（以全局窗为输入，合成结果与现有公式逐像素一致）。
3. **重合成不重读**：改权重不增底层读盘计数，仅增合成计数。
4. **旧世代拒收**：覆盖 (a) 快速拖权重、(b) **数据集切换**、(c) **来源 / ROI 变化**、(d) **后台求窗结果迟到**，各注入一个旧世代结果，断言均被拒收且不上屏。
5. **draft/committed 分离，含旧 session 恢复**：恢复后 `committed_snapshot()` 为恢复值、viewer 跟 `draft_snapshot()`；draft 未 commit 时 Search/Generate 仍用 committed。
6. **旧语义未动**：旧 patch 路径（`normalize=False` + 合成层 auto）与 Search/Generate 的映射规则保持原样（回归绿即为证）。
7. **科学状态保护回归**（配置/调用证据为主，涉及显示处加像素证据）：
   a. **首次启用**：无既有权重的通道首次勾选 → 权重 1.0，同时**显示**并**参与 Fusion**；
   b. **权重保留**：显式 0.0 与旧项目的异质组权重**不得被默认值覆盖**；取消勾选再勾选**不丢**已有权重；
   c. **点击名称**：保留现有选中行为，**不额外改变**勾选与科学权重。
8. **零权通道**（两类证据）：配置/调用测试断言零权通道**未进入映射与合成输入集合**（`channel_gray` 未被调用）；像素测试断言其对结果无贡献。

### C.4 切换门（正常应用内，新 viewer 接管 Overlay/Fusion 之后的真机验收）
- Step0 → Step1 视口位置连续（像素可以不同：Step0 可能在看某预览方法，Step1 遵最终决断，属合法）
- ROI 边界来回平移，ROI 内亮度不随视口变化
- 缺失产物提示可见且文案正确
- Overlay / Fusion 两模式真机验收

### C.5 变异闸门
- 组间 max 改 sum → 门 1 红
- 合成层重新按数组求 auto → 门 2a 红
- 权重进底层键 → 门 3 红
- 去掉世代拒收（任一情形）→ 门 4 红
- 恢复路径直接写 draft → 门 5 红
- 零权通道改为乘零参与映射 → 门 8 的调用测试红
- 首次勾选给 0.0 而非 1.0 / 重勾选时用默认值覆盖已存权重 / 点击名称顺带改勾选 → 门 7 对应子项红

### C.6 回滚
revert 本块，Step1 退回块 B 状态（旧路径服务 Overlay/Fusion）。

---

## 5. 块 D — 流畅度优化与旧路径退役

**前置**：块 C 切换门通过。

### D.1 先测后改
基线指标，Step1 与 Step0 同机对比：首帧时间、平移/缩放帧间隔（p50/p95）、清晰瓦片到达时间、常驻内存峰值、每手势读盘次数。脚本落 `scripts/`（参照 `scripts/benchmark_tissue_frame_scheduler.py`），结果写入提交信息。

### D.2 手段由瓶颈决定
`MultiChannelPrefetchController` 现职是"视图稳定后预取邻近通道的校正缓存"，**不是**现成的可见通道合成预取器；Step0 的 `_floor_cache` 也不能直接当校正 ROI 的粗层方案。是否复用、怎么改，按实测决定。Odon 只作思路研究（视口驱动、多分辨率、多通道高效合成），不预设引入其源码；是否需要 GPU 由瓶颈决定。

### D.3 指标优先级（不要求同时改善）
- **P0 像素正确性**：B/C 全部门保持绿，任何优化不得以此换性能
- **P1** 首帧时间
- **P2** 平移/缩放 p95 帧间隔
- **P3** 内存**上限**：绝对数值**在 D 开始时、任何优化动手之前**依基线实测固定下来并写入提交信息；此后不得上调。不要求相对基线下降
- 读盘次数仅记录，允许为降低等待而上升

### D.4 门（确定性，不用噪声敏感的性能红线）
1. **请求优先级**：可见瓦片请求排在预取之前（断言调度队列顺序，不测时钟）。
2. **取消**：视口移动后旧视口的未决预取被取消，其迟到结果不上屏。
3. 各项指标写入提交信息；未改善 P1/P2 的手段不合入。

### D.5 退役笔（单独提交）
前置：C 的门全绿且新 viewer 已承接 Overlay/Fusion 两模式。删 `prev_gv` / `prev_img` / patch 数组缓存 / `⟳ Update` 强制重读路径（届时若已无意义）。

### D.6 回滚
优化各笔可单独 revert；退役笔单独 revert 即恢复旧绘制路径。

---

## 6. 分块顺序与停止点

```
A（布局与入口）── 真机验收 ──►
B（全片 viewer、选源与导航；界面不可达）── 退出门 ──►
C（Overlay/Fusion 真实像素；接管用户模式）── 切换门/真机验收 ──►
D（先测后改；最后退役旧路径）
```

每块之间必须停下等用户指令。未获新请求不得顺手整理其他代码。

---

## 7. 单列任务：Step1.5 移除（不进本轮四块）

**删**：`ui/step1_5_bg_page.py`；`ui/main_window.py:84`（import）、`:1020-1024`（构建入栈）、`:3276-3298`（`_go_to_step1_5`）；核实后清理 `:1752 / :3431 / :3468` 三处分支（只删确证死路）；`tests/test_main_window_step1_5.py`。

**改**：`tests/test_step1_navigator_policy.py:337`、`tests/test_step1_handoff_invalidation.py:331` 去掉调用；`tests/test_channel_workbench.py`（695 / 784 / 889 附近）三处宿主改挂 Step0 conditioning host。

**保留历史兼容**：`utils/channel_remap_config.py` 的 `CREATED_FROM_STEP1_5_CONDITIONING`、`utils/remap_promotion.py:142`、`ui/main_window.py:6788` 旧路径回退。

**闸门**：改坏 `:6788` 旧路径 → `tests/test_remap_promotion.py` / `tests/test_step2_remap_integration.py` 必须红。

---

## 8. 已裁定的产品契约（不再重开）

- **B-1** ROI 外不画；跨边界只画 ROI 内部分，不补原始、不整块退化。
- **B-2** 校正产物缺失 → 拒绝显示该通道，viewer 内提示原因；其他有效通道照常显示。
- **B-3** 瓦片路径不做临时校正（不调用 `_apply_configured_correction`），不改 Step0 预览方法与现有执行链。
- **B-4** Intensity 是图片全局的，不是视口/瓦片/patch 的；手动值优先；自动窗口从来源的完整有效范围求得并复用。
- **Step1 行的权重编辑＝启用命令**（2026-09-16 裁定）：给一个通道权重＝显示它并把它放进 fusion，保留用户给的数值；权重回到 `0.0` **不**停用该通道（显式 0.0 是一个决定）；取消勾选是唯一把通道移出的手势。同步记于 `UI_SURFACE_RULES.md` §4。
- 右栏 `Viewer` | `Patch Results` 两 tab 切换显示，不堆叠；左栏 `Channels` | `Method & Parameters` 两 tab；比例沿用 Step0 的 1:2。
- 标题 `Step 1 — Channel Fusion + Preliminary Segmentation`；Tissue Preview 在标题行最右；Intensity 在 Channels tab 内并对齐 Step0 位置；删 `① ROI / Patch Overview`、预览子标题与红/蓝图例。
