# 原生负荷与储能控制

## 验证范围与状态

本批使用 PVsyst GUI/CLI 8.1.6，在独立 `STORAGE_LOAD_CONTROLS.PRJ` 中集中访问用户需求、家庭电器、储能策略和系统定义窗口。所有发电、负荷实现、概率实现、充放电和 SOC 均由 PVsyst 执行；本批没有新增计算模型或生产配置 API。

原生运行统一覆盖 2022-01-01 00:00 至 2022-12-31 23:00。8 组均产生 8760 个连续小时、有限数值和原生 PDF，共 61 页。批次脚本记录 7 组限定检查通过、1 组概率范围检查失败；这不是 7 项完整配置能力验收。另做全表/PDF复核，保留概率失败，不通过调整断言或改写输入制造通过。

无负荷 VCD 的日期及 14 个共有数值列与此前朝向批次 VCD 全年逐点相同。源 PRJ/VC 在原生运行前后字节相同；本批 GUI 使用克隆项目，不覆盖源项目。受管 GUI 正常退出，原始 INI 与完整备份逐字节相同。首次 GUI 会话曾提前退出，原因未定；重试会话的正常关闭与恢复不解释首次退出原因。

## 用户需求持久化与消费

实际入口为项目中的自用电按钮、`TUserNeedsForm`，家庭电器继续进入 `TDomesticUseForm`。通用页共有七种负荷选项，含无自用电和已有小时 CSV 路径，不将可见选项数量当作已支持 API 数量。

下列 PV-only 样本的 `pvSystem.Flags=$06000000` 保持不变。有负荷时根级 **pvVCalcul** 的 `Flags=$00080000` 与 `pvLoad` 同时保存；不是 `pvSystem` 的 bit19。保存不刷新旧 `DateCalcul`、`VersionSimul` 和仿真缓存，消费结论必须来自新运行。

| 方案 | 原生负荷对象 | 全年消费检查 | 年用电量 kWh | PDF |
|---|---|---|---:|---:|
| VCD | 无 `pvLoad`，根 Flags=$00 | 无电池、无负荷基线；共有列同前批 | 不导出 E_User | 7 页 |
| VCE | Constant；Flags=$00；ConstantLoad=100 | E_User 的 8760 项均为 100 kW | 876000 | 7 页 |
| VCF | Monthly；Flags=$01；UsesModul=Yearly | 每小时对应当月 50、60、…、160 kW | 922080 | 7 页 |
| VCI | HouseHold；Flags=$0200；UsesModul=Yearly | 365 天均为原生 GUI 显示的 24.5 kWh/day | 8942.5 | 8 页 |
| VCJ | DailyProfile；Flags=$02；NHourlyValues=1 | 每天 00–23 点均对应 50、55、…、165 kW | 941700 | 8 页 |
| VCK | Probability；Flags=$02；PowerStep=10 | GUI 分档语义与 CLI 不符，未通过 | 1819027.112，仅记录实际输出 | 8 页 |

五个有负荷报告的 Used Energy 均与对应 CSV 年度求和在报告舍入精度内一致。表中每种模式尚无新的专用 MCP 定义入口；既有小时 CSV 准备/绑定入口不变。

### 格式、单位与原生提交

- 常数直接保存 `Energy Units=kW` 和 `ConstantLoad=100`。月度也保存该单位字符串，但 `ValMonth` 为 15 项：例如一月 37200、十二月 119040，末尾三个 0.000；本次原生输出证明其对应输入的月均功率。不能把 `ValMonth` 当作十二个原样 kW 标量直接写入，完整单位/历法域仍需限定。
- 日曲线保存 `ValHour_1` 的 24 个小时值及尾项 2580，带尾逗号。本次逐小时消费证明 24 项的小时顺序；尾项数值等于前 24 项之和，但不据此宣称其内部校验语义。
- 家庭电器保存嵌套 `HouseHoldUses=THouseHoldUses`、年度周期及不连续电器编号 1/9。本例为十盏 20 W 灯和 1000 W 待机。原生时钟五次半小时点选最终显示 2.5 h，而不是初始输入的 5 h；`HoursDefined=0,15466496,` 和 `NValperHour=2` 原样保留，不自行生成位图或电器负荷曲线。全年实际小时负荷为 1.0/1.1 kW。
- DailyProfile 与 Probability 的 Flags 都是 $02，模式判别须读取 `LoadType`。已有 HourlyFile 的 $4000 与上述模式分开，不能按位号猜模式或直接复用其缓存布局。
- 旧 VCG/VCH 两次保存仅改了 Comment，未写入负荷对象。复验时逐字段 click 取焦、输入后失焦，VCJ/VCK 才实际落盘。旧文件和失败现场保留；控件文字改变、点击确定或另存成功均不足以证明提交。

### 无电池自用与结果口径

VCE 不含电池或储能策略，新原生报告明确 Fixed constant load 100 kW、876 MWh/Year。原生年度量为太阳能供负荷 `E_Solar=357897.4666`、电网供负荷 `EFrGrid=518102.5331`、上网 `E_Grid=442083.5329` kWh，报告分别显示 357897、518103、442084 kWh。8760 小时负荷侧观测平衡最大舍入残差 0.0005 kW。

本例 `EDirUse` 逐点等于 `E_Avail`，全年 799980.9366 kWh，并非本例报告的太阳能供负荷量。读取 `E_Solar` 与报告核对，不把此次相等关系推广成 `EDirUse` 的通用字段定义。未对 Grid consumption 百分比发明分母，未平均小时 PR 作为年 PR。

### 概率模式失败边界

GUI 末态为十二个 10 kW 档、范围 0–120 kW、显示平均 60.4 kW 和 1449 kWh/day。每次输入会触发原生归一化，最终保存概率不是最初计划的整齐 5/10 数组。

CLI 报告确实读取了前十二个已归一化概率，四舍五入后逐档一致，但报告类宽为 36 kW、范围 0–432 kW、Average 5476 kWh/Day。CSV 实际负荷范围 0.0401–395.98 kW，首小时 166.86 kW，超过 GUI 上限。报告用电总量与实际 CSV 相符，只证明输出内部一致，不证明 GUI 语义已消费。

保留全部 25 槽 `ValHour_1`、PowerStep、单位、报告与失败断言。第十三槽 5476 与报告数值相同，不足以证明缓存来源或全部尾槽语义。未通过外部比例修正、重建概率实现或删尾槽补救；概率配置不得进入本阶段已验收 API 子集。

## 储能策略整窗与全年对照

实际打开 `TGridStorageForm`，原生策略依次为无储能、自用电、Power shifting、削峰填谷、离网运行的弱电网。Power shifting 的充电/放电枚举与控件已记录，仅为可见入口，不承诺本阶段实现。

削峰由原生选择 Universal_Adjustable_LI.BTR 并配置电池。实验中输入 800 V/1000 Ah 后软件保存 768 V/1000 Ah、15 串 10 并；保留软件保存值，不自行反算或把实验值当工程选型依据。切到 PeakShaving 时原生保存移除了原 `pvLoad`；以后切回 WeakGrid 仍须重新定义负荷。

两个保存态的共同部分为 `pvSystem.Flags=$17000040`、`PLimGrid=200.0`、`LimGrSeparLoss=False`，以及 `pvGridStorage` 的 `Strategy=PeakShaving`、`ChargingStrategy=PVToGridMin`、初始 SOC 0.6、高/低 SOC 0.95/0.2、PMaxCharge/PMaxDisch 均 200、转换器 Max/Euro 效率 98/96.5%。本次控件和原生报告确认功率单位 kW、SOC 为分数、效率为百分数。

| 方案 | 唯一调度差异（另有 Comment） | 原生充电 EBatCh kWh | 原生放电 EBatDis kWh | E_Grid kWh |
|---|---|---:|---:|---:|
| VCL | DischStrategy=AfterSunset，无小时字段 | 169110.9995 | 159376.9719 | 769995.0920 |
| VCM | DischStrategy=MinMaxHour；HourBegDisch=20；HourEndDisch=6 | 166172.6383 | 156239.9555 | 767303.4819 |

两组均为 8760 小时、28 个数值列、8 页 PDF；每小时 PGridLim=200 kW，E_Grid 不超过 200 kW。真实充放电与 SOC 输出不同，不仅比较年度上网量。用原生导出量检查：

```text
EOutInv + EBatDis = E_Grid + EBatCh + CL_Chrg + CL_InvB + CL_Over + EUnused
```

VCL/VCM 的逐小时最大绝对残差分别为 0.0103/0.0098 kW。EBatDis 峰值 206.23 是电池侧量，不能直接当作 AC 200 kW 放电限值的违反证据。

VCM 的全年放电小时集合为 0、1、6、20、21、22，全部落在观察窗口 0–6 / 20–23 内；这不证明端点开闭规则，也不表示窗口内必放电。VCL 则有 1508 个 GlobInc>0 且 EBatDis>0 的小时，包括首日 07:00/08:00；不将 AfterSunset 简化为“仅 CSV 无日照小时放电”。其精确时序语义、另外两种放电模式及任意参数域尚待验证，MCP 未新增 PeakShaving 策略。

弱电网随机故障页为 `TUnavailMainF`，与 PV 系统不可用分开。输入 0.1%、种子 1 后原生生成一段 5 小时故障，但确认时仍报“未正确定义电网的失效度设置”。保留现场，未保存为可消费方案；不把时段生成成功记为完整持久化或消费通过，不猜该校验失败原因。既有 WeakGrid CSV 与 SOC 证据继续按原文范围成立。

## DC 端口边界与证据

本会话系统定义实际为 `TSystemegridForm`，不是 `TSystemegridDCForm`；访问过的系统/储能页面为 AC 布局。静态运行时资料确认 DC 类及 `TBattInOutMode` 七个名字：bcINUndef、bcOUTUndef、bcInvHybrid、bcInvSolar、bcMVTransfo、bcHVTransfo、bcInjection。名字不证明枚举序数、保存格式或可达拓扑。

限定指令/指针形态扫描未找到 DC 类的外部直接激活引用。间接调用、注册表及未覆盖形态仍可能存在；不由零命中推断该构建没有 DC 能力，不将只有一个 Batterie 子对象视为拓扑只能由两枚举表达。激活链未闭合，混合 DC 与正向网充仍未验收。

本机证据集中于独立 case，不上传商业模板/气象/原生输出：`storage-load-native-r35.json` 记录全部公共 MCP 调用和概率失败，`storage-load-consumption-review-r35.json` 记录 8 组全表/PDF及原 INI 复核，`storage-load-workspace-r34b.json` 记录受管 GUI 正常退出。`load-family-report-adjudication-r35-4cb81.md` 撤回 children 枚举顺序、尾槽校验语义等未经证实推断；DC 报告的绝对零引用/未装配推断同样已被裁决撤回。后续实现优先已证常数、月度和日曲线的限定原生格式，概率、家庭电器任意位图、随机停电和 DC 激活另证。
