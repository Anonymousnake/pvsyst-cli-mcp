# 原生负荷与储能模式

用户需求、储能策略及DC端口是不同原生入口。负荷、概率、充放电和SOC由PVsyst计算，适配器不生成替代调度。可配置子集见[负荷](storage-loads.md)、[储能参数](grid-storage-parameters.md)及[削峰](native-peak-shaving.md)。

## 用户需求记录

PV-only启用负荷需根pvVCalcul的bit19与pvLoad一致，不能只设置pvSystem位。旧DateCalcul／VersionSimul和缓存不证明新负荷已消费。原生界面可见模式不等于都有MCP创建器。

| 原生LoadType | 单位／结构与范围 |
|---|---|
| Constant | ConstantLoad按kW保存 |
| Monthly | ValMonth为月能量及尾槽，不是十二个原样kW；MCP明确输入月kWh |
| DailyProfile | ValHour_1保存24小时值及尾项；MCP使用24项kW |
| HourlyFile | 独立小时缓存及源绑定；换路径不能代替缓存更新 |
| HouseHold | 嵌套电器／时钟位图，无通用MCP生成器 |
| Probability | 原生归一化及分档依赖未形成可靠配置API |

DailyProfile与Probability均可Flags=$02，必须按LoadType判断，不按位号猜模式；HourlyFile的$4000布局也不直接复用。界面文字、确认和另存成功都不证明字段已提交，应读取保存记录并核对实际E_User。

无电池PV自用时，EDirUse不必等于E_Solar，按实际列和报告解释，不凭单场景等值关系重定义变量，也不平均小时PR当年PR。概率分档可能出现GUI与CLI语义差异；内部CSV/PDF量相符不证明GUI语义正确，不外部缩放或删尾槽来修复。家庭电器时间槽亦不自行生成。

## 储能策略

SelfConsumption、Power shifting、PeakShaving和WeakGrid为不同原生模式。PeakShaving可移除pvLoad但保留根bit19；这种保存态不套PV-only负荷一致性规则。切回需要负荷的模式须重新定义／绑定。

受限Peak profile使用PVToGridMin及AfterSunset／MinMaxHour；后者不保证窗口内必放电或通用端点开闭，前者不是无日照条件判断。软件联动SOC、充放电和上网，EBatDis是电池侧量，AC平衡须包含转换器限制损失和弃电，不能直接套AC额定。

WeakGrid随机故障生成页与PV系统不可用是不同对象；时段生成成功不保证可确认保存或消费。目前完整CSV可用性绑定受到支持，随机故障配置及Power shifting未建立通用API。

## DC端口与范围

AC系统窗口不代表混合逆变器DC布局。DC类名或TBattInOutMode枚举名称不能证明序数、持久化格式、可达拓扑或充电策略，静态未找到直接引用也不能证明功能不存在。混合DC激活链和正向网充尚未形成已支持入口，不以AC构造替代。

模板硬件数量、电压、电池参数及气候条件须由调用者提供和复核。原生读取、保存、运行与工程认证分开，不把保存对象存在当作新配置的消费标志。
