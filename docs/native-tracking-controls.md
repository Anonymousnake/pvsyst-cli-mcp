# 原生跟踪控制

`pvsyst_create_tracking_variant`预览或独占创建新VC，只编辑已存在的原生跟踪类型，不做固定转跟踪、跨类型转换、任意几何、3D或Module Layout。跟踪角、回溯和辐照由PVsyst计算。完整年度任务另见[跟踪模板](tracking-template-flow.md)，其高层范围仅UnlimTracking。

```json
{
  "project":"example.PRJ","source_variant":"VCK","new_variant":"VCX",
  "tracking":{"axis_azimuth_deg":0,"min_rotation_deg":-45,"max_rotation_deg":60,"backtracking":true,"irradiance_optimization":true},
  "variant_name":"Tracking Controls","dry_run":true
}
```

## 输入与类型

至少明确给一个源类型所属控制，未提供值继承。未知字段、混合类型、布尔代替数字、数字字符串和NaN/Inf停止。角度以度表示，CentralGap为米，风速为m/s。公开schema列明各*_deg、central_gap_m、pitch_slope_deg、wind_stow_speed_mps／wind_stow_position_deg及布尔字段。

| 公共旧控制 | 原生字段 | 范围 |
|---|---|---|
| axis_azimuth_deg | AxisAzim | -20..20° |
| min_rotation_deg | MinPhi | -60..-45° |
| max_rotation_deg | MaxPhi | 45..60° |
| backtracking | UsesBacktracking | 严格布尔 |
| irradiance_optimization | UsesIrradOptimiz | 严格布尔 |

既有三角度域保留；角度0.1增量并保存一位小数。其他新增数值仅接受有限0.1精度。所有范围为适配器域，不是已测GUI极限。源及候选的全部min/max对都验序，单边修改使用源另一侧；不交换坏界限、静默舍入或将微小值归零，Decimal溢出作为输入问题。

| FieldType | 可编辑的所属原生字段 |
|---|---|
| UnlimTracking | AxisAzim、MinPhi/MaxPhi、UsesBacktracking、UsesIrradOptimiz、原有UsesElectricalEffect |
| TrackHorizNSAxis | AxisAzim、MinPhi/MaxPhi、CentralGap、PitchSlope、回溯／辐照优化／风收拢及其速度／位置 |
| TrackTiltedAxis | 上一类型的字段及AxisTilt |
| TrackVertAxis | AxisAzim、FieldTilt、MinAzim/MaxAzim |
| TrackSunShield | AxisAzim、MinTilt/MaxTilt、UsesBacktracking |
| TrackHorizAxis | AxisAzim、MinTilt/MaxTilt、PitchSlope、UsesBacktracking |
| TrackTwoAxis | AxisAzim、MinAzim/MaxAzim、MinTilt/MaxTilt |
| TrackFrameNS | AxisAzim、AxisTilt、MinTilt/MaxTilt、FrameMinPhi/MaxPhi、UsesBacktracking |
| TrackFrameEW | AxisAzim、MinPhi/MaxPhi、FrameMinTilt/MaxTilt、UsesBacktracking |

每类字段集合独立完整且唯一，不合并成可转换类型的大字典。只编辑原记录已拥有的字段，尤其不向其他类型补Unlim电气效应。

## 完整源条件

root8.1.6／Flags=0，Grid system仅Flags=$06000000及SystemType=Grid，main Flags=$01900400／Flags2=$082A，subarray8.1.6／$019E1002／$20。须单子阵、单计数TOrientGroup、直属NoOrient=1／Flags=$00，全部组串在朝向1，NStrOrient1=NStringCh且其他NStrOrient*=0，电路计数完整。

Unlim模板保留23个所属字段及Pitch=6.600m、CollectorWidth=3.000m、两侧InactiveBandWidth=0.020m、CellWidth=0.156m、NSheds=10、NModInWidth=3、FracShadingFactor=1.000，三个Aver=False、BTNoTrackers=-1,-1,。它们是模板限制，不是工程建议；Comment不用于判型。

负荷、储能、P50、地平线、显式场景和Module Layout停止。PartialSystemInclObj、平均模式和未请求几何保持。End of TOrientGroup仅在专用解析视图适配，不改文件闭合或全局别名。

## 事务与消费

预览不写；执行以锁、受控路径、同名冲突检查、源快照及独占创建写新VC。失败披露可能的部分产物，不自动删除。仅请求字段、可选根标题和明确BOM可变；非ASCII名称需已有BOM或add_utf8_bom=true，bom_added披露实际变化。

配置的native_consumption_verified始终false。DateCalcul、VersionSimul和旧结果不是新输入已计算的证明；用run_simulation及匹配SIT/MET核对实际CSV/PDF。原有Unlim控制有全年消费，九类型新增控制对照限48h；风收拢触发、任意几何／GUI边界和所有回溯组合不由此获得全年保证。

## 原生二维遮挡与导出

无显式3D场景不等于没有原生二维互遮挡；Near Shadings/no Shadings标签也不证明GlobShd应为零。SFI使用vi*标识，CSV使用实际无vi列名；请求列可能被原生省略，不补列或填零。Unlim电气态可导出ShdElec，不能将它扩展为通用电气阴影或Module Layout能力。回溯／辐照优化不保证在所有天气几何下增加发电。
