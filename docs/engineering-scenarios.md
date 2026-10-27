# 按工程目的比较原生方案

`pvsyst_compare_engineering_scenarios` 组织两或三个已有原生结果，首项为基准，逐项比较候选减基准。它不创建方案、不运行PVsyst、不计算新的物理或调度模型，也不自动判定最优。底层组合函数为 `compare_engineering_scenarios`。

```python
compare_engineering_scenarios(
    store,
    objective="比较同一组件、天气和容量下的固定平面与跟踪方案",
    scenarios=[
        {"label": "固定平面", "project": "BASELINE.PRJ", "variant": "VC0",
         "csv_name": "fixed_2024.csv", "declared_changes": [],
         "run_manifest_name": "fixed_2024_run.json", "year": 2024},
        {"label": "跟踪方案", "project": "TRACKING.PRJ", "variant": "VC0",
         "csv_name": "tracking_2024.csv", "declared_changes": ["orientations"],
         "run_manifest_name": "tracking_2024_run.json", "year": 2024,
         "assumptions": ["两方案使用同一原生组件与天气；结构边界见对应运行记录"]},
    ],
    output_name="fixed_tracking_comparison.json",
    dry_run=True,
)
```

示例中的工程、变体、CSV和运行清单应替换为工作区中的真实文件。

每项必须明确 `label/project/variant/csv_name/declared_changes`。基准的声明改动为空；候选以保存条件的组名或点路径声明有意变化，例如 `orientations`、`storage`、`subarrays.0.values.NModSerie`。可用组为 `project/system/main_array/orientations/subarrays/grid_circuit/sources/load/storage/shading_scene`。显示名称变化不作为工程条件变化。候选可选 `year`、`nominal_dc_power_kw` 和文字 `assumptions`；容量只用于所给时段的单位装机电量，不证明工程选型。

## 历史输入绑定

原生CSV头部必须明确工程、变体、SIT和MET。工程/变体必须与请求相符；SIT/MET保持原文件名。**名称匹配不证明历史配置**：同名VC在仿真后可能已被编辑。当前工程交接只披露当前保存状态，不能充当过去运行的输入记录。

`run_manifest_name` 引用 Results 中由受控工程任务产生的 `operation=engineering_revision` 清单。它必须包含完成的原生运行、运行时 `input_handoff`、`working_files/input_assets/result_files` 和CSV/PDF引用。对比复用既有 `load_engineering_run` 校验工程/变体、请求CSV、完成状态、当前输入与原生输出的一致性；使用其运行时handoff比较历史条件。绑定的SIT/MET还须与CSV头部一致。既有生产快照与冲突保护保留，不把清单当签名认证凭据或跨进程锁。

未给运行清单，或旧清单缺少完整绑定时，仍可查看数值差异，但返回 `needs_review`、`historical_input_binding_verified=false`；条件比较明确标为当前保存状态。显式提供完整新清单却发现漂移、错误引用或未完成运行时停止，不静默退回“已绑定”。需要比较改动前后配置时，保留独立基准和候选工程，不覆盖旧VC后再把当前配置归给旧CSV。

逐时负荷的保存摘要省略原生 `Day_*` 缓存内容，因此不能只凭相同FileName/摘要宣布相同负荷。只有双方运行绑定均已核实 `load_binding.embedded_snapshot_matches=true`，年份、固定时基、UTC偏移、行数及源/profile名称相同，且 `load_manifest/load_source/load_profile` 三项绑定资产完全相同，才将同FileName下的绝对 `UserHourlyParams` 目录位置差异单列为 `normalized_location_differences`。这仅处理 `PathName` 及其交接副本/位置标识，不忽略文件名、其它负荷字段或真实缓存输入差异；原始值仍保留。缺少该证明时负荷不列入已确认共同条件，保留复核。

## 指标与解释

结果复用已有严格CSV读取和 `compare_results`：时间轴必须逐行一致、严格递增且唯一；共同列必须有相同的明确单位。缺测按缺失计数，既不填0，也不根据别列推算。没有共同可比较列时停止。不同方案独有的原生列在各自摘要中保留，并明确其它方案缺失。

既有AC光储列如 `EFrGrid/E_Grid/E_User/E_Miss/EBatCh/EBatDis/SOC_Beg/SOC_End` 以及实际出现的 `EToGrid/SOC` 分别报告，不擅自合并或改名。W/kW在稳定时间步下积分为kWh；完整电量只在该列全部样本有限时给出，部分观测电量单独披露。`EArray/EUseful/EDirUse/E_Solar/EUnused` 均从软件直接列读取，分别显示双方时段电量与差值；缺少原生列时不由其它列相减补造。直接利用、有效供能和太阳能供能保留各自原生定义，不自动视为同一个自用指标。

`EBatCh/EBatDis` 明确为电池DC侧输出，不能冒称AC充电器输入、电池逆变器输出或端口额定功率；本比较也不按效率反推另一侧。SOC和 `SolFrac` 只接受原生ratio或%，不积分为kWh。SolFrac仅报告原生配对样本平均差，不将普通小时比例平均冒充年电量加权比例。PR与辐照不套用电池调度模型，也不对光储直接应用纯并网PR公式。

每方案 `result_summary.columns` 记录首时段 `first_value`、最小/最大值及其时刻；首样本缺测保持null，不改取后一个有限值。`metrics[].soc_changes` 给出双方SOC首样本、各自时段最小/最大值和差值；最低/最高差值可以来自不同时刻，不是逐时配对最大差。`SOC_Beg` 的首样本是原生首时段开始SOC，`SOC_End` 首样本是首时段结束SOC，不能在没有SOC_Beg时当作初始SOC。观测极值不证明触及保存的配置限值，也不换算可用电量。

摘要说明候选减基准的上网、购电、未供负荷和削峰等原生变化，保留共同条件、有意改动和未声明差异。关键时段给出每个共同指标最大绝对差的时间、基准值、候选值和单位。多条件同时变化时不分摊因果。短时输出只解释所给时段，不外推全年；完整8760/8784小时年仍不等于来源、物理或设计认证。

## 输出与状态

`EDirUse`和`EUseful`沿用原生通道含义，不自动等同直接供用户负荷或用户自用率，也不按列名重新归类。

`output_name` 是不含目录的 `.json` 文件名；输出在Results下使用相同stem的JSON、CSV和中文Markdown。CSV为每方案指标与候选减基准的长表，空单元格表示缺失或不可积分；Markdown包含条件、指标、关键时段、假设与复核。JSON保留运行时和当前交接、条件依据、缺列、覆盖率、差异及产物路径。原始商业模型和天气不复制到报告中。

`dry_run=true` 只读取并返回全部对比预览，不建目录或产物；`artifacts` 此时仅为计划路径。`written_artifacts` 明确实际已写的产物。三个目标文件必须全部为新名字；任一已存在则停止。写入中途失败时保留已经创建的产物并报告，不覆盖、不静默删除。

实际CSV和Markdown写完后，持久JSON的 `report_files={csv, markdown}` 使用既有生产 `fingerprint` 绑定两个报告文件；JSON不自指纹。交付包先限定三个受控Results路径，再核对这两个值，并重新加载每个方案的运行清单；报告完整性不替代原生输入绑定。dry-run的 `report_files` 为空，不冒充已生成文件。

`ready/created` 表示预览或报告写入成功；存在历史绑定缺项、继承的复核、缺测、缺列、非完整年或未声明差异时保持 `needs_review`。`blocked` 查看 `issues`。本入口始终 `native_consumption_verified=false`，因为它没有执行新的原生仿真；逐方案的历史绑定状态另列，不把数值对照升级为工程认证。
