# 站点与标准逐时气象转换（`pvsyst_prepare_weather`）

`pvsyst_prepare_weather` 将工作区 `Meteo` 下的一份 PVsyst 标准逐时气象 CSV 一步转换为新的 `.SIT`（站点）和 `.MET`（二进制/编码气象）文件，并在 `Results` 下生成一份来源清单 JSON，供后续 [`pvsyst_simulate_template`](automatic-simulation.md) 绑定和逐时核对使用。

如果需要先从 Open-Meteo ERA5 在线下载某年的标准逐时 CSV，请先调用 `pvsyst_acquire_weather`（或直接用一站式入口 [`pvsyst_simulate_design`](single-design-inputs.md)）。

## 输入要求与调用示例

源 CSV 需先满足 [`pvsyst_inspect_weather_csv`](weather-input-verification.md) 的标准整点校验，且：
- 仅包含水平总辐射 `GHI`、散射辐射 `DHI`（单位 `W/m²` 或 `MJ/m²`）和环境温度 `Tamb`（单位 `deg.C` / `degC`）。
- 完整覆盖目标年份（`1969..2068`）的 8760 或 8784 个整点（含闰年 2 月 29 日）。
- 显式传入 `time_basis="local_standard_interval_start"`（即固定当地冬季标准时、无夏令时跳变、每个时间戳代表该小时区间的起点）以及两位大写 ISO 国家代码 `country_code`（如 `"CN"`、`"US"`）。

先用 `dry_run=true` 预览，确认无报错后再设 `dry_run=false` 执行：

```json
{
  "csv_name": "station_2024.csv",
  "year": 2024,
  "sit_name": "station_2024.SIT",
  "met_name": "station_2024.MET",
  "country_code": "US",
  "time_basis": "local_standard_interval_start",
  "dry_run": true
}
```

## 底层生成的文件与精度校验

当 `dry_run=false` 时，工具会按顺序生成以下新文件（若任一同名文件已存在则报错停止，绝不覆盖源 CSV 或旧文件）：
1. 拆分好独立日期列、统一好单位的规范化导入 CSV 与月度汇总 CSV（位于 `Meteo`）。
2. 调用 `PVsystCLI.exe create-site` 生成 `.SIT` 站点文件（位于 `Sites`），并按 PVsyst 的打印精度回读校验经纬度（`±0.0000501°`）、海拔（`±0.500001 m`）、时区、国家及 12 个月辐射/温度均值（月辐射 `±0.051 kWh/m²`、月均温 `±0.0051 °C`）。
3. 生成绑定 `RefTime=LegalTime` 的 `.MEF` 转换格式文件（位于 `Meteo`），再调用 `PVsystCLI.exe convert-meteo` 生成 `.MET` 文件（确保 `.MET` 内部 `HLeg=True`）。
4. 在 `Results` 下写入来源清单 JSON（路径见返回值的 `artifacts.manifest`）。

## 与全年仿真任务联动

将返回的 `artifacts.manifest` 文件名传给 `pvsyst_simulate_template` 的 `weather_manifest_name` 参数：
- 仿真启动前会自动重验源 CSV、导入 CSV、月值 CSV、`.MEF`、`.SIT`、`.MET` 的哈希指纹及 `LegalTime` 时基。
- 仿真结束后会自动把导出 CSV 里的 `GlobHor`、`DiffHor`、`T_Amb` 与准备阶段的 CSV 逐时比对（辐射差超过 `0.1 W/m²` 或温度差超过 `0.01 °C` 时会保留 `needs_review` 提示）。

关于 PVsyst 标准气象格式与时间偏移的官方说明，可参考：[PVsyst standard format](https://www.pvsyst.com/help/meteo-database/import-meteo-data/pvsyst-standard-format.html)、[Custom meteo import procedure](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/procedure.html) 与 [Time shift in meteo files](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/time-shift.html)。
