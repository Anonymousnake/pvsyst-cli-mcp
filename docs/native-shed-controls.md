# 原生棚架与遮阳板配置

`pvsyst_create_shed_geometry_variant`只从既有原生8.1.6 Sheds或SunShields记录预览／创建新VC。不转换类型，不新建3D、Module Layout或阴影模型。配置的native_consumption_verified始终false，写入与原生运行分开。

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC2","geometry":{"pitch_m":6.5,"field_tilt_deg":15.1},"dry_run":true}
```

## 输入

至少给一个geometry字段，未请求值继承。角度为度，长度为米，factor为保存比例，不从几何换算硬件或电气分组。

| 公共键 | 原生字段 | 范围／精度 |
|---|---|---|
| field_tilt_deg | FieldTilt | 0..90，0.1 |
| field_azimuth_deg | FieldAzim | -180..180，0.1 |
| pitch_m | Pitch | 正数，0.001 |
| collector_width_m | CollectorWidth | 正数，0.001 |
| inactive_band_width_1_m/2_m | InactiveBandWidth1/2 | 非负，0.001，合计小于CollectorWidth |
| cell_width_m | CellWidth | 正数，0.001 |
| shading_factor | FracShadingFactor | 0..1，0.001 |
| shed_count | NSheds | 严格整数1..2147483647 |
| module_columns | NModInWidth | 严格整数1..2147483647 |
| electrical_effect | UsesElectricalEffect | 严格布尔，原记录必须已有该字段 |

这些是适配器域，不是GUI极限或工程建议。源和候选都检查精度与范围，超精度、微小非零和Decimal量化溢出停止，不舍入。二维自遮挡及电气效果仅由原生软件计算。

## 完整模板

root8.1.6／Flags=0，Grid system Flags=$06000000，main Flags=$01900400／Flags2=$082A，subarray8.1.6／$019E1002／$20。须单子阵、单朝向1、全部组串归属该方向、电路计数完整，无负荷、储能、P50、地平线、显式场景或Module Layout。

两种类型都要求完整唯一的20字段直属pvOrient：NoOrient=1、Flags=$00、三个Aver=False、BTNoTrackers=-1,-1,。源值完整验证，坏源不被更新掩盖；编码候选再次验证类型和请求值。End of TOrientGroup仅在专用解析视图适配，不修改全局别名。

## 文件与效果

预览不写，执行独占创建一个新VC，沿用锁、受控路径、同名冲突、源快照、显示名和明确BOM规则。硬件、天气、未请求记录及源PRJ/VC保持。失败披露可能的部分产物，不自动删除。读取返回原始11字段，可从新VC继续创建修改。

支持的消费控制包含48小时配置对照，不代表所有几何、电气和字段组合均经过全年验证。开关启用不保证任意场景产生非零电量差，按原生结果及报告审查；不将输出解释为工程认证。
