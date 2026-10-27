# 原生辅助功率

`pvsyst_update_variant_parameters.auxiliary_updates`保存原生辅助功率参数，运行条件、损耗和并网能量由PVsyst计算。可同批改显示名，其他参数另开事务。profile的两个受限阈值／比例族见[详细损失](native-detailed-losses.md)，不由此支持任意混合模式。

```json
{"mode":"constant","operating_kw":5,"night_kw":2}
```

## 常数模式

operating_kw是整个系统在原生逆变器运行条件下的功率，不是按时钟定义的白天曲线；constant要求输出阈值为零。night_kw是原生待机／夜间辅助功率。两值须明确给出、非负且为0.01kW步长。0/0保留使能，mode=none关闭并移除字段。读取的auxiliaries返回模式及两项kW值。

仅支持8.1.6 PV-only Grid、单固定平面／子阵、无活动阴影或布局、受支持main标志。PF、负荷／储能、限值组合及未知字段在写入前停止。预览、同值无操作、备份、恢复和并发保护沿用变体事务。

| 原生绑定 | 含义 |
|---|---|
| pvSystem.Flags bit18 | 辅助功率使能，$00040000；关闭时清除 |
| AuxPower_Const | 保存单位W，接口operating_kw为kW |
| AuxPower_Night | 保存单位W，接口night_kw为kW |
| AuxPower_Pout | 比例字段，constant不使用；profile另有约束 |

使能0/0时可只保存使能位，不保留零值字段；关闭时移除辅助字段。原生低输出时段可能从电网补给辅助消耗，E_Grid负值不裁零。没有通用辅助损耗公式，不将E_Grid与EOutInv差值包装成独立损耗计算。

## AC与限值位置

AC线缆涉及主阵列摘要、子阵及直接逆变器CableDefinition，不是单一ACWireLength标量。配置使用[原生AC线缆](native-ac-cabling.md)入口，不通过辅助功率事务修改。

存在下游AC损耗时，注入点限值与逆变器端限值可有不同结果，单场景或短时观测不证明全年等价，也不扩大活动AC下的限值编辑域。
