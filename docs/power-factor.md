# 原生功率因数与逆变器额定限制

`pvsyst_update_variant_parameters.power_factor_updates`设置原生PF及同组PNom选项，`pvsyst_get_variant_parameters.power_factor`读取语义状态。它保存带符号TanPhi/TanPhiM，不计算Cosφ、无功、额定功率或发电。PF与注入点限值的组合使用独立[功率控制](native-power-controls.md)事务。

## 输入

mode和pnom_limit必须明确提供，可同批设置variant_name。

| mode | 数值输入 | pnom_limit |
|---|---|---|
| none | 无，移除PF字段 | native |
| constant | tan_phi | native / active / apparent |
| monthly | monthly_tan_phi，一月至十二月12项 | native / active / apparent |

数值是无量纲正切，不是Cosφ。适配器范围[-1,1]、0.001增量，须有限且不是布尔或数字字符串；不是GUI或厂家极限。领先保存负值、滞后保存正值，月值按声明保留，不由符号推导无功曲线。

pnom_limit=native清除Legacy强制位，交OND和原生模式决定；active/apparent改变逆变器PNom的Legacy解释，不是注入点kW/kVA限值，也不修改OND硬件或厂家PF限制。

```json
{
  "power_factor_updates": {"mode": "constant", "pnom_limit": "native", "tan_phi": -0.484},
  "variant_name": "PF_LEADING"
}
```

## 支持范围与保存

仅支持8.1.6 PV-only Grid、单普通固定平面／子阵、自由地平线、完整电路及受支持主阵列标志。活动几何／布局、负荷／储能、逆变器温度、辅助、限值及其他系统字段或位停止。重复或非所属PF字段、位与字段冲突、两个PNom强制位并存也停止；其他功能不混入独立PF事务。

| 保存模式示例 | pvSystem.Flags |
|---|---|
| 无PF | $00 |
| 常数PF | $00100000 |
| 月度PF | $00300000 |
| 常数＋强制有功PNom | $00900000 |
| 常数＋强制视在PNom | $00500000 |
| 月度＋强制有功PNom | $00B00000 |
| 月度＋强制视在PNom | $00700000 |

组合入口中月度PNom加有功注入点、单列损失的族为$05B00000/$05700000。不放开未列出的monthly＋PNom＋视在注入限值；无PF仍要求native。无限值组合明确使用quantity=active、point=inverter，不无条件添加注入点位。

读取和写入使用相同组合规则；候选读回语义须匹配请求。getter返回mode/tan_phi/monthly_tan_phi/pnom_limit，不支持状态返回mode=null及issue。预览、同值无操作、备份和恢复沿用变体事务。

## 结果解释

EReGrid/EApGrid保留原生kVAh表头，不自行改为kvarh或补符号；负TanPhi不保证产生负的无功导出。InvPmax不是每个小时E_Grid的严格上界，native与apparent也不保证对所有OND都产生相同结果。

P50是独立结果分析功能，使用[受限P50编辑器](p50-native-parameters.md)；不通过PF配置创建概率专页。
