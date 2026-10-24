# 读取保存的PV区域关联

现有 `pvsyst_get_variant_parameters` 的活动 `shading_scene` 新增 `pv_bindings`。它读取保存文件并核对PV表→朝向→子阵的声明，不启动软件、写工程或证明原生计算。没有新增工具。**stored_consistent 不等于源几何一致；原生DOrigine也不是房屋/PVCollada坐标。**

调用该工具的参数示例（该工程来自本机已保存研究案例）：

```json
{"project":"GROUPED_ROOF_NATIVE.PRJ","variant":"VC2"}
```

`shading_scene` 原有 `mode/flags/object_count/object_types/values` 保留。活动场景新增内容包含：

| 字段 | 内容 | 使用边界 |
|---|---|---|
| pv_bindings.tables | 原生对象路径、所属字段原文、orientation_id、module_count、origin_m | Comment/NoObjCreated是自动名称/创建计数；object_path是该文件中的位置，不是跨文件稳定身份 |
| pv_bindings.groups | 各朝向原文、关联子阵ID/表路径、电气与场景组件数 | 比较保存的数值引用，不认证电路分支拓扑或真实工程来源 |
| pv_bindings.coordinate_frame | 原生DOrigine，单位米 | 明确非房屋/PVCollada坐标；不猜轴/锚点修正，不自动投影/移动 |
| source_geometry_verified / native_consumption_verified / generic_geometry_decoded | 均false | 不借既有仿真或单参考结果给任意新输入背书 |

## 状态与支持形状

`stored_consistent`：已支持的保存声明中，PV表引用存在、倾角/方位/底面倾角等于目标固定朝向，逐朝向场景组件数等于对应子阵NModSerie×NStringCh之和。它仍不说明PV表在正确屋面、坐标没互换、物理参数有效或输入得到原生消费。

`needs_review`：读取成功，但存在缺失引用、角度差异或逐组组件数量差异；issues指出具体对象/朝向。

`unsupported`：超出形状、关键字段缺失/重复、非有限/不可用的坐标等；即使PV声明核对停止，原有活动场景仍是active，不伪装empty。已采集原文保留，重复字段可显示多值。

当前只映射1..2张native8.1.6/osPVCollada的pvShdArrayTable、1..2普通非平均固定朝向、1..2子阵及普通物体。未知物体类型、其它版本/导入来源、平均/跟踪平面不据此放开。通用物体网格不解码；不实现3D编辑器、阴影算法、坐标近似模型或自动修复。

空场景沿用原返回，不新增pv_bindings。仅把表坐标改为[300,400,500]而保持引用/数量时，stored_consistent仍可能成立；返回的原生坐标会如实变化，但source_geometry_verified始终false。这一反例已纳入公共契约，避免把声明核对当作来源/空间认证。

## 验证与复演

非哈希白名单为 `tests/contracts/test_native_scene_bindings_boundary.py` 与既有 `test_native_parameters.py`；后者保持不动，回归场景计数/分组/缺席/损坏状态。新契约：新stdio MCP会话读取真实保存双朝向配置；验证空场景、角度/底面倾角/数量错配、缺引用、重复字段、非有限坐标、不支持版本/物体、重复子阵、移动坐标的非认证边界和原始Comment保持。伪CLI文件为不可执行内容，证明读取过程不依赖原生调用。成功检查输入字节与目录成员不变，失败保留隔离夹具。

首次负例的辅助写法在Windows把原有CRLF再次转换，先触发“Variant has no pvVCalcul closing tag”，尚未进入目标损坏字段检查。失败夹具 `pvsyst-native-bindings-a0fm_cxh` 保留；只改测试为按原编码写字节，不改变产品守卫或目标。最终白名单2项通过，必要编译/差异检查另计；没有全量发现、旧smoke、哈希测试或no-hash探针。

本机 `pvsyst-native-bindings-study/round1/public-saved-binding-review.json` 另记录真实单双朝向保存项目公开读取：GROUPED_ROOF_NATIVE.VC2为2组、SOURCED_RESIDENTIAL_HOUSE.VC1为1组，均stored_consistent；输入字节保持，零CLI/GUI/仿真。原生模式的既有持久化/计算证据见[分组场景](grouped-house-scenes.md)，不由本读取接口重复运行或升级。

下一步仍是源XML空间与原生区域的独立对应，以及GUI中的可核实逐区身份入口；自动分组导入继续needs_context。本核对只提供可审查的保存数据和一致性差异，为后续连接保留真实缺口。
