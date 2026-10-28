# 单方案设计输入与全年流程

`pvsyst_simulate_design(spec, dry_run=True)` 是面向单子阵列固定倾角并网项目（`fixed_unshaded_grid`）的最高层一站式入口。它把组件查找、模板筛选（或空工作区种子初始化）、串并联整数容量规划、天气下载/转换以及[模板全年仿真](automatic-simulation.md)打包在一个统一的 `spec` 里执行。

如果只想把工程文件配好落盘、暂不跑全年仿真（例如后面还要导 3D 房屋场景），可以把完全相同的 `spec` 传给 [`pvsyst_prepare_design_project`](design-project-preparation.md)。

## `spec` 顶层字段

| 字段名 | 说明与要求 |
|---|---|
| `new_project` | 新生成的 `.PRJ` 工程文件名（连同派生的天气、清单和结果文件均不能与现有文件重名） |
| `year` | 仿真年份（整数 `1969..2068`），需与所选天气数据覆盖年份一致 |
| `target_dc_kw` | 目标直流装机容量（kWp，正数） |
| `tilt` / `azimuth` | 固定倾角（`0..90°`）与方位角（`-180..180°`，遵循 PVsyst 惯例：正南为 `0°`，偏西为正，偏东为负） |
| `module` / `inverter` | 组件与逆变器标识：可传精确的 `{"manufacturer": "...", "model": "..."}` 或直接传工作区文件名 `{"file_name": "..."}` |
| `template_project` / `variant` | 可选；指定作为底板的模板工程与变体，不传则自动扫描工作区择优或使用内置标准种子 |
| `weather` | 气象数据模式：`existing`（已有 `.SIT`/`.MET`）、`csv`（本地标准逐时 CSV）或 `open_meteo`（在线下载 ERA5） |
| `capacity` / `electrical_design` | **二选一必填**：`capacity` 直接给算好的电压/电流边界；`electrical_design` 给带出处的温度系数与极值，由工具推导边界 |
| `losses` | 可选损失参数覆盖，支持 `LIDLoss`、`PMismMPP`、`ModuleQualityLoss` 与主阵列 `UFactor` |
| `report_pages` | 可选 PDF 报告页名列表，见[天气与报告](automatic-simulation.md#天气与报告) |

调用示例：

```json
{
  "spec": {
    "new_project": "DESIGN.PRJ",
    "year": 2024,
    "target_dc_kw": 21.6,
    "tilt": 28,
    "azimuth": 0,
    "module": {"file_name": "ValidatedModule.PAN"},
    "inverter": {"file_name": "ValidatedInverter.OND"},
    "weather": {"mode": "existing", "sit_name": "SITE.SIT", "met_name": "SITE.MET"},
    "capacity": {
      "max_dc_ac_ratio": 1.3,
      "capacity_tolerance_pct": 5,
      "voltage_standard": "IEC",
      "cold_voc_v": 65,
      "hot_vmp_v": 38,
      "cold_vmp_v": 47,
      "module_max_operating_current_a": 16,
      "module_max_short_circuit_current_a": 18,
      "mppt_max_operating_current_a": 32,
      "mppt_max_short_circuit_current_a": 40,
      "design_min_temperature_c": -10,
      "design_min_operating_temperature_c": 20,
      "design_max_operating_temperature_c": 70,
      "design_basis": "Site extreme -10 C ambient, 20..70 C cell operating range."
    }
  },
  "dry_run": true
}
```

## 组件检索、模板筛选与空工作区初始化

1. **组件匹配**：按大小写不敏感的精确匹配查找 `manufacturer + model`（或直接定位 `file_name`）。工作区 `ComposPV` 下的散文件（loose file）优先级高于 `DataRO` 内置库；如果在同一优先级目录里搜出多个同名同型号文件，会报错提示改用明确的 `file_name`。选定 `.PAN`/`.OND` 后会先跑一遍静态校验并读取电气额定值。
2. **模板排序**：如果不传 `template_project`，工具会扫描工作区里所有结构合规的单子阵固定平面模板，试算容量规划后按“逆变器台数改动最少 → 参数修改步数最少 → 容量偏差最小 → 名称字典序”自动挑出最顺手的底板。
3. **空工作区自动打底（Bootstrap）**：对于刚建好的空工作区（要求根目录名为 `PVsyst8.1_Data`），即使里面一个 `.PRJ` 都没有、甚至 26 个标准子目录还没建全，只要放好了所需的明文 `.PAN`/`.OND`，`dry_run=false` 执行时会自动补齐缺失的工作区子目录，并从内置的标准种子模板派生出基础工程。

## 两种容量与电气边界写法

- **直接传 `capacity`**：直接给出冷态 $V_{oc}$、冷/热态 $V_{mp}$、组件与单路 MPPT 最大工作/短路电流等数值边界，详见[容量规划](capacity-sizing.md)。
- **传 `electrical_design`（带出处推导）**：适合从规格书摘录原始参数的场景（也可配合 [`pvsyst_prepare_sourced_design`](weather-engineering-review.md) 自动组装）。需要注意几个常见坑：
  - 温度系数必须写明单位（如 `%/°C` 或 `V/°C`）和出处；不能拿功率温度系数 $\beta_{P\max}$ 去冒充工作电压温度系数 $\beta_{Vmp}$。
  - 逆变器 `.OND` 里的整机 `IDCMax` 不能直接当成单路 MPPT 的最大输入电流（也不能简单除以 MPPT 路数，因为很多逆变器各路分配不均或总量受限），必须明确声明单路 MPPT 电流上限。
  - 如果用短路电流温度系数 $\alpha_{Isc}$ 推导最大电流，必须同时给出最大有效辐照度 `max_irradiance_w_m2` 和双面背面增益 `rear_gain_factor`（不会默认按 $1000\text{ W/m}^2$ 或背面增益 $0$ 偷算）。
  - 温度须满足 `-100 ≤ 最低环境温度 ≤ 最低组件工作温度 ≤ 最高组件工作温度 ≤ 150 °C`，且最低环境温度需提供设计依据 `min_ambient_basis`（不能直接拿某一年气象文件里的最低温当多年极端低温）。

## 三种气象模式（`weather.mode`）

- `existing`：直接使用工作区现有的 `sit_name` 和 `met_name`。
- `csv`：指定 `Meteo` 下已放好的标准逐时 CSV，自动调 [`pvsyst_prepare_weather`](weather-preparation.md) 生成 `.SIT`/`.MET`。
- `open_meteo`：在线调 Open-Meteo 拉取指定年份的 ERA5 逐时辐射（`GHI`/`DHI`）与 2 米气温（`temperature_2m`）。
  - 地点可以通过 [`pvsyst_resolve_location`](tool-reference.md) 查出唯一 `location_id` 传入，或者直接传完整的 `latitude`、`longitude`、`site_label`、`altitude`、`country_code`、`timezone_name`（两种方式二选一，不能混传）。
  - 下载器会自动把 UTC 数据对齐到当地冬季标准时（`LegalTime`，要求时区为 `-12..+12` 整点偏移且年内标准时不变；半小时时区或像 `Europe/Dublin`、`Africa/Casablanca` 这种带负夏令时的时区会直接拦截，避免错位 1 小时）。
  - 因为 ERA5 的 `temperature_2m` 是整点瞬时值，工具会取前后两个整点的算术均值（`endpoint_mean`）作为该小时的平均气温，并在返回结果中保留一条 `needs_review` 说明。

注意：当 `weather.mode="open_meteo"` 且 `dry_run=true` 时，因为还没真正联网下载 CSV，预检会返回 `status="ready_for_weather"` 且 `execution_preflight_complete=false`。
