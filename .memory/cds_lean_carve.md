# CDS 引擎重构(v11 → v12)

> 日期:2026-06-05 ｜ 分支:v12 ｜ 仓库:block01 (Fusion_analysis)
> 时间线:v11 完成第 1/2/3 项(算法方向、find_objects OOM 修复、lean_carve 架构)→
> v12 完成第 4 项(lean_carve 数据通路内存修复)。

目标:CDS 旧路径(per-channel Gi* + flood-fill + watershed)在全 WSI(425MP)上 OOM,
结果不如 MACSiQView。三个核心要求:轻量、快速、稳定。

## 1. 算法方向:inside-out flood-fill → outside-in 收缩
- 问题:旧 flood-fill 用全局阈值 raw>=auto_low 当胞质可达判据 → 弱信号"紧贴核"、孤立肝细胞"涨成圆"。
- 根因:全局绝对阈值,而人眼用局部对比;local_contrast 默认关、且未接入可达判据。
- 改动:outside-in 收缩——从 Voronoi territory 外缘沿"确信背景"(局部 z-score gi_c<tau)向内收缩,
  只从自由边界进入、不跨 Voronoi 共享面、不啃核/minimal 环;多通道逐个收缩后融合。
  关键决定:逐通道独立收缩,不做通道融合(融合会丢 marker 特异性 + 叠加噪声)。

## 2. Bug 修复:find_objects 按 label 最大值分配内存
- 问题:某些数据下小图也 OOM 上百 GB。
- 根因:scipy.ndimage.find_objects(max_label=L) 内存正比 label 最大"数值"(非细胞数);
  全局偏移 id(十亿级)→ 按十亿分配。constrained_donut_segmentation.py 有 7 处,两个引擎都中招。
- 改动:新增 _label_slices(),present label 压成 1..K 再 find_objects、映射回原 id;7 处全部改用。
  id 空间/QC/图层不变。

## 3. 架构重写:lean_carve(流式、恒定内存)
- 问题:结果满意但全 WSI 单 tile 峰值 ~80GB;旧 CDS 同时驻留 ~20+ 全图数组(~145 bytes/pixel)。
  对比 MACSiQView ~16 bytes/pixel、130s、内存不动。
- 根因:重机械(Gi*/watershed/support)既贵又负收益;全分辨率下几十个全图数组同时在内存。
- 改动:新建 workers/lean_carve_segmentation.py,内部自动分块流式(block+halo>=max_radius,与整图等价),
  逐 block 逐通道即用即弃,只持有 nuclei+output+单 block 临时。默认 cytoplasm_engine="lean_carve";
  旧 flood_fill/outside_in 保留为 fallback。

## 4. 第 0 步:lean_carve 数据通路内存修复
- 问题:函数本身已轻(单测 1 亿像素 0.73GB),但 worker 调用侧仍 ~80GB。
- 根因:(A) worker 仍整图读 8 HQ 通道(~13.6GB),与惰性 block loader 并存相互抵消;
  (B) SharedChannelStore(max_cache_items=32) 按"通道+区域"缓存,1×1 模式缓存整图条目达几十 GB;
  (C) 输出 out 是整图 RAM 数组,未用已有 global_mask memmap。
- 改动:(A) lean_carve 路径跳过整图读取、只走惰性 loader,marker_channels 传 [];
  (B) 整图模式收住通道缓存;(C) global_mask memmap 当 output_labels 就地写盘。
- 验证:等价性测试 final_labels 逐像素相等;ulimit 下大图不 OOM;真实跑 cache_bytes 几十GB→~104MB。

## 当前状态与已知边界
- CDS/lean_carve 通过:轻量(缓存~104MB,单测 1亿像素 0.73GB)、稳定(不OOM、结果不变)、结果达标。
  CDS 自身耗时 post≈113s。
- 剩余 80GB 与最大时间瓶颈(infer≈226s)属 cellpose 整图(1×1)出核,非 CDS;
  决定暂不改 cellpose(切块+跨块 label 合并工程量大)。
- 已知边界:更大的图仍会在 cellpose 那步 OOM;临时缓解=GUI 把 tile 设 3×3(仅为 cellpose 分块)。
- 未做的提速选项(随时可捡):第1+2步——整个 block 全程驻留 GPU(EDT/voronoi faces/gi 滤波/
  binary_propagation 收缩/label 连通清理 全在 cupyx,只下载一次标签),治 lean_carve 的 113s、不治内存。
  已确认 cupy 13.3 的 cupyx 支持 binary_propagation/distance_transform_edt/label(grey_reconstruction 不支持但用不到)。
