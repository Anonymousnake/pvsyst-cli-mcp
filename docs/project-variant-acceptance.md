# 项目与变体管理

PRJ组织电站资料，VC为其下可独立配置、运行和比较的方案。工具支持受限文件、参数和电路生命周期，不复刻完整GUI，也不认证电气设计。

| 工作 | 工具与说明 |
|---|---|
| 检查成员 | list_projects、list_variants、inspect_project返回文件集、引用差异、缺件、原始标题和BOM |
| 新建／复制 | create_project接收完整文本；clone_project／clone_variant保留来源和引用，可跨项目复制，不生成默认物理设计 |
| 标题／来源 | update_project_parameters与update_project_sources使用完整文件集保护及整组恢复；[事务](project-source-verification.md) |
| 组件引用 | PAN/OND按子阵选择，BTR/GEN按system选择；仅替换明确存在的引用，不拟合模型或据文件名判断兼容 |
| 参数／结构 | get/update_variant_parameters、validate_variant_structure、clone/remove_subarray；[拓扑与模式](grid-capacity-verification.md) |
| 恢复／归档 | restore_variant及project/variant归档、restore_project_archive；完整恢复清单和当前前置条件，普通变体归档至少保留一个成员 |

典型顺序是检查、预览、应用、重新检查结构、原生运行，再按需逆序恢复或归档。每次都使用最新变体令牌或完整项目files，外部编辑后重新检查。乐观检测不是跨进程锁或断电原子事务。

非ASCII名称要求已有UTF-8 BOM或显式增加，影响整个文件解码。原始UTF-8检查不证明未标记文件被原生正确显示。恢复保留原BOM及换行，详见[标题](project-title.md)。

数量、角度、模式和结构编辑保留各自版本、owner、计数及几何门禁；活动阴影、Module Layout、未知／混合版本和不完整电路不会被视为可编辑。MPPT图示不是模式或物理分配证明，任意合并拆分、跨朝向预算和自动硬件选型仍不支持。

ModuleQualityLoss是成对U/I与默认位的语义入口，原始U/I/P不是三项独立百分比。其他损失及派生缓存按[专题指南](native-entry-map.md)处理，不通过原始字段读取扩大写入域。

版本声明可被CLI8.1.6加载不表示运行过旧版可执行程序；局部原生控制不外推所有模型、全年天气或GUI往返。默认工具集合及新入口见[工具参考](tool-reference.md)，运行中的MCP进程需重启以读取新schema。
