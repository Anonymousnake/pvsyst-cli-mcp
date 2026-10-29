# 模板驱动的全年仿真

`pvsyst_simulate_template` 将“克隆模板工程 → 绑定站点与气象（`.SIT`/`.MET`） → 替换组件与逆变器（`.PAN`/`.OND`） → 修改倾角与容量 → 调用 `PVsystCLI.exe` 跑全年逐时仿真 → 校验 CSV/PDF 输出”串成一步到位的自动化调用。

- 如果想连组件检索、模板自动匹配和在线天气下载也一起包办，见更高层的 [`pvsyst_simulate_design`](single-design-inputs.md)。
- 如果模板是无限水平轴跟踪器（`UnlimTracking`），同一工具会自动走跟踪分支，详见[跟踪模板流程](tracking-template-flow.md)。
- Windows 下这里所有 `PVsystCLI.exe` 调用都经 `native/cli.PVsystCLI._run` 在私有非输入桌面隔离启动（不改计算结果），机制与验收状态见[原生 CLI 私有非输入桌面隔离](native-cli-isolation.md)。

## 固定倾角模式入参

| 参数名 | 说明与要求 |
|---|---|
| `template_project` / `variant` | 工作区中作为底板的 `.PRJ` 与 `.VCx`（要求单子阵列、单固定平面、无 3D 阴影场景或 Module Layout、根级 `PRCorrTemp=False`） |
| `new_project` | 新生成的 `.PRJ` 文件名（工程名、清单名、`.SFI` 及输出 CSV/PDF 均不能与工作区现有文件重名） |
| `pan_name` / `ond_name` | 完整的明文 `.PAN`（要求 `GRef=1000 W/m²`）与 `.OND` 文件名 |
| `site_name` / `met_name` | 工作区 `Sites` 与 `Meteo` 下的 `.SIT` 和 `.MET` 文件名 |
| `tilt` | 固定平面倾角，范围 `0..90` 度 |
| `year` | 仿真日历年份（整数 `1969..2068`），必须与 `.MET` 文件的起止日期匹配 |
| `dry_run` | 布尔值，默认 `true`（仅做只读预检，不创建文件也不调 CLI） |
| `capacity` | 可选的整数串并联容量规划参数，详见[容量规划](capacity-sizing.md)；不传则直接沿用模板原有的逆变器与组串数量 |
| `weather_manifest_name` | 可选，`pvsyst_prepare_weather` 生成的天气清单文件名；传入后会开启源 CSV 与仿真输出辐射/温度的逐时比对 |
| `report_pages` | 可选，指定 PDF 报告要输出的页面名称列表；不传或传 `null` 则输出默认完整报告 |

先用 `dry_run=true` 跑一次预检，确认返回的 `issues` 为空后再把 `dry_run` 设为 `false`：

```json
{
  "template_project": "Template.PRJ",
  "variant": "VC0",
  "new_project": "RoofAnnual.PRJ",
  "pan_name": "ValidatedModule.PAN",
  "ond_name": "ValidatedInverter.OND",
  "site_name": "Site.SIT",
  "met_name": "Weather.MET",
  "tilt": 28,
  "year": 2024,
  "dry_run": true
}
```

## 状态流转与全年输出校验

- `blocked`：参数缺项、模板结构不符、`.MET` 年份对不上或目标文件重名。
- `ready`：`dry_run=true` 预检通过。
- `completed`：全年仿真跑完，且 CSV/PDF 结果校验全部通过。
- `needs_review`：仿真跑完了，但触发了复核提示（例如 CLI 控制台打出了 `warning`/`caution`、天气转换有微小舍入偏差、或 `IL_Pmax` 限幅损失异常偏高）。
- `failed`：写文件或 CLI 运行中途报错，可查看返回的 `stage`、`error` 和保留在工作区的现场文件。

仿真结束后，工具会对导出的 CSV 和 PDF 做一轮严格体检：
1. **上下文与时间轴**：CSV 头部记录的工程、变体、`.SIT`/`.MET` 必须与请求一致；时间轴必须恰好覆盖目标年份 `00:00..23:00` 的全部 8760（平年）或 8784（闰年）个整点小时，缺行、重复或乱序直接判失败。
2. **标准并网 PR 核算**：读取 `E_Grid`、`EArray`、`IL_Pmax`、`GlobInc` 的单位与全量有限值，并用全年的 $\sum E_{\text{Grid}} / (\sum \text{GlobInc} \times P_{\text{nom,DC}} / G_{\text{ref}})$ 交叉核对系统 PR（不能拿每小时 PR 的算术平均值当年度 PR）。
3. **逆变器限幅异常提示**：如果全年的 `IL_Pmax`（逆变器超功率削峰损失）超过了特定启发式阈值，会把任务标为 `needs_review` 提醒检查容配比。

## 天气与报告

### 绑定天气清单核对

如果传入了 `weather_manifest_name`，工具在写工程文件前会先校验源 CSV、归一化 CSV、月值 CSV、`.MEF`、`.SIT`、`.MET` 的文件哈希与时基声明；仿真跑完后，还会把导出 CSV 里的 `GlobHor`、`DiffHor`、`T_Amb` 与准备阶段的 CSV 逐小时比对。辐射差超过 `0.1 W/m²` 或温度差超过 `0.01 °C` 会记入 `needs_review` 提示。

### 原生 PDF 报告页选择（`report_pages`）

`report_pages` 直接映射给 `PVsystCLI.exe` 的 `-rp` 参数，由 PVsyst 原生渲染指定页面（不是生成全量 PDF 后在外部用脚本删页）。支持的页面标识如下：

```text
cover summary notes params horizon shadings usersneeds results econeval circuit losses financialbalance p50p90 carbonbalance predefgraphs
```

注意：页面标识合法只代表 CLI 接受该参数，实际 PDF 里能不能打出那一页取决于变体本身是否启用了对应模块（比如没配地平线或没激活 P50/P90 时，选了 `horizon` 或 `p50p90` 也不会凭空多出一页）。

## 失败保留与回滚

模板工程本身全程只读。执行任务时会先在 `Results` 下创建独占的任务清单 JSON，并在克隆出的新工程上分步修改。万一中途失败，已生成的工作工程、备份、`.SFI` 和日志都会保留在磁盘上方便排查；清单里的 `recovery` 字段按逆序列出了可直接传给 `pvsyst_restore_variant` 和 `pvsyst_restore_project_sources` 的回滚参数。
