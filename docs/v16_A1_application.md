# v16 块 A1 — viewer 零漂移：申请 v3

日期：2026-09-30。分支 `v16`，调查基于 HEAD `87e38c5`（已推送）。依据：`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.2.md` §4、A0 报告 `docs/v16_A0_viewer_shift_report.md`。
状态：v2 获批并已实施（C1–C3、C6），**相机部分用户真机验收通过（2026-09-30），作为 A1a 提交**。§9 的几何补齐方案按用户裁定**不实施**，改由块 A1b（Step0–4 统一的页面框架）解决；§9.6 的裁定 1–5 原样带入 A1b。

修订记录：
- v1：初稿。
- v3（2026-09-30）：真机验收**不通过**。
  - 相机在切片坐标下已经精确一致，所以不再有缩放；但三步里 **viewer 在窗口中的框位置和大小不同**，同一个世界中心落在屏幕上不同的点，图像仍有可见位移。
  - 通道面板在三步里的宽度、位置和内部结构也不同；窄的时候 Step1 会挡住权重框，Step3 不会。
  - 用户要求：**Step0 / Step1 / Step3 的通道面板、tab、viewer 必须固定**。计划 v2.2 §4.3 允许「drawable 大小可以不同」，这一条被用户的要求取代。
  - 新增 §9。
- v2：写入用户裁定（2026-09-30）。§8 的 1–3 按建议定下。**C8 延后**：用户怀疑是 WSL 的问题，因为 Ubuntu 服务器上没有出现过。服务器修好后再看，所以 §3.5 的诊断脚本扩展移出本块。
用户裁定（2026-09-30）：C1–C3 先写申请，C6 等真机日志；另外报告了一个新现象（C8，见 §2.4）。

---

## 1. 必要性

v2.2 §4.1 要求在 Step0、Step1、Step3 之间切换时，画面**视觉上纹丝不动**；这是 TMA 的启动门 1（§9）。A0 已经用日志证实了三个原因（C1–C3），而且 C2 会累积：50 轮切换后，中心偏了 52 个第 0 层像素。

## 2. 调查结论（HEAD `87e38c5`）

### 2.1 C1：GPU 画面比 ViewBox 大一圈

- **现在的做法**：
  - `Step1GpuLayer._sync_attached_geometry` 把 GPU 层设成**整个 graphics viewport**（`ui/step1_gpu_layer.py:1433-1436`，挂载于 :585）；
  - `_gpu_viewport_snapshot` 用的世界范围是 **ViewBox** 的 `viewRange()`，输出尺寸用的却是 GPU 层的尺寸（`ui/step1_viewer_mount.py:772-795`）；
  - ViewBox 在 viewport 里四边各缩进 9 px，因为 pyqtgraph 的 `ci.layout` 有默认边距（`viewer/explore_view.py:1601` 只把外层 QWidget 布局设成了 0）。
- **结果**：同一相机下，GPU 画面比 Step0 横向大 1.4 %、纵向大 2.3 %，四角偏约 6 px（真实 GL 下用相位相关测得，与预测相差 ≤ 0.5 px）。
- Step3 的 mask 由同一个 GPU 层绘制，所以一起受影响。

### 2.2 C2：Step1 / Step3 套用相机时取整，然后重新拟合

- `Step1WholeSlideMount.apply_camera`（`ui/step1_viewer_mount.py:1076-1101`）：
  - 先把矩形 `int(round(...))` 成整数，再交给 `host.jump_to` → `ExploreController.jump_to` → `setRange(rect=…)`；
  - ViewBox 锁定了宽高比，会按窗口重新拟合一次（`viewer/explore_view.py:4633-4641`）。
- Step0 走的是 `set_view_rect_l0`（:4643-4663）：浮点数，两个轴分别设定，不重新拟合。**进入 Step0 是精确的**，A0 的日志里 Δ = 0。
- 每次进入 Step1 / Step3，中心会偏 ±0.5 px，而且方向固定；这个结果随后又成为新的共享相机，所以会累积。

### 2.3 C3：第一次进入 Step1 后，布局变化让 ViewBox 缩小并重新拟合

- `_set_step_active`（`ui/main_window.py:5180-5276`）先套用相机（:5227-5229），**之后**才：
  - 挂载通道 dock（:5234-5241）；
  - `_match_step1_bottom_bar` 给底栏定高（:5274 → :5139-5160）；
  - 把 `QTimer.singleShot(0, _hold_step1_channel_floor)` 排进队列（:5276 → `setMinimumWidth`，:5137）。
- 下一轮事件循环里 ViewBox 变小（离屏实测：高 800→780，宽 1293→1273），pyqtgraph 的 `resizeEvent` 按新尺寸重新拟合目标矩形，比例随之变化（−2.5 %，真实 GL 下 −1.7 %）；live sink（:2261-2277）把这个比例记成了新的共享相机。
- `_hold_step1_channel_floor` 放到 `singleShot(0)` 是**有意的**：代码注释写着「页面布局完成后，它量到的边框才是真的」。所以不能简单地把套用相机挪到最后；否则页面先用旧相机显示一帧，再跳过去，反而多了一次可见的跳动。

### 2.4 C8（新）：用户报告「缩放时画面卡在某个比例不动，过了这个比例才动」

- 这与 C6（高 DPI）无关。本机实测 DPR = 1.0（真实 GL 日志：816 个 viewport 采样、520 个 GPU 层采样，全部是 1.0）。
- **只读探测**（scratch 脚本，不入库）：
  - 在真实 GL 的 Step1 里，每 40 ms 做一次 `ViewBox.scaleBy(0.98)`，放大 120 步，再以 `1/0.98` 缩小 120 步，经过第 2 → 1 → 0 → 1 → 2 层；
  - **每一步 GPU 最后提交的世界范围都与 ViewBox 一致（偏差 < 1 %）**，没有复现卡住。
  - 注意：脚本里「每步提交次数」的统计写错了，计数是在 `scaleBy` 之后才开始的，所以那一栏全为 0，不能用。「GPU 世界范围 = ViewBox」这项判断不受影响。
- **还没排除**：真实鼠标滚轮的事件节奏、Step0（CPU viewer）或 Step3、某些瓦片还没到的时刻、层级滞回（`LEVEL_HYSTERESIS = 0.2`，`viewer/explore_view.py:2535`）附近的行为。需要用户在真机上复现一次，并留下日志（§3.5）。

### 2.5 不在 A1 内（按 A0 的结论）

- C4（重绑源时截断）：同一源不会重绑，切换时不会发生。
- C5（Navigator 整数跳转）：只在用户点击时发生，不属于切换。
- 已知问题「Step1 偶发全黑」「首次进 Step3 时 resizeGL 卡约 6.5 s」「Step2 完成对话框的 Step4 按钮去了 Step3」：A0 没有查出它们与位移同源，**留在 backlog**（v2.2 §4.5）。

## 3. 做法（每条都是最小改动，不建通用的相机框架）

### 3.1 C1：GPU 层只盖住 ViewBox

- `Step1GpuLayer` 的挂载几何从「整个 viewport」改为「**ViewBox 在 viewport 里的矩形**」（`view_box.sceneBoundingRect()` 映射到 viewport 坐标），并在 ViewBox 的几何变化（`sigResized`）和 viewport 尺寸变化时跟着更新（后者已有 eventFilter）。
- 这样 GPU 的输出尺寸 = ViewBox 尺寸，世界范围 = ViewBox 的 `viewRange()`，两者对得上。
- 画面效果：Step1 / Step3 四边也会出现和 Step0 一样的 9 px 边框。**缩放与位置与 Step0 完全一致。**
- 另一个做法见裁定 1。

### 3.2 C2：Step1 / Step3 用浮点、不重新拟合地套用相机

- `apply_camera` 改为：按自己 ViewBox 的尺寸解出矩形（浮点），调用 `controller.set_view_rect_l0`，与 Step0 的 `_apply_full_image_camera` 完全相同的做法。
- `set_view_rect_l0` 和 `jump_to` 一样，会立即发出两批瓦片请求（`viewer/explore_view.py:4643-4663` 的 docstring），所以加载时机不变。
- `jump_to`（patch 按钮、Navigator 用的）不改。

### 3.3 C3：Step1 / Step3 的 ViewBox 尺寸变化时，保持中心和比例

- 在 `Step1WholeSlideMount` 里接上 ViewBox 的 `sigResized`：
  - 尺寸一变，就用变化前记下的 (中心, 比例) 按新尺寸重新解出矩形，再经 `set_view_rect_l0` 放回去；
  - 这正是 v2.2 §4.3 的不变式：drawable 可以变大小，世界锚点和「每设备像素对应的世界单位」不变。
- 这样不必改 `_set_step_active` 的顺序，也不需要延迟套用相机，所以不会多出一帧跳动。
- **用户可见的附带变化**：在 Step1 / Step3 拖动窗口大小或分隔条时，画面保持原来的缩放，只改变看到的范围，不再「缩放以适应」。Step0 在这一点上不变（见裁定 2）。
- 「变化前的相机」来自 mount 已有的 `current_camera()`，在每次 `sigRangeChanged` 时记下。**不新增状态机，不新增 registry**，只是一个缓存的 (cx, cy, scale)。

### 3.4 C6（待真机；裁定 3）：`paintGL` 的 blit 用设备像素

- `paintGL` 把按物理尺寸分配的 FBO 画到 `self.width() / height()` 上（`ui/step1_gpu_layer.py:840-850`），用的是逻辑像素；按 Qt 的约定，默认帧缓冲是设备像素。
- 改法：目标尺寸乘以 `devicePixelRatioF()`，两行。
- 本机 DPR = 1，这一改动在本机**不改变任何像素**；HiDPI 的效果只能在真机 125 % / 150 % 缩放下验证。

### 3.5 C8：~~先诊断~~ **延后**（用户裁定 2026-09-30）

用户怀疑这是 WSL 特有的问题：同一软件在 Ubuntu 服务器上没有出现过。等服务器修好后，先在那边对照，再决定要不要处理。本块不做任何 C8 相关的工作，下面是当时的方案，仅作记录。

- 扩展 `scripts/diagnose_v16_a0_camera.py` 的 `realapp` 模式：额外记录每次 `sigRangeChanged` 的 ViewBox 范围和层级、每次 GPU `submit` 的世界范围，以及每次 `paintGL`。**只在脚本进程里，只记录。**
- 用户在真机上复现一次「卡住」，日志就能回答：相机在不在动？GPU 有没有提交？提交的是不是新范围？是 Step0 还是 Step1 / 3？
- 查明原因后：
  - 若与 C1–C3 同源 → 并入 A1；
  - 否则另开一个块，写申请请用户批准。

## 4. 白名单

- `ui/step1_gpu_layer.py`：挂载几何（`attach` / `_sync_attached_geometry` / eventFilter 相关的几行）；`paintGL` 的 blit 尺寸（C6，裁定 3 已同意）
- `ui/step1_viewer_mount.py`：`apply_camera`；新的 `sigResized` 处理，接在 `_connect_view_rect` 旁边；`_gpu_viewport_snapshot` 若需要，只改输出尺寸的来源
- 测试：
  - 新增 `tests/test_v16_zero_drift.py`：走真实的 `setCurrentIndex` + `_set_step_active` 顺序，不用 `_set_step_active` 单独模拟；50 × (0→1→3→1→0) 的漂移门槛；C3 的尺寸变化用例；
  - `tests/test_step1_gpu_layer.py`：GPU 层几何 = ViewBox 几何；
  - `tests/test_step3_viewer.py`：`test_one_camera_across_step1_and_step3` 增加比例的检查（今天只查中心，容差 2 %）；
  - `tests/test_step1_shared_camera.py`：只在需要时收紧容差（今天 12 px / 6 %）
- 文档：`UI_SURFACE_RULES.md`（Step1 / 3 拖动大小时保持缩放；9 px 边框与 Step0 一致）、两份用户指南（如果写到相关行为）、v2.2 §4 的执行记录

## 5. 不改的范围

- `ui/main_window.py` 的切换顺序、`_capture_camera_of` / `_apply_shared_camera_to`、live sink、`CameraSnapshot`
- Step0 的 viewer（`step0_page`、`compare_strip`、`step0_explore_tab`）
- `ExploreController` 的 `jump_to` / `set_view_rect_l0` 本身、调度、缓存、瓦片请求、预取、层级选择与滞回
- GPU 的着色器、纹理、FBO 分配、提交逻辑（C6 的 blit 尺寸除外）
- patch 跳转、Navigator、ROI 裁剪
- 不做通用的 CameraState（停止规则 1）

## 6. 风险

| 风险 | 对策 |
|---|---|
| C1 改了 GPU 层的几何，可能影响 ROI 裁剪、标签半径、鼠标穿透 | GPU 层对鼠标透明（`WA_TransparentForMouseEvents`，:588）；跑 `test_step1_gpu_roi_clip*`、`test_step1_gpu_layer`、`test_step3_label_binding`；真实 GL 下用 `gl-image` 复测 |
| C3 的 resize 处理和用户自己的缩放交织 | 只响应 ViewBox 的尺寸变化，不响应 range 变化；测试覆盖「先缩放、再改尺寸」 |
| resize 时一次重新拟合加一次恢复，会不会闪 | 两者发生在同一个事件处理里、绘制之前；真机验收时观察 |
| GL 上下文上限 | 真实 GL 测试每个进程不超过约 4 个上下文，分开跑 |
| 回归规模大（viewer 相关的模块很多） | 每个模块单独一个进程；HEAD 副本对比，只看新增失败；C: 盘目前 44 GB |

## 7. 验收门

**自动：**
- [ ] 离屏：走真实切换路径，每个转场 Δ中心 = 0、Δ比例 = 0（容差 1e-6，只容许浮点误差）；50 轮 0→1→3→1→0 没有累积漂移
- [ ] 首次进入 Step1 后，比例不变（C3）
- [ ] GPU 层几何 = ViewBox 几何，并且尺寸变化后仍然相等
- [ ] 真实 GL：`diagnose_v16_a0_camera.py gl-image` 六个组织块的平移都 ≤ 0.5 px（之前是 4–7 px）；GL 转场日志 Δ = 0
- [ ] 回归：viewer / Step1 / Step3 相关的模块，与 HEAD 相比没有新增失败
- [ ] 反向注入：把取整放回去、GPU 层改回整个 viewport、去掉 resize 处理，相应的测试会变红

**真机（用户）：**
- [ ] Overlay、Fusion、Intensity；细胞 mask、核 mask；patch 跳转、Navigator 跳转；coarse → fine；新勾一个通道；Step1 ↔ Step3 来回切：**看不到任何移动**
- [ ] 拖动窗口 / 分隔条时，Step1 / 3 保持缩放（新的行为）
- [ ] 可选：125 % / 150 % 缩放（C6）
- ~~C8~~：延后到服务器修好之后（§3.5）

## 8. 用户裁定（2026-09-30：1–3 按建议；4 延后）

1. **C1 的做法**：
   - (a) GPU 层只盖 ViewBox，四边留 9 px 边框，与 Step0 一致（**建议**）；
   - (b) GPU 层仍盖满 viewport，但世界范围按整个 viewport 反算，画面也画进边框。这样也不会被拉大，但 Step1 / 3 会比 Step0 多出一圈图像。
2. **C3 附带的行为**：Step1 / 3 拖动窗口大小时保持缩放。Step0 要不要也这样？
   - **建议不改 Step0**：它的进入已经是精确的，而且改它要动 Step0 的 viewer，超出 A0 点名的范围；
   - 你希望三步一致的话，可以另开一个小块。
3. **C6 的两行修正**：本机看不出效果。要不要现在就放进 A1（**建议放**：它是按 Qt 约定的正确写法，本机零风险），还是等真机日志确认后再做？
4. **C8**：先按 §3.5 做日志诊断。另外想请你回忆一下：卡住时是在哪一步（Step0 / 1 / 3）？是滚轮缩放还是别的操作？「卡住」是指画面不跟着缩放（而光标周围的内容照常变化），还是怎么滚都缩放不了？

---

## 9. v3 扩展：Step0 / Step1 / Step3 布局统一

### 9.1 实测（离屏，test1 副本，窗口 1600 × 1000；窗口坐标，单位 px）

| | 通道框 QGroupBox | 通道 dock | viewer（ViewBox） | viewer 中心 |
|---|---|---|---|---|
| Step0 | x=12, y=108, 365 × 823 | x=22, y=222, 345 宽 | x=386, y=143, 1193 × 779 | (982.5, 532.5) |
| Step1 | x=7, y=108, 289 × 823 | x=12, y=196, 279 宽 | x=311, y=142, 1273 × 780 | (947.5, 532.0) |
| Step3 | x=7, y=108, 269 × 843 | x=12, y=188, 259 宽 | x=291, y=145, 1293 × 797 | (937.5, 543.5) |

窗口为 2050 × 1330 时，差异的样子相同：中心水平差 20–35 px，垂直差约 11 px。

### 9.2 原因（只读调查；行号基于 `95e0ee7`）

1. **共享的是比例，不是像素宽度**。
   - `_channel_column_fraction` 被分别套用在四个 splitter 上（`ui/main_window.py:1654-1766`）。
   - Step0 的 splitter 嵌在 tab pane 里，外面还有 `sec_c` 的 4 px 边距（`ui/step0/step0_page.py:638-641`、`:864-878`）；Step1 / Step3 的 splitter 取的是整页宽度。两者的总宽度不同，分隔条的宽度也不同：Step0 的 QSS 是 3 px，Step1 / Step3 用 Fusion 默认值。
2. **每页用自己的最小值去夹住这个比例**，而且只有 Step1 有行宽下限。
   - Step1 的下限来自 `_hold_step1_channel_floor`（`:5108-5137`），每次进入 Step1 只算一次，之后就过期了。
   - Step3 没有这样的下限。
   - 这正是窄的时候 **Step1 挡住权重框、Step3 不挡**的原因。
3. **通道框的内部结构不同**：
   - Step0 的 `chl` 没有设边距，Fusion 默认约 9 px；dock 上方是 `Method ▾` / `Intensity…`、分隔线和 cuCIM 警告；没有 Reset / Load。
   - Step1 的框边距是 4 px；上方是表头、横线、`ConfigPanel`（**多一层 4 px 内边距**，`ui/step0/config_panel.py:195-250`）以及 Reset / Load。
   - Step3 的框边距是 4 px；上方是表头、横线、Reset / Load（没有 ConfigPanel 那一层，`ui/step3_page.py:92-97`）。
   - 结果是 dock 顶边分别在 222 / 196 / 188。
4. **viewer 上下左右的邻居不同**：
   - 顶边 134 / 133 / 136：工具栏行分别是 Step0 的 toolbar、Step1 的 `sel_row`、Step3 的 `mode_row`，高度与 spacing 不同，而且 Step1 / Step3 的 `pl` spacing 没有设定。
   - 底边 931 / 931 / 951：Step3 的底栏（`← Back to Step 2`）比 Step1 的底栏矮约 20 px。
   - 右边 1588 / 1593 / 1593：Step0 多了 tab pane 和 `sec_c` 的边距。
   - 左边由上面第 1、2 条决定。
5. **tab 的边框**：Step0 只有一个 QTabWidget，横跨两栏，使用 Fusion 默认样式；Step1 / Step3 的左右两栏各有一个 QTabWidget（`_STEP1_TAB_QSS`，1 px pane）。两者的边框和 pane 边距不同，这也影响了通道框和 viewer 的起点。

### 9.3 目标（写成可自动检查的几何契约）

在 Step0、Step1、Step3 之间：

- **G1 通道框**：`Channels` QGroupBox 在窗口坐标下的矩形**逐像素相同**。
- **G2 通道列表**：dock 的矩形逐像素相同（若裁定 2 选 (b)，则只要求 x 和宽度相同，见 §9.6）；每一行里各控件的横向位置和宽度相同，所以窄的时候三步表现一致，**权重框永远不会被挡住**。
- **G3 viewer**：ViewBox 在窗口坐标下的矩形逐像素相同。加上 A1 已完成的相机修复，同一个世界点就落在同一个屏幕像素上，**图像完全不动**。
- **G4 tab 栏**：左右两栏的 tab 栏顶边和高度相同（今天 Step0 / Step1 已经相同，由 `test_step0_step1_surface_details.py:281` 锁定），左栏 tab pane 的边框和内边距相同。
- 以上在**任意窗口尺寸**下、**拖动分隔条之后**、窄到最小宽度时都成立。

### 9.4 做法（最小改动：不重做页面结构，只统一几何）

1. **共享像素宽度，取代比例**。`_channel_column_fraction` 换成一个「通道框宽度」：
   - 每页按自己的 chrome 反算自己 splitter 的尺寸，使通道框的左右两边落在同一窗口 x 上；
   - 拖动任意一页的分隔条，都换算回这个共享宽度；
   - 窗口缩放时，宽度按比例跟着变（沿用今天「用比例跟随窗口」的做法），但三步始终相同。
2. **一个共同的最小宽度** = 三步里「一行放得下」的最大值（包含滚动条和边框），三页同时生效。Step1 的 `_hold_step1_channel_floor` 并入这里，不再只在进入 Step1 时算一次。
3. **通道框内部对齐**：
   - 三步的框边距统一为 4 px；Step0 的 `chl` 显式设为与 Step1 / Step3 相同；
   - Step1 去掉 `ConfigPanel` 多出的那 4 px 内边距，改到与 Step3 相同；
   - 表头行高度统一；
   - Step0 没有 Reset / Load 这一行，处理方式见裁定 2。
4. **viewer 四周对齐**：
   - 三页 viewer 上方工具栏行的高度和 spacing 统一，为显式的同一个数值；
   - Step3 的底栏与 Step1 的底栏同高（`← Back to Step 2` 所在行，**只改高度**，内容不变）；
   - Step0 的右侧边距补齐，使三页 viewer 的右边在同一 x 上。
5. **tab pane**：左栏 tab pane 的边框和内边距在三页取同一值。Step0 的结构（一个 tab 横跨两栏，Save 行在 pane 里）**保持不变**，只用边距补齐几何。
6. **不改的内容**：
   - 各步的控件种类和顺序（UI_SURFACE_RULES 里已记录的裁定）；
   - 各步行里的附加控件：Step0 的 `method_cb`，Step1 / Step3 的滑条与权重框；
   - 按钮文字、功能，Step2 的布局，viewer 的内部。

### 9.5 白名单（新增）

- `ui/main_window.py`：只改通道列的共享与应用（`_channel_column_splitters` / `_apply_channel_column_fraction` / `_on_channel_column_dragged` / `_fix_step1_split_ratio` / `_hold_step1_channel_floor`，:1654-1777、:5108-5137），以及 Step1 页的边距和 spacing（:810-1000、:1066-1070、:1365-1411）
- `ui/step0/step0_page.py`：只改布局边距、spacing 和通道列宽度的接口（:638-641、:864-1012、:1272-1278、:3491-3543）
- `ui/step0/config_panel.py`：只改外层边距（:195-200 附近）
- `ui/step3_page.py`：只改布局边距、spacing 和底栏高度（:59-152）
- 测试：
  - 新增 `tests/test_v16_layout_lock.py`：三页的 G1–G4 几何逐像素相等；多种窗口尺寸；拖动任意一页的分隔条；最小宽度时权重框完全可见；
  - 更新现有的像素或比例断言：`tests/test_step3_page.py:201`、`tests/test_step1_layout_block_a.py:113 / :231`、`tests/test_step0_step1_surface_details.py`，以及 `tests/test_ui_surface_contract.py`。只改它们锁定的数值或比例语义，行为断言不动。
- 文档：`UI_SURFACE_RULES.md`（「一个比例」改为「一个像素宽度」，并写入 G1–G4 契约）、两份用户指南（如有涉及）、v2.2 §4 的执行记录。
- 验收脚本：本次的布局测量脚本整理成 `scripts/diagnose_v16_a1_layout.py`，输出三步的矩形对照表，供验收使用。

### 9.6 请用户裁定

1. **精度**：三步的通道框和 viewer 要求**逐像素相等**（容差 0 px）。**建议同意。**
2. **Step0 没有 Reset / Load 这一行，dock 的顶边如何对齐**：
   - (a) Step0 在同一位置留出等高的空白行，dock 顶边三步完全相同。缺点是 Step0 多了一块空白；
   - (b) 只要求通道框（外框）和列表的左右宽度一致，dock 的顶边允许因为 Step1 / Step3 多一行 Reset / Load 而不同。**建议 (b)**：框和列都固定，视觉上最重要的是框和宽度不跳；
   - (c) Step0 也显示 Reset / Load，但置灰。这会给 Step0 新增界面元素，需要你单独同意。
3. **Step3 的底栏与 Step1 同高**：底栏会变高约 20 px，内容不变。**建议同意**，否则 viewer 的底边无法对齐。
4. **窄的最小宽度取三步中最大的那个**：Step0 可能因此不能拖得像以前那么窄。**建议同意**，这正是「三步一致、永远不挡住权重框」的代价。
5. **预算**：v2.2 给 A1 的预算是 1–2 天，这次扩展约需再加 1–1.5 天。日程顺延，或者从别处（第 16 天的缓冲）扣除，请你决定。
