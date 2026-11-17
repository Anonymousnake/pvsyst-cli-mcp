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
| `string_layout` | 可选；显式 per-module-cell 组串接线声明 `{tables:[{name,subarray_id,cells:[{row,col,global_string}]}]}`，作为 `layout` 同级兄弟块，不替换 `layout`。`row`/`col` 1 基、`global_string>=0`（`0`=未分配 draft）、`subarray_id` 为真实源子阵 id。省略即旧行为：旧 recipe v1/v2 形状与字节不变、不升版本、不造 v3。draft 可保存/回读但原生 import 前须补全；显式接线仅在 `scene_import_method="native"` 且 `shading_mode="module_layout"` 时被消费，`gui`/`linear` 会在写入前拒绝而不是悄悄丢弃 |
| `module_geometry` | 组件几何尺寸（`model_name`、`nom_power_wp`、`width_m`、`length_m`、`geometry_depth_m`） |
| `report_pages` | 可选的原生 PDF 报告页名称列表；省略时输出默认完整报告 |
| `report_pdf` | 可选 `StrictBool`，默认 `null`。省略/`None`/`True` 保留原有 CSV+PDF 完整报告（**原 PDF 默认仍为 `True`，不改**）；`False` 选择 **CSV-only**：复用原 `out_csv` 单出口（不加 `-rpf:`），落入 `_is_plain_csv_run` 拒窗路径，只产出逐时 CSV、不生成 PDF 文件、结果里不列 `pdf` 产物/守卫。`False` 会持久化并经 sourced→子 house 透传；不接受字符串/数字强制转换。省略时请求/检查点旧序列化形状不变（沿用既有 `exclude_none`，不新增 key）。该开关只选输出模式，**不改变默认 GUI 导入路线**，也不代表「完全无 GUI 软件完成」 |
| `shading_mode` | 可选；省略时保留原有线性阴影流程，`module_layout` 使用原生组件布局与精细电气阴影计算 |
| `scene_import_method` | 可选；省略或 `"gui"` 保持现有 GUI 导入（原行为与检查点形状不变），`"native"` 改用免 GUI 的原生导入阶段（仅限下文支持边界）。native 的**导入阶段**只写 STORED 变体；本工具 `pvsyst_run_house_task` 在 `dry_run=false` 且 `confirm=true` 时，会沿同一流程自动继续全年 CLI 仿真，成功后上下文匹配新变体才把 `native_consumption_verified` 置 true。仅当单独调用底层 [`pvsyst_import_house_pv_scene`](tool-reference.md)`(method="native")` 时，才需对返回变体另调 `pvsyst_run_simulation` |

## Module Layout 电气阴影

对已满足双屋面条件的输入，在 `spec` 中加入 `"shading_mode": "module_layout"`。默认双朝向模板走原厂自动分配（原生水平、左下起、连续填充）；单朝向显式接线则由 `string_layout` 提供并交给免 GUI 的原生 emitter。不在 MCP 内计算或近似电气阴影。

默认（无 `string_layout`）入口限定两个固定朝向、子阵列 `1`/`2` 各一横装零间距矩形表、每阵一条 `1..100` 块组件独立组串/一个逆变器/一个 MPPT（源数据含多串时需显式传入 `string_layout`）。使用 `string_layout` 时可逐 cell 指定全局组串（支持串跨表和一表多串，全局串容量等于所属源子阵的 `NModSerie`），`global_string=0` 的草稿仅用于保存与回读，导入前需补全分配；当前显式逐格接线仅在 `scene_import_method="native"` + `shading_mode="module_layout"` 下启用。

已准备好 `.pvc2` 且对应工程在 GUI 中打开时，也可直接调用导入工具：

```json
{
  "scene_name": "RoofScene.pvc2",
  "variant_name": "Roof electrical shading",
  "shading_mode": "module_layout",
  "dry_run": true
}
```

确认预览后，同时传入 `dry_run=false` 和 `confirm=true` 执行。导入结果中的 `module_layout` 核对已保存模块、原生表格及子阵引用，`native_shading_mode_reload_verified` 表示保存后真实切离重载的 GUI 仍选中精细模式。配置、几何验证和仿真消费仍是不同阶段。

全年任务会自动导出 `ShdElec` 电气阴影损失。单独运行仿真时，可通过 `pvsyst_build_sfi(variables="shading")` 生成包含 `GlobShd`、`ShdElec` 的导出定义。逐小时输出中的 `ShdElec` 单位为 `kW`，全年求和得到 `kWh`；零损失也是有效结果。线性模式可能不导出该列，比较结果时按实际列名对齐，保留缺列状态，不自动补零。

## CSV-only 输出（`report_pdf=False`）

在 `spec` 中加 `"report_pdf": false` 可让最终 CLI 仿真只产出逐时 CSV、不产出 PDF：

- 复用现有纯 CSV 仿真路径（仅 `-ocf:` 指定输出 CSV，不传 `-rpf:`），走 Windows 拒建窗的隔离桌面分支，不启动 `PVsyst.exe`。
- 结果 JSON 的产物列表、校验逻辑与步骤状态均与 CSV-only 模式保持一致：只记录 `csv` 而不会将缺失的 PDF 记为成功；`resume=true` 续跑时在验证 CSV 覆盖完整年份与逐时数据无误后直接视为完成，CSV 损坏则会中断并报错。
- 该开关**只控制是否输出 PDF**：默认场景导入仍为 GUI（`scene_import_method` 省略时取 `gui`），`report_pdf` 默认为 `True`；`report_pdf=False` 也可与 `scene_import_method="native"` 组合使用，需注意两者都不代表整个流程在无任何窗口创建依赖的环境下运行。
- 参数行为：`report_pdf` 必须为严格布尔类型（可选）；当计算结果附带告警（例如未静态校验的内置模型或 CLI 告警）时，任务状态保持 `needs_review` 以供复核。

## 场景导入方式（`scene_import_method`）

在 `spec` 中加入 `"scene_import_method"` 选择场景导入路线：

- 省略或 `"gui"`：冷启动独立的 PVsyst GUI 进程完成导入、朝向关联、有效性检查与另存，入参、检查点与阶段结构均保持原样。
- `"native"`：免去 GUI 操作，根据声明的房屋网格与 PV 区域在内存中直接构建并校验场景记录，写出新变体文件；**导入阶段**不启动 `PVsyst.exe`，不改写 `INI` 配置、剪贴板或许可文件，也不执行仿真。当 `pvsyst_run_house_task(dry_run=false, confirm=true)` 时会在同一流程内自动继续调用 CLI 跑完全年仿真（见下）。

`"native"` 的支持范围：高层 `pvsyst_run_house_task` 与底层导入共用同一套原生构建校验。未提供 `string_layout` 时支持已验证的窄模板：双朝向两张横装零间距矩形表（源子阵 `1`/`2` 各**恰一条**独立组串、一个逆变器、一个 MPPT；每子阵多串时须改用 `string_layout`），或单朝向单子阵 `1..3` 张横装零间距矩形表（线性）／两表两串模式（精细）。提供 `string_layout` 时改走显式逐格接线校验：一个或两个源子阵（每子阵对应一个固定平面）逐 cell 指定全局组串编号，每串容量等于所属子阵 `NModSerie`（支持组串跨表或单表多串，全局编号区间按源子阵顺序排列）；单朝向场景建模支持 `1..64` 张表，双朝向结构上为每朝向一表（共 2 表）。编号 `0` 表示未分配草稿，仅允许保存和回读，导入前须全部填写完整，超出容量、重复占格、未知编号或子阵不匹配均在写入前拦截报错（注：场景导出能力上限为 64 张表，实际仿真请依模板和 PVsyst 版本核对）。所有模板都要求同一 `PAN`/`OND` 组件且总块数与源变体严格相等；自动排布、共享/异构逆变器、竖装（portrait）与非零间距等场景暂不支持，不满足条件时直接报错中止而不做隐式修改。更多细节见[住宅与光伏建模](house-pv-modeling.md)。

状态说明：高层 `pvsyst_run_house_task` 的 **native 导入阶段**只写入场景记录文件（`stored_geometry_verified` 和 `stored_module_layout_verified` 仅反映文件已正确保存），此时 `imported`、`native_geometry_verified` 和 `native_consumption_verified` 仍为 `false`——表示 PVsyst 尚未实际加载并计算该变体。只有当调用 `dry_run=false, confirm=true` 继续跑完 CLI 全年仿真且结果上下文校验通过后，`native_consumption_verified` 才会置为 `true`。如果直接调用底层 [`pvsyst_import_house_pv_scene`](tool-reference.md)`(method="native")` 单独生成变体，随后需再调用 [`pvsyst_run_simulation`](tool-reference.md) 运行仿真。

窗口依赖说明：`"native"` 导入文件本身不启动 GUI 或 CLI；但在后续调用 `PVsystCLI.exe` 运行仿真时，CLI 内部仍可能用到隐藏窗体或消息窗体。特别是损失图 PDF 的生成需要创建窗口权限：在禁用窗口创建的桌面环境中，CLI 渲染损失图页面会失败（不输出损失图 PDF），仅在允许创建窗口的环境中才能正常生成损失图页面。`export-logs` 等日志导出命令的可用性不影响渲染器对窗口权限的这一依赖。

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
| `completed` | 核对全年 8760/8784 小时时间轴、数值列、`GlobShd` 阴影列、PDF（`report_pdf=False` 的 CSV-only 模式跳过 PDF、只核对 CSV 与源文件字节）与源文件指纹通过，将 `native_consumption_verified` 置为 `true` |

说明：`scene_import_method="native"` 时沿用上表 `native_saved` / `gui_closed` 旧检查点名称，但 native 分支既不启动也不关闭 GUI（`gui_closed` 仅为阶段名，无 GUI 进程操作）；表中这两行的 GUI 描述仅适用于默认 `gui` 路线。

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
