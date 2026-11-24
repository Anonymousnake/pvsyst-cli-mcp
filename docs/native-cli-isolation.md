# 原生 CLI 私有非输入桌面隔离

Windows 上所有对原版 `PVsystCLI.exe` 的调用——包括正常的 `help` / 版本 / 项目列表等非仿真命令——都经过同一个入口 `native/cli.PVsystCLI._run`（`src/pvsyst_auto/native/cli.py`）。该入口以原适配器相同的参数数组、`cwd=cli.parent`、显式环境、输出解码与退出码语义启动子进程，并在 Windows 上把子进程放进每次调用独立的私有非输入桌面，而不是继承调用方的交互桌面。非 Windows 平台保持原生 `subprocess.run` 行为不变。`run_simulation`、`convert_meteo`、`create_site`、`command_help`、`license_info` 等全部复用这一入口，没有另起模拟器、没有新增 API、工具或桌面选择参数。

## 普通 CSV 数值仿真拒建窗

仅在普通 CSV 数值仿真（`run-simulation` 带 `-ocf:`，且无 PDF、批处理或 `-rst:1` 阴影重算）时，默认禁止建窗。阴影重算默认在私有非交互桌面上允许建窗；若显式同时请求重算与 `allow_window_creation=false`，CLI 启动前就会报错。

该分支在新建私有桌面上，用 `SetUserObjectSecurity` 只对该用户 SID 拒绝 `DESKTOP_CREATEWINDOW(0x2)`（其余访问保持完整），再启动子进程。因此单纯的整点数值计算不依赖创建任何顶层窗口；若拒绝步骤失败，显式抛错、**不静默回退**到 Default 桌面（与非 Windows 的原路径语义一致）。这依赖标准的 Windows 原生 ACL，不经 hook、patch 或注入。

**PDF / 批处理 / `help` / 版本 / 许可 / 其它 CLI 命令不进入该分支**，维持原有私有桌面行为（允许建窗）。PDF 仍走原私有图形分支，图形库依旧必需；这一分支可能在私有桌面上创建运行时辅助窗口（见下节）。

## 窗口站取自当前进程，不再硬编码 WinSta0

私有桌面的父窗口站名通过 `GetProcessWindowStation` + `GetUserObjectInformation(UOI_NAME)` 读取**当前进程所在窗口站**，再用它拼 `lpDesktop`，`CreateDesktopW` 自然落在当前站；不再硬编码 `WinSta0`。借用的窗口站句柄不关闭，全程**不** `SetProcessWindowStation`、不切换父进程窗口站/桌面。这样运行在非交互服务站内的服务端，能在**本服务站**上正确创建子桌面；`WinSta0` 下旧行为保持一致。句柄继承白名单（3 个 stdio）/ Job 悬挂 `AssignResume`/ 输出超时 partial 捕获 / UUID 并发 / 非 Windows 旧 `run` 均不变。

## 与既有结果的关系

CSV / PDF 仍由原 PVsyst 引擎产出，`pvsyst_run_simulation` 等工具的返回结构、`diagnostics`（超时/失败时的部分输出、经 `_redact` 脱敏）和现有工具 / Schema 均不变。私有桌面只是启动层隔离，不改变任何计算结果或字段。

## 关键区别（不要混淆）

私有桌面 ≠ 零窗口（zero HWND）。**普通 CSV 拒建窗分支**下，自有桌面顶层窗口为 0，且同一 DACL 会使 `CreateWindowEx(HWND_MESSAGE)` 返回访问被拒（err5）；但 `EnumDesktopWindows` 不枚举 message-only 窗口，因此“枚举不到”只代表观测不到，不等于证明不存在。**PDF 私有图形分支**（允许建窗）此前观测到仿真流程自身创建若干运行时辅助窗口（`TPUtilWindow`、`madToolsMsgHandlerWindow$…`、`GDI+ Hook Window Class`、`IME` 等，均不可见）；它们属于该分支的运行时辅助，不是交互式 GUI，也不能据此说普通 CSV 分支同样会建窗。

`CreateDesktopW` 的访问权限不含 `SWITCHDESKTOP`：整个调用全程不切换父进程的窗口站/桌面，也不做进程级全局 monkeypatch。

## 失败与回退

如果当前操作系统环境无法建立隔离桌面、拒绝建窗或隔离启动失败，工具应显式报错，不静默回退到用户 Default 桌面。非 Windows 平台维持原 `subprocess.run` 路径，不套用私有桌面逻辑。

## CLI 工作区设置

对 PVsystCLI 8.1.6 rev49278 和 8.1.7 rev49382，适配器按 Windows 用户配置文件串行启动 CLI，并在进程退出后仅恢复 `DataPath` 和 `WorkspaceN` 最近列表。运行计数等其他 INI 内容保留。此保护不协调独立启动的 PVsystCLI，也不能在 MCP 宿主被强制终止后恢复设置。

## 验证范围

`tests/e2e/test_mcp_full_workflows_e2e.py` 提供了需要连接本机 `PVsystCLI.exe` 的可选端到端测试（默认不随离线 CI 运行）。私有桌面隔离在纯 CSV 模式下禁止 `DESKTOP_CREATEWINDOW` 并保持父进程桌面不变；带 PDF 渲染的场景仍依赖正常桌面图形接口。
