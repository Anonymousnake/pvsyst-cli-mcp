# 工程与变体日常管理

在 PVsyst 的工作区 `Projects` 目录中，一个 `.PRJ` 文件代表一个电站项目（保存站点、气象文件绑定及项目标题），它下面挂着一个或多个 `.VC0`、`.VC1`… 变体文件（每个 `.VCx` 是一套可以独立改参、跑仿真和对比的具体设计方案）。

## 常用管理工具一览

| 操作类别 | 对应工具与说明 |
|---|---|
| 盘点与检查 | `pvsyst_list_projects`、`pvsyst_list_variants`、`pvsyst_inspect_project`：查看文件列表、SHA-256 指纹、`.SIT`/`.MET` 引用是否一致、缺失组件以及标题和 UTF-8 BOM 状态 |
| 新建与克隆 | `pvsyst_create_project`（从完整文本新建）、`pvsyst_clone_project`（整项目克隆）、`pvsyst_clone_variant`（同项目内或跨项目复制单个 `.VCx`） |
| 标题与气象源 | `pvsyst_update_project_parameters`（改项目显示标题）、`pvsyst_update_project_sources`（统一替换 `.PRJ` 和所有 `.VCx` 的 `.SIT`/`.MET`）；详见[工程文件与事务](project-source-verification.md) |
| 替换组件引用 | `pvsyst_get_variant_components`、`pvsyst_update_variant_components`：按子阵列替换 `.PAN`/`.OND`，或按系统替换 `.BTR`/`.GEN` |
| 参数与拓扑编辑 | `pvsyst_get_variant_parameters`、`pvsyst_update_variant_parameters`、`pvsyst_validate_variant_structure`、`pvsyst_clone_subarray`、`pvsyst_remove_subarray`；详见[并网拓扑与参数编辑](grid-capacity-verification.md) |
| 备份恢复与归档 | `pvsyst_restore_variant`（回滚单个变体）、`pvsyst_archive_project`、`pvsyst_archive_variant`（归档单个变体时工程下至少需保留 1 个活动变体）、`pvsyst_restore_project_archive` |

## 改参实操建议

1. **标准改参流程**：先调 `inspect` / `get_variant_parameters` 拿最新的文件哈希令牌 → 传 `dry_run=true` 预览 diff → 传 `dry_run=false` 落盘 → 调 `validate_variant_structure` 复核结构 → 调 `run_simulation` 跑仿真。
2. **避免与桌面端冲突**：如果中间在 PVsyst 桌面 GUI 里手动打开并保存过工程，GUI 往往会顺手改写版本号、默认标志位或缓存字段。此时旧的 SHA-256 令牌会失效拦截，重新调一次 `inspect_project` 或 `get_variant_parameters` 刷新令牌即可。
3. **非 ASCII 命名**：给工程或变体起中文名时，记得检查 `utf8_bom` 并在需要时传 `add_utf8_bom=true`（见[项目显示标题](project-title.md)）。
4. **组件质量损失字段**：改组件质量损失时直接传 `subarray_updates.ModuleQualityLoss`，工具会自动同步底层的 `PQualityModU` 和 `PQualityModI` 并清掉默认位 `0x00100000`。
