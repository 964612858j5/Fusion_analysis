# Step0 看图换用统一看图引擎 计划（第三版，2026-10-26；codex 第 3 轮批准为总体基线，`codex_s0engine_plan_3.out`）

> **用户裁定（2026-10-26）：第一版 §8 五项全部同意，开始执行。**
> - 推翻 10-16 裁定（`docs/v16_5_pre_tma_closeout_plan.md:1`）。
> - 范围：Step0 全图 + 三图对比。
> - 实时预览：甲方案——Python 计算，引擎显示；新增的 Rust 来源已授权。
> - 三图对比：推荐一个 GL 控件，以 S0-0 测量为准。
> - 所有页面共用 `BLOCK01_STEP1_BACKEND`。
> - 与 Step0 现状的画面差异只报告，不作门槛。
>
> 第二、三版只补契约与细节，不改这五项方向。

起点：v17 HEAD a155cb3。底稿：`docs/v16_5_pre_tma_closeout_plan.md` §2.4、§3.2（S0-A/B/C）。

## 1. 现状（只读核对；第一版事实按 codex 修正）

### 1.1 Step0 怎么画图
- **全图**：`Step0ExploreTab`（`ui/step0/step0_explore_tab.py:344`），由 `build_default_stack`（`:203-341`）搭成。
  - 组成：`open_viewer_source` → 原始、校正两个 `LRUByteCache`（512 MiB / 2 GiB，`core/resource_tiers.py` 的 `STEP0_VIEWER_*`）→ `CorrectionCompute` → `TileScheduler` → `ExploreView` + `ExploreController`。DAPI 由 `RawOverlayLayer` 叠加（`viewer/explore_view.py:1744`）。
  - 画法：pyqtgraph + QPainter，每块图一个 `pg.ImageItem`（`:1331`、`:1580`、`:1622`）。没有 GL；Step0 不读后端开关。
- **显示**：
  - 一个标记通道 + DAPI。颜色用 `set_tint`（`:2780`），显示范围用 `set_display_mapping(lo, hi, gamma)`（`:2868`），都在绘制时查表，存储的像素不变。
  - DAPI 有开关；标记就是 DAPI 时不重复叠加。
  - 显示范围的种子来自 `overview_prepared`（`ui/step0/step0_page.py:5787`）。
  - 方法按钮：原图 / TopHat / cuCIM（`step0_page.py:1715-1729`）。按钮切换的是**像素来源**，不是 Overlay / Fusion。
- **三图对比**：`CompareStrip`（`ui/step0/compare_strip.py:344`）。
  - 三套 `ExploreView` / `ExploreController`，共用一个读取源、计算与调度器。
  - 镜头按中心 + 比例联动（`:637-675`）。
  - **冷切换是原子的**：保留旧的三幅完整图并显示 preparing，三幅新图全部准备好后一起发布；对快速第三次点击、等待期间平移、调参都有专门处理（`:785` 起）。
  - 标题、DAPI、指标与显示中的通道保持一致。测试：`tests/test_step0_compare_tiles.py:3200`、`:3496`。
- **Step1 的 Python 回退用的是同一套零件**（`ui/step1_viewer_host.py:525-600`）。但 Step1 挂载的 CPU 回退是 `Step1ComposeCoordinator`（`ui/step1_viewer_mount.py:506`），不是 Step0 的画法。所以 **Step0 不能直接套 Step1 挂载**，见 §3.1。
- **不在本计划内**：
  - 组织预览弹窗 / 导航小图（numpy 缩略图）；
  - Step0 的 ROI 编辑小图；
  - 预分割拼图（Step1，独立 `QOpenGLWidget`，`ui/step1_presegmentation/montage_gpu.py:202`，512 MiB）；
  - Step 0.5 区域 / TMA 页：已经在引擎上（`ui/regions/mount.py:39`），但它复用的是 Step1 来源，不能证明 Step0 的适配已经具备；
  - 无公开产品入口的旧件：`Step15BackgroundCorrectionPage`（测试仍直接调用它）、`BackgroundPreviewWorker`。

### 1.2 实时校正预览
1. 参数改动：`_sync_full_image_param`（`step0_page.py:2933`）→ `show_source`（`step0_explore_tab.py:657`）→ `ExploreController.set_selection`（`explore_view.py:2909`）。
2. `set_selection` 进入临时状态，取消预读，先试缓存整体换图（`:3044`），再建校正底图、发请求。
3. 作废靠代号加 `CorrectionKey`（来源、通道、方法、有效参数、质量、算法版本）。
4. 按层的方案（`viewer/correction_plan.py:64-80`）：
   - SAVED：已保存结果。只在方法、参数、写入身份都匹配，且产品覆盖全图时才用（`:108`）；仅覆盖 ROI 的产品不用。
   - NATIVE：在 0 层校正后平均缩小，是精确的。
   - INTERACTIVE：0 层也用这个标识，但 0 层是精确的；判断"近似"必须用 `is_approximate()`（`:79`）。
5. 计算结果：CPU 上的 float32（`correction_compute.py:166`）。
6. **校正底图（floor）**（`_pick_floor_level_and_stride`，`explore_view.py:3593`）：选满足尺寸条件的最粗层，不一定是金字塔最粗层，必要时再隔点抽样。在 Python 线程上计算，同一个任务里顺便校准每层增益（`:3678`）。
7. **每层增益是显示参数**：用 `lo/g, hi/g` 实现，存储的像素不乘增益（`:2826-2840`）。增益只用于近似层，精确层恒为 1（`:3658`）。校准可能在图块显示之后才完成，完成后显示随之更新（`:4364`）。
8. GPU：CuPy，有 CPU 回退（`core/bg_correction.py`）。

### 1.3 引擎今天
- **来源在建 `ViewerCore` 时固定**（`native/fusion_viewer_core/src/corrected.rs:37-60`）。可选的来源有 `Raw`、`Corrected`、`DisplayTiff`、`Missing`。
- **CPU 缓存的键只有（通道号, 图块）**（`cache.rs:23`）；规划器保留已收到的图块（`plan.rs:241`）；GPU 存储遇到同键不会重传（`gpu_store.rs:170`）。
- Step1 换来源的做法是**重建整个查看器**（`step1_viewer_mount.py:1255-1285`）。这对 Step0 每拖一下滑块来说太重。
- **没有通用的强度 / 校正图块推送接口**。已有的是 Python 提交标签平面的接口（`py/viewer.rs:187`）。
- 重画通知的顺序是 drain socket → ack → 取工作（`notify.rs:8`）。它只表示"需要重画"，不是需求协议。
- 现有的覆盖判定可以复用：`plan/gate.rs::covered`，精确判断视野是否已被某通道的图块完整覆盖。
- 输出：`render/output.rs:11` 把结果 blit 到目标 FBO 的 `(0,0,w,h)`。只设 GL viewport 不会把它挪到别的矩形。
- 显存预算：Step1 / Step3 / 区域页每个查看器 1536 MiB，Step2 小窗 256 MiB（`ui/step2_viewer.py:22`），拼图 512 MiB。隐藏的查看器**通常保留**资源，只在压力或开始计算时释放（`ui/gpu_memory.py`）。`release_to_coarse` 释放纹理和渲染目标，不释放 GL 上下文。
- GL 上下文：现有的引擎查看器 4 个，加拼图 1 个。"WSL 每进程约 5 个"只是 R0 时的环境观察（`tests/test_v16_5_r0_gl_lifecycle.py:3`），没有验证过是普遍的硬上限。

### 1.4 测试
- 42 个 `tests/test_step0_*.py`，静态计数 1030 个 `test_*` 函数定义（参数化之前的数）。
- 很多测试依赖 Step0 的 factory、stack / controller / overlay、缓存、信号和 teardown 契约。对比指标还直接读 Python 缓存（`step0_page.py:7619`）。

## 2. 与 Odon 对照
| 项 | Odon | 本计划 |
|---|---|---|
| 计算出来的图层 | 无 | 新做"由 Python 完成的来源"（§3.3），Odon 无可复用 |
| 多图同屏 | 马赛克：一个画布、一个镜头、一个 tiles renderer 画多个图像（`odon/src/mosaic/mod.rs:196`，`tiles_gl.rs:124`） | 借鉴"一个画布"的思路。但我们是三个各自的 core，所以每个 core 画进自己的离屏 FBO，再由容器拼合（§3.4），不是 Odon 那样一个 renderer |
| 镜头 / 矢量叠加 | 自有 `Camera`；画布内叠加 | 不变：`ExploreController` + 共享镜头；Step0 当前没有矢量叠加 |

## 3. 设计

### 3.1 Step0 自己的挂载，复用同一个引擎（回应 P1-1）
- **不用 `Step1WholeSlideMount`**：它的显示来自 `window._display` 和 Fusion domain，来源来自窗口的 decisions、校正存储和当前 ROI（`step1_viewer_mount.py:239`、`ui/step1_draft_spec.py:95`、`ui/step1_viewer_binding.py:70`），回退也不是 Step0 的画法。
- **新建 `ui/step0/engine_mount.py`**（`Step0EngineMount`），复用同一个引擎：`ViewerCore` 和 `step1_core_adapter` 的清单构建。
  - 复用边界：`Step1CoreLayer` 本身是一个 `QOpenGLWidget`（`ui/step1_core_layer.py:38`），所以 Step0 不按 core 各建一个。S0-A 把它的"一个 core 的 GL 会话"部分（通知 socket、attach / release GL、paint、失败处理）抽成可复用的类 `CoreGlSession`，`Step1CoreLayer` 改为使用它（行为不变，Step1 / 3 / 区域页的引擎测试全跑）；Step0 的 GL 容器（§3.4）为每个 core 持有一个 `CoreGlSession`。
  - Step1 binding 的显示行转换按静态通道名索引（`ui/step1_core_binding.py:121`），Step0 的来源号是动态的，所以 Step0 写自己的行构建，不复用它。
- Step0 现有的 `ExploreStack` 原样保留，作用是：
  - 镜头与交互：`ExploreController` 照旧管 ViewBox；Rust 图层像在 Step1 那样，作为跟随 ViewBox 的兄弟 GL 控件盖在上面；
  - **回退**：引擎启动失败或运行中失败时，隐藏 Rust 图层，重新打开控制器的视野请求（`set_viewport_requests_enabled(True)`），Python 画法立刻接手。这样回退就是今天的 Step0 本身。
- **四个适配点**：
  1. 显示快照：由 Step0 页现有的状态生成，包括标记通道（颜色、lo / hi / gamma）、DAPI（开关、颜色、范围；标记就是 DAPI 时只画一次）、`overview_prepared` 的显示种子，生成 Overlay 行，发给 `core.set_display`。刷新时机就是 Step0 现有设置函数被调用时（`set_tint`、`set_display_mapping`、DAPI 开关）。不接 Step1 的显示设置，不接 `set_mode()`。
  2. 来源：由 Step0 当前的（通道、方法、参数）决定：
     - 原图 → `Raw`；
     - 已保存的全图校正结果（`saved_product` 匹配）→ `Corrected`，只为该通道建清单；
     - TopHat / cuCIM 实时 → S0-A 先走回退（Python 画），S0-B 起走"由 Python 完成的来源"。
     - 永远不读窗口的分析 ROI，Step0 看的是全图。
  3. 镜头与导航框：沿用 Step0 现有接线（`step0_page.py:2376-2463`、`:5778`、`:5842-5872`），全部经过 `ExploreController`，不改。
  4. 回退：上面所述，加上 JPEG 等不支持的格式在启动前就判定。
- **每段结束时产品都完整可用**：S0-A 之后，原图和已保存结果走引擎，实时 TopHat / cuCIM 仍走 Python，切换方法时在两者之间无缝换。

### 3.2 后端开关
所有页面共用 `BLOCK01_STEP1_BACKEND`（用户已同意）。设为 `python` 时，Step0 不建 `Step0EngineMount`，与今天完全一样。现有 Step0 测试默认在 Python 模式下跑，并另加断言确认实际后端；Rust 模式另写测试（§5）。

### 3.3 由 Python 完成的来源（S0-B；回应 P1-2、P1-3、P2-7）
codex 建议"在现有来源 / 发请求路径里加一种由 Python 完成的来源，复用现有缓存、准入和上传"，本版采用，不另建一套图块存储。

- **身份在键里，不靠作废**：
  - 每次选择（通道、方法、有效参数、算法版本）分配一个新的**来源号**，在通道号空间的保留段里分配（例如从 0x8000 起）。
  - 缓存、规划器、GPU 槽位天然按来源号区分，旧选择的图块不可能被当成新的用。
  - 通过 `core.add_python_source(id, format=F32, cache_class=CORRECTED, ...)` 在运行中登记：登记时写明缓存类别（缓存今天的类别集合是建 core 时固定的，`cache.rs:30`，改为可在登记时加入）和准入按 F32 的每块字节数计算。
  - 通过 `core.drop_source(id)` 撤销，**撤销必须覆盖全部状态**：
    - 规划器：粗层状态、计划、已打开记录（`set_active` 今天会保留旧来源的粗层状态，`plan.rs:116`）；
    - 已收图块：`prune_held` 不再保留（`viewer.rs:233`）；
    - CPU 缓存条目；需求表与在途记录；
    - GPU 槽位；
    - **虚拟纹理的页层**：`Vt::slot_of` 今天为每个新通道永久加一层（`vt.rs:91`），页表纹理深度随之增长（`render/upload.rs:15`）。改为撤销时释放该层、新来源复用空闲层。
  - **上界**：每个 core 同时保留的 Python 来源最多 2 个（当前 + 准备中）。登记第三个之前必须先撤销一个（再次调参时丢弃准备中的那个）。所以页层数、缓存条目、GPU 占用都随"保留的来源数"有界，不随调参次数增长。测试：连续调参 1000 次，页层数与内存不增长。
- **需求协议**：
  - 规划器要某块属于 Python 来源的图块时，不交给读盘线程，而是记进该来源的需求表。
  - `core.drain_demands()` 返回（新增需求，撤回需求），撤回指视野移走后不再需要的块。
  - 顺序沿用 drain socket → ack → 取工作。
  - Python 侧把新增需求翻译成 `CorrectionKey` 交给现有调度器，把撤回需求转为取消。
  - **不做预读**：Python 来源不参与引擎的读前；Python 的 HOT 预读继续只填 Python 自己的校正缓存。
- **完成**：
  - `core.complete(id, level, ty, tx, f32 数组, valid)`，只在 GUI 线程调用。
  - 调度器命中缓存时是同步回调（`scheduler.py:275`，不占工作线程），未命中时由工作线程回调。适配器一律把结果放进自己的**待提交队列**，经 Qt 排队信号在 GUI 线程消费，并且绝不在 `core` 调用内部再调 `core`（防重入）。
  - 待提交队列有自己的上界：最多 64 块且 128 MiB（缓存命中也计入）。
    - 额度在**向调度器发请求之前**预留（数量与字节），结果被消费、或请求被取消 / 失败时释放。所以"已发出未完成"与"已完成待提交"合起来不超过上界（codex 第 3 轮 P2：调度器会累积排队与运行中的请求，`scheduler.py:250`，缓存命中同步回调，`:275`）。
    - 额度用完时，需求留在适配器里等。
    - 每个 GUI 轮次最多提交 4 ms 的量，剩下的下一轮再交。
    - 上界数值 S0-0 实测后定。
  - 迟到的完成带着原来源号：来源已撤销的直接丢弃；仍在登记中的照常进缓存（可能马上又用到）。
  - 失败用 `core.fail(...)`，复用 `viewer/failures.rs` 的重试门（200 ms，最多 3 次）。
- **整体换图（与 Step0 现状一致）**：
  - 显示行增加"准备中"标记：这样的行进入规划器的活动集合（`set_display` 今天把显示行直接变成活动集合，`viewer.rs:180`），照常要图、上传、计入准入预算，但**不参与合成**。旧来源的行照常画，平移时旧图跟着走。
  - **就绪的定义**（三条都满足才算）：
    1. GPU 覆盖：准备中来源在 GPU 上驻留的图块的有效矩形，用 `covered()` 判定完整覆盖当前视野。用的是 GPU 驻留，不是 CPU 的 `coarse_complete()`（`plan.rs:164`，它只统计 CPU 交付）。
    2. 绘制成功：把显示行切到新来源后的下一次绘制，画进**待发布的 FBO** 并成功返回。
    3. 同一次发布：宿主核对这次绘制的来源号仍是当前选择（三图时三个都是同一次发布的）。
  - 宿主一直呈现上一次发布的 FBO，三条满足才换成新的；某次绘制失败，就继续呈现旧 FBO，并走现有的失败行为。
  - 准备期间若再次调参，丢弃正在准备的来源，旧图继续保持。
- **粗层**：
  - 引擎要的粗层是金字塔最粗层的完整 512 图块集合（`plan.rs:134`、`admit.rs:73`）。Python 来源的粗层图块按现有逐块方案计算：该层是精确还是近似，照旧由 `plan()` / `is_approximate()` 决定（`correction_plan.py:48`、`:79`），单层或低倍金字塔仍可能是精确层。
  - 因此粗层画面与今天的 floor 画面会不同。按裁定只报告；对照的是现有 floor 算法的输出（不是笼统地与 `compute()` 对照）。
- **增益校准独立保留**（回应 codex 第 2 轮 P1-3）：
  - 今天 floor 任务（含增益校准）在 `viewport_requests_enabled=False` 时被挡住、只记为待办（`explore_view.py:4092`、`:4182`）。引擎接管画面时控制器的视野请求是关着的，所以必须单独放行。
  - 做法：控制器加一个"只校准"状态 `set_engine_owns_view(True)`。在此状态下 floor 任务照常排队与计算（同一个 floor 线程、同一套上下文守卫与缓存），但不把 floor 画到 Python 的图层上；完成时除了现有的 `_level_gain` / `_gain_ctx` 安装，再发一个信号 `gains_ready(ctx, gains)`，Step0 挂载据此发 `set_display`。
  - 生命周期：仍由现有 `suspend_for_production` 等待 floor 线程（`explore_view.py:3859`），换数据集、关闭的排空不变。
  - 回退时：关掉"只校准"状态，打开视野请求，floor 照旧既当画面又给增益，已缓存的 floor 直接可用。
  - 文件：`viewer/explore_view.py`，列入 S0-B。改动范围（codex 第 3 轮 P2）：
    - 三处闸门：`prepare_floor_async`（`:4092`）、`_start_floor_job`（`:4182`）、待办后台任务（`:4429`）；
    - 两条安装路径：缓存命中时安装增益后直接画 floor（`:4157`），计算完成时安装增益并更新可见层（`:4390`）。两条路径在"只校准"状态下都只安装增益并发出 `gains_ready`，不画 floor；缓存命中也必须送出正确的增益。
    - 一个信号 `gains_ready`。
  - 不新建调度器。
- **增益**：
  - 保持"显示参数"性质，像素不乘增益，因此推送的载荷与 `CorrectionCompute` 的输出逐字节一致。
  - 显示行增加按层的增益表 `level_gain[L]`，着色器对每块图按它所在的层用 `lo/g, hi/g`。精确层的增益恒为 1，由 Python 依据 `is_approximate()` 填写。
  - 校准迟到时只需再发一次 `set_display`，不重传任何像素。
  - 这需要改 Rust 的显示行与着色器，属于本段的授权范围。
- **近似角标**：挂载的状态提示加一种"近似预览"，文字沿用 `correction_plan.APPROX_BADGE`。
- **内存上界**（每个 Step0 core）：
  - 需求数不超过规划器所要的块数（视野加粗层，无读前）；
  - 在途计算数不超过调度器并发数；
  - 待提交队列见上（64 块且 128 MiB）；
  - 引擎 CPU 缓存用校正额度，Step0 另设一个较小的额度（建议 512 MiB，S0-0 实测后定），Python 校正缓存 2 GiB 不变；
  - GPU 按查看器预算；三个对比 core 合计不超过一个全图查看器的预算（§3.4）。

### 3.4 Step0 只用一个 GL 容器（回应 P1-5、P2-6）
- 全图和三图对比不会同时显示（同一个 stacked 页面）。所以 **Step0 只建一个 GL 容器** `Step0GlHost`（一个 `QOpenGLWidget`、一个上下文）：
  - 全图模式：1 个 core；
  - 对比模式：3 个 core。
  - 每个 core 先画进自己大小的离屏 FBO（`core.paint(fbo, w, h, view)` 本来就面向目标 FBO，因此不用改 Rust 的 blit 矩形），容器再把这些 FBO 纹理拼到各自的面板矩形。
- **Qt 接线**：三个面板仍然是三个 `ExploreView`，各自负责交互与镜头联动，也可作为回退。容器盖在三者之上并穿透鼠标，按各面板几何与 DPR 换算矩形。
- **每个面板两个 FBO**：已发布的和待发布的。core 只画进待发布的那个；容器只呈现已发布的。
- **三图原子发布**：
  - 每个面板按 §3.3 的就绪定义（GPU 覆盖 + 成功画进待发布 FBO + 同一次发布）报告就绪。容器在三个都就绪之前，继续呈现三幅已发布的 FBO，并显示 preparing；三个都就绪后，在同一帧交换三个面板的两个 FBO。
  - 等待中途若有面板失败，沿用现有的失败行为：失败回调，保留旧图并提示（`compare_strip.py:1048`）。不新增超时机制。
  - 快速第三次点击、等待期间平移、调参，沿用 `compare_strip.py:785` 起的现有规则：Python 侧的状态机不变，只是"就绪"信号改由引擎提供。
  - 标题、DAPI、指标在发布的同一刻更新。
- **资源**：三个 core 共享容器一个上下文；三者显存合计按一个全图查看器的预算分配。
- **上下文总数**：Step0 从 0 变为 1。全部页面合计 = 4 个引擎查看器 + 拼图 + Step0 = 6。约 5 个的上限只是观察，**本计划不保证在笔记本 WSL 上可用**，列入部署前的单独检查。如果确认有上限，后续再申请"离开页面时释放上下文"（不在本计划内）。

### 3.5 生命周期，分情况说明（回应 P1-4）
| 情况 | 计算侧（Python，保留现有排空逻辑） | 引擎侧 |
|---|---|---|
| 普通隐藏（离开 Step0） | 停 HOT（现状） | `pause`（停止需求与呈现）；资源按 `gpu_memory` 规则保留或在压力下 `release_to_coarse`；Step0 core 登记进 `gpu_memory` |
| 全图 ↔ 三图 | 现有 `_hand_gpu_to_compare/_full_image`（`step0_page.py:2877-2910`）：先停 HOT 和挂起的选择，再等共享调度器（`compare_strip.py:1407`） | 被隐藏一方 `pause` 并撤销其 Python 来源；显示一方 `resume` |
| 开始正式计算（含 Save 异步交接） | 现有 `suspend_for_production` / `begin_suspend_for_production`（`explore_view.py:3859`、`:3936`）：停生产者、取消请求、等 floor 与调度器，真正结束后才置完成事件 | 先 `pause`，丢弃所有在途完成（来源号撤销），再 `release_to_coarse` 交出显存；结束后 `resume` 并重新登记来源 |
| 换数据集 | 现有 teardown（`step0_page.py:10810`） | core `close`，容器保留；新数据集重新建 core |
| 关闭程序 | 现有 | core `close` → `release_gl`；线程按 `closing_threads.wait_all` |
| 运行中回退 | 不变（Python 画法接手） | 撤销全部 Python 来源，隐藏图层；本数据集内不再尝试，换数据集时再试（与 Step1 一致） |

### 3.6 不做
- 导航小图、ROI 编辑小图、拼图换引擎；
- 把校正算法写进 Rust；
- 删除 Python 回退；
- "离开页面释放 GL 上下文"；
- 旧件清理。

## 4. 像素与正确性契约（回应 P2-8）
分五项验收，各自独立：
1. **来源身份**：原图、已保存结果、实时三种来源，与（通道、方法、参数）一一对应；已保存结果只在 `saved_product` 匹配时使用。
2. **坐标与有效区域**：图块的世界矩形、边缘图块的有效区与 Python 一致；不受分析 ROI 裁剪。
3. **数值载荷**：引擎回读的 Python 来源图块与 `CorrectionCompute` 的输出逐字节一致；原图与已保存结果沿用 R1 / R2 的读取一致性。
4. **显示映射**：同一载荷下，引擎与 Python 的 lo / hi / gamma / 颜色 / 每层增益结果一致（容差沿用 R2）。
5. **实际呈现**：真实 GL 截图。与今天 Step0 的差异按裁定只报告；三图原子发布用截图序列验证（中间帧不得出现新旧混合）。

## 5. 测试与验收
- 每段：codex 审方案与代码；定向回归。Step0 全部测试在 `python` 模式下通过，并断言实际后端。共享代码（`step1_core_layer`、`step1_core_binding`、Rust）改动后，Step1 / 2 / 3 / 区域页的引擎测试全跑（真实 GL，`DISPLAY=:1`）。
- Rust 模式的新测试覆盖公开操作：
  - 换通道、换方法、调参、DAPI 开关、显示范围；
  - 全图 ↔ 三图、开始计算、换数据集、关闭、运行中回退；
  - 迟到完成、撤回需求、失败重试。
- **性能**（SSD；冷 / 热两种；至少 3 个缩放层级；原图 / TopHat / cuCIM；每项 ≥ 5 次，报中位数与 p95），与今天 Step0（Python）同条件对比：
  1. 调参后第一块正确校正像素上屏；
  2. 视野完整到位；
  3. 三图同时呈现；
  4. GUI 阻塞（>50 ms 次数与最大值）；
  5. 拖动 / 缩放的输入响应（A9 配对规则）。
  - "上屏"指**宿主呈现的合成画面**：用宿主每次发布时记下的帧号，配合截图序列判定。`readback_final` 读的是 core 内部的最终纹理（`py/viewer.rs:266`），只用来验证候选像素，不算上屏。完成回调与缓存命中都不算。
  - 不得变差的项：1、2、4、5；3 只报告。
- 资源：GL 上下文数、显存、进程内存峰值。
- 用户真机验收（每段）。

## 6. 分段（每段单独说"开始"，结束时产品完整可用）
| 段 | 内容 | 主要文件（白名单在各段申请里写全） | 天（初估） |
|---|---|---|---|
| S0-0 | 只读测量：今天 Step0 的性能基线（§5 的 1、2、4、5）；现有引擎查看器每个的显存与上下文数；Step1 引擎与 Step0 Python 在同一显示设置下的画面差异 | 只加脚本 | 2 |
| S0-A | 从 `Step1CoreLayer` 抽出 `CoreGlSession`；`Step0EngineMount` + `Step0GlHost`（全图，1 core，已发布 / 待发布两个 FBO）；原图、已保存结果；显示快照；回退；生命周期表中的全图各行；实时方法仍走 Python | `ui/core_gl_session.py`（新）、`ui/step1_core_layer.py`、`ui/step0/engine_mount.py`、`ui/step0/gl_host.py`（新）、`step0_explore_tab.py`、`step0_page.py` 接线、测试 | 7–10 |
| S0-B | 由 Python 完成的来源：Rust 的来源类型、运行中登记 / 撤销（含规划器、已收图块、缓存类别、虚拟纹理页层复用）、需求 / 完成 / 失败接口、"准备中"行、就绪判定、每层增益；Python 适配器与待提交队列；控制器"只校准"状态；近似角标 | Rust：新建 `python_source.rs`、`viewer/demand.rs`，改 `corrected.rs`、`cache.rs`、`vt.rs`、`plan.rs`、`viewer.rs`、`frame`、`render/programs`、着色器；Python：`ui/step0/python_source.py`（新）、`viewer/explore_view.py`（两处闸门 + 一个信号） | 11–16 |
| S0-C | 三图对比：容器 3 core、离屏 FBO 拼合、原子发布；全图 ↔ 三图交接；HOT 与 Python 来源配合 | `ui/step0/gl_host.py`、`compare_strip.py` 接线、测试 | 7–10 |

合计约 27–38 个工作日，仍是乐观估计。S0-0 只能测现状和已有的引擎，测不了尚不存在的实现。

## 7. 风险
- Python 来源的需求 / 完成 / 撤销是新机制，竞态要专门测（迟到、撤回、重入、关闭中完成）。
- 每层增益要改着色器；会影响 Step1 吗？不会：Step1 的增益表恒为 1，像素测试验证不变。
- 三图原子发布依赖三个 core 都能报告就绪；某一面板失败时，沿用现有失败行为（`compare_strip.py:1048`）。
- 笔记本 WSL 的上下文上限未验证。
- Step0 测试对 Python 内部依赖深：Python 模式必须原样保留 factory、stack、信号、teardown 契约。

## 8. 第二版新增、需要用户知情的点（不改已批准的方向）
1. 实时预览改为"引擎缺图时向 Python 要图"，代替"Python 主动推图"。数值仍全在 Python，引擎不重复存一份。
2. 粗层画面改为逐块计算，不再用今天的整图 floor（floor 只用来校准增益），画面会有差异，只报告。
3. Step0 的全图和三图共用一个 GL 容器，只多占 1 个上下文。
4. 工期从 18–29 天调整为 27–38 天。
5. 第三版：每次调参最多保留 2 个预览来源（当前 + 准备中），资源不随调参次数增长；就绪必须是"显卡上已覆盖 + 新画面已成功画出"；增益校准在引擎接管画面时单独放行。

## 9. S0-0 结果（2026-10-27，只测量，未改产品代码）

条件：服务器 RTX 4090；用户 TMA4 项目副本（`a9_bench/r6/realproj`，私有只读挂载，图像在 NVMe）；Step0 全图，标记 CD45RO；显示 60 Hz（每轮前做刷新检查，16.67 ms），看门程序全程无锁屏、无显示器休眠。冷 2 轮（先清文件缓存）+ 热 2 轮，每轮每项 5 个新参数值。工具：`scripts/a9_drive.py` 新增 `s0method` / `s0param` / `s0channel` 与 `a9.s0_first`（`BLOCK01_A9_WAIT=dataset`），`a9_bench/s00/`（`run_s00.sh`、`make_scen_s00.py`、`s00_report.py`、`lockguard.sh`、`s00_vram_probe.py`）。

### 9.1 今天 Step0（Python）的基线（中位数 / p95，ms；冷热差别很小，下表取冷）
| 项 | 第一块正确校正图上屏 | 视野完整上屏 | >50 ms 卡顿（每次） | 最长卡顿 |
|---|---|---|---|---|
| 调参，全片（第 5 层） TopHat | 30 / 31 | 32 / 32 | 0 | 33 / 35 |
| 调参，全片 cuCIM | 34 / 88 | 35 / 62 | 0 | 36 / 67 |
| 调参，第 4 层 TopHat | 50 / 52 | 52 / 53 | 1 | 55 / 56 |
| 调参，第 4 层 cuCIM | 51 / 69 | 53 / 70 | 1 | 57 / 107 |
| 调参，第 0 层 TopHat（精确） | 75 / 102 | **3802 / 3917** | 2 | 81 / 106 |
| 调参，第 0 层 cuCIM（精确） | 64 / 111 | **1681 / 1781** | 1 | 74 / 115 |
| 拖动（原图 / TopHat / cuCIM） | — | 47 / 42 / 42 | 0 | 31 / 26 / 26 |
| 滚轮缩放（原图 / TopHat / cuCIM） | — | 32 / 31 / 45（p95 63 / 89 / 162） | 0 | 22 / 20 / 34 |
- 第 0 层调参时，最先上屏的是粗层的校正图块（约 70 ms），整个视野要 1.7 s（cuCIM）/ 3.8 s（TopHat）才完整：这是 0 层精确计算本身的耗时。
- 大参数离群：全片视图下 TopHat 半径 53、73 与 cuCIM sigma 93 时，第一块校正图要 2.5–6.1 s（热轮的参数值较大）。随参数增大，不是测量问题。
- 刚切到原图时，"安静"条件 20 s 内一直不满足（每轮都是），推测是后台预读持续读盘；不影响画面，S0-A 时核实。
- 深度：计划的"约第 3 层"实际落在第 4 层（6 格滚轮），三档为第 5 / 4 / 0 层。

### 9.2 显存与上下文
- 一个引擎查看器（Rust，Step1 挂载）画 2 个或 6 个通道，在全片 / 第 4 层 / 第 0 层：引擎 GPU 占用恒为 143 MiB（预留块），进程显存 570 MiB（含驱动开销），内存 0.69–0.75 GB。
- 现有 GL 上下文 5 个（Step1、Step3、Step2 小窗、区域页、预分割拼图）；Step0 加 1 个容器后为 6 个。

### 9.3 未做与偏差
- "Step1 引擎与 Step0 Python 同一显示设置的画面差异"未做：两边显示模型不同（窗口的 Step1 显示设置 vs Step0 自己的设置），对齐成本高，且按裁定只报告。改到 S0-A 做：届时同一个 Step0 页面、同一镜头，只切后端，比较直接可靠。
- 三图对比的基线（三图同时呈现）未测，留到 S0-C 前补测。
- 第 2 段 §3.3 待提交队列的上界（64 块 / 128 MiB）未实测，S0-B 申请时用本节数字估算。

### 9.4 对后续的含义
- 引擎版本的不得变差门槛以 9.1 为准（同条件、同脚本）。
- 主要耗时在 Python 的校正计算（0 层 1.7–3.8 s），引擎换掉的是绘制；预期调参的"视野完整"时间基本不变，拖动缩放有望更快。

## 10. S0-A 申请（第三版，2026-10-27；codex 第 3 轮批准（附两处文字修正，已改），`codex_s0a_app_3.out`；用户已说"开始 A 段"；第一、二版被 codex 否决，`codex_s0a_app_1.out`、`codex_s0a_app_2.out`，本版逐条回应）

### 10.1 范围
全图（1 个面板）上，**原图**与**已保存的全图校正结果**改由引擎画，DAPI 叠加同样由引擎画；**实时 TopHat / cuCIM 仍走 Python**。三图对比、Python 完成的来源、增益表、"只校准"状态都不在本段。

### 10.2 文件白名单
| 文件 | 改动 |
|---|---|
| `ui/core_gl_session.py`（新） | 从 `Step1CoreLayer` 抽出"一个 core 的会话"：通知 socket（drain → ack → 回调）、`attach_gl` / `release_gl` / `forget_gl`、`paint(fbo, w, h, view)` 与报告分类（error / read_failure / capacity_exceeded → 失败回调；deferred / reserving / retry_ms → 要求下一帧）、读盘计数、"绘制"一类的 A9 记录（`gpu.frame`、`gpu.grow`、trace 合并）、`drain_growths` → `gpu_memory` 压力检查。**不含**：QWidget、几何 / DPR、上下文所有权、`frameSwapped` 上的"呈现"记录（`gpu.present`、`coverage_probe.publish`）、标签。（P3） |
| `ui/step1_core_layer.py` | 持有一个 `CoreGlSession`；几何、DPR、上下文、`CoreLabelSurface`、呈现记录留在控件上；`core`、`_initialized`、`_disposed`、`_failed`、`hidden`、`_fail` 照旧可访问（`step1_core_labels.py:53` 的只画标签路径不变）；`dispose` 顺序与返回键不变。 |
| `ui/step0/gl_host.py`（新） | `Step0GlHost`（一个 `QOpenGLWidget`），见 10.5。 |
| `ui/step0/engine_mount.py`（新） | `Step0EngineMount`：来源决定、显示行、换源、失败、生命周期（10.3–10.7）。 |
| `ui/step0/step0_explore_tab.py` | 建栈后建挂载（仅默认工厂建的栈、后端开关为 rust）；`show_source` / `_pull_channel_from_page` 在调用 `set_selection` **之前**先问挂载（10.5.4）；`showEvent` / `hideEvent` / `release_for_production` / `begin_release_for_production` / `resume_from_production` 都只调用一个 `mount.set_running(self._engine_should_run())`，其中"应运行" = 控件可见且未被正式计算挂起（10.7）；`_discard_stack` 先关挂载再拆栈；只读 `backend`。 |
| `viewer/explore_view.py` | **只加两个公开信号**，不改任何现有行为与闸门：`ExploreController.display_changed`（`set_tint`、`set_display_mapping`、`set_marker_visible`、`set_selection`、`_install_overview_record` 末尾）与 `RawOverlayLayer.display_changed`（`set_enabled`、`set_suppressed`、`set_tint`、`set_display_mapping`；校准成功已经走 `set_display_mapping`，失败时另发一次）。codex 第 1 轮已核对这些就是全部改动点。 |
| `native/fusion_viewer_core/src/py/viewer.rs`、`src/frame/shown.rs` | **新增一个只读查询** `ViewerCore.view_covered(view) -> bool`（10.5.2），复用现有 `frame/shown.rs::reach` 与 `plan/gate.rs::covered`，不改任何绘制、规划、缓存行为；重建并安装 wheel。这是 S0-A 唯一的 Rust 改动，需要批准。 |
| `tests/conftest.py` | 按文件名：`tests/test_step0_*.py`（不含 `test_step0_engine_*`）每个测试默认 `BLOCK01_STEP1_BACKEND=python`（monkeypatch，测试结束恢复；测试自己设置的值优先；不影响其他步骤的测试）。 |
| `tests/test_core_gl_session.py`、`tests/test_step0_engine_mount.py`、`tests/test_step0_engine_host.py`（新） | 见 10.8。 |
| `scripts/a9_drive.py` | 引擎模式下 `a9.s0_first` 改读宿主的发布记录（只测量）。 |
| `docs/v17_step0_engine_plan.md` | 本节与结果。 |

`step0_page.py` 不改（codex 第 1 轮核对：对比进入 / 返回、保存的异步交接、导航框接线都经过 explore tab 或同一个 ViewBox）。

### 10.3 来源决定（按控制器的 channel / method / params）
- `method is None` → **原图**：`build_raw_manifest`（与 Step1 同一个函数），`sources=None`。
- `method` 有值且 `correction_plan.saved_product(path, channel, method, params, level_shape(0))` 有结果 → **已保存结果**：Raw 清单 + `build_corrected_manifest(saved.table, raw, strides)`，`roi=None`。判定沿用 `saved_product` 现有全部条件。像素一致性测试时 `BLOCK01_STEP1_DISPLAY_OMETIFF` 必须未设置。
- 其他（实时 TopHat / cuCIM）→ **Python 画**。
- 来源身份 = （通道、方法、参数、已保存结果的 `token`）；身份不变时只发 `set_display`，不建 core。
- 不支持的格式、core 无法导入、GL 初始化失败 → 本数据集一律 Python，日志写原因。

### 10.4 显示行
由控制器与 DAPI 叠加层的现有状态生成（不另存一份）；挂载建好时先取一次快照，之后订阅两个信号，用 `QTimer.singleShot(0)` 合并，行真正变化时才 `set_display`：
- 标记行：`controller.channel`、`display_mapping`、tint（None → 白）、权重 1；Marker 关掉时不发。
- DAPI 行：叠加层通道、`display_mapping`、tint；仅 `effective_enabled` 且已校准时发。
- 叠加模式（`fusion=False`）：着色器把各行颜色相加，与 Step0 的 `CompositionMode_Plus` 一致（codex 已核对）。
- 概览记录装好前（`_blocked_on_overview`）不建 core。
- 已保存结果增益恒为 1。

### 10.5 宿主、发布与换源
#### 10.5.1 恢复已批准的"已发布 / 待发布"契约（回应 P1-1）
- 宿主在自己的上下文里持有：**已发布 FBO**（屏幕上呈现的永远是它）、当前 core 的**待发布 FBO**、候选 core 的**候选 FBO**（`QOpenGLFramebufferObject`，设备像素大小，随尺寸 / DPR 重建）。
- `paintGL`：
  1. 当前 core 画进待发布 FBO（视野 = 此刻 ViewBox 的视野 V）；报告无 error、无 read_failure、无 capacity_exceeded → 待发布与已发布对调；否则不对调（继续呈现上一张），并走 10.6。
  2. 若有候选：候选画进候选 FBO（同一个 V），并做 10.5.2 的就绪判定；就绪 → 候选 FBO 成为已发布，候选成为当前，旧当前暂停为备用（§10.5.6；第三版原文为"`close` 并释放"）。
  3. 已发布 FBO 用 `QOpenGLFramebufferObject.blitFramebuffer` 拷到控件自己的帧缓冲（`paintGL` 里 Qt 已把默认帧缓冲重定向到控件的 FBO）。
- 每次 Rust 调用后，宿主自己重新绑定它要用的帧缓冲（Rust 的 blit 会留下它绑定的 FBO，codex 已指出）。
- **失败优先**（第 2 轮 P2）：第 1 步一旦出现终止性失败（10.6 的任何一种），宿主立刻置"已失败"：本次 `paintGL` 不再推进第 2 步（候选即使就绪也不提升），以后的绘制也不再发布，只呈现最后一张已发布画面；拆除与回退排到下一轮事件循环（与 `step1_core_layer.py:277` 的 `_fail` 同一分工）。
- **尺寸 / DPR 变化**（第 2 轮 P2）：已发布 FBO 不随尺寸立即重建——它保留旧尺寸与旧内容，呈现时按新尺寸缩放拷贝（`blitFramebuffer` 的源 / 目标矩形不同，线性过滤），直到新尺寸的待发布 FBO 画成功并对调；之后旧的才释放。待发布与候选 FBO 按新尺寸重建；候选的就绪判定本来就逐次绘制计算，并且要求候选 FBO 的尺寸等于此刻的设备尺寸、视野等于此刻的 V，所以尺寸或视野一变，之前的"差一点就绪"自然作废。新 FBO 分配失败 = 10.6 的失败。
- "上屏"的 A9 记录只在 `frameSwapped` 上发，且只描述已发布的那张（P3）。
- 稳态多一次全屏 blit（几十微秒级）；S0-C 的三面板拼合用同一套。
- 已在本机真实 GL（`DISPLAY=:1`，PyQt5）验证：`paintGL` 里 `blitFramebuffer(None, …, fbo, …)` 拷到控件帧缓冲，`grabFramebuffer` 读回正确；控件隐藏后 `makeCurrent()` 画进私有 FBO 正确。

#### 10.5.2 就绪 = 对**这次发布的视野**的覆盖（回应 P1-1）
候选的一次绘制同时满足才算就绪：
1. 报告无 error、无 read_failure、无 capacity_exceeded；
2. `core.view_covered(V)` 为真（第三版定义，回应第 2 轮 P1）：
   - **必须覆盖的通道** = `core.display` 的行通道，去重。不是"合成实际画了的通道"——被合成略过的必需通道必须判为不覆盖。
   - 每个必需通道只数**这一帧合成会用的图块**：规划器的 `drawn(c, on_gpu)`（`frame.rs::drawn_rows` 用的同一个函数，只读）中、且在 GPU 上驻留的那些。已被规划器丢掉、但 GPU 槽位还没被挤掉的旧图块不算（第 2 轮的反例：A → B → A 平移时 A 的旧细图块还在显存，但这一帧采样不到）。
   - 这些图块的有效矩形经 `reach` 放宽到片子边缘（与引擎现有的几何容差相同），其并集用 `covered()` 判定是否完整覆盖 V ∩ 片子；任一必需通道没有可用图块即为假。
   - 对**本次绘制的视野**重新计算，不用 hybrid 门"打开后保持"的记忆（第 1 轮反例），与绘制顺序、`coarse_only` 无关。
   - `reach` 是几何容差，不是"每个屏幕像素都有有效样本"的逐像素证明；着色器仍按真实有效区采样。逐像素以宿主截图测试为准，不改渲染器。
3. 这次绘制画进的就是将要发布的候选 FBO（同一个 V、同一次 `paintGL`），发布时核对候选的来源身份仍是当前选择。
- 永远画不出的两种情况按 10.6 失败处理，不会无限等：粗层整体超出预算（`stats()["base_overflow_channels"] > 0`）；任一必需通道在 `stats()["unavailable"]` 里（单通道粗层图块数或字节超限，例如单层 8192×8192 uint16 片子约 128 MiB > 每通道 32 MiB；这时整体 `base_overflow` 可以仍为假）。每次绘制后检查。

#### 10.5.3 宿主的初始化与隐藏时的绘制（回应 P2-4）
- 挂载建好时，宿主 `attach` 到 ViewBox，`show()` + `grabFramebuffer()` 强制初始化（与 Step1 一样）；还没有已发布 FBO 时 `paintGL` 只清成透明并立刻 `setVisible(False)`——同一轮事件循环里完成，不会上屏。初始化失败 → 10.3 的启动失败。
- 宿主可见性（`isVisible`）只表示"已发布 FBO 存在且引擎是画面"；挂载的**运行状态**是另一个标志（10.7）。
- 宿主不可见但有候选时，由宿主自己的驱动显式绘制：通知 socket 回调、或报告要求下一帧（deferred / reserving / retry_ms）时，排一个 `QTimer`，到时 `makeCurrent()` → 候选画进候选 FBO → 就绪判定 → `doneCurrent()`；就绪 → 设已发布、`setVisible(True)`、关闭控制器的视野请求。不依赖 `update()`。

#### 10.5.4 换源的顺序（回应 P1-2）
`show_source` / `_pull_channel_from_page` 在调用 `controller.set_selection` **之前**问挂载 `before_selection(channel, method, params)`：
- 目标是 **Python 画**（实时方法）且当前是引擎：先 `set_viewport_requests_enabled(True)`、隐藏宿主、丢弃候选、当前 core 暂停为备用（§10.5.6），**再** `set_selection`。这样 `set_selection` 发出的 floor 请求不会被闸门挡成 `_floor_pending`（`explore_view.py:4182`），floor 与增益校准照常开始。打开开关时会对旧选择重发一轮请求：通道不变的方法切换只发校正请求、原图的那一代保留（`explore_view.py:3010`），可复用的原图读取会继续，多出一些读盘；不兼容的结果由现有代号 / 上下文检查挡住（`:5377`）。计入测量。
- 目标是**引擎**：`set_selection` 照常调用（控制器状态必须跟上，DAPI / 显示状态都在它那里），之后挂载建候选；来自 Python 画面时控制器的视野请求保持打开，直到候选就绪才关（Python 画面在此期间照常完整）。
- 引擎 → 引擎：建候选，旧画面保持到就绪。
- 测试覆盖：引擎原图 → 未缓存的 TopHat，floor 与增益校准**确实完成**（不是只看开关）。

#### 10.5.5 两个 core 的上限与预算（回应 P2-5）
- 最多两个 core（当前 / 候选 / 备用中的任意两个，§10.5.6）；再次换源时丢弃候选，不排队。
- 1536 MiB 在本项目里是**图块存储额度**（`max_raw_texture_bytes`），不是总分配上限——Step1 / Step3 / 区域页的查看器也是这样，渲染目标另算。Step0 的两个 core **图块存储合计**不超过这一个额度：每个 768 MiB。
- 另外的显存（照实申报，不声称被上述额度约束）：每个 core 的渲染目标约 21 字节 / 设备像素（`render.rs:172`），宿主三个 FBO 各 4 字节 / 设备像素；2560×1440 时约为每 core 73.8 MiB、三个 FBO 合计 42.2 MiB。两个 core 可以长期并存（当前 + 备用，§10.5.6），合计仍在上面的图块存储额度内。宿主的 `gpu_bytes` = 所有 core 的 `gpu_bytes`（含渲染目标）+ FBO，供 `gpu_memory` 统计。S0-0 的 143 MiB 只是观测值，不当作上界。
- CPU 缓存：每个 core 原图 256 MiB、校正 256 MiB。一个 core 最多 512 MiB；两个 core（含备用）最多 1 GiB（其中原图 512 MiB）。Python 栈自己的缓存（原图 512 MiB / 校正 2 GiB）不变。

#### 10.5.6 保留上一个 core（备用，用户裁定 2026-10-27）
- 实测（§10.10）：每次换源都新建 core，新 core 前几帧在 GUI 线程上预热约 4 块显存（每块约 55 ms），切换第一眼 157–210 ms、卡顿 160–290 ms，Python 只要 22–48 ms。用户选"保留上一个内核"。
- 规则：
  - 候选发布时，被替换的当前 core **暂停**（`pause`）并成为**备用**，而不是关闭；引擎让位给 Python 画面时（实时方法），当前 core 同样暂停为备用。
  - 再次需要备用所显示的来源（身份完全相同，含已保存结果的写入身份）时，恢复（`resume`）它作为候选，按 §10.5.2 照常判定就绪后发布；不建新 core、不预热新显存。
  - **仍然最多两个 core**：有当前 core 时要建新 core，先关掉备用；没有当前 core（Python 画面）时，备用 + 新候选 = 2。
  - 备用不跟随镜头、不画（只有当前与候选画）；恢复时先发最新显示行与视野。
  - 失败（§10.6）、换数据集、关闭：备用一起关掉。暂停运行（§10.7）时备用本来就暂停；正式计算开始时 `release_to_coarse` 也包括备用。显存统计包括备用。
- 第一次去某个来源仍要建新 core（约 200 ms）；来回切换（原图 ↔ 已保存、原图 ↔ 实时再回原图）走备用。根治在 S0-B（一个 core 运行中换来源）。

### 10.6 失败（回应 P2-3）
任何一种都在下一轮事件循环里处理，不在 `paintGL` 里：
- 当前或候选 core 的 `attach_gl` 失败、paint error、read_failure、capacity_exceeded、粗层超出预算（`base_overflow_channels > 0`）、必需通道在 `stats()["unavailable"]` 里；宿主 GL 重新初始化失败；FBO 创建失败。
- 处理统一为：丢弃全部 core、隐藏宿主、`set_viewport_requests_enabled(True)`，Python 画法接手（Marker / DAPI / 通道 / 颜色 / 范围都在控制器里，不变）；本数据集不再试，换数据集再试；日志写原因。没有新超时。

### 10.7 生命周期（回应 P2-5）
- "应运行" = explore tab 可见 **且** 未被正式计算挂起（`_released` 为假）。tab 在 `showEvent`、`hideEvent`、`release_for_production`、`begin_release_for_production`、`resume_from_production` 里都只调用 `mount.set_running(应运行)`，所以正式计算结束时 tab 隐藏不会唤醒引擎，tab 显示时正式计算未结束也不会唤醒。
| 情况 | 引擎侧 |
|---|---|
| 停止运行（隐藏 / 进入对比 / 正式计算开始） | 丢弃候选；当前 `pause`；宿主 `hidden=True`（`gpu_memory` 据此在压力下 `release_to_coarse`）；正式计算开始时立刻 `release_to_coarse` |
| 恢复运行 | `hidden=False`、`gpu_memory.on_layer_active`、`resume`，按控制器现状重判来源（保存可能产生了新结果）与显示行 |
| 换数据集 / 关闭 | 先关全部 core（`close` → `release_gl`）、释放 FBO、宿主 `dispose`，再拆 Python 栈 |
| 准备中被临时隐藏 | 不存在：宿主不可见只是"尚无已发布画面"，`hidden` 标志只跟运行状态（P2-5 的区分） |
- 镜头：挂载订阅控制器的 `interaction_event` / `gesture_quiet`，把快照发给当前与候选 core 的 `set_view`（codex 指出只传给 `paint` 不会更新规划）；备用不跟随，恢复时先发最新视野。

### 10.8 测试与验收
- **抽取不变**：Step1 / Step3 / Step2 小窗 / 区域页的引擎测试全跑（真实 GL，`DISPLAY=:1`；GPU 测试不得静默跳过）。
- **Rust 查询**：`view_covered` 的单元测试（覆盖、缺一块、边缘半像素、多通道其一不覆盖、多层混合）。
- **新测试**（真实 GL、合成 OME-TIFF 与合成已保存结果，不碰真实项目），断言实际后端：
  - 后端：rust 起挂载；python 不起；注入工厂的栈不起。
  - 来源：原图 → Raw；已保存且匹配 → Corrected；参数不同 / 只覆盖 ROI → Python；实时 → Python。
  - 像素：引擎画面与 Python 读取 / `SavedProduct.tile` 按同一显示映射算出的结果一致（R1 / R2 容差）。
  - 显示：颜色、范围、Marker、DAPI、标记 = DAPI——只发 `set_display`，不建 core。
  - 发布：**宿主实际呈现的截图**（`grabFramebuffer`）——就绪前是旧画面，就绪后整张是新画面，中间帧没有新旧混合；**候选准备中平移** A → B → A，必须对当前视野、用这一帧会画的图块重新覆盖才发布；第一次可见的那一帧是完整画面；准备期间改窗口尺寸 / DPR；当前 core 失败而候选同一帧就绪时不得提升；发布失败（注入 paint error）旧画面保持并回退。
  - `view_covered` 的 Rust 单元测试另加：规划器丢掉但仍驻留的图块不计；必需通道没有可画图块为假。
  - 换源顺序：引擎原图 → 未缓存 TopHat、**引擎已保存结果 → 未缓存实时**（第 2 轮 P3：可能多算一次 floor，记录并测量，不改 `explore_view.py` 的闸门），floor 与增益校准都完成；Python → 引擎时就绪前宿主不可见、Python 画面完整；连续三次换源最多两个 core。
  - 失败：候选 attach / paint / read / capacity 失败、粗层超预算、必需通道在 `unavailable` 里，都回退；首次就隐藏初始化的路径。
  - 生命周期：hide / show；正式计算开始、在 tab 隐藏时结束（不得唤醒）；换数据集；关闭；显存合计（两个 core + FBO）。
  - 现有 42 个 Step0 测试文件在 python 模式下全过。
- **性能**（同 §9.1 脚本与条件；冷 / 热；第 5 / 4 / 0 层）：原图 / 已保存的拖动、滚轮、整视野到位；原图 ↔ 已保存、原图 ↔ 实时 的首个完整画面；GUI 卡顿。不得变差的项沿用 §5。
  - **需要用户知情的退步风险（回应 P2-6）**：引擎画原图时控制器视野请求是关的，HOT 为当前通道另外两种方法预热的 floor 会被同一闸门挡掉（`explore_view.py:4092`、`:4425`），所以"原图 → TopHat / cuCIM"的第一眼可能比今天慢。本段不改闸门（那是 S0-B 的"只校准"状态）；实测后报告，若超出门槛由用户裁定是否把 S0-B 的那一小部分提前。
  - 顺带核实 §9.1 "原图 20 s 内不安静"。
- **资源**：GL 上下文数（应为 6）、进程显存、内存峰值。
- **画面差异**：同一页面同一镜头只切后端，截图对比，只报告。
- 用户真机验收。

### 10.10 S0-A 实测（2026-10-27，服务器 RTX 4090，60 Hz，TMA4 副本，CD45RO；工具 `a9_bench/s0a/`）
- 条件：同一版代码，`BLOCK01_STEP1_BACKEND=rust`（引擎）与 `=python`（今天的 Step0）同脚本对照。S0-0 场景各 4 轮（冷 2、热 2，种子同 S0-0）；S0-A 场景（已保存的 CD45RO TopHat 15 全片结果，用测量动作 `s0saved` 登记，因为只读副本没有交接也不能保存）各 2 轮（冷、热）。
- **§9.1 的基线需要更正**：0 层"视野完整 3.8 s / 1.7 s"与大参数离群的大部分是 CuPy 第一次遇到某个半径时编译核函数（每个新半径约 3.7 s）；本次用相同半径、核函数已缓存，Python 自己的 0 层视野完整只要 0.24–0.26 s。所以对照一律用本次的 Python 轮。
- 第一版（每次换源新建 core）：拖动缩放变快，但每次换源第一眼 157–210 ms（Python 22–48），卡顿 160–290 ms，原因是新 core 前几帧预热约 4 块 64 MiB 显存（每块约 55 ms）。用户裁定保留上一个 core（§10.5.6）。以下为加了备用之后的数字（中位数 / p95，ms）。

| 项 | Python | 引擎 |
|---|---|---|
| 原图拖动，视野完整 | 47 / 49 | **24 / 24** |
| 原图滚轮，视野完整 | 32 / 62 | **12 / 36** |
| 已保存结果拖动（全片 / 第 4 层 / 第 0 层） | 44 / 50 / 42 | **23 / 24 / 24** |
| 已保存结果滚轮（全片 / 第 4 层 / 第 0 层） | 29 / 60 / 45 | **12 / 12 / 13** |
| 以上手势中 GUI 最长卡顿 | 19–34 | 0（p95 ≤ 35，个别 90） |
| 实时 TopHat / cuCIM 调参（三个深度，Python 画） | 与引擎模式相同（差别在噪声内） | — |
| 全片：已保存 ↔ 原图，第一眼 | 21–24 | 29–30 |
| 第 4 / 0 层：已保存 ↔ 原图，第一眼 | 43–48 | 46–52（p95 139–143：备用 core 在新视野第一次） |
| 原图（引擎）→ 实时 TopHat（Python），第一眼 | 37 / 38 | **103 / 106** |
| 实时 → 已保存（Python → 引擎备用），第一眼 | 38–83 | 80–129 |
| 换源时 GUI 最长卡顿 | 24–54 | 33–88（p95 最多约 200） |

- 结论：
  - 引擎画的内容（原图、已保存结果）拖动 / 缩放的"视野完整"约快一倍，GUI 卡顿基本消失。
  - 实时预览（仍是 Python）不变。
  - 换源仍比 Python 慢：全片约 +7 ms；第 4 / 0 层中位数接近、尾部更长；**原图 → 实时**慢约 65 ms（引擎画原图时 Python 没有原图细图块垫底、HOT 的 floor 预热被闸门挡掉，§10.8 已申报）；实时 → 已保存慢约 40 ms。这些在 S0-B（一个 core 运行中换来源、实时预览也走引擎）里才能根治。
- 未做：与今天 Step0 的截图画面差异对比、GL 上下文 / 显存 / 内存峰值的正式记录（设计上 Step0 多 1 个上下文）。
