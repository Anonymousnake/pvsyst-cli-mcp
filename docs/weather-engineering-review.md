# 气象三层核对与规格书电气包络绑定

本文档说明两个用于设计前/设计后复核的只读工具：
- `pvsyst_review_weather`：把在线下载或本地准备好的气象 CSV、`.SIT`/`.MET` 头部声明与 PVsyst 仿真导出的逐时结果放在一起做三层一致性核对。
- `pvsyst_prepare_sourced_design`：从带原文摘录（`excerpt`）的规格书片段中提取温度系数与极值，组装供 [`pvsyst_simulate_design`](single-design-inputs.md) 使用的 `electrical_design` 包络草案。

## 1. 气象三层核对（`pvsyst_review_weather`）

该工具全程只读，不写文件也不调 CLI，分三层对比气象数据：
1. **数据源到标准 CSV（`provider_to_csv`）**：如果绑定了 `pvsyst_acquire_weather` 下载的 ERA5 清单，会用相同的转换逻辑从原始响应重建标准 CSV 并核对 8760/8784 行辐射与温度是否完全一致。
2. **标准 CSV 到仿真输出（`csv_to_native`）**：逐小时比对标准 CSV 与 PVsyst 仿真结果 CSV 中的 `GlobHor`、`DiffHor`、`T_Amb` 数值及时间标签。
3. **文件头部声明层（`declarations`）**：读取 `.MET` 头部（含内嵌 `SiteMet` 快照、`HLeg` 法定时间标志）与 `.SIT` 根字段，检查经纬度、海拔和时区声明。

### 为什么时基必须用 `LegalTime`（`HLeg=True`）

在对接 PVsyst 的 `.MET` 转换时，有一个非常隐蔽的坑：`.MET` 文件内嵌的布尔标志 `HLeg` 会直接覆盖 `.SIT` 站点里的时基设置。如果导入按当地标准时（`local_standard_interval_start`）打标签的逐时 CSV 时，在 `.MEF` 格式文件里写了 `RefTime=SolarTime`（太阳时），生成的 `.MET` 里 `HLeg=False`，不仅太阳高度角 `HSol`/方位角 `AzSol` 会发生偏移，部分日落边缘的低辐照小时（`5..20 W/m²`）在仿真导出时还会被截成 `0`。

因此，当前的天气准备工具统一在 `.MEF` 中写入 `RefTime=LegalTime`（使 `.MET` 内 `HLeg=True`、`TimeShift=0`）。如果在复用旧清单时发现 `.MET` 还是早期的 `SolarTime` 记录，核对工具会直接拦截并提示用新文件名重新准备 `.SIT`/`.MET`。另外，对于 Open-Meteo ERA5 下载的数据，因为气温使用的是相邻整点瞬时值的算术平均（`endpoint_mean`），核对结果会始终保留一条 `needs_review` 提示。

## 2. 规格书电气参数绑定（`pvsyst_prepare_sourced_design`）

当你想从组件（`.PAN`）、逆变器（`.OND`）规格书和场地温度条件构建 `electrical_design` 包络时，传入 `sources`（资料文本片段）和 `declarations`（每个字段的数值、单位和原文 `excerpt`）：
- 工具会先定位工作区或配置库中的明文 `.PAN` 和 `.OND` 文件，校验文件中的厂家与型号标识。
- 接着逐条检查 `declarations` 中的 `excerpt` 是否确实出现在对应的 `sources[source_id].text` 中，并严格校验数值和单位 token。
- 如果还有必填字段没齐（比如缺了工作电压温度系数 `beta_vmp` 或单路 MPPT 最大输入电流），工具不会胡乱拿默认值去凑，而是返回 `status="blocked"`（或 `needs_context`），附上已组装出的半成品草案和 `missing_fields` 列表，方便你补齐缺项后再调 [`pvsyst_simulate_design`](single-design-inputs.md) 或 [`pvsyst_run_sourced_house_task`](sourced-house-task.md)。

摘录规格书参数时的四条硬规则：
1. 不能拿功率温度系数 $\beta_{P\max}$ 当作工作电压温度系数 $\beta_{Vmp}$。
2. 不能拿逆变器整机直流电流字段 `IDCMax` 当作单路 MPPT 的输入电流上限（也不能简单除以 MPPT 路数）。
3. 组件最低/最高工作温度不等于室外环境温度；设计最低环境温度需要独立依据，不能直接填某一年气象文件里的最低气温。
4. 若用电流温度系数推导最大电流，必须同时给出最大有效辐照度 `max_irradiance_w_m2` 与背面增益 `rear_gain_factor` 的明确出处。
