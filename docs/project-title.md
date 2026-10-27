# 项目显示标题

项目标题是PRJ已有根头部Comment，供原生项目列表、编辑页及CSV/PDF显示；不是PRJ/VC文件名、变体名或窗口文件标题。只编辑唯一所属字段，嵌套Comment、变体、来源和物理设置保持；缺失、重复或异常结构停止，不创建默认标题。

## 参数

| 参数 | 要求 |
|---|---|
| project | Projects中的现有PRJ文件名 |
| expected_files | 最新inspect_project返回的完整files，含全部VC |
| project_name | 严格可打印单行字符串，1..80 UTF-8字节、无首尾空白 |
| dry_run | 严格布尔，默认false；true仅预览 |
| add_utf8_bom | 严格布尔，默认false；增加EF BB BF需明确true |

80字节是适配器范围，不是GUI绝对极限。ASCII中的等号、斜杠和分号按文字处理。原始UTF-8检查值不保证无BOM中文被原生正确解码；非ASCII编辑需已有BOM或明确增加，影响整个PRJ，不只标题。不猜测或转换非UTF-8源。

## 操作与恢复

1. 关闭GUI中待修改项目，避免旧内存状态之后覆盖文件。
2. 调用pvsyst_inspect_project，保存完整files、project_name及utf8_bom。
3. 以原files预览pvsyst_update_project_parameters，检查diff、name_change、changed和bom_added。纯标记更新可文本diff为空但changed=true。
4. 应用使用预览前的原files，而非候选files，并设置dry_run=false。
5. 保存响应完整backups及files，重新检查，再按需打开GUI或执行原生运行。
6. 恢复调用pvsyst_restore_project_sources，传完整backups、当前files及confirm=true。

同名同标记请求无操作且无备份。实际修改备份完整PRJ/VC，但只替换变动PRJ，未变VC的字节、mtime和文件标识保持。恢复重建原始字节、换行和标记；它恢复整个快照，不与外部GUI新内容做字段级合并。

GUI保存后应关闭项目并重新检查，再以新files接续MCP更新。GUI可能改写版本、默认字段或缓存，不将所有字节变化等同物理变化，也不假定都无影响。PDF长标题可换行，提取可归一空格；CSV、GUI和文件原值分开解释，不要求PDF标题恰在一行。

进程锁和完整成员前置条件是乐观冲突检测，不是跨GUI锁或断电原子事务。更广编码／区域／版本、缺标题修复、文件重命名、变体GUI名称及重排是独立功能；见[项目事务](project-source-verification.md)与[项目管理](project-variant-acceptance.md)。
