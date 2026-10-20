# F5：TMA 核心显示与可放大的小看图器（计划 v4：codex 三轮意见已处理；第 3 轮的第 1、2、4 条已改入，第 3 条待用户定，见问题 E）

用户 2026-10-19 真机验收后的三点意见与裁定：
1. Step2 第一块之后停了约 75 秒。日志 `/sda1/Fusion/tma_accept_slice1/rois/full_wsi_20261017_182042_845e/runs/segment_20261019_135551_stardist_nuclei_expansion/segmentation_20261019_135551.log` 显示：13:56:06 第 1 块结束，13:57:21 第 2 块才开始。用户同意去掉"清缓存"。
2. 只要 Regions（Step 0.5）保存过核心，就在 Tissue preview、Step1 看图器、Step2 预览、Step3 看图器上画出核心轮廓。Step1/2/3 各有一个显示开关。
3. Tissue preview 和 Step2 预览改成能放大看清的小看图器，复用主看图器。

裁定（2026-10-19，固定，本计划不再重开）：
- **顺序**：①去掉清缓存 → ②核心轮廓到处显示 + 开关 → ③Step2 小看图器 → ④Tissue preview 小看图器。每一部分先计划、经 codex 审核，再由用户启动实施。本文件覆盖四部分，**审核通过不等于实施授权**。
- **各步显示哪一份核心**：Step1 显示上次保存的核心；Step2 显示交接给它的冻结快照（运行前）或这次运行的快照；Step3 显示这次运行的 `regions_snapshot.json`。
- **编号**：
  - 关联了 TMA 表格的核心，显示表格里的名字（Annotation Name）；
  - 没关联的不显示任何文字；
  - 缩小时不显示，放大到合适程度才显示；
  - 在 Tissue preview 里可以被边框截断。
- **配色沿用 Step 0.5**：会跑的核心是绿色实线，不跑的是黄色实线。不引入灰色或虚线。
- **小看图器的画面**：跟 Step1 的通道和颜色设置走。Step2 的只显示 DAPI，用原始切片（或按 Step0 决定校正后）自带的多层 DAPI，上面叠分块网格和核心轮廓。
- **Tissue preview 的旧规则作废**：弹窗原本的 v14 规则"不是第二个看图器"（`ui/widgets/tissue_navigator_popup.py:6-9`），由本裁定改变。

---

## 第一部分：去掉 Step2 的"清缓存"

### 1.0 现状（代码事实）

`workers/segment_merge_worker.py:1266-1276` `_drop_caches()` 做两件事：
- `os.system('sync')`，即整机落盘；
- 向 `/proc/sys/vm/drop_caches` 写 `3`，清空整机文件缓存。只有 root 能写，非 root 时静默失败。

共 **20 处调用**（`grep -n "_drop_caches()"`，去掉定义那一行），分三类：

| 类别 | 行号 |
|---|---|
| 每块之后：区域循环与共用的跳过块 | `2042`（`_skip_tile`，两个循环共用）、`3151`、`3179`、`3231`、`3282` |
| 每块之后：整片（引擎进程）循环 | `3657`、`4057`、`4086`、`4102`、`4194` |
| 释放模型之后 | `4212` |
| 结束与导出 | `2336`、`3310`、`3341`、`3351`、`3361`、`4243`、`4272`、`4280`、`4344` |

很多处前面紧跟一次 `gc.collect()`。

**75 秒的可能原因（待实测确认）**：
- `sync` 要等**整机**所有未落盘的数据写完。
- 第 1 块之前约 3 分钟刚写完约 4 GB 的 fused.zarr（`runs/fuse_20261019_135148`，13:52）；同一时段另一个进程还在写约 1 GB 试验数据。
- 日志只证实了这段间隔，没有给 `sync` 本身计时。第一部分的验收会测出这段间隔是否消失。

即使不卡，`drop_caches=3` 也会让下一块的 fused.zarr 重新从硬盘读，同一台机器上其他程序的缓存也被清掉。

### 1.1 改法
- 删除 `_drop_caches` 的定义和全部 20 处调用，包括结束与导出处：那里的 `sync` 同样是整机等待。软件其他产物，比如 Step1 生成 fused.zarr，从不调用 `sync`。
- 其余一律不动：`gc.collect()`、`_empty_torch_cache_if_available()`、`del` 都保留。
- 分割、拼接、写盘逻辑都不改。验收比较的是科学结果（标签图、全局掩膜、细胞表），日志和元数据里的时间戳自然不同。

### 1.2 对内存的影响（用户问）
- **进程自己的内存不变**：文件缓存不属于进程，内核在内存紧张时会自动回收它。
- **软件里的读数**：
  - `[MEM] RAM x/y GB`（`segment_merge_worker.py:1327-1330`）按 `total - available` 算。`available` 把可回收的文件缓存算作可用，但其中脏页（还没写盘的）要先写盘才能回收，所以读数可能略有波动。
  - `resource_samples.csv`（`utils/runtime_resource_monitor.py:199`）用 `psutil.used`，不含缓存。
  - `[Step2Profile] rss=`（`utils/step2_profiler.py:170`，进程 RSS）可能偏高。Step2 用内存映射文件写全局掩膜（例如 `:2904-2906`），访问过的映射页计入 RSS，以前每块之后会被清掉。这些是文件页，可以回收。
- 以上都是推断，以第一部分的真机测量为准。

### 1.3 测试
- **新测试**：用 `os.system` 和 `/proc/sys/vm/drop_caches` 的打开动作作为观测点，断言下面每种情况都一次也没有发生：
  - 区域循环：跳过块、空块、有细胞的块、结束导出；
  - 整片循环：同样四种情况。
  
  复用 `tests/test_f4_step2.py`，以及 `test_step2_engine_unified` / `test_step2_runner_path` 里现有的小规模运行夹具。
- **回归**：`test_f4_step2`、`test_step2_engine_unified`、`test_step2_runner_path`、`test_step2_legacy_stop`、`test_step2_ownership_move`、`test_step2_tile_status`。
- **真机**：同一项目再跑一次 Step2，记录下面几项，与 75 秒那次对比，并核对科学结果一致：
  - 第 1→2 块的间隔，以及每块的间隔；
  - 总时间；
  - `[MEM]` 峰值；
  - `rss` 峰值。

### 1.4 白名单
`workers/segment_merge_worker.py`（只删 `_drop_caches` 的定义与调用），以及新测试文件。

---

## 第二部分：核心轮廓到处显示 + 开关

### 2.0 现状（代码事实）

**数据**
- 核心是多边形，点为 level-0 像素坐标 `(x, y)`。
- 保存结果 `<project>/tma/regions.json` 由 `core/regions/lock.py:313` `load_saved` 读。其中 `document.cores` 的每颗核心由若干 `pieces` 组成，`document.positions` 记录 `run` 和 `qc`。
- TMA 表格的 `positions[*].annotation` 来自 "Annotation Name" 列（`core/tma/map_sheet.py:65,107,330`）。
- 冻结快照（`core/regions/analysis.py:158` `build_snapshot`）只带**会跑的**核心轮廓（`cores[*].polygon`，含 `annotation`）。不跑的只在 `omitted` / `positions` 里留编号和原因，**没有轮廓**。
- 快照里每颗核心是**一个**多边形（`geometry.region_outline`，`core/regions/geometry.py:145-171`）。由多块组成的核心，会用 `keyhole` 的往返"桥"把它们连成一个环，所以不适合直接描边，否则会画出假连线。
- 没有表格也能有核心：在 Cores 页手画一个形状，就会生成一颗未关联的核心（`ui/regions/shapes.py:37-45`），保存后在 `document.cores` 里。

**会不会跑**
- 规则只有一处：`analysis.select_cores`（`:78-117`）要求勾了 Run、只绑定一颗核心、`mapping == "matched"`（"review" 不行）、QC 未排除、轮廓有面积；
- 再加 `build_snapshot` 里"与工作区区域至少有一个像素相交"（`core_meets_parent`，`:186-197`）。
- Step1 已经这样调用：`ui/main_window.py:6054` `RA.build_snapshot(pd, sid, [], parent=parent)`（`_random_patch_cores`）。

**Step2 实际用哪份 Regions**
- worker **只**从交接合同里取 Regions（`workers/segment_merge_worker.py:779`）。
- 手动参数运行时，合同会被去掉（`ui/step2_page.py:2972`）；会话恢复也可能回到手动模式（`:1292`）。这时运行不用任何 Regions，整块区域都分割。

**配色与画法**
- 配色在 `ui/regions/cores_panel.py:24-25`：`matched` 是 `#43a047`（绿），`unmatched` 是 `#fdd835`（黄）。
- `RegionsOverlay`（`ui/regions/overlay.py`）是挂在看图器 viewport 上的透明控件，经 ViewBox 映射坐标：
  - `set_items([{id, polygon, color, label}])`；
  - `controller=None` 时不消费鼠标（`:172-174`）；
  - 屏幕尺寸小于 `LABEL_MIN_PX = 14`（`:31`，判断在 `:297`）的形状不画标签。
- 挂载模板：`ui/regions/mount.py:58` `attach_overlay()`，`:81` `follow()`。
- Regions 页以外，目前没有任何地方画核心。

### 2.1 数据：纯函数 `core/regions/display.py`（无 Qt、无缓存）

**`saved_items(doc, study, run_uids)`**：给出 Step1 用的显示项。
- 画 `doc["cores"]` 里每一颗核心，不管有没有关联表格位置。
- **按块画**：每块用它自己的多边形描边，同一颗核心的块颜色相同，标签只挂在第一块上（同 `cores_panel.draw_parts`，`:121-140`），不画假连线。
- 没有归属的碎片、被丢弃的碎片不画，它们不是核心。
- 颜色：`uid ∈ run_uids` 用绿，其余用黄。颜色常量移到 `core/regions/display.py`，`cores_panel.COLOR` 改为引用它，两边共用一份。
- 标签：核心的 `position_key` 对应表格位置、且该位置 `annotation` 非空，才显示 annotation；否则不显示。

**`snapshot_items(snap)`**：只有快照时用（§9.1 修订）。
- 快照 `cores` 画绿色，`display_cores` 画黄色（各块原始轮廓）；缺 `display_cores` 的旧快照只画绿色。
- 快照多边形里由 `keyhole`（`core/regions/geometry.py:168`）加的往返桥线，在显示时拆掉：新函数 `split_keyhole(poly)` 删除同一条边先去后回成对出现的线段，还原成各块各自的轮廓环，只用于显示，不改快照、不改身份。

**`run_uids` 的来源**：

| 位置 | 来源 |
|---|---|
| Step1 | 上次保存 + `build_snapshot(pd, sid, [], study, parent=当前工作区 ROI)` 的 `cores[*].uid`，与 `_random_patch_cores` 同一调用。被拒绝时（一个都没选、核心重叠……）为空，全部画黄色。非 TMA（roi 模式）快照没有核心，所有保存的核心画黄色：它们不作为核心参与分析。 |
| Step2 | 按**实际运行的输入**定：参数来源是交接（index）且合同带 `regions` 时，用合同快照的 `cores[*].uid`；手动参数或合同不带 Regions 时，这次运行不用 Regions（见问题 B）。参数来源、配置、所选 fused 变化时重算。 |
| Step2 运行中/后、Step3 | 运行目录里的 `regions_snapshot.json`（`load_snapshot` 校验身份）。 |

**黄色核心的轮廓从哪来**（快照里没有）：
- Step1：就是保存结果本身。
- Step2、Step3：读当前保存结果，**仅当**它的 `content_sha256` 等于快照里的 `regions.content_sha256`，才用 `saved_items`（`run_uids` 取快照的）。
- 不等时（Regions 之后又保存过），只用 `snapshot_items` 画绿色，绝不拿新保存的轮廓顶替旧运行的。
- 历史运行完整显示的方案见问题 A。

### 2.2 画：复用 `RegionsOverlay`，只读
- 新增构造参数 `label_min_px`，默认 14，Regions 页不变。核心显示时建议用 60：核心在屏幕上至少 60 像素才出编号，真机再调。
- `controller=None`，平移、缩放、Tissue preview 的 ROI/patch 工具都照旧。
- 1.5 像素实线；标签超出 viewport 的部分自然被裁掉。

### 2.3 挂在哪里（坐标要点）

**Step1、Step3 看图器**（`ui/main_window.py:1943` `_step1_whole_slide`、`:1965` `_step3_whole_slide`）
- ViewBox 坐标就是 level-0，直接传多边形。
- 照 `attach_overlay` 的写法：每次 open、sync、进入该步之后，以及 GL 层替换、尺寸变化之后，都重新挂上并抬到最上层。

**Step2 旧预览**（第二部分期间，`ui/step2_page.py:284-292`）
- 概览坐标是**减去 fused 输入原点后再除以 `_ov_ds`**：`ox = (x - x0) / ds`，`oy = (y - y0) / ds`。
- 原点取自**所选 fused 输入自己的** `bbox_fullres`（`core/tile_tissue.py:31` `fused_region`），不取当前窗口的 ROI。

**Tissue preview**（`ui/step0/overview_panel.py:1193`）
- 概览坐标是 level-0 ÷ `OVERVIEW_DOWNSAMPLE`（32，`:1248`, `:1863-1876`），也就是整片坐标，不减原点。
- Tissue preview 没有自己的开关，跟当前步骤走：
  - Step1/2/3：按该步的规则和开关；
  - Step0 与 Regions 页：按 Step1 的规则（上次保存的核心）和 Step1 的开关。Regions 页上，Tissue preview 显示的是上次保存，而 Regions 自己的看图器显示的是正在编辑的草稿，两者可能不同，这是预期行为。

### 2.4 开关
- **三个独立开关**：Step1、Step2、Step3 各一个可勾选按钮 "TMA cores"，样式同 `Smooth`（`MODE_BUTTON_QSS`），默认开。
  - Step1：工具行（`ui/main_window.py:1170-1186`）；
  - Step2：Tile Status 页工具行（`ui/step2_page.py:1156-1159`）；
  - Step3：工具行（`ui/step3_page.py:124`）。
- **会话**：三个开关各自写进会话，仿照 `Smooth` 的存（`main_window.py:4015`）和恢复（`:4226-4229`）；旧会话没有这些字段时，默认开。
- **隐藏**：没有可画的核心时（没保存过，或保存里没有核心），按钮隐藏。
- **提示**：只用按钮的 tooltip 说明当前显示的是哪一份，以及只有绿色的原因。不新增界面上的提示文字。

### 2.5 局限
- Step3 显示旧运行时，如果 Regions 之后又保存过，黄色核心不显示（绿色核心照常，桥线已拆）。是否要完整保留，见问题 A。
- Step1 每次刷新都会调用 `build_snapshot`。它要做几何和栅格化（`core/regions/raster.py:94`），耗时未测。
  - 第二部分先在主线程调用，并在真机记录耗时；
  - 超过 100 ms，就向用户报告，**不**擅自加线程或缓存。
  - 刷新时机：进入 Step1、Regions 保存之后、切换 ROI 之后。不做缓存，所以不存在"换 ROI 后颜色不对"的问题。

### 2.6 测试
- **`display.py`**，用合成文档加表格：
  - 绿色和黄色；
  - 有 annotation、无 annotation、未关联核心；
  - 多块核心按块画，标签只出现一次；
  - `split_keyhole`：拆回的环与进入 `keyhole` 之前的那些轮廓（`_union_ring` 栅格合并后提取的，`core/regions/geometry.py:155-160`；允许起点和方向不同）一致，不留桥线；不要求还原合并前的各块；单块多边形原样返回；
  - 碎片和丢弃的碎片不画；
  - `build_snapshot` 拒绝时全黄；roi 模式全黄；
  - Save A → 交接 A → Save B 之后，Step2/Step3 对 A 的新快照画 A 的绿色和黄色（`display_cores`，§9.1），绝不混入 B；只有缺少 `display_cores` 的旧快照才只画绿色；
  - 同一份保存下切换两个 ROI，颜色随 ROI 变化。
- **overlay**：
  - `label_min_px` 生效；
  - `controller=None` 时，真实 Qt 鼠标事件（单击、拖动、滚轮、双击、空格平移）都传给下面的视图；
  - GL 层替换、尺寸变化之后仍可见。
- **Step2 坐标**：原点 x、y 不为零且不相等，`ds` 不等于 32。
- **Step2 来源切换**：交接 → 手动 → 交接；会话恢复成手动模式。
- **界面**：三个开关互不影响；会话保存和恢复；没有核心时按钮隐藏。

### 2.7 白名单
- 新文件 `core/regions/display.py`。
- `ui/regions/cores_panel.py`：颜色常量改为引用。
- `ui/regions/overlay.py`：只加 `label_min_px`。
- `ui/main_window.py`：Step1/Step3 挂载、开关、会话字段、Tissue preview 挂载。
- `ui/step2_page.py`：开关、旧预览上的 overlay、来源判断。
- `ui/step3_page.py`：开关。
- `ui/step0/overview_panel.py`：只加挂 overlay 的入口。
- 对应的测试文件。

---

## 第三部分：Step2 预览换成可放大的小看图器

### 3.0 现状
- **预览图**：
  - 内容是 fused.zarr 的 nucleus 平面，按固定步长降采样，长边不超过 4096（`core/nucleus_overview.py:22`）；
  - Step2 在后台线程读（`ui/step2_page.py:1691-1745`），画在一张 `pg.ImageItem` 上。
- **分块网格**：
  - `_draw_tile_grid`（`:1847-1873`），每块一个 `pg.RectROI`；
  - 点击块走 `:1876-1879` → `_on_tile_clicked(r, c)`；
  - 块的状态颜色靠改各自的画笔。
- **当初为什么没做**：10 月 1 日用户选了"长边 ≤ 4096"（`docs/v16_Step2_tiles_application.md:122-124, 213-217, 229-230`）。"在 Step2 里再做一个看图器"因为范围大没有采用。现在主看图器已经能多实例（Step1、Step3、Regions 三份常驻）。

### 3.1 改法

**新 mount**
- 第四个 `Step1WholeSlideMount(window, parent=..., camera_reason="step2", labels=False)`，用 `install(layout, self._ov_gv)` 装进去。现有的 pyqtgraph 预览成为回退控件。

**只显示 DAPI：三条后端都要管住**
- 新增构造参数 `display_filter`。它作用在 mount 的两处规格入口：`_gpu_composes`（`ui/step1_viewer_mount.py:866-872`）和 `_gpu_display_snapshot`（`:901-928`）。Rust 后端（`ui/step1_core_binding.py:414`）和 Python GPU 后端（`ui/step1_viewer_mount.py:565`）都经过这两处。
- 效果：Overlay 模式，只有 nucleus 通道，权重 1，颜色和窗口用 Step1 里 DAPI 的。
- 初始通道：`open(channel=nucleus)`。`open()` 的 channel 参数在 `:279`；默认会取 Step1 当前选中的通道（`ui/step1_viewer_binding.py:125`），所以必须显式给出 nucleus。
- **不允许 CPU 回退**：CPU 合成读的是普通 Step1 规格（`ui/step1_compose_binding.py:67`）。mount 进入 CPU 后端的三处——首次打开（`ui/step1_viewer_mount.py:298`）、Rust 回退（`:427`）、换源重绑（`:1237`）——在设了 `display_filter` 时一律改为：先 `pause_requests()`，再 `restore_legacy()` 回到现在的 nucleus 预览，**在任何未过滤的读或合成开始之前**。换源重绑时的 `viewer.open()`（`:1233`）也传 nucleus 通道。没设 `display_filter` 的现有三个 mount 行为不变。
- **回退要持久**：一旦回到旧预览，就关闭这次失败的看图器（`close()`，不只是暂停），之后离开再回到 Step2、或者暂停期间源变了，都不会走 `resume_requests`（`:1148`）或 `source_changed`（`:1192`）重新打开 CPU 读取。只在下一次显式重开时（重新选定输入、数据集更换）才再试 GPU。不新增状态机，只用现有的 `close()` 与 open。
- **只在输入属于当前工作区时启用**：Step2 可以用 Browse 选另一个 fused（`ui/step2_page.py:1314`），而 mount 读的是主窗口当前的切片、ROI 和校正设置（`ui/step1_viewer_binding.py:66`）。所以每次选定输入时，用 `fused_region`（`core/tile_tissue.py:31`）取它的切片路径和 `bbox_fullres`，与主窗口当前的切片路径和工作区区域比对；还要比对该输入冻结的 ROI 多边形（fuse 运行参数里保存的，`main_window.py:10566`）与当前工作区多边形（mount 裁切用它，`ui/step1_viewer_mount.py:1041`），以及该输入记录的 nucleus 通道（`fused_region` 返回值，`core/tile_tissue.py:58`）与当前的 nucleus 通道。全部一致才用小看图器；不一致或取不到，就用旧预览（网格坐标照 2.3 用该输入的原点）。不改 mount 的取源逻辑。
- **每次打开、换源、恢复前都要再比一次**（第 4 轮 P1）：同一个比对函数不只在选定输入时用，还在 mount 的每次 `open`、`sync_source`（包括 `"handoff"` 换源）和 `resume_requests` 之前用。原因：Step2 运行中切到同一切片的另一个工作区 B 再回来，Step2 有意保留 A 的输入（`ui/step2_page.py:218`、`main_window.py:4887`），但照搬的生命周期会 `sync_source("handoff")` 把 mount 换到 B。比对不一致时关闭 mount、显示所选输入的旧预览。不新增注册表、缓存或状态机。
- **模式按钮不影响它**：Overlay/Fusion 按钮的传播（`main_window.py:7759` 一带）不包括这个 mount，它恒为 DAPI。
- **画面差异**：小看图器显示的是原始（或校正后）DAPI 加 Step1 的 DAPI 窗口，和模型实际看到的 fused nucleus 平面亮度不完全一样。这是裁定内的已知差异，不另加界面说明。

**不抢导航框和相机**
- `camera_sink=None`。
- 新增构造参数 `navigator=False`，让 `publish_view_rect`（`:1353`）直接返回。
- Tissue preview 的点击跳转，在 Step2 时是否驱动这个小看图器，见问题 C。

**分块网格**
- 改用 overlay 画：level-0 坐标 = 块坐标 + fused 输入原点（同 2.3）。块状态颜色沿用现有颜色。
- 给 overlay 一个**完整**的只读 controller，接口与 `RegionsOverlay` 的调用一致（`overlay.py:152-226`）：

  | 方法 | 行为 |
  |---|---|
  | `press(y, x, ctrl)` | 返回 False，不拦截拖动 |
  | `click(y, x, ctrl)` | 找到所在块 → `_on_tile_clicked(r, c)` |
  | `wheel(...)` | 返回 False，滚轮照常缩放 |
  | `hover`、`drag`、`released` | 空操作 |
  | `double_click` | 返回 False |
  | `cursor_at` | 返回 None |
- 运行中网格变化（行列数改了）、强制跳过的确认、换工作区时旧运行仍在跑，这些情况都沿用现有逻辑，overlay 只负责画。

**生命周期**
- 照 `RegionsViewerHost.follow`（`ui/regions/mount.py:81`）：只在 Step2 页可见时活动，离开就 `pause_requests()`。
- 数据集更换时**关闭再重开**（同 `RegionsViewerHost.close`，`ui/regions/mount.py:163`）：`sync_source` 比较的源表不含原始数据路径（`ui/step1_viewer_binding.py:162`、`viewer/step1_source.py:273`），只用于同一数据集内的变化。窗口关闭时关闭，与 `main_window.py:7635` 一带的 Step1 关闭并列。
- 运行期间用户仍可平移、缩放。如果真机测出明显拖慢分割，再向用户提"运行中暂停"，不预先做。

### 3.2 显存
- **预算**：
  - 单层上限：原始纹理 1536 MB，每通道粗层 32 MB、细层 48 MB（`core/resource_tiers.py:76-78`）；
  - 全部看图器合计的目标是 5 GB（EDGE 档，`:39`）。这不是整机硬上限，超出时只让隐藏的看图器退回粗层（`ui/gpu_memory.py:73-87`）。
- **小预算**：新增构造参数，同时传给两条 GPU 后端：
  - Rust：`step1_core_binding.build(self, stack, demo_budgets())`，`:378`；
  - Python GPU 层：`:516`；
  - Python GPU 绑定：`:562`。
  
  建议原始纹理上限 256 MB，并在日志里报告实际生效的值。
- **实测**：Step2 页可见、分割运行中、Tissue preview 同时打开时，记录 `gpu_memory.allocated_bytes()` 和整机显存。EDGE 机器和服务器各测一次。

### 3.3 测试与验收
- **offscreen**：
  - 两条 GPU 后端都只请求 DAPI；
  - GPU 失败时回到旧预览，网格、点击、块颜色都照旧；
  - `navigator=False` 时，Tissue preview 的视野框不被 Step2 移动；
  - controller 处理真实 Qt 事件：点击块、拖动平移、滚轮、双击、空格平移；
  - 暂停、恢复、关闭、数据集重置，不留下未完成的读取；
  - Rust 回退到 Python GPU 时，小预算仍然生效。
- **输入与工作区**：用 Browse 选另一个 ROI 或另一张切片的 fused 时，回到旧预览，网格仍按该输入的原点画；选回当前工作区的输入时，小看图器恢复。
- **三处 CPU 入口**：首次打开、Rust 回退、换源重绑，都在任何未过滤的读之前回到旧预览；换源重绑后仍只显示 DAPI。
- **数据集更换**：Step2 页可见时和隐藏时各测一次，有未完成的读取时同样要关闭再重开。
- **同切片、同外框、不同多边形**的历史输入：回到旧预览。
- **运行中换工作区**：在工作区 A 开始 Step2 → 运行中打开同一切片的工作区 B → 回到 Step2：不出现 B 的 DAPI；mount 关闭，显示 A 输入的旧预览，网格和运行叠加仍是 A 的。
- **回退持久**：GPU 失败回到旧预览 → 离开 Step2 → 回来，不出现 CPU 读取；暂停期间换源，同样不出现。
- **复用现有测试**：`test_step2_tile_status`、跳过计划、导航恢复、相机归属。
- **真机**（Slice1）：
  - 放大到单颗核心能看清细胞核；
  - 网格、块颜色、点击块和现在一致；
  - 运行中平移流畅；
  - 分割总时间与第一部分验收的结果相比不变慢（±5%）；
  - 显存按 3.2 记录。

### 3.4 白名单
- `ui/step1_viewer_mount.py`：加 `display_filter`、`navigator`、预算三个构造参数及其传递；设了 `display_filter` 时三处 CPU 入口（`:298`、`:427`、`:1237`）改为暂停并回到旧预览，重绑时传 nucleus 通道。
- `ui/step1_core_binding.py`：预算参数的传递。
- `ui/step2_page.py`。
- `ui/main_window.py`：Step2 mount 的生命周期与关闭。
- 对应的测试文件。

---

## 第四部分：Tissue preview 换成可放大的小看图器

### 4.0 现状
- **画面来源**：`TissueNavigatorPopup`（`ui/widgets/tissue_navigator_popup.py:40,61,102`）里放着一个 `OverviewPanel`。画面由 `TissuePreviewCoordinator`（`ui/block01_display.py:1091`，步骤上下文在 `:72-75`）按步骤合成后装进去（`:1678`）。
- **为什么糊**：底图是 1/32 坐标系上的一张静态缩略图（长边 512–1023 像素，`core/io_loader.py:138-149`）；面板背景不透明，`ImageItem` 由 `_apply_thumbnail()` 管理（`overview_panel.py:1380-1392`, `:1880`）。
- **面板上的工具**：`OverviewPanel` 的 ROI 和 patch 编辑、视野框（`:3043`）、点击跳转、中键平移（`:3177-3823`），全部是 1/32 坐标系里的 pyqtgraph 图元。

### 4.1 方案：先做可行性小试（约 1 天），通过后再单独申请实现
- **推荐 A：工具不动，只换底图。**
  - 小看图器（第五个 mount：`camera_reason="tissue"`、`navigator=False`、`camera_sink=None`、小预算）放在面板视图的**下面**；
  - 面板视图背景透明，隐藏它的 `ImageItem`；
  - 小看图器显示期间，暂停 coordinator 往面板装缩略图；
  - 两个 ViewBox 的范围同步，比例为 1:32。

  小试必须回答下面几个问题：
  - 透明的 QGraphicsView 叠在 QOpenGLWidget 上能否正确合成，结果要截图核对真实像素；
  - 窗口尺寸变化、DPI 变化之后，像素是否对齐；
  - 滚轮和拖动事件由谁接收；
  - 回退时缩略图能否恢复。
- **B：把面板图元整体搬到小看图器的 ViewBox 上**（坐标 ×32）。改动约 2600 行，不推荐。
- **颜色**：跟 Step1 走（裁定），Step0 时也一样。共享显示状态在 Step0 就已存在：直接从 Step0 进 Regions 时，Regions 看图器已经按 Step1 的显示状态出图（`ui/regions/mount.py:1-13`）。Step1 还没调过设置时，用这个共享状态的初始值。只有小看图器打不开时才回退到缩略图。
- **生命周期**：
  - 弹窗隐藏、最小化、关闭时 `pause_requests()`，重新打开时 `resume_requests()`；
  - 数据集更换时关闭再重开（同 3.1，不用 `sync_source`）；
  - 窗口关闭时关闭；
  - 跟 Step1 的模式按钮走（加入 `:7759` 的传播）。
- **显存**：Tissue preview 和当前步骤的主看图器同时可见，`gpu_memory` 只能回收隐藏的那一个，所以必须用小预算，并实测合计显存。

### 4.2 小试的白名单与回退
- 在**单独的 git worktree** 里做，不进主工作区，不提交。小试结束后，向用户汇报截图和结论；回退就是删掉这个 worktree。
- 允许改动的文件：
  - `ui/widgets/tissue_navigator_popup.py`：放小看图器、叠放、生命周期；
  - `ui/step0/overview_panel.py`：只加"背景透明、隐藏 `ImageItem`、恢复"三个入口；
  - `ui/block01_display.py`：只让**弹窗那个面板**在小看图器显示期间不装缩略图。coordinator 同时给 Step0 页的面板和弹窗出图（`:1180`、`:1678`），Step0 页的面板照旧；
  - `ui/main_window.py`：第五个 mount 的创建、暂停、恢复、关闭，以及加入模式按钮的传播；
  - 小试用的测试文件。
  - 问题 E 方案 (1)（用户已授权）：弹窗的来源上下文，只在 `ui/widgets/tissue_navigator_popup.py` 与 `ui/main_window.py` 的弹窗接线处；交接之前用 Step0 已加载的数据集，不改变 Step1 的就绪状态。
- 不改 mount、Rust 核心、读取、调度、缓存。

### 4.3 小试的验收
- 放大到单颗核心清晰。
- ROI 画/改、patch 画/改、视野框、点击跳转、中键平移，与现在一致（逐项手测清单）。
- 事件不串到另一个视图。
- 真实像素对齐。
- 回退正常。
- 来源上下文：Step0 首次加载、尚未保存时弹窗清晰可放大；弹窗打开时更换数据集，旧数据集的读取全部停止并换成新的。
- 现有测试全部通过：`rg -l 'OverviewPanel|TissueNavigatorPopup|TissuePreviewCoordinator' tests`。

---

## 5. 需要用户确认的问题

**A｜旧运行在 Step3 的完整显示**
- 现在的方案：Regions 一旦又保存过，旧运行在 Step3 只显示绿色核心。
- 如果想让旧运行永远完整显示，需要在 **Step1 冻结快照的同一时刻**，另存一份只用于显示的全部核心轮廓（不影响分析、不进快照身份）。这份数据要随交接合同带到 Step2 的运行目录，会改动 Step1 运行和交接合同的文件范围。
- 建议：先不做，接受这个局限。

**B｜Step2 用手动参数运行时**
- 这时运行不用 Regions，整块区域都分割。建议：保存过的核心全部画黄色，tooltip 说明"本次运行不按核心筛选"。

**C｜在 Step2 时点击 Tissue preview**
- 建议：跳转 Step2 的小看图器，与 Step1、Step3 一致。

**E｜Step0 保存之前的 Tissue preview**
- 小看图器读的是主窗口的数据集（`ui/step1_viewer_binding.py:66`），而 Step0 在交接（保存）之前用的是它自己加载的数据集，此时主窗口还没有（数据集更换时会清空，`main_window.py:3155`），Tissue preview 却在 Step0 加载后就自动打开（`ui/step0/step0_page.py:6270`）。
- 两个选择：
  - (1) 给弹窗加一个"来源上下文"，让它在交接之前也能用 Step0 已加载的数据集，不影响 Step1 的就绪状态。这是新机制，需要你授权，并列入第四部分小试的白名单和验收（首次加载未保存时、弹窗打开时更换数据集）。
  - (2) Step0 保存之前保留现在的缩略图，保存之后才换成小看图器。
- 建议 (1)，在小试里一起验证。

**D｜编号出现的放大程度**
- 建议：核心在屏幕上至少 60 像素时出现，真机再调。

## 6. AGENTS.md：新增机制清单（需授权）
- `RegionsOverlay` 加构造参数 `label_min_px`；默认值不变。
- `Step1WholeSlideMount` 加三个构造参数：`display_filter`、`navigator`、预算；默认值保持现有行为。
- 新模块 `core/regions/display.py`：纯函数，没有缓存，也没有注册表。
- 三个会话字段：三步的开关状态。
- 第四部分里两个视图的叠放与同步：小试之后单独申请。
- （问题 E 选 (1) 时）弹窗的来源上下文。
- 不新增调度器、缓存、注册表、线程；不改看图器的读取、调度、缓存逻辑。显示改动不改任何分析结果；§9.2 手动参数按核心筛选是用户批准的科学行为变化。

## 7. 实施顺序与每步验收
（§9 归入：`display_cores` 与 Step2/Step3 按快照画绿黄并入第二部分；§9.2 手动参数按核心筛选作为**第二部分之后的独立一块（2b）**实施与验收，白名单见 §9.3。）
1. **第一部分**（约 0.5 天）：codex 审代码 → 用户真机验收（间隔、总时间、内存读数、科学结果一致）。
2. **第二部分**（约 2–2.5 天）：codex 审 → 真机截图核对，并记录 Step1 刷新耗时。
3. **第三部分**（约 2–2.5 天）：codex 审 → 真机验收清晰度、网格点击、运行中流畅、显存。
4. **第四部分**：先做 1 天可行性小试 → 汇报 → 再定是否实现（估计 2–3 天）。

## 8. 风险
- **Step2 启动时冻结快照的主线程耗时**（§9.2）：真机记录；潜在耗时不构成加线程或缓存的理由。
- **显存**：最多 5 个 mount，Tissue 与当前步骤同时可见。靠小预算加实测控制。
- **读盘**：多一个看图器读原始切片，在服务器 HDD 上与分割同时读，可能互相拖慢。第三部分实测。
- **重画开销**：overlay 的缓存图在视图变化时重画，约 150 颗核心、每颗几百个点。Tissue preview 拖动时要实测；Regions 页在同样数量下已经在用。
- **落盘时机**：去掉 `sync` 后，结束时的落盘由内核按常规节奏完成，这与软件其他步骤一致。

## 9. 用户第二轮裁定（2026-10-19）与待商定方案

### 9.1 Step3 呈现"跑分割时的样子"（已裁定）
- Step3 只显示跑过分割的结果，绿色（当时跑了的核心）和黄色（当时没跑的核心）都按**运行当时**的状态画，与之后 Regions 怎么改无关。
- 做法（替换 §5 问题 A 的建议）：快照冻结时（Step1 的 `_preseg_regions` → `RA.build_snapshot`，`core/regions/analysis.py:158`），把"已绑定、有面积但没跑"的核心轮廓作为**仅显示用**字段一并存入快照（例如 `display_cores: [{position_key, polygon(s), annotation, reason}]`），**不进 `identity()`**（`analysis.py:246`），不影响任何分析结果。经交接（`core/preseg_contract.py:90-100`，整份快照随 contract 走）和 Step2 的 `regions_snapshot.json` 自然带到 Step3。
- 本改动之前的旧运行：快照里没有该字段 → Step3 只画绿色，不拿现在的 Save 去补。
- 2.1 的 `core/regions/display.py` 改为：Step3 / Step2（有快照时）只从快照取绿 + 黄；只有 Step1 / Tissue preview（无运行时）才从最近一次 Save 取。

### 9.2 Step2 手动参数也按核心筛选（用户 2026-10-19 选"做法二"并同意以下全部规则；codex 第 1 轮商定，`codex_f5_manual_1.out`）

现状：Step2 只有在使用 Step1 预分割交接（contract）时才带 Regions（`workers/segment_merge_worker.py:779` `_regions_begin` 只读 `self._contract["regions"]`）；手动参数运行时 `_contract is None`，整张切片分割。

**选择规则**（`core/regions/analysis.py`）

| Regions 的 Run 勾选 | 点开始后 |
|---|---|
| 有勾选，且至少一个合格 | 只跑勾选中的合格核心，不弹框 |
| 有勾选，但全部不合格 | 拒绝，列出原因；不改成跑全部 |
| 一个都没勾（指 `select_cores()` 遍历的有效表格占位全部 `not_ticked`） | 弹框"没有任何核心被选择，将默认跑所有核心，是否继续？"，附一句"仍不包括编号待复核、QC 排除等不合格的核心" |
| 弹框"是" | 跑全部合格核心（`select_cores` 只跳过 `not_ticked` 一条，其余检查全保留：绑定、mapping=matched、QC 未排除、有面积、与父区域至少一个像素相交、核心不重叠） |
| 弹框"否"/关闭 | 取消，不创建运行目录、不写任何文件 |
| 全部合格集合为空 | 拒绝，不回退到整片 |

- "是"只对本次运行有效，不回写 Run 勾选、不自动 Save。
- `build_snapshot` 加 `select="ticked"|"all_eligible"`（默认 `ticked`，Step1 不变）。选择方式写入快照字段 `selection_mode` 和溯源，**不进 `identity()`**：实际核心集合已在身份里，等效集合身份相同，旧快照/合同身份不变。
- 零勾选时，从**同一份已校验 Save** 构造候选，弹框之后提交这份候选，不读第二次。

**冻结位置与数据流**
- Step2 页在参数与输入预检查之后、`_begin_run_progress()` 与 worker 构造之前冻结（`ui/step2_page.py:2922`；worker 构造会建输出目录，`segment_merge_worker.py:154`）。
- `parent` = **所选 fused 运行冻结的区域**（`_use_fuse_item`，`ui/step2_page.py:1248` 取的 `rois`），核对 bbox、尺寸、多边形；不用当前窗口的 ROI。
- `seg_channels` = 按最终方法、用**所选 fused 运行冻结的 `fusion_settings.fusion_config`** 计算；缺失时 TMA 路径拒绝，不用当前窗口设置或空列表。
- 快照经新的 worker 构造参数 `regions_request` 传入（worker 自持副本）；**不放进** `_seg_config` 草稿（会话恢复会带进下次运行），也不伪造 preseg contract。
- worker 统一得到 `{snapshot, identity, input_kind, source: "contract"|"step2_manual", selection_mode}`；合同路径用合同快照，手动路径用本次快照，不合并。
- `_regions_begin`：输入类型用 `self.method` 查 `INPUT_KIND`，未知方法拒绝（不让 `None == None` 通过）；有合同时仍检查合同 method 与实际 method 一致；身份校验、切片、单区域、52 个测量排除区上限、`_regions_for` 的 bbox/多边形匹配全部保留。
- 启动后再改 Regions：不影响本次运行，不加"必须最新"门禁；下次运行读新 Save。`RA.is_current`（目前无产品调用）若保留，按快照的 `selection_mode` 重建，缺字段视为 `ticked`。
- 溯源：worker 运行元数据 `tile_strategy.regions` 记录快照位置、来源、选择方式、输入 fusion hash；`register_segmentation_run()`（`core/provenance.py:463`）从这份实际记录生成 `parameters.regions`，依赖仍按冻结 Save 的 `content_sha256` 查找，查不到沿用 unresolved。

**用户同意的启动规则（行为变化）**
1. TMA 项目缺少可用 Regions Save（没保存、损坏、对不上）：手动参数拒绝运行，提示先在 Regions 保存。非 TMA 项目行为不变（整片）。
2. TMA 项目使用旧 HQ / HQ2 / CSD 方法：开始前拒绝并说明（这些路径没有对象级核心筛选，`_seam_begin` 对它们返回 None，`segment_merge_worker.py:706`）。八种引擎方法都支持；标准方法的 `.npy recovery` 单独测试。
3. 旧参数文件或旧合同不带 Regions：TMA 项目不直接整片跑，提示改用手动参数按本节规则运行。

**绿色的定义**：本次冻结的参与核心集合，不由"最后有没有细胞"反推；零细胞核心仍是绿色。不引入逐核心执行账本。

### 9.3 对前文的替换
- §9.1 字段定为 `display_cores: [{uid, position_key, polygons(各块原始轮廓), annotation, reason}]`，在 `build_snapshot` 内用构造分析快照的**同一份 Save** 生成；范围为**所有未参与的核心**（含未关联表格位置的，与 §2.1 一致）；不进 `identity()`、不进 `AnalysisRaster`。合同路径的"运行当时"指该运行使用的冻结快照（可能早于 Step2 启动）。
- §2.1"黄色核心的轮廓从哪来"中 Step2/Step3 读当前 Save 并比对 `content_sha256` 的做法**作废**：Step2（有快照时）/Step3 只从快照画绿 + 黄；旧快照只画绿。
- §2.5 第一条、§5 问题 A、B 作废：A 由 §9.1 取代；B 由 §9.2 取代（手动参数也按核心筛选，不再"全部画黄"）。§5 C（联动）用户同意；D（60 像素）用户同意；E 用户授权方案 (1)。
- 白名单增加：`core/regions/analysis.py`（`display_cores`、`select`、`selection_mode`、`is_current` 按选择方式）、`workers/segment_merge_worker.py`（`regions_request`、统一检查、元数据）、`core/provenance.py`（手动来源与依赖）、`ui/step2_page.py`（手动启动预检查、零勾选弹框、传参）；`core/preseg_contract.py` 只加传递测试，不改。
- 新增机制（用户已授权）：快照显示字段、选择模式、worker 的 `regions_request` 接口、运行元数据/溯源扩展、上述三条启动规则。
- 测试：选择与确认全组合（部分勾选、零勾选 是/否、勾选全不合格、合格为空、review、QC 排除、区域外、重叠）；输入绑定（两个不同 fusion 设置的输入、同 bbox 不同多边形、非零原点 ROI、会话恢复为手动、合同→手动→合同）；完整过滤链路走**真实启动与快照校验入口**（不替换 `_regions_begin`），覆盖整片与 ROI 两条循环、recovery、tile skip；冻结历史（Save A → 启动 → Save B，运行与 Step3 仍为 A）；兼容与溯源（旧合同身份不变、显示字段不改身份、等效选择身份相同）。

### 9.4 实施记录与偏差（2026-10-19）
- `display_cores` 实际存**所有**核心（每项带 `run` 真/假），不只是没跑的：画绿和画黄用同一份冻结列表，不再从 `cores` 拆桥线（旧快照仍走 `split_keyhole`）。不进 `identity()`。
- 白名单外的小改动：新文件 `ui/regions/core_outlines.py`（只读叠加层的挂载小助手，codex 同意）；`ui/regions/page.py` 加 `saved` 信号、`ui/regions/versions.py` 保存成功后发出（Tissue preview 跟着重画）。
- 实测（Slice1 真实项目，热）：`build_snapshot` 约 230 ms，其中一半是重新解析 TMA 表格 xlsx（`map_metadata`）；进入 Step1 时看图器和 Tissue preview 各算一次，主线程约 0.46 s。按 §2.5 报告用户，未加缓存或线程。

## 10. Step1 核心轮廓不卡顿（用户 2026-10-19 提议并同意）

### 10.0 实测（Slice1 真实项目，热）
- 每次算 Step1 的核心（`_saved_core_items`）：`_regions_context()` 里为取切片信息打开 `OmeTiffSource` 约 **223 ms**；`build_snapshot` 约 **130–270 ms**（一半是重读 TMA 表格 xlsx）；读 `regions.json`（1.8 MB）约 46 ms。
- 进入 Step1 时看图器和 Tissue preview 各算一次，合计约 0.5–0.9 s，全在主线程。

### 10.1 改法
1. **取编号不再打开切片**：Step1 的核心计算只需要 `project_dir` 和 `slide_id`。新函数 `_regions_ids()` 用 `run_store.project_dir_of`（已有）和 `provenance.ensure_raw_slide(project_dir, slide_path)`（实测 1 ms）给出这两项，不调用 `_regions_context()`。`_saved_core_items` 改用它。
2. **一份结果、一个键**：Step1 的核心显示项按"键"存一份（主窗口上一个字段，只存最近一份）：
   - 键 = （`regions.json`、`tma_cores.parquet`、TMA study 文件各自的 `mtime_ns` 与大小，切片路径，当前工作区区域的 bbox 与多边形）。全是 `os.stat` 和内存读取，不读文件内容。
   - 进入 Step1、Tissue preview 刷新时先算键：相同就直接用存的显示项；不同就当场按现在的方法算一次并存下。看图器和 Tissue preview 因此每次只算一次。
3. **保存时后台先算**：Regions 保存成功（`RegionsPage.saved`）后，起一个后台线程（Python `threading`，一次一个；新的保存来了，旧线程的结果作废）按当前工作区算显示项，算完经 Qt 信号回到主线程，键仍有效才存下。之后进入 Step1 直接用，不卡。
   - 后台线程只调用纯函数（`build_snapshot` / `saved_items`），不碰 Qt 对象、不写任何文件。
   - 计算失败只在终端打印一行，进入 Step1 时照常同步计算。
4. **作废**：键里的任一项变了（再次保存、换 ROI、换数据集、表格变了）就不会命中，自动重算；数据集更换时清空。
5. **不变**：Step2 / Step3 仍只从冻结快照画，不用这份存的结果；分析结果、快照内容、Regions 保存都不变。

### 10.1b 第 1 轮审核补充（`codex_f5_s10_plan_1.out`）
- **保存的接线**：`RegionsPage.saved` 不再同步刷新 Tissue preview；改为：主线程先取 `_regions_ids()`（`ensure_raw_slide` 可能写 provenance、未命中时会开切片，所以只在主线程调用），固定键和区域几何的副本，再起后台线程只算 `build_snapshot`/`saved_items`；结果经信号回主线程，**键仍等于当前键**才存下，然后刷新当前可见的 Step1 看图器和 Tissue preview 轮廓。
- **竞争**：每次提交带一个递增序号；回到主线程时序号不是最新、或键已变就丢弃。A 还在算时保存 B：A 的结果被丢，B 照常提交并最终存下。主线程从不等待线程（不 `join`）。
- **键**：规范化的项目路径、`regions.json`/`tma_cores.parquet`/TMA study 文件各自的（存在与否，`mtime_ns`，大小），切片路径与解析出的 `slide_id`，当前区域的 bbox 与多边形。
- **"不卡顿"的范围**：只保证预计算完成且键命中时；第一次打开、刚换 ROI、预计算未完成时仍会当场算一次（但只算一次，且不再打开切片）。真机验收看保存和进入 Step1 的实际响应与轮廓更新。
- §6 的"不新增缓存、线程"对本节为用户批准的例外。

### 10.2 新增机制（用户已同意）
- 主窗口上的"最近一份 Step1 核心显示项"存储（只存一份，键失配即重算，不跨会话）。
- Regions 保存后的一个后台计算线程（一次一个，结果按键核对后才用）。

### 10.3 测试
- 键：保存、换 ROI、表格变化都使键变化；未变时命中，`build_snapshot` 不被调用（计数）。
- 进入 Step1：看图器和 Tissue preview 共用一次计算。
- 后台：保存后线程算好 → 进入 Step1 不再计算；保存 A 的后台还没算完又保存 B → A 的结果被丢弃；计算抛错 → 回到同步计算。
- `_regions_ids` 与 `_regions_context` 给出相同的 `project_dir`、`slide_id`，且不打开切片。
- 真机：进入 Step1 的停顿（终端 `[TMA cores] step1 ... ms` 行）。

### 10.4 白名单
`ui/main_window.py`、对应测试文件。

## 11. 用户第三轮裁定（2026-10-19 晚）
- **Tissue preview 不画核心**（轮廓和名字都不画，太鲜艳、名字无意义）：相关代码（`_refresh_tissue_core_outlines` 及其所有接线、Step2 的 `cores_changed`）已删除。
- **核心名字**：Step1/2/3 用 Regions 页面的叫法 "X1-Y06 <donor block>"（`display.core_name`），不再用表格的 Annotation Name；快照显示按快照自己的 `positions` 取名，旧快照也适用。
- **index 参数与 Regions 解耦**：不管参数从哪来，Step2 一律在开始时按最近一次 Regions 保存冻结核心（9.2 的流程，包括零勾选弹框）；Step1 交接里冻结的 `regions` 在开始时去掉（`Step2Page._without_handover_regions`），交接的其余部分照旧。§9.2 的启动规则 3（"旧参数文件不带 Regions 就拒绝"）作废，`_is_tma_project` 删除。运行来源记为 `step2`。

## 12. 中键拖动失灵与切换卡顿（用户 2026-10-19 晚授权：Mythos 5.1 medium + codex astra high 一起解决）

### 12.1 中键拖动（Mythos 分析：`scratchpad/mythos/drag_answer.md`）
- **原因 1（已离线复现）**：第二次中键按下落在双击间隔（400 ms、5 px）内时，Qt 只发 `MouseButtonDblClick`、吞掉 `MouseButtonPress`；面板只在 Press 时开手势（`ui/step0/overview_panel.py:3965-3967`），之后的移动全被丢给已关闭鼠标的 ViewBox。改：中键 DblClick 当作 Press；日志加 `dblclick` 行；测试用 QWindow 版 `QTest.mousePress` 走真实双击合成。
- **原因 4**：约 20% 手势在首次按下 6 ms 后又来一次按下，`_middle_pan_press` 取消重开、锚点错位。改：手势已开且 `event.buttons()` 仍含中键时，记 `dup-press`、接受并忽略。
- **用户补充（原因 1 解释不了）**：刚启动、或 Step0 切到 Step1 之后，一开始中键**完全没反应**。候选：(a) 主线程忙（启动、Step0 读整片缩略图 2–4 s、首次进入各步 1–3 s、每次切换写会话 0.5 s）时按下排队；(b) 弹窗不是活动窗口时的首次按下触发窗口激活，`WindowDeactivate` 取消（`overview_panel.py:3883-3886`）在激活过程中把刚开的手势取消（Mythos 原因 3）。请审核者重点判断 (b) 在 Step 切换（主窗口取得焦点、弹窗失活）后的首次按下是否必然发生，以及去掉 `WindowDeactivate` 取消（保留 Hide/Close，释放由 X grab 和 `_MidPanReleaseWatch` 兜底）是否安全。
- 真机确认：`BLOCK01_MIDPAN_DEBUG=1 BLOCK01_MIDPAN_LOG=<file>`。

### 12.2 切换卡顿（全软件离线实测，项目副本，`scratchpad/perfharness/run3/`）
来回切换每次 0.67–1.31 s（同步阻塞），主要：
1. **`_rm_save_view`（`main_window.py:7798`）约 0.53 s/次**：每次切换把 247 KB 的 `session.json` 用 `json.dump(indent=2)`+`fsync` 写 4 遍（`step0_page.rm_save_draft`、`_save_step1_session`、`_rm_save_page_drafts`、`run_store.update_session`）。改：一次切换合成一次写；内容与上次写入相同就不写；不排版（紧凑 JSON）。fsync 保留（会话是用户状态）。
2. **Step2 草稿里存整份核心快照**（`drafts.step2.segmentation_config.preseg_contract.regions.snapshot`，75.6 KB，加 `display_cores` 后会更大）：§11 已决定 Step2 不用交接里的 `regions`，草稿里不再存它（加载参数文件时就去掉，`_without_handover_regions`）。
3. **进 Regions：`_regions_context`（`main_window.py:5416`）每次开 `OmeTiffSource` 0.34 s**：改为用主窗口已有的 loader（通道名、尺寸）和 `_regions_ids`，不再开切片。
4. **进 Step1：`_preseg_regions_line`（`main_window.py:6355`）为比一个校验值读整个 1.8 MB `regions.json` 0.24 s**：按文件 `mtime_ns`+大小记住上次读到的校验值（同 §10 的键法），没变就不读。
5. **`_mount_channels_dock`（`main_window.py:5709`）每次 0.14 s**：宿主没变就不重装。
预计来回切换降到 0.1–0.2 s。首次进入各步（1–3.4 s）和 Step0 加载读整片缩略图（约 4 s）另立一块，不在本次。

### 12.3 白名单
`ui/step0/overview_panel.py`（只改中键手势入口与日志）、`ui/main_window.py`、`ui/step0/step0_page.py`（只改 `rm_save_draft` 的写法）、`utils/run_store.py`（写会话的去重/紧凑）、`ui/step2_page.py`（草稿去掉交接 regions）、对应测试。

### 12.4 实施记录（2026-10-20）
- 真机录制（`docs/midpan_real_x_evidence/2026-10-20/`）：程序忙 1.5 s 期间拖动 → 期间不动，结束后 12 个移动只剩 1 个，一次跳到终点；0.12 s 内重按 → 那次按下没收到。两个原因都证实。
- 中键：DblClick 当 Press（记 `dblclick`）；手势开启 50 ms 内、3 px 内的重复按下记 `dup-press` 并忽略（按时间和位置判断：按下事件本身总带中键，不能用 `buttons()` 区分）。
- 会话：`run_store.save_session` 紧凑 JSON、内容与磁盘相同就不写（只对 session.json）；Step1 草稿除 `saved_at` 外不变时沿用旧 `saved_at`。四个写入者不合并（去重后同一次切换通常只真正写 1 次：`view.current_step` 变化）。
- Step2 草稿：`draft()`/`apply_draft()` 去掉交接 `regions`。
- `_regions_context`：尺寸、像素大小取自项目记录的切片描述（`describe_slide` 带 manifest 缓存，记录缺失或文件变了才开切片）。
- `_preseg_regions_line`：两个校验值按文件（路径、大小、mtime_ns）记住，没变不重读（`_json_field_by_stat`，只用于这行信息）。
- 通道面板：codex 指出同宿主时本来就直接返回，此项删除。
- Step0 加载：`build_default_stack` 改用 `switch_overview_for_current_channel()`（共享后台 overview 读取；送达时 `on_overview_delivered` 安装、起 floor、按 `_current_bbox` 发请求；之前 `_blocked_on_overview` 什么都不画不请求）。
- codex 第 3 轮代码审核两处已改：worker 只在交接自带 `regions` 时才判"两份快照冲突"；`_check_preseg_contract` 的交接 Regions 融合设置绑定删除；Step2 提示文字更新。
