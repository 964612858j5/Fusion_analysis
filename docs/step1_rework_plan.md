# Step1 改造行动计划（审核版）

- 状态（2026-09-17）：**块 A 真机验收通过并已收尾**；**块 B 实现与退出门完成**（起始基线 `c494cd7`，提交 `3bb9b95` B1 → `67b1df8` B2 → `ecddc82` B2.1 → `e447e57` B3 → `52a691b` B4 → `b04e71f` B4.1）。**块 C/D 未开工**。执行一块一停；C 接管须落实 C.3 门 7 的保护回归；Step1.5 单列。
- 块 B 的实际产出：**3 个生产文件**（`viewer/step1_source.py`、`ui/step1_viewer_host.py`、`ui/step1_viewer_binding.py`）+ **4 个测试模块**（`tests/test_step1_source_table.py`、`test_step1_viewer_host.py`、`test_step1_viewer_binding.py`、`test_step1_viewer_exit_gates.py`）。
- 块 B 结束时的回归：**141 文件 / 3066 passed / 16 failed**，失败文件与逐文件计数与 `309c9db` 完全一致（hq_marker_segmentation 2、preview_source_provider 1、step0_channel_conditioning 6、step0_no_process_button 1、step0_process_incremental 5、tissue_navigator_viewport_sync 1）。
- **块 B 的三条状态说明（勿误读）**：
  1. 宿主、binding 与导航链**已有生产实现**，但正常界面**未实例化、未 mount**——仅测试可达（B.5 复裁）。
  2. ROI 外的"此处无可显示像素"目前只是**宿主的局部状态属性与 `status_changed` 信号**；用户可见的信息层随 **C 接管 viewer** 时落地（B.4）。
  3. 块 B **不做真机可见验收**；全片 viewer 的用户可见验收在 **C 完成 Overlay/Fusion 接管之后**进行。
- 块 A 相关提交：`69ecc75` / `f2bce5a` / `d6700ed` / `5b24a11` / `70cc81a` / `ee2cb52` / `e388275` / `c494cd7`。回归基线随之更新为 **137 文件 / 2983 passed / 16 failed**，失败文件与 `309c9db` 相同。
- **Step0 现行显示规则**（块 B 起点的既定事实）：新片子只显示细胞核；点击某 marker 或勾选其复选框都表示"显示它"，并自动取消上一个 marker；细胞核为参考层，不被这两个手势移动。Step1 不受此规则约束（其勾选是融合命令，可同时多选）。
- 块 A 的真机验收项（已由用户在真机通过）：缩放窗口时 Viewer 宽度随窗口增长，且左右占比与 Step0 一致（实测 Step0 左栏约 0.278，**不是 1:2**）；切到 Patch Results 时 Viewer 完全隐藏、切回恢复；左侧两 tab 来回切换后通道勾选、当前名称、显式 0.0 与其他权重不变；720p 高度下 Method & Parameters 可滚动且 Save/Generate 可达。
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
| Step0 的 c_split stretch 因子写的是 1:2，但**实测占比为 0.278 / 0.722（≈1:2.6）** —— 左栏初始宽由 `_wire_left_column_sync` 的 `4*(max(minHint,120)+4)//3` 决定，之后按比例伸缩 | `ui/step0/step0_page.py:1304`、`:3187-3212` |
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
1. **比例**：`show()` 后等布局稳定，在 1280 / 1600 / 1920 / 2560 **同时量 Step0 与 Step1**，两者左栏占比之差 ≤0.02；占比在运行时从 Step0 读取，不复制常数。tab bar 不得成为列宽下限（标签 elide + 滚动），否则 Qt 把左栏钉在标签宽度上（实测 304px）。
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

### B.5 模式边界（2026-09-17 复裁，维持原判）
B **不引入**任何用户可见的新模式。Step1 用户可见模式仍只有 Overlay / Fusion，由旧路径服务；新宿主在 B 阶段**界面不可达、仅测试可达**。切换在 C 完成。

**理由（复裁记录）**：B 只完成来源、瓦片、Intensity、导航与生命周期，尚无 C 的 Overlay/Fusion 多通道合成。B 阶段若提前接管可见 viewer，只有两种结果——暴露一个未获批准的单通道模式，或用未完成的合成冒充 Overlay/Fusion。因此阶段出口为：① B 用真实 `ExploreView`、真实瓦片、真实像素跑完自动退出门；② B 评审通过后暂停；③ C 接入正确的 Overlay/Fusion，并在 C 结束时做用户可见的全片 viewer 真机验收。

**"无 patch 也能从 Step0 接着看"属于 C 的最终可见验收门**，不是 B 的界面门。若要 B 结束即可见，必须重新合并 B/C 范围，而不是只把接线提前。

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

### C.1.1 零权输入集合（2026-09-17 审核补）
- `viewer/step1_compose.py` 给出**唯一**的输入集合答案：`overlay_channels(weights)` 与
  `fusion_channels(groups, group_weights, nucleus)`。通道权重 <= 0、组权重 <= 0、nucleus 权重 <= 0
  的通道**不请求、不映射、不进合成输入集合**。
- 与公式等价，不是新公式：`fuse_channels` 本就拒 `w <= 0`、把组乘 `gw`、把 nucleus 乘 `nuc_w`；
  组间取 **max** 且信号非负，故丢弃零权组与零权 nucleus 逐像素等价。
- 显式 0.0 的**参与状态与权重照旧保留**（域模型的答案），只是当下不贡献像素。

### C.2 Intensity 与缓存
- 合成输入信号一律来自 **B.3 的全局映射**；**不在合成层**重新调用 `compute_qupath_auto_minmax(当前数组)` 或 `_norm`。
- 底层瓦片键不含权重、颜色、模式（同 B.3）。
- 合成键 = 参与的底层键集合 + 模式（overlay/fusion）+ 勾选集合 + 各通道权重 + 组与组权重 + nucleus 权重 + 各通道 Intensity 参数 + 颜色。
- **世代号**：旧世代的迟到结果一律拒收，不上屏。

**2026-09-17 审核补（C.2 的硬要求）**
- **合成键含完整底层身份**：dataset_path + fingerprint + stage + corrected_artifact + channel +
  grid_version + tile_size + level + tx + ty（`tile_identity()`），换数据集/换校正产物/换 ROI 不得命中旧 RGBA。
- **合成缓存按字节有界**：复用 `viewer/caches.py` 的 `LRUByteCache`，暴露 `cache_stats()`（bytes/items/evictions）；
  淘汰只动合成缓存，底层瓦片缓存不受影响；被淘汰的合成瓦片可从底层缓存重算且**不增读盘**。
- **合成不在 GUI 线程**：缓存命中与异步到达统一走 compose worker（默认 `ThreadPoolExecutor`，可注入）；
  GUI 线程只做规划、查合成缓存、发布 RGBA；worker 结果经 queued signal 回到 GUI 线程**再查一次世代**；
  `_compose_cache`/`_emitted`/`_inflight`/`_missing` 仅在 GUI 线程改。
- **缺窗口触发共享求窗**：注入 seed port（`request_mapping_seed`），同一缺失窗口**跨世代只求一次**；
  窗口到达由宿主调用 `window_arrived(channel)` → 新世代 → 同一 draft 重合成。
- **missing 绑定世代**：`invalidate()` 清空 missing 与已播报值；每个世代播报自己的 missing，
  包含**空列表**以清除旧提示；新来源缺同名通道会再次播报。

### C.2.1 线程边界与求窗生命周期（2026-09-17 二次审核补）
- **调度器回调不在本对象线程**：真实 `TileScheduler` 在读盘 worker 里回调（`viewer/scheduler.py`）。
  回调只 `emit` 一个 queued 信号；`_start_tile`、缓存访问、`_inflight`/`_emitted`/`_missing` 的修改
  一律回到协调器所属线程。门：从真实后台线程投递读结果，断言 `_start_tile` 在 `coordinator.thread()` 上跑。
- **在途任务带令牌**：`_inflight = {cache_key: (generation, job)}`；只有令牌完全一致才摘除登记，
  防止旧世代 worker 摘掉新世代同 key 的登记导致重复提交/并发重算。门：新旧世代同 key 同时在途。
- **求窗失败可重试**：`request_mapping_seed` 返回 False（overview 像素未到，服务顺带发起读取）
  **不得**记为 outstanding；服务已 pending 时可记为 outstanding（权重变化复用同一在途计算）。
  outstanding 身份为 `(source_identity, channel)`，换数据集/换产物后同名通道重新发起。
  门：先 False 后 True 的重试；已 pending 不重复问；换来源重新问。
- **空输入也要播报**：没有可见瓦片、或全部权重为零的帧，仍发 `windows_missing([])` 清除旧提示。
- **worker 快照不可被原地改写**：交给 worker 的 draft 逐层复制（weights/colors/mappings/groups/
  group_weights/nucleus），C3 接入可原地编辑的真实 draft 后仍成立。
- **身份补全**：`tile_identity()` 纳入 `TileGridSpec.source_chunk_shape`。

### C.2.2 missing 的清除是独立动作（2026-09-18 三次审核补）
- `_note_missing()` 只**合并**每块瓦片的缺窗口集合；把空集合并入 `{"CD8"}` 仍是 `{"CD8"}`，
  所以清除必须是独立方法 `_clear_missing()`（清空 + 必要时发 `windows_missing([])`）。
- missing 集合属于**帧**而非"上一个有缺窗口的帧"：每次 `compose_visible` 开始先清空，
  再随本帧瓦片发布累加；清空本身不播报，播报只在集合与已播报值不同时发生（不闪）。
- 门（**不调用 `invalidate()`**）：已提示 `["CD8"]` 后，(a) 可见瓦片置空再规划、(b) 权重全部归零再规划、
  (c) 窗口补齐后同帧重规划 —— 最后一次通知都必须是 `[]`；另有"仍缺窗口时不重复播报"的不闪门。

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

### C.3.1 实现（2026-09-18 授权范围）
- `ui/step1_draft_spec.py`：唯一的 spec 构建者。Overlay 取"step1 作用域内被勾选的通道 × 科学权重
  （`max(gw·w)`，nucleus 用自身权重，与 `MainWindow._overlay_weight` 同一规则）"；
  Fusion 取 `FusionDomainModel.effective_config()` 整份；颜色与窗口取 `ChannelDisplayState`
  （`mapping()`，**不**调 `mapping_or_seed`，帧路径不做像素工作）；无窗口的通道不进 `mappings`，
  由合成层点名、协调器向共享服务求窗。
- `ui/step1_compose_binding.py`：`Step1ComposeBinding` 跟随 `draft_changed`/`draft_restored`/
  `dataset_bound`/`color_changed`/`mapping_changed`/`visibility_changed`/`state_installed`，
  每次都**重建完整 spec** 并 `invalidate` + `compose_visible`；`set_mode()`、`source_changed()` 同理；
  `recompose()` 用于仅相机移动（不推进世代）。窗口到达走 `coordinator.window_arrived(channel, spec=...)`，
  **不允许**任何代码改协调器私有的最后一帧（测试里有静态门）。
- committed 一侧不动：Search/Generate 继续读 `committed_snapshot()`，本块不向领域模型写入任何东西。
- 旧 session 走 `prepare_restore`/`commit_restore`，显式 0.0、异质组权重、取消再勾选的历史权重均保留。
- **本块仍不可见**：不实例化、不挂载，接管归 C4。

### C.3.1 私有帧不外借（2026-09-18 审核补）
- 窗口到达一律走 `window_arrived(channel, spec=...)`，调用方**重建完整 spec**；
  任何测试或生产代码都不得读写协调器内部的"最后一帧"。
- 门覆盖 `tests/test_step1_*compose*.py`、`tests/test_step1_draft_binding.py` 与
  `ui/step1_compose_binding.py`、`ui/step1_draft_spec.py`、`ui/step1_viewer_binding.py`，
  而不只是写门的那个文件。

### C.4 切换门（正常应用内，新 viewer 接管 Overlay/Fusion 之后的真机验收）
- Step0 → Step1 **只要求坐标与视口位置连续**，不要求像素相同：两步的像素各自遵守本步规则（Step0 一次显示一个 marker、可能在看某预览方法；Step1 遵最终决断并做多通道合成）
- ROI 边界来回平移，ROI 内亮度不随视口变化
- 缺失产物提示可见且文案正确
- Overlay / Fusion 两模式真机验收

### C.4.1 实现（2026-09-18 授权范围）
- `ui/step1_composed_layer.py`：`Step1ComposedLayer` 用**既有** `TileItemPool` 承载 RGBA，
  固定 levels `(0,255)`、无 LUT；世界矩形用既有 `ExploreView.world_rect` + 该层 downsample；
  z 基线 `OVERLAY_BASE_Z + 300`，attach 时**断言**高于 raw/precise/overlay 三个池的 `base_z + num_levels`；
  attach 调既有 `controller.set_marker_visible(False)` 让单通道层休眠，detach/teardown 归还。
- 权重/颜色/窗口变化 → 同坐标**原地替换**（不闪）；**模式 / 来源 / 数据集 / draft 整份恢复 / state 安装**
  → `layer.clear()`（旧像素是另一张图，留着会在平移回来时重现）。清单由 `Step1ComposeBinding.HARD_REASONS` 持有。
- `ui/step1_viewer_mount.py`：`Step1WholeSlideMount` 把 host 装进**现有** Viewer tab 的图片位，
  旧 patch 视图**隐藏保留**为回滚路径（删除归 D）；Overlay/Fusion 两个既有按钮 → `set_mode()`；
  patch 按钮与 Tissue Preview 点击都走既有 `jump_to`；相机静止（既有 `gesture_quiet`）→ `recompose()`（不推进世代）；
  提示只用既有 `view.set_status_text` 信息层（缺产物 / ROI 外 / 窗口计算中），不新增控件。
- `ui/main_window.py`：`_set_step_active` 驱动 activate/deactivate（Step1 不在屏时不合成，回到 Step1 刷新一次）；
  `set_preview_mode` 转 `mount.set_mode`；`_select_preview_patch` 转 `mount.show_patch`；
  `navigate_requested`（既有信号）在 `_current_step == 1` 时转 `mount.jump_to_point`。

### C.5 变异闸门
- 组间 max 改 sum → 门 1 红
- 合成层重新按数组求 auto → 门 2a 红
- 权重进底层键 → 门 3 红
- 去掉世代拒收（任一情形）→ 门 4 红
- 恢复路径直接写 draft → 门 5 红
- 零权通道改为乘零参与映射 → 门 8 的调用测试红
- 首次勾选给 0.0 而非 1.0 / 重勾选时用默认值覆盖已存权重 / 点击名称顺带改勾选 → 门 7 对应子项红
- 零权组 / 零权 nucleus 仍被映射 → C.1.1 对应门红
- 合成键去掉来源身份 → 新来源命中旧 RGBA 的门红
- 合成缓存改回无界 dict → 有界/淘汰门红
- 合成改回 GUI 线程内联 → 线程门红
- 缺窗口每帧重复求窗 → 只求一次门红
- `invalidate()` 不清 missing → missing 随世代门红
- 调度器回调直接进 `_start_tile`（不走 queued 信号）→ 线程门红
- 结果按 key 而非令牌摘除在途登记 → 新旧同 key 门红
- 求窗返回 False 也记为 outstanding → 重试门红
- 求窗身份去掉来源 → 换来源重问门红
- 空输入帧不播报 → 清除提示门红
- worker 拿到浅拷贝 draft → 快照门红
- `tile_identity()` 去掉 `source_chunk_shape` → 网格门红
- 空输入用 `_note_missing(())` 代替 `_clear_missing()` → 清除门红
- 每帧不清空 missing → 窗口补齐后仍被点名的门红
- `_clear_missing()` 不播报 → 清除门红
- viewer 改读 committed 而非 draft → 门 1/2 红
- binding 不推进世代 → 世代门与"快速连编只剩最后一版"门红
- 不跟随 `mapping_changed` → 窗口到达门红
- spec 缓存一次不重建 → 编辑/模式/窗口门红
- 勾选读当前作用域而非 step1 → 作用域门红
- 无窗口通道猜一个窗口 → 缺窗口门红
- overlay 权重忽略组权重 → 组权重门红
- 任一受覆盖文件改回直接改写协调器私有帧 → C.3.1 门红
- 合成池改用可变 levels / 加 LUT → 显示层像素门红
- 合成层 z 基线降到单通道层之下 → z 门红
- attach 不让单通道层休眠 → 休眠门红
- 世界矩形忽略该层 downsample → 落位门红
- `clear()` 只清字典不移除 item → 残留门红
- 模式切换不清层 / 每次变化都清层 → 残留门与不闪门红
- `_set_step_active` 不驱动 mount / 模式按钮不转 `set_mode` / patch 不转 `show_patch` /
  预览点击不判当前步 / 安装时删除旧 patch 视图 / 离开 Step1 仍合成 → 对应接管门红

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

## 6b. 单列下游待办（不写入 B/C，本轮不实施）

用户 2026-09-17 提出并明确暂缓，待 Step1 收尾后另行排期：

- **Step2 不需要 Channels 面板**：该步无需通道列表，面板可隐藏。
- **Step3 的通道列表不显示权重**：Step3 是"多通道 + 分割结果"查看器，行上不应有权重编辑器。

两项都属显示表面改动，实施时须同笔更新 `UI_SURFACE_RULES.md` 与契约测试。

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
- **Step0 / Step1 显示状态隔离**（2026-09-16 裁定）：勾选（显示状态）与当前选中通道**按步骤分区**，往返各自恢复；颜色、Min/Max/Gamma、通道顺序与名称、dock 实例/行对象/搜索/滚动位置继续共享；participation 与权重仍只属 Step1 科学草稿。步骤切换只静默切换投影域（`ChannelDisplayState.set_scope` + `scope_changed` 重绘），不产生用户命令、fusion revision 或保存。**隔离必须落到页面消费者**：Step0 的 selection/visibility 处理器只响应 `scope=="step0"`，Step1 的只响应 `"step1"`，Step3 只响应共享域；进入某页时该页**重放一次自己的答案**（`Step0Page.resync_display_from_state` / `MainWindow._resync_step1_display_from_state`）。**restore 显式携带目标域**（`install/prepare_restore/restore_session_state` 的 `scope=`，ConfigPanel 用 `using_scope`），所以在 Step0 恢复 Step1 session 不动 Step0。dock 的行对象两步共用，因此"某步的行"不是可断言的隔离证据，状态分区才是。**四步显示状态全部独立**（2026-09-16 第二次裁定）：Step2/3 第一次进入、以及 Step1 每次重新 commit 后的第一次进入，由 **Step1 committed snapshot 的 enabled 集合**初始化（显式 0.0 仍勾选；已停用但留有历史权重的不勾选；不读 Step0，不读未提交 draft）。初始化后各步互不回写（Step2 改动不动 Step1/Step3，反之亦然）。实现：`MainWindow._DISPLAY_SCOPES` 四项 + `_seed_downstream_scope`（按 `committed_hash` 判定是否需要重新初始化）。
- **通道列宽度是一条双向同步的共享占比**：Step0 或 Step1 任一侧拖动都写同一个归一化占比，另一侧跟随；写 Step0 必须经 `Step0Page.apply_channel_column_width`，以保持其隐藏的 conditioning splitter 一致。
- **Step1 行的权重编辑＝启用命令**（2026-09-16 裁定）：给一个通道权重＝显示它并把它放进 fusion，保留用户给的数值；权重回到 `0.0` **不**停用该通道（显式 0.0 是一个决定）；取消勾选是唯一把通道移出的手势。同步记于 `UI_SURFACE_RULES.md` §4。
- 右栏 `Viewer` | `Patch Results` 两 tab 切换显示，不堆叠；左栏 `Channels` | `Method & Parameters` 两 tab；**通道列宽度占比在运行时从 Step0 读取**（`MainWindow._step0_left_fraction` 读 `Step0Page._bg_c_split`），不是复制的常数。实测 Step0 占比约 0.278（非 1:2）；硬写 1:2 在真机上明显比 Step0 宽，已被否决。
- 标题 `Step 1 — Channel Fusion + Preliminary Segmentation`；Tissue Preview 在标题行最右；Intensity 在 Channels tab 内并对齐 Step0 位置；删 `① ROI / Patch Overview`、预览子标题与红/蓝图例。
