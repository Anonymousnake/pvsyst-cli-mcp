# 原生削峰放电控制

## 入口与模式

`pvsyst_create_peak_shaving_variant` 从已存在、已激活的原生 AC 削峰模板预览或创建一个新 VC。只编辑 `DischStrategy` 和两个可选小时字段，不创建电池，不转换其它储能策略，不改变充电策略、硬件、SOC、效率、功率、并网限值、PF、负荷或几何，也不编写外部调度计算。

```json
{
  "project": "example.PRJ",
  "source_variant": "VCL",
  "new_variant": "VCN",
  "discharge": {
    "mode": "hour_window",
    "start_hour": 18,
    "end_hour": 8
  },
  "variant_name": "Peak Window",
  "dry_run": true
}
```

| mode | 原生字段 | 请求约束 |
|---|---|---|
| after_sunset | DischStrategy=AfterSunset，移除 HourBegDisch/HourEndDisch | 只有 mode，不携带小时字段 |
| hour_window | DischStrategy=MinMaxHour、HourBegDisch、HourEndDisch | start_hour/end_hour 为 0..23 严格整数且不同 |

小时范围是适配器支持域，不是已测 GUI 硬限；不支持 24 或相等端点的未证语义，不接受布尔、数字字符串或小数。允许同日与跨夜顺序，不自行交换、不推算每天的充放电计划，不进行时区或夏令时转换。`AfterSunset` 是原生枚举，不解释为只在 CSV 无日照小时才放电。

## 固定原生子集

本轮限定声明版本 8.1.6 的既有 `PeakShaving/PVToGridMin` 参考 profile；下列数值是已验证模板条件，不是工程限值、自动选型默认值或对任意电池的推荐：

- 根 Flags=$00080000 且无 pvLoad。这是软件切换削峰后实际保存并被消费的状态；保留它，不套用 PV-only 负荷一致性规则去清位或补造负荷。
- pvSystem 为 Grid、Flags=$17000040、BatteryFile=Universal_Adjustable_LI.BTR；PLimGrid=200、LimGrSeparLoss=False；SOHBattBeg=1、BattVoltage_Syst=768、BattCapacity_Syst=1000；TypeTempBatt=1、TempBattRef=20、TempBattMois 为十二个 20；NBattSerie=15、NBattParall=10。完整 13 项 owning 字段各自唯一，不忽略未知字段或嵌套行。
- pvGridStorage 的 Flags=$00、Strategy=PeakShaving、ChargingStrategy=PVToGridMin；原R39样本SOC为0.6/0.95/0.2/0.5、PMaxCharge/PMaxDisch200、两套Max/Euro98/96.5。R55不再把这些标量常数作为不可修改的前提，改为复用已验收储能SOC/功率/效率语义，使有效标量编辑后仍可改时序；硬件、系统标志及PLim200等其它条件保持。AfterSunset仍恰14字段、MinMaxHour恰16字段，详见[跨入口闭环](grid-storage-parameters.md#r55-原策略标量闭环)。
- 单子阵、朝向 1 的普通固定平面，无平均开关；主阵 Flags=$01900400/Flags2=$082A；子阵 Version=8.1.6、Flags=$019E1002、Flags2=$20。NStrOrient1=NStringCh、其它分配为零，并通过原生电路计数检查。
- 无负荷、P50/P90、保存的地平线、显式 3D 场景或 Module Layout；不扩展到其它削峰时序、Power shifting、WeakGrid、网充或混合 DC 端口。

源模式和小时也先校验，坏源不能用新请求悄悄覆盖修复。两个 flat 对象的每个非空内部行必须是自身已识别的 assignment；空格键、无法识别行、嵌套对象、缺失或重复字段都阻断。三个可改字段只能在唯一直接所属的 pvGridStorage 内出现。对象归属不依赖缩进，`End of TOrientGroup` 仅在读取视图适配，写盘保留原标签。

## 事务与兼容

默认 `dry_run=true` 不写文件，返回候选差异；执行只用独占创建写一个新 VC。复用工作区锁、路径校验、源快照复核，大小写同名目标不覆盖。失败保留并披露可能的部分产物，源 PRJ/VC 及其它输入不改。

`variant_name` 只改变新 VC 根名，中文需源已有 UTF-8 BOM 或显式 `add_utf8_bom=true`；实际新增由 `bom_added` 披露。返回 `native_consumption_verified=false`，创建不替代新一次软件运行，旧 DateCalcul/VersionSimul/结果缓存不作消费证据。

既有 `pvsyst_create_grid_storage_variant` 和 `pvsyst_create_grid_storage_system` 的 SelfConsumption/WeakGrid 范围保持不变，不为新入口放开旧 `validate_storage`，也不将 Peak 状态伪装成 SelfConsumption 去绕过校验。这里不修改并网限值或功率因数；原生报告对该 profile 的限值分类是 **Apparent power**，不是新建 Active power 控制。CSV 的 PGridLim 数字与列头按软件原样读取，不做外部视在/有功换算。

## 验证证据

新进程实际列出 63 个工具，Loads & Storage 为 6 个。公共 MCP 白名单 11 个正向检查、71 个无写入负例通过，另验证同名并发仅成功一次、旧储能接口继续阻断本削峰策略。模式双向转换及不变请求的完整文件均逐字节匹配 [R35 原生 GUI 参考](native-load-storage-controls.md)，四组新小时配置也逐字节匹配软件消费过的探针输入。

源小时字段移到模式行之前的正例验证了删除后的索引修正；显式 BOM、中文名、大写 VC、普通 Comment 中的 pvLoad/pvGridStorage 字样均经过检查。未知/未解析/重复/嵌套源内容、错误 owner、只读 profile 偏差、坏小时和路径/类型错误都不写目标。

先运行四组 2022.01.01–01.02 的 48 小时原生探针：0→23、23→0、18→8、9→17；各 48 行、28 数值列、8 页 PDF，最大 AC 平衡残差 0.0073 kW。23→0 样本有放电的小时标签为 0、23；9→17 样本只在 16、17 标签观察到放电。它们证明输入接收与行为变化，不证明窗口内必放电或通用端点开闭，不外推全年。

随后对三个公共创建的新配置独立执行 2022 全年：

| 新变体 / 小时槽 | 原生充电量 kWh | 原生放电量 kWh | 原生上网量 kWh | 最大 AC 残差 kW |
|---|---:|---:|---:|---:|
| VCV / 0→23 | 169111.2954 | 159552.8184 | 770380.1662 | 0.0103 |
| VCX / 18→8 | 168969.5636 | 158954.8914 | 769732.3734 | 0.0097 |
| VCY / 9→17 | 107677.9585 | 101893.0117 | 714706.5112 | 0.0097 |

各有连续有限的 8760 小时、28 数值列、8 页原生 PDF；前 48 小时全部列及单位与独立探针精确一致。Produced Energy 与原生小时 E_Grid 求和在报告精度内一致；每小时 PGridLim=200 且 E_Grid≤200。SOC 为 ratio，三个年度的实测最小值分别 0.2239/0.2392/0.2267、最大值均 0.95，不把 SOCDischSelf=0.5 当成本削峰模式的硬备用下限。

全小时使用原生观测量检查：

```text
EOutInv + EBatDis = E_Grid + EBatCh + CL_Chrg + CL_InvB + CL_Over + EUnused
```

GlobInc/GlobEff/EArray/EOutInv 在本批与原 20→6 参考逐点相同，EBatCh/EBatDis/SOC_Beg/SOC_End/E_Grid 均有变化。**只编辑放电控制，不代表只有放电结果改变**；SOC、充电和上网由软件耦合计算。电池侧 EBatDis 不直接拿来比较 AC 侧额定值，不用简化平衡漏掉转换器限制损失或弃电。

本机记录为 `peak-native-probe-r39.json`、`peak-public-r39.json`、`peak-native-r39.json` 及原生 CSV/PDF；旧失败样本与 R35 的 AfterSunset 有日照放电证据保持原样。没有运行旧 smoke、全量测试发现或自定义哈希检查，既有生产并发保护保留。原 PRJ/VC 及 INI 保持不变，商业模型、天气、原生输出和临时验证代码不进入仓库。常驻 MCP 进程需重启加载新工具/schema。
