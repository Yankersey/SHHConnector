# Windows 原生运行

原生版直接使用 Windows Python、ConPTY 和 Windows OpenSSH，无需 WSL、Ubuntu 或虚拟机。macOS / Linux 继续运行原来的 `sh start.sh`。WSL 方式见 [WINDOWS-WSL.md](WINDOWS-WSL.md)，入口改为 `start-wsl.cmd`。

## 安装与启动

1. 安装 Windows Python 3.9–3.13（推荐 3.12 / 3.13），确保 PowerShell 中 `python --version` 可用。Windows 需支持 ConPTY（Windows 10 1809+ / Windows 11）。
2. 确认 `ssh -V` 可用。优先使用 Windows 自带的 OpenSSH Client，无需 SSH Server。
3. 在项目文件夹双击 **setup-windows.cmd**。它会创建本项目的 `.venv` 并安装 `requirements-windows.txt` 中的 pywinpty，然后启动程序。不需要管理员权限。
4. 以后双击 **start-windows.cmd**。程序会检查 Git 更新并打开 Windows 默认浏览器；退出请使用工作台里的“退出程序”。

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

Windows OpenSSH 不支持 ControlMaster，因此普通终端各自建立 SSH 连接。文件列表、下载、核对会话、诊断和显式结束 screen 使用独立的非交互连接，**需要可用的 SSH 私钥或 ssh-agent 认证**。在一个终端中输入过密码 / MFA，不会让其他连接自动获得认证；失败时保留会话与下载断点，不自动结束远程 screen。仅使用密码 / MFA 的服务器，可继续使用 WSL 方式获得原来的连接复用能力。

SSH 是否经过代理由现有 SSH 配置和网络路由决定。Windows 的 HTTP/HTTPS 系统代理不会自动代理 SSH TCP 流量；已有的反代地址、ProxyCommand 或 TUN 路由继续按系统 SSH 的行为工作。程序不更改任何代理设置。可先在 Windows PowerShell 用 `ssh 你的Host别名` 验证原来的连接。

## 数据和双机同步

Windows 的布局与断点保存在项目的 `data/`，文件下载到 `downloads/`；“在本机显示”打开 Windows 文件资源管理器。两台电脑各自保留 SSH 配置、密钥、布局和下载数据，只通过 Git 同步代码。

另一台电脑退出旧程序后重新运行启动器，会快进更新到 `origin/main`。Mac 继续用 `启动 ScreenDesk.command` / `sh start.sh`；Windows 使用 `start-windows.cmd`。

## 验证范围

Windows 自动测试覆盖 ConPTY 中文输入输出与大小调整、原生 HTTP 启动、令牌检查、单实例锁、下载管道超时 / 暂停、共享转发连接的生命周期、SSH Include 路径和 Git 更新保护。Ubuntu 测试继续覆盖实际 GNU screen 和本机 SFTP 集成，不访问用户真实服务器。

参考：[Windows OpenSSH 功能范围](https://github.com/PowerShell/Win32-OpenSSH/wiki/Project-Scope)、[pywinpty](https://github.com/andfoy/pywinpty)。
