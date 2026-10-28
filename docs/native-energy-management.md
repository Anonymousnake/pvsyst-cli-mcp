# `pvSystem` 能量管理字段与导出单位对照

调用 `pvsyst_get_variant_parameters` 时，`system_parameters` 会原样返回 `.VCx` 中 `PVObject_System=pvSystem` 直属的 7 个原始字段字符串：`SystemType`、`Flags`、`TempShift`、`TempFixed`、`TanPhi`、`PLimGrid`、`LimGrSeparLoss`。

若要修改这些参数，请分别使用对应的事务入口：[逆变器温度](inverter-temperature.md)、[功率因数](power-factor.md)、[并网有功上限](grid-power-limit.md)或[组合功率控制](native-power-controls.md)。

## 常见 `pvSystem.Flags` 十六进制状态速查

| 系统状态 | `pvSystem.Flags` | 同时存在的字段示例 |
|---|---|---|
| 默认环境温度、无 PF、无限发 | `$00` | 仅 `SystemType=Grid`，无温度/PF/限发字段 |
| 曾开启过视在注入限发后关闭（保留位置与类型位） | `$06000000` | 无温度/PF/限发标量字段 |
| 室外环境温度 + 固定偏移（`offset`） | `$4000` | `TempShift=20.0`, `TempFixed=25.0` |
| 恒定室内温度（`fixed`） | `$8000` | `TempShift=20.0`, `TempFixed=60.0` |
| 常数滞后 PF（`native` PNom） | `$00100000` | `TanPhi=0.484` |
| 常数 PF + 注入点有功限发（单列损失） | `$05100000` | `TanPhi=0.484`, `PLimGrid=250.0`, `LimGrSeparLoss=True` |
| 常数 PF + 注入点视在限发（单列损失） | `$07100000` | 同上，bit 25（`$02000000`）置位代表视在功率 kVA |

阅读 `.VCx` 源码时的两个细节：
1. 在 `offset` 或 `fixed` 温度模式下，`TempShift` 和 `TempFixed` 两个字段会同时存在于文件中，到底哪一个生效完全看 `Flags` 是 `$4000` 还是 `$8000`；切回 `ambient` 模式时这两个字段才会被删掉。
2. 功率因数存的是正切值 `TanPhi`，不是 $\cos\varphi$。

## 仿真 CSV 导出变量与单位

在构建 `.SFI` 导出变量时，能量管理相关的原生变量名为 `viInvTemp`（输出列 `InvTemp`，单位 `°C`）、`viInvPmax`（输出列 `InvPmax`，单位 `kW`）、`viEReGrid`（无功列 `EReGrid`）和 `viEApGrid`（视在列 `EApGrid`）。

需要注意 PVsyst 导出 CSV 的两处原生习惯：
- `EReGrid` 和 `EApGrid` 的单位行在 CSV 里都写着 `kVAh`（即使 `EReGrid` 是无功分量），且超前（负 `TanPhi`）时导出的 `EReGrid` 也不会带负号。
- 当开启视在功率限发（`$02000000`）时，导出的 `PGridLim` 列表头单位依然写着 `kW`，核对时需结合 `.VCx` 的 `Flags` 或 PDF 报告正文。
