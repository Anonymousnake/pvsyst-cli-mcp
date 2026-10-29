# 软件包结构

项目的安装与快速上手见 [README](../README.md) 或 [README.zh-CN](../README.zh-CN.md)，全部 MCP 工具清单见[工具参考](tool-reference.md)。

## 模块划分

源码采用标准 `src` 布局，统一放在 `src/pvsyst_auto/` 下，按领域切分为 9 个子包和 1 个结果解析模块：

| 目录 / 模块 | 主要职责 | 核心文件 |
|---|---|---|
| `api` | MCP 协议层、入参校验、工具分组注册与进程内共享上下文 | `app.py`、`context.py`、`schemas.py` 及 8 个分组模块 |
| `workflows` | 串联文件准备、GUI/CLI 调用与结果校验的端到端工作流 | `revision.py`、`house.py`、`simulation.py`、`blocks.py`、`delivery.py` |
| `engineering` | 可研简报核对、工程参数交接单、输入复核与多方案结果对比 | `brief.py`、`handoff.py`、`review.py`、`comparison.py` |
| `project` | `.PRJ` / `.VCx` 文本解析、逆变器/组串拓扑、模板筛选与朝向控制 | `variants.py`、`circuit.py`、`tracking.py`、`paths.py`、`fileio.py` |
| `components` | `.PAN` / `.OND` / `.BTR` / `.GEN` 组件库管理、曲线编辑、商业字段与引用创建 | `store.py`、`editor.py`、`pan.py`、`battery.py`、`curves.py`、`identity.py` |
| `scene` | `.pvc2` 房屋与光伏阵列 3D 网格构建、XML 回读及空间坐标比对 | `author.py`、`reader.py`、`mapping.py`、`state.py` |
| `energy` | 负荷曲线、储能系统、电气参数与详细损失字段读写 | `profiles.py`、`storage.py`、`system.py`、`power.py`、`ac.py` |
| `weather` | 地理编码、Open-Meteo ERA5 下载、时基转换、`.SIT`/`.MET` 准备与核对 | `acquisition.py`、`preparation.py`、`review.py`、`csv.py` |
| `native` | `PVsystCLI.exe` 命令行封装与受控的 Windows GUI 自动化操作 | `cli.py`、`project.py`、`scene.py`、`horizon.py`、`dc.py` |
| `results.py` | 仿真导出的 CSV 与 PDF 解析、年度能量平衡校核与双结果逐时对比 | `results.py` |

阅读代码时的经验法则：
- 想看某个 MCP 工具怎么接请求、转换异常，去 `api/` 下找对应分组文件。
- 想看一整套任务（比如基于模板改参数并跑全年仿真）怎么编排，看 `workflows/`。
- 想看某个 PVsyst 字段在文件里叫什么、改值时要联动改哪些 `Flags` 位掩码，去 `project/`、`components/`、`energy/`、`weather/` 或 `scene/`。
- 想看怎么调 `PVsystCLI.exe` 或怎么用 Win32 消息控制 PVsyst 窗口，看 `native/`。

Windows 上所有原始 CLI 调用统一经 `native/cli.PVsystCLI._run` 在私有非输入桌面启动的隔离机制、边界与验收状态，见[原生 CLI 私有非输入桌面隔离](native-cli-isolation.md)。

## MCP 上下文与参数校验

`api/context.py` 维护了进程内唯一的 `PVsystCLI` 实例和工作区重入锁（`RLock`），并挂载了一个前置中间件 `reject_unknown_tool_arguments`。所有 82 个工具都会根据 Python 函数签名严格校验顶层入参，一旦传了未知字段名会直接返回明确的错误提示，避免因为参数名拼写错误而在写文件时静默漏掉配置。复杂嵌套参数的结构校验集中在 `api/schemas.py`。

`api/app.py` 根据环境变量 `PVSYST_TOOL_GROUPS` 动态导入选定的工具组模块并启动 stdio 服务。未设置该变量时默认加载全部 8 个分组。

需要注意的是，进程内的 `RLock` 只负责同步当前 MCP 服务进程内部的多线程调用；如果你同时在桌面上开着 PVsyst GUI 手动改同一个工程，还是要靠文件 SHA-256 校验（`expected_sha256`）来防止互相覆盖。

## 打包与兼容入口

`pyproject.toml` 管理构建元数据和运行依赖。安装后可通过下面任一方式启动服务：

```powershell
py -m pip install -e .
py -m pvsyst_auto
# 或者直接执行 console script：
pvsyst-mcp
```

仓库根目录下的 `pvsyst_mcp_server.py` 仅作为向后兼容的启动脚本保留，内部直接调用 `pvsyst_auto.api.app.main`。

## Python 模块导入路径对照

外部 Python 脚本如果只需要调 CLI，直接从顶层包导入即可：

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

早期脚本如果直接 import 了仓库根目录下的旧扁平模块名，需要按下面的对照表改成新的包路径：

| 旧模块名 | 新包路径 |
|---|---|
| `pvsyst_cli` | `pvsyst_auto.native.cli` |
| `pvsyst_components` | `pvsyst_auto.components.store` |
| `pvsyst_component_editor` | `pvsyst_auto.components.editor` |
| `pvsyst_variants` | `pvsyst_auto.project.variants` |
| `pvsyst_simulation` | `pvsyst_auto.workflows.simulation` |
| `pvsyst_house_task` | `pvsyst_auto.workflows.house` |
| `pvsyst_engineering_task` | `pvsyst_auto.workflows.revision` |
| `pvsyst_engineering_delivery` | `pvsyst_auto.workflows.delivery` |
| `pvsyst_scene_native` | `pvsyst_auto.native.scene` |
| `pvsyst_native_scene_mapping` | `pvsyst_auto.scene.mapping` |
| `pvsyst_weather_flow` | `pvsyst_auto.weather.preparation` |
| `pvsyst_results` | `pvsyst_auto.results` |
