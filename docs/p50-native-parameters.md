# 原生P50/P90参数编辑

`pvsyst_get_variant_parameters.p50_p90`读取保存参数；`pvsyst_update_variant_parameters(..., p50_p90_updates=...)`编辑既有`P50_P90=TP50_P90`块，可同批设置variant_name。概率分布、标准差合成、分位值及图表由PVsyst计算。

## 输入

十四项原生键全部必填，不提供别名或单位转换。以下数值只演示格式，实际不确定度和气象类型需有依据：

```json
{
  "MeteoKind": "SpecYear",
  "P90Custom": 85,
  "PxxCustom": 97,
  "PxxReport": true,
  "PxxMainResults": true,
  "PlotKind": 1,
  "YearDevFromAver": 2.3,
  "MeteoAnnualVariab": 4.2,
  "PVModelVariab": 2.11,
  "InverterVariab": 0.62,
  "SoilingVariab": 1.23,
  "DegradVariab": 1.34,
  "CustomVariab": 0.45,
  "CustomVarLabel": "Declared uncertainty"
}
```

- MeteoKind只接受SpecYear、TMY。TMY保留显式YearDevFromAver保存值，但不使用年度偏差；声明TMY不将单年天气变成多年气象。
- 分位参数须为整数，满足51 <= P90Custom < PxxCustom <= 99。P90Custom这个字段名不把分位固定为90。
- YearDevFromAver为百分比，范围[-100,100]；六个Variab字段为百分比，范围[0,100]；均为0.001增量。这是适配器范围，不保证科学合理性或覆盖完整GUI输入域。
- PxxReport控制概率报告章节，PxxMainResults控制主要结果中的分位指标，均为严格布尔。PlotKind只支持1，即概率分布；不支持累计图。
- CustomVarLabel为1..80字节的可打印单行ASCII，无首尾空格。普通等号、分号或T_TEST文字按标签内容处理，不解释为原生字段或嵌套类型。

GUI的变率／模型默认勾选会恢复软件默认值，不是排除该项；这些勾选没有独立十四字段标志。接口保存明确数值，不提供自动默认模式或气候估算。

## 模板与事务

只接受8.1.6原生P50结果模板：root Flags数值$01800001，一个根级平坦块及完整十四字段。还须为普通PV-only Grid、单固定平面／子阵、自由地平线、无几何且电路计数完整。

不新建或删除P50块，不设置结果状态位，不改缓存、物理输入、站点、气象或组件。PF、限电、辅助、温度、储能等耦合及同时修改其他物理参数停止。缺失、重复、非所属、嵌套或未知字段停止；ClimateChange尚不支持。

复制十四字段不足以从无结果工程构造完整概率专页，添加状态位或显式请求p50p90页也不属于此入口。只有既有结果模板的受限编辑受到支持；P50报告变化不要求逐时发电量变化。预览、同值无操作、备份、恢复及源冲突保护沿用变体事务。

统计含义参见[PVsyst P50–P90 evaluations](https://www.pvsyst.com/help/project-design/p50-p90-evaluations.html)。
