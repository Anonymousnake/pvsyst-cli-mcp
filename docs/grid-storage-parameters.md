# 已有原生光储系统参数配置

`pvsyst_create_grid_storage_variant` 对一个已经启用的原生并网储能变体进行预览，或创建新 VC。
本阶段是已有模型的受限字段配置，不是从空项目创建混合 DC 端口或电网充电控制器。

## 调用

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "updates": {
    "strategy": "self_consumption",
    "soc_min": 0.2,
    "soc_max": 0.8,
    "initial_soc": 0.4,
    "max_charge_ac_kw": 1,
    "max_discharge_ac_kw": 1
  },
  "dry_run": true
}
```

这些数值是接口示例，不是任何现场、厂家设备或容量的工程默认值。未提供的字段保留原值。

模板必须是完整 UTF-8 文本 VC，声明版本 8.1.0/8.1.6、一个 Grid pvSystem、现有 BTR 文件名、
正整数串并联电池数量和已启用的电池/储能标志，包含唯一平坦 `pvGridStorage`、`Flags=$00`。
存在惰性储能字段但未启用的模板被阻塞；不会自动改系统标志或添加不明设备。

默认保留 `SelfConsumption/PVAvailable/Immediate`；配置 `strategy=self_consumption` 同步这三项。
已有系统编辑支持 `weak_grid`，但必须显式绑定下文完整 availability；
不把单一枚举值当成完整可运行策略。

| 输入 | 原生字段 | 单位/含义 |
|---|---|---|
| soc_min | SOCDischDecl | 0..1 SOC 硬下限 |
| soc_max | SOCChDecl | 0..1 SOC 充电上限 |
| initial_soc | BattSoCInit | 0..1 初始 SOC |
| reserve_soc | SOCDischSelf | 保存的弱电网自用截止阈值；本阶段自消费模式中不作为备用容量使用 |
| max_charge_ac_kw | PMaxCharge | 充电器 AC 输入功率上限，kW |
| max_discharge_ac_kw | PMaxDisch | 电池逆变器 AC 输出功率上限，kW |
| charger_max/euro_efficiency_pct | BattRectMaxEffic/BattRectEuroEffic | %，不是 0..1 |
| battery_inverter_max/euro_efficiency_pct | BattInvMaxEffic/BattInvEuroEffic | %，不是 0..1 |

要求 SOC min < max，初值/保存的自用阈值处于该区间；功率/效率必须为正，效率至多 100%，
Euro 不大于 max。严格数值类型，拒绝布尔、数值字符串和未知参数。
数值检查/字段保存不等于原生效率曲线有效：F10 的 max/Euro 同为 100% 在原生检查时
报 Charger definition: Efficiency profile not defined，失败副本保留，未计为仿真通过。
接口不会自行拟合一条曲线去绕过软件检查。

`ready` 预览不写文件。`dry_run=false` 只创建新 VC，不覆盖现有文件，原 PRJ/VC 保留；
电池型号/串并联数、温度、寿命初值、PV、负荷快照、站点/天气/拓扑和系统标志不变。
写入失败保留公开路径上的新文件供检查，不清理原件。
`native_consumption_verified=false` 表示这次配置本身没有运行仿真。

## 原生证据

本机 CLI 8.1.6 的既有 Grid+集中电池自消费模型，已用隔离新变体完成以下实际对照：

- C1 initial：单独设置 BattSoCInit=0.4，首小时 SOC_Beg=0.4。
- C1 max：单独 SOCChDecl=0.8，48 小时 SOC_End 最大为 0.8。
- C1 charge：PMaxCharge=1，EBatCh+CL_Chrg 最大为 1 kW；电池 DC 端 EBatCh 为 0.9547 kW。
- C1 discharge：PMaxDisch=1，EBatDis-CL_InvB 最大为 1 kW；DC 端放电为 1.0453 kW。
- C2 正式公开 MCP 预览/创建后 48 小时原生运行，首 SOC=0.4，两侧 AC 功率不超过 1 kW；
  原 VC 字节保留，AC 能量流数值残差最大约 1.42e-14 kW。
- C4 与 initial=0.4 的基线相比只改 SOCDischDecl=0.4：前八小时电池放电均为 0，
  电网逐小时补供 2.5 kW，SOC 最低 0.4。没有用独立发电机电压阈值代替 SOC。

上述无弃电/未供负荷/限幅损失的 C1/C2 例采用简化 AC 平衡。
一般原生并网储能复核应使用完整导出项：
`E_Avail + EFrGrid + EBatDis - CL_InvB = E_User - E_Miss + E_Grid + EBatCh + CL_Chrg + EUnused + CL_Over`。
小时输出以 kW 显示，固定一小时数据可积分为 kWh；SOC 输出为 ratio。
C1 高功率数据存在约 0.009 kW 的导出舍入残差，不据此宣布浮点或电池模型错误。

C3 在初始 SOC=0.21 的两组下限 0.2/0.21 对照中均停止夜间放电，未能区分下限效应；
不把该无差异对照称为独立下限消费证明，实际差异来自后续 C4。

## 从纯并网方案建立光储

`pvsyst_create_grid_storage_system` 在已有 PV-only Grid 方案上创建新光储 VC。
调用者必须给出工作区中的完整 BTR 文件、串并联电池数、十二个月电池温度、
初始循环/静态 SOW，以及完整 SOC、AC 功率限制和效率声明；没有工程默认值。
负荷必须来自已准备的 v1 导入记录，当前源/归一化 CSV 仍需逐点匹配。

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "load_manifest_name": "load_2024_load.json",
  "battery": {
    "file_name": "declared.BTR",
    "series_count": 4,
    "parallel_count": 4,
    "monthly_temperature_c": [20,20,20,20,20,20,20,20,20,20,20,20],
    "cycle_sow": 1,
    "static_sow": 1
  },
  "storage": {
    "strategy": "self_consumption",
    "soc_min": 0.2,
    "soc_max": 0.9,
    "initial_soc": 0.5,
    "reserve_soc": 0.2,
    "max_charge_ac_kw": 3,
    "max_discharge_ac_kw": 5,
    "charger_max_efficiency_pct": 97,
    "charger_euro_efficiency_pct": 95,
    "battery_inverter_max_efficiency_pct": 97,
    "battery_inverter_euro_efficiency_pct": 95
  },
  "dry_run": true
}
```

示例为显式实验输入形状，不是厂家或现场推荐配置。电池温度单位为 °C，SOW 为 0..1，
声明的功率只是原生 AC 设备限制，不自动证明电池倍率、电压、电流和 BMS 兼容。
接口只创建新 VC；原 PRJ/VC、BTR、负荷输入与 PV/几何/天气保持。
已有电池或储能对象的变体应走已有配置接口，不在系统创建中悄悄替换设备。
预览和创建均明确返回 `backend=pvsyst_native_ac_coupled`。

同一系统创建入口也支持 `storage.strategy=weak_grid`：给出上例全部电池/储能/负荷参数，
并在顶层增加下文 `availability` 声明。可用性与已准备负荷的年份及固定标准 UTC 偏移
必须一致，至少一个声明停电小时；直接创建一个新 VC，无需先创建自消费中间方案。
预览保持零写入，输入剖面纳入原有写前字节一致性检查；源方案已有可用性对象时
不追加重复对象。默认 `self_consumption` 不接受额外可用性，不被静默改成 WeakGrid。

G1 经真实 MCP 预览/直接创建/`grid_storage` SFI 生成后运行原生 48 小时：负荷逐点差 0，
初始 SOC 0.4，SOC 范围 0.4..0.9，停电日购电/上网均为 0，最大 AC 充电输入 3 kW、
放电输出 2.1352 kW，完整 AC 平衡最大导出残差 0.0007 kW。
这个显式通用电池/PV 配置产生 55.0672 kWh 未供负荷，原生 `E_Miss` 如实导出，
不是隐藏未供负荷后宣称设计满足用电。该例未触及声明 SOC 硬下限，不代替 F5/F7 的备用阈值对照。
纯 PV 源 VC、负荷和可用性 CSV 保留；实际电池/逆变器配置的容量与功率适配仍需原生验收。

系统创建增加 `pvSystem` 电池文件/数量、明确温度与寿命初值和 `pvGridStorage`，
绑定完整负荷快照，并保留其他位地启用已验证的系统标志组合。
**根对象 bit19 (`0x00080000`) 负责自消费负荷启用**，不是电池系统 bit28 的替代品。
D1 只加系统储能字段时电池已充放电，但 `E_User/EFrGrid` 被原生整列省略；
D2 移动对象顺序未改善；D3 仅增加根 bit19 就恢复全部负荷与购电列：
72 小时负荷 270 kWh，最大逐点差 0，充/放电 AC 最大 3/5 kW。
该分支逆向由 `native-hourly-load [cline-pass/cline-pass/deepseek-v4.1-flash]`
取到 `TVCalcul.UsesSelfConsumption`，最终结论由 D3 实际消费证明，不仅依赖反汇编。

D4 通过正式公开 MCP 的负荷准备、系统预览和创建，再进行完整 2024 年原生运行：

- 8784 行，年负荷 33285.6 kWh、逐点差 0，原 PV-only VC 字节保留。
- 初始 SOC 0.5，全年 SOC_End 范围 0.4137..0.9；本例未触及声明下限 0.2，不另称全年下限对照。
- 电池充电 7759.9067 kWh、放电 6375.1715 kWh；电网补供 12452.1823 kWh，上网 36732.4523 kWh。
- 充电器 AC 输入最大 3 kW、电池逆变器 AC 输出最大 5 kW，AC 平衡最大导出残差 0.001 kW。
- 全年未发现负荷未满足却充电、放电同时上网、电网购电同时充电或同时充放电的小时。

D4 使用声明的通用铅酸 12 V/160 Ah、4 串×4 并、固定月温 20 °C、SOW=1 实验配置；
PVsyst 原生电池模型处理温度、倍率与寿命。没有用线性容量×SOC 公式冒充其电化学模型。
年度 CSV/PDF、D1 未消费负荷反例和其他实验产物保留在独立本机 case，未随仓库上传。

## WeakGrid 可用性与备用 SOC

已有储能编辑接口增加可选 `availability`。使用 `updates.strategy=weak_grid` 时必须提供
完整可用性声明，保留系统/PV/电池/负荷原配置，仅在新 VC 绑定 `pvGridStorage` 内
所属 `pvUnavail` 对象；仍由 PVsyst 计算，不新增独立储能/调度模型。

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "updates": {"strategy": "weak_grid", "initial_soc": 0.4, "reserve_soc": 0.5},
  "availability": {
    "csv_name": "grid_2024.csv",
    "year": 2024,
    "utc_offset_hours": 8,
    "time_basis": "local_standard_interval_start"
  },
  "dry_run": true
}
```

CSV 放 UserData，UTF-8、分号分隔，表头 `Date;Grid Available`、单位行 `;ratio`；
日期为 `YYYY-MM-DD HH:MM` 小时起点。完整 8760/8784 小时，只接受明确 0/1，
1 为可用、0 为停电，不补缺测、不做 DST/时移、不推断中间值。
本入口采用严于原生阈值读取的 0/1 输入；原生帮助的 `<=0.5` / `>0.5` 规则不被默默套用。
源负荷快照年份要一致，根对象自消费位已启用；不因此自动修改原方案时区/天气。

至少有一个停电小时：F6 的全年全可用候选被原生 well-defined 校验拦截；
F7 在复核时段之外保留一个声明停电日后通过。全可用场景使用 SelfConsumption，
接口不会偷偷添加一次不存在的停电。每次配置 WeakGrid 都需要当前完整 CSV 重新绑定，
包括编辑已经存在的 WeakGrid 变体；缺输入时不相信旧缓存。

原生序列化采用 `PVObject_GridUnavail=pvUnavail`、`DefMode=Periods`、
`HourlyVal=GrdUnav`、Factor/Aver/Hour/Simul，WRFactor=1000；0/1 分别用有符号 base62
`+0/+G8` 编码为 Day_DDMM 24 小时快照，闰年含 2902。该处理只是软件原格式绑定，
不是自行计算电池功率或 SOC。源 CSV、原 PRJ/VC 保留，预览零写入，创建只新 VC。

`reserve_soc` 对应 SOCDischSelf：电网可用时保留自用阈值，停电时允许继续向
SOCDischDecl 硬下限放电；不是发电机电压阈值，也不直接给出固定可用 kWh。
容量、温度、倍率与实际可用能量仍由原生电池模型计算。

F4 实际原生 48 小时验证第一天停电上网为 0、第二天恢复上网 1174.62 kWh。
F5/F7 用相同初始 SOC=0.4、SOCDischSelf=0.5 对照：停电时夜间持续放电，SOC 降到
0.3578；有电网时前八小时电池不放电、SOC 保持 0.4，电网逐时补供 2.5 kW。
两组均未观察到夜间网充，不能用备用阈值生效冒称网充条件已实现。
F8 正式通过公开 MCP 预览/创建后完成 2024 原生全年 8784 行：负荷逐点差 0，
30 个声明停电小时（元旦 24 小时及闰日 6 小时）购电/上网均为 0；
初 SOC=0.4、全年最低 0.3578、最高 0.95，源 VC/可用性 CSV 保留，PDF 正常生成。
原生 EUnused、EBatLss、E_Solar、SolFrac 也已实际导出。未供负荷为 0，夜间网充为 0。

**F8 能量差异已由缺少的原生输出项解释**：原先只增加 EUnused 时，元旦 11:00
部分小时充满/弃电仍残差 1.5887 kW。F12 只增加 `viCL_Over` 导出、不改任何物理参数或
源 CSV，该小时原生 `CL_Over=1.5901 kW`，完整平衡残差降至 -0.0014 kW，48 小时最大 0.009 kW。
F13 再完成全年 8784 小时：计入 `CL_Over` 后最大残差 0.0097 kW，全部原有 29 个数值列
相对 F8 逐点差为 0，30 个停电小时购电/上网仍为 0；源 VC/可用性输入保持，PDF 正常生成。
这是软件自身导出的限幅损失项，不是效率等效折算、CSV 补值或独立模型。
原生列使用六位有效数字；全年有符号累计残差 -4.0786 kWh 为导出精度下的复核结果，
不宣称内部浮点每小时精确零或厂商硬件认证。

原生分段代码把两相位 `PLoss_ConvLim` 按各自时长累计，经 `VarStorage` 写入变量代码 0xF3；
该代码由本版输出侧 `TVarIdent` 完整枚举绑定为 `viCL_Over`（范围 0..361），
与输入侧可用性标识枚举分别核对。先前栈局部偏移/相邻字段误归属的逆向推论均已撤回，
本结论以正确元数据和 F12/F13 实际导出为依据。

使用 `pvsyst_build_sfi(filename="storage.sfi", variables="grid_storage")` 可创建完整储能导出定义。
预设包含旧 `all_common` PV 列以及负荷/未供负荷、SOC、电池/电网功率、`EUnused`、
`CL_Chrg`、`CL_InvB`、`CL_Over` 等原生变量；默认旧 `all_common` 不变，已有文件不覆盖。
这些数据仍由 `pvsyst_run_simulation` 的原生 SFI 执行导出，没有新增储能计算后端。

F9 初始 SOC 降至硬下限 0.2 的有电网夜间对照仍不充电，SOC 保持 0.2、逐时电网补供 2.5 kW；
备用阈值生效或低 SOC 本身不作为当前策略网充条件已完成的证据。

F1 日志级别错误拦在 CLI 适配器，F2 空对象/F3 周期候选仍缺有效剖面，
这些失败产物保留；它们未计入接口验收通过。

## 未完成部分

原生电网充电时段/条件、混合 DC 电池端口和每端口电压/电流约束仍待取得实际入口；
不采用软件帮助的效率等效折算作为 DC 端口功能，也不编写近似独立计算替代。
本版本已验证的是集中 AC 耦合原生架构，未取得的混合入口保留为待办。

效率字段采用官方落盘单位编辑，源电池电化学模型和转换曲线仍由原生处理；
当前一组原生结果不认证所有温度/倍率/BTR、硬件/BMS、GUI 回存或其他 CLI 版本。
原生文件与逆向脚本留在独立本机 case，不进入仓库。
