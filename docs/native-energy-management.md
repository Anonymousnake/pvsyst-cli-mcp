# 逆变器温度、PF 与 kVA 原生分组验证

本阶段补齐已有GUI保存状态的原生消费，不新增温度/PF/kVA配置接口。GUI/CLI8.1.6.49278；派生工程 `ENERGY_MANAGEMENT`，440kWp/6台Generic 60kW逆变器，Hawaii 2022原天气；原工程、组件、气象保留。试验温度/PF/限值不是工程建议，也不代表其它OND行为。

## 原生字段与单变量

下列差异均属于 owning `PVObject_System=pvSystem`。其它阵列、模块/逆变器、朝向与电路不变。

| 原生保存状态 | 系统Flags | 保存字段 |
|---|---|---|
| 基线/环境模式恢复/关闭控制 | $00 | 无温度/PF/限值字段 |
| 环境温度+20°C | $4000 | TempShift=20.0、TempFixed=25.0 |
| 固定60°C | $8000 | TempShift=20.0、TempFixed=60.0 |
| 常数滞后PF=0.9 | $00100000 | TanPhi=0.484 |
| PF=0.9、注入点有功250 | $05100000 | TanPhi=0.484、PLimGrid=250.0、LimGrSeparLoss=True |
| PF=0.9、注入点视在250 | $07100000 | 同上，只有bit25不同 |

两个温度字段可同时存在，实际选择由模式位决定；环境恢复时GUI移除两字段。不能仅看TempShift或TempFixed存在就判断使能。PF不是直接保存Cos(phi)，本记录不拟合或自行重算TanPhi；月度、leading、额定功率强制模式未验收。

## 原生消费

先跑八个状态各2022-01-01至01-02的48h，再跑基线/PF/有功250/视在250四个8760h全年，共12次新原生运行。每份均输出原生PDF及逐时CSV，全部有限、时间严格递增且小时连续。

- 温度偏移状态的InvTemp在48h每一点比基线高20°C；固定模式48点全部60°C。相对基线E_Grid单点最大差分别12.78/39.32kW，InvPmax最大差26.28/60kW；GlobInc/GlobEff不变，未借模块热Uc模拟逆变器温度。
- 环境恢复及PF/限值关闭两个48h控制，全部共同16数值列与基线逐点一致。
- PF报告明确Cos(phi) lagging=0.900，逆变器额定从报告中的kWac切到kVA，并出现“Inverter PNom limit defined as apparent power”。InvPmax原生列由360变324.04kW；不把这个列误当每个小时E_Grid严格上界。

| 2022原生状态 | 数值列数 | E_Grid最大值kW | 并网能量kWh |
|---|---:|---:|---:|
| 基线 | 16 | 352.11 | 799980.9366 |
| PF=0.9 | 18 | 324.23 | 778505.9826 |
| PF=0.9、有功250kWac | 21 | 250.00 | 693021.4026 |
| PF=0.9、视在250kVA | 21 | 225.03 | 650958.2226 |

能量为原生小时E_Grid观测汇总，非外部发电/无功模型。四份全年PDF都是7页；参数页明确Active power/250kWac与Apparent power/250kVA及注入点位置，不能把同一个PLimGrid=250都解释为有功250kW。

## 导出与单位边界

指定模型代理实际加载逆向技能，从PVsystCLI.exe的ASCII枚举表定位 `viInvTemp`、`viInvPmax`、`viEReGrid`、`viEApGrid`；它们在宽字符串旧清单中没有vi标识，不根据列名臆造。由原生SFI实际接受并导出后，才升级为导出已证。viPhiAng/viPhiInFr语义未证，未作为PF输出使用。

InvTemp实际单位°C，InvPmax/E_Grid实际kW。PF开启时EReGrid/EApGrid出现，两者原生CSV表头都写kVAh，本阶段原样披露，不擅改为kvarh或推导其它统计保证。视在限值状态的PGridLim仍输出250、表头仍kW，但EApGrid最大250、E_Grid最大225.03；所以不能只凭PGridLim列头认定它总是有功上限。已有NoPF限值适配器的窄范围保持，PF/kVA状态先返回待审，不放宽写入门控。

## 剩余范围

温度目前只证明上述模式/48h；PF与有功/视在组合证明本夹具2022全年，不外推全部逆变器、月份或leading模式。尚未接入专用配置接口，P50/P90仍待保存和原生概率报告验证。

储能本轮只补名称线索：PeakShaving、ChargingStrategy/DischStrategy、AfterSunset/FromASpecifiedHour/SpecifiedHours。未绑定持久化或消费，不宣称削峰、网充或混合DC已实现；已有self_consumption/weak_grid策略选择本来已实现，不重复列成缺口。数值型自用率设置的存在本身未证，输出指标不冒充配置项。

记录与商业输入留本机独立案例，公开仓库只提交证据说明。未跑全量发现、旧smoke、哈希测试，无外部逆变器温度/无功/限电或概率计算代码。原GUI工作区未改，本阶段只通过原生CLI和既有MCP run执行保存状态。
