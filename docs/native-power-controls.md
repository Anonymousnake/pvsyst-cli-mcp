# 原生 PF 与功率限制组合

`power_controls_updates` 在一个独立事务中配置 PF 与网侧功率限制，可附带 `variant_name`；`pvsyst_get_variant_parameters.power_controls` 返回组合状态。两个旧接口 `power_factor_updates`、`grid_limit_updates` 的支持域、互斥和序列化实现保持不变；组合态下旧只读字段可能返回待审，组合字段才是此入口的检查结果。

适配器复用原有 PF、限值 reader/serializer：先在受限解析副本中隔离其各自 owning 字段与标志，再校验完整原始组合并一次替换真实 `pvSystem`。解析副本不交给仿真器、不改真实文件；最终完整文件交由原生 PVsyst 计算。没有 Cos/tan 换算、kVA 降额、无功、限电或其它独立物理模型。

## 公共输入

```json
{
  "power_controls_updates": {
    "power_factor": {
      "mode": "constant",
      "pnom_limit": "native",
      "tan_phi": 0.484
    },
    "grid_limit": {
      "mode": "constant",
      "quantity": "apparent",
      "point": "injection",
      "limit": 250,
      "separate_loss": true
    }
  }
}
```

- `power_factor` 沿用原生正切接口：none/constant/monthly、显式 `pnom_limit`、常数或12项月度值；正值滞后、负值领先，[-1,1]/0.001为适配器域。月度+Legacy PNom仍阻断。
- `grid_limit.quantity` 是 `active` / `apparent`；`limit` 原样表示 kW / kVA，正数、0.1原生单位增量。没有以功率因数转换两者的代码。
- `point` 是 `injection` / `inverter`；启用限制必须显式给 `separate_loss` 布尔。不使能时形状为 `{ "mode":"none", "quantity":"apparent", "point":"injection" }`，不带 limit/separate_loss。禁用值保留明确的类型/位置位，而不是把所有 Flags 擦成零。
- 未使能位的组合也按已验原生 flag 族约束，不接受任意笛卡尔积。若要完全恢复空白基线，可显式请求 PF none/native 与 grid none/active/inverter；另有 GUI 已验 none/apparent/injection 控制。

仅接受8.1.6、PV-only、单子阵/单普通固定平面、自由地平线、无活动几何或布局、完整组串/逆变器计数。温度、P50、辅助、负荷、储能等组合和同次其它物理编辑均在写入前停止；未知字段/位、非 owning/重复字段、缺失或与模式冲突的字段不被清理掩盖。生产冲突保护、预览、无操作、备份和精确恢复复用现有机制。

已验族还做耦合约束：月度或 Legacy PNom 与启用限制搭配时，限注入点+单独损失；逆变器端点限常数 PF/native PNom/单独损失；不单独分类损失限常数 PF/native PNom/注入点；NoPF启用限制限有功/注入点/单独损失。其它族继续待审，不因位域互不重叠而放宽。

## 原生批次与消费

GUI/CLI8.1.6.49278；保持原440 kWp / 6台 Generic 60 kW / Hawaii气象夹具。父代理一次完整窗口采集后，在同一批次保存19态：常数正负/月度正负 × 有功/视在，两个端点，两种损失分类，四种Legacy+限制，以及关闭PF/限制/两者。全部状态在系统块及标题之外逐字节相同；GUI正常退出，原 UTF-16 INI DataPath恢复。

19态全部在原生 CLI 跑2022全年8760行，原生 CSV 有限且小时连续，并产生PDF。月度8页，其余7页；关闭限制保留PF时18数值列，关闭PF保留限制时19列，全部关闭16列；启用PF/限制且单独分类21列，不单独分类20列。列数变化也是原生损失分类消费证据。

| 原生状态 | E_Grid 最大 kW | 原生小时观测汇总 kWh |
|---|---:|---:|
| 常数±0.484，有功250，注入点 | 250.00 | 693021.4026 |
| 常数±0.484，视在250，注入点 | 225.03 | 650958.2226 |
| 月度正负剖面，有功250 | 250.00 | 693131.0026 |
| 月度正负剖面，视在250 | 238.72 | 663231.7626 |
| 强制有功PNom，有功250 | 250.00 | 693356.8866 |
| 强制有功PNom，视在250 | 225.03 | 651106.3066 |
| 关闭限制，保留常数PF与未使能类型/位置位 | 324.23 | 778505.9826 |
| 关闭PF，保留有功250 | 250.00 | 693356.8866 |
| 两者关闭，Flags仍保留06000000 | 352.11 | 799980.9366 |

当前无下游AC损耗夹具的两端点电量相同，不能据此宣称一般端点效应已验证；此前非零AC的NoPF位置对照仅24h，见[独立辅助与AC证据](native-auxiliary-power.md)。活动AC、变压器与新PF耦合未在此接口放宽。`LimGrSeparLoss=False` 本夹具只改变分类列数，未改变电量，不包装成虚构的额外能量损耗。

原生 CSV 的 `EReGrid` / `EApGrid` 均仍写 kVAh；`PGridLim` 的表头在视在模式也仍写 kW。单位原样披露，实际限制类型以保存字段及原生报告为依据；负TanPhi未得到负号无功列，不在适配器补符号。

## 接口验收

临时候选检查19个完整文件/19无操作/38正反模式转换匹配GUI。公共SDK对19态逐一做完整字节匹配、预览、不变性和无操作，再执行38次正反转换；57个来源异常、无效输入与混用事务反例均检查原文件不变。公共原生全年抽样覆盖视在、月度正负、逆变器端、不单独损失、强制有功PNom及关闭控制，与GUI参考逐列/单位相同；PDF全页文本只归一化工程名与运行时间后相同。备份精确恢复已验。

本机证据：`power-control-save-batch-r25.json`、`power-control-native-r25.json`、`power-control-candidate-check-r25.json`、`power-controls-public-r25.json`。不分发商业输入/原始气象，不执行哈希测试、旧smoke或全量测试发现，不新增永久测试框架。早期候选的计数、视图边界、bit25、before形状与布尔落盘缺陷在接入前集中修复，不作为原生通过项。
