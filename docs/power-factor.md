# 功率因数（Power Factor）与逆变器 PNom 限制方式

`pvsyst_update_variant_parameters` 中的 `power_factor_updates` 用于独立配置系统功率因数（常数或 12 个月分月设定）以及逆变器额定功率 `PNom` 的限制基准（`pnom_limit`）；当前配置可通过 `pvsyst_get_variant_parameters.power_factor` 读取。

如果需要同时配置功率因数和并网注入点有功/视在功率上限（`PLimGrid`），请使用组合入口 [`power_controls_updates`](native-power-controls.md)。

## 入参结构与单位约定

调用 `power_factor_updates` 时必须显式传入 `mode` 和 `pnom_limit`（可同批附带 `variant_name`）：

| `mode` | 数值字段 | 允许的 `pnom_limit` |
|---|---|---|
| `none` | 无（从 `pvSystem` 中移除 PF 字段） | `"native"` |
| `constant` | `tan_phi`（单个数值） | `"native"` / `"active"` / `"apparent"` |
| `monthly` | `monthly_tan_phi`（1～12 月共 12 个数值的列表） | `"native"` / `"active"` / `"apparent"` |

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

**关键注意点：传的是 $\tan\varphi$ 而不是 $\cos\varphi$**
- PVsyst 在 `.VCx` 的 `pvSystem` 块里保存的是无量纲相角正切值 `TanPhi`（或月度 `TanPhiM`），**不是余弦功率因数 $\cos\varphi$**（例如 $\cos\varphi = 0.90$ 对应 $|\tan\varphi| \approx 0.484$）。
- 允许范围为 `[-1, 1]`，步长 `0.001`；正值代表滞后（Lagging），负值代表超前（Leading）。
- `pnom_limit` 控制逆变器额定输出是按 `.OND` 原生设置（`native`）、强制按有功功率 kW（`active`）还是强制按视在功率 kVA（`apparent`）限幅。

## 底层 `pvSystem.Flags` 掩码对照

本事务要求源变体为 PVsyst 8.1.6 单子阵列、单固定平面、自由地平线、无 3D 阴影且未开启温度/辅助/限发/储能的纯光伏并网变体。不同组合在 `pvSystem.Flags` 中的十六进制值如下：

| 配置组合 | `pvSystem.Flags` |
|---|---|
| 关闭 PF (`mode="none"`, `pnom_limit="native"`) | `$00` |
| 常数 PF + `native` PNom | `$00100000` |
| 月度 PF + `native` PNom | `$00300000` |
| 常数 PF + 强制有功 PNom (`active`) | `$00900000` |
| 常数 PF + 强制视在 PNom (`apparent`) | `$00500000` |
| 月度 PF + 强制有功 PNom (`active`) | `$00B00000` |
| 月度 PF + 强制视在 PNom (`apparent`) | `$00700000` |

## 仿真导出列的单位提示

在导出包含无功与视在能量的 CSV 时（如 `EReGrid`、`EApGrid`），PVsyst 8.1.6 原生表头把这两列的单位都标为 `kVAh`，并且即使设置了负的 `TanPhi`（超前），导出的 `EReGrid` 也是非负数值。结果解析器会原样保留 PVsyst 输出的表头和数值，不会擅自改表头或加负号。
