# 场景身份与坐标边界

自动双屋面任务仅支持单栋、anchor0的受限形状，通过原生PV表朝向属性、SHD导出、原生归位和保存／重载空间核对建立对应。它不是任意逐区身份或源坐标验证器，非零anchor和任意多组不由此获得支持。任务仍保留needs_review，使用方式见[工程交付](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。

## 原生接口约束

- GUI入口受中文PVsyst8.1.6rev49278、DPI96布局约束，坐标操作使用线程级物理坐标上下文，不修改进程或系统DPI。
- 物体列表中的实际PV表叶可打开`TTableFieldForm`，读取名称、朝向和尺寸。窗口异步创建，应等待实际目标控件，而非依赖固定延时。
- 自动表名、Comment、NoObjCreated、HWND和组合框索引不构成跨文件稳定身份。控件索引也不直接等于保存枚举。
- 原生位置摘要的显示精度低于保存DOrigine；摘要坐标不等于PVCollada源坐标或现场工程净距。
- 原生朝向管理器的虚拟树不保证提供可访问行名。静态导入、可见控件或空UIAutomation结果均不证明通用树身份读取能力。

## 核对原则

逐区对应应同时核对原生朝向引用、组件数量、支持的源几何和保存坐标。读到属性摘要或stored_consistent状态不代表源空间一致，不通过猜测轴、锚点偏移、OCR近似或另建几何模型来补齐未知对应。

控件、窗口或源形状不满足要求时停止并保留实际状态，不凭历史叶名自动拖拽。更广场景的导入与支持状态见[分组场景](grouped-house-scenes.md)和[原生入口地图](native-entry-map.md)。
