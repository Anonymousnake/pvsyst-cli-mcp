# 项目显示标题

## 功能与范围

项目显示标题是 PRJ 根头部的已有 Comment 字段，对应 PVsyst 的项目列表“项目名称”、项目编辑页“项目名称”和原生 CSV/PDF 项目标题。它不是 PRJ 文件名、VC 文件名或变体名称；GUI 窗口标题仍显示 PRJ 文件名。

既有项目标题闭环通过三个工具完成，不为名称和编码分别新增工具：

| 工具 | 用途 |
|---|---|
| pvsyst_inspect_project | 读取原始头部 project_name、PRJ 的 utf8_bom、完整 files 映射和来源检查 |
| pvsyst_update_project_parameters | 预览或修改 project_name，可显式增加 UTF-8 标记 |
| pvsyst_restore_project_sources | 按完整事务备份恢复标题及原始编码状态 |

标题编辑只替换已有唯一根头部字段。嵌套对象中的 Comment、变体、来源与物理设置保持。模板缺少字段、字段重复/所属关系含糊或对象结构异常时，诊断原因并在写入前停止，不创建默认标题。

## 输入契约

| 参数 | 契约 |
|---|---|
| project | 工作区 Projects 下现有 PRJ 的文件名 |
| expected_files | 最近一次 inspect_project 返回的完整 files 字典，含全部 VC，不是仅 PRJ 的单个摘要 |
| project_name | 严格字符串，1–80 UTF-8 字节，可打印、单行、无首尾空白；按字节而非字符计数 |
| dry_run | 严格布尔值，默认 false；true 表示只验证候选和返回差异 |
| add_utf8_bom | 严格布尔值，默认 false；增加 EF BB BF 需明确 true |

80 字节是已验证的适配器契约，不是根据 GUI 文本框推断的原生绝对上限。已核验 1 字节、80 字节 ASCII、80 字节中文混合标题及 81 字节停止条件。数字 1 和字符串 "true" 不作为布尔值接受。ASCII 标题内的等号、斜杠和分号有原生及 GUI 保留证据。

检查接口读取的是 UTF-8 文本原值。无 BOM 中文文件可能检查值正确、GUI/CLI 实际显示却为乱码；project_name 不冒充“原生有效解码值”。中文编辑需要已有 BOM 或显式 add_utf8_bom=true；ASCII 编辑保留现有标记状态。标记改变整个 PRJ 的解码，不只标题。已有非 UTF-8 字节不进行猜测或自动转码。

## 调用顺序

1. 关闭 GUI 中正打开的待修改项目，避免之后把旧内存状态重新保存到文件。
2. 调用 inspect_project，保留完整 files 映射、原名称和 utf8_bom 状态。
3. 调用 update_project_parameters，传入该 files 映射、目标名称和 dry_run=true。需要增加 UTF-8 标记时显式选择 add_utf8_bom=true。
4. 检查 diff、name_change、changed、bom_added 及候选 files。纯标记事务的文本 diff 可以为空，但 changed=true、bom_added=true。
5. 应用时使用预览前的原 files 映射并令 dry_run=false，不把预览返回的候选 files 当作写入前置条件。
6. 保存应用响应中的完整 backups 和 files；再次检查、重新打开 GUI，按需运行原生仿真检查实际显示。
7. 恢复时把完整 backups、当前对应 files 和 confirm=true 传给 restore_project_sources。

相同名称、相同标记的重复请求返回 changed=false，不生成备份。实际修改会保存完整 PRJ/VC 快照，但仅替换字节发生变化的 PRJ；未变 VC 的字节、修改时间与文件标识保持。标题编辑及纯标记编辑均支持恢复原始字节、换行与 BOM 状态。

若 GUI 或其他编辑器在应用后改了文件，旧 files 会触发冲突诊断。先重新检查并确认新状态，再决定新编辑或明确恢复完整旧快照。恢复整个项目也会恢复该快照里的其他字段，不是对最新 GUI 内容做标题字段合并。

## GUI 与 MCP 交替操作

GUI 修改标题并保存后，先关闭该项目，再由 MCP 重新检查；MCP 使用新的完整文件集接续修改。之后重新打开 GUI 读取修改，不继续使用旧项目表单里的标题值。

GUI 保存可能正常改写版本或省略默认字段；首次回存记录已观察根 Version 8.1.0→8.1.6、嵌入站点 Version 及 DiscrOrientDiff 字段变化。逐小时对照验证限定样本的数值一致，不把整份 PRJ 的字节变化简单等同为物理变化，也不宣称所有 GUI 默认变化都无影响。

## 结果和边界

既有实现覆盖检查、严格输入、预览、实际修改、无操作、全文件集冲突保护、完整备份/恢复和显式编码选择。GUI 8.1.6 与 CLI 8.1.6 的独立编码控制、实际保存/重新打开、标题双向交替编辑和恢复记录见 [详细验证](project-source-verification.md)。更广项目/变体管理范围见 [整体验收](project-variant-acceptance.md)。

原生 SFI 的 EArray/E_Grid/IL_Pmax 在本实验中以 kW 输出；固定一小时采样下积分得到 kWh。长标题 PDF 自动换行，文本提取可能把不换行空格显示为空格。PDF 排版核验与 CSV 原文、GUI 字段、PRJ 原值分别记录，不要求长标题恰好在同一行。

本次以 UTF-8 PRJ、已有标题字段及已记录的模板/版本为交付范围。适配器保留 1–80 字节限制；非 UTF-8 转码、全部历史版本/区域保证、标题字段缺失修复、文件重命名和变体 GUI 名称编辑不是本功能的默认行为。

进程内锁与完整文件集前置条件用于冲突检测，并非跨 GUI/外部编辑器的锁或多文件断电原子事务。已运行的 MCP 服务需重启以加载当前接口说明。此次功能验证使用明文文件和公共窗口/可访问性 API，没有专有二进制或 DLL 分析。
