# v16 块 A1b · S1 — 页面框架 + Step1、Step3 迁移：实施申请 v2

日期：2026-09-30。分支 `v16`，调查基于 HEAD `3436953`。依据：S0 设计（`docs/v16_A1b_application.md`，已批准）§3、§4、§7。
状态：**申请 v2，用户 2026-09-30 批准**（§8 的 1–3 同意；授权提交后实施）。

修订记录：
- v1：初稿。
- v2：按独立审核修订（审核结论为有条件批准，并同意 §8 的 1–3），四条约束如下：
  ① 新增 `StepFrameMetrics`，作为长期唯一的几何来源；Step0 的部件只提供 S1 的初始值（§3.1）。
  ② `ColumnWidth` 的语义写死：`W_effective = max(W_user, common_min)`；最小值变小时不自动收窄；最小值按通道行模板计算；防止同步回授（§3.1）。
  ③ `ConfigPanel` 的边距在**实例层**设定，不改这个类，`config_panel.py` 移出白名单。只读核对：整个代码里只有一个实例，在 `ui/main_window.py:954`（§3.2、§4）。
  ④ `StepFrame` 只负责几何和布局，不认识任何 step；宽模式推迟到 S4 再做（§3.1）。

  另外增加验收门：Step1 与 Step3 的 graphics viewport、ViewBox、GPU 层三者的矩形关系必须相同（§7）；并写明 S1 只保证 Step1 = Step3（§7）。

---

## 1. 必要性与目标

S1 建立 `StepFrame`，并先迁移结构最接近它的两页：Step1 和 Step3。完成后：
- **Step1 ↔ Step3 切换时，画面纹丝不动**；
- 两页的通道面板在任何宽度下都完全一样，包括窄的时候权重框都可见。

Step0 在 S2 迁移，所以 S1 之后 Step0 ↔ Step1 之间仍会有布局位移，这是预期的。

## 2. 实测（1600 × 1000，窗口坐标，单位 px；只读探针）

| | Step1 | Step3 | S1 之后（两页相同） |
|---|---|---|---|
| 标题栏 | (6, 37) 高 30 | 同左 | 不变（`_HeightTwinBar`，与 Step0 的加载栏同高） |
| 左 / 右 tab 栏 | y 71，高 33 | y 71，高 33 | 不变 |
| 左栏（QTabWidget） | 291 宽 | **271 宽** | 相同，由共享宽度和共同的最小值决定 |
| tab pane 顶 | 105 | 105 | 不变 |
| 工具行 | 高 **22**（`sel_row`） | 高 **25**（`mode_row`） | 固定 **25**，下方 spacing 6 |
| viewer 顶 | 133 | 136 | 136 |
| 底部槽 | 高 **53**（Save 行） | 高 **33**（`← Back to Step 2`） | 固定为 Step0 Per-Channel Decision 框的高度（今天 53） |
| splitter 高度 | 861 | 881 | 861 |
| `Channels` 框 | (7, 108) 289 × 823，边距 4，spacing 4 | (7, 108) 269 × 843 | 同宽同高 |

左栏宽度不同的原因：两页的 splitter 总宽度相同（1588），但 Step1 有 `_hold_step1_channel_floor` 设的下限（287），Step3 没有；两页各自套用同一个比例，再被各自的最小值卡住。

## 3. 做法

### 3.1 新模块 `ui/step_frame.py`

**`StepFrame(QWidget)`**：
- 根布局 `QVBoxLayout`，边距 `6, 6, 6, 11`，spacing 4。
- **顶部槽**：
  - 标题子槽：`set_title(widget)`；
  - tab 子槽：由左、右两个 QTabWidget 的 tab 栏构成，沿用 `_STEP1_TAB_QSS` 和 `_free_the_tab_bar`；
  - 右 tab 栏的右角部件：`set_right_corner(widget)`，Step3 的运行下拉框和 `Load…` 放在这里。
- **中部**：`QSplitter`（横向，`childrenCollapsible(False)`，分隔条宽度统一），左 = 左 QTabWidget，右 = 右 QTabWidget。
  - `add_left_tab(name, widget)`、`add_right_tab(name, widget)`；
  - 右 tab 的内容页由框架包一层：顶部是**固定高度 25 的工具行**（`set_tool_row(tab_index, widget)`），下方 spacing 6，再下面是内容。
- **底部槽**：固定高度，高度来源与今天 `_match_step1_bottom_bar` 相同（Step0 `_decision_box` 的高度），做成与 `_HeightTwinBar` 同样的「跟随高度」。
  - `set_bottom(left_widget, right_widget)`；左段宽度跟随左栏（沿用 `_follow_step1_channels_box` 的规则）。
- **宽模式推迟到 S4**（审核意见 ④）：S1 不实现，免得为 Step4 提前写逻辑。
- **只负责几何和布局**：`StepFrame` 不认识 fusion、分割、ROI、mask 或任何具体的 step，代码里不出现 `if step == …`。各页的代码往槽里填内容，框架只管几何。

**`StepFrameMetrics`**（审核意见 ①）：一个只读的数值对象，是**长期唯一**的几何来源：
- `page_margins = (6, 6, 6, 11)`、`spacing = 4`、`title_height`、`tab_height`、`tool_height = 25`、`bottom_height`、`handle_width`、`tool_spacing = 6`。
- **S1 的初始值取 Step0 当时的真实尺寸**（`_file_bar` 的高度 → `title_height`，`_decision_box` 的高度 → `bottom_height`），在启动、Step0 完成布局后量一次，之后固定下来。Step1 / Step3 不再随时去问 Step0 的部件有多高。
- S2 迁移 Step0 时，Step0 也改为使用同一个 `StepFrameMetrics`。从那以后，任何一页改字体或边框，都不会暗中牵动其他页。

**`ColumnWidth`**（列宽权威，只有数值和一个应用函数，不是状态机；审核意见 ②）：
- `W_user`：用户最后拖到的宽度（窗口缩放时按比例跟随）。
- `common_min`：所有已迁移页面左栏所需的最大值，按**通道行模板的最小宽度**（行控件的最小宽度 + 滚动条 + 框的边框和边距）计算，**不依据当前有哪些行可见**。它替代只在进入 Step1 时算一次的 `_hold_step1_channel_floor`。
- **实际宽度 `W_effective = max(W_user, common_min)`**。
  - 最小值变小时，**不会自动收窄**左栏，因为 `W_user` 不变。所以隐藏某个控件，不会让 viewer 突然变宽。
  - 最小值变大时，左栏跟着变宽，但 `W_user` 不被改写。
- **防止同步回授**：用户拖动分隔条 → 更新 `W_user` 一次 → 把所有 splitter 设为 `W_effective`。在应用期间，由此产生的 `splitterMoved` 一律忽略（一个 re-entrancy 标志）。这样不会出现 Step1 → Step3 → Step1 的来回回授。
- **尚未迁移的 Step0 和 Step2** 继续走今天的比例路径（`_apply_channel_column_fraction` 里对应的部分），比例由 W 换算得出。今天「拖动一个、全部跟随」的行为保持，直到 S2 / S3 把它们也纳入 W。

### 3.2 通道列：Step1、Step3 共用

- 新函数 `build_channel_column(...)`：`Channels` 框（边距 4，spacing 4）、表头行（`Show all` + `Intensity…`，与 Step0 的 `Method ▾` 同宽，沿用已有规则）、横线、Reset weights / Load weights 行，最后是 dock 的挂载位置。
- Step1 不再经过 `ConfigPanel` 那层多出的 4 px 内边距：**在 Step1 放置它的地方设实例的边距**，写作 `self.config.layout().setContentsMargins(0, …)`，放在 `main_window` 里。**不改 `ConfigPanel` 这个类**（审核意见 ③）。Reset / Load 行的位置因此与 Step3 相同。`ConfigPanel` 的功能不变。
- **停止条件**：如果实施时发现 `ConfigPanel` 还有其他实例，或者这个设定会影响 S1 范围以外的页面，就停下来用 AskUserQuestion 请示。
- dock（`GlobalChannelDock`）的挂载机制不变（`_mount_channels_dock` / `mount_into`）。

### 3.3 Step1 迁移（`ui/main_window.py`，Step1 页的构建）

- `page1_w` 改为 `StepFrame`：
  - 标题栏 → 标题子槽；
  - 左 tab「Fusion」「Pre-segmentation」、右 tab「Viewer」「Patch Results」「Pre-seg Results」照原样放入；
  - `sel_row` → Viewer tab 的工具行；
  - 底栏（`Save Fusion Settings` 放左段；`Save Config & Generate fused.zarr` 与 `force_overwrite_zarr` 放右段）→ 底部槽。
- **属性名保留**：`_step1_page_widget`、`_step1_title_bar`、`_step1_main_split`、`_step1_channels_box`、`_btn_save_fusion_settings`、`_step1_save_slot` 等。现有测试和代码通过它们访问，指向框架里对应的部件。
- `_hold_step1_channel_floor` 并入 `ColumnWidth` 的共同最小值。`_match_step1_bottom_bar` 和 `_follow_step1_channels_box` 由框架的底部槽接管，行为相同。

### 3.4 Step3 迁移（`ui/step3_page.py` 与 `main_window._build_step3_page`）

- `assemble` 改为往 `StepFrame` 里填：
  - 标题栏 → 标题子槽；
  - 左「Fusion」→ 通道列；
  - 右「Viewer」→ 工具行 `mode_row` + viewer / notice；
  - 运行下拉框和 `Load…` → 右角部件；
  - `← Back to Step 2` → 底部槽的左段。

### 3.5 用户可见的变化（全部来自已批准的 S0 裁定，没有新增控件）

- **Step1**：
  - Reset weights / Load weights 行少了 4 px 的缩进，与 Step3 相同；
  - 工具行高 22 → 25，所以 viewer 下移 3 px、矮 3 px。
- **Step3**：
  - 底部槽高 33 → 53，`← Back to Step 2` 所在行变高，viewer 相应变矮；
  - 左栏宽度与 Step1 相同。
- **两页**：左栏的最小宽度相同，窄的时候都不会挡住权重框。

### 3.6 UI_SURFACE_RULES 对照（Step1、Step3 相关条目）

| 规则 | S1 处理 |
|---|---|
| :53-58 Step1 两栏，左栏按 Step0 自己的比例 | **修改**：按 S0 裁定，已迁移的页面共享一个像素宽度；Step0 在 S2 纳入同一个宽度（到那时就完全相同） |
| :81-83 标题栏与 Step0 的加载栏同高、同位置 | 保留（标题子槽跟随 Step0 `_file_bar` 的高度） |
| :85-87 `Intensity…` 在 `Channels` 框内顶部；页面外层没有滚动区 | 保留 |
| :88-93 表头行 = Step0 的表头行 | 保留 |
| :95-104 `Save Fusion Settings` 与 Channels 框同宽、在它正下方，高度对齐 Step0 Decision 框的边框；在 Pre-segmentation 下隐藏但保留位置；两页的 Channels 框起止在同一条线上 | 保留（底部槽左段） |
| :105-110 拖动到某一行放不下时停住，权重框等永远不被遮挡 | 保留，并扩展到 Step3（共同最小值） |
| :110-115 Patch 行带 Overlay / Fusion 和右端四个按钮；上面没有行，下面没有状态行 | 保留（工具行） |
| :229-243 Step3 = Step1 的布局与外观；右边只有一行；运行下拉框在 tab 栏右角 | 保留 |
| :266-274 A1 相机规则；`← Back to Step 2` 在下方 | 保留（底部槽） |
| :223-226、:280-282 一个共享的列宽，拖动任意一个都会跟随 | 保留行为；实现改为像素宽度 W |

S1 完成后，UI 规则按上表更新，写入 StepFrame 的几何契约（F1–F4，覆盖 Step1、Step3）。

## 4. 白名单

- **新增**：`ui/step_frame.py`、`tests/test_v16_frame_lock.py`、`scripts/diagnose_v16_a1b_frame.py`（五页矩形对照表，供验收用）
- `ui/main_window.py`，只改：
  - Step1 页的构建：`_build_ui` 中 `page1_w` 那一段（:810-1440）；
  - `_build_step3_page`（:1547-1631）；
  - 通道列的共享函数：`_channel_column_splitters` / `channel_column_fraction` / `_wire_channel_column_sync` / `_on_channel_column_dragged` / `_apply_channel_column_fraction` / `_fix_step1_split_ratio`（:1654-1777）；
  - `_hold_step1_channel_floor` / `_match_step1_bottom_bar` / `_follow_step1_channels_box`（:5108-5178）。
- `ui/step3_page.py`：`assemble` 与布局。
- **测试**（只更新布局相关的数值或语义，不删行为断言）：
  - `tests/test_step1_layout_block_a.py`
  - `tests/test_step3_page.py`
  - `tests/test_ui_surface_contract.py`
  - `tests/test_step0_step1_surface_details.py`
  - `tests/test_step2_layout.py`（「三页共享一个通道列」这条的语义）
  - `tests/test_step3_viewer.py`、`tests/test_v16_zero_drift.py`（如果 rig 需要）
- **文档**：`UI_SURFACE_RULES.md`、v2.2 的执行记录与日程、两份用户指南（如有涉及）。

## 5. 不改的范围

- Step0、Step2、Step4 页面本身（S2–S4）；Step1.5。
- 所有控件的功能、信号、文字和顺序；`GlobalChannelDock` 的内部和挂载机制；各步的行控件（滑条、权重框、`method_cb`）。
- viewer、GPU、调度、缓存、相机（A1a 已完成）。
- step 栏（`block01_step_bar`，不加按钮）。

## 6. 风险

| 风险 | 对策 |
|---|---|
| 属性名、父子关系被测试或代码依赖 | 全部保留；`test_ui_surface_contract` 的父子断言如果不再成立，改成断言框架里的对应关系，并在申请的执行记录里逐条列出 |
| Step0 / Step2 仍走比例路径，与 W 之间出现 1 px 的取整差 | 暂时保持今天 2 px 的测试容差；S2 / S3 纳入 W 之后改为 0 |
| 最小值现算的性能 | 只遍历可见的行，取最大值，开销与今天 `_hold_step1_channel_floor` 相同量级 |
| 底部槽的高度跟随 Step0 的 Decision 框，而 Step0 还没迁移 | 与今天 `_match_step1_bottom_bar` 的来源相同，行为不变 |

## 7. 验收门

**范围声明**：**S1 只保证 Step1 = Step3**。Step0 = Step1 = Step2 = Step3 要等 S2、S3 完成后才成立。测试和文档都照这个写，免得有人误以为 A1b 已经锁住了所有页面的几何。

**自动：**
- [ ] 在 1500 × 950、1600 × 1000、1920 × 1080、2050 × 1330 下，Step1 与 Step3 的以下矩形逐像素相同：标题子槽、两条 tab 栏、左右 tab pane、工具行、viewer（ViewBox）、底部槽、`Channels` 框、dock 的 x 和宽度
- [ ] 拖动 Step1 的分隔条后，Step3 相同；反过来也相同
- [ ] 拖到最窄时，两页每一行的权重框都完整可见，右边缘相同
- [ ] Step1 ↔ Step3 来回切：viewer 的矩形和相机都不变（沿用 `test_v16_zero_drift`）
- [ ] **三种矩形的关系相同**（审核意见）：Step1 与 Step3 的 graphics viewport、ViewBox、GPU 层三者在窗口坐标下的矩形分别相同，GPU 层 = ViewBox。GPU 这一项在真实 GL 下验证，离屏时只比较前两者。这样可以避免「外框相同、实际绘制区不同」的假通过。
- [ ] `ColumnWidth` 的语义：最小值变小时，左栏宽度不变；拖动一次只触发一次同步，不来回回授
- [ ] 回归：离屏和真实 GL 的相关模块，与 HEAD 对比没有新增失败
- [ ] 反向注入：把 Step3 的最小值去掉、把工具行高度改回各自的值，布局锁测试会变红

**真机（用户）：**
- [ ] Step1 ↔ Step3 来回切：图像、通道面板、tab 纹丝不动（包括拖宽、拖窄、最小宽度）
- [ ] 两页的通道面板在窄的时候完全一样，权重框都可见
- [ ] 其余功能照旧：tab 切换、Save、`← Back to Step 2`、运行下拉框、mask 按钮

## 8. 请用户裁定

1. **工具行统一高度 25**（Step3 现在的高度），Step1 的 viewer 因此下移 3 px。**建议同意。** 25 写在 `StepFrameMetrics.tool_height` 里，不在代码里散落。
2. **底部槽里 `← Back to Step 2` 的位置**：按 S0 放在左段，也就是今天的位置，只是行变高。**建议同意。**
3. **Step0 和 Step2 在 S1 期间仍走比例路径**（与共享宽度之间最多有 1–2 px 的取整差，S2 / S3 之后变为 0）。**建议同意。**

---

## 9. 执行记录（2026-09-30）

**已实施，自动验收通过；待用户真机验收。**

- **`ui/step_frame.py`（新）**：
  - `StepFrameMetrics`（冻结的 dataclass）：`page_margins` 6/6/6/11、`spacing` 4、`tool_height` 25、`tool_spacing` 6，`title_height` / `bottom_height` 在构建时取自 Step0 的 `_file_bar` / `_decision_box` 的尺寸提示，只取一次；
  - `StepFrame`：标题槽、splitter 加左右 QTabWidget（`_STEP1_TAB_QSS`、`_free_the_tab_bar`）、`tool_row()`、底部槽。不认识任何 step。
- **Step1**（`ui/main_window.py`）：`page1_w` 就是 `StepFrame`。标题栏、左右 tab、`sel_row`（工具行）和底栏都放进了对应的槽，所有属性名保留。`ConfigPanel` 的边距在实例上设为 0，类没有改。
- **Step3**（`ui/step3_page.py`）：`assemble` 往页面内部的 `StepFrame` 里填，新增 `left_panel()`；窗口传入同一份 `metrics`。
- **列宽**（`_apply_channel_column_fraction`、`_hold_step1_channel_floor`）：
  - 两个框架页面使用同一个像素宽度；
  - 共同的下限只在 dock 所在的面板上测量（`isAncestorOf`），并同时加到两个面板上；
  - 只有显示中的框架可以把宽度撑大并写回共享值。隐藏页面的 splitter 还是 Qt 的默认尺寸，不能用来量。
- **与申请的一处偏差**：§3.1 写的是「最小值变大时左栏跟着变宽，但 `W_user` 不被改写」，这与同段的「最小值变小时不收窄」互相矛盾。实现按审核的本意：**撑大后的宽度写回 `W_user`**，所以最小值回落时宽度不变（`test_a_floor_that_drops_does_not_narrow_the_column`）。
- **实施中发现并修正的两个问题**：
  - 最初在 dock 不在的面板上测量下限，读到了整块面板的宽度，下限逐步上升，结果左栏 478 px。已按 `isAncestorOf` 修正；
  - 最初用构建时（还没有布局）的 splitter 做夹住和写回，比例被污染成 0.31。已改为只有显示中的框架才能写回。
- **实测**（`scripts/diagnose_v16_a1b_frame.py`，真实 GL，1600 × 1000）：Step1 与 Step3 的标题槽、两条 tab 栏的行、左右栏、工具行（25）、底部槽（47）、`Channels` 框、dock、graphics viewport、ViewBox、**GPU 层（= ViewBox）全部逐像素相同**。离屏在 1600 × 1000 和 2050 × 1330 下也相同。Step0 仍然不同，要到 S2。
- **测试**：
  - 新增 `tests/test_v16_frame_lock.py`（10 条）：4 种窗口尺寸下所有矩形相同；拖动任意一页后两页一起变；两页拉到最窄时一样，而且权重框完整可见；两页持有同一个下限；下限回落时不收窄（因为 Step0 仍走比例路径，容差 1 px）；
  - `tests/test_ui_surface_contract.py`：标题栏的父部件改为框架的 `title_slot`。
- **反向注入**：以下三种改法都会让测试变红——下限只加在 Step1、Step3 用自己的工具行、不写回。
- **回归**：71 个离屏模块加 11 个 GL 模块，每个模块单独一个进程，与 HEAD 对比，**没有新增失败**。
- **文档**：`UI_SURFACE_RULES.md`（页面框架，以及列宽改为像素宽度）。
