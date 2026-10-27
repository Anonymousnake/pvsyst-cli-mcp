# 原生Dome双朝向分配

`pvsyst_create_dome_allocation_variant`从已有效分配的Dome模板创建新VC，只编辑第一面串数。它不创建Dome配对、不修补未分配方案、不转换固定平面或复制硬件。首次配对及两面分配应在原生系统窗口完成。

原生系统可让同一子阵使用第二朝向；保存NoOrientation=1、NoOrientation2=2和NStrOrient1，不保存NStrOrient2。第二面由既有原生关系消费，不生成独立电流、错配或发电模型。

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

first_orientation_strings和源值均须为严格整数1..39。这是40串模板的适配器范围，不是GUI硬限、工程限值或最优分配。总硬件保持，不由两面分配增加容量。

## 支持模板

- 声明版本8.1.6，root Flags=0，pvSystem仅Grid／Flags=$06000000，main Flags=$01900400／Flags2=$082A。
- 一个子阵，Version=8.1.6、Flags=$019E1002、Flags2=$20、ID=1、NoOrientation=1、NoOrientation2=2、NStringCh=40、NModSerie=25、NInverter=6、NInvMPPT=1，引用Generic_Mono_440W_Bifacial.PAN和Generic_60kW.OND。电路节点及完整计数保持，不按NElements名称猜分组。
- TOrientGroup计数为2，恰含两条直属pvDomeOrient，各14个所属字段完整唯一。主面Dome、NoOrient=1、NoPairOrient=2、IsMainOrient=True、倾角／方位10/90；背坡DomeBackfaces、NoOrient=2、NoPairOrient=1、IsMainOrient=False、10/-90。
- 两面Flags=$00、BaseSlope=0、三个Aver=False、BTNoTrackers=-1,-1,。Comment唯一且原样保留，不按残留标题推断类型或角度。

这些是特定模板条件，不是设备推荐。不接受普通pvOrient、错配／重复／缺失／越界朝向列表、未知嵌套内容、坏源计数或其他分配字段。NStrOrient1只接受真实pvSubArray所属字段，保留原WiringResistance/EndTags结构。负荷、储能、P50、地平线、3D场景和Module Layout均停止。

## 事务与解释

预览不写；执行使用工作区锁、路径校验、源快照复核及独占创建，不覆盖大小写同名目标。只改NStrOrient1、可选根Comment和明确BOM，其他记录、PRJ/VC源、设备、天气和拓扑保持。失败披露部分产物，native_consumption_verified始终false。

非ASCII标题沿用显示名及BOM要求；无BOM源需显式add_utf8_bom=true，bom_added报告实际变化。Dome闭合标签只在专用解析视图适配，索引映射回原列表，不放宽其他固定／季节／地平线入口。

DomeBackfaces表示配对屋顶背坡，不等于双面组件背侧发电。原生mixed-orientation mismatch是软件结果，不是通用3D或电气阴影验证。其他Dome角度、几何、首次构造、跨类型转换及任意硬件不由此入口支持。
