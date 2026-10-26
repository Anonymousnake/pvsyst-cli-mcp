# 软件包结构与迁移

本次只整理代码、启动和文档，不扩展原生支持形状，不调整仿真、模型、序列化或生产冲突保护。英文首页为[README](../README.md)，中文首页为[README.zh-CN](../README.zh-CN.md)。详细工具说明从原首页保留到[工具参考](tool-reference.md)。

## 从业务入口阅读

| 目录 | 职责 | 代表模块 |
|---|---|---|
| `api` | MCP协议、严格模型、注册与共享工作区上下文 | `app`、`context`、`schemas`及八个工具组 |
| `workflows` | 组合既有原生与领域能力完成业务任务 | `revision`、`house`、`simulation`、`blocks`、`delivery` |
| `engineering` | 输入、引用、缺项、工程审查及原生结果比较 | `brief`、`handoff`、`review`、`comparison` |
| `project` | PRJ/VC、拓扑、模板和朝向控制 | `variants`、`circuit`、`tracking`、`paths` |
| `components` | 原生组件对象、曲线、商业字段和来源声明 | `store`、`editor`、`pan`、`battery`、`identity` |
| `scene` | 候选房屋/PV文件、严格读取、原生空间核对 | `author`、`reader`、`mapping`、`state` |
| `energy` | 原生负荷／储能／电气与损失配置 | `profiles`、`storage`、`system`、`power`、`ac` |
| `weather` | 气象源、时基、准备和审查 | `acquisition`、`preparation`、`review`、`csv` |
| `native` | 已有CLI和受限Windows原生入口 | `cli`、`project`、`scene`、`horizon`、`dc` |
| `results.py` | 多流程共用的严格原生结果读取和汇总 | 原有年度检查、比较及一致性保护 |

API只组织请求、调用与协议错误；完整任务位于workflows，领域读写保留既有职责，实际原生执行进入native。这是对现有模块的命名与归属整理，不宣称把整个历史依赖图改造成严格无环分层，也没有引入容器、插件、第二套任务框架或新物理模型。

## MCP入口拆分

原约2900行服务文件按已有八组拆为`setup/projects/components/weather/storage/simulation/results/engineering`。共享注册、未知参数中间件、进程内唯一CLI及锁、受控路径助手集中在`api/context.py`；严格请求模型和判别联合集中在`api/schemas.py`；`api/app.py`只导入各组、检查严格入口注册完整性并启动stdio。原来源组件身份读取助手独立归入`components/identity.py`。

82个工具名、描述、输入/输出Schema以及已记录的参数错误响应在临时公开协议比对中保持一致。按组组织代码不赋予工具新的原生能力。模型、字段名、默认值、原生版本/形状门禁和恢复语义原样保留。原有跨模块状态由一个context持有，不为每个工具组新建CLI或工作区锁。

## 安装与三个启动方式

`pyproject.toml`是唯一依赖与包元数据来源，标准src发现只包含`pvsyst_auto`；项目／模型／天气／测试／实验资产不作为包内容。`requirements.txt`保留为安装可编辑包的简短入口。

```powershell
py -m pip install -e .
py -m pvsyst_auto
pvsyst-mcp
```

后两行是两种等价启动选择，不是同时启动两个服务。正式分发可用`pip install .`或构建wheel；所有原生软件与输入仍由调用者单独配置。模块和console从任意工作目录启动，使用安装包而不是依赖根目录的sys.path。

根目录只保留一个受控Python文件`pvsyst_mcp_server.py`，用于已有客户端的绝对路径启动配置；它把本checkout的src加入导入路径并进入同一个app。它不是旧服务器实现，也不逐个转发旧模块API。已存在的未跟踪用户文件不参与迁移。

## Python导入调整

公开适配器入口：

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

直接使用实现模块的脚本需按职责修改导入，不保留67个顶层兼容模块：

| 原导入 | 现在的模块 |
|---|---|
| `pvsyst_cli` | `pvsyst_auto.native.cli`，常用类也从包顶层导出 |
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

已跟踪历史测试仅机械调整导入、限定名和Mock目标位置，不调整断言、预期值或业务行为；既有stdio公共契约继续用旧启动文件，无需跟随实现迁移修改。内部历史测试／旧smoke不因这次导入修订而计作已执行或已验收。历史研究文档中的当时文件名、命令、PID及实验路径按证据保留，不反写成当时已使用新包。

## 验证边界

本批永久非哈希白名单为`test_package_entrypoint_boundary.py`、`test_engineering_brief_boundary.py`、`test_block_project_boundary.py`、`test_two_roof_delivery_boundary.py`、`test_engineering_storage_boundary.py`。新增契约仅保护公开启动行为：checkout模块、旧文件启动、真实wheel安装后的模块和console，要求相同工具协议、只读简报及严格未知参数错误，使用非可执行CLI夹具。安装项缺显式环境时skip，不当作安装成功。

其余契约复用真实已有分块源、双屋面及AC储能完成清单，在临时副本验证公开编辑／比较／交付；不再启动GUI、修改INI或消耗年度运行。编译、迁移前后完整公开协议比对及隔离wheel安装另计。无全量发现、旧smoke、哈希或no-hash探针；wheel构建器的普通产物摘要不是哈希测试，生产一致性保护继续运行。

临时迁移及协议基线在本机`pvsyst-package-study/round1`保留源副本、模块映射和验证记录，完成后清理自有一次性辅助与隔离安装环境。此目录不是运行时依赖，也不上传商业或实验资产。2026-10-26实际验收：五个白名单文件共6项通过、0跳过，耗时62.744秒（不是性能基准）；真实wheel构建与隔离安装成功，源码/旧入口/安装后module及console公开协议一致；88个包模块导入成功，当前四份导航文档无失效文件链接；66个迁移业务模块的AST除导入路径外无变化。GUI、INI、CLI原生调用及年度运行均为0。宿主原先缺setuptools/wheel，通过pip隔离构建取得构建依赖，未向宿主安装额外工具；未执行全量发现或历史测试。外部脚本若仍直接导入旧扁平内部模块，须按上表调整；这项Python路径迁移不由旧MCP启动兼容层隐藏。
