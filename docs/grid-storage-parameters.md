# 原生AC光储系统与参数

PVsyst执行电池、转换器、SOC与策略计算。`pvsyst_create_grid_storage_variant`编辑已有激活系统，`pvsyst_create_grid_storage_system`从PV-only Grid创建集中AC耦合新VC；二者不是混合DC端口、BMS或网充控制器。

## 已有系统标量

源为完整UTF-8 VC，声明8.1.0/8.1.6，一个Grid system、现有BTR、正整数电池串并联、激活位和唯一直属平坦pvGridStorage／Flags=$00。惰性或未知模板不自动激活。未请求字段继承。

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","updates":{"strategy":"self_consumption","soc_min":0.2,"soc_max":0.8,"initial_soc":0.4,"max_charge_ac_kw":1,"max_discharge_ac_kw":1},"dry_run":true}
```

| 输入 | 原生字段／单位 |
|---|---|
| soc_min／soc_max | SOCDischDecl／SOCChDecl，0..1硬低／充电高限 |
| initial_soc | BattSoCInit，0..1 |
| reserve_soc | SOCDischSelf，弱电网可用时自用阈值，不是发电机电压 |
| max_charge_ac_kw | PMaxCharge，充电器AC输入kW |
| max_discharge_ac_kw | PMaxDisch，电池逆变器AC输出kW |
| charger_max/euro_efficiency_pct | BattRectMaxEffic／BattRectEuroEffic，% |
| battery_inverter_max/euro_efficiency_pct | BattInvMaxEffic／BattInvEuroEffic，% |

min<max，初值／备用值位于区间，功率／效率为正，效率≤100且Euro≤Max。严格有限数值，不接受布尔、字符串或未知键。数值合法不保证原生曲线有效，例如100/100效率仍可能被原生判为未定义，接口不拟合曲线绕过。

## 原策略标量闭环

省略strategy才保留源的已支持三元组。显式self_consumption同步SelfConsumption/PVAvailable/Immediate；WeakGrid必须本次完整availability。不声明策略且候选三元组与实际源相同，才允许保留受限PeakShaving/PVToGridMin的AfterSunset或MinMaxHour，不存在调用方豁免标记。

源／候选都先验证SOC、效率、版本、owner、字段集合及有限精度，坏源不被更新掩盖。原生小时0..23严格整数且两端不同。编码后重新解析候选并核对实际after。合法标量修改后可继续走[削峰时序](native-peak-shaving.md)，其硬件／PLim200等其他条件仍保留。

读取grid_storage的mode=stored表示保存状态，不是已运行；无对象为null，未知／损坏声明为unsupported及issues。标量与时序交互的原生消费控制限48h，不外推任意电池、温度、全年或全部组合。

## 从PV-only创建

调用者提供完整BTR、串并联、十二个月电池温度、初始循环／静态SOW、完整SOC、AC功率和效率，以及已准备v1负荷清单。以下仅为参数形状，实际硬件与数值须自行声明并复核：

```json
{
  "project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","load_manifest_name":"load_2024_load.json",
  "battery":{"file_name":"declared.BTR","series_count":4,"parallel_count":4,"monthly_temperature_c":[20,20,20,20,20,20,20,20,20,20,20,20],"cycle_sow":1,"static_sow":1},
  "storage":{"strategy":"self_consumption","soc_min":0.2,"soc_max":0.9,"initial_soc":0.5,"reserve_soc":0.2,"max_charge_ac_kw":3,"max_discharge_ac_kw":5,"charger_max_efficiency_pct":97,"charger_euro_efficiency_pct":95,"battery_inverter_max_efficiency_pct":97,"battery_inverter_euro_efficiency_pct":95},
  "dry_run":true
}
```

温度°C、SOW0..1；不推导倍率、电压电流或BMS兼容。已有电池／储能对象应走标量入口，不在构造时替换设备。新系统绑定真实完整负荷缓存并启用根pvVCalcul bit19；它不同于system电池位。仅有电池运行不证明E_User负荷已消费。返回backend=pvsyst_native_ac_coupled。

storage.strategy=weak_grid可直接创建，无需自用中间方案，但须给完整availability，与负荷同年、同固定标准偏移。默认self_consumption不接受额外availability，也不自动切策略。

## WeakGrid可用性与备用

已有或新建WeakGrid每次都需要当前完整CSV，不信任旧缓存。顶层availability示例：

```json
{"csv_name":"grid_2024.csv","year":2024,"utc_offset_hours":8,"time_basis":"local_standard_interval_start"}
```

UserData中的UTF-8分号CSV为Date;Grid Available，单位行;ratio，时间YYYY-MM-DD HH:MM。完整8760/8784小时，仅明确0/1，至少一个停电小时；不补缺、不套原生0.5阈值、不做DST／时移。全可用用SelfConsumption，不添加虚构停电。源负荷年份／时基及根自用位也须匹配，不自动改天气。

绑定所属pvUnavail，DefMode=Periods、HourlyVal=GrdUnav、WRFactor=1000，以有符号base62的+0/+G8保存Day_DDMM每24小时、闰日2902。这里只做原格式绑定，不计算SOC或电池功率。

reserve_soc在电网可用时保留自用阈值，停电可向SOCDischDecl硬低继续放电；不是固定可用kWh、网充条件或发电机阈值。低SOC／备用生效不能证明网充已实现。

## 回送、结果与文件

两个创建入口的allow_grid_export为严格布尔，false设system bit27，true仅清该位，省略/null保留。它不是数值限值或网充；允许回送仍受功率限制、停电和剩余PV约束，不保证必有上网。

使用grid_storage SFI保留实际负荷／未供负荷、SOC ratio、电池／电网流、EUnused、CL_Chrg、CL_InvB及CL_Over。EBatCh+CL_Chrg核对AC充电，EBatDis-CL_InvB核对AC放电；电池侧量不直接当AC额定。完整平衡需含限幅损失、弃电和E_Miss，保留原生导出舍入残差，不外部裁剪或修CSV。

预览零写入，执行独占新VC，不覆盖源PRJ/VC、BTR、负荷／可用性CSV。失败披露产物，配置native_consumption_verified=false；新运行、源绑定和工程适配分别核对。其他BTR／物理体系、GUI完整回存、随机停电、正向网充及混合DC仍按未支持处理。
