# 交接提示词（2026-09-26 晚，新执行窗口使用）

把下面「提示词」一节整段交给新窗口。

---

## 提示词

你接手 Block01（多重免疫荧光 WSI → 单细胞表达矩阵的 PyQt5 应用）的开发。仓库 `/home/ming/fusionflux/Fusion_analysis`，分支 `v15-interactive-channel-workspace`，远端 `origin`（GitHub）。请用中文和用户交流。

### 开始前必须做
1. 阅读 `AGENTS.md`、`UI_SURFACE_RULES.md`、`docs/P0_SCOPE_RULES.md`，以及 `docs/step1_presegmentation_redesign_plan.md` 的：状态行、修订记录（最新 v3.47）、第五节中 **「Step3 重设计 — 只读调查与用户裁定」「第 ④ 步 — Step3 整张图 mask 浏览：调查结论与用户裁定」「块 ④a」** 三节，以及第六节「后续计划」「已知的已有问题」。
2. 核对 `git status`、HEAD、分支。HEAD 应为 `35024aa`（块 S5）。**工作区里有两个未提交的文档改动**：计划文档（第 ④ 步调查与裁定、块 ④a v2 申请并标为已批准）与本交接文件。先问用户是否授权提交并推送这两个文档，再开工。
3. 仓库局部 git 身份已设（`964612858j5 <mingyuanluan1@gmail.com>`）。提交信息结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。**push 须用户明确授权**（本窗口用户一直是「验收后提交推送」）。

### 工作方式（用户一直这样要求）
- **先只读调查 → 写申请（必要性、做法、白名单、不改的范围、风险、验收门）→ 用户批准 → 实施**。实施中发现要超出白名单，立即停下申请（本窗口多次这样处理：白名单外的测试须用户授权后才改）。
- 用户常贴「独立审核意见」：逐条核对代码后如实回应，属实就修订申请，不属实说明依据。审核不是授权。
- 小步实施、每块单独测试；每块在计划文档写执行记录（范围、改动、测试、回归、真机验收状态、advisory），界面变化时同步 `UI_SURFACE_RULES.md` 与两份用户指南（`docs/user_guide.md`、`docs/用户指南.md`）。
- 自动测试不能代替真机验收；GPU 路径在本机测试环境大部分跳过，以真机为准。
- 用通俗语言向用户解释，必要时举例（用户明确要求过）。
- 不对真实项目做写入探测：`~/fusion_data` 不在测试写保护范围内，只读或复制到临时目录。

### 本机环境（WSL2，RTX 3060 Laptop 6 GB，10 GB 内存）
- 环境：`/home/ming/micromamba/envs/fusion_mesmer`。
- 离屏跑测试：
  ```bash
  cd /home/ming/fusionflux/Fusion_analysis
  E=/home/ming/micromamba/envs/fusion_mesmer
  LD_LIBRARY_PATH=$E/lib QT_QPA_PLATFORM=offscreen KERAS_BACKEND=tensorflow \
    $E/bin/python -m pytest -q -p no:cacheprovider tests/<模块>.py
  ```
- 回归：每模块单独进程；轻量模块 4 并行，**真实引擎模块（test_step2_runner_path、test_step2_engine_unified、test_step2_legacy_stop、test_seg_runner*、test_preseg_run、test_label_pyramid）逐个顺序跑**（并行会超时 / 崩溃）；`git archive HEAD | tar -x -C <临时目录>` 导出 HEAD 同样跑一遍，逐条对比，只看新增失败。grep 失败行前先去掉颜色码（`sed 's/\x1b\[[0-9;]*m//g'`）。离屏测试里不能弹模态对话框（会卡死）。
- 已知与 HEAD 相同的失败 / 问题：`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`、`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`（字体）；`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`；`test_hq_marker_segmentation.py` 2 条（HQ 不维护）；`test_step0_method_prefetch.py` 1–2 条（负载偶发）；StarDist 同机偶发 1 像素差（`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call` 等）；`test_step1_montage_view.py` 第 27 条后 Qt 中止；`test_step0_channel_conditioning.py` 两边都卡住（手动结束、不参与对比）；5 个 GPU 模块不收集；`test_step1_gpu_takeover.py` 34 条无真实 OpenGL 跳过。
- 启动（用户真机验收）：`cd /home/ming/fusionflux && /home/ming/micromamba/envs/fusion_mesmer/bin/python -m block01.main`。
- 数据：`~/fusion_data/cropped_region.ome.tif`（29 通道，15437×16215，uint8，金字塔 1/4/16）；测试项目 `~/fusion_data/test1/`。Mesmer 模型不在本机（相关测试跳过、标「未验收」，不用 mock）。

### 本窗口已完成并推送（都已真机验收，除注明）
- 块 K（`bc280d1`）旧路径 ROI 模式 Stop 不登记成功。
- 块 M（`5b45d79`）Step2 的 8 个方法统一走 `seg_runner` 引擎子进程；Use GPU 两模式保留；StarDist 模型名固定；引擎身份只核对种类。
- 块 N（`37e56e1`）Step2 生成标签金字塔 `label_pyramid_<ROI>.zarr`（`core/label_pyramid.py`，与切片层级网格对齐）。
- 块 2a（`b7af9f4`）viewer 暂停 / 恢复；Navigator 视野框只由前台 viewer 发布。
- 块 2b（`66cb556`）Step3 与 Step1 共用显示范围 step1、fusion 草稿、通道面板行、Tissue Preview 上下文（Step2 不变）。
- 块 2c-1（`1e33fd7`）新 Step3 页面（`ui/step3_page.py` 只负责摆放，控件由 `main_window._build_step3_page` 构建），删除旧页面。
- 块 2c-2（`7382a39`）Step3 的第二个整张图 viewer（常驻、后台暂停、相机共享、Navigator 点击路由、失败回退、换数据集关闭）。真机第 1–3 项通过；第 5 项（重启后直接进 Step3）待有「重新加载历史会话」入口再测。
- 块 S5（`35024aa`）Step3 的 Navigator：ROI 冻结（继承 Step0/Step1），patch 可增删移动改名并全局同步。

### 下一步：实施块 ④a（已批准，未启动）
按计划文档「块 ④a（申请 v2）」执行：新增 `core/step3_masks.py`（无 Qt）——`list_runs`、`choose_run`、`resolve_masks`（细胞 / 核**按方法分类**，见表）、读取前完整校验、坐标来源表（证据不足不显示）、`pyramid_path_for`（移入 `core/label_pyramid.py`，Step2 worker 改用，逐字相同）、`ensure_pyramid`（写盘 → 内存 → 只有第 0 级；取消直接结束）、`read_label_tile`（与 `Step1GpuBinding._world_rect` 同约定；无金字塔时粗层返回「不可用」而非全零）、`outline_reference` / `fill_colour`。新测试 `tests/test_step3_masks.py`。④a 只交付「数据层自动验收通过」，真机随 ④c。
之后：④b（GPU 标签渲染：R32UI 编号图 + 轮廓 / 填充着色器、独立标签读取后台线程、64 MB 标签纹理缓存、mount 的 mask 开关、只 Step3 启用、无 GPU 时提示）；④c（Step3 顶部一行：运行下拉框、细胞 mask ▾、核 mask ▾ 各一个下拉面板设显示 / 颜色 / 透明度 / 线宽 / 轮廓或填充，缺失的类型禁用；提示文字；Step2 新结果与换数据集的刷新）。每块先写申请。

### 待观察 / 未决（计划文档第六节与各块记录中有详述）
- 偶发：Step0 保存 → 直接进 Step3 → 回 Step1，Step1 viewer 全黑、提示 `fine budget refused for DAPI`（再次操作无法复现）。推测第一次打开套用共享相机时窗口尚未排好尺寸。再出现时请用户先试缩放 / 拖动 / 改窗口大小并记下顺序。
- 运行资源监控器 `utils/runtime_resource_monitor.py:336` 的 `NameError`（判定退回 CPU 时 Step2 收尾报错不登记）。
- Step4 优化（先让 Step4 改读 zarr，再清理 Step2 的 `.dat` 与重复 TIFF）；长期：原始切片改用 OME-NGFF（Step4 优化后再议）。
- 第 ③ 步 patch 按钮条组件化（排在 ④ 之后）。
- `set_preview_mode(reconcile=False)` 不把模式交给 viewer；换数据集时 Step1 viewer 不关闭（advisory）。
