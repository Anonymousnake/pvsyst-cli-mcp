# 跟踪模板完整年度任务

`pvsyst_simulate_template`支持从既有UnlimTracking模板执行跟踪方案。仅支持8.1.6rev49278的受限源形状，不做固定转跟踪、任意几何／场景、其他跟踪族、设备选型或数量调整。默认固定模式及`pvsyst_prepare_design_project`的准备行为不变，计算由PVsyst执行。

## 输入与阶段

下例中的工程、设备和气象名称须替换为源模板的实际引用；功率及来源声明应来自实际设备资料。

```json
{
  "template_project": "TRACKING_INPUTS.PRJ",
  "variant": "VCK",
  "new_project": "TRACKING_DESIGN.PRJ",
  "pan_name": "Generic_Mono_440W_Bifacial.PAN",
  "ond_name": "Generic_60kW.OND",
  "site_name": "SITE.SIT",
  "met_name": "SITE.MET",
  "year": 2024,
  "tracking": {
    "axis_azimuth_deg": 5.1,
    "backtracking": true,
    "irradiance_optimization": true
  },
  "tracking_variant": "VCRUN",
  "nominal_dc_power_kw": 440,
  "tracking_power_source": "Project equipment schedule: 1000 modules rated 440 W each.",
  "report_pages": ["summary", "losses"],
  "dry_run": true
}
```

先检查预览的status、issues和review，再以相同请求设置`dry_run=false`。`ready`只表示预检，不表示原生消费。`tracking_variant`必须为源工程中尚不存在的新VC。传入tracking时省略tilt、azimuth、capacity和losses；混合两模式，或只有跟踪变体／功率却没有tracking时停止。

执行依次检查结构、PRCorrTemp=False和跟踪记录，核对设备引用、天气日历、许可、版本及产物冲突，然后受保护克隆、绑定SIT/MET、独占创建跟踪VC并读回。新CLI从保存VC运行完整小时年，再校验原生CSV/PDF、上下文、完整性、PR及输入保持。控制字段和范围见[原生跟踪控制](native-tracking-controls.md)。

## 来源与状态

仅接受没有同名loose覆盖的继承数据库引用。工作区或已配置builtin库中出现同名PAN/OND时停止；运行前后复核。缺少loose文件不被直接解释为原生数据库缺件，也不生成替代文件。PAN/OND名须与源子阵一致，数量、硬件、损失和非请求几何继续继承。

`nominal_dc_power_kw`是明确的正有限kWp，`tracking_power_source`是可审查的非空声明。标准PR交叉检查不从文件名、PNomSizing缓存或历史电量推测铭牌。原生结果须完整覆盖所选小时年，E_Grid/EArray/IL_Pmax等单位与PR一致，所有实际数值列有限，GlobShd单位为W/m²；缺列不补零。报告的no Shadings标签不代表没有内建二维互遮挡。

`native_consumption_verified`只在全部原生结果和输入保持检查通过后为true，预览和失败时为false。数据库模型内容未读取或冻结，即使消费通过也保持`needs_review`、`review_required=true`和`component_resolution=native_database_uninspected`。结果不认证厂家限值、电气选型、工程净距或场址气候。

## 保留与恢复

模板和天气在运行前后保持，只写新的工作工程及产物。失败保留JSON清单、新VC及已创建文件，不自动删除、回滚或重试。`archive_isolated_tracking_task`是人工恢复说明：审查并归档整个新克隆及其VC、备份和输出，再用新工程名重试；它不是可执行API命令。

同名任务无论先前成功或失败都会在预检停止，不覆盖旧产物，不提供自动检查点续算，也不隐式增加年度运行。
