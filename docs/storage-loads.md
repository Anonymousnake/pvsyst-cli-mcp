# 用户负荷配置：常数/月度/日曲线与全年逐时 CSV 绑定

在 PVsyst 8.1.6 中，给并网系统配置用户用电负荷（User's Needs）有两条路径：
1. **原生简明负荷（`pvsyst_create_native_load_variant`）**：适用于无电池的纯光伏自发自用场景，直接写入常数功率（`kW`）、12 个月电量（`kWh`）或 24 小时日负荷曲线（`kW`）。
2. **全年逐时负荷 CSV（`pvsyst_prepare_load_profile` + `pvsyst_create_load_variant`）**：校验 8760/8784 小时负荷 CSV，并将其编码为 `.VCx` 内部 `pvLoad` 的 `Energy/MJ` signed-base62 逐日缓存（适用于纯光伏自用或配合 [AC 光储系统](grid-storage-parameters.md)）。

## 1. 常数、月度与 24 小时日曲线负荷（`pvsyst_create_native_load_variant`）

在 PVsyst 8.1.6 单子阵列、单固定平面（朝向 1）、自由地平线、无 3D 阴影且无储能的纯光伏并网变体上，创建一个开启自发自用的新 `.VCx` 变体：

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "load": {
    "mode": "constant",
    "constant_kw": 75.125
  },
  "variant_name": "Constant Load",
  "dry_run": true
}
```

三种 `load.mode` 的入参与 `.VCx` 内部字段对照（数值步长精度均为 `0.001`，单项上限 `1e6`）：

| `mode` | 入参字段与单位 | `.VCx` 中 `pvLoad` 写入的字段 |
|---|---|---|
| `constant` | `constant_kw`（正数，单位 `kW`） | `LoadType=Constant`, `Flags=$00`, `ConstantLoad=<kW>` |
| `monthly` | `monthly_energy_kwh`（12 个月的月用电量列表，**单位为 `kWh`**，至少 1 个月大于 0） | `LoadType=Monthly`, `Flags=$01`, `UsesModul=Yearly`, 15 槽 `ValMonth`（前 12 槽为月电量 + 3 个尾零槽） |
| `daily` | `daily_power_kw`（24 个整点小时的功率列表，**单位为 `kW`**，至少 1 小时大于 0） | `LoadType=DailyProfile`, `Flags=$02`, `NHourlyValues=1`, 25 槽 `ValHour_1`（24 小时功率 + 第 25 槽日总和） |

**两个底层踩坑点**：
- 在 `Monthly` 模式下，虽然 `.VCx` 里的 `Energy Units` 字段字面上写着 `kW`，但 `ValMonth` 前 12 项实际保存的是每个月的**总千瓦时（kWh）**，不是 12 个功率值！
- 启用用户负荷时，除了写入 `pvLoad` 块外，还必须同时置位变体根部 `pvVCalcul.Flags` 的 **bit 19**（`0x80000`），否则 PVsyst 跑仿真时不会开启自发自用计算。

## 2. 准备全年逐时负荷 CSV（`pvsyst_prepare_load_profile`）

把完整的 8760（平年）或 8784（闰年）小时负荷 CSV 放到工作区 `UserData` 目录下。文件需为 UTF-8 编码，包含日期列、负荷列及紧随其后的单位行（分隔符支持分号或逗号，单位支持 `W`、`kW`、`[W]`、`[kW]`，数值非负）：

```text
Date;P Load
;kW
2024-01-01 00:00;2.5
2024-01-01 01:00;2.5
```

调用 `pvsyst_prepare_load_profile` 验证全年整点时间轴（`00:00..23:00`）、无夏令时跳变，并在 `UserHourlyParams` 下生成 PVsyst 原生格式（ASCII、CRLF、欧洲点分日期 `DD.MM.YYYY HH:MM`）的 CSV 及 `Results` 下的 v1 负荷清单：

```json
{
  "csv_name": "source_load_2024.csv",
  "year": 2024,
  "output_name": "load_2024.csv",
  "utc_offset_hours": 8,
  "time_basis": "local_standard_interval_start",
  "date_format": "iso",
  "dry_run": true
}
```

（`date_format` 支持 `"iso"` 即 `YYYY-MM-DD HH:MM`、`"pvsyst_dot"` 即 `DD.MM.YYYY HH:MM`、或 `"pvsyst_slash"` 即 `DD/MM/YYYY HH:MM`。）

## 3. 将逐时负荷缓存绑定到新变体（`pvsyst_create_load_variant`）

**为什么不能只改 `.VCx` 里的 CSV 文件路径？**
PVsyst 在导入逐时负荷 CSV 时，会把全年的每小时电量转换成以 `MJ` 为单位、乘以比例因子 `WRFactor` 后的 3 字符有符号 base62 编码，按天存在 `.VCx` 内部 `pvLoad` 的 `Day_DDMM`（共 366 行，含闰日 `2902`）缓存里。如果你只改了 `HourlyFile` 的文件名而不重算这 366 行 `Day_DDMM` 缓存，哪怕 CLI 日志打印了读取文件名，实际跑出来的 `E_User` 依然是旧缓存里的负荷！

调用 `pvsyst_create_load_variant(project, source_variant, new_variant, load_manifest_name, dry_run=...)` 时，工具会：
1. 重新核对 `load_manifest_name` 对应的源 CSV 与规范化 CSV 指纹。
2. 自动计算合适的 `WRFactor` 比例因子（防止大负荷溢出 3 位 base62 上限），将全年每小时负荷编码写入 `HourlyLoad`/`ParSimulH` 的 `Day_DDMM` 缓存，同时设置 `HourlyFile`、`Flags=$4000`、`UsesModul=Yearly`（关闭年总量重归一化和夏令时偏移），并生成新 `.VCx` 变体。
3. 在返回结果中给出 base62 整数步量化带来的最大理论/实际舍入误差，供仿真后与导出 CSV 的 `E_User` 列逐小时核对。如果后续修改了外部负荷 CSV，记得重新跑一次准备和绑定。
