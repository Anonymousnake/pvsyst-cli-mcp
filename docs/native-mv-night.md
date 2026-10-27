# 原生MV夜间断开

`pvsyst_create_mv_night_disconnect_variant`预览或创建新VC，只切换既有单MV变压器的`NightDisconnect`与主阵列bit29，可选修改新变体名称和BOM。不创建MV/HV树，不重算变压器损失、参考功率或线缆电阻。

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","night_disconnect":true,"dry_run":true}
```

## 支持范围

仅支持8.1.6、PV-only、单普通固定平面／子阵、全部组串属于朝向1、完整电路计数，且无显式场景／布局、负荷／储能、P50或保存地平线。电路须直接属于主阵列；root/main/subarray及电路版本、标志均检查。

系统Flags=$00，main Flags为$01F00400或$21F00400，Flags2=$082A。源NightDisconnect必须为True/False且与bit29一致。此范围不改变其他编辑器的标志要求。

仅接受保存的20kV单变压器、MVNbTransfos=1及默认参数表。main须保留VOutTransfo=20000、MVWireNoSect=2、长度0、Copper及TrfIronLossFrac=0.00100／TrfResLossFrac=0.01000。唯一TransformerNode必须具有完整七字段：参考功率431.47590、铁损0.36000、损失分数0.00083／0.01199等保持，IsHVTransformer=False。这些是受支持模板条件，不是工程推荐或通用变压器关系。

自定义表、多台变压器和双层HV可由读取入口披露，但此配置入口会停止。

## 文件与结果

源和实际编码候选使用相同检查，只替换所属对象的两个目标字段，不全局替换同名Flags。预览不写；创建复用工作区锁、受控路径、大小写目标冲突检查、源快照复核及独占新建。原PRJ/VC、硬件、AC/DC电缆、MV参数及其他数据保持。

参数读取提供main-array Flags、MVNbTransfos、VOutTransfo和Transformer字段，可从已创建VC继续修改。`native_consumption_verified=false`；写出不等于原生加载或仿真。该入口不配置AC、任意MV参数、HV或DC线缆。
