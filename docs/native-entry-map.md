# 原生功能入口地图

本地图按并网教程的功能组织PVsyst入口、当前MCP能力及支持范围。运行版本为GUI/CLI8.1.6，GUI桥另要求中文8.1.6rev49278／DPI96。8.1.0文件声明可被部分入口读取或编辑，不代表运行过8.1.0可执行程序。

## 状态含义

| 层级 | 含义 |
|---|---|
| 候选 | 仅有教程、窗口、符号或字段线索，尚未完成可靠绑定 |
| GUI持久化 | 界面设置与保存记录已对应，仍不等于参与计算 |
| 原生消费 | 对应输入已由软件运行，并在小时结果或报告体现；验证时段不外推 |
| MCP支持 | 另按公开工具的版本、模板、字段、依赖和恢复条件判断 |

可读、可保存、可运行及可创建／编辑是不同能力。结构一致不等于电气或工程认证；needs_review仍需处理，不由成功运行或ZIP导出自动清除。

## 工程与输入

| 功能 | 当前入口与支持范围 | 说明 |
|---|---|---|
| 工程／变体生命周期 | inspect、create、clone、归档／恢复、标题和源事务 | [项目来源与事务](project-source-verification.md)，完整文本导入不生成物理默认设计 |
| 站点／气象 | create_site、convert_meteo、prepare_weather、acquire_weather、review_weather | [天气准备](weather-preparation.md)，SIT/MET绑定、年历和时基分别核对 |
| 单方案与源准备 | simulate_design、prepare_design_project、prepare_sourced_design | [设计输入](single-design-inputs.md)，准备不执行全年、不生成结果 |
| 分块工程 | prepare_block_project | [分块模板](block-project-preparation.md)，受限固定独占块，不扩展共享变压器或几何 |
| 工程变更／比较／交付 | project_handoff、run_engineering_task、compare_engineering_scenarios、export_engineering_delivery | [工程交付](engineering-delivery.md)，固定、UnlimTracking、既有AC储能各按独立范围 |
| 声明审查 | review_engineering_brief、review_project_inputs | [输入复核](project-input-review.md)，只读差异和缺项，不猜厂家或气候依据 |
| 元件与引用 | 元件读取／复制／曲线及project_components | [来源元件](sourced-components.md)，新loose模型须单独确认消费，不用简短文件遮蔽数据库模型 |
| 电路、数量与朝向 | validate_variant_structure、变体参数、clone/remove_subarray | [拓扑编辑](grid-capacity-verification.md)，完整计数、所属关系、模式与几何条件不可省略 |
| 运行／结果／原生报告 | build_sfi、run_simulation、read_results、read_rows及report_pages | [报告通道](automatic-simulation.md)，小时年覆盖、上下文、单位及原生-rp选择；不后裁PDF冒充原生页选择 |

## 详细损失

| 功能 | 原生与MCP状态 | 支持范围／说明 |
|---|---|---|
| 常数Uc | main_array_updates.UFactor，已有配置及原生消费 | W/m²/K，15..100；GUI往返对照限24h，另有48h标量控制；[保存边界](loss-gui-roundtrip.md) |
| Uv及热模式 | GUI持久化；非零风速正向消费仍待确认，无专用编辑 | 不因Uc支持而扩成完整热模型；[详细损失](native-detailed-losses.md) |
| NOCT规格比较 | 比较／元数据，不是该页实际仿真输入 | 不发明配置参数 |
| DC线缆 | 原生GUI薄桥，设置、原生重算、另存／重载及全年消费 | [DC入口](native-dc-cabling.md)，受限单固定子阵，无AC/MV/HV，不移植比例反算 |
| AC线缆 | 均一线缆多所属对象读改闭环；全年及部分48h控制 | [AC入口](native-ac-cabling.md)，不扩展MV/HV下游或任意变压器 |
| MV夜间断开 | 单默认MV模板的两态全年消费 | [MV入口](native-mv-night.md)，数量、HV、自定义表及任意损耗参数只读 |
| 组件质量 | ModuleQualityLoss同步PQualityModU/I并清默认位 | [-5,20]%，P缓存只读；GUI往返限24h，不推导厂家容差默认 |
| LID／模块失配 | LIDLoss／PMismMPP，受限原生配置与消费 | 0..100%，PMismMPP须清所属默认位；[损失标量](grid-capacity-verification.md) |
| 组串电压失配 | string_mismatch_updates，默认／常数与全年消费 | PMismString及所属bit17，独立事务，不是PMismMPP或详细研究工具 |
| 积灰 | soiling_updates，常数／月度及全年消费 | 0.1百分点精度；[积灰](soiling-verification.md)，不自动推导清洗／积雪 |
| IAM | component／ASHRAE受限配置，全年原生消费 | [IAM输入](native-model-inputs.md)，其他Fresnel／UserProfile／PAN曲线编辑未开放 |
| 辅助功率 | constant／none及两类profile，原生全年消费 | [常数](native-auxiliary-power.md)／[profile](native-detailed-losses.md)，比例／阈值族不扩成任意混合 |
| 老化 | 年数／均值、已有模板启停及成对RMS | [模型输入](native-model-inputs.md)，均值有全年控制；RMS与启停消费限48h，样本复用未证 |
| PV系统停运 | 保存状态可读，尚无专用配置API | 随机时段生成／消费未形成可靠配置入口；不等于WeakGrid停电 |
| 光谱 | 原生入口依赖MET中的PW/RH等数据 | 无专用配置；缺数据不生成替代光谱模型 |
| 交互行为图 | 原生功能候选，无专用调用／导出 | 不等于已导出的全年PDF损失图 |

## 能量管理

| 功能 | 当前状态 | 支持范围／说明 |
|---|---|---|
| 逆变器温度 | ambient／offset／fixed，全年消费 | [温度](inverter-temperature.md)，两保存值与模式位，非模块Uc |
| PF与PNom | 常数／月度领先滞后及native／active／apparent | [PF](power-factor.md)，原生正切与限定组合，全年控制，不换算Cosφ |
| 有功注入限值 | NoPF独立数值入口，全年消费 | [限值](grid-power-limit.md)，bit27仅允许注入，不是数值上限或网充 |
| PF＋有功／视在限值 | 受限组合事务及全年消费 | [组合](native-power-controls.md)，按端点／损失分类／模式约束，不开放任意活动AC耦合 |
| 限值位置与下游AC | 原生不同位置消费已见，非零AC对照限24h | 不从无AC损耗的等值输出推断通用位置等价 |
| P50/P90 | 既有原生结果模板十四字段编辑及概率报告 | [P50](p50-native-parameters.md)，不创建完整无结果专页，不支持ClimateChange／累计图 |

## 朝向与住宅场景

| 功能 | 当前入口与原生状态 | 范围 |
|---|---|---|
| 普通固定平面 | 角度、复制、分配、删除；独占MPPT生命周期消费限48h | 非平均、完整计数、无活动几何；[拓扑](grid-capacity-verification.md) |
| 季节倾角 | seasonal_updates，保存月mask并由原生全年消费 | [季节](seasonal-orientation.md)，FieldTilt=0不是全年水平；恢复固定用备份 |
| Dome | 既有配对模板的双面串数重分配及全年消费 | [Dome](native-dome-allocation.md)，40串同子阵10/±90模板，不首次构造／3D／背面增益 |
| Sheds／SunShields | create_shed_geometry_variant，读改及48h消费 | [棚架](native-shed-controls.md)，既有类型，非任意组合、3D或电气分组计算 |
| UnlimTracking及其他跟踪族 | 既有记录受限新VC；新增控制消费限48h | [跟踪](native-tracking-controls.md)，各类型所属字段；不外推风收拢阈值或所有回溯组合 |
| UnlimTracking完整任务 | simulate_template跟踪分支，完整年度与原生报告 | [跟踪任务](tracking-template-flow.md)，精确继承引用及显式功率来源，不转类型 |
| 远方地平线 | profile／none、原生HOR及全年消费 | [地平线](horizon-profile.md)，100槽／FracAlbedo=1，不自行插值 |
| 房屋场景作者／读取 | create_house_pv_scene、read_house_pv_scene及反馈 | [住宅建模](house-pv-modeling.md)，标准PVCollada2薄序列化，不是CAD／遮挡内核 |
| 原生导入关联 | import_house_pv_scene，GUI导入／有效性／保存／重载 | 匹配已开工程，录制版本布局，GUI仅1..2区；生成与关联不是同一步 |
| 原生住宅单任务 | run_house_task、run_sourced_house_task | [任务](native-house-task.md)，自有GUI退出后新CLI全年；恢复不增跑，review保留 |
| 受限双屋面 | 原生逐表朝向、归位、严格保存／重载和全年消费 | [范围](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)，单栋anchor0／两独占子阵／同PAN-OND；非通用坐标验证器 |
| 场景保存声明 | pv_bindings只读stored_consistent等状态 | [关联读取](native-scene-bindings.md)，不等于源几何或原生消费认证 |
| 既有场景重算 | run_simulation(recompute_shading=...) | 原生遮挡表及全年结果；Module Layout／电气阴影编辑未开放，不扩大无阴影编辑范围 |
| 场景其他属性 | 原生投影属性、坐标变换等仅局部入口 | 非工具菜单全集，无家具、材料、周边环境或通用场景维护API |

## 负荷与储能

| 功能 | 当前入口与状态 | 范围 |
|---|---|---|
| 小时负荷 | prepare_load_profile、create_load_variant | [负荷](storage-loads.md)，完整年／标准偏移，绑定原生缓存并逐点核对E_User |
| 常数／月度／日曲线负荷 | create_native_load_variant，PV-only自用及全年消费 | 原生月kWh／24项日kW；不外推概率／家庭电器或自造曲线 |
| 概率／家庭电器负荷 | 部分GUI记录；无专用配置API | [原生负荷](native-load-storage-controls.md)，保留生成与依赖边界 |
| AC耦合储能硬件 | create_grid_storage_system、BTR及串并联／温度／SOW | [储能](grid-storage-parameters.md)，显式输入不等于BMS或选型认证 |
| SelfConsumption／WeakGrid | 已有构造、标量、可用性快照与原生消费 | 备用SOC不等于硬低；停电放电按原生策略，LegalTime不因晴空提示平移 |
| 原策略标量 | 保留SelfConsumption／WeakGrid／Peak策略再编辑 | 功率／效率／SOC控制限48h；电池侧功率不直接当AC上限 |
| 削峰与时序 | 已有Peak／PVToGridMin模板、受限时序及全年消费 | [削峰](native-peak-shaving.md)，固定硬件／限值profile，不扩任意策略 |
| 全AC能量流／SOC | grid_storage SFI包含CL_Over等原生列 | 原样保留导出残差与单位，不裁剪或以PV-only PR评价储能 |
| 随机电网停运 | GUI生成尚未形成有效保存／消费入口 | 与PV系统停运分开，无替代随机调度器 |
| 网充／混合逆变器DC电池端口 | 候选，激活链尚未闭合，无已支持配置API | 不以AC耦合或静态类名证明DC及网充能力 |
