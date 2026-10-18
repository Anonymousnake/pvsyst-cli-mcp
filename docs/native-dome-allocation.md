# 原生 Dome 双朝向分配

## 原生入口与输入归属

PVsyst 8.1.6 的系统窗口已有“第二朝向”开关：一个子阵可共享两面，不必复制硬件或增加容量。R31 的原 Dome 方案保留两条合法配对朝向，但仅给第一面分配了子阵；原生在 Checking variant 阶段报朝向 #2 无 PV array。R40 在副本中再次复演该失败，原文件和原失败保留。

在原生系统窗口启用第二朝向后，软件将原有 40 串默认分为 39/1；在第一面串分配控件输入 20 并失焦，软件自行更新第二面为 20。两组均保存为新 VC 并重载确认，总计仍为一个子阵、25 块/串、40 串、1000 块组件、6 台逆变器；原生报告为 440 kWp / 360 kWac，未增加硬件。

持久化采用 `NoOrientation=1`、`NoOrientation2=2` 和 `NStrOrient1`，**不保存 NStrOrient2**。39/1 与 20/20 的完整文件仅根 Comment 和 NStrOrient1 不同；两条 Dome 记录、Flags/Flags2 与电路树均不变。首次 GUI 提交还把两处原有 Resistance 从 36.9117423071806 写为 36.912，属于软件保存行为；新 MCP 入口从已分配模板继承该值，不自行归一化或改电阻。

## MCP 入口

`pvsyst_create_dome_allocation_variant` 从已有效分配的 Dome 模板创建新 VC，只编辑第一面串数，不创建 Dome 配对、不修补原未分配方案、不转换固定平面、不复制子阵或逆变器。首次配对与分配仍走上述原生入口。

```json
{
  "project": "example.PRJ",
  "source_variant": "VCX",
  "new_variant": "VCZ",
  "first_orientation_strings": 20,
  "variant_name": "Dome Allocation 20-20",
  "dry_run": true
}
```

`first_orientation_strings` 必须为严格整数 1..39，源计数也必须在同域。它是 40 串参考模板的适配器范围，不是已测 GUI 硬限、工程限值或自动最优分配。剩余串归属由既有原生 NoOrientation2 关系消费，不生成第二面曲线、电流、错配或发电量模型。

支持子集的声明版本为 8.1.6、根 Flags=0、pvSystem 仅 Grid/Flags=$06000000；主阵 Flags=$01900400/Flags2=$082A；一个子阵 Version=8.1.6、Flags=$019E1002、Flags2=$20、ID=1、NoOrientation=1/NoOrientation2=2、NStringCh=40、NModSerie=25、NInverter=6、NInvMPPT=1、Generic_Mono_440W_Bifacial.PAN 与 Generic_60kW.OND。保留原电路节点，复用计数检查，不从 NElements 名称自行推断电气分组。

必须有一个计数为 2 的 TOrientGroup 列表，内含恰两条直接所属 pvDomeOrient。每条 14 个 owning 字段齐全且唯一：主面 Dome、NoOrient=1、NoPairOrient=2、IsMainOrient=True、倾角/方位 10/90；背坡 DomeBackfaces、NoOrient=2、NoPairOrient=1、IsMainOrient=False、10/-90。均为 Flags=$00、BaseSlope=0、三个 Aver=False、BTNoTrackers=-1,-1,。Comment 仅要求唯一且原样保留，不按残留 Fixed 文案判型或取角度。

不接受普通 pvOrient、错配/重复/缺失/越界列表、未知或嵌套朝向内容、源坏计数、其它分配字段；NStrOrient1 只允许出现在真实 pvSubArray owner。子阵含原生 WiringResistance/EndTags 结构，不误当 flat 记录；只有 Dome 记录与两字段 pvSystem 使用共享 flat 校验。不允许负荷、储能、P50/P90、保存的地平线、显式 3D 场景或 Module Layout。固定模板是证据范围，不是硬件选型推荐。

## 事务与兼容

默认预览不写盘，返回差异；执行用锁、路径校验、源快照复核与独占创建写一个新 VC，不覆盖大小写同名目标。只改 NStrOrient1、可选新 VC 根 Comment 和显式 BOM；其它字段、源 PRJ/VC、天气、组件和拓扑原样保留。写入失败披露部分产物路径，`native_consumption_verified` 始终为 false。

中文名字复用已有显示名/BOM 规则，源无 BOM 时须显式 `add_utf8_bom=true`，`bom_added` 报告实际新增。Dome 的 closing tag 只在解析视图适配，根名完整校验也使用该视图，行索引始终映射回原始列表；不修改全局 alias 或旧固定平面/季节/地平线门控。

共享 require_flat 从削峰模块原样搬到结构编辑模块，旧削峰逻辑未改变，其公共预览已作针对性兼容检查。没有通过改旧 `_orientation_list` 来放开 Dome，也没有把不支持的源伪装成普通固定平面。

## 验证与失败记录

公共 MCP 新进程实际列出 64 个工具，Projects & Variants 为 22 个。原白名单在新项目上通过 6 个正向、51 个无写入负例；正向含与 GUI 完整文件逐字节一致、同值创建、1 串下界、Comment 普通词与显式中文 BOM。另验证同名并发仅创建一次，以及共享校验迁移后的旧削峰预览。

首次公共预览因旧根名 helper 只识别普通朝向 closing 而失败，记录 `dome-public-r40.json` 原样保留，无新 VC 写入。修复仅在新入口传入自身的解析视图；在全新 DOME_CONFIG2 工程原样重跑同一断言，`dome-public-r40b.json` 通过。未改旧 helper、全局 alias、原失败记录或断言标准。

GUI 重载时旧等待器的 5 秒期限有一次超时，随后同一窗口已显示正确 20/20，无重开、改输入或重复保存；现场与确认记录分开保存于 `dome-ui-wait-review-r40.json`。GUI 正常关闭，完整 INI 备份逐字节恢复；超时不推成程序崩溃。

两份 GUI 参考均完成原生全年 2022 年运行：

| 分配 | 原生年度 E_Grid kWh | 输出 |
|---|---:|---|
| 39/1 | 749595.2076 | 8760h / 16 数值列 / 7 页 PDF |
| 20/20 | 761489.8343 | 8760h / 16 数值列 / 7 页 PDF |

PDF 明确两面为 Dome front face 10/90 与 Dome backface 10/-90，并确认同一 440 kWp / 6 台逆变器；原生 Mixed orientation mismatch loss 分别 0.02% / 0.01%。这些是原生混合朝向结果，不是独立 3D、电气阴影或模块背面增益的证明；DomeBackfaces 是屋顶配对背坡，不直接等同双面组件的背侧发电。

公共创建的 39/1、20/20 与 1/39 均已有通过的全年运行，1/39 的 E_Grid=773274.3974 kWh。39/1 以及重新独立核对的 20/20，其全部 8760×16 共有值及单位与相应 GUI 参考精确一致，PDF 年量一致；1/39 仍为 440 kWp、两面角度不变，并输出原生 mixed-orientation mismatch。年量差只用于证明本场景分配被消费，不外推分配最优性或其它气象年的收益。

20/20 的首次公共全年调用在初始化阶段未完成，留下空 CSV、无 PDF；该失败及空文件保留于 `dome-api-native-r40.json`。输入与已通过 GUI 参考逐字节相同。没有修改输入、结果、模型或断言；一次独立原生诊断复演返回 0、完整生成 CSV/PDF，完整 stdout/stderr 另存 `dome-native-diagnostic-r40.json`。随后不改任何输入，经新 stdio 会话的标准 MCP 路径独立复核也完成，`dome-native-recheck-r40.json` 确认诊断及公共复核的全部年度数据都精确等于 GUI 参考、两份 PDF 均一致。首次退出原因尚未确立，不把后续成功改写为“从未失败”，亦不新增自动重试来掩盖该现象。

本机证据还有 `dome-source-map-r40-976.md`、`dome-allocation-persistence-r40-4cb81.md`、`dome-native-r40.json`、`dome-workspace-r40.json` 及对应 CSV/PDF。商业模型、气象、软件与临时验证代码不随仓库分发。未运行旧 smoke、全量测试发现或自定义哈希检查；现有生产并发保护保留。常驻 MCP 进程需重启加载新工具/schema。其它 Dome 角度/几何、首次构造、跨类型转换、任意硬件与显式 3D/Module Layout 继续单独验证。
