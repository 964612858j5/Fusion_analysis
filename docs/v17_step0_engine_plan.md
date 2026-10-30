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

## 11. S0-B 申请（第三版，2026-10-27；codex 第 3 轮批准，`codex_s0b_app_3.out`（两处文字已改）；第一、二版被否决，`codex_s0b_app_1.out`、`_2.out`，逐条回应见 11.10、11.11）

依据 §3.3（第三版已批准的设计）、§3.5、§4、§5、§6；S0-A（§10）与验收修正（`884f02c`、`055be4a`）在 HEAD `055be4a`。本节写 S0-B 的落地方式与它和 §3.3 不同的地方。

### 11.1 范围
- 实时 TopHat / cuCIM（NATIVE 精确层、INTERACTIVE 近似层；SAVED 仍走 S0-A 的已保存结果 core）由引擎绘制：引擎缺图时向 Python 要，Python 用**现有** `CorrectionCompute` 与 stack 的**同一个** `TileScheduler` 计算，交回引擎。
- 不在本段：三图对比（S0-C）；导航小图、ROI 小图、拼图；校正算法进 Rust；删除 Python 回退。
- 用户可见变化（§8 第 2 条，已告知）：粗层实时画面逐块计算，不再用整图 floor（floor 只校准增益），差异只报告；原图 ↔ 实时不再新建 core。

### 11.2 文件白名单
- Rust（`native/fusion_viewer_core/`）：新建 `src/python_source.rs`（登记表）、`src/viewer/demand.rs`（需求、完成、失败、撤销）；改 `src/cache.rs`、`src/vt.rs`、`src/plan.rs`、`src/viewer.rs`、`src/viewer/issue.rs`、`src/viewer/formats.rs`、`src/viewer/failures.rs`、`src/gpu_store.rs`（按通道驱逐，不删块）、`src/frame.rs`、`src/frame/compose.rs`、`src/frame/covered.rs`、`src/render/upload.rs`、`shaders/step1_gpu_vt.frag`、`src/py/viewer.rs`、`src/py/stats.rs`；Rust 单测。
- Python：新建 `ui/step0/python_source.py`；改 `ui/step0/engine_mount.py`、`ui/step0/gl_host.py`、`viewer/explore_view.py`（只校准状态及退出时恢复）、`viewer/scheduler.py`（只加一个 `forget_generation`，见 11.4）、`ui/step1_gpu_vt.py` 与 `ui/shaders/step1_gpu_vt.frag`（参数表加增益列，填 1；两份着色器逐字节相同）；测试；`scripts/a9_drive.py`。
- 其他文件不改；需要时先回来申请。

### 11.3 Rust：Python 来源
- **来源号**：通道号空间保留段 `0x8000..0xFFFF`，同一 core 内单调递增、不复用（到顶拒绝并回退，实际不会到）。
- **登记表** `python_source.rs`：来源号 → 格式 F32、状态（登记 / 撤销）。所有"这个通道是什么"的判断改为先查登记表再查 `sources`：
  - `format_of`（F32）；`update_total` 的非原图判断（加 F32 的块余量，回应第一版 P2-6）；`cache_slots` 的额度（校正额度）；
  - CPU 缓存类别：`Cache` 的校正集合改为可在运行中加入 / 移除（`add_class` / `remove_channel`），登记时加入；
  - 读盘池不认识这些号：Python 来源的键从不进 `pool` 的任何队列（见需求协议）。
- **上界**：每个 core 同时登记最多 2 个（当前 + 准备中），第三个登记返回错误，Python 必须先撤销。测试：换参 1000 次，页层、缓存条目、GPU 占用、失败表、需求表不增长。
- **撤销** `drop_source(id)`（不需要 GL 上下文，回应第一版 P2-7）：
  - 规划器 `Planner::drop_channel`（粗层状态、计划、已打开记录、`fine`、`active` 中的该号）；`held`；`issued` / `issued_ahead`；
  - CPU 缓存 `remove_channel`；需求表、在途表、该号的失败记录（Python 来源的失败表在 `demand.rs`，不在 `pool::Failures`）；
  - GPU：`Store::evict_channel(c)` 只调用现有 `evict`（簿记 + 标脏，不调用 GL、不删块），所以块保留、之后不必重新预热；元数据与页表的变化在下一次绘制（上下文当前）时同步上传；
  - 虚拟纹理：该页层写回空值 **-1**（不是 0；0 指整数族第 0 块第 0 层），放入空闲层表；新来源先用空闲层；`layers()` 改为按已分配深度（`pages.len() / (height*width)`）计算，不再按活着的通道数，避免中间空洞使后面的层不可寻址。
- **需求协议**（`demand.rs`）：
  - `issue_once` 中属于 Python 来源的键：CPU 缓存命中照常立即交付；未命中进该来源的需求表，不进读盘队列，不参与预读。
  - `drain_demands()` 返回（新增, 撤回）：新增 = 本次规划要、之前没交出过、不在缓存、不在在途、且不在失败退避期内；撤回 = 已交出、本次不要、未完成。交出后进在途表。
  - 唤醒：有新增需求时经通知 socket 唤醒 GUI（与读盘结果同一通道、同一 drain → ack → 取工作顺序）。
- **完成** `complete(id, level, tx, ty, data, valid)`：GUI 线程、不在其他 core 调用内部（适配器经排队信号）。已撤销 → 丢弃并计数；形状与 `tile_shape` 不符 → 拒绝并记一次失败；否则进 CPU 缓存（校正类别）、清该键失败计数、规划器要就交付并请求重画。
- **失败**（回应第一版 P2-5）：`fail(id, level, tx, ty, why)` 记入 `demand.rs` 自己的失败表（键 → 次数、时间）。退避：失败后 `RETRY_S`（200 ms）内 `drain_demands` 不再交出；到期且规划器仍要时再交出；单键 `KEY_LIMIT`（3）次或连续 `RUN_LIMIT`（8）次失败即放弃。`check_reads` 同时看这张表：放弃时 paint 报 `read_failure`（现有回退），退避中报 `retry_ms`。成功清该键计数与连续计数；撤销清该来源的全部记录。
- **准备中行**：`set_display` 的行加"准备中"标记：进规划器活动集合（取图、上传、计入准入），`compose` 跳过。
- **试画**（第三版，回应第二版 P2-1）：`paint(fbo, w, h, view, trial=None)`；`trial` 是本次绘制要把哪些准备中行"提升"、哪些旧行"让出"。只改这一次合成用的行，**不调用 `set_display`、不改规划器**（旧来源的图块、计划、已打开记录都不动）。返回值多一项：按试画的行算的覆盖结果。
- **按层增益**：参数表每行 5 → 9 列（新 4 列放 16 层增益）。着色器在采样所用的那一层上用 `lo/g, hi/g`；其他查看器与 Python GPU 路径填 1，像素测试验证不变。
- **覆盖查询**：`view_covered(view, channels=None)`：默认查全部显示行（含新开的 DAPI 行）；给定来源号时只查这些（只作就绪前的提示，发布判定见 11.5）。

### 11.4 Python：适配器 `ui/step0/python_source.py`
- **令牌**（回应第一版 P1-1）：每个适配器实例取一个进程级唯一编号（`itertools.count()`），请求的 generation 为 `("s0b", 适配器编号, 来源号, level, tx, ty, 第几次交出)`。不同 core、同一 core 的不同来源、同一块的重新交出都不会撞号。
- **取消粒度**（回应第一版 P2-4）：每块一个令牌；撤回 → `cancel_generation(该块令牌)`：排队中的直接以 `cancelled` 结束，已开始的完成时以 `stale` 结束；来源撤销 → 对该来源所有在途令牌逐个取消。
- **恰好一次释放额度**：所有请求都设 `notify_on_stale_completion=True`，于是每个请求恰好一次终结回调（命中同步回调、完成、`cancelled`、`stale`、`shutdown`）；额度何时释放见下面"额度的生命"。
- **令牌不累积**：`viewer/scheduler.py` 只加 `forget_generation(gen)`（从 `_stale_gens` 移除）；适配器在一个令牌的终结回调到达后调用它（此时已无等待者持有该令牌）。`_stale_gens` 因此有界。
- **额度与提交**（§3.3）：最多 64 块且 128 MiB；发请求前预留；同步命中也计入；回调只入队（经 Qt 排队信号到 GUI 线程），GUI 每轮最多提交 4 ms，余下下一轮；额度满时新增需求留在适配器里等。
- **额度的生命**（第三版，回应第二版 P2-2）：额度覆盖"已发出未结束"加"已结束待提交"。成功结果的额度一直保留到它被 GUI 提交给引擎、或被丢弃（来源撤销、已撤回、关闭）时才释放；`cancelled` / `stale` / 错误 / `shutdown` 结束时立即释放。
- **令牌的生命**：令牌状态为 在途 → 已结束（终结回调到达时，随即 `forget_generation`）。撤回时只对"在途"令牌调用 `cancel_generation`；"已结束"的令牌不再取消（否则会把已忘记的令牌重新加回 `_stale_gens`），它若还有待提交的结果，直接丢弃该结果并释放额度。
- **载荷**：`TileResult.pixels.handle`（float32 ndarray）原样传入，不乘增益；有效区按结果。
- **键**：`correction_plan.plan(method, params, level, downsample, saved=None)` → `CorrectionKey`（与 Python 画面同一身份，命中同一缓存）。
- 不新建调度器、不新建校正缓存；HOT 预读继续只填 Python 校正缓存。

### 11.5 挂载与宿主：同一 core 内换源（回应第一版 P1-2）
- 原图与实时共用原图 core；已保存结果仍是另一个 core（S0-A 候选 / 备用不变）。
- **画面身份**：`CoreSlot` 增加 `picture`（当前选择的来源号与显示行的指纹），与 `CoreSlot.identity`（core 身份）分开。
- **准备期间**：显示行 = 旧行 + 新来源的准备中行。当前 core 每次绘制照常画旧行并发布，所以**平移时旧图跟着走**（§3.3），不是冻结旧 FBO。
- **切换帧（精确的发布闸门，第三版）**：准备期间所有新加入的行（新来源的行、本次新打开的 DAPI 等）都是准备中行。宿主在一次 `paintGL` 里：
  1. 以 `trial = (提升全部准备中行, 让出被取代的旧行)` 画进待发布 FBO（当前视野、当前尺寸）；
  2. 绘制成功、按试画行算的覆盖满足、且 `picture` 仍是挂载要的选择 → 发布这帧；然后挂载调用 `set_display(最终行)` 使之成为正式显示行（此时才改规划器、才撤销旧来源）；
  3. 否则（覆盖不满足或绘制失败）→ 在**同一次 `paintGL`、同一视野**里再按正常行（旧行，准备中行不合成）画一次并照常发布。所以平移中每一帧都画在当前视野，旧图始终跟着走；规划器从未改变，无需回滚。
  - **空画面例外保留**：试画后的显示行为空（标记与 DAPI 都关）时，与现在宿主相同（`gl_host.py` 约 :438），成功绘制即可发布，不看覆盖。
- **之后**：发布成功后才撤销旧来源（若旧的是 Python 来源）；发布的 FBO 自带像素，不依赖旧来源的纹理。
- **准备期间再次调参**：撤销准备中来源、登记新的，旧图继续。
- **增益**（回应第一版 P1-3 后半）：登记新来源时，挂载先读控制器的当前增益快照 `controller.current_gains()`（新加的只读方法，返回 `(ctx, gains)`）；ctx 与新选择一致就随登记一起下发；之后 `gains_ready(ctx, gains)` 到达时核对 ctx 再更新（不重传像素）。精确层增益恒为 1。
- **近似角标**：当前画面有近似层时显示 `APPROX_BADGE`。

### 11.6 控制器"只校准"状态（`viewer/explore_view.py`）
- `set_engine_owns_view(True)`：floor 任务照常排队与计算（同一线程、同一守卫与缓存），不画到 Python 图层；三处闸门（`prepare_floor_async` 约 :4128、`_start_floor_job` 约 :4217、后台待办 约 :4464）放行；两条安装路径（缓存命中约 :4192、计算完成约 :4410）只安装 `_level_gain` / `_gain_ctx` 并发 `gains_ready`。
- **退出时恢复**（回应第一版 P1-3）：`set_engine_owns_view(False)` 明确做三件事：打开视野请求；当前选择的 floor 在缓存里 → 走现有缓存命中路径画出；不在 → 置 `_floor_pending` 并启动 floor 任务。运行中回退（选择不变）也走这条。
- 生命周期不变：`suspend_for_production` 等 floor 线程；换数据集与关闭的排空不变。

### 11.7 生命周期
| 情况 | 引擎 / 适配器 |
|---|---|
| 离开 Step0 | `pause`；适配器停止交出新需求，在途照常结束（额度按"额度的生命"释放），结果若来源仍登记则进缓存 |
| 开始正式计算 / Save 交接 | `pause`；撤销全部 Python 来源（逐块取消）；`release_to_coarse`；结束后 `resume` 并重新登记当前选择 |
| 隐藏时 Save 完成 | 同上一行，回到 Step0 时按当前选择登记 |
| 核心退役 / 重建（换到已保存结果、备用替换） | 退役 core 的适配器撤销全部来源后丢弃；新 core 用新的适配器编号，令牌不可能与旧的相撞 |
| 换数据集 / 关闭 | core `close`；适配器逐块取消、清空待提交队列 |
| 运行中回退 | 撤销全部 Python 来源；控制器 `set_engine_owns_view(False)`（11.6 的恢复）；本数据集内不再尝试 |

### 11.8 测试与验收（回应第一版 P2-8）
1. Rust 单测：登记 / 撤销清全部状态（规划器、`held`、`issued`、缓存、失败表、需求表、GPU 簿记、页层置 -1 并复用、`layers()` 按深度）；1000 次换参不增长；需求差分；退避与放弃；完成（已撤销丢弃、形状不符拒绝、清失败计数）；准备中行不合成但取图；增益列；`update_total` 含 F32 余量。
2. Python：令牌唯一（core 退役重建、同块重新交出）；逐块撤回取消；恰好一次释放额度（命中、完成、cancelled、stale、shutdown）；`forget_generation` 后 `_stale_gens` 有界；4 ms 提交；回调里不调 core；关闭中完成；控制器只校准的三闸门与两条安装路径（缓存命中也发正确增益）、退出时恢复 floor（缓存命中与需计算两种）；登记前已装的增益通过快照下发。
3. 真实 GL：原图 ↔ TopHat ↔ cuCIM、换参、平移中换参、换参时 DAPI 新打开，截图序列无新旧混合帧；引擎回读的图块与 `CorrectionCompute` 逐字节一致；同一载荷下显示映射一致（含增益）；Step1 / Step3 / 区域页像素不变；core 退役重建后同一调度器继续可用；隐藏时 Save 完成后回来正常。
4. 实测：按 §5（每项至少 5 次）与 §10.10 的做法，**同期**跑 Python 对照（同副本、同条件、同 CuPy 编译缓存状态），冷热各 2 轮；项目：原图 → 实时首块校正图上屏、视野完整、卡顿；换参；拖动 / 缩放；原图 ↔ 实时切换；**准备迟迟不完整时持续平移**（试画 + 正常重画两次绘制的代价）。门槛：不劣于同期 Python；单独报告新块预热造成的延迟（第一版附注 9）。
5. 资源记录（显存、内存、页层数、`_stale_gens` 大小）；全量回归；用户项目副本回放；用户真机验收。

### 11.9 风险
- 需求 / 完成 / 撤销是新机制，竞态专门测。
- 参数表 5 → 9 列影响所有引擎查看器与 Python GPU 路径：两份着色器同改，像素测试兜底。
- 额度与提交时长为估计值，实测后只调数值。
- 工期 11–16 天。

### 11.10 第一版审核意见与回应
| 意见 | 回应 |
|---|---|
| P1-1 令牌跨 core 撞号 | 11.4 适配器进程级编号 + 每块令牌 |
| P1-2 同 core 发布闸门不精确、旧图不跟随平移 | 11.5 单次 paintGL 内的切换帧；准备期间照常发布旧行 |
| P1-3 退出只校准不恢复 floor；登记前已装的增益 | 11.6 显式恢复；11.5 增益快照 |
| P2-4 撤回不取消、额度恰好一次 | 11.4 每块令牌 + `notify_on_stale_completion` + `forget_generation` |
| P2-5 失败退避不是自动复用 | 11.3 `demand.rs` 自己的退避与放弃，`check_reads` 同看 |
| P2-6 `update_total` 漏判 | 11.3 登记表优先 |
| P2-7 页层空值 -1、`layers()`、GL 上下文 | 11.3 -1、按深度、`evict_channel` 不调 GL |
| P2-8 验收 | 11.8 |
| 附注 9 预热 | 撤销不删块（11.3），并在 11.8 单独报告 |
| 事实更正 | 着色器路径与 `pixels.handle` 已改 |

### 11.11 第二版审核意见与回应
| 意见 | 回应 |
|---|---|
| P2-1 试画失败时旧图冻结；还原行会改规划器 | 11.3 试画只改本次合成；11.5 失败时同一 paintGL 按旧行重画并发布 |
| P2-2 成功结果在提交前就释放额度；已忘记的令牌被再次取消 | 11.4 额度保留到提交或丢弃；令牌"已结束"后不再取消 |
| 附注 空画面例外 | 11.5 保留 |

## 12. S0-B 实测后的两处修正（草案，2026-10-27，待用户授权）

### 12.1 实测发现
1. **新显存块反复预热**：Step0 实时预览在引擎上，每次 原图 ↔ 实时 切换、隔一次换参，都新建 GPU 块（`gpu.grow`，每块首用 50–75 ms，在 GUI 线程的绘制里）：TMA4 每轮 32 次，用户项目 59 次；Python 画面 0 次。原因：`store_plan::plan` 在采样器够用时把浮点族的上限定为"当前要画的浮点块数"，画的浮点通道一减少（换参时新旧两张 → 一张；实时 → 原图），`frame::top_up` 调 `fit_caps` 把空闲浮点块删掉（`drop_block`），下次再建、再预热。
2. **用户项目上换参慢 15–60 倍**（`/sda1/Fusion/a9_bench/s0a/user/u7_*`，同期对照）：TopHat 整图换参首张新图 Python 50/73 ms（中位/p95）、引擎 3821/4581 ms；中等缩放 80 vs 1196 ms；cuCIM 54–83 vs 1163–1220 ms。原因（codex `codex_s12_plan_1.out` 更正）：该项目有 4 个补丁，按回车即启动"补丁背景校正"，Step0 让出 GPU（`release_for_production`）。Python 画面**保留并复用**池里已有的瓦片——粗层的半径按 `max(1, round(base/downsample))` 缩放，换参后粗层像素身份常常不变，不必重算（16 次换参 15 次两边都无 `sched.compute`，floor join 0 ms）；引擎按 11.7 撤销全部 Python 来源（新参数是新来源，旧瓦片不复用）、`release_to_coarse`、宿主隐藏，补丁算完（约 1–4 s，含一次 0.6–1.4 s 的空闲全量 GC）才重新登记、上传、再预热新块。TMA4 副本无补丁，故 11.8 实测未暴露。

### 12.2 修正 A：保留空闲块（已做原型，codex 技术批准 `codex_keepblocks_1.out`）
- `store_plan.rs`：`Need.have = [整数族块数, 浮点族块数]`；采样器够用时 `f = d1.max(have[1].min(units - d0.max(1))).clamp(1, units - 1)`——一族保留已有块（含空块），除非另一族需要这些采样器。超额分支不变；预算压力仍走 `make_room` / `reclaim_idle`（先还别的格式的空块）。
- `frame.rs::top_up` 传 `have`。新增单测 2 例（保留、按另一族需要让出）。
- 实测（原型，TMA4 副本 4 轮 `run_k_eng_*`，对同期 Python `b_py`）：新块 33 → 7 次/轮；深层换参首图 78–81 vs Python 114–160 ms、完整 62–65 vs 244–264 ms；拖动 / 滚轮更快且无卡顿；整图 / 中层换参首图中位慢 15–20 ms（p95 相当）；平移中准备后的滚轮仍慢（441–491 vs 190–273 ms）。用户项目上换参慢的问题不受影响（12.3）。
- 影响：Step1/2/3 共用内核，空闲浮点块保留到预算压力、显式释放或销毁；`set_split` 程序有缓存。白名单增加 `store_plan.rs`。

### 12.3 修正 B：补丁背景校正时不交还引擎的图（第一稿，codex 否决 `codex_s12_plan_1.out`，待重写）
补丁背景校正只算几个 512×512 小块，不需要引擎交出显存。改 11.7 第二行，只对这一种交接（`reason == "patch background correction"`；Save / 全片正式计算不变）：
1. **先算完当前视野再让出**（与 Python 的 floor join 对等）：`release_for_production` 前，适配器 `pump` 一次交出当前需求；控制器的 `suspend_for_production` 本就等同一调度器空闲，在途图块因此在交接前算完。
2. **只暂停，不撤销**：`pause` + 适配器 `set_paused(True)`；不撤销 Python 来源、不 `release_to_coarse`；完成的结果照常进缓存（第 3 轮修正已保证暂停时不起新读）。
3. **暂停期间照常绘制已有图块**：宿主不隐藏；`paintGL` 只合成、发布缓存里已有的图块，不读、不算（core 已 `pause`）；切换帧（11.5）照常，新图齐了就发布。
4. 结束后 `resume`，与现在相同。
- 测试：真实 GL，补丁交接期间新参数整图上屏且无新旧混合帧；交接期间 core 读数与适配器请求数不变；Save 交接仍撤销并 `release_to_coarse`。
- 实测：用户项目回放 `make_scen_user7.py`，引擎对 Python 同期；门槛不劣于 Python。

- 第一稿否决要点：①允许暂停时绘制会经 `check_reads` / 重规划 / 失败重试起新读（frame.rs:178、200，failures.rs:70）；②同步 suspend 10 s 超时后仍启动批处理，保留的待算任务可能与批处理同时占 GPU，且 Rust 读池的 pause 不等在途读；③一次 `pump` 不等于当前视野算完（额度 64 块，GUI 阻塞时回调不处理）；④"不需要交出显存"须实测；⑤更小的候选：先准备并发布当前选择、再暂停，批处理中只呈现已发布的画面（gl_host.py:396、536 的现成分支）。
- 另一方向（未审）：同一通道同方法、粗层像素身份相同的瓦片跨来源复用（与 Python 池的行为对等），换参时不必等批处理。

## 13. 修正 B 第二稿（codex 第 1 轮否决 `codex_s13_plan_1.out`，修订见 13.5；第 2 轮否决 `codex_s13_plan_2.out`，修订见 13.6；第 3 轮否决 `codex_s13_plan_3.out`，修订见 13.7；第 4 轮否决 `codex_s13_plan_4.out`，修订见 13.8）：标签相同就沿用——交接前从缓存取齐、发布，交接中只呈现（2026-10-28，用户授权"同意'标签相同就沿用'，先让 codex 审核"）

### 13.1 依据（代码事实）
- 引擎的 Python 来源与 Python 画面用**同一个** `TileScheduler`、同一个 corrected 缓存、同一把"标签"：`PythonSourceAdapter._key` 经 `controller.correction_identity(level, ...)` 得到按层缩放后的有效参数（`correction_plan.plan`）。新参数在粗层的有效参数与旧的相同时，`CorrectionKey` 相同，`scheduler.request` 同步命中缓存（u7 回放：16 次换参 15 次两边都无 `sched.compute`）。
- 所以慢不在"认不出同标签"，而在**时序**：回车 → `_on_dec_param_changed` → `set_selection` → 引擎 `sync` → 当前 core 登记新来源、`request_switch`（试画帧在下一次 paintGL 才做，需求在那时才被 `pump` 取走）→ 同一事件中 `_process_current_channel` → `_release_explore_for_production` → 引擎 `release_for_production` 撤销全部 Python 来源 + `release_to_coarse` + `set_running(False)`（宿主 hidden）。新来源一次也没被 pump 过；补丁批处理结束（1–4 s）后才重新登记、命中、上传、发布。
- Python 画面快的直接原因（codex 更正）：池里同标签的瓦片仍有效，直接跳过请求（explore_view.py:5247），不必等批处理；调度器命中回调虽同步，结果处理经 Qt 排队信号（explore_view.py:2634），不能说"同步上屏"。"新来源一次也没被 pump 过"限于本次回车才提交参数的路径（此前失焦已提交、已开始准备的选择不在此列）。

### 13.2 做法（只针对"补丁背景校正"这一种交接；Save / 全片正式计算的交接不变）
1. **交接前，只从缓存取（不算、不读盘、不排队）**：引擎 `release_for_production(keep_picture=True)`（仅 `_release_explore_for_production` 传；`begin_release_for_production`（Save）不传）先对当前 core 做 `prefill_from_cache()`，全部在 GUI 线程同步完成：
   a. `core.drain_demands()`；撤回的照常处理（同 `pump`）。
   b. 每个新需求按 `_key` 生成 `CorrectionKey`，调新增的只读 `TileScheduler.peek_corrected(key)`：只读 corrected 缓存（`self.corrected_cache.get(key)`），不建 entry、不入堆、不回调、不碰 generation。命中 → 直接 `core.complete_python_tile`（不在任何 core 调用之内）；未命中 → 放进 `_waiting`（与 `pump` 相同，后续照常请求或随撤销丢弃）。不占额度（不经请求），所以没有额度 / 排队回调的问题（否决点③）。
   c. 交出后 core 可能重规划出新需求（`take_reissue`）：重复 a–b，直到一轮没有新命中，最多 `PREFILL_ROUNDS = 4` 轮。
   d. 一次同步绘制：`host.paint_now()`（新增：`makeCurrent` → `_paint_current` → `doneCurrent`，再 `update()` 呈现）。是否发布由现有的切换试画规则决定（11.5：新行齐了才发布，否则旧图不动）。这次绘制发生在暂停**之前**，与平时任何一帧相同（它可能起原始通道的读——那是暂停前的正常行为，暂停时 `pause` 清队列）。
2. **判断能否保留**：`kept = 当前 core 无 pending 切换（slot.pending is None）且 slot.picture 是当前选择的来源`。
   - kept：`set_running(False)`（core `pause`、适配器 `set_paused(True)`、宿主 hidden）；**不**撤销 Python 来源、**不** `release_to_coarse`。批处理期间宿主 `paintGL` 走现成分支：hidden → 不调 core 绘制，只 `_present` 已发布的 FBO（gl_host.py:396、536）——没有暂停中的 core 绘制，否决点①不出现。
   - 否则：与现在完全相同（撤销全部 Python 来源、`release_to_coarse`、hidden）。新图在批处理结束后出来，与 Python 在真需要计算时（cuCIM→31，Python 也等 floor 528 ms）同类。
3. **drain 失败时退回**：kept 之后控制器 `suspend_for_production` 返回 `scheduler_drained=False`（10 s 超时）→ 立即补做第 2 条的"否则"分支（撤销来源 = 取消适配器在途请求）。kept 时适配器已暂停、不再发新请求；暂停前已提交的请求属于同一调度器，drain 覆盖它们（codex 第一稿审阅已确认）。超时后批处理照常启动是现状（Python 画面同样如此），本稿不改。Rust 读池：`pause` 清队列，在途的是 CPU 读盘，结果进 CPU 缓存；hidden 期间无 core 绘制，不上传、不占 GPU（否决点②中与本稿相关的部分）。
4. **结束后**：`_on_production_worker_finished` → `set_running(True)` → `resume`，与现在相同；kept 时来源仍在，`resume` 重新 `issue` 只补缺的块。
5. **显存（否决点④）**：kept 时保留的是引擎当前已驻留的纹理（S0-0 实测单 core 约 143 MiB，进程约 570 MiB），批处理另用 CUDA。实测：用户项目副本回放时记录批处理峰值（nvidia-smi 进程显存，kept 与不 kept 各一轮），写进 13.4；若峰值使批处理失败或明显变慢，再定阈值（届时另行审批），本稿不预设。

### 13.3 改动文件（白名单）
`ui/step0/engine_mount.py`（`release_for_production(keep_picture)`、`prefill_from_cache`、kept 判断、drain 失败退回入口）、`ui/step0/python_source.py`（`fill_from_cache()`：a–c）、`ui/step0/gl_host.py`（`paint_now`）、`ui/step0/step0_explore_tab.py`（传 `keep_picture`；读 `scheduler_drained` 调退回）、`ui/step0/step0_page.py`（`_release_explore_for_production` 传 `keep_picture=True`）、`viewer/scheduler.py`（`peek_corrected`，只读）。不改 Rust。测试：`tests/test_step0_python_source.py`（假 core / 假调度器：命中直接交出、未命中进 waiting、不发请求、轮数上限）、`tests/test_step0_engine_live.py`（真实 GL：①粗层同标签换参 + 补丁交接 → 交接返回时新图已发布、来源未撤销、批处理期间 core 绘制次数 0；②需要重算的换参 → 走旧路径；③Save 交接仍撤销并 `release_to_coarse`；④drain 超时 → 退回撤销）。

### 13.4 验收实测
用户项目副本回放 `make_scen_user7.py`，引擎（本稿）对 Python 同期：换参首张新图门槛不劣于 Python（同标签情形目标 < 100 ms）；需要重算的情形与现状相同；批处理时长、显存峰值、GUI 卡顿记录。

### 13.5 第三稿修订（回应 `codex_s13_plan_1.out`；13.1–13.4 中与下列冲突处以本节为准）
1. **同步试画不派活（blocker 1）**：`paint_now()` 走 `_paint_current(..., pump_adapter=False)`，参数贯穿 `_paint_current → _try_switch → _paint_slot`，含试画失败后的旧图重画；`_paint_slot` 在 `pump_adapter=False` 时不调 `adapter.pump()`。普通 paintGL 默认 True，行为不变。于是预填阶段 Python 侧零新请求、零额度、零计算。文案更正：预填阶段可能有**原始通道的读盘**——`complete_python_tile` 交出后的 `issue()`（demand.rs:189）与试画都会排原始读，这与暂停前任何一帧相同，`pause` 时清队列；不声称零 I/O，只声称零校正计算、零 Python 请求。
2. **超时退回必须等真正停下才开批处理（blocker 2）**：只在 kept 分支需要（旧路径在等待前已撤销来源，维持现状）。`step0_explore_tab.release_for_production` 返回 `(kept, timings)`；kept 且 `scheduler_drained is False` → ① 撤销全部 Python 来源 + `release_to_coarse`（旧路径）；② `controller.begin_suspend_for_production(reason)`（已挂起时它照样等 floor 与调度器真正 idle，explore_view.py:4043 起），返回 event。`_process_current_channel` 的**启动条件变更（明确列入本稿授权）**：拿到 event 时不立即 `start()`，用 50 ms `QTimer` 轮询 event；set 后若数据集代次、当前通道与 worker 未变才 `start()`，否则丢弃这次运行并恢复 viewer（`_on_production_worker_finished` 同路径）。其余情况（kept 且已 drain，或旧路径）立即启动，与现状相同。
3. **账务（finding 4）**：预填只 peek 本轮 `drain_demands()` 新交出的键；命中 → `complete_python_tile`（Rust 侧 succeeded 清 handed/wants），未命中 → 进 `_waiting`（Rust 侧保持 handed，后续 drain 不会重复返回，由暂停解除后的普通 pump 请求或随撤销丢弃）。不 peek 已在 `_waiting` 的旧键（避免命中后 waiting 残留）。撤回键照 `pump` 处理。每轮先完成全部交出、返回后再 `drain`（不在 core 调用内递归）；`PREFILL_ROUNDS = 4` 只是上限，不以轮数推断"取齐"，以试画是否发布为准。`peek_corrected` 会更新 LRU 次序与命中计数（不新增工作）。
4. **区分退回原因（finding 5）**：kept=false 时记 `perf_trace.mark("step0.keep_fallback", why=...)`：`miss`（有未命中键）/ `upload`（全部命中但试画未发布：非最粗层受每帧上传预算限制）/ `not_running` / `drain_timeout`。`paint_now` 只发布到内部 FBO，真正上屏在回到事件循环后的 present；验收以呈现帧计时（a9 的首帧 / 完整帧）为准。若实测 `upload` 常见，再另议（不在本稿）。
5. **只对可见的全图引擎（finding 6）**：预填与 kept 的前提是 mount `_running`、`host.hidden` 为假、`host.current` 存在且为当前选择的 core；Compare 视图或引擎未显示时直接走旧路径。
6. **测试（finding 8）**：真实 core（`fusion_viewer_core.ViewerCore`，非 FakeCore）+ 真实 `TileScheduler`（假 compute 计数）：①部分命中 → 预填 + 同步试画后 scheduler 新请求数 0、额度不变、命中键 handed/wants 清掉、未命中键在 `_waiting` 且暂停解除后被请求一次；②全命中最粗层 → 试画发布、kept、批处理期间 core 绘制 0 次、呈现像素为新参数（对照直接计算的像素）；③非最粗层全命中但上传未齐 → `upload` 退回、无混图帧；④受控慢 compute 跨过 drain 超时 → batch worker 在调度器 idle 前未 `start()`，idle 后启动；⑤kept / 退回两种恢复后画面与来源正确；⑥Save 交接仍撤销 + `release_to_coarse`、等待语义不变；⑦Compare 视图回车走旧路径。验收实测见 13.4，另记首次呈现、完整视野、显存峰值（kept / 非 kept）、退回原因计数。

### 13.6 第四稿修订（回应 `codex_s13_plan_2.out`；**取代 13.5 第 2 条与 13.2 第 3 条**，13.5 其余各条不变）
不再延后启动批处理——`_process_current_channel` 的启动条件**不改**，第二轮的三个生命周期缺口（等待中再回车、等待中换通道、等待中换数据集）因此不存在。改为让 kept 路径在交接时的 GPU 风险**不超过现有旧路径**：
1. **kept 前取消适配器的全部未结束请求，但不撤销来源**：新增 `PythonSourceAdapter.suspend_requests()`，在 `set_running(False)` 之前、控制器 `suspend_for_production` 之前调用：对每个未结束的请求 `scheduler.cancel_generation(token)`，其瓦片放回 `_waiting`（Rust 侧这些键仍是 handed，不会再被 drain 返回；暂停解除后普通 `pump` 再请求一次）。调度器对排队中的任务"无人要了就丢弃不跑"（scheduler.py:635–645），已开始的任务跑完（结果照常进 corrected 缓存，回调以 stale 结束、额度释放、`forget_generation`）。已算好待交出的（`_ready`）保留，暂停中照常交出（现有 `set_paused` 行为）。
2. 于是 kept 路径在等待开始时留在调度器里的适配器工作 = 已开始的任务，与旧路径（撤销来源 → 取消请求 → 已开始的跑完）**完全相同**；同步 suspend 照旧等 floor 与调度器 idle，10 s 超时后照旧启动批处理——这是现状（旧路径与 Python 画面相同），本稿不改，也不新增。
3. drain 超时时 kept 路径不另做处理（暂停中的适配器不发新请求，来源保留，hidden 期间无 core 绘制）；记 `keep_fallback` 不再有 `drain_timeout` 一项，改记 `perf_trace.mark("step0.keep_drain_timeout")` 供实测统计。
4. 测试（替换 13.5 第 6 条之④）：④' kept 前有排队与运行中的适配器请求 → `suspend_requests` 后排队的被调度器丢弃不跑（假 compute 计数不增）、运行中的跑完以 stale 结束、额度归零、瓦片在 `_waiting`；暂停解除后每块恰好请求一次、画面补齐；④'' 等待中再回车 / 换通道 / 换数据集：行为与旧路径相同（现有 busy 门与清理不变，仅回归）。

### 13.7 第五稿修订（回应 `codex_s13_plan_3.out`；补全 13.6 第 1 条的账务）
1. **只重排仍有效的需求（finding 2）**：`suspend_requests()` 对每个未结束请求都 `cancel_generation(token)`（与现在撤回相同），但只有 `self._by_tile.get(tile) == token` 的（未被撤回、Rust 侧仍 handed）才把 `tile` 放回 `_waiting`；已撤回的（`_withdraw` 已删 `_by_tile`，Rust 已删 handed）不重排。
2. **在途的成功结果与重排协调（finding 1）**：取消前已在 Qt 队列里的成功回调仍会进 `_ready`、暂停中照常交出。统一规则：**任何瓦片交给 core 时（`_hand_over` 里 `complete_python_tile` 之后，不论 taken 与否），`self._waiting.pop(tile, None)`**；`_on_ended` 成功入 `_ready` 时同样 `pop`。于是同一瓦片要么因成功结果交出而离开 `_waiting`，要么留在 `_waiting` 由恢复后的 pump 请求一次，不会两者都发生。（taken 为假 = 来源已撤销或形状错，前者无需再请求，后者 Rust 记失败并按退避重新 drain 返回。）`_request_waiting` 对 `_by_tile` 已有活 token 的瓦片跳过（防御，保证同一瓦片同时最多一个请求）。
3. **覆盖所有保留 core（finding 3）**：kept 路径对 `host.cores()` 中每个 slot（current 与 standby）的 adapter 调 `suspend_requests()`；standby 的 adapter 本已暂停，其排队请求在此一并取消。等价性只针对计算任务；保留纹理的显存按 13.2 第 5 条实测。
4. 测试补充（真实 scheduler、假 compute 可控时序）：⑧取消前已发出成功回调（结果在 Qt 队列）→ 交出一次、恢复后不再请求（compute 计数与 `complete_python_tile` 次数各 1）；⑨先平移撤回、请求未终结时回车 → 撤回的瓦片恢复后不被请求；⑩A→B 换通道后 standby 仍有排队请求时回车 → 其排队任务被丢弃不跑；⑪全部场景额度归零、`_by_tile` 无悬挂 token。

### 13.8 第六稿修订（回应 `codex_s13_plan_4.out`）
1. **普通失败也清 waiting（finding 1）**：`_on_ended` 的普通失败分支（error 非 cancelled/stale/shutdown 且来源仍在）在 `_free()` 之前，若 `self._by_tile.get(tile) == token`，`self._waiting.pop(tile, None)`，再 `fail_python_tile`——失败交给 Rust 的退避与上限，之后由 drain 重新返回（200 ms 退避、三次上限不被绕过）。取消 / stale 分支保留有效 waiting（13.7 第 1 条）。统一表述：**一个请求的终结若把该瓦片的结论交给了 Rust（成功交出或失败上报），就从 `_waiting` 移除；只有取消 / stale 终结才把仍有效的瓦片留在 `_waiting`。**
2. **文字修正（finding 4）**：`taken` 为假还包括"已成功缓存但 planner 已不需要"（demand.rs:184 先 `succeeded()` 再 `deliver()`），此时 Rust 侧已清 handed/wants，同样无需再请求；13.7 第 2 条"无条件清 waiting"不变。
3. 不改（finding 3，S0-B 既有 advisory）：已撤回 token 的成功结果与同瓦片新 token 的结果可能都交出——不在本稿范围。
4. 测试补充：⑫普通错误已在 Qt 队列 → 回车取消 → 恢复：不立即重请求，退避后经 drain 重来；连续失败到上限后放弃（与无交接时相同）。

### 13.9 实现与实测（2026-10-28）
- 实现按 13.5–13.8；代码审查 codex 第 1 轮 APPROVED（`codex_s13_code_1.out`，非阻断建议：退回原因把此前未完成的请求也计为 miss——已改；保留帧加像素对照——已加）。Step0 相关 51 个测试文件全通过（含真实 GL 4 例、适配器 5 例新增）。
- 用户项目副本回放（`make_scen_user7.py`，`/sda1/Fusion/a9_bench/s0a/user/u13_*`），换参首张新图（ms，16 次逐次）：
  - Python 画面 `u13_py`：47–85（cuCIM→31 516）。
  - 本稿 `u13_eng`：TopHat 117–248、cuCIM 113–173（→31 1733）；15/16 次保留（1 次 miss）。修正前 `u7_eng` 1163–4581。
  - 本稿 + 修正 A 原型 `u13_keep`（测完已撤回）：TopHat 40–686（中位约 150）、cuCIM 81–139；交接本身由 75–96 ms 降到 7–17 ms（剩下的 75 ms 中 62 ms 是新建显存块的预热）。
- 仍比 Python 慢约 2 倍；交接后到首次呈现之间的其余开销（组织预览合成、空闲 GC 等）未细分，待议。

### 13.10 第 3 步（交接后到真正上屏）的原因与修正（2026-10-28，用户授权"同意显存块修正，然后继续查第3步"）
- 显存块修正 A（12.2）已放入（用户同意）：`store_plan.rs` / `frame.rs` 按 `keepblocks.patch`；Rust 98 测试通过。
- 临时计时点（已撤）实测：回车处理（含预填、试画发布、交接）在 37–46 ms 内结束，但真正上屏要到 99–190 ms；其间 GUI 线程一直被占（一次连续 110–190 ms 的 gui.gap）。对照实验：补丁批处理推迟 300 ms 启动 → 上屏 30–42 ms。结论：批处理线程一启动就读盘（Python 代码持有 GIL），GUI 线程等 GIL，`update()` 排的那次绘制被拖后。
- 修正：`paint_now` 发布后立刻做一次**同步的只呈现重绘**（`_present_only` + `repaint()`；paintGL 在此标志下不做 core 绘制、不推进候选，只 `_present`），在批处理线程启动之前把新图送上屏。批处理启动条件不变。
- 计量更正：Python 画面的 `a9.s0_first` 是"池覆盖"时刻（轮询发现），不是上屏；按"回车 → 视口真正绘制"同口径：Python 约 25–40 ms（同标签时像素本就不变），引擎修正后 42–186 ms、中位约 70 ms（`u13_sync` vs `u13_py2`）。剩余差距：同步重绘要先画整窗其它控件（列表、组织预览等）才到 GL 控件，约 15–95 ms，另行细查。
- 代码审查：第 2 轮否决（`codex_s13_code_2.out`：Qt 对 OpenGL 窗口每个刷新周期最多同步合成一次，紧邻上次合成的 `repaint()` 会变成排队的 UpdateRequest）→ 修正：`presented_now` 计数，未呈现则 `sendPostedEvents(window(), UpdateRequest)` 当场投递；新增紧邻合成测试（去掉投递即失败）。第 3 轮 APPROVED（`codex_s13_code_3.out`）。
- 终版实测 `u13_final`（回车 → 引擎真正呈现）：38–193 ms，中位约 72 ms（15/16 保留，1 次 miss）；拖动 / 滚轮仍优于 Python。剩余差距见上（整窗重绘），未处理。

## 14. 深层缩放换参：旧图保持，新图达到应有清晰度后一次性换上（v1 否决 `codex_s14_plan_1.out`，v2 见 14.5，否决 `codex_s14_plan_2.out`，v3 见 14.6，否决 `codex_s14_plan_3.out`，v4 见 14.7；冲突处以最新小节为准）

### 14.1 用户验收结论与要求
- 用户真机（`Fusion_Final_20260824_Slice1_Scan1.qptiff`，放大到细胞级）：换参后新图"至少 0.5 s"才出现，验收不通过。要求："旧版是立刻直接出新图，我要求一样！不要有切换，不要有体感黑屏，最好直接出校正后的结果，越快越好"；"先出预览、后算补丁"式的可见切换、模糊过渡、黑屏都不允许。
- 回放 `make_scen_user8.py`（细胞级，10 次换参，`u14*`）：深层时有效半径不缩放，几乎每次都是新标签，缓存命中不了。引擎走 kept（旧图保持）或 miss 退回，**新图要等补丁批处理结束、恢复后才计算**：完整新图 1.1 s（预读结束后）/ 1.9–4.1 s（打开后约 1 分钟内，后台 57 通道补丁预读与批处理争盘，批处理读盘 0.7 s → 2.0 s）。Python：`suspend_for_production` 前已开始的预览计算在 floor join / 调度器 drain 中算完（GUI 阻塞 0.3–0.5 s），新图在批处理之前上屏（first 0.25–1.0 s，interactive 质量），精确层在批处理后补齐（约 1.2 s）。即旧画法实际顺序 = 先把眼前的图算完并显示，再算补丁；中间没有可见过渡。

### 14.2 做法（只改补丁交接 keep_picture 路径；Save 不变）
1. `_publish_from_cache` 预填后若有未命中（或此前未完成的请求），在暂停之前**同步算完眼前的图**：新增 `PythonSourceAdapter.compute_now(budget_ms)`：
   a. `_request_waiting()` 照常请求（额度内）；
   b. 在 GUI 线程上等调度器 idle（`notify_when_idle` 的 Event，超时 = 剩余预算），然后 `QCoreApplication.sendPostedEvents(adapter, QEvent.MetaCall)` 把排队的 `_ended` 当场处理，`_hand_over` 不受 `COMMIT_MS` 限制全部交出；
   c. 交出后 core 可能有新需求（reissue / 更细的层）：`drain_demands` → 命中走 peek、未命中再请求，回到 b；直到一轮没有新需求或预算用完。预算 `COMPUTE_BUDGET_MS = 1500`（与 Python 的同步交接同量级；Python floor join 不设上限、drain 10 s）。
   d. 然后 `paint_now()`：试画覆盖则一次性发布并同步上屏（13.10），否则旧图保持。
2. 之后与 13.6–13.8 相同：kept → `suspend_requests` 取消未结束请求、暂停、批处理期间只呈现；未能发布（预算用完、未覆盖）→ 仍走 kept（旧图一直在屏幕上，来源保留，批处理后恢复计算再一次性切换），**不再走撤销来源 + `release_to_coarse` 的退回**（该退回只为显存保守而设；kept 的显存已实测可行——13.2 第 5 条）。miss 计数照记。
3. 不改：试画发布规则（11.5：新行覆盖视野才发布，否则旧行照画——没有过渡帧）；批处理启动条件；Save 交接。
4. 另：打开后的后台补丁预读（`step0.preload_read`）与批处理争盘——本节不处理，单列待办（是否在批处理期间暂停预读需用户决定）。

### 14.3 待审问题
- GUI 线程上等调度器 idle 时，其他生产者（预读线程不经调度器；控制器请求已因 engine 拥有视野而关闭；比较条 HOT 已先停）是否会让 idle 迟迟不到；`notify_when_idle` 的"到达过 idle"语义是否足够。
- `sendPostedEvents(adapter, MetaCall)` 只处理 adapter 的排队 `_ended`，是否有重入（`_on_ended` 内 `pump()`、`complete_python_tile` → `issue()`）问题。
- 试画"覆盖"是否会接受比旧图粗的替代层（`planner.drawn` 含粗层回退？）——若会，则可能出现"先粗后细"的可见切换，需改为要求不粗于旧图所用层。
- 去掉 miss 退回后的显存与 standby 影响。

### 14.4 测试与验收
- 真实 GL：细胞级换参（新标签）+ 补丁交接：交接返回时新图已发布并上屏、批处理启动前；期间屏幕只出现旧图或新图（逐帧像素对照，无中间帧）；预算用完时旧图保持、批处理后一次性切换。
- 回放 `make_scen_user8.py`（引擎 / Python 同条件、预读结束后与预读期间各一轮）：完整新图首次上屏时间不劣于 Python 的首图；记录 GUI 阻塞。

### 14.5 v2（回应 `codex_s14_plan_1.out`）
**事实更正（取代 14.1 第 2 条后半）**：Python 交接先取消排队请求，再 join floor、等 idle，只是收尾已开始的计算；它在细胞级先出现的是 L+1（interactive）的校正瓦片（u14b_py n=21：批处理 146 ms 启动，267 ms 出现 L2 瓦片，视口 L1，目标层 1187 ms 才完整），`a9.s0_first` 只检查一个身份匹配的瓦片。引擎 u14c_eng n=21：60.7 ms 即 `engine_switched/kept`，但目标层比例 0.5，1237 ms 才为 1——**现在的试画发布接受粗层替代**（covered.rs 经 `planner.drawn`，draw.rs:49 含最粗层），即已存在"先粗后细"，只是用户未察觉。

**契约**：旧图持续显示 → 有界准备（反复计算交出、上传、试画）→ 新图在整个可见区域达到**目标层**后一次发布并同步上屏 → 原有补丁交接；预算用完则旧图与 pending 保留，恢复后经同一质量门再一次发布。

1. **质量门（Step0 专用，Rust 小模块）**：`frame/covered.rs` 增 `view_covered_at(core, view, max_level)`——同 `view_covered`，但只计 `t.level <= max_level` 的瓦片（更细或等于目标层）。试画报告增 `trial_exact: Option<bool>`，`max_level` = 本帧准入层（`admission.level`；`coarse_only` / `base_overflow` 时为最粗层，即与现规则相同）。其他 Viewer 不读它，覆盖语义不变。Step0 宿主 `_try_switch` 改用 `trial_exact`（无此键时退回 `trial_covered`）；**所有 Step0 切换都用它**（含无补丁项目的普通换参），即新图不达目标层不发布，旧图照画（11.5 的旧行路径）。稳定视口下准入层即"当前缩放应有的清晰度"；手势中切换不受影响（试画每帧按当帧准入层判断）。
2. **同步准备循环**（`PythonSourceAdapter.prepare_sync(deadline)` + mount 驱动，取代 14.2 第 1 条）：
   - 适配器进入 `_sync` 模式：`_on_ended` 只做记账（入 `_ready` / 释放 / 上报失败），不自动 `pump()`；`_on_ended` 加 `@pyqtSlot(object, object)`，使 `QCoreApplication.sendPostedEvents(adapter, QEvent.MetaCall)` 能定向处理排队的 `_ended`（codex 实验：未声明 slot 时定向投递处理 0 个）。
   - 每轮（统一由 mount 推进，单一 deadline）：① `drain_demands`，命中 peek 交出、未命中进 waiting；② `_request_waiting`（照 64 块 / 128 MiB 额度分批）；③ 定向 flush 排队的 `_ended`；④ `_ready` 全部交出（不受 `COMMIT_MS`，受 deadline）；⑤ `host.paint_now(present=False)`（不 pump；上传受每帧预算，所以要多轮）；⑥ 若已发布 → 退出；若适配器无 waiting / 在途 / ready 且 core 无新需求且本轮无新上传 → 退出（无可再做）；否则 `notify_when_idle` 的 Event 等至多 5 ms（只作唤醒，不作完成证明），进入下一轮。完成以适配器自身请求状态判断。
   - 普通失败照旧交 Rust 退避；退避中的键在本轮不再请求。
   - 退出 `_sync` 前，若已发布则做同步上屏（13.10）。`deadline` = 进入后 1500 ms（**软截止**：停止继续准备；之后的 floor join、drain、同步呈现另计，GUI 总阻塞另行记录，不作承诺）。
3. **kept 范围**：mount `_running`、宿主可见、`host.current` 健康且 identity 为当前选择 → 总走 kept（旧图或已发布的新图在屏幕上，来源与 pending 保留，`suspend_requests` 对 current 与 standby 都做）；不满足（不可见、跨 core、引擎失败）→ 现有撤销退回不变。Save 不变。
4. **显存**：kept 保留"旧图来源 + 未发布的新来源 + standby"——未实测，补测该场景的进程峰值（不新增显存机制，若有问题再议）。
5. **改动文件**：`native/.../frame/covered.rs`、`frame.rs`（报告字段）、`py/viewer.rs`（`trial_exact` 入报告字典）、`ui/step0/gl_host.py`（`_try_switch` 用 `trial_exact`；`paint_now(present=)`）、`ui/step0/python_source.py`（`_sync`、`@pyqtSlot`、`prepare_sync` 各步）、`ui/step0/engine_mount.py`（驱动循环、kept 范围）、测试。
6. **测试**：Rust：`view_covered_at` 只计目标层及更细（粗层覆盖不算）。真实 GL：①细胞级新标签换参 + 补丁交接：粗层先到、细层延迟 → 试画不发布、旧图逐帧不变，细层齐后一次发布（像素对照：只出现旧图帧或完整新图帧）；②超过一批额度（>64 块）；③idle 早于 `_ended` 投递（受控时序）；④计算完成但上传分多帧；⑤预算耗尽 → 旧图保持、批处理后同一质量门一次发布；⑥无补丁项目的普通换参同样不出粗图帧；⑦Save 不变。回放 `make_scen_user8.py`：引擎与 Python 同条件（预读结束后一轮、预读期间一轮，分别比较），记录首个新像素、完整合格新图、批处理启动、GUI 阻塞。

### 14.6 v3（回应 `codex_s14_plan_2.out`；补充 / 取代 14.5 的第 1、2、6 条，其余不变）
1. **旧图定格（取代"试画失败后重画旧行"）**：切换等待期间（`slot.switch` 未清），若视野与设备尺寸与已发布帧相同（`published_view` 一致），试画未过门时**不再重画旧行、不发布**，宿主只 `_present` 已发布的旧 FBO——旧图逐像素不变，不受 planner 重留 / 删除旧细层的影响（plan.rs:189 的 carry 不再影响屏幕）。视野变了（切换等待中用户拖动 / 缩放）才照 11.5 重画旧行跟随相机（手势中的画面本就随准入变化，属现状）。不改 planner。
2. **质量门的目标层（取代 14.5 第 1 条的 `max_level` 定义）**：宿主记录已发布帧的层 `published_level`（报告 `level`，= 该帧准入层）。试画的门槛层 `gate = min(本帧准入层, published_level)`——新图不得比屏幕上的旧图粗。Rust 报告增 `trial_exact` 由 Python 传入 `gate`（`paint` 的 trial 参数加 `max_level`），`view_covered_at` 只计 `level <= gate` 的瓦片。
   - 过渡期准入变粗（旧行 + 新行共算预算，admit.rs:85）而使 `准入层 > published_level`：新图无法在过渡期达到旧图层 → 不发布、旧图定格；准备循环 / 恢复后照常尝试，直至新图达标。为防永不发布：若连续 `GATE_STALL_S = 3 s`（且预算软截止外）仍因 `准入层 > published_level` 不能达标，记 `perf_trace.mark("step0.gate_budget_limited")` 并按准入层放行（预算受限例外，明确记录；回放中统计次数）。
   - `coarse_only`：准入层即最粗层，与现规则相同；`base_overflow`：照现有失败处理（`cannot_draw` → 退回 Python 画面），不放行。
3. **候选发布同门（回应 finding 2）**：`_advance_candidate` 改用 `view_covered_at(view, gate)`，`gate = min(候选准入层, published_level)`（无已发布帧时 = 候选准入层）；`py/viewer.rs` 暴露 `view_covered_at`。候选在自己的 core 上准备，不影响 current 的旧图；current 在候选达标前照常（定格规则同样适用于 current 的无变化视野）。standby 恢复、换通道、saved ↔ live 都经此门。
4. **准备循环的进展信息（回应 finding 1）**：`paint_now(present=False)` 返回本次各绘制的汇总 `{published, uploads, deferred, deferred_growth, again_ms}`；退出条件改为：已发布；或 适配器无 waiting / 在途 / ready、core 无新需求、且最近一次绘制 `uploads == 0 and deferred == 0 and deferred_growth == 0`；否则继续到 deadline。
5. **测试（补充 14.5 第 6 条）**：改写 `test_a_change_the_cache_cannot_publish_hands_off_as_before`（现为 miss 撤销来源；改为：有界准备后 kept、新图达标一次发布或旧图定格）。新增：旧图定格（切换等待中逐帧像素与已发布帧相同，且期间 planner 删除旧细层也不影响）；候选（换通道、standby 恢复）粗层先到不发布；预算收紧导致过渡准入变粗 → 不发布、旧图定格，超时后按例外放行并记标记；`uploads == 0` 但 `deferred > 0` 时循环不退出。保留暂停完成、取消恢复测试的账务断言，并在其恢复后验证严格门。Rust：`view_covered_at` 单测。
6. **文件**：`native/.../frame/covered.rs`（`view_covered_at`）、`frame.rs` 与 `py/viewer.rs`（trial `max_level`、`trial_exact`、暴露 `view_covered_at`）、`ui/step0/gl_host.py`（定格、门槛层、`paint_now` 汇总、候选门）、`ui/step0/python_source.py`、`ui/step0/engine_mount.py`、`tests/test_step0_engine_live.py`、`tests/test_step0_python_source.py`、Rust 单测。不改 planner / admit。

### 14.7 v4（回应 `codex_s14_plan_3.out`；补充 14.6）
1. **层号归一**：报告 `level == -1`（`coarse_only`，frame.rs:298）时，宿主把 `published_level` 与本帧准入层都记为实际最粗层（`core` 的金字塔层数 − 1，经已有的 `level_count` / 由宿主在建核时记下）；`gate` 永远是有效层号（非负）。测试：旧画面为 `coarse_only` 时换参，新图在最粗层达标即发布。
2. **到期唤醒**：切换（或候选）因"准入层 > published_level"被门挡住时，宿主记下首次被挡时刻，并 `QTimer.singleShot(剩余到 GATE_STALL_S 的毫秒, request_frame)`（单个定时器，切换完成 / 放弃 / 取消时停止）；到期那一帧按例外放行并记 `step0.gate_budget_limited`。暂停（hidden）期间不计时、不放行（批处理期间只呈现）；恢复（`set_running(True)`）时若仍被挡，重新起算。候选路径同。测试在不做任何鼠标操作、`update()` 或 `grabFramebuffer()` 的情况下，只靠事件循环等待，验证到期发布。
3. **透传**：`ui/core_gl_session.py` 的 `CoreGlSession.paint` 把 trial 的第三个元素（`max_level`）原样传给 core；白名单加入 `ui/core_gl_session.py`（仅此透传；Step1/2/3 不传第三个元素，行为不变）。

## 15. 换参 0.3 s 硬目标（v1 codex 否决 `codex_s15_plan_1.out`，v2 见 15.5，否决 `codex_s15_plan_2.out`，v3 见 15.6；用户："1.5s是完全不能接受的！整个过程（包括最终成像过程）都不能超过0.3s"）
14 节（v4，`codex_s14_plan_4.out` APPROVED）的契约保留：旧图定格 → 新图达旧图层后一次发布并同步上屏 → 再开补丁批处理；没有过渡帧。本节把**准备预算改为回车起 300 ms 的总上限**，并去掉关键路径上的浪费。

### 15.1 实测（细胞级，`make_scen_user8.py`，补丁批处理临时推迟 3 s 以排除其影响，临时代码已撤）
- 回车 → 清晰新图完整上屏：224–559 ms（中位约 400）。分解（n=15，TopHat→25）：回车处理本身 13–28 ms；此后到引擎首次绘制前 GUI 线程上约 120–150 ms 的 Python 时间：**pyqtgraph 底层 ImageItem 重画 42–124 ms**（引擎拥有视野时这些层被盖住，看不见，却被整窗重绘带着重画，`_rescaleData_nditer` / `_clip`）、**`_set_channel_computing` 的 `setStyleSheet` 6–61 ms**、**空闲全量 GC 108–122 ms**（gc_pacer 恰在此刻触发；另有 0.6–1.3 s 的大 GC）；引擎首帧 78 ms，其中**新建显存块预热 56 ms**；之后约 25 块校正计算从 217 ms 才开始（4 路并行，每块 10–95 ms），其中可见的只有约 8–12 块，其余是为平移预取的。
- 补丁批处理若照常立即启动，再加 0.7–2 s（见 14）。

### 15.2 做法
1. **批处理排在新图之后**（14 v4 的同步准备循环），准备循环截止 = 回车起 `250 ms`（留 50 ms 给同步上屏与余量）；到点未达标 → 旧图定格、开批处理、之后照 14 继续，一次性切换（这时会超过 0.3 s，记 `step0.switch_over_budget`，回放中统计）。
2. **可见优先**：准备循环中只请求当前视野内（试画门所需）的瓦片；预取瓦片（`issued_ahead`）推迟到新图发布之后。适配器请求的优先级：视野内 > 视野外（同层内）。
3. **引擎拥有视野时不重画 pyqtgraph 底层图像**：mount 发布后把控制器的 ImageItem（marker / overlay / floor）`setVisible(False)`，交还 Python 画面（`_to_python`、失败退回）时恢复；`_hold_scene_repaint` 现只挡视口更新，挡不住整窗重绘带来的 ImageItem.paint。
4. **`_set_channel_computing` 不再每次 `setStyleSheet`**：改为预先建好的两种样式之间切换（Qt 动态属性 + 一次性样式表，或缓存同一样式字符串时跳过），避免整行重新 polish。
5. **GC 让路**：`gc_pacer` 在用户输入（键盘 / 鼠标 / 换参）后 `GC_QUIET_S = 2 s` 内不做空闲全量 GC；补丁批处理与准备循环期间也不做。
6. **显存块预热移出关键路径**：实时预览来源上屏后，空闲时预先建好并预热一个空闲浮点块（修正 A 之后它会被保留），换参时不再在首帧里付 56 ms。
7. 其余：14 v4 全部条款（质量门、定格、候选同门、`trial_exact`、唤醒、透传、kept 范围、Save 不变）。

### 15.3 目标与验收
- 回放 `make_scen_user8.py`（细胞级 10 次新标签换参）与 `make_scen_user7.py`（整图 / 中层）：**回车 → 清晰新图上屏 ≤ 300 ms**，逐次报告（不只中位），超出的逐次列原因；过程中只出现旧图帧与完整新图帧（逐帧像素）。预读期间（打开后约 1 分钟）单列。
- 若 2–6 全做仍有超 300 ms 的情形（如半径极大、视野内瓦片过多），如实报告并给出剩余瓶颈，不以放宽质量门换速度。

### 15.4 文件
14 v4 的文件 + `ui/step0/step0_page.py`（`_set_channel_computing`）、`utils/gc_pacer.py`、`ui/step0/engine_mount.py`（ImageItem 隐藏 / 恢复、预热触发）、`ui/step0/python_source.py`（可见优先）、必要时 `native/.../viewer`（预热一个空闲块的入口、视野内需求的区分——若需 Rust 改动另列）。

### 15.5 v2（回应 `codex_s15_plan_1.out`；取代 15.2–15.4）
**原则**：硬目标 = 每一次"回车（参数提交入口）→ 清晰校正新图真实上屏"≤ 300 ms；任何一次超过即该目标未通过（旧图保持、FBO 已发布、worker 完成都不算达标）。实现分两批，先做第一批、逐次实测归因，再定第二批。

**第一批**
1. 实现第 14 节 v4（质量门、旧图定格、候选同门、同步准备循环、批处理在新图之后）。准备循环不设 250 ms 截止：截止 = 14 的软截止（1500 ms，仅作防卡死上限）；超过 300 ms 的每一次如实记为失败并逐次归因（不以提前开批处理来"保住"时间）。
2. **门所需瓦片优先**：适配器在消耗额度、提交 scheduler 之前排序——先是 trial 质量门需要的瓦片（视野内、目标层），再是其余需求（最粗层、辅助层、视野外）；后者在新图发布前不提交（`Cur` / `coarse_only` 时门依赖最粗层：此时最粗层即门所需，照常优先）。Rust 侧需提供"门所需瓦片"的判定：`py/viewer.rs` 增 `gate_tiles(view, max_level)`（视野内、`level == max_level` 的该来源瓦片集合；`coarse_only` 时为最粗层），新文件 `native/.../viewer/gate_tiles.rs`（小模块）；原始 halo 读取属计算本身，不受影响。
3. **持续抑制被遮挡的 Python 图像**：控制器增 `set_layers_suppressed(bool)`（`viewer/explore_view.py`），在 `_apply_*` 可见性规则的出口统一 AND 上"未被抑制"，覆盖 overview、raw / precise pools、corrected floor、DAPI pool 及之后新建的项；mount 发布时置真，`_to_python` 按现有供图恢复顺序先恢复供图、再置假并按**当前**规则重算可见性（不恢复快照）；floor 的增益校准照常（只停绘制）。只作用于全图实例，不影响 Compare 三面板、组织预览与交互层。
4. **`_set_channel_computing` 相同状态跳过**（已是计算中则不再设样式）；实测真实状态切换的代价，若仍显著再议局部绘制。
5. **计时与回放口径**：回放的换参改为向数值框发送真实 `KeyPress`（数字 + Return），使 `gc_pacer` 的输入时间、Qt 事件路径与用户一致；计时起点 = 参数提交入口（`_on_dec_param_entered` 进入时打标），终点 = 新来源达质量门的那一帧 `frameSwapped`（`_presented`），并逐帧像素记录只出现旧图帧或新图帧。场景：细胞级（`make_scen_user8.py`）、整图 / 中层（`make_scen_user7.py`）；冷态（重启后首次）、热态、预读期间、首次换参、Compare 往返、Python 退回；每一次单独列出。

**第二批（第一批实测后再定，各自单独审）**：有界 GC 避让（保留 120 s / 1200 s 兜底的处理方式、退出后补收集、内存峰值验证）；在当前 GL context、空闲且预算允许时经 `frame.rs` 的 caps / split / VT 链尽力预热一个空闲块；整块视野一次性校正（减少每块 halo 重算与 Python→Rust 复制）；补丁预读在准备与批处理期间让盘。

**可行性说明（写给用户）**：按记录的最坏单块 95 ms、12 块、4 路，仅计算就约 285 ms；热态常见情形有望 ≤ 300 ms，冷态、大半径、视野内瓦片多的情形第一批不能保证，需第二批（尤其整块一次性校正）再测。

**文件（第一批）**：14 v4 的文件 + `native/fusion_viewer_core/src/viewer/gate_tiles.rs`（新）、`viewer.rs`（mod 声明）、`py/viewer.rs`（`gate_tiles`）、`viewer/explore_view.py`（`set_layers_suppressed`）、`ui/step0/step0_page.py`（相同状态跳过、提交入口打标）、`scripts/a9_drive.py`（真实 KeyPress）、测试。

### 15.6 v3（回应 `codex_s15_plan_2.out`；修订 15.5 第一批第 2 条）
1. **门所需集合 = 绘制前置依赖 + 目标层可见瓦片**：`gate_tiles(view, max_level)` 返回该来源的（a）planner 绘制前置依赖——`plan/draw.rs` 中 `drawn` 要求的**整层最粗瓦片**中尚未到齐者（`coarse_complete` 之前，`Cur` / `coarse_only` 与 hybrid 一律纳入，避免"发布等粗层、粗层等发布"）；（b）视野内、`level == max_level`（`coarse_only` 时即最粗层，与 a 合并）的瓦片。其余需求（辅助层、视野外非最粗层）在新图发布前留在适配器 `_waiting`，不丢弃、不提交；Rust 侧它们保持 handed（demand.rs:54），不会重复交出；发布后普通 `pump` 照常请求。
2. **分类时机**：每轮准备循环、以及恢复后每次 `pump`，都按当前 trial 来源、当前视野与当前门槛层重新分类（视野变了门集合随之变）；撤回照常处理。发布（或放弃切换）后不再分类，恢复普通顺序。
3. **抑制只影响绘制**：`set_layers_suppressed` 只在各 ImageItem 的可见性出口 AND"未抑制"，不改 DAPI 的 `effective_enabled`（engine_mount.py:570 用它决定引擎是否画 DAPI）、不改 floor 校准、不改任何"是否启用"的状态。
4. 验收补充：视野外最粗瓦片尚未到齐时换参（新开数据集后立即换参）——门集合包含它们，新图仍能发布；逐次计时照 15.5 第 5 条。

### 15.7 第一批实现与实测（2026-10-28，用户："同意先做第一批，同时可以放宽到0.4s"）
- 实现：Rust `frame/covered.rs`（`view_covered_at`、`counts`）、`frame.rs`（`Trial.max_level`、`trial_exact`）、`viewer/gate_tiles.rs`（新）、`plan.rs`（只读 `coarse_has`）、`py/viewer.rs`（`trial_level`、`view_covered_at`、`coarsest_level`、`gate_tiles`）；`ui/core_gl_session.py`（透传）；`ui/step0/gl_host.py`（定格、门、`GATE_STALL_S` 定时器、`paint_now(present)` + `present_now` + `last_paint`、层号归一）；`ui/step0/python_source.py`（`_sync`、`@pyqtSlot`、`step_sync`、`gate_fn` 排序、`gate_pending`）；`ui/step0/engine_mount.py`（`_can_keep`、`_prepare_switch`、`_gate_for`、`_NoPaint` 视口不绘）；`ui/step0/step0_page.py`（相同样式跳过、`step0.param_commit`）；`scripts/a9_drive.py`（真实按键送到数值框——送到内部行编辑会提交两次，曾引发 Busy 模态卡住回放）。15.5 第 3 条的"抑制"按更简单的等价做法实现：引擎为画面时吞掉被 GL 宿主覆盖的视口的 Paint 事件（不碰任何可见性 / 启用状态 / floor 校准），回到 Python 画面时移除。
- 测试：Rust 100 通过；`test_step0_engine_live.py` 23 通过（新增：未命中先算后交接、等待中旧图逐帧不变、粗层替代不发布（深层视图，去掉门即失败）、预算受限 3 s 定时放行、额度 2 块仍在交接前发布、被覆盖的 ImageItem 不绘（去掉过滤即失败））。
- 回放 `u16_deep`（细胞级，含预读期间）/ `u16_mid`（整图 / 中层），"提交 → 新图真实上屏"：细胞级 9 次 32–372 ms（第一次值未变不计），整图 / 中层 16 次 23–116 ms；全部 ≤ 400 ms；每次新图首帧 `exact_fraction=1`（目标层完整）。

### 15.8 代码审核第 1 轮修正（`codex_s15_code_1.out` 否决 5 条）
1. **门槛层 = min(本帧准入层, 屏上层)**（P1-1）：Rust `frame::paint` 在试画时自己取 `min(trial max_level, 本帧准入层)`（`coarse_only` 读作最粗层，`admitted_level` 单测）；宿主无已发布帧时传 `NO_GATE = 255`，即按准入层判断，不再退回任意层覆盖。候选路径同：`view_covered_at(min(准入层, 屏上层))`。请求排序 `_gate_for` 改用 `Step0GlHost.gate_levels(slot)`：同一 min（`slot.admitted` = 该核上一帧准入层）。
2. **超时例外只在准入层覆盖时放行**（P1-2）：Rust 报告增 `trial_admitted`（准入层或更细的覆盖）；`_gate_open` 超时后要求它为真，否则不放行、等下一块到来再判。超时后 `gate_levels` 同时返回准入层，准入层的块也排在前面请求。
3. **发布后立刻补发延期块**（P2-3）：切换 / 候选发布时 `adapter.pump_soon()`（下一轮事件循环 pump，不在 core 调用内）。
4. **计时按所有者**（P2-4）：`_gate_open(..., owner)`，所有者为 `("switch", token)` 或 `("candidate", id(slot))`；所有者变了重新起算；`request_switch` 也清零。
5. **只吞宿主正下方的绘制**（P2-5）：`_NoPaint` 只在 Paint 区域全部落在宿主几何内时吞掉；完全在外的照常；跨越的吞掉并在下一轮事件循环只重绘宿主外的边缘（在绘制内调 `update` 会被清掉）。
- 测试：Rust 101 通过；新增 5 个（例外只在准入层放行、无屏上图不退回任意层、新切换不继承计时、门槛层取更细者 + 超时含准入层、边缘照常重绘、发布后补发请求）。注意：`test_step0_python_source.py` 在导入时设 `QT_QPA_PLATFORM=offscreen`，与 GL 测试放在同一进程会让 GL 测试跑在无屏模式而偶发失败；GL 测试须单独在真实显示上跑。

### 15.9 代码审核第 2 轮修正（`codex_s15_code_2.out` 否决 2 条；1–3 项已核验通过）
1. **每个等待中的画面各有自己的计时**（P2-1）：`_gate_since` 按所有者（`_owner_of`：current 的切换按 token、候选按 slot）分别记；`_gate_prune` 只留仍在等待的（current 的切换、candidate），全空时停定时器；单个定时器取最近到期。`_try_switch` 先查 `still_wanted` 再进门——不再需要的旧切换不碰任何计时；发布时只结束自己的计时（`_gate_done`）。`gate_levels` 读本 slot 自己的计时。
2. **边缘重绘不再画被遮挡的图像**（P2-2）：实测 pyqtgraph 下，无论整块边缘区域还是逐条重绘，`ImageItem.paint` 都会被调用（ViewBox 的裁剪不剔除子项）。改为：只落在宿主外的 Paint 由 `_NoPaint._paint_margin` 自己完成——把 ViewBox 的 `childGroup` 透明度暂设 0（`NoViewportUpdate` 下不引发重绘请求，Qt 跳过全透明项），直接 `graphics.viewportEvent(event)`，再恢复。ViewBox 把子项裁剪在自身形状内，子项本就画不到边缘，所以边缘像素不变；测试用 `QGraphicsView.render`（不经 Paint 事件）逐像素比对边缘（除 ViewBox 边界落在像素之间的 1 像素缝），并用真实 pyqtgraph 视图做完整往返，断言 `ImageItem.paint` 零调用。
- 测试：engine_live + engine_mount 59 通过（两次）；新增"每个画面各自计时"，改写计时单测为真实等待中的 slot。
- 第 3 轮审核 **通过**（`codex_s15_code_3.out`）。采纳其建议：`drop_candidate` / `park_current` 也清理计时（`_gate_prune`）。如实记录：ViewBox 边界落在像素之间时，宿主右 / 下侧那 1 像素缝在边缘重绘时会从"部分图像"变为背景色（宿主外、仅此一列 / 一行），属已知小差异。回放 `u18_deep` 9 次 71–344 ms、`u18_mid` 16 次 19–114 ms，无一次超时放行。

## 16. S0-C 申请（第一版否决 `codex_s16_plan_1.out`，第二版见 16.13，否决 `codex_s16_plan_2.out`，第三版见 16.14，codex 第 3 轮批准 `codex_s16_plan_3.out`；冲突处以最新小节为准；2026-10-29；用户："开始执行内核下一步"）：三图对比由引擎画

### 16.1 现状（三图对比，`ui/step0/compare_strip.py`）
- 三个 `ExploreView` + 三个 `ExploreController`（Original / TopHat / cuCIM），一个后端：自己的 `TileScheduler`，借用全图的原图缓存、校正缓存、整图概览库、floor 缓存（`build_compare_stacks`，`compare_strip.py:152`）。
- 一个镜头：任一面板变化以"中心 + 比例"复制给另两个（`_mirror_from`，`:658`）。
- 换通道两条路（`set_channel`，`:785`）：缓存齐了一个 GUI 回合内三个一起换（`_publish`，`:858`）；否则旧三图不动、横幅写"Preparing X — still showing Y"、只往缓存里要缺的块，齐了再一起换（`_enter_pending` / `_settle_pending`，`:930`、`:1062`）。
- 调参（`set_params`，`:1307`）：只重选被改的方法面板，逐块重算，无"三图一起换"保证。
- 全图 ↔ 三图：`_hand_gpu_to_compare` / `_hand_gpu_to_full_image`（`step0_page.py:2890`、`:2908`）——Python 侧一方 `suspend_for_production`、另一方恢复。全图引擎在全图页隐藏时已经暂停（`step0_explore_tab.py:427`）。
- 指标（SNR / BG-CV）：静止后从 scheduler 的原图 / 校正缓存按面板自己的层取块拼图计算（`step0_page.py:7600`）。
- 正式计算（含调参 Enter 触发的补丁批处理）时三图不暂停，只停 HOT（`step0_page.py:3558`）。

### 16.2 基线（S0-0 遗留项，本段开始前补测）
工具：`scripts/a9_drive.py` 新增 `s0compare` / `s0compare_exit` / `s0cmp_channel` / `s0cmp_param` 与 `a9.s0cmp_done`（三个面板都显示目标选择、当前层可见块齐全的时刻；读面板的块池，池齐 = 下一次绘制上屏）。场景 `a9_bench/s0a/user/make_scen_cmp1.py`（用户项目副本，Slice1 qptiff，CD45RA 起，进入三图后换 6 次通道、TopHat / cuCIM 各 3 次调参），整图 / 中层 / 细胞级三档。结果见 16.9。

### 16.3 与 Odon 的逐项对照（`/sda1/Fusion/analysis_pipline/odon`）
| 项 | Odon | 我们（本段） | 复用 / 理由 |
|---|---|---|---|
| GL 上下文 | 整个程序一个 glow 上下文，每个视图区是一个 egui `PaintCallback`，带自己的矩形（`src/mosaic/mod.rs:5549`，`src/mosaic/tiles_gl.rs:103` `paint(info, …)` 用 `info.viewport`） | Step0 一个 `Step0GlHost`（一个上下文），三图时三个面板矩形 | **复用**：一个上下文、按矩形画多个视图区 |
| 多个图像共用纹理库 | 马赛克所有条目共用一个 `MosaicTilesGl`（`tiles_gl.rs:44`，一个容量） | 每个面板一个 core，各自的块库（显存合计按一个全图查看器预算分配） | **不复用**：我们的 core 一个视图一个画面，三面板同一位置不同来源；改成一个 core 多视图要改 Rust 规划器，超出本段 |
| 原子性 | 立即模式，每帧画当时已驻留的块，不等齐（渐进出现） | 三个面板的新画面全部达标才在同一帧一起换（16.5） | **不复用**：用户对三图的既有要求是"一起换、不出现一张新两张旧"（`compare_strip.py:785` 的测量与裁定） |
| 镜头 | 一个相机 | 三个 ViewBox 互相镜像（现状不变），每个面板的视野各自读自己的 ViewBox | 现状保留 |

### 16.4 容器：一个宿主，两种布局（`ui/step0/gl_host.py`）
1. **面板对象**：把今天宿主里"一个画面"的全部状态与逻辑原样搬进 `Panel`（current / candidate / standby、已发布 FBO、`published_view` / `published_level`、各自的质量门计时、`_paint_current` / `_try_switch` / `_advance_candidate` / `gate_levels`、挂钩 `view_rect` / `still_wanted` / `expected_channels` / `cannot_draw` / `on_published` / `on_switched`）。宿主保留上下文、失败、定时器、呈现、显存统计。宿主对外的旧属性（`host.current`、`host.request_switch` 等）委托给全图面板，所以全图挂载和现有测试不改语义。
2. **两种布局**：全图布局 = 一个面板，宿主盖在全图 ViewBox 上（现状）；三图布局 = 三个面板，宿主盖在三图区（`CompareStrip._panes`）里三个 ViewBox 的外接矩形上，每个面板矩形按各自 ViewBox 的场景矩形换算（含 DPR）。切换布局时宿主**改挂父控件**（同一顶层窗口内改父不重建上下文——已实测：`aboutToBeDestroyed` 0 次、`initializeGL` 1 次、上下文对象不变；万一重建，现有 `_context_going` / `initializeGL` 重挂路径照旧处理）。全图面板的 core 在三图期间保持暂停（现状：全图页隐藏即暂停），回到全图时恢复。
3. **呈现**：每个面板的已发布 FBO 拷到自己的矩形；面板之间的缝隙（各 GraphicsView 的边距、三列间距）用底下控件的背景色清屏（边距取 ExploreView 的背景、列间距取 `_panes` 的调色板），与今天看到的一致。三个视口下的 Python 图像项照 §15.5 3 的做法不绘（每个视口一个 `_NoPaint`，只吞宿主正下方，边缘照常）。
4. **未发布时**：三图第一次进入时三个面板都还没有引擎画面——宿主隐藏，三面板照旧显示 Python 画面，三个候选在屏外准备（现有 `_drive` 路径），三个都达标后一起发布并显示宿主（同 S0-A 的首次接管）。

### 16.5 三图原子发布
- 每次绘制先对每个"有待换内容"（切换或候选）的面板做试画并按 §14.6–15.9 的门判断（不提交）；只有**所有**有待换内容的面板都在这一次绘制中达标，才在同一帧一起提交（交换 FBO、`on_switched` / `on_published`）；否则全部不提交，各面板照旧（视野没动则定格旧图，动了则旧行跟镜头）。没有待换内容的面板正常画。
- 只改了一个方法的参数时只有那个面板有待换内容，它自己达标即换（与今天只重选该面板一致）；换通道时三个都有待换内容，一起换。
- **同一回合登记**：三个面板的选择变化由三图引擎在**同一个 GUI 回合**里一起同步（一个合并的 `singleShot(0)`，不是每个面板各排一个），避免两个回合之间插进一次绘制让先登记的面板单独换上。
- 等待时三图区显示现有横幅"Preparing X — still showing Y"（引擎有待换内容即视为 pending），发布时清掉并发 `publication_changed`（指标在此之后计划，同今天）。
- 某一面板失败：沿用 §10.6——引擎退出（全图与三图都回 Python 画面，本数据集不再尝试），旧画面先由 Python 接手，不出现混合帧。

### 16.6 挂载（`ui/step0/engine_mount.py`、新 `ui/step0/compare_engine.py`）
- `Step0EngineMount` 改为面向一个 `Panel`（宿主由外部给出或自建），其余逻辑不动：来源判定、显示行、增益、Python 来源适配器、切换、门的请求排序、`_NoPaint`。
- `CompareEngine`（新，小文件）：给三个面板各建一个面板挂载，`stack` 用一个轻量对象装该面板的 controller / view / overlay 与三图共享的 provider / scheduler；负责布局切换（`set_running(True)` 时宿主改挂到三图区并切三面板布局）、同回合同步、pending 状态、发布信号、失败。它借用全图挂载的宿主；全图引擎被拒或失败时三图也用 Python 画面。
- `CompareStrip` 接线（只加不改 Python 路径）：引擎在画三图时，`set_channel` 不走 Python 的缓存预检 / pending（引擎自己保证一起换），直接 `_publish`（三个 controller 一个回合内 `set_selection`），然后交给 `CompareEngine.sync_all()`；`pending` / `pending_text` 在引擎画三图时读引擎的等待状态；`suspend` / `resume` 同时暂停 / 恢复引擎。引擎不画三图时一切照旧。
- 显存与内存：三个面板各自 current + candidate，三面板合计按一个全图查看器的 GPU 预算（每个 core 1/6）；每个 core 的 CPU 缓存同理 1/6；三图面板不留 standby（换通道后旧 core 直接关闭），所以三图最多 6 个 core，加上全图暂停的最多 2 个。
- 全图 ↔ 三图：Python 侧现有交接不变；引擎侧由页面可见性驱动——全图页隐藏时全图挂载暂停（现状），三图恢复时 `CompareEngine.set_running(True)`；回到全图时三图引擎暂停、宿主改挂回全图、全图挂载恢复，全图面板保留的已发布画面先呈现，再按当前镜头重画。

### 16.7 调参 Enter 与正式计算
- 今天三图在正式计算期间不暂停，只停 HOT——保持。
- 与 §13–15 相同的问题（批处理线程一启动就抢 GIL，新图被拖慢）：Enter 在三图下触发批处理前，先对三图做同样的有界同步准备（每面板 `step_sync` + 一次 `paint_now(present=False)`，三面板一起达标即一起发布并同步呈现），然后批处理照常开始，三图照常运行。预算与全图相同（守护 1.5 s，目标 0.4 s）。

### 16.8 指标
- 引擎画三图时，Python controller 不再自己取块，原图缓存不一定有 Original 面板当前层的块，校正缓存里的块是引擎所在层的。改为：指标按每个面板**已发布画面的层**取键（参数按该层的有效值，`correction_identity(level)`），缺的块在静止后以缓存专用请求向三图 scheduler 补要（同 `_request_missing` 的做法，不进任何画面），补齐后计算；补齐前显示"—"（同今天）。

### 16.9 基线结果（2026-10-29，Python 画三图，`a9_bench/s0a/user/c0_n{0,6,14}`）
"提交 → 三个面板都齐"（毫秒；通道依次 CD8、CD68、CD45RO、CD45RA、CD8、CD163；调参依次 TopHat 15、25、11，cuCIM 40、60、31）：
| 档 | 换通道 | 调参 |
|---|---|---|
| 整图（层 5） | 729、317、334、309、272、134 | 59、25、26、802、125、127 |
| 中层（层 4） | 595、517、467、284、125、240 | 58、352、131、132、177、59 |
| 细胞级（层 1） | 1008、662、333、482、130、190 | 95、529、374、520、565、553 |
说明：读的是面板块池（池齐后下一次绘制上屏，约差一帧）；第一次换通道含进入三图后的冷读。

### 16.10 验收
- 自动：三图截图序列中任何一帧都不出现新旧混合（换通道、调参、换通道时拖动）；首次进入三图先 Python 后三个一起换成引擎；全图 ↔ 三图来回无黑屏；三图下 Enter 的补丁批处理前新图已上屏；任一面板注入失败三图回 Python 画面；指标数值与 Python 画三图时一致（同一视野）。
- 计时（回放 `make_scen_cmp1.py` 三档，同 16.9）：换通道"三图都换上"每档中位数不慢于基线；调参"被改的面板换上"每次 ≤ 0.4 s（同全图）。
- 用户真机验收。

### 16.11 文件白名单
`ui/step0/gl_host.py`、`ui/step0/engine_mount.py`、`ui/step0/compare_engine.py`（新）、`ui/step0/compare_strip.py`（接线）、`ui/step0/step0_page.py`（三图交接、Enter 前同步准备、指标取层）、`ui/step0/python_source.py`（如需按面板区分 scheduler，仅接口）、`scripts/a9_drive.py`（测量）、测试（`tests/test_step0_compare_engine.py` 新、`tests/test_step0_engine_live.py`、现有三图测试的假对象适配）。不改 Rust、不改 `viewer/` 下的读取 / 调度 / 缓存 / 算子。

### 16.12 风险
- 宿主拆成面板是对已验收全图路径的重构：全图的全部现有测试必须原样通过，回放 `u16_deep` / `u16_mid` 复测不变差。
- 三图最多 6 个 core 同时在用（各自读盘线程）；CPU 缓存按 1/6 分配后命中率下降，以回放实测为准。
- 改父控件在其他平台（WSL）的行为未验证；上下文重建路径有测试覆盖。

### 16.13 第二版（回应 `codex_s16_plan_1.out` 四条 P2 与表中意见）
1. **引擎路径不调用 `_publish()`**（P2-1）：`CompareStrip` 新增引擎路径 `_select_for_engine(channel, params_for, tint)`：只在一个回合内对三个 controller `set_selection` / `set_tint`，记下目标，停 HOT（同 `_enter_pending` 的时序），然后 `CompareEngine.sync_all()`。`_displayed_channel` / `_displayed_params_for` / `_displayed_tint` 的更新、横幅清除、HOT 恢复（`start_hot` / `refresh_hot`）、`publication_changed` 统一由组提交回调 `_engine_committed()` 做。横幅：选择后第一次绘制组仍未提交（引擎真在等）才显示"Preparing X — still showing Y"，提交时清除。`pending` / `pending_text` / `pending_channel` / `displayed_channel` 在引擎路径读这里的目标与已显示身份。只改参数（`set_params`）时同样只重选被改的方法面板，不改已显示通道，不发 `publication_changed`（同今天），页面照旧排指标。Python 路径一字不改。
2. **Enter 先同步最新选择再准备**（P2-2）：`CompareEngine.prepare_now(deadline)`：①当前回合内立刻执行排队的三面板同步（不等 `singleShot(0)`），使新来源已登记；②统一 deadline（守护 1.5 s）的准备循环：每个有待换内容的面板 `step_sync`，再一次 `paint_now(present=False)`（三图布局，组提交在其中）；组已提交、无进展、或超时即退出；③已提交则同步呈现；④返回后才 `worker.start()`。由 `_release_explore_for_production(keep_picture=True)` 在三图模式下先调用（全图的 release 照旧）。验收包含"在数值框里输入新值、未失焦、直接回车"的真实按键路径（`a9_drive` 的 `s0cmp_param` 已是真实按键）。
3. **所有权与边界**（P2-3）：
   - 宿主**唯一拥有者是全图挂载**（建于全图 `start`，毁于全图 `close`）。`CompareEngine` 只借用；宿主提供 `on_closing` 回调列表，全图挂载销毁宿主前先通知借用者，借用者关闭自己面板的 core 与适配器。正常换数据集的顺序（先拆三图、后拆全图，`step0_page.py:10777`）下借用者已先关闭。
   - **一切暂停 / 释放 / 关闭只作用于本面板**：`Panel.paused` 取代挂载对 `host.hidden` 的直接写；宿主的 `hidden` = 当前布局的所有面板都暂停。挂载的 `release_for_production` / `_retire_python` / `release_to_coarse` / `close` 只遍历自己面板的 core（全图挂载今天遍历的 `host.cores()` 改为 `panel.cores()`）。`CompareEngine.close()` 在 `CompareStrip.teardown` 最开始（`stacks.teardown()` 关 scheduler 之前）执行，适配器先于其 scheduler 关闭。
   - 离开 Step0：三图页 / 全图页各自的隐藏事件暂停各自面板（全图今天已如此；三图由 `CompareStrip` 的 `suspend` 与 hide 驱动）。宿主级失败（任一面板）通知所有挂载回 Python 画面（失败回调列表），本数据集不再尝试。
   - `attach` 拆成首次 `attach`（初始化前）与布局切换 `use_full(view)` / `use_compare(panes, views)`（改父、断开旧 ViewBox 信号、接新信号、重算几何），后者可在初始化后调用。
   - `_NoPaint`：每个被覆盖的视口一个，覆盖矩形按"宿主几何映射到该视口坐标"计算（不再直接用宿主在父控件里的 `geometry()`）。
4. **测量**（P2-4）：`_S0CompareDone` 分两种口径，按目标选择在动作时判定：引擎画三图时**只**认宿主 `frameSwapped`、且三个面板的已发布画面都等于本次目标（每面板 `shows_wanted`），记录在那次呈现；Python 画三图时为"三个面板块池都齐之后、三个视口各自完成的下一次绘制"（绘制事件之后的回合记时）。起点：调参用 `step0.param_commit`（Enter 处理入口），换通道用行点击动作。基线按此口径重测，替换 16.9。
5. **表中意见**：
   - 显存：三图一个查看器额度不是 Step0 总额度。进入三图时全图面板的 core 释放到粗层（`release_to_coarse`，保留 CPU 缓存与粗层），所以三图期间 Step0 显存 ≈ 一个查看器额度 + 全图粗层 + 各 FBO（`gpu_bytes` 已计入 FBO）；回到全图的耗时在回放中测并报告。`gpu_memory` 的回收按宿主 `hidden`（= 当前布局全部暂停）。
   - 指标：按面板已发布帧的**准入层**取键是近似（准入层不保证每个像素的实际采样层）；验收固定同一视野、同一层、同一来源身份，与 Python 画三图时的指标比；细化期间指标仍只在静止后计算。
   - HOT：引擎路径等待期间停 HOT（同今天 pending），提交后恢复；回放中记录 HOT 活跃时的换通道耗时。
   - 验收补充：窗口缩放 / DPR、DAPI 开关与范围、Marker = DAPI 时去重、三面板 resize 后矩形正确。
   - Odon 对照更正：Odon 每个瓦片要求所有可用通道齐全才画（`tiles_gl.rs:140`），并非完全不等齐；但没有多视图整组发布。

### 16.14 第三版（回应 `codex_s16_plan_2.out`：首轮四条已关闭，新增一条 P2 与一条建议）
1. **调参发布后重排指标**（P2）：引擎路径下只改参数的面板组提交时，`_engine_committed(kind="params")` 调页面现有的 `_schedule_compare_metrics()`（同一个定时器，不发 `publication_changed`），使"等待期间指标计划因 pending 被丢弃"之后在新图上屏时再算一次。验收：无补丁的项目，三图下改参数回车，新图异步准备超过指标静止时间（150 ms），新图上屏后指标显示数值而非"—"。
2. **测量建议**：`_S0CompareDone` 增 `panels` 参数——调参只等被改方法的面板（另两个面板不重绘），换通道等三个。基线调参部分按此重测。

### 16.15 基线重测（新口径，替换 16.9 的数字；`a9_bench/s0a/user/c2_n{0,6,14}`）
口径：换通道 = 三个视口各自"开始时块池已齐"的那次绘制结束；调参 = 只等被改方法的面板。毫秒：
| 档 | 换通道（CD8、CD68、CD45RO、CD45RA、CD8、CD163） | 调参（TopHat 15、25、11；cuCIM 40、60、31） |
|---|---|---|
| 整图（层 5） | 782、264、391、121、102、92 | 45、29、25、910、156、152 |
| 中层（层 4） | 805、460、495、279、122、313 | 55、446、159、232、204、179 |
| 细胞级（层 1） | 920、838、798、432、132、354 | 83、499、515、496、639、563 |

### 16.16 实施记录（2026-10-29，进行中）与一处偏离
- **宿主拆面板**（`ui/step0/gl_host.py`）：`Panel` 承载 current / candidate / standby、已发布 FBO、`published_view` / `published_level`、门计时（`gate_since`）、`paused`、`keep_standby`、`rect` 与挂钩；`trial_switch` / `trial_candidate` 只试画并返回"提交"闭包，`paint_old` 画旧图（或定格）。宿主 `_paint_panels`：先画所有没有切换的当前画面、试画所有切换，再试画候选；多面板时"所有等待的面板都达标"才提交（否则全不提交），提交前先补画未提交切换的旧图，任何一步失败即返回（失败的绘制里不发布任何东西，保留 `test_a_failing_current_core_never_publishes_a_ready_candidate` 的保证）。旧的宿主属性经 `_forward` 转给全图面板。`hidden` = 当前布局全部面板暂停（全图挂载写 `host.hidden` 即写全图面板）。`use_full` / `use_compare` 改父与重算几何（三面板矩形按各 ViewBox，DPR 换算）；面板间缝隙在 `_present` 里用剪刀测试 + `glClear` 按各自背景色清（最初用 QPainter，在 `show()` 引起的同步绘制里偶发段错误，改为直接 GL；本机 PyQt5 无 `versionFunctions` 模块，四个 GL 1.x 函数取自系统 libGL，`glBindFramebuffer` 取自上下文）。宿主新增 `failure_hooks` / `smooth_hooks` / `closing_hooks` / `commit_hooks`。
- **挂载**（`engine_mount.py`）：一切 core 操作经 `self.panel`；`start_on(host, panel)` 借用宿主；`budget_share`（全图 2、三图 6）按份额分 GPU 与 CPU 缓存；`group` 不为空时显示变化交给三图引擎同回合同步；拥有者 `close` 先调 `closing_hooks` 再销毁宿主，借用者只清自己的面板；`_NoPaint` 把宿主映射到各视口坐标。
- **三图引擎**（新 `compare_engine.py`）与 `CompareStrip` / 页面接线按 16.6、16.13、16.14 实现；指标按 16.8（`_CompareMetricFill`）。
- **偏离（需 codex 与用户知情）——换通道不再新建 core**：首次三图回放（`a9_bench/s0a/user/e1_n6`）换通道 600–1280 ms，比基线慢。时间线：每次换通道三个面板各新建一个 core（各约 20–30 ms），每个新 core 预热显存块约 50–65 ms（串行，第二次扩容再各约 50 ms），旧 core 在发布的那次绘制里关闭各约 18 ms。改为：原图与实时方法的 core 不按通道区分，一个数据集一个"原图 core"（`RAW_CORE`），换通道与 Original ↔ 实时一样，是同一个 core 上的切换（准备中行 + 试画 + 质量门 + 三面板一起提交）；已保存结果仍是自己的 core。全图同样受益（换通道不再建新 core）。测试中按通道断言 core 身份的地方改为断言"同一个 core、画面通道正确"（`test_another_channel_...`、`test_..._never_three`、`test_the_nucleus_is_added_...`）。

### 16.17 代码审核第 1 轮修正（`codex_s16_code_1.out` 否决 5 条 P2 + 测量缺口；RAW_CORE 技术上可接受，待用户确认）
1. A→B→A（B 未发布）：选择后与每个呈现帧检查"目标已是屏上画面且无等待"即完成（`_engine_target_shown`）。
2. 构建阶段失败：挂载 `fail()` 之后调用宿主 `_fail`，所有挂载（两种模式）回 Python。
3. 宿主 `_panels` 登记所有面板（布局外的也算）：显存统计、回收、上下文销毁 / 重建都覆盖暂停的三图面板；借用者关闭时 `forget_panel`。
4. 引擎画三图时，任一面板有待换画面（含调参）`pending` 即为真，指标不在新画面发布前计算；发布后 `params_published` 重排。
5. `prepare_now` 覆盖候选 core（保存结果 → 新半径）：`paint_now(candidates=True)`，完成条件"无候选、身份为目标、切换完成"。
- 测量：`_S0CompareDone` 记下本次动作的目标（通道，或面板与参数值），引擎呈现信号只在目标匹配时结束计时。
- 新增测试 5 个（共 13 个三图测试）。

### 16.18 代码审核第 2 轮修正（`codex_s16_code_2.out`：首轮五项已关闭，新增一条 P2 与探针建议）
- 调参改回屏上的值（等待被取消、没有发布）时也重排指标：引擎画三图时 `set_params` 记 `_engine_edit_open`，之后任一呈现帧（或选择后的下一回合）发现"无等待且三面板都是目标"即发 `params_published`。测试 `test_an_edit_taken_back_is_measured_again`。
- 探针：调参探针同时绑定动作时的通道；Python 口径的完成判定也用本次目标（通道与被改面板的参数值）。

### 16.19 User acceptance fixes (2026-10-29)

1. **Zoom walked out on every compare channel switch.** The pending banner sat in the strip's layout; showing/hiding it resized the aspect-locked ViewBoxes (519 <-> 542 px), each refit wider (8 switches: 0.60 -> 0.25 screen px per level-0 px). Its row is now always reserved (one fixed line). Test `test_channel_switches_keep_the_magnification`.
2. **Compare drags stuttered** (20-step drag into new tissue 0.60-0.86 s vs 0.34 nominal). Causes (py-spy + an experiment with the patch preload off: 0.35-0.39 s): the preload readers re-opened the qptiff and parsed every page header in Python per read (GIL), `_Gl` rebuilt every frame (`find_library` subprocess; cache keyed on a fresh wrapper's id), saved-product disk checks per paint. Fixes: per-thread open TiffFile/zarr in `OMETIFFLoader._thread_zarr`, `_libgl()` once + `host._gl`, `_saved` cached on `saved_correction.version()` for 1 s. After: preload read 23 ms avg (was 130-340), compare drags 0.34-0.40 s, full 0.33-0.43 s. Codex fix review 1: APPROVED (advisory: idle preload workers may keep the old loader's handles after a dataset change -- bounded).

### 16.20 User acceptance round 2 (2026-10-29)

1. **VISTA -> compare: no drag for 20 s.** A compare mount stopped its Python stack (requests off, scene repaint held) when ITS candidate was published, but the host shows only when all three have a picture: an early panel froze. Now `take_picture()` runs for all three in the turn the host is shown (`CompareEngine._show_if_ready`), at once for later publications. Test `test_a_panel_published_early_keeps_its_python_picture_live` (fails before, passes after). Codex fix review 2: APPROVED.
2. **"The picture does not stick to the mouse."** Measured with real XTest drags: in-app injection -> frame swapped (a9_xwheel_report) full p50 11 / p95 18 ms, compare 14 / 25-30 ms; on screen (root capture at 120 Hz, phase correlation, scratch `laganal.py`): engine full p50 ~29 ms, compare ~34 ms; old Python picture 43 / 58 ms; Odon (same method, Kevin TMA4 OME-TIFF) ~50-55 ms. The engine moves exactly as far as the view (in-app test: 96 px -> 96 px). Full screen did not change the on-screen lag. The rest is the desktop compositor + 60 Hz scan-out. Bench: a9_bench/s0a/user/make_scen_xdrag.py (+ _fs), lagcap.sh / laganal.py in the session scratchpad.

### 16.21 User acceptance round 3 (2026-10-30): drag in compare, entry time

1. **"Drag does nothing" in compare** -- root cause: swap interval 0 (vsync off). The engine drew unthrottled and starved the GUI thread of input. Vsync is now on by default (`BLOCK01_VSYNC=0` restores the old behaviour); the user confirmed the fix. The three mounts share one manifest.
2. **Entry wait unacceptable** (target <= 0.4 s). The compare strip is now prepared ahead, hidden: about 600 ms after the full image goes quiet, the three engine cores are built and painted off screen (`GlHost.paint_ahead`), then paused. Entry only resumes them and shows them once all three are ready (`EngineMount.ahead_ready`; the P2-3 gate never shows a stale picture). A channel or parameter change in the full image prepares it again. A move stops the off-screen paints. The size is the real panel size.
3. **Remaining entry stall.** The owed floor jobs (`_floor_pending`) were started without looking in the shared floor+gain cache that the full image's HOT already fills (`_prepare_centre_floors`). Fixed at three sites in `ExploreController`: restore from the cache first, then let a waiting background request run. Codex r3: one P2 (stranded background request) and one P3 (preparing badge), both fixed. No new idle-time computation was added.
4. **Codex r2 fixes:** a hidden channel choice now supersedes a pending engine target (stale label). The blocking production hand-off waits until done or the stacks are gone, with no 10 s timeout. The ahead paints stop before preparing again.
5. **File reader** (`raw_tile_provider`, user-approved): short-lived threads with no handle of their own read through one shared handle, instead of re-parsing the qptiff (~110 ms each, holding the GIL).
6. **Double right-click** counts as one gesture (`RightClickViewBox`), user-approved: its second half used to close compare again at once.
7. **Measurement trap:** the bench `_present` glReadPixels probe inflated compare GUI blocks. It now runs only with `DIAG_FB`.

**Results, real machine** (`a9_bench/s0a/user/en25-27`):

| | Before | After |
|---|---|---|
| First entry: picture shown | 0.26-0.34 s | 61-84 ms |
| First entry: longest GUI block | 0.35-0.40 s | 111-135 ms |
| First entry: first camera move | -- | ~0.1 s |
| Re-entries: picture shown | -- | ~50 ms |

User acceptance passed 2026-10-30.

**Open:** Step1/2 drag re-measure with vsync on; split `ensure_built`.

### 16.22 Follow-ups after acceptance (2026-10-30)

1. **Vsync vs Step1/2 drags** (`a9_bench/s0a/user/make_scen_xdrag12.py`, runs `xs12_*`, 3 runs per mode). Injection to on-screen frame, p50: Step1 ~57-60 ms and Step2 ~68-72 ms, the same with vsync on and off. Step1 has occasional stalls of 180-280 ms in both modes, so they are not caused by vsync (backlog).
2. **Splitting the hidden preparation** (Fable 5b). The 1.17 s GUI block had three parts: the qptiff open (~480 ms), the widgets, and the engine. Changes:
   - The slide is opened on a worker thread (`CompareStrip.open_source_ahead`). The result is handed over through an app-long `_OpenRelay` holding a weakref to the strip, because the strip can be deleted while the thread runs (codex r4 P1).
   - The build and the engine preparation run in separate turns, 50 ms apart. The quiet wait still applies between the steps (`_compare_ahead_between`, codex r4 P2).
   - Codex r4_2 found it ready to commit.
   - Result: two blocks, about 0.25-0.6 s and about 0.25 s. The remaining time is GIL contention from the new strip's first requests. Entry is unchanged: about 0.09 s the first time, about 0.05 s after.
3. **Not done** (user, 2026-10-30): the ~4.8 s GUI freeze at slide load. About 2.2 s of it is the full image's engine `_source_setup`.
