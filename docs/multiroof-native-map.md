# 多屋面连接链：已证与缺口

本批是原生入口盘点与隔离电气控制，不新增多屋面MCP能力。现有住宅任务仍限单系统朝向、一／两PV区；多固定平面电气源能运行，不代表多屋面的几何关联已完成。

## 两条连接链

| 链路 | 已证入口/证据 | 当前边界 |
|---|---|---|
| 普通多固定平面电气 | 既有clone_subarray(clone_orientation)、NoOrientation、remove_orientation；[8.1.6生命周期](grid-capacity-verification.md#gui-saved-816-exclusive-fixed-plane-lifecycle) | 完整计数、独占分支、普通非平均固定平面、无活动几何/布局；原48h不升级全年 |
| 本次总硬件数量守恒拆分 | 当前公共数量入口先NInverter2→1，再独立复制子阵/平面 | 最终2逆变器/2串/36模块，两子阵各18串联×1串/1逆变器；不是只复制空平面 |
| 本次电气消费 | 相同模型/天气/SFI/两日期间三组CLI原生CSV/PDF | 2024-06-21 00:00至06-22 23:00，共48h，不是全年或3D |
| 房屋同朝向多区 | 既有source_profile、table_profiles、原生导入/关联/表格/另存/任务 | 仍只有一个源pvOrient、同一组角度和根旋转；1..2区均NoOrient=1 |
| 多屋面不同朝向关联 | TOrientManageForm候选入口/动作选择组合框已定位 | 尚未取得多系统树节点身份、每table→原生朝向对应及安全删除空组的实际证据；未实现 |

## 配置不等于只增一个平面

`pvsyst_clone_subarray(..., clone_orientation=true)`连同独占InverterNode/MPPT/String分支复制，会增加当前配置的硬件。静态盘点据此推断“没有总硬件不变的路径”过强：复用已有数量编辑可先明确减半，再复制恢复原总数。

本次隔离源来自已验收的600Wp公开论文完整PAN、原生Generic9kW和LegalTime93的2024 SIT/MET。原源为Grid/子阵Version8.1.0、Flags=$001E0402、PowerSharing=-1，18串联/2串/2逆变器/2显式MPPT/36模块，倾角28、原生方位0。父通过公共入口：

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

此处复用隔离工作区的标准结构，不新增产品workspace初始化器，不改全局INI，也没有GUI会话或强关进程。所有实验/商业/派生文件留本机`pvsyst-multiroof-study/round1`，不上传仓库。

## 原生几何关联还缺什么

source_profile当前只接受1子阵/1pvOrient；table_profiles的每个区都取源倾角与方位，PlacedPVTableLayout没有朝向身份/角度字段。SceneRoot使用一个共同旋转。原生回读验证要求每张pvShdArrayTable.NoOrient=1，因此单纯给电气源添平面不会自动打开多屋面流程。

现有`_link_orientation`从Shift+Ctrl+O进入TOrientManageForm，验证录制尺寸，按坐标在TVirtualStringTree拖拽导入组/源组；只读取TActionSelectionForm当前组合框文字并要求“修改光伏场区的参数”，不枚举多系统的树节点身份。删除空组的行号仍依1..2区录制布局计算。

下一次集中GUI会话需实际取得：多系统/多几何朝向的节点与角度身份；动作组合框完整选项和每次分配意义；不同table→NoOrient的对应；新VC另存/重载、原生表重算及CSV/PDF。复用现有控件/组合框入口，不猜树行号，不patch私有VC NoOrient，不自建阴影/3D计算。候选与持久化/原生消费/MCP状态继续分列。家具、五金、材质渲染及已暂停的Module Layout不增加。

## 路由与裁决

`multiplane-electrical-map [magpie/mw/deepseek-v4.1-flash-sg]`与`multiroof-native-link-map [magpie/mw/deepseek-v4.1-flash-sg]`已完成限定静态盘点，实际加载相应skill，未执行GUI或测试。父以实际公开组合与原生三控制裁决：采纳clone复制硬件/桥接缺少身份的结论；不采纳“总硬件守恒没有路径”及套用旧三子阵案例-2.1833kWh到新二子阵控制的推断。

本批无产品代码或新增永久测试；未跑全量发现、旧smoke、哈希测试或no-hash探针。生产输入守卫按公共编辑操作正常使用。首个临时预检脚本有括号语法错误，在执行前修正，未产生native动作；workspace两次失败不计成功。后续新年度/几何关联仍待各自独立验收。
