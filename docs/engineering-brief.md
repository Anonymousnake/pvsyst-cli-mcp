# 工程简报只读预审

在拿到项目的 `.PRJ` 工程或 `.PAN`/`.OND` 组件文件之前，往往只有一份可研报告或设备清册。`pvsyst_review_engineering_brief(brief)` 用来对从文档里摘录出来的结构化工程参数做交叉验算，自动找出前後矛盾的数量（比如总组件数除以每串组件数除不尽、总装机容量和组件铭牌乘积对不上）以及还缺哪些建模必填项。

这是纯内存的算术与完整性检查工具，不读写本地文件，也不启动 `PVsystCLI.exe`。

## 入参结构

`brief` 是一个严格校验字段名的字典，包含三部分：

- `sources[{id, title, locator}]`：资料来源列表（最多 50 项），比如可研报告、设备清册或图纸编号。
- `claims[{id, field, scope, value, unit, source_id, pdf_page, excerpt}]`：摘录的参数条目（最多 500 项，`id` 唯一，`pdf_page` 为从 1 起算的页码，`excerpt` 原文摘录最多 2000 字符）。同一个 `field` 可以录入多条来自不同页码或章节的声明，方便工具揪出文档前后的数字打架。数量类字段必须传整数，其余数值传有限浮点数或整数（不接受布尔值或数字字符串）。
- 可选 `blocks[{id, count, strings_per_block?, inverters_per_block?, source_id, pdf_page, excerpt}]`：光伏方阵/子阵分块表（最多 100 项），记录每种分块的个数以及单块内的组串数和逆变器台数。

单条 `claim` 示例：

```json
{
  "id": "project-modules",
  "field": "module_count",
  "scope": "project",
  "value": 218690,
  "unit": "count",
  "source_id": "feasibility",
  "pdf_page": 15,
  "excerpt": "共 218690 块"
}
```

## 支持的字段与单位

字段名和单位必须按表严格配对（如果在原文里看到的是 MW，摘录时请自行换算成 kW 并在 `excerpt` 或 `locator` 里注明原值；注意不要把视在功率 kVA 直接当成有功 kW）：

| `field` | `unit` 与取值要求 |
|---|---|
| `module_count`, `modules_per_string`, `string_count`, `inverter_count`, `inverter_input_count`, `block_count` | `count`，正整数 |
| `module_power_w` | `W` |
| `module_vmp_v`, `module_imp_a` | 分别为 `V`、`A` |
| `inverter_power_kw`, `dc_capacity_kw`, `ac_capacity_kw`, `nominal_capacity_kw`, `storage_power_kw` | `kW` |
| `storage_energy_kwh` | `kWh` |
| `tilt_deg`, `azimuth_deg` | `deg`，分别为 `0..90`、`-180..180` |
| `clearance_m`, `row_spacing_m` | `m` |
| `pan_file`, `ond_file`, `hourly_weather_file` | `file_name`，非空文件名字符串 |
| `module_manufacturer`, `module_model`, `inverter_manufacturer`, `inverter_model`, `storage_strategy`, `weather_source`, `mppt_string_allocation` | `text`，非空文本 |

## 核对逻辑与返回结果

工具只对 `scope="project"` 下无冲突的数值做全站汇总核算（不同 `scope` 的条目会并列保留，不会混在一起算）：
- **同名冲突检查**：同一 `scope` 下同一 `field` 如果出现了不同数值，直接进 `conflicts`。
- **容量与整串验算**：核对 `module_count × module_power_w` 与 `dc_capacity_kw`（注意 `nominal_capacity_kw` 只作摘要名义容量展示，要核对直流装机量请传 `dc_capacity_kw`）、`inverter_count × inverter_power_kw` 与 `ac_capacity_kw`、`module_count` 除以 `modules_per_string` 是否整除并与 `string_count` 一致。
- **分块汇总验算**：如果传了 `blocks`，核对各分块的组串数、逆变器数累加值是否与全站总数一致。
- **组件与储能复核项**：对比组件 $P_{\max}$ 与 $V_{mp} \times I_{mp}$ 的算术差值，计算储能能量与功率的标称时长比（`storage_energy_kwh / storage_power_kw`），并检查是否缺少 MPPT 组串分配说明或 `.PAN`/`.OND`/气象文件。

返回值包含整理后的 `facts`、`blocks`、`checks`、`conflicts`、`missing`、`review_items` 和下一步操作建议 `next_actions`。存在数值冲突或缺关键项时 `status` 为 `blocked`，无硬冲突时为 `needs_review`。
