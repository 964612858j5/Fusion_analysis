# v16 块 PA（A9 之前）申请 v1.2：A5 内存峰值 + 2026-10-05 验收发现的 4 个问题

日期：2026-10-05。依据：用户 2026-10-05 裁定（"A5 内存峰值在 A9 之前单独处理，同时本次验收还发现一些其他性能问题，请一并在 A9 之前处理"）；A5 清单 `docs/v16_a5_boundedness_inventory.md` §4–§5。

本申请只做只读调查的结论和方案。**用户批准之前不改任何代码。**

---

## 0. 总览

| 编号 | 问题 | 根因（已核对代码） | 方案要点 | 结果是否逐位不变 |
|---|---|---|---|---|
| PA-1 | Step0 Save 后通道勾选全部消失 | Step1 在后台写自己的显示状态时，发出了不分步骤的通知，Step0 的勾选框跟着被改 | 后台写别的步骤时不发通知 | 不涉及像素 |
| PA-2 | Step1 预分割：加一个方案后，已跑过的方案也重跑 | 每次 Run 都按全部组合重建任务，不比较已有结果；界面只显示最新一次运行 | 已完成且条件完全相同的任务直接沿用，只跑新的 | 沿用的结果就是原文件 |
| PA-3 | Step1 Generate 慢、内存 9.9 GB | 每块 1.67 亿像素整块处理，4 个通道各存两份 float32 加约 10 个中间数组；串行 | 块内再切成条带，多线程并行；映射一次完成 | **是**（逐位比对作为门槛） |
| PA-4 | Step2 主进程峰值 9.4 GB | 最后导出 OME-TIFF 时整幅 mask 转 float32、整幅 DAPI 读进内存 | 按 512×512 图块流式写出 | **是**（文件逐字节比对） |
| PA-5 | 合成图上切换 step 不流畅 | 只读代码得出的嫌疑点，尚未计时 | 先实测，再按实测结果改前几项 | 不涉及像素 |

---

## 1. PA-1 Step0 Save 后勾选消失（bug）

**复现**：已在无界面环境下复现（子代理，未改文件）。Step0 勾选 CD3 → 执行 Save 交接时的 `config.set_channels / load_panel / set_nucleus` → 状态里 CD3 仍为勾选，但 Step0 的勾选框显示为未勾选。

**调用链**：
1. 勾选保存在共享显示状态（`ui/block01_display.py`）中，按步骤分开存（step0 / step1 / step2）。Step0 的勾选框只是这个状态的画面。
2. Save 完成 → `MainWindow._on_step0_complete` → `_load_step0_roi_result` → Step1 配置面板 `config.load_panel`（`ui/step0/config_panel.py:703–715`）。它在 `using_scope("step1")` 里把 Step1 的可见性设为"只显示细胞核"。
3. `set_display_visible`（`block01_display.py:1035–1057`）写的是 Step1 的存储，没问题；但它**无条件**发出 `visibility_changed`（:1056）。
4. 全局通道栏 `_on_state_visibility`（`ui/widgets/channel_dock/global_dock.py:869`）收到后直接改勾选框，不看是哪个步骤的 → 屏幕上的 Step0 勾选被清掉。之后步骤没变，没有任何东西把它画回来。

`using_scope` 的说明本来就写着"Silent in both directions: no field notices of its own"，现在的行为违背了自己的约定。

**现有测试为什么没发现**：`tests/test_step0_step1_display_isolation.py::test_a_step1_handoff_does_not_untick_step0` 只检查状态，不检查勾选框；而且它的夹具事先跑过一次 `load_panel`，第二次没有变化，就不发通知。

**方案**：
- `block01_display.py`：只有当写入的目标步骤**不是屏幕上的步骤**时（`using_scope` 进入了另一个步骤），`set_display_visible` 和 `set_selected_channel` 才照常写入、照常 `bump` 但**不发**字段通知。目标就是屏幕上的步骤时（例如 `config_panel.py:933、963` 在 Step1 上恢复 Step1 自己的状态），照常通知（codex 意见 6）。颜色和映射的设置函数按同样规则检查一遍，如果它们是分步骤存的，就同样处理；不是就不动。
- 回到那个步骤时已有 `scope_changed` → 重画，Step1 进入时也有 `_resync_step1_display_from_state`，所以不会漏画。
- 测试：在 `tests/test_step0_step1_display_isolation.py` 新增——新建窗口（不预跑 `load_panel`），Step0 勾选 CD3，执行交接三步，断言**勾选框**仍勾选、Step1 的状态是"只显示细胞核"。
- 判定只用现有的 `self._scope` 和 `using_scope` 进入前的值（`previous`）：`using_scope` 真的切到了另一个步骤时不发通知，目标就是当前步骤时照常通知。**不新增**另一份"屏幕上的步骤"状态（独立审核）。
- 一并修复（独立审核裁定）：`ui/step0/step0_dock_adapter.py:241` 引用了未定义的 `visible_marker`，只有在选中通道和细胞核都不在列表里时才会触发 NameError。回退顺序：当前选中有效 → 当前选中；否则细胞核有效 → 细胞核；否则第一个当前可见的通道；否则第一个通道；否则不设选中。

白名单：`ui/block01_display.py`、`ui/step0/step0_dock_adapter.py`；测试 `tests/test_step0_step1_display_isolation.py`。

---

## 2. PA-2 预分割只跑新增或改动的组合

**现状**（`ui/main_window.py:5506` `_on_preseg_run`；`core/preseg_run.py:93–122` `build_tasks`）：
- 任务 = 勾选的 patch × 全部方案组合，`task_id = combo_id + "__" + patch 的 bbox`。
- 不和已有结果比较，所以 8 个 patch × 2 个组合 = 16 个任务。
- 每次 Run 新建一个 `runs/preseg_<时间>/`；界面（结果列表、拼图轮廓）只显示当前这一次运行，旧结果的行和轮廓会被清掉。

**"没有变化"的判定**：对每个新任务，在当前持有的上一次运行记录里找同一个 `task_id`，并要求以下全部相同：
- 记录状态为 OK，且记录里的 mask 文件都还在磁盘上；
- `params` 相同（直接比较参数字典）；
- patch 的 bbox 相同（包含在 `task_id` 里）；
- 像素来源相同：直接比较记录里已存的 `pixel_identity` 结构（原图、ROI、通道重映射、Step0 校正产物的元数据）；
- Fusion 设置相同：比较记录里**已有的** `fusion_settings_hash` 字段。这是现有字段，本方案不新增任何哈希；
- `halo_px` 相同；
- 引擎身份相同：用现有纯函数 `seg_runner.engines.behavior_identity(engine)` 得到当前的引擎身份，与旧记录里的 `engine_identity` 比较（codex 意见 2；独立审核：不依赖新运行尚未填写的 `run["engines"]`，也不需要启动模型）。

失败或取消的任务照常重跑。

**做法**：
- Run 时把任务分成"沿用"和"要跑"两类。确认对话框只按要跑的任务计数，例如"This run has 8 tasks (8 patches × 1 new combination; 8 finished results are kept)"。超过 10 个才弹窗，这个规则不变。
- **任务集合完全相同且全部可沿用**：只提示"所有结果都已是最新"，不新建运行文件夹。
- **任务集合变了，即使只是删除了组合或 patch**：新建一个"只复制"的运行，复制仍然存在且可沿用的任务，0 个新计算，立即发布。例：上次 A + B，删除 B 后按 Run → 新运行只有 A，界面显示与用户当前的方案一致（独立审核）。
- 新运行的 `params.json` 仍列出全部组合和任务，保证界面完整。沿用的结果把 mask 和记录**复制**到新运行（记录的 `run_id` 和 mask 路径改为新运行），所以新运行自成一体：删除或移动旧运行都不影响它，恢复会话只读新运行一个文件夹（codex 意见 3；独立审核裁定：复制）。复制来的记录**新增**两个字段 `reused_from_run`、`reused_from_task`，原来的 `created_at`、`engine_identity`、`device`、`runtime_s` 保留原值，如实说明这个结果是从哪次运行沿用来的。复制在开始跑新任务之前完成，按"先 mask 后记录"的现有发布顺序。
- 任务执行（`run_job.py`）：沿用的记录在开始前就放进任务的记录表和界面的记录表；作业只调度要跑的任务；停止、失败的结算不碰沿用的记录；`combo_status` 和汇总行按全部任务计数（codex 意见 4）。
- 界面：沿用的组合不清除其轮廓和结果行。

**需要裁定**：
- (a) 已裁定：复制到新运行。
- (b) 已裁定：只和当前持有的上一次运行比较，不在工作区历史运行里搜索。

白名单：`core/preseg_run.py`（新增纯函数 `reusable(...)` 和复制沿用结果的函数）、`ui/main_window.py`（`_on_preseg_run`、`_start_results_for_run` 附近）、`ui/step1_presegmentation/run_job.py`（只跑子集）、必要时 `ui/step1_presegmentation/results_panel.py`；测试 `tests/test_preseg_run.py`、`tests/test_step1_preseg_run_ui.py`。

---

## 3. PA-3 Step1 Generate（生成 fused zarr）

**实测**：
- 合成图（30874×32430，融合 4 个通道：DAPI、CD163、CD4（Step0 已校正，float32）、CD3D）：网格 2×3，每块约 1.67 亿像素，平均每块 52 s，共约 5.2 min。主进程 RSS 在 2.7–9.4 GB 之间涨落 6 次，WSL 可用内存降到 0.1 GB，用了 4 GB 交换区。
- proj4（15437×16215，4 个通道）：网格 2×2，每块 15 s，共 60 s。

**根因**（`ui/step0/overview_panel.py` `FullFusionWorker`，`run()` :500–770，`_fuse_tile` :445）：
1. 每块所有通道一起读进内存并转成 float32（`_read_one_source` :346），`_channel_norm` 再为每个通道生成一份 float32 映射结果：每个通道约 8 字节/像素同时存在。
2. `apply_channel_remap`（`core/channel_remap.py:67–144`）对每个通道约 10 次整块运算，每次都生成新的整块临时数组。
3. 输出时 `(cyto*65535).astype` 和 `np.stack` 又各生成一份整块数组。
4. 块与块之间完全串行：读、算、写不重叠，写入单线程。每块还调用两次 `gc.collect()`。

**方案**（保证结果逐位不变）：
- **G1 按 zarr 分块对齐的计算单元**：计算不再按用户选的网格块整块进行，而是按输出 zarr 的分块网格（以**区域原点**为起点、1024×1024）切成计算单元，例如 1024 行 × 若干个 1024 列。每个单元独立完成"读 → 映射 → 融合 → 量化 →（多边形 ROI 时按单元生成多边形掩膜）→ 写入"。映射窗口是 Save 时冻结的全局窗口，与分块无关；现有测试 `test_the_tile_grid_does_not_change_the_result` 已保证分块不改变结果。用户选的网格只用于进度显示。内存从"块面积 × 约 40 字节"降到"单元面积 × 约 40 字节 × 并行数"，预计主进程峰值降到 1–2 GB 级别；多边形掩膜也不再整区域生成（codex 意见 1、7）。
- **G2 单元并行，单一写入者**（独立审核）：
  - **全局只有一个并发预算**：单元级线程池负责"读 + 映射 + 融合"，单元内各通道串行读取，去掉现在每块内部的 8 线程读取池（`MAX_IO_WORKERS`，:268、:656），不允许"单元池 × 通道池"的嵌套。并行数按 CPU 和可用内存取，和 CS P3 的规则相同。
  - **写盘只有一个线程**：计算结果进入有上限的结果队列，由唯一的写入者按单元写入 zarr。不依赖 zarr 目录存储多线程写的安全性；取消和发布也更简单。内存上限 = 计算线程数 × 单元大小 + 队列上限 × 单元输出大小。
  - 单元边界在两个方向上都与区域相对的 zarr 分块对齐（codex 意见 1），一个单元只对应自己的分块。
  - 取消、出错或发布之前，先停止计算线程、排空队列。
- **G3 减少临时数组（有停止规则）**：先做 G1，测内存；再做 G2，测速度和内存。只有当实测仍显示 remap 的临时数组是明显的问题时才做 G3；如果 G1+G2 后合成图上主进程峰值 < 3 GB、Generate 速度可以接受，就**不做 G3**，不碰被多处共用的 `core/channel_remap.py`（独立审核）。G3 的内容：`apply_channel_remap` 保留自己拥有的工作缓冲区（现在 :99 处的那次复制保留），之后的运算在这个缓冲区里原地完成（`out=`），不改调用方传入的数组（codex 意见 5）；去掉 `_channel_norm` 里重复的 `astype(float32)`；输出直接写进预先分配的 uint16 缓冲区；去掉每块两次的 `gc.collect()`。运算顺序和精度不变。
- **一致性测试**：`apply_channel_remap` 新旧实现逐位相等，覆盖 gamma≠1、截断、非有限值、退化窗口（min=max）、uint8/uint16/float32 输入，以及 CuPy 调用方（如有 GPU）；整条 Generate 在小合成数据上新旧输出逐位相等，包括矩形和多边形 ROI、网格 1×1 和 2×3。
- **不做**：uint8 查表加速（可能有最后一位的差异）；按通道缓存映射结果（改动大）。这两项列为以后的选项。
- **验收门槛**：在合成图和 proj4 上，新旧代码生成的 fused zarr 逐像素完全相等；记录耗时和主进程峰值（目标：合成图峰值 < 3 GB，耗时明显缩短；具体数字以实测为准）。
- **取消网格选择对话框**（独立审核裁定）：改为按 1024 分块自动切单元后，用户选的 2×3、4×4 不再影响内存、算法和实际计算，继续询问就是一个假的控制。按下 Generate 后直接开始，进度对话框照常可以取消。`TileSelectDialog` 类保留（`step0_page.py:62` 还导入它），`main_window.py` 不再弹出它。`fusion_meta.json` 里的 `grid` 和 `avg_tile_s` 目前没有任何读取方，改为如实记录计算方式：`compute_unit`（单元尺寸）、`n_units`、`elapsed_s`。

白名单：`ui/step0/overview_panel.py`（`FullFusionWorker`）、`ui/main_window.py`（`_save` 里不再弹出网格对话框，:9885–9905）、`core/channel_remap.py`（只做原地运算，函数签名和结果不变）、`core/fusion_engine.py`（如需原地运算）、`core/resource_tiers.py`（如需加单元内存常量）；测试 `tests/test_step1_fusion_core.py`、`tests/test_step1_fusion_settings_commit.py`（:673、:770 替换了网格对话框，需随之调整）、`tests/test_b3_fixes.py`（按块的进度），新增 `tests/test_v16_pa_fusion_parity.py`、`tests/test_v16_pa_remap_parity.py`。

---

## 4. PA-4 Step2 导出时的整块内存

**实测**（A5，合成图，Cellpose 全细胞融合）：
- **主进程峰值 9.4 GB**：出现在 17:45:30–17:47:00 这段时间，即所有图块分割完之后的导出阶段（profile：`export_mask_ome_tiff` 33.8 s，`export_ome_tiff` 9.2 s）。
- **合计峰值 14 GB**（17:32）：主进程 5.2 GB + 分割子进程 8.8 GB，出现在 Cellpose 推理过程中。

**根因**：`workers/segment_merge_worker.py` 的 :3157、:4095（cell mask）和 :3207、:4142（nuclei mask）调用 `tif.write(mmap_ro.astype(np.float32), tile=(512,512), ...)`，把整幅 mask 转成 float32 放进内存，合成图上约 4.0 GB。:3174、:4110 调用 `np.array(dapi_mmap_ro)`，整幅 DAPI 进内存，约 2.0 GB。

**方案**：
- 改用 tifffile 的图块迭代器写法：`tif.write(<按行优先逐个产生 512×512 float32 图块的生成器>, shape=(H, W), dtype=float32, tile=(512, 512), compression='lzw', photometric='minisblack', metadata=None)`。每次只转换一个图块。tifffile 写出的图块顺序、压缩方式都相同，**输出文件预期逐字节相同**；以 cmp 比对作为门槛。本机 tifffile 版本为 2025.5.10，支持这种写法；已在 1500×1300 的随机 mask 上验证（边缘图块补零），两种写法输出逐字节相同。
- 预计主进程导出峰值从 9.4 GB 降到约主进程平时的水平（约 4.5 GB）。

**不在本块内**：
- 子进程的 8.8 GB 来自 Cellpose 引擎本身，属于引擎内部，不改，只记录。
- 接缝候选整区读回（`core/seam_merge.py:428–446`），实测只用 20 s，不是峰值来源，只记录。

**顺带发现，请裁定**：每个分割结果文件夹里留着 `global_mask_<区域>.dat`（合成图上 4.0 GB）和 `global_dapi_<区域>.dat`（2.0 GB）。分割结束后它们仍在，`ui/batch_step4_dialog.py:77` 等处还会读取 `global_mask.dat`。删掉可以每次省约 6 GB 磁盘，但属于行为改变，需要先确认所有读取方都能改读 zarr 或 OME-TIFF。**已裁定：本块只记录，不删除**（属于数据产品约定的修改，不混进导出内存优化）。

白名单：`workers/segment_merge_worker.py`（只改上面 6 处导出调用，加一个图块生成器函数）；新增测试 `tests/test_v16_pa_ome_export.py`：小图上新旧写法输出逐字节相同，覆盖 dtype、元数据参数和边缘图块补零（codex 意见 7）。验收门槛还包括：在合成图的真实分割结果上，新旧写法输出逐字节相同。

---

## 5. PA-5 合成图上切换 step 不流畅

**状态**：只读代码找出的嫌疑点，**尚未计时**。程序已有性能记录工具：`BLOCK01_PERF=1 BLOCK01_PERF_LOG=<文件>`，内含 5 ms GUI 心跳，界面卡住会记为 `gui.gap`。

**嫌疑点**（按预计影响排序）：
1. `_rm_save_view`（`main_window.py:6958`）：**每次**切换都执行，读写 session.json 3–4 次，并重建 Step0 和 Step1 的草稿内容（随 29 个通道增长），即使什么都没变。
2. `_stop_all_loaders`（:6935）：离开 Step1 时对每个仍在运行的 patch 加载线程 `wait(3000)`，最长让界面卡 3 s。
3. 进入 Step3：`_step3_refresh_masks` 每次都扫描所有工作区的所有运行（读 JSON），并重新打开 mask zarr 和金字塔，即使选择没变。
4. 首次进入 Step1 / Step3：两个视图各自打开一遍整套切片和 GPU 资源，不共享。
5. 再次进入 Step1 / Step3：`sync_source` 会对每个已校正通道读 zarr 属性来确认数据没变。
6. 回到 Step0：29 行逐行刷新，再对所有 patch × 29 个通道发起预加载请求。
7. 离开 Step1 时关掉拼图供应，回来时重新合成。
8. 进入 Step2、Step4 时每次重新列出运行、重新解析定量任务。

**方案（分两步）**：
- **5a 实测**：加临时计时点，覆盖 `_rm_save_view`、`_stop_all_loaders`、`_step3_refresh_masks`、`_rm_bind_step2`、`_step4.set_run`、`_close_montage_supply`，其余已有。请你在合成图上按固定顺序切换一轮（0→1→2→3→4→3→1→0，每步停 5 s），我读 `gui.gap` 和各段耗时，列出实测的前几名，报给你。
- **5b 修复（有边界）**：如果排在第一的热点属于视图打开、`sync_source`、GPU 资源或图块读取，**PA 不修，直接交给 A9**（那是 A9 的职责）。PA-5b 只处理：重复的 JSON 保存、重复的运行解析、没变化却重建、明显多余的流程调度。`_stop_all_loaders` 的 `wait(3000)` 只有在完成独立的线程生命周期和取消审查之后才改（独立审核）。只改实测排在前面的几项。预计的做法（不新增哈希）：
  - 内容和上次写入的相同（直接比较字典）就不写，并把几次读写合成一次；
  - 停加载线程时不等待（需另做线程生命周期和取消的审查，codex 意见 7）；
  - 选择没变就不重新解析 mask；
  - 同一个运行不重新解析定量任务。
  - 修复范围和目标数字（例如每次切换 GUI 卡顿不超过多少毫秒）**在 5a 结果出来后请你裁定**。

白名单（5a）：`ui/main_window.py`（只加 `perf_trace.span`，默认关闭时无开销）。5b 的白名单随实测结果另报。

---

## 6. 顺序、回归与验收

1. 顺序：PA-1（bug，小）→ PA-4（小，逐字节门槛）→ PA-3（G1 → 测 → G2 → 测 → 按停止规则决定 G3）→ PA-2 → PA-5a 实测 → PA-5b（范围由你按实测裁定）。每一项各自走"codex 低档代码审核 → 定向回归 → 你验收"；可以按项分别提交。
2. 定向回归：各项相关的测试文件，加上 A8 的定向清单；与基线比较。块结束时做一次全量回归（本块属于里程碑前的收尾）。
3. 资源复测：PA-3、PA-4 完成后，在合成图上用 `scripts/measure_a5.py` 重测 Step1 Generate 和 Step2 导出段，结果写进 A5 清单 §4。
4. 增长报告：块结束时报告 `step0_page.py` 和 `main_window.py` 的行数变化。

## 7. 裁定（独立审核，2026-10-05）

| 项目 | 裁定 |
|---|---|
| PA-1 显示通知 | 批准 |
| `visible_marker` | 一并修 |
| PA-2 沿用 | 补"只删除也生成只复制的运行"和 `reused_from_*` 后批准 |
| PA-2 mask | 复制，不跨运行引用 |
| PA-2 查找范围 | 只看当前 / 上一次运行 |
| PA-3 1024 分块单元 | 批准 |
| PA-3 并行 | 全局一个线程预算，禁止嵌套线程池；单一写入者 |
| PA-3 G3 | G1、G2 之后仍有必要才做 |
| 网格对话框 | 不再让用户选择 |
| PA-4 流式 TIFF | 批准 |
| 6 GB `.dat` | 本块只记录 |
| PA-5a 实测 | 批准 |
| PA-5b | 数据出来后再裁定；视图类热点交给 A9 |

## 8. 审核记录

- 2026-10-05 codex（gpt-6-astra low）审核 v1：结论 approve with changes。7 条意见都已并入 v1.1：
  1. 高：PA-3 条带必须按区域相对的 zarr 分块在两个方向上对齐，否则并行写会写到同一分块 → §3 G1/G2 改为按分块网格切计算单元。
  2. PA-2 比较加引擎身份 → §2。
  3. PA-2 跨运行引用 mask 有删除和移动的风险 → 推荐改为复制。
  4. PA-2 沿用的记录要在执行前放进作业和界面，结算不碰它们 → §2。
  5. PA-3 remap 保留自有缓冲区，不改调用方数组；测试覆盖面 → §3 G3。
  6. PA-1 只对"写的不是屏幕上的步骤"的情况不发通知 → §1。
  7. 多边形掩膜按单元生成；OME 测试覆盖 dtype、元数据和补零；"不等待"需另审；测试文件名列入白名单。
- 未提出新增哈希。
- 2026-10-05 独立审核 v1.1：有条件批准，修订为 v1.2 后实施。三处必改：PA-2 只删除时的语义和沿用来源字段；PA-3 去掉嵌套线程和并发写 zarr 的假设；PA-5 不侵入 A9 的视图优化。全部并入 v1.2（§1、§2、§3、§4、§5、§7）。已核对：`seg_runner/engines.py:65` `behavior_identity` 存在且是纯函数；`overview_panel.py:268` `MAX_IO_WORKERS = 8`、:656 每块内部线程池；`fusion_meta.json` 的 `grid`、`avg_tile_s`（`overview_panel.py:759–760`）在代码中没有读取方。
