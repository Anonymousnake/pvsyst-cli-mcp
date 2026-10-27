# 原生地平线轮廓

`pvsyst_update_variant_parameters`的独立`horizon_updates`事务配置远方地平线；`pvsyst_get_variant_parameters.horizon`返回点集、标志及保存的派生字段。它不构建近阴影、3D场景或跟踪器，不计算遮挡、插值或散射。

```json
{
  "project": "HORIZON_PROFILE.PRJ",
  "variant": "VC1",
  "expected_sha256": "<当前检查返回值>",
  "horizon_updates": {
    "mode": "profile",
    "profile_name": "HORIZON_LEVEL20",
    "points": [[-120, 20], [-40, 20], [40, 20], [120, 20]]
  }
}
```

实际调用使用当前读取返回的并发令牌。轮廓须明确提供4..100个`[方位角, 高度]`点，使用PVsyst内部角度和0.1°步长。方位角在-180..180内严格递增、不重复，高度在0..90内；南0°、西正东负。其他方位约定应先通过原生导入确认，不猜North参数或自行旋转。不补端点、不闭合、不插值。

全零轮廓使用仅含`mode=none`的对象，移除工程中的整段远方地平线，但不删除独立HOR文件。profile_name为1..80可打印UTF-8字节、无首尾空格的名称，写Horizon Comment而非根变体标题。可同批设置variant_name；非ASCII名称按既有明确BOM规则处理。

## 保存与支持范围

使用原生`PVObject_Horizon=pvHorizon`和`HorizonLine=TCubicProfile`，NPtsMax=100、实际NPtsEff、LastCompile=$81、Mode=1及100个Point槽。仅接受FracAlbedo=1.000模板；其他反照率状态停止，不拟合或替代工程依据。

新建或改变点集使用Flags=$01，不继承旧FHHDiffus/FHPDiffus/FHPAlbedo，派生量交PVsyst处理。已有Flags=$03须具备完整三因子，读取仅披露保存值。只改名时保留缓存；语义相同请求不写或备份，before/after使用相同语义键。

仅支持8.1.6 PV-only Grid、完整电路、单子阵／普通固定平面、全部组串在朝向1、受支持主阵列标志及system Flags=$00；不接受活动近阴影、布局、负荷／储能、平均／季节／跟踪／多平面或其他系统耦合。未知块、字段、标志、曲线模式、缺失派生因子及非1 FracAlbedo均停止。

预览、并发检查、备份和恢复沿用变体事务；其他参数另开事务。参数写入不等于原生消费或场址认证，后续通过PVsyst重算并审查原生结果。
