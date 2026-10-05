# v16 块 A8 — 项目状态的唯一持有者 + A5 资源分级：实施申请 v0（草稿，待审）

日期：2026-10-05。分支 `v16`，HEAD `7851834`（A7 已推送，门 8 通过）。

依据：
- 合并基准 v2.3 §17（A8）、§7（A5，v2.2 §7 原样）、§0.2 E2 / E4（当前身份显式化）、§0.2 伴随项「资源分级常量」。
- v2.4：A8 在 A9 之前；A9 不能写 A7 的相机持有者。
- 用户 2026-10-05 裁定：A8 之前不拆大文件；A8 之后正式复审拆分；每块报告两个大文件的增长。
- `AGENTS.md`：第 4 条（新增持有者 / 状态机必须本块明确授权——本申请即请求）。

状态：**v1，用户 2026-10-05 批准（最低档），附两条实施约束（§11）**。

---

## 1. 目标

§17.1：`ProjectState` 成为四项「当前身份」的**唯一权威持有者**，各页只读：

| 身份 | 含义 |
|---|---|
| `active_slide` | 正在处理的切片（路径 + 已有的 `slide_id`） |
| `active_region` | 当前工作区 / ROI（`roi_id`、`roi_dir`、名称、bbox） |
| `active_segmentation_run` | Step2 → Step3 → Step4 共用的那一次分割结果（run 文件夹 + 区域名） |
| `active_source` | 渲染器取像素的来源（raw / corrected，corrected 产物与决定，所属 correct run） |

相机与显示状态仍归 A7 的持有者与 `ChannelDisplayState`，不动。**只搬归属，不改控件、布局和行为。**

## 2. 只读盘点（2026-10-05，现码）

四项都**没有**显式字段，且每项都在多处重复保存（`MW` = `ui/main_window.py`，`S0` = `ui/step0/step0_page.py`）：

### 2.1 切片（≥ 8 处）
`MW.loader`（:499；写 :2945 / 3003 / 3165 / 3597）；`S0.loader` / `S0.ome_path`（:311 / 313；提交块写 :6144 / 6154）；`MW.step0_output["loader"/"ome_tiff_path"]`；两个文件里的模块全局 `OME_TIFF_FILE`（MW:3003 / 3606，S0:6150）；`Step0ExploreTab._dataset_path`；`ChannelDisplayState` 的身份命名空间；`FusionDomainModel.bind_dataset`；`CameraOwner._dataset`。内容型 `slide_id`（`workspace_session.slide_id_of`）不在任何活对象上。另有两个同名但含义不同的 `_dataset_gen`（MW:564 帧时钟、S0:418 数据集代际）。

### 2.2 区域（≥ 5 处）
`MW._active_roi`（:577，总是 `rois[0]`，「当前」是按位置推定的）与 `MW._rois`；`MW.step0_output["roi_id"/"roi_dir"]`（:3608）；`Step2Page._roi_id/_roi_dir/_step2_dir`（step2_page:200，`set_roi_context`）；`S0._roi_context`（:393）；`MW._step3_mask_roi_dir`（:2051）；磁盘上 roi index 的 `active_roi_id`。

### 2.3 分割 run（≥ 6 处）
`session.json` 的 `viewing`（`run_store.session_pointers`；写：`MW._rm_note_viewing` :10044、S0 Save :11676、删除时 `repoint_session`）；`MW._step3_mask_key`（"run\x1froi"，写 :2050 / 2068 / 2118 / 2169）；`MW._step3_requested_run`（:4836，Step2 的 `open_qc_requested` → `_go_to_step3`，一次性消费 :1971）；Step3 下拉框自身；`Step4Page._run_edit` / `_job.run_dir`（`set_run` :49，`_step4_choice` MW:4929 依次读 `_step3_mask_key` → `step3_output` → `step2_output`）；`MW._rm_step2_applied_from`；`step2_output["output_dir"]`。

### 2.4 数据源（≥ 3 处）
`MW._corrected_zarr_path` / `_corrected_decisions`（:579 / 581；写 :3261 / 3273）；`loader.set_corrected_zarr_store(...)`（MW:3276）；`step0_output["corrected_zarr_path"]`（:3615）。`Step1ViewerBinding` 每次都从窗口的这些字段临时算出身份（step1_viewer_binding:63–82）。`SourceIdentity`（viewer/tile_types:37）是推导值，没人持有。

### 2.4b 不算「竞争持有者」的地方（codex）
相机 / 显示的按切片命名空间、正在运行的 job 自带的身份快照（Step2 worker、Step4 `_job`）、provider 与缓存键里的 `SourceIdentity`——它们合法地**记住**身份，不是当前身份的第二个持有者，A8 不动。`utils/perf_trace.py` 的 deque 已有上限（:149）。工作区身份与「工作区内的某个区域」是两回事。

### 2.5 可参照的持有者
`ui/camera_owner.py`（A7：几个具名写入口、无通用 setter、按切片重置）。`utils/workspace_session.py` 的文档已写明「A8 lifts this into ProjectState」。

### 2.6 A5 现状
约 90 处队列 / 缓存 / 上限设置；几个没有显式上限的 `deque`（`workers/preload_scheduler.py:63–64`、`workers/display_seed_worker.py:48`、`viewer/multichannel_prefetch.py:148`、`utils/perf_trace.py:121`）与 `MW._proc_queue = mp.Queue()`（:8298）需要逐个判断（`perf_trace` 已有上限）是否「实际有上限」。缓存上限各自存在，但**总和可能超过本机内存**：Step0 视图与 Step1 视图各为原始 512 MB + 校正 2 GB（`ui/step0/step0_explore_tab.py:52–53`、`ui/step1_viewer_host.py:37–38`），另有 compose 256 MB、预加载 512 MB、montage 1 GB + 256 MB、preview 160 MB、overview 256 MB、GPU 纹理若干。代表性大图（2×2 合成拼接，约 3.7 GB）已在磁盘清理时删除，生成脚本 `scripts/make_synthetic_mosaic.py` 仍在。

## 3. 做法（v1：建议先做 §17.2 最低档 = 分割 run + A5）

### 3.1 持有者

新模块 `core/project_state.py`：不依赖 Qt 的小对象（照 A7 `CameraOwner`），**具名写入口**、无通用 `set`、无信号。v1 只放 `active_segmentation_run`（run 文件夹 + 区域名 + 是否本工作区产物）；切片、区域、来源三项在最低档下留在现有持有者，记入 v17（§17.2：每项仍只有一个持有者，不建第二个）。

### 3.2 语义（codex：先钉住现有行为，再迁移）

- **与 `session.json` 的 `viewing` 分开**：`viewing` 是「正在看的那条链的最下游 run」，可能是 correct / fuse / segment run，含义不变、写入时机不变。`active_segmentation_run` 是另一个字段：
  - 本工作区的 segment run 被选为当前时，照今天的规则同时写 `viewing`（`_rm_note_viewing`）；
  - **其他工作区或外部载入的结果**（Step3 `Load…`）只在内存里成为当前，**不写持久记录**——与今天一致；
  - 重开工作区时：`viewing` 是 segment run → 它就是当前；否则当前为空，Step3 按今天的 `choose_run` 规则选默认项，并把默认项记为当前。
- **决定字典等可变值不外露**：读口返回不可变快照。
- **运行中的 job 不受影响**：Step2 worker 与 Step4 `_job` / `_pending_run` 保持自己的快照；晚到的 Step2 结果沿用 A6 的「来源工作区」校验，**不能**把旧工作区的 run 设为当前。

### 3.3 写入口（v1 完整清单）

| 写入口 | 现有事件 |
|---|---|
| `choose_segmentation_run(run, region, local)` | Step2 一次运行完成（A6 校验通过后）；Step3 下拉框选择；Step3 `Load…`（`local=False`）；Step3 首次显示时的默认项；删除后 `repoint_session` 的替代项 |
| `clear_segmentation_run()` | 换工作区 / 换切片（`dataset_committed`、打开另一工作区）；当前 run 被删且无替代；上游变化使其不再列出（今天 Step3 列表的规则） |

Generate / 复用改变 `viewing` 为 fuse run 时：当前分割 run **按今天的行为**处理（实施第一步先写特征测试把今天的结果钉住，再迁移）。

### 3.4 读者改为只读同一字段

`_step4_choice`（删除 `_step3_mask_key` → `step3_output` → `step2_output` 的回退链，只读当前字段；Step4 自己的选择与运行中 job 不变）；Step3 默认选中项；`_rm_step2_from_viewed`（RM 第 15 题行为不变）；`_go_to_step3` 不再经由 `_step3_requested_run`（`open_qc_requested` 直接写当前字段）。`_step3_mask_key` 只保留「当前区域内显示哪个 key」的视图状态。

### 3.5 A5

- **资源分级常量** `core/resource_tiers.py`：两个档位（本机 WSL2 约 10 GB / RTX 3060 6 GB；未来边缘档 16 GB RAM / 6 GB VRAM，RAM 目标 ≤ 12 GB、硬限 < 14 GB，VRAM 目标 ≤ 5.0 GB、硬限 < 5.5 GB）。下列已有常量**数值不变**，改为从这里读：`core/bg_parallel.py`（`MEM_RESERVE_BYTES`、`MEM_PER_TILE_BYTES`、`MEM_PER_TILE_FAST_BYTES`）、`ui/step0/step0_explore_tab.py`（`RAW_CACHE_BYTES`、`CORRECTED_CACHE_BYTES`）、`ui/step1_viewer_host.py`（同名两项）、`ui/step1_compose_coordinator.py`（`DEFAULT_COMPOSE_CACHE_BYTES`）、`workers/preload_scheduler.py`（`DEFAULT_MAX_BYTES`）、`core/preview_compose.py`（`DEFAULT_MAX_BYTES`）、`viewer/explore_view.py`（`OVERVIEW_CACHE_BYTES`）、`ui/step1_presegmentation/montage_supply.py`（两项）、`ui/step1_viewer_mount.py`（三项 GPU 纹理）、`ui/step1_gpu_layer.py`（`LABEL_TEXTURE_BYTES`）。**不做动态仲裁，不改任何缓存的行为。**
- **有界性清单** `docs/v16_a5_boundedness_inventory.md`：viewer、Step2、Step4 路径上每个队列 / 缓存 / 暂存的上限（或「实际上限及理由」），包括**各缓存上限之和**与本机内存的对比。发现无界的**新路径**时，单列修正项，作为本申请的修订另请批准。
- **记录**（§7.1）：viewer、Step2、Step4 在代表性数据上各跑一次：峰值 RSS、峰值 VRAM、tile 大小、队列深度、缓存字节、耗时；Windows 侧 working set、commit、系统 commit、page file 增长由用户读取（必需的证据）。验收看「上限有文档、峰值有测量、本机不 OOM」，未来边缘档目标**不是**今天的通过门槛（§7.3）。

## 4. 封闭白名单（v1，最低档）

- 新增：`core/project_state.py`、`core/resource_tiers.py`、`scripts/measure_a5.py`（只测量，不进产品）。
- `ui/main_window.py`：只改 §3.3 / §3.4 列出的写入点与读取点（`_rm_note_viewing` 调用处、`_step3_on_run_chosen`、`_step3_load_run`、`_step3_refresh_masks` 的默认项、`_rm_delete_run` 的替代项、`_go_to_step3`、`_step4_choice`、`_rm_step2_from_viewed`、Step2 完成回调、`_on_step0_dataset_committed`、打开工作区处）。
- `ui/step4_page.py`：仅当 `set_run` 的读取点需要时。
- `core/step3_masks.py`：仅当默认项规则需要读当前字段时。
- §3.5 列出的 11 个文件中的上限常量行（数值不变）。
- 测试：新增 `tests/test_v16_a8_project_state.py`、`tests/project_state_audit.py`（照 A7：包装具名写入口；旧代码上包装 `_rm_note_viewing` 与 `_step3_mask_key` 赋值）、`tests/test_v16_a5_resource_tiers.py`；**先写特征测试**钉住今天的选择行为；既有测试只改读取点，逐条记入执行记录。
- 文档：本申请执行记录、A5 清单与记录。
- **不在白名单**：`ui/step0/step0_page.py`（最低档不需要）、`ui/step1_viewer_binding.py`、`ui/step2_page.py`（除非 Step2 完成回调必须在页内改，届时先报）。

## 5. 不改的范围

切片 / 区域 / 来源三项的归属（最低档下记入 v17）；loader 与 viewer 的读取 / 调度 / 缓存逻辑；A7 相机持有者；`ChannelDisplayState`；`FusionDomainModel`；`session.json` / run_store 的格式与 `viewing` 语义；两个大文件的结构；控件与布局；两个同名 `_dataset_gen` 的命名（只在记录中注明）。`step0_output` 保留为交接记录。

## 6. 预注册验收

- **自动**：
  - 特征测试（迁移前写、迁移后照样过）：Step2 完成 → Step3 默认项；Step3 选择 / `Load…`；删除后的替代；重开工作区恢复；Generate 后的行为；多区域；外部结果不写持久记录。
  - Step2 → Step3 → Step4 在代表性数据上使用同一个 run，来自同一字段（§17.3）；Step3 换 run 后 Step4 与 Step2 参数跟随。
  - 唯一写者审计：切页、resize、身份未变的源刷新、打开通道期间写入 0 次；每个写入口各一条「恰好一次」；晚到的 Step2 结果在换工作区后不写；反向注入在 A8 之前的代码上变红。
  - A1 零漂移、A7 零回写照常通过。
  - A5：常量数值与改前一致且被各模块读取；清单列出每个上限与总和；记录含 §7.1 全部字段。
  - 聚焦回归 + 本里程碑全量回归，单调绿色。
- **真机**：Step2 跑一次 → Step3 显示它 → Step4 用它；Step3 换另一次 → Step2 参数与 Step4 跟随；Step3 `Load…` 外部结果 → 当前改为它，但重开项目后不恢复成它；删除当前 run → 指向替代；换工作区 → 清空。
- **增长报告**：两个大文件的行数增减与落点。

## 7. 估计

- 最低档（推荐）：约 4 天（含特征测试与 A5 清单 / 记录），另需你在 Windows 上读数的时间（约半小时）与重新生成 3.7 GB 合成大图（后台约 10–20 分钟）。
- 全量（四项）：在最低档之后再评估；codex 认为只有把语义（多工作区、外部结果、运行中的 job）理清后，6–7 天的估计才可信。

## 8. 回退

run 迁移一组提交、A5 常量一组、A5 清单 / 记录一组，可分别 revert。若迁移中发现某读者牵连过广，停下报告，未迁移部分留在原持有者（不会出现第二个持有者）。

## 9. 需要用户裁定

1. **范围**：最低档（分割 run + A5，约 4 天，推荐）还是全量？最低档下切片 / 区域 / 来源记入 v17。
2. **持有者**：`core/project_state.py`，照 A7 的「具名写入口、无通用 setter、无信号」？
3. **外部 / 其他工作区的结果**：保持今天的行为——可以成为当前，但不写持久记录（推荐）？
4. **A5 的 Windows 读数**由你在 Windows 上做（我给步骤），以及是否同意重新生成 3.7 GB 合成大图？
5. **缓存上限之和超过本机内存**：本块只如实记录（推荐，改动缓存行为需要单独批准，AGENTS 第 5 条），还是这次就设一个总预算？

## 10. 审核记录

- codex（astra low，2026-10-05）对 v0：方向正确，需修订——`viewing` 可指任意种类 run，不能直接作为分割 run 的持久化；保留跨工作区 / 外部结果不持久化的现有行为；写入点补全（Step0 应用校正产物、恢复、Generate、Step3 默认项、晚到的 Step2 结果的 A6 校验）；运行中的 job 与当前选择分开；白名单要逐项列出常量；源刷新只在身份未变时要求 0 写入；建议先做最低档、约 4 天。均已并入 v1。

## 11. 用户裁定（2026-10-05，同意独立审核意见）

1. **范围：最低档**——`active_segmentation_run` + A5；切片、区域、数据源留 v17（§17.2）。
2. **持有者**：`core/project_state.py`，纯 Python、无 Qt、具名写入口（`choose_segmentation_run`、`clear_segmentation_run`）、无通用 `set`、无信号；读口返回不可变快照。
3. **外部 / 其他工作区结果**：保持现状——可以成为当前，但不写入当前工作区的持久记录。
4. **A5**：同意重建 3.7 GB 合成拼接大图；Windows 侧读数由用户按给定步骤完成。
5. **缓存上限之和**：本块只记录，不设全局预算；只有真机峰值证明有问题时再单独设计。

**两条实施约束**：
- **C-a**：`_step3_mask_key` 降级为**纯显示缓存**。数据流只能是「用户选择 / Step2 完成 → `ProjectState.choose_segmentation_run()` → Step3 据此更新 `_step3_mask_key`」，不得反向由它推断当前 run；`_step4_choice()` 的旧回退链彻底删除。审计测试断言：`_step3_mask_key` 只服务 Step3 的 mask / 视图渲染，不被 Step4、RM 或「当前分割 run」判定读取。
- **C-b**：Generate 对当前分割 run 的影响，**先**由特征测试在旧代码上测出（保留还是清空）并写入执行记录，**再**改生产代码，迁移必须复现这个明确答案。
