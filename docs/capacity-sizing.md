# 模板容量规划与声明边界

simulate_template的可选capacity对象基于调用方明确包络规划有限整数数量，未传时保留模板数量。它不是温度模型、硬件选型、能量优化或工程认证；原生计算仍由PVsyst完成。

## 源与单位

要求完整单子阵／固定平面Grid、无几何／表／Module Layout，子阵8.1.0/8.1.6独立$0400、无共享$0100、PowerSharingInverterId=-1，全部组串归同一平面。一个独占逆变器组、一个可重复MPPT及一个可重复并联串叶；每台MPPT数量等于目标OND.NbMPPT，全部硬件输入使用。ModeOper=MPPT、CompPMax=Lim、CompVMax=Lim，非异质／优化器字符串。

PAN限单晶／多晶硅、GRef=1000W/m²及TRef=25°C。调用者明确IEC或UL，对应根VMaxIEC/VMaxUL，缺额定停止。PAN功率为W，OND Converter功率为kW，电压／电流V/A。IDCMax=0不是正值限制，也不替代逐MPPT工作／短路额定电流。electrical.readable只说明读取，raw温度系数不猜单位。

## capacity结构

下例仅示意明确声明包络，实际来源须说明温度、辐照、背面增益及方法，不是厂家或场址默认值：

```json
{
  "target_dc_kw":21.6,"max_dc_ac_ratio":1.3,"capacity_tolerance_pct":5,"voltage_standard":"IEC",
  "cold_voc_v":65,"hot_vmp_v":38,"cold_vmp_v":47,
  "module_max_operating_current_a":16,"module_max_short_circuit_current_a":18,
  "mppt_max_operating_current_a":32,"mppt_max_short_circuit_current_a":40,
  "design_min_temperature_c":-10,"design_min_operating_temperature_c":20,"design_max_operating_temperature_c":70,
  "design_basis":"Explicit demonstration envelope; replace with sourced project conditions and method."
}
```

最低环境温度≤最低组件工作温度≤最高组件工作温度，范围-100..150°C。cold_voc对应最低环境温度，cold_vmp／hot_vmp对应组件工作温度，不混用。设计范围含参考温度时包络覆盖参考Vmp/Voc，最大电流覆盖Imp/Isc，短路上界覆盖工作上界。design_basis为单行可打印文本，最多2000字符，不改写GUI项目级设计温度。

只有capacity_tolerance_pct默认5、范围0..20%，其余缺项停止。严格有限数值，不接受布尔、字符串或额外键。预览不写或消耗原生运行。

## 有限规划

每串1..100模块，每MPPT1..100串，逆变器1..10000台，保持每台MPPT数量、模式及拓扑。声明边界分别校核串联热态Vmp≥VMppMin、冷态Vmp≤VMPPMax、冷态Voc≤逆变器与所选模块标准额定电压的较小值，并联最大工作／短路电流不超过逐MPPT声明，DC:AC不超过显式上限。

按绝对容量偏差、较少逆变器、接近原串联数择优；1e-9百分点仅处理排序／容量比较数值误差，不是电压电流隐含余量。capacity_plan返回实际数量／容量、有符号偏差、AC与DC:AC、声明边界、搜索域及未核实项。无整数解时停止，不扩范围或改拓扑。

改变数量需既有显式NElements，默认1只在不变时保留，不修补或规范化模板。电路、损失、参考提示及原生硬件模型不重新拟合。

## 执行与恢复

容量阶段复用既有逆变器数、串数和串联数分步事务，每步有当前令牌、备份和逆序恢复；最终数量等于计划，年度PR用实际新组件数。源、组件及旧输出保持。失败保留已提交步骤和原生原因，不改年或自动撤销现场。

线缆、保护、连接器、BMS、其他厂家限制、真实气候覆盖、包络来源及损失重拟合仍属unverified_checks。配置可保留检查或逆序恢复，历史报告不随恢复改变。参考[原生全年任务](automatic-simulation.md)及[官方电压选配说明](https://www.pvsyst.com/help/project-design/grid-connected-system-definition/array-voltage-sizing-according-to-inverter.html)。
