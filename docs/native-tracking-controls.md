# 原生无限水平轴跟踪控制

## 入口与支持域

`pvsyst_create_tracking_variant` 预览或创建一个新的 VC，只修改现存 `UnlimTracking` 朝向记录的五个原生字段。不提供固定平面到跟踪器的转换，不编辑跟踪器几何、电气效应、3D 场景或 Module Layout，不在适配器中计算跟踪角、回溯或辐照优化。已有固定/季节平面和地平线入口的门控不变。

```json
{
  "project": "example.PRJ",
  "source_variant": "VCK",
  "new_variant": "VCX",
  "tracking": {
    "axis_azimuth_deg": 0,
    "min_rotation_deg": -45,
    "max_rotation_deg": 60,
    "backtracking": true,
    "irradiance_optimization": true
  },
  "variant_name": "Tracking Controls",
  "dry_run": true
}
```

源必须已经具备下述原生记录；普通固定平面或其它跟踪类型会被阻断。五个 `tracking` 参数全部必填，未知字段、混合类型、布尔代替数字、字符串数字及 NaN/Inf 不被默默转换。

| 输入 | 原生字段 | 适配器支持域 |
|---|---|---|
| axis_azimuth_deg | AxisAzim | -20..20 度 |
| min_rotation_deg | MinPhi | -60..-45 度 |
| max_rotation_deg | MaxPhi | 45..60 度 |
| backtracking | UsesBacktracking | 严格布尔 |
| irradiance_optimization | UsesIrradOptimiz | 严格布尔 |

角度为 0.1 步长并保存一位小数，使用 Decimal 校验，不用浮点近似容差或静默舍入。上述为本入口的适配器支持域，**不是已测 GUI 原生上下界或 GUI 步长**。非对称界限与四种布尔组合均允许；不交换非法 min/max，不扩大到其它角度域。

配置限定原生声明版本 8.1.6、PV-only、单子阵、单个计数的 TOrientGroup、直接所属 NoOrient=1/Flags=$00/FieldType=UnlimTracking。根 Flags 必须为零；pvSystem 仅含 Flags=$06000000 和 SystemType=Grid；主阵 Flags=$01900400/Flags2=$082A；子阵 Version=8.1.6、Flags=$019E1002、Flags2=$20。所有组串分配至朝向 1，NStrOrient1=NStringCh，其余 NStrOrient*=0，并通过既有电路完整计数检查。

朝向的 23 个已证 owning 字段必须齐全且各自唯一，五个控制不得出现在其它 owner。源角度、开关本身也必须满足支持域，坏值不会被新值覆盖掩盖。只在解析视图匹配 `End of TOrientGroup`，不改变真实文件的 closing tag 或公共解析 alias。

几何固定于已证模板：Pitch=6.600 m、CollectorWidth=3.000 m、两侧 InactiveBandWidth=0.020 m、CellWidth=0.156 m、NSheds=10、NModInWidth=3、FracShadingFactor=1.000；UsesElectricalEffect 和三个 Aver 开关保持 False，BTNoTrackers 保持 `-1,-1,`。这些是本入口模板条件，不是工程选型推荐。朝向 Comment 即使残留 Fixed 文案也原样保留，不用它判断类型。

含负荷、储能、P50/P90、保存的地平线、显式 3D 场景或 Module Layout 的源阻断；**无显式场景不等于关闭原生二维互遮挡**。几何和电气效果字段不改，但原生近处遮挡结果可以随跟踪控制变化。

## 事务与继承

默认 `dry_run=true` 不写文件，返回候选 diff；仅 `dry_run=false` 创建新的 VC，大小写同名目标不覆盖。继承已有工作区锁、路径检查、源快照复核与独占创建；写入失败公开可能的部分产物路径，不删除失败现场。源 PRJ/VC、组件、天气、其它记录及历史结果不改，`native_consumption_verified` 始终为 false。

只有五个控制、可选新 VC 根级 `variant_name` 和显式 BOM 改变。名字复用既有显示名规则；中文名需源已有 BOM 或显式 `add_utf8_bom=true`，新增情况由 `bom_added` 披露。现存名字中的普通 `pvLoad`/`pvGridStorage` 字符串不是模型对象；两个新配置工具均按真实 object_type 判断，而非将 Comment 子串误判为储能或负荷。

源里的 DateCalcul、VersionSimul、缓存及残留描述不代表新输入已计算。创建后通过已有 `pvsyst_run_simulation` 使用匹配年份/时基的 SIT/MET，核对新 CSV/PDF；新工具不生成替代模型或外部结果修正。

## 公共与原生验证

新进程实际列出 62 个工具，Projects & Variants 为 21 个。公共白名单 10 个正向、67 个无写入负例；五组完整输出逐字节等于 [R31 GUI 参考](native-orientation-controls.md)，三个新参数输出也逐字节等于相应原生探针输入。另通过同名并发仅成功一次、中文显式 BOM、大写 VC 回显、Comment 普通保留字、旧固定平面预览及旧接口继续阻断跟踪类型。原生负荷工具的同类 Comment 误拦也做了正例与真正储能对象负例。

R38 先对四组新参数运行 2022.01.01–01.02 的 48 小时探针：负轴方位、双开关、非整数角和非对称界限均被原生报告读出，各有 48 行有限输出与原生 PDF。这些短期结果不外推全年。其后三个公共创建的配置独立运行 2022 全年：

| 新变体 | 参数摘要 | 原生 E_Grid 年量 kWh | PDF |
|---|---|---:|---:|
| VCAG | 轴 -20 度、phi -60/+60、两个开关 False | 912345.0039 | 7 页 |
| VCAH | 轴 0 度、phi -45/+60、两个开关 True | 923043.6916 | 8 页 |
| VCAI | 轴 10.1 度、phi -55.1/+50.2、回溯 True、优化 False | 919563.0201 | 8 页 |

各有连续有限的 8760 小时和 17 个数值列，Produced Energy 与原生小时求和在报告精度内一致；前 48 小时全部 16 个共有列及单位与独立短期探针精确相同。算法/回溯报告与请求一致，源输入保留。表中同时改变了多个条件，不从两行的年量差单独归因某个开关。

### 开关组合隔离对照

另通过公共 MCP 创建三份固定轴 0 度、phi -45/+60 的 FF/TF/FT 变体，各运行 2022.01.01–01.02 的 48 小时；TT 采用上述 VCAH 全年的相同前 48 小时。每一对只改变一个布尔控制，其余原生输入保持相同，比较 GlobInc/GlobShd/GlobEff/EArray/E_Grid：

| 单变量切换 | 有变化的小时数 | 原生 E_Grid 期间变化 kWh |
|---|---:|---:|
| 优化关闭时，开启回溯 | 10 | -24.866 |
| 回溯关闭时，开启优化 | 10 | +0.600 |
| 优化开启时，开启回溯 | 17 | -25.316 |
| 回溯开启时，开启优化 | 3 | +0.150 |

该对照补充 3 次公共创建和 3 次原生短期运行，证明两个开关在已开启另一开关时也有可观察影响，而不只是报告标签同时出现。48 小时变化不外推全年，也不将回溯或优化解释为任意几何/天气下必增发电量。原始数据与调用见 `tracking-combination-controls-r38.json`。

### 原生二维遮挡与导出失败裁决

经已存在的 `pvsyst_build_sfi` 请求 `viGlobHrz/viGlobShd/viShdElec`，三个全年样本实际都导出 `GlobShd`，省略 `GlobHrz` 与 `ShdElec`。初验将额外 `GlobHrz` 列视为必需，因而三组辅助检查失败，**原失败记录保留**；三次原生运行本身正常结束且无警告。没有改输入、补造缺列、填零或重复运行来制造通过。独立复核确认控制验收、全部时序/共有列、完整报告和发电年量均通过；未将缺列推成通用物理开关语义。

实际导出的 GlobInc/GlobShd 单位同为 W/m²，观测积分差占比分别 6.0737%、1.4288%、1.7212%，与报告近处遮挡辐照损失 6.07%、1.43%、1.72% 的舍入一致。分别有 4261、3670、3842 个小时差大于 0.15 W/m²（只用于避开导出舍入的计数阈值，不是物理阈值）。三份报告同时保留 `Near Shadings / no Shadings` 标签，因此该标签不代表没有内建二维互遮挡。电气效果 True、独立 3D/Module Layout 仍未由本批验证，不把 GlobShd 差值建立成外部遮挡模型。

SFI 使用已实证的 `vi*` 序列化标识，CSV/定义表使用不带 vi 的列名；不能将定义表标签当成 SFI 写入形。Show/Simul 或字符串存在仅是选列线索，不等于该场景实际导出或消费。

本机证据：`tracking-native-probe-r38.json`、`tracking-public-r38.json`、保留失败的 `tracking-native-r38.json`、独立 `tracking-consumption-review-r38.json` 及对应 CSV/PDF。原 R37 GUI 会话在范围检查前退出、原因未定，未记为正常关闭；`tracking-recovery-r38.json` 记录随后完整 INI 备份逐字节恢复与所有克隆 VC 同源文件。该退出没有被用作 GUI 硬范围证据。商业输入、程序、实验文件不随仓库分发；未跑全量测试发现、旧 smoke 或自定义哈希检查。

本批不扩大其它跟踪类型、任意几何、跨类型转换、活动 3D、电气遮挡、Dome 系统分配、储能或地平线耦合；也不将上述模板、单年天气或原生默认值当作通用工程限值。已运行的 MCP 进程需重启加载新工具/schema。
