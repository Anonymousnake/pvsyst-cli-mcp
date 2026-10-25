# 原生分块模板项目准备

`pvsyst_prepare_block_project` 将一个既有原生方案中的完整子阵作为分块类型，按明确数量准备一个新项目。它复用既有子阵复制规则，不求解串并联、不按目标容量选设备、不生成地形或储能调度。

```json
{
  "source_project": "FIXED_BLOCK_SOURCE.PRJ",
  "source_variant": "VC1",
  "new_project": "FIXED_BLOCK_ASSEMBLY.PRJ",
  "blocks": [
    {"id": "south-unit", "source_subarray_id": 1, "count": 24},
    {"id": "west-unit", "source_subarray_id": 2, "count": 10}
  ],
  "dry_run": true
}
```

`count` 包含源方案已有的那一个分块；以上产生34个子阵。每个源子阵必须恰好对应一项，不支持隐式删除、遗漏或重复来源。标签仅用于交接映射；块内型号、数量、MPPT模式、独占逆变器组和原生固定朝向直接继承。来源可以是不同朝向、不同设备和不同块内数量，但每种源块必须已经完整定义，且全部满足本入口的原生记录边界。最多100种／合计100块是适配器资源限制，不宣称原生所有版本的数量上限。

预览在内存组装并检查结果，不写临时工程或备份。执行只新建PRJ和所选VC，保留原VC标识，不复制其他方案；目标已有任何工程成员时停止。逐块读回验证拥有字段、PAN/OND引用、朝向、完整逆变器组及串／模块数量，发布前再次核对源PRJ/VC。原工程不改写；既有单次子阵工具的备份及冲突行为保持。本入口不产生每块复制的永久备份。

结果包含 `plan`、`mapping`、`source_serialized_counts`、`serialized_counts`、`totals` 和继承告警。映射保留业务标签、源SubArrayId、全部输出SubArrayId、朝向、设备引用及源数量。它只汇总序列化整数，不从历史图表缓存推断DC/AC功率，不读取或认证组件模型。

当前仅接受已记录的8.1.0/8.1.6普通固定平面、完整Grid计数、独占完整逆变器分支。主阵列Flags数值必须为`$00100400`、Flags2为`$082A`，pvSystem为无额外控制的Grid/Flags0。所有源块包括count1都检查。真实TransformerNode、系统限额／辅助控制、负荷／储能／发电机、共享父、活动场景／阴影表／Module Layout及未知配置停止。`MVNbTransfos=1`这类残留默认值本身不作为启用证据；也不会因历史报表字符串而误判物理对象。复杂共用设备不能随着复制擅自保持原额定量或按比例重算。

站点、天气、损失、设备曲线和计量边界仍继承，文件名不证明依赖存在或工程合规。`ready`只表示可发布预览，`prepared`只表示新工程已创建；`native_consumption_verified`、`simulated`、`engineering_certified`始终false。要证明运行，应通过现有`pvsyst_run_simulation`将新PRJ/VC实际交给PVsyst；本入口没有放开单方案设计流或既有工程修订任务的其他门禁。

## 验收记录

本轮只运行明确的公开MCP契约与所选真实原生场景，不执行全量发现、旧smoke、哈希测试或逐函数测试。原生输入、模型、天气和输出留本机；数量组合的例子不表示龙口可研的设备、接线、地形或设计已经确认。

2026-10-25，公开stdio MCP契约 `python -B -m unittest tests.contracts.test_block_project_boundary` 通过（显式本机原生双块fixture）；覆盖2+3→5块、保留源与所选VC、数量／朝向／模型引用、类型／引用／范围错误、count1下启用共用系统控制仍停止、目标已存在停止及无额外备份。生产子阵复制原有冲突／备份逻辑不改为新测试对象。

独立实际场景从完整双朝向原生源 `DUAL_ROOF_INPUT.PRJ/VC1` 准备 `FIXED_BLOCK_34.PRJ/VC1`：24个28°/0°块与10个28°/90°块，共34子阵、612块组件、34串、34独立逆变器／MPPT输入。源是已用公开参数PAN与Generic9kW的验收样例、北京ERA5 2024 SIT/MET；不是龙口550W/300kW或山地农光模型。

PVsystCLI8.1.6独立从磁盘加载新PRJ/VC，一次2024全年输出8784条连续小时记录，导出的5个数值列全部有限。10页原生PDF列出34个子阵，朝向1/2分别24/10，参数总量为612组件、34逆变器（306kWac），总DC显示367kWp（367.2kWp的原生显示取整）。原生EArray为607003.2997kWh，E_Grid为589291.8607kWh；仅属于该验收样例，不是龙口预测。参数汇总页和结果页已渲染目视核对；CLI加载不冒充GUI另存重载。

原双块源另一次2024-06-21至06-22的48h原生控制，E_Grid187.8617kWh；34块年度输出中相同48h为3181.4726kWh。这是实际原生数量响应，不按比例缩放推算、不归因某项损失、不从两朝向聚合值另算PR。源PRJ/VC字节保持，新MCP会话在运行后重新读取34块结构与朝向／电路数量仍一致。prepared响应的原生标志仍为false，外部验收证据不冒充每次调用自动仿真。

本次实测预览4.52秒、正式准备4.64秒，含模型/报告的原生年度MCP调用43.40秒；是当前样例单次观测，不是性能上限或基准回归。未启动GUI、修改INI、跑全量／smoke／哈希测试或新增每块备份。第一次契约因旧direct_fields解析主阵列EndTags而失败，改用既有objects仅读取 owning主阵列字段后通过，未放宽工程标准。失败输入保留 `C:/Users/2557/AppData/Local/Temp/pvsyst-block-contract-if3xfkky`，未清理掩盖。

精确请求／结果、原生PDF/CSV、参数与小时审阅在本机 `C:/Harness/workspace/pvsyst-longkou-feasibility-study/round2/`。
