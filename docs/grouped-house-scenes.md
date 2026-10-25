# 有限双朝向场景与住宅任务

**当前阶段3已完成受限公共原生验收：**一栋18×10m共脊双坡、ID1/2、同PAN/OND及anchor0的任务完成逐表关联、原生归位、严格保存/重载空间核对、一次2024年8784h原生运行及成套ZIP。保存VC1、9房屋mesh/2PV/36组件，任务stage完成、native_consumption_verified=true，仍保留缺工程依据与保存阴影复核的needs_review。真实结果和门槛集中见[工程交付阶段3](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)，不放开非零anchor、任意多组或异构/共享拓扑，也不宣称工程认证。

本页下述作者与40构件研究记录按其历史批次保留。初始 `pvsyst_create_house_pv_scene` / `pvsyst_read_house_pv_scene` 批次只增加双固定朝向候选文件和严格回读；当时双组自动导入返回needs_context，住宅任务仍限单源。原单朝向、命名多区、单表和旧版本1输入保持，不借后续成果回写当时验收。

## 声明现有来源分组

仅允许2个pvSubArray分别引用普通非平均固定pvOrient1/2；同一PAN/OND、完整一致电路计数，现有8.1.0/8.1.6独占完整分支门禁和单朝向字符串分配。每组恰好一个命名PV区域，区域模块数须等于对应NModSerie×NStringCh，不只核对总和。无活动场景/布局、负荷/储能、P50/P90、保存地平线；北半球限定和PAN尺寸元数据规则延续。不是任意共享父、异构设备或拓扑设计入口。

下面是历史两体量研究案例的 `layout` 片段，不是一栋双屋面住宅或完整请求；调用仍需完整AI命名网格 `house`、源project/variant、output_name及原有module_geometry上下文。相应屋面必须存在于调用方网格中，示例不生成房型。本轮单栋住宅使用新的共脊网格，不复用70m间隔体量作单栋声明。

```json
{
  "tables": [
    {"name":"Source plane A","orientation_id":1,"module_rows":3,"module_columns":6,"orientation":"landscape","row_spacing_m":0,"column_spacing_m":0,"origin_m":[0.8,0.4,7.55484],"roof_mesh":"PV roof"},
    {"name":"Source plane B","orientation_id":2,"module_rows":3,"module_columns":6,"orientation":"landscape","row_spacing_m":0,"column_spacing_m":0,"origin_m":[70.4,-0.8,7.55484],"roof_mesh":"B_PV roof"}
  ]
}
```

`orientation_id` 是已有原生朝向标识的严格整数，非布尔/自选角度；两表必须各取1/2一次。角度来自各自源，不猜工程角度或厂家限值。单源输入省略该字段，明确的分组字段不自动套用到旧单平面。

## 坐标与验证

全部房屋顶点、各 `origin_m` 都保持朝向1的房屋局部坐标。共享SceneRoot保留朝向1的原有方位旋转，各PV节点在自己的平移后附加源相对Z旋转；rack仍使用对应源倾角和PVCollada方位。标准坐标/格式变换只服务序列化与几何校验，不估计辐照/遮挡/电气收益。

屋面法线、四角投影使用同一变换后的PV角点。双组XY投影包围盒相交时保守停止，这只是适配器限制，不是“物理相交已证”或通用碰撞引擎。多层投影重叠、更多组和每组多个区域未由此放开。原单朝向的共面矩形重叠检查不变。

## 严格回读与停点

单源仍写版本1；新双组写版本2，source声明增加每组id/模块数。读取器从实际rack取角度、从XML读取网格并核对组声明、逐组计数和完整ID集合，重建全部支持内容后与原XML规范化比较。额外节点、改动PV旋转却未匹配完整输入、损坏组清单均停止；不放松canonicalize以吞掉未知内容。

`ready` 表示文件方言/几何输入可重建，不代表工程来源已认证。读取始终 `source_verified=false`，元数据是文件声明；用返回create_arguments重建时会再次读取当前原生源并执行全部源门禁。把合法文件声明换成其它project/variant不是来源认证。初始作者批次的生产导入对双组提前needs_context，native_actions为空，不进入GUI，不根据文件声明推断工程已匹配；该历史停点不等于本轮受限任务已验收。无需额外框架或Reader偷偷打开原工程。

创建只写新的UserData/*.pvc2，不覆盖现有文件；项目、变体、PAN保持，模块几何深度仍为调用方声明。所有导入/原生消费标志均false。先前[双根原生研究](multiroof-native-map.md)证明原生入口可执行，但不是本批每PV节点新形状的消费证据；本批没有新GUI或仿真。

## 验证记录

本机 `pvsyst-grouped-scene-study/round1/public-grouped-authoring.json` 记录完整新公开请求/回读/重建/原生停点：76工具，40构件、2组、36模块；源字节保持，零GUI/仿真，只有原生CLI版本查询。保留实际新版本2场景及返回参数，实验资产不上传。

非哈希白名单：`tests/contracts/test_grouped_house_scene_boundary.py`、`test_house_task_boundary.py`、`test_sourced_house_boundary.py`，最终4项通过。覆盖逐组而非仅总数、布尔/未知/重复ID、不同OND声明、投影保守停、非零锚定方位30/120、损坏元数据/旋转/额外XML、旧单源Named格式、公开原生导入停点。旧住宅首次执行夹具目录选错，在copy setup缺文件停止，未进入产品调用；记录失败后定位既有实际夹具，未改测试/标准，重跑通过。必要编译/差异检查另计，未运行全量发现/旧smoke/哈希或no-hash探针。

`grouped-house-scene-review [magpie/mw/deepseek-v4.1-flash-sg]` 无阻断。父裁决：reader不是工程认证器，重建creator重新校验源；reader已有两处完整ID1/2校验；_num只折叠负零，不把-180变零，因此未按错误建议更改正确代码。子审查自报越出给定片段读取pvsyst_grid.py短段及额外variants/MCP片段，已记录范围偏差，不作为这些范围的新增验收；父验证仅以明确公开契约和源码为依据。

该作者批次结束时，自动导入/任务尚缺逐区原生表身份与源几何核对入口。后续研究及本轮受限任务分别记录；候选作者完成不等于业务全链路交付，通用身份验证器仍未由此建立。既有单固定无阴影、同朝向住宅流程继续可用，不回写前批执行状态。

## 新候选的原生空间与两日复核

后续在中文8.1.6rev49278、目标DPI96上，父使用既有原生入口集中操作一次真正持有Popen的会话。由现有公开API克隆独立GROUPED_ROOF_NATIVE.PRJ、重建源声明匹配的新版本2候选；所有输入先存独立字节副本。新文件为单SceneRoot、烘焙B侧建筑网格与每PV节点相对旋转，不借旧双根文件的年度消费作证明。

原生实测导入2PV/40其他对象，仅beta提示。系统属性实际28/0与28/90；继承的Comment25/20仍不是角度来源。本次自动叶名41/42（旧双根是21/42），这直接说明名称不是稳定身份。父按现场叶将41关联系统1、42关联系统2，选择原生“修改光伏场区的参数”；确认第三组0子阵0三维后才删除。原生有效、新VC2另存、切到VC0再载回VC2、两组各1系统子阵/1PV/18组件与孤立项0、重载有效再次通过。随后正常关闭实际Popen，观察退出后恢复DataPath；没有按checkpointPID关闭。

| 验证层 | 观测结果 | 限定意义 |
|---|---|---|
| 两PV表空间/引用 | 原点与旧原生参考的残差均0；倾角/方位、NoOrient、模块配置及其他非自动字段相同；B表LargShed差0.000001m | 该案例未把两张表的空间位置/目标朝向互换；不是按自动名称匹配 |
| 40原生普通构件 | 所有DInclin/BaseSlope/DAzimut为0；局部顶点加DOrigine后比较，最大坐标残差0.000008m；有向多边形连接、其余原生字段一致 | 保留实际残差，不设工程容差，不归因某个内部浮点实现，不做源几何认证 |
| 新配置两日消费 | 2024-06-21 00:00..06-22 23:00，48h、13实际数值列全部有限，GlobShd为W/m²，CSV/PDF上下文匹配 | 不是全年；本批没有新年度运行 |
| 已有结果复用 | 对比旧原生年度CSV的同日期48h切片，13列在各自导出精度内逐点零差；EArray193.2471kWh、E_Grid187.1823kWh、IL_Pmax0 | 没有为对照额外跑年度，也不外推全年等价或最优 |
| 输入/恢复 | GUI退出时既有输入完整存为before-CLI副本；所覆盖文件CLI后字节差异为空，新SFI命令参数另留，Popen已退出/INI已恢复 | 与旧年度辅助断言导致守卫未执行分开；此次差异检查确已运行，但不把后建SFI伪称已在副本中 |

### 字段与比较方法

原生普通物体的Sommets/TPoly3D头声明点数，Point_1..N名字从1开始，但实存Faces包含0，因此面顶点索引是0..N-1；-1分隔多边形。复用现有objects取所属字段，再核对头点数、Point集合、面整数个数NPElements/面数NElements和索引范围，不把曲线Profile.points当3D网格读取器。仅在本案例所有普通物体局部角度0的前提下，加DOrigine得到原生世界点；按空间候选建立顶点一一对应，比较保留绕序的面循环（允许起点/面次序不同，不反转），保留残差和非自动字段差异。对两张表按NoOrient连接真实pvOrient并同时比原点、角度、尺寸及模块元数据。不是哈希/名称签名，也未形成长期私有格式框架。

**证据→结论→下一步**：native-owner-r2的原生操作/PNG/另存重载记录→新形状持久化已证；native-spatial-comparison.json→仅此参考与新配置的空间/连接观测；native-48h-control.json及原生命令/CSV/PDF→新配置两日消费已证。参考和目标原生文件、全部输入副本留本机pvsyst-grouped-native-study/round1，商业/派生资产不上传。该批产品writer/read不把单案例结果作为每次输出的imported/native_consumption_verified，当时自动import仍needs_context；跨任意网格/角度/版本的身份验证器未因此实现。

### 停点与裁决

研究快照枚举时有已销毁窗口句柄，GetWindowRect失败发生在第二个属性页采集，未拖拽；随后改用已确认的目标窗口采集。恢复脚本一度把保留的系统2属性标作系统1，相关旧标注不计系统1证据；后来读取明确#1页取得28/0，#2页28/90，原失败和新确认图分开保留。没有静默计通过或修改产品DPI代码。

`native-spatial-reference-map [magpie/mw/deepseek-v4.1-flash-sg]` 实际加载ponytail/reverse-engineering做限定只读盘点；父采纳局部点/表引用和单案例局限，不采纳“Faces从1开始”、把继承Comment20当实际FieldAzim、把烘焙建筑网格误说成烘焙rack的推断。父实存索引0与解析/比较结果为准。未新增产品代码/永久测试；没有执行全量发现、旧smoke、哈希类测试或no-hash探针，临时解析/驱动完成清理，失败环境/输入/复演序列保留。
