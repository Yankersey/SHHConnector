# Windows 原生运行

原生版直接使用 Windows Python、ConPTY 和 Windows OpenSSH，无需 WSL、Ubuntu 或虚拟机。macOS / Linux 继续运行原来的 `sh start.sh`。WSL 方式见 [WINDOWS-WSL.md](WINDOWS-WSL.md)，入口改为 `start-wsl.cmd`。

## 安装与启动

1. 安装 Windows Python 3.9–3.13（推荐 3.12 / 3.13），确保 PowerShell 中 `python --version` 可用。Windows 需支持 ConPTY（Windows 10 1809+ / Windows 11）。
2. 确认 `ssh -V` 可用。优先使用 Windows 自带的 OpenSSH Client，无需 SSH Server。
3. 在项目文件夹双击 **setup-windows.cmd**。它会创建本项目的 `.venv`，安装 pywinpty 和 pywebview，生成桌面和开始菜单入口，然后打开独立窗口。不需要管理员权限。窗口使用 Microsoft Edge WebView2 Runtime（Windows 11 通常已安装；缺少时需先安装微软的 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)）。
4. 以后从桌面或开始菜单打开 **ScreenDesk**。入口指向本机生成的 `data/desktop/ScreenDesk.exe`，使用 `pythonw.exe` 和隐藏的 Python 后台；没有命令行、启动日志或浏览器标签页。只有安装依赖时显示安装窗口。
5. 关闭窗口后，后台的连接与下载继续运行；再次打开图标会回到已有工作台，重复双击不会重复启动窗口。完整退出请使用工作台里的“退出程序”，后台退出后桌面窗口也会关闭。下次打开时检查 Git 更新。

已在用 ScreenDesk 的电脑，更新后运行一次 **setup-windows.cmd** 即可补齐桌面依赖和更新旧快捷方式。只需重新创建快捷方式时运行 **install-windows-shortcuts.cmd**。移动项目文件夹后，从新位置再次运行安装脚本。这个轻量 exe 依赖项目文件夹和 `.venv`，请完整保留；无需打包全部 Python 环境，也不上传本机生成的 exe。macOS 的应用入口保持原有方式。

`start-windows.cmd` 和 `python launcher.py` 保留带控制台 / 默认浏览器的开发入口；日常使用桌面图标。桌面入口沿用同一个后台、Git 更新机制、SSH 配置和数据，打开前已有后台时直接复用。桌面窗口只允许访问当前本机工作台，不允许跳转到外站或本地文件。

也可在项目目录的 PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe launcher.py
```

开发 / 离线时显式使用 `start-windows.cmd --offline`。下载版本不包含 `.git` 时不检查更新。Git 检查遇到未提交修改、分叉或网络失败会停止启动并保留代码与本机数据。

## 沿用 Windows SSH 配置与转发

默认读取 **`%USERPROFILE%\.ssh\config`**，包括已有 Host、Include、IdentityFile、ProxyJump、ProxyCommand、LocalForward、RemoteForward 和 DynamicForward。无需迁移到 WSL，不修改这个文件，也不会复制或上传密钥。自定义配置可用 `start-windows.cmd --ssh-config "C:\path with spaces\config"`。

窗口里仍按原来的方式选择“启用 / 不启用端口转发”。启用时，一个独立的 Windows SSH 连接承载该服务器配置中的转发；多个启用窗口共享它，不反复绑定相同端口。分离 / 关闭一个窗口不会释放其他窗口使用的转发；关闭整个服务器或退出程序才会释放。转发登录提示会显示在首次启用它的终端中，之后该终端继续连接 screen；使用密码时这两个连接可能分别要求登录。

Windows OpenSSH 不支持 ControlMaster，因此普通终端各自建立 SSH 连接。扫描 screen、文件列表、下载、核对会话、诊断和显式结束 screen 使用独立连接，**没有公钥也可以使用**：需要时界面会弹出“SSH 身份认证”，输入该服务器的密码、私钥口令或验证码，完成后原操作继续。不需要先打开终端登录，也不需要 WSL。

密码不写入磁盘、参数或日志，不缓存；每个独立连接可能需要重新认证。首次主机指纹需在对话框中明确确认，变更的主机密钥仍会被拒绝。取消或认证失败会暂停下载并保留断点，稍后点击“继续”重试，不无限弹窗。认证助手使用本项目 Python 环境中 pip 自带的标准 Windows 启动器，在私有临时目录生成；正常使用 `setup-windows.cmd` 创建的环境即可，无需管理员权限或新增全局程序。

SSH 是否经过代理由现有 SSH 配置和网络路由决定。Windows 的 HTTP/HTTPS 系统代理不会自动代理 SSH TCP 流量；已有的反代地址、ProxyCommand 或 TUN 路由继续按系统 SSH 的行为工作。程序不更改任何代理设置。可先在 Windows PowerShell 用 `ssh 你的Host别名` 验证原来的连接。

## 复制与粘贴

终端中选中文字后，**Ctrl+C** 复制；未选中文字时，Ctrl+C 仍会发送中断。**Ctrl+V** 粘贴，Ctrl+Shift+C / Ctrl+Shift+V 也可使用。右键菜单提供复制、粘贴等文字编辑操作；终端内启用了鼠标的 vim 等程序中，可按住 Shift 拖动选择本地文字。复制在只读观看端也可使用，粘贴需要当前端持有操作权。粘贴沿用 xterm 的 bracketed paste，不会额外发送回车。

## 数据和双机同步

Windows 的布局与断点保存在项目的 `data/`，文件下载到 `downloads/`；“在本机显示”打开 Windows 文件资源管理器。两台电脑各自保留 SSH 配置、密钥、布局和下载数据，只通过 Git 同步代码。

另一台电脑退出旧程序后重新打开 ScreenDesk，会快进更新到 `origin/main`。Mac 和 Windows 的应用入口都沿用项目里的启动脚本。

## 验证范围

Windows 自动测试覆盖 GUI exe / 桌面快捷方式启动、真实 WebView2 渲染和导航限制、后台复用与窗口关闭保留、ConPTY 中文输入输出与大小调整、原生 HTTP 启动、令牌检查、单实例锁、下载管道超时 / 暂停、共享转发连接、SSH Include 路径和 Git 更新保护。Windows、macOS、Ubuntu 均使用真实系统 OpenSSH 验证密码 / MFA 和 SFTP；额外检查取消、错误密码、认证超时、来源鉴权和主机密钥变更拒绝，不访问用户真实服务器。

参考：[Windows OpenSSH 功能范围](https://github.com/PowerShell/Win32-OpenSSH/wiki/Project-Scope)、[pywinpty](https://github.com/andfoy/pywinpty)。
