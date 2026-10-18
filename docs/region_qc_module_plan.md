# Step0 与 Step1 之间的新模块 "Step 0.5: Regions"：TMA 核心 + 模糊 / 折叠排除标注 方案（第四版修订：按 codex `codex_region_qc_plan_v4_1.out`、`_2.out` 修改；codex 复审通过；用户 2026-10-18 批准执行）

**第四版改了什么**（相对第三版）：
- 按用户 2026-10-18 的裁定：通道状态和 Step1 / Step3 共用；勾选就是融合；Step0 暂不处理。
- 采纳 Mythos 5.1 独立设计里的建议：块一律存成多边形；一份标注文件、一条版本线；鼠标只走一条路；不要 DAPI 缩略图后备；排除区按片子存；排除区属于哪个核心，锁定时按位置自动算。
- 第三版的审核意见（codex 两轮、Opus 5.5、Mythos 5.1）仍然有效的部分，已直接写进正文。

依据：
- 用户裁定：`docs/tma_f3_decisions_log.md` 末尾，以及本节"已定"；
- Mythos 5.1 的独立设计：`mythos_regions_redesign_1.out`；
- 第三版审核：`mythos_region_qc_review_1.out`、Opus 5.5 子代理报告（记在本文第 9 节）；
- 9 月规范 `docs/tma_core_editing_and_mapping.md`；
- F3 中间版：提交 `2a3a5ed`（v17）。

## 0. 已定（用户裁定）
- 模块名 **"Step 0.5: Regions"**，位于 Step0 与 Step1 之间，**Step0 Save 之后才能进入**。
- 一个模块两种用途：
  - TMA 片子：找核心、编号、校正、锁定；
  - 大组织（TMA 也可以用）：人工标注模糊、折叠区域。
- 本期**只做人工标注**，自动检测以后另行设计。排除区在下游怎么用，放到 F4 设计。本期交付称为"排除标注"，不能说成分析里已经排除。
- **通道状态与 Step1 / Step3 是同一份**（用户 2026-10-18）：
  - 在 Regions 勾选的通道，进入 Step1 直接显示；在 Step1 改了，回到 Regions 也一样。
  - **勾选就是融合**（与 Step1 的规则相同），用户已确认可以接受：进入分割前，融合权重已经保存过，以后可以重新载入。
- **Step0 暂不改动**：Step0 只是背景校正器，它的通道勾选不需要保存，也不并入这份共用状态。
- 视野全图联动：所有带整片看图的步骤（Step0、Step1、Step3）和 Regions 共用一个视野。
- 去掉 F3 的独立 "○ TMA" 标签和页面，也去掉"在 Step1 看大图"按钮（Regions 本身就能看大图）。
- 轮廓自然贴合、略大于组织：外扩 3%；只做顶点拖动，画笔以后再做。
- 排除只做标记，**不修改原始像素**。

## 1. 结构
- **`core/regions/`**（纯逻辑，不涉及界面）：
  - `document.py`：一份标注文件的草稿模型和各种编辑；
  - `geometry.py`：多边形、合并成一条轮廓、按 ROI 裁剪；
  - `numbering.py`：从 `core/tma/layout.py` 搬过来，编号逻辑不变；
  - `lock.py`：锁定检查、版本、来源记录。
- **`ui/regions/`**：
  - `page.py`：页面（`StepFrame`，左栏三个标签页）；
  - `mount.py`：第三个常驻看图；
  - `overlay.py`：一个透明部件，既负责画，也负责接收鼠标；
  - `tools.py`：一套工具控制（选择 / 多边形 / 圆 / 顶点 / 并入 / 画线 / 点选）；
  - `cores_panel.py`、`exclusions_panel.py`：两个标签页的内容。
- `ui/tma/` 里的编号面板、核心表等部件搬进 `ui/regions/` 复用，F3 的独立页面删掉。
- `ui/main_window.py` 只接线：步骤条、页面、进入检查、看图的打开 / 暂停 / 关闭 / 同步、通道面板挂载、视野、导航小图。

## 2. 看图与通道（照 Step3 的做法，不新增机制）
- **第三个常驻看图**：照 `_step3_whole_slide()`（`ui/main_window.py:1931-1954`）新建一个 `Step1WholeSlideMount`，用真实窗口，数据就是 Step1 的数据（按校正决定显示原始或校正后的通道，按 ROI 裁剪）。
  - 显示方式跟随 Step1 当前的叠加 / 融合模式，做法同 Step3（`set_mode(self._step1_preview_mode)`，:2014）。
  - 不能把同一个看图搬来搬去：搬动要重建 GL，而且 `ui/step1_core_layer.py:111` 会报错。
- **通道**：`_DISPLAY_SCOPES` 里 Regions 用 `"step1"`（:5317），`_STEP_CONTEXTS` 里用 `_CTX_STEP1`（:5273），和 Step3 完全一样。
  - 通道面板挂进 Regions 的 Channels 标签页，行就是 Step1 的行（勾选、色块、名称、权重）。
  - `dock.set_step` 按 Step1 处理，做法同 Step3（:5597 现在是 `1 if active == 3`，加上 Regions）。
  - 导航小图沿用 Step1 的那一套，**不新增导航图机制**。
  - **Intensity**：现有的 `_show_intensity_window`（:7907-7915）读的是 Step1 的当前通道，和 Regions 共用的就是这一份，可以直接用。`UI_SURFACE_RULES.md` §4 加一句：Regions 的 Channels 标签页里也有这个入口。
- **视野**：
  - Regions 的看图设 `camera_sink` 和 `camera_jump_sink`（同 Step3）；
  - `_apply_owner_camera_to`（:2442-2464）和 `_set_step_active` 里套用视野的判断（:5585，现在只允许 0、1、3）加上 Regions；
  - Tissue Navigator 点击跳转（`_on_step1_tissue_navigate`，:2566-2579）加上 Regions；
  - `CameraOwner` 和 `_camera_write` 不用改。
  - `Prev` / `Next`、点表格行都通过 `show_patch` 跳转，写进共用视野。
- **生命周期**（第三版审核的阻断项）：
  - 进入 Regions 时，暂停 Step1 和 Step3 的看图；离开时暂停 Regions 的看图（照 `_step3_follow_step`，:1979-2018）；
  - 换片子时关掉 Regions 的看图（照 `_close_step3_viewer`，:2294-2306，调用点 :3150）；
  - Step1 重新同步交接时，Regions 的看图跟着同步（:2469-2490）；
  - 程序关闭时，在"可以拒绝关闭"的检查之后再关；
  - 从 Step1 进入时，先停 Step1 的加载器。
- **进入检查**（第三版审核的阻断项）：从 `_go_to_step1`（:4919-4987）抽出共用的"交接就绪"函数，有三种结果：
  - 就绪；
  - 正在准备：等好了回到 Regions，不进 Step1；
  - 拒绝：提示显示在 Step0 上。

  这个函数负责加载交接（`_load_step0_roi_result`，:4971）。**不在 Regions 里恢复 Step1 会话**（用户 2026-10-18）：Regions 的通道只是用来复核模糊之类的区域，之后第一次进 Step1 时，如果自动恢复的 Step1 会话把 Regions 里改的通道覆盖掉，可以接受。

  `_go_to_step1` 改用同一个函数，行为不变。F3 那条"不 Save 也能进"的路径作废。
- **叠加层**：`RegionsOverlay(QWidget)`，照 `MontageOverlay`（`ui/step1_presegmentation/montage_view.py:75-118`），放在 GPU 画面上方。
  - 打开、重新同步、Rust 换到 Python GPU 之后，都重新挂到当前视口并提到最上层（GL 层每次改大小都会把自己提上来，`ui/step1_core_layer.py:129-144`）。
  - **鼠标只走一条路**：叠加层在看图视口上装一个事件过滤器。
    - 工具认领的按下 / 拖动 / 滚轮由工具处理，pyqtgraph 不会平移；
    - 不认领的照常交给看图，平移、缩放、右键都和 Step1 一样。
  - **不要 DAPI 缩略图后备**：CPU 显示本来就是后备。
- **代价**：多一个常驻看图，最多再占 512 MB 原始缓存、2 GB 校正缓存和 1.5 GB 显存；三个看图的显存合计约 4.5 GB，接近 `ui/gpu_memory.py` 的 5 GB 目标。验收时在雷电外接固态盘和服务器机械盘上记下内存和切换时间。

## 3. 数据：一份标注文件
| 东西 | 身份 | 存在哪 |
|---|---|---|
| 片子 | `slide_id`（A3） | 项目记录 |
| 对应表位置 | `position_key`（F1，`core/tma/cores.py:37`） | 研究记录 |
| 核心 | `core_uid`（不变） | 标注文件 |
| 组织块 | `piece_id`；多边形，原图整数坐标 | 标注文件 |
| 排除区 | `exclusion_uid`（不变）；一个多边形；原因 blur / fold / other（可多选）；通道范围（全部或指定通道名）；备注；`created_in_workspace` | 标注文件 |
| 区域 | `region_id`：锁定时按 A3 规则由外形算出（`core/project_identity.py:236`） | 只在锁定版本里 |

- **所有块都是多边形**：
  - `Circle` 工具画出来的是 64 边形；
  - 改大小 = 以中心等比缩放顶点；顶点拖动只改一个顶点；
  - 旧的第 1 版候选列表、F3 草稿和锁定版本，读入时一律把圆转成 64 边形。

  所以工具和绘制只有多边形一套。
- **按片子存**：放在 `<project>/tma/`（F1–F3 已经在这里），文件里有 `slide_id`，片子身份不符就拒绝读取。
  - **排除区存的是用户画的原始外形，不裁剪**：在 ROI A 里画的排除区，到 ROI B 里照样保留原样；锁定版本里存的也是原始外形。
  - ROI 只决定看图里显示到哪里为止；真正按某个 ROI 裁剪，放到 F4 使用时再做，到时候每个工作区各裁各的。
  - 所以锁定时不按当前 ROI 裁剪，也不检查"是否在 ROI 内"。
  - TMA 片子上画了两个 ROI，也不会把阵列编号做两遍。
- **文件与版本**：
  - 草稿 `regions_draft.json`：停手 0.6 秒后原子写入；
  - 锁定时写 `regions_v<n>.json` 和 `tma_cores_v<n>.parquet`（沿用 F1 的表结构，有对应表时才写），版本号只增不覆盖；
  - 读取时拒绝不认识的格式版本。
- **旧数据迁移**：
  - 优先顺序：有 `regions_draft.json` 就用它；没有、但有 F3 的 `tma_layout_draft.json` 时，转换一次存成 `regions_draft.json`，旧文件改名为 `.migrated`。
  - 新旧草稿同时存在时（例如转换过程中断过），只用新草稿，旧草稿同样改名为 `.migrated`，不再读取。
  - 所以"放弃草稿"以后，任何旧草稿都不会复活。验收时专门测这种同时存在的情况。
  - 版本号接着所有旧文件往上数（`tma_layout_v*`、`tma_cores_v*`、`regions_v*` 中最大的加 1）；
  - 旧的锁定文件和 `tma_layout` 来源记录只读，不改写；
  - 圆转成 64 边形。
- **一份草稿、一条版本线、一份撤销记录**：标题栏只有一个"版本"下拉框、一个"放弃草稿"、一个"锁定"。
- **锁定检查分两部分**：
  - 有对应表时检查核心部分（F3 已有的全部规则）；
  - 排除区部分总是检查：外形不退化、至少一个原因、通道名存在、`region_id` 不重复（外形相同、类型相同的两个排除区不能同时锁定）。
- **排除区的区域类型**（A3 `rtype`）按原因定：
  - 只勾 blur → `blur`；
  - 只勾 fold → `fold`；
  - 其他情况（两个都勾，或含 other）→ `manual_annotation`。

  原因列表另存。`region_id = region_id(slide_id, 类型, 外框, 多边形)`，不改 A3。
- **排除区与核心的关系**：锁定时自动算出来，写进锁定版本，用户不用维护：
  - 排除区和哪个核心重叠面积最大，就归哪个核心；
  - 和任何核心都不重叠，就记为"不属于核心"。

  整颗排除仍用核心表里的 "Exclude…"，和局部排除区分开记录（9 月规范）。
- **来源记录**：新类型 `regions_layout`，取代 F3 的 `tma_layout`（F3 还没有真正锁定过的数据，`tma_layout` 保留在类型表里，只用于读取旧记录）。
  - `depends_on`：`raw_slide`，以及有的话 `tma_map_sheet`、`tma_dearray`；
  - **标注时用过的工作区都记下来**：每次编辑的操作记录里写明当时所在的工作区，以及那个工作区 Step0 交接文件的 sha256。锁定时把本版用到的所有交接去重后，写进 `unresolved_inputs`：`[{"role": "step0_handoff", "workspace_id": …, "path": …, "sha256": …}, …]`。
  - 文档是整片的，所以 `operates_on` 写整片（`full_wsi`），`slide_id` 照填；令牌用文件的 sha256。
- **轮廓自然贴合**（`core/tma/dearray.py`，F3b 第三版 M 部分原样执行）：
  - 检测层每块的掩膜外扩 3% 核心直径；扩出部分按分水岭分给最近的块，原来的组织不削，相邻块之间只在新扩部分留缝；
  - 取外轮廓，简化误差半个检测层像素，用 `level_to_global` 换算后取整；
  - 扩边前面积不小于 1% 核心面积的块保留；
  - 候选列表升到第 2 版，读取兼容第 1 版；
  - 碎块下限降低后，在真机上重新核对 Slice1 的候选数。
- **ROI 规则**：
  - **TMA 编号只在整片工作区里做**：TMA 片子在 Step0 应该整片 Save。在 ROI 工作区里，Cores 标签页只读，提示"TMA 编号请在整片工作区里做"。这样不会出现 ROI 外的核心没法编号、锁定又过不去的矛盾，也不需要靠虚报"缺失"或"不纳入"才能锁定。
  - 排除标注在 ROI 工作区里可以照常画；看图只显示 ROI 以内，画出 ROI 的部分照样保存原样（见上）。
  - 测试左上角不在 (0,0) 的 ROI 上的叠加层坐标。

## 4. 界面
- **步骤条**：`Step 0 → Step 0.5: Regions → Step 1 → Step 2 → Step 3 → Step 4`。程序内部新步骤号为 5（和页面栈位置不同），Step0–Step4 的步骤号和栈位置都不变。
- **左栏三个标签页**：
  - `Channels`：通道面板，加 `Intensity…`；
  - `Cores`：对应表、`Find cores`、编号面板、核心表，从 F3 搬过来；
  - `Exclusions`：排除区列表，显示编号、原因、通道范围和面积，点一行就跳过去。
- **标题栏**：研究和片子信息，`Version`、`Discard draft`、`Lock`。
- **工具行只有一套**：`Select`、`Polygon`、`Circle`、`Vertex`、`Merge`、`Line`、`Clicks`、`Undo`、`Redo`、`Prev`、`Next`。
- **按键**（同 Odon，`app.rs:12418-12685`）：
  - Enter 闭合多边形，或结束点选；
  - 退格删上一点；Esc 取消；Delete 删除；Ctrl+Z 撤销。
  
  切换标签页时，取消未完成的画线、点选或多边形；通道面板搜索框里的退格不受影响。
- **编号**完全保留 F3 的拖线 / 点选 / 单颗、预览和明确改绑（`core/tma/layout.py:206-257`）。

## 5. 分段（每段有自己的白名单和验收门；每段完成交 codex astra high 审核；用户真机验收通过才进下一段）
- **R0 F3 中间版提交**：已完成，提交 `2a3a5ed`。
- **R1 几何与标注文件（不涉及界面）**：
  - 内容：块一律多边形；候选列表第 2 版（轮廓、碎块下限）；合并后的标注文件、锁定、`regions_layout`；旧数据迁移；排除区数据。
  - **兼容边界**：R1 只**新增** `core/regions/`，**不动** `core/tma/layout*.py` 和 `ui/tma/`。F3 现有页面在 R1 期间照常可用，到 R2 换上新界面时才切到新模型；旧的 `core/tma/layout*.py` 在 R2 结束时删掉。
  - 白名单：
    - 新建 `core/regions/{__init__,document,geometry,numbering,lock,migrate}.py`、`tests/test_regions_r1.py`；
    - 改 `core/tma/dearray.py`（轮廓、碎块下限、第 2 版，读取兼容第 1 版；**第 2 版候选仍保留 `cy`、`cx`、`radius`、`area` 这些旧字段**，F3 现有页面在 R1 期间照常读取）、`core/provenance.py`（加 `regions_layout`）、`tests/test_tma_f2_dearray.py`。
  - 验收门：
    - 自动测试：轮廓贴合与不交叠（紧挨的块、贴边的块）、旧数据转换、锁定检查两部分、版本、`region_id` 不重复、排除区与核心按位置关联；
    - 真机：只核对 Slice1 的候选数和轮廓截图。
- **R2 模块外壳 + 编号功能完整迁移**：
  - 内容：
    - 进入检查（加载交接；不恢复 Step1 会话）；页面；第三个看图；通道共用；
    - 叠加层和事件过滤器；视野与导航小图；生命周期；
    - 把 F3 的 Cores 部件搬进来，切到 `core/regions/` 新模型；
    - **编号用到的工具全部在叠加层上可用**：`Select`（选中、移动、缩放）、`Line`、`Clicks`、`Merge`、`Circle`。F3 已有的编号功能在 R2 结束时完整可用，并通过全部旧测试；
    - 删掉 "○ TMA"、独立页面和 `core/tma/layout*.py`。
  - 白名单：
    - 新建 `ui/regions/{__init__,page,mount,overlay,tools,cores_panel}.py`、`tests/test_regions_r2.py`；
    - 改 / 删 `ui/tma/*`（部件搬到 `ui/regions/` 或被它引用；删掉 `tma_page.py` 的独立框架）、删 `core/tma/layout.py`、`layout_geometry.py`、`layout_lock.py` 和 `tests/test_tma_f3_layout.py`（测试内容迁到 `tests/test_regions_r1.py` / `test_regions_r2.py`）；
    - 改 `ui/main_window.py`、`UI_SURFACE_RULES.md`；
    - 测试：`tests/test_tma_f3_page.py`（整份，改为通过 Regions 构造）、`tests/test_ui_surface_contract.py`（整份）、`tests/test_v16_frame_lock.py`（TMA 部分改为 Regions）、`tests/test_global_channel_dock.py`（:1257-1266，挂载位置从 4 个变 5 个）。
  - 验收门：
    - 自动测试：
      - 进入检查的三种结果；Step0 正在写小块时拒绝进入；
      - 三个看图各自的暂停和恢复；换片子时关掉看图；关闭程序；重新同步；
      - 在 Regions 勾选，Step1 同步看到（反过来也一样）；
      - 视野双向联动；
      - 叠加层重新挂接并保持在最上层；工具认领与平移；右键菜单；
      - 通道面板 5 个挂载位置；走一遍各步不弹新窗口；
      - F3 的全部编号测试。
    - 真机：
      - Slice1 多通道的实际画面；通道与 Step1 同步；视野联动；
      - 拖动、缩放、改窗口大小以后，叠加层仍和组织对齐；
      - 隐藏的看图不占用显示；
      - 强制走 Rust → Python GPU → CPU 三条显示路径时，画面和叠加层都正常；
      - 内存和切换时间（雷电外接固态盘、服务器机械盘）；
      - 用编号工具完成几段编号（验证编号功能迁移完整）。
- **R3 工具与排除标注**：
  - 内容：叠加层上的 `Polygon`、`Vertex` 工具；Exclusions 标签页（列表、原因、通道范围）；排除区与核心的自动关联显示。
  - 白名单：
    - 新建 `ui/regions/exclusions_panel.py`、`tests/test_regions_r3.py`；
    - 改 `ui/regions/{tools,overlay,page}.py`、`core/regions/document.py`；
    - 改 `ui/main_window.py`（关闭时保存草稿）、`UI_SURFACE_RULES.md`、`tests/test_ui_surface_contract.py`。
  - 验收门：
    - 自动测试；全量回归；
    - 真机：Slice1 完成编号、修正、锁定（即上次没做完的第 4–8 步）；一张大组织 ROI 项目，画几个模糊 / 折叠区并锁定。

## 6. 刻意不做
- 自动检测模糊 / 折叠；画笔；拆分；组织掩膜；在更细的层上重新贴合轮廓；折线编号；自动铺满整个网格；
- 下游使用排除区（放到 F4）；
- 按标签页分开的版本线；DAPI 缩略图后备；"在 Step1 看大图"按钮；
- Step0 的通道状态。

## 7. 新机制（需用户授权）
- 第三个常驻看图（已同意）；
- 新步骤号 5；
- `core/regions/` 标注文件和 `regions_layout` 来源记录类型。

本版**不新增**导航小图机制，也**不改**共用看图文件 `ui/step1_viewer_mount.py`。

## 8. 工期
- R1 约 2–3 天；
- R2 约 3–4 天；
- R3 约 3–4 天；

合计约 8–11 天，含真机验收。

## 9. 第三版的审核记录（保留，供追溯）
- **codex** `codex_region_qc_plan_1.out`、`_2.out`；**Opus 5.5** 子代理；**Mythos 5.1** `mythos_region_qc_review_1.out`。
- 第三版审核的阻断项，在本版中的处理：
  - 进入检查 → §2；
  - 测试白名单 → §5 R2；
  - 换片子 / 关闭 / 同步 → §2；
  - 导航小图 → 改为沿用 Step1，不再需要新机制；
  - F3 未提交 → R0 已完成；
  - 存放位置 → 按片子存（用户同意 Mythos 的意见）；
  - 排除区固定编号 → §3。
- 第三版的建议，凡是和第四版的新做法不冲突的，都已写进正文。
- 那些专为"通道和 Step1 分开"而设的条目，在第四版里已经作废：单独的显示描述、CPU 合成子类、`"regions"` 显示范围的初始勾选、Intensity 改读 Regions 的通道。
