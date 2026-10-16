# 原生逆变器温度配置

`pvsyst_update_variant_parameters.inverter_temperature_updates` 是独立原生事务，配套 `pvsyst_get_variant_parameters.inverter_temperature` 语义状态与既有 `system_parameters` 原始字段检查。它设置软件自身的逆变器温度入口，不实现温度、降额或功率计算，不修改模块Uc或OND曲线。

```json
{
  "project": "INVERTER_TEMPERATURE.PRJ",
  "variant": "VCE",
  "expected_sha256": "<当前检查返回值>",
  "inverter_temperature_updates": {
    "mode": "fixed",
    "offset_c": 20,
    "fixed_c": 60
  }
}
```

## 模式与保存值

- `ambient` 仅接受mode：移除TempShift/TempFixed，清原生温度模式位，不把两个字段写为0。
- `offset` 必须显式给offset_c与fixed_c：保存两个值，使能原生$4000模式。fixed_c在此模式为未启用但仍保存的值，不推断25°C默认。
- `fixed` 同样要求两个显式值：保存两个值，使能原生$8000模式。上例中的20是保留值，实际固定温度为60°C，不是60+20。

两个值单位均为°C，适配器支持域[-100,100]、0.1°步长、严格有限数值，不接受bool/数字字符串。支持域是本接口的有意边界，不是已逆出的原生GUI范围、厂家额定值或场址极值；边界值只做预览合同验证，未作为物理设计或全年极端气候验证。

before/after均返回mode/offset_c/fixed_c；环境模式两值为null。相同语义请求不写、不备份；预览和恢复复用既有机制。variant_name可同批修改，非ASCII沿用明确UTF-8 BOM规则。其它参数、模式或功能另开事务。

## 原生门控

限定8.1.6 PV-only Grid、单子阵/普通非平均固定方向1、完整电路且全部组串在该方向、已知主阵列Flags/Flags2、自由地平线及无活动近阴影/布局。不混入PF、限值、辅助、负荷、储能或其它系统模式；包括关闭状态保留$06000000的源也先停止，不暗中清其它位。

owning pvSystem字段仅接受Comment/Flags/SystemType/TempShift/TempFixed，拒绝重复、未知字段和嵌套形态。源Flags只接受ambient/offset-only/fixed-only，$C000组合不尝试解读；ambient源携带休眠温度字段、受控源缺一字段都不覆盖。读取的系统模式不等于所有场景均获写入支持，超出范围通过issue披露，Raw system_parameters可另行检查。

原生保存依据及已确认的字段归属见[能量管理分组证据](native-energy-management.md)。原生模式与字段取自GUI保存参考，不依字段存在猜使能，也不采用外部调温/削峰模型替代软件。

## 公开接口与原生验收

GUI/CLI8.1.6.49278；独立INVERTER_TEMPERATURE工程复用440kWp/360kWac参考及Hawaii 2022原天气，不启用PF，试验数值非真实设计。所有原件保留。

- 公开offset(20,25)、fixed(20,60)、ambient恢复的完整VC分别与GUI参考逐字节相同，温度位属于pvSystem而非root Flags；其它电气、气象、模块Uc和逆变器曲线不变。
- 三模式各48小时、16数值列与对应GUI原生输出逐点一致；offset与fixed另各完成GUI/公开配置全年对照，8760行/16列、单位及时间戳完全一致，所有数值有限、小时连续。
- 预览无写入、无操作、备份恢复、35项输入/混合事务/源状态反例均通过；适配器边界只预览，不把它升级为原生硬件允许范围。
- 没有全量测试发现、旧smoke、哈希测试或新增外部热模型。本机模型、天气、原生报告、完整调用及复演资料不随仓库上传。
