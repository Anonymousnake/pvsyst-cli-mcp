# GUI与MCP支持范围

当前工具名称、默认分组和用途见[工具参考](tool-reference.md)；候选、GUI持久化、原生消费及MCP实现分别见[入口地图](native-entry-map.md)。MCP不是PVsyst全部GUI功能的复刻。

## 工程工作流

已有工程读取与输入交接、有目的变更、原生结果比较、中文解释和交付ZIP组成[工程流程](engineering-delivery.md)。目前包括普通固定／受限UnlimTracking、已有原生AC光储和[单栋受限双屋面](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。设备、气象和场址设计依据仍由工程师提供或复核。

受限双屋面要求anchor0、两独占子阵和同PAN/OND，逐表身份与几何保存／重载检查独立于年度消费。非零anchor、任意多组、共享或异构拓扑不由此获得支持，完成态仍可needs_review。

## 支持判断

安装版CLI的help决定可用命令。文件写入、CLI接受、GUI重载保持、原生计算消费及工程有效性是不同层级；可读取原始状态不等于可编辑，字段可保存也不等于所有派生量自动联动。

| 领域 | 当前支持与主要限制 |
|---|---|
| 项目／变体 | [文件事务](project-source-verification.md)、[标题](project-title.md)、[拓扑](grid-capacity-verification.md)；完整文本／模板，不生成物理默认设计 |
| 元件资料／标量 | 明文PAN/OND/BTR/GEN的白名单字段，类型／owner／单位分别校验；旧二进制PAN只拷贝，不拟合派生模型 |
| OND/BTR曲线 | 受限Mode1点表与CSV输入；非线性编译、未知缓存、三电压等不由此获得支持 |
| 固定／季节／跟踪 | 各类型独立源门禁；不从普通平面标量编辑扩到任意3D或混合电气映射 |
| 损失／能量管理 | [原生地图](native-entry-map.md)列出受限热、质量、失配、积灰、IAM、辅助、PF、限值、P50等；未知耦合保持停止 |
| 天气 | [输入检查](weather-input-verification.md)、[准备](weather-preparation.md)、[审查](weather-engineering-review.md)；时基、单位、转换和气候认证分开 |
| 负荷／储能 | [小时负荷](storage-loads.md)、[AC系统](grid-storage-parameters.md)、[削峰](native-peak-shaving.md)；不等于混合DC端口或网充 |
| 场景 | [住宅作者／原生导入](house-pv-modeling.md)及受限任务；不提供通用CAD、家具、材料或周边环境建模 |
| 原生执行／结果 | [年度任务](automatic-simulation.md)、单次／批次CLI、实际CSV/PDF与[工程比较](engineering-scenarios.md)；不合成缺失结果 |

## 尚未覆盖

完整元件GUI往返与派生拟合、任意PAN技术／BTR化学体系切换、更多曲线模式／原生缓存、任意变压器／混合线缆、完整Module Layout／电气阴影、任意拓扑合并拆分、无结果P50专页构造、特殊随机负荷／停电、网充与混合逆变器DC端口等仍按各专题缺口处理。

原生控制可见或静态符号存在不建立完整入口；公开物理关系也不证明与内部模型完全一致。新能力应复用原生入口，不能用外部近似计算填补未支持功能。使用前以具体工具schema及专题支持条件为准。
