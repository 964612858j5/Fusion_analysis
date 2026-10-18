# Step0 与 Step1 之间的新模块：区域整理（TMA 核心 + 模糊 / 折叠排除）方案（第三版：codex 两轮修订 + 用户裁定 2026-10-18；开工前子代理审核）

来源：用户 2026-10-18 对 `docs/tma_f3b_rework_plan.md` 的回复：
- 同意加常驻看图；
- 这个模块位于 Step0 和 Step1 之间，必须在 Step0 Save 之后才能进入；
- 要加入"反 blur / fold"功能，所以大组织也能用（不做 dearray，而是去除模糊和折叠），这就是必须集成通道的原因；
- 通道作为左侧的一个标签页；
- 所有步骤（含本模块）的视野全图联动；
- 外扩余量 3%、只做顶点拖动、去掉"在 Step1 看大图"都同意。

本方案取代 `tma_f3b_rework_plan.md` 的 V 部分（看图）；该文的 M 部分（轮廓自然贴合）原样并入，作为本方案的第 2 段。

## 0. 已定（用户裁定）
- 模块在 Step0 与 Step1 之间，**Step0 Save 之后才能进入**。
- 一个模块，两种用途：
  - TMA 片子：去阵列、编号、校正（F3 已做的部分）；
  - 大组织：标出模糊、折叠区域并排除。

  两种片子都可以用排除功能。
- 左栏多个标签页，通道是其中之一；右栏是和 Step1 同一套的多通道看图（第三个常驻实例）。
- 所有步骤的视野联动：凡是有整片看图的步骤（Step0、Step1、Step3）和本模块，共用同一个视野。Step2 只有分块概览，Step4 没有看图，都不涉及。
- 数据范围：项目在 Step0 用了 ROI 的，本模块和 Step1 一样只显示、也只能标注 ROI 以内；整片项目就是整片。
- 排除只做标记，**不修改原始像素**（9 月规范；总方案 §5）。

## 1. 模块的样子
### 1.1 名字与位置
- 名字（建议，请用户定）：**"Step 0.5: Regions"**（区域整理）。步骤条变为：

  `Step 0 → Step 0.5: Regions → Step 1 → Step 2 → Step 3 → Step 4`

  F3 加的"○ TMA"标签去掉，并入这个模块。
- 进入条件：与 `_go_to_step1` 相同的 Step0 交接检查（Step0 已 Save、交接已被接受；`main_window.py:4919-4980` 的同一套判断）。不满足时只提示"请先在 Step0 Save"，不切换页面。
- 程序内部给它一个新的步骤号（例如 5，只是内部编号），页面放在页面栈末尾，Step0–Step4 的步骤号和栈位置都不变。进入和离开走 `_set_step_active` 的正常切换：
  - 显示范围 `_DISPLAY_SCOPES`；
  - 通道面板挂载 `_channels_host_for`；
  - 看图暂停和恢复（照 `_step3_follow_step`）；
  - 导航策略：本模块只读。

### 1.2 页面布局（`StepFrame`，左栏宽度与各步通道栏共用）
```
┌ Step 0.5 — Regions ─ 研究/片子 ─ 版本:[草稿▾] [放弃草稿] [锁定] ──────────────┐
│ [Channels] [TMA cores] [Blur / Fold]   │ 工具行: [Select][Line][Clicks][Merge]  │
│ ┌──────────────────────────────────┐ │   [Polygon][Circle][Undo][Redo][Prev]  │
│ │ Channels：勾选 / 色块 / 名称，     │ │   [Next]                               │
│ │ Intensity…（只显示、无权重的行）   │ │                                        │
│ │ TMA cores：对应表、找核心、编号面板、│ │  多通道整片看图（同 Step1 的引擎）      │
│ │ 核心表（F3 已做的左栏，原样搬入）  │ │  上面一层透明叠加：核心轮廓、标签、     │
│ │ Blur / Fold：排除区列表、原因、范围 │ │  编号线、排除区（斜线填充）             │
│ └──────────────────────────────────┘ │                                        │
└──────────────────────────────────────┴────────────────────────────────────────┘
```
- 工具行按当前左栏标签页显示相关的工具：在 TMA 标签页显示编号和并入工具；在 Blur / Fold 标签页显示画多边形、画圈和选择。
- **TMA 标签页始终保留 `Choose map sheet…` 入口**（首次导入就在这里）。没选对应表时，旁边加一行提示："不是 TMA 片子的话，直接用 Blur / Fold 标签页即可"。
- **版本、锁定、放弃草稿、撤销都跟着当前标签页走**：
  - 在 TMA 标签页，作用于 TMA 的编号与核心草稿和版本（F3 已有的那一套）；
  - 在 Blur / Fold 标签页，作用于排除区的草稿和版本；
  - 在 Channels 标签页，这几个按钮不可用。

  标题栏的版本下拉框只显示当前标签页的版本。两边各有自己的撤销记录，互不影响。

### 1.3 看图（第三个常驻实例，沿用 F3b 第二版的结论，入口条件改了）
- 已经 Save 过，所以直接用真实窗口来建看图，和 Step3 的 `_step3_whole_slide()`（`main_window.py:1931-1954`）一样。数据就是 Step1 的数据：按校正决定显示原始或校正后的通道，按 ROI 裁剪；整片项目就是整片。**不需要 F3b 方案里的适配对象。**
- 子类 `RegionsWholeSlideMount`：**GPU 和 CPU 两条路径都用本模块自己的显示描述**，由一个新函数 `regions_display_spec(state)` 生成：
  - 内容：显示范围 `"regions"` 里勾选的通道，按叠加方式显示，每个权重 1，颜色和亮度窗口用全程序共用的那一套；
  - **不调用** `step1_draft_spec.build_spec`：它按 Step1 的融合参与和权重来筛通道（`ui/step1_draft_spec.py:150`），换显示范围名字也不行；
  - 重写的位置：`_gpu_display_snapshot`、`_gpu_composes`（`ui/step1_viewer_mount.py:866`）、`_start_cpu_backend`（:469），三处都改为使用这个描述。
- **勾选只影响显示**，不进入、也不改动 Step1 的融合（Step1 行里的勾选就是融合命令）。通道面板对新步骤号只显示"勾选、色块、名称"的显示行，不显示权重；通道面板只有 Step1 会写融合（`ui/widgets/channel_dock/global_dock.py:992`）。
- **Intensity 窗口**：在本模块里打开时，调的是本模块当前选中的通道（取自通道面板在 `"regions"` 范围里的当前通道），不读 Step1 的设置（现有 `main_window.py:7907` 读的是 Step1 的配置，不能直接沿用）。
- 叠加层：`RegionsOverlay(QWidget)`，照 `MontageOverlay`（`ui/step1_presegmentation/montage_view.py:75-160`）做，透明、不接收鼠标，放在 GPU 画面上方。
- 鼠标：在 `view_box` 里放一个看不见的"工具接收"图元。
  - 工具认领的左键拖动交给工具，不认领的照常平移；
  - 滚轮只在画编号线时调选取带宽度；
  - 右键接 `sigRightClicked`，菜单位置取鼠标的当前位置。
- 后备显示：GPU 失败时走 CPU 回退；两种都失败时，显示 F3 的 DAPI 缩略图（`ui/tma/core_map.py`，改成能画多边形），编辑功能照常可用。
- 代价：第三个常驻看图（用户已同意）。显存紧张时，隐藏的看图会把图层按需降到粗层（`ui/gpu_memory.py`）。

### 1.4 视野全图联动
- 本模块的看图接入现有的共用视野 `CameraOwner`（`ui/camera_owner.py:26-67`；它本身和 `_camera_write` 都不用改）：
  - 给它设 `camera_sink` 和 `camera_jump_sink`（做法同 Step3 的 `_on_step3_camera` / `_on_step3_jump`）；
  - `_apply_owner_camera_to`（`main_window.py:2442-2464`）加上本模块；
  - `_set_step_active` 里套用视野的判断（:5585，现在只允许 0、1、3）加上新步骤号；
  - 进入本模块时套用共用视野，在本模块里平移或缩放也写回共用视野。
- Tissue Navigator：
  - 新建一个本模块的显示上下文 `_CTX_REGIONS`，在 `_register_block01_contexts` 里登记，加入 `_STEP_CONTEXTS`（:5273）。小图按 `"regions"` 范围勾选的通道来画；没有上下文时小图不会画新画面（`ui/block01_display.py:1411`）；
  - 点击跳转（:2566-2579）和视野框显示也加上本模块。
- 本模块里的 `Prev` / `Next` 和点表格行跳到某个核心，都算一次"跳转"，写进共用视野。回到 Step1 时，看到的就是同一个位置。

## 2. TMA 部分（F3 + F3b 的 M 部分）
- F3 已做的逻辑和测试全部保留（`core/tma/layout*.py`、编号、并入、锁定、版本），界面从独立页面搬进本模块的 "TMA cores" 标签页和工具行。
- 轮廓自然贴合、略大于组织：完全按 `docs/tma_f3b_rework_plan.md` 第三版的 M 部分：
  - 外扩 3%；
  - 扩出部分按分水岭分配，原来的组织不削；
  - 不小于 1% 核心面积的碎块保留；
  - 候选列表升到第 2 版并兼容第 1 版；
  - 多边形块支持平移、缩放和顶点拖动。
- 去掉"在 Step1 看大图"按钮（本模块自己就是大图）。

## 3. 模糊 / 折叠排除（新功能；QualityMask 的第一版）
### 3.1 范围
- 本段只做**人工标记**：用户在看图上画出模糊或折叠的区域，附上原因。
- 自动检测（例如按清晰度找模糊、按亮度和厚度找折叠）只作为后续选项，需要先看真实例子再设计，**本段不做**（请用户确认）。

### 3.2 操作（Blur / Fold 标签页）
- 工具：
  - `Polygon`：逐点单击，回车或点回起点闭合，退格删上一点，Esc 取消（与 Odon 画多边形一致，`app.rs:12418-12454`）；
  - `Circle`；
  - `Select`：选中、移动、拖顶点；Delete 删除。
- 每个排除区要填：
  - 原因：blur / fold / other（可多选）；
  - 通道范围：全部通道，或指定通道（9 月规范："排除区记录原因、通道范围"）；
  - 可选备注。
- 列表：每个排除区一行（编号、原因、通道范围、面积）；点一行，看图就跳过去。
- TMA 片子上，排除区可以画在某个核心内部（局部排除）。整颗排除仍用 TMA 表格里已有的"Exclude…"。两者分开记录（9 月规范："局部排除不等于整颗排除"）。

### 3.3 数据与版本
- 存在 `<project>/regions_qc/` 下：
  - 草稿：`exclusions_draft.json`；
  - 锁定版本：`exclusions_v<n>.json`，只增不覆盖。
- 文件格式：
  - 文件级：`schema_version`、`slide_id`（A3）、Step0 交接的版本信息（交接文件路径和它的内容校验值）、通道名单（通道名，与片子通道顺序对应），以及本版号和所基于的版本。
  - 每个排除区：
    - 外形：原图整数坐标；可以是多块，多块时按 TMA 已有的规则连成一条轮廓（`core/tma/layout_geometry.py`：栅格合并、外轮廓、细缝连接），所以仍是一个多边形。代价：排除区中间的空洞会被填上，分开的几块之间多出约 1 像素宽的连接线。排除区只是标注，这点误差可以接受；测试里核对面积只多出这两部分；
    - 类型：只有一个，按原因定——只勾 blur 是 `blur`，只勾 fold 是 `fold`，其余情况（两个都勾，或 other）是 `manual_annotation`，原因列表另存。三个都是 A3 已预留的区域类型（`core/project_identity.py:59`）；
    - 原因列表、通道范围（全部，或指定的通道名）、备注；
    - `region_id = project_identity.region_id(slide_id, 类型, 外框, 多边形)`，也就是 A3 原样的规则（含片子、类型、外框和多边形），不扩展 A3 的公共接口；
    - 创建和修改记录。
- 锁定时登记来源记录，新类型 `quality_exclusions`：
  - `depends_on` 只放已登记的条目：这张片子的 `raw_slide`（`ensure_raw_slide`）；
  - Step0 交接目前没有对应的登记类型，所以放进 `unresolved_inputs`，写成 `{"role": "step0_handoff", "path": …, "sha256": …}`（`core/provenance.py:169` 的现有格式）。
  - 不新增登记类型以外的机制。
- 和 TMA 的锁定是两份独立的版本：互不阻塞，各自锁定。

### 3.4 下游怎么用（本段只存，不改下游）
- 本段交付的名称是**"排除标注"**：标出来、存下来、可追溯；**不能说分析里已经排除了这些区域**。
- 本期不做：低组织量（low-tissue）检测、几何改变后让已有分析"过期"、下游实际排除。
- Step1 / Step2 / Step4 怎样使用排除区（不分割？分割后标记？定量时去掉？），在 TMA 的 F4 段一起设计，因为 F4 本来就要改多区域和按版本解析。
- 本段只保证：锁定版本完整、可追溯、不改像素。

## 4. 分段实施（每段完成都交 codex astra high 审核；每段有自己的白名单和验收门，用户真机验收通过才进下一段）
- **Q0 模块外壳**：
  - 内容：步骤条和进入条件；页面框架和三个左栏标签页；第三个看图（子类、`regions_display_spec`、通道标签页、Intensity）；叠加层和工具接收图元；视野联动和 Navigator；把 F3 的 TMA 界面搬进来（行为不变，仍是圆）；去掉"○ TMA"标签和独立页面。
  - 白名单：
    - 新建 `ui/regions/regions_page.py`、`regions_mount.py`、`regions_overlay.py`、`regions_display.py`；
    - 改 `ui/tma/tma_page.py`、`page_view.py`、`page_actions.py`、`core_map.py`、`map_tools.py`（改成嵌入式：不再自建框架，画到叠加层、接工具接收图元）；
    - 改 `ui/main_window.py`（接线）、`UI_SURFACE_RULES.md`；
    - 测试：替换 `tests/test_ui_surface_contract.py:297` 起的旧 TMA 入口测试（固定了"○ TMA"、空标签、步骤号 4）、`tests/test_v16_frame_lock.py:314` 起的旧 TMA 页面测试；新建 `tests/test_regions_q0.py`。
  - 验收门：
    - 自动测试：进入条件、三个看图的暂停和恢复、显示隔离（勾选不改 Step1 融合、GPU 和 CPU 用同一份描述）、视野双向联动、Navigator、叠加层坐标、工具认领和平移、旧 TMA 测试全过；
    - 真机：多通道显示和切换、视野联动、内存。
- **Q1 TMA 轮廓**：
  - 内容：F3b 的 M 部分。
  - 白名单：`core/tma/dearray.py`、`layout.py`、`layout_geometry.py`、`layout_lock.py`、`ui/tma/map_tools.py`、`ui/tma/page_view.py`（把块的多边形轮廓传给叠加层，现在只传圆心和半径）、`ui/regions/regions_overlay.py`、`ui/tma/core_map.py`，测试 `tests/test_tma_f2_dearray.py`、`tests/test_tma_f3_layout.py`、`tests/test_tma_f3_page.py`。
  - 验收门：F3b 列出的自动测试；真机完成上次没做完的验收第 4–8 步。
- **Q2 排除标注**：
  - 内容：第 3 节。
  - 白名单：新建 `core/quality/__init__.py`、`core/quality/exclusions.py`、`ui/regions/exclusion_panel.py`、`ui/regions/exclusion_tools.py`、`tests/test_regions_q2.py`；改 `core/provenance.py`（加 `quality_exclusions`）、`ui/regions/regions_page.py`。
  - 验收门：自动测试；全量回归；真机（一张大组织 ROI 项目、一张 TMA 片子）。

## 5. 文件清单（总表；以第 4 节各段的白名单为准）
- **新建**：
  - `ui/regions/`：`regions_page.py` 页面组装、`regions_mount.py` 看图子类、`regions_overlay.py` 叠加层与工具接收、`exclusion_panel.py`、`exclusion_tools.py`；
  - `core/quality/exclusions.py`：排除区数据、版本、锁定；纯函数，不涉及界面；
  - 对应测试。
- **移动 / 改**：
  - `ui/tma/*`：从独立页面改成本模块的一个标签页加工具（Q0）；多边形块编辑（Q1）；
  - `core/tma/dearray.py`、`layout*.py`：Q1；
  - `core/provenance.py`：加 `quality_exclusions`；
  - `ui/main_window.py`：只做接线，包括步骤条、新步骤号、第三个看图的创建 / 打开 / 暂停 / 关闭、通道挂载、显示范围、视野联动；删掉 F3 的独立 TMA 页面接线；
  - `UI_SURFACE_RULES.md`；
  - 界面约束和页面框架测试。
- **不改**：Step1 / Step3 的看图和数据来源、Rust 核心、融合、Step0 的校正和 Save 流程、下游各步。

## 6. 验收
- **自动**：
  - 进入条件（未 Save 拒绝）；
  - 三个看图各自的暂停和恢复；
  - 通道勾选只影响本模块、不改 Step1 融合；
  - 视野联动：在本模块移动后回到 Step1 位置一致，反过来也一样；
  - 叠加层坐标与视野一致；
  - 工具认领和平移；
  - TMA 全部旧测试；
  - 轮廓测试（F3b M）；
  - 排除区的增删改、版本、锁定、`region_id`；
  - GPU → CPU → 缩略图三级回退。
- **真机**（用户；每段的真机验收见第 4 节，这里是总的）：
  - 换片子、重新 Save、关闭程序被拒绝以及最终关闭；
  - 注意：DAPI 缩略图那一级后备，不能当作多通道下的模糊 / 折叠功能已经验收过；
  - TMA 片子（Slice1）：Save → 进入 Regions → 多通道 → 编号、校正、锁定；
  - 一张大组织片子：Save → Regions → 画几个模糊 / 折叠区 → 锁定；
  - 两边都检查视野联动、内存和切换速度。

## 7. 用户裁定（2026-10-18，全部同意）
1. 模块名：**"Step 0.5: Regions"**（内部步骤号另定，与页面栈位置不同）。
2. 模糊 / 折叠这次**只做人工标注**，自动检测以后另行设计。
3. 排除区在下游怎么用，**放到 F4 一起设计**；本期交付称为"排除标注"。
4. **去掉 F3 的独立 "○ TMA" 标签和页面**，全部并入本模块。

开工前按用户要求，由两个子代理（Fable 5.1，中等推理强度；Opus 5.5）审核本方案，意见与处理记在第 9 节。

## 8. 工期
- Q0 约 3–4 天；
- Q1 约 2–3 天；
- Q2 约 3–4 天；

合计约 8–11 天，含真机验收。

## 9. 开工前子代理审核（2026-10-18）与处理
审核者：Mythos 5.1（中等推理强度，命令行只读，输出 `mythos_region_qc_review_1.out`）、Opus 5.5（只读子代理）。两位结论一致：**整体可行，现有看图和接口都支持**。他们提出的阻断项和建议按下面处理；本节与前文冲突时，以本节为准。

### 9.1 阻断项（开工前改进方案）
1. **进入条件不能照抄 `_go_to_step1`**（两位都提到）：
   - 它会安排重试并最终打开 Step1（`main_window.py:4938-4947`），提示写在 Step0 上看不见的 Step1 标签里，而且它是唯一加载 Step0 交接的地方（`_load_step0_roi_result`，:4966）。看图要读的 ROI 和校正决定都由这一步填上。
   - 改为：从 `_go_to_step1` 抽出一个共用的"交接就绪"函数，有三种结果——就绪；正在准备（等好了回到 Regions，不进 Step1）；拒绝（提示显示在 Step0 上）。`_go_to_step1` 改用同一个函数，行为不变。
   - 从 Step1 进入本模块时，先停 Step1 的加载器。
   - F3 那条"不 Save 也能进 TMA"的路径作废。
2. **Q0 白名单漏了会被改坏的测试**（两位）：Q0 加入 `tests/test_tma_f3_page.py`（整份；改为通过本模块构造 TMA 标签页）、`tests/test_ui_surface_contract.py`（整份，含 :218 的走步骤测试）、`tests/test_global_channel_dock.py`（:1257-1266 假定只有四个挂载位置）。
3. **第三个看图在换片子和关闭时的处理**（Mythos）：
   - 换片子时关掉本模块的看图，照 Step3 的 `_close_step3_viewer`（:2294-2306，调用点 :3150）；
   - 程序关闭时，放在"可以拒绝关闭"的检查之后再关；
   - Step1 重新同步交接（`_step1_sync_whole_slide_source`，:2469-2490）时，本模块的看图跟着重新同步。

   以上都列入 Q0 的验收门，否则会出现"新片子显示旧片子像素"的老问题（P0 规则）。
4. **Tissue Navigator 需要一个新的显示上下文类**（两位）：Step2、Step3 用的 `_SharedSpecTissueContext`（:344-440）画的是 Step1 的设置。本模块要新建一个类，按 `"regions"` 范围的勾选来画小图，放在 `ui/regions/regions_display.py`。**这是新机制，请用户授权**（AGENTS 第 4 条）。
5. **F3 还没有提交过**（Opus）：Q0 要改写的正是未提交、也没验收完的 F3 代码，没有基线就没法按白名单核对，也没法回退。**建议先把 F3（含 2026-10-18 已修好的 4 处）作为"F3 中间版"提交到 v17**，提交说明写明"真机验收未完成"。需要用户授权提交（AGENTS 第 9 条）。
6. **排除标注存在哪里**（Opus，Q2 之前定）：一个项目可以有多个 ROI 工作区，每个都有自己的 Step0 交接。改为**按工作区存**：`<project>/rois/<工作区>/regions_qc/`，与交接和 ROI 裁剪一一对应；读取时片子身份不符就拒绝（同 F3）。
7. **排除区要有不变的编号**（Opus，Q2 之前）：`region_id` 由外形算出，拖一个顶点、改一次原因都会变，不能当身份。每个排除区加一个不变的 `exclusion_uid`；`region_id` 在锁定时才计算，并检查不重复。

### 9.2 建议（并入方案）
- **看图**：
  - CPU 路径要再子类化 `Step1ComposeBinding`，让它的 `spec()` 返回本模块的显示描述（`ui/step1_compose_binding.py:49-68`）；只重写 `_start_cpu_backend` 不够。
  - 默认的 Rust 路径和 Python GPU 路径都从 `_gpu_display_snapshot` 取画面（`ui/step1_core_binding.py:414`、`ui/step1_gpu_binding.py:217`），已覆盖。
  - 本模块固定用叠加显示，不跟 Step1 的融合 / 叠加切换（不要照抄 :2013、:7536-7538 的 `set_mode`），并测试"Step1 切到融合，本模块画面不变"。
  - **叠加层重新挂接**：在打开、重新同步、Rust 换到 Python GPU 之后，重新挂到当前视口并提到最上层，并加测试（GL 层每次改大小都会把自己提上来，`ui/step1_core_layer.py:129-144`）。
  - `"regions"` 范围第一次使用时的初始勾选：照搬 Step0 当前的勾选；之后随显示状态一起保存。
  - 编号线的滚轮：用视口事件过滤器。
- **主窗口接线补全**：共用栏宽列表（:1729、:1840）、`_on_step1_tissue_navigate`（:2566-2579）、关闭时保存排除草稿、`_update_next_button`（:5208-5216）对新步骤号的处理、关闭时等待 TMA 任务的代码改指向本模块。
- **Intensity**：`UI_SURFACE_RULES.md` §4 原来只允许 Step0、Step1 有 Intensity 入口，要改规则，加上本模块。在本模块打开时，调的是本模块当前选中的通道（不是 :7907 / :7915 读的 Step1 通道）。
- **ROI 项目的规则**：
  - TMA 找核心仍然扫整片；落在 ROI 以外的核心在表里标"在 ROI 外"，不能跳转、不能编号；
  - 排除区画到 ROI 以外的部分会被裁掉；
  - 每个排除区只存一个多边形（Mythos 的简化建议，取代原来复用 TMA 多块合并的写法）；裁剪后如果分成几块，就存成几个排除区，原因相同；
  - 测试左上角不在 (0,0) 的 ROI 上的叠加层坐标。
- **排除区数据规则**：
  - 圆存成 64 边形；
  - 锁定前检查：外形不退化、至少一个原因、通道名存在、在 ROI 内、`region_id` 不重复；
  - 读取时拒绝不认识的格式版本；
  - 原子写入；草稿停手 0.6 秒后保存（同 TMA）。
- **来源记录**：`operates_on` 填工作区的区域（ROI 或整片），加上 `workspace_id`、`slide_id`，令牌用文件的 sha256。TMA 片子另外依赖已锁定的 `tma_layout`。每个排除区可选记录所在核心的 `core_uid`（9 月规范把排除区挂在核心上）。
- **键盘和标签页**：快捷键（Enter、退格、Delete、Esc、Ctrl+Z）按当前标签页分派；切换标签页时取消未完成的编号或多边形；通道面板搜索框里的退格不受影响（加测试）。
- **后备缩略图**：叠加层和 `CoreMap` 实现同一组接口（`set_items` / `set_line` / `set_sequence` / `centre_on` / `screen_px`），工具输入只有一条路。
- **Q1 细节**：
  - 候选列表和草稿用第 2 版格式，读取同时接受第 1 版；
  - 碎块下限从 10% 降到 1% 后，真机上重新核对 Slice1 的候选数；
  - Q1 白名单补上 `ui/tma/page_actions.py`、`ui/tma/tma_page.py`、`tests/test_regions_q0.py`。
- **Q2 白名单补全**：`ui/regions/regions_overlay.py`（排除区斜线填充）、`regions_mount.py` 和工具接收图元（多边形工具的鼠标和按键）、`UI_SURFACE_RULES.md`、界面约束测试、`ui/main_window.py`（关闭时保存）。
- **内存与切换**：
  - 第三个看图最多再占 512 MB 原始缓存、2 GB 校正缓存和 1.5 GB 显存；三个看图的显存合计约 4.5 GB，接近 `gpu_memory.py` 的 5 GB 目标；
  - 隐藏的看图只在显存紧张时释放，所以切换时可能要重新加载；
  - 验收时在雷电外接固态盘和服务器机械盘上分别记下内存和切换时间。
- **验收补充**：
  - Step0 正在写小块时进入，应被拒绝；
  - 有工具接收图元时右键菜单正常；
  - Regions 里勾选通道永远不会改 Step1 融合；
  - 加入本模块后走一遍各步不弹新窗口；
  - 通道面板有 5 个挂载位置。
- **分段顺序**：Q1 主要是几何和缩略图，不依赖新看图，可以和 Q0 并行，或者先做（Mythos 建议）。维持 Q0 → Q1 → Q2，但 Q1 的核心几何部分可以在 Q0 审核期间先写。

### 9.3 需要用户再定的 3 件事
1. 先把 F3 作为"中间版"提交到 v17（推荐），再开始 Q0？
2. 授权 Tissue Navigator 新增本模块的显示上下文（新机制）？
3. 排除标注按 ROI 工作区存（推荐）？
