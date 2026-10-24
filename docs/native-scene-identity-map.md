# 场景逐区身份入口：原生GUI实测

**当前仍没有通用逐区身份/源坐标验证器，分组自动import继续needs_context。** 本批确认了可用的物体属性入口，也实测了虚拟树语义和绘制文本入口的停点；不为一次失败读取补建桌面框架，不把属性摘要当高精度源坐标。

版本/环境：中文PVsyst8.1.6rev49278、目标DPI96；使用既有线程物理坐标上下文，未改产品/进程/OS的DPI设置。隔离克隆SCENE_IDENTITY_REVIEW.PRJ:VC2，读取已有40构件/2PV/2系统朝向配置；新会话实际Popen由父持有，GUI/INI串行。没有导入、另存、能量仿真或阴影系数表计算。

## 入口与证据层

| 入口 | 实测 | 结论与边界 |
|---|---|---|
| 静态可访问性绑定 | 父用现有pefile只读单个PVsyst.exe，确认oleacc!LresultFromObject和UIAutomationCore!UiaReturnRawElementProvider导入 | 仅候选绑定；静态符号不等于树节点可访问 |
| TOrientManageForm两棵TVirtualStringTree | Windows原生UIAutomation各返回一个无名称Pane，0descendants、0支持模式；AutomationId是当次HWND数值；WM_GETOBJECT/OBJID_CLIENT返回0 | 此会话没有程序可读的行名/表身份；不把HWND当稳定标识，不据此推断所有VCL树/版本 |
| 场景物体→PV表属性 | 既有场景右侧物体列表的实际PV叶双击，异步打开TTableFieldForm；名称/朝向/尺寸页可读 | 可复演的原生属性入口，不等于通用树语义；叶坐标/41与42是本案例观察，不硬编跨文件身份 |
| 属性基本参数 | 表41：#1、28/0、18组件；表42：#2、28/90、18组件；继承Comment25/20仍非真实角度 | 与保存引用可交叉查看，但没有认证源XML身份/网格 |
| 场景位置摘要 | 表41[-0.8,-0.4,7.6]m、表42[-70.4,0.8,7.6]m；页面标注基于三维摆放定义，Height on roof显示0.4m | 原生显示精度低于保存DOrigine，不等同于原文坐标；0.4m不是本工具推导的工程净距 |
| 尺寸页框架参考组合框 | 当前“左下角”、CB索引0；列表为左下角/底部中心/右下角/左上角/顶部中心/右上角/矩形中心；读取未改选择 | 保存TypeOrigine=1与当前UI观察并存，不把组合框索引直接当序列化枚举，也不据此解释约10mm偏移 |
| WM_PRINT→GDI/EMF文字 | 对可见属性滚动区只读绘制，结果只有EMR_HEADER/EOF，无文本记录 | 未得到文本；不把空绘制结果说成标签不存在，不加OCR/坐标近似模型填补 |

UIAutomation复用系统已有UIAutomationClient/Types，不安装comtypes/pywinauto。宿主实际测得为WindowsPowerShell5.1路径；辅助读取需要进程级ExecutionPolicy参数，不改持久策略。探针只验证持有进程的HWND/PID，读取Current属性/支持模式，最多256后代，不执行UIA选择、拖拽或默认动作。

原生绘制记录结构按微软[EMREXTTEXTOUT](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-emrexttextouta)及[EMRTEXT](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-emrtext)定义检查，实际未取得文本。研究辅助不是产品解析框架或几何算法。

## 失败与恢复

准备脚本首次未创建父目录、首次所有者脚本假定pwsh在PATH，均在INI/GUI之前停止。新实际会话目录r4保留，旧r3空目录说明该前置失败，未冒充两次GUI验证。首轮UIA辅助被系统脚本策略挡住，日志保留；同一GUI内仅对辅助进程加Bypass后重读成功，首轮失败不算原生不支持。

双击属性会异步加载，首次0.4秒快照尚为父视图，后续等待父视图超时才看见TTableFieldForm；在该超时中菜单键尚未发送。选择页签曾以父窗口作为点击目标，既有WindowFromPoint守卫在鼠标动作前停止；改成实际TPageControl目标。另一次查询隐藏尺寸组合框失败，是现有_kids仅枚举可见子窗口；先切到真实尺寸页再读取，没有为这些研究错误改产品代码。

浏览表42尺寸页后取消出现“是否要对物体进行修改？”；父先读取实际警告图，再选“否”舍弃局部状态，未按未知模态自动确认。表41取消直接返回场景。最后依次取消3D视图和近处遮挡页，不调用会生成阴影表的_close_scene流程；按实际Popen正常关闭工程/主页、观察退出后恢复DataPath。19份输入副本前后字节差为空，未断言仅凭“只读意图”就必然不变。

`native-tree-object-entry-map [magpie/mw/deepseek-v4.1-flash-sg]`实际加载reverse-engineering做限定静态盘点，父核实单exe的导入/资源/字符串面；没有静态DFM窗口定位证明。子代理额外枚举同目录DLL的自述超出文件白名单，该部分不作为验收。没有脱壳/补丁或对原始二进制写入；原生软件仅通过已述GUI入口执行，不把静态建议的自定义消息当已验证入口。

## 留存与下一步

本机pvsyst-native-identity-study/round1保存static-parent-check.json、native-preparation.json、native-owner-r4/state.json及Win32/UIA/PNG/空EMF证据、original-inputs和CLOSED交接。所有者与辅助临时代码完成清理，重要失败输入/环境/动作序列保留。纯入口证据批次没有新增产品代码/永久测试，没有运行测试发现、旧smoke、哈希或no-hash探针；仅文档差异检查，不新增全年成本。

下一步仍需独立的源空间对应/高精度身份入口；没有证据前不自动拖拽到目标组，不扩大MCP写入。此停点不阻止推进其它已证原生入口的固定项目任务闭环；先复用既有读取/参数事务/结果与恢复，不让桌面适配探索占据整条业务路线。
