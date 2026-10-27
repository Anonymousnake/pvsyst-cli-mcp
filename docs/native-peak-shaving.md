# 原生削峰放电控制

`pvsyst_create_peak_shaving_variant`从已激活AC削峰模板创建新VC，仅编辑DischStrategy及两个小时字段。充电策略、硬件、SOC、效率、功率、限值、PF、负荷和几何继承，不提供外部调度。

```json
{"project":"example.PRJ","source_variant":"VCL","new_variant":"VCN","discharge":{"mode":"hour_window","start_hour":18,"end_hour":8},"variant_name":"Peak Window","dry_run":true}
```

## 模式

| mode | 保存与输入 |
|---|---|
| after_sunset | DischStrategy=AfterSunset，移除小时字段；请求只有mode |
| hour_window | MinMaxHour及HourBegDisch/HourEndDisch；0..23严格整数、两端不同 |

允许同日和跨夜，不交换端点，不转换DST／时区。24、相同端点及非整数语义不在支持域。AfterSunset不是CSV“无日照才放电”；窗口内不保证每小时放电，也不声明通用端点开闭。只改放电输入也可能联动SOC、充电和上网。

## 原生模板

仅8.1.6 PeakShaving/PVToGridMin，条件是模板范围而非工程建议：

- 根Flags=$00080000且无pvLoad，保留此原生态，不套PV-only负荷规则清位或补负荷。
- pvSystem恰含13项所属字段：Grid、Flags=$17000040、BatteryFile=Universal_Adjustable_LI.BTR、PLimGrid=200、LimGrSeparLoss=False、SOHBattBeg=1、BattVoltage_Syst=768、BattCapacity_Syst=1000、TypeTempBatt=1、TempBattRef=20、十二月均20、NBattSerie=15、NBattParall=10。
- pvGridStorage Flags=$00、Strategy=PeakShaving、ChargingStrategy=PVToGridMin，标量满足[储能语义](grid-storage-parameters.md)；有效标量修改后仍可再改时序，不强制原样例SOC／功率值。AfterSunset为14字段，MinMaxHour为16字段。
- 单子阵／普通固定朝向1，main Flags=$01900400／Flags2=$082A，子阵8.1.6／$019E1002／$20。全部组串在该方向，电路完整，无平均、P50、地平线、场景或Module Layout。

源和候选都校验，坏小时、未知／重复／嵌套／无法识别行或非所属字段停止。三个控制字段只在唯一直属pvGridStorage中出现；保留原闭合标签。

## 文件与结果

预览不写；执行使用锁、路径／源检查和独占新文件，大小写同名停止，失败披露部分产物且源保持。非ASCII名需已有BOM或明确add_utf8_bom=true，bom_added披露。配置的native_consumption_verified=false，旧缓存不是新运行证明。

现有系统创建仍限SelfConsumption／WeakGrid；保留策略的标量编辑另可识别受限Peak profile，不伪装成自用策略。此入口不改PLimGrid或PF，原生报告对此profile标为Apparent power，不能据PGridLim的kW表头另行换算。

原生全年消费只覆盖支持的时序。使用完整原生AC流量，含CL_Chrg、CL_InvB、CL_Over和EUnused，保留输出精度残差。EBatDis为电池侧量，不直接与AC额定比较；SOCDischSelf不是Peak的硬备用下限。任意硬件、其他时序、Power shifting、网充及混合DC不由此开放。
