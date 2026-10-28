# 工程保存值与准备清单对比（Project Input Review）

在用 `pvsyst_prepare_design_project` 生成好源工程后，工程文件可能会被导入到 PVsyst GUI 里挂接 3D 住宅场景、或者经过其他步骤转存为新变体。`pvsyst_review_project_inputs(project, variant, preparation_manifest_name)` 用于只读对比目标 `.PRJ`/`.VCx` 里当前实际保存的参数值与当初准备清单（`<stem>_prepare_<year>.json`）里记录的设计初衷是否一致，看看中间有没有哪个参数被意外改掉。

该工具全程只读，不修改任何文件，也不调用 `PVsystCLI.exe`。

## 调用方式与返回字段

```json
{
  "project": "HOUSE.PRJ",
  "variant": "VC1",
  "preparation_manifest_name": "HOUSE_INPUT_prepare_2024.json"
}
```

`preparation_manifest_name` 必须是 `Results` 目录下由 `pvsyst_prepare_design_project` 生成的准备清单（文件大小上限 32 MiB）。目标 `project` / `variant` 可以是刚准备好的源工程本身，也可以是基于它另存出来的后续变体（要求保持单子阵列、单固定非平均朝向的并网结构）。

返回字典中的核心字段：

| 字段名 | 说明 |
|---|---|
| `comparisons` | 逐项比对明细（含所属对象 `owner`、单位 `unit`、声明值 `declared`、期望保存值 `expected_saved`、实际保存值 `actual_saved` 以及状态 `matches` / `differs` / `missing`） |
| `differences` | 所有状态不为 `matches` 的差异项汇总 |
| `declared_not_native` | 准备清单中记录但不会直接写成 `.VCx` 标量字段的声明参数（如冷态开路电压计算包络、仿真目标年份等） |
| `inherited_project_saved` | `.PRJ` / `.VCx` 中沿用自模板的设计温度字段原值（如 `DesignTempMin`、`TempOper`、`TempOperMin`、`TempOperMax`） |
| `source_reference_warnings` | 工作区中 `.SIT`/`.MET` 引用文件缺失或无法解析时的告警 |
| `native_mode_flags` | 主阵列、子阵列与系统的原始十六进制 `Flags` 字符串 |

## 比对覆盖的参数项

工具会对以下保存在 `.PRJ` 和 `.VCx` 中的字段做精确比对（浮点数如角度和损失百分比按编辑器写入时的 `.3f` 格式化字符串对齐比对）：
- 组件与逆变器引用文件名（`.PAN` / `.OND`）
- `.PRJ` 与 `.VCx` 中的站点和气象文件名（`.SIT` / `.MET`）
- 固定平面倾角（`FieldTilt`）与方位角（`FieldAzim`），同时检查未开启倾角/方位角平均模式（`UsesTiltAver` / `UsesAzimAver` / `UsesSlopeAver` 必须为假或未设置）
- 容量规划落地后的串联组件数（`NModSerie`）、并联组串数（`NStringCh`）与逆变器台数（`NInverter`）
- 准备阶段显式指定过的损失参数：`LIDLoss`、`PMismMPP`、`ModuleQualityLoss`（同步核对 `PQualityModU` 与 `PQualityModI`）以及主阵列 `UFactor`

## 返回状态说明

- `reviewed`：所有受检保存值与准备清单完全吻合，且无遗留复核项。
- `needs_review`：发现保存值被修改（如倾角变了）、存在未解析的 `.SIT`/`.MET` 引用，或继承了准备阶段的复核项。
- `blocked`：准备清单损坏、目标变体变成了多子阵列/平均朝向等不支持的结构，或字段出现重复歧义。
