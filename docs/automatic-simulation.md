# 模板驱动的全年自动仿真

## 目标与入口

`pvsyst_simulate_template` 将既有的项目复制、来源同步、组件引用替换、倾角编辑、原生仿真和结果验收串成一个有持久恢复记录的任务。AI 提供明确输入后，整个流程不操作 GUI，也不生成猜测的空白物理设计。

单方案输入闭环由 `pvsyst_simulate_design` 提供：它按一份统一 spec 解析精确组件与合格模板、只接受声明式电气来源、规划整数容量、选择天气来源（既有 SIT/MET、暂存标准 CSV 或 Open-Meteo ERA5），再调用本页这套阶段。天气地名先经 `pvsyst_resolve_location` 取候选、由 AI 显式挑一个 `location_id`，年份天气由 `pvsyst_acquire_weather` 获取。输入规则、状态语义与待验收项见 [单方案输入闭环](single-design-inputs.md)。

当前已发布 53 个 MCP 工具，仍按六类组织：站点与天气组 7 个（新增 `pvsyst_resolve_location`、`pvsyst_acquire_weather`），仿真组 4 个（新增 `pvsyst_simulate_design`），其余 50 个工具保持原有名称、参数与已验证记录。另有 2 个已接入但**待 SDK/原生验收**的入口（`pvsyst_review_weather`、`pvsyst_prepare_sourced_design`），目标计数 55；未验收前不当作可用能力。生产实现只使用标准库及既有后端，没有新增第三方依赖（Windows 解析 IANA 时区所需的 `tzdata` 已在 `requirements.txt` 中按平台声明，属于本机 Python 依赖，不是 PVsyst 原生能力变更）。已运行的 MCP 服务需重启读取新工具。

## 输入与支持范围

| 参数 | 含义 |
|---|---|
| template_project | 工作区中现有、完整的 PRJ 模板文件名 |
| variant | 模板内要计算的 VC 标识，例如 VC0 |
| new_project | 尚未存在的新 PRJ 文件名，整个模板项目复制至此前缀 |
| pan_name / ond_name | 工作区或已配置只读库中的完整明文 PAN/OND 文件名 |
| site_name / met_name | 已存在于 Sites/Meteo 的 SIT/MET 文件名 |
| tilt | 严格有限数值，0–90 度；写入唯一固定平面 |
| year | 严格整数，1969–2068；仿真日期为该年 1 月 1 日至 12 月 31 日 |
| dry_run | 严格布尔值，默认 true；显式 false 才创建副本并执行 |
| capacity | 可选的显式设计包络与目标 DC 容量，有限整数规划/校核/分步配置；详见 [容量指南](capacity-sizing.md) |
| weather_manifest_name | 可选；`pvsyst_prepare_weather` 生成的来源清单文件名，在写入前校验资产哈希、年份与 SIT/MET 名称，并启用显式原生加载与逐时天气对照 |

缺少输入时返回对应字段的 issues，不消耗仿真执行次数。日期、倾角和布尔参数不接受数字字符串或布尔值冒充数字。年份限制来自当前两位年份 SFI 时间戳解析契约，不是 PVsyst 原生绝对年份上限。

第一版限定为结构检查完整的单子阵列、单固定平面 Grid 变体，无活动阴影/依赖表/Module Layout，根头部显式 PRCorrTemp=False，PAN GRef=1000 W/m2。不传 capacity 时，原模板的串联数、并联串数、逆变器/MPPT 布局、方位角、损失字段与模式保持；仅选中 VC 的组件引用和倾角改变。传入 capacity 时可在更窄的完整独立 MPPT 模板内规划并修改整数数量，仍保持模式和拓扑，不拟合损失。SIT/MET 来源同步整个工作副本的 PRJ/VC。

不做自动硬件选型、任意拓扑重设计、电压/电流全工况认证、组件内部拟合或能量/经济最优计算。capacity 仅在明确提供设计点和逐 MPPT 限值时校核声明边界并求整数数量；设计包络的准确性、历史气候覆盖和其他工程条件仍需复核。静态组件检查通过不代表物理模型完整有效；应使用用户已验证的完整模型，不把仅含铭牌参数的文本当作已拟合 PAN。加密内置数据库的名称查找与旧二进制 PAN 不在此入口的支持范围。

## 调用与状态

示例文件名需替换为实际已准备的输入；所有文件参数均为文件名，不是任意路径。

```json
{
  "template_project": "Template.PRJ",
  "variant": "VC0",
  "new_project": "RoofAnnual.PRJ",
  "pan_name": "ValidatedModule.PAN",
  "ond_name": "ValidatedInverter.OND",
  "site_name": "Site.SIT",
  "met_name": "Weather.MET",
  "tilt": 28,
  "year": 1990,
  "dry_run": true
}
```

先检查预检响应。执行时仅将 dry_run 设为 false；不会自动重试、替换设备、换天气、填缺测或改年份代跑。预检不是一次原生试运行，也不证明 MET 内部有效期或 SIT/MET 的地理、时区兼容性。

| status | AI 应如何解释 |
|---|---|
| blocked | 输入缺失、格式/范围错误、模板超出支持范围、文件冲突或已知能力/额度问题；修正 issues 后再调用 |
| ready | 只读预检通过，可以尝试执行；不等同电气/物理设计通过 |
| completed | 任务执行且本入口的全年、上下文、单位和指标一致性检查通过 |
| needs_review | 全年输出已产生，但保守异常诊断触发；天气准备/转换保留的警告与超阈值的逐时天气差异同样进入该状态；保留指标/文件/恢复记录，不当作正常设计交付 |
| failed | 配置、原生执行或结果验收失败；检查 stage/error，原模板不由此流程修改 |

MCP 的 is_error=false 不代表任务完成。正常的缺失输入、任务失败和需复核状态通过 status 返回；SDK 参数类型错误仍为工具错误。

预检不写文件、不建备份。实际执行前独占创建 Results 下的新任务清单；输出和 Models 下 SFI 名称由新项目及年份确定。已存在的项目、CSV、PDF、SFI 或清单都会阻止执行，不覆盖旧任务。

## 结果验收

原生 CSV 必须识别所请求的工作项目、选中变体、SIT 和 MET。时间轴必须恰好覆盖指定年的每个整点，从 1 月 1 日 00:00 到 12 月 31 日 23:00，常年 8760 行、闰年 8784 行；不接受短时样本、缺失/重复/倒序时间、额外年份或隐式补行。

E_Grid、EArray、IL_Pmax 和 GlobInc 需要明确单位与有限值。W/kW 按一小时步长积分为 kWh，W/m2 或 kW/m2 积分为 kWh/m2。有辐照时的原生 PR 必须有限。文件读取前后校验摘要，输入、模板与工作文件也核对是否发生外部变化。

标准并网 PR 按官方定义计算：

```text
PR = annual E_Grid [kWh] / (annual GlobInc [kWh/m2] * module-nameplate installed DC power [kWp])
```

模块铭牌功率和 GRef 只读取所属 PAN 根物理对象，嵌套资料同名字段不参与；模块数量来自已核验的模板电路数量。另与原生小时 PR 的辐照加权值交叉检查，容差 0.002 绝对比值，覆盖导出舍入和夜间消费差异。该容差不是 PR 设计合格线。不会把夜间零值纳入简单平均来冒充年度 PR。

返回 grid_energy_kwh、array_energy_kwh、clipping_loss_kwh、incident_irradiation_kwh_m2、nominal_dc_power_kw、pr_ratio/pr_percent、覆盖信息及诊断标记。IL_Pmax 是原生逆变器超额功率损失，不是全部限电或所有逆变器损失。

当全年 IL_Pmax 超过铭牌容量与辐照对应 STC 参考能量的三倍时，触发明确披露的保守复核启发式。它用于拦住明显异常模型结果，不冒充原生物理上限，也不认证其他结果必然正确。PDF 检查原生文件标识；本次验收额外独立读取报告指标，与小时积分按报告显示精度对照，生产入口不依赖 PDF 文本提取库。

传入 weather_manifest_name 时，预检在写任何项目文件之前核对清单内的源 CSV、规范化 CSV、月值 CSV、MEF、SIT 与 MET 哈希，并核对清单状态、年份、时间基准与 SIT/MET 文件名；输入变动即停止，不改写前置条件后继续。原生运行显式加载清单指向的 SIT/MET，而不是只更换项目记录的历史来源文件名；SFI 额外导出 GlobHor、DiffHor 与 T_Amb，与准备好的 CSV 逐时对照，报告样本数、最大绝对差与有符号总差。

0.1 W/m2 与 0.01 degC 是明示的舍入/转换复核阈值，不是对 MET 存储精度的承诺，也不是物理有效性认证。超过阈值，或清单本身为 needs_review（原生转换警告被保留）时，年度状态保持 needs_review；只有清单不带保留警告、且逐时差异全部落在阈值内，年度状态才可能为 completed。对照中出现时间标签、行数或列单位与源 CSV 不一致属于验收失败，不是复核。阈值通过也不等于转换无警告：一个 2020 闰年案例的三列逐时差异全部在阈值内，年度状态仍因保留的原生 32 分钟晴空偏移提示保持 needs_review。天气准备的 prepared 只表示文件已生成，不等同原生 MET 已验证。过程与证据见 [站点、天气与指定年份闭环](weather-preparation.md)。

成功运行接口现在附带已脱敏 diagnostics（尾部最多 12000 字符）及控制台 warning/warn/caution 行。任务 warnings 同时包含适用边界和结果诊断。该提取不宣称覆盖只有原生日志或报告才显示的所有提示。

## 失败保持与恢复

原模板从始至终只读，实际变更仅发生在新工作副本。Results 中的 JSON 清单记录请求、输入快照、各阶段、已完成事务、当前工作文件、报告路径及恢复调用。原生失败或结果验收失败不删除副本、备份、SFI、已生成的报告或清单，也不擅自撤销用户可能想检查的配置。

recovery 列表按逆序给出现有 pvsyst_restore_variant / pvsyst_restore_project_sources 的工具名和参数。只有文件仍处于对应事务状态时，记录的前置条件才有效；若有 GUI/外部修改，先重新检查，不替换前置条件后盲目覆盖。恢复最后回到工作副本刚复制时的完整 PRJ/VC 字节，而不是删除任务或恢复报告内容。

工作区进程内锁与摘要检查不排除外部编辑；任务清单和多阶段事务不是断电原子作业。如果进程被终止在编辑与清单写入之间，应从已保存阶段、后端备份及现存文件检查恢复，不宣称自动恢复任意中断点。不同原生工作区可用于并发；同一进程内单工作区作业串行。

绑定已准备天气的全年仿真若触发 `failed`，项目编辑沿用同一条恢复路径：低辐照案例按逆序的 `pvsyst_restore_variant` 与 `pvsyst_restore_project_sources` 恢复后，工作 PRJ/VC 与模板复制字节相同、且不同于失败配置，失败清单、SFI 与已准备的天气文件保留。独立天气准备不编辑 PRJ/VC；其失败保留已创建的天气文件与阶段清单，而不是套用项目恢复工具。

## 本地验收记录

2026-10-12，使用 CLI 8.1.6 和新的 MCP stdio 会话，不启动 GUI。保护 MCP_GUI_MISMATCH50.PRJ/VC 模板及已有组件、SIT/MET；新增组件和控制方案仅在隔离实验目录中。

完整 PAN 模型来自塞维利亚大学公开论文的 PAN 附录（PDF 第 109–110 页）。仅去除页眉与 PDF 行末空白，没有猜测或重新拟合模型参数。其学术样本来源不等同厂商认证；原生及独立控制验证的是本实验的行为。原始论文、PAN/OND、站点、气象、许可、CSV/PDF 均不提交至仓库。

公开来源：

- [论文及 PAN 附录](https://idus.us.es/server/api/core/bitstreams/12da100e-c9e3-43ca-92b2-1e07cce8243b/content)
- [官方标准 PR 定义](https://www.pvsyst.com/help/project-design/results/performance-ratio-pr.html)

已完成的独立证据：

- 给定模板、600 W PAN、9 kW OND、Wanaluwawa SIT/MET、28 度倾角和 1990 年，自动任务产生 8760 条完整小时行；上网电量 26449.5505 kWh、阵列有效电量 27184.0467 kWh、IL_Pmax 7382.7679 kWh、PR 68.9670379%。
- 独立作者只在原始模板对应字段替换 PAN/OND/倾角，使用既有原生运行入口并明确指定 SIT/MET；12 个导出变量的 8760 个逐小时样本与自动任务全部完全一致。
- 原生 PDF 报告为 26450 kWh/year、PR 68.97%，与小时积分按显示舍入精度一致。首次验证器错误要求能量保留小数，已按实际整数精度修正；复核已完成输出，没有重复执行控制仿真。
- 请求 2020 年的反例在原生执行阶段返回 Invalid simulation dates；任务响应 failed、保留原生原因和完整副本/恢复清单，没有改用 1990 年代跑。该原生消息并不证明所有 MET 的有效期规则。
- 早期仅含铭牌字段的 440 W PAN 虽通过静态检查并运行成功，却出现极大削峰及异常运行电压。原输出保留为反例；最终年度检查复核触发诊断，工作流测试确认 needs_review 状态。没有通过猜测补齐或逆向拟合来修复组件。
- 使用真实 MCP 恢复工具逆序恢复日期失败副本及早期异常副本，全部 PRJ/VC 与刚复制的完整快照一致；原模板保持不变，成功配置和报告单独保留。

验证脚本和阶段 JSON 位于本地隔离实验目录 gui-roundtrip-7cd385dab330，verify_auto63.py / verify_auto64.py 记录自动运行、独立对照、PDF 指标、失败和恢复。最终代码再次通过 MCP 生成 MCP_AUTO65_FINAL.PRJ 的全年结果，与已完成的独立控制在全部 12 个变量/8760 行上完全一致，PDF 指标及原模板保持检查通过；成功工作配置、报告和恢复清单留存。

本轮五次原生调用：早期异常模型、完整模型自动任务、独立控制及最终代码任务共四次产生全年输出；2020 年日期反例未完成仿真。对 PDF 显示精度的复核没有新增控制运行。最终 181 项离线回归通过（66.071s），真实 SDK 生命周期通过。离线只增加两个代表性测试方法，复用既有 PAN/OND/电路样本；覆盖预检、正式执行、警告脱敏、失败/超时、逆序恢复、异常状态、对象作用域、闰年、时间/单位/有限值和严格输入。既有 SDK 生命周期补缺失字段及新入口类型反例，不为每个阶段新增测试模块。Python 编译和差异空白检查通过，没有分析专有二进制/DLL。

## 单方案入口与 Open-Meteo 获取：已验收与待验收

以下内容与上面 2026-10-12 的模板入口记录相互独立，不修改其结论。

- 离线与 SDK：新增离线测试本轮全部通过（纯设计包络 25、组件发现 10、provider 获取 30、统一流程 25）；真实 SDK 生命周期 67 checks passed。这些是本轮快照，本页不固化总数。
- native 81 失败保留：原生 SIT 把经度序列化为 4 位小数（`116.39723 → 116.3972`），未改写源数据。
- native 82：原生序列化精度为 `Lat 39.907567 → 39.9076`、`Long 116.397234 → 116.3972`、`Alt 47.75 → 48`；`check_site` 明示 ±0.0000501 deg / ±0.500001 m，超差反例仍然 `blocked`；这是序列化精度，不是坐标位置完全相等。
- native 83 已验收（`needs_review`）：`pvsyst_simulate_design(dry_run=False)` 在线获取 ERA5 北京 2024 → 新 SIT/MET → 自动选合格模板与精确 PAN/OND → 21.6 kW 声明包络 → 原生全年 8784 行；E_Grid 36945.2864 kWh、标准 PR 90.610745%、IL_Pmax 73.2493 kWh、GlobInc 1887.6679 kWh/m2。保留复核为 `endpoint_mean` 近似与原生 15 分钟天气偏移提示，因此不是 `completed`。设计值来自明确的 integration fixture 与实验用 `Generic_9kW` OND，不是北京气候设计极值或厂家/工程认证。证据为 `single-design83-evidence.json`，报告为 `Results/MCP_DESIGN83_annual_2024.csv` / `.pdf`；未执行任何 hash 类测试。
- 空工程 bootstrap：种子工厂与 fallback 已接入离线覆盖，`dry_run` 可纯内存规划、无需用户手工选模板。**native 87 与 88 均已通过**：87 在规范根名 `PVsyst8.1_Data` + 17 根子目录 + `ComposPV` 下 9 个、仅两组件且无 PRJ/SIT/MET 的条件下，`dry_run` 返回 `ready_for_weather`，执行派生官方种子、建立工作工程并获取新 ERA5 天气，2024 年 8784 行原生 `completed`/`needs_review`，E_Grid 36945.2864 kWh、PR 90.610745%（与 83 同值，仅分别观察两份指标，不新增比较或 hash 测试）。88 是最终实证：仅有 PAN/OND、不预建任何目录时，`dry_run` 预检只报缺失、不创建 `Projects`；执行 `workspace_init` 创建 20 个新目录（天气阶段已建立 `Meteo`/`Sites`/`Results`，`ComposPV`/`PVmodules`/`Inverters` 本就存在），**最终 26 个标准目录自动补齐**（不是 init 创建 26 个），同一次执行 8784 行、E_Grid 36945.2864 kWh、PR 90.61074499969226%，26 目录实存、PDF 620743 字节且 `%PDF-` 有效、source 与工作 PRJ 四个文件齐备；首次脚本 `exit 1` 源于把 8784 误改成 8884 的批量替换，属断言 bug 而非产品故障，改后只读原始 CSV/PDF 重放，无原生重跑、无 hash 测试。85 缺结构且根名不符、86 补 26 目录但根名仍不符，均失败并保留 `single-design85-evidence.json` 与全部现场；根名与 26 目录布局必须同时正确，该超集是真实 bootstrap 已证集合而非静态最小链，且根从不重命名。根名与布局只对本机 CLI 8.1.6 已证，其余版本不外推。种子派生保留 `Version=8.1.0/8.1.6` 声明与 UTF-8 BOM；既有 Parent `.BatchParams` 等历史引用保持未改动、单次运行通过，但**不证明**该文件或批参数被加载或消费。离线本轮 29+10+32+22+42=135 项通过（无 skip，含在任何 provider 请求之前执行的嵌套 `ComposPV` 目录链接逃逸与同名文件预检）、真实 SDK 67 passed / 0 failed，本机 syntax、pycompile 与 git diff check 通过。标准种子是**内部 fallback**，工具总数仍是 53，调用者显式给出模板时不会被替换。
- 天气时区约定：除半小时偏移与年内偏移变化外，**负夏令时（negative DST）约定也阻塞**（`Europe/Dublin` 法律标准 +1 h 而冬季墙钟 0 h），避免 1 小时误标；`Europe/Berlin`、`Australia/Sydney`、`Asia/Shanghai`、`Pacific/Honolulu` 回归正常。
- 逆向授权与进展：用户已明确授权原生小时数据逆向并已委派指定 route（`met-binary-time-semantics[cline-pass/cline-pass/deepseek-v4.1-flash]`），"不解析 binary"不再是永久限制。88 的 MET 容器已确认是**纯 UTF-8 文本 + base62 签名 token**（`DDMM`/`WRFactor`），不是 binary float block；7 个低辐照 outlier 的值在 MET 内存在而原生导出为 0，差异发生在 simulation/SFI 而非 import。
- 时间基准限定：83/87/88 的数值只对当时的 `SolarTime` 输入成立；实测发现 MET 内嵌 `HLeg` 会 override SIT 声明，旧闭环按太阳时解释 legal 标签，**逐时数字对齐不等于时间模型正确**，`needs_review` 的 time basis 从未闭合。92 已实证 public MEF `RefTimeS → LegalTime` 后 `HLeg=True`、`timeshift=0`、全年 3×8784 小时 token 无变化，Jan 29 17h 由 0（源 CSV 7）变为 7.0003**93 已完成**：同一 88 源 CSV 以 `mode=csv` 生成新 93 SIT/MET/项目与全年，`LegalTime`、time fields 0、`HLeg=True`、guard 有效、`execution_status=completed`，2024 年 8784 行、`E_Grid` 36930.3633 kWh、PR 90.60707390969796%；7 个旧 outlier 全部恢复（5.0002/6.0002/7.0003 vs 5/6/7），GHI/DHI 最大差 0.002 W/m²、温度 0.0001 °C、超阈 0；15 分钟晴空警告为空系修正 `RefTimeS` 后的自然结果（非 suppress）。`endpoint_mean` 未变真实平均，source-aware review 仍为 `needs_review`。83/87/88 legacy `SolarTime` 结果不作正确 local legal 工程结果。见 [天气工程审核与来源绑定](weather-engineering-review.md)。
- 另有两个新增入口：`pvsyst_review_weather`（只读三层天气审查，不写/不哈希/不跑仿真；**验收 pending**，合同验收待 native 88 replay 实际结果）与 `pvsyst_prepare_sourced_design`（源文本 + 数值/单位 token + 引文装配 `electrical_design` 草案；**已 SDK 验收**，120 passed / 0 failed）；见 [天气工程审核与来源绑定](weather-engineering-review.md)。
