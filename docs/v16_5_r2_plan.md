# v16_5 R2：完整绘制（计划第二版，2026-10-12 用户批准）

- 依据：总方案 `docs/v16_5_rust_viewer_plan.md` v2.1 的 R2 行（"虚拟纹理扩展到三族格式、Fusion、平滑、ROI 裁剪和模板、准入全部规则、状态上报、接入 Step1（开关，默认关）、截图对比工具"），§3（GL 契约）、§5.1–5.3、§7（像素契约）。
- 前一阶段：R1 完整读图已推送（d416a98，A9 文档 §49），CI 在 Linux 和 Windows 上都通过。
- 用户 2026-10-12 的裁定：
  - R1 的四处偏离全部接受；
  - 整片粗级别现算的分片改成和数据分块对齐，在 R2 里做。
- 原则：
  - Rust 画出的稳定画面和现有 Python GPU 路径逐像素一致；
  - 旧路径仍是回退；
  - 开关默认关（`BLOCK01_STEP1_BACKEND`，默认 `python`），产品默认行为不变；
  - Step3 标签、montage、`gpu_memory` 的隐藏和释放记账属于 R3，不在本阶段。
- 第二版：吸收 codex（astra 高强度，luna 最高强度子代理核对）对第一版的全部意见（§9）。

## 1. 现状（已核对代码）

| 项目 | Python GPU 路径（`ui/step1_gpu_layer.py`、`step1_gpu_vt.py`、`step1_gpu_binding.py`） | Rust（S-Raw + R1 之后） |
|---|---|---|
| 数据格式 | 三种存储格式、两个采样器族：8 位 → R8UI 数组纹理，16 位 → R16UI，其余（校正、浮点原始、缺失）→ R32F，无效像素写成 NaN（`_upload_values`）。着色器里 u8/u16 同属"整数族"（`_family`），浮点单独一族；每块最多 64 MiB，层数按格式宽度定（`_block_layers`）。采样器 32 个单元按固定比例分（`sampler_split`：整数 25、浮点 4）；单遍装不下时 Python 退回逐通道多遍绘制（`_render_vt` 返回 False） | 只有 R8UI；`build()` 还拒绝非 uint8 的片子 |
| 读图 | 调度器按 `Step1TileProvider.read_tile_native` 读 | 线程池只读原始 8 位（`pool::read_tile`）；R1 的 `step1_tile::read`（原始 / 校正 / 缺失）只供测试调用 |
| 单遍合成 | `step1_gpu_vt.frag`：Overlay 逐通道叠加；Fusion 时按组求和再取最大值（`acc.r`），细胞核取最大值（`acc.b`）。参数行第 3 个像素放组号、组权重；组按显示配置里的插入顺序，组内按配置里的通道顺序，细胞核行最后（组号 −1）；同一通道可在多个组里出现（参数行重复，准入和页表按通道去重） | 着色器是同一份逐字节拷贝；参数行只填了 Overlay 的字段，按通道一行 |
| 最后一步 | `_finalize`：Overlay 用 `final_overlay`，Fusion 用 `final_fusion`；ROI 外接框用剪裁框；多边形用模板缓冲按奇偶规则画扇形（`_write_polygon_stencil`，`step1_gpu_mask.vert/.frag`），被拒绝的多边形（`sanitize_roi_polygon` 只查点的坐标和边界）退回外接框并报告原因 | 只有剪裁框（`roi_scissor_box`），只有 `final_overlay` |
| 准入 | 每通道按读出格式计字节：原始用原生宽度，其余 4 字节（`_pixel_bytes`、`_cost_bytes`）；总额度减去每种在用格式各一块的余量（`_total_budget`） | 所有通道按 1 字节计 |
| 状态 | `stats()`：unavailable（缺失原因）、last_error、base_overflow_channels、resolution_limited、admitted / ideal level、coarse_pending、deferred_uploads 等；`_submission` 里有 missing_windows、roi_polygon_error | 只有一部分 |
| 接入 | — | 已接入（S-Raw）。不支持时整体换回旧画布：Fusion、非原始通道、局部 ROI、多边形 ROI |

## 2. R2 要做的

1. **读图线程池用完整的读块规则**
   - `pool.rs` 改用 R1 的 `step1_tile::read`（原始 u8/u16/浮点、校正、缺失），清单来自 `build_corrected_manifest`；
   - 校正的长时间现算用"还要不要"作为取消条件；
   - CPU 缓存里的块带上格式和有效区域（矩形，或浮点里的 NaN），按实际字节计。
2. **整片粗级别现算的分片对齐到数据分块**（用户裁定）
   - 数据分块从产品的左上角（ROI 的第 0 级起点）起算，求平均的块从第 0 级原点起算，两者一般对不齐。所以：分片按**数据分块**对齐（k×k 个分块，k 取能放进 4M 像素的最大值，至少 1），求平均的块跨片时照常累加，每块的样本数按真实样本计（`accumulate` 已按实际落点计算前缀和补齐）；
   - 临时内存：一片的数据（≤ 4M 像素 × 4 字节）、读这一片时正在解压的一个数据分块，加上输出的和与计数，不再有补齐用的临时数组；
   - "每个分块只解压一次"指一次读块请求之内；
   - 结果和 Python 只差累加次序（§7：|a−b| ≤ 1e-6 + 1e-6·|b|），有效掩膜完全一致；
   - 测解压次数和整片耗时。
3. **GPU 三种存储格式、两个采样器族**
   - `gpu_store.rs` 按格式分块：R8UI、R16UI、R32F，每块最多 64 MiB，层数按宽度定（小额度时按 `_block_layers` 的"最多额度的四分之一"）；
   - 整数族和浮点族各自编号；
   - 上传前，浮点块把无效处写成 NaN（同 `_upload_values`）；
   - 预留和预热（§48）按格式各自计算；`gpu_warm.rs`（旧预热，回退开关用）补上 R16UI / R32F。
   - **采样器容量**：Python 按固定比例分（整数 25、浮点 4），装不下时退回多遍绘制。Rust 不做多遍绘制，改为：
     - 按当前在画通道的需要分配两族的单元数（只影响着色器里的采样器安排，不影响像素），分法变化时换用对应的着色器（按分法缓存，编译一次）；
     - 两族合计仍装不下时（总块数超过可用单元），整体换回旧画布并说明原因，不画不完整的画面；
     - **准入完全照 Python**（不因采样器压低额度，否则 Rust 会停在更粗的一级）；准入出来的画面在两族合计装不下时，整体换回旧画布。单独测这种"采样器饱和"的情况。
4. **单遍合成补齐 Fusion**
   - 页表编码带族号；
   - meta 纹理覆盖两族；
   - 参数行补组号、组权重；行序和 Python 相同（组按显示顺序，组内按通道，最后是细胞核行，组号 −1）；同一通道可出现在多行；
   - 最后一步按模式选 `final_overlay` / `final_fusion`（着色器已有，逐字节拷贝）。
5. **ROI**
   - 局部矩形 ROI：剪裁框（已有）加读图裁剪（R1 已有）；
   - 多边形：拷贝 `step1_gpu_mask.vert/.frag`，Rust 画模板（奇偶规则，扇形），只在多边形内显示，画完恢复 GL 状态；
   - 多边形是否可用由 Python 的 `sanitize_roi_polygon` 判定（只查点的坐标和边界，不查几何形状），Rust 只接收判定后的点列；被拒绝时退回外接框并上报原因。
6. **准入全部规则**
   - 每通道的每像素字节数（原始用原生宽度，其余 4）；
   - 总额度减去在用格式各一块的余量；
   - Fusion 的在画通道（组 + 细胞核）；
   - 没有显示窗口的通道不画、上报 missing_windows；
   - 和 Python 的 `_admit_new` 做参数化比对（沿用 S-Raw 的准入比对测试，加上混合格式）。
7. **状态上报**：`Step1CoreBinding.stats()` 和画布的 `environment_report` / `cache_stats` / submission 字段，补齐挂载和 A9 工具读到的键：
   - unavailable（缺失产品的原因）、missing_windows、roi_polygon_error、coarse_pending、deferred_uploads；
   - 键名和含义同 Python 路径；
   - 逐键对照表写进接口文档。
8. **接入和开关**
   - `Step1CoreBinding._unsupported_reason` 去掉"只画 Overlay / 只画原始 / 只画整片 ROI / 不画多边形"四条限制，`build()` 去掉"片子必须是 uint8"的拒绝；
   - 仍然换回旧画布的情况：GL 初始化失败、清单核对失败、Rust 拒绝的数据布局（R1 的拒绝清单）、采样器装不下、标签层（R3）；
   - 开关仍是 `BLOCK01_STEP1_BACKEND`，默认 `python`。
9. **截图对比工具**：把 `scripts/sraw_pixel_probe.py` 扩展成通用工具 `scripts/r2_pixel_compare.py`：
   - 同一项目、同一组镜头，各起一个进程分别用两条路径；
   - "已稳定"不只看画面不变：要核对当前用的是哪条路径、镜头和显示设置是这次要求的、在画通道齐全、目标层全部上屏、没有待上传、并且这一帧确实已经呈现，然后读回 `final`，逐像素比，含透明度；
   - 有差异时输出差异图和统计；
   - 可选项：Overlay / Fusion、通道组合、ROI 形状、平滑开关。

**不做**：Step3 标签、montage 共存、`gpu_memory` 的隐藏释放和记账、运行中回退（R3）；20 通道冷热各 5 轮的正式配对（R4）。

## 3. 像素契约（关口的判定标准）

总方案 §7 要求"新旧稳定画面逐像素一致"。本阶段除下面**一处需要用户批准的标准修改**外，全部逐像素。

- **逐像素一致**（RGB 和透明度都相同）：
  - 原始通道（u8 / u16 / 浮点）；
  - 校正通道的第 0 级和 sidecar 切片；
  - 缺失通道（不画）；
  - Overlay 和 Fusion；
  - 矩形和多边形 ROI；
  - 平滑开和关。
- **请求批准的标准修改：没有 sidecar 时，校正通道的现算层级**
  - 原因：两条路径对同一批源值求平均的累加次序不同（numpy 的成对求和，和 Rust 按分块对齐的分片；后者是用户已批准的改法），数值只在 §7 误差 |a−b| ≤ 1e-6 + 1e-6·|b| 内相同，不能逐字节相同。窄窗口或 gamma 会把这点差别放大到颜色上，所以这一类画面无法保证逐像素。
  - 修改内容：这一类画面不作逐像素关口，改为两项检查：
    1. 数值关口：现算结果按 §7 误差验收，有效掩膜完全一致（`test_v16_5_r2_reduce_aligned.py`）；
    2. 渲染关口：合成项目给每个粗级别配上合格的 sidecar 粗图，两条路径读到逐字节相同的数值，画面要求逐像素一致，证明绘制本身一样。
  - 真实 biopsy 的粗级别画面报告差异统计：差异像素数、每个分量的最大差、对应的源值差。若出现源值差超出 §7 误差的情况，停下查原因。
  - 不批准的替代办法：Rust 逐位复现 numpy 的累加次序。这依赖 numpy 内部的求和方式和版本，既脆弱，也和已批准的分块对齐冲突，不建议。
- **"已稳定"**：见 §2 第 9 条。

## 4. 文件范围

Rust（`native/fusion_viewer_core/`，每个模块只做一件事，不超过约 300 行；需要时拆新模块）：
- 改：
  - `pool.rs`、`cache.rs`：块带格式，用 `step1_tile`；
  - `step1_tile.rs`、`reduce.rs`：对齐分块的分片；
  - `gpu_store.rs`、`gpu_touch.rs`、`gpu_warm.rs`、`reserve.rs`：三种格式；
  - `vt.rs`、`render.rs`、`render/upload.rs`、`frame.rs`：Fusion 行、最后一步选择、模板；
  - `plan.rs`、`admit.rs`、`viewer.rs`：每通道字节数、Fusion 在画通道；
  - `shader.rs`：按采样器分法生成并缓存着色器；
  - `py/corrected.rs`：清单解析复用给 `ViewerCore`；
  - `py/viewer.rs`（`set_display` 的组字段、多边形、stats）；
  - `lib.rs`、`py/mod.rs`。
- 新：
  - `stencil.rs`：多边形模板；
  - `shaders/step1_gpu_mask.vert`、`shaders/step1_gpu_mask.frag`（逐字节拷贝，测试保持一致）；
  - 视需要新增 `family.rs`（格式的字节宽度、GL 格式、族号）。

Python：
- 改：
  - `ui/step1_core_binding.py`：Fusion 行、校正清单、多边形、去掉四条限制、stats；
  - `ui/step1_core_layer.py`：submission / environment / cache 报告；
  - `ui/step1_core_adapter.py`：只在需要时改；
  - `ui/step1_viewer_mount.py`：只改 Rust 接入相关的几行，例如把清单传进去。
- 新：
  - `scripts/r2_pixel_compare.py`；
  - `tests/test_v16_5_r2_pixels.py`（一个进程一条，真实显示）；
  - `tests/test_v16_5_r2_admission.py`；
  - `tests/test_v16_5_r2_lifecycle.py`；
  - `tests/test_v16_5_r2_reduce_aligned.py`。
- **需要授权的测试迁移**：
  - `scripts/sraw_lifecycle_probe.py` 的 `fusion_fallback`、`partial_roi` 两个场景，以及 `tests/test_v16_5_sraw_lifecycle.py` 里对应的参数，现在断言"Fusion / 局部 ROI 换回旧画布"；
  - R2 支持这两种之后，改成断言"仍由 Rust 画、画面正确"；
  - 另加一个"采样器装不下 → 换回旧画布"的场景，保留回退路径的测试；
  - 只改这两个场景。
- 文档：本文件、`docs/v16_5_r0_interfaces.md`（stats 键对照）、`docs/v16_A9_application.md` §50（结果）。

不改：
- Python GPU 路径本身；
- 调度器；
- `Step1SourceTable`；
- Step0；
- 已保存的产品；
- 除上面列出的迁移外，现有测试的断言。

## 5. 关口（不过就停下报告）

1. **逐像素**：`r2_pixel_compare` 在以下组合下按 §3 判定通过：
   - 合成项目：u8 / u16 / 浮点原始 + 校正（每个粗级别都有 sidecar）+ 缺失；Overlay 和 Fusion（两组以上，含同一通道在两组、含细胞核）；整片 / 局部矩形 / 多边形（含凹多边形和点坐标越界、被拒绝的多边形）ROI；平滑开和关；每种 5 个镜头（全片、中间级、最细级、越出片子、放大到平滑生效）。
   - 专门的情况：NaN / ±Inf、整块无效、同一级跨图块和跨纹理块的平滑、粗层顶替、通道数不变但格式变了、小额度下的块尺寸、采样器饱和（换回旧画布）。
   - 真实项目：biopsy（3 个校正通道 + 原始，Full WSI）Overlay 和 Fusion（第 0 级逐像素；粗级别只报告，见 §3）；TMA3 20 通道 Overlay（回归 S-Raw）。
2. **准入**：混合格式下和 Python `_admit_new` 逐项一致（级别、图块、额度、余量）。
3. **分片对齐**：
   - 一次读块请求内，每个数据分块只解压一次；
   - 结果在 §7 误差内，掩膜完全一致；
   - 报告耗时（R1 是 3.7 s，Python 2.6 s；目标不慢于 Python）。
4. **生命周期**：
   - 暂停、恢复、换来源、关闭、从 Fusion 切回 Overlay 再切回；
   - 被拒绝的多边形退回外接框并上报原因；
   - 采样器装不下时换回旧画布；
   - 两条路径不同时运行。
5. **性能不退步**（20 通道热读 Overlay 3 轮，和 §48 的 3 轮同场景比，每项取 3 轮中位数）：
   - 9 个手势各自的停手稳定时间（截图）和程序内"目标层完整"时间，不比 §48 慢 10% 以上，且连续放大仍 ≤ 200 ms；
   - 手势中响应的中位数、95% 线、每轮超过 50 ms 的次数，不比 §48 差；
   - 每轮超过 50 ms 的界面卡顿次数不增加；
   - 打开时的预留耗时单独报告（含新增格式）；
   - 另跑 biopsy 校正通道的 Fusion 3 轮，只报告。
6. **GL 状态**：在**正式绘制路径**（`ViewerCore` 的每帧上传、合成、模板、剪裁、最后一步）上做整套 GL 状态快照比对，覆盖：剪裁框和模板同时生效、ROI 完全不在屏幕上时的提前返回、改尺寸、换父控件、同一画布里另一个绘制者。
7. **回归**：
   - R0、S-Raw、R1 的全部测试；
   - 原 37 个模块（清单见 A9 文档 §49.7 和 R1 计划 §7）、§49 加的 `test_step1_source_table`、`test_v16_a9p_source_parity`、`test_step1_coarse_plane`；
   - 已知失败：`test_step1_gpu_takeover::test_a_cold_patch_still_arrives_tile_by_tile_from_its_worker`、`test_step1_gpu_sources::test_hardware_fbo_partial_coarse_never_shows_a_centre_first_channel`（以及不在这组里的 `test_preview_source_provider::test_save_invalidation_repulls_into_workbench`）；
   - 除这些外没有变化；
   - CI 两个平台通过；
   - codex 代码审核没有必须修的问题。

## 6. 风险

- **校正现算层级的数值差在画面上显现**：窄窗口或 gamma 会放大源值差，所以不把它当成画面关口（§3 用 sidecar 夹具）；真实项目上只报告。
- **R32F 和 R16UI 块的首次使用代价**可能和 R8UI 不同。按 §48 的方法分格式实测，必要时调整预留。
- **采样器数量**：整数族和浮点族共用 32 个单元。Rust 按需分配两族的单元，装不下时整体回退（§2 第 3 条）。很多校正通道同时显示时可能回退到旧路径，这种情况会上报，并在结果里统计。
- **Fusion 的同一通道多行**：页表按通道一层，参数行可以重复，和 Python 相同。

## 7. 工期

10–15 个工作日（codex 的估计）。比总方案的 7–10 天多，用于分片对齐、多边形模板、采样器按需分配和回退、正式绘制路径上的 GL 状态检查，以及更全的像素用例。

## 8. 需要用户决定的事

1. **批准 §3 的标准修改**：没有 sidecar 时，校正通道现算层级的画面不作逐像素关口，改为"数值按 §7 误差 + 用 sidecar 夹具验证绘制逐像素"；真实 biopsy 的粗级别只报告。其余画面全部逐像素。
2. 授权 §4 里的测试迁移：S-Raw 生命周期的两个场景。
3. 采样器的做法：准入照 Python，两族按需分配单元，装不下时整体回退（§2 第 3 条），而不是在 Rust 里再写一套多遍绘制。

## 9. 第一版审核意见的处理（codex，astra 高强度，luna 最高强度子代理核对）

- 必须改：
  - 像素标准不能自行放宽 → §3：其余画面逐像素；没有 sidecar 的现算层级作为一处标准修改请用户批准；
  - 采样器饱和 → §2 第 3 条；
  - 分片对齐写错了坐标系 → §2 第 2 条；
  - 测试迁移和"不改断言"自相矛盾 → §4 写明需要授权的迁移；补上 `gpu_warm.rs`、`py/corrected.rs`；
  - GL 状态要在正式路径上测、"已稳定"要核对路径、镜头、显示、通道和呈现 → §2 第 9 条、§5 第 6 条。
- 应该改：
  - Fusion 的组序和通道序、参数行重复但准入去重、`build()` 的 uint8 拒绝 → §1、§2；
  - 补专门的用例 → §5 第 1 条；
  - 性能不退步写成可量的标准，列出回归清单和已知失败 → §5 第 5、7 条。
- 小问题：措辞（"三种存储格式、两个采样器族"、"最多 64 MiB"、多边形检查只查坐标和边界）→ 已改。
- 工期：采纳 10–15 天。

- 第二轮复核：剩两处要说清，已改。
  - 真实项目上没有 sidecar 的现算层级不作逐像素关口，实际上是对总方案 §7 的修改，要明确请用户批准（§3、§8 第 1 条）；
  - 不能因采样器压低准入额度，否则和"准入与 Python 一致"矛盾，改为准入照 Python、装不下就回退（§2 第 3 条）。
  - 另补：临时内存包括正在解压的数据分块。
- 用户批准（2026-10-12）：§8 三点全部同意——§3 的标准修改、§4 的两个测试场景迁移、采样器"准入照 Python、装不下整体回退"。授权范围包括 §4 列出的文件（含 `pool.rs`、`cache.rs`、读图和缓存相关改动，AGENTS 规则 4）。
