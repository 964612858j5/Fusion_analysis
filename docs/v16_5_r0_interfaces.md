# v16_5 第一轮（骨架和探针）：Rust 内核的接口约定

状态：第一轮（R0）定稿，等用户批准后进入原始图像薄切片（S-Raw）。
依据：方案 `docs/v16_5_rust_viewer_plan.md` v2.1 的 §3、§4、§5.2、§5.3、§6。
本文件写的是"接口长什么样"。这一轮实际实现并测过的部分，在每节里标为"已实现"。

## 1. 分层和文件

| 层 | 位置 | 职责 |
|---|---|---|
| Rust 内核（不认识 Python） | `native/fusion_viewer_core/src/*.rs` | 每个文件只管一件事，见 §8 |
| Rust 绑定层（不含逻辑） | `native/fusion_viewer_core/src/py/*.rs` | 每个文件只包一个内核模块 |
| Python 适配层 | `ui/step1_core_adapter.py` | 接口声明、后端选择、生成原始来源清单；产品代码暂不导入 |
| Qt 控件（S-Raw 起） | `ui/step1_core_layer.py`（新建） | `Step1CoreLayer(QOpenGLWidget)`，只做 Qt 生命周期和把快照转交给内核 |

用户要求：Rust 部分尽量拆成小算子，不写成上万行的大文件。

## 2. Python 适配层的完整接口（`Step1CoreBackend`）

`Step1GpuLayer`（旧路径）已经满足这个接口；Rust 版的 `Step1CoreLayer` 满足同一套，所以挂载、显存登记、Step3 和测量工具不需要知道用的是哪一个。

| 使用方 | 成员 | S-Raw 实现 | 以后实现 |
|---|---|---|---|
| 挂载 | `initialized`、`init_error`、`attach`、`submit`、`is_resident`、`environment_report`、`cache_stats`、`dispose`、`hidden`、`hold_growth`、`upload_budget_ms` | 全部（`hold_growth`、`upload_budget_ms` 只接收，不起作用） | — |
| 挂载 stats | unavailable、last_error、base_overflow_channels、resolution_limited、admitted_level、ideal_level、覆盖情况 | 只做 admitted_level、ideal_level、覆盖情况、last_error | 完整读图、完整绘制阶段补齐 |
| gpu_memory | `gpu_bytes`、`release_to_coarse`、`set_smooth`、`prepare_headroom` | `gpu_bytes`、`set_smooth` | Step3 和生命周期阶段 |
| Step3 | `set_labels`、`label_stats`、`readback_label_ids_for_test` | 不做（调用时报"未实现"） | Step3 和生命周期阶段 |
| Qt | initializeGL、resizeGL、paintGL、上下文销毁回调 | 全部 | — |
| 测量 | 实际上屏时刻、目标层覆盖、各阶段统计（阶段名同 `utils/tile_trace.py`） | 全部 | — |
| 测试 | `readback_rgba_for_test` | 有 | — |

## 3. Python 交给 Rust 的东西（快照，S-Raw 起）

Python 每次只交一个轻量的不可变快照，不参与逐块的读取和调度。

| 调用 | 何时 | 内容 | 是否阻塞 |
|---|---|---|---|
| `ViewerCore(manifests, config, notify_handle)` | 挂载时 | 来源清单（§4）、线程数（≤ 6）、CPU 缓存上限（字节）、通知写端（§6） | 打开文件、核对文件身份 |
| `set_view(world_rect, device_size, dpr, input_hot)` | 镜头每次变动 | 第 0 级世界坐标的视框、设备像素尺寸、缩放比、最近一帧内是否有输入 | 否，只记下最新值 |
| `set_display(channels, mode, smooth)` | 显示设置变动 | 每通道：编号、颜色、下限、上限、gamma、权重；Overlay / Fusion；是否平滑 | 否 |
| `set_sources(manifests)` | 换来源、保存校正产物 | 新清单；修订变化时旧图块全部失效 | 否 |
| `attach_gl(get_proc)` | initializeGL | Qt 的 `getProcAddress` | 是（GUI 线程，一次） |
| `paint(fbo, w, h, budget_ms) -> 帧报告` | paintGL | `defaultFramebufferObject()`、设备像素尺寸、本帧预算 | 是，受预算限制 |
| `release_gl()` / `forget_gl()` | `aboutToBeDestroyed` / 上下文已丢失 | — | 是 / 否 |
| `stats()`、`gpu_bytes()`、`drain_trace()`、`readback_rgba()` | 随时 | — | 否（`readback_rgba` 只用于测试） |
| `close()` | 关闭 | 见 §7 的顺序 | 有上限地等待 |

**Python 锁的规则（第一轮实测得出）**：
- GUI 线程上调用 Rust 的短操作（`set_*`、`paint`）**不释放** Python 锁。
  - 实测：没人抢锁时，释放再拿回只多花约 0.1 微秒；
  - 但只要有 6 个忙碌的 Python 线程，拿回锁的 95% 线是 20 毫秒，最慢一次超过 5 秒。
- 只有长操作，以及 Rust 自己的读图线程，在没有 Python 锁的情况下运行。读图线程从不碰 Python 对象。
- Rust 模式下不启动旧的 Python 读图线程池（方案第 8 项）。

## 4. 来源清单（SourceManifest）

### 4.1 原始金字塔（已实现：`build_raw_manifest` → `RawReader`）

一个 Python 字典，Rust 直接读取：

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | str | 绝对路径；Rust 侧转成 `PathBuf` |
| `size`、`mtime_ns`、`file_number` | int | Python `os.stat` 的 `st_size`、`st_mtime_ns`、`st_ino`（Linux 是 inode，Windows 是 file index）。Rust 打开文件后自己再取一遍，不一致就拒绝读 |
| `source_identity` | dict | 现有 `SourceIdentity` 的字段（`dataset_path`、`dataset_fingerprint`、`stage`），作为缓存键的一部分，不另造身份体系 |
| `dtype` | str | 样本类型 |
| `levels` | list | 每级一项，见下表 |

每一级：

| 字段 | 说明 |
|---|---|
| `height`、`width` | 这一级的像素尺寸 |
| `block_rows`、`block_cols` | 块大小；条带存储时 `block_cols = width`，最后一条可能不足 `block_rows` 行 |
| `stripped` | 是否条带存储（Kevin 的 qptiff 最粗一级就是） |
| `compression` | TIFF 压缩码：1 不压缩、5 LZW、8 / 32946 deflate；zstd（50000）在完整读图阶段加 |
| `predictor` | 1 或 2（水平差分；浮点不支持 2） |
| `sample_bytes`、`kind`、`big_endian` | 每样本字节数、`u`/`i`/`f`、文件字节序；Rust 输出一律是本机字节序 |
| `channels` | 通道数 |
| `offsets`、`counts` | 小端 u64 的字节串，按 `[通道][块行][块列]` 排列；长度 0 的块读成全零 |

块表直接取自 `raw_tile_provider._ChunkPlan`，也就是 Python 直读用的同一份偏移，所以两边读的一定是同一块。只有 `_ChunkPlan` 不处理的条带级别，才从 TIFF 页读条带表。

**读区域的约定**：调用方先把区域截到图像范围内（同 `read_region` 的截取），起点大于终点就是空区域；超出范围是错误，不会崩溃。

**LZW 的解码规则**：解到这一块应有的字节数就停，后面的字节不管，和 tifffile 一致。Kevin 的 qptiff 有 62 块（第 0–2 级）和最粗一级的 2 条，压缩流尾部有坏字节，像素本身完整：
- 现有 Python 直读路径（imagecodecs 不带尺寸解码）会把那 62 块当成读错；
- 最粗一级本来就走 tifffile，不受影响；
- Rust 和 tifffile 一样能读出来，逐字节一致。

### 4.2 完整清单（S-Raw 之后，形状先定）

在 4.1 之外，每次查看还带：

| 字段 | 内容 | 来源 |
|---|---|---|
| `revision` | 交接修订号 + 各通道决定的摘要；任一变化，旧图块全部失效 | `Step1SourceTable.identity_token()` |
| `roi` | 外接框（第 0 级）、多边形（可空） | `Step1SourceTable.roi_bbox()` 和挂载里的多边形 |
| `channels[]` | 每通道：名字、来源（`raw` / `corrected` / `missing`）、缺失原因 | `Step1SourceTable.source_of()`、`MissingProduct` |
| `corrected[]` | 每个校正通道：zarr 路径、`source_identity`、`roi_bbox_fullres`、块形状、压缩（Blosc lz4 + shuffle，f32）、sidecar coarse 平面（stride、`complete`、身份检查结果） | `Step1SourceTable.region()` / `coarse_plane()` |
| `validity` | 有效区域、NaN 规则 | 同上 |

## 5. 新旧后端切换

- 开关：环境变量 `BLOCK01_STEP1_BACKEND`，取值 `python`（默认）或 `rust`；其他值一律按 `python`。不加新的可见界面（按界面规则，需要时另外问用户）。
- 选择流程（S-Raw 起由 Step1 构造边界调用）：
  1. `requested_backend()` 是 `rust`，并且 `core_unavailable_reason()` 为空；
  2. 来源清单能生成，Rust 打开文件、核对身份都成功；
  3. initializeGL 里 `attach_gl` 成功。

  任一步失败，就整体用旧路径，并在终端写明原因。
- Rust 模式下：保留唯一的 Qt 镜头和 `ExploreController`；旧的 Python 图块调度器推迟到真正回退时才建立；控制器自己的图块请求不发出（方案 §5.3，第 8 项已批准，在 S-Raw 实施）。
- 运行中失败：停通知 → 通知读图线程退出（不等正在进行的读）→ 在当前上下文释放 GL → 整体重新挂载旧查看器。鼠标和绘制路径上不做无限等待。

## 6. 重画通知（已实现：`Notifier`）

- **所有权**：Python 用 `socket.socketpair()` 建一对 socket，两端都归 Python。Rust 只借用写端的句柄：Linux 上是 fd，Windows 上是 64 位 SOCKET，整数原样传，不截断。Rust 从不关闭它，只把写端设为非阻塞（写端只给 Rust 用）。
- **合并**：只有"没有待处理"变成"有待处理"时才写一个字节。实测 4 个线程共发 8000 次通知，只写了约 15–45 个字节。
- **GUI 一侧的固定顺序**：收到可读 → 读到"会阻塞"为止（读空）→ `ack()` → 再取工作。
  - `ack` 之后来的通知会再写一个字节，所以不会丢；
  - 读空和 `ack` 之间来的通知，它的工作在 `ack` 之后取，也不会漏；
  - 最坏情况是多醒一次。
  - 反过来"先 ack 再读空"会丢通知：codex 审核发现，`ack` 后写的字节被读空吞掉，下一个通知看到"有待处理"就不再写。已有专门的测试覆盖这个顺序。
  - 测试：2 万件工作全部被取走，GUI 从不轮询。
- **不阻塞**：socket 缓冲区满，说明里面已经有一个没读的字节，直接跳过。
- **含义**：只表示"请重画"，不表示数据已上屏。
- **关闭顺序**：`Notifier.close()`（会等正在进行的那一次写结束，最多几微秒，保证不会写到一个已关闭、又被复用的 fd）→ 停用 QSocketNotifier → Python 关闭两端。

## 7. 线程和关闭顺序

- **GUI 线程**：Qt 事件、全部 GL 调用、`set_*`、`paint`。
- **Rust 读图线程**：服务器 6 个，其他机器按核数，最多 6 个。只做读盘、解压、写 CPU 缓存。不碰 GL，不碰 Python 对象，解压时不持有任何 paint 也要拿的锁。
- **关闭**：停通知 → 停读图线程、清空队列（有上限地等）→ 在当前上下文释放 GL → 销毁 Qt 控件 → 关 socket。第一轮在真实控件上按这个顺序测过：通知风暴还在进行时关闭，没有 GL 错误，没有残留 GL 对象。

## 8. Rust 模块（一个文件一件事）

| 模块 | 做什么 | 参考的设计（不拷代码） |
|---|---|---|
| `clock.rs` | 和 `time.monotonic()` 同一时间基的时钟（Linux CLOCK_MONOTONIC，Windows QueryPerformanceCounter）；实测相差约 1 微秒 | — |
| `posread.rs` | 按位置读：Linux `read_at`，Windows `seek_read`，只此一处 | — |
| `fileid.rs` | 文件大小、修改时间、文件编号 | — |
| `decode.rs` | 块解压（deflate、LZW、不压缩），预测器，字节序 | Odon `xenium/tiff_pyramid.rs`：直接解文件里的块 |
| `manifest.rs` | 原始金字塔的块表 | — |
| `reader.rs` | 定位块、读、解压、裁到区域 | — |
| `batch.rs` | 固定线程数批量读（探针用；产品调度是以后单独的模块） | — |
| `notify.rs` | 重画通知 | — |
| `gl.rs` | 在 Qt 的上下文里画（第一轮只画测试图案） | — |
| `error.rs` | 统一的错误类型 | — |

## 9. GL 约定（第一轮在服务器上逐条测过）

- 函数一律通过 Qt 的 `QOpenGLContext.getProcAddress` 加载（glow `from_loader_function`），不调用 GLX/WGL/EGL。最低要求 OpenGL 3.3 core，着色器 `#version 330 core`，和现有代码一致。
- 画到 Qt 给的 `defaultFramebufferObject()`；视口按设备像素设置。
- 每次 paint 后，把自己动过的状态恢复成 GL 默认值：程序 0、VAO 0、数组缓冲 0、纹理单元 0 且不绑定纹理、混合 / 裁剪 / 模板 / 深度关闭、解包对齐 4。绘制帧缓冲保持 Qt 给的那个。热路径不做查询。
- 和别的绘制者共存的约定：每个绘制者进出时，状态都是上面那组默认值。
- 资源释放：接 `aboutToBeDestroyed`，在其中 `makeCurrent` 后释放（实测此时上下文确实是当前的）；新上下文在下一次 initializeGL 里重建。
