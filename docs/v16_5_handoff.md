# v16_5 交接提示词：Step1 查看器 Rust 内核（从 R0 开始）

把下面整段作为新执行窗口的第一条消息。

---

你是 Block01 项目 v16_5 的执行窗口，任务是按已批准的方案，把 Step1 查看器的性能核心改用 Rust 实现。用户已批准方案 v2.1 的全部 8 项决定。**本窗口只做 R0。** R0 结束后报告并等用户批准，再进入 S-Raw。

## 一、开工前必须做

1. 仓库：`/sda1/Fusion/analysis_pipline/block01_v16`。
   - 分支必须是 `v16_5`；方案批准时 HEAD 为 `docs/v16_5_handoff.md` 所在的提交。
   - 先 `git status`、`git branch --show-current`、`git log --oneline -3` 核对。
   - `cufile.log` 的修改是运行时产生的，不提交、不还原。
2. 通读：
   - `AGENTS.md`、`UI_SURFACE_RULES.md`、`docs/P0_SCOPE_RULES.md`；
   - **方案全文 `docs/v16_5_rust_viewer_plan.md`（v2.1，已批准）**；
   - `docs/v16_A9_application.md` 的 §44（真正 20 通道基线和测量工具）、§45（Python 方案作废的原因和饥饿分析）、§38；
3. **v16 已冻结**为标签 `v16-final`（`8c21be9`）。标签挡不住推送，所以必须做到：
   - 永远不在 v16 上提交，永远不推送 v16；
   - 只在 `v16_5` 上工作。

## 二、用户的工作规则（一直这样要求）

- **回复用中文**，说日常用语，举例说明，不要用内部代号（如 R0、S-Raw、§44）。必须提到时，先用一句话说清是什么。
- **流程**：只读调查 → 写清做法和文件范围 → 用户批准 → 实施 → codex 代码审核 → 定向回归 → 实测 → 提交 → 推送。
  - 用户已授权推送 v16_5：`git push git@github.com:964612858j5/Fusion_analysis.git v16_5`。
  - 提交信息结尾加：`Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
  - 不要 reset、amend、rebase，不改 `/root/.gitconfig`。
- **审核者只审核，不执行**：
  - 调查和实现都由本窗口做；codex 和 Fable 只审核你的结论、方案或代码。
  - codex 代码审核：`codex exec --skip-git-repo-check -s read-only -m gpt-6-astra -c model_reasoning_effort=low "<审核说明>" > <文件>`。方案类审核用 `model_reasoning_effort=high`，或者用户指定的级别。
  - Fable 审核：用 Agent 工具，model 设为 fable，effort 设为 medium。
  - 等 codex 结束时，`pgrep -f` 的匹配串不要包含等待脚本自己的命令行，否则等待脚本会匹配到自己，永远不会结束。
- **回归测试按块定向**：只跑受影响的模块，4 个并行。全量回归只在大块结束或改动共享代码时跑。
- **测试**：
  - Python 环境是 `/root/micromamba/envs/fusion_mesmer/bin/python`（Python 3.10，PyQt5 5.15）。
  - 从 `/tmp` 运行：`PYTHONPATH=<仓库父目录>:<仓库> python -m pytest -q -p no:cacheprovider --rootdir=<仓库> --confcutdir=<仓库> <仓库>/tests/<模块>.py`。仓库根目录的 conftest 会把 `block01` 指向本目录。
  - GPU 测试要真实显示：`DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority QT_QPA_PLATFORM=xcb BLOCK01_REQUIRE_STEP1_GPU=1`，并 `unset LD_LIBRARY_PATH`。
  - 冻结点上已知会失败的测试，不是你引入的：
    - `test_step1_gpu_takeover::test_a_cold_patch_still_arrives_tile_by_tile_from_its_worker`；
    - `test_preview_source_provider::test_save_invalidation_repulls_into_workbench`；
    - `test_step1_gpu_sources::test_hardware_fbo_partial_coarse_never_shows_a_centre_first_channel`。

    怀疑是你引入的失败，先用 `git worktree` 在 `v16-final` 上对照。
- **显示 :1 上不要同时跑图形测试和性能测量**，也不要杀其他会话的进程。
- **报告时如实说**：测试失败就贴输出；跳过的就说跳过；没测的不说"应该可以"。

## 三、本次要做的事（R0：骨架和探针，预估 3–5 个工作日）

**范围只在服务器上。** 用户裁决：笔记本（WSL2）和原生 Windows 都不在本次验收范围，不要在笔记本上安装、探测或测试，也不要请用户去笔记本上操作。跨平台约束（第 7 条）照样遵守。

按方案 §8 的 R0 行和 §3、§5.2、§6 执行。

1. **工具链**：
   - 服务器上用 rustup 安装 Rust，装在 `/root/.cargo`，需要联网，用户已批准；
   - 装 maturin；
2. **crate 骨架**：`fusion_viewer_core`（PyO3 + maturin，abi3-py310）。
   - 位置和目录结构先报用户批准，建议放在仓库内单独目录，如 `native/fusion_viewer_core/`；
   - 实际构建 wheel，并在 fusion_mesmer 环境导入；
   - 依赖许可只用 MIT、Apache、BSD、zlib；
   - Odon（`/sda1/Fusion/analysis_pipline/odon`，GPL-3）只参考设计，不拷代码和常量；每个模块写明参考了哪些设计。
3. **解码探针**：
   - Rust 读出原始块并解压：TMA3 是 OME-TIFF，deflate，uint8，512² 块，69 通道；qptiff 是 TMA4，LZW；
   - 和现有 Python 读取（`viewer/raw_tile_provider.py` 的 `_ChunkPlan` 路径）逐字节比较；
   - 读速按 deflate/LZW × 冷/热分别报告，并测 6 线程、界面线程同时忙的情况；
   - 5 块/ms 是吞吐目标，不是淘汰条件。

   文件路径：
   - TMA3：`/sdb1/cop/Kevin/TMA3/FINALRUN_updated_TMA3_Scan1_bgcorrected.ome.tiff`
   - TMA4：`/sdb1/cop/Kevin/FINALRUN_updated_TMA4_Scan1.qptiff`
4. **GL 生命周期探针**（方案 §3 全部条目），在服务器上测：
   - Rust 通过 Python 传入的 Qt `getProcAddress` 加载 GL；
   - 画到 `defaultFramebufferObject()`；
   - 测首次显示、resize、DPI 变化、reparent（接 `aboutToBeDestroyed`，重建资源）、隐藏后返回、关闭顺序；
   - 同一上下文里另一个绘制者共存；
   - paint 前后用完整的 glGet* 快照比对状态；产品热路径不做同步查询。
   - GPU 测试保持"一个进程一条"的写法，方便将来在笔记本上跑。
5. **GIL 探针**：释放 Python 锁后重新拿锁的代价。同时有忙碌的 Python 线程时，界面心跳间隔会怎样。
6. **定下接口形状**（只写定义和文档，不实现全部功能）：
   - 方案 §5.2 的适配层完整接口；
   - 最小 SourceManifest，复用现有 `SourceIdentity`，只补操作系统层面的文件一致性检查；
   - 新旧后端切换接口；
   - 跨平台通知：socketpair + QSocketNotifier，合并写入、非阻塞、读空，写明所有权和关闭顺序。
7. **跨平台约束从第一行代码开始遵守**（方案 §6）：
   - 定位读封装成一处（Linux `read_at`，Windows `seek_read`）；
   - 路径用 PathBuf；
   - 不用 GLX/WGL/EGL 的专有接口。

**R0 的关口**：
- 解码逐字节一致；
- GL 生命周期在服务器上可靠；
- 没有严重兼容问题。

不过关就停下来报告原因，不要绕过。

**R0 不做**：
- 不改 Step1 产品代码路径，不接入 mount；
- 不改调度器的启动方式（第 8 项已批准，但它属于 S-Raw）；
- 不做 Zarr、Fusion、标签；
- 不涉及笔记本。

## 四、R0 之后（本窗口不做，供了解）

- **S-Raw（薄切片，7–10 天）**：
  - 原始 uint8 + Overlay，Rust 完成规划、读图、缓存、上传，绘制用虚拟纹理的最小子集；
  - Rust 模式下旧的 Python 图块调度器不启动；
  - 先确认测试场景的可比性：全片 ROI、20 通道全为原始来源、跳过 ROI 裁剪不改变截图；
  - 用 §44 的工具和 Odon、和 `v16-final` 配对实测；
  - 继续的 4 个条件见方案 §8。
- 之后依次是：完整读图 → 完整绘制 → Step3 和生命周期 → 服务器验收。合计约 30–46 个工作日；R0 + S-Raw 做完后按实测重估。

## 五、测量工具和基线（S-Raw 起要用；R0 只需知道在哪）

- **基线**：真正 20 通道配对结果在 `/sda1/Fusion/a9_bench/s0_pair20.txt`。
  - 我们这边的运行标签是 `c20c_*`（冷）、`c20w_*`（热）、`c20off_*`（关掉外部观察）；Odon 是 `r20c_*`、`r20w_*`。
  - 每块图的时间分解在 `bench_a9/tiletl_c20*.json`。
  - **作废的 10 通道数据**（`s0_pair.txt`、`b20*`、`q20*`）不要引用。
- **脚本**：
  - 批量：`/sda1/Fusion/a9_bench/run_s0fix.sh`，以及 Odon 补测 `run_odonfix.sh`，带"画面没变就判无效重来"的检查；
  - 单轮：`bench_a9/run_deep3.sh <标签>`，场景用环境变量 `SCEN=kxdeep20bg.json`，`OBS=0` 关掉外部观察；
  - 分析工具在仓库 `scripts/`：`a9_pairreport.py`（配对汇总，用法见文件头）、`a9_tiletl.py`（每块图时间分解）、`a9_stopshots.py`、`a9_shotdiff.py`、`a9_pixwatch.py`、`a9_odon_bench.py`、`a9_xwheel.py`、`a9_drop_file_cache.py`。
- **测前必须核对程序实际画了 20 个通道**（看程序内记录 `gpu.frame` 的 `channels=`）。上一次就因为场景只勾了 10 个通道，整份基线作废。
- 服务器图形测试的坑见记忆文件 `a9-server-bench-2026-10.md`：显示器选择、空闲锁屏会降到 1 fps。

## 六、R0 结束时交给用户的报告（中文、日常用语）

- 每个探针的结果和数据（读速分 4 种情况）；
- GL 生命周期逐条通过与否；
- 定下来的接口和 SourceManifest 格式；
- 按实测修正后的 S-Raw 范围和工期；
- 改了哪些文件；
- codex 审核结论；
- 提交号。

然后等用户批准再开始 S-Raw。
