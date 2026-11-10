# StarDist 改用 PyTorch 运行 —— 方案 v1.2（2026-11-10；方案已 PASS，等开工通知）

**状态**：方案审核已通过（codex astra low 第 2 轮 PASS）。**产品代码一行未改**；除本文件外仓库干净。
**依据**：`docs/handoff_stardist_torch_ubuntu.md`（交接提示词）。
**仓库**：`/sda1/fusionflux/Fusion_analysis-17`，分支 `v17`，HEAD `c00c60c`。
**环境**：`/sda1/fusionflux/block01/envs/fusion_test2`（`.block01/install.env` 记录的解释器）。

**v1.1 → v1.2 的修订**（codex 第 2 轮：PASS，无 P1/P2/P3；两条 advisory + 用户的三项裁定）：

- **advisory-1（验证条款统一）**：§8 的检查重新分成**两类**——诊断检查（只报告，用来发现移植错误）与验收（用户裁定 + 必须成立的产品行为）。并写清两种对照各证明什么：**引擎 vs 直接调用 torch 只证明接线一致**；补边/拼接这类"两边同错"只能靠 **TF 对照**发现。§9 的措辞同步。
- **advisory-2（安装时转换的位置）**：转换放在 **`ensure_models` 之后、`selftest` 启动引擎之前**，并且先调用 `app_env` 保证 `BLOCK01_STARDIST_MODEL_PATH` / `BLOCK01_STARDIST_TORCH_PATH` 已导出。现脚本是 `install_block01.sh:709 ensure_models` → `:711 selftest`，而 `selftest` 内部 `:620` 先启动引擎、`:624` 才做全量 manifest 校验——插在后面会让首次安装因缺 `.pt` 失败。§3.2、§4.5。
- **用户裁定**：`BEHAVIOR_VERSION["stardist"]` 1 → 2（§5）；权重准备用 **B**（§4.5）；验证脚本**入库**（§8）。

**v1 → v1.1 的修订**（按 codex astra low 第 1 轮审核意见，P2×1、P3×4、advisory×3）：

- **P2-1**：MPS 不再排除。Mac 端会先提交 `torch_device()`（`seg_runner/engines.py`）和 `seg_runner/client.py` 的 `BLOCK01_ENGINE_CPU_ONLY` / `PYTORCH_ENABLE_MPS_FALLBACK`；本块把它们列为**合并依赖**，StarDist 直接调用 `torch_device()`，不新增开关、不改调用方。§6、§3.3。
- **P3-2**：设备字符串定为 `gpu` / `mps` / `cpu`（与 Mesmer 一致）。`utils/runtime_resource_monitor.py:195-205` 的 `_engine_reported_device` 已经同时认 `gpu` 和 `cuda`，不需要改监视器。§6.4。
- **P3-3**：删掉“`keras_model` 的其他用法只在训练/诊断路径”的错误声明。改为明确的保证边界：**本块只保证现有产品路径**（`n_tiles=None`、不调 `predict_instances_big`）。§2.4。
- **P3-4**：修正两处事实错误——下采样三层是 **32/64/128**；上采样依次是 **(128,64)、(64,32)、(32,32)**；稀疏路径用 `filter_points` 排除补边点，不是 `resizer.after`。§1.1、§1.2。
- **P3-5**：`tests/test_preseg_run.py` 补进白名单。§3.2。
- **advisory-6**：TF 在**构造模型时就会计算**，所以必须在构造 `StarDist2D` **之前**禁用 TF 的 GPU，并且要能验证隔离生效；“失败只记日志”不算隔离。§6.5。
- **advisory-7**：权重准备方式改推 **B（手动转换）**；A 的依据改为“内容指纹一致”，只清自己创建的临时文件，自检必须在最终发布之前。§4.5。
- **advisory-8**：补 cuDNN/TF32 的局部确定性控制、补非 16 倍数长方形输入、细化第三层验证，并写清“只报告”与“反向注入必须变红”的分界。§6.3、§8、§9。

---

## 0. 摘要

只把 **StarDist 的网络**（csbdeep 的 2D U-Net + prob/dist 两个输出头）换成 PyTorch 前向；归一化、补边、稀疏 NMS、多边形重建、标签图**全部继续由 stardist 包执行**。两者之间只有一个接口面：`self.keras_model.predict(x, verbose=0)`。

做法：照常构造 `StarDist2D`，把它的 `keras_model` 换成 `seg_runner/stardist_torch` 里的适配器；适配器加载从 `weights_best.h5` 转出的 `.pt`，在 `torch_device()` 给出的设备（CUDA / MPS / CPU）上算同一条前向。

与 Mesmer 那次的区别：Mesmer 把整套前后处理也搬进了 `mesmer_torch`，StarDist **不搬**（提示词第 1 条要求）。因此新代码规模小得多：预计 ~600 行（含测试和文档）。

**合并依赖**：Mac 端的 `torch_device()` 与 `seg_runner/client.py` 两个环境变量必须先推送；本块的设备接线等它们落地后再拉取合并（§3.3）。

---

## 1. 现状（已核对）

### 1.1 预测链路（stardist 0.9.2 源码，`site-packages/stardist/models/base.py`）

| 步骤 | 位置 | 本次是否改动 |
|---|---|---|
| 归一化 `normalize(img, 1, 99.8, axis=(0,1))` | `seg_runner/engines.py:249`（应用侧调用） | 否 |
| `predict_instances` → `_predict_instances_generator` | `base.py:776`、`base.py:645` | 否 |
| 稀疏预测（默认 `sparse=True`）`_predict_sparse_generator` | `base.py:541` | 否 |
| 轴检查、`StarDistPadAndCropResizer` 补边、`n_tiles` 处理 | `_predict_setup` `base.py:371-443` | 否 |
| **网络前向** `self.keras_model.predict(x[np.newaxis], verbose=0)` | `base.py:408-410` | **是，唯一替换点** |
| 概率阈值 | `nms.py:6-17` | 否 |
| NMS（C++ 扩展）、多边形重建、标签图 | `_instances_from_prediction`（`model2d.py:513-564`） | 否 |
| 稀疏点回原尺寸 | `resizer.filter_points`（`base.py:621`）——**排除落在补边区域的点**；dense 路径才用 `resizer.after` 裁剪 | 否 |

- 补边尺寸：`_axes_div_by()` = `unet_pool ** unet_n_depth * grid` = `2**3 * 2` = **16**（`model2d.py:567-575`）。适配器收到的 H、W 一定是 16 的倍数。
- 产品里每个瓦片调用一次 `predict_instances`，`n_tiles=None`（`engines.py:246-250`），所以 stardist 内部不再二次分块。

### 1.2 网络与权重（已实测）

- Keras 模型：1 个输入 `(None, None, None, 1)`，**2 个输出**：`prob (None,None,None,1)`（sigmoid）、`dist (None,None,None,32)`（linear）。
- **输出空间尺寸 = 输入 / `grid` = 输入的一半**（实测 256×256 输入 → `prob (1,128,128,1)`、`dist (1,128,128,32)`）。原因是 `model2d.py:317-326` 的“先按 grid 池化”循环。
- **19 个 Conv2D**，无 BatchNorm、无 Dropout（`unet_batch_norm=false`、`unet_dropout=0.0`）。
- 结构（`model2d.py:311-350` + `csbdeep/internals/blocks.py:58-133`；config：`unet_n_depth=3`、`unet_n_filter_base=32`、`unet_n_conv_per_depth=2`、`grid=(2,2)`、`net_conv_after_unet=128`、`n_rays=32`）：

  | 段 | 通道 | 说明 |
  |---|---|---|
  | 输入后（按 grid 下采样） | 32, 32 | 2×(3×3 conv) + MaxPool2 → 1/2 |
  | 下采样 3 层 | **32, 64, 128** | 每层 2 conv；skip 之后 maxpool |
  | 中间 | 256, 128 | 2 conv（最后一层用 `2**(n_depth-1)` 通道） |
  | 上采样 3 层 | **(128,64), (64,32), (32,32)** | 最近邻 ×2 + 拼接 skip + 2 conv；最后一层用 `last_activation` |
  | `features` | 128 | 1×(3×3 conv) + ReLU |
  | 输出头 | `prob` 1×1×1 + sigmoid；`dist` 1×1×32 + linear | |

- `weights_best.h5`（5,771,480 B，manifest sha256 `42202bd2…`）：30 个层组，其中 19 个 conv 组各含 `<layer>/kernel:0`（`(3,3,in,out)` 或 `(1,1,in,out)`）与 `<layer>/bias:0`。**h5py 能直接读，转换不需要 TensorFlow。**
- h5 内的层名（原始 Keras 2.2.4 命名）：`conv2d_1`、`conv2d_2`（前两个未命名 conv）、`down_level_{0,1,2}_no_{0,1}`、`middle_0`、`middle_2`、`up_level_{0,1,2}_no_{0,2}`、`features`、`prob`、`dist`。运行期 Keras 2.8 会把前两个 conv 重新命名成 `conv2d`/`conv2d_1`，**所以映射表以 h5 名为准，不按运行期层名对齐**。

### 1.3 身份与设备（现状）

| 项 | 现状 | 位置 |
|---|---|---|
| `BEHAVIOR_VERSION["stardist"]` | `1` | `seg_runner/engines.py:62` |
| `MODEL_ID["stardist"]` | `stardist_2D_versatile_fluo` | `engines.py:66-67` |
| 引擎设备判定 | `"gpu" if TF 看得到 GPU else "cpu"`（`_tf_gpu_count()`） | `engines.py:222-234` |
| Step2 关掉 Use GPU | `EngineProcess(cpu_only=True)` → 子进程 `CUDA_VISIBLE_DEVICES=""` | `client.py:34,55-56`；`segment_merge_worker.py:2590-2591` |
| Step1 的 Use GPU | **没有这个开关**：`PresegRunJob` 不传 `cpu_only` | `ui/step1_presegmentation/run_job.py:219-220` |
| `device` 字符串 | 引擎自报 `"gpu"`/`"cpu"` | `engines.py:234,278`；监视器 `runtime_resource_monitor.py:195-205` 认 `gpu`/`cuda`/`cpu` |

`torch_device()` 和 `BLOCK01_ENGINE_CPU_ONLY` **在本 checkout 里不存在**（全历史搜索无命中），`docs/v17_cellpose_mac_gpu_plan.md` 也没有推送。按审核意见，它们将由 Mac 端先提交，本块作为合并依赖（§3.3）。

### 1.4 环境与数据（已实测）

| 项 | 值 |
|---|---|
| Python | 3.10.20 |
| torch | 2.5.1+cu121 |
| stardist / csbdeep | 0.9.2 / 0.8.2（BSD-3，两个 dist-info 都带 `LICENSE.txt`） |
| tensorflow / keras | 2.8.4 / 2.8.0 |
| GPU | `NVIDIA GeForce RTX 2080 Ti`（`/proc/driver/nvidia/gpus/…/information`），驱动 595.99.02，内核模块已加载 |
| 本 shell 的 GPU | **不可用**：`/dev` 是私有挂载，只有 14 个设备节点、没有 `/dev/nvidia*`；`nvidia-smi` 报 “couldn't communicate with the NVIDIA driver”；`torch.cuda.is_available()=False`；TF 报 `CUDA_ERROR_NO_DEVICE` |
| 本机 TF 的 CUDA 库 | 也可疑：子调查进程里 TF 报 `Could not load dynamic library 'libcudart.so.11.0'`。**必须在真机上量清楚**（§6.5） |
| 参考数据 | `/sda1/fusionflux/cropped_region.ome.tif`，`CYX uint8`，29 × 15437 × 16215，3 级金字塔；**DAPI = 通道 0**；0.507 µm/px |
| 模型位置 | `/sda1/fusionflux/block01/models/stardist_2D_versatile_fluo/`（`BLOCK01_STARDIST_MODEL_PATH`）；`~/.keras/.../2D_versatile_fluo/` 是空目录 |
| 阈值 | `thresholds.json`：`prob=0.479071463157368`、`nms=0.3` |
| 启动成本（实测，CPU） | `import tensorflow` 1.8 s；`import stardist.models` 2.1 s；模型构造 + 载权重 0.2–0.6 s |
| 推理耗时（实测，CPU，256×256） | 首次 1.2 s（含 TF 预热），之后 0.11 s/张 |
| `import stardist` | **不**拉 TF（`stardist/__init__.py:13-21`）；`import stardist.models` 才 import TF/keras（`models/base.py:18-21`） |

---

## 2. 移植范围与接口

### 2.1 继续用 stardist 包（原样调用，不复制代码）

- `StarDist2D` 实例本身：`config`、`thresholds`；
- `predict_instances` / `_predict_instances_generator`（`base.py:645-789`）；
- `_predict_sparse_generator`（`base.py:541-633`）：补边、`n_tiles`、稀疏点收集、`resizer.filter_points`；
- `StarDistPadAndCropResizer`、`_normalize_axes`、`_make_permute_axes`（`base.py:371-443`）；
- `_instances_from_prediction`：NMS + 多边形重建 + 标签图（`model2d.py:513-564`）；
- `csbdeep.utils.normalize(img, 1, 99.8, axis=(0,1))`（应用侧，`engines.py:249`）。

### 2.2 被替换的仅一个对象

`model.keras_model` 换成 `seg_runner/stardist_torch/network.py` 的 `TorchNetwork`，它只需实现：

```python
def predict(self, x, verbose=0, **kwargs):
    """x: np.float32 (N,H,W,1), 已归一化、H/W 是 16 的倍数。
    返回 [prob (N,H/2,W/2,1) float32, dist (N,H/2,W/2,32) float32]。"""
```

依据：`base.py:408-410` 的 `predict_direct` 只做 `ys = self.keras_model.predict(x[np.newaxis], **predict_kwargs)` 然后 `tuple(y[0] for y in ys)`；`predict_kwargs` 里只有 `verbose=0`（`base.py:481,549`）。

**注意分辨率**：网络输出是输入的**一半**（`grid=(2,2)`），调用方 `_predict_sparse_generator` 自己按 `grid` 折算切片（`base.py:574-578`），最后由 `resizer.filter_points`（`base.py:621`）排除补边区域的点。适配器必须严格给出 `H/2 × W/2`，不能“顺手”放大回去。

### 2.3 接线方式

```python
device = torch_device()                                # Mac 端的约定：CUDA → MPS → CPU（§3.3）
model = StarDist2D(None, name=NAME, basedir=BASEDIR)   # 读 config.json / thresholds.json（TF 在场）
model.keras_model = TorchNetwork(params, device)       # 只换这一个属性
labels, details = model.predict_instances(normalize(image, 1, 99.8, axis=(0, 1)), **kwargs)
```

为什么不用“跳过 Keras 构建的子类”：`import stardist.models` 本身就必须有 TF，子类只省下 0.2 s 和几十 MB，却要依赖 `StarDistBase` 未公开的初始化契约，脆且难复核。**保留 TF 到进程里的成本与今天完全相同**（今天的 `StardistEngine` 本来就 import TF 并建模型）。

为什么不写盘：`StarDist2D(config=None, ...)` 走的是 `_set_logdir` 的只读分支（`csbdeep/models/base_model.py:186-200`），只读 `config.json`/`thresholds.json`/`weights_best.h5`，**不 mkdir、不 save_json**。今天的引擎已经是这个行为（`engines.py:229-233`）。

### 2.4 本块的保证边界（**不含** stardist 内部切块与 `predict_instances_big`）

`keras_model` 在 stardist 里还有两处会被调到，**本块不承诺它们**：

- `_predict_setup` 的 `tiling_setup()` 在 `n_tiles` 的乘积 > 1 时会调 `_axes_tile_overlap()`（`base.py:416`），后者在 `base.py:1100-1110` 走到 `_compute_receptive_field()`，**在 `base.py:1084-1085` 会拿网络跑两次**去推感受野；检测退化时 `base.py:1094-1095` 还会**新建一个原生 Keras 模型**。
- `predict_instances_big()`（`base.py:838`）在 `base.py:901`、`:925` 也走这条路。

产品现在固定 `n_tiles=None`，也从不调用 `predict_instances_big`（`engines.py:246-250`），所以本块**只保证现有产品路径**：`n_tiles=None` 的单次瓦片预测。**不为此新增任何预测框架、不复制 tiling 代码、不改调用参数**。如果以后要用 stardist 内部切块，那是另一个块，需要单独验证感受野那两次调用与我们的适配器。

---

## 3. 文件白名单（封闭白名单）

### 3.1 新增

| 文件 | 职责 | 需要 TF？ |
|---|---|---|
| `seg_runner/stardist_torch/__init__.py` | 一页说明，指向 NOTICE | 否 |
| `seg_runner/stardist_torch/graph.py` | 函数式前向 `forward(params, x)`；`expected_shapes()`；常量 `GRID=2`、`N_RAYS=32` | 否 |
| `seg_runner/stardist_torch/weights.py` | `.pt` 的 `read` / `check` / `content_digest` / `load` | 否 |
| `seg_runner/stardist_torch/convert.py` | `weights_best.h5` → `.pt`，严格映射 + 形状自检 | 否（只用 h5py） |
| `seg_runner/stardist_torch/network.py` | `TorchNetwork` 适配器（`keras_model` 的替身） | 否 |
| `seg_runner/stardist_torch/numerics.py` | 网络计算期间的 cuDNN/TF32 局部开关（照 `mesmer_torch/app.py:35-49`） | 否 |
| `seg_runner/stardist_torch/NOTICE` | 改编/署名说明 | — |
| `seg_runner/stardist_torch/LICENSE-stardist` | 逐字复制 stardist 0.9.2 的 `LICENSE.txt` | — |
| `seg_runner/stardist_torch/LICENSE-csbdeep` | 逐字复制 csbdeep 0.8.2 的 `LICENSE.txt` | — |
| `scripts/convert_stardist_to_torch.py` | 手动转换入口（`--force`、`--update-manifest`） | 否 |
| `scripts/stardist_consistency.py` | 三层一致性验证脚本（见 §8） | 参考侧需要 |
| `tests/test_stardist_torch_graph.py` | 图/权重/严格加载的单元测试 | 否 |
| `tests/test_stardist_torch_convert.py` | 转换、指纹、manifest 的测试 | 否（真转换一条需要真 h5） |
| `tests/test_stardist_torch_engine.py` | 适配器与真实 `predict_instances` 的接口测试；对 TF 的对照只报告不断言 | 部分 |
| `docs/stardist_torch_plan.md` | 本文件 | — |

（v1.1 去掉了 `prepare.py`：权重准备改推方案 B，见 §4.5。）

### 3.2 修改

| 文件 | 改动 |
|---|---|
| `seg_runner/engines.py` | `StardistEngine` 换成 torch 实现并调用 `torch_device()`；`BEHAVIOR_VERSION["stardist"]`（见 §5）；新增 `STARDIST_TORCH_ENTRY`；`lib_versions()` 加 torch/cuda/cudnn/stardist；新增 `model_artifact()` |
| `seg_runner/runner.py` | `runner_version` 的 glob 增加 `stardist_torch/*.py`（`runner.py:96`） |
| `scripts/model_manifest.py` | `MODELS` 加 `stardist_2D_versatile_fluo_torch`；`_content_digest` 按条目选择权重模块（`model_manifest.py:77-79`） |
| `envs/fusion_mesmer/models.json` | 新条目（由 `--write` 生成，含 `content_sha256`） |
| `scripts/install_block01.sh` | ①`MODEL_VAR` 加一行 `[stardist_2D_versatile_fluo_torch]=BLOCK01_STARDIST_TORCH_PATH`（`:41-47`），`ORIGINAL_MODELS` 不变；②新增一步 `convert_stardist_weights`，位置是 `ensure_models`（`:709`）之后、`selftest`（`:711`）之前，函数第一行调用 `app_env` 让两个模型环境变量就位，然后跑 `scripts/convert_stardist_to_torch.py`（方案 B，§4.5） |
| `tests/conftest.py` | 会话级 scratch 覆盖增加 StarDist torch 目录，防止测试写进真实模型目录（`conftest.py:26-41`） |
| `tests/test_seg_runner.py` | 现在断言“引擎输出与 TF 逐像素相等”，改为“与同设备 torch 实现逐像素相等”（见 §9） |
| `tests/test_seg_runner_engines.py` | 同上（`test_stardist_expansion_…`） |
| `tests/test_preseg_run.py` | 同上（`test_the_job_equals_the_steps_done_by_hand` 用 TF 直接对照，`tests/test_preseg_run.py:280-293`） |
| `NOTICE` | 增加 StarDist/CSBDeep 的 BSD-3 署名段 |
| `envs/fusion_mesmer/README.md` | “TF 2.8.4 看不到 GPU，StarDist 跑在 CPU 上”那段改为 torch 版说明 |

**不碰**：`ui/`、`workers/`、`core/`、`utils/`、`seg_runner/client.py`、`seg_runner/protocol.py`。

### 3.3 合并依赖（Mac 端先提交，本块拉取后再接线）

| 依赖 | 内容 | 本块怎么用 |
|---|---|---|
| `seg_runner/engines.py` 的 `torch_device()` | 依次选 CUDA → MPS → CPU；进程环境里 `BLOCK01_ENGINE_CPU_ONLY=1` 时用 CPU | `StardistEngine` 直接调用它，**不另写设备选择、不加新开关** |
| `seg_runner/client.py` | 子进程环境增加 `BLOCK01_ENGINE_CPU_ONLY`（对应现有的 `cpu_only`）与 `PYTORCH_ENABLE_MPS_FALLBACK` | 不改调用方；Step2 的 Use GPU 通过既有 `cpu_only` 传下去 |
| `docs/v17_cellpose_mac_gpu_plan.md` | 方案文档 | 只读参考 |

- **时序**：等用户确认 Mac 那块已推送，我再拉取（`git fetch` + 合并/快进，按当时的分支状态，另行确认），然后按它的**实际签名**接线。
- 在那之前，本块可以做的只有与设备无关的部分（graph / weights / convert / 单元测试 / 转换真跑）。**`StardistEngine` 的接线不抢先做**，避免和 Mac 端的改动冲突。
- 拉取后先核对 `torch_device()` 的真实返回（返回 `torch.device` 还是字符串、含不含索引），再决定 `self.device` 的映射写法。

---

## 4. 权重：转换、存放、校验

### 4.1 `.pt` 格式

```python
{
  "format": "block01-stardist-torch/1",
  "tensors": {"conv2d_1.weight": t, "conv2d_1.bias": t, ...},   # 19×2 = 38 个张量
  "source": {
      "model_key": "stardist_2D_versatile_fluo",
      "root": "<源目录绝对路径>",
      "files": {"config.json": "<sha256>", "thresholds.json": "<sha256>", "weights_best.h5": "<sha256>"},
      "stardist": "0.9.2", "csbdeep": "0.8.2", "keras_version": "2.2.4", "torch": "2.5.1+cu121"
  }
}
```

- 张量名 = h5 层名 + `.weight` / `.bias`；weight 从 Keras `(kh, kw, in, out)` 转成 `(out, in, kh, kw)`。
- 没有 BN，所以没有 Mesmer 的 `eps` 表。
- 用 `torch.save` / `torch.load(..., weights_only=True)`，与 Mesmer 一致。

### 4.2 转换（不需要 TF）

`convert.py` 的顺序（**自检在最终发布之前**）：

1. 读 `config.json`，按 §1.2 的结构推出 19 个 conv 的期望形状（`graph.expected_shapes()`）。
2. 用 `h5py` 打开 `weights_best.h5`：根属性 `layer_names`；每个层组的 `weight_names` 给出 `层名/kernel:0` 与 `层名/bias:0` 两个数据集。
3. **严格核对**：期望的 19 层必须全部存在；不能有多余的带权重组；每个 kernel 形状必须等于期望；dtype 必须是 float32。
4. **预期指纹在发布之前比对**：内容指纹与 manifest 的 `content_sha256` 不符就报错，**什么都不写**；已有文件原样保留（`--force` 也不覆盖）。
5. 写到目标目录里的**临时文件**（名字含 pid 与随机串），然后**从文件读回来**做自检：256×256 与 301×257（按 `_axes_div_by` 补边到 304×272）两种输入各跑一次前向，确认输出形状 `(1,1,h/2,w/2)` / `(1,32,h/2,w/2)` 且无 NaN；再核一遍文件的内容指纹等于内存里算出的。
6. 全部通过后**单次 `os.replace`** 到最终位置。任何一步失败只删自己的临时文件，**不发布任何最终文件**。

为什么能不用 TF：已实测 h5 是标准 Keras 权重组（`backend=tensorflow`、`keras_version=2.2.4`），`kernel:0`/`bias:0` 直接可读。

### 4.3 存放与 manifest

```json
"stardist_2D_versatile_fluo_torch": {
  "engine": "stardist",
  "root": "~/.keras/models/StarDist2D/2D_versatile_fluo_torch",
  "env_override": "BLOCK01_STARDIST_TORCH_PATH",
  "derived_from": "stardist_2D_versatile_fluo",
  "generated": true,
  "content_sha256": "<64 位>",
  "files": [{"path": "stardist_2D_versatile_fluo.pt"}]
}
```

沿用 Mesmer 的 `generated` 语义：只写 `path`，不写 size/sha256；完整性靠加载时的内容指纹。`.pt` **不进仓库**。

`scripts/model_manifest.py` 两处扩展：

- `MODELS` 表加上述条目；
- `_content_digest(path)`（`model_manifest.py:77-79`）现在写死 mesmer 的 `weights`，改为按条目的 `engine` 选模块：
  `{"mesmer": mesmer_torch.weights, "stardist": stardist_torch.weights}[entry["engine"]]`。两个 `generated` 条目的 `engine` 分别是 `mesmer`/`stardist`，不会混。

### 4.4 内容指纹

与 Mesmer **同一种编码**（`mesmer_torch/weights.py:51-66`），但格式串不同，因此两个格式的指纹不会互相冒充：

1. 先做结构检查（缺键、多键、形状）；
2. `update(FORMAT + "\0")`；
3. 张量按名排序，逐个写 `名字\0dtype\0"d1,d2"\0字节长度\0原始字节\0`；
4. 返回 `sha256`。

`source` 不参与指纹（换机器路径不改变内容指纹）。`load(path, device, expected_digest)` 的顺序：读入 → 检查 → 比指纹 → 转 float32 上设备；指纹不符**只拒绝加载、不删除文件**。

### 4.5 权重准备：**已定 B（手动转换 + 安装时转一次）**

用户裁定（2026-11-10）：用 B。

- **B（采用）**：只提供 `scripts/convert_stardist_to_torch.py`。`.pt` 不存在或指纹不符时，引擎抛出明确的 `ModelMissingError`/`WeightsError`，信息里给出该命令和 `BLOCK01_STARDIST_TORCH_PATH`。
  **安装里的确切位置**（`scripts/install_block01.sh`）：新增一步 `convert_stardist_weights`，放在 `ensure_models`（`:709`，原始模型已取得并核验）**之后**、`selftest`（`:711`）**之前**；函数第一行调用 `app_env`（`:586`），确保 `BLOCK01_STARDIST_MODEL_PATH`（源 h5）与 `BLOCK01_STARDIST_TORCH_PATH`（输出目录）都已导出。
  **不能**放在 `selftest` 内部的全量 manifest 校验之后：那里 `:620` 已经启动引擎、`:624` 才 `--verify`，首次安装会因缺 `.pt` 而失败。
  转换本身幂等：`.pt` 已存在且指纹正确就直接跳过（`--force` 才重做）。
  理由：不新增冷启动机制，不碰锁、子进程、原子发布、SOURCE.txt；转换是纯 h5py、约 1 秒。
- **A（不采用，仅备查）**：引擎首次使用时自动生成。若以后要改成 A，规则是：只认**内容指纹一致**（不假设文件字节相同）；临时文件只由本次调用创建、失败时在 `finally` 里删掉自己的；自检与指纹都在发布之前；`os.replace` 是唯一提交点；不加锁。
- **C（不采用）**：照抄 Mesmer 的 `ensure`（锁 + 子进程 + SOURCE.txt）是为 20 秒的 TF 转换设计的，此处没有必要。

---

## 5. 身份（**已裁定：1 → 2**）

用户裁定（2026-11-10）：`BEHAVIOR_VERSION["stardist"]` 从 1 升到 2。

- 理由：换了推理实现，输出会差在浮点舍入级别，按该字段的定义（`engines.py:54-58`）属于“另一种行为”。产品里只有一种 StarDist 行为，所以启动前的静态身份始终和实际运行一致。
- 后果：
  - Step1 不再复用旧的 StarDist 结果，会重算（`core/preseg_run.py:235`）；
  - Step2 拿到旧的 StarDist Step1 结果会出现版本不同的提示（`ui/step2_page.py:3605-3608`），用户可选 Run anyway（记录确认）或取消后重跑 Step1。
- `MODEL_ID["stardist"]` 不变：仍是同一个逻辑模型（同一套权重），只是文件换了格式（与 `MESMER_TORCH_ENTRY` 的处理方式相同）。

---

## 6. 设备

### 6.1 设备选择：直接调用合并依赖提供的 `torch_device()`

```python
from . import torch_device            # Mac 端先提交（§3.3）

device = torch_device()               # CUDA → MPS → CPU；BLOCK01_ENGINE_CPU_ONLY=1 时 CPU
model.keras_model = TorchNetwork(params, device)
# self.device 的映射按 torch_device() 的实际返回写（torch.device 还是字符串、含不含索引），
# 目标值域是 "gpu" / "mps" / "cpu"（§6.4），拉取依赖后按真实签名定稿。
```

- **不新增开关、不改调用方**：Step2 的 Use GPU 仍然通过既有 `cpu_only` → `BLOCK01_ENGINE_CPU_ONLY=1`（Mac 端在 `client.py` 里加）传下去；Step1 不传 `cpu_only`，所以有可用设备就用。
- 在依赖到位之前，本块不写这段接线（§3.3）。

### 6.2 MPS

由 `torch_device()` 负责，本块不写 MPS 分支。`PYTORCH_ENABLE_MPS_FALLBACK` 由 `client.py`（Mac 端）设置。本块在 Mac 上只做“能被 torch_device() 选中并跑通”的接口测试；**MPS 的数值一致性不在本块的 Ubuntu 验证范围内**，in-plane 的对照只在 CUDA 和 CPU 上做。

### 6.3 网络计算期间的确定性开关

照 `mesmer_torch/app.py:35-49` 的 `_numerics()`：只在网络计算期间设 `cudnn.deterministic=True`、`cudnn.benchmark=False`、`cudnn.allow_tf32=False`、`cuda.matmul.allow_tf32=False`，`finally` 恢复原值；`torch.inference_mode()`；float32；batch 1。

**不用** `torch.use_deterministic_algorithms(True)`：它是整个进程的开关，会让同进程的 Cellpose 因 cuBLAS 报错（Mesmer 已踩过，见 `docs/mesmer_torch_plan.md` §5）。

“只有卷积/ReLU/上采样/sigmoid，所以确定”**不作为依据**——CUDA 上重复跑是否一致由验收实测（§8 层 1 的重复性检查）回答。

### 6.4 设备字符串：`gpu` / `mps` / `cpu`

- 与 Mesmer 一致（`engines.py:278`）。
- `utils/runtime_resource_monitor.py:195-205` 的 `_engine_reported_device` 已经同时认 `gpu` 和 `cuda`，**不需要改监视器**。
- 验收按这套字符串统一：`hello.device` 与每个任务的 `device_used` ∈ {`gpu`, `mps`, `cpu`}，并与 `torch_device()` 的选择一致。

### 6.5 TF 必须被挡在 GPU 之外，而且要能验证

TF 在**构造模型时就会计算**（`csbdeep/models/base_model.py:162-167`：建网络 → 初始化 → 载权重），所以不能等构造完再隐藏。流程固定为：

1. 进程启动后**先** `import tensorflow as tf`（此时还没有别的 CUDA 使用）；
2. **立刻** `tf.config.set_visible_devices([], "GPU")`；
3. **断言** `tf.config.list_logical_devices("GPU") == []`；同时记下隐藏**前后**的 `tf.config.list_physical_devices("GPU")`，把结果写进 provenance（例如 `tf_gpu_hidden: true`、`tf_visible_devices: []`）——这样能区分“本来就没有 GPU”和“确实被挡住了”；
4. 之后才 `import torch` / 调 `torch_device()` / 构造 `StarDist2D`。

- 断言失败 → **引擎启动失败并报明确错误**，不是记一条日志继续跑。“禁用失败只记日志”证明不了隔离。
- 若 `set_visible_devices` 在 import 后不可用（TF 已初始化 GPU），回退方案是把 `import tensorflow` 放在 `CUDA_VISIBLE_DEVICES=""` 下、随后恢复，并**断言 torch 仍能枚举到 GPU**；两种都验证不了 → 停下来问用户（属于新的机制，需另行授权）。
- 实施前先在真机量三件事：TF 的物理/逻辑设备、构造模型后 `nvidia-smi` 里该进程的显存、torch 是否仍能看到 GPU。本机 TF 可能连 `libcudart.so.11.0` 都加载不到（§1.4），这也要作为结果记录下来。

---

## 7. 许可证与署名

- StarDist 0.9.2 与 CSBDeep 0.8.2 都是 **BSD-3-Clause**（`stardist-0.9.2.dist-info/licenses/LICENSE.txt`、`csbdeep-0.8.2.dist-info/LICENSE.txt`，版权 “Uwe Schmidt, Martin Weigert”）。
- 我们**不复制**这两个包的代码：`graph.py` 是按 `config.json` 和已公开的网络结构自己写的前向。但结构来自它们，所以：
  - `seg_runner/stardist_torch/LICENSE-stardist`、`LICENSE-csbdeep`：逐字复制已安装包里的许可证；
  - `seg_runner/stardist_torch/NOTICE`：写明“网络结构来自 StarDist 0.9.2 / CSBDeep 0.8.2，BSD-3，未复制其代码；权重为 2D_versatile_fluo 的 PyTorch 转换，不随仓库发布”；
  - 每个源文件头加 2–3 行署名；
  - 仓库根 `NOTICE` 增加一段。
- 模型权重：StarDist 模型卡标 BSD-3，但按仓库既有做法（`NOTICE:25-30`）仍只把许可证交给安装器展示；`.pt` 不入库、不发布。

---

## 8. 一致性验证（三层；诊断与验收分开）

脚本：`scripts/stardist_consistency.py`。用户裁定（2026-11-10）：**脚本入库**；不论如何，本轮都必须在真机上跑出数字。

### 跨平台预检（2026-11-10，Mac 端独立执行，仅作记录）

用户在同一批代码上独立跑过：16 个新测试全部通过；Mac 上转换得到的内容指纹与本仓库 `models.json` 一致（`ea60584…`）；两块真实 DAPI 512×512 小块对比 TF 2.15，prob 最大误差 ≤ 2.3e-6，dist 最大误差 4.5e-5–1.47e-4（相对误差约 1e-5）。这是 Ubuntu 正式数字之前的旁证，不替代第 1/2 层的报告。

### 第 1 层：网络输出

输入：
- 合成随机图 256×256（固定 seed）与 `2×256×256`；
- 真实 DAPI 小块（取自 `cropped_region.ome.tif` 通道 0）；
- **一个非 16 倍数的长方形**（例如 301×257）：先在进程内用 `StarDistPadAndCropResizer` 补到 304×272，再比较网络输出——覆盖真实补边路径。

对比与指标：
- TF（参考）vs torch CPU；TF vs torch GPU；**torch GPU 重跑一次 vs 自己**（重复性）；
- **prob 的最大绝对误差 / 平均绝对误差**，**dist 的最大绝对误差 / 平均绝对误差**（32 通道分开算再取最大）；
- 必须先记录参考侧到底跑在哪个设备：`tf.config.list_physical_devices("GPU")`。
- TF 参考固定用 `CUDA_VISIBLE_DEVICES=""` 的独立进程跑（与今天的产品行为一致）；若实测 TF 能看到 GPU，额外再给一组 GPU 参考，仅作信息。

#### 第 1 层结果（2026-11-10，Ubuntu，CPU 与 RTX 2080 Ti）

参考侧：TensorFlow 2.8.4；即使不隐藏 CUDA，TF 也只枚举到 CPU（缺 CUDA 11 运行库），所以参考固定是 CPU。torch 2.5.1+cu121 在 CUDA 上可用。两遍前向都在 §6.3 的 `numerics()`（cuDNN 确定性、benchmark 关、TF32 关）下执行，每个设备的重复性差值都是 **0**。脚本：`scripts/stardist_consistency.py`。

| 输入 | 设备 | prob 最大 | prob 平均 | dist 最大 | dist 平均 | dist 相对 | 重复 |
|---|---|---|---|---|---|---|---|
| 随机 256×256 | cpu | 2.68e-07 | 1.66e-08 | 2.29e-05 | 1.61e-06 | 8.09e-06 | 0 |
| 随机 256×256 | cuda | 3.19e-07 | 1.68e-08 | 8.15e-05 | 1.83e-06 | 1.26e-05 | 0 |
| 随机 2×256×256 | cpu | 7.75e-07 | 1.59e-08 | 6.49e-05 | 1.73e-06 | 9.03e-06 | 0 |
| 随机 2×256×256 | cuda | 6.86e-07 | 1.60e-08 | 2.94e-04 | 1.91e-06 | 2.92e-05 | 0 |
| DAPI 密集 256×256 | cpu | 1.36e-06 | 3.92e-08 | 2.67e-04 | 3.71e-06 | 1.45e-05 | 0 |
| DAPI 密集 256×256 | cuda | 1.58e-06 | 4.76e-08 | 1.87e-04 | 3.40e-06 | 1.02e-05 | 0 |
| DAPI 301×257→补边 304×272 | cpu | 1.37e-06 | 3.88e-08 | 3.70e-04 | 4.07e-06 | 2.73e-05 | 0 |
| DAPI 301×257→补边 304×272 | cuda | 1.55e-06 | 4.74e-08 | 4.77e-04 | 4.85e-06 | 3.77e-05 | 0 |

对照诊断线：prob 最大绝对误差 ≤ 1e-4 —— 实测最差 1.58e-06，**通过**；dist 相对误差 ≤ 1e-4 —— 实测最差 3.77e-05，**通过**。dist 的绝对误差最大到 4.77e-04，按 §8 的理由不作为诊断线（这是像素距离，量级几十）。dist 的平均误差按 32 个通道分别求平均再取最大。

**补边路径的说明（2026-11-10 审核意见）**：第 1 层用的是补边**规则**（16 的倍数、右侧和下侧 reflect），在脚本里手工 `np.pad` 完成，**没有调用 `StarDistPadAndCropResizer` / `_axes_div_by()` 的真实代码**。规则与上游一致；真实的补边与裁剪路径由第 3 步之后引擎真正跑 `predict_instances` 时覆盖（第 2/3 层验证）。

#### 第 1 层反向注入（同一脚本 `--inject`）

| 注入 | 结果 |
|---|---|
| `concat`：上采样张量与 skip 的拼接顺序对调 | prob 最大 4.72e-01–7.59e-01；dist 最大 2.88e+01–9.87e+01，相对 9.7–11.5。**比基线大约 6 个数量级** |
| `upsample-mode`：最近邻改成双线性 | prob 最大 1.46e-01–3.23e-01；dist 最大 1.55e+01–4.04e+01，相对 6.0–11.9。**同样约 6 个数量级** |
| `upsample-factor`：倍率 2 改成 3 | 前向直接抛 `RuntimeError`（拼接处形状不匹配：期望 48/57，实际 32/38），没有数字。**失败是响亮的**，不会静默给出错值 |

结论：这两类移植错误 TF 对照都能发现；其中倍率错误连数字都出不来。这也印证了 §9 的分工——`引擎 == 直接调用` 只证明接线，这类错误只有 TF 对照（和本注入）能抓住。

### 第 2 层：最终分割结果

输入：`cropped_region.ome.tif` 的 DAPI 通道，取 **6 个 512×512 小块** + **1 个 Step2 大小的瓦片** + **1 个非 16 倍数的长方形（301×257）**：
- 512 块：3 块细胞密集、1 块稀疏、1 块组织边缘、1 块几乎空白；
- 坐标由脚本按 DAPI 粗密度图（第 3 级金字塔 964×1013）自动挑，并把坐标写进报告；
- Step2 瓦片：按 Step2 对这个 15437×16215 图像算出的网格取其中一块（受 60 MP 只读面积上限约束，`ui/step2_page.py:1959-1962`）。

每组对比：**TF(cpu) vs torch(cpu)**、**TF(cpu) vs torch(gpu)**；每个小块分开报告：

| 指标 | 定义 |
|---|---|
| 细胞数 | 两边各多少 |
| 一对一匹配率 | IoU ≥ 0.5，按 IoU 从高到低贪心，每个真值最多配一个预测；两个方向分别给 recall 和 precision |
| 匹配对的平均 IoU | 只统计匹配上的 |
| 前景像素一致率 | `|A∩B| / |A∪B|`（二值前景） |
| 耗时 | 每块单独计时 |

#### 第 2 层结果（2026-11-10，Ubuntu，CPU 与 RTX 2080 Ti）

每行是 `predict_instances` 的真实调用（含 stardist 自己的补边、NMS、多边形和标签图），输入是从 `cropped_region.ome.tif` 取的真实 DAPI 窗口，按 §8 的方法归一化。对照：TF(CPU) vs torch。**所有窗口都是像素级一致**——匹配数等于两边的细胞数，recall/precision/平均 IoU/前景一致率全部 1.000。

| 窗口（level-0 左上角） | 细胞数 | 设备 | 匹配 | 平均 IoU | 前景一致 | TF 耗时 | torch 耗时 |
|---|---|---|---|---|---|---|---|
| dense 512² (10248, 9732) | 406 | cpu / cuda | 406 / 406 | 1.000 | 1.000 | 2.70 s | 0.25 / 0.35 s |
| dense 512² (7174, 7683) | 615 | cpu / cuda | 615 / 615 | 1.000 | 1.000 | 4.02 s | 0.26 / 0.37 s |
| dense 512² (9223, 7683) | 904 | cpu / cuda | 904 / 904 | 1.000 | 1.000 | 5.11 s | 0.31 / 0.41 s |
| sparse 512² (11785, 1536) | 97 | cpu / cuda | 97 / 97 | 1.000 | 1.000 | 0.76 s | 0.18 / 0.29 s |
| edge 512² (7174, 10756) | 298 | cpu / cuda | 298 / 298 | 1.000 | 1.000 | 2.08 s | 0.21 / 0.32 s |
| blank 512² (0, 0) | 0 | cpu / cuda | 0 / 0 | — | 1.000 | 0.19 s | 0.15 / 0.25 s |
| 301×257（真实补边） | 52 | cpu / cuda | 52 / 52 | 1.000 | 1.000 | 0.44 s | 0.08 / 0.27 s |
| Step2 瓦片 5546×4454（own 5146×4054） | 21048 | cpu / cuda | 21048 / 21048 | 1.000 | 1.000 | 138.24 s | 26.01 / 8.69 s |

- 匹配用 `stardist.matching`（一对一、IoU ≥ 0.5），和 stardist 自己的评估口径一致。
- 301×257 覆盖了真实的补边与裁剪路径（不是手工 `np.pad`）。
- 耗时是 `predict_instances` 本身的墙钟；模型早已加载，TF 对小窗口做了同形状预热以免把首次追踪算进去。Step2 瓦片：**torch CPU 比 TF CPU 快 5.3 倍，CUDA 快 16 倍**。

### 第 3 层：产品路径（Step1 Use → Save → Step2）

- 通过 `seg_runner` 引擎进程跑一次 **Step1 预分割**（**新跑，不复用旧结果**）+ 一次 **Step2**，都在合成/临时项目上；
- **Step1 Use → Save → Step2** 走完整流程，检查：
  - `hello.device` 与每个任务的 `device_used` ∈ {`gpu`, `mps`, `cpu`}，并与 `torch_device()` 的选择一致；Use GPU 关掉时必须是 `cpu`；
  - `provenance.model_artifact` 里有 `content_sha256` 和 `source`，`provenance.lib_versions` 里有 torch/cudnn；
  - **实际 mask（这是接线检查）**：Step1 落盘的 `cell`/`nucleus` 与“同一设备上直接调用 torch 实现”的结果逐像素相同（不只比细胞数）。它证明接线一致；移植正确性由第 1/2 层的 TF 对照诊断负责（见下）；
  - **旧版本行为**：用一份 `behavior_version = 1` 的旧 StarDist Step1 结果，Step1 侧必须**重算而不是复用**，Step2 侧必须**出现版本不同的提示**；
- GPU 和 CPU 各跑一遍；同一设备重复一次确认结果一致。

#### 第 3 层结果（2026-11-10，Ubuntu）

| 检查 | 结果 |
|---|---|
| 真实 Step1 运行（引擎进程，合成项目） | `engines.stardist.identity` 为 `behavior_version 2` / `model_id stardist_2D_versatile_fluo`；`devices.stardist == "cpu"`（`cpu_only`）；记录里的 `device == "cpu"` |
| `provenance.model_artifact` | `entry`、64 位 `content_sha256`、`source.model_key` 都在；`lib_versions.stardist_backend == "torch"`、`tf_gpu_hidden is True` |
| Step1 不复用旧结果 | `tests/test_v16_pa_preseg_reuse.py::test_another_engine_makes_it_run_again` 通过（旧身份 → 重算） |
| Step2 的版本提示触发条件 | 单测：`compare_identity({behavior_version: 1}, 当前)` 返回 `differences == [["behavior_version", 1, 2]]`（页面据此提示） |
| Step1 Use → Save → Step2 | `tests/test_step1_step2_handoff_e2e.py` 全部通过（真实 `Use`、`_save`、`_go_to_step2`；Step2 拿到的组合与契约一致） |
| 设备跟随 | 引擎测试：`cpu_only=True` 报 `cpu`；否则等于 `torch_device()`（本机 `gpu`） |
| **Step2 真实运行（CPU）** | 合成项目 + 带金字塔的原始 OME，`SegmentMergeWorker` 跑完并写出 `segmentation_meta.json`；`seg_engine.device == "cpu"`、`identity.behavior_version == 2`、`provenance.model_artifact.entry == stardist_2D_versatile_fluo_torch`，落盘 mask 存在 |
| **Step2 真实运行（CUDA）** | 同上，`seg_engine.device == "gpu"` |
| **引擎 mask vs 同设备直接调用（CPU 与 CUDA）** | 两个设备各跑一次，引擎进程的 nucleus mask 与同设备、同权重的进程内 torch 实现**逐像素相同**；同一进程内跑两次任务结果相同 |
| Step2 页面对 v1 旧结果弹提示 | `tests/test_step2_engine_unified.py::test_another_behaviour_version_is_asked_and_cancel_stops` 通过（`1 -> 2`），替代只测 `compare_identity` 的单测 |
| Step1 不复用 v1 旧结果 | `tests/test_v16_pa_preseg_reuse.py::test_a_version_1_result_runs_again` 通过 |

**已知的既有失败与其原因**：`tests/test_step2_runner_path.py` 与 `test_step2_engine_unified.py::test_a_manual_run_equals_the_runner` 在本机有多处失败，日志停在 `[Step2] no label pyramid: no slide pyramid to align to (no raw OME)`——合成项目没有带金字塔的原始 OME，`SegmentMergeWorker._write_label_pyramids` 于是不写标签金字塔；这类测试断言 `finished` 信号，因此拿不到。我在改动前的 HEAD 上复现了同样两条，**HEAD 同样失败**，不是本块引入的。第 3 层的 Step2 真实运行因此**显式给合成项目加了一份带 3 级金字塔的原始 OME**（`tests/test_stardist_torch_product.py::_raw_ome`），运行即可完成并写出 meta。

**第 3 层的运行命令**：
```
cd /sda1/fusionflux/Fusion_analysis-17
BLOCK01_STARDIST_MODEL_PATH=/sda1/fusionflux/block01/models/stardist_2D_versatile_fluo \
BLOCK01_STARDIST_TORCH_PATH=/sda1/fusionflux/block01/models/stardist_2D_versatile_fluo_torch \
/sda1/fusionflux/block01/envs/fusion_test2/bin/python -m pytest -q \
  tests/test_stardist_torch_product.py tests/test_v16_pa_preseg_reuse.py \
  tests/test_step2_engine_unified.py::test_another_behaviour_version_is_asked_and_cancel_stops
```
结果：`4 passed`（product）+ `7 passed`（reuse 与页面提示）。

### 两类检查：诊断 vs 验收

**一、诊断检查（只报告，用来发现移植错误；不是产品门槛）**

第 1、2 层的 TF vs torch 对照全部属于这一类：

- 数值：prob/dist 的最大与平均绝对误差；掩膜：细胞数、匹配率、平均 IoU、前景一致率、耗时；
- torch GPU 重复跑两次的差异（重复性）；
- 数字照报，**通过线由用户裁定**（见下）。
- 参考线只用于**决定要不要停下来排查移植错误**，不是启用门槛：
  - **prob**：最大绝对误差 ≤ 1e-4（概率在 [0,1]，绝对误差有意义）；
  - **dist**：用**相对误差** `max(|a − b| / max(|b|, 1))` ≤ 1e-4，绝对误差照报；公式里的 `b` 是 **TF 参考值**（`a` 是 torch 的），`dist` 的平均误差按 **32 个通道分别求平均再取最大值**。理由：dist 是像素距离，量级从几到几十，绝对误差随量级放大；Mac 的独立核对里有一块 CPU 图绝对误差 1.47e-4、相对约 1e-5，用固定的 1e-4 绝对线不成立（2026-11-10 审核意见）；
  - 第 2 层匹配率 / 前景一致率明显低于 0.99。

**二、验收（用户裁定 + 产品行为必须成立）**

- **用户裁定**：上面的诊断数字是否可接受，即通过线本身。
- **产品行为必须成立**（不是数值门槛）：第 3 层的 Step1 Use → Save → Step2 完整流程、实际 mask 落盘正确、设备字符串、provenance、旧版本提示与不复用、同设备重复一致。

### 两种“相等”证明的东西不同（别把接线当成移植）

| 对照 | 证明了什么 | 证明不了什么 |
|---|---|---|
| 引擎进程输出 == 同设备直接调用 torch 实现（逐像素） | **接线一致**：同一份实现走了正确的路径（参数、tiling、dtype、设备） | **移植正确性**：如果 graph 的补边 / 拼接 / 上采样错误同时存在于两边，这个对照照样相等 |
| TF vs torch 的误差 | **移植正确性**：与参考实现的偏差 | 产品是否可用（那由用户裁定） |

所以：“引擎 == 直接调用”写成**断言**（测试里必须逐像素相等）；“TF vs torch”写成**诊断**（脚本报数字，不设断言门槛）。

### 反向注入：哪条检查必须变红

| 注入 | 必须变红的检查 | 备注 |
|---|---|---|
| conv 的 padding 改错（same 少补 / 多补 1 像素） | **TF 对照**的最大绝对误差显著变大 | 引擎 == 直接调用**仍然相等**——正好证明接线检查覆盖不到这类错误 |
| 上采样倍数、或 skip 拼接顺序改错 | 同上 | **已实测（2026-11-10）**：拼接顺序或插值方式改错，prob 误差从 ~1e-6 跳到 ~1e-1、dist 从 ~1e-4 跳到 ~1e+1；倍率改错则前向直接抛形状错误。见上面「第 1 层反向注入」 |
| 权重映射转置 / 键序写错 | 严格加载或形状检查（自动测试）；TF 对照误差变大 | 自动测试常态跑 |
| 指纹漏张量、漏 dtype | 指纹测试 | 自动测试常态跑 |
| TF 的可见 GPU 断言被改成永远真 | TF 隔离测试 | 自动测试常态跑 |
| 引擎绕过 torch 实现（例如仍调 Keras） | 引擎 == 直接调用**不再相等** | 自动测试常态跑 |

依赖 TF 的注入在实施期手动执行并记录数字（自动化测试要在没有 TF 的机器上也能跑）；这不能替代真机验收。

---

## 9. 现有测试的迁移

| 测试 | 现在 | 改为 |
|---|---|---|
| `tests/test_seg_runner.py:119` `test_stardist_in_subprocess_equals_direct_call` | 引擎输出 == TF 直接调用，逐像素 | 引擎输出 == **同设备 torch 实现**直接调用（断言；这是**接线一致性**，不是移植正确性）；与 TF 的对照只由 `scripts/stardist_consistency.py` 报告，不写成断言（实现时从测试里去掉，避免把诊断当门槛） |
| `tests/test_seg_runner.py:135` `test_stardist_from_an_installed_folder_equals_from_pretrained` | 文件夹加载 == `from_pretrained` | 保留“文件夹加载”这层语义，但期望值改用 torch 实现 |
| `tests/test_seg_runner_engines.py:47` `test_stardist_expansion_returns_the_nuclei_from_before_expanding` | 逐像素对 TF | 逐像素对 torch 实现；expand 部分不变 |
| `tests/test_preseg_run.py:264` `test_the_job_equals_the_steps_done_by_hand`（TF 对照在 `:280-293`） | 逐像素对 TF | 逐像素对 torch 实现 |
| `tests/test_mesmer_torch_*` | 不动 | 不动 |

新增测试（都在 tmp 目录）：
- 图：形状、参数数量（19 层、38 张量）、严格加载（缺/多/错形状/错 dtype 都拒绝）；
- 转换：h5 → `.pt`，`--force`，`--update-manifest` 只写临时 manifest；
- 指纹：换源路径不改变指纹，改一个权重就改变；
- 适配器：`TorchNetwork.predict` 与 `graph.forward` 一致；真实 `predict_instances` 能跑通并给出 `(labels, details)`；非 16 倍数输入走通补边；
- 引擎：`hello.identity` 的 `behavior_version` 是 2；`hello.provenance.model_artifact` 有指纹；`device` ∈ {`gpu`,`mps`,`cpu`} 且与 `torch_device()` 一致；TF 的 GPU 可见性断言生效；
- 反向注入：按 §8 的表——严格加载、指纹、TF 隔离断言、接线相等这几条写成会变红的自动测试；padding / 上采样 / skip 这两条因为要靠 TF 对照才看得出，在实施期手动注入并记录注入前后的数字。

---

## 10. 风险

1. **合并依赖的时序**：Mac 端的 `torch_device()` 与 `client.py` 未推送前，设备接线做不了；若两处对 `BLOCK01_ENGINE_CPU_ONLY` 的语义有出入，需要先对齐（§3.3）。
2. **TF 与 torch 同进程的 CUDA 共存**（§6.5）：未实测；靠“先隐藏 TF 的 GPU 再验证”兜底。本机的 TF 还可能根本加载不到 CUDA 11 库，真机结果要单独记录。
3. **结果细微变化**：GPU/CPU 与 TF 的浮点差异会让少量细胞的种子或边界不同。用版本号 2 区分新旧结果。
4. **权重映射依赖 h5 的层名**：`conv2d_1`/`conv2d_2` 是 Keras 2.2.4 的自动命名。缓解：严格核对 19 层全部存在且形状正确；源文件 sha256 由 manifest 管住。
5. **旧结果**：已有的 StarDist Step1 结果在 Step2 会提示版本不同；Step1 会重算。
6. **本 shell 没有 GPU**：所有 GPU 数字必须由设备可见的进程跑。
7. **Step1 没有 Use GPU 开关**：StarDist 在 Step1 会有设备就用，和今天 Mesmer 的行为一致；若希望 Step1 也能选 CPU，那是新的 UI 改动，不在本块。
8. **`runner_version` 哈希**：不加 `stardist_torch/*.py` 的话，provenance 看不出代码变了。
9. **stardist 内部切块与 `predict_instances_big`**：本块不覆盖（§2.4），以后要用需另开块。

---

## 11. 决定与待办

### 已裁定（2026-11-10）

1. **方案审核通过**：codex astra low 第 2 轮 PASS（无 P1/P2/P3）；两条 advisory 已并入本稿（v1.2）。
2. **身份**：`BEHAVIOR_VERSION["stardist"]` 1 → 2（§5）。
3. **权重准备**：方案 B（手动转换 + 安装时转一次，§4.5）。
4. **验证脚本**：入库（§8）。
5. **白名单**：§3.1 / §3.2 的清单，随开工通知一并生效。**超出清单的文件一律先停下问**。

### 待办 / 仍需你确认

1. **合并依赖**：等 Mac 端的 `torch_device()` 与 `client.py` 推送；你通知我开工后，我再拉取并按实际签名接线（§3.3）。
2. **TF 的 GPU 隔离验收**：方案按 §6.5 执行（断言失败就启动失败）。若真机上断言无法成立，我会停下来问，而不是降级成“只记日志”。
3. **GPU 验证由谁跑**：这个 agent shell 没有 `/dev/nvidia*`。三个选择：(a) 你在普通终端按脚本跑并把输出贴回来；(b) 给会话设备访问权限；(c) 我先只做 CPU 层，GPU 层等你安排。
4. **后续 codex 审核**：前两轮都由你转达；实施过程中是否仍按“每步做完送 codex、由你转达意见”执行？
5. **提交与推送**：完成后分别授权，不在本轮范围内。

---

## 12. 实施顺序与工作量（约 3–4 个工作日）

**开工条件**：等 Mac 端提交推送、用户通知开工。**在那之前不做任何实施**（本文件与 `docs/` 之外一行代码都不动）。

开工后即可做（不依赖 Mac 改动）：

1. `graph.py` + `weights.py` + `convert.py` + `numerics.py` + 单元测试；用 h5 真转一次，确认 19 层全对（0.5 天）。
2. `scripts/stardist_consistency.py` 第 1 层，CPU 上先跑出 TF vs torch 的误差（0.5 天，**这里可以跑**）。

等 Mac 依赖推送并拉取后：

3. `network.py` + `engines.py` 接线（`torch_device()`）+ 身份 + provenance + TF 隔离验收 + 测试迁移（1 天）。
4. 第 2 层数字（CPU 可做；GPU 需要 §11「待办」第 3 条的决定）+ 第 3 层产品路径（0.5 天）。
5. 文档、README、NOTICE 更新，完整回归，报告（0.5 天）。

每步做完送审；完成后跑完整回归；用户验收后再提交（提交与推送分别授权）。

---

## 13. 实施记录（2026-11-10）

### 第 5 步：文档、回归与收尾（本步）

- `envs/fusion_mesmer/README.md`：把“TF 看不到 GPU，所以 StarDist 跑在 CPU 上”那段改成 torch 版（网络走 `stardist_torch`、TF 的 GPU 在构造前隐藏、安装时转一次权重）。
- 仓库根 `NOTICE`：增加 `seg_runner/stardist_torch/` 的 BSD-3 署名段。
- 修两处测试错误（Mac 核实）：`lib_versions["cudnn"]` 在只有 CPU 的机器上记录为 `""` 是正确的，改为只要求字段存在、有 CUDA 时才要求非空；`_raw_ome` 把 HWC 的融合窗口先 `np.moveaxis(-1, 0)` 再按 CYX 写。
- 完整回归见下。


### 第 1 步：网络、权重、转换、确定性开关（codex 第 2 轮 PASS）

- 新增 `seg_runner/stardist_torch/{graph,weights,convert,numerics}.py`、`scripts/convert_stardist_to_torch.py`、两套测试、NOTICE 与两份 BSD-3 许可证文本。
- 真实转换的内容指纹 `ea60584ea226728dec7613dc93f9ff3cf5ab7ee0a0dfd9119ee0e3c70da95a80`，与 `models.json` 一致；Mac 端独立转换得到同一指纹。
- 转换不需要 TensorFlow（在屏蔽 TF 的进程里跑通，指纹相同）。
- 21 个测试通过。

### 第 2 步：第 1 层一致性（codex PASS）

- 新增 `scripts/stardist_consistency.py`；结果与反向注入见 §8。
- 结论：prob 最大 1.58e-06、dist 相对最大 3.77e-05，均在诊断线内；CPU/CUDA 重复性为 0；拼接顺序与上采样方式注入让误差跳约 6 个数量级，倍率注入直接抛形状错误。

### 第 3 步：引擎接线、身份、provenance、TF 隔离、安装脚本（codex 第 2 轮 PASS）

- 新增 `seg_runner/stardist_torch/network.py`（`keras_model` 的替身，只实现 `predict`）。
- `StardistEngine` 改为 torch：`_hide_tensorflow_gpu()` 在构造 Keras 模型**之前**隐藏 TF 的 GPU 并**断言**逻辑设备为空（§6.5）；`torch_device()` 选设备；`weights.load` 严格校验内容指纹；只替换 `keras_model` 一个属性。
- `BEHAVIOR_VERSION["stardist"]` 1 → 2（用户裁定）；`MODEL_ID` 不变。
- `provenance`：`lib_versions` 增加 torch/cuda/cudnn、`stardist_backend: "torch"`、`stardist_numerics`、`tf_gpu_physical`、`tf_gpu_hidden`；新增 `model_artifact()`（内容指纹、文件 sha256、转换来源）；`runner_version` 的哈希覆盖 `stardist_torch/*.py`。
- 安装脚本：`MODEL_VAR` 增加 `stardist_2D_versatile_fluo_torch`；新增 `convert_stardist_weights` 一步，位于 `ensure_models` 之后、`selftest` 之前，函数第一行 `app_env`（§4.5）。
- 测试迁移：`test_seg_runner.py`、`test_seg_runner_engines.py`、`test_preseg_run.py` 的 TF 逐像素对照改为“同设备 torch 实现”（CPU 上，`cpu_only=True`；`test_preseg_run.py` 用包装 `EngineProcess` 的方式真正选中 CPU，并断言记录的 `device` 是 `cpu`，因为 `client.start()` 会删掉 `BLOCK01_ENGINE_CPU_ONLY`）；新增 `tests/test_stardist_torch_engine.py`（身份、provenance、代码哈希、设备跟随、同进程重复、错误权重被拒绝）；`tests/conftest.py` 给 StarDist 权重目录加 scratch 覆盖。
- TF 隔离的拒绝路径有测试：TF 仍能看到 GPU、或 GPU 已初始化无法隐藏时，`_hide_tensorflow_gpu()` 与 `StardistEngine()` 都会抛错（用假的 `tf.config` 覆盖）。
- 本机实测：TF 在引擎进程里看不到 GPU（本机 TF 2.8.4 缺 CUDA 11 运行库），`tf_gpu_hidden` 断言成立；引擎在 `cpu_only=True` 时报 `cpu`，否则跟随 `torch_device()`（本机为 `gpu`）。
- 测试：StarDist 相关 **28 passed, 2 skipped**；安装脚本、图/转换/引擎、身份套件 **76 passed**（第 4 步后）。
- **资源基线（审核意见：不能写成“与本块无关”）**：本机 11 GB 卡上，`tests/test_seg_runner.py` 整文件跑时曾出现 Cellpose 子进程 CUDA OOM，
  采样显示 pytest 进程里先前那个 Cellpose 模型占到 9.76 GB。**单看源码无法确定因果**：当时新增的会话级夹具会把参考模型一直持有到会话结束，
  构造 Keras 之前没有隐藏 TF 的 GPU，原 Cellpose 测试在推理后也没有清理缓存。按审核建议改了三处后整文件复跑，**OOM 不再出现**：
  ①`stardist_direct` 改为按测试作用域并在结束时释放模型；②构造 Keras 前先 `_hide_tensorflow_gpu()`；③`test_seg_runner.py` 的参考同样先隐藏 TF 的 GPU。
  结果：`test_seg_runner.py` **1 failed（Mesmer 缺模型）+ 7 passed + 7 skipped**；三个文件（排除 Mesmer）**28 passed / 6 skipped**。
  Mesmer 的失败仍是本机没有模型，与本块无关。
- `numerics()` 改为按设备生效：只有 CUDA 才设 cuDNN 开关。之前 CPU 路径也会加载 cuDNN，在只跑 CPU 的进程里多出一个 CUDA 上下文。

### 第 4 步：第 2、3 层验证（第 2 轮补完）

- `scripts/stardist_consistency.py` 增加 `--layer 2`：真实 `predict_instances` 的最终标签对比，六个 512² 窗口（3 密集 / 1 稀疏 / 1 边缘 / 1 空白）、一个 301×257 长方形和一个 Step2 瓦片；结果见 §8。
- 新增 `tests/test_preseg_run.py::test_the_run_records_the_torch_engine_and_its_artifact`（真实 Step1 运行的 identity / device / provenance）与 `tests/test_stardist_torch_engine.py::test_a_version_1_step1_result_is_reported_as_another_behaviour`（Step2 提示的触发条件）。
- 本步相关测试：`test_preseg_run.py`（含两条产品路径用例）、`test_v16_pa_preseg_reuse.py`、`test_step1_step2_handoff_e2e.py`、`test_stardist_torch_engine.py` 共 **28 passed**。
- 第 2 轮补完（codex P2×4、P3×1）：
  - `tests/test_step2_engine_unified.py::test_another_behaviour_version_is_asked_and_cancel_stops` 改为 `behavior_version=1`、断言 `1 -> 2`（这是第 3 步漏改的）；
  - `tests/test_v16_pa_preseg_reuse.py::test_a_version_1_result_runs_again`：v1 的旧结果不复用；
  - 新增 `tests/test_stardist_torch_product.py`：引擎 mask 与同设备进程内 torch 实现逐像素对照（CPU 与 CUDA）、同进程重复、以及 Step2 经 `SegmentMergeWorker` 的真实运行（CPU 与 CUDA），并记录 `seg_engine.device` 与 `model_artifact`；
  - `test_the_run_records_the_torch_engine_and_its_artifact` 增加 `lib_versions.torch` 与 `lib_versions.cudnn` 断言。

## 附录 A：关键位置

- `seg_runner/engines.py:218-250` `StardistEngine`；`:62` `BEHAVIOR_VERSION`；`:66-68` `MODEL_ID`/`MESMER_TORCH_ENTRY`。
- `seg_runner/client.py:34-38,55-63` `cpu_only` 与 `CUDA_VISIBLE_DEVICES`。
- `ui/step1_presegmentation/run_job.py:219-220` 不传 `cpu_only`。
- `workers/segment_merge_worker.py:2590-2591` `cpu_only=not self._wants_gpu()`。
- `seg_runner/runner.py:96` `runner_version` 的 glob；`:118-120` `device_used`；`:73-105` `engine_provenance`。
- `scripts/model_manifest.py:36-59` `MODELS`；`:77-79` `_content_digest`；`:89-118` `write`；`:130-153` `verify`。
- `scripts/install_block01.sh:41-47` `MODEL_VAR` / `ORIGINAL_MODELS`。
- `utils/runtime_resource_monitor.py:195-205` `_engine_reported_device`（认 `gpu`/`cuda`/`cpu`）。
- `seg_runner/mesmer_torch/app.py:35-49` `_numerics()`（确定性开关的范本）。
- `stardist/models/base.py:371-443` `_predict_setup`；`:408-410` `predict_direct`；`:416` 切块时的 `_axes_tile_overlap`；`:541-633` 稀疏预测；`:621` `filter_points`；`:645-789` `_predict_instances_generator`；`:1084-1085` 感受野的两次网络调用；`:1094-1095` 退化时新建 Keras 模型。
- `stardist/models/model2d.py:311-350` `_build`；`:513-564` `_instances_from_prediction`；`:567-575` `_axes_div_by`。
- `csbdeep/models/base_model.py:162-167` 构造时建图/初始化/载权重。
- `csbdeep/internals/blocks.py:58-133` `unet_block`。

## 附录 B：本窗口无法完成、需要你配合的事

1. **任何 GPU 实测**：`/dev` 是私有挂载，`torch.cuda.is_available()=False`。需要设备可见的进程（§11「待办」第 3 条）。
2. **TF 实际使用的设备**：同上，只能在真机上确认。
3. **Mac 端依赖**：`torch_device()` / `client.py` 未推送前无法接线（§3.3、§11「待办」第 1 条）。
