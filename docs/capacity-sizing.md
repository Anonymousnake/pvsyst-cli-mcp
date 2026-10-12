# 模板容量规划与声明边界校核

## 入口与边界

沿用 `pvsyst_simulate_template`，通过可选 `capacity` 对象开启容量规划；未传入时，保持既有模板数量的全年流程。工具仍为 50 个，生产实现无新增依赖。重启 MCP 服务以加载新的参数 schema。

这是基于调用方明确给出的设计包络、模块系统电压等级和逆变器字段的有限规划，不是原生温度模型复刻、硬件选型或完整工程认证。设计包络指指定最低环境温度与最高/最低组件工作温度下的电压边界，以及包括温度、辐照和适用背面增益在内的组件最大工作/短路电流；它们需由调用方提供来源与方法。读取到的 `muVocSpec`、`muISC` 等原始字段不在这里猜单位或自动换算。

支持结构完整的单子阵列、单固定平面 Grid，无活动阴影、表格或 Module Layout。进一步要求子阵列 Version=8.1.0/8.1.6、独立 `$0400`、无 `$0100`、PowerSharingInverterId=-1，所有字符串归属同一平面；一个独占逆变器组包含一个可重复 MPPT 分支，分支包含一个可重复并联串叶节点。MPPT 每台数量必须等于目标 OND 的 NbMPPT，整台硬件输入全部使用，不推断部分输入功率预算。目标逆变器显式 ModeOper=MPPT、CompPMax=Lim、CompVMax=Lim；异质/优化器字符串声明不参与。

PAN 限单晶/多晶硅，GRef=1000 W/m2、TRef=25 degC。IEC/UL 标准由调用方明确选择，对应 PAN 根 VMaxIEC/VMaxUL；没有额定值就阻止配置。OND 功率在所属 Converter 中按 kW 读取，PAN PNom 按 W，电压/电流按 V/A。`pvsyst_get_component.electrical` 返回数值、单位、所属对象、声明状态及未核实项；`readable` 仅描述对象可读取。IDCMax=0 不是正值声明，更不代替逐 MPPT 的工作/短路电流额定值。

## 请求与计划

基础项目、组件、SIT/MET、倾角、年份、新项目名和 dry_run 同 [全年入口](automatic-simulation.md)。下列 capacity 数值是明确标识的实验输入，展示数据结构，不代表厂商校准数据或推荐工程参数；实际调用使用自己的已验证包络。

```json
{
  "target_dc_kw": 21.6,
  "max_dc_ac_ratio": 1.3,
  "capacity_tolerance_pct": 5,
  "voltage_standard": "IEC",
  "cold_voc_v": 65,
  "hot_vmp_v": 38,
  "cold_vmp_v": 47,
  "module_max_operating_current_a": 16,
  "module_max_short_circuit_current_a": 18,
  "mppt_max_operating_current_a": 32,
  "mppt_max_short_circuit_current_a": 40,
  "design_min_temperature_c": -10,
  "design_min_operating_temperature_c": 20,
  "design_max_operating_temperature_c": 70,
  "design_basis": "明确的实验包络；实际输入应填写数据来源、辐照/温度条件及计算方法"
}
```

温度按最低环境温度 <= 最低组件工作温度 <= 最高组件工作温度排序，范围 -100..150 degC。冷态开路电压位于最低环境温度条件；冷态工作 Vmp 对应最低组件工作温度，热态工作 Vmp 对应最高组件工作温度，不把环境温度等同工作温度。当参考温度在设计范围内，包络须覆盖 PAN 参考 Vmp/Voc；最大电流须覆盖 PAN 参考 Imp/Isc，短路边界须覆盖工作电流边界。来源是一行可打印文本，最多 2000 字符。原项目的 GUI 设计温度设置不在此操作中改写。

只有 `capacity_tolerance_pct` 默认 5%，允许 0..20%；其他设计数据缺失会返回 `capacity.*` 字段问题，不创建副本或消耗仿真执行次数。数值为严格有限数字，不接受布尔值、数字字符串、NaN/Infinity；schema 禁止额外键。预检仍以 dry_run=true 默认只读。

候选数量限定为每串 1..100 模块、每 MPPT 1..100 并联串、1..10000 台逆变器。搜索保持模板每台 MPPT 数量及模式/拓扑；计算完整硬件名义 AC 功率，要求 DC:AC 不超过输入上限。校核条件为：

```text
series * hot_vmp >= inverter.VMppMin
series * cold_vmp <= inverter.VMPPMax
series * cold_voc <= min(inverter.VAbsMax, module.VMaxIEC_or_VMaxUL)
parallel * module_max_operating_current <= mppt_max_operating_current
parallel * module_max_short_circuit_current <= mppt_max_short_circuit_current
DC_power = series * parallel * MPPT_per_inverter * inverter_count * PAN.PNom / 1000
```

优先最小绝对容量偏差，再选较少逆变器、接近原串联数的方案；偏差排序按 1e-9 百分点处理浮点舍入，不是能量/经济最优求解。容量容差比较包含 1e-9 百分点的数值误差余量；电压、电流上限没有隐含工程余量。实际容量、带符号偏差、名义 AC 容量、DC:AC、数量、设计条件、边界数值、搜索范围和未核实项返回在 capacity_plan 中。超出范围或整数布局不支持时明确阻止，不扩大数量、不改拓扑凑解。

改变节点数量仍需既有显式 NElements，未声明的默认 1 可保留但不替它插入字段。保持偏移和可恢复编辑；新模板应本来具有可编辑计数。生产入口不规范化或修补原模板。实验种子使用既有 complete-text 创建入口独立提供显式数量 1。

## 执行、恢复与验收

正式执行沿用副本、来源、组件、原生仿真和任务清单，新增 capacity 阶段。逆变器数、字符串数、串联数按已有后端支持分别提交，每步有 SHA 前置条件、备份、逆序恢复记录；最终实际数量须等于已接受计划。年度 PR 使用配置后的模块数量及根 PNom，不沿用原模板容量。原模板、输入组件和旧输出不修改。

物理连接器/汇流箱、线缆/保护及其他厂商契约限制，原生温度拟合/GUI 设置，包络的来源准确性/气候覆盖和损失参数重拟合保留为 unverified_checks。不把声明边界满足当作全工况工程通过。工作副本可保持配置以供检查，也可按清单完整恢复；恢复不删除历史 CSV/PDF，报告描述生成时的配置，恢复后的项目不是该报告的当前输入快照。

CLI 8.1.6、本地真实 MCP stdio 的验收采用完整公开 600 W PAN 和既有目标 OND，Wanaluwawa SIT/MET、28 度、1990 年，未启动 GUI或分析专有二进制/DLL；实验文件均留在隔离目录，不入仓库。包络是上文显式的测试数据，不归因于厂商。

| 目标 DC | 计划 | AC / DC:AC | E_Grid kWh | IL_Pmax kWh | PR |
|---|---|---|---|---|---|
| 21.6 kWp | 18 模块/串，2 串，每 MPPT 1 串，2 台单 MPPT 逆变器 | 18 kW / 1.20 | 33574.0264 | 11.2553 | 87.5440643% |
| 48 kWp | 16 模块/串，5 串，每 MPPT 1 串，5 台单 MPPT 逆变器 | 45 kW / 1.0666667 | 73476.3959 | 0 | 86.2151597% |

两组均有 8760 条完整小时行；每组独立作者直接从实验种子按唯一字段组装控制输入，不调用生产容量求解/缩放函数。12 个导出变量、每个变量 8760 个样本全部 max_abs_delta=0。两份工作副本通过真实恢复工具逐步恢复，PRJ/VC 与复制时的字节完全一致；受保护原模板和实验种子保持不变。

最终代码另保留 MCP_CAPACITY68_FINAL.PRJ 的 21.6 kWp 配置和报告，与已完成控制仍逐点一致。PDF 显示 33574 kWh/year、PR 87.54%，与小时结果按显示精度一致。实验验证器最初错误使用 Performance Ratio PR 全称，实际标签为 Perf. Ratio PR；仅修正验证器并复核已有文件，没有重跑这份仿真。

进一步用一个四 MPPT 的 50 kW 目标 OND：48 kWp 计划为每串 10 模块、4 个 MPPT 各 2 并联串、1 台逆变器、80 模块，总 AC=50 kW、DC:AC=0.96。真实原生 E_Grid=71119.4042 kWh、IL_Pmax=0、PR=83.4495312%，12 变量/8760 行与独立控制逐点一致，工作副本恢复到完整复制快照。该自定义 OND 的曲线/额定参数依然不是原厂认证模型。

2020 年反例在容量配置后于 simulation 阶段返回原生 Invalid simulation dates，副本、已提交数量事务和原生原因保留；按清单恢复全部 PRJ/VC 至复制状态，原模板和种子未变。没有改用 1990 年代跑，也没有据此推断 MET 的通用日期规则。

本块共八次原生调用，七次全年输出（两组自动/控制、最终代码留存、四 MPPT 自动/控制），一次日期失败；PDF 标签复核未增加执行。实验脚本和阶段记录为隔离目录中的 verify_electrical66.py、verify_capacity67.py、verify_capacity69.py，不成为生产依赖或永久测试资产。

既有仅替换组件、保持一台 9 kW 逆变器的同容量/天气/倾角实验为 E_Grid=26449.5505 kWh、IL_Pmax=7382.7679 kWh、PR=68.9670379%。差异是本实验中扩大逆变器数量后的原生结果，不证明此求解策略在其他设备或气候下最优。

182 项离线回归通过，真实 SDK 生命周期及 capacity 数字类型/未知字段反例通过。仅新增一个容量流程契约测试，复用已有输入：覆盖只读预检、三阶段数量编辑、年度容量/指标、阻止缺项与越限、正式失败和逆序恢复；既有读取契约补对象归属/单位/未声明约束，不固化所有求解内部步骤为独立测试。

公开设计依据：

- [官方组件参数、参考条件与温度系数区分](https://www.pvsyst.com/help/component-database/photovoltaics-modules/pv-modules-main-interface/index.html)
- [官方串联电压校核条件](https://www.pvsyst.com/help/project-design/grid-connected-system-definition/array-voltage-sizing-according-to-inverter.html)
- [官方温度系数与模型结果](https://www.pvsyst.com/help/component-database/photovoltaics-modules/pv-modules-main-interface/pv-modules-additional-data/temperature-coefficients.html)
- [官方逆变器输入参数与电流/契约约束](https://www.pvsyst.com/help/component-database/grid-inverters/grid-inverters-main-interface/grid-inverters-main-parameters/index.html)
