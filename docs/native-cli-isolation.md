# 原生 CLI 私有非输入桌面隔离

Windows 上所有对原版 `PVsystCLI.exe` 的调用——包括正常的 `help` / 版本 / 项目列表等非仿真命令——都经过同一个入口 `native/cli.PVsystCLI._run`（`src/pvsyst_auto/native/cli.py`）。该入口以原适配器相同的参数数组、`cwd=cli.parent`、显式环境、输出解码与退出码语义启动子进程，并在 Windows 上把子进程放进每次调用独立的私有非输入桌面，而不是继承调用方的交互桌面。非 Windows 平台保持原生 `subprocess.run` 行为不变。`run_simulation`、`convert_meteo`、`create_site`、`command_help`、`license_info` 等全部复用这一入口，没有另起模拟器、没有新增 API、工具或桌面选择参数。

## 普通 CSV 数值仿真拒建窗（本分支新增）

仅当一次调用被判定为**普通整点 CSV 数值仿真**——命令为 `run-simulation`、带 `-ocf:`（CSV 目标）且**不含** `-rpf:`（PDF）、`-bpf:`（批处理参数）、`-brf:`（批处理 RVT）——才以“拒建窗”方式启动。判定在 `native/cli._is_plain_csv_run` 一处完成。

该分支在自建私有桌面上，用 `SetUserObjectSecurity` 只对该用户 SID 拒绝 `DESKTOP_CREATEWINDOW(0x2)`（其余访问保持完整），再启动子进程。因此单纯的整点数值计算不依赖创建任何顶层窗口；若拒绝步骤失败，显式抛错、**不静默回退**到 Default 桌面（与非 Windows 的原路径语义一致）。这依赖一个已证的原生 OS 原语：标准 ACL，不经 hook / patch / 注入。

**PDF / 批处理 / `help` / 版本 / 许可 / 其它 CLI 命令不进入该分支**，维持原有私有桌面行为（允许建窗）。PDF 仍走原私有图形分支，图形库依旧必需；这一分支可能在私有桌面上创建运行时辅助窗口（见下节）。

## 窗口站取自当前进程，不再硬编码 WinSta0

私有桌面的父窗口站名通过 `GetProcessWindowStation` + `GetUserObjectInformation(UOI_NAME)` 读取**当前进程所在窗口站**，再用它拼 `lpDesktop`，`CreateDesktopW` 自然落在当前站；不再硬编码 `WinSta0`。借用的窗口站句柄不关闭，全程**不** `SetProcessWindowStation`、不切换父进程窗口站/桌面。这样运行在非交互服务站内的服务端，能在**本服务站**上正确创建子桌面；`WinSta0` 下旧行为保持一致。句柄继承白名单（3 个 stdio）/ Job 悬挂 `AssignResume`/ 输出超时 partial 捕获 / UUID 并发 / 非 Windows 旧 `run` 均不变。

## 与既有结果的关系

CSV / PDF 仍由原 PVsyst 引擎产出，`pvsyst_run_simulation` 等工具的返回结构、`diagnostics`（超时/失败时的部分输出、经 `_redact` 脱敏）和 82 个工具 / Schema 均不变。私有桌面只是启动层隔离，不改变任何计算结果或字段。

## 关键区别（不要混淆）

私有桌面 ≠ 零窗口（zero HWND）。**普通 CSV 拒建窗分支**下，自有桌面顶层窗口为 0，且同一 DACL 会使 `CreateWindowEx(HWND_MESSAGE)` 返回访问被拒（err5）；但 `EnumDesktopWindows` 不枚举 message-only 窗口，因此“枚举不到”只代表观测不到，不等于证明不存在。**PDF 私有图形分支**（允许建窗）此前观测到仿真流程自身创建若干运行时辅助窗口（`TPUtilWindow`、`madToolsMsgHandlerWindow$…`、`GDI+ Hook Window Class`、`IME` 等，均不可见）；它们属于该分支的运行时辅助，不是交互式 GUI，也不能据此说普通 CSV 分支同样会建窗。

`CreateDesktopW` 的访问权限不含 `SWITCHDESKTOP`：整个调用全程不切换父进程的窗口站/桌面，也不做进程级全局 monkeypatch。

## 失败与回退

如果当前操作系统环境无法建立隔离桌面、拒绝建窗或隔离启动失败，工具应显式报错，不静默回退到用户 Default 桌面。非 Windows 平台维持原 `subprocess.run` 路径，不套用私有桌面逻辑。

## 验收状态（如实）

OS 原语层面已由两臂私有实验（`pvsyst-local-notes/headless-reverse/windows/native-window-boundary-r1/`）证明：**D**——`WinSta0` 自有私有桌面、只拒 `DESKTOP_CREATEWINDOW` 时，原装 `PVsystCLI.exe 8.1.7` 仍跑完 48h，exit=0、约 7.4s，CSV 数值正确（E_Grid 149.8151 / ShdElec 13.1254 / 48 行），自有顶层窗=0；**N**——既有非交互服务站（`WSF_VISIBLE=false`）的自有私有桌面上，原 CLI 跑完 48h，exit=0、约 12.7s，数值同 D，观测到 3 个运行时顶层窗，父进程窗口站/桌面不变。二者互补地表明：**本 profile** 的核心数值计算不需建窗（D）、不需交互式窗口站（N）。

这**只**是 OS 原语与单 profile 的证据：不证明全局 zeroHWND、不证明 Session 0 / 注销登录 / 跨用户，也不外推到多朝向/多子阵/异构组件等其它模型。产品级 plainCSV 拒建窗接线现已在 **PVsystCLI 8.1.7、本机同一用户、两站** 以真实 public 请求实证（`pvsyst-local-notes/headless-reverse/validation/windowless-product-r1/`）：**WinSta0** 以长期契约 exact 方法实跑 1 次，**既有非交互服务站**（`Service-0x0-104827$`，`WSF_VISIBLE=false`）以真实 public MCP 调用实跑 1 次；两站均产出 48 行连续有限整点 CSV（E_Grid 149.8151 / ShdElec 13.1254 / 14 非零小时），对完整 reference CSV 逐时逐列 max|Δ|=0，返回 `pdf`/`batch_*` 为空、`seconds` 正常；Service 站完整 census 观测其拒建窗桌面顶层窗=0、DACL 为仅当前 SID 拒 `DESKTOP_CREATEWINDOW`，Normal 走同一 `allow=False` 路径/同一 DACL 机械禁止建窗（其真实 child census 未单独枚举，见下）。**边界（如实，保留）**：Normal 该次真实 child 的顶层 / message-only 窗口 census 未单独枚举（结论依托同一 deny DACL + 0-native 校准 err5 及 Service 完整 census，而非对该 child 枚举声称 0）；`EnumDesktopWindows` 不枚举 message-only；“CLI 自建私有桌再建窗”的理论逃逸未扫；PDF 私有图形分支仍需图形库（库依赖不变）；不宣 ServerCore / Session 0 / 注销 / 跨用户，也不外推其它模型。公开外部契约仍是 `tests/contracts/test_native_cli_launch_e2e.py`（默认跳过，需显式真实原生 fixture，一次普通 CSV 请求、消耗一次 license）；OS 生命周期 / 窗口站 / DACL / 句柄清理细节由一次性私有验证器覆盖，不进入永久测试。
