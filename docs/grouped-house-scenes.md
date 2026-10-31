# 双朝向（双屋面）住宅 `.pvc2` 场景生成与回读

在单朝向住宅建模（见[住宅与光伏建模](house-pv-modeling.md)）的基础上，`pvsyst_create_house_pv_scene` 和 `pvsyst_read_house_pv_scene` 支持为包含**两个及以上同质固定朝向（同一 PAN/OND、每个朝向独占一个普通固定平面子阵列）**的住宅工程生成和回读 `version 2` 格式的 `.pvc2` 场景文件。其中一个三固定朝向的单例已用本机 PVsyst 8.1.7 CLI 冷跑观测到原生消费后的 3 组几何计数、`pvModuleLayout` 计算态与 `ShdElec` 结果缓存（仅该单例；精细电气损失的独立精度无 A/B 能量对照、未证；不代表任意 N 或 NoGUI 已完成）。其中**双固定朝向（双屋面）**路径已通过 `pvsyst_run_house_task` 完成单栋双屋面的端到端原生仿真与交付（详见[工程交付中的双屋面章节](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。

## 源变体与输入结构要求

1. **电气与朝向底板**：源 `.VCx` 变体需包含两个及以上子阵列，各自独占关联一个普通非平均固定平面朝向（各朝向 `NoOrient` 为互异正整数），且所有子阵列使用相同的 `.PAN` 组件和 `.OND` 逆变器型号，电路计数完整，无已有 3D 场景、远方地平线、P50、负荷或储能。
2. **逐表绑定 `orientation_id`**：在 `layout.tables` 列表中传入一张或多张 PV 表，每张表除了常规的 7 个排布字段（`module_rows`、`module_columns`、`orientation`、`row_spacing_m`、`column_spacing_m`、`origin_m`、`roof_mesh`）外，还必须显式指定整数 `orientation_id`，取值必须是声明中的真实源朝向 ID（互异正整数，不是固定 `{1,2}` 白名单）；同一个朝向可拆成多张表。
3. **逐组组件数对齐**：不仅总组件数要对上，按 `orientation_id` 分组的各表 `module_rows × module_columns` 之和，还必须严格等于该组子阵列的 `NModSerie × NStringCh`（单表绑定某朝向时即该表本身等于该组总量）；且每个源子阵列都必须至少被一张表覆盖，不得遗漏或凭空新增朝向。

## 局部参考系与几何检查

- **以朝向 1 为局部参考系**：所有房屋构件的顶点坐标 `vertices` 以及各张 PV 表的起点 `origin_m` 均定义在朝向 1（`NoOrient=1`）的局部坐标系下。生成的 `.pvc2` 在公共 `SceneRoot` 节点上挂朝向 1 的方位角旋转，并在每张 PV 表的节点平移后附加相对于朝向 1 的局部 Z 轴旋转，各自的 `rack` 则写入对应朝向的真实倾角与 PVCollada 方位角。
- **屋面法向与防重叠检查**：工具会把每张 PV 表的四个角点变换到同一参考系下，核对承托屋面 `roof_mesh` 的法向是否与对应朝向的倾角/方位角一致、四角竖直投影是否落在屋面三角形内，并检查各张 PV 表的水平 XY 包围盒是否互不重叠。

## Version 2 严格回读与 `.VCx` 内部索引细节

- `pvsyst_read_house_pv_scene` 读取双朝向 `.pvc2` 时，会校验 `version 2` 元数据中的逐组朝向 ID 与组件数，从 XML 还原全部网格和各表的排布参数，并在内存中重建 XML 做归一化比对。
- **`.VCx` 内部 3D 顶点与面索引小贴士**：在 PVsyst 保存进 `.VCx` 的原生 3D 对象（`Sommets` / `TPoly3D`）中，顶点字段名是从 1 开始编号的（`Point_1`..`Point_N`），但 `Faces` 数组里的顶点下标是从 **0** 开始的（`0..N-1`），并用 `-1` 作为多边形面片之间的分隔符。解析原生 `.VCx` 场景状态时切勿把两套基数搞混。
