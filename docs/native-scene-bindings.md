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

## 三种绑定状态（`status`）

该检查支持包含 1..2 张 PVCollada 导入的 `pvShdArrayTable`、1..2 个普通非平均固定朝向和 1..2 个子阵列的 PVsyst 8.1.6 变体（若变体是空场景，则不附加 `pv_bindings` 字段）：

- **`stored_consistent`**：所有 PV 表的 `NoOrient` 引用均指向存在的固定朝向，表格的倾角、方位角、底面坡度与目标固定朝向完全一致，且每个朝向下的 3D 表格组件总数恰好等于对应子阵列的 `NModSerie × NStringCh`。
- **`needs_review`**：发现朝向引用缺失、表格角度与系统朝向不一致，或者某个朝向下的 3D 组件数与电气子阵列组件数对不上（具体差异见 `issues`）。
- **`unsupported`**：场景包含超过 2 张表、使用了跟踪/平均朝向或非有限坐标等超出当前检查规则的结构（此时 `shading_scene` 仍保持 `active` 状态并保留原始字段摘要）。

**需要注意的一点**：`stored_consistent` 只代表 `.VCx` 里保存的朝向引用、角度和组件颗数自洽；如果你在 3D 编辑器里把整张光伏表平移偏离了屋顶（只改了 `DOrigine` 坐标，没改朝向和颗数），`stored_consistent` 依然会是真。要连同屋面空间坐标一起做 `0.00002 m` 精度的完整核对，需走 [`pvsyst_run_house_task`](native-house-task.md) 的场景归位与重载校验流程（详见[场景身份说明](native-scene-identity-map.md)和[双屋面交付](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。
