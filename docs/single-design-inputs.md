# 单方案输入与全年流程

`pvsyst_simulate_design`组合精确组件、合格原生模板、声明包络、整数容量、天气准备和[全年任务](automatic-simulation.md)。scope仅fixed_unshaded_grid：单子阵／普通固定Grid、PRCorrTemp=False，无活动阴影／表／Module Layout，不生成空白物理设计或操作GUI。

## spec

| 字段 | 要求 |
|---|---|
| new_project | 全新PRJ名，派生源与产物也须无冲突 |
| year | 严格整数1969..2068，与实际天气覆盖一致 |
| target_dc_kw | 明确正有限DC目标 |
| tilt／azimuth | 0..90°／-180..180°，PVsyst南0、西正 |
| module／inverter | 精确manufacturer+model或明确file_name |
| template_project／variant | 可选，限制候选，不被别的模板隐式替代 |
| weather | existing、csv或open_meteo |
| capacity／electrical_design | 必须恰有一个；声明包络／来源参数 |
| losses | 仅LIDLoss、PMismMPP、ModuleQualityLoss及main作用域UFactor |
| report_pages | 可选原生页名，见[报告](automatic-simulation.md#天气与报告) |

严格嵌套schema，不接受未知键、布尔／数字字符串冒充数值或浮点冒充整数。内层目标若给出必须等于顶层，不调和矛盾声明，不退化为保留模板数量。未请求损失继承并披露，不按安装类型猜默认值。

以下是完整调用形状示例，文件须实际存在，包络仅演示声明结构，不是厂家／场址建议：

```json
{
  "spec":{
    "new_project":"DESIGN.PRJ","year":2024,"target_dc_kw":21.6,"tilt":28,"azimuth":0,
    "module":{"file_name":"ValidatedModule.PAN"},"inverter":{"file_name":"ValidatedInverter.OND"},
    "weather":{"mode":"existing","sit_name":"SITE.SIT","met_name":"SITE.MET"},
    "capacity":{"max_dc_ac_ratio":1.3,"capacity_tolerance_pct":5,"voltage_standard":"IEC","cold_voc_v":65,"hot_vmp_v":38,"cold_vmp_v":47,"module_max_operating_current_a":16,"module_max_short_circuit_current_a":18,"mppt_max_operating_current_a":32,"mppt_max_short_circuit_current_a":40,"design_min_temperature_c":-10,"design_min_operating_temperature_c":20,"design_max_operating_temperature_c":70,"design_basis":"Explicit demonstration; replace with sourced site and hardware envelope."}
  },
  "dry_run":true
}
```

## 组件、模板与种子

身份按大小写不敏感精确相等，工作区loose文件优先配置库；胜出库有多个同身份文件即歧义，要求精确名。不模糊替换或枚举加密数据库；不可枚举不等于物理缺件。完整PAN/OND先静态检查，再读取所属electrical字段，不凭字段可读保证拟合正确。

模板结构完整，容量计划ready后按逆变器变动最少、参数步数最少、容量误差最小、项目／变体名排序。显式模板错误直接报issues；自动扫描保留每个拒绝原因，不因一个坏候选放弃其他合格成员。

没有合格成员且调用方未指定模板／变体时，可从已安装标准原生模板只读派生源，再复制工作工程。这是内部fallback，不是新工具，不从零造设计。预览仅内存，不建Projects；名称冲突及不匹配种子停止。原种子保留声明版本／BOM及历史引用；继承BatchParams不证明其实际加载。

CLI8.1.6工作区要求根名PVsyst8.1_Data及规范布局：17根子目录、ComposPV内9目录。26目录是支持集合，不声称最小必要集合。根不改名／移动／替换；provider请求前检查缺目录、嵌套链接逃逸及同名文件。执行可补齐缺目录，预览不写；不外推其他原生版本。

## 电气来源

capacity直接采用[明确包络](capacity-sizing.md)。electrical_design接受数值或value/unit/source声明，温度系数须同时明确单位和来源，不猜PAN序列化单位。电压包络可直接提供或按带来源系数形成并回传公开线性关系，这不是PVsyst内部模型拟合。

beta_pmax不替代beta_vmp，整机IDCMax不当每MPPT限值、不除以MPPT数。系数化电流需max_irradiance_w_m2、rear_gain_factor及source，不默认STC或增益1；直接声明两电流限值时多给辐照会提示未使用。显式mA／kV等非期望标度停止，A／V或键携单位的值按规则接受。

设计最低环境温度需min_ambient_basis，不能用单年天气最低值代替。-100≤min环境≤min组件工作≤max组件工作≤150°C；组件工作温度不等于环境温度，相等条件会提示。可选manufacturer_model须匹配实际PAN/OND身份标签。来源绑定不是厂家认证，空请求返回字段级缺项而非ready。

## 天气与获取

existing使用明确SIT/MET；未绑定清单只做名称核对，不宣称逐时源天气比较。csv需已暂存标准CSV及local_standard_interval_start，复用[天气准备](weather-preparation.md)。open_meteo用明确location_id或完整坐标，两者不混用；统一spec的坐标路径须latitude、longitude、site_label、altitude、country_code和timezone_name。

resolve_location只读返回全部候选和整数ID，只有恰好一个时selection_required=false；多候选明确选择，零候选补限定，不自动取首位。ID路径国家码取候选，冲突报错；不是由provider推断地点。

获取仅访问Open-Meteo geocoding/archive，年份1940..2068且须完全发布，只取GHI/DHI及temperature_2m。UTC辐射为前一小时均值、区间终点标签；温度为瞬时值。CSV以固定当地标准小时起点表示，含相邻UTC边界读数及全部闰日，不填缺测／未发布小时或插值。

endpoint_mean是两端UTC瞬时温度的无权平均近似，始终保留review，不是观测小时均温。provider0.25°网格及DEM回显与请求站点元数据分开，底层获取若使用DEM高程必须明示，非实测。原始响应、CSV、来源清单均新文件，失败也保留，拒绝覆盖。

IANA解析在Windows依赖tzdata。固定标准偏移只支持-12..+12整小时；半小时、年内标准偏移变化及采样日负DST约定停止，不重分箱或猜冬季偏移。原生SIT坐标／高程复核界±0.0000501°／±0.500001m只是打印精度，不等于位置完全相同。LegalTime和MET声明须按[天气审核](weather-engineering-review.md)检查，标签匹配不独立证明有效太阳位置。

## 状态与恢复

dry_run=true零文件／零仿真。在线尚无CSV时为ready_for_weather且execution_preflight_complete=false，只是计划；只有实际走到仿真预检才为true。执行协调获取、准备、模板任务，各阶段非明确可继续状态即停止。

返回status、stage、issues、warnings、selected、preview、metrics、artifacts和recovery。completed是该入口检查结论，needs_review保留来源／近似／原生警告，不当工程认证。失败保留源、天气、工作项目及清单，不自动重试、覆盖、回滚或清理；recovery记录而不执行恢复，原模板只读。

方案比较、储能、离网、多朝向及3D使用其他入口；本流程不认证全部气候、物理、电气、曲线或厂家参数。来源草案和准备见[资料绑定](weather-engineering-review.md)、[独立源准备](design-project-preparation.md)及[来源住宅任务](sourced-house-task.md)。
