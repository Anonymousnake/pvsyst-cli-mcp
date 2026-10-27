# 站点、天气与指定年份准备

`pvsyst_prepare_weather`把工作区标准小时CSV转换为新SIT/MET，并生成可供年度任务绑定的来源清单。它不下载气候数据、不修补观测、不解码MET逐时载荷；电气和场址设计依据仍由调用者明确提供。

## 输入

仅接受GHI、DHI、Tamb：辐照W/m²或每小时MJ/m²，温度deg.C/degC。日期列可重排，Year可来自#Year。year为1969..2068，全部00:00..23:00标签及闰日完整，文件名不证明覆盖。

必须声明time_basis=local_standard_interval_start，即固定当地冬季标准时、无DST、每标签为区间起点。位置、海拔、时区、站点、国家标签和source来自CSV；另显式提供大写ISO alpha-2 country_code，避免建站时自动外查。UTC、显式时间参考、非零偏移、缺测、POA和其他列映射停止，不移时、补值或丢列。

## 调用与新文件

以下为已连接MCP客户端中的工具参数示例；先预览，检查结果后再以同一参数设置dry_run=false：

```json
{"csv_name":"station_2024.csv","year":2024,"sit_name":"station_2024.SIT","met_name":"station_2024.MET","country_code":"US","time_basis":"local_standard_interval_start","dry_run":true}
```

CSV序列化为独立日期列和统一单位，再按明确列号生成MEF；SIT走原生create-site。执行创建导入CSV、月值CSV、MEF、SIT、MET及Results来源JSON，同名停止。源CSV保持，执行前后验证指纹。生成SIT按实际打印精度复核位置、时区、国家和月值：月辐照±0.051kWh/m²、月温度±0.0051°C，不将其当物理容差。

## 绑定年度任务

将响应中的artifacts.manifest文件名传给simulate_template的weather_manifest_name；同时明确模板、工作工程、实际PAN/OND、SIT/MET及year。不要猜清单文件名或把准备完成当作全年完成。

预检重验清单中的源CSV、导入CSV、月值、MEF、SIT、MET与请求年份／名称，变化在写项目之前停止。绑定的固定当地标准时profile重新核对MET时基，旧SolarTime需用新名重建，不改写旧资产。

原生运行显式加载SIT/MET，不只改历史来源名称。SFI导出GlobHor、DiffHor、T_Amb与CSV逐时比对，年度完整性另检。0.1W/m²和0.01°C只是公开的舍入／转换复核阈值，不代表MET存储精度或物理认证；标签／数量不匹配属于失败，数值超阈保留review。

## 状态与恢复

blocked／ready为只读预检；prepared／needs_review表示新文件已生成，native_met_verified仍false。转换质量／时间警告保留并传播到年度任务，不压制或自动相移。failed保留已生成文件和阶段清单，审查后用新输出名重试，不删除／修补源输入。

年度任务沿用completed／needs_review／failed与逆序恢复；文件存在、转换退出成功或可读取声明都不认证气候质量。UTC/DST／区间结束转换、额外变量映射和有效太阳位置仍非这个准备入口的保证。

格式依据：[标准天气格式](https://www.pvsyst.com/help/meteo-database/import-meteo-data/pvsyst-standard-format.html)、[自定义导入](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/procedure.html)、[时间标签](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/time-shift.html)。
