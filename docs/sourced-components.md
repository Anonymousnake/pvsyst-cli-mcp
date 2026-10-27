# 按来源创建用户自建 PAN/OND

`pvsyst_build_sourced_component` 从完整模板创建新组件，将引用、单位、实际改动和继承项一起返回。它是有来源的模板编辑入口，不是单二极管拟合器，也不是厂家模型认证入口。所有成功预览和创建都保持 `needs_review`。

## 使用顺序

1. 准备完整 UTF-8 PAN/OND 模板。指定精确 `template_name` 和 `source_library`，不会自动搜索或选择相似型号。
2. 提供资料文本、型号适用性和逐字段引文。数值 token、单位 token 与引文必须一致；字段重复、型号冲突、单位错误或未知字段返回具体问题。
3. 以默认 `dry_run=true` 查看 `diff`、`requested_fields`、`inherited_fields`、`inherited_curves` 与 `provenance`。预览不创建目录或文件。
4. 核实模板技术、参考条件、串并联电池数量、电压额定值、温度系数、IAM/效率曲线等继承项后，用相同输入明确设置 `dry_run=false` 和 `acknowledge_inherited_model=true`。
5. 用引用新文件的项目验证原生加载与运行；实际工程还需现场设计包络与硬件限值。原生运行成功不证明低照度/温度响应或厂家拟合正确。

## 字段和单位

| 类型 | 支持字段 | 输入单位 |
|---|---|---|
| PAN | `PNom` | `W`；引文中的 `Wp` 是明确允许的铭牌功率别名，不做倍率换算 |
| PAN | `ISC`、`Imp` | `A`；写入时保留模板的 `ISC/Isc` 拼写 |
| PAN | `Voc`、`Vmp` | `V` |
| OND | `PNomConv`、`PMaxOUT` | `kW` |
| OND | `VMppMin`、`VMPPMax`、`VAbsMax` | `V` |
| OND | `EfficMax`、`EfficEuro` | `%`，例如 `97.8`，不是 `0.978` |
| 8.1.6 商业表单 | `Width`、`Height`、`Depth` | `m` 或 `mm`；毫米明确除以 1000，并记录 `unit_conversions` |
| 8.1.6 商业表单 | `Weight` | `kg` |

所有本入口支持的值必须为正数。还检查 `Vmp < Voc`、`Imp < ISC`、`VMppMin < VMPPMax <= VAbsMax`、`PMaxOUT >= PNomConv`、`EfficEuro <= EfficMax` 和效率百分比上界。

原生序列化温度系数、技术切换、电池数量、逐 MPPT 电流限值、逆变器温度降额和其他隐藏参数没有新增写入支持。引用正确不等于对应表格列、区域版本或物理模型已认证。源文本没有适用型号时会显式提示，不自动选择某个型号列。

## 可重放请求示例

下面是明确的接口演示假设，不是假冒厂家数据手册。工作区应有已验证完整的 `Generic_Mono_440W_Half.PAN`，且其商业表单声明版本为 8.1.6。

```json
{
  "component_type": "PAN",
  "template_name": "Generic_Mono_440W_Half.PAN",
  "new_name": "User_Mono440_35mm.PAN",
  "manufacturer": "User Models",
  "model": "Mono 440W half-cell",
  "sources": {
    "demo": {
      "kind": "assumption",
      "title": "Explicit component interface demonstration",
      "locator": "fixture://mono440-demonstration",
      "model": "Mono 440W half-cell",
      "text": "Mono 440W half-cell\n440Wp\n35mm"
    }
  },
  "declarations": [
    {
      "field": "PNom",
      "value": 440,
      "unit": "W",
      "source_id": "demo",
      "excerpt": "440Wp",
      "source_value_text": "440",
      "source_unit_text": "Wp"
    },
    {
      "field": "Depth",
      "value": 35,
      "unit": "mm",
      "source_id": "demo",
      "excerpt": "35mm",
      "source_value_text": "35",
      "source_unit_text": "mm"
    }
  ],
  "dry_run": true
}
```

实际创建时添加 `acknowledge_inherited_model=true` 并把 `dry_run` 设为 `false`。新组件按类型写入 `ComposPV/PVmodules` 或 `ComposPV/Inverters`，来源记录写入 `Results/<new_name>.sources.json`。该记录描述候选模型及其引用，不单独证明组件已成功创建或运行；以工具返回的 `component_created`、阶段和实际文件为准。

`Manufacturer`、`Model` 和 `DataSource` 被明确标记为用户自建；在 8.1.6 商业表单中同步身份和显示摘要，必要时同步已有根身份字段。新文件保留模板编码标记；候选含非 ASCII 文本时写 UTF-8 BOM 并返回 `utf8_bom`，避免把新型号作为 ANSI 文本解释。

## 继承项与错误处理

- 单二极管参数、温度系数、参考条件、硬件数量、模型标志和曲线没有根据新铭牌自动重新拟合。`inherited_fields` 列出未改变、未被引用赋值覆盖的所属对象字段。
- OND 保留文件曲线及原有自动/文件模式。原生加载可能按该模式再次编译；仅改 `EfficMax/EfficEuro` 不表示本入口生成或缩放了效率曲线。显式曲线编辑继续使用已有 `pvsyst_update_component`，遵守其版本和单位限制。
- PAN 的第五条商业备注可能含功能选项，因此本入口保留原备注，不借机清除功能标志。
- `blocked` 表示输入、模板、引用、目标名称或确认条件未满足；`failed` 表示已进入写入阶段后失败。失败时保留并披露候选来源记录等产物，不静默清除证据；检查报告路径后使用新名字重试。
- 已有目标组件或同名来源记录都会阻止创建，包括大小写不同的同名文件。源模板在写入前重新读取以检查变化，但同一个进程锁不等于跨进程锁；外部 GUI 编辑仍需协调。
- 此入口不读取许可、不获取天气、不自动运行原生 CLI，也不抓取 PDF。页号和资料真实性仍是调用者声明；完整源文本不回传、不写入来源记录，复验需要保留原请求和文档。

## 原生与模型范围

该入口的原生验证覆盖引用新文件的24小时加载／运行，不代表完整全年、厂家曲线认证、GUI回存或低照度／温度多工况。来源引用、模板继承、新组件创建、实际工程引用及原生消费是不同层级；分别检查，保留needs_review。

官方关于铭牌与模型参数区别的说明：[PV modules basic data](https://www.pvsyst.com/help-pvsyst7/pvmodule_basicdata.htm)、[Determination of model parameters](https://www.pvsyst.com/help-pvsyst7/pvmodule_parameters.htm)。这些公开说明解释接口边界，不作为本机隐藏字段或跨版本序列化的证明。
