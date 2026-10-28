# 逆变器侧均一直流/交流（AC）线缆配置

在 PVsyst 8.1.6 的 `.VCx` 文件里，启用逆变器出口到并网点/变压器前的低压交流（AC）线缆并不是只改一个长度标量，而是需要同时改三处地方：主阵列（`pvMainArray`）的使能位与线缆摘要、子阵列（`pvSubArray`）的有效线长，以及电路树里每个直接 `InverterNode` 下的 `CableDefinition` 块。

`pvsyst_create_ac_cabling_variant` 负责把这三处修改打包在一次操作里，生成一个新的 `.VCx` 变体（同时保持下级 `StringNode` 上的直流 DC 线缆阻值不变）。

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "ac": {
    "length_m": 100,
    "metal": "Copper",
    "section": "ISO60228_10"
  },
  "dry_run": true
}
```

## 参数要求

- `length_m`：线缆长度（米），范围 `(0, 100000]`，精度到 `0.1 m`（传入超出一位小数的值会直接报错而不做静默四舍五入）。
- `metal`：导线材质，支持 `"Copper"` 或 `"Aluminium"`。
- `section`：导线截面规格，支持 `"ISO60228_10"`、`"ISO60228_16"`、`"ISO60228_25"`、`"ISO60228_35"`、`"ISO60228_50"`、`"ISO60228_70"`、`"ISO60228_95"`、`"ISO60228_120"`。
- 如果源变体已经启用了 AC 线缆，只改 `length_m` 时可以省略 `metal` 和 `section`（自动沿用源变体当前材质与截面）；如果源变体尚未启用 AC 线缆且省略了材质/截面，则默认填入 `Copper` / `ISO60228_10`。线阻与损耗由 PVsyst 根据 `CableType` 规格在仿真时自行计算。

## 适用模板条件与底层结构

源变体需为 PVsyst 8.1.6 单子阵列、单固定平面（朝向 1）、无 3D 阴影场景、无中高压变压器（`MVNbTransfos=0` 且无 `TransformerNode`）、无负荷/储能/P50 的纯并网工程（`pvMainArray` 的 `Flags` 为 `$01900400` 或 `$01B00400`，`Flags2=$082A`，`pvSystem.Flags=$00`）：
- **未启用状态（`none`）**：主阵列无 AC 线缆摘要字段，子阵列 `ACWireLength=0.0`，逆变器节点无 AC `CableDefinition`。
- **已启用状态（`active`）**：主阵列置位 bit 21 并带有 `ACWireNoSect=0`、`ACWireLength=0.0`、`ACWireMetal`；子阵列保存实际 `ACWireLength`；每个直接 `InverterNode`（位于 `Children` 之前）包含标准的 8 行 `CableDefinition` / `CableType` 块，且主阵列、子阵列与各逆变器节点的材质和截面一致。

通过 `pvsyst_get_variant_parameters` 读取时，`ac_cabling` 字段会返回当前状态（`none` / `active` / `unsupported`）、长度、材质、截面、逆变器组数以及展开 `NElements` 后的物理逆变器总台数。
