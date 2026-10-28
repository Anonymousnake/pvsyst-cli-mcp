# 源工程准备（不执行全年仿真）

`pvsyst_prepare_design_project(spec, dry_run=True)` 复用了 `pvsyst_simulate_design` 的前半套流水线——组件查找、模板匹配、电气包络与整数容量计算、天气下载/转换以及 `.PRJ`/`.VCx` 参数写入，但在工程文件落盘并校验通过后立刻结束，不生成 `.SFI`，也不调用 `PVsystCLI.exe` 跑全年仿真。

这个工具最常见的用途是**给后续需要进 GUI 绑 3D 场景的任务打底**：比如做住宅光伏建模时，先用它把配好组件、逆变器、串并联数量和站点气象的干净源工程准备好，然后再把生成的 `project`/`variant` 传给 `pvsyst_run_house_task` 导入 3D 房屋场景并跑带阴影的全年仿真，省得在没加阴影前白白浪费一次 CLI 仿真配额和时间。

## 入参说明

请求体与 `pvsyst_simulate_design` 完全共用 `SingleDesignRequest` 结构（详见[单方案设计输入](single-design-inputs.md)）：
- **适用范围**：单子阵列、单固定平面、无阴影场景的并网工程（`fixed_unshaded_grid`）。
- **容量配置**：`capacity`（直接传电压/电流/温度边界）与 `electrical_design`（传带出处的温度系数和极值，由工具推导边界）二选一必填。
- **气象配置**：`weather.mode` 支持 `existing`（直接绑定工作区已有 `.SIT`/`.MET`，可选传 `weather_manifest_name` 核对来源清单）、`csv`（把 `Meteo` 下的标准逐时 CSV 转成新 `.SIT`/`.MET`）和 `open_meteo`（从 Open-Meteo ERA5 下载指定年份数据并转成 `.SIT`/`.MET`）。
- **模板选择**：传了 `template_project` / `variant` 就严格用指定模板；没传则自动扫描工作区筛选合格模板，若工作区为空还会自动用内置的标准种子模板初始化。
- **`report_pages`**：只校验页名是否合法并记入清单，留给后续真正跑仿真的工具使用。

如果要一步到位完成“从资料摘录 → 准备源工程 → 3D 住宅建模与全年仿真”，也可以直接调用组合入口 `pvsyst_run_sourced_house_task`（见[引用资料到住宅交付](sourced-house-task.md)）。

## 返回状态

| `status` | 含义 |
|---|---|
| `blocked` | 必填项缺失、参数超界、文件同名冲突或模板不兼容 |
| `ready` | `dry_run=true` 且使用本地已有气象文件时，全部预检通过 |
| `ready_for_weather` | `dry_run=true` 但气象模式是 `open_meteo`（或依赖尚未落盘的内存种子），此时 `execution_preflight_complete=false`，仅代表下载与准备计划合法 |
| `prepared` | `dry_run=false` 执行完毕，新 `.PRJ`/`.VCx` 已落盘且回读校验通过 |
| `needs_review` | 工程已准备完成（`prepared=true`），但带有需要留意的复核项（例如 ERA5 端平均温度近似或模板继承项提示） |
| `failed` | 写盘或气象转换中途出错，保留现场文件与回滚记录供排查 |

由于该入口不跑仿真，返回结果中的 `simulated` 和 `native_consumption_verified` 恒为 `false`，`metrics` 恒为 `null`。因为不触发 CLI `run-simulation`，即使当前试用版（TRIAL）的仿真剩余次数为 0，只要 `create-site` / `convert-meteo` 可用，依然可以正常准备工程。

## 准备清单与回滚

执行成功后，工具会在 `Results` 目录下生成 `<new-project-stem>_prepare_<year>.json` 清单（与全年仿真的 `_annual_` 清单分开命名），里面记录了：
- `prepared_project`：生成的 `project`、`variant`、`year`、`site`、`met` 与清单文件名。
- `preparation_context`：完整的请求参数、解析到的组件模型、容量规划结果与复核项，可直接传给 `pvsyst_review_project_inputs` 做工程保存值比对。
- `recovery`：按逆序排列的文件回滚步骤。
