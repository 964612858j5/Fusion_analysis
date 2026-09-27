# 交接提示词（2026-09-27，新执行窗口使用）

把下面「提示词」一节整段交给新窗口。

---

## 提示词

你接手 Block01（多重免疫荧光 WSI → 单细胞表达矩阵的 PyQt5 应用）的开发。仓库 `/home/ming/fusionflux/Fusion_analysis`，分支 `v15-interactive-channel-workspace`，远端 `origin`（GitHub）。请用中文和用户交流。

### 开始前必须做
1. 阅读 `AGENTS.md`、`UI_SURFACE_RULES.md`、`docs/P0_SCOPE_RULES.md`，以及 `docs/step1_presegmentation_redesign_plan.md` 的：状态行、修订记录（最新 v3.64 起）、第五节中 **「Step3 重设计 — 只读调查与用户裁定」**（含「第 ⑤ 步改定」与「拟分步」）、**块 ④a / ④b / ④c / N2**，以及第六节「后续计划」（第 0 条是用户排定的总顺序）与「已知的已有问题」。
2. 核对 `git status`、HEAD、分支。HEAD 应为本交接文件所在的提交（其前一个是 `e5f1ed8` 块 N2）。
3. 仓库局部 git 身份已设（`964612858j5 <mingyuanluan1@gmail.com>`）。提交信息结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。**提交与 push 须用户明确授权**（用户习惯「验收后提交推送」；只说「提交」时只提交不推送）。

### 工作方式（用户一直这样要求）
- **先只读调查 → 写申请（必要性、只读调查结论、做法、白名单、不改的范围、风险、验收门、请用户裁定）→ 用户批准 → 提交申请 → 实施**。实施中发现要超出白名单（包括白名单外的测试），立即停下，用 AskUserQuestion 说明原因与改法，得到授权再改。
- 用户常贴「独立审核意见」：逐条对照代码核对后如实回应，属实就修订申请（版本号 +1），不属实说明依据。审核不是授权。
- 小步实施、每块单独测试；**反向注入**（在任务临时目录的副本里故意改错，确认测试变红；没抓到就补测试）；每块在计划文档写执行记录（范围、改动、测试、反向注入、回归、真机验收、advisory），界面变化时同步 `UI_SURFACE_RULES.md` 与两份用户指南（`docs/user_guide.md`、`docs/用户指南.md`）。
- 自动测试不能代替真机验收。
- 用通俗语言向用户解释，必要时举例。
- 不对真实项目做写入探测：`~/fusion_data` 不在测试写保护范围内，只读或复制到临时目录（复制件须改写 metadata 里的绝对路径，否则仍会读写原项目）。

### 本机环境（WSL2，RTX 3060 Laptop 6 GB，10 GB 内存）
- 环境：`/home/ming/micromamba/envs/fusion_mesmer`。
- 离屏跑测试：
  ```bash
  cd /home/ming/fusionflux/Fusion_analysis
  E=/home/ming/micromamba/envs/fusion_mesmer
  LD_LIBRARY_PATH=$E/lib QT_QPA_PLATFORM=offscreen KERAS_BACKEND=tensorflow \
    $E/bin/python -m pytest -q -p no:cacheprovider tests/<模块>.py
  ```
- **真实 GL 测试（本机可跑）**：`QT_QPA_PLATFORM=xcb PYOPENGL_PLATFORM=glx MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA BLOCK01_REQUIRE_STEP1_GPU=1`（不设 ADAPTER 时 WSLg 选 Intel 核显）。本机 D3D12 驱动在**同一进程里建到约第 5 个 GL 上下文就中止**，所以 GPU 回归要**每条测试单独一个进程**（按 `--collect-only` 列出的编号逐条跑；编号里有空格，用 `while IFS= read -r`）。画过 mask 的测试进程退出时约一半概率段错误（驱动收尾缺陷，产品退出时未出现，见 ④b advisory）。
- 回归：每模块单独进程；**真实引擎模块（test_step2_runner_path、test_step2_engine_unified、test_step2_legacy_stop、test_seg_runner*、test_preseg_run、test_label_pyramid、test_step2_keeps_nuclei）逐个顺序跑**；`git archive HEAD | tar -x -C <临时目录>` 导出 HEAD 同样跑一遍，逐条对比，只看新增失败。grep 失败行前先去掉颜色码（`sed 's/\x1b\[[0-9;]*m//g'`）。离屏测试里不能弹模态对话框。回归会用到正式代码目录，跑的时候不要改代码（改副本）。
- 已知与 HEAD 相同的失败 / 问题：`test_global_channel_dock.py` 1 条、`test_step1_channel_panel.py` 1 条（字体）；`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`；`test_hq_marker_segmentation.py` 2 条（HQ 不维护）；`test_seg_runner_engines.py` 3 条（2 条 Mesmer 无模型、1 条 StarDist）；StarDist 同机偶发 1 像素差（`test_seg_runner.py`、`test_step2_runner_path.py`、`test_step2_engine_unified.py` 的 StarDist 逐像素用例，单独重跑通过）；`test_step1_montage_view.py` 第 27 条后 Qt 中止；`test_step0_channel_conditioning.py` 两边都卡住；真实 GL 下 `test_step1_gpu_takeover.py::test_the_mounted_widget_framebuffer_holds_real_gpu_pixels`（断言厂商名含 nvidia，本机是 Microsoft Corporation）。
- 启动（用户真机验收）：`cd /home/ming/fusionflux && /home/ming/micromamba/envs/fusion_mesmer/bin/python -m block01.main`。
- 数据：`~/fusion_data/cropped_region.ome.tif`（29 通道，15437×16215，uint8，金字塔 1/4/16）；测试项目 `~/fusion_data/test1/`。**Mesmer 模型不在本机**：真实 Mesmer 相关测试跳过；N2 经用户批准用替身引擎测试了 nuclear-guided 的核配对逻辑。

### 2026-09-26 至 27 这两个窗口已完成并推送
- 块 K / M / N / 2a / 2b / 2c-1 / 2c-2 / S5（见计划文档，均真机验收通过）。
- **块 ④a**（`e5a7164`）`core/step3_masks.py`：列运行、选运行、按方法（及 N2 起按 `label_store`）解析细胞 / 核 mask、完整校验金字塔、按 viewer 分块读标签、补生成金字塔（写盘 → 内存 → 只有第 0 级）。
- **块 ④b**（`11a2fcc`）GPU 标签渲染：`Step1GpuLayer(labels=True)`（R32UI 屏幕编号图 + `shown` 目标共用模板、轮廓 / 填充着色器 `ui/shaders/step1_gpu_labels.frag`、独立 256 MB 标签纹理预算）；`ui/step3_label_binding.py`（一个后台读取线程、可见块 + 一圈、目标层级优先、预算淘汰、暂停取消补生成）；mount 的 mask 开关与 `set_mask_sources` / `set_mask_style` / `mask_status`。
- **块 ④c**（`8e5310a`）Step3 右栏顶部一行：运行下拉框、`Cell mask ▾` / `Nucleus mask ▾` 面板、提示文字（`ui/step3_mask_bar.py` + `main_window._step3_refresh_masks` 等）。真机第 1–4、6 项通过，第 5 项（无金字塔旧运行现场补生成）**真机未验**。
- **块 N2**（`e5f1ed8`）所有计算了核的方法保留核 mask：`core/nuclei_pairing.py`（核须完全在一个细胞内，否则按三类丢弃；多核细胞全保留）；Step2 为两个 expansion 与 nuclear-guided 写核 zarr（自己的编号 1…M）与「核 → 细胞」对应表 `global_nuclei_cell*.zarr`，经 `*.partial` 改名、`label_store` 作语义入口（对标未来 Step4 流式定量）；不写核 OME-TIFF；附带修复整图模式 Mesmer metadata 的 `UnboundLocalError`。真机验收通过（nuclear-guided 的真机验收随 Mesmer 暂缓项）。

### 下一步：第 ③ 步（尚未申请）
Step3 的 patch 按钮条组件化 + patch 空降（用户裁定 11：patch 按钮条抽成 Step1 / Step3 共用组件，允许修改 Step1 并回归）。先只读调查，写申请。之后按第六节第 0 条的顺序：Step4 流式定量 → WSI 阻塞项清理 → TMA 基础 → QualityMask → TMA 批处理 → 真实数据剖析 → Rust 基础。

### 待观察 / 未决（计划文档第六节与各块记录中有详述）
- 偶发：Step0 保存 → 直接进 Step3 → 回 Step1，Step1 viewer 全黑、提示 `fine budget refused for DAPI`（未能复现）。
- 运行资源监控器 `utils/runtime_resource_monitor.py:336` 的 `NameError`（判定退回 CPU 时 Step2 收尾报错不登记）。
- Step2 完成对话框的「→ Feature Extraction (Step 4)」按钮也发 `open_qc_requested`（进 Step3）——未处理。
- `set_preview_mode(reconcile=False)` 不把模式交给 viewer；换数据集时 Step1 viewer 不关闭（advisory）。
