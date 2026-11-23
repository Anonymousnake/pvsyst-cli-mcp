# 已保存 3D 场景中的 PV 表格与朝向关联检查（`pv_bindings`）

当一个 `.VCx` 变体包含活动的 3D 近阴影场景（`shading_scene`）时，调用 `pvsyst_get_variant_parameters` 会在 `shading_scene.pv_bindings` 中返回场景内各张光伏组件表（`pvShdArrayTable`）与电气朝向（`pvOrient`）、子阵列（`pvSubArray`）之间的绑定核对结果。

```json
{
  "project": "HOUSE.PRJ",
  "variant": "VC1"
}
```

## `pv_bindings` 返回字段

| 字段路径 | 内容说明 |
|---|---|
| `pv_bindings.tables` | 每张 `pvShdArrayTable` 在文件中的对象路径、原始字段值、绑定的 `orientation_id`（`NoOrient`）、表格组件数（`module_count`）以及原生局部原点 `origin_m`（`DOrigine`） |
| `pv_bindings.groups` | 按朝向汇总关联的子阵列 ID、PV 表路径，以及电气侧声明组件数（`NModSerie × NStringCh`）与 3D 场景侧表格组件数的对比 |
| `pv_bindings.coordinate_frame` | 原生 `DOrigine` 坐标值（单位米） |

## 原生组件布局（`module_layout`）

活动场景还返回 `shading_scene.module_layout`，用于读取已保存的原生组件布局。`configured=false` 表示没有 `pvModuleLayout`；有布局且 `status="stored_consistent"` 时，会返回 `global_state`、总组件数和逐表子阵/朝向、原生物体引用、行列及组串信息。

完整性检查按**逐 module cell** 统计（而非硬性要求每表一串或仅限两张表）：每个 global string 的容量等于所属子阵列 `NModSerie`，组串可跨表也可一表多串。校验逻辑核对每张表的 cell 覆盖（无重复、无遗漏）、各 global string 累计块数等于 `NModSerie`、各子阵列编号区间连续以及 `NoField3D`/`NoField3DCreated`/`NoObjCreated` 场景对象唯一性（表序按场景对象原序而非 `SubArrayId` 数字排序）。双朝向每面一表、单朝向两表两串等常见排布都属于该统一规则下的特例。需注意 `stored_consistent` 仅表示 `.VCx` 存储结构有效，不等价于已通过 PVsyst CLI 仿真：目前已通过 PVsyst 8.1.7 CLI 48 小时测试的示例包括单朝向 3 表 36 块跨表接线和双子阵 2 表 72 块（相同 Tilt/Azim 下的两个独立 `NoOrient`）两种配置，其他多表组合仅验证过文件存储结构。当多子阵逐格接线中各表的 `NoSubArray` 与其全局组串所属子阵列区间不匹配时，写入前会直接报错拦截。

两个源子阵的逐格绑定按**真实源结构**判定（两个互异正整数 `SubArrayId`，按文件出现顺序），不再使用 `{1,2}` 数字白名单。例如源子阵 id 依文件顺序为 `7,2`（各自 2 串 × 18 块，全局串 1/2 属 7、3/4 属 2）时，`stored_consistent` 判定与回读都按位置序 `[7,2]`（而非数字排序 `[2,7]`），两表各 36 块、共 72 块；容量仍挂每个全局串（= 所属源 `NModSerie`）。该支持范围适用于两个固定朝向、每朝向一表、无功率共享、横装且零间距的布局；未提供 `string_layout` 的传统路径仍沿用源子阵 `{1,2}` 规则。

以下情形仍**显式拒绝**（返回 `needs_review` 或 `unsupported`）：`ModOrient!=moLandScape`、非零 `ModSpacingX/Y`/`RowPadding`、`PowerSharingInverterId!=-1`、非 `FixedPlane`/非零 `BaseSlope`、`NoShed3D!=1`、`NIdentical!=1`，以及重复 cell、未知全局编号、容量不符或未知子阵引用。目前双固定朝向（每朝向一表或多表，例如 72 块拆分为 3 表且保持 0.5 m 正净距）均可通过存储层检查，并有代表性示例在 PVsyst 8.1.7 CLI 中跑通仿真（注意同屋面零间距场景即使文件结构正确，PVsyst 仍会因 PV 场穿透报错）；对于三朝向及以上、共享/异构逆变器、竖装或非零间距的场景，精细电气损失精度尚未完成全面实测验证。`string_layout_state="draft_incomplete"`（含 `global_string=0` 或未铺满整表）表示草稿可保存与回读，但不可直接用于仿真。

原生全局状态 `mlDefStrings` 或 `mlDefInverters` 只是保存状态，不表示当前选中了精细计算。布局的 `stored_consistent` 与 `native_consumption_verified=false` 也不构成电气损失计算证明。通过[原生住宅任务](native-house-task.md#module-layout-电气阴影)选用 `shading_mode="module_layout"` 后，导入结果另行记录 GUI 重载后的模式选择，仿真再检查 `ShdElec` 输出。

## 三种绑定状态（`status`）

该检查（存储层核对）支持 PVsyst 8.1.6 变体中一个或多个普通非平均固定朝向及其对应子阵列：每个朝向可对应一张或多张 `pvShdArrayTable`，全场景表数上限为该变体全部朝向电气组件数之和（`1..Σ NModSerie×NStringCh`），逐朝向的表组件数之和仍须等于该朝向电气组件数（若变体是空场景，则不附加 `pv_bindings` 字段）：

- **`stored_consistent`**：所有 PV 表的 `NoOrient` 引用均指向存在的固定朝向，表格的倾角、方位角、底面坡度与目标固定朝向完全一致，且每个朝向下的 3D 表格组件总数恰好等于对应子阵列的 `NModSerie × NStringCh`。
- **`needs_review`**：发现朝向引用缺失、表格角度与系统朝向不一致，或者某个朝向下的 3D 组件数与电气子阵列组件数对不上（具体差异见 `issues`）。
- **`unsupported`**：全场景 PV 表数为 0 或超过全部朝向电气组件数之和、使用了跟踪/平均朝向或非有限坐标等超出当前检查规则的结构（此时 `shading_scene` 仍保持 `active` 状态并保留原始字段摘要）。

**需要注意的一点**：`stored_consistent` 只代表 `.VCx` 里保存的朝向引用、角度和组件颗数自洽；如果你在 3D 编辑器里把整张光伏表平移偏离了屋顶（只改了 `DOrigine` 坐标，没改朝向和颗数），`stored_consistent` 依然会是真。要连同屋面空间坐标一起做 `0.00002 m` 精度的完整核对，需走 [`pvsyst_run_house_task`](native-house-task.md) 的场景归位与校验流程（另见[工程交付说明](engineering-delivery.md)）。
