# 原生PF与功率限制组合

`pvsyst_update_variant_parameters.power_controls_updates`在独立事务内配置PF与网侧限值，可附带variant_name；读取使用`pvsyst_get_variant_parameters.power_controls`。独立power_factor_updates和grid_limit_updates仍执行各自窄范围；组合状态以power_controls结果为准。

适配器复用原生字段读写，完整校验pvSystem所属字段和标志，再一次替换系统块。解析副本不交给仿真器，不计算Cos/tan、无功、kVA降额或限电能量。

```json
{
  "power_controls_updates": {
    "power_factor": {"mode": "constant", "pnom_limit": "native", "tan_phi": 0.484},
    "grid_limit": {"mode": "constant", "quantity": "apparent", "point": "injection", "limit": 250, "separate_loss": true}
  }
}
```

## 输入与组合

power_factor使用none/constant/monthly、明确pnom_limit和常数或12项月度正切。正值滞后、负值领先，范围[-1,1]、0.001增量。月度PNom范围见[功率因数](power-factor.md)。

grid_limit.quantity为active/apparent，limit直接表示kW/kVA，须为正且满足0.1原生单位增量，不通过PF换算。point为injection/inverter，启用时须明确separate_loss布尔。关闭形状例如mode=none、quantity=apparent、point=injection，不带limit/separate_loss；类型与位置位保留，不将所有Flags清零。

只接受受支持标志组合，不按位域任意组合。完全空白基线可明确请求PF none/native与grid none/active/inverter。限制启用时的额外约束：

- 月度PF或Legacy PNom与限值组合要求注入点及单列损失；不支持monthly＋PNom＋视在注入限值的未列族。
- 逆变器端要求常数PF、native PNom及单列损失。
- 不单列损失要求常数PF、native PNom及注入点。
- NoPF启用限值要求有功、注入点及单列损失。

## 支持范围与事务

仅接受8.1.6 PV-only、单子阵／普通固定平面、自由地平线、无活动几何或布局、完整电路计数。温度、P50、辅助、负荷、储能和同次其他物理更新停止。未知／重复／非所属字段、模式冲突或缺失字段不被清理掩盖。

预览、同值无操作、源冲突保护、备份和恢复沿用原事务。活动AC、变压器与新PF的任意耦合不由这个入口支持。

## 原生结果解释

EReGrid/EApGrid保留kVAh，PGridLim在视在模式也可能保留kW表头；类型以请求、保存字段及原生报告判定，不补无功符号或改单位。LimGrSeparLoss影响损失分类，不自行解释为额外能量损失。

存在下游AC损耗时，两种限值位置可能产生不同结果；没有AC损耗的等值输出不证明通用等价。原生结果及工程设计依据仍须单独审查。
