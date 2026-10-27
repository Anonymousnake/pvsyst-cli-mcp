# 小时负荷准备与原生绑定

prepare_load_profile准备完整小时数据，create_load_variant将真实缓存绑定到新VC。它们不自动建立储能硬件、混合端口或网充策略；原生自用和调度由PVsyst执行。

## 原生常数、月度与日曲线

`pvsyst_create_native_load_variant`为受限PV-only源创建新VC，不生成外部小时曲线或概率模型。示例：

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","load":{"mode":"constant","constant_kw":75.125},"variant_name":"Constant Load","dry_run":true}
```

| mode | 输入 | 保存 |
|---|---|---|
| constant | constant_kw，正kW | Constant、Flags=$00、ConstantLoad |
| monthly | monthly_energy_kwh，12项月kWh | Monthly、$01、Yearly、ValMonth及3尾零 |
| daily | daily_power_kw，24项小时kW | DailyProfile、$02、NHourlyValues=1、ValHour_1及总和尾槽 |

有限非负数、0.001步长、每项≤1e6；月／日至少一项正值，日尾总和不套每项上界。ValMonth不是十二个kW，即使Energy Units写kW；不外部按月历换算。输入长度、类型、精度、模式和未知键均检查。

要求8.1.6单普通固定朝向1／单子阵、自由地平线、完整电路，无阴影／布局、P50或储能。system仅Grid／Flags=$06000000，main $01900400／$082A，subarray $019E1002，NStrOrient1=NStringCh且其他分配0。源根Flags只能0或0x80000并与负荷存在性一致。

源可无负荷或三种支持的完整flat定义；原字段／15或25槽／尾项先验证。HourlyFile、Probability、HouseHold、未知／嵌套／重复owner不被覆盖删除。只改变根bit19、所属pvLoad、可选标题及明确BOM，源和系统其余字段保持。非ASCII名称沿用add_utf8_bom规则，预览diff超过32000字符披露截断。ready／created的native_consumption_verified=false，全年消费不代表任意年份／模式或GUI全覆盖。

## 准备完整小时CSV

UserData输入为UTF-8、明确日期／负荷两列和紧随单位行，支持逗号或分号，单位仅W/kW及[W]/[kW]。负荷非负，是消费而非净购售电。表头片段如下，实际需全年记录：

```text
Date;P Load
;kW
2024-01-01 00:00;2.5
2024-01-01 01:00;2.5
```

```json
{"csv_name":"source_load_2024.csv","year":2024,"output_name":"load_2024.csv","utc_offset_hours":8,"time_basis":"local_standard_interval_start","date_format":"iso","dry_run":true}
```

year严格整数1969..2068，小时区间起点00:00..23:00，完整8760/8784行、无DST。不补测、不插值、不聚合15分钟。iso为YYYY-MM-DD HH:MM，pvsyst_dot为DD.MM.YYYY HH:MM，pvsyst_slash为DD/MM/YYYY HH:MM；只改日期表示，不转时区。

预览不建目录；执行创建UserHourlyParams中的原生ASCII、CRLF、欧洲点分日期CSV，以及Results的v1导入清单。保留源和失败产物，同名停止。prepared只表示文件完成，不是原生消费。

## 绑定新VC

create_load_variant接收project、source_variant、new_variant、load_manifest_name及dry_run。重新读取当前工作区原CSV和规范化CSV，核对日期、单位转换及逐点负荷。历史绝对路径只是元数据，不按其访问外部文件。

替换所属pvLoad的明确内嵌缓存：HourlyFile、Flags=$4000、UsesModul=Yearly，关闭年归一化和DST。HourlyLoad/ParSimulH的Energy/Accum以MJ及WRFactor序列化，Day_DDMM有符号base62保存每24小时及闰日2902；比例适配三位token范围，不截断大负荷。返回实际编码误差及半整数步理论界，不把编码换算包装成负荷模型。

只创建新VC，原PRJ/VC、输入、系统标志、策略和天气保持。非ASCII负荷路径在新VC增加明确披露BOM。外部CSV改变后需重新绑定；原生日志显示读取成功不证明旧缓存已失效。运行用匹配年份／时基SIT/MET，并核对E_User；SOC保留ratio，不混用百分数。

原生文件匹配可忽略CSV年份，但MCP仍核对请求年及闰日。input-param-file是高级DAT通道，不是负荷CSV导入。其他CLI版本、GUI回存、随机／家庭电器负荷、全部BTR模型、混合DC和网充按各自限制处理，见[储能](grid-storage-parameters.md)。
