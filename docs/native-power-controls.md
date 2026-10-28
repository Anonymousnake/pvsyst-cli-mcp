# 功率因数（PF）与并网功率限制组合配置

当同一个变体既要设功率因数（`TanPhi` / `TanPhiM` 与逆变器 `PNom` 基准），又要设网侧功率上限（有功 kW 或视在 kVA、注入点或逆变器端、是否单独统计限电损失）时，因为这两组设置共用 `.VCx` 中 `pvSystem.Flags` 的同一个 32 位掩码，需要通过 `pvsyst_update_variant_parameters` 的 `power_controls_updates` 在一次事务中联合写入；读取时查看 `pvsyst_get_variant_parameters.power_controls`。

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

## 参数结构与合法组合规则

### 1. `power_factor` 子对象
- `mode`：`"none"`、`"constant"` 或 `"monthly"`。
- `pnom_limit`：`"native"`、`"active"` 或 `"apparent"`（当 `mode="none"` 时必须为 `"native"`）。
- `tan_phi` / `monthly_tan_phi`：相角正切值（正数滞后、负数超前，范围 `[-1, 1]`，步长 `0.001`；详见[功率因数](power-factor.md)）。

### 2. `grid_limit` 子对象
- `mode`：`"none"` 或 `"constant"`。
- `quantity`：`"active"`（有功上限，单位 kW）或 `"apparent"`（视在上限，单位 kVA）。
- `point`：`"injection"`（并网注入点）或 `"inverter"`（逆变器出口端）。
- `limit` 與 `separate_loss`：当 `mode="constant"` 时必填，`limit` 步长精度 `0.1`（直接代表所选 `quantity` 的 kW 或 kVA 值，工具不做余弦折算），`separate_loss` 为布尔值（对应 `LimGrSeparLoss`）。当 `mode="none"` 时省略这两个字段（工具会移除标量字段，但按 PVsyst 原生习惯保留 `Flags` 中的 `quantity` 和 `point` 位置位）。

### 3. 支持的组合约束
当 `grid_limit.mode="constant"` 启用限发时，需满足以下 PVsyst 8.1.6 已验证组合：
- 若使用了月度 PF（`monthly`）或强制 `active`/`apparent` PNom：限发位置必须为 `point="injection"` 且 `separate_loss=true`（其中 `monthly` + 强制 `active`/`apparent` PNom 仅支持 `quantity="active"` 有功注入限值，对应 `Flags` `$05B00000` / `$05700000`）。
- 若限发位置选在逆变器端（`point="inverter"`）：要求 `power_factor` 为 `constant`、`pnom_limit="native"` 且 `separate_loss=true`。
- 若不单独列计限电损失（`separate_loss=false`）：要求 `power_factor` 为 `constant`、`pnom_limit="native"` 且 `point="injection"`。
- 若关闭 PF（`power_factor.mode="none"`）仅开限发：要求 `quantity="active"`、`point="injection"` 且 `separate_loss=true`。

如果要把变体重置回完全无 PF、无限发的空白默认态（`Flags=$00`），可传入 `power_factor: {"mode": "none", "pnom_limit": "native"}` 加 `grid_limit: {"mode": "none", "quantity": "active", "point": "inverter"}`。

## 导出结果的表头小贴士

在视在功率限发模式（`quantity="apparent"`）下，PVsyst 导出的 CSV 中 `PGridLim` 列的单位表头依然固定写着 `kW`（尽管它的数值和限幅逻辑实际上是 kVA），而 `EReGrid` / `EApGrid` 依然标为 `kVAh`（见[系统字段与输出单位](native-energy-management.md)）。
