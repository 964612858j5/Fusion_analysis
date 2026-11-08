# RT2 正式环境切换记录（2026-11-08）

验收提交：`6c68960`（记录提交 `8342c1b`）。本次把**日常使用环境**切到与验收相同的配置。

| 项 | 值 |
|---|---|
| 环境 | `/sda1/fusionflux/block01/envs/fusion_test2`（`scripts/block01` 的解释器） |
| 新增包 | `PySide6-Essentials==6.11.3`、`shiboken6==6.11.3`（`pip install --no-cache-dir`） |
| 系统依赖 | `libxcb-cursor0` 0.1.1-4ubuntu1（Qt ≥ 6.5 的 xcb 插件依赖） |
| PyQt5 | **保留未卸载**（日常环境里仍共存；`block01.qt` 不再导入它） |
| 绑定自检 | `block01.qt.BINDING == "PySide6"`；GPU 后端 `runtime-kernels` |
| 启动自检 | 经 `scripts/block01` 启动 → 关闭，进程退出码 **0**，无 traceback |

## 兼容性说明

日常环境的安装记录（`.block01/install.env`）指向 `Fusion_analysis-17` 这个 checkout，
其工作区现处于 `8342c1b`（只比验收提交多一条文档），代码与验收版本一致。
`PyQt5` 与 `PySide6` 可以共存一环境：项目只有一个 Qt 导入入口（`block01.qt`），
里面已经改为 PySide6；pyqtgraph 通过 `PYQTGRAPH_QT_LIB` 跟随同一绑定。

## 回退

如需回到 PyQt5：`pip uninstall PySide6-Essentials shiboken6`，并把代码 checkout 到
`91d8771`（最后一个 PyQt5 提交）。两者的依赖文件都在 `envs/`、`scripts/install_block01.sh`
的 git 历史里。

## Windows

仍**未验证**：仓库里没有 Windows 安装器/启动器（只有 bash 脚本），见
`docs/v17_windows_acceptance.md`。
