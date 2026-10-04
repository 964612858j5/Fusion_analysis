# v16 块 A7 — Viewer 相机的唯一持有者（E4，门 8）：实施申请 v1（已批准）

日期：2026-10-02。分支 `v16`，调查基于 HEAD `bf048c7`（A6 G1–G5、W1–W3 已在本地提交，A6 全量回归进行中）。

依据：
- 合并基准 v2.3（`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.3_consolidated.md`）：§0.2 E4（:113）、§9 门 8（:952–955）、§16 A7（:1096–1135）、§0.4 块规则（规则 1、3、5、6、7）、停止规则 8（:1041）。
- v2.4（`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.4.md`）：v2.4 没有改写 A7、E4、门 8 和停止规则 8（v2.4:5）。只有三处与 A7 相关：
  - §9 门 10（v2.4:64）把 waiver 条款的门编号改为「gates 1–7, 9 and 10 with the explicit E4 waiver for gate 8」，**本申请按这个写法引用**（handoff_next2 D2）；
  - §12 的 A9 行（v2.4:89）：A9 在 A7 之后，A9 不能写 A7 的相机持有者；
  - §20.3 A9-8（v2.4:149）：A9 要求「A1 zero-drift and A7 zero-write-back invariants still pass」，所以 A7 要留下一条可以反复使用的自动断言。
- `AGENTS.md` 第 5 条：Viewer 的改动需要单独的 P0 批准。本申请就是这份批准的请求。

状态：**申请 v1，用户 2026-10-02 批准；2026-10-04 开工前复核（§13）与第 15–17 题裁定已批准**（「确认」，第 12–14 题按推荐项）。还没有改代码。**开工前提**：用户先完成 W1–W3 的 Step0 真机验收（§9）。

修订记录：v0 草稿；v1 写入用户对 v0 的审阅：第 1、2、6、7、8 题加条件；第 9 题改写；新增第 12–14 题；§1 写明 E4 的核心语义。

---

## 1. 必要性与目标

**E4「Viewer state has a single owner」**（合并版 :113）：
- 相机、以及「切页后必须保留的显示状态」，在 viewer 一侧各有**唯一的**权威持有者；各页面只消费、只渲染。
- renderer 可以读取并应用权威相机，但布局、resize、换源、coarse / fine / mask 到达等事件**一律不能写回**它。
- 切页不再走「capture → apply → read-back」。
- **核心语义（用户 2026-10-02）**：持有者保存的是**用户意图**，而不是屏幕结果。各页按自己的 clamp 渲染，所以**切页后不同页面看到的范围，允许因 clamp 而略有不同**。这不算漂移；漂移是指持有者里保存的相机被非用户事件改变了。真机验收按这个口径判断。

例子（今天会发生的情形）：用户在 Step1 放大到某个细胞，切到 Step3。进入 Step3 时，MainWindow 先把共享相机应用到 Step3，然后**再从 Step3 读回来**，因为 viewer 会做 clamp。Step3 的 viewer 刚好在 resize 或 fit，读回来的就是 viewer 自己算出的范围，而不是用户放大的位置，并且被写进了共享相机。A1 用「缓存的 (camera, size) 对」挡住了已知的几种情况，但这条写回的路径还在。

## 2. 只读调查结果（2026-10-02）

### 2.1 现在的相机路径

| 位置 | 内容 |
|---|---|
| `ui/shared_camera.py` | `CameraSnapshot(dataset, cx, cy, scale, origin)`；`valid_for(dataset)` 拒绝其他切片的快照 |
| `ui/main_window.py:520` | `self._shared_camera`：事实上的共享相机，由 MainWindow 持有 |
| `ui/main_window.py:2270–2295` | `_on_step0/1/3_camera` → `_remember_camera`：只有在屏的那一页能写 |
| `ui/main_window.py:2299 / 2316` | `_capture_camera_of`（离开时读取）、`_apply_shared_camera_to`（进入时应用，**然后读回**）——计划要去掉的路径 |
| `ui/main_window.py:5205–5251` | `_set_step_active` 调用上面两者 |
| `ui/step0/step0_page.py:2426–2460, 5898–5907, 3001` | Step0 的 `current_camera_snapshot` / `apply_camera_snapshot` / `publish_camera`；`sigRangeChanged` 的**任何**范围变化都会 publish |
| `ui/step1_viewer_mount.py:1053–1113, 1194–1251` | Step1 / Step3 共用的 mount：`apply_camera`、`publish_camera`；A1 C3 的缓存对 `_kept_camera`、`_on_view_resized` |

**关键缺口**：Step0 的 `sigRangeChanged` 和 Step1 的 `_on_range_changed` 都把所有范围变化当作相机变化，**分不清是用户拖动，还是布局重拟合**。今天靠 `_current_step` 和 `_kept_camera` 来区分，这是在猜，而不是由「谁写」来决定。

### 2.2 显示状态

`ui/block01_display.py:104` 的 `ChannelDisplayState` 已经是每个通道的颜色、mapping、选中通道和可见性的**唯一持有者**，所有步骤都读写它（A1b / L 系列）。所以显示状态这一半已经满足 E4。见第 3 题。

### 2.3 现有测试

- 守相机的：`test_v16_zero_drift`（A1：逐位精确、50 次往返）、`test_step1_shared_camera`（隐藏页不写、另一张切片不继承等）、`test_step0_compare_toggle_drift`、`test_step1_camera_stability`、`test_overview_camera_ownership`。
- 守布局的：`test_v16_frame_lock`（A1b F1–F4）。
- **门 8 要求的「零回写」自动断言目前不存在**。`scripts/diagnose_v16_a0_camera.py` 只是 JSONL 诊断脚本，不是测试。

## 3. 做法

| 行 | 做法 |
|---|---|
| C1 持有者 | 新模块 `ui/camera_owner.py`（规则 6：新逻辑放在两个大文件之外）：一个小对象，持有当前权威相机（复用 `CameraSnapshot`），**只有三个写入口**：`user_navigated(step, snapshot)`、`jump(step, snapshot)` 和切换数据集时的 `reset_for_dataset(dataset)`。**没有通用的 `set`**。读取口是 `current(dataset)`。三个写入口各打一行普通 logging（不算新机制）。MainWindow 用它替换 `_shared_camera` |
| C2 renderer 只读 | 进入页时，持有者把相机交给 renderer 应用（`apply_camera_snapshot` / `apply_camera`），**不再读回**。删除 `_capture_camera_of` / `_apply_shared_camera_to` 的读回路径；C3 缓存对在持有者覆盖之后删除（合并版 §16.2） |
| C3 区分「谁写」 | 鼠标拖动和滚轮用 pyqtgraph ViewBox 的 **`sigRangeChangedManually`**：它只在鼠标拖动 / 滚轮时发出，布局变化和程序调用 `setRange` 都不触发，正好是「谁写」的天然分界。连上它，调用 `user_navigated`；`sigRangeChanged` 不再 publish。显式命令（Navigator 跳转、patch / preview 跳转、Fit / Reset、Load 后的初始 fit）各自调用 `jump`。resize、layout、tile 到达都不写。完整入口清单见 §3.1 |
| C4 零回写断言 | 做成**可以复用的 pytest fixture**（例如 `camera_writes`，放在 `tests/conftest` 一级可导入的辅助模块里）：用 monkeypatch 包装持有者的三个写入口，记录每次写入的来源。在下列事件期间，**写入次数必须为 0**：Step0↔1↔3 切换、resize、同源 source refresh、coarse / fine / mask 到达、channel enable、**Step0 compare 面板的进入 / 离开**。A9-8 和每日检查直接调用这个 fixture，不内联在某一条测试里 |

### 3.1 用户动作的完整清单（第 14 题）

每一项都要显式接线，并各配一条「恰好写一次」的测试。漏掉一项，就是「用户拖了，相机却没记住」。

| # | 入口 | 写入口 | 所在页 |
|---|---|---|---|
| U1 | 鼠标拖动 | `user_navigated`（`sigRangeChangedManually`） | Step0 Full Image、Step1、Step3 |
| U2 | 滚轮缩放 | `user_navigated`（`sigRangeChangedManually`） | 同上 |
| U3 | 键盘平移 / 缩放（**实施前先核实是否存在**；不存在就在执行记录里写明） | `user_navigated` | — |
| U4 | Navigator 跳转 | `jump` | Step0、Step1 |
| U5 | patch 跳转（patch 按钮 / `Patch ▾` 菜单） | `jump` | Step0、Step1 |
| U6 | preview 跳转 | `jump` | Step1 |
| U7 | Fit / Reset 按钮 | `jump` | 各页 |
| U8 | compare 面板的进入（右键打开对比） | 不写持有者（走 Step0 现有的进入 / 离开规则，第 8 题）；列在这里，是为了让测试证明它**不写** | Step0 |
| U9 | Load 新切片后的初始 fit（第 12 题） | `jump` 一次 | Step0 |
| U10 | 切换数据集 | `reset_for_dataset` | MainWindow |

实施第一天先用 grep 核对这张表（例如 `setRange` / `autoRange` / `set_view_rect_l0` 的调用点），发现表外的入口就补进来，并在执行记录里写明。

**不做的**：不碰 active source（A8 才搬进 ProjectState，A7 不建临时的 source 持有者）；不拆 `main_window.py` / `step0_page.py`；不做通用的 CameraState 框架；不改显示状态的持有者（如果第 3 题选 (a)）。

## 4. 封闭白名单

- 新增 `ui/camera_owner.py`。
- `ui/main_window.py`：只改 `_shared_camera` 的创建、`_on_step0/1/3_camera`、`_remember_camera`、`_capture_camera_of`、`_apply_shared_camera_to`、`_set_step_active` 里与相机有关的几行，以及 `camera_sink` 的接线。
- `ui/step0/step0_page.py`：只改 `publish_camera` 的调用点（区分用户动作和布局），以及 `apply_camera_snapshot`。
- `ui/step1_viewer_mount.py`：只改 `publish_camera` 的调用点、`apply_camera`、C3 缓存对的移除。
- `ui/shared_camera.py`：只在需要时给快照加来源字段。
- 测试：新增 `tests/test_v16_a7_camera_owner.py` 和可复用的零回写 fixture 模块（`tests/camera_write_audit.py`）；已有相机测试只改读取点（第 6 题），逐条记入执行记录。
- 文档：本申请的执行记录、v2.4 §12 进度、`UI_SURFACE_RULES.md`（如果用户可见的行为有变化）。

## 5. 不改的范围

active source / source binding；显示状态（`ChannelDisplayState`）；viewer 的读取、调度、缓存、线程、I/O 和 tile 逻辑（规则 5）；Tissue Preview / Navigator popup 内部的相机（第 8 题）；`prev_vb` 预览框（第 8 题）。

## 6. 预注册验收门（门 8）

- **自动**：
  - C4 零回写断言：上列事件期间，持有者的写入次数为 0；
  - 用户动作（拖动、缩放、跳转、Navigator 跳转）各自恰好写一次；
  - 反向注入：零回写断言在 A7 之前的代码上**必须变红**。旧代码里没有持有者，所以在旧代码上由 fixture 包装 `MainWindow._remember_camera`，并拦截对 `_shared_camera` 的赋值来计数（第 13 题）。今天的读回路径会在切页时写，所以必然变红；
  - `test_v16_zero_drift`、`test_v16_frame_lock`（F1–F4）、`test_step1_shared_camera`、`test_step0_compare_toggle_drift`、`test_step1_camera_stability`、`test_overview_camera_ownership` 的**断言语义**原样通过。读取点可以改（第 6 题），但每改一处都要在执行记录里写「旧读取点 → 新读取点」；**zero-drift 的逐位断言本身一个字不改**；
  - §3.1 的每个入口各一条「恰好写一次」测试；
  - 全量单调绿色回归（每个里程碑一次，用户 2026-10-02 裁定）。
- **真机**：50 × Step0↔1↔3，其中包括 resize、Navigator 跳转、coarse→fine、channel enable，肉眼看不到漂移。
- **门 8 只在 A7 通过时通过**。门 8 只测相机和显示状态，不测 active source（那由 A8 的 §17.3 验证）。

## 7. 回退（合并版 §16.4 + 停止规则 8；门编号按 v2.4:64）

- **触发**：A7 周末真机 50 次切换仍然漂移，或者零回写断言失败而且块内修不好。
- **判定**：执行窗口停下，提交证据（失败的测试、真机记录），**由用户判定**。
- **动作**：A7 revert 回 A1 + A1b 的状态，计划继续做 A8。**E4 保持 NOT PASS，门 8 不会自动通过**。用户二选一：
  - **A**：给 E4 waiver。按 v2.4:64 的写法，TMA 启动条件是「gates 1–7, 9 and 10 with the explicit E4 waiver for gate 8」。E4 记为「waived, not passed」，债务进 v17 backlog（§11），豁免和理由写进本计划的执行记录和项目 provenance 记录；
  - **B**：不给 waiver。门 8 保持打开，架构门不关闭。
- 回退本身是一个安全机制，不是 E4 的完成。

## 8. 风险

| 风险 | 对策 |
|---|---|
| 分不清用户动作和布局，导致漏写（用户拖动后相机没被记住） | 每种用户动作各配一条「恰好写一次」的测试；真机 50 次 |
| 删除读回之后，viewer 的 clamp 让各页看到的范围略有不同 | 持有者保存的是用户意图；各页按自己的 clamp 渲染，但不把 clamp 的结果写回。zero-drift 测试守住往返不漂移 |
| 改到 viewer 的事件路径（规则 5） | 只改「何时 publish」，不改读取、调度、缓存；本申请即请求批准 |
| 这是计划里风险最高的一周 | 每天跑 zero-drift 和零回写断言（第 10 题） |

## 9. 估计与顺序

- 计划估计约 4–5 天，不变。
- A7 必须在 A6 之后（它依赖 A6 对过期结果的拒绝）。
- **开工前提**（第 9 题，用户裁定）：用户先做 W1–W3 的 Step0 真机验收（交接 §7 第 1 条，约半小时），通过或者 revert 之后，A7 才能动 `step0_page.py`。原因：W1–W3 改的也是 `step0_page.py`，如果 W1–W3 需要 revert，而 A7 的提交叠在上面，revert 就会变脏。不用等 A6 的全部真机验收，只等这一条。

## 10. 与计划的关系

- A8 把 `active_source_id` 搬进 ProjectState；相机和显示状态仍留在 A7 的 viewer 侧持有者里（合并版 :1147）。
- A9 不能写 A7 的持有者，并且复用 C4 的零回写断言（v2.4:89、:149）。

## 11. 用户裁定（2026-10-02，对 v0 的审阅）

| # | 裁定 |
|---|---|
| 1 | (a) 新对象；**明确列出第三个写入口 `reset_for_dataset`**，不能有通用 `set` |
| 2 | (a) 在事件入口区分；拖动 / 滚轮用 `sigRangeChangedManually`，显式命令各自调用 `jump` |
| 3 | (a) 只处理相机 |
| 4 | 同意 |
| 5 | 同意 |
| 6 | (a)，条件：执行记录逐条写「旧读取点 → 新读取点」；zero-drift 的逐位断言一个字不改 |
| 7 | 用 monkeypatch；写入口加普通 logging 不算新机制；零回写断言做成可复用的 pytest fixture |
| 8 | 三个边界都不纳入；但零回写断言的事件里**包含 compare 面板的进入 / 离开** |
| 9 | **改写**：W4 不并入任何修正，单独成块，A7 不依赖它；A7 开工前，用户先做 W1–W3 的 Step0 真机验收，通过或 revert 之后才动 `step0_page.py`（§9） |
| 10 | 同意 |
| 11 | 同意 |
| 12 | 见 §12 |
| 13 | 见 §12 |
| 14 | 见 §12 |

## 12. 新增的问题（用户 2026-10-02 补充；已按推荐项批准）

12. **首次打开时持有者是空的，谁写第一笔**：
    - (a) Load 算显式命令：初始 fit 完成后 `jump` 一次（§3.1 U9）；
    - (b) 持有者为空时各页自行 fit，不写。
    - **建议 (a)**：Load 是用户发起的。否则切到 Step1 时持有者给不出相机，Step1 只能自己 fit，两页范围不一致，直到用户第一次拖动。
13. **反向注入怎么在旧代码上跑**：旧代码没有持有者，由 fixture 包装 `MainWindow._remember_camera` 并拦截 `_shared_camera` 的赋值来计数（§6）。**建议同意**。
14. **用户动作的完整清单**：见 §3.1（U1–U10），每项配一条「恰好写一次」测试；实施第一天先 grep 核对。**建议同意**。

---

## （v0 的原题，留作记录）

1. **持有者的形式**：
   - (a) 新的小对象 `ui/camera_owner.py`，只有「用户导航」「显式跳转」两个写入口，renderer 只读；
   - (b) 保留 MainWindow 上的 `_shared_camera`，只去掉读回路径和 `camera_sink` 的回写。
   - **建议 (a)**：写入口少，零回写断言容易挂上去，A9 也能直接复用。
2. **在哪一层区分「用户动作」和「布局变化」**：
   - (a) 在各页面的事件入口：拖动、滚轮、跳转各自显式调用写入口，`sigRangeChanged` / `_on_range_changed` 不再 publish；
   - (b) 在 viewer controller 层统一拦截。
   - **建议 (a)**：改动最小，不碰 controller（规则 5）。
3. **显示状态**：
   - (a) A7 只处理相机；显示状态已经由 `ChannelDisplayState` 唯一持有，只在执行记录里登记合规，不改；
   - (b) A7 还要把各页私有的显示读数（例如 Step0 compare 的入口相机读数）收进持有者。
   - **建议 (a)**。
4. **回退走 waiver 时 A9-8 的口径**：A7 的零回写不变量不存在了，A9-8 改为「只要 A1 zero-drift 和现有相机测试通过」。**建议同意**。
5. **waiver 条款的门编号**：按 v2.4:64 写「gates 1–7, 9 and 10」，并注明合并版的旧写法「1–7 and 9」已被 v2.4 更新。**建议同意**。
6. **「已有测试原样通过」的含义**：
   - (a) 断言语义不变，读取点可以改（例如 `rig.w._shared_camera` 改读持有者）；
   - (b) 测试文件一个字都不许改。
   - **建议 (a)**：计划同时要求删除 `_capture_camera_of` 等，(b) 做不到。
7. **零回写断言的审计钩子**：允许持有者带一个只记录「谁、何时写」的计数器（不影响行为，测试读它）？还是测试只用 monkeypatch 包装两个写入口？**建议后者**：不新增产品机制。
8. **范围边界**：Step0 compare 三联面板、Tissue Navigator popup、`prev_vb` 预览框，是否纳入持有者？**建议都不纳入**：计划只点名 Step0 / Step1 / Step3 三个 renderer。compare 面板走 Step0 现有的进入 / 离开规则，并在执行记录里登记。
9. **启动条件**：
   - (a) A6 的 G1–G5 + W1–W3 完成、REV 后修正完成、全量回归干净，就启动 A7；W4 并入这次修正，W5 另写申请，以后做；
   - (b) 等 A6 全部真机验收之后再启动。
   - **建议 (a)**：A7 只依赖 A6 的过期结果拒绝（G 系列），不依赖工作区的部分。
10. **「每天跑相机日志」的具体内容**：每天跑 `test_v16_zero_drift` 加 C4 零回写断言，**不跑**诊断脚本。**建议同意**。
11. **回退时 waiver 记在哪里**：执行记录，加上项目 provenance 目录里的一条记录（kind 和字段在执行时提交给用户审）。**建议同意**。

---

## 13. 开工前复核（2026-10-04，只读，HEAD `74a3505` + 未提交的 CG）

申请写于 `bf048c7`；其后 RM、CS、CG 共 45 个提交改动了 `main_window.py`、`step0_page.py`、`viewer/explore_view.py`。按现码复核如下（未改代码）。

### 13.1 结构未变，行号更新

| 申请里的位置 | 现在 |
|---|---|
| `main_window.py:520` `_shared_camera` | `:522` |
| `main_window.py:2270–2316` `_on_step0/1/3_camera`、`_remember_camera`、`_capture_camera_of`、`_apply_shared_camera_to` | `:2324–2397`，内容未变（仍「应用后读回」） |
| `main_window.py:5205–5251` `_set_step_active` 调用 | `:5274`（离开时 capture）、`:5318`（进入时 apply + 读回） |
| `step0_page.py:2426–2460` snapshot / apply / publish | `:2426–2460`（未变） |
| `step0_page.py:5898–5907` 全图 `sigRangeChanged` → publish | `:5745` 连接、`:5751–5754` `_on_full_image_camera_moved` → `publish_camera("step0-full")` |
| `step0_page.py:3001` compare publish | `:3001`（未变） |
| `step1_viewer_mount.py:1053–1113, 1194–1251` | `:1053–1113`、`:1181–1251`（`_kept_camera` 仍在） |

六个既有相机测试都在：zero_drift 4、frame_lock 12、step1_shared_camera 14、compare_toggle_drift 27、camera_stability 6、overview_camera_ownership 15 项。

### 13.2 C3 的前提成立

pyqtgraph 0.14.0：`ViewBox.wheelEvent`、`mouseDragEvent` 发 `sigRangeChangedManually`；`setRange`、`translateBy`、`scaleBy` 不发。Step0 全图、Step1、Step3、compare 面板都是 `ExploreView`，其 `RightClickViewBox` 只接管右键，拖动 / 滚轮走基类。

### 13.3 §3.1 清单核对结果

| # | 现码 |
|---|---|
| U1/U2 | 三页均为 `ExploreView` 的 ViewBox，可接 `sigRangeChangedManually` |
| U3 键盘 | **不存在**：唯一的键是 compare 中的 Esc（`step0_page.py:2278–2290`，离开 compare，不移动相机）。记为不存在 |
| U4 Navigator | Step0 全图 `_on_tissue_navigate`（`:5733` `controller.jump_to`）、Step0 compare `_navigate_compare_to`（`:5661`）；Step1 `main_window.py:2494` `jump_to_point` |
| U5 patch | Step0 全图 `_navigate_full_image_to_patch`（`:10199`）、Step0 compare `_navigate_compare_to_patch`（`:10281`）；Step3 `main_window.py:6422`（`_select_step3_patch`）、Step1 `:6553` `show_patch` |
| U6 preview | Step1 `jump_to_point`（mount `:1261`） |
| U7 Fit | **只有 Step0** 的 `_btn_full_fit` → `_fit_full_image`（`:1762`、`:3151`）；Step1 / Step3 没有 Fit 按钮 |
| U8 compare 进入 | 见 13.4 第 15 题 |
| U9 Load 初始 fit | 建栈时 `step0_explore_tab.py:319/322`（`jump_to` / `setRange`）；`:717` 是换源时的视口恢复，**不是** Load 的初始 fit，不写 |
| U10 | MainWindow 切换数据集 |
| 表外（程序调用，均不写） | `compare_strip.py:293/297`（建栈初始视口）、`:765`（三面板联动 `set_view_rect_l0`）；`step1_viewer_binding.py:142`（换源后恢复视口）；`step1_viewer_host.py:418`（初始化范围）。后两个文件不在白名单，也不需要改：去掉 `sigRangeChanged` 上的 publish 后它们自然不写 |

### 13.4 新发现，需要裁定

15. **compare 面板里的用户拖动 / 缩放**：现在 `step0_page.py:3001` 在 compare 的任何范围变化时都 publish，且 compare 打开时 `current_camera_snapshot` 返回 compare 的相机——即用户在 compare 里平移后切到 Step1，Step1 会到 compare 的位置。第 8 题只裁定了 compare 的**进入 / 离开**不写。
    - (a) compare 面板上的拖动 / 滚轮（`sigRangeChangedManually`）= `user_navigated`；进入、离开、三面板联动不写。
    - (b) compare 里什么都不写，持有者停在进入 compare 前的位置。
    - **建议 (a)**：保留今天「在 compare 里看到哪里，切页就到哪里」的行为，只去掉非用户的写入。
16. **Step1 / Step3 的 `_on_gesture_quiet`**（`step1_viewer_mount.py:1269–1271`）：controller 的 `gesture_quiet` 在手势结束后发，但**程序跳转之后也会发**（`explore_view.py:4742` 注释），它现在调用 `publish_camera`——这是申请 §2.1 没列出的一条非用户写入路径。
    - 做法：`_on_gesture_quiet` 不再 publish（它的重组、`publish_view_rect` 保留）；用户写入只来自 `sigRangeChangedManually`，跳转由各自的 `jump` 写。属于白名单里「`publish_camera` 的调用点」，不扩白名单。

### 13.4b 测试口径修订（codex）

- `sigRangeChangedManually` 在一次拖动中**每次移动都会发**，不是每个手势一次。§3.1 / §6 的「恰好写一次」改为：**每个手动通知恰好写一次**，且同一移动没有来自 `sigRangeChanged` / `gesture_quiet` 的重复写入；跳转类（U4–U7、U9）仍是每次命令恰好一次。
- U2 包括**右键拖动缩放**（`RightClickViewBox` 只接管右键单击，不接管右键拖动）。
- compare 的 Navigator / patch 跳转（`:5661`、`:10281`）各自显式 `jump`：去掉 compare 的通用 publish 后不能丢掉这两条用户命令。第 15 题选 (a) 时，从 `step0_page.py` 连接三个 compare ViewBox 的 `sigRangeChangedManually`，不改 strip / controller 内部。

### 13.5 与 CS / CG 的关系

CS P2 在 `viewer/explore_view.py` 加的 `begin_suspend_for_production` 只锁相机、不写相机；A7 不改 `explore_view.py`（§5），无冲突。CG 未动相机相关文件。

### 13.6 复核审核

codex（astra low，2026-10-04）：主要行号、C3 前提、第 16 题正确；补充了 U5 的 Step3、compare 的两条跳转、两处程序视口路径、测试口径与右键拖动（均已并入 13.3 / 13.4b）；第 15 题 (a) 可行，在白名单内；RM / CS / CG 不影响 A7 的做法；U3 无可用的键盘导航。

### 13.7 用户裁定（2026-10-04）

- **第 15 题：(a)**。保留「在哪看就切到哪」：在 compare 里拖动 / 缩放到哪里，进入 Step1 也必须在那里。compare 面板的拖动 / 滚轮 = `user_navigated`；进入、离开、三面板联动不写。
- **第 16 题：同意**。`_on_gesture_quiet` 不再写持有者，其余工作保留。用户补充：程序的位置记录与用户的位置记录解耦——这正是 E4 的做法：持有者只存用户意图，各 viewer 自己显示的（clamp 后的）范围是程序的，永不写回持有者。
- **第 17 题：同意**（修订 2026-10-02 第 8 题）。compare 的「进入」是用户右键点 P，属于用户命令：进入时 `jump` 写一次（P）；在 compare 里拖动 / 滚轮只记用户操作的那个面板（`user_navigated`）；另外两个面板的联动不写；离开不写（持有者已是最后看的位置，离开时全图回到面板相机，现有规则）。§3.1 的 U8 与 C4 的零回写事件随之改为：进入 = 恰好一次 `jump`，离开 / 联动 = 0 次。

---

## 14. 执行记录（2026-10-04/05，未提交）

### 14.1 改动

- 新增 `ui/camera_owner.py`：`CameraOwner`，读 `current(dataset)` / `dataset`，写只有 `user_navigated`、`jump`、`reset_for_dataset`，每次写入一行 debug 日志；无通用 `set`。
- `ui/main_window.py`：`_shared_camera` → `_camera_owner`；用户导航 `camera_sink`（`_on_stepN_camera`）与显式跳转 `camera_jump_sink`（新 `_on_stepN_jump`）两路，只有在屏的那一步能写；删除 `_capture_camera_of`、`_remember_camera`；切页不再读取离开的那一页；`_apply_owner_camera_to` 只应用、不读回；`_camera_dataset(step)` 按该页显示的切片标记（Step0 用 Step0 自己的切片——第一次 Save 前窗口还没有 loader，codex）；Step0 数据集提交时 `reset_for_dataset`（U10），进入某页时持有者属于另一切片也重置。
- `ui/step1_viewer_mount.py`：`sigRangeChanged` 只做视口框与 `_kept_camera` 维护，不再 publish；`sigRangeChangedManually` → 用户导航；`show_patch` / `jump_to_point` → 跳转；`_on_gesture_quiet` 不再 publish（第 16 题）。
- `ui/step0/step0_page.py`：全图只在 `sigRangeChangedManually` 时 publish；compare 面板的 `sigRangeChangedManually` → 用户导航（第 15 题），`_on_compare_camera_changed` 不再 publish；进入 compare 一次跳转（第 17 题）；跳转：Navigator（全图 / compare）、patch（全图 / compare）、Fit；U9：数据集提交后第一次显示全图时一次跳转，写入成功才消费。
- **偏离申请（需用户知悉）**：§3 C2 写「C3 缓存对在持有者覆盖之后删除」。实施中**保留**了 `_kept_camera`：它是 Step1/Step3 viewer 自己在 resize 时保持显示范围的本地记录，从不写持有者，正是第 16 题用户所说的「程序的位置记录」。删除它会让 resize 后的显示跳回旧范围，且需改动 `test_a_zoom_after_a_resize_is_the_users` 的语义。codex 认为保留合理。
- U3 键盘：不存在（见 13.3）。U7 Fit：只有 Step0。

### 14.2 既有测试的改动（第 6 题：只改准备步骤与读取点，断言不改）

| 文件 | 旧 → 新 |
|---|---|
| `test_v16_zero_drift.py` `_start` | 程序设置起始相机后，加 `as_user(vb)`（发出用户拖动时 pyqtgraph 发的 `sigRangeChangedManually`）——起始位置是用户的 |
| `test_v16_zero_drift.py::test_fifty_round_trips_do_not_drift` | `rig.w._shared_camera.camera` → `rig.w._camera_owner.current(rig.w._camera_dataset()).camera`；逐位断言一字未改 |
| `test_step1_shared_camera.py` `_window` | 加 `_connect_full_image_view_rect(tab.stack)`：产品里 `_show_full_image` 做这一步，测试搭的假 Step0 没走到 |
| `test_step1_shared_camera.py` A1、A2、A3、A4、A5、A6、B1 | 程序移动后加 `as_user(...)`（全图 / Step1 / compare 面板） |
| `test_step1_shared_camera.py` A6 | `_shared_camera is not None` → `_camera_owner.current(...) is not None`；`_shared_camera.valid_for(other) is False` → `_camera_owner.current(other) is None`；`_shared_camera.dataset == other` → `_camera_owner.dataset == other` |

六个相机模块 + `test_viewer_pause` 共 122 项通过。

### 14.3 新增测试

- `tests/camera_write_audit.py`：可复用 fixture `camera_writes`（monkeypatch 包装三个写入口；A7 之前的代码上改为包装 `MainWindow._remember_camera`，第 13 题）与 `as_user(view_box)`。
- `tests/test_v16_a7_camera_owner.py`（17 项）：切页 / resize / 同源刷新 / 打开通道 0 次写入；compare 进入恰好 1 次跳转，联动与离开 0 次；Step0/1/3 拖动与滚轮每次通知恰好 1 次；compare 面板拖动后 Step1 跟到那里；8 个显式命令各恰好 1 次跳转；载入后第一次全图 1 次跳转、之后的重建 0 次；另一切片重置；第一次 Save 之前 Step0 的移动不丢；数据集提交重置；没有通用 setter。
- **反向注入**：在 A7 之前的代码（`a7956b3`）上，零回写测试失败——切页等事件中写入 24 次（离开读取 + 进入读回），compare 中 4 次。

### 14.4 审核

- codex 代码审核第 1 轮：1 条（第一次 Save 前窗口无 loader，Step0 的写入被拒、载入跳转被白白消费、切片标记可能沿用旧切片）——已修，补 2 项测试；保留 `_kept_camera` 合理，需记录（见 14.1）。

### 14.5 回归

- 聚焦回归 102 模块（涉及相机、切页、compare、视图范围信号、MainWindow 的全部测试模块）：失败均在旧代码上存在（`test_global_channel_dock`、`test_step0_floor_prefetch` 2 项、`test_step1_channel_panel`、`test_step3_patch_strip`、`test_step1_montage_view` 崩溃）。`test_step0_compare_tiles` 这一轮有 1 项（`test_the_dapi_mapping_is_its_own_channels`）失败，单独重跑 3 次均通过，整模块重跑 190 项全部通过——该模块本身有时序不稳定（以往轮次失败的是另一项）。
- 待办：用户真机验收（§6：50 次 Step0↔1↔3，含 resize、Navigator 跳转、粗→细、打开通道；compare 中移动后进入 Step1 位置一致）。
