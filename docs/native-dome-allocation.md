# 双坡对置（Dome）双朝向组串重分配

在工商业平屋顶常见的东-西（或南-北）双坡对置支架（PVsyst 中称为 `Dome`）方案里，同一个子阵列会同时跨接两个背靠背的配对朝向（`Dome` 与 `DomeBackfaces`）。

在 `.VCx` 文件内部，子阵列通过 `NoOrientation=1` 和 `NoOrientation2=2` 绑定这两个配对朝向，但**只显式保存第一面的组串数 `NStrOrient1`**（第二面的组串数由 PVsyst 自动按 `NStringCh - NStrOrient1` 推导，文件里并没有 `NStrOrient2` 字段）。

`pvsyst_create_dome_allocation_variant` 用于在已经配好 `Dome` 双朝向的模板变体上修改第一面的分配串数 `first_orientation_strings`（从而调整两面的组串比例），并另存为一个新的 `.VCx` 变体。

```json
{
  "project": "example.PRJ",
  "source_variant": "VCX",
  "new_variant": "VCZ",
  "first_orientation_strings": 20,
  "variant_name": "Dome Allocation 20-20",
  "dry_run": true
}
```

## 参数要求与适用模板

- `first_orientation_strings`：分配给第一朝向（`NoOrient=1`）的组串数，必须为严格正整数，且保证两面都至少分到 1 串（对于总串数 `NStringCh=40` 的标准模板，允许取值范围为 `1..39`，例如传 `20` 即为 20 串对 20 串等分，传 `39` 即为 39 串对 1 串）。
- 源模板变体需满足 PVsyst 8.1.6 的已分配 `Dome` 结构：
  - `TOrientGroup` 下恰好包含两条互指的 14 字段 `pvDomeOrient` 记录：主面 `FieldType=Dome`（`NoOrient=1`, `NoPairOrient=2`, `IsMainOrient=True`，倾角/方位角 `10/90`）与背坡面 `FieldType=DomeBackfaces`（`NoOrient=2`, `NoPairOrient=1`, `IsMainOrient=False`，倾角/方位角 `10/-90`）。
  - 单子阵列（`NStringCh=40`, `NModSerie=25`, `NInverter=6`, `NInvMPPT=1`），已关联 `NoOrientation=1` 与 `NoOrientation2=2`，无 3D 阴影场景、地平线、P50、负荷或储能。
- 注意：首次从普通固定平面创建 `Dome` 配对需要在 PVsyst GUI 的 Orientation / System 窗口完成（如果在 GUI 里只选了 `Dome` 朝向却没在 System 窗口把组串分给第二面，PVsyst 仿真时会直接报错）。另外，`DomeBackfaces` 指的是对置支架的另一侧坡面，不要与双面组件（Bifacial）的背面发电混淆。
