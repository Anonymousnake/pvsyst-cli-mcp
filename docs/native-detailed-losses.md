# 详细损失参数：失配、辅助耗电与停运配置

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

## 清除 PV 系统停运配置

`pvsyst_get_variant_parameters.system_unavailability` 读取 PV 系统自身已保存的停运配置。要移除已有配置，在 `pvsyst_update_variant_parameters` 的通用请求中传入以下专用字段，`project`、`variant` 和当前 `expected_sha256` 仍按普通变体事务提供：

```json
{"system_unavailability_updates": {"mode": "none"}, "dry_run": true}
```

确认预览后用 `dry_run=false` 执行。该操作移除所属 `pvSystem` 的整个 `SystemUnavail=TUnavailability` 子对象，并清理其存在标记；只清标记而保留子对象并不会可靠地关闭停运。返回的 `system_unavailability.before/after` 描述保存状态，执行后读取的 `configured` 为 `false`。已经没有停运配置时是无操作，不创建备份。

执行返回 `backup_name`，其中保留完整原文件、原生时段及随机生成设置。恢复时使用 `pvsyst_restore_variant`，传入该备份名、最新读取的 `expected_sha256` 和 `confirm=true`。配置更新返回的 `native_consumption_verified=false` 不是运行结果，实际仿真仍通过 `pvsyst_run_simulation` 执行。

此事务只适用于已识别的 PVsyst 8.1.6 `Periods` 模板：单子阵、普通固定平面、自由地平线、完整电路计数，无负荷、储能、P50/P90 或活动阴影。只接受 `mode=none`，可附带 `variant_name`，其他物理更新分开提交。它不生成新时段、不重新随机抽样，也不修改 WeakGrid 的电网可用性。

## 其他详细损失项入口索引

- 常数热损系数 $U_c$（`UFactor`）、光致衰减（`LIDLoss`）、组件质量（`ModuleQualityLoss`）与组件失配（`PMismMPP`）：见[并网拓扑与参数编辑](grid-capacity-verification.md)及[损失 GUI 往返说明](loss-gui-roundtrip.md)。
- 直流/交流线缆与中压变压器夜间断开：分别见[DC 线缆桥接](native-dc-cabling.md)、[AC 线缆配置](native-ac-cabling.md)、[MV 夜间断开](native-mv-night.md)。
- 入射角修正（IAM）与组件年衰减（Ageing）：见[老化与 IAM 输入](native-model-inputs.md)。
- 积灰损失（Soiling）：见[积灰配置](soiling-verification.md)。
