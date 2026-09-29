# v16 块 A0.5 — 行为身份与运行出处分开：申请 v3

日期：2026-09-29。分支 `v16`，调查基于 HEAD `14ae33e`。依据：`docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.2.md` §3。
状态：**申请 v3，用户 2026-09-29 批准**（§8 的裁定全部定案）。

修订记录：
- v1：身份 v2 = runner 代码哈希 + 扩展的库版本 + 每次启动时计算的模型文件 sha256。
- v2：按独立审核修订（用户同意）。v1 为了消除一个假警报，造了一整套精确的指纹系统，过度设计了。v2 把三个问题分开处理：
  1. **行为有没有变**：用一个显式维护的 `behavior_version`，写进很小的 `engine_identity`，Step1 → Step2 只比较它；
  2. **当时用了什么**：放进 `engine_provenance`，只记录，不参与比较；
  3. **模型文件有没有坏**：部署时用 `scripts/model_manifest.py --verify` 核对，运行时不再计算哈希。

  v1 §8 的四项裁定按审核意见定下，见 §8。
- v3：用户裁定（2026-09-29）：
  - `behavior_version` 或 `model_id` 与 Step1 不同时，**弹窗警告，用户确认后才运行并记录**。于是 Step2 页面的运行前检查进入白名单，界面新增一个对话框，UI 规则和两份用户指南要同步更新（§3.4、§4）；
  - 不加守护测试。

---

## 1. 必要性

问题**已经在真实数据上出现了**：

- test1 里三个 Step2 运行记录的 `seg_engine.identity.lock_hash` 都是 `ddf963353c497824`，而现在的代码算出来的是 `51572c582acfc589`。原因是 S4-2（`50e8290`）为了装 anndata 更新了 `envs/fusion_mesmer` 的 lock 文件，而 anndata 与分割无关。
- 所以 S4-2 之前的每个 Step1 结果，今天交给 Step2 都会报「the engine differs from the Step1 run in: lock_hash」。
- 装 pyarrow（A4）时，所有身份还会再变一次。
- 反过来，环境真的变了、但 lock 文件没跟着更新时，身份又一点也不变。

## 2. 只读调查结论（HEAD `14ae33e`，与 v1 相同，这里摘要）

- **`engine_identity`**（`seg_runner/runner.py:32-49`，在 `hello` 消息里发出，:97-98）现在有五个字段：
  - `engine`
  - `lock_hash`：对仓库里 `conda-linux-64.lock` 和 `requirements-pip.txt` 取哈希，并不反映实际安装的环境
  - `lib_versions`：各引擎只列自己的库（`seg_runner/engines.py:117`、`:149`、`:173`）
  - `model_checksum`：对 `models.json` 里对应条目的哈希，**不是对权重文件的哈希**
  - `runner_version`：`seg_runner/*.py` 全部文件的哈希
  
  设备本来就不放进身份，单独报告。
- **比较身份的地方只有一处会受影响：Step2 的 `_start_contract_engine`**（`workers/segment_merge_worker.py:2310-2326`）。
  - 引擎种类不同就中止；其他字段不同只警告，照常运行（块 M 的规则）。
  - Step1 的 `preseg_contract.build()` 要求整个身份字典完全相等（`core/preseg_contract.py:62-72`）。但每个预分割运行都是新建的（`ui/main_window.py:5398`），一个引擎在一个运行里只启动一次（`ui/step1_presegmentation/run_job.py:179`），所以运行内部的身份一定一致，不受环境变化影响。
- **模型完整性已经有机制**：`models.json` 为每个文件记录了 size 和 sha256，`scripts/model_manifest.py --verify` 负责核对。本机 `--verify` 报出 7 个问题：
  - StarDist 的 manifest 除了根目录下的 3 个文件，还列了 `2D_versatile_fluo_extracted/` 下的 3 个同名文件。本机只有根目录那一份，它和 TF 2.8 的实际解压位置一致，内容与 manifest 里记录的 sha256 相同。
  - Mesmer 模型不在本机（已知情况）。
- 给真实权重文件算哈希的开销：cpsam（1.2 GB）每次约 1.08 s。**v2 不在运行时做这件事。**

## 3. 做法

### 3.1 `engine_identity` v2：只用来判断兼容性，保持很小

```json
{"version": 2, "engine": "stardist", "behavior_version": 1, "model_id": "stardist_2D_versatile_fluo"}
```

- **`behavior_version`**：每个引擎一个整数常量，写在 `seg_runner/engines.py`，初值都是 1。
  - **只有当改动可能改变分割输出时才加一**：归一化、阈值的语义、扩展算法、输入通道的排列、后处理、换默认模型等等。
  - 加包、改日志、改通信协议、改界面，都**不加**。
  - 这条规则写在常量旁边的注释里，并同步写进 v2.2 计划 §3。
- **`model_id`**：这个引擎实际加载的模型在 `models.json` 里的键：`cellpose_cpsam`、`stardist_2D_versatile_fluo`、`mesmer_multiplex_segmentation`。由引擎对象报告；StarDist 的 `model_name` 与键对应。

### 3.2 `engine_provenance`：只记录，不比较

```text
git_commit                  若是 git 仓库（复用 workers/feature_extract_worker.py:57 的做法）
runner_version              今天的 seg_runner/*.py 哈希（原样保留）
lib_versions                今天的引擎库，再加上已知的几个预处理库（numpy、scipy、scikit-image；StarDist 另加 csbdeep）
env_lock_hash               今天的 lock_hash（原样保留，只是改了名）
device                      同今天
model_manifest_entry_hash   今天 model_checksum 的算法，改名，不再冒充权重的校验和
model_resolved_path         实际加载的模型路径（能拿到就填，拿不到就是 null）
```

`hello` 消息从今天的 `identity + device` 变成 `identity + provenance + device`。

### 3.3 存在哪里

- **Step1**：`run.json["engines"][engine]` 仍然只存 `identity`（小的 v2 身份），另外新增 `run.json["engine_provenance"][engine]`。这是 `run_job.py:179` 旁边加的一行。记录（`preseg_run.py:167`）照旧抄写 `identity`。`preseg_contract` 不改。
- **Step2**：`seg_engine` 记录 `identity`、`step1_identity`、`provenance`，外加 `identity_comparison`（比较了哪些字段，结果如何）。

### 3.4 Step2 的比较（只改 `_start_contract_engine` 里这一段）

| 情况 | 处理 |
|---|---|
| `engine` 不同 | **中止**（不变） |
| 两边都是 v2，`behavior_version` 或 `model_id` 不同 | **运行前弹窗，用户确认后才运行并记录**，见下文 |
| 两边都是 v2 且完全相同 | 不警告 |
| Step1 那一边是**旧身份**（没有 `version`，带 `lock_hash`） | 引擎种类一致就接受；日志里说明一次「legacy Step1 identity; compared on the engine kind only」；记录 `identity_comparison.legacy_identity = true`。**不去逐字段比较旧的 `lock_hash`、`runner_version`、`lib_versions`**：新旧两种格式的语义不同 |

**弹窗确认分两道：**

1. **运行前（Step2 页面，与现有的 `_check_preseg_contract` 放在一起，`ui/step2_page.py:2289`）。**
   - 当前的 `behavior_version` 和 `model_id` 是 `seg_runner/engines.py` 里的常量。那个模块在顶层只导入 json / os / pathlib，所以在界面进程里读取不用启动引擎，也不会加载 TensorFlow 或 torch。
   - 读出来与 Step1 的身份不同时，弹出一个对话框（沿用 `_confirm_ignored_settings` 的做法），列出每一项差异，例如：
     > StarDist 的分割行为自 Step1 运行以来已改变（版本 1 → 2）。Step1 的预览是按旧行为得到的，全图结果可能与预览不同。
     >
     > [取消]　[仍然运行]
   - **取消** → 不运行；**仍然运行** → 把确认内容（差异清单 + 时间）交给 worker。
2. **引擎启动后（worker，`_start_contract_engine`）。**
   - 用引擎自己报告的身份，再与 Step1 比一次。
   - 差异与用户确认的一致 → 运行，并在 `seg_engine.identity_comparison` 里记下 `user_confirmed: {differences, at}`。
   - 出现**没有被确认的差异** → 中止，并提示「引擎与确认时不一致，请重新开始」。比如弹窗之后代码又变了，或者走了没有弹窗的路径。这样没有任何一条路能绕过确认。

旧身份（Step1 在 v2 之前）：不弹窗，只记录（§8 裁定 1）。

### 3.5 小的维护修补：StarDist manifest

- 从 `envs/fusion_mesmer/models.json` 的 StarDist 条目里**只删掉 3 条 `2D_versatile_fluo_extracted/…`**。
- 保留根目录下的 `config.json`、`thresholds.json`、`weights_best.h5` 和 zip，它们已经在 manifest 里，size 和 sha256 都与本机一致。
- 不重跑 `--write`：它会重写整个文件，而本机没有 Mesmer 模型。
- 不改任何加载逻辑。
- 修补后 `--verify` 的结果：StarDist 与 cellpose 通过；Mesmer 仍报缺失（已知，不在本块处理）。

### 3.6 计划文档

v2.2 §3 的「model artifact identity」那一条（上一轮审核加入，要求在运行时给实际模型文件算哈希）改为：模型文件的完整性在部署时由 `model_manifest.py --verify` 保证；运行时出处只记录 `model_manifest_entry_hash` 和实际路径；兼容性由 `behavior_version` + `model_id` 判断。执行记录写明这项改动及其原因。

## 4. 白名单

- `seg_runner/runner.py`：`engine_identity` 改为 v2；新增 `engine_provenance`；`hello` 带上 `provenance`
- `seg_runner/engines.py`：每个引擎加 `BEHAVIOR_VERSION` 和 `model_id`；`lib_versions` 加上已知的预处理库；报告 `model_resolved_path`。**不改 predict**
- `ui/step1_presegmentation/run_job.py`：只在 :179 旁加一行，记录 `engine_provenance`
- `workers/segment_merge_worker.py`：只改 `_start_contract_engine` 的比较与记录（:2310-2326），外加接收用户确认的构造参数
- `ui/step2_page.py`：**只在运行前检查**里加入身份比较和确认对话框（`_check_preseg_contract` 附近，:2289-2324），并把确认内容传给 worker（:2427 的构造调用）
- `UI_SURFACE_RULES.md`、`docs/user_guide.md`、`docs/用户指南.md`：记录这个新对话框
- `envs/fusion_mesmer/models.json`：只删 StarDist 的 3 条 `_extracted` 条目
- 测试：
  - 新增 `tests/test_engine_identity.py`：纯函数，不跑真实引擎
  - `tests/test_step2_runner_path.py`：改 `test_another_engine_version_runs_and_is_recorded`，并加三条用例：旧身份；已确认的差异；没有确认的差异会中止
  - `tests/test_step2_engine_unified.py`：对话框用例，沿用 :320 的做法替换 `exec_`，离屏时不弹窗。取消时不运行；确认后运行；没有差异时不弹窗
  - `tests/test_preseg_contract.py`：按需加一组 v2 的 IDENT
- 文档：v2.2 §3 的修正与执行记录；本申请

## 5. 不改的范围

- 分割算法、参数、predict、归一化、扩展，全部不动
- `preseg_contract` 的规则、Step1 的运行与记录格式（只加一个出处字段）、`is_stale`
- `scripts/model_manifest.py`；Mesmer 的条目与模型
- 运行时不计算任何权重哈希；不加缓存；不装任何包

## 6. 风险

| 风险 | 对策 |
|---|---|
| 改了影响输出的代码却忘了加 `behavior_version` | 规则写在常量旁边的注释和计划里。可选的守护测试见裁定 2 |
| 本块自己改了 seg_runner 代码 | 现在只影响出处里的 `runner_version`，不再引发警告 |
| 旧的 Step1 结果 | 按 §3.4 接受，并说明一次 |
| 真实引擎测试带来的磁盘和页面文件压力 | C: 盘目前有 50 GB 空闲。只按顺序跑本块相关的模块：`test_engine_identity`、`test_preseg_contract`、`test_preseg_run`、`test_step2_runner_path`、`test_seg_runner*` |

## 7. 验收门

- [ ] 身份只剩 `version`、`engine`、`behavior_version`、`model_id`；只改 lock 文件、只改库版本、只改 seg_runner 的日志或通信代码，身份都不变（纯函数测试）
- [ ] `behavior_version` 或 `model_id` 不同时，Step2 在运行前弹窗：取消就不运行；确认后运行，并记录 `user_confirmed`；worker 遇到没有被确认的差异会中止；`engine` 不同时仍然直接中止
- [ ] 没有差异时不弹窗（不打扰正常使用）
- [ ] UI 规则与两份用户指南已写明这个对话框；真机验收：看一次对话框的文字和两个按钮
- [ ] 旧 Step1 身份（test1 副本里真实的 contract 形状）被接受，只说明一次，并记录 `legacy_identity = true`，不出现 `lock_hash` 相关的差异
- [ ] `engine_provenance` 被记录在 Step1 的 `run.json` 和 Step2 的 `seg_engine` 里
- [ ] `model_manifest.py --verify`：StarDist 与 cellpose 通过（Mesmer 缺失，已知）
- [ ] `test_preseg_contract`、`test_preseg_run`、`test_step2_runner_path`、`test_seg_runner*` 与 HEAD 相比没有新增失败
- [ ] 反向注入：把 `lock_hash` 或 `lib_versions` 放回身份，相应的测试会变红
- [ ] `git status` / `git diff` 的改动 ⊆ 白名单

## 8. 裁定

已按独立审核定下（用户同意）：

1. `runner_version` 不进兼容性身份，只保留在出处里；另设显式的 `behavior_version`。
2. 运行时不给权重文件算哈希；完整性在部署时由 `--verify` 核对。
3. 旧 Step1 身份：接受，说明一次，不比较旧的 `lock_hash`。
4. StarDist manifest：在本块内作为极小的修补，只删 3 条 `_extracted` 条目，不改加载行为。

仍请用户裁定：

1. ~~`behavior_version` 或 `model_id` 不同时的处理~~ **用户裁定：弹窗警告，用户确认后才运行并记录**（2026-09-29），见 §3.4。

   **旧身份**（Step1 在 v2 之前，没有 `behavior_version`）：**用户裁定：不弹窗，只记录**（2026-09-29）。日志里说明一次，并记录 `identity_comparison.legacy_identity = true`，只核对引擎种类。
2. ~~可选的守护测试~~ **用户裁定：不加**（2026-09-29）。`behavior_version` 靠规则和评审维护。
