# v16 块 RM — 用「运行链」替代「数据版本」：数据模型申请 v1.4

日期：2026-10-03。分支 `v16`，HEAD `9be3386`。状态：**v1.4，用户 2026-10-03 批准 §16 并批准 RM-1 开工**。

修订记录：
- v1.4：用户批准 §16（白名单追加、16.3 十条、16.4 DV 入口停用、16.5 阶段边界）。§4 改用现有文件名，交接文件放 `settings/step0/`；§13 RM-1 搬迁验收改为 §16.3-6 的口径。
- v0：Run 模型草稿。
- v1：按 codex 审核 8 条修订。用户裁定：沿用 `.trash`；**Step0–4 每一步都依赖前一步的结果，不同 step 的 run 彼此不独立**（§3）；corrected 不得在 Generate 时现算。
- v1.1：用户裁定 **不起名**（显示名 = 时间 + 参数摘要）；**不做旧布局兼容、不做导入**。
- v1.3：按独立审核修正两处操作语义：Step2 Run 用当前实际生效配置，不静默重置为 Step1 最新参数（§5）；Step1 是**一个按钮** `Save Config & Generate fused.zarr`，文档不再拆成两步（§3、§5）；删 preseg 后 fuse 的来源详情显示「已删除」（§9）。
- v1.2：按独立审核 4 条流程缺口 + 2 项边界说明修订：预分割的来源是 correct run（§3）；保留「已保存配置」这一层（§2、§5、§6）；草稿带编辑上下文（§6）；删除限制含预分割、预分割落盘归 RM-1（§9、§13）；部分删除后的恢复（§9）；不可变边界只含科学产物（§2）。

背景：DV / DV-D 已连续 9 轮 codex 复核修复（规则 7：两轮不收敛即停）。本申请把数据模型换成同类产品共有的「运行 + 引用」模型，删机制是结果不是目标。

---

## 1. 一句话

**现场记最后一刻；已保存配置记用户点 Save 的那一刻；运行记执行的一刻；不可变产物靠引用连成链；删除、复用、恢复都沿链走。**

## 2. 原则

1. 唯一不可变的输入是原始 OME-TIFF（在 project 外，只记路径和 `slide_id`）。
2. **run = 一次按钮点击产出的文件夹 + 当时的参数 + 它用的上游 run。** 写完 `.done` 后，**科学产物不再改**：像素、label store、执行参数、输入引用、冻结几何。显示派生文件（mask 金字塔、coarse 侧车之类）按今天的既有规则按需补建，不属于不可变边界，**不改 viewer**。没有 current / draft / committed / legacy 状态。
3. 参数有三层，含义不同，都保留：
   - **现场**（`session.json`）：正在编辑、还没点 Save 的草稿。
   - **已保存配置**（`settings/step0.json`、`settings/step1.json`）：用户最后一次点 Step0 Save / Save Fusion Settings 写下的；今天的 `correction_config` + remap 与 `step1_fusion_settings.json` 的语义原样保留。
   - **执行快照**（run 的 `params.json`）：那次运行实际用的。
   draft / committed 不再作为「版本状态」暴露给用户，但这三层语义不删。
4. 每个 run 用 `inputs.json` 指明它的**像素上游**。参数的来源（例如 Generate 采用了哪次预分割的结果）写在 `params.json` 里。
5. 记录只写 **project 根相对路径**。project 文件夹整体可拷贝、可改名。
6. 列表 = 扫描 `runs/`；「上次正在看哪个、正在编辑什么」= `session.json`。
7. 删除一个 run = 删它和链上所有下游，先下游后上游；移动手段沿用 `.trash`。

## 3. 依赖链（用户要求，本模型的核心约束）

```
raw OME-TIFF ──> correct run ──┬──> preseg run      (Step1 预分割：在 patch 上用已保存融合配置算)
 (Step0 Save)                  │
                               └──> fuse run ──> segment run ──> quant run
                              (Step1 Generate)   (Step2 Run)    (Step4 Extract)
```

- 每个 run **恰有一个像素上游**（correct run 的上游是 raw）。`inputs.json` 只写这一个；再往上顺着文件走。
- 现有操作顺序不变：**Save Fusion Settings → 跑 patch 预分割 → Use → `Save Config & Generate fused.zarr`（一个按钮：先保存分割参数，再复用或生成 fuse run）**。预分割的像素来源是 correct run，它的 `params.json` 冻结融合配置、patch、方法；Generate 的 `params.json` 另记 `preseg_run`（采用了哪次预分割的参数）和分割参数文件内容。这两种关系分别表达，不混成一个「上游」。
- **选一个 run 就是选一整条链。** 在 Step3 选中 segment S，Step2 的输入显示为 S 的 fuse run，Step1 显示那个 fuse run，Step0 显示它的 correct run。
- Step N 的可选项只列当前上游链下的 run（Step2 列当前 correct run 下的 fuse run；Step3 列全部 segment run，选中即切换整条链）。
- 删除沿链向下：删 fuse run F 必须同时删 F 下的 segment、quant；删 correct run C 必须同时删 C 下的 preseg、fuse 及其下游。确认框列全。
- 复用判定沿链（§8）。
- 现场恢复沿链：`session.json` 记「正在看的最下游 run」和「正在编辑的上下文」，链由 `inputs.json` 还原（§6）。

## 4. 目录

```
<project>/
  project_manifest.json         slide_id、原图路径、创建时间（沿用现有文件名，§16.3-1）
  provenance/                   A3 不变（§10）
  .trash/                       沿用今天的回收站（§9）
  rois/<roi_id>/
    roi_manifest.json           工作区身份（沿用现有文件名；A3 靠它识别工作区）
    settings/step0/             Step0 交接：step0_roi_result.json、roi_config.json（当前可编辑几何）、
                                patch_config.*.json；可变，patch 编辑会重写（§16.3-2）。
                                每个 run 在 params.json 冻结自己那份几何（§7），之后的编辑不改它
    session.json                现场（§6）
    settings/step0/correction_config.json、step0_channel_remap.json   已保存的 Step0 配置（校正参数 + Intensity）
    settings/step1_fusion_settings.json   已保存的融合配置（Save Fusion Settings 写，沿用现有文件名）
    settings/segmentation_params/   今天的分割参数文件机制，不变
    runs/
      correct_<stamp>/          params.json · inputs.json · corrected_channels.zarr · corrected_coarse.zarr · .done
      preseg_<stamp>/           params.json · inputs.json · records/ masks/ · .done
      fuse_<stamp>/             params.json · inputs.json · fused_<区域>.zarr · .done
      segment_<stamp>_<method>/ params.json · inputs.json · masks / label store · .done
      quant_<stamp>/            params.json · inputs.json · h5ad / 表格 · .done
```

文中 `settings/step0.json` 是简称，指 `settings/step0/` 下的 `correction_config.json` 与 `step0_channel_remap.json`；`settings/step1.json` 指 `settings/step1_fusion_settings.json`（v1.4）。

所有路径相对 `<project>/`。三个约定文件：
- `params.json`：这次运行的全部参数 + 冻结的区域几何 + 算法版本 + 参数来源（fuse：`preseg_run`、分割参数文件内容；segment：分割参数文件内容）。各步今天已有的 json 原样放进来，不新造 schema。
- `inputs.json`：`{"upstream": "rois/…/runs/correct_…"}`；correct run 写 `{"upstream": "raw", "slide_id": "…"}`。
- `.done`：最后写。没有它的文件夹 = 不存在，启动时清理（今天已验证的「标记最后写」协议，唯一保留的状态）。

**correct run 只含校正像素及其逐通道参数**（方法、参数、original/corrected 决策、逐通道 `source_identity` 原样保留供 coarse 侧车配对）。**Intensity / remap 不属于 correct run**：它进 `settings/step0.json`，并在 preseg / fuse run 的 `params.json` 里冻结。

## 5. 每个按钮做什么

| 按钮 | 行为 |
|---|---|
| Step0 Save | 写 `settings/step0.json`。只有**校正像素会变**（任一通道的方法 / 参数 / 决策、ROI 几何、原图）时才新建 `correct_<stamp>/`：没变的通道从上游 correct run 复制，变了的重算（今天的增量逻辑，目标换成新文件夹）。只改 Intensity → 只写 settings，不新建 run，不复制像素。什么都没变 → 「No changes」。 |
| Step1 Save Fusion Settings | 写 `settings/step1.json`（今天 `step1_fusion_settings.json` 的语义）。不产生 run。预分割和 Generate 读的是这份，不是现场草稿（今天的规则不变）。 |
| Step1 预分割 Run | 新建 `preseg_<stamp>/`，上游 = 当前 correct run，`params.json` 冻结 `settings/step1.json` 的内容、patch、方法。 |
| Step1 `Save Config & Generate fused.zarr` | **保持现有单按钮**，一次点击两个阶段：① 要先 Use 一个预分割结果，把它的分割参数写入 `settings/segmentation_params/`（不产生 run）；② 按 §8 复用已有 fuse run，否则新建 `fuse_<stamp>/`，上游 = 当前 correct run，`params.json` 冻结 `settings/step1.json`、Intensity、几何、`preseg_run`、分割参数文件内容。①成功而②取消或失败时，不发布新的 fuse run，已有输出不变。 |
| Step2 Run | 「输入」下拉：当前 correct run 下的 fuse run，默认最新。不再有 dirty draft 禁用：现场改了没 Generate，照样可以跑已有 fuse run。分割参数：Step1 最新参数文件按现有规则作为 Step2 的**初始配置 / 参数来源**（用户 2026-10-02 裁定，fuse 里的副本只是记录）；用户在 Step2 改了方法、参数或换了参数文件后，Run 用的是 `get_seg_config()` 当前实际生效的配置，并完整冻结到 segment 的 `params.json`。**Run 时不得静默重置为 Step1 最新参数。** |
| Step3 下拉 | 扫描 `runs/segment_*`，每行显示名 + 它的上游 fuse run 显示名；选中 = 切换整条链的**显示**（§6）。行首 ×（§9）。 |
| Step4 | 从选中的 segment run 沿链找到 correct run，读它的 corrected 和逐通道决策；raw / corrected 语义与今天 `quant_sources` 相同。产物写成 `quant_<stamp>/`。 |
| Load 选择框 | 一行一个 ROI；选中后按 `session.json` 恢复现场。 |
| 显示名 | 不起名。显示名 = 「10-03 12:00 · 参数摘要」，由 `params.json` 现算。文件夹名不上界面。 |

## 6. 现场（审核第 1、2、3 条）

`session.json`（每个 ROI 一份）：
- `viewing`：正在看的最下游 run（Step3 选中的 segment 或 Step2 选中的 fuse）。只影响显示链。
- `editing`：正在编辑的上下文 = `{correct_run, geometry_revision}`。Step0 / Step1 的草稿和 `settings/` 都针对它。
- `drafts`：Step0 草稿、Step1 融合草稿、Step2 草稿（方法、参数文件选择）、Step4 草稿（输出设置），每份带 `edited_against`（它的 `editing` 上下文）。沿用今天 `_step1_session_payload` 的 `fusion_draft` / `last_save` 结构。
- `view`：当前步骤、viewer 相机、patch 选择、Step3 显示选项。

写入时机：每次 Save / Generate / Run 完成后、切步时、关闭时。

恢复：Load → 选 ROI → 读 `session.json` → 按 `viewing` 恢复显示链 → 按 `editing` 恢复编辑上下文和 `settings/` → 把 `edited_against` 等于 `editing` 的草稿装回界面 → 回到当前步骤。`edited_against` 不等于 `editing` 的草稿不装回、不丢弃，终端说明。

**查看历史不覆盖编辑**：Step3 选旧结果只改 `viewing`；`editing`、`settings/`、`drafts` 不动。回到 Step1 编辑时按 `editing` 恢复原上下文（审核第 3 条场景：在 C2 上编辑，看 C1 的 S1，关闭重开，回 Step1 仍是 C2 和它的草稿）。「用这次的参数」是显式动作：把 `editing` 切到那个 run 的链，并把它的 `params.json` 复制进 `settings/` 和草稿。

审核第 2 条场景：F1 用 0.5（F1 的 `params.json`），Save Fusion Settings 写 0.8（`settings/step1.json`），现场调到 0.9 没保存（`drafts`）。三份分开存，重开后三份都在。

## 7. 几何与原图

- `roi.json` 是当前可编辑几何。每个 run 的 `params.json` 冻结自己生成时的几何。改几何后 Save：按今天 W2 规则先提示，确认后新 correct run 冻结新几何，`editing.geometry_revision` 随之更新；历史 run 按自己的几何解释。
- 原图：`project.json` 记路径 + `slide_id`。打开时文件不在或 `slide_id` 不符 → 弹重新定位，不复制原图进 project。

## 8. 复用判定

两次运行「相同」比较且只比较：像素上游 run 路径、冻结几何、影响计算的参数、算法版本。不比较时间戳、显示状态。逐字段比较，不新造哈希。替换今天两套（`_try_reuse_fused_zarr` 的 hash + DV `same_content`）。

## 9. 删除（审核第 4 条 + 边界说明）

- 删任何 run：列出链上全部下游，确认框列全，一起进 `.trash` 或取消。
- 顺序：先下游后上游，任何一步失败立即停止。同盘移动是一次原子改名，所以停止后**留在正常目录里的 run 之间没有悬空引用**。`trash.json` 只作完成记录，**不做续做**。
- 部分删除后的收尾（本轮不新建 journal）：停止时终端说明已挪走哪些、剩下哪些；`session.json` 的 `viewing` / `editing` 若指向已挪走的 run，立即改为链上最近的幸存者，都没了就回到初始状态；下次 Load 时同样校验这两个指针。
- 进行中拒绝：Step0 Save、**预分割**、Generate、Step2、Step4 任一在跑（今天 `_dv_deletion_blocker` 的判断**加上 `_preseg_job`**）。
- A3 追加一条 deletion（今天已有）。
- `params.json.preseg_run` 是参数来源引用，不是像素依赖，删除不沿它级联：删 preseg 后保留的 fuse 仍有完整参数副本；界面上的来源详情显示「已删除」，代码不得假定该目录存在。

## 10. 身份与 A3

- 保留两种身份：`slide_id`（哪张图）、run 路径（哪份产物）。逐通道 `source_identity` 原样留在 correct run 里，**不改 viewer**。`handoff_identity` 只在融合设置变成 run params 之后、逐个核对消费者再删（RM-4）。
- A3 schema 不改。登记时机统一为「写 `.done` 时」，内容从 `params.json` / `inputs.json` 取；corrected 仍按通道登记一条。

## 11. 旧项目（用户裁定：不做兼容、不做导入）

- 开发阶段，今后只用新布局。不保留读取旧布局的任何分支，也不做导入。
- 旧 project 文件夹打开时识别为「不是本版本的项目」，提示换一个输出目录；文件不动。现有 `fusion_data` 下的工作区需要重跑，用户已接受。

## 12. 被替换掉的东西

| 今天 | 替换为 |
|---|---|
| `versions/index.json`、`version.json`、current / complete / legacy v1 | `runs/*/` + `.done` + `session.viewing` / `editing` |
| dirty draft、Step2 Run 禁用 | Step2 输入下拉 |
| 写时复制规则表、`versions/corrected/cNNN` | 像素会变才新建 correct run；Intensity 不进 correct run |
| `localize()`、`_dv_localize_dirs`、绝对路径 | project 相对路径 |
| 延迟 A3 登记、`provenance_jobs` 重放 | 写 `.done` 时登记 |
| `trash.json` 续做 | 先下游后上游 + 指针校验 |
| `roi_index.segmentation_runs`、`segmentation_registry.json`、`segmentation_results_index.json` | 目录扫描 + `session.json` |
| 两套 reuse | §8 一套 |
| 版本文件夹里 4 份参数副本 + 冻结 seg params | 一个 `params.json` |
| 所有 legacy / 旧布局读取分支 | 删除（§11） |

不改：校正、融合、分割、定量算法；viewer 读取、调度、缓存；Step0 handoff 的 patch / 几何部分；A3 schema；`settings/step1.json` 与 `segmentation_params/` 的现有语义；预分割 → Use → Save → Generate 的操作顺序。

## 13. 分阶段、白名单草稿、验收

每阶段单独验收、单独提交；白名单在开工前按只读盘点定稿，超出即停。

**RM-1 运行链落盘**（correct / preseg / fuse 三种 run，`settings/`，`session.json`，project 相对路径）
- 白名单：新 `utils/run_store.py`（建目录、三个约定文件、扫描、沿链查找、相对路径、显示名）；`core/step0_handoff.py`（`corrected_zarr_path` 的来源）；`ui/step0/step0_page.py`（Save → settings + 像素会变才新建 correct run）；`ui/step0/overview_panel.py`（`FullFusionWorker` 输出目录）；`core/preseg_run.py`（预分割 run 的目录位置与三个约定文件）；`ui/main_window.py`（Save Fusion Settings → `settings/step1.json`；Generate → fuse run，`params.json` 记 `preseg_run`；session 写读含 `viewing` / `editing` / `edited_against`）；测试 `tests/test_v16_rm1_run_store.py`。
- 验收：两套校正参数 → 两个 correct run，第一份逐字节不变；只改 Intensity 再 Save → 无新 correct run、无像素复制；Save Fusion Settings → 预分割 → Use → Save → Generate 这条顺序走通，preseg run 的上游是 correct run，fuse 的 `params.json` 记下 `preseg_run`；两次 Generate → 两个 fuse run，`inputs.json` 各指对；相同参数再 Generate → 复用；复制整个 project 到别处、**把原项目改名或移走后**打开正常，只覆盖已转换为相对路径的记录（交接、session、run 记录、settings）；HQ 分割参数里的路径在 RM-2 后补验，此前不记「整个项目复制后全部可用」为通过（§16.3-6）；**F1=0.5、Save=0.8、草稿=0.9 关闭重开三份都在**；**在 C2 上编辑、看 C1 的历史、关闭重开、回 Step1 仍是 C2 与其草稿**。

**RM-2 下游沿链**（Step2 输入下拉、Step3 列表、Step4 沿链取 corrected、显示名、删除）
- 白名单：`ui/step2_page.py`；`workers/segment_merge_worker.py`（run 目录与三个约定文件）；`core/step3_masks.py`（扫描 `runs/segment_*`）；`ui/step3_mask_bar.py`（显示名、×；`unknown` 字样删除）；`core/quant_sources.py`、`ui/step4_page.py`（沿链、quant run）；`utils/trash.py`（去续做）；`ui/main_window.py`（`viewing` 切换、删除接线、删除限制含 `_preseg_job`、部分删除后的指针校验）；测试。
- 验收：同一 fused 跑两个方法，新 fused 再跑第三个，三份 `inputs.json` 正确，Step4 各读对 corrected；**同一 fused 在 Step2 先跑 StarDist，再改成 Cellpose 跑第二次，两份 `params.json` 各自正确**；在 Step3 看历史不改 `editing` 与草稿；删 preseg 后其 fuse 的来源详情显示「已删除」且 Step2/3/4 正常；删一个 segment run 不动共享 fuse 和别的 segment；删 fuse run 时确认框列出全部下游；预分割进行中拒绝删除；模拟第二个目录移动失败 → 停止、终端说明、`viewing` 落到幸存者、重开后一致。

**RM-3 删 DV 与旧布局**（`utils/data_versions.py`、`_dv_*`、draft、COW、localize、DV-D 选择框行；`_dv_register_legacy`、handoff schema v1 分支、`segmentation_registry.register_legacy_result`、固定路径回退）
- 验收：全量回归；RM-1/2 验收重跑。

**RM-4 删多余索引与第二套 reuse**（三个运行索引、`_try_reuse_fused_zarr`、`handoff_identity` 经消费者核对后）
- 验收：Step3 列表与目录一致；全量回归。

## 14. 已裁定 / 待裁定

已裁定（2026-10-03）：Run 模型方向；corrected 不现算；沿用 `.trash`；依赖链约束；不起名；不做旧布局兼容、不做导入。
待裁定：**批准 RM-1 开工**（白名单 §13，开工前先做只读盘点定稿）。

## 15. 不做与停止门

不新造 hash、registry、authority、token、journal、状态机；不加 UI 之外的保护机制；不改算法与 viewer。复核若要求新增上述任何一项，按规则 7 停下报用户，不先实现。

---

## 16. RM-1 只读盘点（2026-10-03，HEAD `9be3386`，未改代码；v2 按审核意见修订）

本节是实施白名单的草案，**未经用户批准不扩围**。没有运行测试，16.6 的数字只作排期参考。

### 16.1 原白名单逐项核对

| 文件 | 行数 | 盘点结论 |
|---|---|---|
| 新 `utils/run_store.py` | — | 按 §4 新建 |
| `core/step0_handoff.py` | 754 | 交接 manifest 写 10 余个绝对路径字段；还把 `source_ome / output_dir / roi_dir` 绝对路径写进 corrected zarr 属性（只有 `source_ome` 有读者，原图在 project 外，可保留绝对）。要改：相对路径、`corrected_zarr_path` 指向 `editing.correct_run`、`.done` 前写完属性 |
| `ui/step0/step0_page.py` | 12396 | Save 在 `_save_and_continue`（11768 起）；DV 草稿逻辑 `_dv_base_corrected_path / _dv_prepare_draft / _dv_draft_for_geometry`（11675 起）；Load 选择框与 DV-D 行在 10934–11640 |
| `ui/step0/overview_panel.py` | 4174 | `FullFusionWorker` 输出目录来自 `cfg["output_dir"]`，在其中写 `fused_<区域>.zarr`、`fusion_meta.json`、`roi_config.json`。预计不改或只改一两行；`OUTPUT_DIR` 两处是 ROI 配置的浏览路径，逐处核对 |
| `core/preseg_run.py` | 245 | 目录由 `runs_dir(step1_dir)` 决定，`run.json` 已是冻结快照 |
| `ui/main_window.py` | 10606 | Generate 是 `_save`（9753）；Save Fusion Settings 是 `_commit_fusion_settings`（8620）；现场是 `_step1_session_payload`（3740）与 `_find_step1_session`（3694）；DV 入口见 16.4 |

### 16.2 需要加入 RM-1 白名单的文件（限定范围）

| 文件 | 原因与范围 |
|---|---|
| `utils/roi_project.py` | 新建工作区时建 `step0…step3/future` 目录。**只改**新布局的目录创建、路径解析和返回的上下文，不扩展其他项目管理功能 |
| `utils/workspace_session.py` | Load 只列「有 `step0/step0_roi_result.json` 的工作区」。**只改**工作区发现和 Load 路径 |
| `ui/step1_presegmentation/run_job.py` | 预分割 job 自己调 `preseg_run.write_run(step1_dir, …)`、逐任务发布记录。**只加**发布位置和结束时的 `.done` |
| `core/tile_tissue.py` | Step2 跳空 tile 时到 fused zarr 旁边或 `step1/` 找融合设置。**只改**读取融合参数的位置，组织判断算法不动 |
| `utils/segmentation_params.py` | **条件项**。现有函数接受目录参数，优先只改调用方；确需改它时先报 |

只读核对、预计不改：`ui/step0/search_ctrl.py`（校正 worker 写进传入目录）、`core/provenance.py`（A3 已按 project 相对路径记录；靠向上找 `roi_manifest.json` 识别工作区，所以依赖 16.3 第 1 条）。

### 16.3 计划书没覆盖的点：建议与边界

1. **文件名**：沿用 `project_manifest.json`、`roi_manifest.json`、`step0/roi_config.json` 的现有名字（改名会牵动 provenance、workspace_session、tile_tissue、object_tables 等至少 6 个文件）。**批准后正式改写 §4**，不让概念名与实际文件名并存。
2. **Step0 交接文件**：`step0_roi_result.json`、`roi_config.json`、修订号 `patch_config.*.json` 是可变的当前状态（patch 编辑由后台任务重写），放 `rois/<id>/settings/step0/`。correct run 自己的 `params.json` 仍冻结它生成时的几何和参数，**之后的 patch 编辑不得改它**。
3. **无校正通道**：首次 Save 仍新建 correct run（空 zarr + 逐通道决策 + 来源）。之后 Save 若像素层面无变化，**复用该 run，不每次新建空 run**。
4. **预分割 `.done`**：job 结束（完成、取消、失败）时写。`.done` 只表示「记录已完整收尾」，**不表示全部任务成功**；Use 仍按实际成功的记录判断。只有无 `.done` 的文件夹（进程被杀）在启动时清理。
5. **两种 Generate 产物**：全细胞融合 zarr 和 DAPI 输入 zarr（`dapi_input_meta.json`，今天复用函数 `_try_reuse_dapi_input_zarr`）都归入 fuse run。§8 复用判定**必须包含产物类型和实际输入变换**，两种产物不得互相复用。§8 一套判定替换三套（fused、DAPI 输入、DV `same_content`）。
6. **HQ 参数中的绝对路径**（`hq_source_zarr`、`multichannel_source_path`，读者是 `segment_merge_worker`、`hq_source_resolver`）：RM-1 照旧写绝对路径，RM-2 改为相对并把 `workers/hq_source_resolver.py`（只限路径解析）加入 RM-2 白名单。**因此 RM-1 不宣称完整支持项目搬迁**：
   - RM-1 的搬迁验收只覆盖已转换的路径（交接、session、run 记录、settings）。
   - 搬迁验收要在**原项目改名或移走后**进行，证明副本不再读原目录；「没有报错」不算通过。
   - HQ 相关路径在 RM-2 完成后补做完整搬迁验收；在那之前不记「整个项目复制后全部可用」为通过。§13 RM-1 验收第 6 项相应改为上述口径。
7. **草稿持久化**：Step0、Step2、Step4 草稿是新功能（今天只有 Step1）。RM-1 **只交付并验收 Step0、Step1** 的草稿与恢复；Step2、Step4 的现场恢复在 RM-2 完成。
8. **`OUTPUT_DIR` 不是联动变量**。主窗口重新赋值的是自己模块里的全局名；`step2_page`、`step4_page`、`batch_step4_dialog` 等用 `from ..config import OUTPUT_DIR` 导入的是另一份，不会同步。用途也不同：例如 `step2_page` 第 2451、2532、2866 行把它当**实际输出目录的回退**，第 1303、1309 行只是**文件对话框默认目录**。接线方式：
   - 主窗口中已确认用于保存配置的路径指向 `settings/`。
   - 科学产物的输出路径由 `run_store` 显式传入，不经 `OUTPUT_DIR`。
   - 其他模块逐处区分「默认浏览目录」与「实际写入位置」，不做全局含义替换；**Step2 输出不得落入 `settings/`**。
9. **DV 入口与测试**：见 16.4。
10. **RM-2 白名单追加**：`core/object_tables.py`、`workers/feature_extract_worker.py`，只限目录与来源接线；`workers/hq_source_resolver.py` 只限路径解析（第 6 条）。

### 16.4 DV 入口：RM-1 必须停用，删除留到 RM-3

盘点 v1 说「RM-1 之后 DV 成为死代码」，**这一判断不对**。DV 接线仍在正常流程上：

| 位置 | 今天的行为 | RM-1 起若不处理 |
|---|---|---|
| `_dv_bind_step2`（`main_window.py:4764`，进入 Step2 时调用，4740） | 工作区内没有 data version 时提示 "No data version yet" 并让 Step2 不能 Run；设置不一致时以 dirty draft 禁用 | 新 fuse run 生成后，Step2 仍被拦住 |
| `_dv_register_legacy`（3018，Step0 交接后打开工作区时） | 把旧工作区登记为 v1 | 在新布局上误登记 |
| `_dv_install_step1_files`（3020）、`_dv_step3_run`（3016、2035）、`_dv_auto_step1`（3021、4858） | Load 一个版本时装回 Step1 文件、跳到 Step3 | 读不到版本，行为不定 |
| `_dv_commit_pending`（8834，融合完成时） | 发布 data version | Generate 不再分配版本，需改为写 fuse run 的 `.done` |
| Generate 中 `_dv_workspace / _dv_reuse_same / new_version_folder`（`_save` 内） | 版本复用与分配 | 由 §8 与 fuse run 取代 |
| `step0_page.py` 的 DV 草稿三函数与 Load 选择框 DV-D 行 | 写 `versions/corrected/cNNN`、列版本 | 由 correct run 与「一行一个 ROI」取代 |

做法：RM-1 **停用上述入口和接线**（调用点改走 run 模型，或不再调用）；已经不可达的实现（`utils/data_versions.py`、`_dv_*` 方法体）留到 RM-3 删除。RM-1 阶段 Step2 的输入：由主窗口把当前 `editing.correct_run` 下最新的 fuse run 交给 Step2 现有的输入框，`step2_page.py` 不改；完整输入下拉在 RM-2。

测试分类（未运行前一律称「预计受影响」，不预先把整份文件定为已知失败）：
- **纯验证 DV 布局的测试**（版本编号、`index.json`、COW 表、legacy v1 登记等）：逐项标明被哪一节替代、在哪个阶段删除。
- **仍有效的行为测试**（旧输入不被覆盖、共享输入不被误删、运行中拒绝删除、标记最后写等）：保留，或迁移到新布局的对应测试。
- 分类表在 RM-1 第一次聚焦回归后按实际结果写出，随 RM-1 一起提交。

### 16.5 RM-1 结束时公开路径的状态

| 路径 | RM-1 结束时 |
|---|---|
| 新建项目 → Step0 Save → correct run | 可用，验收 |
| Save Fusion Settings → 预分割 → Use → Save Config & Generate → fuse run（两种产物） | 可用，验收 |
| Step0、Step1 草稿与 `session.json` 恢复（含审核第 2、3 条场景） | 可用，验收 |
| Load 已有新布局项目 | 可用，验收 |
| 进入 Step2 并对最新 fuse run 运行 | 可用（经现有输入框），验收；输入下拉在 RM-2 |
| Step2 切换历史 fuse run、Step3 按链切换、Step4 沿链取 corrected | **未交付**，RM-2 |
| Step2、Step4 草稿恢复 | **未交付**，RM-2 |
| 删除 run（沿链） | **未交付**，RM-2；RM-1 期间 DV-D 删除入口停用 |
| 项目搬迁 | **部分**：已转换路径验收；HQ 路径在 RM-2 后补验 |
| 旧布局项目 | 提示「不是本版本的项目」（§11） |

### 16.6 测试与工作量（排期参考）

- 引用当前布局文件名或 DV 的测试共 72 个（grep 计数），多数自建临时目录，不一定失败；实际数量以 RM-1 第一次聚焦回归为准。
- RM-1 估计 1.5–2 天（含 16.2 的 5 个文件、16.4 的入口停用、Step0/1 草稿），总量 5–6 个工作日。未运行测试，这不是完成承诺。

待裁定：16.2 白名单追加；16.3 第 1–10 条；16.4 的停用做法；16.5 的阶段边界。批准后先改写 §4（第 1 条）和 §13 RM-1 验收第 6 项（第 6 条），再开工。

---

## 17. RM-1 实施记录（2026-10-03，未提交，待回归与验收）

### 17.1 改动文件（均在 §13 RM-1 + §16.2 白名单内）

| 文件 | 内容 |
|---|---|
| 新 `utils/run_store.py` | run 文件夹（`<kind>_<stamp>`）、`params.json` / `inputs.json` / `.done`、沿链查找、§8 逐字段复用、未完成 run 清理、显示名、`session.json`、记录路径的相对化（`to_records` / `from_records`）、草稿保留（`put_draft`） |
| `utils/roi_project.py` | 新工作区建 `settings/step0/`、`settings/`、`runs/`、`step2/`、`step3/`；识别旧布局工作区 |
| `utils/workspace_session.py` | 工作区发现读 `settings/step0/`；列出旧布局工作区 |
| `core/step0_handoff.py` | manifest 落盘为 project 相对路径、读出时还原；交接写入在发布 manifest 之前发布新 correct run；corrected zarr 不再写工作区绝对路径属性 |
| `core/preseg_run.py`、`ui/step1_presegmentation/run_job.py` | 预分割 run 放 `runs/preseg_<stamp>/`，冻结快照改名 `params.json` 且路径相对；job 结束（完成 / 停止 / 失败）写 `.done` |
| `core/tile_tissue.py` | 融合设置读 fuse run 的 `params.json`，交接读 `settings/step0/` |
| `ui/step0/step0_page.py` | Save：像素会变才新建 correct run（增量时从上游复制）；只改 Intensity 复用；取消 / 出错 / 交接失败丢弃未发布 run；Load：一行一个工作区、无删除 ×、旧项目拒绝打开和写入、Step0 草稿保存与恢复 |
| `ui/main_window.py` | 交接读取解析相对路径；Step1 现场并入 `session.json` 的 `drafts.step1`；预分割 / Generate 写 run；§8 复用；进入 Step2 绑定当前 correct run 下最新 fuse run（无则清空）；融合设置相对路径；§16.4 的 DV 入口全部停用；删除限制加入预分割 job |
| `tests/test_v16_rm1_run_store.py` | 新，31 项，含 §13 RM-1 验收场景 |
| `tests/test_v16_a6_workspace.py` | §16.4 迁移：夹具改为已发布 correct run，DV 行标签断言改为一行一工作区；被测行为不变 |

### 17.2 codex（gpt-6-astra，low）审核

- 第 1 轮 5 条，全部核实属实并修复：Load 后自动保存抢先覆盖 Step1 草稿；Step2 保留了别的 correct run 的 fuse run；恢复融合结果仍走「当前设置」门槛；预分割与 fuse 记录仍有绝对路径；复制像素失败时泄漏未发布 run。另判定进程内 `_open_runs` 集合属于 §15 禁止的新登记机制：已删除，清理改为仅在已有的「无任务在跑」判断为空时执行。
- 第 2 轮 1 条：之后的 Step0 Save 清掉了待恢复标记。已修。同时自查发现：上下文不符的草稿会被下一次自动保存覆盖，违反 §6「不丢弃」。改为移入 `drafts.<step>_kept`。
- 第 3 轮 1 条：`_kept` 只有一个位置，第二次换上下文仍会丢。改为列表，每个上下文保留最新一份。codex 确认保留在 `session.json` 内不属于 §15 禁止的机制。

### 17.3 与计划书的差异（需用户知悉）

- `session.json.view` 只记录 `current_step`；Load 后不自动跳回该步骤（今天的行为是停在 Step0、进入 Step1 时自动恢复）。
- `viewing` 由 Generate 完成时写入；按 `viewing` 恢复显示链在 RM-2（Step3 列表）实现。
- 上下文不符的草稿保存在 `drafts.<step>_kept` 列表中，暂无界面入口。

### 17.4 聚焦回归（65 个模块，新旧代码各跑一遍，`~/fusionflux/bench_rm/`）

旧代码（`9be3386`）本来就不正常、新代码相同的：`test_step0_channel_conditioning`（10 分钟超时，失败点相同）、`test_step1_montage_view`（进程崩溃）、`test_step3_patch_strip`（1 项失败）。

新代码新增失败的处理：

| 模块 | 原因 | 处理 |
|---|---|---|
| `test_step0_authoritative_save_barrier` | 代码缺陷：工作区不在 project 里时记录 `editing` 抛类型错误 | 已修：不在 project 或不是已发布 correct run 时不记录 |
| `test_step0_correction_param_inheritance` | 无工作区的上下文：缺「就地写入」退路 | 已修：无工作区时仍写 step0 目录下的 corrected，不建 run（与 DV 原退路一致） |
| `test_preseg_contract`、`test_preseg_run`、`test_step1_step2_handoff_e2e` | 预分割 run 改到 runs 根目录、快照改名 `params.json` | 测试迁移（替身补 `_preseg_runs_root`；读 `params.json`；文件列表含 `.done`） |
| `test_step0_step1_handoff_contract` | 夹具 manifest 没有 `roi_dir`，旧代码靠旧布局推断 | 测试迁移：夹具写 `roi_dir`（真实交接总会写） |
| `test_step2_skip_empty_tiles`、`test_v16_artifact_graph` | 夹具把设置 / 产物写死在旧目录 | 测试迁移：设置放 `settings/`，路径断言改为与实际产物比较 |
| `test_step2_skip_empty_tiles` 1 项、`test_v16_artifact_graph` 6 项 | Step4 的 `quant_sources` 仍读 `<ws>/step0/` 交接 | **等 RM-2**（§16.5：Step4 沿链取 corrected 属 RM-2） |

修复后重跑上述全部模块：205 通过，余下 7 项全部是 RM-2 的 Step4 项。

### 17.5 DV 测试分类（§16.4；文件随 RM-3 删除）

`test_v16_data_versions.py`（20 项失败）、`test_v16_dv_delete.py`（22 项失败）；纯 `data_versions` 模块单元测试仍通过。

| 类别 | 测试 | 去向 |
|---|---|---|
| 纯 DV 语义（版本号、current、draft 行、版本切换、legacy v1、版本内冻结 session） | `the_chooser_lists_one_row_per_workspace_and_version`、`a_dirty_draft_is_its_own_row_first_and_preselected`、`the_draft_row_continues_the_draft`、`choosing_the_current_version_over_a_draft_loads_the_version`、`loading_an_older_version_restores_it_and_republishes_the_handoff`、`opening_the_current_version_rewrites_nothing`、`step1_settings_saved_since_the_version_are_a_draft_too`、`once_a_draft_exists_saves_only_touch_the_draft`、`the_handoff_stays_in_the_workspaces_step0_and_registers_nothing`、`the_page_marks_a_published_product_read_only_for_the_writer`、`the_handoff_reader_maps_before_it_writes` | 被 §2/§5/§6/§12 替代，RM-3 删除 |
| 行为仍有效，已迁移到 `test_v16_rm1_run_store.py` | `changing_one_channel_copies_…`（→ `a_correction_change_writes_a_new_run…`）、`a_save_with_no_corrected_change_…`（→ `an_intensity_only_save_keeps_the_correct_run`）、`another_roi_makes_a_fresh_draft…` / `a_new_outline_with_the_same_boxes…`（→ 同一新建 run 分支，`a_withdrawn_channel_is_a_new_run_of_the_same_pixels`）、`a_save_never_corrects_into_another_projects_product`（→ `…another_projects_run`）、`a_region_without_its_product_…`、`an_error_or_cancel_leaves_no_version…`、`a_cancel_before_the_completion…`（→ `a_region_without_its_product_publishes_nothing`、`a_cancelled_correction_leaves_no_run`、fuse 丢弃路径）、`an_opened_workspace_restores_step1_by_itself_once`（→ `an_opened_workspace_does_not_overwrite_its_step1_draft…`、`a_later_step0_save_keeps_the_step1_restore_pending`） | 已有新测试 |
| 删除相关（DV-D） | `test_v16_dv_delete.py` 全部 22 项：行内 ×、回收站、续做、运行中拒删、先释放再移动、替换版本 | RM-2 的沿链删除重写；「运行中拒删」「先释放」「替换失败不删」三类行为在 RM-2 迁移 |

---

## 18. RM-2 实施记录（2026-10-03，未提交，待回归与验收）

### 18.1 用户裁定（2026-10-03 晚）

- 删除入口：每一步都有，放在该步已有的 Load 里，沿用 DV 的 ×。Step0：▶Load 的工作区选择框，每个工作区下列出它的 Step0 结果（correct run），行首 ×；Step1：「Load plan…」的列表，计划之下列出预分割 run，行首 ×；Step2：Input 下拉框的 fuse run 行首 ×；Step3：结果列表的 segment run 行首 ×。Step4 不设删除入口，由用户手动删。
- provenance：批准改一行，`core/provenance.workspace_regions` 读 `settings/step0/roi_config.json`。

### 18.2 改动文件

| 文件 | 内容 |
|---|---|
| `workers/segment_merge_worker.py` | Step2 写 `runs/segment_<stamp>_<method>/`；完成时写 `params.json`（Run 时实际生效的配置）、`inputs.json`（其 fuse run），再 `.done`，之后登记 A3；输入不是已发布 fuse run 时不发布；重读参数文件时还原相对路径；HQ corrected 优先取本链 correct run |
| `core/step3_masks.py` | 列表只扫描已发布的 `runs/segment_*`；每个 run 带显示名与上游 fuse run 的显示名（上游已删显示 `(deleted)`） |
| `ui/step3_mask_bar.py` | 行尾标签为上游名；`unknown` 删除；本项目的 run 行首 × |
| `core/quant_sources.py` | Step4 沿链取 correct run 的冻结决策与像素；产物须包含所需通道；DV 版本读取删除 |
| `workers/feature_extract_worker.py` | 项目里的 segment run 一律定量进新的 `runs/quant_<stamp>/`（上游 = 该 segment run），写在已发布 run 里的输出被拒绝 |
| `ui/step4_page.py` | 草稿保存 / 恢复；项目结果的输出框只读，显示「新 quant run」 |
| `ui/step2_page.py` | Input 下拉框（当前查看链的 correct run 下的 fuse run，选中即带上其冻结区域）；× 删除；Browse 到列表里的 run 等同选中；Run 只接受本工作区、且为下拉框所选的 fuse run；草稿；参数文件路径还原 |
| `utils/trash.py` | `move`：按调用方顺序（下游先）逐个原子改名，失败即停；记录 `complete` / `partial`；续做删除 |
| `utils/run_store.py` | 会话指针校验与改指；`put_draft`；`param_file` 等路径键 |
| `core/object_tables.py` | 运行目录与 ROI 配置读新布局 |
| `core/provenance.py` | 一行（18.1） |
| `ui/main_window.py` | Step2 按查看链绑定；删除流程（运行中拒绝、确认框列全、先释放、移动、指针改到幸存者、A3 删除记录）；Step0 / Step1 的删除入口接线；Step2 / Step4 草稿；分割参数文件写相对路径 |
| `ui/step0/step0_page.py` | Load 时校验会话指针；Save 产生新 correct run 时 `viewing` 指向它；选择框列出 Step0 结果 |
| 新 `tests/test_v16_rm2_chain.py` | 26 项 |
| 测试迁移 | `test_quant_sources`、`test_v16_artifact_graph`、`test_step2_skip_empty_tiles`、`test_step3_masks`、`test_b3_fixes`（经共用夹具）、`test_step4_page`、`test_step4_worker`、`test_v16_object_tables`、`test_v16_a6_async`（unknown 标签改为上游标签）、`test_v16_a6_workspace`、`test_step1_method_plan` |

### 18.3 codex 审核

- 第 1 轮 5 条，全部属实已修：Step4 可写进已发布 quant run；查看历史链时 Step2 仍用编辑中的区域；HQ 优先用了参数文件里的 corrected；Browse 可接受别的工作区的 fuse run；Step2 草稿恢复后 Run 会被索引覆盖。
- 第 2 轮 2 条（第 1 轮修得不彻底），已修：全图 fuse run 未清空区域；同工作区 Browse 到别的链时下拉框不跟随。发现逐轮收窄，审核停止。

### 18.4 与计划书的差异（需用户知悉）

- Step3 选中历史结果后，Step2 输入跟随该链；Step0、Step1 仍显示编辑中的上下文，不切换显示。
- 项目里的结果做 Step4 时，输出文件夹不能再自选（一律新 quant run）。
- 「当前运行」（active）暂仍从工作区索引读取，RM-4 删索引时改为 `session.viewing`。

### 18.5 真机验收反馈（2026-10-03 晚）与处理

通过：1、2、3、4、5、7、9、11、12、13、14。

- **#6 预分割未恢复**：原因是 Use 之后不触发 Step1 草稿保存，关窗也不保存 Step1 草稿。改为 Use、预分割结束、切步、关窗时都保存；Step1 草稿新增 `preseg`（方法、勾选的 patch、run 位置、在用的结果），恢复时从磁盘读回结果面板与 montage 轮廓，在用的结果仍可用时恢复选择。
- **#8 用户裁定 B**：Step3 选中结果后进入 Step2，Step2 换成该次运行 `params.json` 里的方法与参数（每次选择只载入一次，之前的 Step2 设置先存为草稿）；刚打开工作区时以保存的 Step2 草稿为准。
- **#8 / #10 下拉框太窄**：Step3 结果列表与 Step2 Input 的弹出列表按最长一行（含行尾标签与 ×）加宽。
- **#1 Save 弹窗慢**：实测 Save 处理到弹窗 < 2 ms；用户裁定不处理。
