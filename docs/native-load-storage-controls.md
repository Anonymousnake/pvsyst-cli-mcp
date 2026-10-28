# PVsyst 负荷模式与储能策略的内部结构笔记

实际配置用户负荷、AC 耦合光储或削峰放电时，请直接查看对应的工具手册：[逐时与简明负荷](storage-loads.md)、[AC 光储系统与参数](grid-storage-parameters.md)及[削峰放电控制](native-peak-shaving.md)。本文档记录 PVsyst 8.1.6 `.VCx` 文件中各种负荷类型与储能模式的序列化差异和常见坑点。

## 1. 六种原生负荷类型（`LoadType`）对比

在纯光伏变体上启用用户负荷（User's Needs），必须同时存在 `pvLoad` 对象并置位根部 `pvVCalcul.Flags` 的 **bit 19**（`0x80000`）。PVsyst 的 `pvLoad` 支持 6 种 `LoadType`：

| `LoadType` | `.VCx` 内部存储结构 | 当前 MCP 支持状态 |
|---|---|---|
| `Constant` | `Flags=$00`，`ConstantLoad` 直接存常数功率（`kW`） | 已支持（`pvsyst_create_native_load_variant`） |
| `Monthly` | `Flags=$01`，`UsesModul=Yearly`，`ValMonth` 15 个槽（前 12 槽存每月的**总电量 `kWh`** + 3 个尾零槽；注意不要被文件里写的 `Energy Units=kW` 误导成传功率） | 已支持（`pvsyst_create_native_load_variant`） |
| `DailyProfile` | `Flags=$02`，`NHourlyValues=1`，`ValHour_1` 25 个槽（前 24 槽存每小时功率 `kW` + 第 25 槽存日总和） | 已支持（`pvsyst_create_native_load_variant`） |
| `HourlyFile` | `Flags=$4000`，外加内嵌在 `HourlyLoad`/`ParSimulH` 中的 366 行 `Day_DDMM` signed-base62 逐时缓存（光改 `HourlyFile` 路径不改缓存不会生效） | 已支持（`pvsyst_prepare_load_profile` + `pvsyst_create_load_variant`） |
| `HouseHold` | 嵌套家用电器数量与 24 小时使用时段位图 | 仅识别，未开放创建 |
| `Probability` | 与 `DailyProfile` 同样使用 `Flags=$02`（因此解析时必须看 `LoadType` 而不是光看 `Flags`），依赖内部分档与归一化规则 | 仅识别，未开放创建 |

## 2. 四种并网储能策略的差异

PVsyst 的并网储能（`pvGridStorage`）包含 `SelfConsumption`（自发自用）、`WeakGrid`（弱电网备电）、`PeakShaving`（削峰）和 `Power shifting` 四种模式：
- **`PeakShaving` 的特殊根标志状态**：当在 GUI 里配置 `PeakShaving`（削峰，充电策略 `PVToGridMin`）时，PVsyst 会把 `pvLoad` 用户负荷块删掉（因为削峰只看并网点上限 `PLimGrid`，不需要用户负荷曲线），但**根部 `pvVCalcul.Flags` 依然保留着 `$00080000`（bit 19）**！因此解析和修改削峰模板时，不能拿纯光伏变体“有 bit 19 就必须有 `pvLoad`”的规则去报错。
- **电网随机停电 vs PV 系统随机故障**：`WeakGrid` 下的电网停电配置保存在 `pvUnavail`（`HourlyVal=GrdUnav`），而详细损失里的 PV 系统故障停运保存在 `pvSystem` 直属的 `SystemUnavail`，两者类名相近但归属和作用完全不同。目前 `WeakGrid` 支持通过完整的 0/1 逐时 CSV 绑定停电序列（见[AC 光储系统](grid-storage-parameters.md)）。
- **AC 耦合 vs 混合逆变器 DC 电池口**：当前的储能创建与修改工具均针对集中式 AC 耦合架构（`pvGridStorage` 下配置独立的 AC 充电器与电池逆变器参数）；混合逆变器的 DC 电池端口与强制电网充电时间表暂未开放。
