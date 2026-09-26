# 交接提示词（2026-09-26，新执行窗口使用）

把下面「提示词」一节整段交给新窗口。

---

## 提示词

你接手 Block01（多重免疫荧光 WSI → 单细胞表达矩阵的 PyQt5 应用）的开发。仓库在 `/home/ming/fusionflux/Fusion_analysis`，分支 `v15-interactive-channel-workspace`。请用中文和用户交流。

### 开始前必须做
1. 阅读 `AGENTS.md`、`UI_SURFACE_RULES.md`、`docs/P0_SCOPE_RULES.md`，以及 `docs/step1_presegmentation_redesign_plan.md` 的状态行、修订记录、第五节末尾的块 L / F / U1 / S 记录和第六节（「后续计划」「已知的已有问题」）。
2. 核对 `git status`、HEAD 和分支；工作区应当是干净的，最新提交是本文件所在的提交。**所有提交都只在本地，没有 push**；push 必须用户明确授权。
3. 本仓库已设置局部 git 身份（`964612858j5 <mingyuanluan1@gmail.com>`，与历史提交一致）。

### 工作方式（用户一直这样要求）
- **先只读调查，再写申请，批准后才实施。** 申请写明：必要性、做法、白名单（文件和函数）、不改的范围、风险、验收门。实施中发现要超出白名单，立即停下申请，不要先做完再说。
- 用户常把「独立审核意见」贴进来：逐条核对代码后如实回应，属实就修订申请，不属实就说明依据。审核意见不是授权。
- 小步实施：每一步单独测试、单独提交；**提交只在用户授权后**；不 reset / amend / rebase；不整仓暂存。
- 每块完成后在计划文档里写执行记录（范围、改动、测试结果、真机验收状态、advisory），并同步 `UI_SURFACE_RULES.md`（界面有变化时）。
- 自动测试不能代替真机验收；最终以用户在真机上的确认为准。
- 不对真实项目做写入探测：`~/fusion_data` **不在**测试写保护范围内（写保护只保护 `config.py` 里的两个路径）。需要真实数据时只读，或复制到临时目录。

### 本机环境（WSL2，RTX 3060 Laptop 6 GB，10 GB 内存）
- 运行环境：`/home/ming/micromamba/envs/fusion_mesmer`（按 `envs/fusion_mesmer/` 的锁文件重建）。编译依赖的 gcc 在另一个环境 `/home/ming/micromamba/envs/buildtools`。
- 离屏跑测试（Qt 需要环境自己的库）：
  ```bash
  cd /home/ming/fusionflux/Fusion_analysis
  E=/home/ming/micromamba/envs/fusion_mesmer
  LD_LIBRARY_PATH=$E/lib QT_QPA_PLATFORM=offscreen KERAS_BACKEND=tensorflow \
    $E/bin/python -m pytest -q -p no:cacheprovider tests/<模块>.py
  ```
- 回归做法：每个模块单独一个进程、4 个并行；用 `git archive HEAD | tar -x -C <临时目录>` 导出 HEAD，同样跑一遍，逐条对比失败列表，只看新增失败。
- 本机与 HEAD 相同的已知失败（环境差异，不是缺陷）：`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`、`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`、`test_step0_step1_display_isolation.py::test_step0_work_does_not_make_step1_load_or_redraw`；`test_step1_montage_view.py` 在第 27 条后 Qt 中止（WSL GPU/EGL）；5 个 GPU 模块不收集测试。
- 启动程序（用户自己在真机上验收）：`/home/ming/fusionflux/block01` 是指向仓库的软链接，
  `cd /home/ming/fusionflux && /home/ming/micromamba/envs/fusion_mesmer/bin/python -m block01.main`。
- WSL 下弹窗可能出现在屏幕最左上角，用户可能看不到；报告「没反应」时先请用户贴终端输出。
- Mesmer 模型不在本机（DeepCell token 暂时申请不到），Mesmer 相关测试会跳过并注明「未验收」，**不要用 mock 顶替**。
- 数据：`~/fusion_data/cropped_region.ome.tif`（29 通道，15437×16215，uint8，金字塔 1/4/16×）；用户的测试项目在 `~/fusion_data/test1/`。

### 已完成（本轮）
预分割计划的块 P、A1、A2、B、C、D、V0、V2（Step2 经 `seg_runner` 执行 Step1 交接）、E（Save 只认预分割 `Use`，旧 Phase1/Phase2 面板和 Patch Results 标签页隐藏）；计划外的 L1（Save 只保留进度弹窗）、L2（Step2 参数面板放左栏、与 Step0/Step1 同宽联动、Step2 不显示 Channels）、F（`Load weights` 只读 `step1_session.json`）、U1（Step1 不再写 `fusion_config.json`，改为会话里的 `last_save`）、Results 从上往下排列、S（`Load Previous Step1 Session` 每次弹对话框、拒绝原因上屏）。除 Mesmer 外都已通过用户真机验收（块 S 的最终提示文字尚待用户在真机上看一眼）。

### 下一步（用户排定的顺序，都未启动，须先申请）
1. **Step2：旧路径 ROI 模式中途 Stop 仍登记成功。** 没有契约的参数文件（旧路径）在 ROI 模式下中途 Stop，外层仍会汇总、`_register_completed_result()` 并发 `finished`（`workers/segment_merge_worker.py` 的 ROI 外层循环，参见 V2 第 3 步里契约路径的修法：`_ContractStopped`）。先只读核实，再申请。
2. **Step3 重设计**：做成和 Step1 一模一样的布局和设置——左侧通道面板，右侧组织图像，可叠加分割 mask，可拖动、缩放，Tissue Navigator 空降和 patch；用户可以画 ROI，但不产生任何下游影响。之前从未审查过 `ui/step3_page.py`，先做只读调查，列出可复用的 Step1 组件（viewer、GPU 显示层、全局通道 dock、Tissue Navigator、patch 选择器）和 Step3 现有功能，再写申请。
3. **项目 / 会话架构**（最后做）：打开别的项目并切换一切；S2 的调查结论写在计划文档的块 S 里。切换数据集后 Step2 残留上一个项目的路径、正在跑的分割不停——这一条随它一起治理。

### 尚待用户确认的事
- 计划文档 V2 执行记录里，三条 2026-09-25 的裁定（新旧参数文件按版本号区分、裁定 A、手动模式丢掉契约）措辞标着「待用户确认的补记」。
- U2（`step1_fusion_settings.json` 并入会话）暂缓。
