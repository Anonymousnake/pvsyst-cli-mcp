# 基于规格书引用创建自定义 PAN / OND 组件

当手头只有组件或逆变器的 PDF 规格书参数，还没有现成的 `.PAN` / `.OND` 文件时，可以用 `pvsyst_build_sourced_component` 基于工作区里已有的同类型明文模板派生出一个新的自定义 `.PAN` 或 `.OND` 文件。

工具会严格核对每个修改字段的数值、单位和原文摘录（`excerpt`），并把“本次按规格书改了哪些字段”与“从模板直接继承了哪些底层模型参数和曲线”一五一十地列在返回结果和 `.sources.json` 清单里，所以生成后的状态固定为 `needs_review`，提醒工程师清楚哪些底层参数沿用了模板。

## 操作步骤

1. **选定明文模板**：在工作区或配置库里挑一个技术路线相近（例如同为半片单晶或同拓扑逆变器）、商业表单版本为 `8.1.6` 的完整 UTF-8 `.PAN` 或 `.OND` 作为 `template_name`。
2. **准备规格书摘录与字段声明**：在 `sources` 中提供规格书文本片段和适用型号，在 `declarations` 中逐项指定要覆盖的字段、数值、单位以及对应的原文 token。
3. **预览（`dry_run=true`）**：查看返回的 `diff`、`requested_fields`、`inherited_fields`、`inherited_curves` 和 `provenance`。
4. **确认创建（`dry_run=false` + `acknowledge_inherited_model=true`）**：核对继承的单二极管参数、温度系数或效率曲线无误后，带上 `acknowledge_inherited_model=true` 正式落盘。新组件会写入 `ComposPV/PVmodules` 或 `ComposPV/Inverters`，配套的来源记录写入 `Results/<new_name>.sources.json`。

## 支持修改的字段与单位

| 组件类型 | 字段名 | 允许单位与换算规则 |
|---|---|---|
| `PAN` | `PNom` | `W`（原文里写 `Wp` 也可通过 `source_unit_text="Wp"` 直接匹配） |
| `PAN` | `ISC`, `Imp` | `A`（写盘时自动保留模板原有的 `ISC` 或 `Isc` 大小写拼写） |
| `PAN` | `Voc`, `Vmp` | `V` |
| `OND` | `PNomConv`, `PMaxOUT` | `kW` |
| `OND` | `VMppMin`, `VMPPMax`, `VAbsMax` | `V` |
| `OND` | `EfficMax`, `EfficEuro` | `%`（传百分数如 `97.8`，不要传小数 `0.978`） |
| 通用商业字段（8.1.6） | `Width`, `Height`, `Depth` | `m` 或 `mm`（传 `mm` 时自动除以 `1000` 转为 `.PAN`/`.OND` 内部使用的米，并在 `unit_conversions` 中记录） |
| 通用商业字段（8.1.6） | `Weight` | `kg` |

所有数值必须为正数，且工具会自动校验基本的物理大小关系：`Vmp < Voc`、`Imp < ISC`、`VMppMin < VMPPMax <= VAbsMax`、`PMaxOUT >= PNomConv`、`EfficEuro <= EfficMax < 100`。

## 请求示例

```json
{
  "component_type": "PAN",
  "template_name": "Generic_Mono_440W_Half.PAN",
  "new_name": "User_Mono440_35mm.PAN",
  "manufacturer": "User Models",
  "model": "Mono 440W half-cell",
  "sources": {
    "datasheet": {
      "kind": "assumption",
      "title": "Module datasheet excerpt",
      "locator": "fixture://mono440-datasheet",
      "model": "Mono 440W half-cell",
      "text": "Mono 440W half-cell\nNominal Power: 440Wp\nFrame thickness: 35mm"
    }
  },
  "declarations": [
    {
      "field": "PNom",
      "value": 440,
      "unit": "W",
      "source_id": "datasheet",
      "excerpt": "Nominal Power: 440Wp",
      "source_value_text": "440",
      "source_unit_text": "Wp"
    },
    {
      "field": "Depth",
      "value": 35,
      "unit": "mm",
      "source_id": "datasheet",
      "excerpt": "Frame thickness: 35mm",
      "source_value_text": "35",
      "source_unit_text": "mm"
    }
  ],
  "dry_run": true
}
```

## 模板继承与注意事项

- **哪些参数是继承自模板的**：`.PAN` 的单二极管模型内部参数（`RShunt`、`RSerie`、`Gamma`）、温度系数、串联电池片数、IAM 曲线，以及 `.OND` 的效率曲线（`ProfilPIO`）和温度降额参数不会随着铭牌功率或电压的修改而自动重新拟合。`inherited_fields` 和 `inherited_curves` 会把这些沿用自模板的项明确列出来。如果需要改效率曲线，创建完成后可接着调 `pvsyst_update_component`（见[OND 效率曲线编辑](ond-curve-verification.md)）。
- **商业标识与编码**：新组件的 `Manufacturer`、`Model`、`DataSource` 及根级 `Comment` 头部会同步更新（若包含中文等非 ASCII 字符会自动写入 UTF-8 BOM）。模板原有的 `Remarks` 备注（包括 `.PAN` 第 5 行可能携带的功能标志）会原样保留。
- 关于 PVsyst 中组件铭牌参数与单二极管模型参数的区别，可参考官方文档：[PV modules basic data](https://www.pvsyst.com/help-pvsyst7/pvmodule_basicdata.htm) 与 [Determination of model parameters](https://www.pvsyst.com/help-pvsyst7/pvmodule_parameters.htm)。
