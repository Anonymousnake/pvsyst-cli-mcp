# 季节朝向与远方地平线的 `.VCx` 内部格式笔记

要通过 MCP 工具修改季节性倾角或远方地平线，请直接参考[季节倾角配置](seasonal-orientation.md)与[地平线轮廓配置](horizon-profile.md)。本文档记录这两块配置在 PVsyst 8.1.6 `.VCx` 文件里的底层序列化细节。

## 1. 季节性倾角（`SeasonalTilt`）

在 PVsyst 中，季节性可调支架并不是建两个固定平面，而是直接把 `TOrientGroup` 下直属 `pvOrient` 的 `FieldType` 改成 `SeasonalTilt`：

| 场景 | `.VCx` 内的关键字段 |
|---|---|
| 常规固定 15° 倾角 | `FieldType=FixedPlane`, `FieldTilt=15.0`（无 `SummerTilt`/`WinterTilt`/`WinterMonths`） |
| 夏 10° / 冬 30° 季节可调 | `FieldType=SeasonalTilt`, `FieldTilt=0.0`, `SummerTilt=10.0`, `WinterTilt=30.0` |
| 冬季设为 1、2、3、10、11、12 月 | `WinterMonths=$0E07`（bit 0..2 对应 1..3 月，bit 9..11 对应 10..12 月） |
| 在上例基础上把 4 月也划入冬季 | `WinterMonths=$0E0F`（bit 3 置位） |

注意：`SeasonalTilt` 下 PVsyst 固定把 `FieldTilt` 写成 `0.0`，读取参数时应看 `SummerTilt`、`WinterTilt` 和 `WinterMonths`。

## 2. 远方地平线（`pvHorizon`）

远方地平线遮挡（Far Shadings / Horizon）在 `.VCx` 中作为根部 `pvVCalcul` 的内嵌块 `PVObject_Horizon=pvHorizon` 存储（与近阴影 3D 场景 `pvShading` 相互独立），哪怕最初是从外部 `.HOR` 文件导入的，点集也会被完整拷贝内嵌到 `.VCx` 中：
- 曲线块 `HorizonLine=TCubicProfile` 固定使用 100 个点槽（`NPtsMax=100`，有效点数记在 `NPtsEff`，`LastCompile=$81`，`Mode=1`）。
- 手工新建点集时 `pvHorizon.Flags=$01`、`FracAlbedo=1.000`，此时不含三个散射/反照率积分因子。
- 当经过 GUI 保存或导入 `.HOR` 计算后，`pvHorizon.Flags` 变为 `$03`，并多出 `FHHDiffus`、`FHPDiffus`、`FHPAlbedo` 三个派生因子。如果后续通过 `horizon_updates` 改动了任何一个点的坐标，工具会把 `Flags` 重置回 `$01` 并删掉这三个旧因子，让 PVsyst 按新轮廓重算。
