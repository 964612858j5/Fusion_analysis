# v16 CPU Save 优化申请（块 CS，待用户审批）

日期：2026-10-04。分支 `v16`，HEAD `390a736`。状态：**方案 v2，用户 2026-10-04 批准**（codex、独立审核意见均已并入）。

## 1. 背景

- Step0 Save 时 GUI 冻结 11.8 s：`_save_and_continue` → `release_for_production` → `explore_view.suspend_for_production` 在界面线程上无超时 `join` 视图的 floor 线程（`floor join 11442 ms`）。
- 本机 Step0 背景校正 GPU 路径关闭（`libnvrtc.so.12` 不在加载路径），全部走 CPU。用户裁定：本块只做 CPU 优化，不碰 GPU（E）。

## 2. 现状（实测，16 核 / 10 GB / 真实切片 15437×16215，29 通道，uint8 LZW 512 分块 OME-TIFF）

| 环节 | 现状 | 实测 |
|---|---|---|
| 分块 | 每通道切 4096×4096 tile，带 `method_overlap` 光环；全片 16 块 | — |
| 读 | 每块重新打开 TiffFile，经 tifffile→zarr 读 | 约 0.09 s / 块 |
| 算（TopHat） | `skimage.white_tophat(disk(r), mode='reflect')`，float32 | r=5：2.0 s / 块；r=15：21.6 s / 块 |
| 算（cuCIM-CPU） | `scipy.ndimage.gaussian_filter` | σ=50：3.4 s / 块 |
| 并行 | `core/bg_parallel`：最多 4 个线程（写死 `MAX_WORKERS=4`，每块按 0.75 GB 估内存） | 16 核只用约 1/4 |
| 写 | 按块顺序写 zarr v2（float32，1024×1024 chunk，Blosc lz4）+ 粗分辨率平面累加 | 约 0.04 s / 块 |
| 视图 floor | 本片 floor = level 1 隔点取样 → 1929×2026；另做 3 个 2048×2048 全分辨率窗口的亮度标定，用**完整半径** | r=15 每窗口约 5.4 s → 11 s 冻结的主要来源 |

结论：瓶颈是 skimage 的圆盘形态学运算本身，不是读写，也不是线程数。

## 3. 关键发现

用 OpenCV `cv2.erode` + `cv2.dilate`，结构元素用**完全相同的** `skimage.disk(r)`，边界用 `BORDER_REFLECT`，结果与现有 skimage 结果**逐像素完全相同**（真实切片 r=5、r=15；随机数据 r=1…40；float32 与 uint8 输入均验证），速度：

| 4156² 块，r=15 | 耗时 |
|---|---|
| skimage（现状） | 21.6 s |
| OpenCV，float32 输入 | 0.91 s |
| OpenCV，uint8 输入 | 0.31 s |
| 16 块（整通道），OpenCV uint8，4 线程 | 1.4 s |

多进程与多线程实测速度相同（这类计算本身会释放 GIL），因此**不需要多进程**。

## 4. 方案（顺序执行，每步实测后再进入下一步）

**P1 —— CPU TopHat 换 OpenCV 实现（结果不变）**
- 只改 `core/bg_correction._tophat_cpu`：同一 `disk(r)` 核、中心锚点、先腐蚀一次再膨胀一次、显式 `BORDER_REFLECT`，输出 float32 = 输入 − 开运算。
- 先做 float32 路径（调用方现在都传 float32，所以 Save、视图图块、floor、亮度标定一起受益）。uint8 直通（再快约 3 倍）需要改调用方，另行审批，本块不做。
- 输入含 NaN/Inf、OpenCV 不可用或类型不支持时，回退到现有 skimage。
- 结果逐像素相同，所以 `bg_correction_algo_version` 与签名（`'cpu','disk'`）不变——**前提是等价测试全部通过**。GPU 路径不动。
- 预期：TopHat-15 整通道 Save 约 90 s → 数秒；floor 亮度标定约 16 s → 1 s 以内。

**P2 —— Save 的准备阶段全部移出界面线程（A，无论 P1 效果如何都要做）**
- 只给 Step0 Save 新增一条异步交接路径：点 Save → 立即锁住视图、不再发新的预览计算 → 后台等 floor 线程**和**调度器真正空闲 → 发出"就绪"信号 → 才启动 `WsiCorrectionWorker`。窗口全程可响应，状态栏显示等待原因；期间禁止第二次 Save，处理取消、关窗、出错后恢复。
- **现有同步的 `suspend_for_production()` 不改**：Compare 模式切换（`_hand_gpu_to_compare`、`compare_strip`）依赖它"返回即已停完"的语义，改成异步会让两边同时计算。
- **增量 Save 复制上一份校正结果（`_rm_new_correct_run` 里的 `shutil.copytree`，RM-1 引入）同样移到后台准备阶段**：它现在在界面线程上、且在等待视图之前执行（本项目一次复制约 0.1–0.2 GB，大切片会到数 GB）。复制失败时照旧丢弃未发布的 run。
- 超时不得作为"可以开始"的依据：预览计算没停下，Save 就不开始。

**P3 —— 先测量，再决定是否改线程数（D，probe 优先）**
- P1 之后每块只需约 1 s，4 线程可能已接近饱和。先在本机测 1/2/4/8/12 线程的吞吐，并记录 `cv2.getNumThreads()`（避免外层多线程 × OpenCV 内部线程造成过度订阅，必要时在工作线程里限定 OpenCV 线程数）。
- **只有多于 4 线程有明确收益时才实现动态规则**，否则保留 4，记录测量结果即可。
- 若实施：替换写死的 4：线程数 = min(可用 CPU 数 − 保留, 块数, (可用内存 − 保留) ÷ 每块实测内存)。
- 每块内存按方法分别实测（OpenCV TopHat 与 scipy Gaussian 不同），计入光环、临时缓冲、算完未写的结果、压缩线程；内存未知时取保守值。
- 在本机实测 1/2/4/8/12 线程的吞吐，找出饱和点；终端打印选择结果与依据。

**P4 —— floor 可中断（B，视实测决定）**
- P1+P2 之后，多次测量最坏情况下的交接等待（含亮度标定）。仍明显（> 约 1 s）才做：floor 与标定分块计算、块间检查停止标志、已完成部分保留、Save 后续算；分块须保持现有隔点取样、有效参数、边界规则，标定沿用同一取样窗口与整体分位数，结果与现在相同。否则记录为"不需要"。

**不做**：GPU（E）；OpenCV Gaussian（与 scipy 有 1e-4 级差异，非逐像素相同）；多进程（无收益）；Save 期间继续浏览（C）；TIFF 每块重开（P1 后再测量，必要时另议）。

## 5. 文件白名单

- `core/bg_correction.py`（P1）
- `ui/step0/step0_page.py` 的 `_rm_new_correct_run` 与 Save 启动流程（P2：复制移到后台）
- `core/bg_parallel.py`、必要时 `ui/step0/search_ctrl.py` 的线程数接线（P3）
- `viewer/explore_view.py`、`ui/step0/step0_explore_tab.py`、`ui/step0/step0_page.py` 的 Save 交接部分（P2；P4 若做，仅 `viewer/explore_view.py` 的 floor/标定）
- 测试：新增 `tests/test_bg_tophat_cpu_equivalence.py`（P1 等价扫描）、`tests/test_bg_parallel_workers.py`（P3 线程数规则）、`tests/test_step0_save_handoff_async.py`（P2）；视图既有测试回归。

## 6. 验收

1. 等价：真实切片同一 TopHat 通道，新旧两版各 Save 到不同输出目录，解码后的 level-0 数组与粗平面数组逐像素相同（含 ROI 边缘与块接缝）；floor 与亮度标定输出相同。
2. 速度：每通道 Save 耗时前后对比；P3 各线程数吞吐表。
3. 冻结：Save 时 gui-watchdog 不再报警，窗口可拖动。
4. 视图预览外观不变。
5. 回退：每步单独提交，可单独回退；P1 出问题可立即恢复 skimage。

## 7. 审核记录

- codex（astra low，2026-10-04）：同意 P1 → P2 → 实测后 P3 → 视情况 P4；P2 必须做；P1 先只做 float32；NaN/Inf 回退；P3 需按方法估内存；验收比较解码后的数组而非目录字节。均已并入本稿。
- 独立审核（2026-10-04）：P1 直接批准；P2 必须同时把增量 Save 的 `copytree` 移出界面线程，且只为 Save 新增异步路径、不改全局 `suspend_for_production()` 的同步语义（已核实：`_hand_gpu_to_compare` 与 `compare_strip` 同步调用它；`copytree` 在等待视图之前、界面线程上执行）；P3 改为先测量、超过 4 线程确有收益才实施；P4 维持视实测决定。均已并入 v2。

## 8. 实施记录

### P1（2026-10-04，本地提交）
- `core/bg_correction._tophat_cpu`：OpenCV 腐蚀 + 膨胀，核为 `disk(r)`，`BORDER_REFLECT`；无 OpenCV、非二维、含 NaN/Inf、图小于核时回退 skimage。
- 等价：`tests/test_bg_tophat_cpu_equivalence.py` 88 项（r=1–40、奇偶 / 极小形状、非连续、uint16、NaN/Inf、回退）；极小图（小于核）上 skimage 自身输出不可重复（越界读取），该情形只验证走回退。
- 真实切片：整通道 16 块 r=15，skimage 100.5 s → OpenCV 8.9 s，逐块相同；完整 Save（CD68 r15 + TIM3 r5）新代码 7.7 s，level-0 与粗平面与旧代码产物（proj2 correct_20261004_123913）逐像素相同。
- 回归 76 模块：失败均在旧代码上复现；`test_step0_compare_tiles::test_hot_requests_never_outrank_the_foreground` 因计算变快、合成切片在平移前已全部预取而变得不稳定，经用户授权改为平移同时放大 4 倍（10/10 通过），测试意图不变。
- codex 代码审核：同意。
