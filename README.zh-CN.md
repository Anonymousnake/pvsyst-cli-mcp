# PVsyst CLI MCP

[English](README.md) | **简体中文**

Windows 下配合本地 PVsyst 8 使用的 MCP 服务端与 Python 封装库。它一方面封装了官方 `PVsystCLI.exe` 的仿真、气象转换和许可查询命令；另一方面提供全套无 GUI 的原生文本解析与零模板合成引擎（`.PRJ`、`.VCx`、`.PAN`、`.OND`、`.BTR`、`.GEN`、`.SIT`），覆盖双面地面阵列与有限 3D 地块、3D 住宅与遮挡物（`.pvc2` -> `.VCx`）、规格书单二极管组件合成、多源气象获取、物理异常诊断与有界自动优化、TOU 经济性/光储套利评估以及 Plotly 交互式 HTML 工程报告。

运行本项目需要本机已安装 PVsyst 并配置好有效许可证。

## 常用任务入口

| 要做的事 | 入口工具 | 详细文档 |
|---|---|---|
| 零模板执行固定倾角或地面光伏棚架/跟踪器设计（单/双面、有限 3D 地块 `site_polygon` + 障碍物 + `base_slope_deg`、可选自动选型） | `pvsyst_simulate_design`、`pvsyst_suggest_sizing` | [单方案设计输入](docs/single-design-inputs.md) |
| 按电气参数筛选工作区/内置组件，或按规格书无模板合成自定义 `.PAN`/`.OND`（单/三电压）/ 锂电 `.BTR`（支持已有 `.PAN` 改参可选单二极管重拟合） | `pvsyst_list_components`、`pvsyst_build_sourced_component`、`pvsyst_update_component` | [规格书组件构建](docs/sourced-components.md) |
| 拉取权威气象数据（`Open-Meteo ERA5`、`PVGIS v5.3 TMY`、`NASA POWER`）并可选一键制备 `.SIT`/`.MET` | `pvsyst_resolve_location`、`pvsyst_acquire_weather`、`pvsyst_prepare_weather` | [气象文件制备](docs/weather-preparation.md) |
| 单次 CLI 批量参数扫描（倾角、间距、反照率、积灰、限发）并自动排序 | `pvsyst_run_batch_sweep` | [工具参考](docs/tool-reference.md) |
| 对仿真结果 CSV（及 `.VC0` 归因）做物理损失链诊断，并基于克隆变体执行有界“诊断 → 修正 → 重仿”闭环 | `pvsyst_diagnose_results`、`pvsyst_optimize_variant` | [工具参考](docs/tool-reference.md) |
| 按 TOU 电价自动派生 `PowerShift` 48 槽两充两放策略，并运行两阶段混合 PV+BESS 容量寻优（快筛热力图/Pareto + 可选实机校验） | `pvsyst_plan_tou_storage_schedule`、`pvsyst_optimize_bess_sizing` | [工具参考](docs/tool-reference.md) |
| 评估固定/分时（TOU）/两部制需量电价、电池中途更换、敏感性龙卷风分析、25 年现金流、LCOE、NPV、IRR 与回收期 | `pvsyst_evaluate_economics` | [工具参考](docs/tool-reference.md) |
| 一站式运行端到端光伏/光储可研（组件合成 → 仿真/断点复用 → 诊断 → 光储寻优 → 经济性 → 中英双语 Plotly 交互报告）或交付打包 | `pvsyst_run_feasibility_study`、`pvsyst_generate_report`、`pvsyst_export_engineering_delivery` | [工程修订与交付](docs/engineering-delivery.md) |
| 生成屋顶光伏与周边遮挡 3D 场景、原生导入并跑全年阴影仿真（`linear` 或 `module_layout`） | `pvsyst_run_house_task` | [住宅建模任务](docs/native-house-task.md) |
| 准备逐时或亚小时（`5/10/15/30 min`）气象并评估 AC 耦合光储方案 | `pvsyst_prepare_weather`、`pvsyst_create_grid_storage_system`、`pvsyst_run_simulation` | [AC 光储评估](docs/engineering-storage.md) |
| 读取现有工程参数、隔离改参或进行多方案结果横评 | `pvsyst_project_handoff`、`pvsyst_run_engineering_task`、`pvsyst_compare_engineering_scenarios` | [多方案对比](docs/engineering-scenarios.md) |
| 可研简报核对与固定分块拼装（需 `expert` 组） | `pvsyst_review_engineering_brief`、`pvsyst_prepare_block_project` | [工程简报预审](docs/engineering-brief.md) |

服务默认仅注册 **8 个默认核心组共 46 个高频工具**以降低模型上下文开销；`PVSYST_TOOL_GROUPS` 显式列出的组会替换这 8 个默认组，单独设置 `admin` 或 `expert` 只加载对应组。设置 `PVSYST_TOOL_GROUPS=all` 可加载全部 **75 个工具**；若要保留默认组并追加 `expert`，请完整写成 `setup,projects,components,weather,storage,simulation,results,engineering,expert`。从 `v0.1.0` 升级的完整迁移说明见[工具参考中的升级小节](docs/tool-reference.md#upgrading-from-v010)。

## 安装与环境准备

环境要求：
- Windows 系统，Python 3.10+
- 已安装 `PVsystCLI.exe`（已在 8.0.6、8.1.6 rev49278 与 8.1.7 rev49382 验证）
- 仅在显式使用旧版 `scene_import_method="gui"` 时需要桌面 GUI（中文版 8.1.6/8.1.7，96 DPI）；3D 住宅场景、周边遮挡物、`module_layout` 与 DC 线缆均已默认免 GUI 原生写入

在仓库根目录执行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.7\PVsystCLI.exe'
$env:PVSYST_WORKSPACE = 'C:\path\to\PVsyst8.1_Data'
.\.venv\Scripts\python.exe -m pvsyst_auto
```

包依赖（MCP SDK 2.x、`pydantic` 2.x、交互图表库 `plotly` 与 Windows 时区库 `tzdata`）统一定义在 `pyproject.toml` 中。执行 `pip install -r requirements.txt` 同样会以可编辑模式安装本包；常规安装用 `pip install .`。

## 接入 MCP 客户端

把客户端的启动命令指向虚拟环境里的 Python 解释器。用 `-m pvsyst_auto`（或安装后生成的 `pvsyst-mcp` 命令）启动时不依赖客户端当前所在的工作目录：

```json
{
  "mcpServers": {
    "pvsyst": {
      "command": "C:/path/to/pvsyst-cli-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "pvsyst_auto"],
      "env": {
        "PVSYST_CLI": "C:/Program Files/PVsyst8.1.7/PVsystCLI.exe",
        "PVSYST_WORKSPACE": "C:/path/to/PVsyst8.1_Data"
      }
    }
  }
}
```

仓库根目录保留了 `pvsyst_mcp_server.py` 兼容壳，老配置直接指向该脚本也能正常启动。改了环境变量或更新代码后记得重启 MCP 客户端。

连上后，服务端会向 MCP 客户端提供 `instructions`、6 个标准工作流 Prompt（`design_fixed_or_utility_pv`、`design_rooftop_3d_pv`、`design_pv_with_storage`、`diagnose_and_optimize_simulation`、`ci_bess_feasibility_study`、`optimize_bess_sizing`）和 5 个按需资源（`pvsyst://guide/workflows`、`pvsyst://guide/variables`、`pvsyst://workspace/projects`、`pvsyst://workspace/components`、`pvsyst://workspace/results`）。建议先调用 `pvsyst_capabilities` 与 `pvsyst_license_info` 确认本机 CLI 能力与剩余许可配额（PVsyst 的 GUI 许可与 CLI 许可分开计数；激活/停用等写操作仍在 `admin` 组按需开启）。

### 按需调整暴露的工具组

未配置 `PVSYST_TOOL_GROUPS` 时，服务默认仅暴露 **46 个高频核心工具**（涵盖 `setup`、`projects`、`components`、`weather`、`storage`、`simulation`、`results`、`engineering` 八个默认组），大幅减少 LLM 上下文占用。可通过 `env` 中的 `PVSYST_TOOL_GROUPS` 精确控制；显式列出的组会替换默认组：

- `"PVSYST_TOOL_GROUPS": "all"`：加载全部 **75 个工具**（包含默认组及 `admin`、`expert`）。
- `"PVSYST_TOOL_GROUPS": "setup,projects,components,weather,storage,simulation,results,engineering,expert"`：保留默认组并追加 `expert`，共 71 个工具。
- `"PVSYST_TOOL_GROUPS": "setup,engineering,results"`：只加载这三组的 14 个工具。

合法的组名（区分大小写）：`setup`、`projects`、`components`、`weather`、`storage`、`simulation`、`results`、`engineering`、`admin`、`expert`（或单独传入 `all`）。传了空项、重复项或拼错的组名会在启动时直接报错退出，免得静默漏挂工具。

## 状态字段说明

高层工作流工具在返回结果里用固定的状态字段区分“只做了预检”、“只生成了文件”和“真正跑完了 PVsyst 仿真”：

| 状态 / 标志 | 含义 |
|---|---|
| `ready` | `dry_run` 预检通过，没写盘也没调 PVsyst |
| `prepared` | 工程或输入文件已落盘，但还没跑仿真 |
| `stage=completed` 且 `native_consumption_verified=true` | PVsyst 已跑完仿真，且输出文件、全年时间轴和数值列检查通过 |
| `needs_review` | 流程跑通了，但带有需要人工过目的复核项（例如部分参数沿用了模板默认值、气象时间基准有提示、或未填全工程依据出处） |
| `blocked` / `failed` | 前置检查未过或仿真报错；现场工程与清单会保留在工作区，排查 `issues` 后换个新工程名重试即可，工具不会自动覆盖已有文件 |

## 目录结构

```text
src/pvsyst_auto/
├── api/          # MCP 服务注册、协议引导（Prompt/Resource）、严格参数校验、8 个工具组入口
├── workflows/    # 端到端业务流（统一设计、地面 3D、批处理扫描、自动优化、住宅任务、Plotly 交互报告、交付打包）
├── engineering/  # 自动选型、物理诊断、财务/TOU 经济性评估、工程输入交接、可研简报与多方案对比
├── project/      # .PRJ/.VCx 解析、零模板合成器、pvBiFacial 双面模型、子阵电路树与朝向控制
├── components/   # .PAN/.OND/.BTR/.GEN 管理 + mtime 索引缓存、曲线编辑、规格书单二极管/Mode-1 无模板合成
├── scene/        # PVCollada (.pvc2) 3D 房屋与有限 3D 地面多边形裁剪排布及阴影构建
├── energy/       # 逐时负荷、AC 耦合储能、详细损失标量、功率因数与并网限发
├── weather/      # Open-Meteo ERA5 / PVGIS v5.3 TMY / NASA POWER 下载、亚小时 CSV 与 .SIT/.MET 转换
├── native/       # PVsystCLI.exe 进程封装、免 GUI 3D 阴影/精细接线构建与旧版 Win32 辅助桥
└── results.py    # 仿真结果 CSV 解析与逐时/亚小时指标汇总
```

各子模块的详细职责与旧版扁平模块导入路径对照见[软件包结构](docs/architecture.md)，PVsyst 各功能点的支持情况见[功能入口地图](docs/native-entry-map.md)。

如果在 Python 脚本里直接调用底层 CLI 封装：

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

## 开发与测试

集成测试和单元测试分别放在 `tests/integration/` 与 `tests/unit/` 下，需要连接本机真实 PVsyst 环境的端到端测试放在 `tests/e2e/`。具体分套件运行方式、CI 校验与发版步骤见[测试与发布说明](docs/testing-and-release.md)。

## 许可证

[MIT](LICENSE)。
