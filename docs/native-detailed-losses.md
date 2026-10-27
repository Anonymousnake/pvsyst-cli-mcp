# 原生详细损失参数

PVsyst计算损失、热、IAM和老化；适配器只配置受支持原生字段。下面两类事务要求8.1.6 PV-only Grid、单普通固定平面／子阵、自由地平线、完整电路及受支持main/subarray标志。可伴随variant_name，不混入其他物理更新、P50、温度、PF、限值、活动AC、老化、负荷／储能。未知、重复或非所属字段停止。

## 组串电压失配

`pvsyst_update_variant_parameters.string_mismatch_updates`接受mode=constant及loss_percent，或仅mode=default。常数范围0.01..100%、0.01百分点增量；不是完整GUI输入域。默认模式不填猜测数字，subarrays[].string_mismatch.loss_percent为null。

只修改所属pvSubArray.Flags bit17和PMismString，按原生PMismUFixe后的字段位置保存。不修改模块失配PMismMPP、组件电参数、缓存或其他位。组串电压失配不是模块失配，也不等于详细研究工具。

## 辅助profile

原有constant／none见[辅助功率](native-auxiliary-power.md)。profile必须明确给五个量：

```json
{"auxiliary_updates":{"mode":"profile","operating_kw":0,"night_kw":0,"operating_threshold_kw":0,"proportional_w_per_kw":10,"proportional_threshold_kw":5}}
```

仅支持两族：运行常数>0且运行阈值>0，其余三值为0；或比例>0、比例阈值≥0，常数／运行阈值／夜间为0。混合、带夜间及全零profile停止；全零使用原constant模式。

两功率为0.01kW精度，阈值与比例为0.1原生单位增量。Const/Night保存W；CThresh/PThresh保存原生kW数值，Pout为W/kW，不将所有字段统一乘1000。源若属于未支持混合族也停止，不用新请求掩盖异常源。配置具备原生全年消费对照，仍不是自动辅助负荷设计。

## 其他损失与依赖

- UFactor、UVFactor属于pvMainArray。Uc支持不等于完整热模式支持；Uv仍缺独立非零风速正向消费控制，不从零风速下输出不变认定字段无效。
- DC涉及子阵RSCablage、默认位及电路CableDefinition，AC/MV/HV还可能改变多层owner。使用[DC](native-dc-cabling.md)、[AC](native-ac-cabling.md)、[MV](native-mv-night.md)各自入口；不以单标量或铭牌反算原生电阻、参考功率、损失分数。
- IAM原生块可以保存模型和TCubicProfile缓存；现有MCP仅支持[component／ASHRAE](native-model-inputs.md)，不生成任意Fresnel／自定义曲线或编辑PAN。
- 老化年数和损失因子属于main，ModuleDegradFactor属于嵌套pvAgeing。End of TAgeing为已识别闭合，不扩成任意T类别名。保存因子不必是当前有效损失，模型输入由原生重算；RMS／KeepMC和模板启停按[模型入口](native-model-inputs.md)限制。
- PV系统不可用时段与WeakGrid停电不是同一对象；仅能读取保存状态不表示随机生成已支持。光谱页依赖PW/RH等气象数据，不补造数据或以可见系数当启用证明。

原生GUI可能改变缓存、文本精度或模式；实际持久化、原生消费和MCP写入域分开判断。预览、同值无操作、冲突检查、备份与恢复沿用变体事务。
