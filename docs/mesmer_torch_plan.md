# Mesmer 改用 PyTorch 运行 —— 方案 v4.1（2026-10-25，按用户裁定修改；codex astra high 第 5 轮通过，待用户授权）

**用户裁定（2026-10-25）**："PyTorch 和 TensorFlow 本来结果就不可能完全一致，不论结果如何都优先使用 GPU PyTorch 版本的 Mesmer。"

v3 → v4：
- 目标从"与 TF 完全一致"改为"**GPU PyTorch 成为唯一的 Mesmer**"；没有 GPU 时用 CPU PyTorch。
- 身份：`BEHAVIOR_VERSION["mesmer"]` 从 1 升到 2。TF 后端**退出产品路径**，因此同一时间只有一种 Mesmer 行为，全局升版本就足以区分新旧结果。v1 审核的反对理由是"两种行为同时保留"，现在不再成立。旧的 Mesmer Step1 结果在 Step2 会提示版本不同；开发期允许这种不兼容升级（用户 2026-10 裁定）。
- 同一身份内 GPU 和 CPU 有细微差别，和 Cellpose 现行做法一致（它同样用 torch，GPU/CPU 共用一个身份）。
- 和 TF 的对比从"启用门槛"改为"**移植正确性检查**"：用来发现写错的移植，差异报告交给用户，不阻止启用。
- v3 的"GPU 不达标就强制 CPU"和"另立身份"两条全部取消。


v2 → v3（codex 第 2 轮：P1×1、P2×1、P3 若干；身份部分已被 v4 取代，仅作历史）：
- **本任务只启用通过"完全一致"门槛的路径**。达不到门槛的路径（例如 GPU）在本任务中**不可用**，因此身份始终是静态的、正确的。"另立身份"的 GPU 路径整体留作后续任务，另写方案，届时再打通启动前身份、Step1 复用和 Step2 提示这条链。
- 改编代码的许可证交付物写全。
- 300×200 改为"同样拒绝"，不复刻异常文本；奇数补边多出的像素放在高侧；样条拼接有条件分支；`mesmer_available()` 用的是 `find_spec`；转换脚本的自检依赖 `graph.py`，实施顺序已调整。

v1 → v2 的改动（codex astra high 第 1 轮，P1×3、P2×4、P3；门槛部分已被 v4 取代，仅作历史）：
- 通过门槛改为"最终 mask 完全一致"。GPU 不达标时另立模型身份，不用"版本号加一"把差异放行。
- 补全处理顺序表；小图走 reflect 补边；记录旧的边界行为。
- 一次网络推理 = 每个小块只算一次；两种分割**各自** watershed、**各自**按单通道缩放回原尺寸，阈值路由不变。
- GPU 确定性设置写全；首版固定 batch=1。
- 验收补上 wrapper 差分、缩放路径、三种真实输入、Step1→Step2 正常流程。
- manifest 并入现有工具；provenance 覆盖新子目录。
- 授权：wrapper 改编自 DeepCell 代码，同样要保留许可证和归属。

## 1. 目标与边界

**目标**：Mesmer 的网络改由 PyTorch 运行（同一模型、同一权重、同一前后处理），**默认在 GPU 上运行**，成为唯一的 Mesmer。结果和 TF 版只有数值精度带来的细微差别。好处：
- Mesmer 能用 GPU。TF 2.8.4 认不到 RTX 4090（`envs/fusion_mesmer/README.md:33-34`）。
- Mesmer 推理不再经过 TF 和 deepcell。

**不做**：
- 不换模型：vanvalenlab/torch-mesmer 是重新训练的另一个模型。
- 不改阈值语义、输入通道、min_size 后处理，也不改 Step1/Step2 交接契约。
- 不删 TF：StarDist 仍需要它。
- 不宣称已解除整个产品对 deepcell 和 Python<3.11 的依赖。Mesmer 可用性检查（`utils/segmentation_param_schema.py:138`）仍用 `find_spec("deepcell")` 检查它是否安装，deepcell_toolbox 0.12.1 也声明 Python<3.11。这两项列为后续范围。

## 2. 现状（已核对）

- `seg_runner/engines.py`：
  - `MesmerEngine`（:214）把 TF 模型交给 `deepcell.applications.Mesmer`，调用 `predict(batch, image_mpp, compartment, batch_size=1, postprocess_kwargs_*)`。
  - `run()`（:264）：
    - `mesmer_nuclear_guided` 对同一块图调用两次 `predict`，网络算两遍；
    - 用户阈值（`maxima_threshold`、`interior_threshold`）只作用于 `MESMER_THRESHOLD_TARGET` 指定的那个分割，外层 `postprocess_mask(min_size)` 也一样；
    - nuclear_guided 的细胞核输出用库默认值，不做 min_size。
- 输入：`core/preseg_input.py` 的 `model_input`，float32，`[nucleus, fusion]`；nuclei 方法是 `[nucleus, 0]`。`normalize_input=False`。
- 权重：`models.json` 由 `scripts/model_manifest.py` 根据固定的 `MODELS` **整份重写**（:51）。
- 兼容身份：`behavior_identity()` 比较 `behavior_version` 和 `model_id`。Step1 结果复用（`core/preseg_run.py:235`）和 Step2 一致性提示都依赖它。
- provenance：`seg_runner/runner.py:67` 只对 seg_runner 顶层的 `*.py` 计算 hash。

## 3. 可行性证据与它的局限

- 原型（scratchpad `port_proto.py`）：
  - 手写 PyTorch 前向，按层名搬运权重，共 78 个卷积、57 个 BN；
  - 4 个输出头和 TF 最大差 ≤3.5e-6；
  - 模型没有带状态的层：两次 `training=False` 前向，4 个头完全相同。
- `real_tiles.py`：3 块真实 1024² 图，**两边都用原版 DeepCell `Mesmer` 包装，只换了网络**，mask 逐像素相同。
- **局限**（codex 指出）：
  - 只证明了网络移植；新写的不含 TF 的 wrapper 还没验证；
  - 三块图都是 `image_mpp=0.5`，没走缩放路径；
  - 没测 GPU。

## 4. 现有处理顺序（wrapper 必须逐项照做）

| 阶段 | 现有行为（DeepCell 0.12.10 / toolbox 0.12.1） |
|---|---|
| 输入检查 | NHWC、4 维、2 通道；产品输入为 float32 |
| MPP 缩放 | **先缩放、再预处理**。`model_mpp=0.5`；新尺寸 `int(size × image_mpp / 0.5)`；mpp 为 None 或等于 0.5 时跳过。双通道走 toolbox `resize`：skimage 线性插值、`anti_aliasing=True`、`mode="constant"`、保留值域和返回 dtype |
| 预处理 | 先复制。逐图逐通道，对**非零值**取 99.9 百分位截断；再做 CLAHE（kernel 128）。结果为 float32，常数通道归零 |
| 切块 | 任一轴 <256：两轴都按差值绝对值做 **reflect** 补边（奇数差值时多出的 1 像素在高侧），忽略 `pad_mode`。否则 `tile_image(stride_ratio=0.75, pad_mode="constant")` |
| 网络 | 每块固定 256×256×2 → 4 个头 |
| 拼回 | 4 个头**分别**拼回：补边分支直接裁掉补边；切块分支用 `untile_image`（按 toolbox 的条件决定是否用 power=2 样条窗、float64 累加后转回块的 dtype、裁边，原样调用、不重写） |
| 格式化 | whole-cell = [头0, 头1 的第 1 通道]；nuclear = [头2, 头3 的第 1 通道] |
| 后处理 | `deep_watershed`。默认参数：whole-cell maxima 0.075，nuclear 0.1；两者都是 interior 0.2、interior_smooth 2、small_objects 15、fill_holes 15、radius 2。用户给的阈值覆盖默认值 |
| 缩放回去 | 只要尺寸不同，标签图逐个用 toolbox `resize(labeled_image=True)` 缩放回原尺寸。**单通道走 OpenCV INTER_NEAREST**；双通道会改走 skimage order-0，规则不同（codex 实测 4×4→7×7 有 24/49 个像素不同），所以必须单通道逐个缩放 |

**旧的边界行为照样保留**：一轴 <256、另一轴 >256 时，原实现两轴都按差值的绝对值补边，例如 300×200 会变成 344×256。TF 模型输入尺寸固定，会报错；torch 前向却能直接算出结果。原版的异常类型和文本随 TF 的追踪历史而变：首次是 `ValueError`，之后变成拼接处的 `InvalidArgumentError`（codex 实测）。wrapper 对补边后不是 256×256 的输入**同样拒绝**，抛出自己的明确错误，不复刻原版文本。是否支持这种输入，另作明确的行为变更。

## 5. 方案

小模块、单一职责。新目录 `seg_runner/mesmer_torch/`（需授权）：

| 模块 | 职责 |
|---|---|
| `graph.py` | 函数式前向 `forward(params, x)`：只有参数张量，没有 `nn.Module`，因此 BN 始终是推理模式 |
| `weights.py` | 读 `.pt`；严格核对：每个键都用到、形状正确、没有多余键 |
| `resize.py` | MPP 缩放（双通道输入）和单通道标签缩放，直接调用 toolbox `resize` |
| `preprocess.py` | 调用 toolbox 的 `percentile_threshold` 和 `histogram_normalization` |
| `tiling.py` | §4 的补边或切块、拼回；固定尺寸检查 |
| `postprocess.py` | 格式化，再按 compartment 分别 `deep_watershed`，默认参数与用户阈值合并 |
| `app.py` | 每个小块只算一次网络，返回 `{"whole-cell": mask, "nuclear": mask}`。两个 compartment 分别后处理、分别缩放回去；参数 `kwargs` 按 compartment 给出替换默认值的后处理参数 |

所有前后处理都**直接调用已安装的 deepcell_toolbox**（不导入 TF，已核对），不复制它的代码。

`app.py` 的编排逻辑改编自 DeepCell `Application` 和 `Mesmer`，因此：
- **许可证交付物**（按已安装 DeepCell LICENSE §4）：
  - 完整许可证副本：`seg_runner/mesmer_torch/LICENSE-deepcell`，逐字复制已安装的 deepcell 0.12.10 LICENSE；
  - 每个改编文件的文件头写修改声明（"改编自 deepcell-tf 0.12.10 的某某函数，改动为……"），并保留原有版权和归属行；
  - `seg_runner/mesmer_torch/NOTICE`：列出所有改编文件、来源版本和"仅限非商业学术用途"的限制；
  - 只调用 deepcell_toolbox、没有改编其代码的文件不需要这些，但 NOTICE 里注明依赖它；
  - `.pt` 不发布，这一点不能代替对代码部分的处理。

**`MesmerEngine` 和 `run()` 的改动**：
- `MesmerEngine` 改为 torch 实现：有 CUDA 就用 GPU，否则用 CPU；`engine_obj.device` 写实际设备。产品里不再提供 TF 后端，TF 只留在转换脚本和对照测试里。
- `run()`：torch 后端调用一次 `app.predict`。
  - 阈值路由和现在完全相同：用户阈值只给 `MESMER_THRESHOLD_TARGET` 指定的分割；
  - `min_size` 只给现在用它的输出；
  - nuclear_guided 的细胞核输出仍用库默认值，不做 min_size。
- GPU 设置：
  - fp32；只在网络计算期间设 `cudnn.deterministic=True`、`cudnn.benchmark=False`、关掉 TF32（cudnn 和 matmul 两处），算完恢复原值（`app._numerics`）；`torch.inference_mode()`；
  - **不用** `torch.use_deterministic_algorithms(True)`（实施时改，codex 第 4 步复核同意）：它是整个进程的开关，会让同一进程里的 Cellpose 等因 cuBLAS 报错。这个前向只有卷积、BN 推理、ReLU、双线性上采样（前向确定）和 softmax，没有矩阵乘和反向传播，上面的局部设置就够了；重复性由验收 E 实测确认；
  - **首版 CPU 和 GPU 都固定 batch=1**。批量推理留到以后另行验证，包括最后一个不满的批次。
- **身份政策**（用户裁定后）：
  - `MODEL_ID["mesmer"]` 定义为**逻辑模型身份**（同一套权重），与存储条目分开，engines.py 注释同步修改。`model_id` 不变。
  - `BEHAVIOR_VERSION["mesmer"]` 升到 **2**（换了推理实现，结果可能有细微差别，正属于这个字段的定义："能改变分割输出的改动"）。
  - 产品里只有一种 Mesmer 行为（torch），所以启动前的静态身份始终和实际运行一致。Step1 复用（`core/preseg_run.py:235`）、Step2 运行前确认（`ui/step2_page.py:3283`）和交接契约（`core/preseg_contract.py:65`）的代码都不用改，只是它们会把 version 1 的旧结果认作"不同"：
    - Step1：旧的 Mesmer 结果不再复用，会重算；
    - Step2：交给它的旧 Mesmer Step1 结果会出现版本不同的提示。
  - GPU 和 CPU 共用版本 2，不区分设备，和 Cellpose 相同。
- **provenance**：记录实际的 backend、设备、torch/CUDA/cuDNN 版本、确定性开关、batch（`lib_versions`），以及 `model_artifact`：`.pt` 的 sha256 和转换来源（源 SavedModel 的条目、各文件 hash、deepcell/TF 版本）。`runner.py:67` 的代码 hash 扩展到 `seg_runner/mesmer_torch/*.py`。只记录，不比较。

**权重转换与 manifest**：
- 扩展现有 `scripts/model_manifest.py`，`MODELS` 增加 `mesmer_multiplex_segmentation_torch`：
  - `root` 指向本地转换产物目录，可用 `BLOCK01_MESMER_TORCH_PATH` 覆盖；
  - **一个路径规则贯穿四处**（codex 第 4 轮 P2）：转换脚本的输出、`model_manifest.py --write`、`--verify` 和引擎加载，都通过同一个函数取实际目录（有覆盖变量就用它，否则用 `root`）。现在 `write()`（:54）只读固定 `root`，要改成用这个函数；
  - 目录不存在或没有文件时，`--write` 报错退出，不写出空清单；
  - 验收：在覆盖目录里转换、写清单、核验、加载，整套正常用法走一遍；
  - 条目记录 `derived_from: mesmer_multiplex_segmentation` 和源文件的 sha256；
  - 产物缺失时，和 TF 模型一样报 `ModelMissingError`，不悄悄回退。
- 新增 `scripts/convert_mesmer_to_torch.py`：只有这一步需要 TF。它做三件事：
  - 生成 `.pt`；
  - 旁边写一份 `SOURCE.txt`，说明来源、许可证、修改说明；
  - 用合成图对比 4 个输出头，然后运行 `model_manifest.py --write`。
- `.pt` **不进仓库、不发布**。

## 6. 验收（与 TF 对照 = 移植正确性检查，不是启用门槛）

**A. 网络**（原型适配器，batch=1）：
- 合成图和真实块上，4 个头与 TF 的最大差 ≤1e-4（CPU）；
- 记录 GPU 的数值，只作诊断。

**B. wrapper**：新 `app.py` 与原版 DeepCell `Mesmer.predict` 比较，**两边用同一个 torch CPU 网络**（原型的适配器）。这样只检查 wrapper，必须**逐像素**一致，包括中间值（预处理、拼回）和最终两个 mask。尺寸覆盖：
- <256、恰好 256、奇数尺寸、长方形、>1024；
- 补边后不是 256×256 的输入：必须同样被拒绝（不要求异常类型或文本相同）。

**C. 输入和参数**：
- 三种方法的真实输入，包括 nuclei 的 `[nucleus, 0]`；
- `image_mpp` 放大和缩小两种；
- 默认阈值和非默认阈值；
- `min_size` 为 0 和非 0。

**D. 端到端**：Step1 Use → Save → Step2 正常流程，GPU 和 CPU（用 Step2 现有的 GPU 开关选 CPU）各跑一次。比较对象是**同一设备上 torch 实现的直接调用**：产品流程的最终 mask 和边界要和它逐像素相同，不只比细胞数。这里**不和 TF 比**。

**E. 重复性**：同进程重复运行、新进程重跑，结果都相同。

**判定**（用户裁定后）：
- **必须通过（确认移植没写错）**：
  - A：CPU 上 4 个头与 TF 的最大差 ≤1e-4；
  - B：同一网络下，wrapper 与 DeepCell 原版逐像素一致（中间值和最终 mask）；
  - C：缩放路径、三种输入、阈值路由、min_size 都正确；
  - E：同一设备上重复运行结果相同（GPU 开确定性设置）。
- **报告给用户、不阻止启用**：
  - GPU torch 和 CPU torch 分别与 TF 的最终 mask 对比：细胞数差、IoU≥0.5 配对的 F1、配对 IoU 中位数、不一致细胞的叠图；
  - 若差异明显超出数值精度能解释的范围（例如 F1 明显低于 0.99），先当作移植错误排查，查清后再交给用户判断。
- **端到端（D）**：Step1 Use → Save → Step2 正常流程在 GPU 和 CPU 上各跑通一次。旧的 version 1 Mesmer 结果在 Step2 出现版本提示，Step1 会重算而不是复用旧结果。
- 已知限制（不在本任务范围）：`utils/runtime_resource_monitor.py:297` 的设备标签看的是父进程的 TF GPU 探测，不读 runner 回报，可能标错。只影响诊断显示。

**现有测试的迁移**（codex 第 4 轮 P2）：
- `tests/test_seg_runner.py:136`、`tests/test_seg_runner_engines.py:181` 现在要求产品 runner 的最终 mask 与 TF 逐像素相等。改为：产品 runner 对照**同设备的 torch 实现**逐像素相等；跨 TF 的对比移到移植检查测试里，只报告差异，不断言相等。
- `tests/test_seg_runner_engines.py:115` 的 mock 依赖"每个 compartment 调一次 predict"，改为新的"一次推理、两种结果"接口。
- 文档：`envs/fusion_mesmer/README.md:33` 的"Mesmer 在 CPU 上跑 TF"和旧权重路径说明同步更新。

**自动测试**（只用 scratch 副本）：
- 权重严格加载；切块、拼回和缩放规则；阈值路由；身份和 provenance；
- 和 TF 对照的测试在 TF 缺失时跳过，但发布前的移植正确性检查必须真的跑过。

## 7. 风险

1. **结果细微变化**（用户已接受）：GPU/CPU 和 TF 的数值差会让少量细胞的种子或边界不同。用版本号 2 区分新旧结果；差异报告让用户心里有数。
2. **旧结果**：已有的 Mesmer Step1 结果在 Step2 会提示版本不同，可选 Run anyway（记录确认）或取消后重跑 Step1；Step1 自己不再复用旧的 Mesmer 结果。
3. **授权**：权重和改编代码都受非商业学术许可约束。`.pt` 不发布；NOTICE 和 SOURCE 写清来源。
4. TF 仍在环境里；deepcell 和 Python<3.11 的依赖还在（见 §1）。
5. 若以后换成 torch-mesmer 的新权重，那是另一个模型，必须用新的 model_id。

## 8. 需要用户决定

1. 新目录 `seg_runner/mesmer_torch/`、新的 manifest 条目、转换脚本、NOTICE/LICENSE 副本，以及 `BEHAVIOR_VERSION["mesmer"]` 升到 2，是否授权？
2. TF 后端退出产品路径（只留在转换脚本和对照测试中），是否同意？

## 9. 实施顺序与工作量（约 5–7 个工作日）

1. `graph.py` / `weights.py`，用原型适配器做验收 A（1 天）。
2. 转换脚本（它的输出头自检要用第 1 步的 `graph.py`）+ manifest 扩展 + SOURCE/NOTICE/LICENSE 副本（0.5 天）。
3. `resize` / `preprocess` / `tiling` / `postprocess` / `app`，加验收 B 和 C（1.5–2 天）。
4. 接入 `MesmerEngine` 和 `run()`：torch 实现、GPU 优先、阈值路由、版本号 2、provenance（1 天）。
5. 验收 D 和 E，GPU/CPU 对 TF 的差异报告，用户真机跑一次完整 Step2（1–1.5 天）。

每步做完都送 codex 审核；完成后跑完整回归；用户验收后再提交。

## 10. 补充：第一次使用时自动准备 PyTorch 权重（2026-10-26，第三版，codex 批准，用户已授权）

### 10.1 问题（已核对）

1. 别的机器现在要手动跑一次 `scripts/convert_mesmer_to_torch.py`。
2. 现在的转换脚本会先重写仓库里的 `models.json`，再做 verify。
   - 后果：仓库基准被部署机器改掉，代码目录变脏；仓库不可写或其他模型缺失时，脚本整体失败。
   - 根源：manifest 固定的是本机这份文件的大小和 sha256。但文件字节并不固定：
     - `SOURCE.txt` 里有转换时间和源路径；
     - `.pt` 的 `source` 字典里有源路径（没有时间）。
   - 不同机器转出来的字节可以不同，大小也可能不同。
3. 权重**内容**是确定的（实验记录，codex 未复现）：在本机重新转换一次，384 个张量和 57 个 epsilon 与现有文件逐位相同（指纹 `e75cbc70…`）。转换约 10 秒（读 Keras 7.9 秒，转换 0.2 秒），另加头部自检。

### 10.2 方案

1. **内容指纹**：`weights.content_digest(data)`，由引擎、转换器和 manifest 工具共用这一个函数。
   - 输入是 `torch.load(path, map_location="cpu", weights_only=True)` 读出的**原始**数据，在任何 dtype 或设备转换之前。
   - 先做现有的结构检查 `check()`，再求指纹。
   - 编码固定：
     - 先写 `FORMAT`；
     - 再按键名排序，逐个写入 `名字\0 dtype\0 形状\0 字节长度\0 字节`；
     - 再按层名排序，写入 epsilon 的 `repr(float)`。
   - 排除 `source`，也就是路径和版本信息。
   - `weights.load(path, device, expected_digest)` 的顺序：读入 → `check()` → 比指纹 → 转成 float32 并放到设备上。
     - `expected_digest` 为空就报错，不退化为只检查文件是否存在。
     - 指纹不符时只拒绝加载，**不删除**用户已有的文件。
2. **manifest 条目**
   - torch 条目改为 `"generated": true`，加 `"content_sha256"`；`files` 只列 `.pt`，不记 size 和 sha256。
   - `model_path()` 对 generated 条目只检查文件是否存在。
   - `model_manifest.py`：
     - `verify()` 对 generated 条目比内容指纹；
     - `write()` 跳过它，保留原有的 `content_sha256`；汇总逻辑不再依赖每个文件都有 `size`。
3. **引擎冷启动顺序**：`MesmerEngine.__init__` 依次执行：
   1. 读 manifest 条目；
   2. `model_root()` 解析目录；
   3. `prepare.ensure(root, keras_entry, expected_digest, log)`；
   4. `weights.load(..., expected_digest)`。
   - 全部在发送 `hello` 之前完成。
   - 600 秒的启动上限包括锁等待、转换和加载。本机实测约 20 秒；更慢的机器如果超时，报错里写明可以手动转换。
4. **`prepare.ensure()`**：手动入口和自动准备共用这一段。
   - `.pt` 已存在：直接返回。之后照常比指纹，不需要 TensorFlow。
   - 不存在时：
     1. 先建好输出目录（全新部署时它还不存在），再由**父进程**（引擎进程，或手动脚本）在输出目录拿 `fcntl` 文件锁 `.convert.lock`。锁覆盖复查、转换、检查和发布的全过程；子进程不再抢锁。
     2. 拿到锁后再查一次 `.pt` 是否已在：两个运行同时开时，后到的一方直接使用。
     3. 转换子进程用 `[sys.executable, "-m", "seg_runner.mesmer_torch.prepare", "--convert-to", <临时目录>]` 启动。
        - 环境复制后**强制**设置 `CUDA_VISIBLE_DEVICES=""`，不用 `setdefault`。
        - 子进程留在 runner 的进程组内，所以现有的停止和清理机制同样会结束它。
        - 子进程的 stdout 和 stderr 进入引擎日志。
     4. 临时目录建在目标目录内，名字随机，形如 `.convert-<pid>-<随机>`。子进程在里面写 `.pt`，并做头部自检（误差 ≤1e-4）。
     5. 父进程核对指纹等于 manifest 的 `content_sha256`。通过后**一次**原子改名，把 `.pt` 放到最终位置。
        - **`.pt` 是唯一的提交点。**
        - `--force` 替换时也是这一次 `os.replace`：成功前旧 `.pt` 一直原样保留，失败或中断后仍是旧文件。
     6. **`SOURCE.txt` 只是说明，由 `.pt` 生成，不参与成败判断。**
        - 它的内容来自 `.pt` 里的 `source` 字典和内容指纹，第一行写明指纹。
        - `ensure()` 每次（拿锁后，或 `.pt` 已存在时）都检查 `SOURCE.txt`：缺失，或内容与按 `.pt` 生成的文字不同（不只比第一行的指纹，也比来源信息），就从 `.pt` 重新生成（写临时文件后原子改名）。生成的文字只取自 `.pt`，不含当前时间，所以内容稳定。
        - 因此任何中断后，最多短暂缺少或带着过时的说明，下一次启动会自动改正，永远不会描述错的权重。
        - 目录不可写时只记一条警告，不影响运行。
     7. **失败后的文件状态**
        - 首次生成失败（没有 TF、转换出错、自检或指纹不符）：删掉自己的临时目录，报错，没有最终 `.pt`。
        - 替换失败：旧 `.pt` 不变，报错。
        - 进程被强行杀掉：只会留下临时目录，它永远不会被当成成功产物；下次拿到锁时，清掉不属于活进程的旧临时目录。
   - 进度写入引擎日志，例如：`[Mesmer] first use on this machine: preparing the GPU weights from the Keras model (one time)`。
   - 适用范围：这套环境本来只支持 Linux x86_64 部署（README 已写明），这里的 `fcntl` 文件锁和进程组也只在这个范围内使用和测试。
5. **失败时都报错，不偷偷退回**（沿用计划 7.10.3）
   - 没有 Keras 模型：提示先拷贝它，并给出 `DEEPCELL_MESMER_MODEL_PATH`。
   - 输出目录不可写：提示用 `BLOCK01_MESMER_TORCH_PATH`。
   - 没有 TensorFlow：提示安装 fusion_mesmer 环境。
   - 不退回 TF 版 Mesmer，也不换用别的路径。
6. **转换脚本** `scripts/convert_mesmer_to_torch.py`
   - 默认：拿锁 → `ensure`（加 `--force` 时替换已有文件，替换同样走临时目录和原子改名）→ 核对指纹。**不改仓库的 manifest。**
   - `--update-manifest`：只在构建机上、建立新基准时用，例如权重格式升级。
     - 转换后把新指纹写进 manifest，不与旧指纹比较；
     - 这是唯一会写 `models.json` 的路径，测试里只写临时 manifest。
7. **部署说明**（README）改为：
   1. 设置路径，拷贝 Keras 模型；
   2. 跑 `seg_runner.selftest`：第一次会自动生成 GPU 权重；
   3. 跑 `model_manifest.py --verify`。
   - selftest 里"只写临时目录"的说明相应改为"第一次会在模型目录生成 GPU 权重"。
8. **身份和溯源**
   - 身份（版本 2）不变。
   - `model_artifact` 记录：
     - 文件 sha256：各机器可能不同，只记录不比较；
     - `content_sha256`：实际校验过的内容指纹；
     - `source`。
   - 内容相同、文件字节不同，不算身份差异。
9. **测试**：全部在 tmp 目录里，用 env 覆盖路径和临时 manifest，不碰真实模型目录和仓库 `models.json`。
   - 指纹：改 `source` 里的路径或时间，指纹不变；改张量、dtype 或 epsilon，加载被拒绝；预期指纹缺失时报错。
   - `ensure`（用假的转换子进程）：
     - 缺文件时转换，已存在时不转换也不需要 TF；
     - 两个进程同时启动，只转换一次；
     - 转换失败、自检失败、指纹不符：不出现最终 `.pt`，临时目录被清掉；
     - 被杀后的残留临时目录，下次被清掉；
     - `--force` 替换：成功时换成新文件；在改名之前失败或中断，旧 `.pt` 原样保留；
     - `SOURCE.txt` 缺失、或第一行指纹与 `.pt` 不符：下次 `ensure` 从 `.pt` 重新生成；目录不可写时只给警告；
     - 已有 `.pt` 指纹不符：拒绝加载，但不删除；
     - 没有 Keras 模型、目录不可写、没有 TF：报错清楚。
   - 经真实 `EngineProcess.start()`：冷启动（自动转换）后再启动一次（直接使用）。
   - 转换脚本：默认不改 manifest 字节；`--update-manifest` 只写临时 manifest，而且旧指纹不同时也照样写入新指纹，不会被旧指纹挡住。
   - 真实转换：在本机必须实际运行，不能用 skip 充数。转换到 tmp，指纹等于 manifest，引擎能读入并分割；随后用合成项目走一遍 Step2 产品流程。

### 10.3 需要用户授权

这是新机制，按 AGENTS.md 需要授权，内容有两项：
1. 引擎第一次使用时，自动在模型目录生成权重文件；
2. torch 权重改为按内容指纹校验。
