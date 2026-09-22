# G3.2b.4F — Step0 Save 后进入 Step1 的 GUI 冻结

## 真机定位

用户报告首次进入 Step1 曾需 5–6 秒且整个窗口无响应。2026-09-22 同一项目再次复现时主观等待约 2–3 秒；`BLOCK01_PERF` 在真实桌面、真实 TIM3/CXCR6 产物上记录如下（单位 ms）：

| 分段 | 耗时 |
|---|---:|
| Step0 Save 后 handoff 接受 | 65 |
| Step1 入口同步总耗时 | 1919 |
| 来源/Viewer stack 打开 | 127 |
| GPU layer `show()`，含首次 PyOpenGL 导入/GL 初始化 | 1156 |
| 共享通道栏移入 Step1（Qt `layout.addWidget`） | 540 |
| GUI 心跳最大间隔 | 2104 |

证据文件为 `/tmp/step1_entry_live.log`；同期 `/tmp/save_diag.log` 的 `display.scope`→`context.active` 间隔约 1819 ms。此次**没有复现 5–6 秒**，不得把 1.9 秒当作先前每一次冻结的数值。来源打开、handoff 校验、corrected coarse 和 scheduler join 不是此次主导项。

## 已撤回的预加载试验与对照

曾在 Save 成功并接受 handoff 后，用短命 daemon thread 提前导入 `OpenGL.GL` Python 模块。用户真机第二轮进入 Step1 后报告「没有任何图像」，因此这项试验**未通过画面验收，已从生产代码撤回**；当前只保留无副作用的分段计时。尚未做同一会话内的 A/B，不能断言后台导入就是空白画面的根因。

同一真实产物、同一屏幕外 RTX 4090 台架：未提前导入时首次入口约 1763–1933 ms；提前导入模块的对照约 1178 ms；走实际 Save 完成回调并待后台导入结束后约 792 ms，backend=`gpu`。用户真机第二轮日志的入口为 1052 ms、最长 GUI 间隔 1441 ms，其中 GL `show()` 202 ms、共享通道栏搬移 500 ms；**但无图像，故这些耗时不是有效修复成绩**。数字受进程首次导入、OS 缓存和 Qt 布局波动影响，不是严格配对真机 A/B。

试过而未采用：提前 `mount.open()` 可把点击耗时降至约 602 ms，但会把约 1313 ms 的 GUI 停顿移到 Save 完成时；`setUpdatesEnabled(False)`、搬移前隐藏 dock 都未减少 Qt `layout.addWidget` 的约 0.6 秒。没有为数字做更多架构改造。

## 验证与边界

- `test_step0_step1_handoff_contract.py` + `test_step1_viewer_takeover.py`：59 passed。
- `test_step1_viewer_host.py` + `test_step1_viewer_mount.py` + `test_step1_gpu_overview_skip.py`：45 passed、3 个原有 offscreen GPU skip。真实 GPU 对照 backend=`gpu`。
- 当前生产改动是 `ui/main_window.py`、`ui/step1_viewer_mount.py`、`ui/step1_viewer_host.py` 的诊断计时，以及 `ui/widgets/channel_dock/global_dock.py` 对已有宿主间搬移的一处显式 `setParent()`。后台和启动时的两种模块预导入均已撤回。来源身份、首帧发布、Step0 Save、corrected 产物、相机、通道栏实例和 CPU fallback 规则未改。
- 回退后给 `BLOCK01_PERF=1` 增加一次延后 1.5 秒的只读 `step1.entry.picture` 快照，记录 GPU 图层可见性/尺寸、descriptor 通道、coarse/fine 到达数与错误；普通运行关闭。回退后的 focused 回归 65 passed、3 个既有离屏 GPU skip；加入该快照后的 Step1 takeover + handoff 59 passed。
- 所有台架运行从 `/tmp` 启动，真实项目只读；`cufile.log`、`docs/tma_study_mode.md` 未触碰。
- 回退后用户已确认图像恢复；通道栏搬移的最终真机验收见文末。先前那次 5–6 秒的额外差额没有独立重现或归因。

## 回退确认、被拒绝的同线程预导入

用户重启后确认 Step1 图像恢复，入口主观约 1–2 秒；该轮诊断日志测得入口 **2442 ms**，其中 GL layer `show()` **1311 ms**、来源打开 **254 ms**、通道栏搬移 **647 ms**。后台预导入与空白的因果关联因此很强，但尚未隔离其他会话变量；无论如何该方案已撤回。

随后试了在 `MainWindow.__init__()` 所在的 GUI 线程、窗口显示之前预导入 `OpenGL.GL`。真实项目只读的 XCB/RTX 4090 台架测得主窗口构造约 **701–718 ms**、Step1 点击约 **1290–1319 ms**，真实 GPU 像素门通过；但这不是严格配对的真机 A/B。用户真机确认画面和 TIM3/CXCR6 正常，却感觉进入反而更慢。日志证实入口 **2285 ms**、GUI 最长无响应 **2611 ms**：GL `show()` 降到 **157 ms**，但共享通道栏搬移涨到 **1734 ms**。启动时预导入成本 **129 ms**。因此**同线程预导入也已撤回**，不能用台架数字替代这次失败的真机体验。

## 当前候选：缩短共享通道栏搬移

只读真实 GPU 台架中，通道栏从 Step0 宿主移到 Step1 时，`removeWidget()` 约 **0.02 ms**，直接 `addWidget()` 为 **461–537 ms**。若在加入新布局前对同一个 dock 显式 `setParent(new_host)`，`setParent()` 为 **48–88 ms**，随后 `addWidget()` 约 **0.02 ms**。六次交替、各三次：直接路径 Step1 入口 **1745–1767 ms**，显式 reparent 路径 **999–1372 ms**；每次都通过产品通道命令启用 TIM3，GPU 帧缓冲有 **161 个非黑像素**。这只是 XCB 台架收益，不冒充真机。

产品只在 **已有旧 layout** 时显式 reparent，初次挂载保留 Qt 原路径；不更换 dock/list/row 对象，不改焦点和滚动恢复。产品代码下的真实 GPU 台架入口 **1298 ms**，TIM3 coarse+fine 齐、帧缓冲有像素。保护合集 **151 passed / 42 skipped / 1 failed**；失败是 `test_the_step0_panel_looks_like_the_baseline_panel` 固定像素几何断言，单独运行且把本块新行完全撤掉后仍以**相同坐标**失败，已确认不由本块造成。其余身份、搜索、滚动、选择、焦点和跨 Step 往返门通过。最终真机结果见下。`cufile.log` 与 `docs/tma_study_mode.md` 未动。

## 真机验收（2026-09-22）

用户按 `bash /tmp/run_step1_trace.sh` 重启，完成 Step0 Save→Step1，反馈：图像正常，通道可以立刻加载，进入 Step1 **不到 1 秒**，整个窗口**没有感到卡住**。本次日志中的 `step1.entry.total` 为 **994.70 ms**，`step1.entry.dock_mount` 为 **93.14 ms**；延后诊断快照记录 backend=`gpu`、图层可见、DAPI coarse/fine 齐且无错误。用户确认通道加载正常；日志快照只采样了当时的 DAPI，不能单独作为 TIM3/CXCR6 像素证据。

同一日志的 GUI 心跳仍有 **1712 ms** 间隔（从入口前延续到入口返回后的事件处理），因此“没有感到卡住”是用户本次的体验结论，并非严格证明事件循环全程无长间隔。当前验收针对这次入口体验通过；该心跳记录为后续性能观测，不把它伪称为低于 1 秒。没有为此继续改动已验收路径。两种 OpenGL 预导入均保持撤回；未提交、未 push。
