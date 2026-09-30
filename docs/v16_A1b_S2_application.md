# v16 块 A1b · S2 — Step0 迁移到页面框架：实施申请 v2

日期：2026-09-30。分支 `v16`，调查基于 HEAD `c3b70b1`（S1 已提交）。依据：S0 设计（`docs/v16_A1b_application.md`，已批准）§3.4、§4、§7；S1（`docs/v16_A1b_S1_application.md`）§9 的执行记录。
状态：**申请 v2，用户 2026-09-30 批准**（§8：1 选 (a)；2 按 v2 方案；3、4 同意；授权提交后实施）。

修订记录：
- v1：初稿。用户裁定：1 选 (a)，3、4 同意；2 不接受残余，要求彻底消除，并允许「通道列按同一宽度，覆盖 Channels 面板右侧的组件（例如权重框）」，适用于所有 step 的 Channels。
- v2：共同下限不再取决于通道行（§3.2、§8.2），因此不会再出现某一页把列撑宽的情况。相应推翻两条旧规则：「窄的时候权重框永远可见」和「权重框永远不被遮住」（§3.5）。

---

## 1. 必要性与目标

S1 之后，Step1 ↔ Step3 已经逐像素不动。但 Step0 仍然是自己排的布局，所以 Step0 ↔ Step1 切换时，通道面板和图像还会移动。S2 把 Step0 也放进同一个 `StepFrame`。完成后：

- Step0 = Step1 = Step3：标题槽、tab 栏、左右栏、工具行、viewer、底部槽、`Channels` 框、dock 的 x 和宽度，逐像素相同；
- 三页共用**一个像素列宽**。S1 里 Step0 的 ≤1 px 取整差随之消失，测试容差收紧到 0。

## 2. 实测（1600 × 1000，窗口坐标，单位 px；只读探针，在 test1 的副本上运行）

| 部分 | Step0 今天 | Step1 = Step3 |
|---|---|---|
| `Channels` 框 | 12, 108, **365** × 823 | 7, 108, **289** × 823 |
| dock | **22**, 222, 345 × 699 | **12**, 188, 279 × 738 |
| graphics viewport | **377, 134**, 1211 × 797 | **302, 136**, 1291 × 795 |
| ViewBox | 386, 143, 1193 × 779 | 311, 145, 1273 × 777 |
| 标题 / tab 栏 / 底部槽 | 不在框架里（加载栏 y 37 高 30；**一个**横跨两栏的 tab；Save 行在 tab 框**里面**） | 标题 6, 37, 1588 × 30；底部槽 6, 936, 1588 × 53 |

差别来自四个地方：

1. **结构**：Step0 是「一个 tab `Background Correction` → `main_split`（只剩一个子部件）→ `sec_c`（边距 4，底色 #1c1c1c）→ `_bg_c_split`［左：`Channels` 列｜右：toolbar + viewer］，再加上 tab 框内的 Save 行」。于是 viewer 和通道列都比 Step1 多嵌了一层边框和边距。
2. **`Channels` 框的内边距**：Step0 用 Qt 默认的约 9 px，Step1 用 4 px，所以 dock 在框里的位置差 5 px，每边宽度差 10 px。Step1 的表头行是按 Step0 的边距**反算**缩进的（`main_window.py:910-915`），用来对齐两页的表头。
3. **列宽**：
   - Step0 开页时按自己的规则设宽（最小宽度的 4/3，即 v15 的用户反馈，= 365）；
   - 共享比例却是在另一时刻量到的（0.171，对应 270 px）。进一次 Step1 之后，Step0 自己也被改成 270，Step1 被最小值夹到 291。
   - 所以今天三页的列宽取决于你先去过哪一页。这就是你看到的 Step0 ↔ 1 位移的主要来源。
4. **工具行**：Step0 的 toolbar 高 22，Step1 / Step3 的工具行高 25。

**最小宽度（下限）的实测**：

| 页 | 行需要的宽度（行最小宽 + 框内边距 + 滚动条） | 左栏内容本身的最小宽 | 该页需要 |
|---|---|---|---|
| Step0 | 196 + 30 + 14 = 240（改成 4 px 边距后是 230） | 270（表头行：`Method ▾` + `Intensity…` + 隐藏后仍占位的 `Show all`） | 270（改边距后约 260） |
| Step1 / Step3 | 253 + 20 + 14 = 287 | 193 | 287 |

隐藏的 workbench 同步 splitter（`_cond_workbench._h_split`）左侧的最小宽度是 155，**不会**成为约束；`apply_channel_column_width` 仍然负责让它跟上。

## 3. 做法

### 3.1 Step0 页的组装（`ui/step0/step0_page.py`，`_build_ui` 中组装的部分）

控件本身一个都不新建、不删除，只是换容器：

```
Step0Page（外层布局，边距 0）
└ StepFrame(metrics)
  ├ 标题槽：_file_bar（原样）
  ├ 左 tab「Background Correction」：c_left（Channels 框）
  ├ 右 tab「Viewer」：view_box［工具行 = toolbar；_view_area（全图 / compare）］
  └ 底部槽：_decision_box ｜ stretch ｜ Save（_btn_continue）
```

- **`_bg_c_split` = `frame.splitter`**（名字保留）。**`_step0_tabs` = 左 tab**（名字保留，所以「只有一个 Background Correction tab」这类断言、`_background_correction_tab_width` 都继续成立）。
- **去掉** `main_split` / `sec_c` 这两层，以及只为 `main_split` 服务的 `_fix_split_ratio`（在 `showEvent` / `resizeEvent` 里调用。它现在只有一个子部件，已经什么都不做；如果留着，一旦指向框架的 splitter，就会把列宽强行改成 1:3）。`_main_split` 这个属性随之删除，测试里 3 处引用改为 `_frame`。
- **toolbar 放进工具行**：用 `frame.tool_row(view_lay, row)`，其中 `row` 是只装着 toolbar 的 `QHBoxLayout`。toolbar 本身不改。
- **`Channels` 框的内边距**：`chl` 改为 `4, 4, 4, 4`、间距 4，与 Step1 相同。Step1 表头的反算缩进因此自动变为 0，两页的表头仍然对齐。
- **`StepFrameMetrics` 由 Step0 在组装时生成**：标题高 = `_file_bar.sizeHint()`，底部高 = `_decision_box.sizeHint()`，两个值与 S1 相同，只是量的地方从 main_window 挪到 Step0 自己组装的时候。暴露为 `Step0Page.frame_metrics`，main_window 改为 `self._frame_metrics = self._step0.frame_metrics`。
- **tab 样式与 `_free_the_tab_bar` 搬进 `ui/step_frame.py`**（`FRAME_TAB_QSS`、`free_tab_bar`），作为框架的默认值。main_window 里原来的两个名字保留为导入的别名，Step0 无须依赖 main_window。
- **底部槽的左边缘对齐 `Channels` 框的左边缘**（与 Step1 的 `Save Fusion Settings` 规则相同，今天是 1 px 的 tab 框边）。Save 贴底部槽的右边缘，也就是右栏的右边缘。
- 新增 `left_panel()`，返回 c_left，供共同下限使用（与 Step3 一样）。

### 3.2 通道列：三页一个像素宽度（`ui/main_window.py`）

- `_channel_column_frame_splitters()` 加入 Step0 的 `_bg_c_split`。`_apply_channel_column_fraction` 里原来「Step0 先走、它的结果说了算」那一段删除，Step0 并入框架的循环；只是写入 Step0 时仍然调用 `apply_channel_column_width(W)`，这样隐藏的同步 splitter 也跟着走。
- **开页宽度**：三页都按 Step0 的「最小宽度的 4/3」规则打开（改边距后约 352）。见裁定 1。实现上把这个值作为共享宽度的**初始值**写入一次，不再在布局稳定之前从某一页现量比例。
- **共同下限（v2，按用户裁定 2）：不再看通道行。**
  - **为什么 Step1 会撑宽**：S1 的下限 = 「最宽那一行的最小宽度 + 框内边距 + 滚动条」，目的是保证权重框永远可见。Step1 / Step3 的行有滑条（最小 60）加权重框（56），Step0 的行只有 method 下拉框（64），所以 Step1 的行要宽得多（287 对 240）。而且 dock 只有一个，行按当前页切换成该页的样子，所以 Step1 需要多宽，只有 dock 挂到 Step1 时才量得到。于是会出现「在 Step0 拖到最窄 → 第一次进 Step1 被撑宽」。
  - **新规则**：下限 = 三页 `Channels` 列**自身内容**的最小宽度中最大的那个。这个内容就是表头行等，**不包括**列表里的行，因为列表本来就不把行的宽度往上报。今天的决定因素是 Step0 的表头（`Method ▾` + `Intensity…` + 占位的 `Show all`），改边距后约 260。
  - 这三个值在窗口建好时就能算出（隐藏页面的布局一样给得出最小宽度），与 dock 挂在哪页、有几个通道、通道名多长**都无关**。所以三页从一开始就持有同一个下限，任何时候都不会有某一页把列撑宽。
  - **代价**：列比某一行需要的更窄时，这一行从**右边**被列的边缘遮住。Step1 / Step3 先遮住权重框，再往里是滑条；Step0 是 method 下拉框。例子：在 test1 上把 Step1 拉到最窄（271），通道名都短，权重框仍然完整可见；如果通道名很长（名字列按最长的名字统一宽度），权重框就会被遮住一部分。拖宽即可看到，功能不受影响。
  - `_hold_step1_channel_floor` 因此不再测量 dock 的行，也不再需要「只在 dock 所在的那一页量」的判断，函数变简单。
- 保留 S1 的语义：W = max(W_user, 下限)，下限把列撑宽时写回 W_user。下限现在是固定值，只在窗口建好时（字体确定后）算一次，所以撑宽只可能发生在开页那一次。
- Step2 仍然按比例取（S3 再迁）。

### 3.3 Step1 底栏的 +1 px（`_match_step1_bottom_bar`）

那 1 px 是因为「Step0 的行在 tab 框里面，下面有 1 px 边框」。S2 之后 Step0 的行在底部槽里，下面没有边框，所以按实测改成让 `Save Fusion Settings` 仍然与 Decision 框的边框对齐。现有测试 `test_step1_has_no_back_button_and_save_fusion_takes_its_place` 会验证。

### 3.4 用户可见的变化（除第 1 条来自 S0 裁定 3 外，都是框架带来的几何变化；没有新增或删除控件）

| # | 变化 | 大小 |
|---|---|---|
| 1 | Step0 的一个 tab 拆成两个：左「Background Correction」、右「Viewer」；tab 样式与 Step1 相同 | S0 裁定 3 |
| 2 | Per-Channel Decision + Save 这一行从 tab 框里移到框下的底部槽：纵向位置不变（y 936，高 53）；Decision 框左移约 4 px（对齐 Channels 框），Save 右移约 5 px | 小 |
| 3 | Step0 内容区的底色 #1c1c1c 改为 tab 框的底色，与 Step1 一样 | 小 |
| 4 | Step0 的 `Channels` 框内边距 9 → 4：dock 和表头左移、上移约 5 px；Step1 的表头也左移约 5 px，两页仍然对齐 | 小 |
| 5 | 三页通道列同宽：开页约 352（今天 Step0 365、Step1 291）；最窄约 260（今天 Step1 / Step3 是 287），任何一页都不会把列撑宽 | 裁定 1、2 |
| 5b | 列拉得很窄时，行的右侧（Step1 / Step3 的权重框、Step0 的 method 框）可能被列的边缘遮住 | 裁定 2（用户允许） |
| 6 | Step0 的 viewer 与 Step1 / Step3 完全相同：左移约 75 px，上移 2 px | 这就是目标 |
| 7 | Step0 的 toolbar 在 25 px 的工具行里垂直居中，按钮下移约 1–2 px | 小 |

### 3.5 UI_SURFACE_RULES 对照（Step0 相关条目）

| 条目 | 处理 |
|---|---|
| Step0 只有一个工作区，没有第二个顶层 Remap tab | **保留**：左 tab 仍然只有「Background Correction」，右 tab「Viewer」是同一个工作区的右半边（S0 裁定 3） |
| `Per-Channel Decision` 排成一行，在底部，`Save` 左边；图像下面再没有别的 | **映射到底部槽**：左段 Decision、右段 Save |
| 状态行宽度 = 最长的 `Saved: …` + 一个 `Background Correction` tab 的宽度 | **保留**：量的是左 tab。tab 样式变了，宽度会差几个像素；共同下限约 260，大于这个 tab 需要的宽度（约 160），所以它永远不会被省略号截短 |
| `Method ▾` 领头、`Intensity…` 紧随、隐藏的 `Show all` 保留占位 | **保留** |
| patch 选择器在 viewer toolbar 左端、`Original | TopHat | cuCIM` 之前 | **保留**（toolbar 原样，进工具行） |
| 通道列开页为最小宽度的 4/3（v15 反馈） | **保留并扩展到 Step1 / Step3**（裁定 1） |
| Step1 的 `Save Fusion Settings` 与 Decision 框边框同高、同一行 | **保留**（§3.3） |
| 通道列的右边缘、滚动条、权重框和 `Save Fusion Settings` 永远不被 viewer 遮住；权重框贴行的右边缘（`UI_SURFACE_RULES.md:107`） | **按本申请裁定 2 修改**：滚动条和 `Save Fusion Settings` 仍然不会被遮住（它们跟着列走），**权重框允许被列的边缘遮住**；权重框贴行的右边缘这一条保留 |
| S1 的「下限在 dock 所在的页上量，两页都不会遮住权重框」（`UI_SURFACE_RULES.md:230`） | **改为**：下限来自各页 `Channels` 列自身的内容，与行无关 |

## 4. 白名单

- `ui/step0/step0_page.py`，只改：
  - `_build_ui` 中组装页面的部分（:542-660 的外层 / file_bar / tab，:863-890 的 `sec_c` / `c_split` / `chl` 边距，:1265-1280 的 `view_box`，:1440-1480 的 Save 行）；
  - `showEvent` / `resizeEvent` / `_fix_split_ratio`（:5959-5980）；
  - `_wire_left_column_sync` 中开页宽度的测量对象（:3491-3516）；
  - 新增 `frame_metrics`、`left_panel()`，以及底部槽左边缘的对齐。
- `ui/step_frame.py`：搬入 `FRAME_TAB_QSS`、`free_tab_bar`，作为默认值。
- `ui/main_window.py`，只改：
  - `_STEP1_TAB_QSS` / `_free_the_tab_bar` 改为从 `step_frame` 导入（名字保留）；
  - `_frame_metrics` 的来源；
  - `_channel_column_frame_splitters` / `channel_column_fraction` / `_apply_channel_column_fraction`；
  - `_hold_step1_channel_floor` / `_match_step1_bottom_bar`。
- **测试**（只更新布局相关的数值或语义，不删行为断言）：
  - `tests/test_v16_frame_lock.py`：各项加入 Step0（Step0 没有 GPU 层）；Step0 的容差从 1 px 收紧到 0；「最窄时不遮住权重框」改为「最窄时三页同宽，并且等于共同下限」；新增「下限在进入任何一页之前就已确定，从 Step0 拉到最窄后第一次进 Step1，列宽不变」；
  - `tests/test_step0_background_correction_tab.py`：`_main_split` → `_frame`；「Save 在 tab 里」改为「Save 在框架的底部槽里」；
  - `tests/test_step0_channel_conditioning.py`、`tests/test_step0_full_image.py`：tab 断言改为「左 = [Background Correction]，右 = [Viewer]」；
  - `tests/test_step0_step1_surface_details.py`（其中 `test_step1_channel_column_is_never_covered` 按裁定 2 改为「滚动条和列边缘不被 viewer 遮住」，不再要求权重框完整可见）、`tests/test_step1_layout_block_a.py`、`tests/test_step2_layout.py`、`tests/test_step3_page.py`、`tests/test_ui_surface_contract.py`：数值或语义按需更新。
- `scripts/diagnose_v16_a1b_frame.py`：Step0 也输出框架的各部分。
- **文档**：`UI_SURFACE_RULES.md`；两份用户指南（`docs/user_guide.md` 与 `docs/用户指南.md`）中「各页布局仍略有不同，图像可能偏几个像素」这句改为「Step0 / 1 / 3 布局相同，切换时图像不动」，并写上 Step0 的两个 tab 名；v2.2 的执行记录。

## 5. 不改的范围

- 所有控件的功能、信号、文字和顺序：Decision、Save、Method 弹窗、Intensity 窗口、Tissue Navigator、compare 模式、patch 选择器。
- 隐藏的 conditioning workbench 本身（`channel_workbench.py`），以及它与 Intensity 窗口的关系。
- `GlobalChannelDock` 的内部和挂载机制；dock adapter。
- viewer、GPU、调度、缓存、相机。
- Step2、Step4（S3、S4）；Step1.5。

## 6. 风险

| 风险 | 对策 |
|---|---|
| Step0 布局与行为耦合：compare 模式、隐藏同步 splitter、eventFilter、浮动窗口 | 只换容器，不换控件实例；compare 仍然是同一个 `_view_area` 的第二页；逐条跑 Step0 全部离屏测试和 GL 测试 |
| 删掉 `main_split` 后，有代码按父子关系查找 | 只读 grep：`_main_split` 只在 `_fix_split_ratio` 和 3 条测试里用到 |
| 开页宽度的时序：今天共享比例在布局稳定前被量走 | 初始值只写一次（§3.2）；新增测试「首次打开时三页都是 4/3 规则的宽度」，并用反向注入证明它能抓到问题 |
| Step0 单独使用（没有 main_window 的测试） | Step0 自己生成 metrics、自己组装框架，不依赖窗口 |
| tab 省略号改变状态行宽度 | 共同下限约 260，比「Background Correction」这个 tab 需要的宽度（约 160）大 |
| 行比列宽时，Qt 怎么处理这一行 | 探针实测：行宽低于最小宽度时，先收窄名字列和滑条的伸缩部分，再从右边被列表视口裁掉；不会重叠，也不会出现横向滚动条。实施时用一条长名字的测试确认 |
| 下限在隐藏页面上算不准 | 只用布局给出的最小宽度（不依赖页面是否已显示）；测试在从未显示过 Step1 的情况下验证下限 |

## 7. 验收门

**范围声明**：S2 保证 Step0 = Step1 = Step3。Step2 在 S3、Step4 在 S4 之后才纳入。

**自动：**
- [ ] 在 1500 × 950、1600 × 1000、1920 × 1080、2050 × 1330 下，Step0 / 1 / 3 的以下矩形逐像素相同：标题槽、两条 tab 栏的行、左右栏、工具行、graphics viewport、ViewBox、底部槽、`Channels` 框、dock 的 x 和宽度；真实 GL 下 Step1 / 3 的 GPU 层 = ViewBox
- [ ] 在任意一页拖动，三页一起变；拖到最窄时三页同宽，并且等于共同下限（与通道行无关）
- [ ] 下限回落时不收窄（容差 0）；打开项目后还没进过 Step1，就在 Step0 拖到最窄，然后第一次进 Step1、Step3：列宽不变（这就是裁定 2 要消除的情况）
- [ ] 首次打开：三页的列宽都等于 4/3 规则算出的宽度
- [ ] Step0 的 compare 模式：进出 compare 后，工具行和 viewer 的矩形不变
- [ ] Decision + Save 在底部槽里，排成一行；`Save Fusion Settings` 与 Decision 框的边框同高、同一行
- [ ] 回归：离屏和真实 GL 的相关模块逐个进程运行，与 HEAD 对比，没有新增失败
- [ ] 反向注入：去掉 Step0 的下限、恢复 Step0 的 9 px 边距、恢复「Step0 先走」那一段、恢复按行量下限，布局锁测试都会变红

**真机（用户）：**
- [ ] Step0 ↔ Step1 ↔ Step3 来回切：图像、通道面板、tab 纹丝不动（包括拖宽、拖窄、最窄；特别是打开后先在 Step0 拉到最窄再进 Step1）
- [ ] 拉到最窄时，权重框被遮住的程度可以接受
- [ ] Step0 的功能照旧：Load、Method ▾、Intensity…、Decision 的 Apply、Save、右键进出 compare、patch 选择器、Tissue Navigator

## 8. 请用户裁定

1. **开页宽度**：
   - (a) 三页都按 Step0 的「最小宽度的 4/3」规则打开（约 352 px，是 v15 你给 Step0 定的规则）；
   - (b) 三页都从共同下限（287 px）打开，更窄，图像更大。
   - **建议 (a)**，沿用已有的裁定。
2. **下限的残余情况**：v1 建议接受；用户裁定**彻底消除**，并允许覆盖 Channels 面板右侧的组件（适用于所有 step）。v2 的方案（§3.2）：
   - 下限只取各页 `Channels` 列自身的内容（表头行等），不看通道行，约 260；
   - 列比某一行窄时，这一行从右边被遮住（先是权重框 / method 框）；
   - 推翻「权重框永远可见 / 不被遮住」这两条旧规则（§3.5）。
   - 另一种方案是在 Step0 上预先按 Step1 的行模板计算宽度，要改 dock 的行，而且仍然不能保证行宽只与当前页有关，所以不采用。
   - **请确认 v2 的方案。**
3. **底部槽里 Decision 的左边缘**对齐 `Channels` 框的左边缘（与 Step1 的 `Save Fusion Settings` 规则一致）；Save 贴右边缘。**建议同意。**
4. **Step0 的 `Channels` 框内边距改为 4**（与 Step1 相同，§3.4 的第 4 条）。这是 dock 在三页里 x 和宽度相同的必要条件。**建议同意。**

## 9. 用户裁定（2026-09-30）

1. 开页宽度：**(a)**，三页都按「最小宽度的 4/3」规则。
2. 下限：**彻底消除**残余；下限只取各页 `Channels` 列自身的内容，允许覆盖行右侧的组件（所有 step 的 Channels 都适用）。**批准 v2。**
3. Decision 左边缘对齐 `Channels` 框，Save 贴右边缘：**同意**。
4. Step0 的 `Channels` 框内边距改为 4：**同意**。

## 10. 执行记录（2026-09-30）

**已实施，自动验收通过；待用户真机验收。**

- **Step0**（`ui/step0/step0_page.py`）：
  - 页面组装进 `StepFrame`（`_assemble_frame`）：标题槽 = 加载栏；左 tab「Background Correction」= Channels 列；右 tab「Viewer」= 工具行（toolbar）+ `_view_area`；底部槽 = Decision ｜ stretch ｜ Save。
  - `main_split` / `sec_c` / `c_split` / `c_right` 以及 `_fix_split_ratio` 已删除；`_bg_c_split` = 框架的 splitter，`_step0_tabs` = 左 tab。
  - `Channels` 框内边距 4 / 间距 4。Channels 列加上 Step1 / Step3 列同样的 `#1c1c1c` 底色和 3 px 顶边距（这是逐像素相同的必要条件：Step1 那 3 px 本来就是为了对齐 Step0 的 `sec_c` 边距）。所以 §3.4 第 3 条「底色变化」只发生在右栏，左栏的底色与今天相同。
  - `frame_metrics` 在组装时量一次；`opening_channel_column_width()`（4/3 规则）；`left_panel()`；`_align_bottom_row()`（Decision 对齐 Channels 框左边缘）。
- **`ui/step_frame.py`**：搬入 `FRAME_TAB_QSS`、`free_tab_bar`；main_window 保留原名作为别名。
- **`ui/main_window.py`**：
  - metrics 取自 `self._step0.frame_metrics`；
  - Step0 并入框架的像素宽度循环，写入仍经 `apply_channel_column_width`（它还没接上隐藏的同步 splitter 时，直接写 splitter）；
  - 开页宽度：第一次从**显示中**的框架取 4/3 规则并记住；
  - 下限：三列自身内容最小宽度中的最大值，与行无关；
  - `_match_step1_bottom_bar` 去掉 +1。
- **白名单扩展（用户 2026-09-30 批准）**：`ui/widgets/channel_dock/global_dock.py` 新增 `GlobalChannelRow.hold_own_width()`，在 `set_step` 之后和统一名字宽度之后调用，让行不窄于自己的最小宽度。
  - 起因：长通道名实测发现，列比行窄时，名字列固定宽度不收缩，滑条被画到名字上面；
  - 现在行从右边被列表视口裁掉，名字完整，也没有横向滚动条。
- **与申请的偏差**：
  1. 开页宽度那一段一度被当作多余删掉：在真实程序和 frame_lock 的 rig 里，没有它也开在 346。回归中 `test_a_drag_reaches_step0_s_hidden_peer_too` 失败：先显示 Step1 的窗口里，共享比例是从还没布局的 Step0 splitter 上量来的，每次又从上次的结果重新量，结果三页的列都是 862 px。于是恢复了这一段，这条测试就是它的反向注入证据。
  2. `test_step0_channel_conditioning`、`test_step0_full_image` 的 tab 断言不需要修改：`_step0_tabs` 就是左 tab，只有「Background Correction」一个。
  3. 下限实测约 256；因为 tab 控件自身的边框，最窄的列比下限宽 2 px，三页相同。
- **实测**（`scripts/diagnose_v16_a1b_frame.py`，test1 副本）：
  - 离屏和真实 GL 下，1600 × 1000 与 2050 × 1330：Step0 / 1 / 3 的标题、左右栏、工具行、graphics viewport、ViewBox、底部槽、`Channels` 框、dock 的 x 和宽度全部相同；
  - 真实 GL 下 GPU 层 = ViewBox；
  - 三页开页列宽 346；
  - 只有 tab 标签的宽度和 dock 的顶边不同（后者是 S0 裁定 (b)）。
- **测试**：
  - `test_v16_frame_lock.py` 扩展到三页，共 15 条：矩形相同；任意一页拖动；最窄等于下限；三页同一下限；未进过的页不撑宽；开页 4/3；compare 模式不移动工具行和 view area；下限回落容差 0；
  - `test_step0_step1_surface_details.py`：`never_covered` 按裁定 2 改写，新增长通道名「从右边遮住、控件不重叠」（Step0、Step1）；
  - `test_step0_background_correction_tab.py`：`_main_split` 改为 `_bg_c_split` / `_frame.bottom_slot`。
- **反向注入**（都变红）：
  - Step0 不加下限（frame_lock 1 条）；
  - 恢复 9 px 边距（10 条）；
  - Step0 不走框架宽度（8 条）；
  - 下限按行算（5 条）；
  - 开页值写错（1 条）；
  - 删掉开页那一段（`test_step1_layout_block_a` 1 条）；
  - 行不保持最小宽度（长名字测试 2 条）。
- **回归**（离屏 106 个模块 + 真实 GL 11 个，逐个进程运行；只把失败的模块放到 HEAD `9ae89f6` 上重跑，逐条对比失败的测试名）：
  - 修复后**没有新增失败**。`test_step0_compare_tiles::test_hot_requests_never_outrank_the_foreground` 在回归中失败了一次，单独重跑 3 次都通过，属于不稳定测试；
  - 两边相同的已有失败：`test_global_channel_dock::test_the_step0_panel_looks_like_the_baseline_panel`、`test_preview_source_provider`、`test_step0_channel_conditioning`（超时，两条不稳定）、`test_step0_no_process_button`、`test_step0_process_incremental`、`test_step1_channel_panel`、`test_tissue_navigator_viewport_sync`、`test_step1_montage_view`（abort）、GL 下的 `test_step1_gpu_takeover` 1 条；
  - GL 下 `test_step1_fusion_visibility` / `gpu_roi_clip` 三件在两边都 abort，是 GL 环境的问题（上下文上限），不是 S2 引起的；
  - `test_step3_label_render` 在 GL 下有一次退出时的 139，重跑 3 次都是 rc 0。
- **文档**：`UI_SURFACE_RULES.md`（Step1 列、列宽、页面框架三处）；两份用户指南。
