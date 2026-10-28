# 多屋面场景的电气拓扑与 3D 几何对应关系

在 PVsyst 中把一栋多坡屋顶住宅跑通，需要把四层东西对齐：**电气子阵列与固定朝向列表**、**`.pvc2` 3D 场景中的房屋与组件表网格**、**导入 PVsyst 后的原生 PV 表格身份与位置**，以及**最终的 CLI 全年仿真**。

当前全自动打通的双屋面闭环（`anchor0` 单栋双坡屋顶）详见[工程交付中的双屋面说明](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。本文档梳理这几层之间的衔接细节。

## 1. 电气层：固定朝向与子阵列拆分

- 通过 `pvsyst_clone_subarray(..., clone_orientation=true)`、修改 `subarray_updates.NoOrientation` 以及 `pvsyst_remove_subarray(..., remove_orientation=true)`，可以在无阴影固定平面变体上增删朝向并绑定独立子阵列（详见[固定平面生命周期](grid-capacity-verification.md#fixed-plane-lifecycle)）。
- 注意：`pvsyst_clone_subarray` 会把源子阵列的逆变器与组串分支原样复制一份（全站总组件数会翻倍）。如果你是想把原本 36 块组件拆成东西坡各 18 块，记得先用 `pvsyst_update_variant_parameters` 把源子阵列的组串数减半，再去 `clone_subarray`。

## 2. 几何层：从 `.pvc2` 到 PVsyst 原生对象

- **单朝向分块 vs 双朝向分块**：如果两块光伏板装在同一个朝向的屋面上（只是中间隔了个天窗），它们共用同一个电气朝向 ID；如果装在两个不同朝向的坡面上，则需要用[双朝向场景生成](grouped-house-scenes.md)为每张表指定各自的 `orientation_id`（`1` 和 `2`）。
- **为什么不能光靠对象名或顺序认表**：当 `.pvc2` 导入 PVsyst 3D 编辑器后，PVsyst 自动生成的对象名称（`Comment`）和创建序号（`NoObjCreated`）并不保留源 XML 里的 ID。在 `.VCx` 内部，真正决定哪张表归属哪个电气朝向的是 `pvShdArrayTable.NoOrient`（指向 `pvOrient`），而子阵列则通过 `NoOrientation` 指向同一个朝向组。
- **导入偏移与归位**：PVsyst 导入双朝向 `.pvc2` 时，可能会在表格位置上产生厘米级的初始偏移，并多建出一个含 0 个子阵列、0 个三维对象的空朝向组。因此双屋面自动化任务会先读取每张表的原生属性选定朝向 `#1` / `#2`，导出 `.SHD` 核对坐标并用原生位置编辑（`Ctrl+B`）消除偏移，再删掉确认无对象的空朝向组，最后另存为新变体并切离重载核对。
