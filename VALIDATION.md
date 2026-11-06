# 验证记录

2026-11-06，0.7.0 终端滚动与主题：本机 55 项 Python 回归测试完成（54 通过，1 项 Windows 路径检查按平台跳过）；鼠标协议和终端主题 / 滚轮 / 快捷键 JavaScript 测试通过。隔离 GNU screen 验证了回看包含当前显示上方的历史、读取后会话仍保持分离状态、临时快照清理，以及远程失败不会改变会话状态。浏览器实际验证了向上滚轮回看、继续滚动、返回实时终端、Shift+PageUp / Esc、日间白底 / 夜间黑底切换与刷新后记住设置。未连接用户云服务器。

2026-11-05，macOS / Python 3.9.6。

- 14 项 Python 集成测试通过。覆盖端口转发选择、SSH 认证提示、目录分页、SFTP 实际中断后的续传、暂停与重启、文件变化、HTTP 来源校验、screen / WebSocket 断开、服务器切换、逐项关闭、布局恢复、SSH config 导入、慢显示端、远程操作超时、本机会话核对与移除。
- macOS 自带 GNU screen 4.00.03 完整测试通过；工作目录中临时编译的 GNU screen 4.9.1 也完成了终端、断线和慢显示端验证。未安装或替换系统 screen。
- 68 项鼠标协议检查通过，包括跨数据块的鼠标模式、SGR / X10 输入、混合备用屏幕模式、OSC / DCS 字符串，以及用户主动启用远程鼠标。
- 隔离本机 OpenSSH 服务验证通过：四个真实 SSH 窗口分别选择关闭 / 开启 / 开启 / 关闭转发，连接可共存；screen 状态在断开后恢复；SFTP 下载文件与源文件一致。全部使用临时测试密钥和回环端口。
- 静态界面的脚本语法、DOM 引用与 ID 唯一性检查通过。较早版本已实际操作验证拖动、缩放、服务器切换、分离 / 关闭和恢复布局；最后的文件抽屉及会话核对界面未做浏览器自动操作验证，因为当前 Work 设置关闭了本地浏览器控制。
- 已确认本机默认 SSH config 可以解析到 11 个具体 Host 别名，未输出或打包其内容。

Git / Windows 入口更新：本机 20 项 Python 测试通过，新增覆盖远程快进更新、本机数据保留、未提交修改、未推送提交 / 分支分叉、远程不可达，以及 WSL 浏览器与文件夹打开的模拟检查。仓库提供 Ubuntu / Python 3.9、3.12 的 GitHub Actions 流程。当前验证电脑为 macOS，Windows WSL 启动器尚未在 Windows 设备实际运行。

没有连接用户的云服务器。对已存在的坏 socket，没有执行删除、kill 或 quit；程序提供有时限、只针对当前会话的诊断与核对。

运行 `SCREENDESK_TEST_SCREEN=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` 可重跑 Python 测试。若安装了 Node，运行 `node tests/test_mouse.mjs` 可重跑鼠标协议测试。
