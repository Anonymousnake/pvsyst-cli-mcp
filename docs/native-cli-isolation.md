# 原生 CLI 私有非输入桌面隔离

Windows 上所有对原版 `PVsystCLI.exe` 的调用——包括正常的 `help` / 版本 / 项目列表等非仿真命令——都经过同一个入口 `native/cli.PVsystCLI._run`（`src/pvsyst_auto/native/cli.py`）。该入口以原适配器相同的参数数组、`cwd=cli.parent`、显式环境、输出解码与退出码语义启动子进程，并在 Windows 上把子进程放进每次调用独立的私有非输入桌面，而不是继承调用方的交互桌面。非 Windows 平台保持原生 `subprocess.run` 行为不变。`run_simulation`、`convert_meteo`、`create_site`、`command_help`、`license_info` 等全部复用这一入口，没有另起模拟器、没有新增 API、工具或桌面选择参数。

## 与既有结果的关系

CSV / PDF 仍由原 PVsyst 引擎产出，`pvsyst_run_simulation` 等工具的返回结构、`diagnostics`（超时/失败时的部分输出、经 `_redact` 脱敏）和 82 个工具/Schema 均不变。私有桌面只是启动层隔离，不改变任何计算结果或字段。

## 关键区别（不要混淆）

私有桌面 ≠ 零窗口（zero HWND）。已观测到仿真流程自身会在私有桌面上创建 4 个运行时辅助窗口（`TPUtilWindow`、`madToolsMsgHandlerWindow$…`、`GDI+ Hook Window Class`、`IME`，均不可见）；它们属于运行时辅助，不是交互式 GUI。另需注意 `EnumDesktopWindows` 不枚举 message-only（`HWND_MESSAGE`）窗口，因此“默认桌面枚举不到自有窗口”只代表观测不到，不等于证明不存在。

`CreateDesktopW` 的访问权限不含 `SWITCHDESKTOP`：整个调用全程不切换父进程的窗口站/桌面，也不做进程级全局 monkeypatch。

## 失败与回退

如果当前操作系统环境无法建立隔离桌面或隔离启动失败，工具应显式报错，不静默回退到用户 Default 桌面。非 Windows 平台维持原 `subprocess.run` 路径，不套用私有桌面逻辑。

## 验收状态（如实）

已在隔离验证环境中证明：原装 `PVsystCLI.exe` 在本用户 `WinSta0` 私有桌面冷启动可正常跑完一次 `run-simulation`（48h，exit=0，CSV 数值与对照源一致），默认桌面无本进程自有窗口，窗口/进程/Job/桌面均回收（证据见 `pvsyst-local-notes/headless-reverse/windows/noninteractive-r2/`）。

这**只**证明本机本用户、本模板的一次运行无需 user input desktop；不证明注销登录、Session 0 或跨用户可用，也不能据此宣称“零窗口”。产品级私有桌面隔离的原生验收尚未完成——在该产品原生验收通过前，私有桌面只能按“设计已定、待验收”陈述，不得对外宣称已成功支持。公开的外部契约是 `tests/contracts/test_native_cli_launch_e2e.py`（默认跳过，需显式真实原生 fixture 才运行）；OS 生命周期细节由一次性私有验证器覆盖，不进入永久测试。
