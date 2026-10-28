# 辅助耗电（Auxiliaries）常数模式配置

`pvsyst_update_variant_parameters` 中的 `auxiliary_updates` 用于配置电站的辅助耗电（如空调、监控、照明等自用电损耗）；`pvsyst_get_variant_parameters` 则在 `auxiliaries` 字段中返回当前配置。

本文档介绍基础的 `constant`（固定功率）与 `none`（关闭）模式；如果需要配置带输出功率阈值或按发电功率比例计算的辅助耗电，见[详细损失参数中的 `profile` 模式](native-detailed-losses.md)。

```json
{
  "auxiliary_updates": {
    "mode": "constant",
    "operating_kw": 5,
    "night_kw": 2
  }
}
```

## 模式与底层字段映射

- **`mode="constant"`**：必须同时传入非负的 `operating_kw`（逆变器运行期间的辅助功率）和 `night_kw`（夜间/待机期间的辅助功率），步长精度均为 `0.01 kW`。
- **`mode="none"`**：关闭辅助耗电，清除使能标志位并从 `pvSystem` 中移除辅助功率字段。

底层 `.VCx` 文件的 `pvSystem` 块映射关系：

| `.VCx` 字段 / 标志位 | 说明 |
|---|---|
| `pvSystem.Flags` bit 18 (`$00040000`) | 辅助耗电总使能位；`constant` 模式置位，`none` 模式清除 |
| `AuxPower_Const` | 运行期常数辅助功率，**文件内存储单位为 W**（入参 `operating_kw` 单位为 `kW`，工具自动乘 1000） |
| `AuxPower_Night` | 夜间/待机辅助功率，**文件内存储单位为 W**（入参 `night_kw` 单位为 `kW`，工具自动乘 1000） |

注意：如果在 `constant` 模式下传入 `operating_kw=0` 且 `night_kw=0`，工具会保留 bit 18 使能位（与 PVsyst GUI 勾选启用但填 0 的状态一致）；若要彻底关闭请传 `mode="none"`。

## 适用范围与仿真结果表现

- 适用范围：PVsyst 8.1.6 单子阵列、单固定平面、无 3D 阴影场景、未混开 PF/限发/负荷/储能的纯光伏并网变体。
- **夜间负上网电量（`E_Grid < 0`）**：当配置了 `night_kw > 0` 或低辐照时段阵列输出不足以覆盖辅助耗电时，PVsyst 在逐时 CSV 中导出的并网电量 `E_Grid` 会出现负值（代表从电网倒送供电给辅助设备）。结果读取与汇总工具会原样保留这些负值，不会把它们截断成 0。
