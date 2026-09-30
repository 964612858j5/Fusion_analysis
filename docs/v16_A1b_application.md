# v16 块 A1b — 所有 step 共用的页面框架：S0 设计申请 v1

日期：2026-09-30。分支 `v16`，调查基于 HEAD `28838eb`（A1a 已提交，未推送）。
状态：**S0 设计申请，用户 2026-09-30 批准**（裁定见 §7）。 本申请只定框架的设计和分阶段计划。**S1–S4 每个阶段都会另写实施申请**（白名单、测试、真机验收），批准后才动代码。
依据：
- 用户裁定（2026-09-30）：「设计一个整个 GUI 的通用框架：顶部槽（标题子槽 + tab 子槽）、左小槽、右大槽、底部槽，按这个布局固定设计，沿用到所有 step；允许重构，Step0 的代价可以接受，允许延长时间。」
- A1 v3 §9.6 裁定 1–5（原样带入）：逐像素相等；dock 顶边按 (b)；Step3 底栏与 Step1 同高（在框架里就是底部槽统一高度）；最小宽度取各步最大值；日程可以延长。

---

## 1. 必要性

A1a 之后，Step0 / 1 / 3 之间切换时，相机在切片坐标下已经精确一致。但各页的布局是各自写的，**同样的窗口里，viewer、通道面板、tab 的矩形都不一样**（1600 × 1000 下，viewer 中心分别是 982.5 / 947.5 / 937.5 px），所以图像在屏幕上仍然会动。

逐页补齐几何（A1 v3 §9 的做法）能暂时对齐，但只要以后任何一页改动一个边距或加一行，就会重新错开。用户要的是**由结构本身保证一致**：所有 step 共用一个框架，页面只往槽里填内容。以后的 TMA 页面（核心网格、QualityMask）也直接用这个框架。

## 2. 调查结论：五个页面今天的样子（1600 × 1000，窗口坐标，单位 px）

| | 根布局边距 | 顶部 | tab | 左栏 | 右栏 | 底部 |
|---|---|---|---|---|---|---|
| Step0 | 6,6,6,6 | 加载栏 `_file_bar`（y 37，高 30） | **一个** QTabWidget「Background Correction」**横跨两栏**（Fusion 默认样式） | tab 内的 splitter 左侧：`Channels` 框 365 宽（默认约 9 px 边距） | tab 内：toolbar + viewer | **在 tab 里面**：Per-Channel Decision + `Save`，横跨两栏 |
| Step1 | 6,6,6,11 | 标题栏 `_HeightTwinBar`（与 Step0 加载栏同高） | 左「Fusion / Pre-segmentation」，右「Viewer / Patch Results / Pre-seg Results」（`_STEP1_TAB_QSS`） | 291：`Channels` 框（4 px）+ `ConfigPanel`（再 4 px） | `sel_row` + viewer | `Save Fusion Settings`（与 Channels 框同宽、在它正下方）+ `Save Config & Generate fused.zarr` + `force_overwrite_zarr` |
| Step2 | **8,8,8,8** | **居中的大号 QLabel 标题**（y 39，高 31） | **没有** | 271：参数 QScrollArea | Tile Status Overview + 进度 | `← Back to Step 1`、`Run`、`Stop` |
| Step3 | 6,6,6,11 | 标题栏 `_HeightTwinBar` | 左「Fusion」，右「Viewer」（运行下拉框和 `Load…` 在 tab 栏右角） | 271：`Channels` 框（4 px） | `mode_row` + viewer | `← Back to Step 2`（比 Step1 的底栏矮约 20 px） |
| Step4 | **10,10,10,10** | 居中的大号 QLabel 标题 | **没有** | **没有：单栏** | — | `← Back to Step 3`、`Batch...`、`Stop`、`Extract Features` |

**共享机制**：
- 通道列共享的是**比例**（`_channel_column_fraction`，`ui/main_window.py:1654-1766`），分别套用在 Step0 / 1 / 2 / 3 各自的 splitter 上。每个 splitter 的总宽度不同，也各自被自己的最小值卡住，所以左栏宽度不一样。
- 只有 Step1 有行宽下限（`_hold_step1_channel_floor`，:5108-5137），而且每次进入 Step1 只算一次。
- 顶部的 step 栏由主窗口共用（`block01_step_bar`，没有按钮，这一点由测试锁定）。
- Step1.5 页在 stack 里，但界面上已经进不去了（v14.1 起移除了入口，只有测试直接调用）。

**锁定布局的现有测试**（改框架时必须同步更新，或者保留被测的属性名）：
- `test_step0_step1_surface_details.py`（tab 栏和 Channels 框顶边对齐、底行等）
- `test_step1_layout_block_a.py`（比例在 0.02 以内）
- `test_step2_layout.py`（左栏在 2 px 以内、没有横向滚动条）
- `test_step3_page.py:201`（四页 320 ± 2）
- `test_ui_surface_contract.py`（Step1 的 tab 名、`_step1_main_split`、`_step1_title_bar` 的父子关系、step 栏没有按钮、每行控件等）

## 3. 框架设计

### 3.1 结构（所有 step 相同）

```text
┌─ StepFrame（一个 step 页；根边距与 spacing 由框架统一） ──────────────────┐
│ ┌─ 顶部槽 ─────────────────────────────────────────────────────────────┐ │
│ │ 标题子槽：固定高度 H_title                                            │ │
│ │ （Step0 = 加载栏；其他 = 标题 + 右侧按钮区，例如 Tissue Navigator）   │ │
│ ├─ tab 子槽：固定高度 H_tab ────────────────────────────────────────────┤ │
│ │ [左 tab 栏：与左栏同宽]    │ [右 tab 栏：与右栏同宽][右角部件]        │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│ ┌─ 左小槽（共享像素宽度 W） ┐┃┌─ 右大槽 ──────────────────────────────┐ │
│ │ tab pane（统一边框和内边距）││ tab pane（统一边框和内边距）          │ │
│ │ 内容：各 step 自己的        ││ 工具行子槽：固定高度 H_tool           │ │
│ │ （Step0 / 1 / 3 = 通道列）  ││ 内容区：viewer / 概览 / 表单          │ │
│ └─────────────────────────────┘┃└────────────────────────────────────────┘ │
│ ┌─ 底部槽：固定高度 H_bottom ──────────────────────────────────────────┐ │
│ │ [左段：与左栏同宽，例如 Save Fusion Settings] │ [右段：按钮，右对齐] │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

- **固定的量由框架唯一给出**：根边距、spacing、`H_title`、`H_tab`、`H_tool`、`H_bottom`、分隔条宽度、tab 样式（沿用 `_STEP1_TAB_QSS`）、pane 的边框和内边距。
- **左栏宽度 W 是一个共享的像素宽度**，所有页面共用一个「列宽权威」：拖动任意一页的分隔条，所有页面一起变；窗口缩放时按比例跟随，但所有页面始终相同。**最小宽度**取所有页面左栏内容所需的最大值（裁定 4），所有页面同时生效，替代只在 Step1 里算一次的 `_hold_step1_channel_floor`。
- **没有内容的子槽也占住原来的高度或宽度**（例如没有 tab 的页面，tab 子槽照样保留），这样页面之间的几何永远相同。
- 页面通过框架的接口填内容：`set_title(widget)`、`add_left_tab(name, widget)`、`add_right_tab(name, widget)`、`set_right_corner(widget)`、`set_tool_row(widget)`、`set_bottom(left_widget, right_widget)`。页面自己不再设置外层的边距、spacing 和 splitter。

### 3.2 通道列（Step0 / 1 / 3 的左槽内容）

一个共用的「通道列」容器：
- `Channels` 框（统一 4 px 边距）
- 表头行（统一高度；Step0 是 `Method ▾` + `Intensity…`，Step1 / 3 是 `Show all` + `Intensity…`，沿用已有裁定）
- 横线
- 可选的「Reset weights / Load weights」行（Step1 / 3 有，Step0 没有；按裁定 (b)，dock 的顶边允许因此不同）
- 共享 dock（`GlobalChannelDock`，挂载机制不变）

Step1 的 `ConfigPanel` 多出来的那 4 px 内边距去掉，与 Step3 一致。

### 3.3 几何契约（写成测试）

在同样的窗口尺寸下，所有 step 之间：
- **F1**：顶部槽、tab 子槽、左栏、右栏、底部槽的矩形逐像素相同。
- **F2**：Step0 / 1 / 3 的 `Channels` 框矩形逐像素相同；dock 的 x 和宽度相同；每行里各控件的横向位置和宽度相同，窄的时候权重框永远可见。
- **F3**：Step0 / 1 / 3 的 viewer（ViewBox）矩形逐像素相同。加上 A1a 的相机修复，切换时图像纹丝不动。
- **F4**：多种窗口尺寸下、拖动任意一页的分隔条之后、窄到最小宽度时，以上都成立。

验收脚本：把本次的测量探针整理成 `scripts/diagnose_v16_a1b_frame.py`，输出五页的矩形对照表。

### 3.4 各 step 的填法与用户可见的变化

| step | 标题子槽 | tab 子槽 | 左槽 | 右槽 | 底部槽 | 可见变化（需裁定的见 §6） |
|---|---|---|---|---|---|---|
| Step0 | 加载栏（原样） | 左、右各一个 tab（名字见裁定 3） | 通道列 | toolbar（工具行）+ viewer / compare | 左段：Per-Channel Decision；右段：`Save` | **「Background Correction」这一个横跨两栏的 tab 拆成左右两个；Save 行移出 tab pane，放到底部槽** |
| Step1 | 标题栏（原样） | 原样 | 通道列（去掉多出的 4 px） | `sel_row` + viewer | 原样（`Save Fusion Settings` 在左段） | 几乎没有 |
| Step2 | 标题栏式（左对齐，与其他页同高） | 裁定 2 | 参数滚动区（原样） | 标题行「Tile Status Overview」（作为工具行）+ 概览 + 进度 | `← Back to Step 1`；`Run`、`Stop` | 标题样式变化；边距统一；tab 子槽按裁定 2 |
| Step3 | 标题栏（原样） | 原样（右角部件照旧） | 通道列 | `mode_row` + viewer | `← Back to Step 2`（底部槽高度统一，变高约 20 px） | 底栏变高（已裁定） |
| Step4 | 标题栏式 | 裁定 2 | 裁定 4 | 裁定 4 | `← Back to Step 3`；`Batch...`、`Stop`、`Extract Features` | 由单栏改为按槽放置；标题样式变化 |

Step1.5（界面上已经进不去）不纳入本块，原样不动。

## 4. 分阶段计划（每个阶段单独申请、单独真机验收）

| 阶段 | 内容 | 预计 |
|---|---|---|
| S1 | `StepFrame` 与列宽权威；迁移 **Step1、Step3**；新增 F1–F4 的布局锁测试（先覆盖这两页）；更新相关的现有测试；UI 规则 | 1.5 天 |
| S2 | 迁移 **Step0**：拆 tab、Save 行移入底部槽、处理 workbench 的隐藏同步 splitter（`_cond_workbench._h_split`）、compare 模式；更新大量 Step0 布局测试和 UI 规则 | 2 天 |
| S3 | 迁移 **Step2** | 1 天 |
| S4 | 迁移 **Step4** | 0.5–1 天 |
| — | 回归与多轮真机验收的余量 | 1 天 |
| **合计** | | **约 6–6.5 天** |

在 S1 验收通过之后，Step1 ↔ Step3 的切换就应当完全不动；S2 之后，Step0 ↔ 1 ↔ 3 全部不动。

## 5. 规则、风险与边界

- **P0 规则**：
  - 框架本身是新的结构组件，由本申请明确授权；
  - 每个阶段都是封闭的白名单，只改布局和容器，**不改任何控件的功能、信号、文字或顺序**（除 §3.4 列出并经裁定的可见变化）；
  - 不改 viewer、调度、缓存、GPU；
  - 不新增状态机或 registry；列宽权威只是一个共享的数值加上它的应用函数。
- **UI_SURFACE_RULES**：已记录的裁定逐条对照，每条标明「保留」「映射到某个槽」或「本申请裁定修改」。S1 开工前把对照表写进 S1 申请。
- **风险**：
  - Step0 的布局和行为绑在一起：compare 模式、隐藏的同步 splitter、Save / Decision 行等。所以 Step0 单独作为一个阶段，放在 Step1 / 3 之后，那时框架已经得到验证。
  - 布局测试会有一批数值断言要改，只改数值或语义，不删行为断言。
  - 回归面大：每个阶段都要做离屏 + 真实 GL 的全量相关回归，逐个进程、与 HEAD 对比。
- **计划文档**：A1b 写入 v2.2 的日程（§12），TMA 的起点相应后移，用户已同意延长。

## 6. 裁定项（原文保留；结果见 §7）

1. **统一的根边距**：建议采用 Step1 / Step3 现在的值 `6, 6, 6, 11`，因为 Step0 / 1 / 3 的 tab 栏与 Channels 框的对齐就是按它调出来的。Step2 的 8 和 Step4 的 10 改成这个值。
2. **没有 tab 的页面（Step2、Step4）的 tab 子槽**：
   - (a) 保留一行等高的空白；
   - (b) 给它们各放一个 tab，名字建议 Step2 左「Parameters」、右「Tile Status」，Step4 见裁定 4；
   - **建议 (b)**：所有页面外观一致，也不会留出莫名的空白。
3. **Step0 的 tab 名**：「Background Correction」拆成左右两个。建议左边叫「Background Correction」（它的内容就是按通道选校正方法），右边叫「Viewer」（与 Step1 / 3 一致）。
4. **Step4 怎么放进左右两栏**：
   - (a) 左槽放 `Input Files` + `What to quantify`，右槽放 `Output` + 进度；
   - (b) 所有内容放在右槽，左槽保留同宽但空着；
   - (c) Step4 用「宽模式」，左右合并成一栏。这样 Step4 的左右几何就与其他页不同，但它没有 viewer 和通道面板，看不出位移；
   - **建议 (a)**：左窄右宽正好对应「选项在左、结果在右」，也与 Step2「参数在左、概览在右」一致。
5. **底部槽的高度**：建议取 Step1 现在底栏的高度（它已经与 Step0 的 Per-Channel Decision 边框对齐）。所有页面统一为这个高度。
6. **S0 本身**：批准这个框架设计和分阶段计划后，我写 **S1（框架 + Step1、Step3）的实施申请**，包括白名单、UI 规则对照表和测试清单。

## 7. 用户裁定（2026-09-30）

1. **统一的根边距** `6, 6, 6, 11`：**同意**。
2. **没有 tab 的页面**：
   - **Step2 按 (b)**：左 tab「Parameters」，右 tab「Tile Status」；
   - **Step4 按 (a)**：tab 子槽保留一行等高的空白。
3. **Step0 的 tab**：左「Background Correction」、右「Viewer」。**同意**。
4. **Step4 豁免左右分栏**：它的左右槽**合并为一个槽**（框架的「宽模式」），内容照原来单栏排列。
   - 所以 F1 契约里的「左栏 / 右栏矩形相同」不适用于 Step4；
   - 顶部槽（标题 + 空白 tab 行）和底部槽的几何仍然与其他页相同；
   - Step4 合并后的内容区 = 其他页左栏 + 分隔条 + 右栏所占的矩形。
5. **底部槽**统一采用 Step1 现在底栏的高度：**同意**。
6. **批准 S0**。下一步写 S1（框架 + Step1、Step3）的实施申请。
