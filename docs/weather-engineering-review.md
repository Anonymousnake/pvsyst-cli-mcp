# 天气工程审核与来源绑定（pending）

本页记录**尚未验收**的两个新增入口，以及天气量化与厂家来源的当前实测边界。
这里的结论都是"待验收/未决"，不是已通过声明；计数口径等主代理的 SDK 记录下发后再更新。

## 两个新入口

| 工具 | 组 | 职责 | 状态 |
|---|---|---|---|
| `pvsyst_review_weather` | 站点与天气 | 只读比对一份已准备 CSV 与一份原生结果：provider→CSV、CSV→原生逐时数值与标签、`.MET`/`.SIT` 头部**声明层**字段 | 已实现；weather final agent 仍在 working，**SDK 验收 pending** |
| `pvsyst_prepare_sourced_design` | 仿真 | 用调用者声明的源文本 + 精确数值/单位 token + 引文，装配 `electrical_design` 草案并复用既有包络 | 已实现并**已 SDK 验收**（120 passed / 0 failed，exit 0） |

工具总数目标为 **55**（现有 53 + 上述 2 个），已在真实 MCP 会话（pwsh 362）中确认为 55 工具 / 6 组。两个入口都在严格参数名单内：未知参数被拒，`year` 之类的位置只接受严格整数。**尚未 reload 的运行宿主仍提供旧集合**，不要假定当前实例已是 55。

### `pvsyst_review_weather` 的边界

- **不做哈希、不写文件、不跑仿真、不读许可**：CSV 用无哈希的值检查器重新检查，历史清单结论不作为依据。
- `status` 为 `blocked` / `needs_review` / `completed`；`completed` 只表示**没有剩余的数值层复核项**，不是物理认证。
- `provider_to_csv` 用的是与获取模块**同一个转换器**重建原始载荷：这是**同转换器一致性**检查，不是独立算法证明。
- `time_basis` 报告原生 `.MET` 头部与 `.SIT` 文件**声明了什么**。逐时数值与标签逐行一致**不能**证明原生运行中有效的太阳位置时间偏移为零：该残差没有任何公开字段暴露。
- 不认证晴空模型、不认证 ERA5 源质量、不认证气候极值。

### `pvsyst_prepare_sourced_design` 的边界

- 组件按**精确明文文件名**（工作区优先，再查已安装库）；只有厂家名的搜索不会被替换，模型也不会被推断；身份标签由所属对象组合，调用者不能覆盖。
- 每个声明都绑定一个 `source_id`、一段引文，以及与声明值一致的**精确数值 token 与单位 token**；引文必须原样出现在声明的源文本中，单位经显式字面白名单映射，**不做重标定、不做单位推断**。
- 它是**源一致性检查器**，不是 PDF/表格读取器：不抓取、不解析 PDF、不做哈希、不调用 SDK；**声明文本原样不回传**。
- 缺失项返回**草案 + 缺项 + 冲突**供 AI 补齐，不抛异常、不猜值；装配出的 `electrical_design` 交给既有包络流程。
- 缺失/重复的身份、旧二进制或非 UTF-8 组件返回 `blocked` 与具体字段。

## 天气量化：当前实测

三层审查的读法：

1. **provider → CSV**：同转换器重建后逐点比对。
2. **CSV → 原生逐时**：数值与时间标签逐行比对。
3. **声明层**：只读 `.MET` 根字段/`SiteMet` 快照与 `.SIT` 根字段。

88 次实测（`weather-time-basis-88/time-basis88-verification.json`，另有 `time-basis88-offset-diagnostics.json`）：

| 项 | 结果 |
|---|---|
| 三列映射 | raw → 标准 CSV 8784 行，GHI/DHI/Tamb 映射正确 |
| 时间标签 | 原生小时标签与准备 CSV **完整匹配** |
| GlobHor（低辐照 5..20 带） | max 7 W/m2，RMSE 0.171，年度 delta −0.0418 kWh/m2（−0.0027%） |
| DHI | max 6，RMSE 0.131，年度 delta 约 −0.032 |
| Tamb | max 1e-4 degC |

**不要把这些差值直接归因于原生 float32 或截断算法**。binary 侧已确认：88 的 `.MET` 容器是**纯 UTF-8 文本**，使用 base62 签名 token 与 `DDMM`/`WRFactor` 一类字段，**不是 binary float block**；7 个低辐照 outlier 的 5–7 值**确实存在于 MET 中**，而旧 88 原生导出为 0，因此差异发生在 **simulation/SFI 阶段，而不在 import**；93 用正确 `LegalTime` 重跑后这 7 个小时全部恢复（见上节），与"import 丢值"的解释不符。

## 时间基准：新发现与当前状态

实测到的问题（同一 project、SIT 88 声明 `HLeg=True`、6 月 21 日）：

- 只把**新 MET 的 `HLeg` 从 False 改为 True**，两次 run 的 `HSol`/`AzSol` 就出现差异：**MET 内嵌 flag 生效并 override SIT 声明**。
- 因此旧 88 是按**太阳时**解释 legal 标签的：**hourly 数字逐行对齐不能证明时间模型正确**。这里被区分的是「时间字段的声明」与「effective 的太阳几何」两件事。
- MEF `RefTime=SolarTime` 确实**不应**配 LocalLegal CSV。
- native 侧 False 与 True 的画面差异**不是 RTti 判断**。

当前状态（93 已通过）：同一 88 源 CSV 以 `mode=csv` 生成新 93 的 SIT/MET/工作 PRJ 与全年 CSV/PDF；`LegalTime`、time fields 0、`HLeg=True`、新生成的 guard 有效；`execution_status=completed` / `stage=completed`，2024 年 **8784 行**，`E_Grid` **36930.3633 kWh**、PR **90.60707390969796%**（与旧 88 的 36945.2864 kWh / 90.610745% 是两个不同时基输入的结果）。7 个旧低辐照 outlier 小时**全部恢复**为 native 5.0002/6.0002/7.0003（源值 5/6/7），GHI/DHI 最大绝对差 0.002 W/m²、温度最大差 0.0001 °C、超阈项 0。15 分钟晴空转换警告现在为空，**是修正 `RefTimeS` 后原生自然不再提示，不是代码 suppress 或删除警告**。verify93 只复用 88 源 CSV、不重新下载、不改旧 88；93 脚本最初的 `exit 1` 是脚本自身硬要求 needs_review（csv 模式没有 acquisition 继承）所致——实际 execution 为 `completed`，而 source-aware review 为 `needs_review`；修正脚本后只读真实 93 CSV/PDF 重放，**没有原生重跑**。**`endpoint_mean` 并未变成真实小时平均**：source-aware review 读入 88 acquisition + 93 weather manifest + 93 CSV，得到 `provider.matches=True`、`context_match=True`、`timebasis_conflicts=[]`，但 `review_status=needs_review`（温度近似仍在，且该接口没有 runtime geometry 证据，仍需 supplied diagnostics）。

三条不做推论的边界：15 分钟 warning 的**成因仍未独立证明**，不用数值接近 15 分钟去反推成因；把 `RefTimeS` 改正确**不等于**做了自动 weather 相位移校准；旧闭环的数值验收（含 83/87/88 的 `needs_review`）仍然只是**数值层**验收，**time basis 从未闭合**——现在发现并保留可回溯。

## 未决项

- `.SIT` 声明 `HLeg=True` 与 `.MET` 内嵌 `SiteMet.HLeg=False` 的口径冲突已由 92/93 处理：public MEF 切 `LegalTime` 后 `HLeg=True`、time fields 0，93 全年通过。
- `HourShift`/`TimeShift`/`TimeShiftTotalMin=0` 只是**声明层**，不证明太阳位置；runtime geometry 证据仍需 supplied diagnostics。
- 93 中 15 分钟晴空转换警告为**空**（修正 `RefTimeS` 后原生自然不再提示，非代码 suppress），残留复核为温度近似；**不自作平移**。
- 用户已明确授权原生小时数据逆向并已委派指定 route（`met-binary-time-semantics[cline-pass/cline-pass/deepseek-v4.1-flash]`）：**不再把"不解析 binary"当作永久限制**。当前公开审查只读头部，逆向结果尚未定论。

## 来源缺项清单

厂家与现场资料的现状（`manufacturer-evidence/manufacturer-source-verification.md` + 3 份 PDF 已实查）：

- 本轮**未取得** JKM600…/Solis 原厂文件；这**不等于**世界上没有该文件，只表示本轮没有拿到可引用的原件。
- 本地 Jinko PAN 文本与第三方论文附件相同：不是原厂发布件。
- 本地 Solis OND 自声明为通用效率缩放，不是原厂效率 profile。
- 官网 F1 STC 吻合（受 Imp 打印精度限制）；Depth 30 与 35 不等，未解决。
- 序列化 `MuISC` 等 raw unit 仍**未验证**，因此不能宣布混版结论。
- Solis 60 A（global）与 45 A（AU）短路电流差异是**地区/版本**不同，不自动选更大的那个。
- "4 MPPT 8 strings"**不等于**每个 input 有 20 A 硬上限。
- 仍缺项：`beta_vmp`、`alpha_imp`、现场设计极值、`E_max`。**不用实验值或单年天气文件填这些空**。
- `partial-source-inputs.json` 只是两个厂家系数 quote + 真实来源 page 2 与 page 13 的 missing 记录，**不是 ready 输入**；它已被用作一次真实后端验证的文本来源（见下节）。

用户教程口径（`C:\工作\学习\微网\PV8.1.7教程`）：

- weather 部分：pdf 23/24 用 interval start，27 讲晴空质量。
- components 3/8：可以用数据库表 clone **自建** PAN/OND（明确是自建，原厂文件并非唯一路径）。
- components 6/7：Model 的 Voc 温度系数**不是**厂商指定值，且只用于 sizing；12 讲 MPPT。
- grid 20：电压约束的默认 60/20/−10 **不当作**现场真实输入。

### 教程旁注（仅用于解释模型范围）

以下三点是主代理已核实的**事实旁注**，用来说明模型可用范围，**不是**要放宽或偷偷改用户组件：

- components p11：没有完整效率曲线时，官方原生会自动按 Max/Euro/CEC 效率建立 profile，用户自定义 OND 属正常路径。缺原厂 curve 只会让**出处与拟合假设**需要说明，**不一定**硬性阻断用户建立模型。
- components p9 的"Max PV current sometimes ISC of array, not used"指的是特定 GUI 总 ISC 字段，**不能**推广为"所有 MPPT `IDCMax` 在本机版本都不参与仿真"——原生仍有 `IL_Imax` 导出。
- NOCT：不设该参数是教案口径；实际是否需要要看原生型号模型，**不当作** source-input 的 site 温度默认值。

来源二进制解析仍在进行中；主代理本轮没有新的原生运行。

## 真实 source 案例（已 SDK 验收）

用 `manufacturer-evidence/partial-source-inputs.json` 的真实 PDF 第 2 页文本，加实际 Jinko PAN / Solis OND 走实际 MCP 入口（pwsh 362；无哈希、不改写文件）：
真实 Jinko + Solis 文件身份解析成功，官方 PDF 的两条引用均 `reference_match=true`（2/2：`betaVoc −0.25 %/℃`、`alphaIsc 0.046 %/℃`），
并生成含两个 `pct_per_degC` 系数的 `electrical_design`；结果 `status=blocked`、`missing=13`，**不是完整的厂家工程包**，
现场温度、`E_max`、`betaVmp`、operating Imp、per-MPPT 等**不推断取值**。
SDK 侧 120 passed / 0 failed（exit 0）；source 侧当前 23 项离线测试直供实际入口（**snapshot**，仍在补数字表达式截取边界两例；含真实 PDF 中数字紧贴单位 token 与 `℃` 别名的用例）。

证据范围（功能解释）：

- **页号是调用者的声明，不是页面坐标自检**：声明里不带 `page` 时，工具不会去核实该值是否真的出现在那一页。
- `kind=manufacturer` **不认证 PDF 本身的真实性**，也不代表厂商已发布该结论。

这两个入口只承载公开函数，**没有**引入框架级的新原生 MET decode；二进制解析仍是独立产物，**不代表生产二进制支持已交付**。
`pvsyst_review_weather` 的合同验收已由 93 的 source-aware review 覆盖（读入 88 acquisition + 93 weather manifest + 93 CSV，得到 `provider.matches=True`、`context_match=True`、`timebasis_conflicts=[]`）；其 `review_status=needs_review` 只反映仍在的 `endpoint_mean` 近似与该接口没有 runtime geometry 证据，不是未验收。

binary 侧对 88 的 `.MET` 容器判定已完成（纯 UTF-8 文本 + base62 签名 token，非 binary float block），HLeg 太阳语义由 92 实证；**低辐照残差不被认证为 rounding/截断的结果**——93 在正确时基下已把它恢复。

## 结论

源码侧 55 工具 / 6 组已是目标实现，**不再有 pending 的 weather runtime 88/93 审查接口**；宿主仍需 reload（待 merge 后）。当前示例用 **93**（`LegalTime`、`E_Grid` 36930.3633 kWh、PR 90.60707390969796%、8784 行）；83/87/88 保留为 history 数字，但其 legacy `SolarTime` 结果**不作为正确的 local legal 工程结果**。接入公开来源绑定与天气量化之后，工程用例仍然 **blocked：缺资料**（源侧两条 quote 匹配、`missing=13`），当前不宣称任何"真实厂家参数的认证年仿真"；本页记录的是实测范围与来源缺口，不是工程或厂家认证。验证按**非哈希白名单**进行（weather review 52、source 23、上一阶段 135、SDK 120）。
