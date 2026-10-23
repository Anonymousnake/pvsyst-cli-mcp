# 引用资料到住宅交付

`pvsyst_run_sourced_house_task` 把既有来源token绑定、[源工程准备](design-project-preparation.md)和[住宅原生任务](native-house-task.md)组合成一次公共任务。没有新发电/选型/调度引擎，也不另建作业框架；源准备和住宅两份既有清单分别管理自己的阶段。

## 一份输入

| 字段 | 要求 |
|---|---|
| `design` | 复用 `SingleDesignRequest`；`new_project` 是独立新源工程，精确明文 `module.file_name/inverter.file_name`；地点/天气/倾角/目标等明确提供 |
| `new_project`, `variant_name` | 最终住宅的新工程名与展示标题；工程名与源不同、已有命名空间先停 |
| `sources`, `declarations` | 复用 `pvsyst_prepare_sourced_design` 的来源类型、引用、独立数值/单位token及模型身份规则；空/错资料返回草案，不猜值 |
| `goals` | 原有 `SourcedDesignGoals`；工程目标不是厂家限值。design.target_dc_kw 也可作为显式目标，冲突由原有来源/目标门禁停止 |
| `house`, `layout`, `module_geometry` | 复用完整住宅网格、一/两具名区或显式放置及组件几何输入；不重新发明房屋模型 |

`design.capacity/electrical_design` 应省略，由已绑定声明产生 `electrical_design` 草案，避免另一份数值覆盖引用。只有 exact loose PAN/OND，不在此协调器里猜设备或改用模糊厂家搜索。引用只证明声明与文本/身份一致，不认证资料真实性或模型拟合。

使用现有天气模式与预备入口；新天气尚未准备时只返回计划。设计包络的声明不自动改写模板所有项目级设计温度/损失，继承项仍通过源准备/住宅摘要披露；单年气象最低温度不变成现场设计依据。

完整源文本不写进产品准备清单，也不在结果回显；保存的是重新验证后的引用来源、摘录、数值、单位、草案与复核项。宿主原始请求/会话仍由宿主管理。恢复重新验证本次引用并比较已接受绑定，不依赖全文里与声明无关的段落。

## 执行与状态

函数参数为 `spec, dry_run=True, confirm=False, resume=False, retry_simulation=False, accept_source_review=False`，均为严格公开schema。

- `needs_context`：先补 `source_binding.electrical_design/missing_fields/issues`，不建源/住宅工程，不打开GUI。
- `needs_review`：来源或天气的保留问题；默认先停。只有显式 `accept_source_review=true` 才继续，`confirm` 本身不认证来源；即使最终原生计算通过，该状态仍保留。
- `ready_for_preparation`：新源准备预览已通过，但源尚未存在，`house_preflight_complete=false`，不是整栋住宅的完整预检。
- `ready_for_weather`：天气/内存种子仍是计划，不读取虚构文件继续走住宅。
- `confirmation_required`：写入/GUI执行仍需 `dry_run=false AND confirm=true`。
- `completed/needs_review` 且 `stage=completed`：查看顶层 `native_consumption_verified` 与 `house`。后者使用原住宅任务的原生验证结果；有源复核时顶层仍 `needs_review`，不当正常工程认证。
- `blocked/needs_attention`：查看当前来源、准备或住宅停点；不因为 MCP `is_error=false` 就当完成。

新鲜执行先绑定、预检源/最终命名空间、确认无现存PVsyst GUI，再准备源工程（零次全年），随后在一场自有GUI完成住宅导入/关联/有效性/遮挡表/原生SaveAs，正常退出后新CLI只运行最终方案的全年。原软件边界、进程所有权、未知窗口保护和不改INI约定不变。

## 检查点与恢复

源检查点为 Results 的 `<source-stem>_prepare_<year>.json`；住宅为 `<final-stem>_house_task.json`。没有第三份可变作业状态。

`resume=true` 先重新绑定来源，再检查准备记录的 operation/status、解析后的请求/引用、工作工程快照及请求推导的 PAN/OND/SIT/MET/天气清单资产。仅对受限标准路径使用既有生产冲突保护，不根据记录里的任意路径读取或执行。然后按是否存在住宅检查点继续原子任务；fresh调用遇到已有源或住宅清单先停，子清单也不隐式续跑。

源阶段尚未完成时保留原编辑/天气事务供查看和受保护恢复，不自动重做部分准备。准备已完成而住宅未建立时，可明确resume继续，不重获天气/重配置源。住宅已完成时重验已有输出，不重开GUI、不增跑；失败仿真沿原有显式 `retry_simulation=true` 新文件名策略，不覆盖旧输出。

组件或引用变动、损坏根、错误阶段/请求/路径均先停止，不改写准备清单。检查点不是签名认证凭据、OS进程租约或跨服务器事务；仍只支持单服务器/workspace进程内锁及协调后的外部编辑。

当前最终住宅按原有原生 CSV 上下文验证 SIT/MET，并守护对应资产；本协调器没有新增逐时源天气CSV对照，明确 `weather_native_comparison_performed=false`。有前置天气清单时复用其完整性/时基复核与原有review，不据此额外宣称住宅逐时天气列对照已执行。

## 已验收证据

本机 `pvsyst-project-input-study/sourced-house-r1` 留存完整声明请求、公共响应、原生结果及反例；商业/天气/模型不上传。

| 证据 | 结果 |
|---|---|
| `public-input-preview.json` | 75工具新会话；空资料13缺项；错误数值token停止；假设来源review默认停，显式继续才进入准备计划；所有路径保持 |
| `public-native-sourced-house.json` | 一次统一调用：绑定→源准备零次年→20构件/两区36模块原生住宅→新CLI2024全年；原生消费true、顶层仍needs_review |
| 同文件恢复记录 | completed+retry显式恢复无新源/场景/年度输出，child仍1次；改变目标容量时两个清单字节保持 |
| `public-recovery-negatives.json` | PAN附空白、源清单根[]、记录PAN路径改为OUTSIDE_NOT_ALLOWED均停止；确认/预览不改写清单；输入探针恢复并另存复演 |

原生年为8784小时、13数值列、8页PDF、年并网约36908.6885kWh；最终住宅仅一次年计算。源为公开论文完整600Wp PAN、原生Generic9kW、明确引用的实验电气包络与假设温度，沿用已证LegalTime93的2024 SIT/MET；几何为已声明的完整网格变换验收夹具。它验证流程和边界，不冒充现场住宅测量、厂家边界或气候设计认证。自有GUI已退出，父恢复原DataPath。

永久白名单 `tests/contracts/test_sourced_house_boundary.py` 两项使用明确外部来源/组件/已完成清单夹具和不可执行CLI，保护公开schema、缺项草案、错误引用、review门槛、零写入及完成态只读恢复不增跑；缺夹具skip不计原生，不当全年验收。此次两项均通过无skip。原生与恢复证据另计；旧入口契约和必要编译/差异检查按受影响范围执行，不运行全量发现、旧smoke或哈希类测试，生产冲突保护保留。

指定 `sourced-house-task-review [magpie/mw/deepseek-v4.1-flash-sg]` 初稿关于schema必变request及续跑门禁死代码的两条与源码/公开实证相反，父未照单改动；`sourced-house-final-review [magpie/mw/deepseek-v4.1-flash-sg]` 已纠正。父采用末次准备重验结果并保持review粘性，临时非原生子任务完成/状态变更探针`late-review-probe.json`通过；不计为新增原生运行。其余stem推断被`Path('X.PRJ').stem == 'X'`及同构命名代码否定，派生seed长度文案已有明确衍生名说明，不扩大无关补丁。审查与父实测独立记录，不互相替代。
