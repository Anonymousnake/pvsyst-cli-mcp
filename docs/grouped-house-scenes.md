# 有限双朝向场景作者

既有 `pvsyst_create_house_pv_scene` 和 `pvsyst_read_house_pv_scene` 新增候选文件的双固定朝向分组，不新增工具、PV/阴影计算或自动桌面流程。原单朝向、命名多区、单表和旧版本1输入保持。**双组自动原生导入明确返回 needs_context，住宅交付任务仍保持原单源边界。**

## 声明现有来源分组

仅允许2个pvSubArray分别引用普通非平均固定pvOrient1/2；同一PAN/OND、完整一致电路计数，现有8.1.0/8.1.6独占完整分支门禁和单朝向字符串分配。每组恰好一个命名PV区域，区域模块数须等于对应NModSerie×NStringCh，不只核对总和。无活动场景/布局、负荷/储能、P50/P90、保存地平线；北半球限定和PAN尺寸元数据规则延续。不是任意共享父、异构设备或拓扑设计入口。

下面是本机实际案例的 `layout` 片段，不是完整请求；调用仍需完整AI命名网格 `house`、源project/variant、output_name及原有module_geometry上下文。相应屋面必须存在于调用方网格中，示例不生成房型。

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

`ready` 表示文件方言/几何输入可重建，不代表工程来源已认证。读取始终 `source_verified=false`，元数据是文件声明；用返回create_arguments重建时会再次读取当前原生源并执行全部源门禁。把合法文件声明换成其它project/variant不是来源认证。生产导入对双组提前 needs_context，native_actions为空，不进入GUI，不根据文件声明推断工程已匹配；现有接口仍会读取CLI版本。无需额外框架或Reader偷偷打开原工程。

创建只写新的UserData/*.pvc2，不覆盖现有文件；项目、变体、PAN保持，模块几何深度仍为调用方声明。所有导入/原生消费标志均false。先前[双根原生研究](multiroof-native-map.md)证明原生入口可执行，但不是本批每PV节点新形状的消费证据；本批没有新GUI或仿真。

## 验证记录

本机 `pvsyst-grouped-scene-study/round1/public-grouped-authoring.json` 记录完整新公开请求/回读/重建/原生停点：76工具，40构件、2组、36模块；源字节保持，零GUI/仿真，只有原生CLI版本查询。保留实际新版本2场景及返回参数，实验资产不上传。

非哈希白名单：`tests/contracts/test_grouped_house_scene_boundary.py`、`test_house_task_boundary.py`、`test_sourced_house_boundary.py`，最终4项通过。覆盖逐组而非仅总数、布尔/未知/重复ID、不同OND声明、投影保守停、非零锚定方位30/120、损坏元数据/旋转/额外XML、旧单源Named格式、公开原生导入停点。旧住宅首次执行夹具目录选错，在copy setup缺文件停止，未进入产品调用；记录失败后定位既有实际夹具，未改测试/标准，重跑通过。必要编译/差异检查另计，未运行全量发现/旧smoke/哈希或no-hash探针。

`grouped-house-scene-review [magpie/mw/deepseek-v4.1-flash-sg]` 无阻断。父裁决：reader不是工程认证器，重建creator重新校验源；reader已有两处完整ID1/2校验；_num只折叠负零，不把-180变零，因此未按错误建议更改正确代码。子审查自报越出给定片段读取pvsyst_grid.py短段及额外variants/MCP片段，已记录范围偏差，不作为这些范围的新增验收；父验证仅以明确公开契约和源码为依据。

下一步继续逐区原生表身份与完整几何对应验证，再扩自动原生导入/任务。既有单固定无阴影、同朝向住宅流程继续可用；不把候选作者完成标成业务全链路交付。
