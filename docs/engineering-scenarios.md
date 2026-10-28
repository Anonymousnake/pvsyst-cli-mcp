# 多方案仿真结果对比

`pvsyst_compare_engineering_scenarios`（底层函数 `compare_engineering_scenarios`）用于把两到三个已跑完的 PVsyst 仿真方案放到一起做差值对比（以列表第一项为基准方案，逐项计算“候选方案 − 基准方案”），并在 `Results` 目录下生成配套的 JSON、CSV 和中文 Markdown 对比报告。

```python
compare_engineering_scenarios(
    store,
    objective="比较同一组件、天气和容量下的固定平面与跟踪方案",
    scenarios=[
        {"label": "固定平面", "project": "BASELINE.PRJ", "variant": "VC0",
         "csv_name": "fixed_2024.csv", "declared_changes": [],
         "run_manifest_name": "fixed_2024_run.json", "year": 2024},
        {"label": "跟踪方案", "project": "TRACKING.PRJ", "variant": "VC0",
         "csv_name": "tracking_2024.csv", "declared_changes": ["orientations"],
         "run_manifest_name": "tracking_2024_run.json", "year": 2024,
         "assumptions": ["两方案使用同一组件与气象文件"]},
    ],
    output_name="fixed_tracking_comparison.json",
    dry_run=True,
)
```

## 方案入参说明

`scenarios` 列表中的每个方案都需要指定 `label`、`project`、`variant`、`csv_name` 和 `declared_changes`：
- 基准方案（第 0 项）的 `declared_changes` 必须为空列表 `[]`。
- 候选方案在 `declared_changes` 中声明本次打算修改的参数分组或点路径，例如 `"orientations"`、`"storage"` 或 `"subarrays.0.values.NModSerie"`。顶层支持的配置分组包括：`project`、`system`、`main_array`、`orientations`、`subarrays`、`grid_circuit`、`sources`、`load`、`storage`、`shading_scene`。
- 工具会自动把实际发生改变的配置分成“符合声明的有意改动（intended）”和“声明之外的意外差异（unintended）”，防止对比两个方案时不小心漏改或误改了其他参数而不自知。
- 可选字段包括 `year`、`nominal_dc_power_kw`（用于计算比发电量 kWh/kWp）和文本列表 `assumptions`。

## 历史运行清单绑定（Run Manifest）

为什么要传 `run_manifest_name`？因为 PVsyst 导出的 CSV 头部只写了当时的工程名、变体名和 `.SIT`/`.MET` 文件名。如果跑完仿真后有人又打开 `.VC0` 改了参数，光读现在的 `.VC0` 文件根本没法还原当初跑出那份 CSV 时的真实配置。

- **有绑定清单**：传入 `pvsyst_run_engineering_task` 生成的 `<stem>_engineering_<year>.json` 清单名。工具会通过 `load_engineering_run` 校验当时的运行时快照（`input_handoff`）、工程文件与 CSV/PDF 的哈希指纹是否一致。如果提供了清单但发现文件已被篡改或仿真未完成，工具会直接报错停止。
- **无绑定清单**：如果不传 `run_manifest_name`，工具仍然会计算 CSV 数值差异并对比当前磁盘上的 `.PRJ`/`.VCx` 保存状态，但会把 `historical_input_binding_verified` 设为 `false`，且整体状态标为 `needs_review`。
- **逐时负荷路径归一化**：在对比两个带逐时负荷的方案时，因为 `.VCx` 摘要不展开 365/366 天的 `Day_*` base62 缓存，只有当两个方案的运行清单都确认 `load_binding.embedded_snapshot_matches=true` 且绑定的负荷清单、源 CSV、归一化 CSV 完全一致时，工具才会把工作区临时目录路径（`PathName`）的差異归入 `normalized_location_differences`，视为相同负荷条件。

## 逐时列积分与指标口径

底层调用 `compare_results` 对齐各方案的逐时 CSV：
- **时间轴与单位**：所有方案的时间戳必须逐行严格对齐且递增，同名列单位必须一致。缺失值按缺测统计，不会补 0 或用其他列加减凑数。
- **能量积分与极值**：功率列（`W` / `kW`）在固定时间步下自动积分为 `kWh`。每个方案的 `result_summary.columns` 会记录首个时间步的值（`first_value`）以及全时段的最小值、最大值和出现时刻；对比结果还会列出每个共有指标最大绝对差值发生的具体时刻（方便定位是哪一天的限电或停电拉开了差距）。
- **光储专用列处理**：
  - 电网与负荷列（`EFrGrid`、`E_Grid`、`E_User`、`E_Miss`、`EToGrid`）、阵列能量列（`EArray`、`EUseful`、`EDirUse`、`E_Solar`、`EUnused`）均直接从各自的原生导出列读取。
  - `EBatCh` / `EBatDis` 是电池直流侧（DC）充放电量，工具不会用效率系数去反推交流侧功率。
  - `SOC`、`SOC_Beg`、`SOC_End` 和 `SolFrac` 按原样比例或百分比统计极值与差值，不做时间积分；`metrics[].soc_changes` 会分别汇总两方案的初始 SOC、全时段最低/最高 SOC 及其差值。

## 输出文件

`output_name` 传入不含目录的 `.json` 文件名（如 `"fixed_tracking_comparison.json"`）。当 `dry_run=false` 时，会在 `Results` 下生成三个同前缀的新文件（若同名文件已存在则拒绝覆盖）：
1. `<stem>.json`：完整的结构化对比数据，并通过 `report_files` 字段记录另外两份报告的文件指纹，供 `pvsyst_export_engineering_delivery` 打包时校验完整性。
2. `<stem>.csv`：各方案指标与“候选 − 基准”差值的扁平长表。
3. `<stem>.md`：包含共同条件、有意/意外配置差异、核心指标汇总、关键差异时刻与复核提示的中文 Markdown 报告。
