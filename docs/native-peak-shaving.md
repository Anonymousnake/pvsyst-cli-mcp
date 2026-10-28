# 削峰储能（Peak Shaving）放电窗口配置

在并网削峰储能（`Strategy=PeakShaving`、`ChargingStrategy=PVToGridMin`）方案中，光伏阵列优先向电网送电，只有超出并网点上限 `PLimGrid` 的那部分功率才充入电池；而电池什么时候把电放出来，则由放电策略 `DischStrategy` 控制。

`pvsyst_create_peak_shaving_variant` 用于在现有的 PVsyst 8.1.6 AC 削峰模板上修改放电时序（切换为日落后放电 `AfterSunset` 或指定起止小时窗口 `MinMaxHour`），并另存为一个新的 `.VCx` 变体。

如果需要修改该削峰模板的 SOC 上下限、充放电功率或变流器效率，可配合使用 [`pvsyst_create_grid_storage_variant`](grid-storage-parameters.md)（不传 `strategy` 参数即可保留原有的 `PeakShaving` 策略）。

```json
{
  "project": "example.PRJ",
  "source_variant": "VCL",
  "new_variant": "VCN",
  "discharge": {
    "mode": "hour_window",
    "start_hour": 18,
    "end_hour": 8
  },
  "variant_name": "Peak Window 18-08",
  "dry_run": true
}
```

## 两种放电时序模式（`discharge.mode`）

| `mode` | 入参要求 | `.VCx` 中 `pvGridStorage` 写入的字段 |
|---|---|---|
| `after_sunset` | 仅传 `{"mode": "after_sunset"}` | 写入 `DischStrategy=AfterSunset`，并移除 `HourBegDisch` 和 `HourEndDisch` 字段（此时 `pvGridStorage` 共 14 个字段） |
| `hour_window` | 传 `start_hour` 与 `end_hour`（`0..23` 的严格整数，且起止小时不能相等；支持同日窗口如 `8..20` 或跨夜窗口如 `18..8`） | 写入 `DischStrategy=MinMaxHour`、`HourBegDisch=<start_hour>`、`HourEndDisch=<end_hour>`（此时 `pvGridStorage` 共 16 个字段） |

**注意 `AfterSunset` 的原生行为**：在 PVsyst 内部，选 `AfterSunset` 并不等于“只有当太阳辐照为 0 的夜间小时才允许放电”，PVsyst 会根据自身的削峰与日落判据联动控制充放电与 SOC，解读结果时以导出的逐时列为准。

## 适用模板条件与结果核对

源变体需为 PVsyst 8.1.6 的集中式 AC 削峰模板：
- 根部 `pvVCalcul.Flags=$00080000` 且不含 `pvLoad` 对象（这是 PVsyst 保存削峰变体时的原生状态）。
- `pvSystem` 为并网系统（`Flags=$17000040`、`PLimGrid=200`、`LimGrSeparLoss=False`，挂载 `Universal_Adjustable_LI.BTR` 15 串 10 并、12 个月 20 °C 电池温度）。
- `pvGridStorage` 的 `Flags=$00`、`Strategy=PeakShaving`、`ChargingStrategy=PVToGridMin`。
- 单子阵列、单固定平面（朝向 1），无 3D 阴影场景、远方地平线或 P50。

查看削峰仿真结果时，注意该模板下 `pvSystem.Flags=$17000040` 将并网限值 `PLimGrid=200` 定义为**视在功率（Apparent power, kVA）**（尽管 CSV 里 `PGridLim` 列头仍标为 `kW`）；使用 `pvsyst_build_sfi(variables="grid_storage")` 导出完整列（含 `CL_Chrg`、`CL_InvB`、`CL_Over`、`EUnused`）即可完整闭合全年的 AC 能量平衡。
