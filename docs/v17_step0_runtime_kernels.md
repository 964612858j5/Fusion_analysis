# Step0 实时校正：运行时参数化 GPU 内核（block RT）

日期 2026-11-06。基线 `8e5fc61`。动机与测量见本文件 §1，实现 §2–§4，
验收数据 §6，回退 §8。

## 1. 问题（实测，非推断）

CuPy 的 `cupyx.scipy.ndimage.grey_erosion/dilation(size=(2r+1, 2r+1))`
和 `gaussian_filter(sigma=...)` 把支持宽度写进生成的 CUDA 源码（循环上界是
字面量，核函数名带 `_w{w_shape}`，见 `cupyx/scipy/ndimage/_filters_core.py:244,294`）。
于是**每个新的 radius/sigma 都是一次新的 NVRTC 编译**。在 RTX 2080 Ti 上实测
（A9 驱动、真实按键、全图 level 0、结果缓存为空）：

| 方法 | 编译冷/结果冷 | 编译热/结果冷 | 编译热/结果热 |
|---|---|---|---|
| TopHat r=63 | 4681 / 4796 / 5047 ms | 303 / 276 / 235 ms | 47.6 ms |
| cuCIM σ=120 | 4722 / 5051 / 4692 ms | 441 / 508 / 425 ms | 55.3 ms |

慢事件期间 CuPy 缓存新增 8 个 `.cubin`，`sched.compute` 达 3.8–4.5 s。

## 2. 实现：两个内核，参数在运行时

新模块 `core/gpu_kernels.py`，CUDA 源码是模块内的字符串（随仓库发布）：

* `s0_minmax(x, y, H, W, r, axis, is_max)` — 可分离 1-D min/max，
  `r`、`axis`、`is_max` 全是运行时参数；
* `s0_gauss(x, y, w, H, W, r, axis)` — 可分离相关，权重 `w` 是**运行时数据**
  （每次 sigma 变化只重算权重并上传 host→device，不生成新程序）。

源码里没有 `wsize` 字面量，核函数名也不含尺寸，因此 CuPy 的缓存键
（源码 + 编译选项 + 架构）与 radius/sigma 无关。

**数值契约（逐位一致，不是“接近”）**

* TopHat：方形结构元边长 `2r+1`、`reflect` 边界（scipy 半样本对称：
  `if (i<0) i=-1-i; i%=2n; i=min(i,2n-1-i)`，与 cupyx 生成的代码逐字对应）、
  erosion→dilation→`x-opening`→`clip(...,0)`。min/max 是精确运算，与可分离
  顺序无关，因此与 cupyx 参考**逐像素相同**。
* Gaussian：`radius = int(4*sigma+0.5)`（truncate=4）、权重先在 float64 用
  `exp(-0.5*x²/σ²)` 归一化、再**转 float32**（这正是 cupyx
  `_get_weights_dtype` 对 float32 输入返回 `promote_types(float32,float32)`
  的结果，`_call_kernel` 随后把权重 cast 成 float32）、float32 累加、
  axis 0 → axis 1 两遍、float32 中间数组。与 `gaussian_filter` **逐位相同**。
  差异只可能来自累加精度/顺序，这两点都按参考实现复刻，测试以 `== 0` 断言。

因为数值完全不变，**没有**修改 `BG_CORRECTION_ALGO_VERSION` 和
`GAUSSIAN_IMPL_GPU = "cupyx-gaussian"`：旧结果、旧 Save 产品的身份仍然正确，
增量 Save 不需要无谓重算。这一点由 §6 的逐位测试支撑，而不是靠“看起来一样”。

## 3. 调用路径：一个入口，全部覆盖

所有 GPU 校正都经过 `core/bg_correction.py` 的两个公共算子入口，本次只改它们的
内部实现，因此这些路径同时生效：

* 全图实时预览、三图比较：`viewer/correction_compute.py` →
  `_apply_tophat_gpu_or_cpu` / `_apply_cucim_or_cpu`；
* floor / 多层亮度校准：同一个 `CorrectionCompute`（`_ensure_corrected_floor`
  的 job 走同一个 compute）；
* patch 计算、Save 的整图校正：`correct_tile` → `_tophat_gpu` / `_cucim_gpu`；
* 搜索/缩略图路径（`ui/step0/search_ctrl.py`）也走同一对入口。

没有第二份实现，也没有“只优化可见瓦片”。

## 4. 初始化与部署

* **在哪编译**：`core/bg_correction.py` 导入时的 `_run_gpu_morph_smoke_test()`，
  也就是旧实现编译按尺寸内核的同一个阶段（窗口显示之前、事件循环之前）。
  它调用 `gpu_kernels.warm()`，编译这 2 个核并用 8×8 合成数组各跑一次。
  总初始化成本见 §6；冷缓存下 `compile_ms ≈ 556 ms`，**总 cubin 数 4 个**
  （1 个缓存条目含 2 个自定义核 `s0_minmax`/`s0_gauss`，加上 CuPy 的 add/subtract/clip 三个逐元素核，共 4 个文件）。
* **缓存**：完全复用 CuPy 自己的机制（`CUPY_CACHE_DIR`，安装器已指向持久目录）。
  键是源码+选项+架构，所以换机器/换架构会各编译一次，之后复用；没有新增缓存
  管理系统，也没有 `/tmp` 文件或开发机绝对路径。
* **启动路径**：`scripts/block01` 已经 source `install.env`（`CUPY_CACHE_DIR`、
  `LD_LIBRARY_PATH`、`CUDA_PATH`），无需用户手工 export。本机用正式启动器与
  独立空缓存验证过（§6.4）。
* **失败时**：编译失败 → 打一行
  `[GPU] runtime kernels unavailable (...); using the cupyx implementation, which
  compiles a new kernel for every radius/sigma ...`，并把 `GPU_MORPH_SMOKE_TEST
  ["runtime_kernels"]` 记为 `fail: …`；GPU 仍可用（走参考实现）。运行期若自定义
  核抛异常，`_disable_runtime_kernels` 一次性降级并记录原因。任何情况下都不静默。
* **可观测**：`bg_correction.gpu_filter_backend()` 返回
  `"runtime-kernels"` / `"cupyx-legacy"` / `"cpu"`；
  `gpu_kernels.stats()` 有 `tophat_calls`、`gaussian_calls`、`legacy_calls`、
  `compile_ms`。测试用它确认没有误走旧路径。

## 5. Enter 去重

两次刷新来自两个独立原因，都已处理：

1. `_on_dec_param_entered` 里 `interpretText()` 会同步触发 `valueChanged`
   （已连到 `_on_dec_param_changed`），随后又显式调用一次 → 新增
   `_commit_spin()` 报告值是否真的变了，变了就不再重复调用；
2. 更隐蔽的一条：QSpinBox 在 Return 时**先自己 commit**（valueChanged 在
   `_on_dec_param_entered` 运行之前就发出了），所以 handler 里的
   `interpretText()` 看到的是“没变化”。为此 `_on_dec_param_changed` 增加了
   状态签名守卫：以 `(数据集路径 ome_path, channel, 生效 radius, 生效 sigma)`
   为键，同一状态只刷新一次。用“生效值”而不是“override 字典”，是为了让全局
   Method Parameters 变化后（生效值变了）仍然触发刷新；键里带数据集路径，是为了
   让“换了数据集、同名通道、同样数字”仍然刷新。行状态（glyph/“Saved”行）的更新
   放在守卫**之前**，重复提交也照常执行——被跳过的只有视图刷新。

测量：一次 Enter 的 `tissue.input` 从 **2 → 1**（真机 perf 日志，三次调参各一次）。
未使用任何延时 debounce；值不变时按 Enter 仍走 `_process_current_channel`
（有 patch 时照旧启动批处理；无 patch 时只更新状态行）。TopHat 编辑不影响
cuCIM 的同步路径，反之亦然。

## 6. 验收数据（本机 RTX 2080 Ti，CuPy 13.3.0）

### 6.1 数值（`tests/test_gpu_runtime_kernels.py`，19 项全过）

输入：常量、全零、随机、梯度、单亮点（含角落 border 点）、非方形、
小于窗口（5×5 / 3×500）、非连续（转置视图）；参数：TopHat r ∈ {1,2,15,63,150}，
Gaussian σ ∈ {1,5,50,120,200}。对 cupyx 参考的 **max|Δ| 全部为 0.0**。
瓦片拼接：`_apply_background_method_tiled` 与整图结果 max|Δ| = 0.0。

### 6.2 不编译的证据

独立空 `CUPY_CACHE_DIR`、全新进程：初始化后 4 个 cubin；连续调用
TopHat r ∈ {7,23,44,63,91,150} 与 cuCIM σ ∈ {2,9,31,120,200}，
**新增 cubin = 0**；另有测试用例在进程内计数同样断言 0。

### 6.3 单算子耗时（636×636 瓦片，热态）

| | 旧（cupyx 热） | 新（runtime kernels） |
|---|---|---|
| TopHat r=63 | 4.3 ms | **1.6–2.2 ms** |
| cuCIM σ=120 | 5.8 ms | **3.5 ms** |

### 6.4 端到端（A9 驱动，真实按键，隔离空缓存，结果缓存为空）

每行是一轮独立运行：全新进程 + 全新 `CUPY_CACHE_DIR`（目录里只有 4 个 cubin，
见 §6.5）。`a9.s0_first` / `a9.s0cmp_done` 只在引擎把该选择真正呈现时触发，
且触发帧的 `exact_fraction=1, target_fraction=1, gap_cells=0`。

| 场景 | 参数 | 上屏耗时 | 说明 |
|---|---|---|---|
| 全图 level 0（第 1 轮） | 63 → 44 → 91 | **260.6 / 116.2 / 131.2 ms** | 63 连续使用过；44、91 是全新值 |
| 全图 level 0（第 2 轮，独立空缓存） | 63 → 44 → 91 | **242.4 / 116.0 / 148.0 ms** | 独立进程、独立缓存；两轮都保留 |
| 全图全片（level 2） | 63 → 44 → 63 | **154.9 / 63.1 / 42.1 ms** | 全片视图，目标层 2 |
| 三图 level 0 | 63 → 44 → 63 | **137.1 / 91.9 / 43.1 ms** | 三面板同帧发布 |
| 全图 level 0（确认轮） | 63 → 44 → 91 | **234.6 / 121.3 / 124.2 ms** | 带 legacy 钩子的那一轮，见 §6.6 |

结论：五个场景/轮次（含两轮独立 level 0、全片、三图、确认轮）、多个新参数都在 261 ms 以内，**全部满足 ≤400 ms**。

### 6.4b cuCIM（Gaussian）的 GUI 验收（同口径：独立空缓存、结果缓存为空）

| 场景 | σ=120（首次） | σ=50 | σ=200（首次） | σ=200（紧接着重复） |
|---|---|---|---|---|
| 全图 level 0（第 1 轮） | 367.0 ms | 154.6 ms | 544.0 ms（第 2 轮） | — |
| 全图 level 0（第 2 轮 / 追加轮） | 366.6 / 195.5 ms | 116.6 ms | **754.8 ms** | 58.2 ms |
| 全片（level 2） | 261.5 / 258.9 ms | 65.4 / 63.6 ms | 77.6 / 60.9 ms | — |
| 三图 level 0 | 213.7 / 190.3 ms | 114.2 / 111.9 ms | **482.6 ms** | 167.9 ms（随后 σ=120） |

* **本次测试的 σ=50、120 场景**（全图 / 全片 / 三图）均 ≤ 400 ms。这**不**表示
  [1,120] 整个区间内每一个取值都测过或都达标；未测取值只能按随附脚本复测。
* **σ=200 在 level 0 首次提交超出目标**：544.0、754.8 ms（全图）与 482.6 ms（三图）。
  这不是编译（同轮 cubin 仍是 4 个，legacy 调用 0 次），重复触发同一 σ 只要 58.2 ms。
  该回合的端到端包含：4σ=800 像素 halo → 每个瓦片的输入窗口 2112×2112（约 4.5 MP）、
  冷 raw 缓存下的读盘、校正、上传与呈现。本轮**没有**拆分各自占比，也不通过降低
  画质、放宽质量门或改算法来换取数字。
**同参数对比**：表格第一列（TopHat r=63）的旧实现是 4681–5047 ms（编译冷）与
276–441 ms（编译热），本次 240.5–260.6 ms 落在其“编译热”水平。这个对比只对
**同一个参数值**成立；σ=200 没有对应的旧测量，**不能**拿旧的 σ=120 数值当它的
同比基线。

初始化：空缓存下 `import + smoke` 约 1.4–1.5 s，其中两个核的编译
`compile_ms ≈ 550 ms`；之后再启动是磁盘加载（约 3–10 ms）。启动日志会打印
`(filters: runtime-kernels, cache <dir>, kernels ready in <ms> ms)`，
所以任何一台机器的日志都能回答“这次启动编了什么、用的是哪个缓存”。

**测量口径**：初始化时间与端到端时间是两件事；端到端里包含瓦片读取、
校正计算（4 个瓦片并行）、GPU 上传与呈现，不能把总耗时全部算作算子时间。
单算子时间见 §6.3（636² 瓦片 1.3–5.1 ms）。

## 6.6 正式启动器与缓存证据

* **正式 `scripts/block01` 会覆盖外部 `CUPY_CACHE_DIR`**：启动器 `source
  .block01/install.env`，其中 `export CUPY_CACHE_DIR=/sda1/fusionflux/block01/cache/cupy`。
  实测：外部先设 `CUPY_CACHE_DIR=/tmp/diag4/should_be_ignored` 再运行
  `scripts/block01`，13 秒后读 `/proc/<pid>/environ`，得到
  `CUPY_CACHE_DIR=/sda1/fusionflux/block01/cache/cupy`，外部目录 0 个文件。
  因此 README 里“`CUPY_CACHE_DIR=<临时目录> scripts/block01`”是**错的**，
  已改为下面的方法。
* **可复现的隔离方法**（不改用户安装、不改 `install.env`）：把
  `scripts/block01` 复制到 `/tmp`，在 `check_install` 之后加两行
  （`export CUPY_CACHE_DIR="${S0_CACHE:?...}"`、`PYTHONPATH` 前插注入目录），
  其余流程（install 检查、同一个 python、同一个 `-m block01.main`、同一个 cwd）
  完全不变。diff 见 `evidence/launcher_diff.txt`。
* **进程确实用的是那个目录**：启动日志现在自带
  `(filters: runtime-kernels, cache <dir>, kernels ready in <ms>)`；
  同一进程的 `/proc/<pid>/environ` 也显示同一个 `CUPY_CACHE_DIR`
  （`evidence/gui_cache.txt`）。5 轮端到端运行每轮都是独立空目录，
  运行后各 4 个文件（§6.5）。

## 6.7 生产路径（patch 批处理与 Save）实跑证据

`tests/test_production_gpu_path.py`（2 项，真 GPU、合成像素）：

* **patch 批处理**：真的跑 `BatchProcessWorker.run()`（一个 patch、TopHat r=15）。
  断言 `legacy_calls` 不变、`payload["tophat_raw"]` 与 `gk.tophat(raw, 15)` 的
  `max|Δ| == 0.0`。
* **Save**：真的跑 `WsiCorrectionWorker.run()` + `product_swap` 落盘。断言
  corrected zarr 的 `ROI_1/CD68` 像素与 `correct_tile(ROI, "tophat", 15, "gpu")`
  的 `max|Δ| == 0.0`，且 attrs 记录 `bg_compute_path="gpu"`、
  `tophat_footprint="square"`，`legacy_calls` 不变。

这两条覆盖的是“公共入口之上的真实消费者”，与前面的算子级测试互补。
**覆盖范围**：生产集成目前只跑 TopHat。cuCIM 的数值（§6.1）与 GUI（§6.4b）已验证，
但生产路径（patch 批处理 / Save）没有单独跑过 Gaussian；这是已知的未测项。

## 7. 新增/修改文件

| 文件 | 变更 |
|---|---|
| `core/gpu_kernels.py` | 新增：CUDA 源码、RawKernel 封装、权重、预热、计数 |
| `core/bg_correction.py` | `_tophat_gpu`/`_cucim_gpu` 改为调度器；旧实现改名 `_*_legacy` 保留为参考/回退；smoke test 改为预热新内核；新增 `gpu_filter_backend()` |
| `ui/step0/step0_page.py` | Enter 去重（`_commit_spin` + 状态签名守卫：键含数据集路径、行状态刷新不被跳过） |
| `tests/test_gpu_runtime_kernels.py` | 新增：直接调核的逐位数值（19 项；含非连续、1×1/小于窗口、拼接、无编译、公共入口不回退） |
| `tests/test_step0_enter_dedupe.py` | 真实页面 + 真实键盘事件，10 项 |
| `tests/test_step0_patch_enter.py` | 有/无 patch 的 Enter 批处理，3 项 |
| `tests/test_production_gpu_path.py` | 真跑 patch 批处理与 Save（TopHat），校验产物与参考 `max|Δ|=0.0`，2 项 |
| `tests/test_qt_import_point.py` | 既有规则检查（全仓不得直接 import Qt 绑定）；两个 UI 测试改用 `block01.qt.QtTest` 后通过 |

## 8. 回退

* 进程级：`BLOCK01_LEGACY_GPU_FILTERS=1` → 完全回到旧 cupyx 实现（不再预热
  自定义内核），日志与 `gpu_filter_backend()` 都会显示 `cupyx-legacy`。
* 代码级：`_tophat_gpu_legacy` / `_cucim_gpu_legacy` 是原实现原样保留；
  `core/gpu_kernels.py` 未被其它模块直接引用，删除该文件并把两个调度器改回
  直接调用 legacy 即可完全回退。

## 9. 跨机器状态

* 已测：本机 RTX 2080 Ti、CuPy 13.3.0、driver 595.99、Linux。
* **修复随源码交付，不依赖本机积累的参数缓存**：内核源码在仓库里，新机器只在
  首次启动编译一次（约 0.55 s，见 §6.4），之后同一个 `CUPY_CACHE_DIR` 复用；
  参数值不参与编译。这是本次最关键的部署改进。
* **未测**：其它 GPU 架构（sm_75 之外的 sm_80/86/89 等）、其它 CuPy/CUDA 版本、
  Windows/macOS。内核只用标准 CUDA C（无架构内建、无共享内存假设），
  首次启动会按目标架构编译一次；`scripts/benchmark_step0_runtime_kernels.py`
  用于在任意机器上复现“新参数不编译”的验证。
* 不声称“任何机器都零编译”：换架构/换编译环境仍有一次性的两核编译成本。

## 10. 剩余问题（未在本块修复）

* 组织预览的 1.8 s 异常仍未复现（上一轮 3 次尝试），本块只修了 Enter 重复提交；
  现在每次调参只产生 1 次 `tissue.input`，不会再因重复而叠加。
* 启动阶段**不再**编译按尺寸的 cupyx 内核：空缓存下整个运行（启动 + 载入
  项目 + 多次调参）只有 4 个 cubin，见 §6.5。上一版本文档里“约 28 个 cupyx
  内核”是修复前（A/B 实验）的记录，已删除。
