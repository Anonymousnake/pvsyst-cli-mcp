# GUI 与 MCP 能力对照及逆向笔记

全部公开工具的调用签名见[工具参考](tool-reference.md)，各功能模块的参数范围和底层字段见[功能入口地图](native-entry-map.md)。本文档记录哪些功能直接通过明文工程文件读写实现、哪些通过 CLI/GUI 桥接实现，以及还有哪些 PVsyst 桌面端能力尚未封装。

## 高层工程任务主线

除了单点改参工具外，仓库提供了一套串联“输入审查 → 隔离改参仿真 → 多方案比选 → 交付打包”的工程级工作流（详见[工程修订与交付](engineering-delivery.md)）：
1. **固定倾角 / `UnlimTracking` 工程修订**：基于现有工程克隆独立副本，修改倾角、方位角、详细损失或跟踪器参数，跑完全年仿真并生成中文交接说明。
2. **集中式 AC 光储对比评估**：绑定全年逐时负荷与储能充放电参数，对比不同储能配置下的自用电量、弃光与 SOC 表现（见[光储工程评估](engineering-storage.md)）。
3. **双坡屋顶住宅 3D 场景交付**：生成单栋双坡屋顶 `.pvc2` 场景，通过 GUI 桥完成导入、逐表朝向关联、坐标归位与阴影表计算，再交由 CLI 跑全年仿真并打包交付（见[双屋面交付说明](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。

## 各领域实现方式与限制

在对接 PVsyst 8.1.6 / 8.1.7 时，全仓库主路径已收敛为“官方 `PVsystCLI.exe` + 明文 `.PRJ`/`.VCx`/`.PAN`/`.OND` 原生解析与构建”，不再在默认业务流中驱动桌面 GUI。`native/scene.py` 仅作为旧版 `"gui"` 兼容性外壳保留（未继续维护），`pvsyst_open_native_project` 则作为纯交互查看辅助工具保留。

| 领域 | 当前实现方式 | 关键技术细节与限制 |
|---|---|---|
| 工程与变体文件 | 直接读写 `.PRJ` / `.VCx` 文本 | 支持新建、克隆、归档、恢复、改标题（自动处理 UTF-8 BOM）与切换 `.SIT`/`.MET`；见[工程文件与事务](project-source-verification.md)、[项目显示标题](project-title.md) |
| 并网拓扑与容量 | 解析修改 `.VCx` 电路树 | 支持常规及独立 MPPT（`$0400`）子阵列的逆变器数、MPPT 数、并联串数、串联组件数调整，以及受限共享逆变器（`PowerSharingInverterId`）启停和子阵列克隆/删除；见[并网拓扑与参数编辑](grid-capacity-verification.md) |
| 组件库标量与资料 | 直接读写 8.1.6 / 8.1.7 明文 `.PAN`/`.OND`/`.BTR`/`.GEN` | 支持商业字段、五行备注与白名单电气标量修改（兼容 GUI 导出的无 `LargApp`/`LongApp` PAN 及 `Isc`/`muISC` 别名）；内置加密/不可读 `.PAN` 仅支持整文件引用或基于 `resistance` 模式的 DC 线缆配置 |
| OND / BTR 曲线 | 直接读写 Mode 1 点表 + CSV 导入导出 | 支持单电压 OND `Converter/ProfilPIO` 效率曲线与 AGM/Gel BTR `CapaCourant` 容量曲线的 4..256 点表修改；修改点数时自动清空旧 `a/b/c` 三次样条缓存系数，交由 PVsyst 运行时重编；见[曲线表编辑](curve-table-verification.md) |
| 固定 / 季节 / 跟踪 / 棚架 / Dome | 修改或克隆 `.VCx` 朝向块 | 各朝向类型只改所属字段与标志位，不支持跨朝向类型直接转换（例如不能把固定平面直接改成跟踪器，需基于对应类型的模板变体克隆修改） |
| 详细损失与能量管理 | 修改 `.VCx` 的 `pvMainArray` / `pvSubArray` / `pvSystem` | 涵盖 $U_c$、组件质量、LID、MPP 失配、组串电压失配、积灰、IAM、老化、辅助功率、逆变器温度、功率因数（`TanPhi`）、并网点限发（`PLimGrid`）、P50/P90 与免 GUI DC 线缆（`pvsyst_create_dc_cabling_variant`，支持本地 `.PAN` 单二极管 STC 百分比互算与内置组件 `mOhm` 回退）；每次事务只改一类参数，同时同步对应的 `Flags` 默认位/使能位 |
| 气象数据 | Open-Meteo ERA5 下载 + CLI `convert-meteo` | 自动对齐当地冬季标准时（`LegalTime`，写入 `.MEF` 的 `RefTime=LegalTime` 以确保 `.MET` 内 `HLeg=True`），不填补未发布小时；见[天气输入检查](weather-input-verification.md)、[天气准备](weather-preparation.md) |
| 负荷与 AC 储能 | 编码 `pvLoad` base62 缓存 + 写入 `pvGridStorage` | 修改外部负荷 CSV 路径不会刷新 PVsyst 内部缓存，工具直接把逐时负荷编码为 `Energy/MJ` signed-base62 写入 `.VCx`，并打开根级 `pvVCalcul` bit 19 使能位；见[逐时负荷](storage-loads.md)、[AC 光储参数](grid-storage-parameters.md) |
| 3D 住宅场景与周边遮挡 | `.pvc2` 序列化 + 无 GUI 原生 `.VCx` 写入（默认 `method="native"`） | 生成符合 PVCollada 2.0 的房屋构件、屋顶 PV 区域与周边遮挡物（`building`、原生 `oeTree` 树木 `tree`、`wall`、`mesh`），支持 XML 无损回读；原生导入可直接写 `pvShdArrayTable`、`pvShdGenObject`、`pvShdElemObject`（`oeTree`）及 `module_layout`（含显式 `string_layout` 接线）；见[住宅建模](house-pv-modeling.md) |

## 暂未封装的功能点

以下桌面端功能目前尚未开放写入接口（部分支持通过 `pvsyst_get_variant_parameters` 只读查看保存状态）：
- **组件内部模型重拟合**：修改 `.PAN` 的 STC 电压电流时不会像 GUI 那样自动重写保存 `.PAN` 内的单二极管拟合系数（`RShunt`、`RSerie` 等）；不同电池化学体系（如锂电与铅酸之间）的切换、三电压 OND 曲线及非线性曲线模式也未开放。
- **复杂电气与中高压网络**：不支持从零构建 MV/HV 变压器拓扑、跨层级混合线缆或自定义变压器损耗表（目前仅支持单 MV 模板的夜间断开开关和逆变器侧均一 AC 线缆）。
- **复杂地形与非原生 3D 对象**：已支持邻栋建筑（`building`）、树木（`oeTree`）、围墙（`wall`）、自定义三角网遮挡物（`mesh`）与 `module_layout` 逐格接线；但连续地形等高线网格（terrain DEM）以及三朝向以上全自动布局仍待扩展。
- **特殊负荷与混合逆变器 DC 储能口**：暂不支持随机电网停电生成器、PV 系统随机故障生成器、家用电器分项负荷拼装、电网充电时间表（Grid Charging）以及混合逆变器的 DC 电池端口配置。
