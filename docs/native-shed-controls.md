# 原生棚架与遮阳板配置

`pvsyst_create_shed_geometry_variant`只从现存原生8.1.6 `Sheds`或`SunShields`记录预览/创建新VC。复用批准参考51148b9的字段映射及主线模板门禁，不转换类型、不新增3D、Module Layout或独立阴影模型。配置返回`native_consumption_verified=false`，写入成功与原生运行分开验收。

## 输入与边界

```json
{"project":"example.PRJ","source_variant":"VC0","new_variant":"VC2","geometry":{"pitch_m":6.5,"field_tilt_deg":15.1},"dry_run":true}
```

至少显式声明一个值，未声明字段保持继承。角度为度、长度为米，factor为存储比例；没有从棚架几何换算电路硬件、模块或电气分组。

| 公共键 | 原生字段 | 适配器支持域与存储精度 |
|---|---|---|
| field_tilt_deg | FieldTilt | 0..90，0.1 |
| field_azimuth_deg | FieldAzim | -180..180，0.1 |
| pitch_m | Pitch | 正数，0.001 |
| collector_width_m | CollectorWidth | 正数，0.001 |
| inactive_band_width_1_m/2_m | InactiveBandWidth1/2 | 非负，0.001，合计小于CollectorWidth |
| cell_width_m | CellWidth | 正数，0.001 |
| shading_factor | FracShadingFactor | 0..1，0.001 |
| shed_count | NSheds | 严格整数1..2147483647 |
| module_columns | NModInWidth | 严格整数1..2147483647 |
| electrical_effect | UsesElectricalEffect | 严格布尔，原记录必须已经拥有 |

这些是明确的适配器合理性域，不是已测GUI极限、厂商设计边界或安装建议。源与候选使用同一精度/域，`15.123°`、`0.0001m`及巨大Decimal量化溢出会在写入前停止，不做舍入或微小非零值归零。只有原生软件计算二维自遮挡和电气效应，不按字段名推导替代公式。

## 完整模板门禁

复用`pvsyst_tracking._require_scope`，source与candidate均执行：root8.1.6/Flags=0、Grid系统Flags=$06000000、main Flags=$01900400/Flags2=$082A、subarray8.1.6/$019E1002/$20，单子阵/单朝向1、所有串归属该朝向、完整一致电路计数，以及无负荷/储能/P50/保存地平线/显式场景/Module Layout。不是只看朝向块Version就写任意父模型。

两种类型各自必须为已证20字段完整唯一的直接pvOrient，字段不出现在其它owner；NoOrient=1、Flags=$00、三个Aver=False、BTNoTrackers=-1,-1,。同一次解析内判owner，保留实际`End of TOrientGroup`，不扩大公共parser alias。源值先完整验证，坏源值不会被新值掩盖；候选再次通过相同门禁、类型检查和请求token逐项回读。

默认预览无写；执行只独占创建一个新VC。复用工作区锁、路径/大小写同名冲突检查、源快照复核、显示名与显式BOM规则，保留未请求记录、硬件、气象及源PRJ/VC。失败保留并披露可能的部分产物，不自动删除现场。公共getter返回11字段的原始值，可以从新VC再创建下一份修改。

## R54 验证

新增长期公共协议契约`tests/contracts/test_shed_roundtrip.py`。显式`PVSYST_CONTRACT_WORKSPACE`指向原生文本夹具，再复制到系统临时目录并使用非可执行CLI占位；真stdio调用验证预览无写、Sheds创建/回读/再创建、SunShields原字段开关、错误版本/坏源精度/舍入/带宽关系/计数/目标覆盖停止及源字节保持。不是内部函数测试，也不包含原生商业样本。

原生8.1.6八组均由公共配置入口新建，再独立跑2022-01-01..02；匹配R31原同类型/气象年度输出的前48h作为基线。每组48连续有限小时，无原生警告；原PRJ/VC与新输入字节保持。原生报告7页，电气True为8页；一般17数值列，电气True为18列。不是把48h外推全年，原R31两种基线8760h记录仍保留。

| 新配置（其它参数保持源值） | 原生E_Grid kWh | 相对原同类型基线变化小时 |
|---|---:|---:|
| Sheds Pitch6.5 | 4703.617 | 22 |
| Sheds FieldTilt15.1 | 4521.652 | 22 |
| Sheds Electrical=True | 4691.227 | 4 |
| SunShields Pitch6.5 | 4608.091 | 22 |
| SunShields FieldTilt60 | 4596.6403 | 22 |
| SunShields Electrical=True | 4596.0219 | 0 |
| Sheds NSheds10 | 4689.958 | 22 |
| Sheds CollectorWidth4 | 4640.702 | 22 |

Sheds基线4694.026kWh，SunShields基线4596.0219kWh。SunShields电气True在此样本下并网输出相同，但报告页与导出列有变化；不写成开关普遍无效。InactiveBands/CellWidth/FracShadingFactor及NModInWidth的任意组合、所有几何/电气耦合与GUI边界没有由八组结果覆盖，不宣称全部场景消费或工程认证。

首轮临时验证器在seed工程创建后因证据目录未创建而写日志失败，尚未调用原生仿真。保留`native-migration-r54/verifier-initial-failure.json`；按UTF8-sig解码文本逐项核对seed PRJ/VC0/VC1与原R31输入后复用，不覆盖或重复创建。实际八次原生结果为`native-migration-r54/native-shed-48h.json`。源码复核阶段纠正了重复解析导致的对象身份误判及共享Decimal函数的参数类型，并去掉重复渲染/候选校验。独立只读审查通过；长期契约、定向编译和补丁检查通过，未跑全仓发现、旧smoke或哈希类测试。重启常驻MCP读取新增工具/schema。
