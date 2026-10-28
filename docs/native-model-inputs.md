# 组件老化（Ageing）与入射角修正（IAM）参数配置

本文档说明 `pvsyst_update_variant_parameters` 中的 `ageing_updates`、`ageing_enabled` 与 `iam_updates`。这三项操作均要求源变体为 PVsyst 8.1.6 rev49278 单子阵列、单固定平面、自由地平线、无 3D 阴影场景、未混开 PF/限发/负荷/储能的纯光伏并网变体，每次更新作为独立事务执行（可附带 `variant_name`）。

## 组件老化参数（`ageing_updates` 与 `ageing_enabled`）

### 1. 修改老化运行年数与年均衰减率

要求源变体中已包含激活状态的完整 `pvAgeing` 模板块（主阵列 `Flags2=$0010082A`、子阵列 `Flags=$019E1002` 且 `Flags2=$20`）：

```json
{
  "ageing_updates": {
    "simulation_year": 10,
    "annual_module_degradation_percent": 0.6
  }
}
```

- `simulation_year`：仿真第几年（整数 `1..30`），对应主阵列 `pvMainArray.NoYearSimul`。
- `annual_module_degradation_percent`：组件年均衰减率（百分比 `0..1`%，步长 `0.01`），对应直属 `pvAgeing.ModuleDegradFactor`。注意：把年均衰减率设为 `0` 仍代表启用老化模型（只是衰减率为 0），不等于关闭老化开关。
- 工具只改写输入参数 `NoYearSimul` 与 `ModuleDegradFactor`，不会去手动算或篡改文件里的缓存损耗因子（`DegradLossFactor` / `DegradMismFactor`），实际衰减因子由 PVsyst 在仿真时重算。
- 可选附加参数：布尔值 `keep_mc_values`（对应 `KeepMC`），以及必须成对传入的蒙特卡洛离散度参数 `modules_rms_year_isc_percent` 与 `modules_rms_year_voc_percent`（范围 `0..100`%，步长 `0.01`）。

### 2. 独立切换老化启停开关（`ageing_enabled`）

如果变体里已经有完整的 `pvAgeing` 块，可以用独立的布尔参数 `ageing_enabled` 切换主阵列 `pvMainArray.Flags2` 的 **bit 20**：
- `ageing_enabled=true`：写入 `Flags2=$0010082A`
- `ageing_enabled=false`：写入 `Flags2=$082A`（保留 `pvAgeing` 块内的参数，读取时 `ageing.enabled` 为 `false`，且停用状态下仍可通过 `pvsyst_get_variant_parameters` 查看块内保存的参数）

## 入射角修正（`iam_updates`）

`iam_updates` 支持在“使用 `.PAN` 组件自带 IAM 定义（`component`）”与“ASHRAE 单参数模型（`ashrae`）”之间切换：

```json
{
  "iam_updates": {
    "mode": "ashrae",
    "ashrae_b0": 0.08
  }
}
```

底层 `.VCx` 修改逻辑：
- **`mode="component"`**：将所属 `pvSubArray` 的组件定义位（bit 5）置位，保留原有的 `pvIAM` 缓存块（即使保留的块里写着 `Ashrae`，只要组件定义位 bit 5 为真，PVsyst 实际生效的依然是 `.PAN` 组件自带的 IAM）。
- **`mode="ashrae"`**：必须同时传入 `ashrae_b0`（范围 `0..1`，步长 `0.001`）。工具会清除子阵列 bit 5，并写入包含 `Flags`、`IAMMode` 和 `B0_IAM` 的 `pvIAM` 块（若源子阵列原本没有 `pvIAM` 块，会自动插在直属 `WiringResistance=TWiringResistance` 块之前）。
- 通过 `pvsyst_get_variant_parameters` 读取时，`subarrays[].iam` 会分别返回当前实际生效的模型（`effective`）、文件里保存的块类型（`stored_model`）以及 `stored_ashrae_b0`。自定义的 `FresnelSmooth` 和 `UserProfile` 折线点表支持只读检查，暂不支持通过本入口写入。
