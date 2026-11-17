# 住宅与屋顶光伏 3D 场景建模（PVCollada 2.0 / `.pvc2`）

在 PVsyst 8.1.6 / 8.1.7 中，3D 近阴影场景支持基于 COLLADA 1.5 扩展的 PVCollada 2.0（`.pvc2`）XML 格式。本模块提供三个递进的工具来管理住宅及周边环境遮挡 3D 场景：
1. `pvsyst_create_house_pv_scene`：将结构化的房屋三维网格（`house`）、屋顶光伏排布（`layout`）、可选逐格接线（`string_layout`）以及周边遮挡物（`obstacles`：`building`、`tree`、`wall`、`mesh`）序列化为工作区 `UserData` 下的 `.pvc2` 文件。
2. `pvsyst_read_house_pv_scene`：从已有的 `.pvc2` XML 中无损解析并还原出当初的 `house`、`layout`、`string_layout` 与 `obstacles` 参数，方便在此基础上微调尺寸后另存新场景。
3. `pvsyst_import_house_pv_scene`：默认以 `method="native"`（免 GUI 原生构建，见第 5 节）在不启动 PVsyst 桌面窗口、不运行仿真的情况下构造新的原生输入 `.VCx` 变体（含 `pvShdArrayTable`、`pvShdGenObject` 与原生 `oeTree` 树木对象 `pvShdElemObject`）；仅当用户显式指定旧版未维护的 `method="gui"` 时才走桌面 GUI 导入路径（第 4 节）。

如果要从头到尾一键跑完“生成场景 → 无 GUI 原生写入 `.VCx` → 调 CLI 跑全年阴影仿真”，请直接使用高层任务入口 [`pvsyst_run_house_task`](native-house-task.md)（双坡屋顶场景另见[双朝向住宅场景](grouped-house-scenes.md)）。

## 1. 构建房屋网格（`house`）

推荐使用显式三角网格模式（`kind="mesh"`），一次性传入墙体、多坡屋面、挑檐、门窗外框、烟囱、门廊或附属体量等构件列表 `meshes`：
- 每个构件包含：`name`（`1..120` 字符，忽略大小写唯一）、`vertices`（局部米制 `[x, y, z]` 坐标列表，保留 6 位小数）和 `triangles`（零基顶点索引 `[i, j, k]` 列表）。
- 容量上限：单场景最多 128 个构件、50,000 个顶点、100,000 个三角形。
- 几何自检：序列化前会自动检查退化三角形（共线或面积过小）、越界索引等错误。若发现问题，会在 `issues` / `diagnostics` 中返回精确到构件与三角形下标的定位（如 `house.meshes[3].triangles[7]`）及修复建议 `repair_hint`。
- 向后兼容参数化模式：仍支持旧版的简单矩形平顶（`roof.kind="flat"`）或双坡顶（`roof.kind="gable"`）参数化输入。

## 2. 布置屋顶光伏板（`layout` 与 `module_geometry`）

### 单区域与多区域排布
- **单区域（单张组件表）**：直接在 `layout` 中指定 7 个字段：`module_rows`（行数）、`module_columns`（列数）、`orientation`（`"portrait"` 竖放或 `"landscape"` 横放）、`row_spacing_m`、`column_spacing_m`、`origin_m`（局部起点 `[x, y, z]`）以及承托该阵列的屋面构件名 `roof_mesh`。
- **同朝向多区域（`tables` 列表）**：当同一个朝向的组件分块摆在屋顶不同位置（例如被烟囱或天窗隔开成左右两块）时，使用 `layout.tables` 列表（作者/读取器接受 1..64 个分区；GUI 自动导入桥最多 1..2 个分区；低层 `method="native"` 线性导入对单朝向 `1..3` 个分区提供存储读写，其有限 3 表 profile 已经公开 product 导入 + CLI 完成真实原生消费验收，见第 5 节；高层 `pvsyst_run_house_task` 与低层共用同一原生门，单朝向按已证窄模板或显式 `string_layout` 接线，见第 5 节）。每个分区带独立的 `name` 和上述 7 个字段，且所有分区的 `module_rows × module_columns` 累加和必须等于源变体子阵列的总组件数（`NModSerie × NStringCh`）。

几何校验规则：
- 所选 `roof_mesh` 必须是法向朝上且倾角/方位角与源变体固定朝向一致的共面屋面网格。
- 光伏阵列四角的竖直投影必须落在 `roof_mesh` 三角形并集内部且不低于屋面，共面多分区之间不能互相重叠（否则返回 `pv_tables_overlap`）。

### 组件几何尺寸（`module_geometry`）
传入 `model_name`、`nom_power_wp`、`width_m`、`length_m` 和 `geometry_depth_m`（组件边框厚度，米）。若工作区有明文 `.PAN`，工具会自动核对长宽尺寸是否与 `.PAN` 一致；因为 `.PAN` 核心参数里不一定带边框厚度，所以创建场景时仍需显式给出 `geometry_depth_m`。

## 3. 周边环境遮挡物（`obstacles`）

`pvsyst_create_house_pv_scene`、`pvsyst_run_house_task` 与 `pvsyst_run_sourced_house_task` 均接受可选的 `obstacles` 列表（`1..64` 个，构件名在全场景忽略大小写唯一且不与 `house.meshes` 重名），支持 4 种遮挡体类型：
- **`building`**：邻近建筑体量（`origin_m`、`length_m`、`width_m`、`height_m`、可选 `rotation_deg` 与 `roof` 平顶/`gable` 双坡顶），在 `.pvc2` 与 `.VCx` 中生成带三角网的遮挡体（`pvShdGenObject`）。
- **`wall`**：围墙/女儿墙实体（`start_m`、`end_m`、`height_m`、可选 `thickness_m` 默认 `0.2`），生成长方体遮挡网格（`pvShdGenObject`）。
- **`mesh`**：自定义任意闭合/非退化三角网格遮挡物（`name`、`vertices`、`triangles`，`pvShdGenObject`）。
- **`tree`**：参数化树木（`origin_m`、`top_height_m`、`middle_height_m`、`bottom_height_m`、`trunk_height_m`、`crown_diameter_m`、`trunk_diameter_m`、可选 `rotation_deg`）。在 `.pvc2` COLLADA 中生成可视化树冠树干代理网格并持久化准确参数；在 `method="native"` 原生导入写入 `.VCx` 时，直接发射 PVsyst 原生 `pvShdElemObject`（`ObjetElemType=oeTree`、`Flags=$02200004`、`ColorSurf=$8000`、`ColorBord=$275E`、`Source=osPVsyst`、`Params=top,mid,bot,trunk_h,crown_d,trunk_d,`）。

## 4. 无损回读与修订（`pvsyst_read_house_pv_scene`）

调用 `pvsyst_read_house_pv_scene(scene_name)`（`scene_name` 须以 `.pvc2` 结尾，如 `my_house_scene.pvc2`）会解析 `UserData/<scene_name>` 中的真实 XML 网格、遮挡节点与 PV 支架节点，并用解析出的参数在内存里重新生成一份 XML 与原文件做归一化比对，确保没有漏掉任何未知节点或手改变换。

返回的 `create_arguments` 字典包含 `house`、`layout`、可选 `string_layout` 与 `obstacles`，可以直接拿来修改（比如把烟囱高度从 `6.4 m` 改成 `7.2 m` 或调整树木冠径），然后换一个新的 `output_name` 调 `pvsyst_create_house_pv_scene` 生成修订版场景。

## 5. 旧版 GUI 自动导入（`method="gui"`，未维护）

当明确需要走桌面 UI 且 PVsyst 8.1.6 rev49278（中文界面，96 DPI）已经在桌面上打开了匹配的源工程时，可显式传入 `method="gui"`（该路径已冻结为旧版遗留外壳，不再新增特性或测试；`obstacles` 请使用默认的 `method="native"`）。

关于 PVsyst 官方对 PVCollada 格式和朝向管理窗口的说明，可参考 [PV Collada file format](https://www.pvsyst.com/help/project-design/shadings/near-shadings/near-shadings-import/pv-collada-file-format.html) 与 [Orientation management window](https://www.pvsyst.com/help/project-design/shadings/near-shadings-orientations/orientation-management-window.html)。

## 6. 默认无 GUI 原生导入（`method="native"`）

`pvsyst_import_house_pv_scene` 默认使用 **`method="native"`**（完全解耦自 `native/scene.py`，不引用 `user32` 窗口脚本）。它从同一份受支持配方在当前工作区构造一个新的原生输入变体：不查阅、不激活、不启动 GUI，不读写 `PVsyst.ini`，不用剪贴板，不启动 `PVsyst.exe`，不消耗许可证，也不运行仿真。该“写入输入文件”步骤不启动任何 GUI 或 CLI 进程，不涉及窗体。

受支持的源配方要求 PVsyst 8.1.6 / 8.1.7、北半球项目，具体支持模式包括：
- 双朝向两张横装零间距组件表：源子阵 `1`/`2` 各一条独立组串/逆变器/MPPT，同一 `PAN`/`OND`，各表组件总数匹配源变体。
- 单朝向单子阵 `1..3` 张横装零间距组件表：每张表归同一电气朝向（`NoOrient=1`），各表组件总数匹配源子阵 `NModSerie × NStringCh`。
- 单朝向、单子阵、恰两张横装零间距组件表且子阵含两条源组串和两台独立逆变器（`NStringCh=2`、`NInverter=2`、`NInvMPPT=2`）时，`method="native"` + `shading_mode="module_layout"` 会按声明序绑定源全局组串 `1`/`2`。

来源不匹配、元数据未知或声明冲突会在写入前拦截。负方位角按源 `FieldAzim` 有符号约定写入（`-45` 保留为 `-45`）。精细模式（`shading_mode="module_layout"`）写入 `pvShading.Flags=$20100900`，线性模式写入 `$20000100`。

**显式逐格接线（`string_layout`）**：可提供与 `layout` 同级的 `string_layout` 块逐 module cell 指定全局组串（`row`/`col` 1 基、`global_string>=0`、`subarray_id` 为对应源子阵 id）。组串支持跨表，单表也可包含多串，每条全局串的容量等于所属源子阵的 `NModSerie`。`global_string=0` 为未分配草稿，可在 `pvsyst_create_house_pv_scene` 与 `pvsyst_read_house_pv_scene` 中读写（`string_layout_state="draft_incomplete"`），在原生导入前需补全分配；重复 cell、越界行列号或超容等无效输入会在写入前拦截，显式接线仅在 `method="native"` + `shading_mode="module_layout"` 下使用。

`dry_run=true` 仅在内存中构建并校验原生记录，不写任何文件；可返回明确的预检状态（如 `status="ready_to_import"` 及 `verification` 明细），但 `stored_geometry_verified` 与 `stored_module_layout_verified` 均为 `false`（尚未写盘）。实际写入需 `dry_run=false` 且 `confirm=true`：只新增一个 `.VCx` 变体，不改动源变体、`PAN`/`OND`。

成功时 `status="native_variant_created"`，返回新变体的 `project`、`variant` 与 `file`（以及源 `source_variant`）。此时只表示“已写入并通过存储记录校验”：`stored_geometry_verified` 在执行成功后才为 `true`，`stored_module_layout_verified` 仅在执行成功且确已写入并核对布局记录后才为 `true`（线性模式无布局记录，该项不适用）。`stored_*` 只说明写入的记录正确，`imported`、`native_consumption_verified`、`native_geometry_verified`、`native_module_layout_verified` 均为 `false`——PVsyst 尚未加载或消费它；必须再用 `pvsyst_run_simulation` 对该返回变体显式运行，才能证明原生消费。未运行仿真不会自动消耗许可证。

```python
result = pvsyst_import_house_pv_scene(
    scene_name="my_house_scene.pvc2",
    variant_name="Native Import",
    method="native",
    shading_mode="module_layout",
    dry_run=False,
    confirm=True,
)
# result["status"] == "native_variant_created"
# result["project"], result["variant"], result["file"] 指向新建的原生输入变体
```

窗体事实：`method="native"` 的建文件步骤不启动 GUI/CLI、不涉及窗体；但随后用 CLI 运行仿真（`pvsyst_run_simulation`）时，仿真流程自身会用到隐藏的 `TForm`/消息窗体。`pvsyst_import_house_pv_scene(..., method="native")` 无需 GUI 操作、无需 `PVsyst.exe`、通常没有可见窗体，但这不等于“零窗体”或“无桌面依赖”。该说明属于 CLI 仿真流程本身，不限于 `method="gui"`。另外，PDF 报告的生成并不等于全程无 GUI：在本机对照中，损失图 PDF 在拒绝窗口创建的权限下同页 fatal（默认/生产路径的损失图 PDF 仍为负），只有在授予窗口创建权限时才产出 1 页损失图 PDF——这仅证明原 PDF renderer 存在、加强“损失图 PDF 生成需窗口创建权限”的权限情境关联，不证明损失算法物理上必需窗口，也不代表全部/多页/批处理 PDF 均可无 GUI 生成。

## 6. 编码边界（待跟踪）

本机 PVsyst 8.1.7 在将旧 8.0 源工程另存时，会把含度数符号 `°` 的 `Comment` 从源 UTF-8 写成 `Â°`（UTF-8 字节被按 cp1252 双重解码），这是 PVsyst 导出器自身的问题而非本工具改写所致。读取端保持原字节不变，不做隐式替换，避免掩盖原始编码状态。对于旧 `8.0` 源文件的编码处理，目前已知导出结果带 UTF-8 BOM、CRLF 且度数符号被双重编码，后续若明确源 `DataVersion` 与 BOM 写入的对应关系再单独修正；当前不改变其他模块的编码策略。
