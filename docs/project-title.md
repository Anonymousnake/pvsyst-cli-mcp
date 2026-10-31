# 项目显示标题（Project Display Title）

在 PVsyst 中，项目列表、工程编辑窗口以及仿真導出的 CSV/PDF 报告顶部显示的“项目名称”，并不是 `.PRJ` 的文件名，而是 `.PRJ` 文本里根对象 `PVObject_=pvProject` 头部的 `Comment=` 字段。

`pvsyst_update_project_parameters` 专门用来安全地修改这个根级 `Comment` 显示标题（不会改动 `.PRJ`/`.VCx` 文件名、变体标题或嵌套子对象里的 `Comment`）。

## 参数说明

| 参数名 | 说明与要求 |
|---|---|
| `project` | `Projects` 目录下的 `.PRJ` 文件名 |
| `expected_files` | 最近一次调 `pvsyst_inspect_project` 返回的完整 `files` 哈希字典（包含 `.PRJ` 和该工程下所有 `.VCx`） |
| `project_name` | 单行可打印字符串，长度 `1..80` UTF-8 字节，首尾不能带空格 |
| `dry_run` | 布尔值，默认 `false`；传 `true` 时只返回预览 diff，不写盘 |
| `add_utf8_bom` | 布尔值，默认 `false`；当标题含中文等非 ASCII 字符且原 `.PRJ` 无 BOM 时，需显式传 `true` |

## 中文乱码问题与 UTF-8 BOM

PVsyst 8.1.6 在读取 `.PRJ` 和 `.VCx` 时有个老毛病：如果文件开头没有 3 字节的 UTF-8 BOM（`EF BB BF`），哪怕文件内容本身是合法的 UTF-8，PVsyst 的 GUI 和 CLI 也会按西欧单字节编码（类似 `cp1252`）去解码，导致中文标题在软件列表和导出的 PDF 报告里全部变成乱码。

因此：
- 当你传入包含中文等非 ASCII 字符的 `project_name` 时，如果原 `.PRJ` 没有 UTF-8 BOM，工具会要求你显式传入 `add_utf8_bom=true`（并在返回结果中标记 `bom_added=true`）。
- 如果原文件已经是 UTF-8 中文但因为没 BOM 在 PVsyst 里乱码，你也可以传入相同的 `project_name` 加 `add_utf8_bom=true`，只给文件补上 BOM 头（此时文本 `diff` 为空，但 `changed=true`、`bom_added=true`）。

## 原生变体写出时的 BOM 规则（Native import / 新变体）

同一套“无 BOM 即按 cp1252 读”的规则也适用于 `.VCx` 变体。所有**新写出/重建的原生变体**（native import，以及 load / grid-storage / grid-storage-system 的新变体 writer）现在按**最终正文内容**判断 BOM，而不是只看源文件或显示名：

- 源变体本来有 BOM → 保留；
- 显示名（`variant_name`）含非 ASCII → 需源文件已有 BOM 或显式 `add_utf8_bom=true`，否则报错；满足条件则保留或补上 BOM；
- 变体正文本身含非 ASCII（例如从真实 8.0 工程继承来的定倾面注释 `Comment=Fixed, Tilt 25.0°, Azim. 20.0°`）→ 也加 BOM。

原因是：如果只按显示名判断，一个 ASCII 显示名 + 正文含度数符号的变体会被写成“无 BOM 的 UTF-8”，原始软件随后按 cp1252 读回，`°` 变成 `Â°`，再保存就变成双重编码（round17 观测到的乱码路径）。只有纯 ASCII 正文的旧变体继续保持无 BOM 形态，不会被无谓改写。

这条规则只作用于**新写出/重建**的变体；对既有文件的显示名或注释编辑仍然只替换目标行，BOM、CRLF、未知对象与仿真缓存原样保留。它不是对全部文件的批量迁移：CSV、天气档与既有变体的就地编辑都不受影响。对应的公共契约见 `tests/contracts/test_native_text_writer_encoding_boundary.py`。

## 推荐操作与回滚步骤

1. 如果该工程正在 PVsyst 桌面 GUI 中打开，先在 GUI 中关闭它，防止稍后 GUI 把内存里的旧状态重新写盘覆盖。
2. 调用 `pvsyst_inspect_project`，拿到最新的 `files` 哈希字典、当前 `project_name` 和 `utf8_bom` 状态。
3. 先用 `dry_run=true` 调 `pvsyst_update_project_parameters` 看一眼 `diff` 和 `name_change`。
4. 确认无误后，带着同一份 `expected_files` 和 `dry_run=false` 正式执行修改。工具会备份整套 `.PRJ`/`.VCx`，但只改写 `.PRJ` 文件本身，不会碰各个 `.VCx` 的修改时间和文件内容。
5. 如需回滚到修改前的状态（包括还原最初有无 BOM 的字节状态），将返回的 `backups` 和当前 `files` 传给 `pvsyst_restore_project_sources(..., confirm=true)`。
