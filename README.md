# colmon

Colpilot 的持续运行插件，启动并管理一个独立的 WinForms 窗口宿主。窗口宿主负责创建三个任务栏窗口：

- Codex weekly
- Codex 5hlimit
- Tokens today

## 功能

插件页面显示三个窗口的当前数据、更新时间、状态和刷新频率。信息更新频率支持 10 到 3600 秒，保存后会重新启动原生窗口宿主。

Codex 配额查询会按需创建短时 App Server 进程。原生窗口宿主和查询进程都由 Colpilot 的进程管理器登记；插件停止或 Colpilot 退出时会按顺序关闭这些进程。

## 启动要求

插件运行于 Windows。首次启动时，如果插件目录中没有原生窗口宿主程序，插件会使用 .NET 10 SDK 自动编译宿主，然后启动三个任务栏窗口。首次编译可能需要联网下载依赖，编译期间插件显示启动状态。

编译成功的宿主保存在 `native/publish` 中，后续启动直接使用该程序。编译日志保存在 `data/artifacts/native-build.log` 中。自动编译进程由 Colpilot 管理，退出程序时会一同结束。

## 配置

刷新频率保存在插件目录的 config.ini 中的 [colmon] 配置节。三个窗口的状态文件保存在插件目录的 data 目录中。

状态文件在进程内共用的读写锁中完整更新，状态接口在写入完成后读取 JSON。状态更新保留原文件，允许普通读取句柄持续打开。程序启动后会重新生成窗口状态。

## 文件结构

- backend.py：插件后端和状态接口
- native_host.py：原生窗口宿主的生命周期管理
- taskbar.py：任务栏窗口和 Codex 数据读取逻辑
- native：WinForms 原生窗口宿主项目
- ui：Colpilot 前端页面
- data：窗口状态文件

## 运行日志

插件在主程序的 `data/logs/backend.log` 记录窗口宿主生命周期、通信错误、数据查询错误和每分钟的运行状态。原生窗口日志保存在本插件的 `data/logs/native-日期.log`，每个文件达到 5 MB 后轮换，最多保留 14 个文件。独立检查线程每 5 秒检查窗口线程，超过 5 秒没有响应时记录当前操作。日志在程序重新启动后继续保留。

宿主输出由独立线程发送，窗口线程将消息提交至容量为 256 条的队列。输出操作超过 5 秒时，独立检查线程记录一次错误，由主程序按 15 秒没有收到状态判定宿主异常并结束该进程。队列容量不足、状态文件保存失败或宿主输出处理失败时立即终止相关处理，插件页面显示错误原因。

## 服务接口

- GET /api/plugins/colmon/status：读取三个任务栏窗口的状态、数据和更新时间
- POST /api/plugins/colmon/settings：保存信息更新频率

## 参考项目

任务栏窗口的展示方式参考 [TrafficMonitor](https://github.com/zhongyang219/TrafficMonitor)。

# colpilot.colmon
