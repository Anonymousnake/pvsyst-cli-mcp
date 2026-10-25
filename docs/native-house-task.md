# 住宅单任务：原生工程到全年报告

`pvsyst_run_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False)` 将既有生成器、工程打开入口、住宅原生桥和 CLI 仿真组合成一个受限任务，不加入光伏、遮挡、调度或选型计算器。旧 `pvsyst_simulate_design` 的固定无阴影范围保持不变。

## 前提与输入

仅 Windows、中文 PVsyst8.1.6 rev49278、DPI96 及已记录窗口布局。原生 INI DataPath 须已匹配 MCP workspace；执行前须没有现存 PVsyst GUI。已有同工程窗口也只读识别，不因为 caption/INI 匹配就接管其未保存工作。库不写 INI、不强关进程；每个 workspace 只使用一个服务器/GUI 驱动，现有 RLock 是进程内协调，不是跨服务器桌面锁。

源方案沿用住宅桥的普通固定、单子阵、单朝向、北半球 PV-only Grid 子集，无已激活场景/模块布局、负荷、储能、P50/P90 或已存地平线。PRJ 与所选 VC 须已引用相同的工作区 SIT/MET，SIT 坐标须与源场景站点一致。任务不获取新天气、不重定地点、不推算厂家限值或设计温度；地点、天气和电气设计需要先由原有入口准备成明确源工程。[`pvsyst_prepare_design_project`](design-project-preparation.md)现已提供不先跑全年的独立源准备，返回精确项目/变体再交本文任务；来源/天气复核项仍须保持。[后续来源住宅统一任务](sourced-house-task.md)现已另批完成一次公共调用闭环；本页原始两调用与后续统一入口证据分开，不升级旧阶段结论。

| spec 字段 | 要求 |
|---|---|
| `source_project`, `source_variant` | 精确现有源 PRJ/VC；只读保护，不复制整个多方案目录 |
| `new_project` | 独立新 PRJ 文件名；源名、已有工程/检查点/SFI/场景/首轮输出冲突均先停 |
| `variant_name` | 原生另存新方案的展示标题，沿用原桥 1..80 printable UTF-8 bytes；实际 VC 编号由软件选择 |
| `year` | 明确整数 1969..2068；设定原生运行区间，不改写所继承气象文件的来源年份 |
| `house` | `kind=mesh` 的完整具名构件、顶点和零基三角；AI 一次提供，不由后端固定屋型重建 |
| `layout` | 一个显式放置区域，或 `tables` 中两个具名区域；组件总数匹配源方案，同一源 PAN/固定平面 |
| `module_geometry` | 沿用原生成器：可读 PAN 元数据优先；数据库引用需显式名称、功率、米制尺寸和几何厚度 |
| `report_pages` | 可选原生页名列表；省略为软件默认完整 PDF，错误/空列表在写入前停止 |

墙体、屋面、挑檐、门窗、烟囱、门廊、附属体量、天窗可一次提交；家具与精细五金不增加。网格完整性仍不是建筑结构认证；原生有效性也不证明全部孔洞/自相交/物理设计。模块元数据的来源与这些几何边界沿用[住宅建模契约](house-pv-modeling.md)。

`plan.source/modules/inherited_parameters/inherited_project_parameters` 披露继承的组件、数量、朝向、损失及项目设计温度等。软件默认值/实验模板值仍是继承值，不是现场或厂家依据；源没有可读 PAN/OND 时，数据库模型的内容完整性不由文件守卫额外证明。

## 执行和恢复

`dry_run=true` 不启动/激活 GUI，不创建目录、工程或检查点。实际执行须 `dry_run=false AND confirm=true`。返回 `blocked`、`confirmation_required`、`needs_attention`、`needs_review` 时检查 `issues/phase`，不只看 MCP `is_error`。

| phase | 已验证进度 |
|---|---|
| `initialized` | 请求/源预检，建立本任务检查点 |
| `project_created` | 通过既有完整文本入口创建工作 PRJ/VC0；源保留 |
| `scene_created` | 既有薄序列化器写独立 PVC2 |
| `prepared` | 既有 SFI builder 完成逐小时变量；尚未冒充原生导入 |
| `native_saved` | 自有 GUI 导入、朝向关联、原生有效性和遮挡表、原生 SaveAs、硬件/对象持久化检查通过 |
| `gui_closed` | 本次持有的 Popen 已正常退出；恢复时只观察无原生进程，不凭旧 PID 关闭窗口 |
| `simulated` | 新 CLI 进程重新载入已保存 VC，重算原生遮挡并返回 CSV/PDF |
| `completed` | 上下文、完整年、全部数值有限、GlobShd、PDF 和输入保持验证通过 |

工作区产物使用 `<new-project-stem>_house` 前缀：UserData 中 PVC2、Models 中 SFI、Results 中 `_task.json` 和 `_run01.csv/.pdf`。完整请求只存检查点，不在每次响应重复回传全部网格。原生保存、几何验证与 `native_consumption_verified` 是独立状态；只有原生全年结果验证后才升级消费标志。年度汇总复用原有结果验证器，不自算替代发电模型。

同一调用只开一场 GUI；已存 VC 的重新加载由随后新 CLI 进程完成，不重复导入。收尾只向本次 Popen 的 Project/Home 发原生 close，遇其它窗口仅等待完整 20 秒退出期限；不给未知窗口发送确认/关闭消息，不强杀。PVsyst 退出时出现的 TSplashScreen 曾使过早返回，现以真实 Popen 退出为准；持续询问/异常仍保留窗口和停点。

恢复使用相同 `spec`、`resume=true`；执行仍需明确 confirm。每次重建受限路径并检查原有生产输入守卫、已生成场景读回及已保存 VC。坏 JSON 根、坏 schema/request/证据、缺失/变化的资产在写入前停止；只读预览和错误请求不改写检查点。原生返回了文件但尚未通过检查时先人工查看，避免重复导入。已完成任务恢复会重验文件/结果，不再跑 GUI/仿真，即使附带 `retry_simulation=true` 也不增跑。

工作工程最初只创建 VC0。若检查点仍为 `prepared`，但工程已有其他 VC，可能是原生已另存而宿主尚未登记结果就中断。恢复预检会返回 `needs_attention`、`stage=native_save_reconciliation` 及 `unrecorded_native_variants` 的编号和路径，不重开 GUI、不重导入、不改写检查点。先人工核对并明确处置这些候选，不能用重复调用推定它们不存在。此规则仅用于导入前的 `prepared`；已登记变体的 `native_saved` 及后续阶段按原有恢复路径继续。

`status` 是本次调用结论，`phase/stage` 是任务进度，`native_consumption_verified` 单独表示已验证的原生消费。来源住宅也可能在补资料或等待复核时返回 `needs_review`；不能跨入口仅凭该状态判断全年已交付。完成任务的只读预览沿用 `ready` 与消费标志 false，不据此重新运行。

仿真失败保留原始输出和尝试编号。默认恢复只报停点；明确 `retry_simulation=true` 才用下一组新 CSV/PDF 名称继续，既不重建住宅也不覆盖残留。检查点不是认证凭据、进程租约或跨进程事务；中断/不完整元数据和锁外编辑可能需要查看保留产物，不自动删除或回滚用户资料。

## 已测调用示例

下面函数需要已初始化的 MCP `session`，以及验收工作区中明确的 `House_Revised_R46B.pvc2` 与其源 `HOUSE_SCENE.PRJ/VC0`。这是已有 AI 网格的完整读回复用样例；新住宅应由 AI 按用户尺寸/草图提交新的完整 `house/layout`，不把夹具尺寸说成现场测量。

```python
import json

async def deliver_fixture(session, execute=False):
    async def call(name, arguments):
        response = await session.call_tool(name, arguments)
        if response.is_error:
            raise ValueError(response.content)
        return json.loads("\n".join(x.text for x in response.content if hasattr(x, "text")))

    scene = await call("pvsyst_read_house_pv_scene", {"scene_name": "House_Revised_R46B.pvc2"})
    authoring = scene["create_arguments"]
    spec = {
        "source_project": authoring["project"], "source_variant": authoring["variant"],
        "new_project": "ROOF_DELIVERY.PRJ", "variant_name": "Residential roof delivery",
        "year": 2022, "house": authoring["house"], "layout": authoring["layout"],
        "module_geometry": authoring["module_geometry"],
    }
    preview = await call("pvsyst_run_house_task", {"spec": spec})
    if not execute or preview["status"] != "ready":
        return preview
    return await call("pvsyst_run_house_task", {"spec": spec, "dry_run": False, "confirm": True})
```

## 证据与裁决

本次身份/恢复维护执行了既有 `test_native_project_boundary.py` 和 `test_house_task_boundary.py` 各一项公开契约，均通过且无 skip。临时隔离验证确认：相似后缀的错误工程在编辑前停止；`prepared` 中已有未登记 VC 时，预览及显式重试均停止且清单保持；正常完成态预览不被新规则拦截；清单替换失败保留旧记录，首次独占创建不覆盖已有文件。这些验证使用窗口替身、不可执行 CLI 和本机既有导出夹具，不计为新的原生 GUI 或全年验收。

临时脚本首次把错工程的状态预期误写为 `blocked`，实际接口按既有语义返回 `needs_attention` 且没有编辑动作；仅更正脚本预期后继续通过，未为断言修改产品状态。临时文件已清理，未新增永久测试、备份层或哈希探针，未运行全量发现、旧 smoke 或新年度计算。

本机 `pvsyst-house-delivery-study` 保留原始请求、公共响应、失败停点和恢复记录；商业输入/派生产物不上传。以下为受限真实路径，不外推其它版本/拓扑或工程认证：

| 证据 | 结论 | 调用路径 |
|---|---|---|
| `house-task-r2/public-house-task.json` | 首次 20 构件+2 区+40 模块原生校验/保存通过；收尾保护停，未仿真，不算完整成功 | fresh task → `native_saved` |
| `house-task-r2/public-resume.json` | 不再开 GUI，恢复到全年 8760 小时、14 数值列、8 页 PDF；完成态不增跑，改年份/场景拦截且检查点字节保留 | resume → native CLI reload → validation |
| `house-task-r3/public-house-task.json` | 再次原生保存通过，捕获 TSplashScreen；2 秒提前返回保留，不抹掉为首次成功 | fresh task → close evidence |
| `house-task-r4/public-house-task.json` | 完整冷启动一次交付、真实自有进程退出、全年验证及完成态/错误请求恢复通过，父恢复 DataPath，无残留 GUI | fresh task → close grace → CLI → completed |
| `house-task-r5/public-retry.json` | 一次临时命令交接故障注入产生明确标注的非原生残留；默认不重跑，显式 run02 原生全年成功，run01 保留，完成态 retry=true 仍不增跑 | saved checkpoint → injected failure → explicit native retry |

成功的原生年结果均为 2022 区间、8760 连续小时、14 数值列、GlobShd 与 8 页默认 PDF，年并网电量约 32781.5254kWh。同一 17.6kWp 源硬件和已证住宅夹具产生相同数值，是流程复演，不是新增选型/气象或烟囱单因果结论。继承 Hawaii_2020 SIT/MET，运行年份不同不表示新取得了 2022 现场天气。源输入和已保存原生产物保持，父只对自己的基准环境临时选择/恢复 DataPath。

永久白名单仅 `tests/contracts/test_house_task_boundary.py` 的公开 schema/预检/损坏检查点不改写/首输出冲突契约，使用不可执行 CLI 与明确非原生天气占位，不把它算原生天气或 GUI 验收。另执行受影响的原生工程打开公开契约及必要编译检查；未运行全量发现、旧 smoke 或哈希类测试，既有生产冲突保护保留。

指定 SG 首次报告中的相等分支、阶段守卫、变体标题及未知窗口推断与代码相反，父按源码及运行证据裁决，没有照单改错。最终限定复核 `house-task-final-review [magpie/mw/deepseek-v4.1-flash-sg]` 未发现必须修复项；静态审查与上述真实原生证据独立，不互相冒充。
