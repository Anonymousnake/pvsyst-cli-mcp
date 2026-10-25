# 教程原生入口地图

## 推进顺序与判定口径

地图建立于 PR #36 / `6115503`，后续限定实现按各行记录更新；运行证据来自 PVsyst CLI/GUI 8.1.6。采用“全量盘点 → 按功能组确认原生持久化与消费 → 分批实现 → 原生对照验收与简洁英文 PR”的顺序。本地图覆盖所提供的 83 页并网教程，但不表示每项都已有编辑器或已验收。

原生证据与 MCP 实现必须分列：

| 原生证据等级 | 含义 | 下一阶段门槛 |
|---|---|---|
| 候选 | 教程、符号、窗口或字段线索，尚未建立可靠绑定 | 用 GUI 单变量操作确认保存位置、模式、单位、开关和依赖 |
| GUI 持久化已证 | 原生界面操作与保存内容已对应 | 用原生 CLI 检查实际消费；保存成功不等于运行生效 |
| 原生消费已证 | 已有匹配输入的小时/报告或其他原生运行证据 | 注明版本、场景、时段、输出及失败记录；不把 48h 外推为全年 |

MCP 状态另记“已有受限入口 / 尚无专用配置入口 / 已验收实现”。已有工程可运行，不等于可以新建或编辑该功能；结构计数有效，也不等于电气或物理有效。`needs_review` 不能悄悄升级为完整验收。

## 基础闭环：第 1、2、4 章

| PDF 页 / 功能 | 现有 MCP 入口 | 已证范围与待补事项 |
|---|---|---|
| 5–10：项目、变体、源绑定 | `pvsyst_inspect_project`、`pvsyst_clone_project`、`pvsyst_clone_variant`、`pvsyst_update_project_sources`、项目/变体参数工具 | [基础闭环](tutorial-grid-cycle.md) 已验收固定式示例的原生全年、CSV/PDF上下文；复制保留模型和快照，工程适用性另证 |
| 8–10：站点、气象与年份 | `pvsyst_create_site`、`pvsyst_convert_meteo`、`pvsyst_prepare_weather`、`pvsyst_acquire_weather`、`pvsyst_review_weather` | 原生 SIT/MET 绑定与已验收示例消费；其他提供商/时基/转换警告按各自证据判定，不把文件名当年份或气候真实性 |
| 8–10：MET 年历预检 | `pvsyst_simulate_template`、`pvsyst_simulate_design` | PR #35 已合并：原生错年失败促成 owning-header 预检；通用1990/缺失/未知日期仍显式待审，头部不证明实际小时或时区 |
| 17–21：组件、逆变器与引用 | `pvsyst_project_components`、组件读写/复制/引用工具、`pvsyst_run_simulation` | 原始加密数据库模型已有全年/PDF证据；任意新 loose PAN/OND 需重新引用运行，避免简短模型遮蔽原始库 |
| 17–21：子阵、串并联、MPPT、共享 | 变体参数、`pvsyst_validate_variant_structure`、`pvsyst_clone_subarray`、`pvsyst_remove_subarray` | 既有受限拓扑入口；按[拓扑与原生对照记录](grid-capacity-verification.md)的具体模式/版本/几何条件使用，不把图形计数或缓存额定值当成运行功率 |
| 8–10、33：全年执行、小时结果与报告 | `pvsyst_build_sfi`、`pvsyst_run_simulation`、`pvsyst_read_results`、`pvsyst_read_rows`，模板及统一设计的report_pages | 原2022/8760h记录保留；R52三条2024/8784h原生路径验证默认7页、模板2页、设计1页，三份CSV完全一致；页选择走原生-rp，不事后裁PDF，见[报告通道](automatic-simulation.md) |

基础闭环已验收不代表高级编辑器齐全。原始组件、天气、教程、商业二进制和本机实验产物不随本地图分发。

## 工程任务组织

[既有工程变更与交付](engineering-delivery.md)组合已有读取器、固定参数/跟踪新VC入口及原生CLI年度运行；新增工程交接、运行时输入记录、方案比较与成套ZIP，不新增物理计算入口或放开几何门禁。固定28→30度与既有UnlimTracking轴5.1→6.1各一次2024原生全年已完成；固定基准另一次全年用于有绑定比较，原生CSV/PDF及输入保持分项记录。多屋面自动逐区关联继续按其证据推进；AC光储专题另见下条，不由本条的PV-only结果升级。

[AC光储工程专题](engineering-storage.md)已组合既有自消费系统/负荷/标量入口，明确时基与真实缓存绑定后对3kW及2kW充电AC上限各一次2024原生全年、比较和成套ZIP。两例负荷逐点、SOC和完整AC能量流通过；不套纯并网E_Grid PR、不新增调度模型、不由此升级混合DC端口或任意策略。

## 功能组地图

四个功能组分别盘点第 3、5–10 章。其静态发现只用于定位；共享字段、模式冲突和原生消费确认之前，暂停新增相关 MCP 配置代码。下表经父代理裁决，不直接照搬组报告中的旧覆盖矩阵或缺项计数。

### 详细损失：第 5 章

| 教程节 / PDF页 | 功能与现有入口 | 原生证据 / MCP状态 | 共享依赖与下一验证 |
|---|---|---|---|
| 5.1 / 22–23 | 常数热损系数 Uc：`main_array_updates.UFactor`，W/m²/K | 已有受限入口；[Uc29 GUI载入/另存及24h原生对照已证](loss-gui-roundtrip.md)，不外推全年或GUI改写后的标志可再次编辑 | 主阵列 owning 字段及限定Flags/Flags2；范围15–100，非原生全范围，不解释未证低值行为 |
| 5.1 / 22–23 | 风速项 Uv、热模式/吸收率/效率 | Uv2原生保存为pvMainArray.UVFactor；原生8760h风速全0，输出与控制相同，非零风速正向作用待验 | [整窗与全年批次](native-detailed-losses.md)；与Uc分开，不因支持Uc就宣称完整热模型编辑 |
| 5.1 / 23 | NOCT规格比较 | 教程说明不是实际仿真输入 | 记录为比较/元数据项，不发明仿真设置 |
| 5.2.1 / 23–24 | DC线路电阻、损失比例、长度/截面工具 | [R60/61原生DC薄MCP已验收](native-dc-cabling.md)：两态及跨模式再改、SaveAs/重载，四公共产物各8760h/16数值列完整同独立GUI控制 | 仅已开中文8.1.6/DPI96单普通固定子阵、无AC/MV/HV等受限源；保留源与非DC物理记录，全部阻值由原生GUI计算，比例反算不搬；任意多阵/线路长度截面仍未升级 |
| 5.2.2 / 24–26 | AC线路、参考功率、MV/HV变压器、铁铜损及夜间断开 | [R57均一AC线缆多owner/读改闭环已验收](native-ac-cabling.md)，Cu100新8760h同GUI、另四组48h；[R56单默认MV夜间开关](native-mv-night.md)两态新全年同GUI | 不推导参考功率或电阻；AC在MV/HV下游、任意变压器参数/构造仍停，数量/HV/自定义表仍只读；旧[AC限值交互](native-auxiliary-power.md)仍限24h |
| 5.3.1 / 26 | 组件质量：`ModuleQualityLoss`，%（负值为增益） | 已有受限入口；原生配对U/I及质量5%的GUI载入/另存、24h对照已证；GUI会重写P缓存 | 同步子阵 `PQualityModU/I`，只清质量默认位0x00100000，保留P字段；不能只改一个标量 |
| 5.3.2 / 26–27 | LID：`LIDLoss`，% | 已有受限入口；0/3/100等原生消费对照，LID3的GUI载入/另存及24h对照已证 | owning子阵/声明版本/场景约束；不推断组件技术或默认值 |
| 5.3.3 / 27 | 模块失配：`PMismMPP`，% | 已有受限入口；GUI默认/手动绑定及原生消费已证，失配5%的GUI载入/另存与24h对照已证 | 只清子阵默认位0x00040000；单存百分比在默认模式可能无效 |
| 5.3.4 / 27–28 | string_mismatch_updates：组串电压失配默认/常数 | [原生保存/8760h与受限MCP](native-detailed-losses.md)：PMismString及owning默认bit17，旧教程忽略口径不覆盖8.1.6 | 独立事务，默认不臆补0，非PMismMPP；保持未知位/其它物理字段，详细研究工具仍待 |
| 5.4 / 28 | 积灰：`soiling_updates` 和 `soiling` SFI | [PR #36 原生与MCP已验收](soiling-verification.md)：常数/月度8760h、15数值列一致 | 主阵列Soiling块，0.1百分点精度；8.1.6 PV-only、无阴影固定单平面；不是自动计算清洗/积雪模型 |
| 5.5 / 28–29 | iam_updates：组件定义/ASHRAE；其它IAM与PAN仍待 | [受限MCP及8760h/全页PDF已验](native-model-inputs.md)，保留组件默认缓存语义；Fresnel/custom/PAN编辑仍待 | [整窗与默认恢复证据](native-detailed-losses.md)，不自行算菲涅尔/ASHRAE；OND/BTR编辑不冒充PAN |
| 5.6 / 30 | auxiliary_updates：常数/夜间及profile阈值、比例族 | [原常数MCP](native-auxiliary-power.md)不变；[受限profile](native-detailed-losses.md)两族GUI保存/8760h已证 | pvSystem bit18/5原生字段；新profile显式五值、night0、未验混合停；保留负E_Grid，无外部负荷模型 |
| 5.7 / 30–31 | ageing_updates年数/均值/成对RMS原生输入，ageing_enabled独立启停 | [R51共享完整模板及公共闭环已验](native-model-inputs.md)，停用仍可读；37次边界、6组48h加两次同输入复跑，原8760h证据保留 | 只改原生输入/bit20，不写保存因子；非零RMS重复输出变化，MC样本数组复用未证，不自算或以单次能量归因开关 |
| 5.8 / 31 | PV系统不可用时段/统计配置 | seed7生成5%两态文件仅标题不同，8760h逐列一致；MCP专用配置仍待 | [原生随机时段复演](native-detailed-losses.md)；与WeakGrid电网不可用分开，不以24h随机未命中判无消费 |
| 5.9 / 32–33 | 光谱模型与系数集 | 原生页显示当前MET缺PW与RH，启用禁用；系数可见不等消费 | [真实依赖缺口](native-detailed-losses.md)；不补造数据、不估算光谱校正 |
| 5.10 / 33 | 各损失效应的交互行为图 | 教程有原生交互图；专用调用/导出尚未定位 | 与全年PDF损失图不同；后者已在基础闭环产生，不借此宣称交互图已接入 |

标量、模式和原生反例依据[损失字段与原生对照](grid-capacity-verification.md)；本地图不重跑该文档中的旧测试，也不把8.1.6加载旧声明文件升级为旧版可执行程序的验证。

### 只读状态选择性移植：R49

`pvsyst_get_variant_parameters`已接入变压器序列化层、PV系统停运时段、削峰策略、活动场景列表、老化模板及四类IAM；P50读取扩大到已记录root状态，写入仍限原结果模板。28次新公共调用及一条不依赖商业样本的公共协议长期契约通过，输入字节保留；物体与分组误计的首轮失败及修正记录保留。见[原生存储状态](native-state-inspection.md)。这是读取与诊断，不把此前表中待证的配置、随机生成或物理模型标为完成。

### 能量管理：第 10 章

| 教程节 / PDF页 | 原生入口与所属对象 | 原生证据 / MCP状态 | 下一验证及边界 |
|---|---|---|---|
| 10.1 / 77 | inverter_temperature_updates：TempShift/TempFixed及$4000/$8000；ambient移除两字段 | [MCP受限配置已验收](inverter-temperature.md)：三模式完整文件同GUI、offset/fixed8760h/16列一致、35负例/恢复 | 两显式保存值不补inactive默认；[-100,100]/0.1仅适配器域，非GUI/厂家限；普通单平面/free horizon/无PF等耦合 |
| 10.2 / 78–81 | power_factor_updates：常数TanPhi、月度12项TanPhiM；正滞后/负领先 | [受限MCP已验收](power-factor.md)：七个完整文件同GUI、七组8760h原生控制及两组公共8760h/18列精确对照 | 直接传原生无量纲正切，不换算Cos；CSV EReGrid/EApGrid均原样标kVAh，负TanPhi也未导出负无功，不补符号 |
| 10.2 / 78–81 | 同一PF事务的pnom_limit native/active/apparent；系统bit22/23 | [常数及R50月度PNom闭环已证](power-factor.md)：57次公共闭环、6组8760h/原生PNom单位验证；月度读写共用规则 | 不改OND硬件或算余弦；新增组合仅已有无限值及有功注入/单列损失族，其他耦合仍限原域 |
| 10.3 / 81–82 | owning `pvSystem.PLimGrid`（kWac）、`LimGrSeparLoss`；GUI注入点Flags=$05000000，逆变器端=$01000000，禁用注入点保留$04000000 | GUI持久化及2022全年原生消费已证；[NoPF受限MCP切片已验收](grid-power-limit.md) | 限定Active power/注入点250kW夹具；`E_Grid`最大250kW，693356.8866kWh；`PGridLim/EGrdLim`实际导出，`EUnused=0`不是未限电 |
| 10.3 / 81–82 | 逆变器端/注入点位置、单独损失 | 原无下游损耗夹具全年相同；非零AC的24h控制已区分：并网最大250与246.93kW | 本场景注入点多15.340kWh，不外推所有场景/全年；未扩大MCP活动AC标志范围，EOutInv不是有界并网输出 |
| 10.3 / 81–82 | power_controls_updates：PF+有功/视在、端点、损失分类的独立组合事务 | [受限MCP已验收](native-power-controls.md)：19完整GUI文件/19原生全年、38公共转换、57反例、7公共8760h与CSV/PDF一致 | 旧PF/NoPF限值域不变；仅已验flag族；R50补齐月度PNom的无限值与有功注入族，其它无证月度族、NoPF视在/温度/P50/储能及活动AC耦合仍阻断；PGridLim视在模式列头仍kW，不做外部换算 |
| 10.4 / 82–83 | p50_p90_updates：根级P50_P90/TP50_P90十四字段，既有GUI结果模板 | [受限MCP已验收](p50-native-parameters.md)：四个完整文件同GUI、四组8760h/16列及概率页正文一致、50反例/恢复 | 仅原生结果模板/root Flags01800001；未仿真保存不落参数、输入块单独不足以取得专页，反例保留；新建/ClimateChange/累计图仍待，不自算分位 |

限电本机证据为隔离case的 `native-grid-limit-reference.json` 和原生7页报告：第3页明确 `Active power`、`Limit applied at the injection point`、250kWac；第5页为 `Unused energy (grid limitation)`、693357kWh。参考输入与结果不随仓库分发。系统bit27仅表示允许/禁止注入，与数值限值位及PLimGrid不同。

### 朝向、地平线与 3D：第 3、8、9 章

| 教程节 / PDF页 | 功能及现有入口 | 原生证据 / MCP状态 | 共享依赖与下一验证 |
|---|---|---|---|
| 3.1.1 / 12 | 固定平面倾角、方位角：`orientation_updates` | 已有受限配置及原生消费证据；无活动几何/布局，普通非平均平面 | 角度影响已有阴影/布局映射时不得只改标量 |
| 3.1.1、4.1 / 12、17–21 | 多固定平面复制、分配、删除：`clone_orientation`、`NoOrientation`、`remove_orientation` | 已有受限入口；8.1.6 GUI源的三个状态与MCP结果有48h逐点对照，不只支持单平面 | [固定平面生命周期](grid-capacity-verification.md#gui-saved-816-exclusive-fixed-plane-lifecycle)：独占MPPT/完整计数、无活动场景；不是按季切换 |
| 3.1.2 / 12 | 季节性倾角切换：seasonal_updates及原生季节字段检查 | [MCP受限写入已验收](seasonal-orientation.md)：固定切换/月mask完整文件同GUI、8760h/14列一致、23负例和恢复通过 | 单平面/子阵/自由地平线/普通Grid；移除零BaseSlope，保留方位及分配；FieldTilt=0非全年水平，回固定用恢复 |
| 3.1.3 / 13 | Domes：Dome/DomeBackfaces配对与同子阵分配 | [pvsyst_create_dome_allocation_variant受限MCP已验收](native-dome-allocation.md)：原生第二朝向开关、39/1与20/20保存重载，6正向/51负例，三种分配均有8760h/16列/PDF正例 | 一个40串子阵/440kWp/6逆变器未增硬件，NoOrientation2=2且不写NStrOrient2；仅既有有效配对10±90模板重分配，保留原未分配失败及一次退出未定的原生失败，不等于3D或首次构造API |
| 3.1.4 / 13 | Unlimited sheds：Sheds | [R54公共新VC/回读/再编辑及48h消费](native-shed-controls.md)，Pitch/Tilt/NSheds/Width/Electrical均有变化；保留R31全年基线 | 完整8.1.6源门禁、角0.1/长0.001不舍入，不推导电气分组、不转换3D |
| 3.1.5 / 14 | Unlimited sun-shields：SunShields | [R54同入口与48h消费](native-shed-controls.md)，Pitch/Tilt有变化，Electrical并网值此段相同；保留R31全年 | 如实保留相同结果，不等于开关普遍无效；任意组合/GUI边界仍未测 |
| 3.2.1 / 14 | 无限水平轴：UnlimTracking | [R38原生全年与R53受限配置](native-tracking-controls.md)；原五控制保持，R53电气True在新公共配置48h消费并导出ShdElec；后续[完整模板任务](tracking-template-flow.md)复用克隆/跟踪新VC/新CLI2024全年/两页报告与故障保留 | 完整任务限8.1.6rev49278继承数据库引用、显式功率及出处；保持needs_review，不转换类型/固定几何，不含Module Layout/独立3D，不自行算跟踪角 |
| 3.2.2 / 15 | 水平N-S/倾斜轴：TrackHorizNSAxis/TrackTiltedAxis | [R53共同MCP入口与公共回读](native-tracking-controls.md)，新轴方位/轴倾角各48h有变化，原R31全年保留 | 只编辑源所属字段；风收拢触发阈值、任意场景/布局与GUI边界未测 |
| 3.2.3 / 15 | 竖直轴：TrackVertAxis | [R53受限MCP/部分修改/回读](native-tracking-controls.md)，FieldTilt25新配置48h有变化 | 不外推任意方位界限、几何和3D映射 |
| 3.2.4 / 16 | 跟踪遮阳板：TrackSunShield | [R53受限MCP与48h消费](native-tracking-controls.md)，新倾角对有变化；R31全年保留 | 不转类型，不开放任意几何与活动场景 |
| 3.2.5 / 16 | 东/西水平轴：TrackHorizAxis | [R53受限MCP与48h消费](native-tracking-controls.md)，坡度2此段无变化；追加倾角0/30有22小时变化 | 如实保留无变化，不据此说坡度无效；时段不外推全年 |
| 3.3.1 / 16 | 双轴：TrackTwoAxis | [R53界限公共创建/回读/再编辑与48h消费](native-tracking-controls.md)，Azim=-120/120、Tilt=-5/70有变化 | 全部源/候选minmax对验序，未改侧用源值，不做场景映射 |
| 3.3.2 / 17 | 双轴N/S与E/W框架：TrackFrameNS/EW；backtracking | [R53同入口两框架48h新参数消费](native-tracking-controls.md)；保留原R31全年证据 | 不把Unlim回溯单变量证据外推到框架回溯全部条件，不替代原生算法 |
| 8 / 40–41 | 地平线高度/方位点集、原生HOR保存导入及清除；horizon_updates | [MCP受限profile/none已验收](horizon-profile.md)：四/六点8760h/14列同GUI、名称/清除整文件一致、25负例；原生HOR两路径24h通过 | 单普通固定平面/子阵/plain Grid；原生100槽模板及FracAlbedo=1，变点不继承派生因子、名称保留缓存；不自行闭合/插值/遮挡 |
| 9.1 / 43 | 3D场景与系统朝向兼容校验 | [房屋/PV原生绑定及R47/48自动入口已证](house-pv-modeling.md)：公共import工具的关联/有效性/保存/重载/必要年度独立通过 | 只限已打开匹配工程和录制中文8.1.6布局，GUI1..2区；显示模型不等于关联完成，停止保留现场 |
| 9.2 / 44 | 已有场景执行及重算：`run_simulation(recompute_shading=...)` | R43三态及R44 AI完整住宅各8760h/14数值列/8页PDF已证，均有同硬件无场景基线 | 通过既有底层run，不放宽fixed_unshaded_grid；基线省略GlobShd，不补列或零，差值不归因到单个构件 |
| 9.3 / 46 | 线性阴影表、逐步/快速算法、Module Layout | 原生线性/快速表格与显式重算已证；Module Layout与电气阴影依用户要求暂缓 | 同几何开关房体投影的全年14列完全相同，不宣称非零房体损失或电气阴影效果 |
| 9.4 / 48–50 | AI一次生成整栋房屋/PV场景 | [pvsyst_create_house_pv_scene](house-pv-modeling.md)：AI显式命名构件mesh+PV放置，薄序列化PVCollada2；19构件102顶点70三角通过官方4项校验、原生导入保存重载与全年 | 不是房型模板扩充或自动GUI导入；旧参数路径保留，格式写入、原生关联与仿真分开声明，beta提示保留 |
| 9.5 / 51–53 | 房屋构件与PV对象 | R44原生保存19个pvShdGenObject+1个pvShdArrayTable；墙/多屋面/挑檐/门窗外形/烟囱/门廊/同栋附属体量由AI输入 | 不要求矩形footprint或两种roof枚举；不做周边树木建筑、材料/建筑物理语义或通用CAD内核 |
| 9.6 / 54 | 房体投影属性 | 原生TGenericObjectForm勾选/取消启用阴影投射并另存对照已证，未直接patch私有位；暂无通用属性编辑MCP | 仅当前房体控制，不外推所有对象/电气阴影；编辑后的原生表与全年结果如实记录 |
| 9.7 / 54–55 | 房屋/PV坐标与变换 | PVC2使用共同父旋转+局部平移；本机导入转换、角度/数量/位置保存证据保留 | 只生成标准场景输入，不替代原生几何处理；不是Transform全菜单API |
| 9.8 / 55–62 | 必要的Tools入口 | 原生Shift+Ctrl+O朝向管理已用于房屋/PV绑定 | 只覆盖本建模流程需要的入口，不宣称工具菜单全集支持 |
| 9.9 / 63–76 | 房屋/PV导入、保存、重载、重算 | 两种旧屋顶与AI完整住宅全过程已证；CSV/PDF来自原生全年运行 | PVsyst目前只导入PVCollada2，不导出该格式；通用场景维护API不在本批声明 |

### 多屋面连接链批量盘点

[多屋面连接链与原生往返](multiroof-native-map.md)分开记录电气、几何候选、GUI持久化/重载、原生消费与MCP状态。既有数量入口减半再clone_orientation保持2逆变器/2串/36模块，三组2024-06-21..22各48h已消费；同角度分组E_Grid比原单子阵+0.7567kWh原因未定，仅第二方位90相对分组控制+1.7385kWh，不外推全年。后续8.1.6rev49278/DPI96研究：双根40构件/2PV区逐叶关联2系统朝向，原生有效、新VC2另存/切离重载、NoOrient1/2及36模块已证，正常持有Popen退出后恢复INI。该新配置一次2024原生全年已返回8784h、32892.3871kWh；实际13数值列含GlobShd，请求viILPmxSH未导出，14列辅助断言失败保留，不补零/重跑，全年后置全字节守卫未执行不作该声明。XML身份与原生位置完整等价仍有缺口；现有自动关联/住宅任务仍单源朝向1..2区；[有限双朝向作者与严格回读](grouped-house-scenes.md)另批复用现有入口实现候选version2，恰好2源朝向/2独占子阵/同PAN-OND/每组1表及逐组计数，所有坐标在朝向1框架内、每PV节点标准Z旋转。该作者批次的公共往返和needs_context停点已证，没有新增GUI/仿真。

之后对version2新形状另作原生复核：8.1.6rev49278/CN/DPI96、单SceneRoot/每PV旋转/40构件2区，实导入、逐叶关联、新VC2另存切离重载及有效性通过，实际Popen退出/INI恢复。与旧原生参考比较两PV原点残差0、B宽差1微米；40构件有向面连接/其他字段相同、最大坐标残差8微米，不设工程容差或声称源几何认证。新配置只运行2024-06-21..22的48h，13实际列完整有限，与既有年度同日期切片在导出精度内逐点零差，E_Grid187.1823kWh；零新年度运行。GUI退出时保留的既有输入副本覆盖文件，CLI后字节差为空，新SFI命令另留。原生持久化/消费证据仅此案例；自动MCP关联仍needs_context，尚无通用逐区身份/源坐标验证器，不按自动名称、计数或validtrue升级任意多屋面能力。

后续[保存PV区域关联读取](native-scene-bindings.md)复用已有参数读取与对象解析器，暴露原生PV坐标/原文字段、每表NoOrient引用及逐组组件数，状态区分stored_consistent/needs_review/unsupported。真实单双朝向保存配置只读核对、输入保持及损坏声明公共契约已证；零CLI/GUI/仿真，几何/原生消费标志仍false。移动坐标但不改引用/数量仍可能stored_consistent，故它是保存声明的可观察性/差异核对，不是上述通用身份验证器。

再后续[原生GUI身份入口实测](native-scene-identity-map.md)：8.1.6rev49278/CN/DPI96、已保存2PV配置，实际UIAutomation对朝向管理器左右VTV均为无名称Pane/0后代/0模式，WM_GETOBJECT客户端0；属性TTableFieldForm可读取实际名称、18组件及28/0、28/90，但位置摘要只有低精度原生值，框架参考7选项也不直接等于序列化枚举。WM_PRINT空EMF未得到文字。整个实际Popen会话零导入/另存/仿真/阴影表计算，取消已知确认后正常退出/恢复INI、19输入字节不变。身份/源坐标缺口仍保留，不写桌面框架或自动升级分组导入。

### 房屋一次性建模补齐：R45

| 功能组 | 入口 / 当前状态 | 本批证据与剩余边界 |
|---|---|---|
| 同朝向多块PV区域 | layout三态兼容union已实现；每区独立标准table/rack，20+20与15+25原生消费已证 | 20房屋+2PV/40模块/89.67m²/NoOrient1保存重载，3方案各8760h及PDF；仅单SKU与普通固定朝向 |
| AI构件定位与修正反馈 | code/path/part/triangle/repair_hint已实现，多错误有界返回；25个相关反例与无写入、修正后同入口成功 | 不擅自改面，不充当CAD/遮挡引擎；R48 Native有效性及干涉诊断已由独立import工具执行，反例未保存 |
| 场景回读与修订 | R46 MCP reader已实现；24次新公开调用、官方4项、新修订场景原生消费及8760h已证 | 从真实XML读网格，整树重建归一比较拒绝未知内容；R44返回缺上下文草稿；烟囱6.4→7.2m实际持久化，新增依赖零 |
| 原生自动导入/绑定 | pvsyst_import_house_pv_scene已实现；等分/不等分/单区公共原生另存、重载及3个8760h消费已证 | 不拼私有VC；已打开匹配工程页、中文8.1.6录制物理布局，GUI仅1..2区，创建仍64区；CtrlC只读新复制文本，停止保留现场 |

范围仅房屋必要构件与光伏；家具、室内陈设、精细五金、材质渲染和已暂停功能均不增加。

### 负荷与储能：第 6、7 章

| 教程节 / PDF页 | 功能及现有入口 | 原生证据 / MCP状态 | 共享依赖与下一验证 |
|---|---|---|---|
| 6 / 33–34 | 小时CSV准备、快照绑定：`prepare_load_profile`、`create_load_variant` | 已实现；[小时负荷](storage-loads.md)及[储能记录](grid-storage-parameters.md)记载8784小时 `E_User` 逐点一致 | 全年、单位、固定标准偏移；绑定 `pvLoad` 的原生缓存，不能仅换CSV路径 |
| 6 / 33–34 | 无电池PV-only自用使能及结果 | [pvsyst_create_native_load_variant已验收](storage-loads.md#原生常数月度与日曲线)：根pvVCalcul bit19及pvLoad，三个新配置8760h/CSV/PDF；旧小时API继承语义不变 | 新VC限定PV-only普通固定平面；本例EDirUse不等于E_Solar，不泛化字段定义，不把保存返回值标为已消费 |
| 6 / 33 | 常数/月度/日曲线/概率/家庭电器负荷 | [常数/月度/日曲线MCP已验收](storage-loads.md#原生常数月度与日曲线)，月度显式kWh/日曲线24项kW；[概率语义失败与家庭电器原生证据](native-load-storage-controls.md)保留，后二者无新API | 15正向/65无写入负例及3组新参数8760h；Monthly ValMonth非原样kW，拒未知/重复/嵌套owner与源HourlyFile/Probability/HouseHold，不自造曲线或归一化模型 |
| 7 / 35–36 | BTR、串并联、12个月电池温度、SOW：`create_grid_storage_system` | 已有原生AC耦合构造；GUI重载48h及其他原生参数对照 | 明确硬件/温度/SOW，不推导自动最优选型或BMS |
| 7.1 / 36–37 | AC耦合自用策略：`create_grid_storage_*` 的 `self_consumption` | 已实现；48h参数对照，已有全年记录按各自fixture/时基使用 | 不把它误列为“策略切换缺入口”；不外推DC端口/网充 |
| 7.1 / 36–37 | 充电器AC输入、放电逆变器AC输出功率及效率 | [R55保留原Peak策略的标量编辑/回读/时序再编辑已验收](grid-storage-parameters.md#r55-原策略标量闭环)，新8组48h及1条交叉路径；旧C1/C2保留 | AC100导出充100/放100.004，保留残差不裁剪；不把DC功率当AC限值，不新增调度算法 |
| 7.1、7.3 / 36–39 | 初始/高/硬低/备用SOC | 已实现，分数0..1；初始/高SOC、SelfConsumption硬低C4已证；WeakGrid备用语义按停电条件验证 | 备用 `SOCDischSelf` 不是发电机电压阈值；停电允许朝硬低放电，不能以“停在备用之上”作验收标准 |
| 7.1、7.3 / 36–39 | 允许注入：`allow_grid_export` | 已实现；48h禁止/恢复对照，系统Flags仅bit27 | 不是数值kW限值，也不是网充开关；与其它系统位并存 |
| 7.3 / 38–39 | WeakGrid及全年可用性CSV快照 | 已实现；停电/恢复48h控制，已有8784小时记录须按对应fixture说明 | 负荷与可用性同年同时基，重新绑定；原生日志已读不等于已消费 |
| 7.3 / 38–39 | 随机电网停运分布 | [原生整页与seed1生成时段已见](native-load-storage-controls.md)，确认仍报配置错误；未形成通过的保存/消费方案；缺生成API | 与PV系统不可用分开，保留失败不猜原因，不用外部随机调度替代 |
| 7.2 / 37–38 | Peak shaving及四种放电时序 | [pvsyst_create_peak_shaving_variant受限MCP已验收](native-peak-shaving.md)：既有Peak/PVToGridMin profile，两态完整文件同GUI，11正向/71负例及3组新8760h/28列/PDF；另两态仍待 | 保留root80000但无pvLoad的原生状态；固定电池/200限值profile，报告为Apparent power；全AC平衡/充放SOC联动已核，AfterSunset不等于仅夜间，不外推端点语义或网充 |
| 扩展范围 | 电网充电、混合逆变器DC电池端口 | [实际AC窗口、DC类/枚举候选已区分](native-load-storage-controls.md)，DC激活链未闭合；尚无已验收入口 | 未找到直接引用不证明不存在；无日照时没有充电不构成正例，先证原生端口/策略，不以AC耦合替代 |
| 7 / 34–39 | 原生能量平衡与SOC结果：`grid_storage` SFI | 已有完整导出含 `CL_Over`；旧8784行最大0.0097kW保留；R39三个新削峰2022年8760h/28列，完整AC残差最大0.0103kW | 未重跑旧年制造通过；新场景SOC/充放/上网实际联动，电池侧放电不能直接当AC限值；SolarTime/LegalTime与GUI48h证据按各自输入使用 |

储能时段来源：既有C1/C2/C4、全年负荷与全AC平衡属于各自记录；后续 `STORAGE_K1_GUI` 的四次2024-01-01至01-02原生GUI往返只证明那48小时。当地标准时间CSV使用 `LegalTime`，不因为晴空提示自动移时。

## 当前交付范围

房屋阶段仍限住宅必要构件和光伏，不建家具、室内、精细五金、周围树木或通用CAD。R47/48完成受限原生自动导入的独立验收；旧无阴影编辑器边界不因此放宽。

用户批准以稳定主线为底座选择性移植私有参考51148b9；R48–55已独立交付房屋原生入口、只读状态、跟踪/老化/IAM读取扩展、PF/PNom闭环、Shed、报告页及储能标量。R56完成单默认MV夜间断开两态公共全年闭环，R57完成均一AC线缆门禁/所有者/精度和回读再编辑；R60/61另完成原生DC设置/重算薄桥：公共两态与再编辑、GUI重载及四个公共产物的新全年逐列同控制；DC比例反算明确不搬。每批源门禁、owner、精度和公共闭环缺陷先修复，再以新公共/原生证据验收，不把参考提交或旧运行当作移植后的通过。混合DC端口、网充、特殊负荷、随机电网停运等未证能力不由本次批准自动升级。

## 住宅单任务与工程生命周期

原生工程打开入口复用已证Grid→加载→文件过滤→原生确认，不另造桌面框架。受控冷启动、目标工程精确核对、源PRJ保持、已有同工程只读识别、错工程及已编辑未确认模态保持已通过同一公共GUI批次。已有窗口的caption/当前INI不证明其已加载路径或无未保存工作，明确`automation_ready=false`，不擅自切换或接管；别安装/多实例/枚举异常均先停。打开工具本身不写INI、不关进程。[受控原生工程打开](native-project-session.md)已通过PR75合入。

完整住宅单任务`pvsyst_run_house_task`已在独立工程一次冷启动贯通生成→原生导入/朝向关联/有效性/遮挡表/SaveAs→自有Popen正常退出→新CLI重新载入已存VC全年及报告；源保持、20构件/2区/40模块、8760小时/14数值列/8页PDF已证。已存检查点续做不再开GUI，完成态恢复不增跑；注入交接失败的残留保留，显式新命名重试已原生全年通过。TSplashScreen退出等待的两次初始停点与修复后成功分开保留。版本/DPI/单固定朝向及一/两区、声明来源/模板继承与单服务器协调边界保持，未扩展混合DC/网充/任意变压器；见[住宅单任务与证据裁决](native-house-task.md)。

## 项目输入与源工程准备

`pvsyst_prepare_design_project`复用既有组件/模板发现、声明包络、原生天气准备及受保护工程配置链，把配置结束与全年仿真分开，保持`fixed_unshaded_grid`源范围。公共完整配置、零仿真拦截、合成额度/复核传播与未来天气只读状态已通过；准备无SFI/CSV/PDF，不宣称发电消费。已准备工程随后接现有住宅公共入口，20构件/两区36模块、自有GUI正常退出、新CLI2024全年8784小时/13数值列/8页PDF已证，最终住宅仅一次年运行，源和DataPath保持。现有全年入口默认行为保留，资料缺项/模板继承/review继续披露，不补厂家限值或现场气候依据；输入为论文模型、Generic逆变器及声明实验包络，不是实际工程认证。见[准备工程与消费证据](design-project-preparation.md)。来源草案到单一住宅总任务的协调仍是下一批，不由本次两调用实证升级为已实现。

统一入口`pvsyst_run_sourced_house_task`已独立公共/原生验收：缺资料给13项草案、错引用停、假设复核默认停且需显式继续；同一调用来源绑定→源准备零次年→20构件/两区36模块原生住宅→最终2024全年8784小时/13列/8页，顶层保留needs_review。复用两清单，完成态不重跑；改目标/源PAN/坏根/记录路径篡改均停且清单保持，探针已恢复。最后重验状态刷新且review保持的临时故障验证通过。不新增job/计算框架，仍只保证声明一致性及已证原生范围，未做最终逐时源天气比较。PR77两调用与本次统一调用分开计证，见[来源住宅任务](sourced-house-task.md)。

## 声明与工程保存值审查

只读`pvsyst_review_project_inputs`复用准备清单和工程/参数/对象读取核对设备/SIT/MET引用、固定角度、计划数量和显式损失；永久公开契约已证改倾角仅报对应差异、坏基线/平均标志停止、未解析天气引用保留review、审查零写入。真实原生另存住宅VC1与准备源另各一次公开只读对比零差异、源review保持、原文件字节保持（76工具）。没有新GUI/年度运行，不把文件一致当原生消费认证；包络温度/系数/限值与项目保存温度分列，不猜对应，损失模式仅披露未解释；单Grid单固定非平均范围。见[声明与保存值审查](project-input-review.md)。新原生温度编辑/自动修订/多阵列未由本批升级。

## 共享对象与下一批验证

| 交叉面 | 裁决与验证顺序 |
|---|---|
| 系统Flags、PF、数值限值与储能 | bit27导出控制、数值限值开关/位置、储能使能不是同一能力；先确认各自owner与掩码，保持不相关位，禁止用一个布尔值包办 |
| 主阵列/子阵列损失 | 热Uc和Soiling在主阵列，质量/LID/失配在子阵；质量与失配的默认位各自独立，不能把所有损失都说成同一个Flags/Flags2门控 |
| 限电点与下游损耗 | 注入点与逆变器端的区别依赖AC线路/辅助/变压器；已有无下游损耗的250kW全年结果只作第一参考 |
| 朝向、场景与电气映射 | 平面列表、子阵分配、线路级引用、场景/布局缓存需要一致；固定平面生命周期不等于季节或跟踪控制 |
| PV不可用、WeakGrid停电与负荷缓存 | 同名/近似类名不是同一owner；必须核对可用性对象、时基、年份及快照，保持PV不可用与电网停运分离 |
| 概率、老化与原生图表 | P50/P90、老化随机失配和Loss Graph有各自结果形式；用原生报告/数据验证，不以外部概率或损失计算补齐 |

限值、常数辅助、季节、地平线、逆变器温度、独立PF、受限PF/限值组合及既有结果模板P50写入均已验收；损失四标量GUI往返、非零AC位置差别已证，AC编辑及其PF耦合仍待全部分支绑定。P50从无结果输入构造概率专页仍待，不靠伪造状态位补齐。跟踪/3D、削峰及混合DC缺口不由既有运行或字符串存在升级。只交付有原生证据的子集，不等待全部复杂入口同时完成。

## 分组验证与交付规则

1. 同一原生窗口/对象尽量在一次会话中取得多个独立方案；先做默认/关闭控制，再逐项改变单变量并另存，保留所有参考与失败记录。
2. 同组先确认字段归属、单位、开关、默认/手动模式、缓存与版本，尤其检查不同功能共用的对象及标志位。
3. 静态读取可以并行；GUI、INI 和共享工程修改只由一个执行者串行完成。子代理读取明确单文件白名单，各写独立报告，只回短摘要和证据引用。
4. 原生消费验证先用能回答问题的最小运行；需要全年覆盖的功能做完整年份，跨功能交互另设有依据的组合控制，不重复运行无新增价值的场景。
5. 仅实现已证实部分，复用既有预览/备份/恢复和软件计算；不编写替代发电、限电、阴影或调度模型。每个验收阶段独立 PR，未验收部分保留待办，不制造全教程完成声明。

本地图属于入口盘点，不是新增功能的运行验收，也不创建新的测试框架或全仓门禁。
