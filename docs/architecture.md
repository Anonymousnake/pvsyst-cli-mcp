# 软件包结构

英文首页为[README](../README.md)，中文首页为[README.zh-CN](../README.zh-CN.md)，接口说明见[工具参考](tool-reference.md)。

## 按职责阅读

| 目录 | 职责 | 代表模块 |
|---|---|---|
| `api` | MCP协议、严格请求模型、工具注册和共享上下文 | `app`、`context`、`schemas`及八个工具组 |
| `workflows` | 组合原生与领域能力完成业务任务 | `revision`、`house`、`simulation`、`blocks`、`delivery` |
| `engineering` | 输入、引用、缺项、工程审查及原生结果比较 | `brief`、`handoff`、`review`、`comparison` |
| `project` | PRJ/VC、拓扑、模板和朝向控制 | `variants`、`circuit`、`tracking`、`paths` |
| `components` | 原生组件对象、曲线、商业字段和来源声明 | `store`、`editor`、`pan`、`battery`、`identity` |
| `scene` | 房屋/PV文件、读取和原生空间核对 | `author`、`reader`、`mapping`、`state` |
| `energy` | 原生负荷、储能、电气与损失配置 | `profiles`、`storage`、`system`、`power`、`ac` |
| `weather` | 气象源、时基、准备和审查 | `acquisition`、`preparation`、`review`、`csv` |
| `native` | CLI和受限Windows原生入口 | `cli`、`project`、`scene`、`horizon`、`dc` |
| `results.py` | 共用的严格原生结果读取、汇总和比较 | 年度完整性与结果一致性检查 |

API层组织请求与协议错误，完整任务位于workflows，领域模块负责各自的输入与读写，物理计算和仿真由原生PVsyst执行。

## MCP上下文

工具按`setup/projects/components/weather/storage/simulation/results/engineering`组织。`api/context.py`持有注册器、未知参数中间件、受控路径助手、进程内唯一CLI及工作区锁。严格请求模型集中在`api/schemas.py`。

`api/app.py`导入所选固定工具组、检查活动严格入口的注册完整性，并启动stdio服务。省略`PVSYST_TOOL_GROUPS`时公开全部工具；设置后只公开指定组。各组共用同一上下文，不分别创建CLI或工作区锁。进程内锁不提供跨进程桌面协调。

## 安装与启动

`pyproject.toml`是依赖和包元数据来源，使用标准src布局，只打包`pvsyst_auto`。`requirements.txt`提供可编辑安装入口。

```powershell
py -m pip install -e .
py -m pvsyst_auto
```

也可使用安装后的`pvsyst-mcp`命令。原生软件和输入资料由调用者配置；模块及console启动不依赖当前工作目录。

根目录`pvsyst_mcp_server.py`是旧客户端的兼容启动入口，进入同一个app，不转发旧内部模块API。

## Python导入

公共适配器入口：

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

旧脚本若直接导入扁平实现模块，需改用职责对应的包路径：

| 原导入 | 包路径 |
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
