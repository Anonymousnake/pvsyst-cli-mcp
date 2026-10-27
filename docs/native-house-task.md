# 住宅单任务：原生工程到全年报告

`pvsyst_run_house_task(spec, dry_run=True, confirm=False, resume=False, retry_simulation=False)`组合场景生成、工程打开、住宅原生桥和CLI仿真。物理、遮挡、调度及设备计算由PVsyst执行。固定无阴影的`pvsyst_simulate_design`仍使用自己的支持范围。

## 前提与输入

仅支持Windows、中文PVsyst8.1.6rev49278、DPI96及对应窗口布局。INI DataPath须已匹配MCP工作区，执行前不得有现存PVsyst GUI。即使工程标题匹配，也不接管已有窗口或未保存工作。库不写INI、不强关进程；每工作区使用一个服务器／GUI驱动，RLock仅在进程内有效。

普通源范围为北半球PV-only Grid、单子阵／固定单朝向，无活动场景、Module Layout、负荷、储能、P50/P90或保存地平线。受限双朝向入口另见[工程交付](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance)。PRJ和VC须引用相同工作区SIT/MET，SIT坐标须与场景站点一致。任务不获取天气、不重定地点、不推算厂家限值或设计温度。可先用[源工程准备](design-project-preparation.md)生成精确project/variant，或使用[来源住宅任务](sourced-house-task.md)组合资料绑定与准备。

| spec字段 | 要求 |
|---|---|
| `source_project`, `source_variant` | 精确源PRJ/VC，只读保护 |
| `new_project` | 独立新PRJ名；工程、检查点、SFI、场景和首轮输出均需无冲突 |
| `variant_name` | 1..80可打印UTF-8字节的展示标题；实际VC编号由软件选择 |
| `year` | 明确整数1969..2068；不改写继承天气的来源年份 |
| `house` | kind=mesh的完整具名构件、顶点及零基三角，不由后端固定屋型补建 |
| `layout` | 一个显式放置区域或tables中的两个具名区域；总组件数匹配源方案及支持的硬件／朝向结构 |
| `module_geometry` | 可读PAN元数据优先；数据库引用需明确名称、功率、米制尺寸与厚度 |
| `report_pages` | 可选原生页名；省略时使用软件默认完整PDF，错误或空列表在写入前停止 |

可提交墙体、屋面、挑檐、门窗、烟囱、门廊、附属体量和天窗，不扩展家具或精细五金。网格完整性和原生有效性不等于建筑／工程认证；几何限制见[住宅建模](house-pv-modeling.md)。`plan.source/modules/inherited_parameters/inherited_project_parameters`披露继承设备、数量、损失、角度和项目温度。继承值不是现场依据，文件保持也不证明数据库模型内容完整。

## 执行与阶段

预览不启动或激活GUI，不创建目录、工程或检查点。执行需`dry_run=false AND confirm=true`。遇到blocked、confirmation_required、needs_attention或needs_review时检查issues及phase，不只看MCP的is_error。

| phase | 含义 |
|---|---|
| `initialized` | 请求和源预检完成，建立检查点 |
| `project_created` | 创建工作PRJ/VC0，源保持 |
| `scene_created` | 写出独立PVC2 |
| `prepared` | 准备SFI变量，尚未原生导入 |
| `native_saved` | 自有GUI完成导入、关联、有效性、遮挡表、SaveAs和持久化核对 |
| `gui_closed` | 持有的Popen实际退出，不凭旧PID关闭窗口 |
| `simulated` | 新CLI重新加载已保存VC并产生CSV/PDF |
| `completed` | 上下文、完整年、数值有限性、GlobShd、PDF及输入保持检查通过 |

产物使用`<new-project-stem>_house`前缀：UserData中的PVC2、Models中的SFI、Results中的`_task.json`和`_run01.csv/.pdf`。完整请求存检查点，不在每次响应重复回传网格。原生保存、几何核对和native_consumption_verified独立；只有全年结果通过后才升级消费标志。

同一调用只开一场GUI。收尾只向持有进程的Project/Home发送原生close，等待实际Popen退出；其他窗口只等待20秒，不发送未知确认或强杀。持续询问或异常时保留窗口与停点。

## 恢复

使用相同spec和`resume=true`，实际继续仍需confirm。恢复重建受限路径，检查请求、清单、输入快照、场景及已保存VC；损坏根、schema、请求或资产变化会在写入前停止。预览和错误请求不改检查点。

若阶段仍为prepared而工作工程已有其他VC，可能是原生另存完成但尚未登记。返回needs_attention、stage=native_save_reconciliation及unrecorded_native_variants，不重开GUI、不导入或改检查点；先人工核对候选。此规则不拦截已登记的native_saved及后续恢复。

完成态恢复只重验文件和结果，即使retry_simulation=true也不重跑。失败仿真保留原输出和尝试编号；显式retry_simulation=true才使用下一组新CSV/PDF名，不重建住宅、不覆盖残留。检查点不是认证凭据、进程租约或跨服务器事务；不完整状态和锁外编辑可能需要人工处理，不自动删除或回滚。

status表示调用结论，phase/stage表示进度，native_consumption_verified另表原生消费。needs_review可能是补资料、等待复核或已完成但仍有问题；完成态只读预览的ready和消费false不表示应重新运行。

## 复用已有场景

以下函数需要已初始化的MCP session和工作区内可完整读回的场景。新住宅应按真实尺寸或明确假设提供house/layout，而非照搬样例几何。

```python
import json

async def deliver_scene(session, scene_name, new_project, year, execute=False):
    async def call(name, arguments):
        response = await session.call_tool(name, arguments)
        if response.is_error:
            raise ValueError(response.content)
        return json.loads("\n".join(x.text for x in response.content if hasattr(x, "text")))

    scene = await call("pvsyst_read_house_pv_scene", {"scene_name": scene_name})
    authoring = scene["create_arguments"]
    spec = {
        "source_project": authoring["project"], "source_variant": authoring["variant"],
        "new_project": new_project, "variant_name": "Residential roof delivery",
        "year": year, "house": authoring["house"], "layout": authoring["layout"],
        "module_geometry": authoring["module_geometry"],
    }
    preview = await call("pvsyst_run_house_task", {"spec": spec})
    if not execute or preview["status"] != "ready":
        return preview
    return await call("pvsyst_run_house_task", {"spec": spec, "dry_run": False, "confirm": True})
```
