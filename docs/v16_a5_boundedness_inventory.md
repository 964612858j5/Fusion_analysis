# v16 A5 有界性清单与资源记录（块 A8 的一部分）

日期：2026-10-05。依据：合并基准 v2.3 §7（v2.2 §7 原样）；A8 申请 v1 §3.5；用户 2026-10-05 裁定（只记录缓存上限之和，不设全局预算；发现的问题另行申请）。

§7.2 的要求：**新路径**不得含有意无界的图像队列、解码图块队列、分割暂存队列、标签队列、缓存或与整张切片像素数成正比的对象列表；**已有**的、有实际上限的缓冲区列为清单。

分类：**B** 有显式上限；**BS** 结构性有界（例如每个在途 worker 一项、每块清空、按用户画的 patch 数）；**PROP** 与整张切片像素数或细胞数成正比；**?** 需进一步确认。

上限常量统一在 `core/resource_tiers.py`（A8）。

---

## 1. 视图（Step0 全图 / compare、Step1、Step3）

| 位置 | 内容 | 上限 | 类 |
|---|---|---|---|
| `step0_explore_tab.py:280`、`step1_viewer_host.py:404`、`compare_strip.py:235`（复用标签页的缓存） | 原始 / 校正图块 LRU（`viewer/caches.py`） | 每个视图栈：原始 512 MB、校正 2 GB；每次放入都淘汰，但最新一项即使单独超预算也保留 | B |
| `explore_view.py:1099` | 整片 overview 记录 | 256 MB（总保留最后一条） | B（软：一条） |
| `explore_view.py:2363` | floor 缓存 | 8 项，每项 ≤ 4 MP | B |
| `explore_view.py` 预取 | 方向预取 48、原始预取环 64、图元池每层 400 | 显式常量 | B |
| `viewer/scheduler.py:163–165` | 待处理表与两个堆 | 无 maxsize；条目只来自视口相关的请求（可见块、环 ≤ 64、方向 ≤ 48、HOT 预取）；取消的代际在 worker 取到时丢弃 | BS |
| `viewer/scheduler.py:347` | `_stale_gens`（已取消的代际集合） | 每次取消加一项，从不清理；每项很小，但随会话时长增长 | **?** |
| `multichannel_prefetch.py:148–149` | HOT 预取队列 / 在途 | 每次计划变更清空；在途 2 | BS / B |
| `workers/preload_scheduler.py` | patch 预加载队列与存储 | 队列 256；存储 512 MB | B |
| `workers/display_seed_worker.py:48` | 显示种子队列（带数组） | 按（绑定、通道、角色）去重，实际约每个可见通道一项 | BS |
| `core/preview_compose.py:62` | 预览缓存 | 64 项且 160 MB | B |
| `step1_compose_coordinator.py` | 合成缓存 / 线程池 | 256 MB；线程池队列由视口图块产生并按键去重 | B / BS |
| `step1_gpu_layer.py`、`step1_viewer_mount.py` | GPU 纹理 | 原始 512 MB、标签 256 MB；粗层每通道 32 MB、细层每通道 48 MB | B（显存） |

**缓存上限之和（主机内存）**：Step0 视图 2.5 GB + Step1/Step3 视图 2.5 GB + overview 256 MB + compose 256 MB + 预加载 512 MB + 预览 160 MB + montage 1.25 GB ≈ **7.4 GB**，接近本机约 10 GB。各缓存不会同时满，实际峰值以 §4 的测量为准（用户裁定：只记录，不设全局预算）。

## 2. Step2 分割

| 位置 | 内容 | 上限 | 类 |
|---|---|---|---|
| `segment_merge_worker.py:2656–2715`、`3603–3647` | 全分辨率 mask / DAPI / nuclei 的 `np.memmap` | 磁盘映射，页面可换出，大小为 H×W；逐块写入 | PROP（磁盘映射） |
| **`segment_merge_worker.py:3157、4095`（cell mask）、`3207、4142`（nuclei mask）** | **导出 OME-TIFF 时 `mmap.astype(np.float32)`** | **整个区域一次性进内存，4 B/像素**；旁边的 zarr 写入是分块的，这里不是 | **PROP（内存）** |
| **`segment_merge_worker.py:3174、4110`** | **导出 DAPI 时 `np.array(dapi_mmap)`** | **整个区域进内存，2 B/像素** | **PROP（内存）** |
| `segment_merge_worker.py:2136` | HQ2 调试层导出 | 整层 float32 进内存 | PROP（内存） |
| `core/seam_merge.py:428–446` | 接缝候选 | 先逐块落盘，`finish` 时**整区一起读回**内存 | PROP（随接缝长度 × 细胞密度） |
| `segment_merge_worker.py:714` | nucleus → cell 表 | 整表进内存，每核 4 B | PROP（细胞数） |
| `segment_merge_worker.py:2686、3022` | `hq_qc_rows`（HQ / HQ2 / CDS） | 每个细胞一行字典 | PROP（细胞数；这几种方法不在维护范围） |
| `utils/tile_prefetch.py`、`utils/channel_cache.py` | 图块预取 / 通道缓存 | 预取 2；通道缓存 32 项（按项数，不按字节） | B |
| `ui/main_window.py:8324` `_proc_queue = mp.Queue()` | Step1 patch 预览消息 | 无 maxsize；生产者是用户画的 patch × 参数组合，界面定时取走 | BS |

按合成拼接大图（30,874 × 32,430 ≈ 1.0 G 像素）估算：cell mask 导出约 **4.0 GB**、DAPI 约 **2.0 GB**，导出时两者先后各自整块进内存。

## 3. Step4 定量

| 位置 | 内容 | 上限 | 类 |
|---|---|---|---|
| `core/quant_engine.py:931` | 通道块队列 | `queue_depth` = 2，每块 ≤ 256 MB（约 4 块同时在内存） | B |
| `core/quant_engine.py:156–190` `ChannelAcc` | 每通道统计累加器 | `accumulator_budget` 1.5 GB，按通道分组；但**每组至少 1 个通道**，极高细胞数时单通道可超预算 | B（单通道例外，**?**） |
| `core/quant_engine.py:131–153` `Geometry` | 9 个按标签的形态累加数组 | 约 150 B/标签，**不在累加器预算内** | PROP（细胞数） |
| `core/quant_engine.py:1067–1100` | counts、ids、obs（17 列 float64） | 按细胞 | PROP（细胞数） |
| `workers/feature_extract_worker.py:124` | 写 h5ad 时整个 objects × channels 的 X | **整矩阵进内存** | PROP（细胞 × 通道） |
| `workers/feature_extract_worker.py:159` | 分布统计层 | 每层整体读入 | PROP（细胞 × marker） |
| `core/object_tables.py:199–252` `build_cells` | 整个 cells.parquet 在内存（含重复的 run_id / slide_id 字符串列表） | 不分块 | PROP（细胞数） |
| `core/quant_engine.py:385–420` | 表达矩阵 zarr | 磁盘，按组写、按行块读 | BS（磁盘） |

## 4. 资源记录（§7.1）

待测：视图浏览、Step2、Step4 各在代表性数据上跑一次，记录峰值 RSS、峰值显存、tile 大小、队列深度、缓存字节、耗时；Windows 侧 working set、commit、系统 commit、page file 增长由用户读取。数据：`~/fusionflux/synthetic/synthetic_2x2_mirror.ome.tif`（2026-10-05 重新生成，3.72 GB，生成峰值 RSS 2.73 GB）与原始 `cropped_region.ome.tif`。

## 5. 结论与待裁定事项

- **新路径**（A6 以来新增的代码）没有发现无界或与像素数成正比的结构。
- **已有路径中值得关注的**（只记录，不在本块修改；修改需另行申请）：
  1. Step2 导出 OME-TIFF：mask（float32）与 DAPI 整块进内存——在大切片上最可能导致本机内存不足；§4 的测量会验证。
  2. Step4：`Geometry` 不在累加器预算内；写 h5ad 时整个 X 矩阵进内存；`build_cells` 整表进内存——随细胞数增长，TMA 规模下需要关注。
  3. 视图调度器的 `_stale_gens` 随会话增长（很小）。
  4. 主机缓存上限之和约 7.4 GB，接近本机内存。
