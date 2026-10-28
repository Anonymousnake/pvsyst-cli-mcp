# 中压（MV）变压器夜间断开配置

在含中压升压变压器的并网项目中，夜间逆变器停机时如果变压器不从电网断开，铁损（空载损耗）会持续消耗电量。`pvsyst_create_mv_night_disconnect_variant` 用于在现有的单台 20 kV 默认 MV 变压器模板上切换夜间断开开关（`NightDisconnect`），并另存为一个新的 `.VCx` 变体。

```json
{
  "project": "example.PRJ",
  "source_variant": "VC0",
  "new_variant": "VC1",
  "night_disconnect": true,
  "dry_run": true
}
```

## 底层修改的字段与标志位

在 PVsyst 8.1.6 中，切换 MV 变压器夜间断开需要同步修改两处：
1. 电路树中 `TransformerNode` 下的布尔字段 `NightDisconnect=True` / `False`。
2. 主阵列 `pvMainArray.Flags` 的 **bit 29**（未勾选夜间断开时为 `$01F00400`，勾选夜间断开时置位 bit 29 变为 `$21F00400`）。

工具只会精确更新所属对象的这两处值（可选同时传入 `variant_name` 和 `add_utf8_bom` 修改新变体显示名称），不会改动其他层级的同名 `Flags`。

## 适用模板条件

源变体必须满足 PVsyst 8.1.6 的单 MV 默认变压器模板结构：
- 单子阵列、单固定平面（朝向 1）、无 3D 阴影/布局、无地平线、无负荷/储能/P50 的纯光伏并网变体（`pvSystem.Flags=$00`，`pvMainArray.Flags2=$082A`）。
- 主阵列包含 `MVNbTransfos=1`、`VOutTransfo=20000`、`MVWireNoSect=2`、线长 `0`、`Copper` 以及 `TrfIronLossFrac=0.00100`、`TrfResLossFrac=0.01000`。
- 电路树包含唯一一个直属主阵列的 `TransformerNode`（`IsHVTransformer=False`，且具备完整的 7 项铁损/铜损/参考功率字段），且修改前其 `NightDisconnect` 布尔值与主阵列 bit 29 状态一致。

如果工程使用了多台变压器、高压（HV）变压器或自定义损耗表，可通过 `pvsyst_get_variant_parameters` 的 `transformers` 字段只读查看（见[保存状态检查](native-state-inspection.md)）。
