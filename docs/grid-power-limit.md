# 原生注入点有功功率上限

`pvsyst_update_variant_parameters.grid_limit_updates`设置PVsyst8.1.6的pvSystem数值限值。适配器不截断功率、不重算损失或执行储能调度；PF、kVA、P50、温度及文件限值使用各自入口。

## 调用

先准备工作方案，并用`pvsyst_get_variant_parameters`取得当前并发令牌，再预览：

```json
{
  "project": "WORKING.PRJ",
  "variant": "VC1",
  "expected_sha256": "<current-token>",
  "grid_limit_updates": {"mode": "constant", "limit_kw": 250},
  "dry_run": true
}
```

预览后使用同一未变化源的当前令牌设置dry_run=false。limit_kw须为正有限数、0.1kW精度；示例不是工程建议。mode=none不带limit_kw，关闭限制并移除限值和单列损失标量，但保留未使能的位置位。

这是独立事务，可附带显示名称，不混入角度、容量、积灰、模式或储能更新。预览、同值无操作、备份、恢复及并发保护沿用既有机制，不修改源项目、设备、天气、损失或数量。

## 支持范围

仅支持8.1.6 PV-only Grid、一个无活动阴影／布局的普通固定平面和子阵，以及受支持扁平系统。PF、负荷／储能、其他系统标志、未知或非所属字段会在写入前停止，不删除或忽略它们。读取的grid_limit状态不保证任意场景可写。

原生有功、注入点、单列损失组合保存为：

```text
Flags=$05000000
SystemType=Grid
PLimGrid=250.0
LimGrSeparLoss=True
```

逆变器端组合为$01000000；关闭注入点后为$04000000且两个标量消失，说明使能位与位置位不同。系统bit27表示允许／禁止注入，不是数值限值或网充；此独立入口不接受该模式。

## 结果与扩展

`pvsyst_build_sfi(variables="grid_limit")`加入原生PGridLim、EGrdLim、EUnused、EOutInv。PGridLim是限值，不是传输电量；启用单列损失时EOutInv不代表最终受限并网功率，应检查E_Grid。关闭模式可省略限值列，不补列或修正能量。

注入点与逆变器端在存在下游AC损耗时可有不同结果，不从无AC损耗场景推出两者等价。其他功率量、位置和PF组合见[原生功率控制](native-power-controls.md)，不因读取或共有字段扩大本入口范围。
