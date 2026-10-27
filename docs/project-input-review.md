# 比较声明与工程保存值

`pvsyst_review_project_inputs(project, variant, preparation_manifest_name)` 只读比较一份源准备清单的声明与目标PRJ/VC保存值。它不做物理验算或自动修订，不读取未保存GUI状态，不查询许可、不仿真、不改文件。

## 调用与结果

三个参数均为严格字符串。基线应为Results下`pvsyst_prepare_design_project`产生的`<source-stem>_prepare_<year>.json`；目标可为源本身、独立副本或同一受限结构的已保存住宅。基线名称、项目、变体和年份必须自洽，不接受任意全年或住宅任务清单。

```json
{
  "project": "HOUSE.PRJ",
  "variant": "VC1",
  "preparation_manifest_name": "HOUSE_INPUT_prepare_2024.json"
}
```

| 字段 | 解释 |
|---|---|
| `comparisons` | 逐项owner/unit/declared/expected_saved/actual_saved及matches/differs/missing |
| `differences` | 非matches项，不代表全部原生告警或工程问题 |
| `declared_not_native` | 电气包络、容量声明和执行年份等非直接设置 |
| `inherited_project_saved` | 所属对象的DesignTempMin/TempOper/TempOperMin/TempOperMax等原始保存字符串 |
| `source_reference_warnings` | 工作区内SIT/MET引用未解析等警告，保留needs_review |
| `native_mode_flags` | 原始Flags，不推断损失默认／手动模式 |
| `readonly` / `repairs_applied` | 始终true/false |
| `native_consumption_verified` / `runtime_state_observed` | 始终false/false |

`reviewed`只表示受支持保存值没有差异且无已知复核项。差异、未解释的损失模式、来源复核和工作区引用警告均为`needs_review`；基线损坏、歧义字段、平均朝向或超范围结构为`blocked`。

## 比较范围

仅支持一组Grid子阵及其固定、非季节、非平均朝向。UsesTiltAver/UsesAzimAver/UsesSlopeAver均检查；披露原始字段不等于支持编辑所有模式。

可比较PAN/OND文件名、PRJ与VC中的SIT/MET文件名、倾角／方位角、容量计划的NModSerie/NStringCh/NInverter，以及明确请求的LIDLoss/PMismMPP/ModuleQualityLoss/UFactor。角度与损失按编辑器`.3f`序列化比较，不用浮点容差替代相等。ModuleQualityLoss对应PQualityModU/I，UFactor属于main_array。

组件或天气内容、完整模型适用性、来源真实性、现场温度极值和原生有效损失模式不在认证范围。文件名相同不证明模型等价；可解析SIT/MET引用不证明逐时气象质量通过。包络温度、系数、电流限值不按名称映射为项目设置。安装数量按原接受计划比较，不把允许的目标容量误差当错配；执行年份不推算为VC模拟年份。

基线读取限制32MiB，读取结束复核字节；工程使用文件快照避免混合状态。所有名称经过受控文件名及工作区路径检查，不执行清单中的任意路径。workspace锁仅在进程内有效。自动修订、阴影／负荷缓存认证和多子阵不属于此入口。
