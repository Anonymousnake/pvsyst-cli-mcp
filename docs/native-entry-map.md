# PVsyst 功能入口与支持矩阵

本文档按 PVsyst 并网工程的主要功能模块，整理底层文件字段、CLI/GUI 调用入口以及当前 MCP 工具的支持范围。默认测试环境为 PVsyst CLI/GUI 8.1.6（涉及桌面窗口自动化的入口要求中文版 8.1.6 rev49278、96 DPI）。

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
| 工程与变体生命周期 | `inspect`、`create`、`clone`、归档/恢复、标题更新、气象源切换 | 见[工程文件与事务](project-source-verification.md)、[项目显示标题](project-title.md) |
| 站点与气象准备 | `create_site`、`convert_meteo`、`prepare_weather`、`acquire_weather`、`review_weather` | 见[天气准备](weather-preparation.md)；支持 Open-Meteo ERA5 下载、当地标准时（`LegalTime`）对齐与 `.SIT`/`.MET` 转换 |
| 单方案设计与源工程准备 | `simulate_design`、`prepare_design_project`、`prepare_sourced_design` | 见[单方案设计输入](single-design-inputs.md)、[源工程准备](design-project-preparation.md) |
| 固定分块工程拼装 | `prepare_block_project` | 见[分块工程准备](block-project-preparation.md)；按模板子阵分块的整数倍批量复制组装新工程 |
| 工程改参、多方案对比与交付 | `project_handoff`、`run_engineering_task`、`compare_engineering_scenarios`、`export_engineering_delivery` | 见[工程修订与交付](engineering-delivery.md)、[多方案对比](engineering-scenarios.md) |
| 可研简报与工程输入核对 | `review_engineering_brief`、`review_project_inputs` | 见[工程简报预审](engineering-brief.md)、[工程输入复核](project-input-review.md) |
| 组件库读写与引用创建 | 组件增删改查、曲线表编辑、`build_sourced_component` | 见[按来源创建组件](sourced-components.md)；新生成的 `.PAN`/`.OND` 落在工作区 `ComposPV` 下 |
| 电路拓扑与串并联数量 | `validate_variant_structure`、`update_variant_parameters`、`clone/remove_subarray` | 见[并网拓扑与参数编辑](grid-capacity-verification.md)；同步维护子阵列、逆变器组、MPPT 与组串节点计数 |
| 仿真运行、结果读取与报告页选择 | `build_sfi`、`run_simulation`、`read_results`、`read_rows`、`report_pages` | 见[模板自动仿真](automatic-simulation.md)；PDF 页裁剪直接调用 CLI 原生 `-rp` 参数 |

## 详细损失（Detailed Losses）

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 常数热损系数 $U_c$ | MCP 可读写 | `main_array_updates.UFactor`（范围 `15..100` W/m²·K），要求主阵列标准 `Flags`/`Flags2`；见[损失 GUI 往返](loss-gui-roundtrip.md) |
| 风速热损项 $U_v$ | 仅 GUI 保存对照，未开放写入 | 对应 `pvMainArray.UVFactor`；当前未在 `main_array_updates` 中开放修改 |
| DC 线缆阻值 / 损失率 | MCP 已封装（通过 GUI 桥接） | `pvsyst_create_native_dc_cabling_variant`；驱动已打开的 8.1.6 GUI 填入阻值或百分比，由 PVsyst 自动重算关联字段并另存新变体，见[DC 线缆桥接](native-dc-cabling.md) |
| AC 线缆 | MCP 已封装 | `pvsyst_create_ac_cabling_variant`；同步更新主阵列使能位、子阵列长度（`0.1 m` 精度）与直接 `InverterNode` 的 `CableDefinition`，见[AC 线缆配置](native-ac-cabling.md) |
| MV 变压器夜间断开 | MCP 已封装 | `pvsyst_create_mv_night_disconnect_variant`；切换单台默认 MV 变压器的 `NightDisconnect` 与主阵列 bit 29，见[MV 夜间断开](native-mv-night.md) |
| 组件质量损失 | MCP 可读写 | `subarray_updates.ModuleQualityLoss`（`[-5, 20]%`），同步写入子阵列 `PQualityModU` 与 `PQualityModI`，并清除默认标志位 `0x00100000` |
| LID 与组件 MPP 失配 | MCP 可读写 | `LIDLoss` 与 `PMismMPP`（`0..100%`）；写 `PMismMPP` 时自动清除子阵列默认位 `0x00040000`，见[并网拓扑与参数编辑](grid-capacity-verification.md) |
| 组串电压失配 | MCP 可读写 | `string_mismatch_updates`（`default` / `constant`），操作 `PMismString` 与子阵列默认位 bit 17，见[详细损失参数](native-detailed-losses.md) |
| 积灰损失（Soiling） | MCP 可读写 | `soiling_updates`（`none` / `constant` / `monthly`，`0.1%` 精度），配合 `soiling` SFI 预设可导出 `GlobSlg`/`SlgLoss`，见[积灰配置](soiling-verification.md) |
| 入射角修正（IAM） | MCP 可读写 | `iam_updates` 支持切换 `component` 与 `ashrae`（`b0` 参数），见[老化与 IAM 输入](native-model-inputs.md) |
| 辅助耗电（Auxiliaries） | MCP 可读写 | `auxiliary_updates` 支持 `none`、`constant`（运行/夜间 kW）及两类带阈值的 `profile` 模式，见[辅助功率](native-auxiliary-power.md)与[详细损失参数](native-detailed-losses.md) |
| 组件老化（Ageing） | MCP 可读写 | `ageing_updates`（年数、年均衰减率、成对 RMS 参数）与 `ageing_enabled`（bit 20 开关），要求模板已包含完整老化块，见[老化与 IAM 输入](native-model-inputs.md) |
| PV 系统不可用时段 | 只读状态 | `pvsyst_get_variant_parameters` 可读出已保存的停运配置与时段，暂未开放生成/写入接口 |
| 光谱修正 | 未开放 | 依赖 `.MET` 内含可降水量（PW）或相对湿度（RH）数据 |

## 能量管理（Energy Management）

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 逆变器工作温度 | MCP 可读写 | `inverter_temperature_updates` 支持 `ambient`、`offset`、`fixed` 三种模式，管理 `TempShift`/`TempFixed` 与系统标志位 `$4000`/`$8000`，见[逆变器温度](inverter-temperature.md) |
| 功率因数（PF）与 PNom 基准 | MCP 可读写 | `power_factor_updates` 直接读写带符号正切值 `TanPhi` / 月度 `TanPhiM`，并可同步设置逆变器 `pnom_limit`（`native`/`active`/`apparent`），见[功率因数](power-factor.md) |
| 并网点有功限发 | MCP 可读写 | `grid_limit_updates`（无 PF 场景下的 `PLimGrid` 有功上限），见[并网功率上限](grid-power-limit.md) |
| PF + 功率上限组合控制 | MCP 可读写 | `power_controls_updates` 统一管理常数/月度 PF、有功/视在限值、逆变器端/注入点位置及 `LimGrSeparLoss` 单独损失记账，见[组合功率控制](native-power-controls.md) |
| P50 / P90 概率参数 | MCP 可读写 | `p50_p90_updates` 编辑已有结果模板中的 14 个 `TP50_P90` 字段并输出概率报告页，见[P50/P90 参数](p50-native-parameters.md) |

## 阵列朝向、跟踪与 3D 住宅场景

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 固定倾角与多平面管理 | MCP 可读写 | 支持改倾角/方位角、追加固定平面（`clone_orientation`）、子阵重绑定（`NoOrientation`）及删除独占平面（`remove_orientation`），见[并网拓扑与参数编辑](grid-capacity-verification.md) |
| 季节性倾角调整 | MCP 可读写 | `seasonal_updates` 设置夏/冬倾角与冬季月份掩码 `WinterMonths`，见[季节倾角](seasonal-orientation.md) |
| 双朝向 Dome 分配 | MCP 已封装 | `pvsyst_create_dome_allocation_variant` 在已有配对 `Dome` 模板上重新分配正反两面组串数，见[Dome 双朝向分配](native-dome-allocation.md) |
| 棚架与遮阳板（Sheds / SunShields） | MCP 已封装 | `pvsyst_create_shed_geometry_variant` 修改现有棚架/遮阳板模板的间距、倾角、排数、宽度与电气阴影开关，见[棚架与遮阳板](native-shed-controls.md) |
| 单轴 / 双轴跟踪器参数 | MCP 已封装 | `pvsyst_create_tracking_variant` 支持 9 种原生跟踪类型的所属几何、角度限值、回溯（Backtracking）与间距修改，见[跟踪控制](native-tracking-controls.md) |
| 无限水平轴跟踪模板仿真 | MCP 已封装 | `pvsyst_simulate_template` 支持直接跑基于 `UnlimTracking` 模板的全年仿真流程，见[跟踪模板流程](tracking-template-flow.md) |
| 远方地平线（Far Horizon） | MCP 可读写 | `horizon_updates` 写入 4..100 个方位角/仰角点对或清空地平线，见[地平线轮廓](horizon-profile.md) |
| 房屋与光伏 `.pvc2` 建模与回读 | MCP 已封装 | `pvsyst_create_house_pv_scene` 与 `pvsyst_read_house_pv_scene`，支持单朝向多分区与双坡屋顶场景生成及 XML 逆向还原，见[住宅与光伏建模](house-pv-modeling.md) |
| 3D 场景 GUI 导入与全年任务 | MCP 已封装 | `pvsyst_import_house_pv_scene`、`pvsyst_run_house_task`、`pvsyst_run_sourced_house_task`，见[住宅单任务](native-house-task.md)与[双屋面交付说明](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance) |
| 已保存场景的 PV 表格关联检查 | 只读状态 | `pvsyst_get_variant_parameters` 返回 `shading_scene.pv_bindings`，核对表格朝向引用与组件数，见[PV 区域关联读取](native-scene-bindings.md) |
| 既有 3D 场景阴影重算 | MCP 已封装 | `pvsyst_run_simulation(recompute_shading=True)` 调 CLI `-rs` 参数重新计算阴影表 |

## 负荷与储能（Loads & Storage）

| 功能项 | 当前支持状态 | 底层字段与注意事项 |
|---|---|---|
| 全年逐时负荷 CSV 准备与绑定 | MCP 已封装 | `pvsyst_prepare_load_profile` 校验 8760/8784 小时负荷；`pvsyst_create_load_variant` 将逐时数据编码写入 `pvLoad` 的 `Energy/MJ` base62 缓存，见[逐时负荷与原生绑定](storage-loads.md) |
| 常数 / 月度 / 24 小时日曲线负荷 | MCP 已封装 | `pvsyst_create_native_load_variant` 在纯光伏变体上配置常数（`kW`）、月度（12 个月 `kWh`）或日曲线（24 小时 `kW`）自发自用负荷，见[逐时负荷与原生绑定](storage-loads.md) |
| 集中式 AC 耦合光储系统构建 | MCP 已封装 | `pvsyst_create_grid_storage_system` 在纯光伏变体上挂载电池包、充放电功率、SOC 阈值、月度电池温度与逐时负荷，支持 `SelfConsumption` 与 `WeakGrid`（含电网可用性 0/1 序列绑定），见[光储系统与参数](grid-storage-parameters.md) |
| 既有光储系统参数修改 | MCP 已封装 | `pvsyst_create_grid_storage_variant` 在保留原有储能策略（含 `PeakShaving`）的前提下修改 SOC、充放电功率、效率与 `allow_grid_export`（系统 bit 27 防逆流位） |
| 削峰（Peak Shaving）放电窗口 | MCP 已封装 | `pvsyst_create_peak_shaving_variant` 修改现有削峰模板的放电时序（`AfterSunset` 或起止小时窗口），见[削峰放电控制](native-peak-shaving.md) |
| 完整 AC 光储能量流导出 | MCP 已封装 | `pvsyst_build_sfi(variables="grid_storage")` 自动包含 `CL_Over`（变流器限幅损失）等完整能量平衡列 |
| 概率/家用电器负荷、随机停电、DC 耦合混储 | 仅分析记录，未开放写入 | 底层字段调查见[负荷与储能模式笔记](native-load-storage-controls.md) |
