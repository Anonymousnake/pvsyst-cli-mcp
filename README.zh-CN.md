# PVsyst CLI MCP

[English](README.md) | **简体中文**

Windows 下配合本地 PVsyst 8 使用的 MCP 服务端与 Python 封装库。它一方面封装了官方 `PVsystCLI.exe` 的仿真、气象转换和许可查询命令；另一方面直接解析和修改 PVsyst 的明文工程与组件文件（`.PRJ`、`.VCx`、`.PAN`、`.OND`、`.BTR`、`.GEN`、`.SIT`），并对少数 CLI 没开放的功能（如 3D 住宅场景导入、DC 线缆阻值重算）提供受控的 Win32 GUI 自动化桥接。

运行本项目需要本机已安装 PVsyst 并配置好有效许可证。

## 常用任务入口

| 要做的事 | 入口工具 | 详细文档 |
|---|---|---|
| 核对可研简报里的设备数量、串并联关系与缺项 | `pvsyst_review_engineering_brief` | [工程简报预审](docs/engineering-brief.md) |
| 读取现有工程参数，隔离改参并跑全年仿真 | `pvsyst_project_handoff`、`pvsyst_run_engineering_task` | [工程修订与交付](docs/engineering-delivery.md) |
| 按固定子阵分块模板批量拼装新工程 | `pvsyst_prepare_block_project` | [分块工程准备](docs/block-project-preparation.md) |
| 生成双坡屋顶住宅 3D 场景、导入绑定并跑全年阴影仿真 | `pvsyst_run_house_task` | [住宅建模任务](docs/native-house-task.md) |
| 绑定全年逐时负荷，评估集中式 AC 耦合光储方案 | `pvsyst_run_engineering_task` | [AC 光储评估](docs/engineering-storage.md) |
| 对比两到三个方案的仿真结果并打包交付 ZIP | `pvsyst_compare_engineering_scenarios`、`pvsyst_export_engineering_delivery` | [多方案对比](docs/engineering-scenarios.md) |

服务默认注册 **8 组共 82 个工具**。如果需要直接调底层的组件增删改、变体参数编辑、拓扑调整、天气下载转换或 CSV 结果分页读取，见[完整工具参考](docs/tool-reference.md)。

## 安装与环境准备

环境要求：
- Windows 系统，Python 3.10+
- 已安装 `PVsystCLI.exe`（已在 8.0.6 与 8.1.6 验证）
- 涉及桌面窗口自动化的功能（3D 住宅场景导入、DC 线缆设置）要求中文界面 PVsyst 8.1.6 rev49278，显示器缩放 96 DPI（100%）

在仓库根目录执行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
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
        "PVSYST_CLI": "C:/Program Files/PVsyst8.1.6/PVsystCLI.exe",
        "PVSYST_WORKSPACE": "C:/path/to/PVsyst8.1_Data"
      }
    }
  }
}
```

仓库根目录保留了 `pvsyst_mcp_server.py` 兼容壳，老配置直接指向该脚本也能正常启动。改了环境变量或更新代码后记得重启 MCP 客户端。

连上之后建议先调一次 `pvsyst_capabilities` 和 `pvsyst_license_info`，确认本机 CLI 版本支持的命令行选项以及 CLI 许可剩余配额（PVsyst 的 GUI 许可和 CLI 许可是分开计数的）。

### 按需裁剪暴露的工具组

一次挂满 82 个工具比较占上下文。如果某个客户端只需要其中几类能力，可以在 `env` 里加上 `PVSYST_TOOL_GROUPS`，比如 `"PVSYST_TOOL_GROUPS": "setup,engineering,results"`，这样就只注册这三组的 15 个工具。不配该变量则默认暴露全量工具。

合法的组名（区分大小写）：`setup`、`projects`、`components`、`weather`、`storage`、`simulation`、`results`、`engineering`。传了空项、重复项或拼错的组名会在启动时直接报错退出，免得静默漏挂工具。

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
└── results.py    # 仿真结果 CSV/PDF 解析与年度指标汇总
```

各子模块的详细职责与旧版扁平模块导入路径对照见[软件包结构](docs/architecture.md)，PVsyst 各功能点的支持情况见[功能入口地图](docs/native-entry-map.md)。

如果在 Python 脚本里直接调用底层 CLI 封装：

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

## 开发与测试

本地开发先执行 `pip install -e .`。公开接口契约测试放在 `tests/contracts/` 下；涉及真实 PVsyst 样本或调用 `PVsystCLI.exe` 的测试需要通过环境变量指定本机工作区和程序路径。

## 许可证

[MIT](LICENSE)。
