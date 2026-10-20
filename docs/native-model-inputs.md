# 原生老化与 IAM 模型输入

GUI/CLI8.1.6.49278，单子阵、普通固定平面、无活动阴影/布局、自由地平线、完整电路数量的PV-only并网配置。模型与启停各为独立事务，仅`variant_name`可伴随；不与其它物理参数、P50、温度、PF/限值、辅助、负荷/储能或几何配置混用。原R30证据与R51新增控制分别记载，不把新域外推到其它接口。

## 老化模型

```json
{
  "ageing_updates": {
    "simulation_year": 10,
    "annual_module_degradation_percent": 0.6
  }
}
```

模型编辑要求已有激活的原生`pvAgeing`模板及已验模式/字段；不创建模板或自行生成随机样本。R51开放已有模板的独立启停，以及`keep_mc_values`和成对RMS原生输入，见末节。适配器域为严格整数第1..30年、年均值0..1%、0.01百分点步长，均显式给出；它不是PVsyst完整范围或厂家衰减保证。保留原保修曲线、分配参数及其它字段，不从曲线或工程常识推断均值。

必需输入仅写`pvMainArray.NoYearSimul`和直属`pvAgeing.ModuleDegradFactor`；R51可选字段仍限原生输入，不写`DegradLossFactor`、`DegradMismFactor`或计算年度损失。root `ageing`把模型参数与`stored_loss_factor_percent`/`stored_mismatch_factor_percent`分开，后两项只是保存元数据，未必是本次有效损失。原生仿真重算，实际值从原生结果/报告读取。

独立年度对照已证明：保留旧缓存1.80、只改模型至第10年/.60，8760×16列与GUI参考完全一致；只改缓存到5.70而保留原第5年/.40，仍匹配第5年参考。不能以要求源文件回写来判断内核是否重算。零均值是仍激活模型的真实输入，**不是关闭**：原生报告0%/year、Module Degradation Loss为0.00%，但该夹具的800008.3461kWh并非停用模型的799980.9366kWh；差异成因未臆测。另已跑第30年/1%端点，原生输出574096.1551kWh。

## IAM 模式

```json
{"iam_updates":{"mode":"ashrae","ashrae_b0":0.08}}
```

```json
{"iam_updates":{"mode":"component"}}
```

`ashrae` 必须显式给出系数，适配器域0..1、0.001增量；`component` 仅接受模式。只针对 owning `pvSubArray` 的原生组件定义bit5与 `pvIAM` 块。source限已验三类组件态（FresnelAR缓存、保留Ashrae缓存、无独立块）或原生Ashrae态；未验菲涅尔手动、UserProfile、PAN组件编辑、其它模型保持待验。

选择ASHRAE时清组件位、写原生最小Flags/IAMMode/B0_IAM块；缺块时按已证原生位置放在唯一直属 `WiringResistance=TWiringResistance` 前。当前模板必须有这个原生锚点；无该锚点的其它模板未开放。选择组件定义时只置bit5，保留现有独立块或无块状态，不为它生成/移植组件曲线。检查接口在 `subarrays[].iam` 分开报告 `effective`、`stored_model`、`stored_ashrae_b0`：组件位为1时，保留的Ashrae块不是有效模型。

组件默认恢复两候选（保留旧块/移除块）均有8760×16列及7页PDF全页匹配组件默认的证据；切换只用“置位并保留”这条最小路径。ASHRAE.05/.08及0/1端点均由原生内核运行，端点报告bo0.00/1.00，对应815060.1823/553575.9500kWh。没有计算余弦、菲涅尔、插值、曲线编译值或损失。

## 来源与事务

只认 owning 模型字段与唯一直属对象；年数、均值、保存量和IAM特有字段的重复/非owning记录、未知模型字段/子对象、非有限数和未知模板模式均在写入前停止。FresnelAR曲线仅复用现有Profile解析器检查字段/计数/数值格式，不求值；模型以外未触及的字段保留。源和目标分别校验，先在候选副本构造完整结果再提交；getter与before/after形状一致。已有预览、无操作、备份、精确恢复与生产并发保护复用。

老化模型编辑只接受主阵列Flags2启用值0x10082A；R51启停和读取还识别停用值0x82A，完整模板共享校验。其它旧接口仍保持原门禁。模型入口要求已验子阵Flags=$019E1002，老化另要求组件定义Flags2=$20，避免未验老化+自定义IAM耦合。原生对象`End of TAgeing`配对兼容，错误/未知闭合仍停止。

## 验收证据

公开stdio MCP验收：8个完整文件正例（包含组件默认保留缓存/无块、两种模型端点），预览不写、无操作不建备份；70个无效类型/值/模板/非owning/未知标志/版本/计数/P50/负荷/混合事务输入保持原字节，另8个FresnelAR根字段/光学字段、Ashrae未知子对象、无块外来字段、未知曲线字段及重复标志反例保持原字节，共78个；正式备份恢复精确。getter与更新after两向闭合，合法显示名P50_P90=Research不被误当块。三个公开配置各跑2022全年，8760连续小时、16数值列、单位及时间戳与独立原生参考完全一致；各7页PDF仅归一显式身份及报告日期后全页一致。十一项既有接口预览及实际无操作均保持输入且不建备份，原UTF-16 DataPath仍恢复。原工程、原组件和天气保持。

本机证据：`ageing-native-probe-r29.json`、`ageing-range-native-r29.json`（保留错误零均值=停用断言）、`ageing-range-native-review-r29.json`、`iam-native-probe-r29.json`、`iam-range-native-r30.json`、`model-pdf-review-r29.json`、`model-controls-public-r30.json`。整体入口图见[原生地图](native-entry-map.md)，前一组标量/辅助与整窗研究见[详细损失](native-detailed-losses.md)。重启长驻MCP进程以刷新新增可选参数；原生仿真无需外部模型。

本机补充验收记录为 `model-owner-check-r30.json`、`model-controls-shared-r30.json`。未执行旧smoke、全量测试发现或哈希类探针，生产并发标记仍按公共契约使用；无新增永久测试依赖。

默认MCP工作区无覆盖新增14个文件：`MODEL_INPUT_CONFIG`工程、VC5/D/N/I/X/Y、SFI及三对年度CSV/PDF。全新进程读取第10年/.60同时保存量仍1.80，老化与组件默认两类无操作均不改；默认工作区再跑8760h/16列/7页PDF，与部署参考完全一致（只归一报告日期/运行时间）。记录 `model-controls-deployment-r30.json`、`model-controls-default-r30.json`，说明有效结果来自原生内核重算而非写入伪造缓存。

## 原生启停与RMS：R51

`ageing_enabled`为独立严格布尔事务，只切换已存在模板主阵列Flags2的bit20。读、启停和模型编辑经同一完整模板读取器，先校验版本/owner/字段集合/ProgressMode等再改候选；字段名集合完整并不足以激活。源位允许同值十六进制大小写/补零，改变状态时写原生`$0010082A`或`$082A`；幂等不规范化旧文本、不建备份。

getter的`mode=template`表示识别出模板，不等于启用；以新增`enabled`布尔判定。停用仍可读11个template字段与原模型输入，再启用后可继续编辑。停用时模型编辑停止，不构造缺失模板或抢救未知模板。完整PV-only/版本/单固定面/counts/无几何/自由地平线/无P50共同门禁不被False分支绕过。

`ageing_updates`仍必需年数与均值，可选`keep_mc_values`严格布尔；`modules_rms_year_isc_percent`和`modules_rms_year_voc_percent`必须成对，适配器域0..100%、0.01步长。保存为两位小数，源也要求记录中的两位令牌，避免科学计数极小值先下溢成0。保修列表、分配模式和stored factors不改。KeepMC只是设置原生输入字段，**未承诺样本数组持久化或跨CLI进程复用**。

一条长期公开MCP契约覆盖停用/幂等/回读/重启用/RMS/再编辑、ProgressMode=2停止且不写、十六进制同值格式与停用时编辑停止；用`PVSYST_CONTRACT_WORKSPACE`显式提供原生文本夹具，复制至系统临时目录，非可执行CLI占位。初轮夹具写入因Windows换行翻倍失败，保留环境/输入变换复演记录后修正为精确字节写入，同目标通过。37次临时公开边界调用补验版本/源模式/未知位/缺模板/类型/互斥/数值/原字节保持。

原生8.1.6公共新配置六组2022-01-01..02，每组48连续小时、16数值列、7页PDF、无CLI警告；第10年/均值0.60，未把48小时外推全年：

| 本次原生输入 | 并网kWh（单次观测） |
|---|---:|
| 启用，RMS 0/0，KeepMC false | 4294.086 |
| 停用，同一模板 | 4534.800 |
| ISC RMS 1.50，VOC 0 | 3874.439 |
| ISC 0，VOC RMS 1.20 | 4282.718 |
| 两项RMS 1.50/1.20，KeepMC false | 3845.857 |
| 两项RMS 1.50/1.20，KeepMC true | 3838.554 |

非零RMS两态各用相同输入复跑一次，结果分别3844.351/3847.717kWh，各有22个数值小时变化。因此上述随机输出不是精确回归金值，也不把False/True两次不同结果当作单开关因果。新配置的原生随机路径已见，缓存/样本数组复用未证，不以“输出相同/不同”替代缓存证据。记录`native-migration-r51/native-ageing-48h.json`与`native-rms-repeat.json`，公共37次及夹具首错单独保留；源工程/原配置保留。独立审查通过，未运行全量/旧smoke/哈希探针，无替代衰减/MC模型。
