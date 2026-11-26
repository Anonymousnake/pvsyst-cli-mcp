# PVsyst 工程工作流与开发指南

本文档整合了单方案设计、规格书组件合成、气象制备、3D 住宅建模、AC 耦合光储、多方案对比、简报预审、工程交付以及测试发版的完整操作指南。完整工具列表见[工具参考](tool-reference.md)，底层支持边界见[功能入口地图](native-entry-map.md)。

---

## 1. 单方案设计输入与全年流程

`pvsyst_simulate_design(spec, dry_run=True)` 是面向单方案设计（固定倾角、地面棚架/跟踪器及离网系统）的最高层一站式入口。它把组件查找、零模板合成或模板筛选、串并联整数容量规划、天气下载/转换以及全年仿真打包在一个统一的 `spec` 里执行。如果只想把工程文件配好落盘、暂不跑全年仿真（例如后面还要导 3D 房屋场景），可以把完全相同的 `spec` 传给 [`pvsyst_prepare_design_project`](tool-reference.md)（注意：`scope="stand_alone"` 离网光储柴仅支持 `pvsyst_simulate_design`，`pvsyst_prepare_design_project` 会直接 `blocked` 拦截）。

### `spec` 顶层字段

| 字段名 | 说明与要求 |
|---|---|
| `new_project` | 新生成的 `.PRJ` 工程文件名（连同派生的天气、清单和结果文件均不能与现有文件重名） |
| `year` | 仿真年份（整数 `1969..2068`），需与所选天气数据覆盖年份一致 |
| `target_dc_kw` | 目标直流装机容量（kWp，正数） |
| `tilt` / `azimuth` | 固定倾角（`0..90°`）与方位角（`-180..180°`，遵循 PVsyst 惯例：正南为 `0°`，偏西为正，偏东为负） |
| `module` / `inverter` | 组件与逆变器标识：可传精确的 `{"manufacturer": "...", "model": "..."}` 或直接传工作区文件名 `{"file_name": "..."}` |
| `template_project` / `variant` | 可选；指定作为底板的模板工程与变体，不传则自动扫描工作区择优或使用内置标准种子 |
| `weather` | 气象数据模式：`existing`（已有 `.SIT`/`.MET`）、`synthetic_from_site`（基于已有 `.SIT` 原生合成逐时气象）、`csv`（本地标准逐时 CSV）、`open_meteo`（Open-Meteo ERA5）、`pvgis_tmy`（PVGIS v5.3 TMY）或 `nasa_power`（NASA POWER） |
| `capacity` / `electrical_design` | 默认严格模式（`sizing_mode="strict"`）下**二选一必填**：`capacity` 直接给电压/电流边界，`electrical_design` 给温度系数与极值推导；若设 `sizing_mode="auto_from_components"` 则**两者都必须省略**，由工具按组件参数自动推导 |
| `losses` | 可选损失参数覆盖，支持 `LIDLoss`、`PMismMPP`、`ModuleQualityLoss` 与主阵列 `UFactor` |
| `scope` | 可选场景范围：`fixed_unshaded_grid`（默认）、`ground_sheds`、`ground_tracker` 或 `stand_alone`（仅 `pvsyst_simulate_design`） |
| `ground_layout` | 地面电站布局配置（`ground_sheds`/`ground_tracker`）：支持 `model_mode="unlimited"\|"3d"`、多朝向无限棚架 `sub_orientations`、有限地块 `site_polygon`、`obstacles`、`base_slope_deg` 与 3D DEM `terrain` |
| `bifacial` / `transformers` / `standalone` | 可选双面组件参数、MV/HV 多级变压器配置（含 PVsyst 8.1.7 原生容量与损耗预检）及离网光储柴 `standalone` 规格 |
| `report_pages` / `report_language` | 可选 PDF 报告页名列表与输出语言代码（仅支持 `"en"` 或 `"zh"`） |

调用示例：

```json
{
  "spec": {
    "new_project": "DESIGN.PRJ",
    "year": 2024,
    "target_dc_kw": 21.6,
    "tilt": 28,
    "azimuth": 0,
    "module": {"file_name": "ValidatedModule.PAN"},
    "inverter": {"file_name": "ValidatedInverter.OND"},
    "weather": {"mode": "existing", "sit_name": "SITE.SIT", "met_name": "SITE.MET"},
    "capacity": {
      "max_dc_ac_ratio": 1.3,
      "capacity_tolerance_pct": 5,
      "voltage_standard": "IEC",
      "cold_voc_v": 65,
      "hot_vmp_v": 38,
      "cold_vmp_v": 47,
      "module_max_operating_current_a": 16,
      "module_max_short_circuit_current_a": 18,
      "mppt_max_operating_current_a": 32,
      "mppt_max_short_circuit_current_a": 40,
      "design_min_temperature_c": -10,
      "design_min_operating_temperature_c": 20,
      "design_max_operating_temperature_c": 70,
      "design_basis": "Site extreme -10 C ambient, 20..70 C cell operating range."
    }
  },
  "dry_run": true
}
```

### 组件检索、模板筛选、边界写法与六种气象模式

1. **组件匹配**：按大小写不敏感精确匹配查找 `manufacturer + model`（或 `file_name`）。工作区 `ComposPV` 散文件优先级高于 `DataRO` 内置库；同优先级目录出现多个同名同型号文件时需改用明确 `file_name`。
2. **模板排序与空工作区打底（Bootstrap）**：不传 `template_project` 时按“逆变器台数改动最少 → 参数修改步数最少 → 容量偏差最小 → 名称字典序”自动择优；对刚建好的空工作区（根目录名为 `PVsyst8.1_Data`），`dry_run=false` 会自动补齐 26 个标准子目录并从内置标准种子派生基础工程。
3. **两种容量边界写法**：
   - **直接传 `capacity`**：直接给出冷态 $V_{oc}$、冷/热态 $V_{mp}$、组件与单路 MPPT 最大工作/短路电流（IEC 62548 / NEC 690）。
   - **传 `electrical_design`**：温度系数必须写明单位和出处（不能拿 $\beta_{P\max}$ 冒充 $\beta_{Vmp}$）；整机 `IDCMax` 不能直接充当单路 MPPT 最大电流；用 $\alpha_{Isc}$ 推导最大电流必须同时给出 `max_irradiance_w_m2` 和 `rear_gain_factor`；温度须满足 `-100 ≤ 最低环境温度 ≤ 最低组件工作温度 ≤ 最高组件工作温度 ≤ 150 °C` 并附 `min_ambient_basis`。
4. **六种 `weather.mode`**：`existing`、`synthetic_from_site`（基于工作区已有 `.SIT` 由 PVsyst MeteoTables 原生合成逐时气象）、`csv`、`open_meteo`（取前后整点均值 `endpoint_mean` 并保留 `needs_review` 说明）、`pvgis_tmy`（仅非闰年）、`nasa_power`。三种在线模式支持传唯一 `location_id`，或显式传齐 `latitude`、`longitude`、`site_label`、`altitude` 与 `timezone_name`（二选一），自动对齐当地冬季标准时（`LegalTime`，拦截半小时或负夏令时区）；`dry_run=true` 时在线模式返回 `status="ready_for_weather"` 与 `execution_preflight_complete=false`。

---

## 2. 基于规格书引用创建自定义 PAN / OND / BTR 组件

使用 `pvsyst_build_sourced_component` 可基于工作区模板覆盖（`template_name` 设值，继承底层参数记入 `.sources.json` 且状态为 `needs_review`）或零模板合成（`template_name` 省略）：
- **`.PAN`**：5 参数单二极管求解器（`RShunt`、`Rp_0`、`RSerie`、`Gamma`），自动写入双面标志位 `$00800000`；已有 `.PAN` 可通过 `pvsyst_update_component(..., refit_single_diode=True)` 重拟合。
- **`.OND`**：自动生成 Mode-1 8 点单电压 `ProfilPIO` 或三电压 `ProfilPIOV1..3`（`$1560` + `VNomEff`）。
- **`.BTR`**：零模板合成 `LFP` / `NMC` 锂电池，自动校核单电芯内阻 ESR（`<= 40 mV@I10`）与 `AlphaSOC` 约束；离网 `.GEN` 由 `pvsyst_simulate_design(scope="stand_alone")` 内联合成。

操作流程：`dry_run=true` 预览 `diff` / `inherited_fields` / `inherited_curves` / `provenance` → 审核无误后以 `dry_run=false` + `acknowledge_inherited_model=true` 正式写盘。

| 组件类型 | 字段名 | 允许单位与换算规则 |
|---|---|---|
| `PAN` | `PNom` / `ISC`, `Imp` / `Voc`, `Vmp` | `W`（原文 `Wp` 可用 `source_unit_text="Wp"`）/ `A` / `V` |
| `PAN` | `MuISC` | `mA/degC`、`mA/C`、`A_per_degC`、`pct_per_degC`（正值） |
| `PAN` | `muVocSpec` | `mV/degC`、`mV/C`、`V_per_degC`、`pct_per_degC`（**负值**） |
| `PAN` | `muPmpReq` | 仅 `pct_per_degC`（**负值**） |
| `PAN` | `BifacialityFactor`, `NCelS`, `NCelP` | `ratio`（`0..1`）或 `%`，与 `count`/`cells`/`strings` |
| `OND` | `PNomConv`, `PMaxOUT` / `VMppMin`, `VMPPMax`, `VAbsMax`, `VNomEff` / `IDCMax`, `IMaxDC`, `NbMPPT` / `EfficMax`, `EfficEuro` | `kW` / `V` / `A` / `%`（如 `97.8`） |
| 通用商业字段（8.1.6） | `Width`, `Height`, `Depth`, `Weight` | `m` 或 `mm`（`mm` 自动 `/1000` 并记录 `unit_conversions`）、`kg` |

除温度系数 `muVocSpec`、`muPmpReq` 必须为**负值**外，其余电学/尺寸数值均须为正数（`EfficMax`/`EfficEuro` 在 `(0, 100]`），且强制校验 `Vmp < Voc`、`Imp < ISC`、`VMppMin < VMPPMax <= VAbsMax`、`PMaxOUT >= PNomConv`、`EfficEuro <= EfficMax`。`Manufacturer`/`Model`/`DataSource`/根 `Comment` 同步更新（非 ASCII 自动加 UTF-8 BOM）并保留原 `Remarks`。官方参考：[PV modules basic data](https://www.pvsyst.com/help-pvsyst7/pvmodule_basicdata.htm)、[Determination of model parameters](https://www.pvsyst.com/help-pvsyst7/pvmodule_parameters.htm)。

---

## 3. 站点与标准逐时/亚小时气象制备（`pvsyst_prepare_weather`）

`pvsyst_prepare_weather` 将 `Meteo` 下经 `pvsyst_inspect_weather_csv` 校验通过的标准 CSV（支持原生 `5/10/15/30/60 min` 步长）转换为 `.SIT` 与 `.MET`，并在 `Results` 下生成来源清单 JSON。如须联网下载，可先用 `pvsyst_acquire_weather(..., prepare=true)` 一键串联。

输入要求与校验规则：
- 仅含 `GHI`、`DHI`（`W/m²` 或 `MJ/m²`）与 `Tamb`（`deg.C`/`degC`），完整覆盖目标年份（`1969..2068`，含闰年 2 月 29 日）。
- 显式传入 `time_basis="local_standard_interval_start"` 与两位大写 ISO `country_code`。
- `dry_run=false` 拒绝覆盖同名文件，按序写出规范导入 CSV/月值 CSV、`create-site` 生成 `.SIT`（回读校验经纬度 `±0.0000501°`、海拔 `±0.500001 m`、月辐射 `±0.051 kWh/m²`、月均温 `±0.0051 °C`）、生成 `RefTime=LegalTime` 的 `.MEF` 并调用 `convert-meteo` 生成 `HLeg=True` 的 `.MET`，最后写入清单。
- 将清单文件名传给 `pvsyst_simulate_design` 的 `weather.weather_manifest_name`，仿真前自动重验哈希与时基，结束后自动比对 `GlobHor`/`DiffHor`/`T_Amb`（差值超 `0.1 W/m²` 或 `0.01 °C` 标 `needs_review`）。官方参考：[PVsyst standard format](https://www.pvsyst.com/help/meteo-database/import-meteo-data/pvsyst-standard-format.html)、[Custom meteo procedure](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/procedure.html)、[Time shift](https://www.pvsyst.com/help/meteo-database/custom-meteo-files/time-shift.html)。

---

## 4. 住宅 3D 场景端到端任务（`pvsyst_run_house_task`）

`pvsyst_run_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False)` 全程无桌面 GUI 自动化，串联工作工程克隆、`.pvc2` 生成（含屋顶网格与 `building`/`tree`/`wall`/`mesh` 四类 `obstacles`）、免头写入含 3D 阴影/组件布局的 `.VCx`，再调 `PVsystCLI.exe` 跑全年仿真并支持断点续跑；正式执行（`dry_run=False`）与断点续跑（`resume=True`）都必须同时传入 `confirm=True`（未确认时返回 `status="confirmation_required"`）。若需同时绑定规格书，可用 `pvsyst_run_sourced_house_task`（正式执行时需在审核完规格书绑定后显式传入 `accept_source_review=true`）。

### 关键特性与边界

- **多屋面与坐标基准**：单/多固定朝向屋面均需符合 `anchor0` 约束（原生多朝向中朝向 1 使用 `FieldAzim=0` 作为参考系）；源 `.PRJ` 与 `.VCx` 必须引用同一对工作区 `.SIT`/`.MET`。
- **Module Layout 与显式 `string_layout`**：传入 `"shading_mode": "module_layout"` 启用原生组件布局与精细电气阴影（导出 `ShdElec`）。无 `string_layout` 时支持单朝向双表双串或分组多朝向每子阵 1 串 1 表（`PowerSharingInverterId >= 0`）；提供同级 `string_layout={tables:[{name,subarray_id,cells:[{row,col,global_string}]}]}`（`row`/`col` 1 基，`global_string=0` 草稿禁止直接导入）可支持 `1..64` 张表跨表/一表多串，且 `linear` 模式会在写入前拒绝带有 `string_layout` 的场景避免静默忽略。
- **CSV-only 输出（`report_pdf=False`）**：严格布尔可选参数；复用纯 CSV 单出口（不传 `-rpf:`），不产 PDF 也不列 PDF 守卫，阴影重算仍在私有非交互桌面允许建窗（若同时设置 `allow_window_creation=false` 会在预检拦截）。损失图 PDF 渲染仍依赖 CLI 创建窗口权限。
- **阶段与续跑**：产物统一使用 `<stem>_house` 前缀，阶段依次为 `initialized` → `project_created` → `scene_created` → `prepared` → `native_saved`（`stored_geometry_verified` 为 `true`；仅 `shading_mode="module_layout"` 时 `stored_module_layout_verified` 为 `true`，`linear` 模式下保持 `false`；此时 `native_consumption_verified` 仍为 `false`）→ `simulated` → `completed`。支持 `resume=true, confirm=true` 断点续跑、`native_save_reconciliation` 保护未登记变体以及 `resume=true, confirm=true, retry_simulation=true` 递增 `_run02` 重试仿真。

---

## 5. AC 耦合光储工程（`pvsyst_run_engineering_task` & `engineering-storage`）

`pvsyst_run_engineering_task` 支持固定平面、无限跟踪（`UnlimTracking`）以及集中式 AC 耦合光储工程的隔离修订与全年仿真（在 `spec` 中传 `changes.storage` 和 `load_manifest_name`；`changes: {"storage": {}}` 可生成基线对照清单）：

- **负荷与光储前提**：源 `.PRJ`/`.VCx` 须引用同一对 `.SIT`/`.MET`，站点标准时偏移须与 `load_manifest_name` 一致；`WeakGrid` 策略必须同时提供覆盖全年的 `availability` 宣言。注意 PVsyst 仿真直接消费 `pvLoad` 内嵌的 `Energy/MJ` base62 缓存——仅修改外部 CSV 或 `HourlyFile` 仍会继续沿用旧负荷，改完 CSV 须重跑 `pvsyst_prepare_load_profile` 和 `pvsyst_create_native_load_variant`。同时 PVsyst 8.1.7 中 `PowerShift` 仅使用可用光伏发电充电（含 `SpecChargPower` 模式，不支持电网充电）。
- **能量平衡与字段核对**：使用 `grid_storage` `.SFI` 预设导出 `E_User`、`EFrGrid`、`E_Grid`/`EToGrid`、`E_Miss`、`EUnused`、`CL_Over`、`EBatCh`、`EBatDis`、`SOC_Beg`/`SOC_End`/`SOC`；自动核对全年时间轴、`E_User` 与逐时负荷一致性、SOC 与 AC 充放电功率边界（含变流器效率损耗换算）及 AC 能量平衡，光储场景跳过标准纯光伏 PR 校核。在组合功率控制中，视在功率限发模式下 `PGridLim` 列头仍标为 `kW` 但实际为 kVA，且 `LimGrSeparLoss=True` 时 `E_Grid` 为扣减限发损失后的上网电量。

---

## 6. 多方案对比、简报预审与工程交付打包

### 多方案对比（`pvsyst_compare_engineering_scenarios`）

对 2~3 个已完成仿真做差值对比（候选 − 基准），输出 `<stem>.json`/`.csv`/`.md`：
- 基准方案 `declared_changes` 必须为空 `[]`；候选方案显式声明改动的分组或点路径（如 `"orientations"`、`"storage"`、`"subarrays.0.values.NModSerie"`），工具自动区分 `intended` 有意改动与 `unintended` 意外差异。
- 强烈建议绑定 `run_manifest_name`（`<stem>_engineering_<year>.json`），由 `load_engineering_run` 校验运行时快照与文件指纹防篡改；不传时 `historical_input_binding_verified=false` 且状态降为 `needs_review`。两方案均为逐时负荷时须确认内嵌 base62 快照与清单/源 CSV 一致才会把临时目录差异归入 `normalized_location_differences`。
- 逐时对比严格按时间戳对齐，功率列按固定步长积分为 `kWh`，保留缺测不补零，`EBatCh`/`EBatDis` 按直流侧原值读取、`SOC*`/`SolFrac` 不做积分并汇总 `soc_changes`。

### 简报只读预审（`pvsyst_review_engineering_brief`）

纯内存校验 `sources`（`<=50`）、`claims`（`<=500`）与可选 `blocks`（`<=100`）：
- 按 `field` 严格配对单位（例如 `module_count`/`modules_per_string`/`string_count`/`inverter_count`/`inverter_input_count`/`block_count` 传 `count` 正整数；`dc_capacity_kw`/`ac_capacity_kw` 传 `kW`，勿把视在功率 kVA 当有功 kW）。
- 仅对 `scope="project"` 无冲突条目汇总验算容量乘积、整串整除、分块累加、$P_{\max}$ 与 $V_{mp}\times I_{mp}$ 偏差及储能时长比；有硬冲突或缺失关键项时返回 `blocked`，否则返回 `needs_review`。

### 工程修订与 ZIP 交付（`pvsyst_export_engineering_delivery`）

1. `pvsyst_project_handoff(project, variant, declarations?)` 只读导出保存态与缺失项。
2. `pvsyst_run_engineering_task(..., dry_run=True/False)` 克隆并运行修订（`nominal_dc_power_kw` 须显式声明；失败时现场保留在工作区，需换新 `new_project` 重试）。
3. `pvsyst_export_engineering_delivery(manifest_name, output_name, dry_run=True, comparison_name="")` 校验清单指纹并打包 `summary.md`、`inputs.md`、`.PRJ`/`.VCx`、CSV/PDF、运行清单及可选多方案对比资产为新 `.zip`；导出响应保留底层 `task_status` 与 `task_native_consumption_verified`，自身 `native_consumption_verified=false`。

---

## 7. 测试、CI 验证与发版

- **分层套件**：
  - `tests/integration/`：快速无依赖集成测试（`--suite mcp` 约 <5s，`--suite integration` 全集成）。
  - `tests/unit/<core|energy|weather|workflows>/`：离线单元测试（`python scripts/run_tests.py --suite <slice|unit|all> -v`）。
  - `tests/e2e/test_mcp_full_workflows_e2e.py`：需连接真实 Windows `PVsystCLI.exe`（`PVSYST_E2E_REAL_NATIVE=1 PVSYST_E2E_CLI=... python -m unittest tests/e2e/test_mcp_full_workflows_e2e.py -v`），自动从 `DataRO\PVsyst8.1_Data` 播种并快照恢复 `PVsyst.ini`/`PVsystCLI.ini`。
- **本地 Wheel 与 CI 校验**：
  ```sh
  python -m pip install build
  python -m build
  python -m pip install dist/pvsyst_cli_mcp-0.8.0-py3-none-any.whl
  python scripts/ci_checks.py --report dist/ci-report.json
  python scripts/run_tests.py --suite workflows -v
  ```
- **CI/发布**：[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 在 Ubuntu 与 Windows（Python 3.10 / 3.12）运行离线构建、安装与零跳过集成+单元测试；打 `v<version>` tag 时校验与 `pyproject.toml` 一致并发布草稿 Release。
