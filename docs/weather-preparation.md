# 站点、天气与指定年份闭环

本流程把已有标准小时天气 CSV 转为新的 SIT/MET，并用来源清单连接到原生全年仿真。它不下载气候数据、不修补观测，也不解析二进制 MET。适用于已验证的模板与组件；容量设计包络仍由调用方明确提供。

## 已核实输入与边界

- 标准 CSV 的 GHI、DHI、Tamb 三列，辐照单位 W/m2 或每小时 MJ/m2，温度 deg.C/degC；日期列可重排，Year 可来自 #Year。
- 指定年份为1969..2068，源时间标签完整覆盖00:00..23:00，包括闰年2月29日。文件名不作为年份证据。
- 显式声明 `time_basis="local_standard_interval_start"`：固定当地冬季标准时，没有夏令时，每条标签表示一小时区间起点。
- 经纬度、海拔、时区、站点名、国家标签与来源来自 CSV 元数据；ISO alpha-2 大写国家代码必须明确传入，避免站点创建器自动查询外部服务。
- UTC、显式时间参考、非零时间偏移、缺测、POA 和三列以外的变量映射在首版中返回具体预检问题，不自动转换、补值或丢列。

MEF 的完整明文结构来自安装目录中的官方 `Hawaii_2020_hourly.MEF` 示例。先把已验证 CSV 序列化为单独的 ISO 日期列和统一单位，再按固定列号导入；这不是修改原 CSV 或移动其时间标签。SIT 由官方 create-site 生成，不采用近似天文计算器。

## 调用

```python
prepared = pvsyst_prepare_weather(
    csv_name="station_2020.csv", year=2020,
    sit_name="station_2020.SIT", met_name="station_2020.MET",
    country_code="US", time_basis="local_standard_interval_start",
    dry_run=True,
)
```

只读预检通过后，将同一调用的 `dry_run` 设为 `False`。流程创建新的导入 CSV、月值 CSV、MEF、SIT、MET 与 Results 中的天气来源 JSON；同名文件已存在时停止。原始 CSV 保持字节不变，执行前后校验其 SHA-256。生成 SIT 的经纬度、时区、国家与月值按实际输出精度再核对（月辐照 ±0.051 kWh/m2、月温度 ±0.0051 degC，对应打印精度）。

`prepared` 表示转换完成但尚未验证原生 MET；原生转换出现质量或时间提示时返回 `needs_review` 并保留提示。两者都不等同于气候质量认证。下一次全年仿真绑定 JSON 文件名：

```python
result = pvsyst_simulate_template(
    template_project="Template.PRJ", variant="VC0",
    new_project="station_2020_work.PRJ",
    pan_name="module.PAN", ond_name="inverter.OND",
    site_name="station_2020.SIT", met_name="station_2020.MET",
    tilt=25, year=2020,
    weather_manifest_name="station_2020_weather.json", dry_run=False,
)
```

仿真预检检查清单中的源 CSV、导入 CSV、月值 CSV、MEF、SIT 与 MET 哈希，核对清单与请求年份/文件名；输入变化时在写项目之前停止。原生运行显式加载 SIT/MET，不只更换项目的历史来源文件名。SFI 另外输出 GlobHor、DiffHor、T_Amb，与源 CSV 逐时对照；全年覆盖仍由既有年度验收器检查。

## 状态与证据

- `blocked/ready`：天气准备预检结果，默认无写入。
- `prepared/needs_review`：新天气文件已生成，响应中 `native_met_verified` 仍为 false，原生全年的验收在后续仿真完成。
- `failed`：保留已生成文件和阶段清单，原输入不自动修补或删除；选择新的输出名重试。
- 年度 `completed/needs_review/failed` 保留原有含义；转换警告及天气差异会继续传到年度状态，项目编辑仍有原有逆序恢复清单。

逐时比较报告差异的样本数、最大绝对差与有符号总差。0.1 W/m2、0.01 degC 仅是明示的舍入/转换复核阈值，不是对 MET 存储精度的断言，也不是物理有效性认证。超过阈值返回复核状态，绝不把阈值扩大到掩盖已知差异。对照中的小时标签或数量不一致则是验收失败。

已观察到：官方 2022 小时源经真实 MCP 准备与原生全年仿真得到完整 8760 行输出，温度最大绝对差约0.0005 degC；36 小时辐照对照中 GHI/DHI 的最大变化分别约20.182 与13.079 W/m2，原生同时报告相对晴空模型25分钟偏移。来源 CSV 字节保持不变，该案例返回 needs_review，而不是假称逐点一致。

另一个低辐照试验的 MET 虽然生成，原生显式加载时提示最佳晴天晴空指数过低，任务以 failed 结束，恢复情况见下文清单。因此仅有文件或转换退出成功并不足以完成验收。

## 年份排查

安装目录名为 `Hawaii_2020_60min_ClipCorr.MET` 的文件，经公开原生运行按 MM.DD 诊断后实际使用2022年；对同一个文件明确请求2020年会出现 Invalid simulation dates。该诊断用于识别输入的实际年份，不用于生产自动改年。指定年份的生产请求一直传 YYYY.MM.DD，并按输出完整年份验收。

文件名或标签写的年份与数据实际年份可能不一致，这只影响输入可信度判断，指定年份的生产请求本身不需要改年。一个明确标注的 2020 合成闰年集成输入（重复安装目录里的一天，不是历史实测气候）经真实 MCP SDK 走完 prepare→simulate：全年 8784 行，上网电量 38464.7346 kWh、标准 PR 87.07453376%，三天气列逐时最大差为 GHI 0.0051667、DHI 0.0050167 W/m2、T_Amb 0.0005 degC，全部落在复核阈值内；原生转换仍提示相对晴空模型32分钟偏移，因此年度保持 needs_review。

## 公开依据

- [PVsyst 标准天气格式](https://www.pvsyst.com/help/meteo-database/import-meteo-data/pvsyst-standard-format.html)：单位、日期列、区间起点、完整夜间/闰年记录、合成年份标记。
- [自定义天气导入](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/procedure.html)：日期模式、列映射、单位、时间基准和转换后复核。
- [时间偏移](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/time-shift.html)：当地冬季标准时、UTC/DST/区间标签的不同处理。

## 已完成与仍未验证

- 2020 闰年：明确标注的合成闰年集成输入完成 prepare→simulate 全年 8784 行；不是历史气候。
- 独立对照：2022 独立作者的 71 小时结果与自动任务的 75 CSV，在 GlobHor、DiffHor、GlobInc、EArray、E_Grid、PR、IL_Pmax 七个变量 × 8760 行上逐点差为 0、配对覆盖 1.0；该比较只覆盖双方已解析的时间戳与单位。
- 失败与恢复：低辐照输入在原生 simulation 阶段 failed，失败清单、SFI 与天气文件保留；两步逆序恢复后工作 PRJ/VC 与模板复制字节相同。
- 建站与转换诊断：2022 源复制（字节相同）的 prepared 运行返回完整建站/转换诊断，原源文件与复制件均未改动。
- 代码契约：稳定代码 189 项离线测试通过（37.866s），smoke_weather 与 smoke_components 通过；未知参数键（例如把清单写成 `weather_manifest`）由 SDK 中间件在解析前作为工具错误返回，不会被静默忽略。

仍未验证：UTC/DST 或区间结束标签的转换、GHI/DHI/Tamb 之外的列映射、二进制 MET 解析与气候/原生物理有效性认证。以上记录不是提前宣布整块完成。
