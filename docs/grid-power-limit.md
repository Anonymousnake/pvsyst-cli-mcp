# 并网注入点有功功率上限（Grid Power Limit）

当电站受限于变压器容量或接入批复、需要在并网注入点限制最大有功上网功率（kWac）时，可通过 `pvsyst_update_variant_parameters` 的 `grid_limit_updates` 独立事务设置 `pvSystem` 的限发参数。

如果需要把限发位置设在逆变器出口、按视在功率（kVA）限发，或者需要与功率因数（PF）同时开启，请使用组合入口 [`power_controls_updates`](native-power-controls.md)。

## 调用示例

先调 `pvsyst_get_variant_parameters` 拿到当前变体的 `expected_sha256` 令牌：

```json
{
  "project": "WORKING.PRJ",
  "variant": "VC1",
  "expected_sha256": "<current-token>",
  "grid_limit_updates": {
    "mode": "constant",
    "limit_kw": 250
  },
  "dry_run": true
}
```

- **开启限发（`mode="constant"`）**：`limit_kw` 为正数，步长精度 `0.1 kW`。
- **关闭限发（`mode="none"`）**：不传 `limit_kw`，工具会移除限值与单独损失记账字段，并将使能位清零。

## 底层 `.VCx` 字段与标志位

该入口适用于 PVsyst 8.1.6 单子阵列、单固定平面、无 3D 阴影且未开启 PF/负荷/储能的纯光伏并网变体。开启注入点有功限发且单独统计限电损失时，`pvSystem` 块写入如下字段：

```text
Flags=$05000000
SystemType=Grid
PLimGrid=250.0
LimGrSeparLoss=True
```

标志位拆解说明：
- `$01000000`（bit 24）是限发总使能位；`$04000000`（bit 26）代表限发位置在**并网注入点**（若在逆变器出口则为 `$01000000`）。因此当你在 GUI 里把注入点限发关掉时，文件里往往会留下 `$04000000`（仅保留位置位，使能位已清零，且 `PLimGrid` 与 `LimGrSeparLoss` 字段消失）。
- 注意区分 `pvSystem.Flags` 的 **bit 27**：bit 27 是光储系统里的“禁止向电网送电（防逆流）”开关，不是数值型有功限发。

## 导出限发相关逐时列

调用 `pvsyst_build_sfi(variables="grid_limit")` 生成 `.SFI` 时，会自动勾选 `PGridLim`（功率上限值）、`EGrdLim`（限电削峰损失电量）、`EUnused` 和 `EOutInv`。

看结果时注意：当 `LimGrSeparLoss=True`（单独统计并网限电损失）时，`EOutInv` 是逆变器出口未削峰前的电量，最终受限后的上网电量应看 **`E_Grid`**（其逐时最大值不会超过 `PLimGrid`），被削掉的电量记在 **`EGrdLim`** 中（此时 `EUnused` 为 0 不代表没发生限电）。
