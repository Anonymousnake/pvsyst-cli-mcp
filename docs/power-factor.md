# 原生功率因数与逆变器额定限制

`pvsyst_update_variant_parameters.power_factor_updates` 配置原生 PF 模式及同组 PNom 选项；`pvsyst_get_variant_parameters.power_factor` 读回语义状态。只序列化软件实际保存的带符号 `TanPhi/TanPhiM`，不实现 Cosφ 换算、无功、额定功率或发电模型。
后续的独立 `power_controls_updates` 事务已覆盖限定PF/有功/视在限值组合，见[原生功率控制](native-power-controls.md)。本页独立PF接口的旧域保持不变，不因新组合入口放宽。


## 参数与范围

必须显式提供 `mode` 和 `pnom_limit`，可在同一事务附带 `variant_name`。

| mode | 数值输入 | pnom_limit |
|---|---|---|
| none | 无；移除两个 PF 字段 | native |
| constant | tan_phi | native / active / apparent |
| monthly | monthly_tan_phi，按一月至十二月的12项 | native |

值是无量纲原生正切，不是 Cosφ。本适配器域为 [-1,1]、0.001 增量，严格有限数值且不接受布尔或数值字符串；这是支持范围，不是 GUI 或厂家边界。原生 GUI 的领先保存负值、滞后保存正值，原生报告已确认。允许 signed 月值原样输入，不从符号推导外部无功曲线。

`pnom_limit=native` 清除旧版强制位，交由原有 OND/原生模式决定。`active/apparent` 是逆变器 PNom 的旧版强制解释，不是并网注入点的 kW/kVA 限值；GUI 本身将其列为 Legacy parameters。月度与旧版强制模式的组合尚未验证，入口暂不接受。它不调整 OND 硬件数据或厂家功率因数限值。

```json
{
  "power_factor_updates": {
    "mode": "constant",
    "pnom_limit": "native",
    "tan_phi": -0.484
  },
  "variant_name": "PF_LEADING"
}
```

限定 8.1.6、PV-only Grid、一个普通固定平面/子阵、自由地平线、完整电路计数及已验证主阵列标志。活动几何/布局、负荷/储能、逆变器温度、辅助功率、并网限值及其它系统字段或位保持阻断。重复/非 owning PF 字段、位与字段冲突、两个 PNom 强制位同时开启等也在写入前阻断。其它功能事务不与 PF 混用；既有 NoPF 限电写入范围不扩大。

getter 成功返回 `mode/tan_phi/monthly_tan_phi/pnom_limit`；不支持的系统返回 `mode=null` 和 `issue`，与既有系统状态检查器一致。预览、无操作、备份及恢复沿用原有变体事务。

## 原生证据

GUI/CLI 8.1.6.49278；派生 `ENERGY_CONTROLS`、`POWER_FACTOR`，原有440 kWp/6台 Generic 60 kW 夹具、Hawaii 天气，2022年8760小时。这里的 PF、月值及场景不是工程建议。

| GUI 状态 | owning pvSystem.Flags | 原生保存字段 | E_Grid观测汇总 kWh |
|---|---|---|---:|
| 无 PF | $00 | 无 | 799980.9366 |
| 常数滞后 | $00100000 | TanPhi=0.484 | 778505.9826 |
| 常数领先 | $00100000 | TanPhi=-0.484 | 778505.9826 |
| 月度滞后 | $00300000 | TanPhiM=0.484 至0.311的12项 | 785228.2026 |
| 月度领先 | $00300000 | 同12项的负值 | 785228.2026 |
| 常数+强制有功PNom | $00900000 | TanPhi=0.484 | 799980.9366 |
| 常数+强制视在PNom | $00500000 | TanPhi=0.484 | 778505.9826 |

七次原生全年均有7页PDF、严格连续8760行及有限数值；基线16列，其余18列。月度报告逐月显示 GUI 输入的 Cosφ 0.900、0.905、…、0.955，领先/滞后有明确文字，且 InvPmax 随月变化。两种 Legacy 报告分别明确 `PNom limit forced to active power` / `PNom limit forced to apparent power`。

七个公共接口生成状态的完整文件逐字节匹配 GUI 参考，含 PF 清除；各自预览不落盘、再次调用为无操作。月度滞后与强制有功两种接口生成状态另跑全年，与 GUI 来源的8760行/18数值列、表头和单位完全相同；快照恢复逐字节一致。42个公共反例覆盖输入、来源、计数及事务混用，均在写入前拦截且保留文件。

本夹具中，正负同幅 PF 的全部共同小时列相同；`EReGrid/EApGrid` 表头仍为原生 `kVAh`，负 TanPhi 的 EReGrid 也仍为正值。这证明原生导出行为，不表示取得带符号无功或所有 OND 都具有同样反应。不重标单位、不自行补符号、不把 InvPmax 当成每个小时 E_Grid 的严格上界。

本机证据为 `energy-native-batch-r22.json`、`power-factor-acceptance-r22-r2.json`、原生 CSV/PDF；商业输入与本机产物不随仓库分发。初次验收因布局反例错误使用通用 PVObject_ 标签而暂停，原失败记录保留；更正为原生 PVObject_ModuleLayout 标签后续跑，未修改生产门控来使反例通过。未执行哈希测试、旧 smoke 或全量测试发现。

## P50 独立待办

同一窗口未仿真时另存的六个 P50 VC 实质只有标题变化，原反例与截图保留，不计配置成功。后续已在原生全年结果态取得十四字段及概率报告，并交付[已有结果模板的受限编辑器](p50-native-parameters.md)。勾选部分参数会恢复原生默认值，而非简单纳入/排除；不提供替代概率算法。无结果输入仍缺概率专页构造入口，不因已有模板可编辑而宣称补齐。GUI 已正常退出，原 DataPath 已恢复。
