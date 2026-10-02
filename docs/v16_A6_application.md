# v16 块 A6 — 异步生命周期 + Step0 项目工作区（打开 / Save / Save as）：实施申请 v1

日期：2026-10-01（夜间起草，供用户 2026-10-02 上午审阅）。分支 `v16`，调查基于 HEAD `935d809`。

依据：
- 合并基准 §15（A6：异步生命周期，E5，门 9）、§0.2 E5、§9 门 9、§0.4 块规则（规则 6：A6 可以在现有回调里加代数检查）；
- 用户 2026-10-01 的裁定（记忆 `a6-scope-step0-session-save`）：Step0 打开已有项目、Save 覆盖当前工作区、Save 旁边加 Save as 下拉；正在运行的 Step2 跑完后标记「不是最新参数」；Step2 运行期间做的 fusion 先放进本会话的临时目录；Step4 计算期间冻结页面；fusion 只确认「取消后等线程真正退出才解锁」；viewer / Step3 的晚到结果按过期丢弃处理。**用户授权为这些新需求扩大白名单**（包括 UI 改动，并同步更新三份 UI 文档）。

状态：**申请 v1，用户 2026-10-02 批准**（「批准申请、可以提交」；§10 第 1–7 题按 §11 裁定）。**实施中**：G1–G5、W1–W3 已在本地提交，W4、W5 未开始，见 §12。

修订记录：v0 夜间草稿；v1 写入用户 2026-10-02 上午对第 1–7 题的裁定。

---

## 1. 必要性与目标

**E5「异步结果不会串」**：为数据集 / 页面 / 运行 A 启动的任务，在用户换到 B 之后才完成时，不能改动 B。
- 写 UI 或共享状态的回调：带上启动时的身份，到达时核对，过期就丢弃；
- 迟到的旧任务可能覆盖或污染的磁盘写入者：目标按运行隔离，并有明确的提交协议。

**用户新增的工作区规则**（同属「谁在什么时候写哪个工作区」的问题，所以并进本块）：

例子：用户在 test1 上用工作区 `full_wsi_…_6bad` 做到了 Step2，第二天重新打开程序。
- **今天**：Load 之后再按 Save，一定会新建工作区 `full_wsi_…_<新的>`。test1 副本里已经积累了 34 个同名的 "Full WSI"。
- **A6 之后**：Load 时认出这个项目里已经有这张切片的工作区，继续用它；Save 覆盖它；只有在下拉里选 **Save as** 时才新建工作区。

## 2. 只读调查结果

完整清单在调查笔记里（夜间由子代理整理）；下面关键的几条行号都已抽查核实。

### 2.1 已经合规的（只登记，不改；计划 §15.2 第 3 条）

| 任务 | 核对机制 |
|---|---|
| Step0 概览图加载 | `_result_is_mine`：代数 + loader + token（`ui/step0/overview_panel.py:1920`） |
| Step0 预读调度 | `dataset_gen` 比较（`ui/step0/step0_page.py:4096`） |
| Step0 预览 / patch 批处理 | `_gen_slot` 绑定代数（`step0_page.py:10764-10784`），切换数据集前等它们退出 |
| Step0 整片校正 | 运行期间拒绝切换数据集（`step0_page.py:6156-6163`），回调经 `_gen_slot`。磁盘上是原地写入，见 §2.3 |
| Step0 几何持久化 / 交接写入 | 代数 + 权威下限 + `os.replace`，在发布锁之内 |
| Block01 显示种子 / 低分辨率读取 | binding 代数 |
| Step1 patch 加载 / 帧合成 / 合成协调器 / viewer 调度 | 线程对象身份或代数 |
| Step1 预分割的磁盘写入 | 按运行目录隔离，先写掩膜、最后写记录 |
| Step3 标签绑定与金字塔 | 代数；金字塔写到 `.partial`，`complete` 最后写 |
| Step2 概览图 | 同步执行（`_ov_thread` 是死代码），不涉及异步 |

### 2.2 缺口（要改的行）

| # | 任务 | 缺口（已核实） |
|---|---|---|
| G1 | **fusion 取消 / 重启** | 取消 → `worker.stop` → worker 在 `run()` **里面**发出 `error` → `_on_fusion_error` → `_unlock_ui`（`ui/main_window.py:8754-8758`），这时线程**还没退出**，`finally` 还没执行完（`overview_panel.py:764-771`，会 rmtree `_tmp_stores`）。<br>临时目录名是固定的 `zarr_path + ".inprogress"`（`:578`）；新任务开始时会先 rmtree 它（`:579-580`）。所以旧任务的 `finally` 可能删掉新任务的临时目录，新任务也可能删掉旧任务正在写的目录。<br>最后一个 tile 之后，多边形、预览图、发布这几步都不再检查 `_stop`（`:693-724`），所以很晚才按的取消，结果仍然会发布。<br>`fusion_meta.json`、`roi_config.json`、预览 PNG 是直接写入的（`:705-717`、`:750-757`） |
| G2 | **Step2 完成回调** | 信号接的是普通槽（`ui/step2_page.py:2750-2754`），没有启动时的身份。`_on_finished`（`:2826` 起）会写 UI、发出 `segmentation_done` 和 `open_qc_requested`、弹出模态框；`mark_roi_step` 用的是页面**当前**的 `_roi_id`（`:2858`）。<br>`segmentation_meta.json` 和运行注册表是直接写入（读-改-写）的 |
| G3 | **随机 patch** | `_on_random_patches_done`（`main_window.py:6083`）不核对任何身份，直接把 patch 加到**当前**的 Step0 页上 |
| G4 | **Step4 页 / 批处理对话框** | 没有身份；切换数据集或关窗时不停止 worker；计算期间只禁用了 Run 按钮（`ui/step4_page.py:115-117`），其他控件都能操作。批处理对话框在运行中关闭时，QThread 还在跑，而它的 lambda 指向已经销毁的对话框 |
| G5 | **预分割完成** | `_on_preseg_finished`（`main_window.py:5466-5477`）不比对 `run_id`，迟到的 finished 会清掉更新那次运行的状态。逐条记录本身是比对 `run_id` 的（`:5457`） |
| G6 | **Step1 重新做 fusion 时，可能替换掉 Step2 正在读的 fused** | Step2 通过缓存的 zarr 句柄一块一块地读（`utils/channel_cache.py:63-80`）。`_publish_store` 先把旧目录改名，再把新目录换到原路径，然后删掉旧目录（`overview_panel.py:397-427`）。期间没有任何东西锁住 Step1 |

### 2.3 Step0 的项目与 Save（用户新需求的现状）

- **没有「打开已有项目」的入口**：Step0 的 `▶ Load`（`_reload_from_paths`，`step0_page.py:6122`）会重置 `_roi_context`（`:10918-10919`），从不读取 `project_manifest.json`、`roi_index.json` 或 `step0_roi_result.json`。
- **Save**（`_btn_continue`，普通的 `QPushButton`，`:1434`、`:1445`，放在底栏 `:1492`）：同一个会话里，分析区域不变就复用工作区；否则新建（`:11052-11074`）。页面上已经有一个 `QToolButton` + `QMenu` 的例子可以照着做（`_patch_menu_btn`，`:1653`）。
- **整片校正写的是同一个工作区里的 `corrected_channels.zarr`**：增量 Save 用 `mode="a"`，否则用 `"w"`（`ui/step0/search_ctrl.py:2262-2283`）。Step2 的引擎方法只读 fused，不读校正产品；只有 HQ / HQ2 / CSD 方法读它，而这几种方法不再维护。
- **Step2 运行期间，其他页面都不锁定**（顶部导航标签从不禁用）。

## 3. 做法（按行，每处改动都配一条受控延迟测试）

### 3.1 异步生命周期（E5）

| 行 | 做法 | 复用的现有机制 |
|---|---|---|
| G1 | ① 解锁和重新开始改为**等 `QThread.finished`**，也就是线程真正退出之后。取消后对话框显示「正在停止…」，线程退出了才解锁。<br>② 临时目录名按运行区分：`<zarr>.inprogress.<run>`，`<run>` 是启动时生成的 uuid，不是哈希。<br>③ 发布之前再检查一次 `_stop`。<br>④ `fusion_meta.json` 和 `roi_config.json` 改为原子写入（复用 `core/provenance.write_json_atomic`）；预览 PNG 先写临时文件，再 `os.replace` | 现有的 fusion 令牌 `_fusion_callback_allowed` |
| G2 | ① 启动时记下令牌 `(dataset_gen_seen, roi_dir, roi_id, zarr_path)`；`_on_finished` / `_on_error` 先核对。对不上就**只记日志**：不写 UI、不跳页、不弹框。<br>② `mark_roi_step` 改用 **worker 自己的** `roi_dir`。<br>③ `segmentation_meta.json` 和注册表原子写入。<br>④ 按用户裁定，**不自动停止** Step2：用户不按 Stop，它就跑完，写进它自己的工作区 | 显示代数 `_dataset_gen_seen` |
| G3 | 请求里带上 `dataset_gen` 和 loader；回调里核对，对不上就丢弃 | 显示代数 |
| G4 | ① 计算期间冻结 Step4 页（只留 Stop，用户裁定）。<br>② 记下令牌；`_on_finished` / `_on_error` 先核对。<br>③ 切换数据集、关闭窗口时请求停止（与 Step2 的 `stop_background_jobs` 同一种方式）。<br>④ 批处理对话框运行中拒绝关闭（提示先按 Stop），或者先停止、等它退出再关闭（第 6 题） | Step2 的 `stop_background_jobs` 模式 |
| G5 | `_on_preseg_finished` 比对 `run_id` | 现有的逐条记录比对 |

### 3.2 Step0 项目工作区（用户新需求）

| 行 | 做法 |
|---|---|
| W1 打开已有项目 | `▶ Load` 时，如果输出目录里已经有项目（`project_manifest.json`），而且其中有**同一张切片**的工作区（按 A3 的 `slide_id` 判断，不按路径），就恢复那个工作区：`_roi_context` 指向它，分析区域和签名取自它的 `roi_manifest` / `roi_config`，Load 状态行显示「Project: <workspace>」。<br>选哪一个工作区：第 2 题。<br>没有项目，或者切片不同：行为与今天一样 |
| W2 Save 覆盖 | 打开了已有工作区后，Save 写回**同一个**工作区（走今天的增量逻辑：只重算有变化的通道）。<br>分析区域的几何变了（例如重画了 ROI）怎么办：第 3 题 |
| W3 Save as | Save 改为 `QToolButton`：主按钮是 Save，旁边的小三角菜单里有 **Save as new workspace**。它总是新建工作区，后面的步骤都用新的工作区 |
| W4 「不是最新参数」 | 只读比较，不传播，不重建（§0.3）：<br>- Step3 的运行下拉和 Step4 的来源标签读这个运行的 A3 出处（它依赖的 fused 与校正通道的令牌），与工作区**当前**的令牌比较；<br>- 不一致就加一个标记「made with older Step0/Step1 settings」；<br>- 没有出处记录的旧运行不加标记（无法判断，如实显示「unknown」还是不显示：第 4 题）。<br>不新增任何哈希 |
| W5 Step2 运行期间的 fusion | Step2 正在为这个工作区运行时，Step1 的 fusion 写到本会话的暂存目录 `step1/.staging/<run>/fused_<roi>.zarr`；Step2 结束（完成、出错或停止）时，再按今天的 `_publish_store` 换到正式位置，同时登记出处。<br>程序在 Step2 结束前就被关掉时，暂存怎么处理：第 5 题 |

## 4. 封闭白名单（草稿）

- `ui/main_window.py`：只改 G1 的解锁与重启门（`_on_fusion_error` / `_on_fusion_done` / `_start_fusion_worker` 一带）、G3 的回调、G5 的回调、关窗时增加 Step4 的停止请求、W5 的暂存发布接线。这些都是规则 6 允许的代数检查或接线。
- `ui/step0/overview_panel.py`：只改 `FullFusionWorker` 的临时目录名、发布前的 `_stop` 检查、原子写入、暂存目标。
- `ui/step2_page.py`：只改 worker 令牌、`_on_finished` / `_on_error` 的核对、`mark_roi_step` 的目标。
- `workers/segment_merge_worker.py`：只改 `segmentation_meta.json` 和注册表的原子写入。
- `ui/step4_page.py`：只改冻结、令牌和停止。`ui/batch_step4_dialog.py`：只改关闭时的处理。
- `ui/step1_presegmentation/random_job.py`：只改请求里带上身份。
- `ui/step0/step0_page.py`：W1 / W2 / W3 / W4 的接线。打开项目、选择工作区、Save / Save as 的逻辑放进**新模块** `utils/workspace_session.py`（规则 6：新的编排放在两个大文件之外）。
- `ui/step3_mask_bar.py` / `MainWindow._step3_run_label`：W4 的标记文字。
- 新增 `core/async_guard.py`：**仅在**现有代数都不适用时才用的一个小辅助函数（启动时取一个值，到达时比较）。这是计划允许 A6 新增的唯一机制。
- 测试：新增 `tests/test_v16_a6_async.py`、`tests/test_v16_a6_workspace.py`。
- 文档：`UI_SURFACE_RULES.md`（Step0 的 Load 行、底栏 Save ▾、Step4 冻结、Step3 / Step4 的标记）、`docs/user_guide.md`、`docs/用户指南.md`、本申请的执行记录、v2.4 §12 进度。

## 5. 不改的范围

TaskManager、事务框架、新的存储框架（§15.3）；worker 的计算逻辑；清单之外的 worker；HQ / HQ2 / CSD 读校正产品的方式（不再维护，只记录）；Step0 整片校正原地写入的方式（它被「运行期间拒绝切换数据集」保护着，只登记）。

## 6. 预注册验收门

- **每一处改动**（G1–G5、W5）都配一条受控延迟测试：让旧任务在切换之后才到达，**在 A6 之前的代码上必须变红**（证明缺口真实存在，即反向注入），A6 之后变绿。
- **只登记为合规的行**：每种完成 / 提交协议各配一条测试（单文件 `os.replace` 一条，「标记最后写」一条），不必每行一条。
- **W1–W3**：
  - 打开已有项目后 Save 不新建工作区；Save as 一定新建；
  - 切片不同时不复用；
  - 工作区数量的变化符合预期；
  - 增量逻辑不变（签名相同的通道照旧跳过）。
- **W4**：改了 Step0 参数并 Save 覆盖之后，旧运行带标记、新运行不带；只读（运行前后项目目录树一致）。
- **W5**：Step2 运行期间做的 fusion 不碰正式位置；Step2 结束后才发布；Step2 读到的 fused 在整个运行期间逐位不变。
- **单调绿色规则**：全部离屏模块和 GPU 模块，当前代码和 HEAD 各跑一遍，逐条对比失败的测试名。
- **真机**：
  1. 关掉程序再打开 test1 副本，Save 不新建工作区；Save as 新建；
  2. Step2 运行中回 Step0 改参数并 Save，Step2 照常跑完，Step3 里那个运行带标记；
  3. Step2 运行中重新做 fusion，Step2 结束后新 fusion 才生效；
  4. fusion 运行中点取消，「正在停止」之后才解锁；
  5. Step4 计算期间页面被冻结。

## 7. 风险

| 风险 | 对策 |
|---|---|
| 打开已有项目时选错工作区 | 只认同一张切片（`slide_id`）；状态行显示工作区；第 2 题 |
| Save 覆盖时改了几何，让下游结果失效 | 第 3 题（建议几何一变就提示 Save as） |
| 等线程退出才解锁，让取消显得「慢」 | 对话框显示「正在停止…」；最多再等一个 tile |
| 暂存的 fusion 被遗留 | 第 5 题 |
| 范围比计划的 A6（约 3 天）大 | 估计 §8；用户已授权扩大范围 |

## 8. 估计

- 异步部分（G1–G5）：约 2 天；
- 工作区部分（W1–W5）：约 2.5 天；
- 回归：0.5 天；
- 真机：约 1 天。
- 合计约 **5–6 个工作日**，比计划中 A6 的约 3 天多，多出的是用户新增的工作区需求。按停止规则 5，总上限不悄悄延长：这一点记入 v2.4 §12，由用户确认（第 7 题）。

## 9. 与计划的关系

- 计划 §15 的 A6 只覆盖 G1–G5。W1–W5 是用户 2026-10-01 裁定并入的，记进 v2.4 §12 的「用户插入块」表（与 S2T / S0P 同一张表）。
- 计划 §17 的 A8「项目状态的唯一持有者」以后会接管「当前打开的是哪个工作区」。W1 新增的 `utils/workspace_session.py` 正是那份状态，A8 只是把它提升为 ProjectState，不另造第二份（§17.1）。

## 10. 请用户裁定（每题给出推荐项）

1. **范围**：按 §3 把 G1–G5 和 W1–W5 并进 A6，记入「用户插入块」表。**建议同意。**
2. **W1 打开已有项目时选哪个工作区**：
   - (a) 同一张切片里最近一个，即 `active_roi_id`，前提是它属于这张切片，否则取这张切片最近的那个；
   - (b) 有多个时弹出选择框；
   - (c) 在 Load 行加一个下拉框，让用户选择。
   - **建议 (a)**，并在状态行显示工作区名，后续可以再加 (c)。
3. **W2 覆盖时几何变了**：
   - (a) 提示「几何变了：建议 Save as 新工作区」，用户选择 Save as 或仍然覆盖；
   - (b) 几何一变就自动 Save as（与今天的行为一致）；
   - (c) 一律覆盖。
   - **建议 (a)**。
4. **W4 没有出处记录的旧运行**：
   - (a) 不加标记；
   - (b) 标为「unknown」。
   - **建议 (a)**。
5. **W5 程序在 Step2 结束前被关掉时，暂存的 fusion**：
   - (a) 下次打开这个工作区时，如果没有 Step2 在跑，就自动发布暂存，并记日志；
   - (b) 丢弃暂存；
   - (c) 下次打开时询问用户。
   - **建议 (c)**：这是用户的数据，不擅自替他决定。
6. **G4 批处理对话框运行中被关闭**：
   - (a) 拒绝关闭，提示先按 Stop；
   - (b) 先停止、等它退出再关闭。
   - **建议 (b)**。
7. **工作量与上限**：A6 约 5–6 天，记入 v2.4 §12。**请用户确认。**

## 11. 用户裁定（2026-10-02 上午，第 1–5 题）

1. 同意：G1–G5 与 W1–W5 并入 A6，记入「用户插入块」表。
2. **(b)**：同一张切片有多个工作区时，弹出选择框，由用户决定。
3. **(a)**：几何变了就提示「建议 Save as 新工作区」；用户看到提示后仍要覆盖，就照用户的意思覆盖。
4. **(b)**：没有出处记录的旧运行标为「provenance unknown」。用户的理由：现在处在开发阶段，这样的运行会很多；正式发布后就不会有了。
5. **(c)**：程序在 Step2 结束前被关掉时，下次打开这个工作区要询问用户：「上次有一份 fusion 还没生效，要用它吗？」

6. **(b)**：批处理对话框运行中被关闭时，先停止当前的 worker，等它退出，再关闭对话框。
7. **同意**：A6 约 5–6 个工作日（比计划中 A6 的约 3 天多，多出的是用户新增的工作区需求），记入 v2.4 §12。

**按裁定对正文的修改**（实施时以本节为准）：
- W1：同一张切片有多个工作区时，弹出选择框；只有一个时直接用它，并在状态行显示工作区名。
- W2：几何变了先提示；用户坚持覆盖，就覆盖。
- W4：没有出处记录的运行显示「provenance unknown」。
- W5：暂存的 fusion 在下次打开工作区时询问用户是否启用。
- G4：批处理对话框关闭时，先停止、等它退出，再关闭。
- 这些都是 UI 改动，`UI_SURFACE_RULES.md` 和两份用户指南要同步更新（§4 已经列出）。

**实施中补充的裁定（2026-10-02，用户在终端里选择）**：
8. **W1 恢复方法和参数**：打开已有工作区时，从它的 `step0/correction_config.json` 恢复每个通道的方法和参数（以及它保存过的 Intensity）。corrected zarr 里已有、而且签名（方法、参数、算法版本、后端）相同的通道，算作「已计算」。所以不改任何东西直接 Save，会提示「No changes」，不重写任何文件。这只适用于打开已有工作区；新项目仍按 v15「不预先填入」的规则。
   - 理由：不恢复的话，重新打开后直接 Save，会把这个工作区里已经校正好的通道覆盖成 raw。

---

## 12. 执行记录（2026-10-02）

### 12.1 提交（本地，未推送；基准 `f8ace2b`）

| 提交 | 行 | 内容 |
|---|---|---|
| `4b73c5a` | G1 | fusion：等 `QThread.finished` 之后才解锁，重启门；取消后对话框显示「Stopping…」，Cancel 置灰；临时目录 `<zarr>.inprogress.<uuid>`（旧的固定名 `.inprogress` 作为遗留清掉）；发布前再查一次 `_stop`；`fusion_meta.json` / `roi_config.json` 原子写入，预览 PNG 先写临时文件再 `os.replace` |
| `1f7fce3` | G2–G5 | G2 Step2：令牌 `(dataset_gen, roi_dir, roi_id, zarr_path)`；迟到时只记日志，释放 Run / Stop，在**它自己的**工作区 `mark_roi_step`；`segmentation_meta*.json` 与 `segmentation_results_index.json` 原子写入。G3 随机 patch：请求和回传带 `dataset_gen` 与 loader。G4 Step4：计算期间除 Stop 外全部冻结，结束后各控件恢复原状态；令牌；`stop_background_jobs`；冻结期间不接受 `set_run`；批处理对话框关闭时先停止、等线程退出、再关闭（不再启动下一样本，不弹「Batch complete」）。G5：finished 带上 `run_id`，与当前运行比对 |
| `ffbfe12` | W1–W3 | 新模块 `utils/workspace_session.py`（按 A3 `slide_id` 找同一张切片、已提交 Step0 的工作区）；Load 时打开（只有一个就直接打开，多个弹选择框，可选「Start a new workspace」）；恢复区域、patch、方法、参数、Intensity；Save 写回当前工作区；区域变了先问（Save as / Overwrite / Cancel）；Save 旁的 `▾` 里有 `Save as new workspace`；Load 状态行显示 `Workspace: <id>` |
| （本提交） | 文档 | `UI_SURFACE_RULES.md`、两份用户指南、本记录、v2.4 §12 A6 行 |

**W4、W5 没有开始**，留待下一次。

### 12.2 改动的已有测试（都因为已批准的行为变化）

- `tests/test_step1_result_publication.py`（G1）：临时目录名变了，两处断言从 `.inprogress` 改为 glob `.inprogress*`。
- `tests/test_step0_background_correction_outputs.py`、`tests/test_step0_authoritative_save_barrier.py`（W2）：这几条测试重画区域后再 Save。现在会先弹询问（裁定 3a），测试改为回答推荐项 Save as，原来「新建工作区」的断言不变。

### 12.3 验收

- **反向注入**：`test_v16_a6_async.py` 的 15 条在 `f8ace2b` 的代码树上逐条变红（G1 5 条、G2 2 条、G3 1 条、G4 4 条、G5 1 条；另外 2 条是「同一上下文照常工作」的对照组，在新旧两边都通过）。在工作区里 15 条全部通过。`test_v16_a6_workspace.py` 17 条全部通过（旧代码里没有这个模块和这些入口）。
- **相关范围**（工作区代码，逐模块单独跑）都通过：`test_step2_tile_status`、`test_step2_skip_empty_tiles`、`test_preseg_contract`、`test_step2_legacy_stop`、`test_step2_runner_path`、`test_step4_page`、`test_batch_step4_dialog`、`test_step1_preseg_run_ui`、`test_random_patches`、`test_step1_fusion_isolation`、`test_step1_save_progress`、`test_step1_result_publication`、`test_step0_background_correction_outputs`、`test_step0_authoritative_save_barrier`、`test_step0_correction_param_inheritance`、`test_step0_background_correction_tab`、`test_step0_dataset_switch`、`test_step0_full_image_first`、`test_step0_full_image_recovery`、`test_step0_bg_parallel`、`test_ui_surface_contract`。
  - `test_step0_process_incremental` 有 5 条失败，与 S0P 回归中 old（`6b252e2`）的失败名单完全相同，是 A6 之前就有的。
  - `test_step1_preseg_run_ui` 第一次跑有 1 条失败（与 S0P 全量回归并发，用时 376 s），单独重跑 10/10 通过。
- **实施中发现并修复**：区域变了而用户选择 Overwrite 时，「无校正通道」那条分支仍然会提示「No changes」，导致 manifest 已经改写、handoff 却没有更新。现在改写区域本身就算作有变化。
- **A6 全量单调绿色回归**：还没有跑，要等 S0P 全量回归跑完。
- **真机**（§6）：留给用户。

### 12.4 advisory（未处理，等用户裁定）

1. G1：每次运行用自己的临时目录名以后，被强行杀掉的进程留下的 `.inprogress.<uuid>` 不会被下一次运行清掉（只清旧的固定名）。目前只能手工删。
2. G2：`utils/segmentation_registry.save_registry` 的注册表写入不在白名单里，仍然是直接写入。只把 worker 里的 `segmentation_results_index.json` 改成了原子写入。
3. G2：迟到运行的 `progress` / `tile_done` 信号不核对令牌（申请只要求核对 finished / error），在切换后仍可能更新 tile 网格和细胞计数。
4. G4：§4 白名单对 `main_window.py` 只写了「关窗时」。数据集切换时的停止请求是按 §3.1 G4 ③ 加的。REV 发现最初的位置不对（见 §12.6 B1），现在放在 `_on_step0_dataset_committed`。
5. W1：Intensity 的恢复直接写了 `ChannelWorkbench` 的 `_params` / `_user_adjusted`。这个控件没有公开的设置接口，而 `channel_workbench.py` 不在白名单里。

### 12.5 按 handoff_next2 补齐（T0 = 2026-10-02 15:10）

- **入口条件**：S0P 全量回归完成（`e381dac`），没有测试在跑，工作区干净。
- **G2–G5 核对**：代码和受控延迟测试都已在 `1f7fce3` 里，不重做。只补缺的：
  - G2：补原子写入测试 `test_step2s_results_index_survives_a_failed_write`（结果索引写到一半失败，旧文件完整、没有临时文件）；
  - §6 的两条协议测试：`test_protocol_a_single_file_is_replaced_whole`（`write_json_atomic`）和 `test_protocol_b_the_completion_mark_is_written_last`（Step3 标签金字塔：先写 `.partial`，`complete` 最后写）。两条在新旧两边都通过，说明这两处登记为合规的协议确实成立；
  - G4：冻结测试补一条断言：标记列表随它所在的组一起被禁用。
- **反向注入（按 next2 §4.6，旧代码树在 `~/fusionflux/bench_a6/old/block01` = `f8ace2b`）**：`test_v16_a6_async.py` 共 18 条。
  - 在旧代码上：14 条失败；批处理对话框那条让进程崩溃（rc=139：旧代码销毁了仍在运行的 QThread），也算失败；4 条通过（2 条对照组、2 条协议测试）。
  - 在工作区：18 条全部通过。
  - `test_v16_a6_workspace.py` 在旧代码上无法导入（没有 `utils/workspace_session.py`），在工作区 17 条全部通过。
- **G4 冻结范围与 next2 列表对照**：next2 列的是运行选择、ROI、各勾选项、输出目录、前缀、Batch、Back。实现冻结的是页面上的全部输入控件（只留 Stop），比列表多出两类（记为 advisory，没有改）：
  - 两个 `Browse` 按钮（属于运行选择和输出目录那两行）；
  - 6 个分组折叠箭头（`QToolButton`）：计算期间不能折叠或展开分组。

### 12.6 REV：独立审核（Fable 5.1 + codex gpt-6-astra medium；Fable 整合）

审核范围：G1–G5（`4b73c5a`、`1f7fce3`、`d487c45`）。两位审核员都没有发现新增哈希或新机制，也都确认同一上下文下令牌不会误丢合法结果。最终结论：**1 条 blocker（已修），9 条 advisory**。

**B1（已修，提交见下）**
- 问题：Step4 的停止请求原来挂在 `_discard_step1_dataset_state` 里。这个函数也被**同一数据集**的 handoff 失效调用（Step4 运行中回 Step0 改已经 Save 过的 ROI）。这时数据集代数不变，令牌相同，于是提取被中止，还弹出「Stopped by user.」。
- 修复：停止请求移到 `_on_step0_dataset_committed`，只有真正切换数据集时才停止。
- 测试：
  - `test_a_same_dataset_invalidation_leaves_step4_running`：修复前（`d487c45`）红，修复后绿；
  - 对照组 `test_a_dataset_switch_stops_step4_and_drops_its_answer`：修复前后都是绿。

**advisory（没有改，等用户裁定）**
1. 批处理对话框 `_stop_then_close` 先 `stop()`、后连接 `QThread.finished`。线程如果恰好在这几行之间结束，对话框会停在「Stopping…」，但 Esc 或 ✕ 仍能关闭。codex 判为 blocker，Fable 整合后判为 advisory：窗口只有微秒级，公开路径无法有意复现，也不违反 G4 验收门。修复很小（先 connect，再 stop，然后复查一次 `isRunning()`），并有确定性的测试思路。
2. G1：发布前的 `_stop` 检查放在预览 PNG 写入之后。很晚的取消会留下一张新预览图，而对应的 zarr 并没有发布。
3. G4：运行中 `set_run` 被拒绝，Step3 → Step4 的交接被丢掉，运行结束后也不补。
4. G1：`QProgressDialog.close()` 会发出 `canceled`，所以 `_close_fusion_dialog` 会重入取消 handler。今天无害，属于潜在重入。
5. 测试证明力：协议 B 的测试没有观察 `complete` 确实最后写；G3 的测试用私有属性模拟切换，没有走 `dataset_committed`。
6. `save_registry` 仍是直接写入（即 §12.4 第 2 条）。
7. Step2 的 progress / tile 信号不核对令牌（即 §12.4 第 3 条）。
8. Step4 冻结比 handoff 列表多出 Browse 和分组折叠箭头（即 §12.5）。
9. 字面白名单偏差：G5 在请求发起处加了 `run_id` 包装；G1 改了已有测试的两行断言（§12.2 已记录）。

### 12.7 A6 全量回归（2026-10-02 15:25–17:39）

- new = `bf048c7`（代码冻结点），old = `f8ace2b`。脚本 `~/fusionflux/bench_a6/reg/run.sh` 按用户当天的裁定提速：每个模块超时 10 分钟；33 个既不导入 Qt 也不加载真引擎的模块合成一组跑；S0P 回归里两边都坏的 7 个模块只跑 new 一侧。另有磁盘门和内存门。
- **结论：没有 A6 造成的新增失败。**
  - 逐条对比失败测试名，只在 new 上失败的有 2 条：
    - `test_seg_runner_engines::test_stardist_expansion_returns_the_nuclei_from_before_expanding`：S0P 回归时已确认两边都不稳定；
    - `test_step0_step1_handoff_contract::test_step0_write_failure_does_not_emit`：**W3 引起**。测试用 `Step0Page.__new__` 造了一个只有部分属性的页面，缺少 W3 新加的 `_btn_save_menu`。真实路径走不到这里。修复见 §12.8（`_set_save_enabled` 用 `self.__dict__.get`，与页面里现有的写法一致；没有改测试）。
  - `test_step0_floor_prefetch` 在两边都超过 10 分钟。放宽到 25 分钟各重跑一次：new 12 条失败，old 13 条失败，没有「new 失败、old 通过」。
  - 其余有失败、超时或崩溃的模块，两边表现相同。

### 12.8 REV 后修正（用户 2026-10-02 下午在终端裁定）

| 项 | 内容 |
|---|---|
| A1 | 批处理对话框：先连接 `QThread.finished`，再 `stop()`；接线后复查一次 `isRunning()`；`_close_after_stop` 只执行一次 |
| A2 | fusion 不再写 `fused_*_preview.png`。没有任何代码读它；Step2 的概览图直接取自 fused zarr |
| A3 | Step4 运行中收到的交接（Step3 → Step4）先记下，页面上显示一行「Another run was chosen: … It is opened when this extraction ends.」，结束后自动打开 |
| A5 | Step2 的 progress / tile_done / tile_skipped 也核对令牌；对不上时不绘制，页面「Progress」下方显示一行旧运行的提示，并给出它的输出目录绝对路径（运行中显示「still computing」，结束后显示「has finished / has stopped」） |
| A6 | `utils/segmentation_registry.save_registry` 改为 `write_json_atomic` |
| A7 | fusion 开始时，只要窗口里没有仍在运行的已退役 fusion 线程，就删除目标旁边遗留的 `.inprogress.*`（被强行杀掉的运行留下的）。这一点由 MainWindow 判断，通过 worker 的 `sweep_leftovers` 告诉它 |
| B9（W4 的一部分） | Step3 运行下拉框：没有 `segmentation_run` provenance 记录的运行，在行尾右对齐显示「provenance unknown」，可以盖住名字的右端；tooltip 里也有。只看有没有记录，不比较令牌，不新造哈希。下拉框收起时显示的当前项不带这个标记 |
| 兼容 | `_set_save_enabled`：没有 `_btn_save_menu` 的页面对象跳过（§12.7） |

- 测试：`test_v16_a6_async.py` 新增 11 条（r1、r2、r3、r5 ×2、r6、r7 ×3、b9 ×2）。
  - 在修正前的代码（`ec5846a`）上：10 条失败，1 条通过（r7 中不允许清理的那组对照）；
  - 在当前代码上：整个文件 31 条全部通过。
  - `test_step0_step1_handoff_contract`：在修正前失败 1 条，修正后 39 条全部通过。
- 相关模块（逐个单独跑）都通过：`test_v16_a6_workspace`、`test_step2_tile_status`、`test_step2_skip_empty_tiles`、`test_step4_page`、`test_batch_step4_dialog`、`test_step1_result_publication`、`test_step1_fusion_isolation`、`test_step3_masks`、`test_step3_mask_bar`、`test_step3_mask_wiring`、`test_ui_surface_contract`。
  - `test_step2_runner_path` 的 StarDist 交接测试不稳定：同一条测试连跑 3 次，结果是失败 1 条、全过、失败 2 条，每次失败的参数组合都不同。它不涉及本次改动的页面代码，和 §12.7 的 StarDist 不稳定是同一类。
- **执行窗口的失误（如实记录）**：17:39 回归跑完后，只在终端里告诉了用户，没有按协议发邮件；16:21 之后也没有再按 30 分钟的间隔查邮件。用户 18:19 的邮件直到 21:33 才回复。这段时间也没有跑修正的测试。21:33 起恢复按协议执行。

### 12.9 真机验收第一批（用户，2026-10-02 晚）与修复

- **W1–W3 第 1–6 项：通过**。
- 第 7 项（另一张切片）：用户找不到别的 OME-TIFF。机器上有一张合成切片 `/home/ming/fusionflux/synthetic/synthetic_2x2_mirror.ome.tif`，可用来补验。
- **发现：打开已有工作区后进不了 Step1**（Step2 可以进）。
  - 原因：Step1 的入口要求窗口已绑定一份 Step0 handoff，而绑定只在 `step0_complete` 时发生。打开工作区不发这个信号；不改东西直接 Save 只显示「No changes」，也不发。
  - 用户的规则：之前 Save 过 Step0、而且 Step0 没有改动的工作区，就应该能进 Step1。
  - 修复：打开工作区后，在 Load 的最末尾（`dataset_committed` 之后，因为它的处理会清掉窗口里 Step1 的状态），把这个工作区**已经提交**的 handoff 原样发出去。不重写任何文件；由窗口的权威读取器照常验证。发 `step0_complete` 的 payload 改由 `_handoff_payload` 统一构造，Save 和打开工作区共用。
  - 验证：
    - `test_v16_a6_workspace` 新增 2 条（打开后发出一次、内容是磁盘上的 handoff、不重写；没有打开工作区时不发）；
    - 在 `…_48e7` 的临时副本上走真实的 Load：Load 后 `step0_done=True`、`step1_ready=True`，`_go_to_step1()` 进入 Step1。
- G1、G5 要进 Step1，等修复后补验。
- G3（随机 patch 期间切换切片）：生成太快，真机上来不及切换。用户认为实际使用中也一样。改由自动测试 `test_random_patches_for_the_previous_slide_are_dropped`（受控延迟）覆盖，不做真机。

### 12.10 真机：G1 复验通过；fusion 对话框残留的修复（2026-10-02 夜）

- 去掉 Generate 写回（`a256f05`）之后，用户复验 **G1：通过**。取消后可以马上再 Generate，也能正常跑完。
- **发现**：不论是取消后停止，还是正常 Save 完成，屏幕上都会残留一个空白的黑色「Step1 — Fusion」窗口。
  - 原因 1：`QProgressDialog.close()` 会发出 `canceled`，于是每次正常结束都会再走一遍 Cancel 的处理函数，并重新 `show()` 对话框。这就是 REV 的 advisory A4，当时判断为「今天无害」，判断有误。
  - 原因 2：对话框只是被 `close()`，引用丢掉之后对象并没有销毁，它的原生窗口还留着。在 WSLg 上做了实测：close 之后，这个原生窗口仍在应用的窗口列表里，合成器能否把它真正移除取决于时机。
- **修复**：`_close_fusion_dialog` 依次 blockSignals → hide → close → deleteLater；Cancel 处理函数在对话框已被撤下时直接返回。
- **测试**：`test_the_fusion_dialog_is_destroyed_and_not_re_shown`，正常结束和取消后结束各一条。
  - 修复前（`a256f05`）两条都变红：正常结束时 Cancel 处理函数又跑了一遍；对话框对象没有被销毁。
  - 修复后两条都通过。
  - 相关模块都通过：`test_v16_a6_async`（33）、`test_step1_fusion_isolation`、`test_step1_save_progress`、`test_step1_result_publication`、`test_step1_fusion_settings_commit`、`test_step1_display_mapping_commit`、`test_ui_surface_contract`。

### 12.11 真机第二批（2026-10-03 凌晨）与修复

- **第 13、14、15、17、18 项：通过**。第 16 项（批处理窗口）留待以后验。
- **第 12 项发现**：Step2 正在跑的时候，在 Step0 点 Save as，Step2 的计算没有中断，但 Status Overview 和 Progress 卡住了，直到运行结束才弹出完成框，进度条一下跳到 100%。Save as 那一刻正在跑的 tile 一直是黄色；从它到最后一块之间的 tile 全是灰色。
  - 原因：
    - Save as 之后，主窗口读取新的交接，在 `main_window.py:3423` 把 Step2 页面改绑到了新工作区；
    - A5 的令牌核对因此把旧运行的进度信号**全部丢掉**，既不画，也不记；
    - 回到 Step2 时，`_go_to_step2` 用的是仍然指向旧工作区的 `step1_output`，又把页面改回了旧工作区。身份重新对上，后面的信号照常画出来，结束时也照常弹框；
    - 中间被丢掉的那几块 tile，从此没有任何记录。
  - 修复：
    - 运行的进度、每块 tile 的状态和细胞数，**一律记进这次运行自己的记录**；
    - 只有页面绑定的上下文就是这次运行的上下文时，才把它们画出来；
    - 页面一回到这个上下文（`set_roi_context` 之后），就从记录里把网格、进度条和细胞数整个补画回来，并隐藏「旧运行」那一行提示；
    - 被替换掉的旧 worker 迟到的信号，一概不记。
  - 测试：新增 2 条。修复前（`0306bde`）两条都变红，修复后通过。
  - 相关模块都通过：`test_step2_tile_status`、`test_step2_skip_empty_tiles`、`test_step2_layout`、`test_preseg_contract`、`test_step1_to_step2_handoff`、`test_step1_step2_handoff_e2e`。
  - 遗留问题（只记录，没改）：Save as 之后，Step2 页面到底应该绑定哪个工作区，现在由 `step0_output` 和 `step1_output` 谁最近被赋值来决定，前后并不一致。这一点留给数据版本管理块：届时「当前版本」是唯一的答案。
- **第 17 项**：标记文字由 `provenance unknown` 改为 `unknown`（用户裁定）。
- **第 19 项**：清理本身生效了，真正遗留的 `…inprogress.a499d1f5…` 在 fusion 之后被删掉了。用户手动建的测试文件夹因为名字里的空格没加引号，实际建成了两个文件夹（`fused_Full`、`WSI.zarr.inprogress.test`），它们不匹配 `fused_Full WSI.zarr.inprogress.*`，所以没被删，这是预期行为。
- **Step2 GPU OOM**：原因是验收清单里的启动命令带了 `LD_LIBRARY_PATH`，StarDist 因此改在 GPU 上跑（之前的成功运行都是 CPU）。StarDist 在 GPU 上不设 `n_tiles`，6 GB 显存放不下一整块 tile。清单已改正。这个 GPU 隐患只记录，没改，等用户裁定。

### 12.12 第 12 项复验失败与重新设计（2026-10-03，用户裁定）

- **复验结果**：Save 成功之后，tile 全部变灰，进度条仍然不动（终端里的进度一直在更新）。切到 Step3 再切回 Step2 之后，tile 和进度条才恢复正常。
  - 原因：上一次的修复（`f3dcd58`）在页面改绑到别的工作区时，把 tile 全部置灰，并停止绘制。只有页面再被绑回原来的工作区时，才从记录里补画。而直接回到 Step2 时，页面并没有被绑回去；先去 Step3 再回来，才碰巧绑了回去。
- **用户裁定（新的规则）**：
  - Step2 跑着的时候，在 Step0 点 Save as，Step2 在运行结束之前**一直绑定旧工作区**；
  - 运行结束时，完成框里提示「将切换到新工作区」；
  - 用户点任何一个按钮（Step3、Step4 或 OK），Step2 都切换到最新的工作区。
- **实施**：
  - `Step2Page.set_roi_context`：运行进行中，交给它的另一个工作区只记为「待切换」，不改绑。交给它的若是这次运行自己的工作区（比如 `_go_to_step2` 手里那份过时的副本），什么也不做，「待切换」保持不变。
  - 运行结束时（完成、出错、停止、迟到结束都一样）调用 `_apply_pending_context`，切换到待切换的工作区。完成框的正文最后一行写明「Step2 now switches to the new workspace: <id>」，切换发生在完成框关闭之后，不管按的是哪个按钮。
  - `MainWindow._go_to_step2`：`step1_output` 和 `step0_output` 指向不同的工作区时（Save as 之后，新工作区还没有 Step1 结果），Step2 绑定 `step0_output` 的工作区，也就是最新的那个，不再被 `step1_output` 拉回旧工作区。
  - 真正换了切片（数据集代数变了）时，仍按 A6 G2 的规则：不显示旧运行的结果，只显示一行旧运行的提示。
- **测试**：
  - 原来 4 条「运行中换工作区 → 什么都不显示」的测试，改成按「换数据集」来触发（保证不变）；
  - 新增 3 条：运行中 Save as 不改绑、进度照常绘制、完成框写明要切换的工作区、关掉完成框后切换；出错结束时同样切换；进入 Step2 时绑定最新的工作区。
  - 修复前（`f3dcd58`）新增的 3 条都变红；4 条换数据集的测试在新旧两边都通过。
  - 相关模块都通过：`test_step2_tile_status`、`test_step2_skip_empty_tiles`、`test_step2_layout`、`test_preseg_contract`、`test_step1_to_step2_handoff`、`test_step1_step2_handoff_e2e`、`test_step1_handoff_invalidation`、`test_step1_dataset_switch`、`test_step0_step1_handoff_contract`、`test_v16_a6_workspace`、`test_step2_remap_integration`、`test_step2_legacy_stop`。
- **已知不一致（只记录）**：Save as 时，主窗口 `_on_step0_complete` 会立刻把 Step2 的「Output」输入框改成新工作区的 step2 目录。运行结束切换之后，两者才重新一致。这只影响下一次运行的默认输出位置，不影响正在跑的这次（它的输出目录在开始时就已经固定）。

### 12.13 第 12 项：切换到新工作区后清空上一次运行的显示（2026-10-03，用户裁定）

- **真机**：Save as 之后，运行结束，点 OK，Step2 确实切到了新工作区，但页面上仍是上一个工作区那次运行的全绿 tile 和 100% 进度。新工作区还没有 Step1 结果，所以概览图也还是旧的。
- **用户裁定**：切换后**保留概览图**，只清空 tile 和进度。
- **实施**：Step2 换到另一个工作区时，如果页面上还留着上一个工作区那次运行的记录，就清空它：tile 全部回到灰色，进度条和细胞数归零，旧运行提示隐藏。概览图不动。
- **测试**：在 `test_save_as_during_a_run_keeps_step2_on_the_runs_workspace_until_it_ends` 里补了 4 条断言：tile 全灰、进度为 0、细胞数为 0、概览图的 zarr 保留。修复前（`52ee0e8`）变红，修复后通过。相关模块都通过。
