# Step1 预分割（Method & Parameters / Patch Results）重设计 — 项目计划

日期：2026-09-23（第三版，块 A0 产出）　分支 `v15-interactive-channel-workspace`，起点 `c9f80df`，A0 核查基于 `e655409`。
状态：**已执行：块 P、A1、A2、B、C、D、V0。本文档记录的验收：B「用户验收总体通过」、C 第 4 步「用户人工测试通过」、D「块 D 验收通过（2026-09-25）」；P、A1、A2、V0 的执行记录仍写「待用户验收」，文档中没有后续验收记录。V2（代码中称「Step2 hook-up」）第 1、2 步已提交（`54e825d`、`1adfe4e`），第 3 步已提交（`dcaca2c`），真机上 Cellpose 路径跑通，Mesmer 未验收；块 L 已提交（`1d14801`、`2294bc7`）并通过真机验收；块 F 已提交（`5bc65bc`）并通过真机验收；块 E 已提交（`7ee98fc`）并通过真机验收（Mesmer 除外）；U1、Results 顺序、块 S 已提交；块 K 已实施并通过真机验收；块 M 已实施并通过真机验收；块 N 已实施并通过真机验收；块 ④a 已实施（数据层自动验收通过）；块 ④b 已实施（自动验收通过）；S2 冻结；后续计划见第六节。** 提交本文档不代表批准任何生产实施。每块须用户单独启动；模块级改动须另行批准。

修订记录：
- v1：初稿。
- v2：采纳独立审核的第 1、3、4、5、6 条，以及「先做契约准备块、A 拆成 A1/A2、选定的基本约束随 C 落地」的建议。第 2 条（同名方法合并）按用户裁定**维持 v1 方案**。另补上可核验的测试基线（附录）。
- v3：块 A0 的 8 项产出，见**第七节**。第一至六节的正文不改，凡被第七节更正或细化的地方，以第七节为准（4.5 的 Mesmer 两行、4.4 的来源字段、4.7 的资格规则）。第七节里标「待确认」的条目，确认前不算定稿。
- v3.1：按独立审核意见修订第七节：F1、P1–P3、O1、O2、L1、S1、T1、T2、E1、E2 已裁定，另外明确块 D 缓存和线程的授权边界。新增 7.10 块 V（分割方法运行沙箱，用户提出，**未批准**）。
- v3.2：块 V 的方向通过独立审核。7.10 按审核意见重写，分为 V0 / V1/C / V2 三个阶段，只有 V0 可以申请启动。
- v3.99：块 S4-2 真机验收通过；记录真机中发现的 StarDist 巨大误检（另立块）。
- v3.98：块 S4-2 已实施（自动验收通过，待真机验收），写入执行记录。
- v3.97：块 S4-2 v2 获批（N3 之前的运行允许定量并提示风险；CSV 只在勾选时输出，批处理加勾选框）。
- v3.96：块 S4-2 申请 v2（单一 h5ad、一行一个主对象；输出内存上界：通道分组 + FeatureMatrixSink、50 万对象的内存门；`Expression regions`；N3 之前的运行如何处理待裁定）。
- v3.95：块 N3b 真机验收通过（20 块网格）。
- v3.94：块 N3b 已实施（自动验收通过，待真机验收），写入执行记录与实施中的用户裁定（区域结束时一次裁决）。
- v3.93：用户锁定两个 0.5、批准 N3b 的测试改动清单；N3a 提交推送，开始 N3b。
- v3.92：N3a 之后的审核意见（锁定两个 0.5、元数据记录 `seam_merge` 版本与参数）与 N3b 须报批的测试改动清单，待用户确认。
- v3.91：块 N3a 完成（纯函数、13 条测试、真实数据探测；分布分成两堆；须加辅助条件「空像素 < 50 % 整个放弃」），等用户锁定规则。
- v3.90：块 N3 v3 获批；申请提交推送后开始 N3a。
- v3.89：块 N3 按第二轮独立审核修订为 v3（重叠图的连通分量、以切块版本为单位裁决以处理 split / merge 分歧；等齐上下左右与对角邻块、四块交汇的验收；分 N3a 探测 / N3b 启用，τ 在 N3a 之后由用户锁定；`seam_reconciliation` 记录）。
- v3.88：块 N3 按独立审核修订为 v2（B2：接缝候选在最终去留与编号之前裁决，补回两边都丢掉的细胞；重复按归属余量取舍、与遍历顺序无关；τ 先测分布再锁定；流式收尾核对）。
- v3.87：用户裁定 S4-2 只有一个 h5ad、先修 Step2；块 N3 只读调查与申请 v1（Step2 切块接缝的合并冲突：后写覆盖先写，造成重复 / 截断 / 空标签与核错位，所有方法受影响）。
- v3.86：块 S4-2 申请 v1（核 / 胞质按逐像素定义、h5ad 新结构、核表、CSV 可选、输出范围问卷、装 anndata；新发现：Step2 合并在切块接缝上违反「核在它的细胞内」，457 个像素 / 38 个核）。
- v3.85：块 S4-1 真机验收通过。
- v3.84：块 S4-1 已实施（自动验收通过，待真机验收），写入执行记录与实施中的两项用户裁定（纯核运行按核定量；批处理的 median 不改）。
- v3.83：块 S4-1 v2 获批（8 项裁定按 v2）。
- v3.82：块 S4-1 按独立审核修订为 v2（产品输出目录 `step4/quantification_runs/…`；不输出圆度；周长改名 `boundary_pixel_count` 并写明定义；QuantReader / QuantBackend / QuantFinalizer 三个边界；「科学来源绝不退回」写成契约；S4-0 报告补记产品路径的来源缺陷）。
- v3.81：块 S4-1 申请 v1（严格定量来源 + Numba 流式融合内核；新发现：产品路径的 Step4 找不到 `correction_config.json`，校正通道被静默按原始数据定量；「读 8.8 GB 要 10 s」的原因与按分块并行解压的实测）。
- v3.80：S4-1P 结果的用户裁定（Numba 按通道并行作产品、保留 Rust 原型与切换条件、GPU 不在 S4-1、uint8 直接输入、形态对 skimage 验收、空标签不输出）。
- v3.79：块 S4-1P 完成（整个区域 12–13 s，已经受 I/O 限制；旧的长短轴 / 偏心率有 float32 误差；累加器结构与空细胞约定待 S4-1 定）。
- v3.78：块 S4-1P 获批（Rust 工具链授权、GPU 纳入；决定规则待结果出来后讨论）。
- v3.77：块 S4-1P 申请 v1（CPU / GPU 内核探测，采纳用户与 ChatGPT 的讨论；本机核实 CuPy 的 nvrtc 加载问题、没有 Rust 工具链）。
- v3.76：块 S4-0 B 部分完成；S4-0 完成。
- v3.75：块 S4-0 A 部分实测完成（报告 `docs/benchmarks/step4/baseline_2026-09-27.md`）。
- v3.74：块 S4-0 v2 获批（用户做校正数据；主区域至少一个统计量；核表在 S4-2；周长只保留新定义）。
- v3.73：块 S4-0 按独立审核修订为 v2（两份对照、分项计时不重复、峰值内存双记录、推算标明、来源记录、输出目录参数化；test1 无校正通道的更正）；Step4 契约 8 条写进调查一节。
- v3.72：Step4 改造的调查结论、用户裁定（输出范围问卷等）与分块；块 S4-0 申请 v1（基线实测）。
- v3.71：块 B3 真机验收通过。
- v3.70：块 B3 获批并实施（自动验收通过，待真机验收），写入执行记录。
- v3.69：块 B3 申请 v1（Step1 Save 进度与按钮位置；Step3 通用结果查看器；回到 Step1 时 Pre-seg Results 图像消失）。
- v3.68：第 ③ 步真机验收通过；Step3 重设计的各步全部完成。
- v3.67：第 ③ 步已实施（自动验收通过，待真机验收），写入执行记录。
- v3.66：第 ③ 步申请 v2 获批（一行；运行下拉框移到标签栏右侧角落；patch 选中与 Step1 共用）。
- v3.65：第 ③ 步申请 v1（patch 按钮条组件化 + Step3 patch 空降）；交接文件更新。
- v3.64：块 N2 真机验收通过。
- v3.63：块 N2 已实施（自动验收通过，待真机验收），写入执行记录；附带用户授权修复整图模式下 Mesmer metadata 的 `UnboundLocalError`。
- v3.62：块 N2 v4 获批；第六节后续顺序按独立审核建议更新（Step4 之后：WSI 阻塞项清理 → TMA 基础 → QualityMask → TMA 批处理 → 真实数据剖析 → Rust 基础）。
- v3.61：块 N2 按独立审核修订为 v4（归属措辞：核唯一属于一个细胞、细胞可有多核；科学输出的事务规则；LabelStore 作统一语义入口；对应表按块写入与 2^32 保护；丢弃分三类并记保留率；新核通路与 HQ 解耦；I/O 失败事务与切块边界两项验收）；清理第六节中被块 K / M / S5 取代的过期条目。
- v3.60：块 N2 按裁定 A（多核全保留）/ B（不写核 OME-TIFF）修订为 v3：核独立编号 + 「核 → 细胞」对应表。
- v3.59：块 N2 按用户裁定修订为 v2（核与细胞一一对应、骑跨即丢弃、实测配对速度无需 Rust；以 zarr 为准的标签存储与编号契约，对标未来 Step4 的流式定量；不再写核 OME-TIFF；核的临时 memmap 用后即删）；后续计划按用户排定的顺序改写。
- v3.58：块 N2 申请 v1（所有计算了核的方法保留核 mask：expansion 带回核；nuclear-guided 的核改为独立归属与编号；新标志 keeps_nuclei；Step3 分类表更新）。
- v3.57：块 ④c 真机验收（第 1–4、6 项通过，第 5 项真机未验）；记录 expansion 方法的核 mask 被 Step2 丢弃，用户裁定另立块。
- v3.56：块 ④c 已实施（自动验收通过，待真机验收），写入执行记录。
- v3.55：块 ④c v2 获批（4 项裁定同意，含 ④b 错误状态的最小扩围）。
- v3.54：块 ④c 按独立审核修订为 v2（Show 打开可补读；两种 mask 默认显示；④b 错误状态的最小扩围；来源不变不重设；颜色只用于轮廓；默认选择按规则验收；复制件须改写路径；设置在 mount 重建后重新应用；不闪空的前提；退出段错误在部署机出现即生命周期不通过）。
- v3.53：块 ④c 申请 v1（运行下拉框、细胞 / 核 mask 各一个下拉面板、提示文字；进入 Step3 / Step2 新结果 / 换数据集的刷新；兼作 ④a、④b 的真机验收）。
- v3.52：块 ④b 已实施（自动验收通过，含本机真实 GL 读回；真机随 ④c），写入执行记录与环境问题（本机 D3D12 驱动退出时段错误）。
- v3.51：块 ④b v2 获批，5 项裁定按建议（核在上、逻辑线宽、旧层暂留目标优先、暂停停止补生成、256 MB）。
- v3.50：块 ④b 按独立审核修订为 v2（目标层级优先与编号 0 覆盖；逻辑线宽与参考半径扩到 8；标签沿用图像的多边形边界；暂停与补生成、单线程的如实说明；预算实测与超额顺序、CPU 侧计入等待结果；编号图独立参考；Step3 图像不变的验收）。
- v3.49：块 ④b 申请 v1（GPU 标签渲染：标签绑定与后台线程、64 MB 标签纹理、编号图与轮廓 / 填充着色器、mount 的 mask 开关；本机真实 GL 读回验证）。
- v3.48：块 ④a 已实施（数据层自动验收通过，真机随 ④c），写入执行记录与实施中确定的 6 项细节。
- v3.47：块 ④a 按独立审核修订为 v2（细胞 / 核按方法分类；读取前完整校验；坐标来源表与证据不足不显示；只有第 0 级时粗层返回「不可用」；扫描去重与排除未完成运行；取消不走内存退路；屏幕编号图的轮廓规则）。
- v3.46：第 ④ 步调查结论与用户裁定（拆三块、后台线程、新增项批准、暂不做 CPU 版、控件位置、细胞 / 核 mask 各一个下拉面板）；块 ④a 申请。
- v3.45：块 S5 已实施并通过真机验收。
- v3.44：第 ⑤ 步改定（Step3 的 ROI 冻结、patch 全局同步）；块 S5 申请。
- v3.43：块 2c-2 已实施，真机验收第 1–3 项通过；偶发的 Step1 全黑记为待观察；第 ⑤ 步提前并记录沙盒要求。
- v3.42：块 2c-2 按独立审核修订为 v2（Navigator 连接不依赖 Step1 的 viewer；重绑失败的回退；后台契约、热切换、模式验收的措辞；权限修补的文件验证）；记录用户目标权限（Step3 可编辑不保存 = 第 ⑤ 步）。
- v3.41：块 2c-2 申请（第二个 viewer；修补 2c-1 中 Step3 的 Tissue Navigator 按钮沿用 Step1 编辑权限的缺陷）。
- v3.40：块 2c-1 已实施并通过真机验收。
- v3.39：块 2c-1 按独立审核修订为 v2（功能空档须用户接受；入口的写文件边界；Load weights 的表述与两种验收；Show all 保留 Step1 语义；模式同步在 2c-1 / 2c-2 的分工；用户指南只写当前可用功能；拒绝原因显示在可见页面）。
- v3.38：块 2c 只读调查结论；2c 拆为 2c-1 / 2c-2；块 2c-1 申请（新 Step3 页面并删除旧页面）。
- v3.37：块 2b 已实施并通过真机验收（真机验收步骤修正为只看 Step1 / Step3）。
- v3.36：块 2b 按独立审核修订为 v2（明确接受旧 Step3 页面不跟随勾选的过渡限制；模式按钮切换的验收移到 2c；测试白名单封闭）。
- v3.35：块 2b 申请（Step3 与 Step1 共用显示范围、fusion 草稿与 Navigator 上下文）。
- v3.34：块 2a 已实施并通过真机验收。
- v3.33：块 2a v3（暂停期间模式切换只记录）；Load / Reset weights 措辞改正；全局联动范围写明为 Step1 ↔ Step3；删除旧方案残留；2b / 2c 待办（模式同步、界面规则精确替换、完整公开操作验收）。
- v3.32：用户改定勾选、权重等全部与 Step1 全局联动（方案 A 作废）；块 2a 按独立审核修订为 v2（只做暂停 / 恢复、前台发布、相机标识）。
- v3.31：Step3 骨架调查结论与用户裁定（Intensity 与颜色全局联动、方案 A 的 Step3 fusion 草稿、Reset / Load weights 只读、Navigator 实时联动、拆成 2a / 2b / 2c）；块 2a 申请。
- v3.30：块 N 已实施并通过真机验收。
- v3.29：块 N v2 获批；Step3 裁定 8a（补生成失败时的退路）。
- v3.28：Step3 重设计的只读调查、Odon 参考与用户裁定；块 N 申请（Step2 生成标签金字塔，已按独立审核修订为 v2）；后续计划加入 Step4 优化（`.dat` 清理）与长期的 NGFF 评估。
- v3.27：块 M 已实施并通过真机验收；新增已有问题（StarDist 偶发不一致、Cellpose CPU 极慢）。
- v3.26：块 M 申请与用户裁定（Step2 的 8 个方法统一走引擎子进程）。
- v3.25：块 K 真机验收通过并提交（`bc280d1`）；记录「旧路径 Stop 不能立即停止」的调查结论和用户决定（不改）。
- v3.24：块 K 已实施（旧路径 ROI 模式中途 Stop 不再登记成功），写入执行记录，待真机验收；新增已有问题：运行资源监控器 `NameError`。
- v3.23：用户裁定 HQ / HQ2 / CDS 这类不经 Step1 交接的方法不再维护（R2 加注，第六节新增「用户裁定」）。写入块 K 申请（Step2 旧路径 ROI 模式中途 Stop 不再登记成功，待批准），已按独立审核意见修订。
- v3.22：块 U1、Results 顺序、块 S（每次弹对话框、拒绝原因上屏）已提交；S2 冻结，记录调查结论；新增「后续计划」和「已知的已有问题」。
- v3.21：块 F、块 E 已提交（`5bc65bc`、`7ee98fc`），都通过真机验收。
- v3.20：记录 V2 第 3 步的提交（`dcaca2c`）和真机情况；新增块 L（Step1 Save 进度框、Step2 布局，计划外，用户 2026-09-25 提出）的申请、执行记录和真机验收。
- v3.19：补记 V2（Step2 hook-up）第 1、2 步的执行记录和用户裁定 A（写在 7.10.7 的 V2 下）；更新状态行。第 3 步的范围另行申请。
- v3.18：块 B 已执行（Methods 部分、参数表、R9 合并、方案保存与加载）。
- v3.17：Step1 patch 按钮统一用 patch 色（与 Step0 共用样式）。
- v3.16：左栏标签页 Channels 改名为 Fusion。
- v3.15：A2 后续：Delete（删除勾选的 patch）、标签页改名为 Pre-segmentation。
- v3.14：A2 已执行（实测、用户裁定层级为固定 16×、实现、测试）。
- v3.13：A1 已执行，写入执行记录；用户接受「重启后 Step0 模型为空」的风险，暂不另立块处理。
- v3.12：块 P 已执行，写入执行记录和 advisory（重启后 Step0 模型为空）。
- v3.11：新增块 P（patch 稳定编号与同步：编号永久不变、不复用；只显示一个名字；Navigator 可重命名），排在 A1 之前。A1 按用户意见修订：悬停时不显示信息；已勾选的小块可以用 × 删除、双击重命名；可修改规则文件。
- v3.10：V0 已执行，新增 7.10.8，记录执行结果、实测数据、执行中的发现（Cellpose 原地改写输入；conda 和 pip 同名包；子进程工作目录）和输入归属表。
- v3.9：块 V 按用户裁定，由「每个引擎一个环境」改为「一个环境 `fusion_mesmer`，每个引擎一个子进程」；记录这个环境的实际改动和核验结果；写入经用户审定的 V0 范围（导出清单、照清单临时重建后删除、模型清单与断网验证、子进程原型放进仓库 `seg_runner/`、输入归属表）。
- v3.8：按审核意见收口。R13 改为「按相同规则参与构造」；「只定标一次」改为「应用侧不额外拉伸，每个引擎只执行一套标准预处理」；Mesmer nuclei 的第二个通道为 0，并记录 Step2 现在给的是 fusion；多余定标改为「新流程不再调用」，旧参数保持原语义；搬迁和新行为分开验收；7.11.4 写明「读取区域相同」的定义，输入数组按方法构造后再比较；几处过强的说法改为待验证。
- v3.7：用户裁定 Mesmer 的膜通道用 Fusion、CLAHE 保留。写定 Mesmer 的输入为 `[fusion 核通道, fusion]`，并列出 Step1 的两处修改：膜通道要 ÷ 65535，核通道要改为带权重的 fusion 核通道。新界面只提供 Fusion 当膜通道（已裁定）。
- v3.6：R11 按用户裁定修订：保留模型的自动定标，按局部做，每条路径严格只做一次；I0–I3 作废；7.11.5 改写为定标规则和逐处排查表；验收第 2 条改为「自动定标只做一次」。
- v3.5：新增用户裁定 R13（权重在两边都生效）和 R14（一套轮子、方法模块化），新增 7.12 架构大纲。7.11.2 更正 Mesmer 两层定标的事实描述，补充膜通道为空时输入全为 0。用户对「是否关闭模型内部归一化」有新的考虑，I1、I2 **重新开放**，改为统一的 I0 决策。
- v3.4：按审核意见修订 7.11：I1、I2、H1 已裁定；I3 改写（Mesmer 有三步局部预处理）；新增 N1（纯核输入的核权重和量化）；写定 H2；更正细胞和核的配对规则，以及 Step2 实际生效的归属路径；验收拆成两条。
- v3.3：新增用户裁定 R10–R12（通道排法、全局亮度、Step1 HALO）；新增 7.11 模型输入契约；按讨论意见修正 7.10.4 和 7.10.5（终态登记、引擎身份与实际设备分开）。

---

## 一、目标

把 Step1 的预分割从「一次只能搜一种方法的参数网格 + patch × 参数的格子结果」改为：

1. **上半部 Patches**
   - 已有 patch 以紧凑网格列出，默认全选，可以取消。
   - 没有 patch 时，提示去画，或按指定数量和长宽**随机生成**。
2. **下半部 Methods**
   - 初始为空，用 `+` 弹窗逐个添加「方法 + 参数列表」。
   - 每个方法成为一个块，可以查看、可以删除。
   - 多方法、多参数一次跑完。
3. **结果**
   - 参考 step5_v8 的 montage viewer：所有勾选的 patch 排进一张画布，用分隔线隔开，统一缩放和平移。
   - 所有参数组合的细胞 mask 和核 mask 叠加显示；每个组合各自有开关，另有全开/全关。
   - 线宽、线型、颜色可调。
   - 通道和 Fusion 的呈现由 Step1 的 Channels 栏控制。
4. **选定**：用户比较后选定**唯一一个方法 + 唯一一组参数**，Save 后进入全量分割。**Step2 实际执行的方法和参数必须就是这一组**（见块 E）。

## 二、用户裁定（2026-09-23，必须继承）

| # | 裁定 |
|---|---|
| R1 | 随机 patch：有 ROI 就在当前 ROI 内生成，没有就在组织内生成。**空白面积超过 40% 的候选直接丢弃并重新生成。** 生成结果要在 Tissue Navigator 可见，也就是成为 Step0 的正式 patch。 |
| R2 | HQ / HQ2 / CDS 只是**在新界面不可见**，后台代码和 Step2 兼容**全部保留**。<br>**2026-09-26 用户补充裁定：HQ / HQ2 / CDS 这类不经 Step1 交接的方法不再维护**（见第六节「用户裁定」）。 |
| R3 | 可以写成列表的参数：<br>• Cellpose：diameter、flow、cellprob<br>• StarDist：prob、nms、expand<br>• Mesmer：主要阈值<br>模型名这类参数只允许单值。**取消原来的两阶段流程**（Phase1 定直径 → Phase2 扫 flow × cellprob），改为每个方法对各参数列表取笛卡尔积。 |
| R4 | 任务数（勾选 patch 数 × 全部组合数）**超过 10 个时，开始前弹窗提示**；用户坚持就照常运行。**某个结果一出来就可以在 Step1 看**，不必等全部结束。 |
| R5 | 纯核方法只有核 mask，纯全细胞方法只有细胞 mask，expansion 类两种都有。**没有的那一种，开关置灰。** |
| R6 | 结果视图参考 step5_v8 的 montage viewer：所有 patch 都可以 zoom in/out，可以查看通道和分割情况。 |
| R7 | **只允许一个方法、一组唯一参数组合**进入最终分割。 |
| R8 | 执行层的模块级改动单独成块申请（块 C），批准后才动手。 |
| R9 | 同名方法：询问是否合并；合并时每个参数取两边取值的并集，单值参数冲突时让用户二选一。**不做展开后的过滤去重。**（审核第 2 条，用户裁定维持原方案。）**2026-09-24 用户修订**：选「不合并」时**保留新添加的块**，不再取消这次添加，因此同一方法可以有多个块；选「合并」时并入该方法的第一个块。另外 `Edit` 允许更换方法。 |
| R10 | （2026-09-23）Cellpose whole-cell 的模型输入，Step1 和 Step2 **统一为 `[fusion, fusion, DAPI]`**，并显式指定 `channel_axis=-1`。不再测量旧的两种通道排法哪个更好。fused.zarr 可以继续存 `[fusion, DAPI]` 两通道，送进模型前再复制 fusion。纯核方法按方法定义只用 DAPI。 |
| R11 | （2026-09-23，**当日修订**，以修订版为准）亮度分两层：①用户手调的显示窗口（min/max/gamma）和 fusion 权重，这是用户的设置，**不算自动定标**，在全局上生效，随运行快照冻结；②模型的**自动定标**：**保留，按局部进行**，也就是在当前送进模型的那张图上计算。应用侧**不再额外做任何自动拉伸**，每个引擎只执行**一套**明确列出的标准预处理流程，而且只执行一次（见 7.11.5）。同一个细胞在不同 patch 或切块里定标后**可能不同，幅度尚未实测**，用户**接受**。（原版要求「全局固定、禁止局部估计」，已被本修订取代。） |
| R12 | （2026-09-23）Step1 采用和 Step2 相同的 **HALO** 做法：在 patch 四周多读一圈参与计算，推理和后处理都在带 HALO 的区域上完成，然后只保留和统计中央的 patch。 |
| R13 | （2026-09-23；v3.8 按审核意见改写措辞）fusion 的通道窗口（min/max/gamma）和权重（通道权重、组权重、核权重），在 Step1 和 Step2 都必须**按相同规则参与 fusion 输入的构造**。这**不等于**「调了权重，mask 就一定会变」：局部定标可能把整体亮度拉回去（7.11.5「已知的后果」），验收时不能这样要求。Step1 纯核方法绕过 fusion、直接读 DAPI，是**设计错误**，要改正。N1 据此定为 (a)：两边都读 fusion 的核通道，核权重为 0 时拒绝运行。 |
| R14 | （2026-09-23）**一套轮子**：Step1 和 Step2 能共用的组件一律共用，尽量不为同一功能造不同的轮子。分割方法**模块化**，Step1 和 Step2 只是调用方法模块的基座，便于维护。大纲见 7.12。 |

同时继续遵守 `AGENTS.md`、`UI_SURFACE_RULES.md`、`docs/P0_SCOPE_RULES.md`。和本计划最相关的几条：
- 不擅自增加可见界面。
- 科研数据和 committed 边界要保护：任务只在 committed fusion snapshot 上运行，结果带 `fusion_settings_hash`，Save 时校验。
- 测试只写合成项目。
- 自动化测试通过不等于真机验收。

**设计选择与裁定分开标注。** 下文凡是标为「设计选择」的内容，都不是用户裁定，可以在 A0 或对应块开工时再讨论。包括：
- 随机种子固定；
- 新 patch 不重叠；
- 尝试次数上限；
- 结果身份字段；
- 默认线型和颜色。

## 三、现状（重设计要替换或绕开的部分）

以下行号均以 `c9f80df` 为准。

- **方法注册表**：`utils/segmentation_config.py:23-314`，共 11 个方法。按 R2，新界面只列其中 8 个：
  - Cellpose ×3：whole-cell、nuclei、nuclei + expansion
  - StarDist ×2：nuclei、nuclei + expansion
  - Mesmer ×3：whole-cell、nuclei、nuclear-guided
- **界面**：`ui/step0/search_ctrl.py` 的 `SearchCtrlPanel` 只有一个方法下拉框。真正的参数网格只有 Cellpose Phase 2，由 `_run_p2` 取笛卡尔积。
- **执行**：`_launch_worker` 起一个 `multiprocessing.Process`，按**第一个任务**的方法决定执行目标；结果通过 `mp.Queue` 回传，由 `ui/main_window.py:6532` 一带的轮询逻辑直接读取队列里的 `masks`。
  - 每个任务只回传一张 label mask。
  - HQ 系列和 Mesmer nuclear-guided 算出的核 mask 被丢掉。
  - **Cellpose / StarDist expansion 在 `workers/cellpose_worker.py:584` 和 `:715` 用 `expand_labels` 的结果直接覆盖了核 mask，扩张前的核标签没有保留。**
- **结果**：`ui/step0/result_grid.py` 的 `ResultGridPanel`，行 = patch、列 = 参数组合。区分列的 `_pkey` 只认 Cellpose 参数。
- **交接**：`_save` 调用 `save_segmentation_params` 写出参数文件和 `segmentation_params_index.json`，Step2 的 `load_step1_active_params` 再把它们填进 Step2 自己的控件。
  - **Step2 的运行入口（`ui/step2_page.py:2268`）调用的是 `get_seg_config()`，读的是 Step2 自己的控件**，不是 Step1 写出的文件本身。
- **ROI**：ROI 记录带 `polygon_fullres`（手绘多边形，level-0 坐标）；Step1 的 GPU 多边形裁切已经在用它。
- **组织掩膜**：初查没有找到现成可复用的组织掩膜，只找到背景校正内部用的 Otsu。**A0 要确认这一点。**
- **patch**：用 level-0 坐标 `(y0, y1, x0, x1)`，编号按位置排；数量一变，全部预览历史都会被清空。patch 由 Step0 的几何发布通道写出。

**必须修掉的现有缺陷**（新设计下一定会暴露）：
1. 每到达一个结果就会把它设为当前参数，于是用户还没选，Save 就解锁了。
2. 同一个 patch 的多个组合写进同一个 npz 文件，互相覆盖。
3. 结果列的 key 在非 Cellpose 方法之间会撞。
4. 预览结果和参数写到两个不同的目录。

## 四、设计

### 4.1 页面结构

Step1 左侧的 `Method & Parameters` 标签页改为上下两部分：

```
┌ Patches ──────────────────────────────────────────┐
│ [✓P1][✓P2][✓P3][✓P4][ P5][✓P6] …（紧凑网格，自动换行）│
│ [全选] [全不选]            [随机生成…]  已选 5/6      │
└───────────────────────────────────────────────────┘
┌ Methods ──────────────────────────────────────────┐
│ ┌Cellpose whole-cell ──────────── 6 组合 [查看][×]┐ │
│ └ diameter 30 · flow 0.2,0.4 · cellprob -1,0,0.5 ┘ │
│ ┌StarDist nuclei ─────────────── 2 组合 [查看][×]┐  │
│ [+]                                                │
│ [保存方案] [加载方案…]      总任务：5×8 = 40  [Run] [Stop] │
└───────────────────────────────────────────────────┘
```

- **Patch 块**：每个 patch 一个可勾选的小块，颜色沿用 `PATCH_COLORS`；鼠标悬停显示尺寸和坐标；默认全选。没有 patch 时显示提示，并给出随机生成入口（A2）。
- **方法块**：显示方法名、各参数的取值摘要和组合数。「查看」重新打开弹窗编辑，「×」删除。
- **`+` 弹窗**：
  - 方法下拉框只列 8 个方法；
  - 可列表参数用逗号分隔，其他参数只接受单值；
  - 默认值来自方法注册表，逐项校验，并实时显示本方法的组合数；
  - Save 保存并关闭，Cancel 放弃。
- **同名方法**：按 R9 处理。
- **总任务数**：实时显示；超过 10 个时，Run 之前先弹窗确认（R4）。
- **运行前置**：fusion 草稿未保存时拒绝运行，沿用 `_require_committed_fusion_settings`。
- **旧界面**：原来的 Phase1/Phase2 控件在块 E 退场，代码保留，清理另行申请。

### 4.2 随机生成 patch（R1，块 A2）

- **输入**：数量 N、宽 W、高 H（level-0 像素）。
- **ROI 约束**：
  - 有 ROI 时，ROI 的 **bbox 只用来抽候选位置**；
  - 每个候选 patch 必须**完整落在真实 ROI 多边形（`polygon_fullres`）之内**；凹进去的部分、多边形外的部分都不能生成 patch。
  - 没有多边形的 ROI 按矩形处理。
- **组织和空白**：「空白超过 40%」的计算方式在 A0 定稿。
  - **不能**把 DAPI 阈值分出的核像素直接当成组织面积，否则细胞之间的间隙会被算成空白。
  - A0 先核查有没有现成的组织掩膜可以复用；没有的话，再定一个在 overview 层级计算的组织掩膜，例如对平滑后的信号做阈值，再做形态学闭运算和填洞，把核间隙也算作组织。然后在合成图和真实切片上各验证一次。
- **设计选择（不是裁定）**：
  - 新 patch 之间、以及新 patch 和已有 patch 之间不重叠；
  - 随机种子默认固定，并写进生成记录；
  - 最多尝试 `N × 200` 次，凑不够就如实提示「只找到 k 个」，不放宽标准。
- **写入**：通过 Step0 已有的 patch 写入和发布通道，和手画 patch 走同一条路径，所以 Tissue Navigator、Step0、Step1 都能看到。不另建第二套 patch 模型。

### 4.3 方案（method plan）的保存和加载

- **文件**：与最终参数分开存放。
  - `<step1_dir>/segmentation_search_plans/plan_<YYYYmmdd_HHMMSS>.json`
  - `<step1_dir>/segmentation_search_plans/index.json`
- **内容**：`{version, created_at, source_identity, methods: [{method, params}], selected_patches: [bbox...]}`。
- **加载**：列出本项目的历史方案。已经不存在的 patch 自动忽略，并提示忽略了几个。
- 保存和加载都**不触发任何计算**。

### 4.4 结果记录、运行快照与运行中编辑规则（A0 定稿，C 实施）

**结果记录**：每个任务一条，字段如下。不新建复杂的身份框架，复用已有的来源身份和 committed snapshot。
- `source`：复用 `_handoff_identity()` 或 provider 的 `source_identity`，说明结果属于哪张切片、哪个 Step0 发布版本。
- `run_id`：一次 Run 一个。
- `patch_bbox`：level-0 坐标。在同一来源下，它就是 patch 的稳定标识；界面上的 P 编号只是显示用的。
- `method` 和规范化后的 `params`，由此得到组合 ID。
- `fusion_settings_hash`：取启动时 committed snapshot 的值。
- `status`，三者之一：
  - `ok`（其中 `cells = 0` 表示成功但零细胞）；
  - `failed`（附错误信息）；
  - 该方法本身没有某种 mask 时，那种 mask 记为 `not_produced`。
- 两张 mask 各自的文件路径和细胞数。

**运行快照规则**（正常操作下）：
- 点 Run 时，冻结以下内容作为这次运行的快照：
  - patch 列表（bbox）；
  - 方案，即方法和组合；
  - committed fusion snapshot。
- 正在运行的任务**始终使用启动时的快照**。运行中如果有人增删 patch、编辑方法块、重新保存 Fusion Settings：
  - 已经派发的任务照常跑完；
  - 新的设置只作用于下一次 Run。
- **迟到的结果**按 `patch_bbox` 和 `run_id` 归位，**不按当前的 P 编号**挂到别的 patch 上。
  - 来源或 fusion hash 和当前不一致的结果，标为过期，照常显示但不能被选为最终。
- **任何结果都不会自动成为最终选择。**

### 4.5 各方法的输出表（A0 定稿，C 实施）

| 方法 | 细胞 mask | 核 mask | C 需要做的 |
|---|---|---|---|
| Cellpose whole-cell | ✓ | —（置灰） | 无 |
| Cellpose nuclei | — | ✓ | 无 |
| Cellpose nuclei + expansion | ✓（扩张后） | ✓（**扩张前**） | 在 `expand_labels` 之前保留核标签 |
| StarDist nuclei | — | ✓ | 无 |
| StarDist nuclei + expansion | ✓（扩张后） | ✓（**扩张前**） | 同上 |
| Mesmer whole-cell | ✓ | A0 核查 Mesmer 是否同时给出核输出 | 视核查结果 |
| Mesmer nuclei | — | ✓ | 无 |
| Mesmer nuclear-guided | ✓ | ✓ | 回传目前被丢弃的 `nuclei_mask` |

每个格子都要能区分三种状态：「该方法没有这种输出」（开关置灰）、「成功但零细胞」、「运行失败」。

### 4.6 结果视图：montage（R5、R6，块 D）

借鉴 step5_v8 montage viewer 的核心做法，在 pyqtgraph 上实现：

- **一张画布、一个相机**：所有勾选的 patch 按行装箱排进同一个 `ViewBox`，patch 之间留固定间隙并画分隔线，统一缩放和平移。
  - 双击或按 F 适配全部。
  - 点击某个 patch 只选中它，视图不移动。
  - 保留一张布局表，记录每个 patch 在画布上的矩形。
  - 每个 patch 左上角有一个 P 编号标签，作为独立图层。
- **显示供给**：块 D 在施工前必须先在 A0 写明以下三点，否则执行时很容易以「复用合成路径」为名扩改 Viewer。
  - **复用哪个接口**：初步候选是 Step1 现有的 patch 合成路径（Overlay/Fusion，跟随模式按钮、Channels 勾选、Intensity 和 fusion 权重）。它具体从哪一层读像素、按缩放选哪一级金字塔、能不能不经过整张切片的 viewer 直接取 patch，都要在 A0 查实。
  - **缓存归谁**：patch 底图缓存归结果视图自己所有，有容量上限，不借用、不扩展 viewer/scheduler 的缓存。
  - **何时释放**：切换数据集、离开 Step1、patch 被取消勾选、新的一次 Run 开始时释放。
  - 如果必须改 viewer、scheduler 或缓存层，停下申请。
- **mask 图层**：每个（组合 × 细胞/核）是一个图层。
  - **先验证矢量方案**：用 cosmetic `QPen` 画轮廓，线宽按屏幕像素计，原生支持实线、虚线和颜色。
  - 路径的颗粒度（每个 patch 一条、每个图层一条，或者分块）**不预先锁定**，由实测决定。
  - **栅格方案不是自动等价的回退**：膨胀出来的线宽会随缩放变化，也不天然支持虚线。
  - 性能不达标时，先报告具体瓶颈（细胞数、路径数、帧时间）和可选的功能取舍，由用户裁定。
- **控制栏**：每个组合一行，包括细胞开关、核开关（按 4.5 置灰）、颜色、线宽、线型、状态和进度（如 `3/5 patch`、失败数）；另有全开/全关，细胞和核分开控制。
  - 默认值（设计选择）：细胞轮廓实线、核轮廓虚线，同一组合共用一种颜色。
- **渐进显示（R4）**：每个任务一完成，它的轮廓立即加入对应图层。

### 4.7 选定 → 全量分割（R7）

- **选定资格**（设计选择，A0 请用户确认）：
  - 一个组合的所有 patch 任务都已结束，并且至少有一个 `ok`，才能被选为最终；
  - 有失败的 patch 要在控制栏标出；
  - 过期结果（4.4）不能选。
- **基本约束随块 C 落地，不拖到最后**：
  - 没选组合时，Save 保持禁用；
  - 结果到达不会自动选中；
  - hash 不一致时拒绝 Save。
- **写出格式**：与今天同构，即 `normalize_segmentation_config` 加 `save_segmentation_params`。
- **交接目标**：从 Step1 Save 进入 Step2，**不做任何额外编辑直接运行**时，Step2 实际提交给执行器的方法和参数（`get_seg_config()` 的返回值）必须和所选组合一致。
  - 如果不一致，就是本计划交接目标的缺口，在块 E 里修复，不能记成 advisory。
  - 修复只限于让 Step2 正确装载所选参数，不重做 Step2 的参数界面。

## 五、分块、白名单与验收门

- 每块单独启动，真机验收通过后才进入下一块。
- 所有测试从 `/tmp` 启动，带 `-p no:cacheprovider --confcutdir=/`，只写合成项目。
- 每块结束时按附录的方法跑全量回归：新失败一律先和 HEAD 对比；**本块改动涉及的路径上的失败，不能用基线来豁免。**

### 块 A0 — 契约准备（只做复核和设计定稿，不改生产代码）
- **产出**：在本文档中补齐以下内容，供用户确认：
  1. 8 个方法的参数表：哪些可以写列表、类型、范围、默认值，包括 Mesmer 的「主要阈值」具体是哪几个；
  2. 8 个方法的输出表（4.5），包括 Mesmer whole-cell 是否有核输出；
  3. 任务格式和结果记录格式（4.4）、结果文件布局；
  4. 来源绑定：复用哪个身份，以及运行快照和运行中编辑的规则（4.4）；
  5. 组织掩膜：有无现成可复用的，以及「空白 40%」的计算方式；ROI 多边形包含判定的实现位置；
  6. montage 的显示供给：复用哪个接口、缓存归属、释放时机（4.6）；
  7. 选定资格规则（4.7）；
  8. Step2 交接的实测：在合成项目上从 Step1 Save 进入 Step2，记录 `get_seg_config()` 和所选参数是否一致。
- **白名单**：只写本文档，以及 scratchpad 里的只读诊断脚本。
- **验收**：用户逐条确认。

### 块 P — patch 稳定编号与同步（用户裁定 2026-09-24；排在 A1 之前；**跨模块，须单独启动**）

**用户裁定**：
- **P-1 编号稳定**：删掉 P2 后，P3 及以后的编号**不变**。新 patch 的编号是「历史上用过的最大号 + 1」，**不回填空号**，已删除的号永不复用。例如有 P1、P2、P3，删掉 P3 再新增，新的 patch 叫 **P4**。随机生成的 patch 也遵守这条规则。
- **P-2 显示**：每个 patch 只显示**一个名字**，默认就是它的 `Px`，不附加尺寸、坐标或其他任何内容。重命名之后，显示的就是新名字（这是对用户「只显示 Px」的理解，按单一名字执行；用户如有不同意见，另行更正）。内部始终用永久编号来识别 patch。
- **P-3 同步与编辑**：以下各处显示的 patch 编号和名字必须一致：Tissue Navigator / Tissue Preview、Step0、Step1 viewer 顶部的 patch 选择器、Method & Parameters 里的 Patches 栏。Tissue Navigator 可以移动、调整大小、删除和**重命名** patch；重命名的方式是在 patch 列表中**双击**，和现在 ROI 的改名方式一样。

**现状**（2026-09-24 核查）：
- 代码里**没有**任何稳定的 patch 编号，所有 `P{i+1}` 都是按列表位置临时算出来的：
  - `overview_panel.py:2663/2724`；
  - `step0_page.py:6413/9079/9113/11237`；
  - `main_window.py:4594-4646/3039/3075/3131`；
  - `step1_5_bg_page.py:536`。
- Step0、Navigator、Step1 之间**只传坐标**。`geometry_committed` 携带的名字在 `main_window.py:2068-2101` 被丢掉了。
- 判断 patch 是否变化**只比较坐标**（`core/step0_handoff.py:369/445/525`），所以只改名字不会被发布出去。
- Step1 会先按 ROI 的 bbox 过滤 patch，再按位置编号（`main_window.py:4548`），导致**现在** Step0 的 P3 在 Step1 可能显示为 P2。
- Navigator 编辑后，要等 Step0 保存过至少一次才会发布到 Step1（`step0_handoff.py:516`）。这条行为保持不变。

**做法**：
- **数据**：每个 patch 记录增加 `id`（整数，永久不变）和 `name`（默认 `P{id}`）。patch 配置里另记 `next_patch_id`。
- **旧数据迁移**：没有 `id` 的旧记录，第一次读取时按原来的顺序补上 1…n，这样和用户以前看到的编号一致。
- **变化比较**：改为比较 `(id, name, bbox)` 三项。
- **Step1**：接收 `id` 和 `name`。工具栏按钮、Patch 菜单、结果历史（`_seg_preview_history`）、session 里的 `selected_patch`，都改为按 `id` 来记。
  - 按下标索引的缓存（通道缓存、加载状态等）保持现在的失效规则，只影响性能，不影响正确性。
- **Navigator 重命名**：在 Step0 的 patch 列表中双击改名，名字不能为空，也不能和其他 patch 重复。
- **不改**：分割 worker 的 npz 命名（`cellpose_worker.py:491`、`mesmer_worker.py:162`），以及旧的结果网格。这两处会在块 C 和块 E 里整体重写。

**白名单**（启动时再确认一次）：
- `ui/step0/overview_panel.py`
- `ui/step0/step0_page.py`
- `ui/step0/roi_context_model.py`
- `core/step0_handoff.py`
- `ui/main_window.py`：只改 patch 接收、显示和历史相关的部分
- `ui/step1_5_bg_page.py`：只改按钮显示
- 测试：
  - 新增 `tests/test_patch_stable_ids.py`；
  - 更新按位置写死 `P1..Pn` 的现有测试：`test_step1_patch_selector_menu.py`、`test_step0_step1_surface_details.py`、`test_step0_channel_conditioning.py`（`:300/:473`）、`test_step1_geometry_sync.py`、`test_step1_dataset_switch.py`、`test_tissue_navigator_popup.py:452` 等。每条修改都在报告里逐条说明。

**验收门**：
- 删掉中间的 patch 后，其余 patch 的编号和名字都不变；删掉最大号后再新增，编号顺延，不复用。
- 重命名之后，Navigator、Step0 画布和列表、Step1 工具栏和菜单、Patches 栏同时更新，并且写进磁盘、能被发布出去。
- 旧项目读取后的编号和原来一致。
- 在 Step1 打开 Navigator 做的编辑，Step1 能收到，编号不错位（包括 ROI 过滤之后）。
- 全量回归；真机验收。

**风险与回退**：
- 数据格式只增加字段，旧文件可以读取，旧代码读取新文件时会忽略新字段。
- 可以按文件回退。

**执行记录**（2026-09-24 启动，待用户验收）：
- **用户补充裁定**：重命名后显示用户起的名字，不必再是 Px。内部永久编号不变。
- **做法**：
  - `ui/step0/roi_context_model.py` 新增 `Patch` 类型。它仍然是 `(y0, y1, x0, x1)` 这个 4 元组，现有代码照常拆包、转换和比较；在此基础上多带 `id` 和 `name`，所以能原样经过 `patches_changed`、Navigator 弹窗（`tissue_navigator_popup.py` 不用改）、Step0 列表和 handoff。
  - 编号统一由模型的 `allocate_patch_id` 分配，只增不减。换数据集时重置；绑定到已发布的项目时，从 manifest 的 `next_patch_id` 取起点。旧 manifest 没有这个字段，就取 `n_patches + 1`。
- **Step0**：
  - 画布标签、信息行、提示、patch 列表、按钮和菜单都显示名字；
  - 按钮通过 `patch_index` 属性来匹配，不再按文字匹配；
  - 在 patch 列表中**双击可以重命名**，名字不能为空、不能重复，改名走 `patches_changed` 通道发布；
  - 写入磁盘的记录带上 `id` 和 `name`。
- **handoff**：
  - `patch_identities` 按 `(id, name, bbox)` 比较，所以只改名字也会发布；
  - manifest 带上 `next_patch_id`，全量 Save 和只改几何的发布都写，而且这个值只增不减。
- **Step1**：
  - 从 Step0 提交、磁盘、session 读入 patch 时都保留 `id` 和 `name`；
  - ROI 过滤之后编号不重排；
  - 工具栏、菜单、状态文字都显示名字；
  - 历史记录（`_seg_preview_history`）和 session 的 `selected_patch` 改为按编号记，键写成 `P{id}`，和旧 session 兼容；
  - 删除一个 patch 时，只清掉被删掉的或矩形变了的那几个 patch 的历史，其余保留。按下标索引的缓存仍用原来的失效规则。
- **Step1.5**：按钮显示名字，按下标匹配。
- **Step0 patch 列表的行格式**：保持原来的 `名字  ROI  [HxWpx]`，只把 `P{idx+1}` 换成了名字。P-2 说的「只显示一个名字」，这里理解为指 patch 标签本身；列表这一行要不要去掉 ROI 和尺寸，**请用户确认**。
- **测试**：
  - `tests/test_patch_stable_ids.py` 共 11 条。
  - 在 HEAD 导出树上运行会报收集错误。
  - 另做了两处反向注入，对应的测试都会失败：handoff 只比较 bbox；ROI 过滤时丢掉 id。
  - 与 patch 相关的现有模块全部通过，只剩附录基线里原有的 7 条失败。
- **全量回归**（2026-09-24，171 个模块，每个模块单独一个进程）：
  - 结果：3757 passed / 16 failed / 1 skipped。回归期间代码冻结，结束后逐个核对哈希，都没有变化。
  - 16 条失败和附录基线的清单**逐条相同**。
  - 其中 `test_step0_channel_conditioning` 落在本块改过的 Step0 patch 路径上，所以不能直接拿基线豁免，另外做了核对：
    - `test_dapi_lazy_loads_like_a_marker` 的断言信息和上一轮不同：上一轮是 `[] == ['DAPI']`，这一轮是 `None is not None`；
    - 在 HEAD 导出树上把整个模块跑两次、单条跑三次，都是 `None is not None`，而且其中一次整模块运行还多了一条已知的偶发失败；
    - 当前代码的表现和 HEAD 完全相同。
    - 结论：这条用例的断言信息本来就随运行状态变化，**不是本块引入的**。
  - 其余 5 个有失败的模块，断言信息和上一轮逐条相同。
- **Advisory**（本块之前就存在，**未实测**）：
  - Step0 重启后不会从磁盘把 patch 读回模型：`_roi_model` 只会通过 Step0 自己的编辑填充。
  - 所以重启之后，如果直接在 Step1 打开 Navigator 编辑，Navigator 看到的可能是一个空模型，发布出去的几何可能会覆盖磁盘上原有的 patch。
  - 块 P 只保证编号不冲突，也就是 `next_patch_id` 从 manifest 取起点。这个问题本身需要另外核实，并单独立块处理。

### 块 A1 — Patches 区（已有 patch 的勾选；依赖块 P；用户 2026-09-24 修订）
- **内容**：
  - 在 Method & Parameters 页的**顶部**放一个 Patches 栏，旧控件原样留在它下面，到块 E 再退场。
  - 每个 patch 是一个可勾选的小块，显示它的名字（P-2），边框颜色用 `PATCH_COLORS`。
  - **鼠标悬停时不显示任何信息**（用户裁定）。
  - 小块自动换行，最多显示 3 行，超出时在栏内滚动。
  - 下方一行：`Select all`、`Select none`、已选 k/n。
  - 默认全选，新 patch 也默认勾选；勾选状态**按 patch 编号保留**。
  - 没有 patch 时，显示提示 `No patches yet — draw them in Step0 or the Tissue Navigator`。
- **编辑**（用户裁定）：
  - **已勾选的**小块右上角有一个小 `×`，点击即删除该 patch；
  - **双击**小块可以重命名。
  - 这两个操作走的是和 Navigator 编辑**同一条**发布通道（`_reconcile_roi_edit` → `_persist_geometry_edit`），不另建一条路径。所以编辑结果会同步到 Navigator、Step0 和 Step1 工具栏。
- **勾选结果目前不接入任何计算**，要到块 C 才接上；也不写进 session。
- **规则文件**：用户同意在 `UI_SURFACE_RULES.md` 第 4 节补充 Patches 栏的描述。
- **白名单**：
  - 新文件 `ui/step1_presegmentation/__init__.py`、`ui/step1_presegmentation/patches_panel.py`（控件本身和自动换行布局）；
  - `ui/main_window.py`：Method & Parameters 页的装配处（约 `:1176-1203`）、`_on_patches`（约 `:4710`），以及删除和重命名接到发布通道的接线；
  - `UI_SURFACE_RULES.md` 第 4 节；
  - 测试：新增 `tests/test_step1_patches_panel.py`。
- **验收门**：
  - 布局正确，能自动换行，高度有上限；
  - **不会抬高左栏的最小宽度**：最小宽度不超过一个小块加上边距；
  - 默认全选；全选、全不选、逐个取消后，勾选集合都正确；
  - patch 增删之后，按编号保留勾选状态；
  - 没有 patch 时显示提示；
  - 点击 `×` 删除、双击重命名之后，Navigator、Step0、Step1 工具栏都同步更新；
  - 新测试先在 HEAD 导出树上跑，确认会失败；
  - 全量回归；真机验收。

**A1 执行记录**（2026-09-24 启动，待用户真机验收）：
- **范围内的用户裁定**：
  - Navigator 的 patch 列表每行只显示名字，去掉 ROI 和尺寸，所以本块改动了 `ui/step0/step0_page.py`。
  - 块 P 列出的 advisory「重启后 Step0 模型为空，可能覆盖 patch」：用户**接受这个风险，暂不另立块处理**，原因是当前使用的是测试数据。
- **实现**：
  - 新增 `ui/step1_presegmentation/patches_panel.py`，内容是 `PatchesPanel`、`PatchTile` 和 `FlowLayout`：
    - `FlowLayout` 的最小宽度等于一个 tile 的宽度，内容自动换行；
    - 高度随行数增长，最多 3 行，超过后在内部滚动；
    - 条带纵向不拉伸，多出的空间留给下面的方法控件；
    - 计数标签不计入最小宽度；
    - 双击时，把勾选状态恢复成本组点击开始之前的状态。
  - `ui/step0/step0_page.py`：新增公开的 `delete_patch(pid)` 和 `rename_patch(pid, name)`，都经由画布的 `patches_changed` 发布；列表行改为只显示名字。
  - `ui/main_window.py`：在 Method & Parameters 页的顶部装配这个条带；在 `_on_patches` 里把 patch 同步过去；把 `×` 和重命名接到 Step0。
  - `UI_SURFACE_RULES.md` 第 4 节新增两条：Patches 条带、patch 名称规则。
- **测试**（`tests/test_step1_patches_panel.py`，13 条）：
  - 在 HEAD 导出树上运行，报收集错误。
  - 五处反向注入都会让对应的测试失败：
    - FlowLayout 按整行宽度要最小宽度；
    - 双击时不恢复勾选状态；
    - 列表行重新带上 ROI 和尺寸；
    - 条带纵向可以被拉伸；
    - 计数标签被弹性空白挤掉。
  - 其中「双击」这条，第一版测试用的是 `QTest.mouseDClick`，它会多发一次按下事件，把缺陷掩盖了，反向注入时测试仍然通过。已改成手动发送 Qt 5 真实的事件序列：按下、松开、双击、松开。
  - 另外两条屏幕布局问题（条带被拉伸、计数被挤成宽度 0）是在离屏 `grab()` 截图里发现的，之前的自动测试没有覆盖。已补上窗口显示后的实测门：`test_on_screen_...`。截图是离屏渲染的，**不是物理屏幕截图**。
- **全量回归**（2026-09-24，共 172 个模块，每个模块单独一个进程）：
  - 结果：3769 passed / 17 failed / 1 skipped。
  - 第一轮跑到中途，我发现布局问题并修改了代码，所以那一轮作废、中止，改完后重跑。重跑期间代码冻结，结束后核对哈希一致。
  - 17 个失败中，16 条与附录基线**逐条相同**。
  - 第 17 条是 `test_overview_patch_editing.py::test_an_edit_on_the_thumbnail_reaches_the_patch_list`：
    - 原因：它断言列表行包含尺寸 `1000x1000px`，这与用户「列表只显示名字」的裁定冲突。
    - 处理：坐标断言（`page.patches`）保持不变；列表断言改为「只有一行，且等于 `P1`」。
    - 验证：改后这个模块 37 条全部通过；在 HEAD 导出树上，新断言会失败（旧代码的列表行带 ROI 和尺寸），说明它确实在检验新规则。
    - 只动了这一个测试文件，其他模块不受影响，所以没有重跑整个回归。
- **左栏宽度**：
  - 在离屏窗口中对比：显示条带和隐藏条带时，左侧标签页的最小宽度相同。第一版比隐藏时宽了 3 px，原因是计数标签计入了最小宽度，已经修正。
  - 已有测试门：patch 数量多时，最小宽度不变。

### 块 A2 — 随机生成 patch（A0 的组织和 ROI 判定确认之后）
- **白名单**：
  - 新文件 `core/random_patches.py`（纯函数，不依赖界面）；
  - `patches_panel.py` 中的入口和弹窗；
  - 调用 Step0 **现有的** patch 写入接口。只调用、不改接口时不算扩围；需要改接口就停下申请。
  - 测试：新增 `tests/test_random_patches.py`。
- **验收门**：
  - 每个 patch 完整落在 ROI 多边形内（包括凹多边形的反例）；
  - 空白比例按 A0 定义不超过 40%（合成组织图上验证，核之间有间隙的组织不被误判为空白）；
  - 没有 ROI 时落在组织内；
  - 设计选择项：不重叠、同一种子结果相同、凑不够时如实提示；
  - Tissue Navigator 能看到新 patch；
  - 真机上生成一次。

**A2 执行记录**（2026-09-24 启动，待用户真机验收）：
- **第一阶段实测**（真实切片，只读）：
  - 16× 层：耗时 2.0 s，峰值内存 0.7 GB；
  - 4× 层：耗时 52.8 s，峰值内存 6.3 GB；
  - 256、512、1024 px 三种候选，16× 层与 4× 层的取舍判断有 99.7% 一致，空白比例平均相差 0.2–0.3%；
  - 空白比例几乎全部集中在两端：约 97% 的候选空白比例不到 10% 或超过 90%，所以 40% 这个阈值放在哪里影响很小；
  - 接受率约 71%；
  - 这张切片只有 7 个洞，全部是小洞，都被填成组织，因此「大腔隙保持为空白」这条规则在这张切片上**没有得到检验**。
- **用户裁定**（2026-09-24）：
  - 掩膜层级改为**固定选用「最接近 16× 且不比它更细」的现有层**，**取代 T2 原来按 patch 短边选层的规则**；
  - 参数（level-0 单位）：σ 128、闭运算半径 256、最小组织块 819 200 px²、填洞上限 2 048 000 px²，都是经验值；
  - 后台线程：批准；
  - 随机种子：固定；
  - 生成记录：只打印到终端。
- **实现**：
  - `core/random_patches.py`：纯函数，包括选层、组织掩膜（距离变换实现精确圆盘闭运算）、面积加权的积分图空白比例、凹多边形的精确包含判定（四角都在多边形内，且没有任何边接触矩形轮廓）、带重试上限的生成过程。
  - `ui/step1_presegmentation/random_job.py`：一次性后台线程，用 tifffile 只读取出层级信息，再用 `read_region_lowres` 读核通道。
  - Patches 条带新增单独一行 `Random…`，点开是数量、宽、高对话框。放在单独一行是为了不把左栏撑宽。
  - `OverviewPanel.add_patch_rects` 和 `Step0Page.add_patches`：批量加入，只发布一次，编号接着现有的顺延。
  - `ui/main_window.py`：负责接线、结束后的处理，以及数量不够时如实提示。
  - `UI_SURFACE_RULES.md`：补充 `Random…` 的说明。
- **真实切片端到端**（只读，`run_generation`）：没有 ROI 时生成 10 个 512 px 的 patch，耗时 2.46 s，抽了 14 次候选就全部找到，层级 16×。
- **测试**（`tests/test_random_patches.py`，11 条）：
  - 在 HEAD 导出树上会报收集错误；
  - 四处反向注入都会让对应的测试变红：多边形只判四个角、掩膜不做平滑和闭运算、不检查重叠、数量不够时不提示；
  - 相关的现有模块（Patches 条带、稳定编号、overview 编辑、UI 契约）全部通过。
- 离屏 `grab()` 截图显示 `Random…` 单独占一行，布局正常。这是离屏渲染，**不是物理屏幕截图**。
- **全量回归**（2026-09-24，173 个模块，每个模块单独一个进程）：3781 passed / 16 failed / 1 skipped。16 个失败与附录基线的清单**逐条相同**，没有新增失败。回归期间代码冻结，结束后核对哈希一致。

**A2 后续小改**（2026-09-24，用户裁定；A2 已提交为 `44b1eb2`）：
- **`Delete` 按钮**：放在 `Random…` 右边，只删除**勾选**的 patch。配合 `Select all` 就是全部删除，只勾几个就只删那几个。不单独设 "Delete all" 按钮。
  - 没有勾选时按钮是灰的；删除前要确认一次；一次删除多个 patch 只算一次编辑、只发布一次（`OverviewPanel.remove_patches`、`Step0Page.delete_patches`）。
  - 删除前的确认框是我加的设计选择，用户没有专门表态；如果不需要，可以去掉。
- **标签页改名**：`Method & Parameters` 改为 **`Pre-segmentation`**。代码、`UI_SURFACE_RULES.md`、检查标签名的 3 个测试同步更新。
  - 历史计划 `docs/step1_rework_plan.md` 保持不动。
  - 用户指南 `docs/user_guide.md` 和 `docs/用户指南.md` 暂不改：它们描述的还是旧的 Phase1/Phase2 界面，等块 E 旧界面退场时一起更新。
- **测试**：
  - `test_step1_patches_panel.py` 增加到 16 条；
  - 两处反向注入都会失败：Delete 删除全部 patch、按 patch 逐个发布；
  - 相关模块全部通过。
- **全量回归**（2026-09-24，173 个模块）：3784 passed / 16 failed / 1 skipped，16 条失败与基线**逐条相同**。
- **离屏截图**：左栏最小宽度仍是 191。离屏窗口的左栏只有 257 px，两个标签名都会被省略显示，这是标签栏允许的行为，而且旧名字更长。真机上的显示请用户验收时确认。

**标签页改名 Fusion**（2026-09-24，用户裁定；上一批改动已提交为 `5269553`）：
- 左栏第一个标签页从 `Channels` 改名为 **`Fusion`**。原因：标签页里的框标题 `Channels` 与 Step0 保持一致，标签页再叫 Channels 就重复了。这个页面定义的是 fusion（勾选、权重、Save Fusion Settings），与 `Pre-segmentation` 放在一起，正好对应 Step1 标题的前后两半。
- 框标题 `Channels` 和 Step0 都**不改**。
- 按用户要求只跑相关测试，**不做全量回归**：`test_step1_layout_block_a`、`test_ui_surface_contract`、`test_step1_channel_panel`、`test_step1_patches_panel`、`test_global_channel_dock`、`test_step0_step1_surface_details` 全部通过。

**Step1 patch 按钮配色**（2026-09-24，用户裁定；上一批改动已提交为 `0cf9f04`）：
- **原因**：Step1 viewer 顶部的 patch 按钮按预载状态上色，只有 ready 才显示 patch 色，其余状态是灰色；而整张切片 viewer 从不预载，所以按钮一直是灰的。
- **改为**：
  - 按钮一律用 patch 自己的颜色，样式与 Step0 按钮**完全相同**。
  - 样式由同一个 `patch_button_qss(color)`（`ui/step0/step0_page.py`）生成，Step0 和 Step1 共用，符合 R14「一套轮子」。
  - 加载状态只由标签上的 ⟳ / ✓ / ✗ 表示。
- **取色方式**：与 Step0 按钮、Pre-segmentation 条带、Navigator 画布一样，按 patch 在列表中的位置取 `PATCH_COLORS`，所以四处颜色一致。
  - **已按用户裁定修改（2026-09-24）**：改为按 patch 的**永久编号**取色，`P<n>` 用第 n-1 号颜色，所以删掉 P2 之后 P3 仍保持原来的颜色。从没编辑过的列表，编号与位置一致，颜色和改之前完全相同。
    - 取色统一用 `patch_color` / `patch_color_for_id`（`ui/step0/roi_context_model.py`）。
    - 使用这两个函数的地方：Navigator 画布与 Step0 画布（标签框和信息行）、Step0 按钮、Step1 按钮、Pre-segmentation tile、Step1.5 按钮。
    - 旧的结果网格 `result_grid.py` 仍按行号取色，它会在块 E 退场。
    - 新增 `test_a_patch_keeps_its_colour_in_every_view_when_another_is_deleted`。把取色反向注入为按位置后，这条测试会失败。
    - 相关模块全部通过。`test_step0_channel_conditioning` 第一次运行多出 1 条失败，重跑整个模块后只剩基线的 6 条；相关测试单独跑 3 次都通过，属于附录记录过的偶发失败。
    - 按用户要求**没有做全量回归**。
- **测试**：
  - 新增 `test_every_patch_button_wears_its_patch_colour_in_every_state`：四种状态下，按钮样式都等于 Step0 样式，且与条带 tile 同色。
  - 在旧代码上，idle、loading、error 三种状态会失败。
  - 相关模块通过：patch selector、Step0/Step1 surface、patches 条带、稳定编号、几何同步。`test_step0_channel_conditioning` 的 6 条失败与基线相同。

### 块 B — Methods 区、`+` 弹窗、方案的保存和加载
- **白名单**：
  - 新文件 `ui/step1_presegmentation/method_editor.py`、`method_blocks.py`、`plan_store.py`；
  - `ui/main_window.py` 中的装配；
  - 隐藏 HQ/HQ2/CDS 通过「界面可见方法列表」实现，**不改** `SEGMENTATION_METHODS`；
  - 测试：新增 `tests/test_step1_method_plan.py`。
- **验收门**：
  - 参数列表的解析和校验（非法值、空值、重复值），按 A0 的参数表；
  - 组合数和总任务数正确；
  - R9 的合并（并集规则、单值冲突二选一、不合并则取消）；
  - 方案保存后再加载能完整还原；
  - 下拉框里没有 HQ/HQ2/CDS，但加载含这些方法的旧参数不报错；
  - 这一块不触发任何计算。

**B 执行记录**（2026-09-24 启动；用户验收总体通过，验收后修改已人工复验通过）：
- **用户裁定**：
  - 本块不放 Run / Stop，留给块 C（界面上不放点了没反应的按钮）；
  - 合并时，单值参数的冲突在同一个弹窗里逐项二选一。
- **实现**：
  - `utils/segmentation_param_schema.py`：纯数据加纯函数，内容是 8 个方法的参数表（按 7.1），以及解析与校验、笛卡尔积、`combo_id`、R9 合并、摘要。**不改** `SEGMENTATION_METHODS`。
  - `ui/step1_presegmentation/method_editor.py`：`+` 弹窗，只列 8 个方法。没有 DeepCell 时，Mesmer 置灰并写明原因。每个字段在输入时就校验，红字表示被拒绝，黄字只提示「去掉了重复值」、不阻止保存；组合数实时显示。
  - `method_blocks.py`：
    - 方法块：标题可换行，组合数单独一行，下面是摘要，右侧是 `Edit` 和 `×`；
    - 总任务数；
    - R9 合并：选 Yes 取并集，单值冲突用 `ConflictDialog` 选择；选 No 取消这次添加（验收后已改为保留成单独一块，见下）。
  - `plan_store.py`：方案文件放在 `segmentation_search_plans/`，另有 `index.json`，都是原子写入。patch 按永久编号记录，加载时忽略已经不存在的 patch 和隐藏的方法，并告诉用户忽略了几个。
  - `patches_panel.set_selected_ids`：加载方案时恢复勾选状态。
  - `ui/main_window.py`：负责装配，以及 Save plan、Load plan 的处理。
  - `UI_SURFACE_RULES.md` 补充了 Methods 部分的说明。
- **离屏截图发现并修正的问题**（`grab()` 是离屏渲染，**不是物理屏幕截图**）：
  - 方法块标题和「Methods」字号过大，而且标题和组合数互相挤压、被截断：已统一字号，组合数移到单独一行；
  - 按钮默认最小宽度为 80，导致左栏变宽（191→212，由宽度门发现）：`+` 单独放一行，`Edit` 设了最小宽度，组合数让出宽度；
  - 弹窗里的 `Method:` 标签宽度为 0（两个 QFormLayout 各自计算标签列宽）：改成普通的横向行；
  - 提示文字里出现了内部编号「(plan P2)」：改成直白的说明。
- **测试**：
  - `tests/test_step1_method_plan.py` 共 20 条；
  - 在 HEAD 导出树上会报收集错误；
  - 五处反向注入都会让测试变红：合并时不取并集、选 No 仍然合并、小数位不校验、不忽略已不存在的 patch、Mesmer 不置灰；
  - A1 的页面顺序测试改为 Patches → Methods → 旧控件。
- 按 R4，任务数超过 10 时的提醒放在块 C 点 Run 的时候做。
- **全量回归**（2026-09-24，共 174 个模块）：3808 passed / 17 failed / 1 skipped。16 条与基线相同。另外 1 条是 `test_step0_compare_tiles::test_hot_requests_never_outrank_the_foreground`（「no foreground request」，属于时序问题）：把整个模块单独重跑，当前代码 3/3、HEAD 3/3 都是 190 条全部通过，而且块 B 没有碰 Step0 的分块读取，判定为**偶发**失败，不是本块引入的。
- **验收后修改**（2026-09-24，用户「验收总体通过」后提出三点）：
  - 同名合并选 No：**保留**这张新的方法卡，单独成块，不再取消（R9 已同步改写）；
  - `Edit`：弹窗里的方法下拉框可以更换方法；换成已有的方法时，同样询问是否合并，选 Yes 就并进已有的那块，本块去掉；
  - `Save plan`：保存**全部** patch（编号、名称、坐标和大小），以及哪些被勾选；
  - `Load plan`：先问是否恢复 patch。当前没有 patch 就直接恢复；有的话再问「覆盖当前 patch」还是「共存」。共存时，编号已存在的 patch 跳过；名称重复的改名，并告诉用户改了哪些。恢复的 patch 保留原编号（按此复验通过；`overview_panel.restore_patch_records`，一次编辑、一次发布），勾选等 Step0 发布后再加上。
  - 测试增加到 24 条（含覆盖、共存、不恢复三种加载流程，都在已发布 handoff 的窗口里跑）；七处反向注入都会变红：选 No 仍取消、Edit 锁住方法、只存勾选的 patch、覆盖不删旧 patch、重名不改、编号重复、勾选不等发布；相关 7 个模块全部通过。
  - **修改后全量回归**（2026-09-24，174 个模块，运行期间代码冻结并核对哈希）：3813 passed / 16 failed / 1 skipped，16 条与基线逐条相同，无新增失败。

### 块 C — 执行层与结果接入（**模块级，须单独批准**）
- **必要性**：
  - 现有 worker 按第一个任务决定执行目标，一次运行不能混合多种方法；
  - 核 mask 被丢弃，expansion 方法的核标签被覆盖，满足不了 R5；
  - 结果文件互相覆盖；
  - 结果到达会自动变成当前参数。
- **拟改文件和范围**：
  - **发送端**
    - `workers/cellpose_worker.py`：
      - 按每个任务的方法分派；
      - 按 4.5 回传细胞和核两种 mask，expansion 方法在扩张前保留核标签；
      - 结果写到唯一路径；
      - 队列只传结果记录（路径和摘要），不传整张 mask。
    - `workers/mesmer_worker.py`：同上，另外回传 nuclear-guided 的核 mask。
  - **调度**
    - `ui/main_window.py` 的 `_launch_worker`：按方法族把任务分组派发，按 4.4 冻结运行快照。
  - **接收端**：`ui/main_window.py`，以下几处都纳入：
    - 结果轮询（`:6532` 一带），改为按结果记录读取；
    - 进度和结束的处理；
    - 旧结果视图的适配，在块 D 上线前维持可用；
    - `_record_segmentation_preview_result` 不再自动设置当前参数。
  - **基本选定约束**（4.7）：未选则 Save 禁用，hash 不一致则拒绝。
  - 可选的第二步（**另行申请**）：同一个 patch 的核分割阶段和 patch 读取在组合之间复用。
- **不改**：
  - 各方法的算法实现（cellpose/stardist/mesmer 的调用方式、HQ 系列的实现）；
  - fusion 合成算子；
  - 调度器和缓存基础设施。
- **风险**：进程和显存占用随方法族切换而变化。缓解办法是 mask 落盘、队列只传记录。
- **回退**：发送端、调度、接收端各自独立，可以按文件 revert；新旧结果路径并存。
- **验收门**：
  - 合成 patch 上三种方法族混合的任务都正确分派；
  - 每个任务的细胞和核 mask 与单独运行该方法的结果逐像素一致，expansion 方法的核 mask 等于扩张前的结果；
  - 三种状态可以区分：没有这种输出、成功但零细胞、失败；
  - 结果文件不互相覆盖；
  - 运行中增删 patch、编辑方法块、重新保存 Fusion，都不影响正在运行的任务；迟到的结果按 bbox 归位；
  - 结果不会自动成为选中项，未选组合时 Save 保持禁用；
  - 停止和关闭时没有残留进程；
  - 所有结果带正确的来源和 hash；
  - 真机上跑一个小方案（不超过 10 个任务）。

**C 申请与批准**（2026-09-24 用户批准；和 V1 合并执行，新流程走 `seg_runner` 子进程，旧 worker 不改）：
- **分四步，每步单独测试、验收、提交**：
  1. **抽共用零件（纯搬迁，不改行为）**：把 Step2 生效的归属与重编号内联代码（`segment_merge_worker.py:2538-2575`）原样抽成 `core/label_ownership.py`；门：同一张 label 图、同一个 own_bbox 上，与内联代码的保留集合和新标签完全一致。Step2 改为调用放到 V2。
  2. **补全引擎**：`seg_runner` 加 expansion（保留扩张前的核）、Mesmer `postprocess_mask`、nuclear-guided 回传核；Mesmer 模型路径改为读模型清单，缺失时明确报错、不退回本机绝对路径；门：与同环境直接调用逐像素相同。
  3. **后台运行流程（无界面）**：按 7.3、7.4 冻结 `run.json`（patch、组合、fusion 快照、来源和 pixel_key、HALO，HALO 与 Step2 overlap 同值，默认 200）；按 7.10 的归属表构造输入；引擎串行；按 7.3 原子发布；Stop 记 cancelled、崩溃记 failed、关闭无残留；核权重为 0 或 DAPI 无 committed 窗口时拒绝运行。
  4. **界面接入**：Methods 区总任务数下方放 Run / Stop 和进度；超过 10 个任务先提示（R4）；结果列表（每个组合一行：状态、细胞数、失败数、「选用」），过期可看不可选（7.7）；修掉结果到达自动设为当前参数（现有缺陷 1）；选定后按现有格式写参数文件。
- **用户裁定（a）**：块 C 不做图像结果视图，只有结果列表；轮廓显示留给块 D。旧结果网格不改造。
- **新增可见界面已授权**：Run、Stop、进度、结果列表、选用按钮，都在 Pre-segmentation 页。
- **拟新增**：`core/label_ownership.py`、`core/preseg_input.py`、`core/preseg_run.py`、`ui/step1_presegmentation/run_job.py`、`ui/step1_presegmentation/results_panel.py` 及测试。**拟修改**：`seg_runner/engines.py`、`runner.py`、`client.py`、`envs/fusion_mesmer/models.json`、`ui/main_window.py`、`method_blocks.py`、`UI_SURFACE_RULES.md`、本文档。
- **不改**：Step2 全部代码、`workers/cellpose_worker.py`、`workers/mesmer_worker.py`、fusion 算法、viewer / 调度器 / 缓存。
- **测试环境**：默认环境没有 deepcell，Mesmer 相关测试另用 `fusion_mesmer` 跑。

**C 执行记录**（2026-09-24 启动）：
- **第 1 步：抽共用零件（完成，待提交）**
  - `core/label_ownership.py`：`centroids`、`kept_labels`、`ownership_lut`、`apply_ownership`（共享标签的第二张 mask 走同一张 LUT，超出主输出最大标签的置 0），从 Step2 生效的内联代码原样搬出；Step2 没改。
  - 门：`tests/test_label_ownership.py` 让 **Step2 真实的 `_segment_one_zarr`** 在合成 fused.zarr 上跑（模型换成固定的标注函数），它写出的全局 mask 与用本模块逐块归属、按 Step2 同样方式粘贴的结果逐像素相同（4 种切块和 overlap）；质心与 `_centroids_vectorised` 逐元素相同。8 条测试；把半开区间改成闭区间，3 条变红。
- **第 2 步：补全引擎（完成，待提交）**
  - `seg_runner/engines.py`：
    - expansion 两个方法输出细胞和核：核是扩张前的预测，细胞是 `expand_labels(核, expand_distance)`，距离 0 时细胞等于核；
    - Mesmer：列表阈值按 P2 进入主输出的 `postprocess_kwargs_*`（whole-cell、nuclear-guided 的细胞；nuclei 的核），nuclear-guided 的副核用库默认值；`postprocess_min_size` 只作用于主输出，和现有 Step1 worker 一致；不传 `preprocess_kwargs`；
    - Mesmer 模型路径改为读模型清单（可被清单里写明的环境变量覆盖），逐个核对文件存在和大小，缺失或不符明确报错，不再退回写死的路径；
    - `postprocess_mask` 在 runner 里有一份拷贝（runner 不导入主程序），测试保证与 `utils/mesmer_utils.postprocess_mask` 相同，Step2 改用 runner（V2）后只剩一份。
  - `tests/test_seg_runner_engines.py` 共 10 条：两种 expansion 在子进程里与直接调用逐像素相同；阈值到达 `app.predict` 的 kwargs（不需要 DeepCell 的替身测试）；真实 Mesmer 上阈值改变了结果，且子进程与直接调用相同；模型清单的路径与核对。`fusion_test2` 20 passed / 3 skipped（Mesmer），`fusion_mesmer` 23 passed（含原 `test_seg_runner.py`）。四处反向注入都变红：核被扩张结果覆盖、阈值不传、副核也做后处理、清单路径失效时退回别处。
  - **与 P1 裁定的出入（2026-09-24 用户同意）**：P1 原写「块 C 在 `mesmer_utils.run_mesmer_prediction` 和 `mesmer_worker` 的 Step1、Step2 两条路径里透传阈值」。按本块批准的范围（不改 Step2、不改旧 worker），新流程的阈值在 runner 里透传；Step2 的透传随 Step2 改用同一个 runner（V2）一起做，旧路径不加。
- **第 3 步：后台运行流程（完成，待提交）**
  - `core/config_hash.py`：规范化哈希从 `MainWindow._step1_config_hash` 搬出，窗口改为调用它；测试用搬迁前算出的 6 个摘要把两者钉住。
  - `core/preseg_input.py`：读取范围（patch ± HALO，与分析区域取交集，不补边）、`fuse_fullres` 融合成 uint16、多边形置 0、÷65535、按方法组装输入（7.11.4 的表）；核通道没设、核权重为 0、核通道没有已确认的显示窗口时拒绝。
    - **实测发现**：`cv2.fillPoly` 在画布边缘会裁剪多边形，逐个窗口栅格化时边缘有少量像素和 Step2 不同（测试里 53 个像素）。改为 `RoiMask`：和 Step2 一样在整个 ROI bbox 上栅格化一次，存成按位压缩的数组，再按窗口切出；测试与 Step2 的 `_poly_mask` 逐像素相同。内存和 Step2 写 fused.zarr 时相同（短暂地每个 ROI 像素 1 字节）。
  - `core/preseg_run.py`：`run_id`、任务展开（组合 × patch）、`run.json`、结果记录（先 mask 后记录，原子替换）、`pixel_key`（结构化身份，7.4 的「必须变 / 必须不变」各项都有测试）、选定资格（7.7 的五条）和过期判断。
    - **设计选择**：两个方法块列出了同一个组合时，每个 patch 只跑一次（同样的方法、参数和像素，结果文件名也相同），按第一次出现的顺序。
  - `ui/step1_presegmentation/run_job.py`：普通线程，不依赖 Qt。引擎按 Cellpose → StarDist → Mesmer 串行；同一 patch 同一种输入只准备一次，各组合共用；每个任务收到结果后按共用归属函数保留中央区域（expansion 的核跟随细胞用同一张 LUT，nuclear-guided 各自归属、`paired: false`），裁成 patch 大小再发布；每个任务正好一条记录。Stop：正在跑和后面的任务记为已取消，后面的引擎不再启动；引擎起不来：它的任务记为失败并写明原因，其他引擎照常；patch 在分析区域外：只有这个 patch 失败。
  - `tests/test_preseg_run.py` 共 17 条，其中端到端用真实 StarDist 进程，与手工逐步计算逐像素相同。`fusion_test2`、`fusion_mesmer` 都 17 passed。另在 `fusion_mesmer` 里实跑 Cellpose whole-cell + Mesmer nuclear-guided（两个阈值）×2 个 patch：全部 ok，Cellpose 用 GPU、Mesmer 用 CPU，run 目录只剩 run.json、records、masks 和引擎日志。
  - 反向注入：HALO 置 0、跳过归属、expansion 不共用 LUT、Stop 不设停止标记（第一次没测出，已加强测试：Stop 后下一个引擎不得启动）、pixel_key 混入 patch 数、忽略已取消、Mesmer nuclei 第二通道不为 0，都会变红。
- **第 3 步已提交**：`3f179ca`。
- **第 4 步：界面接入（完成，待真机验收）**
  - `method_blocks.py`：总任务数下方加 `Run`、`Stop` 和进度行；运行中 Run 禁用、Stop 可用（不再静默返回）。
  - `results_panel.py`（新）：`Results` 标题、「正在使用」一行、每个组合一行（方法、参数、`k/n patches`、细胞数、失败 / 取消数、`out of date`、`Use` 按钮）；不能选时原因写在行里。
  - `ui/main_window.py`：
    - Run：要求已保存的 Fusion 设置，按 `check_fusion` 拒绝；超过 10 个任务先问（R4）；冻结 run（HALO 暂取 Step2 Tile Grid 的默认 overlap 200，与 Step2 设置本身绑定放到块 E）；运行用自己的 loader，在后台线程里；记录经 Qt 信号回到界面线程。
    - Use：按 7.7 判断；有失败时先问；选中后 `_p2_params` 是该组合自己的方法和参数，带 `fusion_settings_hash`、`pixel_key`、`preseg_run_id`、`combo_id`；Mesmer 另写 `normalize_input: false` 和 `threshold_target`。Save 写参数文件时不再混入旧面板的参数。
    - 过期：Step0 发布或保存新的 Fusion 设置后重新判断；选中的结果过期就取消选中、Save 重新禁用；Save 时再核对一次（记录或文件缺失、过期都拒绝）。
    - 现有缺陷 1 修掉：旧流程里结果到达不再成为当前参数；结束后的自动选中只选 Phase 1 的列（它只是把直径交给 Phase 2）。
    - 关闭窗口、切换数据集时结束运行，不留引擎进程。
  - `UI_SURFACE_RULES.md` 同步。离屏截图（`grab()`，**不是物理屏幕截图**）检查了布局：左栏宽度不变；`Results` 标题字号改成和 `Methods` 一致。
  - `tests/test_step1_preseg_run_ui.py` 共 9 条（真实 StarDist 进程）；八处反向注入都变红：结果到达自动成为参数、自动选中 Phase 2 列、过期不取消选中、Save 不再核对、超过 10 个不问、关窗不结束运行、运行中 Run 仍可点、结束后自动选用。
  - 布局顺序测试更新为 Patches → Methods → Results → 旧控件。
  - **全量回归**（2026-09-24，178 个模块，运行期间代码冻结并核对哈希）：3854 passed / 17 failed / 3 skipped。16 条与基线逐条相同；第 17 条是已知偶发的 `test_step0_compare_tiles::test_hot_requests_never_outrank_the_foreground`，整个模块单独重跑 3/3 全部通过。
  - **真机验收**：用户人工测试通过（2026-09-24）。
  - **验收后修改**（用户要求）：Step1 的 `Save Fusion settings` 和 `Save plan` 保存成功后弹窗说明（文件位置；方案里有几个方法、几个 patch）；失败时原有的警告不变；只有按钮会弹窗，代码内部的保存不弹。新增 1 条测试；相关 6 个模块通过；按新的测试规则不再跑全量。
- **测试规则（用户裁定，2026-09-24）**：每一步只跑相关测试；全量回归只在一个块收尾、或某一步大改共用文件时跑，并行 4 组。

### 块 D — montage 结果视图（包含显示供给）
- **白名单**：
  - 新文件 `ui/step1_presegmentation/montage_view.py`、`mask_layers.py`、patch 底图缓存（归结果视图所有）；
  - `ui/main_window.py` 中 Patch Results 标签页的装配和结果回调；
  - A0 定稿的复用接口，**只调用不修改**；需要改 viewer、scheduler 或缓存层就停下申请；
  - 测试：新增 `tests/test_step1_montage_view.py`。
- **验收门**：
  - 尺寸不一的 patch 布局和点击命中正确；
  - 统一缩放和平移，适配全部；
  - Channels、Intensity、Overlay/Fusion 调节后底图跟随；
  - 每个组合的细胞和核开关、全开/全关、颜色、线宽（屏幕像素恒定）、虚实线都生效；
  - 按 4.5 置灰；失败和零细胞标注正确；
  - 渐进显示：结果一到就出现；
  - 缓存按 4.6 释放；
  - 在真实切片上实测性能（patch 数、组合数、细胞数、帧时间）；不达标时先报告瓶颈和取舍，由用户裁定；
  - 画面证据在真实 X display 上取。

**D 申请与批准**（2026-09-24 用户批准）：
- **分三步，每步单独测试、验收、提交**：① 画布与底图（按行装箱布局、分隔线、P 编号、点击选中不移动、统一缩放平移、双击/F 适配；底图走 7.6 的复用接口，只调用不修改；按画布缩放选层级；读取和合成在 montage 自己的线程里）；② 轮廓图层与控制栏（每个组合一行：细胞/核开关、颜色、线宽、实线/虚线、状态与进度；全开/全关分细胞和核；按 4.5 置灰；失败和零细胞标注；默认细胞实线、核虚线、同组合同色、线宽按屏幕像素；矢量 cosmetic QPen，路径粒度按实测）；③ 真实切片只读实测性能（patch 数、组合数、细胞数、帧时间；不达标先报告瓶颈与取舍）和释放门、viewer 与 montage 同时读取的实测；最后一次全量回归（并行 4 组）。
- **用户裁定**：
  - 结果视图放在右侧**新标签页**「Pre-seg Results」，旧「Patch Results」不动，块 E 一起退场；
  - `Use` 只留在左侧结果列表；
  - 控制栏是标签页右侧**可收起的侧栏**（约 220 像素，默认展开），不做悬浮窗；行的顺序和名称与左侧结果列表一一对应。
- **授权项（AGENTS.md 第 4 条）**：两层缓存（通道块 1 GiB，合成结果 256 MiB，归 montage 所有）、一个 montage 工作线程（最新请求优先）、释放时机按 7.6（切换数据集或 pixel_key 变化、离开 Step1、关窗：线程退出、缓存清空、无迟到回调；取消勾选的 patch 只清它的；新的 Run 只清轮廓；Channels 或 Intensity 变化只清合成层）。
- **显示哪些 patch**：有运行时显示该次运行冻结的 patch；还没运行时显示当前勾选的 patch（只有底图）。
- **拟新增**：`ui/step1_presegmentation/montage_view.py`、`mask_layers.py`、`montage_supply.py` 及测试。**拟修改**：`ui/main_window.py`（装配、结果回调、释放时机）、`UI_SURFACE_RULES.md`、本文档。**不改**：viewer、scheduler、已有缓存层、Step2、分割流程；需要改就停下申请。
- **画面证据**：只用离屏截图，不在用户的 DISPLAY 上弹测试窗口，最终画面以用户真机验收为准。

**D 执行记录**（2026-09-24 启动）：
- **第 1 步：画布与底图（完成，2026-09-25 真机验收通过）**
  - `montage_view.py`：`pack_rows` 按行装箱（顺序和尺寸不变、互不重叠、间隙为中位边长的 6%，至少 16）；布局表与命中判定（间隙不属于任何 patch）；画布单位是 level-0 像素，所以之后的 level-0 mask 和任何层级的底图天然对齐；分隔框和选中框用 cosmetic 笔；P 名称是独立的 `TextItem`，不随缩放；双击或 F 适配全部；点击只选中、视图不动；按画布缩放用 `pick_display_level` 选层级（120 ms 去抖）。
  - `montage_supply.py`：`build_spec` 由页面在界面线程取好再交出；`spec_channels` 调 `overlay_channels` / `fusion_channels`；像素调 Step1 provider 的 `read_region`（不经过 scheduler）；合成调 `step1_compose.compose`；两层按字节计的 LRU（通道块 1 GiB、合成结果 256 MiB）；一个工作线程，新的请求取代所有还没开始的；过时的代计数结果不报告；缺显示窗口的通道经 `missing` 交给页面调用 `request_mapping_seed`，缺窗口时的半成品不进缓存；`close()` 结束线程、清空两层缓存，之后不再报告。
  - `ui/main_window.py`：右侧新增「Pre-seg Results」标签页；没有运行时显示勾选的 patch，Run 开始后显示那次运行冻结的 patch；只在 Step1、标签页可见时合成；勾选、颜色、显示窗口、fusion 草稿、Overlay/Fusion 模式变化后只清合成层再合成；取消勾选的 patch 清掉它的缓存；`pixel_key` 变化清空全部；离开 Step1、切换数据集、关闭窗口都结束线程并清空缓存。
  - 右侧标签页的约定测试（4 个模块）和 `UI_SURFACE_RULES.md` 改为三个标签页。
  - `tests/test_step1_montage_view.py` 共 12 条：底图与用同一条链手工合成的结果逐像素相同（Overlay 和 Fusion 各一，跨过 NaN 边界）；改 Intensity 或模式不重新读盘；释放规则；关闭后没有迟到的结果。七处反向注入都变红（通道缓存失效、取消勾选不清、关闭不清、层级恒为 0、离开 Step1 不释放、不跟随设置变化、点击移动视图）；其中「不跟随设置变化」第一次没测出——测试里重新适配后的层级检查顺带重画了一次，已让测试先等它结束。
  - 离屏截图（**不是物理屏幕截图**）：三个大小不一的 patch 按行排开，名称在左上角，选中的 patch 有黄框。
  - **真机验收通过后的修改**（用户 2026-09-24）：
    - 画布上方加 `Overlay` / `Fusion` 两个按钮，和 Viewer 的两个按钮是同一个命令，互相同步；
    - 双击某个 patch 让它单独占满画布，再双击（或双击间隙）回到全部；F 仍是全部；
    - **卡顿**（启用新通道、调 Intensity）：合成数据上复现不出（8 个 1024 px patch，读图加合成 66 ms，界面线程最长停顿 1 ms）；真实切片只读实测原始通道读 4 个通道：level 0 为 256 ms，level 1 为 11 ms，不是瓶颈；嫌疑是 corrected 通道没有粗层平面时要从 level 0 归约，以及同一时刻其他界面部分的重算。已做：按屏幕精度合成（`stride`，从层级数据里隔点取样，不比屏幕更细）；只合成屏幕上看得见的 patch，近中心的先做，其余进入画面时再做；每次请求结束在终端打印一行 `[Montage] … reads … ms, compose … ms, … total … ms`，真机复测时据此定位。
    - 第二个工作线程：实测 8 个 patch 合成 52 ms → 27 ms；**用户 2026-09-25 同意**，已改为两个。
  - **真机复测（2026-09-25）**：用户贴出的日志是调 Intensity 时的，每次 4 个 patch（level 0、stride 1），0 次读盘，合成 100–160 ms。用户指出 Intensity 要等拖动停顿才画——这是我加的 80 ms 去抖计时器：每次变化都重新计时，拖动时永远不触发。改为**一帧接一帧**：没有正在画的帧就立刻画；有的话记下「有新变化」，这一帧一上屏就按最新设置画下一帧；拖动期间按两倍 stride（像素数四分之一）画，静止 150 ms 后画一次全精度；拖动时不打印耗时行。合成数据上模拟一秒拖动：拖动期间持续出帧（约每 17 ms 一帧），第一帧 0.2 s 内，全精度在停下后 1 s 内。
  - 画布关掉自动范围、图像不参与范围计算——**这不是卡顿的原因**（反向注入去掉它测试照样通过，`setRange` 本身已关掉自动范围），只是保护；之前测试里拖动中出现的全精度请求，实为测试窗口尺寸还在变化引起的层级检查，已让测试先等窗口稳定。
  - 反向注入：恢复去抖（两条变红）、帧上屏后不补画最新设置（一条变红）。
  - **启用新通道仍卡**：用户的日志里没有那一次的数据；等用户贴出启用新通道时的 `[Montage]` 行再定位（嫌疑：corrected 通道没有粗层平面时从 level 0 归约）。
  - 如果 Intensity 在真机上仍不够流畅，下一步是改用 Step1 viewer 的 GPU 显示层（`Step1GpuLayer`，只调用不修改）：原始像素一次上传到显卡，改 Intensity 只改参数、每帧重画。它是覆盖在画布上的独立 GL 窗口，会盖住 patch 名称、选中框和第 2 步的轮廓，需要另外设计叠放，并新增显存缓存，**要先征得用户同意**。
  - **真机复测（2026-09-25）**：卡顿解决，用户验收通过；但启用/关闭通道和调 Intensity 时整个画面细微抖动。原因（我的修复引入）：① 拖动时半精度、停下后全精度，来回切换，清晰度跳变；② 半精度图边长向上取整后拉伸回原尺寸，边长不能被 2 整除时比例差一点点，最远处偏差约 1 像素，每次切换都错位。Step0/Step1 不抖，是因为 viewer 用 GPU 显示层按屏幕精度从同一份原始数据每帧重画，没有过渡。
  - **用户裁定（2026-09-25）：直接复用 Step1 的成像机制**，全部同意：
    - 复用 `Step1GpuLayer`（着色器、Overlay/Fusion 合成）和 `build_spec`，只调用不修改；viewer 的 `Step1GpuBinding` 按整张切片的调度器设计，不适合多个 patch 的画布，montage 自己供原始平面（只供数据，不合成）；画布坐标就是显示层的世界坐标。
    - 新增 montage 自己的显存缓存，上限 512 MiB（与 viewer 相同），离开 Step1、切换数据集、关闭窗口时释放。
    - GL 显示层盖在画布最上面，patch 名称、选中框和第 2 步的轮廓画在它上面一层透明的绘制层里，鼠标穿透到下面的画布。
    - 显卡不可用时退回 CPU 合成（去掉半精度过渡，不抖但慢），终端打印原因，与 viewer 的处理一致。
    - 去掉 CPU 合成结果缓存和半精度过渡；原始通道缓存保留，只存裁切后的数据。
  - **超大 patch 与 512 MiB**（用户问）：与 viewer 同样两层——每个 patch 一块整块的粗平面（边长有上限），再加一块只覆盖屏幕可见部分的细平面；每次提交前按字节算总量，超过预算就这一帧改用更粗的层级，所以用量取决于屏幕和通道数，与 patch 大小无关；显示层自己也在超过上限时拒绝提交，不会溢出。
  - **GPU 复用的实施**：
    - `montage_gpu.py`（新）：`display_snapshot`（与 viewer mount 的 `_gpu_display_snapshot` 是同一份拷贝，测试逐项相等）；`plan_planes`（粗平面整块、边长上限 1024 并在总量超过 128 MiB 时减半；细平面只覆盖可见部分，对齐到细层级的 256 像素块，总量超过 256 MiB 时这一帧退到更粗的层级）；平面在画布上的位置按它实际读到的像素范围算，不拉伸到 patch 边框；`read_key`（像素）与 `identity`（像素加画布位置）分开——patch 在画布上移动时 GPU 层不会报「同一身份不同几何」，也不重新读盘；`build_layer` 与 mount 一样当场强制初始化，失败给出原因。
    - `montage_supply.py`：新增按平面读取（只读、不合成），存进原有的 1 GiB 通道缓存；CPU 合成保留为退回路径。
    - `montage_view.py`：边框、名称、选中框从场景移到 `MontageOverlay`（盖在 GL 层上面的透明窗口，鼠标穿透，坐标经同一个 ViewBox 换算）。
    - `ui/main_window.py`：supply 建立时尝试 GPU 层，失败打印原因并用 CPU 画面；设置变化直接规划并提交（同步，一次提交）；平面到达时同一轮事件合并成一次提交；相机移动时每次都从显卡重画，细平面最多每 60 ms 重新规划一次；提交失败打印原因并退回 CPU；离开 Step1、切换数据集、关闭窗口时 `dispose` 掉 GL 名称和纹理。CPU 退回路径去掉半精度过渡。
    - 离屏测试可以用真实的 RTX 4090（EGL，不在用户的 DISPLAY 上弹窗）。`tests/test_step1_montage_view.py` 共 26 条：CPU 退回会说明原因；快照与 viewer 相同；超大 patch 的预算；平面精确落位；调 Intensity 时同步提交、不读盘、画面变化；勾选从未读过的通道时先读再画；叠放顺序；移动不报错不重读；释放。七处反向注入六处变红，没变红的一处是「设置变化时直接走 GPU 分支」——跳过它后另一条路径同样同步提交，行为相同。
    - 离屏截图（`grab()`，**不是物理屏幕截图**）：GPU 画面，名称、边框和黄色选中框在上面。

- **第 1 步已提交**：`4cdcaff`（2026-09-25）。
- **第 2 步：轮廓图层与控制（用户裁定 2026-09-25）**：
  - 控制**合并进左侧结果列表的每个组合框**，不做右侧侧栏：每个框新增一行「Cells / Nuclei 开关、颜色块、▾ 菜单（线宽 1 / 1.5 / 2 / 3 像素，细胞线、核线各自实线或虚线）」；Results 标题下加细胞、核各自的 All / None；方法没有的那种 mask 开关置灰；左栏宽度门继续把关。
  - 轮廓默认全关；按组合顺序用固定配色；缩得很小（细胞中位直径不足约 3 个屏幕像素）时只画边框，放大后自动出现。
  - 轮廓在 montage 已授权的两个工作线程里提取（不新增线程），画在 GL 层上面的透明层里（cosmetic 笔，线宽按屏幕像素）；失败的 patch 角上标 `failed`，成功但零细胞标 `0 cells`；新的 Run 只清轮廓，底图保留。
  - **执行记录（完成，2026-09-25 真机验收通过，含 Membrane / 核通道开关与按钮外观）**：
    - `mask_layers.py`（新）：`outlines`（按 `find_objects` 逐个标签取外轮廓，点在边界像素中心，给出细胞数和中位直径）；`qpath`（每个多边形闭合、互不相连，一条路径）；固定配色、线宽档、默认样式。
    - `montage_supply.py`：描轮廓任务排在读平面之后，在原有两个线程里做；新的 Run 清空轮廓；关闭时清空。
    - `results_panel.py`：每个框新增一行 Cells / Nuclei（按方法输出置灰）、颜色块（`QColorDialog`）、▾ 菜单（线宽、细胞线虚线、核线虚线）；标题下 Cells / Nuclei 的 All / None（置灰的不动）；组合多时框不再被压扁，改为可滚动（离屏截图发现：矮的标签页会把框压到不可读）。
    - `montage_view.py`：透明层按每个组合的样式画轮廓（cosmetic 笔、虚线），细胞中位直径不足 3 个屏幕像素时不画；`failed` / `0 cells` 标签；路径按布局缓存。**测试发现并修正**：`mapFromScene` 把坐标取整，用 (0,0)、(1,1) 推缩放时误差被放大，轮廓整体缩放偏了（最多约 15 像素）；改为浮点换算，缩放取视图两个对角。
    - `ui/main_window.py`：新的 Run 调 `_start_results_for_run`（框、清轮廓，底图保留）；记录到达时失败立刻标注，成功的交给后台描轮廓，描好即画；montage 没有 supply 时到达的记录在它出现后补上；框里的样式变化直接送到画布。
    - `tests/test_step1_montage_outlines.py` 共 10 条；七处反向注入六处变红，没变红的一处是把坐标换算改回取整——缩放已改用视图对角推算，取整只让边框和名称偏不到 1 像素。相关 9 个模块并行 4 组全部通过。
    - 离屏截图（**不是物理屏幕截图**）：GPU 底图上，细胞实线、核虚线同色，`failed` 标签在角上；左侧框的开关行、颜色块和 ▾；10 个组合时可滚动、每个框完整高度。
    - **真机验收通过后的修改**（用户 2026-09-25）：Fusion 模式下画布上方加 `Fusion signal` 和核通道（显示通道名，如 `DAPI`）两个开关，默认都开，只在 Fusion 模式显示；关掉哪个，结果视图这一帧就不画哪个——只改 `build_spec` 结果的一份拷贝（`groups` 置空 / `nucleus` 置为 `("", 0.0)`），Fusion 设置、分割数据和 Viewer 都不受影响（测试核对模型不变）；由 GPU 当场重画。新增 2 条测试，两处反向注入都变红。
    - **外观（用户 2026-09-25）**：montage 的 Overlay / Fusion 与 Viewer 的两个按钮用同一份样式（抽出为 `ui/step1_button_styles.MODE_BUTTON_QSS`，Viewer 改为引用它，字符串不变）；`Fusion signal` 改名 `Membrane`；Membrane / 核通道开关与模式按钮同形状、字号、边框和圆角，按下时分别亮成 Fusion 画面里该层的颜色（细胞质红、核蓝），弹起时暗底灰字——风格一致，但不与模式按钮重复。

- **第 2 步已提交**：`9cab8cc`（2026-09-25）。
- **第 3 步：真实切片实测与收尾（2026-09-25）**
  - 只读：`RawTileProvider` 读真实 OME-TIFF（59040×35520，29 通道，层级 1/4/16/64），脚本放在 scratchpad，不写真实项目，`cufile.log` 不变；GPU 为离屏 EGL 下的 RTX 4090。
  - **底图**（13 个 patch：8 个 1024 px、4 个 2048 px、1 个 10000 px；4 通道）：第一块平面 160 ms 到达，全部 300 ms；首次上传纹理的一次提交 184 ms；**拖 Intensity 50 次：每帧中位 1.9 ms、最慢 4.4 ms、不读盘**；切到 Fusion 4.9 ms；放大到 10000 px 的 patch 后平移：每帧中位 5.7 ms、最慢 12 ms。显存峰值 121 MiB / 512 MiB，内存平面缓存 156 MiB / 1 GiB。三个线程模拟 viewer 与 montage 同时读同一张切片：无错误。
  - **轮廓**（8 个 1024 px patch，每个 1521 个细胞，3 个组合 × 细胞加核 = 48 层、约 72000 个轮廓）：提取每个 mask 约 36 ms（后台）。**瓶颈**：① 路径在界面线程里生成，每条 18 ms，一次打开 48 层时首帧卡约 0.9 s；② 每帧重画 98 ms（全部实线也要 94 ms，虚线不是主因，负担是点数与数量）。按用户裁定（2026-09-25）做 ① ②：
    - ① 路径改在 montage 已授权的两个线程里生成（`request_path` / `path_ready`），按 patch 自己的像素保存，画时平移到画布位置——换位置不重建；
    - ② 按屏幕精度分档简化（`lod_bucket`：误差不超过半个屏幕像素；Douglas-Peucker），缩放后需要的那档还没好时先用最近的一档，好了自动换上；
    - 复测：一次打开 48 层时界面线程最长一帧 65 ms（原约 0.9 s）；显示全部时每帧 55 ms（原 98 ms）；放大到一个 patch 每帧 23 ms（原 28 ms）。这是最重的情形；平时开 1–2 个组合约 10–20 ms。③（移动时用快照）、④（GPU 画轮廓）暂不做。
  - 测试：`tests/test_step1_montage_outlines.py` 增至 13 条（简化误差、路径在后台线程生成、缩小时要更简的一档并先用最近的）；三处反向注入都变红。
  - **真机验收通过（2026-09-25）后的修改**：Results 标题下的 All / None 按钮改为 `Cells`、`Nuclei` 两个勾选框（用户裁定）：勾选＝所有框都显示这一种，不勾＝都不显示，默认不勾；各框不一致时显示半勾，从半勾点一下变为全部显示；没有任何组合能产生这种 mask 时置灰。新增 1 条测试，两处反向注入都变红。
  - 结果列表每个组合框加上与 Methods 方法块相同的灰色矩形边框（用户 2026-09-25）：边框样式抽成 `method_blocks.block_frame_qss`，两处共用；「In use」时该框边框为绿色。新增 1 条测试。
  - **块 D 验收通过（2026-09-25）**。
  - **分区（用户 2026-09-25，方案 A）**：Pre-segmentation 标签页的三部分各装进一个带标题的框——`Patches`（新加的标题）、`Methods`、`Results`——样式与下方旧控件的「Segmentation Method」框相同（`ui/step1_button_styles.SECTION_BOX_QSS`，测试核对与 `search_ctrl.py` 的字符串一致）；Methods、Results 原来写在里面的标题去掉；面板内边距相应缩小，每侧总边距不变，左栏宽度门通过。布局顺序测试改为检查三个框。
  - **块 D 收尾全量回归**（2026-09-25，180 个模块，并行 4 组，代码冻结并核对哈希与 HEAD）：3897 passed / 17 failed / 3 skipped。16 条与基线逐条相同；第 17 条 `test_step0_channel_conditioning::test_loaded_channel_switch_is_cache_hit`（基线记录的偶发项）：单独各跑 10 次，当前 8/10、块 D 之前的 `dcf6244` 同样 8/10 通过——既有的偶发失败，与块 D 无关（它测的是 Step0 通道调节，块 D 没有触及）。

### 块 E — 完整交接验收与旧界面退场
- **白名单**：
  - `ui/main_window.py` 的选定和 Save 相关状态；
  - `ui/step0/search_ctrl.py`、`ui/step0/result_grid.py` 只做「不再上屏」；
  - 如果 A0 或本块实测出 Step2 装载不一致，修复 `ui/step2_page.py` 中装载所选参数的部分，只限装载，不改 Step2 的界面；
  - 测试：更新 `tests/test_step1_fusion_settings_commit.py`、`tests/test_step1_to_step2_handoff.py`，新增端到端交接测试。
- **验收门**：
  - 从 Step1 Save 进入 Step2、不作任何编辑直接运行时，`get_seg_config()`（也就是实际提交给执行器的方法和参数）和所选组合**一致**，8 个方法各覆盖一次；
  - 选定资格规则（4.7）生效；
  - 旧的 Phase1/Phase2 控件不再上屏；
  - 用户在真机上完成一次「预分割 → 选定 → 全量分割」全流程。

**E 申请与批准**（2026-09-25 用户批准第 1–3 项）：
1. 旧的 Phase1/Phase2 面板（`self.search`）和右侧「Patch Results」标签页**不再显示、不删除**（与 R2 处理 HQ/HQ2/CDS 的方式一致）；
2. **Save 只认预分割「Use」选定的结果**（4.7「未选则 Save 禁用」）：旧面板和旧会话里的旧参数都不能解锁 Save，要重新预分割再选定；
3. 交接验收：真实窗口的 Use → `_save` → `_go_to_step2` → `get_seg_config()`，8 个方法各一次（只核对参数，不需要引擎，Mesmer 也覆盖）。
- 白名单：`ui/main_window.py`（旧面板和 Patch Results 的可见性、Save 的解锁条件和 `_save` 入口的拒绝）、`UI_SURFACE_RULES.md`、测试、本文档。`search_ctrl.py`、`result_grid.py` 不需要改（在容器层隐藏）；Step2 装载实测一致，`step2_page.py` 不需要改。

**E 执行记录**（2026-09-25，真机验收通过）：
- `ui/main_window.py`：新增 `_save_allowed()`（有 `Use` 选定的结果才为真），`_check_save_unlock`、`_unlock_ui` 和 `_save` 入口都只认它；`_save` 被拒时提示「Choose a pre-segmentation result first: Pre-segmentation tab → Results → Use.」。旧面板的滚动区 `setVisible(False)`；Patch Results 标签页 `setTabVisible(False)`；旧的 `_show_step1_patch_results_tab` 调用在标签页隐藏时不做任何事。
- 新增 `tests/test_step1_step2_handoff_e2e.py` 10 条：8 个方法走真实的 Use → `_save`（写出真实参数文件，fusion 作业开始前停下）→ `_go_to_step2`，Step2 不作编辑时 `get_seg_config()` 通过契约核对、没有不一致，`runner_params` 正好是所选组合加方法的固定规则；旧 Phase 2 参数不能解锁 Save；旧面板和 Patch Results 标签页不再显示、仍保留。
- 更新因旧界面退场而失效的 12 条测试：Save 相关的 guard 测试（未保存设置、显示映射提交、重绑、运行写入的快照）改为经「选定的结果」到达 Save，原有断言不变；「Phase 2 参数在别的设置上搜出来」那条改为断言 Save 直接拒绝；旧面板 Save 的文件钉住测试改为断言拒绝、不写文件；两条 Patch Results 标签页测试和一条 720p 布局测试改为断言标签页和旧面板不显示。
- 回归（Step1 全部 55 个模块 + 契约 + 界面约定，逐模块单独进程）：与已提交的 HEAD 逐条对比，没有新增失败。仍失败的都在 HEAD 上同样失败：`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`（字体差异）；`test_step1_montage_view.py` 在第 27 条后 Qt 异常中止（本机 WSL 的 GPU/EGL，HEAD 同样）；5 个 GPU 模块在本机不收集测试。
- **真机验收通过（用户 2026-09-25）**：「预分割 → 选定 → Save → Step2 全量分割」（Mesmer 除外，本机无模型）。
- 提交：`7ee98fc`。

### 块 L — Step1 Save 进度框与 Step2 布局（计划外；用户 2026-09-25 提出并批准）
- **L1**：Step1 的 Save 生成 fused.zarr 时，模态进度弹窗和页面底部的旧进度条同时出现。用户裁定只保留弹窗（它有 Cancel）；底部进度条保留但不再显示，它的文字改为打到终端（前缀 `[Step1-Fusion]`），完成和出错仍各有消息框。
- **L2**：Step2 的参数面板放到左栏，宽度与 Step0、Step1 的通道列共用一份（三页的分隔条联动）；Tile Status Overview 和进度放到右栏；Step2 不显示全局 Channels 组件（框和组件保留，只是隐藏）。
- **白名单**：`ui/main_window.py`（底部进度条的显示；通道列同步机制加入 Step2 的分隔条，同步的接线挪到 Step2 建好之后）、`ui/step2_page.py`（两栏摆放、Channels 框隐藏、参数行和提示文字的显示方式、改名）、`UI_SURFACE_RULES.md`、测试。不改 Step2 的运行逻辑、控件的取值和含义、Step0/Step1 的布局、Step3。
- **实施中的停止与裁定**（均为用户 2026-09-25 裁定）：
  - 实测 Step2 参数面板最窄 543 px，放不进共用列宽 → 停下申请。用户选「压缩」：参数行的标签按文字原宽显示、**绝不截断**，由后面的控件让出宽度（下拉框省略显示当前选项，展开时仍为完整列表；数值框变窄）。先试过把标签列缩到 110 px，离屏测出大多数标签会被截断，已放弃。
  - 刚启动时共用列宽只有约 300 px（1920 宽窗口；Step0 按自身内容定宽，不随窗口变化）。用户要求刚启动绝不能出现横向滚动条，并裁定改名：`Segmentation Index` → `Index`，`Parameter Source` → `Source`，`Index method` → `Method`，`Parameter version` → `Version`，Recovery 框标题 → `Recovery from .npy`（原标题放进鼠标提示）；去掉 Tile Grid 里不准确的 VRAM 估计；Output、Recovery 的说明文字自动换行。
  - 从 index 取参数时只显示一行 `Method:`（index 的方法）；Step2 自己的方法框隐藏但保留，仍保存实际应用的方法。
- **结果**（离屏）：面板最窄 257 px；刚启动时共用列宽为 271 / 300 / 401 px（1500 / 1920 / 2560 宽窗口），都没有横向滚动条；11 个方法下都没有被截断的标签。
- **测试**：`tests/test_step1_save_progress.py`（L1）、`tests/test_step2_layout.py`（L2，12 条）。相关 11 个模块 264 passed / 2 failed；这 2 条在已提交的 HEAD 上同样失败，不在附录的已有失败清单里，是这台新机器上的环境差异：`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`（逐像素几何，字体不同）、`test_step0_step1_display_isolation.py::test_step0_work_does_not_make_step1_load_or_redraw`。
- **提交**：L1 `1d14801`，L2 `2294bc7`。**真机验收通过（用户 2026-09-25）。**
- **Advisory**：
  - 用户主动 Stop 后，Step2 用标题为「Error」的 `QMessageBox.critical` 报告「Stopped by user.」（现有行为）；
  - 离屏 1500 宽窗口下 Step0 与 Step1 的通道列宽度不一致（365 vs 271 px），已提交的 HEAD 上一样，可能只是离屏现象；
  - 旧路径 ROI 模式中途 Stop 仍会登记成功（见 V2 第 3 步的 advisory）。

### 块 F — `Load weights` 只读 Step1 会话（计划外；用户 2026-09-25 报告并批准）
- **缺陷**（真机报告）：Step1 的 `Load weights` 选中 `step1_session.json` 后，通道勾选和权重都没有恢复；之后再勾选通道，viewer 只显示 DAPI；Save Fusion Settings 被拒绝。
- **原因**（已有问题，不是块 L 引入的）：`_load_weights_from_file` 按 `fusion_config.json` 的格式读取（最外层直接有 `groups`），而会话文件的权重放在 `fusion_config` 里面。代码不检查就调用 `apply_full_config`，装入一个没有任何组的草稿：所有 marker 移出 fusion，只剩 DAPI。之后勾选的通道只记了权重，没有组可加入，所以始终进不了 fusion；fusion 里没有 marker，Save 因此被拒绝。
- **用户裁定**：`Load weights` 只读 `step1_session.json`，减少复杂度；其他文件一律拒绝。
- **做法**：窗口新增 `load_weights_from_step1_session(path)`：按会话特有的字段识别会话文件（`fusion_draft`、`channel_visibility`、`channel_weights`、`patches`、`preview_mode`、`p2_params` 至少有一个；`fusion_config.json` 和 `step1_fusion_settings.json` 都没有）；先用 `fusion_domain.migrate_session` 检查会话里至少有一个当前切片的 marker 通道（兼容 `{members: [...]}` 和 `{channels: {...}}` 两种组格式）；再调用和 `Load Previous Step1 Session` 相同的两个函数（`_restore_step1_scientific_state` → `_apply_step1_display_state`），一次性恢复权重、参与、勾选、颜色、当前通道和显示模式；patch、路径、分割参数等字段不恢复。`config_panel._load_weights_from_file` 改为打开文件对话框（默认过滤 `step1_session.json`）后交给窗口，拒绝时弹出原因。
- **白名单**：`ui/step0/config_panel.py` 的 `_load_weights_from_file`、`ui/main_window.py` 新增的入口、`tests/test_step1_load_weights.py`。不改 `apply_full_config`、fusion 模型、会话加载、Save。
- **测试**：`tests/test_step1_load_weights.py` 6 条：同一个真实会话文件，`Load weights` 恢复的权重、参与、组和勾选与 `Load Previous Step1 Session` 的恢复路径完全相同；`fusion_config.json`、`step1_fusion_settings.json`、其他 JSON 被拒绝且状态不变；没有当前切片 marker 的会话被拒绝且状态不变；加载后新勾选的通道进入组、参与 fusion。相关 9 个模块 215 passed / 1 failed（`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`，按钮高 19 px 而非 20 px，HEAD 上同样失败，是这台机器的字体差异）。测试写的会话文件都在沙箱临时目录，没有写到 `~/fusion_data`。
- **真机验收通过（用户 2026-09-25）**。提交：`5bc65bc`。
- **Advisory**：Step1 目前写三个 JSON——`fusion_config.json`（`Save` 时写，Step3 从中读原始切片路径，Step2 通过 `step1_output` 拿到它的路径）、`step1_fusion_settings.json`（`Save Fusion Settings` 的已确认快照，预分割用它的 hash 判断是否过期）、`step1_session.json`（会话）。用户希望 Step1 只生成一个统一的 JSON；这会牵涉 Step2、Step3 和预分割的读取方，另立块处理。

### 块 U1 — Step1 不再写 `fusion_config.json`（计划外；用户 2026-09-26 裁定）
- **背景**：Step1 目录里有 `fusion_config.json`（Save 时写）、`step1_fusion_settings.json`（已确认的设置）、`step1_session.json`（会话）和 `fusion_meta.json`（fused.zarr 的产物说明）。用户希望 Step1 只生成一个统一的 JSON。只读核查：`fusion_config.json` 几乎没人读内容——Step2 只在信息栏显示文件名，Step3 查找原始切片路径时把它当备选之一；`step1_fusion_settings.json` 是已确认设置的权威来源（hash、身份核对、预分割过期判断都靠它），并入会话风险高。
- **用户裁定**：先做 U1（去掉 `fusion_config.json`）；`fusion_meta.json` 保留；Step2 信息栏那一行去掉。U2（把 `step1_fusion_settings.json` 并入会话）另行决定。
- **做法**：
  - Save 不再写 `fusion_config.json`；它原来写的内容（已确认的 fusion 配置、`ome_tiff`、`output_dir`、`norm_low/high`、`channel_remap_params`、`saved_at`）改为存进 `step1_session.json` 的 `last_save`，并立即请求一次会话保存。fusion 作业用的配置不变。
  - `last_save` 随会话恢复（两条恢复路径），切换数据集时清空。
  - `step1_output` 去掉 `fusion_config_path`；Step2 信息栏不再追加「Config: …」。
  - Step3 查找原始切片路径时先读 `step1_session.json` 的 `raw_ome_path`；旧项目的 `fusion_config.json` 仍作为备选读取，**不删不改**。
- **白名单**：`ui/main_window.py`（Save 写文件的一段、会话 payload 和恢复、`step1_output`、进入 Step2 时的信息栏）、`ui/step3_page.py`（`_resolve_raw_ome_path`）、`UI_SURFACE_RULES.md`、测试、本文档。`step2_page.py` 不需要改。
- **测试**：`tests/test_step1_step2_handoff_e2e.py` 新增 2 条（真实 Save 后没有 `fusion_config.json`，会话 payload 带着同一份 `last_save`，Step2 信息栏没有「Config:」；Step3 能从会话找到原始切片）；两条读回或断言 `fusion_config.json` 的旧测试改为读 `last_save`。回归 62 个模块（Step1 全部、Step3、会话和写保护、界面约定），与 HEAD 逐条对比没有新增失败。
- **真机验收通过（用户 2026-09-26）**：Step1 目录不再新生成 `fusion_config.json`，会话里有 `last_save`，Step2 信息栏不再显示 Config，Step3 正常打开。

### 块 S — `Load Previous Step1 Session` 每次弹对话框、打开所选场景（计划外；用户 2026-09-26 报告并批准）
- **问题**（真机）：点 `Load Previous Step1 Session` 没有弹窗、没有反应。终端显示：程序自己找到了当前 ROI 的 `step1_session.json`，所以不弹文件对话框；这个文件随每次改动自动保存，恢复后「changed nothing」；成功提示只打到终端。这是原有设计（`7c54a10`），不是 E 或 U1 的回归。另外，手动选择别的 ROI 的会话时，被拒绝的原因也只打到终端。
- **用户裁定**：`Load weights` 只恢复通道权重；`Load Previous Step1 Session` 完全复原当时的场景（ROI、patch、通道权重），每次都弹文件对话框；选中别的 ROI 或项目的会话时，直接切换到那个工作目录。
- **只读核实（第 1 步）**：数据集和工作目录归 Step0 所有——Step0 的 Load 事务式切换并发出 `dataset_committed`，Step1 丢弃自己的状态并退回 Step0，进入 Step1 时再从新 ROI 的 manifest 读取交接；Step1 的读取器不碰 Step0 页面；Step0 每次加载似乎新建一个 ROI 工作区，能否重新打开已有工作区尚未确认。跨 ROI 打开因此需要改 Step0 页面，触发申请里的停止条件。
- **用户裁定（2026-09-26）**：先交付缩小的一版（本块），同时做 S2 的只读调查。
- **本块执行记录**（2026-09-26 提交）：
  - `ui/main_window.py`：按钮改接 `_on_load_previous_session_clicked`：每次都弹文件对话框，默认在当前 ROI 的 step1 目录（没有时用输出目录）；取消则什么都不做；加载成功弹窗「Opened the Step1 session」；被拒绝时弹窗说明原因。加载函数和 v2 恢复里原来只打印的拒绝点改为 `_refuse_session(reason)`（打印并记下原因）；别的 ROI 或项目的会话提示「This session belongs to another ROI or project. Opening another project is not supported yet; this session was not loaded.」（用户 2026-09-26：Step0 没有「打开已有项目」的入口，原先让用户去 Step0 打开的提示会误导，改为如实说明）。程序打开 ROI 时的自动恢复仍然只打印，行为不变。
  - `tests/test_step1_session_button.py` 4 条：已有自己的会话时也弹对话框、取消什么都不做；选中的文件被加载并提示成功；别的 ROI 的会话走真实加载函数被拒绝、原因上屏、状态不变；自动恢复仍只打印。相关模块（交接契约、Load weights、会话、写保护、布局、端到端交接等）213 条全部通过。
- **拒绝时的「失败即锁」**：新格式会话被拒绝时，Step1 的就绪标志在核对之前就先清掉（有意设计，`test_step0_step1_handoff_contract.py:724` 锁定）；下次进入 Step1 会重新读交接，项目、ROI 和数据都不变。
- **S2（跨 ROI/项目打开整个场景）——冻结（用户 2026-09-26）**：用户认为「Load Previous Step1 Session」的定位是打开别的项目并切换一切（工作目录、每个 step 的结果，可能包括 Step2、Step3），相当于新开会话或历史复盘，属于全局 / 架构级；先完成 Step2、Step3 的优化，最后统一调整架构（见第六节「后续计划」）。只读调查结论（2026-09-26）：
  - Step0 目前**没有**重新打开已有 ROI 工作区的路径：`Load`（`_reload_from_paths`）每次清空 ROI、patch 和校正决定，不绑定已有的 `corrected_channels.zarr`；之后的第一次 Save 一定新建工作区（`utils/roi_project.py` 的 `create_roi_context` / `create_full_wsi_context`）。
  - 可复用的零件：`build_roi_context`（只算路径）、`OverviewPanel.set_rois_and_patches`（不触发编辑）、`_apply_corrected_store`、`Block01DisplayServices.adopt_mappings`（尚无调用方）、Step1 的权威读取器 `_load_step0_roi_result`。
  - 缺：可带参数调用的 Step0 Load、把场景装进 Step0 且不触发保存的入口、Step3 按 roi_id 选 ROI、切换时重置 Step2/Step3、Step2 分割运行时的忙碌检查、逐通道校正决定的恢复策略（v15 有意不在 Load 时预填）。
  - 建议顺序：纯函数「读取场景」→ Step0 Load 参数化（行为不变）→ Step0「装入场景」→ 忙碌检查 → 主窗口编排 → Step2/Step3 重置与按 roi_id 选择 → 两个合成项目的写入隔离测试。
  - 风险：Step0 提交切换之后再失败就回不到原项目；打开后若 Save 且没有恢复校正决定，会把该 ROI 的决定改写为 original；原始切片被改动（大小/mtime）会被拒绝；会话和 manifest 用绝对路径，项目移动后失效。

### 块 K — Step2 旧路径 ROI 模式中途 Stop 不再登记成功（用户 2026-09-26 批准；已实施，**真机验收通过**）
- **缺陷**（只读核实，未复现运行）：参数没有契约时（手动模式、旧参数文件），ROI 模式中途 Stop，`_segment_one_zarr` 在 Stop 检查点 `return 0`，但 ROI 外层的收尾检查（`workers/segment_merge_worker.py:3113`）只拦截契约路径：外层照常写 `segmentation_meta.json`、`_register_completed_result()` 登记为 `completed`、`update_roi_segmentation_run` 记 `done`、发 `finished`。界面随之显示「✓ Done!」、弹「Segmentation Complete」（带 Step3/Step4 按钮）、把该 ROI 的 Step2 标成 done。多 ROI 时，被停下的 ROI 还会沿用上一个 ROI 的 `_last_region_meta` 路径（`:3084`）。
- **影响面**：现在的交接把全图工作区也作为一个 ROI（`Full WSI`）交给 Step2（`ui/main_window.py:4163-4165`；`~/fusion_data/test1` 只读核对），所以 ROI 外层循环是日常主路径。
- **做法**：把 `:3113` 的 `if self._contract is not None and self._stop:` 改为 `if self._stop:`，旧路径复用已有的 `_ContractStopped` 收尾（不写汇总、不登记、不改 roi_index、不发 `finished`，只发 `error('Stopped by user.')`）；`run()` 的 `except` 里那条日志去掉「(Step1 hand-over)」，改为不区分路径的措辞。类名不改，不新增机制。
- **白名单**：
  - `workers/segment_merge_worker.py`：`run()` 中 ROI 外层收尾的这一处条件；`run()` 的 `except` 里 `_ContractStopped` 分支的那一行日志。
  - 新增 `tests/test_step2_legacy_stop.py`。
  - 本文档（执行记录）。
- **不改的范围**：`_segment_one_zarr` 及其各 Stop 检查点；全图分支（本块只修已定位的 ROI 收尾问题，全图分支不在本块处理）；契约路径；Step2 页面（包括 Stop 后标题为「Error」的对话框，已知 advisory）；输出目录里的文件清理；HQ / HQ2 / CDS（不再维护）。
- **测试**（只写合成项目的临时目录；真实引擎 Cellpose 或 StarDist，缺模型则跳过并注明，不用 mock）：参数不带契约，
  1. 单 ROI，推理中 Stop；
  2. 两个 ROI，第二个 ROI 推理中 Stop；
  3. 两个 ROI，第一个 ROI 完成后、第二个开始前 Stop；
  4. 不 Stop，正常完成。
  1–3：`finished` 为空，`error == ["Stopped by user."]`，结果登记和 roi_index 里没有本次运行，先前已登记的结果保持不变。4：`finished` 发出，结果登记为 `completed`，roi_index 记 `done`。
- **回归**：Step2 相关模块（含 `test_step2_runner_path.py`、`test_step2_ownership_move.py`、`test_step2_layout.py`、交接端到端）每模块单独进程，与 `git archive HEAD` 导出件逐条对比，只看新增失败。
- **真机验收**（用户）：在已有项目里用手动模式或旧参数跑一次 ROI 分割，中途 Stop：没有「Segmentation Complete」对话框；结果列表里没有本次成功登记；原有结果保持不变。
- **风险**：
  - Stop 在最后一个 ROI 刚写完输出、到达检查点之前才生效时，本次结果被丢弃（契约路径已是如此）。
  - 本改动只阻止整次运行被登记为成功，**不回收文件**：多 ROI 时前面已完成 ROI 的产物（`global_mask_<ROI>.zarr`、`global_mask_<ROI>.ome.tiff`、`global_dapi_<ROI>.ome.tiff`、`segmentation_meta_<ROI>.json`、`tile_masks/`）以及被停下 ROI 的半成品（`.dat`、部分切块）都留在本次输出目录里，未登记、不被下游读取。
- **回退**：恢复 `:3113` 的条件（加回 `self._contract is not None and`）和 `except` 里那行日志的原措辞，两处。
- **Advisory（不在本块处理）**：全图分支只在切块循环入口检查 Stop；最后一个切块推理期间或合并、写出阶段按 Stop，运行仍会完成并登记（结果是完整的，但与用户的 Stop 意图不符）。
- **执行记录**（2026-09-26，未提交）：
  - `workers/segment_merge_worker.py`：`:3113` 条件改为 `if self._stop:`（紧邻的注释同步改措辞）；`except` 的 `_ContractStopped` 分支日志改为「Stopped by user; nothing registered.」。别处未改。
  - `tests/test_step2_legacy_stop.py` 4 条（合成 ROI 工作区、真实 StarDist、参数不带契约）：改后单独运行 4 passed（连续两次）。同一测试放在 `git archive HEAD` 导出件上：3 条 Stop 场景失败——单 ROI 中途 Stop 仍发 `finished`（0 个细胞），另两种发 `finished`（60，即 ROI A 的数目）；不 Stop 的一条通过。缺陷由此复现并被锁住。
  - 回归：24 个模块（Step2 worker/页面/交接、契约、归属、remap、HQ 选择、Tissue Navigator、写保护等），每模块单独进程、4 个并行，改后与 HEAD 导出件逐条对比，已有模块**没有新增失败**。两边相同的失败：`test_hq_marker_segmentation.py` 2 条（HQ，不再维护）、`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`。只在 HEAD 一侧出现的 `test_step2_runner_path.py::test_a_hand_over_equals_the_runner_with_shared_ownership[stardist_nuclei_dapi-full]`（1 像素差），两边单独重跑都通过，是负载下的偶发。
  - 并行时新测试有 3 条失败，原因是下面的已有问题（运行资源监控器的 `NameError`），不是本块改动：单独运行全部通过。
  - **真机验收通过（用户 2026-09-26）**：手动模式 ROI 分割中途 Stop，不再弹完成对话框、不登记。用户反馈：点 Stop 后不能立即停止（旧路径要等当前切块推理结束），另行调查。

### 块 M — Step2 的 8 个方法统一走引擎子进程（用户 2026-09-26 批准；已实施，**真机验收通过**）
- **背景**：块 K 之后，用户发现手动模式和 Step1 继承走两套流程（进程内推理 vs 引擎子进程）：Stop 快慢不同、失败处理不同、Cellpose whole-cell 输入不同（旧路径 `[fusion, DAPI]` 不指定 `channel_axis`，契约为 `[fusion, fusion, DAPI]`），违背 R14。用户选择完全统一。
- **用户裁定（2026-09-26）**：
  1. 范围覆盖 Cellpose、StarDist、Mesmer 共 8 个方法（HQ / HQ2 / CDS 不维护，留在旧路径；从 .npy 恢复不跑模型，不受影响）。手动、继承、无契约的旧参数文件都走引擎。
  2. 接受 Cellpose whole-cell 手动结果改变。
  3. Step2 保留「Use GPU」：手动和继承都可以选择 CPU（GPU 部署失败或用户想强制 CPU）；Step1 预分割不加，仍由引擎自动选择。StarDist 模型名固定为 `2D_versatile_fluo`，Step1、Step2 都不能修改。
  4. 失败处理同契约路径：切块推理失败时整次运行报错、不登记。
  5. Step2 手动界面里契约之外的 Mesmer 控件删除（nuclear_channel、membrane_channels、input_mode、use_gpu、tile_size、overlap、batch_size、normalize_input、percentile_low/high）；保留 image_mpp、postprocess_min_size；新增 maxima_threshold、interior_threshold（与 Step1 参数表一致）。
  6. 旧参数文件里引擎不用的设置：点 Run 时弹窗逐条说明原因，按钮只有「按契约运行 / 取消」，强制按契约执行。
  7. **引擎身份不再作为拒绝理由**：只核对引擎种类（`engine`）和能否启动；Step1 那次运行的身份与本次身份都记进运行元数据，不同时只写日志和终端。原因：Step1 定下的是参数，参数正确就复用；`runner_version` 按整个 `seg_runner/` 目录哈希，改启动代码也会误拒。
  8. 旧路径中这 8 个方法走不到的推理分支删除（Cellpose 进程内加载留给 HQ）。
  9. Mesmer 3 个方法本机无模型，推理标「未验收」，不用 mock。
- **白名单**：
  - `seg_runner/client.py`：`EngineProcess` 增加 CPU 选项（子进程环境 `CUDA_VISIBLE_DEVICES=""`）。
  - `workers/segment_merge_worker.py`：`run()` 开头的路径选择与元数据；`_init_segmentation_backend`；`_start_contract_engine`（身份只核对种类、记录身份与设备）；`_segment_tile`（8 个方法走引擎，删除其旧分支）；`_segment_tile_contract`（无契约时按参数表取参数）；`_validate_mesmer_config`；两个切块循环 `except` 的判断。
  - `ui/step2_page.py`：统一的 Use GPU 行（两种模式都显示）；Mesmer 控件删除 / 新增；StarDist 模型名只读；读写配置（`get_seg_config`、两处套用配置到界面）；`_run` 的旧设置弹窗。
  - `utils/segmentation_param_schema.py`：`ParamSpec` 增加「固定值」，StarDist 模型名固定；`ui/step1_presegmentation/method_editor.py`：固定值只读。
  - 纯函数（列出旧参数文件里引擎不用的设置）放在 `core/preseg_contract.py`。
  - 测试、`UI_SURFACE_RULES.md`、本文档。
- **不改的范围**：`seg_runner` 的 engines / runner / protocol（引擎行为不变）；Step1 预分割的运行；HQ 系；从 .npy 恢复；输出写出阶段；Step2 其他控件与布局。
- **风险**：手动 Cellpose whole-cell 结果改变（已接受）；每块写一个临时 `.npy`（契约路径已如此）；旧 Mesmer 设置被强制忽略（弹窗说明）；Mesmer 未验收。
- **验收门**：手动配置下 Cellpose 3 个、StarDist 2 个方法 × 两个循环，全局 mask 与「同一 runner 逐块跑 + Step2 方式粘贴」逐像素相同；手动模式推理中 Stop 在 2 s 内结束、不登记；切块失败整次报错；不勾 Use GPU 时引擎设备为 CPU（继承和手动）；引擎种类不同仍拒绝、身份其他键不同照常运行并记录；界面：Mesmer 控件删除 / 新增、模型名只读、两种模式都有 Use GPU、旧文件弹窗列出原因且「取消」不运行；Step1 模型名只读。回归与 HEAD 逐条对比无新增失败。真机验收：手动模式 Stop 立即生效；取消 Use GPU 后用 CPU 跑通。
- **执行记录**（2026-09-26，未提交）：
  - `seg_runner/client.py`：`EngineProcess(..., cpu_only=False)`；为 True 时子进程环境 `CUDA_VISIBLE_DEVICES=""`，torch 和 TensorFlow 都看不到 GPU。engines / runner / protocol 未改。
  - `workers/segment_merge_worker.py`：新增 `_runs_on_engine()`（8 个方法且非 .npy 恢复）、`_wants_gpu()`、`_engine_params()`（有契约取契约参数；无契约按 `segmentation_param_schema` 的参数表从配置取值，加方法固定规则；固定参数一律取固定值）；`_init_segmentation_backend`、`_segment_tile`、两个切块循环的 `except` 改按 `_runs_on_engine()` 判断；`_start_contract_engine` 传 CPU 选项，引擎身份只核对种类，其余不同则打印并写日志，身份、设备、Use GPU 记入 `self._engine_meta`，写进 `segmentation_meta.json` 的 `seg_engine`；`_validate_mesmer_config` 只剩「读 fused 切块」；删除 8 个方法的旧推理分支（Cellpose whole-cell / nuclei / expansion、StarDist、Mesmer 的进程内推理和加载），以及随之不用的 `_mesmer_uses_selected_channels` 和 4 个导入。Cellpose 进程内加载只留给 HQ 系。
  - `ui/step2_page.py`：`GPU:` 行对所有方法、两种参数来源都显示；删除 Mesmer 的 10 个控件和只对 Mesmer 显示的 `tile size` / `batch size`，新增 `maxima_threshold`、`interior_threshold`；`get_seg_config` 对 Mesmer 写入契约的固定规则和该方法的 `input_mode`，去掉引擎不用的键；旧的 Mesmer `use_gpu`（auto / gpu / cpu）套用到 Use GPU；StarDist 模型名只读、恒为 `2D_versatile_fluo`；`_check_preseg_contract` 不把固定参数算作不一致；新增 `_confirm_ignored_settings`，在 index 来源 Run 时弹窗。
  - `core/preseg_contract.py`：新增 `STARDIST_MODEL`、`mesmer_input_mode()`、`ignored_settings()`（按写在文件里的内容判断，`params` 优先；契约文件只检查模型名）。
  - `utils/segmentation_param_schema.py`：`ParamSpec.fixed`；StarDist `model_name` 固定；`parse_values` 拒绝非固定值；`combinations`、`summary` 对固定参数取固定值（旧方案里别的模型名按固定值运行）。`method_editor.py`：固定参数只读、显示固定值。
  - 白名单外的小改动：`_check_preseg_contract`（固定参数不算不一致，属于「旧设置弹窗」的一部分）、`schema.summary`；已在上面列出。
  - 测试：新增 `tests/test_step2_engine_unified.py`（38 条：手动配置 8 方法 × 两个循环与 runner 逐像素相同，Mesmer 6 条因本机无模型跳过并注明「未验收」；手动推理中 Stop 2 s 内结束、不登记；引擎失败整次报错；不勾 Use GPU 时 StarDist 手动 / 继承都在 CPU 上跑，Cellpose 仅验证 CPU 模式启动报告 `cpu`（本机 Cellpose-SAM 在 CPU 上一个 120×120 块约 230 s，不放进常规测试）；勾选时设备由引擎决定；模型名固定；HQ 不走引擎；`ignored_settings` 纯函数；界面各项；Step1 模型名只读）。改写 2 条旧测试：`test_step2_runner_path.py::test_another_engine_is_refused` → `test_another_engine_version_runs_and_is_recorded`（裁定 7），`test_preseg_contract.py::test_ticking_normalize_input_in_step2_is_a_mismatch` → `test_step2_mesmer_keeps_the_fixed_rules_and_checks_its_thresholds`（控件已按裁定 5 删除）。
  - 回归：31 个模块，与 `git archive HEAD` 导出件逐条对比。轻量模块 4 个并行；真实引擎模块在 4 并行下超时或崩溃（10 GB 内存 / 6 GB 显存不够），改为逐个顺序运行。**已有模块没有新增失败**。两边相同的失败：`test_hq_marker_segmentation.py` 2 条（HQ 不维护）、`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`、`test_seg_runner_engines.py` 3 条（2 条 Mesmer 缺模型，1 条 StarDist expansion）、`test_seg_runner.py::test_mesmer_in_subprocess_equals_direct_call`（缺模型）。`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call` 与新测试的 StarDist ROI 一条各失败过一次，重跑 3 次：HEAD 上失败 2 次、改后失败 1 次、新测试 3 次通过——StarDist 同机多次运行偶发 1 像素差，HEAD 上已有。
  - **真机验收通过（用户 2026-09-26）**。

### Step3 重设计 — 只读调查与用户裁定（2026-09-26，未启动实施）
- **现状**（只读调查）：`ui/step3_page.py` 3705 行。左栏：项目 / ROI / 结果选择、1/32 DAPI 缩略图、Channels 与 Channel Overlay 面板；右栏：矩形框出的 patch 放大视图（Alpha、Show Outline、Reset View）；另有「Channel Remap Review / QC」标签页。显示为 pyqtgraph + CPU 合成，无 GPU、无金字塔读取；只统计 ROI 内细胞数。写 `step3_input_files.json` 和 `step3_channel_overlay_config.json`（后者每次勾选都写，落在 Step2 运行目录里），无人读取；不向 Step4 传任何东西。已有缺陷：离开到 Step0/1 不停后台线程；切换数据集不重置。核心功能无测试。
- **Step1 组件复用评估**：显示服务、全局通道面板、Navigator、相机快照为全局单实例，可直接用；`Step1ViewerHost` + 读取栈、`Step1GpuLayer` 可再建实例（montage 已有第二个 GPU 层）；`Step1WholeSlideMount` / `Step1ViewerBinding` 写死 `STEP1_SCOPE`、Step1 草稿、`window._active_roi` 和 step==1 判断，需参数化；patch 按钮条是主窗口代码，需抽成组件；Navigator 的 ROI 编辑经 `_persist_geometry_edit` 写 Step0 的 `roi_config.json` 等文件；仓库里没有整张图的 mask 叠加。
- **Odon 参考**（https://github.com/alexcoulton/odon ，提交 `b01faef010b14ea03c33e92a14e438061d257e39`，GPL-3.0，只参考设计不复制代码；只读了 `src/render/labels*.rs`、`src/masks/layers.rs` 与 `src/app.rs` 的相关段落，其余功能未逐项核实）：标签图用 OME-NGFF 多级金字塔；mask 层级锁定为图像当前层级；按视野计算所需分块，LRU 块缓存 + 一个后台读取线程，快速平移时丢弃过期请求；每块带 1 像素 halo 以 R32UI 整数纹理上传，片元着色器比较相邻编号求边，线宽 0–4 px，单色 + 透明度（默认绿、0.75），CPU 不做轮廓或多边形。
- **用户裁定（2026-09-26）**：
  1. 重建 Step3：取消「画矩形 → 只看这块」，改为整张图的 mask 浏览；排版与外观同 Step1，是 Step1 的简化版（只有通道面板和 viewer），尽可能复用 Step1 组件。
  2. 交互：Tissue Navigator 空降、patch 空降、缩放拖动；保留 Intensity、Overlay / Fusion。
  3. 可以在 Tissue Navigator 上画 ROI，但不产生下游影响（Navigator 在 Step3 为沙盒：只在 Step3 内存，不写文件，不影响 Step0/Step1）。
  4. ~~通道权重保留、可调整，但不保存：Step3 有自己的 fusion 草稿~~（**已被「骨架裁定」中的全局联动取代，不再执行**），**首次进入、以及 Step1 每次新的确认之后**从 Step1 已确认的设置复制（与现有的勾选播种规则一致），其余时候保留 Step3 自己的临时调整；需要修改 `UI_SURFACE_RULES.md`「Step3 行只有勾选、颜色、名称」一条。
  5. mask 选择：默认当前 ROI 最新（`roi_index` 的 active run），面板上方一个小下拉框切换。
  6. mask 控件放在 viewer 上方一行：显示开关、透明度、线宽、颜色；默认只画轮廓（单色、0.75、线宽 1），「填充」为选项（按细胞编号的固定随机色）。
  7. 按 Odon 的做法：在 GPU 显示层加标签渲染（R32UI 纹理 + 求边 / 填充着色器）——用户批准修改 GPU 层。
  8. 标签金字塔在 Step2 分割结束时生成（块 N）；旧结果没有金字塔时，Step3 打开时调用同一个生成函数现场补一次，存进该运行目录。
  8a. 补生成也失败时的退路（用户 2026-09-26 同意；第 ④ 步实施）：金字塔只负责缩小时的粗层级，第 0 级就是 Step2 的 mask 本身。① 写不进运行目录（磁盘满、无权限）→ 在内存里生成、只在本次打开期间用、不写文件（粗层约为 mask 像素的 6.6%）；② 内存生成也失败 → 只在第 0 级显示 mask，缩小时不画，mask 控件行显示「缩小时无法显示 mask：原因」；③ mask 本身读不出 → 不显示 mask，状态栏写明原因并建议到 Step2 重跑。其余功能照常；原因同时打到终端；`read` 不接受未完成或第 0 级不吻合的金字塔。
  9. 删除「Channel Remap Review / QC」标签页；不再写 Step2 运行目录里的两个配置文件。
  10. Step3 的 viewer 常驻，离开 Step3 不释放（目标部署在资源充足的服务器，避免每次重新准备）；隐藏时停止视野请求，换数据集时关闭并按新数据重开，退出程序时释放——常驻不等于继续读旧数据。
  11. patch 按钮条抽成 Step1 / Step3 共用组件，允许修改 Step1 并回归。
- **骨架调查结论**（2026-09-26，只读）：viewer 底层（host、读取栈、GPU 层、合成）不碰主窗口，可建第二个实例；`Step1WholeSlideMount` / `Step1ViewerBinding` 需参数化：显示范围（`STEP1_SCOPE` 写死在 `step1_viewer_mount.py:602-603`、`step1_viewer_binding.py:111`，CPU 路径构造合成绑定时未传 scope，`:333`）、fusion 模型（`_domain = window._display.fusion`，`:188-190`）、数据来源（binding `:66-96` 读 `loader.filepath`、`_corrected_decisions`、`_corrected_zarr_path`、`_active_roi`、`step0_output`；mount `:735` 读 `_active_roi["polygon_fullres"]`）、相机标识（`"step1-*"`，`:1018-1038`）。`deactivate()` 不停视野请求（GPU 绑定仍连着控制器的 `interaction_event` / `gesture_quiet`，`step1_gpu_binding.py:323-327`），隐藏的 viewer 仍会读数据，`publish_view_rect`（`:970`）不查前台，会改写共享 Navigator 的视野框。全局通道面板构造时绑定 Step1 的 fusion 模型、不可换绑，权重与「勾选即加入 fusion」只在 Step1 生效（`global_dock.py:489-495`、`:957-1025`）。一个独立的 `FusionDomainModel` 没有任何全局监听者，不会触发 Step1 的会话保存。Step1 已确认的 fusion 快照只含参与 fusion 的通道（`main_window.py:7898-7911`）。换数据集时 Step1 的 viewer 不关闭，留到下次进入 Step1（advisory，未改）。每个 viewer 实例的缓存上限约 2.7 GB 内存 + 512 MB 显存。
- **骨架裁定（用户 2026-09-26）**：
  - Intensity（Min / Max / Gamma）与颜色保持全局联动：在 Step3 调也会改 Step1 的显示；分割用的是 Save Fusion Settings 保存的快照，可随时用 Load weights 恢复，所以不受影响。
  - ~~方案 A：Step3 自己一份 fusion 草稿~~（**作废**，用户 2026-09-26 改为下一条）。
  - **勾选、权重、Intensity、颜色、fusion 草稿与 Overlay / Fusion 模式全部在 Step1 ↔ Step3 之间联动**（用户 2026-09-26；Step0、Step2 的勾选仍各自独立，Intensity 与颜色延续原有的全局规则）：Step3 直接使用 Step1 的显示范围（step1）和同一份 fusion 草稿。理由：用户在 Step3 对着 mask 调出更满意的 fusion 后，回到 Step1 直接重算，不必手工记录再调一遍。后果（用户确认）：Step3 里的调整由 Step1 的会话自动保存写进 `step1_session.json`，与在 Step1 里调完全相同；已确认的快照（Save Fusion Settings）只有在 Step1 点保存时才变，分割不受影响；Step2 不变（仍只有自己的勾选，从 Step1 已确认的设置播种）。
  - Step3 保留 Reset weights（所有 marker 权重清零）与 Load weights（读取同一项目的历史会话）。Load weights **不改写所选的历史会话文件**；读入后修改共用草稿，并按现有机制自动保存到当前的 `step1_session.json`。Reset weights 同样会自动保存草稿。两者都不提交已确认快照。若所选文件恰好就是当前的 `step1_session.json`，读入后它会像平常一样被自动保存改写——不承诺该文件永不被写入。Step3 没有任何 Save 按钮。
  - Tissue Navigator 与 Step3 当前画面实时联动（与 Step1 看同一份合成；「已发布的规格是否随草稿实时更新」在 2b 核实）。
  - Step3 仍用第二个 viewer 实例（与 Step1 画同样的内容，另加 mask 层）；两个实例交替进入后台，都要暂停 / 恢复。只用一个 viewer 在两页间搬动可能导致 OpenGL 重建与纹理重传，不采用。
  - 两个 viewer 各自保存 `_mode`，共用草稿不会自动同步模式：2b / 2c 须明确模式同步的接线（切换写入共用的模式并通知两个实例；后台实例按 2a 只记录、恢复时合成），并保证只有前台实例发布 Navigator 视野框。
  - 2b 修改 `UI_SURFACE_RULES.md` 时，精确替换「四步的勾选各自独立」相关条款（第 197、214 行附近）为：Step1 ↔ Step3 共用勾选与权重，Step0、Step2 仍各自独立；并修改第 211、222、292 行（权重只属于 Step1、Step3 行只有勾选 / 颜色 / 名称、权重只在 Step1 的行里编辑）。
  - 2b / 2c 的验收包含一次完整的公开操作：在 Step3 调整勾选与权重 → Step1 显示同样的设置 → 当前会话记住草稿；已确认的快照和 Step2 的输入保持原样，直到用户在 Step1 明确保存。
  - 骨架拆成 2a / 2b / 2c：2a viewer 的暂停 / 恢复、Navigator 视野框只在前台发布、相机标识参数（重构，除视野框一项外界面与 Step1 行为不变）；2b Step3 使用 Step1 的显示范围与 fusion 草稿（通道面板在 Step3 与 Step1 相同、Reset / Load weights 同一套功能、Navigator 实时联动的核实）、界面规则与规则测试修改；2c 新的 Step3 页面（左栏同 Step1 的 Channels 框 + Show all / Intensity + Reset / Load weights + 通道列表；右栏 Overlay / Fusion 按钮 + viewer），删除旧页面、旧标签页与写进 Step2 目录的配置文件，加入共用列宽，常驻、隐藏暂停、换数据集与退出时释放。
- **顺序调整（用户 2026-09-26）**：第 ⑤ 步（Navigator 沙盒 ROI）提前到第 ③、④ 步之前。沙盒的要求：Step3 里 Navigator 可以画 ROI、改 patch，但**不保存、不改任何本地文件、不影响 Step0 / Step1**；在 Step3 画的 ROI / patch **保留到下次进入 Step3**（本次运行内，不写盘）；用途是**临时标记和快速切换位置**。
- **第 ⑤ 步改定（用户 2026-09-26，取代上一条的沙盒要求）**：只读调查发现，弹窗的编辑无条件交给 Step0 处理与保存（`step0_page.py:4521-4524`），新 patch 的编号取自 Step0 的计数器（`:4516`），弹窗里的 ROI / Patch 列表是 Step0 的控件——「可编辑不保存」需要给 Step3 一份独立的列表与路由；用户曾考虑「patch 全局同步、Step3 可增删自己的 ROI 并单独记录」，因需要「受保护 ROI」等新机制而放弃。**最终裁定：Step3 的 ROI 全部继承 Step0 / Step1、冻结锁定，Step3 不能新建、删除或修改任何 ROI；patch 在 Step3 可以新增、移动、删除、改名，并与 Step0 / Step1 全局同步（走现有的保存流程）。** 不需要沙盒、不需要 Step3 的本地记录文件。
- **拟分步**（每步单独申请、单独验收；② 已拆成 2a / 2b / 2c，见上）：① 块 N（标签金字塔，先做）；② Step3 骨架（2a / 2b / 2c：复用 viewer 与通道面板，与 Step1 共用显示范围和 fusion 草稿，删除旧功能与标签页）；③ patch 按钮条组件化 + Navigator 空降；④ GPU 标签渲染与 mask 控件；⑤ Navigator 沙盒 ROI。在 ⑤ 完成之前，Step3 的 Navigator 保持现在的只读策略，不开放任何会写 Step0 文件的编辑。

### 块 N — Step2 生成标签金字塔（申请 v2，按独立审核修订，用户 2026-09-26 批准；已实施，**真机验收通过**）
- **必要性**：Step3 按 Odon 的做法浏览整张图的 mask，需要与图像金字塔同级的标签金字塔；Step2 的 mask 只有一层（uint32 zarr，1024² 分块，ROI 坐标）。用户裁定在 Step2 生成，用户只等一次。
- **实测**（单个样本，只读真实 mask，输出写临时目录）：15437×16215 的 mask 生成两级 1.6 s；同尺寸合成 mask 0.7 s。不代表全切片耗时。
- **坐标契约**（与 Viewer 一致，`viewer/raw_tile_provider.py:169-193`：几何一律用每轴不取整的比例）：
  - 标签第 L 级与原始切片第 L 级使用**同一网格**：该级尺寸 `(H_L, W_L)` 取自原始切片金字塔，比例 `ds_y = H_0/H_L`、`ds_x = W_0/W_L`（浮点、两轴独立，不取整）。
  - 采样规则（像素中心最近邻）：第 L 级像素 `(i, j)` 的值取第 0 级切片坐标 `(floor((i+0.5)·ds_y), floor((j+0.5)·ds_x))` 处的 mask；该点落在 ROI 外时为 0。ROI 局部坐标 = 切片坐标 − ROI bbox 起点。
  - 每级数组只覆盖 ROI：行 `i ∈ [floor(y0/ds_y), ceil(y1/ds_y))`，列同理；数组名为级号（`1`、`2`…），不是倍数。
  - 元数据（`.zattrs`）：版本；第 0 级 mask 的相对路径与形状（第 0 级不复制）；原始切片各级尺寸；每级的 `ds_y`、`ds_x`、在该级网格中的起点 `(i0, j0)` 与数组形状；ROI bbox；mask 种类（cell / nucleus）；采样规则的文字说明；`complete` 标志。
  - 原始切片拿不到时不生成（记录原因），Step3 退回现场补生成。
- **做法**：新增纯函数模块 `core/label_pyramid.py`（无 Qt）：`level_grid(raw_level_shapes, roi_bbox)`、`build(level0_zarr, out_path, raw_level_shapes, roi_bbox, kind, cancel_check=None)`、`read(out_path)`（只返回 `complete` 且第 0 级路径与形状吻合的金字塔，否则 None）。Step3 现场补生成调用同一个 `build`。
- **停止与完整性**：
  - 开始前已按 Stop：跳过。
  - 生成中按块检查 Stop：写在临时目录 `label_pyramid_<ROI>.zarr.partial`，全部层级写完后先写 `complete: true`，再原子改名为正式名；Stop 或失败时删除临时目录。
  - 之前已完成的 ROI 的金字塔保留（与块 K 一致：已完成 ROI 的产物留在目录，整次运行不登记）。
  - `read` 不接受缺少 `complete` 或第 0 级不吻合的目录，未完成的金字塔不会被 Step3 当作完整产物。
- **失败**：金字塔是可重建的显示派生产物。mask 已写成、金字塔因 I/O 等失败时：保留 mask 和分割结果，终端与日志明确报告，不写金字塔路径；Step3 打开时现场补生成（用户 2026-09-26 同意；补生成也失败时见 Step3 裁定 8a）。
- **meta**：每个 ROI、每种 mask 分别记录：内层 ROI meta 写 `label_pyramid: {"cell": 路径或 null, "nucleus": 路径或 null}`；外层逐字段构造的 ROI 记录（`segment_merge_worker.py` 的 `roi_meta_all`）显式带上该字段；汇总 `segmentation_meta.json` 按 ROI 列出；全图模式同样写入。
- **接入**：两个循环在 mask 写完、调用 `_record_step2_geometry(...)` 之后（`:2790`、`:3649`）调用 `_write_label_pyramids(...)`。
- **白名单**：`core/label_pyramid.py`（新）；`workers/segment_merge_worker.py`（新增 `_write_label_pyramids`，两处调用，内层 ROI meta、外层 `roi_meta_all` 与汇总的 `label_pyramid` 字段）；`tests/test_label_pyramid.py`（新）；本文档。
- **不改的范围**：mask 本身与其他输出、`.dat` 等现有文件（清理归 Step4 优化）；Step3（现场补生成在 Step3 块里接）；分割、归属、拼接逻辑；Viewer。
- **空间**：两级下采样层的**未压缩像素量**约为第 0 级的 1/16 + 1/256 ≈ 6.6%（本数据按比例折算约 1 MB 级）；压缩后的实际增量随 mask 内容变化，不作保证。
- **风险**：每次运行多几秒（单样本 2 s 以内，全切片未测）；原子改名在同一文件系统内。回退：删除两处调用。
- **验收门**：
  - 纯函数：奇数尺寸、非整数比例、两轴比例不同、ROI 起点非倍数、ROI 贴边；对照**用 Viewer 的坐标映射**（`RawTileProvider.level_downsample_yx` 读同一个合成 OME-TIFF 金字塔）独立算出每个标签像素的中心，而不是复用自身公式；各级形状等于该级网格上的 ROI 覆盖范围；0 与空 mask。
  - 停止与完整性：生成中取消后无正式目录、无临时目录；缺 `complete` 或第 0 级不吻合时 `read` 返回 None。
  - 失败：模拟普通 I/O 失败，分割照常完成并登记，meta 里没有金字塔路径，日志有报告。
  - Step2 真实运行（StarDist，ROI 两个 + 全图）：每个 ROI、每种 mask 的路径都记录在内层、外层与汇总 meta 中，且能用 `read` 打开；第二个 ROI 中途 Stop 时，第一个 ROI 的金字塔保留、第二个不存在、整次不登记。
  - 回归与 HEAD 逐条对比无新增失败。
- **执行记录**（2026-09-26，未提交）：
  - `core/label_pyramid.py`（新）：`raw_level_shapes`、`level_grid`、`build`（逐级、按 1024² 输出块用正交索引取第 0 级的像素中心，块间检查取消；写 `.partial` → 最后写 `complete` → 原子改名；取消或失败删除临时目录；ROI 与 mask 形状不符报错）、`read`（只认 `complete`、版本相符、第 0 级仍在且形状一致）。
  - `workers/segment_merge_worker.py`：新增 `_write_label_pyramids`（已 Stop 跳过；原始切片取自 `_raw_channel_source_path()`，拿不到或不足两级时报告并跳过；cell / nucleus 分别生成 `label_pyramid_<ROI>.zarr`、`label_pyramid_nuclei_<ROI>.zarr`；取消时保留已完成的；普通失败打印并写日志、不写路径）；两处调用（ROI 循环与全图循环的 `_record_step2_geometry` 之后）；内层 ROI meta、外层 `roi_meta_all`、汇总（`label_pyramid: {ROI 名: {cell, nucleus}}`）与全图 meta 写入路径。
  - 测试 `tests/test_label_pyramid.py` 12 条：4 种 ROI（起点非倍数、整张切片、右下贴边奇数尺寸、小于一个粗像素）在合成 OME-TIFF 金字塔（401×523 → 101×131 → 26×33，比例 3.970 / 3.992、15.42 / 15.85）上，各级逐像素等于用 `RawTileProvider.level_downsample_yx` 独立计算的像素中心采样，起点与形状等于该级网格上的 ROI 覆盖；全零 mask；形状不符报错且无残留；取消后无正式目录与临时目录；`read` 拒绝缺 `complete`、第 0 级形状改变、第 0 级删除；Step2 真实运行（StarDist，两个 ROI）三处 meta 都有路径且 `read` 能打开并指向对应 mask；全图运行；第二个 ROI 中途 Stop 时第一个 ROI 的金字塔保留、第二个无残留、整次不登记；模拟磁盘满时分割照常登记、meta 路径为 null、终端有报告；核 mask 生成独立金字塔。反向注入 2 处（采样改为像素左上角、比例取整）各使 4 条对齐测试变红。
  - 真实数据试算（只读真实 mask，输出写临时目录）：`cropped_region.ome.tif` 的层级 3859×4053、964×1013，生成 0.7 s，两级形状与原始切片层级一致，比例 4.0003 / 4.0007、16.0135 / 16.0069；压缩后 2.24 MB（mask 本身 18 MB，约 12%，与未压缩像素比例 6.6% 不同，符合前述限定）。
  - 回归：Step2 相关 19 个模块（轻量 4 并行，真实引擎 3 个逐个顺序），与 `git archive HEAD`（`4b33999`）逐条对比**无新增失败**；两边相同：`test_hq_marker_segmentation.py` 2 条（HQ 不维护）；HEAD 一侧偶发 StarDist ROI 等价 1 条（已知的 StarDist 偶发不一致）。
  - **真机验收通过（用户 2026-09-26）**：`test1` 上一次 Cellpose whole-cell 全图运行生成 `label_pyramid_Full WSI.zarr`（2.1 MB，mask 16 MB），`read` 正常，两级形状与原始切片一致，三处 meta 都有路径，无临时目录残留，生成 0.61 s（整次 857 s）。

### 块 2a — viewer 的暂停 / 恢复与前台发布（申请 v3，按两轮独立审核与用户改定修订，用户 2026-09-26 批准；已实施，**真机验收通过**）
- **必要性**：Step3 用第二个 viewer 实例，与 Step1 的实例交替进入后台；后台实例必须不读数据、不改写共享的 Navigator。现在 `deactivate()` 不停读取（GPU 绑定仍连着控制器的 `interaction_event` / `gesture_quiet`，`step1_gpu_binding.py:323-327`；粗层完成的回调会接着申请精层，`:709-720`；`refresh_display()` 可能为新勾选的通道申请读取，`:211`；CPU 路径下合成协调器直接向调度器取数据，控制器的开关只停控制器自己的读取，`viewer/explore_view.py:3654-3672`），`publish_view_rect`（`step1_viewer_mount.py:970`）也不查前台。
- **v1 → v2**：用户改定为 Step3 与 Step1 共用显示范围、fusion 草稿与数据来源，v1 的 `scope` / `domain` / `source` 参数用不上，**不做**；暂停方案按独立审核补齐。
- **暂停语义**：不再派发任何新的读取请求；已开始的读取允许完成，但完成回调不得启动下一轮读取；不强行中断底层 I/O，不改调度器。重复暂停 / 恢复幂等，不重复连接。
- **做法**：
  - `Step1GpuBinding` 新增 `pause()` / `resume()` 与一个暂停标志：暂停时断开控制器的 `interaction_event`、`gesture_quiet`，停计时器；粗层回调里「接着规划精层」与 `refresh_display()` 里的规划、`update_viewport()` 在暂停时都不派发（已到达的结果照常收下）。恢复时重连并执行**一次恢复刷新入口**：按最新的显示快照与视野重新规划，继续取回缺失的粗层与精层（不是只补一个瓦片）。
  - `Step1WholeSlideMount` 新增 `pause_requests()` / `resume_requests()`：GPU 路径调用上面的 `pause` / `resume`；CPU 路径复用现有的 `deactivate()`（断开合成的 fusion / 显示监听）并关闭控制器的视野请求（`_set_controller_viewport_requests(False)`），暂停期间 `_recompose_for_the_camera` 不触发合成；恢复时复用 `activate()` 重连并刷新到最新状态，再打开视野请求。不重构合成协调器。
  - 模式切换也是读取入口（CPU 路径 `set_mode()` 直接调用 `compose.set_mode()` 并触发合成，`step1_viewer_mount.py:796-811`）：暂停期间 `set_mode()` 只记录最新模式（GPU 与 CPU 路径都是），恢复时按最新模式合成 / 刷新。不改协调器。
  - `publish_view_rect` 与 `_on_range_changed` 只在 mount 激活且未暂停时发布（后台实例不改写共享 Navigator 的视野框）。**这是 Step1 行为上唯一的变化。**
  - 构造参数 `camera_reason="step1"`，替代 `"step1-patch"` / `"step1-preview"` / `"step1-gesture"` 等标识的前缀；默认值下标识与现在逐字相同。
  - 不接入主窗口：Step1 的 mount 仍以默认参数构造，暂停 / 恢复在 2c 接到 Step1 与 Step3 的切换上。
- **白名单**：`ui/step1_gpu_binding.py`（`pause` / `resume`、暂停标志、粗层回调与 `refresh_display` / `update_viewport` 的暂停判断）；`ui/step1_viewer_mount.py`（`pause_requests` / `resume_requests`、`_recompose_for_the_camera` 与 `set_mode` 的暂停判断、`publish_view_rect` 与 `_on_range_changed` 的激活判断、`camera_reason` 参数）；新测试 `tests/test_viewer_pause.py`；本文档。
- **不改的范围**：主窗口接线；host、读取栈、调度器、缓存、GPU 层与着色器、合成协调器、`step1_draft_spec`、`step1_viewer_binding.py`；通道面板、fusion 模型、显示状态；任何可见界面。
- **风险**：GPU 真实渲染在本机测试环境大部分跳过（`test_step1_gpu_takeover.py` 34 条需要真正的 OpenGL），交付时 GPU 路径保持「真机未验收」，需真机确认 Step1 画面不变；暂停判断漏掉某个读取入口会让后台实例继续读数据（新测试覆盖已知入口）。回退：恢复这两个文件。
- **验收门**：
  - 除 Navigator 视野框一项外 Step1 行为不变：Step1 viewer 相关测试（mount、binding、host、exit gates、GPU takeover / request gate / overview skip、shared camera、viewer takeover、navigator teardown）与 HEAD 逐条对比无新增失败。
  - GPU 绑定（沿用现有仿真 GPU 层与调度器的测试写法）：暂停后控制器事件不再触发规划；**粗层读取未完成时暂停，随后结果到达，不再派发精层请求**；暂停期间改变共享的 Intensity / 颜色与 fusion 草稿、勾选新通道，不新增请求；重复暂停 / 恢复不重复连接；恢复后按最新状态刷新，缺失的数据继续取回。
  - CPU 路径（沿用 `test_step1_viewer_mount.py` 的仿真窗口与合成金字塔）：暂停后调度器不再收到来自该实例的请求（控制器与合成），相机移动与草稿 / 显示变化都不触发合成；恢复后显示最新状态并补齐数据。
  - 暂停期间切换 Overlay / Fusion 不新增请求（两条路径），恢复后显示最新模式。
  - 未激活或暂停时 `publish_view_rect` 不发布；`camera_reason` 默认值下相机标识与现在逐字相同，传入前缀时按前缀生成。
  - 真机（用户）：Step1 画面、Overlay / Fusion、Intensity、拖动缩放、patch 空降与现在一样；GPU 路径以真机结果为准。
- **执行记录**（2026-09-26，未提交）：
  - `ui/step1_gpu_binding.py`：暂停标志；`pause()`（停计时器、断开控制器两个事件）/ `resume()`（重连；暂停期间来源变过则先 `source_changed()`，否则为缺粗层的已画通道启动粗层，并对当前视野重新规划所有已画通道的精层——一次恢复刷新入口）/ `paused`；`source_changed`（暂停时只记下）、`update_viewport`、`refresh_display`、`_start_coarse_channel`、`_plan_fine_for_channel` 在暂停时不派发；粗层完成的回调在暂停时不接着规划精层（已到达的结果照常收下）。
  - `ui/step1_viewer_mount.py`：`camera_reason="step1"` 构造参数（默认值下标识与原来逐字相同）；`pause_requests()`（先 `deactivate()`，再暂停 GPU 绑定，或在 CPU 路径关闭控制器的视野请求）/ `resume_requests()`（先处理暂停期间的来源变化，GPU 绑定恢复或 CPU 路径把最新模式交给合成并打开视野请求，再 `activate()`）/ `paused`；`set_mode` 暂停时只记录；`_recompose_for_the_camera` 暂停时不合成；`publish_view_rect` 只在激活且未暂停时发布。
  - **申请之外的必要覆盖**：`source_changed` 在暂停时只记下原因、恢复时再重建（重建读取栈时新控制器会读总览，GPU 路径还会新建一个未暂停的绑定——不覆盖就无法兑现「暂停期间不发新请求」）。与审核要求覆盖模式切换同理，未扩大到别的文件。
  - 测试 `tests/test_viewer_pause.py` 12 条：GPU 绑定——暂停后相机事件（平移、Navigator 空降、手势结束）与 `update_viewport` 不产生请求；**粗层读取未完成时暂停、结果随后到达，粗层照常收下且不派发精层请求**；暂停期间勾选新通道、改颜色 / Intensity / 权重不产生请求；重复暂停 / 恢复不重复连接（按信号接收者计数）；恢复后为新通道启动粗层、为新视野只请求缺失的精层、全部到达后画面包含两个通道；暂停期间来源变化在恢复时被接手。mount（CPU 路径，沿用 `test_step1_viewer_mount.py` 的仿真窗口与合成金字塔）——暂停后移动相机、改草稿、改颜色 / 勾选 / Intensity、切换模式、patch 空降，合成代数与读取数都不变；恢复后以 Fusion 模式重新合成且像素等于参考值；暂停期间的来源变化在恢复时才重建；只有激活且未暂停的实例发布 Navigator 视野框；相机标识带构造时的前缀（默认 `step1`，传入 `step3` 时为 `step3-patch` 等）。反向注入：去掉暂停期间模式切换只记录、去掉视野框的前台判断，各有 1 条变红；粗层回调与 `_plan_fine_for_channel` 两道判断同时去掉时，粗层那条变红（只去其一会被另一道挡住）。
  - 回归：27 个模块（Step1 viewer 挂载 / 绑定 / host / 退出门、GPU takeover / request gate / overview skip / sources / ROI 裁切、共享相机、Navigator、patch 选择、Tissue Preview 契约、Step0 对比视图等），与 `git archive HEAD`（`d1f3f36`）逐条对比**无新增失败**。两边相同：`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`；`test_step1_montage_view.py` 第 27 条后 Qt 中止；5 个 GPU 模块不收集测试；`test_step1_gpu_takeover.py` 34 条因无真实 OpenGL 跳过。只在 HEAD 一侧出现 `test_step0_compare_tiles.py::test_a_pan_during_a_pending_switch_replans_for_the_new_viewport`（负载下偶发）。
  - **真机验收通过（用户 2026-09-26）**：Step1 画面、Overlay / Fusion、Intensity、拖动缩放、patch 与 Navigator 空降、步骤往返与改动前一样（含 GPU 路径）。

### 块 2b — Step3 与 Step1 共用显示范围、fusion 草稿与 Navigator 上下文（申请 v2，按独立审核修订，用户 2026-09-26 批准，过渡限制按 (b) 接受；已实施，**真机验收通过**）
- **必要性**：用户裁定 Step1 ↔ Step3 的勾选、权重、fusion 草稿、Overlay / Fusion 模式全局联动，Tissue Navigator 与 Step3 画面实时联动。现在 Step3 有自己的显示范围 step3（首次进入与 Step1 每次新确认后从已确认快照播种，`main_window.py:4448-4517`），通道面板在 Step3 只有勾选 / 颜色 / 名称且勾选不影响 fusion（`global_dock.py:226-264`、`:957-1025`），Navigator 在 Step3 画 Step1 发布的共享规格（`_SharedSpecTissueContext`，`main_window.py:354`），而该规格只在权重变化时重新发布（`_refresh_published_render_spec`，`:2143`；勾选与模式不更新）。
- **做法**（全部在主窗口的步骤切换里，不改通道面板、fusion 模型、显示状态与合成代码）：
  1. `_DISPLAY_SCOPES[3]` 由 `"step3"` 改为 `"step1"`；`_DOWNSTREAM_STEPS` 由 `(2, 3)` 改为 `(2,)`（只有 Step2 从已确认快照播种）。
  2. `_set_step_active`：显示范围切到 step1 时（进入 Step1 **或 Step3**）都调用 `_resync_step1_display_from_state()`——否则从 Step0 进 Step3 再回 Step1，范围不变，Step1 页面会漏掉 Step0 期间的变化。
  3. 通道面板：进入 Step3 时 `dock.set_step(1)`（与 Step1 相同的行：勾选即 fusion 命令、权重滑块与数值框），挂载位置仍由 `_mount_channels_dock(3)` 决定（Step3 页面的 `channels_host`）。
  4. Navigator：`_STEP_CONTEXTS[3]` 改为 Step1 的上下文（窗口本身，按 step1 范围的勾选、草稿与模式实时合成）；现有的重画请求（勾选 `request_frame(kind="visibility")`、权重、模式）在 Step3 里即按 Step1 的方式生效。Step3 的编辑策略不变（`_apply_navigator_policy_for_step(3)` 仍为只读，沙盒 ROI 在第 ⑤ 步）。`_SharedSpecTissueContext` 对 Step3 不再使用（保留注册，不删）。
  5. 会话自动保存：勾选的保存受 `_display_scope_is_step1()` 约束，Step3 现在在 step1 范围，Step3 的勾选与权重按现有机制写进当前 `step1_session.json`（用户已确认）。已确认快照只在 Step1 点保存时变化。
  6. `UI_SURFACE_RULES.md` 精确替换：第 196-217 行「四步的勾选各自独立」改为「Step0、Step2 各自独立；Step1 ↔ Step3 共用勾选、当前通道、权重与 fusion 草稿；Step2 首次进入与 Step1 每次新确认后从已确认快照播种」；第 211 行「Fusion participation and weights belong to Step1's scientific draft alone」改为「属于 Step1 的科学草稿，Step3 编辑的是同一份」；第 222 行「Step2 / Step3 rows -- tick, swatch, name」改为只写 Step2，Step3 行与 Step1 相同；第 292 行「Weights -- edited in Step1's rows」改为「Step1 与 Step3 的行」。「进入一个 Step 只是重画、不是命令」一条保留。
- **不在本块**：Step3 的新页面、Reset / Load weights 按钮、Overlay / Fusion 按钮与模式同步、第二个 viewer（均在 2c）。
- **过渡限制（明确接受，不改旧页面）**：旧 Step3 页面在 2b 与 2c 之间仍在，但它的 `_display_scope_is_shared()` 只接受 `""` / `"step3"` 范围（`step3_page.py:1466-1478`），改用 step1 后 `_on_shared_display_changed` 直接返回：**旧页面的画面不会跟随勾选加载或重画**。2c 整体删除旧页面，不为它改代码；2b 的真机验收只看通道面板、Step1 同步与 Navigator，不看旧页面的画面。
- **白名单**：`ui/main_window.py`（`_DISPLAY_SCOPES`、`_DOWNSTREAM_STEPS`、`_STEP_CONTEXTS`、`_set_step_active` 中的重新同步条件与 `dock.set_step` 的参数）；`UI_SURFACE_RULES.md`（上述条款）；现有测试**仅限**以下 5 个文件、且**仅限与新裁定直接冲突的断言**（Step3 独立范围与播种、Step3 行只有勾选 / 颜色 / 名称、Step3 的 Navigator 用共享规格）：`tests/test_downstream_display_seed.py`、`tests/test_global_channel_dock.py`、`tests/test_step0_step1_display_isolation.py`、`tests/test_block01_tissue_preview_contract.py`、`tests/test_channel_row_template.py`；每条改动在执行记录中列出。其他测试失败不因「涉及 Step3」而被当作预期变化——须停下说明；新测试 `tests/test_step3_shares_step1.py`；本文档。
- **不改的范围**：`GlobalChannelDock`、`FusionDomainModel`、`ChannelDisplayState`、合成与渲染代码、Step2 的行为、Step0 的行为、Intensity 窗口、Navigator 编辑策略、Step3 页面。
- **风险**：Step3 的操作会写 Step1 的会话（已确认）；旧 Step3 页面的过渡显示；按 step 号判断的其他代码若假设「Step3 的范围是 step3」会受影响（回归覆盖）。回退：恢复主窗口这四处与规则文件。
- **验收门**：
  - 完整公开操作（真实主窗口，合成项目）：进入 Step3，在通道面板勾选一个 Step1 未参与 fusion 的通道并调其权重 → 回到 Step1，通道面板与 fusion 草稿是同样的设置 → 当前会话记住草稿；已确认快照（哈希）与 Step2 的勾选保持原样，直到在 Step1 明确保存。
  - Step3 的通道面板行与 Step1 相同（有权重控件），勾选即改变 fusion 参与；Step2 的行仍只有勾选 / 颜色 / 名称，Step2 仍从已确认快照播种、不受 Step3 操作影响。
  - Navigator：Step3 激活的是 Step1 的上下文；在 Step3 勾选或调权重时发出重画请求，合成出的帧包含新设置；进入 Step3 时沿用 Step1 当前的 Overlay / Fusion 模式（上下文对两种模式的合成可在测试里直接验证，但**在 Step3 通过模式按钮切换并使两页与 Navigator 同步属于 2c 的验收**）。
  - 从 Step0 进入 Step3、再回 Step1：Step1 页面反映 Step0 期间的共享变化（颜色 / Intensity），无残留。
  - 进入 Step3 本身不产生命令：不改勾选、不改权重、不产生 fusion 修订、不触发会话保存。
  - 回归：相关模块与 HEAD 逐条对比，除按裁定改写的测试外无新增失败。
  - 真机（用户）：只看 Step1 与 Step3——Step3 通道面板与 Step1 相同（有权重控件）；Step3 里勾选一个 Step1 未用的通道并调权重，Navigator 实时变化；回到 Step1 是同样的设置；旧 Step3 页面的画面不作要求（过渡限制）。Step2 页面按块 L2 不显示通道列表，2b 不改 Step2 的任何行为，真机不验 Step2。
- **执行记录**（2026-09-26，未提交）：
  - `ui/main_window.py`：`_STEP_CONTEXTS[3] = _CTX_STEP1`；`_DISPLAY_SCOPES[3] = "step1"`（上方注释同步改正）；`_DOWNSTREAM_STEPS = (2,)`；`_set_step_active` 中重新同步条件改为「进入的 Step 的范围是 step1」；`dock.set_step(1 if active == 3 else active)`。
  - `UI_SURFACE_RULES.md`：「What a step keeps to itself」整条按裁定改写（Step0 / Step2 各自独立；Step1 ↔ Step3 一个范围，共用勾选、当前通道、权重与草稿；Step3 的编辑与 Step1 一样写进会话，已确认快照只在 Step1 保存时变；只有 Step2 播种；进入 Step 只是重画；Step3 的 Tissue Preview 画 Step1 的实时上下文）；「Step2 / Step3 rows」拆为 Step2 三项、Step3 同 Step1；「Weights」加上 Step3 的行。`tests/test_ui_surface_contract.py` 通过。
  - 按裁定改写的现有测试（仅与新裁定直接冲突的断言）：
    - `test_downstream_display_seed.py`：模块说明；`test_an_uncommitted_draft_does_not_reach_step2_or_step3`（Step3 改为等于 Step1 的勾选）；`test_step2_and_step3_open_on_the_committed_channels`（Step3 改为等于 Step1）；`test_a_step3_tick_writes_back_to_nobody` 改为 `test_a_step3_tick_is_step1_s_tick_and_leaves_step2_alone`；`test_a_new_commit_re_seeds_the_downstream_steps_on_next_entry` 的 Step3 断言改为等于 Step1。
    - `test_global_channel_dock.py`：字段表 Step3 = Step1（显示权重控件）；`test_a_hidden_control_cannot_command_its_owner` 与 `test_the_tick_box_is_display_alone_outside_step1` 的步骤列表去掉 3；`test_the_mixed_marker_is_shown_only_where_the_weight_is` 的 Step3 显示 `CD3 *`。
    - `test_block01_tissue_preview_contract.py`：Step3 的激活上下文为 STEP1（`test_every_step_transition_moves_the_context_and_the_generation`、`test_a_mapping_drag_in_step2_and_step3_publishes_intermediate_frames`、`test_a_stale_frame_is_refused_between_two_steps_of_the_SAME_mode`，后者说明同步改写；Step2 的旧帧在 Step3 仍因归属不符被拒）。
    - `test_step0_step1_display_isolation.py`、`test_channel_row_template.py`：无需修改。
    - **白名单外（用户 2026-09-26 授权）**：`test_step1_checkbox_is_the_fusion_command.py::test_outside_step1_the_same_gestures_are_display_only` 的步骤列表由 `(0, 2, 3)` 改为 `(0, 2)`（Step3 的勾选按裁定即 fusion 命令，由新测试覆盖）。
  - 新测试 `tests/test_step3_shares_step1.py` 8 条：完整公开操作（Step3 里勾选 CD8 并把权重调到 0.6 → Step1 行与状态相同 → 会话被要求保存；已确认快照与哈希不变、Step2 勾选不变）；Step3 行 = Step1 行、Step2 行无权重；从 Step0 / Step1 / Step2 进入 Step3 不改草稿、不改勾选、不触发保存；Step0 → Step3 重新同步一次、Step3 → Step1 不重复、Step2 → Step3 再同步；Tissue Preview 在 Step3 为 STEP1 上下文、勾选使绿色通道出现、调低权重使其变暗；进入 Step3 沿用 Step1 的 Fusion 模式。反向注入主窗口 4 处改动（上下文、范围、重新同步条件、通道面板步骤），分别使 2 / 5 / 1 / 3 条变红。
  - 回归：46 个模块（步骤切换、显示范围、通道面板、Tissue Preview、会话、Step1 viewer 等），与 `git archive HEAD`（`e13be53`）逐条对比，除上述授权修改的一条外**无新增失败**；两边相同：`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`、`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`（本机字体差异）。
  - **真机验收通过（用户 2026-09-26）**：按上面只看 Step1 / Step3 的 6 步。

### 块 2c 只读调查（2026-09-26）
- **旧页面的依赖**：生产代码里只有 `ui/main_window.py` 引用旧页面：构造（`:1484`，无参数；构造时会读 `step3_input_files.json`）、`set_display_services`、`go_back`（保留）、`go_step4`（从未发出）、页面栈固定在第 3 格（`setCurrentIndex(3)`）、`set_channel_context` / `set_output_dir`（`_go_to_step3` 与 `_on_step2_complete`）、`_stop_loaders`（鸭子类型）、`channels_host()`（**新页面必须保留，且与 Step1 的宿主是不同的控件**）。`_skip_to_step3` 无调用者。Step4 从 Step3 得不到任何东西：`step3_output` 只是 `step2_output` 的拷贝，`is_sequential_flow` 从不为真。`ChannelWorkbench` 另由 Step0 与 Step1.5 使用，删除 Step3 的标签页不会连带坏掉别的功能（Step1.5 才是正式的 remap 保存路径）。两个配置文件只有旧页面自己读写。`utils/mask_renderer.py` 与 `utils/roi_project.py` 仍被别处使用，不会变成死代码。
- **新页面的做法**：Step1 左栏的 Channels 框、Show all、Intensity 的样式都取自 Step0 的控件与主窗口里的模块级辅助函数。**不能再建第二个 ConfigPanel**：它构造时会用空列表重建共用的通道面板，清掉所有行。Reset / Load weights 改为两个普通按钮，调用现有 `self.config.zero_marker_weights()` 与 Load weights 的流程（`load_weights_from_step1_session`）。Overlay / Fusion 的 `set_preview_mode` 会无条件同步 Step1 的两个按钮，Step3 的一对按钮要加在同一处。共用列宽：把 Step3 的分隔条加进 `_channel_column_splitters`，并把 `_wire_channel_column_sync` 的唯一调用移到 Step3 构造之后。
- **第二个 viewer 要接线的地方**：构造（照 `_step1_whole_slide()`，`install` 必须给一个占位控件）；步骤切换（Step1 离开时由 `deactivate()` 改为 `pause_requests()`，进入时先 `sync_source` 再 `resume_requests()`；Step3 同样）；相机（`_on_step3_camera`，`_capture_camera_of` / `_apply_shared_camera_to` 加 Step3，`(0, 1)` 的门槛改为 `(0, 1, 3)`）；Tissue Preview 点击改为按当前步骤路由；handoff 重绑；换数据集时关闭；关窗时释放。两个常驻 GPU 实例约各占 512 MiB 显存纹理上限。
- **进入 Step3 的数据前提**：viewer 需要 `loader.filepath`、校正决定、ROI 等，只有 Step0 Save、进入 Step1 时自动读取 handoff、或加载会话时才会填上。重启后没做这些就直接进 Step3，viewer 打不开，也没有退路。所以 Step3 的入口要沿用 Step1 的检查与自动读取，不满足时提示并留在原处。
- **已有的小问题（advisory，不在 2c 处理）**：`set_preview_mode(..., reconcile=False)`（会话恢复、Load weights）不会把模式交给 viewer；换数据集时 `_step1_preview_mode` 被重置，按钮却不跟着变。

### 块 2c-1 — 新 Step3 页面（左栏 + 右栏框架）并删除旧页面（申请 v2，按独立审核修订，用户 2026-09-26 批准，功能空档已接受；已实施，**真机验收通过**）
- **范围**：2c 拆成两块。2c-1 做页面、删除旧页面，并完成**两页的 Overlay / Fusion 按钮、共享模式与 Tissue Preview 的模式同步**；viewer 位置先放占位提示。2c-2 接入第二个 viewer（生命周期、相机、Tissue Preview 点击路由），并把**第二个 viewer 接到这个共享模式**。
- **功能空档（须用户明确接受）**：2c-1 删除旧页面后、2c-2 完成前，Step3 右栏只有占位提示，**没有任何图像**（旧页面的缩略图与放大视图已删，第二个 viewer 尚未接入）。2c-1 不是完整的 Step3 交付。
- **做法**：
  - 重写 `ui/step3_page.py`（类名仍为 `Step3Page`，页面栈仍在第 3 格）。页面只负责摆放，所有动作由主窗口注入，页面自己不读写任何文件：
    - 左栏：`Channels` 框（与 Step1 相同的样式）；Show all / Intensity 一行（Show all 直接复用 Step1 的行为 `dock.set_all_visible`：对每个允许批量切换的 marker 行执行勾选命令，显示并参与 fusion，没有权重答案的按既有规则处理；核通道不允许批量切换，保持原状态；不新增规则。Intensity 调 `_show_intensity_window`）；Reset weights / Load weights 一行（调 `self.config.zero_marker_weights()` 与 Load weights 流程，对话框父窗口为 Step3）；`channels_host()` 给出通道面板的挂载位置。
    - 右栏：一行 Overlay / Fusion 按钮（与 Step1 相同的样式），点击调 `set_preview_mode`；下方是 viewer 的位置（2c-1 先放一个占位提示，2c-2 由第二个 viewer 接管）。
    - 底部：`← Back to Step 2`（保留 `go_back`）。
    - `channel_column_splitter()`：左右分隔条，加入共用列宽。
  - `ui/main_window.py`：
    - 构造新页面、注入动作与样式来源；删除 `set_display_services`、`go_step4` 的接线。
    - `set_preview_mode` 同步 Step3 的一对按钮（与 Step1 的两个按钮一样无条件 `setChecked`）。
    - `_go_to_step3`：去掉 `set_channel_context` / `set_output_dir`；记下 Step2 传来的运行目录（给第 ④ 步选 mask 用，2c-1 不使用）；**沿用 Step1 的入口检查**（几何保存未完成时等待、handoff 已绑定但未读取时自动读取），不满足时留在原处，**拒绝原因以对话框显示在当前可见的页面上**（不写到 Step1 隐藏页面的状态文字里）。
    - 写文件的边界：上下文已就绪时，单纯切换到 Step3 **不触发**会话保存；需要自动读取 handoff 时，**沿用现有读取流程及其会话保存副作用**（`_load_step0_roi_result` 会安排一次会话保存，`main_window.py:2941`），不为此改造读取流程。
    - 删除 `_on_step2_complete` 里对旧页面的调用、`_go_to_step2` / `_go_to_step4` 里的 `_stop_loaders` 分支、无调用者的 `_skip_to_step3`。
    - `_channel_column_splitters` 加入 Step3 的分隔条；`_wire_channel_column_sync` 的唯一调用移到 Step3 构造之后。
  - 删除：旧页面的全部内容（缩略图、矩形、patch 放大视图、Channel Overlay 面板、`Channel Remap Review / QC` 标签页、隐藏的开发者路径覆盖）；不再读写 `step3_input_files.json` 与 `step3_channel_overlay_config.json`（已存在的旧文件不删）。
  - 文档：`docs/user_guide.md` 与 `docs/用户指南.md` 的 Step3 一节只描述当前可用的功能：左栏通道与权重、模式按钮；**右栏暂为占位，整张图浏览待 2c-2，mask 待第 ④ 步**；去掉「在缩略图上框一小块」的描述与对应截图占位。`UI_SURFACE_RULES.md` 加一条 Step3 页面的描述。
- **白名单**：`ui/step3_page.py`（重写）；`ui/main_window.py`（上述各处）；`UI_SURFACE_RULES.md`；`docs/user_guide.md`、`docs/用户指南.md`（Step3 一节）；测试：删除或改写只测旧页面功能的测试——`tests/test_channel_workbench.py` 中构造 `Step3Page` 的 15 条、`tests/test_global_channel_dock.py` 的 `test_step3_marker_rows_carry_no_public_controls` 与 `test_step3_reads_the_shared_visibility_and_colour`、`tests/test_step0_step1_display_isolation.py::test_step3_does_not_follow_step0_or_step1_ticks`、`tests/test_step1_step2_handoff_e2e.py::test_step3_finds_the_raw_slide_in_the_session`（其保护的 `raw_ome_path` 会话字段只有旧页面读取；会话照常写入该字段，不改）；新测试 `tests/test_step3_page.py`；本文档。其他测试失败须停下说明。
- **不改的范围**：`ChannelWorkbench` 模块本身（Step0、Step1.5 继续使用，其中提到 Step3 的按钮文字与提示另记 advisory）；`utils/mask_renderer.py`；viewer、mount、GPU 代码；Step1 页面；Step2、Step4；显示范围与通道面板（2b 已定）。
- **风险**：删除约 3700 行旧代码；从 Step2 的完成对话框进入 Step3 时若上下文不满足会被拦下（按 Step1 的规则）；2c-1 与 2c-2 之间 Step3 右栏只有占位提示，没有图像。回退：恢复旧 `ui/step3_page.py` 与主窗口相关各处。
- **验收门**：
  - 新页面：左栏有 Channels 框、Show all / Intensity、Reset / Load weights 与通道面板（Step1 的行，带权重），右栏有 Overlay / Fusion 与 viewer 占位；样式与 Step1 相同的控件取自同一来源。
  - Show all：marker 显示并参与 fusion（没有权重答案的按既有规则处理），核通道不被批量操作改变；再点一次取消。与 Step1 的 Show all 行为相同。
  - Reset weights 把 marker 权重清零，Step1 同步；已确认快照不变。
  - Load weights：加载动作不直接改写来源文件，当前会话仍按既有机制自动保存。分别验收：选**历史会话文件**时，来源文件内容不变、共用草稿变为该文件的权重、Step1 同步；选**当前的 `step1_session.json`** 时，草稿恢复为其中的权重，之后按既有机制被自动保存改写（预期）；两种情况下已确认快照都不变。
  - Step3 的 Overlay / Fusion 与 Step1 的按钮始终一致：在 Step3 点 Fusion，Step1 的按钮与 Tissue Preview 都是 Fusion，反之亦然。
  - 共用列宽：拖动 Step0、Step1、Step2、Step3 任一分隔条，四页一起动。
  - 进入 Step3：上下文已就绪时正常进入，且单纯切换不触发会话保存；handoff 已绑定未读取时自动读取（允许其既有的会话保存）；不满足时留在原处，拒绝原因以对话框显示在当前可见页面上。
  - `_channels_host_for(3)` 与 Step1 的宿主不同，通道面板在四页之间移动（现有测试 `test_the_one_panel_moves_between_the_steps_hosts`）。
  - 回归与 HEAD 逐条对比，除上述列出的测试外无新增失败。
  - 真机（用户）：Step3 的外观与 Step1 的左栏、模式按钮一致；上述操作正常；在 Step3 切换模式时 Step1 的按钮与 Tissue Preview 同步；右栏暂为占位提示（功能空档，已接受）。
- **执行记录**（2026-09-26，未提交）：
  - `ui/step3_page.py` 重写（3705 行 → 约 170 行）：`Step3Page` 只负责摆放，不读写任何文件、不持有状态；`assemble()` 接收主窗口建好的控件；`channels_host()`（独立宿主）、`channel_column_splitter()`、`viewer_layout()` / `viewer_notice()`（给 2c-2）；`VIEWER_PENDING_TEXT` 占位提示；`go_back`。
  - `ui/main_window.py`：新增 `_build_step3_page`（标题栏同 Step1、带 Tissue Navigator；Show all 用 Step1 的宽度匹配复选框与 Step0 的样式来源，调 `dock.set_all_visible`；Intensity 调 `_show_intensity_window`；Reset weights 调 `self.config.zero_marker_weights()`；Load weights 调新增的 `_load_weights_dialog(parent)`——与 `ConfigPanel._load_weights_from_file` 同一流程，对话框父窗口为 Step3；Overlay / Fusion 用 `MODE_BUTTON_QSS`，点击调 `set_preview_mode`）；`set_preview_mode` 同步 Step3 的一对按钮；`_go_to_step3` 改为先过 `_step3_entry_ready`（与 `_go_to_step1` 相同的两项检查），记下 `_step3_run_dir`；新增 `_say_step3_refusal`；删除 `set_display_services` / `go_step4` 接线、`_on_step2_complete` 中对旧页面的调用、`_go_to_step2` / `_go_to_step4` 的 `_stop_loaders` 分支、`_skip_to_step3`；`_channel_column_splitters` 加入 Step3；`_wire_channel_column_sync` 的唯一调用移到 Step3 构造之后。
  - **与申请的差异**：拒绝原因仍以对话框显示在当前页面上，但改为**非模态**（`_say_step3_refusal`，重复拒绝复用同一个框）。模态对话框会阻塞事件循环；离屏测试 `test_main_window_step1_5.py::test_navigation_alone_creates_no_outputs`（未就绪时调用 `_go_to_step3()`）因此卡死，改为非模态后通过。
  - 删除的旧页面测试（与白名单一致，共 19 条）：`test_channel_workbench.py` 中构造 `Step3Page` 的 15 条；`test_global_channel_dock.py` 的 2 条；`test_step0_step1_display_isolation.py` 的 1 条；`test_step1_step2_handoff_e2e.py` 的 1 条。
  - 新测试 `tests/test_step3_page.py` 10 条：页面结构与样式来源（Channels 框的样式、Show all 与 Step0 相同、模式按钮 `MODE_BUTTON_QSS`、占位提示、只有 Fusion / Viewer 两个标签页、Step3 宿主与 Step1 不同、进入后通道面板挂在 Step3 且带权重控件）；Show all 使三个 marker 显示并参与 fusion、再点取消，核通道不变；Reset weights 清零、Step1 同步、已确认快照不变；Load weights 两种情况（历史文件 / 当前会话文件）——对话框从 Step3 弹出、权重读入、来源文件字节不变、已确认快照不变、会话被要求保存、Step1 同步；两对模式按钮一致且在 Step3 点 Fusion 后 Tissue Preview 为 Fusion、再点已选中的按钮仍保持选中；四页共用列宽（拖 Step3，Step0 / Step1 / Step2 跟随）；就绪时进入不触发保存；handoff 已绑定未读取时先自动读取；不满足时留在原页、非模态对话框显示原因、重复拒绝复用同一个框。反向注入 3 处（Step3 按钮同步、Step3 不在列宽列表、跳过入口检查）分别使 1 / 1 / 2 条变红。
  - 文档：`docs/user_guide.md`、`docs/用户指南.md` 的 Step3 一节改为「正在重建」——只写现在可用的功能，写明右侧暂为占位、整张图浏览在下一次更新、掩膜在其后；概览表的 Step3 一行同步。`UI_SURFACE_RULES.md` 新增「Step3's page」一条。
  - 回归：75 个构造主窗口或涉及步骤切换 / 显示范围 / 通道面板 / `ChannelWorkbench` 的模块，与 `git archive HEAD`（`d69402f`）逐条对比**无新增失败**；通过数的差异正好是删除的旧页面测试（15 / 2 / 1）。两边相同：`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`、`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`（字体差异）、`test_step0_method_prefetch.py::test_the_neighbouring_channel_is_still_prepared_as_well`；HEAD 一侧另有 `test_step0_method_prefetch.py::test_a_remounted_coordinator_does_not_reuse_a_cancelled_generation`（负载下偶发）。
  - **真机验收通过（用户 2026-09-26）**。

### 块 2c-2 — Step3 的第二个 viewer，与 2c-1 的 Navigator 权限修补（申请 v2，按独立审核修订，用户 2026-09-26 批准；Step3 只读至第 ⑤ 步；已实施，**真机验收通过（第 1–3 项）**）
- **2c-1 留下的缺陷（须修补）**：Step3 标题栏的 `Tissue Navigator` 按钮接到了 Step1 的 `_show_tissue_navigator`，它调用 `show_navigator(_CTX_STEP1, roi_policy="delete_only", patch_editable=True)`，而 `show_navigator` 带参数时会先改写编辑权限（`block01_display.py:2248-2249`）。所以在 Step3 点这个按钮，Navigator 会变成可删除 ROI、可编辑 patch，这些编辑会写进 Step0 的项目文件，违反「沙盒 ROI（第 ⑤ 步）完成前 Step3 的 Navigator 只读」的裁定。修补：Step3 的按钮改为调用新的 `_show_step3_tissue_navigator`，以只读权限（`roi_policy="read_only"`、`patch_editable=False`，与 `_apply_navigator_policy_for_step(3)` 相同）打开。
- **用户的目标权限（2026-09-26）**：Step3 里**可以**编辑 ROI 与 patch，但编辑不保存、不改本地文件——即第 ⑤ 步「Navigator 沙盒 ROI」。现在 Navigator 的每次编辑都经 Step0 的唯一模型写进 `roi_config.json` 等文件（`_reconcile_roi_edit` → `_persist_geometry_edit`），没有「只在内存里」的路径，所以在第 ⑤ 步完成之前 Step3 必须只读；本块的修补只是堵住现在会写文件的口子，第 ⑤ 步再实现可编辑、不保存。
- **必要性**：用户裁定 Step3 是 Step1 的简化版，右侧是整张图 viewer，可拖动缩放、Navigator 空降；2c-1 之后右栏只有占位提示（已接受的功能空档）。
- **做法**（全部在主窗口；viewer / mount / GPU 代码不改，只使用 2a 的暂停 / 恢复与 `camera_reason`）：
  1. `_step3_whole_slide()`：照 `_step1_whole_slide()` 建第二个 `Step1WholeSlideMount(self, parent=self, camera_reason="step3")`，`camera_sink = self._on_step3_camera`，`install(page.viewer_layout(), page.viewer_notice())`——占位提示成为它的回退控件：viewer 打开后隐藏，打不开时显示并改写为「整张图 viewer 打不开：原因」。
  2. 步骤切换（`_step1_whole_slide_step_changed_inner`）：两个 viewer 都常驻。离开 Step1 时 Step1 的 viewer 由 `deactivate()` 改为 `pause_requests()`；进入 Step1 时先 `sync_source`，已暂停则 `resume_requests()`，否则 `activate()`。Step3 的 viewer 同样：不在 Step3 时暂停，进入时同步来源并恢复；第一次进入时 `open()`，失败记在 `_step3_mount_refused_for`（同一张切片不重试），成功后 `set_mode(当前模式)`。
  3. 模式：`set_preview_mode` 在给 Step1 的 viewer `set_mode` 的同一处也给 Step3 的 viewer `set_mode`（后台的那个只记录，恢复时按新模式合成，2a 已保证）。
  4. 相机：新增 `_on_step3_camera`（只在当前是 Step3 时记录）；`_capture_camera_of` / `_apply_shared_camera_to` 加 Step3 分支；`_set_step_active` 中应用共享相机的条件由 `(0, 1)` 改为 `(0, 1, 3)`。Step1 ↔ Step3 往返保持同一位置。
  5. Tissue Preview 点击：`_on_step1_tissue_navigate` 按当前步骤路由——Step1 给 Step1 的 viewer，Step3 给 Step3 的 viewer。**连接不能依赖 Step1 的 viewer 已创建**：现在 `navigator_created` 的连接只在 `_step1_whole_slide()` 里建立，重启后直接进 Step3 时不存在。`_step3_whole_slide()` 创建 viewer 时同样以唯一连接接上 `navigator_created`，并在 Navigator 已存在时立即调用 `_wire_step1_tissue_navigation()`（`Qt.UniqueConnection`，不会重复连接）。视野框只由前台的 viewer 发布（2a 已保证）。
  6. handoff 重绑：`_step1_sync_whole_slide_source` 也同步 Step3 的 viewer，并清除它的失败记录。**重绑失败时的回退与首次打开相同**：同一数据集换 ROI 或 handoff 更新后 `sync_source` 失败（异常），Step3 的 viewer 回退——隐藏 viewer、显示占位提示并写明原因，不把旧来源的画面当作新来源继续显示；失败记录在 handoff 更新或换数据集后清除（与第 7 条一致）。
  7. 换数据集：`_discard_step1_dataset_state` 关闭 Step3 的 viewer（`close()`，回退控件恢复为占位提示，失败记录清除），下次进入 Step3 按新数据重开。Step1 viewer 不在换数据集时关闭（原有行为，advisory 已记）。
  8. 关窗：`closeEvent` 在关闭 Step1 的 viewer 的同一处关闭 Step3 的 viewer。
- **白名单**：`ui/main_window.py`（上述各处与 `_show_step3_tissue_navigator`）；`ui/step3_page.py`（仅在需要时增加改写占位提示文字的方法）；`UI_SURFACE_RULES.md`（Step3 页面一条：viewer 已接入）；`docs/user_guide.md`、`docs/用户指南.md`（Step3 一节：可浏览整张图、拖动缩放、Navigator 空降；mask 待第 ④ 步）；测试：新测试 `tests/test_step3_viewer.py`；`tests/test_step1_viewer_takeover.py` 与 `tests/test_step1_shared_camera.py` 中替换 `Step1WholeSlideMount.__init__` 的桩函数签名加上 `**kwargs`（否则构造 Step3 的 viewer 时 `camera_reason` 会报错），不改其断言；本文档。其他测试失败须停下说明。
- **不改的范围**：`step1_viewer_mount.py`、`step1_gpu_binding.py`、host、GPU 层与着色器；patch 按钮条（第 ③ 步）；mask（第 ④ 步）；Navigator 的沙盒 ROI（第 ⑤ 步）；`set_preview_mode(reconcile=False)` 不把模式交给 viewer 的已有问题（advisory，本块不改：Step3 的 viewer 与 Step1 的在同一处接收模式，行为一致）。
- **风险**：两个常驻 viewer 的内存与显存上限约为一个的两倍（用户已接受）；Step1 离开时改为暂停是 Step1 行为的变化（后台不再读取；回来时补齐）；GPU 路径本机测试环境大部分跳过，须真机确认。回退：恢复主窗口这几处。
- **验收门**：
  - Navigator 权限：在 Step3 点 `Tissue Navigator` 后编辑权限仍为只读；在合成项目中验证 Step3 下删除 ROI、编辑 patch 都被拒绝，`roi_config.json` / `patch_config.json` / `step0_roi_result.json` 不变；Step1 的按钮仍给 Step1 的权限（可删除 ROI、编辑 patch）。
  - 进入 Step3 打开第二个 viewer，装在 Step3 右栏、占位提示隐藏；打不开时占位提示显示原因，同一张切片不重复尝试。
  - 后台：在 Step3 时 Step1 的 viewer 已暂停，在 Step1 / 其他步骤时 Step3 的 viewer 已暂停；各自在后台期间**不派发新请求，已开始的读取允许完成**（2a 的契约）；回来时补齐。
  - 模式：用本块接入的按钮（Step3 或 Step1 的 Overlay / Fusion）切换时，两个 viewer 的模式都跟着变（后台的恢复后按新模式显示），Tissue Preview 一致。**不**宣称会话恢复 / Load weights（`reconcile=False`）路径下 viewer 模式一致（已记 advisory）。
  - 相机：Step1 → Step3 → Step1 位置保持；在 Step3 拖动后回 Step1 是同一位置。
  - Navigator 空降：在 Step3 点 Tissue Preview 空降，移动的是 Step3 的 viewer，视野框跟随 Step3；**未进入过 Step1、直接进入 Step3** 后打开 Navigator 空降可用；**Navigator 已打开时进入 Step3** 空降可用；Step1 ↔ Step3 往返后，一次点击只移动当前的 viewer、且只移动一次。
  - 重绑失败：同一数据集换 ROI / 更新 handoff 后重绑失败时，Step3 显示原因、不再显示旧来源的画面；handoff 再次更新后会重试。
  - 换数据集后 Step3 的 viewer 已关闭，再进 Step3 按新数据打开；关窗时两个 viewer 都释放。
  - 回归与 HEAD 逐条对比，除上述两个桩函数签名外无新增失败。
  - 真机（用户）：Step3 右栏显示整张图，拖动缩放、Intensity、Overlay / Fusion 与 Step1 一致，Navigator 空降到 Step3，Step1 ↔ Step3 往返位置不变；**同一来源、数据已准备好的热切换**无明显等待（首次打开与来源变化后的等待单独记录耗时，不以常驻推定为无等待）；Step3 里 Navigator 不可编辑（第 ⑤ 步再改为可编辑不保存）。

- **执行记录**（2026-09-26，未提交）：
  - Navigator 权限修补：Step3 的 `Tissue Navigator` 按钮改接新增的 `_show_step3_tissue_navigator`（`show_navigator(_CTX_STEP3, roi_policy="read_only", patch_editable=False)`）。
  - `ui/main_window.py`：新增 `_step3_whole_slide`（第二个 mount，`camera_reason="step3"`，装进 Step3 的 viewer 位置、占位提示作回退控件；创建时以唯一连接接上 `navigator_created`）、`_step3_viewer_shown`（成功时显示 viewer、隐藏提示——`restore_legacy` 之后 `install` 以外没有代码会重新显示 viewer）、`_step3_viewer_failed`（暂停、回退、提示写明原因、记下失败）、`_step3_follow_step`、`_close_step3_viewer`、`_on_step3_camera`、`_step3_sync_whole_slide_source`；`_step1_whole_slide_step_changed_inner` 拆出 `_step1_follow_step`，先暂停要离开的 viewer、再恢复要进入的；Step1 离开时由 `deactivate()` 改为 `pause_requests()`，进入时已暂停则 `resume_requests()`；`_capture_camera_of` / `_apply_shared_camera_to` 加 Step3；共享相机条件改为 `(0, 1, 3)`；`_on_step1_tissue_navigate` 按当前步骤路由；`_step1_sync_whole_slide_source` 同时重绑 Step3；`_discard_step1_dataset_state` 关闭 Step3 的 viewer；`closeEvent` 关闭 Step3 的 viewer；`set_preview_mode` 在同一处给 Step3 的 viewer `set_mode`。
  - `ui/step3_page.py`：新增 `set_viewer_notice(text=None)`。
  - 测试桩：`tests/test_step1_viewer_takeover.py` 与 `tests/test_step1_shared_camera.py` 中替换 `Step1WholeSlideMount.__init__` 的桩函数加上 `**kwargs`（未改断言）。
  - 新测试 `tests/test_step3_viewer.py` 10 条：Step3 按钮打开的 Navigator 保持只读，新建 ROI、删除 ROI、增删 patch、改 patch 几何都不生效，`roi_config.json` / `patch_config.json` / `step0_roi_result.json` 不变，Step1 的按钮仍给 Step1 的权限；进入 Step3 打开独立的第二个 viewer、装在 Step3 位置、提示隐藏、`camera_reason` 为 step3；只有前台的 viewer 读取（Step3 时 Step1 已暂停、改草稿与移动相机不使 Step1 读取；反之亦然；Step2 时两者都暂停）；两对模式按钮使两个 viewer 的模式一致（后台的记录）；Step3 → Step1 与 Step1 → Step3 两个方向位置保持；未建过 Step1 的 viewer 时 Navigator 出现后点击移动 Step3；Navigator 先打开、多次往返后一次点击只移动当前的 viewer 一次；打开失败时提示写明原因、同一张切片不重试；重绑失败时回退（提示原因、viewer 隐藏并暂停、不再读取），handoff 再更新后重试成功；换数据集关闭 Step3 的 viewer、提示恢复默认；关窗两个都关闭。反向注入 5 处（按钮修补、Step3 离开不暂停、Step3 创建时不接 `navigator_created`、Step3 路由、相机条件）各使至少 1 条变红（相机一条补了 Step1 → Step3 方向后才能被测出）。
  - 文档：`UI_SURFACE_RULES.md` 的 Step3 页面一条（viewer 已接入、只读 Navigator）；两份用户指南的 Step3 一节（可浏览整张图、拖动缩放、Navigator 空降、Navigator 只能看、mask 待后续）。
  - 回归：78 个模块，与 `git archive HEAD`（`455d127`）逐条对比**无新增失败**。只在改后一侧出现的 `test_step0_method_prefetch.py::test_a_remounted_coordinator_does_not_reuse_a_cancelled_generation`（上一轮只在 HEAD 一侧出现）与 StarDist ROI 等价一条，单独各重跑 3 次都通过，属负载下偶发；只在 HEAD 一侧出现 `test_preseg_run.py::test_the_job_equals_the_steps_done_by_hand`。两边相同的已知失败见前几块。`test_step0_channel_conditioning.py` 在两边都卡住（上一轮 2c-1 的回归中两边同样卡住、失败位置相同），本轮手动结束，未参与对比（已有问题，advisory）。
  - **真机验收（用户 2026-09-26）**：第 1 项（Step3 显示整张图，拖动缩放、Intensity、Overlay / Fusion 与 Step1 一致）、第 2 项（Step1 ↔ Step3 位置保持、热切换无明显等待）、第 3 项（Navigator 空降到当前步骤的 viewer）**通过**。第 4 项：只读按设计生效；用户的目标是 Step3 可编辑 ROI / patch 但不保存（第 ⑤ 步），只读只是过渡。第 5 项（重启后直接进 Step3）：程序目前没有重新加载历史会话的入口，以后再测。
  - **偶发问题（待观察，未改代码）**：一次真机操作中，Step0 保存 → 直接进 Step3（有图）→ 回 Step1，Step1 的 viewer 全黑，提示 `fine budget refused for DAPI`；再次操作无法复现。机制：GPU 显示的规则是通道第一次出现时要等精细层到齐才显示（`step1_gpu_binding.py` 的 `_publish_current`，`test_a_first_channel_over_the_fine_budget_stays_off_the_screen` 锁定），精细层因超预算被拒时通道永远不出现。推测的触发：Step1 的 viewer 第一次打开时套用 Step3 的共享相机，此刻窗口尺寸可能尚未排好，视野与选中的精细层级不匹配。再出现时请先试滚轮缩放 / 拖动 / 改窗口大小并记下操作顺序，据此确认后再申请修补（候选：第一次打开时等布局完成再套相机）。

### 块 S5 — Step3 的 Navigator：ROI 锁定、patch 可编辑并全局同步（用户 2026-09-26 批准；已实施，**真机验收通过**）
- **必要性**：用户裁定（见 Step3 重设计「第 ⑤ 步改定」）。块 2c-2 把 Step3 的 Navigator 设为完全只读（过渡）。
- **只读核实**：
  - 现有权限校验已支持「ROI 只读 + patch 可编辑」的组合：`set_navigator_policy(roi_policy="read_only", patch_editable=True)`（`block01_display.py:2216-2228` 只校验 ROI 策略取值，patch 可编辑是独立开关）；面板上每个编辑入口都在改动前按对应开关拒绝（ROI 新建 / 删除按 ROI 策略，patch 的增删移动改名按 patch 开关）。
  - Step0 的处理与保存（`_reconcile_roi_edit` → `_persist_geometry_edit`）不按步骤判断；Step1 接收 Step0 的几何提交（`_on_step0_geometry_committed`）也不按当前页判断。所以在 Step3 改 patch，会与在 Step1 改一样写进 Step0 的 `patch_config.json` 等文件，Step1 随之同步。
  - 弹窗里的 ROI 列表删除按钮按 ROI 删除权限禁用；patch 列表（删除、双击改名）按 patch 开关启用。
- **做法**：
  1. `_apply_navigator_policy_for_step`：Step3 改为 `roi_policy="read_only"`、`patch_editable=True`（Step2、Step4 仍为完全只读）。
  2. `_show_step3_tissue_navigator`：同样传 `roi_policy="read_only"`、`patch_editable=True`。
- **白名单**：`ui/main_window.py`（上述两处）；`UI_SURFACE_RULES.md`（Step3 的 Navigator：ROI 锁定、patch 可编辑并全局同步）；`docs/user_guide.md`、`docs/用户指南.md`（Step3 一节）；测试：`tests/test_step3_viewer.py::test_step3_s_navigator_button_keeps_the_navigator_read_only` 改写为新行为；`tests/test_step1_navigator_policy.py::test_steps_after_step1_get_a_read_only_navigator` 的步骤参数由 `[2, 3, 4]` 改为 `[2, 4]`（Step3 的新行为由改写后的测试覆盖）；本文档。其他测试失败须停下说明。
- **不改的范围**：Navigator 面板、Step0 页面、权限校验与保存流程；Step2 / Step4 的只读；patch 按钮条（第 ③ 步）。
- **风险**：Step3 的 patch 编辑会立即写进 Step0 的项目文件（按裁定，与 Step1 相同）；patch 的中心须落在已有 ROI 内（现有规则）。回退：恢复这两处。
- **验收门**：
  - 在 Step3（进入时与点 Step3 的 `Tissue Navigator` 按钮后）：新建 ROI、删除 ROI（画布与 ROI 列表）都被拒绝，`roi_config.json` 不变；新增、移动、删除、改名 patch 都生效并写进 `patch_config.json`，回到 Step1 看到同样的 patch。
  - Step1 的权限不变（可删除 ROI、可编辑 patch）；Step2、Step4 仍完全只读。
  - 回归与 HEAD 逐条对比，除上述两条测试外无新增失败。
  - 真机（用户）：在 Step3 的 Navigator 里不能画 / 删 ROI；能加、拖、删、改名 patch；回到 Step1 与 Step0 看到同样的 patch。
- **执行记录**（2026-09-26，未提交）：
  - `ui/main_window.py`：`_apply_navigator_policy_for_step` 为 Step3 新增一支 `roi_policy="read_only"`、`patch_editable=True`；`_show_step3_tissue_navigator` 改传 `patch_editable=True`。
  - 文档：`UI_SURFACE_RULES.md` 的 Step3 页面一条（ROI 冻结、patch 可编辑并全局同步）；两份用户指南 Step3 一节第 5 条。
  - 测试：`tests/test_step3_viewer.py` 的只读测试改写为 `test_step3_s_navigator_freezes_rois_and_syncs_patches`（两个入口：进入 Step3、点 Step3 的按钮）——画 ROI、删最后一个 ROI、用 ROI 列表删除都不生效，`roi_config.json` 不变；新增 patch、移动、改名（“Mark A”）、删除都写进 `patch_config.json`，`step0_output["patches"]` 跟随（Step1 同步）；Step1 的按钮仍给 Step1 的权限。`tests/test_step1_navigator_policy.py` 的 `_downstream_steps` 改为 `[2, 4]`；**白名单外（用户 2026-09-26 授权）**：同文件 `test_coming_back_to_step0_from_a_read_only_step_restores_everything` 中作为「只读步骤」的 Step3 换成 Step2（验证意图不变）。反向注入 2 处（Step3 的步骤权限、Step3 按钮的权限）各使 1 条变红。
  - 回归：19 个模块（Navigator 权限、patch 几何保存、Step3 各测试、Tissue Preview 契约、界面规则），与 `git archive HEAD`（`7382a39`）逐条对比，除上述授权修改的一条外**无新增失败**；两边相同：`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`。
  - **真机验收通过（用户 2026-09-26）**：Step3 的 Navigator 里 ROI 不能画、不能删；patch 可新增、拖动、改名、删除，并同步到 Step1 / Step0；Step1 的权限不变。

### 第 ④ 步 — Step3 整张图 mask 浏览：调查结论与用户裁定（2026-09-26）
- **调查结论**（只读）：
  - Step3 的 viewer 用切片 level-0 坐标，层级就是原始切片的金字塔层级（`Step1TileProvider` 的几何取自 `RawTileProvider`）；标签金字塔（块 N）在同一网格上，可逐级对齐。须核对金字塔记录的 `raw_level_shapes` 与 viewer 的层级一致，不一致就不用。
  - 标签分块不能走现有的 `TileScheduler` / provider：它们只处理 float32（细胞编号超过 2^24 会失真），且 `step1_gpu_demo_plan.md` 禁止修改。需要一个独立的标签读取器，跟随同一个控制器的快照（层级、可见分块、epoch），读取可见分块加一圈外扩，丢弃过期请求，随 mount 暂停 / 恢复 / 释放。
  - GPU（GL 3.3 core）支持整数纹理（R32UI、`usampler2D`、`texelFetch`）。建议两遍：先把可见标签块画进屏幕大小的「编号图」，再逐像素比较相邻编号画轮廓（线宽正好为屏幕 1–4 像素，无接缝，不需要 halo）；填充模式按编号的散列算固定随机色。标签纹理不能用现有 `_TextureLru`（强制 float32），需单独的缓存。现有渲染只在 `submit()` 时重画 `final`，标签块晚到时需要自己的重画路径。
  - 只有 Step3 的 mount 画 mask（构造开关）。
  - 运行列表：当前 ROI 工作区 `roi_index.json` 的 `segmentation_runs` 与 `active_segmentation_run`，再扫描 `step2/segmentation_runs` 与 `step2/segmentation_results` 补充（旧 Step3 的做法）。多 ROI 的运行按 `rois[].roi_name == _active_roi["name"]` 匹配并核对 bbox；全图运行的 meta 里 `label_pyramid` 是扁平的 `{cell, nucleus}`，没有 `roi_id` / bbox。
  - 已有问题：Step2 跑完新结果不通知已打开的 Step3；换数据集时 `step2_output` / `_step3_run_dir` 不清除——Step3 只认属于当前 ROI 工作区的运行。
- **用户裁定（2026-09-26）**：
  1. 拆成 ④a 数据层、④b GPU 渲染、④c Step3 界面。
  2. 旧结果补生成金字塔放在 ④b 的标签读取后台线程里（整个 ④ 只新增这一个线程）。
  3. 批准新增：一个后台读取线程；过期请求记录；GPU 标签纹理缓存（单独 64 MB 显存上限，不挤占原 512 MB）；mask 显示设置的状态；新着色器与编号图；mount 的 mask 开关。解压后的分块暂不做内存缓存（先实测）。
  4. 先不做 CPU 版的 mask：没有 GPU 时显示「mask 需要 GPU 显示」。（macOS：系统 OpenGL 支持到 4.1 core，本程序要求 3.3 core；本程序从未在 Mac 上测试，GPU 层若在 Mac 上启动失败，图像退回 CPU、mask 不显示。）
  5. 控件在 Step3 右栏顶部一行：运行下拉框、mask 控件、提示文字，右端是 Overlay / Fusion。
  6. **细胞 mask 与核 mask 都有控件**，都放在 viewer 顶部；每种 mask 一个按钮，点开是下拉展开面板，设置显示 / 隐藏、颜色、透明度、线宽、轮廓或填充。运行没有核 mask 时核 mask 的控件禁用。

### 块 ④a — Step3 mask 的数据层（申请 v2，按独立审核修订，**用户 2026-09-26 批准**；已实施，**数据层自动验收通过**，真机随 ④c）
- **必要性**：④b（GPU）与 ④c（界面）都依赖「选哪一次运行、它的哪种 mask 在哪、按 viewer 的分块读出标签」。主要风险是把正确的标签放到错误的 mask 控件或错误的坐标上，所以先把这些数据契约写死并单独测试。
- **细胞 / 核的分类按方法，不按文件名**：Step2 的主输出一律叫 `global_mask*.zarr`，块 N 生成金字塔时也一律记作 `cell`（`segment_merge_worker.py:611`），但纯核方法的主输出其实是核标签。分类表（与 `seg_runner.engines.METHOD_OUTPUTS` 对照，但以 Step2 实际写出的文件为准）：

  | 方法 | 主输出 `global_mask*` | `global_nuclei_mask*` | Step3 的细胞 mask | Step3 的核 mask |
  |---|---|---|---|---|
  | cellpose_wholecell_fusion、mesmer_whole_cell | 细胞 | 无 | 主输出 | 无 |
  | cellpose_nuclei_dapi、stardist_nuclei_dapi、mesmer_nuclei | **核** | 无 | 无 | **主输出** |
  | cellpose_nuclei_expansion、stardist_nuclei_expansion | 扩张后的细胞 | 无（Step2 不保留原核） | 主输出 | 无（不能从扩张结果推回原核） |
  | mesmer_nuclear_guided | 细胞 | 核 | 主输出 | 核文件 |
  | HQ / HQ2 / CDS（不维护） | 细胞 | 核 | 主输出 | 核文件 |
  | 未知方法 | — | — | 不显示，写明原因 | 不显示 |

  方法取自运行的 meta（`method`）；块 N 金字塔里的 `kind` 只作校验，不决定归类。④c 按实际得到的类型禁用缺失的控件（包括没有细胞 mask 的情况）。
- **读取前的完整校验**（在新模块里做，Step2 与 `label_pyramid.read` 的行为不变）：`read` 通过之后再逐项检查——`levels` 里每一级的数组都存在、形状等于记录的 `shape`、dtype 为 uint32；`level0` 解析出的路径**就是**当前选中的 mask；`roi_bbox` 等于这个源的 bbox；`raw_level_shapes` 与 viewer 的层级一致。任何一项不符，这个金字塔就不用（该 mask 按「没有金字塔」处理），并写明原因。
- **坐标来源：只用元数据能确定的，证据不足就不显示**。支持的格式与优先级：

  | 运行格式 | mask 路径 | bbox 来源（按优先级） |
  |---|---|---|
  | 块 N 起的 ROI 模式 | `rois[i].zarr_path`（`roi_name == 当前 ROI 名`） | `rois[i].bbox_fullres` → 该 ROI 的 `segmentation_meta_<ROI>.json` 的 `bbox` / `roi_bbox_fullres`；并须等于当前工作区该 ROI 的 bbox |
  | 块 N 之前的 ROI 模式 | 同上（字段相同，只是没有 `label_pyramid`） | 同上 |
  | 全图模式 | `zarr_path` / `paths.mask_zarr`（`global_mask.zarr`） | 当前工作区 manifest 的 `bbox_fullres`，**且** mask 的形状恰好等于这个 bbox 的尺寸 |
  | 只有 OME-TIFF、没有 uint32 zarr 的旧运行 | 不支持 | ——（float32 TIFF 已丢失 2^24 以上的编号，转成 uint32 也恢复不了），写明原因 |

  只读 uint32 zarr；bbox 与形状对不上、找不到当前 ROI、运行不属于当前工作区，都不显示并写明原因。
- **只有第 0 级时的约定**：金字塔不存在（或补生成失败、落到裁定 8a 的第 ② 条）而 viewer 在粗层时，`read_label_tile` **明确返回「该层不可用」**（一个带原因的结果），**不从第 0 级临时采样、也不返回全零标签**。④b 据此在粗层不画 mask，由 ④c 在控件行显示「缩小时无法显示 mask：原因」；放大到第 0 级时照常显示。
- **做法**：新增 `core/step3_masks.py`（无 Qt）：
  - `list_runs(roi_dir)`：读 `roi_index.json` 的 `segmentation_runs` 并扫描 `step2/segmentation_runs`、`step2/segmentation_results`；按 `run_id`（缺失时用目录的规范路径）**去重**；**排除**未完成 / 失败的运行（索引里 `status` 不是 `done`，或目录里没有 `segmentation_meta.json` / `run_metadata.json`）；按 `created_at` 新到旧；标出 active（`active_segmentation_run` 指向的运行已失效时不标）。
  - `choose_run(runs, requested_dir=None, current=None)`：显式目录（Step2 完成对话框）且在列表中 → 该运行；否则当前选择仍在列表中 → 保持；否则 active；否则最新；列表为空 → None。不属于当前工作区的目录一律不用。
  - `resolve_masks(run, roi_name, roi_bbox, view_level_shapes)`：按上面两张表返回 `{"cell": 源或 None, "nucleus": 源或 None, "reasons": {...}}`；每个源记下种类、第 0 级 mask 路径、bbox、以及通过完整校验的金字塔（或 None 与原因）。
  - `pyramid_path_for(mask_zarr)`：金字塔命名规则放进 `core/label_pyramid.py` 作为公共函数，Step2 的 worker 改用它（测试锁定与原命名逐字相同）。
  - `ensure_pyramid(source, raw_level_shapes, cancel_check)`（由 ④b 的后台线程调用）：先写进运行目录；**写不进去**（无权限、磁盘满等 OSError）才在内存里生成、只供本次运行使用（`label_pyramid` 增加写入内存存储的选项）；内存也失败则返回「只有第 0 级」与原因。**取消直接结束**，不当作写盘失败、不再尝试内存退路；「无残留」指清理本次未完成的产物（`.partial`、内存中的半成品），**不删除已有的有效结果**。
  - `read_label_tile(source, level, tx, ty, tile_size, view_level_shapes)`：按 viewer 的分块约定（与 `Step1GpuBinding._world_rect` 相同：`x0 = tx·T·ds_x`，范围为数组形状 × 不取整的比例）读出一个 uint32 标签块与它的 world rect；第 0 级从 ROI 坐标的 mask 读（减去 bbox 起点），第 L 级从金字塔数组读（减去 origin）；块内超出覆盖范围的部分补 0；该层不可用时返回带原因的「不可用」。
  - 数值参考（给 ④b 的着色器对照）：`outline_reference(ids, width)`——输入是**屏幕编号图**（每个屏幕像素一个编号）；像素的编号不为 0，且以它为中心、边长 2·width+1 的方形邻域内（切比雪夫距离 ≤ width）存在不同的编号，则该像素为轮廓；图像边缘以外按编号 0 处理（即 mask 边界处画线）；width 取 0–4，0 表示不画轮廓。`fill_colour(ids)`——按编号的整数散列得到固定 RGB，编号 0 透明，同一编号颜色稳定。
- **白名单**：`core/step3_masks.py`（新）；`core/label_pyramid.py`（`pyramid_path_for`、写入内存存储的选项）；`workers/segment_merge_worker.py`（金字塔命名改用 `pyramid_path_for`，行为不变）；新测试 `tests/test_step3_masks.py`；本文档。
- **不改的范围**：任何界面、GPU、viewer、mount、调度器、provider；Step2 的其他行为；`label_pyramid.read` 的现有语义。
- **风险**：**无 Qt、暂未接入 Step3**；但本块包含磁盘写入（补生成金字塔）、内存分配（内存退路）与 Step2 命名规则的搬迁，仍有运行风险——命名搬迁用测试锁定逐字相同，写入只发生在运行目录且沿用块 N 的 `.partial` → 改名流程。
- **验收门**：
  - 运行列表：索引与扫描去重；未完成 / 失败的运行被排除；排序；active 标记；active 指向失效运行时的退路；选择规则四种情况与空列表；外工作区的目录被拒绝。
  - 分类：表中每一类方法得到正确的细胞 / 核归属（纯核方法的主输出归入核、细胞为 None；expansion 只有细胞；nuclear-guided 两种都有）；未知方法不显示。
  - 完整校验：缺一层、某层形状不符、dtype 不是 uint32、`level0` 指向另一个 mask、bbox 不符、层级尺寸与 viewer 不符，各自使金字塔为 None 并给出不同的原因；完整的金字塔通过。
  - 坐标来源：表中每种格式；bbox 与当前 ROI 不符、全图 mask 形状与 bbox 不符、只有 TIFF 的旧运行，都不显示并写明原因。
  - `read_label_tile`：奇数尺寸、非整数比例的合成金字塔上，各级各分块逐像素等于独立参考（由 viewer 的比例逐像素计算）；world rect 与 `Step1GpuBinding._world_rect` 公式一致；编号超过 2^24 不失真；没有金字塔时请求粗层返回「不可用」而不是全零，第 0 级照常读出。
  - `ensure_pyramid`：可写 → 写进运行目录且通过完整校验；不可写 → 内存生成、不写盘；内存失败（模拟）→ 只有第 0 级并给出原因；取消 → 不尝试内存退路，`.partial` 被清理，已有的有效金字塔不被删除。
  - `pyramid_path_for` 与 Step2 原命名逐字相同（cell / nucleus、ROI / 全图）；块 N 的测试照常通过。
  - `outline_reference` / `fill_colour`：按上面写定的邻域、边缘与线宽规则的小例子手算对照（含 width 0 与 4、图像边缘、相邻两个细胞）；编号 0 不画；同一编号颜色稳定。
  - 回归：Step2 与标签金字塔相关模块与 HEAD 逐条对比无新增失败。
  - ④a 只交付为**数据层自动验收通过**，不宣称实际叠加正确；真机验收随 ④c。
- **执行记录**（2026-09-26，未提交）：
  - `core/label_pyramid.py`：新增 `pyramid_path_for`（Step2 原命名规则原样搬入）；`build` 的逐级写入与属性拆成 `_write_levels` / `_attrs` 两个内部函数，`build` 的行为与写出的属性不变；新增 `build_in_memory`（同样的层级写进 zarr 内存存储，`level0.path` 记绝对路径，不写盘）。`read` 未改。
  - `workers/segment_merge_worker.py`：`_write_label_pyramids` 的目标路径改用 `label_pyramid.pyramid_path_for(src)`，其余不变。
  - `core/step3_masks.py`（新，无 Qt）：`list_runs`、`choose_run`、`resolve_masks`、`check_pyramid`（完整校验，磁盘与内存金字塔共用）、`ensure_pyramid`、`read_label_tile`、`outline_reference`、`fill_colour`，按申请实现。
  - **实施中按事实确定的细节**（都在白名单文件内，未扩大行为范围，请用户知悉）：
    1. ROI 模式的 mask 路径**先取 `rois[i].paths.mask_zarr`，再退到 `rois[i].zarr_path`**：实测第一个 ROI 的 `zarr_path` 是 Step2 的 `global_mask.zarr` 别名（符号链接；建链失败时是**复制件**），而块 N 的金字塔记录的是真实文件 `global_mask_<ROI>.zarr`。比较一律用真实路径（`realpath`）。
    2. 核 mask（nuclear-guided、HQ 系）取主 mask 旁边的 `global_nuclei_mask*`：Step2 的 ROI 记录里没有核 zarr 的路径字段，只有核的 OME-TIFF 与金字塔路径；命名与 Step2 写出的一致。
    3. `list_runs` 另外排除 meta 里 `roi_id` 是别的 ROI 的运行（属于「不属于当前工作区」）。
    4. 「写不进去」的判定：zarr 2.18 的目录存储把 `PermissionError` 包成 `KeyError(key) from e` 抛出，所以沿异常的 cause 链找 `OSError`，找到才走内存退路；其他错误直接「只有第 0 级」并写明原因。
    5. 「不删除已有的有效结果」落实为：**目标位置上 `label_pyramid.read` 接受的金字塔一律不覆盖**（例如为别的层级建的），返回「只有第 0 级」并写明「原有金字塔保留」；只有 `read` 不接受的（未完成、第 0 级已变）才重建。
    6. `fill_colour`：lowbias32 整数散列（着色器里可逐字实现），R、G、B 各取散列的一个字节映射到 55–255（避免近黑），不透明；编号 0 全透明。
  - 测试：新增 `tests/test_step3_masks.py`（47 条）：运行列表（索引 + 扫描去重、失败 / 无 meta / 外工作区 / 别的 ROI 排除、排序、active 与失效 active）、选择规则、9 种方法的分类与未知方法、nuclear-guided 缺核文件、完整金字塔通过与 6 种缺陷各自不同的原因、未完成金字塔、坐标来源（ROI 记录的 bbox、退到该 ROI 的 meta、bbox 不符、找不到 ROI、别名为复制件、块 N 之前的运行、全图模式与形状不符、只有 TIFF）、`read_label_tile` 在 3 种 bbox 下各级各分块逐像素等于独立参考且 world rect 与真实的 `Step1GpuBinding._world_rect` 相等、编号超过 2^24、无金字塔时粗层「不可用」、`ensure_pyramid` 的写盘 / 只读目录走内存（目录内容不变）/ 内存失败（模拟）/ 取消 / 不覆盖有效金字塔 / 重建未完成的、`pyramid_path_for` 与原公式逐字相同（5 种名字）、内存与磁盘构建逐级相同、模块不加载 Qt、轮廓与填充色手算例子（含宽 0 / 4、图像边缘、相邻两个细胞、只在对角相邻）。反向注入 11 处（纯核方法归为细胞、未知方法、粗层返回全零、用别名路径、world rect 取整、去掉 dtype 校验、取消后继续、覆盖有效金字塔、列出失败运行、改命名、轮廓邻域改为菱形）各使至少 1 条变红。
  - 只读核对真实项目（`~/fusion_data/test1` 的一次 ROI 模式运行，不写入）：运行被列出并标为 active；细胞 mask 解析到 `global_mask_Full WSI.zarr`，块 N 的金字塔通过完整校验；核为「该方法不产生核 mask」；三级分块可读。
  - 回归：11 个模块（`test_label_pyramid`、`test_step2_runner_path`、`test_step2_engine_unified`、`test_step2_legacy_stop`、`test_step2_ownership_move`、`test_step2_remap_integration`、`test_step1_step2_handoff_e2e`、`test_step1_to_step2_handoff`、`test_step2_tile`、`test_step2_profiler`、`test_seg_runner_engines`），每模块单独进程、顺序运行，与 `git archive HEAD`（`faee7f1`）逐条对比，**无新增失败**。两边相同：`test_seg_runner_engines.py` 的 2 条 Mesmer（本机无模型）。StarDist 偶发（已知问题）：`test_step2_engine_unified.py::test_a_manual_run_equals_the_runner[stardist_nuclei_dapi-roi]` 本侧失败一次、单独重跑通过；`test_seg_runner_engines.py::test_stardist_expansion_returns_the_nuclei_from_before_expanding` 本侧失败，HEAD 批量时通过、单独重跑同样失败（该模块不引用本块改动的任何文件）。
  - 真机：本块无界面，真机验收随 ④c。

### 块 ④b — Step3 的 GPU 标签渲染（申请 v2，按独立审核修订，**用户 2026-09-26 批准**；已实施，**自动验收通过**，真机随 ④c）
- **必要性**：④a 已能按 viewer 的分块读出标签；要在 Step3 的整张图上画出 mask，须在 GPU 显示层加标签渲染（裁定 7、第 ④ 步裁定 2–4）。本块只交付「画得对、跟得上相机、资源有界」，不加任何界面；运行选择与控件是 ④c。
- **只读调查结论**：
  - 画面的产生：`Step1GpuBinding._publish_current` 每次相机事件（跳转立即，拖动每 33 ms 一次，`BindingBudgets.motion_interval_ms`）调用 `Step1GpuLayer.submit`，后者把各通道合成进屏幕大小的 `final`（RGBA8）目标（`step1_gpu_layer.py:439-488`），`paintGL` 只把 `final` 拷到屏幕（`:586-598`）。所以 mask 必须在**同一次提交、同一个视野矩形**里画进去，否则拖动时 mask 会和图像错开；标签块晚到时又不能为此重算全部通道——需要「图像不动、只重画标签」的路径。
  - 相机：同一个控制器的 `interaction_event(kind, snapshot)` / `gesture_quiet(snapshot)`；快照带 `epoch`、`level`、`visible_tiles`，分块边长在 `controller.grid.tile_size`（`viewer/explore_view.py:1270-1310、2200-2219`）。图像绑定按这些计划分块（`step1_gpu_binding.py:396-457、677-687`）。
  - 纹理缓存 `_TextureLru` 只收 float32（R32F），不能放编号；512 MB 原始纹理上限不得挤占（`step1_gpu_demo_plan.md` G3.2a）。
  - 两个 viewer 由同一个类 `Step1WholeSlideMount` 构建，Step3 的在 `main_window.py:1864`（`camera_reason="step3"`）；暂停 / 恢复 / 关闭 / 换数据源都经 mount（`step1_viewer_mount.py:756-860、876-955、1187`）。
  - 本机在真实显示（WSLg，`QT_QPA_PLATFORM=xcb`）下能建起 GPU 层：Intel Iris Xe（D3D12 转译，GL 4.1 core）——不是 RTX 3060。所以着色器可以在本机用读回像素的方式自动验证；离屏平台下照旧跳过。
- **做法**：
  1. **GPU 层**（`ui/step1_gpu_layer.py`）：构造参数 `labels=False`（默认，Step1 与 montage 不变：不建新目标、不编译新着色器、`paintGL` 照旧）。`labels=True` 时：
     - 新增两个屏幕大小的目标：`ids`（R32UI，屏幕编号图）和 `shown`（RGBA8，图像 + mask）；新着色器文件 `ui/shaders/step1_gpu_labels.frag`（顶点着色器沿用），两个程序：`label_ids`（按世界坐标把标签块画进 `ids`，`usampler2D` + `texelFetch`，不插值）与 `label_draw`（读 `ids`，按 ④a 的 `outline_reference` 规则求轮廓，或按 `fill_colour` 的 lowbias32 散列上色，按透明度混合到 `shown` 上）。
     - 新的标签纹理缓存（R32UI，**单独 64 MB 上限**，最近最少使用淘汰），与 `_TextureLru` 分开。
     - 公开入口 `set_labels(snapshot)`：每种 mask 一层（种类、显示与否、颜色、透明度、线宽、轮廓 / 填充、标签块列表）。`submit()` 末尾与 `set_labels()` 都执行同一段「`final` → `shown`，再按层画 mask」；`set_labels` 用上一次提交的视野，**不重算通道**。
     - 画的顺序（v2，按审核改）：每种 mask 内**当前目标层级最后画、优先**；旧层级只填补目标层级尚未到达的区域（按与目标层级的距离由远到近先画）。放大、缩小都成立——缩小时旧的细块不会盖住新到的粗块。编号图这一遍**不丢弃编号 0**：新块里的背景照样覆盖旧标签，不留旧轮廓。两层都显示时先细胞、后核（核在上，待裁定 1）。
     - ROI 边界（v2）：`shown` 目标**共用 `final` 的模板缓冲**，mask 在与图像同一个多边形和矩形内绘制（`_finalize` 写的同一份模板，`step1_gpu_layer.py:743-814`、`:872-906`）；只重画 mask 时沿用上一次提交写好的模板。
     - 线宽（v2）：设置值是逻辑像素 0–4，屏幕半径 = floor(线宽 × 设备像素比 + 0.5)，上限 8（设备像素比 > 2 时封顶并在状态里说明）。
  2. **标签绑定**（新文件 `ui/step3_label_binding.py`，Qt 对象）：
     - 输入：mount 的控制器、viewer 各层级尺寸、GPU 层、④a 的 `MaskSource`（细胞 / 核）与显示设置。
     - **一个后台读取线程**（整个 ④ 唯一的新线程）：先对缺金字塔的源调用 `ensure_pyramid`（可取消），再读标签块（`read_label_tile`）。请求带「源的代次 + 相机 epoch」；线程取任务时，比最新 epoch 旧的直接丢弃并计数（「过期请求记录」）；结果经排队信号回到界面线程，代次已变的丢弃。
     - 读哪些块：当前层级的可见块**外加一圈**；跟随相机的节奏与图像绑定相同（跳转立即，拖动节流 33 ms，停手补一次）。仍覆盖视野的已画块保留，换层级时旧层级的块按上面的规则暂留，不再覆盖视野的块释放。**不做屏幕外分块的内存缓存**（裁定 3）。
     - 预算（v2）：一块 512² uint32 = 1 MB。**按代码估算（未实测，viewer 宽高取 1600×1000 逻辑像素）**：viewer 选层级的规则是「降采样不超过所需比例的最大一级」（`viewer/explore_view.py:4254-4263`），一个层级像素占 1 到不足 4 个屏幕像素；刚要切到更粗一级之前，1600×1000 的 viewer 一种 mask 的可见块约 14×9 ≈ 126 块 ≈ 126 MB——**64 MB 在这种缩放下连一种 mask 的可见块都放不下**（放大到层级像素 ≈ 屏幕像素时约 20 块 / 种）。须用户裁定（裁定 5）。无论哪种，超额时的顺序固定为：先淘汰旧层级的块 → 再淘汰外圈 → 目标层级的可见块仍放不下时，从视野中心向外画到上限为止，其余不画，状态写明「mask 显存不足，部分区域未显示」，终端给出所需与上限。
     - CPU 数组：GPU 纹理之外，绑定手里的数组（已画的块 + 已读出、等待界面线程接收的结果 + 正在读的一块）计入同一预算；达到上限时线程暂不取新任务。内存金字塔（④a 的退路，约为 mask 像素的 6.6%）与两个屏幕目标**不计入**这个预算，单独列出。
     - 粗层级且没有金字塔（「只有第 0 级」）：该层不请求、不画，状态给出原因（供 ④c 显示「缩小时无法显示 mask：原因」）。金字塔补生成期间同样按「暂时只有第 0 级」处理，生成完成后自动开始画粗层。
     - 暂停（v2，待裁定 4）：建议**补生成也停止**（取消，未完成的 `.partial` 按 ④a 清理），恢复时重新开始；断开相机、清空未开始的请求，正在读的一块读完保留。恢复：从当前相机补一次。换源 / 关闭：取消、结束线程并等待其退出、释放纹理。
     - 如实说明：只有一个线程，**补生成期间不读任何标签块，第 0 级也要等补生成结束或被取消**；状态写明「正在生成缩小用的 mask 层级」。
  3. **mount**（`ui/step1_viewer_mount.py`）：构造参数 `labels=False`；为 True 时 GPU 层以 `labels=True` 构建，并随 GPU 后端创建 / 释放标签绑定；`pause_requests` / `resume_requests` / `source_changed` / `close` 转交给它。给 ④c 的公开入口：`set_mask_sources(sources)`、`set_mask_style(kind, …)`、`mask_status()`。没有 GPU（走 CPU 画面）时不建标签绑定，`mask_status()` 返回「mask 需要 GPU 显示」，终端打印原因。
  4. `ui/main_window.py`：只改一处——Step3 的 mount 构造加 `labels=True`（`:1864`）。**本块产品里没有人调用 `set_mask_sources`**（④c 接入），所以用户看不到任何变化。
- **白名单**：`core/step3_masks.py`（**最小扩围，v2**：`outline_reference` 的半径上限由 4 改为 8，其余不变）与 `tests/test_step3_masks.py`（对应用例）；`ui/step1_gpu_layer.py`；`ui/shaders/step1_gpu_labels.frag`（新）；`ui/step3_label_binding.py`（新）；`ui/step1_viewer_mount.py`；`ui/main_window.py`（上述一处）；新测试 `tests/test_step3_label_binding.py`（假 GPU 层：请求、过期丢弃、层级、暂停 / 恢复 / 关闭、线程退出、预算）与 `tests/test_step3_label_render.py`（真实 GL，读回像素；离屏跳过，`BLOCK01_REQUIRE_STEP1_GPU=1` 时不许跳过）；本文档。其他测试失败须停下说明。
- **不改的范围**：`viewer/explore_view.py`、`viewer/scheduler.py`、`viewer/caches.py`、`viewer/raw_tile_provider.py`、图像绑定 `ui/step1_gpu_binding.py`、`_TextureLru` 与 512 MB 上限、Step1 与 montage 的 GPU 层行为、CPU 画面、`core/step3_masks.py` 除上述一处外（发现缺陷先停下说明）、任何界面与用户指南。
- **风险**：
  - GPU 层是 Step1 与 Step3 共用的类：新代码全部在 `labels=True` 之后，Step1 的回归以现有 GPU 测试与像素读回为准。
  - 新线程的生命周期：关闭、换数据集、退出程序时必须结束；测试逐项锁定。
  - 显存：标签纹理（预算见裁定 5）+ 两个屏幕目标（2560×1440 时约 29 MB，另计）；内存：同额 CPU 数组 + 内存金字塔（另计）。
  - 本机的 GPU（Intel，经 D3D12 转译）与部署机不同；整数纹理属于 GL 3.3 核心功能，但仍以真机为准。
  - 回退：Step3 的 mount 去掉 `labels=True` 即回到现在的行为。
- **验收门**：
  - 编号图（真实 GL，读回，v2）：GPU 画出的屏幕编号图**逐像素等于独立的 CPU 参考**——由视野矩形、屏幕尺寸和标签块的世界矩形逐像素换算（与图像着色器同一换算），不从 GPU 结果推导；覆盖编号超过 2^24、奇数尺寸、非整数比例、跨块的细胞。
  - 轮廓与填充：在上面已验证的编号图上，轮廓像素逐像素等于 `outline_reference`，设备像素比 1、1.5、2 各测（逻辑线宽 0–4，屏幕半径按上面的取整规则）；填充色逐像素等于 `fill_colour` 再按透明度混合；编号 0 处图像不变；两层叠放顺序正确。
  - 层级覆盖：放大（新细块盖旧粗块）、缩小（新粗块盖旧细块）、新块为空（编号 0 抹掉旧轮廓）各一例；目标层级缺块处由旧层级填补。
  - ROI：斜边与凹多边形 ROI 外 mask 不上屏（与图像同一边界），只重画 mask 时同样成立。
  - 同步：同一次提交里图像与 mask 用同一视野矩形；只改 mask 设置或标签块到达时不重新提交通道（通道提交计数不变）。
  - 读取：只请求当前层级可见块加一圈；过期请求被丢弃并计数；换层级时旧块保留到被盖住；只有第 0 级时粗层不请求、状态给出原因；补生成完成后粗层开始显示。
  - 生命周期：暂停不发请求（按裁定 4 处理补生成）、恢复补一次；换源 / 关闭后线程已退出、纹理已释放、迟到结果被丢弃。
  - 预算：GPU 与 CPU 两侧都不超上限（CPU 侧含等待接收的结果）；超额按「旧层级 → 外圈 → 中心向外」的顺序，状态给出提示。
  - Step3 图像不变（v2）：`labels=True`、尚未设置 mask 来源时，Step3 的画面与 `labels=False` **逐像素相同**（读回对比），通道提交计数相同。
  - 无 GPU：不建标签绑定，`mask_status()` 给出「mask 需要 GPU 显示」。
  - Step1 不变：`labels=False` 的层不建新目标与程序；Step1 与 montage 现有 GPU 测试、像素读回无新增失败；回归与 HEAD 逐条对比。
  - ④b 只交付为**自动验收通过**（含本机真实 GL 读回）；用户真机验收随 ④c。
- **请用户裁定**：
  1. 细胞与核同时显示时，**核画在细胞上面**（建议），还是反过来？
  2. 线宽按**逻辑像素**（在高分屏上乘以设备像素比，看起来与普通屏同样粗，建议），还是按物理像素？
  3. 缩放换层级时，旧层级的 mask 块**暂留、目标层级优先覆盖**（不闪空，建议），还是只画当前层级（新块到之前那片暂时没有 mask）？
  4. 暂停（离开 Step3）时补生成金字塔：**停止、恢复后重来**（建议，与「不在屏幕上就不读」一致），还是作为例外继续？
  5. 标签纹理预算：(a) **上限改为 256 MB**，mask 与图像同一层级、精度一致（两种 mask 最坏约 250 MB；本机显存 6 GB，图像另有 512 MB）——建议；(b) 保持 64 MB，mask 改用「不细于屏幕」的层级（一个层级像素占 1–4 个屏幕像素，块数约为 (a) 的 1/16，缩小到切换点附近时轮廓会显得粗糙）；(c) 保持 64 MB 与同一层级，超额时按上面的顺序只画中心部分。
- **用户裁定（2026-09-26，全部按建议）**：1 核画在细胞上面；2 线宽按逻辑像素；3 旧层级暂留、目标层级优先覆盖；4 暂停时补生成停止、恢复后重来；5 **标签纹理上限 256 MB**（mask 与图像同一层级），CPU 数组同额；超额顺序不变。
- **执行记录**（2026-09-26，未提交）：
  - `core/step3_masks.py`：`outline_reference` 的半径上限改为 8（`MAX_OUTLINE_RADIUS`），其余不变；`tests/test_step3_masks.py` 相应改一条、加一条（半径 8）。
  - `ui/step1_gpu_layer.py`：构造参数 `labels=False` / `max_label_texture_bytes`（默认 256 MB，`LABEL_TEXTURE_BYTES`）；新数据类型 `LabelPlane` / `LabelLayer` / `LabelSnapshot` 与 `label_radius`（floor(线宽 × 设备像素比 + 0.5)，0–8）；`_LabelTextureLru`（R32UI，独立预算）；`labels=True` 时才有的两个目标 `ids`（R32UI）与 `shown`（RGBA8，挂 `final` 的同一个模板缓冲）、两个程序；`submit()` 末尾与 `set_labels()` 共用 `_compose_labels`（`final` → `shown`，每层先画编号图再画轮廓 / 填充，沿用上一次 `_finalize` 的矩形与多边形裁剪）；超预算的快照在保存之前就拒绝（否则之后每次图像提交都会失败）；`paintGL` 在有 mask 时显示 `shown`，否则照旧 `final`；测试用读回 `readback_shown_for_test` / `readback_label_ids_for_test`；`label_stats`。`labels=False` 时以上都不存在。
  - `ui/shaders/step1_gpu_labels.frag`（新）：`PASS_LABEL_IDS`（按像素中心 `gl_FragCoord` 与图像同一换算取标签块的纹素，编号 0 照样写入）与 `PASS_LABEL_DRAW`（切比雪夫半径内有不同编号即轮廓；填充色为 lowbias32 散列，与 `fill_colour` 同式）。
  - `ui/step3_label_binding.py`（新）：`Step3LabelBinding`，按申请实现（一个后台线程、先补生成再读块、可见块 + 一圈、中心优先、节奏同图像绑定、过期请求与迟到结果计数、目标层级最后画、旧层级只留仍在视野内的、预算淘汰顺序与提示、CPU 侧计入正在读与等待接收的结果、粗层无金字塔不请求并给原因、暂停取消补生成恢复重来、`dispose` 结束线程并等待）。默认显示设置：细胞绿色、核青色，透明度 0.75，线宽 1，轮廓。
  - `ui/step1_viewer_mount.py`：`labels=False` 构造参数；GPU 后端启动后才建标签绑定（`_start_label_binding`，层级尺寸取自 provider、分块边长取自控制器的 grid），停止 GPU 后端时先释放它；`pause_requests` / `resume_requests` 转交；公开 `set_mask_sources` / `set_mask_style` / `mask_status`（无 GPU 时 `available=False`、「Masks need the GPU display」并在终端打印）；**换数据源时清空已设置的 mask 来源**（它们属于旧的数据源，由 ④c 重新设置）。
  - `ui/main_window.py`：Step3 的 mount 构造加 `labels=True`，只此一处。
  - **实施中查明的环境问题（advisory）**：本机 WSL 的 GPU（D3D12 转译，Mesa 23.2）上，只要本进程画过 mask（着色器真正读取纹理），**进程退出时约有一半概率段错误**——发生在全部测试通过、所有对象都已释放之后的解释器收尾阶段。排查：不读纹理的替身着色器（相同的 Python / GL 调用序列）从不出现；与 R32UI 无关（改 RGBA8 仍出现）；把 `texelFetch` 换成 `texture()`、关闭着色器缓存、`glFinish`、提前销毁 QApplication 都无效；`os._exit` 时不出现。判断为驱动在收尾时的缺陷，不在我们的调用序列里。另外，HEAD 自己的 `test_step1_gpu_roi_polygon_clip` / `test_step1_gpu_roi_clip` 在本机同一进程里建到约第 5 个 GL 上下文就中止（与本块无关，HEAD 上一样）。**对产品的影响**：在本机退出程序时可能报段错误（此时数据早已写完）；部署机（真正的 NVIDIA 驱动）是否出现须在真机验收时确认。本机跑真实 GL 测试的环境：`QT_QPA_PLATFORM=xcb PYOPENGL_PLATFORM=glx MESA_D3D12_DEFAULT_ADAPTER_NAME=NVIDIA`（不设最后一项时 WSLg 选 Intel 核显）。
  - 测试：`tests/test_step3_label_render.py`（22 条，真实 GL 读回，本机 RTX 3060 经 D3D12）：编号图在设备像素比 1 / 1.5 / 2 下逐像素等于独立的 CPU 参考；后画的块连同编号 0 覆盖先画的；轮廓在 3 种设备像素比 × 线宽 0 / 1 / 2 / 4 下逐像素等于 `outline_reference`；填充色等于 `fill_colour`，透明度 0.5 混合误差 ≤ 1；核层在细胞层之上；凹多边形 ROI 外 mask 不上屏（只重画 mask 时同样）；只改 mask 不重新合成通道；`labels=True` 未设 mask 时画面与 `labels=False` 逐像素相同；预算超额被拒绝。`tests/test_step3_label_binding.py`（26 条，离屏，假控制器与假 GPU 层、真实线程与 `step3_masks`）：只读可见块 + 一圈、中心优先、读出等于 `read_label_tile`；相机移开丢弃队列并计数、迟到结果丢弃并计数；节流与停手补一次；放大 / 缩小时旧层级在目标层级之下（远的先画）；离开视野的块释放；无金字塔粗层不请求并给原因、第 0 级照读；缺金字塔先补生成再显示粗层；纯核方法只有核层；两层时核在后、改显示设置不读数据；暂停不请求、恢复补一次；暂停取消补生成（不留 `.partial`）、恢复后重来；`dispose` 结束线程并清空 mask；换源丢弃旧代次；预算：先丢外圈、再丢远处可见块并提示；线程持有量不超预算；GUI 未接收结果时线程停在预算处；屏幕半径的取整（9 组逻辑线宽 × 设备像素比，含封顶 8）。`tests/test_step3_label_mount.py`（3 条）：无 GPU 时不建标签绑定并给出原因；Step1 的 viewer 不画 mask；Step3 的 viewer 有标签绑定（层级与分块边长取自自身的栈）、mask 画出、暂停 / 恢复转交、换数据源后旧线程结束且 mask 来源清空、关闭后线程结束。
  - 反向注入 15 处，各使至少 1 条变红：离屏 10 处（不读外圈、目标层级先画、无金字塔也请求粗层、暂停不取消补生成、超预算不丢外圈、迟到的无用结果被接收、核画在细胞下面、线程无视预算、远处先读，以及写死半径取整后的「忽略设备像素比」）；真实 GL 5 处（编号 0 当透明、轮廓半径少 1、不做多边形裁剪、填充散列常数改动、像素中心偏移半像素）。首轮「线程无视预算」「忽略设备像素比」两处未被抓到，分别补了「GUI 未接收结果时」与「半径取整」两类测试后抓到。
  - 回归：与 `git archive HEAD`（`f91824d`）逐条对比，**无新增失败**。
    - 离屏（每模块单独进程）26 个模块：Tissue Preview 契约、通道面板 / 工作台、标签金字塔、Navigator 权限、Step3 各模块、Step1 各 viewer / 相机 / 暂停 / 勾选模块、GPU 接管 / 数据供给 / 请求门 / 总览跳过、Step2 重映射集成等。两边相同：`test_global_channel_dock.py` 1 条（字体），`test_step1_montage_view.py` 第 27 条后崩溃。
    - 真实 GL（本机，**每条测试单独进程**，避开驱动的上下文数限制）：Step1 的 8 个 GPU 模块共 150 条 + 本块 25 条。两边相同的唯一失败：`test_step1_gpu_takeover.py::test_the_mounted_widget_framebuffer_holds_real_gpu_pixels`（断言厂商名含「nvidia」，本机经 D3D12 显示为「Microsoft Corporation」，环境原因）。名字带空格的 7 条参数化用例另行单独补跑，两边都通过。
  - 真机：本块用户看不到变化（没有人调用 `set_mask_sources`），真机验收随 ④c；届时一并确认上面的退出段错误在部署机上是否存在。

### 块 ④c — Step3 的 mask 界面与接线（申请 v2，按独立审核修订，**用户 2026-09-27 批准**；已实施，**真机验收通过（第 1–4、6 项；第 5 项真机未验）**）
- **必要性**：④a（数据层）与 ④b（GPU 渲染）已就位，但产品里没有人选运行、没有人把 mask 交给 viewer，用户看不到任何 mask。本块按第 ④ 步裁定 5、6 加界面并接线，是第 ④ 步的最后一块，**也是 ④a、④b 的真机验收**。
- **只读调查结论**：
  - Step3 页面（`ui/step3_page.py`）只负责摆放；右栏顶部现在一行只有右端的 `Overlay` / `Fusion`（`assemble(mode_widgets=...)`，`:111-117`），控件由 `main_window._build_step3_page` 构建（`main_window.py:1566-1633`）。现有测试没有锁定这一行的内容。
  - 当前 ROI 工作区：`step0_output["roi_dir"]`；当前 ROI：`_active_roi`（`name` 与 `bbox_fullres` = y0, y1, x0, x1）。Step2 用的 `rois` 就是窗口的 `_rois`，`roi_name == _active_roi["name"]` 成立（`segment_merge_worker.py:3016`）。
  - 进入 Step3：`_go_to_step3(output_dir)`（`:4515`）；Step2 完成对话框的「Open QC Viewer」经 `open_qc_requested(output_dir)` 进来（`step2_page.py:2608`），这就是 `choose_run` 的「显式目录」；Step2 跑完另发 `segmentation_done(output_dir)`（`:2570`）。Step3 的 viewer 在 `_step3_follow_step` 里打开 / 恢复（`:1914-1950`）。
  - 换数据集：`_close_step3_viewer()`（`:2685`）关闭 Step3 的 mount；④b 已让 mount 在关闭 / 换数据源时丢掉 mask 来源。
  - viewer 各层级尺寸：`mount.host.stack.provider.level_shape(l)`，与 ④b 标签绑定用的是同一组。
  - 界面规则：Step1 的 viewer 下方没有状态行，警告与错误走终端（`UI_SURFACE_RULES.md` Step1 一节）；Step3 一节写着「no mask yet」——本块须改写这两处中与 Step3 相关的描述。
- **界面**（用户已裁定位置与内容；**新增的可见控件以下列为准**）：Step3 右栏 `Viewer` 标签页顶部一行，从左到右：
  1. **运行下拉框**：当前 ROI 工作区的已完成运行（④a `list_runs`），每项显示「方法名 · 日期 时间」，active 运行加「(active)」；新到旧。没有运行时只有一项「No segmentation results for this ROI」并禁用。默认按 ④a `choose_run`（显式目录 → 保持当前 → active → 最新）。
  2. **`Cell mask ▾`** 与 **`Nucleus mask ▾`** 两个按钮，样式同 `Overlay` / `Fusion`（`MODE_BUTTON_QSS`）；按钮文字前有一个该 mask 当前颜色的小色块。点开是下拉面板：`Show`（勾选，**两种 mask 默认都显示**）、颜色（一排预设色块 + `Custom…` 打开系统取色对话框；**颜色只用于轮廓**——`Fill` 按细胞编号给固定随机色（④b），面板上写明「Fill uses one colour per cell」）、`Opacity`（滑块 0–100 %，默认 75 %，轮廓与填充都用）、`Width`（1–4，逻辑像素，默认 1，只用于轮廓；隐藏用 `Show`，所以不提供 0）、`Outline` / `Fill` 二选一（默认 Outline）。当前运行没有这种 mask 时按钮禁用。默认颜色：细胞绿、核青（④b 的默认值）。
  3. **提示文字**（占剩余宽度，放不下时截断，悬停显示全文；**终端只在提示内容变化时打印一次**，拖动、缩放时不重复刷屏）：按优先级只显示一条——没有运行；mask 读不出（④a 的原因，并建议「re-run Step2」）；标签块读取失败（④b 的错误）；「Masks need the GPU display」；「Preparing zoomed-out masks…」（补生成中）；「Zoomed out, masks cannot be shown: 原因」（只在当前处于粗层时）；「Mask memory is full: part of the view has no mask」。都没有时为空。
  4. 右端 `Overlay` / `Fusion`（不变）。
- **接线**（`ui/main_window.py`）：
  - `_step3_refresh_masks(requested_dir=None)`：读当前 ROI 工作区的运行 → 选运行 → 按 `_active_roi` 与 viewer 层级 `resolve_masks` → 更新下拉框、两个按钮的可用状态与提示。**只有选中的运行或解析出的来源（mask 路径、bbox、金字塔路径）与正在显示的不同时，才调用 `mount.set_mask_sources`**；否则只更新列表——进入 Step3、后台新结果到达时，正在显示的 mask 不被清空、不重新读取。换数据集后 mount 已丢掉来源（④b），下一次进入必定重新设置。
  - 触发：进入 Step3 且 viewer 打开 / 恢复之后（带 `_go_to_step3` 的 `output_dir` 作显式目录）；在 Step3 页面上收到 `segmentation_done`（Step2 在后台跑完）时刷新列表并保持当前选择；用户切换下拉框；换数据集（`_close_step3_viewer`）时清空列表与选择。运行列表只在这些时刻读文件（几个 JSON），不轮询。
  - 显示设置：两个面板的改动直接调 `mount.set_mask_style`。颜色、透明度、线宽、Outline / Fill **只重画、不读数据**；`Show` 由关到开时，允许补读当前视野缺的标签块（④b `set_style` 的现有行为：隐藏期间不读）。设置在本次运行内保留（换运行、换数据集都不重置），不写入任何文件；mount 重建（换数据集）后，mount 自己保存的设置（④b `_mask_styles`）重新应用到新的标签绑定。
  - 提示随状态更新：mount 新增信号 `mask_status_changed`，转发 ④b 标签绑定的 `status_changed`（后者每次规划、补生成开始 / 结束时发出）。
- **④b 模块的最小扩围（v2，须批准）**：`ui/step3_label_binding.py` 只改错误状态——① 标签块读取失败时发出 `status_changed`，使提示立即更新；② `set_sources` 清除上一个来源的错误；③ 旧代次的迟到错误直接丢弃（先查代次，再记错误）。读取架构、线程、预算、层级规则都不改；`tests/test_step3_label_binding.py` 加对应用例。
- **白名单**：`ui/step3_label_binding.py`（仅上述三点）；`ui/step3_mask_bar.py`（新：这一行与两个下拉面板，只负责控件，不读文件）；`ui/step3_page.py`（`assemble` 接收这一行的控件）；`ui/main_window.py`（构建控件、上述接线）；`ui/step1_viewer_mount.py`（只加 `mask_status_changed` 信号的转发）；`UI_SURFACE_RULES.md`（Step3 一节：新的一行，删除「no mask yet」）；`docs/user_guide.md`、`docs/用户指南.md`（Step3 一节）；新测试 `tests/test_step3_mask_bar.py`（控件：禁用、默认值、改动转发、颜色、截断）与 `tests/test_step3_mask_wiring.py`（合成项目：进入 Step3 时的选择规则、显式目录、Step2 新结果、换数据集清空、ROI 不符 / 只有 TIFF / 纯核方法时按钮与提示、无 GPU 提示）；本文档。其他测试失败须停下说明。
- **不改的范围**：④a / ④b 的模块（发现缺陷先停下说明）、GPU 层、Step1 / Step0 / Step2 页面与行为（`segmentation_done` / `open_qc_requested` 只接收不改）、Step3 左栏、Tissue Navigator、任何文件写入（本块不写文件；补生成金字塔是 ④b 已批准的写入）。
- **风险**：
  - 新的可见界面：按裁定的位置与内容，界面规则与两份用户指南同步。
  - `segmentation_done` 在 Step2 后台完成时到达：只在 Step3 页面上时刷新，其他页面等下次进入。
  - 多个 ROI 的运行只显示当前 ROI（`_active_roi`）的 mask；要看别的 ROI，先在 Step1 切换当前 ROI（现有行为）。
  - 本机 D3D12 驱动的退出段错误（④b advisory）在真机验收时须确认部署机是否存在。
  - 回退：去掉这一行与接线，④b 的 mount 仍然不画任何 mask。
- **验收门**：
  - 控件：没有运行 / 当前运行缺某种 mask 时相应控件禁用；默认值（两种都显示、绿 / 青、75 %、1、Outline）；颜色、透明度、线宽、模式每项改动只调一次 `set_mask_style`、不触发读取；`Show` 由关到开只补读当前视野缺的块；预设色与自定义色都生效（自定义取色对话框在测试里替换为直接返回颜色，不弹模态框）；提示截断时悬停有全文，终端对同一条提示只打印一次。
  - 不重复设置来源：保持同一选择时的刷新（进入 Step3、后台新结果到达）不调用 `set_mask_sources`，已画的标签块不被清空、不重新读取。
  - 错误提示（④b 扩围）：标签块读取失败时提示立即出现；切换到正常的运行后旧错误消失；旧来源迟到的错误不出现。
  - 设置保留：换数据集（mount 重建）后再进入 Step3，之前改过的颜色 / 透明度 / 线宽 / 模式 / Show 在新的标签绑定上生效（检查绑定的实际设置，不只看控件外观）。
  - 选择规则：进入 Step3 时 active 运行被选中；从 Step2 完成对话框进入时选中那次运行；在 Step3 时 Step2 新结果到达，列表出现新运行、当前选择不变；换数据集后列表清空、mask 不再显示。
  - 分类与原因：纯核方法只有 `Nucleus mask` 可用；nuclear-guided 两个都可用；ROI 不符、只有 TIFF 的运行两个都禁用且提示原因（含「re-run Step2」）；无 GPU 时提示「Masks need the GPU display」。
  - 回归：Step3 / Step1 / GPU / 标签相关模块与 HEAD 逐条对比，无新增失败。
  - **真机（用户，同时是 ④a、④b 的真机验收）**：
    1. 用 `~/fusion_data/test1` 进入 Step3：选中的运行符合「显式指定 → 当前选择 → active → 最新」（从导航条进入时即 active 运行），整张图上看到细胞轮廓，与组织对齐；放大、缩小、拖动、Tissue Navigator 空降、patch 空降时 mask 跟随、无错位、无明显延迟。
    2. 缩放到最粗与最细两端，mask 都显示（块 N 的金字塔）；在金字塔有效、预算足够、旧块覆盖视野时，换层级不闪空。冷空降（目标区域从未读过）、读取失败、预算不足时按已批准的退路验收（暂时没有 mask / 提示原因），不算闪空。
    3. `Cell mask ▾` 改颜色、透明度、线宽，立即生效；改为 Fill 时每个细胞一种固定颜色（所选颜色不用于填充）；`Show` 关掉再打开。
    4. 切换运行（若有多个）；在 Step2 再跑一次、用完成对话框进入 Step3，选中的是新运行。
    5. 一次没有金字塔的旧运行：**只复制运行目录不行**——运行的 metadata 用绝对路径指向原 mask，仍会读写原运行。做法：我准备一个脚本，把整个测试项目复制到临时目录、删去复制件里的 `label_pyramid_*.zarr`，并把复制件 metadata 与索引里的路径全部改写为指向复制件，再核对没有任何路径指向 `~/fusion_data`；程序打开这份复制件。若现有界面打不开一份复制的项目（「打开别的项目」尚未支持，S2 冻结），本项只由自动测试覆盖，并如实记为「真机未验」。预期：先显示「Preparing zoomed-out masks…」，完成后缩小时也有 mask。
    6. 退出程序：记录是否出现段错误（④b advisory）。**若在部署机上正常关闭时出现，记为生命周期验收未通过**，第 ④ 步不宣称全部通过，另立块处理。
- **用户裁定（2026-09-27，全部同意）**：
  1. 颜色：预设色块 + `Custom…` 系统取色对话框。
  2. 线宽 1–4，不提供 0，隐藏用 `Show`。
  3. 提示文字：截断 + 悬停全文 + 终端只在提示变化时打印。
  4. 批准上面 ④b 模块的最小扩围（只改错误状态的通知、换源清除、迟到错误丢弃）。
- **执行记录**（2026-09-27，未提交）：
  - `ui/step3_label_binding.py`（④b 扩围，只改错误状态）：读取结果先查代次再记错误（旧来源的迟到错误直接丢弃并计入 `late_dropped`）；读取失败与画不出时发出 `status_changed`；`set_sources` 清除上一个来源的错误。
  - `ui/step1_viewer_mount.py`：信号 `mask_status_changed`（转发标签绑定的 `status_changed`，标签绑定建立 / 释放、设置来源时也发出）；只读的 `mask_sources()` / `mask_styles()`，供窗口比较与测试。
  - `ui/step3_mask_bar.py`（新）：运行下拉框、`Cell mask ▾` / `Nucleus mask ▾`（带颜色色块，下拉面板：`Show`、7 个预设色 + `Custom…`、`Opacity`、`Width` 1–4、`Outline` / `Fill`、「Fill uses one colour per cell」）、截断并悬停显示全文的提示；只有控件，不读文件。`Custom…` 的取色经一个可替换的入口（产品用系统对话框，测试直接返回颜色）。
  - `ui/step3_page.py`：`assemble` 接收 `mask_widgets` 与 `mask_hint`，与 `Overlay` / `Fusion` 同一行，提示占剩余宽度。
  - `ui/main_window.py`：构建这一行；`_step3_refresh_masks`（列运行 → `choose_run` → 按 `_active_roi` 与 viewer 层级 `resolve_masks` → 按钮可用状态 → **来源有变化才** `set_mask_sources`）；触发点：viewer 打开 / 恢复 / 打开失败之后（带 `_go_to_step3` 显式给出的目录，只用一次）、下拉框选择、在 Step3 页面上收到 `segmentation_done`；换数据集时清空列表、按钮与提示，**并清掉 mount 记住的 mask 来源**（mount 关闭后仍保留来源，否则重开时会把旧数据集的 mask 用到新数据上——实施中发现，属本块范围）；提示按申请的优先级，终端只在内容变化时打印一次。
  - 文档：`UI_SURFACE_RULES.md` Step3 一节（新的一行；删除「no mask yet」）；两份用户指南 Step3 一节（第 6 条改为 mask 的用法；「正在重建」的提示改为只差 patch 按钮；产出说明补生成的金字塔）。
  - 测试：`tests/test_step3_mask_bar.py`（5 条）：默认值（两种都显示、绿 / 青、75 %、1、Outline、线宽范围 1–4）；每个控件只发一次、只含自己的字段；预设色、自定义色（含取消）与按钮色块；运行列表的空状态、选中、选择信号；按钮禁用；提示截断与悬停全文。`tests/test_step3_mask_wiring.py`（10 条，真实窗口 + 合成项目）：进入时选 active；完成对话框的运行；列表中选择；导航条回来保持选择；Step3 上收到新结果时列表加一项、选择不变、**不再次设置来源**，普通再次进入也不设置，不在 Step3 时不刷新；纯核 / nuclear-guided / expansion 三种方法的按钮；别的 ROI 的运行两个都禁用并提示原因与「re-run Step2」；无运行的提示与只打印一次；换数据集清空列表与 mount 的来源；面板设置到达 mount；GPU：mount 重建后标签绑定的**实际**设置与来源都恢复。`tests/test_step3_label_binding.py` 加 2 条：读取失败立即提示、换到正常来源后错误消失；旧来源的迟到错误不出现。
  - 反向注入 11 处，各使至少 1 条变红：总是重设来源、换数据集不清来源、任何页面都刷新、忽略完成对话框的运行、不提示读不出、每次都打印、换源不清错误、错误不发信号、先记错误再查代次、线宽允许 0、提示无悬停全文。
  - 回归：与 `git archive HEAD`（`d24bc7e`）逐条对比，**无新增失败**。离屏 31 个模块（含界面规则契约 `test_ui_surface_contract`、Step1 → Step2 交接、Step3 / Step1 viewer / GPU / 标签相关模块），两边相同：`test_global_channel_dock.py` 1 条（字体）、`test_step1_montage_view.py` 第 27 条后崩溃。真实 GL（每条单独进程）：GPU 接管 / 总览跳过 / 请求门 / 标签渲染 / 标签 mount 共 92 条 + 本块接线 10 条，两边相同的唯一失败仍是 `test_the_mounted_widget_framebuffer_holds_real_gpu_pixels`（厂商名，环境原因）。
  - **真机验收（用户 2026-09-27，同时是 ④a、④b 的真机验收）**：第 1–4 项通过；第 6 项退出程序无段错误（本机 ④b 测试进程中的退出段错误未在产品中出现）；第 5 项（无金字塔的旧运行现场补生成）**真机未验**，只有自动测试覆盖。真机中发现：`cellpose_nuclei_expansion` 的新运行在 Step3 没有核 mask——Step2 丢弃了引擎算出的核（`segment_merge_worker.py:2125`），按 ④a 批准的分类表核按钮禁用；用户裁定所有计算了核的方法都必须保留核 mask 并能在 Step3 显示，另立块（见第六节）。第 5 项的复制件由任务临时目录里的脚本 `make_pyramidless_copy.py` 生成（复制一个 ROI 工作区、去掉 `.dat` 与 `label_pyramid_*.zarr`、把复制件全部 JSON 里的项目路径改写到复制件并核对无残留；原项目只读）。

### 块 N2 — 所有计算了核的方法都保留核 mask，并能在 Step3 显示（申请 v4，按用户裁定与独立审核修订，**用户 2026-09-27 批准**；已实施，**真机验收通过**）
- **必要性**：用户裁定（2026-09-27，④c 真机验收中发现）：凡是计算了核的方法，都必须保留核 mask，并能在 Step3 的 `Nucleus mask` 里显示。
- **只读调查结论**：
  - 8 个方法都在引擎子进程里分割（`_runs_on_engine`，`segment_merge_worker.py:1979`）；引擎按方法返回 `{cell, nucleus}`（`seg_runner/engines.py` `run()`）。各方法的核：

    | 方法 | 引擎算核吗 | Step2 现在 | Step3 现在 |
    |---|---|---|---|
    | cellpose_wholecell_fusion、mesmer_whole_cell | 否（只预测整细胞） | — | 只有细胞（正确） |
    | cellpose_nuclei_dapi、stardist_nuclei_dapi、mesmer_nuclei | 是，核**就是**主输出 | 存为 `global_mask*` | 显示为核（正确） |
    | cellpose_nuclei_expansion、stardist_nuclei_expansion | 是：先得核，再扩张成细胞 | **丢弃核**，只存扩张后的细胞（`:2125`「expansion: the expanded cells only」） | 核按钮禁用 |
    | mesmer_nuclear_guided | 是：整细胞与核各预测一次 | 存核，但**用细胞的编号表重编号**（见下） | 显示的核很可能不对 |
    | HQ / HQ2 / CDS（不维护） | 是 | 存核（核编号 = 细胞编号） | 显示 |

  - **核的重编号用的是细胞的编号表**：两个切块循环（ROI `:2621-2629`、全图 `:3483-3490`）都是 `remapped_nuclei = lut[where(nuclei <= n_raw, nuclei, 0)]`，其中 `lut` 与 `n_raw` 来自细胞 mask。这只在「核编号 = 它所属细胞的编号」时成立：HQ 系成立；expansion 也成立（扩张不改编号）。但 **mesmer_nuclear_guided 的核与细胞是 Mesmer 两次独立预测的两套编号**：核 k 会被接到细胞 k 的全局编号上（错的细胞），细胞 k 不在本块时核被丢掉，编号大于细胞数的核也被丢掉。本机没有 Mesmer 模型，无法实测；依据是引擎代码（两次独立的 `predict`）。
  - 核的写盘整套绑在 `is_hq` 上（`is_hq = HQ 系 or nuclear-guided`，`:2343`）：核的 memmap、`global_nuclei_mask*.zarr`、核的 OME-TIFF（float32）、金字塔，但同一个标志也驱动 HQ 的质检表与 HQ 专用 metadata（`_hq_meta_fields`，`:2904`），expansion 不能直接并进 `is_hq`。
  - Step4 不读任何核文件（`feature_extract_worker.py` 等处无引用），保存核不改变 Step4。
  - 「从 .npy 恢复」的运行只读回每块的主 mask（`:2450-2466`），拿不到核。
  - Step1 预分割（`core/preseg_run.py`）已经同时保留细胞与核，不在本块范围。
- **用户裁定（2026-09-27）**：
  1. 凡是计算了核的方法都必须保留核 mask，并能在 Step3 显示。核与整细胞 mask **骑跨**（不被一个细胞完全包含）时丢弃该核。须考虑速度与资源；必要时可用 Rust，不强制。
  2. **一个细胞可以同时保留多个核**（正常生物学现象）。因此（按审核修正措辞）：**每个保留的核唯一归属于一个细胞；一个细胞可以有 0、1 或多个核**——「核 → 细胞」是单值映射，「细胞 → 核」是一对多。
  3. **不再写核的 OME-TIFF**（expansion 与 nuclear-guided）；外部软件需要时，作为下一阶段的「按需流式导出」。
  4. Step4 将来要读核（核形态、核面积、核表达量、胞质表达量），并可能用 Rust 重构：Step2 的保存格式对标未来的 Step4；本次允许扩大 Step2 的白名单。
  5. 顺序：本块 → Step3 剩余的第 ③ 步 → Step4 → 下一阶段优化（第六节）。
- **归属规则**（只用于新的核通路：两个 expansion 与 nuclear-guided；在每个切块里、细胞归属与重编号之前做）：
  - 核 n 的全部像素都落在**同一个**细胞 c 里（像素下的细胞编号只有 c 一种，且不为 0）→ 保留，所属细胞 = c。否则丢弃，分三类计数：`multiple_cells`（跨两个以上细胞）、`partial_background`（一个细胞 + 背景）、`outside_cells`（完全在背景）。
  - 同一细胞里的多个核**全部保留**。
  - 核的去留**跟随它的细胞**：细胞被本切块保留（现有的归属规则），它的核才保留——跨切块不会重复、也不会有核失去细胞。
  - expansion 天然满足（核 ⊂ 扩张后的同编号细胞），规则等于核对；nuclear-guided 靠它把 Mesmer 两套独立编号接起来。
  - **速度（实测，本机）**：4096² 切块、约 1.2 万个核，只在核像素上对打包的（核, 细胞）编号取 `np.unique` 判定包含，约 32 ms；推理每块几十秒以上，**不需要 Rust**。额外内存约为核像素数 × 8 字节。
  - 严格的「完全包含」规则可能让两次独立预测的边界差 1–2 像素的核被丢弃：nuclear-guided 的真机验收除了看画面，**必须报告保留率**（见验收门）；若某真实数据集保留率明显偏低，不是代码缺陷，而是这条科学规则需要重新讨论。
- **LabelStore：统一的语义入口**（对标未来的 Step4 / Rust；文件名保持历史兼容，**metadata 才是语义权威**）：
  - 每个区域的 metadata（`segmentation_meta_<ROI>.json`、运行汇总里该 ROI 的记录，全图模式同理）新增：

    ```
    label_store:
      version: 1
      complete: true
      cell:            {path, dtype: uint32, shape, chunks: [1024, 1024], n_objects}  或 null
      nucleus:         {path, dtype: uint32, shape, chunks: [1024, 1024], n_objects}  或 null
      nucleus_to_cell: {path, dtype: uint32, length: M + 1, chunks}                    或 null
      relation:        {nucleus_to_cell: many_to_one} 或 null
      nuclei: {predicted, kept, retained_fraction,
               dropped: {multiple_cells, partial_background, outside_cells}}           或 null
    ```

  - 各类方法的结构：

    | 方法 | `cell` | `nucleus` | `nucleus_to_cell` |
    |---|---|---|---|
    | 整细胞（cellpose_wholecell_fusion、mesmer_whole_cell） | `global_mask*` | null | null |
    | 纯核（cellpose / stardist / mesmer 的纯核方法） | null | `global_mask*` | null |
    | expansion、nuclear-guided | `global_mask*` | `global_nuclei_mask*` | `global_nuclei_cell*` |
    | HQ / HQ2 / CDS（不维护） | 不写 `label_store`，保持原样 | | |

  - **编号契约**：细胞编号区域内连续 1 … N、核编号区域内连续 1 … M（0 为背景，各自互不相同）；每个保留的核完全在它的细胞内；`nucleus_to_cell[k]` = 核 k 的细胞编号（第 0 项为 0）。**N 或 M 达到 2^32 时直接报错**，不允许 uint32 回绕。
  - **二维数组**：细胞与核两个 zarr v2、uint32、同一形状、同一分块 1024 × 1024、同一压缩（Blosc lz4），分块 (i, j) 覆盖同一批像素；Rust 用 `zarrs` 即可读。
  - **一维对应表**：zarr v2、uint32、分块 1 048 576 项；每个切块产出一个 `numpy.uint32` 数组，按块追加写入（`resize` + 写入末尾），内存里只有当前切块的数组与一个小缓冲——**不用 Python 整数列表累积**。
  - 三个数组的属性里写同样的契约摘要（`label_store_version`、`kind`、编号规则、对象数）。
  - Step3（`core/step3_masks.py`）：**有 `label_store` 时以它为准**（不再按方法猜 `global_mask` 是什么）；没有时（旧运行）沿用现在的分类表。未来的 Step4 也只读 `label_store`。
- **科学输出的事务**（与标签金字塔不同：金字塔是显示用的派生物，失败不影响运行；核与对应表是科学输出）：
  - 声明保留核的方法，一个区域的科学输出 = 细胞数组 + 核数组 + 对应表 + `label_store`。核数组与对应表先写到 `*.partial`，全部写完才改名；`label_store`（`complete: true`）最后写。
  - **任一项写失败**（磁盘满、I/O 错误等）→ 本次运行失败，**不登记**（不进 `roi_index`、不写汇总的完成状态），清理本次的 `*.partial`；已有的其他运行不受影响。Stop 同理（沿用现有规则）。
  - 核的标签金字塔仍按块 N 的规则：失败只打印，不影响运行。
- **不整图展开、临时文件**：新核通路里没有对整图的 `astype` / `np.array`：核 memmap → zarr 按 1024 行一条写（与细胞相同）；核的临时 memmap（`global_nuclei_mask*.dat`）在核 zarr、对应表与金字塔都完成后**删除**（没有读取方）；失败或 Stop 时同样删除。细胞 mask 与 DAPI 现有的 OME 导出与 `.dat` 留到下一阶段。
- **做法**：
  1. 新文件 `core/nuclei_pairing.py`（无 Qt）：`pair_nuclei(cell_local, nuclei_local)` → 保留的核（局部连续编号）、每个核的局部所属细胞（uint32 数组）、四项计数。
  2. `_segment_tile_contract`：两个 expansion 方法与 nuclear-guided 返回 `{"mask": 细胞, "nuclei": 配对后的核, "nuclei_cell": 所属细胞, "nuclei_counts": 计数}`。
  3. 新标志 `keeps_secondary_nuclei`（两个 expansion、nuclear-guided）驱动新的核通路：配对、核 memmap、合并（按细胞的 `lut` 决定去留：所属细胞的 `lut` 为 0 的核丢弃；保留的核按局部编号顺序接到核的全局序列后面，另一个计数器；全局所属细胞 = `lut[局部所属细胞]`，按块追加到对应表）、事务写出、`label_store`、临时文件删除。**HQ / HQ2 / CDS 走 `is_hq` 的原路径，完全不动**；需要核 memmap 的条件写成 `is_hq or keeps_secondary_nuclei`。nuclear-guided 原来走的 `is_hq` 核分支改为走新通路（它原来的「用细胞编号表映射核」就是要修的缺陷）。ROI 与全图两条循环同样改。
  4. metadata：`label_store`；nuclear-guided 的 `nuclei_mask_path`（原指核 OME）改为空；新增 `nuclei_zarr_path`、`nuclei_cell_table_path`。
  5. Step3：见上（`label_store` 优先）；旧运行没有核文件 → 核按钮禁用，原因「this run was made before nuclei were kept — re-run Step2」。
  6. 「从 .npy 恢复」的运行：没有核，`label_store.nucleus` 为 null 并写明原因。
- **白名单（按裁定 4 扩大）**：`workers/segment_merge_worker.py`；`core/nuclei_pairing.py`（新）；`utils/mesmer_utils.py`（仅 `mesmer_metadata` 的核路径字段，若需要）；`core/step3_masks.py`（`label_store` 优先与分类表）；测试：`tests/test_step3_masks.py`、新测试 `tests/test_nuclei_pairing.py`、`tests/test_step2_keeps_nuclei.py`，以及现有 Step2 测试中**因不再写核 OME-TIFF 而须改的断言**（实施前列出具体用例报批）；`docs/user_guide.md`、`docs/用户指南.md`；本文档。其他测试失败须停下说明。
- **不改的范围**：引擎（`seg_runner/`）、Step1 预分割、HQ / HQ2 / CDS 的行为、细胞 mask 的归属与编号、细胞 mask 与 DAPI 的 OME 导出与 `.dat`、Step4、Step3 界面与 GPU、Step2 的缓存 / 切块策略 / 引擎通信。
- **风险**：
  - Step2 是科学输出：细胞 mask 必须与改动前逐像素相同——验收锁定。
  - nuclear-guided 无法在本机用真实 Mesmer 验证（无模型）：用替身引擎的合成输出测试；真机验收（含保留率）随 Mesmer 暂缓项。
  - 已有的 expansion 结果不会自动多出核，须重跑；已有的 nuclear-guided 结果里的核是按旧（有缺陷的）规则保存的，Step3 仍会显示，须重跑。
  - 回退：恢复这些文件。
- **验收门**：
  - 配对函数（合成标签）：完全包含 → 保留并记下所属细胞；`multiple_cells`、`partial_background`、`outside_cells` 各自正确计数并丢弃；同一细胞两个核 → 都保留、各有编号、都指向该细胞；独立编号（核编号大于细胞数、与细胞编号无关）正确。
  - expansion（真实 Cellpose / StarDist 引擎、合成图像，ROI 与全图两种模式）：细胞 mask 与改动前**逐像素相同**；核与细胞同形状同分块、uint32；核编号 1 … M 连续且唯一；对应表长度 M + 1；逐像素「核像素所在的细胞 = 对应表[核编号]」；核金字塔通过 ④a 的完整校验；`label_store` 完整且计数与实际一致；临时核 `.dat` 已删除；没有核 OME-TIFF；④a `resolve_masks` 按 `label_store` 得到细胞与核两个源。
  - nuclear-guided（替身引擎返回独立编号的两套标签，含三类丢弃、同一细胞多核、核编号大于细胞数、跨切块的细胞）：上面的契约全部成立；细胞 mask 与改动前逐像素相同；`label_store.nuclei` 的保留率与计数正确。
  - **切块边界**：核碰到读取区边缘、细胞跨过本块的归属区、细胞归邻块所有——配对 → 归属 → 核的全局编号不会错误保留或重复核（每个核恰好出现一次，且在它的细胞里）。
  - **普通 I/O 失败的事务**：细胞与核数组写成、对应表写到一半时注入普通写入错误 → 运行不登记、`label_store` 不存在或不是 complete、`*.partial` 被清理，不会留下「看起来成功但科学输出不完整」的运行；已有运行不受影响。
  - 纯核与整细胞方法：`label_store` 按表写出（`cell` / `nucleus` 各自为 null）；HQ 系：输出与改动前逐像素相同（回归），不写 `label_store`。
  - 旧运行（无 `label_store`、无核文件）：Step3 按原分类表，核按钮禁用并给出原因；「从 .npy 恢复」的运行：无核并写明原因。
  - 资源：新核通路不对整图调用 `astype` / `np.array`（代码审查 + 一次合成大图的内存峰值检查）；对应表按块写入。
  - Stop：中途停止不登记、不留半成品。
  - 回归：Step2 / 标签金字塔 / Step3 相关模块与 HEAD 逐条对比，无新增失败。
  - 真机（用户）：重跑 `cellpose_nuclei_expansion`（和 / 或 `stardist_nuclei_expansion`），Step3 的 `Nucleus mask` 可用，核在细胞内部、与组织对齐，同一细胞里的多个核各有轮廓；终端与 metadata 给出预测数、保留数、三类丢弃数与保留率。
- **执行记录**（2026-09-27，未提交）：
  - `core/nuclei_pairing.py`（新，无 Qt）：`pair_nuclei(cells, nuclei)` → 保留的核（局部 1 … K）、每个核的局部所属细胞、四项计数、各丢弃原因的原始编号；`owned_drop_counts` 按被丢弃核自己的质心归属计数（重叠区不重复）。
  - `workers/segment_merge_worker.py`：`SECONDARY_NUCLEI_METHODS`（两个 expansion、nuclear-guided）与 `_keeps_secondary_nuclei()`（只在引擎路径上）；`_segment_tile_contract` 对这三个方法返回配对后的核、所属细胞、原始核与丢弃编号；两条循环里：丢弃计数放在第一处提前 `continue` 之前（没有保留细胞的切块也计入）；核的去留跟随细胞的 `lut`，按块接到核的全局序列、对应表按块 `append` 到 `*.partial`；核数组按 1024 × 1024 **分块**写进 `*.partial`（实施中改的：照抄细胞的 4096 行整行写法在 5.9 万像素宽的切片上一条约 1 GB），写完才改名；HQ 系仍走原路径（`hq_nuclei = is_hq and not nuclear-guided`）；`label_store`（按方法的四种结构，`nuclei` 一节含预测 / 保留 / 保留率 / 三类丢弃），写进区域 meta、ROI 记录与汇总；`nuclei_zarr_path`、`nuclei_cell_table_path`；细胞数 / 核数达到 2^32 报错；nuclear-guided 不再写核 OME-TIFF；核的临时 `.dat` 用后删除；`run()` 的 `finally` 统一清理未改名的 `*.partial`；终端打印核的统计。
  - **白名单外、用户 2026-09-27 授权的修复**：整图模式里 `del model` 之后写 Mesmer metadata 又读 `model`（HEAD 上就有，所有 Mesmer 方法的整图运行都会在最后以 `UnboundLocalError` 失败、不登记）——改为在 `del model` 之前记下 device status。
  - `core/step3_masks.py`：有 `label_store` 时以它为准（`complete` 不为真 → 两种都不显示，提示重跑）；expansion 归入「细胞 + 核文件」；旧的 expansion 运行提示「this run was made before nuclei were kept — re-run Step2」。
  - 文档：两份用户指南（Step2 产出：核 zarr 与对应表、保留规则、旧结果须重跑；Step3：按钮变灰的几种情况）。
  - 测试：`tests/test_nuclei_pairing.py`（5 条）；`tests/test_step2_keeps_nuclei.py`（8 条：替身引擎的 nuclear-guided，ROI 与整图两种模式的完整契约（含三类丢弃、同一细胞多核、跨切块、核编号大于细胞数，细胞 mask 等于归属代码的结果），对应表写到一半的 I/O 失败，核数组写到一半的 I/O 失败（最终文件名不出现），Stop，从 `.npy` 恢复，核写出的内存峰值 < 16 MB（60 MB 的图），真实 Cellpose 引擎上的 expansion）；`tests/test_step3_masks.py` 加 3 条（`label_store` 优先、不完整、旧 expansion 的提示）；**白名单外、用户授权**：`tests/test_step2_runner_path.py` 的 `_oracle` 按新规则生成核与对应表的参考，核的比对扩到两个 expansion（新增 `_assert_nuclei`），`tests/test_step2_engine_unified.py` 同步——真实 Cellpose / StarDist 引擎上 expansion 的核与对应表在整图与 ROI 两种模式下都与参考逐像素一致，细胞 mask 不变。
  - 反向注入 11 处，各使至少 1 条变红：部分落在背景的核被保留、核不跟随细胞的归属、丢弃计数不按归属、临时 `.dat` 不删、核数组直接写最终文件名（首轮未被抓到，补了「核数组写到一半失败」一条后抓到）、`*.partial` 不清理、Step3 忽略 `label_store`、接受不完整的 `label_store`、整图展开写核、核编号每块从 1 重新开始。
  - 回归：32 个模块（Step2 全部相关模块、HQ / HQ2 / CDS 的 worker 测试、标签归属、标签金字塔、预分割、remap 提升、Step3 数据层 / 接线 / 标签绑定 / viewer），每模块单独进程、顺序运行，与 `git archive HEAD`（`02ba00d`）逐条对比，**无新增失败**。两边相同：`test_hq_marker_segmentation.py` 2 条（HQ 不维护）、`test_seg_runner_engines.py` 3 条（2 条 Mesmer 无模型、1 条 StarDist 偶发）。本侧一次：`test_step2_runner_path.py::test_a_hand_over_equals_the_runner_with_shared_ownership[stardist_nuclei_dapi-full]` 0.46 % 像素差 1（StarDist 偶发，已知；本块未改纯核方法的路径），单独重跑 3 次都通过。
  - **真机验收通过（用户 2026-09-27）**。nuclear-guided 的真机验收（含保留率）随 Mesmer 暂缓项。

### 第 ③ 步 — patch 按钮条组件化 + Step3 的 patch 空降（申请 v2，按用户裁定修订，**用户 2026-09-27 批准**；已实施，**真机验收通过**）
- **必要性**：Step3 重设计裁定 2（「patch 空降」）与 11（「patch 按钮条抽成 Step1 / Step3 共用组件，允许修改 Step1 并回归」）。现在 Step3 只能靠 Tissue Navigator 或拖动去某个 patch；Step1 的按钮条是主窗口里的一段代码，不能直接放进 Step3。
- **只读调查结论**：
  - Step1 的按钮条在 `main_window.py:1105-1149`（构建）与 `:6107-6197`（`_rebuild_patch_buttons` / `_rebuild_patch_menu` / `_sync_patch_selection_marks` / `_set_patch_btn_state`）：行首一个 `Patch` 下拉按钮（列出**全部** patch，最小宽度 = 一整条内联按钮的宽度，`step1_patch_menu_width()`），后面最多 `STEP1_INLINE_PATCH_BUTTONS = 7` 个内联按钮（42 × 22，间距 4，颜色取 patch 自己的颜色 `patch_button_qss`，与 Step0 的按钮同一外观），名字取 `_patch_label`（稳定编号 / 改名后的名字），按钮与菜单都可勾选、只标出当前选中的一个；同一行右端是 `Overlay` / `Fusion` 与 Step1 的会话按钮。
  - 点击的入口只有一个：`_select_preview_patch(idx)`（`:6303`）——标出选中、记下 `_preview_patch_idx` / `_selected_step1_patch_idx`、调 Step1 viewer 的 `mount.show_patch(patch)`（空降，与 Tissue Preview 同一台相机）、安排 Step1 会话保存；其余分支是旧的 patch 渲染器（整张图 viewer 在前台时不启动）。按钮上的 ⟳ ✓ ✗ 是旧渲染器的加载状态。
  - Step3 的 viewer 是同一个类（`Step1WholeSlideMount`），`show_patch(bbox)` 已有（`step1_viewer_mount.py:1186`），发布相机时带 `step3-patch`。
  - patch 列表只有一份：`_all_patches`，每次变化经 `_on_patches` → `_rebuild_patch_buttons`（Step0 发布、Step1 / Step3 的 Navigator 编辑都走这里，块 S5）。
  - **16 个现有测试文件直接用到按钮条的内部名字**（`_patch_sel_btns`、`_patch_menu_btn`、`_patch_menu`、`_patch_menu_actions`、`_patch_sel_container`、`_rebuild_patch_buttons`、`_set_patch_btn_state`、`_select_preview_patch`、`STEP1_INLINE_PATCH_BUTTONS`、`step1_patch_menu_width`、`patch_button_qss`）。
- **做法**：
  1. 新文件 `ui/patch_strip.py`：`PatchStrip`——`Patch` 下拉按钮 + 最多 7 个内联按钮 + 菜单，外观、尺寸、宽度规则全部照搬 Step1（常量移进这个文件，`main_window` 里保留同名常量指向它）；方法 `rebuild(patches, label_of, colour_of)`、`set_selected(idx)`、`set_label(idx, text)`；信号 `chosen(int)`。只有控件，不读文件、不知道 viewer。
  2. **Step1 改用它，行为与外观不变**：窗口上的 `_patch_sel_btns`、`_patch_menu_btn`、`_patch_menu`、`_patch_menu_actions`、`_patch_sel_container` 保留为指向组件内部的**同名别名**，`_rebuild_patch_buttons` 等方法保留为转交给组件——**现有 16 个测试文件一条都不改**，全部照常通过即为「Step1 不变」的验收。
  3. **Step3 用第二个实例**，patch 的名字与颜色与 Step1 相同，随 `_all_patches` 一起重建；点击 → Step3 viewer 的 `show_patch`（空降），并走与 Step1 相同的选中记录（`_preview_patch_idx` / `_selected_step1_patch_idx`、会话保存），两个条的选中标记一起更新；按钮上不显示旧渲染器的 ⟳ ✓ ✗（Step3 没有旧渲染器）。
  4. 位置按裁定 1；运行下拉框（④c）移到标签栏右侧的角落。
- **白名单**：`ui/patch_strip.py`（新）；`ui/main_window.py`（Step1 改用组件、Step3 的实例与接线）；`ui/step3_page.py`（`assemble` 接收 patch 条与标签栏角落控件）；`UI_SURFACE_RULES.md`（Step3 一节：patch 条，删除「no patch strip (a later block)」）；`docs/user_guide.md`、`docs/用户指南.md`（Step3：patch 空降；「正在重建」的提示可以去掉）；新测试 `tests/test_patch_strip.py`、`tests/test_step3_patch_strip.py`；本文档。其他测试失败须停下说明。
- **不改的范围**：Step0 的 patch 按钮；Tissue Navigator；patch 的保存与同步（块 P / S5）；Step1 的会话按钮、预分割、旧 patch 渲染器；viewer / mount（只调用现有的 `show_patch`）；④c 的 mask 控件本身（只可能换行，见裁定 1）。
- **风险**：
  - Step1 的按钮条是用户每天用的控件：外观与行为逐项不变，由现有测试全部照常通过、加上新测试比对样式表与尺寸锁定；
  - 别名写错会让 Step1 的旧代码操作到旧列表：测试覆盖「重建后别名仍指向组件的当前对象」；
  - 回退：恢复 `main_window.py` 的原段落，删新文件。
- **验收门**：
  - 组件：7 个内联按钮上限、菜单列出全部并与内联同名同状态、最小宽度规则、颜色与样式表与现在的 Step1 逐字相同、选中标记、`chosen` 信号、0 个 patch 时菜单禁用并提示「No patches yet」。
  - Step1：现有 16 个测试文件全部照常通过（不改一条）；按钮条的外观（样式表、尺寸、位置）与改动前相同。
  - Step3：patch 条出现在裁定的位置、与 Step1 同名同色；点击 patch 后 Step3 的 viewer 空降到该 patch（相机与 Step1 的同一次 `show_patch` 结果相同）；在 Step3 的 Navigator 里增删改名 patch 后 Step3 的条立即更新；选中与 Step1 共用（两边标记一致、记进会话）；运行下拉框在标签栏右侧、不在标签页内容里。
  - 回归：Step1 / Step3 / Navigator / patch 相关模块与 HEAD 逐条对比，无新增失败。
  - 真机（用户）：Step3 的 patch 条可用、点哪去哪；Step1 的按钮条看起来、用起来与之前一样。
- **用户裁定（2026-09-27）**：
  1. **必须一行**。Step3 `Viewer` 标签页内容顶部一行：`[Patch ▾][P1]…[P7][Cell mask ▾][Nucleus mask ▾] 提示…… [Overlay][Fusion]`。**运行下拉框移到标签栏那一行的右侧**（`Viewer` 标签所在的一行，作为标签栏的角落控件，`QTabWidget.setCornerWidget(TopRightCorner)`），**不并入 `Viewer` 标签页的内容**。
  2. **patch 是全局组件，选中必须共用**：在 Step3 选 P3，Step1 也标 P3，并像 Step1 一样记进会话；两个按钮条（Step1 / Step3）始终标出同一个选中。
- **执行记录**（2026-09-27，未提交）：
  - `ui/patch_strip.py`（新）：`PatchStrip`——`Patch` 下拉按钮、至多 7 个内联按钮、菜单；常量与 `step1_patch_menu_width()` 从 `main_window.py` 原样移入（`main_window` 仍可导入这些名字）；`rebuild` / `rebuild_menu` / `set_selected` / `set_entry`、信号 `chosen`、Step3 用的 `holder()`；`buttons` 与 `menu_actions` 在整个生命期是同一个列表（只重填、不替换）。
  - `ui/main_window.py`：Step1 的按钮条改用组件，`_patch_menu_btn`、`_patch_menu`、`_patch_menu_actions`、`_patch_sel_btns`、`_patch_sel_container` 是组件内部对象的别名，`_rebuild_patch_buttons` / `_rebuild_patch_menu` / `_sync_patch_selection_marks` / `_set_patch_btn_state` 转交给组件；点击时才查 `_select_preview_patch`（与原来按钮上的 lambda 一样，实施中由现有测试发现后改回）；Step3 的第二个实例 `_step3_patch_strip`，`_rebuild_step3_patch_strip`（同名同色、无加载符号，随 `_all_patches` 重建）与 `_select_step3_patch`（Step3 viewer 的 `show_patch`、同一个选中、会话保存）；选中标记同时更新两个条；Step3 的一行按裁定 1 组装，运行下拉框作标签栏角落控件。
  - `ui/step3_page.py`：`assemble` 接收 `corner_widget`（`QTabWidget.setCornerWidget(TopRightCorner)`）。
  - 文档：`UI_SURFACE_RULES.md` Step3 一节（一行；patch 条是 Step1 的同一个组件、选中共用；运行下拉框在标签栏右侧；删除「no patch strip (a later block)」）；两份用户指南 Step3 一节（去掉「正在重建」，新增 patch 空降一条，运行下拉框的位置）。
  - 测试：`tests/test_patch_strip.py`（6 条：外观与改动前逐字相同的样式表 / 尺寸 / 间距 / 菜单宽度、7 个上限与菜单全列、点击与菜单都发 `chosen`、选中 / 名字 / 样式、无 patch 时禁用与提示、列表对象不被替换）；`tests/test_step3_patch_strip.py`（4 条：一行的组成与顺序、运行下拉框是标签栏角落控件且不在标签页内容里、与 Step1 同名同色并跟随列表变化、Step3 点击后 Step3 viewer 空降且与 Step1 viewer 对同一 patch 的空降位置相同、选中共用与会话保存、viewer 未打开时的选中）。**直接用到按钮条内部名字的 16 个现有测试文件一条未改、全部通过。**
  - 反向注入 8 处，各使至少 1 条变红：Step3 点击不更新 Step1 的标记、不保存会话、Step3 条不随列表重建、运行下拉框不在角落、Step3 点击移动了 Step1 的 viewer、内联上限改为 6、列表对象被替换、菜单按钮样式漂移。
  - 回归：40 个模块（上述 16 个 patch 相关文件、界面规则契约、Step3 各模块、Step1 viewer / 相机 / 暂停 / Navigator 权限 / 通道面板 / montage / GPU 接管相关模块），每模块单独进程，与 `git archive HEAD`（`1177185`）逐条对比：**38 个既有模块两边结果完全相同，无新增失败**（两边相同：`test_global_channel_dock.py`、`test_step1_channel_panel.py` 各 1 条字体，`test_tissue_navigator_viewport_sync.py::test_mapping_slide_local_to_full`，`test_step1_montage_view.py` 第 27 条后崩溃）；新增 2 个模块 10 条全部通过。
  - **真机验收通过（用户 2026-09-27）**。

### 块 B3 — 三个测试中发现的问题（申请 v1，**用户 2026-09-27 批准**，4 项裁定全部按建议；已实施，**真机验收通过**；插在 Step4 之前）
- **必要性**：用户 2026-09-27 真机测试中报告，要求在 Step4 之前修掉。
- **问题 1：Step1 Save 的进度条始终 0 %；`Save Fusion Settings` 出现在 Pre-segmentation 标签页下**
  - 调查：Save 生成 fused.zarr 的 `FullFusionWorker`（`ui/step0/overview_panel.py:242`）只按**区域**报进度：每处都是 `progress.emit(reg_i, len(regions), …)`，切块完成时只改文字、不改完成数（`:554`、`:601`）。「区域」是 ROI（Full WSI 就是 1 个区域），用户每次选的 `2 × 2` 是区域里的**切块网格**（4 块）：4 块逐一完成时进度一直是 0 / 1，直到整个区域写完才变 1 / 1，而进度框随即关闭——所以看到的始终是 0 %。`Save Fusion Settings` 放在 Step1 页面的底部栏（`main_window.py:1009`，块 A 的裁定：在 Channels 框正下方、跟随它的宽度），不属于左栏的某个标签页，所以切到 Pre-segmentation 时仍然显示。
  - 做法：进度改为按切块汇报：完成数 = 区域序号 × 1000 + 本区域已完成切块数 / 切块总数 × 1000，总数 = 区域数 × 1000，每块完成时发一次（文字不变）。`Save Fusion Settings` 只在左栏当前是 `Fusion` 标签时显示，切到 `Pre-segmentation` 时隐藏，**保留它的位置**（不让下面的内容跳动，见裁定 1）。
- **问题 2：Step3 看不到之前其他会话的分割结果；Step3 应是通用的分割结果查看器**
  - 调查：每次在 Step0 保存 Full WSI 都新建一个 ROI 工作区（`~/fusion_data/test1/rois/` 下现有 6 个，其中 2 个有运行）；Step3 只列**当前**工作区的运行（④a `list_runs(roi_dir)`），并且按当前 ROI 的名字与 bbox 解析 mask（`resolve_masks(run, roi_name, roi_bbox, …)`）。所以之前会话的结果看不到。
  - 做法：
    - 运行下拉框列出**当前项目所有 ROI 工作区**里的已完成运行（当前工作区的在前，其余按时间新到旧），每项写明来自哪个工作区；每个运行按它**自己的**区域解析 mask（多 ROI 的运行每个 ROI 一项），不再要求等于当前 ROI。
    - 下拉框旁加一个 `Load…` 按钮（在标签栏右侧角落，与下拉框一起）：选一个运行目录（或它的 `segmentation_meta.json`），可以来自别的项目；载入后加入列表并选中。
    - **只接受在当前打开的切片上做的结果**：运行 metadata 里的原始切片路径（`paths.raw_ome`，按真实路径比较）或所在工作区的 `source_ome` 与当前切片相同；否则不显示并写明原因（「这个结果是在另一张切片上做的」）。区域必须在切片范围内，金字塔仍按 ④a 的完整校验。
    - 选择规则：显式指定（Step2 完成对话框、`Load…`）→ 当前选择 → 当前工作区的 active → 当前工作区最新 → 其他最新。
  - 请用户裁定 2–4（见下）。
- **问题 3：从 Step2 回到 Step1，Pre-seg Results 的 patch 图像消失，只剩细胞 / 核的轮廓；点 Overlay / Fusion / Membrane / DAPI 后恢复**
  - 调查：离开 Step1 时 `_step1_whole_slide_step_changed` 调 `_close_montage_supply()`（`main_window.py:2096`）：关掉 montage 的供给与它的 GPU 层（块 D 的设计：离开 Step1 释放缓存与线程）；轮廓是 montage 视图上单独的一层，不随之清除。回到 Step1 时**没有任何地方重新请求 montage 的图像**——`_request_montage_images` 只在切换右栏标签、缩放层级、patch 变化、模式 / 图层按钮时调用（`:1306-1310`、`:5409`、`:5607`）。所以回来后只剩轮廓，点任一按钮就触发重新请求。与方法（StarDist + expansion）无关。
  - 做法：进入 Step1 时（Step1 页面已在屏幕上之后，下一轮事件循环），如果右栏当前是 `Pre-seg Results`，调一次 `_request_montage_images()`——它会重建供给与 GPU 层并按当前的模式 / 图层画出来。不改 montage 的供给、GPU 层或释放规则。
- **白名单**：`ui/step0/overview_panel.py`（只改 `FullFusionWorker` 的进度数值）；`ui/main_window.py`（`Save Fusion Settings` 随左栏标签显示 / 隐藏；进入 Step1 时补请求 montage；Step3 的运行列表、`Load…` 与接线）；`core/step3_masks.py`（跨工作区列运行、按运行自己的区域解析、切片一致性检查、`Load…` 的目录解析）；`ui/step3_mask_bar.py`（`Load…` 按钮）；`ui/step3_page.py`（角落控件放两个）；`UI_SURFACE_RULES.md`（Step1：`Save Fusion Settings` 只在 `Fusion` 标签下；Step3：列表范围与 `Load…`）；`docs/user_guide.md`、`docs/用户指南.md`；测试：新测试 `tests/test_b3_fixes.py`（或分放进各自的新测试文件），以及 `tests/test_step3_masks.py`、`tests/test_step3_mask_wiring.py`、`tests/test_step3_mask_bar.py` 中**因列表范围与解析规则改变而须改的用例**（实施前列出报批）；本文档。
- **不改的范围**：fusion 的计算与写盘；montage 的供给、GPU 层与释放；Step3 的 GPU 渲染与标签绑定；Step2；打开别的项目 / 别的切片（S2 冻结）。
- **风险**：
  - 问题 2 改变了 ④a 的选择与解析规则（当前 ROI → 运行自己的区域）：已有的 ④a / ④c 测试须相应改写（报批）；
  - 别的工作区的结果区域若不在当前 ROI 内，Step3 的画面按 Step1 的规则只显示当前 ROI（ROI 外是空的），mask 在那里也看不到（见裁定 4）；
  - 回退：各自恢复。
- **验收门**：
  - 问题 1：合成数据的 fusion（1 个 ROI × `2 × 2` 切块、2 个 ROI × `2 × 2` 切块）：进度按切块单调上升、最后到 100 %；`Save Fusion Settings` 在 `Fusion` 标签下可见、在 `Pre-segmentation` 下隐藏且位置不变，切回来再出现。
  - 问题 2：合成项目（两个工作区，同一切片；另一个项目；另一张切片的结果）：列表包含两个工作区的运行、写明来源、当前工作区在前；按运行自己的区域解析；`Load…` 载入别的项目里同一切片的运行并选中；另一张切片的结果被拒绝并写明原因；选择规则各情形。
  - 问题 3：进入 Step2 再回 Step1，Pre-seg Results 的图像重新请求并画出（供给与 GPU / CPU 画面重新建立），无需点任何按钮。
  - 回归：Step1 / Step3 / montage / fusion 相关模块与 HEAD 逐条对比，无新增失败。
  - 真机（用户）：三个问题各自复现步骤下不再出现。
- **请用户裁定**：
  1. 切到 `Pre-segmentation` 时 `Save Fusion Settings` **隐藏但保留位置**（建议：下面的内容不跳动），还是隐藏并把高度让给上面的内容？
  2. 运行列表的范围：(a) **自动列出当前项目所有 ROI 工作区的运行 + `Load…` 载入别处的**（建议）；(b) 只列当前工作区，其余都用 `Load…`。
  3. 只接受**在当前切片上做的**结果（按原始切片的真实路径判断），别的切片的结果拒绝并写明原因——确认？（要看别的切片，须先在 Step0 打开那张切片。）
  4. 结果的区域不在当前 ROI 内时（例如在另一个 ROI 上做的）：(a) 照样列出，提示「这个结果有一部分在当前 ROI 之外，那里不显示」（建议）；(b) 不列出。
- **用户裁定（2026-09-27）**：全部同意（1 隐藏并保留位置；2 自动列出项目所有工作区 + `Load…`；3 只接受当前切片的结果；4 照样列出并提示）。
- **执行记录**（2026-09-27，未提交）：
  - 问题 1：`ui/step0/overview_panel.py` `FullFusionWorker.run` 的进度改为每个区域 1000 个单位、按切块均分（开始、每块开始 / 完成、多边形掩膜、区域完成各一处），文字不变；`main_window`：`Save Fusion Settings` 的尺寸策略设 `retainSizeWhenHidden`，左栏 `currentChanged` → `_follow_step1_left_tab`（只在 `Fusion` 下显示）。
  - 问题 2：`core/step3_masks.py` 新增 `Run.workspace` / `workspace_label`、`Entry`（运行 + 它的一个区域，键 = 运行目录 + 区域名）、`workspace_slide` / `run_slide` / `same_slide`、`list_project_runs`（当前工作区在前，其他工作区只收同一切片的）、`load_run`（任意运行目录或它的 meta 文件）、`run_regions`（运行自己的区域）、`entries`、`choose_entry`、`inside`；④a 原有函数不变。`main_window._step3_refresh_masks` 改用这些（按运行自己的区域解析；区域不在当前 ROI 内时提示）；`_step3_load_run` 与可替换的 `_step3_pick_run_folder`（产品用系统目录对话框）；另一张切片 / 不是运行目录 → 提示 `Not loaded: …`，下次刷新清除；换数据集时清空已载入的运行。`ui/step3_mask_bar.py`：`Load…` 按钮、`load_requested`、角落控件 `corner()`（下拉框 + `Load…`）、`current_run_dir()` 从键里取目录。
  - 问题 3：`main_window._step1_whole_slide_step_changed`：进入 Step1 后下一轮事件循环调一次 `_request_montage_images()`（montage 不在屏幕上时它自己拒绝）。
  - 文档：`UI_SURFACE_RULES.md`（Step1：`Save Fusion Settings` 只属于 `Fusion` 标签；Step3：通用结果查看器、列表范围、`Load…`、按运行自己的区域）；两份用户指南 Step3 一节的运行下拉框。
  - 测试：新测试 `tests/test_b3_fixes.py`（7 条：1 个 ROI × 2 × 2 与 2 个 ROI × 2 × 2 的进度按切块上升到 100 %、`Save Fusion Settings` 随标签隐藏 / 显示且保留位置、项目所有工作区的列表与排序（另一切片的工作区不列）、Step3 列出之前会话的结果、`Load…` 另一项目的同一切片结果并选中、另一切片的结果与非运行目录被拒绝并提示、区域在当前 ROI 之外的提示、回到 Step1 重新请求 montage 图像）。**用户授权改的 2 条**：`test_step3_mask_wiring.py` 的「别的 ROI 的运行」改为按它自己的区域显示；`test_step3_patch_strip.py` 的角落控件断言改为「包含下拉框与 `Load…`」。
  - 反向注入 9 处，各使至少 1 条变红：进度仍按区域、回到 Step1 不重新请求、按钮始终显示、隐藏时不保留位置、只列当前工作区、列出别的切片、`Load…` 不查切片、不提示区域在 ROI 外、拒绝提示不清除。
  - 回归：46 个模块（Step1 保存 / fusion 发布 / fusion 隔离 / 预分割界面、patch 相关、Step3 各模块、Step1 viewer / 相机 / Navigator / 通道面板 / montage / GPU 接管、界面规则契约等），每模块单独进程，与 `git archive HEAD`（`343117e`）逐条对比：**45 个既有模块两边结果完全相同，无新增失败**（两边相同：两条字体、`test_mapping_slide_local_to_full`、montage 第 27 条后崩溃；授权改写的 2 条在各自一侧都通过）；新增 `test_b3_fixes` 7 条通过。
  - **真机验收通过（用户 2026-09-27）**。

### Step4 改造 — 调查结论与用户裁定（2026-09-27）
- **现状（只读调查）**：`workers/feature_extract_worker.py`（406 行）整区域一次进内存：mask 读 `global_mask.dat`（没有时读 float32 的 OME-TIFF 再整张转 uint32，编号超过 2^24 失真）；形态用整区域的坐标数组 `ys` / `xs`、`ones` 与 `xs*xs` 等临时数组，周长用整区域的 `binary_erosion`；每个通道 `read_region` 读整区域为 float32，再用 `scipy.ndimage` 按统计项各扫一遍（mean / sum / std / min / max 各一遍，median 排序，p90 逐细胞 Python）。**界面默认只勾 Mean**（`ui/step4_page.py:111-119`）。**Step4 现场重做背景校正**：loader 只带 `correction_config`，对 tophat / cuCIM 通道每次都对整区域重跑校正（`core/io_loader.py:236-257`），不读 Step0 已持久化的 `corrected_channels.zarr`。**环境里没有 anndata**，所以本机的 Step4 从未写出 h5ad（worker 跳过）；没有 pyarrow；有 numba 0.67。
- **按代码估算（未实测）**：本项目（约 2.5 亿像素、29 通道、约 4.4 万细胞）峰值内存约 5–6 GB，随像素数线性增长（5.9 万 × 3.5 万的 WSI 约 40–50 GB）；流式之后约 0.2–1 GB，随细胞数增长。~~默认流程里最大的时间开销很可能是现场校正~~——**更正（2026-09-27 核实）**：`~/fusion_data/test1` 的 6 个工作区里，28 个通道的校正决定**全是 `original`**，这个项目上 Step4 根本不做现场校正；现场校正的开销只能在有 tophat / cuCIM 通道的数据上测（见 S4-0 v2）。
- **独立意见（ChatGPT，用户转述，采纳）**：Step4 定位为「一次扫描、多区域（全细胞 / 核 / 胞质）的特征定量引擎」：定量来源 = 原始强度或 Step0 持久化的校正后强度，**绝不用**显示映射 / fusion / 模型归一化后的值，每个通道记下来源与校正的来龙去脉；按空间分块、一次处理若干通道（`channel_batch_size`），细胞 / 核标签每块只读一次；融合累加器一遍得到 count / 和 / 平方和 / min / max（mean、std 由它们算出）；胞质 = 全细胞 − 核（面积与强度和都如此），核数目本身是特征；分布统计（median / p90 / Gini）默认关闭、可只选部分 marker；线程数由实测定；h5ad 结构：`X` = 全细胞 mean，其余统计与核 / 胞质在 `layers`，形态在 `obs`；膜环等新区域先在数据模型里留位置。
- **用户裁定（2026-09-27）**：
  1. 快速统计（mean / sum / std / min / max）默认都算，**但由用户选择**。
  2. 有核时也输出核（与胞质）的特征，**同样由用户选择**。
  3. **允许安装 anndata**；默认输出 h5ad，采用上面的 h5ad 结构；**CSV 改为可选**，勾选时才输出。
  4. 验收：新旧逐细胞一致（mean / sum / std / min / max、面积、质心、长短轴、偏心率，按现有定义；周长改为新定义，见 S4-0 裁定 5）；改读 Step0 校正结果的通道，另与「Step0 结果上的旧算法」对照。
  5. **输出范围问卷**（用户设计）：特征按几个「范围」组织——计算方法（mean、sum、std、min、max，以及可选的分布统计）、区域（全细胞 / 核 / 胞质）、特征类别（表达量、形态……）；每个范围是一个**默认展开、可折叠**的分组，组内逐项勾选；Step4 只计算勾选的组合，不做全量计算。
- **分块**（每块单独申请、验收）：S4-0 基线实测（不改产品代码）→ S4-1 定量来源（读 Step0 的校正结果）+ 流式融合内核（全细胞形态 + 快速统计），输出与现在一致以便对照 → S4-2 核 / 胞质、输出范围问卷的界面、h5ad 新结构（装 anndata）、CSV 可选 → S4-3 分布统计（按 marker 选择）、Parquet 等。
- **独立审核（2026-09-27）已核实、须写进 S4-1 / S4-2 申请的契约**：
  1. **严格的定量来源（QuantSourceResolver）**：现在的 `OMETIFFLoader.read_region`（`core/io_loader.py:88-117`）在校正结果读不到时**静默退回原始数据并现场校正**——Step4 不能沿用。Step0 决定为 `original` 的通道读原始数据；决定为 tophat / cuCIM 的通道**必须**读 Step0 持久化的校正结果，并核对形状 / ROI / dtype / 来源标识 / 校正方法（复用已有的 `utils/calibration_source.open_corrected_channel_array` 与产品属性）；不存在或不符 → **直接报错**，不退回原始数据、不现场校正。
  2. **两份对照**：旧 Step4 的实际表现（原始 + 现场校正 + 旧算法）与「来源等价的正确值」（Step0 的校正结果 + 旧算法）分开；新 Step4 与后者对照，这样结果不同时能分清是来源的问题还是新算法的问题。S4-1 内部按「来源」「内核」两步分开验收（可不增加用户可见的块）。
  3. **胞质的 min / max 不能相减**：count / 和 / 平方和（因而 mean、std、面积）= 全细胞 − 核；min / max 在同一次扫描里对「属于细胞且不在核内」的像素直接累加。
  4. **h5ad 的 `X` 要固定**：见裁定（请用户确认）。
  5. **细胞层面的核汇总 ≠ 每个核的特征**：细胞表（一行一个细胞：核数、核面积合计 / 平均 / 最大、核占比、核与胞质的表达量）是主输出；每个核一行的核表（核编号、所属细胞、面积、质心、形状、marker 均值……）现在就在数据模型里分开，交付时间见裁定。
  6. **周长**：现在的周长是对**所有细胞的并集**做腐蚀（`mask > 0`），紧贴的两个细胞之间的边界不计入——不是标准的逐细胞周长。旧定义只作兼容对照；新的形态注册表另给按标签判断的周长（中心标签 ≠ 邻居标签，分块带 1 像素重叠）。
  7. **快速特征注册表**写清楚（都来自同一次扫描的矩与计数，收尾时算出）：形态——面积、质心、长短轴、偏心率、方向、外接框、等效直径、长宽比、extent、圆度（4πA / P²）；核派生——核数、核面积合计 / 平均 / 最大、核占比、胞质面积；表达量——全细胞 / 核 / 胞质 × mean / sum / std / min / max；扩展（S4-3）——median、p90、p95、Gini。
  8. 分块边长、每批通道数、线程数都只是基准测试的初值，由实测选定，不写成契约；分布统计只选部分 marker 时不放成整张 layer（S4-3 再定存法）。

### 块 S4-0 — Step4 基线实测（申请 v2，按独立审核修订，**用户 2026-09-27 批准**，已完成）
- **必要性**：S4-1 起每块的加速与省资源都要有实测依据；并为新算法准备逐细胞对照的参考。
- **数据**：`~/fusion_data/test1` 的通道**全部不校正**（见上面的更正），所以分两部分：
  - **A. 旧 Step4 的实际表现**（test1，一次整区域运行，约 2.5 亿像素、28 个通道）：原始数据 + 旧算法。
  - **B. 校正通道**：需要一份有 tophat / cuCIM 通道、并已由 Step0 写出 `corrected_channels.zarr` 的数据（见裁定 1）。在它上面各跑：旧 Step4（现场校正 + 旧算法）= 对照 A 的校正部分；「Step0 的校正结果 + 旧算法」= 来源等价的正确值（对照 B，S4-1 的验收参考）。至少覆盖一个不校正、一个 tophat、一个 cuCIM 通道（若有）。
- **做法**：新增脚本 `scripts/benchmark_step4_baseline.py`（不被产品导入），参数 `--project`、`--run`、`--output-dir`（默认 `~/fusionflux/bench_step4/<项目名>/<日期>_<git 短哈希>/`，仓库外、不进 git，不写 `~/fusion_data`）：
  - 只读地调用现在的 `FeatureExtractWorker`；对照 B 用脚本把 worker 里的 loader 换成已接上 Step0 校正结果的 loader（测试手段，不改产品代码）。
  - **分项计时不重复计**：包装 `_read_roi_zarr`（原始读图）、`_apply_configured_correction`（现场校正）、校正结果的读取、`scipy.ndimage` 各统计、形态计算与输出写出；报告按「原始读图 / 校正 / 统计 / 形态 / 写出」列出。
  - **峰值内存双记录**：每 0.2 s 采样进程 RSS，外加 `resource.getrusage(...).ru_maxrss`；有 cuCIM 通道时另记峰值显存。
  - 配置：① 界面默认（只 mean）；② 快速统计全选；③ median + p90 只测 2 个通道——报告里写实测值，28 通道的数字**明确标为粗略推算**，不作验收数字。
  - 保存 ① ② 与对照 B 的输出（CSV）作为 S4-1 的逐细胞参考。
  - **来源记录**写进报告：git 提交、数据路径与标识、分割运行编号、mask 路径、Step0 manifest、校正结果的标识、统计项、本机 CPU / 内存 / GPU、存储设备与路径。
  - 结果写进 `docs/benchmarks/step4/baseline_2026-09-27.md` 与本文档。
- **白名单**：`scripts/benchmark_step4_baseline.py`（新）；`docs/benchmarks/step4/`（新）；本文档。**不改任何产品代码、不装包、不写 `~/fusion_data`。**
- **风险**：一次可能几十分钟（28 通道 × 整区域）；峰值内存可能 5–6 GB（本机 10 GB）——后台运行，期间不跑别的重任务。
- **验收门**：A、B 两部分的分项时间与两种峰值内存齐全；三种配置有数字（③ 的推算标明）；参考输出已保存；来源记录齐全；报告写明是本机（WSL2、10 GB）的数字。
- **用户裁定（2026-09-27）**：
  1. 校正通道的数据：**用户重新做一次带校正的流程**（Step0 里设 tophat / cuCIM 并保存），B 部分用它。
  2. 输出目录按参数化的默认值（`~/fusionflux/bench_step4/<项目>/<日期>_<哈希>/`）。
  3. **h5ad 的主区域必选，且至少选一个统计量**（不强制 mean：用户可能只要 std 或 sum）。落实：`X` = 主区域中按 mean → sum → std → min → max 的顺序取用户所选的第一个统计量，写进 `uns["X_statistic"]` 与 `uns["primary_compartment"]`，下游据此知道 `X` 的含义。
  4. **每个核一行的核表在 S4-2 一起交付。**
  5. **周长只保留新定义**（按标签判断、分块带 1 像素重叠）；旧的并集腐蚀周长不再输出——验收里的逐细胞对照因此不含周长，新周长另用独立的参考实现核对。
- **执行记录 A 部分**（2026-09-27）：脚本 `scripts/benchmark_step4_baseline.py`；报告 `docs/benchmarks/step4/baseline_2026-09-27.md`。test1（2.5 亿像素、29 通道、40 843 细胞，通道全部不校正）：① 只 mean **215.5 s、峰值 8.63 GB**；② 快速统计全选 **1034 s（17.2 min）、峰值 8.66 GB**，其中通道统计 917 s（min / max 每通道约 10 s、std 约 6.6 s、mean / sum 约 2.5 s）、形态约 1 min、读通道约 1 s / 通道；③ median 约 15 s / 通道、p90 约 3 s / 通道（2 个通道实测，29 通道约 7 min + 1.4 min 为推算）。参考输出保存在 `~/fusionflux/bench_step4/test1/2026-09-27_bdbd29e/`。
- **执行记录 B 部分**（2026-09-27）：用户新做的带校正工作区 `full_wsi_20260927_121444_6bad`（2 个 tophat 通道 CD3D、HsBAg；本机 cuCIM 不能用 GPU，没有 cuCIM 通道），StarDist + expansion 运行，56 874 细胞。只 mean：旧 Step4 **654 s**，其中**现场 tophat 481 s（每通道约 4 min）**；快速统计全选：旧 1503 s vs **对照 B（读 Step0 校正结果）1014 s**，读校正结果 2 个通道共 7.5 s（约为现场校正的 1/60）；峰值 8.4–8.6 GB。来源核对通过（原始读 27 次、校正结果 2 次）。**两份全选输出全部 153 列在 1e-5 内相同**——改读 Step0 结果不改变数值。**补记（2026-09-27，S4-1 调查）**：「旧：原始 + 现场 tophat」是基准脚本**显式把** `step0/correction_config.json` 交给 worker 测出的；产品路径上 Step4 去输出目录（`step2/`）找这个文件、找不到，校正通道被**静默按原始数据定量**——旧产品不只是慢，还有来源错误（S4-1 修）。参考输出 `~/fusionflux/bench_step4/test1_tophat/2026-09-27_bdbd29e/`。**S4-0 完成（自动验收：分项时间、两种峰值内存、三种配置、参考输出、来源记录齐全）。**

### 块 S4-1P — CPU / GPU 内核探测（申请 v1，**用户 2026-09-27 批准**；已完成，**自动验收通过**；插在 S4-1 之前，不改产品代码）
- **来由**：用户与 ChatGPT 的讨论（`~/nextstep.txt`，用户转述）有三点建议：Step4 一开始就做成 CPU / GPU 双后端，二者执行同一份累加器契约；CPU 后端必须认真优化，不能「没有 GPU 就回到 17 分钟」；正式实现之前先用一个小探测在真实数据块上比较 SciPy、Numba 融合内核与 Rust / Rayon 融合内核，由实测决定 CPU 后端用哪个。**采纳**，另按本机核实的事实补充与修正如下。
- **本机核实（2026-09-27，只读）**：
  1. GPU：RTX 3060 Laptop，6 GB，驱动 581.95。环境里有 CuPy 13.3（`cupy-cuda12x`）、torch 2.5.1+cu121（CUDA 可用），numba 0.67 的 `numba.cuda` 也报告可用。
  2. **CuPy 的自定义内核（RawKernel）默认编译不了**：报 `libnvrtc.so.12: cannot open shared object file`。这个库在 pip 包 `nvidia-cuda-nvrtc-cu12` 里，不在动态加载器的搜索路径上。把它所在的目录加进 `LD_LIBRARY_PATH` 之后，RawKernel 可以编译运行（用 atomicAdd 做的计数和求和结果正确）。产品若用 CuPy 内核，必须明确解决 nvrtc 的加载问题，不能依赖用户的环境变量。**推测**：本机「cuCIM 不能用 GPU」可能也是这个原因（cuCIM 建在 CuPy 上）。这只是推测，本块不查，列入 advisory。
  3. Rust：本机**没有**工具链（没有 `cargo`、`rustc`、`maturin`）。
  4. CPU：WSL 里有 16 个逻辑核（i7-12700H 有 6 个 P 核、8 个 E 核；WSL 分给 16 个）。
- **对讨论的几处修正与补充**：
  1. **I/O 下限要在「按块读」下重新测**。S4-0 的 42 s 是整区域一次读的时间；流式按块读的模式不同，还要加上 mask zarr 的解压。如果融合内核的计算时间已经小于按块读的时间，那么 Rust 或 GPU 的收益主要看 I/O 与计算能否重叠，而不在内核本身。所以探测**同时测按块读的速度**（原始 OME-TIFF、Step0 校正结果 zarr、细胞 mask zarr），作为判断的基准线。
  2. **累加精度**：CPU 上 sum / sumsq 与坐标矩用 float64（在 CPU 上代价很小）。std 用 `sumsq/n − mean²` 计算有抵消误差，探测里与逐细胞的 float64 双遍参考对照。GPU 版先用「块内 shared memory 局部归约 + float64 atomicAdd」（sm_86 支持 double 的 atomicAdd），与纯 float32 atomic 各测一次，看误差和速度的代价。不要求逐位相同：count / min / max 必须完全相同，sum / mean / std 的阈值按实测定。
  3. **Rust 的探测接法**：不用 PyO3 / maturin，也不往产品环境里装包。Rust 写成导出 C ABI 的 `cdylib`，Python 用 `ctypes` 传 numpy 指针调用；Rayon 每个线程一份私有累加器，最后归约。产品是否改用 PyO3 / maturin，以及 Windows 上怎么构建，都等决定用 Rust 之后，在 S4-1 里另定。
  4. **Numba 并行**也按「每线程一份私有累加器 + 归约」来写（`prange` 按行带并行），与 Rust 结构相同，这样比较才公平。
  5. **GPU 一起测**：做一个 CuPy RawKernel 的融合版，测「H2D 传输 + 内核」的时间和每块峰值显存。它回答 S4-1 是否现在就做 CUDA 后端；无论结果如何，后端接口（`accumulate(tile) / merge / finalize`）都按双后端设计。
- **做法**：新增 `scripts/probe_step4_kernels.py`（不被产品导入）和 `scripts/probe_step4_rust/`（Cargo 工程，`src/lib.rs`；构建产物放在 `target/`，不进 git）。数据用 B 部分的工作区 `full_wsi_20260927_121444_6bad`：StarDist + expansion 运行的细胞 mask zarr（56 874 个细胞），4 个 marker（2 个不校正，加上 2 个 tophat 通道 CD3D、HsBAg，读 Step0 的校正结果），只读。
  - 块边长 2048² / 4096² / 8192²，各取有细胞的真实位置；通道 1 个或一批 4 个；线程数 1 / 8 / 16。
  - 同一份计算：count、sum、sumsq、min、max（每细胞 × 每通道），加上几何的 count、Σx、Σy、Σx²、Σy²、Σxy 与外接框；收尾算 mean / std。
  - 参赛者：
    - A. 今天的 SciPy 调用，与旧 Step4 相同的 5 项统计与形态；
    - B. Numba 融合内核；
    - C. Rust / Rayon 融合内核；
    - D. CuPy 融合内核（分两次记：只算内核的时间，以及含传输的时间）。
  - 正确性：每个块上 B / C / D 都与 float64 双遍参考逐细胞对照。
  - 另测按块读的时间：同样的块，读 4 个通道与 mask，分成原始 OME-TIFF、校正结果 zarr、mask zarr 三项分别计时。
  - 最后用胜出的 CPU 内核在整个区域上按块循环跑一次（29 通道，只做统计、不写产品输出），与 S4-0 的对照 B 参考（`fast_corrected/cell_features.csv`）比较 mean / sum / std / min / max 与面积、质心。
  - 记录指标：时间、每秒像素数、有效 GB/s、峰值 RSS / 显存。
  - 结果写进 `docs/benchmarks/step4/kernel_probe_2026-09-27.md` 与本文档；原始数据写到 `~/fusionflux/bench_step4/test1_tophat/<日期>_<哈希>_probe/`。
- **决定规则**（建议，请用户裁定）：
  - CPU 后端：
    - Rust 比 Numba 快 **2 倍以上**：S4-1 的 CPU 内核用 Rust；
    - 快 **不到 1.3 倍**：用 Numba；
    - 在两者之间：看整区域的计算时间是否已经小于按块读的时间。已经小于（接近 I/O 受限）就用 Numba，否则用 Rust。
  - GPU 后端：
    - 最快的 CPU 内核在整区域上的计算时间**超过按块读时间的 1.5 倍**：S4-1 同时交付 CUDA 后端，Auto 模式是「有 CUDA 且显存够用 GPU，否则用 CPU」；
    - 否则 S4-1 只交付 CPU 后端，CUDA 后端的接口位置留好，等出现计算重的特征（S4-3 的分布统计等）或 WSI 实测需要时再做。
- **白名单**：`scripts/probe_step4_kernels.py`（新）；`scripts/probe_step4_rust/`（新）；`.gitignore` 加上 `scripts/probe_step4_rust/target/`；`docs/benchmarks/step4/kernel_probe_2026-09-27.md`（新）；本文档。**不改产品代码，不往 `fusion_mesmer` 环境装包，不写 `~/fusion_data`。**
- **须授权的环境改动**：用 rustup 把 Rust 工具链装到用户目录（`~/.rustup`、`~/.cargo`，约 1 GB 以内）；从 crates.io 取 `rayon`。只对当前用户生效，删掉这两个目录即可完全撤销。
- **风险**：8192² × 4 通道的块约 1.3 GB，加上参考计算，峰值约 3–4 GB。探测期间不跑其他重任务。整区域那一遍约几分钟。
- **验收门**：
  - 三种块边长 × 四个参赛者的时间与内存都有数字；
  - B / C / D 的正确性对照通过（count / min / max 完全相同，sum / mean / std 的误差如实报告）；
  - 按块读的三项时间齐全；
  - 整区域那一遍与 S4-0 参考一致；
  - 按决定规则写出结论。
- **用户裁定（2026-09-27）**：
  1. 授权用 rustup 把工具链装到用户目录，从 crates.io 取 `rayon`。
  2. GPU（CuPy 内核）纳入探测。
  3. 决定规则**暂不定**：先拿到结果，再讨论。
  4. 申请提交推送后开始执行。
- **执行记录**（2026-09-27）：报告 `docs/benchmarks/step4/kernel_probe_2026-09-27.md`，原始数据 `~/fusionflux/bench_step4/test1_tophat/2026-09-27_e284d62_probe/`。Rust 1.98.1 装在用户目录。
  - **块**（8192² × 4 通道）：scipy 约 38 s；Numba 16 线程 0.058 s，Rust 16 线程 0.051 s（单线程也只快 1.1 倍）；GPU 内核 8 ms，但拷到显卡要 182 ms；读这一块约 1.2 s。
  - **整个区域**（29 通道，4096² 分块，每批 4 个通道）：**12–13 s**（S4-0 对照 B 是 1014 s），峰值 1.2–1.9 GB（原来 8.6 GB）。其中读通道 10–12 s，计算：Rust 0.75 s、Numba 1.1 s、GPU 4.8 s（传输占 4.5 s）；串行读时总时间 25 s。
  - 探测中另加了一个变体 numba_ch：按通道并行、全局只有一套累加器，只要 43 MB（按行并行、每线程一套的要 692 MB），速度相同。
  - **正确性**：块上 count / 外接框 / min / max 完全相同，std ≤ 2e-13。cupy32（float32 原子加）的 std 误差 1e-4 到 7e-4。整个区域与 S4-0 参考逐细胞对照：面积完全相同，5 项统计 × 29 通道没有一个超过 1e-5。
  - **发现**：
    1. **已经受 I/O 限制**，Rust 与 GPU 在总时间上都没有可见收益。
    2. 旧的长短轴、偏心率有 float32 坐标误差（emulation 与 CSV 一致到 5e-5）；新值与 skimage 一致到 3e-8。新旧之差：短轴最大 3.4 px，偏心率最大 2.6。
    3. 每线程一套累加器的内存随细胞数增长，WSI 上不可接受。
    4. mask 里有 2 个没有像素的细胞编号，空细胞的输出约定待定。
  - 决定规则按用户裁定 3，结果出来后讨论。
- **用户裁定（2026-09-27，同意 ChatGPT 对结果的审核意见）**：
  1. **胜出的是算法结构，而不是语言**：同一种结构下，Rust 的计算比 Numba 快约 1.5 倍；但按通道并行的 Numba（numba_ch）总时间 11.6–12.0 s，累加器只有 43 MB，已经不输每线程一套累加器的 Rust（346–692 MB）。
  2. **S4-1 的产品用 Numba，按通道并行、全局一套累加器**；后端接口 `QuantBackend` 定下来（NumbaBackend 是产品；RustBackend、CUDABackend 留位置）。**做好接 Rust 的准备，但不先用 Rust。**
  3. **Rust 探测保留**（`scripts/probe_step4_rust/`），作为将来原生定量后端的原型。将来正式采用时，要改成按通道并行（Rayon 按通道分任务，几何单独做一次并行归约），不把现在「每线程一套累加器」的原型原样搬进产品。切换 Rust 的触发条件（任一即可）：
     - 特征复杂度明显增加（边界图、膜环、纹理、邻域等），Numba 代码开始难以维护；
     - 开始 Rust 化 viewer / IO（与 §6 的「Rust 重构 viewer / Step2 的基础」同一阶段）；
     - 真实 WSI 的剖析显示计算重新成为瓶颈。
  4. **GPU 后端不在 S4-1 交付**：现在它反而更慢（拷到显卡 4.5 s，CPU 计算约 1 s），而且必须用 float64 累加。接口留好，等计算重的特征出现时再评估。
  5. **S4-1 的主战场是 I/O**：
     - 读、解压与定量流水线重叠；
     - 查清「读 8.8 GB 为什么要 10 秒」；
     - 以后再看冷读的 SSD 与更大的 WSI。
  6. **原始 uint8 不转 float32**（S4-1 契约）：内核支持混合输入类型（27 个 uint8 原始通道 + 2 个 float32 校正通道），累加器仍用 float64。
  7. **形态的验收改为对独立参考**：面积、质心、表达量与旧值（S4-0 参考）对照；长短轴、偏心率、方向与独立的 float64 参考（skimage `regionprops`）对照。这是修正科学错误，不算回归。输出的元数据写 `morphology_version = 2`。
  8. **空标签**（编号存在但 mask 里没有像素）：有效细胞 = `count > 0`；h5ad **不输出**空标签。来源记录写 `max_label_id`、`n_valid_cells`、`n_empty_labels`，数量少时另写 `empty_label_ids`。不再为兼容旧 CSV 保留 mean = NaN、min / max = 0 的假细胞。

### 块 S4-1 — 严格定量来源 + 流式融合内核（全细胞形态 + 快速统计）（申请 v2，按独立审核修订，**用户 2026-09-27 批准**；已实施，**真机验收通过**）
- **必要性**：
  - S4-0 / S4-1P 已证明：今天的 Step4 在 2.5 亿像素上峰值 8.6 GB、快速统计全选 17–25 min；同样的结果流式融合只要 12–13 s、峰值 1.2–1.9 GB。WSI（约 20 亿像素）上旧 Step4 按像素线性增长到约 70 GB，根本跑不了。
  - **新发现（本次只读调查，产品缺陷）**：今天的产品路径里，Step4 页面去**输出目录**（默认 = `step2_dir`）找 `correction_config.json`（`ui/step4_page.py:274-276`），而这个文件在 `step0/`。找不到时 `_load_correction_config` 返回 `None`，worker 的 loader 也没有接上 `corrected_channels.zarr`——**tophat / cuCIM 通道被静默地按原始数据定量，既不读 Step0 的结果，也不现场校正**。S4-0 B 部分的「旧：原始 + 现场 tophat」是基准脚本手动把配置交给 worker 得到的，不是产品路径的实际行为。本块的严格来源同时修掉这个缺陷。
- **只读调查结论**（HEAD `5d49bac`）：
  1. **旧 worker**（`workers/feature_extract_worker.py`，406 行）：mask 读 `.dat` 或 float32 的 OME-TIFF 整张转 uint32（`:67-85`）；ROI 偏移靠三处猜 bbox（`:125-171`）；形态用整区域的 float32 坐标数组（`:191-232`，即 S4-1P 查出的长短轴 / 偏心率误差）；周长是并集腐蚀（`:234-241`）；每通道 `read_region` 整区域转 float32 后 `ndimage` 逐项扫（`:249-297`）；CSV 用 `%.6g`（`:336`）；h5ad 只在装了 anndata 时写（本机没装，从未写过）；空标签照样输出（mean = NaN、min / max = 0）。
  2. **交接**：`_go_to_step4`（`ui/main_window.py:4840-4867`）只找 `global_mask.dat` / `global_mask.ome.tiff`，不知道 Step3 选的是哪个运行、哪个 ROI；多 ROI 的运行只能拿到第一个 ROI 的别名。`_step4._out_edit` 在 4 处被设为 `step2_dir`（`:2951`、`:3604`、`:4247`、`:4604`），所以所有运行的输出都写进同一个 `step2/cell_features.csv`，互相覆盖。
  3. **Step3 已有可复用的运行解析**：`core/step3_masks.py` 的 `load_run` / `list_project_runs` / `run_regions` / `Entry(run, roi_name, bbox)`，Step3 当前选择存在 `main_window._step3_mask_key`。**LabelStore**（块 N2）在运行 meta 的 `label_store[<ROI>]` 里给出细胞 zarr 的路径、形状、分块、`n_objects`；ROI 的全图坐标在 `rois[i].bbox_fullres`。
  4. **旧运行没有 `label_store`**：test1 的 6 个工作区里，`074333_6ea6` 的两个运行（S4-0 A 部分用的 `seg_20260927_074644_cellpose_wholecell_fusion` 在其中）没有；其余 4 个有。
  5. **Step0 的来源记录**：工作区 `step0/step0_roi_result.json`（交接 manifest）给出 `raw_ome_path`、`correction_config_path`、`corrected_zarr_path`、`corrected_decisions`；`correction_config.json` 有 `channel_decisions`（28 个通道；DAPI 作核通道不在其中）与每通道参数；`corrected_channels.zarr` 根属性里另存一份 `correction_config` 与 `source_ome`、`mode: roi_only`，每个 ROI 一个组（组名是 ROI 名的空格换成 `_`，属性 `roi_name`、`bbox_fullres`），每个数组的属性有 `correction_method`、`correction_param_name / value`、`channel_index`、`source_shape`、`roi_bbox_fullres`、`bg_correction_algo_version`、`source_identity`。
  6. `utils/calibration_source.open_corrected_channel_array` **不能直接复用**：它在指定的 ROI 组没有该通道时会**退到任意一个含同名通道的组**（`:84-92`），可能读到别的 ROI 的数组。本块写严格的查找（只认本 ROI 的组），可复用它的「身份以打开的数组为准」的做法。
  7. **「读 8.8 GB 要约 10 s」的原因**（本次只读实测，热缓存，切片 29 通道 uint8、512² LZW、无 predictor）：
     - 29 个通道压缩数据共 0.88 GB，单纯读字节 0.8 s；
     - LZW 解压单线程 0.48 GB/s，imagecodecs 解压时释放 GIL，8 线程到 2.05 GB/s，16 线程不再增加；
     - tifffile 的 `aszarr` 按区域读（S4-1P 与旧 Step4 的读法）：1 / 4 / 8 / 16 线程 14.9 / **7.5** / 9.0 / 9.1 s，**4 线程封顶**（每个 512² 分块的 Python 开销与 store 内部的串行化），再 `astype(float32)` 多约 1 s；
     - **自己按 TIFF 分块取字节（`os.pread`）+ tifffile 的 `page.decode` 并行解压**：8 / 16 线程 **4.3 / 4.0 s**，与 `aszarr` 逐像素相同。
     - 所以 S4-1P 的 10–12 s ≈ aszarr 封顶的 7.5 s + 转 float32 + 2 个校正通道（float32 lz4 zarr，约 2 GB）的读取。
  8. 环境：numba 0.67（已在 `envs/fusion_mesmer/environment.yml`），无 tbb；没有 anndata（S4-2 装）、没有 pyarrow。产品代码里目前没有任何地方导入 numba。
  9. 测试：Step4 只有 `tests/test_batch_step4_dialog.py`（只测批处理的目录发现与 CSV 计划表）；worker 与页面都没有测试。
- **做法**：
  1. **`core/quant_sources.py`（新，无 Qt）**：
     - `resolve_quant_job(run_dir 或其中的文件, roi_name)` → 一个不可变的作业描述：细胞 LabelStore（只读 `label_store[roi]`，`complete` 必须为真、`cell` 不为 null；形状、dtype uint32 与打开的 zarr 一致）、ROI 的全图 bbox、切片路径（运行所属工作区 `roi_manifest.json` 的 `source_ome`）、每个通道的来源。
     - **`QuantSourceResolver`**：Step0 的记录以工作区 `step0/step0_roi_result.json` 为入口。通道的决定取 `correction_config.json`；`original`（及不在表里的通道，如核通道）→ 读原始切片；`tophat` / `cucim` → **必须**读 `corrected_channels.zarr` 里本 ROI 组的同名数组，并核对：组属性 `roi_name` / `bbox_fullres` 包含本 ROI、数组形状 = 组的 ROI 形状、dtype float32、`correction_method` = 决定、参数 = 配置里该通道的有效参数、`channel_index` = 切片里该通道的序号、根属性 `source_ome` = 切片路径；另外 `correction_config.json`、zarr 根属性里的配置、manifest 的 `corrected_decisions` 三者对校正通道的决定必须一致。**任一项不符或缺失 → 作业开始前就报错**（列出通道与原因），不退回原始数据、不现场校正。新路径不导入 `OMETIFFLoader` 与任何校正函数。
     - **`QuantReader` 边界**：定量内核只拿到「一块标签 + 一批通道的数组」，不知道 TIFF / zarr 的内部细节。产品实现：
     - **原始读取**：`TiffTileReader` 按 TIFF 分块取字节（`os.pread`）、用 tifffile 的 `page.decode` 在线程池里解压，直接拼成 **uint8（或切片原有 dtype）** 的块，不转 float32。切片不是分块存储、或布局不是「每通道一页」时，退到 tifffile `aszarr` 按区域读（仍是同一来源，只是慢）——这不是来源退回，来源记录里写 `raw_reader`。以后的 NGFF 读取作为另一个 `QuantReader` 接入，不动内核与收尾。
     - **科学契约（fail-closed）**：`original` → 原始切片；`tophat` / `cucim` → 只读 Step0 持久化的校正结果并逐项核对；任何必需的校正结果缺失或不符 → **定量开始前中止并明确报错**。绝不允许「校正结果不可用 → 退回原始」，也绝不允许「校正结果不可用 → Step4 现场重做校正」。**读取方式可以退（性能），科学来源绝不退。**
     - 切片一致：所选运行的切片（`step3_masks.run_slide`）= 运行所属工作区的 `source_ome` = 当前打开的切片（与 Step3 的 `Load…` 规则相同：别的项目在同一张切片上的运行可以定量，别的切片的运行拒绝）。
     - 每个通道的来源记录：`kind`（raw / corrected）、路径、数组身份（形状、dtype、`source_identity`、方法与参数、算法版本）、读取方式。
  2. **`core/quant_engine.py`（新，无 Qt）**：
     - 三个边界：`QuantReader`（上面）、**`QuantBackend`**（`accumulate(labels, halo_labels, channel_block, ...)` / `merge()`，只产出累加器）、**`QuantFinalizer`**（由累加器算出形态与统计、过滤空标签；与后端无关，换后端不改科学公式）。**`NumbaBackend`** 是产品实现（`@njit(parallel=True, cache=True)`，按 S4-1P 的 `numba_ch`：每个通道一个任务 + 几何一个任务，**全局一套累加器**，float64 累加）；`RustBackend`、`CUDABackend` 只在接口处留名（选到时报「未实现」），不写实现。
     - 累加：几何 = count、Σx、Σy、Σx²、Σy²、Σxy（float64，**全图坐标**）、外接框、边界像素数；每个通道按所选统计只分配需要的数组（sum 总要；std 才要平方和；min / max 各自可选）。内核对 uint8 与 float32 各编译一份，一批通道按 dtype 分组调用。
     - **按空间分块、按通道分批**：细胞标签每块只读一次（LabelStore zarr，带 1 像素的外圈供周长用）；分块边长、每批通道数、读线程数、Numba 线程数都是实测初值，不写成契约。
     - **流水线**：一个读线程池预取「下一块 / 下一批」（队列深度 2，内存有上界），主计算线程同时累加当前批。
     - **边界像素数（`morphology_version = 2`）**：细胞 L 的一个像素，只要它 3×3（8 邻域）里有一个像素的标签 ≠ L（其他细胞或背景；图像外按背景算）就计入；列名 **`boundary_pixel_count`**，来源记录写 `perimeter_definition = "8-neighbor label-aware boundary pixel count"`。它**不是**旧的 `perimeter`：旧代码先把所有细胞并成一个二值图再腐蚀，紧贴的两个细胞之间的边界不计入；也不是标准的欧氏 / Crofton 周长（以后另加）。分块靠 1 像素外圈保证与整图计算逐像素相同。
     - **收尾**：有效细胞 = `count > 0`；标签编号 > `n_objects` → 报错（违反 LabelStore 契约）。形态：面积、质心（全图坐标）、外接框、长短轴、偏心率、方向（skimage 的约定：行轴与长轴的夹角，−π/2 … π/2）、等效直径、长宽比、extent、`boundary_pixel_count`。**不输出圆度**（边界像素数不是欧氏周长，4πA / P² 可能大于 1，易被误读；等 S4-2 定下周长语义再加）。表达量：所选的 mean / sum / std / min / max。
  3. **`workers/feature_extract_worker.py`（重写）**：QThread 外壳，参数改为 `run_dir`（或其中的文件）、`roi_name`、`output_dir`、`statistics`、`file_prefix`；进度按「块 × 批」计；Stop 在批之间生效。输出：
     - `<prefix>_cell_features.csv`：一行一个**有效**细胞；列 = `cell_id` + 形态（旧的 `perimeter` 列不再有，改为 `boundary_pixel_count`）+ `<通道>_<统计>`（通道名与今天相同：OME 名，`/` 与空格换成 `_`）；
     - `<prefix>_cell_features_provenance.json`：`morphology_version = 2`、运行 / ROI / LabelStore、`max_label_id`、`n_valid_cells`、`n_empty_labels`（≤ 1000 个时另写 `empty_label_ids`）、每个通道的来源记录、统计项、分块参数、耗时与峰值内存、git 提交；
     - 先写 `*.partial`，全部写完才改名；失败或 Stop 时删除，不留半成品。h5ad 在 S4-2。
  4. **`ui/step4_page.py`**（见裁定 1–4）：`Mask` 一行改为 `Run`（运行文件夹 + Browse）与 `ROI` 下拉框（运行只有一个 ROI 时只显示名字）；切片改为只读显示（取自运行所属的工作区，不再可以另选）；统计项 5 个快速统计默认全选，去掉 median / p90；输出说明改为 CSV + 来源记录；旧运行（没有 `label_store`）或来源核对失败，在页面上显示原因，`Extract Features` 不可用。
  5. **`ui/main_window.py`**：`_go_to_step4` 把 Step3 当前选择的（运行, ROI）交给 Step4（没有选择时用 Step2 刚完成的运行）；默认输出目录 `<运行所属工作区>/step4/quantification_runs/<segmentation_run_id>/<region>/`（`region` = ROI 名，空格换成 `_`，与校正结果 zarr 的组名同一规则；S4-2 的 h5ad 以后写进同一目录）；原来 4 处把 `_step4._out_edit` 设为 `step2_dir` 的地方改为不设（由选择决定）。
  6. **`ui/batch_step4_dialog.py`**（见裁定 7）：只改两处——把找到的 mask 所在的运行文件夹交给新 worker；删去在输出目录及其上级找 `correction_config.json` 的逻辑（来源由 resolver 决定）。
  7. **`scripts/verify_step4_s41.py`（新，不被产品导入）**：在带 tophat 的工作区上只读地跑产品 worker（输出到 `~/fusionflux/bench_step4/test1_tophat/<日期>_<哈希>_s41/`），做下面「真实数据」一节的全部对照，写报告 `docs/benchmarks/step4/s41_<日期>.md`。
- **白名单**：`core/quant_sources.py`（新）、`core/quant_engine.py`（新）、`workers/feature_extract_worker.py`、`ui/step4_page.py`、`ui/main_window.py`（只限 `_go_to_step4` 与上面 4 处 `_step4` 的设置）、`ui/batch_step4_dialog.py`（只限上面两处）；测试：`tests/test_quant_sources.py`（新）、`tests/test_quant_engine.py`（新）、`tests/test_step4_worker.py`（新）、`tests/test_step4_page.py`（新）、`tests/test_batch_step4_dialog.py`（只在断言因上面两处改动而须改时，实施前报批）；`scripts/verify_step4_s41.py`（新）；`docs/benchmarks/step4/s41_<日期>.md`（新）；`docs/benchmarks/step4/baseline_2026-09-27.md`（只补记产品路径的来源缺陷，见下）；`UI_SURFACE_RULES.md`（新增 Step4 一节）、`docs/user_guide.md`、`docs/用户指南.md`、本文档。
- **不改的范围**：`core/io_loader.py`（`OMETIFFLoader` 其他步骤照用）、`core/bg_correction.py`、`utils/calibration_source.py`、Step0–Step3 的行为与界面、viewer 及其读取 / 缓存 / 线程、Step2 的输出与 `.dat` 清理（④）、`core/step3_masks.py`（只调用）、Rust 探测、GPU。核 / 胞质、问卷界面、h5ad 新结构、CSV 可选属于 S4-2；分布统计属于 S4-3。**不装包。**
- **风险**：
  - 科学输出：新值必须与「来源等价的正确值」一致——验收锁定；长短轴 / 偏心率按用户裁定 7 改对 skimage。
  - 自己按分块解压 TIFF 是 Step4 内部的新读取方式：只用于 Step4，与 `aszarr` 逐像素对照（合成切片 + 真实切片抽查），布局不符时退到 `aszarr`。
  - Numba 首次编译约 2–3 s（`cache=True` 之后只有第一次）；缓存目录不可写时 numba 自己退到用户缓存目录。Numba 的线程层（本机无 tbb）只在一个线程里调用，不并发调用内核。
  - 功能空档：median / p90 到 S4-3 才回来（裁定 3）；h5ad 到 S4-2（本机今天本来就没写过）；旧运行须重跑 Step2（裁定 2）。
  - 回退：恢复这些文件。
- **验收门**：
  - **来源（合成项目）**：original → 原始、tophat → 校正结果；校正结果缺失、ROI 组不对、形状 / dtype / 方法 / 参数 / `channel_index` / `source_ome` 不符、三处决定不一致 → 各自报错且不产生输出；代码中新路径不调用 `OMETIFFLoader` 与校正函数；来源记录与实际读取一致（读原始 / 校正结果的次数计数）。
  - **原始读取**：合成的分块 TIFF（奇数尺寸、边缘分块、多通道）上与 `aszarr` 逐像素相同；非分块 TIFF 走退路也相同；真实切片抽 10 个块逐像素相同（只读）。
  - **内核（合成标签）**：与 float64 的 scipy / skimage 参考逐细胞一致：count、外接框、min、max、周长**完全相同**，sum / mean / std 相对误差 ≤ 1e-12，长短轴 / 偏心率 / 方向与 skimage `regionprops` ≤ 1e-8；**分块边长（含不整除的边长）、每批通道数、线程数改变时结果逐位相同**（周长的接缝）；空标签不输出且计数正确；标签 > `n_objects` 报错；只选部分统计时只输出所选列。
  - **边界像素数**：与独立的 label-aware 3×3 参考实现（整图的 3×3 最大 / 最小值滤波判定「邻域里有不同标签」再按标签计数）逐细胞完全相同；**不与旧 CSV 的 `perimeter` 对照**（语义不同）。
  - **worker**：CSV 与来源记录齐全；Stop 与写入失败不留 `*.partial`、不留半成品；旧运行（无 `label_store`）报出原因。
  - **页面（离屏）**：运行 / ROI 的选择与 Step3 交接一致；5 个统计默认勾选；旧运行的原因显示在页面上且不能运行。
  - **真实数据**（`scripts/verify_step4_s41.py`，带 tophat 的工作区，56 874 个标签）：
    - 有效细胞 56 872，`empty_label_ids = [29630, 53238]`；来源记录为 27 个原始 + 2 个校正（CD3D、HsBAg）；
    - 面积与参考 CSV（S4-0 对照 B）完全相同；质心与 29 通道 × 5 项统计相对差 ≤ 1e-5（受 CSV 6 位有效数字限制）；
    - 长短轴、偏心率、方向与 skimage `regionprops`（整张 mask）逐细胞 ≤ 1e-6；
    - `boundary_pixel_count` 与独立参考逐细胞完全相同；CSV 里没有圆度列；
    - 报告总时间、分项时间（读 mask、读原始、读校正、计算、写出）与峰值内存。**目标**（不作硬门）：总时间 < 15 s、峰值 < 3 GB。
  - **反向注入**：在任务临时目录的副本里至少注入——校正通道退回原始、跳过参数核对、边界计数不带外圈、float32 累加、校正结果缺失时退回原始（必须被来源测试抓到）、空标签照样输出、质心不加 ROI 偏移、`*.partial` 不清理、方向符号反了——每处至少一条测试变红。
  - **回归**：Step4 相关与 `test_step3_masks.py`、`test_batch_step4_dialog.py` 等，每模块单独进程，与 `git archive HEAD` 逐条对比，无新增失败。
  - **真机（用户）**：Step3 选带 tophat 的运行 → Step4 → 默认 5 项统计 → 运行：界面显示运行 / ROI / 切片，进度走完，输出目录里有 CSV 与来源记录，来源记录写明 2 个校正通道；选旧运行时页面说明原因。
- **请用户裁定**（v1 的 8 项；v2 按独立审核的意见修订，**用户 2026-09-27 确认批准**，申请提交推送后实施）：
  1. 输入：Step4 默认定量 Step3 当前选择的（运行, 该运行自己的 ROI），页面可另选；切片不能另选；继续核对所选运行的切片与当前打开的切片一致。——审核：同意。
  2. 没有完整 `label_store` 的旧运行直接拒绝，提示重跑 Step2，不建兼容层。——审核：同意。
  3. median / p90 在 S4-1 从界面去掉，S4-3 作为分布统计加回。——审核：同意。
  4. S4-1 固定写 CSV + 来源记录 JSON，h5ad 在 S4-2；默认目录为稳定的产品路径 `<工作区>/step4/quantification_runs/<segmentation_run_id>/<region>/`（v1 的 `step4/<run_id>/<ROI>/` 改掉）。——审核：按此修订后同意。
  5. 形态输出由矩与几何直接得到的项；**不输出圆度**。——审核：部分同意，按此修订。
  6. 3×3（8 邻域）label-aware 边界像素计数，列名 `boundary_pixel_count`，来源记录写定义；不要求复现旧的并集腐蚀周长。——审核：按此修订。
  7. 批处理对话框只做最小接线。——审核：同意。
  8. Step4 内部按 TIFF 分块并行解压，封装为独立的 `QuantReader`；非分块、未知压缩或不支持的 TIFF 退到 tifffile 读法；性能可退，科学来源不退。——审核：同意。

- **实施中的用户裁定（2026-09-27）**：
  9. **纯核方法的运行**（LabelStore 只有 `nucleus`）：把核当作主对象定量，来源记录写 `primary_compartment = nucleus`，页面写明「nuclei (this result has no cell mask)」（与旧 Step4 的行为一致，不出现功能空档）。
  10. **批处理对话框的 median 勾选项不改**（白名单保持「两处」）：勾了 median 的那一行会失败并显示原因「not a fast statistic: ['median'] — Step 4 computes mean, sum, std, min and max」。
- **执行记录**（2026-09-27，未提交）：
  - `core/quant_sources.py`（新，无 Qt）：`resolve_quant_job` → `QuantJob`（只读 LabelStore；`complete`、dtype、形状、`n_objects` 核对；ROI bbox；运行 / 工作区 / 当前打开的切片三者一致）；**fail-closed 的来源**：Step0 交接 → `correction_config.json` → 校正结果，逐项核对（本 ROI 的组、组的 bbox 覆盖区域、形状、float32、方法、有效参数、`channel_index`、`source_ome`、handoff / 配置 / zarr 三处决定一致），任何一项不符就在定量前抛 `QuantSourceError`，不退回原始、不现场校正（模块不导入 `OMETIFFLoader` 与校正函数，只用 `resolve_effective_correction_params` 算有效参数）。`QuantReader` 边界：`TiffTileReader`（按 TIFF 分块取字节、tifffile `page.decode` 并行解压，保持切片 dtype；OME 的后续页是轻量 frame，须先 `aspage()` 才能判断布局；布局不符退到 tifffile zarr，来源记录写 `raw_reader`）与 `JobReader`（带 1 像素背景外圈的标签、按来源分批读通道并计数）。
  - `core/quant_engine.py`（新，无 Qt）：`QuantBackend` / `NumbaBackend`（`@njit(parallel=True, nogil=True, cache=True)`，每个通道一个任务 + 几何一个任务，全局一套累加器，强度 float64 累加；只为所选统计分配数组）/ `RustBackend`、`CUDABackend`（只留名，报未实现）；`QuantFinalizer`；`quantify`（生产者线程预读下一块 / 下一批，队列深度 2；Stop 在批之间生效）。**实施中的改进**：几何矩改为 **int64 精确累加**，中心矩用 Python 整数算 n·Σx² − (Σx)²——没有抵消误差，圆形细胞两个方差精确相等时与 skimage 一样取 −π/4（首版 float64 的 `Σx²/n − cx²` 在这种细胞上方向差 π/4，被测试抓到）。
  - `workers/feature_extract_worker.py`（重写）：`run_extraction`（CSV 整数列用 `%d`，其余 `%.6g`；来源记录 JSON；`*.partial` 写完才改名，来源记录改名失败时连同已改名的 CSV 一起删除）与 QThread 外壳（来源 / 标签错误给原因而不是 traceback；非快速统计给出说明）。
  - `ui/step4_page.py`：`Run` + Browse、`Region` 下拉框、只读 `Slide`、来源摘要、红色原因行（此时 `Extract Features` 不可用）；5 个快速统计默认全选；输出说明为 CSV + `_provenance.json`；运行时保留用户改过的输出目录；完成 / 出错的对话框走可替换的接缝（离屏测试不弹框）。
  - `ui/main_window.py`：`_go_to_step4` + `_step4_choice`（调用方给的运行 → Step3 当前选择的（运行, 区域）→ 最近的 Step2 结果；都没有时保留页面自己的选择）；删去 4 处把 `_step4` 的 OME / 输出框设为 `step2_dir` 的代码。
  - `ui/batch_step4_dialog.py`：把找到的 mask 所在的运行文件夹交给新 worker；删去 `_find_correction_config`。
  - 文档：`UI_SURFACE_RULES.md` 新增 Step4 页面一条；两份用户指南的 Step4 一节（来源规则、Run / Region、统计项、输出列与 `boundary_pixel_count` 的定义、来源记录、旧长短轴的差异）。
  - **测试**（新）：`tests/test_quant_sources.py` 26 条、`tests/test_quant_engine.py` 18 条、`tests/test_step4_worker.py` 11 条、`tests/test_step4_page.py` 8 条，全部通过；`tests/test_batch_step4_dialog.py` 未改、6 条通过。
    - 说明一处与申请措辞的差别：分块边长改变时，**float32 通道**的 sum / mean / std 的累加顺序随分块变化，逐位不一定相同（测试按相对误差 1e-12）；其余所有列（uint8 通道的全部统计、count、外接框、min / max、边界像素数、几何）逐位相同。每批通道数与线程数改变时全部逐位相同。
    - 偏心率 = √(1 − λ₂/λ₁)：近圆细胞上平方根把 1e-14 的舍入放大到约 1e-7，测试对偏心率平方按 1e-12、对偏心率按 1e-6（真实数据门）核对。
  - **反向注入** 12 处（任务临时目录的副本），每处至少 1 条变红：校正结果缺失时退回原始、跳过参数核对、任意含同名通道的组都接受、边界计数不带外圈、float32 累加、空标签照样输出、质心不加区域原点、方向符号反了、`*.partial` 不清理、原始分块的偏移错一位、页面运行时覆盖用户的输出目录、忽略 Step3 的选择。
  - **回归**：19 个模块（上面 5 个 Step4 模块，以及 `test_main_window_step1_5`、`test_step0_step1_handoff_contract`、`test_step3_masks`、`test_step0_authoritative_save_barrier`、`test_step1_step2_handoff_e2e`、`test_step1_session_button`、`test_step1_load_weights`、`test_b3_fixes`、`test_step3_page`、`test_step1_handoff_invalidation`、`test_step1_dataset_switch`、`test_no_real_project_writes`、`test_step1_to_step2_handoff`、`test_step0_step1_surface_details`）每模块单独进程，与 `git archive HEAD`（`70b4565`）逐条对比：两边都没有失败，**无新增失败**；`test_ui_surface_contract.py` 两边 8 条通过。
  - **真实数据**（`scripts/verify_step4_s41.py`，报告 `docs/benchmarks/step4/s41_2026-09-27.md`）：总时间 **8.6 / 8.3 s**、峰值 **1.16 GB**（S4-0 对照 B：1014 s / 8.6 GB）；有效细胞 56 872、空标签 [29630, 53238]；27 raw + 2 corrected；面积与 S4-0 参考完全相同，质心与 29 通道 × 5 项统计 147 列相对差 > 1e-5 的为 0；长短轴 / 偏心率 / 方向对 skimage 最大 7e-14；`boundary_pixel_count` 对独立参考 56 872 个细胞完全相同；原始读取抽查 10 块逐像素相同。仍受读盘限制（计算 1.4 s，读 6.8 s）。
  - **真机验收通过（用户 2026-09-27）。**
  - advisory：① 批处理对话框按旧的 `Scan*/` 目录找样本，与现在的项目结构不符（TMA 批处理时重做）；② Step2 完成对话框的「→ Feature Extraction (Step 4)」按钮仍发 `open_qc_requested`（进 Step3，已有问题，未处理）；③ `core/bg_correction` 在导入时做 CuPy 的 GPU 自检（本机因 nvrtc 失败并打印两行），Step4 的来源解析因导入参数函数而触发它——产品里主窗口本来就导入了这个模块，不增加开销。

### 块 N3 — Step2 切块接缝的合并冲突（只读调查结论与申请 v3，按两轮独立审核修订，**用户 2026-09-27 批准**；插在 S4-2 之前；分 N3a / N3b 两步）
- **来由**：S4-2 调查发现，带 tophat 的工作区里有 457 个核像素不在它的细胞里。用户与 ChatGPT 讨论后裁定（2026-09-27）：先修 Step2，再做 S4-2；S4-2 改为只有一个 h5ad（修订为 v2，放在 N3 之后审）。
- **只读调查结论**（HEAD `de80ec5`）：
  1. **合并规则**（ROI 循环 `segment_merge_worker.py:2785-2845`，整图循环 `:3676-3733`，两处相同）：
     - 每个切块带 200 px 的重叠带读入，分割后只保留**质心落在本块归属区内**的整个细胞（`core/label_ownership.kept_labels`）；
     - 保留的细胞**连同伸进重叠带的部分一起**写进全图：`np.copyto(dst, remapped, where=remapped > 0)`，切块按行优先的顺序写，**后写的覆盖先写的**；
     - 核的数组同样由后写者覆盖。
  2. **缺陷的机制**：相邻两块各自分割同一段重叠带，同一个真实细胞可能在两边都被分出来，形状不同、质心各落一边，于是两边都保留（重复）；也可能两边都不保留（丢失，无法从最终 mask 里看出来）。重复的两份在像素上重叠时，后写的那份盖掉先写那份的一部分，先写的只剩一块小碎片或碎成几块；如果全部被盖掉，就成了空标签。核的数组被覆盖的范围和细胞不同，所以核像素会落进邻块的细胞里。类的注释写着「This guarantees every cell is counted exactly once and no cell is truncated」（`:107-110`），只在两块对重叠带的分割完全一致时才成立。
  3. **实测（test1 全部已完成的运行，只读）**：「接缝附近」= 外接框距切块归属区边界 150 px 以内的细胞，「对照带」= 把接缝线平移 1000 px 后同样统计；「很小的碎片」= 面积不到中位数 25%。

     | 运行 | 空标签 | 接缝附近：碎成几块 / 很小的碎片 | 对照带：碎成几块 / 很小的碎片 | 核错位 |
     |---|---|---|---|---|
     | cellpose 整细胞（074644，40 843 个细胞） | 1 | 47 / **153**（6059 个细胞中） | 35 / 38（4864 个中） | — |
     | cellpose + expansion（48 398） | 0 | 63 / **51** | 43 / 16 | 421 px / 17 个核 |
     | stardist + expansion（56 874） | 2 | 31 / **88** | 12 / 26 | 457 px / 38 个核 |

     接缝附近的小碎片约为对照带的 3–4 倍，整细胞方法也有：**这是细胞合并的缺陷，所有方法都受影响，不只是核**。按比例估计，每个运行在接缝上有约 60–120 个被截断或重复的细胞（约占全部细胞的 0.1–0.3 %；这次的切块是 3 × 4，WSI 的切块多得多，接缝总长也随之增加）。
  4. 运行目录里没有保存每块的原始分割（`tile_masks/` 是空的），所以「重复」与「丢失」只能间接量化；修好之后可以在同一数据上直接数出被合并规则处理的细胞数。
  5. 共用这两处循环的还有 HQ / HQ2 / CDS（不维护，但共用路径上的回归照常算失败）。Step1 预分割只有单个窗口，没有接缝，不受影响。
- **修法的两种选择**：
  - **A. 只修核（最小）**：全图合并完成之后、`label_store` 标记完成之前，逐核核对「全部像素都在对应表所指的细胞里」，有一个像素不在就**整个丢弃**（N2 的严格规则），重新紧凑编号核与对应表，计入新的丢弃类别 `seam_conflict`。细胞 mask 不变。N2 的契约重新成立，但**细胞的截断、重复与空标签照旧**。
  - ~~B. 合并时解决冲突~~（v1；独立审核指出：它放在 `kept_labels` 的质心归属**之后**，只能处理「两份都被保留、互相覆盖」，处理不了「两边都被质心规则丢掉」的细胞——v2 改为 B2）。
  - **B2. 接缝候选在最终去留与编号之前统一裁决（建议，v2）**：
    1. **候选**：每个切块的分割结果里，凡是不碰到**内部**窗口边（有邻块的那几条边；碰到切片 / ROI 真实边界的细胞照常可用）的细胞都是候选，不再只看质心。每个候选记下：所在切块、局部标签、像素（裁出的小块）、质心、**归属余量**（质心到本块归属区边界的有符号距离，在归属区内为正，越大越「位于本块内部」）。
    2. **内部细胞立即写出**：与任何邻块的读取窗口都不相交的候选（离接缝超过重叠宽度），不可能与别的切块冲突，按现在的规则（质心在归属区内）立即写出、编号。
    3. **接缝候选推迟**：与邻块窗口相交的候选先放进一个「待裁决」存储，等覆盖它的**所有**邻块——上下左右与四个对角——都处理完再裁决（行优先顺序下，下方、左下、右下邻块都在下一行，所以最多等一行；四块交汇处的候选要等四块都完成）；存储只含重叠带里的小块，WSI 上约 100 MB 量级。
    4. **裁决**（只在接缝候选之间，v3）：
       - **重叠图**：候选是节点；来自**不同**切块、满足重叠条件（重叠 / 较小面积 ≥ τ，及 N3a 实测后可能加的辅助条件）的两个候选连一条边；按**连通分量**裁决（A≈B、B≈C 而 A、C 不直接相连时，三者同属一个分量），与处理顺序无关。
       - **以切块的版本为单位**：一个分量里每个切块的候选合起来是该切块对这片区域的「版本」；每个分量选出**一个获胜切块，保留它在这个分量里的全部候选**，其余切块在这个分量里的候选全部丢弃。这样「一块把两个相邻细胞分成一个 M、另一块分成 P 与 Q」时，要么保留 M，要么同时保留 P 和 Q，不会只留 P 而丢掉 Q。
       - **获胜规则**（确定的）：该切块版本的并集质心的归属余量最大者 → （可选）再比较面积等确定性指标 → 最后按切块序号打平。
       - 只有一个切块的分量（与谁都不重叠的候选）：质心在自己归属区内 → 保留（与现在相同）；质心在邻块归属区内、而邻块没有任何版本 → **也保留**（邻块漏掉了它；这是「两边都丢掉」的补救）；否则丢弃。
    5. **像素**：保留下来的候选按归属余量从大到小写入，只写还空着的像素（不覆盖），因此结果不依赖切块的遍历顺序；被让出的边界像素记为 `clipped_pixels` / `clipped_fraction`。
    6. **编号**在写入时分配，所以没有空标签、编号 1 … N 连续。代价：接缝细胞比同一块的内部细胞晚编号，**所有细胞的编号顺序与现在不同**（形状不变的细胞只是编号变了）。
    7. **核跟随细胞**：核在每块里照旧配对（N2）；细胞被丢弃，它的核也丢弃；核只写在全图上属于它的细胞的像素上，有任何像素写不进去就整个丢弃，计入新类别 `seam_conflict`——**构造上保证** N2 的包含关系。
    8. **收尾核对（流式、按块，不整图展开）**：细胞编号 1 … N 连续且每个都有像素；核编号 1 … M 连续且每个都有像素；每个核像素所在的细胞 = `nucleus_to_cell[核]`；任一项不成立 → 运行失败、不登记（`label_store` 不写 complete），当作程序缺陷的最后防线。
    9. **记录**（运行汇总与 `label_store` 的 `seam_reconciliation`）：`candidates`、`duplicate_groups`、`duplicate_versions_removed`、`split_merge_groups`（分量里某个切块有不止一个候选）、`recovered_missing_cells`、`cells_with_clipped_pixels`、`clipped_pixels`、`nuclei_dropped_seam_conflict`、`threshold`。
    - **分两步实施（v3）**：
      - **N3a（探测，不改产品输出）**：`core/seam_merge.py` 的候选提取、归属余量、重叠图与分量、按切块版本裁决（先都做成纯函数）；探测脚本 `scripts/probe_seam_merge.py`（不被产品导入）在带 tophat 的工作区的**复制件**上用真实 StarDist 引擎逐块分割（写到任务临时目录与 `~/fusionflux/bench_step2/`，不写 `~/fusion_data`），输出每条边与每个分量的「重叠 / 较小面积」、IoU、质心距离、归属余量、每个切块的候选数（找出 split / merge 分歧），以及按 τ = 0.5 裁决时各项计数。报告写进 `docs/benchmarks/step2/seam_probe_<日期>.md`，**然后停下**，由用户锁定 τ 与必要的辅助条件。
      - **N3b（正式启用）**：按锁定的规则接进 Step2 的两处循环，完成下面全部验收。
    - τ = 0.5 只是 N3a 的探测起点，**不是产品参数**，锁定前不接进 Step2。
    - 远离接缝的细胞形状逐像素不变（编号可能不同）；接缝上的细胞按上面的规则改变——科学输出的改变，须重跑。

- **白名单**（按 B2；N3a 只用其中的 `core/seam_merge.py`、`tests/test_seam_merge.py`、`scripts/probe_seam_merge.py`、`docs/benchmarks/step2/`、本文档，不改 Step2）：`workers/segment_merge_worker.py`（两处切块循环：候选、内部细胞的写出、待裁决存储、裁决后的写出与编号、收尾核对、汇总与 `label_store` 的计数）；新文件 `core/seam_merge.py`（无 Qt：候选的提取与归属余量、待裁决存储、分组与裁决、写入顺序、流式收尾核对；两处循环共用，便于单测）；测试：新 `tests/test_seam_merge.py`，以及现有 Step2 测试中**因接缝规则改变而须改的参考与断言**（预计 `test_step2_runner_path.py`、`test_step2_engine_unified.py`、`test_step2_keeps_nuclei.py`、`test_step2_ownership_move.py`；实施前列出具体用例报批）；`docs/user_guide.md`、`docs/用户指南.md`（Step2 产出一节）；本文档。
- **不改的范围**：分割引擎与输入、切块策略与重叠宽度、`core/label_ownership.py` 的质心归属（Step1 共用）、Step1 预分割、Step3、Step4、标签金字塔的算法、`.dat` / OME 导出（④）。
- **风险**：
  - 接缝上的细胞 mask 改变：已有运行不会自动修复（Step4 的 S4-2 仍按逐像素定义防御）；
  - τ 取值影响「重复」与「相邻两个真实细胞」的区分：先测分布、由用户锁定（见上）；
  - 复杂度：待裁决存储与推迟写出是新的状态（本块明确申请）；接缝细胞的编号晚于同块的内部细胞，**所有细胞的编号顺序改变**（下游按编号引用旧结果的地方不受影响，因为旧运行不改）；
  - 补救「邻块漏检」的细胞会让接缝上多出少量细胞（今天被丢掉的）；实施时报告数量；
  - 内存：待裁决存储约为「重叠带面积 × 4 字节」的量级（WSI 上约 100 MB）；性能：每块多一次候选提取与分组，相对推理可忽略；收尾核对多一遍流式读（本机约 2 s，WSI 约 20 倍）。
- **验收门**：
  - `core/seam_merge.py`（合成）：重复（重叠 ≥ τ）→ 保留归属余量大的版本，另一份丢弃；**两边都被质心规则丢掉的细胞被补回**；邻块完全漏检的细胞被保留；边界分歧 → 余量大的先写、另一个只写空像素并记录裁掉的像素；**切块处理顺序打乱（行优先 / 列优先 / 倒序）结果相同**；链式重叠（A≈B、B≈C、A 与 C 不直接相连）归为一个分量；**split / merge 分歧**（一块一个 M、另一块 P 与 Q）要么保留 M、要么同时保留 P 和 Q；**四块交汇点**上同一个细胞在四块里各有一个版本 → 只保留一个，且结果与顺序无关；编号连续且每个都有像素；核跟随细胞、写不全就丢弃并计为 `seam_conflict`；多核细胞；收尾核对对每一项破坏都失败；两处循环（ROI、整图）结果相同。
  - Step2（真实 Cellpose / StarDist 引擎、合成图像，ROI 与整图两种模式）：远离接缝的细胞与改动前形状逐像素相同（按标签对应比较，编号可不同）；全图没有空标签；逐像素「核像素所在的细胞 = 对应表[核]」**全部成立**；汇总计数与实际一致；收尾核对故意破坏时运行失败、不登记。
  - **真实数据**（本机重跑 StarDist + expansion 约 3.5 min，写到临时目录的复制工作区；以及用户真机重跑）：先报告「重叠 / 较小面积」、IoU、质心距离的分布，**由用户锁定 τ**；然后：核错位 0；空标签 0；接缝附近的小碎片与碎成几块的数量降到对照带水平；报告重复组数、补救数、裁掉的边界像素数；收尾核对通过；耗时与峰值内存与改动前相近。
  - 反向注入：仍然覆盖写、重复不丢弃、裁决按先写者而不按余量（顺序打乱测试应变红）、逐对贪心而不按连通分量、按单个候选而不按切块版本裁决（split / merge 用例应变红）、不等对角邻块就裁决（四块交汇用例应变红）、不补救两边都丢掉的细胞、编号在去留之前分配（出现空标签）、核不跟随细胞、核写不全仍保留、收尾核对不生效。
  - 回归：Step2 / 标签归属 / 标签金字塔 / Step3 相关模块与 HEAD 逐条对比，只有已报批的参考改动。
  - 真机（用户）：重跑一个 expansion 方法，Step3 看接缝处（x ≈ 8108 等）的细胞与核轮廓：没有被直线截断的细胞、没有残片；终端与 metadata 给出上面的计数。
- **请用户裁定**（v2 按独立审核修订）：
  1. **修法**：B2（接缝候选在最终去留与编号之前统一裁决，能补回两边都丢掉的细胞；所有方法受益；接缝上的细胞 mask 与全部编号顺序改变）。——审核：选 B，修订为 B2。
  2. **τ = 0.5 只作 N3a 的探测起点**：指标用「重叠 / 较小面积」，同时记录 IoU、质心距离与归属余量；N3a 报告分布后停下，由用户锁定 τ 与辅助条件，再做 N3b。——审核：同意，并要求分 N3a / N3b。
  3. **按重叠图的连通分量、以切块版本为单位裁决**：每个分量保留归属余量最大的那个切块的全部候选（split / merge 分歧不丢真实细胞），切块序号只作打平；**边界分歧时余量大的先写**，另一个只占空像素，并记录 `clipped_pixels` / `clipped_fraction`。——审核：不用先写者，按此修订。
  4. **收尾核对**（流式：细胞编号连续且都有像素、核编号连续且都有像素、每个核完全在它的细胞里）**不通过就让运行失败、不登记**。——审核：同意。

- **N3a 执行记录**（2026-09-27，未提交）：
  - `core/seam_merge.py`（新，无 Qt，纯函数）：`extract`（候选：不碰内部窗口边；质心、归属余量、是否按今天的规则归属）、`is_seam`、`pairs`（跨切块共享像素的每一对：重叠 / 较小面积、IoU、质心距离）、`components`（并查集）、`resolve`（按连通分量、以切块版本为单位裁决；获胜 = 版本并集质心的归属余量最大，切块序号打平；单切块的分量保留，质心在邻块的计为补回）、`paint`（按余量从大到小只写空像素；**N3a 实测后加的辅助条件** `min_free_fraction`：空像素不到自身面积的这个比例就整个放弃）。
  - `tests/test_seam_merge.py` 13 条：内部窗口边与切片边界、余量符号、重复取余量大者（包括后面的切块赢）、今天的重复、两边都丢的补回、邻块漏检、split / merge（M 赢，或 P 与 Q 一起赢）、链式重叠为一个分量、四块交汇只留一个、切块与候选顺序打乱结果相同、只写空像素与连续编号、不足最小比例整个放弃。反向注入 5 处（胜者按序号、按单个候选裁决、不连通分量、不排除内部窗口边、覆盖写），首轮有 2 处没有被专门的用例抓到，补了「后面的切块赢」与「P 与 Q 一起赢」两条后全部抓到。
  - `scripts/probe_seam_merge.py` + 报告 `docs/benchmarks/step2/seam_probe_2026-09-27.md`：**分布清楚分成两堆**（0.3–0.7 之间只有 695 对，约 2 %）；τ = 0.5 只写空像素时接缝反而更差（小碎片 88 → 126），**加上「空像素 < 50 % 整个放弃」后**三种 τ 结果几乎相同：碎块 31 → 23、小碎片 88 → 79、空标签 2 → 0、细胞数 +74；剩下的碎块 / 小细胞与模型预测本身的比例相同（合并之前就有），不是合并造成的。
  - 停下等用户锁定 τ 与辅助条件；**用户 2026-09-27 锁定 τ = 0.5、`minimum_writable_fraction` = 0.5**。

- **N3a 之后的独立审核与用户裁定（2026-09-27，用户确认：锁定两个 0.5，批准下面的测试改动清单，开始 N3b）**：锁定 τ = 0.5 与「可写空像素 < 0.5 整个放弃」；元数据**永久记录**这两个参数与版本：

  ```
  seam_merge:
    version: 1
    duplicate_overlap_threshold: 0.5
    minimum_writable_fraction: 0.5
  ```

  将来换方法 / 重叠宽度 / 真实 WSI 发现新的分布时，版本化为 `version: 2`，不悄悄改阈值。剩下的接缝富集是分割模型本身的切块上下文问题，不在合并层继续清洗（否则会误删真实的小细胞）。
- **N3b 须报批的测试改动**（这些测试的参考都复刻了旧的「后写覆盖」合并，N3b 后不再成立）：
  - `tests/test_step2_runner_path.py`：`_oracle` 改为用 `core/seam_merge` 的规则由每块结果生成参考（`test_a_hand_over_equals_the_runner_with_shared_ownership` 用它）；
  - `tests/test_step2_engine_unified.py`：`test_a_manual_run_equals_the_runner` 调用上面的 `_oracle`，随之变化，本文件预计不改代码；
  - `tests/test_step2_keeps_nuclei.py`：第 160–170 行的细胞参考改为新规则，核的参考加上「跟随细胞、写不全就丢弃（`seam_conflict`）」；
  - `tests/test_step2_ownership_move.py::test_both_loops_equal_the_legacy_inline_ownership`：两处循环与「旧的内联归属 + 覆盖写」相等 → 改为两处循环都与新规则的参考相等（HQ / HQ2 的参数化用例同样）；
  - `tests/test_label_ownership.py::test_step2s_real_merge_equals_this_module_tile_by_tile`：「Step2 的合并 = 本模块逐块 + 覆盖写」→ 改为「Step2 的候选归属与本模块一致、合并按 `seam_merge`」；`core/label_ownership.py` 本身不改（Step1 共用），其余 4 条不变。

- **N3b 实施中的用户裁定（2026-09-27）**：接缝候选在**整个区域的切块跑完后一次裁决**（v3 写的「等齐邻块、最多等一行」不成立：边界分歧的像素要按余量分配才与遍历顺序无关，部分重叠的候选会沿接缝连成一串、横竖接缝在交汇处相连，可能一直连到最后一块）。内部细胞仍逐块立即写出。
- **N3b 执行记录**（2026-09-27，未提交）：
  - `core/seam_merge.py`：`SeamMerger`（`add_tile`：候选提取，内部且按今天规则归属的细胞立即写出，接缝候选的 bool 裁剪与核的局部编号裁剪写到运行目录的 `.seam_candidates_<区域>/`；`finish`：读回全部接缝候选、`resolve`、按余量写入、删除临时目录）；`_write`：只写空像素、可写 < 50 % 整个放弃、编号在决定之后分配；核只在全部像素都落在细胞写入的像素里时写入，否则计 `nuclei_dropped_seam_conflict`；「核 → 细胞」用 numpy 缓冲，每 2^20 项交给 zarr 表追加一次（N2 契约）；`validate`：按行块流式核对细胞编号 1 … N 都有像素、核编号 1 … M 都有像素、每个核像素在它的细胞里，失败抛 `SeamContractError`；`SEAM_MERGE = {version 1, 0.5, 0.5}`。
  - `workers/segment_merge_worker.py`：`_seam_begin` / `_seam_add_tile` / `_seam_finish`，两处循环（ROI、整图）只对引擎方法走新合并，HQ / HQ2 / CDS 照旧；每块的 `tile_stats.n_cells` 现在是该块立即写出的内部细胞数（接缝细胞在收尾时计入总数）；收尾核对失败 → 运行失败、不登记；`label_store` 写入 `seam_merge` 与 `seam_reconciliation`，`nuclei.dropped` 增加 `seam_conflict`（终端打印「on tile seams」）；临时目录登记进 `_nuclei_partials`，成功、失败、Stop 都会清理。
  - 测试：`tests/test_seam_merge.py` 共 20 条（N3a 的 13 条，加 `SeamMerger` 的内部 / 接缝写入、核因接缝冲突丢弃、对应表分块、收尾核对对每种破坏报错，Step2 端到端：两块各给一个版本、按今天的规则两边都丢的细胞被补回且完整，替身的核对失败 → 运行不登记，**真实核对**抓到被破坏的合并结果 → 运行失败）；按批准的清单改参考：`test_step2_runner_path.py::_oracle`（改用 `reference_merge`，并按 Step2 的编号顺序——内部细胞按切块与标签、接缝细胞按余量——因此仍是**逐像素、逐编号**相等，`test_step2_engine_unified.py` 不改代码）、`test_step2_keeps_nuclei.py`（细胞参考、`seam_merge` 记录、`seam_conflict: 0`；对应表 I/O 失败的注入点由「第 2 次追加」改为「第 1 次」，因为对应表现在每 2^20 项才追加一次）、`test_step2_ownership_move.py`（只有引擎方法 `cellpose_wholecell_fusion` 改为新参考；HQ / HQ2 保持旧参考——比清单改得少）、`test_label_ownership.py`（Step2 的归属标志等于本模块的质心规则，合并等于 `reference_merge`，逐编号相等）。
  - 反向注入 9 处：覆盖写、重复不丢弃、胜者按切块序号、逐对不连通分量、编号在去留之前分配、核不完整仍保留、不调用收尾核对、`seam_conflict` 不计入——各使至少 1 条变红；「不按归属也写内部细胞」没有测试变红，它是**等价变异**（候选的质心必在外接框内；质心不在本块就在邻块的归属区内，外接框就与邻块窗口相交，它就是接缝候选，所以内部候选必然按今天的规则归属）。
  - 真实数据（`docs/benchmarks/step2/seam_probe_2026-09-27.md` 的 N3b 一节）：细胞 56 948、空标签 0、**核像素不在自己的细胞里 0**（原 457）、接缝附近碎块 / 小碎片 23 / 79（原 31 / 88）——与 N3a 的离线预测逐项相同；因接缝冲突丢掉的核 110；Step2 耗时与峰值内存不变。
  - 实引擎回归：`test_step2_runner_path` 30 通过 6 跳过、`test_step2_engine_unified` 32 通过 6 跳过（跳过的都是 Mesmer，本机无模型）。
  - 回归：34 个模块（Step2 全部相关、HQ / HQ2 / CDS 的 worker、标签归属 / 金字塔、预分割、Step3 数据层与标签绑定、Step1→Step2 交接）每模块单独进程，与 `git archive HEAD`（`5d8cb59`）逐条对比，**无新增失败**。两边相同：`test_seg_runner.py` 2 条（StarDist 偶发 1 像素差、Mesmer）、`test_seg_runner_engines.py` 3 条（2 条 Mesmer 无模型、1 条 StarDist）、`test_hq_marker_segmentation.py` 2 条（HQ 不维护）；HEAD 侧另有 `test_preseg_run.py::test_the_job_equals_the_steps_done_by_hand` 1 条偶发失败，本侧通过。
  - 文档：两份用户指南的 Step2 产出加「切块接缝」一段（规则、以前的结果须重跑、元数据里的计数、重叠带须比最大的细胞宽）。
  - **前提**（写进用户指南）：重叠带必须比最大的细胞宽；否则一个细胞可能在所有覆盖它的窗口里都碰到内部窗口边，从而不成为候选（旧规则会保留一个被截断的版本）。产品的重叠带 = Step1 的 halo（默认 200 px），远大于细胞。
  - **真机验收通过（用户 2026-09-27）**：工作区 `full_wsi_20260927_194511_cc8e`，StarDist + expansion，**4 × 5 = 20 块**（与本机测试的 3 × 4 不同）：接缝候选 24 076、重复组 10 298（丢弃版本 11 504，split / merge 366）、补回 173、可写不足一半而放弃 87、被裁边界像素的细胞 427（12 864 px）、因接缝冲突丢掉的核 97；细胞 56 892；核保留率 99.8 %（含「97 on tile seams」）；3 min 28.6 s、峰值 9.23 GB。Step3 里接缝处没有明显不合理的 mask。
  - advisory（与本块无关）：Step2 完成后自动进入 Step3 时，GUI 线程在 Step3 GPU 图层的 `resizeGL`（`step1_viewer_mount._start_gpu_backend` → `layer.show()`）停了 6.5 s（gui-watchdog）。

### 块 S4-2 — 核 / 胞质表达、输出范围问卷、单一 h5ad、CSV 可选（申请 v2，按用户裁定与独立审核修订，**用户 2026-09-27 批准**）
- **v1 → v2 的来由**：v1（修订记录 v3.86）之后，用户裁定（2026-09-27）：**只要一个 h5ad**，一行一个主对象，核 / 胞质是同一个细胞的不同表达层，与全细胞一起进入下游注释；不做单独的核表（S4-0 裁定 4 由此改掉，以后需要时另议）；先修 Step2（块 N3，已完成、真机验收通过）。独立审核（ChatGPT，用户转述）另提出：主对象的 schema、**输出的内存上界**（不能为了建 h5ad 把所有 layer 同时摊在内存里）、问卷第二组改名为 `Expression regions`、float32 读回精度门。都采纳，写进下面。
- **只读调查结论**（HEAD `c9f53a3`；v1 的调查仍成立，这里只列变化）：
  1. N3 之后，新的 Step2 运行在构造上保证「每个核像素都在 `nucleus_to_cell` 所指的细胞里」，`label_store` 带 `seam_merge`（版本 1）；N3 之前的运行没有这一节，可能有接缝上的核错位（test1 上 17–38 个核、421–457 个像素）。
  2. anndata 0.11.4：先 `write_h5ad` 一个只有 X / obs / var / uns 的骨架，再用 h5py 在 `layers/` 下按行块逐个写数据集（`encoding-type = array`、`encoding-version = 0.2.0`），`read_h5ad` 能原样读回（在临时目录里用下载的 wheel 验证，未装进环境）。所以 h5ad 可以**一次一个 layer、按行块**写出。
  3. S4-1 的累加器在整遍扫描期间常驻内存：每个「区域 × 统计」一张 `通道数 × 细胞数` 的 float64 表。WSI 上（200 万细胞 × 29 通道）全选三个区域时约 10 张 → 约 4.6 GB，再加一份 float32 的输出副本，会把 S4-1 做到的 1.2 GB 峰值推回 10 GB 以上。
- **做法**：
  1. **区域的定义**（逐像素；新运行上与「细胞 − 核」完全相同，旧运行上不会出现负数）：细胞 c = `cell == c`；c 的核区域 = `cell == c` 且 `nucleus > 0` 且 `nucleus_to_cell[nucleus] == c`；c 的胞质 = c 中其余像素。来源记录写 `nucleus_pixels_outside_their_cell`（像素数与核数；新运行应为 0）。
  2. **累加（同一遍扫描）**：每块多读一次核标签（对应表整张读入，M + 1 个 uint32）。细胞同 S4-1；核区域：count 与每通道 sum / 平方和 / min / max（只分配所选的）；胞质：count / sum / 平方和 = 细胞 − 核区域（契约 3），**min / max 直接累加**；核汇总另记每个细胞的核数与各核在细胞内的像素数（用于平均 / 最大）。
  3. **内存上界（新，审核 P0）**：
     - **通道分组**：按一个累加器预算（初值 1.5 GB，可调、不作契约）把通道分成若干组，每组各扫一遍（每遍重读标签，本机约 0.8 s；原始通道的总读量不变）；小数据集一组就够，与现在一遍相同。
     - **FeatureMatrixSink**：每组扫完立刻收尾，把这组通道的各「区域 × 统计」列写进磁盘上的临时 float32 矩阵（zarr，运行输出目录下的 `*.partial` 里），然后释放这组累加器。
     - 写 h5ad 时一次只从 sink 读一个 layer 的一个行块。契约：**任何时刻都不在内存里同时持有全部所选 layer 的稠密副本**。
  4. **h5ad（`<base>.h5ad`，一个文件，一行一个主对象）**：
     - 有细胞的运行：一行一个有像素的细胞，`obs` 索引为 `cell_id` 的字符串；纯核运行：一行一个有像素的核，索引为 `nucleus_id`，只有 Nucleus 一个区域。`uns["primary_object"]` 与 `uns["primary_compartment"]` 为 `cell` 或 `nucleus`。
     - `X` = 主区域在 mean → sum → std → min → max 里第一个被选的统计量；`uns["X_statistic"]`。
     - `layers["<区域>_<统计>"]`：所选的每个组合各一张，**包括 X 的那一张**。
     - `obs`：`cell_id`（或 `nucleus_id`）、形态（S4-1 的 15 列，勾选时）、核汇总（勾选时）：`n_nuclei`、`nuclear_area`、`nuclear_area_mean`、`nuclear_area_max`、`nuclear_fraction`、`cytoplasm_area`（都按「在细胞内的核像素」计；新运行上等于核的全部像素）。
     - `var`：通道名（OME 名）为索引，列 `slide_index`、`decision`、`source`、`correction_method`、`correction_param`。
     - `uns`：`statistics`、`expression_regions`、`morphology_version`、`perimeter_definition`、`seam_merge`（运行的 `label_store` 里那一节，旧运行写 `absent`）、`provenance_json`（完整来源记录的 JSON 字符串）；另外仍写 `_provenance.json`。
     - 数值：计算全程 float64，只在写 X / layers 时转 float32；obs 的编号与计数用整数，坐标与形态用 float64。
     - 写法：先写骨架（X、obs、var、uns），再逐个 layer 按行块写入；全部经 `*.partial`，写完才改名。
  5. **CSV 可选（默认不勾）**：勾选时按行块从 sink 流式写出 `<base>.csv`；全细胞列仍用 S4-1 的 `<通道>_<统计>`，核 / 胞质列用 `<通道>_<区域>_<统计>`。
  6. **输出范围问卷**（替换 `Intensity Statistics` 框；几个默认展开、可折叠的分组，标题行左边一个 ▾ / ▸ 箭头，只在 `ui/step4_page.py` 里实现）：
     - `Statistics`：Mean、Sum、Std dev、Min、Max（默认全选；至少一个）；
     - `Expression regions`（只作用于表达量）：Whole cell（主区域，必选、不可取消；纯核运行显示为 Nucleus）、Nucleus、Cytoplasm——没有核时后两项不可选并写明原因；
     - `Features`：Expression（必选）、Morphology、Nuclear summary（需要核）；
     - `Outputs`：h5ad（必选）、CSV（默认不勾）。
     - 有核的运行默认勾上 Nucleus、Cytoplasm、Morphology、Nuclear summary；只计算勾选的组合。下面一行写出输出文件名与「X = cell mean」。
  7. **写出的事务**：h5ad、CSV、来源记录与 sink 都经 `*.partial`；任一步失败，删除本次已写 / 已改名的全部文件与 sink。
  8. **批处理**（用户裁定：CSV 只在用户勾选时输出）：批处理对话框的统计项旁加一个 `Also write CSV` 勾选框，**默认不勾**，交给 worker 的 `write_csv`；默认只写 h5ad。
  9. **环境**：`fusion_mesmer` 里 `pip install --no-deps anndata==0.11.4 array-api-compat==1.15.0`，用 `scripts/export_fusion_mesmer_env.sh` 更新 `envs/fusion_mesmer/` 的清单。接受 `lock_hash` 改变带来的那一行 Step2 警告，把「引擎身份不应因装一个 Step4 的文件格式库而改变」记为设计债务（advisory）。
- **白名单**：`core/quant_sources.py`（读核标签与对应表）、`core/quant_engine.py`（核 / 胞质累加、通道分组、sink）、`workers/feature_extract_worker.py`（h5ad / CSV 写出）、`ui/step4_page.py`（问卷）、`ui/batch_step4_dialog.py`（只加 `Also write CSV` 勾选框并交给 worker）；测试：`tests/test_quant_sources.py`、`tests/test_quant_engine.py`、`tests/test_step4_worker.py`、`tests/test_step4_page.py`（S4-1 建的，按新行为改），`tests/test_batch_step4_dialog.py`（只在上面那一处需要时）；`scripts/verify_step4_s42.py`（新，不被产品导入）；`docs/benchmarks/step4/s42_<日期>.md`（新）；环境清单 `envs/fusion_mesmer/`；`UI_SURFACE_RULES.md`、两份用户指南、本文档。
- **不改的范围**：Step0–Step3、Step2（N3 已完成）、viewer、`core/step3_masks.py`、`core/io_loader.py`、`core/bg_correction.py`；分布统计与周长 / 圆度（S4-3）；Rust / GPU。
- **风险**：
  - 科学输出：全细胞的值必须与 S4-1 逐位相同（回归锁定）。
  - 通道分组时多扫几遍标签（每遍约 0.8 s）；分组数只由预算与数据量决定，写进来源记录。
  - 装包改变环境（回退：卸载两个包、恢复清单）。
  - 写 h5ad 绕过 anndata 的整对象写法（骨架 + 逐个 layer）：验收以 `anndata.read_h5ad` 读回为准。
- **验收门**：
  - **引擎（合成）**：核 / 胞质的 count、min、max 与逐像素的 float64 参考完全相同，sum / mean / std ≤ 1e-12；胞质的极值是直接累加的（核里的极值不会出现在胞质里）；构造「核像素不在它的细胞里」「细胞被抹掉、核仍在」两种旧运行的情况：结果符合逐像素定义、没有负数、计数正确；多核细胞的核汇总；**通道分组数不同（1 组 / 每通道一组）时结果逐位相同**；分块 / 批 / 线程的不变性同 S4-1；不勾的区域不分配累加器。
  - **内存（审核 P0）**：用合成的累加器模拟 **50 万个对象 × 29 通道 × 3 个区域 × 5 项统计**的收尾与 h5ad 写出，峰值常驻内存 **< 3 GB**，并报告实际峰值；代码审查确认没有同时持有全部 layer 的稠密副本。
  - **h5ad**：`anndata.read_h5ad` 读回：X 等于所选主区域统计（只勾 std 时 X = cell_std）；layers 名称与内容（与 float64 结果的相对差 ≤ 1e-6、没有 inf）；obs / var / uns 齐全；空标签不在其中；纯核运行一行一个核、`primary_object = nucleus`。
  - **CSV**：不勾不写；勾选时全细胞列与 S4-1 逐字符相同。
  - **事务**：写骨架、写 layer、写 CSV、写来源记录、改名任一步失败，不留任何本次的输出、sink 与 `*.partial`。
  - **页面（离屏）**：分组默认展开、可折叠；没有核的运行核相关项不可选并有原因；纯核运行主区域为 Nucleus；Whole cell、Expression、h5ad 不可取消；至少一个统计量；文件名与 X 的说明随勾选变化。
  - **真实数据**（N3 之后重跑的带核运行，以及一个 N3 之前的运行）：全细胞值与 S4-1 逐位相同；核 / 胞质与独立的整图参考一致；`nucleus_pixels_outside_their_cell`：新运行 0、旧运行如实报告；总时间与峰值内存；h5ad 能被 anndata（如装了 scanpy，也用 scanpy）读回。
  - **反向注入**：至少——胞质 min / max 用相减或用全细胞的值、核区域不检查 `nucleus_to_cell[nucleus] == c`、X 不按约定顺序取、layers 漏掉 X 的那一张、空标签进入 h5ad、CSV 不勾仍写出、改名中途失败留下部分文件、没有核的运行仍允许勾选 Nucleus、sink 之外又保留一份全部 layer（内存门应变红）。
  - **回归**：S4-1 的 19 个模块 + `test_batch_step4_dialog.py`，与 HEAD 逐条对比。
  - **真机（用户）**：N3 之后的带核运行：问卷默认项、运行、用 anndata / scanpy 读回 h5ad；无核运行：核相关项不可选。
- **用户裁定（2026-09-27）**：1 允许定量，但提示有风险；2 同意（1.5 GB / < 3 GB）；3 同意装 anndata；4 同意，**CSV 只在用户勾选时输出**（Step4 页面与批处理都默认不勾；批处理加一个勾选框）。申请提交推送后开始实施。
- **请用户裁定**（v2 原文）：
  1. **N3 之前的带核运行**：允许做核 / 胞质定量（逐像素定义保证不出负数），来源记录与页面提示「这个结果做于接缝修复之前，接缝上有 N 个核像素不在自己的细胞里，建议重跑 Step2」（建议）；或者对这类运行禁用 Nucleus / Cytoplasm。
  2. **内存门**：累加器预算初值 1.5 GB（超出就分组多扫），50 万对象模拟的峰值门 < 3 GB。
  3. **装 anndata**：按上面的清单安装并更新清单，接受那一行 Step2 警告（设计债务记为 advisory）。
  4. 其余沿用 v1 的建议：X 与 layers 用 float32、layers 里也放 X 的那一张、~~批处理加 `write_csv=True`~~（改为勾选框，默认不勾）、周长 / 圆度放到 S4-3。

- **执行记录**（2026-09-27，未提交）：
  - 环境：`fusion_mesmer` 里 `pip install --no-deps anndata==0.11.4 array-api-compat==1.15.0`；`scripts/export_fusion_mesmer_env.sh` 更新 `requirements-pip.txt`、`environment.yml`（导出脚本顺带把 `prefix` 改成本机路径，已还原）；`README.md` 的 pip 包数 216 → 218。
  - `core/quant_sources.py`：`QuantJob` 增加 `nucleus_path`、`table_path`、`n_nuclei`、`seam_merge`、`has_nuclei`（细胞运行且保留了核）；核数组的形状 / dtype 与对应表长度 M + 1 不符就拒绝；`JobReader.nuclei()` 与 `nucleus_table()`。
  - `core/quant_engine.py`：区域（主对象 / 核 / 胞质，逐像素定义）；`Geometry` 与每个通道组的 `ChannelAcc`（只分配所选区域与统计用得到的表）；内核的通道任务多了核 / 胞质两个分支（胞质的 min / max 直接累加，count / sum / 平方和 = 细胞 − 核）；**通道分组**（`accumulator_budget` 1.5 GB，超出就分组多扫）；`FeatureMatrixSink`（zarr，每个 layer 一张 (对象, 通道) 的表）；`QuantFinalizer.expression` **逐个 layer 生成**、写进 sink 后立即释放（实施中改的：一次返回一组的全部 layer 时，50 万对象会同时持有约 1.7 GB）；核汇总；`QuantResult`（id 列、obs、sink、layers、`nucleus_outside`）；第一遍顺带统计每个核在细胞内 / 外的像素数。
  - `workers/feature_extract_worker.py`：`run_extraction(regions, features, write_csv)`；`_write_h5ad`：anndata 写骨架（X、obs、var、uns），再用 h5py 逐个 layer、按行块写入；CSV 按行块从 sink 流式写出；sink 放在输出目录的 `.<base>_features.partial`，与所有 `*.partial` 一起在成功 / 失败 / Stop 时清理，失败时删掉本次已改名的文件（新建的空输出目录也删掉）；**勾选 CSV 时 sink 用 float64**（实施中发现：从 float32 读会让 CSV 与 S4-1 在第 6 位有效数字上不同），h5ad 仍写 float32；来源记录增加 `primary_object`、`expression_regions`、`features`、`X`、`layers`、`nuclei`（含 `nucleus_pixels_outside_their_cell`）、`seam_merge`、`channel_groups`、`sink_dtype`。
  - `ui/step4_page.py`：`What to quantify` 问卷（四组，箭头折叠；Whole cell / Expression / h5ad 固定勾选且灰；没有核时 Nucleus / Cytoplasm / Nuclear summary 灰并在 tooltip 写原因，有核时默认勾；纯核结果主区域显示为 Nucleus）；输出文件与 `X = …` 一行；**接缝修复之前的带核结果**选了核相关项时显示橙色 `Risk:`，不阻止运行；完成对话框列出文件，旧结果另写受影响的核像素数。**实施中发现并修正**：运行前重新解析作业会把问卷重置成默认勾选（用户取消的 Cytoplasm 又被勾回）——改为只在换了运行 / 区域时套用默认值。
  - `ui/batch_step4_dialog.py`：`Also write CSV` 勾选框（默认不勾）交给 worker。
  - 测试：`tests/test_quant_sources.py` 29 条（合成项目可带核，含两种旧运行缺陷）、`tests/test_quant_engine.py` 29 条（核 / 胞质对逐像素参考、胞质极值是自己的、旧运行缺陷不出负数且计数正确、核汇总、通道分组 1 组 / 每通道一组逐位相同、不勾的区域不分配、纯核运行、需要核的项在无核运行上拒绝、float32 sink ≤ 1e-6、X 取第一个统计量）、`tests/test_step4_worker.py` 19 条（单一 h5ad 的结构与读回、X 跟随统计量、纯核一行一个核、旧运行定量并记录风险、CSV 只在勾选时写且与 h5ad 一致、CSV 保持全精度而 h5ad 为 float32、写 CSV / h5ad / 来源记录或改名失败不留输出）、`tests/test_step4_page.py` 16 条（分组展开与折叠、无核变灰、有核默认勾、固定项、纯核主区域、输出行随勾选变化、旧运行的风险提示、页面按所选范围运行且不重置用户的选择）；`tests/test_batch_step4_dialog.py` 未改、6 条通过。
  - 反向注入 12 处：胞质极值用全细胞的、核区域不查对应表、X 不取第一个统计量、layers 漏掉 X 的那一张、空标签进入 h5ad、不勾仍写 CSV、改名失败留下已改名的文件、无核运行可勾 Nucleus、运行时重置用户的选择、CSV 从 float32 读、通道分组共用一列累加器——各使至少 1 条测试变红；「收尾先把所有 layer 攒在内存里」由内存门抓到（峰值 4.42 GB > 3 GB）。
  - 真实数据与内存门：报告 `docs/benchmarks/step4/s42_2026-09-27.md`——全范围 9.9 s、峰值 1.76 GB；与独立参考最大相对差 6e-8、NaN 位置一致；接缝修复之后的运行核错位 0，之前的运行 457 px / 38 个核；只做全细胞并勾 CSV 时与 S4-1 的 CSV 逐字符相同；50 万对象 × 29 通道 × 3 区域 × 5 统计：峰值 2.81 GB（门 < 3 GB）。
  - 文档：`UI_SURFACE_RULES.md` 的 Step4 条目（问卷、风险提示、批处理的勾选框）；两份用户指南的 Step4 一节（区域定义与用途、单一 h5ad 的结构、CSV 可选、旧结果的风险）。
  - 回归：S4-1 的 19 个模块（Step4 五个模块、`test_main_window_step1_5`、`test_step0_step1_handoff_contract`、`test_step3_masks`、Step0→Step1→Step2 交接与会话的各模块）+ `test_ui_surface_contract.py`，每模块单独进程，与 `git archive HEAD`（`afda34a`）逐条对比：本侧 20 个模块**全部通过**，无新增失败。
  - **真机验收通过（用户 2026-09-27）**：接缝修复之后的带核运行 `seg_20260927_194616_stardist_nuclei_expansion`，输出 `cell_features.h5ad` 与来源记录（默认不写 CSV）。
  - 真机中发现（与 S4-2 无关，另立块）：该运行右上角有一个 414 万像素的巨大「细胞」（标签 7592）。4 × 5 网格重跑并保存每块的原始预测证实：它是 StarDist 在几乎全是背景的切块（tile_004）上直接输出的星凸多边形（像素数与最终标签完全相同）；tile_004 / tile_006 另有 3 个同类巨大标签因碰到内部窗口边被 N3 排除。重跑时还撞上了运行资源监控器的 `NameError`（已知 advisory）。

- **v1（已被 v2 取代）** 的全文见修订记录 v3.86 对应的提交 `c7f43c7`（与 v2 的主要差别：两个 h5ad（细胞表 + 核表）、没有输出内存上界、问卷第二组叫 `Compartments`）。

## 六、未决与 advisory

### 后续计划（用户 2026-09-26 排定；都未启动，每块须单独申请）
0. **顺序（用户 2026-09-27；Step4 之后的部分按独立审核的建议，用户同意）**：
   ① 块 N2（所有计算了核的方法保留核 mask；LabelStore 数据契约，对标未来的 Step4）→
   ② Step3 剩余的第 ③ 步（patch 按钮条组件化 + patch 空降）→
   ③ Step4 改为流式定量（逐块读细胞 / 核 zarr 与通道，按细胞累加 count、坐标矩、强度和与平方和、最小 / 最大、边界计数，最后一次算出面积、质心、形状、均值、标准差等；胞质可由细胞合计减去核合计得到；中位数 / 分位数列为高级慢速统计，默认不开；周长 / 边界的分块接缝规则在 Step4 开工时定；以后可能用 Rust 重构）→
   ④ 必要的 WSI 阻塞项清理：禁止整图展开（细胞 mask 与 DAPI 的 OME 导出改为流式、按需导出）；Step4 改读 zarr 后默认删除临时 `.dat`；Step2 通道缓存改为按字节预算（现在 `SharedChannelStore(max_cache_items=32)` 按条数）→
   ⑤ TMA 基础 →
   ⑥ QualityMask（模糊 / 折叠检测）→
   ⑦ TMA 批处理 →
   ⑧ 用真实数据做性能剖析 →
   ⑨ Rust 重构 viewer / Step2 的基础。
   另有来自同一意见、尚未排入上面序列的：自适应切块规划（按实测峰值显存调整，目标 70–80 %）、ResourceGovernor（统一 viewer / Step2 / Step4 的资源预算）、引擎通信改为共享内存 / 固定的内存映射环形缓冲、更深的 NGFF——在 ⑧ 的剖析结果出来后再定。
1. ~~Step2：旧路径 ROI 模式中途 Stop 仍登记成功~~（**已由块 K 修好**，块 M 之后 8 个方法都走引擎）。
2. **Step3 重设计**（调查与裁定见第五节「Step3 重设计」）：块 N、2a–2c、S5、④a–④c 已完成；剩第 ③ 步（patch 按钮条组件化 + patch 空降）。ROI 在 Step3 冻结、patch 可编辑并全局同步（块 S5 的裁定取代了早先的「可画 ROI 不产生下游影响」）。
3. **项目 / 会话架构**（最后做）：打开别的项目并切换一切（S2 的调查结论见块 S）；切换数据集后 Step2 仍留着上一个项目的 fused.zarr 和参数路径、正在跑的分割不停止——这一条随会话恢复一起治理。
4. **Step4 优化**（用户 2026-09-26 排定，在 Step3 之后）：Step2 每次运行留下两个未压缩的临时内存映射 `global_mask_<ROI>.dat`、`global_dapi_<ROI>.dat`（本项目一次运行 955 MB + 478 MB，占运行目录约 95%），另有重复的 `global_mask_*.ome.tiff`（float32）和 `global_dapi_*.ome.tiff`。Step4 旧代码仍把 `global_mask.dat` 当作 mask 路径的备选（`ui/main_window.py:4313/4322`、`workers/feature_extract_worker.py:70`、`ui/batch_step4_dialog.py:78`），须先让 Step4 改读 zarr，再清理这些文件。
5. **长期**（Step4 优化完成后再议，用户 2026-09-26）：原始切片与 Step0 / Step1 的图像是否改用 OME-NGFF 多级 zarr（Odon 的数据结构）。评估：原始切片本身已是 1/4/16 金字塔（512 分块、LZW）；可能的收益是 LZ4 解压更快、更多粗层级（大切片的总览）；代价是一次转换（本机约一两分钟、多占约一倍磁盘，全切片更久）和所有读取方（Step0/1 viewer、Step2、Step3、Step4）改为支持 zarr，属于 P0 规则须单独审批的 Viewer 读取与缓存范围。建议先用现有基准测出瓶颈（解压 / 合成 / 上传）再立项。
6. **暂缓**：Mesmer 3 个方法的真机验收（本机无模型，DeepCell token 申请网站不可用）；U2（`step1_fusion_settings.json` 并入会话）。

### 用户裁定（2026-09-26）
- **HQ / HQ2 / CDS 这类不经 Step1 交接的方法不再维护。** 它们在新界面本来就不可见（R2）。此后各块不为它们修缺陷、不为它们补测试，也不把它们放进验收门；只有这些方法自身的测试失败不阻塞其他块——共用路径（例如两个切块循环、归属与合并）上仍维护方法的回归照常算失败。代码暂不删除；删除须另行申请。

### 已知的已有问题（advisory，未排期）
- **StarDist 同一输入多次运行偶发不一致**（块 M 回归中确认，HEAD 上已有）：偶尔约 0.5% 像素的标签差 1，使逐像素相等的测试（`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call` 等）时过时不过。
- **Cellpose 用 CPU 极慢**：本机 Cellpose-SAM 在 CPU 上一个 256×256 内部块约 230 s；全图（15437×16215）按此外推约两周。Use GPU 关闭只适合作为兜底或很小的 ROI。
- 「旧路径 Stop 不能立即停止」已由块 M 解决（8 个方法都走引擎子进程，约 0.2 秒停止）；原调查结论见块 M 之前的修订记录。
- **运行资源监控器在判定「推理退回 CPU」时崩溃**（块 K 回归中发现，2026-09-26）：`utils/runtime_resource_monitor.py:336` 的 `_cpu_fallback_reasons()` 用到 `likely_gpu_inference`、`gpu_peak_util`，它们只是 `diagnose()` 的局部变量，抛 `NameError`。只在 `likely_cpu_fallback` 为真时走到（推理期间 GPU 显存增长 ≥128 MB、GPU 利用率 <10%、CPU ≥70%）；Step2 在收尾 `_finish_runtime_monitor()` 时调用，此时输出已写完，运行却以错误结束、不登记。离屏 4 个并行进程时可复现；真机上 GPU 繁忙或推理退回 CPU 时可能遇到。未修，等用户裁定。
- 用户主动 Stop 后，Step2 用标题为「Error」的对话框报告「Stopped by user.」。
- Step1 读交接时把 Step0 界面上的「Output」输入框改写为 `<roi>/step1`（`_set_gui_work_dir`）；之后在 Step0 直接 Load，可能把 step1 当成项目根目录、在其下再建 `rois/`。
- 仓库的测试写保护只保护 `config.py` 里的两个路径，不保护 `~/fusion_data`；在真实项目上做探测时只能用复制件。
- 「没有任何组时勾选的通道进不了 fusion」（块 F 修好后 `Load weights` 走不到这个状态）。
- 离屏 1500 宽窗口下 Step0 与 Step1 的通道列宽不一致（HEAD 上一样）。
- 本机（WSL2、RTX 3060）上与 HEAD 相同的失败：`test_step1_channel_panel.py::test_the_weight_row_and_the_buttons_kept_their_look`、`test_global_channel_dock.py::test_the_step0_panel_looks_like_the_baseline_panel`、`test_step0_step1_display_isolation.py::test_step0_work_does_not_make_step1_load_or_redraw`（字体/几何差异），`test_step1_montage_view.py` 在第 27 条后 Qt 中止（WSL 的 GPU/EGL），5 个 GPU 模块不收集测试。

- A0 各项产出（见块 A0）。
- 同一个 patch 的核分割阶段复用：块 C 的第二步，另行申请。
- Step2 的重复参数界面：本计划只保证「不作编辑直接运行时与所选组合一致」，不重做它的界面。
- 附录中的已有失败与本计划无关，不在范围内。但它们**不能豁免**本计划涉及路径上的任何失败。

---

## 七、块 A0 产出（v3，2026-09-23，待用户逐条确认）

核查基于 `e655409`，没有改生产代码。诊断脚本都放在会话 scratchpad，只读：
- `a0_step2_handoff_probe.py`：⑧，Qt offscreen，只写临时目录；
- `a0_tissue_mask_probe.py`：⑤，**只读**打开真实切片的 overview 层；
- `a0_mesmer_both_probe.py`：②，合成图，`fusion_mesmer` 环境。

三次运行前后，`cufile.log` 都保持 4449898 B；真实切片的 size 和 mtime 也没有变。

v3.1 起，各条的裁定结果标在原位，汇总见 7.9。「设计选择」是我的建议，可以改。7.10 块 V 的 V1–V4 已裁定。

### 7.0 A0 查出的、影响全计划的事实

1. **Mesmer 在真机环境里跑不起来。**
   - 用户正在运行的程序用的是 `fusion_test2`：进程 `python -m block01_v14.main`，exe 为 `/root/micromamba/envs/fusion_test2/bin/python3.10`。这个环境装了 cellpose 4.1.1 和 stardist 0.9.2，**没有 deepcell**。
   - deepcell 0.12.10 只在 `fusion_mesmer` 环境里，而这个环境**没有 stardist**。
   - 目前没有一个环境能同时跑这 8 个方法。Step1 在 `fusion_test2` 里选 Mesmer，会直接走 `status.mesmer_available=False` 报错。
   - **GPU 实测**（2026-09-23，驱动 535.309.01）：
     - `fusion_test2`：TF 2.21 是 CPU 版（`is_built_with_cuda=False`），torch 能用 CUDA。
     - `fusion_mesmer`：TF 2.8.4 是 CUDA 版，但看不到 GPU；torch 能用 CUDA。
     - 所以目前**只有 Cellpose 真正用上了 GPU**。
     - StarDist 每个任务都会先起一个 GPU 子进程。这个子进程报「TensorFlow sees no GPU」失败后，再起一个 CPU 子进程重跑（`workers/cellpose_worker.py:236-372`，两次都用 `sys.executable`）。
     - Mesmer 只能跑 CPU。
   - **F1 裁定（审核，2026-09-23）**：
     - Mesmer 保留在界面上。当前环境没有 deepcell 时置灰，并明确显示「缺少 deepcell」。
     - 运行环境**不是 advisory**：在块 C 的 Mesmer 验收之前，必须另立环境块解决（见 7.10 块 V）。否则不能宣称 8 个方法已经交付。
   - **v3.9 状态**：`fusion_mesmer` 已经同时具备三个引擎，用户已在真机上验证主程序可以运行（7.10.1）。按 F1 的要求，块 C 的 Mesmer 验收仍然以 V0 通过为前提。
2. **Mesmer 的阈值目前没有接入。**
   - `utils/mesmer_utils.py:331 run_mesmer_prediction` 调用 `app.predict` 时，只传了 `image_mpp`、`compartment`、`batch_size`，没有传 `postprocess_kwargs_*`。
   - 要让 Mesmer 的阈值可以写成列表（R3），就必须改这一处调用，Step1 和 Step2 两条路径都要改。
   - **P1 裁定**：同意把这项加进块 C 的范围（见 7.1）。

### 7.1 ① 8 个方法的参数表

**来源**：
- 默认值：`utils/segmentation_config.py:23-314`；
- worker 读参：`workers/cellpose_worker.py:568-577`、`:297-307`、`:713`，以及 `workers/mesmer_worker.py`；
- Step2 控件范围：`ui/step2_page.py:388-460`、`:855-870`。

**精度约束**（⑧ 实测）：
- Step2 的 `QDoubleSpinBox` 默认只有 2 位小数。3 位小数的值会被四舍五入：0.375→0.38，-0.125→-0.13，0.475→0.47，0.325→0.33。
- Step2 的 diameter 上限是 300，Step1 的是 500，超过 300 会被 Step2 截断。这一条是静态阅读得出的，Qt `setRange` 的行为，没有实测。
- **设计选择**：新弹窗的范围和精度与 Step2 控件一致，也就是按下表校验。这样块 E 不用改 Step2 就能保证一致。Mesmer 阈值是例外，见下。

**可列表参数**。「auto」表示传 `None`，由库自己决定，可以作为列表里的一项。

| 方法 | 参数 | 类型 / 范围 / 精度 | 默认 | 说明 |
|---|---|---|---|---|
| Cellpose ×3 | `diameter` | float，0–300，1 位小数；0 = auto | auto（`None`） | cpsam 自动估计 |
| | `flow_threshold` | float，0–3，2 位小数 | 0.4 | |
| | `cellprob_threshold` | float，-6–6，2 位小数 | 0.0 | |
| StarDist ×2 | `prob_thresh` | float，0–1，2 位小数；auto | auto（模型自带） | 只有非 None 时才传给 `predict_instances` |
| | `nms_thresh` | float，0–1，2 位小数；auto | auto | 同上 |
| StarDist expansion | `expand_distance` | float，0–200，1 位小数 | 8 | 只有 expansion 方法有这个参数；StarDist nuclei 没有 |
| Mesmer ×3 | `maxima_threshold` | float，0–1，3 位小数 | whole-cell 0.075；nuclear 0.1 | 见下 |
| | `interior_threshold` | float，0–1，3 位小数 | 0.2 | 见下 |

**Mesmer 的「主要阈值」定为 `maxima_threshold` 和 `interior_threshold` 两个。**
- 依据：deepcell 0.12.10 的 `deepcell/applications/mesmer.py:273-290`。`deep_watershed` 的后处理参数里，只有这两个是阈值：前者决定种子，后者决定前景。
- 其余参数单值隐藏，沿用库的默认值：`maxima_smooth=0`、`interior_smooth=2`、`small_objects_threshold=15`、`fill_holes_threshold=15`、`radius=2`。
- 实测：合成图上把两个值调到 0.3 / 0.5，细胞数从 173 变成 151，说明阈值确实起作用。
- 3 位小数是因为 whole-cell 的默认值 0.075 本身就是 3 位。
- 这两个值要能到达 Step2，前提是：
  - 块 C 在 `run_mesmer_prediction` 里增加 `postprocess_kwargs` 的透传；
  - 块 E 让 Step2 带上这两个值。Step2 没有对应控件，目前它们只能靠 `get_seg_config()` 里的 `data = dict(self._seg_config)` 留在顶层，⑧ 实测是这样；`params` 里没有，也没有代码读取。
- **P1 裁定（同意）**：块 C 的范围扩大，接入 `maxima_threshold` 和 `interior_threshold`。明确包括以下几处：
  - `utils/mesmer_utils.py:331 run_mesmer_prediction`：透传 `postprocess_kwargs_whole_cell` 和 `postprocess_kwargs_nuclear`；
  - `workers/mesmer_worker.py`：Step1 预览和 Step2 tile 两条路径；
  - Step2 保留这两个参数：进入 `params`，并随 `get_seg_config()` 一起提交；
  - Step2 实际执行时透传。
  - **验收门**：测试要证明两件事。第一，参数到达了 DeepCell `app.predict` 的 kwargs。第二，结果确实改变，用合成图上的细胞数或 mask 差异来证明。
- **P2 裁定（同意）**：
  - Mesmer whole-cell 和 nuclear-guided 的列表阈值**只作用于细胞输出**（`postprocess_kwargs_whole_cell`）；
  - Mesmer nuclei 的列表阈值作用于核输出（`postprocess_kwargs_nuclear`）；
  - nuclear-guided 的副核输出用库的默认值。
  - 这个语义要在 `+` 弹窗的参数说明里写清楚，结果记录和参数文件的 metadata 里也要写，比如 `threshold_target: "whole_cell" | "nuclear"`。

**单值参数**（弹窗里显示，只接受单值；默认值来自注册表）：
- Cellpose：`min_size`，int，1–10000，默认 15。注意 worker 里是 `int(x or 15)`，所以 0 会变成 15，校验下限定为 1。`model_type` 固定为 cpsam，不显示。`use_gpu`、`tile_size`、`batch_size` 不显示，用默认值。
- Cellpose expansion：`expand_distance`，0–200，1 位小数，默认 8。**只允许单值。**
  - P3 裁定：R3 只授权了 StarDist 的 expand 可以写成列表，没有授权 Cellpose 的。
- StarDist：`model_name`，默认 `2D_versatile_fluo`；`device_preference`，不显示。
- Mesmer：
  - `image_mpp`，0.01–10，3 位小数，默认 0.5。本切片 OME 记录的像素尺寸是 0.5069 µm；
  - `postprocess_min_size`，int，≥0；
  - `use_gpu`、`batch_size`、`tile_size`、`overlap` 不显示。
  - **v3.7 起不再显示**：
    - `nuclear_channel`、`membrane_channels`、`input_mode`：输入固定下来，膜通道只能用 Fusion（7.11.1、7.11.5）。按方法区分：
      - whole-cell 和 nuclear-guided：`[fusion 核通道, fusion]`；
      - **nuclei：`[fusion 核通道, 0]`**，第二个通道为 0。
      - 参数文件和共用的构造函数都要按这个区分执行；
    - `normalize_input`、`percentile_low`、`percentile_high`：这是应用侧的那一次多余定标，要删掉（7.11.5 第 3、4 处）。
    - 这些键仍然写进参数文件，值固定为：`input_mode="step1_weighted_fusion"`，`normalize_input=False`。这样 Step2 按现有的 `fused_zarr` 路径执行，并且不再做那一次多余的拉伸。具体键值在 V1/C 实施时，对照 Step2 的读取代码最终确定。

**列表校验**（设计选择，块 B 的验收门按这条写）：
- 用逗号分隔，去掉空格；
- 每一项都在范围内，精度不超过上表；
- 重复值去重后保留原顺序，并提示；
- 空列表不能保存；
- 「auto」只能出现一次；
- 单值参数不接受逗号。
- 注意：这里的去重是**单个参数列表内**的去重，和 R9 说的「不做展开后的过滤去重」不冲突。

### 7.2 ② 8 个方法的输出表（更正 4.5）

| 方法 | 细胞 mask | 核 mask | 依据 / C 需要做的 |
|---|---|---|---|
| Cellpose whole-cell | ✓ | —（置灰） | 无 |
| Cellpose nuclei | — | ✓ | 无 |
| Cellpose nuclei + expansion | ✓（扩张后） | ✓（扩张前） | `cellpose_worker.py:578-585` 直接覆盖了核标签，要在 `expand_labels` 之前保留一份 |
| StarDist nuclei | — | ✓ | 无 |
| StarDist nuclei + expansion | ✓（扩张后） | ✓（扩张前） | `:711-715`，处理同上 |
| Mesmer whole-cell | ✓ | **见 O1** | |
| Mesmer nuclei | — | ✓ | 无 |
| Mesmer nuclear-guided | ✓ | ✓ | 目前算了两次推理（`mesmer_worker.py:193-208`），核的结果被丢弃，没有放进队列 |

**核查结果**：
- **Mesmer 模型每次推理都同时输出 whole-cell 和 nuclear 两个 head。** `compartment` 只决定后处理的是哪一个（`deepcell/applications/mesmer.py:136-150`）。
- 合成图实测（CPU）：`compartment='both'` 返回 `(1, H, W, 2)`，其中第 0 层和单独调用 `whole-cell` 的结果**逐像素相同**，第 1 层和单独调用 `nuclear` 的结果**逐像素相同**。
- 这次 CPU 计时没有体现出节省（both 3.39 s，whole-cell 1.71 s，nuclear 1.61 s，受首次调用影响），所以**不声称能提速**。
- 现状：
  - nuclear-guided 的细胞 mask 与 Mesmer whole-cell 完全相同，因为 `method_to_mesmer_mode` 对二者都返回 `whole_cell`；
  - nuclear-guided 只是多做一次 nuclear 推理；
  - Step2 的 `get_seg_config` 对 nuclear-guided 写的是 `compartment: "whole-cell"`，注册表里写的是 `"both"`。这个字段目前没有代码读取，只是记录在这里。

**O1 裁定（同意）**：Mesmer whole-cell 只显示细胞 mask，核开关置灰。

**O2 裁定（同意）**：保留现在的两次调用，只把已经算出来的核 mask 回传。本计划不做 `compartment="both"` 优化。

**三种状态**（块 C 实施）：
- `not_produced`：方法本身没有这种输出，开关置灰；
- `ok` 且 `count = 0`：成功，但零细胞；
- `failed`：运行失败，附错误信息。

另外增加 `cancelled`：Stop 时还没执行的任务记为这个状态（见 7.7）。

### 7.3 ③ 任务格式、结果记录格式、结果文件布局

**组合 ID**：
- `combo_id = sha256(canonical({method, params}))` 的前 12 位。
- 规范化复用 `_step1_config_hash` / `_canonical_step1_config_value`（`ui/main_window.py:7255-7281`）：浮点数保留 6 位，并去掉时间类字段。
- `params` 只包含方法注册表里的键，去掉 `device_used` 等运行后才写入的字段。

**任务**（进程之间传的内容，全部是可 pickle 的普通类型）：
```
{task_id, run_id, combo_id, method, params,     # params 已展开，不含列表
 patch_bbox: [y0,y1,x0,x1], patch_label: "P3"}  # label 只用于显示和日志
```
- 按方法族分组派发，这一点在块 C 实施：
  - Cellpose / StarDist 进 `run_cellpose_process`；
  - Mesmer 进 `run_mesmer_patch_preview`。
- 现在的 worker 本来就按每个任务的方法分派（`cellpose_worker.py:486`、`:559`、`:692`）；只有「选哪个进程入口」是按第一个任务决定的（`main_window.py:6463-6469`）。
- 所以要改的是 `_launch_worker` 的分组，以及两个 worker 回传内容的改造。

**结果记录**（每个任务一条；队列里只传这条记录，不传 mask）：
```
{schema: 1, run_id, task_id, combo_id, method, params,
 patch_bbox, patch_label,
 source: {pixel_key, manifest_digest, manifest_path, raw_ome_path},   # 见 7.4
 fusion_settings_hash,
 status: ok|failed|cancelled, error: "",
 cell:    {status: ok|not_produced, path, count},
 nucleus: {status: ok|not_produced, path, count},
 device, runtime_s, created_at}
```

**文件布局**（设计选择）：
```
<step1_dir>/presegmentation_runs/<run_id>/
    run.json                       # 点 Run 时冻结的快照，见 7.4
    records/<combo_id>__<bboxkey>.json
    masks/<combo_id>__<bboxkey>.cell.npy      # uint32，level-0 分辨率
    masks/<combo_id>__<bboxkey>.nucleus.npy
```
- `<bboxkey> = y0_y1_x0_x1`。
- `run_id = YYYYmmdd_HHMMSS_<4 位十六进制>`。
- 同一次 Run 里，`(combo, bbox)` 唯一；不同 Run 在不同目录。因此现有缺陷 2（npz 互相覆盖，`cellpose_worker.py:733-737` 的文件名里没有组合信息）不会再出现。
- `<step1_dir>` 就是现在的 `OUTPUT_DIR`（`main_window.py:2290`、`:2746`），和 `segmentation_params/`、`patch_preview_results/` 在同一目录。这样现有缺陷 4 也解决了。
- 旧的 `patch_preview_results/` 保留，新路径不写那里。
- 方案文件放在 `segmentation_search_plans/`（4.3），和结果目录分开。
- **L1 裁定（同意）**：旧 Run 目录不自动删除。新的一次 Run 开始时，只从视图里清掉旧结果。磁盘清理另立任务。
- **原子发布**（E1 裁定的要求，块 C 实施）：
  - mask 文件先写成 `*.tmp`，再用 `os.replace` 原子替换成正式文件名；
  - 结果记录**最后**发布，同样先写临时文件再原子替换；
  - 记录发布成功后，才往队列发送这条记录。
  - 这样只要一条记录存在，它引用的 mask 文件就一定完整。

### 7.4 ④ 来源绑定、运行快照、运行中编辑

**已有身份**：
- `_handoff_identity()`，`ui/main_window.py:6700-6731`，给出 `manifest_path`、`manifest_digest`、`channel_remap_config_hash`、`handoff_schema_version`、`source_identity`、`raw_ome_path`。
  - `manifest_digest` 是对整个 manifest 做的 hash；时间类字段不算在内。
  - **Step0 每次发布都会变**，包括只改几何的发布（增删 patch 时 `geometry_revision` 和 `n_patches` 会变，`core/step0_handoff.py:291-293`）。
- 视图 provider 的 `source_identity()`（`ui/step1_viewer_host.py:75-90`）：只改几何的发布不会让它变化，而且它只在视图里有效。**不采用。**
- `fusion_settings_hash`（`main_window.py:6750`）只对 `fusion_config` 和 `display_mapping` 做 hash，**和几何无关**。

**问题**：如果用 `manifest_digest` 判断结果是否过期，那么在 A2 随机生成 patch，或者手画一个新 patch，都会让**所有**已有结果变成过期，哪怕那些 patch 的像素完全没变。

**S1 裁定（同意采用 pixel_key，定义按审核意见修订）**：记录里 `pixel_key`、`manifest_digest`、`fusion_settings_hash` 三个都保存，各管一件事：
- `pixel_key`：判断**像素是否过期**；
- `manifest_digest`：只用于审计追溯，不参与过期判断；
- `fusion_settings_hash`：独立校验，和 pixel_key 分开比较，不并入 pixel_key。

**pixel_key 的定义**：它和 **patch 列表无关**，不能笼统地说成「和几何无关」，因为分析 ROI 本身属于几何，而且会影响像素。
- 做法：先生成一个**结构化**的身份字典，再用 `_step1_config_hash` 做规范化哈希。**不从 `identity_token()` 字符串里解析后删字段。**
- 至少包含：
  ```
  {raw:        {dataset_path, dataset_fingerprint},         # manifest.source_identity
   roi:        {bbox_fullres, polygon_fullres | None},      # 当前分析 ROI；full_wsi 两项都为 None
   remap:      channel_remap_config_hash,
   channels:   {<ch>: {decision: original|<corrected 决定>,
                       product: None | {shape, dtype, correction_method,
                                        roi_name, source_identity, written_at}}},
   handoff_schema_version}
  ```
- **排除**：patch 列表、P 编号、`n_patches`、只改几何时的 `geometry_revision`、`patch_config_path`，以及各种发布时间和时间戳。
- **取值来源**（块 C 实施，不改 viewer）：
  - `raw`、`remap`、`handoff_schema_version`、各通道的 `decision` 取自 manifest，也就是 `_handoff_identity()` 已经读出的那份（`core/step0_handoff.py:220-283`）；
  - `roi` 取自当前 `_active_roi`，或 manifest 的 `roi_config`；
  - `product` 由块 C 的新代码用 `utils/calibration_source.open_corrected_channel_array` **只读**打开 corrected 数组，读取它的 shape、dtype 和 attrs。字段和 `viewer/step1_source.py:_product_token` 用的一致，但是结构化的。
  - 不调用、也不修改 `Step1SourceTable` 的私有成员。
- patch 本身由 `patch_bbox` 标识。bbox 变了，就是另一个 patch。
- **块 C 的门**（在 HEAD 导出树上要先确认会红）：以下几种情况，pixel_key 必须**变化**：
  - 重新生成 corrected 产物；
  - 改某个通道的 original/corrected 决定；
  - 改分析 ROI 的 polygon；
  - 改 remap。

  以下几种情况，pixel_key 必须**不变**：
  - 增加或删除 patch；
  - 只改几何的发布；
  - 单纯重新发布。

**点 Run 时冻结的内容**（写入 `run.json`，内存里也保留一份）：
- 勾选的 `patch_bbox` 列表，以及它们当时的 P 编号；
- 展开后的任务表，也就是方法、组合和 `combo_id`；
- committed fusion snapshot 的 `hash`、`fusion_config` 和 `display_mapping`。现在的 `_launch_worker` 已经只从 snapshot 取值（`main_window.py:6424-6444`），保持不变；
- `source`，也就是 pixel_key 和 `_handoff_identity()` 的相关字段。

**运行中编辑**（继承 4.4，补充以下几条）：
- 正在运行时，Run 按钮禁用。现在 `_launch_worker` 在进程还活着时会**静默返回**（`:6419`），新界面要把这个状态明确显示出来。
- 运行中增删 patch、编辑方法块、保存 Fusion，都不影响已经派发的任务，只作用于下一次 Run。
- 迟到的结果按 `(run_id, combo_id, patch_bbox)` 归位，不看当前的 P 编号。
  - Step1 `_on_patches` 在 patch 数量变化时会清空 `_seg_preview_history`（`main_window.py:4740-4746`）。新的结果存储不能挂在这个按位置编号的结构上。
- 结果的 `pixel_key` 或 `fusion_settings_hash` 和当前不一致时，照常显示，但标为过期，不能选为最终。
- **任何结果都不会自动成为选中项。**
  - 现有缺陷 1 的位置在 `_record_segmentation_preview_result`（`:3128-3151`）：只要结果的 `_phase != 1`，它就会写 `self._p2_params`。
  - 轮询代码随后设置 `_params_source="patch_preview"`，并调用 `_check_save_unlock`（`:6541-6543`）。
  - 这几处在块 C 里一起改。

### 7.5 ⑤ 组织掩膜、「空白超过 40%」、ROI 包含判定

**确认：没有可以复用的组织掩膜。** 全仓搜索过 tissue、otsu、foreground、background_mask、fill_holes、TMA。同名的东西都是别的用途：
- `core/tissue_compose.py` 只做显示合成；
- `core/bg_correction.py:236 _safe_otsu` 只算 SNR 标量；
- `workers/hq2_marker_segmentation.py:201` 是细胞级的二值化；
- `utils/mesmer_utils.py:280 postprocess_mask` 处理的是细胞 label。

所以要在 `core/random_patches.py` 里新写（A2 白名单已经包含这个文件）。

**算法（设计选择，已在真实切片上只读验证）**：
1. **读取**：`OMETIFFLoader.read_region_lowres(DAPI, 0,H,0,W, loader.overview_downsample(), normalize=False)`（`core/io_loader.py:132`），走 TIFF 金字塔。
   - 真实切片是 59040×35520，`ds=64`，overview 为 923×555。
   - 读全部 29 个通道共 4.2 s（实测）；只读 DAPI 的耗时没有单独测。
2. **信号**：`log1p`，再按 p1–p99.5 做稳健归一化。
3. **平滑、阈值、形态学**：`gaussian(σ=2 px)` → Otsu → `closing(disk(4))` → `binary_fill_holes` → `remove_small_objects(200 px)`。约 0.56 s。
   - 这组数值是 A0 可行性核查时在 ds=64 上用的。正式定义改用 level-0 单位，见下面的 T1、T2。
4. **组织掩膜**就是上一步的结果。在 ds=64 的 overview 上，1 个像素对应 64×64 个 level-0 像素。
5. **空白比例** = 1 −（候选 patch 覆盖范围内组织像素的占比）。空白比例 > 0.4 的候选直接丢弃（R1）。计算方式见 T2。

**实测（真实切片）**：
- 直接对 DAPI 做 Otsu，也就是**不允许的定义**：只有 **15.1%** 的像素算作组织。核之间的间隙全被当成空白，任何 patch 都会被判为空白超过 40%。
- 上面的算法：只用 DAPI 时组织占 **71.8%**，用全部通道取最大值时占 **72.2%**，两者 **98.9%** 的像素一致，所以**默认只用 DAPI**。
- 对照图在会话 scratchpad 的 `tissue_probe/side.png`（左边是直接 Otsu，右边是本算法）：轮廓贴合组织外缘，核之间的间隙被算作组织。

**T1、T2 裁定（按审核意见修订）**

**T1：只填小洞，洞的面积阈值用 level-0 面积来定义。**
- `binary_fill_holes` 会把大腔隙（血管、撕裂）也算作组织。改成只填面积小于 `max_hole_area_l0` 的洞，大腔隙仍然算空白。
- 所有形态学参数都用 **level-0 像素单位**定义，在所选的掩膜层级上按 `ds` 换算：
  - 长度 ÷ ds；
  - 面积 ÷ ds²。
- 像素尺寸确认之后，同时记录对应的 µm² 值。
- A0 在 ds=64 上用过的值，换算成 level-0 分别是：

  | 参数 | ds=64 上的值 | level-0 值 |
  |---|---|---|
  | σ | 2 px | 128 px |
  | closing 半径 | 4 px | 256 px |
  | 最小组织块 | 200 px | 819 200 px² |
  | 洞面积阈值 | 500 px | 2 048 000 px² |

- 这些都是**这张切片上的像素空间经验值，不是最终常量**。A2 实测之后才能固定（见 T2 最后一条）。
- 不能直接固定为「500 个 mask 像素」，因为换一个金字塔层级，同样的像素数对应的面积就不同。

**T2：每次随机生成，只建一张定义固定的组织掩膜。不对每个候选窗口单独归一化、单独做 Otsu。**
- 如果每个窗口各算各的，同一个组织位置会因为候选窗口不同而得到不同的判断。
- **层级**：根据 patch 的最短边，选一个足够细的**现有**金字塔层，目标是最短边至少覆盖约 32 个掩膜像素，也就是 `ds ≤ 最短边 / 32` 时取最大的那一层。例如 512 px 的 patch 用 ds=16。
  - 层级只来自 TIFF 已有的金字塔（`read_region_lowres` 会取 ds 不超过所请求值的最粗一层），不另外重采样。
- **范围**：一次性算出整个生成范围的掩膜。生成范围是：有 ROI 时为 ROI 的 bbox，没有 ROI 时为整张切片。
- 同一批候选**共享**同一组归一化、阈值和形态学参数，这组参数按 T1 从 level-0 换算过来。
- **组织占比**：在掩膜上建积分图（`cumsum` 两次），每个候选的组织像素数用 O(1) 查表得到。候选边界和掩膜像素不对齐时，按覆盖面积加权；在 A2 的门里用合成图验证这一点。
- **先实测，后定阈值**：A2 开工后，先在真实切片上实测，报告给用户，再固定常量。实测内容：
  - 所选层级；
  - 掩膜内存；
  - 读盘和计算的耗时；
  - 组织外缘的表现（出对照图）。
- 内存估算（按算术，**未实测**）：ds=16 时整张切片约 3690×2220 ≈ 8.2 M 像素，float32 约 33 MB；ds=8 时约 131 MB。
- 生成记录里写明：掩膜层级 `ds`、level-0 单位的参数、Otsu 阈值和随机种子。
- 合成图上的验证（核之间有间隙的组织不被判为空白）放在 A2 的验收门里，本块只做了真实切片的只读核查。

**ROI 包含判定**：
- **现有工具**：
  - `cv2.fillPoly` 栅格化，在 `ui/step0/overview_panel.py:411`、`ui/step0/search_ctrl.py:2167`；
  - 射线法点判定 `OverviewPanel._point_in_polygon`（`overview_panel.py:2033`，overview 坐标，UI 类的 staticmethod）；
  - Step1 只做 bbox 包含判定（`main_window.py:4540 _patch_inside_roi_bbox`）；
  - 仓库里没有「矩形完整落在多边形内」的判定。
- **设计选择**：在 `core/random_patches.py` 里写纯函数 `rect_inside_polygon(bbox_l0, polygon_fullres)`，用 level-0 坐标精确判定：
  - 矩形 4 个角都在多边形内（偶奇射线法）；
  - 并且多边形的每条边都不与矩形内部相交（线段与矩形求交）。
  - 这对凹多边形是精确的。
  - 不依赖 UI 类，也不引入 shapely。
- **坐标约定**：`polygon_fullres` 是 `(x, y)`，bbox 是 `(y0, y1, x0, x1)`（`overview_panel.py:2135-2149`）。函数内部统一换算，测试里要覆盖。
- 没有多边形的 ROI（`polygon_fullres=None`，full_wsi）：只按组织判定。有 ROI 但没有多边形时，按 `bbox_fullres` 矩形判定。
- 有 ROI 时，候选只在 ROI 的 bbox 内抽取，而且**同样要满足空白比例不超过 40%**。R1 的空白规则对两种情况都适用。

**写入 Step0 的正式 patch**（A2 实施，只调用现有接口）：
- `OverviewPanel.add_patch_rect(y0,y1,x0,x1, roi_idx)`（`overview_panel.py:2310`）每调用一次发一次 `patches_changed`。之后走 `_on_patches_changed` → `_reconcile_roi_edit` → `_persist_geometry_edit` → `GeometryPersistWorker` → `commit_geometry_only` → `geometry_committed` → Step1 `_on_patches`（`step0_page.py:737-742`、`:4610`、`:5189`、`:5237`；`main_window.py:2068`）。
- 连续调用 N 次时，持久化任务会合并成最后一次（`geometry_persist_worker.py:91-102`），结果正确。
- 前提：Step0 至少发布过一次 handoff，否则只存在内存里（`step0_page.py:4648-4676`）。
- **注意**：
  - `add_patch_rect` 不会自动计算 `roi_idx`，要传入；
  - Step1 会把 patch 过滤到 ROI 的 bbox（`main_window.py:4548`）；
  - 增加 patch 会改变 `manifest_digest`，这正是 7.4 建议改用 pixel_key 的原因。

### 7.6 ⑥ montage 的显示供给

**复用接口**（只调用，不修改）：用整张切片 viewer 现在的合成链，而不是旧的 patch 预览链。
1. `spec = build_spec(window._display.fusion, window._display.state, mode, scope="step1")`（`ui/step1_draft_spec.py:95`）。
   - 它已经包含 Channels 勾选、Overlay/Fusion 模式、Intensity 映射 `mappings={ch:(lo,hi,gamma)}`，以及 fusion 权重和颜色。
2. 需要读的通道：`viewer/step1_compose.py:84 overlay_channels` 和 `:94 fusion_channels`。
3. 层级：用 `viewer/request_planning.py:45 pick_display_level` 按 montage 自己的缩放来选，用 `:116 bbox_to_level` 换算坐标。
4. 像素：`window._step1_mount.host.stack.provider.read_region(ch, level, y0,y1,x0,x1)`（`ui/step1_viewer_host.py:148`）。
   - 它是同步调用，返回 float32，缺失的地方是 NaN。
   - 它会遵守 original 和 corrected 的决定、corrected 的 coarse 平面，以及 ROI 裁切。
5. 合成：`viewer/step1_compose.py:193 compose(mode, tiles, **spec)`，纯 numpy，不依赖 Qt，不带缓存。
6. `missing_windows` 里的通道，调用 `window._display.request_mapping_seed(ch)`（`ui/block01_display.py:1964`）。这是现有的共享服务；映射到达后会发 `state.mapping_changed`，montage 收到后重新合成。

**不采用旧的 patch 预览链**，原因有三：
- 整张切片 viewer 在屏幕上时，它不会填缓存（`main_window.py:5706` 提前返回）；
- 没有映射的通道，它会退回 patch 百分位或 `loader._norm`，画出来和 viewer 不一样；
- 它读的是全分辨率，不用金字塔。

**缓存归属**：montage 自己持有两层 LRU，**不借用、不扩展** viewer 或 scheduler 的缓存。
- **授权边界**：按 `AGENTS.md` 第 4 条，**新增这两层缓存和 montage 工作线程，属于块 D 必须明确授权的范围**。它们不涉及修改 viewer 或 scheduler，但块 D 启动时要由用户明确批准三件事：
  - 两层缓存的容量；
  - 缓存和线程的生命周期，也就是下面的释放时机；
  - 取消和关闭的门：切换数据集、离开 Step1、关闭窗口时，线程都要退出，缓存都要清空，而且没有迟到的回调。
- 下面的数值是建议，不是已经批准的值。
- **通道块**：键为 `(patch_bbox, level, channel, pixel_key)`，值为 float32，建议上限 1 GiB。Intensity、模式或勾选变化时，不用重新读盘，只需重新合成。
- **合成结果**：键为 `(patch_bbox, level, spec_hash)`，值为 RGBA uint8，建议上限 256 MiB。
- 读取和合成在 montage 自己的工作线程里执行，最新请求优先；GUI 线程只上传图像。

**对现有缓存的影响**（已核实）：
- 直接调用 `provider.read_region`，**不经过** `TileScheduler.request`，所以不会写入或挤掉 viewer 的 raw 512 MiB 缓存和 corrected 2 GiB 缓存（`viewer/scheduler.py:206`、`:532-539`）；
- 不会碰合成 LRU，也不会碰 GPU 纹理；
- 唯一会被填充的是 `Step1SourceTable` 的元数据备忘，也就是打开的数组句柄，不含像素。

**线程安全（advisory）**：
- `Step1SourceTable` 的惰性填充没有加锁（`viewer/step1_source.py:252-258`、`:325-348`）。
- scheduler 的多个 tile-io 线程已经在这样共用它，montage 线程只是多一个同类调用方，不引入新的风险类型。
- 块 D 的门里加一条「viewer 和 montage 同时读取不出错」的实测。如果发现问题，停下来申请，不擅自给 source table 加锁。

**释放时机**（继承 4.6）：
- 切换数据集，或 `pixel_key` 变化：清空；
- 离开 Step1：清空；
- 某个 patch 被取消勾选：清掉这个 patch 的条目；
- 新的一次 Run 开始：只清结果图层，底图缓存保留，因为像素没变；
- Channels 或 Intensity 变化：只清合成层。

**不需要改 viewer、scheduler 或已有的缓存层。** 但 montage 自己新增的两层缓存和工作线程，要作为块 D 的明确授权项申请（见上文「授权边界」），不能算作「不需要申请」。

**step5_v8 参考（更正 R6 的前提描述）**：
- step5_v8 的 montage viewer 是**浏览器 WebGL**（`deepseek/step5_v8/agentic/montage_viewer_web.py`），不是 Qt。
- 它的底图是预先拼好的整张 montage，patch 尺寸统一；分隔线是一个栅格通道；mask 是栅格的填充图；画布上**没有矢量轮廓**，也**没有文字标签**。
- 本计划借用的是它的思路：一张画布、一个相机、一张布局表、分隔线作为独立图层、按行列命中判定。
- 本计划不同的地方：
  - patch 尺寸不一，采用按行装箱的布局；
  - 底图实时合成；
  - mask 用 cosmetic `QPen` 画矢量轮廓（4.6，块 D 实测）；
  - P 编号是独立图层。
- 这些都是本计划自己的设计，参考里没有现成实现。

**坐标**：mask 是 level-0 分辨率，底图是 level-k。每个 patch 的图元都要设置 level-k → 画布的变换。轮廓路径直接用 level-0 坐标乘以画布缩放，不跟随底图层级。

### 7.7 ⑦ 选定资格规则（E1、E2 已裁定）

**状态的含义**：
- `ok`、`failed`、`cancelled` 都是**运行终态**：任务不会再有结果。
- `pending`、`running` 是非终态。
- **E2 裁定**：`cancelled` 是终态，但会让整个组合**不具备选定资格**。

一个组合可以被选为最终结果，需要**同时**满足以下五条（**E1 裁定**：同意主体规则，并补上第 2 条的文件完整性条件）：
1. 在它所属 Run 的**冻结 patch 集合**里，每个任务都已到达终态，而且没有一个是 `cancelled`。
2. **文件完整**：这些任务的结果记录，以及记录要求的 mask 文件，都已经完整发布。也就是说，`status=ok` 的输出，`path` 必须存在，并且是原子替换后的正式文件（7.3 原子发布）。有记录缺失或文件缺失的组合，不能选。
3. 至少有一个 `ok`。`ok` 且零细胞也算 `ok`。
4. 不过期：`pixel_key` 和 `fusion_settings_hash` 分别都和当前一致。过期的结果**可以查看，但不能选定**。
5. 有 `failed` 的 patch 时，按原草案处理：控制栏里标出失败数，选定前弹窗提示「k/n 个 patch 失败」，用户坚持就可以选。

其他规则：
- 选定之后再运行新的 Run，旧的选定保留。但如果它变成过期，就自动取消选定，Save 重新禁用。
- 基本约束随块 C 落地（4.7）：没选组合时 Save 禁用；结果到达不会自动选中；hash 不一致时拒绝 Save。
- 现有的 `_params_match_committed_settings`（`main_window.py:6681-6696`）比较的是 `_p2_params["fusion_settings_hash"]`。**Step1 Save 写出的参数文件里没有这个 hash**，`cpcfg` 由显式的键构造（`:8057-8069`）。
  - 块 E 建议把 `fusion_settings_hash` 和 `pixel_key` 写进参数文件，只作追溯用。
  - Step2 不读这两个字段，这条不改 Step2 的行为。

### 7.8 ⑧ Step2 交接实测

**方法**：
- 脚本 `a0_step2_handoff_probe.py`，Qt offscreen，不在 DISPLAY :1 上弹窗。
- 对 8 个方法，各用非默认值构造一个「所选组合」，经过 `normalize_segmentation_config` → `save_segmentation_params`，写入临时目录。这是现有交接契约，也是 4.7 规定的写出格式。
- 然后走公开路径 `MainWindow._go_to_step2()`（`main_window.py:3913` 起，`:3924-3940` → `step2.load_step1_active_params`）。
- 最后读 `step2.get_seg_config()`，也就是 `step2_page.py:2268` 实际提交给执行器的内容，逐个键比较顶层和 `params` 两处。
- 这条路径和现有测试 `tests/test_step1_to_step2_handoff.py` 的做法一致。

**结果**：

| 方法 | 方法名一致 | 所选参数一致 |
|---|---|---|
| Cellpose whole-cell（d 17.5，flow 0.35，prob -0.5） | ✓ | ✓ |
| Cellpose nuclei（d 12，flow 0.6，prob 0.5） | ✓ | ✓ |
| Cellpose nuclei + expansion（同上 + expand 5） | ✓ | ✓ |
| StarDist nuclei（prob 0.55，nms 0.35） | ✓ | ✓ |
| StarDist expansion（同上 + expand 6） | ✓ | ✓ |
| Mesmer ×3（maxima、interior、mpp 0.65） | ✓ | mpp ✓；**maxima 和 interior 只在顶层，`params` 里没有**，而且目前没有任何代码读取（见 7.0 第 2 条） |
| 精度探针：flow 0.375，prob -0.125，sd prob 0.475，nms 0.325 | ✓ | **✗，被四舍五入为 0.38、-0.13、0.47、0.33** |

**结论**：
- 在 7.1 的范围和精度之内，Cellpose 和 StarDist 这 5 个方法从 Step1 Save 到 Step2 `get_seg_config()` **一致**，块 E 不需要改 Step2 的装载。
- 缺口有两处：
  - 超出精度或范围的值，由块 B 的弹窗校验挡住；
  - Mesmer 阈值，按 P1 由块 C 和块 E 处理。
- **测量范围**：只测了「交接文件 → Step2」这一段，没有测 Step1 `_save` 自身怎样组装 `cpcfg`。
  - 现有的 `_save` 对非 Cellpose 方法也会把 `diameter`、`flow_threshold`、`cellprob_threshold` 写到顶层（`:8062-8069`）。
  - 新界面的 Save 在块 E 里重写组装逻辑，块 E 的端到端门会覆盖这一段（8 个方法各一次）。
- 另记：`_apply_seg_config_to_ui` 对 Mesmer 强制把 `tile_size` 和 `overlap` 设为 0，`get_seg_config` 再转成 `None`（`step2_page.py:1665`、`:1769`、`:1799-1800`）。
  - 所以 Step1 的 Mesmer `tile_size=2048` 和 `overlap=128` 不会到达 Step2。
  - 这是 Step2 有意的设计（控件已禁用，并提示「from Step2 Tile Grid」），而且 7.1 已经把它们定为不显示，不算缺口。

### 7.9 裁定汇总（独立审核，2026-09-23）

| 编号 | 裁定 | 落在哪里 |
|---|---|---|
| F1 | Mesmer 保留在界面上，缺少 deepcell 时置灰并明确提示；环境问题**另立块 V**，块 C 的 Mesmer 验收以块 V 为前提 | 7.0、7.10 |
| R10–R14 | whole-cell 输入 `[fusion, fusion, DAPI]`；用户窗口和权重全局生效，模型自动定标按局部做，每个引擎只执行一套标准预处理；Step1 采用 HALO；权重在两边按相同规则参与构造（N1 选 a）；一套轮子、方法模块化。这几条是用户裁定。H1、H2 已裁定；R11 已修订为「局部自动定标，应用侧不额外拉伸，每个引擎只执行一套标准预处理」，I0–I3 作废 | 二、7.11、7.12 |
| V1–V4 | v3.9 改为一个环境 `fusion_mesmer`，每个引擎一个子进程（只做进程隔离）；micromamba 按平台锁定；Step2 分步接入；顺序为 V0 → V1/C → V2 → E | 7.10 |
| P1 | 同意扩大块 C：`mesmer_utils`、Mesmer worker、Step2 保留参数、实际执行透传；测试要证明参数到达 DeepCell kwargs，并且改变结果 | 7.1 |
| P2 | whole-cell / nuclear-guided 的阈值作用于细胞，nuclei 的阈值作用于核，副核输出用默认值；界面和 metadata 里写清楚 | 7.1 |
| P3 | Cellpose `expand_distance` 保持单值 | 7.1 |
| O1 | Mesmer whole-cell 只显示细胞，核开关置灰 | 7.2 |
| O2 | 保留两次调用，只回传核 mask；不做 `both` 优化 | 7.2 |
| L1 | 旧 Run 不自动删除；磁盘清理另立任务 | 7.3 |
| S1 | 采用 pixel_key：和 patch 列表无关，结构化生成后再哈希；manifest_digest 只用于审计；fusion hash 独立校验 | 7.4 |
| T1 | 只填小洞；阈值用 level-0 面积定义，属于经验值，A2 实测后才固定 | 7.5 |
| T2 | 每次生成只建一张固定定义的掩膜：最短边覆盖约 32 个掩膜像素的现有层级，参数共享，用积分图计算；先实测，后定阈值 | 7.5 |
| E1 | 同意主体规则，并加上文件完整性条件，写盘采用原子发布 | 7.3、7.7 |
| E2 | `cancelled` 是终态，但会让该组合不具备选定资格 | 7.7 |
| 另 | montage 的两层 LRU 和工作线程，是块 D 的明确授权项 | 7.6 |

**审核结论**：
- A0 的调查证据可以接受。按上面修订后，A0 定稿。
- A1 可以随后单独申请。
- A2 要等 S1、T1、T2 的定义写实之后才能启动。本版已经写入，待用户确认。
- 文档暂时不提交，没有 push 授权。

### 7.10 块 V：统一运行环境，每个引擎一个子进程（v3.9 按用户裁定由「每个引擎一个环境」改为单一环境；**只有 V0 可以申请启动**）

**动机**：用户提出，每种分割方法彼此隔离，并且能随项目部署到其他电脑上。F1 要求另立的环境块，就用这个方案落实。

**v3.9 变更（用户裁定，2026-09-23）**：
- 用户不想再增加 micromamba 环境，所以改为**一个环境**：`fusion_mesmer`，同时装主程序和三个引擎。
- 隔离只保留在**进程**这一层：每个引擎在同一个环境里各启动一个独立子进程，跑完就退出并释放显存，一个引擎崩溃不会拖垮主程序。
- 放弃的是**依赖隔离**：各个库只能共用同一套版本。比如 deepcell 把 TensorFlow 锁在 2.8，StarDist 也只能跟着用 2.8。
- 下表 V1 的「分 3 个引擎环境」由此作废；V2 中「按引擎各自锁定依赖」改为锁定这一个环境；V3、V4 不变。

**审核裁定**：

| 项 | 裁定 |
|---|---|
| V1 | ~~分 3 个引擎环境~~（v3.9 作废）→ **一个环境 `fusion_mesmer`**，里面有 Cellpose、StarDist、Mesmer 三个**引擎**，每个引擎一个子进程；8 个方法作为引擎内部的配置。 |
| V2 | micromamba，按平台锁定依赖（v3.9 起只锁定这一个环境），引擎作为独立子进程运行。**只提供依赖隔离和进程隔离，不提供文件权限隔离或 GPU 资源隔离。** 下文不再使用「沙箱」一词暗示更强的隔离。 |
| V3 | Step2 最终和 Step1 使用同一套引擎、同一个模型、同一种参数解释。分步接入，**保留 Step2 现有的切块、合并和恢复机制**。 |
| V4 | 协议和环境验证（V0）现在就先做。Step1 接入与块 C 合并（V1/C）。Step2 接入单独成块（V2），在块 E 的全流程验收之前完成。这样可以避免块 V 和块 C 重复改派发代码。 |

#### 7.10.1 已核实的事实与尚未证明的推断

**已核实**：
- 同一个 conda 环境里只能装一个 TensorFlow 版本。原来 StarDist 装在 `fusion_test2`（TF 2.21，CPU 版），deepcell 装在 `fusion_mesmer`（TF 2.8.4，CUDA 版）。
- **v3.9，2026-09-23，经用户批准，已对 `fusion_mesmer` 做了以下改动**（每一步前后的 `pip freeze` 都存在会话 scratchpad 里）：
  - 新增：stardist 0.9.2、csbdeep 0.8.2、numba 0.67.0、llvmlite 0.49.0、PyOpenGL 3.1.10、pynvml 13.0.1、nvidia-ml-py 13.610.43、cucim-cu12 25.6.0、click 8.5.0、nvidia-nvimgcodec-cu12 0.7.0.11；
  - 降级：cupy-cuda12x，从 13.6.0 降到 13.3.0。
  - 没有改动其他任何已有包，`pip check` 显示没有冲突。
- **改动后的核验**（合成数据，同机）：
  - StarDist 在 TF 2.8.4 下的结果，和 `fusion_test2`（TF 2.21）**逐像素相同**；
  - Mesmer 的输出和改动前相同；
  - Cellpose 在 GPU 上正常运行；
  - GPU 背景校正（tophat、cucim）的结果和 `fusion_test2` **逐像素相同**；
  - 离屏跑 Step1→Step2 交接探针，10 个用例的结果和 `fusion_test2` 相同；
  - 用户已在真机上用 `/root/micromamba/envs/fusion_mesmer/bin/python -m block01_v14.main` 测试通过；
  - Mesmer 和 StarDist 在这个环境里只能用 CPU，用户**接受**。
- **修 cupy 时的发现**：cupy 13.6.0 现场编译 `cupyx.ndimage` 时会用到系统里 `/usr/local/cuda-12.2` 的头文件，编译报错（`cuda_fp8.h`：`__nv_bfloat16_raw` 未定义）。结果是 GPU 背景校正悄悄退回 CPU，tophat 的结果最多相差 1.98。降到 13.3.0 后恢复正常。**这说明 GPU 背景校正依赖系统里 CUDA 头文件的版本**，这一条要写进部署要求。
- `fusion_test2` 里 Keras 默认用 torch 后端，StarDist 必须设置 `KERAS_BACKEND=tensorflow` 才能导入。项目代码里已经设置了。
- **在这两个环境里，TF 都看不到 GPU**，只有 torch（Cellpose）能用 CUDA。
- 仓库里没有任何环境描述文件，主程序自己的环境也没有。
- Mesmer 模型路径硬编码在 `utils/mesmer_utils.py:302 _default_mesmer_model_path`，里面有本机的绝对路径 `/sda1/Fusion/benchmark/...`。
- StarDist 在 Step1 走子进程（`workers/cellpose_worker.py:236-372`，用 `sys.executable`），在 Step2 走主进程（`workers/segment_merge_worker.py:1974-1976` 调用 `load_stardist_model`）。两条路径已经不同。

**尚未证明，不能写成理由或承诺**：
- ~~「StarDist 必须用 TF 2.21」没有证明~~：v3.9 已实测 StarDist 在 TF 2.8.4 下可用，结果一致。
- CUDA 版本**不预先写死**（包括 cu121），按各引擎在 V0 实测通过的组合来锁定。
- 老版本 TF 在 RTX 4090 上能否用 GPU，**必须实测**，不能因为缺少 sm_89 就断言靠 PTX 一定能跑或一定不能跑。驱动版本和 GPU 架构是部署约束，要写进部署说明。

#### 7.10.2 部署目标

- **第一版只支持 Linux x86_64。** 目标机器还要满足操作系统、系统库（glibc 等）和 NVIDIA 驱动的兼容条件，具体版本在 V0 实测后写明。
- **不承诺跨平台。** conda-pack 不是跨平台打包工具，要求源平台和目标平台兼容。不承诺同一个包能直接在 Linux、Windows、macOS 上运行。
- 要提供**这一个环境的规格文件和锁文件**，主程序和三个引擎都包含在里面。
- **系统依赖**，写进部署说明：
  - NVIDIA 驱动：本机是 535.309.01，兼容的下限在 V0 里记录；
  - GPU 背景校正需要系统里有 CUDA 12.2 的头文件（`/usr/local/cuda-12.2`），原因见 7.10.1；
  - `KERAS_BACKEND=tensorflow`。
- 验收层级分开说清楚：
  - 在同一台机器上用新用户测试，只能证明不依赖原用户的配置；
  - 它**不能代替**在另一台电脑上的部署验收。
  - 另一台电脑的验收是否需要、何时做，由用户指定机器。

#### 7.10.3 没有隐式回退

- **产品运行时不回退到主环境**。引擎环境缺失，或者引擎身份和结果记录、参数文件里记的不一致时，这个方法**明确不能运行**：界面置灰，显示原因。
  - 旧的进程内代码路径可以保留，作为**开发用的回退**，必须通过显式的开发开关才能启用，产品默认关闭，并在结果记录里标明。
- 部署配置**不自动退回**本机的 Mesmer 绝对路径。
  - 模型文件太大（cpsam 1.2 GB），不放进仓库。仓库里只放**模型清单**：路径、大小、SHA-256。部署时按清单放到约定的位置，并核对校验值；缺失或校验不符就明确报错。
  - 把 Mesmer 的路径改成可配置，属于修改生产代码，放在 V1/C 做；V0 只做记录。
- **CPU 回退**可以作为明确的策略：由引擎配置显式允许，实际设备如实写进结果记录。**CPU 自检通过不等于 GPU 验收通过**，两者分开报告。

#### 7.10.4 输入准备与科学后处理的归属

**原则**：
- 「主程序负责读像素和做 fusion」，指的是**应用侧的后台任务**，不能把大数组的读取和合成搬到 GUI 线程上。
- 引擎进程只负责：模型推理，以及列在下表「引擎侧」一栏里的步骤。
- 每一步只在一侧执行一次。

**现状**（静态阅读，**尚未实测**）。Step1 和 Step2 的输入准备已经不一致：

| 方法 | Step1 输入（`cellpose_worker.py` / `mesmer_worker.py`） | Step2 输入（`segment_merge_worker.py`） | 不一致之处 |
|---|---|---|---|
| Cellpose whole-cell | `fuse_fullres` 的结果 /65535，拼成 `[cyto, cyto, nuc]` 的 3 通道图，`channel_axis=-1`（`:511-527`、`:575-576`） | fused.zarr 切块 /65535，直接传 `[cyto, nuc]` 2 通道图，**不传 `channel_axis`**（`:2003-2015`） | 通道布局不同，有没有 `channel_axis` 也不同 |
| Cellpose nuclei（含 expansion） | `loader.read_region(DAPI)`，默认已归一化，再按 **patch 做 min-max**（`fusion._normalize_intensity`，`:540-545`） | fused.zarr 的第 1 通道 /65535（`:2018`） | 归一化的范围不同：一个按 patch，一个是 fusion 产物 |
| StarDist（含 expansion） | 和 Cellpose nuclei 的 DAPI 相同，再在子进程里做 `normalize(1, 99.8)` | fused.zarr 的第 1 通道，再按切块做 `normalize(1, 99.8)`（`:2132`） | 同上，另外百分位是按窗口计算的 |
| expansion 后处理 | 主进程 worker 里的 `expand_labels`（`:578-585`、`:711-715`） | 切块内的 `expand_labels`（`:2031-2038`、`:2142-2146`） | Step2 在切块边界上扩张，由现有的合并机制处理 |
| Mesmer | `build_mesmer_input(loader, ...)` 按参数里的百分位归一化，再调用 `postprocess_mask` | `run_mesmer_on_channel_source` 或 `run_mesmer_on_fused_tile`，再调用 `postprocess_mask` | 输入来源有两种 |

**块 V 的要求**：
- V0 要为 8 个方法各出一张归属表，列出以下各项分别在哪一侧执行、怎样执行：
  - 输入通道；
  - 数组布局（HW / HWC，通道顺序）；
  - dtype；
  - 归一化（范围、百分位、按什么窗口）；
  - 像素尺寸（Mesmer 的 `image_mpp`）；
  - 模型推理；
  - 扩张（expansion）；
  - 后处理（`min_size`、`postprocess_mask`、`fill_holes` 等）。
- 表中「引擎侧」的步骤只在引擎里执行；「应用侧」的步骤只在应用后台任务里执行。
- 上表里 Step1 和 Step2 的不一致，已由用户裁定 R10–R12 统一处理，契约写在 **7.11**。
  - 统一规则必须在 **V1/C 接入之前**写定，否则 Step1 接完后再改就要返工。
  - V0 只负责记录并复现现有的两条路径，**不自行改变科学处理**。
  - 长期保留两套输入语义并各自记录，**不能**作为预览有效性的最终验收。
- 搬迁时，expansion 和 Mesmer 后处理**不能遗漏，也不能执行两次**。V1 和 V2 的门都要逐项检查。

#### 7.10.5 进程模型与通信协议（第一版保持简单）

- **按引擎串行**：所有引擎进程都用同一个环境里的解释器。一次 Run 里，同一时刻只运行一个引擎进程。当前引擎加载模型，完成自己的全部任务，然后退出并释放显存，再启动下一个引擎。**不同时保留三个模型进程。**
- **协议**：stdin 和 stdout 上传 JSON 行，但 **stdout 只用于协议**，库的日志一律重定向到 stderr 或日志文件，不得混进 stdout。
  - 每条消息都带 `protocol_version`。
  - 消息类型：`hello`（引擎身份、设备）→ `task`（task_id，输入 `.npy` 路径，参数）→ `result`（task_id，记录路径，只在记录原子发布之后发送）| `error`（task_id，错误信息）→ `done`（完成）。
  - **终态登记**：
    - 进程被杀掉或崩溃时，没法保证 runner 还能发出消息。所以规则是：正常执行时，由 runner 用 `result` 或 `error` 回报；异常退出或被取消时，由**应用侧**给每个还没结束的任务登记唯一的终态。
    - 每个任务**有且只有一个**终态，登记之后不能覆盖。
    - 用户主动 Stop 的任务记为 `cancelled`，**不能**被通用的崩溃处理覆盖成 `failed`。应用侧要先记下「这是用户发起的停止」，再去结束进程。
- **取消、崩溃、关闭**：
  - 引擎进程用单独的进程组启动（`start_new_session`）；
  - Stop 或关闭窗口时，先发 `cancel`，超时后对整个进程组依次发 SIGTERM、SIGKILL，确保本应用启动的进程**及其子进程**全部结束；
  - 引擎在没有收到 Stop 的情况下崩溃时，由应用侧把还没完成的任务记为 `failed`，并附上退出码和 stderr 的末尾。
- **引擎身份**和**实际设备**分开记录，两者都写进每条结果记录，也都写进 Step1 保存的参数文件：
  - `engine_identity = {engine, lock_hash, lib_versions, model_checksum, runner_version}`：用来判断引擎是否匹配。`lock_hash` 是这一个环境的锁文件的哈希。不匹配就拒绝运行（7.10.3）。
    - `runner_version` 用 runner 代码的内容哈希。原因是：锁文件和模型都不变时，参数的解释代码仍可能改变。
  - `device_used`（cpu / gpu，以及 GPU 型号）：**不属于身份**，只记录这一次实际用的是哪个设备。
    - 设备变化按已经批准的回退和验收策略处理（7.10.3、7.10.6），不触发「身份不同就拒绝」。
    - 这样「显式允许 CPU 回退」和「身份不同就拒绝」两条规则不会互相冲突。

#### 7.10.6 验收原则：「差异可以解释」不能作为通过条件

- 同环境、同模型、同输入：先核对迁移前后的结果，要求**逐像素一致**。
  - 做法：在旧路径和新引擎进程上，用同一个 `.npy` 输入各跑一次。
- 设备不同（CPU 对 GPU）可能导致结果不完全一致。这种情况要**事先**定义比较指标和接受条件，由用户接受后才算数，不能事后拿解释来代替验收。
  - 例如：label 数目的差、匹配后的 IoU 分布、不匹配对象的比例。
- Step1 的小 patch 和 Step2 的全量切块，**不能仅凭用了同一个引擎就承诺逐像素一致**，因为切块边界、按窗口计算的归一化和合并都会带来差异。
  - 能承诺的只是：方法相同、模型相同、参数解释相同，输入准备按 7.10.4 的表执行。

#### 7.10.7 三个交付阶段

**V0：运行环境验证**（范围已经用户审定；2026-09-23 已启动并执行，结果见 7.10.8，**待用户验收**）

- **目标**：证明三件事：
  1. `fusion_mesmer` 能**照清单重建**；
  2. 三个引擎都能**在独立子进程里、不联网**运行，结果和直接调用**逐像素相同**；
  3. 定下主程序和子进程之间的通信方式，供 V1/C、V2 直接复用。
- **不改**：主程序、现有的分割代码、界面，以及 `fusion_test2` 和 `fusion_mesmer` 这两个环境本身。

**五项工作**：
1. **导出环境清单**，写入仓库新目录 `envs/fusion_mesmer/`：
   - conda 部分：`micromamba env export`，163 个包，锁定版本，另外附一份 linux-64 的 explicit 锁文件；
   - pip 部分：精确版本，约 217 个包。已确认没有从本地路径安装的包；
   - 部署说明：Linux x86_64、驱动、CUDA 12.2 头文件、`KERAS_BACKEND`（7.10.2）。
2. **照清单临时重建，然后删除**（用户同意）：
   - 在临时路径里照清单新建一个环境，和 `fusion_mesmer` 逐项比较包列表，要求完全一致；
   - 在新建的环境里跑一遍第 4 项的检查；
   - **验证完立即删除这个临时环境**。
   - 预计占用约 12 GB 临时磁盘，需要联网，耗时没有实测。
   - 这只证明清单在本机可用，不能代替在另一台电脑上的验收（7.10.2）。
3. **模型清单与断网验证**：
   - 记录三个模型的路径、大小和 SHA-256：cpsam 在 `~/.cellpose/models`，1.2 GB；StarDist `2D_versatile_fluo` 在 `~/.keras/models/StarDist2D`，17 MB；Mesmer 在 `/sda1/Fusion/benchmark/spacec/models/Mesmer_model`，104 MB。
   - 在没有网络的命名空间（`unshare -n`）里启动三个引擎，确认它们都只从本地加载模型。
4. **子进程运行原型**，放进仓库新目录 `seg_runner/`（用户同意），配测试，**主程序不调用它**：
   - 按 7.10.5 的协议实现：版本号、stdout 只走协议、唯一终态、Stop 记为 `cancelled`、进程组清理；
   - **正常运行**：三个引擎各跑一次合成图，结果和同一环境里直接调用**逐像素相同**；
   - **中途 Stop**：已完成的任务保留，其余任务登记为 `cancelled`；
   - **崩溃**：用 `kill -9` 杀掉子进程，应用侧登记为 `failed`，不会卡死；
   - **关闭**：没有残留进程；
   - **实测**：每个引擎的模型加载时间、单个 patch 的耗时、内存和显存占用。
5. **输入归属表**：把 7.10.4 和 7.11 整理成最终表格，列出 8 个方法各自的输入通道、布局、dtype、定标（由哪一侧、做几步）、扩张和后处理，每一项都附代码位置。

**拟新增的文件**（具体白名单在启动时再确认一次）：
- `envs/fusion_mesmer/`：规格文件、锁文件、pip 清单、部署说明、模型清单；
- `seg_runner/`：协议模块和三个引擎的 runner，只依赖 numpy 和对应的引擎库；
- `scripts/`：导出清单和重建验证用的脚本；
- `tests/test_seg_runner*.py`。

**验收门**：
1. 照清单在临时环境里重建成功，包列表和 `fusion_mesmer` 完全一致；重建完已删除。
2. 断网时，三个引擎都能加载模型并完成分割。
3. 子进程里的结果和直接调用的结果**逐像素相同**。
4. Stop、崩溃、关闭三种情况下，终态登记都正确，没有残留进程。
5. 受保护文件不变，两个现有环境不变，主程序行为不变。
6. 新增的测试先在 HEAD 导出树上运行，确认会失败，避免断言写空。

**V1/C：Step1 接入**（和块 C 合并申请）
- 多方法任务按引擎串行派发，细胞和核两种 mask，结果记录和原子发布，取消和关闭。
- 7.2 至 7.4、7.7 的要求都由这一块落地。

**V2：Step2 接入**（单独成块，在块 E 之前）
- 复用同一个 runner。保留 Step2 现有的切块、合并和恢复机制，只替换每个切块的推理调用。
- 验证：参数语义一致；输入语义按 7.10.4 和用户的裁定执行；引擎身份和 Step1 保存的参数文件一致，不一致时拒绝运行。

**V2 执行记录**（代码和提交说明里称「Step2 hook-up」；本节于 2026-09-25 按提交说明和代码补记，第 1、2 步执行时没有同步写进本文档）：
- **用户裁定（2026-09-25；待用户确认的补记）**：
  - 参数文件以版本号区分新旧：顶层有 `preseg_contract` 的是新格式；没有的是旧文件，**继续走旧路径**，行为不变。新格式里版本未知或缺字段一律报错，不猜测、不退回。
  - **裁定 A**：在新的执行路径接通之前，Step2 遇到有效的契约也**拒绝运行**，绝不在旧路径上运行 Step1 交来的结果。
  - 手动模式（参数来源不是 index）的参数属于用户自己，运行时丢掉契约，按旧路径执行。
  - 以上措辞依据 `1adfe4e` 的提交说明和代码注释整理，原始裁定文字没有留存；用户确认前不作为定稿。
- **第 1 步：纯搬迁（`54e825d`，已提交）**
  - `workers/segment_merge_worker.py` 的两个切块循环（`_segment_one_zarr` 和 `run()` 里的全图循环）改为从 `core.label_ownership` 取质心归属和重编号 LUT（`kept_labels`、`ownership_lut`），不再用内联拷贝；HQ 核、HQ2 各层和 QC 行仍走同一张 LUT；Step2 粘贴整个读取窗口的做法不变。
  - 影子对比（merge-policy shadow compare）关闭：删去 5 处调用和两行 `shadow_compare=enabled` 日志，引擎元数据记为 disabled；对比函数本身保留。这对应 7.12「V2：Step2 改为调用；停掉影子对比」。
  - 性能统计注意：选取归属标签的耗时从 `relabel` 阶段移到了 `postprocess` 阶段，跨这次提交不要比较这两项。
  - 门：`tests/test_step2_ownership_move.py`，两个循环（whole-cell、HQ、HQ2；跨切块边界的细胞、空切块）与冻结的旧内联代码逐项相同：主输出、核、HQ2 各层、QC id、HQ2 切块元数据和总数。
- **第 2 步：版本化交接契约（`1adfe4e`，已提交）**
  - `core/preseg_contract.py`（新，无 Qt）：`build`、`validate`、`runner_params`、`mismatches`、`fixed_rules`。契约块（版本 1）包含：`method`；`params`（组合自己的参数加方法的固定规则，Mesmer 为 `normalize_input: false` 和 `threshold_target`）；`halo_px`；`pixel_key`；`fusion_settings_hash`；`preseg_run_id`；`combo_id`；**那一次运行的**引擎身份——取自该组合成功的结果记录，各记录之间以及与 `run.json` 必须一致，从不取保存时所在环境的身份。
  - Step1 Save（选定结果的分支）：只写组合自己的参数（不再混入固定的 `min_size 15`、Cellpose 默认值、`phase1_diameter`），并在写任何文件之前先构造契约，被拒时不留下半成品。旧面板的 Save 不变（测试用本次提交之前的代码钉住它写出的文件）。
  - Step2：装载参数文件时校验契约；运行前再核对一次——控件里的参数与契约不一致按 mismatch 拒绝；一致也按裁定 A 拒绝（提示「新的运行方式尚未接通」）。worker 同样拒绝（双保险）。手动模式丢掉契约。旧文件不受影响。
  - Step2 的 `image_mpp` 输入框改为 3 位小数，和 Step1 编辑器一致（0.325 不再被四舍五入）。
  - 测试：`tests/test_preseg_contract.py`（新）、`tests/test_step1_save_params_file.py`。
- **复核（2026-09-25，另一台机器：WSL2、RTX 3060 Laptop，按锁文件重建的 `fusion_mesmer`，离屏）**：相关 7 个模块（契约、Save 参数文件、归属搬迁、label_ownership、Step1→Step2 交接、preseg_run、preseg 界面）95 passed；没有跑全量回归；该机器没有 Mesmer 模型和真实数据。
- **Q2 核实（2026-09-25）**：`fusion_settings_hash` 是 fusion 配置和 display mapping 的摘要（`MainWindow._fusion_settings_hash`，`ui/main_window.py:7716`）；fused.zarr 的 `config_hash` 另含来源、区域、方法和产物版本等字段（产物身份构造，`ui/main_window.py:8270` 起）。两者**不能直接比较**。fused 数据来源一致性的校验留待块 E 裁定；第 3 步不声称已验证。
- **第 3 步：Step2 切块推理改用 `seg_runner`（申请第四版，2026-09-25 用户批准；经三轮独立审核）**
  - **行为范围**：只有带契约的参数文件走新路径；旧文件、HQ/HQ2/CDS、手动模式行为不变。唯一例外见「关窗」。
  - **白名单**：
    - `workers/segment_merge_worker.py`：backend 初始化的契约分支（启动引擎子进程、核对引擎身份与 HALO）；`_segment_tile` 的契约分支；两个切块循环 `except` 里的契约判断（失败向外传播）；取消退出分支（全图循环和 ROI 外层，只限契约路径）；`stop()`；`run()` 的 `finally` 清理；去掉裁定 A 的拒绝。
    - `ui/step2_page.py`：`_check_preseg_contract`（去掉裁定 A 的拒绝，加 HALO 核对）；新增 `stop_background_jobs()`（只调 `worker.stop()`，返回 worker 是否仍在运行）。
    - `ui/main_window.py`：`closeEvent` 加一个 Step2 分支，按 fusion job / patch loader / overview read 的现有做法「仍在运行则暂缓关闭，500 ms 后重试」。
    - 测试：`tests/test_preseg_contract.py` 只改 `:176`、`:218` 两条临时拒绝测试；新增 `tests/test_step2_runner_path.py`。
    - 本文档。
  - **不改**：`seg_runner/`（包括 client）、`core/preseg_input.py`、`core/label_ownership.py`、切块、归属、合并、恢复、fused.zarr 写入、Step2 控件。不新增 registry 或状态机，只复用现有协议。
  - **输入**：`preseg_input.INPUT_KIND` + `model_input`，与 Step1 同一函数——Cellpose whole-cell `[f,f,n]`；Cellpose/StarDist nuclei 与 expansion 单通道核图；Mesmer whole-cell 与 nuclear-guided `[n,f]`；Mesmer nuclei `[n,0]`。参数取 `runner_params`。
  - **输出映射（Q1 裁定）**：whole-cell、expansion、Mesmer whole-cell 取 `cell`（expansion 只接回扩张后的细胞）；nuclei 类取 `nucleus` 作主输出；Mesmer nuclear-guided 取 `cell` 作主输出，`nucleus` 仍按原 `nuclei` 字段交回。
  - **runner 对接**：临时目录 `runner_io/` 归本次运行所有，每块读完即删，结束、Stop、出错时整目录删除；任务 ID `{out_prefix}_r{r}_c{c}`。
  - **失败**：契约路径的推理失败向外传播到 `run()` 最外层，发出 `error`，不登记结果、不发布成功结果；旧路径保持「零 mask 后继续」。
  - **取消**：契约路径下不再写入取消的那一块、不发布成功结果；ROI 内层返回后外层 `run()` 也退出，不进入汇总、别名写入、`_register_completed_result()` 和 `finished`；沿用 `error.emit('Stopped by user.')` 后返回。之前的切块可能已留下中间文件，如实说明。
  - **Stop 不阻塞界面**：`worker.stop()` 只置标志、调用 `ep.stop()`；引擎还在加载模型时再对进程组发 SIGKILL，不等待。等待都在 worker 线程里（client 每 0.2 s 检查标志后自己 `terminate()`；加载期间 `start()` 读到 EOF 抛 `EngineStartError`，按用户停止处理）。
  - **关窗**：界面线程不等待；worker 未结束时暂缓关闭并重试，不设总超时。**申请例外**：旧路径关窗也会先停 Step2 worker 再关（以前不理会），旧路径推理不能中途打断，可能要等当前切块结束。
  - **验收门**：
    1. 旧文件：无契约时两个循环的输出与改动前逐像素相同，含原有多输出方法的核。
    2. 等价：合成 fused.zarr 上，契约运行的全局 mask 与「同一 runner 逐块跑 + 共用归属函数粘贴」逐像素相同；两个循环都覆盖；nuclear-guided 比对核。
    3. 输入与 Step1 同一窗口的构造一致（见上面的输入表）。
    4. 引擎身份不符、HALO 与 overlap 不一致、参数不一致都拒绝运行并写明原因。
    5. 加载期间 Stop、推理期间 Stop、kill -9 引擎子进程、运行中关主窗口：登记正确，界面线程不阻塞（Stop 调用 < 50 ms），结束后无残留进程；关窗在加载和推理期间验证暂缓关闭，任务恰好已结束时直接关闭也算通过。
    6. 取消：单 ROI、多 ROI 中途 Stop、全图中途 Stop，结果索引里没有本次运行，没有 `finished`，无残留进程。
    7. 8 个方法各用真实引擎跑一次；Mesmer 在缺模型的机器上记为「未验收」，不用 mock 或 skip 顶替。反向注入只用来证明测试有效，不引出新的产品防御要求。
    8. 真机：用户在原机器上完成一次「Step1 选定 → Save → Step2 运行」。
    9. 交付时写明：块 E 之前没有验证 fused 数据来源一致（见 Q2 核实）。
  - **说明**：父进程意外退出时 runner 读到 stdin EOF，会在当前任务返回后退出；这不保证立即无残留，不作为验收依据。
  - **Advisory（不在本块处理）**：旧路径 ROI 模式中途 Stop 同样会汇总并登记成功——现有行为，等用户裁定。
  - **实施中的扩围（用户 2026-09-25 批准）**：Mesmer whole-cell 和 nuclear-guided 的契约文件经 normalize 后 `input_mode` 为默认的 `selected_channels`（离屏实测），worker 会去打开 corrected 通道组并逐块读取。按批准，在 `_validate_mesmer_config` 开头加一个分支：契约路径直接记 `mesmer_input_source="fused_zarr"` 并返回，不打开通道组；旧路径不变。
  - **第 3 步执行记录（2026-09-25，待用户真机验收；代码未提交）**：
    - `workers/segment_merge_worker.py`：
      - `run()`：用 `preseg_contract.validate` 取得契约（裁定 A 的拒绝去掉）；HALO 与 overlap 不同即报错；`finally` 里 `_close_contract_engine()`（正常结束先 shutdown，Stop 或出错时结束进程组；删除 `runner_io/`）；`except` 里 `_ContractStopped` 发 `error('Stopped by user.')`，不写汇总、不登记、不发 `finished`。
      - `_start_contract_engine`：按方法启动 `EngineProcess`，hello 里的引擎身份必须与契约的 `engine_identity` 完全相同，否则报错并写明不同的键；加载期间被 Stop 时按用户停止处理。
      - `_segment_tile_contract`：`INPUT_KIND` + `model_input` 构造输入 → `runner_params` → runner；`cancelled` 抛 `_ContractStopped`，其他非 ok 状态抛错；主输出按 Q1 映射，nuclear-guided 另交回 `nuclei`；输入、mask 和记录读完即删。任务 ID 为 `tile_` + Step2 的 `tile_id`（`{ROI 名}:{序号}` 或序号，非字母数字换成 `_`），每次运行内唯一——与申请里写的 `{out_prefix}_r{r}_c{c}` 字面不同，唯一性要求相同。
      - 两个切块循环的 `except`：契约路径关 scheduler 后向外抛；ROI 外层循环之后：契约路径被 Stop 时抛 `_ContractStopped`。
      - `stop()`：只置标志、调 `ep.stop()`；hello 未到时对进程组发 SIGKILL，不等待。`__init__` 加 `_contract`、`_engine`、`_runner_io` 三个默认值。
    - `ui/step2_page.py`：`_check_preseg_contract` 去掉「未接通」拒绝、加 HALO 核对；新增 `stop_background_jobs()`。
    - `ui/main_window.py`：`closeEvent` 在「close is CERTAIN」之前加 Step2 分支（暂缓关闭、状态栏提示、500 ms 后重试）。
    - 测试：`tests/test_preseg_contract.py` 两条临时拒绝测试改为「放行」「worker 启动契约的引擎进程」；新增 `tests/test_step2_runner_path.py`（36 条）。
  - **第 3 步验收结果**（WSL2、RTX 3060 Laptop、`fusion_mesmer`，离屏）：
    - 门 1 旧路径：不带契约时，HEAD（`6049fe9`，`git archive` 导出到 scratchpad）与改动后，Cellpose whole-cell、Cellpose expansion、StarDist nuclei、StarDist expansion × ROI / 全图共 16 项 mask 和细胞数逐像素相同；HQ/HQ2 的合并由 `test_step2_ownership_move.py` 覆盖。Mesmer 旧路径没有测（缺模型）。
    - 门 2 等价：Cellpose 3 个、StarDist 2 个方法 × 两个循环，全局 mask 与「同一 runner 逐块跑 + 共用归属函数按 Step2 方式粘贴」逐像素相同；**Mesmer 3 个方法 × 2 = 6 条未验收**（本机无 Mesmer 模型，测试显式跳过并注明）。
    - 门 3 输入：8 个方法送进 runner 的数组与测试里按 7.11.4 表独立写出的构造逐元素相同（这条测试替换了引擎进程，只查输入）；Mesmer 契约不打开通道组。
    - 门 4 拒绝：引擎身份不符（真实 StarDist 进程）、worker 和页面的 HALO 不一致都拒绝并写明原因；参数不一致由 `test_preseg_contract.py` 覆盖。
    - 门 5、6 生命周期与取消：加载期间 / 推理期间 Stop（ROI 与全图）、两个 ROI 之间 Stop、kill -9 引擎子进程、运行中关主窗口——Stop 调用 < 50 ms，加载期间 Stop 后 2 s 内结束；没有 `finished`、结果索引里没有本次运行、进程组已不存在、`runner_io/` 已删；关窗第一次被暂缓并登记 500 ms 重试，worker 结束后关闭。
    - 门 7：反向注入 10 处都变红（不核对引擎身份、worker 不核对 HALO、输入种类用错、失败被吞掉、ROI 外层仍登记、加载期间不发信号、Mesmer 仍打开通道组、关窗不暂缓、页面不核对 HALO、结束不关引擎）。「加载期间 Stop 后 2 s 内结束」这条门是为了让「不发信号」能被测出而加的。
    - 相关模块回归（23 个模块）：420 passed / 3 failed / 6 skipped；3 条失败都在附录基线清单里，HEAD 上同样失败。`cufile.log` 不变。没有跑全量回归。
    - 门 8 真机（2026-09-25，原机器已不可用，改在本机 WSL2 上做；数据 `~/fusion_data/cropped_region.ome.tif`，29 通道、uint8）：
      - 「Step1 选定 → Save → Step2 运行」跑通（用户确认）；
      - 中途 Stop 立即停止，并能再次启动。用户第一次以为不能再启动，实际是「Stopped by user.」对话框在 WSL 下弹在屏幕最左上角、没有看到，属操作问题，不是缺陷；
      - 运行中关主窗口：未做真机验收（离屏测试已覆盖）；
      - **Mesmer 3 个方法未验收**：DeepCell 申请 token 的网站不可用，本机没有 Mesmer 模型；用户同意暂时跳过。
    - 第 3 步已提交：`dcaca2c`。
    - 门 9：块 E 之前**没有**验证 fused 数据来源一致（Q2）。


#### 7.10.8 V0 执行结果（2026-09-23，待用户验收）

**新增文件**（都没有接入主程序）：
- `envs/fusion_mesmer/`：
  - `conda-linux-64.lock`：161 个包，带 md5；
  - `requirements-pip.txt`：216 个包，精确版本；
  - `environment.yml`；
  - `models.json`；
  - `README.md`：部署说明。
- `scripts/`：
  - `export_fusion_mesmer_env.sh`：导出环境清单；
  - `rebuild_fusion_mesmer_env_check.sh`：照清单临时重建，比对后删除；
  - `model_manifest.py`：写入或核对模型清单。
- `seg_runner/`：
  - `protocol.py`：消息格式、原子写盘；
  - `engines.py`：三个引擎；
  - `runner.py`：子进程入口；
  - `client.py`：父进程侧的启动、派发、终态登记和清理；
  - `selftest.py`：自检；
  - `synthetic.py`：合成输入。
- `tests/test_seg_runner.py`：13 条测试。

**验收门的结果**：

| # | 验收门 | 结果 |
|---|---|---|
| 1 | 照清单临时重建 | conda 部分 161 个包逐条相同；pip 的 216 个版本锁定全部满足；`pip check` 没有冲突。耗时 3 分 20 秒（包缓存是热的），体积 12 GB。重建出的环境里自检全部通过，13 条测试全部通过。**已删除。** |
| 2 | 断网 | 在 `unshare -n` 里先确认连域名都解析不了，再跑自检：三个引擎都能加载模型，都能完成分割，退出码都是 0 |
| 3 | 子进程结果和直接调用相同 | Cellpose、StarDist、Mesmer（nuclear-guided 的细胞和核）都**逐像素相同** |
| 4 | Stop / 崩溃 / 关闭 | Stop 时，已完成的任务保留为 ok，其余登记为 cancelled，进程组清空；`kill -9` 后，所有任务登记为 failed，退出码 -9，不会卡住；正常关闭时退出码为 0，进程组清空；任务报错后，下一个任务照常运行 |
| 5 | 不改现有的东西 | 没有改主程序和现有 worker；`fusion_test2`、`fusion_mesmer` 两个环境没有变化；`cufile.log` 始终是 4449898 B，SHA-256 `04c8a602…` |
| 6 | 测试不是空断言 | 新测试在 HEAD 导出树上会报收集错误。另外做了两处**反向注入**，各自对应的测试都会失败：去掉 Cellpose 的输入拷贝；去掉「stdout 只走协议」的重定向。恢复代码后测试通过 |

**全量回归**（2026-09-24，按附录的方法：170 个模块，每个模块单独一个进程，`fusion_test2`）：
- 结果：**3745 passed / 17 failed / 1 skipped**。回归期间代码冻结，运行结束后逐个核对文件哈希，都没有变化。
- 17 个失败中，有 16 条和附录基线的清单**逐条相同**，而且不涉及本块的任何路径。
  - `git diff c9f80df HEAD` 显示，生产代码和基线相同，只有本计划文档有改动。
  - 附录里提到的偶发失败 `test_loaded_channel_switch_is_cache_hit`，这一次通过了。
- 第 17 条是**本块新引入的**：`test_seg_runner.py::test_stardist_in_subprocess_equals_direct_call`。
  - 原因：这条测试的「直接调用」参照在 pytest 进程里导入 StarDist，而回归命令没有设置 `KERAS_BACKEND`。`fusion_test2` 里 Keras 默认用 torch 后端，csbdeep 会拒绝。这是测试自身的问题：主程序在 `workers/cellpose_worker.py:181` 设置了这个变量，引擎子进程也由客户端设置了。
  - 修复：测试模块开头改为 `os.environ.setdefault("KERAS_BACKEND", "tensorflow")`。
  - 修复后，按回归命令（清空 `KERAS_BACKEND`）重跑这个模块：`fusion_test2` 12 passed / 1 skipped（Mesmer 因为缺 deepcell 而跳过），`fusion_mesmer` 13 passed。
  - 这次修改只改了这一个测试文件，其他模块不受影响，所以没有重跑整个回归。

**实测数据**（384×384 的合成图）：

| 引擎 | 设备 | 加载模型 | 一个任务 | 峰值内存 | 显存 |
|---|---|---|---|---|---|
| cellpose | cuda:0 | 6.1 s | 1.3 s | 1.8 GiB | 3.2 GiB（进程退出后释放） |
| stardist | cpu | 2.5 s | 1.3 s | 0.7 GiB | 0 |
| mesmer | cpu | 9.4 s | 3.1 s | 1.4 GiB | 0 |

**执行中的发现**：
1. **Cellpose 4.1.1 在 `eval` 时会原地改写多通道的输入数组**，改动幅度最大 0.04（在 [0,1] 的数据上）。同一个数组传进去两次，第二次分割的其实是被改过的图，结果就会不同。
   - 单通道输入不受影响。
   - 排查时我一度以为这是「第一次调用效应」或 GPU 不确定性，**这个判断是错的**。每次传入一份新拷贝，结果就完全确定。
   - `seg_runner` 的做法是传拷贝。已核对：现有的 Step1（`cellpose_worker.py:577`）和 Step2（`segment_merge_worker.py:2008-2016`）在 `eval` 之后都**没有复用**那个数组，不受影响。
   - 在 7.12 的共用输入构造里，这一条要作为约束写进去。
2. **环境导出时 conda 和 pip 有同名包**：conda 的 `tzdata` 是时区数据库，pip 的 `tzdata` 是 Python 包。第一版导出脚本按名字区分两者，漏掉了 pip 的 `tzdata`，第一次重建因此 `pip check` 报错。改成按 pip 自己记录的安装者（`INSTALLER`）区分后，第二次重建全部通过。
3. **子进程的工作目录不能是仓库**：CUDA 库会把 `cufile.log` 这类文件写到当前工作目录，所以引擎进程的工作目录改为系统临时目录。相应地，协议里的路径一律由客户端转成绝对路径。
4. 在 `fusion_test2` 里跑 `test_seg_runner.py`，结果是 12 条通过、1 条跳过（Mesmer，因为那个环境里没有 deepcell）。

**输入归属表**（交付第 5 项；这是 V1/C 和 V2 的新流程。旧参数按 7.11.5 保持原有语义）：

**所有方法都由应用侧完成的公共步骤**，按顺序：
1. `read_bbox = patch ± HALO`，并与 ROI 的 bbox 取交集（7.11.3）；
2. fusion：committed 窗口（min/max/gamma）和权重（R13），`fuse_fullres` 或 fused.zarr；
3. 量化为 uint16；
4. 多边形以外置 0；
5. ÷ 65535 得到 `F`；
6. 按下表构造数组，交给引擎（**必须交拷贝**，见发现 1）；
7. 引擎返回之后：按 Step2 生效的归属代码（7.11.3）裁出中央区域，重新编号，统计细胞数。

| 方法 | 输入（应用侧构造） | dtype / 值域 | 定标（引擎侧，一套、一次） | 推理参数 | 扩张 | 其他后处理 | 输出 |
|---|---|---|---|---|---|---|---|
| Cellpose whole-cell | `stack([F0, F0, F1], -1)`，HWC，`channel_axis=-1` | float32 [0,1] | `eval(normalize=True)`：逐通道取 1–99 百分位（`cellpose/models.py:274-292`） | diameter、flow、cellprob、min_size | — | 在 `eval` 内部完成（min_size） | 细胞 |
| Cellpose nuclei | `F1`，HW | 同上 | 同上 | 同上 | — | 同上 | 核 |
| Cellpose nuclei + expansion | `F1`，HW | 同上 | 同上 | 同上 | **引擎侧**：`expand_labels(distance)`，同时**保留扩张前的核 mask** | 同上 | 细胞（扩张后）、核（扩张前） |
| StarDist nuclei | `F1`，HW | 同上 | csbdeep `normalize(1, 99.8)`，只做一次（`seg_runner/engines.py`） | prob、nms（None 时用模型默认值） | — | — | 核 |
| StarDist nuclei + expansion | `F1`，HW | 同上 | 同上 | 同上 | **引擎侧**：同 Cellpose expansion | — | 细胞、核 |
| Mesmer whole-cell | `stack([F1, F0], -1)`，HW2 | 同上 | DeepCell 默认预处理：99.9% 截断、rescale、CLAHE 128（`mesmer.py:62-70`） | image_mpp、compartment=whole-cell；P1 的阈值给 whole_cell kwargs | — | **引擎侧**：`postprocess_mask`（min_size 等） | 细胞 |
| Mesmer nuclei | `stack([F1, 0], -1)` | 同上 | 同上 | compartment=nuclear；P1 的阈值给 nuclear kwargs | — | 同上 | 核 |
| Mesmer nuclear-guided | `stack([F1, F0], -1)` | 同上 | 同上，两次调用（O2） | 细胞用 P1 的阈值，核用默认值（P2） | — | 同上 | 细胞、核（`paired: false`） |

- **原型的边界**：扩张和 `postprocess_mask` 目前还**没有**写进 `seg_runner`（V0 只覆盖推理）。按上表，它们放在引擎侧，由 V1/C 实现，届时由 7.10.6 的逐像素对比来把关。
- 表中「引擎侧」的步骤只在引擎里做，「应用侧」的步骤只在应用后台任务里做，两边都不能重复做（7.10.4）。

### 7.11 模型输入契约（R10–R12；V1/C 和 V2 接入之前必须写定）

**契约**：Step1 和 Step2 共用同一套模型输入规则。
- whole-cell 的输入是 `[fusion, fusion, DAPI]`，`channel_axis=-1`；
- 亮度：用户的窗口和权重在全局上生效；应用侧不额外做自动拉伸；每个引擎只执行一次它的那一套标准预处理（R11 修订版，见 7.11.5）；
- Step1 采用和 Step2 一致的 HALO 与边界处理，再裁出中央的结果。

旧的通道排法对比实验取消。验证的内容改为：**读取区域相同时**，两边的输入数组、参数和输出是否一致；并且应用侧不额外拉伸、每个引擎只执行它那一套预处理（7.11.4）。

#### 7.11.1 R10 通道

| 方法 | 送进模型的数组（两个 Step 相同） |
|---|---|
| Cellpose whole-cell | `np.stack([fusion, fusion, DAPI], -1)`，`channel_axis=-1`。Step2 在模型入口从 fused.zarr 的 `[fusion, DAPI]` 构造，**不改** fused.zarr 的存储 |
| Cellpose nuclei / nuclei + expansion | 单通道 DAPI（cpsam 内部会补成 `[DAPI, 0, 0]`，`cellpose/transforms.py:609-614`） |
| StarDist ×2 | 单通道 DAPI |
| Mesmer whole-cell / nuclear-guided | **`[fusion 的核通道, fusion 通道]`**，两个 Step 相同，都取自 fusion 输出 ÷ 65535。用户平时就是用 Fusion 当膜通道（2026-09-23）。Step2 的「DAPI + Fusion channel」模式（`step1_weighted_fusion`）本来就走 fused.zarr 切块，也就是这种排法（`segment_merge_worker.py:1722-1725` → `mesmer_worker.run_mesmer_on_fused_tile`） |
| Mesmer nuclei | `[fusion 的核通道, 0]`，与现有的「DAPI only」模式一样，第二个通道为 0 |

#### 7.11.2 R11 全局亮度：应用层和模型内部都要检查

**应用层**（已核实）：
- **已经符合**：fusion 和 fusion 里的核通道。
  - Step1 用 `fuse_fullres`（`core/fusion_engine.py:109-147`），Step2 的 fused.zarr 由 `FullFusionWorker._fuse_tile`（`ui/step0/overview_panel.py:373-405`，`_channel_norm` 在 `:308`）写出。
  - 两边都只用 committed 的窗口；没有窗口的通道直接不参与，不会按区域估计。
  - 两边都通过 `fuse_channels` 乘上 group/nucleus 权重，裁剪到 [0,1]，再**量化成 uint16**。
  - V0 要核实一点：`apply_channel_remap(raw, p)` 和 `_channel_norm(ch, arr)` 在同一个窗口下**逐元素相等**。
- **不符合**：
  - Step1 的纯核方法先调用 `loader.read_region(DAPI, normalize=True)`，这会走 `_norm`，对当前区域取 p1–p99.5（`core/io_loader.py:294-302`）；然后又对 patch 做 min-max（`workers/cellpose_worker.py:544`）。这是两次局部归一化。
  - Mesmer 的 `build_mesmer_input`（`utils/mesmer_utils.py:229-258`）分两种情况：
    - 有 committed 窗口的通道，用 `apply_channel_remap`，也就是**全局窗口**（`:232-234`）；
    - 没有窗口的通道，才按当前区域做 1–99.8 百分位（`:235-236`）。
    - 膜通道按 `weights` 加权后取最大值（`:250-253`）。**没有设置膜通道时，第二个输入通道全为 0**（`:254-255`），而注册表里 `membrane_channels` 的默认值就是空列表。
    - 核通道没有乘核权重，违反 R13。

**纯核方法的输入（审核指出：只有「同一个 DAPI 窗口」还不够）**：
- Step2 读到的核通道是：`clip(apply_window(DAPI) × nuc_w, 0, 1)`，量化为 uint16，再 /65535（`core/fusion_engine.py:66-68`）。
- 如果 Step1 只是把原始 DAPI 按全局窗口映射成 float，就少了**核权重**和**uint16 量化**这两步，两边的输入仍然不同。
- 契约要写明：窗口（包括 gamma）、核权重、裁剪、uint16 量化，每一步都做，还是每一步都不做。**两边必须一样。**
- **待裁定 N1**：
  - (a) 两边都用 fusion 的核通道，也就是 Step2 现有的 fused.zarr 第 1 通道。Step1 取 `fuse_fullres(...)[:, :, 1] / 65535`。这样核权重和量化都参与，Step2 不用改。
    - 副作用：核权重 < 1 会压低纯核方法的输入亮度；`nuc_w = 0` 时输入全为 0，应当拒绝运行。
  - (b) 两边都忽略核权重，只用「窗口 → 裁剪 → 量化」。这样 Step2 就不能直接用 fused.zarr 的核通道，要单独读 DAPI，改动更大。
  - **N1 裁定（R13）：选 (a)**，外加 `nuc_w = 0` 时拒绝运行。
  - Mesmer 也一样：纯核输入和核通道都从 fusion 结果中取，不再在 `build_mesmer_input` 里另读原始通道。膜通道的来源由块 B/C 按 R13 另外写定。

**模型内部**（已核实）：
- **Cellpose**：`eval` 默认 `normalize=True`，会对每张输入图的每个通道单独做 1–99 百分位拉伸（`cellpose/models.py:157`、`:174`、`:274-292`）。
- **StarDist**：归一化是我们自己的代码调用 `csbdeep.normalize(1, 99.8)`（`cellpose_worker.py:300`、`segment_merge_worker.py:2132`）。
- **Mesmer**：DeepCell 的 `mesmer_preprocess`（`deepcell/applications/mesmer.py:62-70`）默认对**当前输入**做三步局部处理：
  1. `percentile_threshold(99.9)`，百分位截断；
  2. `histogram_normalization` 里的 `rescale_intensity(out_range=(0,1))`，按当前输入的最小值和最大值重新拉伸（`deepcell_toolbox/processing.py:78-79`）；
  3. `equalize_adapthist(kernel_size=128)`，也就是 CLAHE（`:80`）。
  - **现状：两层定标都开着。** 第一层是上面的全局窗口，或者局部百分位；第二层是 DeepCell 的这三步。代码调用 `app.predict` 时没有传 `preprocess_kwargs`（`mesmer_utils.py:337`），所以用的是默认值。
  - **会不会「过度拉伸」**（按代码推理，**未实测**）：
    - 第一层已经把数值映射到 [0,1] 并截断。第二层的 99.9% 截断，会让最亮的 0.1% 再饱和一次。
    - 最主要的是 `rescale_intensity` 按当前输入的最小值和最大值重新拉伸：在信号很弱的区域（比如只有背景和少量暗细胞），最大值本身就小，拉到 0–1 之后，**背景噪声被放大成满幅**。
    - CLAHE 再增强局部对比度。
    - 原始数据是 uint8（OME `Type="uint8"`，只有 256 级）。多次拉伸**可能**带来色阶断层，但这只是待验证的可能影响，不能仅凭数据是 uint8 就下结论。
    - 所以在弱信号的 patch 上，确实可能过度放大噪声。至于这是不是用户看到「Mesmer 表现不佳」的原因，还没有证据。
  - **其他可能拖累 Mesmer 的因素**：
    - 已核实：膜通道为空时，whole-cell 的第二个输入全为 0；
    - 像素尺寸：OME 的 `PhysicalSizeX` 是 0.5069 µm，和默认的 `image_mpp=0.5` **接近**，但这**不能**说明像素尺寸的影响已经排除，仍然是待验证的可能影响。
  - 局部处理**不只是 CLAHE**：前两步取决于整个输入窗口里有没有更亮的信号。**HALO 宽度不能解决这个问题**，128 也不是 HALO 的充分条件，因为还涉及模型的输入缩放和 CLAHE 网格的位置。这一版删掉了「HALO ≥ 128」的说法。按 R11 修订版，这种随窗口变化的差异用户已经接受。

**裁定**：I0–I3 已被 R11 修订版取代，I1（关掉 Cellpose 内部归一化）、I2（删掉 StarDist 的 `normalize`）、I3（关掉 Mesmer 预处理）**全部作废**。定标规则见 7.11.5。

**用户窗口与权重的冻结**（不属于自动定标）：
- 使用 committed fusion snapshot 里的 `display_mapping`，也就是 `_launch_worker` 现在已经冻结的那份（`main_window.py:6424-6444`），另加上核权重（N1）。
- 不要求每个任务重新扫描整张图。
- DAPI 没有 committed 窗口时，明确拒绝运行，不退回局部百分位。

#### 7.11.3 R12 Step1 HALO、ROI 边界与归属

**Step2 的实际做法**（已核实）：
- **HALO**：默认 `overlap_px=200`（`workers/segment_merge_worker.py:113`）。`read_bbox = own_bbox ± overlap`，在 fused.zarr 的边界（也就是 ROI 的 bbox）处截断，不补边（`utils/tile_scheduler.py:37-60`）。
- **多边形以外**：在**写 fused.zarr 的时候**，先 fusion，再量化成 uint16，然后把多边形以外的像素，两个通道都**置 0**（`ui/step0/overview_panel.py:606-622`，用 `cv2.fillPoly` 做掩膜）。
  - 分割阶段没有再做多边形处理：`_segment_one_zarr` 接收了 `poly_fullres` 参数（`:2194`、`:2969`），但函数内部**没有使用**。
  - 所以 Step2 的模型输入和全部预处理，看到的都是「多边形以外为 0」的图像。
- **归属规则**：生效的路径是 `segment_merge_worker.py:2538-2575` 里的内联代码，**不是** `CentroidOwnershipMergePolicy`。后者在 `:2584` 附近只做影子比较，注释写的是「legacy remains authoritative」。
  - 质心用 `_centroids_vectorised` 计算（`:2877`，bincount）。
  - 质心落在半开区间 `[own_y0, own_y1) × [own_x0, own_x1)` 内的细胞保留，按原标签顺序通过 LUT 重新编号。
  - HQ 方法的核用**同一个 LUT** 重新编号（`:2559-2562`）。

**H1（审核同意）**：
- HALO 宽度和 Step2 Tile Grid 的 overlap 用**同一个配置**，冻结进运行快照，写进参数文件。
- Step2 实际执行时必须用这个值。如果改了它，旧的预览就**不能**再被说成是同一执行配置，要在界面上标出来。

**H2（审核同意统一；具体规则写定如下）**：
1. **读取范围**：`read_bbox = patch_bbox ± H`，然后与**分析 ROI 的 bbox** 取交集（full_wsi 时与整张切片取交集）。超出的部分不读、不补边，和 Step2 一样。
2. **先 fusion，再量化**：和 fused.zarr 的写出顺序一样，得到 uint16 的 `[fusion, DAPI]`。
3. **多边形掩膜**：在量化**之后**、构造模型输入**之前**，把多边形以外的像素，两个通道都置 0。
   - 用和 `_poly_mask` 同一种栅格化方式（`cv2.fillPoly`，坐标为 level-0 `(x, y)`）。
   - 这样 Step2 在 fused.zarr 里看到的 0，和 Step1 看到的 0，是同一批像素。
4. **哪些像素参与预处理**：`read_bbox` 里的全部像素，包括多边形以外的 0，都送进模型，参与模型那唯一一次局部定标。Step2 的 fused.zarr 在同样的位置也是 0，所以读取区域相同时，两边参与定标的像素完全一样。
5. 最后按下面的归属规则裁出中央区域。
- 没有多边形的 ROI，跳过第 3 步。

**归属与细胞 / 核的配对**（审核指出，原来的写法是错的）：
- **不能**给细胞和核分别按各自的质心筛选、分别重新编号，然后声称配对自然保留。扩张后的细胞，质心可能在 patch 内，而它的核质心在 patch 外；分开编号还可能让本来不对应的对象拿到同一个 ID。
- **共享标签的输出**（Cellpose / StarDist 的 nuclei + expansion：`expand_labels` 保留原标签，细胞和它的核 ID 相同）：
  - 只按**主输出**决定归属。主输出是细胞 mask（扩张后的那张）。
  - 用主输出的质心生成**一张** LUT，细胞 mask 和核 mask 都用这张 LUT 重新编号。
  - 这和 Step2 对 HQ 核的做法一样（`:2559-2562`）。
- **独立生成的输出**（Mesmer nuclear-guided：细胞和核来自两次独立的后处理，标签没有对应关系）：
  - 细胞和核各自按自己的质心决定归属，各自重新编号。
  - 结果记录里写明 `paired: false`，界面和下游都**不能**把「编号相同」当作「配对」。
- **只有一种输出的方法**：只按这一种输出决定归属。
- **细胞数**用保留下来的不同标签的个数，不用最大标签值。

**复用方式**：
- 归属和重编号的**规则**以 Step2 生效的内联代码为准：用 bincount 算质心，半开区间，按原标签顺序生成 LUT。
- Step1 可以调用 `_centroids_vectorised`（静态方法），或者调用 `CentroidOwnershipMergePolicy`。但无论调用哪个，**V1/C 都要有一道门证明**：在同一张 label 图和同一个 own_bbox 上，它给出的保留集合和新标签，与 `segment_merge_worker.py:2538-2575` 的结果**完全一致**。
- 没有这个证明，就不能写「调用这个类就等于照搬 Step2」。

#### 7.11.4 验收：分成两个独立的问题

**「读取区域相同」的定义**：以下各项都相同，才算读取区域相同：
- 同一个 level-0 坐标区域 `read_bbox`；
- 同一个 ROI 多边形掩膜；
- 同一个来源版本，也就是 `pixel_key`；
- 同一份冻结配置：fusion 快照、HALO、引擎身份、参数。

比较最终归属之后的 mask 时，还要用**同一个中央区域**，也就是 `own_bbox`。

1. **执行路径一致**：读取区域相同时，Step1 和 Step2 一致吗？
   - 比较两条路径实际送进模型的**输入数组**（逐元素相等）、实际传给库的**参数**（kwargs 相等），以及**输出**。
   - 输出比较以设备相同为前提。设备不同时，按 7.10.6 事先定下的指标来比较。
2. **应用侧没有额外拉伸，引擎只执行它那一套预处理**：
   - **输入数组要按方法分别构造后再比较**，不能一律写成「等于两通道的 fusion 数组」。设 `F` = fusion 输出（uint16，已经做完多边形置 0）÷ 65535，`F[...,0]` 是 fusion 通道，`F[...,1]` 是核通道：

     | 方法 | 送进引擎的数组必须正好等于 |
     |---|---|
     | Cellpose whole-cell | `stack([F0, F0, F1], -1)`：fusion 复制一份 |
     | Cellpose nuclei（含 expansion）、StarDist ×2 | `F1` |
     | Mesmer whole-cell、nuclear-guided | `stack([F1, F0], -1)`：通道顺序对调 |
     | Mesmer nuclei | `stack([F1, 0], -1)` |

   - **引擎内部的预处理，检查具体的处理步骤和参数，不能只数 `normalize()` 被调用了几次**：
     - Cellpose：`normalize=True`，没有额外的 `lowhigh` 或 `percentile` 覆盖；
     - StarDist：只有一次 `normalize(img, 1, 99.8, axis=(0,1))`，作用在上表的输入上；
     - Mesmer：调用 `app.predict` 时不传 `preprocess_kwargs`，也就是按 DeepCell 的默认流程：`threshold=True, percentile=99.9, normalize=True, kernel_size=128`。
   - 同一个细胞在不同的窗口或 HALO 下，定标后**可能不同，幅度尚未实测**。用户已经接受（R11 修订版），**不作为验收项**。
- Step2 跨切块合并后的接缝区域，另外做针对性验收，不提前保证所有像素完全一样。


#### 7.11.5 定标规则（R11 修订版，用户裁定 2026-09-23）

**两层，分清楚**：
1. **用户的设置**：显示窗口（min/max/gamma），以及通道、组、核的权重。它们在 fusion 里全局生效（R13），随运行快照冻结。**这一层不算自动定标。**
2. **模型的自动定标**：保留，在**当前送进模型的那张图**上计算（Step1 是 patch 加 HALO，Step2 是切块加 overlap）。应用侧**不再额外自动拉伸**；每个引擎只执行**一套**下表列出的标准预处理流程，而且只执行一次。这里说的是「一套流程执行一次」，不是「只允许一次数学变换」：Mesmer 的那一套流程本身就包含截断、拉伸和 CLAHE 三步。

**每个引擎保留的那一套标准预处理**：

| 方法 | 保留的那一套预处理 | 位置 |
|---|---|---|
| Cellpose ×3 | 模型自带：`eval(normalize=True)`，每个通道取 1–99 百分位 | 引擎内部（`cellpose/models.py:274-292`） |
| StarDist ×2 | 库本身不做定标，保留**我们代码里唯一的一次** `normalize(1, 99.8)` | 引擎模块内，只保留一处 |
| Mesmer ×3 | DeepCell 自带的默认预处理：99.9% 截断，按最小/最大值拉到 0–1，再做 CLAHE（`mesmer.py:62-70`） | 引擎内部，调用时不传 `preprocess_kwargs` |

**新流程里不再调用的多余定标**（逐处排查的结果；在 V1/C、V2 里落实，由验收第 2 条把关）：
- 要求是**新流程不再经过这些分支**，**不是**把旧代码整个删掉。
- 旧的参数文件（比如手选膜通道、`normalize_input=True`）在 Step2 里**仍然按原来的语义执行**。
- 如果要改变旧项目的行为，需要用户另外明确决定。

| # | 位置 | 现在做了什么 | 处理 |
|---|---|---|---|
| 1 | Step1 纯核方法：`loader.read_region(DAPI, normalize=True)` → `_norm`（`core/io_loader.py:294-302`，`workers/cellpose_worker.py:540`） | 按当前区域取 p1–p99.5 | 新流程不再调用：纯核方法改为从 fusion 结果里取核通道（R13） |
| 2 | Step1 纯核方法：`fusion._normalize_intensity`（`cellpose_worker.py:544`） | 按 patch 做 min-max | 新流程不再调用 |
| 3 | Mesmer：`build_mesmer_input` 对没有窗口的通道调用 `normalize_percentile`（`utils/mesmer_utils.py:235-236`） | 按区域取百分位，之后 DeepCell 又会再做一次 | 新流程不再调用 `build_mesmer_input`。旧的手选膜通道参数仍然走它，行为不变 |
| 4 | Mesmer Step2：`build_mesmer_input_from_fused_tile(normalize=True)`（`mesmer_utils.py:103-112`） | 对 fused 切块按百分位拉伸，之后 DeepCell 又会再做一次 | 新流程写出的参数带 `normalize_input=False`，所以不会进入这一步；旧参数文件里没有这个键或值为 True 的，行为不变 |
| 5 | `FusionEngine.compute(prenormalized=False)`（`core/fusion_engine.py:87-108`） | 每个通道做 min-max | 目前模型输入路径上没用到，因为 `fuse_fullres` 传的是 `prenormalized=True`。在 V0 的归属表里确认没有其他调用方 |

**不算定标的操作**：fusion 输出 ÷ 65535 只是换一下单位，保留。多边形以外置 0 也保留。

**Mesmer 的 CLAHE**：用户裁定**保留**（2026-09-23）。它和截断、拉伸一起，算作 DeepCell 自带的那一次预处理。

**Mesmer 的「Fusion 当膜通道」路径，现状与要改的地方**（已核实）：
- **Step2**（`_validate_mesmer_config` → `fused_zarr` → `build_mesmer_input_from_fused_tile`，`utils/mesmer_utils.py:103-112`）：
  - 通道排法已经是 `[核, fusion]`。对新流程来说，只需要不进入它自己做的那次百分位拉伸，也就是上表第 4 处。
  - **已核实的新发现**：Mesmer nuclei 的 `input_mode="DAPI only"` 不在 `_mesmer_uses_selected_channels` 的集合里（`segment_merge_worker.py:1713-1720`），所以也会走 `fused_zarr` 路径，第二个通道拿到的是 **fusion，而不是 0**。Step1 的「DAPI only」给的却是 0（`mesmer_utils.py:243-244`）。两边现在就不一致。
  - 新流程的共用构造函数按 7.11.4 的表执行：nuclei 的第二个通道为 0。旧参数文件在 Step2 里保持原来的行为。
- **Step1**（`workers/mesmer_worker.py:172-190` → `build_mesmer_input`）：
  - 膜通道取 `fuse_fullres(...)[:, :, 0]`。这是**没有 ÷ 65535 的 uint16**，之后靠 `normalize_percentile` 拉到 0–1。去掉这次拉伸时，**必须补上 ÷ 65535**，否则 DeepCell 拿到的就是 0–65535 的数值。
  - 核通道是单独按窗口读的原始 DAPI，**没有乘核权重**，违反 R13。改为取 fusion 的核通道。
  - 改完以后，Step1 和 Step2 走的是同一个构造函数。按 R14，只保留 `build_mesmer_input_from_fused_tile` 这一条路，Step1 把 `fuse_fullres` 的结果交给它。
- **新界面**（**用户裁定，2026-09-23**）：Mesmer 的膜通道**只提供「Fusion」一种**。「手选膜通道」（`selected_channels`）模式要单独读原始通道，绕开 fusion，和 R13、R14 冲突。它在新界面里不显示，代码按 R2 的做法保留，Step2 仍然兼容。

**已知的后果**（推理，**未实测**，用户已知悉）：
- 局部定标会抵消「整体亮度倍数」。比如核权重 0.6 这种对整张图的统一缩放，对纯核方法基本不起作用。
- 但 gamma 的曲线形状，以及通道之间、组之间的相对权重，都会保留，因为它们是在定标之前混合进去的。

### 7.12 架构大纲：一套轮子，方法模块化（R14）

**目标**：
- Step1 和 Step2 都只是**基座**：Step1 驱动「patch × 组合」，Step2 驱动「切块 × 一个组合」加合并和恢复。
- 分割方法是**模块**，两个基座调用的是同一批模块。
- 同一个功能只保留一份实现。

**共用组件**（每一项只有一份实现，Step1 和 Step2 都调用它）：

| 组件 | 内容 | 现状（重复的轮子） | 落在哪一块 |
|---|---|---|---|
| 方法注册表与参数模式 | 方法 → 引擎、参数的类型、范围、精度和默认值、参数校验、`combo_id` | 注册表只有默认值；Step1 和 Step2 各有一套控件和读参逻辑（7.1、7.8） | B（Step1），E（Step2 装载） |
| 输入准备 | 带 HALO 的读取范围 → fusion（窗口、权重、gamma）→ uint16 → 多边形置 0 → 按方法构造模型输入（R10）（不做任何自动定标，定标只在引擎内做一次，见 7.11.5） | Step1 用 `fuse_fullres`，Step2 用 `FullFusionWorker._fuse_tile`；纯核方法在 Step1 里绕过 fusion（R13）；Mesmer 另有 `build_mesmer_input` 和 `build_mesmer_input_from_fused_tile` 两条路 | V1/C，V2 |
| 引擎模块（方法插件） | 每个引擎一个 runner，方法是引擎内部的配置；推理和引擎侧后处理（expansion、`min_size`、`postprocess_mask`） | Step1 的 `cellpose_worker` 和 `mesmer_worker`，Step2 的 `segment_merge_worker._segment_tile`，各自调用一遍模型；StarDist 在 Step1 走子进程，在 Step2 走主进程 | V0（原型），V1/C，V2 |
| 归属与重编号 | 质心、半开区间、LUT、共享标签的输出共用同一张 LUT | Step2 生效的内联代码（`:2538-2575`），加上一份只做影子对比的 `CentroidOwnershipMergePolicy` | V1/C（抽出），V2（Step2 改为调用；停掉影子对比） |
| 结果记录与原子发布 | 7.3 的记录格式和写盘顺序 | Step1 写 npz，Step2 有自己的输出 | V1/C；Step2 的输出格式不在本计划里统一，只增加引擎身份字段 |
| 引擎身份与设备 | 7.10.5 | 没有 | V0 |

**做法**：
- **共用组件由搬迁得到，不重写。** 以 Step2 生效的代码为准，原样抽成共用函数，Step2 改为调用它。
- **验收分成两类，不能混在一起**：
  1. **纯搬迁**（只是把代码抽成共用函数，不改行为）：配回归测试，要求搬迁前后 Step2 的输出**完全相同**。
  2. **落实新的输入契约**（R10–R13：通道排法、输入来源、去掉多余定标）：这会**有意改变**结果，**不和旧结果比较**，不能让旧结果的基线挡住新规则。改为验证：读取区域、配置和设备都相同时，Step1 和 Step2 一致（7.11.4）。
  - 每次提交只做其中一类。先纯搬迁、测试通过，再改行为。这样一旦结果变了，能分清是搬迁出了错，还是新规则带来的预期变化。
  - 这就是 7.11.3 里「不需要证明两份实现一致」的前提：本来就只剩一份实现。
- **影子对比**：V2 里停掉 `_merge_policy_shadow_compare` 的调用（`segment_merge_worker.py:2584`）。
  - `CentroidOwnershipMergePolicy` 类还被 `utils/tile_scheduler.py` 和 `tests/test_merge_policy.py` 引用，是否删除另立清理任务。
- **边界**（遵守 `AGENTS.md`）：
  - R14 是**方向**，不是一次性大重构的授权。每一个组件的抽取和替换，都在上表对应的块里做，按块的白名单单独批准。
  - HQ、HQ2、CDS 按 R2 保留原样，不纳入这次模块化。
  - Step2 的切块、合并和恢复机制保留（V3 裁定）。

---

## 附录：测试基线（`c9f80df`，2026-09-23）

**命令**：逐模块运行。多个界面模块放在同一个进程里跑会出现 segfault，所以必须一个模块一个进程。强制 GPU 时，renderer 须为 RTX 4090。

```bash
T=/sda1/Fusion/analysis_pipline/block01_v14/tests
cd /tmp && for f in $(cd $T && ls test_*.py); do
  BLOCK01_REQUIRE_STEP1_GPU=1 PYTHONPATH=/sda1/Fusion/analysis_pipline/block01_v14 \
    timeout 1500 python -m pytest $T/$f -p no:cacheprovider --confcutdir=/ -q -rfEs
done
```

需要 `PYTHONPATH` 的原因：部分模块用顶层的 `viewer.` 导入。

**结果**：169 个模块，3734 passed，16 failed，0 skipped；`cufile.log` 零增长（4449898 B）。

**与 HEAD 对比的方法**：
1. `git archive <commit> | tar -x -C <scratch>/headtree`；
2. 在 `<scratch>` 下对失败的模块运行 `PYTHONPATH=<headtree> python -m pytest <headtree>/tests/<f>.py --rootdir=<headtree> --confcutdir=<headtree> -p no:cacheprovider -q -rf`。

2026-09-23 以 `4c1a418` 的导出树为对照，下面 16 条在对照树上**逐条同样失败**。

| 模块 | 失败用例 |
|---|---|
| test_step0_channel_conditioning.py | test_patch_switch_reads_only_active_channel<br>test_unloaded_channel_lazy_loads_on_switch<br>test_dapi_lazy_loads_like_a_marker<br>test_sync_passes_the_active_channel_eagerly_and_the_rest_lazily<br>test_unvisited_patch_does_not_inherit_zoom<br>test_patches_keep_independent_viewports |
| test_step0_process_incremental.py | test_a_finished_run_leaves_its_channel_up_to_date<br>test_a_sigma_change_makes_only_that_channel_stale<br>test_a_changed_patch_list_invalidates_every_channel<br>test_a_recompute_always_runs_its_own_channel<br>test_dataset_reset_forgets_the_signatures |
| test_hq_marker_segmentation.py | TestHqMarkerSegmentation::test_hq_roi_only_source_does_not_silently_fallback_to_first_group<br>TestHqMarkerSegmentation::test_hq_roi_only_source_uses_saved_roi_not_first_group |
| test_step0_no_process_button.py | test_save_still_builds_its_config_from_the_ticked_rows |
| test_preview_source_provider.py | test_save_invalidation_repulls_into_workbench |
| test_tissue_navigator_viewport_sync.py | test_mapping_slide_local_to_full |

**批量运行时偶发失败、单独运行 3/3 通过**：`test_step0_channel_conditioning.py::test_loaded_channel_switch_is_cache_hit`。

**使用规则**：
- 这张表只用来说明「这条失败不是本块引入的」，而且必须在当时重新和 HEAD 对比过；
- 本计划涉及的路径上，任何失败都要修复或单独报告，不能引用这张表来豁免；
- 块 C 涉及 `test_hq_marker_segmentation` 所覆盖的 worker 路径，届时要重新评估这两条失败。
