# 逆变器工作温度配置（Inverter Temperature）

在 PVsyst 的能量管理（Energy Management）设置中，逆变器所处环境温度支持三种模式：直接跟随气象文件的室外环境温度（`ambient`）、室外环境温度加上固定温升偏移（`offset`）、或者恒定室内温度（`fixed`）。

`pvsyst_update_variant_parameters` 通过独立事务 `inverter_temperature_updates` 修改该配置，`pvsyst_get_variant_parameters` 则在 `inverter_temperature`（以及原始字段字典 `system_parameters`）中返回当前状态。

```json
{
  "project": "INVERTER_TEMPERATURE.PRJ",
  "variant": "VC1",
  "expected_sha256": "<current-token>",
  "inverter_temperature_updates": {
    "mode": "fixed",
    "offset_c": 20,
    "fixed_c": 60
  }
}
```

## 三种模式的字段与标志位规则

在 `.VCx` 文件的 `pvSystem` 块中，温度模式由 `pvSystem.Flags` 的 `$4000` 和 `$8000` 两个位控制，温度数值保存在 `TempShift` 和 `TempFixed` 中：

| `mode` | 入参要求 | `.VCx` 内部写入行为 |
|---|---|---|
| `ambient` | 仅传 `{"mode": "ambient"}` | 清除 `pvSystem.Flags` 中的 `$4000` 与 `$8000` 位，并直接移除 `TempShift` 和 `TempFixed` 两个字段（不是把它们写成 `0`） |
| `offset` | 必须同时传 `offset_c` 和 `fixed_c` | 置位 `$4000`（清除 `$8000`），同时保存 `TempShift=offset_c` 与 `TempFixed=fixed_c`（此时 PVsyst 按“室外气温 + `TempShift`”计算，但文件里仍保留未激活的 `TempFixed` 值） |
| `fixed` | 必须同时传 `offset_c` 和 `fixed_c` | 置位 `$8000`（清除 `$4000`），同时保存 `TempShift=offset_c` 与 `TempFixed=fixed_c`（此时逆变器恒定按 `fixed_c` 运行，比如上面示例中恒为 `60 °C`，而不是 `60 + 20`） |

数值要求：`offset_c` 与 `fixed_c` 单位均为 `°C`，允许范围 `[-100, 100]`，步长 `0.1 °C`。

## 适用模板条件

源变体需为 PVsyst 8.1.6 单子阵列、单固定平面（朝向 1）、自由地平线、无 3D 阴影场景、且 `pvSystem` 未开启功率因数（PF）、并网限发、辅助耗电或储能的纯光伏并网工程（若 `pvSystem.Flags` 带有残留的 `$06000000` 关闭位或未知字段，写入器会报错拦截以防误清其他位）。
