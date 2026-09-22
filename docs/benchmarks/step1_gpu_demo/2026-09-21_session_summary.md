# 2026-09-21 会话汇总：G3.2b.4 → G3.2b.4B1.2

**仓库：** `/sda1/Fusion/analysis_pipline/block01_v14`
**分支：** `v15-interactive-channel-workspace`
**起止 HEAD：** `9d78d1c`（全程未提交、未 push、未 reset/checkout/restore/clean/stash/rebase/amend）

本文是本次会话十一个封闭任务块的汇总索引。每块的完整证据在各自的报告里，
本文只给结论、数据和边界，**不重复细节**。

---

## 0. 一页总览

| 块 | 性质 | 结果 | 详细报告 |
| --- | --- | --- | --- |
| **G3.2b.4** | 只读诊断 | 找到两个主导瓶颈，**均在只读文件内 → 按停止条件停止上报** | `2026-09-21_g3_2b_4_report.md` |
| **G3.2b.4A** | 实施 | 校正归约优化，**逐位相同**，coarse 3.93×、四通道 10.79× | `2026-09-21_g3_2b_4a_report.md` |
| **G3.2b.4B1** | 实施 | Step0 Full Image 后台准备当前通道双方法；顺带修一个 generation 缺陷 | `2026-09-21_g3_2b_4b1_report.md` |
| **G3.2b.4B2** | 只读诊断 | 冷 Patch 慢的**唯一**原因是视口几何；给出三个候选方案 | `2026-09-21_g3_2b_4b2_report.md` |
| **G3.2b.4B2C(+.1)** | 实施 | GPU 接管期间停掉旧 controller 的不可见读取；复核发现的生命周期 blocker 已收口 | `2026-09-21_g3_2b_4b2c_report.md` |
| **G3.2b.4B1.1** | 实施 | Full Image / Compare 共享 raw + corrected LRU；floor 按 §八 停止上报 | `2026-09-21_g3_2b_4b11_report.md` |
| **G3.2b.4B1.2** | 实施 | 复用既有 floor cache + 后台准备双方法；**两项退出门未达标、变异未跑** | `2026-09-21_g3_2b_4b12_report.md` |
| **G3.2b.4B1.2.1** | 收口 | 逐次归因证明两项「未达标」是计数口径；修一个真实的前台优先缺陷；**13/13 变异全红** | `2026-09-21_g3_2b_4b12_1_report.md` |
| **G3.2b.4B1.2.2** | 纯测试收口 | **生产冻结**；补 raw 读取增量 = 0 与逐像素一致两道门；修正计数窗口；**退出门 A–G 全部达标** | `2026-09-21_g3_2b_4b12_2_report.md` |
| **G3.2b.4B1.2.3a** | 实施 | 真机发现的不对称：cuCIM 在屏时 TopHat 从未被准备；单个等待位被前台自己的方法顶掉；**7/7 变异全红** | `2026-09-21_g3_2b_4b12_3a_report.md` |
| **G3.2b.4B1.2.3b** | 实施 | Full/Compare 往返累计相机漂移：未移动的 Compare here 不再改写 Full Image 位置；**9/9 变异全红** | `2026-09-21_g3_2b_4b12_3b_report.md` |

**没有任何一块宣称真机通过。**

---

## 1. G3.2b.4 —— 冷 Patch / 冷 Tissue 真实分段测量（只读，未修）

首次在**真实演示级数据**上测量：真实 `biopsy.ome.tif`（4 层金字塔）+ Step0 真实写出的
cuCIM/tophat 产物 + 生产全栈，入口只用公开 `mount.show_patch()` / `mount.jump_to_point()`。

- **冷 Patch 与冷 Tissue 确认共用同一条底层路径**（`host.jump_to()` 即合流）。
- **主导成本一：校正来源归约。** 校正产物没有金字塔，一个 cuCIM 通道的完整粗层要把整张
  1.70 GB level-0 数组按 stride 64 归约一遍 —— **8 293 ms**，同片 raw 通道只要 **0.03 ms**；
  四个 cuCIM 通道 **31 936 ms**。
- **主导成本二：GPU 接管期间旧 `ExploreController` 仍全速发请求** —— 每次冷空降多出
  **20–24 个 binding 从未请求过的 RawKey**，占单通道全部读取的 **62 %**，像素永远上不了屏。
- **白名单内无可消除的浪费**：binding 是 `12 请求 → 12 唯一 key → 12 读取 → 12 上传`，零重复。

→ 两个瓶颈都在只读文件（`viewer/step1_source.py`、`viewer/explore_view.py`），
**触发停止条件，未实施**，给出三个最小方案等待裁定。

---

## 2. G3.2b.4A —— 校正粗层归约与 stride=1 读取优化（已实施）

只改 `viewer/step1_source.py` 一个纯读取函数。

- `stride == 1` 不再走归约：先问 `region.overlaps()`（**完全在 ROI 外时零读盘**），
  有交集只发一次读取，按 `placed` 与实际返回形状放回。
- `stride > 1` 改为**有界向量化分块求和**：网格仍锚定 level-0 原点；游标按 block 边界步进；
  每个 slab 按自己的 `placed` 相位补齐后 reshape 求和；计数用两个小向量的外积，
  **边块按实际样本数归一**。
- `_slab_geometry()` 把预算花在 `(rows+stride)×(band+stride)` 而非 `rows×band`——
  否则超预算的会是补齐后的临时数组，而产物侧的门看不见它。**`MAX_REDUCTION_ELEMENTS` 未提高。**

| | 前 | 后 | 倍数 |
| --- | ---: | ---: | ---: |
| CD22 完整粗层（viewer 墙钟） | 8 293.56 ms | **2 108.87 ms** | **3.93×** |
| 四个 cuCIM 通道完整粗层 | 31 936.37 ms | **2 960.73 ms** | **10.79×** |
| CD22 完整粗层（单线程五次中位） | 4 418.27 ms | **1 708.68 ms** | 2.59× |
| stride=1 瓦片（12 块中位） | 4.15 ms | **1.86 ms** | 2.23× |

**数值逐位相同：** 真实 1.70 GB 产物 `max|Δ| = 0.0`、**100 % 逐元素相等**、`valid` 一致；
送进既有 C1 Overlay/Fusion 后 **RGBA 最大差 0 LSB、alpha 完全一致**。

门：新增 74 条（含逐像素独立参考算法、4 320 个参数化案例），强化既有 33 → 42 条；
**八项变异全红**；强制 GPU `90 passed / 0 skipped`，六个保护模块 `161 passed`。

---

## 3. G3.2b.4B1 —— Step0 当前通道双方法后台准备（已实施）

只读核实：`MultiChannelPrefetchController(` 全仓**只有** compare strip 一个构造点；
且 `prefetch_policy.hot_order()` 第一行 `seen = {center}` **按设计排除当前通道**——
这在 Compare 是对的（三栏前台已在准备），在 **Full Image 是错的**（只有一个面板）。

- `viewer/multichannel_prefetch.py` 加 `include_center`（**默认 False**，compare strip 逐项不变）。
- `ui/step0/step0_explore_tab.py` 把同一个生产协调器挂到 Full Image 自己的 stack 上，
  生命周期用 **Qt 自己的 `showEvent`/`hideEvent`**（实测 `QStackedWidget` 页面切换确实把
  hideEvent 投递到后代 widget，所以"Step1 活跃时隐藏 Step0 不再准备"对真实产品成立）。
- **顺带修掉一个真实缺陷：** generation token 是每实例从 0 开始的整数，而 `cancel_generation`
  在共享 scheduler 上**永久**标记过期 —— 离开 Step0 再回来时新实例复用已过期 token
  （实测 12 请求 / 6 完成 / **6 取消**、`settle_aborted = 0`）。改为每实例唯一的 `("hot", n)`。

真实切片实测：静置后由 `CD22/cucim 0/35` 变为 **35/35**；**首次 TopHat→cuCIM
`434.5 → 103.9 ms`（-76 %）**；六次切换 provider 读取两侧都是 **0**。

门 17 条；回归 **`659 passed`**。

---

## 4. G3.2b.4B2 —— 冷 Patch 为什么比冷 Tissue 慢（只读，未修）

真实 `MainWindow`，Patch 用**真实 `QTest.mouseClick` 打在真实 P 按钮**上，
Tissue 用真实 `navigate_requested` 槽。

**唯一原因是视口几何：**

- `show_patch()` 把相机设为**整个 patch bbox** —— 实测恒为 `1465×1088`、12–16 块瓦片，
  **与用户当时缩放无关**；
- `_on_step1_tissue_navigate()` 取当前 `viewRect()` 的 `min(宽,高)`，**保留用户缩放**。

**决定性证据：相同最终视口后差距消失**（两轮 `158.2/107.4` 与 `160.9/168.6 ms`，均 12 块瓦片）。

**缩放扫描：**

| 用户缩放（短边 L0） | Tissue | Patch | 比值 |
| ---: | --- | --- | ---: |
| **300** | 2 块 / **42.6 ms** | 12 块 / **136.6 ms** | **3.21×** |
| 700 | 6 块 / 81.8 ms | 9 块 / 94.7 ms | 1.16× |
| **1500** | 20 块 / 219.5 ms | 12 块 / 153.9 ms | **0.70×**（Tissue 反而慢） |

**被否定：** 无 legacy loader（14 次动作 `PreviewLoaderThread = 0`）、
无 `_ensure_channels_cached` / `_refresh_patch_preview`、session save 在测量窗口内**一次未写盘**、
hidden controller 请求**按视口面积等比例**出现（非 Patch 独有）、两者**都在 level 0**。

**方法学教训（如实记录）：** 第一轮编排不重置相机，结果**毫无差异**——第一次 patch 点击后
相机就是 patch 大小，之后每次 Tissue 都继承它。改正为每次测量前停回同一放大过的停车位。

三个候选方案按贡献排序：C（去掉不可见请求）> B（Patch 保留缩放，**产品语义变更须裁定**）> A（现状）。

---

## 5. G3.2b.4B2C(+.1) —— 停掉 GPU 接管期间的不可见请求（已实施）

`ExploreController` 新增一个布尔 + 公开 `viewport_requests_enabled` /
`set_viewport_requests_enabled()`，**默认 `True`**（Step0、Compare、CPU fallback 逐字节不变）。
只在与既有 `_suspended` 完全相同的判断位置多加一个条件。

**绝不碰**：ViewBox/相机、鼠标、`jump_to`、两个 timer、`interaction_event`、`gesture_quiet`、
`selection_context_changed`、view-rect 查询，以及别的消费者的请求。
**不是 `suspend_for_production`**：不锁相机、不写状态文字、不 join、不排空。

| | 前 | 后 |
| --- | ---: | ---: |
| controller-only 请求（Patch / Tissue） | 29–37 / 18–22 | **0** |
| provider 读取（1 通道，5 次冷 Patch / Tissue 合计） | 224 / 128 | **64（-71 %）/ 26（-80 %）** |
| provider 读取（4 通道） | 416 / 206 | **256（-38 %）/ 104（-50 %）** |
| binding 请求 / 瓦片 / view rect / level / 上传 | | **逐项不变** |
| 4 通道热返回读取 | 66 / 34 | **0 / 0** |

**诚实边界：不声称任何毫秒级加速。** 1 通道把 arm 顺序对调后墙钟无可辨差异；4 通道中位数
有改善但方差极大且测量时本机另有窗口在跑测试。能证明的是**读取量与浪费请求的确定性下降**。

**B2C.1 收口（复核发现的生命周期 blocker，三条全部属实）：** 第一版把"停掉 GPU backend"
一律当成"controller 的图层又是画面了"——(1) 来源重建时唤醒一个**下一行就被销毁**的 controller；
(2) `close()` 在**关闭过程中又发起一轮读取**；(3) `restore_legacy()` 显示的是旧 **Patch** widget
且**下一行就隐藏 whole-slide host**。收口：`_stop_gpu_backend(restore_controller=…)`，
来源重建与 close 传 `False`，`restore_legacy()` 完全移除那次唤醒。
**第一版的三条门把错误行为写成了契约，已一并改正。**

门 15 条；**七项变异全红**（第一轮 M2/M5 曾为绿，据此补了两条门后才全红）；
回归 `291 passed / 0 skipped`。

---

## 6. G3.2b.4B1.1 —— Full Image / Compare 共享瓦片缓存（已实施）

**路径 A（新通道后静止仍重算）在本演示数据上不成立**：九组测量
（Original/TopHat/cuCIM × 静止 1/2/3 s）显示 HOT 每次正确重规划、
**两种方法的当前视野瓦片在 145–315 ms 内全部驻留**、方法切换时**当前视野新计算为 0**。
真正被重算的是 **level+1 回退批次（16 块）** 与 **corrected floor**。

**路径 B 成立，已修**：两个模式各建一份 LRU。改为借用（沿用 `overview_store` 的
"Full Image 拥有、Compare 借用"范式）：

| | 前 | 后 |
| --- | --- | --- |
| raw / corrected 同一对象 | False / False | **True / True** |
| scheduler / provider / controller | 各自 | **仍各自** |
| 进入 Compare 的 corrected tile 计算 | 30 | **12（-60 %）** |
| strip teardown 后 Full 仍驻留 | 45/45 | **45/45** |

**一个回归，已修并加门：** 第一版把 `caches=` 无条件传给 `_stack_factory`，
旧签名 builder 会抛错并被报成 "Compare could not be opened" —— 实测 **97 条门转红**。
收口为「提供而非强加」（`inspect.signature` 判断）。

门 12 条；回归 `584 passed / 1 failed`（既有时序敏感项，单独重跑 3/3 通过、
整个 compare_tiles 文件 `190 passed`，与本块无关）。

---

## 7. G3.2b.4B1.2 —— 复用既有 floor cache + 后台准备双方法（已实施，两项未达标）

**先更正一处事实：** B1.1 说 floor「没有任何缓存」**是错的**。
`ExploreController._floor_cache`（8 entry `OrderedDict`，`7d7bb5a` / `a609c26`）早已存在。
B1.1 报告已加 **§0.1 更正**。实测基线证明它本身完全好用：
**同通道来回翻四次，全部 0 tile、0 `correct_array`、每次命中 +1**。
真正缺口只有**首次使用另一方法**与**跨 controller**。

实现（**无任何新 cache**）：`floor_cache` / `owns_floor_cache` 注入；floor job 输入冻结为
`_FloorRequest`，`foreground=False` 的结果**只**写进 cache；公开
`has_cached_floor()` / `prepare_floor_async()`（**从不调用 `set_selection()`**）；
HOT 在**空闲之后**为中心通道请求另一方法的 floor，并排 level+1 回退层。
前台永远优先（一次最多一个 job，`_floor_pending` 先于**一个不可变的**后台请求值）。
`_floor_cache_limit` 仍 **8**（共享后总共 8 张），`FLOOR_MAX_PIXELS` 未动。

| | 前 | 后 |
| --- | ---: | ---: |
| 首次翻另一方法：`correct_array` | 29 ×4 | **12 ×4** |
| 首次翻另一方法：floor cache 命中 | **+0 ×4** | **+1 ×4** |
| 首次翻另一方法：level+1 tile | 16 ×4 | 12 ×4 |
| Full→Compare：floor cache 同一对象 | `[False×3]` | **`[True×3]`** |
| Full→Compare：`correct_array` | 28 | **7** |

**两项未达标 + 一项未做，均不记为通过：**

1. 退出门 A 要求首次切换 floor `correct_array` = 0，**实测仍有 12 次**（floor 本身确实命中，
   按时机看是切换后 HOT 为新的"另一方法"再做的一轮后台准备，但**未逐次归因**）；
2. level+1 fallback 由 16 降到 12 **而非 0**，两批瓦片**未逐块比对**；
3. **§十四 的九项变异闸门未执行** —— 本块不声称具备变异证据。

门 8 条；`test_explore_controller.py` **134 passed**（既有 floor 门一条未改）；
合集 **`381 passed / 0 failed`**；prefetch 三套 `67 passed`。

---

## 7.1 G3.2b.4B1.2.1 —— B1.2 两项退出门收口（已实施，全部达标）

**B1.2 的两项「未达标」是两处计数口径错误叠在一起，不是机制缺陷。**

1. **「12 次 `correct_array`」就是「12 块 level+1 tile」本身。**
   `CorrectionCompute.compute()` 内部调用 `self.correct_array()`
   （`viewer/correction_compute.py:130`），同一事件被两个计数器各记一次。
   逐次归因（包住 `compute()` / `_calibrate_level_gains()` / `_run_floor_job()` 三个真入口，
   **不用栈回溯**）：四条轨迹切换窗口 **12/12 归因为 `tile`，floor job = 0，floor 命中 +1**。
   「12」在另一处出现纯属巧合：gain calibration 是 `GAIN_WINDOWS(3) × num_levels(4) = 12`。
2. **那 12 块 level+1 是前台的一圈前瞻 ring，不是视口 fallback。**
   前台在 level+1 上要覆盖视口的 4 块 **外加** `FALLBACK_HALO_TILES = 1` 的 12 块前瞻
   （优先级 1000，低于当前层的 100）。HOT 准备前者，**逐块相等**（两个差集都空）；
   后者按「禁止扩大为 ring」故意不准备。**未删除任何合法后台准备**，改的是 benchmark 的分段。

**唯一的生产改动，由变异闸门 3 查出：** `_handle_floor_result` 的后台分支不看
`_floor_pending` —— 后台准备会插到用户正在等的前台 floor 前面，**而且没有等待中的后台请求时
那个前台 floor 根本不会被启动**（这条分支上 `_floor_pending` 无人消费），用户停在
「Preparing corrected preview…」。修复是同一函数前台分支**已经在用**的同两行，**不新增任何状态**。

| | 四条首次方法切换 |
| --- | --- |
| floor job / floor 命中 | **0 / +1** ×4 |
| 画面所需 key 的新增计算 | **0** ×4 |
| level+1 覆盖集计算 / 前瞻 ring 计算 | **0** / 12 ×4 |
| Full→Compare | floor job **0**，三 controller 同一 floor cache 对象，scheduler/provider/controller 仍独立 |

**退出门 A–G 全部达标**（A 的「不重新读取 raw」「逐像素一致」未独立测量，不记为通过）；
**九项变异拆成 13 个逐项施加，13/13 全红**，每次 SHA-256 确认逐字节还原。
如实记录：6d 第一次为绿是**我选错了门**；变异 3 在未变异代码上就是红的（即上面那个真实缺陷）。

回归：floor 门 `8 → 28 passed`；focused `191` + `67`；保护合集 `210 passed`。
`test_step0_compare_tiles.py` 一项既有时序敏感项间歇失败 —— 因果探针显示整份文件里
**后台 floor 结果落地 0 次**（`floor_jobs = 216`），本块改动的分支一次未执行，
且该断言只检查两档请求各出现过、**并不检查顺序**。**如实列出。**

---

## 7.2 G3.2b.4B1.2.2 —— 纯测试收口（生产冻结，退出门 A–G 全部达标）

按复核意见执行。**九个生产文件起止 SHA-256 逐字节相同**，本块只改了
`tests/test_step0_floor_prefetch.py` 与三份文档。

**缺口一：「不重新读取 raw」与「逐像素一致」此前未独立验证。** 两道新门各 4 条轨迹：

- **raw 读取增量：** 断言对象写死为**画面真正由之构成的**瓦片（当前层可见集 + level+1
  覆盖集）。前瞻 ring 的 raw 确实会读（HOT 从不准备它，它不属于这一帧），**按构造排除，
  不是靠容忍值**。四条轨迹：帧内瓦片 raw 读取 **0**、该通道整层 `read_region` **0**、floor job **0**。
  门内先证明同一个匹配器在 arrive 阶段**确实能找到**这些读取记录，所以「切换后为零」不是「匹配了个空」。
- **逐像素一致：** 参照系是 `open_full_image(with_hot=False)` —— 不装 specs provider 时
  `start_hot()` 直接返回，什么都不在后台准备，即本条工作线之前的行为，且只走公开 API。
  两次运行用**同一条数据集路径**（source identity 相同），因此连瓦片的完整 `CorrectionKey`
  都能断言。比的是屏幕上的东西：每个可见瓦片 `ImageItem` 的像素数组、levels、颜色表、放置、
  可见性、key，加 corrected floor 的全部同类项，加 `_level_gain`、仍在透出的 RAW 瓦片集合、
  钉住的 overview。四条轨迹**全部逐像素一致**。门内先断言参照运行至少 4 块带像素的入池瓦片、
  至少一块真的可见、floor 像素非空，**不能靠比空集通过**。
  如实记录：初稿有一项 `raw_layer_visible` 读的是**根本不存在的** `raw_underlay_item`，
  两边都是 `False`，等于没比；已换成真正被 `_update_layer_visibility()` 驱动的两个图层。

**缺口二：计数窗口。** `set(tile_computes)` 的长度被用来切原始列表 —— 一旦有 key 被算过两次，
切片起点就偏早，**切换之前**的计算会被算到切换头上。已改为 `len(tile_computes)`。
如实说明：今天这条门是**侥幸通过**的（该 rig 下确实无重复，但那是另一条门的断言，
不该做本门的隐含前提），修正未改变任何结论；全仓复查确认没有第二处同样写法。

**两道新门都用变异验证过能转红**（floor 不装回去 → raw 门红；恢复 floor 时丢 gain 表 →
像素门红），还原后 SHA-256 逐字节一致。连同 B1.2.1 的 13 项，**本条工作线变异证据 15/15 全红**。

**退出门 A–G 至此全部达标。** 回归：floor 门 `28 → 36 passed`；focused **266 passed**；
保护合集 **210 passed**；`test_step0_compare_tiles.py` **190 passed**（B1.2.1 记录的那项
既有时序敏感失败本次未复现，继续如实挂着）。

**诚实边界：** 逐像素门跑在 rig 的**合成**切片上，真实 `biopsy.ome.tif` 上的逐像素比对未做，
**不声称**；**不宣称真机通过**。

---

## 7.3 G3.2b.4B1.2.3a —— cuCIM 在屏时 TopHat 从未被后台准备（已实施）

真机验收发现的不对称：TopHat→新通道→静止→cuCIM 通过，**cuCIM→新通道→静止→TopHat 失败**，
Original 两种都通过。

**根因（逐事件证据）：** `_prepare_centre_floors()` 无条件请求两种方法；floor 侧按设计只有
**一个可替换的后台等待值**，所以**最后一个请求赢得等待位**，而最后一个永远是 `cucim`：

```
PREPARE tophat cached=False running=cucim  slot: None   -> tophat
PREPARE cucim  cached=False running=cucim  slot: tophat -> cucim    ← 顶掉
RESULT  cucim  fg=True                     ← 前台把 cuCIM 存进 cache
NEXT    waiting=cucim already_cached=True  ← 正确地丢弃
```

用户在看 cuCIM 时，那个最后的请求是**前台正在算的同一件事**的副本，它顶掉了唯一有用的
TopHat 请求，然后被正确去重丢弃 —— TopHat 从未准备。TopHat 在屏时同样被顶替，但幸存的
恰好是前台**没在**算的方法，所以**正向通过是固定顺序的运气**。Original 没有前台 floor job，
第一个请求立即开跑、不占等待位，所以两种都准备。

**本机自然时序下竞态根本不会发生**（`_prepare_centre_floors()` 只在 `_hot_idle()` 后动手，
那时前台 floor 早已完成 —— 实测 597 ms 完成、362 ms 后 HOT 才问），这就是自动化此前没抓到的
原因。诊断用 `--slow-floor`（只在 floor worker 线程、只对 floor 自己的整层数组 sleep，
不改任何决策/key/像素）强制打开窗口后，缺陷逐事件复现，且与真机结果一一对应。

**最小修复（只改 `viewer/multichannel_prefetch.py` 一处）：** 屏幕上那个方法已经有主，
HOT 不再请求它 —— `if method == snapshot.method: continue`。不是换顺序、没有写死方法名
（Original 仍两种都准备）、**不新增任何状态**（无第二等待位、无队列、无 registry/token/
authority/journal/状态机），floor key、上限、前台生命周期、worker/scheduler/算法全未动。
**相机 / Compare / UI 一个文件都没碰**（B1.2.3b 的范围）。

| 强制窗口，同一组设置 | 修复前 | 修复后 |
| --- | --- | --- |
| TopHat → 新通道 → cuCIM | 命中 +1 / 新 job 0 | 命中 +1 / 新 job 0 |
| **cuCIM → 新通道 → TopHat** | **命中 +0 / 新 job 1** | **命中 +1 / 新 job 0** |
| Original → 新通道 → 两种 | 命中 +1 / 新 job 0 | 命中 +1 / 新 job 0 |

自然时序下修复前后四条都是 `+1 / 0` —— 本机碰不到窗口，**修复在这台机器上没有可观测变化，
如实说明，不算成收益**。

**七项变异 7/7 全红**，每次 SHA-256 逐字节还原。如实记录：变异 5 有一个参数化分支是绿的 ——
把判断写死成 `"tophat"` 时「当前显示 TopHat」恰好同值，正因如此该门参数化了三种起始状态，
`[None]` 分支立刻转红；变异 7 临时改的是本块**只读**的 `viewer/explore_view.py`，
只施加→跑门→逐字节还原。

回归：focused **273 passed**；保护合集 **210 passed**；`compare_tiles` **190 passed**；
floor 门 `36 → 43`。B1.2.2 的 raw 门与逐像素门（参数化含 cuCIM→TopHat）继续全绿。

**Advisory：** Full Image→Compare 相机累计漂移（真机第 5 条）属 B1.2.3b，本块禁止处理。
**不宣称真机通过。**

---

## 7.4 G3.2b.4B1.2.3b —— Full Image / Compare 往返的累计相机漂移（已实施）

真机：Full Image → Compare → Full Image，用户在 Compare 内不动任何东西，连续往返却明显向一个方向累计漂移。

**逐段实测（真实 ExploreView / ViewBox / GraphicsScene，真实 QMouseEvent，固定视口像素，十轮）：**
漂移**不在**返回路径，也**不在**布局或 aspect lock —— 那四个接缝**全为 0.000000 屏幕像素**
（entry 落在点击点、面板自身不动、返回精确采用、布局稳定后不变）。
每轮位移**恒为 (−334, −115) 屏幕像素 = 鼠标相对视口中心的偏移**，十轮合计 (−3340, −1150)。
根因只有一条语义：「Compare here」把鼠标下的世界点设为中心，而离开时**无条件**把它带回 Full Image；
固定屏幕像素每轮对应的世界点都被上一轮挪走了。

**最小修复（只改 `ui/step0/step0_page.py`）：** 离开时先问「用户动过没有」。
新增**一个**不可变三元组 `_compare_entry_panel_camera` —— 面板**真正落位之后**从真实 ViewBox 的回读
（拿请求值比会把每次进入都判成移动过，因为三个面板各解各的矩形、`viewPixelSize` 还带 device transform）。
它不驱动任何 ViewBox、只在一处被读。判定在**屏幕空间**做：**先比 scale**（绕不动中心的 zoom 中心一点没动），
再比中心，预算 1 屏幕像素 + 1e-6 相对 scale。**没有补偿常数、没有方向性修正、没有固定 level-0 阈值、
没有第二套相机/事件总线/状态机、没有新增按钮。**

| 十轮合计（屏幕 px） | 修复前 | 修复后 |
| --- | ---: | ---: |
| 固定偏心像素 | **(−3340, −1150)** | **(0.0, 0.0)** |
| 偏离中心半个屏幕像素 | **(10.0, 10.0)** | **(0.0, 0.0)** |
| 每轮真实平移（对照） | (−505.4, −47.1) | **不变** |

Compare here 仍落在点击点；平移 / zoom（中心不变）/ Patch / Tissue 落点仍被带回 Full Image；
只改通道不动相机仍回进入前位置；右键与 Esc 一致；未移动的临时 Compare here 不再永久改写 Step0 共享相机。

**按用户裁定改写了旧测试的产品判断：** `test_step0_compare_toggle_drift.py` 原本论证
「漂移不是 bug、是 Compare here 的自然结果、要无漂移只能用那个已删除的按钮」——
**结论撤回，测量保留**（进入接缝的半像素量化仍作为上界断言存在，只是不再被带出 compare 模式）；
`test_step0_compare_tiles.py` 两条记录旧契约的测试一并改写，并注明它们喂的是**固定 level-0 点**，
这正是它们当初能通过而用户手势却漂移的原因。**没有恢复任何按钮，没有降低断言或减少轮数。**

**9/9 变异全红**，每次 SHA-256 逐字节还原。如实记录：变异 5 有一个门为绿（Patch 落点距离超过了
变异塞进去的 4000 level-0 容差，照样被判为移动过），真正暴露该错误的是平移门。

回归（逐模块）：drift `13 → 27 passed`；compare_tiles 190；patch_compare_navigation 26；
full_image_viewport 8；step1_shared_camera 15；compare_contract 5；overview_camera_ownership 33；
B1.2 保护集九个模块全 passed。
**`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full` 是既有失败**
（还原本块改动后同样失败，纯坐标映射断言），按规则未改动，**如实列出**。

**未修改 CompareStrip、viewer、缓存或科学算子；不宣称真机通过。**

---

## 8. 本次会话改动的生产文件

| 文件 | 由哪个块改 |
| --- | --- |
| `viewer/step1_source.py` | G3.2b.4A |
| `viewer/explore_view.py` | B2C(+.1)、B1.2、**B1.2.1**（一处：后台 floor 落地时先服务欠着的前台 floor） |
| `viewer/multichannel_prefetch.py` | B1、B1.2 |
| `ui/step1_viewer_mount.py` | B2C(+.1) |
| `ui/step0/step0_explore_tab.py` | B1、B1.1、B1.2 |
| `ui/step0/compare_strip.py` | B1.1、B1.2 |
| `ui/step0/step0_page.py` | B1、B1.1、B1.2 |

**本次会话全程未改动**：`viewer/scheduler.py`、`viewer/caches.py`、
`viewer/correction_compute.py`、`viewer/raw_tile_provider.py`、`ui/main_window.py`、
`ui/step1_gpu_binding.py`、`ui/step1_gpu_layer.py`、`ui/step1_viewer_host.py`、
`ui/step1_viewer_binding.py`、`ui/widgets/tissue_navigator_popup.py`、
`ui/step0/**` 其余文件、`core/**`、`workers/**`、shader、`conftest.py`、
`utils/project_write_guard.py`、Step2/3/5/Nexus。

（`ui/main_window.py`、`ui/step1_gpu_binding.py`、`ui/step1_gpu_layer.py`、
`ui/widgets/tissue_navigator_popup.py`、`tests/test_step1_*` 在 `git status` 中显示为已修改，
那是**本次会话之前**的 G3–G3.2b.3.1 未提交工作，本次会话逐块以 SHA-256 验证未触碰。）

### 新增文件

**测试**：`test_step1_corrected_reduction.py`、`test_step1_gpu_request_gate.py`、
`test_step0_method_prefetch.py`、`test_step0_cache_sharing.py`、`test_step0_floor_prefetch.py`

**基准/诊断脚本**（均不被生产导入）：`benchmark_step1_gpu_cold_landing.py`、
`benchmark_step1_corrected_reduction.py`、`benchmark_step1_patch_vs_tissue.py`、
`benchmark_step1_gpu_request_gate.py`、`benchmark_step0_method_switch.py`、
`benchmark_step0_channel_switch_readiness.py`、`benchmark_step0_full_compare_reuse.py`、
`benchmark_step0_floor_prefetch.py`

**报告/数据**：`docs/benchmarks/step1_gpu_demo/2026-09-21_g3_2b_4*.{md,json}`

---

## 9. 全程边界证据

- **`cufile.log`：** 会话结束 `60674` 行、mtime `2026-09-21 04:31:15`（B1.2.1 结束时逐字节相同）。
  G3.2b.4 期间由 cuFile 自行追加 18 行（`60656 → 60674`，非本人写入）；
  此后每一块**均为零增长**（所有运行改从 `/tmp` 启动）。**未截断、未恢复、未清理、未提交。**
- **`docs/tma_study_mode.md`：** SHA-256 全程 `fd9bda07…`，未触碰。
- **`/tmp/save_diag.log`：** 只读，mtime 与大小全程不变。
- **真实演示项目 / OME-TIFF / Zarr / manifest / handoff / ROI / Patch：** 全程只读，
  每块结束后 `find -newermt` 均无输出。Step1 会话自动保存保持开启但写入 `/tmp` scratch。
- **其他窗口：** 全程未停止任何进程（期间有另一窗口在跑 Nexus 测试，未触碰）。
- **未暂存、未提交、未 push。**

## 10. 未进入的范围

方案 B（Patch 保持当前缩放）、G3.2b.4B3（Step1 coarse/fine 顺序）、
Step1 首个 cuCIM 当前视野优先、corrected pyramid、轨迹 B 的旧 stack 后台退休、
Patch 抖动、Patch 下拉栏、Tissue 空降优化、G3.2c、G3.2d、G4 —— **均未开始，也均未获授权。**

## 11. 待裁定事项

1. **G3.2b.4B2 方案 B**：Patch 改为保留当前缩放 —— **产品语义变更，须用户明确批准**。
2. ~~**G3.2b.4B1.2 的两项未达标**~~ —— **已由 G3.2b.4B1.2.1 收口**：逐次归因证明那是
   两处计数口径错误（`compute()` 内部调用 `correct_array()` 造成重复计数；那 12 块 level+1
   是前台的前瞻 ring 而非视口 fallback），退出门 A 本来就成立；九项变异拆成 13 项，13/13 全红。
   顺带修掉一个真实的前台优先缺陷。详见 `2026-09-21_g3_2b_4b12_1_report.md`。
3. **真机验收**：本次会话七块**均未宣称真机通过**，各报告末尾列出了各自的验收步骤。
