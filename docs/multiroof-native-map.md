# 多屋面连接链：已证与缺口

分开记录首批电气48h控制与后续隔离几何/全年观测，不新增多屋面MCP能力。现有住宅任务仍限单系统朝向、一／两PV区；新的两翼研究夹具不等于已交付单栋多屋面业务任务。

## 两条连接链

| 链路 | 已证入口/证据 | 当前边界 |
|---|---|---|
| 普通多固定平面电气 | 既有clone_subarray(clone_orientation)、NoOrientation、remove_orientation；[8.1.6生命周期](grid-capacity-verification.md#gui-saved-816-exclusive-fixed-plane-lifecycle) | 完整计数、独占分支、普通非平均固定平面、无活动几何/布局；原48h不升级全年 |
| 总硬件数量守恒拆分 | 当前公共数量入口先NInverter2→1，再独立复制子阵/平面 | 最终2逆变器/2串/36模块，两子阵各18串联×1串/1逆变器；不是只复制空平面 |
| 首批电气消费 | 相同模型/天气/SFI/两日期间三组CLI原生CSV/PDF | 2024-06-21 00:00至06-22 23:00，共48h，不是全年或3D |
| 房屋同朝向多区 | 既有source_profile、table_profiles、原生导入/关联/表格/另存/任务 | 仍只有一个源pvOrient、同一组角度和根旋转；1..2区均NoOrient=1 |
| 后续双朝向几何研究 | 原生导入/逐叶关联/删除空组/另存/重载/有效性与一次2024原生全年已观测 | 仅40构件/2PV区/2系统朝向研究夹具；源几何逐值等价未完整验证，MCP接口未扩展 |

## 配置不等于只增一个平面

`pvsyst_clone_subarray(..., clone_orientation=true)`连同独占InverterNode/MPPT/String分支复制，会增加当前配置的硬件。静态盘点据此推断“没有总硬件不变的路径”过强：复用已有数量编辑可先明确减半，再复制恢复原总数。

隔离源来自已验收的600Wp公开论文完整PAN、原生Generic9kW和LegalTime93的2024 SIT/MET。原源为Grid/子阵Version8.1.0、Flags=$001E0402、PowerSharing=-1，18串联/2串/2逆变器/2显式MPPT/36模块，倾角28、原生方位0。父通过公共入口：

1. clone_project成独立工程，保留原源。
2. update_variant_parameters的NInverter=1先预览后应用；现有耦合编辑器同步NStringCh/NStrOrient1/NInvMPPT到1及分支NElements到1，不另推算电气分组。
3. clone_subarray(clone_orientation=true)恢复最终2逆变器/2串/36模块，朝向ID1/2及子阵ID1/2均结构有效。
4. 保留两朝向都28/0的VC0；再clone_variant为VC1，只改朝向2的FieldAzim=90。

`electrical-conserved-split.json`保存精确公共请求、dry-run diff、真实写入/计数和源保持；这不是任意比例、共享父或异构设备的容量设计接口。没有猜厂家温度/限值，原实验包络未认证。

## 三组原生控制

本机PVsystCLI8.1.6，显式指定同一SIT/MET/SFI、小时步长和两日期间，三个CSV各48条规则逐时记录，EArray/E_Grid/IL_Pmax均48有限值，生成原生参数/线路/结果PDF。输入项目/变体字节保持。`native-electrical-controls-r5.json`保存三个原生命令回执、汇总、单位与逐点差异。

| 对照 | EArray累计差(kWh) | E_Grid累计差(kWh) | E_Grid最大小时差(kW) |
|---|---:|---:|---:|
| 双子阵同角度 − 原单子阵 | +0.7766 | +0.7567 | 0.1190 |
| 仅朝向2方位90 − 双子阵同角度 | +1.7423 | +1.7385 | 3.0038 |

三个控制IL_Pmax均为0。原单子阵E_Grid为185.3665kWh。**同硬件数量不代表配置或逐时结果等价**；第一项差异随电气分组改变出现，尚未确定原因，不归因到方位角、布线或任何特定损失。PNomAC_InMPPT在两个子阵仍为7.50，未被改写，故也不声称它造成了差异。第二项只改了拆分控制中的一个已知方位角，但仍只是本两日、此模型组合的原生观测，不代表全年增益或最优方案。

## workspace停点

第一次只复制工程/模型/天气等必要文件，第二次补已观察的顶层标准目录，两次都在workspace载入处停止，均未产生可用小时结果。随后只新增已验收工作区的7个组件类别目录（Batteries/Clients/Gensets/Pumps/RegulPmp/Regulators/Sellers），未改任何工程参数，第三次三组原生消费通过。保留r3/r4失败JSON和诊断。只能说补齐这组目录后恢复，不声称已定位每个单独目录的最小必要性。

首批CLI控制复用隔离工作区标准结构，未改全局INI、未开GUI。后续研究GUI临时DataPath变化与恢复另记。所有实验/商业/派生文件留本机`pvsyst-multiroof-study/round1`，不上传仓库。

## 产品桥仍有边界

source_profile当前只接受1子阵/1pvOrient；table_profiles的每个区都取源倾角与方位，PlacedPVTableLayout没有朝向身份/角度字段。SceneRoot使用一个共同旋转。原生回读验证要求每张pvShdArrayTable.NoOrient=1，因此单纯给电气源添平面不会自动打开多屋面流程。

现有`_link_orientation`从Shift+Ctrl+O进入TOrientManageForm，验证录制尺寸，按坐标在TVirtualStringTree拖拽导入组/源组；只读取TActionSelectionForm当前组合框文字并要求“修改光伏场区的参数”，不枚举多系统的树节点身份。删除空组的行号仍依1..2区录制布局计算。本次研究没有解除这些生产门禁，也不放松reader的完整重建/规范化比较。

## 后续原生关联往返

中文PVsyst8.1.6 rev49278、目标窗口DPI96。先用现有clone/remove取得两份单子阵/单朝向源，通过既有序列化各生成完整场景；仅在隔离研究中组合标准XML的两组根节点，第二根平移70m，保留各自0/-90度根旋转。XML解析、唯一ID及引用检查通过后，原生实际导入2PV/40其他对象，仅既有beta提示。是两翼研究夹具，不是新的生成器接口或单栋建筑认证。

原生最初将两个PV区域归入第三个平均朝向组。父在同一Popen所有者会话集中完成：

1. 从属性页读取系统组1=28/0、组2=28/90；两组树标题仍是继承Comment25/20，因此不按标题判实际角度。
2. 原生管理器展开第三组三维叶；Table21逐叶拖到系统1，Table42逐叶拖到系统2。TActionSelectionForm实测两选项为“修改光伏场区的参数”和“修改朝向参数”，本次选前者，不改系统朝向。
3. 两组各1子阵/1PV区/18组件，第三组明确0子阵0三维后仅删除该空组；原生有效性返回“此场景正确”。
4. 复用原生表格/另存入口创建新VC2，不覆盖源；文件回读40pvShdGenObject、2pvShdArrayTable、2pvOrient、2pvSubArray、36组件/2逆变器/2串，角度28/0与28/90。
5. 切到VC0再重新加载VC2，重新打开3D朝向管理器，各组1子阵1PV区、孤立项0，有效性再次通过；正常关闭实际持有的Popen，观察退出后恢复原DataPath，无按checkpointPID关闭。

| 原生表 | NoObjCreated | NoOrient | DInclin / DAzimut | DOrigine |
|---|---|---|---|---|
| 表格42 | 2 | 2 | 28 / 90 | -70.395309,0.800000,7.563670 |
| 表格21 | 3 | 1 | 28 / 0 | -0.800000,-0.395305,7.563670 |

Comment是原生自动名，NoObjCreated也不是源XML id，不能把2/3与42/21当成相同编号。源XML id/name未在表中保留。NoOrient连接pvOrient，子阵NoOrientation连接对应系统组；只核对计数和这两类引用仍会漏表互换、位置或其他几何变化。`native-coordinate-extract.json`仅列出两根、模块深30mm、源局部原点0.8/0.4/7.55484与上述原生原点；坐标轴/锚点转换未完整逐值建立，不把差值当错位，也不声称原始几何完全等价。之后产品连接需每区/原生表/目标朝向及完整几何身份检查，不猜私有字段或自建阴影引擎。

## 一次全年及保留失败

GUI退出及DataPath恢复后，VC2只运行一次2024原生全年，显式shading重算。原生CSV/PDF已返回，复用既有annual验证器检查工程/变体/SIT/MET上下文、全年规则时轴、实际全部数值有限和GlobShd单位。8784小时、E_Grid=32892.3871kWh、EArray=33894.0652kWh、IL_Pmax=43.6967kWh。没有新全年对照，不把差值归因到单一构件、阴影或方向，也不声称最优/工程认证。

辅助断言错误地照前例期待14个数值列，实际13而失败；请求的viILPmxSH未导出，GlobShd存在且为W/m²。不擅补零，不重跑原生，不将“全部请求变量已导出”标通过。原失败保留在`native-multiroof-annual.json`；`native-multiroof-year-audit.json`只读核对实际列、既有完整性结果及保存核心字段，零新原生运行。原计划全字节后置守卫因该辅助断言停止而未执行，因此不作本次全年输入文件全字节不变声明；另证核心计数/角度/引用与GUI保存记录相同。

GUI研究的其他停点也保留：预检辅助脚本误把ready_to_open期待为ready，启动前停止并恢复INI；初始直接调用底层_load_owned绕过已有_physical_coordinates上下文，观察坐标虚拟化792×42而物理门禁990×53；只在该研究线程恢复物理坐标后继续。生产_open/import本来已有正确的线程上下文及finally还原，不是生产DPI缺陷，不重复加实现。打开属性页后的脚本一度等待被禁用的manager而超时，现场保留后明确读取并取消属性页。切离已存场景的原WM_COMMAND 2s通知超时但VC0实际载入；再用同一原生通知载入VC2、30s有界预算实耗0.406s，不凭一次记录全局改超时或静默忽略失败。

## 路由与裁决

`multiplane-electrical-map [magpie/mw/deepseek-v4.1-flash-sg]`与`multiroof-native-link-map [magpie/mw/deepseek-v4.1-flash-sg]`完成首批限定盘点；父不采纳“总硬件守恒没有路径”及套用旧三子阵-2.1833kWh到新二子阵的推断。后续`native-multiroof-identity-map [magpie/mw/deepseek-v4.1-flash-sg]`实际加载ponytail/reverse-engineering后只读检查新持久化证据，未执行GUI或测试。采纳身份/几何等价缺口；不把拖拽前快照当重载后尚未修复，也不采纳“多根只是两个旋转字段”的过度简化。当前接口还需要来源分组、数量/分配、几何/身份及严格回读契约。

本批只有文档变化，无产品代码或新增永久测试；未跑全量发现、旧smoke、哈希测试或no-hash探针。生产输入守卫正常保留。下一批基于上述已证入口实现有限双朝向连接，家具、五金、材质及已暂停Module Layout不增加，未知拓扑/版本继续停止。
