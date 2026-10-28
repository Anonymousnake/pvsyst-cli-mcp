# 季节性倾角（Seasonal Tilt）配置

对于采用可调支架、每年按夏冬两季手动切换倾角的电站，`pvsyst_update_variant_parameters` 提供了 `seasonal_updates` 独立事务，可将现有的常规固定平面（`FixedPlane`）切换为季节性倾角平面（`SeasonalTilt`），或直接更新已有的季节性平面参数。

```json
{
  "project": "SEASONAL_PLANE.PRJ",
  "variant": "VC1",
  "expected_sha256": "<current-token>",
  "seasonal_updates": {
    "summer_tilt": 10,
    "winter_tilt": 30,
    "winter_months": [1, 2, 3, 10, 11, 12],
    "orientation_name": "SEASONAL_S10_W30"
  }
}
```

## 参数要求与底层字段

- `summer_tilt` 与 `winter_tilt`：夏季与冬季倾角（单位度，范围 `0..90`，步长 `0.1°`），两项均必填。
- `winter_months`：属于冬季倾角的月份列表，需传入 `1..11` 个不重复的整数（取值 `1..12`，顺序不限，未被列出的其余月份自动归为夏季倾角；不允许将全年 12 个月全划归同一个季节）。
- `orientation_name`：可选，用于修改该朝向块自身的 `Comment` 名称（不是变体标题 `variant_name`；若包含中文等非 ASCII 字符，按常规规则需要文件已有 UTF-8 BOM 或显式开启 `add_utf8_bom=true`）。

在 `.VCx` 文件的 `TOrientGroup` 直属 `pvOrient` 块中，切换为季节模式会同步改写以下字段：
- `FieldType=SeasonalTilt`
- `FieldTilt=0.0`（注意：在 `SeasonalTilt` 模式下，PVsyst 原生固定把 `FieldTilt` 置为 `0.0`，实际参与仿真的是 `SummerTilt` 和 `WinterTilt`，看到 `FieldTilt=0.0` 不要误以为变成了水平安装）
- `SummerTilt=<summer_tilt>`、`WinterTilt=<winter_tilt>`
- `WinterMonths=$<16进制月份位掩码>`（例如 `[1, 2, 3, 10, 11, 12]` 编码为 `$0E07`）
- 原本固定平面下的 `BaseSlope=0.0` 字段会在切换时移除（若源固定平面带有非零 `BaseSlope` 则会报错拦截）。方位角 `FieldAzim`、子阵列绑定与组串分配保持不变。

## 适用范围与回滚

- 仅适用于 PVsyst 8.1.6 单子阵列、单朝向（朝向 1）、自由地平线、无 3D 阴影场景、未开启倾角平均（`Uses*Aver=False`）及 PF/限发/负荷/储能的纯光伏并网变体。
- 如果想把已经改成 `SeasonalTilt` 的变体切回普通固定平面，直接用修改事务返回的令牌调用 `pvsyst_restore_variant` 还原备份快照即可。关于季节朝向与远方地平线的底层字段细节，另见[季节朝向与地平线维护笔记](plane-horizon-native.md)。
