# 软件包结构

项目的安装与快速上手见 [README](../README.md) 或 [README.zh-CN](../README.zh-CN.md)，全部 MCP 工具清单见[工具参考](tool-reference.md)。

## 模块划分

源码采用标准 `src` 布局，统一放在 `src/pvsyst_auto/` 下，按领域切分为 9 个子包和 1 个结果解析模块：

| 目录 / 模块 | 主要职责 | 核心文件 |
|---|---|---|
| `api` | MCP 协议层、引导层（`instructions`/Prompt/Resource）、入参校验、工具分组注册与进程内共享上下文 | `context.py`、`projects.py`、`schemas.py`、`simulation.py` |
| `workflows` | 串联文件准备、零模板合成、批处理扫描、自动优化、PV+BESS 两阶段寻优、一站式可研编排、Plotly 交互报告与交付打包的端到端工作流 | `batch.py`、`bess_optimize.py`、`design.py`、`house.py`、`optimize.py`、`report.py`、`revision.py`、`simulation.py`、`sourced_design.py`、`utility.py` |
| `engineering` | 自动选型、物理诊断引擎、财务/TOU/两部制需量/项目融资与敏感性评估、可研简报核对、工程参数交接单与多方案结果对比 | `comparison.py`、`diagnostics.py`、`economics.py`、`review.py`、`sizing.py` |
| `project` | `.PRJ` / `.VCx` 文本解析、并网/离网零模板工程合成器、`pvBiFacial` 双面建模、原地朝向转换、棚架/跟踪/季节朝向、原生 PDF `TEcoEvaluation` 与逆变器/组串拓扑 | `bifacial.py`、`circuit.py`、`paths.py`、`standalone.py`、`synthesize.py`、`template.py`、`tracking.py`、`variant_updates.py`、`variants.py` |
| `components` | `.PAN` / `.OND` / `.BTR` / `.GEN` 组件库管理与 mtime 元数据索引、单/三电压曲线编辑、商业字段、无模板单二极管/三电压 OND/锂电 BTR/柴发 GEN 合成与 `.PAN` 改参重拟合 | `builder.py`、`editor.py`、`single_diode.py`、`store.py` |
| `scene` | `.pvc2` 3D 房屋与光伏网格构建、有限 3D 地面多边形裁剪与 3D DEM 连续地形贴合（`pvShdGround`）、XML 回读及空间坐标比对 | `author.py`、`mapping.py`、`reader.py`、`terrain.py` |
| `energy` | 负荷曲线（含 `household`/`probability`）、AC 耦合储能系统、弱网确定性停电生成、TOU 48 槽 `PowerShift` 排程规划、多级 MV/HV 变压器、AC/DC 线缆、光谱修正、系统停运与详细损失字段读写 | `dc.py`、`profiles.py`、`storage.py`、`system.py`、`transformers.py` |
| `weather` | 地理编码、Open-Meteo ERA5 / PVGIS v5.3 TMY / NASA POWER 下载、亚小时 CSV 与 `.SIT`/`.MET` 制备与核对 | `acquisition.py`、`preparation.py`、`review.py` |
| `native` | `PVsystCLI.exe` 命令行封装、免 GUI 3D 阴影/精细接线与地平线/P50/限发/负荷构建、以及私有非输入桌面进程隔离 | `_winlaunch.py`、`cli.py`、`headless.py`、`load.py` |
| `results.py` | 仿真结果 CSV 解析与逐时/亚小时指标汇总、年度能量平衡校核与双结果逐时对比 | `results.py` |

阅读代码时的经验法则：
- 想看某个 MCP 工具怎么接请求、转换异常，去 `api/` 下找对应分组文件。
- 想看一整套任务（比如基于模板改参数并跑全年仿真）怎么编排，看 `workflows/`。
- 想看某个 PVsyst 字段在文件里叫什么、改值时要联动改哪些 `Flags` 位掩码，去 `project/`、`components/`、`energy/`、`weather/` 或 `scene/`。
- 想看怎么调 `PVsystCLI.exe`、私有桌面隔离或免 GUI 原生场景/参数构建，看 `native/`。

Windows 上所有原始 CLI 调用统一经 `native/cli.PVsystCLI._run` 启动：在 Windows 上将子进程隔离到私有非输入桌面（仅对普通纯 CSV 数值仿真拒绝 `DESKTOP_CREATEWINDOW`，PDF/批处理/`-rst:1` 阴影重算允许建窗），读取当前进程窗口站而不再硬编码 `WinSta0`，并按用户配置文件串行恢复 `DataPath`/`WorkspaceN`（该恢复机制不协调独立启动的 `PVsystCLI` 进程，且 MCP 宿主被强制终止时无法恢复设置）；非 Windows 保持原生 `subprocess.run`。

## MCP 上下文与参数校验

`api/context.py` 维护了进程内唯一的 `PVsystCLI` 实例和工作区重入锁（`RLock`），挂载了前置中间件 `reject_unknown_tool_arguments`，并根据环境变量 `PVSYST_TOOL_GROUPS` 动态导入选定的工具组模块并启动 stdio 服务（共 10 个分组；未设置该变量时默认加载 8 个核心分组共 48 个工具，设为 `all` 时加载全部 10 个分组共 75 个工具）。所有 75 个工具都会根据 Python 函数签名严格校验顶层入参，一旦传了未知字段名会直接返回明确的错误提示，避免因为参数名拼写错误而在写文件时静默漏掉配置。复杂嵌套参数的结构校验集中在 `api/schemas.py`、`api/projects.py` 与 `api/simulation.py`。

需要注意的是，进程内的 `RLock` 只负责同步当前 MCP 服务进程内部的多线程调用；如果你同时在桌面上开着 PVsyst GUI 手动改同一个工程，还是要靠文件 SHA-256 校验（`expected_sha256`）来防止互相覆盖。

## 打包与兼容入口

`pyproject.toml` 管理构建元数据和运行依赖。安装后可通过下面任一方式启动服务：

```powershell
py -m pip install -e .
py -m pvsyst_auto
# 或者直接执行 console script：
pvsyst-mcp
```

仓库根目录下的 `pvsyst_mcp_server.py` 仅作为向后兼容的启动脚本保留，内部直接调用 `pvsyst_auto.api.context.main`。

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
| `pvsyst_engineering_delivery` | `pvsyst_auto.workflows.revision` |
| `pvsyst_native_scene_mapping` | `pvsyst_auto.scene.mapping` |
| `pvsyst_weather_flow` | `pvsyst_auto.weather.preparation` |
| `pvsyst_results` | `pvsyst_auto.results` |
