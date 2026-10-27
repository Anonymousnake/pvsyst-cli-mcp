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
