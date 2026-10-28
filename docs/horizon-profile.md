# 远方地平线轮廓（Far Horizon Profile）配置

`pvsyst_update_variant_parameters` 中的独立事务 `horizon_updates` 用于在 `.VCx` 变体中写入或清除远方地平线遮挡轮廓（`PVObject_Horizon=pvHorizon`）；`pvsyst_get_variant_parameters.horizon` 则用于读取当前保存的地平线点集、标志位与散射衰减缓存因子。

```json
{
  "project": "HORIZON_PROFILE.PRJ",
  "variant": "VC1",
  "expected_sha256": "<current-token>",
  "horizon_updates": {
    "mode": "profile",
    "profile_name": "HORIZON_LEVEL20",
    "points": [[-120, 20], [-40, 20], [40, 20], [120, 20]]
  }
}
```

## 参数要求

- **`mode="profile"`（写入或更新点集）**：
  - `points`：传入 `4..100` 个 `[方位角, 仰角]` 坐标对，步长精度均为 `0.1°`。
  - 角度约定：遵循 PVsyst 内部方位角定义（北半球正南为 `0°`，偏西为正，偏东为负），方位角必须在 `[-180, 180]` 范围内**严格递增**且不重复；仰角在 `[0, 90]` 范围内。
  - `profile_name`：`1..80` UTF-8 字节的单行可打印名称，写入 `pvHorizon` 的 `Comment` 字段（可同批搭配 `variant_name` 修改变体显示标题）。
- **`mode="none"`（清除地平线）**：
  - 仅传 `{"mode": "none"}`，工具会将整个内嵌的 `PVObject_Horizon=pvHorizon` 块从 `.VCx` 中移除（不会删除工作区里独立的 `.HOR` 文件）。

## 底层 100 槽结构与缓存因子处理

在 `.VCx` 内部，地平线保存在 `PVObject_Horizon=pvHorizon` 下的 `HorizonLine=TCubicProfile` 块中，固定分配 100 个点槽（`NPtsMax=100`、`LastCompile=$81`、`Mode=1`、`FracAlbedo=1.000`），前 `NPtsEff` 个槽存实际坐标点，其余槽填零：
- **新建或修改坐标点时**：工具写入 `Flags=$01`，并且**清除**旧的派生散射/反照率因子（`FHHDiffus`、`FHPDiffus`、`FHPAlbedo`），避免坐标变了却还带着旧地平线算出来的散射积分缓存，交由 PVsyst 在仿真时重新计算。
- **仅修改 `profile_name` 而不改坐标点时**：原样保留已有的 `Flags=$03` 及三个派生缓存因子。

适用模板：PVsyst 8.1.6 单子阵列、单固定平面（朝向 1）、无 3D 近阴影场景、无负荷/储能及附加系统控制的纯光伏并网变体。更多格式细节见[季节朝向与地平线维护笔记](plane-horizon-native.md)。
