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

完整性检查按**逐 module cell** 的真值语义，而非“每表一 string / 表数==NModSerie / 只有两表 / global∈{1,2}”的伪限：每个 global string 容量 = 源 `NModSerie`（容量挂串不挂表），串可跨表、一表可多串，表格数与每表格数互相独立。检查核对每表 cell 覆盖（`{(r,c)}` 恰一次）、逐 global 的容量合计 `==NModSerie`、每子阵 ordinals 段完整无重、以及 `NoField3D`/`NoField3DCreated` 唯一场景身份（`NoObjCreated` 全场唯一非 0）；表名是原场景身份，不按稀疏 `SubArrayId` 数字排序。双朝向“每朝向一表单串”、单朝向 fine“恰 2 表 / 2 源串 / 2 逆变器 / 2 总 MPPT”都只是通用不变量在特定取值下的特例，仍过同一函数。需注意：`stored_consistent` 仅是存储/校验层自洽，通用表数下的 `stored_consistent` 不等于已做原生物理消费——由本机 PVsyst 8.1.7 CLI 冷跑 48h 真实 Native 消费验证的示值目前有两处：(1) 单固定朝向/单子阵/3 表各 12 cell、36 module、2 串跨表（CSV `E_Grid`=190.0354/`Shd`=0）；(2) 两个源子阵各 2 串、2 表各 36 块、共 72 块（全局串 1/2 属子阵 1、3/4 属子阵 2，CSV `E_Grid`=380.067/`Shd`=0），两处逐时 maxdiff=0。注意：72 块示值的两个源平面虽为两个不同 `NoOrient`（1/2），但存储的 Tilt/Azim 完全相同（28.000°/0.000°），因此只证明“两个固定平面 id 的逐格接线被真实消费”，不证明不同倾角/方位角的多朝向。其它 N 表仅存储自洽，尚未 Native 消费。多子阵逐格接线在每表 `NoSubArray` 与其全局串的源子阵区间一致时可 `stored_consistent`；跨子阵交换全局串（总容量仍对但归属错）在写入前拒绝。

两个源子阵的逐格绑定按**真实源结构**判定（两个互异正整数 `SubArrayId`，按文件出现顺序），不再使用 `{1,2}` 数字白名单。例如源子阵 id 依文件顺序为 `7,2`（各自 2 串 × 18 块，全局串 1/2 属 7、3/4 属 2）时，`stored_consistent` 判定与回读都按位置序 `[7,2]`（而非数字排序 `[2,7]`），两表各 36 块、共 72 块；容量仍挂每个全局串（= 所属源 `NModSerie`）。此稀疏限定仍只覆盖**两个固定朝向、每朝向一表、无功率共享、非 portrait、零间距**的边界；legacy None（无 `string_layout`）路径仍要求源子阵 `{1,2}`，不变。

仍**显式拒绝**（返回 `needs_review`/`unsupported`，不静默当作已验证）：`ModOrient!=moLandScape`、非零 `ModSpacingX/Y`/`RowPadding`、`PowerSharingInverterId!=-1`、非 `FixedPlane`/非零 `BaseSlope`、`NoShed3D!=1`、`NIdentical!=1`，以及重复 cell/未知 global/容量不符/未知子阵引用。两个普通固定朝向（每朝向各自 table/global 的 grouped source-data 映射，如多子阵 2×2：两源子阵各两串、72 块）已可存储/回读并校验为多 global 串（存储层 `stored_consistent`），且该 2×2/72 块示值已由原 engine 冷跑 48h 真实 Native 消费（详见上节；两平面 Tilt/Azim 相同，不证不同角度）；同一普通固定朝向也可拆成多张表：每表列 `orientation_id`，按朝向分组的组件数求和须等于该源子阵的 `NModSerie×NStringCh`——该“每朝向多 table”形状（例如 72 块 3 表：朝向 1 两张 18 块表承载 global 1/2、朝向 2 一张 36 块表承载 global 3/4）已可创建/回读/导入为 `stored_consistent`；其中 3 表 18/18/36、Az 0/90、声明 0.5 m 正净距的一例已由本机 PVsyst 8.1.7 CLI 冷跑 48h 真实原生消费（逐时 14 列全有限，单次观测非 golden；0.5 m 为调用方设计输入，非软件最小值），而零间距同屋面场景即使存储层 `stored_consistent`、原生仍因 PV 场穿透被拒，故存储自洽不等于原生合法；仍属待证边界的是超过两个固定朝向，以及共享/异构/portrait/非零间距的逐格接线。`string_layout_state="draft_incomplete"`（含 `global_string=0` 或未覆盖全表）表示可存/可读但不可算，绝不等于 `stored_consistent`。

原生全局状态 `mlDefStrings` 或 `mlDefInverters` 只是保存状态，不表示当前选中了精细计算。布局的 `stored_consistent` 与 `native_consumption_verified=false` 也不构成电气损失计算证明。通过[原生住宅任务](native-house-task.md#module-layout-电气阴影)选用 `shading_mode="module_layout"` 后，导入结果另行记录 GUI 重载后的模式选择，仿真再检查 `ShdElec` 输出。

## 三种绑定状态（`status`）

该检查支持 PVsyst 8.1.6 变体中 1..2 个普通非平均固定朝向、1..2 个子阵列：单固定朝向时对应 1..3 张 PVCollada 导入的 `pvShdArrayTable`，双固定朝向时对应 1..2 张（若变体是空场景，则不附加 `pv_bindings` 字段）：

- **`stored_consistent`**：所有 PV 表的 `NoOrient` 引用均指向存在的固定朝向，表格的倾角、方位角、底面坡度与目标固定朝向完全一致，且每个朝向下的 3D 表格组件总数恰好等于对应子阵列的 `NModSerie × NStringCh`。
- **`needs_review`**：发现朝向引用缺失、表格角度与系统朝向不一致，或者某个朝向下的 3D 组件数与电气子阵列组件数对不上（具体差异见 `issues`）。
- **`unsupported`**：场景的表数超出其朝向对应上限（单朝向超过 3 张、双朝向超过 2 张）、使用了跟踪/平均朝向或非有限坐标等超出当前检查规则的结构（此时 `shading_scene` 仍保持 `active` 状态并保留原始字段摘要）。

**需要注意的一点**：`stored_consistent` 只代表 `.VCx` 里保存的朝向引用、角度和组件颗数自洽；如果你在 3D 编辑器里把整张光伏表平移偏离了屋顶（只改了 `DOrigine` 坐标，没改朝向和颗数），`stored_consistent` 依然会是真。要连同屋面空间坐标一起做 `0.00002 m` 精度的完整核对，需走 [`pvsyst_run_house_task`](native-house-task.md) 的场景归位与重载校验流程（详见[场景身份说明](native-scene-identity-map.md)和[双屋面交付](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。
