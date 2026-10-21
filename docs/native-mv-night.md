# 原生MV夜间断开

`pvsyst_create_mv_night_disconnect_variant`预览或创建新VC，只切换既有单MV变压器的`NightDisconnect`与主阵列依赖位bit29，可选改新变体名称/BOM。没有变压器损失、参考功率、线缆电阻或等效物理计算，不创建MV/HV树。

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","night_disconnect":true,"dry_run":true}
```

## 已证支持域

复用批准参考51148b9中“两个原生字段协调更新”的映射，重构源/candidate检查，不直接搬运其弱门禁。限8.1.6、PV-only、单普通固定平面/子阵、所有串朝向1、完整电路计数，无显式场景/布局、负荷/储能、P50或保存地平线。Root/main/subarray及电路版本和标志分别检查，原生电路必须直接属于主阵列。

原生R26默认单MV样本的系统标志是`$00`，主阵列为`$01F00400`或`$21F00400`，Flags2=`$082A`。共享门禁接受受信内部调用者提供的固定标志族；Tracking/Shed的默认main01900400/system06000000保持，不清位或伪装视图来绕过检查。源bool必须已是True/False且与bit29一致。

限定原生源保存的20kV单变压器、MVNbTransfos=1与默认参数表；main还须保留VOutTransfo20000、MVWireNoSect2、长度0、Copper及TrfIronLossFrac0.00100/TrfResLossFrac0.01000。唯一TransformerNode恰有7个已证字段，参考功率431.47590、铁损0.36000及分数0.00083/0.01199原样冻结，IsHVTransformer=False。**这些是有证模板条件，不是工程推荐、GUI限值或通用变压器关系**。自定义表、两台变压器、双层HV等仍可读取，但本写入入口停止，不说软件不支持。

Source和实际编码后的candidate均经完整相同检查。依照解析器的明确owner与字段索引，只替换两个目标字段，不全局替换同名Flags。预览无写；执行复用锁、路径、大小写目标冲突、源快照复核及exclusive新建。原PRJ/VC、硬件、AC/DC电缆、MV参数及其它数据保持。新配置`native_consumption_verified=false`不因写出而改为真。

公共getter同时提供main-array原始Flags、MVNbTransfos和VOutTransfo，以及既有Transformer层字段，支持读取后从新VC再修改。本入口仅开关，不重算铁铜损百分比和原生参考功率的关系。

## R56 验收

长期公共协议契约使用显式PVSYST_CONTRACT_WORKSPACE真实原生文本夹具，复制到系统临时目录，CLI为非可执行占位。检查预览/创建/回读、开→关后全文件等于原字节、源保护，以及真实数量2/HV夹具、伪造依赖位、错误版本/系统类型/组串计数被拦。数量2与双层HV的fixture身份先经公共getter确认。没有内部实现绑定测试或商业样本入库。

首轮共享门禁错把后期Tracking的system06000000套到R26 MV的system00，正例在原生执行前被拦。失败保留`native-migration-r56/public-source-profile-failure.json`，以实际原生记录修复为分调用者明确白名单，不扩大其它调用者默认域。

两份由新公共入口创建的OFF/ON分别跑8.1.6原生2022全年，使用同一SIT/MET/SFI。每份8760连续有限小时、16数值列、8页PDF，无原生警告，所有新表的17列（含时间）都与对应R26 GUI源全年结果逐点一致；原PRJ/VC及新输入字节保持。

| 夜间断开 | 年E_Grid kWh | 最低E_Grid kW | 负E_Grid小时 |
|---|---:|---:|---:|
| OFF | 781229.3599 | -0.36 | 4311 |
| ON | 782781.3199 | 0 | 0 |

两态4311小时变化，年量差1551.96kWh。数据是该原生单MV样本的实际结果，不是从固定铁损自行补算或裁剪CSV，也不外推其它变压器、全年气象或HV结构。公开ON→OFF的配置字节精确还原原OFF输入。

证据`native-migration-r56/native-mv-annual.json`、对应CSV/PDF/文本留在独立本机case，商业数据不上传。独立静态复审未见必须修复项；其P2意见指出单纯blocked断言不足以独立确认每个negative隔离原因，父补充公共fixture身份读取，并明确不宣称逐项内部拦截点穷举。实际provider/model未回显，按未知记录，不伪称历史指定路线。定向公共契约、共享域受影响回归及编译通过；未运行全量/旧smoke/哈希类测试。AC写入、任意MV参数、HV构造及原生DC设置另批交付，非本入口功能。常驻MCP需重启刷新新增schema。
