# v16 A0-1：viewer 位移诊断报告

日期：2026-09-29。分支 `v16`，产品代码 = `9999601`（未改动）。依据：`docs/v16_A0_application.md` v2 §3 A0-1。
脚本：`scripts/diagnose_v16_a0_camera.py`（插桩只在脚本进程里，包装后照常调用原方法）。
数据：test1 工作区 `full_wsi_20260927_121444_6bad` 的**副本**（`~/fusionflux/bench_a0/test1_copy`，改写了 24 个文件里的绝对路径）；切片 `cropped_region.ome.tif` 只读。每次运行前后都对原项目做目录指纹（3187 个文件的路径、大小、mtime），**全部未变**。

状态：**自动部分完成**。真机部分（裁定 1，含 Windows 125 % / 150 %）待用户执行，见 §5。

---

## 1. 结论

| # | 机制 | 结论 | 何时出现 | 量级 |
|---|---|---|---|---|
| **C1** | GPU 层铺满整个 graphics viewport，却画 ViewBox 的世界范围；ViewBox 四边各缩进 9 px | **证实**（数值 + 真实 GL 画面） | Step1 / Step3 的每一帧 | 与同一相机下的 Step0 相比，横向放大 1.4 %、纵向放大 2.3 %，中心对齐，四角偏约 6 px（本窗口尺寸） |
| **C2** | Step1 / Step3 套用相机时取整，再交给锁宽高比的 `setRange(rect)` 重新拟合 | **证实** | 每次进入 Step1 / Step3 | 中心 ±0.5 个第 0 层像素，比例约 ±0.01 %；**会累积**：50 轮 0→1→3→1→0 后 cy 偏了 −52 px |
| **C3** | 套用相机之后，布局变化使 ViewBox 缩小：高度变化发生在 `_mount_channels_dock` / `_match_step1_bottom_bar` 之后，日志未区分是哪一个；宽度变化发生在 `_hold_step1_channel_floor`（`setMinimumWidth`）之后。pyqtgraph 按新尺寸重新拟合；新比例被 live sink 写回共享相机 | **证实** | 第一次进入 Step1 | 比例 −2.49 %（离屏）/ −1.72 %（真实 GL）；之后尺寸稳定，不再出现 |
| C4 | 源重绑时 `int()` 截断 | **同源时排除**：同一交接重新加载不触发重绑，相机不动 | 只有交接的源真的变了才会发生 | 未测（不属于切换本身） |
| C5 | Navigator 的整数正方形跳转 | 不属于切换，未测 | 用户点击时 | — |
| C6 | DPR ≠ 1 时 `paintGL` 的 blit 目标用逻辑尺寸 | **待真机** | 仅 HiDPI | — |
| C7 | 现有测试不走 `setCurrentIndex`、没有 GPU 层、容差 12 px / 6 % | 解释了为什么这些问题没有被测试发现 | — | — |

**进入 Step0 是精确的**（`set_view_rect_l0`，浮点，不重新拟合）：1→0、3→0 的 Δ 都是 0。

**没有**发现第二次恢复、autoRange、层级切换、Navigator 回调或 coarse / fine 发布改动相机。每个转场里，除了下表列出的写入，没有别的写入。

**结论对 A1 的含义**：三个原因都是局部的，按 v2.1 §4.2 的顺序逐一修复即可，**不需要通用的 CameraState**（停止规则 1）。具体修法属于 A1 申请，本报告不定。

## 2. 每个转场（离屏，窗口 1600 × 1000，起点相机 (9000, 7000, 0.37)）

Δ 相对于该转场之前的相机；「切换后」= `_go_to_stepN` 刚返回时，「500 ms」= 事件循环再跑 500 ms 之后。

| 转场 | 切换后 Δcx / Δcy / Δscale | 500 ms 后 | 第一个改动相机的调用 |
|---|---|---|---|
| 0→1（首次） | +0.50 / 0 / −0.012 % | +0.50 / 0 / **−2.493 %** | `_go_to_step1 > _set_step_active > _apply_shared_camera_to > Mount.apply_camera > ExploreController.jump_to > ViewBox.setRange`（取整）；随后在 `_go_to_step1` 返回后发生 `ViewBox.resizeEvent`（高 800→780，接着宽 1293→1273），重新拟合，经 `_remember_camera('step1:step1')` 写回 |
| 1→3（首次） | +0.50 / −0.50 / −0.002 % | 同左 | `Mount.apply_camera > jump_to > setRange`（取整） |
| 3→1 | +0.50 / −0.50 / −0.012 % | 同左 | 同上 |
| 1→0 | 0 / 0 / 0 | 同左 | 无（Step0 精确套用） |
| 0→3 | −0.50 / −0.50 / +0.012 % | 同左 | `Mount.apply_camera > jump_to > setRange` |
| 3→0 | 0 / 0 / 0 | 同左 | 无 |

真实 GL（xcb + D3D12，同一流程）：0→1 首次 −1.720 %（高 806→792），其余各转场是 ±0.5 px 的取整，进入 Step0 为 0。

**C3 的调用序列**（离屏日志，首次 0→1）：

```text
_set_step_active(1)
  _capture_camera_of(0)                       (9000.0, 7000.0, 0.37)
  _step1_whole_slide_step_changed → Mount.open (新 stack，先 setRange 到整张切片)
  _apply_shared_camera_to(1) → apply_camera → jump_to(5919, 7253, 3495, 2162)   ← 取整
      _remember_camera (9000.5, 7000.0, 0.36996) 'step1:step1'
  _mount_channels_dock(1)
  _match_step1_bottom_bar()
_go_to_step1 返回
ViewBox.resizeEvent → updateViewRange          ViewBox 高 800 → 780
  _remember_camera (9000.5, 7000.0, 0.36078) 'step1:step1'   ← −2.49 %
_hold_step1_channel_floor()                   （singleShot(0)）
ViewBox.resizeEvent                           ViewBox 宽 1293 → 1273
```

## 3. 累积漂移（50 × 0→1→3→1→0，离屏）

| 轮次 | Step0 的 Δcx | Δcy | Δscale |
|---|---|---|---|
| 0 | +1.5 | −3.0 | −2.507 % |
| 9 | +1.5 | −12.0 | −2.507 % |
| 29 | +1.5 | −32.0 | −2.507 % |
| 49 | +1.5 | **−52.0** | −2.507 % |

- **中心有系统性漂移**：每轮 cy −1 个第 0 层像素，来自 1→3 和 3→1 各 −0.5 的取整。取整方向由该窗口尺寸下 `h / scale` 的小数部分决定，所以每轮同向，不会互相抵消。
- 比例只在首次进入时跳一次（C3），之后恒定。
- 在这个缩放级别（0.36），52 个第 0 层像素约等于 19 个屏幕像素。按 v2.1 §4.5 的门槛（不许有累积漂移），**不通过**。

## 4. C1 的画面验证（真实 GL）

设置：
- 同一相机 (9000, 7000, 0.37)；
- 同一个原始通道 DAPI，白色，同一显示窗口 [5, 120]，gamma 1；
- Overlay 模式下只显示 DAPI，不开 fusion，不开 mask；
- Step1 用控制器精确（不重新拟合）的入口设定相机，排除 C2 和 C3 的影响，所以两页的 `scale` 都是 0.37000，完全相等。

| 量 | Step0（CPU） | Step1（GPU） |
|---|---|---|
| graphics viewport | 1211 × 809 | 1287 × 810 |
| ViewBox（世界范围映射到这里） | 1193 × 791，位于 (9, 9) | 1269 × 792，位于 (9, 9) |
| 实际输出画面 | = ViewBox | **GPU 层 = 1287 × 810，位于 (0, 0)；FBO 1287 × 810** |

**比例**（由变换数值直接得出）：GPU 画面把 1269 × 792 的世界范围画进 1287 × 810，横向 1287 / 1269 = **1.0142**，纵向 810 / 792 = **1.0227**。

**平移**（相位相关，128² 块，upsample 10；测的是 Step1 GPU 截图相对于「它本应在的 ViewBox 位置」的偏移）：

| 位置 | 实测 (dy, dx) px | C1 预测 (dy, dx) px |
|---|---|---|
| 左中 | (−0.2, −4.4) | (0, −4.5) |
| 右中 | (−0.1, +4.3) | (0, +4.5) |
| 左上 | (−5.9, −6.4) | (−6.3, −6.3) |
| 右上 | (−6.1, +6.7) | (−6.3, +6.3) |
| 左下 | (+6.2, −6.1) | (+6.3, −6.3) |
| 右下 | (+6.7, +6.4) | (+6.3, +6.3) |
| 中心 | 背景（无纹理），跳过 | (0, 0) |

实测与预测相差 ≤ 0.5 px，即：画面中心对齐，越往外偏移越大，与 1.4 % / 2.3 % 的拉伸一致。截图、叠加图（红 = Step0，绿 = Step1 GPU，按世界坐标对齐）、差分图和 JSON 结果在 `~/fusionflux/bench_a0/camera_gl_image/`。

## 5. 待真机（裁定 1）

用户在真机上启动（请打开**副本**项目 `~/fusionflux/bench_a0/test1_copy`，不要打开 `~/fusion_data/test1`）：

```bash
cd /home/ming/fusionflux
PYTHONPATH=/home/ming/fusionflux /home/ming/micromamba/envs/fusion_mesmer/bin/python \
  Fusion_analysis/scripts/diagnose_v16_a0_camera.py realapp
```

清单：
1. Step0 → Step1 → Step3 → Step1 → Step0，每一步停 2 秒；
2. 在 Step1 分别用 Overlay、Fusion 做一遍；
3. 打开细胞 mask / 核 mask 做一遍；
4. patch 跳转、Navigator 跳转各一次；
5. Windows 显示缩放 125 %、150 % 下各做一遍第 1 步（记录 DPR，看 C6）。

日志写到 `~/fusionflux/bench_a0/camera_logs_real/`，退出时自动核对原项目未变。

## 6. 局限

- 离屏和 xcb 窗口的尺寸与真机不同。C3 的量级取决于底栏和通道栏的实际尺寸，C1 的量级取决于 viewport 的大小，比值是 18 / 宽 和 18 / 高。
- 本机 DPR = 1，C6 只能在真机上看。
- 插桩只包装、记录，但会稍微改变时序。GL 画面的比较没有用插桩（`gl-image` 模式不装钩子）。
