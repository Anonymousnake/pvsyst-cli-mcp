# 准备源工程，不先跑全年

`pvsyst_prepare_design_project(spec, dry_run=True)` 复用统一设计入口的发现、声明包络、容量配置、天气与受保护文件编辑链，停在工程准备结束。它不启动 GUI、不生成 SFI/CSV/PDF、不调用全年仿真，也不把配置文件落盘表述为原生发电消费。既有 `pvsyst_simulate_design` / `pvsyst_simulate_template` 默认全年行为保持。

## 输入与使用

输入直接复用 `SingleDesignRequest`，详见[单方案输入](single-design-inputs.md)。仍是单子阵、单普通固定平面、无活动场景/布局的 Grid 源工程；不是直接配置已带场景的住宅。完整明文 PAN/OND、组件身份、站点/气象、倾角/方位角和显式容量依据先准备好，再交给[住宅任务](native-house-task.md)。

- `capacity` 与 `electrical_design` 二选一；前者仍要求 `voltage_standard=IEC/UL`、`design_basis` 及全部设计数值，Pydantic 可空不代表业务允许省略。
- 沿用已验收的包络和容量工具，不加入新的电气/发电算法。没有资料的设计温度、厂家限值、系数或气候极值不自动补齐。
- `weather.mode=existing` 使用明确 SIT/MET；`csv/open_meteo` 复用已有天气入口和 LegalTime 约定，不把单年最小温度当现场设计极值。
- `weather_manifest_name` 可绑定既有来源审查；未给清单的 existing 天气只做原有名称/日历门禁，不新增质量认证。
- 不指定模板时保留原标准模板 fallback；显式指定的模板不被替换。未指定字段、曲线、模式和损失继续继承，并通过配置摘要披露。
- `report_pages` 仍验证词表，但准备入口不生成报告；最终执行者选择实际报告页。

调用顺序为同一 spec 先预览、检查 `issues/warnings/preview`，再明确 `dry_run=false` 准备。返回 `prepared_project.project/variant/year/site/met/manifest`，调用方将精确 project/variant 作为住宅任务源，另给新的住宅工程名和完整 house/layout；无需先执行一次无阴影全年。

## 状态与保持

| 状态 | 解释 |
|---|---|
| `blocked` | 缺项、冲突、超范围或已知前置问题；未取得可用准备结果 |
| `ready` | 当前源资产的准备预检通过；只读且未写工程 |
| `ready_for_weather` | 新天气或仅内存种子还只是计划，`execution_preflight_complete=false`，不冒充完整预检 |
| `prepared` | 工作工程已配置并结构读回通过；尚未运行发电 |
| `needs_review` | 工程可以已准备，但来源/天气复核项仍在；查看 `prepared`，不把该状态升级成正常工程验收 |
| `failed` | 中途失败，文件/备份/事务保持供查看与受保护恢复 |

`simulated=false`、`native_consumption_verified=false`、`metrics=null` 在此入口始终保持。`prepared=true` 只表示配置准备验证，不代表 PVsyst 已加载它，也不等于电气/物理认证。

Results 下使用 `<stem>_prepare_<year>.json`，不同于全年 `_annual_` 命名；无关全年报告不会阻止新准备。清单记录事务、恢复步骤、工作文件、输入守卫、配置继承，以及 `preparation_context` 中的请求、已选模型、声明包络、warnings/review_required。保留既有生产冲突保护；不覆盖原文件、不自动回滚/删除。重复请求已有工程停止，不静默重新准备。

准备没有仿真，故 TRIAL 的剩余仿真次数为零本身不阻止它；版本、许可信息和需要的天气命令仍按原入口查询。该行为不改变原全年入口的额度门禁，也不承诺其它许可证允许什么操作。

`artifacts.planned` 中的未来 SIT/MET/CSV 等是计划路径，不是已生成资产。dry-run 不建目录/备份；新天气尚未存在时，不读取虚构文件继续做工程准备。

恢复仍复用逆序整文件事务与新鲜状态前置条件；不是断电原子任务，不是任意中断点自动恢复。workspace RLock 仅进程内协调，外部编辑/多服务器仍需串行管理。此批准备链本机 CLI8.1.6 已证，后续住宅原生会话仍受中文8.1.6rev49278/DPI96/一或两区等既有边界约束。

## 公开与原生证据

隔离案例保存在本机 `pvsyst-project-input-study/prepare-r1`，源取自已验收的 bootstrap88/93 资料副本，原始商业/气象/论文模型与派生产物不上传仓库。

| 证据 | 已证行为 |
|---|---|
| `public-preparation.json` normal | 公共入口创建 `RESIDENTIAL_INPUT.PRJ/VC0`，读回18串联×2串、源保持；无年度命令/产物 |
| 同文件 quota probe | 临时合成 TRIAL=0 仅验证额度分支；准备成功，旧全年入口预检仍阻；没有修改实际许可 |
| 同文件 review probe | 临时注入 review_required 验证传播；响应和持久清单均保留 `needs_review`，不是实际天气新审查结论 |
| `public-weather-plan.json` | 暂存真实2024逐时 CSV 的只读预览返回 ready_for_weather，全部路径保持，无转换/采集/仿真 |
| `prepared-house-native.json` | 已准备源接现有公共住宅任务，一次自有GUI导入20构件/两区36模块、原生有效性/遮挡表/SaveAs、正常退出后新CLI最终全年；准备未先跑全年 |

后者为2024闰年8784小时、13数值列、8页原生PDF，年并网约36908.6885kWh，最终住宅只有一次原生年运行；源准备工程保持，无残留进程，父恢复原DataPath。新几何是明确声明的验收夹具：既有完整网格x向放大2、z按tan28/tan15变换，两区各3×6模块，不是现场实测房屋。模型为公开论文完整600Wp PAN与原生Generic9kW实验逆变器，包络明确标为integration fixture；不冒充厂家认证或北京设计气候极值。天气沿用已证LegalTime93的2024 SIT/MET，此次未重新获取。

永久非哈希白名单 `tests/contracts/test_prepare_design_project_boundary.py` 含两项公共契约：不可执行CLI上的schema/缺项/严格类型/零写入，以及显式`PVSYST_PREPARATION_FIXTURE`/`PVSYST_PREPARATION_CLI`提供真实完整输入后的隔离准备成功、清单与源保持、无年度产物。第二项只调用真实CLI的原有预检，不产生仿真；缺少显式资料则skip，不算原生验收；失败保留临时工作区，成功清理。此次两项均通过无skip，另住宅/工程打开公开边界通过；必要编译及diff检查通过。无全量发现、旧smoke或哈希类测试；生产输入保护保持。

独立只读复核 `design-project-preparation-review [magpie/mw/deepseek-v4.1-flash-sg]` 无阻断。旧元组对齐不是功能缺陷，不为此扩大补丁；capacity默认值与业务必填边界保留；future-weather公开状态已另由真实CSV预览补证。静态推导和父原生执行分别计证。

## 下一阶段

当前两次公共调用已实证准备→住宅交付，尚非一个包含全部资料提取与源准备恢复的住宅总任务。下一阶段接入来源草案/缺项和准备清单，集中协调已有入口，避免引入新计算器或用新一套任务框架替换已验收住宅检查点。
