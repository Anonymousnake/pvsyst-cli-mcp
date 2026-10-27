# 原生朝向类型与依赖

PVsyst8.1.6的朝向窗口将类型分组展示，分组标题不是类型。Dome一次选择产生两条配对记录，因此可选项数量不等于FieldType字面值数量。读取原生记录不表示适配器可任意构造该类型。

| 原生类型 | 主要保存字段／结构 | MCP入口 |
|---|---|---|
| FixedPlane | FieldTilt、FieldAzim、BaseSlope | [固定平面与生命周期](grid-capacity-verification.md) |
| Dome＋DomeBackfaces | 两条pvDomeOrient／TDomeOrientGroup，互指配对 | [既有有效模板的分配](native-dome-allocation.md) |
| SeasonalTilt | SummerTilt、WinterTilt、WinterMonths | [季节倾角](seasonal-orientation.md) |
| Sheds／SunShields | 角度及棚架几何，类型不同 | [棚架配置](native-shed-controls.md) |
| UnlimTracking | 轴方位、phi界限、回溯、辐照优化、棚架 | [跟踪控制](native-tracking-controls.md) |
| TrackHorizNSAxis | phi、CentralGap、PitchSlope及风收拢 | 同一跟踪入口，按类型限定 |
| TrackTiltedAxis | 前一类字段及AxisTilt | 同一跟踪入口 |
| TrackVertAxis | FieldTilt、AxisAzim、MinAzim/MaxAzim | 同一跟踪入口 |
| TrackSunShield | 倾角界限、回溯、棚架 | 同一跟踪入口 |
| TrackHorizAxis | 倾角界限、PitchSlope、回溯、棚架 | 同一跟踪入口 |
| TrackTwoAxis | 方位及倾角界限 | 同一跟踪入口 |
| TrackFrameNS | AxisTilt、倾角、FrameMinPhi/MaxPhi | 同一跟踪入口 |
| TrackFrameEW | phi、FrameMinTilt/MaxTilt | 同一跟踪入口 |

角度为度，Pitch、CollectorWidth、InactiveBandWidth和CellWidth保存为米；GUI可能以厘米显示CellWidth。类型及角度以所属字段为准，残留Comment不是当前配置。不得把配对Dome记录扁平化按字段名去重，也不将TDomeOrientGroup当作3D路径引用。

Dome的NoOrient=1/2、NoPairOrient=2/1及IsMainOrient区分两面，系统须为两面提供实际PV归属；未分配第二面会在原生检查停止。它不表示类型不受支持，也不应通过改FieldType或删除配对绕过。

普通、季节、棚架及跟踪类型已有原生全年基线；新增控件和MCP消费仍按各指南的模板与时段范围，不把既有工程可运行当成所有类型可新建。跟踪报告中的no Shadings标签不排除内建二维互遮挡；参见[导出解释](native-tracking-controls.md#原生二维遮挡与导出)。原生新增字段、显示名、缓存和窗口状态都不自动扩大固定无阴影写入范围。
