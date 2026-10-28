# P50 / P90 概率评估参数编辑

在 PVsyst 中，P50/P90 年发电量不确定度评估参数保存在变体文件（`.VCx`）根部的 `P50_P90=TP50_P90` 块中。`pvsyst_get_variant_parameters.p50_p90` 用于读取这 14 个字段，`pvsyst_update_variant_parameters(..., p50_p90_updates=...)` 用于在已有 P50 结果模板上修改它们并生成带概率分布页的年度报告。

关于 PVsyst 中各项方差合成与分位数计算的统计定义，可参考官方文档 [P50–P90 evaluations](https://www.pvsyst.com/help/project-design/p50-p90-evaluations.html)。

## 入参格式（14 个必填字段）

调用 `pvsyst_update_variant_parameters` 时，`p50_p90_updates` 必须完整提供以下 14 个原生键名（可同批附带 `variant_name`）：

```json
{
  "MeteoKind": "SpecYear",
  "P90Custom": 85,
  "PxxCustom": 97,
  "PxxReport": true,
  "PxxMainResults": true,
  "PlotKind": 1,
  "YearDevFromAver": 2.3,
  "MeteoAnnualVariab": 4.2,
  "PVModelVariab": 2.11,
  "InverterVariab": 0.62,
  "SoilingVariab": 1.23,
  "DegradVariab": 1.34,
  "CustomVariab": 0.45,
  "CustomVarLabel": "Declared uncertainty"
}
```

各字段取值规则：
- `MeteoKind`：支持 `"SpecYear"`（特定年份气象）或 `"TMY"`（典型气象年）。注意选 `"TMY"` 时，PVsyst 不将 `YearDevFromAver` 计入偏差，但文件里仍需保留该字段的保存值。
- `P90Custom` 与 `PxxCustom`：两个自定义分位点（整数），必须满足 `51 <= P90Custom < PxxCustom <= 99`（尽管字段名叫 `P90Custom`，它可以设成 85、90 等任意满足顺序的整数）。
- `YearDevFromAver`：特定年份相对于多年均值的偏差百分比，范围 `[-100, 100]`，步长 `0.001`。
- 六个方差/不确定度百分比字段（`MeteoAnnualVariab`、`PVModelVariab`、`InverterVariab`、`SoilingVariab`、`DegradVariab`、`CustomVariab`）：范围 `[0, 100]`，步长 `0.001`。
- `PxxReport` 与 `PxxMainResults`：布尔值，分别控制是否在 PDF 报告中输出独立的 P50/P90 章节以及是否在摘要页展示分位结果。
- `PlotKind`：固定传整数 `1`（概率密度分布图）。
- `CustomVarLabel`：自定义不确定度项的说明文字，`1..80` 字节的单行可打印 ASCII 字符串，首尾无空格。

## 为什么要求源变体是“已保存结果模板”（`Flags=$01800001`）

在 PVsyst 8.1.6 里，如果一个 `.VCx` 变体还没有在 GUI 里跑过仿真并勾选保存 P50/P90（即根部 `pvVCalcul.Flags` 不是 `$01800001` 且没有现成的 `P50_P90=TP50_P90` 块），单纯往普通输入变体里硬塞一个 `TP50_P90` 文本块，跑 CLI 时并不会产出 P50/P90 报告页。

因此，`pvsyst_update_variant_parameters` 的 `p50_p90_updates` 只接受根级 `Flags=$01800001` 且已带有完整 14 字段 `TP50_P90` 块的单子阵列固定平面并网模板（无 3D 阴影、无地平线、未混开 PF/限发/温度/储能）。修改这些统计参数会改变 PDF 报告中的 P50/P90 分位评估页正文，而不会改变逐时 CSV 里的 8760 小时发电量列。
