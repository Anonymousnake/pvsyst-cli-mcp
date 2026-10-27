# 原生季节倾角写入

`pvsyst_update_variant_parameters.seasonal_updates`将受支持固定平面切换为季节平面，或更新已有季节平面。适配器只保存原生参数，按月选角、辐照、温度和能量由PVsyst计算。

```json
{
  "project": "SEASONAL_PLANE.PRJ",
  "variant": "VC1",
  "expected_sha256": "<当前检查返回值>",
  "seasonal_updates": {
    "summer_tilt": 10,
    "winter_tilt": 30,
    "winter_months": [1, 2, 3, 10, 11, 12],
    "orientation_name": "SEASONAL_S10_W30"
  }
}
```

实际调用使用当前读取返回的并发令牌。夏冬角均须明确给出，范围0..90°、0.1°步长。winter_months为1..11个不重复的JSON整数，值在1..12，其余月份由原生季节模式处理；顺序不影响mask。不支持全年只有单一季节，不优化角度或按地理位置猜默认值。

orientation_name只修改所属平面的Comment，不是变体标题；省略时继承名称。可同批设置variant_name。非ASCII平面名需要明确UTF-8 BOM或既有显式BOM命名事务，不隐式改变全文件解码。

## 保存与支持范围

成套保存FieldType=SeasonalTilt、FieldTilt=0.0、SummerTilt、WinterTilt、WinterMonths。固定模式BaseSlope=0随切换删除；非零坡度或已有季节态携带BaseSlope时停止。FieldAzim、NoOrient、三个Uses*Aver、BTNoTrackers、电气分配、损失和天气保持。FieldTilt=0不是夏冬有效角度。

仅支持8.1.6 PV-only Grid、单子阵／单平面、全部组串在朝向1、完整电路计数、受支持main标志、平面Flags=0且非平均、无活动几何／布局、负荷／储能，自由地平线和system Flags=$00。跟踪、Dome、坡地、平均平面及辅助／PF／限值等耦合停止，不覆盖未知状态。

沿用预览、同值无操作、冲突检查、备份与恢复，其他参数另开事务。这不是固定模式回切器；需要恢复原平面时使用事务返回的备份及当前令牌。不重算几何映射或自行生成阴影表。
