# 从规格书摘录到 3D 住宅全年交付（`pvsyst_run_sourced_house_task`）

`pvsyst_run_sourced_house_task` 将三条已有的工作流串成一个统一调用：
1. [`pvsyst_prepare_sourced_design`](weather-engineering-review.md)：从规格书原文摘录（`sources` + `declarations`）中提取温度系数、电压电流限值和极值温度，组装 `electrical_design` 包络。
2. [`pvsyst_prepare_design_project`](design-project-preparation.md)：根据组装好的电气包络匹配模板、规划整数串并联容量、准备气象文件，落盘生成不跑全年仿真的源工程（`<source-stem>_prepare_<year>.json`）。
3. [`pvsyst_run_house_task`](native-house-task.md)：基于刚生成的源工程构建 `.pvc2` 房屋网格（支持可选周边遮挡物 `obstacles`）、以默认免 GUI 原生写入生成带 3D 场景的新变体，最后用 `PVsystCLI.exe` 跑出带 3D 阴影的全年 CSV/PDF 报告（`<final-stem>_house_task.json`）。传入 `report_pdf: false` 时同一流程只产出 CSV-only 报告（见下表透传字段，默认仍是 PDF）。

这样整个过程只需要跑**最后那一次**带 3D 阴影的全年 CLI 仿真，中间准备源工程时不会白跑一次无阴影全年。

## `spec` 入参结构

| 字段名 | 说明与要求 |
|---|---|
| `design` | 复用 `SingleDesignRequest` 结构（见[单方案设计输入](single-design-inputs.md)）：其中的 `design.new_project` 是中间生成的**源工程**名称；必须指定精确的明文 `module.file_name` 和 `inverter.file_name`；注意省略 `design.capacity` 和 `design.electrical_design`（由下面的 `declarations` 自动生成） |
| `new_project`, `variant_name` | 最终 3D 住宅工程的 `.PRJ` 文件名（不能与 `design.new_project` 同名）与新变体显示标题 |
| `sources`, `declarations` | 规格书来源声明与字段数值/单位摘录（格式同 `pvsyst_prepare_sourced_design`） |
| `goals` | 可选的 `SourcedDesignGoals`（目标容量也可直接在 `design.target_dc_kw` 中给出，两者不能矛盾） |
| `house`, `layout`, `module_geometry`, `obstacles` | 房屋三维网格、PV 分区排布、组件几何尺寸及可选周边遮挡物 `obstacles`（`building`、`tree`、`wall`、`mesh`，格式同 [`pvsyst_run_house_task`](native-house-task.md)） |
| `string_layout`, `shading_mode`, `report_pdf` | 可选透传字段：`string_layout` 为显式 per-cell 组串接线结构（`{tables:[{name,subarray_id,cells:[{row,col,global_string}]}]}`，见[住宅与光伏建模](house-pv-modeling.md) 第 6 节）；`shading_mode`（`linear`/`module_layout`）选择阴影模式；`report_pdf`（`StrictBool`，默认 `null`）语义同 [`pvsyst_run_house_task`](native-house-task.md)：省略、`None` 或 `True` 保留默认的 CSV+PDF 输出，`False` 则仅导出 CSV（`dry_run` 同样会先校验本机 PVsyst 许可状态）。显式 `string_layout` 仅在 `shading_mode="module_layout"` 时启用原生显式校验 |

## 状态流转与两步确认开关

函数签名为：`pvsyst_run_sourced_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False, accept_source_review=False)`。

- **`needs_context`**：`declarations` 里的规格书参数还没给全（查看返回的 `source_binding.missing_fields` 和 `electrical_design` 草案补齐即可）。
- **`needs_review`（前置拦截）**：如果规格书绑定或在线天气带有复核项（例如使用了 ERA5 `endpoint_mean` 温度近似或模板继承项），工具默认会先停下并返回 `needs_review`。确认复核项可接受后，需显式传入 **`accept_source_review=true`** 才会继续往下走（并且最终任务跑完后顶层 `status` 仍会保持 `needs_review` 以保留记录）。
- **`ready_for_preparation` / `ready_for_weather`**：`dry_run=true` 时的预检通过状态（因为源工程或在线天气还没真正落盘，此时 `house_preflight_complete=false`）。
- **正式执行**：传入 `dry_run=false, confirm=true, accept_source_review=true`，一次性跑完“源工程落盘 → 原生 3D 变体生成 → CLI 全年仿真”。

## 双清单断点续跑（`resume=true`）

该任务不发明第三套状态文件，直接复用 `Results` 目录下的两份标准清单：
1. 源工程准备清单：`<source-stem>_prepare_<year>.json`
2. 住宅任务清单：`<final-stem>_house_task.json`

当传入 `resume=true` 时：
- 如果源工程已经准备完毕（`_prepare_` 清单已完成且 `.PRJ`/`.VCx`/`.PAN`/`.OND`/`.SIT`/`.MET` 指纹未变），工具会直接跳过天气下载和源工程配置，进入或接续后面的住宅任务。
- 如果住宅任务也已在 `native_saved`、`simulated` 或 `completed` 阶段，则直接按 `pvsyst_run_house_task` 的恢复规则续跑或复核结果（若仅最后一步 CLI 仿真失败，同样可加 `retry_simulation=true` 换新输出名重跑 CLI）。

## CSV-only 输出与续跑说明

- 当传入 `report_pdf=false` 时，仿真仅导出 CSV 而不生成 PDF（保持默认 `None`/`true` 时输出行为不变）；`dry_run=true` 仅做配置预检而不修改源工程。
- `pvsyst_prepare_sourced_design` 会在缺少数据手册标量时保留 `needs_review` 并明确列出假设值与 `capacity.target_dc_kw`。
- `resume=true` 会先校验源工程与已完成结果 CSV 的覆盖范围和完整性，通过后直接复用现有结果，避免重复消耗仿真执行次数。
