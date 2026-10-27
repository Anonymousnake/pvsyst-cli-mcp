# 原生逆变器温度配置

`pvsyst_update_variant_parameters.inverter_temperature_updates`是独立事务，配套读取状态为`pvsyst_get_variant_parameters.inverter_temperature`，原始字段位于system_parameters。它设置原生温度入口，不计算温度、降额或功率，不修改模块Uc及OND曲线。

```json
{
  "project": "INVERTER_TEMPERATURE.PRJ",
  "variant": "VC1",
  "expected_sha256": "<当前检查返回值>",
  "inverter_temperature_updates": {"mode": "fixed", "offset_c": 20, "fixed_c": 60}
}
```

实际调用使用当前读取返回的并发令牌。

## 模式与数值

- ambient仅接受mode，移除TempShift/TempFixed并清温度模式位，不将字段写为0。
- offset须显式给offset_c及fixed_c，保存两值并使能$4000。fixed_c是未启用但保留的值，不推断默认温度。
- fixed也须给两个值，保存并使能$8000。示例实际固定温度为60°C，不是60+20。

两值单位为°C，范围[-100,100]、0.1°步长、严格有限数，不接受布尔或数字字符串。这是适配器范围，不是GUI范围、厂家额定值或场址极值。

before/after返回mode/offset_c/fixed_c，ambient两值为null。相同语义请求不写或备份；预览、恢复和同批variant_name沿用现有事务及明确UTF-8 BOM规则，其他参数另开事务。

## 支持范围

仅支持8.1.6 PV-only Grid、单子阵／普通非平均固定朝向1、完整电路及全部组串归属该方向、受支持main Flags/Flags2、自由地平线，无活动近阴影或布局。不混入PF、限值、辅助、负荷、储能及其他系统模式；关闭状态仍保留$06000000的源也停止，不暗中清位。

pvSystem直属字段仅接受Comment/Flags/SystemType/TempShift/TempFixed。重复、未知或嵌套字段停止。源标志仅接受ambient、offset-only、fixed-only；$C000组合不解释。ambient携带休眠温度字段、受控源缺字段均停止。

读取到系统模式不表示该状态可编辑；超范围问题通过issue披露，可另查原始system_parameters。字段归属和原生模式见[能量管理](native-energy-management.md)，不得用外部调温或削峰模型替代PVsyst。
