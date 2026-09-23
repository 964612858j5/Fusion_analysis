# Step0 Full Image：cuCIM 切通道后回 Original 持续模糊 — 诊断与最小修复

日期：2026-09-23　分支 `v15-interactive-channel-workspace`，HEAD `b50e392`（未提交）。
状态：**自动化通过，待用户真机验收。** G3 保持打开；未暂存、未提交、未 push。

## 一、用户现象

Full Image 中 PD1 处于 cuCIM；保持 cuCIM 切到其他通道，画面正常；再把该通道切到
Original，画面「经常」一片模糊且不自行变清晰，zoom in/out 后才出现清晰细胞。

## 二、根因（证据链）

1. 页面的通道/方法切换都走 `Step0ExploreTab.show_source()` →
   `ExploreController.set_selection()`。
2. cuCIM 下切通道，若 HOT 已把目标通道当前视口的 cuCIM 瓦片备进缓存，
   `_try_atomic_cached_channel_swap()` 同一 GUI 事件内清空 raw/precise 两个 pool、
   只填 precise；此时 `need_raw = channel_changed and atomic_kind != "precise"`
   为 False，不请求 raw（raw 层本就藏在 floor 下，这一步本身是对的）。
3. 随后切 Original 只改方法：`channel_changed` 为 False → 只调
   `_issue_settled_request()`，它在无方法时直接返回。当前层 raw pool 为空、也无任何
   请求，`_update_layer_visibility()` 只能露出 overview；直到相机移动触发
   `_issue_raw_requests()` 才恢复。
4. 「经常」而非「总是」：仅当上一步走了 precise 原子替换且之后相机未动。走普通路径的
   切换在切通道时已请求 raw，回 Original 立即清晰。

归类：**瓦片未请求**（非「请求后未到达」「到达后未显示」「当前 zoom 选了粗层」——
全程选中层都是 L1）。

## 三、修复（唯一生产改动）

`viewer/explore_view.py` · `ExploreController.set_selection()`，一处请求条件：

```python
need_raw = ((channel_changed and atomic_kind != "precise")
            or not self._wants_precise())
```

无方法的选择，其可见图像就是 raw 层，因此无论通道是否变化都走
`_issue_raw_requests()`；该函数只请求当前层缺失的瓦片，raw 已驻留时可见读取为 0。
未改 scheduler、cache、HOT、算子、Step1 或其他生产文件。回退：恢复这一行。

## 四、自动化验证

新增 `tests/test_step0_original_after_cached_switch.py`（公开入口 `show_source`，
真实 TileScheduler / 缓存 / CorrectionCompute，仅切片为合成；`scheduler.request`
只做计数包装）。

| 测试 | 修复前 | 修复后 |
|---|---|---|
| 缓存 cuCIM 切换 → Original，相机不动：先断言 `atomic_channel_swaps +1`、`atomic_raw_channel_swaps` 不变、raw pool 为空；再断言当前层 raw 被请求、铺满、overview 被遮住、相机与层级未变、画面与缩放一次后的同相机画面一致、锐度 > overview 的 3 倍 | **FAIL**：`Original issued 0 current-level raw requests for 8 visible tiles` | PASS |
| 普通切换路径 → Original：无新 raw 请求、无重复读 | PASS | PASS |
| raw 已驻留 → Original：0 请求、0 读取 | PASS | PASS |
| 缓存路径 → Original → 一次缩放：每块切片读取 ≤ 1 次 | **FAIL**（未铺满） | PASS |

请求/读取计数（合成台架，visible = 8）：

| 场景 | 当前层 raw 请求 | 其他层 raw 请求 | 切片实际读取 |
|---|---|---|---|
| 缓存 cuCIM 切换 → Original | 8 | 4（L+1 underlay） | 0（raw 缓存命中） |
| 　之后一次 zoom in/out | 0 | 0 | 0 |
| 普通切换 → Original | 0 | 0 | 0 |
| raw 已驻留 → Original | 0 | 0 | 0 |

回归（均从 /tmp，`-p no:cacheprovider --confcutdir=/`）：
`test_explore_controller` + `test_step0_explore_tab` + `test_step0_compare_tiles` +
`test_step0_method_prefetch` + `test_step0_floor_prefetch` +
`test_step0_full_image_{viewport,first,tint}` 共 501 passed；
`test_raw_overlay_layer` 38、`test_step0_full_image` 16、
`test_step0_full_image_recovery` 19、`test_step0_channel_click` 22、
`test_step0_compare_channel_state` 12、`test_step0_patch_full_image_navigation` 20、
`test_ui_surface_contract` 8、`test_multichannel_prefetch` 30、
`test_coverage_prefetch_experimental` 16，全部通过（后两者需
`PYTHONPATH=<worktree>`，因其用顶层 `viewer.` 导入，与本改动无关）。

## 五、真实切片只读复测（修复前 / 后）

用户切片（config `OME_TIFF_FILE`，59040×35520，只读），`show_source` 驱动，HOT 与页面
同样挂载（σ=50、r=15 为诊断取值，非页面实际参数），视口 L1、12 块可见。

| 路径 | 切 cuCIM 是否原子替换 | 修复前：Original 静止 60 s | 修复后：Original 静止 |
|---|---|---|---|
| PD1 → FOXP3 | 是 | raw 0/12，请求 0，overview 可见，60 s 不变；一次缩放后 16 请求 0.52 s 清晰 | 0.21 s 内 12+4 请求全部到达、raw 12/12、overview 遮住；60 s 画面与修复前「缩放后」画面逐像素相同（max diff 0） |
| HLA-DR → PD1 | 是 | 同上 | 同上 |
| PD1 → HLA-DR（对照） | 否（HOT 90 s 未备好） | 立即清晰 | 立即清晰，0 新请求 |

修复后一次 zoom in/out：0 新请求、画面不变。

## 六、局限与如实说明

- 首次诊断曾在用户 X display `:1` 上开窗约 4 分钟，且其屏幕截取被其他窗口遮挡、
  作废；之后改为 offscreen + `widget.grab()`（ExploreView 为 raster
  QGraphicsView，这是其实际绘制路径，但**不是物理屏幕截图**）。
- 驱动用的是 `Step0ExploreTab`（page=None），未点击页面控件；HOT 参数为诊断取值。
- 对照组中 HOT 90 s 未备好 HLA-DR 的原因未追查（不影响本结论，未扩围）。
- 自动化通过 ≠ 真机验收。

## 七、待用户真机验收

在 PD1、HLA-DR、FOXP3 上各做一次：Full Image cuCIM → 保持 cuCIM 切到该通道 →
切 Original → 相机完全静止 ≥ 60 s，确认无需缩放即自行变清晰；再做一次 zoom
in/out，确认画面不变、无闪烁。
