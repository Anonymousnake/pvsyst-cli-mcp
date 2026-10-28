# 无限长棚架（Sheds）与遮阳板（SunShields）配置

`pvsyst_create_shed_geometry_variant` 用于在已有的 PVsyst 8.1.6 `Sheds`（无限长多排固定棚架）或 `SunShields`（无限长立面遮阳百叶）模板变体上修改几何尺寸、角度与电气遮挡开关，并另存为一个新的 `.VCx` 变体。

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC2",
  "geometry": {
    "pitch_m": 6.5,
    "field_tilt_deg": 15.1
  },
  "dry_run": true
}
```

## `geometry` 可编辑字段与精度要求

调用时在 `geometry` 字典中传入至少一个需要修改的字段，未传入的字段自动继承源变体的原值：

| 入参键名 | 对应 `.VCx` 字段 | 取值范围与步长精度 |
|---|---|---|
| `field_tilt_deg` | `FieldTilt` | `0..90` 度，步长 `0.1` |
| `field_azimuth_deg` | `FieldAzim` | `-180..180` 度，步长 `0.1` |
| `pitch_m` | `Pitch` | 正数（米），步长 `0.001` |
| `collector_width_m` | `CollectorWidth` | 正数（米），步长 `0.001` |
| `inactive_band_width_1_m` / `inactive_band_width_2_m` | `InactiveBandWidth1` / `InactiveBandWidth2` | 非负数（米），步长 `0.001`，且两项之和必须严格小于 `CollectorWidth` |
| `cell_width_m` | `CellWidth` | 正数（米），步长 `0.001`（注意传米，如 `0.156`） |
| `shading_factor` | `FracShadingFactor` | `0..1` 比例，步长 `0.001` |
| `shed_count` | `NSheds` | 正整数 `1..2147483647` |
| `module_columns` | `NModInWidth` | 正整数 `1..2147483647` |
| `electrical_effect` | `UsesElectricalEffect` | 布尔值（源记录中需已存在该字段） |

传入超出步长精度的小数（比如给 `pitch_m` 传 4 位小数）会直接报错拦截，不会静默四舍五入。

## 源模板前提条件

- 源变体必须已经是 PVsyst 8.1.6 的 `FieldType=Sheds` 或 `FieldType=SunShields`（不支持把普通 `FixedPlane` 转成 `Sheds`，也不支持在 `Sheds` 和 `SunShields` 之间互转）。
- 单子阵列、单朝向（`NoOrient=1`，`TOrientGroup` 下包含唯一一个完整的 20 字段直属 `pvOrient` 记录，且 `Flags=$00`、`Uses*Aver=False`、`BTNoTrackers=-1,-1,`）。
- 系统为纯并网模式（`pvSystem.Flags=$06000000`，主阵列 `Flags=$01900400`、`Flags2=$082A`，子阵列 `Flags=$019E1002`、`Flags2=$20`），无 3D 场景、Module Layout、远方地平线、P50、负荷或储能。

生成新 `.VCx` 后（可选同时设置 `variant_name` 与 `add_utf8_bom`），可通过 `pvsyst_get_variant_parameters` 读取全部 11 项棚架几何参数，或直接调用 `pvsyst_run_simulation` 进行仿真。
