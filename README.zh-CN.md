# PVsyst CLI MCP

[English](README.md) | **简体中文**

围绕**已安装 PVsyst 软件**的本地 MCP 服务与 Python 适配器：审查工程输入、准备或修订受限原生项目、调用原生仿真、比较关联方案，并交付原工程、结果与可读交接说明。

物理计算由 PVsyst 执行。这是非官方项目，不附带 PVsyst、许可证、商业模型、气象资产，也不实现替代 PV／储能／阴影仿真器。

## 从任务开始

| 任务 | 入口 | 详细说明 |
|---|---|---|
| 审查有引用的可研数量、冲突和缺项 | `pvsyst_review_engineering_brief` | [工程简报预审](docs/engineering-brief.md) |
| 查看已有工程，规划隔离修订 | `pvsyst_project_handoff`、`pvsyst_run_engineering_task` | [工程修订与交付](docs/engineering-delivery.md) |
| 用完整固定分块准备新工程 | `pvsyst_prepare_block_project` | [分块工程准备](docs/block-project-preparation.md) |
| 生成并由原生软件消费一栋受限双屋面房屋 | `pvsyst_run_house_task` | [住宅任务](docs/native-house-task.md) |
| 结合逐时负荷评估已有原生 AC 光储 | `pvsyst_run_engineering_task` | [光储工程](docs/engineering-storage.md) |
| 比较已绑定原生方案并打包交付 | `pvsyst_compare_engineering_scenarios`、`pvsyst_export_engineering_delivery` | [方案比较](docs/engineering-scenarios.md) |

默认提供 **8 组、82 个工具**。组件、工程编辑、气象、负荷、控制、仿真与结果等低层 API 见[完整工具参考](docs/tool-reference.md)。本次包结构整理保持既有 MCP 工具名和请求 Schema。

## 安装

使用 Windows、Python 3.10+，并自行安装 PVsystCLI。原生 CLI 行为有 8.0.6／8.1.6 的对应证据；受限中文 GUI 路径另有 8.1.6 rev49278／DPI96 要求。请按具体功能证据判断，不按版本号推断全部兼容。

在本仓库目录执行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE = 'C:\path\to\PVsyst8.1_Data'
.\.venv\Scripts\python.exe -m pvsyst_auto
```

包元数据与依赖统一由 `pyproject.toml` 管理，包括 MCP SDK 2.x 和 Windows 所需的 `tzdata`。原 `pip install -r requirements.txt` 方式现在安装同一个可编辑包；正式非可编辑安装使用 `pip install .`。

## 连接 MCP 客户端

配置已安装本包的 Python 解释器。模块启动方式不依赖客户端当前工作目录：

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

安装后也可用 `pvsyst-mcp` 命令启动同一 stdio 服务。已有客户端若指向仓库中的 `pvsyst_mcp_server.py`，仍由一个薄兼容入口启动；旧根目录实现模块不逐个保留转发壳。更新包或环境配置后，重启 MCP 客户端。

连接后先用 `pvsyst_capabilities` 和 `pvsyst_license_info` 查看 **CLI 自身**支持的参数与许可／配额，再安排原生运行。这些查询不执行仿真；GUI 与 CLI 的许可状态可能不同。凭据和许可资料不进入版本库。

### 按任务精简工具发现

默认仍提供全部 82 个工具。若某个客户端只做特定任务，可在其现有 `env` 中增加 `PVSYST_TOOL_GROUPS`，例如 `"PVSYST_TOOL_GROUPS": "setup,engineering,results"`；当前只公开对应的 15 个设置／工程／结果工具，其余组不再出现在目录中。省略该变量保持完整工具集。

组键区分大小写：`setup`、`projects`、`components`、`weather`、`storage`、`simulation`、`results`、`engineering`。空项、重复项或未知项会在启动时明确报错，不静默选择其它集合。选择仅作用于当前进程，修改后重启；它调整工具发现与调用可用性，不改变原生执行、工作区或工程要求。工具内容体积、CSV 读取收益及仍未得出稳定结论的冷启动数据，见[性能实测与边界](docs/performance.md)。

## 正确理解状态

| 状态 | 含义 |
|---|---|
| `ready` | 预检／预览通过，不等于已运行原生仿真 |
| `prepared` | 请求的文件已创建，不代表 PVsyst 已消费 |
| `stage=completed` 且 `native_consumption_verified=true` | 该任务记录的原生执行及输入／输出检查通过 |
| `needs_review` | 保留来源、模型、工程依据或原生告警的待审事项，即使执行已经完成 |
| `blocked`／`failed` | 查看问题与保留产物，不暗示自动覆盖或重试 |

导出或数值比较不会额外运行原生仿真。仅凭历史 CSV 文件名也不足以确定历史输入。模型、天气、硬件拓扑和工程声明仍是明确依赖；运行成功不等于设备或现场设计认证。

## 能力边界

固定／跟踪工程修订、AC 光储评估、双屋面住宅和固定分块准备分别保留自己的文档边界。anchor0 双屋面案例不扩展为任意几何或非零锚定；固定分块不扩展为地形、共享变压器或任意拓扑；AC 自用证据也不扩展为所有混合 DC 端口或储能策略。

物理行为始终使用 PVsyst 原有入口。工作区路径检查、生产冲突保护、进程内锁、原始输入保护和失败留存继续保持；进程内锁不等于跨进程桌面锁。坐标、版本、语言及短时／全年证据在[原生入口地图](docs/native-entry-map.md)中分别标注。

## 代码结构

```text
src/pvsyst_auto/
├── api/          # MCP 上下文、严格请求模型与八组接口
├── workflows/    # 准备、修订、全年执行与交付任务
├── engineering/  # 简报、输入交接、审查与方案比较
├── project/      # PRJ/VC、电路、模板及朝向修改
├── components/   # PAN/OND/BTR、对象编辑和引用来源建模
├── scene/        # 房屋/PV 作者、读取与空间核对
├── energy/       # 负荷、储能、损失及电气控制
├── weather/      # 采集、时基、准备与审查
├── native/       # 既有 CLI 与受限 Windows GUI 入口
└── results.py    # 共享原生结果检查与汇总
```

完整业务从 workflow 进入，领域修改找对应模块，软件执行查看 `native`；API 层调用这些已有实现。本次没有添加插件／依赖注入框架、调度仿真器或通用桌面引擎。见[结构与导入迁移说明](docs/architecture.md)。

公共 Python 适配器入口为：

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

## 开发与证据

直接使用 Python 模块前先安装可编辑包。验证聚焦 `tests/contracts` 中明确指定的公共契约；真实输入契约可能要求显式本机夹具，skip 不计作原生验收。当前仓库工作约定不在活动批次执行全量发现、旧 smoke 或哈希测试，既有生产一致性检查保持，见 [AGENTS.md](AGENTS.md)。

本次结构整理不扩展原生支持范围，也不增加年度运行。详细历史验收、当前功能边界与失败恢复记录仍从[工具参考](docs/tool-reference.md)和[工程交付](docs/engineering-delivery.md)进入。

## 许可

[MIT](LICENSE)。PVsyst 与第三方输入保留各自的所有权及许可条件。
