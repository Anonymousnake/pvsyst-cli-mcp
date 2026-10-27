# 原生系统字段与输出单位

`pvsyst_get_variant_parameters.system_parameters`返回pvSystem所属的SystemType、Flags、TempShift、TempFixed、TanPhi、PLimGrid、LimGrSeparLoss原始字符串。唯一字段才返回，缺失／重复时省略，system不存在时为空；不补默认或计算PF、kW/kVA。

温度配置、独立PF及组合限值分别使用[逆变器温度](inverter-temperature.md)、[功率因数](power-factor.md)、[功率控制](native-power-controls.md)。只读暴露原始字段不扩大写入支持范围。

## 保存模式示例

下列字段属于PVObject_System=pvSystem，不应与root或其他对象同名字段混用。数值用于说明保存格式，不是设计建议。

| 原生状态 | 系统Flags | 字段示例 |
|---|---|---|
| 环境模式 | $00 | 无温度／PF／限值字段 |
| PF／限值关闭，保留位置／类型位 | $06000000 | 无温度／PF／限值字段 |
| 环境温度偏移 | $4000 | TempShift=20.0、TempFixed=25.0 |
| 固定温度 | $8000 | TempShift=20.0、TempFixed=60.0 |
| 常数滞后PF | $00100000 | TanPhi=0.484 |
| PF＋注入点有功限值 | $05100000 | TanPhi=0.484、PLimGrid=250.0、LimGrSeparLoss=True |
| PF＋注入点视在限值 | $07100000 | 相同标量，bit25不同 |

温度字段可同时存在，模式位决定实际使用；环境模式移除两字段。字段存在不等于使能。PF保存的是TanPhi，不直接保存Cosφ，适配器不拟合或重算转换关系。

## 输出与解释

原生SFI通道viInvTemp、viInvPmax、viEReGrid、viEApGrid对应温度、功率和能量输出。viPhiAng/viPhiInFr的含义未确认，不作为PF通道使用。

InvTemp单位为°C，InvPmax及E_Grid为kW；EReGrid/EApGrid保留原生kVAh表头，不自行改为kvarh、补符号或推导额外统计。视在限值下，PGridLim的kW列头不能单独证明有功上限，须结合请求的quantity和原生报告。InvPmax不自动等于每个小时E_Grid的严格上界。

原生字段与模式不能外推到所有OND或拓扑。活动AC与PF的任意耦合、从无结果工程构造P50专页仍不由这些读取和配置入口支持。P50的受限结果模板见[概率参数](p50-native-parameters.md)；储能策略和DC端口使用各自入口，不把名称线索或输出指标当配置能力。
