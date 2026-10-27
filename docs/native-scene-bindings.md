# 读取保存的PV区域关联

`pvsyst_get_variant_parameters`在活动`shading_scene`中返回`pv_bindings`，核对保存的PV表、朝向和子阵声明。它不启动软件、不写工程，也不证明原生计算。**stored_consistent不等于源几何一致，DOrigine也不是房屋或PVCollada坐标。**

```json
{"project":"HOUSE.PRJ","variant":"VC1"}
```

## 返回内容

`shading_scene`保留mode/flags/object_count/object_types/values。活动场景另含：

| 字段 | 内容 | 使用边界 |
|---|---|---|
| `pv_bindings.tables` | 对象路径、所属字段原文、orientation_id、module_count、origin_m | Comment和NoObjCreated不是稳定身份；object_path仅定位当前文件 |
| `pv_bindings.groups` | 朝向原文、关联子阵ID／表路径、电气与场景组件数 | 比较保存引用，不认证电路拓扑或工程来源 |
| `pv_bindings.coordinate_frame` | 原生DOrigine，单位米 | 不猜轴或锚点修正，不自动投影或移动 |
| `source_geometry_verified` / `native_consumption_verified` / `generic_geometry_decoded` | 均false | 不用旧运行替任意新输入背书 |

## 状态与范围

`stored_consistent`表示受支持声明中PV表引用存在，倾角、方位和底面倾角等于目标固定朝向，逐朝向场景组件数等于对应子阵NModSerie×NStringCh之和。它不说明PV表在正确屋面、坐标未交换、物理参数有效或已被仿真消费。

`needs_review`表示存在缺失引用、角度或逐组数量差异，issues定位对象与朝向。`unsupported`表示形状超范围、关键字段缺失／重复或坐标非有限等；原场景仍为active，不伪装成empty，已读取原文及重复值保留。

仅支持1..2张native8.1.6/osPVCollada的pvShdArrayTable、1..2普通非平均固定朝向、1..2子阵及普通物体。不支持其他版本／导入来源、平均／跟踪平面或未知物体；通用网格不解码，不自动修复。

空场景不添加pv_bindings。仅改变表坐标而保持引用和数量时，stored_consistent仍可能成立，source_geometry_verified始终false。源坐标与身份限制见[场景身份说明](native-scene-identity-map.md)；受限双屋面运行见[工程交付](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)，不将保存声明核对升级为几何认证。
