# PVsyst 3D 场景窗口控件与表格坐标核对笔记

本文档记录在中文版 PVsyst 8.1.6 rev49278（96 DPI）下驱动 3D 近阴影编辑器、识别每张光伏组件表（`pvShdArrayTable`）并核对空间坐标时的底层技术细节。关于双屋面自动交付任务的完整使用方式，见[工程交付双屋面章节](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。

## 为什么不能直接用 UIAutomation 读朝向管理器树

在对 PVsyst 8.1.6 的 3D 界面做逆向测试时，我们发现：
1. **虚拟树控件（`TVirtualStringTree`）无文本暴露**：朝向管理窗口（`Shift+Ctrl+O`）左右两侧的树形列表是 Delphi 的虚拟树控件，Windows UIAutomation 只能看到一个无名称的空 `Pane`（0 个子元素），发 `WM_GETOBJECT` 或 `WM_PRINT` EMF 拿不到行文字。
2. **属性窗口（`TTableFieldForm`）可精确读取**：在 3D 编辑器对象列表里点开具体 PV 表的属性窗口 `TTableFieldForm`，可以直接读出并修改该表的实际名称、组件颗数（如 18 块）、倾角/方位角（如 `28°/0°`、`28°/90°`）以及所属朝向组合框（`#1` / `#2`）。注意该窗口是异步创建的，自动化代码需要轮询等待目标子控件就绪，不能靠死等固定毫秒数。
3. **界面位置摘要精度不够**：属性窗口上显示的位置摘要精度低于 `.VCx` / `.SHD` 里实际保存的 `DOrigine`，并且导入 `.pvc2` 时的自动对象名（`Comment`）和序号（`NoObjCreated`）也不能作为跨文件固定不变的 ID。

## 双屋面（`anchor0`）的坐标对应与校验做法

正因为界面树和摘要存在上述限制，在单栋双屋面（`anchor0`，即朝向 1 `FieldAzim=0`）自动化流程中，工具采取了“属性窗口定朝向 + `.SHD` 导出与 `Ctrl+B` 归位 + 保存重载后解析 `.VCx` 复核”的组合方案：
- 在 `anchor0` 坐标系下，导入 PVsyst 后的原生 `X`/`Y` 坐标与源 `.pvc2` 局部坐标符号相反（`-X`, `-Y`），`Z` 坐标不变。
- 由于 PVsyst 的位置输入框最多只吃 7 个字符，工具在生成和归位时会校验坐标在 7 字符精度下的舍入误差不超过 `0.00002 m`（20 微米），并在 `SaveAs` 另存和重新加载变体后，同时核对每张表的 `NoOrient` 引用、组件颗数、三维尺寸、角度以及 `0.00002 m` 以内的坐标残差。

关于 `.VCx` 保存状态的静态关联读取，另见[已保存 PV 区域关联检查](native-scene-bindings.md)与[双朝向场景生成](grouped-house-scenes.md)。
