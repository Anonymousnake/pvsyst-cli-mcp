# 有限双朝向场景与住宅任务

双朝向文件作者／读取器和[受限原生住宅任务](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)是不同层。作者只生成标准PVCollada2及可严格重建声明；自动任务另须满足单栋anchor0、原生逐表身份、归位与保存／重载条件，不扩展任意多组、共享或异构拓扑。

## 来源分组

只接受两个pvSubArray分别引用普通非平均pvOrient1/2，同PAN/OND、完整计数及8.1.0/8.1.6独占完整分支。每组恰好一个命名PV区域，组件数分别匹配该组NModSerie×NStringCh，不只匹配总和。

无活动场景／布局、负荷、储能、P50或地平线，延续北半球和PAN尺寸规则。orientation_id是已有原生朝向的严格整数，两表各使用1/2一次，不是布尔、自选角度或新建朝向。角度来自源；单源省略该字段，分组输入不隐式套到旧平面。

创建完整请求还须提供与实际屋面对应的命名网格house、layout中的表位置／屋面及module_geometry。作者不补房型、尺寸、工程角度或厂家限值。

## 坐标与几何检查

所有房屋顶点和origin_m位于朝向1局部框架。共享SceneRoot保留该朝向的方位旋转，每PV节点在平移后附加相对Z旋转；rack使用对应源倾角及PVCollada方位。变换服务于格式与几何校验，不估算辐照或电气收益。

屋面法线及四角投影使用同一组变换后PV角点。双组XY包围盒重叠时保守停止，是适配器限制，不是通用碰撞认证。更多组、每组多区域或多层投影重叠不由此开放。原单朝向共面矩形重叠检查不变。

## 严格回读

单源写version1，双组写version2，source声明含逐组ID和组件数。读取从实际rack取角度、XML取网格，检查组声明、逐组计数及完整ID集合，重建支持内容并与原XML规范化比较。额外节点、旋转不匹配、坏清单等停止，不放松规范化来吞掉未知内容。

ready表示文件方言／几何输入可重建，不认证工程来源；读取始终source_verified=false。返回create_arguments用于重新创建时，会重新读取实际工程并执行源门禁。合法文件声明被换成另一个project/variant不是来源认证。

创建只写新的UserData场景，不覆盖原文件或修改工程、变体、PAN；组件厚度仍来自声明。作者／读取的导入和原生消费标志为false。原生桥对不支持形状返回needs_context，不按文件元数据推断当前工程匹配。

## 原生任务与身份

受限任务通过原生PV属性、SHD及位置入口核对每表实际朝向和空间状态；自动名称不作为稳定身份。只对明确空组执行删除，原生有效、保存、重载与年度消费分别校验，needs_review保留缺工程依据及保存阴影复核。

native-state的Sommets/TPoly3D点名从1开始，但Faces索引为0..N-1，-1分隔多边形；两者不混用。普通物体局部角度为0时才能直接用局部点加DOrigine比较世界点，不能把此局部条件泛化为通用变换器。逐表NoOrient引用、坐标、尺寸与完整网格一起审查；stored_consistent或相同数量不能替代源几何认证。

版本、DPI、七字符位置精度及0.00002m接受条件详见[交付范围](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。原单朝向、一／两区域、旧version1及固定无阴影路径各自保持支持条件。
