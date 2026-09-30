# v16 块 A1 — viewer 零漂移：申请 v2

日期：2026-09-30。分支 `v16`，调查基于 HEAD `87e38c5`（已推送）。依据：`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.2.md` §4、A0 报告 `docs/v16_A0_viewer_shift_report.md`。
状态：**申请 v2，用户 2026-09-30 批准**（授权提交后执行）。

修订记录：
- v1：初稿。
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
