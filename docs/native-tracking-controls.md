# 单轴与双轴跟踪器（Tracking）变体参数配置

`pvsyst_create_tracking_variant` 用于在已有的 PVsyst 8.1.6 跟踪器变体模板上修改跟踪轴角度、旋转限值、回溯（Backtracking）、大风保护（Wind Stow）等参数，并另存为一个新的 `.VCx` 变体。

如果要直接跑“克隆 `UnlimTracking` 模板 → 改跟踪参数 → 跑全年仿真并核对报告”的完整工作流，见[跟踪模板全年仿真流程](tracking-template-flow.md)。

```json
{
  "project": "example.PRJ",
  "source_variant": "VCK",
  "new_variant": "VCX",
  "tracking": {
    "axis_azimuth_deg": 0,
    "min_rotation_deg": -45,
    "max_rotation_deg": 60,
    "backtracking": true,
    "irradiance_optimization": true
  },
  "variant_name": "Tracking Controls",
  "dry_run": true
}
```

## 支持的 9 种跟踪器类型与可编辑字段

工具严格按照源变体 `pvOrient` 当前的 `FieldType` 校验允许修改的所属字段（不支持跨 `FieldType` 转换，每次至少传一个需要修改的字段，未传字段沿用源变体原值）：

| `FieldType` | 可通过 `tracking` 修改的底层字段 |
|---|---|
| `UnlimTracking`（无限水平轴） | `AxisAzim`, `MinPhi`/`MaxPhi`, `UsesBacktracking`, `UsesIrradOptimiz`, `UsesElectricalEffect` |
| `TrackHorizNSAxis`（水平南北轴） | `AxisAzim`, `MinPhi`/`MaxPhi`, `CentralGap`, `PitchSlope`, `UsesBacktracking`, `UsesIrradOptimiz`, 大风保护开关及风速/收拢角度 |
| `TrackTiltedAxis`（倾斜轴） | 包含 `TrackHorizNSAxis` 的全部可编辑字段，外加轴倾角 `AxisTilt` |
| `TrackVertAxis`（竖直轴） | `AxisAzim`, `FieldTilt`, `MinAzim`/`MaxAzim` |
| `TrackSunShield`（跟踪遮阳百叶） | `AxisAzim`, `MinTilt`/`MaxTilt`, `UsesBacktracking` |
| `TrackHorizAxis`（东西水平轴） | `AxisAzim`, `MinTilt`/`MaxTilt`, `PitchSlope`, `UsesBacktracking` |
| `TrackTwoAxis`（双轴跟踪） | `AxisAzim`, `MinAzim`/`MaxAzim`, `MinTilt`/`MaxTilt` |
| `TrackFrameNS`（南北框架双轴） | `AxisAzim`, `AxisTilt`, `MinTilt`/`MaxTilt`, `FrameMinPhi`/`MaxPhi`, `UsesBacktracking` |
| `TrackFrameEW`（东西框架双轴） | `AxisAzim`, `MinPhi`/`MaxPhi`, `FrameMinTilt`/`MaxTilt`, `UsesBacktracking` |

常用字段范围与精度要求：
- 角度与间距步长精度均为 `0.1`（保留一位小数），其中 `axis_azimuth_deg`（`AxisAzim`）范围为 `-20..20°`，`min_rotation_deg`（`MinPhi`）为 `-60..-45°`，`max_rotation_deg`（`MaxPhi`）为 `45..60°`。
- 所有成对的最小/最大角度限值（如 `MinPhi`/`MaxPhi`、`MinTilt`/`MaxTilt`、`MinAzim`/`MaxAzim`）在落盘前都会校验大小顺序（只改单侧时自动与源变体的另一侧比对）。

## 源模板前提条件

- PVsyst 8.1.6 单子阵列、单朝向（`TOrientGroup` 下唯一一个 `NoOrient=1`、`Flags=$00` 的跟踪朝向，全部组串归属朝向 1，`NStrOrient1=NStringCh`）。
- 纯光伏并网系统（`pvSystem.Flags=$06000000`，主阵列 `Flags=$01900400`、`Flags2=$082A`，子阵列 `Flags=$019E1002`、`Flags2=$20`），无独立 3D 场景、Module Layout、远方地平线、P50、负荷或储能。

## 原生二维遮挡与导出

使用跟踪器变体跑仿真并查看结果时，有几个容易混淆的地方需要注意：
1. **PDF 报告里的 `no Shadings` 不代表没有排间遮挡**：`UnlimTracking` 等自带排间距（`Pitch`）和组件宽度（`CollectorWidth`）参数的跟踪类型，由 PVsyst 内核直接按二维几何计算排间互遮挡（或回溯角度）。只要没挂接独立的 3D 场景对象，PDF 摘要里就会显示 `Near Shadings: no Shadings`，但逐时 CSV 里的 `GlobShd`（遮挡后辐照）和当勾选 `UsesElectricalEffect=True` 时的 `ShdElec`（电气遮挡损失）是正常计算并导出的。
2. **`.SFI` 变量名与 CSV 列名**：`.SFI` 输入文件里变量名带 `vi` 前缀（如 `viGlobShd`、`viShdElec`），而导出的 CSV 列名去掉了 `vi` 前缀（`GlobShd`、`ShdElec`）。当某项开关未启用、PVsyst 省略导出对应列时，结果读取器会如实反映该列未导出，不会凭空补全一列 0。
