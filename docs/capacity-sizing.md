# 模板整数容量规划（Capacity Sizing）

在调用 `pvsyst_simulate_template`、`pvsyst_simulate_design` 或 `pvsyst_prepare_design_project` 时，如果不想手动算“每串多少块组件、每路 MPPT 并几串、一共用几台逆变器”，可以传入 `capacity` 对象。工具会根据你给定的目标直流容量（`target_dc_kw`）和电压/电流/容配比边界，在模板允许的拓扑结构内自动搜索最优的整数串并联组合。

如果不传 `capacity`，模板工作流会原样保留模板工程里原有的逆变器台数和组串数。

## 适用模板条件与单位约定

要启用自动整数容量规划，源模板变体必须满足规整的独立 MPPT 拓扑：
- 单子阵列、单固定平面并网（Grid）工程，无 3D 阴影场景或 Module Layout。
- 子阵列启用独立 MPPT 模式（`Flags` 含 `$0400`），未开启逆变器功率共享（无 `$0100` 且 `PowerSharingInverterId=-1`）。
- 电路树中恰好包含 1 个独占逆变器组、1 个可按倍数缩放的 MPPT 组（每台逆变器的 MPPT 路数必须等于目标 `.OND` 的 `NbMPPT`，即所有 MPPT 通道均被使用）以及 1 个并联组串叶节点，且修改前的 `NElements` 计数必须显式存在。
- 组件 `.PAN` 为晶硅组件（`GRef=1000 W/m²`、`TRef=25 °C`），且包含所选电压标准对应的系统最大电压字段（选 `IEC` 时读根级 `VMaxIEC`，选 `UL` 时读 `VMaxUL`）。
- 单位约定：`.PAN` 额定功率为 `W`，`.OND` 逆变器功率为 `kW`，电压为 `V`，电流为 `A`（注意 `.OND` 中若 `IDCMax=0` 表示未设整机电流上限，不会当作 `0 A` 拦截）。

## `capacity` 参数结构

```json
{
  "target_dc_kw": 21.6,
  "max_dc_ac_ratio": 1.3,
  "capacity_tolerance_pct": 5,
  "voltage_standard": "IEC",
  "cold_voc_v": 65,
  "hot_vmp_v": 38,
  "cold_vmp_v": 47,
  "module_max_operating_current_a": 16,
  "module_max_short_circuit_current_a": 18,
  "mppt_max_operating_current_a": 32,
  "mppt_max_short_circuit_current_a": 40,
  "design_min_temperature_c": -10,
  "design_min_operating_temperature_c": 20,
  "design_max_operating_temperature_c": 70,
  "design_basis": "Site extreme -10 C ambient, 20..70 C cell operating range."
}
```

校验规则：
- 温度顺序必须满足 `-100 ≤ design_min_temperature_c ≤ design_min_operating_temperature_c ≤ design_max_operating_temperature_c ≤ 150`（°C）。其中 `cold_voc_v` 对应最低环境温度下的开路电压，`cold_vmp_v` 和 `hot_vmp_v` 对应组件工作温度范围下的工作电压。
- 若工作温度区间覆盖了 STC 参考温度（`25 °C`），则 `hot_vmp_v .. cold_vmp_v` 必须包住 `.PAN` 的参考 `Vmp`，`cold_voc_v` 必须不低于参考 `Voc`；声明的最大工作/短路电流也必须不低于 `.PAN` 的参考 `Imp`/`Isc`。
- 除 `capacity_tolerance_pct` 默认值为 `5`（允许范围 `0..20%`）外，其余字段均为必填。

## 整数求解与分步写入

搜索空间限定为：每串串联组件数 `1..100`、每路 MPPT 并联串数 `1..100`、逆变器台数 `1..10000`。每个候选整数组合必须同时满足：
1. **串联电压约束**：热态组串 $V_{mp} \ge \text{VMppMin}$，冷态组串 $V_{mp} \le \text{VMPPMax}$，冷态组串 $V_{oc} \le \min(V_{\text{Max,Inv}}, V_{\text{Max,Mod}})$。
2. **并联电流约束**：单路 MPPT 下的并联总工作电流与总短路电流不超过 `mppt_max_operating_current_a` 与 `mppt_max_short_circuit_current_a`。
3. **容量与容配比约束**：实际直流容量落在 `target_dc_kw ± capacity_tolerance_pct%` 区间内，且直流/交流容配比（DC:AC）不超过 `max_dc_ac_ratio`。

在所有满足约束的可行解中，按**容量绝对偏差最小 → 逆变器台数最少 → 与模板原串联数最接近**的优先级选出唯一解，并在 `capacity_plan` 中返回选定的串并联数、实际装机量、容配比和校核明细。如果整个搜索域内没有满足所有硬约束的整数解，工具会直接报错停止并指出是哪条约束卡住了。

落盘时，工具会依次调用底层的逆变器数量、并联总串数和每串串联数更新事务，每一步都带文件哈希校验和备份，写入完成后再用新算出的组件总数去校核全年仿真的标准 PR。关于 PVsyst 官方的阵列电压选配逻辑，可参考 [PVsyst Help: Array voltage sizing](https://www.pvsyst.com/help/project-design/grid-connected-system-definition/array-voltage-sizing-according-to-inverter.html)。
