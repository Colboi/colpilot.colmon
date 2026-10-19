# colmon

Colpilot 的持续运行插件，启动并管理一个独立的 WinForms 窗口宿主。窗口宿主负责创建三个任务栏窗口：

- Codex weekly
- Codex 5hlimit
- Tokens today

## 功能

插件页面显示三个窗口的当前数据、更新时间、状态和刷新频率。信息更新频率支持 10 到 3600 秒，保存后会重新启动原生窗口宿主。

Codex 配额查询会按需创建短时 App Server 进程。原生窗口宿主和查询进程都由 Colpilot 的进程管理器登记；插件停止或 Colpilot 退出时会按顺序关闭这些进程。

## 配置

刷新频率保存在插件目录的 config.ini 中的 [colmon] 配置节。三个窗口的状态文件保存在插件目录的 data 目录中。

## 文件结构

- backend.py：插件后端和状态接口
- native_host.py：原生窗口宿主的生命周期管理
- taskbar.py：任务栏窗口和 Codex 数据读取逻辑
- native：WinForms 原生窗口宿主项目
- ui：Colpilot 前端页面
- data：窗口状态文件

## 服务接口

- GET /api/plugins/colmon/status：读取三个任务栏窗口的状态、数据和更新时间
- POST /api/plugins/colmon/settings：保存信息更新频率

## 参考项目

任务栏窗口的展示方式参考 [TrafficMonitor](https://github.com/zhongyang219/TrafficMonitor)。

# colpilot.colmon
