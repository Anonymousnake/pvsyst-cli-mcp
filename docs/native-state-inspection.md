# 变体保存状态只读检查（Native State Inspection）

调用 `pvsyst_get_variant_parameters` 时，除了返回常规的倾角、组串数和基础损失外，还会把 `.VCx` 里保存的变压器、系统停运、削峰、3D 阴影场景、组件老化、IAM 和 P50/P90 等高级配置块一并解析出来，方便在改参或排错前看清变体的完整底数。

## 解析返回的字段结构

| 返回字段路径 | 包含的内容与解析说明 |
|---|---|
| `transformers` | 解析电路树中的 `TransformerNode`，返回 7 个核心损耗/额定字段、节点层级深度及嵌套关系。注意 `count` 表示序列化的 `TransformerNode` 块数量，不一定等于物理变压器台数 |
| `system_unavailability` | 解析 `pvSystem` 直属的 `SystemUnavail` 块，返回不可用模式、目标停运比例、时段数量（最多返回 1000 段，超限报错不截断）、持续时长、随机种子及具体停运时段列表（与 `WeakGrid` 储能的电网停电序列相区分） |
| `peak_shaving` | 解析 `PeakShaving` 块，返回放电时序模式（`AfterSunset` 或起止小时窗口）、削峰功率限值、SOC 阈值与充放电效率 |
| `shading_scene` | 解析 3D 阴影块标志位、`ListeObjets` 内的三维物体数量与类型统计、保存的阴影标量，以及 [PV 表格与朝向的关联核对结果（`pv_bindings`）](native-scene-bindings.md) |
| `ageing.template` | 解析老化配置块的 11 个模式与参数字段（运行年数、年均衰减率、RMS 离散度、保修点数组等） |
| `subarrays[].iam` | 解析各子阵列的入射角修正（IAM）配置，区分组件自带（`component`）、`ASHRAE`、`FresnelSmooth` 和自定义折线 `UserProfile` |
| `p50_p90` | 若变体根部包含 14 字段的 `P50_P90=TP50_P90` 块则返回各字段值；未配置时返回 `null` |

如果某个配置块在 `.VCx` 里压根没出现，对应字段会标为未配置；如果出现了重复块、残缺字段或未知枚举值，会在 `issues` 中明确指出。

## 只读检查与可写范围的区别

`pvsyst_get_variant_parameters` 的覆盖面比写入接口更广——有些复杂块（比如 `FresnelSmooth`/`UserProfile` 的自定义 IAM 点表、多级变压器损耗字段、`SystemUnavail` 随机停运时段）目前只支持读出保存状态，尚未开放直接修改。

各个可写领域的具体前提条件（例如 `p50_p90_updates` 要求变体已具备结果模板标志 `$01800001`）以对应写入工具的文档为准：
- [P50/P90 参数编辑](p50-native-parameters.md)
- [老化与 IAM 输入](native-model-inputs.md)
- [削峰放电控制](native-peak-shaving.md)
- [MV 夜间断开](native-mv-night.md)与[AC 线缆配置](native-ac-cabling.md)
