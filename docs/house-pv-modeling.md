# 房屋与光伏：PVCollada 2.0

`pvsyst_create_house_pv_scene(project, variant, output_name, house, layout, dry_run=True, module_geometry=None)`

创建外部 `.pvc2` 场景输入：一栋矩形房屋和一张固定光伏板块。支持平顶与对称双坡屋顶，不包含树木、周边建筑、地形或自动排布。所有遮挡、辐照和发电量由 PVsyst 原生计算，生成器只写几何、组件元数据和标准关系。

本工具不是原生 VC 编辑器，也不执行 GUI 导入。文件写入成功、原生导入成功、原生仿真成功是三个独立状态；返回 `imported=false`、`native_consumption_verified=false`，不把本机参考方案的验收外推到每次新输入。

## 输入

| house 字段 | 单位 | 含义 |
|---|---|---|
| `length_m`, `width_m` | m | 矩形房屋的局部 X / Y 长宽 |
| `eave_height_m` | m | 檐口高度；平顶时即屋面高度 |
| `roof.kind` | — | `flat` 或 `gable` |
| `roof.pitch_deg` | ° | 仅双坡，严格大于 0 且小于 90；须与源光伏倾角一致 |

双坡屋脊平行局部 X，位于宽度中线，光伏放在前坡。返回 `roof_rise_m` 是屋脊相对檐口的升高量，绝对屋脊标高为 `eave_height_m + roof_rise_m`。

| layout 字段 | 单位 | 含义 |
|---|---|---|
| `module_rows`, `module_columns` | 个 | 严格正整数，乘积须等于源方案的 `NModSerie × NStringCh` |
| `orientation` | — | `portrait` 或 `landscape` |
| `row_spacing_m`, `column_spacing_m` | m | 模块边缘间距，非负 |
| `clearance_m` | m | 板块低边相对屋面的竖直净距，非负 |
| `edge_margin_m` | m | 局部 X 的退距；局部 Y 在平顶按水平退距、双坡按坡面退距解释 |

所有字段显式传入，不自动摆满、不推断工程间距。倾角和方位来自源方案的唯一固定朝向，布局不接受另一个倾角/方位覆盖值。

| module_geometry 字段 | 单位 | 含义 |
|---|---|---|
| `model_name` | — | 组件实际型号，不从文件名猜测 |
| `nom_power_wp` | Wp | 名义功率 |
| `width_m`, `length_m` | m | 组件宽度、长度 |
| `geometry_depth_m` | m | 显式几何厚度，不推断厂家规范 |

可读 PAN 优先从工作区、再从已配置内置松散文件库读取。根 `LargApp`/`LongApp` 与商业信息块 `Width`/`Height` 都按已有米制声明解析，重复/冲突停止；型号、功率和尺寸须与调用方提供的元数据一致，写前复查源字节。

原生初始数据库引用不一定有独立 PAN 文件。此时必须显式提供 `module_geometry`，返回 `metadata_source=caller-provided-for-native-database-reference`；这不是数据库或厂家已校验声明。源有可读 PAN 时也要显式提供几何厚度，因此当前完整 `module_geometry` 对实际创建仍是必需输入；参数默认空只会返回具体缺项。

## 示例

下列数值是本机验证夹具，不是住宅设计推荐。原方案含 1000 块组件，所以对应大屋面；住宅项目应绑定其自身的组件数量和硬件，不照搬本例。

```json
{
  "project": "ROOFTOP_IMPORT.PRJ",
  "variant": "VCD",
  "output_name": "Roof_Gable.pvc2",
  "house": {
    "length_m": 120,
    "width_m": 60,
    "eave_height_m": 8,
    "roof": {"kind": "gable", "pitch_deg": 15}
  },
  "layout": {
    "module_rows": 20,
    "module_columns": 50,
    "orientation": "landscape",
    "row_spacing_m": 0,
    "column_spacing_m": 0,
    "clearance_m": 0.3,
    "edge_margin_m": 2
  },
  "module_geometry": {
    "model_name": "Mono 440 Wp 72 cells bifacial",
    "nom_power_wp": 440,
    "width_m": 1.052,
    "length_m": 2.131,
    "geometry_depth_m": 0.035
  },
  "dry_run": true
}
```

确认预览后使用 `dry_run=false`，只新建 `UserData/<output_name>.pvc2`，源 PRJ/VC/PAN 不写。默认预览不创建文件或目录；禁止覆盖、大小写同名、越界路径、未知键、布尔/字符串充当数字及非法 XML 字符。失败披露产物路径，保留原生检查提示。

## 原生导入流程

PVsyst 8.1.6 实测路径：近处遮挡 → 建模/透视图 → 文件 → 导入 → `Import a 3D scene (3DS, DAE, PVC, PVC2, PVZ2)`。核对米制尺寸、模块数量以及完整导入摘要；此版本始终提示 PVC2 支持为 beta。

本机导入会创建新的朝向，即使屏幕上倾角/方位与原有朝向相同。仅显示模型尚不满足系统关联：

1. 在三维编辑器按 `Shift+Ctrl+O` 打开原生朝向管理工具。
2. 将新朝向的三维场区拖到原系统朝向；原生提示参数差异时，本机参考选择“修改光伏场区的参数”，不启用平均朝向。
3. 删除已空的新朝向，不删除光伏场或系统子阵；核对原朝向同时包含系统和三维场区。
4. 关闭场景，核对组件数量、面积和朝向兼容性，使用原生表格/重算，再另存新方案并重载。
5. 对新方案使用既有 `pvsyst_run_simulation(..., recompute_shading=true)`，保留实际 CSV 列、单位、PDF 和所有警告。

这组原生绑定步骤尚未被宣称为一个自动导入 MCP 工具。`pvsyst_create_house_pv_scene` 不调用未证实的 CLI 导入参数、不直接写场景私有位，也不放宽旧固定式无阴影编辑器的门控。

## 格式与范围

- COLLADA 1.5，几何米制、`Z_UP`；PV 扩展命名空间 `https://pvcollada.org/2026/XMLSchema`、profile `PVCollada-2.0`。`pv:module` 的长宽厚为毫米，只有该处做单位转换。
- 房屋是无 PV 标签的普通封闭 mesh；平顶 8 顶点/12 三角，双坡 10 顶点/16 三角。PVsyst 实测分别保存为 6 面、7 面原生通用物体，并不保持三角分面形式。
- 房屋与光伏共用父节点 Z 旋转，光伏平移在房屋局部系中。源 PVsyst 方位转为地理北 0 顺时针：`(180 + FieldAzim) % 360`，本批限定北半球。
- 四角几何包容检查、模块计数、有限数值和屋面净距检查不是遮挡或电气模型。当前六位小数坐标表达不接受会被写成零的微小非零值。
- 源侧仅一个普通固定平面、一个 Grid 子阵、完整电路计数、内嵌站点；活动场景/布局、负荷/储能/P50/已存地平线停止，不悄悄保留不一致映射。
- Module Layout/电气阴影、其它朝向和跟踪类型、DC 耦合与电网充电、特殊原生负荷、随机电网停运依用户要求暂缓；不作为本建模阶段的能力声明。

## 验证记录

新 stdio 会话公开 65 个工具，建模工具属于 `Projects & Variants`。公共预览和两种屋顶文件创建通过，44 个错误输入均未写入新文件；横放非零间距、竖放与 Unicode 大写扩展名通过；并发同名请求一建一阻。原源文件及最初导出文件字节保持不变，未运行全量或哈希类测试。

固定版本官方 XSD 与 structure/references/business 三组 Schematron 分别验证原版样例、平顶、双坡均通过。原生导入摘要均为 1 个光伏对象 + 1 个普通物体，只有 beta 提示；站点、海拔和组件尺寸不匹配提示均未出现。关联后两种屋面均为 1000 块、2241.81m²、15°/0°，原生保存并重载通过。

另用同一公开接口产物验证非零间距和竖放：行/列间距各 0.02m 原生保存为 `ModSpacingX=0.020`、`ModSpacingY=0.020`，板块横向/坡向尺寸约 107.53/21.42m；竖放 25×40 原生保存为 `moPortrait`、42.08/53.275m。两组均仅有 beta 导入提示，系统关联、原生表格和重载通过，未把它们另记为全年仿真。

房体原生“启用阴影投射”已核对勾选；另存同几何关闭对照，仅 Comment 与房体投射位变化，未以私有位写入实现开关。两态已存直射系数表相同，未声称此屋面场景已产生非零房体遮挡损失。

原生 CLI 8.1.6 对无场景基线、平顶、双坡、同几何房体投影关闭四态分别使用 `recompute_shading=true`，2022-01-01 至 12-31 的 8760 个逐小时时间戳均完整、连续、数值有限。基础方案导出 13 个数值列与 7 页 PDF；三组场景方案各导出 14 个数值列与 8 页 PDF，报告明确 `Linear shadings : Fast (table)`，新增场景透视及等阴影图页。

| 原生方案 | 年并网电量 kWh | 说明 |
|---|---:|---|
| 无场景基线 | 799980.9366 | 不导出 GlobShd，不将缺列补成零 |
| 平顶房屋/PV | 799819.6410 | 含实际 GlobShd 列，PDF Produced Energy 为 799820kWh/year |
| 双坡房屋/PV | 799819.6410 | 8760 小时/14 列与平顶逐项完全相同 |
| 双坡、房体投影关闭 | 799819.6410 | 与开启状态逐项相同，不归因未证的房体损失 |

CSV 的 `E_Grid` 原生单位为 kW；以上年度电量按已证 1 小时步长积分，未重命名结果单位。共有列只在相同时间戳和相同单位下比较：场景相对基线年差 -161.2956kWh、`GlobInc` 全年相同；不把总差值宣称为房屋投影的因果效果。四次运行输入原件字节不变、无原生警告，GUI 会话正常关闭且完整 INI 已逐字节恢复。

临时验证器的 SDK 超时类型错误、已创建 SFI 的无覆盖停止、结果分析对缺列/日期类型的初始错误均保留本机记录；修正验证器后复用原始输入完成上述结果，没有删除/覆盖失败证据，也没有在生产代码中加入自动重试。

参考：[PVsyst PVCollada 支持说明](https://www.pvsyst.com/help/project-design/shadings/near-shadings-import/pv-collada-file-format.html)、[原生朝向关联](https://www.pvsyst.com/help/project-design/shadings/near-shadings-orientations/orientation-management-window.html)、[规范与 Schema](https://github.com/pvlib/pvcollada/tree/68ad4388e6b6e3b3f8646767eb74c954312650af)。本机工程、组件、天气、教程及商业二进制不随仓库分发。
