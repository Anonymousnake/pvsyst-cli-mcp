# 跟踪模板全年仿真流程

除了固定倾角模板外，`pvsyst_simulate_template` 还支持基于现有的无限水平轴跟踪（`UnlimTracking`）模板克隆新工程、修改跟踪器几何与控制参数，并调用 `PVsystCLI.exe` 跑完全年逐时仿真。

## 调用示例

传入 `tracking`、`tracking_variant`、`nominal_dc_power_kw` 和 `tracking_power_source` 即可触发跟踪分支（此时不要传固定平面专用的 `tilt`、`azimuth`、`capacity` 或 `losses`）：

```json
{
  "template_project": "TRACKING_INPUTS.PRJ",
  "variant": "VCK",
  "new_project": "TRACKING_DESIGN.PRJ",
  "pan_name": "Generic_Mono_440W_Bifacial.PAN",
  "ond_name": "Generic_60kW.OND",
  "site_name": "SITE.SIT",
  "met_name": "SITE.MET",
  "year": 2024,
  "tracking": {
    "axis_azimuth_deg": 5.1,
    "backtracking": true,
    "irradiance_optimization": true
  },
  "tracking_variant": "VCRUN",
  "nominal_dc_power_kw": 440,
  "tracking_power_source": "Project equipment schedule: 1000 modules rated 440 W each.",
  "report_pages": ["summary", "losses"],
  "dry_run": true
}
```

参数说明：
- `tracking`：要修改的 `UnlimTracking` 参数字典（支持的字段、角度范围与精度规则见[原生跟踪控制](native-tracking-controls.md)）。
- `tracking_variant`：在新工程里生成的新变体 ID（如 `"VCRUN"`，不能与源工程已有变体同名）。
- `nominal_dc_power_kw` 与 `tracking_power_source`：因为跟踪模板可以直接引用 PVsyst 内置加密组件库里的组件（不一定有外部明文 `.PAN` 可供读取 `PNom`），所以必须显式传入直流装机容量（kWp）及其来源说明，供仿真结束后的标准 PR 交叉验算使用。

## 执行流程与组件引用规则

1. **前置检查**：校验源变体是否为 PVsyst 8.1.6 rev49278 的 `UnlimTracking` 结构（且 `PRCorrTemp=False`），检查 `.MET` 文件年份声明、CLI 许可证配额以及输出文件名冲突。
2. **防止同名散文件意外遮蔽（Shadowing）**：在这条继承式跟踪工作流中，`pan_name` 和 `ond_name` 必须与源模板子阵列里引用的型号完全一致，且工作区 `ComposPV` 或配置的 `builtin` 目录里**不能存在同名的明文散文件**——这是为了防止有人在工作区放了一个只有铭牌功率的残缺同名 `.PAN` 文件，把 PVsyst 内置加密库里的完整双面/跟踪组件模型给静默覆盖掉。
3. **克隆与运行**：克隆源工程到 `new_project`，同步 `.SIT`/`.MET`，调用底层跟踪变体构造器生成 `tracking_variant` 并回读核对，最后启动 `PVsystCLI.exe` 跑完全年 8760/8784 小时仿真并导出 CSV 和 PDF（含 `GlobShd` 近阴影辐照列）。

注意：PVsyst 的 `UnlimTracking` 自带二维排间互遮挡计算，哪怕没有建 3D 场景也会算出近阴影损失（PDF 报告摘要上显示 `no Shadings` 只代表没挂独立 3D 场景文件，不代表二维跟踪遮挡没生效）。由于内置加密组件库的参数未在外部静态展开，任务跑完后 `native_consumption_verified` 为 `true`，同时会保留 `status="needs_review"` 和 `component_resolution="native_database_uninspected"` 标记。

## 失败现场保留

如果仿真中途失败，新克隆的工程、`tracking_variant`、JSON 清单和已产生的输出文件都会留在工作区供排查，不会自动删除或覆盖。重试时请先归档或清理失败的新工程，或者换一个新的 `new_project` 名称。
