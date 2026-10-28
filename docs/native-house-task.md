# 住宅 3D 场景端到端任务（`pvsyst_run_house_task`）

`pvsyst_run_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False)` 把住宅光伏建模的各个环节——克隆工作工程、生成 `.pvc2` 三维场景、冷启动 PVsyst 8.1.6 GUI 导入场景并绑定朝向、校验阴影表并另存新变体、正常关闭 GUI、再启动 `PVsystCLI.exe` 重新加载该变体跑完全年逐时仿真——整合成一条带断点续跑检查点（Checkpoint）的完整流水线。

- 如果还没准备源工程，可以先调 [`pvsyst_prepare_design_project`](design-project-preparation.md) 生成源工程，或者直接用把规格书绑定也包进去的 [`pvsyst_run_sourced_house_task`](sourced-house-task.md)。
- 如果不需要 3D 房屋阴影、只跑常规无阴影固定平面，直接用 [`pvsyst_simulate_design`](single-design-inputs.md) 即可，无需启动 GUI。

## 环境前提与输入参数

运行环境要求：
- Windows 系统，中文版 PVsyst 8.1.6 rev49278，显示器缩放 96 DPI（100%）。
- `PVsyst.ini` 中的 `DataPath` 已指向当前 MCP 工作区，且执行前桌面上**没有已打开的 PVsyst GUI 进程**（为防止冲掉用户未保存的工作，只要检测到已有 PVsyst 窗口，工具就会拒绝接管并停止）。
- 源工程支持北半球单子阵列/单固定朝向的纯并网工程，以及符合 `anchor0` 约束的单栋双屋面工程（双屋面条件见[工程交付双屋面章节](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。源 `.PRJ` 与 `.VCx` 必须引用同一对工作区 `.SIT`/`.MET`。

`spec` 字典字段：

| 字段名 | 说明与要求 |
|---|---|
| `source_project`, `source_variant` | 作为底板的源 `.PRJ` 与 `.VCx`（全程只读） |
| `new_project` | 新生成的住宅工程 `.PRJ` 文件名（工程、检查点、`.SFI`、`.pvc2` 及输出 CSV/PDF 均不可与现有文件重名） |
| `variant_name` | 新变体的显示标题（`1..80` UTF-8 字节单行文本；实际保存的 `.VCx` 编号由 PVsyst `SaveAs` 自动分配） |
| `year` | 仿真年份（整数 `1969..2068`），需与继承的 `.MET` 文件年份一致 |
| `house` | `kind="mesh"` 的完整具名房屋构件三角网格（墙体、屋面、挑檐、门窗、烟囱、门廊等；详见[住宅与光伏建模](house-pv-modeling.md)） |
| `layout` | 单区域排布或 `tables` 中的双区域排布，组件总颗数必须与源变体子阵列严格一致 |
| `module_geometry` | 组件几何尺寸（`model_name`、`nom_power_wp`、`width_m`、`length_m`、`geometry_depth_m`） |
| `report_pages` | 可选的原生 PDF 报告页名称列表；省略时输出默认完整报告 |

## 执行阶段（`phase`）与产物命名

`dry_run=true` 只做内存预检；正式执行需同时传入 `dry_run=false` 和 `confirm=true`。所有生成的文件统一使用 `<new-project-stem>_house` 前缀（包括 `UserData` 下的 `.pvc2`、`Models` 下的 `.SFI`、`Results` 下的 `_task.json` 清单及 `_run01.csv`/`.pdf`），并按以下阶段推进：

| `phase` | 完成的动作 |
|---|---|
| `initialized` | 入参、源工程与环境预检通过，写入初始 `_task.json` 检查点 |
| `project_created` | 克隆出工作工程 `.PRJ` 与 `VC0` |
| `scene_created` | 生成 `UserData/<stem>_house.pvc2` |
| `prepared` | 生成 `.SFI` 导出变量定义文件，准备启动 GUI |
| `native_saved` | 冷启动自有 PVsyst GUI 进程，完成 `.pvc2` 导入、朝向关联、有效性检查、阴影表计算与 `SaveAs` 另存，并回读校验新 `.VCx` |
| `gui_closed` | 向自有 GUI 进程发送正常关闭指令并等待进程退出（等待上限 20 秒，绝不强杀非自有进程） |
| `simulated` | 启动全新的 `PVsystCLI.exe` 进程加载刚保存的 `.VCx`，跑完全年仿真并导出 CSV/PDF |
| `completed` | 核对全年 8760/8784 小时时间轴、数值列、`GlobShd` 阴影列、PDF 与源文件哈希通过，将 `native_consumption_verified` 置为 `true` |

## 断点续跑（`resume=true`）与仿真重试（`retry_simulation=true`）

- **断点续跑**：如果任务在 `native_saved` 或 `gui_closed` 之后中断，使用相同的 `spec` 加上 `resume=true, dry_run=False, confirm=True` 调用，工具会校验检查点清单和已落盘文件，直接从当前阶段往后接续（已经导完保存好 `.VCx` 的不会再重开一次 GUI；已经 `completed` 的只会重验结果文件，不会再跑一次仿真）。
- **未登记变体保护（`native_save_reconciliation`）**：如果检查点还停在 `prepared` 阶段，但恢复时发现工作工程下已经多出了新的 `.VCx` 文件（说明上次 GUI `SaveAs` 刚写完盘還沒来得及更新检查点就断了），工具会返回 `status="needs_attention"`、`stage="native_save_reconciliation"` 并列出 `unrecorded_native_variants` 供人工核对，不会盲目重开 GUI 再导一遍。
- **失败仿真换名重试**：如果 GUI 导入保存都成功了，仅在最后一步 CLI 仿真时失败，传入 `resume=true, retry_simulation=true, dry_run=False, confirm=True` 会自动递增序号（例如生成 `_run02.csv`/`.pdf`）重新调 CLI 跑仿真，同时保留 `_run01` 的失败现场。

## Python 调用示例：回读已有 `.pvc2` 场景并执行住宅任务

```python
import json

async def deliver_scene(session, scene_name, new_project, year, execute=False):
    async def call(name, arguments):
        response = await session.call_tool(name, arguments)
        if response.is_error:
            raise ValueError(response.content)
        return json.loads("\n".join(x.text for x in response.content if hasattr(x, "text")))

    scene = await call("pvsyst_read_house_pv_scene", {"scene_name": scene_name})
    authoring = scene["create_arguments"]
    spec = {
        "source_project": authoring["project"],
        "source_variant": authoring["variant"],
        "new_project": new_project,
        "variant_name": "Residential roof delivery",
        "year": year,
        "house": authoring["house"],
        "layout": authoring["layout"],
        "module_geometry": authoring["module_geometry"],
    }
    preview = await call("pvsyst_run_house_task", {"spec": spec})
    if not execute or preview["status"] != "ready":
        return preview
    return await call("pvsyst_run_house_task", {"spec": spec, "dry_run": False, "confirm": True})
```
