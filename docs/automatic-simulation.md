# 模板驱动的全年仿真

`pvsyst_simulate_template`组合现有工程复制、源同步、组件引用、参数事务、原生运行与结果检查，不生成空白物理设计或接管GUI。单一设计spec由[simulate_design](single-design-inputs.md)协调；显式跟踪使用[继承UnlimTracking任务](tracking-template-flow.md)，不套用固定平面范围。

## 固定模式输入

| 参数 | 要求 |
|---|---|
| template_project／variant | 工作区完整PRJ及所选VC |
| new_project | 全新PRJ前缀；工程、清单、SFI及CSV/PDF均无冲突 |
| pan_name／ond_name | 精确完整明文PAN/OND，不猜模型或用简短铭牌代替拟合 |
| site_name／met_name | 已有SIT/MET，或由已准备清单明确绑定 |
| tilt | 严格有限0..90° |
| year | 严格整数1969..2068，完整所选小时年 |
| dry_run | 严格布尔，默认true |
| capacity | 可选[声明包络与整数数量规划](capacity-sizing.md) |
| weather_manifest_name | 可选天气来源清单，启用资产／时基及逐时对照 |
| report_pages | 原生页名列表；省略/null为默认完整报告 |

要求结构完整的单子阵／固定平面Grid，无活动阴影／表／Module Layout，根PRCorrTemp=False，PAN GRef=1000W/m²。未传capacity时保持数量、MPPT、方位、损失和模式；SIT/MET同步整个副本，选中VC改引用和倾角。capacity仅接受更窄的完整独立MPPT模板，不重建拓扑或拟合损失。

不自动选硬件、填缺测、改年、换设备／天气或认证全工况。MET日历预检只读声明，ready不是原生试运行，也不证明载荷覆盖、气候质量或地理／时区一致。

## 预览与状态

使用实际工作区文件调用，检查issues后才设置dry_run=false：

```json
{"template_project":"Template.PRJ","variant":"VC0","new_project":"RoofAnnual.PRJ","pan_name":"ValidatedModule.PAN","ond_name":"ValidatedInverter.OND","site_name":"Site.SIT","met_name":"Weather.MET","tilt":28,"year":2024,"dry_run":true}
```

blocked表示输入／范围／能力／冲突问题，ready表示只读预检；completed表示执行和本入口结果检查通过。needs_review保留原生警告、异常启发式或天气差异，不能当正常工程认证。failed查看stage/error和产物。MCP is_error=false不保证完成；参数类型错误仍为工具错误。

预览不创建文件或备份；执行先独占创建Results清单，不覆盖旧任务，不自动重试。新的任务请求、实际配置、快照及恢复调用保存在清单中。

## 年度结果

CSV上下文须匹配项目、变体、SIT/MET；时轴恰为该年00:00..23:00的8760或8784个小时，缺失／重复／倒序／额外年份停止，不补行。E_Grid、EArray、IL_Pmax、GlobInc需明确单位与有限值。标准并网PR使用原生E_Grid、GlobInc与已核验组件数／PAN根PNom和GRef，不平均小时PR；辐照加权交叉检查的0.002绝对比值为导出复核容差，不是设计合格线，也不用于储能。

IL_Pmax不是全部限电或逆变器损失。年度IL_Pmax超过铭牌容量与入射辐照对应STC参考能量的三倍会触发公开的保守review，不是原生物理上限。PDF检查原生文件标识，生产路径不依赖文本提取库。diagnostics尾部最多12000字符；本次原生warning/warn/caution行保留并可使needs_review，固定适用范围说明本身不触发。

## 天气与报告

绑定清单时，在写项目之前重验源／规范化／月值CSV、MEF、SIT、MET及状态、年份、时基和名称。原生显式加载SIT/MET，输出GlobHor/DiffHor/T_Amb与准备CSV比较。0.1W/m²和0.01°C仅为舍入／转换复核阈值；超阈或保留转换警告保持review，标签／行数／单位不一致则失败。prepared不等于MET验证；见[天气准备](weather-preparation.md)。

report_pages沿严格schema进入请求清单和原生-rp，不后裁／重排PDF。可用名称：

```text
cover summary notes params horizon shadings usersneeds results econeval circuit losses financialbalance p50p90 carbonbalance predefgraphs
```

未知／空列表／错误类型在发现、天气或副本创建之前停止。页名合法不保证当前配置有该页内容，例如P50仍需原生结果状态。run_simulation原逗号字符串接口不变。

## 失败与恢复

原模板只读，失败保留副本、备份、SFI、报告和清单。recovery按逆序给出现有restore_variant／restore_project_sources参数；只有文件仍处对应事务状态时有效，外部编辑后先重新检查，不替换前置条件盲目覆盖。恢复回到刚复制的PRJ/VC，不删除任务或改写历史报告。

清单更新使用同目录临时文件替换，非跨文件原子作业或断电持久性保证。编辑与登记之间中断需结合已有阶段、后端备份和现场人工恢复；进程锁不排除外部编辑。同进程单工作区串行，不把恢复记录当跨进程租约。
