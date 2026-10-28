# GUI 与 MCP 能力对照及逆向笔记

全部公开工具的调用签名见[工具参考](tool-reference.md)，各功能模块的参数范围和底层字段见[功能入口地图](native-entry-map.md)。本文档记录哪些功能直接通过明文工程文件读写实现、哪些通过 CLI/GUI 桥接实现，以及还有哪些 PVsyst 桌面端能力尚未封装。

## 高层工程任务主线

除了单点改参工具外，仓库提供了一套串联“输入审查 → 隔离改参仿真 → 多方案比选 → 交付打包”的工程级工作流（详见[工程修订与交付](engineering-delivery.md)）：
1. **固定倾角 / `UnlimTracking` 工程修订**：基于现有工程克隆独立副本，修改倾角、方位角、详细损失或跟踪器参数，跑完全年仿真并生成中文交接说明。
2. **集中式 AC 光储对比评估**：绑定全年逐时负荷与储能充放电参数，对比不同储能配置下的自用电量、弃光与 SOC 表现（见[光储工程评估](engineering-storage.md)）。
3. **双坡屋顶住宅 3D 场景交付**：生成单栋双坡屋顶 `.pvc2` 场景，通过 GUI 桥完成导入、逐表朝向关联、坐标归位与阴影表计算，再交由 CLI 跑全年仿真并打包交付（见[双屋面交付说明](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)）。

## 各领域实现方式与限制

在对接 PVsyst 时，我们优先采用“官方 CLI 参数 + 明文文件解析/写入”的方式；只有当某个字段涉及未公开的内部联动重算（比如 DC 线缆阻值与百分比互算）或必须触发桌面端三维建模器（比如导入 `.pvc2` 场景并生成内部阴影缓存）时，才使用针对中文版 PVsyst 8.1.6 rev49278（96 DPI）的受控 GUI 自动化桥接。

| 领域 | 当前实现方式 | 关键技术细节与限制 |
|---|---|---|
| 工程与变体文件 | 直接读写 `.PRJ` / `.VCx` 文本 | 支持新建、克隆、归档、恢复、改标题（自动处理 UTF-8 BOM）与切换 `.SIT`/`.MET`；见[工程文件与事务](project-source-verification.md)、[项目显示标题](project-title.md) |
| 并网拓扑与容量 | 解析修改 `.VCx` 电路树 | 支持常规及独立 MPPT（`$0400`）子阵列的逆变器数、MPPT 数、并联串数、串联组件数调整，以及受限共享逆变器（`PowerSharingInverterId`）启停和子阵列克隆/删除；见[并网拓扑与参数编辑](grid-capacity-verification.md) |
| 组件库标量与资料 | 直接读写 8.1.6 明文 `.PAN`/`.OND`/`.BTR`/`.GEN` | 支持商业字段、五行备注与白名单电气标量修改（兼容 GUI 导出的无 `LargApp`/`LongApp` PAN 及 `Isc`/`muISC` 别名）；旧版二进制加密 `.PAN` 仅支持整文件拷贝 |
| OND / BTR 曲线 | 直接读写 Mode 1 点表 + CSV 导入导出 | 支持单电压 OND `Converter/ProfilPIO` 效率曲线与 AGM/Gel BTR `CapaCourant` 容量曲线的 4..256 点表修改；修改点数时自动清空旧 `a/b/c` 三次样条缓存系数，交由 PVsyst 运行时重编；见[曲线表编辑](curve-table-verification.md) |
| 固定 / 季节 / 跟踪 / 棚架 / Dome | 修改或克隆 `.VCx` 朝向块 | 各朝向类型只改所属字段与标志位，不支持跨朝向类型直接转换（例如不能把固定平面直接改成跟踪器，需基于对应类型的模板变体克隆修改） |
| 详细损失与能量管理 | 修改 `.VCx` 的 `pvMainArray` / `pvSubArray` / `pvSystem` | 涵盖 $U_c$、组件质量、LID、MPP 失配、组串电压失配、积灰、IAM、老化、辅助功率、逆变器温度、功率因数（`TanPhi`）、并网点限发（`PLimGrid`）与 P50/P90；每次事务只改一类参数，同时同步对应的 `Flags` 默认位/使能位 |
| 气象数据 | Open-Meteo ERA5 下载 + CLI `convert-meteo` | 自动对齐当地冬季标准时（`LegalTime`，写入 `.MEF` 的 `RefTime=LegalTime` 以确保 `.MET` 内 `HLeg=True`），不填补未发布小时；见[天气输入检查](weather-input-verification.md)、[天气准备](weather-preparation.md) |
| 负荷与 AC 储能 | 编码 `pvLoad` base62 缓存 + 写入 `pvGridStorage` | 修改外部负荷 CSV 路径不会刷新 PVsyst 内部缓存，工具直接把逐时负荷编码为 `Energy/MJ` signed-base62 写入 `.VCx`，并打开根级 `pvVCalcul` bit 19 使能位；见[逐时负荷](storage-loads.md)、[AC 光储参数](grid-storage-parameters.md) |
| 3D 住宅场景 | `.pvc2` 序列化 + 8.1.6 GUI 导入桥 | 生成符合 PVCollada 2.0 的房屋构件与光伏板网格，亦可从 XML 无损回读；GUI 桥支持 1..2 个 PV 分区的导入、朝向绑定与另存新变体；见[住宅建模](house-pv-modeling.md) |

## 暂未封装的功能点

以下桌面端功能目前尚未开放写入接口（部分支持通过 `pvsyst_get_variant_parameters` 只读查看保存状态）：
- **组件内部模型重拟合**：修改 `.PAN` 的 STC 电压电流时不会像 GUI 那样自动重算单二极管模型参数（`RShunt`、`RSerie` 等）；不同电池化学体系（如锂电与铅酸之间）的切换、三电压 OND 曲线及非线性曲线模式也未开放。
- **复杂电气与中高压网络**：不支持从零构建 MV/HV 变压器拓扑、跨层级混合线缆或自定义变压器损耗表（目前仅支持单 MV 模板的夜间断开开关和逆变器侧均一 AC 线缆）。
- **复杂 3D 阴影与 Module Layout**：暂不支持在 `.pvc2` 中生成周边树木、邻栋建筑或地形网格，也未开放 Module Layout 详细组件排布与电气阴影表（Electrical Shadings）编辑。
- **特殊负荷与混合逆变器 DC 储能口**：暂不支持随机电网停电生成器、PV 系统随机故障生成器、家用电器分项负荷拼装、电网充电时间表（Grid Charging）以及混合逆变器的 DC 电池端口配置。
