# 原生朝向整窗与年度消费

## 范围与状态

本阶段使用 PVsyst GUI/CLI 8.1.6，在隔离工程 `ORIENTATION_CONTROLS.PRJ` 中完成朝向整窗批量保存与原生年度运行。覆盖教程第 3 章中的 14 个可选择类型，以及无限水平轴跟踪的四组控制。三个下拉分组标题不是类型；Dome 一次选择生成 `Dome`、`DomeBackfaces` 两条配对记录，因此可选类型数不等于序列化 `FieldType` 字面值数。

**本 R31 阶段交付入口证据，当时不新增跟踪或 Dome MCP 配置工具。** 后续 R38 已交付[现存 UnlimTracking 模板的受限五控制入口](native-tracking-controls.md)，不扩展为全部类型的新建或几何编辑。既有固定平面、[季节性倾角](seasonal-orientation.md)及[地平线](horizon-profile.md)入口保持原有适用域。通过 `pvsyst_run_simulation` 运行 GUI 保存的方案，本身仍不等于该类型配置工具已验收。

GUI 在同一会话中新增保存 **18 份方案 VCF–VCW**，另有既有基线 VCD 与先前保存的跟踪默认 VCE。年度批次运行 VCD、VCF–VCW，共 **19 次：18 成功、1 次因原生分配前提未满足而失败**；VCE 未列入该年度批次。原始工程及变体保留，GUI 正常关闭，临时工作区的 INI DataPath 已恢复。

## 持久化与依赖

以下是原生序列化记录，不据缩进推断软件内存对象归属，也不增加通用解析器 closing alias。

| GUI 索引 | 新保存变体 | 原生类型 | 已观察的结构区别 |
|---|---|---|---|
| 1 | VCF | `FixedPlane` | `FieldTilt`、`FieldAzim`、`BaseSlope` |
| 2 | VCG | `Dome` + `DomeBackfaces` | 两条 `pvDomeOrient` / `TDomeOrientGroup` 配对记录 |
| 3 | VCH | `SeasonalTilt` | `SummerTilt`、`WinterTilt`、`WinterMonths` |
| 4 | VCI | `Sheds` | 固定平面角度与棚架几何 |
| 5 | VCJ | `SunShields` | 本次保存与 Sheds 的键集相同，类型不同 |
| 7 | VCK | `UnlimTracking` | 轴方位、phi 界限、回溯、辐照优化、棚架几何 |
| 8 | VCL | `TrackHorizNSAxis` | phi 界限、`CentralGap`、`PitchSlope`、风收拢字段 |
| 9 | VCM | `TrackTiltedAxis` | 相比 VCL 另有 `AxisTilt` |
| 10 | VCN | `TrackVertAxis` | `FieldTilt`、轴方位、方位角界限 |
| 11 | VCO | `TrackSunShield` | 倾角界限、回溯与棚架几何 |
| 12 | VCP | `TrackHorizAxis` | 倾角界限、`PitchSlope`、回溯与棚架几何 |
| 14 | VCQ | `TrackTwoAxis` | 方位角和倾角两组界限 |
| 15 | VCR | `TrackFrameNS` | 轴倾角、倾角及 `FrameMinPhi` / `FrameMaxPhi` |
| 16 | VCS | `TrackFrameEW` | phi 及 `FrameMinTilt` / `FrameMaxTilt` |

18 份新保存中，17 份声明一个 `TOrientGroup`，仅 VCG 声明两个。VCG 的 `NoOrient=1/2` 与 `NoPairOrient=2/1` 互指，`IsMainOrient=True/False` 区分两面；`TDomeOrientGroup` 是配对朝向记录类型，不是 3D 场景引用。不得将两条记录扁平化为按字段名去重的字典。

本批保存未新增 `ShdPartialScene`，18 份的 `pvShading` 块均与无阴影基线相同。这只描述本批输入，不是软件所有类型的阴影行为结论。

无限水平轴默认保存为 `AxisAzim=0.0`、`MinPhi=-60.0`、`MaxPhi=60.0`、`UsesBacktracking=False`、`UsesIrradOptimiz=False`。GUI 标签确认前三者单位为度；`Pitch`、`CollectorWidth`、两侧 `InactiveBandWidth` 为米；原生 `CellWidth=0.156` 对应 GUI 15.60 cm。该单位对应不包含任何外部物理计算。其它类型字段、`BTNoTrackers` 语义与任意输入范围未由本阶段验收。

四组独立控制均基于无限水平轴默认方案，除方案标题外，仅改变下列原生字段：

| 变体 | 控制 | 原生保存变化 |
|---|---|---|
| VCT | 回溯 | `UsesBacktracking=False` → `True` |
| VCU | 旋转界限 | `MinPhi/MaxPhi=-60/+60` → `-45/+45` |
| VCV | 轴方位 | `AxisAzim=0.0` → `20.0` |
| VCW | 辐照优化 | `UsesIrradOptimiz=False` → `True` |

原生 VCL 下拉通知需要将控件 ID 限定到 `WM_COMMAND` 的低 16 位，且类型切换会重建窗口句柄；仅改变下拉显示文本不足以证明模型改变。最终批次由保存记录、原生报告和年度结果交叉确认，不依靠显示名称判型。部分原生朝向 Comment 仍保留旧固定平面描述，不能代替 `FieldType` 与运行报告。

## 原生年度结果

所有年度调用显式指定 `2022.01.01` 至 `2022.12.31`、小时步长、原生 SFI、SIT 和 MET；不从气象文件名推断年份。18 个成功方案均有连续 **8760 行、16 个有限数值列**及原生 PDF；VCT 为 8 页，其余为 7 页。下表 kWh 为原生小时 `E_Grid` 的求和，不是独立发电模型。

| 变体 | 场景 | 原生年度 E_Grid / kWh |
|---|---|---:|
| VCD | 既有固定基线 | 799980.9366 |
| VCF | 本批固定平面 | 799980.9366 |
| VCG | Dome 配对 | 未运行：朝向 #2 未分配 PV array |
| VCH | 季节性倾角 | 813920.9656 |
| VCI | Unlimited sheds | 796746.4739 |
| VCJ | Unlimited sun-shields | 575152.5244 |
| VCK | 无限水平轴默认 | 921203.9905 |
| VCL | 水平 N-S 轴 | 981560.3128 |
| VCM | 倾斜轴 | 997719.4490 |
| VCN | 竖直轴 | 925771.6989 |
| VCO | 跟踪遮阳板 | 837704.9209 |
| VCP | E-W 水平轴 | 848347.7680 |
| VCQ | 双轴 | 1023410.8075 |
| VCR | N-S 框架 | 1012363.7572 |
| VCS | E-W 框架 | 1021161.4167 |
| VCT | 无限水平轴回溯 | 920170.4515 |
| VCU | 无限水平轴 ±45° | 922712.4176 |
| VCV | 无限水平轴方位 20° | 915497.2943 |
| VCW | 无限水平轴辐照优化 | 926284.7837 |

VCD 的小时表头、单位和全部数据与前一详细损失批次基线完全一致；VCF 的全部小时列也与 VCD 一致。四组跟踪控制均改变原生小时结果，且未在适配器计算跟踪角、辐照或回溯。

跟踪报告披露 `Tracking algorithm`；普通控制显示 `Astronomic calculation`，VCW 明确显示 `Irradiance optimization`，VCT 披露 `Backtracking activated`。这些报告同时显示 `Near Shadings / no Shadings`，但该标签不足以判断原生二维互遮挡是否活动；R31 未单独导出遮挡量，不再以该标签将场景称为“无阴影”。后续 [R38 原生全年 GlobShd 与损失图对照](native-tracking-controls.md#原生二维遮挡与导出失败裁决) 已在无显式 3D 场景的 UnlimTracking 中观测到辐照遮挡，且双开关有单变量隔离对照。电气效果 True、独立 3D/Module Layout 与其它类型的任意几何路径仍未由该证据验证。

原生 Dome 失败信息为 `You have defined an orientation #2 ... but no PV array has been attributed to it !`。这证明本方案缺少第二朝向的阵列分配，不证明 Dome 类型不受支持。后续需在原生系统入口完成配对分配，再作独立运行；不以删除第二条记录或放宽校验绕过此前提。

## 证据与后续

本机证据保留于隔离 case `C:\Harness\workspace\pvsyst-tutorial-grid-study\energy-native-r8`，不随仓库上传原始模型、天气、商业程序或实验文件：

| 证据 | 用途 |
|---|---|
| `orientation-save-batch-r31.json`、`orientation-save-batch-review-r31.json` | 18 份新保存、原变体保留及独立复核 |
| `orientation-native-types-r31.json`、`orientation-type-matrix-r31.md` | 下拉类型及实际保存结构；早期草稿的计数错误以本文和运行裁决为准 |
| `orientation-native-r31.json` | 19 次公共 MCP 调用参数、原生诊断、18 组小时/PDF摘要和 Dome 原始失败 |
| `orientation-consumption-review-r31.json` | 基线完整小时表一致、跟踪控制消费与恢复复核；当时“无阴影”口径按上述 R38 遮挡观测收窄为无显式 3D 场景 |
| `orientation-workspace-r31.json` | GUI 正常关闭、INI DataPath 恢复 |
| `PVsyst8.1_Data\Results\ORIENTATION_CONTROLS_<VC>_2022_R31.csv/.pdf` | 原生小时结果与完整报告 |
| `ORIENTATION_CONTROLS_<VC>_2022_R31.txt` | 各成功报告的完整文本提取 |

保存驱动在全部原生保存及保留断言完成后，因末尾向 `json.dumps` 误传 `flush` 而退出 1。原始失败记录保留，独立复核确认 18 份文件已保存；未以重复 GUI 操作掩盖该日志错误。年度批次正常结束，Dome 依赖失败另计。

R38 已实现现存 `UnlimTracking` 模板五控制、受限适配器角度域和四种开关组合，新 VC、源保留及原生全年验证见独立文档；不是 R31 已有能力，也不外推原生 GUI 硬范围。Dome 的系统分配、显式 3D、电气效果 True 和其它跟踪类型的任意几何编辑仍待原生正例。全过程保留既有闭环，不增加替代计算或永久验证框架。
