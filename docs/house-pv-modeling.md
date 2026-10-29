# 住宅与屋顶光伏 3D 场景建模（PVCollada 2.0 / `.pvc2`）

在 PVsyst 8.1.6 中，3D 近阴影场景支持导入基于 COLLADA 1.5 扩展的 PVCollada 2.0（`.pvc2`）XML 格式。本模块提供三个递进的工具来管理住宅 3D 场景：
1. `pvsyst_create_house_pv_scene`：将结构化的房屋三维网格（`house`）与屋顶光伏排布（`layout`）序列化为工作区 `UserData` 下的 `.pvc2` 文件。
2. `pvsyst_read_house_pv_scene`：从已有的 `.pvc2` XML 中无损解析并还原出当初的 `house` 和 `layout` 参数，方便在此基础上微调尺寸后另存新场景。
3. `pvsyst_import_house_pv_scene`：默认以 `method="gui"` 驱动已打开的 PVsyst 8.1.6 GUI 导入 `.pvc2` 文件、绑定屋顶朝向、校验几何有效性并计算阴影表，最后另存为新 `.VCx` 变体；或以 `method="native"` 在不启动 GUI、不运行仿真的情况下构造新的原生输入变体（见第 5 节）。

如果要从头到尾一键跑完“生成场景 → 启动 GUI 导入另存 → 退出 GUI → 调 CLI 跑全年仿真”，请直接使用高层任务入口 [`pvsyst_run_house_task`](native-house-task.md)（双坡屋顶场景另见[双朝向住宅场景](grouped-house-scenes.md)）。

## 1. 构建房屋网格（`house`）

推荐使用显式三角网格模式（`kind="mesh"`），一次性传入墙体、多坡屋面、挑檐、门窗外框、烟囱、门廊或附属体量等构件列表 `meshes`：
- 每个构件包含：`name`（`1..120` 字符，忽略大小写唯一）、`vertices`（局部米制 `[x, y, z]` 坐标列表，保留 6 位小数）和 `triangles`（零基顶点索引 `[i, j, k]` 列表）。
- 容量上限：单场景最多 128 个构件、50,000 个顶点、100,000 个三角形。
- 几何自检：序列化前会自动检查退化三角形（共线或面积过小）、越界索引等错误。若发现问题，会在 `issues` / `diagnostics` 中返回精确到构件与三角形下标的定位（如 `house.meshes[3].triangles[7]`）及修复建议 `repair_hint`。
- 向后兼容参数化模式：仍支持旧版的简单矩形平顶（`roof.kind="flat"`）或双坡顶（`roof.kind="gable"`）参数化输入。

## 2. 布置屋顶光伏板（`layout` 与 `module_geometry`）

### 单区域与多区域排布
- **单区域（单张组件表）**：直接在 `layout` 中指定 7 个字段：`module_rows`（行数）、`module_columns`（列数）、`orientation`（`"portrait"` 竖放或 `"landscape"` 横放）、`row_spacing_m`、`column_spacing_m`、`origin_m`（局部起点 `[x, y, z]`）以及承托该阵列的屋面构件名 `roof_mesh`。
- **同朝向多区域（`tables` 列表）**：当同一个朝向的组件分块摆在屋顶不同位置（例如被烟囱或天窗隔开成左右两块）时，使用 `layout.tables` 列表（作者/读取器接受 1..64 个分区；GUI 自动导入桥最多 1..2 个分区；低层 `method="native"` 线性导入对单朝向 `1..3` 个分区提供存储读写，其有限 3 表 profile 已经公开 product 导入 + CLI 完成真实原生消费验收，见第 5 节；高层 `pvsyst_run_house_task` 仍只接受双朝向）。每个分区带独立的 `name` 和上述 7 个字段，且所有分区的 `module_rows × module_columns` 累加和必须等于源变体子阵列的总组件数（`NModSerie × NStringCh`）。

几何校验规则：
- 所选 `roof_mesh` 必须是法向朝上且倾角/方位角与源变体固定朝向一致的共面屋面网格。
- 光伏阵列四角的竖直投影必须落在 `roof_mesh` 三角形并集内部且不低于屋面，共面多分区之间不能互相重叠（否则返回 `pv_tables_overlap`）。

### 组件几何尺寸（`module_geometry`）
传入 `model_name`、`nom_power_wp`、`width_m`、`length_m` 和 `geometry_depth_m`（组件边框厚度，米）。若工作区有明文 `.PAN`，工具会自动核对长宽尺寸是否与 `.PAN` 一致；因为 `.PAN` 核心参数里不一定带边框厚度，所以创建场景时仍需显式给出 `geometry_depth_m`。

## 3. 无损回读与修订（`pvsyst_read_house_pv_scene`）

调用 `pvsyst_read_house_pv_scene(scene_name)` 会解析 `UserData/<scene_name>.pvc2` 中的真实 XML 网格与 PV 支架节点，并用解析出的参数在内存里重新生成一份 XML 与原文件做归一化比对，确保没有漏掉任何未知节点或手改变换。

返回的 `create_arguments` 字典可以直接拿来修改（比如把烟囱高度从 `6.4 m` 改成 `7.2 m`），然后换一个新的 `output_name` 调 `pvsyst_create_house_pv_scene` 生成修订版场景。

## 4. GUI 自动导入与关联（`pvsyst_import_house_pv_scene`）

当 PVsyst 8.1.6 rev49278（中文界面，96 DPI）已经在桌面上打开了匹配的源工程且无未保存弹窗时，调用 `pvsyst_import_house_pv_scene(scene_name, variant_name, dry_run=False, confirm=True)` 会自动执行以下步骤：
1. 打开 3D 近阴影编辑器（要求标准窗口尺寸 `1475×899`），导入 `.pvc2` 场景（支持 1 或 2 个 PV 分区）。
2. 打开朝向管理窗口（`Shift+Ctrl+O`，尺寸 `1126×826`），将场景中的 PV 场区关联到系统朝向，并清理导入产生的多余空朝向组。
3. 执行原生几何有效性检查与阴影因子表计算，最后通过 `SaveAs` 另存为新 `.VCx` 变体并回读校验。

关于 PVsyst 官方对 PVCollada 格式和朝向管理窗口的说明，可参考 [PV Collada file format](https://www.pvsyst.com/help/project-design/shadings/near-shadings-import/pv-collada-file-format.html) 与 [Orientation management window](https://www.pvsyst.com/help/project-design/shadings/near-shadings-orientations/orientation-management-window.html)。

## 5. 无 GUI 原生导入（`method="native"`）

`pvsyst_import_house_pv_scene` 默认仍为 `method="gui"`（第 4 节，行为与字段含义保持不变）；`method="native"` 需显式选择。它从同一份受支持配方在当前工作区构造一个新的原生输入变体：不查阅、不激活、不启动 GUI，不读写 `PVsyst.ini`，不用剪贴板，不启动 `PVsyst.exe`，不消耗许可证，也不运行仿真。该“写入输入文件”步骤不启动任何 GUI 或 CLI 进程，不涉及窗体。

受支持的源配方边界固定（PVsyst 8.1.6、北半球）。线性模式下，双朝向模板已原生验证；单朝向 `1..3` 表模板为本轮新增低层实现、存储读取自洽（`stored_consistent`），其有限 3 表 profile 已用公开 product 导入 + CLI 完成真实原生消费验收（仅限该有限 3 表 profile，不表示 `1..64`、全部模板或高层单朝向已支持，也不作逐表独立归因）：

- 双朝向、两张横装零间距组件表：源子阵 `1`/`2` 各一条独立组串/逆变器/MPPT，同一 `PAN`/`OND`，各分区组件数累加须严格等于源变体组件总数。
- 单朝向、单子阵、`1..3` 张横装零间距组件表：每张表都归同一电气朝向（`NoOrient=1`），子阵列可含多串，各表组件数累加须严格等于源子阵的 `NModSerie × NStringCh`。`1..3` 是 Native 导入阶段当前支持的存储上限；其中 3 表 profile 已完成真实原生消费验收（有限 3 表 profile）。底层作者/读取器仍接受 `1..64` 张表，但 `1..64` 是作者能力，不等于 Native 已支持 `1..64`。

来源不匹配、元数据未知或声明相互冲突时，在写入前停止并给出原因，不做暗中修正。负方位角按源 `FieldAzim` 的有符号约定写入（`-45` 即 `-45`，不写成 `315`）。精细模式（`shading_mode="module_layout"`）仅对双朝向模板写入与布局配套、已单独验证的完整精细阴影状态（`pvShading.Flags=$20100900`）；单朝向场景的 `module_layout` 在写入前被拒绝。线性模式写入 `$20000100`；不对其它取值或单个比特的内部语义作推断，也不外推到本模板之外的配方。本次扩展不新增用户参数或开关。

`dry_run=true` 仅在内存中构建并校验原生记录，不写任何文件；可返回明确的预检状态（如 `status="ready_to_import"` 及 `verification` 明细），但 `stored_geometry_verified` 与 `stored_module_layout_verified` 均为 `false`（尚未写盘）。实际写入需 `dry_run=false` 且 `confirm=true`：只新增一个 `.VCx` 变体，不改动源变体、`PAN`/`OND`。

成功时 `status="native_variant_created"`，返回新变体的 `project`、`variant` 与 `file`（以及源 `source_variant`）。此时只表示“已写入并通过存储记录校验”：`stored_geometry_verified` 在执行成功后才为 `true`，`stored_module_layout_verified` 仅在执行成功且确已写入并核对布局记录后才为 `true`（线性模式无布局记录，该项不适用）。`stored_*` 只说明写入的记录正确，`imported`、`native_consumption_verified`、`native_geometry_verified`、`native_module_layout_verified` 均为 `false`——PVsyst 尚未加载或消费它；必须再用 `pvsyst_run_simulation` 对该返回变体显式运行，才能证明原生消费。未运行仿真不会自动消耗许可证。

```python
result = pvsyst_import_house_pv_scene(
    scene_name="my_house_scene",
    variant_name="Native Import",
    method="native",
    shading_mode="module_layout",
    dry_run=False,
    confirm=True,
)
# result["status"] == "native_variant_created"
# result["project"], result["variant"], result["file"] 指向新建的原生输入变体
```

窗体事实：`method="native"` 的建文件步骤不启动 GUI/CLI、不涉及窗体；但随后用 CLI 运行仿真（`pvsyst_run_simulation`）时，仿真流程自身会用到隐藏的 `TForm`/消息窗体。`pvsyst_import_house_pv_scene(..., method="native")` 无需 GUI 操作、无需 `PVsyst.exe`、通常没有可见窗体，但这不等于“零窗体”或“无桌面依赖”。该说明属于 CLI 仿真流程本身，不限于 `method="gui"`。
