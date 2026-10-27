# 准备源工程，不先跑全年

`pvsyst_prepare_design_project(spec, dry_run=True)` 复用统一设计入口的发现、声明包络、容量配置、天气与受保护文件编辑链，停在工程准备结束。它不启动GUI、不生成SFI/CSV/PDF、不调用全年仿真。`pvsyst_simulate_design`和`pvsyst_simulate_template`仍按各自入口执行仿真。

## 输入与使用

输入复用`SingleDesignRequest`，详见[单方案输入](single-design-inputs.md)。源范围是单子阵、单普通固定平面、无活动场景或布局的Grid工程；不是直接编辑已带场景的住宅。完整明文PAN/OND、组件身份、站点、气象、角度和明确容量依据应先准备好，再交给[住宅任务](native-house-task.md)。

- `capacity`与`electrical_design`二选一。前者要求`voltage_standard=IEC/UL`、`design_basis`及设计数值；请求模型允许空值不代表业务允许缺项。
- 设计温度、厂家限值、系数和气候极值由调用者提供，不自动补齐。
- `weather.mode=existing`使用明确SIT/MET；`csv/open_meteo`复用天气入口和LegalTime约定。单年最低温不作为现场设计极值。
- `weather_manifest_name`可绑定来源审查；没有清单的existing天气只执行名称与日历检查。
- 未指定模板时可使用标准模板；显式模板不被替换。未指定字段、曲线、模式和损失继续继承并在配置摘要中披露。
- `report_pages`验证词表，但准备入口不生成报告；最终运行时选择实际报告页。

同一spec先预览并检查`issues/warnings/preview`，再明确设置`dry_run=false`。返回`prepared_project.project/variant/year/site/met/manifest`。后续住宅任务使用精确project/variant作为源，并指定新的住宅工程名及完整house/layout，无需预先运行一次无阴影全年。来源文本到住宅任务的组合入口见[来源住宅任务](sourced-house-task.md)。

## 状态

| 状态 | 解释 |
|---|---|
| `blocked` | 缺项、冲突、超范围或其他前置问题 |
| `ready` | 现有源资产的只读准备预检通过 |
| `ready_for_weather` | 天气或内存种子仍是计划，`execution_preflight_complete=false` |
| `prepared` | 工程配置与结构读回完成，尚未仿真 |
| `needs_review` | 可能已准备，但来源或天气复核项仍在；查看`prepared` |
| `failed` | 中途失败，保留文件、备份和事务供检查及恢复 |

`simulated=false`、`native_consumption_verified=false`、`metrics=null`始终保持。`prepared=true`不证明软件已加载工程，也不代表工程认证。

## 文件与恢复

准备清单位于Results的`<stem>_prepare_<year>.json`，与全年`_annual_`清单分开。它记录事务、恢复步骤、工作文件、配置继承和`preparation_context`中的请求、模型、声明包络与复核状态。已有目标工程会停止；不覆盖源文件、不自动回滚或重试。

准备不消耗仿真次数；TRIAL剩余额度为零本身不阻止准备。版本、许可信息及需要的天气命令仍由原入口查询，不改变全年入口的额度检查。

`artifacts.planned`只表示计划路径。预览不建目录或备份；新天气尚未生成时，不读取虚构文件继续配置。

恢复按逆序整文件事务与新鲜状态条件执行，不保证断电原子性或任意中断点自动恢复。workspace RLock仅协调当前进程；外部编辑和多个服务需串行管理。准备链支持CLI8.1.6；后续住宅会话另受中文8.1.6rev49278、DPI96和一／两区的范围约束。PDF摘录和来源引用由调用方提供，不自动认证厂家或现场资料。
