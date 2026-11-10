# 交接提示词：StarDist 改用 PyTorch，并在 Ubuntu 上验证与原版一致（2026-11-09）

把下面「提示词」一节整段交给 Ubuntu 服务器上的新窗口。

---

## 提示词

你接手 Block01（FusionFlux）的一个新任务块：**把 StarDist 的网络改由 PyTorch 运行，并在本机（Ubuntu + NVIDIA GPU）验证它和现在的 TensorFlow 版 StarDist 结果一致**。请用中文和用户交流，用通俗的语言，必要时举例。

### 背景
- 现在 StarDist 只能跑在 TensorFlow 上：产品环境固定 TF 2.8.4，它认不到新显卡，所以 StarDist 实际在 CPU 上跑；苹果芯片的 Mac 上也没有 TF 2.8.4。
- Mesmer 已经走过同样的路：网络改由 PyTorch 运行，同一份模型权重，前后处理不变。方案和记录在 `docs/mesmer_torch_plan.md`，代码在 `seg_runner/mesmer_torch/`，转换和对照测试是 `tests/test_mesmer_torch_*.py`。StarDist 照这个做法来。
- 用户的目标：PyTorch 版 StarDist 能在 NVIDIA GPU 和 Mac 的 GPU（MPS）上运行。**一致性验证放在这台 Ubuntu 上做**，因为 Mac 内存只有 16 GB。

### 开始前必须做
1. 代码同步：
   ```bash
   cd /sda1/fusionflux/Fusion_analysis-17
   git status; git branch --show-current; git log --oneline -3; git remote -v
   ```
   - 如果不是 git 仓库、有未提交的改动，或者分支不是 `v17`：**停下来问用户**，不要清理、覆盖或 reset。
   - 干净的话：`git fetch origin && git checkout v17 && git pull --ff-only`。更新后 HEAD 至少要到 `cda413d`（Step2 跳过切块），往前依次是 `c2c5ff9`（Mac 显示修复）和 `3dea5c9`（PySide6 收尾）。
2. 阅读：
   - `AGENTS.md`、`docs/P0_SCOPE_RULES.md`、`UI_SURFACE_RULES.md`；
   - `docs/mesmer_torch_plan.md`：照搬它的流程、权重转换、校验和许可证处理；
   - `seg_runner/engines.py`：`StardistEngine`、`behavior_identity`、`BEHAVIOR_VERSION`；
   - 设备选择的约定见 Mac 端的 Cellpose/Mesmer 方案（`docs/v17_cellpose_mac_gpu_plan.md`，可能还没推送到 GitHub）。约定是：`torch_device()` 依次选 CUDA → MPS → CPU；取消 Use GPU 时，引擎进程会拿到 `BLOCK01_ENGINE_CPU_ONLY=1`，这时用 CPU。
3. 环境：
   - 日常环境是 `/sda1/fusionflux/block01/envs/fusion_test2`（`scripts/block01` 用的解释器）。先确认里面 torch、stardist、csbdeep、tensorflow 的版本，以及 `torch.cuda.is_available()`。
   - 需要装包或建新环境时，先说明理由，**得到用户同意再装**。
4. 数据：`/sda1/fusionflux/cropped_region.ome.tif`（29 通道，DAPI 是核通道，15437×16215，0.507 µm/px）。**只读**。所有输出放在临时目录，不写进任何真实项目。

### 工作方式（用户一直这样要求）
- 流程：**只读调查 → 写方案 → 送 codex 审核 → 用户批准 → 实施 → codex 代码审核 → 测试与验证 → 报告**。方案没批准之前不写产品代码。
- codex 审核默认用 astra **low**，用户特别要求时才用 high：
  `codex exec --skip-git-repo-check -s read-only -m gpt-6-astra -c model_reasoning_effort=low "<提示>" < /dev/null > 输出文件`
  一定要加 `< /dev/null`，否则 codex 会一直等输入。
- 任务块是封闭白名单。要超出白名单就先停下问用户。新增 registry、缓存、调度器或状态机都要用户明确授权。连续两轮审核都没发现新的公共路径缺陷，就停止加固。
- 测试只写合成数据和临时目录。改动产品代码后做反向注入：故意改错，确认对应测试会变红。
- 提交和推送都要用户分别授权。提交信息结尾加：
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- 自动测试不能代替真机验收。

### 方案里至少要回答的问题
1. **移植范围**：只移植网络，也就是 `StarDist2D` 的 U-Net 加上 prob/dist 两个输出头。归一化（`csbdeep.normalize(img, 1, 99.8)`）、分块预测、多边形重建和 NMS **继续用 stardist 包自己的代码**，这样两边只有网络不同。写清楚 `predict_instances` 的哪些步骤留在 stardist 包里，接口怎么接。
2. **权重**：从 `2D_versatile_fluo`（`envs/fusion_mesmer/models.json` 里的条目，`~/.keras/models/StarDist2D/2D_versatile_fluo`）转换成 torch 权重。说明权重文件放哪、manifest 怎么写、内容校验和怎么算，参照 Mesmer 的 `prepare` / `weights`。
3. **身份**：StarDist 的 `BEHAVIOR_VERSION` 要不要升，旧的 Step1 结果怎么处理。这件事要用户裁定；Mesmer 当时是把版本加一。
4. **设备**：沿用 `torch_device()`（CUDA → MPS → CPU，取消 Use GPU 时用 CPU），和 Cellpose、Mesmer 一致。
5. **许可证**：StarDist 和 CSBDeep 都是 BSD-3。改编的代码要保留许可证和署名。

### 一致性验证（在这台 Ubuntu 上做）
分三层做，每层都写清楚数字：
1. **网络输出**：同一张输入图，分别比较 TF 网络和 torch 网络的 prob 图和 dist 图，给出最大绝对误差和平均绝对误差。输入用随机图和真实 DAPI 小块。torch 在 CPU 和 GPU 上各跑一遍，TF 在 CPU 上跑，因为这台机器的 TF 2.8.4 认不到 GPU。先确认 TF 用的是什么设备。
2. **最终分割结果**：从 `cropped_region.ome.tif` 的 DAPI 通道取至少 6 个 512×512 小块，覆盖细胞密集、稀疏、组织边缘和几乎空白的位置；再取 1 个 Step2 大小的瓦片。对比 TF 版和 torch 版（GPU 和 CPU 都要）：
   - 细胞数；
   - 一对一匹配上的比例：IoU ≥ 0.5，每个细胞最多配一个，按 IoU 从高到低配，两个方向都算；
   - 匹配上的细胞的平均 IoU；
   - 前景像素一致率；
   - 耗时。
   每个小块分开报告。
3. **产品路径**：通过引擎进程（`seg_runner`）跑一次 Step1 预分割（新跑，不复用旧结果），再跑一次 Step2，都在合成或临时项目上。确认 `hello.device` 和每个任务的 `device_used` 是 `cuda` 或 `cpu`，和预期一致。
- **通过线由用户裁定**：先报告数字，再请用户决定能不能接受。参考：Mesmer 当时的裁定是“PyTorch 和 TF 本来就不可能完全一致，优先用 GPU 版”。Cellpose 在 Mac 上的 GPU/CPU 差异，用户也已经接受了。
- 把验证脚本放在 `scripts/` 下，报告写进方案文档。脚本要不要入库，由用户决定。

### 本机注意
- 真实 GPU 测试每条单独开一个进程。
- 长任务放在后台跑，并写一个完成标记文件。
- 做完后清理临时输出。

### 先交给用户的东西
调查结论和方案初稿，以及 codex 的审核结果。然后等用户批准。
