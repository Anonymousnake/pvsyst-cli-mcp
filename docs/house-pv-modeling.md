# 住宅与屋顶光伏 3D 场景建模（PVCollada 2.0 / `.pvc2`）

在 PVsyst 8.1.6 中，3D 近阴影场景支持导入基于 COLLADA 1.5 扩展的 PVCollada 2.0（`.pvc2`）XML 格式。本模块提供三个递进的工具来管理住宅 3D 场景：
1. `pvsyst_create_house_pv_scene`：将结构化的房屋三维网格（`house`）与屋顶光伏排布（`layout`）序列化为工作区 `UserData` 下的 `.pvc2` 文件。
2. `pvsyst_read_house_pv_scene`：从已有的 `.pvc2` XML 中无损解析并还原出当初的 `house` 和 `layout` 参数，方便在此基础上微调尺寸后另存新场景。
3. `pvsyst_import_house_pv_scene`：驱动已打开的 PVsyst 8.1.6 GUI 导入 `.pvc2` 文件、绑定屋顶朝向、校验几何有效性并计算阴影表，最后另存为新 `.VCx` 变体。

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
- **同朝向多区域（`tables` 列表）**：当同一个朝向的组件分块摆在屋顶不同位置（例如被烟囱或天窗隔开成左右两块）时，使用 `layout.tables` 列表（支持 1..64 个分区，不过注意后面的 GUI 自动导入桥目前最多支持 1..2 个分区）。每个分区带独立的 `name` 和上述 7 个字段，且所有分区的 `module_rows × module_columns` 累加和必须等于源变体子阵列的总组件数（`NModSerie × NStringCh`）。

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
