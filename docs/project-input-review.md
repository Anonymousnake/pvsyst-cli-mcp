# 比较声明与工程保存值

`pvsyst_review_project_inputs(project, variant, preparation_manifest_name)` 是只读审查，不是原生物理验算或自动修订。它复用工程/参数/引用读取，比较一份已完成源准备清单的声明与目标 PRJ/VC 保存值。未保存GUI状态不在读取范围；不打开软件、不查询许可、不跑全年、不改文件或检查点。

## 调用与结果

三个参数均为严格字符串。基线为 Results 下 `pvsyst_prepare_design_project` 的 `<source-stem>_prepare_<year>.json`，目标可以是源本身、独立副本或同一受限结构的已原生另存住宅；基线名称/项目/变体/年份需自洽。不是任意全年或住宅job清单，基线声明也不是签名认证。

```json
{
  "project": "SOURCED_RESIDENTIAL_HOUSE.PRJ",
  "variant": "VC1",
  "preparation_manifest_name": "SOURCED_RESIDENTIAL_INPUT_prepare_2024.json"
}
```

上例对应本机已保留的验收案例，不要求新建或重复跑它。新资料使用调用方实际准备的名称。

| 字段 | 解释 |
|---|---|
| `comparisons` | 逐项 owner/unit/declared/expected_saved/actual_saved 及 matches/differs/missing |
| `differences` | 上述非matches项；不是全部原生告警或工程问题 |
| `declared_not_native` | 电气包络/容量声明与执行年份等非直接设置，明确不按名称猜对应 |
| `inherited_project_saved` | 拥有对象的 DesignTempMin/TempOper/TempOperMin/TempOperMax等原始保存字符串，独立展示 |
| `source_reference_warnings` | 既有读取器未在工作区解析到SIT/MET引用等警告；有警告保留needs_review |
| `native_mode_flags` | 只披露保存Flags；当前不解释损失默认/手动模式 |
| `readonly` / `repairs_applied` | 始终true/false，不生成候选工程或偷偷修订 |
| `native_consumption_verified` / `runtime_state_observed` | 始终false/false，本次没有软件运行或运行态观察 |

`reviewed` 只表示本次受限保存值没有差异且无已知保留项，不是产品/工程认证。差异、声明损失但未解释其模式、来源review或工作区引用警告均为 `needs_review`。损坏基线、歧义字段、平均朝向或超出本批结构为 `blocked`，不返回部分已通过结论。

## 能比较什么

一组Grid子阵列及分配的一组固定非季节/非平均朝向。UsesTiltAver/UsesAzimAver/UsesSlopeAver沿既有读取与普通平面约束检查；参数读取只新增后两项原始字段，不扩写编辑器能力。

PAN/OND文件名、PRJ与VC的SIT/MET文件名、倾角/方位角、已接受容量计划的NModSerie/NStringCh/NInverter，以及明确请求的LIDLoss/PMismMPP/ModuleQualityLoss/UFactor。角度与损失按既有编辑器 `.3f` 序列化对比，不用浮点容差假装相等；ModuleQualityLoss按已有编辑行为分别对应PQualityModU/I，UFactor属于main_array。原生值单位/owner不因展示而重新定义。

组件/天气内容、完整模型适用性、源真实性、现场温度极值和原生有效损失模式未被认证。文件名匹配不表示模型等价；SIT/MET工作区可解析性提示也不表示气象逐时质量通过。包络温度、系数、电流限值不被对接为项目同名设置；目标DC容量是目标，安装数量按原接受计划对比，不把允许的目标误差当错配；年份是执行选项，不推算VC模拟年份。

基线读入/结束均限制32MiB并比较字节，工程读入/结束复用原有文件快照防混合状态。所有输入名称走共享受限文件名/工作区路径校验，不执行基线里的任意路径。仍为单进程workspace锁，不是跨服务器桌面租约。

## 已证与未证

本机 `pvsyst-project-review-study/round1/public-native-export-review-r2.json`：真实既有准备源与已由PVsyst中文8.1.6另存并完成2024全年的住宅VC1各一次只读MCP审查，76工具，两例保存比较项均零差异，来源review保持，原Projects/Results字节保持；使用不可执行CLI，零新软件运行。旧全年证据不记成本次新增全年。

永久非哈希白名单 `tests/contracts/test_project_input_review_boundary.py`：不可执行CLI/隔离副本上，原始对比零写入；通过既有clone/update公开入口改变倾角后仅报告tilt差异且审查字节保持；MET不可解析产生review警告；方位/坡度平均、保留名、[]/null/错误operation、逃逸路径/未知参数均停止。阴影/缓存/自动修订与多子阵列不由此契约升级。

首次执行在测试setup误用clone的project参数而非真实source_project失败，工作区保留，之后只修调用名，标准未改；初次失败不计通过。另有一次初始定位grep的pvsyst_*.py范围覆盖了受保护文件名，未返回该文件内容、未改动或暂存，检索不计验收；已单独记录scope-incident并改为精确文件白名单。验收只计随后明确执行的公共契约/保存值检查。

指定 `project-input-review-audit [magpie/mw/deepseek-v4.1-flash-sg]` 无阻断，父采纳共享文件名边界、完整平均标志和引用警告处理，补充边界契约后通过；纯文案/假设不冒充缺陷。静态审查与父公开执行分别计证。文档技能缺tool-index文件，使用实际可读通用API模板和纯文本工具，无额外bootstrap或证据脚本。

下一步在明确差异基础上另批设计“新工程副本→既有编辑预览→确认修订→原生重载”的流程；本入口不自动执行这条修订链，不将其标为已交付。
