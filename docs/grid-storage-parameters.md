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

只接受 `SelfConsumption/PVAvailable/Immediate` 三元策略组合。
配置 `strategy=self_consumption` 同步这三项；不把单一枚举值当成完整可运行策略。

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

AC 平衡检查使用已实际导出的变量：
`E_Avail + EFrGrid + EBatDis - CL_InvB = E_User + E_Grid + EBatCh + CL_Chrg`。
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

## 未完成部分

WeakGrid 枚举确实存在，但仅把 Strategy 改为 WeakGrid 的 C1 原生运行被 Checking variant
以 `The grid Unavailability profile is not well defined` 阻塞。因此暂不开放该策略。
电网可用性逐时 CSV 的 `factor=1/0` 只证明文件语义，未配置子对象前不代表完整策略。
备用容量、可用性剖面、电网充电时段/条件、混合 DC 电池端口
和每端口电压/电流约束是后续独立交付，不用保存 JSON 或解释枚举冒称原生消费。

效率字段采用官方落盘单位编辑，源电池电化学模型和转换曲线仍由原生处理；
当前一组原生结果不认证所有温度/倍率/BTR、硬件/BMS、GUI 回存或其他 CLI 版本。
原生文件与逆向脚本留在独立本机 case，不进入仓库。
