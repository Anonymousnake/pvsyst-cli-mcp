# 详细损失参数在 GUI 重新保存时的变化说明

在用 `pvsyst_update_variant_parameters` 修改了 `.VCx` 的损失参数（如 `UFactor`、`LIDLoss`、`ModuleQualityLoss`、`PMismMPP`）之后，如果你把该工程在 PVsyst 8.1.6 桌面 GUI 里打开并重新点了一次“保存”，即使你没在界面上改动任何数字，GUI 写回磁盘的 `.VCx` 文件也会发生几处格式和标志位上的变化：

1. **小数位数格式化**：MCP 编辑器按 `.3f`（三位小数）写入百分比标量，而 PVsyst GUI 打开详细损失窗口再保存时，往往会把 `LIDLoss` 和 `PMismMPP` 重写成两位小数。
2. **组件质量损失缓存字段（`PQualityModP`）**：修改 `ModuleQualityLoss` 时，MCP 编辑器同步更新子阵列的 `PQualityModU` 和 `PQualityModI` 并清除默认标志位 `0x00100000`。当你在 GUI 里打开并重新保存后，GUI 会顺手重算并改写只读缓存字段 `PQualityModP`。
3. **主阵列标志位与默认字段改写**：热损系数 `UFactor` 要求主阵列处于标准的 `Flags=$00100400`、`Flags2=$082A` 状态。但在 GUI 里打开详细损失各标签页并保存后，GUI 可能会把主阵列 `Flags` 改写成 `$01900400` 或 `$03900400`，并顺带写入 `MVNbTransfos`、`ACWireNoSect` 等默认字段。

## 实操建议

- 一旦在 GUI 里手动重新保存了某个变体，因为文件 SHA-256 和主阵列 `Flags` 已经被 GUI 改写，原来只认 `$00100400` 的部分标量写入器可能会拒绝再次直接修改该变体。
- 如果需要在恢复成干净状态后继续用 MCP 批量改参，可以直接用上次事务返回的令牌调用 `pvsyst_restore_variant` 还原到 GUI 打开前的备份快照，或者从原始干净模板重新克隆变体。
- 各类详细损失对应的专门修改入口：
  - 基础标量（`UFactor`、`LIDLoss`、`ModuleQualityLoss`、`PMismMPP`）：`pvsyst_update_variant_parameters`（见[并网拓扑与参数编辑](grid-capacity-verification.md)）
  - 组串间电压失配（`PMismString`）与带阈值的辅助耗电：`pvsyst_update_variant_parameters`（见[详细损失参数](native-detailed-losses.md)）
  - 积灰损失：`soiling_updates`（见[积灰配置](soiling-verification.md)）
  - 交直流线缆与中压夜间开关：分别使用 [`pvsyst_create_ac_cabling_variant`](native-ac-cabling.md)、[`pvsyst_create_native_dc_cabling_variant`](native-dc-cabling.md) 和 [`pvsyst_create_mv_night_disconnect_variant`](native-mv-night.md)
