# 住宅 3D 场景端到端任务（`pvsyst_run_house_task`）

`pvsyst_run_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False)` 把住宅光伏建模的各个环节——克隆工作工程、生成 `.pvc2` 三维场景（含屋顶与可选周边遮挡物 `obstacles`）、免 GUI 原生写入含 3D 阴影/组件布局的 `.VCx` 变体、再启动 `PVsystCLI.exe` 重新加载该变体跑完全年逐时仿真——整合成一条带断点续跑检查点（Checkpoint）的完整流水线。

- 全流程完全免去桌面 GUI 启动与窗口自动化（`native/scene` GUI 桥接已在 `v0.6.0` 移除）。
- 如果还没准备源工程，可以先调 `pvsyst_prepare_design_project`（[`single-design-inputs.md`](single-design-inputs.md)）生成源工程，或者直接用把规格书绑定也包进去的 `pvsyst_run_sourced_house_task`（正式执行时需在审核完规格书绑定后显式传入 `accept_source_review=true`，见[工具参考](tool-reference.md)）。
- 如果不需要 3D 房屋阴影、只跑常规无阴影固定平面，直接用 [`pvsyst_simulate_design`](single-design-inputs.md) 即可。

## 环境前提与输入参数

运行环境要求：
- Windows 系统 + 已安装的 `PVsystCLI.exe`（8.1.6 rev49278 / 8.1.7 rev49382）；场景导入阶段完全无头，不启动桌面 GUI。
- `PVsyst.ini` 中的 `DataPath` 已指向当前 MCP 工作区。
- 源工程支持北半球单子阵列/单固定朝向的纯并网工程，以及符合 `anchor0` 约束的单栋多朝向工程（双屋面条件见[工程交付双屋面章节](engineering-delivery.md#two-roof-house-workflow)）。源 `.PRJ` 与 `.VCx` 必须引用同一对工作区 `.SIT`/`.MET`。

`spec` 字典字段：

| 字段名 | 说明与要求 |
|---|---|
| `source_project`, `source_variant` | 作为底板的源 `.PRJ` 与 `.VCx`（全程只读） |
| `new_project` | 新生成的住宅工程 `.PRJ` 文件名（工程、检查点、`.SFI`、`.pvc2` 及输出 CSV/PDF 均不可与现有文件重名） |
| `variant_name` | 新变体的显示标题（`1..80` UTF-8 字节单行文本；实际保存的 `.VCx` 编号由 PVsyst `SaveAs` 自动分配） |
| `year` | 仿真年份（整数 `1969..2068`），需与继承的 `.MET` 文件年份一致 |
| `house` | `kind="mesh"` 的完整具名房屋构件三角网格（墙体、屋面、挑檐、门窗、烟囱、门廊等） |
| `obstacles` | 可选；周边环境遮挡物列表（`1..64` 个），支持 `building`、`tree`（原生 `oeTree`）、`wall`、`mesh` 四类构件 |
| `layout` | 单区域排布或 `tables` 中的多区域排布，组件总颗数必须与源变体子阵列严格一致 |
| `string_layout` | 可选；显式 per-module-cell 组串接线声明 `{tables:[{name,subarray_id,cells:[{row,col,global_string}]}]}`，作为 `layout` 同级兄弟块，不替换 `layout`。`row`/`col` 1 基、`global_string>=0`（`0`=未分配 draft）、`subarray_id` 为真实源子阵 id。省略即旧行为：旧 recipe v1/v2 形状与字节不变、不升版本、不造 v3。draft 可保存/回读但原生 import 前须补全；显式接线仅在 `shading_mode="module_layout"` 时被消费，`linear` 会在写入前拒绝而不是悄悄丢弃 |
| `module_geometry` | 组件几何尺寸（`model_name`、`nom_power_wp`、`width_m`、`length_m`、`geometry_depth_m`） |
| `report_pages` | 可选的原生 PDF 报告页名称列表；省略时输出默认完整报告 |
| `report_pdf` | 可选 `StrictBool`，默认 `null`。省略/`None`/`True` 保留原有 CSV+PDF 完整报告；`False` 选择 **CSV-only**：复用原 `out_csv` 单出口（不加 `-rpf:`），只产出逐时 CSV，不列 `pdf` 产物/守卫。阴影重算仍在私有非交互桌面上允许建窗；新任务若显式设置 `allow_window_creation=false`，会在预检报错。`False` 会持久化并经 sourced→子 house 透传；不接受字符串/数字强制转换。省略时请求/检查点旧序列化形状不变（沿用既有 `exclude_none`，不新增 key） |
| `shading_mode` | 可选；省略时保留原有线性阴影流程，`module_layout` 使用原生组件布局与精细电气阴影计算 |

## Module Layout 电气阴影

在 `spec` 中加入 `"shading_mode": "module_layout"` 即可启用精细电气阴影。默认（无 `string_layout`）支持单朝向双表双串、以及 **2 个或更多（3+ 坡屋面）**独立固定朝向子阵列（每阵一条 `1..100` 块组件组串，每阵一张横装 `landscape` 或竖装 `portrait` 组件表，支持正 `row_spacing_m`/`column_spacing_m` 间距与一致的 `PowerSharingInverterId >= 0` 共享逆变器绑定）。多串或多表分摊时提供 `string_layout` 逐 cell 指定全局组串（支持串跨表和一表多串，全局串容量等于所属源子阵的 `NModSerie`），`global_string=0` 的草稿仅用于保存与回读，导入前需补全分配；显式逐格接线仅在 `shading_mode="module_layout"` 下启用。不在 MCP 内计算或近似电气阴影。

已准备好 `.pvc2` 场景文件时，也可直接调用导入工具：

```json
{
  "scene_name": "RoofScene.pvc2",
  "variant_name": "Roof electrical shading",
  "shading_mode": "module_layout",
  "dry_run": true
}
```

确认预览后，同时传入 `dry_run=false` 和 `confirm=true` 执行。导入结果中的 `verification` / `stored_module_layout_verified` 核对已保存模块、原生表格及子阵引用（在随后调 `pvsyst_run_simulation` 执行仿真前，`native_module_layout_verified` 与 `imported` 仍保持 `false`）。配置、几何验证和仿真消费仍是不同阶段。

全年任务会自动导出 `ShdElec` 电气阴影损失。单独运行仿真时，可通过 `pvsyst_build_sfi(variables="shading")` 生成包含 `GlobShd`、`ShdElec` 的导出定义。逐小时输出中的 `ShdElec` 单位为 `kW`，全年求和得到 `kWh`；零损失也是有效结果。线性模式可能不导出该列，比较结果时按实际列名对齐，保留缺列状态，不自动补零。

## CSV-only 输出（`report_pdf=False`）

在 `spec` 中加 `"report_pdf": false` 可让最终 CLI 仿真只产出逐时 CSV、不产出 PDF：

- 复用现有纯 CSV 仿真路径（仅 `-ocf:` 指定输出 CSV，不传 `-rpf:`），阴影重算仍允许在私有非交互桌面建窗；原生导入不启动 `PVsyst.exe`。
- 结果 JSON 的产物列表、校验逻辑与步骤状态均与 CSV-only 模式保持一致：只记录 `csv` 而不会将缺失的 PDF 记为成功；`resume=true` 续跑时在验证 CSV 覆盖完整年份与逐时数据无误后直接视为完成，CSV 损坏则会中断并报错。
- 该开关**只控制是否输出 PDF**：`report_pdf` 默认为 `True`；全流程仅在纯 CLI 阶段创建导出进程，无需 PVsyst 桌面 GUI，但不代表 CLI 内部完全不涉及任何系统窗口句柄（丢失报告页 PDF 在无窗口创建权限时仍受限于 CLI 自身渲染器）。
- 参数行为：`report_pdf` 必须为严格布尔类型（可选）；当计算结果附带告警（例如未静态校验的内置模型或 CLI 告警）时，任务状态保持 `needs_review` 以供复核。

## 无 GUI 原生场景写入边界

免去 GUI 操作，根据声明的房屋网格、`obstacles` 与 PV 区域在内存中直接构建并校验场景记录，写出新变体文件；**导入阶段**不启动 `PVsyst.exe`，不改写 `INI` 配置、剪贴板或许可文件，也不执行仿真。当 `pvsyst_run_house_task(dry_run=false, confirm=true)` 时会在同一流程内自动继续调用 CLI 跑完全年仿真（见下）。

支持范围：高层 `pvsyst_run_house_task` 与底层导入共用同一套原生构建校验，支持横装 `landscape` 与竖装 `portrait`（`ModOrient=moPortrait`）、零或正的模块间距（`row_spacing_m` / `column_spacing_m >= 0`，写入 `ModSpacingX`/`ModSpacingY`）、任意多个（2 个、3 个及以上）固定朝向屋面与 `PowerSharingInverterId >= 0` 共享多 MPPT 逆变器。未提供 `string_layout` 时：`linear` 支持分组多朝向 `1..64` 张表及单朝向 `1..3` 张表；`module_layout` 支持单朝向双表双串或分组多朝向每子阵 1 串 1 表。提供 `string_layout` 时可对 `1..64` 张表跨表/单表多串逐 cell 绑定任意固定平面与共享逆变器组合。不满足拓扑/总数/平面约束时直接报错中止而不做隐式修改。更多功能支持矩阵见[功能入口地图](native-entry-map.md)。

状态说明：高层 `pvsyst_run_house_task` 的 **native 导入阶段**只写入场景记录文件（`stored_geometry_verified` 和 `stored_module_layout_verified` 仅反映文件已正确保存），此时 `imported`、`native_geometry_verified` 和 `native_consumption_verified` 仍为 `false`——表示 PVsyst 尚未实际加载并计算该变体。只有当调用 `dry_run=false, confirm=true` 继续跑完 CLI 全年仿真且结果上下文校验通过后，`native_consumption_verified` 才会置为 `true`。如果直接调用底层 [`pvsyst_import_house_pv_scene`](tool-reference.md) 单独生成变体，随后需再调用 [`pvsyst_run_simulation`](tool-reference.md) 运行仿真。

窗口依赖说明：导入文件本身不启动 GUI 或 CLI；但在后续调用 `PVsystCLI.exe` 运行仿真时，CLI 内部仍可能用到隐藏窗体或消息窗体。特别是损失图 PDF 的生成需要创建窗口权限：在禁用窗口创建的桌面环境中，CLI 渲染损失图页面会失败（不输出损失图 PDF），仅在允许创建窗口的环境中才能正常生成损失图页面。`export-logs` 等日志导出命令的可用性不影响渲染器对窗口权限的这一依赖。

## 执行阶段（`phase`）与产物命名

`dry_run=true` 只做内存预检；正式执行需同时传入 `dry_run=false` 和 `confirm=true`。所有生成的文件统一使用 `<new-project-stem>_house` 前缀（包括 `UserData` 下的 `.pvc2`、`Models` 下的 `.SFI`、`Results` 下的 `_task.json` 清单及 `_run01.csv`/`.pdf`），并按以下阶段推进：

| `phase` | 完成的动作 |
|---|---|
| `initialized` | 入参、源工程与环境预检通过，写入初始 `_task.json` 检查点 |
| `project_created` | 克隆出工作工程 `.PRJ` 与 `VC0` |
| `scene_created` | 生成 `UserData/<stem>_house.pvc2` |
| `prepared` | 生成 `.SFI` 导出变量定义文件，准备原生场景写入 |
| `native_saved` | 原生无头写入新 `.VCx`（3D 阴影/组件布局）并校验已存储的几何记录 |
| `simulated` | 启动全新的 `PVsystCLI.exe` 进程加载刚保存的 `.VCx`，跑完全年仿真并导出 CSV/PDF |
| `completed` | 核对全年 8760/8784 小时时间轴、数值列、`GlobShd` 阴影列、PDF（`report_pdf=False` 的 CSV-only 模式跳过 PDF、只核对 CSV 与源文件字节）与源文件指纹通过，将 `native_consumption_verified` 置为 `true` |

## 断点续跑（`resume=true`）与仿真重试（`retry_simulation=true`）

- **断点续跑**：如果任务在 `native_saved` 或 `simulated` 之后中断，使用相同的 `spec` 加上 `resume=true, dry_run=False, confirm=True` 调用，工具会校验检查点清单和已落盘文件，直接从当前阶段往后接续（已经写入好 `.VCx` 的不会重复写入；已经 `completed` 的只会重验结果文件，不会再跑一次仿真）。
- **未登记变体保护（`native_save_reconciliation`）**：如果检查点还停在 `prepared` 阶段，但恢复时发现工作工程下已经多出了新的 `.VCx` 文件，工具会返回 `status="needs_attention"`、`stage="native_save_reconciliation"` 并列出 `unrecorded_native_variants` 供人工核对，不会盲目覆盖。
- **失败仿真换名重试**：如果原生变体保存成功，仅在最后一步 CLI 仿真时失败，传入 `resume=true, retry_simulation=true, dry_run=False, confirm=True` 会自动递增序号（例如生成 `_run02.csv`/`.pdf`）重新调 CLI 跑仿真，同时保留 `_run01` 的失败现场。

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
