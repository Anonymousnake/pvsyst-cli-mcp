# 原生老化与IAM模型输入

仅支持GUI/CLI8.1.6.49278、单子阵／普通固定平面、无活动阴影或布局、自由地平线、完整电路数量的PV-only Grid。模型编辑与启停均为独立事务，只允许variant_name伴随；不混入P50、温度、PF、限值、辅助、负荷／储能或几何配置。模型计算由PVsyst执行。

## 老化参数

```json
{"ageing_updates":{"simulation_year":10,"annual_module_degradation_percent":0.6}}
```

要求已激活的完整pvAgeing模板，不新建模板或生成随机样本。simulation_year为严格整数1..30，annual_module_degradation_percent范围0..1%、0.01百分点步长，两者必填；范围不是PVsyst完整能力或厂家衰减保证。保修、分配及未请求字段继续继承，不从曲线猜均值。

必需输入只写pvMainArray.NoYearSimul及直属pvAgeing.ModuleDegradFactor，不写DegradLossFactor或DegradMismFactor。读取将模型参数与stored_loss_factor_percent／stored_mismatch_factor_percent分开；缓存值不必等于当前有效损失，实际结果由原生重算。均值为0仍是启用模型的输入，不等于关闭。

## 启停与RMS

ageing_enabled为独立严格布尔，只切换已有模板的main Flags2 bit20；启用写$0010082A，停用写$082A。同值请求不规范化旧十六进制文本，不创建备份。读取、启停和编辑共用完整版本、字段、模式和所属对象检查。

getter的mode=template仅表示识别到模板，以enabled判断是否启用。停用时仍可读模板及参数，但模型编辑停止；不构造缺块或修复未知模板。

ageing_updates可选keep_mc_values严格布尔，以及成对的modules_rms_year_isc_percent／modules_rms_year_voc_percent。RMS范围0..100%、0.01步长，源和候选均检查两位存储精度，避免微小科学计数值下溢为零。保修、分配和保存损失因子不变。

KeepMC只设置原生输入，不保证样本数组持久化或跨CLI复用。RMS输出可随随机过程变化，单次电量不是固定回归值；其新配置消费验证仅覆盖48小时，不外推为全年随机序列等价。

## IAM

```json
{"iam_updates":{"mode":"ashrae","ashrae_b0":0.08}}
```

component模式仅接受mode。ashrae须明确ashrae_b0，范围0..1、0.001增量。只修改所属pvSubArray的组件定义bit5及pvIAM块，不计算余弦、菲涅尔、曲线插值或损失。

源仅接受受支持的组件态：FresnelAR缓存、保留Ashrae缓存、无独立块，或原生Ashrae态。选择ASHRAE时清组件位并写最小Flags/IAMMode/B0_IAM块；缺块时插入唯一直属WiringResistance=TWiringResistance前，缺该锚点的模板停止。选择component只置bit5，保留已有块或无块状态，不生成组件曲线。

读取的subarrays[].iam分别报告effective、stored_model、stored_ashrae_b0；组件位启用时，保留Ashrae块不是有效模型。手动FresnelSmooth、UserProfile、PAN曲线编辑及其他模型不由该入口写入。

## 模板与事务

仅接受所属字段和唯一直属对象。重复／未知字段、子对象、非有限值或未知ProgressMode停止。FresnelAR Profile只检查结构、数量和数值，不求值。源与编码后的候选分别校验，再发布完整结果。

老化编辑要求main Flags2启用值0x10082A；读取／启停另识别0x82A。子阵Flags=$019E1002，老化还要求组件定义Flags2=$20，不扩展老化与自定义IAM耦合。原生End of TAgeing闭合有效，未知或错误闭合停止。预览、同值无操作、备份、恢复与并发保护沿用变体事务。
