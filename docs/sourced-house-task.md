# 从规格书摘录到 3D 住宅全年交付（`pvsyst_run_sourced_house_task`）

`pvsyst_run_sourced_house_task` 将三条已有的工作流串成一个统一调用：
1. [`pvsyst_prepare_sourced_design`](weather-engineering-review.md)：从规格书原文摘录（`sources` + `declarations`）中提取温度系数、电压电流限值和极值温度，组装 `electrical_design` 包络。
2. [`pvsyst_prepare_design_project`](design-project-preparation.md)：根据组装好的电气包络匹配模板、规划整数串并联容量、准备气象文件，落盘生成不跑全年仿真的源工程（`<source-stem>_prepare_<year>.json`）。
3. [`pvsyst_run_house_task`](native-house-task.md)：基于刚生成的源工程构建 `.pvc2` 房屋网格、启动 PVsyst 8.1.6 GUI 导入并绑定屋顶朝向、另存新变体、退出 GUI，最后用 `PVsystCLI.exe` 跑出带 3D 阴影的全年 CSV/PDF 报告（`<final-stem>_house_task.json`）。传入 `report_pdf: false` 时同一流程只产出 CSV-only 报告（见下表透传字段，默认仍是 PDF）。

这样整个过程只需要跑**最后那一次**带 3D 阴影的全年 CLI 仿真，中间准备源工程时不会白跑一次无阴影全年。

## `spec` 入参结构

| 字段名 | 说明与要求 |
|---|---|
| `design` | 复用 `SingleDesignRequest` 结构（见[单方案设计输入](single-design-inputs.md)）：其中的 `design.new_project` 是中间生成的**源工程**名称；必须指定精确的明文 `module.file_name` 和 `inverter.file_name`；注意省略 `design.capacity` 和 `design.electrical_design`（由下面的 `declarations` 自动生成） |
| `new_project`, `variant_name` | 最终 3D 住宅工程的 `.PRJ` 文件名（不能与 `design.new_project` 同名）与新变体显示标题 |
| `sources`, `declarations` | 规格书来源声明与字段数值/单位摘录（格式同 `pvsyst_prepare_sourced_design`） |
| `goals` | 可选的 `SourcedDesignGoals`（目标容量也可直接在 `design.target_dc_kw` 中给出，两者不能矛盾） |
| `house`, `layout`, `module_geometry` | 房屋三维网格、1..2 个 PV 分区排布与组件几何尺寸（格式同 [`pvsyst_run_house_task`](native-house-task.md)） |
| `string_layout`, `shading_mode`, `scene_import_method`, `report_pdf` | 可选透传字段：`string_layout` 为显式 per-cell 组串接线兄弟块（`{tables:[{name,subarray_id,cells:[{row,col,global_string}]}]}`，见[住宅与光伏建模](house-pv-modeling.md) 第 5 节）；`shading_mode`（`linear`/`module_layout`）与 `scene_import_method`（`gui`/`native`）选择阴影与导入路线；`report_pdf`（`StrictBool`，默认 `null`）语义同 [`pvsyst_run_house_task`](native-house-task.md)：省略/`None`/`True` 保留原 CSV+PDF（原 PDF 默认 `True` 不改），`False` 选择 CSV-only 且随 `False` 透传给子 house。均原样透传给内部 `pvsyst_run_house_task`；省略时保持旧 None 行为与检查点形状不变，显式 `string_layout` 仅在 `scene_import_method="native"` + `shading_mode="module_layout"` 时进入原生显式门。注意高层 sourced 的 dry 仍**先走原许可（lic）校验**，本波不实际发放该工具、也不 patch 许可或伪造事实让 forward 案例通过；`report_pdf` 经 sourced 链已验证 wire 发布、`StrictBool` 严格拒绝、省略/`None` 旧形状与 `False` 透传给子 house；CSV-only 的 prepared-source→child 原生全年→completed resume 已在下方「CSV-only 原生全链外部契约（已执行验证）」节实测通过，PDF 模式、GUI 导入及其它 profile/source 业务仍未覆盖 |

## 状态流转与两步确认开关

函数签名为：`pvsyst_run_sourced_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False, accept_source_review=False)`。

- **`needs_context`**：`declarations` 里的规格书参数还没给全（查看返回的 `source_binding.missing_fields` 和 `electrical_design` 草案补齐即可）。
- **`needs_review`（前置拦截）**：如果规格书绑定或在线天气带有复核项（例如使用了 ERA5 `endpoint_mean` 温度近似或模板继承项），工具默认会先停下并返回 `needs_review`。确认复核项可接受后，需显式传入 **`accept_source_review=true`** 才会继续往下走（并且最终任务跑完后顶层 `status` 仍会保持 `needs_review` 以保留记录）。
- **`ready_for_preparation` / `ready_for_weather`**：`dry_run=true` 时的预检通过状态（因为源工程或在线天气还没真正落盘，此时 `house_preflight_complete=false`）。
- **正式执行**：传入 `dry_run=false, confirm=true, accept_source_review=true`，一次性跑完“源工程落盘 → GUI 导入另存 → CLI 全年仿真”。

## 双清单断点续跑（`resume=true`）

该任务不发明第三套状态文件，直接复用 `Results` 目录下的两份标准清单：
1. 源工程准备清单：`<source-stem>_prepare_<year>.json`
2. 住宅任务清单：`<final-stem>_house_task.json`

当传入 `resume=true` 时：
- 如果源工程已经准备完毕（`_prepare_` 清单已完成且 `.PRJ`/`.VCx`/`.PAN`/`.OND`/`.SIT`/`.MET` 指纹未变），工具会直接跳过天气下载和源工程配置，进入或接续后面的住宅任务。
- 如果住宅任务也已在 `native_saved`、`gui_closed` 或 `completed` 阶段，则直接按 `pvsyst_run_house_task` 的恢复规则续跑或复核结果（若仅最后一步 CLI 仿真失败，同样可加 `retry_simulation=true` 换新输出名重跑 CLI）。

## CSV-only 原生全链外部契约（已执行验证）

`tests/contracts/test_sourced_house_csv_native_boundary.py` 只固定公开 stdio-MCP 外部行为，不绑定内部 helper、调用顺序或检查点快照：

- `report_pdf` 的 nullable-boolean wire 与 StrictBool 严格拒绝（省略/`None`/`True` 维持原 CSV+PDF 默认，`False` 选择 CSV-only）；
- 0 CLI 的 `pvsyst_prepare_sourced_design` 绑定：`needs_review` 保留、每个非数据手册标量都是显式 `assumption`、`missing_fields` 为空、`capacity.target_dc_kw` 存在；
- 一次 **opt-in** 真实原生全链（`PVSYST_CONTRACT_REAL_NATIVE=1`）：恰好两次公开 confirm 请求 ——（1）`confirm=True, resume=False` 准备源工程并交付一个子住宅 2024 闰年 8784 小时 CSV-only 全年（无 PDF）；（2）仅在该次成功且原生消费已验证后 `resume=True` 复核同一 CSV，无新仿真、无新增 run、源工程/CSV 逐字节一致。

本阶段实测（原运行观察，非自建 golden）：3×4=12 块 600 W 单串单表 7.2 kW，2024 全年 8784 行 / 14 个数值列；E_Grid 合计 12320.8084 kWh、PR 90.685736%（均取自该次运行自身 CSV/summary，未独立构造基准）。源工程与住宅均保留 `needs_review`，9 个 caller 标注假设仍属人工复核，非工程/厂商认证。

范围仅此：CSV-only 全年 + completed resume。PDF 模式、GUI 导入、其它 profile/source 业务与完整 245 矩阵不在此；高层 sourced 的 dry 仍**先走原许可校验**，契约不发送任何 valid `dry_run` 请求。旧的 CSV-only 因 caller 省略 `report_pdf` 落到默认 `None` 而产出 PDF 的失败样本原样保留、未擦除，本契约以显式 `spec["report_pdf"]=False` 修复该调用输入问题。契约 env：`PVSYST_CONTRACT_CLI`、`PVSYST_CONTRACT_WORKSPACE`、`PVSYST_CONTRACT_SOURCED_JSON`（完整 `SourcedHouseTaskRequest` 体）、`PVSYST_CONTRACT_REAL_NATIVE`。
