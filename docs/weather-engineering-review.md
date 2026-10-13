# 天气工程审核与来源绑定

本页记录两个新增入口的已验收边界、天气量化的实测结果与厂家来源的缺项。
它们已随 **55 工具 / 6 组**（main `16edffe`）合并；宿主重载后即可用，重载前仍提供旧集合。

## 两个新入口

| 工具 | 组 | 职责 | 状态 |
|---|---|---|---|
| `pvsyst_review_weather` | 站点与天气 | 只读比对一份已准备 CSV 与一份原生结果：provider→CSV、CSV→原生逐时数值与标签、`.MET`/`.SIT` 头部**声明层**字段 | 已实现；93 次实测已 verified |
| `pvsyst_prepare_sourced_design` | 仿真 | 用调用者声明的源文本 + 精确数值/单位 token + 引文，装配 `electrical_design` 草案并复用既有包络 | 已实现；SDK 120 passed / 0 failed（exit 0） |

两者都在严格参数名单内：未知参数被拒，`year` 之类的位置只接受严格整数。

### `pvsyst_review_weather` 的边界

- **不做哈希、不写文件、不跑仿真、不读许可**：CSV 用无哈希的值检查器重新检查，历史清单结论不作为依据。
- `status` 为 `blocked` / `needs_review` / `completed`；`completed` 只表示**没有剩余的数值层复核项**，不是物理认证。
- `provider_to_csv` 用的是与获取模块**同一个转换器**重建原始载荷：这是**同转换器一致性**检查，不是独立算法证明。
- `time_basis` 报告原生 `.MET` 头部与 `.SIT` 文件**声明了什么**。逐时数值与标签逐行一致**不能**证明原生运行中有效的太阳位置：**本接口只读证据、不建立有效 sun position**，这是接口范围声明，不是对其它证据途径的全局否定。
- 不认证晴空模型、不认证 ERA5 源质量、不认证气候极值。

### `pvsyst_prepare_sourced_design` 的边界

- 组件按**精确明文文件名**（工作区优先，再查已安装库）；只有厂家名的搜索不会被替换，模型也不会被推断；身份标签由所属对象组合，调用者不能覆盖。
- 每个声明都绑定一个 `source_id`、一段引文，以及与声明值一致的**精确数值 token 与单位 token**；引文必须原样出现在声明的源文本中，单位经显式字面白名单映射，**不做重标定、不做单位推断**。
- 它是**源一致性检查器**，不是 PDF/表格读取器：不抓取、不解析 PDF、不做哈希；**声明文本原样不回传**。
- 缺失项返回**草案 + 缺项 + 冲突**供 AI 补齐，不抛异常、不猜值；装配出的 `electrical_design` 交给既有包络流程。
- 缺失/重复的身份、旧二进制或非 UTF-8 组件返回 `blocked` 与具体字段。

## 天气量化：93 实测

三层审查的读法：**provider → CSV**（同转换器重建后逐点比对）、**CSV → 原生逐时**（数值与时间标签逐行比对）、**声明层**（只读 `.MET` 根字段/`SiteMet` 快照与 `.SIT` 根字段）。

| 项 | 结果 |
|---|---|
| 三列映射 | raw → 标准 CSV 8784 行，GHI/DHI/Tamb 映射正确 |
| 时间标签 | 原生小时标签与准备 CSV 完整匹配 |
| 7 个低辐照 outlier | 旧 88 下 native 为 0（源值 5/6/7），正确时基下全部恢复为 5.0002 / 6.0002 / 7.0003 |
| GHI / DHI 最大绝对差 | 0.002 W/m2 |
| Tamb 最大绝对差 / 超阈项 | 0.0001 degC / 0 |

这些差值**不归因于原生 float32 或截断算法**。独立报告已确认 88 的 `.MET` 容器是**纯 UTF-8 文本**，使用 base62 签名 token 与 `DDMM`/`WRFactor` 一类字段，**不是 binary float block**：7 个低辐照值确实存在于 MET 内，差异发生在 simulation/SFI 阶段而非 import，93 在正确时基下已把它们恢复。

## 时间基准：93 结果

以下五点即当前结论（对应 `mode=csv` 的全流程）：

- **93 数字**：2024 年 8784 行，`E_Grid` 36930.3633 kWh、PR 90.60707390969796%（旧 88 的 36945.2864 kWh / 90.610745% 是另一个时基输入的结果）。
- **执行状态**：`mode=csv` 复用 88 源 CSV 生成新 SIT/MET/工作 PRJ 与全年 CSV/PDF，`execution_status=completed`、`stage=completed`。
- **source-aware review**：读入 88 acquisition + 93 weather manifest + 93 CSV，得到 `provider.matches=True`、`context_match=True`、`timebasis_conflicts=[]`，但 `review_status=needs_review`——`endpoint_mean` 仍是温度近似，**没有变成真实小时平均**。
- **7 个 outlier 已恢复**：见上表，与"import 丢值"的解释不符。
- **声明已正确、无相位平移**：`LegalTime`、time fields 0、`HLeg=True`、生成的 guard 有效；93 中 15 分钟晴空转换警告为空，是修正 `RefTimeS` 后原生自然不再提示，**不是代码 suppress 或删除警告**，也不存在自动 weather 相位移校准。

## 时基 guard

复用已准备的 manifest 时，工具会**重新校验 MET 的时基**：旧 `SolarTime` 时基的 MET 会被 `blocked`，必须用新文件重建，**不 rewrite 旧文件**。该约束针对 **manifest 绑定的 local-standard-time 导入 profile**，不声称所有既有 MET 的普通 run 都被阻断。

## 来源缺项清单

厂家与现场资料的现状（`manufacturer-evidence/manufacturer-source-verification.md` + 3 份 PDF 已实查）：

- 本轮**未取得** JKM600…/Solis 原厂文件，只表示没有拿到可引用的原厂发布链，**不等于**世界上没有该文件。
- 本地 Jinko PAN 文本与第三方论文附件相同：**不证明**它并非原厂来源，只是本机文件来源未获认证。
- 本地 Solis OND 自声明为通用效率缩放，不是原厂效率 profile。
- 官网 F1 STC 吻合（受 Imp 打印精度限制）；Depth 30 与 35 不等，未解决。
- 序列化 `MuISC` 等 raw unit 仍**未验证**，因此不能宣布混版结论。
- Solis 60 A（global）与 45 A（AU）短路电流差异是**地区/版本**不同，不自动选更大的那个。
- "4 MPPT 8 strings"**不等于**每个 input 有 20 A 硬上限。
- 仍缺项：`beta_vmp`、`alpha_imp`、现场设计极值、`E_max`。**不用实验值或单年天气文件填这些空**。
- `partial-source-inputs.json` 是两个厂家系数 quote 加 **13 个缺项**记录（不是"第 13 页"），**不是 ready 输入**；它已被用作一次真实 source 案例的文本来源（见下节）。

用户教程口径（`C:\工作\学习\微网\PV8.1.7教程`）：

- weather 部分：pdf 23/24 用 interval start，27 讲晴空质量。
- components 3/8：可以用数据库表 clone **自建** PAN/OND（明确是自建，原厂文件并非唯一路径）。
- components 6/7：Model 的 Voc 温度系数**不是**厂商指定值，且只用于 sizing；12 讲 MPPT。
- grid 20：电压约束的默认 60/20/−10 **不当作**现场真实输入。
- components p11：没有完整效率曲线时，官方原生会自动按 Max/Euro/CEC 效率建立 profile，用户自定义 OND 属正常路径；缺原厂 curve 只会让**出处与拟合假设**需要说明，**不一定**硬性阻断建模。
- components p9 的"Max PV current sometimes ISC of array, not used"指特定 GUI 总 ISC 字段，**不能**推广为"所有 MPPT `IDCMax` 在本机版本都不参与仿真"——原生仍有 `IL_Imax` 导出。
- NOCT：不设该参数是教案口径；实际是否需要看原生型号模型，**不当作** source-input 的 site 温度默认值。

## 真实 source 案例

用 `manufacturer-evidence/partial-source-inputs.json` 的真实 PDF 第 2 页文本，加实际 Jinko PAN / Solis OND 走实际 MCP 入口：
身份解析成功，官方 PDF 两条引用均 `reference_match=true`（2/2：`betaVoc −0.25 %/℃`、`alphaIsc 0.046 %/℃`），
并生成含两个 `pct_per_degC` 系数的 `electrical_design`；结果 `status=blocked`、`missing=13`，**不是完整的厂家工程包**，
现场温度、`E_max`、`betaVmp`、operating Imp、per-MPPT 等**不推断取值**。

证据范围（功能解释）：**页号是调用者的声明、不是页面坐标自检**；`kind=manufacturer` **不认证 PDF 本身的真实性**，也不代表厂商已发布该结论。

## 结论

55 工具 / 6 组已合并，当前示例用 **93**；83/87/88 保留为 history 数字，其 legacy `SolarTime` 结果**不作为正确的 local legal 工程结果**。
接入公开来源绑定与天气量化之后，工程用例仍 **blocked：缺资料**（源侧两条 quote 匹配、`missing=13`），不宣称任何"真实厂家参数的认证年仿真"。
验证按**非哈希白名单**进行：weather review 52、source 23、上一阶段 135、SDK 120。
