# PVsyst CLI MCP

[English](README.md) | **简体中文**

Windows 下配合本地 PVsyst 8 使用的 MCP 服务端与 Python 封装库。它一方面封装了官方 `PVsystCLI.exe` 的仿真、气象转换和许可查询命令；另一方面直接以无 GUI 的原生文本构建器解析和修改 PVsyst 工程与组件文件（`.PRJ`、`.VCx`、`.PAN`、`.OND`、`.BTR`、`.GEN`、`.SIT`），涵盖 3D 住宅与环境遮挡物（`.pvc2` -> `.VCx`）、`module_layout` 精细接线以及 DC 线缆阻值/损耗计算。

运行本项目需要本机已安装 PVsyst 并配置好有效许可证。

## 常用任务入口

| 要做的事 | 入口工具 | 详细文档 |
|---|---|---|
| 一站式执行固定倾角单方案选型与全年仿真 | `pvsyst_simulate_design` | [单方案设计输入](docs/single-design-inputs.md) |
| 读取现有工程参数，隔离改参并跑全年仿真 | `pvsyst_run_engineering_task` | [工程修订与交付](docs/engineering-delivery.md) |
| 生成屋顶光伏与周边遮挡 3D 场景、原生导入并跑全年阴影仿真 | `pvsyst_run_house_task` | [住宅建模任务](docs/native-house-task.md) |
| 绑定全年逐时负荷，评估集中式 AC 耦合光储方案 | `pvsyst_create_grid_storage_system`、`pvsyst_run_engineering_task` | [AC 光储评估](docs/engineering-storage.md) |
| 对比两到三个方案的仿真结果 | `pvsyst_compare_engineering_scenarios` | [多方案对比](docs/engineering-scenarios.md) |
| 可研简报核对、交接读参、分块拼装与交付打包（需 `expert` 组） | `pvsyst_review_engineering_brief`、`pvsyst_project_handoff`、`pvsyst_prepare_block_project`、`pvsyst_export_engineering_delivery` | [工程简报预审](docs/engineering-brief.md) |

服务默认仅注册 **8 个默认核心组共 34 个高频工具**以降低模型上下文开销；`PVSYST_TOOL_GROUPS` 显式列出的组会替换这 8 个默认组，单独设置 `admin` 或 `expert` 只加载对应组。设置 `PVSYST_TOOL_GROUPS=all` 可加载全部 **66 个工具**；若要保留默认组并追加 `expert`，请完整写成 `setup,projects,components,weather,storage,simulation,results,engineering,expert`。从 `v0.1.0` 升级的完整迁移说明见[工具参考中的升级小节](docs/tool-reference.md#upgrading-from-v010)。

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

包依赖（MCP SDK 2.x 与 Windows 时区库 `tzdata`）统一定义在 `pyproject.toml` 中。执行 `pip install -r requirements.txt` 同样会以可编辑模式安装本包；常规安装用 `pip install .`。

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

连上之后建议先调一次 `pvsyst_capabilities` 确认本机 CLI 版本支持的命令行选项（若需查询或管理 CLI 许可配额，请在 `PVSYST_TOOL_GROUPS` 中开启 `admin` 或 `all` 后调用 `pvsyst_license_info`；注意 PVsyst 的 GUI 许可和 CLI 许可是分开计数的）。

### 按需调整暴露的工具组

未配置 `PVSYST_TOOL_GROUPS` 时，服务默认仅暴露 **34 个高频核心工具**（涵盖 `setup`、`projects`、`components`、`weather`、`storage`、`simulation`、`results`、`engineering` 八个默认组），大幅减少 LLM 上下文占用。可通过 `env` 中的 `PVSYST_TOOL_GROUPS` 精确控制；显式列出的组会替换默认组：

- `"PVSYST_TOOL_GROUPS": "all"`：加载全部 **66 个工具**（包含默认组及 `admin`、`expert`）。
- `"PVSYST_TOOL_GROUPS": "setup,projects,components,weather,storage,simulation,results,engineering,expert"`：保留默认组并追加 `expert`，共 61 个工具。
- `"PVSYST_TOOL_GROUPS": "setup,engineering,results"`：只加载这三组的 6 个工具。

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
├── api/          # MCP 服务注册、严格参数校验中间件、8 个工具组入口
├── workflows/    # 端到端业务流（模板仿真、工程改参、住宅任务、交付打包）
├── engineering/  # 可研简报核对、工程输入交接、多方案结果对比
├── project/      # .PRJ/.VCx 解析、子阵电路树、模板匹配与朝向修改
├── components/   # .PAN/.OND/.BTR/.GEN 读写、白名单字段编辑、Mode 1 曲线表
├── scene/        # PVCollada (.pvc2) 房屋与光伏网格生成、回读与空间坐标核对
├── energy/       # 逐时负荷、储能配置、详细损失标量、功率因数与并网限发
├── weather/      # Open-Meteo ERA5 下载、标准时对齐、.SIT/.MET 转换与核对
├── native/       # PVsystCLI.exe 进程封装与受控 Win32 GUI 自动化入口
└── results.py    # 仿真结果 CSV 解析与逐时指标汇总
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
