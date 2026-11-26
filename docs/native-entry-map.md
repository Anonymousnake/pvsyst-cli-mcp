# PVsyst 功能入口与支持矩阵

本文档按 PVsyst 并网工程的主要功能模块，整理底层文件字段、CLI/原生文本写入入口以及当前 MCP 工具的支持范围。当前目标版本为 PVsyst CLI/原生文本 `8.1.6`（`rev. 49278`）/ `8.1.7`（`rev. 49382`）。3D 场景与 DC 线缆已收敛为无 GUI 原生写入。

## 状态说明

在逆向和对接 PVsyst 的过程中，不同功能处于不同的打通阶段：

| 阶段 | 含义 |
|---|---|
| 仅识别字段 / 只读 | 已摸清 `.VCx` / `.PRJ` 中的类名、字段或 `Flags` 掩码，`pvsyst_get_variant_parameters` 可读出当前值，但未开放写入接口 |
| GUI 持久化已摸清 | 已对比 GUI 保存前后的文件差异，确认了关联字段与标志位 |
| 仿真消费已验证 | 修改后的文件经 `PVsystCLI.exe` 跑完仿真，且在逐时 CSV 或 PDF 报告中确认了物理量的变化 |
| MCP 已封装 | 已提供带参数校验、预览（`dry_run`）、备份与回滚的公开 MCP 工具 |

## 工程管理与基础工作流

| 功能模块 | 对应工具 | 实现说明与相关文档 |
|---|---|---|
| 工程与变体生命周期 | `inspect`、`create`、`clone_variant`、标题更新、气象源切换（部分位于 `expert` 组） | 标题更新支持单行 UTF-8 校验及 BOM 保留，见[工具参考](tool-reference.md) |
| 站点与气象准备 | `create_site`、`convert_meteo`、`prepare_weather`、`acquire_weather`、`review_weather` | 见[天气准备](workflows-guide.md#3-站点与标准逐时亚小时气象制备pvsyst_prepare_weather)；支持 Open-Meteo ERA5 / PVGIS / NASA POWER 下载、当地标准时（`LegalTime`）对齐与逐时/亚小时（`5/10/15/30/60 min`）`.SIT`/`.MET` 转换 |
| 单方案设计与源工程准备 | `simulate_design`、`prepare_design_project`、`suggest_sizing`、`prepare_sourced_design` | 见[单方案设计输入](workflows-guide.md#1-单方案设计输入与全年流程)；支持零模板 `fixed_unshaded_grid` / `ground_sheds`（含多朝向 `sub_orientations`、有限 3D 地块/障碍物/坡度/3D DEM `terrain`） / `ground_tracker` + `pvBiFacial`、MV/HV `transformers` 与 `auto_from_components` 自动选型；**Stand-Alone 离网光储柴微网（`scope="stand_alone"`）仅由 `pvsyst_simulate_design` 支持，`pvsyst_prepare_design_project` 会直接 `blocked` 拦截** |
| 参数化批量扫描（Batch Sweep） | `pvsyst_run_batch_sweep`、`pvsyst_read_batch_results` | 自动生成 UTF-8 BOM + CRLF `UserBatch/<name>_BatchParams.CSV` 与 `_BatchVariables.RVT`，支持倾角/间距/反照率/积灰/限发/MPPT 扫描，单次 CLI 跑完最多 100 组参数并返回曲线提取、按目标指标排序与基线差值 |
| 物理结果诊断与归因 | `pvsyst_diagnose_results` | 纯 Python 对 SFI 逐时/亚小时结果执行损失链、剪峰、电压限幅、阴影、双面增益与储能循环异常诊断（可选 `.VC0` 根因归因）；`simulate_design` 与 `run_house_task` 自动附带轻量摘要 |
| 亚小时仿真与结果积分 | `pvsyst_prepare_weather(step_minutes=...)`、`pvsyst_build_sfi(time_step="subhour")`、`pvsyst_run_simulation(time_step="subhour")` | 支持 `5/10/15/30 min` 气象 CSV -> `.MEF`（`MeasureStep`）-> `.MET`（`VarMeteoVarAcc=SubHour`）-> `TypeValW=SubHour` SFI 与 `-ts:subhour` 仿真及步长精确能量积分；原生 `pvLoad` 嵌入式快照仍为逐时（60 min）并在仿真步长内保持恒定功率 |
| 固定分块工程拼装 | `prepare_block_project` | 按模板子阵分块的整数倍批量复制组装新工程（`expert` 组，见[工具参考](tool-reference.md)） |
| 工程改参、多方案对比与交付 | `project_handoff`、`run_engineering_task`、`compare_engineering_scenarios`、`export_engineering_delivery` | 见[工程修订、多方案对比与交付](workflows-guide.md#6-多方案对比简报预审与工程交付打包) |
| 原生 PDF 财务评估（`TEcoEvaluation`） | `pvsyst_get_variant_parameters(include_economics=True)`、`pvsyst_update_variant_parameters(native_economics_updates=...)` | 读写变体原生 `EconomicEval=TEcoEvaluation`（`InvestmentCosts`/`OperatingCosts`/`Loans`/电价与折旧参数），开启后 CLI 生成的 PDF 报告原生附带财务评估页；已在 PVsyst 8.1.7 实测通过 |
| 共享电路拓扑读取（只读，可选） | `pvsyst_get_variant_parameters(include_topology=True)` | 读出变体自身持久化的原生 `PVObject_SystemCircuit=pvCircuit` 节点树（节点类型、源行号、原始字段含未知字段）；`subarray_id`/`nelements` 带 `present` 标记，省略属主为 `present=false/value=null` 而非 0，显式 0 保持 0；成员按源序不排序，逐 `StringNode` 报 `NElements`；默认不返回、保持旧响应；不推导计数、电气分组或功率分配 |
| 可研简报与工程输入核对 | `review_engineering_brief`、`review_project_inputs` | 见[工程简报预审](workflows-guide.md#6-多方案对比简报预审与工程交付打包)与[工具参考](tool-reference.md) |
| 遗留 8.0 DCGrid 声明读取（只读） | `pvsyst_get_variant_parameters` | 仅当根 `Version=8.0.x` 且唯一 `SystemType=DCGrid` 时返回 `legacy_read_scope`（源版本与 `TypeRegul`/`VNomAlim`/`VPVMax`/`RGridPV`/`RGridAlim` 原值）；仅作只读展示，不支持 8.1 编辑、升级或推导缺省组件参数 |
| 组件库读写与引用创建 | 组件增删改查、曲线表编辑、`build_sourced_component` | 见[按来源创建组件](workflows-guide.md#2-基于规格书引用创建自定义-pan--ond--btr-组件)；新生成的 `.PAN`/`.OND` 落在工作区 `ComposPV` 下 |
| 电路拓扑与串并联数量 | `validate_variant_structure`、`update_variant_parameters`、`clone/remove_subarray` | 同步维护子阵列、逆变器组、MPPT 与组串节点计数 |
| 仿真运行、结果读取与报告页选择 | `build_sfi`、`run_simulation`、`read_results`、`read_rows`、`report_pages` | PDF 页裁剪直接调用 CLI 原生 `-rp` 参数 |

## 详细损失（Detailed Losses）

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 常数热损系数 $U_c$ | MCP 可读写 | `main_array_updates.UFactor`（范围 `15..100` W/m²·K），要求主阵列标准 `Flags`/`Flags2` |
| 风速热损项 $U_v$ | MCP 可读写（present-only） | `main_array_updates.UVFactor`（原生 `Uv` 字面值，仅已有字段可编辑、不建默认/别名）；适配域为任意有限数值（无原生量程/权威单位：字面 `Uw fact; [W/m2K]` 与量纲 `W/m2K per (m/s)` 存 SourceGap，原生数值校验延后）；接受真实跟踪夹具 `$01900400`/`$082A` 的 8.1.0/8.1.6 Grid 主阵（不要求无几何，保留 80 物体场景与跟踪/遮挡字节），编辑后失效自身派生结果缓存并只清 root 仿真位、保留其余控制位 |
| DC 线缆阻值 / 损失率 | MCP 已封装（免 GUI 原生写入） | `pvsyst_create_dc_cabling_variant`；读取本地 `.PAN` 单二极管模型求解 STC `(Vmp, Imp)` 完成 `mOhm <-> %` 互算（电阻输入精度 `0.001 mOhm`，损失率输入精度 `0.01%`；`.PAN` 可读时 `percent` 与 `resistance` 模式换算并舍入到两位小数后的损失率均必须 `< 99%`），写入 `RSCablageCh`/`RSCablagePC`、`pvMainArray`/`pvSubArray.Flags` 与 `StringNode` `CableDefinition`；内置加密/不可读组件允许 `resistance` 模式（`.PAN` 不可读时会写出 `RSCablagePC=0.00`，`PVsystCLI 8.1.7` 实际消费 `StringNode` 的 `Resistance`，并非线损未生效），拒绝 `percent` 模式；`dry_run=True` 与落盘写入均按大小写无关要求目标 `new_variant` 不存在，`dry_run=True` 预览 diff，写入时清除结果缓存与根仿真位 |
| AC 线缆 | MCP 已封装 | `pvsyst_create_ac_cabling_variant`；同步更新主阵列使能位、子阵列长度（`0.1 m` 精度）与直接 `InverterNode` 的 `CableDefinition` |
| 多级 MV/HV 变压器与夜间断开 | MCP 已封装（已过 8.1.7 实测） | `pvsyst_configure_transformers`（支持单台/并联/级联 MV+HV 变压器、`NightDisconnect` 及 ISO 60228 AC 集电线缆；内置 PVsyst 8.1.7 `PNomInvAC` 损耗基准与 `80%–200%` 容量、正值铭牌损耗、`≤1%` 铁损 / `≤5%` 铜损及 `HV ≥ MV` 电压预检） |
| 组件质量损失 | MCP 可读写 | `subarray_updates.ModuleQualityLoss`（`[-5, 20]%`），同步写入子阵列 `PQualityModU` 与 `PQualityModI`，并清除默认标志位 `0x00100000` |
| LID 与组件 MPP 失配 | MCP 可读写 | `LIDLoss` 与 `PMismMPP`（`0..100%`）；写 `PMismMPP` 时自动清除子阵列默认位 `0x00040000` |
| 组串电压失配 | MCP 可读写 | `string_mismatch_updates`（`default` / `constant`），操作 `PMismString` 与子阵列默认位 bit 17 |
| 积灰损失（Soiling） | MCP 可读写 | `soiling_updates`（`none` / `constant` / `monthly`，`0.1%` 精度），配合 `soiling` SFI 预设可导出 `GlobSlg`/`SlgLoss` |
| 入射角修正（IAM） | MCP 可读写 | `iam_updates` 支持切换 `component` 与 `ashrae`（`b0` 参数） |
| 辅助耗电（Auxiliaries） | MCP 可读写 | `auxiliary_updates` 支持 `none`、`constant`（运行/夜间 kW）及两类带阈值的 `profile` 模式 |
| 组件老化（Ageing） | MCP 可读写 | `ageing_updates`（年数、年均衰减率、成对 RMS 参数）与 `ageing_enabled`（bit 20 开关），要求模板已包含完整老化块 |
| PV 系统不可用时段 | MCP 可读写（已过 8.1.7 实测） | `system_unavailability_updates` 支持 `mode="periods"`（写入 1..1000 条 `date_beg`/`duration_hours` 计划检修/故障时段）、`mode="random"`（按目标年停运比例/时段数/平均时长与随机种子生成）及 `mode="none"` 清除；支持含 3D 场景与多朝向变体叠加 |
| 光谱修正（Spectral Correction） | MCP 可读写（已过 8.1.7 实测） | `spectral_updates` 支持 `mode="first_solar"`（依 `.MET` 含有的 PW/RH 数据计算 First Solar 光谱修正）、`mode="user_monthly"`（12 个月自定义光谱修正系数）与 `mode="none"` 清除 |

## 能量管理（Energy Management）

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 逆变器工作温度 | MCP 可读写 | `inverter_temperature_updates` 支持 `ambient`、`offset`、`fixed` 三种模式，管理 `TempShift`/`TempFixed` 与系统标志位 `$4000`/`$8000` |
| 功率因数（PF）与 PNom 基准 | MCP 可读写 | `power_factor_updates` 直接读写带符号正切值 `TanPhi` / 月度 `TanPhiM`，并可同步设置逆变器 `pnom_limit`（`native`/`active`/`apparent`） |
| 并网点有功限发 | MCP 可读写 | `grid_limit_updates`（无 PF 场景下的 `PLimGrid` 有功上限） |
| PF + 功率上限组合控制 | MCP 可读写 | `power_controls_updates` 统一管理常数/月度 PF、有功/视在限值、逆变器端/注入点位置及 `LimGrSeparLoss` 单独损失记账；注意视在功率限发模式下 `PGridLim` 列头仍标为 `kW` 但实际输出为 kVA 数值，且启用 `LimGrSeparLoss=True` 时 `E_Grid` 已经是扣减限发损失后的上网电量 |
| P50 / P90 概率参数 | MCP 可读写 | `p50_p90_updates` 编辑已有结果模板中的 14 个 `TP50_P90` 字段并输出概率报告页 |

## 阵列朝向、跟踪与 3D 住宅场景

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 固定倾角与多平面管理 | MCP 可读写 | 支持改倾角/方位角、追加固定平面（`clone_orientation`）、子阵重绑定（`NoOrientation`）及删除独占平面（`remove_orientation`） |
| 季节性倾角调整 | MCP 可读写 | `seasonal_updates` 设置夏/冬倾角与冬季月份掩码 `WinterMonths` |
| 朝向类型原地转换 | MCP 已封装 | `pvsyst_convert_orientation`（在 `fixed` / `seasonal` / `sheds` / `tracker_ns` / `tracker_2axis` 之间原地转换，自动清理不兼容的 3D 阴影/结果缓存；拒绝 `dome` 转换） |
| 棚架与遮阳板（Sheds / SunShields） | MCP 已封装 | `pvsyst_create_shed_geometry_variant` 修改现有棚架/遮阳板模板的间距、倾角、排数、宽度与电气阴影开关 |
| 单轴 / 双轴跟踪器参数 | MCP 已封装 | `pvsyst_create_tracking_variant` 支持 9 种原生跟踪类型的所属几何、角度限值、回溯（Backtracking）与间距修改 |
| 无限水平轴跟踪仿真 | MCP 已封装 | `pvsyst_create_tracking_variant` + `pvsyst_run_engineering_task` 支持修改并运行 `UnlimTracking` 全年仿真 |
| 远方地平线（Far Horizon） | MCP 可读写 | `horizon_updates` 写入 4..100 个方位角/仰角点对或清空地平线 |
| 房屋、周边遮挡与光伏 `.pvc2` 建模与回读 | MCP 已封装 | `pvsyst_create_house_pv_scene` 与 `pvsyst_read_house_pv_scene`，支持单朝向多分区、多朝向屋顶及 4 类 `obstacles`（`building`、`tree`、`wall`、`mesh`）生成与 XML 无损还原 |
| 3D 场景免 GUI 原生导入与全年任务 | MCP 已封装（免 GUI 原生写入） | `pvsyst_import_house_pv_scene`、`pvsyst_run_house_task`、`pvsyst_run_sourced_house_task`（支持 `landscape`/`portrait` 竖装、正模块间距、3+ 独立固定朝向屋面、`PowerSharingInverterId >= 0` 共享逆变器、`pvShdGenObject` 与原生 `oeTree`），见[住宅单任务与双屋面说明](workflows-guide.md#4-住宅-3d-场景端到端任务pvsyst_run_house_task) |
| 已保存场景的 PV 表格关联检查 | 只读状态 | `pvsyst_get_variant_parameters` 返回 `shading_scene.pv_bindings`，以及 `module_layout` 保存完整性 |
| Module Layout 精细电气阴影 | MCP 已封装（原生写入 + 显式 `string_layout`） | 导入工具及住宅任务的 `shading_mode="module_layout"` 写入原生组件布局、支持双朝向自动填串与显式逐格 `string_layout` 接线（兼容含 `obstacles`/`oeTree` 场景）；已通过 PVsyst 8.1.7 CLI `ShdElec` 仿真验证，见[原生住宅任务](workflows-guide.md#4-住宅-3d-场景端到端任务pvsyst_run_house_task) |
| 既有 3D 场景阴影重算 | MCP 已封装 | `pvsyst_run_simulation(recompute_shading=True)` 调 CLI `-rs` 参数重新计算阴影表 |
| 地面有限 3D 多边形阵列、障碍物、坡度与 3D DEM 地形 | MCP 已封装（已过 8.1.7 实测） | `pvsyst_simulate_design(scope="ground_sheds", ground_layout={site_polygon, setback_m, obstacles, base_slope_deg, terrain, ...})` 与多朝向 `sub_orientations` 支持 2D 凸/凹多边形自动裁剪排布 `pvShdArrayTable`、叠加 3D 障碍物、非零 `BaseSlope` 与连续 3D DEM 地形贴合（`pvShdGround`，纯 Python 支持 JSON/CSV XYZ，安装 `[geo]` 可选支持 GeoTIFF） |
| 离网 Stand-Alone 光储柴微网 | MCP 已封装（已过 8.1.7 实测） | `pvsyst_simulate_design(scope="stand_alone")` 零模板构建离网 `.PRJ`/`.VC0`（PV 阵列 + `.BTR` 蓄电池 + 可选内置/零模板 `.GEN` 柴油发电机 + 负荷），配合 `stand_alone` SFI 预设与离网柴发经济性评估 |

## 商业组件、多源气象、经济性、自动优化与交互报告

| 功能项 | 当前支持状态 | 底层模块与注意事项 |
|---|---|---|
| 商业组件电气筛选与索引缓存 | MCP 已封装 | `pvsyst_list_components` 支持 `min_power_w`/`max_power_w`/`bifacial`/`min_system_voltage_v`/`min_mppt`/`manufacturer` 筛选，工作区自动维护 mtime 缓存 `.mcp-component-index.json` |
| 规格书无模板 `.PAN`/`.OND` 合成 | MCP 已封装（已过 8.1.7 实测） | `pvsyst_build_sourced_component(template_name=None)` 调用 `single_diode.py` 数值求解五参数单二极管模型（自动写双面 bit 23 `$00800000`）与 Mode-1 8 点逆变器效率曲线；620W 双面 `.PAN` + 110kW `.OND` 已过 PVsystCLI 8.1.7 零警告实测 |
| 多权威气象数据源与一键制备 | MCP 已封装 | `pvsyst_acquire_weather` 支持 `provider="open_meteo_era5" \| "pvgis_tmy" \| "nasa_power"` 及 `prepare=true` 一键串接 `prepare_weather`；`pvsyst_simulate_design` 的 `weather.mode` 同步支持 `pvgis_tmy` 与 `nasa_power` |
| 财务 / 分时电价（TOU）、两部制需量、可组合项目融资与敏感性评估 | MCP 已封装 | `pvsyst_evaluate_economics`（纯 Python，独立于 `.VC0` 的 `TEcoEvaluation`）流式结算小时/子小时 CSV 或批处理汇总表，支持两部制月度需量削峰与按容/按需对比、电池中途更换、可组合项目融资（`debt`、`tax`、`depreciation`、`inflation`、`salvage`、`working_capital`、DSCR/LLCR 与杠杆股权 IRR）、多因子敏感性龙卷风、25 年衰减现金流、LCOE、NPV、IRR、静动态回收期与多方案排名 |
| TOU 自动派生 `PowerShift` 48 槽 & PV+BESS 两阶段联合寻优 | MCP 已封装（v0.5.0） | `pvsyst_plan_tou_storage_schedule` 将 TOU 电价转为 1/2 充放 48 槽排程；`pvsyst_optimize_bess_sizing` Stage 1 纯 Python 毫秒级扫描 `BESS kWh × PCS kW × 策略` 输出 Pareto 前沿与 2D 热力图，Stage 2 可选调 `PVsystCLI` 实机精确校验 Top-K |
| 零模板锂电 `.BTR`、三电压 `.OND` 与 `.PAN` 改参重拟合 | MCP 已封装（v0.5.0，已过 8.1.7 实测） | `pvsyst_build_sourced_component(template_name=None)` 扩展支持 `LFP`/`NMC` 锂电 `.BTR`（按单电芯 ESR ≤ 40 mV@I10 与 `AlphaSOC` 约束校验）与三电压 `.OND`（`$1560` + `VNomEff` + `ProfilPIOV1..3`）；`pvsyst_update_component` 支持三电压曲线编辑及可选 `refit_single_diode=true` `.PAN` 重拟合 |
| 一站式可研编排、CLI 瞬态重试与动态 MCP 资源 | MCP 已封装（v0.5.0） | `pvsyst_run_feasibility_study` 串联组件合成、仿真/断点复用、诊断、TOU/BESS 寻优、经济性/敏感性与双语报告；`PVsystCLI` 支持可配置无窗口早退瞬态重试（`PVSYST_CLI_TRANSIENT_RETRIES`）；新增 3 个只读动态 MCP Resources（`projects`/`components`/`results`）与 2 个工商业光储 Prompts |
| 诊断 → 修正 → 重仿有界优化闭环 | MCP 已封装 | `pvsyst_optimize_variant` 默认 `dry_run=true` 返回确定性修复方案与 diff；`dry_run=false, confirm=true` 在克隆变体上迭代（`max_iterations` 1..5，绝不覆盖基线） |
| Plotly 交互式 HTML 工程报告 | MCP 已封装 | `pvsyst_generate_report` 输出中/英文单方案或多方案对比 HTML 报告（月度发电/PR、损失瀑布图、24h TOU/光储曲线、两部制需量削峰对比、25 年现金流、敏感性龙卷风图、BESS 寻优热力图/Pareto、诊断明细与打印 PDF 样式） |

## 负荷与储能（Loads & Storage）

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 全年逐时负荷 CSV 准备与绑定 | MCP 已封装 | `pvsyst_prepare_load_profile` 校验 8760/8784 小时负荷；`pvsyst_create_native_load_variant(load={"mode":"hourly","load_manifest_name":...})` 将逐时数据编码写入 `pvLoad` 的 `Energy/MJ` base62 缓存（PVsyst 仿真直接消费该内嵌缓存——仅修改外部 CSV 或 `HourlyFile` 仍会继续沿用旧负荷，改完 CSV 须重跑准备与重绑定工具），见[AC 光储评估](workflows-guide.md#5-ac-耦合光储工程pvsyst_run_engineering_task--engineering-storage) |
| 常数 / 月度 / 日曲线 / 家用电器 / 概率分布负荷 | MCP 已封装 | `pvsyst_create_native_load_variant` 在纯光伏变体上配置常数（`kW`）、月度（12 个月 `kWh`）、日曲线（24 小时 `kW`）、家用电器（`mode="household"`，全年或分季）与概率分布（`mode="probability"`）自发自用负荷 |
| 集中式 AC 耦合光储系统构建 | MCP 已封装 | `pvsyst_create_grid_storage_system` 在纯光伏变体（兼容 3D 阴影/多朝向）上挂载工作区或内置 `.BTR` 锂电/铅酸电池，支持 `SelfConsumption`、`WeakGrid`（支持外部 CSV 与确定性 `mode="generated"` 停电生成器）、`PeakShaving`（无需手动负荷，写入 `PLimGrid`）与 `PowerShift`（`PVProdFraction` / `SpecChargPower` / `PVToGridMin` + `SpecifiedHours` / `Night` / `MinMaxHour` 的 48 槽 `TParSimulSeries`；注意 PVsyst 8.1.7 中 `PowerShift` 仅使用可用光伏发电充电，含 `SpecChargPower` 模式，不支持电网充电），见[AC 光储评估](workflows-guide.md#5-ac-耦合光储工程pvsyst_run_engineering_task--engineering-storage) |
| 既有光储系统参数修改 | MCP 已封装 | `pvsyst_create_grid_storage_variant` 修改 SOC、充放电功率、效率、`PowerShift` 48 槽充放电曲线、`PeakShaving` limits/discharge timing 及 `allow_grid_export`（系统 bit 27 防逆流位），保留 `pvShading`/`pvModuleLayout` 并自动清理 `VarList` |
| 削峰（Peak Shaving）放电窗口 | MCP 已封装 | 由 `pvsyst_create_grid_storage_variant` 统一设置 `strategy="peak_shaving"` 与 `discharging_strategy`（`after_sunset` / `hour_window`） |
| 完整 AC 光储能量流导出 | MCP 已封装 | `pvsyst_build_sfi(variables="grid_storage")` 自动包含 `CL_Over`（变流器限幅损失）等完整能量平衡列 |
| DC 耦合混储 | 实测边界锁定 | PVsyst 8.1.7 并网储能仅实现 Common AC Bus（无 DC 直充电池端口），`coupling` 锁定 `"ac_coupled"` 并在传入 `"dc_coupled"`/`"hybrid"` 时显式拒接 |
