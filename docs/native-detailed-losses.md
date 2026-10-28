# 详细损失参数：组串电压失配与辅助耗电曲线

本文档说明 `pvsyst_update_variant_parameters` 中的 `string_mismatch_updates`（组串间电压失配）和 `auxiliary_updates` 的 `profile`（带阈值/比例的辅助耗电）模式。这两项均针对 PVsyst 8.1.6 单子阵列、单固定平面、自由地平线、无 3D 阴影的纯光伏并网变体，每次更新作为独立事务执行（可同时附带 `variant_name`）。

## 组串电压失配（`string_mismatch_updates`）

在 PVsyst 的详细损失窗口里，“组件 MPP 失配（`PMismMPP`）”和“组串间电压失配（`PMismString`）”是两个独立的字段，分别由不同的默认标志位控制。

- **切换为常数百分比**：传 `{"mode": "constant", "loss_percent": 2.5}`（范围 `0.01..100`%，步长 `0.01`）。工具会清除所属 `pvSubArray.Flags` 的 bit 17 默认位，并在 `PMismUFixe` 之后写入 `PMismString`。
- **恢复默认模式**：传 `{"mode": "default"}`。工具会把 `pvSubArray.Flags` 的 bit 17 重新置位，并移除手动的 `PMismString` 字段（此时 `pvsyst_get_variant_parameters` 中的 `subarrays[].string_mismatch.loss_percent` 返回 `null`）。

## 带阈值或比例的辅助耗电（`auxiliary_updates` 的 `profile` 模式）

关于基础的 `constant`（固定运行/夜间耗电）和 `none` 模式，见[辅助功率配置](native-auxiliary-power.md)。

当使用 `mode="profile"` 时，必须显式传入全部 5 个数值字段：

```json
{
  "auxiliary_updates": {
    "mode": "profile",
    "operating_kw": 0,
    "night_kw": 0,
    "operating_threshold_kw": 0,
    "proportional_w_per_kw": 10,
    "proportional_threshold_kw": 5
  }
}
```

支持两种配置形态（不支持将常数和比例混开，且 `profile` 模式下 `night_kw` 必须为 `0`）：
1. **带启动阈值的常数运行耗电**：`operating_kw > 0`（步长 `0.01 kW`）且 `operating_threshold_kw > 0`（步长 `0.1 kW`），其余三项为 `0`。
2. **与输出功率成比例的运行耗电**：`proportional_w_per_kw > 0`（单位 `W/kW`，步长 `0.1`）且 `proportional_threshold_kw >= 0`（单位 `kW`，步长 `0.1`），其余三项为 `0`。

**底层单位差异注意**：在 `.VCx` 文件的 `pvSystem` 块里，`AuxPower_Const` 和 `AuxPower_Night` 是按 **瓦（W）** 存储的，而阈值 `AuxPower_CThresh` / `AuxPower_PThresh` 是按 **千瓦（kW）** 存储的，比例系数 `AuxPower_Pout` 则是 **W/kW**。工具内部会按各自字段的原生单位分别转换，不会一刀切地全部乘 1000。

## 其他详细损失项入口索引

- 常数热损系数 $U_c$（`UFactor`）、光致衰减（`LIDLoss`）、组件质量（`ModuleQualityLoss`）与组件失配（`PMismMPP`）：见[并网拓扑与参数编辑](grid-capacity-verification.md)及[损失 GUI 往返说明](loss-gui-roundtrip.md)。
- 直流/交流线缆与中压变压器夜间断开：分别见[DC 线缆桥接](native-dc-cabling.md)、[AC 线缆配置](native-ac-cabling.md)、[MV 夜间断开](native-mv-night.md)。
- 入射角修正（IAM）与组件年衰减（Ageing）：见[老化与 IAM 输入](native-model-inputs.md)。
- 积灰损失（Soiling）：见[积灰配置](soiling-verification.md)。
