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

当前完整性检查限定为两个固定朝向、子阵列 `1`/`2` 各自独占一张零间距横装表、一个逆变器/MPPT 和一条 `1..100` 块组件的组串。检查会核对原生列表声明数量、模块序号/行列/组串、物体创建引用、朝向和回路计数；缺失、重复或未识别的结构返回 `needs_review`，不会视为布局消失。

原生全局状态 `mlDefStrings` 或 `mlDefInverters` 只是保存状态，不表示当前选中了精细计算。布局的 `stored_consistent` 与 `native_consumption_verified=false` 也不构成电气损失计算证明。通过[原生住宅任务](native-house-task.md#module-layout-电气阴影)选用 `shading_mode="module_layout"` 后，导入结果另行记录 GUI 重载后的模式选择，仿真再检查 `ShdElec` 输出。

## 三种绑定状态（`status`）

该检查支持 PVsyst 8.1.6 变体中 1..2 个普通非平均固定朝向、1..2 个子阵列：单固定朝向时对应 1..3 张 PVCollada 导入的 `pvShdArrayTable`，双固定朝向时对应 1..2 张（若变体是空场景，则不附加 `pv_bindings` 字段）：

- **`stored_consistent`**：所有 PV 表的 `NoOrient` 引用均指向存在的固定朝向，表格的倾角、方位角、底面坡度与目标固定朝向完全一致，且每个朝向下的 3D 表格组件总数恰好等于对应子阵列的 `NModSerie × NStringCh`。
- **`needs_review`**：发现朝向引用缺失、表格角度与系统朝向不一致，或者某个朝向下的 3D 组件数与电气子阵列组件数对不上（具体差异见 `issues`）。
- **`unsupported`**：场景的表数超出其朝向对应上限（单朝向超过 3 张、双朝向超过 2 张）、使用了跟踪/平均朝向或非有限坐标等超出当前检查规则的结构（此时 `shading_scene` 仍保持 `active` 状态并保留原始字段摘要）。

**需要注意的一点**：`stored_consistent` 只代表 `.VCx` 里保存的朝向引用、角度和组件颗数自洽；如果你在 3D 编辑器里把整张光伏表平移偏离了屋顶（只改了 `DOrigine` 坐标，没改朝向和颗数），`stored_consistent` 依然会是真。要连同屋面空间坐标一起做 `0.00002 m` 精度的完整核对，需走 [`pvsyst_run_house_task`](native-house-task.md) 的场景归位与重载校验流程（详见[场景身份说明](native-scene-identity-map.md)和[双屋面交付](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。
