# 集中式 AC 耦合光储系统构建与参数配置

本模块提供两个工具来管理 PVsyst 8.1.0 / 8.1.6 并网光储（Grid Storage）变体：
- `pvsyst_create_grid_storage_variant`：在**已经激活储能**的变体上修改 SOC 阈值、AC 充放电功率上限、变流器效率或防逆流开关，并另存为新 `.VCx`。
- `pvsyst_create_grid_storage_system`：在**纯光伏并网（PV-only Grid）**变体上挂载电池包（`.BTR`）、串并联数量、12 个月电池温度、初始 SOW、储能控制策略（`SelfConsumption` 或 `WeakGrid`）以及已准备好的全年逐时负荷缓存，一步生成完整的 AC 耦合光储新 `.VCx`。

## 1. 修改现有光储系统的标量参数（`pvsyst_create_grid_storage_variant`）

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "updates": {
    "strategy": "self_consumption",
    "soc_min": 0.2,
    "soc_max": 0.8,
    "initial_soc": 0.4,
    "max_charge_ac_kw": 1,
    "max_discharge_ac_kw": 1
  },
  "dry_run": true
}
```

支持的更新字段与 `.VCx` 中 `pvGridStorage` 内部字段对照：

| 入参字段 | `.VCx` 字段 | 取值要求与物理含义 |
|---|---|---|
| `soc_min` / `soc_max` | `SOCDischDecl` / `SOCChDecl` | `0..1` 比例（需满足 `soc_min < soc_max`），代表放电硬下限与充电上限 |
| `initial_soc` | `BattSoCInit` | `0..1` 比例（需落在 `[soc_min, soc_max]` 之间），仿真初始 SOC |
| `reserve_soc` | `SOCDischSelf` | `0..1` 比例（需落在 `[soc_min, soc_max]` 之间），`WeakGrid` 模式下电网正常时的自用保留 SOC 下限 |
| `max_charge_ac_kw` | `PMaxCharge` | 正数（kW），充电器交流输入功率上限 |
| `max_discharge_ac_kw` | `PMaxDisch` | 正数（kW），电池逆变器交流输出功率上限 |
| `charger_max_efficiency_pct` / `charger_euro_efficiency_pct` | `BattRectMaxEffic` / `BattRectEuroEffic` | 充电整流器最大效率与欧洲效率（`%`，需满足 `0 < Euro <= Max <= 100`） |
| `battery_inverter_max_efficiency_pct` / `battery_inverter_euro_efficiency_pct` | `BattInvMaxEffic` / `BattInvEuroEffic` | 电池放电逆变器最大效率与欧洲效率（`%`，需满足 `0 < Euro <= Max <= 100`） |

关于 `strategy`（策略保持）的规则：
- 如果省略 `strategy`，工具会原样保留源变体已有的策略三元组——这意味着除了 `SelfConsumption` 和 `WeakGrid` 外，你也可以在不传 `strategy` 的情况下直接修改现有 `PeakShaving` / `PVToGridMin` 削峰变体的 SOC、充放电功率和效率标量（改完后仍可继续用 [`pvsyst_create_peak_shaving_variant`](native-peak-shaving.md) 调整放电时间窗）。
- 如果源变体是 `WeakGrid`（或显式传了 `strategy="weak_grid"`），每次调用都必须同时传入完整的电网可用性 `availability` 声明。

## 2. 从纯光伏变体新建 AC 光储系统（`pvsyst_create_grid_storage_system`）

先用 [`pvsyst_prepare_load_profile`](storage-loads.md) 准备好全年逐时负荷清单（`load_manifest_name`），然后传入电池硬件参数（`battery`）与储能控制参数（`storage`）：

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "load_manifest_name": "load_2024_load.json",
  "battery": {
    "file_name": "declared.BTR",
    "series_count": 4,
    "parallel_count": 4,
    "monthly_temperature_c": [20, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20],
    "cycle_sow": 1,
    "static_sow": 1
  },
  "storage": {
    "strategy": "self_consumption",
    "soc_min": 0.2,
    "soc_max": 0.9,
    "initial_soc": 0.5,
    "reserve_soc": 0.2,
    "max_charge_ac_kw": 3,
    "max_discharge_ac_kw": 5,
    "charger_max_efficiency_pct": 97,
    "charger_euro_efficiency_pct": 95,
    "battery_inverter_max_efficiency_pct": 97,
    "battery_inverter_euro_efficiency_pct": 95
  },
  "dry_run": true
}
```

底层關鍵细节：在从纯光伏变体构建光储系统时，除了在 `pvSystem` 挂电池硬件和 `pvGridStorage` 块、在 `pvLoad` 写入 366 天 `Energy/MJ` base62 负荷缓存外，工具还会自动置位变体根部 `pvVCalcul.Flags` 的 **bit 19**（`0x80000`）。如果不置位根部 bit 19，PVsyst 跑仿真时虽然会算电池，但用户负荷 `E_User` 和电网购电 `EFrGrid` 不会生效！

## 3. 弱电网（`WeakGrid`）可用性 CSV 绑定与备用 SOC

当配置 `strategy="weak_grid"` 时，需在顶层传入 `availability` 参数，指向 `UserData` 下的全年整点电网可用性 CSV（分号分隔，表头 `Date;Grid Available`、单位行 `;ratio`、时间格式 `YYYY-MM-DD HH:MM`，取值为严格的 `0` 或 `1`，且全年至少包含 1 个停电小时 `0`）：

```json
{
  "csv_name": "grid_2024.csv",
  "year": 2024,
  "utc_offset_hours": 8,
  "time_basis": "local_standard_interval_start"
}
```

- 电网可用性 CSV 的年份和固定标准时 UTC 偏移必须与负荷清单完全一致。工具会将其编码为有符号 base62 的 `+0` / `+G8`（`WRFactor=1000`）写入 `pvUnavail` 块的 `Day_DDMM` 缓存中。
- **`reserve_soc`（`SOCDischSelf`）与 `soc_min`（`SOCDischDecl`）的区别**：在 `WeakGrid` 模式下，当电网正常供电（`1`）时，电池自发自用放电只会放到 `reserve_soc` 就停下，把剩余电量留作备电；一旦发生电网停电（`0`），电池则会继续向下放电直到触及硬下限 `soc_min`。

## 4. 防逆流开关（`allow_grid_export`）与能量平衡核对

- **防逆流位（`allow_grid_export`）**：两个光储工具均支持可选布尔参数 `allow_grid_export`。传 `false` 会置位 `pvSystem.Flags` 的 **bit 27**（禁止向电网送电，此时光伏富余且电池充满后的电量进入弃电 `EUnused`）；传 `true` 会清除 bit 27；不传则保持源变体原设置。
- **使用 `grid_storage` SFI 预设核对 AC 能量平衡**：
  调用 `pvsyst_build_sfi(variables="grid_storage")` 会导出完整的光储能量流列，包括 `E_User`、`E_Miss`、`EFrGrid`、`E_Grid`、`EUnused`、`EBatCh`、`EBatDis`、`CL_Chrg`（充电整流器损耗）、`CL_InvB`（放电逆变器损耗）以及 **`CL_Over`（变流器限幅损耗）**。
  注意：`EBatCh` 和 `EBatDis` 是**电池直流侧（DC）**的能量，核对交流侧充放电功率是否超限时，应使用 `EBatCh + CL_Chrg`（交流充电输入）和 `EBatDis - CL_InvB`（交流放电输出），并把 `CL_Over` 纳入全站 AC 能量平衡，这样全年的逐时 AC 平衡残差可以完全收敛在 CSV 导出舍入精度（`< 0.01 kW`）以内。
