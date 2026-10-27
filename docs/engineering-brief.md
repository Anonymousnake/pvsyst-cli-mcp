# 工程简报只读预审

`pvsyst_review_engineering_brief(brief)` 接收调用方从可研或设备清册摘出的结构化事实，在尚无 PRJ、精确 PAN/OND 时也能聚合数量冲突和缺项。它不读取 PDF、引用地址、文件或网络，不初始化 CLI，不查许可，不操作 GUI/INI，不写文件。引用是调用方声明；工具不验证原文真实性，也不选择某一章节为准。

`brief` 禁止额外字段，含：

- `sources[{id,title,locator}]`：引用资料标识与定位说明，最多50项。locator仅作为文字保留。
- `claims[{id,field,scope,value,unit,source_id,pdf_page,excerpt}]`：最多500项，ID唯一；`pdf_page`为从1起算的PDF物理页码；引文最多2000字符。同一字段可保留多个声明。数量是严格整数，其他数值是有限严格数字；不接受布尔值或数字字符串。
- 可选 `blocks[{id,count,strings_per_block?,inverters_per_block?,source_id,pdf_page,excerpt}]`：最多100种块，每种明确数量及每块整数串数／逆变器数；未给的分配标为缺项。

字段和单位严格配对，不自动转换：

| field | unit / value |
|---|---|
| `module_count`, `modules_per_string`, `string_count`, `inverter_count`, `inverter_input_count`, `block_count` | `count`，正整数 |
| `module_power_w` | `W` |
| `module_vmp_v`, `module_imp_a` | 分别 `V`, `A` |
| `inverter_power_kw`, `dc_capacity_kw`, `ac_capacity_kw`, `nominal_capacity_kw`, `storage_power_kw` | `kW` |
| `storage_energy_kwh` | `kWh` |
| `tilt_deg`, `azimuth_deg` | `deg`，分别0…90、−180…180 |
| `clearance_m`, `row_spacing_m` | `m` |
| `pan_file`, `ond_file`, `hourly_weather_file` | `file_name`，非空字符串；不检查文件存在 |
| `module_manufacturer`, `module_model`, `inverter_manufacturer`, `inverter_model`, `storage_strategy`, `weather_source`, `mppt_string_allocation` | `text`，非空字符串 |

全项目算术只用 `scope="project"` 下无冲突的明确输入。不同scope并列保存，不被推断为同一方案。`nominal_capacity_kw`是项目摘要口径，不自动当作实际DC容量；要比较实际组件数量与电量表容量，声明 `dc_capacity_kw`。调用方若把MW转为kW，应在引文／定位中保留原单位和换算依据；不得把kVA变成kW。

审查包括同字段同scope多值、组件数量乘名牌功率、逆变器数量乘额定AC、整串余数、分块汇总和数量对照。Pmax与Vmp×Imp只显示十进制算术差及复核，不设置虚构工程容差。输入路数缺MPPT／并串分配只提示复核，不能推出绝对不可接。储能能量／功率仅为名义小时比；不会生成电池、SOC、调度或可用时长结论。

结果含保留的facts/blocks、各项引用、checks/conflicts/missing/review_items和next_actions。缺真实PAN/OND、逐时天气不会遮掉其他矛盾。`status`仅为`blocked`或`needs_review`；即使无数量冲突且文件名齐全，也永不返回工程ready或认证。`source_quotes_verified`、`simulated`、`native_consumption_verified`、`engineering_certified`始终为false。它是进入现有受支持准备／原生运行路径前的交接审查，不解锁多分块构造、物理拟合或任意储能策略。

例如一个claim可写成：

```json
{
  "id": "project-modules",
  "field": "module_count",
  "scope": "project",
  "value": 218690,
  "unit": "count",
  "source_id": "feasibility",
  "pdf_page": 15,
  "excerpt": "共218690 块"
}
```
