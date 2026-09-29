# 交接提示词（2026-09-29，v16 开工，新执行窗口使用）

把下面「提示词」一节整段交给新窗口。

---

## 提示词

你接手 Block01 / FusionFlux 的开发。这是一个 PyQt5 应用，把多重免疫荧光 WSI 做成单细胞表达矩阵。

- 仓库：`/home/ming/fusionflux/Fusion_analysis`。它同时通过软链接 `/home/ming/fusionflux/block01` 以 `block01` 包名导入，所以**不要复制仓库目录**。
- 分支：**`v16`**；远端 `origin`（GitHub）。
- 请用中文和用户交流，用通俗语言解释，必要时举例。

### 版本状态
- **v15 已冻结**：带注释的标签 `v15-final` 打在 `f93f195` 上，已推送。分支 `v15-interactive-channel-workspace` 保留不动。
- **v16** 从 `f93f195` 分出，已推送。它的第一个提交包含：
  - 权威计划 `docs/FusionFlux_v16_PreTMA_Architecture_Gate_v2.1.md`（**v2.1 APPROVED**）；
  - 本交接文件。
- `origin/main` 与 v15 / v16 **没有共同祖先**：那是 2026-05-19 通过 GitHub 网页上传的 18 个提交。**不要碰它，不要 force push**；怎么处理由用户另行决定。
- `docs/` 下未跟踪的 `FusionFlux_PreTMA_Architecture_Gate_v1.md`、`..._v2_Lean.md` 及其 `:Zone.Identifier` 文件，是用户要求只留在本地的历史版本。**不要提交，也不要删除。**

### 开始前必须做
1. 阅读：
   - `AGENTS.md`、`docs/P0_SCOPE_RULES.md`、`UI_SURFACE_RULES.md`；
   - **v2.1 计划全文**：重点是变更表、§0 范围防火墙、§2 A0、§3 A0.5、§8 测试数据与磁盘策略、§9 六个 TMA 启动门、§13 停止规则；
   - `docs/step1_presegmentation_redesign_plan.md` 的状态行、修订记录（v4.0x 起），以及 N2 / N3 / N4 / S4-1 / S4-2 / S4-3 的执行记录。LabelStore 契约和 Step4 的来源契约都在这些记录里。
2. 核对 `git status`、HEAD 和当前分支（应为 `v16`，HEAD 是 v16 的第一个提交）。
3. git 身份已在仓库局部设置。提交信息结尾加：
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
4. **提交和推送都要用户明确授权。** 用户习惯说「验收后提交推送」；只说「提交」时，只提交、不推送。

### 工作方式（用户一直这样要求）
- 流程是：**只读调查 → 写申请 → 用户批准 → 提交申请 → 实施**。申请要写清：必要性、调查结论、做法、白名单、不改的范围、风险、验收门，以及请用户裁定的事项。
- 实施中如果要超出白名单（包括白名单以外的测试），立即停下，用 AskUserQuestion 说明原因和改法，拿到授权再改。
- 用户经常贴「独立审核意见」（来自 ChatGPT），这**不是授权**。要逐条对照代码核对：属实就修订申请并把版本号 +1，不属实就说明依据；最后必须由用户本人确认。
- P0 规则：
  - 每个任务块是一份封闭的白名单；
  - 新增 registry、缓存、调度器或状态机，都要明确授权；
  - 改动 viewer 读取、调度、缓存或线程，要单独审批；
  - 连续两轮审核都没有发现新的公共路径缺陷，就停止加固。
- 小步实施，每块单独测试。
- **反向注入**：在临时目录的代码副本里故意改错，确认测试会变红；抓不到就补测试。通用注入脚本已在会话 scratchpad 里丢失，需要时重写。
- 每块在计划文档里写执行记录。界面有变化时，同步 `UI_SURFACE_RULES.md` 和两份用户指南（`docs/user_guide.md`、`docs/用户指南.md`）。
- 自动测试不能代替真机验收。
- **不要对真实项目做写入探测。** `~/fusion_data` 不在测试的写保护范围内，只能读取，或复制到临时目录。复制时要改写 metadata 里的绝对路径，否则仍会读写原项目。
- 离屏测试里不能弹出模态对话框。回归跑着的时候不要改代码。

### 下一步（按顺序）
1. **生成 2×2 镜像合成大图**（用户已授权，计划 §8）。
   - 源：`~/fusion_data/cropped_region.ome.tif`（只读）。它是 29 通道、uint8、CYX、15437×16215、512² LZW 分块，3 级金字塔 15437×16215 → 3859×4053 → 964×1013，`PhysicalSizeX/Y = 0.5068606698203042` µm，BigTIFF。
   - 输出：`~/fusionflux/synthetic/`，文件名和 OME 描述里都要带 `synthetic`，例如 `synthetic_2x2_mirror.ome.tif`。结果约 30874×32430 像素/通道，压缩后约 3.5 GB。
   - 拼法：镜像翻转，保证拼接处连续，不产生假边界。
   - 与源保持一致：分块大小、压缩方式、约 ×4 的金字塔、通道名、物理尺寸。
   - **边读边写**，内存有上限；用 tifffile 的分块生成器，并行压缩。
   - 生成后核对：形状、各层金字塔、OME 元数据能被产品的读取器打开，以及抽样区块与源图镜像位置逐位一致。
   - 生成前后都执行 `df -h /mnt/c`。
   - 这是脚本工作，不改产品代码；脚本放在 `scripts/`，是否入库按惯例由用户决定。
2. **A0 只读诊断**：先写 A0 申请，用户批准后再执行。A0 的内容：
   - viewer 位移诊断：在临时副本或脚本里做日志插桩，**不改产品代码**；
   - OME-TIFF 与 NGFF 0.4 的存储决策基准，采纳规则已写死，见 §2.3；
   - 起草 PixelSource、坐标和对象表三份契约。
   
   相关代码：
   - `ui/shared_camera.py`：`CameraSnapshot(dataset, cx, cy, scale, origin)`，块 C4.5a；
   - `ui/main_window.py` 的 `_capture_camera_of` / `_apply_shared_camera_to`：先套用相机，再从 viewer 回读；
   - Step0 用 pyqtgraph ViewBox（`step0_explore_tab`、`compare_strip`）；
   - ui / viewer 里有约 142 处 autoRange / setRange / fit；与 devicePixelRatio 有关的只有 3 处。
3. **A0.5 引擎身份范围修复**：必须在安装 pyarrow 之前完成。
   - 问题：`seg_runner/runner.py:32-49` 的 `engine_identity` 含 `lock_hash`，它对整个 `conda-linux-64.lock` 和 `requirements-pip.txt` 取哈希。
   - 这个身份整体用于比对：`core/preseg_contract.py` 以及 `workers/segment_merge_worker.py:2311`。
   - 测试：`tests/test_preseg_contract.py`。
4. 之后按计划顺序：A1 → A2a / b / c → A3 → A4 → A5 → 六个启动门 → TMA Foundation。

### 本机环境与限制
- 硬件：WSL2，RTX 3060 Laptop 6 GB，约 10 GB 内存（`.wslconfig`：memory=11GB，swap=24GB）。
- 环境：`/home/ming/micromamba/envs/fusion_mesmer`，其中有 numba 0.67、zarr 2.18.3（v2 格式）、tifffile、anndata 0.11.4。
  - **没有** pyarrow、duckdb、polars、ome-zarr、spatialdata。
  - NGFF 按 0.4 版用现有 zarr 2.18 写，**不装 ome-zarr-py，不升级 zarr v3**。
- 离屏测试命令：
  ```bash
  cd /home/ming/fusionflux/Fusion_analysis
  E=/home/ming/micromamba/envs/fusion_mesmer
  LD_LIBRARY_PATH=$E/lib QT_QPA_PLATFORM=offscreen KERAS_BACKEND=tensorflow \
    $E/bin/python -m pytest -q -p no:cacheprovider tests/<模块>.py
  ```
  单独运行脚本时，需要设置 `PYTHONPATH=/home/ming/fusionflux`。
- **真实 GL 测试**的环境变量：
  `QT_QPA_PLATFORM=xcb PYOPENGL_PLATFORM=glx MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA BLOCK01_REQUIRE_STEP1_GPU=1`
  
  D3D12 驱动在**同一个进程里建到约第 5 个 GL 上下文就会中止**，所以每条 GPU 测试要单独一个进程。
- **回归**：
  - 每个模块单独一个进程。
  - 真实引擎的模块要**逐个顺序跑**：test_step2_runner_path、test_step2_engine_unified、test_step2_legacy_stop、test_seg_runner*、test_preseg_run、test_label_pyramid、test_step2_keeps_nuclei。
  - 用 `git archive HEAD | tar -x -C <临时目录>` 导出一份 HEAD 代码，同样跑一遍，逐条对比，只看新增的失败。
  - grep 失败行之前，先用 `sed 's/\x1b\[[0-9;]*m//g'` 去掉颜色码。
- **已知失败（HEAD 上也存在）**：
  - Mesmer：本机没有模型；
  - `test_hq_marker_segmentation.py` 2 条：HQ 不再维护；
  - StarDist 偶发 1 像素差：`test_seg_runner`、`test_seg_runner_engines`、`test_step2_runner_path`、`test_preseg_run`，单独重跑能通过；
  - `test_global_channel_dock.py` 1 条、`test_step1_channel_panel.py` 1 条：字体问题；
  - `test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`；
  - `test_step1_montage_view.py` 跑到第 27 条后 Qt 中止；
  - `test_step0_channel_conditioning.py` 卡住；
  - 真实 GL 下 `test_step1_gpu_takeover.py` 的厂商名断言失败。
- **磁盘（重要）**：详见 memory 里的 `wsl-disk-and-memory`。
  - C: 盘剩约 40 GB。
  - WSL 里删文件后，要用户执行 `wsl --shutdown` 并运行 `diskpart /s C:\Users\96461\compact_wsl_diskpart.txt` 压缩 vhdx，空间才会回到 C: 盘。
  - 真实引擎运行会让 Windows 页面文件临时增长约 15 GB。
  - C: 低于约 20 GB 时，不要跑完整的真实引擎回归。
  - 长任务用 `setsid nohup … &` 启动，并写一个完成标记文件。断网会结束会话，也会杀掉后台任务。
  - 每块做完后清理 bench 和 scratch 的输出。
- 用户真机启动命令：`cd /home/ming/fusionflux && /home/ming/micromamba/envs/fusion_mesmer/bin/python -m block01.main`
- 数据：
  - `~/fusion_data/test1/`：带 tophat 的工作区 `rois/full_wsi_20260927_121444_6bad`，运行 `seg_20260927_124316_stardist_nuclei_expansion`，含核 zarr 和「核 → 细胞」对应表；
  - Step4 的实测结果在 `~/fusionflux/bench_step4/`。
- 验证脚本：`scripts/verify_step4_s4{1,2,3}.py`、`scripts/probe_seam_merge.py`。

### v15 已完成的最后几块（都已真机验收并推送）
| 块 | 提交 | 内容 |
|---|---|---|
| S4-1 | `de80ec5` | 严格的定量来源（fail-closed）+ 流式 Numba 内核 |
| N3 | `5d8cb59`、`c9f53a3` | Step2 分块接缝按连通分量整体裁决 |
| S4-2 | `50e8290` | 每个主对象一个 h5ad，内存有上限 |
| N4 | `c50bb4c` | 过滤超大伪影 + 修复监控器的 NameError |
| S4-3 | `f93f195` | 选定 marker 的分布统计 + Crofton 周长 / 圆度 |

### 待观察 / 未决
- 偶发：Step1 viewer 全黑，提示 `fine budget refused for DAPI`，未能复现。
- 首次进入 Step3 时，GPU `resizeGL` 让 GUI 卡顿约 6.5 s。
- Step2 完成对话框里的「→ Feature Extraction (Step 4)」按钮发出的也是 `open_qc_requested`（会进入 Step3）。
- 以上三项只有在 A0 查出与位移同源时才并入 A1，否则留在待办。
- 需要真实 WSI 的补充验收，等服务器修好再做，**不阻塞 TMA**（计划 §8）。
