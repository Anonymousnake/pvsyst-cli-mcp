# PVsyst 朝向类型（`FieldType`）与编辑入口对照

在 PVsyst 8.1.6 的 `.VCx` 变体文件中，`TOrientGroup` 下的朝向对象根据 `FieldType` 的不同，拥有各自专属的几何与角度字段（其中双坡对置的 `Dome` 选一次会在列表里成对生成两条互指的 `pvDomeOrient` 记录）。

下面是全量 `FieldType` 的底层关键字段与对应 MCP 工具入口速查表：

| `FieldType` | 核心保存字段 / 结构特点 | 对应 MCP 编辑入口与文档 |
|---|---|---|
| `FixedPlane` | `FieldTilt`, `FieldAzim`, `BaseSlope` | `pvsyst_update_variant_parameters` / `pvsyst_clone_subarray`（见[并网拓扑与固定平面](grid-capacity-verification.md)） |
| `Dome` + `DomeBackfaces` | 两条 `pvDomeOrient` 记录，通过 `NoOrient=1/2` 与 `NoPairOrient=2/1` 互指 | `pvsyst_create_dome_allocation_variant`（见[Dome 双朝向分配](native-dome-allocation.md)） |
| `SeasonalTilt` | `SummerTilt`, `WinterTilt`, `WinterMonths`（此时 `FieldTilt=0.0`） | `pvsyst_update_variant_parameters.seasonal_updates`（见[季节倾角](seasonal-orientation.md)） |
| `Sheds` / `SunShields` | 倾角/方位角 + 排间距 `Pitch`、宽度 `CollectorWidth`、排数 `NSheds` 等 | `pvsyst_create_shed_geometry_variant`（见[棚架与遮阳板配置](native-shed-controls.md)） |
| `UnlimTracking` | `AxisAzim`, `MinPhi`/`MaxPhi`, `UsesBacktracking`, `UsesIrradOptimiz`, 棚架几何 | `pvsyst_create_tracking_variant`（见[跟踪控制](native-tracking-controls.md)） |
| `TrackHorizNSAxis` | `AxisAzim`, `MinPhi`/`MaxPhi`, `CentralGap`, `PitchSlope`, 回溯与大风保护（Wind Stow） | `pvsyst_create_tracking_variant` |
| `TrackTiltedAxis` | 在 `TrackHorizNSAxis` 基础上增加轴倾角 `AxisTilt` | `pvsyst_create_tracking_variant` |
| `TrackVertAxis` | `FieldTilt`, `AxisAzim`, `MinAzim`/`MaxAzim` | `pvsyst_create_tracking_variant` |
| `TrackSunShield` | `AxisAzim`, `MinTilt`/`MaxTilt`, `UsesBacktracking` | `pvsyst_create_tracking_variant` |
| `TrackHorizAxis` | `AxisAzim`, `MinTilt`/`MaxTilt`, `PitchSlope`, `UsesBacktracking` | `pvsyst_create_tracking_variant` |
| `TrackTwoAxis` | `AxisAzim`, `MinAzim`/`MaxAzim`, `MinTilt`/`MaxTilt` | `pvsyst_create_tracking_variant` |
| `TrackFrameNS` | `AxisAzim`, `AxisTilt`, `MinTilt`/`MaxTilt`, `FrameMinPhi`/`MaxPhi`, `UsesBacktracking` | `pvsyst_create_tracking_variant` |
| `TrackFrameEW` | `AxisAzim`, `MinPhi`/`MaxPhi`, `FrameMinTilt`/`MaxTilt`, `UsesBacktracking` | `pvsyst_create_tracking_variant` |

## 常见踩坑细节

1. **单位差异**：在 `.VCx` 文件里，所有角度均以**度（deg）**存储，而 `Pitch`、`CollectorWidth`、`InactiveBandWidth` 和 `CellWidth` 全部以**米（m）**存储（尽管 PVsyst GUI 界面上经常把电池片宽度 `CellWidth` 显示为厘米，落盘时仍是米，例如 `0.156`）。
2. **`Comment` 不代表当前类型**：在 GUI 里切换过朝向类型后，对象头部的 `Comment` 字符串有时还会残留上一次类型的名字，判断朝向类型一律以 `FieldType` 为准。
3. **成对的 `Dome` 记录**：`Dome`（`NoOrient=1`, `IsMainOrient=True`）和 `DomeBackfaces`（`NoOrient=2`, `IsMainOrient=False`）必须成对出现，且子阵列必须同时关联这两面（`NoOrientation=1`, `NoOrientation2=2`）；如果第二面没有分配组串，PVsyst 会在校验时报错拦截。
4. **跟踪器与棚架的内建二维遮挡**：`Sheds`、`SunShields` 和 `UnlimTracking` 等类型在 PVsyst 内核里自带二维排间遮挡计算，详情见[原生二维遮挡与导出](native-tracking-controls.md#原生二维遮挡与导出)。
