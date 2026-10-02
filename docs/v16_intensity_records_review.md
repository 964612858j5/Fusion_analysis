# Intensity 记录方式：只读审核报告（2026-10-02）

状态：**只读审核；用户 2026-10-02 已裁定 Q1–Q4（见 §9），白名单按裁定收窄（§6）**。没有改任何代码。调查基于 HEAD `38d73cb`。

起因：真机验收 G1 时，取消 fusion 后不能再 Save，弹出「The chosen result can no longer be used: the image or the Fusion settings changed since this run」。追查发现，原因是 Step1 的「Generate fused.zarr」会把 Step1 的 Intensity 写回、覆盖 Step0 的记录。用户的原则：**每一步只记录自己结束时的状态；Step1 不应写回覆盖 Step0；Intensity 的实时状态应该另有一个全局记录**。

---

## 1. 现在 Intensity 存在哪里（三个地方）

| # | 地方 | 是什么 | 什么时候写 |
|---|---|---|---|
| ① | **内存里的「当前 Intensity」** | 所有页面共用的一份：Step0 的 Intensity 窗口、Step1 的显示、Step2 / Step3 的显示，都读写它。程序关掉就没了 | 你拖任何一个滑块，都会立刻改它 |
| ② | **Step0 的 Intensity 文件** `<工作区>/step0/step0_channel_remap.json`（另有按内容哈希命名的版本，例如 `step0_channel_remap.98f92c….json`），由 Step0 交接 `step0_roi_result.json` 用哈希指着 | Step0 交给后面各步的记录 | (a) Step0 的 Save；(b) **Step1 的 Generate fused.zarr（问题所在）** |
| ③ | **Step1 的 fusion 设置** `<工作区>/step1/step1_fusion_settings.json` | Step1 自己的记录：fusion 配置 + 各通道窗口 + 自己的哈希 | Step1 的 Save Fusion Settings |

**关键点**：① 其实就是你说的「全局实时状态」，它已经存在，只是**没有自己的文件**。程序把 ② 当作它的存档：重新打开时，① 是从 ② 恢复的。

## 2. 问题在哪里（用你今晚的操作举例）

1. 10 月 1 日：你在 Step0 只调过 DAPI（max 175），然后 Step0 Save → ② 里只有 DAPI 175。
2. 今晚：你在 Step1 把 DAPI 调到 208，加了 CCR7、CD4、CD15，点 Save Fusion Settings → ③ 记下这 4 个通道（DAPI 208），哈希 `83a559…`。**② 不变**，这是对的。
3. 你跑了预分割，点 Use。预分割记下了「Step0 交接的身份」，其中包含 ② 的哈希（`98f92c…`），也记下了 ③ 的哈希（`83a559…`）。
4. 你点 Generate fused.zarr：
   - 先检查预分割结果还对不对：对得上，通过；
   - **然后把 ① 里这 4 个通道的窗口写进 ②**，DAPI 175 被覆盖成 208，再重新发布 Step0 交接，② 的哈希变成 `a6329a…`；
   - 接着开始 fusion。fusion 用的是 ③ 的数字，并不读 ②。
5. 你取消，再 Generate：检查预分割结果，发现「Step0 交接的身份」变了（因为 ② 的哈希变了），判为过期，拒绝。

所以：fused.zarr 的生成本身和 Step0 无关（第 4 步里 fusion 只用 ③）。写回 ② 是**多余的**，而且产生了三个副作用：
- **覆盖了 Step0 的记录**：② 里的 DAPI 不再是 Step0 结束时的 175；
- **误判过期**：每次 Generate 都会重新发布 Step0 交接，绑定了交接身份的东西（这次是预分割结果）就被判成「图像变了」；
- **重新打开时的显示依赖它**：重开工作区时，① 是从 ② 恢复的。正因为 Generate 把 Step1 的窗口写进了 ②，重开后 Step1 才看到自己的窗口。去掉写回，这一点要另外处理（见第 4 节 Q2）。

## 3. 这条写回是怎么来的（git 历史）

- **9 月 8 日** `933deec`：当时 Step1 没有自己的 Intensity 记录，磁盘上只有 ②。为了让 fused.zarr 能说清楚「用的是哪套窗口」，Generate 之前先把窗口写进 ②，再发布交接。
- **9 月 9 日** `5e9031b`、`11307b9`：Step1 有了自己的记录 ③，Generate 改为用 ③ 的窗口。**从这时起写回就多余了，但没有删**。
- 之后有三处依赖了它（所以去掉写回要一起处理）：
  - 9 月 9 日 `3a60e5b` / `5c4b441`：③ 绑定了「Step0 交接的身份」（含 ② 的哈希）。为此加了一个补救动作：写回 ② 之后，把 ③ 改认到新的交接上（`_rebind_fusion_settings_to_handoff`）；
  - 9 月 24 日 `3f179ca`：预分割结果的身份里加入了 ② 的哈希；
  - 10 月 2 日 `ffbfe12`（A6 W1）：打开工作区时，① 从 ② 恢复。

## 4. 建议的做法和需要你裁定的问题

**做法（一句话）**：Generate 不再碰 Step0。Step0 的 ② 只由 Step0 Save 写；Step1 的 ③ 只由 Step1 写；凡是需要「Step1 用了哪套窗口」的地方，都读 ③。

需要你裁定：

**Q1. 全局实时 Intensity 要不要有自己的文件？**
- (a) 本块**不加**。① 仍只在内存里。重开时，Step0 页从 ② 恢复，Step1 页从 ③ 恢复（见 Q2）。最小改动。
- (b) **加**一个 `<工作区>/intensity_live.json`：每次拖完滑块（松手时）写一次，重开时 ① 从它恢复。这样「当前状态」和「各步结束时的状态」彻底分开。这是一个新机制，要动共享显示状态（`ui/block01_display.py`），和 A7、A8 有交叉。
- **建议 (a)**，把 (b) 放到 A8（项目状态的唯一持有者）一起设计。理由：(b) 牵涉「什么时候写、写失败怎么办、几个窗口同时改怎么办」，单独做容易变成补丁。

**Q2. 重新打开工作区、进入 Step1 时，Step1 显示哪套窗口？**
- 场景：今晚你在 Step1 把 DAPI 调到 208 并 Save Fusion Settings。明天重开，进 Step1。
  - 去掉写回、又不处理的话：① 从 ② 恢复，DAPI 是 175。Step1 会显示「Unsaved fusion changes」，因为屏幕上是 175，③ 里是 208。
  - 建议：进入 Step1、且 ③ 被采用之后，把 ③ 的窗口装回 ①（用现成的 `adopt_mappings`）。这时 Step1 显示 208，和你上次离开 Step1 时一样；回 Step0 看到的也是 208（① 只有一份）。Step0 Save 时，② 才会变成 208。
- 你要定的是：回到 Step0 时应该看到哪套窗口？
  - (a) 看到 ① 的当前值（208）。只有一份当前状态，最简单；
  - (b) 回 Step0 时切回 Step0 自己的 175。这需要 ① 按步分开，等于 Q1(b) 的一部分。
- **建议 (a)**。

**Q3. 预分割结果的身份里，要不要保留 ② 的哈希？**
- 去掉写回之后，② 只在 Step0 Save 时变化。
- 场景：你回 Step0 改了 DAPI 的窗口，然后 Step0 Save，② 变了。此时之前在 Step1 跑的预分割结果，要不要判过期？
  - 预分割用的是 ③ 的窗口，不是 ②。只要 ③ 没变，像素就没变。但 Step0 Save 也可能改了校正（TopHat / cuCIM 参数），那个变化会体现在身份的「校正产品」部分，不靠 ② 的哈希。
- (a) **去掉** ② 的哈希：预分割只在「校正产品、区域、③」变了时才过期。要改一条测试（`test_preseg_run.py:174`）。旧的预分割结果会全部失效一次（身份格式变了）。
- (b) 保留：Step0 Save 改了 Intensity，Step1 的预分割结果也会过期，哪怕 ③ 没变。
- **建议 (a)**，它符合「每一步只看它真正依赖的东西」。

**Q4. Step2 的 HQ2 / CSD 方法读的是 ②。** 去掉写回后，它读到的是 Step0 Save 时的窗口，不再是 Step1 的。这几种方法已经不再维护，而且 ROI 工程里直接不生效。**建议不改，只记录。**

## 5. 改了之后，用户能看到的变化（场景）

| 场景 | 现在 | 改了之后 |
|---|---|---|
| Generate 之后取消，再 Generate | 被拒：「the image or the Fusion settings changed」 | 能直接再 Generate |
| Generate 完成后，再 Generate 一次 | 同样会被拒（同一个原因） | 正常（设置没变时直接复用已有结果） |
| Step1 调了 DAPI 并 Generate，回 Step0 看 `step0_channel_remap.json` | 已经被改成 Step1 的值 | 仍是 Step0 Save 时的值 |
| 在 Step1 调了窗口，再回 Step0 点 Save | 显示「No changes」（因为 Generate 已经把 ② 改成一样的） | Step0 会把当前窗口作为 Step0 的新状态保存。这是你在 Step0 主动点 Save 的结果 |
| 重开工作区进入 Step1 | 显示上次 Generate 写回的窗口 | 显示 ③ 里 Step1 自己的窗口（Q2） |

## 6. 白名单草稿（按 Q1(a)、Q2(a)、Q3(a)、Q4 不改）

**`ui/main_window.py`**，只改：
- `_save`：删除「写回 Step0 + 改认交接」那一段（约 9731–9757 行）；
- 删除 `_commit_display_mapping_for_save`、`_rebind_fusion_settings_to_handoff`、`_on_display_mapping_committed` 及其信号连接；
- 三处「复用身份」改为读 ③ 的窗口，不再读 ②：`_expected_dapi_input_meta`（DAPI 的 remap 哈希）、`_display_mapping_identity`（去掉 ② 的文件路径）、`_expected_meta_for_current_config`（恢复会话时的比对）。三处构造保持一致；
- `_restore_fusion_settings` 及进入 Step1 的接线：③ 被采用后，把它的窗口装回 ①（Q2）；
- ~~③ 绑定的「交接身份」去掉 ② 的哈希~~：**不做**（Q3 裁定，留给数据版本管理）。③ 继续绑定 Step0 交接的身份；去掉写回后，交接只在 Step0 Save 时变化。

**`ui/step0/step0_page.py`**，只改：
- 删除 `commit_display_mapping`、信号 `display_mapping_committed`，以及只被它用到的 `_mapping_file_holds`、`_write_mapping_file`；
- `_handoff_spec` / `_write_step0_handoff` 的 `remap_config_path` 参数不再有调用者，去掉。

**`core/preseg_run.py`**：**不改**（Q3 用户裁定：保留哈希，改由「数据版本管理」处理，见 §9）。

**测试**：
- 删除专门守写回的测试：
  - `test_step1_display_mapping_commit.py` 中 commit 相关的 7 条；
  - `test_step1_fusion_settings_commit.py` 中 rebind 相关的 3 条。
- 改打桩点（窗口来源改为 ③）：
  - `test_dapi_input_remap_cache.py`；
  - `test_step1_fusion_reuse_identity.py`；
  - （`test_preseg_run.py:174` 不改，Q3）。
- 新增：
  - Generate 不改 ②、不重新发布交接；
  - Generate 后取消，再 Generate 能通过；
  - 重开后 Step1 显示 ③ 的窗口；
  - Step1 Save Fusion Settings 不碰 ②。
  - 每条都先在改动前的代码上变红。

**文档**：`UI_SURFACE_RULES.md`（如有可见变化）、两份用户指南、执行记录。

**不碰**：
- `core/step0_handoff.py`：Step0 Save 自己写 ② 的那部分；
- Step0 的 `_persist_step0_remap_config`、`_emit_complete`；
- 权威读取器 `_load_step0_roi_result` 对 ② 的哈希校验：Step0 交接本身要自洽，这一点保留；
- `workers/segment_merge_worker.py`、`core/provenance.py`、`ui/step2_page.py`、`ui/block01_display.py`（Q1(a)）。

## 7. 风险

| 风险 | 对策 |
|---|---|
| 预分割身份格式变了，已有的预分割结果全部失效一次 | 只影响「Use」的选择；重新 Run 即可。执行记录里写明 |
| 已有 fused.zarr 的复用身份变了（不再含 ② 的路径），第一次 Generate 会重新生成一次 | 只发生一次；执行记录里写明 |
| Step1 拖了窗口之后，回 Step0 点 Save，会把 Step1 的值存成 Step0 的状态 | 这是 Q2(a)「只有一份当前状态」的直接结果；要避免，就需要 Q1(b) |

## 8. 估计

约 1 天：代码半天，测试和反向注入半天。真机复验 G1 加上本块的场景约 30 分钟。

## 9. 用户裁定（2026-10-02 晚）

- **Q1：(a)**。本块不加全局 Intensity 文件；放到 A8 一起设计。
- **Q2：(a)**。只有一份当前 Intensity。重开工作区进入 Step1、③ 被采用后，把 ③ 的窗口装回当前 Intensity；回 Step0 看到的也是这套当前值。
- **Q3：哈希不去掉**。用户的判断：这是 session / 数据版本 / 分割版本的管理问题，要升级为「同一个 session 下的数据版本管理」（另立一块，见下）。本块不动预分割的身份，也不动 ③ 对 Step0 交接身份的绑定。
- **Q4：不改**。

### 用户提出的「数据版本管理」（另立一块，先写申请；本块不做）

用户原话要点：
- 每个 session（今天的工作区）只对应一个 OME-TIFF，以创建时间戳区分。
- 一个 session 内会有多个**数据版本**：背景校正、Intensity、用于融合的通道及其权重。场景：用户手动调一次参数做一次分割，然后再调、再分割，反复好几次。
- 每个数据版本对应一个或多个**分割版本**。
- 数据版本变化**不会让已有的分割版本变化**。新的数据版本不作废旧的分割，旧分割仍属于它自己的数据版本。
- 所有数据版本都可以**加载回 Step0 和 Step1 重新浏览**。
- 所有分割版本都可以在 viewer 里叠加在 OME-TIFF 上查看。Step3 右上角运行下拉框的每条记录，最右侧标出对应数据版本的 label 或 id，右对齐，可以盖住记录名字的右端（与 B9 的「provenance unknown」同一种显示方式）。

## 10. 执行记录（2026-10-02 晚，按 §9 的裁定实施「去掉写回」）

**改动**
- `ui/main_window.py`：
  - `_save` 不再写回 Step0、不再改认交接，原来那一段换成一个检查：Step1 已保存的设置里，每个有权重的通道都必须有窗口，缺了就点名拒绝。这道检查原来由写回顺带完成，现在单独保留；
  - 删除 `_commit_display_mapping_for_save`、`_rebind_fusion_settings_to_handoff`、`_on_display_mapping_committed` 及其信号连接；
  - 三处复用身份改为统一读 `_fusion_display_mapping()`。来源依次是：本次运行的配置 → Step1 已保存的设置 → 当前草稿；
  - `_display_mapping_identity` 去掉了 Step0 文件的路径，只保留窗口数值的哈希；
  - `_restore_fusion_settings` 采用 Step1 的设置之后，调用 Step0 页的 `adopt_intensity`，把这些窗口装回当前 Intensity（Q2a）。
- `ui/step0/step0_page.py`：
  - 删除 `commit_display_mapping`、`display_mapping_committed` 信号，以及只被它用到的 `_file_bytes`、`_restore_file`、`_mapping_file_holds`、`_write_mapping_file`；
  - 新增 `adopt_intensity`：只改内存里的当前 Intensity，不写盘；复用 W1 的 `_apply_workspace_intensity`。
- **没有动**：`core/preseg_run.py`（Q3）；③ 对 Step0 交接身份的绑定（Q3）；`ui/step2_page.py`（Q4）；Step0 Save 自己写 ② 的那部分；`_load_step0_roi_result` 对 ② 的哈希校验。

**测试**
- 删除只守写回的 8 条：`test_step1_display_mapping_commit.py` 中 6 条，`test_step1_fusion_settings_commit.py` 中 2 条（rebind 写不进去、republish 没变化）。
- 改写 3 条：
  - 「草稿变动不是一次 commit」→「草稿变动不写 Step0」；
  - 「有权重但没有窗口的通道阻止 Save」→ 改为按 Step1 已保存的设置检查；
  - 「Save 重新发布后设置仍可恢复」→「Generate 不动 Step0 交接，设置不用 rebind 就能恢复」。这一条要求两次 Generate 都走过原来写回的位置，之后 Step0 的文件逐字节不变、预分割的身份不变、设置仍可恢复。
- 新增 2 条：Save Fusion Settings 不写 Step0；恢复设置时，把窗口装回当前 Intensity。
- 改桩点：`test_dapi_input_remap_cache.py`（窗口来源改为 Step1 设置）；`test_step1_fusion_settings_commit.py` 的一个桩补上了 DAPI 窗口（真实的设置里每个通道都有窗口）。
- **反向注入**（改动前的代码 `38d73cb`）：
  - 「Generate 不动 Step0」那条变红：旧代码的 Generate 依赖写回 Step0 成功，在这个测试环境里根本走不到 fusion；
  - 「恢复时装回窗口」变红；「按 Step1 设置检查窗口」变红；
  - 另外 2 条守护性测试（Save Fusion Settings 不写 Step0、草稿变动不写 Step0）在新旧代码上都通过，符合预期。
- **针对性回归**（逐个模块单独跑，按用户要求不跑全量）：以下 24 个模块全部通过：
  - Step1 显示映射与设置：`test_step1_display_mapping_commit`、`test_step1_fusion_settings_commit`、`test_dapi_input_remap_cache`、`test_step1_fusion_reuse_identity`、`test_step1_viewer_binding`、`test_step0_display_mapping`、`test_block01_shared_intensity_lifecycle`、`test_downstream_display_seed`；
  - 交接与数据集切换：`test_step0_step1_handoff_contract`、`test_step1_handoff_invalidation`、`test_step1_dataset_switch`、`test_step0_dataset_switch`、`test_step0_authoritative_save_barrier`、`test_step0_background_correction_outputs`；
  - fusion 与 A6：`test_step1_save_progress`、`test_step1_fusion_isolation`、`test_step1_result_publication`、`test_v16_a6_workspace`、`test_v16_a6_async`；
  - 其他：`test_step2_remap_integration`、`test_ui_surface_contract`；
  - 预分割（真引擎）：`test_preseg_run`、`test_step1_preseg_run_ui`。
  - `test_step0_channel_conditioning` 没有跑：上一次全量回归里它在新旧两边都超过 10 分钟。
- **真机探针**：在验收副本 `…_48e7` 上（先备份，跑完逐字节还原，验证无误）：重开 → 进 Step1 → 恢复 Step1 的设置。DAPI 的窗口由 175 变为 Step1 保存的 208，CCR7 为 110，29 个通道的窗口与设置完全一致。

**发现（没有改，记录）**：进入 Step1 时，并不会自动恢复 Step1 上次的会话和 fusion 设置，要用户点「Load Previous Step1 Session」。今晚真机上要先点 Save Fusion Settings 才能跑预分割，就是这个原因。打开工作区时是否应该自动恢复 Step1，留给「数据版本管理」一起考虑。
