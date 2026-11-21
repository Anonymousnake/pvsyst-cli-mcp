# GUI 与 MCP 能力对照及逆向笔记

全部公开工具的调用签名见[工具参考](tool-reference.md)，各功能模块的参数范围和底层字段见[功能入口地图](native-entry-map.md)。本文档记录哪些功能直接通过明文工程文件读写实现、哪些通过 CLI/GUI 桥接实现，以及还有哪些 PVsyst 桌面端能力尚未封装。

## 高层工程任务主线

除了单点改参工具外，仓库提供了一套串联“输入审查 → 隔离改参仿真 → 多方案比选 → 交付打包”的工程级工作流（详见[工程修订与交付](engineering-delivery.md)）：
1. **固定倾角 / `UnlimTracking` 工程修订**：基于现有工程克隆独立副本，修改倾角、方位角、详细损失或跟踪器参数，跑完全年仿真并生成中文交接说明。
2. **集中式 AC 光储对比评估**：绑定全年逐时负荷与储能充放电参数，对比不同储能配置下的自用电量、弃光与 SOC 表现（见[光储工程评估](engineering-storage.md)）。
3. **双坡屋顶住宅 3D 场景交付**：生成单栋双坡屋顶 `.pvc2` 场景，通过无 GUI 原生写入完成 3D 场景构建、逐表朝向绑定与 `module_layout` 接线，再交由 CLI 跑全年仿真并打包交付（见[双屋面交付说明](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。

## 各领域实现方式与限制

在对接 PVsyst 8.1.6 / 8.1.7 时，自 **`v0.6.0`** 起全仓库已实现 **Grid-Connected（并网）+ Stand-Alone（离网 PV+BESS+柴油备发）100% 纯无头化（Headless）覆盖**。旧版 Win32 GUI 点击桥（`native/scene.py`）已彻底移除，全链路仅通过官方 `PVsystCLI.exe` + 明文 `.PRJ`/`.VCx`/`.PAN`/`.OND`/`.BTR`/`.GEN`/`.SIT` 原生解析与零模板合成运行；`pvsyst_open_native_project`（`expert` 组）仅保留为轻量化交互查看辅助工具（不执行任何 Win32 点击）。

| 领域 | 当前实现方式 | 关键技术细节与限制 |
|---|---|---|
| 工程与变体文件 | 直接读写 `.PRJ` / `.VCx` 文本 | 支持新建、克隆、归档、恢复、改标题（自动处理 UTF-8 BOM）与切换 `.SIT`/`.MET`；见[工程文件与事务](project-source-verification.md)、[项目显示标题](project-title.md) |
| 并网拓扑与容量 | 解析修改 `.VCx` 电路树 | 支持常规及独立 MPPT（`$0400`）子阵列的逆变器数、MPPT 数、并联串数、串联组件数调整，以及受限共享逆变器（`PowerSharingInverterId`）启停和子阵列克隆/删除；见[并网拓扑与参数编辑](grid-capacity-verification.md) |
| 中高压变压器与 AC 集电网络 | 直接构建 `pvCircuit` 多级 `TransformerNode`（`pvsyst_configure_transformers`） | 支持单台 MV、多台并联 MV、MV+HV 级联变压器（含自动补齐 8.1.7 必需的 HV 铭牌参数 `HVTrfDS*`）、ISO 60228 标准截面（`10..1200 mm²`）MV/HV 集电线缆与夜间断开开关，并可导出 `EMVOhmL`/`EMVTrfL`/`EHVOhmL`/`EHVTrfL` |
| 组件库标量与零模板合成 | 直接读写/合成 8.1.6 / 8.1.7 明文 `.PAN`/`.OND`/`.BTR`/`.GEN` | 支持零模板合成 `.PAN`（单二极管拟合）、单/三电压 `.OND`、锂电 `.BTR`（LFP/NMC）与柴油/燃气 `.GEN`；支持已有 `.PAN` 改参可选 `refit_single_diode=true` 自动重拟合；内置加密 `.PAN` 支持整文件引用及 `resistance` 模式 DC 线缆 |
| OND / BTR 曲线 | 直接读写 Mode 1 点表 + CSV 导入导出 | 支持单/三电压 OND `ProfilPIO`/`ProfilPIOV1..3` 效率曲线与 BTR `CapaCourant` 容量曲线的 4..256 点表修改；修改点数时自动清空旧 `a/b/c` 三次样条缓存系数；见[曲线表编辑](curve-table-verification.md) |
| 朝向与跨朝向转换 | 直接读写/原地转换 `.VCx` 朝向块（`pvsyst_convert_orientation`） | 支持 `fixed`（固定倾角）↔ `seasonal`（季节倾角）↔ `sheds`（无限棚架）↔ `tracker_ns`（水平单轴追踪）↔ `tracker_2axis`（双轴追踪）↔ `dome`（东西双坡）原地跨朝向转换，自动清理不兼容 3D 阴影与结果缓存 |
| 详细损失、光谱修正与原生财务 PDF | 修改 `.VCx` 的 `pvMainArray` / `pvSubArray` / `pvSystem` / `TEcoEvaluation` | 涵盖 $U_c$、组件质量、LID、MPP/组串失配、积灰、IAM、老化、辅助功率、逆变器温度、功率因数（`TanPhi`）、并网点限发（`PLimGrid`）、P50/P90、免 GUI DC 线缆、**First Solar 光谱修正（`UseSpectralCorr` + `SpectralCorrection`）**、**随机 PV 故障（`mode="random"` 确定性物化 `UnavailPeriods`）** 与 **原生 `EconomicEval=TEcoEvaluation` 写入**（令 CLI PDF 报告直接输出 `econeval`/`financialbalance` 财务页） |
| 气象数据 | Open-Meteo ERA5 / PVGIS v5.3 TMY / NASA POWER + CLI `convert-meteo` | 自动对齐当地冬季标准时（`LegalTime`，写入 `.MEF` 的 `RefTime=LegalTime` 以确保 `.MET` 内 `HLeg=True`），支持逐时与亚小时（`5/10/15/30/60 min`）；见[天气输入检查](weather-input-verification.md)、[天气准备](weather-preparation.md) |
| 负荷、弱电网与 AC 储能 | 编码 `pvLoad`（含 `HouseHold`/`Probability`）+ `pvGridStorage` + `WeakGrid` | 支持恒定/逐月/逐日/逐时、`HouseHold` 家用电器分项（年度/季节 24-bit 半小时掩码）与 `Probability` 概率分布负荷（确定性采样至嵌入式逐时快照以供 Grid 变体消费）；支持 `SelfConsumption`、`WeakGrid`（含 `mode="generated"` 确定性停电序列生成器）、`PeakShaving` 与 `PowerShift` 48 槽两充两放策略 |
| 3D 住宅、地面阵列与 3D DEM 地形 | `.pvc2` / 原生 `pvShading` + 纯 Python Delaunay TIN `pvShdGround` | 支持屋顶房屋、周边遮挡（`building`/`oeTree`/`wall`/`mesh`）、`module_layout` 逐格接线、有限 3D 地块多边形与 **3D 连续地形 DEM（`pvShdGround`，支持 XYZ JSON/CSV、经纬度 ENU 投影及可选 `[geo]` 扩展 `rasterio` GeoTIFF）**，可将固定棚架与单轴追踪支架直接贴合地形高程与坡度 |
| Stand-Alone 离网光储柴微网 | 零模板合成 `SystemType=Battery`（`pvsyst_simulate_design(scope="stand_alone")`） | 完整支持 `.PAN` + 铅酸/锂电 `.BTR` + MPPT/PWM `.RLT` 控制器 + 可选柴油/燃气 `.GEN` 备用发电机（支持在线自动合成 `.GEN`）+ 全类型 `pvLoad` + 离网 SFI 变量组（`E_User`/`E_Solar`/`E_BkUp`/`E_Miss`/`EUnused`/`BkUp_ON`/`FuelBU`/`SOCmean`/`T_LOL`）+ 离网诊断与含柴油油耗/运维成本的全生命周期 LCOE |

## 已验证的 PVsyst 8.1.7 引擎边界说明

以下能力边界已在 PVsyst 8.1.7 原生引擎上实测验证，并提供了明确的工程替代路径：
- **并网储能（`pvGridStorage`）电网充电（Grid-to-Battery Charging）**：PVsyst 8.1.7 原生 `pvGridStorage` 仅支持由光伏阵列为电池充电（不支持原生从电网购电给电池充电）。若需进行“谷电购电充电 + 峰电放电”的 TOU 套利建模，请使用本仓库内置的纯 Python 一阶段光储寻优引擎（`pvsyst_optimize_bess_sizing` 中开启 `allow_grid_charging=true`）。
- **其他非光伏/非微网专用模块**：太阳能抽水（`SystemType=Pumping`）与直流直驱微网（`SystemType=DCGrid`）不在当前 Grid + Stand-Alone 100% 范围内，后续按需扩展。
