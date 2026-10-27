# 原生AC线缆配置

`pvsyst_create_ac_cabling_variant`从受限8.1.6模板预览或创建新VC，首次启用AC线缆或修改已启用的均一线缆。主阵列启用位与摘要、子阵列有效长度、直接InverterNode的CableDefinition协调更新，StringNode的DC电阻保持。电阻与损耗由PVsyst计算。

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","ac":{"length_m":100,"metal":"Copper","section":"ISO60228_10"},"dry_run":true}
```

## 输入

`length_m`须为正数、不超过100000m，且可精确表示为0.1m；更细精度会停止，不舍入。这是适配器范围，不是GUI或厂家工程限值。

`metal`为Copper或Aluminium；`section`为ISO60228_10/16/25/35/50/70/95/120。已启用源省略导体或截面时继承；未启用源省略时使用Copper/ISO60228_10模板值，不构成工程选型建议。适配器不按截面自行计算电阻。

## 支持范围

仅支持PV-only、单普通固定平面／子阵、全部组串属于朝向1、完整一致电路计数的8.1.6源。main Flags为01900400或01B00400，Flags2=082A，system Flags=$00。MVNbTransfos须为0且无TransformerNode；不支持显式几何／布局、负荷／储能、P50或保存地平线，也不编辑MV/HV下游电缆。

启用态须同时具有主阵列bit21、ACWireNoSect=0、ACWireLength=0.0及ACWireMetal，子阵列ACWireLength和每个直接逆变器的非custom电缆定义。各所属对象的长度、金属、截面必须一致。非启用态须无主AC摘要和直接逆变器AC电缆，子阵列长度为0。缺失、重复、混合或部分声明会停止。

只接受InverterNode直属、位于Children之前的原生8行CableDefinition/CableType结构。未知结构、自定义电阻和嵌套伪装停止；下级StringNode的DC CableDefinition不修改。最终编码后的候选重新解析并核对状态。

## 输出与文件

参数读取中的`ac_cabling`返回none/active/unsupported、长度、金属、截面、组数和物理逆变器数量。组的NElements参与物理数量，不将节点数当设备数。

预览不写文件；创建使用工作区锁、受控路径、同名冲突检查、源快照复核及新VC独占创建。失败时披露可能的部分产物。`native_consumption_verified`始终false，实际效果需另行原生运行。导体或截面映射不替代温度、载流量、保护设备及厂家限制校核。
