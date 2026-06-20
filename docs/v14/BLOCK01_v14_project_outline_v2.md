# Block01 v14 项目大纲 v2：MACSiQView-style Setup & Preprocessing Workbench

**项目代号：Block01 v14**  
**文档版本：v2**  
**核心主题：主流程 GUI 架构重构 + MACSiQView / QuPath / napari 风格 Step0 Preprocessing Workbench**  
**当前结论：v14 是架构级版本，不再作为 v13.1 小修处理。**

---

## 0. v2 相对 v1 的关键修正

本版在 v1 基础上补强了 source-aware Channel Conditioning 的结构约束，避免把“完整版 raw/corrected source support”实现成 GUI 声明或 config 自报。

关键修正：

```text
1. Channel Conditioning / Remap 是 source-aware calibration，不是 source declaration。
2. GUI source selector 只产生 SourceRequest，不是 provenance。
3. CalibrationSourceIdentity 必须来自实际 pixel source，而不是 GUI label。
4. Promotion 必须用 resolver 独立复算 per-channel source identity，不能信 config 自报。
5. 所有参与 conditioning 的 raw/corrected source 必须几何一致，并等于 step2_input_shape。
6. mixed raw/corrected source 明确标记为 mixed signal semantics，需要 v14.6 smoke 验证。
7. 用户只能保存 preview-only calibration config；step2_ready config 只能由 promotion 生成。
8. UI 上 Step1.5 消失；如果物理路径暂存 step1_5，provenance 仍应写 step0_channel_conditioning。
```

---

## 1. v14 定位

Block01 v14 的目标不是继续在现有 Step0 / Step1.5 / Step3 之间打补丁，而是重构主流程 GUI 架构，将原本分散的：

- background correction
- channel conditioning / remap
- tissue preview
- ROI navigation
- high-quality image viewer
- raw / corrected source selection
- source-aware calibration
- MACSiQView-style preprocessing flow

统一整合到主流程的 **Step0: Setup & Preprocessing** 中。

v14 的核心变化是：

```text
从：
    分散的 Step0 / Step1.5 / Skip buttons / 独立工具页

升级为：
    统一的 5-step 主流程
    + 一个 MACSiQView-style Step0 preprocessing workbench
    + 一个 source-aware calibration pipeline
```

---

## 2. 顶层主流程架构

v14 的顶部主流程只保留 5 步：

```text
Step0: Setup & Preprocessing
Step1: Fusion / ROI construction
Step2: Segmentation
Step3: Review / QC
Step4: Export
```

### 必须删除 / 隐藏

```text
Step1.5 独立主流程入口
Skip -> Step2
Skip -> Step3
Skip -> Step4
重复的 Background Correction 入口
```

### 主流程原则

用户可以直接点击任意 step：

- 不要求必须完成前一步才能进入后一步。
- 如果某个 step 缺少必要上下文，则页面打开，但显示 missing-context 状态。
- 直接导航不得自动运行分析。
- 直接导航不得自动创建 config、mask、corrected zarr 或 segmentation output。
- Step1 业务逻辑当前阶段暂时不改。

---

## 3. Step0 总体定位

```text
Step0 = Setup & Preprocessing Workbench
```

Step0 不是 setup-only 表单页，也不是简单的路径选择页。  
Step0 是整个 Block01 的前处理工作台，参考：

- MACSiQView 的流程心智
- QuPath 的左侧工具栏 + 右侧高质量 viewer
- napari 的高画质、流畅 zoom/pan、多层 overlay 交互

Step0 由三部分组成：

```text
1. 顶部薄 Load bar
2. Floating Tissue Preview / ROI Navigator
3. 主工作区两个 tabs：
   - Background Correction
   - Channel Conditioning / Remap
```

---

## 4. Step0 顶部 Load bar

顶部只保留最必要的加载功能：

```text
Input path
Output path
Marker / panel path
Load
```

### 不放在顶部的内容

```text
Validate
全局 Save project
Background Correction 按钮
Channel Conditioning 按钮
组织 preview 嵌入主窗口
Marker channel 细节控制
Nuclear channel 独立控件
```

### Save 逻辑

不设置“全局 Save”。

每个 tab 自己负责自己的保存动作：

```text
Background Correction tab:
    Write corrected_channels.zarr

Channel Conditioning / Remap tab:
    Save preview-only calibration config
```

用户不能直接保存 step2_ready config。  
step2_ready config 只能由 promotion 生成。

### Load 成功后的行为

用户点击 `Load` 且数据成功加载后：

```text
自动弹出 Tissue Preview / ROI Navigator
初始化 Step0 主 viewer
初始化 channel list / marker 信息
```

---

## 5. Tissue Preview / ROI Navigator

Tissue Preview 是 floating navigator，不占据 Step0 主工作区。

### 功能定位

Tissue Preview 负责全局空间导航与 ROI 管理：

```text
显示 whole tissue / WSI thumbnail
显示当前主 viewer 的视野矩形
支持画 ROI
支持编辑 ROI
支持删除 ROI
不限制 ROI 数量
支持 Full WSI mode / ROI mode
点击 ROI / 区域后，主 viewer 快速跳转
```

### 窗口行为

Tissue Preview 应该像 Zoom 会议小窗一样灵活：

```text
可拖动
可缩放
可最小化成一条小 bar
可从最小化状态恢复
记住上次位置
记住上次大小
记住是否最小化
```

最小化 bar 示例：

```text
Tissue Preview | Full WSI | ROI: 3 | Current View
```

### 当前视野框

Tissue Preview 必须显示当前主 viewer 位于组织的什么位置：

```text
用正方形 / 长方形 viewport rectangle 标出当前主 viewer 视野
```

这个 rectangle 应该与主 viewer 的 zoom/pan 同步。

### 不属于 Tissue Preview 的内容

以下内容不放在 Tissue Preview 中：

```text
channel overlay
DAPI / marker quick preview
raw / corrected image comparison
remap preview
background correction preview
```

这些必须放在 Step0 主窗口右侧的高质量 viewer 中。

---

## 6. HighQualityImageViewer 核心模块

v14 必须抽象一个可复用的高质量图像 viewer 核心模块。

暂定名称：

```text
HighQualityImageViewer
或
NapariLikeImageViewer
```

### 来源

应优先复用当前 Block01 中表现最好的 channel conditioning / remap viewer 逻辑。  
如果当前 Step1 / Channel Conditioning / Remap 中已有较好的 napari-like viewer，应抽象为核心模块，而不是在 Step0 重新写一套低质量 viewer。

抽象时必须确认带入的是已经修复过的版本，尤其不能重新引入：

```text
参数变化导致 autoRange / zoom / pan 被重置
刷新时 viewer 跳回 fit-to-view
低分辨率 pixmap resize 导致马赛克
```

### 底线要求

```text
高画质
高流畅
不能马赛克
平滑 zoom in / zoom out
pan
fit-to-view
参数变化不重置 zoom / pan
支持多通道 overlay
支持 single-channel view
支持 split view / before-after
支持 raw / corrected / remapped source preview
支持 tiled / lazy loading
支持 viewport 与 Tissue Preview 同步
支持从 Tissue Preview 跳转到 ROI / patch / coordinate
```

### 长期复用目标

该核心 viewer 后续应可用于：

```text
Step0 Background Correction
Step0 Channel Conditioning / Remap
Step1 Fusion / ROI construction
Step2 Segmentation preview
Step3 Review / QC
```

当前 v14 优先服务 Step0，Step1 业务逻辑暂时不动。

---

## 7. Step0 主工作区布局

Step0 主工作区只有两个 tab：

```text
Tab 1: Background Correction
Tab 2: Channel Conditioning / Remap
```

用户可以：

```text
只做 Background Correction
只做 Channel Conditioning / Remap
两个都做
两个都跳过
```

这两个 tab 是可选 preprocessing 模块，不是强制流程。

---

## 8. Tab 1：Background Correction

### 8.1 功能定位

Background Correction 负责生成：

```text
corrected_channels.zarr
```

主要服务：

```text
non-HQ2/CSD workflows
Cellpose
Mesmer
普通 segmentation 流程
```

它是图像背景校正模块，不是 brightness / contrast / gamma remap 模块。

### 8.2 布局

采用左右布局：

```text
左侧：sidebar，小
右侧：HighQualityImageViewer，大
```

左侧 sidebar 用可调 splitter：

```text
上 2/3：channel list
下 1/3：background correction parameters
```

用户可以手动调整上下比例。

### 8.3 左侧上部：channel list

要求：

```text
支持通道名称搜索
显示所有 channel，包括 DAPI
DAPI 作为普通 channel 出现在列表中
不把 DAPI 做成独立控件
支持选择哪些通道参与背景矫正
复刻已有 Step0 通道矫正逻辑
支持通道可见性 / 当前显示通道 / correction target
```

### 8.4 左侧下部：background correction parameters

这里放 background correction 参数，而不是 remap 参数。

应包含：

```text
Tophat 参数
cuCIM 参数
已有 Step0 背景矫正参数
patch preview / selected ROI / full WSI 应用范围
Run / Apply correction
Write corrected_channels.zarr
```

这里不放：

```text
histogram
min / max
gamma
brightness / contrast
auto / reset
```

这些属于 Channel Conditioning / Remap，不属于 Background Correction。

### 8.5 右侧 viewer

右侧负责图像展示和 correction 结果比较：

```text
raw preview
corrected preview
before / after
split view
overlay view
single-channel view
current patch correction preview
```

必须尽可能复用已有 Step0 的通道矫正逻辑，而不是重写一套新的 correction preview pipeline。

### 8.6 Full WSI / ROI correction 后的抽查

Background Correction 对 full WSI / ROI 写出 corrected result 后，用户应能：

```text
在 Tissue Preview 中画新的 inspection ROI
主 viewer 跳转到该 ROI
加载 raw / corrected 对比
检查不同区域 correction 是否泛化
```

---

## 9. Tab 2：Channel Conditioning / Remap

### 9.1 功能定位

Channel Conditioning / Remap 负责：

```text
为 HQ2/CSD 或 marker-driven segmentation 提供 source-aware channel conditioning
保存 preview-only calibration config
```

它不是背景校正，而是 per-channel source + intensity remap 工具。

用户可以保存的是 preview-only calibration config：

```text
preview_only = true
step2_ready = false
```

正式 step2_ready config 必须由 promotion 生成，不能由 GUI 直接保存。

---

### 9.2 布局

与 Background Correction 保持一致：

```text
左侧：sidebar，小
右侧：HighQualityImageViewer，大
```

左侧 sidebar 用可调 splitter：

```text
上 2/3：channel list
下 1/3：remap / brightness-contrast parameters
```

用户可以手动调整上下比例。

---

### 9.3 左侧上部：channel list

要求：

```text
支持通道名称搜索
显示所有 channel，包括 DAPI
DAPI 作为普通 channel 出现在列表中
选择参与 conditioning 的 marker
通道可见性 / overlay 状态
```

每个 channel 支持选择 source request：

```text
raw
corrected
```

如果 `corrected_channels.zarr` 不存在，对应 corrected source 应提示不可用，或引导用户先运行 Background Correction。

---

### 9.4 Source-aware calibration，不是 source declaration

v14 采用完整版 raw / corrected source 架构，但必须定义为 **source-aware calibration**，不是 GUI declaration。

```text
GUI source selector 只产生 SourceRequest。
SourceRequest 表示用户想让该通道从 raw 还是 corrected 读取。
SourceRequest 不是 provenance，也不是 promotion 可直接信任的 source identity。
```

真正写入 preview config 的 source identity 必须来自实际 pixel source：

```text
CalibrationSourceIdentity 必须由 viewer/backend 实际读取成功的 pixel source 生成。
不能从 GUI label 复制。
不能从下拉框文字声明。
不能由 config 自报成为权威。
```

正确链条：

```text
GUI SourceRequest
    ↓
source loader / resolver / pixel source handle 实际读取像素
    ↓
HighQualityImageViewer 显示这些真实像素
    ↓
用户在这些真实像素上调 Min/Max/Gamma/Brightness/Contrast
    ↓
preview config 记录 SourceRequest + 实际 CalibrationSourceIdentity + remap 参数
    ↓
promotion 用 resolver 独立复算 source identity
    ↓
promotion 生成 step2_ready config
    ↓
Step2 runtime 只信 promoted config
```

禁止的错误链条：

```text
GUI selector 显示 corrected
    ↓
config 写 source_kind = corrected
    ↓
promotion 直接信 config 字段
    ↓
Step2 runtime 直接按 config 自报读取
```

---

### 9.5 Preview config 必须记录 per-channel SourceRequest 和 CalibrationSourceIdentity

每个通道至少记录：

```text
channel_name

source_request:
    requested_source_kind: raw_ome / corrected_zarr

calibration_source_identity:
    actual_source_kind: raw_ome / corrected_zarr
    actual_source_path
    actual_source_shape
    actual_intensity_space
    actual_channel_index / actual_channel_key
    coordinate_space, if available
    roi_id / roi_bbox, if available

remap parameters:
    min
    max
    brightness
    contrast
    gamma
    auto/manual state
```

注意：

```text
source_request 来自 GUI 选择。
calibration_source_identity 来自实际 pixel source 读取。
promotion 不把二者任何一个当作最终权威，必须独立 resolve。
```

---

### 9.6 Promotion 必须独立复算 source identity

Promotion 不能信 config 自报。  
即使 config 中记录的 calibration_source_identity 来自 backend handle，也仍然必须被 promotion 独立复算和交叉验证。

Promotion 对每个 conditioning channel 执行：

```text
1. 读取 preview config 中的 SourceRequest。
2. 使用 resolver 独立解析该 SourceRequest 对应的真实 pixel source。
3. 得到 promotion_resolved_source_identity。
4. 将 promotion_resolved_source_identity 与 config 中记录的 calibration_source_identity 比对。
5. 计算 Step2 runtime 将实际读取的 runtime_source_identity。
6. 比对三者：

   recorded calibration_source_identity
   ==
   promotion_resolved_source_identity
   ==
   runtime_source_identity

7. 三者一致才允许 promotion。
```

任何不一致都必须拒绝。

这条规则是 2.1c-b “promotion 不信 config 自报、复用 resolver 做结构验证” 在 per-channel source-aware 场景下的延伸。

---

### 9.7 Geometry guard：混合 source 不能混合几何

source-aware config 可以混合 raw 和 corrected，但不能混合几何坐标系。

最低要求：

```text
所有参与 conditioning 的 channel source_shape 必须一致。
所有参与 conditioning 的 channel source_shape 必须等于 step2_input_shape。
```

如果出现：

```text
raw OME = full WSI shape
corrected zarr = ROI crop shape
```

则必须拒绝 promotion。

禁止：

```text
crop / resize / transpose / normalize shape 来凑一致
把 full-image source 和 ROI-crop source 混为一谈
在几何不一致时继续 promotion
```

几何不一致是正确的拒绝条件，不是 bug。

---

### 9.8 Intensity space 与 mixed signal semantics

raw 和 corrected 可以在同一个 source-aware config 中混合，但这会引入混合信号语义：

```text
raw channel:
    raw -> remap

corrected channel:
    raw -> background correction -> remap
```

这不一定错误，但必须显式标记，不能无声发生。

Config / promotion report / smoke report 应记录：

```text
source_mixture_mode:
    homogeneous_raw
    homogeneous_corrected
    mixed_raw_corrected
```

如果是 `mixed_raw_corrected`，必须显示 warning：

```text
This config mixes raw and corrected marker sources.
Signal semantics are mixed: some channels use raw -> remap, others use raw -> correction -> remap.
This must be validated by source-aware smoke tests.
```

v14.6 必须专门验证 mixed source 场景。

---

### 9.9 Gi / camp / fusion source semantics

source-aware remap config 影响的不应只是 viewer 显示。

必须明确每个 channel 的 selected source 是否用于：

```text
remap gate
fuse_channels input
local contrast / _block_gi
camp arbitration
```

默认规则建议为：

```text
For a source-aware remap config, the selected per-channel source applies to all marker-derived segmentation signals for that channel, including remap conditioning, fusion input, and local-contrast / camp input, unless explicitly documented otherwise.
```

禁止隐式发生：

```text
viewer 显示 corrected
remap gate 用 corrected
camp / Gi 偷偷用 raw
```

如果未来决定 camp / Gi 永远使用 raw，则必须作为单独架构决策显式写明，并在 UI / config / report 中展示，不能隐式混用。

---

### 9.10 左侧下部：remap parameters

这里放 QuPath-style Brightness & Contrast 工具：

```text
histogram
min
max
brightness
contrast
gamma
auto
reset
save preview-only calibration config
```

---

### 9.11 右侧 viewer

右侧负责：

```text
raw preview
corrected preview
remapped preview
DAPI / marker overlay
split view
overlay view
single-channel view
zoom in / zoom out
pan
从 Tissue Preview 快速切换到其他区域
```

---

## 10. v14 与现有版本关系

### v13.1 / v13 当前状态

当前已有的一些工作仍然保留：

```text
source-identity promotion
HQ2/CSD raw_ome_only default
Step2 input geometry provenance / derivation
Step1.5 ChannelWorkbench 原型
```

但 v14 重新定义 UI 架构：

```text
Step1.5 不再作为主流程出现
Step1.5 功能并入 Step0
Step0 成为 Setup & Preprocessing Workbench
```

### 0ff1fc9 的新定位

`0ff1fc9` 中的 HQ2/CSD `raw_ome_only` 不废弃，而是降级为默认规则：

```text
无 promoted source-aware remap config:
    HQ2/CSD 默认 raw

有 promoted source-aware remap config:
    Step2 runtime 按 per-channel source identity 读取
```

因此：

```text
default raw
explicit promoted config overrides default
```

### 关于现有内部路径

为了降低风险，内部物理路径可以暂时保持兼容：

```text
<ROI>/step1_5/channel_remap_configs/
```

但 UI 上不再显示 Step1.5。

同时，provenance 字段必须诚实反映新架构：

```text
created_from_step = "step0_channel_conditioning"
ui_context = "Step0: Setup & Preprocessing / Channel Conditioning"
legacy_storage_path = "<ROI>/step1_5/channel_remap_configs/"
```

后续可以单独做 path migration，例如：

```text
<ROI>/step0/preprocessing/
<ROI>/step0/channel_remap_configs/
<ROI>/step0/background_correction/
```

当前不把路径迁移和 GUI 架构重构混在一起。

---

## 11. v14 不修改的内容

v14 当前阶段不做：

```text
不重写 Step1 Fusion / ROI construction 业务逻辑
不重写 segmentation algorithms
不改 h5ad feature extraction
不做 Step2 auto-load
不做 promotion smoke
不删除旧 run 输出
不强制迁移所有历史路径
```

---

## 12. v14 分阶段实施计划

### Phase v14.1：主导航与 Step0 页面架构

目标：

```text
统一顶部导航为 5 步
删除 Skip buttons
删除 Step1.5 主流程入口
Step0 改为 Setup & Preprocessing
Step0 主工作区包含 Background Correction + Channel Conditioning / Remap tabs
```

输出：

```text
Step0 页面承载 preprocessing workbench
Step1–Step4 暂时保持原业务逻辑
```

### Phase v14.2：Tissue Preview / ROI Navigator

目标：

```text
Load 成功后自动弹出 floating tissue preview
支持拖动、缩放、最小化 bar
支持 ROI 画 / 改 / 删
不限制 ROI 数量
显示当前主 viewer 的 viewport rectangle
支持 Full WSI / ROI mode
```

输出：

```text
TissueNavigatorPopup
与主 viewer 的 coordinate / viewport 同步接口
```

### Phase v14.3：HighQualityImageViewer 核心模块

目标：

```text
抽象当前最好用的 Channel Conditioning / Remap viewer
形成可复用核心模块
支持高画质、流畅 zoom/pan、多通道 overlay、split view
```

输出：

```text
HighQualityImageViewer
统一接口：
    set_source(...)
    set_channels(...)
    set_view_region(...)
    set_overlay(...)
    set_remap(...)
    get_viewport_rect(...)
    jump_to_roi(...)
```

### Phase v14.4：Background Correction tab 重构

目标：

```text
复用已有 Step0 背景校正逻辑
左侧 channel list + tophat/cuCIM parameters
右侧 HighQualityImageViewer 显示 raw/corrected preview
支持 patch preview 和 full ROI/full WSI correction
支持 correction 后画新 ROI 抽查
```

输出：

```text
corrected_channels.zarr 写出流程保持兼容
non-HQ2/CSD corrected support 保持
```

### Phase v14.5：Channel Conditioning / Remap 完整版

目标：

```text
每个 channel 支持 raw/corrected SourceRequest
保存 per-channel SourceRequest + actual CalibrationSourceIdentity + remap parameters
promotion 用 resolver 独立复算 per-channel source identity
promotion 做 per-channel geometry guard
promotion 显式报告 source_mixture_mode
Step2 runtime 按 promoted config 的 per-channel source identity 读取
禁止 silent fallback
```

输出：

```text
source-aware preview config
source-aware promotion
source-aware Step2 runtime
mixed-source geometry guard
mixed-source signal-semantics reporting
```

### Phase v14.6：集成测试与产品化 polish

目标：

```text
测试主导航
测试 Step0 不自动创建文件
测试 Background Correction 写 corrected_channels.zarr
测试 Channel Conditioning raw/corrected source-aware calibration
测试 promotion 独立 resolver 复算
测试 viewer zoom/pan/viewport 不重置
测试 Tissue Preview ROI 跳转
测试高画质 viewer 不再马赛克
```

source-aware smoke 必须覆盖：

```text
raw-only config smoke
corrected-only config smoke
mixed raw/corrected config smoke
source mismatch refusal smoke
geometry mismatch refusal smoke
unknown geometry refusal smoke
mixed source signal-semantics warning smoke
```

输出：

```text
v14 可用主流程
Step0 具备 MACSiQView-style preprocessing workbench 形态
source-aware calibration / promotion / runtime 链条真实验证
```

---

## 13. v14 验收标准

### GUI 架构验收

```text
顶部只有 5 步主导航
没有 Step1.5 主流程 tab
没有 Skip buttons
Step0 是 Setup & Preprocessing
Step0 只有两个主工作 tab：
    Background Correction
    Channel Conditioning / Remap
```

### Tissue Preview 验收

```text
Load 成功后自动弹出
可拖动
可缩放
可最小化成 bar
可恢复
支持 ROI 画 / 改 / 删
不限 ROI 数量
显示当前 viewer viewport rectangle
```

### Viewer 验收

```text
高画质
流畅 zoom / pan
不马赛克
参数变化不重置视野
支持 overlay / split / single channel
支持 Tissue Preview 跳转
```

### Background Correction 验收

```text
DAPI 在 channel list 中
左侧上 2/3 channel list + search
左侧下 1/3 tophat/cuCIM/background parameters
右侧 viewer raw/corrected 对比
支持 patch preview
支持 full ROI/full WSI correction
支持 correction 后新 ROI 抽查
```

### Channel Conditioning 验收

```text
DAPI 在 channel list 中
每个 channel 支持 raw/corrected SourceRequest
保存 preview-only calibration config
config 记录 actual CalibrationSourceIdentity
promotion 独立 resolver 复算 source identity
promotion per-channel geometry guard
promotion 报告 source_mixture_mode
Step2 runtime 按 promoted per-channel source 读取
禁止 silent fallback
```

---

## 14. 关键风险

### 风险 1：只改 UI，不改 source identity

如果 UI 允许选择 corrected，但 config / promotion / runtime 不记录和验证 corrected source，会重新引入 source mismatch。

必须禁止。

### 风险 2：source identity 变成 GUI 声明

如果 config 只是复制 GUI 下拉框文字，例如 `source_kind = corrected`，这不是 source-aware calibration，而是 source declaration。

必须禁止。

### 风险 3：source identity 变成 config 自报

即使 config 记录了 backend handle 生成的 source identity，promotion 也不能直接信。  
promotion 必须用 resolver 独立复算并交叉验证。

### 风险 4：mixed source 混合几何

raw OME 与 corrected zarr 可能不在同一几何坐标系。  
所有 conditioning source 必须与 step2_input_shape 一致，否则必须拒绝。

### 风险 5：mixed source 混合信号语义

raw channel 与 corrected channel 进入同一个 fusion / camp 可能混合单层与双层信号变换。  
这必须显式标记，并由 v14.6 smoke 验证。

### 风险 6：重写 viewer 导致画质更差

v14 不应该重写一套低质量 QLabel/QPixmap viewer。  
必须复用并抽象当前表现最好的 viewer，并继续优化。

### 风险 7：Background Correction 和 Channel Conditioning 参数混淆

Background Correction 参数是：

```text
Tophat / cuCIM / background correction parameters
```

Channel Conditioning 参数是：

```text
histogram / min / max / brightness / contrast / gamma
```

两者不能混用。

### 风险 8：preview 弹窗变成主 viewer

Tissue Preview 只做空间导航和 ROI 管理，不做 channel overlay 或 correction preview。

---

## 15. 当前最高优先级

在完成 v14 Step0 架构前：

```text
不做 promotion smoke
不做 5f-b
不继续修 Step2 auto-load
不让用户直接保存 step2_ready remap config
```

允许：

```text
用户在 Step0 Channel Conditioning 中保存 preview-only calibration config
```

不允许：

```text
GUI 直接保存 step2_ready config
```

最高优先级：

```text
重构 GUI 主流程
合并 Step1.5 到 Step0
建立 Step0 Setup & Preprocessing Workbench
抽象 HighQualityImageViewer
建立 Tissue Preview / ROI Navigator
定义 source-aware calibration pipeline
```

---

## 16. 一句话总结

Block01 v14 的核心不是“加一个 Step1.5”，而是：

```text
把 Block01 的前处理阶段升级成 MACSiQView-style、QuPath-like、napari-quality 的
Setup & Preprocessing Workbench。
```

同时，v14 的 Channel Conditioning 不是简单的 raw/corrected 下拉框，而是：

```text
per-channel source-aware calibration
+ actual pixel-source identity
+ promotion independent resolver recomputation
+ geometry guard
+ explicit mixed-source signal semantics
+ source-aware smoke validation
```

Step0 是新的主战场。  
Step1.5 从 UI 中消失。  
Background Correction 和 Channel Conditioning / Remap 统一进入 Step0。  
高画质 viewer、Tissue Navigator、source-aware remap 是 v14 的底层核心能力。
