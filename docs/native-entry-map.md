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
| 8–10、33：全年执行、小时结果与报告 | `pvsyst_build_sfi`、`pvsyst_run_simulation`、`pvsyst_read_results`、`pvsyst_read_rows` | 原生2022年8760小时、13列有限样本、7页PDF；799980.9366kWh 与报告799981kWh一致。年度PR按原生/标准年口径，不平均逐小时PR |

基础闭环已验收不代表高级编辑器齐全。原始组件、天气、教程、商业二进制和本机实验产物不随本地图分发。

## 功能组地图

四个功能组分别盘点第 3、5–10 章。其静态发现只用于定位；共享字段、模式冲突和原生消费确认之前，暂停新增相关 MCP 配置代码。下表经父代理裁决，不直接照搬组报告中的旧覆盖矩阵或缺项计数。

### 详细损失：第 5 章

| 教程节 / PDF页 | 功能与现有入口 | 原生证据 / MCP状态 | 共享依赖与下一验证 |
|---|---|---|---|
| 5.1 / 22–23 | 常数热损系数 Uc：`main_array_updates.UFactor`，W/m²/K | 已有受限入口；[Uc29 GUI载入/另存及24h原生对照已证](loss-gui-roundtrip.md)，不外推全年或GUI改写后的标志可再次编辑 | 主阵列 owning 字段及限定Flags/Flags2；范围15–100，非原生全范围，不解释未证低值行为 |
| 5.1 / 22–23 | 风速项 Uv、热模式/吸收率/效率 | Uv2原生保存为pvMainArray.UVFactor；原生8760h风速全0，输出与控制相同，非零风速正向作用待验 | [整窗与全年批次](native-detailed-losses.md)；与Uc分开，不因支持Uc就宣称完整热模型编辑 |
| 5.1 / 23 | NOCT规格比较 | 教程说明不是实际仿真输入 | 记录为比较/元数据项，不发明仿真设置 |
| 5.2.1 / 23–24 | DC线路电阻、损失比例、长度/截面工具 | 原生45mΩ/2.5%两态GUI保存与2022全年消费已证，MCP专用编辑仍待 | 跨子阵/多电路CableDefinition与缓存；[原生损失批次](native-detailed-losses.md)，不由外部公式反算等效电阻 |
| 5.2.2 / 24–26 | AC线路、参考功率、MV/HV变压器、铁铜损及夜间断开 | AC100m/MV默认与数量/铁铜损/夜间/HV参数表GUI保存及8760h已证，任意构造未接入MCP | [变压器与电路联动](native-detailed-losses.md)，多owner/树结构/原生参考功率换算；旧[AC限值交互](native-auxiliary-power.md)仍限24h |
| 5.3.1 / 26 | 组件质量：`ModuleQualityLoss`，%（负值为增益） | 已有受限入口；原生配对U/I及质量5%的GUI载入/另存、24h对照已证；GUI会重写P缓存 | 同步子阵 `PQualityModU/I`，只清质量默认位0x00100000，保留P字段；不能只改一个标量 |
| 5.3.2 / 26–27 | LID：`LIDLoss`，% | 已有受限入口；0/3/100等原生消费对照，LID3的GUI载入/另存及24h对照已证 | owning子阵/声明版本/场景约束；不推断组件技术或默认值 |
| 5.3.3 / 27 | 模块失配：`PMismMPP`，% | 已有受限入口；GUI默认/手动绑定及原生消费已证，失配5%的GUI载入/另存与24h对照已证 | 只清子阵默认位0x00040000；单存百分比在默认模式可能无效 |
| 5.3.4 / 27–28 | string_mismatch_updates：组串电压失配默认/常数 | [原生保存/8760h与受限MCP](native-detailed-losses.md)：PMismString及owning默认bit17，旧教程忽略口径不覆盖8.1.6 | 独立事务，默认不臆补0，非PMismMPP；保持未知位/其它物理字段，详细研究工具仍待 |
| 5.4 / 28 | 积灰：`soiling_updates` 和 `soiling` SFI | [PR #36 原生与MCP已验收](soiling-verification.md)：常数/月度8760h、15数值列一致 | 主阵列Soiling块，0.1百分点精度；8.1.6 PV-only、无阴影固定单平面；不是自动计算清洗/积雪模型 |
| 5.5 / 28–29 | iam_updates：组件定义/ASHRAE；其它IAM与PAN仍待 | [受限MCP及8760h/全页PDF已验](native-model-inputs.md)，保留组件默认缓存语义；Fresnel/custom/PAN编辑仍待 | [整窗与默认恢复证据](native-detailed-losses.md)，不自行算菲涅尔/ASHRAE；OND/BTR编辑不冒充PAN |
| 5.6 / 30 | auxiliary_updates：常数/夜间及profile阈值、比例族 | [原常数MCP](native-auxiliary-power.md)不变；[受限profile](native-detailed-losses.md)两族GUI保存/8760h已证 | pvSystem bit18/5原生字段；新profile显式五值、night0、未验混合停；保留负E_Grid，无外部负荷模型 |
| 5.7 / 30–31 | ageing_updates：现存激活RMS0模板年数/均值 | [MCP模型输入及8760h已验](native-model-inputs.md)，只改两输入、内核重算、缓存保留；启停/MC/任意模板待 | [重算与保存量证据](native-detailed-losses.md)，零均值非关闭，不自行按年算损失/蒙特卡罗 |
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
| 10.2 / 78–81 | 同一PF事务的pnom_limit native/active/apparent；系统bit22/23 | [常数PF的两种Legacy模式已证](power-factor.md)：原生PDF明确强制有功/视在PNom，年度电量/InvPmax不同 | 不是注入点kW/kVA限值，不改OND硬件；月度+Legacy组合未验，保持阻断 |
| 10.3 / 81–82 | owning `pvSystem.PLimGrid`（kWac）、`LimGrSeparLoss`；GUI注入点Flags=$05000000，逆变器端=$01000000，禁用注入点保留$04000000 | GUI持久化及2022全年原生消费已证；[NoPF受限MCP切片已验收](grid-power-limit.md) | 限定Active power/注入点250kW夹具；`E_Grid`最大250kW，693356.8866kWh；`PGridLim/EGrdLim`实际导出，`EUnused=0`不是未限电 |
| 10.3 / 81–82 | 逆变器端/注入点位置、单独损失 | 原无下游损耗夹具全年相同；非零AC的24h控制已区分：并网最大250与246.93kW | 本场景注入点多15.340kWh，不外推所有场景/全年；未扩大MCP活动AC标志范围，EOutInv不是有界并网输出 |
| 10.3 / 81–82 | power_controls_updates：PF+有功/视在、端点、损失分类的独立组合事务 | [受限MCP已验收](native-power-controls.md)：19完整GUI文件/19原生全年、38公共转换、57反例、7公共8760h与CSV/PDF一致 | 旧PF/NoPF限值域不变；仅已验flag族，NoPF视在/月度Legacy/温度/P50/储能及活动AC耦合仍阻断；PGridLim视在模式列头仍kW，不做外部换算 |
| 10.4 / 82–83 | p50_p90_updates：根级P50_P90/TP50_P90十四字段，既有GUI结果模板 | [受限MCP已验收](p50-native-parameters.md)：四个完整文件同GUI、四组8760h/16列及概率页正文一致、50反例/恢复 | 仅原生结果模板/root Flags01800001；未仿真保存不落参数、输入块单独不足以取得专页，反例保留；新建/ClimateChange/累计图仍待，不自算分位 |

限电本机证据为隔离case的 `native-grid-limit-reference.json` 和原生7页报告：第3页明确 `Active power`、`Limit applied at the injection point`、250kWac；第5页为 `Unused energy (grid limitation)`、693357kWh。参考输入与结果不随仓库分发。系统bit27仅表示允许/禁止注入，与数值限值位及PLimGrid不同。

### 朝向、地平线与 3D：第 3、8、9 章

| 教程节 / PDF页 | 功能及现有入口 | 原生证据 / MCP状态 | 共享依赖与下一验证 |
|---|---|---|---|
| 3.1.1 / 12 | 固定平面倾角、方位角：`orientation_updates` | 已有受限配置及原生消费证据；无活动几何/布局，普通非平均平面 | 角度影响已有阴影/布局映射时不得只改标量 |
| 3.1.1、4.1 / 12、17–21 | 多固定平面复制、分配、删除：`clone_orientation`、`NoOrientation`、`remove_orientation` | 已有受限入口；8.1.6 GUI源的三个状态与MCP结果有48h逐点对照，不只支持单平面 | [固定平面生命周期](grid-capacity-verification.md#gui-saved-816-exclusive-fixed-plane-lifecycle)：独占MPPT/完整计数、无活动场景；不是按季切换 |
| 3.1.2 / 12 | 季节性倾角切换：seasonal_updates及原生季节字段检查 | [MCP受限写入已验收](seasonal-orientation.md)：固定切换/月mask完整文件同GUI、8760h/14列一致、23负例和恢复通过 | 单平面/子阵/自由地平线/普通Grid；移除零BaseSlope，保留方位及分配；FieldTilt=0非全年水平，回固定用恢复 |
| 3.1.3 / 13 | Domes：Dome/DomeBackfaces配对与同子阵分配 | [pvsyst_create_dome_allocation_variant受限MCP已验收](native-dome-allocation.md)：原生第二朝向开关、39/1与20/20保存重载，6正向/51负例，三种分配均有8760h/16列/PDF正例 | 一个40串子阵/440kWp/6逆变器未增硬件，NoOrientation2=2且不写NStrOrient2；仅既有有效配对10±90模板重分配，保留原未分配失败及一次退出未定的原生失败，不等于3D或首次构造API |
| 3.1.4 / 13 | Unlimited sheds：Sheds | [GUI保存及2022年8760h/16列/PDF已证](native-orientation-controls.md)；尚无专用配置 | 本次默认几何消费不证明任意棚架、自遮挡或电气分组编辑 |
| 3.1.5 / 14 | Unlimited sun-shields：SunShields | [GUI保存及2022年8760h/16列/PDF已证](native-orientation-controls.md)；尚无专用配置 | 与普通固定平面、3D遮阳板区分；任意几何/活动阴影仍待 |
| 3.2.1 / 14 | 无限水平轴：UnlimTracking | [pvsyst_create_tracking_variant受限MCP已验收](native-tracking-controls.md)：5个GUI整文件、3组新8760h/17列/PDF，四开关组合有48h隔离对照 | 五控制由原生消费，不转换类型/不改固定几何；GlobShd与损失图证明无显式3D也有二维遮挡，电气True/独立3D仍待，不自行算跟踪角 |
| 3.2.2 / 15 | 水平N-S/倾斜轴：TrackHorizNSAxis/TrackTiltedAxis | [GUI保存及无显式3D场景8760h/报告已证](native-orientation-controls.md)；尚无专用配置 | 本批无3D场景也可运行基本跟踪；不据此外推活动场景/布局和阴影路径，教程相关3D依赖另验 |
| 3.2.3 / 15 | 竖直轴：TrackVertAxis | [GUI保存及无显式3D场景8760h/报告已证](native-orientation-controls.md)；尚无专用配置 | 类型和本次默认控制消费已证，任意方位界限、几何域待验 |
| 3.2.4 / 16 | 跟踪遮阳板：TrackSunShield | [GUI保存及无显式3D场景8760h/报告已证](native-orientation-controls.md)；尚无专用配置 | 本次类型消费不等于任意几何、回溯组合或3D场景编辑 |
| 3.2.5 / 16 | 东/西水平轴：TrackHorizAxis | [GUI保存及无显式3D场景8760h/报告已证](native-orientation-controls.md)；尚无专用配置 | 与其它水平轴类型分开保存；任意倾角界限、活动阴影另验 |
| 3.3.1 / 16 | 双轴：TrackTwoAxis | [GUI保存及无显式3D场景8760h/报告已证](native-orientation-controls.md)；尚无专用配置 | 本次默认双轴消费已证，任意两组界限及场景映射仍待 |
| 3.3.2 / 17 | 双轴N/S与E/W框架：TrackFrameNS/EW；backtracking | [两框架保存及无显式3D场景8760h已证](native-orientation-controls.md)；回溯单变量仅在UnlimTracking已证；尚无专用配置 | 不把无限水平轴回溯证据外推到框架回溯/3D field；不由两个角度替代原生跟踪与场景 |
| 8 / 40–41 | 地平线高度/方位点集、原生HOR保存导入及清除；horizon_updates | [MCP受限profile/none已验收](horizon-profile.md)：四/六点8760h/14列同GUI、名称/清除整文件一致、25负例；原生HOR两路径24h通过 | 单普通固定平面/子阵/plain Grid；原生100槽模板及FracAlbedo=1，变点不继承派生因子、名称保留缓存；不自行闭合/插值/遮挡 |
| 9.1 / 43 | 3D场景与系统朝向兼容校验 | [房屋/PV原生绑定已证](house-pv-modeling.md)：R43两屋顶1000块；R44 AI完整住宅40块/89.67m²，原生关联保存重载；无自动绑定MCP | 显示模型不等于关联完成；中断后按实际孤立场区重定位，不盲目重放拖放 |
| 9.2 / 44 | 已有场景执行及重算：`run_simulation(recompute_shading=...)` | R43三态及R44 AI完整住宅各8760h/14数值列/8页PDF已证，均有同硬件无场景基线 | 通过既有底层run，不放宽fixed_unshaded_grid；基线省略GlobShd，不补列或零，差值不归因到单个构件 |
| 9.3 / 46 | 线性阴影表、逐步/快速算法、Module Layout | 原生线性/快速表格与显式重算已证；Module Layout与电气阴影依用户要求暂缓 | 同几何开关房体投影的全年14列完全相同，不宣称非零房体损失或电气阴影效果 |
| 9.4 / 48–50 | AI一次生成整栋房屋/PV场景 | [pvsyst_create_house_pv_scene](house-pv-modeling.md)：AI显式命名构件mesh+PV放置，薄序列化PVCollada2；19构件102顶点70三角通过官方4项校验、原生导入保存重载与全年 | 不是房型模板扩充或自动GUI导入；旧参数路径保留，格式写入、原生关联与仿真分开声明，beta提示保留 |
| 9.5 / 51–53 | 房屋构件与PV对象 | R44原生保存19个pvShdGenObject+1个pvShdArrayTable；墙/多屋面/挑檐/门窗外形/烟囱/门廊/同栋附属体量由AI输入 | 不要求矩形footprint或两种roof枚举；不做周边树木建筑、材料/建筑物理语义或通用CAD内核 |
| 9.6 / 54 | 房体投影属性 | 原生TGenericObjectForm勾选/取消启用阴影投射并另存对照已证，未直接patch私有位；暂无通用属性编辑MCP | 仅当前房体控制，不外推所有对象/电气阴影；编辑后的原生表与全年结果如实记录 |
| 9.7 / 54–55 | 房屋/PV坐标与变换 | PVC2使用共同父旋转+局部平移；本机导入转换、角度/数量/位置保存证据保留 | 只生成标准场景输入，不替代原生几何处理；不是Transform全菜单API |
| 9.8 / 55–62 | 必要的Tools入口 | 原生Shift+Ctrl+O朝向管理已用于房屋/PV绑定 | 只覆盖本建模流程需要的入口，不宣称工具菜单全集支持 |
| 9.9 / 63–76 | 房屋/PV导入、保存、重载、重算 | 两种旧屋顶与AI完整住宅全过程已证；CSV/PDF来自原生全年运行 | PVsyst目前只导入PVCollada2，不导出该格式；通用场景维护API不在本批声明 |

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
| 7.1 / 36–37 | 充电器AC输入、放电逆变器AC输出功率及效率 | 已实现；C1/C2证明两侧1kW限值，分别包含 `CL_Chrg` / `CL_InvB` | 与电池DC功率及网充能力分开；已有控制不重复执行 |
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

用户随后批准以稳定主线为底座选择性移植私有参考线51148b9，当前为待交付批次：已证只读状态、跟踪/老化/IAM、PF/PNom闭环、储能标量、AC/MV/Shed和报告通道。源门禁、owner、精度和公共闭环缺陷先修复；DC比例反算不搬，先定位原生设置/重算入口。逐批另行验收，不把参考提交或既有运行当作移植后的通过证据。混合DC端口、网充、特殊负荷、随机电网停运等未证能力不由本次批准自动升级为完成。

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
