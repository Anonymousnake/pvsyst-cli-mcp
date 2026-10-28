# 分块模板工程准备

在大中型地面电站或工商业项目中，经常会遇到“全站由 24 个南向标准子阵单元 + 10 个西向标准子阵单元组成”的排布方式。`pvsyst_prepare_block_project` 允许把现有模板变体（`.VCx`）里的各个完整子阵列（SubArray）当作标准分块，按指定的整数倍数量在内存中批量克隆拼装出一个新工程（`.PRJ` + `.VCx`），并返回业务分块 ID 到生成的 `SubArrayId` 的映射表。

该工具只负责工程结构组装与落盘，不自动跑仿真。生成新工程后，可直接交给 `pvsyst_run_simulation` 执行仿真。

## 调用示例

```json
{
  "source_project": "FIXED_BLOCK_SOURCE.PRJ",
  "source_variant": "VC1",
  "new_project": "FIXED_BLOCK_ASSEMBLY.PRJ",
  "blocks": [
    {"id": "south-unit", "source_subarray_id": 1, "count": 24},
    {"id": "west-unit", "source_subarray_id": 2, "count": 10}
  ],
  "dry_run": true
}
```

## 参数规则与组装逻辑

- **分块数量（`count`）**：表示最终工程里该类分块的总数（包含源变体里原有的那 1 个）。例如上面示例中，子阵列 1 最终会有 24 个（原 1 个 + 新克隆 23 个），子阵列 2 最终会有 10 个，合计生成 34 个子阵列。
- **全覆盖要求**：源变体中的每一个子阵列都必须在 `blocks` 列表里恰好出现一次（不需要扩增的分块传 `count: 1`），不支持通过漏传某个 `source_subarray_id` 来隐式删掉子阵列，也不允许同一个 `source_subarray_id` 绑定多个块 ID。
- **容量上限**：单次最多支持 100 种分块类型、生成后的子阵列总数不超过 100 个。
- **继承关系**：每个克隆出来的子阵列完整保留源分块的 `.PAN`/`.OND` 引用、串联组件数、并联组串数、MPPT 模式、专属逆变器组分支以及绑定的固定平面朝向（不同源分块可以使用不同的固定朝向和不同的组件/逆变器型号）。

## 源模板前提条件

为了保证批量复制子阵列后电气拓扑依然合法，源变体（包括 `count=1` 的分块）必须满足以下条件：
- 版本声明为 `8.1.0` 或 `8.1.6` 的常规并网（Grid）工程，主阵列 `Flags=$00100400`、`Flags2=$082A`，系统 `pvSystem` 无附加功率控制（`Flags=0`）。
- 所有朝向均为常规固定平面（`FixedPlane`），无 3D 阴影场景、阴影表或 `Module Layout`。
- 每个源子阵列在电路树（`grid_circuit`）中拥有完整且独占的逆变器分支，不含跨子阵列共享逆变器（`PowerSharingInverterId` 必须为 `-1`）。
- 不含实际的 `TransformerNode`（注：PVsyst 默认残留的 `MVNbTransfos=1` 空字段不会触发拦截，但挂了真实中高压变压器节点的拓扑不支持直接按子阵翻倍复制）、不含系统级限发/辅助损耗控制、也不含负荷/储能/柴油发电机模块。

## 执行与返回值

- `dry_run=true`：在内存中完成全部子阵克隆与结构校验，返回 `status="ready"`，不写任何文件。
- `dry_run=false`：新建 `new_project`（`.PRJ`）及对应的单个 `.VCx`（保留源变体的 VC 编号，不拷贝源工程下的其他无关变体；若目标工程名已存在则直接报错停止）。写盘后会重新解析新变体，逐块核对所属字段、组件引用、朝向编号、逆变器组与串并联数量，确认无误后返回 `status="prepared"`。
- 返回结构包含 `plan`、`mapping`（记录每个业务分块 `id` 对应的全部输出 `SubArrayId`）、`source_serialized_counts`、`serialized_counts`、`totals`（全站逆变器、MPPT、组串、组件总数汇总）以及继承项提示。
