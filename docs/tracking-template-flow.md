# 跟踪模板完整年度任务

`pvsyst_simulate_template`增加显式跟踪分支，不新增工具或调度/发电模型。默认固定模式、`pvsyst_prepare_design_project`的零年准备行为保持。跟踪分支只接受已证8.1.6rev49278的既有UnlimTracking模板；不做固定转跟踪、任意几何/场景、其他跟踪族、设备选型或数量调整。

## 输入与阶段

下例是本机已执行的公开请求，文件需在当前工作区；工程/型号/天气为实验夹具，不是厂家或现场认证。

```json
{
  "template_project": "TRACKING_INPUTS.PRJ",
  "variant": "VCK",
  "new_project": "DECLARED_TRACKING_FLOW.PRJ",
  "pan_name": "Generic_Mono_440W_Bifacial.PAN",
  "ond_name": "Generic_60kW.OND",
  "site_name": "MCP_DESIGN93.SIT",
  "met_name": "MCP_DESIGN93.MET",
  "year": 2024,
  "tracking": {
    "axis_azimuth_deg": 5.1,
    "backtracking": true,
    "irradiance_optimization": true
  },
  "tracking_variant": "VCRUN",
  "nominal_dc_power_kw": 440,
  "tracking_power_source": "Inherited native report TRACKING_CONFIG_VCAG_2022_R38.pdf: 1000 x 440Wp; Original PVsyst database. Experimental source, not manufacturer/site certification.",
  "report_pages": ["summary", "losses"],
  "dry_run": true
}
```

先预检`status=ready`及issues/review；再同一请求显式`dry_run=false`执行。预检零写入/零仿真，不把ready当原生消费。`tracking_variant`必须是源工程尚不存在的新VC；保留原VC。传入tracking时省略tilt、azimuth、capacity、losses；两模式输入混合或只有tracking_variant/功率而没有tracking均停止。

复用链为：既有结构/PRCorrTemp=False/跟踪记录门禁→精确核对PAN/OND引用→天气年历/许可/版本/产物冲突检查→受保护克隆→既有SIT/MET绑定及快照→`create_tracking_variant`独占创建新VC→结构回读→新CLI加载已存VC一次全年→原生CSV/PDF/上下文/完整性/PR检查→清单与原始输入保护。跟踪控制字段、范围和源形状仍见[原生跟踪控制](native-tracking-controls.md)，不复刻原生跟踪、回溯、辐照或二维遮挡算法。

## 来源与状态

此分支只处理无同名loose覆盖的继承数据库引用，工作区或已配置builtin中出现同名PAN/OND就停止，运行前后再核对。它不把找不到loose文件直接判成原生数据库缺件，也不伪造同名文件。PAN/OND名字必须与源子阵完全对应，数量、硬件、损失和非请求几何保持继承；源码中只读检查不调用组件替换。

`nominal_dc_power_kw`为显式正有限kWp，`tracking_power_source`为可审查的非空来源声明。它们沿用已有年度结果汇总的标准PR交叉检查，不从文件名、PNomSizing缓存或历史发电量猜铭牌。原生结果须完整覆盖所选小时年，E_Grid/EArray/IL_Pmax等单位与PR一致，所有实际数值列有限，并有GlobShd的W/m²单位；原生省略的变量不补零。

`native_consumption_verified`在预检/失败为false，仅在全部原生结果和输入保护检查通过后为true。数据库内部模型未静态读取或内容快照，因此即使消费通过仍返回`needs_review`、`review_required=true`、`component_resolution=native_database_uninspected`。当前可复演证据不是数据库完整冻结、厂家限值/电气选型、工程净距、实际气候或全套项目类型认证。

## 保留与恢复

原生前/后都保留原模板和天气；只写新工作工程及结果。跟踪任务失败保留JSON清单、独占新VC及已创建产物，不自动删除、回滚或重试。恢复项`archive_isolated_tracking_task`明确要求审查并归档整个新克隆（含其全部VC）、备份和输出，再用新工程名重试；不是可执行API命令。它不返回因新增VC而过期的逐文件撤销命令。成功或失败后的同名调用均在预检停止，既有产物不覆盖，不偷偷增加年度运行；没有自动检查点续算承诺。

## 实际证据

中文8.1.6rev49278源文件，执行器为CLI同版本、英文报告；本批无GUI/INI操作，DPI不适用。原始Unlim模板来自既有原生类型样本，25串联×40组串=1000组件、6台逆变器保持。父独立读取旧原生PDF确认`Original PVsyst database`、440kWp/360kWac，但不采纳“由文件缺失可证明虚拟生成算法”的推断。当前新运行才是消费证据。天气为既有北京ERA5 2024实验SIT/MET，原始LegalTime93及现场/气候来源边界保持。

公开任务只执行一次新的2024全年；准备/预检零年，未重复旧年度控制。新VC轴5.1°、回溯/辐照优化True在公开getter重载及原生PDF回显；CSV上下文精确绑定新工程/VCRUN/SIT/MET，8784小时、13实际数值列连续有限。E_Grid=758735.0392kWh、EArray=772802.2696kWh、IL_Pmax=24.9846kWh；PR=86.1898427291%，原生辐照加权PR=86.1897433832%，既有差异检查通过。原生summary/losses两页明确Unlimited Trackers with backtracking、5.1°、440kWp/1000组件/6台与年度能量。近处遮挡标题no Shadings与原生二维GlobShd/损失并存，不解释成不存在二维互遮挡。

35份源文件前后字节差为空；完成态同名调用不变更产物，真实loose覆盖件停在克隆前。临时消费边界故障注入在进入原生run-simulation之前抛错：状态failed/阶段simulation/消费false，新VC、SFI与清单保留、无CSV；随后同名重试停止，保留文件不变。此临时故障检查不是第二次原生年度、不是永久Mock测试，也没有用伪造结果验收消费。

永久非哈希白名单仅`tests/contracts/test_tracking_template_flow_boundary.py`与既有未改`tests/contracts/test_prepare_design_project_boundary.py`，4项通过、无skip；分别覆盖新schema/混合模式/缺来源与零写入、真实源预检及旧固定准备零年。显式环境为`PVSYST_TRACKING_FLOW_FIXTURE`/`PVSYST_TRACKING_FLOW_CLI`和原准备契约的两个环境；缺真实资料时相应项skip，不算原生通过。必要编译及diff检查另计；未跑全量发现、旧smoke、哈希或no-hash探针，既有生产冲突保护保留。44.805秒是契约执行耗时，不是性能基准。

辅助失败均保留：最初按loose文件复制原生数据库引用，发生缺文件并在任何原生操作前停止；首次年度驱动给新版SDK传timedelta，初始化前失败、原生年度次数0，改数字秒后才执行上述唯一一次。临时审查将标题写为区分大小写的Unlimited trackers，而实际为Unlimited Trackers，故在原生完成及恢复检查之后报断言失败；只修验证记录并独立读真实PDF，不重跑或改输入。

本机`pvsyst-tracking-task-study/round1`保存request、原始输入、预检/唯一原生运行、父侧报告摘录、最终审计及故障清单。`tracking-source-asset-provenance [magpie/mw/deepseek-v4.1-flash-sg]`已收集；子代理超文件白名单的PDF/目录调查及“必然虚拟生成”结论未验收，父侧独立证据与实时输出优先。派生实验资产不上传，完成后一次性驱动/故障辅助清理。
