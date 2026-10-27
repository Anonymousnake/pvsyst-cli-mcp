# 季节朝向与地平线的原生记录

季节倾角和远方地平线分别使用[季节写入](seasonal-orientation.md)与[地平线写入](horizon-profile.md)。下述为PVsyst8.1.6记录的维护说明，不替代各配置入口的完整支持条件或计算。

## 季节朝向

季节状态位于TOrientGroup中直属pvOrient，不是复制两个固定平面。

| 状态示例 | 原生字段 |
|---|---|
| 固定15° | FieldType=FixedPlane、FieldTilt=15.0，无夏冬字段 |
| 夏10°、冬30° | FieldType=SeasonalTilt、FieldTilt=0.0、SummerTilt=10.0、WinterTilt=30.0 |
| 冬季1/2/3/10/11/12月 | WinterMonths=$0E07 |
| 上述月份再加4月 | WinterMonths=$0E0F |

角度单位为度。读取入口返回实际存在的SummerTilt、WinterTilt、WinterMonths和UsesTiltAver字符串，缺失不填零。季节FieldTilt=0.0不代表全年水平安装，普通固定平面编辑也不因可读取季节字段而放开。

## 远方地平线

pvVCalcul中内嵌PVObject_Horizon=pvHorizon，不是近阴影pvShading，也不只是HOR文件路径引用。原生点集使用HorizonLine=TCubicProfile、NPtsMax=100、实际NPtsEff、LastCompile=$81和Mode=1，并保留100个Point槽。

手工轮廓的Flags=$01及FracAlbedo=1.000不包含三个派生因子；原生HOR保存／导入可产生Flags=$03及FHHDiffus、FHPDiffus、FHPAlbedo。系数由软件产生，不作为适配器默认值或外部插值模型。清除点集移除整个内嵌段，但独立HOR文件可继续存在。

原生导入列表可能包含演示文件，必须明确选择自己的轮廓并核对点集。不同输入方位约定先通过原生导入确认，不猜转换；参数具体范围见[地平线接口](horizon-profile.md)。

## 结果使用

季节月份和地平线由原生内核消费，适配器不计算辐照或遮挡。原生报告的页数及损失图位置可随配置变化，不硬编码固定页码。短时格式等价不证明任意角度、轮廓或全年结果等价，季节与地平线组合也不扩大各自写入入口的耦合范围。
