# 原生AC线缆配置

`pvsyst_create_ac_cabling_variant`从受限原生8.1.6模板预览/创建新VC，首次启用AC线缆或修改已启用的均一线缆。主阵列启用位/摘要、子阵列有效长度与每个直接InverterNode的CableDefinition协调更新，StringNode的DC电阻不变。只序列化原生字段，电阻与损耗仍由PVsyst计算。

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC1","ac":{"length_m":100,"metal":"Copper","section":"ISO60228_10"},"dry_run":true}
```

## 原生所有者与输入

length_m为正数且不超过100000m，须精确0.1m表示；0.01/0.0001等值写入前停止，不舍成零。此为适配器域，不是GUI/厂商工程限值。metal为Copper或Aluminium，section为原生标识ISO60228_10/16/25/35/50/70/95/120，不自行换算电阻。已启用源省略metal/section时继承原值；未启用源省略时采用已记录的Copper/ISO60228_10模板值，不是工程选型建议。

限PV-only、单普通固定平面/子阵、全部组串属于朝向1、完整一致电路计数，8.1.6根/子阵/电路及已证标志族；无显式几何/布局、负荷/储能、P50或保存地平线。main Flags只认01900400/01B00400、Flags2=082A，system Flags=$00。MVNbTransfos须0且无TransformerNode，不在此编辑MV/HV下游电缆或陈旧参考功率。

启用态须同时具备：主阵列bit21、ACWireNoSect=0/ACWireLength=0.0/ACWireMetal，子阵列ACWireLength，以及每个直接逆变器的非custom电缆定义。长度、金属、截面必须在各owner间一致；缺失/重复/混合/部分声明不被覆盖掩盖。非启用态须无主AC摘要和直接逆变器AC电缆、子阵列长度为0。源和候选使用相同检查。

仅在每个InverterNode的Children之前解析直接CableDefinition，嵌套伪装、未知结构、自定义电阻均停止。原生8行CableDefinition/CableType形状保留，不读取或修改下级StringNode的DC CableDefinition。源坐标事务按降序应用，避免先插主摘要后偏移其它owner索引；最终编码bytes重解析并比对期望状态。工作区锁、路径/同名冲突、源快照、新VC独占创建、可选名称/BOM规则保持，失败保留可能的部分产物路径。

公共getter的ac_cabling返回none/active/unsupported、长度、金属/截面及组数和物理逆变器数；例如该原生模型两个表示组NElements=2+4，合计6台，不把两个节点当作两台设备。配置工具的native_consumption_verified始终false，另行原生运行才形成消费证据。

## 已执行验收

长期公共协议契约`tests/contracts/test_ac_cabling_roundtrip.py`使用显式原生文本夹具复制到系统临时目录，CLI是非可执行占位。覆盖预览无写、首次启用、两次再编辑、Aluminium/16继承、公共状态读取、DC Resistance行保持及源字节保留；超精度/微小非零/巨大长度、源电缆不一致、错误版本、MV源均停止。不是生产内部函数测试，不包含商业样本。

新公共入口创建的5组配置由8.1.6内核运行，均无警告、输入字节保持、16个数值列与7页PDF，使用显式2022年、同一SIT/MET。第1组完整8760小时的全部新列与R26 GUI保存的AC100m参考逐点一致；其余仅2022-01-01..02连续有限48h，不外推全年：

| 配置 | 时长 | E_Grid kWh |
|---|---:|---:|
| Copper/10，100m | 8760h | 790029.8099 |
| Copper/10，再改50.1m | 48h | 4505.203 |
| Aluminium/16，50.1m | 48h | 4485.158 |
| Aluminium/16，继承导体截面再改75.2m | 48h | 4460.303 |
| Copper/25，100m | 48h | 4475.670 |

各条请求、公共读回、CSV/PDF和源/新输入保持检查记录于本机`native-migration-r57/native-ac.json`，商业数据不进入仓库。金属与截面组合同时改变时不把差值单独归因某一项；标准标识映射不是所有温度/载流量/设备或厂商限制的工程认证。

独立静态复审通过指定的`ac-review [magpie/mw/deepseek-v4.1-flash-sg]`返回核心契约成立，父逐项裁决并补齐外部边界契约后验收。初次默认通道的复审被停止，随后错误provider别名调用返回null，均不计通过；最终从实际profile注册项核实了SG的准确provider/model，没有回退其它模型，仓库约定也已同步。

复审要求补0.1m正例，父已用真实公共MCP断言0.1创建及读回；0.01/0.0001/100.05拒绝与声明0.1网格一致，并非实现矛盾，数值域保持原定义。复审推测HV逃逸不成立：解析器收集每个TransformerNode，创建器拒绝任意非空变压器列表，与IsHVTransformer布尔和摘要数量独立；新增公共反例把main标志和摘要数量伪装成AC/零变压器，读取仍证实实际HV节点存在，创建停止且无目标文件，不为此加冗余门禁。没有扩成全DC字段或电气验证器。

审查者自述了白名单之外的临时helper执行与大范围变体抽样，缺少可独立核实的工具轨迹；此自述违反限定只读任务，明确不计为测试、原生证据或覆盖保证，记录与父裁决保留于本机`sg-review-adjudication-pending.json`及最终response。受限静态代码意见与父明确白名单公共执行分开表述，不把“862个抽样”等未经复核数字写成通过依据。

定向AC、既有MV和储能公开契约、编译及补丁检查通过，显式环境为本机case的PVSYST_CONTRACT_WORKSPACE，缺该商业文本夹具的环境会skip而不算通过。此前参考线只有首次启用并会把更细长度舍入，本实现重构原生字段继承及再次编辑，不整批搬参考测试或另造线路模型。没有执行全量发现、旧smoke或哈希类测试；现有生产并发保护保持。常驻MCP需重启刷新70工具schema。
