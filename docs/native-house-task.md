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
| `shading_mode` | 可选；省略时保留原有线性阴影流程，`module_layout` 使用原生组件布局与精细电气阴影计算 |
| `scene_import_method` | 可选；省略或 `"gui"` 保持现有 GUI 导入（原行为与检查点形状不变），`"native"` 改用免 GUI 的原生导入阶段（仅限下文支持边界）。native 的**导入阶段**只写 STORED 变体；本工具 `pvsyst_run_house_task` 在 `dry_run=false` 且 `confirm=true` 时，会沿同一流程自动继续全年 CLI 仿真，成功后上下文匹配新变体才把 `native_consumption_verified` 置 true。仅当单独调用底层 [`pvsyst_import_house_pv_scene`](tool-reference.md)`(method="native")` 时，才需对返回变体另调 `pvsyst_run_simulation` |

## Module Layout 电气阴影

对已满足双屋面条件的输入，在 `spec` 中加入 `"shading_mode": "module_layout"`。任务会在 PVsyst 中放置组件、自动分配组串、选中精细计算模式，再另存新变体；不在 MCP 内计算或近似电气阴影。

当前入口限定为两个固定朝向、子阵列编号 `1` 和 `2`，每个朝向独占一张横装矩形表，每个子阵列恰好一条 `1..100` 块组件的组串、一个独立逆变器及一个 MPPT，组件间距为零。沿用普通双屋面入口的站点、模型、朝向、坐标及原生版本要求。自动分配采用原生水平、左下起、连续填充方式；自定义接线、跨多表分串及任意多朝向尚未接入。

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

## 场景导入方式（`scene_import_method`）

在 `spec` 中加入 `"scene_import_method"` 选择场景导入路线：

- 省略或 `"gui"`：保持现有流程——冷启动自有 PVsyst GUI 进程完成导入、朝向关联、校验与另存，入参、检查点与阶段形状均不变。
- `"native"`：免 GUI，用已声明的房屋网格与 PV 区域在内存中构建并校验原生场景记录，直接写出一份新变体；**导入阶段**不启动 `PVsyst.exe`、不操作 `INI`/剪贴板/license、不运行仿真。全年仿真由高层 `pvsyst_run_house_task(dry_run=false, confirm=true)` 在同一流程内自动继续执行（见下）。

`"native"` 的支持边界固定：高层 `pvsyst_run_house_task` 原生导入仍只支持双朝向、两张横装零间距矩形表（源子阵 `1`/`2` 各一条独立组串/一个独立逆变器/一个 MPPT，`MAX_TABLES_BRIDGED=2`），不接受单朝向；单朝向源在写入前的预检即被明确拒绝并给出原因，不做 GUI 回退。单朝向、单子阵、`1..3` 张横装零间距矩形表仅由低层 [`pvsyst_import_house_pv_scene`](tool-reference.md)`(method="native")` 导入工具提供存储写入与读取，其单朝向线性 3 表 profile 已用公开 product 导入 + CLI 完成真实原生消费验收（有限 3 表 profile，不作逐表独立归因）；`1..3` 是导入阶段的存储上限，作者仍接受 `1..64` 张表（作者能力不等于 Native 能力）。双朝向与单朝向线性 3 表模板均已原生验证；单朝向模板为本轮新增低层实现、存储读取自洽（`stored_consistent`）。精细 `module_layout` 已对双朝向单串模板验证；单朝向仅接受低层 `method="native"` 的“恰 2 表 / 2 源串 / 2 逆变器 / 2 总 MPPT（`NInvMPPT=2`，非每机 2）”fine 窄模板（详见[住宅与光伏建模](house-pv-modeling.md)），其余单朝向布局在写入前被拒绝。所有模板都要求同一 `PAN`/`OND`，各分区组件数累加须严格等于源变体组件总数。不满足条件的请求在写入前停止并给出原因，不做暗中修正；条件细节见[住宅与光伏建模](house-pv-modeling.md)。

状态语义：高层 `pvsyst_run_house_task` 的 **native 导入阶段**只产出 STORED 记录（`stored_geometry_verified` / `stored_module_layout_verified` 反映已保存记录），此时 `imported` / `native_geometry_verified` / `native_consumption_verified` 仍为 false——表示 PVsyst 尚未加载或消费该变体，stored 不等于已 load；`native_geometry_verified` 与重载标志不能由 stored 记录伪置。若同时传入 `dry_run=false` 与 `confirm=true`，该次调用会沿原流程自动继续全年 CLI 仿真并产出 CSV/PDF，成功后上下文匹配新变体才把 `native_consumption_verified` 置 true。只有单独调用底层 [`pvsyst_import_house_pv_scene`](tool-reference.md)`(method="native")` 这个导入阶段独立入口时，才需再对返回的变体调用 [`pvsyst_run_simulation`](tool-reference.md) 跑仿真。

窗体事实：`"native"` 的建文件步骤不启动 GUI/CLI、不涉及窗体；但随后用 CLI 运行仿真（`pvsyst_run_simulation`）时，仿真流程自身仍可能用到隐藏的 `TForm`/消息窗体。因此 `"native"` 不等于「零窗体」或「无桌面依赖」，该说明属于 CLI 仿真流程本身，详见[住宅与光伏建模](house-pv-modeling.md)。

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
