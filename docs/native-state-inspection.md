# 原生保存状态读取

`pvsyst_get_variant_parameters`返回已支持原生记录的保存状态。读取字段、支持编辑和原生实际消费是不同能力；读取本身不运行PVsyst或改变配置。

## 返回内容

| 字段 | 内容与边界 |
|---|---|
| `transformers` | TransformerNode的七项字段、层级深度和嵌套关系；count是序列化层数，不是设备台数，不换算电压、参考功率或损失 |
| `system_unavailability` | pvSystem直属SystemUnavail的模式、目标比例、段数、时长、种子和保存时段；区别于WeakGrid电网可用性，不生成新安排 |
| `peak_shaving` | PeakShaving的AfterSunset或小时窗、功率、SOC和效率字段；按所属对象读取，不计算调度或推断端点包含关系 |
| `shading_scene` | 标志、ListeObjets内的物体计数与类型、保存标量及[PV区域关联](native-scene-bindings.md)；列表之后的分组不计为物体，不认证源几何 |
| `ageing.template` | 模板的11个原生模式、保留／分配／RMS及保修字段；数组保留原文，不计算老化模型 |
| `subarrays[].iam` | 组件缓存、ASHRAE、FresnelSmooth、UserProfile的有效／保存模型；点值不经适配器拟合 |
| `p50_p90` | 受支持root状态中实际存在的14字段块；缺块返回none，不构造概率页或计算概率 |

未知、重复或不完整记录返回issue，缺席与已配置分开。PV不可用列表最多1000段；超限停止返回列表，不以截断内容冒充完整安排。读取受文件大小、版本及记录形状约束。

## 编辑边界

P50写入仍要求原生结果模板`$01800001`，读取其他root状态不放宽该条件。FresnelSmooth和UserProfile点表保持只读；读取不删除或重建用户曲线。其他老化、朝向、线损、储能等修改使用各自配置入口，并执行该入口的版本、所属对象、精度及支持形状检查。

返回的是保存数据，不是运行中有效值、来源认证或新的物理计算结果。
