# 多屋面电气与几何连接

多平面电气、住宅几何、原生表身份和年度运行是独立层。当前自动双屋面仅支持[工程交付限定形状](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)，不是任意多屋面能力。

## 电气层

现有clone_subarray(clone_orientation=true)、NoOrientation和remove_orientation支持[受限固定平面生命周期](grid-capacity-verification.md#fixed-plane-lifecycle)：完整计数、独占分支、普通非平均平面，无活动几何／布局。clone同时复制分支与子阵，会增加数量，不只是增加空平面。

需要保持总数量时，应明确声明并先用既有数量入口调整，再按支持条件复制；不自动推导比例或选型。数量相同也不保证分组前后逐时结果等价。电气生命周期的消费控制限48h，不是3D或全年收益结论。

## 几何层

同朝向一／两PV区域可归属一个源朝向；[双朝向作者](grouped-house-scenes.md)使用两独占来源及每表orientation_id，在朝向1框架内序列化，严格回读与实际原生导入分开。

原生朝向管理器可关联PV区域到系统朝向；必要时只删除已确认0子阵且0三维对象的空组。原生有效、另存及切离重载均须核对，不以拖拽前快照代替重载状态。

原生自动叶名、Comment和NoObjCreated不是源XML ID。pvShdArrayTable.NoOrient连接pvOrient，子阵NoOrientation连接系统组；仅计数和引用仍可能漏掉表互换或位置变化。需要同时核对表身份、原点、角度、尺寸、组件元数据和完整几何。

## 支持与结果

保存声明读取的stored_consistent只证明支持范围内引用和数量一致，不认证源坐标。通用逐区身份／坐标变换仍未建立；非零anchor、任意多组、共享／异构硬件不因成功导入或有年度结果获得支持。

CSV核对实际列和原生单位；缺失请求变量不补零。局部几何等值或两日结果等值不证明全年等价，年量差不单独归因某个房体、方位或损失。受限单栋双屋面流程的严格位置、保存／重载和消费条件以[交付指南](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)为准。
