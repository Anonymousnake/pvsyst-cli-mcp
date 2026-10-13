# 光储逐时负荷导入与原生绑定

本阶段新增 `Loads & Storage` 分组：`pvsyst_prepare_load_profile` 与
`pvsyst_create_load_variant`。这两个操作负责完整小时负荷准备和新变体绑定；
不把建立负荷表等同于已经建立混合逆变器电池端口、储能策略或电网充电条件。
原有无储能自动设计入口保持原范围，不因这两个工具而自动变为光储设计入口。

## 准备负荷

将源 CSV 放在 workspace/UserData。必须只有调用者声明的日期/负荷两列，紧跟单位行。
支持分号或逗号；功率单位仅 W/kW 及 `[W]`/`[kW]` 两个明确别名。
负荷是非负消费，不是含光伏的净购售电表。输入是 UTF-8，输出为纯 ASCII。

```text
Date;P Load
;kW
2024-01-01 00:00;2.5
2024-01-01 01:00;2.5
```

上面仅示意表头和两行。实际必须完整覆盖所选年份的 8760/8784 个小时，从
1 月 1 日 00:00 到 12 月 31 日 23:00；不补缺测、不插值、不把 15 分钟数据默默汇总。

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

`iso` 为 `YYYY-MM-DD HH:MM`；`pvsyst_dot` 为 `DD.MM.YYYY HH:MM`；
`pvsyst_slash` 为 `DD/MM/YYYY HH:MM`。时间标签为小时区间起点，声明固定标准时间
偏移且无夏令时；导入不做时间转换。年份为严格整数 1969..2068。

预览 `ready` 不建目录、不写文件；将 `dry_run` 设为 false 才创建：

- `UserHourlyParams/load_2024.csv`：原生格式 `Date;P Load`、`;kW`、欧洲点分日期、CRLF。
- `Results/load_2024_load.json`：v1 导入记录，包括源列/日期格式、实际覆盖、月/年消费量、峰值和时基声明。

原源文件保留，已有同名输出不覆盖；写入失败保留公开路径上的产物。
`prepared` 仅说明文件准备成功，`native_consumption_verified` 仍为 false。
PVsyst 的小时 CSV 日历匹配会忽略文件年份，MCP 则明确核对请求年份及闰日。

## 绑定新变体

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "load_manifest_name": "load_2024_load.json",
  "dry_run": true
}
```

工具重新读取本工作区的源 CSV 和归一化 CSV，核对完整时序、单位转换和逐点消费。
清单中的历史绝对路径仅作元数据，不据其打开外部文件；当前工作区的源 CSV 也必须可读。
预览返回候选差异；执行只创建新 VC，原 PRJ、VC、CSV、系统标志、策略和天气不变。

绑定采用**明确的内嵌小时快照**，替换所属 `pvLoad`；不是仅修改文件路径后依赖重读：

- `LoadType=HourlyFile`、`Flags=$4000`、`UsesModul=Yearly`，禁用全年归一化和 DST 偏移。
- `HourlyLoad/ParSimulH` 的 Energy/Accum 数据以 MJ 储存：`整数 = kW × 3.6 × WRFactor`。
- 有符号 base62 token 用 `Day_DDMM` 表示每一天的 24 个小时，闰年含 `Day_2902`。
- 返回实际编码误差和半个整数步长的理论界限；不存在用未知二进制格式替换字段。
- 超出已验证的三位 token 范围时调整比例，不截断负荷。浮点序列化及导出舍入独立说明。

非 ASCII 负荷路径在新 VC 添加明确披露的 UTF-8 BOM；不改变原 VC。
`created` 表示新文件已创建，不表示原生执行或物理验收。更改外部 CSV 后要重新绑定新变体；
运行必须采用与负荷时基/年份匹配的 SIT/MET，并核对原生 `E_User`。

## 实证与边界

本机 8.1.6 的公开 MCP 负荷准备、预览、新变体创建经过白名单检查。
原生输出变量已实际导出：`viE_User`、`viE_Avail`、`viEBatCh`、`viEBatDis`、
`viEFrGrid`、`viSOC_Beg`、`viSOC_End`、`viEArrMPP`、`viEDirUse`、`viCL_Chrg`、`viCL_InvB`。
SOC 导出为 ratio，不把百分数与 0..1 分数混用。

隔离的 B3 反例证明：仅改外部 CSV 路径/文件名、单位及 `IsRenormalized=False`，
日志仍可显示 `Reading load profile from file - 100%`，但原生沿用旧 BDEW 快照。
输入年消费 33285.6 kWh，实际输出 702208.297 kWh，首小时 2.5 kW 被旧 42.123 kW 替代。
B4 仅换 `P Load` 为 `Load`，结果不变。日志、CSV 存在、旧快照不回写都不足以证明消费。

B5 把完整 366 天快照明确替换为当前负荷，匹配 2024 SIT/MET 后得到 8784 行：
年消费 33285.6 kWh，逐点最大差 0，2 月 29 日特殊的 17.0..19.3 kW 全部复现。
B6 正式通过公开 MCP 的准备、预览、新变体创建再运行：8784 行、年消费 33285.6 kWh、
最大逐点差 0、闰日特殊负荷复现，原 VC 字节保留。公开负荷白名单 49 项、既有设计 SDK 兼容 120 项通过；
不会把实验脚本作为配置 API，也不把这一次负荷验收冒称端口/储能策略验收。
1990 年示例天气与 2024 年直接组合曾触发原生日期拦截，失败产物保留；
不以修改日期格式或略过闰日掩盖该失败。

原生用户负荷缓存编码及消费已有上述证据；自动读取外部文件的完整模式切换、
混合 DC 端口、电网充电调度、全部 BTR 物理模型、其他 CLI 版本、GUI 回存仍需分别验收。
`input-param-file` 是高级参数 DAT 通道，不拿它冒充负荷 CSV 导入。
商业模板、气象样本、逆向脚本与原生 CSV/PDF 留在本机独立 case，不进入仓库。
