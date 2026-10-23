# 单方案输入闭环：从声明输入到全年仿真

`pvsyst_simulate_design` 把组件解析、模板筛选、电气包络、容量规划、天气获取/准备、项目配置和全年仿真串成一次调用。
AI 提供明确输入后，流程不操作 GUI，也不从零发明一份空白物理设计。

本页记录最初53工具阶段的单方案闭环，计数不是当前发布状态；当前总览见[README](../README.md)。后续[独立源准备](design-project-preparation.md)、[来源住宅任务](sourced-house-task.md)与[声明/保存值审查](project-input-review.md)另有分批证据，旧阶段记录不自动升级为新能力。
本页只覆盖新增的单方案输入闭环；模板仿真的既有边界见 [模板驱动的全年自动仿真](automatic-simulation.md)，
容量规划见 [容量指南](capacity-sizing.md)。无合格模板时使用的标准模板种子是**内部 fallback**，不新增 MCP 工具入口。
已运行的 MCP 服务需重启才能读到新工具与 schema。

## 入口与范围

单方案输入闭环由三个新入口组成：

| 工具 | 职责 |
|---|---|
| `pvsyst_resolve_location(query, country_code)` | 只读地名解析，返回全部候选及其整数 `location_id`、坐标、ISO 国家码与 IANA 时区；`country_code` 可选，仅用于收窄候选 |
| `pvsyst_acquire_weather(...)` | 规划或执行一个 Open-Meteo ERA5 年份，产出 PVsyst 标准小时 CSV + 原始响应 + 来源清单 |
| `pvsyst_simulate_design(spec, dry_run)` | 统一 spec 的单方案全年流程：解析→包络→容量→天气→配置→仿真 |

三者都属于**严格参数入口**：未知顶级参数在进入后端前被拒绝，`spec` 及其嵌套对象使用 `extra="forbid"` 的严格模型，
布尔值与数字字符串不会被当作设计数字接受，需要整数的位置也不接受浮点数。

限定范围固定为 `scope="fixed_unshaded_grid"`：单子阵列、单普通固定平面、并网系统、`PRCorrTemp=False`、无活动阴影/依赖表/Module Layout。
不做方案比较、不做储能/离网、不做多朝向、不做 3D 场景。`scope` 不接受其他取值，也不会从其他输入推断屋顶或地面安装属性。

### 新增的两个入口

以下为最初计划55工具阶段的接入说明，不代表当前待验收状态；后续验收与边界以[天气工程审核与来源绑定](weather-engineering-review.md)和当前总览为准：

- `pvsyst_review_weather`（站点与天气组）：只读三层天气审查——provider→CSV（同转换器重建）、CSV→原生逐时数值与标签、`.MET`/`.SIT` 头部**声明层**字段；不写文件、不做哈希、不跑仿真、不读许可，也不证明原生运行中有效的太阳位置时间偏移。
- `pvsyst_prepare_sourced_design`（仿真组）：用调用者声明的源文本 + 精确数值/单位 token + 引文装配 `electrical_design` 草案，缺项以草案/冲突返回，再复用既有包络；身份标签由所属对象组合、调用者不可覆盖。

当时记录：`pvsyst_prepare_sourced_design` 通过 SDK 验收（120 passed / 0 failed，exit 0；真实 Jinko/Solis 案例返回 `blocked`、`missing=13`），天气审查在该时点仍待验收。后续结果不反写为当时已通过。
**未 reload 的运行宿主仍提供其旧工具集合**，以当前会话实际schema为准，不按本文历史计数推断。边界与来源缺项见 [天气工程审核与来源绑定](weather-engineering-review.md)。

## 统一 spec

`pvsyst_simulate_design` 的 `spec` 字段：

| 字段 | 规则 |
|---|---|
| `new_project` | 必填；新的工作 PRJ 文件名，整个流程只写入这个新前缀 |
| `year` | 必填；整数 1969–2068（两位年份原生标签契约），仿真与该年天气一致 |
| `target_dc_kw` | 必填；有限正数 DC 目标；除显式声明外不推断任何容量 |
| `tilt` / `azimuth` | 必填；`tilt` 0–90 度，`azimuth` −180…180 度，零为南、西为正（PVsyst 约定） |
| `module` / `inverter` | 必填；`manufacturer`+`model` 精确对，或一个显式 `file_name` |
| `template_project` / `variant` | 可选；省略时自动扫描工作区 PRJ 及其变体 |
| `weather` | 必填；`mode` 取 `existing` / `csv` / `open_meteo` |
| `capacity` | 与 `electrical_design` **二选一且必须恰好给一个**：直接给设计包络与限值 |
| `electrical_design` | 厂家数据绑定后的线性包络请求；见下节 |
| `losses` | 可选；只接受 `LIDLoss`、`PMismMPP`、`ModuleQualityLoss`（子阵列）与 `UFactor`（整个主阵列） |
| `scope` | 固定 `fixed_unshaded_grid` |

`capacity` 与 `electrical_design` 同时出现、或两者都缺，都在预检阶段返回 `blocked`，不会退化成"用模板原有数量跑一次"。
`capacity.target_dc_kw` 与 `electrical_design.target_dc_kw` 若出现，必须等于 `spec.target_dc_kw`：统一入口只注入一个目标，不调和两个互相矛盾的声明。

未提供的字段**保留模板保存值**，并在返回的 `configuration_summary` 中披露实际请求的角度与损失；
没有任何损失、系数、温度或方位角会被拟合、推断或填默认值。

## 一份可调用的 spec 示例

下面的 JSON 可直接作为 `pvsyst_simulate_design` 的 `spec` 实参。**示例中的电气数值是用户已确认的设计数据，
不是厂家实测参数**；`location_id` 与文件名同样是占位值，实际必须来自 `pvsyst_resolve_location` 的候选与工作区现有文件。

```json
{
  "spec": {
    "new_project": "NEWDESIGN.PRJ",
    "year": 2024,
    "target_dc_kw": 12.0,
    "tilt": 28,
    "azimuth": 0,
    "scope": "fixed_unshaded_grid",
    "module": { "manufacturer": "EXAMPLE MFR", "model": "EXAMPLE-600" },
    "inverter": { "manufacturer": "EXAMPLE MFR", "model": "EXAMPLE-10K" },
    "template_project": "TEMPLATE.PRJ",
    "variant": "VC0",
    "weather": {
      "mode": "open_meteo",
      "country_code": "CN",
      "location_id": 123456,
      "site_label": "EXAMPLE PLACE",
      "timezone_name": "Asia/Shanghai",
      "temperature_method": "endpoint_mean"
    },
    "electrical_design": {
      "temperatures": {
        "min_ambient_c": -20,
        "min_ambient_basis": "user-confirmed site extreme design minimum (IEC 62548-1 basis), not a single weather-file minimum",
        "min_module_operating_c": -5,
        "max_module_operating_c": 70
      },
      "envelope": { "cold_voc_v": 45.2, "cold_vmp_v": 37.5, "hot_vmp_v": 30.1 },
      "coefficients": {
        "beta_voc": { "value": -0.27, "unit": "pct_per_degC", "source": "user-confirmed design data" },
        "beta_vmp": { "value": -0.30, "unit": "pct_per_degC", "source": "user-confirmed design data" },
        "alpha_isc": { "value": 0.05, "unit": "pct_per_degC", "source": "user-confirmed design data" },
        "alpha_imp": { "value": 0.04, "unit": "pct_per_degC", "source": "user-confirmed design data" }
      },
      "mppt_limits": {
        "max_operating_current_a": { "value": 26, "source": "user-confirmed design data" },
        "max_short_circuit_current_a": { "value": 30, "source": "user-confirmed design data" }
      },
      "effective_irradiance": {
        "max_irradiance_w_m2": 1100,
        "rear_gain_factor": 1.0,
        "source": "user-confirmed design irradiance envelope"
      },
      "design_basis_extra": "user-confirmed design basis; not a manufacturer measurement"
    },
    "losses": { "LIDLoss": 2.0, "UFactor": 29.0 }
  },
  "dry_run": true
}
```

`module_limits` 省略、电流由系数计算时，`effective_irradiance` 是必需输入（见"电气来源规则"）。

## 组件解析规则

- 只按**精确 Manufacturer+Model 大小写不敏感相等**匹配工作区与已安装库中的散文件，或使用一个显式 `file_name`。
- 工作区散文件优先；被同名的已安装库匹配只作为阴影警告，不替换身份。
- 在胜出的库内出现两个及以上可用同身份文件时报告**歧义并阻塞**，要求显式文件名，绝不猜替身。
- 不模糊匹配、不近似替换；**加密内置组件库不可枚举**：工作区散文件中找不到的引用报告为不可用，而不是"缺失"。
- 选中的 PAN/OND 必须能完整读取并通过组件静态检查；随后才附加所属对象的 `electrical` 声明字段。

### 与 `electrical_design` 绑定

包络请求的 `manufacturer_model` 若给出，必须等于实际选中的 `"PAN 厂家 型号 / OND 厂家 型号"` 标签，否则停止。
该标签是**来源绑定**，保证包络描述的就是本次仿真使用的组件，不是厂家认证或重新拟合。

## 模板筛选与排序

模板必须是工作区中**通过完整结构检查**的 PRJ/VC 成员：单子阵列、单普通固定平面、Grid、根头部显式 `PRCorrTemp=False`、无活动几何/布局/阴影。

- 给定 `capacity` 时，每个候选都必须返回 `ready` 的现有容量规划结果，否则该候选记为不合格。
- 候选排序完全公开：**先按与模板逆变器数量的差异最小，再按参数步数最少，再按容量误差最小，最后按项目/变体名的稳定字典序**。
- 显式 `template_project` 的读取/结构错误直接成为 `issues`；自动扫描时不可用项目记为被拒候选，不会因为一个坏项目而放弃其他合格模板。
- **没有任何合格模板时，返回每个候选的具体缺项**（结构、PRCorrTemp、几何、容量不可规划等），不从零发明一份 PRJ。

### 无合格模板时的内部标准种子

当工作区没有任何合格模板、但 PAN 与 OND 都已精确解析、且**调用者没有指定 `template_project`/`variant`** 时，流程回退到**已安装的标准模板**：
以新项目名派生 `<stem>_template.PRJ` 与固定 `SEED_VARIANT`，按已接受的容量改写为源模板，再复制成工作 PRJ/VC。

- 种子模板**只读**：预览与执行前都校验原文件未改动，旧版本或不匹配的种子不会被替换使用。
- 这是**内部 fallback**，不是新的 MCP 工具（不改变已发布工具计数），也不需要用户先手工建项目或手选模板。
- **显式给出 `template_project`/`variant` 时不会触发 fallback**：指定模板不可用就按缺项返回，不会被种子顶替。
- 被拒候选仍逐条出现在 `warnings` 中，它们的问题永远不会被当作种子成功的理由。
- `dry_run=true` 的种子规划是纯内存的，不写文件、不创建 `Projects` 目录；名称冲突在种子预览前就阻塞。
- **工作区前提（本机 CLI 8.1.6 已证，不外推其他版本）**：根目录名必须是 `PVsyst8.1_Data`（**根从不重命名、移动或替换**），规范布局为 **17 个根子目录 + `ComposPV` 内 9 个 = 26 个**。这个 26 目录超集是真实 bootstrap 证明过的集合，**不是**从静态目录链裁剪出的"最小集合"；流程另用只读预检报告缺失目录与文件冲突，不据此改名或覆盖。
- **在任何 provider 请求之前**先做只读工作区预检：报告缺失目录、嵌套 `ComposPV` 的目录链接逃逸与同名文件冲突；出现问题即停止，不改名、不覆盖、不向外写。
- 种子派生保留 `Version=8.1.0/8.1.6` 声明与 UTF-8 BOM（`project_bom_preserved` / `variant_bom_preserved` 分别报告）。既有 Parent `.BatchParams` 等历史引用保持**未改动**，单次运行通过；这**不证明**该文件或批参数被加载或消费，batch 加载未验证。
- **原生状态**：在本机 CLI 8.1.6 的规范根名与布局下，bootstrap 87 与最终实证的 bootstrap 88 都已原生通过（见下节），其中包含"执行时自动补齐目录"的最终实现。

## 电气来源规则

`electrical_design` 只接受声明值，每个值可以是严格数字，或 `{value, unit, source}` 声明对象：

- **不读取、也不假设** PAN 中序列化温度系数的单位。`beta_voc`/`beta_vmp`/`alpha_isc`/`alpha_imp` 必须同时给出 `unit` 与 `source`，否则报具体缺项。
- 电压包络取两条路径之一：厂家直接给出的 `envelope.cold_voc_v`/`cold_vmp_v`/`hot_vmp_v`，或带显式单位与来源的系数，用公开线性关系计算并回传公式与来源。
- **`beta_pmax ≠ beta_vmp`**：只有 Pmax 温度系数而没有 Vmp 系数时，明确拒绝，不用 Pmax 系数顶替。
- **`IDCMax` 是整机电流上限，不是逐 MPPT 限值**：逐 MPPT 工作/短路电流必须来自逆变器数据表；缺失就报错，不拿整机字段替代、不除以 MPPT 数。
- 系数化电流需要**显式 `max_irradiance_w_m2` 与 `rear_gain_factor` 及其 `source`**；缺失即 `blocked`，不默认按 STC 辐照度、也不默认增益为 1（单面组件要显式写 `1.0`）。两个模块电流限值都直接给出时，多给的辐照输入会收到"已声明但未被使用"的警告。
- **单位必须匹配量纲**：显式写出 `mA`、`kV` 这类非标度单位会被拒绝；`A`/`V`，或键本身已含单位而只给数值或 `{value, source}`，都可以接受。
- `GRef` 不是 1000 W/m² 的模板组件只给出警告，不作静默换算。
- 现场极端设计最低温必须是**声明的设计极值**（`min_ambient_basis` 必填，IEC 62548-1 用历史最低温度），**不接受单年气象文件最低值替代**；`annual_min_ambient_c` 若等于设计极值会给出明确警告。
- **组件工作温度不是环境温度**：`min_module_operating_c` 等于 `min_ambient_c` 时给出警告，要求 `−100 ≤ min 环境 ≤ min 组件工作 ≤ max 组件工作 ≤ 150 degC`。
- 空对象永远不会变成 `ready`；每个缺失值都返回字段级 `issues`。

## 天气三种模式

| `weather.mode` | 需要的输入 | 行为 |
|---|---|---|
| `existing` | 显式 `sit_name` + `met_name` | 使用工作区现有 SIT/MET；不重新获取天气。未提供清单时跳过原生源天气对照并只做名称核对 |
| `csv` | `csv_name` + `time_basis="local_standard_interval_start"` | 把已暂存的标准小时 CSV 经既有准备流程转成新 SIT/MET 与来源清单 |
| `open_meteo` | `location_id`，或完整显式坐标（`latitude`+`longitude`+`site_label`+`country_code`+`timezone_name`） | 先解析天气计划，再按需下载一个 ERA5 年份并准备 SIT/MET |

**国家码规则**：用 `location_id` 时不必填——ISO 国家码直接取自地名候选结果（调用者若同时给出且与候选冲突则报错）；准备 SIT/MET 时若仍未提供，会取该地点的 ISO 码并给出明示警告。用显式坐标时**必须**给出 `latitude`、`longitude`、`site_label`、`altitude`、`country_code` 与 `timezone_name`，都不从 provider 推断。`location_id` 与显式坐标**混用被阻塞**。

### 自然语言地名的处理

AI 先用 `pvsyst_resolve_location(query, country_code)` 取候选，然后**必须由 AI 明确挑一个整数 `location_id`**：

- 返回全部候选（含 `location_id`、坐标、ISO 国家码、IANA 时区、行政名）与 `candidate_count`。
- 多于一个候选时 `ambiguous=true`、`selection_required=true`，并给出"不要取首位候选"的警告。
- **只有恰好一个候选时 `selection_required` 才为 false**；不会静默采用第一个命中，也不会从一个命中推断地点。
- 零候选时要求补充国家或行政限定，或改用显式坐标。
- 该入口只读：不下载、不写文件。

### Open-Meteo ERA5 获取细节

- 固定两个公开主机（`geocoding-api.open-meteo.com`、`archive-api.open-meteo.com`），只请求 GHI（`shortwave_radiation`）、
  DHI（`diffuse_radiation`）与温标 `temperature_2m`，显式要求 W/m² 与摄氏度；请求风（默认 km/h）不在范围内。
- 年份整数 1940–2068，且**请求年份必须已被 provider 完全发布**（ERA5 约滞后五天）；未发布时间返回 `null` 时**不填补、不清零、不插值**，而是阻塞。
- 原始响应标签为 GMT。辐射量是 **preceding hour mean**，标签标记区间的 UTC **END**；温度是标签 UTC 小时的**瞬时值**，不是观测的小时均值。
- 产出 CSV 的时间基准固定为 `local_standard_interval_start`：先用 `utcoffset − dst` 求出该年**每日采样的固定冬季标准偏移**，
  再按当地标准时 00:00–23:00 的小时区间起点打标签；**不做 UTC/DST 平移**。
- 边界裁剪：请求区间为当地标准年首 00:00 减偏移、到年尾 24:00 减偏移，于是会**包含相邻 UTC 日期**，
  并覆盖该年**含闰日的全部小时**（平年 8760、闰年 8784）。相邻 UTC 日期只为取得边界小时，CSV 只写该年的小时行。
- 一行需要三个读数（辐射取区间终点标签、温度取区间两端两个瞬时点），任何一个缺失/非有限都记为缺失。

### 明示近似与披露

- `temperature_method="endpoint_mean"` 是**唯一**已核验的方法：每小时 Tamb 取该当地标准小时两个端点 UTC 瞬时值的**非加权平均**，
  作为**线性近似**在 `warnings` 与 `review_required` 中明示，并**永久保持复核状态**；它不是观测小时均值。
- provider 回显的坐标是 **ERA5 0.25° 网格中心**、高程是 DEM 值：两者作为 `provider_echo` 证据记录，
  **站点元数据保留请求的地点坐标**，网格偏差在 `warnings` 中披露。
- 未提供 `altitude` 时复用 provider 网格高程回显并标注为 DEM 值，不当作实测场址高程。
- 产出三个**新**文件：`Meteo/<name>.csv`、`Meteo/<name>_openmeteo_raw.json`（原始 provider 响应）、
  `Results/<name>_acquisition.json`（来源清单）。任一目标已存在即拒绝覆盖。**失败或阻塞同样保留原始响应与清单**，便于复现原因。

### 时区与 Windows 依赖

- IANA 时区名必须能解析；**Windows 的 CPython 不自带 IANA 数据**，因此需要 `tzdata` 包（已在 `requirements.txt` 中按 `sys_platform == 'win32'` 声明）。
  这是**本机 Python 依赖，不是 PVsyst 原生能力变更**，也不影响 CLI 行为。
- 时区数据库缺失时给出明确安装提示，而不是把地名强行映射到别的偏移。
- 标准偏移按已安装 CLI 档案的 **−12…+12 整小时**处理；**半小时偏移立即阻塞**（需要 UTC 小时重分箱，本流程不做）。
- 该年内标准偏移发生变化（历史偏移变更/夏令制）同样阻塞，不静默重分箱；实际 create-site 前还会显式拦截大于 +12 h 的地点。
- **负夏令时（negative DST）约定阻塞**：任一采样日的 `dst < 0` 即返回阻塞并列出前几个例子，因为 `utcoffset − dst` 给出的是法律标准偏移（如 Dublin 冬季为 +1 h），而当地冬季墙钟偏移是 0 h，模块无法判断"当地冬季标准时"指哪一种约定。不猜偏移、不取最小值替代：`Africa/Casablanca` 在斋月也用负 DST，因此负值并不总是冬季信号。
  `Europe/Berlin`、`Australia/Sydney`、`Asia/Shanghai`、`Pacific/Honolulu` 等常规时区回归正常。

## 容量、损失与角度

- 容量规划沿用既有 planner：在完整独立均匀 MPPT 模板内规划整数串联/并联/逆变器数量，并校核声明电压、电流、温度与 DC:AC 上限。
- `capacity` 与 `electrical_design` 都只提供"平面输入"；流程**不自动选型、不重建拓扑、不拟合损失**。
- `losses` 只接受四个参数：`LIDLoss`、`PMismMPP`、`ModuleQualityLoss`（子阵列作用域）与 `UFactor`（整个主阵列）；
  其它键在预检阶段被判为不支持的损失参数。**不按屋顶/地面安装猜测任何损失默认值**。
- 方位角按 PVsyst 约定（零南、西正）写入唯一固定平面；底层模板入口返回的 `configuration_summary` 披露实际请求的角度与损失。

## 状态机与 dry-run 语义

`status` 取值为 `blocked` / `ready_for_weather` / `ready` / `completed` / `needs_review` / `failed`，并附带 `stage`、
`dry_run`、`execution_preflight_complete`、`issues`、`warnings`、`selected`、`preview`、`metrics`、`artifacts`、`recovery`。

- `dry_run=true` 不写任何文件、不消耗仿真执行次数。天气获取即使在线也**只返回请求计划**；
  此时**还没有实际 CSV**，因此状态是 **`ready_for_weather`**、`execution_preflight_complete=false`，
  并且 `warnings` 明确说明"尚无 SIT/MET，全年仿真只是被计划、未预检"。**这不是 ready**，AI 不应把它当成可运行方案或已完成的预检。
- `execution_preflight_complete=true` 只在真正走到仿真预检后才置位。
- `dry_run=false` 由流程协调既有 `pvsyst_acquire_weather` / `pvsyst_prepare_weather` / `pvsyst_simulate_template` 三个阶段；
  每阶段只接受自己的显式成功状态，任何其它状态都停止流程。
- 返回 `artifacts`（天气获取/准备/仿真各自产物）与 `recovery`（逆序恢复步骤）；
  provider 派生的天气来源永远不是原生认证，保留的复核项会让最终状态成为 `needs_review`。
- `needs_review` 表示"全年输出已产生但有保留的保守告警或来源近似"，不是正常设计交付。

## 失败保持与恢复

- 原模板从始至终只读；实际改动只发生在新的工作副本与新建的输出文件上。
- 不自动清理、不覆盖、不回滚：失败时保留已产生的天气产物、工作项目、原始响应与阶段清单，`recovery` 只**记录**恢复步骤而不执行。
- 天气获取本身不编辑 PRJ/VC；其失败保留已创建的天气文件与清单，不套用项目恢复工具。

## 明确不做

不做方案比较、不做储能/离网、不做多朝向、不做 3D 场景；不做自动硬件选型、任意拓扑重设计、全工况电压/电流工程认证、
组件内部拟合或能量/经济最优计算；不做 native MET 转换认证与辐照计质量认证；不枚举加密内置组件库；不编辑旧二进制 PAN。

当前公开方案**不依赖任何 DLL 或二进制反向**：新模块只使用标准库访问两个固定公开端点，并用自己的受限校验器检查 provider 载荷。
若将来确实需要逆向，才另行交付给指定 agent，且不以此作为本流程的前提。

## 已可声明的证据

- 真实 Open-Meteo ERA5 下载：**北京 2024 年（闰年 8784 行）与 2025 年（8760 行）**均成功产出标准小时 CSV、原始 provider 响应与来源清单。
- 新增离线测试本轮全部通过，分布为：纯设计包络 29、组件发现 10、provider 获取 32、标准种子 22、统一流程 42（合计 135，无 skip），
  分别位于 `tests/test_design_envelope.py`、`tests/test_design_inputs.py`、`tests/test_weather_acquisition.py`、`tests/test_standard_template.py`、`tests/test_design_flow.py`。
- 真实 SDK 生命周期：**67 passed / 0 failed**；本机 syntax、pycompile 与 git diff check 通过。
- SDK 89 只读正向预检通过：`electrical_design` 用 fixture 来源与 V/A 声明得到 flat capacity，`manufacturer_model` 精确身份绑定命中既有 seed 与已生成的天气，`status=ready`、`execution_preflight_complete=true`；角度 30 / −15 与 `LIDLoss 1`、`PMismMPP 2.5`、`ModuleQualityLoss −0.3`、`UFactor 29` 的请求值全部保留，`Projects` 成员列表不变。这是**预检**通过，不是新的原生年度执行，也不构成厂家或工程包络认证。
- 旧 189 项离线回归属于 PR#22 的历史记录，本轮未运行，也未修改其结论。

## 真实原生记录

**native 81：失败并保留记录。** 走 `pvsyst_prepare_weather` 的原生 SIT 在经度上按 4 位小数序列化：`116.39723 → 116.3972`，
该次运行按失败保留，未改写源数据，也未收缩判据。

**native 82：坐标序列化精度已探明。** 原生文件保存 `Lat 39.907567 → 39.9076`、`Long 116.397234 → 116.3972`、`Alt 47.75 → 48`。
据此 `check_site` 明示判据为**坐标 ±0.0000501 deg、高程 ±0.500001 m**（另有时区 1e-6 与月值打印精度），超差反例仍然 `blocked`。
这条边界描述的是**原生序列化精度**，不是"坐标位置完全相等"。

**native 83：`dry_run=false` 全流程通过（`needs_review`）。** 一次真实 MCP 调用完成
在线获取 ERA5 北京 2024 → 新 SIT/MET → 自动选合格模板与精确 PAN/OND → 21.6 kW 声明包络容量规划 → 原生全年 8784 行：

| 项 | 结果 |
|---|---|
| status / stage | `needs_review` / `completed`，`execution_preflight_complete=true` |
| 全年行数与区间 | 8784 行，2024-01-01 00:00 → 2024-12-31 23:00，步长 60 min |
| E_Grid | 36945.2864 kWh |
| 标准 PR | 90.610745%（`E_Grid/(GlobInc×铭牌 DC)`，不是小时 PR 均值） |
| IL_Pmax / GlobInc | 73.2493 kWh / 1887.6679 kWh/m² |
| 组件与模板 | `Public_JKM600N_78HL4_BDV.PAN`、`Generic_9kW.OND`；模板 `MCP_CAPACITY67_CONTROL_A.PRJ/VC0` |
| 规划结果 | 18 串联 × 2 串、2 台逆变器、36 模块，DC 21.6 kW / AC 18 kW，DC:AC 1.2 |
| 保留复核 | `endpoint_mean` 近似；原生转换提示导入天气相对晴空模型偏移 15 分钟，天气准备保持 `needs_review` |

**证据边界：** 该次容量使用的是**明确的 integration fixture 设计值与实验用 `Generic_9kW` OND**，
不是北京气候设计极值，也不是厂家或工程认证（证据文件的 `scope` 与 `design_basis` 都如此声明）。
报告为 `Results/MCP_DESIGN83_annual_2024.csv` / `.pdf`，证据 JSON 位于
`C:\Harness\workspace\component-edit-verification\gui-roundtrip-7cd385dab330\single-design83-evidence.json`。
子代理曾误执行含哈希校验的新测试及全量测试；相关新增哈希用例已删除，这些执行不计本轮验收，后续按非哈希白名单验证（weather-review 52、source 23、上一阶段 135、SDK 120）。偏差记录：全量套件实际执行 6 次、新增哈希探针约 9 次，均不计验收、后续不再发生。产品既有冲突保护与已有哈希测试保留，但不再运行。

**native 85 / 86：失败并保留现场。** 两次都从一个只含 PAN/OND 的新 Root 出发，流程已 `create` 出源 template 与工作 PRJ/VC，并生成了 2024 年 SIT/MET；原生运行**都在加载工作区阶段以 `LoadWorkspace` 失败**。
85 既缺目录结构、根名也不符；86 已补齐 26 个标准目录但根名仍不符，同样失败。**根名与布局必须同时正确**。
失败现场完整保留：`single-design85-evidence.json`、全部 source、`Projects`、天气文件与恢复清单都未被清理或回滚。

**native 87：空工程 bootstrap 通过。** 使用规范最终目录名 `PVsyst8.1_Data` + 17 个根子目录 + `ComposPV` 下 9 个，且**只有两个组件、没有 PRJ/SIT/MET**：
`dry_run` 返回 `ready_for_weather`；执行派生官方种子、建立工作工程并获取新的 ERA5 天气，2024 年 8784 行原生 `completed`/`needs_review`，
`E_Grid` 36945.2864 kWh、标准 PR 90.610745%（与 83 同值；这里只是分别观察两份指标，不追加比较或哈希类测试）。
85/86/87 互为对照：缺结构与错误根名都失败，只有根名与布局联合正确时才通过；
**不能**把"25 目录静态链"当作最小已证集合，也不能把此前与 basename 无关的逻辑外推。

**native 88：最终实证（自动补齐空目录）。** 与 87 相同的规范根名与布局，但**初始只有 PAN/OND 以及 `ComposPV`/`PVmodules`/`Inverters` 三个组件目录，不预建其他标准目录**：
`dry_run` 预检只报告缺失、**不创建** `Projects`（preview 中 `Projects` 仍在 missing 列表）；
执行阶段 `workspace_init` 创建 **20 个**新目录——天气阶段此前已建立 `Meteo`/`Sites`/`Results`，初始即存在的三个组件目录不计入——
因此准确表述是**最终 26 个标准目录被自动补齐**，而不是"init 一次创建 26 个"。
同一次 `execution` 走完 8784 行：`status=needs_review`、`stage=completed`、`execution_preflight_complete=true`，
`E_Grid` 36945.2864 kWh、PR 90.61074499969226%；26 个目录实存、PDF 620743 字节且以 `%PDF-` 开头、source 与工作 PRJ 四个文件都在。
首次运行脚本 `exit 1` 是主代理把 87 批量替换成 88 时把 `8784` 误改成 `8884` 触发的断言错误，**不是产品故障**；
修正脚本断言后改为**只读原始 CSV/PDF 重放**（没有原生重跑，也没有哈希类测试），逐时仍是 2024-01-01 00:00 → 12-31 23:00 连续 8784 点。
证据：`C:\Harness\workspace\component-edit-verification\gui-roundtrip-7cd385dab330\single-design88-evidence.json`。

**时间基准限定（适用于 83、87、88 全部数值）：** 这些指标只对**当时的 `SolarTime` 输入**成立。后续实测发现 MET 内嵌 `HLeg` 会**override** SIT 声明（同 project/date，仅改 MET `HLeg`，`HSol`/`AzSol` 即变化），旧闭环因此按**太阳时**解释 legal 标签——**逐时数字对齐不等于时间模型正确**，`E_Grid 36945.2864 kWh`/PR 也不是"真实法定时间已正确"的证据。

已实证的时基修复结果：92 把 public MEF `RefTimeS → LegalTime` 后确认 `HLeg=True`、`timeshift=0`，全年 3×8784 小时 token 无变化，Jan 29 17h 由旧 88 的 0（源 CSV 7）变为 7.0003。**93 已用同一 88 源 CSV（`mode=csv`）生成新 SIT/MET/工作 PRJ 并完成全年**：`LegalTime`、time fields 0、`HLeg=True`、新生成的 guard 有效，`execution_status=completed`，2024 年 8784 行、`E_Grid` **36930.3633 kWh**、PR **90.60707390969796%**；7 个旧低辐照 outlier 全部恢复为 5.0002/6.0002/7.0003（源 5/6/7），GHI/DHI 最大绝对差 0.002 W/m²、温度最大差 0.0001 °C、超阈项 0；15 分钟晴空警告为空是修正 `RefTimeS` 后的自然结果，**不是 suppress**。**`endpoint_mean` 并未因此变成真实小时平均**（source-aware review 仍为 `needs_review`）。83/87/88 保留为 history 数字，但其 legacy `SolarTime` 结果不作为正确的 local legal 工程结果；**当前示例改用 93**。详见 [天气工程审核与来源绑定](weather-engineering-review.md)。

## 待验收

- 85/86 的历史 failure（结构缺失与错误根名）与"根目录名必须是 `PVsyst8.1_Data`"的要求仍然成立：根从不重命名、移动或替换，已存在文件不被覆盖。
- 只保留更广范围未验证：更多 MPPT 数/硬件拓扑、其他 PVsyst 版本与区域、其他场景类型；根名与 26 目录布局只对本机 CLI 8.1.6 已证。
- 后续验收只针对输入校验、时间基准与单位、模型阶段状态与原生年度能量；不新增哈希类测试，也不拆除已有的产品级冲突保护。
- 其余待验证项：状态迁移仍落在 `needs_review`（保留的 `endpoint_mean` 与原生天气差异复核），以及更多年份、地点与气候覆盖。
- 本页记录的都是**实测范围**，任何一次运行都不构成厂家或工程认证。
